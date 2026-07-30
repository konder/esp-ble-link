# esp-ble-link

A reusable BLE link layer for ESP32 devices, paired with a macOS central that
actually stays connected.

> Most documentation is currently in Chinese. English translation is in progress —
> [docs/pitfalls.md](docs/pitfalls.md) is the one worth translating first.

## What this is for

The shape of project this serves: a small ESP32 device, an always-on Mac, JSON
over BLE between them. Status panels, desk pets, remotes, agent notifiers.

The hard part was never getting them to connect. It's keeping them connected.
This framework encodes the link-layer discipline on the ESP32 side, the
CoreBluetooth permission and process-recovery model on the macOS side, and the
retained/replay semantics that BLE lacks and MQTT gave you for free.

Every design decision here traces back to a specific failure. Those are written
down in [docs/pitfalls.md](docs/pitfalls.md), organised by symptom — that file
is worth more than the code.

## Firmware side (PlatformIO library, NimBLE-Arduino)

- Nordic UART Service peripheral, delimiter framing, automatic chunking
- BLE callbacks only memcpy into a ring buffer — framing and parsing happen on
  the main loop, so the host task never starves
- **Never requests connection parameters.** This is the single most important
  rule; see pitfalls A1 for the three firmware generations that learned it
- Clears buffers and re-advertises on disconnect
- Overflow and oversize-frame counters, so you can tell "never arrived" from
  "arrived but lost bytes"
- WiFi-mode HTTP OTA that releases the 2.4 GHz radio first

## Host side (pip package, macOS only)

- A compiled Swift `.app` acts as the CoreBluetooth central. Python + bleak can
  never hold a stable Bluetooth TCC grant — macOS grants it to the bundle
  executable that touches CoreBluetooth, and `exec` replaces the image
- One generic source file; bundle id injected at build time, UUIDs and device
  name passed at runtime
- Any failure discards the whole helper process. CoreBluetooth's bad state is
  process-level; in-process retries only accumulate it
- Strictly ACK-driven chunked writes, one chunk in flight at a time
- Keepalive is a real ATT write — a `ping` to the helper proves nothing about
  the BLE link
- `RetainedChannel` restores MQTT's retained value and offline replay semantics

## Quick start

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

```bash
pip install "git+https://github.com/konder/esp-ble-link.git#subdirectory=host"
espble build-helper --name MyBLEHelper --bundle-id com.you.mydevice.blehelper
espble watch --app .build/native/MyBLEHelper.app --device-name MyDevice
```

## Requirements

- Firmware: ESP32 family, PlatformIO + Arduino framework, NimBLE-Arduino **1.4.x**
  (2.x changed callback signatures; not yet supported)
- Host: **macOS 13+**. The host half is deliberately macOS-specific — working
  around CoreBluetooth is the whole point. The firmware half is platform-neutral.
- Python 3.9+, no runtime dependencies

## Status

Early. Protocol and API may still change.

## License

MIT
