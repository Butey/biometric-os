#!/bin/bash
# Обновление на сервере: зависимости, проверка кода и тесты; с --restart еще перезапуск служб
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Установка зависимостей..."
.venv/bin/pip install -q -r requirements.lock

echo "Компиляция модулей..."
find bot plugin admin health_core -name '*.py' -print0 | xargs -0 .venv/bin/python -m py_compile

echo "Смоук импорта..."
.venv/bin/python -c "import bot.main, admin.server"

echo "Тесты..."
.venv/bin/python test_bot.py

if [ "${1:-}" = "--restart" ]; then
    echo "Перезапуск служб..."
    since=$(date '+%Y-%m-%d %H:%M:%S')
    systemctl restart health-agent.service health-admin.service
    sleep 20

    echo "Проверка состояния служб..."
    for svc in health-agent.service health-admin.service; do
        if ! systemctl is-active --quiet "$svc"; then
            echo "Служба $svc не активна"
            exit 1
        fi
    done

    echo "Проверка журнала бота..."
    out=$(journalctl -u health-agent.service --since "$since" --no-pager)
    if ! grep -q "бот запущен" <<<"$out"; then
        echo "В журнале health-agent нет строки «бот запущен»"
        exit 1
    fi
fi

echo "Обновление завершено"
