"""BleHub —— 一个 bundle,多台设备,每台一个进程。

    BleHub
      ├─ BleLink(paper)      → helper 进程 A  --session-dir …/sessions/c119cc
      ├─ BleLink(cardputer)  → helper 进程 B  --session-dir …/sessions/7a1b02
      └─ BleLink(stick)      → helper 进程 C  --session-dir …/sessions/d63f91
             共用同一个 .app(一次 TCC 授权),各自独立的 CBCentralManager

============================================================================
为什么是「共用 bundle、不共用进程」
============================================================================
一个 CBCentralManager 完全可以同时连多台 peripheral,CoreBluetooth 支持。
但那会毁掉这个框架的地基:**CoreBluetooth 的坏状态是进程级的,恢复只能靠丢掉
进程**。一个进程管 N 台设备,就意味着一台把进程搞坏、N 台一起挂。

⚠️⚠️ **下面这段推理是错的,留在这里是因为它错得很贵。看 docs/pitfalls.md C10。**

> 而「一个 bundle 只能有一个进程」是个误解 —— `open -n` 的语义本来就是强制新实例。
> 所以:一个 bundle → 一次 TCC 授权;N 个实例 → N 个进程 → N 个独立 central。
> 两个好处都要得到。(已实测:同 bundle 两实例各自拿到 central_state:5,互不干扰。)

那个"实测"只验了两边都能**开机**(`central_state:5`),没验两边都**收得到扫描结果**。
实测三格对照(同一份二进制、同一个 worker,只改 bundle id):**同 id 的第二个进程
一个 `didDiscover` 都收不到**,换个 id 立刻正常。和有没有创建 central 无关。

**后果:`register()` 起 N 个同 bundle 的 worker,对 N≥2 是坏的** —— 第二台之后的
worker 永远扫不到自己那台设备,表现成"链路疯狂 flapping"。当前只有 1 台设备在用,
所以还没暴露。方向已定:改成**一个 worker 进程管 N 个 peripheral**(CoreBluetooth
本来支持),代价是放弃「坏状态是进程级、丢进程就能恢复」那条隔离 —— 那是个真取舍,
不是免费的。

bundle 唯一性除了解决**不同产品之间**撞车(CodeBuddy / claude-stick / 本框架),
现在还多了一条:**同一个 id 同时只能有一个活进程**。

============================================================================
路由在主机侧,ROM 不需要 targetId
============================================================================
每条 BLE 链路本来就是点对点的:hub 按 target 选中哪个 BleLink,消息发出去时
对端只有一个。broadcast 就是 for-each-link 发一遍。所以固件那边只需要能
**报出自己是谁**(hello 帧),不需要在消息里带路由字段。
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional

from .channel import RetainedChannel
from .framing import DEFAULT_LINE_LIMIT, fit_line
from .helper_session import DeviceConfig
from .link import BleLink
from .registry import DEFAULT_PATH, DeviceRecord, DeviceRegistry

DEFAULT_SESSION_ROOT = os.path.expanduser("~/.config/espble/sessions")


def _log(*a):
    print("[hub]", *a, file=sys.stderr, flush=True)


class DeviceLink:
    """一台设备的全部东西:注册记录 + 链路 + retained/history 语义。"""

    def __init__(self, record: DeviceRecord, link: BleLink, channel: RetainedChannel):
        self.record = record
        self.link = link
        self.channel = channel

    @property
    def connected(self) -> bool:
        return self.link.connected

    @property
    def label(self) -> str:
        return self.record.label


class BleHub:
    def __init__(self, app_path: str, *,
                 device_type: str = "",
                 registry_path: str = DEFAULT_PATH,
                 session_root: str = DEFAULT_SESSION_ROOT,
                 # ---- 投递语义:单点与广播分开配 ----
                 # 默认值的理由:单点多是状态帧(过期即无用,重连有 retained 补),
                 # 广播多是指令(值得等)。两者性质不同,所以不共用一个开关。
                 unicast_queue_offline: bool = False,
                 broadcast_queue_offline: bool = True,
                 history_n: int = 8,
                 line_limit: int = DEFAULT_LINE_LIMIT,
                 keepalive_sec: float = 30.0,
                 reconnect_sec: float = 5.0,
                 backoff_max_sec: float = 45.0,
                 scan_timeout: float = 20.0,
                 on_message: Optional[Callable[[DeviceRecord, str], None]] = None,
                 on_device_change: Optional[Callable[[DeviceRecord, str], None]] = None):
        self.app_path = os.path.abspath(os.path.expanduser(app_path))
        self.device_type = device_type
        self.session_root = os.path.expanduser(session_root)
        self.unicast_queue_offline = unicast_queue_offline
        self.broadcast_queue_offline = broadcast_queue_offline
        self.history_n = history_n
        self.line_limit = line_limit
        self.keepalive_sec = keepalive_sec
        self.reconnect_sec = reconnect_sec
        self.backoff_max_sec = backoff_max_sec
        self.scan_timeout = scan_timeout
        self.on_message = on_message
        self.on_device_change = on_device_change

        self.registry = DeviceRegistry(registry_path)
        self._links: Dict[str, DeviceLink] = {}
        self._lock = threading.Lock()

    # ---- 注册 ----

    def register(self, device_id: str, alias: str = "", *,
                 device_type: str = "", start: bool = True) -> DeviceLink:
        """登记一台设备并(默认)立刻开始维护它的链路。

        设备名不用给 —— 按 `<type>-<id>` 算得出来,这正是固件那边的拼法。
        """
        dtype = device_type or self.device_type
        rec = self.registry.add(device_id, device_type=dtype, alias=alias)
        with self._lock:
            if rec.device_id in self._links:
                return self._links[rec.device_id]

        session_dir = os.path.join(self.session_root, rec.device_id)
        device = DeviceConfig(
            app_path=self.app_path,
            # ⚠️ session-dir 按 device_id 分,这是多实例互不干扰的关键:
            #    HelperSession._kill() 按 session-dir 匹配进程,分错了就会误杀兄弟。
            session_dir=session_dir,
            device_name=rec.name,
            scan_timeout=self.scan_timeout,
        )
        link = BleLink(
            device,
            reconnect_sec=self.reconnect_sec,
            backoff_max_sec=self.backoff_max_sec,
            keepalive_sec=self.keepalive_sec,
            on_notification=lambda line, _id=rec.device_id: self._on_line(_id, line),
            on_disconnect=lambda _id=rec.device_id: self._notify_change(_id, "disconnected"),
            autostart=False,
        )
        channel = RetainedChannel(link, history_n=self.history_n,
                                  line_limit=self.line_limit, keepalive_key=None)
        # RetainedChannel 的构造里会 link.start(),所以「注册即开始维护」是默认行为。
        # start=False 时把它停掉 —— 用于「先登记一批、稍后统一拉起」。
        if not start:
            link.close()

        dl = DeviceLink(rec, link, channel)
        with self._lock:
            self._links[rec.device_id] = dl
        self._notify_change(rec.device_id, "registered")
        return dl

    def alias(self, device_id: str, alias: str) -> DeviceRecord:
        return self.registry.set_alias(device_id, alias)

    def unregister(self, target: str) -> bool:
        dl = self._resolve(target)
        if dl is None:
            return False
        with self._lock:
            self._links.pop(dl.record.device_id, None)
        dl.link.close()
        return self.registry.remove(dl.record.device_id)

    def start_all(self):
        for dl in list(self._links.values()):
            dl.link.start()

    def adopt_registry(self, *, start: bool = True) -> List[str]:
        """把注册表里已知的设备全部重新登记,返回这次新接管的 device_id。

        ⚠️ 这个方法存在的理由:`__init__` 只是**打开**注册表,`_links` 是空的,
        而 `start_all()` 遍历的也是 `_links`。也就是说 hub 进程一重启就"忘了"
        所有设备,得靠调用方把 register 再走一遍 —— 而 supervisor 重启是常态
        (launchd 拉起、改配置、崩溃恢复)。忘了调它的现象是:注册表里明明有设备,
        菜单栏也列得出来,但没有任何 worker 在跑、一台都连不上。

        幂等:register() 遇到已在 _links 里的 device_id 直接返回现有 link。
        """
        adopted = []
        for rec in list(self.registry):
            if rec.device_id in self._links:
                continue
            self.register(rec.device_id, rec.alias,
                          device_type=rec.device_type, start=start)
            adopted.append(rec.device_id)
        if adopted:
            _log(f"从注册表接管 {len(adopted)} 台:{', '.join(adopted)}")
        return adopted

    # ---- 发送 ----

    def send(self, target: str, obj: Mapping[str, Any], *,
             queue_offline: Optional[bool] = None) -> bool:
        """发给一台设备。target 是 id 或别名。

        queue_offline 不给时用 hub 的 unicast_queue_offline 默认值。
        """
        dl = self._resolve(target)
        if dl is None:
            _log(f"send: 找不到设备 {target!r}")
            return False
        q = self.unicast_queue_offline if queue_offline is None else queue_offline
        dl.channel.send_now(obj, queue_while_offline=q)
        return True

    def broadcast(self, obj: Mapping[str, Any], *,
                  cap: str = "", device_type: str = "",
                  queue_offline: Optional[bool] = None) -> List[str]:
        """发给所有(可按 cap / 类型过滤)设备,返回实际发到的设备 id。

        cap 过滤靠设备 hello 上报的能力表:设备没上报过 hello 时**按支持处理** ——
        宁可多发一条它会忽略的消息,也不要因为握手没到就静默漏发。
        """
        q = self.broadcast_queue_offline if queue_offline is None else queue_offline
        sent = []
        for dl in list(self._links.values()):
            rec = dl.record
            if device_type and rec.device_type != device_type:
                continue
            if cap and not rec.supports(cap):
                continue
            dl.channel.send_now(obj, queue_while_offline=q)
            sent.append(rec.device_id)
        return sent

    def set_retained(self, target: str, key: str, obj: Mapping[str, Any]) -> bool:
        """设一台设备的 retained 状态:它每次(重)连上都会被补推。"""
        dl = self._resolve(target)
        if dl is None:
            return False
        dl.channel.set_retained(key, obj)
        return True

    def set_retained_all(self, key: str, obj: Mapping[str, Any]) -> List[str]:
        """给**每台**设备设同一个 retained 状态,返回设到的设备 id。

        看板这类"最新状态"就该用它:每台设备(重)连上都会被补推最新一帧。
        fan-out 放在框架里而不是让每个消费方自己写 for 循环 —— broadcast() 已经是这个路子。
        """
        done = []
        for dl in list(self._links.values()):
            dl.channel.set_retained(key, obj)
            done.append(dl.record.device_id)
        return done

    def publish(self, target: str, obj: Mapping[str, Any]) -> bool:
        """发一条事件:进该设备的历史环,重连时以 live=false 重放。"""
        dl = self._resolve(target)
        if dl is None:
            return False
        dl.channel.publish(obj)
        return True

    def publish_all(self, obj: Mapping[str, Any]) -> List[str]:
        """把一条事件发给**每台**设备,并进各自的历史环。

        ⚠️ 别拿 broadcast() 代替这个。broadcast() 走 channel.send_now ——
        只做"现在发出去"(加可选的离线排队),**不进历史环**;
        而 publish() 才进历史环、重连时以 live=false 重放。
        事件要的是后者:设备重连后应该能看到它离线期间错过的那几条。
        """
        done = []
        for dl in list(self._links.values()):
            dl.channel.publish(obj)
            done.append(dl.record.device_id)
        return done

    # ---- 状态 ----

    def status(self) -> Dict[str, dict]:
        out = {}
        for dl in list(self._links.values()):
            r = dl.record
            out[r.device_id] = {
                "alias": r.alias, "type": r.device_type, "name": r.name,
                "fw": r.fw, "caps": r.caps,
                "connected": dl.connected,
                "fatal": dl.link.fatal_error,
            }
        return out

    def close(self):
        for dl in list(self._links.values()):
            try:
                dl.link.close()
            except Exception as exc:        # noqa: BLE001
                _log(f"关闭 {dl.label} 出错: {exc!r}")
        self._links.clear()

    # ---- 内部 ----

    def _resolve(self, target: str) -> Optional[DeviceLink]:
        rec = self.registry.get(target)
        if rec is None:
            return None
        return self._links.get(rec.device_id)

    def _on_line(self, device_id: str, line: str):
        """设备 notify 上来的一行。hello 由框架吃掉,其余原样交给应用。"""
        if '"hello"' in line:
            try:
                import json
                payload = json.loads(line)
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get("t") == "hello":
                rec = self.registry.absorb_hello(payload)
                if rec is not None:
                    _log(f"hello: {rec.label} type={rec.device_type} "
                         f"fw={rec.fw} caps={rec.caps}")
                    self._notify_change(rec.device_id, "hello")
                return          # 框架自己的协议,不往应用层转发
        if self.on_message:
            rec = self.registry.get(device_id)
            if rec is not None:
                try:
                    self.on_message(rec, line)
                except Exception as exc:    # noqa: BLE001 —— 回调是应用代码
                    _log(f"on_message 回调抛异常: {exc!r}")

    def _notify_change(self, device_id: str, what: str):
        if not self.on_device_change:
            return
        rec = self.registry.get(device_id)
        if rec is None:
            return
        try:
            self.on_device_change(rec, what)
        except Exception as exc:            # noqa: BLE001
            _log(f"on_device_change 回调抛异常: {exc!r}")
