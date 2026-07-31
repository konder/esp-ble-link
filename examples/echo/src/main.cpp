// esp-ble-link 回环示例。
//
// 行为:
//   - 广播为 <ECHO_DEVICE_TYPE>-<efuse MAC 后3字节>,如 echo-c119cc
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
#ifndef ECHO_DEVICE_TYPE
#define ECHO_DEVICE_TYPE "echo"
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
    // 失败时 otaOverWifi 会自己 resume() 回广播 —— 配置还是原来那份,不用重新 begin。
    espble::OtaResult r = espble::otaOverWifi(cfg);

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

    // 身份不写死:广播名自动是 `<type>-<id>`,id 取自 efuse MAC 后三字节。
    // 同一份固件烧多块板子会得到不同名字,中枢按 `echo-` 前缀发现它们。
    espble::LinkConfig cfg;
    cfg.deviceType = ECHO_DEVICE_TYPE;
    cfg.fwVersion  = FW_VERSION;
    cfg.caps       = "echo,cmd";       // 随 hello 帧上报,中枢据此决定广播发不发给它
    espble::onConnectionChange(onConnChange);
    if (!espble::begin(cfg)) {
        Serial.println("[echo] BLE 初始化失败(内存不够?)");
        while (true) delay(1000);
    }
    Serial.printf("[echo] 广播中: %s  (id=%s)\n", espble::deviceName(), espble::deviceId());
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
        // fw 放第一个:OTA 之后靠它确认设备真的换了固件(串口未必读得到)
        String t = "{\"t\":\"stats\",\"fw\":" + String(FW_VERSION) +
                   ",\"rx_bytes\":" + String(s.rxBytes) +
                   ",\"rx_dropped\":" + String(s.rxDroppedBytes) +
                   ",\"rx_frames\":" + String(s.rxFrames) +
                   ",\"rx_oversize\":" + String(s.rxOversize) +
                   ",\"tx_frames\":" + String(s.txFrames) +
                   ",\"tx_chunks\":" + String(s.txChunks) +
                                      ",\"mtu\":" + String(espble::peerMtu()) +
                   ",\"uptime\":" + String(now / 1000) + "}";
        espble::notify(t);
    }

    delay(20);
}
