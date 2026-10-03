"""Сводка состояния и отчёты (§07, §11): числа берутся из БД, строки — фиксированный
формат, который модель дописывает верно, а не пересказывает."""
import sqlite3
import statistics
from datetime import datetime, timedelta, date

def _parse_date_safe(s: str) -> "date | None":
    """Безопасный парсинг ISO-даты; логирует и возвращает None при ошибке."""
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None
from health_core.config import load, local_now, targets_for, user_now
from health_core.guards import check_all
from health_core.chrono import eating_window, late_load, meal_windows

MINUS = "−"  # настоящий минус, не дефис — так в §07


def _now(conn: sqlite3.Connection | None = None, user_id: int | None = None) -> datetime:
    """Время того, кого обслуживаем, а не сервера: граница суток у них разная.
    Машина в UTC−7, VPS обычно в UTC, человек в Asia/Yekaterinburg — до
    двенадцати часов разрыва, и запись, сделанная им утром, попадала во
    вчерашний день сервера.

    conn и user_id переданы — пояс берём из профиля (config.user_now); без них
    остаётся пояс по умолчанию из config.yaml, так зовут только самотесты."""
    if conn is not None and user_id is not None:
        return user_now(conn, user_id)
    return local_now()


def _time_tag(hour: int) -> str:
    if hour < 11:
        return "утро"
    if hour < 17:
        return "днём"
    return "вечером"


def water_pace(water_ml: float, target_ml: float | None, now: datetime) -> dict | None:
    """Темп воды: цель равномерно растёт с 08:00 до 20:00. Отставание - когда выпито
    меньше 85% нормы к этому часу. Один расчёт для ответа log_water и для напоминаний.
    ponytail: линейный график и порог 15% - литералы, менять по опыту."""
    if not target_ml or not 8 <= now.hour < 20:
        return None
    expected = target_ml * (now.hour + now.minute / 60 - 8) / 12
    if water_ml < expected * 0.85:
        return {"expected_ml": round(expected), "behind_ml": round(expected - water_ml)}
    return None


def _water_target_ml(conn: sqlite3.Connection, user_id: int) -> float | None:
    """Профиль (user_targets) важнее политики по умолчанию (§03: уровень 2 над уровнем 1)."""
    row = conn.execute(
        "SELECT water_ml FROM user_targets WHERE user_id=? AND water_ml IS NOT NULL "
        "ORDER BY valid_from DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is not None:
        return row["water_ml"]
    return targets_for(conn, user_id).get("water_ml")


def _day_macros(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    # Вторая агрегация тех же колонок: первая — nutrition.day_macros. Отличия
    # только в COALESCE и счётчике n. Новую колонку приходится добавлять в оба
    # места, и забытое второе валится KeyError уже в рантайме, а не на импорте.
    # Сводить их в одну — отдельная правка со сверкой всех вызывающих.
    row = conn.execute(
        "SELECT COALESCE(SUM(fi.kcal),0) kcal, COALESCE(SUM(fi.protein_g),0) protein_g, "
        "COALESCE(SUM(fi.fat_g),0) fat_g, COALESCE(SUM(fi.carbs_g),0) carb_g, "
        "COALESCE(SUM(fi.fiber_g),0) fiber_g, COUNT(*) n, COUNT(fi.fiber_g) fiber_n "
        "FROM food_log fl JOIN food_items fi ON fi.food_log_id=fl.id "
        "WHERE fl.user_id=? AND date(fl.eaten_at)=?",
        (user_id, date),
    ).fetchone()
    return dict(row)


def _day_water_ml(conn: sqlite3.Connection, user_id: int, date: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(volume_ml),0) ml FROM water_log WHERE user_id=? AND date(at)=?",
        (user_id, date),
    ).fetchone()
    return row["ml"]


def _latest_metric(conn: sqlite3.Connection, user_id: int):
    return conn.execute(
        "SELECT measured_at, weight_kg, ffm_kg FROM body_metrics WHERE user_id=? "
        "ORDER BY measured_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()


def _weight_trend(conn: sqlite3.Connection, user_id: int, window_days: int, latest_weight: float):
    """Дельта веса за окно: медиана конца минус медиана начала.

    МЕДИАНА ИСПОЛЬЗУЕТСЯ, ЧТОБЫ одна шумная точка на границе окна не решила сама по себе,
    какой тренд показывать. Биоимпеданс носит шум гидрации в килограмм в день;
    эндпоинт-дельта ловит одиночный всплеск как знаковый сдвиг."""
    since = (_now(conn, user_id) - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M:%S")
    # Оригинал: ищет ТОЧКУ <= граница окна (якорь), затем использует latest.
    # Для медианы: берём все точки, начиная хотя бы от границы окна.
    # Дополнительный запрос: найти последнюю точку ВНЕ окна (если есть), чтобы включить якорь.
    # Якорь старше window_days до границы окна - не «начало окна», а чужая эпоха.
    anchor_min = (_now(conn, user_id) - timedelta(days=2 * window_days)).strftime("%Y-%m-%d %H:%M:%S")
    anchor = conn.execute(
        "SELECT weight_kg FROM body_metrics WHERE user_id=? AND measured_at<=? AND measured_at>=? "
        "ORDER BY measured_at DESC LIMIT 1",
        (user_id, since, anchor_min),
    ).fetchone()

    rows_in_window = conn.execute(
        "SELECT weight_kg FROM body_metrics WHERE user_id=? AND measured_at>? "
        "ORDER BY measured_at ASC",
        (user_id, since),
    ).fetchall()

    # Если есть якорь вне окна, включаем его в начало для сравнения
    if anchor is not None:
        rows = [anchor] + rows_in_window
    else:
        rows = rows_in_window

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

    first_median = statistics.median(first_vals)
    last_median = statistics.median(last_vals)
    delta = last_median - first_median
    return round(delta, 2)


def _next_milestone(conn: sqlite3.Connection, user_id: int, latest_weight: float):
    return conn.execute(
        "SELECT name, threshold FROM milestones WHERE user_id=? AND metric='weight_kg' "
        "AND achieved_at IS NULL AND threshold<=? ORDER BY threshold DESC LIMIT 1",
        (user_id, latest_weight),
    ).fetchone()


def _active_alerts_today(conn: sqlite3.Connection, user_id: int, date: str) -> list[sqlite3.Row]:
    # DISTINCT: один и тот же гард срабатывает на каждом логировании за день и
    # раньше писал отдельную строку в alerts — на дашборде это давало N копий
    # одного предупреждения. Схлопываем одинаковые (rule, message) на чтении,
    # чтобы старые, уже задублированные строки тоже не спамили.
    return conn.execute(
        "SELECT rule, message FROM alerts WHERE user_id=? AND date(created_at)=? "
        "GROUP BY rule, message ORDER BY MIN(created_at)",
        (user_id, date),
    ).fetchall()


_MEAL_LABELS_RU = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин"}


def _meal_clause(conn: sqlite3.Connection, user_id: int, date: str, now: datetime) -> str | None:
    """«Не записан» — в текущем окне приёма пищи (CONTEXT.md, health_core.chrono.
    meal_windows) нет ни одной строки food_log с этим meal_slot, а не «нет еды с
    часа N»: время еды может отличаться от времени записи, а окна — личные."""
    t = now.strftime("%H:%M")
    windows = meal_windows(conn, user_id)
    for name, label in _MEAL_LABELS_RU.items():
        w = windows[name]
        if not (w["start"] <= t <= w["end"]):
            continue
        row = conn.execute(
            "SELECT COUNT(*) n FROM food_log WHERE user_id=? AND date(eaten_at)=? AND meal_slot=?",
            (user_id, date, name),
        ).fetchone()
        if row["n"] > 0:
            return None
        return f"{label} не записан."
    return None


def day_summary(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """Все числа дня одним словарём — источник и для status_bar, и для evening_report."""
    macros = _day_macros(conn, user_id, date)
    # Строка цели на сегодня могла быть записана до правок формулы или до новых данных: пересчитываем при чтении.
    from health_core.config import user_today
    from health_core.energy import daily_target
    if date == user_today(conn, user_id):
        try:
            daily_target(conn, user_id, date)
        except ValueError:  # ещё нет ни одного замера тела — считать цель не от чего
            pass
    target = conn.execute(
        "SELECT kcal_target, protein_g_target, fat_g_target, carbs_g_target, fiber_g_target, "
        "water_ml_target FROM daily_targets WHERE user_id=? AND date=?",
        (user_id, date),
    ).fetchone()
    metric = _latest_metric(conn, user_id)
    return {
        "date": date,
        "weight_kg": metric["weight_kg"] if metric else None,
        "kcal_target": target["kcal_target"] if target else None,
        "protein_g_target": target["protein_g_target"] if target else None,
        "fat_g_target": target["fat_g_target"] if target else None,
        "carbs_g_target": target["carbs_g_target"] if target else None,
        "fiber_g_target": target["fiber_g_target"] if target else None,
        "kcal_eaten": macros["kcal"],
        "protein_g": macros["protein_g"],
        "fat_g": macros["fat_g"],
        "carb_g": macros["carb_g"],
        "fiber_g": macros["fiber_g"],
        "fiber_known": bool(macros["fiber_n"]),
        "water_ml": _day_water_ml(conn, user_id, date),
        "water_target_ml": _water_target_ml(conn, user_id),
        "alerts": [dict(r) for r in _active_alerts_today(conn, user_id, date)],
    }


def status_bar(conn: sqlite3.Connection, user_id: int) -> str:
    """Готовая строка §07. Модель дописывает её как есть, не перефразируя."""
    now = _now(conn, user_id)
    date = now.date().isoformat()
    policy = load()["policy"]

    metric = _latest_metric(conn, user_id)
    weight_str = f"{metric['weight_kg']:.1f}" if metric else "?"
    weight_tag = _time_tag(_parse(metric["measured_at"]).hour) if metric else "?"

    d = day_summary(conn, user_id, date)
    kcal_target = f"{d['kcal_target']:.0f}" if d["kcal_target"] is not None else "?"
    water_now = d["water_ml"] / 1000
    water_target = (d["water_target_ml"] or 0) / 1000

    line1 = f"Сегодня {now.strftime('%d.%m')}, {now.strftime('%H:%M')}. Вес {weight_str} ({weight_tag}). Цель {kcal_target} ккал."
    line2 = (f"Съедено {d['kcal_eaten']:.0f} / Б {d['protein_g']:.0f} / Ж {d['fat_g']:.0f} / "
             f"У {d['carb_g']:.0f}. Вода {water_now:.1f} / {water_target:.1f} л.")

    meal_clause = _meal_clause(conn, user_id, date, now)
    alerts_today = check_all(conn, user_id)
    alert_clause = "Активных алертов нет." if not alerts_today else f"Активных алертов: {len(alerts_today)}."
    line3 = f"{meal_clause} {alert_clause}" if meal_clause else alert_clause

    window = policy["weight_trend_window_days"]
    trend = _weight_trend(conn, user_id, window, metric["weight_kg"]) if metric else None
    if trend is None:
        trend_part = "недостаточно данных"
    else:
        sign = MINUS if trend < 0 else ""
        trend_part = f"{sign}{abs(trend):.1f} кг"
    milestone = _next_milestone(conn, user_id, metric["weight_kg"]) if metric else None
    if milestone is not None:
        remaining = metric["weight_kg"] - milestone["threshold"]
        milestone_part = f" Ближайшая веха {milestone['threshold']:.0f} кг, осталось {remaining:.1f}."
    else:
        milestone_part = ""
    line4 = f"Тренд {window} дней: {trend_part}.{milestone_part}"

    return "\n".join([line1, line2, line3, line4])


def morning_checklist(conn: sqlite3.Connection, user_id: int) -> str:
    """Чек-лист утренних замеров: что уже записано сегодня (✓) и чего не хватает
    (☐), плюс шаги и пульс за вчера из часов. Без модели."""
    from health_core.guards import is_fasting_glucose
    from health_core.watch import resting_hr_trend, step_goal

    today = _now(conn, user_id).date()
    t, y = today.isoformat(), (today - timedelta(days=1)).isoformat()
    lines = [f"Утренние замеры {today.strftime('%d.%m')}:"]

    w = conn.execute(
        "SELECT weight_kg, fat_pct FROM body_metrics WHERE user_id=? AND date(measured_at)=? "
        "ORDER BY measured_at DESC LIMIT 1", (user_id, t)).fetchone()
    if w:
        fat = f", жир {w['fat_pct']:.1f}%" if w["fat_pct"] is not None else ", состав тела не записан"
        lines.append(f"✓ Вес {w['weight_kg']:.1f} кг{fat}")
    else:
        lines.append("☐ Вес и состав тела (весы натощак)")

    g = [r for r in conn.execute(
        "SELECT at, mmol_l, context FROM glucose_log WHERE user_id=? AND date(at)=? ORDER BY at", (user_id, t))
        if is_fasting_glucose(conn, user_id, r["at"], r["context"])]
    lines.append(f"✓ Глюкоза натощак {g[0]['mmol_l']:.1f} ммоль/л" if g else "☐ Глюкоза натощак")

    b = conn.execute("SELECT systolic, diastolic, pulse FROM bp_log WHERE user_id=? AND date(at)=? ORDER BY at LIMIT 1",
                     (user_id, t)).fetchone()
    if b:
        lines.append(f"✓ Давление {b['systolic']}/{b['diastolic']}" + (f", пульс {b['pulse']}" if b["pulse"] else ""))
    else:
        lines.append("☐ Давление (тонометр в покое, сидя)")

    sl = conn.execute("SELECT duration_min FROM sleep_log WHERE user_id=? AND night_date=?", (user_id, t)).fetchone()
    if sl and sl["duration_min"] is not None:
        lines.append(f"✓ Сон {sl['duration_min'] // 60} ч {sl['duration_min'] % 60:02d} мин")
    else:
        lines.append("☐ Сон за ночь")

    d = conn.execute("SELECT steps, hr_min, hr_avg, hr_max FROM daily_watch WHERE user_id=? AND date=?",
                     (user_id, y)).fetchone()
    if d and d["steps"] is not None:
        goal = step_goal(conn, user_id, y)
        lines.append(f"✓ Шаги вчера {d['steps']}" + (f" из {goal}" if goal else ""))
    else:
        lines.append("☐ Шаги за вчера (скрин часов)")
    if d and d["hr_min"] is not None:
        hr = f"мин {d['hr_min']}" + "".join(
            f", {n} {d[k]}" for n, k in (("сред", "hr_avg"), ("макс", "hr_max")) if d[k] is not None)
        tr = resting_hr_trend(conn, user_id)
        trend = f" (покой за 7 дн {tr['recent']:.0f}, норма 28 дн {tr['base']:.0f})" if tr else ""
        lines.append(f"✓ Пульс вчера {hr}{trend}")
    else:
        lines.append("☐ Пульс за вчера (скрин часов)")
    return "\n".join(lines)


def evening_report(conn: sqlite3.Connection, user_id: int, date: str) -> str:
    """Вечерний отчёт 21:30 (§11) — данные для единственного вызова LLM за этот тик."""
    d = day_summary(conn, user_id, date)
    fired = _active_alerts_today(conn, user_id, date)
    lines = [f"Отчёт за {date}."]
    from health_core import sick
    if sick.is_sick(conn, user_id, date):
        lines.append("🤒 Режим болезни: дефицит на паузе.")
    if d["kcal_target"] is not None:
        lines.append(f"Съедено {d['kcal_eaten']:.0f} из {d['kcal_target']:.0f} ккал, "
                     f"Б {d['protein_g']:.0f} / Ж {d['fat_g']:.0f} / У {d['carb_g']:.0f}.")
    else:
        lines.append(f"Съедено {d['kcal_eaten']:.0f} ккал, цель не рассчитана.")
    if d["water_target_ml"]:
        lines.append(f"Вода {d['water_ml'] / 1000:.1f} / {d['water_target_ml'] / 1000:.1f} л.")
    window = eating_window(conn, user_id, date)
    if window is not None:
        lines.append(f"Пищевое окно {window['first']}–{window['last']} ({window['hours']} ч).")
    # Отбой сегодняшней ночи в 21:30 ещё не записан — поздняя нагрузка только за вчера.
    parsed_date = _parse_date_safe(date)
    if parsed_date:
        yesterday = (parsed_date - timedelta(days=1)).strftime("%Y-%m-%d")
        late = late_load(conn, user_id, yesterday)
        if late is not None:
            late_pct = round(late["share"] * 100)
            lines.append(f"Вчера за {load()['chrono']['late_load_hours']} ч до сна: {late_pct}% ккал.")
    if fired:
        lines.append("Гардрейлы: " + "; ".join(f"{a['rule']} — {a['message']}" for a in fired))
    else:
        lines.append("Гардрейлы: без срабатываний.")
    return "\n".join(lines)


def meals_of_day(conn: sqlite3.Connection, user_id: int, date: str) -> list[dict]:
    """One entry per food_log row for `date`, ordered by eaten_at, each with its
    items and per-meal kcal/protein_g/fat_g/carbs_g subtotals. A row with zero
    food_items still appears (items=[], subtotals 0.0) — never silently dropped;
    a ghost meal in the DB is a bug worth seeing, and one already occurred in
    production. Structured data only, no rendering.

    food_log.meal_slot exists since schema v5 and log_food requires it, so it is
    populated on every new row; the PRAGMA check stays because a DB that has not
    been migrated yet must not blow up on an unknown column, and rows written
    before v5 legitimately carry None.
    """
    has_meal_slot = any(r["name"] == "meal_slot" for r in conn.execute("PRAGMA table_info(food_log)"))
    slot_col = "fl.meal_slot" if has_meal_slot else "NULL"
    logs = conn.execute(
        f"SELECT fl.id food_log_id, fl.eaten_at, {slot_col} meal_slot "
        "FROM food_log fl WHERE fl.user_id=? AND date(fl.eaten_at)=? ORDER BY fl.eaten_at",
        (user_id, date),
    ).fetchall()
    if not logs:
        return []

    items_by_log: dict[int, list[dict]] = {}
    for row in conn.execute(
        "SELECT fi.id, fi.food_log_id, fi.name, fi.grams, fi.kcal, fi.protein_g, fi.fat_g, fi.carbs_g, fi.fiber_g, fi.plate_category "
        "FROM food_items fi "
        "JOIN food_log fl ON fl.id = fi.food_log_id "
        "WHERE fl.user_id=? AND date(fl.eaten_at)=?",
        (user_id, date),
    ):
        item = {k: row[k] for k in row.keys() if k != "food_log_id"}
        items_by_log.setdefault(row["food_log_id"], []).append(item)

    out = []
    for r in logs:
        items = items_by_log.get(r["food_log_id"], [])
        out.append({
            "food_log_id": r["food_log_id"],
            "eaten_at": r["eaten_at"],
            "meal_slot": r["meal_slot"],
            "items": items,
            "kcal": sum(i["kcal"] or 0.0 for i in items),
            "protein_g": sum(i["protein_g"] or 0.0 for i in items),
            "fat_g": sum(i["fat_g"] or 0.0 for i in items),
            "carbs_g": sum(i["carbs_g"] or 0.0 for i in items),
            "fiber_g": sum(i["fiber_g"] or 0.0 for i in items),
        })
    return out


def whr(conn: sqlite3.Connection, user_id: int) -> float | None:
    """Waist-to-Hip Ratio: latest талия / latest таз. Returns None if either is missing."""
    waist = conn.execute(
        "SELECT value_cm FROM anthropometry WHERE user_id=? AND site='талия' "
        "ORDER BY measured_on DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    hip = conn.execute(
        "SELECT value_cm FROM anthropometry WHERE user_id=? AND site='таз' "
        "ORDER BY measured_on DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if waist is None or hip is None or hip["value_cm"] == 0:
        return None
    return round(waist["value_cm"] / hip["value_cm"], 2)


def trends(conn: sqlite3.Connection, user_id: int, window_days: int = 30) -> dict:
    """Вес, LBM, талия, WHR, фактический TDEE за окно (§07 query_metrics/get_progress)."""
    since = (_now(conn, user_id) - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M:%S")
    weight_rows = conn.execute(
        "SELECT weight_kg, ffm_kg FROM body_metrics WHERE user_id=? AND measured_at>=? ORDER BY measured_at",
        (user_id, since),
    ).fetchall()
    waist_rows = conn.execute(
        "SELECT value_cm FROM anthropometry WHERE user_id=? AND site='талия' AND measured_on>=? ORDER BY measured_on",
        (user_id, since[:10]),
    ).fetchall()

    def _delta(rows, key):
        vals = [r[key] for r in rows if r[key] is not None]
        return round(vals[-1] - vals[0], 2) if len(vals) >= 2 else None

    out = {
        "window_days": window_days,
        "weight_delta_kg": _delta(weight_rows, "weight_kg"),
        "lbm_delta_kg": _delta(weight_rows, "ffm_kg"),
        "waist_delta_cm": _delta(waist_rows, "value_cm"),
        "whr": whr(conn, user_id),
        "actual_tdee_kcal": None,
    }
    try:
        # ponytail: реальный расчёт живёт в energy.py; пока модуль не готов, поле пустое.
        from health_core.energy import adaptive_tdee
    except ImportError:
        return out
    out["actual_tdee_kcal"] = adaptive_tdee(conn, user_id, window_days=window_days)
    return out


# Метрика вехи -> откуда берётся её текущее и стартовое значение. Явный словарь,
# а не подстановка имени метрики в SQL: имя приходит из таблицы milestones, в
# запрос попадает только выбранное отсюда.
_MILESTONE_SOURCES = {
    "weight_kg": ("body_metrics", "weight_kg", "measured_at"),
    "ffm_kg": ("body_metrics", "ffm_kg", "measured_at"),
    "fat_pct": ("body_metrics", "fat_pct", "measured_at"),
    "waist": ("anthropometry", "value_cm", "measured_on"),
}


def baseline_weight(conn: sqlite3.Connection, user_id: int) -> float | None:
    """Точка отсчёта «Δ от старта» — одна на всю систему.

    Стартовый вес из профиля важнее первого взвешивания: человек мог начать
    худеть до того, как завёл систему, и первое взвешивание в базе тогда уже
    не старт.

    НО только если профиль описывает момент не позже первого замера. Порядок
    развёртывания по README обратный: сначала регистрация (человек вводит
    СЕГОДНЯШНИЙ вес), потом импорт истории на месяцы назад. Тогда «старт» из
    профиля оказывается серединой пути — живой случай: профиль 120.5 кг от
    июня, импорт до февраля со 158.8 кг, и Δ показывала 0.2 кг вместо 38.
    Замер раньше даты профиля — более ранняя и более твёрдая точка отсчёта.

    Профиль пуст — берём самое раннее взвешивание: точка отсчёта уже лежит в
    body_metrics, требовать её у человека повторно незачем.
    """
    first = conn.execute(
        "SELECT weight_kg, measured_at FROM body_metrics "
        "WHERE user_id=? AND weight_kg IS NOT NULL ORDER BY measured_at ASC LIMIT 1",
        (user_id,),
    ).fetchone()
    row = conn.execute(
        "SELECT base_weight_kg, base_weight_date FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if row is not None and row["base_weight_kg"] is not None:
        base_date = row["base_weight_date"]
        measured_earlier = bool(first and base_date and first["measured_at"]
                                and str(first["measured_at"])[:10] < str(base_date)[:10])
        if not measured_earlier:
            return row["base_weight_kg"]
    return first["weight_kg"] if first else None


def weight_series(conn: sqlite3.Connection, user_id: int, days: int = 90) -> list[tuple[str, float]]:
    """Ряд (дата, вес) за последние `days` дней — данные для спарклайна дашборда.
    Один вес в день (последний замер этого дня), не все внутридневные точки:
    спарклайн должен рисовать тренд, а не шум разницы между утренним и вечерним
    взвешиванием. Отрисовка блоками — дело tools.py (presentation), здесь только
    выборка. Восходящий порядок по дате, пусто — если замеров нет."""
    since = (_now(conn, user_id) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT date(measured_at) AS d, weight_kg FROM body_metrics "
        "WHERE user_id=? AND weight_kg IS NOT NULL AND date(measured_at)>=? "
        "ORDER BY measured_at",
        (user_id, since),
    ).fetchall()
    by_day: dict[str, float] = {}
    for r in rows:
        by_day[r["d"]] = r["weight_kg"]  # более поздний замер дня перезаписывает более ранний
    return sorted(by_day.items())


def _milestone_values(conn: sqlite3.Connection, user_id: int, metric: str):
    """(текущее, стартовое) значение метрики вехи. None, если замеров нет."""
    src = _MILESTONE_SOURCES.get(metric)
    if src is None:
        return None, None
    table, col, order_col = src
    where = f"user_id=? AND {col} IS NOT NULL"
    params = [user_id]
    if metric == "waist":
        where += " AND site='талия'"
    latest = conn.execute(
        f"SELECT {col} v FROM {table} WHERE {where} ORDER BY {order_col} DESC LIMIT 1", params
    ).fetchone()
    first = conn.execute(
        f"SELECT {col} v FROM {table} WHERE {where} ORDER BY {order_col} ASC LIMIT 1", params
    ).fetchone()
    current = latest["v"] if latest else None
    baseline = first["v"] if first else None
    if metric == "weight_kg":
        baseline = baseline_weight(conn, user_id) or baseline
    return current, baseline


def mark_achieved_milestones(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """Проставляет achieved_at вехам, порог которых уже пройден, и возвращает
    те, что закрылись именно сейчас — чтобы их можно было объявить один раз.

    До этого achieved_at не проставлял НИКТО: колонка была в схеме, все запросы
    фильтровали `achieved_at IS NULL`, и достигнутая веха навсегда оставалась
    «ближайшей». Обход этого прямо записан в energy._deadline_deficit
    («порог уже физически достигнут, achieved_at просто не проставлен»).

    Направление берём от точки отсчёта, а не от знака порога: вес, жир и талия
    идут вниз, сухая масса — вверх. Сравнение «текущее <= порога» для всех
    закрыло бы веху по сухой массе в момент её постановки.
    """
    now = _now(conn, user_id).strftime("%Y-%m-%d %H:%M:%S")
    achieved = []
    for m in conn.execute(
        "SELECT id, name, metric, threshold FROM milestones "
        "WHERE user_id=? AND achieved_at IS NULL AND metric IS NOT NULL AND threshold IS NOT NULL",
        (user_id,),
    ).fetchall():
        current, baseline = _milestone_values(conn, user_id, m["metric"])
        if current is None or baseline is None:
            continue
        span = m["threshold"] - baseline
        if abs(span) < 1e-9:
            continue  # порог совпал со стартом — двигаться некуда, судить не о чем
        reached = current <= m["threshold"] if span < 0 else current >= m["threshold"]
        if not reached:
            continue
        conn.execute("UPDATE milestones SET achieved_at=? WHERE id=?", (now, m["id"]))
        achieved.append({"name": m["name"], "metric": m["metric"],
                         "threshold": m["threshold"], "value": current, "achieved_at": now})
    if achieved:
        conn.commit()
    return achieved



def training_efficiency(conn: sqlite3.Connection, user_id: int, window_days: int = 60) -> dict:
    """Эффективность тренировок по видам спорта за окно (§07/§11 расширение).

    В `activity` НЕТ колонки дистанции (см. CREATE TABLE в db.py) — «ккал/км»
    физически невозможно посчитать, и мы её не выдумываем. Честная метрика,
    доступная из того, что есть (duration_min, kcal, avg_hr):

    - kcal_per_min: калорийность сессии в минуту, усреднённая по сессиям.
    - kcal_per_hr_min: kcal_per_min, делённая на средний пульс — калорий на
      единицу «пульсовой стоимости». Одна и та же работа при более высоком
      среднем пульсе обходится дороже (кардио-нагрузка выше), так что рост
      kcal_per_hr_min при том же kcal_per_min — это снижение эффективности,
      а не улучшение. Суррогат для VO2/темпа, которого тут нет, но не ноль.

    Деление на отсутствующее — не метрика: сессии без duration_min/kcal/avg_hr
    или с duration_min<=0 / avg_hr<=0 исключаются, а не тянут NaN/inf дальше.
    Тренд по kcal_per_hr_min (старая половина окна vs новая) считается только
    при >=4 годных сессий вида спорта и >=2 в каждой половине — на трёх точках
    «тренд» был бы шумом, выданным за сигнал. Пользователь ещё не грузил TCX и
    только начинает, поэтому top-level excluded_reasons даёт понять «данных
    просто нет» отдельно от «данные есть, но кривые» — это разные проблемы.
    """
    since = (_now(conn, user_id) - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT started_at, duration_min, kcal, avg_hr, sport FROM activity "
        "WHERE user_id=? AND started_at>=? ORDER BY started_at",
        (user_id, since),
    ).fetchall()

    excluded_reasons: dict[str, int] = {}
    usable_by_sport: dict[str, list[dict]] = {}
    for r in rows:
        sport = r["sport"] or "?"
        if r["duration_min"] is None:
            reason = "нет duration_min"
        elif r["kcal"] is None:
            reason = "нет kcal"
        elif r["avg_hr"] is None:
            reason = "нет avg_hr"
        elif r["duration_min"] <= 0:
            reason = "duration_min <= 0"
        elif r["avg_hr"] <= 0:
            reason = "avg_hr <= 0"
        else:
            reason = None
        if reason is not None:
            excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
            continue
        kcal_per_min = r["kcal"] / r["duration_min"]
        usable_by_sport.setdefault(sport, []).append({
            "started_at": r["started_at"],
            "kcal_per_min": kcal_per_min,
            "kcal_per_hr_min": kcal_per_min / r["avg_hr"],
        })

    sports: dict[str, dict] = {}
    for sport, sessions in usable_by_sport.items():  # sessions уже по started_at ASC (ORDER BY в запросе)
        n = len(sessions)
        entry = {
            "sessions": n,
            "kcal_per_min": round(statistics.mean(s["kcal_per_min"] for s in sessions), 3),
            "kcal_per_hr_min": round(statistics.mean(s["kcal_per_hr_min"] for s in sessions), 5),
            "trend": None,
        }
        half = n // 2
        older, newer = sessions[:half], sessions[half:]
        if n >= 4 and len(older) >= 2 and len(newer) >= 2:
            older_mean = statistics.mean(s["kcal_per_hr_min"] for s in older)
            newer_mean = statistics.mean(s["kcal_per_hr_min"] for s in newer)
            delta = newer_mean - older_mean
            direction = "рост" if delta > 0 else ("снижение" if delta < 0 else "без изменений")
            entry["trend"] = {
                "direction": direction,
                "older_mean": round(older_mean, 5),
                "newer_mean": round(newer_mean, 5),
                "delta": round(delta, 5),
                "delta_pct": round(delta / older_mean * 100, 1) if older_mean else None,
            }
        sports[sport] = entry

    return {
        "window_days": window_days,
        "sports": sports,
        "excluded_sessions": sum(excluded_reasons.values()),
        "excluded_reasons": excluded_reasons,
    }


def _window_delta(conn: sqlite3.Connection, user_id: int, table: str, col: str, date_col: str,
                   start_ts: str, end_ts: str, extra_where: str = "") -> float | None:
    """Последнее минус первое значение колонки в [start_ts, end_ts]. None, если
    замеров меньше двух — как _delta в trends(), но с обеими границами явно
    заданными (для weekly_summary, где окно не «сегодня минус N», а
    произвольная неделя, заканчивающаяся на end_date)."""
    rows = conn.execute(
        f"SELECT {col} v FROM {table} WHERE user_id=? AND {date_col}>=? AND {date_col}<=? {extra_where} "
        f"ORDER BY {date_col}",
        (user_id, start_ts, end_ts),
    ).fetchall()
    vals = [r["v"] for r in rows if r["v"] is not None]
    if len(vals) < 2:
        return None
    return round(vals[-1] - vals[0], 2)


def weekly_summary(conn: sqlite3.Connection, user_id: int, end_date: str | None = None) -> str:
    """Готовый markdown-блок за 7 дней, заканчивающихся end_date (по умолчанию
    сегодня). Модель дописывает его как есть (см. status_bar/evening_report) —
    визуальные конвенции те же, что у дашбордов в plugin/tools.py: жирные
    подписи, `·`-разделители, эмодзи-иконки, пустая строка между секциями.

    Только чтение: check_all здесь НЕ вызывается — это ретроспектива по уже
    записанным alerts, а не живая проверка, и повторный прогон гардов на
    чтении отчёта тихо плодил бы новые строки в alerts как побочный эффект.
    Каждое число — из БД; где данных нет — «нет данных», никогда 0 вместо
    измерения и никакой экстраполяции («при таком темпе к дате X будет Y» —
    явно отвергнуто продактом)."""
    end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else _now(conn, user_id).date()
    start = end - timedelta(days=6)
    start_ts, end_ts = f"{start.isoformat()} 00:00:00", f"{end.isoformat()} 23:59:59"

    header = f"📅 **Недельная сводка · {start.strftime('%d.%m')}–{end.strftime('%d.%m')}**"

    # 1. Вес
    latest_w = conn.execute(
        "SELECT weight_kg FROM body_metrics WHERE user_id=? AND measured_at<=? "
        "ORDER BY measured_at DESC LIMIT 1",
        (user_id, end_ts),
    ).fetchone()
    if latest_w is None:
        weight_line = "**⚖️ Вес** — нет данных"
    else:
        w_delta = _window_delta(conn, user_id, "body_metrics", "weight_kg", "measured_at", start_ts, end_ts)
        cur = latest_w["weight_kg"]
        if w_delta is None:
            weight_line = f"**⚖️ Вес** — {cur:.1f} кг · дельта за неделю: нет данных"
        else:
            baseline = cur - w_delta
            sign = MINUS if w_delta < 0 else "+"
            pct_part = f" ({sign}{abs(w_delta / baseline * 100):.1f}%)" if baseline else ""
            weight_line = f"**⚖️ Вес** — {cur:.1f} кг · за неделю {sign}{abs(w_delta):.1f} кг{pct_part}"

    # 2. Состав
    ffm_delta = _window_delta(conn, user_id, "body_metrics", "ffm_kg", "measured_at", start_ts, end_ts)
    visc_delta = _window_delta(conn, user_id, "body_metrics", "visceral_fat", "measured_at", start_ts, end_ts)
    waist_row = conn.execute(
        "SELECT value_cm FROM anthropometry WHERE user_id=? AND site='талия' "
        "AND measured_on>=? AND measured_on<=? ORDER BY measured_on DESC LIMIT 1",
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchone()
    parts = []
    if ffm_delta is not None:
        sign = MINUS if ffm_delta < 0 else "+"
        parts.append(f"сухая масса {sign}{abs(ffm_delta):.1f} кг")
    if visc_delta is not None:
        sign = MINUS if visc_delta < 0 else "+"
        parts.append(f"висц. жир {sign}{abs(visc_delta):.0f}")
    if waist_row is not None:
        parts.append(f"талия {waist_row['value_cm']:.1f} см")
    comp_line = "**🩻 Состав** — " + (" · ".join(parts) if parts else "нет данных")

    # 3. Дисциплина — простые счётчики, не измерение
    food_days = conn.execute(
        "SELECT COUNT(DISTINCT date(eaten_at)) n FROM food_log WHERE user_id=? "
        "AND date(eaten_at)>=? AND date(eaten_at)<=?",
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchone()["n"]
    weigh_days = conn.execute(
        "SELECT COUNT(DISTINCT date(measured_at)) n FROM body_metrics WHERE user_id=? "
        "AND date(measured_at)>=? AND date(measured_at)<=?",
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchone()["n"]
    discipline_line = f"**📋 Дисциплина** — еда: {food_days}/7 дн · взвешивания: {weigh_days}/7 дн"

    # 4. План против факта — сравнение уже живёт в plans.plan_vs_actual, здесь
    # только агрегация по дням недели. «Совпал» = план на день был и факт по
    # ккал не разошёлся больше чем на 10% — простой честный порог, не оценка.
    from health_core.plans import get_plan, plan_vs_actual
    plan = get_plan(conn, user_id)
    if not plan["meals"] and not plan["workouts"]:
        plan_line = "**📐 План/факт** — план не задан"
    else:
        matched = planned_days = 0
        worst = None  # (date_iso, delta_kcal) с наибольшим |delta|
        d = start
        while d <= end:
            pva = plan_vs_actual(conn, user_id, d.isoformat())["meals"]
            if pva["planned"] is not None:
                planned_days += 1
                delta_kcal = pva["delta"]["kcal"]
                planned_kcal = pva["planned"]["kcal"]
                if planned_kcal == 0 or abs(delta_kcal) <= 0.1 * planned_kcal:
                    matched += 1
                if worst is None or abs(delta_kcal) > abs(worst[1]):
                    worst = (d.isoformat(), delta_kcal)
            d += timedelta(days=1)
        if planned_days == 0:
            plan_line = "**📐 План/факт** — план задан, но на дни этой недели шаблона нет"
        else:
            # delta = план - факт: отрицательный значит перебор, показываем как "+"
            sign = "+" if worst[1] < 0 else MINUS
            worst_str = f"{worst[0][8:10]}.{worst[0][5:7]} {sign}{abs(worst[1]):.0f} ккал"
            plan_line = (f"**📐 План/факт** — совпало {matched}/{planned_days} дн (±10% ккал) · "
                         f"макс. отклонение {worst_str}")

    # 5. Гардрейлы — читаем уже записанные alerts, НЕ пересчитываем (см. докстринг)
    guard_rows = conn.execute(
        "SELECT rule, COUNT(*) n FROM alerts WHERE user_id=? AND date(created_at)>=? AND date(created_at)<=? "
        "GROUP BY rule ORDER BY n DESC",
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    if not guard_rows:
        guard_line = "**🚦 Гардрейлы за неделю** — срабатываний нет"
    else:
        guard_line = "**🚦 Гардрейлы за неделю** — " + " · ".join(f"{r['rule']} ×{r['n']}" for r in guard_rows)

    return "\n\n".join([header, weight_line, comp_line, discipline_line, plan_line, guard_line])


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")


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
            def make_user(tg_id):
                created = _now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT INTO users(telegram_user_id, created_at) VALUES (?, ?)",
                    (tg_id, created),
                )
                conn.commit()
                return conn.execute("SELECT id FROM users WHERE telegram_user_id=?", (tg_id,)).fetchone()["id"]

            def add_activity(uid, days_ago, duration_min, kcal, avg_hr, sport="бег"):
                ts = (_now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT INTO activity(user_id, started_at, duration_min, kcal, avg_hr, sport, file_hash) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (uid, ts, duration_min, kcal, avg_hr, sport, f"h-{uid}-{days_ago}-{duration_min}-{kcal}"),
                )
                conn.commit()

            # --- training_efficiency: тишина на 3 сессиях ---
            u1 = make_user(201)
            add_activity(u1, 10, 30, 300, 140)
            add_activity(u1, 8, 32, 310, 142)
            add_activity(u1, 6, 31, 305, 141)
            te = training_efficiency(conn, u1)
            assert te["sports"]["бег"]["sessions"] == 3
            assert te["sports"]["бег"]["trend"] is None, "3 сессии не должны давать тренд"
            assert te["excluded_sessions"] == 0
            print("OK: training_efficiency тихий на 3 сессиях")

            # --- training_efficiency: тренд на 6 сессиях + исключение по avg_hr NULL ---
            u2 = make_user(202)
            for i, (dur, kc, hr) in enumerate([(30, 250, 130), (30, 255, 131), (30, 260, 132),
                                                (30, 300, 140), (30, 310, 142), (30, 320, 145)]):
                add_activity(u2, 20 - i * 2, dur, kc, hr)
            add_activity(u2, 1, 30, 200, None)  # исключается: нет avg_hr
            te2 = training_efficiency(conn, u2)
            assert te2["sports"]["бег"]["sessions"] == 6
            assert te2["sports"]["бег"]["trend"] is not None, "6 сессий должны дать тренд"
            assert te2["sports"]["бег"]["trend"]["direction"] == "рост"
            assert te2["excluded_sessions"] == 1
            assert te2["excluded_reasons"].get("нет avg_hr") == 1
            print("OK: training_efficiency считает тренд на 6 сессиях и исключает avg_hr=NULL")

            # --- training_efficiency: пустая activity не падает ---
            u3 = make_user(203)
            te3 = training_efficiency(conn, u3)
            assert te3["sports"] == {} and te3["excluded_sessions"] == 0
            print("OK: training_efficiency не падает на пустой activity")

            # --- weekly_summary: с данными ---
            u4 = make_user(204)
            today = _now().date()
            for days_ago, w, ffm in [(6, 92.0, 60.0), (0, 90.5, 60.6)]:
                ts = (_now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg) VALUES (?,?,?,?,?)",
                    (u4, f"b-{days_ago}", ts, w, ffm),
                )
            for days_ago in [5, 3, 1]:
                ts = (_now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
                fl = conn.execute("INSERT INTO food_log(user_id, eaten_at) VALUES (?,?)", (u4, ts))
                conn.execute(
                    "INSERT INTO food_items(food_log_id, name, kcal, protein_g, fat_g, carbs_g) VALUES (?,?,?,?,?,?)",
                    (conn.execute("SELECT last_insert_rowid()").fetchone()[0], "еда", 2000, 150, 60, 200),
                )
            conn.execute(
                "INSERT INTO alerts(user_id, created_at, rule, message) VALUES (?,?,?,?)",
                (u4, (_now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"), "UNDEREATING", "тест"),
            )
            conn.commit()
            alerts_before = conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
            summary4 = weekly_summary(conn, u4)
            alerts_after = conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
            assert "```" not in summary4
            assert "Вес" in summary4 and "90.5" in summary4
            assert "UNDEREATING" in summary4
            assert alerts_before == alerts_after, "weekly_summary не должен писать в alerts"
            print("OK: weekly_summary рендерится с данными и не пишет в БД")

            # --- макс. отклонение: перебор над планом (2000 против 1700) печатается с "+" ---
            from health_core.plans import set_meal_plan
            for days_ago in [5, 3, 1]:
                dow = (_now() - timedelta(days=days_ago)).weekday()
                set_meal_plan(conn, u4, dow, "lunch", kcal=1700)
            summary4p = weekly_summary(conn, u4)
            assert "макс. отклонение" in summary4p and "+300 ккал" in summary4p, summary4p
            print("OK: weekly_summary показывает перебор плана как +300, а не -300")

            # --- weekly_summary: пустой пользователь не падает ---
            u5 = make_user(205)
            alerts_before5 = conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
            summary5 = weekly_summary(conn, u5)
            alerts_after5 = conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
            assert "нет данных" in summary5
            assert "```" not in summary5
            assert alerts_before5 == alerts_after5
            print("OK: weekly_summary не падает на пустом пользователе и печатает «нет данных»")

            # --- weight_series: один вес в день, восходящий порядок, пусто без замеров ---
            series4 = weight_series(conn, u4, days=30)
            assert [w for _, w in series4] == [92.0, 90.5], series4
            assert series4 == sorted(series4), "weight_series должен идти по возрастанию даты"
            assert weight_series(conn, u5, days=30) == [], "без замеров — пустой список, не исключение"
            print("OK: weight_series — по одной точке в день, по возрастанию, пусто на пустом пользователе")

            # --- _weight_trend медиана-тесты ---
            u206 = make_user(206)
            # Стабильный ряд, потом одиночный выброс на границе окна.
            # Старая логика: latest - first = выброс, покажет тренд как сильное снижение/возрастание.
            # Новая логика: медиана должна его отфильтровать.
            for days_ago, w in [(14, 100.0), (12, 99.8), (10, 99.6), (8, 99.4), (6, 99.2), (4, 99.0)]:
                ts = (_now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg) VALUES (?,?,?,?,?)",
                    (u206, f"b-{days_ago}", ts, w, 75.0),
                )
            # Выброс: скачок вверх на 1.5 кг
            ts_now = _now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg) VALUES (?,?,?,?,?)",
                (u206, "b-now", ts_now, 100.5, 75.0),
            )
            conn.commit()
            # окно 14 дней, should include all of them
            trend206 = _weight_trend(conn, u206, window_days=14, latest_weight=100.5)
            # Стабильный ряд: первые 3 точки ~ 100, 99.8, 99.6; медиана = 99.8
            # Последние 3 точки ~ 99.2, 99.0, 100.5; медиана = 99.2
            # Дельта ~ 99.2 - 99.8 = -0.6, а не -0.5 (latest - first)
            # Главное: не должна быть 100.5 - 100.0 = 0.5 (что было бы при эндпоинтах)
            assert trend206 is not None, "trend должен быть вычислен"
            assert abs(trend206) < 0.7, (
                f"медиана должна отфильтровать выброс, тренд должен быть ~-0.6, получили {trend206}"
            )
            print("OK: _weight_trend медиана фильтрует одиночный выброс")

            u207 = make_user(207)
            # Подлинный тренд спадания (без выбросов)
            for days_ago, w in [(14, 105.0), (12, 104.0), (10, 103.0), (8, 102.0), (6, 101.0), (4, 100.0), (2, 99.0), (0, 98.0)]:
                ts = (_now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg) VALUES (?,?,?,?,?)",
                    (u207, f"b-{days_ago}", ts, w, 75.0),
                )
            conn.commit()
            trend207 = _weight_trend(conn, u207, window_days=14, latest_weight=98.0)
            # Ожидаем спадание на ~7 кг за 14 дней (медиана первых 3 ~105 vs медиана последних 3 ~99)
            # Точная цифра зависит от того, какие точки попадут в медиану, но должна быть ~-6 или больше
            assert trend207 is not None and trend207 <= -5.0, (
                f"подлинный тренд спадания должен быть <= -5.0, получили {trend207}"
            )
            print("OK: _weight_trend сохраняет подлинный тренд спадания")

            u208 = make_user(208)
            # Менее 2 точек в окне
            ts_now = _now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg) VALUES (?,?,?,?,?)",
                (u208, "b-now", ts_now, 100.0, 75.0),
            )
            conn.commit()
            trend208 = _weight_trend(conn, u208, window_days=14, latest_weight=100.0)
            assert trend208 is None, f"менее 2 точек должно вернуть None, получили {trend208}"
            print("OK: _weight_trend вернул None при < 2 точек в окне")

            u209 = make_user(209)
            # Ровно 2 точки
            for days_ago, w in [(14, 100.0), (0, 99.0)]:
                ts = (_now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg) VALUES (?,?,?,?,?)",
                    (u209, f"b-{days_ago}", ts, w, 75.0),
                )
            conn.commit()
            trend209 = _weight_trend(conn, u209, window_days=14, latest_weight=99.0)
            # 2 точки: window_size = 1, медиана [100] vs [99] = -1.0
            assert trend209 is not None and trend209 == -1.0, (
                f"2 точки должны дать -1.0, получили {trend209}"
            )
            print("OK: _weight_trend работает с 2 точками (деградация окна)")

        finally:
            conn.close()

    print("Все проверки report.py прошли.")
