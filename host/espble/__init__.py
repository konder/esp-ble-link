"""espble —— ESP 设备 BLE 中枢(macOS)。

典型用法:

    from espble import BleLink, DeviceConfig, RetainedChannel

    device = DeviceConfig(
        app_path=".build/native/MyBLEHelper.app",
        session_dir="~/.config/myapp/ble-session",
        device_name="MyDevice",
    )
    # autostart=False:让 RetainedChannel 先把回调接上再启动,
    # 否则首次连接可能在 on_connect 挂上之前就完成了,补推就漏了。
    link = BleLink(device, on_notification=print, autostart=False)
    channel = RetainedChannel(link)      # 构造时会启动链路

    channel.set_retained("state", {"t": "state", "rev": 7, "battery": 82})
    channel.publish({"t": "ev", "kind": "done", "msg": "构建完成"})

链路会自己保持连接、自己重连、重连后自动补推 retained 与历史。

多台设备用 BleHub —— **一个 bundle(一次 TCC 授权),每台设备一个进程**:

    from espble import BleHub

    hub = BleHub(app_path=".build/native/MyBLEHelper.app", device_type="m5paper")
    hub.register("c119cc", alias="客厅")
    hub.register("7a1b02", alias="书房")

    hub.send("客厅", {"t": "usage", "rev": 7})
    hub.broadcast({"t": "cmd", "cmd": "ota"}, cap="cmd")

设备身份由固件从 efuse MAC 派生,广播名是 `<type>-<id>` —— 同一份固件烧多块板子
自动不重名。路由完全在主机侧,消息里不需要带 target 字段。
"""

from .channel import RetainedChannel
from .framing import DEFAULT_LINE_LIMIT, encode, fit_line, limit_for_ring
from .helper_session import DeviceConfig, HelperSession
from .hub import BleHub, DeviceLink
from .link import BleLink
from .registry import DeviceRecord, DeviceRegistry

__version__ = "0.1.0"

__all__ = [
    "BleHub",
    "BleLink",
    "DeviceLink",
    "DeviceRecord",
    "DeviceRegistry",
    "DeviceConfig",
    "HelperSession",
    "RetainedChannel",
    "DEFAULT_LINE_LIMIT",
    "encode",
    "fit_line",
    "limit_for_ring",
    "__version__",
]
