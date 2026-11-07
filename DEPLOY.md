# Установка и эксплуатация

## Структура и настройки

Код запускается из одного каталога проекта. Рабочие файлы:

- `.env`: секреты, Telegram ID и настройки окружения, права 0600;
- `health.db`: SQLite; `HEALTH_DB` явно переопределяет путь;
- `config.yaml`: правила расчётов и конфигурация моделей;
- `backups/`, `export/`, `history_archive/`: данные, исключённые из Git;
- `systemd/`: все сервисы и таймеры; `admin/nginx-health-admin.conf`: пример nginx.

`health_core.environment` читает `.env` относительно файлов проекта, независимо
от текущей папки запуска. Окружение процесса имеет приоритет. Админ-панель
записывает ключи в тот же файл, сохраняя права 0600. `.env` не включается в релиз.

## Установка на Linux

```bash
./install.sh /абсолютный/путь/к/проекту
chmod 600 .env
# Заполнить .env: TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS, HEALTH_ADMIN_IDS
# и ключи провайдеров, названные в config.yaml
.venv/bin/python -m admin.server --set-password
```

Установщик создаёт окружение, сохраняет существующие настройки и БД, применяет
миграции и запускает проверки. Он не запускает и не перезапускает сервисы.
Содержимое `.env.example` — только шаблон, реальные ключи нужно заполнить.
Для локального запуска и Windows используйте команды из README; `.env`
копируйте из `.env.example` только при отсутствии существующего файла.

## Миграция старой установки

1. Остановить бот, панель и фоновые задания на сервере.
2. Выполнить установщик из нового каталога проекта.
3. Если `.env` отсутствует, он копируется из `~/.hermes/.env` с правами 0600.
   Известный старый стандартный `HEALTH_DB` меняется на `health.db`.
4. Если БД проекта отсутствует, старая `~/.hermes/health.db` переносится через
   SQLite backup с учётом WAL. Исходные файлы сохраняются для отката.
5. Существующие `.env` и БД проекта никогда не перезаписываются. Для уже
   существующего `.env` вручную проверьте `HEALTH_DB` и уберите старое значение.
6. Удалить старые переопределения `HEALTH_DB` из оболочки/unit drop-ins.

Только миграция знает о старом каталоге. Рабочие процессы его не читают.

## Службы systemd

Поставляемые unit-файлы — системные. Их стандартный путь проекта:
`/opt/webapps/health_agent_system`. При другой установке обновите
`WorkingDirectory`, `ExecStart` и `EnvironmentFile` во всех файлах `systemd/`.

```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now health-agent.service health-admin.service
sudo systemctl enable --now health-dispatch.timer health-backup.timer
```

При обновлении уже работающих сервисов перезапуск выполняет оператор после
проверки миграции. Используйте `systemctl status` и `journalctl -u` для диагностики.
Панель по умолчанию слушает 127.0.0.1:8765; доступ — SSH-туннель или TLS-прокси
с настройкой `ADMIN_BEHIND_TLS=1`. Подробности — `admin/README-proxy.md`.

### Управление сервисами на сервере

Команды выполняются по SSH на сервере из любого каталога:

```bash
# Остановить / запустить / перезапустить бота
sudo systemctl stop health-agent.service
sudo systemctl start health-agent.service
sudo systemctl restart health-agent.service

# Проверить состояние и смотреть журнал (выход — Ctrl+C)
sudo systemctl status health-agent.service
sudo journalctl -u health-agent.service -f

# Перезапустить бота и административную панель после обновления кода или .env
sudo systemctl restart health-agent.service health-admin.service
```

Админ-панель управляется теми же командами с именем `health-admin.service`.
Перезапуск бота прерывает обрабатываемые ответы. После изменения самих
unit-файлов сначала скопируйте их в `/etc/systemd/system/` и выполните
`sudo systemctl daemon-reload`, затем перезапустите нужные службы.

Перед обслуживанием или переносом базы остановите бота, панель и таймеры:

```bash
sudo systemctl stop health-agent.service health-admin.service \
  health-dispatch.timer health-backup.timer

# Убедиться, что запущенные ранее фоновые задания завершились
sudo systemctl status health-dispatch.service health-backup.service
```

Остановка таймера не прерывает уже запущенное задание. Если фоновые службы
ещё активны, дождитесь их завершения перед переносом базы. Также приостановите
отдельные cron-задания, если они настроены вне systemd.

После обслуживания:

```bash
sudo systemctl start health-agent.service health-admin.service \
  health-dispatch.timer health-backup.timer
```

## Проверки и резервные копии

```bash
.venv/bin/python test_bot.py
.venv/bin/python test_e2e.py
.venv/bin/python scripts/dispatch.py --selftest
```

Тесты используют временные БД; E2E требует локальных Metrics. Не копируйте
открытый `health.db` обычным `cp`: для резервирования применяется SQLite backup.
`/export` отправляет человеку архив собственных записей. JSON до суммаризации
находятся в `history_archive/<telegram_id>/` рядом с рабочей БД.

## Релиз

`scripts/build_release.py` создаёт частный установочный архив с отдельным
снимком БД и шаблоном окружения. Реальные `.env` исключаются. Архив содержит
персональные данные, поэтому не публикуйте его как общедоступный дистрибутив.
Старые спецификации и инструкции сохранены в `Docs/archive/`.
