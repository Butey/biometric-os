# Замена Hermes: собственный цикл агента

Статус: контракт на реализацию. Каждый модуль пишется отдельно и проверяется
своим `__main__`, как принято в этом проекте.

## Почему

Hermes давал ровно четыре вещи: телеграм-транспорт, цикл вызова инструментов,
хранение истории и подгрузку персоны. За это платили обвязкой — собственным
системным промптом шлюза, skills-машинерией, tool-бабблами, session-контекстом
и каталогом-двойником `~/.hermes/plugins/health/`, который надо было
синхронизировать руками. Отсюда латентность и расход токенов.

Постоянный контекст нашей стороны измерен: персона 9.2 KB + 24 схемы 12.7 KB.
Всё, что сверх этого, приносил Hermes.

## Что НЕ меняется

- `health_core/` — вся доменная логика. О шлюзе не знает, менять нечего.
- `plugin/tools.py`, `plugin/schemas.py` — 24 хендлера и схемы. **Ни одной правки.**
  Контракт `register(ctx)` мы вызываем сами со своим `ctx`.
- `scripts/*.py` — cron в режиме `--no-agent`, печатают в stdout.
- `admin/` — FastAPI-панель, от Hermes не зависела.
- `config.yaml`, схема БД, `migrate.py`, `test_e2e.py`.

## Раскладка

Бот запускается **из каталога проекта**. Копий в `~/.hermes/` нет: ни плагина,
ни `SOUL.md`, ни `config.yaml`. Правка файла — правка боевого кода.
От `~/.hermes/` остаются только `.env` (секреты, общий канал с админкой)
и `health.db` (путь уже задаётся `HEALTH_DB`).

```
bot/
  registry.py    мост к plugin/tools.py: ctx-шим, описания, OpenAI-массив tools, диспетч
  llm.py         OpenAI-совместимый клиент, цепочка фолбэка, цикл tool-calls
  history.py     история диалога на пользователя в SQLite, обрезка окна
  knowledge.py   инструмент knowledge(topic) поверх Knowledge/
  main.py        aiogram: polling, allowlist, слэш-команды, склейка всего
scripts/notify.py  stdout cron-скрипта → sendMessage
```

## Контракты модулей

Соблюдать дословно: модули пишутся параллельно и должны собраться без правок.

### bot/registry.py

```python
TOOLS: dict[str, dict]        # {name: {"schema": dict, "handler": callable, "description": str}}
def openai_tools() -> list[dict]
    # [{"type":"function","function":{"name","description","parameters":schema}}, ...]
def dispatch(name: str, args: dict) -> str
    # синхронный вызов handler(args); всегда возвращает строку JSON, никогда не бросает
def set_caller(telegram_id: str) -> None
    # plugin.tools._CALLER_FALLBACK.set(str(telegram_id))
SLASH_COMMANDS: list[tuple[str, callable, str]]   # реэкспорт plugin.tools._SLASH_COMMANDS
def quick_macro(text: str) -> str | None          # реэкспорт plugin.tools._quick_macro_rewrite
```

Наполнение: собственный `ctx`-шим с методами `register_tool/register_hook/register_command`,
переданный в `plugin.tools.register(ctx)`. Образец шима уже есть в самотестах —
`plugin/tools.py:3362`.

Описания инструментов: `@_handler_wrapper` теряет `__doc__` (нет `functools.wraps`),
поэтому в `registry.py` лежит явный словарь `DESCRIPTIONS` — 24 строки по одной
фразе на инструмент, по-русски, из схем и текста `_HELP_USER`.

Личность звонящего: `plugin.tools._caller_telegram_id()` сперва пробует
`gateway.session_context` (у нас его нет → ImportError), затем читает ContextVar
`_CALLER_FALLBACK`. Мы пишем в этот ContextVar — правок в tools.py не требуется.
Хендлеры синхронные и блокирующие: вызывать через `asyncio.to_thread`, который
копирует контекст, поэтому ContextVar доезжает.

### bot/llm.py

Асинхронный, на `aiohttp` — его и так тянет aiogram, второй http-клиент в
проект не заводим.

```python
class Provider(TypedDict): base_url, api_key_env, model
async def chat(session, messages: list[dict], tools: list[dict], providers: list[Provider]) -> dict
    # POST {base_url}/chat/completions, Bearer из os.environ[api_key_env].
    # Ошибка/429/5xx/таймаут → следующий провайдер. Все упали → RuntimeError.
    # Возвращает message-объект ассистента как есть.
async def run_loop(session, messages, tools, providers, dispatch, max_iters=6) -> tuple[str, list[dict]]
    # цикл: chat → есть tool_calls, исполнить ВСЕ через
    # `await asyncio.to_thread(dispatch, name, args)` (хендлеры синхронные и
    # блокирующие, и to_thread копирует контекст, поэтому ContextVar с
    # личностью звонящего доезжает), дописать role:"tool" результаты, повторить.
    # Возвращает (текст ответа, дополненные messages).
    # Упёрлись в max_iters — вернуть последний текст или явную строку об этом.
```

`tool_calls[].function.arguments` приходит строкой JSON и бывает битым —
разбирать в try, при провале отдавать модели `{"error": ...}` как результат
инструмента, а не ронять ход.

Никакого стриминга: телеграм всё равно отдаёт сообщение целиком.

### bot/history.py

```python
def load(conn, telegram_id: str) -> list[dict]     # окно последних сообщений
def append(conn, telegram_id: str, message: dict) -> None
def clear(conn, telegram_id: str) -> None          # для /new
```

Таблица `chat_history(telegram_user_id TEXT, ts TEXT, role TEXT, content TEXT)`,
создаётся своим `CREATE TABLE IF NOT EXISTS` (не трогая `health_core/db.py`).
Окно: последние 20 сообщений И не больше 8000 символов, что жёстче. Резать
по границе хода — `tool`-сообщение без своего `assistant` ломает запрос.

### bot/knowledge.py

Один инструмент `knowledge(topic)`. В системный промпт уходит только индекс:
имя темы + одна строка описания на каждый файл в `Knowledge/`. Содержимое
читается по вызову. Так задумано под рост: планируются книги по питанию,
и вливать их в префикс нельзя.

Индекс и enum схемы строятся **при каждом запросе, из каталога**, а не
замораживаются при старте: файлы добавляются через веб-админку на живой
системе, и перезапуск бота ради нового файла недопустим.

```python
def index() -> str                                  # markdown-список тем для системного промпта
def schema() -> dict                                # JSON-схема, enum = текущее содержимое каталога
def read(topic: str, query: str | None = None) -> str
```

- Тема — имя файла без расширения. Описание — первая непустая строка файла
  после заголовка, обрезанная до 100 символов.
- Файл до 20 KB отдаётся целиком.
- Файл больше 20 KB **требует** `query`: возвращаются абзацы, содержащие
  подстроку (регистронезависимо), максимум 8 KB, с указанием сколько найдено
  ещё. Без `query` — строка-отказ с подсказкой, что нужен запрос.
  `ponytail:` подстрочный поиск, не эмбеддинги — перейти на нормальный поиск,
  когда книг станет больше десятка или начнёт мазать по синонимам.
- `admin_commands` отдаётся только в режиме admin (`plugin.tools._get_mode`).

### Загрузка знаний через админку

Раздел в `admin/` (менять только его — доменный код не трогать). Панель это
stdlib `http.server.ThreadingHTTPServer`, не FastAPI: зависимостей нет, и
приносить их не надо. Модуля `cgi` в python 3.13+ больше нет — multipart
разбирается через `email.parser.BytesParser` по boundary из Content-Type,
это ~40 строк.

- Страница `/knowledge`: список файлов `Knowledge/` (имя, размер, дата), форма
  загрузки, кнопка удаления. Под той же аутентификацией, что и остальная
  панель (`admin/auth.py`), доступ только админу.
- Приём: `.md`, `.txt` — кладутся как есть; `.pdf` — текст извлекается при
  загрузке и сохраняется как `.md` (зависимость `pypdf`). Прочие расширения
  отклоняются с внятным текстом.
- Имя файла санируется: только `[A-Za-z0-9_-]`, расширение принудительно `.md`
  или `.txt`. Путь собирается от `Knowledge/` и проверяется, что результат
  лежит внутри каталога — загрузка файла это граница доверия, `../` обязан
  отбиваться.
- Потолок размера — 5 MB на файл.
- Перезапись существующей темы — только с явным подтверждением в форме.

Бот подхватывает новый файл со следующего сообщения: индекс читается из
каталога на каждый запрос, перезапуск не нужен.

### bot/main.py

aiogram, long polling. Многопользовательский режим — это требование, не опция.

Порядок обработки входящего сообщения:
1. allowlist по `TELEGRAM_ALLOWED_USERS`; чужой id — молчание в чат, строка в лог.
2. `registry.set_caller(id)`.
3. слэш-команда → `SLASH_COMMANDS`, ответ сразу, модель не зовём.
4. `registry.quick_macro(text)` → если не None, дальше идёт переписанный текст.
5. история + системный промпт + `openai_tools()` + `knowledge.schema()` → `run_loop`.
6. ответ в телеграм, ход дописан в историю.

Системный промпт: `Core/system_promt.md` читается с диска при старте
(и перечитывается, если mtime изменился) + индекс знаний. Никаких копий.

Встроенные команды шлюза, которые придётся отдать самим, раз Hermes ушёл:
`/new` (очистить историю), `/help` (наш `handle_help`), `/status`.
Меню телеграма — `set_my_commands` этими тремя.

### scripts/notify.py

```
python scripts/notify.py <script.py> [--to <telegram_id>|--all]
```

Запускает cron-скрипт, забирает stdout, шлёт `sendMessage`. Пусто на выходе —
не слать ничего. Ненулевой код возврата — слать текст ошибки только админам.

## Конфигурация

Новый раздел в `config.yaml` (провайдеры — по порядку, первый основной):

```yaml
bot:
  history_window: 20
  history_chars: 8000
  max_tool_iters: 6
  providers:
    - base_url: https://generativelanguage.googleapis.com/v1beta/openai
      api_key_env: GEMINI_API_KEY
      model: gemini-3.7-flash
    - base_url: https://generativelanguage.googleapis.com/v1beta/openai
      api_key_env: GEMINI_API_KEY
      model: gemini-3.5-flash-lite
```

Секреты остаются в `~/.hermes/.env` — общий канал с админкой, менять его
незачем. Ключей в `config.yaml` нет и не будет.

## Что уходит в утиль

`HERMES-CONFIG.md`, раздел раскладки плагина в `DEPLOY.md`, симлинк `SOUL.md`,
каталог `~/.hermes/skills/health/`, `plugin/plugin.yaml` (файл описания для
чужого загрузчика — нашему не нужен, но пусть лежит: это ещё и единственный
плоский список 24 имён).

## Границы

- Проверок на LLM у нас, как и раньше, нет: числа считает python, модель их не трогает.
- `llm_calls` теперь заполнить МОЖНО — usage приходит в ответе провайдера.
  В этот заход не делаем; появится, когда понадобится учёт.
- Ретраи внутри одного провайдера не делаем: цепочка фолбэка дешевле и проще.
