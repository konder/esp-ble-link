// esp-ble-link 回环示例。
//
// 行为:
//   - 广播为 ECHO_DEVICE_NAME
//   - 收到任意一行 → 原样包成 {"t":"echo","n":<序号>,"payload":"…"} notify 回去
//   - 收到含 "\"cmd\":\"ota\"" 的行 → 切 WiFi 走 HTTP OTA
//   - 每 30s notify 一次链路统计,便于观察丢包/溢出
//
// 刻意不引 ArduinoJson:这个示例要证明的是**链路层**能跑通,少一个依赖就少一个
// 环境变量。真实项目里该用 JSON 库解析,这里的字符串匹配只是够用而已。
#include <Arduino.h>
#include <EspBleLink.h>
#include <EspBleOta.h>

#ifndef FW_VERSION
#define FW_VERSION 1
#endif
#ifndef ECHO_DEVICE_NAME
#define ECHO_DEVICE_NAME "EspBleEcho"
#endif
#ifndef OTA_HOST
#define OTA_HOST ""
#endif
#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""
#endif

static uint32_t g_seq = 0;
static uint32_t g_lastStatsMs = 0;

// ⚠️ 这个回调跑在 NimBLE 主机任务里,只能打日志/置标志位(见 EspBleLink.h 纪律 1)。
static void onConnChange(bool up) {
    Serial.printf("[echo] link %s\n", up ? "up" : "down");
}

// JSON 字符串转义。回显要把收到的原文塞进 JSON,不转义遇到引号就会造出烂 JSON。
static String jsonEscape(const String& s) {
    String out;
    out.reserve(s.length() + 8);
    for (size_t i = 0; i < s.length(); i++) {
        char c = s[i];
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if ((uint8_t)c < 0x20) {
                    char buf[7];
                    snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += c;
                }
        }
    }
    return out;
}

static void runOta() {
    if (!strlen(OTA_HOST)) {
        espble::notify("{\"t\":\"ota\",\"result\":\"skipped\",\"why\":\"OTA_HOST 未配置\"}");
        return;
    }
    Serial.println("[echo] 收到 ota 指令,切 WiFi");
    espble::OtaConfig cfg;
    cfg.host           = OTA_HOST;
    cfg.currentVersion = FW_VERSION;
    cfg.seedSsid       = WIFI_SSID;
    cfg.seedPassword   = WIFI_PASSWORD;
    cfg.onStage = [](const char* stage, int pct) {
        if (pct >= 0) Serial.printf("[ota] %s %d%%\n", stage, pct);
        else          Serial.printf("[ota] %s\n", stage);
    };

    // 成功的话设备直接重启,下面这些不会执行。
    espble::OtaResult r = espble::otaOverWifi(cfg);

    // otaOverWifi 内部失败时已经把 BLE 拉回来了,但用的是**默认** LinkConfig。
    // 我们有自定义配置(设备名),所以重新 begin 一次自己的。
    espble::LinkConfig link;
    link.deviceName = ECHO_DEVICE_NAME;
    espble::begin(link);

    const char* why = "unknown";
    switch (r) {
        case espble::OtaResult::NoUpdate:     why = "already-latest"; break;
        case espble::OtaResult::WifiFailed:   why = "wifi-failed";    break;
        case espble::OtaResult::CheckFailed:  why = "check-failed";   break;
        case espble::OtaResult::UpdateFailed: why = "update-failed";  break;
        case espble::OtaResult::Updating:     why = "updating";       break;
    }
    Serial.printf("[echo] ota 结束: %s\n", why);
}

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.printf("\n[echo] esp-ble-link 回环示例 v%d\n", FW_VERSION);

    espble::LinkConfig cfg;
    cfg.deviceName = ECHO_DEVICE_NAME;
    espble::onConnectionChange(onConnChange);
    if (!espble::begin(cfg)) {
        Serial.println("[echo] BLE 初始化失败(内存不够?)");
        while (true) delay(1000);
    }
    Serial.printf("[echo] 广播中: %s\n", ECHO_DEVICE_NAME);
}

void loop() {
    // 排空到没有为止 —— popMessage 一次只交一条。
    String line;
    while (espble::popMessage(line)) {
        Serial.printf("[echo] rx(%u): %s\n", (unsigned)line.length(), line.c_str());

        if (line.indexOf("\"cmd\":\"ota\"") >= 0) {
            runOta();
            continue;
        }

        String reply = "{\"t\":\"echo\",\"n\":" + String(++g_seq) +
                       ",\"len\":" + String(line.length()) +
                       ",\"payload\":\"" + jsonEscape(line) + "\"}";
        espble::notify(reply);
    }

    uint32_t now = millis();
    if (now - g_lastStatsMs >= 30000) {
        g_lastStatsMs = now;
        espble::LinkStats s = espble::stats();
        String t = "{\"t\":\"stats\",\"rx_bytes\":" + String(s.rxBytes) +
                   ",\"rx_dropped\":" + String(s.rxDroppedBytes) +
                   ",\"rx_frames\":" + String(s.rxFrames) +
                   ",\"rx_oversize\":" + String(s.rxOversize) +
                   ",\"tx_frames\":" + String(s.txFrames) +
                   ",\"mtu\":" + String(espble::peerMtu()) +
                   ",\"uptime\":" + String(now / 1000) + "}";
        espble::notify(t);
    }

    delay(20);
}
