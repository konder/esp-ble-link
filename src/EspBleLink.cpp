#include "EspBleLink.h"

#include <NimBLEDevice.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

namespace espble {

const char* const NUS_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E";
const char* const NUS_RX      = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E";
const char* const NUS_TX      = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E";

namespace {

LinkConfig s_cfg;
bool       s_started = false;

NimBLEServer*         s_server = nullptr;
NimBLECharacteristic* s_tx     = nullptr;

volatile bool     s_connected = false;
volatile uint16_t s_mtu       = 0;

ConnectionCallback s_connCb = nullptr;

// ---- 接收环形缓冲(单生产者 / 单消费者)-------------------------------------
// 生产者 = NimBLE 主机任务,只推进 head;消费者 = 主循环,只推进 tail。
// 各自只写自己那个索引,所以 volatile 就够,不需要锁、不需要 FreeRTOS 队列。
// 加锁反而危险:在协议栈任务里等锁正是把主机任务饿死的经典做法。
uint8_t*         s_ring     = nullptr;
size_t           s_ringCap  = 0;
volatile size_t  s_ringHead = 0;
volatile size_t  s_ringTail = 0;

// 组帧缓冲。**只有主循环碰它**,所以可以放心用 String 做动态增长。
String s_assembly;

// popMessageWait 的等待信号量:收到字节就 give,等的人立刻醒。
// 比 delay 轮询强在电池设备上 CPU 真的能睡到有数据为止。
SemaphoreHandle_t s_rxSignal = nullptr;

LinkStats s_stats;

inline size_t ringNext(size_t i) { return (i + 1) % s_ringCap; }

// 只在 NimBLE 主机任务里调用:memcpy 进环,立刻返回。不做任何解析。
void ringPush(const uint8_t* p, size_t n) {
    s_stats.rxBytes += n;
    for (size_t i = 0; i < n; i++) {
        size_t next = ringNext(s_ringHead);
        if (next == s_ringTail) {
            // 满了。丢弃剩余字节,**绝不阻塞协议栈**。
            // 计入 rxDroppedBytes:排查时看到它非 0,就知道是主循环排空太慢
            // (或者中枢单行超过了 rxRingBytes),而不是链路本身丢包。
            s_stats.rxDroppedBytes += (n - i);
            return;
        }
        s_ring[s_ringHead] = p[i];
        s_ringHead = next;
    }
}

class RxCallbacks : public NimBLECharacteristicCallbacks {
    void onWrite(NimBLECharacteristic* c) override {
        NimBLEAttValue v = c->getValue();
        // data()/length() 而不是 c_str():负载里出现 NUL 时 c_str() 会截断后面的内容。
        if (v.length()) {
            ringPush(v.data(), v.length());
            if (s_rxSignal) xSemaphoreGive(s_rxSignal);
        }
    }
};

class ServerCallbacks : public NimBLEServerCallbacks {
    void onConnect(NimBLEServer* server, ble_gap_conn_desc* desc) override {
        s_connected = true;
        s_mtu = server->getPeerMTU(desc->conn_handle);
        s_stats.connects++;
        // ⚠️ 这里**没有** updateConnParams,将来也不要加(见 EspBleLink.h 纪律 2)。
        if (s_connCb) s_connCb(true);
    }

    void onDisconnect(NimBLEServer* server) override {
        s_connected = false;
        s_mtu = 0;
        s_stats.disconnects++;
        // 丢掉半截帧,别让残字节污染下一次连接。
        s_ringHead = s_ringTail = 0;
        s_assembly = "";
        server->startAdvertising();
        if (s_connCb) s_connCb(false);
    }

    void onMTUChange(uint16_t mtu, ble_gap_conn_desc* desc) override {
        (void)desc;
        s_mtu = mtu;
    }
};

RxCallbacks*     s_rxCallbacks     = nullptr;
ServerCallbacks* s_serverCallbacks = nullptr;

// dBm → NimBLE 的功率档位枚举(硬件只有这 8 档,取最接近的)。
esp_power_level_t toPowerLevel(int8_t dbm) {
    struct { int8_t dbm; esp_power_level_t lvl; } table[] = {
        {-12, ESP_PWR_LVL_N12}, {-9, ESP_PWR_LVL_N9}, {-6, ESP_PWR_LVL_N6},
        {-3, ESP_PWR_LVL_N3},   {0, ESP_PWR_LVL_N0},  {3, ESP_PWR_LVL_P3},
        {6, ESP_PWR_LVL_P6},    {9, ESP_PWR_LVL_P9},
    };
    esp_power_level_t best = ESP_PWR_LVL_P9;
    int bestDelta = 127;
    for (auto& e : table) {
        int delta = abs(int(dbm) - int(e.dbm));
        if (delta < bestDelta) { bestDelta = delta; best = e.lvl; }
    }
    return best;
}

// 单片 notify 能带多少字节:ATT_MTU - 3 字节的 ATT 头。
// 未协商到(部分中心不主动交换 MTU)时退回 BLE 规定的默认 23。
size_t notifyChunkSize() {
    uint16_t mtu = s_mtu ? s_mtu : 23;
    return mtu > 3 ? size_t(mtu - 3) : 20;
}

}  // namespace

bool begin(const LinkConfig& cfg) {
    if (s_started) end();
    s_cfg = cfg;

    if (s_cfg.rxRingBytes < 64) s_cfg.rxRingBytes = 64;
    s_ring = (uint8_t*)malloc(s_cfg.rxRingBytes);
    if (!s_ring) return false;
    s_ringCap  = s_cfg.rxRingBytes;
    s_ringHead = s_ringTail = 0;
    s_assembly = "";

    if (!s_rxSignal) s_rxSignal = xSemaphoreCreateBinary();
    if (!s_rxSignal) { free(s_ring); s_ring = nullptr; return false; }

    const char* svcUuid = s_cfg.serviceUuid ? s_cfg.serviceUuid : NUS_SERVICE;
    const char* rxUuid  = s_cfg.rxUuid      ? s_cfg.rxUuid      : NUS_RX;
    const char* txUuid  = s_cfg.txUuid      ? s_cfg.txUuid      : NUS_TX;

    NimBLEDevice::init(s_cfg.deviceName);
    NimBLEDevice::setPower(toPowerLevel(s_cfg.txPowerDbm));
    NimBLEDevice::setMTU(s_cfg.mtu);   // 只是「允许协商到这么大」,实际由中心定

    s_serverCallbacks = new ServerCallbacks();
    s_rxCallbacks     = new RxCallbacks();

    s_server = NimBLEDevice::createServer();
    s_server->setCallbacks(s_serverCallbacks);

    NimBLEService* svc = s_server->createService(svcUuid);
    NimBLECharacteristic* rx = svc->createCharacteristic(
        rxUuid, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
    rx->setCallbacks(s_rxCallbacks);
    s_tx = svc->createCharacteristic(txUuid, NIMBLE_PROPERTY::NOTIFY);
    svc->start();

    NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
    adv->addServiceUUID(svcUuid);
    adv->setScanResponse(true);
    // 唯一允许碰连接参数的地方:广播里的**建议值**。中心可以完全无视它。
    adv->setMinPreferred(s_cfg.advMinInterval);
    adv->setMaxPreferred(s_cfg.advMaxInterval);
    NimBLEDevice::startAdvertising();

    s_started = true;
    return true;
}

void end() {
    if (!s_started) return;
    NimBLEDevice::deinit(true);
    s_server = nullptr;
    s_tx     = nullptr;
    s_connected = false;
    s_mtu = 0;
    // NimBLE 的 deinit 会销毁 server/service/characteristic,但 callbacks 对象是
    // 我们 new 出来的,由我们负责。
    delete s_serverCallbacks; s_serverCallbacks = nullptr;
    delete s_rxCallbacks;     s_rxCallbacks     = nullptr;
    free(s_ring); s_ring = nullptr;
    s_ringCap = 0;
    s_ringHead = s_ringTail = 0;
    s_assembly = "";
    s_started = false;
}

bool started()   { return s_started; }
bool connected() { return s_connected; }
uint16_t peerMtu() { return s_connected ? s_mtu : 0; }

void onConnectionChange(ConnectionCallback cb) { s_connCb = cb; }

bool popMessage(String& out) {
    if (!s_started) return false;
    while (s_ringTail != s_ringHead) {
        char ch = (char)s_ring[s_ringTail];
        s_ringTail = ringNext(s_ringTail);
        if (ch == s_cfg.delimiter || ch == '\r') {
            // '\r' 一并当分隔符:中枢若发 CRLF,第二个字节会命中空帧分支被跳过。
            if (s_assembly.length()) {
                out = s_assembly;
                s_assembly = "";
                s_stats.rxFrames++;
                return true;
            }
            continue;   // 空行 / CRLF 的另一半
        }
        if (s_assembly.length() < s_cfg.maxFrameBytes) {
            s_assembly += ch;
        } else {
            // 异常超长帧:整条丢弃,等下一个分隔符重新开始。
            s_assembly = "";
            s_stats.rxOversize++;
        }
    }
    return false;
}

bool popMessageWait(String& out, uint32_t waitMs) {
    if (!s_started) return false;
    uint32_t start = millis();
    for (;;) {
        if (popMessage(out)) return true;
        uint32_t elapsed = millis() - start;
        if (elapsed >= waitMs) return false;
        // 等信号量而不是 delay 轮询:没数据时 CPU 可以一路睡到中枢写进来。
        xSemaphoreTake(s_rxSignal, pdMS_TO_TICKS(waitMs - elapsed));
    }
}

bool notify(const uint8_t* data, size_t len) {
    if (!s_started || !s_connected || !s_tx || !data) return false;

    const size_t chunk = notifyChunkSize();
    const bool needDelimiter = (len == 0) || (data[len - 1] != (uint8_t)s_cfg.delimiter);

    size_t sent = 0;
    while (sent < len) {
        size_t n = len - sent;
        if (n > chunk) n = chunk;
        // 最后一片如果还有空位,把分隔符捎上,省一次 notify。
        bool tailFits = (sent + n == len) && needDelimiter && (n < chunk);
        if (tailFits) {
            uint8_t buf[517];
            size_t copy = n < sizeof(buf) - 1 ? n : sizeof(buf) - 1;
            memcpy(buf, data + sent, copy);
            buf[copy] = (uint8_t)s_cfg.delimiter;
            s_tx->setValue(buf, copy + 1);
        } else {
            s_tx->setValue(data + sent, n);
        }
        s_tx->notify();
        s_stats.txChunks++;
        sent += n;
        if (tailFits) { s_stats.txFrames++; return true; }
        // 片间让出 CPU,给协议栈时间把这一片冲出去。连续猛推会把 NimBLE 的
        // mbuf 池打空,表现为 notify 静默失败。
        if (sent < len) delay(4);
    }

    if (needDelimiter) {
        uint8_t d = (uint8_t)s_cfg.delimiter;
        s_tx->setValue(&d, 1);
        s_tx->notify();
        s_stats.txChunks++;
    }
    s_stats.txFrames++;
    return true;
}

bool notify(const String& payload) {
    return notify((const uint8_t*)payload.c_str(), payload.length());
}

LinkStats stats() { return s_stats; }
void resetStats() { s_stats = LinkStats(); }

}  // namespace espble
