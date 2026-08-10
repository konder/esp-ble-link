"""hubd —— 把 BleHub 包成一个独立进程,用 NDJSON 走 stdin/stdout。

    应用进程(不 import espble)
      └─▶ espble hubd
            stdin  ◀─ 每行一个 JSON 指令   {"op":"publish_all","obj":{…}}
            stdout ─▶ 每行一个 JSON 事件   {"event":"message","device":"c119cc",…}
            stderr ─▶ 继承(和调用方的日志混在一起,这是刻意的,见下)
              └─▶ helper 进程 ×N ──NUS──▶ 设备

============================================================================
为什么要有这一层
============================================================================
`import espble` 这条依赖在生产上很贵:消费方得和框架**同一个 Python 解释器**。
实测的代价是 —— m5paper 看板的 collector 跑 `/usr/bin/python3`(Xcode 3.9),
它的 pip 老到装不了 PEP 660 editable,于是只能在 LaunchAgent 里塞一句
`PYTHONPATH=…/esp-ble-link/host`。那个补丁不进版本控制,只活在一台机器的
plist 里,换机器就是一次盲踩。

换成进程边界之后:消费方只需要知道**一个命令路径**,语言、解释器、依赖全解耦,
而且框架崩了不会把消费方一起带走。

============================================================================
协议上的几个取舍
============================================================================
**不做请求/响应关联(没有 seq、没有超时重试)。** 状态变化时 hubd 主动推
`status`,消费方维护一份缓存即可。这不只是图省事:消费方问"连上了吗"往往在
它的主循环里,如果那是一次"写管道 → 等回答",一个卡住的 hubd 就能把消费方
的主循环挂死。推模型让读侧永远是本地内存。

**每行写完必须 flush。** 管道默认块缓冲(不是行缓冲,那只对 tty 成立),
不 flush 的现象是"渠道起来了但什么都不动",而且两边都不报错 —— 很难查。

**stderr 继承而不是捕获。** 这样 `tail` 一个日志文件能同时看到消费方和框架的
输出,时间线是连着的。代价是两边日志混在一起,所以这里的前缀是 `[hubd]`。

**行内不可能有裸换行。** 设备协议本身就是换行分帧的(见 framing),
再加 `json.dumps` 的转义,NDJSON 在这里是安全的。
"""
from __future__ import annotations

import json
import signal
import sys
import threading
from typing import Any, Mapping, Optional

from .hub import BleHub
from .registry import DeviceRecord


def _log(*a):
    print("[hubd]", *a, file=sys.stderr, flush=True)


# ---------- 指令 ----------

def apply_command(hub: BleHub, cmd: Mapping[str, Any]) -> Optional[dict]:
    """执行一条指令,返回要回给对方的事件(None = 不用回)。

    **刻意做成纯函数**:不碰 stdin/stdout、不碰全局状态。于是它能脱离管道、
    脱离 CoreBluetooth 单测 —— 只要给个假 hub 就能验"哪条指令落到 hub 的哪个
    方法上"。真链路由 test_mailbox 和真机覆盖。
    """
    op = cmd.get("op")
    if not op:
        return _error("指令缺 op 字段", cmd)

    if op == "register":
        device_id = str(cmd.get("id") or "").strip()
        if not device_id:
            return _error("register 缺 id", cmd)
        hub.register(device_id,
                     str(cmd.get("alias") or ""),
                     device_type=str(cmd.get("device_type") or ""))
        return status_event(hub)

    if op == "unregister":
        target = str(cmd.get("target") or "")
        if not hub.unregister(target):
            return _error(f"unregister: 找不到设备 {target!r}", cmd)
        return status_event(hub)

    if op == "set_retained_all":
        key = str(cmd.get("key") or "")
        obj = cmd.get("obj")
        if not key or not isinstance(obj, dict):
            return _error("set_retained_all 需要 key + obj(对象)", cmd)
        hub.set_retained_all(key, obj)
        return None

    if op == "publish_all":
        obj = cmd.get("obj")
        if not isinstance(obj, dict):
            return _error("publish_all 需要 obj(对象)", cmd)
        hub.publish_all(obj)
        return None

    if op == "send":
        target = str(cmd.get("target") or "")
        obj = cmd.get("obj")
        if not target or not isinstance(obj, dict):
            return _error("send 需要 target + obj(对象)", cmd)
        if not hub.send(target, obj, queue_offline=cmd.get("queue_offline")):
            # 找不到设备是**消费方的配置问题**,不是链路问题 —— 必须报上去,
            # 否则表现成"发了但没到",会被当成 BLE 的锅去查。
            return _error(f"send: 找不到设备 {target!r}", cmd)
        return None

    if op == "broadcast":
        obj = cmd.get("obj")
        if not isinstance(obj, dict):
            return _error("broadcast 需要 obj(对象)", cmd)
        hub.broadcast(obj, queue_offline=cmd.get("queue_offline"))
        return None

    if op == "status":
        return status_event(hub)

    return _error(f"不认识的 op: {op!r}", cmd)


def _error(message: str, cmd: Optional[Mapping[str, Any]] = None) -> dict:
    ev = {"event": "error", "message": message}
    if cmd is not None:
        ev["op"] = cmd.get("op")
    return ev


# ---------- 事件 ----------

def status_event(hub: BleHub) -> dict:
    return {"event": "status", "devices": hub.status()}


def message_event(rec: DeviceRecord, line: str) -> dict:
    return {"event": "message", "device": rec.device_id,
            "label": rec.label, "line": line}


def device_event(hub: BleHub, rec: DeviceRecord, what: str) -> dict:
    st = hub.status().get(rec.device_id) or {}
    return {"event": "device", "device": rec.device_id, "label": rec.label,
            "what": what, "connected": bool(st.get("connected"))}


def parse_device_args(items) -> list:
    """`["c119cc:看板", "7a1b02"]` → `[("c119cc","看板"), ("7a1b02","")]`。"""
    out = []
    for item in items or []:
        device_id, _, alias = str(item).partition(":")
        device_id = device_id.strip()
        if device_id:
            out.append((device_id, alias.strip()))
    return out


# ---------- 进程 ----------

def run(args) -> int:
    # 回调是在 BleLink 的监护线程里跑的,而主线程也会写 stdout(指令的回执)——
    # 两个线程各写半行就会把 NDJSON 撕坏。一把锁保证「一行是原子的」。
    out_lock = threading.Lock()

    def emit(obj: dict):
        line = json.dumps(obj, ensure_ascii=False)
        with out_lock:
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()          # 管道是块缓冲的,不 flush 对方永远收不到
            except (BrokenPipeError, ValueError):
                # 消费方先走了。别抛 —— 让主循环靠 stdin EOF 正常收尾。
                pass

    hub = BleHub(
        app_path=args.app,
        device_type=args.device_type,
        registry_path=args.registry,
        session_root=args.session_root,
        history_n=args.history_n,
        reconnect_sec=args.reconnect_sec,
        backoff_max_sec=args.backoff_max_sec,
        keepalive_sec=args.keepalive_sec,
        scan_timeout=args.scan_timeout,
        on_message=lambda rec, line: emit(message_event(rec, line)),
    )
    # on_device_change 要用到 hub 自己(查 connected),所以构造完再挂上。
    # 推两条:device 说"发生了什么",status 给全量快照 —— 消费方只认后者做缓存,
    # 这样它永远不需要自己维护增量状态机。
    def on_change(rec: DeviceRecord, what: str):
        emit(device_event(hub, rec, what))
        emit(status_event(hub))
    hub.on_device_change = on_change

    # 先从注册表接管已知设备(hubd 重启是常态),再按参数补登记。两步都幂等。
    hub.adopt_registry()
    for device_id, alias in parse_device_args(args.device):
        hub.register(device_id, alias)

    if not hub.status():
        _log("⚠️ 一台设备都没登记 —— 用 --device <id>[:别名],id 是广播名的后缀")

    # SIGTERM 时要走 finally 把 helper 进程带走,否则留一批孤儿。
    # (孤儿其实也能被下次 start() 按 session-dir 收掉,见 B5,但别指望它。)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    emit({"event": "ready", "devices": hub.status()})

    try:
        while True:
            raw = sys.stdin.readline()
            if not raw:                     # EOF:消费方退出了,跟着收摊
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                cmd = json.loads(raw)
            except ValueError as exc:
                # 一行坏 JSON 不能掀翻整个循环 —— 那等于一次输入错误就断了链路。
                emit(_error(f"坏 JSON: {exc}"))
                continue
            if not isinstance(cmd, dict):
                emit(_error("指令必须是 JSON 对象"))
                continue
            try:
                reply = apply_command(hub, cmd)
            except Exception as exc:        # noqa: BLE001 —— 同上,不让它掀翻循环
                emit(_error(f"{type(exc).__name__}: {exc}", cmd))
                continue
            if reply is not None:
                emit(reply)
    except KeyboardInterrupt:
        pass
    finally:
        hub.close()
    return 0
