// EspBleOta —— WiFi 模式的 HTTP OTA,给 BLE 设备用的「临时开一次 WiFi 升级完再回来」。
//
// 为什么不走 BLE 传固件字节:BLE 的吞吐做几百 KB 的镜像太慢,而且要自己做分片、
// 校验、断点续传、回滚。设备既然本来就在家里的 WiFi 覆盖下,临时切一次 WiFi 走
// HTTP 是最省事也最可靠的做法。
//
// ⚠️ ESP32 的 BLE 和 WiFi 共用同一颗 2.4G 射频。两个同时开会互相拖垮
//    (表现是 OTA 龟速 + BLE 掉线)。所以 otaOverWifi() 的第一件事就是
//    espble::end() 彻底释放 BLE,升级失败再 begin() 回去。
//
// 典型用法(收到中枢下发的 ota 指令时):
//
//     espble::OtaConfig ota;
//     ota.host = "10.0.0.9";
//     ota.currentVersion = FW_VERSION;
//     ota.seedSsid = WIFI_SSID;          // 只在 NVS 为空时用来播种
//     ota.seedPassword = WIFI_PASSWORD;
//     ota.onStage = [](const char* stage, int pct) { showOnScreen(stage, pct); };
//     espble::otaOverWifi(ota);          // 成功不返回(设备重启)
//
// 服务端最简实现见 host/ 的 `espble ota serve`,只要提供两个 URL:
//     GET {basePath}/version      → 纯文本整数,如 "40"
//     GET {basePath}/current.bin  → 固件镜像
#pragma once

#include <Arduino.h>
#include <stdint.h>

namespace espble {

enum class OtaResult {
    NoUpdate,        // 远端版本 <= 本地,什么也没做
    WifiFailed,      // 连不上 WiFi
    CheckFailed,     // /version 拿不到或不是数字
    UpdateFailed,    // 镜像下载/写入失败(设备仍在跑旧固件)
    Updating,        // 已开始写入;正常情况下会自动重启,不会返回这个值
};

struct OtaConfig {
    // 固件服务器。注意这台机器不一定是 BLE 中枢那台。
    const char* host = nullptr;
    uint16_t    port = 8899;
    const char* basePath = "/fw";

    // 本地固件版本号。远端 > 本地才升级。
    int currentVersion = 0;

    // WiFi 凭据**只在 NVS 为空时**用来播种。之后改 WiFi 走 wifiSaveNvs(),
    // 不必为了换个 WiFi 重烧固件 —— 这正是把凭据放 NVS 的意义。
    const char* seedSsid     = nullptr;
    const char* seedPassword = nullptr;

    uint32_t wifiTimeoutMs = 15000;
    uint32_t httpTimeoutMs = 10000;

    // 进度回调。stage ∈ {"wifi","check","download","reboot","failed"};
    // pct 仅 download 阶段有意义,其余为 -1。用来在屏幕上显示进度,
    // 库本身不依赖任何显示驱动。
    void (*onStage)(const char* stage, int pct) = nullptr;
};

// 覆盖写 NVS 里的 WiFi 凭据(供以后无线改 WiFi)。
void wifiSaveNvs(const char* ssid, const char* password);

// 读 NVS 连 WiFi;NVS 为空时用 seed 值播种再连。
//
// 深睡设备的优化:成功后把 BSSID + 信道存进 RTC 内存,下次唤醒直接定向快连,
// 跳过全信道扫描(能省几秒的射频开机时间,对靠电池的设备是实打实的电量)。
bool wifiConnectNvs(const char* seedSsid, const char* seedPassword, uint32_t timeoutMs);

// 假设 WiFi 已连好,检查版本并升级。不碰 BLE,也不管 WiFi 的生命周期。
OtaResult checkAndUpdate(const OtaConfig& cfg);

// 完整序列:end() 释放射频 → 连 WiFi → 检查并升级 → 失败时 begin() 回 BLE。
// 成功时设备会重启,本函数不返回。
OtaResult otaOverWifi(const OtaConfig& cfg);

}  // namespace espble
