#!/bin/zsh
# 从通用的 BleHelper.swift 构建一个专属的 BLE 中枢 helper.app。
#
# 为什么每个设备都要单独构建一个 bundle:
#   macOS 的蓝牙 TCC 授权是按 bundle 走的,而 LaunchServices 又按 bundle id 认 app。
#   两个 helper 共用 id → 被当成同一个 app → 启动一个另一个就再也起不来。
#   所以源码共用一份,bundle id 和可执行文件名在**构建期**注入。
#
# 用法:
#   build_helper.sh --name EchoBLEHelper \
#                   --bundle-id com.example.echo.blehelper \
#                   [--usage-desc "…"] [--out DIR] [--version 1.0]
#
# 通常不直接调,而是走 `espble build-helper`(它会把源码路径填好)。
#
# ⚠️ 这里用 `codesign --sign -` 做 adhoc 自签。装了 EDR 类安全软件的机器
#    (例如公司配发的电脑)可能会直接 SIGKILL adhoc 自签的二进制 —— 那种机器上
#    构建能过、一运行就没,别在那儿浪费时间。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/BleHelper.swift"
TMPL="$HERE/Info.plist.tmpl"

NAME=""
BUNDLE_ID=""
USAGE_DESC=""
OUT_DIR="$PWD/.build/native"
VERSION="1.0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)       NAME="$2";       shift 2 ;;
    --bundle-id)  BUNDLE_ID="$2";  shift 2 ;;
    --usage-desc) USAGE_DESC="$2"; shift 2 ;;
    --out)        OUT_DIR="$2";    shift 2 ;;
    --version)    VERSION="$2";    shift 2 ;;
    --src)        SRC="$2";        shift 2 ;;
    --template)   TMPL="$2";       shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$NAME" ]]      || { echo "缺 --name" >&2; exit 2; }
[[ -n "$BUNDLE_ID" ]] || { echo "缺 --bundle-id" >&2; exit 2; }
[[ -f "$SRC" ]]       || { echo "找不到源码: $SRC" >&2; exit 2; }
[[ -f "$TMPL" ]]      || { echo "找不到模板: $TMPL" >&2; exit 2; }
[[ -z "$USAGE_DESC" ]] && USAGE_DESC="$NAME 通过蓝牙低功耗与 ESP 设备通信。"

# 可执行文件名会进 ps 输出,Python 侧靠它 + session-dir 定位进程。
# 带空格会把 ps 解析搞乱,直接挡掉。
if [[ "$NAME" == *" "* ]]; then
  echo "--name 不能含空格(它要当可执行文件名,且 Python 侧靠它匹配进程)" >&2
  exit 2
fi

# 已知的上游 bundle id,撞上必然出问题。
for taken in com.charlex.codebuddy.blehelper; do
  if [[ "$BUNDLE_ID" == "$taken" ]]; then
    echo "bundle id '$BUNDLE_ID' 是已知的上游 id,换一个" >&2
    exit 2
  fi
done

APP="$OUT_DIR/$NAME.app"
BIN="$APP/Contents/MacOS/$NAME"
REGISTRY="${ESPBLE_REGISTRY:-$HOME/.config/espble/helpers.json}"

# 撞车检查。两道,因为单靠哪一道都不够:
#   ① 本地登记表 —— 确定性的,记着这台机器上每个 helper 建在哪。
#      这道拦得住最现实的情况:照着另一个项目抄配置时顺手把 bundle id 也抄了。
#   ② mdfind —— 能发现登记表以外的 app(比如别人装的),但只看得到 Spotlight
#      索引过的路径(/tmp 之类就查不到),所以只能当补充,不能当唯一防线。
BUNDLE_ID="$BUNDLE_ID" APP="$APP" REGISTRY="$REGISTRY" python3 - <<'PY' || exit 1
import json, os, sys
bundle_id, app, registry = os.environ["BUNDLE_ID"], os.environ["APP"], os.environ["REGISTRY"]
app = os.path.realpath(app)
try:
    with open(registry, encoding="utf-8") as fh:
        known = json.load(fh)
except (OSError, ValueError):
    known = {}
prev = known.get(bundle_id)
if prev and os.path.realpath(prev) != app:
    sys.stderr.write(
        f"bundle id '{bundle_id}' 本机已被另一个 helper 占用:\n"
        f"     {prev}\n"
        "     两个 app 共用 id 会被 LaunchServices 当成同一个 —— 启动一个,\n"
        "     另一个就再也起不来。换个 id,或删掉那个旧 helper 后编辑\n"
        f"     {registry}\n")
    sys.exit(1)
PY

if command -v mdfind >/dev/null 2>&1; then
  CLASH="$(mdfind "kMDItemCFBundleIdentifier == '$BUNDLE_ID'" 2>/dev/null \
           | grep -vx "$APP" || true)"
  if [[ -n "$CLASH" ]]; then
    echo "bundle id '$BUNDLE_ID' 已被系统里的其它 app 占用:" >&2
    echo "$CLASH" | sed 's/^/     /' >&2
    echo "     两个 app 共用 id 会被 LaunchServices 当成同一个,互相顶掉。换个 id。" >&2
    exit 1
  fi
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

# 渲染 Info.plist。用 python3 做替换,免得 sed 遇到中文/斜杠/& 出幺蛾子。
NAME="$NAME" BUNDLE_ID="$BUNDLE_ID" USAGE_DESC="$USAGE_DESC" VERSION="$VERSION" \
python3 - "$TMPL" "$APP/Contents/Info.plist" <<'PY'
import os, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
for key, env in (("HELPER_NAME", "NAME"), ("BUNDLE_ID", "BUNDLE_ID"),
                 ("USAGE_DESC", "USAGE_DESC"), ("VERSION", "VERSION")):
    value = os.environ[env].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("@@%s@@" % key, value)
open(dst, "w", encoding="utf-8").write(text)
PY

# -parse-as-library 是 @main 必需的
swiftc -parse-as-library -O -framework AppKit -framework CoreBluetooth "$SRC" -o "$BIN"

xattr -cr "$APP"
codesign --force --sign - "$APP"

# 建成了才登记 —— 编译失败的不该占住一个 id。
BUNDLE_ID="$BUNDLE_ID" APP="$APP" REGISTRY="$REGISTRY" python3 - <<'PY'
import json, os
bundle_id, app, registry = os.environ["BUNDLE_ID"], os.environ["APP"], os.environ["REGISTRY"]
try:
    with open(registry, encoding="utf-8") as fh:
        known = json.load(fh)
except (OSError, ValueError):
    known = {}
known[bundle_id] = os.path.realpath(app)
os.makedirs(os.path.dirname(registry), exist_ok=True)
tmp = registry + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(known, fh, ensure_ascii=False, indent=2, sort_keys=True)
os.replace(tmp, registry)
PY

echo "built: $APP"
echo "bundle id: $(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist")"
echo
echo "首次运行会弹蓝牙授权框(或需去 系统设置 > 隐私与安全性 > 蓝牙 手动允许)。"
echo "⚠️ 授权框只在 GUI(Aqua)会话里弹得出来。从 SSH 会话 open 出来的 helper"
echo "   拿不到 bluetoothd,现象是事件流停在 central_created 再无下文 —— 那不是"
echo "   签名或 TCC 的问题,是会话上下文不对。详见 docs/pitfalls.md。"
