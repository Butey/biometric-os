"""Хронопитание и хронотерапия: окна приёмов, поздняя нагрузка перед сном, кофеиновое окно."""
import json
import sqlite3
import statistics
from datetime import datetime, timedelta

_MEAL_NAMES = ("breakfast", "lunch", "dinner")
_DEFAULT_WINDOWS = {
    "breakfast": {"start": "07:00", "end": "11:00"},
    "lunch": {"start": "12:00", "end": "16:00"},
    "dinner": {"start": "18:00", "end": "22:00"},
}


def meal_windows(conn: sqlite3.Connection, user_id: int) -> dict:
    """Окна приёмов пищи (CONTEXT.md «Окно приёма пищи») этого человека.

    Умолчание — config.yaml meals.*, частично переопределяемое users.meal_windows
    (JSON с теми же ключами breakfast/lunch/dinner, {"start": "HH:MM", "end": "HH:MM"}).
    Одно определение действует и для записи (meal_slot), и для статус-бара/напоминаний.
    """
    from health_core.config import load

    cfg = load().get("meals", {})
    windows = {name: dict(cfg.get(name, _DEFAULT_WINDOWS[name])) for name in _MEAL_NAMES}

    row = conn.execute("SELECT meal_windows FROM users WHERE id=?", (user_id,)).fetchone()
    raw = row["meal_windows"] if row else None
    if raw:
        try:
            override = json.loads(raw)
        except (ValueError, TypeError):
            override = {}
        for name in _MEAL_NAMES:
            w = override.get(name)
            if isinstance(w, dict):
                windows[name] = {**windows[name], **w}
    return windows


def meal_slot(conn: sqlite3.Connection, user_id: int, eaten_at: datetime, named: str | None = None) -> str:
    """Приём пищи (CONTEXT.md «Приём пищи»).

    Прямое слово человека (named) побеждает всегда. Иначе: eaten_at вне всех
    окон — snack; внутри окна X — X, если только у этого пользователя в этот
    день ещё нет приёма X, начавшегося (по самой ранней записи) не позже, чем
    snack_after_main_minutes назад — тогда тоже snack (второй, поздний "приём"
    в то же окно не основной). Время — локальное время пользователя (eaten_at
    вызывающий обязан передать уже в этом поясе, см. health_core/config.py).
    """
    if named in ("breakfast", "lunch", "dinner", "snack"):
        return named

    windows = meal_windows(conn, user_id)
    t = eaten_at.strftime("%H:%M")
    date_str = eaten_at.date().isoformat()

    for name in _MEAL_NAMES:
        w = windows[name]
        if w["start"] > w["end"]:
            in_window = t >= w["start"] or t <= w["end"]
        else:
            in_window = w["start"] <= t <= w["end"]
        if not in_window:
            continue
        row = conn.execute(
            "SELECT MIN(eaten_at) AS first FROM food_log WHERE user_id=? AND date(eaten_at)=? AND meal_slot=?",
            (user_id, date_str, name),
        ).fetchone()
        if row and row["first"]:
            from health_core.config import load

            after_min = load().get("meals", {}).get("snack_after_main_minutes", 30)
            first_dt = datetime.strptime(row["first"], "%Y-%m-%d %H:%M:%S")
            if (eaten_at - first_dt).total_seconds() / 60 > after_min:
                return "snack"
        return name

    return "snack"


def eating_window(conn: sqlite3.Connection, user_id: int, date: str) -> dict | None:
    """Окно питания за день: время первого и последнего приёма + продолжительность в часах.

    Требует минимум 2 записи food_log за дату, иначе None.
    Возвращает {"first": "HH:MM", "last": "HH:MM", "hours": float}.
    """
    rows = conn.execute(
        "SELECT eaten_at FROM food_log WHERE user_id=? AND date(eaten_at)=? ORDER BY eaten_at",
        (user_id, date),
    ).fetchall()

    if len(rows) < 2:
        return None

    first_ts = rows[0]["eaten_at"]
    last_ts = rows[-1]["eaten_at"]

    first_dt = datetime.strptime(first_ts, "%Y-%m-%d %H:%M:%S")
    last_dt = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")

    first_str = first_dt.strftime("%H:%M")
    last_str = last_dt.strftime("%H:%M")

    hours = (last_dt - first_dt).total_seconds() / 3600
    hours_rounded = round(hours, 1)

    return {
        "first": first_str,
        "last": last_str,
        "hours": hours_rounded,
    }


def late_load(conn: sqlite3.Connection, user_id: int, date: str) -> dict | None:
    """Поздняя нагрузка: доля ккал дня за 3 часа до сна.

    date — дата дня логирования еды (YYYY-MM-DD).
    Смотрит на sleep_log для night_date = date + 1 день.
    Если bedtime есть и парсится, считает ккал:
    - total: все ккал дня с 00:00:00 до момента отбоя
    - late: ккал за late_load_hours (3 часа) перед отбоем

    Возвращает {"share": доля (0.0–1.0), "kcal": поздних ккал, "bedtime": "HH:MM"}
    или None, если нет ночи, нет bedtime, или total == 0.
    """
    from health_core.config import load

    cfg = load().get("chrono", {})
    late_load_hours = cfg.get("late_load_hours", 3)

    # Ночь = дата пробуждения, то есть дата + 1 день
    next_date = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    sleep_row = conn.execute(
        "SELECT bedtime FROM sleep_log WHERE user_id=? AND night_date=?",
        (user_id, next_date),
    ).fetchone()

    if sleep_row is None or sleep_row["bedtime"] is None:
        return None

    bedtime_str = sleep_row["bedtime"]
    try:
        bed_hour, bed_min = map(int, bedtime_str.split(":"))
        # hour >= 12 — вечер дня date; hour < 12 — уже после полуночи, день next_date
        bed_date = date if bed_hour >= 12 else next_date
        bedtime_dt = datetime.strptime(bed_date, "%Y-%m-%d").replace(hour=bed_hour, minute=bed_min)
    except ValueError:
        return None

    # Окно поздней нагрузки: [bedtime - late_load_hours, bedtime)
    late_start_dt = bedtime_dt - timedelta(hours=late_load_hours)

    # total: все ккал с начала дня (date 00:00) до bedtime
    day_start_dt = datetime.strptime(f"{date} 00:00:00", "%Y-%m-%d %H:%M:%S")

    total_row = conn.execute(
        "SELECT COALESCE(SUM(fi.kcal), 0) kcal FROM food_log fl "
        "JOIN food_items fi ON fi.food_log_id=fl.id "
        "WHERE fl.user_id=? AND fl.eaten_at>=? AND fl.eaten_at<?",
        (user_id, day_start_dt.strftime("%Y-%m-%d %H:%M:%S"), bedtime_dt.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()

    total_kcal = total_row["kcal"] if total_row else 0.0

    if total_kcal == 0:
        return None

    # late: ккал в окне [late_start, bedtime)
    late_row = conn.execute(
        "SELECT COALESCE(SUM(fi.kcal), 0) kcal FROM food_log fl "
        "JOIN food_items fi ON fi.food_log_id=fl.id "
        "WHERE fl.user_id=? AND fl.eaten_at>=? AND fl.eaten_at<?",
        (user_id, late_start_dt.strftime("%Y-%m-%d %H:%M:%S"), bedtime_dt.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()

    late_kcal = late_row["kcal"] if late_row else 0.0

    share = round(late_kcal / total_kcal, 2)

    return {
        "share": share,
        "kcal": int(late_kcal),
        "bedtime": bedtime_str,
    }


def caffeine_cutoff(conn: sqlite3.Connection, user_id: int) -> str | None:
    """Час отсечки кофеина на основе медианы поздних отбоев.

    Берёт последние bedtime_nights (14) строк sleep_log с non-NULL bedtime (ORDER BY DESC).
    Конвертирует каждый в минуты от полуночи:
    - if hour < 12: это after-midnight, добавляем 1440
    - else: это предыдущий день (PM/evening), берём как есть

    Вычисляет медиану. Отсечка = медиана - half_life_h × 60 × log2(dose / residual).
    Возвращает "HH:MM" (% 1440 для циклизации на день).

    None, если нет bedtimes.
    """
    import math
    from health_core.config import load

    cfg = load().get("chrono", {})
    bedtime_nights = cfg.get("bedtime_nights", 14)
    half_life_h = cfg.get("caffeine_half_life_h", 5.5)
    dose_mg = cfg.get("caffeine_dose_mg", 100)
    residual_mg = cfg.get("caffeine_residual_mg", 50)

    rows = conn.execute(
        "SELECT bedtime FROM sleep_log WHERE user_id=? AND bedtime IS NOT NULL "
        "ORDER BY night_date DESC LIMIT ?",
        (user_id, bedtime_nights),
    ).fetchall()

    if not rows:
        return None

    bedtimes_min = []
    for row in rows:
        bedtime_str = row["bedtime"]
        try:
            hour, minute = map(int, bedtime_str.split(":"))
        except (ValueError, IndexError):
            continue

        min_from_midnight = hour * 60 + minute
        # if hour < 12, это after-midnight, добавляем 1440
        if hour < 12:
            min_from_midnight += 1440

        bedtimes_min.append(min_from_midnight)

    if not bedtimes_min:
        return None

    median_min = statistics.median(bedtimes_min)

    # cutoff_min = median - half_life_h * 60 * log2(dose / residual)
    log2_ratio = math.log2(dose_mg / residual_mg)
    decay_min = half_life_h * 60 * log2_ratio
    cutoff_min = median_min - decay_min

    # Циклизация на день (1440 минут)
    cutoff_min_cycled = cutoff_min % 1440

    # Преобразование в HH:MM
    hour = int(cutoff_min_cycled // 60)
    minute = int(cutoff_min_cycled % 60)

    return f"{hour:02d}:{minute:02d}"


if __name__ == "__main__":
    import os
    import sys
    import tempfile
    from pathlib import Path

    sys.stdout.reconfigure(encoding="utf-8")

    # Тесты, как задано в требованиях
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HEALTH_DB"] = str(Path(tmp) / "health.db")

        import health_core.db as db
        db.DB_PATH = Path(os.environ["HEALTH_DB"])

        conn = db.connect()
        db.migrate(conn)

        # Insert a test user
        conn.execute(
            "INSERT INTO users(telegram_user_id, created_at) VALUES (?, ?)",
            (1, "2026-09-01 00:00:00"),
        )
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]

        # Test 1: eating_window with meals at 09:40 and 20:04 → hours 10.4; one meal → None.
        conn.execute(
            "INSERT INTO food_log(user_id, eaten_at) VALUES (?, ?)",
            (uid, "2026-09-10 09:40:00"),
        )
        fl_id1 = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO food_items(food_log_id, kcal) VALUES (?, ?)",
            (fl_id1, 500),
        )

        conn.execute(
            "INSERT INTO food_log(user_id, eaten_at) VALUES (?, ?)",
            (uid, "2026-09-10 20:04:00"),
        )
        fl_id2 = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO food_items(food_log_id, kcal) VALUES (?, ?)",
            (fl_id2, 500),
        )
        conn.commit()

        window = eating_window(conn, uid, "2026-09-10")
        assert window is not None, "eating_window should return dict for 2 meals"
        assert window["first"] == "09:40", f"first should be 09:40, got {window['first']}"
        assert window["last"] == "20:04", f"last should be 20:04, got {window['last']}"
        assert window["hours"] == 10.4, f"hours should be 10.4, got {window['hours']}"
        print("OK: eating_window with 2 meals at 09:40 and 20:04 → hours 10.4")

        # One meal → None
        window_one = eating_window(conn, uid, "2026-09-09")
        assert window_one is None, "eating_window should return None for < 2 meals"
        print("OK: eating_window returns None for < 2 meals")

        # Test 2: late_load - day 2026-09-10 food 600 kcal at 13:00 and 400 kcal at 21:30,
        # sleep night_date 2026-09-11 bedtime '23:30' → share 0.4, kcal 400.
        # Clear food from previous test
        conn.execute("DELETE FROM food_items")
        conn.execute("DELETE FROM food_log WHERE user_id=? AND date(eaten_at)='2026-09-10'", (uid,))

        conn.execute(
            "INSERT INTO food_log(user_id, eaten_at) VALUES (?, ?)",
            (uid, "2026-09-10 13:00:00"),
        )
        fl_id3 = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO food_items(food_log_id, kcal) VALUES (?, ?)",
            (fl_id3, 600),
        )

        conn.execute(
            "INSERT INTO food_log(user_id, eaten_at) VALUES (?, ?)",
            (uid, "2026-09-10 21:30:00"),
        )
        fl_id4 = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO food_items(food_log_id, kcal) VALUES (?, ?)",
            (fl_id4, 400),
        )

        conn.execute(
            "INSERT INTO sleep_log(user_id, night_date, bedtime) VALUES (?, ?, ?)",
            (uid, "2026-09-11", "23:30"),
        )
        conn.commit()

        late = late_load(conn, uid, "2026-09-10")
        assert late is not None, "late_load should return dict"
        assert late["share"] == 0.4, f"share should be 0.4, got {late['share']}"
        assert late["kcal"] == 400, f"kcal should be 400, got {late['kcal']}"
        print("OK: late_load day 2026-09-10 with bedtime 23:30 → share 0.4, kcal 400")

        # Test 3: late_load after-midnight bedtime
        # same day, bedtime '00:30' on night_date 2026-09-11, plus 200 kcal at 2026-09-11 00:10
        # window 21:30..00:30 → late 600 (400 from 21:30 + 200 from 00:10) of total 1200 → share 0.5
        conn.execute("DELETE FROM food_items")
        conn.execute("DELETE FROM food_log WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM sleep_log WHERE user_id=?", (uid,))

        conn.execute(
            "INSERT INTO food_log(user_id, eaten_at) VALUES (?, ?)",
            (uid, "2026-09-10 13:00:00"),
        )
        fl_id5 = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO food_items(food_log_id, kcal) VALUES (?, ?)",
            (fl_id5, 600),
        )

        conn.execute(
            "INSERT INTO food_log(user_id, eaten_at) VALUES (?, ?)",
            (uid, "2026-09-10 21:30:00"),
        )
        fl_id6 = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO food_items(food_log_id, kcal) VALUES (?, ?)",
            (fl_id6, 400),
        )

        conn.execute(
            "INSERT INTO food_log(user_id, eaten_at) VALUES (?, ?)",
            (uid, "2026-09-11 00:10:00"),
        )
        fl_id7 = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO food_items(food_log_id, kcal) VALUES (?, ?)",
            (fl_id7, 200),
        )

        conn.execute(
            "INSERT INTO sleep_log(user_id, night_date, bedtime) VALUES (?, ?, ?)",
            (uid, "2026-09-11", "00:30"),
        )
        conn.commit()

        late_am = late_load(conn, uid, "2026-09-10")
        assert late_am is not None, "late_load with after-midnight bedtime should return dict"
        assert late_am["share"] == 0.5, f"share should be 0.5, got {late_am['share']}"
        assert late_am["kcal"] == 600, f"kcal should be 600 (400+200), got {late_am['kcal']}"
        print("OK: late_load after-midnight bedtime 00:30 → share 0.5, kcal 600")

        # Test 4: no bedtime → None
        conn.execute("DELETE FROM sleep_log WHERE user_id=?", (uid,))
        late_none = late_load(conn, uid, "2026-09-10")
        assert late_none is None, "late_load without bedtime should return None"
        print("OK: late_load returns None without bedtime")

        # Test 5: caffeine_cutoff with bedtimes '23:00', '23:30', '00:30'
        # median 23:30 → with defaults 5.5 h × log2(2) → '18:00'
        conn.execute("DELETE FROM sleep_log WHERE user_id=?", (uid,))

        conn.execute(
            "INSERT INTO sleep_log(user_id, night_date, bedtime) VALUES (?, ?, ?)",
            (uid, "2026-09-10", "23:00"),
        )
        conn.execute(
            "INSERT INTO sleep_log(user_id, night_date, bedtime) VALUES (?, ?, ?)",
            (uid, "2026-09-11", "23:30"),
        )
        conn.execute(
            "INSERT INTO sleep_log(user_id, night_date, bedtime) VALUES (?, ?, ?)",
            (uid, "2026-09-12", "00:30"),
        )
        conn.commit()

        cutoff = caffeine_cutoff(conn, uid)
        assert cutoff is not None, "caffeine_cutoff should return time string"
        assert cutoff == "18:00", f"cutoff should be 18:00, got {cutoff}"
        print("OK: caffeine_cutoff with bedtimes 23:00, 23:30, 00:30 → 18:00")

        # Test 6: no bedtimes → None
        conn.execute("DELETE FROM sleep_log WHERE user_id=?", (uid,))
        cutoff_none = caffeine_cutoff(conn, uid)
        assert cutoff_none is None, "caffeine_cutoff without bedtimes should return None"
        print("OK: caffeine_cutoff returns None without bedtimes")

        conn.close()

    # Test 7: meal_slot / meal_windows (§ Окно приёма пищи).
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HEALTH_DB"] = str(Path(tmp) / "health2.db")
        db.DB_PATH = Path(os.environ["HEALTH_DB"])
        conn = db.connect()
        db.migrate(conn)
        conn.execute(
            "INSERT INTO users(telegram_user_id, created_at) VALUES (?, ?)",
            (2, "2026-09-01 00:00:00"),
        )
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=2").fetchone()["id"]

        # Прямое слово побеждает, даже вне окна и даже как snack в окне основного приёма.
        assert meal_slot(conn, uid, datetime(2026, 9, 10, 23, 0), named="dinner") == "dinner", \
            "прямое слово должно побеждать вне окна"
        assert meal_slot(conn, uid, datetime(2026, 9, 10, 7, 30), named="snack") == "snack", \
            "прямое слово 'snack' должно побеждать внутри окна основного приёма"

        # 07:30 без слова, окно завтрака свободно -> breakfast.
        d = "2026-09-10"
        t1 = datetime(2026, 9, 10, 7, 30)
        slot1 = meal_slot(conn, uid, t1)
        assert slot1 == "breakfast", f"07:30 без слова должно быть breakfast, получили {slot1}"
        conn.execute("INSERT INTO food_log(user_id, eaten_at, meal_slot) VALUES (?, ?, ?)",
                     (uid, f"{d} 07:30:00", slot1))
        conn.commit()

        # Вторая запись 07:50 (20 минут от начала завтрака) -> всё ещё breakfast.
        t2 = datetime(2026, 9, 10, 7, 50)
        slot2 = meal_slot(conn, uid, t2)
        assert slot2 == "breakfast", f"07:50 (20 мин) должно быть breakfast, получили {slot2}"

        # Запись 08:15 (45 минут от начала завтрака) -> перекус.
        t3 = datetime(2026, 9, 10, 8, 15)
        slot3 = meal_slot(conn, uid, t3)
        assert slot3 == "snack", f"08:15 (45 мин) должно быть snack, получили {slot3}"

        # 11:30 вне всех окон -> snack.
        slot4 = meal_slot(conn, uid, datetime(2026, 9, 10, 11, 30))
        assert slot4 == "snack", f"11:30 вне окон должно быть snack, получили {slot4}"

        # 23:00 вне всех окон -> snack.
        slot5 = meal_slot(conn, uid, datetime(2026, 9, 10, 23, 0))
        assert slot5 == "snack", f"23:00 вне окон должно быть snack, получили {slot5}"

        print("OK: meal_slot без прямого слова — окна и правило 30 минут")

        # Личные окна переопределяют умолчание: завтрак у этого пользователя 05:00-06:00,
        # значит 07:30 больше не в его окне завтрака -> snack.
        conn.execute("UPDATE users SET meal_windows=? WHERE id=?",
                     (json.dumps({"breakfast": {"start": "05:00", "end": "06:00"}}), uid))
        conn.commit()
        w = meal_windows(conn, uid)
        assert w["breakfast"] == {"start": "05:00", "end": "06:00"}, \
            f"личное окно завтрака должно переопределить умолчание, получили {w['breakfast']}"
        assert w["lunch"] == {"start": "12:00", "end": "16:00"}, \
            "частичное переопределение не должно трогать lunch/dinner"
        slot6 = meal_slot(conn, uid, datetime(2026, 9, 11, 7, 30))
        assert slot6 == "snack", f"07:30 вне личного окна завтрака должно быть snack, получили {slot6}"
        print("OK: личные окна пользователя переопределяют умолчание (частично)")

        conn.close()

    print("\nВсе проверки chrono.py прошли.")
