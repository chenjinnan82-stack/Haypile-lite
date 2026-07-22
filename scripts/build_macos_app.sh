#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "Haypile v0.3 internal build requires Apple Silicon macOS." >&2
  exit 1
fi

PYTHON="${PYTHON:-python3.12}"
VENV="$ROOT/.build-venv"
BUILD_DIR="$ROOT/build"
DIST_DIR="$ROOT/dist"
DEPLOY_DIR="$ROOT/deployment"
DEPLOY_LOG="$BUILD_DIR/pyside6-deploy.log"
ICONSET="$BUILD_DIR/Haypile.iconset"
APP="$DIST_DIR/Haypile.app"
BIN="$APP/Contents/MacOS/Haypile"
ZIP="$DIST_DIR/Haypile-v0.3.0-alpha.5-macos-arm64.app.zip"
MACOS_BUILD_VERSION="3005"
SPEC="$ROOT/pysidedeploy.spec"
ICON_SOURCE="$ROOT/assets/haypile-app-icon.png"
SPEC_BACKUP=""
SMOKE_ROOT=""
backend_pid=""

cleanup() {
  if [[ -n "$backend_pid" ]] && kill -0 "$backend_pid" 2>/dev/null; then
    kill "$backend_pid" 2>/dev/null || true
    wait "$backend_pid" 2>/dev/null || true
  fi
  if [[ -n "$SMOKE_ROOT" ]]; then
    rm -rf "$SMOKE_ROOT"
  fi
  if [[ -n "$SPEC_BACKUP" && -f "$SPEC_BACKUP" ]]; then
    cp "$SPEC_BACKUP" "$SPEC"
    rm -f "$SPEC_BACKUP"
  fi
  rm -rf "$DEPLOY_DIR"
}
trap cleanup EXIT

command -v "$PYTHON" >/dev/null
command -v sips >/dev/null
command -v iconutil >/dev/null
command -v codesign >/dev/null
command -v ditto >/dev/null
test -f "$ICON_SOURCE"

if [[ ! -x "$VENV/bin/python3" ]]; then
  "$PYTHON" -m venv "$VENV"
fi

"$VENV/bin/python3" -m pip install --quiet --upgrade pip
"$VENV/bin/python3" -m pip install --quiet -r requirements-desktop.txt "Nuitka==4.0"

SPEC_BACKUP="$(mktemp)"
cp "$SPEC" "$SPEC_BACKUP"

rm -rf "$DEPLOY_DIR" "$ICONSET" "$APP" "$ZIP" "$ZIP.sha256"
mkdir -p "$ICONSET" "$DIST_DIR"

while read -r pixels filename; do
  sips -z "$pixels" "$pixels" "$ICON_SOURCE" --out "$ICONSET/$filename" >/dev/null
done <<'EOF'
16 icon_16x16.png
32 icon_16x16@2x.png
32 icon_32x32.png
64 icon_32x32@2x.png
128 icon_128x128.png
256 icon_128x128@2x.png
256 icon_256x256.png
512 icon_256x256@2x.png
512 icon_512x512.png
1024 icon_512x512@2x.png
EOF
iconutil -c icns "$ICONSET" -o "$BUILD_DIR/Haypile.icns"

"$VENV/bin/pyside6-deploy" -c pysidedeploy.spec -f --keep-deployment-files \
  2>&1 | tee "$DEPLOY_LOG"

test -x "$BIN"
test -f "$APP/Contents/Resources/Haypile.icns"
test -f "$APP/Contents/MacOS/ui_assets/haypile-icon.png"
test -f "$APP/Contents/MacOS/ui_assets/drop-leaf-frame.svg"
test -f "$APP/Contents/MacOS/assets/haypile-app-icon.png"
BUILD_COMMIT="$(git rev-parse HEAD)"
GITHUB_RUN_ID="${GITHUB_RUN_ID:-local}"
/usr/bin/python3 -c 'import json, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(json.dumps({"version":"0.3.0-alpha.5","commit":sys.argv[2],"platform":"macos-arm64","workflow_run":sys.argv[3]}, indent=2, sort_keys=True)+"\n")' \
  "$APP/Contents/Resources/BUILD_INFO.json" "$BUILD_COMMIT" "$GITHUB_RUN_ID"
test -f "$APP/Contents/Resources/BUILD_INFO.json"
forbidden_runtime_path="$({
  find "$APP" -type d -name storage -print -quit
  find "$APP" -type f \( \
    -name .env -o \
    -name ipc_authkey -o \
    -name assets_manifest.json -o \
    -name storage_runtime.db -o \
    -name gui_state.json \
  \) -print -quit
} | head -n 1)"
if [[ -n "$forbidden_runtime_path" ]]; then
  echo "Haypile.app contains runtime or user state: $forbidden_runtime_path" >&2
  exit 1
fi
/usr/libexec/PlistBuddy -c 'Set :CFBundleIdentifier io.github.chenjinnan82-stack.haypile' "$APP/Contents/Info.plist"
if /usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP/Contents/Info.plist" >/dev/null 2>&1; then
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $MACOS_BUILD_VERSION" "$APP/Contents/Info.plist"
else
  /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $MACOS_BUILD_VERSION" "$APP/Contents/Info.plist"
fi
test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist")" = \
  "io.github.chenjinnan82-stack.haypile"
test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP/Contents/Info.plist")" = \
  "$MACOS_BUILD_VERSION"
if /usr/libexec/PlistBuddy -c 'Print :LSUIElement' "$APP/Contents/Info.plist" >/dev/null 2>&1; then
  echo "Haypile.app unexpectedly hides its Dock icon." >&2
  exit 1
fi

codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

MCP_SMOKE_OUTPUT="$(printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"haypile-build","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | "$BIN" --mcp)"
grep -q '"version": "0.3.0-alpha.5"' <<<"$MCP_SMOKE_OUTPUT"

SMOKE_ROOT="$(mktemp -d)"
SMOKE_PORT="${HAYPILE_SMOKE_PORT:-18010}"

STORAGE_DIR="$SMOKE_ROOT/storage" \
PORT="$SMOKE_PORT" \
IPC_CHANNEL="haypile_v030_packaged_smoke_$$" \
HAYPILE_IPC_AUTHKEY_FILE="$SMOKE_ROOT/ipc_authkey" \
  "$BIN" --backend >"$SMOKE_ROOT/backend.log" 2>&1 &
backend_pid=$!

for _ in {1..50}; do
  if curl -fsS "http://127.0.0.1:$SMOKE_PORT/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done
curl -fsS "http://127.0.0.1:$SMOKE_PORT/healthz" >/dev/null
curl -fsS "http://127.0.0.1:$SMOKE_PORT/readyz" >/dev/null
curl -fsS "http://127.0.0.1:$SMOKE_PORT/api/v1/bundles?status=ready" >/dev/null

kill "$backend_pid"
wait "$backend_pid" 2>/dev/null || true
backend_pid=""

ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
(
  cd "$DIST_DIR"
  shasum -a 256 "$(basename "$ZIP")" >"$(basename "$ZIP").sha256"
)

echo "Built: $APP"
echo "Archive: $ZIP"
echo "Checksum: $ZIP.sha256"
