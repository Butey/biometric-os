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

from health_core.config import latest_ffm, load, local_now
from health_core.energy import adaptive_tdee, bmr_mifflin, _age_years

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
    вставаниями), затем среднее по дням. Одиночный замер как отправная точка
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
    window = ordered[-days:]
    value = statistics.fmean(statistics.median(by_day[d]) for d in window)
    return value, ordered[-1]


def _observed_intake(conn: sqlite3.Connection, user_id: int, window_days: int = 14) -> float | None:
    """Средний залогированный приход за окно. None — еды записано слишком мало.

    Порог тот же, что у adaptive_tdee (11 дней из 14): ниже него среднее
    считается по огрызку и занижено пропущенными днями.
    """
    end = local_now().date()
    start = end - timedelta(days=window_days - 1)
    rows = conn.execute(
        "SELECT date(fl.eaten_at) d, SUM(fi.kcal) kcal FROM food_log fl "
        "JOIN food_items fi ON fi.food_log_id = fl.id "
        "WHERE fl.user_id=? AND date(fl.eaten_at) BETWEEN ? AND ? GROUP BY 1",
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    values = [r["kcal"] for r in rows if r["kcal"] is not None]
    if len(values) < -(-window_days * 11 // 14):
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
    weeks = (local_now().date() - started).days / 7.0
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

    today = local_now().date()
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
        assumed = "фактический лог за 14 дней"
    if intake_kcal is None:
        return {"error": "Еды записано меньше 11 дней из 14 — среднего прихода нет. "
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

    conn.close()
    print("\nforecast.py: все проверки прошли")
