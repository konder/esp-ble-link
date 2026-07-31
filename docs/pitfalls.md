# 踩坑清单

这份文档是这个仓库最值钱的东西。代码可以重写,下面这些是拿掉线、拿一晚上一晚上
的排查换来的 —— 每一条都记了**症状**、**成因**和**判别方法**,因为它们最狠的地方
不是难修,而是**症状指向错误的方向**。

按「你看到什么现象」来查。

---

## A. 外设侧(ESP32)

### A1. 连上几秒就断,`reason=520`,日志里有 `rc=7 BLE_HS_ENOTCONN`

**成因**:外设在连接建立后调了 `updateConnParams()`。

`reason=520` 是监管超时(supervision timeout)。Apple 对 BLE 外设请求的连接参数有
硬性约束(见 Apple Accessory Design Guidelines),其中监管超时必须 ≤ 6 秒。请求
`timeout=10s` 会被 iOS/macOS 直接拒绝,而拒绝的后果不是「保持原参数」,是链路崩掉。
更阴的是即使改成合规值也常常不稳 —— 请求发出的时机(刚连上、GATT 还没配置完)本身
就容易撞上 `BLE_HS_ENOTCONN`。

**结论:外设永远不要请求连接参数。** 参数由中心驱动,这是设计不是妥协。
外设唯一能碰的是广播包里的 `setMinPreferred/setMaxPreferred` **建议值**,
中心可以完全无视。

`EspBleLink` 里**不存在** `updateConnParams`,请不要「为了完整性」加回来。
曾经有一版只删了调用点、把函数体留着 —— 那等于埋了一颗随时会被复活的雷。

### A2. 消息偶尔少几个字节 / JSON 解析失败

先看 `espble::stats()`:

| 计数器 | 非 0 意味着 |
|---|---|
| `rxDroppedBytes` | 接收环形缓冲满了。要么主循环排空太慢(检查有没有阻塞操作),要么中枢单行超过了 `rxRingBytes` |
| `rxOversize` | 有帧超过 `maxFrameBytes` 被整条丢弃 |
| 两个都是 0 | 那问题不在链路层,去看中枢发的内容 |

最常见的成因是**中枢按字符数而不是字节数限长**。中文一个字 3 字节,1200 字 = 3600 字节,
直接冲爆 2048 的环形缓冲。host 侧用 `fit_line()`,它限的是字节并且按字符边界截断。

### A3. 刷屏 / 长耗时操作导致掉线

BLE 回调跑在 NimBLE 主机任务里。在回调里做任何耗时的事都会饿死协议栈,中心侧看到的
就是监管超时(又是 `reason=520`,和 A1 症状一样,别混淆)。

墨水屏全刷要 1–2 秒,而监管超时上限只有 6 秒 —— 这类设备「连上就刷屏」几乎必然掉线。

`EspBleLink` 的 `onWrite` 只做 `memcpy` 进环形缓冲然后立刻返回;组帧、解析、渲染
全在主循环。**你自己的代码也要守这条**:`onConnectionChange` 回调同样跑在协议栈任务里,
只能置标志位。

反面教材:早期版本在 `onWrite` 里逐字节 `String += ch`,一条 4KB 消息就是上千次堆
重分配发生在协议栈任务里。

### A4. 负载里带 NUL 时后半截丢了

`NimBLECharacteristic::getValue().c_str()` 遇到 NUL 会截断。用 `data()` / `length()`。

### A5. 断连重连后第一条消息是烂的

上次连接遗留在环形缓冲里的半截帧,和新连接的首帧拼在了一起。
`onDisconnect` 里必须清 `head`/`tail` 和组帧缓冲 —— `EspBleLink` 已经做了。

### A6. ★ 短消息 notify 正常,长消息回来是一段乱码

**症状极具迷惑性**:单片能装下的消息(几十上百字节)完美往返;超过一片的长消息,
中枢收到的是一段**残帧** —— 开头半个汉字、JSON 解析失败,而且常伴随几条内容
一模一样的旧帧被重复吐出来。设备侧的 `rxDroppedBytes`/`rxOversize` **全是 0**,
看起来一切正常,所以很容易往中枢的重组逻辑上找,找不到。

**成因**:notify 分片取满了 `ATT_MTU - 3`。MTU 协商到 517 时那就是 514 字节一片,
连推几片会把 NimBLE 的 mbuf 池打空,后面的片**静默丢弃**。

**为什么发现不了**:NimBLE-Arduino 1.4.x 的 `NimBLECharacteristic::notify()`
**返回 `void`** —— 协议栈吃不下时不给任何信号。所以这里没法靠检查返回值重试,
只能靠不把它撑爆。这也是本库没有 `txDropped` 计数器的原因:统计不到的东西
不该假装能统计。

**解**:`LinkConfig::notifyChunkMax` 默认 180(实测值,不是理论值),
片间 `notifyChunkDelayMs = 4`。**别把 notifyChunkMax 调大**,那个「MTU 都协商到
517 了不用白不用」的直觉正是这个 bug 的来源。

实测(M5PaperS3 / NimBLE 1.4.3 / MTU 517):改成 180 之后,21B / 198B / 696B /
1398B / 1839B 五种长度(1~11 片)全部逐字节一致,连发 20 条 20/20 无丢失。

### A7. OTA 龟速 / OTA 期间 BLE 断

ESP32 的 BLE 和 WiFi **共用一颗 2.4G 射频**。做 OTA 前先用 `espble::quiesce()`
把 BLE 静下来(停广播 + 断连),`otaOverWifi()` 已经封装了这个序列。

> 理论上更干净的做法是 `end()` 彻底 deinit,把射频完全让出来 —— 但那条路在
> Arduino core 3.x 上会 panic(见 [A9](#a9))。实测**共存足够**:局域网上
> 1.3MB 的镜像几秒钟就下完了。

### A8. 收到 ota 指令后设备直接重启,固件没换

**症状**:下发指令 → 设备重启 → 版本还是旧的,固件服务器的日志里**一条设备的请求都没有**
(说明连 HTTP 那步都没走到)。串口上能看到:

```
E NimBLEAdvertising: Error enabling advertising; rc=30
E NimBLEDevice: esp_nimble_hci_and_controller_deinit() failed with error: 259
CORRUPT HEAP: Bad tail at 0x...
assert failed: multi_heap_free
```

**成因**:拆 BLE 栈时会触发断连,断连回调里又去 `startAdvertising()` —— 在一个
正在拆的协议栈上开广播,`rc=30` 失败,连带 deinit 也失败,最后堆损坏 panic。

**解**:断连回调必须能区分「对端走了,该重新广播」和「是我们自己在拆栈」。
本库用一个 `s_stopping` 标志,`quiesce()`/`end()` 进入前置位。
**自己写外设固件的话这条一定要有** —— 只要你有任何「主动关掉 BLE」的路径就会撞上。

### A9. ★ NimBLE-Arduino 1.4.x 的 `deinit()` 在 Arduino core 3.x 上必 panic

```
E NimBLEDevice: esp_nimble_hci_and_controller_deinit() failed with error: 259
assert failed: heap_caps_free heap_caps_base.c:75
               (free() target pointer is outside heap areas)
```

`259` = `ESP_ERR_INVALID_STATE`。**成因**:NimBLE-Arduino 1.4.x 是照 ESP-IDF 4.x 写的,
Arduino ESP32 core 3.x 底下是 IDF 5.x,HCI/controller 的 deinit API 变了。

**要命的地方是它只坏这一条路**:广播、连接、收发、重连全都正常,单元测试和日常
使用一点问题都没有 —— 只有「主动关掉 BLE」时才炸。所以很容易到了要做 OTA
那天才发现。

**解**:别调 `end()`。用 `quiesce()`(停广播 + 断连,保留协议栈),
需要彻底干净的射频状态就重启设备。本库的 `otaOverWifi()` 已经这么做了。

如果你的项目非要真正的 deinit,那就得上 NimBLE-Arduino 2.x —— 但 2.x 改了回调
签名,本库尚未适配(见 [A9](#a9))。

### A10. ★ 编译报 `expected unqualified-id before string constant`,指向**你自己的** config.h

```
src/config.h:23: error: expected unqualified-id before string constant
 #define NUS_RX  "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
```

**成因**:你的头文件里有 `#define NUS_RX "…"`,而某个库的头文件里声明了同名的
C++ 标识符。**宏不认 namespace** —— 哪怕那个声明写在 `namespace foo` 里面,
预处理器照样替换,于是 `extern const char* const NUS_RX;` 变成了
`extern const char* const "6E400002-…";`。

最坑的是**报错位置指向你自己的 config.h**,而真正占了名字的是库,
所以第一反应总是去怀疑自己的宏写错了。

**判别**:注释掉那个 `#define`,看错误是否转移;或 `grep -rn NUS_RX` 找有没有第二处
**声明**(不是定义)。

本库因此把 UUID 常量命名为 `espble::kNusService / kNusRx / kNusTx`,而不是
`NUS_SERVICE / NUS_RX / NUS_TX` —— ALL_CAPS 按惯例归宏所有,库不该去抢。
所以你项目里继续写 `#define NUS_RX` 是安全的。

(这条是 m5work 迁移时真炸出来的,不是纸上推演。)

### A11. ★ 烧完就再也不运行:每次复位都进 `boot:0x0 (DOWNLOAD)`

**症状**:烧录成功、hash 校验通过,但设备就是不跑固件 —— 不广播、不上 WiFi、屏幕不动。
串口里每次都是:

```
rst:0x15 (USB_UART_CHIP_RESET),boot:0x0 (DOWNLOAD(USB/UART0))
waiting for download
```

`esptool --after hard_reset`、`esptool run`、手工脉冲 EN,全都没用。

**判别(不复位就能测)**:
```bash
esptool --port <口> --before no_reset --after no_reset --connect-attempts 1 chip_id
#   同步成功 → 芯片正停在 ROM 下载器里
#   同步失败 → 芯片在跑应用代码
```

**定位到底是不是 GPIO0 被按住**,读两个寄存器(ESP32-S3):
```bash
esptool ... read_mem 0x60004038   # GPIO_STRAP_REG:复位那一刻锁存的 strapping
esptool ... read_mem 0x6000403C   # GPIO_IN_REG:GPIO0 当前电平(bit0)
```
实测拿到 `strap=0x00000000` 但 `in=0x3c000009`(bit0=1)—— 也就是
**GPIO0 现在是高的,只在复位瞬间被拉低**。那就不是 BOOT 键卡住,而是板上的
自动下载电路:主机每次经 DTR/RTS 复位,都会顺带把 GPIO0 拉低,而 esptool 的
`hard_reset` 在这个板子/适配器组合上释放 GPIO0 的时序不对。

**解:让芯片自己内部复位,绕开那条电路。**
```bash
esptool --port <口> --before no_reset --after watchdog_reset flash_id
```
`watchdog_reset` 用 RTC 看门狗从芯片内部触发复位,不碰 EN/GPIO0,strapping 就会读到
上拉的高电平,正常启动。

⚠️ **这个选项 esptool 4.7 才加,而 4.7.0 本身还没有**(`--after` 只有
hard_reset/soft_reset/no_reset/no_reset_stub)。PlatformIO 捆绑的往往就是老版本,
装个新的到临时 venv 即可:`python3 -m venv /tmp/espt && /tmp/espt/bin/pip install -U esptool`。

顺带一提:**在这块板上「打开串口」这个动作本身就会触发上述复位**,所以
「读串口看看它在干嘛」会把它敲回下载模式。判活优先用 BLE 或上面那个 no_reset 探针。

### A12. NimBLE 2.x 编译不过

2.x 改了回调签名(`onWrite(NimBLECharacteristic*, NimBLEConnInfo&)`)。
本库当前只支持 `h2zero/NimBLE-Arduino@^1.4.2`。

---

## B. 中枢侧(macOS)—— 权限与进程

### B1. ★ 事件流停在 `central_created`,再也没有下文

这是**最容易误判**的一条,因为它有三个完全不同的成因,症状一模一样。

先看健康的事件序列长什么样:

```
launch → central_created → central_state:5 → scan_started → discovered… → connected
```

`central_state:5` 就是 `CBManagerState.poweredOn`。**25 秒内没有 `central_state`
回调 = 中招了。** 三个可能:

| 成因 | 判别 | 解 |
|---|---|---|
| 进程没有 bundle 身份 | 是不是直接跑了 `Contents/MacOS/xxx`? | 必须 `open -g -j -n <app>` |
| 进程不在 GUI(Aqua)会话里 | 是不是从 SSH 会话 `open` 的? | 监护进程必须是 LaunchAgent(`gui/$UID`) |
| Info.plist 缺 TCC 文案 | `PlistBuddy -c 'Print :NSBluetoothAlwaysUsageDescription'` | 补上那两个 key |

第二条尤其误导人:进程活着、bundle 身份正常、TCC 里明明是 allowed —— 但
`centralManagerDidUpdateState` 永远不回调。**从 SSH 会话 `open` 出来的 helper
拿不到 `bluetoothd`。** 手工调试时这一点会反复咬人。

> **⚠️ 关于第一条,一个容易得出错误结论的观察。**
> 从**终端手工**直接跑 `Contents/MacOS/xxx`,常常是能正常工作的 —— 拿得到
> `central_state:5`,扫得到设备。这不代表 bundle 身份不重要,而是因为
> TCC 有「responsible process」的概念:从 Terminal 启动的进程,权限归属会算到
> Terminal.app 头上,而 Terminal 多半早就被授过蓝牙权限了。
>
> 一旦改由 launchd / LaunchAgent 启动,就没有这么一个「有权限的爹」可以借,
> bundle 自己的身份才成为唯一依据 —— 这时直接 exec 就废了。
>
> 所以:**手工调试通过 ≠ 常驻部署能用**。这两种启动方式的权限路径根本不同,
> 别拿前者的成功去推翻后者的失败。

> **⚠️ 修正(实测):GUI 会话只有「首次授权」那一次需要。**
> 早先的说法是「SSH 里 open 出来的 helper 拿不到 bluetoothd」。后来分离变量重测,
> 发现那次结论把两件事混在了一起 —— 当时既是**直接 exec 二进制**(没有 bundle 身份,
> TCC 无从匹配),又**还没有任何授权记录**(所以只能等一个弹不出来的弹框)。
>
> 实际规律是:
> - **直接 exec bundle 内的二进制** → 没有 bundle 身份 → 怎么都不行,和会话无关
> - **`open -g -j -n <app>` + TCC 里已有该 bundle id 的授权** → **从 SSH / launchd
>   照样能用**,拿得到 `central_state:5`
> - 缺的只是**第一次那个授权框**,它必须在 GUI(Aqua)会话里弹出来给人点
>
> 判断当前有没有授权(TCC.db 可读):
> ```bash
> sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db \
>   "select client,auth_value from access where service='kTCCServiceBluetoothAlways'"
> # auth_value: 2=允许 0=拒绝;查不到该 bundle id = 从没请求过
> ```
> **「查不到」和「0」意义完全不同**:查不到说明 app 根本没走到创建 CBCentralManager
> 那一步(最常见的原因是启动参数不全,它在解析参数时就退出了),这时再怎么等也不会弹框。

如果拿到的是 `central_state:3`(unauthorized),那才是真的 TCC 问题,
去 系统设置 → 隐私与安全性 → 蓝牙 里允许。helper 会 emit 一条明确的 error,
Python 侧据此标记 `fatal` 并停止重连 —— 因为重试一万次也没用。

### B2. macOS 上用 Python + bleak 做中枢,授权时好时坏

**macOS 把蓝牙 TCC 授给「实际接触 CoreBluetooth 的那个 bundle 可执行文件」。
Python 进程永远不可能是那个文件** —— 套什么壳都不行,`exec` 会替换镜像,
TCC 跟着新镜像走。

这就是为什么本框架的中枢是编译出来的 Swift `.app`,由 Python 以子进程驱动,
而不是直接 `pip install bleak`。

### B3. 装了新 helper 之后,另一个 helper 再也起不来

两个 bundle 用了同一个 `CFBundleIdentifier`,LaunchServices 把它们当成同一个 app。

**每个 helper 必须有独立的 bundle id。** `build_helper.sh` 会用 `mdfind` 查一遍
撞没撞,撞了直接拒绝构建。已知占用见 [bundle-ids.md](bundle-ids.md)。

> **⚠️ 这条规则的范围曾经写窄了:是「每个 helper 产品一个 bundle」,
> 不是「每台设备一个」。**
> 要解决的是不同产品之间撞车(CodeBuddy / claude-stick / 本框架)。
> 同一个产品接多台设备时,**共用一个 bundle 即可** —— `open -n` 的语义是强制
> 新实例,所以一个 bundle 能开出 N 个进程,各有各的 `CBCentralManager`,
> 而 TCC 只需授权一次。已实测:同 bundle 两实例都拿到 `central_state:5`,互不干扰。
>
> 这很重要,因为「每台设备一个 bundle」意味着每加一台设备就要人去点一次授权框,
> 多设备场景下根本不可用。做法见 [porting.md 接多台设备](porting.md)。

顺带一提:同一台 Mac 上**多个 CoreBluetooth central 并发是没问题的**,
实测三个 helper 各连各的设备同时跑得好好的。问题只出在 bundle id 撞车。

### B4. 重连陷入死循环,helper 永远起不来

`open` 在 LaunchServices **收下请求**时就返回了,那时进程还不存在。
立刻检查存活会得到「已退出」→ 触发重连 → 重连时的 kill 又把刚要起来的 helper
杀掉 → 永不收敛。

必须**轮询等进程真的出现**(`HelperSession.start()` 里等 10 秒)。

### B5. 起了一个 helper,把别人的 helper 也弄死了

按二进制名杀进程(`pkill -f SomeBLEHelper`)会误伤。同一台机器上常同时跑好几个
helper —— 必须**按 session-dir 匹配**,它每个会话唯一。

### B6. helper 一启动就重放了一堆旧命令

上次会话残留的 command 文件被当成新的读了,残留的 `events.jsonl` 被从头再读一遍。
**每次启动都用全新的 session 目录**(`start()` 里 `rmtree` 再建)。

### B7. `launchctl bootstrap` 报 `Bootstrap failed: 5: Input/output error`

旧实例还挂着。`bootout` → `sleep 2` → `bootstrap`。

### B8. adhoc 自签的 helper 一运行就消失

装了 EDR 类安全软件的机器(典型是公司配发的电脑)会 SIGKILL adhoc 自签二进制。
构建能过、一运行就没。换台机器构建和运行,别在这上面浪费时间。

---

## C. 中枢侧 —— 连接与收发

### C1. 连上了别人的设备

按 service UUID 过滤扫描结果会连上任何一台跑 NUS 的设备。一张桌子上常有好几台。

本框架**全扫 + 按名字精确匹配**,service UUID 只用来收窄 `discoverServices`。
而且三个匹配条件(`device_name` / `name_prefix` / `device_id`)一个都不给时,
helper **谁都不连** —— 与其连错,不如不连。

### C2. 掉线后死活恢复不了,重启进程就好了

**macOS CoreBluetooth 的坏状态是进程级的。** 在同一个 `CBCentralManager` 里反复
retry 只会越积越坏。

本框架的恢复策略是:任何失败(连不上/掉线/扫描超时)helper 都自我终结,
由 Python 起一个**全新进程**。这也是为什么 `HelperSession` 是一次性对象。

### C3. 冷启动时一台设备都没扫到,而且永远不超时

上游的实现把扫描超时检查放在了「订阅成功后才启动」的定时器里 —— 冷启动一个都没
发现时那个定时器根本没跑起来。**扫描超时看门狗必须是独立的 Timer。**

### C4. 明明连着,发出去的东西设备没收到

`ping` 只走到 helper 进程,**探不到 BLE 链路**。链路可能已经是个僵尸了而 helper
还活得好好的。

唯一可靠的探测是**发一次真实的 ATT 写**。所以 `BleLink` 的 keepalive 会定期重发
retained 帧(一次真实的 write + ack)。相应地,**设备侧必须能识别「内容没变就别重绘」**
(通常靠一个 rev 字段),否则墨水屏之类会被 keepalive 刷爆。

### C5. 大消息发一半就断

连续猛写不等 ATT ack 会把协议栈的缓冲打空。本框架**严格一次只有一片在飞**:
下一片只在 `didWriteValueFor` 回调里发。分片长度取
`min(chunk_ceiling, maximumWriteValueLength(for: .withResponse))`。

外设侧 notify 同理:`EspBleLink::notify()` 按协商 MTU 分片,片间 `delay(4)` 让
协议栈冲刷。

### C6. 设备 notify 上来的内容粘在一起 / 少最后一条

外设 notify 没补分隔符。`EspBleLink::notify()` 会自动补,所以自家固件不会有这问题。

对接**别人的**固件时,如果它不补分隔符,用 `accept_unterminated=True` 打开兼容模式
—— 它靠「这段文本是不是以 `}` 结尾」猜边界,本质是猜,只在没得选时才开。

### C7. 重连后设备上的历史列表空了 / 状态是旧的

BLE 没有 MQTT 的 retained 和离线队列。用 `RetainedChannel`:
`set_retained()` 的内容每次重连自动补推,`publish()` 的事件进历史环、
重连时以 `live=False` 重放(设备据此只进列表、不重复蜂鸣)。

**未连接时的即时消息是直接丢弃的**,这是有意的:攒着的消息重连后会一次灌爆设备。

### C8. 设备整天不在时,Mac 上其它 helper 的链路变差了

持续扫描会占用射频。固定 5s 重试 + 20s 扫描 = 每天几千次进程启动,而且干扰同机
其它 helper。

`BleLink` 用两级退避:前几次快(撞设备刚回来的窗口),之后放缓。
**别改成指数退避** —— 设备要么在(几秒内就连上)要么不在(等多久都白搭),
指数退避唯一的作用是把「设备刚回来」那一刻错过去。

退避上限也别设太大:很多设备只在特定窗口广播,退避超过那个窗口就永远撞不上。
