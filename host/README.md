# espble

macOS BLE central for ESP32 NUS devices — the host half of
[esp-ble-link](https://github.com/konder/esp-ble-link).

A compiled Swift `.app` does the CoreBluetooth work (Python can never hold a
stable Bluetooth TCC grant), driven by Python over a file mailbox, with
process-level recovery and MQTT-style retained/replay semantics on top.

```bash
pip install "git+https://github.com/konder/esp-ble-link.git#subdirectory=host"
espble build-helper --name MyBLEHelper --bundle-id com.you.mydevice.blehelper
espble watch --app .build/native/MyBLEHelper.app --device-name MyDevice
```

See the [main README](https://github.com/konder/esp-ble-link) and
`docs/pitfalls.md` for the reasoning behind the design.

macOS 13+ · Python 3.9+ · no runtime dependencies.
