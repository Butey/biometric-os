"""Сопоставимая оценка доли FFM в снижении веса, не измерение потери мышц."""
import sqlite3
import statistics
from datetime import date

from health_core.config import load, user_now


def lean_loss_ratio(conn: sqlite3.Connection, user_id: int, since: str | None,
                    min_points: int = 2) -> tuple[float, float, float, int] | None:
    now = user_now(conn, user_id)
    query = (
        "SELECT date(measured_at) d, weight_kg, ffm_kg FROM body_metrics "
        "WHERE user_id=? AND measured_at<=? AND weight_kg>0 "
        "AND ffm_kg>0 AND ffm_kg<=weight_kg "
        "AND time(measured_at) BETWEEN '06:00:00' AND '11:00:00'"
    )
    params = [user_id, now.strftime("%Y-%m-%d %H:%M:%S")]
    if since is not None:
        query += " AND measured_at>=?"
        params.append(since)
    by_day = {}
    for row in conn.execute(query + " ORDER BY measured_at", params):
        by_day.setdefault(row["d"], []).append((row["weight_kg"], row["ffm_kg"]))
    days = sorted(by_day)
    if len(days) < max(2, min_points):
        return None
    if (now.date() - date.fromisoformat(days[-1])).days > load()["guards"]["no_measure_days"]:
        return None
    daily = [(statistics.median(w for w, _ in by_day[d]),
              statistics.median(f for _, f in by_day[d])) for d in days]
    k = min(3, len(daily) // 2)  # крайние группы не перекрываются
    first, last = daily[:k], daily[-k:]
    dw = statistics.median(w for w, _ in last) - statistics.median(w for w, _ in first)
    if -dw < load()["policy"]["lean_share_min_loss_kg"]:
        return None
    df = statistics.median(f for _, f in last) - statistics.median(f for _, f in first)
    return max(0.0, -df) / -dw, dw, df, len(daily)
