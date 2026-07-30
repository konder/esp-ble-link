#!/bin/zsh
# 端到端验一遍框架:构建 helper → 连设备 → 发一条 → 看回显。
#
# 前置:
#   1. 把 echo 固件烧进一块 ESP32-S3(pio run -e esp32s3 -t upload)
#   2. 装上 host 包(pip install -e ../../host)
#   3. **在 GUI 会话里跑**。从 SSH 跑的话 helper 拿不到 bluetoothd,
#      现象是卡在 central_created —— 那不是 bug,见 docs/pitfalls.md。
set -euo pipefail

DEVICE_NAME="${DEVICE_NAME:-EspBleEcho}"
HELPER_NAME="${HELPER_NAME:-EchoBLEHelper}"
BUNDLE_ID="${BUNDLE_ID:-com.example.espble.echo.blehelper}"
APP=".build/native/$HELPER_NAME.app"

if [[ ! -x "$APP/Contents/MacOS/$HELPER_NAME" ]]; then
  echo "==> 构建 helper($HELPER_NAME)"
  echo "    ⚠️ BUNDLE_ID 现在是示例值 $BUNDLE_ID —— 你自己的项目请换一个唯一的。"
  espble build-helper --name "$HELPER_NAME" --bundle-id "$BUNDLE_ID" \
                      --usage-desc "esp-ble-link 回环示例通过蓝牙与 ESP 设备通信。"
fi

echo "==> 发一条短消息,等回显"
espble send --app "$APP" --device-name "$DEVICE_NAME" --wait 4 '{"hi":1}'

echo
echo "==> 发一条超长中文,验分片与长度约束"
LONG=$(python3 -c 'import json;print(json.dumps({"t":"probe","msg":"中"*900},ensure_ascii=False))')
espble send --app "$APP" --device-name "$DEVICE_NAME" --wait 4 "$LONG"

echo
echo "==> 盯 35s,应该能看到一条 stats 遥测(设备每 30s 发一次)"
espble watch --app "$APP" --device-name "$DEVICE_NAME" --seconds 35

echo
echo "全部完成。预期观察到:"
echo "  - 两条 {\"t\":\"echo\",…} 回显,第二条的 len 被截到 ~1800 字节以内"
echo "  - 一条 {\"t\":\"stats\",…},其中 rx_dropped=0 且 rx_oversize=0"
