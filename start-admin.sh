#!/usr/bin/env sh
# Быстрый запуск админ-панели.
#
#     ./start-admin.sh              # 127.0.0.1:8765
#     ./start-admin.sh 127.0.0.1 9000
#
# Хост/порт берутся из аргументов, иначе из HEALTH_ADMIN_HOST/HEALTH_ADMIN_PORT,
# иначе loopback:8765. Небоквой-loopback хост панель сама отвергнет без флага
# --i-know-this-is-exposed — здесь этот флаг сознательно не пробрасывается.
set -eu

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

HOST="${HEALTH_ADMIN_HOST:-127.0.0.1}"
PORT="${HEALTH_ADMIN_PORT:-8765}"
[ $# -ge 1 ] && HOST="$1"
[ $# -ge 2 ] && PORT="$2"

# venv проекта (install.sh кладёт его именно сюда, не в ~/.hermes).
# Windows и POSIX раскладывают его по разным подкаталогам.
if [ -x "$PROJECT_DIR/.venv/Scripts/python.exe" ]; then
    PY="$PROJECT_DIR/.venv/Scripts/python.exe"
elif [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PY="$PROJECT_DIR/.venv/bin/python"
else
    PY="${PYTHON:-python}"
fi

# Без этого кириллица в выводе падает с UnicodeEncodeError на cp1251 в консоли
# Windows, и живая панель выглядит рухнувшей.
export PYTHONIOENCODING=utf-8

printf 'admin: %s\n' "$("$PY" --version 2>&1)"
printf 'открыть: http://%s:%s/\n\n' "$HOST" "$PORT"

# exec: панель становится этим же процессом, поэтому Ctrl+C и systemd-stop
# доходят до неё напрямую, без осиротевшего python под оболочкой.
exec "$PY" -m admin.server --host "$HOST" --port "$PORT"
