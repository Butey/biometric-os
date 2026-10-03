"""Давление (CONTEXT.md «Замер давления»): категория по домашним порогам и
выборка последних замеров. Решает код, модель только пересказывает."""
import sqlite3
from datetime import datetime, timedelta


def category(sys_mmhg: float, dia_mmhg: float) -> str:
    """Категория одного замера (домашние пороги ESH/AHA; нижняя граница - обычная для гипотонии)."""
    if sys_mmhg >= 180 or dia_mmhg >= 120:
        return "кризисный диапазон"
    if sys_mmhg >= 160 or dia_mmhg >= 100:
        return "высокое, 2 степень"
    if sys_mmhg >= 140 or dia_mmhg >= 90:
        return "высокое, 1 степень"
    if sys_mmhg >= 130 or dia_mmhg >= 85:
        return "повышенное"
    if sys_mmhg < 90 or dia_mmhg < 60:
        return "пониженное"
    return "норма"


def recent(conn: sqlite3.Connection, user_id: int, now: datetime, days: int) -> list:
    """Замеры за последние days суток, от старых к новым."""
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return conn.execute(
        "SELECT id, at, systolic, diastolic, pulse FROM bp_log WHERE user_id=? AND at>=? ORDER BY at, id",
        (user_id, since),
    ).fetchall()
