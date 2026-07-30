#include "EspBleOta.h"
#include "EspBleLink.h"

#include <HTTPClient.h>
#include <HTTPUpdate.h>
#include <Preferences.h>
#include <WiFi.h>

namespace espble {

namespace {

const char* kNvsNamespace = "espble_wifi";

// 跨深睡保留:上次成功连上的 AP 的 BSSID + 信道。有了这两个就能定向快连,
// 省掉全信道扫描。RTC 内存在深睡期间不掉电,普通复位后会清零(那就退回扫描)。
RTC_DATA_ATTR uint8_t s_bssid[6];
RTC_DATA_ATTR int     s_channel   = 0;
RTC_DATA_ATTR bool    s_hasBssid  = false;

void stage(const OtaConfig& cfg, const char* name, int pct) {
    if (cfg.onStage) cfg.onStage(name, pct);
}

// httpUpdate 的进度回调是全局注册的,而我们的进度回调挂在 cfg 上。
// 用一个文件级指针搭桥,update() 返回后立刻清掉,避免悬垂。
const OtaConfig* s_progressCfg = nullptr;

String baseUrl(const OtaConfig& cfg) {
    return String("http://") + cfg.host + ":" + String(cfg.port) + cfg.basePath;
}

}  // namespace

void wifiSaveNvs(const char* ssid, const char* password) {
    Preferences prefs;
    prefs.begin(kNvsNamespace, false);
    prefs.putString("ssid", ssid ? ssid : "");
    prefs.putString("pass", password ? password : "");
    prefs.end();
}

bool wifiConnectNvs(const char* seedSsid, const char* seedPassword, uint32_t timeoutMs) {
    Preferences prefs;
    prefs.begin(kNvsNamespace, true);
    String ssid = prefs.getString("ssid", "");
    String pass = prefs.getString("pass", "");
    prefs.end();

    // NVS 空 → 用编译进来的默认值播种(只有第一次)。以后换 WiFi 调 wifiSaveNvs 即可,
    // 不用重烧固件。
    if (ssid.isEmpty()) {
        if (!seedSsid || !*seedSsid) return false;
        ssid = seedSsid;
        pass = seedPassword ? seedPassword : "";
        wifiSaveNvs(ssid.c_str(), pass.c_str());
    }

    WiFi.mode(WIFI_STA);
    uint32_t t0 = millis();

    // 有缓存就定向快连(指定信道 + BSSID,跳过扫描)。给它 4s,不成再走常规路径。
    if (s_hasBssid) {
        WiFi.begin(ssid.c_str(), pass.c_str(), s_channel, s_bssid);
        while (WiFi.status() != WL_CONNECTED && millis() - t0 < 4000) delay(100);
    }
    if (WiFi.status() != WL_CONNECTED) {
        WiFi.disconnect();
        WiFi.begin(ssid.c_str(), pass.c_str());
        while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs) delay(150);
    }

    if (WiFi.status() != WL_CONNECTED) return false;

    memcpy(s_bssid, WiFi.BSSID(), 6);
    s_channel  = WiFi.channel();
    s_hasBssid = true;
    return true;
}

OtaResult checkAndUpdate(const OtaConfig& cfg) {
    if (!cfg.host || !*cfg.host) return OtaResult::CheckFailed;
    const String base = baseUrl(cfg);

    // 1) 问远端版本
    stage(cfg, "check", -1);
    HTTPClient http;
    http.setTimeout(cfg.httpTimeoutMs);
    if (!http.begin(base + "/version")) return OtaResult::CheckFailed;
    int code = http.GET();
    if (code != 200) {
        http.end();
        stage(cfg, "failed", -1);
        return OtaResult::CheckFailed;
    }
    String body = http.getString();
    http.end();
    body.trim();
    int remote = body.toInt();
    // toInt() 对非数字返回 0,所以显式挡一下,免得把「服务器返回了一页 HTML」
    // 误判成「远端版本 0」。
    if (body.isEmpty() || (remote == 0 && body[0] != '0')) {
        stage(cfg, "failed", -1);
        return OtaResult::CheckFailed;
    }
    if (remote <= cfg.currentVersion) return OtaResult::NoUpdate;

    // 2) 拉镜像自更新
    stage(cfg, "download", 0);
    if (cfg.onStage) {
        s_progressCfg = &cfg;
        httpUpdate.onProgress([](int done, int total) {
            if (s_progressCfg && s_progressCfg->onStage && total > 0) {
                s_progressCfg->onStage("download", (int)((int64_t)done * 100 / total));
            }
        });
    }
    WiFiClient client;
    httpUpdate.rebootOnUpdate(true);
    t_httpUpdate_return ret = httpUpdate.update(client, base + "/current.bin");
    s_progressCfg = nullptr;   // cfg 是引用参数,出了本函数就不能再碰
    if (ret == HTTP_UPDATE_FAILED) {
        stage(cfg, "failed", -1);
        return OtaResult::UpdateFailed;
    }
    if (ret == HTTP_UPDATE_NO_UPDATES) return OtaResult::NoUpdate;
    // 走到这里说明 rebootOnUpdate 没生效(极少见),交给调用方决定
    stage(cfg, "reboot", 100);
    return OtaResult::Updating;
}

OtaResult otaOverWifi(const OtaConfig& cfg) {
    const bool hadBle = started();
    // 让 BLE 安静下来(停广播 + 断连),把射频占用降下来。
    //
    // ⚠️ 这里**刻意不调 end()**。彻底 deinit 才是理论上最干净的做法,但
    //    NimBLE-Arduino 1.4.x 的 deinit 在 Arduino core 3.x(IDF 5.x)上必 panic
    //    —— 见 EspBleLink.h 里 end() 的注释与 docs/pitfalls.md A9。
    //    实测 WiFi 与 BLE 共存足以跑完 OTA:局域网上 1.3MB 的镜像几秒钟就下完。
    if (hadBle) quiesce();

    stage(cfg, "wifi", -1);
    if (!wifiConnectNvs(cfg.seedSsid, cfg.seedPassword, cfg.wifiTimeoutMs)) {
        stage(cfg, "failed", -1);
        WiFi.mode(WIFI_OFF);
        if (hadBle) resume();
        return OtaResult::WifiFailed;
    }

    OtaResult result = checkAndUpdate(cfg);

    // 只有失败/无更新才会走到这里(成功的话上面已经重启了)。
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    if (hadBle) resume();
    return result;
}

}  // namespace espble
