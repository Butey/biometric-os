# Развёртывание health_core на слабом VPS

## Состояние на момент написания

**Готово к развёртыванию:**
- `health_core/` полностью реализован: db.py, energy.py, export.py, guards.py, ingest/, models.py, nutrition.py, report.py
- `plugin/tools.py` — 24 инструмента и схемы, `register(ctx)`
- `bot/` — собственный цикл агента (aiogram): registry, llm, history, knowledge, main. Контракт и архитектура — `Docs/bot_design.md`
- `Core/system_promt.md` — системный промпт, читается ботом с диска напрямую
- `Knowledge/*.md` — база знаний, инструмент `knowledge(topic)`; файлы также загружаются через `admin/` (`/knowledge`)
- `scripts/` — 6 cron-скриптов (режим `--no-agent`) + `notify.py`, который их запускает и доставляет вывод в телеграм
- `config.yaml` — конфигурация, включая раздел `bot.providers`
- `migrate.py` — импорт исторических данных
- `pyproject.toml` — конфигурация пакета, зависимость `aiogram` (тянет `aiohttp`)
- `install.sh` — автоматизированный скрипт установки для Debian VPS
- `health-agent.service` — пользовательский systemd-юнит: `ExecStart=.venv/bin/python -m bot.main`

Hermes в системе больше нет: ни шлюза, ни плагин-каталога, ни skills. Бот
запускается из каталога проекта собственным циклом на aiogram.

## Предпосылки

- **Python:** 3.10+
- **RAM:** 1 GB
- **Swap:** 2 GB (будет создан на шаге 1)
- **Диск:** ≥500 MB свободного места
- **ОС:** Debian/Ubuntu (инструкции и `install.sh` рассчитаны на Debian)

## Шаг 1. Настройка swap

**Только на VPS.** На Windows swap управляется автоматически.

```bash
sudo fallocate -l 2G /swapfile
```

```bash
sudo chmod 600 /swapfile
```

```bash
sudo mkswap /swapfile
```

```bash
sudo swapon /swapfile
```

```bash
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

```bash
sudo sysctl -w vm.swappiness=10
```

```bash
echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf
```

## Шаг 2. Установка (автоматическая)

```bash
cd <project_root>
./install.sh
```

Скрипт создаёт venv в каталоге проекта (`.venv/`), ставит туда пакет
(`pip install -e .`), инициализирует базу, заводит пустой `~/.hermes/.env`
(если его ещё нет) и прогоняет самотесты. Идемпотентен: повторный запуск
ничего не портит. Если скрипт не сработал — раздел «Ручная установка» ниже
делает то же самое по шагам.

### Ручная установка (если install.sh не подошёл)

```bash
cd <project_root>
python3 -m venv .venv
.venv/bin/pip install -e .
```

Инициализация БД:

```bash
.venv/bin/python -c "from health_core.db import connect, migrate; c=connect(); migrate(c); c.close()"
```

`.env` (создаётся, только если ещё не существует):

```bash
mkdir -p ~/.hermes
if [ ! -f ~/.hermes/.env ]; then
  cat > ~/.hermes/.env << 'EOF'
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=
GOOGLE_API_KEY=
EOF
  chmod 600 ~/.hermes/.env
fi
```

`~/.hermes/` — единственное, что осталось от Hermes, и то не как код, а как
общий канал секретов с `admin/` (`admin/auth.py` читает пароль оттуда же) и
как путь к боевой базе (`health.db`, задаётся `HEALTH_DB`, по умолчанию тоже
там). Ни плагин-каталога, ни `SOUL.md`, ни `skills/` в `~/.hermes/` больше не
появляется: бот читает код, персону и конфиг прямо из каталога проекта.

## Шаг 3. Секреты

```bash
nano ~/.hermes/.env
```

```
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklmnoPQRstuvWXYZabcd1234
TELEGRAM_ALLOWED_USERS=12345,67890
GOOGLE_API_KEY=AI...
```

- `TELEGRAM_BOT_TOKEN` — получить у BotFather в Telegram.
- `TELEGRAM_ALLOWED_USERS` — список Telegram id через запятую. **Критично:**
  бот по умолчанию отказывает всем — без этой переменной он не ответит никому
  (`bot/main.py::allowed_users`, fail closed).
- `GOOGLE_API_KEY` — имя переменной задаётся в `config.yaml`, раздел
  `bot.providers` (`api_key_env`). Если провайдера сменили — переменную нужно
  переименовать и здесь. Подробности выбора модели и фолбэка — в
  `Docs/llm_config.md`.

`chmod 600 ~/.hermes/.env` — install.sh делает это сам, но если файл правился
через другой канал, права стоит проверить: иначе ключи читает любой
пользователь на машине.

## Шаг 4. Проверки

Каждый запуск ниже должен вывести `OK` (модульные самотесты) или
`ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ` / `ALL TESTS PASSED` (сквозные). `install.sh`
прогоняет их сам; здесь — для ручного контроля после правок.

```bash
cd <project_root>
.venv/bin/python -m health_core.db
.venv/bin/python -m health_core.energy
.venv/bin/python -m health_core.guards
.venv/bin/python -m health_core.ingest.scale
.venv/bin/python -m health_core.export
.venv/bin/python -m plugin.tools
.venv/bin/python -m bot.registry
.venv/bin/python -m bot.llm
.venv/bin/python -m bot.history
.venv/bin/python -m bot.knowledge
```

Сквозные тесты — доменная логика и цикл агента отдельно:

```bash
.venv/bin/python test_e2e.py
.venv/bin/python test_bot.py
```

`test_bot.py` подменяет HTTP-слой `bot/llm.py` и не трогает сеть; живой ответ
провайдера этим не проверяется (см. README.md, раздел «Состояние»).

## Шаг 5. Регистрация пользователя

На этом этапе бот ещё не запущен постоянно — регистрация происходит после
шага 7 (автозапуск), диалогом `/start` в телеграме.

## Шаг 6. Миграция исторических данных

Запустите с путями к файлам:

```bash
.venv/bin/python migrate.py --user 1 --scale <path_or_dir> --tcx <dir> --anthro <file.csv>
```

Каждый флаг необязателен:
- `--user` ID пользователя в БД (по умолчанию 1)
- `--scale` путь к файлу (.xlsx, .csv) или каталогу весов
- `--tcx` каталог с файлами тренировок (.tcx)
- `--anthro` CSV с антропометрией

Примеры:

```bash
.venv/bin/python migrate.py --user 1 --scale ./exports/scale.xlsx
```

```bash
.venv/bin/python migrate.py --user 1 --scale ./exports --tcx ./tcx --anthro ./exports/anthro.csv
```

Миграция идемпотентна; повторный запуск не дублирует строки.

## Шаг 7. Автозапуск и первый запуск

Скопируйте systemd unit в пользовательскую директорию:

```bash
mkdir -p ~/.config/systemd/user
cp <project_root>/health-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

Юнит уже настроен на venv каталога проекта
(`ExecStart=%h/Health_agent_system/.venv/bin/python -m bot.main`,
`WorkingDirectory=%h/Health_agent_system`). Если проект развёрнут не в
`~/Health_agent_system` — поправьте оба пути в файле перед копированием.

Разрешите пользовательским сервисам жить без активной сессии. Без этого
systemd убьёт бота, как только вы выйдете из ssh — он замолчит, а логи будут
выглядеть как штатная остановка. Один раз, от root:

```bash
loginctl enable-linger <username>
```

Включите автозапуск и запустите:

```bash
systemctl --user enable --now health-agent.service
```

Проверьте статус и логи:

```bash
systemctl --user status health-agent.service
journalctl --user-unit health-agent.service -f
```

`health-agent.service` не задаёт `MemoryMax` — строка закомментирована.
Значение стоит ставить только после замера реального RSS в бою: заниженный
предел убьёт бота посреди диалога.

Теперь пользователь может отправить боту `/start` и зарегистрироваться
(шаг 5).

## Шаг 8. Задачи расписания

Только теперь, после регистрации: на пустой базе таблица `users` пуста, и
большинство скриптов из списка ниже завершается с ненулевым кодом, если в БД
нет ни одного пользователя (`morning_checkin.py`, `meal_window_check.py`,
`export_backup.py`, `weekly_recalc.py`, `evening_report.py` — все читают
`SELECT id FROM users` и отказываются, если пусто; `injection_reminder.py`
исключение, он ничего не проверяет и печатает текст безусловно, но
`notify.py --all` в этом случае просто не находит получателей).

Расписание — один systemd-таймер `health-dispatch`, раз в 10 минут. Он
запускает `scripts/dispatch.py`, который для КАЖДОГО человека смотрит его
локальное время (`users.timezone`, иначе `schedule.default_timezone`) и
отправляет задачи из `config.yaml::schedule.jobs`, чей слот наступил. Время
задач — местное время человека, сервер может стоять в любом поясе.
`dispatch_log` в базе не даёт отправить один слот дважды.

```bash
systemctl link /opt/webapps/health_agent_system/systemd/health-dispatch.service \
               /opt/webapps/health_agent_system/systemd/health-backup.service
systemctl enable --now /opt/webapps/health_agent_system/systemd/health-dispatch.timer \
                       /opt/webapps/health_agent_system/systemd/health-backup.timer
python scripts/dispatch.py --dry-run   # что ушло бы прямо сейчас
```

Отправку одной задачи делает `notify.py` — его можно звать и руками:

```bash
python scripts/notify.py <script.py> [--to <telegram_id> | --all | --admins]
```

При `--all` (и при `--to`, если этот telegram id зарегистрирован) скрипт
запускается отдельно на каждого получателя с `--user <db_id>`, и каждому
уходит только его собственный текст — так вес, еда и цели одного человека не
попадают в чат другого. Скрипт, который `--user` не понимает
(`injection_reminder.py` — статический текст без разбивки по людям),
запускается как раньше, одним общим прогоном на всех. `--admins`
(технические отчёты вроде `export_backup.py`) всегда один общий прогон без
`--user` — админам нужен один и тот же текст.

Пустой stdout — ничего не отправляется тому получателю, для которого он
пустой (например, `meal_window_check.py` вне окна приёма пищи — тихо для
всех). Ненулевой код возврата — текст ошибки уходит только администраторам
(`admin.telegram_admin_ids` в `config.yaml`), не пользователю.

Восьмая задача — вечерний отчёт (21:30) — раньше была единственной с вызовом
LLM: Hermes пересказывал текст `evening_report()` персоной. Теперь она тоже
детерминированная: `scripts/evening_report.py` печатает готовый текст, LLM в
расписании не вызывается вовсе (см. комментарий в самом файле).

Путь `/opt/webapps/health_agent_system` в юнитах `systemd/` — поправьте под
реальное расположение проекта, то же самое, что в `health-agent.service`.

## Откат

Если бот не влезает в 1 GB (OOM в `dmesg`):

1. Остановите: `systemctl --user stop health-agent.service`
2. Отключите автозапуск: `systemctl --user disable health-agent.service`
3. Разберитесь, что именно ест память — `bot/main.py` держит один
   `aiohttp.ClientSession` и по соединению SQLite на запрос, второго
   постоянного процесса (как у Hermes) в системе больше нет.

Если swap душит отзывчивость (ответ дольше 5 с) — переходите на VPS с 2 GB.
Раздутого агентного кеша, который раньше настраивался в `agent_cache` Hermes,
в системе больше нет: это была специфика чужого шлюза.

## Локальная проверка на Windows

Разработка и тестирование происходят на Windows перед развёртыванием на VPS.

### Путь к домашней папке

На Windows `~` означает `C:\Users\<username>`, поэтому `~/.hermes/` =
`C:\Users\<username>\.hermes\`.

### Кодировка консоли

Windows консоль по умолчанию использует cp1251. Кириллица и эмодзи из вывода
самотестов становятся нечитаемыми, а иногда роняют процесс `UnicodeEncodeError`.
Перед любыми `python -m ...` командами:

```powershell
$env:PYTHONIOENCODING='utf-8'
```

Или в cmd:

```cmd
set PYTHONIOENCODING=utf-8
```

### Установка

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .
```

```powershell
New-Item -Path "$env:USERPROFILE\.hermes" -ItemType Directory -Force
New-Item -Path "$env:USERPROFILE\.hermes\.env" -ItemType File -Force
```

Откройте `.env` в редакторе и заполните так же, как на шаге 3:

```
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklmnoPQRstuvWXYZabcd1234
TELEGRAM_ALLOWED_USERS=12345,67890
GOOGLE_API_KEY=AI...
```

Инициализация БД:

```powershell
.venv\Scripts\python -c "from health_core.db import connect, migrate; c=connect(); migrate(c); c.close()"
```

### Проверки — Windows и Debian одинаково

Благодаря `python -m`, проверки работают идентично на обеих платформах (не
забудьте `PYTHONIOENCODING` из раздела выше):

```powershell
python -m health_core.db
python -m health_core.energy
python -m health_core.guards
python -m health_core.ingest.scale
python -m health_core.export
python -m plugin.tools
python -m bot.registry
python -m bot.llm
python -m bot.history
python -m bot.knowledge
python test_e2e.py
python test_bot.py
```

### Запуск бота на Windows

```powershell
.venv\Scripts\python -m bot.main
```

Требует заполненного `.env` (шаг выше). Останавливается `Ctrl+C` — polling
не демонизирован.

**Автозапуск на Windows:** systemd недоступен. Используйте Task Scheduler
вручную или запускайте перед тестированием — этот шаг только на VPS.

### Миграция данных на Windows

```powershell
python migrate.py --user 1 --scale <path> --tcx <dir> --anthro <file.csv>
```

Пути к файлам используют обратные слэши или прямые — Python понимает оба:

```powershell
python migrate.py --user 1 --scale ".\exports\scale.xlsx"
```

## Не определено

- **Учёт LLM-токенов:** таблица `llm_calls` существует и описана в спеке, но
  ничто в коде её не заполняет. `bot/llm.py::chat` получает usage в теле
  ответа провайдера вместе с остальным JSON, но нигде его не сохраняет —
  запись отложена до момента, когда учёт токенов реально понадобится
  (`Docs/bot_design.md`, раздел «Границы»).
- **Бэкап и восстановление:** `export_backup.py` в 03:00 пишет
  `health_YYYY-MM-DD.db` и хранит `backup.keep_copies` последних копий (по
  умолчанию 7 — неделя). Повторный запуск в те же сутки перезаписывает файл
  этого дня, ротация идёт по дням, а не по числу запусков. Политика хранения
  задана нами, спека её не описывает.
- **Staging окружение:** процесс тестирования перед production не определён.
