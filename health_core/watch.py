"""День с часов (CONTEXT.md «День с часов», «Дневной пульс», «Максимальный
пульс», «Цель шагов»): один набор показателей на календарный день — пульс
(min/avg/max), шаги, калории активности, стресс, HRV. Запись дня заменяет
прежнюю ПО КАЖДОМУ ПОКАЗАТЕЛЮ ОТДЕЛЬНО: батч с одними шагами не стирает
пульс того же дня, записанный раньше, — только сам показатель, если он в
батче пришёл снова. Отдельно — авто-поднятие личного максимума пульса: когда
часы за 90 дней дважды показали дневной максимум выше текущего личного
значения, поднимаем users.hr_max_bpm сами (source='watch'), тест часами не
перебиваем и вниз не снижаем.
"""
import statistics
from datetime import datetime, timedelta

from health_core import config

_RANGES = {
    "hr_min": (30, 150), "hr_avg": (35, 200), "hr_max": (50, 240),
    "steps": (0, 100000), "active_kcal": (0, 5000),
    "stress_avg": (0, 100), "hrv_ms": (5, 300),
}


def _validate_day(date_str, raw: dict, today: str) -> tuple[str, dict]:
    try:
        d = datetime.strptime(str(date_str), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError(f"date должен быть YYYY-MM-DD, получено {date_str!r}")
    if d.isoformat() > today:
        raise ValueError(f"{d.isoformat()} в будущем — день с часов пишется только за прошедший день")

    vals = {}
    for name, (lo, hi) in _RANGES.items():
        raw_v = raw.get(name)
        # Модель (GPT) заполняет непереданные числовые поля нулём. Там, где 0
        # заведомо не бывает реальным значением (нижняя граница диапазона >0),
        # это неотличимо от "не прислали" — не отклоняем всю запись дня.
        if raw_v is None or (raw_v == 0 and lo > 0):
            vals[name] = None
            continue
        try:
            v = int(raw_v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} должен быть целым числом, получено {raw_v!r}")
        if not (lo <= v <= hi):
            raise ValueError(f"{name}={v} вне допустимого диапазона {lo}-{hi}")
        vals[name] = v

    if all(v is None for v in vals.values()):
        raise ValueError(
            "нужно хотя бы одно значение: hr_min, hr_avg, hr_max, steps, active_kcal, stress_avg или hrv_ms"
        )

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
    """Батч дня с часов — скриншоты/текст за месяц разом. Каждый день
    валидируется независимо: одна плохая дата не должна ронять остальные.
    Upsert ПО КАЖДОМУ ПОКАЗАТЕЛЮ: показатель, не пришедший в этом батче,
    сохраняет прежнее значение (COALESCE с excluded), пришедший — заменяет."""
    today = config.user_today(conn, user_id)
    now = config.user_now(conn, user_id).strftime("%Y-%m-%d %H:%M:%S")
    results = []
    for day in days:
        if not isinstance(day, dict):
            results.append({"date": day, "error": "элемент days должен быть объектом {date, ...}"})
            continue
        date_str = day.get("date")
        try:
            d_iso, vals = _validate_day(date_str, day, today)
        except ValueError as e:
            results.append({"date": date_str, "error": str(e)})
            continue
        existing = conn.execute(
            "SELECT id FROM daily_watch WHERE user_id=? AND date=?", (user_id, d_iso)
        ).fetchone()
        conn.execute(
            "INSERT INTO daily_watch(user_id, date, hr_min, hr_avg, hr_max, steps, active_kcal, "
            "stress_avg, hrv_ms, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, date) DO UPDATE SET "
            "hr_min=COALESCE(excluded.hr_min, daily_watch.hr_min), "
            "hr_avg=COALESCE(excluded.hr_avg, daily_watch.hr_avg), "
            "hr_max=COALESCE(excluded.hr_max, daily_watch.hr_max), "
            "steps=COALESCE(excluded.steps, daily_watch.steps), "
            "active_kcal=COALESCE(excluded.active_kcal, daily_watch.active_kcal), "
            "stress_avg=COALESCE(excluded.stress_avg, daily_watch.stress_avg), "
            "hrv_ms=COALESCE(excluded.hrv_ms, daily_watch.hrv_ms), "
            "source=excluded.source, created_at=excluded.created_at",
            (user_id, d_iso, vals["hr_min"], vals["hr_avg"], vals["hr_max"], vals["steps"],
             vals["active_kcal"], vals["stress_avg"], vals["hrv_ms"], source, now),
        )
        results.append({
            "date": d_iso, "action": "replaced" if existing else "added",
            **vals,
        })
    conn.commit()

    max_hr_update = _maybe_raise_max(conn, user_id)
    return {"days": results, "max_hr_update": max_hr_update}


def list_days(conn, user_id: int, limit: int = 30, since_days: int | None = None) -> list[dict]:
    q = ("SELECT date, hr_min, hr_avg, hr_max, steps, active_kcal, stress_avg, hrv_ms, source "
         "FROM daily_watch WHERE user_id=?")
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


def steps_on(conn, user_id: int, date: str) -> int | None:
    row = conn.execute("SELECT steps FROM daily_watch WHERE user_id=? AND date=?", (user_id, date)).fetchone()
    return row["steps"] if row else None


def step_goal(conn, user_id: int, date: str) -> int | None:
    """CONTEXT.md «Цель шагов»: медиана шагов за 28 дней, оканчивающихся
    ближайшим понедельником не позже date (включительно), + 1000, потолок
    10000. None, если дней с шагами в этом окне меньше 7 (цель пересчитывается
    по понедельникам — опора умышленно последний понедельник, а не сам date)."""
    d = datetime.strptime(str(date), "%Y-%m-%d").date()
    monday = d - timedelta(days=d.weekday())
    start = monday - timedelta(days=27)
    rows = conn.execute(
        "SELECT steps FROM daily_watch WHERE user_id=? AND date>=? AND date<=? AND steps IS NOT NULL",
        (user_id, start.isoformat(), monday.isoformat()),
    ).fetchall()
    if len(rows) < 7:
        return None
    med = statistics.median(r["steps"] for r in rows)
    return min(10000, round(med) + 1000)


def steps_drop(conn, user_id: int) -> dict | None:
    """Медиана шагов за последние 28 дней против предыдущих 28 — падение
    активности. None, если в любом из окон меньше 7 дней с шагами, или падение
    ниже guards.steps_drop_pct."""
    today = datetime.strptime(config.user_today(conn, user_id), "%Y-%m-%d").date()
    start_recent = (today - timedelta(days=27)).isoformat()
    start_prev = (today - timedelta(days=55)).isoformat()
    recent = [r["steps"] for r in conn.execute(
        "SELECT steps FROM daily_watch WHERE user_id=? AND date>=? AND steps IS NOT NULL",
        (user_id, start_recent),
    ).fetchall()]
    previous = [r["steps"] for r in conn.execute(
        "SELECT steps FROM daily_watch WHERE user_id=? AND date>=? AND date<? AND steps IS NOT NULL",
        (user_id, start_prev, start_recent),
    ).fetchall()]
    if len(recent) < 7 or len(previous) < 7:
        return None
    recent_med = statistics.median(recent)
    prev_med = statistics.median(previous)
    if prev_med == 0:
        return None
    drop_pct = (prev_med - recent_med) / prev_med * 100
    threshold = config.load()["guards"]["steps_drop_pct"]
    if drop_pct < threshold:
        return None
    return {"recent": round(recent_med), "previous": round(prev_med), "drop_pct": round(drop_pct, 1)}


def trend_block(conn, user_id: int) -> dict | None:
    """Медианы дня с часов за последние 28 дней против предыдущих 28 — пульс,
    шаги (+ цель шагов на сегодня), стресс, HRV. Для get_trends и пакета
    консилиума. None, если данных за последние 28 дней нет вовсе."""
    today_str = config.user_today(conn, user_id)
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    start_recent = (today - timedelta(days=27)).isoformat()
    start_prev = (today - timedelta(days=55)).isoformat()

    fields = ("hr_min", "hr_avg", "steps", "stress_avg", "hrv_ms")
    cols = ", ".join(fields)
    recent = conn.execute(
        f"SELECT {cols} FROM daily_watch WHERE user_id=? AND date>=?",
        (user_id, start_recent),
    ).fetchall()
    if not recent:
        return None
    prev = conn.execute(
        f"SELECT {cols} FROM daily_watch WHERE user_id=? AND date>=? AND date<?",
        (user_id, start_prev, start_recent),
    ).fetchall()

    def _median(rows, field):
        vals = [r[field] for r in rows if r[field] is not None]
        return statistics.median(vals) if vals else None

    out = {"coverage_days": len(recent)}
    for field in fields:
        med = _median(recent, field)
        prev_med = _median(prev, field)
        out[f"{field}_median"] = med
        out[f"{field}_delta"] = (med - prev_med) if med is not None and prev_med is not None else None
    out["steps_coverage_days"] = sum(1 for r in recent if r["steps"] is not None)
    out["step_goal_today"] = step_goal(conn, user_id, today_str)
    return out


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

    # --- per-metric upsert: батч с одними шагами не стирает пульс того же дня ---
    save_days(conn, uid, [{"date": "2026-09-01", "steps": 8000}])
    row_pm = conn.execute(
        "SELECT hr_min, hr_avg, hr_max, steps FROM daily_watch WHERE user_id=? AND date='2026-09-01'", (uid,)
    ).fetchone()
    assert row_pm["hr_min"] == 50 and row_pm["hr_avg"] == 68 and row_pm["hr_max"] == 118, dict(row_pm)
    assert row_pm["steps"] == 8000, dict(row_pm)
    # повторная запись steps заменяет именно steps, остальное не трогает
    save_days(conn, uid, [{"date": "2026-09-01", "steps": 9000}])
    row_pm2 = conn.execute("SELECT hr_min, steps FROM daily_watch WHERE user_id=? AND date='2026-09-01'", (uid,)).fetchone()
    assert row_pm2["hr_min"] == 50 and row_pm2["steps"] == 9000, dict(row_pm2)

    # --- GPT заполняет непереданные hr/hrv нулём — не должно валить steps ---
    r_zero = save_days(conn, uid, [{
        "date": "2026-09-02", "steps": 540, "hr_min": 0, "hr_avg": 0, "hr_max": 0,
        "active_kcal": 0, "hrv_ms": 0, "stress_avg": 0,
    }])
    assert "error" not in r_zero["days"][0], r_zero
    row_zero = conn.execute("SELECT steps, hr_min FROM daily_watch WHERE user_id=? AND date='2026-09-02'", (uid,)).fetchone()
    assert row_zero["steps"] == 540 and row_zero["hr_min"] is None, dict(row_zero)

    # --- валидация: min > max отклоняется, остальные дни батча не страдают ---
    r3 = save_days(conn, uid, [
        {"date": "2026-09-02", "hr_min": 100, "hr_max": 90},
        {"date": "2026-09-03", "hr_avg": 65},
    ])
    assert "error" in r3["days"][0], r3
    assert r3["days"][1].get("action") == "added", r3

    # --- новые показатели: диапазоны и хотя бы одно значение ---
    r3b = save_days(conn, uid, [{"date": "2026-09-04", "steps": 200000}])
    assert "error" in r3b["days"][0], r3b
    r3c = save_days(conn, uid, [{"date": "2026-09-04"}])
    assert "error" in r3c["days"][0] and "хотя бы одно" in r3c["days"][0]["error"], r3c
    r3d = save_days(conn, uid, [{"date": "2026-09-04", "hrv_ms": 45, "stress_avg": 30, "active_kcal": 400}])
    assert r3d["days"][0]["action"] == "added", r3d

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
    for days_ago, hr_min, hr_avg, steps, stress, hrv in (
        (5, 55, 70, 9000, 30, 50), (10, 57, 72, 8000, 32, 48),
        (40, 60, 78, 6000, 40, 40), (45, 62, 80, 5000, 42, 38),
    ):
        d = (today2 - timedelta(days=days_ago)).isoformat()
        save_days(conn2, uid2, [{"date": d, "hr_min": hr_min, "hr_avg": hr_avg, "steps": steps,
                                  "stress_avg": stress, "hrv_ms": hrv}])
    tb = trend_block(conn2, uid2)
    assert tb["coverage_days"] == 2, tb
    assert tb["hr_min_median"] == 56 and tb["hr_avg_median"] == 71, tb
    assert tb["hr_min_delta"] == 56 - 61 and tb["hr_avg_delta"] == 71 - 79, tb
    assert tb["steps_median"] == 8500 and tb["steps_delta"] == 8500 - 5500, tb
    assert tb["stress_avg_median"] == 31 and tb["hrv_ms_median"] == 49, tb
    assert tb["steps_coverage_days"] == 2, tb

    # --- step_goal: медиана 28д до последнего понедельника + 1000, потолок 10000, <7 дней -> None ---
    conn3 = sqlite3.connect(":memory:")
    conn3.row_factory = sqlite3.Row
    conn3.executescript(db.DDL)
    conn3.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (3, '2026-01-01 00:00:00')")
    uid3 = conn3.execute("SELECT id FROM users WHERE telegram_user_id=3").fetchone()["id"]
    today3 = datetime.strptime(config.user_today(conn3, uid3), "%Y-%m-%d").date()
    monday3 = today3 - timedelta(days=today3.weekday())
    for i in range(5):
        d = (monday3 - timedelta(days=i)).isoformat()
        save_days(conn3, uid3, [{"date": d, "steps": 5000}])
    assert step_goal(conn3, uid3, today3.isoformat()) is None, "меньше 7 дней с шагами в окне -> None"
    for i in range(5, 10):
        d = (monday3 - timedelta(days=i)).isoformat()
        save_days(conn3, uid3, [{"date": d, "steps": 5000}])
    assert step_goal(conn3, uid3, today3.isoformat()) == 6000, step_goal(conn3, uid3, today3.isoformat())
    for i in range(10):
        d = (monday3 - timedelta(days=i)).isoformat()
        save_days(conn3, uid3, [{"date": d, "steps": 15000}])
    assert step_goal(conn3, uid3, today3.isoformat()) == 10000, "потолок цели шагов — 10000"
    assert steps_on(conn3, uid3, monday3.isoformat()) == 15000, steps_on(conn3, uid3, monday3.isoformat())

    # --- steps_drop: падение медианы шагов >= guards.steps_drop_pct ---
    conn5 = sqlite3.connect(":memory:")
    conn5.row_factory = sqlite3.Row
    conn5.executescript(db.DDL)
    conn5.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (5, '2026-01-01 00:00:00')")
    uid5 = conn5.execute("SELECT id FROM users WHERE telegram_user_id=5").fetchone()["id"]
    today5 = datetime.strptime(config.user_today(conn5, uid5), "%Y-%m-%d").date()
    assert steps_drop(conn5, uid5) is None, "нет данных -> None"
    for days_ago in range(7):
        d = (today5 - timedelta(days=days_ago)).isoformat()
        save_days(conn5, uid5, [{"date": d, "steps": 4000}])
    assert steps_drop(conn5, uid5) is None, "нет предыдущего окна -> None"
    for days_ago in range(28, 35):
        d = (today5 - timedelta(days=days_ago)).isoformat()
        save_days(conn5, uid5, [{"date": d, "steps": 10000}])
    sd = steps_drop(conn5, uid5)
    assert sd is not None and sd["drop_pct"] >= 15 and sd["recent"] == 4000 and sd["previous"] == 10000, sd

    conn.close()
    conn2.close()
    conn3.close()
    conn5.close()
    print("OK: watch.py — upsert по каждому показателю отдельно, валидация (диапазоны/hr min<=avg<=max/"
          "хотя бы одно значение/будущее), авто-поднятие максимума (два дня, не test, не вниз), "
          "list/delete, trend_block (пульс/шаги/стресс/HRV), step_goal (понедельник/+1000/потолок/<7 дней), "
          "steps_drop (падение медианы шагов)")
