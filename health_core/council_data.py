"""Пакет данных для консилиума (docs/adr/0003-консилиум.md, CONTEXT.md
«Консилиум»): только детерминированные числа, посчитанные существующими
модулями. Сам этот модуль ничего не решает и ничего не вычисляет заново —
только переиспользует health_core.report/energy/nutrition/meds/glp1 и
собирает их результаты в один словарь, который потом читают модели.
"""
import sqlite3
import statistics
from datetime import timedelta

from health_core.config import load, local_now, latest_ffm
from health_core.energy import bmr_floor, adaptive_tdee, kcal_floor, fat_mass_kg, lean_share, _tcx_net
from health_core.report import whr, trends
from health_core.nutrition import day_macros
from health_core.meds import stock_runs_out
from health_core import glp1
from health_core import watch as _watch
from health_core.guards import check_recovery_low


def _since(days: int) -> str:
    return (local_now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _body_composition(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute(
        "SELECT fat_pct, measured_at FROM body_metrics WHERE user_id=? AND fat_pct IS NOT NULL "
        "ORDER BY measured_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return {
        "fat_mass_kg": fat_mass_kg(conn, user_id),
        "ffm_kg": latest_ffm(conn, user_id),
        "fat_pct_latest": row["fat_pct"] if row else None,
        "measured_at": row["measured_at"] if row else None,
        # энергетический баланс §«Доля мышц в потере» — доказательство, не прогноз
        "lean_share_in_loss": lean_share(conn, user_id),
    }


def _waist_12w(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT measured_on, value_cm FROM anthropometry WHERE user_id=? AND site='талия' "
        "AND measured_on>=? ORDER BY measured_on",
        (user_id, (local_now() - timedelta(weeks=12)).date().isoformat()),
    ).fetchall()
    return [{"date": r["measured_on"], "cm": r["value_cm"]} for r in rows]


def _nutrition_vs_target_28d(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """Питание против цели по дням: day_macros() (факт) против сохранённой
    daily_targets (цель того дня) — читаем уже посчитанную цель, не
    пересчитываем её заново (energy.daily_target пишет в БД, сюда не годится)."""
    start = (local_now() - timedelta(days=28)).date()
    out = []
    for i in range(28):
        d = (start + timedelta(days=i)).isoformat()
        macros = day_macros(conn, user_id, d)
        target = conn.execute(
            "SELECT kcal_target, protein_g_target FROM daily_targets WHERE user_id=? AND date=?",
            (user_id, d),
        ).fetchone()
        if not macros["kcal"] and target is None:
            continue  # ни факта, ни цели в этот день — сравнивать нечего
        out.append({
            "date": d,
            "kcal": macros["kcal"],
            "protein_g": macros["protein_g"],
            "kcal_target": target["kcal_target"] if target else None,
            "protein_g_target": target["protein_g_target"] if target else None,
        })
    return out


def _kcal_floor_now(conn: sqlite3.Connection, user_id: int) -> dict:
    """Текущий пол калоража и его причина через kcal_floor() — функцию, которая
    НЕ пишет в БД (в отличие от energy.daily_target). full_tdee считается тем
    же способом, что шаг 1 daily_target(), без побочных эффектов.

    Неполный профиль (нет body_metrics/роста/даты рождения) -> bmr_floor()
    бросает ValueError — тогда пол консилиуму неизвестен, а не падение пакета."""
    today = local_now().date().isoformat()
    try:
        bmr = bmr_floor(conn, user_id)
    except ValueError:
        return {"kcal_floor": None, "floor_reason": None, "full_tdee": None,
                "current_target_kcal": None, "current_target_source": None}
    adaptive = adaptive_tdee(conn, user_id, for_date=today)
    if adaptive is not None:
        base = adaptive
    else:
        factor = load()["policy"].get("activity_factor") or 1.0
        base = bmr * factor
    full_tdee = base + _tcx_net(conn, user_id, today)
    floor, reason = kcal_floor(conn, user_id, full_tdee)
    current = conn.execute(
        "SELECT kcal_target, computed_from FROM daily_targets WHERE user_id=? AND date=?",
        (user_id, today),
    ).fetchone()
    return {
        "kcal_floor": round(floor, 1),
        "floor_reason": reason,
        "full_tdee": round(full_tdee, 1),
        "current_target_kcal": current["kcal_target"] if current else None,
        "current_target_source": current["computed_from"] if current else None,
    }


def _meds_12w(conn: sqlite3.Connection, user_id: int) -> dict:
    doses = conn.execute(
        "SELECT at, substance, dose, unit, route FROM med_log WHERE user_id=? AND at>=? ORDER BY at",
        (user_id, _since(84)),
    ).fetchall()
    schedule = conn.execute(
        "SELECT substance, dose, unit, route, every_days, next_at, stock_doses, dose_by_doctor "
        "FROM med_schedule WHERE user_id=?",
        (user_id,),
    ).fetchall()
    return {
        "doses_12w": [dict(r) for r in doses],
        "schedule": [dict(r) for r in schedule],
        "stock_warnings": stock_runs_out(conn, user_id),
        "glp1_profile": glp1.profile(conn, user_id),
    }


def _side_effects_12w(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT at, symptom, severity, notes FROM side_effects WHERE user_id=? AND at>=? ORDER BY at",
        (user_id, _since(84)),
    ).fetchall()
    return [dict(r) for r in rows]


def _sleep_activity_28d(conn: sqlite3.Connection, user_id: int) -> dict:
    since = _since(28)
    sleep = conn.execute(
        "SELECT night_date, duration_min, quality FROM sleep_log WHERE user_id=? AND night_date>=? "
        "ORDER BY night_date",
        (user_id, since[:10]),
    ).fetchall()
    activity = conn.execute(
        "SELECT started_at, duration_min, kcal, avg_hr, sport FROM activity WHERE user_id=? AND started_at>=? "
        "ORDER BY started_at",
        (user_id, since),
    ).fetchall()
    return {"sleep": [dict(r) for r in sleep], "activity": [dict(r) for r in activity]}


def _refeed_sick_days_28d(conn: sqlite3.Connection, user_id: int) -> dict:
    since = _since(28)[:10]
    refeed = [r["date"] for r in conn.execute(
        "SELECT date FROM refeed_days WHERE user_id=? AND date>=? ORDER BY date", (user_id, since)
    ).fetchall()]
    sick = [r["date"] for r in conn.execute(
        "SELECT date FROM sick_days WHERE user_id=? AND date>=? ORDER BY date", (user_id, since)
    ).fetchall()]
    return {"refeed_days": refeed, "sick_days": sick}


def _recent_alerts_28d(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT created_at, rule, message FROM alerts WHERE user_id=? AND created_at>=? ORDER BY created_at",
        (user_id, _since(28)),
    ).fetchall()
    return [dict(r) for r in rows]


def build(conn: sqlite3.Connection, user_id: int) -> dict:
    """Единый детерминированный пакет для консилиума. Пустая/неполная БД не
    роняет сборку — недостающие числа просто становятся None/[]."""
    return {
        "weight_trend": {
            "4w": trends(conn, user_id, window_days=4 * 7),
            "8w": trends(conn, user_id, window_days=8 * 7),
            "12w": trends(conn, user_id, window_days=12 * 7),
        },
        "body_composition": _body_composition(conn, user_id),
        "waist_12w": _waist_12w(conn, user_id),
        "nutrition_vs_target_28d": _nutrition_vs_target_28d(conn, user_id),
        "kcal_floor": _kcal_floor_now(conn, user_id),
        "meds": _meds_12w(conn, user_id),
        "side_effects_12w": _side_effects_12w(conn, user_id),
        "sleep_activity_28d": _sleep_activity_28d(conn, user_id),
        "refeed_sick_days_28d": _refeed_sick_days_28d(conn, user_id),
        "recent_alerts_28d": _recent_alerts_28d(conn, user_id),
        "daily_watch_28d": _watch.trend_block(conn, user_id),
        "steps_drop_28d": _watch.steps_drop(conn, user_id),
        "recovery_low": check_recovery_low(conn, user_id),
    }


def plateau_3w(conn: sqlite3.Connection, user_id: int) -> bool:
    """CONTEXT.md «Консилиум» / docs/adr/0003-консилиум.md: медиана веса за
    последние 7 дней отличается от медианы 21-28 дней назад меньше чем на
    0.5 кг, И талия за 21 день не уменьшилась (нужно >=2 замера в окне,
    последний не меньше первого)."""
    now = local_now()
    recent = [r["weight_kg"] for r in conn.execute(
        "SELECT weight_kg FROM body_metrics WHERE user_id=? AND weight_kg IS NOT NULL AND measured_at>=?",
        (user_id, (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()]
    older = [r["weight_kg"] for r in conn.execute(
        "SELECT weight_kg FROM body_metrics WHERE user_id=? AND weight_kg IS NOT NULL "
        "AND measured_at>=? AND measured_at<?",
        (user_id, (now - timedelta(days=28)).strftime("%Y-%m-%d %H:%M:%S"),
         (now - timedelta(days=21)).strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()]
    if not recent or not older:
        return False
    if abs(statistics.median(recent) - statistics.median(older)) >= 0.5:
        return False
    waist = conn.execute(
        "SELECT value_cm FROM anthropometry WHERE user_id=? AND site='талия' AND measured_on>=? "
        "ORDER BY measured_on",
        (user_id, (now - timedelta(days=21)).date().isoformat()),
    ).fetchall()
    if len(waist) < 2:
        return False
    return waist[-1]["value_cm"] >= waist[0]["value_cm"]


if __name__ == "__main__":
    import os
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HEALTH_DB"] = str(Path(tmp) / "health.db")
        import health_core.db as db
        db.DB_PATH = Path(os.environ["HEALTH_DB"])
        conn = db.connect()
        db.migrate(conn)

        conn.execute(
            "INSERT INTO users(telegram_user_id, created_at) VALUES (1, '2026-08-01 00:00:00')"
        )
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]

        # --- пустая БД: build() не падает, числа отсутствуют, а не выдуманы ---
        empty = build(conn, uid)
        assert set(empty) == {
            "weight_trend", "body_composition", "waist_12w", "nutrition_vs_target_28d",
            "kcal_floor", "meds", "side_effects_12w", "sleep_activity_28d",
            "refeed_sick_days_28d", "recent_alerts_28d", "daily_watch_28d",
            "steps_drop_28d", "recovery_low",
        }
        assert empty["daily_watch_28d"] is None, "нет данных с часов -> блок None, а не выдуманные числа"
        assert empty["steps_drop_28d"] is None, "нет данных шагов -> None"
        assert empty["recovery_low"] is None, "нет данных часов -> RECOVERY_LOW молчит"
        assert empty["weight_trend"]["4w"]["weight_delta_kg"] is None
        assert empty["body_composition"]["fat_mass_kg"] is None
        assert empty["waist_12w"] == []
        assert empty["kcal_floor"]["kcal_floor"] is None, "нет профиля/весов -> пол неизвестен, не BMR по умолчанию"
        assert empty["meds"]["doses_12w"] == [] and empty["meds"]["glp1_profile"] is None
        assert plateau_3w(conn, uid) is False, "нет замеров -> плато не может быть доказано"
        print("OK: build() и plateau_3w() не падают на пустой БД, числа отсутствуют явно")

        # --- заполненная БД: плато НЕ сработало (вес всё ещё падает) ---
        conn.execute(
            "UPDATE users SET height_cm=185, birth_date='1992-08-09', sex='m' WHERE id=?", (uid,)
        )
        for days_ago, w in ((27, 92.0), (24, 91.5), (21, 91.0), (14, 90.0), (7, 89.0), (1, 88.5)):
            ts = (local_now() - timedelta(days=days_ago)).strftime("%Y-%m-%d 07:00:00")
            conn.execute(
                "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, fat_pct, ffm_kg) "
                "VALUES (?, ?, ?, ?, 28.0, 65.0)",
                (uid, f"cd-{days_ago}", ts, w),
            )
        conn.commit()
        assert plateau_3w(conn, uid) is False, "вес падает на 2.5кг за 3 недели — это не плато"
        print("OK: plateau_3w() = False, когда вес продолжает падать")

        # --- плато: вес почти не двигался 3+ недели, талия не уменьшилась ---
        conn.execute("DELETE FROM body_metrics WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM anthropometry WHERE user_id=?", (uid,))
        for days_ago, w in ((27, 90.0), (24, 90.1), (21, 90.0), (14, 89.9), (7, 89.8), (1, 90.1)):
            ts = (local_now() - timedelta(days=days_ago)).strftime("%Y-%m-%d 07:00:00")
            conn.execute(
                "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, fat_pct, ffm_kg) "
                "VALUES (?, ?, ?, ?, 28.0, 65.0)",
                (uid, f"pl-{days_ago}", ts, w),
            )
        for days_ago, waist in ((20, 95.0), (2, 95.2)):
            d = (local_now() - timedelta(days=days_ago)).date().isoformat()
            conn.execute(
                "INSERT INTO anthropometry(user_id, measured_on, site, value_cm) VALUES (?, ?, 'талия', ?)",
                (uid, d, waist),
            )
        conn.commit()
        assert plateau_3w(conn, uid) is True, "вес и талия стоят 3 недели — должно сработать"
        print("OK: plateau_3w() = True на застрявшем весе и не уменьшившейся талии")

        # --- талия уменьшилась -> плато не подтверждено, даже если вес стоит ---
        conn.execute("DELETE FROM anthropometry WHERE user_id=?", (uid,))
        for days_ago, waist in ((20, 95.0), (2, 93.5)):
            d = (local_now() - timedelta(days=days_ago)).date().isoformat()
            conn.execute(
                "INSERT INTO anthropometry(user_id, measured_on, site, value_cm) VALUES (?, ?, 'талия', ?)",
                (uid, d, waist),
            )
        conn.commit()
        assert plateau_3w(conn, uid) is False, "талия уменьшилась — жир уходит, это не плато"
        print("OK: plateau_3w() = False, когда талия за 21 день уменьшилась")

        # --- заполненный пакет содержит реальные числа ---
        full = build(conn, uid)
        assert full["kcal_floor"]["kcal_floor"] is not None
        assert len(full["waist_12w"]) == 2
        print(f"OK: build() на заполненной БД -> kcal_floor={full['kcal_floor']}")

        conn.close()
        print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
