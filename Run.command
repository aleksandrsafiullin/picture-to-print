#!/bin/bash
# Double-click from Finder: starts the server and opens a browser.
# Stop with Ctrl+C, or close the Terminal window.

cd "$(dirname "$0")" || exit 1
PORT=8765
URL="http://127.0.0.1:$PORT"

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }
die() {
  printf '\n\033[1;31m%s\033[0m\n\n' "$1"
  osascript -e "display alert \"lineart2print\" message \"$1\"" >/dev/null 2>&1
  printf 'You can close this window.\n'
  exit 1
}

port_busy() {
  /usr/bin/python3 - "$PORT" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket()
s.settimeout(0.4)
sys.exit(0 if s.connect_ex(('127.0.0.1', int(sys.argv[1]))) == 0 else 1)
PY
}

# ---------------------------------------------------------------- already up?
if port_busy; then
  say "Already running at $URL — opening the browser."
  open "$URL"
  exit 0
fi

# ---------------------------------------------------------------- python
PY_BIN=""
for c in python3 /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$c" >/dev/null 2>&1; then PY_BIN="$c"; break; fi
done
[ -n "$PY_BIN" ] || die "Python 3 was not found. Install it with: xcode-select --install"

# ---------------------------------------------------------------- dependencies
if [ ! -d .venv ]; then
  say "First launch: installing dependencies. This takes a couple of minutes…"
  "$PY_BIN" -m venv .venv || die "Could not create the .venv environment"
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r app/requirements.txt || die "Could not install dependencies"
  say "Done."
fi

# ---------------------------------------------------------------- start
cleanup() { [ -n "$SRV" ] && kill "$SRV" 2>/dev/null; }
trap cleanup EXIT INT TERM

say "Starting lineart2print at $URL"
printf 'To stop: Ctrl+C, or close this window.\n\n'

./.venv/bin/python -m uvicorn server:app --app-dir app \
  --host 127.0.0.1 --port "$PORT" &
SRV=$!

# wait until the port answers, then open the browser
for _ in $(seq 1 60); do
  if port_busy; then open "$URL"; break; fi
  kill -0 "$SRV" 2>/dev/null || break
  sleep 0.5
done

wait "$SRV"
