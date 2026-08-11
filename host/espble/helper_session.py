"""驱动一个 BLE helper.app 进程的会话层(文件邮箱 + 进程级恢复)。

这一层只管「和**一个** helper 进程说话」;什么时候重开进程是 link.py 的事。

============================================================================
四条进程坑 —— 每一条都真的把人卡过,注释保留成因,别精简
============================================================================

1. **必须用 `open -g -j -n` 启动,不能直接 exec bundle 里的二进制。**
   直接跑 Contents/MacOS/xxx 的进程**没有 bundle 身份**,CoreBluetooth 会在
   central 创建后把它干掉 —— 现象是事件流停在 `central_created` 再无下文。
   -g 不抢前台 / -j 隐藏 / -n 强制新实例。

2. **`open` 在 LaunchServices 收下请求就返回,那时进程还不存在。**
   立刻检查存活会得到「已退出」→ 触发重连 → 重连时的 kill 又把刚要起来的 helper
   杀掉 → 永不收敛。所以 start() 必须**轮询等进程真的出现**。

3. **杀进程要按 session-dir 匹配,不能只按二进制名。**
   同一台机器上常同时跑好几个 helper;`pkill -f SomeBLEHelper` 会把别人的一起带走。
   session-dir 每个会话唯一,按它匹配才安全。

4. **每次启动都用全新的 session 目录。**
   残留的 command 文件会被重放,残留的 events.jsonl 会被从头再读一遍。

还有一条不在本文件、但同样致命的(见 docs/pitfalls.md):
   **监护进程必须在 GUI(Aqua)会话里。** 从 SSH 会话 open 出来的 helper 拿不到
   bluetoothd —— 症状和坑 1 一模一样(卡在 central_created),极易误判成签名问题。
   健康的序列是 central_created → central_state:5 → scan_started。

============================================================================
邮箱协议
============================================================================
  commands/{seq:08d}.json   我们写(.tmp → os.replace 原子落盘),helper 读后删除
                            {"seq":int,"op":"write_json"|"ping"|"shutdown","line":str?}
  events.jsonl              helper 追加,我们按字节偏移 tail
                            {"event":"launch|central_created|central_state|scan_started|
                                      discovered|connect_started|connected_transport|
                                      connected|disconnected|notification|ack|
                                      command_error|error", ...}
"""
from __future__ import annotations

import collections
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


def _log(msg: str):
    print(f"[espble] {msg}", file=sys.stderr, flush=True)


@dataclass
class DeviceConfig:
    """一台设备的连接参数。传给 helper 的命令行由它生成。"""

    app_path: str
    session_dir: str

    # 三选一的匹配方式,优先级 device_id > device_name > name_prefix。
    # ⚠️ 一个都不给的话 helper 什么都不会连 —— 这是故意的:一张桌子上常有好几台
    #    跑 NUS 的设备,「连上第一个看到的」几乎一定是错的。
    device_name: Optional[str] = None
    name_prefix: Optional[str] = None
    device_id: Optional[str] = None

    # GATT。默认 Nordic UART Service,和 EspBleLink 的默认值对齐。
    service_uuid: Optional[str] = None
    rx_uuid: Optional[str] = None
    tx_uuid: Optional[str] = None

    scan_timeout: float = 20.0
    chunk_ceiling: int = 180

    # 兼容 notify 不补分隔符的老固件(靠「像不像完整 JSON」猜边界)。
    # EspBleLink 会补分隔符,所以默认关。
    accept_unterminated: bool = False

    extra_args: list = field(default_factory=list)

    @property
    def helper_name(self) -> str:
        """bundle 名去掉 .app —— 同时是可执行文件名和 ps 里的匹配串。"""
        return os.path.basename(self.app_path.rstrip("/")).removesuffix(".app")

    @property
    def executable(self) -> str:
        return os.path.join(self.app_path, "Contents", "MacOS", self.helper_name)

    def helper_args(self) -> list:
        args = ["--session-dir", self.session_dir,
                "--scan-timeout", str(self.scan_timeout),
                "--chunk-ceiling", str(self.chunk_ceiling)]
        if self.device_name:  args += ["--device-name", self.device_name]
        if self.name_prefix:  args += ["--name-prefix", self.name_prefix]
        if self.device_id:    args += ["--device-id", self.device_id]
        if self.service_uuid: args += ["--service", self.service_uuid]
        if self.rx_uuid:      args += ["--rx", self.rx_uuid]
        if self.tx_uuid:      args += ["--tx", self.tx_uuid]
        if self.accept_unterminated: args += ["--accept-unterminated"]
        return args + list(self.extra_args)


class HelperSession:
    """一个 helper 进程的生命周期 + 文件邮箱收发。

    这个对象是**一次性**的:链路一断就作废(_dead),要恢复请丢掉它新建一个。
    这不是偷懒 —— macOS CoreBluetooth 的坏状态是进程级的,进程内重试只会越积越坏。
    """

    def __init__(self, device: DeviceConfig, *,
                 connect_timeout: float = 25.0, command_timeout: float = 10.0,
                 event_sink: Optional[Callable[[dict], None]] = None):
        self.device = device
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        # 每条 helper 事件都会过一遍这个钩子。start() 内部也在 poll,所以想看到
        # 建连过程中的事件(discovered / central_state)只能靠它 —— poll() 的
        # 返回值只有调用者自己那一次的增量。
        self.event_sink = event_sink

        self.session_dir = device.session_dir
        self._commands_dir = os.path.join(self.session_dir, "commands")
        self._events_path = os.path.join(self.session_dir, "events.jsonl")
        self._offset = 0
        self._seq = 0
        self._connected = False
        self._dead = False
        self._fatal = False       # TCC 未授权之类:重试无意义,要人来处理
        self._last_error = ""
        self._acked: set = set()
        self._last_alive_check = 0.0   # _pids() 要 fork 一个 ps,得节流(见 poll)
        self.peer_name = ""
        self.chunk_size = 0
        # 设备 notify 上来的行。send_line 内部也会 poll,所以通知必须缓冲在这里,
        # 否则会被 send_line 顺手吞掉。
        self.notifications: collections.deque = collections.deque(maxlen=256)

    # ---- 进程 ----

    def _pids(self) -> list:
        """按 session-dir + helper 名找进程(见坑 3)。"""
        try:
            out = subprocess.run(["ps", "-ax", "-o", "pid=,command="],
                                 capture_output=True, text=True, timeout=5).stdout
        except (subprocess.TimeoutExpired, OSError):
            return []
        pids = []
        name = self.device.helper_name
        for line in out.splitlines():
            line = line.strip()
            if not line or self.session_dir not in line or name not in line:
                continue
            try:
                pids.append(int(line.split(None, 1)[0]))
            except ValueError:
                continue
        return pids

    def _kill(self):
        for sig in ("-TERM", "-KILL"):
            alive = self._pids()
            if not alive:
                return
            for pid in alive:
                subprocess.run(["kill", sig, str(pid)], capture_output=True)
            deadline = time.time() + 1.0
            while time.time() < deadline and self._pids():
                time.sleep(0.05)

    def alive(self) -> bool:
        return bool(self._pids())

    @property
    def connected(self) -> bool:
        return self._connected and not self._dead

    @property
    def fatal(self) -> bool:
        """True 表示重试也没用(例如蓝牙未授权),上层应停下来报给人。"""
        return self._fatal

    @property
    def last_error(self) -> str:
        return self._last_error

    # ---- 生命周期 ----

    def start(self) -> bool:
        if not os.path.exists(self.device.executable):
            self._last_error = (
                f"helper 未编译: {self.device.app_path}\n"
                f"  跑一次: espble build-helper --name {self.device.helper_name} "
                f"--bundle-id <你的唯一 id>")
            self._dead = self._fatal = True
            return False

        self._kill()                                            # 坑 3
        shutil.rmtree(self.session_dir, ignore_errors=True)     # 坑 4
        os.makedirs(self._commands_dir, exist_ok=True)
        open(self._events_path, "a").close()
        self._offset = 0
        self._seq = 0
        self._connected = False
        self._dead = False
        self._acked.clear()

        # 坑 1:必须 open,不能 exec bundle 内的二进制
        cmd = ["open", "-g", "-j", "-n", self.device.app_path, "--args"] + self.device.helper_args()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            self._last_error = f"open 失败: {proc.stderr.strip()[:200]}"
            self._dead = True
            return False

        # 坑 2:等进程真的出现,别立刻判存活
        deadline = time.time() + 10.0
        while time.time() < deadline and not self.alive():
            time.sleep(0.2)
        if not self.alive():
            self._last_error = "helper 进程没起来(open 成功但进程不存在)"
            self._dead = True
            return False

        # 等 connected(扫描 → 建连 → 服务发现 → 订阅通知,四步都成了才算可用)
        deadline = time.time() + self.connect_timeout
        while time.time() < deadline:
            self.poll()
            if self._connected:
                return True
            if self._dead or not self.alive():
                self._last_error = self._last_error or "helper 在连上之前就退出了"
                return False
            time.sleep(0.15)
        self._last_error = self._last_error or f"{self.connect_timeout:.0f}s 内没连上"
        return False

    def stop(self):
        if self.alive():
            try:
                self._write_command("shutdown", None)
                deadline = time.time() + 2.0
                while time.time() < deadline and self.alive():
                    self.poll()
                    time.sleep(0.1)
            except OSError:
                pass
        self._kill()
        self._connected = False
        self._dead = True

    # ---- 邮箱 ----

    def poll(self) -> list:
        """tail events.jsonl,更新内部状态,返回本次新增的事件。"""
        try:
            size = os.path.getsize(self._events_path)
        except OSError:
            return []
        if size < self._offset:
            self._offset = 0          # helper 重建了文件
        if size == self._offset:
            return []
        try:
            with open(self._events_path, "rb") as fh:
                fh.seek(self._offset)
                blob = fh.read()
        except OSError:
            return []
        # 最后一行可能只写了一半(helper 正在追加),留到下次再读
        nl = blob.rfind(b"\n")
        if nl < 0:
            return []
        self._offset += nl + 1

        events = []
        for raw in blob[:nl].split(b"\n"):
            if not raw.strip():
                continue
            try:
                event = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            self._absorb(event)
            if self.event_sink:
                try:
                    self.event_sink(event)
                except Exception as exc:        # noqa: BLE001 —— 钩子是调用方代码
                    _log(f"event_sink 抛异常: {exc!r}")
            events.append(event)
        return events

    def check_process_alive(self, now: Optional[float] = None) -> bool:
        """进程没了就是链路没了 —— 哪怕事件流里一个字都没说。

        返回 **False 只表示「本次探测发现进程消失了」**(并顺手把 session 标死)。
        探测不适用时(还没连上、或已经知道它死了)返回 True —— 那两种情况本来就由
        `connected` 那条判据管,这里再报一次 False 只会让上层重复打日志。

        **由监护方(link.py)调,不在 poll() 里自动跑** —— 本层只管「和一个 helper
        说话」,什么时候重开进程是上层的事(见文件头)。节流和「只在连上之后才判」
        这两条规则放在这里,是因为它们和 `_pids()` 是一码事。

        为什么必须有这一步:`connected` 以前只看事件流,而 helper **崩掉或被 kill -9
        时压根不会写 `disconnected` 事件**(它自己都没机会跑)。于是 `_connected`
        永远是 True,link.py 的「session 没连上就重开」那个判据永远不成立 ——
        **helper 一崩就永久失联**。实测:`kill -9` worker 之后 3.5 分钟零恢复,
        日志只在刷「keepalive 失败 → 触发重连」,而那句话当时是假的。

        ⚠️ 只在**已经连上之后**才判。启动阶段进程还没出现(`open` 一收下请求就返回,
        见文件头坑 2),那时候判存活会得到「已退出」→ 触发重连 → 重连时的 kill
        又把正要起来的 helper 杀掉 → 永不收敛。start() 里那个轮询等待才是启动阶段的判据。

        ⚠️ 节流:`_pids()` 要 fork 一个 `ps -ax`,而监护循环 0.2s 一轮。
        不节流就是每秒 5 次 fork。2 秒一次足够 —— 重连本身就是 5 秒起步的量级。
        """
        if not self._connected or self._dead:
            return True
        now = time.time() if now is None else now
        if now - self._last_alive_check < 2.0:
            return True
        self._last_alive_check = now
        if not self.alive():
            self._dead = True
            self._last_error = "helper 进程消失了(崩溃或被杀),没来得及写 disconnected"
            return False
        return True

    def _absorb(self, event: dict):
        kind = event.get("event")
        if kind == "connected":
            self._connected = True
            self.peer_name = event.get("name") or ""
            self.chunk_size = int(event.get("chunk") or 0)
            _log(f"connected {self.peer_name} chunk={self.chunk_size}")
        elif kind == "disconnected":
            self._connected = False
            self._dead = True         # helper 自己会终结,这个会话就作废了
            _log(f"disconnected err={event.get('error') or '-'}")
        elif kind == "notification":
            line = event.get("line")
            if line:
                self.notifications.append(line)
        elif kind == "ack":
            try:
                self._acked.add(int(event.get("seq", -1)))
            except (TypeError, ValueError):
                pass
        elif kind in ("error", "command_error"):
            self._last_error = str(event.get("message") or "")[:300]
            _log(f"{kind}: {self._last_error}")
            if "unauthorized" in self._last_error:
                # TCC 没授权。重试一万次也一样,停下来让人去点「允许」。
                self._dead = self._fatal = True

    def _write_command(self, op: str, line: Optional[str]) -> int:
        self._seq += 1
        seq = self._seq
        payload = {"seq": seq, "op": op}
        if line is not None:
            payload["line"] = line
        base = os.path.join(self._commands_dir, f"{seq:08d}")
        with open(base + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(base + ".tmp", base + ".json")   # 原子:helper 绝不会读到半个文件
        return seq

    def send_line(self, line: str) -> bool:
        """把一行写给设备,阻塞等 ACK(helper 逐片 withResponse 等 ATT ack)。

        返回 False 有两种可能:超时,或链路在中途断了。两种都该触发重连。
        """
        if not self.connected:
            return False
        seq = self._write_command("write_json", line)
        deadline = time.time() + self.command_timeout
        while time.time() < deadline:
            self.poll()
            if seq in self._acked:
                self._acked.discard(seq)
                return True
            if self._dead or not self.alive():
                return False
            time.sleep(0.05)
        self._last_error = f"seq={seq} 等 ack 超时"
        return False

    def ping(self) -> bool:
        """⚠️ 只证明 helper 进程活着,**探不到 BLE 链路**。
        要探链路死活必须发一次真实的 send_line(见 link.py 的 keepalive)。"""
        if not self.alive():
            return False
        seq = self._write_command("ping", None)
        deadline = time.time() + self.command_timeout
        while time.time() < deadline:
            self.poll()
            if seq in self._acked:
                self._acked.discard(seq)
                return True
            time.sleep(0.05)
        return False
