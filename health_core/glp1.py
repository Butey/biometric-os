"""Оценка относительного уровня тирзепатида в недельном цикле дозирования.

ЗАЧЕМ. Пользователь колет тирзепатид раз в неделю (вс 22:00, 12.5 мг). Перед
рефид-днём или сравнением голода/сытости полезно знать, где по циклу сейчас
находится уровень препарата: у пика, на спаде или у минимума перед следующим
уколом. Это ТОЛЬКО информация для планирования — она не двигает калорийность
и никакие цели: числа для таргетов считает детерминированный Python в других
модулях, а спекулятивная фармакокинетика в них не участвует.

МОДЕЛЬ. Однокамерная модель с всасыванием первого порядка (уравнение Бейтмана):

    C(t) ∝ D · (e^(−ke·t) − e^(−ka·t)),   t — часы с момента укола

При нескольких инъекциях уровни складываются (суперпозиция линейной системы).
ke — константа элиминации, взята из периода полувыведения по инструкции
препарата. ka — константа всасывания, аналитически не выражается через Tmax
и решается численно (бисекцией) из уравнения

    Tmax = ln(ka/ke) / (ka − ke)

Абсолютная концентрация не нужна: наружу отдаётся C(t) как % от максимума за
окно наблюдения, так что пропорциональность ∝ (без нормирующего множителя
ka/(ka−ke)) ничего не меняет — она сокращается при делении.

ЧЕГО МОДЕЛЬ НЕ УМЕЕТ: это население-усреднённая фармакокинетика по вкладышу
препарата, а не измерение у конкретного человека. Индивидуальная скорость
всасывания и выведения гуляет в разы. Число — ориентир для планирования дня,
не медицинский факт.
"""
import math
import re
import sqlite3
from datetime import datetime, timedelta

from health_core import config
from health_core.meds import canon

# Mounjaro prescribing information (Eli Lilly): период полувыведения ≈5 дней.
HALF_LIFE_DAYS = 5.0
KE = math.log(2) / (HALF_LIFE_DAYS * 24)  # 1/час, константа элиминации

# Mounjaro prescribing information: Tmax в диапазоне 8-72ч, здесь взят центр.
TMAX_H = 24.0

DOUBLE_DOSE_CEILING_H = 72  # см. _collapse ниже
WINDOW_WEEKS = 8
_TARGET = "Тирзепатид"


def _solve_ka(ke: float, tmax: float) -> float:
    """ka из Tmax = ln(ka/ke)/(ka-ke). Замкнутой формы нет — ka входит и под
    логарифмом, и линейно, поэтому решаем бисекцией, а не по формуле."""

    def f(ka: float) -> float:
        return math.log(ka / ke) / (ka - ke) - tmax

    lo, hi = ke * 1.0001, ke * 1000.0
    flo, fhi = f(lo), f(hi)
    assert flo > 0 > fhi, ("вилка бисекции не накрывает корень", flo, fhi)
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


KA = _solve_ka(KE, TMAX_H)


def _parse_dose(text) -> float | None:
    """Число из TEXT дозы ('12.5', '12.5 мг', ...). None, если не нашли."""
    if text is None:
        return None
    m = re.search(r"[-+]?\d*\.?\d+", str(text))
    return float(m.group()) if m else None


def _parse_dt(s: str) -> datetime:
    s = s.strip()
    if len(s) == 10:
        s += " 00:00:00"
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


def _bateman(dose_mg: float, t_hours: float) -> float:
    """Вклад одной дозы в C(t) через t часов после неё (0 до укола)."""
    if t_hours < 0:
        return 0.0
    return dose_mg * (math.exp(-KE * t_hours) - math.exp(-KA * t_hours))


def _concentration(doses: list[tuple[datetime, float]], t: datetime) -> float:
    return sum(_bateman(d, (t - at).total_seconds() / 3600) for at, d in doses)


def _collapse(injections: list[tuple[datetime, float | None]]) -> list[tuple[datetime, float]]:
    """Инъекции ближе DOUBLE_DOSE_CEILING_H часов подряд — одна доза, лог
    хранит более позднюю запись (обычно один и тот же укол задвоен вводом).

    ponytail: потолок 72ч жёсткий, а не оценка типичного интервала между
    уколами — настоящая двойная доза (случайный повторный укол раньше срока)
    будет по ошибке схлопнута в одну и недосчитана моделью.
    """
    injections = sorted(injections, key=lambda x: x[0])
    collapsed: list[tuple[datetime, float | None]] = []
    for at, dose in injections:
        if collapsed and (at - collapsed[-1][0]) < timedelta(hours=DOUBLE_DOSE_CEILING_H):
            collapsed[-1] = (at, dose)
        else:
            collapsed.append((at, dose))

    resolved: list[tuple[datetime, float]] = []
    last_good = None
    for at, dose in collapsed:
        if dose is None:
            dose = last_good if last_good is not None else 1.0
        else:
            last_good = dose
        resolved.append((at, dose))
    return resolved


def profile(conn: sqlite3.Connection, user_id: int, now: datetime | None = None) -> dict | None:
    """Оценка уровня тирзепатида сейчас + ближайшие пик/минимум цикла.

    None, если за последние WINDOW_WEEKS недель не было ни одного укола.
    """
    if now is None:
        now = config.local_now().replace(tzinfo=None)

    since = (now - timedelta(weeks=WINDOW_WEEKS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT at, substance, dose, route FROM med_log WHERE user_id=? AND at>=? AND at<=? "
        "ORDER BY at",
        (user_id, since, now.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()
    injections = [
        (_parse_dt(r["at"]), _parse_dose(r["dose"]))
        for r in rows
        if canon(r["substance"]) == _TARGET and (r["route"] is None or r["route"] == "injection")
    ]
    if not injections:
        return None

    doses = _collapse(injections)
    last_at, last_dose = doses[-1]

    next_at = None
    for r in conn.execute(
        "SELECT substance, next_at FROM med_schedule WHERE user_id=?", (user_id,)
    ).fetchall():
        if r["next_at"] and canon(r["substance"]) == _TARGET:
            next_at = _parse_dt(r["next_at"])
            break
    if next_at is None:
        next_at = last_at + timedelta(days=7)

    # Прогноз будущих доз: та же доза, что последняя фактическая, раз в
    # неделю начиная с next_at (просроченный next_at докручиваем вперёд).
    window_end = now + timedelta(days=7)
    t = next_at
    while t < now:
        t += timedelta(days=7)
    future: list[tuple[datetime, float]] = []
    while t <= window_end:
        future.append((t, last_dose))
        t += timedelta(days=7)
    next_dose_ref = future[0][0] if future else next_at

    all_doses = doses + future
    window_start = doses[0][0]
    total_h = int((window_end - window_start).total_seconds() // 3600)
    grid = [_concentration(all_doses, window_start + timedelta(hours=h)) for h in range(total_h + 1)]
    max_c = max(grid) if grid else 0.0

    c_now = _concentration(all_doses, now)
    c_prev = _concentration(all_doses, now - timedelta(hours=1))
    rising = c_now > c_prev
    level_pct = round(c_now / max_c * 100) if max_c > 0 else 0

    trough_at = None
    trough_pct = None
    if max_c > 0:
        lo_search = max(next_dose_ref - timedelta(hours=48), window_start)
        hrs = int((next_dose_ref - lo_search).total_seconds() // 3600)
        best_t, best_c = lo_search, _concentration(all_doses, lo_search)
        for h in range(1, hrs + 1):
            tt = lo_search + timedelta(hours=h)
            c = _concentration(all_doses, tt)
            if c < best_c:
                best_c, best_t = c, tt
        trough_at, trough_pct = best_t, round(best_c / max_c * 100)

    peak_at = None
    if max_c > 0:
        n = int((window_end - now).total_seconds() // 3600)
        future_vals = [_concentration(all_doses, now + timedelta(hours=h)) for h in range(n + 1)]
        for i in range(1, len(future_vals) - 1):
            if future_vals[i] >= future_vals[i - 1] and future_vals[i] >= future_vals[i + 1] \
                    and future_vals[i] > future_vals[0]:
                peak_at = now + timedelta(hours=i)
                break

    if level_pct >= 90:
        phase = "пик"
    elif rising:
        phase = "рост"
    elif trough_pct is not None and level_pct <= trough_pct + 5:
        phase = "минимум"
    else:
        phase = "спад"

    return {
        "level_pct": int(level_pct),
        "phase": phase,
        "peak_at": peak_at.strftime("%Y-%m-%d %H:%M") if peak_at else None,
        "trough_at": trough_at.strftime("%Y-%m-%d %H:%M") if trough_at else None,
        "trough_pct": int(trough_pct) if trough_pct is not None else None,
        "half_life_days": int(HALF_LIFE_DAYS),
    }


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    # 1) Пик одиночной дозы ≈ Tmax.
    fine = [h * 0.1 for h in range(0, 1000)]  # 0..100ч шагом 6 мин
    vals = [_bateman(1.0, h) for h in fine]
    peak_i = max(range(len(vals)), key=lambda i: vals[i])
    peak_t, peak_v = fine[peak_i], vals[peak_i]
    assert abs(peak_t - TMAX_H) <= 1.0, (peak_t, TMAX_H)
    print(f"OK: пик одиночной дозы на t={peak_t:.1f}ч (Tmax={TMAX_H}ч, ka={KA:.5f}, ke={KE:.5f})")

    # 2) Уровень через 120ч после пика — между 45% и 60% от пика.
    later = _bateman(1.0, peak_t + 120)
    ratio = later / peak_v
    assert 0.45 <= ratio <= 0.60, ratio
    print(f"OK: через 120ч после пика уровень {ratio*100:.1f}% от пика")

    # 3) Стационарный пик при еженедельном введении / пик одной дозы ≈ 1.61.
    weekly = [(timedelta(hours=168 * i), 1.0) for i in range(20)]
    weekly_doses = [(datetime(2000, 1, 1) + dt, d) for dt, d in weekly]
    last_dose_time = weekly_doses[-1][0]
    steady_peak = max(
        _concentration(weekly_doses, last_dose_time + timedelta(hours=h)) for h in fine
    )
    expected = 1 / (1 - math.exp(-KE * 168))
    got_ratio = steady_peak / peak_v
    assert abs(got_ratio - expected) < 0.08, (got_ratio, expected)
    assert abs(got_ratio - 1.61) < 0.08, got_ratio
    print(f"OK: стационарный пик/пик одной дозы = {got_ratio:.3f} (ожидание {expected:.3f})")

    # 4) profile() на in-memory БД с минимальными таблицами.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE med_log(id INTEGER PRIMARY KEY, user_id INT, at TEXT, substance TEXT,"
        " dose TEXT, unit TEXT, route TEXT);"
        "CREATE TABLE med_schedule(id INTEGER PRIMARY KEY, user_id INT, substance TEXT,"
        " dose REAL, every_days INT, next_at TEXT);"
    )
    base = datetime(2026, 8, 2, 22, 0, 0)  # воскресенье
    for i in range(5):
        at = base + timedelta(days=7 * i)
        conn.execute(
            "INSERT INTO med_log(user_id, at, substance, dose, unit, route) VALUES (1,?,?,?,?,?)",
            (at.strftime("%Y-%m-%d %H:%M:%S"), "Тирзепатид", "12.5", "mg", "injection"),
        )
    last_at = base + timedelta(days=7 * 4)
    next_at = last_at + timedelta(days=7)
    conn.execute(
        "INSERT INTO med_schedule(user_id, substance, dose, every_days, next_at) VALUES (1,?,?,7,?)",
        ("Тирзепатид", 12.5, next_at.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()

    now = last_at + timedelta(days=3)  # где-то в середине цикла
    p = profile(conn, 1, now=now)
    assert p is not None, "должен найтись профиль"
    assert 0 <= p["level_pct"] <= 100, p
    assert p["trough_at"] is not None
    trough_dt = datetime.strptime(p["trough_at"], "%Y-%m-%d %H:%M")
    assert abs((trough_dt - next_at).total_seconds()) <= 3600, (trough_dt, next_at)
    assert p["half_life_days"] == 5
    print(f"OK: profile() = {p}")

    # 5) Две инъекции за 24ч схлопываются в одну.
    d1 = datetime(2026, 1, 1, 22, 0, 0)
    d2 = d1 + timedelta(hours=24)
    coll = _collapse([(d1, 12.5), (d2, 12.5)])
    assert len(coll) == 1, coll
    assert coll[0][0] == d2, "должна остаться более поздняя запись"
    print("OK: инъекции ближе 72ч схлопываются в одну дозу")

    # 6) Нет инъекций за 8 недель -> None.
    conn2 = sqlite3.connect(":memory:")
    conn2.row_factory = sqlite3.Row
    conn2.executescript(
        "CREATE TABLE med_log(id INTEGER PRIMARY KEY, user_id INT, at TEXT, substance TEXT,"
        " dose TEXT, unit TEXT, route TEXT);"
        "CREATE TABLE med_schedule(id INTEGER PRIMARY KEY, user_id INT, substance TEXT,"
        " dose REAL, every_days INT, next_at TEXT);"
    )
    assert profile(conn2, 1, now=base) is None
    print("OK: без инъекций за 8 недель — None")

    conn.close()
    conn2.close()
    print("\nglp1.py: все проверки прошли")
    sys.exit(0)
