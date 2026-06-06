#!/bin/bash
# package_shadow.sh — 给已构建的 Shadow.app 签名并生成 DMG / 安装
#
# 背景：adhoc 签名下，系统音频 helper(audio_capture) 拿不到屏幕录制授权传导，
# 且每次重打包/重启都要重授。用自签名证书 "Shadow Self Signed" 统一签
# app + 三个 helper，授权才能传导到 helper 并跨重启/重打包记住。
# 详见 memory/rerecord_permission_fix.md。
#
# 用法:
#   ./package_shadow.sh sign   <Shadow.app路径>     只签名
#   ./package_shadow.sh dmg    <Shadow.app路径>     签名 + 生成 dist/Shadow.dmg
#   ./package_shadow.sh install <Shadow.app路径>    签名 + 覆盖安装到 /Applications + 去隔离
#
# 前置：证书已在登录钥匙串且受信任。若 `security find-identity -v -p codesigning`
# 看不到 "Shadow Self Signed"，先重建证书（见 memory/code_signing_cert.md）。
set -e

CERT="Shadow Self Signed"
SRC_DIR="/Users/yangxiaohui/Desktop/Claude Shadow"
ACTION="${1:-sign}"
APP="${2:-/Applications/Shadow.app}"

if ! security find-identity -v -p codesigning | grep -q "$CERT"; then
  echo "✗ 找不到签名身份 \"$CERT\"，先重建证书（见 memory/code_signing_cert.md）"; exit 1
fi
[ -d "$APP" ] || { echo "✗ 找不到 app: $APP"; exit 1; }

echo "=== 签名 helper（由内到外）==="
for b in audio_capture subtitle_recognizer ocr_frame; do
  codesign --force --timestamp=none --sign "$CERT" "$APP/Contents/Frameworks/Shadow/$b"
  echo "   ✓ $b"
done
echo "=== --deep 签名整个 app ==="
codesign --force --deep --timestamp=none --sign "$CERT" "$APP"
codesign --verify --deep --strict "$APP" && echo "✓ 签名校验通过"
codesign -dv "$APP" 2>&1 | grep -E "Authority|Signature"

if [ "$ACTION" = "dmg" ]; then
  echo "=== 生成签名版 DMG（覆盖 dist/Shadow.dmg）==="
  STAGE=/tmp/shadow_dmg_stage; rm -rf "$STAGE"; mkdir -p "$STAGE"
  cp -R "$APP" "$STAGE/Shadow.app"; ln -s /Applications "$STAGE/Applications"
  rm -f "$SRC_DIR/dist/Shadow.dmg"
  hdiutil create -volname "Shadow" -srcfolder "$STAGE" -ov -format UDZO "$SRC_DIR/dist/Shadow.dmg"
  rm -rf "$STAGE"
  hdiutil verify "$SRC_DIR/dist/Shadow.dmg" && echo "✓ DMG 生成并校验通过: $SRC_DIR/dist/Shadow.dmg"
elif [ "$ACTION" = "install" ]; then
  echo "=== 覆盖安装到 /Applications ==="
  pkill -9 -f "Shadow.app/Contents/MacOS/Shadow" 2>/dev/null || true
  pkill -9 -f audio_capture 2>/dev/null || true; sleep 1
  if [ "$APP" != "/Applications/Shadow.app" ]; then
    rm -rf /Applications/Shadow.app
    cp -R "$APP" /Applications/Shadow.app
  fi
  xattr -dr com.apple.quarantine /Applications/Shadow.app 2>/dev/null || true
  echo "✓ 已安装并去隔离。注意：换签名后首次需重新授权屏幕录制一次（之后跨重启记住）"
fi
echo "=== 完成 ==="
