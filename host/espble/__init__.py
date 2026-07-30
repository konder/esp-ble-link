"""espble —— ESP 设备 BLE 中枢(macOS)。

典型用法:

    from espble import BleLink, DeviceConfig, RetainedChannel

    device = DeviceConfig(
        app_path=".build/native/MyBLEHelper.app",
        session_dir="~/.config/myapp/ble-session",
        device_name="MyDevice",
    )
    link = BleLink(device, on_notification=print)
    channel = RetainedChannel(link)

    channel.set_retained("state", {"t": "state", "rev": 7, "battery": 82})
    channel.publish({"t": "ev", "kind": "done", "msg": "构建完成"})

链路会自己保持连接、自己重连、重连后自动补推 retained 与历史。
"""

from .channel import RetainedChannel
from .framing import DEFAULT_LINE_LIMIT, encode, fit_line, limit_for_ring
from .helper_session import DeviceConfig, HelperSession
from .link import BleLink

__version__ = "0.1.0"

__all__ = [
    "BleLink",
    "DeviceConfig",
    "HelperSession",
    "RetainedChannel",
    "DEFAULT_LINE_LIMIT",
    "encode",
    "fit_line",
    "limit_for_ring",
    "__version__",
]
