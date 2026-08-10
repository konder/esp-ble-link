#include "EspBleLink.h"

#include <NimBLEDevice.h>
#include <esp_mac.h>       // esp_read_mac —— 设备身份从 efuse MAC 派生
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

namespace espble {

const char* const kNusService = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E";
const char* const kNusRx      = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E";
const char* const kNusTx      = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E";

namespace {

LinkConfig s_cfg;
bool       s_started = false;

// end() 期间置位。onDisconnect 靠它区分「对端走了,该重新广播」和
// 「是我们自己在拆栈,别碰它」—— 见 end() 里的成因注释。
volatile bool s_stopping = false;

NimBLEServer*         s_server = nullptr;
NimBLECharacteristic* s_tx     = nullptr;

volatile bool     s_connected  = false;
volatile uint16_t s_mtu        = 0;
volatile uint16_t s_connHandle = 0;

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

// notify 拼「最后一片 + 分隔符」用的暂存区。只有主循环碰它。
// 256 覆盖 notifyChunkMax 的默认值;配得更大时最后的分隔符会单独发一片,不影响正确性。
uint8_t s_txScratch[256];

// ---- 设备身份 ----
char s_id[16]   = {0};    // 如 "c119cc"
char s_name[48] = {0};    // 如 "m5paper-c119cc"
volatile bool s_helloPending = false;   // onConnect 里置位,由主循环发出(回调里不干活)
uint32_t s_lastVisCheck = 0;            // 可见性看门狗的节流时间戳

// efuse MAC 的后三字节。同一份固件烧多块板子会自动得到不同身份。
// 用 esp_read_mac 而不是 ESP.getEfuseMac():后者返回的字节序是反的,拼出来的 id
// 和你在 USB 描述符、系统蓝牙列表里看到的 MAC 对不上,排查时非常误导。
void deriveId(char* out, size_t n) {
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(out, n, "%02x%02x%02x", mac[3], mac[4], mac[5]);
}

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
        s_connHandle = desc->conn_handle;
        s_mtu = server->getPeerMTU(desc->conn_handle);
        s_stats.connects++;
        // ⚠️ 这里**没有** updateConnParams,将来也不要加(见 EspBleLink.h 纪律 2)。
        //
        // ⚠️ 这里**也不置 s_helloPending**。曾经置在这儿,结果 hello 被稳定丢掉:
        //    onConnect 之后 ~20ms 主循环就把 hello notify 出去了,而中枢要走完
        //    discoverServices → discoverCharacteristics → setNotifyValue 才订阅上 TX。
        //    发在订阅之前 = 没有订阅者 = 被协议栈静默丢弃,而 1.4.x 的 notify() 返回 void,
        //    设备连"这条没发出去"都不知道。现象是中枢永远拿不到 fw/caps。
        //    正确的时机是**对方订阅上来那一刻** —— 见下面的 TxCallbacks::onSubscribe。
        if (s_connCb) s_connCb(true);
    }

    void onDisconnect(NimBLEServer* server) override {
        s_connected = false;
        s_mtu = 0;
        s_stats.disconnects++;
        s_helloPending = false;   // 对方走了,这次的自我介绍作废
        // 丢掉半截帧,别让残字节污染下一次连接。
        s_ringHead = s_ringTail = 0;
        s_assembly = "";
        // ⚠️ 拆栈途中绝不能再开广播。end() 会先断连,这个回调随之触发;
        //    此时 startAdvertising() 会 rc=30 失败,进而让 deinit 也失败,
        //    最后堆损坏 panic。成因见 docs/pitfalls.md A8。
        if (s_stopping) return;
        server->startAdvertising();
        if (s_connCb) s_connCb(false);
    }

    void onMTUChange(uint16_t mtu, ble_gap_conn_desc* desc) override {
        (void)desc;
        s_mtu = mtu;
    }
};

// TX 特征的回调,只为了一件事:知道对方**什么时候真的开始听**。
//
// BLE 的 notify 只有在中枢写了 CCCD(订阅)之后才会真的发出去,之前发的被静默丢弃。
// 所以"自我介绍"(hello)必须等到这一刻,不能在 onConnect 里发。
class TxCallbacks : public NimBLECharacteristicCallbacks {
    void onSubscribe(NimBLECharacteristic* chr, ble_gap_conn_desc* desc,
                     uint16_t subValue) override {
        (void)chr; (void)desc;
        // subValue: bit0=notify, bit1=indicate。只关心 notify 打开的那一下。
        if (subValue & 0x01) {
            s_helloPending = true;   // 真正的拼串与发送留给主循环(纪律 1)
        }
    }
};

// 回调对象做成静态单例而不是 new/delete。
// deinit 失败时协议栈可能还留着指向它们的指针,那时 delete 就是 use-after-free;
// 而反复 begin/end 又不能泄漏。静态单例把这两个问题一起消掉。
//
// ⚠️ 但静态单例有个必须配套的下半句:**NimBLE 的 setCallbacks 默认要拿所有权**。
//    `setCallbacks(cb, bool deleteCallbacks = true)` —— 默认 true,于是 ~NimBLEServer
//    会 `delete` 它。delete 一个 .bss 上的对象 = free 一个不在堆里的指针,`end()` 必 panic。
//    所以下面注册 s_serverCallbacks 时**必须显式传 false**(见 docs/pitfalls.md A13)。
//    (特征的 setCallbacks 没有这个参数,而 ~NimBLECharacteristic 也不删回调,所以
//     s_rxCallbacks / s_txCallbacks 用静态单例天然安全。)
RxCallbacks     s_rxCallbacks;
ServerCallbacks s_serverCallbacks;
TxCallbacks     s_txCallbacks;

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

// 单片 notify 能带多少字节:ATT_MTU - 3 字节的 ATT 头,再夹到 notifyChunkMax。
// 未协商到(部分中心不主动交换 MTU)时退回 BLE 规定的默认 23。
//
// ⚠️ 上限不是理论值而是实测值。取满 MTU-3(协商到 517 时是 514)连续推几片,
//    NimBLE 的 mbuf 池会被打空 —— 见 LinkConfig::notifyChunkMax 的注释。
size_t notifyChunkSize() {
    uint16_t mtu = s_mtu ? s_mtu : 23;
    size_t byMtu = mtu > 3 ? size_t(mtu - 3) : 20;
    size_t cap = s_cfg.notifyChunkMax ? s_cfg.notifyChunkMax : byMtu;
    return byMtu < cap ? byMtu : cap;
}

// 推一片。
//
// ⚠️ NimBLE-Arduino 1.4.x 的 notify() 返回 void —— **协议栈吃不下这一片时我们
//    收不到任何信号**。所以这里不能靠重试兜底,只能靠「别把它撑爆」:
//    分片保守(notifyChunkMax)+ 片间留出冲刷时间(notifyChunkDelayMs)。
//    这也是为什么 notifyChunkMax 的默认值是实测出来的 180 而不是理论上的 MTU-3。
void notifyChunk(const uint8_t* p, size_t n) {
    s_tx->setValue(p, n);
    s_tx->notify();
    s_stats.txChunks++;
}

}  // namespace

bool begin(const LinkConfig& cfg) {
    if (s_started) end();
    s_cfg = cfg;
    s_stopping = false;      // 上一轮 end() 若中途失败可能留着,别让新连接一上来就不广播

    if (s_cfg.rxRingBytes < 64) s_cfg.rxRingBytes = 64;
    s_ring = (uint8_t*)malloc(s_cfg.rxRingBytes);
    if (!s_ring) return false;
    s_ringCap  = s_cfg.rxRingBytes;
    s_ringHead = s_ringTail = 0;
    s_assembly = "";

    if (!s_rxSignal) s_rxSignal = xSemaphoreCreateBinary();
    if (!s_rxSignal) { free(s_ring); s_ring = nullptr; return false; }

    const char* svcUuid = s_cfg.serviceUuid ? s_cfg.serviceUuid : kNusService;
    const char* rxUuid  = s_cfg.rxUuid      ? s_cfg.rxUuid      : kNusRx;
    const char* txUuid  = s_cfg.txUuid      ? s_cfg.txUuid      : kNusTx;

    // 身份:显式 deviceName 优先;否则 `<type>-<id>`,id 缺省从 efuse MAC 派生
    if (s_cfg.deviceId && *s_cfg.deviceId) {
        snprintf(s_id, sizeof(s_id), "%s", s_cfg.deviceId);
    } else {
        deriveId(s_id, sizeof(s_id));
    }
    if (s_cfg.deviceName && *s_cfg.deviceName) {
        snprintf(s_name, sizeof(s_name), "%s", s_cfg.deviceName);
    } else {
        snprintf(s_name, sizeof(s_name), "%s-%s",
                 s_cfg.deviceType ? s_cfg.deviceType : "espble", s_id);
    }

    NimBLEDevice::init(s_name);
    NimBLEDevice::setPower(toPowerLevel(s_cfg.txPowerDbm));
    NimBLEDevice::setMTU(s_cfg.mtu);   // 只是「允许协商到这么大」,实际由中心定

    s_server = NimBLEDevice::createServer();
    // 第二个参数 false = 所有权留在我们这儿,别让 ~NimBLEServer 去 delete 一个静态对象。
    // 漏掉它 → end() 必 panic(pitfalls A13)。改这行前先把 A13 读完。
    s_server->setCallbacks(&s_serverCallbacks, false);

    NimBLEService* svc = s_server->createService(svcUuid);
    NimBLECharacteristic* rx = svc->createCharacteristic(
        rxUuid, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
    // 这里**没有**也**不能加**第二个参数:NimBLECharacteristic::setCallbacks 只有一个形参,
    // 而 ~NimBLECharacteristic 本来就不删回调(只删 descriptor),静态单例天然安全。
    // 别看着上面那行「对称地」给它补个 false —— 编译不过。
    rx->setCallbacks(&s_rxCallbacks);
    s_tx = svc->createCharacteristic(txUuid, NIMBLE_PROPERTY::NOTIFY);
    // 订阅回调:hello 要等中枢真的订阅上来才发(见 TxCallbacks)。
    s_tx->setCallbacks(&s_txCallbacks);
    svc->start();

    NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
    adv->addServiceUUID(svcUuid);
    adv->setScanResponse(true);
    // 唯一允许碰连接参数的地方:广播里的**建议值**。中心可以完全无视它。
    adv->setMinPreferred(s_cfg.advMinInterval);
    adv->setMaxPreferred(s_cfg.advMaxInterval);
    NimBLEDevice::startAdvertising();

    s_started = true;
    s_helloPending = false;
    return true;
}

void end() {
    if (!s_started) return;

    // 拆栈顺序是有讲究的,乱来会堆损坏 panic(docs/pitfalls.md A8):
    //   ① 先置 stopping —— 后面主动断连会触发 onDisconnect,它必须知道
    //      「这次别重新广播」。在正在拆的协议栈上 startAdvertising() 会 rc=30 失败,
    //      连带 deinit 也失败,最后死在 multi_heap_free 的断言上。
    //   ② 停广播、断开对端,给协议栈时间把断连事件跑完。
    //   ③ 最后才 deinit。
    s_stopping = true;
    NimBLEDevice::stopAdvertising();
    if (s_connected && s_server) {
        s_server->disconnect(s_connHandle);
        for (int i = 0; i < 20 && s_connected; i++) delay(10);   // 至多等 200ms
    }
    delay(20);
    NimBLEDevice::deinit(true);

    s_server = nullptr;
    s_tx     = nullptr;
    s_connected = false;
    s_mtu = 0;
    s_stopping = false;
    free(s_ring); s_ring = nullptr;
    s_ringCap = 0;
    s_ringHead = s_ringTail = 0;
    s_assembly = "";
    s_started = false;
}

void quiesce() {
    if (!s_started) return;
    // 只做「安全」的两件事:停广播、断开对端。绝不碰 deinit —— 那条路在
    // NimBLE 1.4.x + core 3.x 上必 panic(见 end() 的注释)。
    s_stopping = true;          // 断连回调别去重新广播
    NimBLEDevice::stopAdvertising();
    if (s_connected && s_server) {
        s_server->disconnect(s_connHandle);
        for (int i = 0; i < 20 && s_connected; i++) delay(10);
    }
    s_ringHead = s_ringTail = 0;
    s_assembly = "";
}

void resume() {
    if (!s_started) return;
    s_stopping = false;
    NimBLEDevice::startAdvertising();
}

bool started()   { return s_started; }
const char* deviceId()   { return s_id; }
const char* deviceName() { return s_name; }
bool connected() { return s_connected; }
uint16_t peerMtu() { return s_connected ? s_mtu : 0; }

void onConnectionChange(ConnectionCallback cb) { s_connCb = cb; }

// 可见性看门狗:确保设备**要么真连着,要么真在广播**,不会卡在"两头都不是"。
//
// ★ 为什么必须有这个:BLE-only 的设备一旦既没连上又没广播,就**彻底失联** ——
//   没有 WiFi 兜底,没有远程入口,只能接 USB。而下面这两种情况都真实发生过:
//
//   ① 对端进程被杀 / 主机崩了,断连事件没送到设备 → s_connected 还是 true,
//      于是不广播、也没人来,永远沉默。实测:杀掉 worker 后设备就此消失,
//      bluetoothd 扫得到别的设备但扫不到它,最后只能用 esptool 硬复位救回来。
//   ② 广播因为某次 rc 失败没起来(比如拆栈途中那条 rc=30,见 A8),而我们不知道。
//
//   老的双模固件靠"每 5 分钟回试一次 BLE"(重跑 begin())意外地兜住了这两种情况;
//   改成 BLE-only 之后那个安全网没了,这个函数就是把它补回来。
//
// 判据是**问协议栈**而不是靠超时猜:getConnectedCount() 和 isAdvertising() 都是
// 精确状态,所以不会误伤"连着但很久没说话"的正常空闲连接。
void checkVisibility() {
    if (!s_started || s_stopping || !s_server) return;

    // ⚠️ 节流。popMessage() 是被应用**在 while 循环里**反复调的
    // (`while (popMessage(m)) handle(m);`),而下面这两个调用会进 NimBLE 拿锁、
    // 遍历 peer 表。不节流就是在主循环里对协议栈做高频轮询,反而可能拖累链路 ——
    // 这个看门狗是"救失联"的,1 秒一次绰绰有余。
    uint32_t now = millis();
    if (s_lastVisCheck && (uint32_t)(now - s_lastVisCheck) < 1000) return;
    s_lastVisCheck = now;

    size_t live = s_server->getConnectedCount();

    // ① 我们以为连着,协议栈说没有 → 断连回调丢了。自己收拾:清状态 + 重开广播。
    if (s_connected && live == 0) {
        s_connected = false;
        s_mtu = 0;
        s_helloPending = false;
        s_ringHead = s_ringTail = 0;
        s_assembly = "";
        s_stats.staleDrops++;
        if (s_connCb) s_connCb(false);
    }

    // ② 没连着就该在广播。否则谁都找不到我们。
    if (!s_connected) {
        NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
        if (adv && !adv->isAdvertising()) {
            adv->start();
            s_stats.advRestarts++;
        }
    }
}

bool popMessage(String& out) {
    if (!s_started) return false;

    // 搭主循环的车把两件事办掉:可见性看门狗 + hello。主循环一定会调 popMessage,
    // 所以不需要额外的钩子;也保证了拼串与分片发生在主循环而非协议栈任务里。
    checkVisibility();

    if (s_helloPending && s_connected) {
        s_helloPending = false;
        String hello = String("{\"t\":\"hello\",\"id\":\"") + s_id +
                       "\",\"type\":\"" + (s_cfg.deviceType ? s_cfg.deviceType : "espble") +
                       "\",\"name\":\"" + s_name +
                       "\",\"fw\":" + String(s_cfg.fwVersion) +
                       ",\"mtu\":" + String(s_mtu) +
                       ",\"caps\":\"" + (s_cfg.caps ? s_cfg.caps : "") + "\"}";
        notify(hello);
    }
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
        bool tailFits = (sent + n == len) && needDelimiter && (n < chunk)
                        && (n + 1 <= sizeof(s_txScratch));
        if (tailFits) {
            memcpy(s_txScratch, data + sent, n);
            s_txScratch[n] = (uint8_t)s_cfg.delimiter;
            notifyChunk(s_txScratch, n + 1);
            s_stats.txFrames++;
            return true;
        }
        notifyChunk(data + sent, n);
        sent += n;
        // 片间让出 CPU,给协议栈把这一片冲出去。省掉它就是长消息变残帧的原因。
        if (sent < len) delay(s_cfg.notifyChunkDelayMs);
    }

    if (needDelimiter) {
        uint8_t d = (uint8_t)s_cfg.delimiter;
        notifyChunk(&d, 1);
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
