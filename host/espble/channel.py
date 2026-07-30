"""RetainedChannel —— 在 BLE 上补出 MQTT 的两个语义。

从 MQTT 换到 BLE 直连时会突然发现少了两样东西,而且是设备行为里最显眼的两样:

  - **retained**:设备一上电就该看到当前状态,而不是空屏等下一次变化。
  - **QoS1 离线队列**:设备重启后历史列表不该是空的。

BLE 两样都没有,所以在这里用内存态补:

  - `set_retained(key, obj)` —— 每次(重)连上按插入顺序补推一遍最新值
  - `publish(obj)` —— 进历史环形队列,重连后重放;重放时打上标记,
                       让设备知道「这是补的,别再蜂鸣/弹卡一次」

刻意**不做**的事:未连接时的即时消息不排队,直接丢。攒着的消息重连后一次灌爆
设备,比丢了更糟。真正重要的状态应该是 retained,重要的事件靠 history 补 ——
这两条已经覆盖了「设备重启后看起来是对的」这个真实需求。
"""
from __future__ import annotations

import collections
import threading
from typing import Any, Callable, Dict, Mapping, Optional

from .framing import DEFAULT_LINE_LIMIT, fit_line
from .link import BleLink


class RetainedChannel:
    def __init__(self, link: BleLink, *,
                 history_n: int = 8,
                 line_limit: int = DEFAULT_LINE_LIMIT,
                 shrink_field: str = "msg",
                 replay_transform: Optional[Callable[[Mapping[str, Any]], Mapping[str, Any]]] = None,
                 keepalive_key: Optional[str] = None):
        """
        replay_transform: 重放历史事件时的改写钩子。默认加 `"live": False` ——
            设备侧据此只把它放进列表,不蜂鸣、不弹全屏卡。
        keepalive_key: 用哪个 retained 键当 keepalive 帧。None 表示用第一个。
            这个帧会被反复重发,所以设备侧必须能识别「内容没变就别重绘」
            (通常靠一个 rev/版本字段),否则墨水屏之类会被刷爆。
        """
        self.link = link
        self.line_limit = line_limit
        self.shrink_field = shrink_field
        self.replay_transform = replay_transform or self._default_replay_transform
        self.keepalive_key = keepalive_key

        self._lock = threading.Lock()
        self._retained: Dict[str, dict] = {}
        self._history: collections.deque = collections.deque(maxlen=history_n)

        link.on_connect = self._on_connect
        link.keepalive_provider = self._keepalive_line
        # 接好回调**之后**才启动监护线程。反过来的话,链路可能在 on_connect
        # 被挂上之前就连上了,首次连接就错过补推。
        # 所以配套的 BleLink 应当用 autostart=False 构造;
        # 已经在跑的 link 调 start() 是幂等的,不会起第二个线程。
        link.start()

    # ---- 对外 ----

    @property
    def connected(self) -> bool:
        return self.link.connected

    def set_retained(self, key: str, obj: Mapping[str, Any]):
        """设置一个「设备一连上就该看到」的状态帧,并立即尝试推送。"""
        with self._lock:
            self._retained[key] = dict(obj)
        self.link.send_soon(self._encode(obj))

    def publish(self, obj: Mapping[str, Any]):
        """发一条事件:进历史(供重连补发)+ 尝试立即推送。"""
        with self._lock:
            self._history.append(dict(obj))
        self.link.send_soon(self._encode(obj))

    def send_now(self, obj: Mapping[str, Any], *, queue_while_offline: bool = False):
        """一次性消息:不进历史、不 retained。断连时默认直接丢。

        queue_while_offline=True 用于「值得等」的指令(例如 ota),它们排队等重连。
        """
        self.link.send_soon(self._encode(obj), queue_while_offline=queue_while_offline)

    def close(self):
        self.link.close()

    # ---- 内部 ----

    def _encode(self, obj: Mapping[str, Any]) -> str:
        return fit_line(obj, self.line_limit, self.shrink_field)

    @staticmethod
    def _default_replay_transform(obj: Mapping[str, Any]) -> Mapping[str, Any]:
        out = dict(obj)
        out["live"] = False
        return out

    def _keepalive_line(self) -> Optional[str]:
        with self._lock:
            if not self._retained:
                return None
            if self.keepalive_key is not None:
                obj = self._retained.get(self.keepalive_key)
            else:
                obj = next(iter(self._retained.values()))
        return self._encode(obj) if obj else None

    def _on_connect(self, link: BleLink):
        """连上后的补发 = MQTT 的 retained + 离线队列重放。

        注意顺序:先 retained 后 history。设备通常先画状态再画列表,
        反过来会看到一瞬间的空状态。
        """
        with self._lock:
            retained = list(self._retained.values())
            history = list(self._history)
        # 断连期间攒下的即时消息作废 —— 它们的内容已经被 retained/history 覆盖了,
        # 不清掉就会和下面的补推重复。
        link.clear_outbox()

        for obj in retained:
            link.send_blocking(self._encode(obj))
        for obj in history:      # 旧 → 新
            if not link.send_blocking(self._encode(self.replay_transform(obj))):
                break            # 链路又断了,剩下的下次重连再补
