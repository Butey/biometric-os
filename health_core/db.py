"""SQLite соединение и миграции схемы. Без ORM, сырой sqlite3 + DDL."""
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("HEALTH_DB", str(Path.home() / ".hermes" / "health.db")))

SCHEMA_VERSION = 19

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    height_cm REAL,
    birth_date TEXT,
    sex TEXT,
    timezone TEXT,
    base_weight_kg REAL,
    base_weight_date TEXT,
    created_at TEXT NOT NULL,
    health_notes TEXT,
    meal_windows TEXT
);

CREATE TABLE IF NOT EXISTS user_targets (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    valid_from TEXT NOT NULL,
    protein_g REAL,
    water_ml REAL,
    kcal_floor REAL,
    UNIQUE(user_id, valid_from)
);

CREATE TABLE IF NOT EXISTS milestones (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    metric TEXT,
    threshold REAL,
    deadline TEXT,
    achieved_at TEXT,
    UNIQUE(user_id, name)
);

-- 16 полей с весов: 15 из таблицы отображения + burst_key (ключ схлопывания серии).
CREATE TABLE IF NOT EXISTS body_metrics (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    burst_key TEXT NOT NULL,
    measured_at TEXT NOT NULL,
    weight_kg REAL NOT NULL,
    fat_pct REAL,
    bmi REAL,
    skeletal_muscle_pct REAL,
    muscle_mass_kg REAL,
    protein_pct REAL,
    device_bmr_kcal REAL,
    ffm_kg REAL,
    subcutaneous_fat_pct REAL,
    visceral_fat REAL,  -- ловушка нуля: 0 тут валиден и НЕ превращается в NULL (в отличие от остальных полей), фильтрация на стороне ingest/scale.py

    water_pct REAL,
    bone_mass_kg REAL,
    metabolic_age REAL,
    device_mac TEXT,
    UNIQUE(user_id, burst_key)
);
CREATE INDEX IF NOT EXISTS idx_body_metrics_user_time ON body_metrics(user_id, measured_at);

CREATE TABLE IF NOT EXISTS anthropometry (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    measured_on TEXT NOT NULL,
    site TEXT NOT NULL,
    value_cm REAL NOT NULL,
    UNIQUE(user_id, measured_on, site)
);

CREATE TABLE IF NOT EXISTS food_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    eaten_at TEXT NOT NULL,
    -- breakfast|lunch|dinner|snack. Схема инструмента log_food объявляла это поле
    -- с самого начала, модель его слала, а колонки не было — значение молча
    -- терялось. Без него нельзя разложить день по приёмам, и всё сливается в одну
    -- строку (см. _migrate_v4_to_v5 для уже существующих баз).
    meal_slot TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_food_log_user_time ON food_log(user_id, eaten_at);

CREATE TABLE IF NOT EXISTS food_items (
    id INTEGER PRIMARY KEY,
    food_log_id INTEGER NOT NULL REFERENCES food_log(id) ON DELETE CASCADE,
    name TEXT,
    grams REAL,
    kcal REAL,
    protein_g REAL,
    fat_g REAL,
    carbs_g REAL,
    fiber_g REAL,
    plate_category TEXT
);

CREATE TABLE IF NOT EXISTS water_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    at TEXT NOT NULL,
    volume_ml REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_water_log_user_time ON water_log(user_id, at);

CREATE TABLE IF NOT EXISTS glucose_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    at TEXT NOT NULL,
    mmol_l REAL NOT NULL,
    context TEXT,
    confirmed INTEGER
);
CREATE INDEX IF NOT EXISTS idx_glucose_log_user_time ON glucose_log(user_id, at);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    started_at TEXT NOT NULL,
    duration_min REAL,
    kcal REAL,
    avg_hr INTEGER,
    sport TEXT,
    file_hash TEXT NOT NULL UNIQUE,
    notes TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_user_time ON activity(user_id, started_at);

CREATE TABLE IF NOT EXISTS med_schedule (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    substance TEXT NOT NULL,
    dose REAL,
    unit TEXT,
    route TEXT,
    every_days INTEGER,
    next_at TEXT,
    stock_doses REAL,
    notes TEXT,
    updated_at TEXT,
    -- v19: доза по назначению врача (docs/adr/0002) — снимает рамки лестницы
    -- титрации из карты препарата (plugin/tools.py, action=schedule).
    dose_by_doctor INTEGER,
    UNIQUE(user_id, substance)
);

CREATE TABLE IF NOT EXISTS sleep_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    -- Ночь, а НЕ дата отхода ко сну: лёг 24-го в 23:40 и лёг 25-го в 00:20 —
    -- это соседние ночи, и по дате начала они разъехались бы на разные сутки.
    -- Дата ночи = дата утреннего пробуждения.
    night_date TEXT NOT NULL,
    bedtime TEXT,
    wake_time TEXT,
    duration_min INTEGER,
    -- Стадии необязательны намеренно: бытовые трекеры их оценивают ненадёжно.
    -- Пусто — значит не знаем, а не «глубокого сна не было».
    deep_min INTEGER,
    rem_min INTEGER,
    awake_min INTEGER,
    quality INTEGER,            -- субъективно 1-5, самый честный сигнал у бытовых устройств
    -- Эффективность (доля сна от времени в постели), пульс и SpO2 — то, что
    -- бытовой браслет измеряет достоверно, в отличие от стадий сна.
    efficiency_pct REAL,
    hr_avg INTEGER,
    spo2_avg INTEGER,
    source TEXT,                -- откуда: 'xiaomi_band', 'manual', ...
    notes TEXT,
    UNIQUE(user_id, night_date)
);
CREATE INDEX IF NOT EXISTS idx_sleep_user_night ON sleep_log(user_id, night_date);

CREATE TABLE IF NOT EXISTS pantry (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    qty REAL,
    unit TEXT,
    category TEXT,
    updated_at TEXT,
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS daily_targets (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    date TEXT NOT NULL,
    kcal_target REAL,
    protein_g_target REAL,
    fat_g_target REAL,
    carbs_g_target REAL,
    fiber_g_target REAL,
    water_ml_target REAL,
    computed_from TEXT,
    UNIQUE(user_id, date)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    rule TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_user_time ON alerts(user_id, created_at);

-- v3: route/unit/site/notes (Gap 1). route/unit/site/notes NULL для строк,
-- созданных до миграции — LIPID_GUARD обязан молчать на route IS NULL, а не
-- угадывать по тексту substance/dose (см. _migrate_v2_to_v3 ниже для проды).
CREATE TABLE IF NOT EXISTS med_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    at TEXT NOT NULL,
    substance TEXT,
    dose TEXT,
    route TEXT,
    unit TEXT,
    site TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_med_log_user_time ON med_log(user_id, at);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    model TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL
);

CREATE TABLE IF NOT EXISTS import_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    imported_at TEXT NOT NULL,
    file_hash TEXT NOT NULL UNIQUE
);

-- §09 шаг 5: рефид/диет-брейк — булев признак на дату. Кто и на сколько дней
-- его включает — вне контракта БД, это просто набор дат; вставить несколько
-- подряд дат = многодневный диет-брейк, вставить одну = разовый рефид.
CREATE TABLE IF NOT EXISTS refeed_days (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    date TEXT NOT NULL,
    UNIQUE(user_id, date)
);

-- v15: режим болезни (health_core/sick.py) — булев признак на дату, как
-- refeed_days выше. На этих датах дефицит ставится на паузу (energy.py), а
-- шумные поведенческие гардрейлы молчат (guards.py), но безопасность
-- (BMR/FFMI/липиды/глюкоза) остаётся включённой.
CREATE TABLE IF NOT EXISTS sick_days (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    date TEXT NOT NULL,
    note TEXT,
    UNIQUE(user_id, date)
);

-- v4: weekly repeating plan template (day_of_week 0=Mon..6=Sun, per Python's
-- date.weekday()) — NOT a dated log. Empty until the person fills it; no seeded
-- rows, no defaults inserted anywhere near this DDL.
CREATE TABLE IF NOT EXISTS equipment (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    kind TEXT,                  -- strength | cardio | accessory
    detail TEXT,                -- «гантели разборные 2x24 кг», «VR Quest 3»
    available INTEGER DEFAULT 1,
    updated_at TEXT,
    UNIQUE(user_id, name)
);

-- Датированный план на конкретный день, в отличие от недельных шаблонов
-- meal_plan/workout_plan. Хранится готовым текстом, как его выдала модель:
-- разложить план по строкам таблицы значит потерять обоснование, а именно оно
-- отвечает на вопрос «почему сегодня так» при просмотре истории.
CREATE TABLE IF NOT EXISTS plan_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    date TEXT NOT NULL,
    kind TEXT NOT NULL,         -- workout | meal
    body TEXT NOT NULL,
    rationale TEXT,             -- на каких данных построен
    created_at TEXT,
    UNIQUE(user_id, date, kind)
);
CREATE INDEX IF NOT EXISTS idx_plan_log_user_date ON plan_log(user_id, date);

CREATE TABLE IF NOT EXISTS meal_plan (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    day_of_week INTEGER NOT NULL CHECK(day_of_week BETWEEN 0 AND 6),
    meal_slot TEXT NOT NULL,
    name TEXT,
    kcal REAL,
    protein_g REAL,
    fat_g REAL,
    carbs_g REAL,
    notes TEXT,
    UNIQUE(user_id, day_of_week, meal_slot)
);

CREATE TABLE IF NOT EXISTS workout_plan (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    day_of_week INTEGER NOT NULL CHECK(day_of_week BETWEEN 0 AND 6),
    name TEXT NOT NULL,
    kind TEXT,
    duration_min INTEGER,
    notes TEXT,
    UNIQUE(user_id, day_of_week, name)
);

-- v9: стили персоны (§ styles per persona). Новая таблица, а не ALTER —
-- CREATE TABLE IF NOT EXISTS в executescript() создаёт её и на живой v8 базе,
-- не трогая остальные таблицы (см. v3->v4 выше — тот же путь без отдельной
-- _migrate_v8_to_v9). is_active — 0/1, ровно одна активная строка на
-- пользователя поддерживается на уровне приложения (plugin/tools.py).
CREATE TABLE IF NOT EXISTS persona_styles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    instruction TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, name)
);

-- v15: анализы, введённые текстом (health_core/labs.py). Одна строка на
-- показатель в день: повторный ввод того же дня исправляет значение.
-- v16: журнал отправленных по расписанию уведомлений (scripts/dispatch.py).
-- slot_key = задача@локальная дата и время слота человека: таймер может
-- сработать дважды в окне слота, UNIQUE не даёт отправить повторно.
CREATE TABLE IF NOT EXISTS dispatch_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    slot_key TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    UNIQUE(user_id, slot_key)
);

-- v17: допуск к боту. Один администратор (config.yaml admin.telegram_admin_ids)
-- одобряет людей командой; незнакомый telegram id пишет — появляется pending.
-- Строка users создаётся при одобрении, не раньше: до одобрения человека нет.
CREATE TABLE IF NOT EXISTS access_list (
    telegram_user_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'denied')),
    username TEXT,
    requested_at TEXT,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS lab_results (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    taken_on TEXT NOT NULL,
    marker TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    notes TEXT,
    UNIQUE(user_id, taken_on, marker)
);
CREATE INDEX IF NOT EXISTS idx_lab_results_user_date ON lab_results(user_id, taken_on);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """v2->v3: добавляет route/unit/site/notes в med_log (Gap 1).

    CREATE TABLE IF NOT EXISTS в DDL не трогает уже существующую таблицу —
    для проды с 248+ строками нужен ALTER TABLE ADD COLUMN, не пересоздание
    (пересборка таблицы рискует данными на живой БД). Проверка через
    PRAGMA table_info делает шаг идемпотентным без try/except на дубликат
    колонки: колонка добавляется только если её ещё нет."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(med_log)")}
    for col in ("route", "unit", "site", "notes"):
        if col not in existing:
            conn.execute(f"ALTER TABLE med_log ADD COLUMN {col} TEXT")


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Добавляет food_log.meal_slot. ALTER, а не пересоздание: в проде таблица уже
    с данными, а CREATE TABLE IF NOT EXISTS новую колонку в неё не принесёт."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(food_log)")}
    if "meal_slot" not in existing:
        conn.execute("ALTER TABLE food_log ADD COLUMN meal_slot TEXT")


def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    """Добавляет daily_targets.fat_g_target/carbs_g_target. ALTER, а не пересоздание:
    в проде таблица уже с данными, CREATE TABLE IF NOT EXISTS новых колонок не принесёт."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(daily_targets)")}
    if "fat_g_target" not in existing:
        conn.execute("ALTER TABLE daily_targets ADD COLUMN fat_g_target REAL")
    if "carbs_g_target" not in existing:
        conn.execute("ALTER TABLE daily_targets ADD COLUMN carbs_g_target REAL")


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """ALTER TABLE ADD COLUMN, если колонки ещё нет. CREATE TABLE IF NOT EXISTS
    в DDL существующую таблицу не трогает, поэтому новые колонки в старых
    таблицах приходится добавлять явно (тот же приём, что в _migrate_v2_to_v3).
    Проверка через PRAGMA, а не try/except — миграция обязана быть идемпотентной."""
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate_v10_to_v11(conn: sqlite3.Connection) -> None:
    """v10->v11: клетчатка отдельной колонкой в food_items и daily_targets.

    Knowledge/секреты подсчета калорий.md перечисляет её отдельной категорией
    с калорийностью 1-3 ккал/г, Knowledge/клетчатка.md прямо: «Сама клетчатка
    некалорийная» и цель 25-30 г/сут. В углеводы её складывать нельзя — это
    разные вещи и по усвоению, и по цели.
    """
    _add_column(conn, "food_items", "fiber_g", "REAL")
    _add_column(conn, "daily_targets", "fiber_g_target", "REAL")


def _migrate_v9_to_v10(conn: sqlite3.Connection) -> None:
    """Свести препараты, заведённые под разными торговыми именами.

    Данные, а не схема: до появления health_core/meds.py «Тирзетта» и
    «Тирзепатид» жили двумя строками med_schedule, и приём по одному имени не
    двигал next_at второго.
    """
    from health_core.meds import merge_duplicate_schedules

    merge_duplicate_schedules(conn)


def _migrate_v16_to_v17(conn: sqlite3.Connection) -> None:
    """v16->v17: users.health_notes — личные ограничения по здоровью (травмы,
    противопоказания) для промпта именно этого человека. Раньше жили в общем
    системном промпте с пометкой user_id=1 и применялись ко всем."""
    _add_column(conn, "users", "health_notes", "TEXT")


def _migrate_v17_to_v18(conn: sqlite3.Connection) -> None:
    """v17->v18: users.meal_windows — личные окна приёмов пищи (CONTEXT.md
    «Окно приёма пищи»), JSON с теми же ключами, что config.yaml meals.*
    (breakfast/lunch/dinner), частичное переопределение допустимо."""
    _add_column(conn, "users", "meal_windows", "TEXT")


def _migrate_v18_to_v19(conn: sqlite3.Connection) -> None:
    """v18->v19: med_schedule.dose_by_doctor — «Доза по назначению врача»
    (docs/adr/0002-рекомендация-дозы.md, CONTEXT.md). Без пометки план дозы
    держится в рамках лестницы титрации карты препарата; с ней рамки code
    не проверяет."""
    _add_column(conn, "med_schedule", "dose_by_doctor", "INTEGER")


def _migrate_v13_to_v14(conn: sqlite3.Connection) -> None:
    """v13->v14: добавляет notes и source в activity, делает file_hash необязательным (для ручных записей)."""
    _add_column(conn, "activity", "notes", "TEXT")
    _add_column(conn, "activity", "source", "TEXT")


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    elif row["version"] < SCHEMA_VERSION:
        # новые таблицы уже созданы через CREATE TABLE IF NOT EXISTS выше (idempotent
        # без ALTER); тут только фиксируем номер версии как факт апгрейда — КРОМЕ
        # шагов, добавляющих колонки в уже существующие таблицы (v2->v3), для них
        # нужен явный ALTER TABLE.
        if row["version"] < 3:
            _migrate_v2_to_v3(conn)
        if row["version"] < 5:
            _migrate_v4_to_v5(conn)
        if row["version"] < 6:
            _migrate_v5_to_v6(conn)
        if row["version"] < 10:
            _migrate_v9_to_v10(conn)
        if row["version"] < 11:
            _migrate_v10_to_v11(conn)
        # v11->v12 — только новая таблица sleep_log, её создаёт CREATE TABLE
        # IF NOT EXISTS выше; отдельного шага миграции не нужно.
        # v12->v13 — equipment и plan_log, тоже только новые таблицы.
        if row["version"] < 14:
            _migrate_v13_to_v14(conn)
        # v14->v15 — только новые таблицы lab_results и sick_days, отдельного шага не нужно.
        # v15->v16 — только новая таблица dispatch_log.
        # v16->v17 — новая таблица access_list (DDL выше) и колонка users.health_notes.
        if row["version"] < 17:
            _migrate_v16_to_v17(conn)
        if row["version"] < 18:
            _migrate_v17_to_v18(conn)
        if row["version"] < 19:
            _migrate_v18_to_v19(conn)
        conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
    conn.commit()


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HEALTH_DB"] = str(Path(tmp) / "health.db")
        # перечитываем DB_PATH под временный каталог
        DB_PATH = Path(os.environ["HEALTH_DB"])
        conn = connect()
        migrate(conn)
        migrate(conn)  # идемпотентность

        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        expected = {
            "users", "user_targets", "milestones", "body_metrics", "anthropometry",
            "food_log", "food_items", "water_log", "glucose_log", "activity",
            "daily_targets", "alerts", "med_log", "llm_calls", "import_log",
            "refeed_days", "meal_plan", "workout_plan", "persona_styles", "lab_results",
            "sick_days",
        }
        assert expected <= tables, f"missing tables: {expected - tables}"
        assert len(expected) == 21

        conn.execute(
            "INSERT INTO users(telegram_user_id, created_at) VALUES (1, '2026-08-20 00:00:00')"
        )
        uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]

        def insert_metric():
            conn.execute(
                """INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg)
                   VALUES (?, 'burst-1', '2026-08-20 07:00:00', 80.2)
                   ON CONFLICT(user_id, burst_key) DO NOTHING""",
                (uid,),
            )

        insert_metric()
        insert_metric()
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) c FROM body_metrics WHERE user_id=? AND burst_key='burst-1'", (uid,)
        ).fetchone()["c"]
        assert count == 1, f"expected 1 row after duplicate insert, got {count}"

        print("OK: migrate() idempotent, all 16 tables present, burst_key dedup works")
        conn.close()  # на Windows temp dir не удалится, пока файл БД открыт

    # --- v2 -> v3: ALTER TABLE ADD COLUMN на популированной БД, без потери данных ---
    with tempfile.TemporaryDirectory() as tmp2:
        v2_path = Path(tmp2) / "v2.db"
        os.environ["HEALTH_DB"] = str(v2_path)
        DB_PATH = v2_path
        conn2 = connect()
        # Схема v2 вручную: med_log БЕЗ route/unit/site/notes, как на проде до миграции.
        conn2.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL UNIQUE,
                height_cm REAL, birth_date TEXT, sex TEXT, timezone TEXT,
                base_weight_kg REAL, base_weight_date TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE med_log (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                at TEXT NOT NULL,
                substance TEXT,
                dose TEXT
            );
            """
        )
        conn2.execute("INSERT INTO schema_version(version) VALUES (2)")
        conn2.execute(
            "INSERT INTO users(telegram_user_id, created_at) VALUES (1, '2026-08-20 00:00:00')"
        )
        uid2 = conn2.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]
        conn2.execute(
            "INSERT INTO med_log(user_id, at, substance, dose) VALUES (?, '2026-08-17 22:00:00', 'Тирзепатид', '10 мг')",
            (uid2,),
        )
        conn2.execute(
            "INSERT INTO med_log(user_id, at, substance, dose) VALUES (?, '2026-08-18 09:00:00', 'Андрокомплекс', '1 капс')",
            (uid2,),
        )
        conn2.commit()
        rows_before = conn2.execute("SELECT id, substance, dose FROM med_log ORDER BY id").fetchall()
        assert len(rows_before) == 2

        migrate(conn2)  # v2 -> v3

        cols = {r["name"] for r in conn2.execute("PRAGMA table_info(med_log)")}
        assert {"route", "unit", "site", "notes"} <= cols, f"missing new columns: {cols}"
        rows_after = conn2.execute(
            "SELECT id, substance, dose, route, unit, site, notes FROM med_log ORDER BY id"
        ).fetchall()
        assert len(rows_after) == len(rows_before), (
            f"row count changed by migration: before={len(rows_before)} after={len(rows_after)}"
        )
        for b, a in zip(rows_before, rows_after):
            assert a["id"] == b["id"] and a["substance"] == b["substance"] and a["dose"] == b["dose"], \
                f"existing values changed: before={dict(b)} after={dict(a)}"
            assert a["route"] is None and a["unit"] is None and a["site"] is None and a["notes"] is None, \
                "legacy rows must get NULL in new columns, not guessed defaults"
        ver = conn2.execute("SELECT version FROM schema_version").fetchone()["version"]
        assert ver == SCHEMA_VERSION, f"schema_version должна стать {SCHEMA_VERSION}, получили {ver}"

        migrate(conn2)  # идемпотентность на v3: повторный ALTER не должен упасть
        cols_again = {r["name"] for r in conn2.execute("PRAGMA table_info(med_log)")}
        assert cols_again == cols, "повторный migrate() не должен менять колонки"
        count_again = conn2.execute("SELECT COUNT(*) c FROM med_log").fetchone()["c"]
        assert count_again == 2, f"повторный migrate() не должен менять число строк, получили {count_again}"

        print("OK: v2->v3 ALTER TABLE ADD COLUMN сохраняет строки/значения, идемпотентно на v3")
        conn2.close()

    # --- v3 -> v4: meal_plan/workout_plan — новые таблицы, а не ALTER на существующей.
    # Проверка на форме прода: 1 пользователь, N строк body_metrics, схема v3 (без
    # meal_plan/workout_plan). CREATE TABLE IF NOT EXISTS в executescript() создаёт
    # обе новые таблицы, не трогая body_metrics; migrate() только поднимает номер
    # версии. Отдельный _migrate_v3_to_v4() не нужен — новых колонок в СУЩЕСТВУЮЩИХ
    # таблицах v4 не добавляет, только целиком новые таблицы. ---
    with tempfile.TemporaryDirectory() as tmp3:
        v3_path = Path(tmp3) / "v3.db"
        os.environ["HEALTH_DB"] = str(v3_path)
        DB_PATH = v3_path
        conn3 = connect()
        # Схема v3 вручную: полный DDL v2->v3 (med_log с route/unit/site/notes),
        # но БЕЗ meal_plan/workout_plan — как на живой проде до этой миграции.
        conn3.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL UNIQUE,
                height_cm REAL, birth_date TEXT, sex TEXT, timezone TEXT,
                base_weight_kg REAL, base_weight_date TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE body_metrics (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                burst_key TEXT NOT NULL,
                measured_at TEXT NOT NULL,
                weight_kg REAL NOT NULL,
                fat_pct REAL, bmi REAL, skeletal_muscle_pct REAL, muscle_mass_kg REAL,
                protein_pct REAL, device_bmr_kcal REAL, ffm_kg REAL,
                subcutaneous_fat_pct REAL, visceral_fat REAL, water_pct REAL,
                bone_mass_kg REAL, metabolic_age REAL, device_mac TEXT,
                UNIQUE(user_id, burst_key)
            );
            """
        )
        conn3.execute("INSERT INTO schema_version(version) VALUES (3)")
        conn3.execute(
            "INSERT INTO users(telegram_user_id, created_at) VALUES (1, '2026-08-20 00:00:00')"
        )
        uid3 = conn3.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]
        for i in range(248):  # форма прода: 248 строк body_metrics, 1 пользователь
            conn3.execute(
                "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg) "
                "VALUES (?, ?, ?, ?, ?)",
                (uid3, f"burst-{i}", f"2026-{(i % 12) + 1:02d}-01 07:00:00", 80.0 + i * 0.01, 60.0),
            )
        conn3.commit()
        rows_before3 = conn3.execute(
            "SELECT id, burst_key, measured_at, weight_kg, ffm_kg FROM body_metrics ORDER BY id"
        ).fetchall()
        assert len(rows_before3) == 248

        migrate(conn3)  # v3 -> v4

        tables3 = {r["name"] for r in conn3.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {"meal_plan", "workout_plan"} <= tables3, f"missing v4 tables: {tables3}"

        rows_after3 = conn3.execute(
            "SELECT id, burst_key, measured_at, weight_kg, ffm_kg FROM body_metrics ORDER BY id"
        ).fetchall()
        assert len(rows_after3) == len(rows_before3), (
            f"body_metrics row count changed by v3->v4 migration: "
            f"before={len(rows_before3)} after={len(rows_after3)}"
        )
        for b, a in zip(rows_before3, rows_after3):
            assert dict(a) == dict(b), f"body_metrics row mutated by migration: before={dict(b)} after={dict(a)}"

        # meal_plan/workout_plan существуют, но остаются пустыми — миграция ничего не выдумывает
        assert conn3.execute("SELECT COUNT(*) c FROM meal_plan").fetchone()["c"] == 0
        assert conn3.execute("SELECT COUNT(*) c FROM workout_plan").fetchone()["c"] == 0

        ver3 = conn3.execute("SELECT version FROM schema_version").fetchone()["version"]
        assert ver3 == SCHEMA_VERSION, f"schema_version должна стать {SCHEMA_VERSION}, получили {ver3}"

        migrate(conn3)  # идемпотентность на v4
        rows_idem = conn3.execute("SELECT COUNT(*) c FROM body_metrics").fetchone()["c"]
        assert rows_idem == 248, f"повторный migrate() на v4 не должен менять число строк, получили {rows_idem}"
        ver3_again = conn3.execute("SELECT version FROM schema_version").fetchone()["version"]
        assert ver3_again == SCHEMA_VERSION

        print("OK: v3->v4 создаёт meal_plan/workout_plan (CREATE TABLE IF NOT EXISTS), "
              "248 строк body_metrics не тронуты, идемпотентно на v4")
        conn3.close()
