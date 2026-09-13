"""КБЖУ и баланс тарелки — §09/§03. Относительные коэффициенты, не абсолютные граммы."""
import sqlite3

from health_core.config import targets_for


def day_macros(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    row = conn.execute(
        "SELECT SUM(fi.kcal) kcal, SUM(fi.protein_g) protein_g, "
        "SUM(fi.fat_g) fat_g, SUM(fi.carbs_g) carb_g, SUM(fi.fiber_g) fiber_g, "
        "COUNT(fi.fiber_g) fiber_n "
        "FROM food_log fl JOIN food_items fi ON fi.food_log_id = fl.id "
        "WHERE fl.user_id=? AND date(fl.eaten_at)=?",
        (user_id, date),
    ).fetchone()
    return {
        "kcal": row["kcal"] or 0.0,
        "protein_g": row["protein_g"] or 0.0,
        "fat_g": row["fat_g"] or 0.0,
        "carb_g": row["carb_g"] or 0.0,
        "fiber_g": row["fiber_g"] or 0.0,
        "fiber_n": row["fiber_n"] or 0,
    }


def plate_balance(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """Доля каждой категории тарелки (food_items.plate_category) в дне.

    §04 (100-173) описывает только то, что у food_items есть колонка
    plate_category — конкретные названия категорий и целевые доли нигде в
    прочитанном диапазоне не заданы (в config.yaml их тоже нет, только
    protein/water/tcx-коэффициенты). Поэтому здесь считаются только
    НАБЛЮДАЕМЫЕ доли; target_share=None пока где-то не появится авторитетный
    источник целевых процентов — заполнять их произвольно значило бы выдумывать
    протокол, а не воспроизводить его.
    """
    rows = conn.execute(
        "SELECT fi.plate_category cat, SUM(fi.kcal) kcal "
        "FROM food_log fl JOIN food_items fi ON fi.food_log_id = fl.id "
        "WHERE fl.user_id=? AND date(fl.eaten_at)=? AND fi.plate_category IS NOT NULL "
        "GROUP BY fi.plate_category",
        (user_id, date),
    ).fetchall()
    total = sum(r["kcal"] or 0.0 for r in rows)
    return {
        r["cat"]: {
            "kcal": r["kcal"] or 0.0,
            "share": (r["kcal"] or 0.0) / total if total else 0.0,
            "target_share": None,
        }
        for r in rows
    }


def protein_target(conn: sqlite3.Connection, user_id: int) -> float:
    """protein_g_per_kg_ffm (config.yaml, уровень «Политика») × текущий FFM пользователя."""
    return targets_for(conn, user_id)["protein_g"]
