"""Реестр инструментов для собственного цикла агента (замена Hermes).

plugin/tools.py и plugin/schemas.py — чужая, неприкасаемая территория (их
контракт — register(ctx), больше ничего). Мы наполняем TOOLS через тот же
register(), подсунув ему ctx-шим с register_tool/register_hook/register_command —
образец такого шима уже есть в самотестах plugin/tools.py:3362, повторяем его
дословно, а не изобретаем свой протокол.
"""
import json
import threading

from health_core import config
from plugin import tools as _tools

# {name: {"schema": dict, "handler": callable, "description": str}}
TOOLS: dict[str, dict] = {}

# @_handler_wrapper в plugin/tools.py не использует functools.wraps, поэтому
# __doc__ хендлеров теряется — модели нечем объяснить, зачем нужен инструмент.
# Список сверяется в самопроверке с provides_tools из plugin/plugin.yaml (24 шт).
DESCRIPTIONS = {
    "log_food": "Записать приём пищи: блюда с ккал/БЖУ от модели, либо grams + per_100g (из food_lookup) + source — тогда КБЖУ считает код. Также action=delete по food_log_id.",
    "food_lookup": "Состав продукта по названию: match — найти СВОЙ сохранённый продукт; search — до 5 кандидатов из Open Food Facts; remember — сохранить/обновить свой продукт (off_code сам подтянет состав, либо числа на 100 г); list — свои продукты; forget — убрать.",
    "log_water": "Записать выпитую воду в мл.",
    "log_glucose": "Записать замер сахара крови (ммоль/л), опционально контекст (до/после еды).",
    "log_side_effect": "Записать побочный эффект с тяжестью mild/moderate/severe. Без привязки к препарату — связь по времени приёма устанавливает модель.",
    "log_heart_rate": "Записать дневной пульс с часов (min/avg/max за конкретный день, можно сразу несколько дней). Только суточные значения — сводки за неделю/месяц не принимать.",
    "council": "Консилиум: честный разбор всех данных человека несколькими независимыми моделями в фоне. request запускает разбор и сразу отвечает, итог придёт отдельным сообщением; status — последний результат.",
    "log_labs": "Анализы крови текстом (маркеры и единицы — см. параметр markers): add — записать на дату сдачи; list — история; delete — по lab_id; derived — HOMA-IR, eGFR, non-HDL, eAG, TyG, TG/HDL, AIP, FIB-4.",
    "log_sleep": "Записать сон за ночь: длительность в минутах, время отбоя и подъёма, субъективная оценка 1-5.",
    "equipment": "Инвентарь для тренировок: список, добавить, убрать.",
    "plan_day": "План на конкретный день: get — прочитать, list — история, save — записать, delete — удалить (по дате или kind). kind=workout или meal. Текст плана пишешь ты, код только хранит.",
    "log_workout": "Записать выполненную тренировку/активность: вид спорта/упражнения, длительность в мин, калории, средний пульс, заметки.",
    "refeed": "Плановые перерывы в дефиците по протоколу MATADOR: status — фаза сегодня, schedule — расставить цикл 2 недели дефицита / 2 недели поддержания, clear — снять.",
    "sick": "Режим болезни: start — человек заболел; stop — выздоровел; status — текущее состояние. Правила режима — в ответе инструмента.",
    "forecast": "Прогноз массы: project — траектория на горизонт до 182 дней, reach — когда будет заданный вес. Правила подачи — в ответе инструмента.",
    "log_weight": "Записать вес и, если есть, показатели состава тела с весов (жир, мышцы, вода и т.п.).",
    "log_anthropometry": "Записать замер тела сантиметровой лентой: талия, таз, грудь, бедро, шея, бицепс.",
    "log_med": "Записать приём препарата или инъекцию: название, доза, путь введения.",
    "pharma": "Фарма-расписание: статус и следующая доза, задать/обновить препарат, пополнить остаток, убрать из расписания. schedule держит рамки лестницы титрации из карты препарата; by_doctor=true снимает рамки.",
    "drug_card_draft": "Черновик карты препарата без карты: fetch — официальные тексты по МНН латиницей; save — черновик из этих текстов на одобрение админу (/drafts).",
    "plans": "Недельный план питания и тренировок: show — показать, set_meal/set_workout — задать, vs_actual — план vs факт, remove_meal/remove_workout/clear — удалить позицию или очистить шаблон.",
    "import_scale_export": "Импортировать файл выгрузки с весов или тренировки (xlsx/xls/csv/tcx, включая путь на Google Диске).",
    "get_day_summary": "Сводка дня: КБЖУ-дашборд, детальный журнал еды по приёмам или общий метаболический пульт.",
    "get_trends": "Тренды веса, состава тела и эффективность всех тренировок из TCX (ккал/мин, пульс, виды спорта).",
    "get_status_bar": "Короткая статус-строка дня или визуальный дашборд-сводка.",
    "get_evening_report": "Вечерний отчёт по итогам дня.",
    "help": "Показать список возможностей бота (справка пользователю).",
    "get_weekly_summary": "Недельная сводка: вес, состав тела, дисциплина по КБЖУ, гарды.",
    "query_metrics": "Ряд значений одной метрики состава тела (вес, жир, мышцы, ИМТ и т.д.) за период.",
    "query_food": "Список съеденных блюд за диапазон дат.",
    "pantry": "Запасы в холодильнике/кладовке: список по категориям, добавить продукт, списать.",
    "style": "Стили ответов персоны: список, создать, сделать активным, удалить.",
    "explain_target": "Разложить расчёт целевых калорий и БЖУ на дату по шагам.",
    "get_progress": "Прогресс к конкретной вехе (цели) пользователя.",
    "register_user": "Зарегистрировать нового пользователя: рост, дата рождения, пол и другие базовые параметры, включая личные окна приёмов пищи (meal_windows).",
    "set_milestone": "Поставить новую веху (цель) по метрике с пороговым значением.",
    "admin_cmd": "Выполнить административную команду (доступно только в режиме admin).",
}


class _RegistryCtx:
    """Шим для plugin.tools.register(ctx) — тот же контракт, что у FakeCtx
    в самотестах plugin/tools.py (register_tool/register_hook/register_command),
    только тут это не одноразовый тест, а постоянный реестр бота."""

    def __init__(self):
        self.tools = {}
        self.hooks = {}
        self.commands = {}

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = {"schema": schema, "handler": handler}

    def register_hook(self, name, handler):
        self.hooks[name] = handler

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = (name, handler, description)


def _build() -> None:
    ctx = _RegistryCtx()
    _tools.register(ctx)
    for name, info in ctx.tools.items():
        TOOLS[name] = {
            "schema": info["schema"],
            "handler": info["handler"],
            # KeyError тут — это сигнал, что в DESCRIPTIONS забыли завести
            # новый инструмент. Лучше упасть на старте, чем молча отдать
            # модели тул без описания.
            "description": DESCRIPTIONS[name],
        }


_build()

# Прямой реэкспорт — bot/main.py не должен трогать plugin.tools напрямую.
SLASH_COMMANDS: list[tuple[str, callable, str]] = _tools._SLASH_COMMANDS


def openai_tools() -> list[dict]:
    """Формат OpenAI chat completions: [{"type": "function", "function": {...}}, ...]."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": info["description"],
                "parameters": info["schema"],
            },
        }
        for name, info in TOOLS.items()
    ]


_leaked = threading.local()
_real_connect = _tools.connect


def _tracking_connect():
    """Подменяет connect() в пространстве имён plugin.tools, чтобы знать, какие
    соединения открыл хендлер. Смысл — в release_connections ниже."""
    conn = _real_connect()
    if not hasattr(_leaked, "conns"):
        _leaked.conns = []          # threading.local: свой список на каждый поток пула
    _leaked.conns.append(conn)
    return conn


_tools.connect = _tracking_connect


def release_connections() -> None:
    """Хендлеры делают `conn = connect(); migrate(conn)` и закрывают соединение
    в конце — но НЕ в блоке finally. Упал INSERT (например, FOREIGN KEY) —
    соединение остаётся с открытой пишущей транзакцией, а поток-исполнитель
    держит на него ссылку до следующей задачи. В WAL один писатель блокирует
    остальных: следующий человек получает "database is locked" на пять секунд
    и отказ. В одном пользователе под Hermes это не всплывало, в многопользо-
    вательском боте всплывёт обязательно.

    Чиним в шве, который принадлежит нам: 24 хендлера в plugin/tools.py не
    трогаем, а после каждого вызова откатываем и закрываем всё, что он открыл.
    Двойной close() безвреден, закрытое соединение бросит ProgrammingError —
    её и глотаем."""
    conns = getattr(_leaked, "conns", None)
    if not conns:
        return
    for conn in conns:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
    conns.clear()


def dispatch(name: str, args: dict) -> str:
    """Синхронный вызов хендлера. Хендлеры уже обёрнуты @_handler_wrapper и сами
    не бросают — но неизвестное имя инструмента появляется ДО хендлера (модель
    может позвать несуществующий tool), поэтому здесь свой отдельный guard."""
    info = TOOLS.get(name)
    if info is None:
        return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)
    try:
        return info["handler"](args)
    finally:
        release_connections()


def set_caller(telegram_id: str) -> None:
    """Пишем в тот же ContextVar, который plugin.tools._caller_telegram_id()
    читает как fallback, когда gateway.session_context недоступен (мы и есть
    этот случай — Hermes ушёл).

    Заодно выставляем часовой пояс этого человека: от него зависит граница
    суток, а значит и то, в какой день лягут еда и вода. Без этого запись,
    сделанная в Москве утром, попадала во вчера по времени сервера."""
    _tools._CALLER_FALLBACK.set(str(telegram_id))
    config.set_tz(_tz_of(str(telegram_id)))


def _tz_of(telegram_id: str) -> str | None:
    """Пояс из профиля, без кэша: новый человек заполняет пояс при регистрации,
    и закэшированный None держал бы его на времени сервера до перезапуска.

    Профиля ещё нет или пояс не указан — общий дефолт из config.yaml
    (schedule.default_timezone), тот же, что использует диспетчер напоминаний.
    Совсем без дефолта в конфиге — на время сервера."""
    conn = _real_connect()
    try:
        row = conn.execute(
            "SELECT timezone FROM users WHERE telegram_user_id=?", (telegram_id,)
        ).fetchone()
        tz = row["timezone"] if row else None
    except Exception:
        tz = None               # профиля ещё нет — ниже отдадим дефолт из конфига
    finally:
        conn.close()
    if tz:
        return tz
    return (config.load().get("schedule") or {}).get("default_timezone")


def quick_macro(text: str) -> str | None:
    return _tools._quick_macro_rewrite(text)


if __name__ == "__main__":
    import os
    import tempfile
    from datetime import date
    from pathlib import Path

    tmp = tempfile.mkdtemp()
    os.environ["HEALTH_DB"] = str(Path(tmp) / "health.db")

    # health_core.db.DB_PATH — модульная константа, посчитанная один раз при
    # импорте; plugin.tools уже успел импортировать connect/migrate с дефолтным
    # путём (~/.hermes/health.db). reload() переисполняет health_core/db.py В
    # ТОМ ЖЕ __dict__ модуля, поэтому старые connect/migrate (уже привязанные в
    # plugin.tools) продолжают смотреть на этот же namespace и увидят новый
    # DB_PATH — ровно тот же приём, что в самопроверке plugin/tools.py.
    import importlib
    import health_core.db as db
    importlib.reload(db)
    db.DB_PATH = Path(os.environ["HEALTH_DB"])

    conn = db.connect()
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users(telegram_user_id, height_cm, birth_date, sex, created_at) "
        "VALUES (1, 185, '1992-08-09', 'm', '2026-08-20 00:00:00')"
    )
    uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]
    conn.execute(
        "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg) "
        "VALUES (?, 'burst-0', '2026-08-20 07:00:00', 80.0)",
        (uid,),
    )
    today = date.today().isoformat()
    conn.execute(
        "INSERT INTO daily_targets(user_id, date, kcal_target, protein_g_target, water_ml_target) "
        "VALUES (?, ?, ?, ?, ?)",
        (uid, today, 2200, 150, 3000),
    )
    conn.execute(
        "INSERT INTO user_targets(user_id, valid_from, water_ml) VALUES (?, ?, ?)",
        (uid, today, 3000),
    )
    conn.commit()

    expected_tools = {
        "log_food", "food_lookup", "log_water", "log_glucose", "log_side_effect", "log_heart_rate", "log_labs", "log_sleep", "log_weight",
        "equipment", "plan_day", "log_workout", "refeed", "sick", "forecast",
        "log_anthropometry", "log_med", "pharma", "drug_card_draft", "plans", "import_scale_export",
        "get_day_summary", "get_trends", "get_status_bar",
        "query_metrics", "query_food", "pantry", "style", "explain_target", "get_progress",
        "get_evening_report", "register_user", "set_milestone", "admin_cmd", "help",
        "get_weekly_summary", "council",
    }

    registered = set(TOOLS)
    assert registered == expected_tools, (
        f"не хватает: {expected_tools - registered}, лишние: {registered - expected_tools}")
    assert len(TOOLS) == len(expected_tools), f"ожидалось {len(expected_tools)} инструментов, получили {len(TOOLS)}"
    print(f"OK: зарегистрировано {len(TOOLS)} инструментов, имена совпадают с plugin.yaml")

    for name in TOOLS:
        assert DESCRIPTIONS.get(name), f"{name}: пустое описание в DESCRIPTIONS"
    print("OK: у всех инструментов непустое описание")

    ot = openai_tools()
    assert len(ot) == len(expected_tools)
    for item in ot:
        assert item["type"] == "function"
        fn = item["function"]
        assert fn["name"] in TOOLS
        assert fn["description"]
        assert isinstance(fn["parameters"], dict)
    print("OK: openai_tools() отдаёт валидную структуру")

    result = json.loads(dispatch("get_status_bar", {"user_id": uid, "format": "bar"}))
    assert "error" not in result, f"get_status_bar вернул ошибку: {result}"
    assert "status_bar" in result
    print(f"OK: dispatch(get_status_bar) на временной БД -> {result['status_bar']!r}")

    err = json.loads(dispatch("no_such_tool", {}))
    assert "error" in err, f"неизвестный инструмент должен вернуть {{'error': ...}}, получили {err}"
    print("OK: dispatch() несуществующего инструмента возвращает JSON с error, не бросает")

    set_caller("374939064")
    assert _tools._caller_telegram_id() == "374939064", "set_caller не долетел до _CALLER_FALLBACK"
    print("OK: set_caller() виден в plugin.tools._caller_telegram_id()")

    assert quick_macro("+30 белка") is not None, "quick_macro должен разобрать ярлык добора"
    assert quick_macro("2+2") is None, "quick_macro не должен цеплять произвольную арифметику"
    print("OK: quick_macro() распознаёт ярлык и не мажет по постороннему тексту")

    print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
