# esp-ble-link

**ESP 设备 BLE 通信通用框架** —— ESP32 外设链路层 + macOS 中枢,两侧配套。

[English](README.en.md)

给这样一类项目用:一台 ESP32 小设备,一台常开的 Mac,中间走 BLE 传 JSON。
状态面板、桌宠、遥控器、agent 通知器都是这个形状。

这类东西的难点从来不是「让它连上」,而是**让它一直连着**。这个框架把
ESP32 侧的链路纪律、macOS 侧的 CoreBluetooth 权限与进程恢复,以及
BLE 缺失的 retained / 离线补发语义,一次性做对。

> 所有设计决定的成因都记在 **[docs/pitfalls.md](docs/pitfalls.md)** 里。
> 那份文档比代码值钱 —— 每一条都对应一次真实的排查。

## 它做了什么

**固件侧**(PlatformIO 库,NimBLE-Arduino)

- Nordic UART Service 外设,分隔符组帧,自动分片
- BLE 回调只搬字节,组帧/解析全在主循环 —— 不饿死协议栈
- **不请求连接参数**(这是最重要的一条,见 pitfalls A1)
- 断连自动清缓冲重新广播
- 溢出/超长帧计数,排查时能区分「没收到」和「收到了但丢字节」
- WiFi 模式 HTTP OTA,自动让出 2.4G 射频

**中枢侧**(pip 包,macOS)

- 编译出来的 Swift `.app` 做 CoreBluetooth 中心 —— Python + bleak 拿不到稳定的
  蓝牙 TCC 授权(pitfalls B2)
- 一份通用源码,bundle id 构建期注入,UUID/设备名运行期传
- 任何失败都丢掉整个 helper 进程重开 —— CoreBluetooth 的坏状态是进程级的
- 严格 ACK 驱动的分片写,一次只有一片在飞
- 真实 ATT 写做 keepalive(`ping` 探不到 BLE 链路)
- `RetainedChannel` 补出 MQTT 的 retained 与离线重放语义

## 快速开始

固件:

```ini
; platformio.ini
lib_deps = https://github.com/konder/esp-ble-link.git
```

```cpp
#include <EspBleLink.h>

void setup() {
    espble::LinkConfig cfg;
    cfg.deviceName = "MyDevice";
    espble::begin(cfg);
}

void loop() {
    String line;
    while (espble::popMessage(line)) handle(line);
    delay(20);
}
```

中枢:

```bash
pip install "git+https://github.com/konder/esp-ble-link.git#subdirectory=host"
espble build-helper --name MyBLEHelper --bundle-id com.you.mydevice.blehelper
espble watch --app .build/native/MyBLEHelper.app --device-name MyDevice
```

```python
from espble import BleLink, DeviceConfig, RetainedChannel

link = BleLink(DeviceConfig(
    app_path=".build/native/MyBLEHelper.app",
    session_dir="~/.config/myapp/ble-session",
    device_name="MyDevice",
))
channel = RetainedChannel(link)
channel.set_retained("state", {"t": "state", "rev": 1, "battery": 82})
channel.publish({"t": "ev", "msg": "构建完成"})
```

完整流程见 **[docs/porting.md](docs/porting.md)**。

## 文档

| | |
|---|---|
| [docs/pitfalls.md](docs/pitfalls.md) | **踩坑清单** —— 按症状查,含成因与判别方法 |
| [docs/protocol.md](docs/protocol.md) | 线协议:GATT、组帧、长度约束、helper 邮箱 |
| [docs/porting.md](docs/porting.md) | 接一台新设备的完整步骤 |
| [docs/bundle-ids.md](docs/bundle-ids.md) | bundle id 登记表(必须唯一) |

## 示例

`examples/echo` 是一个最小回环:收什么回什么。烧进任意 ESP32-S3,
`./run.sh` 会构建 helper、发消息、验回显。新设备接入前先拿它验环境。

## 环境要求

- 固件:ESP32 系列,PlatformIO + Arduino framework,NimBLE-Arduino **1.4.x**
  (2.x 改了回调签名,尚未适配)
- 中枢:**macOS 13+**。中枢部分是 macOS 专属的 —— 它的核心价值就是绕开
  CoreBluetooth 的那些坑。固件侧与平台无关。
- Python 3.9+,零运行时依赖(只用标准库)

## 状态

早期。协议与 API 可能还会变。

## License

MIT
