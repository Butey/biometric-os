"""Калораж — §09. Формулы, коэффициенты и порядок клампа воспроизводятся дословно.

Порядок применения (§09) — пять шагов:
  1  TDEE_факт ← расход дня, взвешенное среднее оценок по весу/еде, шагам и
     часам (см. daily_expenditure, docs/adr/0004-расход-дня.md)
  2  срок до вехи → требуемый дефицит
  3  clamp: цель ≥ безопасного пола (kcal_floor — от жировой массы, не BMR)
  4  распределение по дням недели, недельная сумма неизменна
  5  рефид активен? → цель := полный TDEE, шаги 2-4 на паузе

Все пять шагов реализованы. Инвариант безопасности сильнее буквального порядка
шагов: §09 говорит "шаг 3 стоит выше шага 2 намеренно" — но клампа держим не
только после шага 2, а и после шага 4 (недельный множитель мог бы снова
столкнуть число под пол) и на рефиде (шаг 5) тоже. daily_target() никогда не
возвращает kcal < kcal_floor ни при каком входе.

Пол считается не от BMR, а от того, сколько энергии способна отдать жировая
ткань (предел Alpert на кг жировой массы, допустимая доля предела убывает по
шкале процента жира) — см. kcal_floor. BMR не смотрит на жировую массу вовсе и
потому одинаково неверен в обе стороны: переосторожен при большом запасе жира
и слишком мягок у сухого человека. Нижняя граница снизу подпёрта
макро-минимумом: в цель обязана помещаться собственная норма белка.
"""
import sqlite3
import statistics
from datetime import date, datetime, timedelta

from health_core.config import latest_ffm, load, targets_for, local_now, user_now

_KCAL_PER_KG = 7700  # тот же коэффициент, что и в "Адаптивном TDEE" §09

def bmr_katch(ffm_kg: float) -> float:
    return 370 + 21.6 * ffm_kg


def bmr_mifflin(weight_kg: float, height_cm: float, age_years: int, sex: str = "m") -> float:
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age_years
    return base + (5 if sex == "m" else -161)


def _age_years(birth_date: str, on_date: str) -> int:
    """Наивная разница лет (год_замера - год_рождения), без учёта месяца/дня.

    Подтверждено перекрёстной сверкой с таблицей §09: 2026-1992=34 воспроизводит
    Mifflin 2294 (08.07) и 2209 (20.08) для одного и того же человека на обеих
    датах, тогда как точный возраст на дату замера (33/34) этого не делает.
    """
    return date.fromisoformat(on_date[:10]).year - date.fromisoformat(birth_date[:10]).year


def _latest_metric(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT weight_kg, ffm_kg, measured_at FROM body_metrics WHERE user_id=? "
        "ORDER BY measured_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()


def bmr_floor(conn: sqlite3.Connection, user_id: int) -> float:
    """BMR_floor = max(Mifflin, Katch) — затравка §09, берётся по максимуму,
    потому что Katch молча считает жир метаболически инертным и занижает
    расход у крупной жировой массы; ошибка вверх стоит темпа, ошибка вниз — мышц."""
    metric = _latest_metric(conn, user_id)
    if metric is None:
        raise ValueError(f"no body_metrics for user {user_id}")
    user = conn.execute(
        "SELECT height_cm, birth_date, sex FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if user is None or user["height_cm"] is None or user["birth_date"] is None:
        raise ValueError(f"incomplete profile for user {user_id}")

    # FFM не снят этими весами -> последний известный замер (см. config.latest_ffm):
    # вес тела вместо FFM завышает Katch и роняет/поднимает цель на сотни ккал.
    ffm = metric["ffm_kg"] or latest_ffm(conn, user_id) or metric["weight_kg"]
    age = _age_years(user["birth_date"], metric["measured_at"])
    katch = bmr_katch(ffm)
    mifflin = bmr_mifflin(metric["weight_kg"], user["height_cm"], age, user["sex"] or "m")
    return max(katch, mifflin)


def fat_mass_kg(conn: sqlite3.Connection, user_id: int) -> float | None:
    """Жировая масса — медиана (вес × %жира) по замерам с процентом жира за
    policy.fat_mass_window_days от последнего такого замера: шум биоимпеданса
    не должен двигать цель день ко дню. Нет биоимпеданса — None: считать жир
    от веса «на глаз» здесь нельзя, от этого числа зависит нижняя граница
    калоража."""
    window_days = load()["policy"].get("fat_mass_window_days", 7)
    last = conn.execute(
        "SELECT measured_at FROM body_metrics WHERE user_id=? AND fat_pct IS NOT NULL "
        "AND fat_pct > 0 ORDER BY measured_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if last is None:
        return None
    end = date.fromisoformat(last["measured_at"][:10])
    start = end.fromordinal(end.toordinal() - window_days + 1)
    rows = conn.execute(
        "SELECT weight_kg, fat_pct FROM body_metrics WHERE user_id=? AND fat_pct IS NOT NULL "
        "AND fat_pct > 0 AND date(measured_at) BETWEEN ? AND ?",
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    vals = [r["weight_kg"] * r["fat_pct"] / 100 for r in rows if r["weight_kg"]]
    return statistics.median(vals) if vals else None


def lean_share(conn: sqlite3.Connection, user_id: int) -> float | None:
    """Доказанная доля тощей массы в потере веса за policy.lean_share_window_days:
    медианы веса и FFM по 5 первым и 5 последним замерам окна. Это
    доказательство, а не прогноз — без достаточных, свежих данных о реальной
    потере веса возвращает None (см. fat_share)."""
    policy = load()["policy"]
    window_days = policy["lean_share_window_days"]
    min_points = policy["lean_share_min_points"]
    min_loss = policy["lean_share_min_loss_kg"]
    no_measure_days = load()["guards"]["no_measure_days"]

    since = (user_now(conn, user_id) - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT measured_at, weight_kg, ffm_kg FROM body_metrics WHERE user_id=? "
        "AND ffm_kg IS NOT NULL AND measured_at >= ? ORDER BY measured_at ASC",
        (user_id, since),
    ).fetchall()
    if len(rows) < min_points:
        return None
    gap = (user_now(conn, user_id).date() - date.fromisoformat(rows[-1]["measured_at"][:10])).days
    if gap > no_measure_days:
        return None

    k = min(5, len(rows))
    first, last = rows[:k], rows[-k:]
    d_weight = statistics.median(r["weight_kg"] for r in last) - statistics.median(r["weight_kg"] for r in first)
    if -d_weight < min_loss:
        return None  # потери меньше min_loss (в т.ч. набор) — доказательства нет
    d_lean = statistics.median(r["ffm_kg"] for r in last) - statistics.median(r["ffm_kg"] for r in first)
    return max(0.0, -d_lean) / -d_weight  # набор тощей массы на фоне потери — не потеря мышц


def fat_share(conn: sqlite3.Connection, user_id: int, fat_pct: float) -> float:
    """Допустимая доля предела Alpert — линейная интерполяция по шкале
    процента жира от fat_share_lean (≤ нижней границы) до fat_share_fat
    (≥ верхней). Шкала своя для пола, неизвестный пол — мужская (осторожнее).
    На жирном конце цель интерполяции — fat_share_fat_proven вместо
    fat_share_fat, если lean_share доказывает малую долю мышц в потере."""
    policy = load()["policy"]
    urow = conn.execute("SELECT sex FROM users WHERE id=?", (user_id,)).fetchone()
    sex = urow["sex"] if urow else None
    lo, hi = policy["fat_share_bounds_f"] if sex == "f" else policy["fat_share_bounds_m"]

    fat_end = policy["fat_share_fat"]
    proven = lean_share(conn, user_id)
    if proven is not None and proven < load()["guards"]["lbm_ratio_threshold"]:
        fat_end = policy["fat_share_fat_proven"]

    lean_val = policy["fat_share_lean"]
    if fat_pct <= lo:
        return lean_val
    if fat_pct >= hi:
        return fat_end
    t = (fat_pct - lo) / (hi - lo)
    return lean_val + t * (fat_end - lean_val)


def _smoothed_weight_kg(conn: sqlite3.Connection, user_id: int) -> float | None:
    """Последний сглаженный вес — медиана за policy.weight_trend_window_days
    дней от последнего замера. Опора потолка дефицита (см. kcal_floor): без
    сглаживания один шумный день на весах двигал бы потолок сам по себе."""
    window_days = load()["policy"].get("weight_trend_window_days", 7)
    last = conn.execute(
        "SELECT measured_at FROM body_metrics WHERE user_id=? AND weight_kg IS NOT NULL "
        "ORDER BY measured_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if last is None:
        return None
    end = date.fromisoformat(last["measured_at"][:10])
    start = end.fromordinal(end.toordinal() - window_days + 1)
    rows = conn.execute(
        "SELECT weight_kg FROM body_metrics WHERE user_id=? AND weight_kg IS NOT NULL "
        "AND date(measured_at) BETWEEN ? AND ?",
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    vals = [r["weight_kg"] for r in rows]
    return statistics.median(vals) if vals else None


def _deficit_cap_kcal(conn: sqlite3.Connection, user_id: int) -> float | None:
    """Потолок дефицита: темп min(weight_rate_kg_week_max кг,
    weight_rate_pct_week_max % веса) в неделю, переведённый в ккал/сут через
    _KCAL_PER_KG — тот же порог, по которому RATE_HIGH предупреждает о риске
    для желчного пузыря."""
    weight = _smoothed_weight_kg(conn, user_id)
    if weight is None:
        return None
    guards_cfg = load()["guards"]
    cap_kg_week = min(guards_cfg["weight_rate_kg_week_max"], weight * guards_cfg["weight_rate_pct_week_max"] / 100)
    return cap_kg_week * _KCAL_PER_KG / 7


def macro_minimum_kcal(conn: sqlite3.Connection, user_id: int) -> float | None:
    """Сколько калорий физически занимают обязательные белок и жир из целей.
    Ниже этого числа цель бессмысленна: её нельзя набрать, не нарушив саму же
    норму белка (мышцы) или минимум жиров (гормоны, усвоение витаминов)."""
    t = targets_for(conn, user_id)
    if not t.get("protein_g"):
        return None
    # Жир — доля бюджета, поэтому минимум решается уравнением, а не суммой:
    # белок*4 <= kcal * (1 - доля_жира_min). Ниже этого kcal белок и нижняя
    # граница AMDR по жирам не помещаются вместе даже при нуле углеводов.
    pct_min = t.get("fat_pct_min", 0.20)
    return t["protein_g"] * 4 / (1 - pct_min)


def kcal_floor(conn: sqlite3.Connection, user_id: int, full_tdee: float) -> tuple[float, str]:
    """Нижняя граница калоража. Возвращает (ккал, причина).

    Пол на уровне BMR — грубый предохранитель: он вообще не смотрит на жировую
    массу, хотя именно она определяет безопасный дефицит. У человека со 41 кг
    жира он переосторожен, у сухого — наоборот, слишком мягок.

    Физиологичнее считать от потолка отдачи жировой ткани: жир способен отдать
    fat_supply_kcal_per_kg ккал в сутки с килограмма (Alpert), но в дефицит
    разрешена не вся эта величина, а fat_share — доля, убывающая по шкале
    процента жира (см. fat_share). Сверху безопасный дефицит дополнительно
    ограничен потолком темпа снижения веса (см. _deficit_cap_kcal). Дефицит
    сверх меньшего из двух пределов организм добирает из тощей массы — это и
    есть та опасность, ради которой пол существует.

        безопасный дефицит = min(жировая масса × fat_supply_kcal_per_kg × доля,
                                  потолок темпа)
        пол = расход − безопасный дефицит

    Пол не опускается ниже макро-минимума (белок + жиры из целей): цель, в
    которую не помещается собственная норма белка, гарантирует потерю мышц.

    Жировая масса неизвестна (нет биоимпеданса) — возвращаемся к BMR: без
    данных о жире оценивать его отдачу нечем, и осторожный вариант честнее.
    """
    policy = load()["policy"]
    bmr = bmr_floor(conn, user_id)
    fat_kg = fat_mass_kg(conn, user_id)
    weight = _smoothed_weight_kg(conn, user_id)
    if fat_kg is None or not weight:
        return bmr, "bmr"

    per_kg = policy.get("fat_supply_kcal_per_kg")
    if not per_kg:
        return bmr, "bmr"        # модель выключена настройкой — прежнее поведение

    # процент жира из тех же сглаженных величин, что и жировая масса: шум одного замера не двигает долю
    share = fat_share(conn, user_id, fat_kg / weight * 100)
    fat_supply_deficit = fat_kg * per_kg * share
    cap = _deficit_cap_kcal(conn, user_id)
    if cap is not None and cap < fat_supply_deficit:
        safe_deficit, reason = cap, "deficit_cap"
    else:
        safe_deficit, reason = fat_supply_deficit, "fat_supply"

    floor = full_tdee - safe_deficit

    macro_min = macro_minimum_kcal(conn, user_id)
    if macro_min is not None and floor < macro_min:
        return macro_min, "macro_minimum"
    return floor, reason


def ffmi(ffm_kg: float, height_cm: float) -> float:
    height_m = height_cm / 100
    return ffm_kg / (height_m ** 2)


def _morning_weights(conn: sqlite3.Connection, user_id: int, start: str, end: str) -> list[float]:
    # §09: "в расчёт тренда идут только замеры с 06:00 до 11:00" — вечерние
    # (вода к вечеру) сохраняются для гидратации, но цель не двигают.
    rows = conn.execute(
        "SELECT weight_kg FROM body_metrics WHERE user_id=? AND date(measured_at) BETWEEN ? AND ? "
        "AND time(measured_at) BETWEEN '06:00:00' AND '11:00:00' ORDER BY measured_at",
        (user_id, start, end),
    ).fetchall()
    return [r["weight_kg"] for r in rows]


def _adaptive_tdee_core(
    conn: sqlite3.Connection, user_id: int, window_days: int, for_date: str | None, min_days: int
) -> tuple[float | None, int]:
    """Общее тело adaptive_tdee() и адаптивной части daily_expenditure() (ADR
    0004) — отличаются только порогом полноты лога (min_days: 11/14 у
    adaptive_tdee, policy.blend_min_logged_days у блендера). Возвращает (TDEE
    или None, число залогированных дней в окне) — logged_days нужен блендеру
    для доли адаптивной части независимо от того, прошла ли она сама порог.

    TDEE_факт = средний_intake_Nд + (Δвес_Nд · 7700 / N).

    Окно по умолчанию заканчивается сегодня (для forecast.py/report.py, которым
    нужен именно текущий TDEE). daily_target() пересчитывает цель задним числом
    и обязан передавать свою же дату через for_date, иначе повторный вызов на
    одну и ту же историческую дату даёт разный результат в зависимости от того,
    когда он был выполнен — окно "плывёт" вместе с user_now(conn, user_id)."""
    end = datetime.fromisoformat(for_date[:10]).date() if for_date else user_now(conn, user_id).date()
    start = end.fromordinal(end.toordinal() - window_days + 1)
    start_s, end_s = start.isoformat(), end.isoformat()

    logged_days = conn.execute(
        "SELECT COUNT(DISTINCT date(eaten_at)) c FROM food_log WHERE user_id=? "
        "AND date(eaten_at) BETWEEN ? AND ?",
        (user_id, start_s, end_s),
    ).fetchone()["c"]
    if logged_days < min_days:
        return None, logged_days

    daily_kcals = conn.execute(
        "SELECT SUM(fi.kcal) kcal FROM food_log fl JOIN food_items fi ON fi.food_log_id = fl.id "
        "WHERE fl.user_id=? AND date(fl.eaten_at) BETWEEN ? AND ? GROUP BY date(fl.eaten_at)",
        (user_id, start_s, end_s),
    ).fetchall()
    kcal_values = [r["kcal"] for r in daily_kcals if r["kcal"] is not None]
    if not kcal_values:
        return None, logged_days
    mean_intake = sum(kcal_values) / len(kcal_values)

    weights = _morning_weights(conn, user_id, start_s, end_s)
    if len(weights) < 2:
        return None, logged_days
    delta_weight = weights[-1] - weights[0]

    tdee = mean_intake - (delta_weight * 7700 / window_days)
    # Расход ниже базового обмена физиологически невозможен — так выходит, когда
    # в окно попали недописанные дни (220 ккал за день в логе). Такая калибровка
    # хуже, чем никакой: от неё считается пол, и цель проваливалась до ~1090 ккал.
    if tdee < bmr_floor(conn, user_id):
        return None, logged_days
    return tdee, logged_days


def adaptive_tdee(
    conn: sqlite3.Connection, user_id: int, window_days: int = 14, for_date: str | None = None
) -> float | None:
    """TDEE_факт = средний_intake_Nд + (Δвес_Nд · 7700 / N).

    Достоверность требует еду залогированной минимум 11 из 14 дней (порог
    масштабируется пропорционально для нестандартного окна). Иначе — None,
    калибровка замораживается, а не подгоняется. См. _adaptive_tdee_core —
    тот же расчёт с более низким порогом идёт в daily_expenditure() (ADR 0004)."""
    min_days = -(-window_days * 11 // 14)  # ceil, воспроизводит порог 11/14 по умолчанию
    tdee, _logged_days = _adaptive_tdee_core(conn, user_id, window_days, for_date, min_days)
    return tdee


def _tcx_net(conn: sqlite3.Connection, user_id: int, date_: str) -> float:
    """Тренировочная добавка: Σ(калории лапа) × 0.75, и ещё × 0.9 без пульса."""
    policy = load()["policy"]
    discount, no_hr = policy["tcx_discount_factor"], policy["no_hr_discount"]
    rows = conn.execute(
        "SELECT kcal, avg_hr FROM activity WHERE user_id=? AND date(started_at)=?",
        (user_id, date_),
    ).fetchall()
    total = 0.0
    for r in rows:
        if r["kcal"] is None:
            continue
        mult = discount * (no_hr if r["avg_hr"] is None else 1.0)
        total += r["kcal"] * mult
    return total


def _target_kcal(base: float, tcx_net: float, floor: float) -> float:
    """Гардрейл шага 3: цель никогда не опускается ниже BMR_floor."""
    return max(base + tcx_net, floor)


def _active_milestone(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """§09 шаг 2: "ближайшая недостигнутая веха, у которой проставлен срок".

    Детерминированный выбор: из вех без achieved_at и с непустым deadline берём
    самый ранний deadline — это и есть "ближайшая" веха по сроку. Равные
    дедлайны бьём по возрастанию id (порядок вставки), чтобы выбор был
    воспроизводим при повторном вызове с теми же данными."""
    return conn.execute(
        "SELECT id, name, metric, threshold, deadline FROM milestones "
        "WHERE user_id=? AND achieved_at IS NULL AND deadline IS NOT NULL "
        "ORDER BY deadline ASC, id ASC LIMIT 1",
        (user_id,),
    ).fetchone()


def _deadline_deficit(conn: sqlite3.Connection, user_id: int, milestone: sqlite3.Row, today: str) -> float | None:
    """§09 шаг 2: срок до вехи -> требуемый дневной дефицит в ккал.

    ponytail: §09 не выписывает формулу дефицита явно. Переиспользуем
    коэффициент 7700 ккал/кг из "Адаптивного TDEE" того же раздела, чтобы
    перевести недостающие килограммы в ккал и разделить на дни до срока.
    Считаем дефицит только для metric='weight_kg' — для талии/висцерального
    жира/FFMI в §09 нет формулы перевода в ккал, и придумывать её здесь не
    будем; такая веха срока дефицита не диктует (как если бы у неё не было
    deadline). Возвращает None, если дефицит неприменим, float('inf'), если
    срок уже наступил/прошёл (дефицит физически недостижим за 0 дней).

    Отправная точка — сглаженный вес (_smoothed_weight_kg), а не последнее
    единичное взвешивание: один шумный день на весах не обязан двигать
    требуемый дефицит так же, как он не двигает потолок дефицита (см.
    _deficit_cap_kcal)."""
    if milestone["metric"] != "weight_kg":
        return None
    weight = _smoothed_weight_kg(conn, user_id)
    if weight is None:
        return None
    required_kg = weight - milestone["threshold"]
    if required_kg <= 0:
        return 0.0  # порог уже физически достигнут, achieved_at просто не проставлен
    days_remaining = (
        date.fromisoformat(milestone["deadline"][:10]) - date.fromisoformat(today[:10])
    ).days
    if days_remaining <= 0:
        return float("inf")
    return required_kg * _KCAL_PER_KG / days_remaining


def _is_refeed(conn: sqlite3.Connection, user_id: int, date_: str) -> bool:
    """§09 шаг 5: рефид активен на эту дату? Источник — таблица refeed_days,
    которую пользователь/бот заполняет отдельно (вне контракта этого модуля)."""
    return conn.execute(
        "SELECT 1 FROM refeed_days WHERE user_id=? AND date=?", (user_id, date_)
    ).fetchone() is not None


def _is_sick(conn: sqlite3.Connection, user_id: int, date_: str) -> bool:
    """Режим болезни (health_core/sick.py) на эту дату? Для калоража день
    болезни ведёт себя ровно как рефид — полный TDEE, шаги 2-4 на паузе:
    во время острой болезни организму не до дефицита, а есть заставлять через
    силу вредно. Отдельный тег "+sick" в source вместо переиспользования
    "+refeed" — чтобы explain честно называл причину паузы дефицита."""
    return conn.execute(
        "SELECT 1 FROM sick_days WHERE user_id=? AND date=?", (user_id, date_)
    ).fetchone() is not None


def _weekday_multiplier(date_: str) -> float:
    """§09 шаг 4: "распределение по дням недели, недельная сумма неизменна".

    ponytail: §09 не задаёт саму схему распределения (какие дни тяжелее,
    какие легче) — придумывать её не будем. Консервативное чтение: без
    настройки распределение равномерное, то есть шаг 4 не меняет число.
    Хук для реального распределения — policy.weekday_multipliers в
    config.yaml (7 чисел, Пн..Вс); множитель нормируется на среднее по семи,
    так что недельная сумма остаётся кратна цели до шага 4, как требует §09."""
    mults = load()["policy"].get("weekday_multipliers")
    if not mults or len(mults) != 7:
        return 1.0
    avg = sum(mults) / 7
    if avg <= 0:
        return 1.0
    weekday = date.fromisoformat(date_[:10]).weekday()  # Monday=0 ... Sunday=6
    return mults[weekday] / avg


def deadline_verdict(conn: sqlite3.Connection, user_id: int, date_: str | None = None) -> dict | None:
    """Честный вердикт по сроку активной вехи (CONTEXT.md «Недостижимый срок»).

    None — активной вехи со сроком нет (см. _active_milestone). Иначе:
      milestone/deadline    — имя и срок вехи.
      plan_below_floor      — план питания требует дефицита глубже пола
                               калоража (kcal_floor): то же условие, что
                               daily_target() кладёт в тег "deadline_unreachable"
                               внутри computed_from, — только план, не факт.
      forecast_reach         — health_core.forecast.reach() по фактически
                               залогированному питанию к порогу вехи, если
                               данных хватило, иначе None.
      unreachable             — True, только когда ОБА признака недостижимости
                               выполнены разом: план ниже пола И прогноз по
                               факту не доходит до цели к сроку (не уложился в
                               горизонт, или уложился позже дедлайна). False —
                               план ниже пола, но факт всё же успевает (или
                               план в пол укладывается вовсе — тогда вопрос о
                               недостижимости не встаёт). None — прогноза нет
                               (еды залогировано меньше 11 дней из 14 и т.п.):
                               подтвердить или опровергнуть срок нечем.
      forecast_note           — причина отсутствия прогноза, только когда
                               unreachable is None.
    """
    if date_ is None:
        date_ = user_now(conn, user_id).date().isoformat()
    milestone = _active_milestone(conn, user_id)
    if milestone is None:
        return None

    # Те же примитивы (шаг 1 и пол §09), что daily_target() уже считает для
    # этой же даты — деривация одна и та же, вызвана заново для вехи-вердикта.
    milestone_deficit = _deadline_deficit(conn, user_id, milestone, date_)
    full_tdee = daily_expenditure(conn, user_id, date_)["kcal"]
    floor, _ = kcal_floor(conn, user_id, full_tdee)
    # milestone_deficit истинный: не None, не 0.0 (порог уже достигнут), не
    # inf-нейтральный по умолчанию — тот же гейт, что в daily_target().
    plan_below_floor = bool(milestone_deficit) and (full_tdee - milestone_deficit) < floor

    verdict = {
        "milestone": milestone["name"],
        "deadline": milestone["deadline"],
        "plan_below_floor": plan_below_floor,
    }
    if not plan_below_floor:
        verdict["forecast_reach"] = None
        verdict["unreachable"] = False
        return verdict

    from health_core import forecast  # локальный импорт: forecast.py сам импортирует energy.py
    fr = forecast.reach(conn, user_id, milestone["threshold"])
    if "error" in fr:
        verdict["forecast_reach"] = None
        verdict["unreachable"] = None
        verdict["forecast_note"] = fr["error"]
        return verdict

    verdict["forecast_reach"] = fr
    if fr.get("reached"):
        verdict["unreachable"] = (
            date.fromisoformat(fr["earliest"][:10]) > date.fromisoformat(milestone["deadline"][:10])
        )
    else:
        verdict["unreachable"] = True  # цель за горизонт модели тоже не встречает срок
    return verdict


def daily_expenditure(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    """Расход дня (ADR 0004, docs/adr/0004-расход-дня.md) — взвешенное среднее
    трёх оценок расхода вместо одной адаптивной-или-затравочной базы.

    parts: {"adaptive"|"steps"|"watch"|"activity_factor": {"estimate", "share"}
    или None, если часть недоступна}. kcal = Σ estimate × share. logged_days —
    дни с логом еды в 14-дневном окне (та же величина двигает долю адаптивной
    части). tags — имена частей с долей > 0, в порядке
    adaptive/steps/watch/activity_factor: источник тега "blend:..." в
    daily_target().

    Доля адаптивной части a = policy.blend_adaptive_max_share × logged_days/14,
    и только если сама часть доступна (иначе a=0). Остаток 1-a делят поровну
    доступные {steps, watch}; если недоступна ни одна — весь остаток уходит
    activity_factor (BMR × коэффициент активности + тренировочная добавка, как
    в daily_target() до ADR 0004). Тренировочная добавка (_tcx_net) входит в
    adaptive/steps/activity_factor как есть — watch её не берёт: часы уже
    считают калории тренировки в active_kcal, добавлять её ещё раз было бы
    двойным счётом."""
    policy = load()["policy"]
    bmr = bmr_floor(conn, user_id)
    tcx_net = _tcx_net(conn, user_id, date)

    min_days = policy.get("blend_min_logged_days", 4)
    adaptive_est, logged_days = _adaptive_tdee_core(conn, user_id, 14, date, min_days)

    parts: dict[str, dict | None] = {"adaptive": None, "steps": None, "watch": None, "activity_factor": None}
    a = 0.0
    if adaptive_est is not None:
        a = policy.get("blend_adaptive_max_share", 0.6) * logged_days / 14
        parts["adaptive"] = {"estimate": adaptive_est + tcx_net, "share": a}

    window_days = policy.get("blend_watch_window_days", 7)
    end = datetime.fromisoformat(date[:10]).date()
    start = end.fromordinal(end.toordinal() - window_days + 1)
    start_s, end_s = start.isoformat(), end.isoformat()

    steps_vals = [
        r["steps"] for r in conn.execute(
            "SELECT steps FROM daily_watch WHERE user_id=? AND date BETWEEN ? AND ? AND steps IS NOT NULL",
            (user_id, start_s, end_s),
        ).fetchall()
    ]
    active_vals = [
        r["active_kcal"] for r in conn.execute(
            "SELECT active_kcal FROM daily_watch WHERE user_id=? AND date BETWEEN ? AND ? AND active_kcal IS NOT NULL",
            (user_id, start_s, end_s),
        ).fetchall()
    ]

    weight_kg = _smoothed_weight_kg(conn, user_id)
    steps_est = None
    if steps_vals and weight_kg is not None:
        mean_steps = sum(steps_vals) / len(steps_vals)
        over_steps = max(0.0, mean_steps - policy.get("blend_steps_free", 3000))
        steps_est = (
            bmr * policy.get("blend_steps_base_factor", 1.2)
            + over_steps * policy.get("blend_kcal_per_step_kg", 0.0005) * weight_kg
            + tcx_net
        )

    watch_est = None
    if active_vals:
        mean_active = sum(active_vals) / len(active_vals)
        watch_est = bmr * policy.get("blend_watch_base_factor", 1.1) + mean_active * policy.get("blend_watch_discount", 0.8)

    remaining = 1.0 - a
    available = [n for n, est in (("steps", steps_est), ("watch", watch_est)) if est is not None]
    if available:
        share_each = remaining / len(available)
        if steps_est is not None:
            parts["steps"] = {"estimate": steps_est, "share": share_each}
        if watch_est is not None:
            parts["watch"] = {"estimate": watch_est, "share": share_each}
    else:
        factor = policy.get("activity_factor") or 1.0
        parts["activity_factor"] = {"estimate": bmr * factor + tcx_net, "share": remaining}

    kcal = sum(p["estimate"] * p["share"] for p in parts.values() if p is not None)
    tags = [n for n in ("adaptive", "steps", "watch", "activity_factor") if parts[n] and parts[n]["share"] > 0]

    return {"kcal": kcal, "parts": parts, "logged_days": logged_days, "tags": tags}


def daily_target(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    bmr = bmr_floor(conn, user_id)
    expenditure = daily_expenditure(conn, user_id, date)
    full_tdee = expenditure["kcal"]  # шаг 1 (ADR 0004: расход дня — блендер), без дефицита и без клампа
    tcx_net = _tcx_net(conn, user_id, date)

    # Пол считается ПОСЛЕ расхода и от него: безопасный дефицит ограничен тем,
    # сколько энергии способен отдать жир, а не абстрактным BMR. См. kcal_floor.
    floor, floor_reason = kcal_floor(conn, user_id, full_tdee)

    seed_tag = "blend:" + "+".join(expenditure["tags"]) if expenditure["tags"] else "blend"
    tags = [seed_tag]
    if tcx_net:
        tags.append("tcx")

    deadline_unreachable = False
    active_milestone_name = None
    refeed = _is_refeed(conn, user_id, date)
    sick = _is_sick(conn, user_id, date)

    if refeed or sick:
        # шаг 5 (и режим болезни): цель := полный TDEE, шаги 2-4 на паузе.
        # Но безопасный пол (шаг 3) — не опция, а инвариант: держим его и тут.
        kcal = max(full_tdee, floor)
        # Если день одновременно рефид и болезнь — одного тега достаточно
        # (см. докстринг _is_sick): "sick" называет более конкретную причину.
        tags.append("sick" if sick else "refeed")
        if full_tdee < floor:
            tags.append("clamped")
    else:
        milestone = _active_milestone(conn, user_id)
        milestone_deficit = None
        if milestone is not None:
            active_milestone_name = milestone["name"]
            milestone_deficit = _deadline_deficit(conn, user_id, milestone, date)

        # §09: "если срока нет — режим не включается, и дефицит определяется
        # политикой по умолчанию" — источник дефицита без активной вехи-со-сроком.
        default_deficit = load()["policy"].get("default_deficit_kcal", 0) or 0
        deficit = milestone_deficit if milestone_deficit is not None else default_deficit

        step2_target = full_tdee - deficit
        if milestone_deficit:  # реальный дедлайн дал число (не None, не 0, не inf-нейтрально)
            if step2_target < floor:
                # §09: "если срок требует дефицита ниже пола, отклоняется срок" —
                # не режем калораж ниже безопасной границы, вместо этого помечаем
                # срок недостижимым и сообщаем об этом наружу через словарь.
                deadline_unreachable = True
                tags.append("deadline_unreachable")
            else:
                tags.append("deadline")
        elif deficit:
            tags.append("default_deficit")

        # шаг 3: цель ≥ безопасного пола — гардрейл выше срока (§09).
        step3_kcal = max(step2_target, floor)
        if step2_target < floor:
            tags.append("clamped")

        # шаг 4: распределение по дням недели, недельная сумма неизменна.
        mult = _weekday_multiplier(date)
        step4_kcal = step3_kcal * mult
        if mult != 1.0:
            tags.append("weekday")

        # ре-кламп: шаг 3 — гардрейл безопасности, а не разовая проверка, поэтому
        # держим его и после шага 4, даже если §09 текстуально ставит шаг 3 раньше.
        kcal = max(step4_kcal, floor)
        if step4_kcal < floor and "clamped" not in tags:
            tags.append("clamped")

    source = "+".join(tags)

    targets = targets_for(conn, user_id)
    protein_g = targets.get("protein_g")
    # Жир считается здесь, а не в targets_for: он доля уже утверждённого
    # калоража. См. config.yaml::fat_pct_of_kcal.
    # Если действующий пол — macro_minimum, он сам был выведен из fat_pct_min
    # (см. macro_minimum_kcal), а не из номинального fat_pct. Если здесь всё
    # равно взять номинальный fat_pct, белок+жир по факту превысят весь kcal
    # (при protein_g=126, kcal=630 получается 504+189=693>630) — цель станет
    # физически невыполнимой в трекере. Поэтому на macro_minimum считаем жир
    # той же долей, что и сам пол.
    effective_fat_pct = targets.get("fat_pct_min") if floor_reason == "macro_minimum" else targets.get("fat_pct")
    fat_g = round(kcal * effective_fat_pct / 9, 1) if effective_fat_pct else None
    # Углеводы — остаток калорий после белка (4 ккал/г) и жира (9 ккал/г). Клампим
    # снизу нулём: на очень низкой цели белок+жир могут перекрыть весь калораж.
    carbs_g = None
    if protein_g is not None and fat_g is not None:
        carbs_g = round(max(0.0, kcal - protein_g * 4 - fat_g * 9) / 4.0, 1)
    conn.execute(
        "INSERT INTO daily_targets(user_id, date, kcal_target, protein_g_target, fat_g_target, "
        "carbs_g_target, fiber_g_target, water_ml_target, computed_from) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, date) DO UPDATE SET "
        "kcal_target=excluded.kcal_target, protein_g_target=excluded.protein_g_target, "
        "fat_g_target=excluded.fat_g_target, carbs_g_target=excluded.carbs_g_target, "
        "fiber_g_target=excluded.fiber_g_target, "
        "water_ml_target=excluded.water_ml_target, computed_from=excluded.computed_from",
        (user_id, date, kcal, protein_g, fat_g, carbs_g, targets.get("fiber_g"),
         targets.get("water_ml"), source),
    )
    conn.commit()

    return {
        "kcal": kcal,
        "protein_g": protein_g,
        "fat_g": fat_g,
        "carbs_g": carbs_g,
        "source": source,
        "bmr_floor": bmr,          # сам BMR — его показывают в пульте и в explain
        "kcal_floor": floor,       # действующая нижняя граница (см. kcal_floor)
        "floor_reason": floor_reason,
        "tcx_net": tcx_net,
        "active_milestone": active_milestone_name,
        "deadline_unreachable": deadline_unreachable,
        "deadline_verdict": deadline_verdict(conn, user_id, date),
        "refeed": refeed,
        "sick": sick,
    }


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

        # Фикстуры засевают замеры по local_now(), рабочий код считает окна
        # по поясу пользователя (user_now) — без общего пояса даты разъезжаются
        # на часы и окна ловят не те точки.
        from health_core.config import set_tz
        set_tz((load().get('schedule') or {}).get('default_timezone'))

        conn.execute(
            "INSERT INTO users(telegram_user_id, height_cm, birth_date, sex, created_at) "
            "VALUES (1, 185, '1992-08-09', 'm', '2026-08-20 00:00:00')"
        )
        uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]
        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg, device_bmr_kcal) "
            "VALUES (?, 'b1', '2026-08-20 07:00:00', 121.8, 79.5, 2085)",
            (uid,),
        )
        conn.commit()

        katch = bmr_katch(79.5)
        print(f"bmr_katch(79.5) = {katch:.2f}  (device 2085)")
        # §09 сам заявляет точность "до двух" для августовского замера (в июле —
        # "до килокалины"), не "до одного" — используем заявленную спекой точность.
        assert abs(katch - 2085) <= 2.5, f"katch {katch} strays too far from device 2085"

        ffmi_val = ffmi(79.5, 185)
        print(f"ffmi(79.5, 185) = {ffmi_val:.4f}")
        assert abs(ffmi_val - 23.2286) < 0.001

        floor = bmr_floor(conn, uid)
        print(f"bmr_floor = {floor:.2f}")
        assert abs(floor - 2209.25) < 0.01  # Mifflin выигрывает по максимуму на этих данных

        for tcx in (0, -5000, 5000, -1_000_000):
            kcal = _target_kcal(base=floor, tcx_net=tcx, floor=floor)
            assert kcal >= floor, f"target {kcal} fell below floor {floor} for tcx={tcx}"
        print("daily_target clamp holds for tcx in {0, -5000, 5000, -1000000}")

        result = daily_target(conn, uid, "2026-08-20")
        print(f"daily_target = {result}")
        assert result["kcal"] >= result["bmr_floor"]

        # --- шаг 2: достижимый срок даёт дефицит строго между полом и базой ---
        # Без адаптивного TDEE база == пол (нет запаса, дефицит любого размера
        # уже недостижим) — добавляем тренировку, чтобы появился зазор над полом,
        # в котором дефицит физически умещается.
        conn.execute(
            "INSERT INTO activity(user_id, started_at, kcal, avg_hr, sport, file_hash) "
            "VALUES (?, '2026-08-21 08:00:00', 1000, 140, 'run', 'selfcheck-activity-1')",
            (uid,),
        )
        conn.execute(
            "INSERT INTO milestones(user_id, name, metric, threshold, deadline) "
            "VALUES (?, 'reachable', 'weight_kg', 118, '2026-12-01')",
            (uid,),
        )
        conn.commit()
        # База без дефицита = оценка расхода (BMR × коэффициент активности,
        # пока нет калибровки) + тренировочная добавка. Раньше здесь стоял голый
        # BMR — это молча предполагало, что база равна полу, а значит любой
        # дефицит обрезается. Именно то допущение и убрали.
        _factor = load()["policy"].get("activity_factor") or 1.0
        base_before_deficit = bmr_floor(conn, uid) * _factor + _tcx_net(conn, uid, "2026-08-21")
        result_reachable = daily_target(conn, uid, "2026-08-21")
        print(f"daily_target (reachable deadline) = {result_reachable}")
        assert result_reachable["active_milestone"] == "reachable"
        assert result_reachable["deadline_unreachable"] is False
        assert result_reachable["bmr_floor"] < result_reachable["kcal"] < base_before_deficit, (
            f"reachable-deadline kcal {result_reachable['kcal']} should sit strictly between "
            f"floor {result_reachable['bmr_floor']} and undeficited base {base_before_deficit}"
        )
        conn.execute("DELETE FROM milestones WHERE user_id=? AND name='reachable'", (uid,))
        conn.commit()

        # --- шаг 2: невозможный срок (20 кг за 14 дней) держит ровно пол и флагает недостижимость ---
        conn.execute(
            "INSERT INTO milestones(user_id, name, metric, threshold, deadline) "
            "VALUES (?, 'impossible', 'weight_kg', 101.8, '2026-09-03')",
            (uid,),
        )
        conn.commit()
        result_impossible = daily_target(conn, uid, "2026-08-21")
        print(f"daily_target (impossible deadline) = {result_impossible}")
        assert result_impossible["deadline_unreachable"] is True
        assert result_impossible["kcal"] == result_impossible["bmr_floor"] == floor
        assert "deadline_unreachable" in result_impossible["source"]
        # инвариант держит пол даже под запросом дефицита от невозможного срока
        assert result_impossible["kcal"] >= floor

        # --- достигнутая веха пропускается при выборе активной вехи ---
        conn.execute(
            "INSERT INTO milestones(user_id, name, metric, threshold, deadline, achieved_at) "
            "VALUES (?, 'achieved-first', 'weight_kg', 200, '2026-08-22', '2026-08-20 00:00:00')",
            (uid,),
        )
        conn.commit()
        active = _active_milestone(conn, uid)
        assert active is not None and active["name"] == "impossible", (
            f"достигнутая веха с более ранним deadline не должна перебивать недостигнутую, получили {dict(active) if active else None}"
        )
        conn.execute("DELETE FROM milestones WHERE user_id=? AND name IN ('impossible', 'achieved-first')", (uid,))
        conn.commit()

        # --- шаг 5: рефид -> полный TDEE, дефицит с шага 2 не применяется, пол держится ---
        conn.execute(
            "INSERT INTO milestones(user_id, name, metric, threshold, deadline) "
            "VALUES (?, 'refeed-deadline', 'weight_kg', 101.8, '2026-09-03')",
            (uid,),
        )
        conn.execute("INSERT INTO refeed_days(user_id, date) VALUES (?, '2026-08-21')", (uid,))
        conn.commit()
        result_refeed = daily_target(conn, uid, "2026-08-21")
        print(f"daily_target (refeed) = {result_refeed}")
        assert result_refeed["refeed"] is True
        assert result_refeed["deadline_unreachable"] is False  # шаг 2 на паузе, не оценивается
        assert result_refeed["kcal"] >= result_refeed["bmr_floor"]
        # Рефид отдаёт ПОЛНЫЙ расход, а он теперь оценивается как
        # BMR × коэффициент активности, пока нет калибровки (см. daily_target).
        expected_full_tdee = (bmr_floor(conn, uid) * (load()["policy"].get("activity_factor") or 1.0)
                              + _tcx_net(conn, uid, "2026-08-21"))
        assert abs(result_refeed["kcal"] - max(expected_full_tdee, floor)) < 0.01
        conn.execute("DELETE FROM milestones WHERE user_id=? AND name='refeed-deadline'", (uid,))
        conn.execute("DELETE FROM refeed_days WHERE user_id=?", (uid,))
        conn.commit()

        # --- режим болезни: тот же полный TDEE, что и рефид, но тег "+sick" ---
        conn.execute(
            "INSERT INTO milestones(user_id, name, metric, threshold, deadline) "
            "VALUES (?, 'sick-deadline', 'weight_kg', 101.8, '2026-09-03')",
            (uid,),
        )
        result_no_sick = daily_target(conn, uid, "2026-08-21")
        conn.execute("INSERT INTO sick_days(user_id, date) VALUES (?, '2026-08-21')", (uid,))
        conn.commit()
        result_sick = daily_target(conn, uid, "2026-08-21")
        print(f"daily_target (sick) = {result_sick}")
        assert result_sick["sick"] is True
        assert result_sick["kcal"] >= result_no_sick["kcal"], (
            f"цель в день болезни должна быть выше или равна цели без него: "
            f"{result_sick['kcal']} < {result_no_sick['kcal']}"
        )
        assert "sick" in result_sick["source"], result_sick["source"]
        conn.execute("DELETE FROM milestones WHERE user_id=? AND name='sick-deadline'", (uid,))
        conn.execute("DELETE FROM sick_days WHERE user_id=?", (uid,))
        conn.commit()
        print("OK: день болезни даёт цель без дефицита (как рефид), тег «sick» в source")

        # --- daily_targets upsert: второй вызов на ту же дату оставляет одну строку ---
        daily_target(conn, uid, "2026-08-25")
        daily_target(conn, uid, "2026-08-25")
        n_rows = conn.execute(
            "SELECT COUNT(*) c FROM daily_targets WHERE user_id=? AND date='2026-08-25'", (uid,)
        ).fetchone()["c"]
        assert n_rows == 1, f"expected 1 row after upsert twice, got {n_rows}"

        # --- ручное взвешивание без состава не двигает ни пол, ни норму белка ---
        # Регрессия: раньше ffm_kg подменялся весом тела, и Katch на "121.8 тощих кг"
        # поднимал цель почти на 900 ккал, а белок с 143 г до 219 г — в зависимости
        # только от того, чья строка легла в body_metrics последней.
        floor_before = bmr_floor(conn, uid)
        protein_before = targets_for(conn, uid)["protein_g"]
        conn.execute(
            "INSERT INTO body_metrics(user_id, measured_at, weight_kg, burst_key) "
            "VALUES (?, '2026-08-26 07:00:00', ?, 'manual-no-ffm')",
            (uid, 121.8),
        )
        conn.commit()
        assert abs(bmr_floor(conn, uid) - floor_before) < 1e-6, (
            f"пол уехал после взвешивания без состава: {floor_before} -> {bmr_floor(conn, uid)}"
        )
        assert targets_for(conn, uid)["protein_g"] == protein_before, (
            f"норма белка уехала: {protein_before} -> {targets_for(conn, uid)['protein_g']}"
        )
        print("OK: взвешивание без состава не двигает BMR_floor и норму белка")

        # ---- пол от жировой массы, доли предела и потолка дефицита (kcal_floor) ----
        # docs/adr/0001-пол-калорий.md: доля предела Alpert убывает по шкале
        # процента жира (своей для пола), на жирном конце растёт до
        # fat_share_fat_proven, если lean_share доказывает малую долю мышц в
        # потере, а сверху дефицит режет потолок темпа снижения веса.
        def _reset_metrics():
            conn.execute("DELETE FROM body_metrics WHERE user_id=?", (uid,))

        def _add_metric(days_ago, weight_kg, fat_pct=None, ffm_kg=None, hour=7):
            ts = (local_now() - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0, microsecond=0)
            conn.execute(
                "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, fat_pct, ffm_kg) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (uid, f"fs-{days_ago}-{hour}", ts.strftime("%Y-%m-%d %H:%M:%S"), weight_kg, fat_pct, ffm_kg),
            )

        tdee = 2614.0
        conn.execute("UPDATE users SET sex='m' WHERE id=?", (uid,))

        # 1) мужчина 118.7 кг / 33.3% жира, доля мышц не доказана -> жирный
        # конец шкалы = fat_share_fat (0.3) -> дефицит ~822, пол ~1792
        _reset_metrics()
        _add_metric(0, 118.7, fat_pct=33.3)
        conn.commit()
        assert lean_share(conn, uid) is None, "без истории FFM доля мышц не может быть доказана"
        share1 = fat_share(conn, uid, 33.3)
        assert abs(share1 - 0.3) < 1e-9, share1
        floor1, reason1 = kcal_floor(conn, uid, tdee)
        assert reason1 == "fat_supply", reason1
        assert abs(floor1 - 1792.23) < 1, floor1
        print(f"OK: 118.7кг/33.3%, доля {share1}, пол {floor1:.0f} (ожидание ~1792)")

        # 2) та же жировая масса, но за 28 дней доказана доля мышц <15% в
        # потере -> жирный конец шкалы поднимается до fat_share_fat_proven (0.4)
        _reset_metrics()
        for days_ago, w, f in (
            (27, 121.7, 79.6), (24, 121.0, 79.55), (21, 120.4, 79.5), (18, 119.9, 79.45),
            (14, 119.5, 79.4), (10, 119.1, 79.35), (5, 118.9, 79.3), (0, 118.7, 79.2),
        ):
            _add_metric(days_ago, w, ffm_kg=f)
        conn.execute("UPDATE body_metrics SET fat_pct=33.3 WHERE user_id=? AND burst_key='fs-0-7'", (uid,))
        conn.commit()
        proven2 = lean_share(conn, uid)
        assert proven2 is not None and proven2 < 0.15, proven2
        share2 = fat_share(conn, uid, 33.3)
        assert abs(share2 - 0.4) < 1e-9, share2
        floor2, reason2 = kcal_floor(conn, uid, tdee)
        assert reason2 == "fat_supply", reason2
        assert abs((tdee - floor2) - 1095.69) < 1, tdee - floor2
        print(f"OK: доказанная доля мышц {proven2:.3f}<0.15 -> доля {share2}, "
              f"дефицит {tdee - floor2:.0f} (ожидание ~1096)")

        # 2b) тощая масса растёт на фоне потери веса — доля мышц в потере 0, а не |Δ|
        _reset_metrics()
        for days_ago, w, f in (
            (27, 121.7, 78.2), (24, 121.0, 78.3), (21, 120.4, 78.4), (18, 119.9, 78.5),
            (14, 119.5, 78.7), (10, 119.1, 78.9), (5, 118.9, 79.1), (0, 118.7, 79.2),
        ):
            _add_metric(days_ago, w, ffm_kg=f)
        conn.commit()
        assert lean_share(conn, uid) == 0.0, lean_share(conn, uid)

        # 3) худой 85 кг / 12% -> нижняя граница шкалы, доля = fat_share_lean (0.7)
        _reset_metrics()
        _add_metric(0, 85.0, fat_pct=12.0)
        conn.commit()
        share3 = fat_share(conn, uid, 12.0)
        assert abs(share3 - 0.7) < 1e-9, share3
        floor3, reason3 = kcal_floor(conn, uid, tdee)
        assert reason3 == "fat_supply", reason3
        assert abs((tdee - floor3) - 494.8) < 1, tdee - floor3
        print(f"OK: 85кг/12%, доля {share3}, дефицит {tdee - floor3:.0f} (ожидание ~495)")

        # 4) женщина 80 кг / 32% -> женская шкала, интерполяция ~0.433; тот же
        # человек с полом NULL -> мужская шкала как более осторожная (доля 0.3)
        _reset_metrics()
        conn.execute("UPDATE users SET sex='f' WHERE id=?", (uid,))
        _add_metric(0, 80.0, fat_pct=32.0)
        conn.commit()
        share4 = fat_share(conn, uid, 32.0)
        assert abs(share4 - 0.43333) < 1e-4, share4
        floor4, reason4 = kcal_floor(conn, uid, tdee)
        assert reason4 == "fat_supply", reason4
        assert abs((tdee - floor4) - 768.77) < 1, tdee - floor4
        conn.execute("UPDATE users SET sex=NULL WHERE id=?", (uid,))
        conn.commit()
        share4_unknown = fat_share(conn, uid, 32.0)
        assert abs(share4_unknown - 0.3) < 1e-9, share4_unknown
        print(f"OK: женщина 80кг/32% доля {share4:.3f} (~0.433); пол NULL -> "
              f"мужская шкала, доля {share4_unknown}")

        # 5) очень полный 160 кг / 50%, доля мышц доказана (<15%) -> доля 0.4,
        # но потолок темпа (min(1.5кг, 1.5% веса)/нед = 1650 ккал/сут) режет раньше
        conn.execute("UPDATE users SET sex='m' WHERE id=?", (uid,))
        _reset_metrics()
        for days_ago, w, f in (
            (27, 162.5, 95.0), (24, 162.0, 94.9), (21, 161.6, 94.85), (18, 161.2, 94.8),
            (14, 160.9, 94.75), (10, 160.5, 94.7), (5, 160.2, 94.65), (0, 160.0, 94.6),
        ):
            _add_metric(days_ago, w, ffm_kg=f)
        conn.execute("UPDATE body_metrics SET fat_pct=50.0 WHERE user_id=? AND burst_key='fs-0-7'", (uid,))
        conn.commit()
        proven5 = lean_share(conn, uid)
        assert proven5 is not None and proven5 < 0.15, proven5
        share5 = fat_share(conn, uid, 50.0)
        assert abs(share5 - 0.4) < 1e-9, share5
        big_tdee = 3200.0
        floor5, reason5 = kcal_floor(conn, uid, big_tdee)
        assert reason5 == "deficit_cap", reason5
        assert abs((big_tdee - floor5) - 1650.0) < 0.01, big_tdee - floor5
        print(f"OK: 160кг/50%, доля {share5}, потолок темпа режет дефицит до "
              f"{big_tdee - floor5:.0f} (reason={reason5})")

        # Макро-минимум как нижняя подпорка: жира много, расход мал -> формула
        # уводит пол ниже, чем физически занимают белок и жиры из целей.
        _reset_metrics()
        _add_metric(0, 120.0, fat_pct=34.0, ffm_kg=79.0)
        conn.commit()
        tiny_tdee = 800.0
        macro_floor, macro_reason = kcal_floor(conn, uid, tiny_tdee)
        assert macro_reason == "macro_minimum", macro_reason
        assert abs(macro_floor - macro_minimum_kcal(conn, uid)) < 0.01

        # Нет биоимпеданса -> возвращаемся к BMR, а не гадаем о жире.
        _reset_metrics()
        _add_metric(0, 120.0)
        conn.commit()
        nofat_floor, nofat_reason = kcal_floor(conn, uid, tdee)
        assert nofat_reason == "bmr" and abs(nofat_floor - bmr_floor(conn, uid)) < 1e-6
        print("OK: пол считается от доли предела Alpert по проценту жира, "
              "подпёрт макро-минимумом и падает на BMR без биоимпеданса")

        # ---- deadline_verdict: честный вердикт срока (CONTEXT.md «Недостижимый срок») ----
        # План ниже пола (8 кг за 20/40 дней требует дефицита сильно глубже пола
        # для 100кг/30% жира) в обоих случаях — различается только срок, поэтому
        # прогноз по фактическому логу (1400 ккал/сут, 12 из 14 дней) один и тот же.
        _reset_metrics()
        today = local_now().date()
        for days_ago in range(13, -1, -1):
            _add_metric(days_ago, 100.0, fat_pct=30.0, ffm_kg=70.0)
        conn.execute("DELETE FROM food_log WHERE user_id=?", (uid,))
        for days_ago in range(13, 1, -1):
            d = (today - timedelta(days=days_ago)).isoformat()
            cur = conn.execute(
                "INSERT INTO food_log(user_id, eaten_at, meal_slot) VALUES (?, ?, 'lunch')",
                (uid, f"{d} 12:00:00"),
            )
            conn.execute(
                "INSERT INTO food_items(food_log_id, name, kcal) VALUES (?, 'selfcheck-food', 1400)",
                (cur.lastrowid,),
            )
        conn.commit()

        # плана хватает — дефицит успевает в срок (40 дней)
        conn.execute("DELETE FROM milestones WHERE user_id=?", (uid,))
        conn.execute(
            "INSERT INTO milestones(user_id, name, metric, threshold, deadline) VALUES (?, 'dv-loose', 'weight_kg', 92, ?)",
            (uid, (today + timedelta(days=40)).isoformat()),
        )
        conn.commit()
        v_ok = deadline_verdict(conn, uid, today.isoformat())
        assert v_ok is not None and v_ok["plan_below_floor"] is True, v_ok
        assert v_ok["forecast_reach"] is not None and v_ok["forecast_reach"]["reached"] is True, v_ok
        assert v_ok["unreachable"] is False, v_ok
        print(f"OK: план ниже пола, прогноз успевает к {v_ok['deadline']} "
              f"(earliest={v_ok['forecast_reach']['earliest']}) -> unreachable False")

        # тот же план и лог, но срок туже — прогноз по факту к сроку не успевает
        conn.execute("DELETE FROM milestones WHERE user_id=?", (uid,))
        conn.execute(
            "INSERT INTO milestones(user_id, name, metric, threshold, deadline) VALUES (?, 'dv-tight', 'weight_kg', 92, ?)",
            (uid, (today + timedelta(days=20)).isoformat()),
        )
        conn.commit()
        v_bad = deadline_verdict(conn, uid, today.isoformat())
        assert v_bad["plan_below_floor"] is True, v_bad
        assert v_bad["unreachable"] is True, v_bad
        print(f"OK: план ниже пола, прогноз к {v_bad['deadline']} не успевает "
              f"(earliest={v_bad['forecast_reach']['earliest']}) -> unreachable True")

        # тот же план и срок, но еды залогировано меньше 11/14 дней -> прогноза нет
        conn.execute("DELETE FROM food_log WHERE user_id=?", (uid,))
        conn.commit()
        v_none = deadline_verdict(conn, uid, today.isoformat())
        assert v_none["plan_below_floor"] is True, v_none
        assert v_none["forecast_reach"] is None and v_none["unreachable"] is None, v_none
        assert v_none.get("forecast_note"), "unreachable=None обязан объяснять причину в forecast_note"
        print(f"OK: план ниже пола, прогноза нет ({v_none['forecast_note']}) -> unreachable None")

        conn.execute("DELETE FROM milestones WHERE user_id=?", (uid,))
        conn.commit()
        assert deadline_verdict(conn, uid, today.isoformat()) is None, "без активной вехи со сроком вердикта нет"
        print("OK: без активной вехи со сроком deadline_verdict возвращает None")

        # ---- daily_expenditure(): блендер расхода дня (ADR 0004) ----
        def _reset_all(uid):
            conn.execute("DELETE FROM body_metrics WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM food_log WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM daily_watch WHERE user_id=?", (uid,))

        def _add_food_day(uid, d, kcal):
            cur = conn.execute(
                "INSERT INTO food_log(user_id, eaten_at, meal_slot) VALUES (?, ?, 'lunch')",
                (uid, f"{d} 12:00:00"),
            )
            conn.execute(
                "INSERT INTO food_items(food_log_id, name, kcal) VALUES (?, 'blend-food', ?)",
                (cur.lastrowid, kcal),
            )

        def _add_watch_day(uid, d, steps=None, active_kcal=None):
            conn.execute(
                "INSERT INTO daily_watch(user_id, date, steps, active_kcal, source, created_at) "
                "VALUES (?, ?, ?, ?, 'selfcheck', ?)",
                (uid, d, steps, active_kcal, f"{d} 23:00:00"),
            )

        de_date = today.isoformat()

        # общий фон: 14 дней постоянного веса 100.0 кг (Δвес=0 -> измеренный TDEE = средний intake)
        _reset_all(uid)
        for days_ago in range(13, -1, -1):
            _add_metric(days_ago, 100.0)
        conn.commit()
        bmr_de = bmr_floor(conn, uid)
        factor_de = load()["policy"].get("activity_factor") or 1.0

        # 1) полный лог (14/14), часов нет -> adaptive 0.6 + activity_factor 0.4 (§1 бита ADR 0004)
        for days_ago in range(13, -1, -1):
            _add_food_day(uid, (today - timedelta(days=days_ago)).isoformat(), 2600)
        conn.commit()
        exp1 = daily_expenditure(conn, uid, de_date)
        assert exp1["logged_days"] == 14, exp1
        assert exp1["parts"]["steps"] is None and exp1["parts"]["watch"] is None, exp1
        assert abs(exp1["parts"]["adaptive"]["share"] - 0.6) < 1e-9, exp1
        assert abs(exp1["parts"]["activity_factor"]["share"] - 0.4) < 1e-9, exp1
        expected_kcal1 = 0.6 * 2600 + 0.4 * (bmr_de * factor_de)
        assert abs(exp1["kcal"] - expected_kcal1) < 0.01, (exp1["kcal"], expected_kcal1)
        assert exp1["tags"] == ["adaptive", "activity_factor"], exp1["tags"]
        print(f"OK: daily_expenditure полный лог без часов -> доли 0.6/0.4, kcal={exp1['kcal']:.1f}")

        # 2) + шаги и часы за последние 7 дней -> 0.6/0.2/0.2, kcal — ручная взвешенная сумма
        for days_ago in range(6, -1, -1):
            _add_watch_day(uid, (today - timedelta(days=days_ago)).isoformat(), steps=8000, active_kcal=400)
        conn.commit()
        exp2 = daily_expenditure(conn, uid, de_date)
        for name in ("adaptive", "steps", "watch"):
            assert exp2["parts"][name] is not None, exp2
        assert exp2["parts"]["activity_factor"] is None, exp2
        assert abs(exp2["parts"]["adaptive"]["share"] - 0.6) < 1e-9, exp2
        assert abs(exp2["parts"]["steps"]["share"] - 0.2) < 1e-9, exp2
        assert abs(exp2["parts"]["watch"]["share"] - 0.2) < 1e-9, exp2
        weight_de = _smoothed_weight_kg(conn, uid)
        steps_est_expected = bmr_de * 1.2 + max(0, 8000 - 3000) * 0.0005 * weight_de
        watch_est_expected = bmr_de * 1.1 + 400 * 0.8
        expected_kcal2 = 0.6 * 2600 + 0.2 * steps_est_expected + 0.2 * watch_est_expected
        assert abs(exp2["kcal"] - expected_kcal2) < 0.01, (exp2["kcal"], expected_kcal2)
        assert exp2["tags"] == ["adaptive", "steps", "watch"], exp2["tags"]
        print(f"OK: daily_expenditure + шаги/часы -> доли 0.6/0.2/0.2, kcal={exp2['kcal']:.1f}")

        # 5) пол по-прежнему клампит цель, даже когда расход — смесь нескольких частей,
        # не только activity_factor: невозможный срок доводит блендированную цель ровно до пола.
        conn.execute(
            "UPDATE body_metrics SET fat_pct=40.0 WHERE user_id=? AND date(measured_at)=?",
            (uid, today.isoformat()),
        )
        conn.commit()
        floor2, _ = kcal_floor(conn, uid, exp2["kcal"])
        conn.execute(
            "INSERT INTO milestones(user_id, name, metric, threshold, deadline) VALUES (?, 'blend-floor', 'weight_kg', 70, ?)",
            (uid, (today + timedelta(days=3)).isoformat()),
        )
        conn.commit()
        result5 = daily_target(conn, uid, de_date)
        assert result5["kcal"] == floor2, (result5["kcal"], floor2)
        assert result5["source"].startswith("blend:adaptive+steps+watch"), result5["source"]
        assert "clamped" in result5["source"], result5["source"]
        conn.execute("DELETE FROM milestones WHERE user_id=? AND name='blend-floor'", (uid,))
        conn.execute("UPDATE body_metrics SET fat_pct=NULL WHERE user_id=? AND date(measured_at)=?",
                     (uid, today.isoformat()))
        conn.commit()
        print(f"OK: пол ({floor2:.0f}) применяется к блендированному расходу adaptive+steps+watch, source={result5['source']}")

        # 3) всего 3 залогированных дня -> адаптивная часть недоступна (порог blend_min_logged_days=4), 0.5/0.5
        conn.execute("DELETE FROM food_log WHERE user_id=?", (uid,))
        for days_ago in range(2, -1, -1):
            _add_food_day(uid, (today - timedelta(days=days_ago)).isoformat(), 2600)
        conn.commit()
        exp3 = daily_expenditure(conn, uid, de_date)
        assert exp3["logged_days"] == 3, exp3
        assert exp3["parts"]["adaptive"] is None, exp3
        assert abs(exp3["parts"]["steps"]["share"] - 0.5) < 1e-9, exp3
        assert abs(exp3["parts"]["watch"]["share"] - 0.5) < 1e-9, exp3
        expected_kcal3 = 0.5 * steps_est_expected + 0.5 * watch_est_expected
        assert abs(exp3["kcal"] - expected_kcal3) < 0.01, (exp3["kcal"], expected_kcal3)
        assert exp3["tags"] == ["steps", "watch"], exp3["tags"]
        print("OK: daily_expenditure 3 дня лога -> адаптивная часть недоступна, шаги/часы 0.5/0.5")

        # 4) совсем ничего -> activity_factor 1.0, тот же путь, что старый estimated_tdee
        _reset_all(uid)
        _add_metric(0, 100.0)
        conn.commit()
        exp4 = daily_expenditure(conn, uid, today.isoformat())
        assert exp4["parts"]["adaptive"] is None, exp4
        assert exp4["parts"]["steps"] is None and exp4["parts"]["watch"] is None, exp4
        assert abs(exp4["parts"]["activity_factor"]["share"] - 1.0) < 1e-9, exp4
        bmr4 = bmr_floor(conn, uid)
        expected_kcal4 = bmr4 * factor_de
        assert abs(exp4["kcal"] - expected_kcal4) < 0.01, (exp4["kcal"], expected_kcal4)
        assert exp4["tags"] == ["activity_factor"], exp4["tags"]
        print(f"OK: daily_expenditure без данных -> activity_factor 1.0, kcal={exp4['kcal']:.1f} = BMR×коэффициент "
              f"(тот же путь, что старый estimated_tdee)")

        _reset_all(uid)
        conn.commit()

        conn.close()
        print("OK: energy.py self-check passed")
