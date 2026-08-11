"""BleLink —— 一条「永远在线」的设备链路(监护线程 + 进程级重连 + 真链路 keepalive)。

对上层暴露的心智模型很简单:构造出来它就一直在后台努力保持连接,你随时
send_soon() 就行。断了会自己重连,重连时通过 on_connect 回调让上层补推状态。

三个设计决定,都有代价换来的理由:

1. **重连 = 丢掉整个 helper 进程重开**,不在进程内 retry。
   macOS CoreBluetooth 的坏状态是进程级的。

2. **两级退避,而不是固定间隔。**
   固定 5s 适合「设备常在」的场景。设备可能整天不在(拔电/没电)时,
   20s 扫描 + 5s 重试 = 每天几千次进程启动,而且**持续扫描会干扰同一台
   Mac 上其它 helper 的 BLE 链路**。所以连续失败后放缓,一成功立刻回到 5s。
   退避上限别设太大:很多设备只在特定窗口广播,退避超过那个窗口就永远错过。

3. **keepalive 必须是一次真实的 ATT 写。**
   ping 只走到 helper 进程,探不到 BLE 链路死活。所以由上层提供一个
   「重发也无副作用」的帧(通常就是最新的 retained 状态),定期真发一次。
"""
from __future__ import annotations

import collections
import sys
import threading
import time
from typing import Callable, Optional

from .helper_session import DeviceConfig, HelperSession


def _log(msg: str):
    print(f"[espble] {msg}", file=sys.stderr, flush=True)


def backoff_delay(fails: int, reconnect_sec: float, backoff_after: int,
                  backoff_max_sec: float) -> float:
    """连续失败 fails 次后该等多久再试。

    刻意**不用指数退避**:设备要么在(几秒内就能连上),要么不在(等多久都白搭)。
    指数退避在这里唯一的作用是把「设备刚回来」的那一刻错过去。
    两档就够:快档撞窗口,慢档省电、少打扰同机其它 helper。
    """
    if fails < backoff_after:
        return reconnect_sec
    return backoff_max_sec


class BleLink:
    def __init__(self, device: DeviceConfig, *,
                 reconnect_sec: float = 5.0,
                 backoff_after: int = 5,
                 backoff_max_sec: float = 45.0,
                 keepalive_sec: float = 30.0,
                 connect_timeout: float = 25.0,
                 command_timeout: float = 10.0,
                 outbox_max: int = 32,
                 on_connect: Optional[Callable[["BleLink"], None]] = None,
                 on_disconnect: Optional[Callable[[], None]] = None,
                 on_notification: Optional[Callable[[str], None]] = None,
                 keepalive_provider: Optional[Callable[[], Optional[str]]] = None,
                 autostart: bool = True):
        self.device = device
        self.reconnect_sec = reconnect_sec
        self.backoff_after = backoff_after
        self.backoff_max_sec = backoff_max_sec
        self.keepalive_sec = keepalive_sec
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.on_notification = on_notification
        self.keepalive_provider = keepalive_provider

        self._outbox: collections.deque = collections.deque(maxlen=outbox_max)
        self._outbox_lock = threading.Lock()
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._session: Optional[HelperSession] = None
        self._fatal_error = ""
        self._thread: Optional[threading.Thread] = None

        if autostart:
            self.start()

    # ---- 对外 ----

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="espble-link", daemon=True)
        self._thread.start()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def fatal_error(self) -> str:
        """非空表示重试无意义(例如蓝牙未授权),需要人来处理。"""
        return self._fatal_error

    def wait_connected(self, timeout: float = 40.0) -> bool:
        return self._connected.wait(timeout)

    def send_soon(self, line: str, *, queue_while_offline: bool = False) -> bool:
        """排队交给监护线程发,不阻塞。

        默认**断连时直接丢弃**:BLE 没有 MQTT 的离线队列,攒着的消息重连后
        会一次灌爆设备。真正需要保证送达的状态请走 RetainedChannel 的 retained /
        history 语义 —— 那是「重连后补推最新值」,而不是「补发每一条」。
        """
        if not queue_while_offline and not self._connected.is_set():
            return False
        with self._outbox_lock:
            self._outbox.append(line)
        return True

    def clear_outbox(self):
        """丢掉排队中的消息。重连补推前调用,免得补推的和队列里的重复。"""
        with self._outbox_lock:
            self._outbox.clear()

    def send_blocking(self, line: str) -> bool:
        """绕过队列直接发并等 ACK。给 CLI / 测试用;常规路径请用 send_soon。"""
        session = self._session
        if not session or not session.connected:
            return False
        return session.send_line(line)

    def close(self):
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        session = self._session
        if session:
            try:
                session.stop()
            except OSError:
                pass
        self._session = None
        self._connected.clear()

    # ---- 监护线程 ----

    def _drop_session(self):
        session = self._session
        if session is not None:
            try:
                session.stop()
            except OSError:
                pass
            self._session = None
        if self._connected.is_set():
            self._connected.clear()
            if self.on_disconnect:
                try:
                    self.on_disconnect()
                except Exception as exc:            # noqa: BLE001 —— 回调是用户代码
                    _log(f"on_disconnect 回调抛异常: {exc!r}")

    def _run(self):
        fails = 0
        last_keepalive = 0.0

        while not self._stop.is_set():
            session = self._session

            # helper 崩了/被杀时不会写 disconnected 事件,所以「连没连上」不能只信
            # 事件流 —— 还得看进程在不在。这一步之前是缺的,后果是 helper 一崩就
            # 永久失联(实测 kill -9 后 3.5 分钟零恢复)。节流在 session 那边。
            if session is not None and not session.check_process_alive():
                _log(f"{session.last_error} —— 重开")

            if session is None or not session.connected:
                self._drop_session()
                session = HelperSession(self.device,
                                        connect_timeout=self.connect_timeout,
                                        command_timeout=self.command_timeout)
                if not session.start():
                    if session.fatal:
                        # 蓝牙没授权之类:再试一万次也一样,停下来等人处理。
                        self._fatal_error = session.last_error
                        _log(f"致命错误,停止重连: {session.last_error}")
                        try:
                            session.stop()
                        except OSError:
                            pass
                        return
                    fails += 1
                    delay = backoff_delay(fails, self.reconnect_sec,
                                          self.backoff_after, self.backoff_max_sec)
                    _log(f"连接失败({fails}): {session.last_error or '未知'} —— {delay:.0f}s 后重试")
                    try:
                        session.stop()
                    except OSError:
                        pass
                    self._stop.wait(delay)
                    continue

                fails = 0
                self._session = session
                self._connected.set()
                last_keepalive = time.time()
                if self.on_connect:
                    try:
                        self.on_connect(self)
                    except Exception as exc:        # noqa: BLE001
                        _log(f"on_connect 回调抛异常: {exc!r}")

            # 排空发件箱
            #
            # ⚠️ 发失败必须**真的**丢掉 session。以前这里只 break 跳出内层循环,
            #    日志写着「并重连」而重连压根没发生 —— 因为顶上那个重开判据看的是
            #    `session.connected`,而 helper 活着时它一直是 True。
            failed = False
            while not self._stop.is_set() and session.connected:
                with self._outbox_lock:
                    if not self._outbox:
                        break
                    line = self._outbox.popleft()
                if not session.send_line(line):
                    _log(f"发送失败({session.last_error or '链路断了'}) —— 丢弃本条并重连")
                    failed = True
                    break
            if failed:
                self._drop_session()
                continue

            # 收 notify
            while session.notifications:
                line = session.notifications.popleft()
                if self.on_notification:
                    try:
                        self.on_notification(line)
                    except Exception as exc:        # noqa: BLE001
                        _log(f"on_notification 回调抛异常: {exc!r}")

            session.poll()

            # keepalive:发一帧真实数据。这是唯一能探出「链路其实已经死了」的手段。
            if (session.connected and self.keepalive_provider
                    and time.time() - last_keepalive >= self.keepalive_sec):
                last_keepalive = time.time()
                try:
                    payload = self.keepalive_provider()
                except Exception as exc:            # noqa: BLE001
                    payload = None
                    _log(f"keepalive_provider 抛异常: {exc!r}")
                if payload and not session.send_line(payload):
                    # 同上:以前这行日志是假的 —— 它说「触发重连」,而代码什么都没做。
                    # keepalive 是唯一能探出「helper 活着但链路已经死了」的手段,
                    # 探出来了却不动,等于白探。
                    _log("keepalive 失败 → 触发重连")
                    self._drop_session()
                    continue

            self._stop.wait(0.2)

        self._drop_session()
