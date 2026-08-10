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

不想 import(或者你的消费方根本不是 Python)—— 用 `hubd`,它把多设备中枢包成一个
进程,stdin 收 JSON 指令、stdout 出 JSON 事件,一行一条:

```bash
# 仓库自带的 shim,不需要 pip(有些消费方跑在你挑不了的解释器上,比如 Xcode 的 python3)
host/bin/espble hubd --app ~/.local/espble/MyHub.app \
    --device-type m5paper --device c119cc:看板

{"op":"set_retained_all","key":"usage","obj":{"t":"usage","pct":60}}
{"op":"publish_all","obj":{"t":"ev","msg":"构建完成"}}
{"op":"send","target":"看板","obj":{"t":"cmd","cmd":"ota"},"queue_offline":true}
← {"event":"ready","devices":{…}}
← {"event":"message","device":"c119cc","label":"看板","line":"{\"pct\":100}"}
← {"event":"status","devices":{…}}          # 状态变化时主动推,消费方读缓存即可
```

状态是**推**的、不是问答式的:消费方常在自己的主循环里读"连上了吗",
问答式会让一个卡住的 hubd 把对方主循环挂死。

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

### 验证到哪了

主机侧已验证:

| | |
|---|---|
| 48 个单测 | `cd host && PYTHONPATH=. python3 -m pytest tests -q` |
| pip 安装形态与 CLI 入口 | `pip install ./host` → `espble --help` |
| Swift helper 编译 + adhoc 签名 | `espble build-helper …`,bundle 结构/plist/codesign 均核对过 |
| bundle id 三道护栏 | 本地登记表、上游 id 黑名单、名字含空格,均按预期拒绝 |
| helper 真机扫描 | 实际扫到周围设备,事件序列 `central_created → central_state:5 → scan_started` 正常 |
| 扫描超时看门狗 | 连跑三次,均在 `scan_timeout + 1s` 自我终结 |
| OTA publish / serve | 端到端 `curl` 过 `/fw/version` 与 `/fw/current.bin` |

固件侧已在真机验证(M5PaperS3 / ESP32-S3,Arduino ESP32 core 3.2.1 +
NimBLE-Arduino 1.4.3,MTU 协商到 517):

| | |
|---|---|
| 编译 | `arduino-cli compile -b esp32:esp32:esp32s3` 零警告 |
| 广播与建连 | 中枢扫到 `EspBleEcho` 并完成订阅 |
| 收发往返 | 21B / 198B / 696B / 1398B / 1839B(1~11 片)全部**逐字节一致** |
| 连发 | 20 条连发,20/20 ack + 20/20 回显 |
| 设备统计 | `rx_dropped=0  rx_oversize=0`,累计 6359B / 27 帧 |
| 长度约束 | 2725B 的中文对象经 `fit_line` 压到 1843B,设备收到的字节数完全吻合 |
| **OTA 端到端** | BLE 下发 `{"t":"cmd","cmd":"ota"}` → 设备静默 BLE → 连 WiFi → 拉 1.29MB 镜像 → 重启 → BLE 报 `fw:2` |

PlatformIO 侧已验证的部分:

| | |
|---|---|
| `library.json` | 通过 PlatformIO 的 manifest schema 校验 |
| 两份清单共存 | PlatformIO 选 `library.json`,Arduino IDE 选 `library.properties`,不打架 |
| `lib_deps = symlink://../..` | 解析成功,`NimBLE-Arduino@1.4.3` 作为传递依赖被自动带出 |
| git URL 形态 | clone 后从目录安装 → 同样解析成功、依赖自动带出 |

**仍未验证**:`pio run` 的完整编译链路。开发机拉不动 PlatformIO 的工具链包(见下),
上面的固件是用 arduino-cli 编的。有条件的机器上跑一次 `cd examples/echo && pio run`。

### 已知问题

**`end()` 在 NimBLE-Arduino 1.4.x + Arduino core 3.x 上会 panic。**
1.4.x 照 IDF 4.x 写的 HCI deinit 在 IDF 5.x 上返回 `ESP_ERR_INVALID_STATE`,
随后死在 `heap_caps_free` 的断言里。**只有这一条路坏**,广播/连接/收发全正常。
要临时让出射频用 `quiesce()`(`otaOverWifi()` 已经这么做了),要彻底释放就重启设备。
详见 [pitfalls A9](docs/pitfalls.md)。

`library.json` 里的 `export.include` **只在打包发布到 registry 时生效**;
用 git URL 或本地目录安装时 PlatformIO 会整仓拷进 `.pio/libdeps/`,
`host/`、`docs/` 也一起进去。不影响编译(`build.srcDir` 限定只编 `src/`),
只是占点空间。

### 用 arduino-cli 代替 PlatformIO 编译

如果你的网络也拉不动 PlatformIO 的工具链包,而 Arduino ESP32 core 是现成的:

```bash
arduino-cli compile -b esp32:esp32:esp32s3:CDCOnBoot=cdc,FlashSize=16M,PSRAM=opi \
  --library /path/to/esp-ble-link \
  --library /path/to/NimBLE-Arduino \
  <你的 sketch 目录>
```

`library.properties` 就是为这条路准备的。注意板子参数必须与硬件一致 ——
拿默认的 `FlashSize=4M,PSRAM=disabled` 烧 16MB+OPI PSRAM 的板子,
设备会**静默不广播**,没有任何报错。

## License

MIT
