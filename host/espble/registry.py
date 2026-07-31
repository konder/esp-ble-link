"""设备注册表 —— 把「这台设备是谁」这件事持久化下来。

多设备场景下,「谁是谁」必须有个不随部署漂移的答案。这里的取舍:

**身份是设备 id,不是别名,也不是广播名。**
  id 由固件从 efuse MAC 后三字节派生(见 EspBleLink 的 deviceType/deviceId),
  同一份固件烧多块板子自动不重名。广播名是 `<type>-<id>` 拼出来的**派生物**,
  别名是给人看的**标签** —— 两者都可以变,id 不变。

**别名绑 id。** 换了一块新板子就是新 id,别名要手动重绑。
  这是刻意的:如果别名跟着「角色」自动漂移到新硬件,那设备一坏就会静默地
  把数据发到另一台上,而你不会察觉。宁可让它断,也不要让它悄悄连错。

**注册表只记事实,不记配置。** 连接参数、投递策略属于 BleHub 的构造参数,
  不进这个文件 —— 否则改一次策略要去改一个看起来像"设备清单"的文件,很别扭。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterator, Optional

DEFAULT_PATH = os.path.expanduser("~/.config/espble/devices.json")


@dataclass
class DeviceRecord:
    device_id: str                      # 身份,来自固件(efuse MAC 后 3 字节)
    device_type: str = ""               # 如 "m5paper";广播名 = f"{type}-{id}"
    alias: str = ""                     # 给人看的名字;空则回退到 id
    fw: int = 0                         # 由 hello 帧填
    caps: list = field(default_factory=list)
    last_seen: float = 0.0
    peripheral_uuid: str = ""           # CoreBluetooth 的 identifier,**只是缓存**
                                        # (它按 host 不同而不同,不能当身份用)

    @property
    def name(self) -> str:
        """广播名。固件按 `<type>-<id>` 拼,中枢照同样规则算回去。"""
        return f"{self.device_type}-{self.device_id}" if self.device_type else self.device_id

    @property
    def label(self) -> str:
        return self.alias or self.device_id

    def supports(self, cap: str) -> bool:
        # caps 为空 = 设备没上报过 hello。此时**认为它什么都支持** ——
        # 宁可多发一条它会忽略的消息,也不要因为握手没到就静默漏发。
        return not self.caps or cap in self.caps


class DeviceRegistry:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = os.path.expanduser(path)
        self._devices: Dict[str, DeviceRecord] = {}
        self.load()

    # ---- 持久化 ----

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        for did, d in (raw.get("devices") or {}).items():
            d.pop("device_id", None)
            try:
                self._devices[did] = DeviceRecord(device_id=did, **d)
            except TypeError:
                # 文件是更新版本写的、带了本版本不认识的字段 —— 跳过这条而不是整个崩掉
                continue

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {"version": 1, "devices": {}}
        for did, rec in self._devices.items():
            d = asdict(rec)
            d.pop("device_id")
            payload["devices"][did] = d
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, self.path)      # 原子:别让并发读到半个文件

    # ---- 查改 ----

    def __len__(self) -> int:
        return len(self._devices)

    def __iter__(self) -> Iterator[DeviceRecord]:
        return iter(self._devices.values())

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def get(self, key: str) -> Optional[DeviceRecord]:
        """按 id 或别名找。id 优先 —— 别名是可变标签,不该盖过身份。"""
        if key in self._devices:
            return self._devices[key]
        for rec in self._devices.values():
            if rec.alias and rec.alias == key:
                return rec
        return None

    def add(self, device_id: str, device_type: str = "", alias: str = "",
            save: bool = True) -> DeviceRecord:
        rec = self._devices.get(device_id)
        if rec is None:
            rec = DeviceRecord(device_id=device_id, device_type=device_type, alias=alias)
            self._devices[device_id] = rec
        else:
            if device_type:
                rec.device_type = device_type
            if alias:
                rec.alias = alias
        if save:
            self.save()
        return rec

    def set_alias(self, device_id: str, alias: str) -> DeviceRecord:
        """别名绑 id。重名会被拒 —— 两台设备同名的话路由就没法确定了。"""
        rec = self._devices.get(device_id)
        if rec is None:
            raise KeyError(f"未注册的设备 id: {device_id}")
        clash = self.get(alias)
        if clash is not None and clash.device_id != device_id:
            raise ValueError(f"别名 {alias!r} 已被 {clash.device_id} 占用")
        rec.alias = alias
        self.save()
        return rec

    def remove(self, key: str) -> bool:
        rec = self.get(key)
        if rec is None:
            return False
        self._devices.pop(rec.device_id, None)
        self.save()
        return True

    def absorb_hello(self, payload: dict) -> Optional[DeviceRecord]:
        """吃一帧 hello,更新身份信息。返回对应记录(没有 id 就返回 None)。

        hello 是**框架自己的协议**,不是业务内容 —— 所以由这一层消化掉,
        不往应用层转发。这是框架「只管传输」原则里少数几个刻意的例外之一。
        """
        did = str(payload.get("id") or "").strip()
        if not did:
            return None
        rec = self._devices.get(did) or self.add(did, save=False)
        if payload.get("type"):
            rec.device_type = str(payload["type"])
        try:
            rec.fw = int(payload.get("fw") or 0)
        except (TypeError, ValueError):
            pass
        caps = payload.get("caps")
        if isinstance(caps, str):
            rec.caps = [c.strip() for c in caps.split(",") if c.strip()]
        elif isinstance(caps, list):
            rec.caps = [str(c) for c in caps]
        rec.last_seen = time.time()
        self.save()
        return rec
