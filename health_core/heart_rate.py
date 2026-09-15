"""Дневной пульс с часов (CONTEXT.md «Дневной пульс», «Максимальный пульс»):
один набор min/avg/max на календарный день, повторная запись дня заменяет
прежнюю. Отдельно — авто-поднятие личного максимума пульса: когда часы за
90 дней дважды показали дневной максимум выше текущего личного значения,
поднимаем users.hr_max_bpm сами (source='watch'), тест часами не перебиваем
и вниз не снижаем.
"""
import statistics
from datetime import datetime, timedelta

from health_core import config

_RANGES = {"hr_min": (30, 150), "hr_avg": (35, 200), "hr_max": (50, 240)}


def _validate_day(date_str, hr_min, hr_avg, hr_max, today: str) -> tuple[str, dict]:
    try:
        d = datetime.strptime(str(date_str), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError(f"date должен быть YYYY-MM-DD, получено {date_str!r}")
    if d.isoformat() > today:
        raise ValueError(f"{d.isoformat()} в будущем — дневной пульс пишется только за прошедший день")

    if hr_min is None and hr_avg is None and hr_max is None:
        raise ValueError("нужно хотя бы одно значение: hr_min, hr_avg или hr_max")

    vals = {}
    for name, raw in (("hr_min", hr_min), ("hr_avg", hr_avg), ("hr_max", hr_max)):
        if raw is None:
            vals[name] = None
            continue
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{name} должен быть целым числом, получено {raw!r}")
        lo, hi = _RANGES[name]
        if not (lo <= v <= hi):
            raise ValueError(f"{name}={v} вне допустимого диапазона {lo}-{hi}")
        vals[name] = v

    if vals["hr_min"] is not None and vals["hr_avg"] is not None and vals["hr_min"] > vals["hr_avg"]:
        raise ValueError(f"hr_min ({vals['hr_min']}) больше hr_avg ({vals['hr_avg']})")
    if vals["hr_avg"] is not None and vals["hr_max"] is not None and vals["hr_avg"] > vals["hr_max"]:
        raise ValueError(f"hr_avg ({vals['hr_avg']}) больше hr_max ({vals['hr_max']})")
    if vals["hr_min"] is not None and vals["hr_max"] is not None and vals["hr_min"] > vals["hr_max"]:
        raise ValueError(f"hr_min ({vals['hr_min']}) больше hr_max ({vals['hr_max']})")

    return d.isoformat(), vals


def _maybe_raise_max(conn, user_id: int) -> dict | None:
    """CONTEXT.md «Максимальный пульс»: значение с часов поднимается само, когда
    два разных дня за 90 дней дали дневной максимум выше текущего личного
    значения (или, без личного значения, выше оценки по формуле); тест часами
    не перебивается, вниз автоматически не снижается."""
    user = conn.execute(
        "SELECT hr_max_bpm, hr_max_source, birth_date FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if user is None or user["hr_max_source"] == "test":
        return None

    today = datetime.strptime(config.user_today(conn, user_id), "%Y-%m-%d").date()
    if user["hr_max_bpm"]:
        current = user["hr_max_bpm"]
    elif user["birth_date"]:
        from health_core.hr_zones import hr_max as _hr_max_formula
        birth = datetime.strptime(user["birth_date"][:10], "%Y-%m-%d").date()
        age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
        current = round(_hr_max_formula(age))
    else:
        return None  # нет ни личного максимума, ни даты рождения — не с чем сравнивать

    since = (today - timedelta(days=90)).isoformat()
    rows = conn.execute(
        "SELECT hr_max FROM daily_watch WHERE user_id=? AND date>=? AND hr_max IS NOT NULL "
        "ORDER BY hr_max DESC LIMIT 2",
        (user_id, since),
    ).fetchall()
    if len(rows) < 2:
        return None
    second_highest = rows[1]["hr_max"]
    if second_highest <= current:
        return None

    conn.execute("UPDATE users SET hr_max_bpm=?, hr_max_source='watch' WHERE id=?",
                 (second_highest, user_id))
    conn.commit()
    return {"old": current, "new": second_highest, "source": "watch"}


def save_days(conn, user_id: int, days: list, source: str = "watch") -> dict:
    """Батч дневного пульса — скриншоты/текст за месяц разом. Каждый день
    валидируется независимо: одна плохая дата не должна ронять остальные."""
    today = config.user_today(conn, user_id)
    now = config.user_now(conn, user_id).strftime("%Y-%m-%d %H:%M:%S")
    results = []
    for day in days:
        if not isinstance(day, dict):
            results.append({"date": day, "error": "элемент days должен быть объектом {date, hr_min, hr_avg, hr_max}"})
            continue
        date_str = day.get("date")
        try:
            d_iso, vals = _validate_day(date_str, day.get("hr_min"), day.get("hr_avg"), day.get("hr_max"), today)
        except ValueError as e:
            results.append({"date": date_str, "error": str(e)})
            continue
        existing = conn.execute(
            "SELECT id FROM daily_watch WHERE user_id=? AND date=?", (user_id, d_iso)
        ).fetchone()
        conn.execute(
            "INSERT INTO daily_watch(user_id, date, hr_min, hr_avg, hr_max, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, date) DO UPDATE SET "
            "hr_min=excluded.hr_min, hr_avg=excluded.hr_avg, hr_max=excluded.hr_max, "
            "source=excluded.source, created_at=excluded.created_at",
            (user_id, d_iso, vals["hr_min"], vals["hr_avg"], vals["hr_max"], source, now),
        )
        results.append({
            "date": d_iso, "action": "replaced" if existing else "added",
            "hr_min": vals["hr_min"], "hr_avg": vals["hr_avg"], "hr_max": vals["hr_max"],
        })
    conn.commit()

    max_hr_update = _maybe_raise_max(conn, user_id)
    return {"days": results, "max_hr_update": max_hr_update}


def list_days(conn, user_id: int, limit: int = 30, since_days: int | None = None) -> list[dict]:
    q = "SELECT date, hr_min, hr_avg, hr_max, source FROM daily_watch WHERE user_id=?"
    args: list = [user_id]
    if since_days is not None:
        since = (datetime.strptime(config.user_today(conn, user_id), "%Y-%m-%d").date()
                 - timedelta(days=since_days)).isoformat()
        q += " AND date>=?"
        args.append(since)
    q += " ORDER BY date DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def delete_day(conn, user_id: int, date: str | None) -> str | None:
    """Удаляет запись за дату; без даты — последнюю. None, если удалять нечего."""
    if date is None:
        row = conn.execute(
            "SELECT date FROM daily_watch WHERE user_id=? ORDER BY date DESC LIMIT 1", (user_id,)
        ).fetchone()
        if row is None:
            return None
        date = row["date"]
    cur = conn.execute("DELETE FROM daily_watch WHERE user_id=? AND date=?", (user_id, date))
    conn.commit()
    return date if cur.rowcount else None


def trend_block(conn, user_id: int) -> dict | None:
    """Медиана hr_min/hr_avg за последние 28 дней против предыдущих 28 — для
    get_trends и пакета консилиума. None, если данных за последние 28 дней нет."""
    today = datetime.strptime(config.user_today(conn, user_id), "%Y-%m-%d").date()
    start_recent = (today - timedelta(days=27)).isoformat()
    start_prev = (today - timedelta(days=55)).isoformat()

    recent = conn.execute(
        "SELECT hr_min, hr_avg FROM daily_watch WHERE user_id=? AND date>=?",
        (user_id, start_recent),
    ).fetchall()
    if not recent:
        return None
    prev = conn.execute(
        "SELECT hr_min, hr_avg FROM daily_watch WHERE user_id=? AND date>=? AND date<?",
        (user_id, start_prev, start_recent),
    ).fetchall()

    def _median(rows, field):
        vals = [r[field] for r in rows if r[field] is not None]
        return statistics.median(vals) if vals else None

    hr_min_med = _median(recent, "hr_min")
    hr_avg_med = _median(recent, "hr_avg")
    prev_hr_min_med = _median(prev, "hr_min")
    prev_hr_avg_med = _median(prev, "hr_avg")

    return {
        "hr_min_median": hr_min_med,
        "hr_avg_median": hr_avg_med,
        "hr_min_delta": (hr_min_med - prev_hr_min_med) if hr_min_med is not None and prev_hr_min_med is not None else None,
        "hr_avg_delta": (hr_avg_med - prev_hr_avg_med) if hr_avg_med is not None and prev_hr_avg_med is not None else None,
        "coverage_days": len(recent),
    }


if __name__ == "__main__":
    import sqlite3
    import health_core.db as db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.DDL)
    conn.execute(
        "INSERT INTO users(telegram_user_id, birth_date, created_at) VALUES (1, '1992-08-09', '2026-08-20 00:00:00')"
    )
    uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]

    # --- upsert: повторная запись дня заменяет, не плодит вторую строку ---
    r1 = save_days(conn, uid, [{"date": "2026-09-01", "hr_min": 55, "hr_avg": 70, "hr_max": 120}])
    assert r1["days"][0]["action"] == "added", r1
    r2 = save_days(conn, uid, [{"date": "2026-09-01", "hr_min": 50, "hr_avg": 68, "hr_max": 118}])
    assert r2["days"][0]["action"] == "replaced", r2
    n = conn.execute("SELECT COUNT(*) c FROM daily_watch WHERE user_id=?", (uid,)).fetchone()["c"]
    assert n == 1, f"upsert должен оставить одну строку, получили {n}"
    row = conn.execute("SELECT hr_min FROM daily_watch WHERE user_id=? AND date='2026-09-01'", (uid,)).fetchone()
    assert row["hr_min"] == 50, dict(row)

    # --- валидация: min > max отклоняется, остальные дни батча не страдают ---
    r3 = save_days(conn, uid, [
        {"date": "2026-09-02", "hr_min": 100, "hr_max": 90},
        {"date": "2026-09-03", "hr_avg": 65},
    ])
    assert "error" in r3["days"][0], r3
    assert r3["days"][1].get("action") == "added", r3

    # --- будущая дата отклоняется ---
    future = (datetime.strptime(config.user_today(conn, uid), "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d")
    r4 = save_days(conn, uid, [{"date": future, "hr_avg": 70}])
    assert "error" in r4["days"][0] and "будущем" in r4["days"][0]["error"], r4

    # --- авто-поднятие максимума: один всплеск ничего не делает ---
    conn.execute("DELETE FROM daily_watch WHERE user_id=?", (uid,))
    conn.commit()
    r5 = save_days(conn, uid, [{"date": "2026-08-25", "hr_max": 200}])
    assert r5["max_hr_update"] is None, "один день выше формулы не должен поднимать максимум"

    # --- второй день выше текущего — поднимает, source='watch' ---
    r6 = save_days(conn, uid, [{"date": "2026-08-26", "hr_max": 195}])
    assert r6["max_hr_update"] is not None, r6
    assert r6["max_hr_update"]["new"] == 195, r6
    urow = conn.execute("SELECT hr_max_bpm, hr_max_source FROM users WHERE id=?", (uid,)).fetchone()
    assert urow["hr_max_bpm"] == 195 and urow["hr_max_source"] == "watch", dict(urow)

    # --- источник 'test' часами не перебивается ---
    conn.execute("UPDATE users SET hr_max_bpm=210, hr_max_source='test' WHERE id=?", (uid,))
    conn.commit()
    save_days(conn, uid, [{"date": "2026-08-27", "hr_max": 220}])
    save_days(conn, uid, [{"date": "2026-08-28", "hr_max": 221}])
    urow2 = conn.execute("SELECT hr_max_bpm, hr_max_source FROM users WHERE id=?", (uid,)).fetchone()
    assert urow2["hr_max_bpm"] == 210 and urow2["hr_max_source"] == "test", "тест не должен перебиваться часами"

    # --- вниз автоматически не снижается (чистая история — без старых высоких дней) ---
    conn.execute("DELETE FROM daily_watch WHERE user_id=?", (uid,))
    conn.execute("UPDATE users SET hr_max_bpm=210, hr_max_source='watch' WHERE id=?", (uid,))
    conn.commit()
    save_days(conn, uid, [{"date": "2026-08-29", "hr_max": 130}])
    save_days(conn, uid, [{"date": "2026-08-30", "hr_max": 131}])
    urow3 = conn.execute("SELECT hr_max_bpm FROM users WHERE id=?", (uid,)).fetchone()
    assert urow3["hr_max_bpm"] == 210, "максимум не должен снижаться от низких дней"

    # --- list/delete ---
    entries = list_days(conn, uid, limit=100)
    assert len(entries) == 2, entries
    last_date = entries[0]["date"]
    deleted = delete_day(conn, uid, None)
    assert deleted == last_date
    assert delete_day(conn, uid, "1999-01-01") is None

    # --- trend_block: медиана за 28д и её отсутствие без данных ---
    conn2 = sqlite3.connect(":memory:")
    conn2.row_factory = sqlite3.Row
    conn2.executescript(db.DDL)
    conn2.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (2, '2026-08-20 00:00:00')")
    uid2 = conn2.execute("SELECT id FROM users WHERE telegram_user_id=2").fetchone()["id"]
    assert trend_block(conn2, uid2) is None, "без данных блок должен отсутствовать"
    today2 = datetime.strptime(config.user_today(conn2, uid2), "%Y-%m-%d").date()
    for days_ago, hr_min, hr_avg in ((5, 55, 70), (10, 57, 72), (40, 60, 78), (45, 62, 80)):
        d = (today2 - timedelta(days=days_ago)).isoformat()
        save_days(conn2, uid2, [{"date": d, "hr_min": hr_min, "hr_avg": hr_avg}])
    tb = trend_block(conn2, uid2)
    assert tb["coverage_days"] == 2, tb
    assert tb["hr_min_median"] == 56 and tb["hr_avg_median"] == 71, tb
    assert tb["hr_min_delta"] == 56 - 61 and tb["hr_avg_delta"] == 71 - 79, tb

    conn.close()
    conn2.close()
    print("OK: heart_rate.py — upsert, валидация (диапазоны/min<=avg<=max/будущее), "
          "авто-поднятие максимума (два дня, не test, не вниз), list/delete, trend_block")
