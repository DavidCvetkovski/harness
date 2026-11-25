#!/usr/bin/env bash
# Build, optionally bundle the backend + llama-server, sign, notarize, and
# package Harness.app into a drag-to-Applications .dmg for Apple Silicon Macs.
#
# Usage:
#   macos/scripts/package_dmg.sh                       # unsigned local build + DMG
#   macos/scripts/package_dmg.sh --bundle-backend      # also embed harness/ + llama-server
#   DEVELOPER_ID="Developer ID Application: ..." \
#   NOTARY_PROFILE="harness-notary" \
#     macos/scripts/package_dmg.sh --bundle-backend    # signed + notarized release DMG
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MACOS_DIR="$ROOT/macos"
DIST_DIR="$ROOT/dist"
DERIVED="/tmp/harness-mac-release"
BUNDLE_BACKEND=false
VERSION="${VERSION:-1.0.0}"

for arg in "$@"; do
  case "$arg" in
    --bundle-backend) BUNDLE_BACKEND=true ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

echo "==> Generating Xcode project"
(cd "$MACOS_DIR" && xcodegen generate)

echo "==> Building Release Harness.app (arm64)"
xcodebuild build \
  -project "$MACOS_DIR/HarnessMac.xcodeproj" \
  -scheme HarnessMac \
  -configuration Release \
  -arch arm64 \
  -derivedDataPath "$DERIVED" \
  CODE_SIGN_IDENTITY="-" \
  CODE_SIGNING_ALLOWED=YES \
  | grep -E "error:|warning:|BUILD SUCCEEDED|BUILD FAILED" || true

APP_SRC="$DERIVED/Build/Products/Release/Harness.app"
test -d "$APP_SRC"

mkdir -p "$DIST_DIR"
STAGE="$(mktemp -d /tmp/harness-dmg-stage.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
APP_DST="$STAGE/Harness.app"
cp -R "$APP_SRC" "$APP_DST"

if [ "$BUNDLE_BACKEND" = true ]; then
  echo "==> Bundling Harness Python backend into Contents/Resources/backend/"
  BACKEND="$APP_DST/Contents/Resources/backend"
  mkdir -p "$BACKEND/bin"
  rsync -a --exclude='__pycache__' --exclude='*.pyc' "$ROOT/harness/" "$BACKEND/harness/"
  LLAMA_BIN="${HARNESS_LLAMA_SERVER:-$HOME/llm/llama.cpp/build/bin/llama-server}"
  if [ -x "$LLAMA_BIN" ]; then
    cp "$LLAMA_BIN" "$BACKEND/bin/llama-server"
    # Copy Metal shader / dynamic libraries if built as shared libs alongside llama-server
    LLAMA_DIR="$(dirname "$LLAMA_BIN")"
    for lib in "$LLAMA_DIR"/*.dylib "$LLAMA_DIR"/*.metallib; do
      [ -e "$lib" ] && cp "$lib" "$BACKEND/bin/"
    done
    echo "    Bundled llama-server from $LLAMA_BIN"
  else
    echo "    Note: llama-server not found at $LLAMA_BIN (skipped)"
  fi
fi

if [ -n "${DEVELOPER_ID:-}" ]; then
  echo "==> Signing Harness.app with Developer ID ($DEVELOPER_ID)"
  # Sign any nested binaries/dylibs first, then the outer .app with hardened runtime.
  if [ -d "$APP_DST/Contents/Resources/backend/bin" ]; then
    find "$APP_DST/Contents/Resources/backend/bin" -type f -perm +111 -print0 \
      | xargs -0 -I{} codesign --force --options runtime --timestamp --sign "$DEVELOPER_ID" "{}"
  fi
  codesign --force --deep --options runtime --timestamp \
    --entitlements "$MACOS_DIR/HarnessMac/HarnessMac.entitlements" \
    --sign "$DEVELOPER_ID" "$APP_DST"
  codesign --verify --deep --strict "$APP_DST"
fi

ln -s /Applications "$STAGE/Applications"
DMG_PATH="$DIST_DIR/Harness-${VERSION}-arm64.dmg"
rm -f "$DMG_PATH"

echo "==> Creating $DMG_PATH"
hdiutil create \
  -volname "Harness" \
  -srcfolder "$STAGE" \
  -ov -format UDZO \
  "$DMG_PATH" >/dev/null

if [ -n "${DEVELOPER_ID:-}" ]; then
  codesign --force --timestamp --sign "$DEVELOPER_ID" "$DMG_PATH"
fi

if [ -n "${NOTARY_PROFILE:-}" ]; then
  echo "==> Submitting to Apple notarization ($NOTARY_PROFILE)"
  xcrun notarytool submit "$DMG_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG_PATH"
fi

echo "==> Done: $DMG_PATH"
