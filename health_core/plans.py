"""Weekly plan template (meal_plan/workout_plan, schema v4) + plan-vs-actual diff.

day_of_week: 0=Monday..6=Sunday (Python's date.weekday()). A REPEATING weekly
template, not a dated log — set_meal_plan/set_workout_plan upsert by
(user, day_of_week, slot/name); nothing here ever writes a calendar date.

Zero invented numbers: an empty plan stays an empty list, a missing plan in
plan_vs_actual is None (never 0), and nothing in this module judges or scores —
it returns numbers, the caller speaks.
"""
import sqlite3
from datetime import datetime

_MEAL_FIELDS = ("name", "kcal", "protein_g", "fat_g", "carbs_g", "notes")
_WORKOUT_FIELDS = ("kind", "duration_min", "notes")


def _upsert(conn: sqlite3.Connection, table: str, key: dict, fields: dict, allowed: tuple) -> int:
    """Shared UPSERT for meal_plan/workout_plan: INSERT on the (user, day, slot/name)
    unique key, DO UPDATE the given fields on conflict. Unknown field names raise —
    a silently-dropped typo in a plan field is worse than a loud TypeError."""
    unknown = set(fields) - set(allowed)
    if unknown:
        raise TypeError(f"{table}: unknown field(s) {unknown}")
    cols = list(key) + list(fields)
    vals = list(key.values()) + list(fields.values())
    placeholders = ",".join("?" * len(cols))
    conflict = ",".join(key)
    set_clause = ",".join(f"{c}=excluded.{c}" for c in fields) or f"{cols[0]}=excluded.{cols[0]}"
    conn.execute(
        f"INSERT INTO {table}({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict}) DO UPDATE SET {set_clause}",
        vals,
    )
    conn.commit()
    where = " AND ".join(f"{c}=?" for c in key)
    return conn.execute(f"SELECT id FROM {table} WHERE {where}", list(key.values())).fetchone()["id"]


def set_meal_plan(conn: sqlite3.Connection, user_id: int, day_of_week: int, meal_slot: str, **fields) -> int:
    """UPSERT on (user_id, day_of_week, meal_slot). fields subset of
    name/kcal/protein_g/fat_g/carbs_g/notes (meal_plan columns)."""
    return _upsert(
        conn, "meal_plan",
        {"user_id": user_id, "day_of_week": day_of_week, "meal_slot": meal_slot},
        fields, _MEAL_FIELDS,
    )


def set_workout_plan(conn: sqlite3.Connection, user_id: int, day_of_week: int, name: str, **fields) -> int:
    """UPSERT on (user_id, day_of_week, name). fields subset of
    kind/duration_min/notes (workout_plan columns)."""
    return _upsert(
        conn, "workout_plan",
        {"user_id": user_id, "day_of_week": day_of_week, "name": name},
        fields, _WORKOUT_FIELDS,
    )


def remove_meal_plan(conn: sqlite3.Connection, user_id: int, day_of_week: int | None = None, meal_slot: str | None = None) -> int:
    """Удалить позицию/день из шаблона питания meal_plan."""
    if day_of_week is None:
        cur = conn.execute("DELETE FROM meal_plan WHERE user_id=?", (user_id,))
    elif meal_slot is None:
        cur = conn.execute("DELETE FROM meal_plan WHERE user_id=? AND day_of_week=?", (user_id, day_of_week))
    else:
        cur = conn.execute("DELETE FROM meal_plan WHERE user_id=? AND day_of_week=? AND meal_slot=?", (user_id, day_of_week, meal_slot))
    conn.commit()
    return cur.rowcount


def remove_workout_plan(conn: sqlite3.Connection, user_id: int, day_of_week: int | None = None, name: str | None = None) -> int:
    """Удалить тренировку/день из шаблона workout_plan."""
    if day_of_week is None:
        cur = conn.execute("DELETE FROM workout_plan WHERE user_id=?", (user_id,))
    elif name is None:
        cur = conn.execute("DELETE FROM workout_plan WHERE user_id=? AND day_of_week=?", (user_id, day_of_week))
    else:
        cur = conn.execute("DELETE FROM workout_plan WHERE user_id=? AND day_of_week=? AND name=?", (user_id, day_of_week, name))
    conn.commit()
    return cur.rowcount


def delete_plan_day(conn: sqlite3.Connection, user_id: int, date: str | None = None, kind: str | None = None) -> int:
    """Удалить план на дату из plan_log."""
    if date is None and kind is None:
        cur = conn.execute("DELETE FROM plan_log WHERE user_id=?", (user_id,))
    elif date is None:
        cur = conn.execute("DELETE FROM plan_log WHERE user_id=? AND kind=?", (user_id, kind))
    elif kind is None:
        cur = conn.execute("DELETE FROM plan_log WHERE user_id=? AND date=?", (user_id, date))
    else:
        cur = conn.execute("DELETE FROM plan_log WHERE user_id=? AND date=? AND kind=?", (user_id, date, kind))
    conn.commit()
    return cur.rowcount


def get_plan(conn: sqlite3.Connection, user_id: int, day_of_week: int | None = None) -> dict:
    """{"meals": [...], "workouts": [...]} for one weekday, or the whole week if
    day_of_week is None. Empty lists when nothing is saved — never fabricated."""
    if day_of_week is None:
        meals = conn.execute(
            "SELECT * FROM meal_plan WHERE user_id=? ORDER BY day_of_week, meal_slot", (user_id,)
        ).fetchall()
        workouts = conn.execute(
            "SELECT * FROM workout_plan WHERE user_id=? ORDER BY day_of_week, name", (user_id,)
        ).fetchall()
    else:
        meals = conn.execute(
            "SELECT * FROM meal_plan WHERE user_id=? AND day_of_week=? ORDER BY meal_slot",
            (user_id, day_of_week),
        ).fetchall()
        workouts = conn.execute(
            "SELECT * FROM workout_plan WHERE user_id=? AND day_of_week=? ORDER BY name",
            (user_id, day_of_week),
        ).fetchall()
    return {"meals": [dict(r) for r in meals], "workouts": [dict(r) for r in workouts]}


def _meals_vs_actual(conn: sqlite3.Connection, user_id: int, dow: int, date: str) -> dict:
    p = conn.execute(
        "SELECT COUNT(*) n, SUM(kcal) kcal, SUM(protein_g) protein_g, SUM(fat_g) fat_g, SUM(carbs_g) carbs_g "
        "FROM meal_plan WHERE user_id=? AND day_of_week=?",
        (user_id, dow),
    ).fetchone()
    planned = None
    if p["n"]:
        planned = {
            "count": p["n"], "kcal": p["kcal"] or 0.0, "protein_g": p["protein_g"] or 0.0,
            "fat_g": p["fat_g"] or 0.0, "carbs_g": p["carbs_g"] or 0.0,
        }
    a = conn.execute(
        "SELECT COUNT(DISTINCT fl.id) n, COALESCE(SUM(fi.kcal),0) kcal, "
        "COALESCE(SUM(fi.protein_g),0) protein_g, COALESCE(SUM(fi.fat_g),0) fat_g, "
        "COALESCE(SUM(fi.carbs_g),0) carbs_g "
        "FROM food_log fl LEFT JOIN food_items fi ON fi.food_log_id=fl.id "
        "WHERE fl.user_id=? AND date(fl.eaten_at)=?",
        (user_id, date),
    ).fetchone()
    actual = {"count": a["n"], "kcal": a["kcal"], "protein_g": a["protein_g"], "fat_g": a["fat_g"], "carbs_g": a["carbs_g"]}
    delta = None if planned is None else {k: round(planned[k] - actual[k], 2) for k in planned}
    return {"planned": planned, "actual": actual, "delta": delta}


def _workouts_vs_actual(conn: sqlite3.Connection, user_id: int, dow: int, date: str) -> dict:
    p = conn.execute(
        "SELECT COUNT(*) n, SUM(duration_min) duration_min FROM workout_plan WHERE user_id=? AND day_of_week=?",
        (user_id, dow),
    ).fetchone()
    planned = None
    if p["n"]:
        planned = {"count": p["n"], "duration_min": p["duration_min"] or 0.0}
    a = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(duration_min),0) duration_min, COALESCE(SUM(kcal),0) kcal "
        "FROM activity WHERE user_id=? AND date(started_at)=?",
        (user_id, date),
    ).fetchone()
    actual = {"count": a["n"], "duration_min": a["duration_min"], "kcal": a["kcal"]}
    delta = None if planned is None else {k: round(planned[k] - actual[k], 2) for k in planned}
    return {"planned": planned, "actual": actual, "delta": delta}


def plan_vs_actual(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """Planned (weekly template for date's weekday) vs actual (that calendar date),
    for meals and workouts. Each side is {"planned": dict|None, "actual": dict,
    "delta": dict|None}. delta = planned - actual per field, present only when a
    plan exists; a missing plan is None all the way through, never a 0 stand-in.
    No judgment, no advice — just the numbers."""
    dow = datetime.strptime(date, "%Y-%m-%d").weekday()
    return {
        "meals": _meals_vs_actual(conn, user_id, dow, date),
        "workouts": _workouts_vs_actual(conn, user_id, dow, date),
    }


if __name__ == "__main__":
    import os
    import sys
    import tempfile
    from pathlib import Path

    sys.stdout.reconfigure(encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HEALTH_DB"] = str(Path(tmp) / "health.db")
        import health_core.db as db
        db.DB_PATH = Path(os.environ["HEALTH_DB"])
        conn = db.connect()
        db.migrate(conn)

        try:
            uid = conn.execute(
                "INSERT INTO users(telegram_user_id, created_at) VALUES (1, '2026-08-20 00:00:00')"
            ).lastrowid

            # --- set_meal_plan twice on same key -> one row, updated ---
            id1 = set_meal_plan(conn, uid, 0, "breakfast", name="Овсянка", kcal=400, protein_g=20)
            id2 = set_meal_plan(conn, uid, 0, "breakfast", name="Омлет", kcal=350, protein_g=30)
            assert id1 == id2, f"upsert should keep the same row id, got {id1} vs {id2}"
            rows = conn.execute(
                "SELECT * FROM meal_plan WHERE user_id=? AND day_of_week=0 AND meal_slot='breakfast'", (uid,)
            ).fetchall()
            assert len(rows) == 1, f"expected 1 row after double upsert, got {len(rows)}"
            assert rows[0]["name"] == "Омлет" and rows[0]["kcal"] == 350, f"row not updated: {dict(rows[0])}"
            print("OK: set_meal_plan upserts, one row survives, values updated")

            # --- get_plan with no rows -> empty lists, no exception, nothing fabricated ---
            empty = get_plan(conn, uid, day_of_week=3)
            assert empty == {"meals": [], "workouts": []}, f"expected empty plan, got {empty}"
            print("OK: get_plan on empty day returns empty lists, nothing fabricated")

            # --- plan_vs_actual: plan present, no actual -> actual zero, delta == plan ---
            set_workout_plan(conn, uid, 1, "Бег", kind="cardio", duration_min=30)
            set_meal_plan(conn, uid, 1, "lunch", kcal=600, protein_g=40, fat_g=20, carbs_g=50)
            tuesday = "2026-08-25"  # Python weekday() == 1 for this date
            assert datetime.strptime(tuesday, "%Y-%m-%d").weekday() == 1
            pva = plan_vs_actual(conn, uid, tuesday)
            assert pva["meals"]["planned"] == {"count": 1, "kcal": 600.0, "protein_g": 40.0, "fat_g": 20.0, "carbs_g": 50.0}
            assert pva["meals"]["actual"] == {"count": 0, "kcal": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0}
            assert pva["meals"]["delta"] == pva["meals"]["planned"], (
                f"delta should equal the plan when actual is zero, got {pva['meals']['delta']}"
            )
            assert pva["workouts"]["planned"] == {"count": 1, "duration_min": 30.0}
            assert pva["workouts"]["delta"] == pva["workouts"]["planned"]
            print("OK: plan_vs_actual with plan and no actual -> actual zero, delta equals the plan")

            # --- plan_vs_actual: actual present, no plan -> planned None, not 0 ---
            wednesday = "2026-08-26"  # weekday() == 2, no plan rows exist for day_of_week=2
            assert datetime.strptime(wednesday, "%Y-%m-%d").weekday() == 2
            flid = conn.execute(
                "INSERT INTO food_log(user_id, eaten_at) VALUES (?, ?)", (uid, f"{wednesday} 08:00:00")
            ).lastrowid
            conn.execute(
                "INSERT INTO food_items(food_log_id, kcal, protein_g, fat_g, carbs_g) VALUES (?,?,?,?,?)",
                (flid, 500, 30, 15, 40),
            )
            conn.execute(
                "INSERT INTO activity(user_id, started_at, duration_min, kcal, sport, file_hash) "
                "VALUES (?,?,?,?,?,?)",
                (uid, f"{wednesday} 18:00:00", 45, 300, "run", "hash-1"),
            )
            conn.commit()
            pva2 = plan_vs_actual(conn, uid, wednesday)
            assert pva2["meals"]["planned"] is None, f"expected planned None, got {pva2['meals']['planned']}"
            assert pva2["meals"]["delta"] is None, f"expected delta None, got {pva2['meals']['delta']}"
            assert pva2["meals"]["actual"]["kcal"] == 500, f"actual kcal wrong: {pva2['meals']['actual']}"
            assert pva2["workouts"]["planned"] is None
            assert pva2["workouts"]["actual"]["duration_min"] == 45
            print("OK: plan_vs_actual with actual and no plan -> planned None (not 0), actual present")

            # --- meals_of_day: ordering, subtotals, and a ghost (zero-item) meal ---
            from health_core.report import meals_of_day

            thursday = "2026-08-27"
            flid_a = conn.execute(
                "INSERT INTO food_log(user_id, eaten_at) VALUES (?, ?)", (uid, f"{thursday} 08:00:00")
            ).lastrowid
            conn.execute(
                "INSERT INTO food_items(food_log_id, name, kcal, protein_g, fat_g, carbs_g) VALUES (?,?,?,?,?,?)",
                (flid_a, "Овсянка", 300, 15, 5, 40),
            )
            conn.execute(
                "INSERT INTO food_items(food_log_id, name, kcal, protein_g, fat_g, carbs_g) VALUES (?,?,?,?,?,?)",
                (flid_a, "Яйцо", 100, 10, 7, 1),
            )
            # ghost meal: food_log row with zero food_items (this happened in prod)
            flid_ghost = conn.execute(
                "INSERT INTO food_log(user_id, eaten_at) VALUES (?, ?)", (uid, f"{thursday} 13:00:00")
            ).lastrowid
            flid_b = conn.execute(
                "INSERT INTO food_log(user_id, eaten_at) VALUES (?, ?)", (uid, f"{thursday} 19:00:00")
            ).lastrowid
            conn.execute(
                "INSERT INTO food_items(food_log_id, name, kcal, protein_g, fat_g, carbs_g) VALUES (?,?,?,?,?,?)",
                (flid_b, "Курица", 250, 40, 8, 0),
            )
            conn.commit()

            meals = meals_of_day(conn, uid, thursday)
            assert len(meals) == 3, f"expected 3 food_log rows for {thursday}, got {len(meals)}"
            assert [m["food_log_id"] for m in meals] == [flid_a, flid_ghost, flid_b], (
                f"meals must be ordered by eaten_at, got {[m['food_log_id'] for m in meals]}"
            )
            m0 = meals[0]
            assert len(m0["items"]) == 2 and m0["kcal"] == 400 and m0["protein_g"] == 25 and \
                m0["fat_g"] == 12 and m0["carbs_g"] == 41, f"breakfast subtotal wrong: {m0}"
            ghost = meals[1]
            assert ghost["items"] == [], f"ghost meal must show items=[], got {ghost['items']}"
            assert ghost["kcal"] == 0.0 and ghost["protein_g"] == 0.0, f"ghost meal must have zero subtotals: {ghost}"
            print("OK: meals_of_day orders by eaten_at, sums subtotals correctly, surfaces the zero-item ghost meal")
        finally:
            conn.close()  # close before TemporaryDirectory cleanup, or Windows raises WinError 32 first
