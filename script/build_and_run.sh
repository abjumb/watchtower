#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-run}"
APP_NAME="Watchtower"
MODULE_NAME="watchtower"
BUNDLE_ID="local.watchtower.Watchtower"
MIN_SYSTEM_VERSION="14.0"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"
LOG_FILE="${TMPDIR:-/tmp}/watchtower.log"
PID_FILE="${TMPDIR:-/tmp}/watchtower.pid"
DIST_DIR="$ROOT_DIR/dist"
APP_BUNDLE="$DIST_DIR/$APP_NAME.app"
APP_CONTENTS="$APP_BUNDLE/Contents"
APP_MACOS="$APP_CONTENTS/MacOS"
APP_BINARY="$APP_MACOS/$APP_NAME"
INFO_PLIST="$APP_CONTENTS/Info.plist"
LAUNCHER_C="$APP_MACOS/launcher.c"

usage() {
  echo "usage: $0 [run|--debug|--logs|--telemetry|--verify]" >&2
}

ensure_environment() {
  if [[ ! -x "$PYTHON" ]]; then
    echo "Missing .venv. Create it with Python 3.11, 3.12, or 3.13, then rerun." >&2
    echo "See README.md for the source-run setup." >&2
    exit 1
  fi

  "$PYTHON" - <<'PY'
import pygame  # noqa: F401
import watchtower  # noqa: F401
PY
}

stop_existing() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
    fi
    rm -f "$PID_FILE"
  fi

  pkill -f "$PYTHON -m $MODULE_NAME" >/dev/null 2>&1 || true
  pkill -f "$ROOT_DIR/.venv/bin/$MODULE_NAME" >/dev/null 2>&1 || true
}

build_app() {
  "$PYTHON" -m compileall -q "$ROOT_DIR/watchtower" "$ROOT_DIR/main.py"
  rm -rf "$APP_BUNDLE"
  mkdir -p "$APP_MACOS"

  cat >"$LAUNCHER_C" <<LAUNCHER
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(void) {
  if (chdir("$ROOT_DIR") != 0) {
    perror("chdir");
    return 127;
  }

  setenv("PYTHONUNBUFFERED", "1", 1);
  freopen("$LOG_FILE", "a", stdout);
  freopen("$LOG_FILE", "a", stderr);

  char *const argv[] = {"$PYTHON", "-m", "$MODULE_NAME", NULL};
  execv("$PYTHON", argv);
  perror("execv");
  return 127;
}
LAUNCHER
  cc "$LAUNCHER_C" -o "$APP_BINARY"

  cat >"$INFO_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>$APP_NAME</string>
  <key>CFBundleIdentifier</key>
  <string>$BUNDLE_ID</string>
  <key>CFBundleName</key>
  <string>$APP_NAME</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>LSMinimumSystemVersion</key>
  <string>$MIN_SYSTEM_VERSION</string>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
PLIST
}

find_app_pid() {
  pgrep -f "$PYTHON -m $MODULE_NAME" | head -n 1 || true
}

launch_app() {
  : >"$LOG_FILE"
  /usr/bin/open -n "$APP_BUNDLE"
  sleep 2

  local pid
  pid="$(find_app_pid)"
  if [[ -z "$pid" ]]; then
    echo "Watchtower exited during startup. Recent log output:" >&2
    tail -n 40 "$LOG_FILE" >&2 || true
    exit 1
  fi

  echo "$pid" >"$PID_FILE"
  echo "Watchtower launched with pid $pid"
  echo "Log: $LOG_FILE"
}

cd "$ROOT_DIR"
ensure_environment
stop_existing
build_app

case "$MODE" in
  run)
    launch_app
    ;;
  --debug|debug)
    "$PYTHON" -m pdb "$ROOT_DIR/watchtower/__main__.py"
    ;;
  --logs|logs)
    launch_app
    tail -f "$LOG_FILE"
    ;;
  --telemetry|telemetry)
    launch_app
    tail -f "$LOG_FILE"
    ;;
  --verify|verify)
    launch_app
    sleep "${WATCHTOWER_VERIFY_DELAY:-4}"
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" >/dev/null 2>&1; then
      echo "Verified Watchtower is running with pid $pid"
    else
      echo "Watchtower is not running after launch. Recent log output:" >&2
      tail -n 40 "$LOG_FILE" >&2 || true
      exit 1
    fi
    ;;
  *)
    usage
    exit 2
    ;;
esac
