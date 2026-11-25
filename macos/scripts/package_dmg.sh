#!/usr/bin/env bash
# Build, optionally bundle a self-contained backend + llama-server (with all
# non-system dylibs relocated to @rpath/@loader_path), sign, notarize, and
# package Harness.app into a drag-to-Applications .dmg for Apple Silicon Macs.
#
# Usage:
#   macos/scripts/package_dmg.sh                       # local build + DMG
#   macos/scripts/package_dmg.sh --bundle-backend      # embed harness/ + relocatable llama-server
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
  -destination 'platform=macOS,arch=arm64' \
  -derivedDataPath "$DERIVED" \
  CODE_SIGN_IDENTITY="-" \
  CODE_SIGNING_ALLOWED=YES \
  | grep -E "error:|warning:|BUILD SUCCEEDED|BUILD FAILED" || true

APP_SRC="$DERIVED/Build/Products/Release/Harness.app"
test -d "$APP_SRC"

mkdir -p "$DIST_DIR"
STAGE="$(mktemp -d /tmp/harness-dmg-stage.XXXXXX)"
trap 'rm -rf "$STAGE" "$DERIVED"' EXIT
APP_DST="$STAGE/Harness.app"
cp -R "$APP_SRC" "$APP_DST"

bundle_relocatable_llama() {
  local llama_bin="$1"
  local bin_dir="$2"
  local llama_dir
  llama_dir="$(dirname "$llama_bin")"

  cp -L "$llama_bin" "$bin_dir/llama-server"
  chmod 755 "$bin_dir/llama-server"
  # Copy Metal shader libraries if built separately alongside llama-server
  for mlib in "$llama_dir"/*.metallib; do
    [ -e "$mlib" ] && cp -L "$mlib" "$bin_dir/"
  done

  # Recursively collect every @rpath and non-system dylib needed by llama-server
  # (skipping unused CLI binaries and triplicate symlinks), then rewrite every
  # install name to @rpath/<basename> so zero /opt/homebrew or /Users paths leak.
  python3 - "$llama_dir" "$bin_dir" <<'PY'
import os, shutil, subprocess, sys

llama_dir, bin_dir = sys.argv[1], sys.argv[2]

def otool_deps(path):
    out = subprocess.check_output(["otool", "-L", path], text=True)
    deps = []
    for line in out.splitlines()[1:]:
        dep = line.strip().split(" ", 1)[0]
        if dep:
            deps.append(dep)
    return deps

def is_system(dep):
    return dep.startswith(("/System/", "/usr/lib/"))

queue = [os.path.join(bin_dir, "llama-server")]
seen = set()
copied = {}  # old_dep -> basename

while queue:
    target = queue.pop(0)
    if target in seen:
        continue
    seen.add(target)
    for dep in otool_deps(target):
        if is_system(dep):
            continue
        base = os.path.basename(dep)
        if base == os.path.basename(target):
            continue
        dst = os.path.join(bin_dir, base)
        if not os.path.exists(dst):
            src = os.path.join(llama_dir, base) if dep.startswith("@rpath/") else dep
            if not os.path.exists(src):
                raise RuntimeError(f"Missing dylib {dep} (looked at {src})")
            shutil.copy2(os.path.realpath(src), dst)
            os.chmod(dst, 0o755)
            queue.append(dst)
        if not dep.startswith("@rpath/"):
            copied[dep] = base

# Ensure llama-server searches its own directory for @rpath dylibs.
llama_dst = os.path.join(bin_dir, "llama-server")
subprocess.run(["install_name_tool", "-add_rpath", "@loader_path", llama_dst],
               stderr=subprocess.DEVNULL)

for name in sorted(os.listdir(bin_dir)):
    path = os.path.join(bin_dir, name)
    if not os.path.isfile(path) or name.endswith(".metallib"):
        continue
    if name.endswith(".dylib"):
        subprocess.run(["install_name_tool", "-id", f"@rpath/{name}", path], check=True)
    for dep in otool_deps(path):
        if dep in copied:
            subprocess.run(["install_name_tool", "-change", dep, f"@rpath/{copied[dep]}", path],
                           check=True)

# Verify zero non-system absolute paths remain anywhere in bin_dir.
for name in sorted(os.listdir(bin_dir)):
    path = os.path.join(bin_dir, name)
    if not os.path.isfile(path) or name.endswith(".metallib"):
        continue
    bad = [d for d in otool_deps(path)
           if not d.startswith(("/System/", "/usr/lib/", "@rpath/", "@loader_path/", "@executable_path/"))]
    if bad:
        raise RuntimeError(f"Unrelocated dylibs in {name}: {bad}")
print(f"    Relocated {len(seen) - 1} dylibs cleanly (0 Homebrew/host paths remaining)")
PY
}

if [ "$BUNDLE_BACKEND" = true ]; then
  echo "==> Bundling Harness Python backend into Contents/Resources/backend/"
  BACKEND="$APP_DST/Contents/Resources/backend"
  mkdir -p "$BACKEND/bin"
  rsync -a --exclude='__pycache__' --exclude='*.pyc' "$ROOT/harness/" "$BACKEND/harness/"
  LLAMA_BIN="${HARNESS_LLAMA_SERVER:-$HOME/llm/llama.cpp/build/bin/llama-server}"
  if [ -x "$LLAMA_BIN" ]; then
    bundle_relocatable_llama "$LLAMA_BIN" "$BACKEND/bin"
  else
    echo "    Note: llama-server not found at $LLAMA_BIN (skipped)"
  fi
fi

# Re-sign every nested Mach-O binary/dylib and the outer .app (required on Apple
# Silicon after install_name_tool, even for local ad-hoc builds).
SIGN_ID="${DEVELOPER_ID:--}"
SIGN_OPTS=()
if [ "$SIGN_ID" != "-" ]; then
  SIGN_OPTS=(--options runtime --timestamp)
  echo "==> Signing Harness.app with Developer ID ($SIGN_ID)"
else
  echo "==> Ad-hoc signing Harness.app and bundled binaries for local Apple Silicon"
fi

if [ -d "$APP_DST/Contents/Resources/backend/bin" ]; then
  for bin in "$APP_DST/Contents/Resources/backend/bin"/*; do
    [ -f "$bin" ] || continue
    [[ "$bin" == *.metallib ]] && continue
    codesign --force ${SIGN_OPTS[@]+"${SIGN_OPTS[@]}"} --sign "$SIGN_ID" "$bin"
  done
fi
codesign --force ${SIGN_OPTS[@]+"${SIGN_OPTS[@]}"} \
  --entitlements "$MACOS_DIR/HarnessMac/HarnessMac.entitlements" \
  --sign "$SIGN_ID" "$APP_DST"
codesign --verify --deep --strict "$APP_DST"

ln -s /Applications "$STAGE/Applications"
DMG_PATH="$DIST_DIR/Harness-${VERSION}-arm64.dmg"
rm -f "$DMG_PATH"

echo "==> Creating $DMG_PATH"
hdiutil create \
  -volname "Harness" \
  -srcfolder "$STAGE" \
  -ov -format UDZO \
  "$DMG_PATH" >/dev/null

if [ "$SIGN_ID" != "-" ]; then
  codesign --force --timestamp --sign "$SIGN_ID" "$DMG_PATH"
fi

if [ -n "${NOTARY_PROFILE:-}" ]; then
  echo "==> Submitting to Apple notarization ($NOTARY_PROFILE)"
  xcrun notarytool submit "$DMG_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG_PATH"
fi

echo "==> Done: $DMG_PATH"
