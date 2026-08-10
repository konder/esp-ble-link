// EspBleLink —— ESP32 BLE 外设链路层(Nordic UART Service + 分隔符组帧)
//
// 这一层只负责「把字节可靠地搬过 BLE」:广播、连接、分帧、重组、重连后重新广播。
// 它不认识 JSON,不认识业务 —— 拿到的是一行完整的 UTF-8 文本,怎么解释是调用方的事。
//
// 配套的中枢在 host/(macOS 原生 Swift helper + Python 会话层)。两侧的线协议
// 见 docs/protocol.md,历代踩的坑见 docs/pitfalls.md。
//
// ============================================================================
// 三条不可协商的纪律 —— 每一条都是拿掉线换来的,改之前先读 docs/pitfalls.md
// ============================================================================
//
// 1. BLE 回调里只搬字节,不干活。
//    onWrite 跑在 NimBLE 主机任务里。组帧、JSON 解析、屏幕渲染全部留给主循环。
//    反面教材:早期版本在 onWrite 里逐字节 `String += ch`,一条 4KB 的消息就是
//    上千次堆重分配**发生在协议栈任务里** —— 主机任务被饿死,中心侧判监管超时。
//
// 2. 外设永不请求连接参数。
//    本文件里**不存在** updateConnParams,将来也不要加。历史:某版在连上瞬间请求
//    timeout=10s(违反 Apple 的 ≤6s 硬规)→ 被拒 → rc=7 BLE_HS_ENOTCONN /
//    断连 reason=520(监管超时)。后来有一版只删了调用点、把函数体留着,等于埋雷。
//    外设唯一该碰参数的地方是广播包里的 min/maxPreferred **建议值**(见 begin())。
//    真正的连接参数由中心决定,这是设计,不是妥协。
//
// 3. 断连时清空环形缓冲和半截帧,再重新广播。
//    否则上一次连接遗留的残字节会和下一次的首帧拼成一条烂消息。
//
// ============================================================================
// 依赖:NimBLE-Arduino ^1.4.2
// ⚠️ NimBLE 2.x 改了回调签名(onWrite 多一个 NimBLEConnInfo& 参数),本库尚未适配。
// ============================================================================
#pragma once

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

namespace espble {

// Nordic UART Service —— 事实标准,中枢侧(含 CodeBuddy 系的 stick)都认这套。
// 换成自定义 UUID 也行,但两侧要一起换。
//
// ⚠️ 名字刻意用 kXxx 而不是 NUS_RX 这种 ALL_CAPS。**宏不认 namespace** ——
//    消费方只要写一句 `#define NUS_RX "…"`(对一个 NUS 项目来说是最自然的写法),
//    下面的声明就会被展开成 `extern const char* const "6E400002-…";`,
//    报出来的是 "expected unqualified-id before string constant",而且指向的是
//    **它自己的** config.h,完全看不出是库占了名字。ALL_CAPS 归宏所有,库别去抢。
//    (这不是假设 —— m5work 迁移时就是这么炸的。)
extern const char* const kNusService;   // 6E400001-B5A3-F393-E0A9-E50E24DCCA9E
extern const char* const kNusRx;        // 6E400002-…  中心→外设,WRITE | WRITE_NR
extern const char* const kNusTx;        // 6E400003-…  外设→中心,NOTIFY

struct LinkConfig {
    // ---- 设备身份 ----
    //
    // 广播名的默认形态是 `<deviceType>-<deviceId>`,例如 `m5paper-c119cc`。
    // 中枢按 `<type>-` 前缀发现同类设备,按全名认具体是哪一台。
    //
    // deviceId 留空时**自动取 efuse MAC 的后三字节**(十六进制)。这意味着
    // 同一份固件烧十块板子会得到十个不同身份 —— 不必为了区分设备去改常量重编,
    // 那是多设备场景里最容易出错的一步。
    //
    // 为什么身份塞在名字里而不是 manufacturer data:BLE 广播包只有 31 字节,
    // 一个 128 位 service UUID 就吃掉 18 字节,再塞厂商数据基本没地方了
    // (名字本身现在也是靠 scan response 才装得下)。塞名字零额外开销,还人眼可读。
    const char* deviceType = "espble";
    const char* deviceId   = nullptr;    // nullptr → 自动取 efuse MAC 后 3 字节

    // 显式指定广播名。设了就完全覆盖上面的 `<type>-<id>` 拼装。
    // ⚠️ 多设备场景下别用 —— 写死的名字意味着每台设备一份固件。
    const char* deviceName = nullptr;

    // 随 hello 帧上报给中枢,供其判断「这台设备吃哪些消息」。
    int         fwVersion = 0;
    const char* caps      = "";          // 逗号分隔,如 "usage,ev,cmd"

    const char* serviceUuid = nullptr;   // nullptr → kNusService
    const char* rxUuid      = nullptr;   // nullptr → kNusRx
    const char* txUuid      = nullptr;   // nullptr → kNusTx

    // 允许协商到的最大 ATT MTU。设大只是「允许」,实际值由中心决定
    // (macOS 实测常落在 185)。notify 的分片长度按协商结果动态算,不写死。
    uint16_t mtu = 517;

    // 接收环形缓冲字节数。这是**真正的单帧上限**:中枢一次写入的一整行必须能在
    // 主循环下次排空之前塞得下。默认 2048 够放两条千字级中文消息。
    // 中枢侧的 fit_line() 应按这个值收窄单行上限(默认取 90%)。
    size_t rxRingBytes = 2048;

    // 单条重组消息的上限。超了整条丢弃(防御异常/恶意的超长帧)。
    size_t maxFrameBytes = 4096;

    // 帧分隔符。两侧必须一致。
    char delimiter = '\n';

    // notify 单片上限。**别改大。**
    // 实测:按 MTU-3(协商到 517 时就是 514)连续推几片,NimBLE 的 mbuf 池会被打空,
    // notify() 开始返回 false,中枢那边收到的是残帧 —— 现象是长消息回来变成
    // 一段乱码,而短消息一切正常。180 是 CodeBuddy 系固件用了很久的保守值。
    // 实际分片长度 = min(ATT_MTU - 3, notifyChunkMax)。
    size_t notifyChunkMax = 180;

    // 多片 notify 的片间停顿,给协议栈时间把上一片冲出去。
    // ⚠️ NimBLE-Arduino 1.4.x 的 notify() 返回 void —— 协议栈吃不下时**我们收不到
    //    任何信号**,没法重试。所以这个停顿不是优化,是正确性的一部分。
    uint16_t notifyChunkDelayMs = 4;

    // 发射功率(dBm)。硬件档位是 -12/-9/-6/-3/0/+3/+6/+9,会取最接近的一档。
    int8_t txPowerDbm = 9;

    // 广播里给中心的连接参数**建议值**(单位 1.25ms):0x06=7.5ms, 0x12=22.5ms。
    // 这是外设唯一可以碰连接参数的地方(见文件头纪律 2)。
    uint16_t advMinInterval = 0x06;
    uint16_t advMaxInterval = 0x12;
};

// 运行统计。排查「帧损坏」「消息丢了」时先看这里 —— 这几个计数器能直接区分
// 「中枢没发」「设备来不及收(ring 溢出)」「帧超长被丢」三种情况。
struct LinkStats {
    uint32_t rxBytes        = 0;   // 从 BLE 收到的原始字节
    uint32_t rxDroppedBytes = 0;   // 环形缓冲满而丢弃 —— 非 0 说明主循环排空太慢
    uint32_t rxFrames       = 0;   // 成功组出的完整帧
    uint32_t rxOversize     = 0;   // 超过 maxFrameBytes 被整条丢弃的帧
    uint32_t txFrames       = 0;   // notify 出去的帧
    uint32_t txChunks       = 0;   // notify 实际分了多少片
                                   // (没有 txDropped —— NimBLE 1.4.x 的 notify()
                                   //  返回 void,发失败我们根本不知道)
    uint32_t connects       = 0;   // 累计建连次数(排查 flapping)
    uint32_t disconnects    = 0;
    // 下面两个是可见性看门狗的战绩。**非 0 就说明发生过"设备本来会失联"的情况** ——
    // 对 BLE-only 的设备(没有 WiFi 兜底、失联就只能接 USB)这是最该盯的指标。
    uint32_t staleDrops     = 0;   // 「以为连着、其实早断了」被纠正的次数(断连回调丢了)
    uint32_t advRestarts    = 0;   // 「没连着又没广播」被重新拉起广播的次数
};

// 连接状态变化回调。⚠️ 它在 NimBLE 主机任务里被调用 —— 只能置标志位,
// 不要在里面渲染、写 flash 或做任何耗时操作(纪律 1)。
using ConnectionCallback = void (*)(bool connected);

// 初始化并开始广播。重复调用会先 end()。失败返回 false(通常是内存不够)。
bool begin(const LinkConfig& cfg = LinkConfig());

// 停止并彻底释放 BLE(NimBLEDevice::deinit)。
//
// ⚠️ **在 NimBLE-Arduino 1.4.x + Arduino ESP32 core 3.x 上这个函数会 panic。**
//    1.4.x 是照 IDF 4.x 写的,core 3.x 底下是 IDF 5.x,HCI deinit 的 API 变了:
//    `esp_nimble_hci_and_controller_deinit() failed with error: 259`
//    (ESP_ERR_INVALID_STATE),紧接着死在 heap_caps_free 的断言上。
//    正常收发完全不受影响 —— **只有拆栈这条路是坏的**。
//    要临时让出射频请用 quiesce();要彻底释放请重启设备。
//    详见 docs/pitfalls.md A9。
//
// ⚠️ 0.1.1 之前的版本在**所有** core 版本上都会 panic —— 那是另一个成因(回调对象
//    所有权,见 pitfalls A13),已修。如果你是从 0.1.0 升上来的,`end()` 现在是可用的:
//    m5paper-monitor 就靠它在 BLE 超时后让出 2.4G 给 WiFi。
void end();

// 让 BLE 安静下来:停广播 + 断开对端,但**保留协议栈**。
//
// 这是 end() 在 core 3.x 上的安全替代品。做 OTA 之类要用 WiFi 的事情之前调它:
// 射频占用降下来了(不再广播、没有连接事件),又不碰那条会 panic 的 deinit 路径。
// 之后调 resume() 恢复广播。
void quiesce();

// 从 quiesce() 恢复:重新开始广播。
void resume();

// 是否已初始化(begin 成功且未 end)。
bool started();

// 本机身份。begin() 之后才有意义。
// deviceId 若未显式配置,这里返回自动派生的那个(efuse MAC 后 3 字节)。
const char* deviceId();
const char* deviceName();   // 实际用于广播的名字

// 当前是否有中心连着。
bool connected();

// 协商后的 ATT MTU;未连接时返回 0。
uint16_t peerMtu();

void onConnectionChange(ConnectionCallback cb);

// 非阻塞:从环形缓冲组帧,取出一条完整消息(不含分隔符)。
// **必须在主循环里调用**,而且要调到返回 false 为止 —— 一次只交一条。
bool popMessage(String& out);

// 阻塞至多 waitMs 取一条消息。内部靠信号量等待,期间 CPU 真的能进轻睡眠
// (对电池设备有意义;忙等的 delay 轮询做不到这点)。
bool popMessageWait(String& out, uint32_t waitMs);

// 经 TX 特征 notify 一行文本给中心。
// 自动补分隔符、自动按协商 MTU 分片、片间让出 CPU 给协议栈冲刷。
// 未连接时静默丢弃并返回 false。
//
// ⚠️ 这里自动补分隔符是有来由的:早期固件的 notify 既不补分隔符也不分片,
// 逼得中枢侧靠「这段文本是不是以 } 结尾」来猜边界。发送端做对,中枢就不用猜。
bool notify(const String& payload);
bool notify(const uint8_t* data, size_t len);

LinkStats stats();
void resetStats();

}  // namespace espble
