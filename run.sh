#!/usr/bin/env bash
# Запуск lineart2print. Первый раз создаст .venv и поставит зависимости.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r app/requirements.txt
fi
exec ./.venv/bin/python -m uvicorn server:app --app-dir app --host 127.0.0.1 --port 8765 "$@"
