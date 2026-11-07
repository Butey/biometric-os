"""Shared observation of weight pauses; operational signals, never a diagnosis.

The short policy comes from config.guards. The longer comparison preserves the
council policy (last week versus 21–28 days ago, 0.5 kg). Neither determines
whether a change is caused by water, food intake, or metabolic adaptation.
"""
import sqlite3
import statistics
from datetime import date, timedelta

from health_core.config import load, user_now


def _direction(change: float) -> tuple[str, str]:
    if change < 0:
        return "decreasing", "вес снижается"
    if change > 0:
        return "increasing", "вес увеличивается"
    return "variable_weight", "вес колеблется без ясного направления"


def assess(conn: sqlite3.Connection, user_id: int) -> dict:
    now = user_now(conn, user_id)
    today = now.date()
    cfg = load()["guards"]
    window = int(cfg["plateau_window_days"])
    threshold = float(cfg["plateau_range_kg"])
    since = today - timedelta(days=max(28, window))
    excluded = {r["date"] for r in conn.execute(
        "SELECT date FROM refeed_days WHERE user_id=? AND date BETWEEN ? AND ? "
        "UNION SELECT date FROM sick_days WHERE user_id=? AND date BETWEEN ? AND ?",
        (user_id, since.isoformat(), today.isoformat(),
         user_id, since.isoformat(), today.isoformat()),
    )}
    rows = conn.execute(
        "SELECT date(measured_at) d, weight_kg w FROM body_metrics "
        "WHERE user_id=? AND measured_at>=? AND measured_at<=? "
        "AND time(measured_at) BETWEEN '06:00:00' AND '11:00:00' "
        "AND weight_kg IS NOT NULL ORDER BY measured_at",
        (user_id, since.isoformat(), now.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()
    by_day = {}
    for row in rows:
        if row["d"] not in excluded:
            by_day.setdefault(row["d"], []).append(row["w"])
    daily = [(date.fromisoformat(d), statistics.median(ws))
             for d, ws in sorted(by_day.items())]
    short_start = today - timedelta(days=window - 1)
    recent_start = today - timedelta(days=6)
    short = [(d, w) for d, w in daily if d >= short_start]
    fresh = bool(daily and (today - daily[-1][0]).days <= 7)
    short_ready = (fresh and len(short) >= cfg["plateau_min_points"]
                   and (short[-1][0] - short[0][0]).days >= window - 1)
    spread = None
    short_delta = None
    if short_ready:
        xs = [(d - short[0][0]).days for d, _ in short]
        ys = [w for _, w in short]
        short_delta = (statistics.linear_regression(xs, ys).slope * window
                       if len(ys) >= 6 else ys[-1] - ys[0])
        spread = (abs(short_delta) if len(ys) >= 6 else max(ys) - min(ys))
    short_pause = bool(short_ready and spread <= threshold)
    recent = [w for d, w in daily if d >= recent_start]
    older = [w for d, w in daily
             if today - timedelta(days=28) <= d < today - timedelta(days=21)]
    required_days = int(cfg["plateau_min_points"])
    long_ready = fresh and len(recent) >= required_days and len(older) >= required_days
    long_delta = (statistics.median(recent) - statistics.median(older)
                  if long_ready else None)
    waist = conn.execute(
        "SELECT measured_on d, value_cm w FROM anthropometry "
        "WHERE user_id=? AND site IN ('талия','waist') "
        "AND measured_on BETWEEN ? AND ? AND value_cm IS NOT NULL ORDER BY measured_on",
        (user_id, (today - timedelta(days=28)).isoformat(), today.isoformat()),
    ).fetchall()
    waist_by_day = {}
    for row in waist:
        waist_by_day.setdefault(row["d"], []).append(row["w"])
    waist_days = sorted(waist_by_day)
    waist_delta = None
    waist_span = 0
    if len(waist_days) >= 2:
        waist_delta = (statistics.median(waist_by_day[waist_days[-1]])
                       - statistics.median(waist_by_day[waist_days[0]]))
        waist_span = (date.fromisoformat(waist_days[-1])
                      - date.fromisoformat(waist_days[0])).days
    long_flat = long_delta is not None and abs(long_delta) < 0.5
    waist_fresh = bool(waist_days and (today - date.fromisoformat(waist_days[-1])).days <= 7)
    sustained = bool(long_flat and waist_delta is not None and waist_fresh
                     and waist_span >= 21 and waist_delta >= 0
                     and not (short_ready and not short_pause))
    state = "insufficient_data"
    verdict = "недостаточно данных"
    details = "Данных недостаточно, чтобы подтвердить или исключить длительную остановку снижения веса."
    if long_delta is not None and abs(long_delta) >= 0.5:
        state, verdict = _direction(long_delta)
        details = f"Разница медиан последней недели и недели 21–28 дней назад: {long_delta:+.2f} кг."
    elif long_flat and short_ready and not short_pause:
        state, verdict = _direction(short_delta)
        details = (f"Медианы недель близки, но в коротком окне {window} дней "
                   f"тренд изменился на {short_delta:+.2f} кг. Длительная непрерывная пауза не подтверждена.")
    elif long_flat or short_pause:
        if waist_fresh and waist_delta is not None and waist_delta < 0:
            state, verdict = "waist_decreasing", "вес без явного снижения, талия уменьшается"
            details = "Изменение талии не подтверждает остановку всей динамики; причину колебаний веса эти данные не устанавливают."
        elif sustained:
            state, verdict = "sustained_flat_weight", "длительная остановка снижения веса и талии"
            details = "Сравнение недельных медиан веса и замеров талии с интервалом не менее трёх недель не выявило снижения. Причина не установлена; это не клинический диагноз."
        else:
            state, verdict = "short_pause", "сигнал паузы в снижении веса"
            details = "Вес без явного снижения; длительное плато веса и талии не подтверждено. Проверьте достаточность и даты замеров."
    elif short_ready:
        state, verdict = _direction(short_delta)
        details = f"Изменение тренда в окне {window} дней: {short_delta:+.2f} кг; длительную динамику оценивают отдельно."
    return {
        "status": state, "verdict": verdict, "details": details,
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
        "short_weight_pause": short_pause, "sustained_weight_waist_pause": sustained,
        "window_days": window, "threshold_kg": threshold,
        "weight_days": len(short), "short_window_ready": short_ready,
        "weight_first_date": daily[0][0].isoformat() if daily else None,
        "weight_last_date": daily[-1][0].isoformat() if daily else None,
        "short_period_start": short_start.isoformat(),
        "recent_period_start": recent_start.isoformat(),
        "older_period_start": (today - timedelta(days=28)).isoformat(),
        "older_period_end_exclusive": (today - timedelta(days=21)).isoformat(),
        "weight_trend_change_kg": round(short_delta, 3) if short_delta is not None else None,
        "weight_spread_10d_kg": round(spread, 3) if spread is not None else None,
        "weight_median_change_21d_kg": round(long_delta, 3) if long_delta is not None else None,
        "waist_change_21d_cm": round(waist_delta, 2) if waist_delta is not None else None,
        "waist_span_days": waist_span,
        "waist_window_days": 28, "waist_fresh": waist_fresh,
        "waist_first_date": waist_days[0] if waist_days else None,
        "waist_last_date": waist_days[-1] if waist_days else None,
        "recent_weight_days": len(recent), "older_weight_days": len(older),
        "required_weight_days_per_week": required_days, "long_window_ready": long_ready,
        "excluded_days": len(excluded),
        "cause": "unknown",
        "method": "morning_daily_medians; short_config_policy; council_7_vs_21_28d",
    }
