"""Прогноз массы тела — динамический баланс энергии, а не экстраполяция тренда.

ПОЧЕМУ НЕ ЛИНЕЙНАЯ ЭКСТРАПОЛЯЦИЯ. В system_promt.md стоит прямой запрет на
"при таком темпе к дате X", и он правильный: продолжение прямой врёт всегда в
одну сторону — вниз. По мере падения массы падает и расход, дефицит при том же
приходе сжимается, потеря замедляется.

Цена ошибки известна количественно. Правило Wishnofsky (1958) "7700 ккал = 1 кг"
статично и не видит обратной связи: смещение 4.8 кг против 0.4 кг у динамической
модели (Thomas et al., J Acad Nutr Diet, 2014). На годовом горизонте при дефиците
~480 ккал/сут статичное правило обещает ~22 кг, динамическая модель — ~10-11 кг,
то есть переоценка вдвое (Hall KD et al., Lancet, 2011).

ЧТО СЧИТАЕТСЯ ЗДЕСЬ. Посуточная симуляция в духе модели Hall (PLOS Comput Biol,
2009; Lancet, 2011 — она же лежит в основе NIH Body Weight Planner):

    BMR(W)   — Mifflin, пересчитывается каждый день по текущей массе
    TDEE     — BMR × activity_factor × (1 − адаптивный термогенез)
    дефицит  — TDEE − приход
    ткань    — дефицит делится на жир и тощую по правилу Форбса
    W        — сумма пересчитанных FM и FFM

Энергетические плотности взяты оттуда же: жир 39.5 МДж/кг ≈ 9441 ккал/кг,
тощая ткань 7.6 МДж/кг ≈ 1816 ккал/кг.

ПРАВИЛО ФОРБСА И ЕГО ГРАНИЦА. Forbes (Hum Biol, 1987): FFM = 10.4·ln(FM) + 14.2.
Прямая производная даёт dFFM/dFM = 10.4/FM — это НЕ то, что нужно для разнесения
потери массы. Нужна доля от изменения полной массы, а W = FM + FFM, поэтому

    dFFM/dW = (dFFM/dFM) / (1 + dFFM/dFM) = (10.4/FM) / (1 + 10.4/FM)
            = 10.4 / (10.4 + FM)

Шаг через цепное правило здесь выписан намеренно: без него формула в коде
выглядит опечаткой, и перепроверяющий получит 10.4/FM и решит, что нашёл баг.
При FM 42 кг это ~20%
тощей ткани в потере. Но формула строго верна только для малых изменений: на
потерях в десятки килограммов она систематически ЗАНИЖАЕТ потерю тощей массы,
и корректная форма требует W-функции Ламберта (Hall KD, Br J Nutr, 2007). Здесь
взята дифференциальная форма, применяемая посуточно, — на шаге в один день
изменение мало, но накопленное смещение всё равно в сторону оптимизма.
Heymsfield et al. (Obes Rev, 2014) отдельно критикуют расхожее "четверть потери
— тощая масса" как непригодное для конкретного человека.

Сверка с фармакологией: в SURMOUNT-1 к 72 неделе на тирзепатиде вес −21.3%,
жировая −33.9%, тощая −10.9%, то есть примерно 75% жира и 25% тощей (Look M
et al., Diabetes Obes Metab, 2025). Форбс при 42 кг жира даёт 80/20 — модель
слегка оптимистична и по этому срезу тоже.

ЧЕГО МОДЕЛЬ НЕ УМЕЕТ, и это не лечится подбором коэффициентов:
  - приход еды известен плохо. Занижение в пищевых дневниках — медиана 605
    ккал/сут по литературе, у этого человека расчётно около 400. Это главный
    источник ошибки, поэтому наружу отдаётся полоса, а не одна кривая;
  - фармакологический эффект тирзепатида на аппетит и расход не заложен вовсе.
    Он сидит внутри наблюдаемого приёма пищи, и при смене дозы прогноз, снятый
    на старой дозе, силы не имеет;
  - плато модель не предсказывает. В SURMOUNT медиана времени до плато при
    ожирении III степени — 36.1 недели (Horn DB et al., Clin Obes, 2025).
    Симуляция про это не знает и продолжит снижать вес;
  - правило Форбса выведено на данных по ПОХУДЕНИЮ. К набору массы оно здесь
    применяется зеркально, и это допущение без источника: состав набора не
    обязан быть отражением состава потери. Профицитные сценарии — грубая
    прикидка, а не прогноз;
  - симуляция обрывается, когда жировая масса подходит к неснижаемому минимуму
    (ESSENTIAL_FAT_PCT_M / ESSENTIAL_FAT_PCT_F). Дальше модель неприменима: правило Форбса при пустом
    депо продолжало бы срезать одну тощую массу и увело бы вес в бессмыслицу;
  - адаптивный термогенез — одно число на весь горизонт, и по умолчанию ноль.
    Согласованной величины в литературе нет: Fothergill et al. (Obesity, 2016)
    на участниках Biggest Loser показали −499 ккал/сут спустя 6 лет, Martins
    et al. (AJCN, 2022) возражают, что эффект — артефакт измерения в дефиците.
    Ставить сюда правдоподобное число значило бы выдумать его.

Прогноз — оценка, и он помечен как оценка везде, где выходит наружу.
"""
import sqlite3
import statistics
from datetime import date, timedelta

from health_core.config import latest_ffm, load, local_now, user_now
from health_core.energy import (MIN_LOG_STREAK_DAYS, adaptive_tdee, bmr_mifflin,
                                logged_streak, _age_years)

# Энергетическая плотность тканей, ккал/кг (Hall, Lancet 2011).
KCAL_PER_KG_FAT = 9441
KCAL_PER_KG_LEAN = 1816

# Forbes: dFFM/dW = FORBES_C / (FORBES_C + FM). Константа из оригинальной работы.
FORBES_C = 10.4

# Горизонт, дальше которого модель не отдаёт ничего.
MAX_HORIZON_DAYS = 182

# Свежесть замера массы: старше — прогноз строить не от чего.
MAX_WEIGHT_AGE_DAYS = 7

# Неопределённость приёма пищи, ккал/сут в обе стороны.
DEFAULT_INTAKE_UNCERTAINTY = 300.0

# Собственная ошибка модели, доля массы. Из валидации NIH Body Weight Planner
# на реальных пациентах: индивидуальная относительная ошибка прогноза на
# горизонте 6 месяцев укладывается в −6.2% .. +3.7% массы (5-й и 95-й
# перцентили). ВНИМАНИЕ: опубликована точка на 182 дня, кривой нарастания
# ошибки нет — здесь линейная интерполяция по горизонту, это допущение, а не
# результат из статьи. Смысл: полоса от одного лишь разброса прихода заведомо
# уже реальной, и показывать её как полную было бы враньём.
MODEL_ERR_LO_PCT = 0.062
MODEL_ERR_HI_PCT = 0.037
MODEL_ERR_REF_DAYS = 182

# Неснижаемый жир — около 3-5% массы у мужчин, ~10-13% у женщин (физиология
# существенно другая). Ниже этой границы симуляция обрывается: правило Форбса
# там продолжало бы срезать одну тощую массу.
ESSENTIAL_FAT_PCT_M = 0.05
ESSENTIAL_FAT_PCT_F = 0.11

# Диапазон вменяемости для сценарного прихода, ккал/сут. Без него отрицательное
# или абсурдное число даёт гигантский дефицит и обваливает траекторию за сутки.
MIN_SCENARIO_KCAL = 400
MAX_SCENARIO_KCAL = 10000

# Медиана времени до плато в SURMOUNT по классам ожирения, недели
# (Horn DB et al., Clin Obes, 2025). Ключ — нижняя граница ИМТ.
PLATEAU_WEEKS = ((40.0, 36.1), (35.0, 36.1), (30.0, 26.0), (25.0, 24.3))


def _policy() -> dict:
    return load().get("policy", {})


def _forecast_cfg() -> dict:
    return load().get("forecast", {})


def smoothed_weight(conn: sqlite3.Connection, user_id: int, days: int = 7) -> tuple[float, str] | None:
    """Сглаженная масса и дата последнего замера.

    Медиана внутри дня (весы дают до 0.9 кг разброса между соседними
    вставаниями), затем линия тренда по дням, взятая на последнюю дату. Одиночный замер как отправная точка
    прогноза — это перенос шума прибора на весь горизонт.
    """
    rows = conn.execute(
        "SELECT date(measured_at) d, weight_kg w FROM body_metrics "
        "WHERE user_id=? AND weight_kg IS NOT NULL ORDER BY measured_at",
        (user_id,),
    ).fetchall()
    if not rows:
        return None
    by_day: dict[str, list[float]] = {}
    for r in rows:
        by_day.setdefault(r["d"], []).append(r["w"])
    ordered = sorted(by_day)
    cutoff = (date.fromisoformat(ordered[-1]) - timedelta(days=days - 1)).isoformat()
    window = [d for d in ordered if d >= cutoff]
    ys = [statistics.median(by_day[d]) for d in window]
    if len(ys) < 3:
        return statistics.fmean(ys), ordered[-1]
    # Среднее окна отстаёт от тренда: при -0.15 кг/сут база на ~0.5 кг выше сегодняшней.
    # Берём значение линии тренда на последнюю дату (в границах окна, чтобы не улетать).
    last = date.fromisoformat(ordered[-1])
    xs = [(date.fromisoformat(d) - last).days for d in window]
    slope, icpt = statistics.linear_regression(xs, ys)
    return min(max(icpt, min(ys)), max(ys)), ordered[-1]


def _observed_intake(conn: sqlite3.Connection, user_id: int, window_days: int = 14) -> float | None:
    """Средний залогированный приход за окно. None — еды записано слишком мало.

    Порог тот же, что у adaptive_tdee: MIN_LOG_STREAK_DAYS дней подряд, окно
    сужается до серии. Считать по огрызку с пропусками нельзя — среднее
    занижено пропущенными днями.
    """
    # Конец окна - вчера: сегодняшний день ещё не закрыт (частичный лог занижает среднее).
    streak, end = logged_streak(conn, user_id, user_now(conn, user_id).date() - timedelta(days=1))
    if streak < MIN_LOG_STREAK_DAYS:
        return None
    window_days = min(window_days, streak)
    start = end - timedelta(days=window_days - 1)
    rows = conn.execute(
        "SELECT date(fl.eaten_at) d, SUM(fi.kcal) kcal FROM food_log fl "
        "JOIN food_items fi ON fi.food_log_id = fl.id "
        "WHERE fl.user_id=? AND date(fl.eaten_at) BETWEEN ? AND ? GROUP BY 1",
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    # Дни рефида/болезни: intake сознательно повышен до maintenance.
    refeed_sick_dates = {r["date"] for r in conn.execute(
        "SELECT date FROM refeed_days WHERE user_id=? AND date BETWEEN ? AND ? "
        "UNION SELECT date FROM sick_days WHERE user_id=? AND date BETWEEN ? AND ?",
        (user_id, start.isoformat(), end.isoformat(), user_id, start.isoformat(), end.isoformat()),
    ).fetchall()}
    values = [r["kcal"] for r in rows if r["kcal"] is not None and r["d"] not in refeed_sick_dates]
    if not values:
        return None
    return statistics.fmean(values)


def _plateau_note(conn: sqlite3.Connection, user_id: int, weight_kg: float,
                  height_cm: float) -> str | None:
    """Где человек находится относительно медианы времени до плато в SURMOUNT.

    Модель плато не предсказывает — она продолжит снижать вес и за ним. Это
    единственное место, где про плато вообще говорится, поэтому молчать нельзя.
    """
    row = conn.execute(
        "SELECT base_weight_date FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None or not row["base_weight_date"]:
        return None
    started = date.fromisoformat(row["base_weight_date"])
    weeks = (user_now(conn, user_id).date() - started).days / 7.0
    if weeks < 0:
        return None
    bmi = weight_kg / (height_cm / 100) ** 2
    median_weeks = next((w for lo, w in PLATEAU_WEEKS if bmi >= lo), PLATEAU_WEEKS[-1][1])
    note = (f"На программе {weeks:.0f}-я неделя. В SURMOUNT медиана времени до "
            f"плато при таком ИМТ — {median_weeks:.0f} недель "
            f"(Horn et al., Clin Obes, 2025). Модель плато не предсказывает.")
    if weeks >= median_weeks * 0.8:
        note += " Срок близко: прогноз ниже, скорее всего, оптимистичен."
    return note


def _simulate(weight_kg: float, fat_kg: float, height_cm: float, age_years: int,
              sex: str, intake_kcal: float, days: int, activity_factor: float,
              adaptive_pct: float) -> tuple[list[float], int | None]:
    """Посуточная траектория массы и день, на котором модель перестала работать.

    Возвращает (days+1 значений включая старт, индекс обрыва или None).
    После обрыва траектория держит последнее значение — не потому, что вес
    встанет, а потому, что модель дальше не знает. Продолжать считать нельзя:
    при пустом жировом депо правило Форбса срезает одну тощую массу и уводит
    вес в отрицательные числа молча.
    """
    lean_kg = weight_kg - fat_kg
    out = [weight_kg]
    truncated = None
    essential_fat_pct = ESSENTIAL_FAT_PCT_F if sex == "f" else ESSENTIAL_FAT_PCT_M
    for day in range(days):
        if truncated is not None:
            out.append(weight_kg)
            continue

        bmr = bmr_mifflin(weight_kg, height_cm, age_years, sex)
        tdee = bmr * activity_factor * (1.0 - adaptive_pct)
        deficit = tdee - intake_kcal

        # Forbes: доля тощей ткани растёт по мере исчерпания жирового депо.
        lean_share = FORBES_C / (FORBES_C + fat_kg)
        kcal_per_kg = lean_share * KCAL_PER_KG_LEAN + (1 - lean_share) * KCAL_PER_KG_FAT

        delta_kg = deficit / kcal_per_kg
        fat_kg -= delta_kg * (1 - lean_share)
        lean_kg -= delta_kg * lean_share
        weight_kg = fat_kg + lean_kg

        # Неснижаемый жир: ниже него у модели нет ни физиологии, ни данных.
        if fat_kg <= weight_kg * essential_fat_pct:
            truncated = day + 1
        out.append(weight_kg)
    return out, truncated


def project(conn: sqlite3.Connection, user_id: int, horizon_days: int = 84,
            intake_kcal: float | None = None) -> dict:
    """Прогноз массы на горизонт. Отказ вместо догадки, если данных мало.

    intake_kcal задаётся явно — сценарий "что если есть ровно столько".
    Не задан — берётся фактический залогированный приход за 14 дней.
    """
    if horizon_days < 1:
        return {"error": "Горизонт должен быть хотя бы 1 день"}
    if horizon_days > MAX_HORIZON_DAYS:
        return {"error": f"Горизонт больше {MAX_HORIZON_DAYS} дней не считается: "
                         f"точность прогноза на таких сроках не изучена"}

    user = conn.execute(
        "SELECT height_cm, birth_date, sex FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if user is None or not user["height_cm"] or not user["birth_date"]:
        return {"error": "Нет роста или даты рождения — расход посчитать не от чего"}

    sw = smoothed_weight(conn, user_id)
    if sw is None:
        return {"error": "Нет ни одного замера массы"}
    weight_kg, last_date = sw

    today = user_now(conn, user_id).date()
    age_days = (today - date.fromisoformat(last_date)).days
    if age_days > MAX_WEIGHT_AGE_DAYS:
        return {"error": f"Последний замер массы {age_days} дней назад — "
                         f"прогноз строить не от чего, нужно взвеситься"}

    ffm = latest_ffm(conn, user_id)
    if ffm is None:
        return {"error": "Нет замера состава тела: без жировой массы правило "
                         "Форбса не применимо, а без него прогноз — экстраполяция"}
    fat_kg = weight_kg - ffm
    if fat_kg <= 0:
        return {"error": "Жировая масса вышла неположительной — замер состава недостоверен"}

    assumed = "задан вручную"
    if intake_kcal is not None and not (MIN_SCENARIO_KCAL <= intake_kcal <= MAX_SCENARIO_KCAL):
        return {"error": f"Приход {intake_kcal:.0f} ккал вне диапазона "
                         f"{MIN_SCENARIO_KCAL}–{MAX_SCENARIO_KCAL}: такой сценарий "
                         f"модель не считает"}
    if intake_kcal is None:
        intake_kcal = _observed_intake(conn, user_id)
        assumed = "фактический лог, последние дни подряд"
    if intake_kcal is None:
        return {"error": f"Еды записано меньше {MIN_LOG_STREAK_DAYS} дней подряд — среднего прихода нет. "
                         "Либо логируй полнее, либо задай intake_kcal вручную сценарием"}

    cfg = _forecast_cfg()
    activity = _policy().get("activity_factor", 1.20)
    adaptive_pct = float(cfg.get("adaptive_thermogenesis_pct", 0.0))
    unc = float(cfg.get("intake_uncertainty_kcal", DEFAULT_INTAKE_UNCERTAINTY))

    age_years = _age_years(user["birth_date"], today.isoformat())
    sex = user["sex"] or "m"

    def run(intake):
        return _simulate(weight_kg, fat_kg, user["height_cm"], age_years, sex,
                         intake, horizon_days, activity, adaptive_pct)

    mid, truncated = run(intake_kcal)
    # Больше еды -> меньше дефицит -> выше вес: верхняя граница полосы.
    hi_intake, _ = run(min(intake_kcal + unc, MAX_SCENARIO_KCAL))
    lo_intake, _ = run(max(intake_kcal - unc, 0.0))

    traj = []
    for i in range(horizon_days + 1):
        # Два источника ошибки КОМБИНИРУЮТСЯ, а не выбираются по большему.
        # Раньше тут стояло min(lo_intake, mid*(1-err)) — это брало бо́льшую из
        # двух однофакторных границ, и ни одна точка полосы не отвечала на
        # вопрос "а если человек ест меньше И модель ошиблась в ту же сторону".
        # Полоса выходила систематически уже, чем совокупная неопределённость
        # метода. Складываем линейно, а не в квадратуре: для медицинского
        # прогноза ошибка в сторону широкой полосы безопаснее.
        f = i / MODEL_ERR_REF_DAYS
        lo = lo_intake[i] * (1 - MODEL_ERR_LO_PCT * f)
        hi = hi_intake[i] * (1 + MODEL_ERR_HI_PCT * f)
        traj.append({"date": (today + timedelta(days=i)).isoformat(),
                     "lo": round(lo, 1), "mid": round(mid[i], 1), "hi": round(hi, 1)})

    measured_tdee = adaptive_tdee(conn, user_id)
    return {
        "estimate": True,
        "start_weight_kg": round(weight_kg, 1),
        "start_fat_kg": round(fat_kg, 1),
        "intake_kcal": round(intake_kcal),
        "intake_source": assumed,
        "intake_uncertainty_kcal": round(unc),
        "activity_factor": activity,
        "adaptive_thermogenesis_pct": adaptive_pct,
        "measured_tdee": round(measured_tdee) if measured_tdee else None,
        "horizon_days": horizon_days,
        "end": traj[-1],
        "trajectory": traj,
        "truncated_after_days": truncated,
        "plateau": _plateau_note(conn, user_id, weight_kg, user["height_cm"]),
        "caveat": (f"Оценка, не обещание. Полоса — разброс прихода ±{round(unc)} ккал/сут "
                   f"плюс собственная ошибка метода (валидация NIH Body Weight Planner: "
                   f"−6.2%..+3.7% массы на 6 месяцах). Смена дозы препарата или переход "
                   f"в плато делают прогноз недействительным."),
    }


def reach(conn: sqlite3.Connection, user_id: int, target_kg: float,
          intake_kcal: float | None = None) -> dict:
    """Когда масса дойдёт до target_kg. Диапазон дат, не дата.

    Вне горизонта модели — честное "не в пределах N дней", а не дата,
    полученная продолжением кривой за границу применимости.
    """
    p = project(conn, user_id, MAX_HORIZON_DAYS, intake_kcal)
    if "error" in p:
        return p
    if target_kg >= p["start_weight_kg"]:
        return {"error": f"Цель {target_kg} кг не ниже текущих {p['start_weight_kg']} кг"}

    def first_at_or_below(key):
        return next((pt["date"] for pt in p["trajectory"] if pt[key] <= target_kg), None)

    # lo — сценарий меньшего прихода, доходит раньше всех.
    early, mid, late = (first_at_or_below(k) for k in ("lo", "mid", "hi"))
    if mid is None:
        end = p["end"]
        return {
            "estimate": True, "target_kg": target_kg, "reached": False,
            "note": (f"За {MAX_HORIZON_DAYS} дней при текущем приходе модель до "
                     f"{target_kg} кг не доходит: к концу горизонта {end['mid']} кг "
                     f"(полоса {end['lo']}–{end['hi']})."),
            "plateau": p["plateau"], "caveat": p["caveat"],
        }
    return {
        "estimate": True, "target_kg": target_kg, "reached": True,
        "earliest": early, "expected": mid, "latest": late,
        "intake_kcal": p["intake_kcal"], "intake_source": p["intake_source"],
        "plateau": p["plateau"], "caveat": p["caveat"],
    }


def calibrate(
    conn: sqlite3.Connection,
    user_id: int,
    target_kg: float | None = None,
    deadline: str | None = None,
    target_kcal: float | None = None,
    apply: bool = False,
) -> dict:
    """Интерактивный подбор оптимальной калорийности и согласование вехи.

    Режимы:
    1. target_kcal задан (принудительное изменение калорийности):
       - проверка безопасности (пол калорий kcal_floor, темп сброса <= 1.5 кг/нед);
       - пересчет срока достижения target_kg и веса к текущему дедлайну;
       - расчет выровненного срока вехи (aligned_deadline) под требуемый калораж;
       - при apply=True — сохранение нового срока в milestones.
    2. deadline задан (цель и желаемый срок):
       - расчет необходимого калоража под срок;
       - проверка безопасности (если ниже пола — расчет безопасной альтернативы и даты);
       - при apply=True — фиксация вехи.
    3. Только target_kg (подбор без дедлайна):
       - генерация 3 вариантов темпа (комфортный, оптимальный, интенсивный) с БЖУ и сроками.
    """
    from health_core.energy import daily_expenditure, kcal_floor, _active_milestone
    from health_core.config import targets_for

    today = user_now(conn, user_id).date()
    sw = smoothed_weight(conn, user_id)
    if sw is None:
        return {"error": "Нет свежих замеров массы тела за последние 7 дней для расчета"}
    start_weight, _ = sw

    active_ms = _active_milestone(conn, user_id)
    if active_ms and active_ms["metric"] != "weight_kg":
        active_ms = None
    if target_kg is None:
        if active_ms and active_ms["threshold"]:
            target_kg = float(active_ms["threshold"])
            if deadline is None and active_ms["deadline"]:
                deadline = active_ms["deadline"][:10]
        else:
            return {"error": "Не указан целевой вес target_kg и нет активной вехи по весу"}
    else:
        target_kg = float(target_kg)

    if target_kg >= start_weight:
        return {"error": f"Целевой вес {target_kg} кг должен быть ниже текущего ({start_weight:.1f} кг)"}

    exp_dict = daily_expenditure(conn, user_id, today.isoformat())
    full_tdee = exp_dict["kcal"]
    floor, floor_reason = kcal_floor(conn, user_id, full_tdee)
    targets = targets_for(conn, user_id)

    g_cfg = load().get("guards", {})
    max_rate_kg = g_cfg.get("weight_rate_kg_week_max", 1.5)
    max_rate_pct = g_cfg.get("weight_rate_pct_week_max", 1.5)
    safe_rate_kg_week = min(max_rate_kg, start_weight * (max_rate_pct / 100.0))
    max_safe_daily_deficit = (safe_rate_kg_week / 7.0) * 7700.0

    def _calc_macros(kcal_val: float) -> dict:
        protein = targets.get("protein_g")
        fat_pct = targets.get("fat_pct_min") if floor_reason == "macro_minimum" else targets.get("fat_pct", 0.25)
        fat = round(kcal_val * (fat_pct or 0.25) / 9.0, 1) if fat_pct else None
        carbs = None
        if protein is not None and fat is not None:
            carbs = round(max(0.0, kcal_val - protein * 4.0 - fat * 9.0) / 4.0, 1)
        return {"protein_g": protein, "fat_g": fat, "carbs_g": carbs}

    # Режим 1: принудительно заданная калорийность
    if target_kcal is not None:
        target_kcal = float(target_kcal)
        if target_kcal < MIN_SCENARIO_KCAL or target_kcal > MAX_SCENARIO_KCAL:
            return {"error": f"Калорийность {target_kcal} вне допустимого диапазона ({MIN_SCENARIO_KCAL}–{MAX_SCENARIO_KCAL} ккал)"}

        deficit = full_tdee - target_kcal
        is_below_floor = target_kcal < floor
        is_excessive_deficit = deficit > max_safe_daily_deficit
        is_safe = (not is_below_floor) and (not is_excessive_deficit)

        safety_warnings = []
        if is_below_floor:
            safety_warnings.append(
                f"Калорийность {target_kcal:.0f} ккал ниже безопасного пола {floor:.0f} ккал ({floor_reason}). "
                f"Дефицит глубже пола сжигает мышечную ткань. Безопасный минимум: {floor:.0f} ккал."
            )
        if is_excessive_deficit:
            safety_warnings.append(
                f"Дефицит {deficit:.0f} ккал/сут даёт темп сброса быстрее безопасных {safe_rate_kg_week:.2f} кг/нед "
                f"(риск желчнокаменной болезни)."
            )

        fr = reach(conn, user_id, target_kg, intake_kcal=target_kcal)

        cur_deadline = deadline or (active_ms["deadline"][:10] if active_ms and active_ms["deadline"] else None)
        weight_at_cur_deadline = None
        days_to_cur_deadline = None
        if cur_deadline:
            days_to_cur_deadline = (date.fromisoformat(cur_deadline) - today).days
            if 0 < days_to_cur_deadline <= MAX_HORIZON_DAYS:
                proj = project(conn, user_id, days_to_cur_deadline, intake_kcal=target_kcal)
                if "error" not in proj:
                    weight_at_cur_deadline = proj["end"]

        kg_diff = start_weight - target_kg
        # Срок из динамической модели (как forecast_reach); 7700 - только запасной путь.
        aligned_deadline = fr.get("expected")
        if aligned_deadline is None and deficit > 0 and kg_diff > 0:
            aligned_days = max(1, round(kg_diff * 7700.0 / deficit))
            aligned_deadline = (today + timedelta(days=aligned_days)).isoformat()

        applied = False
        applied_message = None
        if apply:
            if not is_safe:
                return {
                    "error": f"Нельзя зафиксировать калорийность {target_kcal:.0f} ккал: " + " ".join(safety_warnings)
                }
            if active_ms:
                conn.execute(
                    "UPDATE milestones SET threshold=?, deadline=? WHERE id=?",
                    (target_kg, aligned_deadline, active_ms["id"])
                )
                conn.commit()
                applied = True
                applied_message = (
                    f"Веха «{active_ms['name']}» обновлена: цель {target_kg} кг, срок {aligned_deadline}. "
                    f"Суточная цель зафиксирована на уровне {target_kcal:.0f} ккал."
                )
            else:
                ms_name = f"{target_kg:.0f} кг"
                conn.execute(
                    "INSERT INTO milestones(user_id, name, metric, threshold, deadline) VALUES (?, ?, 'weight_kg', ?, ?) "
                    "ON CONFLICT(user_id, name) DO UPDATE SET metric='weight_kg', "
                    "threshold=excluded.threshold, deadline=excluded.deadline, achieved_at=NULL",
                    (user_id, ms_name, target_kg, aligned_deadline)
                )
                conn.commit()
                applied = True
                applied_message = (
                    f"Создана веха «{ms_name}» со сроком {aligned_deadline}. "
                    f"Суточная цель зафиксирована на уровне {target_kcal:.0f} ккал."
                )

        return {
            "mode": "force_kcal",
            "start_weight_kg": round(start_weight, 1),
            "target_kg": target_kg,
            "target_kcal": round(target_kcal),
            "daily_expenditure_kcal": round(full_tdee),
            "deficit_kcal": round(deficit),
            "kcal_floor": round(floor),
            "floor_reason": floor_reason,
            "safe": is_safe,
            "safety_warnings": safety_warnings,
            "macros": _calc_macros(target_kcal),
            "forecast_reach": fr,
            "current_milestone": {
                "name": active_ms["name"] if active_ms else None,
                "deadline": cur_deadline,
                "days_remaining": days_to_cur_deadline,
                "weight_at_deadline": weight_at_cur_deadline,
            } if cur_deadline else None,
            "aligned_deadline": aligned_deadline,
            "suggested_action": (
                f"Согласовать срок {aligned_deadline} для вехи {target_kg} кг "
                f"(удерживает цель {target_kcal:.0f} ккал/сут)"
            ) if is_safe else "Скорректировать калорийность до безопасного диапазона",
            "applied": applied,
            "applied_message": applied_message,
        }

    def _calc_options() -> list[dict]:
        opts = []
        comfort_kcal = max(floor, full_tdee - 500.0)
        fr_comfort = reach(conn, user_id, target_kg, intake_kcal=comfort_kcal)
        opts.append({
            "name": "comfort",
            "label": "Комфортный",
            "intake_kcal": round(comfort_kcal),
            "deficit_kcal": round(full_tdee - comfort_kcal),
            "weekly_rate_kg": round((full_tdee - comfort_kcal) * 7.0 / 7700.0, 2),
            "expected_date": fr_comfort.get("expected"),
            "earliest_date": fr_comfort.get("earliest"),
            "macros": _calc_macros(comfort_kcal),
        })

        optimal_kcal = max(floor, full_tdee - 800.0)
        fr_opt = reach(conn, user_id, target_kg, intake_kcal=optimal_kcal)
        opts.append({
            "name": "optimal",
            "label": "Оптимальный",
            "intake_kcal": round(optimal_kcal),
            "deficit_kcal": round(full_tdee - optimal_kcal),
            "weekly_rate_kg": round((full_tdee - optimal_kcal) * 7.0 / 7700.0, 2),
            "expected_date": fr_opt.get("expected"),
            "earliest_date": fr_opt.get("earliest"),
            "macros": _calc_macros(optimal_kcal),
        })

        max_safe_kcal = floor
        fr_max = reach(conn, user_id, target_kg, intake_kcal=max_safe_kcal)
        opts.append({
            "name": "max_safe",
            "label": "Интенсивный (пол калорий)",
            "intake_kcal": round(max_safe_kcal),
            "deficit_kcal": round(full_tdee - max_safe_kcal),
            "weekly_rate_kg": round((full_tdee - max_safe_kcal) * 7.0 / 7700.0, 2),
            "expected_date": fr_max.get("expected"),
            "earliest_date": fr_max.get("earliest"),
            "macros": _calc_macros(max_safe_kcal),
        })
        return opts

    # Режим 2: задан желаемый срок
    if deadline is not None:
        deadline_date = date.fromisoformat(deadline[:10])
        days = (deadline_date - today).days
        if days <= 0:
            return {"error": f"Срок {deadline} уже наступил или в прошлом"}
        if days > MAX_HORIZON_DAYS:
            return {"error": f"Срок {deadline} за горизонтом модели ({MAX_HORIZON_DAYS} дней)"}

        kg_diff = start_weight - target_kg
        required_linear_deficit = (kg_diff * 7700.0) / days
        required_intake = full_tdee - required_linear_deficit

        is_below_floor = required_intake < floor
        is_excessive_deficit = required_linear_deficit > max_safe_daily_deficit
        deadline_safe = (not is_below_floor) and (not is_excessive_deficit)

        fr = reach(conn, user_id, target_kg, intake_kcal=max(MIN_SCENARIO_KCAL, required_intake))

        optimal_safe_intake = max(floor, full_tdee - min(required_linear_deficit, max_safe_daily_deficit))
        if required_intake < floor:
            optimal_safe_intake = max(floor, full_tdee - 750.0)
            if optimal_safe_intake < floor:
                optimal_safe_intake = floor

        opt_fr = reach(conn, user_id, target_kg, intake_kcal=optimal_safe_intake)

        applied = False
        applied_message = None
        if apply:
            if not deadline_safe and "expected" not in opt_fr:
                return {"error": "Срок небезопасен, а при безопасной калорийности цель не достигается в пределах горизонта: веху не фиксирую"}
            chosen_deadline = deadline if deadline_safe else opt_fr["expected"]
            if active_ms:
                conn.execute(
                    "UPDATE milestones SET threshold=?, deadline=? WHERE id=?",
                    (target_kg, chosen_deadline, active_ms["id"])
                )
            else:
                conn.execute(
                    "INSERT INTO milestones(user_id, name, metric, threshold, deadline) VALUES (?, ?, 'weight_kg', ?, ?) "
                    "ON CONFLICT(user_id, name) DO UPDATE SET metric='weight_kg', "
                    "threshold=excluded.threshold, deadline=excluded.deadline, achieved_at=NULL",
                    (user_id, f"{target_kg:.0f} кг", target_kg, chosen_deadline)
                )
            conn.commit()
            applied = True
            applied_message = f"Веха установлена на {chosen_deadline}."

        return {
            "mode": "target_and_deadline",
            "start_weight_kg": round(start_weight, 1),
            "target_kg": target_kg,
            "deadline": deadline,
            "days_remaining": days,
            "daily_expenditure_kcal": round(full_tdee),
            "required_intake_kcal": round(required_intake),
            "required_deficit_kcal": round(required_linear_deficit),
            "kcal_floor": round(floor),
            "floor_reason": floor_reason,
            "deadline_safe": deadline_safe,
            "unreachable_reason": (
                f"Срок требует дефицита {required_linear_deficit:.0f} ккал/сут (калораж {required_intake:.0f} ниже пола {floor:.0f} ккал — сжигание мышц)"
                if is_below_floor else (
                    f"Темп сброса выше безопасного максимума {safe_rate_kg_week:.1f} кг/нед"
                    if is_excessive_deficit else None
                )
            ),
            "forecast_reach": fr,
            "optimal_recommendation": {
                "safe_intake_kcal": round(optimal_safe_intake),
                "macros": _calc_macros(optimal_safe_intake),
                "realistic_earliest": opt_fr.get("earliest"),
                "realistic_expected": opt_fr.get("expected"),
                "realistic_latest": opt_fr.get("latest"),
            },
            "options": _calc_options(),
            "applied": applied,
            "applied_message": applied_message,
        }

    # Режим 3: подбор вариантов без конкретного дедлайна
    return {
        "mode": "options",
        "start_weight_kg": round(start_weight, 1),
        "target_kg": target_kg,
        "daily_expenditure_kcal": round(full_tdee),
        "kcal_floor": round(floor),
        "floor_reason": floor_reason,
        "options": _calc_options(),
        "recommendation": "Выбери подходящий темп или назови желаемую калорийность, и система выставит срок вехи.",
    }


def plateau_forecast(
    conn: sqlite3.Connection,
    user_id: int,
    intake_kcal: float | None = None,
) -> dict:
    """Комплексный прогноз и клинический анализ плато массы тела.

    Включает:
    1. Фармакологический прогноз (SURMOUNT timeline): неделя терапии, медиана времени
       до плато (Horn et al., Clin Obes, 2025), ожидаемая дата замедления.
    2. Метаболическое плато (Hall equilibrium): асимптотический вес W_eq при текущем
       калораже (где TDEE сравняется с приходом) и дата замедления потери до < 100 г/нед.
    3. Текущий статус застоя: истинное плато, псевдоплато (вода при уменьшении талии)
       или нормальное снижение.
    4. Клинические рекомендации по преодолению плато (рефид, консилиум, замеры талии).
    """
    from health_core.energy import daily_expenditure, kcal_floor

    u = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if u is None:
        return {"error": "Пользователь не найден"}
    if not u["height_cm"]:
        return {"error": "Нет роста - расход посчитать не от чего"}

    today = user_now(conn, user_id).date()
    sw = smoothed_weight(conn, user_id)
    if sw is None:
        return {"error": "Нет свежих замеров массы тела за последние 7 дней"}
    curr_weight, _ = sw

    height_cm = u["height_cm"]
    sex = u["sex"] or "m"
    birth_date = u["birth_date"]
    age = _age_years(birth_date, today.isoformat()) if birth_date else 35
    base_weight = u["base_weight_kg"] or curr_weight
    base_date_str = u["base_weight_date"] or u["created_at"][:10]
    started_date = date.fromisoformat(base_date_str[:10])

    # 1. Фармакологическое плато (SURMOUNT)
    days_on_program = max(0, (today - started_date).days)
    weeks_on_program = round(days_on_program / 7.0, 1)
    start_bmi = round(base_weight / ((height_cm / 100.0) ** 2), 1)
    curr_bmi = round(curr_weight / ((height_cm / 100.0) ** 2), 1)
    median_plateau_weeks = next((w for lo, w in PLATEAU_WEEKS if start_bmi >= lo), PLATEAU_WEEKS[-1][1])
    expected_plateau_date = (started_date + timedelta(weeks=median_plateau_weeks)).isoformat()
    weeks_until_plateau = round(median_plateau_weeks - weeks_on_program, 1)

    if weeks_on_program >= median_plateau_weeks:
        pharma_phase = "в фазе плато"
        pharma_desc = (
            f"На программе {weeks_on_program:.0f}-я неделя (медиана SURMOUNT {median_plateau_weeks:.0f} нед). "
            f"Активная фаза снижения на текущей дозе физиологически завершена."
        )
    elif weeks_on_program >= median_plateau_weeks * 0.8:
        pharma_phase = "приближение к плато"
        pharma_desc = (
            f"На программе {weeks_on_program:.0f}-я неделя из {median_plateau_weeks:.0f} нед. "
            f"До медианы плато осталось ~{weeks_until_plateau:.0f} нед (ориентировочно {expected_plateau_date}). "
            f"Темп снижения естественным образом замедляется."
        )
    else:
        pharma_phase = "активное снижение"
        pharma_desc = (
            f"На программе {weeks_on_program:.0f}-я неделя из {median_plateau_weeks:.0f} нед. "
            f"Ожидаемое время активного снижения до плато: ~{weeks_until_plateau:.0f} нед ({expected_plateau_date})."
        )

    # 2. Метаболическое равновесие (Hall dynamic equilibrium)
    exp_dict = daily_expenditure(conn, user_id, today.isoformat())
    full_tdee = exp_dict["kcal"]
    floor, _ = kcal_floor(conn, user_id, full_tdee)

    if intake_kcal is None:
        obs = _observed_intake(conn, user_id)
        if obs is not None:
            intake_kcal = obs
        else:
            t_row = conn.execute(
                "SELECT kcal_target FROM daily_targets WHERE user_id=? AND date=?",
                (user_id, today.isoformat()),
            ).fetchone()
            intake_kcal = t_row["kcal_target"] if t_row and t_row["kcal_target"] else max(floor, full_tdee - 500.0)

    intake_kcal = float(intake_kcal)
    act_factor = _policy().get("activity_factor", 1.20)
    adapt_pct = _forecast_cfg().get("adaptive_thermogenesis_pct", 0.0)
    k = act_factor * (1.0 - adapt_pct)
    c_const = 6.25 * height_cm - 5.0 * age + (5.0 if sex == "m" else -161.0)
    denom = 10.0 * k
    w_eq = round((intake_kcal - k * c_const) / denom, 1)

    proj = project(conn, user_id, MAX_HORIZON_DAYS, intake_kcal=intake_kcal)
    slowdown_date = None
    slowdown_weight = None
    if "trajectory" in proj:
        traj = proj["trajectory"]
        was_falling = False
        for i in range(7, len(traj)):
            weekly_drop = traj[i - 7]["mid"] - traj[i]["mid"]
            if weekly_drop > 0.10:
                was_falling = True
            elif was_falling:
                slowdown_date = traj[i]["date"]
                slowdown_weight = traj[i]["mid"]
                break

    # 3. Текущий статус застоя веса (последние 14–21 день)
    since_21d = (today - timedelta(days=21)).isoformat()
    rows_w = conn.execute(
        "SELECT date(measured_at) d, weight_kg w FROM body_metrics "
        "WHERE user_id=? AND measured_at >= ? AND weight_kg IS NOT NULL ORDER BY measured_at",
        (user_id, since_21d),
    ).fetchall()

    stagnation_verdict = "нормальная динамика"
    stagnation_details = "Застоя массы тела нет."
    weight_spread_10d = None
    waist_change_21d = None

    if len(rows_w) >= 4:
        since_10d = (today - timedelta(days=10)).isoformat()
        rows_10d = [r["w"] for r in rows_w if r["d"] >= since_10d]
        if len(rows_10d) >= 4:
            weight_spread_10d = round(max(rows_10d) - min(rows_10d), 2)

        rows_waist = conn.execute(
            "SELECT measured_on d, value_cm w FROM anthropometry "
            "WHERE user_id=? AND site IN ('талия', 'waist') AND measured_on >= ? AND value_cm IS NOT NULL ORDER BY measured_on",
            (user_id, since_21d[:10]),
        ).fetchall()
        if len(rows_waist) >= 2:
            waist_change_21d = round(rows_waist[-1]["w"] - rows_waist[0]["w"], 1)

        if weight_spread_10d is not None and weight_spread_10d <= 0.5:
            if waist_change_21d is not None and waist_change_21d <= -1.0:
                stagnation_verdict = "псевдоплато (задержка воды)"
                stagnation_details = (
                    f"Вес колеблется в пределах {weight_spread_10d} кг за 10 дней, но талия уменьшилась "
                    f"на {abs(waist_change_21d)} см. Жир сгорает, застой на весах вызван задержкой воды/гликогена."
                )
            elif (len(rows_w) >= 6 and (today - date.fromisoformat(rows_w[0]["d"])).days >= 18
                  and max(r["w"] for r in rows_w) - min(r["w"] for r in rows_w) <= 0.5):
                spread_all = round(max(r["w"] for r in rows_w) - min(r["w"] for r in rows_w), 2)
                stagnation_verdict = "истинное плато"
                stagnation_details = (
                    f"Вес зафиксирован более 18 дней (размах всего {spread_all} кг), объемы не падают. "
                    f"Это метаболическая адаптация: рекомендуется плановый рефид или консилиум."
                )
            else:
                stagnation_verdict = "пауза в весе (< 2 недель)"
                stagnation_details = (
                    f"Вес в узком коридоре {weight_spread_10d} кг за 10 дней. Застой менее 2 недель "
                    f"физиологически нормален и обычно связан с солью, водой или мышечным восстановлением."
                )
        else:
            delta_w = round(rows_w[-1]["w"] - rows_w[0]["w"], 1)
            stagnation_details = f"Динамика за 3 недели: {delta_w:+.1f} кг. Застоя веса не зафиксировано."

    # 4. Рекомендации
    recommendations = []
    if stagnation_verdict == "истинное плато":
        recommendations.append("Плановый рефид (MATADOR 2 недели на уровне TDEE) для нормализации лептина и щитовидной оси.")
        recommendations.append("Консилиум по титрации дозы GLP-1 с врачом (команда /council).")
    elif stagnation_verdict == "псевдоплато (задержка воды)":
        recommendations.append("Продолжать режим: талия уходит, состав тела улучшается, вес сбросит воду скачком.")
    else:
        recommendations.append("Текущий дефицит работает штатно. Поддерживать каденцию взвешиваний и шагов.")

    return {
        "start_weight_kg": round(base_weight, 1),
        "current_weight_kg": round(curr_weight, 1),
        "start_bmi": start_bmi,
        "current_bmi": curr_bmi,
        "pharmacological_plateau": {
            "weeks_on_program": weeks_on_program,
            "median_plateau_weeks": median_plateau_weeks,
            "weeks_remaining": weeks_until_plateau,
            "expected_date": expected_plateau_date,
            "phase": pharma_phase,
            "description": pharma_desc,
        },
        "metabolic_equilibrium": {
            "intake_kcal": round(intake_kcal),
            "equilibrium_weight_kg": w_eq,
            "slowdown_date": slowdown_date,
            "slowdown_weight_kg": slowdown_weight,
            "note": f"При калораже {intake_kcal:.0f} ккал/сут метаболический предел снижения составляет {w_eq:.1f} кг.",
        },
        "current_stagnation": {
            "verdict": stagnation_verdict,
            "details": stagnation_details,
            "weight_spread_10d_kg": weight_spread_10d,
            "waist_change_21d_cm": waist_change_21d,
        },
        "recommendations": recommendations,
    }


# ---------------------------------------------------------------- ПРОВЕРКА

if __name__ == "__main__":
    import os
    import sys
    import tempfile
    from pathlib import Path

    sys.stdout.reconfigure(encoding="utf-8")
    # ignore_cleanup_errors: под Windows sqlite держит -wal и -shm открытыми
    # до сборки мусора, и обычный cleanup падает на PermissionError.
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    os.environ["HEALTH_DB"] = str(Path(tmp.name) / "health.db")
    import health_core.db as db
    db.DB_PATH = Path(os.environ["HEALTH_DB"])
    from health_core.db import connect, migrate

    conn = connect()
    migrate(conn)

    today = local_now().date()
    conn.execute(
        "INSERT INTO users(id, telegram_user_id, height_cm, birth_date, sex, timezone, "
        "base_weight_kg, base_weight_date, created_at) VALUES (1, 111, 185, '1992-08-09', "
        "'m', 'Asia/Yekaterinburg', 158.8, '2026-02-19', '2026-02-19 08:00:00')")
    for i in range(7):
        d = today - timedelta(days=6 - i)
        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg, fat_pct) "
            "VALUES (1, ?, ?, 122.0, 79.6, 34.8)", (f"k{i}", f"{d.isoformat()} 08:00:00"))
    conn.commit()

    p = project(conn, 1, 84, intake_kcal=1400)
    assert "error" not in p, p
    assert p["end"]["mid"] < p["start_weight_kg"], p["end"]
    print(f"OK: дефицит роняет массу {p['start_weight_kg']} -> {p['end']['mid']} кг за 84 дня")

    # Замедление — то, чего линейная экстраполяция не видит.
    t = p["trajectory"]
    first_half = t[0]["mid"] - t[42]["mid"]
    second_half = t[42]["mid"] - t[84]["mid"]
    assert second_half < first_half, (first_half, second_half)
    print(f"OK: потеря замедляется — {first_half:.2f} кг за первые 42 дня против "
          f"{second_half:.2f} за вторые")

    assert p["end"]["lo"] < p["end"]["mid"] < p["end"]["hi"], p["end"]
    print(f"OK: полоса упорядочена {p['end']['lo']} < {p['end']['mid']} < {p['end']['hi']}")

    # Полоса не уже собственной ошибки метода.
    f = 84 / MODEL_ERR_REF_DAYS
    assert p["end"]["lo"] <= p["end"]["mid"] * (1 - MODEL_ERR_LO_PCT * f) + 0.05
    print("OK: полоса не уже известной ошибки метода")

    # Правило Форбса: доля тощей ткани при 42 кг жира.
    share = FORBES_C / (FORBES_C + 42.0)
    assert 0.19 < share < 0.21, share
    print(f"OK: Форбс при 42 кг жира даёт {share*100:.1f}% тощей ткани в потере")

    # Энергоплотность потерянной ткани — рядом с расхожими 7700 ккал/кг.
    dens = share * KCAL_PER_KG_LEAN + (1 - share) * KCAL_PER_KG_FAT
    assert 7500 < dens < 8300, dens
    print(f"OK: плотность потерянной ткани {dens:.0f} ккал/кг (проверка вменяемости)")

    bmr = bmr_mifflin(122.0, 185, _age_years("1992-08-09", today.isoformat()), "m")
    p_flat = project(conn, 1, 30, intake_kcal=bmr * 1.20)
    assert abs(p_flat["end"]["mid"] - 122.0) < 0.15, p_flat["end"]
    print(f"OK: приход на уровне расхода держит массу ({p_flat['end']['mid']} кг за 30 дней)")

    # Поддержание: вес не падал, "замедления" быть не должно.
    pf = plateau_forecast(conn, 1, intake_kcal=bmr * 1.20)
    assert pf["metabolic_equilibrium"]["slowdown_date"] is None, pf["metabolic_equilibrium"]
    print("OK: на поддержании замедление снижения не объявляется")

    p_up = project(conn, 1, 30, intake_kcal=4000)
    assert p_up["end"]["mid"] > 122.0, p_up["end"]
    print(f"OK: профицит поднимает массу до {p_up['end']['mid']} кг")

    assert p["plateau"] and "SURMOUNT" in p["plateau"], p["plateau"]
    print(f"OK: контекст плато отдаётся — {p['plateau'][:58]}...")

    assert "error" in project(conn, 1, MAX_HORIZON_DAYS + 1)
    print(f"OK: горизонт больше {MAX_HORIZON_DAYS} дней отклоняется")

    # Исчерпание жирового депо. Через project() при поле прихода 400 ккал и
    # горизонте 182 дня недостижимо (жир падает лишь до ~12%), поэтому гард
    # проверяется на уровне _simulate из состояния, близкого к пустому депо.
    # До правки lean_kg вычитался вечно и уводил вес в минус молча.
    traj_s, cut = _simulate(80.0, 6.0, 185, 34, "m", 400, 180, 1.20, 0.0)
    assert cut is not None, "обрыв не сработал"
    assert min(traj_s) > 0, f"вес ушёл в ноль или ниже: {min(traj_s)}"
    assert traj_s[-1] == traj_s[cut], "после обрыва траектория обязана держать последнее значение"
    print(f"OK: при исчерпании жира симуляция обрывается на {cut}-й день, "
          f"минимум траектории {min(traj_s):.1f} кг (раньше уходил в минус)")

    # И контрольный факт: в разрешённых пределах гард не срабатывает вовсе —
    # он защита от абсурдного входа, а не рабочий режим.
    _, cut_normal = _simulate(122.0, 42.4, 185, 34, "m", MIN_SCENARIO_KCAL,
                              MAX_HORIZON_DAYS, 1.20, 0.0)
    assert cut_normal is None, cut_normal
    print("OK: в разрешённых пределах прихода и горизонта обрыв не срабатывает")

    # Сценарный приход вне диапазона вменяемости -> отказ, а не обвал кривой.
    for bad in (-500, 0, 50000):
        assert "error" in project(conn, 1, 30, intake_kcal=bad), bad
    print(f"OK: приход вне {MIN_SCENARIO_KCAL}–{MAX_SCENARIO_KCAL} ккал отклоняется")

    # Полоса комбинирует оба источника, а не выбирает больший: она должна быть
    # шире, чем каждый по отдельности.
    pw = project(conn, 1, 84, intake_kcal=1400)
    f84 = 84 / MODEL_ERR_REF_DAYS
    only_model = pw["end"]["mid"] * (1 - MODEL_ERR_LO_PCT * f84)
    assert pw["end"]["lo"] < only_model, (pw["end"]["lo"], only_model)
    print(f"OK: полоса шире каждого источника по отдельности "
          f"({pw['end']['lo']} против {only_model:.1f} от одной лишь ошибки метода)")

    assert "error" in project(conn, 1, 30), "должен отказать без лога еды"
    print("OK: без 11 дней еды из 14 прогноз не выдаётся")

    r = reach(conn, 1, 115.0, intake_kcal=1400)
    assert r["reached"] and r["earliest"] <= r["expected"] <= r["latest"], r
    print(f"OK: 115 кг ожидается {r['expected']} (от {r['earliest']} до {r['latest']})")

    r2 = reach(conn, 1, 60.0, intake_kcal=1400)
    assert r2["reached"] is False and "note" in r2, r2
    print("OK: недостижимая за горизонт цель возвращает отказ, а не выдуманную дату")

    assert "error" in reach(conn, 1, 130.0, intake_kcal=1400)
    print("OK: цель выше текущей массы отклоняется")

    conn.execute("UPDATE body_metrics SET measured_at = '2026-01-01 08:00:00' WHERE user_id=1")
    conn.commit()
    assert "error" in project(conn, 1, 30, intake_kcal=1400)
    print("OK: замер старше недели прогноз не даёт")

    # Окно сглаживания - 7 календарных дней, а не 7 дней с замерами.
    conn.execute("DELETE FROM body_metrics WHERE user_id=1")
    for i, (ago, w) in enumerate([(28, 130.0), (21, 128.0), (14, 126.0), (7, 124.0), (3, 123.0), (0, 122.0)]):
        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg) VALUES (1, ?, ?, ?)",
            (f"w{i}", f"{(today - timedelta(days=ago)).isoformat()} 08:00:00", w))
    conn.commit()
    assert abs(smoothed_weight(conn, 1)[0] - 122.0) < 0.6, smoothed_weight(conn, 1)
    print("OK: сглаженная масса берёт только замеры последних 7 календарных дней")

    conn.close()
    print("\nforecast.py: все проверки прошли")
