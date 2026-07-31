"""espble 命令行:构建 helper、连设备、收发、发布固件。

    espble build-helper --name EchoBLEHelper --bundle-id com.you.echo.blehelper
    espble watch  --app .build/native/EchoBLEHelper.app --device-name EspBleEcho
    espble send   --app … --device-name EspBleEcho '{"hi":1}'
    espble scan   --app …                      # 只扫不连,看看周围有什么
    espble ota publish firmware.bin --version 2
    espble ota serve
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

from . import ota as ota_mod
from .framing import fit_line
from .helper_session import DeviceConfig, HelperSession
from .link import BleLink

NATIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native")
DEFAULT_APP_DIR = ".build/native"
DEFAULT_SESSION_ROOT = os.path.expanduser("~/.config/espble/sessions")


def _device_args(parser: argparse.ArgumentParser):
    parser.add_argument("--app", help=f"helper.app 路径(默认 {DEFAULT_APP_DIR}/<name>.app)")
    parser.add_argument("--device-name", help="按广播名精确匹配")
    parser.add_argument("--name-prefix", help="按广播名前缀匹配(没有 --device-name 时生效)")
    parser.add_argument("--device-id", help="按 CoreBluetooth 的 peripheral identifier 匹配")
    parser.add_argument("--service", help="service UUID(默认 NUS)")
    parser.add_argument("--rx", help="RX 特征 UUID(默认 NUS)")
    parser.add_argument("--tx", help="TX 特征 UUID(默认 NUS)")
    parser.add_argument("--session-dir", help="会话目录(默认按设备名放到 ~/.config/espble/sessions)")
    parser.add_argument("--scan-timeout", type=float, default=20.0)
    parser.add_argument("--chunk-ceiling", type=int, default=180)
    parser.add_argument("--accept-unterminated", action="store_true",
                        help="兼容 notify 不补分隔符的老固件")


def _build_device(args) -> DeviceConfig:
    app = args.app
    if not app:
        # 没给 --app 就在默认目录里找唯一的那个 .app,省得每次敲长路径。
        candidates = []
        if os.path.isdir(DEFAULT_APP_DIR):
            candidates = [os.path.join(DEFAULT_APP_DIR, n)
                          for n in sorted(os.listdir(DEFAULT_APP_DIR)) if n.endswith(".app")]
        if len(candidates) == 1:
            app = candidates[0]
        elif not candidates:
            raise SystemExit(f"找不到 helper。先跑 espble build-helper,或用 --app 指定路径。")
        else:
            raise SystemExit("{} 下有多个 helper,请用 --app 指定:\n  {}".format(
                DEFAULT_APP_DIR, "\n  ".join(candidates)))

    if not (args.device_name or args.name_prefix or args.device_id):
        raise SystemExit(
            "必须给 --device-name / --name-prefix / --device-id 之一。\n"
            "  不指定的话 helper 什么都不会连 —— 一张桌子上常有好几台跑 NUS 的设备,\n"
            "  「连上第一个看到的」几乎一定是错的。")

    session_dir = args.session_dir
    if not session_dir:
        tag = args.device_name or args.name_prefix or args.device_id
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)
        session_dir = os.path.join(DEFAULT_SESSION_ROOT, safe)

    return DeviceConfig(
        app_path=os.path.abspath(app),
        session_dir=os.path.abspath(os.path.expanduser(session_dir)),
        device_name=args.device_name,
        name_prefix=args.name_prefix,
        device_id=args.device_id,
        service_uuid=args.service,
        rx_uuid=args.rx,
        tx_uuid=args.tx,
        scan_timeout=args.scan_timeout,
        chunk_ceiling=args.chunk_ceiling,
        accept_unterminated=args.accept_unterminated,
    )


# ---------- build-helper ----------

def cmd_build_helper(args) -> int:
    script = os.path.join(NATIVE_DIR, "build_helper.sh")
    cmd = ["/bin/zsh", script,
           "--name", args.name,
           "--bundle-id", args.bundle_id,
           "--out", os.path.abspath(args.out),
           "--src", os.path.join(NATIVE_DIR, "BleHelper.swift"),
           "--template", os.path.join(NATIVE_DIR, "Info.plist.tmpl")]
    if args.usage_desc:
        cmd += ["--usage-desc", args.usage_desc]
    if args.version:
        cmd += ["--version", args.version]
    # 菜单栏模式的默认值(写进 Info.plist);无界面 worker 不读它们
    for flag, val in (("--device-name", args.device_name),
                      ("--name-prefix", args.name_prefix),
                      ("--session-dir", args.session_dir)):
        if val:
            cmd += [flag, val]
    if args.install:
        cmd += ["--install"]
    return subprocess.call(cmd)


# ---------- scan ----------

def cmd_scan(args) -> int:
    """只扫不连:helper 会把看到的每台设备报一次,扫描超时后自己退出。

    这是排查「连不上」的第一步 —— 能看到别的设备就说明扫描本身是好的,
    问题在匹配条件或设备没广播;一台都看不到多半是权限或会话上下文。
    """
    device = DeviceConfig(
        app_path=os.path.abspath(args.app),
        session_dir=os.path.abspath(os.path.expanduser(
            args.session_dir or os.path.join(DEFAULT_SESSION_ROOT, "_scan"))),
        scan_timeout=args.scan_timeout,
        # 三个匹配条件都不给 → helper 谁都不连,只报 discovered,超时自退。
    )
    found = {}

    def collect(event: dict):
        if event.get("event") == "discovered":
            found[event.get("identifier")] = event
        elif event.get("event") == "error":
            print(f"  helper: {event.get('message')}", file=sys.stderr)

    session = HelperSession(device, connect_timeout=args.scan_timeout + 5, event_sink=collect)
    # 必然返回 False(没给匹配条件,谁都不会连上)。我们要的是过程中的 discovered 事件。
    session.start()
    session.poll()
    session.stop()
    found = list(found.values())

    if not found:
        print("一台设备都没扫到。检查:蓝牙是否打开、helper 是否已授权、"
              "监护进程是否在 GUI 会话里(见 docs/pitfalls.md)。", file=sys.stderr)
        return 1
    print(f"扫到 {len(found)} 台:")
    for event in sorted(found, key=lambda e: -e.get("rssi", -999)):
        name = event.get("name") or event.get("local_name") or "(无名)"
        print(f"  {event.get('rssi'):>4} dBm  {name:<28} {event.get('identifier')}")
    return 0


# ---------- watch / send ----------

def _run_link(args, after_connect=None, seconds: float = 0.0) -> int:
    """连上设备 → 跑 after_connect → 观察 seconds 秒(0 且无 after_connect = 一直看)。"""
    device = _build_device(args)
    link = BleLink(device,
                   on_notification=lambda line: print(f"← {line}", flush=True),
                   keepalive_sec=getattr(args, "keepalive", 30.0))
    try:
        if not link.wait_connected(args.connect_timeout):
            print(f"没连上: {link.fatal_error or '超时'}", file=sys.stderr)
            return 1
        print(f"已连接 {device.device_name or device.name_prefix or device.device_id}", flush=True)

        code = 0
        if after_connect:
            code = after_connect(link)

        if seconds > 0:
            deadline = time.time() + seconds
            while time.time() < deadline and link.connected:
                time.sleep(0.2)
        elif after_connect is None:
            while link.connected:      # watch 模式:一直看到断开或 Ctrl-C
                time.sleep(0.2)
        return code
    except KeyboardInterrupt:
        return 130
    finally:
        link.close()


def cmd_watch(args) -> int:
    return _run_link(args, seconds=args.seconds)


def cmd_send(args) -> int:
    try:
        line = fit_line(json.loads(args.payload))
    except ValueError:
        # 不是 JSON 也照发 —— 链路层只认分隔符,不关心内容。
        line = args.payload

    def do_send(link: BleLink) -> int:
        ok = link.send_blocking(line)
        print("已发送" if ok else "发送失败", file=sys.stderr)
        return 0 if ok else 1

    return _run_link(args, after_connect=do_send, seconds=args.wait)


# ---------- ota ----------

def cmd_ota_publish(args) -> int:
    target = ota_mod.publish(args.image, args.version, os.path.abspath(args.dir))
    size = os.path.getsize(target)
    print(f"已发布 v{args.version} ({size} 字节) → {target}")
    return 0


def cmd_ota_serve(args) -> int:
    return ota_mod.serve(os.path.abspath(args.dir), port=args.port,
                         base_path=args.base_path, bind=args.bind) or 0


# ---------- main ----------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="espble", description="ESP 设备 BLE 中枢工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build-helper", help="编译一个专属的 BLE helper.app")
    p.add_argument("--name", required=True, help="helper 名(同时是可执行文件名,不能含空格)")
    p.add_argument("--bundle-id", required=True, help="**必须全局唯一**,复用会被 LaunchServices 顶掉")
    p.add_argument("--usage-desc", help="蓝牙授权弹框文案")
    p.add_argument("--version", default="1.0")
    p.add_argument("--out", default=DEFAULT_APP_DIR)
    p.add_argument("--install", action="store_true",
                   help="额外装一份到 ~/Applications(访达/启动台可见,双击进菜单栏模式)")
    p.add_argument("--device-name", help="菜单栏模式默认盯哪台设备(精确名)")
    p.add_argument("--name-prefix", help="菜单栏模式默认盯哪台设备(名字前缀)")
    p.add_argument("--session-dir", help="菜单栏模式只读地看哪个会话目录的链路状态")
    p.set_defaults(func=cmd_build_helper)

    p = sub.add_parser("scan", help="扫描周围的 BLE 设备(排查连不上的第一步)")
    p.add_argument("--app", required=True)
    p.add_argument("--session-dir")
    p.add_argument("--scan-timeout", type=float, default=10.0)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("watch", help="连上设备并打印它 notify 上来的内容")
    _device_args(p)
    p.add_argument("--connect-timeout", type=float, default=40.0)
    p.add_argument("--keepalive", type=float, default=30.0)
    p.add_argument("--seconds", type=float, default=0.0, help="0 = 一直看到断开")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("send", help="发一行给设备(JSON 会走长度约束)")
    _device_args(p)
    p.add_argument("payload")
    p.add_argument("--connect-timeout", type=float, default=40.0)
    p.add_argument("--wait", type=float, default=3.0, help="发完再等几秒收回复")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("ota", help="固件分发")
    osub = p.add_subparsers(dest="otacmd", required=True)
    q = osub.add_parser("publish", help="发布一个编译产物")
    q.add_argument("image")
    q.add_argument("--version", type=int, required=True)
    q.add_argument("--dir", default="fw")
    q.set_defaults(func=cmd_ota_publish)
    q = osub.add_parser("serve", help="起固件服务")
    q.add_argument("--dir", default="fw")
    q.add_argument("--port", type=int, default=8899)
    q.add_argument("--base-path", default="/fw")
    q.add_argument("--bind", default="0.0.0.0")
    q.set_defaults(func=cmd_ota_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
