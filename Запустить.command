#!/bin/bash
# Двойной клик из Finder: поднимает сервер и открывает браузер.
# Закрыть — Ctrl+C или закрыть окно Терминала.

cd "$(dirname "$0")" || exit 1
PORT=8765
URL="http://127.0.0.1:$PORT"

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }
die() {
  printf '\n\033[1;31m%s\033[0m\n\n' "$1"
  osascript -e "display alert \"lineart2print\" message \"$1\"" >/dev/null 2>&1
  printf 'Окно можно закрыть.\n'
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

# ---------------------------------------------------------------- уже запущен?
if port_busy; then
  say "Сервер уже работает на $URL — открываю браузер."
  open "$URL"
  exit 0
fi

# ---------------------------------------------------------------- python
PY_BIN=""
for c in python3 /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$c" >/dev/null 2>&1; then PY_BIN="$c"; break; fi
done
[ -n "$PY_BIN" ] || die "Не найден Python 3. Установите его: xcode-select --install"

# ---------------------------------------------------------------- зависимости
if [ ! -d .venv ]; then
  say "Первый запуск: ставлю зависимости, это займёт пару минут…"
  "$PY_BIN" -m venv .venv || die "Не удалось создать окружение .venv"
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r app/requirements.txt || die "Не удалось поставить зависимости"
  say "Готово."
fi

# ---------------------------------------------------------------- запуск
cleanup() { [ -n "$SRV" ] && kill "$SRV" 2>/dev/null; }
trap cleanup EXIT INT TERM

say "Запускаю lineart2print на $URL"
printf 'Чтобы остановить — Ctrl+C или закройте это окно.\n\n'

./.venv/bin/python -m uvicorn server:app --app-dir app \
  --host 127.0.0.1 --port "$PORT" &
SRV=$!

# ждём, пока порт ответит, и открываем браузер
for _ in $(seq 1 60); do
  if port_busy; then open "$URL"; break; fi
  kill -0 "$SRV" 2>/dev/null || break
  sleep 0.5
done

wait "$SRV"
