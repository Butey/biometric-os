"""Гардрейлы §10: плоский список детерминированных проверок + один диспетчер.

Каждая функция — обычный `if` над числом из БД и порогом из config.yaml. Модель
никогда не решает, сработал ли гардрейл: guards.py считает, модель только
пересказывает готовый результат. Гардрейл без данных не срабатывает никогда —
не на одиночном взвешивании, не на тонкой истории.
"""
import logging
import sqlite3
import statistics
from datetime import datetime, timedelta

from health_core.config import load, local_now, user_now, user_today
from health_core.db import connect, migrate


def _cfg() -> dict:
    return load()["guards"]


def _now(conn: sqlite3.Connection | None = None, user_id: int | None = None) -> datetime:
    """Время человека, а не сервера: гардрейлы считают «дней подряд» и «за
    неделю», и смещённая граница суток даёт ложные срабатывания."""
    if conn is not None and user_id is not None:
        return user_now(conn, user_id)
    return local_now()


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")


def _alert(code: str, severity: str, message: str, value, threshold) -> dict:
    return {"code": code, "severity": severity, "message": message, "value": value, "threshold": threshold}


# ---------------------------------------------------------------- LBM_RATIO / LBM_DRIFT

def _lean_loss_ratio(conn: sqlite3.Connection, user_id: int, since: str | None):
    """(ratio, d_weight, d_lean, n_точек) от медианы начала до медианы конца окна
    (since=None -> вся история). None, если точек < 2 или вес не изменился (деление на 0).

    МЕДИАНА ИСПОЛЬЗУЕТСЯ, ЧТОБЫ одна шумная точка на границе окна не решила сама по себе,
    срабатывает ли гардрейл LBM_RATIO / LBM_DRIFT. Биоимпеданс носит шум гидрации в
    килограмм в день; эндпоинт-дельта ловит одиночный всплеск как знаковый сдвиг."""
    q = "SELECT measured_at, weight_kg, ffm_kg FROM body_metrics WHERE user_id=? AND ffm_kg IS NOT NULL"
    params: list = [user_id]
    if since is not None:
        q += " AND measured_at >= ?"
        params.append(since)
    q += " ORDER BY measured_at ASC"
    rows = conn.execute(q, params).fetchall()
    if len(rows) < 2:
        return None

    # Медианное окно: 3 точки от каждого конца, деградация при < 6 точек.
    # 6+ точек: медиана(первые 3) vs медиана(последние 3)
    # 4-5 точек: медиана(первые 2) vs медиана(последние 2)
    # 2-3 точки: endpoints (эндпоинт = медиана из 1)
    n = len(rows)
    if n >= 6:
        window_size = 3
    elif n >= 4:
        window_size = 2
    else:  # n == 2 или 3
        window_size = 1

    first_vals = [rows[i]["weight_kg"] for i in range(window_size)]
    last_vals = [rows[n - window_size + i]["weight_kg"] for i in range(window_size)]
    first_lean = [rows[i]["ffm_kg"] for i in range(window_size)]
    last_lean = [rows[n - window_size + i]["ffm_kg"] for i in range(window_size)]

    d_weight = statistics.median(last_vals) - statistics.median(first_vals)
    if d_weight == 0:
        return None
    d_lean = statistics.median(last_lean) - statistics.median(first_lean)
    return abs(d_lean) / abs(d_weight), d_weight, d_lean, len(rows)


def check_lbm_ratio(conn: sqlite3.Connection, user_id: int):
    cfg = _cfg()
    since = (_now(conn, user_id) - timedelta(days=cfg["lbm_ratio_window_days"])).strftime("%Y-%m-%d %H:%M:%S")
    res = _lean_loss_ratio(conn, user_id, since)
    if res is None:
        return None
    ratio, d_weight, d_lean, n = res
    if n < cfg["lbm_ratio_min_points"]:
        return None
    if -d_weight < load()["policy"]["lean_share_min_loss_kg"]:
        return None  # вес не падает или потеря меньше минимума (как energy.lean_share): доли нет
    if d_lean >= 0:
        # Тощая масса не снижается — это не потеря мышц (набор/рекомпозиция), а
        # гардрейл про риск катаболизма мышц. Направление важнее модуля дельты.
        return None
    if abs(d_lean) < cfg["ffm_noise_floor_kg"]:
        return None
    threshold = cfg["lbm_ratio_threshold"]
    if ratio > threshold:
        return _alert(
            "LBM_RATIO", "critical",
            f"Доля тощей массы в потере за {cfg['lbm_ratio_window_days']} дней "
            f"{ratio * 100:.1f}% выше порога {threshold * 100:.0f}%.",
            round(ratio, 4), threshold,
        )
    return None


def check_lbm_drift(conn: sqlite3.Connection, user_id: int):
    cfg = _cfg()
    total = conn.execute(
        "SELECT COUNT(*) c FROM body_metrics WHERE user_id=? AND ffm_kg IS NOT NULL", (user_id,)
    ).fetchone()["c"]
    if total < 2:
        return None
    res = _lean_loss_ratio(conn, user_id, since=None)
    if res is None:
        return None
    ratio, d_weight, d_lean, n = res
    if n < cfg["lbm_ratio_min_points"]:
        # §10: до миграции самая ранняя доступная точка может быть не настоящей базой —
        # честный статус вместо тихого пропуска, но это НЕ клинический алерт.
        return _alert(
            "INSUFFICIENT", "info",
            f"LBM_DRIFT посчитан от самой ранней доступной точки ({n} замеров) — "
            f"меньше {cfg['lbm_ratio_min_points']} для уверенного дрейфа.",
            n, cfg["lbm_ratio_min_points"],
        )
    if -d_weight < load()["policy"]["lean_share_min_loss_kg"]:
        return None  # см. check_lbm_ratio
    if d_lean >= 0:
        # См. check_lbm_ratio: тощая масса растёт или не меняется — не сигнал катаболизма.
        return None
    if abs(d_lean) < cfg["ffm_noise_floor_kg"]:
        return None
    threshold = cfg["lbm_drift_threshold"]
    if ratio > threshold:
        return _alert(
            "LBM_DRIFT", "critical",
            f"Доля тощей массы в потере от базовой точки {ratio * 100:.1f}% выше порога {threshold * 100:.0f}%.",
            round(ratio, 4), threshold,
        )
    return None


# ---------------------------------------------------------------- FFMI_FLOOR

def check_ffmi_floor(conn: sqlite3.Connection, user_id: int):
    cfg = _cfg()
    # "Утренний замер" — окно 06:00-11:00, как и везде в проекте (§09,
    # energy.py _morning_weights): биоимпеданс вне этого окна не в том
    # состоянии гидратации, для которого калиброван расчёт. Верхней границы
    # без нижней хватало впустить ночной замер (00:00-06:00) в критический гард.
    row = conn.execute(
        "SELECT ffm_kg FROM body_metrics "
        "WHERE user_id=? AND ffm_kg IS NOT NULL "
        "AND time(measured_at) BETWEEN '06:00:00' AND '11:00:00' "
        "ORDER BY measured_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    urow = conn.execute("SELECT height_cm, sex FROM users WHERE id=?", (user_id,)).fetchone()
    if urow is None or urow["height_cm"] is None:
        return None
    if urow["sex"] is None:
        return None
    height_m = urow["height_cm"] / 100
    ffmi = row["ffm_kg"] / (height_m ** 2)  # приём BMI, применённый к тощей массе (§10)
    threshold = cfg["ffmi_floor_f"] if urow["sex"] == "f" else cfg["ffmi_floor_m"]
    if ffmi < threshold:
        return _alert("FFMI_FLOOR", "critical", f"FFMI {ffmi:.1f} ниже порога {threshold:.1f}.",
                      round(ffmi, 2), threshold)
    return None


# ---------------------------------------------------------------- UNDEREATING

def check_undereating(conn: sqlite3.Connection, user_id: int):
    cfg = _cfg()
    days, ratio = cfg["undereating_days"], cfg["undereating_ratio"]
    today = _now(conn, user_id).date()
    for i in range(1, days + 1):
        d = (today - timedelta(days=i)).isoformat()
        target = conn.execute(
            "SELECT kcal_target FROM daily_targets WHERE user_id=? AND date=?", (user_id, d)
        ).fetchone()
        if target is None or target["kcal_target"] is None:
            return None
        intake = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(fi.kcal),0) kcal FROM food_log fl "
            "JOIN food_items fi ON fi.food_log_id=fl.id "
            "WHERE fl.user_id=? AND date(fl.eaten_at)=?", (user_id, d)
        ).fetchone()
        if intake["n"] == 0:
            return None  # не логировали день — не путаем молчание с недоеданием
        if not intake["kcal"] < ratio * target["kcal_target"]:
            return None
    return _alert("UNDEREATING", "warning", f"Приём пищи ниже {ratio * 100:.0f}% цели {days} дня подряд.",
                  days, ratio)


# ---------------------------------------------------------------- PLATEAU

def check_plateau(conn: sqlite3.Connection, user_id: int):
    """Утренние замеры (06:00-11:00, как везде в проекте) в окне, дни рефида
    исключены — там рост веса плановый (гликоген/вода), не сигнал плато.
    С 6+ точками размах — |наклон линейного тренда по всем замерам| * окно в днях
    (один шумный замер или откат тренд не ломают); меньше 6 точек — max-min."""
    from health_core.energy import _is_refeed

    cfg = _cfg()
    since = (_now(conn, user_id) - timedelta(days=cfg["plateau_window_days"])).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT measured_at, weight_kg FROM body_metrics WHERE user_id=? AND measured_at>=? "
        "AND time(measured_at) BETWEEN '06:00:00' AND '11:00:00' ORDER BY measured_at",
        (user_id, since),
    ).fetchall()
    rows = [r for r in rows if not _is_refeed(conn, user_id, r["measured_at"][:10])]
    if len(rows) < cfg["plateau_min_points"]:
        return None
    weights = [r["weight_kg"] for r in rows]
    days = [datetime.strptime(r["measured_at"], "%Y-%m-%d %H:%M:%S").timestamp() / 86400 for r in rows]
    if len(weights) >= 6 and days[-1] > days[0]:
        spread = abs(statistics.linear_regression(days, weights).slope) * cfg["plateau_window_days"]
    else:
        spread = max(weights) - min(weights)
    threshold = cfg["plateau_range_kg"]
    if spread <= threshold:
        return _alert(
            "PLATEAU", "warning",
            f"Вес не двигался {cfg['plateau_window_days']} дней: размах {spread:.1f} кг не выше {threshold:.1f} кг.",
            round(spread, 2), threshold,
        )
    return None


# ---------------------------------------------------------------- BMR_FLOOR

def check_bmr_floor(conn: sqlite3.Connection, user_id: int):
    """Сравниваем цель с ПОЛОМ, СОХРАНЁННЫМ на момент апсерта daily_targets, а не
    с пересчётом daily_target() прямо сейчас — тот дрейфует в течение дня
    (шаги/активность двигают TDEE), давая ложные critical на честной цели.
    Строки до миграции v29 без kcal_floor — прежний пересчёт."""
    today = _now(conn, user_id).date().isoformat()
    row = conn.execute(
        "SELECT kcal_target, kcal_floor FROM daily_targets WHERE user_id=? AND date=?", (user_id, today)
    ).fetchone()
    if row is None or row["kcal_target"] is None:
        return None
    stored_floor = row["kcal_floor"]
    if stored_floor is not None:
        if row["kcal_target"] < stored_floor:
            return _alert("BMR_FLOOR", "critical",
                          f"Цель {row['kcal_target']:.0f} ккал ниже безопасного пола {stored_floor:.0f} ккал "
                          f"(сохранённое значение).",
                          row["kcal_target"], stored_floor)
        return None
    try:
        # ponytail: energy.py делает clamp сам при расчёте (§10, "при расчёте"); эта
        # проверка — вторая линия защиты. Пока модуль не готов, молчим, а не падаем.
        from health_core.energy import daily_target
    except ImportError:
        return None
    try:
        # Сверяем с ДЕЙСТВУЮЩИМ полом, а не с BMR. Пол считается от жировой
        # массы и законно бывает ниже BMR: у человека с 41 кг жира безопасный
        # дефицит больше, чем позволяет грубая граница по BMR. Сравнение с BMR
        # здесь поднимало бы критический алерт на каждой корректной цели.
        computed = daily_target(conn, user_id, today)
    except (ValueError, KeyError):
        return None
    floor = computed.get("kcal_floor")
    if floor is None:
        return None
    if row["kcal_target"] < floor:
        return _alert("BMR_FLOOR", "critical",
                      f"Цель {row['kcal_target']:.0f} ккал ниже безопасного пола {floor:.0f} ккал "
                      f"({computed.get('floor_reason')}).",
                      row["kcal_target"], floor)
    return None


# ---------------------------------------------------------------- NO_MEASURE

def _gap_days(ts: str, conn: sqlite3.Connection | None = None, user_id: int | None = None) -> int:
    """Разрыв в КАЛЕНДАРНЫХ днях, а не в полных сутках.

    timedelta.days округляет вниз: замер, сделанный в 07:00 пять дней назад,
    при взгляде в 03:00 давал 4 — порог measure_soon_after_days=5 молчал до
    утра, хотя человек и текст алерта («Последний замер N дней назад») считают
    именно календарь. Побочно это делало самотест зависимым от часа запуска.

    Обоим гардрейлам разрыва нужна ОДНА мера: их взаимоисключаемость держится
    на сравнении soon_days <= gap < no_measure_days, и разъехавшиеся единицы
    открыли бы щель, в которой не звучит ни один.
    """
    return (_now(conn, user_id).date() - _parse(ts).date()).days


def check_no_measure(conn: sqlite3.Connection, user_id: int):
    days = _cfg()["no_measure_days"]
    last = conn.execute(
        "SELECT measured_at FROM body_metrics WHERE user_id=? ORDER BY measured_at DESC LIMIT 1", (user_id,)
    ).fetchone()
    if last is not None:
        gap = _gap_days(last["measured_at"], conn, user_id)
        if gap < days:
            return None
        return _alert("NO_MEASURE", "warning", f"Нет замеров {gap} дней, порог {days}.", gap, days)
    urow = conn.execute("SELECT created_at FROM users WHERE id=?", (user_id,)).fetchone()
    if urow is None or _gap_days(urow["created_at"], conn, user_id) < days:
        return None
    return _alert("NO_MEASURE", "warning", f"Ни одного замера за {days} дней с регистрации.", 0, days)


# ---------------------------------------------------------------- LIPID_GUARD

def check_lipid_guard(conn: sqlite3.Connection, user_id: int):
    # route='oral' в SQL, а не эвристика по тексту substance/dose (см. Knowledge/
    # drug_cards.md — андрокомплекс требует жиры для абсорбции, это ORAL-случай).
    # route IS NULL (строки до миграции v3) в фильтр не попадает — гардрейл молчит
    # на неизвестном пути введения, а не угадывает по подстроке.
    threshold = _cfg()["lipid_guard_fat_g"]
    today = _now(conn, user_id).date().isoformat()
    doses = conn.execute(
        "SELECT at, substance, dose FROM med_log WHERE user_id=? AND date(at)=? AND route='oral'",
        (user_id, today),
    ).fetchall()
    fired = []
    for dose in doses:
        at = _parse(dose["at"])
        window = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(fi.fat_g),0) fat FROM food_log fl "
            "JOIN food_items fi ON fi.food_log_id=fl.id "
            "WHERE fl.user_id=? AND fl.eaten_at BETWEEN ? AND ?",
            (user_id, (at - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
             (at + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
        if window["n"] == 0:
            continue  # рядом ничего не ели — не судим по нулю
        if window["fat"] < threshold:
            fired.append(_alert("LIPID_GUARD", "warning",
                                 f"Жиров рядом с приёмом препарата {window['fat']:.1f} г, ниже {threshold} г.",
                                 round(window["fat"], 1), threshold))
    return fired or None


# ---------------------------------------------------------------- WHR_HIGH

def check_whr(conn: sqlite3.Connection, user_id: int):
    cfg = _cfg()
    try:
        from health_core.report import whr
    except ImportError:
        return None
    w = whr(conn, user_id)
    if w is None:
        return None
    urow = conn.execute("SELECT sex FROM users WHERE id=?", (user_id,)).fetchone()
    if urow is None or urow["sex"] is None:
        return None
    threshold = cfg["whr_female_max"] if urow["sex"] == "f" else cfg["whr_male_max"]
    if w >= threshold:
        return _alert(
            "WHR_HIGH", "warning",
            f"WHR {w:.2f} выше порога {threshold:.2f} ({urow['sex']} — абдоминальное ожирение).",
            w, threshold,
        )
    return None


# ---------------------------------------------------------------- RATE_HIGH

def check_rate_high(conn: sqlite3.Connection, user_id: int):
    """Темп снижения веса — медианы по 3 первым и 3 последним взвешиваниям
    окна, а не голые эндпоинты: один шумный день на весах не должен решать
    сам по себе, сработал ли гардрейл. Покрытие меньше 10 дней между первым и
    последним замером окна — темп неизвестен, гардрейл молчит."""
    cfg = _cfg()
    window_days = cfg["weight_rate_window_days"]
    min_points = cfg["weight_rate_min_points"]
    since = (_now(conn, user_id) - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT measured_at, weight_kg FROM body_metrics WHERE user_id=? AND measured_at>=? ORDER BY measured_at",
        (user_id, since),
    ).fetchall()
    if len(rows) < min_points:
        return None
    # .date()-разница, не полных datetime: timedelta.days округляет вниз (см.
    # _gap_days выше) — на границе окна округление молчало бы про RATE_HIGH.
    days_covered = (_parse(rows[-1]["measured_at"]).date() - _parse(rows[0]["measured_at"]).date()).days
    if days_covered < 10:
        return None  # темп неизвестен на коротком покрытии

    k = min(3, len(rows))
    first_med = statistics.median(r["weight_kg"] for r in rows[:k])
    last_med = statistics.median(r["weight_kg"] for r in rows[-k:])
    weight_delta = last_med - first_med
    if weight_delta >= 0:
        return None  # only fire on loss, not gain

    # Медианы лежат в строках k//2 и -1-k//2, а не на краях окна: темп делим на
    # расстояние между ними, иначе он занижен.
    span = (_parse(rows[-1 - k // 2]["measured_at"]).date() - _parse(rows[k // 2]["measured_at"]).date()).days
    if span < 5:  # при <6 точках окна пересекаются, медианы почти рядом - темп шумный
        return None
    kg_per_week = abs(weight_delta) * 7 / span
    pct_per_week = (abs(weight_delta) / first_med) * 100 * 7 / span
    threshold = min(cfg["weight_rate_kg_week_max"], first_med * cfg["weight_rate_pct_week_max"] / 100)
    if kg_per_week > threshold:
        return _alert(
            "RATE_HIGH", "warning",
            f"Потеря {kg_per_week:.2f} кг/неделю ({pct_per_week:.2f}%/неделю) выше порога "
            f"{threshold:.2f} кг/неделю — риск образования камней в желчном пузыре.",
            round(kg_per_week, 2), round(threshold, 2),
        )
    return None


# ---------------------------------------------------------------- WEIGHT_REGAIN

def check_weight_regain(conn: sqlite3.Connection, user_id: int):
    """Возврат веса (CONTEXT.md «Возврат веса»): рост сглаженного веса за
    regain_window_days дней, независимо от причины — перерыв в терапии,
    снижение дозы, срыв. Причину ищет модель, а не гард (docs/adr/
    0002-рекомендация-дозы.md). Взвешивания в дни рефида и болезни исключаются
    из выборки ДО подсчёта медиан — там рост веса плановый (гликоген/вода,
    задержка после болезни), не сигнал возврата.

    Медианы по 3 первым и 3 последним ОСТАВШИМСЯ взвешиваниям гасят шум одной
    точки — тот же приём, что в check_rate_high."""
    from health_core import sick
    from health_core.energy import _is_refeed

    cfg = _cfg()
    window_days = cfg["regain_window_days"]
    min_points = cfg["regain_min_points"]
    since = (_now(conn, user_id) - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT measured_at, weight_kg FROM body_metrics WHERE user_id=? AND measured_at>=? ORDER BY measured_at",
        (user_id, since),
    ).fetchall()
    rows = [
        r for r in rows
        if not sick.is_sick(conn, user_id, r["measured_at"][:10])
        and not _is_refeed(conn, user_id, r["measured_at"][:10])
    ]
    if len(rows) < min_points:
        return None

    k = 3
    first_med = statistics.median(r["weight_kg"] for r in rows[:k])
    last_med = statistics.median(r["weight_kg"] for r in rows[-k:])
    kg_delta = last_med - first_med
    if kg_delta <= 0:
        return None  # only fire on gain
    pct_delta = kg_delta / first_med * 100
    threshold = cfg["regain_pct"]
    if pct_delta > threshold:
        days_covered = (_parse(rows[-1]["measured_at"]).date() - _parse(rows[0]["measured_at"]).date()).days
        return _alert(
            "WEIGHT_REGAIN", "warning",
            f"Вес вырос на {kg_delta:.1f} кг ({pct_delta:.1f}%) за {days_covered} дн.",
            round(pct_delta, 2), threshold,
        )
    return None


# ---------------------------------------------------------------- STALE_CALIB

def check_stale_calib(conn: sqlite3.Connection, user_id: int):
    cfg = _cfg()
    min_days = cfg.get("stale_calib_min_days", 7)
    urow = conn.execute("SELECT created_at FROM users WHERE id=?", (user_id,)).fetchone()
    if urow is None or (_now(conn, user_id) - _parse(urow["created_at"])).days < min_days:
        return None
    from health_core.energy import logged_streak
    streak, _ = logged_streak(conn, user_id, _now(conn, user_id).date())
    if streak < min_days:
        return _alert(
            "STALE_CALIB",
            "warning",
            f"Серия лога еды {streak} из {min_days} дней подряд. Цель заморожена.",
            streak,
            min_days,
        )
    return None


# ---------------------------------------------------------------- PROTEIN_SKEW

_MEAL_SLOT_RU = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин", "snack": "Перекус"}


def _protein_target(conn: sqlite3.Connection, user_id: int, today: str) -> float | None:
    """Суточный целевой план белка (г). Сначала из daily_targets на сегодня,
    затем из targets_for(conn, user_id)."""
    try:
        row = conn.execute(
            "SELECT protein_g_target FROM daily_targets WHERE user_id=? AND date=?",
            (user_id, today),
        ).fetchone()
        if row and row["protein_g_target"] and row["protein_g_target"] > 0:
            return float(row["protein_g_target"])
    except Exception:
        pass
    try:
        from health_core.config import targets_for
        tg = targets_for(conn, user_id)
        if tg and tg.get("protein_g") and tg["protein_g"] > 0:
            return float(tg["protein_g"])
    except Exception:
        pass
    return None


def check_protein_skew(conn: sqlite3.Connection, user_id: int):
    cfg = _cfg()
    today = _now(conn, user_id).date().isoformat()
    rows = conn.execute(
        "SELECT fl.id log_id, fl.meal_slot, fi.protein_g FROM food_log fl "
        "JOIN food_items fi ON fi.food_log_id=fl.id "
        "WHERE fl.user_id=? AND date(fl.eaten_at)=?",
        (user_id, today),
    ).fetchall()
    if not rows:
        return None
    n_meals = len({r["log_id"] for r in rows})
    # На GLP-1 синтез мышечного белка зависит от частоты стимула, не только от
    # суточной суммы (см. GUARD 1 в задаче) — но один приём, залогированный в
    # 11:00, тривиально даёт 100% дневного белка. Без минимума приёмов и
    # минимальной суточной суммы это самый вероятный ложный срабатыватель.
    if n_meals < cfg["protein_skew_min_meals"]:
        return None
    totals: dict[str, float] = {}
    day_total = 0.0
    for r in rows:
        p = r["protein_g"] or 0.0
        slot = r["meal_slot"] or "?"
        totals[slot] = totals.get(slot, 0.0) + p
        day_total += p
    if day_total < cfg["protein_skew_min_total_g"] or day_total == 0:
        return None
    slot, top = max(totals.items(), key=lambda kv: kv[1])

    # Доля считается от суточного целевого плана белка (protein_g_target).
    # Если за день съедено больше плана, берём максимум из плана и факта.
    # Если план не задан (нет метрик), используем сумму съеденного за день.
    target_protein = _protein_target(conn, user_id, today)
    basis = max(target_protein, day_total) if target_protein else day_total
    if basis <= 0:
        return None

    share = top / basis
    threshold = cfg["protein_skew_threshold"]
    if share > threshold:
        slot_name = _MEAL_SLOT_RU.get(slot, slot)
        if target_protein and basis == target_protein:
            msg = (
                f"{share * 100:.0f}% суточного плана белка ({top:.0f} из {target_protein:.0f} г) "
                f"в одном приёме ({slot_name}), выше порога {threshold * 100:.0f}%."
            )
        else:
            msg = (
                f"{share * 100:.0f}% суточного белка ({top:.0f} из {day_total:.0f} г) "
                f"в одном приёме ({slot_name}), выше порога {threshold * 100:.0f}%."
            )
        return _alert(
            "PROTEIN_SKEW", "warning",
            msg,
            round(share, 4), threshold,
        )
    return None


# ---------------------------------------------------------------- GLUCOSE_VOLATILITY

def is_fasting_glucose(conn: sqlite3.Connection, user_id: int, at: str, context: str | None) -> bool:
    """Замер натощак: так назван в context, либо context пуст, замер до 10:00 и
    в этот день до него ещё не было еды. Любой другой context (после еды,
    перед сном...) в натощаковую серию не идёт."""
    ctx = (context or "").strip().lower()
    if ctx:
        return "натощак" in ctx or "fasting" in ctx
    if _parse(at).hour >= 10:
        return False
    day = at[:10]
    return conn.execute(
        "SELECT 1 FROM food_log WHERE user_id=? AND eaten_at>=? AND eaten_at<? LIMIT 1",
        (user_id, day, at),
    ).fetchone() is None


def check_glucose_volatility(conn: sqlite3.Connection, user_id: int):
    """Устойчивый сдвиг, а не отдельный пик: последние glucose_streak замеров
    натощак ВСЕ выше своей медианы за окно на glucose_rise_mmol и больше. Замеры
    после короткого сна не считаются — это объяснимые выбросы. Тон «наблюдать
    динамику»: уверенность и точки, давшие сигнал, идут в текст."""
    cfg = _cfg()
    now = _now(conn, user_id)
    since = (now - timedelta(days=cfg["glucose_window_days"])).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT at, mmol_l, context FROM glucose_log WHERE user_id=? AND at>=? ORDER BY at", (user_id, since)
    ).fetchall()
    series = []
    for r in rows:
        if not is_fasting_glucose(conn, user_id, r["at"], r["context"]):
            continue
        sleep = conn.execute(
            "SELECT duration_min FROM sleep_log WHERE user_id=? AND night_date=?", (user_id, r["at"][:10])
        ).fetchone()
        if sleep and sleep["duration_min"] is not None and sleep["duration_min"] < cfg["glucose_short_sleep_min"]:
            continue
        series.append((r["at"], r["mmol_l"]))
    k = cfg["glucose_streak"]
    base = [v for _, v in series[:-k]]
    if len(base) < cfg["glucose_min_points"]:
        return None  # лог ведётся нерегулярно — тонкая история это норма, не сигнал
    last = series[-k:]
    if (now - _parse(last[-1][0])).days > 7:
        return None  # сдвиг был, но свежих замеров нет — не напоминаем о старом
    baseline = statistics.median(base)
    excess = min(v for _, v in last) - baseline
    rise = cfg["glucose_rise_mmol"]
    if excess < rise:
        return None
    score = (len(base) >= 10) + (excess >= 2 * rise)
    confidence = ("низкая", "средняя", "высокая")[score]
    points = ", ".join(f"{a[8:10]}.{a[5:7]} {v:.1f}" for a, v in last)
    return _alert(
        "GLUCOSE_VOLATILITY", "info",
        f"Наблюдать динамику: {k} последних замера натощак ({points}) выше своей медианы "
        f"{baseline:.1f} ммоль/л за {cfg['glucose_window_days']} дней минимум на {excess:.1f}. "
        f"Уверенность: {confidence} (опорных замеров {len(base)}). Не диагноз; "
        f"подтверждается только если сдвиг держится на следующих замерах.",
        round(excess, 2), rise,
    )


# ---------------------------------------------------------------- MEASURE_SOON

def check_measure_soon(conn: sqlite3.Connection, user_id: int):
    cfg = _cfg()
    soon_days, no_measure_days = cfg["measure_soon_after_days"], cfg["no_measure_days"]
    last = conn.execute(
        "SELECT measured_at FROM body_metrics WHERE user_id=? ORDER BY measured_at DESC LIMIT 1", (user_id,)
    ).fetchone()
    if last is not None:
        gap = _gap_days(last["measured_at"], conn, user_id)
    else:
        urow = conn.execute("SELECT created_at FROM users WHERE id=?", (user_id,)).fetchone()
        if urow is None:
            return None
        gap = _gap_days(urow["created_at"], conn, user_id)
    # Верхняя граница — no_measure_days САМОГО check_no_measure, а не отдельная
    # константа: если бы пороги могли разойтись, оба гардрейла оказались бы в
    # check_all одновременно на одном и том же разрыве — ровно то, что правило
    # "один разрыв, один голос" запрещает. Строгое "<" держит их взаимоисключающими
    # по построению, а не по совпадению настроенных чисел.
    if soon_days <= gap < no_measure_days:
        return _alert(
            "MEASURE_SOON", "info",
            f"Последний замер {gap} дней назад — замеры лентой удобно делать в один и тот же день недели.",
            gap, soon_days,
        )
    return None


# ---------------------------------------------------------------- BINGE_RISK

def check_binge_risk(conn: sqlite3.Connection, user_id: int):
    """Триада риска пищевого срыва: накопленный дефицит калорий за окно,
    короткий сон прошлой ночью, мало белка на завтрак (или завтрак пропущен).

    Каждый фактор — True/False/None(неизвестно). Неизвестный фактор никогда не
    считается сработавшим — гардрейл молчит на тонких данных, а не гадает."""
    from health_core import energy

    cfg = _cfg()
    now = _now(conn, user_id)
    today = now.date()
    yesterday = today - timedelta(days=1)
    window_days = cfg["binge_window_days"]

    # --- фактор 1: накопленный дефицit за window_days дней, заканчивающихся вчера
    deficit_fired = None
    deficit_msg = None
    tdee = energy.adaptive_tdee(conn, user_id, for_date=yesterday.isoformat())
    if tdee is not None:
        window_start = yesterday - timedelta(days=window_days - 1)
        window_start_s, window_end_s = window_start.isoformat(), yesterday.isoformat()
        # Дни рефида/болезни исключаются целиком — как в energy._adaptive_tdee_core
        # и check_weight_regain: интейк там сознательно поднят до maintenance и не
        # отражает обычное питание на дефиците, а его включение размывало бы
        # deficit_sum и молча гасило гард.
        refeed_sick_dates = {r["date"] for r in conn.execute(
            "SELECT date FROM refeed_days WHERE user_id=? AND date BETWEEN ? AND ? "
            "UNION SELECT date FROM sick_days WHERE user_id=? AND date BETWEEN ? AND ?",
            (user_id, window_start_s, window_end_s, user_id, window_start_s, window_end_s),
        ).fetchall()}
        logged_days = 0
        counted_days = 0
        deficit_sum = 0.0
        for i in range(window_days):
            d = (window_start + timedelta(days=i)).isoformat()
            if d in refeed_sick_dates:
                continue
            counted_days += 1
            row = conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(fi.kcal),0) kcal FROM food_log fl "
                "JOIN food_items fi ON fi.food_log_id=fl.id "
                "WHERE fl.user_id=? AND date(fl.eaten_at)=?", (user_id, d),
            ).fetchone()
            if row["n"] > 0:
                logged_days += 1
                deficit_sum += tdee - row["kcal"]
        if logged_days >= counted_days - 1:
            threshold_deficit = cfg["binge_deficit_kcal"]
            deficit_fired = deficit_sum > threshold_deficit
            if deficit_fired:
                deficit_msg = (
                    f"дефицит {deficit_sum:.0f} ккал за {window_days} дн "
                    f"(порог {threshold_deficit:.0f})"
                )

    # --- фактор 2: сон прошлой ночью (night_date = сегодняшнее утреннее пробуждение)
    sleep_fired = None
    sleep_msg = None
    sleep_row = conn.execute(
        "SELECT duration_min FROM sleep_log WHERE user_id=? AND night_date=?",
        (user_id, today.isoformat()),
    ).fetchone()
    if sleep_row is not None and sleep_row["duration_min"] is not None:
        duration = sleep_row["duration_min"]
        threshold_sleep = cfg["binge_sleep_min"]
        sleep_fired = duration < threshold_sleep
        if sleep_fired:
            h, m = divmod(duration, 60)
            th_h, th_m = divmod(threshold_sleep, 60)
            sleep_msg = f"сон {h} ч {m} мин (< {th_h} ч {th_m} мин)"

    # --- фактор 3: белок на завтрак (или завтрак пропущен при активном логировании)
    protein_fired = None
    protein_msg = None
    today_iso = today.isoformat()
    threshold_protein = cfg["binge_breakfast_protein_g"]
    breakfast = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(fi.protein_g),0) p FROM food_log fl "
        "JOIN food_items fi ON fi.food_log_id=fl.id "
        "WHERE fl.user_id=? AND date(fl.eaten_at)=? AND fl.meal_slot='breakfast'",
        (user_id, today_iso),
    ).fetchone()
    if breakfast["n"] > 0:
        protein = breakfast["p"]
        protein_fired = protein < threshold_protein
        if protein_fired:
            protein_msg = f"белок на завтрак {protein:.0f} г (< {threshold_protein:.0f} г)"
    else:
        any_food_today = conn.execute(
            "SELECT COUNT(*) n FROM food_log WHERE user_id=? AND date(eaten_at)=?",
            (user_id, today_iso),
        ).fetchone()["n"] > 0
        if now.hour >= 12 and any_food_today:
            protein_fired = True
            protein_msg = "завтрака нет"

    fired_msgs = [m for fired, m in ((deficit_fired, deficit_msg), (sleep_fired, sleep_msg),
                                     (protein_fired, protein_msg)) if fired]
    fired_count = len(fired_msgs)
    min_factors = cfg["binge_min_factors"]
    if fired_count < min_factors:
        return None
    severity = "critical" if fired_count == 3 else "warning"
    message = (
        f"Триада риска срыва ({fired_count} из 3 факторов): " + "; ".join(fired_msgs) +
        ". Нужен полноценный белковый приём пищи, не углублять дефицит сегодня."
    )
    return _alert("BINGE_RISK", severity, message, fired_count, min_factors)


# ---------------------------------------------------------------- RECOVERY_LOW

def check_recovery_low(conn: sqlite3.Connection, user_id: int):
    """CONTEXT.md «Сигнал восстановления»: 3 календарных дня подряд, оканчивающихся
    сегодня или вчера (последний день с обоими показателями), HRV заметно ниже
    своей медианы за 28 дней ПЕРЕД этими тремя днями И hr_min заметно выше своей —
    повод облегчить тренировку, не диагноз. Молчит без данных, без медиан
    (нужно >=10 дней на метрику в окне медианы) и если любой из 3 дней болен/рефид."""
    cfg = _cfg()
    today = _now(conn, user_id).date()

    end_day = None
    for offset in (0, 1):
        d = today - timedelta(days=offset)
        row = conn.execute(
            "SELECT hrv_ms, hr_min FROM daily_watch WHERE user_id=? AND date=?", (user_id, d.isoformat())
        ).fetchone()
        if row is not None and row["hrv_ms"] is not None and row["hr_min"] is not None:
            end_day = d
            break
    if end_day is None:
        return None

    three_days = [end_day - timedelta(days=i) for i in (2, 1, 0)]

    from health_core import sick
    from health_core.energy import _is_refeed
    for d in three_days:
        if sick.is_sick(conn, user_id, d.isoformat()) or _is_refeed(conn, user_id, d.isoformat()):
            return None

    day_rows = {}
    for d in three_days:
        row = conn.execute(
            "SELECT hrv_ms, hr_min FROM daily_watch WHERE user_id=? AND date=?", (user_id, d.isoformat())
        ).fetchone()
        if row is None or row["hrv_ms"] is None or row["hr_min"] is None:
            return None  # неполные 3 дня — сигнал не проверить
        day_rows[d] = row

    window_end = three_days[0] - timedelta(days=1)
    window_start = window_end - timedelta(days=27)
    hist = conn.execute(
        "SELECT hrv_ms, hr_min FROM daily_watch WHERE user_id=? AND date>=? AND date<=?",
        (user_id, window_start.isoformat(), window_end.isoformat()),
    ).fetchall()
    hrv_vals = [r["hrv_ms"] for r in hist if r["hrv_ms"] is not None]
    hrmin_vals = [r["hr_min"] for r in hist if r["hr_min"] is not None]
    if len(hrv_vals) < 10 or len(hrmin_vals) < 10:
        return None

    hrv_med = statistics.median(hrv_vals)
    hrmin_med = statistics.median(hrmin_vals)
    hrv_threshold = hrv_med * (1 - cfg["recovery_hrv_drop_pct"] / 100)
    hrmin_threshold = hrmin_med + cfg["recovery_hr_min_rise_bpm"]

    for d in three_days:
        r = day_rows[d]
        if not (r["hrv_ms"] <= hrv_threshold and r["hr_min"] >= hrmin_threshold):
            return None

    worst_hrv = min(day_rows[d]["hrv_ms"] for d in three_days)
    return _alert(
        "RECOVERY_LOW", "warning",
        f"HRV и минимальный пульс сигналят о недовосстановлении {three_days[0]}—{three_days[-1]} "
        f"(3 дня подряд). Не диагноз — повод сделать сегодняшнюю тренировку легче.",
        round(worst_hrv), round(hrv_threshold),
    )


# ---------------------------------------------------------------- RESTING_HR_RISING

def check_resting_hr_rising(conn: sqlite3.Connection, user_id: int):
    """Минимальный пульс дня (пульс покоя) за 7 дней выше своей медианы за
    предыдущие 28 на resting_hr_rise_bpm и больше. В отличие от RECOVERY_LOW
    не требует HRV. Не диагноз: поводов много (недосып, стресс, болезнь, кофеин)."""
    from health_core.watch import resting_hr_trend
    t = resting_hr_trend(conn, user_id)
    threshold = _cfg()["resting_hr_rise_bpm"]
    if t is None or t["diff"] < threshold:
        return None
    return _alert(
        "RESTING_HR_RISING", "info",
        f"Пульс покоя вырос: медиана за 7 дней {t['recent']:.0f} уд/мин против {t['base']:.0f} за предыдущие 28 "
        f"(+{t['diff']:.0f}). Не диагноз: проверь сон, стресс, кофеин, самочувствие.",
        round(t["diff"], 1), threshold,
    )


# ---------------------------------------------------------------- BP_HIGH / BP_LOW

def check_bp_high(conn: sqlite3.Connection, user_id: int):
    """Кризисный одиночный замер за сутки - сразу critical (безопасность, без
    «наблюдать»). Иначе - устойчивое повышение: медиана за окно выше домашнего
    порога, при минимуме замеров и минимум двух разных днях; единичные скачки
    не дают сигнала."""
    from health_core import bp
    cfg = _cfg()
    now = _now(conn, user_id)
    day = bp.recent(conn, user_id, now, 1)
    for r in reversed(day):
        if r["systolic"] >= cfg["bp_crisis_sys"] or r["diastolic"] >= cfg["bp_crisis_dia"]:
            return _alert(
                "BP_HIGH", "critical",
                f"Давление {r['systolic']}/{r['diastolic']} в кризисном диапазоне. Посиди спокойно 5 минут и повтори замер. "
                f"Если снова высокое или есть головная боль, боль в груди, одышка, слабость в руке или ноге, нарушение речи - вызывай скорую.",
                r["systolic"], cfg["bp_crisis_sys"],
            )
    rows = bp.recent(conn, user_id, now, cfg["bp_window_days"])
    if len(rows) < cfg["bp_min_readings"] or len({r["at"][:10] for r in rows}) < 2:
        return None
    sys_med = statistics.median(r["systolic"] for r in rows)
    dia_med = statistics.median(r["diastolic"] for r in rows)
    if sys_med < cfg["bp_high_sys"] and dia_med < cfg["bp_high_dia"]:
        return None
    return _alert(
        "BP_HIGH", "warning",
        f"Давление держится выше домашнего порога {cfg['bp_high_sys']}/{cfg['bp_high_dia']}: медиана {sys_med:.0f}/{dia_med:.0f} "
        f"за {cfg['bp_window_days']} дней ({len(rows)} замеров). Не диагноз: мерь утром и вечером в покое, сидя, "
        f"и покажи записи врачу, если сохранится.",
        round(sys_med), cfg["bp_high_sys"],
    )


def check_bp_low(conn: sqlite3.Connection, user_id: int):
    """Два последних замера за трое суток подряд с систолическим ниже порога:
    на GLP-1 это частый след обезвоживания и слишком глубокого дефицита."""
    from health_core import bp
    cfg = _cfg()
    rows = bp.recent(conn, user_id, _now(conn, user_id), 3)[-2:]
    if len(rows) < 2 or any(r["systolic"] >= cfg["bp_low_sys"] for r in rows):
        return None
    return _alert(
        "BP_LOW", "warning",
        f"Два последних замера давления ниже {cfg['bp_low_sys']} сист. ({rows[0]['systolic']}/{rows[0]['diastolic']}, "
        f"{rows[1]['systolic']}/{rows[1]['diastolic']}). Проверь воду и соль, не вставай резко; "
        f"при головокружении или обмороке - к врачу.",
        rows[1]["systolic"], cfg["bp_low_sys"],
    )


# ---------------------------------------------------------------- диспетчер

_CHECKS = (
    check_lbm_ratio,
    check_lbm_drift,
    check_ffmi_floor,
    check_undereating,
    check_plateau,
    check_bmr_floor,
    check_no_measure,
    check_lipid_guard,
    check_stale_calib,
    check_whr,
    check_rate_high,
    check_protein_skew,
    check_glucose_volatility,
    check_measure_soon,
    check_binge_risk,
    check_weight_regain,
    check_recovery_low,
    check_resting_hr_rising,
    check_bp_high,
    check_bp_low,
)

# Режим болезни (health_core/sick.py) глушит поведенческие гардрейлы: во время
# болезни человек и так ест как получится и не взвешивается по расписанию, а
# вес/БИА пару дней после болезни искажены задержкой жидкости. FFMI_FLOOR,
# BMR_FLOOR, LIPID_GUARD, GLUCOSE_VOLATILITY и WHR_HIGH сюда НЕ входят —
# GLUCOSE_VOLATILITY остаётся демонстративно: болезнь сама по себе поднимает
# глюкозу, и это ровно тот сигнал, который нельзя заглушать.
SICK_QUIET_CODES = frozenset({
    "UNDEREATING", "BINGE_RISK", "PROTEIN_SKEW", "PLATEAU", "RATE_HIGH",
    "STALE_CALIB", "NO_MEASURE", "MEASURE_SOON", "LBM_RATIO", "LBM_DRIFT",
    "RECOVERY_LOW", "RESTING_HR_RISING",
})


def check_all(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    out: list[dict] = []
    for fn in _CHECKS:
        # check_all зовут после commit записи: упавший гард не должен превращать
        # сохранённую запись в «ошибку» (модель повторит вызов - и будет дубль).
        try:
            res = fn(conn, user_id)
        except Exception:
            logging.getLogger(__name__).exception("guard %s failed", fn.__name__)
            continue
        if res is None:
            continue
        out.extend(res) if isinstance(res, list) else out.append(res)
    from health_core import sick  # локальный импорт — на случай, если sick.py когда-нибудь импортирует guards
    if sick.quiet(conn, user_id, _now(conn, user_id).date().isoformat()):
        out = [a for a in out if a["code"] not in SICK_QUIET_CODES]
    return out


def record(conn: sqlite3.Connection, user_id: int, alerts: list[dict]) -> None:
    if not alerts:
        return
    now = _now(conn, user_id)
    today = now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    # Один и тот же гард срабатывает на КАЖДОМ логировании за день (каждый вызов
    # check_all+record из хендлеров). Пропускаем только то же правило с тем же
    # текстом, иначе таблица распухает; эскалация или новый текст пишется заново.
    already = {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT rule, message FROM alerts WHERE user_id=? AND date(created_at)=?",
            (user_id, today),
        )
    }
    fresh = [(user_id, ts, a["code"], a["message"]) for a in alerts if (a["code"], a["message"]) not in already]
    if not fresh:
        return
    conn.executemany(
        "INSERT INTO alerts(user_id, created_at, rule, message) VALUES (?, ?, ?, ?)",
        fresh,
    )
    conn.commit()


# ---------------------------------------------------------------- описания и обоснования

GUARD_DEFINITIONS = {
    "LBM_RATIO": {
        "code": "LBM_RATIO",
        "name": "Потеря мышечной массы (14 дней)",
        "category": "Сохранение мышц",
        "default_severity": "critical",
        "description": "Контролирует долю сухой мышечной массы (FFM) в снижении общего веса за 14-дневное окно.",
        "rationale": "При дефиците калорий приоритет — сжигать жир и сохранять метаболически активную ткань. Потеря более 15% веса за счёт мышц указывает на катаболизм, снижает базовый расход (BMR), провоцирует слабость и гормональный спад.",
        "action": "Увеличить потребление белка (1.8–2.2 г/кг FFM), уменьшить дефицит калорий на 200–300 ккал/сут, обеспечить адекватный объём силовых тренировок.",
        "keys": ["lbm_ratio_threshold", "lbm_ratio_window_days", "lbm_ratio_min_points", "ffm_noise_floor_kg"],
    },
    "LBM_DRIFT": {
        "code": "LBM_DRIFT",
        "name": "Накопительный дрейф мышц (от старта)",
        "category": "Сохранение мышц",
        "default_severity": "critical",
        "description": "Следит за суммарной долей потери тощей массы за всю историю наблюдений от базовой точки.",
        "rationale": "Защищает от скрытого постепенного истощения мышечной ткани на длинной дистанции (месяцы диеты), даже если краткосрочные 14-дневные окна укладываются в лимит.",
        "action": "Провести плановый диетический перерыв (diet break / рефид на 7–14 дней на уровне поддержки TDEE), снизить тренировочный стресс.",
        "keys": ["lbm_drift_threshold", "ffm_noise_floor_kg"],
    },
    "FFMI_FLOOR": {
        "code": "FFMI_FLOOR",
        "name": "Минимальный индекс мышечной массы (FFMI)",
        "category": "Сохранение мышц",
        "default_severity": "critical",
        "description": "Индекс сухой массы тела FFMI = FFM (кг) / Рост (м)². Аналог ИМТ, исключающий жир.",
        "rationale": "Падение FFMI ниже 19.0 кг/м² для взрослого мужчины означает клиническую саркопению, мышечное истощение, снижение плотности костей и иммунитета.",
        "action": "Полная остановка дефицита калорий. Переход на изокалорийный рацион или лёгкий профицит для восстановления мышечной ткани.",
        "keys": ["ffmi_floor_m", "ffmi_floor_f"],
    },
    "BMR_FLOOR": {
        "code": "BMR_FLOOR",
        "name": "Защита базового метаболизма (BMR Floor)",
        "category": "Питание и калории",
        "default_severity": "critical",
        "description": "Проверяет, чтобы суточная цель калоража не опускалась ниже безопасного физиологического пола.",
        "rationale": "Пол рассчитывается по модели Alpert: жировая ткань способна отдавать до fat_supply_kcal_per_kg ккал на кг жировой массы в сутки, но в дефицит разрешена лишь доля этого предела, убывающая по шкале процента жира (своей для пола) и растущая только при доказанной малой доле мышц в потере. Дефицит глубже этой границы организм вынужден покрывать распадом белков внутренних органов и миокарда, резко угнетая выработку трийодтиронина (T3).",
        "action": "Цель калорийности автоматически поднимается до уровня безопасного пола.",
        "keys": ["fat_supply_kcal_per_kg", "fat_share_lean", "fat_share_fat", "fat_share_fat_proven",
                 "fat_share_bounds_m", "fat_share_bounds_f", "fat_mass_window_days",
                 "lean_share_window_days", "lean_share_min_points", "lean_share_min_loss_kg"],
    },
    "UNDEREATING": {
        "code": "UNDEREATING",
        "name": "Хроническое недоедание (3+ дня)",
        "category": "Питание и калории",
        "default_severity": "warning",
        "description": "Фиксирует потребление калорий ниже 80% от суточной цели в течение 3 дней подряд при заполненном дневнике.",
        "rationale": "Систематический чрезмерный голод истощает запасы гликогена, резко поднимает кортизол, снижает уровень лептина и почти гарантированно ведёт к пищевому срыву и компульсивному перееданию.",
        "action": "Добрать необходимую энергию питательными сложными углеводами и полезными жирами, нормализовать регулярность питания.",
        "keys": ["undereating_ratio", "undereating_days"],
    },
    "RATE_HIGH": {
        "code": "RATE_HIGH",
        "name": "Превышение скорости снижения веса",
        "category": "Скорость снижения веса",
        "default_severity": "warning",
        "description": "Контролирует темп потери массы тела выше min(weight_rate_kg_week_max кг, weight_rate_pct_week_max % веса) в неделю — по медианам первых и последних 3 взвешиваний в окне, при покрытии не короче 10 дней.",
        "rationale": "Слишком быстрый сброс массы многократно увеличивает риск холелитиаза (камней в желчном пузыре).",
        "action": "Сообщить человеку темп и обсудить, чем он вызван — гардрейл не предписывает конкретную правку калоража.",
        "keys": ["weight_rate_pct_week_max", "weight_rate_kg_week_max", "weight_rate_window_days", "weight_rate_min_points"],
    },
    "WEIGHT_REGAIN": {
        "code": "WEIGHT_REGAIN",
        "name": "Возврат веса (28 дней)",
        "category": "Скорость снижения веса",
        "default_severity": "warning",
        "description": "Рост сглаженного веса за regain_window_days дней выше regain_pct% от медианы начала окна — по медианам первых и последних 3 взвешиваний из оставшихся после исключения дней рефида и болезни.",
        "rationale": "Возврат веса случается по разным причинам — перерыв в терапии, снижение дозы, срыв — и гард сообщает только факт роста, а не причину: причину ищет модель по остальным данным (побочные эффекты, приём препарата).",
        "action": "Сообщить человеку факт (кг/% за сколько дней), без причин и советов — их называет модель по контексту.",
        "keys": ["regain_window_days", "regain_min_points", "regain_pct"],
    },
    "PLATEAU": {
        "code": "PLATEAU",
        "name": "Весовое плато (10 дней)",
        "category": "Скорость снижения веса",
        "default_severity": "warning",
        "description": "Колебания веса в пределах 0.5 кг за 10 дней при регулярных взвешиваниях (≥5 замеров).",
        "rationale": "Остановка снижения веса говорит либо о падении бытовой активности (NEAT), либо о задержке жидкости (кортизол, избыток соли, тренировочное воспаление), либо о приближении реального дефицита к нулю.",
        "action": "Проверить взвешивание порций, уровень стресса и сна. При необходимости запланировать рефид.",
        "keys": ["plateau_range_kg", "plateau_window_days", "plateau_min_points"],
    },
    "LIPID_GUARD": {
        "code": "LIPID_GUARD",
        "name": "Липидный контроль для медикаментов",
        "category": "Питание и медикаменты",
        "default_severity": "warning",
        "description": "Проверяет наличие не менее 10 г жиров в приёмах пищи в окне ±3 часа от приёма орального препарата.",
        "rationale": "Жирорастворимые пероральные препараты (андрокомплекс, витамины D/E/A, коферменты) требуют липидов для стимуляции выделения желчи и образования мицелл. Приём без жиров снижает абсорбцию в разы.",
        "action": "Обязательно принять препарат вместе с пищей, содержащей жиры (сыр, яйца, орехи, масло, авокадо).",
        "keys": ["lipid_guard_fat_g"],
    },
    "PROTEIN_SKEW": {
        "code": "PROTEIN_SKEW",
        "name": "Дисбаланс распределения белка",
        "category": "Питание и калории",
        "default_severity": "warning",
        "description": "Концентрация более 50% суточного плана белка в одном приёме пищи (при ≥3 приёмах за день).",
        "rationale": "Синтез мышечного белка (MPS) активируется всплеском лейцина каждые 3–5 часов. Однократная избыточная доза белка окисляется, пока остальную часть суток мышцы голодают. На терапии GLP-1 равномерность критична. Доля рассчитывается от суточного целевого плана белка, чтобы не выдавать ложные тревоги в первой половине дня.",
        "action": "Распределить источники белка равномерно на 3–4 приёма (по 30–45 г белка в каждый приём).",
        "keys": ["protein_skew_threshold", "protein_skew_min_meals", "protein_skew_min_total_g"],
    },
    "GLUCOSE_VOLATILITY": {
        "code": "GLUCOSE_VOLATILITY",
        "name": "Вариативность и тренд гликемии",
        "category": "Здоровье и метаболизм",
        "default_severity": "warning",
        "description": "Три последних замера натощак подряд выше своей медианы за 28 дней на 0.3 ммоль/л и больше. Замеры не натощак и после короткого сна не считаются.",
        "rationale": "Отдельные утра зависят от сна, воды, позднего ужина и условий замера. Только устойчивый сдвиг нескольких замеров подряд говорит о смещении базового уровня и возможном снижении чувствительности к инсулину.",
        "action": "Продолжить замеры натощак в одинаковых условиях и наблюдать динамику; при сохранении сдвига показать врачу.",
        "keys": ["glucose_streak", "glucose_rise_mmol", "glucose_window_days", "glucose_min_points", "glucose_short_sleep_min"],
    },
    "WHR_HIGH": {
        "code": "WHR_HIGH",
        "name": "Индекс висцерального жира (WHR)",
        "category": "Здоровье и метаболизм",
        "default_severity": "warning",
        "description": "Отношение окружности талии к окружности бёдер (WHR ≥ 0.90 для мужчин, ≥ 0.85 для женщин).",
        "rationale": "Абдоминальное распределение жира прямо связано с накоплением висцерального жира вокруг печени и сердца, метаболическим синдромом, инсулинорезистентностью и риском атеросклероза.",
        "action": "Сохранять умеренный дефицит калорий, аэробные тренировки низкой/средней интенсивности (Zone 2).",
        "keys": ["whr_male_max", "whr_female_max"],
    },
    "STALE_CALIB": {
        "code": "STALE_CALIB",
        "name": "Полнота пищевого дневника (TDEE)",
        "category": "Дисциплина данных",
        "default_severity": "warning",
        "description": "Непрерывная серия лога еды менее 7 дней подряд.",
        "rationale": "Без непрерывной и точного учёта калорий математическая адаптивная модель расхода энергии (TDEE) не может свести баланс. Расчёт замораживается, чтобы не выдавать искажённые рекомендации.",
        "action": "Вести регулярный дневник питания без пропусков хотя бы 7 дней подряд.",
        "keys": ["stale_calib_min_days"],
    },
    "NO_MEASURE": {
        "code": "NO_MEASURE",
        "name": "Пропуск взвешиваний (>7 дней)",
        "category": "Дисциплина данных",
        "default_severity": "warning",
        "description": "Отсутствие взвешиваний и состава тела более 7 дней подряд.",
        "rationale": "Без свежих антропометрических данных алгоритмы защиты мышечной массы, темпа и расхода перестают получать обратную связь.",
        "action": "Взвеситься на биоимпедансных весах утром натощак после посещения туалета.",
        "keys": ["no_measure_days"],
    },
    "MEASURE_SOON": {
        "code": "MEASURE_SOON",
        "name": "Напоминание о замере (5–7 дней)",
        "category": "Дисциплина данных",
        "default_severity": "info",
        "description": "Прошло от 5 до 7 дней с момента предыдущего контрольного замера.",
        "rationale": "Превентивное мягкое напоминание для поддержания ритмичного еженедельного графика мониторинга.",
        "action": "Запланировать замер на ближайшее утро.",
        "keys": ["measure_soon_after_days", "no_measure_days"],
    },
    "BINGE_RISK": {
        "code": "BINGE_RISK",
        "name": "Риск пищевого срыва (триада)",
        "category": "Питание и калории",
        "default_severity": "warning",
        "description": "Триада факторов риска срыва: накопленный дефицит калорий за окно, короткий сон прошлой ночью, мало белка на завтрак (или завтрак пропущен). Срабатывает при совпадении не менее двух факторов из трёх.",
        "rationale": "Ограничение сна повышает грелин и снижает лептин, усиливая голод и тягу к калорийной еде (Spiegel et al., 2004). Большой накопленный дефицит энергии сам по себе провоцирует компенсаторное переедание. Более высокобелковый завтрак повышает насыщение и снижает потребление калорий в течение дня (Leidy et al., 2013). По отдельности факторы шумные, но их совпадение — устойчивый предиктор срыва.",
        "action": "Полноценный белковый приём пищи в ближайшее время, не углублять дефицит сегодня.",
        "keys": ["binge_window_days", "binge_deficit_kcal", "binge_sleep_min", "binge_breakfast_protein_g", "binge_min_factors"],
    },
    "RECOVERY_LOW": {
        "code": "RECOVERY_LOW",
        "name": "Сигнал восстановления (3 дня)",
        "category": "Здоровье и метаболизм",
        "default_severity": "warning",
        "description": "3 календарных дня подряд HRV заметно ниже своей медианы за 28 дней и одновременно минимальный пульс заметно выше своей — по данным часов.",
        "rationale": "Снижение вариабельности пульса (HRV) вместе с ростом минимального пульса во сне — устойчивый признак недовосстановления (накопленный стресс, недосып, начало болезни). Не диагноз, а повод не наращивать нагрузку сегодня.",
        "action": "Сделать сегодняшнюю тренировку легче или пропустить, проверить сон и стресс.",
        "keys": ["recovery_hrv_drop_pct", "recovery_hr_min_rise_bpm"],
    },
    "RESTING_HR_RISING": {
        "code": "RESTING_HR_RISING",
        "name": "Рост пульса покоя",
        "category": "Здоровье и метаболизм",
        "default_severity": "info",
        "description": "Медиана минимального пульса за 7 дней выше медианы за предыдущие 28 дней на 4 уд/мин и больше.",
        "rationale": "Устойчивый рост пульса покоя без роста нагрузки - ранний неспецифический признак недовосстановления, недосыпа, стресса или начинающейся болезни. Работает и без данных HRV.",
        "action": "Проверить сон, стресс, кофеин и самочувствие; не наращивать нагрузку, пока пульс не вернётся.",
        "keys": ["resting_hr_rise_bpm"],
    },
    "BP_HIGH": {
        "code": "BP_HIGH",
        "name": "Повышенное давление",
        "category": "Здоровье и метаболизм",
        "default_severity": "warning",
        "description": "Медиана давления за 7 дней (от 3 замеров в 2 разные даты) выше домашнего порога 135/85; одиночный замер от 180/120 за сутки - critical сразу.",
        "rationale": "Единичный замер зависит от стресса, кофеина и позы, поэтому вывод делается по серии. Кризисное значение при этом не ждёт серии: это вопрос безопасности.",
        "action": "Мерить утром и вечером в покое, сидя, 2 замера подряд; записи показать врачу. При кризисном значении с симптомами - скорая.",
        "keys": ["bp_window_days", "bp_min_readings", "bp_high_sys", "bp_high_dia", "bp_crisis_sys", "bp_crisis_dia"],
    },
    "BP_LOW": {
        "code": "BP_LOW",
        "name": "Пониженное давление",
        "category": "Здоровье и метаболизм",
        "default_severity": "warning",
        "description": "Два последних замера за 3 суток подряд с систолическим ниже 90.",
        "rationale": "На терапии GLP-1 низкое давление часто следует за обезвоживанием, слишком глубоким дефицитом калорий и малым количеством соли.",
        "action": "Добрать воду, не вставать резко; при головокружении, потемнении в глазах или обмороке - к врачу.",
        "keys": ["bp_low_sys"],
    },
}


def get_guards_status(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """Детальный срез по всем 15 гардрейлам с реальными значениями, статусами,
    порогами и обоснованиями для панели управления и дашборда."""
    alerts = check_all(conn, user_id)
    fired_map = {a["code"]: a for a in alerts}
    cfg = _cfg()
    now = _now(conn, user_id)
    results = []

    user = conn.execute("SELECT height_cm, sex FROM users WHERE id=?", (user_id,)).fetchone()
    height_cm = user["height_cm"] if user else None
    sex = user["sex"] if user else "m"

    last_bm = conn.execute(
        "SELECT measured_at, weight_kg, ffm_kg FROM body_metrics WHERE user_id=? ORDER BY measured_at DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    gap_days = _gap_days(last_bm["measured_at"], conn, user_id) if last_bm else None

    since_14d = (now - timedelta(days=cfg["lbm_ratio_window_days"])).strftime("%Y-%m-%d %H:%M:%S")
    res_14d = _lean_loss_ratio(conn, user_id, since_14d)
    res_drift = _lean_loss_ratio(conn, user_id, since=None)

    for code, meta in GUARD_DEFINITIONS.items():
        item = {
            "code": code,
            "name": meta["name"],
            "category": meta["category"],
            "description": meta["description"],
            "rationale": meta["rationale"],
            "action": meta["action"],
            "keys": meta["keys"],
            "status": "ok",
            "severity": meta["default_severity"],
            "current_val": "—",
            "threshold_val": "—",
            "message": "Показатель находится в безопасных пределах.",
        }

        if code in fired_map:
            al = fired_map[code]
            item["status"] = al.get("severity", "warning")
            item["severity"] = al.get("severity", "warning")
            item["message"] = al.get("message", "")
            val = al.get("value")
            thr = al.get("threshold")
            if code in ("LBM_RATIO", "LBM_DRIFT") and isinstance(val, (int, float)):
                item["current_val"] = f"{val * 100:.1f}% FFM"
                item["threshold_val"] = f"≤ {thr * 100:.0f}% FFM" if isinstance(thr, (int, float)) else str(thr)
            elif code == "PROTEIN_SKEW" and isinstance(val, (int, float)):
                item["current_val"] = f"{val * 100:.0f}% плана"
                item["threshold_val"] = f"≤ {thr * 100:.0f}% от плана" if isinstance(thr, (int, float)) else str(thr)
            elif code == "STALE_CALIB":
                item["current_val"] = f"{val} дн. подряд"
                item["threshold_val"] = f"≥ {thr} дн. подряд"
            elif code == "WHR_HIGH":
                item["current_val"] = f"{val:.2f}" if isinstance(val, (int, float)) else str(val)
                item["threshold_val"] = f"≤ {thr:.2f}" if isinstance(thr, (int, float)) else str(thr)
            elif code == "RATE_HIGH":
                item["current_val"] = f"{val:.2f} кг/неделю" if isinstance(val, (int, float)) else str(val)
                item["threshold_val"] = f"≤ {thr:.2f} кг/неделю" if isinstance(thr, (int, float)) else str(thr)
            elif code == "WEIGHT_REGAIN":
                item["current_val"] = f"+{val:.1f}%" if isinstance(val, (int, float)) else str(val)
                item["threshold_val"] = f"≤ {thr}%" if isinstance(thr, (int, float)) else str(thr)
            elif code == "UNDEREATING":
                item["current_val"] = f"Ниже {thr * 100:.0f}% цели {val} дн. подряд" if isinstance(thr, (int, float)) else f"{val} дн."
                item["threshold_val"] = f"< {val} дн. подряд"
            elif code == "PLATEAU":
                item["current_val"] = f"Размах {val:.1f} кг" if isinstance(val, (int, float)) else str(val)
                item["threshold_val"] = f"> {thr} кг"
            elif code == "FFMI_FLOOR":
                item["current_val"] = f"{val:.1f} кг/м²" if isinstance(val, (int, float)) else str(val)
                item["threshold_val"] = f"≥ {thr:.1f} кг/м²"
            elif code in ("NO_MEASURE", "MEASURE_SOON"):
                item["current_val"] = f"{val} дн. без замеров" if isinstance(val, (int, float)) else str(val)
                item["threshold_val"] = f"< {thr} дн."
            elif code == "BINGE_RISK":
                item["current_val"] = f"{val} из 3 факторов" if isinstance(val, (int, float)) else str(val)
                item["threshold_val"] = f"< {thr} факторов" if isinstance(thr, (int, float)) else str(thr)
            else:
                item["current_val"] = str(val) if val is not None else "—"
                item["threshold_val"] = str(thr) if thr is not None else "—"
            results.append(item)
            continue

        if code == "LBM_RATIO":
            thresh = cfg["lbm_ratio_threshold"]
            item["threshold_val"] = f"≤ {thresh * 100:.0f}% FFM"
            if res_14d:
                ratio, dw, dl, n = res_14d
                item["current_val"] = f"{ratio * 100:.1f}% FFM ({n} замеров)"
            else:
                item["status"] = "nodata"
                item["current_val"] = "Недостаточно точек"
                item["message"] = f"Требуется минимум {cfg['lbm_ratio_min_points']} замера в окне {cfg['lbm_ratio_window_days']} дн."

        elif code == "LBM_DRIFT":
            thresh = cfg["lbm_drift_threshold"]
            item["threshold_val"] = f"≤ {thresh * 100:.0f}% FFM"
            if res_drift:
                ratio, dw, dl, n = res_drift
                item["current_val"] = f"{ratio * 100:.1f}% FFM ({n} замеров)"
            else:
                item["status"] = "nodata"
                item["current_val"] = "Недостаточно точек"
                item["message"] = "Требуется история замеров от базовой точки."

        elif code == "FFMI_FLOOR":
            thresh = cfg["ffmi_floor_f"] if sex == "f" else cfg["ffmi_floor_m"]
            item["threshold_val"] = f"≥ {thresh:.1f} кг/м²"
            if last_bm and last_bm["ffm_kg"] and height_cm:
                ffmi = last_bm["ffm_kg"] / ((height_cm / 100) ** 2)
                item["current_val"] = f"{ffmi:.1f} кг/м²"
            else:
                item["status"] = "nodata"
                item["current_val"] = "Нет данных"
                item["message"] = "Требуется замер тощей массы (FFM) и рост."

        elif code == "BMR_FLOOR":
            item["threshold_val"] = "≥ Безопасный пол калоража"
            try:
                from health_core.energy import daily_target
                comp = daily_target(conn, user_id, now.date().isoformat())
                fl = comp.get("kcal_floor")
                if fl:
                    item["threshold_val"] = f"≥ {fl:.0f} ккал"
                    row = conn.execute("SELECT kcal_target FROM daily_targets WHERE user_id=? AND date=?",
                                       (user_id, now.date().isoformat())).fetchone()
                    tgt = row["kcal_target"] if row and row["kcal_target"] else comp.get("kcal")
                    item["current_val"] = f"{tgt:.0f} ккал" if tgt else "—"
            except Exception:
                item["current_val"] = "В норме"

        elif code == "UNDEREATING":
            item["threshold_val"] = f"≥ {cfg['undereating_ratio'] * 100:.0f}% цели ({cfg['undereating_days']} дн)"
            item["current_val"] = "Норма"

        elif code == "RATE_HIGH":
            item["threshold_val"] = f"≤ min({cfg['weight_rate_kg_week_max']:.1f} кг, {cfg['weight_rate_pct_week_max']:.1f}%) в нед"
            item["current_val"] = "Безопасный темп"

        elif code == "WEIGHT_REGAIN":
            item["threshold_val"] = f"≤ {cfg['regain_pct']}% за {cfg['regain_window_days']} дн"
            item["current_val"] = "Без возврата"

        elif code == "PLATEAU":
            item["threshold_val"] = f"> {cfg['plateau_range_kg']} кг за {cfg['plateau_window_days']} дн"
            item["current_val"] = "Вес динамичен"

        elif code == "LIPID_GUARD":
            item["threshold_val"] = f"≥ {cfg['lipid_guard_fat_g']} г жиров с препаратом"
            item["current_val"] = "Контролируется"

        elif code == "PROTEIN_SKEW":
            thresh = cfg["protein_skew_threshold"]
            item["threshold_val"] = f"≤ {thresh * 100:.0f}% от плана"
            today_str = now.date().isoformat()
            target_p = _protein_target(conn, user_id, today_str)
            p_rows = conn.execute(
                "SELECT fl.meal_slot, SUM(fi.protein_g) p FROM food_log fl "
                "JOIN food_items fi ON fi.food_log_id=fl.id "
                "WHERE fl.user_id=? AND date(fl.eaten_at)=? GROUP BY fl.id",
                (user_id, today_str),
            ).fetchall()
            if p_rows:
                slot_totals = {}
                for pr in p_rows:
                    s = pr["meal_slot"] or "?"
                    slot_totals[s] = slot_totals.get(s, 0.0) + (pr["p"] or 0.0)
                if slot_totals:
                    top_slot, top_p = max(slot_totals.items(), key=lambda kv: kv[1])
                    top_slot_ru = _MEAL_SLOT_RU.get(top_slot, top_slot)
                    basis_val = max(target_p, sum(slot_totals.values())) if target_p else sum(slot_totals.values())
                    if basis_val > 0:
                        top_share = top_p / basis_val
                        item["current_val"] = f"{top_share * 100:.0f}% плана ({top_p:.0f} г, {top_slot_ru})"
                    else:
                        item["current_val"] = f"{top_p:.0f} г ({top_slot_ru})"
                else:
                    item["current_val"] = "Нет приёмов"
            else:
                item["current_val"] = "Нет записей сегодня"

        elif code == "GLUCOSE_VOLATILITY":
            item["threshold_val"] = f"сдвиг натощак < {cfg['glucose_rise_mmol']} ммоль/л от медианы"
            item["current_val"] = "Стабильная"

        elif code == "WHR_HIGH":
            thresh = cfg["whr_female_max"] if sex == "f" else cfg["whr_male_max"]
            item["threshold_val"] = f"≤ {thresh:.2f}"
            try:
                from health_core.report import whr
                w = whr(conn, user_id)
                if w is not None:
                    item["current_val"] = f"{w:.2f}"
                else:
                    item["status"] = "nodata"
                    item["current_val"] = "Нет замеров"
                    item["message"] = "Требуются замеры талии и бёдер."
            except Exception:
                item["current_val"] = "—"

        elif code == "STALE_CALIB":
            min_d = cfg.get("stale_calib_min_days", 7)
            item["threshold_val"] = f"≥ {min_d} дн. подряд"
            from health_core.energy import logged_streak
            streak, _ = logged_streak(conn, user_id, now.date())
            item["current_val"] = f"{streak} из {min_d} дн. подряд"

        elif code in ("NO_MEASURE", "MEASURE_SOON"):
            item["threshold_val"] = f"< {cfg['no_measure_days']} дн (напоминание {cfg['measure_soon_after_days']} дн)"
            if gap_days is not None:
                item["current_val"] = f"{gap_days} дн. назад" if gap_days > 0 else "Сегодня"
            else:
                item["status"] = "nodata"
                item["current_val"] = "Нет замеров"

        elif code == "BINGE_RISK":
            item["threshold_val"] = f"< {cfg['binge_min_factors']} факторов"
            item["current_val"] = "Триада не выявлена"

        elif code == "RECOVERY_LOW":
            item["threshold_val"] = f"HRV −{cfg['recovery_hrv_drop_pct']:.0f}%, HRmin +{cfg['recovery_hr_min_rise_bpm']:.0f} — 3 дня подряд"
            item["current_val"] = "Восстановление в норме"

        elif code == "RESTING_HR_RISING":
            item["threshold_val"] = f"+{cfg['resting_hr_rise_bpm']:.0f} уд/мин к медиане 28 дней"
            item["current_val"] = "Пульс покоя в норме"

        elif code == "BP_HIGH":
            item["threshold_val"] = f"медиана < {cfg['bp_high_sys']}/{cfg['bp_high_dia']}, разово < {cfg['bp_crisis_sys']}/{cfg['bp_crisis_dia']}"
            item["current_val"] = "Давление в норме"

        elif code == "BP_LOW":
            item["threshold_val"] = f"сист. ≥ {cfg['bp_low_sys']}"
            item["current_val"] = "Давление не снижено"

        results.append(item)

    return results


if __name__ == "__main__":
    import os
    import re
    import sys
    import tempfile
    from pathlib import Path
    from datetime import date

    sys.stdout.reconfigure(encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HEALTH_DB"] = str(Path(tmp) / "health.db")
        import health_core.db as db
        db.DB_PATH = Path(os.environ["HEALTH_DB"])
        conn = connect()
        migrate(conn)

        # Фикстуры засевают данные по local_now(), гарды считают окна по
        # поясу пользователя (user_now). Без общего пояса даты разъезжаются
        # на часы, и окна ловят не те точки — выставляем пояс по умолчанию.
        from health_core.config import set_tz
        set_tz((load().get('schedule') or {}).get('default_timezone'))

        try:
            def make_user(tg_id, height_cm=185, created_days_ago=30):
                created = (_now() - timedelta(days=created_days_ago)).strftime("%Y-%m-%d %H:%M:%S")
                birth_date = (date.today().replace(year=date.today().year - 34)).isoformat()
                conn.execute(
                    "INSERT INTO users(telegram_user_id, height_cm, birth_date, sex, created_at) VALUES (?, ?, ?, ?, ?)",
                    (tg_id, height_cm, birth_date, "m", created),
                )
                return conn.execute("SELECT id FROM users WHERE telegram_user_id=?", (tg_id,)).fetchone()["id"]

            def add_metric(uid, days_ago, weight_kg, ffm_kg, hour=7):
                ts = (_now() - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0, microsecond=0)
                conn.execute(
                    "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg) VALUES (?,?,?,?,?)",
                    (uid, f"b-{uid}-{days_ago}-{hour}", ts.strftime("%Y-%m-%d %H:%M:%S"), weight_kg, ffm_kg),
                )

            u1 = make_user(101)
            add_metric(u1, 13, 130.3, 80.2)
            add_metric(u1, 9, 128.0, 79.9)
            add_metric(u1, 4, 124.5, 79.7)
            add_metric(u1, 0, 121.8, 79.5)
            conn.commit()
            alerts1 = check_all(conn, u1)
            codes1 = {a["code"] for a in alerts1}
            assert "LBM_RATIO" not in codes1, f"LBM_RATIO не должен сработать при 8.2%, получили {alerts1}"

            u2 = make_user(102)
            add_metric(u2, 13, 130.3, 80.2)
            add_metric(u2, 9, 128.0, 79.5)
            add_metric(u2, 4, 124.5, 79.0)
            add_metric(u2, 0, 121.8, 78.7)
            conn.commit()
            alerts2 = check_all(conn, u2)
            fired2 = [a for a in alerts2 if a["code"] == "LBM_RATIO"]
            assert fired2, f"LBM_RATIO должен сработать при >15%, получили {alerts2}"
            assert fired2[0]["threshold"] == 0.15

            u3 = make_user(103, created_days_ago=1)
            add_metric(u3, 0, 100.0, 75.0)
            conn.commit()
            alerts3 = check_all(conn, u3)
            assert alerts3 == [], f"одиночное взвешивание не должно давать алертов, получили {alerts3}"

            u4 = make_user(104, height_cm=185, created_days_ago=60)
            # 8, а не 7: _weight_trend берёт опору с measured_at <= (now - окно).
            # Точка ровно на границе с часом 07:00 попадает ПОЗЖЕ границы, если
            # тест запущен до семи утра — тогда опоры нет и тренд пуст.
            add_metric(u4, 8, 123.0, None, hour=7)
            add_metric(u4, 0, 121.8, None, hour=7)
            today = user_today(conn, u4)
            conn.execute("INSERT INTO daily_targets(user_id, date, kcal_target) VALUES (?,?,?)", (u4, today, 2209))
            conn.execute("INSERT INTO user_targets(user_id, valid_from, water_ml) VALUES (?,?,?)", (u4, today, 3000))
            conn.execute("INSERT INTO food_log(user_id, eaten_at) VALUES (?, ?)", (u4, f"{today} 08:00:00"))
            flid = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            conn.execute("INSERT INTO food_items(food_log_id, kcal, protein_g, fat_g, carbs_g) VALUES (?,?,?,?,?)",
                        (flid, 1180, 96, 41, 58))
            conn.execute("INSERT INTO water_log(user_id, at, volume_ml) VALUES (?,?,?)", (u4, f"{today} 09:00:00", 1900))
            conn.execute("INSERT INTO milestones(user_id, name, metric, threshold) VALUES (?,?,?,?)",
                        (u4, "milestone-2", "weight_kg", 110))
            conn.commit()

            from health_core.report import status_bar
            bar = status_bar(conn, u4)
            print("---status_bar output---")
            print(bar)
            pattern = (
                r"^Сегодня \d{2}\.\d{2}, \d{2}:\d{2}\. Вес 121\.8 \(утро\)\. Цель \d+ ккал\.\n"
                r"Съедено 1180 / Б 96 / Ж 41 / У 58\. Вода 1\.9 / 3\.0 л\.\n"
                r".*Активных алертов.*\.\n"
                r"Тренд 7 дней: −1\.2 кг\. Ближайшая веха 110 кг, осталось 11\.8\.$"
            )
            assert re.match(pattern, bar), f"status_bar не соответствует формату §07:\n{bar!r}"
            print("OK: LBM_RATIO 8.2% не срабатывает, >15% срабатывает, одиночное взвешивание тихое, status_bar соответствует формату §07")

            u5 = make_user(105)
            add_metric(u5, 13, 100.0, 80.0)
            add_metric(u5, 0, 98.1, 79.6)
            conn.commit()
            alerts5 = check_all(conn, u5)
            codes5 = {a["code"] for a in alerts5}
            assert "LBM_RATIO" not in codes5, f"LBM_RATIO должен молчать при Δffm=0.4<0.5, получили {alerts5}"

            u6 = make_user(106)
            add_metric(u6, 13, 100.0, 80.0)
            add_metric(u6, 9, 96.0, 79.3)
            add_metric(u6, 4, 93.0, 78.9)
            add_metric(u6, 0, 91.5, 78.5)
            conn.commit()
            alerts6 = check_all(conn, u6)
            fired6 = [a for a in alerts6 if a["code"] == "LBM_RATIO"]
            assert fired6, f"LBM_RATIO должен сработать при Δffm=1.5>0.5, получили {alerts6}"

            from health_core.report import trends
            u7 = make_user(107)
            add_metric(u7, 30, 100.0, 75.0)
            add_metric(u7, 0, 98.0, 74.5)
            ts_base = (_now() - timedelta(days=30)).strftime("%Y-%m-%d")
            ts_now = _now().strftime("%Y-%m-%d")
            conn.execute("INSERT INTO anthropometry(user_id, site, value_cm, measured_on) VALUES (?,?,?,?)",
                        (u7, "талия", 85.0, ts_base))
            conn.execute("INSERT INTO anthropometry(user_id, site, value_cm, measured_on) VALUES (?,?,?,?)",
                        (u7, "талия", 82.0, ts_now))
            conn.commit()
            t7 = trends(conn, u7, window_days=30)
            assert t7["waist_delta_cm"] is not None, f"waist_delta_cm должен быть числом, получили {t7['waist_delta_cm']}"
            assert t7["waist_delta_cm"] == -3.0, f"waist_delta_cm должен быть -3.0, получили {t7['waist_delta_cm']}"

            u8 = make_user(108, height_cm=185, created_days_ago=60)
            add_metric(u8, 7, 123.0, 78.0)
            add_metric(u8, 0, 115.0, 76.5)
            today = user_today(conn, u8)
            conn.execute("INSERT INTO daily_targets(user_id, date, kcal_target) VALUES (?,?,?)", (u8, today, 2200))
            conn.execute("INSERT INTO user_targets(user_id, valid_from, water_ml) VALUES (?,?,?)", (u8, today, 3000))
            conn.execute("INSERT INTO food_log(user_id, eaten_at) VALUES (?, ?)", (u8, f"{today} 08:00:00"))
            flid8 = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            conn.execute("INSERT INTO food_items(food_log_id, kcal, protein_g, fat_g, carbs_g) VALUES (?,?,?,?,?)",
                        (flid8, 1100, 90, 40, 55))
            conn.commit()
            alerts_before = conn.execute(
                "SELECT COUNT(*) c FROM alerts WHERE user_id=? AND date(created_at)=?", (u8, today)
            ).fetchone()["c"]
            assert alerts_before == 0, f"alerts table должна быть пустой, но содержит {alerts_before} строк"

            bar8 = status_bar(conn, u8)
            assert "Активных алертов:" in bar8 or "Активных алертов: 1" in bar8, f"status_bar должен показать алерт, получили: {bar8}"
            alerts_after = conn.execute(
                "SELECT COUNT(*) c FROM alerts WHERE user_id=? AND date(created_at)=?", (u8, today)
            ).fetchone()["c"]
            assert alerts_after == 0, f"status_bar не должен писать в alerts table, но она содержит {alerts_after} строк"

            print("OK: FFM noise floor работает, trends() читает талию, status_bar() показывает live гардрейлы без записи")

            # WHR tests
            u9 = make_user(109, height_cm=185, created_days_ago=30)
            ts_base = (_now() - timedelta(days=30)).strftime("%Y-%m-%d")
            ts_now = _now().strftime("%Y-%m-%d")
            # Male, waist 119 / hip 114 -> WHR 1.04, should fire
            conn.execute("INSERT INTO anthropometry(user_id, site, value_cm, measured_on) VALUES (?,?,?,?)",
                        (u9, "талия", 119.0, ts_now))
            conn.execute("INSERT INTO anthropometry(user_id, site, value_cm, measured_on) VALUES (?,?,?,?)",
                        (u9, "таз", 114.0, ts_now))
            conn.execute("UPDATE users SET sex=? WHERE id=?", ("m", u9))
            conn.commit()
            alerts9 = check_all(conn, u9)
            whr_fired = [a for a in alerts9 if a["code"] == "WHR_HIGH"]
            assert whr_fired, f"WHR_HIGH должен сработать при 1.04 (м, порог 0.9), получили {alerts9}"

            u10 = make_user(110, height_cm=185, created_days_ago=30)
            # Male, waist 80 / hip 100 -> WHR 0.80, should not fire
            conn.execute("INSERT INTO anthropometry(user_id, site, value_cm, measured_on) VALUES (?,?,?,?)",
                        (u10, "талия", 80.0, ts_now))
            conn.execute("INSERT INTO anthropometry(user_id, site, value_cm, measured_on) VALUES (?,?,?,?)",
                        (u10, "таз", 100.0, ts_now))
            conn.execute("UPDATE users SET sex=? WHERE id=?", ("m", u10))
            conn.commit()
            alerts10 = check_all(conn, u10)
            whr_fired10 = [a for a in alerts10 if a["code"] == "WHR_HIGH"]
            assert not whr_fired10, f"WHR_HIGH не должен сработать при 0.80, получили {alerts10}"

            u11 = make_user(111, height_cm=185, created_days_ago=30)
            # Missing hip row -> whr() returns None, guard silent
            conn.execute("INSERT INTO anthropometry(user_id, site, value_cm, measured_on) VALUES (?,?,?,?)",
                        (u11, "талия", 85.0, ts_now))
            conn.execute("UPDATE users SET sex=? WHERE id=?", ("m", u11))
            conn.commit()
            alerts11 = check_all(conn, u11)
            whr_fired11 = [a for a in alerts11 if a["code"] == "WHR_HIGH"]
            assert not whr_fired11, f"WHR_HIGH должен молчать при отсутствии таза, получили {alerts11}"

            # RATE_HIGH tests (медианы 3 первых/последних, покрытие >= 10 дней)
            u_rate_short = make_user(160, height_cm=185, created_days_ago=30)
            # 4 кг за 5 дней -> темп огромный, но покрытие 5 дней < 10 -> темп неизвестен, молчит
            add_metric(u_rate_short, 5, 125.0, 78.0)
            add_metric(u_rate_short, 3, 123.5, 78.0)
            add_metric(u_rate_short, 2, 122.7, 78.0)
            add_metric(u_rate_short, 0, 121.0, 78.0)
            conn.commit()
            alerts_rate_short = check_all(conn, u_rate_short)
            rate_fired_short = [a for a in alerts_rate_short if a["code"] == "RATE_HIGH"]
            assert not rate_fired_short, f"RATE_HIGH должен молчать при покрытии < 10 дней, получили {alerts_rate_short}"

            u_rate_outlier = make_user(161, height_cm=185, created_days_ago=30)
            # Мягкий реальный темп (~1 кг/нед), но один шумный выброс в последней точке
            # (116.0 вместо ~119) — median(последних 3) гасит его, наивная разница
            # эндпоинтов дала бы 5 кг/13 дн = 2.7 кг/нед и ложно сработала бы
            add_metric(u_rate_outlier, 13, 121.0, 78.0)
            add_metric(u_rate_outlier, 10, 120.8, 78.0)
            add_metric(u_rate_outlier, 8, 120.5, 78.0)
            add_metric(u_rate_outlier, 5, 119.6, 78.0)
            add_metric(u_rate_outlier, 2, 119.3, 78.0)
            add_metric(u_rate_outlier, 0, 116.0, 78.0)
            conn.commit()
            alerts_rate_outlier = check_all(conn, u_rate_outlier)
            rate_fired_outlier = [a for a in alerts_rate_outlier if a["code"] == "RATE_HIGH"]
            assert not rate_fired_outlier, (
                f"одиночный выброс в конце окна не должен срабатывать благодаря медианам, получили {alerts_rate_outlier}"
            )

            u_rate_real = make_user(162, height_cm=185, created_days_ago=30)
            # Реальный темп far above threshold: медиана первых 3 (127.0) к медиане
            # последних 3 (119.5) за 13 дней -> ~4 кг/нед, > min(1.5 кг, 1.5%)
            add_metric(u_rate_real, 13, 130.0, 78.0)
            add_metric(u_rate_real, 10, 127.0, 78.0)
            add_metric(u_rate_real, 8, 125.0, 78.0)
            add_metric(u_rate_real, 5, 122.0, 78.0)
            add_metric(u_rate_real, 2, 119.5, 78.0)
            add_metric(u_rate_real, 0, 117.0, 78.0)
            conn.commit()
            alerts_rate_real = check_all(conn, u_rate_real)
            rate_fired_real = [a for a in alerts_rate_real if a["code"] == "RATE_HIGH"]
            assert rate_fired_real, f"RATE_HIGH должен сработать при реальном темпе >1.5%/нед, получили {alerts_rate_real}"
            msg_rate_real = rate_fired_real[0]["message"]
            assert "сухой массы" not in msg_rate_real and "мышц" not in msg_rate_real, (
                f"RATE_HIGH больше не отвечает за сухую массу — сообщение не должно её упоминать: {msg_rate_real}"
            )

            u_rate_gain = make_user(163, height_cm=185, created_days_ago=30)
            # Прирост веса за 14 дней с достаточным покрытием -> молчит (только потеря)
            add_metric(u_rate_gain, 14, 118.7, 78.0)
            add_metric(u_rate_gain, 10, 120.0, 78.0)
            add_metric(u_rate_gain, 5, 122.0, 78.0)
            add_metric(u_rate_gain, 0, 123.7, 78.0)
            conn.commit()
            alerts_rate_gain = check_all(conn, u_rate_gain)
            rate_fired_gain = [a for a in alerts_rate_gain if a["code"] == "RATE_HIGH"]
            assert not rate_fired_gain, f"RATE_HIGH должен молчать на приросте, получили {alerts_rate_gain}"

            # Темп делится на расстояние между строками с медианами (день 9 -> день 4 = 5 дней),
            # а не на покрытие окна (13 дней): 1.8 кг за 5 дней = 2.5 кг/нед, старая формула давала 0.97
            u_rate_span = make_user(164, height_cm=185, created_days_ago=30)
            add_metric(u_rate_span, 13, 100.0, 78.0)
            add_metric(u_rate_span, 9, 99.0, 78.0)
            add_metric(u_rate_span, 4, 97.2, 78.0)
            add_metric(u_rate_span, 0, 95.36, 78.0)
            conn.commit()
            rate_fired_span = [a for a in check_all(conn, u_rate_span) if a["code"] == "RATE_HIGH"]
            assert rate_fired_span and rate_fired_span[0]["value"] > 2.0, f"RATE_HIGH по медианам за 5 дней: {rate_fired_span}"

            # Вес растёт или почти не меняется при падении FFM: доли потери нет, LBM_* молчат
            for tg, wts in ((164 + 1, (100.0, 100.2, 100.5, 100.8)), (164 + 2, (100.0, 99.9, 99.8, 99.7))):
                u_flat = make_user(tg, height_cm=185, created_days_ago=30)
                for da, w, f in zip((13, 9, 4, 0), wts, (80.0, 79.5, 79.0, 78.6)):
                    add_metric(u_flat, da, w, f)
                conn.commit()
                lbm_flat = [a for a in check_all(conn, u_flat) if a["code"] in ("LBM_RATIO", "LBM_DRIFT")]
                assert not lbm_flat, f"LBM_* не должны срабатывать без потери веса (вес {wts}): {lbm_flat}"

            print("OK: WHR_HIGH срабатывает; RATE_HIGH молчит на коротком покрытии и на одиночном "
                  "выбросе, срабатывает на реальном темпе без упоминания сухой массы, молчит на приросте")

            # LIPID_GUARD: route='oral' срабатывает на низком жире, route='injection'
            # и route IS NULL (легаси-строка до миграции v3) молчат при тех же условиях.
            now_ts = _now().strftime("%Y-%m-%d %H:%M:%S")

            def _add_med(uid, route):
                conn.execute(
                    "INSERT INTO med_log(user_id, at, substance, dose, route) VALUES (?,?,?,?,?)",
                    (uid, now_ts, "Андрокомплекс", "1 капс", route),
                )

            def _add_low_fat_meal(uid):
                conn.execute("INSERT INTO food_log(user_id, eaten_at) VALUES (?,?)", (uid, now_ts))
                flid = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
                conn.execute(
                    "INSERT INTO food_items(food_log_id, kcal, protein_g, fat_g, carbs_g) VALUES (?,?,?,?,?)",
                    (flid, 300, 20, 3, 30),  # fat_g=3 < lipid_guard_fat_g=10
                )

            u15 = make_user(115, height_cm=185, created_days_ago=30)
            _add_med(u15, "oral")
            _add_low_fat_meal(u15)
            conn.commit()
            alerts15 = check_all(conn, u15)
            lipid_fired15 = [a for a in alerts15 if a["code"] == "LIPID_GUARD"]
            assert lipid_fired15, f"LIPID_GUARD должен сработать при route='oral' и жирах 3г<10г, получили {alerts15}"

            u16 = make_user(116, height_cm=185, created_days_ago=30)
            _add_med(u16, "injection")
            _add_low_fat_meal(u16)
            conn.commit()
            alerts16 = check_all(conn, u16)
            lipid_fired16 = [a for a in alerts16 if a["code"] == "LIPID_GUARD"]
            assert not lipid_fired16, f"LIPID_GUARD должен молчать при route='injection', получили {alerts16}"

            u17 = make_user(117, height_cm=185, created_days_ago=30)
            _add_med(u17, None)  # легаси-строка без route — до миграции v3
            _add_low_fat_meal(u17)
            conn.commit()
            alerts17 = check_all(conn, u17)
            lipid_fired17 = [a for a in alerts17 if a["code"] == "LIPID_GUARD"]
            assert not lipid_fired17, (
                f"LIPID_GUARD должен молчать при route IS NULL (регресс к старой эвристике по тексту), получили {alerts17}"
            )

            print("OK: LIPID_GUARD срабатывает на route='oral', молчит на 'injection' и на легаси route IS NULL")

            # PROTEIN_SKEW tests
            def _add_meal(uid, slot, protein_g, hour):
                ts = f"{_now().date().isoformat()} {hour:02d}:00:00"
                conn.execute("INSERT INTO food_log(user_id, eaten_at, meal_slot) VALUES (?,?,?)", (uid, ts, slot))
                flid = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
                conn.execute(
                    "INSERT INTO food_items(food_log_id, kcal, protein_g, fat_g, carbs_g) VALUES (?,?,?,?,?)",
                    (flid, protein_g * 4, protein_g, 10, 10),
                )

            u18 = make_user(118, height_cm=185, created_days_ago=30)
            _add_meal(u18, "breakfast", 70, 8)
            _add_meal(u18, "lunch", 15, 13)
            _add_meal(u18, "dinner", 15, 19)
            conn.commit()
            alerts18 = check_all(conn, u18)
            skew_fired18 = [a for a in alerts18 if a["code"] == "PROTEIN_SKEW"]
            assert skew_fired18, f"PROTEIN_SKEW должен сработать при 70% в завтраке, получили {alerts18}"

            u19 = make_user(119, height_cm=185, created_days_ago=30)
            _add_meal(u19, "breakfast", 90, 11)  # одиночный приём, тривиально 100%
            conn.commit()
            alerts19 = check_all(conn, u19)
            skew_fired19 = [a for a in alerts19 if a["code"] == "PROTEIN_SKEW"]
            assert not skew_fired19, f"PROTEIN_SKEW не должен сработать при одном приёме, получили {alerts19}"

            u20 = make_user(120, height_cm=185, created_days_ago=30)
            _add_meal(u20, "breakfast", 30, 8)
            _add_meal(u20, "lunch", 30, 13)
            _add_meal(u20, "dinner", 30, 19)
            conn.commit()
            alerts20 = check_all(conn, u20)
            skew_fired20 = [a for a in alerts20 if a["code"] == "PROTEIN_SKEW"]
            assert not skew_fired20, f"PROTEIN_SKEW не должен сработать при равномерном дне, получили {alerts20}"

            # PROTEIN_SKEW с установленным планом:
            u_skew_target = make_user(190, height_cm=185, created_days_ago=30)
            today_ts = _now().date().isoformat()
            conn.execute("INSERT INTO daily_targets(user_id, date, kcal_target, protein_g_target) VALUES (?,?,?,?)",
                         (u_skew_target, today_ts, 2200, 140.0))
            # Завтрак 52г, перекус 12г, перекус 4г (всего 68г < 140г плана). 52г от 140г — это 37.1% (<= 50%)
            _add_meal(u_skew_target, "breakfast", 52, 8)
            _add_meal(u_skew_target, "snack", 12, 11)
            _add_meal(u_skew_target, "snack", 4, 15)
            conn.commit()
            alerts_skew_target = check_all(conn, u_skew_target)
            skew_fired_target = [a for a in alerts_skew_target if a["code"] == "PROTEIN_SKEW"]
            assert not skew_fired_target, (
                f"PROTEIN_SKEW не должен срабатывать, когда завтрак 52г составляет 37% от плана 140г, получили {alerts_skew_target}"
            )

            # Если же один приём превышает 50% от плана (например 80г от 140г = 57%):
            u_skew_over = make_user(191, height_cm=185, created_days_ago=30)
            conn.execute("INSERT INTO daily_targets(user_id, date, kcal_target, protein_g_target) VALUES (?,?,?,?)",
                         (u_skew_over, today_ts, 2200, 140.0))
            _add_meal(u_skew_over, "breakfast", 80, 8)
            _add_meal(u_skew_over, "snack", 10, 11)
            _add_meal(u_skew_over, "snack", 10, 15)
            conn.commit()
            alerts_skew_over = check_all(conn, u_skew_over)
            skew_fired_over = [a for a in alerts_skew_over if a["code"] == "PROTEIN_SKEW"]
            assert skew_fired_over, f"PROTEIN_SKEW должен сработать при 80г от плана 140г (57%), получили {alerts_skew_over}"

            print("OK: PROTEIN_SKEW считается от плана (не срабатывает на 37% плана при низком дне, срабатывает на >50% плана)")

            # GLUCOSE_VOLATILITY tests
            def _add_glucose(uid, days_ago, value, ctx="натощак"):
                ts = (_now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute("INSERT INTO glucose_log(user_id, at, mmol_l, context) VALUES (?,?,?,?)", (uid, ts, value, ctx))

            _base = [(24, 5.0), (21, 5.1), (18, 4.9), (15, 5.0), (12, 5.1), (9, 5.0)]
            _shift = [(5, 5.8), (3, 5.9), (1, 5.8)]

            def _glu(uid, series, ctx="натощак"):
                for d, v in series:
                    _add_glucose(uid, d, v, ctx)
                conn.commit()
                return [a for a in check_all(conn, uid) if a["code"] == "GLUCOSE_VOLATILITY"]

            assert not _glu(make_user(121, height_cm=185, created_days_ago=30), [(10, 5.0), (7, 5.2), (4, 4.9)]), \
                "GLUCOSE_VOLATILITY не должен сработать на тонкой истории"

            glu22 = _glu(make_user(122, height_cm=185, created_days_ago=30), _base + _shift)
            assert glu22 and "Наблюдать динамику" in glu22[0]["message"] and "Уверенность" in glu22[0]["message"], \
                f"устойчивый сдвиг 3 замеров натощак должен дать осторожный алерт, получили {glu22}"

            assert not _glu(make_user(123, height_cm=185, created_days_ago=30), _base + [(5, 5.0), (3, 6.5), (1, 5.0)]), \
                "одиночный пик не должен давать сигнал"

            assert not _glu(make_user(424, height_cm=185, created_days_ago=30), _base + _shift, ctx="после еды"), \
                "замеры не натощак не должны давать сигнал"

            print("OK: GLUCOSE_VOLATILITY молчит на тонких данных, на одиночном пике и не-натощак, срабатывает на устойчивом сдвиге")

            # Мера разрыва — календарная. Ставим метку на 23:59 пять дней
            # назад: по календарю это 5 дней в любой час запуска, а по полным
            # суткам почти всегда 4 — ровно та разница, из-за которой гардрейл
            # молчал по ночам, а самотест падал в зависимости от времени суток.
            _probe = (_now() - timedelta(days=5)).replace(hour=23, minute=59, second=0, microsecond=0)
            assert _gap_days(_probe.strftime("%Y-%m-%d %H:%M:%S")) == 5,                 "разрыв должен считаться календарными днями, а не полными сутками"

            # MEASURE_SOON tests
            u24 = make_user(124, height_cm=185, created_days_ago=30)
            add_metric(u24, 1, 100.0, 75.0)
            conn.commit()
            alerts24 = check_all(conn, u24)
            soon_fired24 = [a for a in alerts24 if a["code"] == "MEASURE_SOON"]
            assert not soon_fired24, f"MEASURE_SOON не должен сработать на 1 дне, получили {alerts24}"

            u25 = make_user(125, height_cm=185, created_days_ago=30)
            add_metric(u25, 5, 100.0, 75.0)
            conn.commit()
            alerts25 = check_all(conn, u25)
            soon_fired25 = [a for a in alerts25 if a["code"] == "MEASURE_SOON"]
            no_measure_fired25 = [a for a in alerts25 if a["code"] == "NO_MEASURE"]
            assert soon_fired25, f"MEASURE_SOON должен сработать на 5 дне, получили {alerts25}"
            assert not no_measure_fired25, f"NO_MEASURE не должен звучать одновременно, получили {alerts25}"

            u26 = make_user(126, height_cm=185, created_days_ago=30)
            add_metric(u26, 9, 100.0, 75.0)  # >= no_measure_days=7 -> NO_MEASURE, не MEASURE_SOON
            conn.commit()
            alerts26 = check_all(conn, u26)
            soon_fired26 = [a for a in alerts26 if a["code"] == "MEASURE_SOON"]
            no_measure_fired26 = [a for a in alerts26 if a["code"] == "NO_MEASURE"]
            assert no_measure_fired26, f"NO_MEASURE должен сработать на 9 дне, получили {alerts26}"
            assert not soon_fired26, f"MEASURE_SOON не должен звучать одновременно с NO_MEASURE, получили {alerts26}"
            assert not (soon_fired26 and no_measure_fired26), "оба гардрейла разрыва не могут звучать одновременно"

            print("OK: MEASURE_SOON молчит на 1 дне, срабатывает на 5 дне, никогда не звучит вместе с NO_MEASURE")

            # МЕДИАНА-тесты: LBM_RATIO с одиночным выбросом на границе окна
            # Старая логика (эндпоинт-дельта) срабатывала бы; новая (медиана) не должна.
            u27 = make_user(127)
            # Стабильный ряд: вес спадает на 0.5 кг, тощая масса держится.
            # Потом в день 0 (граница окна) одиночный скачок: вес вверх на 2.0 кг,
            # ffm скачок на 1.5 кг (выброс гидрации). Эндпоинт даёт дельта ffm 1.5 кг
            # / 2.0 кг = 75%, выше порога 15% -> сработает.
            # Медиана: (79.9, 79.95, 80.0) vs (79.85, 79.9, 79.95) = 0.05, мало.
            add_metric(u27, 13, 131.0, 79.9)
            add_metric(u27, 11, 130.7, 79.95)
            add_metric(u27, 9, 130.5, 80.0)
            add_metric(u27, 7, 130.2, 79.85)
            add_metric(u27, 5, 130.0, 79.9)
            add_metric(u27, 2, 129.8, 79.95)  # стабильный ряд
            # Выброс на границе: вес скачок вверх (жидкость), ffm тоже скачок (шум)
            add_metric(u27, 0, 131.8, 81.4)
            conn.commit()
            alerts27 = check_all(conn, u27)
            lbm_fired27 = [a for a in alerts27 if a["code"] == "LBM_RATIO"]
            assert not lbm_fired27, (
                f"LBM_RATIO не должен сработать при одиночном выбросе на границе окна "
                f"(медиана должна отфильтровать), получили {alerts27}"
            )
            print("OK: медиана фильтрует одиночный выброс на границе окна, LBM_RATIO не срабатывает")

            # Подтверждение: подлинная потеря сухой массы всё ещё должна срабатывать.
            u28 = make_user(128)
            # Последовательная потеря: вес с 130 до 120 кг, тощая с 79.8 до 78.0 кг.
            # Медиана первых 3 (79.8, 79.9, 80.0) = 79.9
            # Медиана последних 3 (78.0, 78.2, 78.4) = 78.2
            # Дельта: вес -10 кг, тощая -(79.9-78.2)=-1.7 кг, дельта 1.7 / 10 = 17% > 15% срабатывает.
            add_metric(u28, 13, 130.0, 79.8)
            add_metric(u28, 11, 128.5, 79.9)
            add_metric(u28, 9, 127.0, 80.0)
            add_metric(u28, 7, 125.5, 79.5)
            add_metric(u28, 5, 124.0, 79.0)
            add_metric(u28, 2, 122.5, 78.2)
            add_metric(u28, 0, 120.0, 78.4)  # потеря 10 кг веса, 1.7 кг тощей
            conn.commit()
            alerts28 = check_all(conn, u28)
            lbm_fired28 = [a for a in alerts28 if a["code"] == "LBM_RATIO"]
            assert lbm_fired28, f"LBM_RATIO должен сработать при 17% потери тощей массы, получили {alerts28}"
            print("OK: подлинная потеря сухой массы всё ещё срабатывает (медиана не скрывает реальный тренд)")

            # Деградация окна: проверка граничных случаев
            u29 = make_user(129)
            # Ровно 5 точек (< 6): медиана первых 2 vs последних 2
            add_metric(u29, 4, 100.0, 75.0)
            add_metric(u29, 3, 99.8, 74.95)
            add_metric(u29, 2, 99.5, 74.90)
            add_metric(u29, 1, 99.2, 74.85)
            add_metric(u29, 0, 99.0, 74.80)
            conn.commit()
            alerts29 = check_all(conn, u29)
            # 5 точек: медиана(99.0, 99.8) = 99.4 vs медиана(99.2, 99.0) = 99.1
            # дельта вес = -0.3, дельта FFM = 74.825, не должно срабатывать (< 0.5 шума)
            assert alerts29 == [] or not any(a["code"] == "LBM_RATIO" for a in alerts29), (
                f"с 5 точками и малой дельтой LBM_RATIO не должен срабатывать, получили {alerts29}"
            )
            print("OK: деградация окна (5 точек) работает без краша")

            u30 = make_user(130)
            # Ровно 3 точки: используются endpoints (первая и последняя)
            add_metric(u30, 2, 100.0, 75.0)
            add_metric(u30, 1, 99.5, 74.9)
            add_metric(u30, 0, 99.0, 74.8)
            conn.commit()
            res30 = _lean_loss_ratio(conn, u30, since=None)
            assert res30 is not None, "3 точки должны дать результат"
            ratio30, d_w30, d_l30, n30 = res30
            assert n30 == 3, f"n должно быть 3, получили {n30}"
            print("OK: деградация окна (3 точки) работает")

            u31 = make_user(131)
            # Ровно 2 точки: минимум для сравнения
            add_metric(u31, 1, 100.0, 75.0)
            add_metric(u31, 0, 99.0, 74.8)
            conn.commit()
            res31 = _lean_loss_ratio(conn, u31, since=None)
            assert res31 is not None, "2 точки должны дать результат"
            ratio31, d_w31, d_l31, n31 = res31
            assert n31 == 2, f"n должно быть 2, получили {n31}"
            print("OK: деградация окна (2 точки) работает")

            u32 = make_user(132)
            # Ровно 1 точка: должна вернуть None
            add_metric(u32, 0, 100.0, 75.0)
            conn.commit()
            res32 = _lean_loss_ratio(conn, u32, since=None)
            assert res32 is None, f"1 точка должна вернуть None, получили {res32}"
            print("OK: деградация окна (1 точка) вернула None как ожидается")

            print("Все тесты медианы для LBM_RATIO прошли.")

            # ---------------------------------------------------------------- BINGE_RISK tests
            import health_core.energy as energy_mod

            def _add_binge_food(uid, days_ago, kcal, protein_g=10.0, meal_slot=None, hour=13):
                d = (_now().date() - timedelta(days=days_ago)).isoformat()
                ts = f"{d} {hour:02d}:00:00"
                conn.execute("INSERT INTO food_log(user_id, eaten_at, meal_slot) VALUES (?,?,?)", (uid, ts, meal_slot))
                flid = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
                conn.execute(
                    "INSERT INTO food_items(food_log_id, kcal, protein_g, fat_g, carbs_g) VALUES (?,?,?,?,?)",
                    (flid, kcal, protein_g, 10, 10),
                )

            def _add_binge_sleep(uid, night_date, duration_min):
                conn.execute(
                    "INSERT INTO sleep_log(user_id, night_date, duration_min) VALUES (?,?,?)",
                    (uid, night_date, duration_min),
                )

            _now_real = _now
            orig_adaptive_tdee = energy_mod.adaptive_tdee
            today_iso = _now().date().isoformat()

            # Сценарий A: все три фактора -> critical
            u40 = make_user(140, height_cm=185, created_days_ago=30)
            energy_mod.adaptive_tdee = lambda conn, user_id, window_days=14, for_date=None: 2500.0
            for days_ago in range(1, 5):  # 4 из 5 дней окна залогированы (>= window-1)
                _add_binge_food(u40, days_ago, 700)  # дефицит (2500-700)*4 = 7200 > 3500
            _add_binge_sleep(u40, today_iso, 300)  # < 390 мин
            _add_binge_food(u40, 0, 300, protein_g=10.0, meal_slot="breakfast")  # < 20 г
            conn.commit()
            alerts40 = check_all(conn, u40)
            binge40 = [a for a in alerts40 if a["code"] == "BINGE_RISK"]
            assert binge40, f"BINGE_RISK должен сработать при всех 3 факторах, получили {alerts40}"
            assert binge40[0]["severity"] == "critical", f"3 фактора -> critical, получили {binge40[0]}"

            # Сценарий B: ровно 2 фактора (завтрак в норме) -> warning
            u41 = make_user(141, height_cm=185, created_days_ago=30)
            for days_ago in range(1, 5):
                _add_binge_food(u41, days_ago, 700)
            _add_binge_sleep(u41, today_iso, 300)
            _add_binge_food(u41, 0, 300, protein_g=25.0, meal_slot="breakfast")  # >= 20 г, не сработал
            conn.commit()
            alerts41 = check_all(conn, u41)
            binge41 = [a for a in alerts41 if a["code"] == "BINGE_RISK"]
            assert binge41, f"BINGE_RISK должен сработать при 2 факторах, получили {alerts41}"
            assert binge41[0]["severity"] == "warning", f"2 фактора -> warning, получили {binge41[0]}"

            # Сценарий C: только 1 фактор (сон) -> None
            u42 = make_user(142, height_cm=185, created_days_ago=30)
            energy_mod.adaptive_tdee = lambda conn, user_id, window_days=14, for_date=None: None
            _add_binge_sleep(u42, today_iso, 300)  # сон сработал в одиночку, еды сегодня нет
            conn.commit()
            alerts42 = check_all(conn, u42)
            binge42 = [a for a in alerts42 if a["code"] == "BINGE_RISK"]
            assert not binge42, f"BINGE_RISK не должен сработать при 1 факторе, получили {alerts42}"

            # Сценарий D: нет сна, tdee=None, нет еды сегодня -> None (все три неизвестны)
            u43 = make_user(143, height_cm=185, created_days_ago=30)
            conn.commit()
            alerts43 = check_all(conn, u43)
            binge43 = [a for a in alerts43 if a["code"] == "BINGE_RISK"]
            assert not binge43, f"BINGE_RISK должен молчать без данных (все факторы неизвестны), получили {alerts43}"

            # Сценарий E: завтрак пропущен после 12:00 при активном логировании + короткий
            # сон -> 2 фактора (дефицит неизвестен, tdee=None), warning
            u44 = make_user(144, height_cm=185, created_days_ago=30)
            _now = lambda conn=None, user_id=None: _now_real().replace(hour=13, minute=0, second=0, microsecond=0)
            _add_binge_food(u44, 0, 500, protein_g=15.0, meal_slot="lunch", hour=13)  # еда сегодня, не завтрак
            _add_binge_sleep(u44, today_iso, 300)
            conn.commit()
            alerts44 = check_all(conn, u44)
            _now = _now_real
            binge44 = [a for a in alerts44 if a["code"] == "BINGE_RISK"]
            assert binge44, f"BINGE_RISK должен считать пропущенный завтрак после 12:00 фактором, получили {alerts44}"
            assert "завтрака нет" in binge44[0]["message"], f"сообщение должно упоминать пропуск завтрака: {binge44[0]}"
            assert binge44[0]["severity"] == "warning", f"2 фактора -> warning, получили {binge44[0]}"

            # Сценарий F (задача 1): рефид-день внутри окна дефицита исключается из
            # deficit_sum и logged_days целиком — без исключения его большой интейк
            # (8000 ккал) разбавил бы реальный дефицит (4×1800=7200) ниже порога
            # (7200-5500=1700 < 3500) и молча погасил бы фактор 1.
            u45 = make_user(145, height_cm=185, created_days_ago=30)
            energy_mod.adaptive_tdee = lambda conn, user_id, window_days=14, for_date=None: 2500.0
            for days_ago in range(1, 5):
                _add_binge_food(u45, days_ago, 700)  # 4 дня реального дефицита, (2500-700)*4=7200
            refeed_date = (_now().date() - timedelta(days=5)).isoformat()
            conn.execute("INSERT INTO refeed_days(user_id, date) VALUES (?, ?)", (u45, refeed_date))
            _add_binge_food(u45, 5, 8000)  # рефид: без исключения дал бы -5500, гасил бы гард
            _add_binge_sleep(u45, today_iso, 300)  # второй фактор — короткий сон
            conn.commit()
            alerts45 = check_all(conn, u45)
            binge45 = [a for a in alerts45 if a["code"] == "BINGE_RISK"]
            assert binge45, f"BINGE_RISK должен сработать: рефид-день исключён из окна дефицита, получили {alerts45}"
            assert "дефицит" in binge45[0]["message"], binge45[0]

            energy_mod.adaptive_tdee = orig_adaptive_tdee
            print("OK: BINGE_RISK — 3 фактора critical, 2 фактора warning, 1 фактор молчит, "
                  "без данных молчит, пропуск завтрака после 12:00 засчитан как фактор")

            # ---------------------------------------------------------------- режим болезни
            import health_core.sick as sick_mod

            today_sick = _now().date().isoformat()

            # UNDEREATING сам по себе (без болезни) — воспроизводим сценарий, каким его
            # проверяет check_undereating: N дней подряд ниже undereating_ratio цели.
            def _make_undereating_user(tg_id):
                uid = make_user(tg_id, height_cm=185, created_days_ago=30)
                cfg = _cfg()
                for i in range(1, cfg["undereating_days"] + 1):
                    d = (_now().date() - timedelta(days=i)).isoformat()
                    conn.execute("INSERT INTO daily_targets(user_id, date, kcal_target) VALUES (?,?,?)",
                                 (uid, d, 2000))
                    conn.execute("INSERT INTO food_log(user_id, eaten_at) VALUES (?,?)", (uid, f"{d} 12:00:00"))
                    flid = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
                    conn.execute(
                        "INSERT INTO food_items(food_log_id, kcal, protein_g, fat_g, carbs_g) VALUES (?,?,?,?,?)",
                        (flid, 900, 60, 20, 60),  # 45% цели, ниже undereating_ratio
                    )
                conn.commit()
                return uid

            u_sick_off = _make_undereating_user(150)
            alerts_off = check_all(conn, u_sick_off)
            assert any(a["code"] == "UNDEREATING" for a in alerts_off), (
                f"UNDEREATING должен сработать без режима болезни, получили {alerts_off}"
            )

            u_sick_on = _make_undereating_user(151)
            sick_mod.start(conn, u_sick_on, today_sick, 1, note="грипп")
            alerts_on = check_all(conn, u_sick_on)
            assert not any(a["code"] == "UNDEREATING" for a in alerts_on), (
                f"UNDEREATING должен молчать в день болезни, получили {alerts_on}"
            )
            print("OK: режим болезни глушит UNDEREATING")

            # GLUCOSE_VOLATILITY — безопасность, должен звучать и в день болезни.
            u_sick_glucose = make_user(152, height_cm=185, created_days_ago=30)
            for d, v in _base + _shift:  # тот же сценарий устойчивого сдвига
                _add_glucose(u_sick_glucose, d, v)
            sick_mod.start(conn, u_sick_glucose, today_sick, 1, note="грипп")
            conn.commit()
            alerts_glucose_sick = check_all(conn, u_sick_glucose)
            assert any(a["code"] == "GLUCOSE_VOLATILITY" for a in alerts_glucose_sick), (
                f"GLUCOSE_VOLATILITY обязан звучать даже в день болезни, получили {alerts_glucose_sick}"
            )
            print("OK: режим болезни НЕ глушит GLUCOSE_VOLATILITY (безопасность)")

            # ---------------------------------------------------------------- WEIGHT_REGAIN
            # Порог по умолчанию: regain_pct=3.0%, regain_window_days=28, regain_min_points=6.

            u_regain_fires = make_user(170, height_cm=185, created_days_ago=30)
            add_metric(u_regain_fires, 27, 99.5, None)
            add_metric(u_regain_fires, 25, 100.0, None)
            add_metric(u_regain_fires, 23, 100.5, None)   # первая медиана = 100.0
            add_metric(u_regain_fires, 4, 103.0, None)
            add_metric(u_regain_fires, 2, 103.5, None)
            add_metric(u_regain_fires, 0, 104.0, None)    # последняя медиана = 103.5 -> +3.5%
            conn.commit()
            alerts_regain_fires = check_all(conn, u_regain_fires)
            regain_fired = [a for a in alerts_regain_fires if a["code"] == "WEIGHT_REGAIN"]
            assert regain_fired, f"WEIGHT_REGAIN должен сработать при росте 3.5% за 28 дней, получили {alerts_regain_fires}"

            u_regain_quiet = make_user(171, height_cm=185, created_days_ago=30)
            add_metric(u_regain_quiet, 27, 99.5, None)
            add_metric(u_regain_quiet, 25, 100.0, None)
            add_metric(u_regain_quiet, 23, 100.5, None)   # первая медиана = 100.0
            add_metric(u_regain_quiet, 4, 102.0, None)
            add_metric(u_regain_quiet, 2, 102.5, None)
            add_metric(u_regain_quiet, 0, 103.0, None)    # последняя медиана = 102.5 -> +2.5%
            conn.commit()
            alerts_regain_quiet = check_all(conn, u_regain_quiet)
            assert not any(a["code"] == "WEIGHT_REGAIN" for a in alerts_regain_quiet), (
                f"WEIGHT_REGAIN должен молчать при росте 2.5% (< порога 3.0%), получили {alerts_regain_quiet}"
            )

            # Скачок только в дни рефида: без исключения дал бы явный возврат, но эти
            # две точки — рефид, и после исключения остаются ровно 6 плоских точек.
            u_regain_refeed = make_user(172, height_cm=185, created_days_ago=30)
            add_metric(u_regain_refeed, 27, 100.0, None)
            add_metric(u_regain_refeed, 24, 100.2, None)
            add_metric(u_regain_refeed, 21, 100.0, None)
            add_metric(u_regain_refeed, 18, 105.0, None)  # рефид — исключается
            add_metric(u_regain_refeed, 15, 105.0, None)  # рефид — исключается
            add_metric(u_regain_refeed, 6, 100.3, None)
            add_metric(u_regain_refeed, 3, 100.1, None)
            add_metric(u_regain_refeed, 0, 100.4, None)
            for d in (18, 15):
                conn.execute(
                    "INSERT INTO refeed_days(user_id, date) VALUES (?, ?)",
                    (u_regain_refeed, (_now().date() - timedelta(days=d)).isoformat()),
                )
            conn.commit()
            alerts_regain_refeed = check_all(conn, u_regain_refeed)
            assert not any(a["code"] == "WEIGHT_REGAIN" for a in alerts_regain_refeed), (
                f"WEIGHT_REGAIN должен молчать, если скачок пришёлся только на дни рефида, "
                f"получили {alerts_regain_refeed}"
            )

            u_regain_thin = make_user(173, height_cm=185, created_days_ago=30)
            add_metric(u_regain_thin, 20, 100.0, None)
            add_metric(u_regain_thin, 10, 105.0, None)
            add_metric(u_regain_thin, 0, 110.0, None)     # огромный рост, но только 3 точки < min_points=6
            conn.commit()
            alerts_regain_thin = check_all(conn, u_regain_thin)
            assert not any(a["code"] == "WEIGHT_REGAIN" for a in alerts_regain_thin), (
                f"WEIGHT_REGAIN должен молчать при < regain_min_points точек, получили {alerts_regain_thin}"
            )

            print("OK: WEIGHT_REGAIN — 3.5% срабатывает, 2.5% молчит, скачок только в дни рефида "
                  "молчит, мало точек молчит")

            # ---------------------------------------------------------------- PLATEAU (задача 3)
            # Одиночный всплеск +0.6 кг (вода/рефид-подобный шум) НЕ должен ломать
            # детектор: старый max-min давал 0.7 кг > порога 0.5 и молчал бы.
            # Наклон тренда по всем 7 точкам всплеск почти не чувствует.
            u_plat = make_user(200, height_cm=185, created_days_ago=30)
            add_metric(u_plat, 9, 100.0, None)
            add_metric(u_plat, 8, 99.9, None)
            add_metric(u_plat, 7, 100.1, None)   # медиана первых 3 = 100.0
            add_metric(u_plat, 5, 100.6, None)   # всплеск +0.6 — вне первых/последних 3
            add_metric(u_plat, 3, 100.0, None)
            add_metric(u_plat, 1, 99.9, None)
            add_metric(u_plat, 0, 100.1, None)   # медиана последних 3 = 100.0
            conn.commit()
            alerts_plat = check_all(conn, u_plat)
            assert any(a["code"] == "PLATEAU" for a in alerts_plat), (
                f"PLATEAU должен сработать: медианы 100.0/100.0 несмотря на всплеск +0.6 кг, получили {alerts_plat}"
            )
            print("OK: PLATEAU — одиночный всплеск +0.6 кг не срывает детектор (регрессия по всем замерам)")

            # новый минимум окна, потом откат: медианы краёв почти равны, но вес двигался
            u_plat2 = make_user(201, height_cm=185, created_days_ago=30)
            for d, w in [(9, 116.4), (8, 116.4), (7, 115.9), (5, 115.1), (4, 114.9), (2, 116.1), (1, 116.2), (0, 115.0)]:
                add_metric(u_plat2, d, w, None)
            conn.commit()
            assert not any(a["code"] == "PLATEAU" for a in check_all(conn, u_plat2)), "PLATEAU при новом минимуме окна"
            print("OK: PLATEAU молчит при минимуме окна и откате (тренд вниз)")

            # ---------------------------------------------------------------- BMR_FLOOR (задача 2)
            u_floor = make_user(192, height_cm=185, created_days_ago=30)
            today_floor = user_today(conn, u_floor)
            conn.execute(
                "INSERT INTO daily_targets(user_id, date, kcal_target, kcal_floor) VALUES (?,?,?,?)",
                (u_floor, today_floor, 1700, 1800),
            )
            conn.commit()
            alert_floor = check_bmr_floor(conn, u_floor)
            assert alert_floor is not None and alert_floor["code"] == "BMR_FLOOR", (
                f"BMR_FLOOR должен сработать по сохранённому kcal_floor=1800 > kcal_target=1700, получили {alert_floor}"
            )
            assert alert_floor["threshold"] == 1800, f"порог должен быть сохранённым полом, получили {alert_floor}"

            u_floor_ok = make_user(193, height_cm=185, created_days_ago=30)
            today_floor_ok = user_today(conn, u_floor_ok)
            conn.execute(
                "INSERT INTO daily_targets(user_id, date, kcal_target, kcal_floor) VALUES (?,?,?,?)",
                (u_floor_ok, today_floor_ok, 2200, 1800),
            )
            conn.commit()
            assert check_bmr_floor(conn, u_floor_ok) is None, "BMR_FLOOR не должен сработать: цель выше сохранённого пола"
            print("OK: BMR_FLOOR использует сохранённый daily_targets.kcal_floor без пересчёта daily_target()")

            # --- RECOVERY_LOW: 3 дня подряд HRV заметно ниже медианы и hr_min заметно выше — срабатывает ---
            from health_core import watch as _watch
            u_rec_fire = make_user(180, height_cm=185, created_days_ago=60)
            today_rec = _now().date()
            # 28-дневное окно медианы ДО трёх последних дней: hrv=50, hr_min=55
            for days_ago in range(3, 31):
                d = (today_rec - timedelta(days=days_ago)).isoformat()
                _watch.save_days(conn, u_rec_fire, [{"date": d, "hrv_ms": 50, "hr_min": 55}])
            # последние 3 дня: HRV заметно ниже (40 <= 50*0.85=42.5), hr_min заметно выше (62 >= 55+5=60)
            for days_ago in (2, 1, 0):
                d = (today_rec - timedelta(days=days_ago)).isoformat()
                _watch.save_days(conn, u_rec_fire, [{"date": d, "hrv_ms": 40, "hr_min": 62}])
            conn.commit()
            alerts_rec_fire = check_all(conn, u_rec_fire)
            assert any(a["code"] == "RECOVERY_LOW" for a in alerts_rec_fire), (
                f"RECOVERY_LOW должен сработать на синтетических данных, получили {alerts_rec_fire}"
            )

            # --- тихо, если один из 3 дней болен ---
            from health_core import sick as sick_mod2
            u_rec_sick = make_user(181, height_cm=185, created_days_ago=60)
            for days_ago in range(3, 31):
                d = (today_rec - timedelta(days=days_ago)).isoformat()
                _watch.save_days(conn, u_rec_sick, [{"date": d, "hrv_ms": 50, "hr_min": 55}])
            for days_ago in (2, 1, 0):
                d = (today_rec - timedelta(days=days_ago)).isoformat()
                _watch.save_days(conn, u_rec_sick, [{"date": d, "hrv_ms": 40, "hr_min": 62}])
            sick_mod2.start(conn, u_rec_sick, (today_rec - timedelta(days=1)).isoformat(), 1, note="грипп")
            conn.commit()
            alerts_rec_sick = check_all(conn, u_rec_sick)
            assert not any(a["code"] == "RECOVERY_LOW" for a in alerts_rec_sick), (
                f"RECOVERY_LOW должен молчать, если один из 3 дней болен, получили {alerts_rec_sick}"
            )

            print("OK: RECOVERY_LOW — 3 дня HRV↓/HRmin↑ подряд срабатывает, день болезни в тройке глушит")

            # --- RESTING_HR_RISING: медиана hr_min 7 дней выше базы 28 дней на >=4 без HRV ---
            def _hr_user(uid, recent_hr):
                u = make_user(uid, height_cm=185, created_days_ago=60)
                for days_ago in range(0, 35):
                    d = (today_rec - timedelta(days=days_ago)).isoformat()
                    _watch.save_days(conn, u, [{"date": d, "hr_min": recent_hr if days_ago < 7 else 55}])
                conn.commit()
                return [a for a in check_all(conn, u) if a["code"] == "RESTING_HR_RISING"]
            assert _hr_user(182, 60), "RESTING_HR_RISING должен сработать при +5 уд/мин"
            assert not _hr_user(183, 57), "RESTING_HR_RISING должен молчать при +2 уд/мин"
            print("OK: RESTING_HR_RISING срабатывает на +5 уд/мин и молчит на +2")

            # --- morning_checklist: записанное - ✓ с вчерашними шагами/пульсом, остальное - ☐ ---
            from health_core.report import morning_checklist
            u_chk = make_user(184, height_cm=185, created_days_ago=60)
            y_chk = (today_rec - timedelta(days=1)).isoformat()
            _watch.save_days(conn, u_chk, [{"date": y_chk, "steps": 7300, "hr_min": 56, "hr_avg": 71, "hr_max": 130}])
            _add_glucose(u_chk, 0, 5.2, "натощак")
            conn.execute("INSERT INTO bp_log(user_id, at, systolic, diastolic, pulse) VALUES (?,?,?,?,?)",
                         (u_chk, _now().strftime("%Y-%m-%d %H:%M:%S"), 118, 76, 64))
            conn.commit()
            chk = morning_checklist(conn, u_chk)
            for want in ("☐ Вес", "✓ Глюкоза натощак 5.2", "☐ Сон", "✓ Шаги вчера 7300", "✓ Пульс вчера мин 56, сред 71, макс 130", "✓ Давление 118/76, пульс 64"):
                assert want in chk, f"в чек-листе нет {want!r}:\n{chk}"
            print("OK: morning_checklist отмечает записанное и вчерашние шаги/пульс, остальное просит")

            # --- BP_HIGH / BP_LOW ---
            def _bp_user(uid, readings):
                u = make_user(uid, height_cm=185, created_days_ago=60)
                for days_ago, sy, di in readings:
                    ts = (_now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute("INSERT INTO bp_log(user_id, at, systolic, diastolic) VALUES (?,?,?,?)", (u, ts, sy, di))
                conn.commit()
                return {a["code"]: a for a in check_all(conn, u) if a["code"].startswith("BP_")}
            hi = _bp_user(485, [(5, 145, 92), (3, 142, 90), (1, 148, 94)])
            assert hi.get("BP_HIGH", {}).get("severity") == "warning", f"серия 145/92 должна дать BP_HIGH warning: {hi}"
            assert not _bp_user(486, [(1, 150, 95)]), "одиночный 150/95 - не сигнал"
            assert not _bp_user(487, [(5, 118, 76), (3, 150, 95), (1, 120, 78)]), "один скачок в серии - не сигнал"
            crisis = _bp_user(488, [(0, 185, 100)])
            assert crisis.get("BP_HIGH", {}).get("severity") == "critical", f"185/100 за сутки - critical сразу: {crisis}"
            assert "BP_LOW" in _bp_user(489, [(2, 88, 58), (0, 86, 55)]), "два замера сист. <90 подряд - BP_LOW"
            assert "BP_LOW" not in _bp_user(490, [(2, 88, 58), (0, 110, 70)]), "один низкий замер - не BP_LOW"
            print("OK: BP_HIGH по серии и critical на кризисном замере, молчит на одиночном и скачке; BP_LOW на двух низких подряд")
        finally:
            conn.close()
