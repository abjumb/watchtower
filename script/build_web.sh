#!/usr/bin/env bash
# Build (or serve) the browser/WASM version of Watchtower with pygbag.
#
# The browser build is demo-mode only: single-threaded, deterministic demo
# telemetry, no network and no model API keys (see main.py / WatchtowerApp.run_async).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"
SRC="$ROOT_DIR/build/web_src"
MODE="${1:-build}"

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing .venv. Create it with Python 3.12 or 3.13, then rerun." >&2
  exit 1
fi
if ! "$PYTHON" -c "import pygbag" >/dev/null 2>&1; then
  echo "pygbag is not installed. Install it with:" >&2
  echo "  uv pip install --python .venv/bin/python 'pygbag>=0.9'" >&2
  exit 1
fi

# Assemble a minimal source tree for pygbag: just the entry point and the
# watchtower package, so the bundle stays small (no .venv/.git/dist/tests).
rm -rf "$SRC"
mkdir -p "$SRC"
cp "$ROOT_DIR/main.py" "$SRC/main.py"
cp -R "$ROOT_DIR/watchtower" "$SRC/watchtower"
find "$SRC" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

case "$MODE" in
  build)
    "$PYTHON" -m pygbag --build "$SRC/main.py"
    echo "Built web bundle at: $SRC/build/web"
    echo "Serve it with any static server, e.g.:  python -m http.server -d $SRC/build/web"
    ;;
  serve)
    echo "Serving at http://localhost:8000  (Ctrl-C to stop)"
    "$PYTHON" -m pygbag "$SRC/main.py"
    ;;
  *)
    echo "usage: $0 [build|serve]" >&2
    exit 2
    ;;
esac
