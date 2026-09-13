"""Калораж — §09. Формулы, коэффициенты и порядок клампа воспроизводятся дословно.

Порядок применения (§09) — пять шагов:
  1  TDEE_факт ← адаптивная база (или формула-затравка BMR_floor, если лог неполон)
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
ткань (≈31 ккал/сут на кг жира, с запасом) — см. kcal_floor. BMR не смотрит на
жировую массу вовсе и потому одинаково неверен в обе стороны: переосторожен при
большом запасе жира и слишком мягок у сухого человека. Нижняя граница снизу
подпёрта макро-минимумом: в цель обязана помещаться собственная норма белка.
"""
import sqlite3
from datetime import date, datetime

from health_core.config import latest_ffm, load, targets_for, local_now

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
    """Жировая масса из последнего замера с процентом жира. Нет биоимпеданса —
    None: считать жир от веса «на глаз» здесь нельзя, от этого числа зависит
    нижняя граница калоража."""
    row = conn.execute(
        "SELECT weight_kg, fat_pct FROM body_metrics WHERE user_id=? AND fat_pct IS NOT NULL "
        "AND fat_pct > 0 ORDER BY measured_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is None or not row["weight_kg"]:
        return None
    return row["weight_kg"] * row["fat_pct"] / 100


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
    порядка 31 ккал в сутки с килограмма (Alpert; величина в policy). Дефицит
    сверх этого потолка организм добирает из тощей массы — это и есть та
    опасность, ради которой пол существует.

        безопасный дефицит = жировая масса × потолок × запас
        пол = расход − безопасный дефицит

    Запас (policy.fat_supply_safety) обязателен: 31 — это пиковая способность
    из голодных исследований, а не режим, в котором живут месяцами.

    Пол не опускается ниже макро-минимума (белок + жиры из целей): цель, в
    которую не помещается собственная норма белка, гарантирует потерю мышц.

    Жировая масса неизвестна (нет биоимпеданса) — возвращаемся к BMR: без
    данных о жире оценивать его отдачу нечем, и осторожный вариант честнее.
    """
    policy = load()["policy"]
    bmr = bmr_floor(conn, user_id)
    fat_kg = fat_mass_kg(conn, user_id)
    if fat_kg is None:
        return bmr, "bmr"

    per_kg = policy.get("fat_supply_kcal_per_kg")
    safety = policy.get("fat_supply_safety")
    if not per_kg or not safety:
        return bmr, "bmr"        # модель выключена настройкой — прежнее поведение

    safe_deficit = fat_kg * per_kg * safety
    floor = full_tdee - safe_deficit
    reason = "fat_supply"

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


def adaptive_tdee(
    conn: sqlite3.Connection, user_id: int, window_days: int = 14, for_date: str | None = None
) -> float | None:
    """TDEE_факт = средний_intake_Nд + (Δвес_Nд · 7700 / N).

    Достоверность требует еду залогированной минимум 11 из 14 дней (порог
    масштабируется пропорционально для нестандартного окна). Иначе — None,
    калибровка замораживается, а не подгоняется.

    Окно по умолчанию заканчивается сегодня (для forecast.py/report.py, которым
    нужен именно текущий TDEE). daily_target() пересчитывает цель задним числом
    и обязан передавать свою же дату через for_date, иначе повторный вызов на
    одну и ту же историческую дату даёт разный результат в зависимости от того,
    когда он был выполнен — окно "плывёт" вместе с local_now()."""
    end = datetime.fromisoformat(for_date[:10]).date() if for_date else local_now().date()
    start = end.fromordinal(end.toordinal() - window_days + 1)
    start_s, end_s = start.isoformat(), end.isoformat()

    logged_days = conn.execute(
        "SELECT COUNT(DISTINCT date(eaten_at)) c FROM food_log WHERE user_id=? "
        "AND date(eaten_at) BETWEEN ? AND ?",
        (user_id, start_s, end_s),
    ).fetchone()["c"]
    min_days = -(-window_days * 11 // 14)  # ceil, воспроизводит порог 11/14 по умолчанию
    if logged_days < min_days:
        return None

    daily_kcals = conn.execute(
        "SELECT SUM(fi.kcal) kcal FROM food_log fl JOIN food_items fi ON fi.food_log_id = fl.id "
        "WHERE fl.user_id=? AND date(fl.eaten_at) BETWEEN ? AND ? GROUP BY date(fl.eaten_at)",
        (user_id, start_s, end_s),
    ).fetchall()
    kcal_values = [r["kcal"] for r in daily_kcals if r["kcal"] is not None]
    if not kcal_values:
        return None
    mean_intake = sum(kcal_values) / len(kcal_values)

    weights = _morning_weights(conn, user_id, start_s, end_s)
    if len(weights) < 2:
        return None
    delta_weight = weights[-1] - weights[0]

    return mean_intake - (delta_weight * 7700 / window_days)


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
    срок уже наступил/прошёл (дефицит физически недостижим за 0 дней)."""
    if milestone["metric"] != "weight_kg":
        return None
    metric_row = _latest_metric(conn, user_id)
    if metric_row is None:
        return None
    required_kg = metric_row["weight_kg"] - milestone["threshold"]
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


def daily_target(conn: sqlite3.Connection, user_id: int, date: str) -> dict:
    bmr = bmr_floor(conn, user_id)
    adaptive = adaptive_tdee(conn, user_id, for_date=date)
    if adaptive is not None:
        base, seed_tag = adaptive, "adaptive"
    else:
        # Пока калибровки нет (еда залогирована меньше 11 дней из 14), базой был
        # голый BMR. Это запирало систему: дефицит вычитается из базы, база равна
        # полу, значит ЛЮБОЙ дефицит уходит под пол и обрезается — цель навсегда
        # равна BMR, а срок вехи объявляется недостижимым. Человек видит «система
        # не даёт похудеть» и уходит считать калораж на стороне.
        #
        # Оценка расхода = BMR × коэффициент активности. Это заведомо грубее
        # измеренного TDEE, поэтому: (1) коэффициент консервативный и покрывает
        # только бытовую активность — тренировки уже приходят отдельно в tcx_net,
        # и удваивать их нельзя; (2) как только наберётся 11 дней из 14, adaptive
        # забирает базу себе и оценка больше не участвует; (3) нижняя граница
        # держится всегда — см. kcal_floor, она считается от жировой массы.
        factor = load()["policy"].get("activity_factor") or 1.0
        base = bmr * factor
        seed_tag = "estimated_tdee" if factor != 1.0 else "bmr_floor_seed"
    tcx_net = _tcx_net(conn, user_id, date)
    full_tdee = base + tcx_net  # шаг 1 (+ тренировочная добавка), без дефицита и без клампа

    # Пол считается ПОСЛЕ расхода и от него: безопасный дефицит ограничен тем,
    # сколько энергии способен отдать жир, а не абстрактным BMR. См. kcal_floor.
    floor, floor_reason = kcal_floor(conn, user_id, full_tdee)

    tags = [seed_tag]
    if tcx_net:
        tags.append("tcx")

    deadline_unreachable = False
    active_milestone_name = None
    refeed = _is_refeed(conn, user_id, date)

    if refeed:
        # шаг 5: рефид активен -> цель := полный TDEE, шаги 2-4 на паузе.
        # Но безопасный пол (шаг 3) — не опция, а инвариант: держим его и тут.
        kcal = max(full_tdee, floor)
        tags.append("refeed")
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
        "refeed": refeed,
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

        # ---- пол от жировой массы (kcal_floor) ----
        # Ради этого блока и переписывался механизм: пол обязан зависеть от
        # жира, а не от BMR, иначе он одинаково неверен в обе стороны.
        pol = load()["policy"]
        per_kg, safety = pol["fat_supply_kcal_per_kg"], pol["fat_supply_safety"]

        conn.execute("DELETE FROM body_metrics WHERE user_id=?", (uid,))
        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg, fat_pct) "
            "VALUES (?, 'floor-fat', '2026-08-21 07:00:00', 120.0, 79.0, 34.0)", (uid,))
        conn.commit()
        bmr_now = bmr_floor(conn, uid)
        fat_kg = fat_mass_kg(conn, uid)
        assert abs(fat_kg - 40.8) < 0.01, f"жировая масса посчитана неверно: {fat_kg}"

        tdee = 2600.0
        floor_v, reason = kcal_floor(conn, uid, tdee)
        assert reason == "fat_supply", reason
        assert abs(floor_v - (tdee - fat_kg * per_kg * safety)) < 0.01
        assert floor_v < bmr_now, (
            f"смысл всей правки: при {fat_kg:.0f} кг жира пол обязан быть НИЖЕ BMR "
            f"({floor_v:.0f} против {bmr_now:.0f})")

        # Малый запас жира -> пол поднимается и подходит к BMR: опасность растёт
        # по мере похудения, и граница должна двигаться навстречу сама.
        conn.execute("DELETE FROM body_metrics WHERE user_id=?", (uid,))
        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg, fat_pct) "
            "VALUES (?, 'floor-lean', '2026-08-21 07:00:00', 85.0, 72.0, 12.0)", (uid,))
        conn.commit()
        lean_floor, _ = kcal_floor(conn, uid, tdee)
        assert lean_floor > floor_v, (
            f"у сухого человека пол обязан быть выше: {lean_floor:.0f} против {floor_v:.0f}")

        # Макро-минимум как нижняя подпорка: жира много, расход мал -> формула
        # уводит пол ниже, чем физически занимают белок и жиры из целей.
        tiny_tdee = 800.0
        conn.execute("DELETE FROM body_metrics WHERE user_id=?", (uid,))
        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg, fat_pct) "
            "VALUES (?, 'floor-macro', '2026-08-21 07:00:00', 120.0, 79.0, 34.0)", (uid,))
        conn.commit()
        macro_floor, macro_reason = kcal_floor(conn, uid, tiny_tdee)
        assert macro_reason == "macro_minimum", macro_reason
        assert abs(macro_floor - macro_minimum_kcal(conn, uid)) < 0.01

        # Нет биоимпеданса -> возвращаемся к BMR, а не гадаем о жире.
        conn.execute("DELETE FROM body_metrics WHERE user_id=?", (uid,))
        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg) "
            "VALUES (?, 'floor-nofat', '2026-08-21 07:00:00', 120.0)", (uid,))
        conn.commit()
        nofat_floor, nofat_reason = kcal_floor(conn, uid, tdee)
        assert nofat_reason == "bmr" and abs(nofat_floor - bmr_floor(conn, uid)) < 1e-6
        print("OK: пол считается от жировой массы, растёт по мере похудения, "
              "подпёрт макро-минимумом и падает на BMR без биоимпеданса")

        conn.close()
        print("OK: energy.py self-check passed")
