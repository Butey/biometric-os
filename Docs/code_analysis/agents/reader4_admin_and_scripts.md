# Reader-4: Анализ веб-админки (`admin/`) и cron-скриптов (`scripts/`)

**Дата:** 2026-09-13  
**Агент:** Reader-4 (Gemini Flash / Code Reader)  
**Объект анализа:**
- Веб-панель администратора (`admin/`): `server.py`, `pages.py`, `auth.py`, `upload.py` (~4 760 строк)
- Cron-скрипты и оповещения (`scripts/`): `notify.py`, `morning_checkin.py`, `evening_report.py`, `injection_reminder.py`, `meal_window_check.py`, `weekly_recalc.py`, `export_backup.py`, а также вспомогательные `build_release.py`, `test_notify.py` (~950 строк)

---

## 1. Архитектурный обзор

### 1.1. Веб-админка (`admin/`)
- **Стек:** Исключительно стандартная библиотека Python 3.10+ (`http.server.ThreadingHTTPServer`, `http.server.BaseHTTPRequestHandler`, `hashlib`, `hmac`, `secrets`, `email.message_from_bytes`, `urllib`, `sqlite3`). Никаких сторонних веб-фреймворков (Flask/FastAPI/uvicorn) и шаблонизаторов (Jinja2) — жесткое ограничение по памяти для VPS с 1 ГБ RAM (где Hermes/LLM уже потребляет 250–400 МБ).
- **Сетевая изоляция:** По умолчанию сервер биндится строго на `127.0.0.1:8765`. Доступ предполагается через SSH-туннель (`ssh -L 8765:localhost:8765 user@vps`). Попытка биндинга на внешний интерфейс пресекается, если не передан флаг `--i-know-this-is-exposed`.
- **Рендеринг интерфейса:** 100% inline f-strings в `admin/pages.py`. Никаких сторонних CSS/JS библиотек, иконочных шрифтов или CDN. Графики генерируются на сервере в виде inline SVG. Поддерживается переключение светлой и темной тем (CSS Custom Properties + `localStorage`).
- **Модель пользователей:** Панель ориентирована на одного оператора. Большинство страниц (`/`, `/guards`, `/milestones`, `/plans`, `/workouts`, `/forecast`) берут первого пользователя из базы (`SELECT id FROM users ORDER BY id LIMIT 1`), при этом страница `/personas` позволяет просматривать все персоны и безвозвратно удалять выбранную.

### 1.2. Подсистема скриптов расписания (`scripts/`)
- **Запуск и расписание:** Скрипты вызываются из планировщика (системный cron или шлюз) с флагом `--no-agent` (без обращения к LLM на каждый тик, за исключением аварийного отчета).
- **Транспорт уведомлений:** `scripts/notify.py` оборачивает запуск скриптов, перехватывает `stdout` и отправляет сообщения в Telegram через `https://api.telegram.org/bot{token}/sendMessage` (stdlib `urllib.request`).
- **Принцип разделения данных:** При массовой рассылке (`--all`) для каждого зарегистрированного пользователя запускается отдельный процесс с флагом `--user <id>`, что исключает утечку персональных медицинских данных между пользователями.
- **Тихие тики:** Если скрипту нечего сообщить (например, `meal_window_check.py` вне окна или еда уже залогирована), он завершается с пустым выводом, и сообщение в Telegram не отправляется.

---

## 2. Модуль `admin/auth.py`

### 2.1. Назначение и сигнатуры функций
Обеспечивает примитивы безопасности: хэширование паролей, хранилище сессий, генерацию и валидацию CSRF-токенов, Rate Limiting и чтение/атомарную запись переменных в `~/.hermes/.env`.

```python
def load_env_file(path: Path = ENV_PATH, force: bool = False) -> None:
    """Populate os.environ from a KEY=VALUE file."""

def read_env_vars(path: Path = ENV_PATH) -> dict[str, str]:
    """Read all key-value pairs from .env without mutating os.environ."""

def update_env_vars(updates: dict[str, str | None], path: Path = ENV_PATH) -> None:
    """Update or append variables in .env atomically with 0600 permissions,
    preserving comments and existing variables. Also updates os.environ."""

def _strip_inline_comment(value: str) -> str:
    """Отрезает хвостовой комментарий (`374939064 # Comma-separated IDs`).
    Режет только `#` после пробела/табуляции."""

def gen_salt() -> bytes:
    """Генерирует 16 байт криптографической соли."""

def hash_password(password: str, salt: bytes) -> bytes:
    """Scrypt-хэширование пароля (N=16384, r=8, p=1, dklen=64)."""

def verify_password(password: str, salt_hex: str, expected_hash_hex: str) -> bool:
    """Constant-time compare against the stored hash — hmac.compare_digest, never ==."""

def client_ip(remote_addr: str, xff_header: str | None) -> str:
    """Real client IP for rate limiting. X-Forwarded-For is trusted ONLY when
    ADMIN_BEHIND_TLS is set, taking the LAST hop."""

def csrf_ok(session: dict | None, submitted: str | None) -> bool:
    """Проверяет соответствие CSRF-токена токену сессии через hmac.compare_digest."""
```

### 2.2. Классы и логика
1. **`RateLimiter`:**
   - Скользящее окно (`window_sec = 900` — 15 минут).
   - Ограничения: `max_per_ip = 5` (блокирует IP после 5 неудач), `global_max = 20` (блокирует форму входа для всех при 20 неудачах суммарно).
   - Защищен `threading.Lock()`.
   - Единое сообщение `LOGIN_ERROR_MSG = "Неверный пароль или вход временно заблокирован."` предотвращает time-based и статус-атаки.
2. **`SessionStore`:**
   - `timeout_sec = 3600` (читается из `ADMIN_SESSION_TIMEOUT_MIN`, по умолч. 60 мин).
   - `create(authed=False)`: генерирует токен `secrets.token_urlsafe(32)` и привязывает CSRF-токен `secrets.token_urlsafe(32)`.
   - `get(token)`: обновляет время жизни сессии (`expires = now + timeout_sec`), реализуя честный idle timeout.
   - `authenticate(token)`: защита от Session Fixation — удаляет старый анонимный токен и возвращает новый случайный токен, сохраняя сессионные данные.
   - Защищен `threading.Lock()`.

---

## 3. Модуль `admin/upload.py`

### 3.1. Назначение и сигнатуры функций
Разбор multipart/form-data и сохранение файлов в каталог знаний (`Knowledge/`).

```python
def parse_multipart(content_type: str, body: bytes) -> dict:
    """Разбирает тело multipart/form-data через email.message_from_bytes с policy=email.policy.HTTP."""

def sanitize_filename(raw_filename: str, is_pdf: bool) -> str | None:
    """Санирует имя файла: разрешены только [A-Za-z0-9_-], расширения .md/.txt/.pdf (для pdf выходной файл .md)."""

def safe_knowledge_path(knowledge_dir: Path, filename: str) -> Path | None:
    """Проверяет resolve()-ом, что путь не выходит за пределы knowledge_dir."""

def extract_pdf_text(data: bytes) -> str:
    """Извлекает текст из PDF через pypdf (опциональная зависимость)."""

def save_knowledge_file(knowledge_dir: Path, raw_filename: str, data: bytes, overwrite: bool) -> tuple[bool, str]:
    """Полный цикл валидации и сохранения файла знаний с ограничением 5 МБ (MAX_UPLOAD_BYTES)."""
```

### 3.2. Алгоритмы и безопасность
- Разбор HTTP-заголовков multipart с политикой `email.policy.HTTP` предотвращает ошибки при не-ASCII (кириллических) именах файлов.
- Двойная проверка путей: регулярное выражение `_SAFE_CHARS.sub("", p.stem)` + `Path.resolve()` с проверкой `parents`.
- Файлы PDF автоматически парсятся и сохраняются в формате Markdown (`.md`).
- Лимит на загрузку: 5 МБ (`MAX_UPLOAD_BYTES = 5 * 1024 * 1024`).

---

## 4. Модуль `admin/server.py`

### 4.1. Архитектура сервера
- Класс `Handler(BaseHTTPRequestHandler)` обслуживает запросы в пуле потоков `ThreadingHTTPServer`.
- Безопасные заголовки на всех ответах:
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: no-referrer`
  - `Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`
- Флаг куки `Secure`: устанавливается только если в окружении задана переменная `ADMIN_BEHIND_TLS=1`. В противном случае кука отдается без `Secure` для обеспечения работы через локальный SSH-туннель по HTTP.
- Входной контроль: метод `_require_auth()` проверяет аутентифицированность сессии, делает редирект на `/login` при отсутствии авторизации, открывает соединение с БД и запускает миграции (`db_migrate`).

### 4.2. Таблица маршрутов (Endpoints)

#### GET-эндпоинты
| URL | Хэндлер | Назначение |
|---|---|---|
| `/login` | `_get_login` | Форма входа, генерация CSRF-токена |
| `/` | `_get_dashboard` | Дашборд: замеры, КБЖУ, гарды, графики, тренды |
| `/guards` | `_get_guards` | Мониторинг всех 14 гардрейлов и форма редактирования |
| `/milestones` | `_get_milestones` | Список вех и форма добавления |
| `/thresholds` | `_get_thresholds` | Построчное редактирование порогов `config.yaml` |
| `/keys` | `_get_keys` | Модели, API-ключи, ротация, сортировка fallback-цепочки |
| `/personas` | `_get_personas` | Список персон, статистика записей, удаление |
| `/actions` | `_get_actions` | Запуск системных действий (экспорт, бэкап, пересчет, импорт) |
| `/knowledge` | `_get_knowledge` | Список файлов базы знаний `Knowledge/` |
| `/alerts` | `_get_alerts` | История алертов гардрейлов с пагинацией по 50 записей |
| `/plans` | `_get_plans` | Дневные планы питания/тренировок и недельный шаблон |
| `/workouts` | `_get_workouts` | Журнал тренировок, агрегаты, эффективность по видам спорта |
| `/forecast` | `_get_forecast` | Модель динамики веса Холла / NIH (band chart) |

#### POST-эндпоинты
| URL | Хэндлер | CSRF | Назначение |
|---|---|---|---|
| `/login` | `_post_login` | Да | Проверка пароля, защита от брутфорса, ротация сессии |
| `/logout` | `_post_logout` | Да | Удаление сессии, редирект |
| `/guards/save` | `_post_guards_save` | Да | Сохранение порогов гардрейлов через `_admin_set` |
| `/milestones` | `_post_milestones` | Да | UPSERT вехи (через `handle_set_milestone`) или удаление |
| `/thresholds` | `_post_thresholds` | Да | Хирургическое сохранение параметров `config.yaml` |
| `/keys` | `_post_keys` | Да | Сохранение ключей в `.env`, смена приоритета моделей |
| `/keys/test` | `_post_keys_test` | Да | Проверка доступности ключей и квот моделей (ping) |
| `/personas` | `_post_personas` | Да | Полное удаление персоны со всеми данными |
| `/actions` | `_post_actions` | Да | Запуск фоновых действий (`export`, `backup`, `recalc`, `import`) |
| `/actions/import-scale` | `_post_actions_import_scale` | Да | Загрузка и импорт выгрузок весов/TCX/ZIP |
| `/knowledge/upload` | `_post_knowledge_upload` | Да | Загрузка файла знаний с валидацией |
| `/knowledge/delete` | `_post_knowledge_delete` | Да | Удаление файла знаний с проверкой пути |
| `/plans/save` | `_post_plans_save` | Да | Сохранение плана на дату (`plan_log`) |
| `/plans/delete` | `_post_plans_delete` | Да | Удаление плана на дату |
| `/plans/template/save` | `_post_plans_template_save` | Да | Добавление элемента в недельный шаблон |
| `/plans/template/delete` | `_post_plans_template_delete` | Да | Удаление элемента/очистка недельного шаблона |
| `/workouts/save` | `_post_workouts_save` | Да | Ручное добавление тренировки |
| `/workouts/delete` | `_post_workouts_delete` | Да | Удаление тренировки по ID |

### 4.3. SQL-запросы и работа с базой данных
1. **Каскадное удаление персоны (`_delete_persona`):**
   - Охватывает 19 таблиц, ссылающихся на `user_id`:
     `user_targets`, `milestones`, `body_metrics`, `anthropometry`, `food_log`, `water_log`, `glucose_log`, `activity`, `med_schedule`, `pantry`, `daily_targets`, `alerts`, `med_log`, `llm_calls`, `import_log`, `refeed_days`, `meal_plan`, `workout_plan`, `persona_styles`, `plan_log`.
   - Явное удаление зависимой таблицы второго уровня:
     ```sql
     DELETE FROM food_items WHERE food_log_id IN (SELECT id FROM food_log WHERE user_id=?);
     ```
   - Явное удаление таблицы режима (ключуется по `telegram_user_id`):
     ```sql
     DELETE FROM user_mode WHERE telegram_user_id=?;
     ```
   - Удаление пользователя:
     ```sql
     DELETE FROM users WHERE id=?;
     ```
   - Выполняется в единой транзакции с `commit/rollback`.
2. **Параметризация:** Все динамические значения передаются строго через кортеж параметров `?`. Имена таблиц в `_delete_persona` интерполируются из жестко заданной константы `PERSONA_TABLES`.
3. **Ограничение одного пользователя:**
   ```sql
   SELECT id FROM users ORDER BY id LIMIT 1;
   ```
   Все основные разделы работают только с первой найденной персоной.

---

## 5. Модуль `admin/pages.py`

### 5.1. Архитектура HTML-генерации
- Полное отсутствие Jinja2 или сторонних шаблонизаторов: все страницы генерируются через Python f-strings.
- Функция экранирования: `_e(value)` возвращает `html.escape(str(value))` или `"—"` при `None`.
- Функция форматирования чисел: `_n(value, fmt)` предотвращает инъекции через числа.
- SVG-генерация графиков:
  - `svg_line_chart`: линейный график тренда с заливкой градиентом, динамической сеткой и крайними точками.
  - `svg_band_chart`: коридор прогноза (верхняя, средняя, нижняя границы + фактические замеры слева).

### 5.2. Формы и компоненты
- `dashboard_page`: карточки биометрии, прогресс от старта, дельта дня, полосы макронутриентов (КБЖУ + клетчатка + вода), активные предупреждения гардрейлов, лог приемов пищи (`<details>/<summary>`), графики веса и сухой массы за 90 дней.
- `guards_page`: карточки всех 14 правил гардрейлов с клиническим обоснованием, формулами и формой правки порогов.
- `thresholds_page`: построчная таблица параметров `config.yaml` с комментариями, извлеченными через `parse_config_comments`.
- `keys_page`: интерфейс управления провайдерами с Drag-and-Drop перетаскиванием строк таблицы (`_REORDER_SCRIPT`), быстрым назначением основной модели (#1) и проверкой доступности ключей.

---

## 6. Скрипты расписания (`scripts/`)

### 6.1. `scripts/notify.py`
- Шлюз отправки вывода скриптов в Telegram через Bot API `sendMessage`.
- Лимит одного сообщения Telegram — 4096 символов (`TELEGRAM_LIMIT`); более длинный текст автоматически нарезается.
- Режимы доставки:
  - `--admins`: один прогон скрипта, вывод рассылается всем админам из `config.yaml` (`admin.telegram_admin_ids`).
  - `--all`: для каждого пользователя в БД (`SELECT id, telegram_user_id FROM users`) запускается отдельный подпроцесс `python <script> --user <id>`. Если скрипт не поддерживает `--user`, выполняется fallback на запуск без флага.
  - `--to <tg_id>`: поиск пользователя в БД и запуск скрипта с флагом `--user`.
- Обработка сбоев: если скрипт завершается с ошибкой (`returncode != 0`), `stderr` пересылается администраторам.

### 6.2. `scripts/morning_checkin.py`
- Cron: ежедневно в 08:00 (§11), `--no-agent`.
- Вызывает `health_core.report.status_bar(conn, user_id)`.
- Выводит строку статуса дня: текущий вес, расход, BMR floor, бюджет КБЖУ.

### 6.3. `scripts/evening_report.py`
- Cron: ежедневно в 21:30 (§11), `--no-agent`.
- Вызывает `health_core.report.evening_report(conn, user_id, today)`.
- Изолирует ошибки пользователей через `try/except`.

### 6.4. `scripts/injection_reminder.py`
- Cron: воскресенье 21:00 (§11), еженедельно, `--no-agent`.
- Безусловно печатает `"Инъекция сегодня."`.

### 6.5. `scripts/meal_window_check.py`
- Cron: 10:00 (завтрак с 08:00), 14:00 (обед с 12:00), 20:00 (ужин с 18:00).
- Проверяет наличие записей в `food_log`:
  ```sql
  SELECT COUNT(*) c FROM food_log WHERE user_id=? AND eaten_at>=? AND date(eaten_at)=?;
  ```
- Если еда не залогирована, печатает напоминание. Если залогирована — вывод пустой (тихий тик).

### 6.6. `scripts/weekly_recalc.py`
- Cron: понедельник 08:00 (§11), `--no-agent`.
- Проверяет наличие $\ge 3$ утренних замеров (с 06:00 до 11:00) за всю историю:
  ```sql
  SELECT COUNT(*) c FROM body_metrics WHERE user_id=? 
  AND time(measured_at) BETWEEN '06:00:00' AND '11:00:00';
  ```
- При выполнении условия вызывает `health_core.energy.daily_target(conn, user_id, today)`.

### 6.7. `scripts/export_backup.py`
- Cron: ежедневно в 03:00 (§11), `--no-agent`.
- Экспорт витрины (`export_all`) для пользователей из `HEALTH_GDRIVE_USERS` (по умолчанию только `id=1`).
- Онлайн-бэкап базы данных через `sqlite3.backup()` (`backup_db`).
- Опциональная заливка в `HEALTH_RCLONE_REMOTE`.
- Вывод строки `MEDIA:<path>` для отправки файла бэкапа в Telegram.

---

## 7. Детальный аудит безопасности и найденные дефекты

### 7.1. Уязвимости безопасности (Security Vulnerabilities)

#### 1. Критическая уязвимость Stored XSS в `admin/pages.py` (строка 1689)
- **Файл:** `admin/pages.py:1689`
- **Код:**
  ```python
  sport_val = r.get("sport") or "Тренировка"
  # ...
  del_btn = f"""<form method="post" action="/workouts/delete" style="display:inline;" onsubmit="return confirm('Удалить тренировку #{w_id} ({sport_val})?');">"""
  ```
- **Уязвимость:** Значение `sport_val` извлекается из таблицы `activity` и подставляется в HTML-атрибут `onsubmit` **абсолютно без экранирования**.
- **Вектор эксплуатации:** При создании тренировки через форму `/workouts/save` (или через API бота) злоумышленник может передать название вида спорта:
  ```text
  Силовая'); alert(document.cookie); //
  ```
  или с выходом из атрибута двойной кавычкой:
  ```text
  Силовая" onmouseover="alert(1)
  ```
  Так как CSP в `server.py` содержит `script-src 'unsafe-inline'`, скрипт успешно выполняется в браузере администратора.

#### 2. Stored XSS через отсутствие валидации даты в `plans_page`
- **Файлы:** `admin/pages.py:1340, 1347, 1364` и `admin/server.py:1065-1081`
- **Код:**
  В `admin/server.py`:
  ```python
  date_str = (form.get("date") or "").strip()
  # Валидация формата даты отсутствует!
  conn.execute("INSERT INTO plan_log(user_id, date, kind, body, rationale, created_at) VALUES (?, ?, ?, ?, ?, ?)...", (user_id, date_str, ...))
  ```
  В `admin/pages.py`:
  ```python
  cells.append(f'<div class="card"><h3>{date}</h3>...')  # Не экранировано!
  del_form = f"""... onsubmit="return confirm('Удалить план ({kind_label}) на {date}?');">"""  # Не экранировано!
  del_btn = f"""... onsubmit="return confirm('Удалить план ({kind_label}) на {d}?');">"""  # Не экранировано!
  ```
- **Уязвимость:** Поле `date` при сохранении плана не проверяется на соответствие формату `YYYY-MM-DD`. Если передать в `date` HTML/JS пейлоад, он попадет в базу и будет выведен в открытом виде в `<h3>{date}</h3>`.

#### 3. XSS / Attribute Breakout через одинарные кавычки в `confirm(...)`
- **Файлы:** `admin/pages.py:1340, 1364, 1464, 1482`
- **Код:**
  ```python
  del_w = f"""... onsubmit="return confirm('Удалить тренировку {name}?');">"""
  ```
- **Уязвимость:** Хотя `name` проходит через `_e()`, HTML-сущность `&#x27;` в HTML-атрибуте декодируется браузером обратно в одинарную кавычку `'` **до** вызова JS-парсера. Название тренировки вида `O'Connor` ломает синтаксис скрипта, а специально сформированная строка приводит к выполнению произвольного JS.

#### 4. Риск Zip Slip при распаковке архивов выгрузок (`admin/server.py:180-181`)
- **Файл:** `admin/server.py:180-181`
- **Код:**
  ```python
  with zipfile.ZipFile(tmp_path, "r") as zf:
      zf.extractall(extract_dir)
  ```
- **Уязвимость:** Распаковка архива через `extractall()` без валидации путей файлов в архивах с относительными путями вида `../../path` может привести к перезаписи системных файлов.

---

### 7.2. Логические дефекты и несоответствия

1. **Несоответствие CSP и документации (`admin/pages.py:1751` vs `admin/server.py:391`):**  
   В коде страницы `knowledge` утверждается, что inline-JS заблокирован CSP (`default-src 'none' без script-src`). Однако на самом деле `server.py` отдает `script-src 'unsafe-inline'`, и на других страницах inline JS активно работает.
2. **Single-user Lock админки (`admin/server.py:99-101`):**  
   Админка всегда жестко берет `LIMIT 1` из таблицы `users`. Если ботом пользуются несколько человек, администратор в веб-панели может видеть данные только первого зарегистрированного пользователя.
3. **Невозможность удалить / очистить `rationale` плана (`admin/server.py:1077`):**  
   Конструкция `rationale=COALESCE(excluded.rationale, rationale)` не позволяет очистить поле: передача пустой строки преобразуется в `NULL`, и `COALESCE` оставляет старое значение.
4. **Неограниченное окно замеров в `weekly_recalc.py:48-51`:**  
   Подсчет утренних замеров для пересчета BMR ведется по всей истории базы без фильтра по дате (`measured_at`), что приводит к ложному срабатыванию гейта по замерам полугодовой давности.
5. **Безусловная рассылка напоминания об инъекциях (`scripts/injection_reminder.py`):**  
   Напоминание об инъекции уходит всем пользователям при рассылке `--all`, даже тем, у кого нет терапии инъекционными препаратами.

---

## 8. Сводная таблица уязвимостей

| # | Файл | Строка | Риск | Тип проблемы | Описание |
|---|---|---|---|---|---|
| 1 | `admin/pages.py` | L1689 | **Критический** | Stored XSS | `sport_val` не экранирован в атрибуте `onsubmit` |
| 2 | `admin/pages.py` | L1347, L1340 | **Высокий** | Stored XSS | `date` плана не валидируется и выводится сырым в `<h3>` и `onsubmit` |
| 3 | `admin/pages.py` | L1482, L1464 | **Средний** | JS Injection | Одинарные кавычки в `confirm('{name}')` ломают синтаксис JS |
| 4 | `admin/server.py` | L180-181 | **Высокий** | Zip Slip | `zipfile.extractall()` без фильтрации путей |
| 5 | `admin/server.py` | L99-101 | **Средний** | Архитектура | Админка залочена на `ORDER BY id LIMIT 1`, нет выбора персоны |
| 6 | `admin/server.py` | L1078 | **Низкий** | Баг логики | `COALESCE` блокирует очистку поля `rationale` в планах |
| 7 | `scripts/weekly_recalc.py` | L48-51 | **Средний** | Логика | Замеры для пересчета считаются без ограничения по дате |
| 8 | `scripts/injection_reminder.py` | L10-12 | **Низкий** | Логика рассылки | Сообщение об инъекциях уходит всем пользователям |

---

## 9. Рекомендации по исправлению

1. **Исправление XSS в `admin/pages.py`:**
   - Обернуть `sport_val` в `_e(sport_val)`.
   - Заменить опасные вызовы inline `onsubmit="return confirm(...)"` на отдельный скрипт с навешиванием слушателей событий и получением параметров через `dataset` (`data-name="..."`), либо экранировать кавычки в JS.
2. **Валидация формата даты в `_post_plans_save`:**
   - Добавить проверку `datetime.strptime(date_str, "%Y-%m-%d")` перед сохранением в `plan_log`.
3. **Безопасная распаковка ZIP:**
   - Проверять каждый элемент архива перед извлечением: путь не должен быть абсолютным и не должен содержать компонент `..`.
4. **Окно актуальности в `scripts/weekly_recalc.py`:**
   - Добавить условие `AND measured_at >= date('now', '-14 days')`.
5. **Мультипользовательский интерфейс:**
   - Добавить выпадающий список пользователей в панель навигации с параметром `?user_id=N`.
