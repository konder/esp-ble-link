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
extern const char* const NUS_SERVICE;   // 6E400001-B5A3-F393-E0A9-E50E24DCCA9E
extern const char* const NUS_RX;        // 6E400002-…  中心→外设,WRITE | WRITE_NR
extern const char* const NUS_TX;        // 6E400003-…  外设→中心,NOTIFY

struct LinkConfig {
    // 广播名。中枢默认按**精确名**匹配 —— 一张桌子上往往插着好几根 NUS 设备,
    // 只按 service UUID 过滤会抢到别人的(docs/pitfalls.md #7)。
    const char* deviceName = "EspBleDevice";

    const char* serviceUuid = nullptr;   // nullptr → NUS_SERVICE
    const char* rxUuid      = nullptr;   // nullptr → NUS_RX
    const char* txUuid      = nullptr;   // nullptr → NUS_TX

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
    uint32_t connects       = 0;   // 累计建连次数(排查 flapping)
    uint32_t disconnects    = 0;
};

// 连接状态变化回调。⚠️ 它在 NimBLE 主机任务里被调用 —— 只能置标志位,
// 不要在里面渲染、写 flash 或做任何耗时操作(纪律 1)。
using ConnectionCallback = void (*)(bool connected);

// 初始化并开始广播。重复调用会先 end()。失败返回 false(通常是内存不够)。
bool begin(const LinkConfig& cfg = LinkConfig());

// 停止并彻底释放 BLE,把 2.4G 射频让给 WiFi。做 OTA 前必须调
// (ESP32 的 BLE 和 WiFi 共用射频,同时开会互相拖垮)。
void end();

// 是否已初始化(begin 成功且未 end)。
bool started();

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
