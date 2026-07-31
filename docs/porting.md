# 接一台新设备

假设你要做一台叫 `Foo` 的新设备。从零到收发跑通,大概十分钟。

## 1. 固件

`platformio.ini`:

```ini
[env:foo]
platform = espressif32
board = esp32-s3-devkitm-1
framework = arduino
lib_deps = https://github.com/konder/esp-ble-link.git
```

`src/main.cpp`:

```cpp
#include <EspBleLink.h>

void setup() {
    Serial.begin(115200);
    espble::LinkConfig cfg;
    cfg.deviceName = "FooDevice";       // 中枢按这个名字精确匹配
    espble::begin(cfg);
}

void loop() {
    String line;
    while (espble::popMessage(line)) {   // 排空到 false 为止,一次只交一条
        handle(line);                    // 你的业务:解析 JSON、渲染、响应
    }
    delay(20);
}
```

就这些。广播、分帧、重组、断连重新广播都在库里。

**两条纪律得你自己守**:

- `onConnectionChange` 回调跑在协议栈任务里,只能置标志位。耗时的事放主循环。
- 别加 `updateConnParams`。库里没有这个函数是有原因的([pitfalls A1](pitfalls.md#a1))。

## 2. 中枢

```bash
pip install "git+https://github.com/konder/esp-ble-link.git#subdirectory=host"

# 一个**产品**一个 helper,bundle id 必须唯一。
# 注意不是「一台设备一个」—— 多台同类设备共用一个 bundle 即可,见下面「接多台设备」。
espble build-helper --name FooBLEHelper \
                    --bundle-id com.yourname.foo.blehelper \
                    --usage-desc "Foo 面板通过蓝牙与设备通信。"
```

把新的 bundle id 记进 [bundle-ids.md](bundle-ids.md)。

先手动验一下链路:

```bash
espble watch --app .build/native/FooBLEHelper.app --device-name FooDevice
espble send  --app .build/native/FooBLEHelper.app --device-name FooDevice '{"t":"ping"}'
```

**第一次运行会弹蓝牙授权框,必须在 GUI 会话里点** —— 最简单的办法是**双击这个 app**,
它会进菜单栏模式并请求授权。授过之后 Python / launchd 从 SSH 驱动都没问题,
GUI 只有首次授权那一次需要([pitfalls B1](pitfalls.md#b1))。

## 3. 接进你的程序

```python
from espble import BleLink, DeviceConfig, RetainedChannel

device = DeviceConfig(
    app_path=".build/native/FooBLEHelper.app",
    session_dir="~/.config/foo/ble-session",
    device_name="FooDevice",
)
link = BleLink(device, on_notification=lambda line: print("设备说:", line))
channel = RetainedChannel(link)

# 一上电就该看到的状态。重连时自动补推,也被拿来当 keepalive 帧。
channel.set_retained("state", {"t": "state", "rev": 1, "battery": 82})

# 事件。进历史环,重连时以 live=False 重放。
channel.publish({"t": "ev", "kind": "done", "msg": "构建完成"})
```

链路会自己保持连接、自己重连。`BleLink` 起一个后台线程,构造完就在跑了。

**设备侧记得处理 `rev`**:keepalive 会反复重发同一帧 retained,
`rev` 没变就别重绘([protocol.md](protocol.md#rev-字段的作用))。

## 4. 常驻起来

监护进程必须是 **LaunchAgent(`gui/$UID`)**,不能是 `launchd` 的 system domain,
也不能从 SSH 手工起 —— helper 需要 GUI 会话才拿得到 `bluetoothd`。

```xml
<!-- ~/Library/LaunchAgents/com.yourname.foo.plist -->
<key>ProgramArguments</key>
<array>
  <string>/usr/bin/python3</string>
  <string>-m</string>
  <string>foo.main</string>
</array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
```

```bash
launchctl bootout gui/$UID ~/Library/LaunchAgents/com.yourname.foo.plist 2>/dev/null
sleep 2      # 不等的话 bootstrap 会报 "Input/output error"
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.yourname.foo.plist
```

## 5. 加上 OTA(可选)

固件:

```cpp
#include <EspBleOta.h>

if (line.indexOf("\"cmd\":\"ota\"") >= 0) {
    espble::OtaConfig ota;
    ota.host = "10.0.0.9";              // 跑 espble ota serve 那台机器
    ota.currentVersion = FW_VERSION;
    ota.seedSsid = WIFI_SSID;           // 只在 NVS 为空时播种
    ota.seedPassword = WIFI_PASSWORD;
    espble::otaOverWifi(ota);           // 成功不返回(设备重启)
}
```

主机:

```bash
espble ota publish .pio/build/foo/firmware.bin --version 2
espble ota serve                       # 起在 :8899
```

然后 `channel.send_now({"t":"cmd","cmd":"ota"}, queue_while_offline=True)`。

`otaOverWifi` 会先 `end()` 释放 2.4G 射频再开 WiFi —— 两个射频抢一根天线,
不释放的话 OTA 慢到超时([pitfalls A6](pitfalls.md#a6))。

WiFi 凭据存在 NVS 里,首次用编译值播种。以后换 WiFi 调 `wifiSaveNvs()` 就行,
不用重烧固件。

## 接多台设备

一个 bundle 就够,**不要**给每台设备建一个:

```bash
espble build-helper --name MyBLEHelper --bundle-id com.you.mydevices.blehelper
```

```python
from espble import BleHub

hub = BleHub(app_path=".build/native/MyBLEHelper.app", device_type="m5paper")
hub.register("c119cc", alias="客厅")
hub.register("7a1b02", alias="书房")

hub.send("客厅", {"t": "usage", "rev": 7})
hub.broadcast({"t": "cmd", "cmd": "ota"}, cap="cmd")
```

固件那边只需要给 `deviceType`,`id` 自动从 efuse MAC 派生:

```cpp
espble::LinkConfig cfg;
cfg.deviceType = "m5paper";
cfg.fwVersion  = FW_VERSION;
cfg.caps       = "usage,ev,cmd";
espble::begin(cfg);      // 广播名自动 = m5paper-<id>
```

**共用 bundle 但不共用进程**:`open -n` 会为每台设备开独立实例,各有各的
`CBCentralManager`,而 TCC 授权只需要一次。这样既省掉 N 次授权,又保住了
「一台设备把进程搞坏不会拖垮其它设备」——
后者是 [pitfalls C2](pitfalls.md#c2) 那条纪律的直接要求。

⚠️ 每台设备的 **session-dir 必须互斥**(`BleHub` 按 device_id 自动分)。
`HelperSession._kill()` 是按 session-dir 匹配进程的,分错了会误杀兄弟设备的 helper。

## 排查

连不上时的顺序:

```bash
espble scan --app .build/native/FooBLEHelper.app     # 周围有什么?
```

- 一台都扫不到 → 权限或会话上下文,查 [pitfalls B1](pitfalls.md#b1)
- 扫得到别的、扫不到自己的 → 设备没广播,或名字对不上
- 扫得到自己的但连不上 → 看 helper 的 `events.jsonl` 停在哪一步

设备侧的 `espble::stats()` 能区分「没收到」和「收到了但丢字节」,
见 [pitfalls A2](pitfalls.md#a2)。
