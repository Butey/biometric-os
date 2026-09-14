"""Ручной ввод лабораторных анализов + детерминированные клинические формулы.

Зачем: OCR не нужен — бланк с анализами читает человек и диктует текст боту,
а формулы (HOMA-IR, CKD-EPI, non-HDL, eAG, TyG, TG/HDL, AIP, FIB-4) нужны на связке тирзепатид
(инсулинорезистентность) + высокобелковая диета (нагрузка на почки), чтобы не
ждать врача ради арифметики, которую он делает по тем же формулам. Никакой
интерпретации моделью — только справочные формулы из литературы, посчитанные
детерминированно, поэтому модуль не зависит от health_core.llm и не требует
сети.
"""
from datetime import datetime
import math

MARKERS = {
    "glucose": {
        "label": "глюкоза",
        "unit": "ммоль/л",
        "lo": 1.0,
        "hi": 35.0,
        "aliases": ("глюкоза", "сахар", "glucose"),
    },
    "insulin": {
        "label": "инсулин",
        "unit": "мкЕд/мл",
        "lo": 0.5,
        "hi": 300.0,
        "aliases": ("инсулин", "insulin"),
    },
    "hba1c": {
        "label": "гликированный гемоглобин",
        "unit": "%",
        "lo": 3.0,
        "hi": 20.0,
        "aliases": ("гликированный гемоглобин", "гликогемоглобин", "hba1c"),
    },
    "creatinine": {
        "label": "креатинин",
        "unit": "мкмоль/л",
        "lo": 20.0,
        "hi": 1500.0,
        "aliases": ("креатинин", "creatinine"),
    },
    "total_chol": {
        "label": "общий холестерин",
        "unit": "ммоль/л",
        "lo": 1.0,
        "hi": 20.0,
        "aliases": ("холестерин", "общий холестерин", "cholesterol"),
    },
    "hdl": {
        "label": "ЛПВП",
        "unit": "ммоль/л",
        "lo": 0.1,
        "hi": 5.0,
        "aliases": ("лпвп", "hdl"),
    },
    "ldl": {
        "label": "ЛПНП",
        "unit": "ммоль/л",
        "lo": 0.1,
        "hi": 15.0,
        "aliases": ("лпнп", "ldl"),
    },
    "tg": {
        "label": "триглицериды",
        "unit": "ммоль/л",
        "lo": 0.1,
        "hi": 30.0,
        "aliases": ("триглицериды", "tg"),
    },
    "alt": {
        "label": "АЛТ",
        "unit": "Ед/л",
        "lo": 1.0,
        "hi": 2000.0,
        "aliases": ("алт", "alt"),
    },
    "ast": {
        "label": "АСТ",
        "unit": "Ед/л",
        "lo": 1.0,
        "hi": 2000.0,
        "aliases": ("аст", "ast"),
    },
    "ggt": {
        "label": "ГГТ",
        "unit": "Ед/л",
        "lo": 1.0,
        "hi": 2000.0,
        "aliases": ("ггт", "гамма-гт", "ggt"),
    },
    "alp": {
        "label": "щелочная фосфатаза",
        "unit": "Ед/л",
        "lo": 10.0,
        "hi": 2000.0,
        "aliases": ("щф", "щелочная фосфатаза", "alp"),
    },
    "uric_acid": {
        "label": "мочевая кислота",
        "unit": "мкмоль/л",
        "lo": 50.0,
        "hi": 1500.0,
        "aliases": ("мочевая кислота", "uric acid"),
    },
    "tsh": {
        "label": "ТТГ",
        "unit": "мЕд/л",
        "lo": 0.01,
        "hi": 100.0,
        "aliases": ("ттг", "tsh"),
    },
    "ft4": {
        "label": "свободный Т4",
        "unit": "пмоль/л",
        "lo": 1.0,
        "hi": 100.0,
        "aliases": ("свободный т4", "свт4", "ft4"),
    },
    "ft3": {
        "label": "свободный Т3",
        "unit": "пмоль/л",
        "lo": 0.5,
        "hi": 50.0,
        "aliases": ("свободный т3", "свт3", "ft3"),
    },
    "testosterone": {
        "label": "тестостерон общий",
        "unit": "нмоль/л",
        "lo": 0.1,
        "hi": 100.0,
        "aliases": ("тестостерон", "тестостерон общий", "testosterone"),
    },
    "shbg": {
        "label": "ГСПГ",
        "unit": "нмоль/л",
        "lo": 1.0,
        "hi": 300.0,
        "aliases": ("гспг", "shbg"),
    },
    "hematocrit": {
        "label": "гематокрит",
        "unit": "%",
        "lo": 10.0,
        "hi": 75.0,
        "aliases": ("гематокрит", "hct"),
    },
    "apob": {
        "label": "аполипопротеин B",
        "unit": "г/л",
        "lo": 0.1,
        "hi": 5.0,
        "aliases": ("апоb", "апов", "apob"),
    },
    "cystatin_c": {
        "label": "цистатин C",
        "unit": "мг/л",
        "lo": 0.2,
        "hi": 10.0,
        "aliases": ("цистатин c", "цистатин с", "cystatin c"),
    },
    "platelets": {
        "label": "тромбоциты",
        "unit": "10^9/л",
        "lo": 20.0,
        "hi": 2000.0,
        "aliases": ("тромбоциты", "plt", "platelets"),
    },
}

# Обратная карта псевдоним -> канонический ключ. Канонический ключ сам себе
# псевдоним (принимаем "glucose" не только как ключ, но и через тот же путь
# нормализации, что и любой другой ввод).
_ALIAS_TO_CANON = {}
for _canon, _meta in MARKERS.items():
    _ALIAS_TO_CANON[_canon.casefold()] = _canon
    for _alias in _meta["aliases"]:
        _ALIAS_TO_CANON[_alias.casefold()] = _canon


def canon_marker(name):
    """Строка от пользователя -> канонический ключ MARKERS, либо None."""
    if name is None:
        return None
    key = " ".join(str(name).split()).strip().casefold()
    return _ALIAS_TO_CANON.get(key)


def _reject_reason(canon, value, meta):
    lo, hi = meta["lo"], meta["hi"]
    if lo <= value <= hi:
        return None
    unit = meta["unit"]
    # Частая ошибка — перепутанные единицы (мг/дл вместо ммоль/л и наоборот).
    # Не пытаемся угадать точный коэффициент пересчёта, просто подсказываем
    # правильную единицу для самых частых путаниц.
    if canon in ("glucose", "total_chol", "hdl", "ldl", "tg") and value > hi:
        return f"похоже на мг/дл, нужны {unit}"
    if canon in ("creatinine", "uric_acid") and value < lo:
        return f"похоже на мг/дл, нужны {unit}"
    if canon == "apob" and value > hi:
        return f"похоже на мг/дл, нужны {unit}"
    return f"вне допустимого диапазона ({lo}–{hi} {unit})"


def save(conn, user_id, taken_on, values, notes=None):
    """Сохраняет словарь {название: значение}. Возвращает {"saved": ..., "rejected": ...}.

    taken_on обязателен и должен быть YYYY-MM-DD — без этого производные формулы
    не могут сопоставить анализы одного дня. Неизвестные названия и
    неправдоподобные значения не роняют весь вызов, а уходят в rejected с
    причиной на русском, чтобы бот показал их пользователю как есть.
    """
    try:
        datetime.strptime(taken_on, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError(f"taken_on должен быть в формате YYYY-MM-DD, получили: {taken_on!r}")

    saved = {}
    rejected = {}
    for raw_name, raw_value in values.items():
        canon = canon_marker(raw_name)
        if canon is None:
            rejected[raw_name] = "неизвестный показатель"
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            rejected[raw_name] = "значение должно быть числом"
            continue
        meta = MARKERS[canon]
        reason = _reject_reason(canon, value, meta)
        if reason:
            rejected[raw_name] = reason
            continue
        conn.execute(
            """INSERT INTO lab_results(user_id, taken_on, marker, value, unit, notes)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, taken_on, marker)
               DO UPDATE SET value=excluded.value, unit=excluded.unit, notes=excluded.notes""",
            (user_id, taken_on, canon, value, meta["unit"], notes),
        )
        saved[canon] = value
    conn.commit()
    return {"saved": saved, "rejected": rejected}


def history(conn, user_id, limit=30):
    """Последние записи, новые сначала."""
    rows = conn.execute(
        """SELECT id, taken_on, marker, value, unit FROM lab_results
           WHERE user_id=? ORDER BY taken_on DESC, id DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        meta = MARKERS.get(r["marker"], {})
        out.append({
            "id": r["id"],
            "taken_on": r["taken_on"],
            "marker": r["marker"],
            "label": meta.get("label", r["marker"]),
            "value": r["value"],
            "unit": r["unit"],
        })
    return out


def delete(conn, user_id, lab_id):
    cur = conn.execute("DELETE FROM lab_results WHERE id=? AND user_id=?", (lab_id, user_id))
    conn.commit()
    return cur.rowcount > 0


def _latest(conn, user_id, marker):
    return conn.execute(
        """SELECT taken_on, value FROM lab_results
           WHERE user_id=? AND marker=? ORDER BY taken_on DESC, id DESC LIMIT 1""",
        (user_id, marker),
    ).fetchone()


def _latest_pair_day(conn, user_id, marker_a, marker_b):
    """Последняя дата, где ОБА показателя есть одновременно (иначе формула не имеет смысла)."""
    return conn.execute(
        """SELECT a.taken_on AS taken_on, a.value AS va, b.value AS vb
           FROM lab_results a JOIN lab_results b
             ON a.user_id = b.user_id AND a.taken_on = b.taken_on
           WHERE a.user_id=? AND a.marker=? AND b.marker=?
           ORDER BY a.taken_on DESC LIMIT 1""",
        (user_id, marker_a, marker_b),
    ).fetchone()


def _latest_triple_day(conn, user_id, marker_a, marker_b, marker_c):
    """Последняя дата, где ВСЕ ТРИ показателя есть одновременно."""
    return conn.execute(
        """SELECT a.taken_on AS taken_on, a.value AS va, b.value AS vb, c.value AS vc
           FROM lab_results a
             JOIN lab_results b ON a.user_id = b.user_id AND a.taken_on = b.taken_on
             JOIN lab_results c ON a.user_id = c.user_id AND a.taken_on = c.taken_on
           WHERE a.user_id=? AND a.marker=? AND b.marker=? AND c.marker=?
           ORDER BY a.taken_on DESC LIMIT 1""",
        (user_id, marker_a, marker_b, marker_c),
    ).fetchone()


def _age_years(birth_date, on_date):
    b = datetime.strptime(birth_date, "%Y-%m-%d").date()
    d = datetime.strptime(on_date, "%Y-%m-%d").date()
    return d.year - b.year - ((d.month, d.day) < (b.month, b.day))


_KDIGO = (
    (90.0, "G1", "норма или высокая"),
    (60.0, "G2", "лёгкое снижение"),
    (45.0, "G3a", "умеренное снижение"),
    (30.0, "G3b", "умеренно-тяжёлое снижение"),
    (15.0, "G4", "тяжёлое снижение"),
    (0.0, "G5", "почечная недостаточность"),
)


def _kdigo(egfr_val):
    for threshold, stage, desc in _KDIGO:
        if egfr_val >= threshold:
            return stage, desc
    return "G5", "почечная недостаточность"


def derived(conn, user_id):
    """Производные показатели. Ключ отсутствует, если не хватает входных данных —
    никаких догадок и подстановки средних значений."""
    out = {}

    pair = _latest_pair_day(conn, user_id, "glucose", "insulin")
    if pair is not None:
        homa = pair["va"] * pair["vb"] / 22.5
        interpretation = (
            "повышен — вероятна инсулинорезистентность" if homa >= 2.7 else "в пределах нормы"
        )
        out["homa_ir"] = {
            "value": round(homa, 2),
            "taken_on": pair["taken_on"],
            "inputs": {"glucose": pair["va"], "insulin": pair["vb"]},
            "interpretation": interpretation,
        }

    creat = _latest(conn, user_id, "creatinine")
    if creat is not None:
        user = conn.execute(
            "SELECT birth_date, sex FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if user is not None and user["birth_date"] and user["sex"] in ("m", "f"):
            age = _age_years(user["birth_date"], creat["taken_on"])
            sex = user["sex"]
            scr = creat["value"] / 88.4  # мкмоль/л -> мг/дл
            k = 0.7 if sex == "f" else 0.9
            a = -0.241 if sex == "f" else -0.302
            egfr_val = (
                142
                * min(scr / k, 1) ** a
                * max(scr / k, 1) ** (-1.200)
                * (0.9938 ** age)
            )
            if sex == "f":
                egfr_val *= 1.012
            stage, desc = _kdigo(egfr_val)
            out["egfr"] = {
                "value": round(egfr_val, 1),
                "taken_on": creat["taken_on"],
                "inputs": {"creatinine_umol_l": creat["value"], "age": age, "sex": sex},
                "interpretation": f"KDIGO {stage} — {desc}",
            }

    nonhdl = _latest_pair_day(conn, user_id, "total_chol", "hdl")
    if nonhdl is not None:
        val = nonhdl["va"] - nonhdl["vb"]
        interpretation = (
            "выше типичного ориентира (< 3.4 ммоль/л при высоком риске)"
            if val >= 3.4
            else "в пределах типичного ориентира"
        )
        out["non_hdl"] = {
            "value": round(val, 2),
            "taken_on": nonhdl["taken_on"],
            "inputs": {"total_chol": nonhdl["va"], "hdl": nonhdl["vb"]},
            "interpretation": interpretation,
        }

    hba1c = _latest(conn, user_id, "hba1c")
    if hba1c is not None:
        eag = 1.59 * hba1c["value"] - 2.59
        out["eag_mmol"] = {
            "value": round(eag, 2),
            "taken_on": hba1c["taken_on"],
            "inputs": {"hba1c": hba1c["value"]},
            "interpretation": "среднее содержание глюкозы за последние ~3 месяца",
        }

    # TyG = ln(ТГ_мг/дл × глюкоза_мг/дл / 2)
    # Пересчёт из ммоль/л: ТГ×88.57, глюкоза×18.0182
    tg_glucose = _latest_pair_day(conn, user_id, "tg", "glucose")
    if tg_glucose is not None:
        tg_mg_dl = tg_glucose["va"] * 88.57
        glucose_mg_dl = tg_glucose["vb"] * 18.0182
        tyg_val = math.log((tg_mg_dl * glucose_mg_dl) / 2)
        interpretation = (
            "высокий риск" if tyg_val >= 8.8
            else "повышенный риск" if tyg_val >= 8.5
            else "норма"
        )
        out["tyg"] = {
            "value": round(tyg_val, 2),
            "taken_on": tg_glucose["taken_on"],
            "inputs": {"tg_mmol_l": tg_glucose["va"], "glucose_mmol_l": tg_glucose["vb"]},
            "interpretation": interpretation,
        }

    # TG/HDL (ммоль/л)
    tg_hdl = _latest_pair_day(conn, user_id, "tg", "hdl")
    if tg_hdl is not None:
        tg_hdl_val = tg_hdl["va"] / tg_hdl["vb"]
        interpretation = (
            "высокий риск" if tg_hdl_val > 2.0
            else "норма" if tg_hdl_val < 1.3
            else "средний риск"
        )
        out["tg_hdl"] = {
            "value": round(tg_hdl_val, 2),
            "taken_on": tg_hdl["taken_on"],
            "inputs": {"tg_mmol_l": tg_hdl["va"], "hdl_mmol_l": tg_hdl["vb"]},
            "interpretation": interpretation,
        }

    # AIP = log10(ТГ/ЛПВП) в ммоль/л
    aip_pair = _latest_pair_day(conn, user_id, "tg", "hdl")
    if aip_pair is not None:
        if aip_pair["vb"] > 0:
            aip_val = math.log10(aip_pair["va"] / aip_pair["vb"])
            interpretation = (
                "высокий риск" if aip_val > 0.21
                else "средний риск" if aip_val >= 0.11
                else "низкий риск"
            )
            out["aip"] = {
                "value": round(aip_val, 2),
                "taken_on": aip_pair["taken_on"],
                "inputs": {"tg_mmol_l": aip_pair["va"], "hdl_mmol_l": aip_pair["vb"]},
                "interpretation": interpretation,
            }

    # FIB-4 = (возраст × АСТ) / (тромбоциты × √АЛТ)
    fib4_triple = _latest_triple_day(conn, user_id, "ast", "alt", "platelets")
    if fib4_triple is not None and fib4_triple["vc"] > 0:  # platelets не должны быть нулём
        user = conn.execute(
            "SELECT birth_date FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if user is not None and user["birth_date"]:
            age = _age_years(user["birth_date"], fib4_triple["taken_on"])
            ast = fib4_triple["va"]
            alt = fib4_triple["vb"]
            platelets = fib4_triple["vc"]
            fib4_val = (age * ast) / (platelets * math.sqrt(alt)) if alt > 0 else None
            if fib4_val is not None:
                if age < 35 or age > 65:
                    accuracy_note = " (низкая точность < 35 или > 65 лет)"
                else:
                    accuracy_note = ""
                interpretation = (
                    "высокий риск фиброза" if fib4_val > 2.67
                    else "неопределённый риск" if fib4_val >= 1.3
                    else "низкий риск фиброза"
                ) + accuracy_note
                out["fib4"] = {
                    "value": round(fib4_val, 2),
                    "taken_on": fib4_triple["taken_on"],
                    "inputs": {"age": age, "ast": ast, "alt": alt, "platelets": platelets},
                    "interpretation": interpretation,
                }

    return out


if __name__ == "__main__":
    import sqlite3
    import health_core.db as db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.DDL)
    conn.execute(
        "INSERT INTO users(telegram_user_id, created_at) VALUES (1, '2026-08-20 00:00:00')"
    )
    uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]

    # --- aliases ---
    assert canon_marker("ЛПВП") == "hdl"
    assert canon_marker("HbA1c") == "hba1c"
    assert canon_marker("  Глюкоза  ") == "glucose"
    assert canon_marker("совершенно неизвестное имя") is None

    # --- save: unknown name + out-of-range glucose rejected ---
    res = save(conn, uid, "2026-09-01", {
        "неизвестный маркер": 1.0,
        "glucose": 95,  # похоже на мг/дл, не ммоль/л
    })
    assert "неизвестный маркер" in res["rejected"]
    assert "glucose" in res["rejected"]
    assert "glucose" not in res["saved"]
    assert res["rejected"]["glucose"] == "похоже на мг/дл, нужны ммоль/л"

    # --- upsert: same user/date/marker keeps ONE row with the new value ---
    save(conn, uid, "2026-09-01", {"glucose": 5.0})
    save(conn, uid, "2026-09-01", {"glucose": 5.4})
    rows = conn.execute(
        "SELECT COUNT(*) c FROM lab_results WHERE user_id=? AND taken_on=? AND marker='glucose'",
        (uid, "2026-09-01"),
    ).fetchone()["c"]
    assert rows == 1, f"expected 1 row after upsert, got {rows}"
    val = conn.execute(
        "SELECT value FROM lab_results WHERE user_id=? AND taken_on=? AND marker='glucose'",
        (uid, "2026-09-01"),
    ).fetchone()["value"]
    assert val == 5.4

    # --- HOMA-IR: glucose 5.0 x insulin 10 / 22.5 = 2.2222 -> >= 2.7? нет, "в пределах нормы" ---
    save(conn, uid, "2026-09-02", {"glucose": 5.0, "insulin": 10})
    d = derived(conn, uid)
    homa_expected = 5.0 * 10 / 22.5  # 2.2222...
    assert abs(d["homa_ir"]["value"] - 2.22) < 0.02, d["homa_ir"]
    assert d["homa_ir"]["interpretation"] == "в пределах нормы"

    # HOMA-IR над порогом инсулинорезистентности
    save(conn, uid, "2026-09-03", {"glucose": 7.0, "insulin": 20})
    d = derived(conn, uid)
    homa2 = 7.0 * 20 / 22.5  # 6.222 -> повышен
    assert d["homa_ir"]["taken_on"] == "2026-09-03"
    assert d["homa_ir"]["interpretation"] == "повышен — вероятна инсулинорезистентность"

    # --- non-HDL: 5.2 - 1.3 = 3.9 ---
    save(conn, uid, "2026-09-04", {"total_chol": 5.2, "hdl": 1.3})
    d = derived(conn, uid)
    assert abs(d["non_hdl"]["value"] - 3.9) < 0.02, d["non_hdl"]

    # --- eAG: HbA1c 6.0 -> 1.59*6.0 - 2.59 = 6.95 ---
    save(conn, uid, "2026-09-05", {"hba1c": 6.0})
    d = derived(conn, uid)
    assert abs(d["eag_mmol"]["value"] - 6.95) < 0.02, d["eag_mmol"]

    # --- derived() без sex/birth_date -> нет egfr ---
    save(conn, uid, "2026-09-06", {"creatinine": 88.4})
    d = derived(conn, uid)
    assert "egfr" not in d, "sex/birth_date не заданы, eGFR не должен считаться"

    # --- eGFR мужчина, 34 года, креатинин 88.4 мкмоль/л (=1.0 мг/дл) ---
    # Scr/k = 1.0/0.9 = 1.1111; min(.,1)=1; max(.,1)=1.1111
    # eGFR = 142 * 1^-0.302 * 1.1111^-1.2 * 0.9938^34 ≈ 101.28 (без ×1.012, это мужчина)
    conn.execute(
        "INSERT INTO users(telegram_user_id, birth_date, sex, created_at) VALUES (2, '1992-09-01', 'm', '2026-08-20 00:00:00')"
    )
    uid_m = conn.execute("SELECT id FROM users WHERE telegram_user_id=2").fetchone()["id"]
    save(conn, uid_m, "2026-09-01", {"creatinine": 88.4})
    d = derived(conn, uid_m)
    assert abs(d["egfr"]["value"] - 101.28) < 0.5, d["egfr"]
    assert d["egfr"]["inputs"]["age"] == 34
    assert d["egfr"]["interpretation"].startswith("KDIGO G1")

    # --- eGFR женщина, 60 лет, креатинин 53.04 мкмоль/л (=0.6 мг/дл) ---
    # Scr/k = 0.6/0.7 = 0.8571; min(.,1)=0.8571; max(.,1)=1
    # eGFR = 142 * 0.8571^-0.241 * 1^-1.2 * 0.9938^60 * 1.012 ≈ 102.69
    conn.execute(
        "INSERT INTO users(telegram_user_id, birth_date, sex, created_at) VALUES (3, '1966-09-01', 'f', '2026-08-20 00:00:00')"
    )
    uid_f = conn.execute("SELECT id FROM users WHERE telegram_user_id=3").fetchone()["id"]
    save(conn, uid_f, "2026-09-01", {"creatinine": 53.04})
    d = derived(conn, uid_f)
    assert abs(d["egfr"]["value"] - 102.69) < 0.5, d["egfr"]
    assert d["egfr"]["inputs"]["age"] == 60

    # --- history / delete ---
    hist = history(conn, uid, limit=5)
    assert hist[0]["taken_on"] == "2026-09-06"  # newest first
    assert hist[0]["marker"] == "creatinine"
    assert hist[0]["label"] == "креатинин"

    lab_id = hist[0]["id"]
    assert delete(conn, uid, lab_id) is True
    assert delete(conn, uid, lab_id) is False  # уже удалено

    # --- new markers: GGT, ALP, uric_acid, TSH, FT4, FT3, testosterone, SHBG, hematocrit, ApoB, cystatin_c, platelets ---
    assert canon_marker("ГГТ") == "ggt"
    assert canon_marker("Цистатин С") == "cystatin_c"  # Cyrillic С
    assert canon_marker("цистатин с") == "cystatin_c"  # Cyrillic с, lowercase
    assert canon_marker("тромбоциты") == "platelets"
    assert canon_marker("plt") == "platelets"

    # hematocrit 45 -> saved
    res = save(conn, uid, "2026-09-07", {"hematocrit": 45})
    assert "hematocrit" in res["saved"], f"hematocrit 45 should be saved: {res}"
    assert res["saved"]["hematocrit"] == 45

    # hematocrit 450 -> rejected
    res = save(conn, uid, "2026-09-08", {"hematocrit": 450})
    assert "hematocrit" in res["rejected"], f"hematocrit 450 should be rejected: {res}"

    # uric_acid 6 -> rejected with мг/дл hint (value < lo of 50)
    res = save(conn, uid, "2026-09-09", {"uric_acid": 6})
    assert "uric_acid" in res["rejected"], f"uric_acid 6 should be rejected: {res}"
    assert "мг/дл" in res["rejected"]["uric_acid"], f"expected мг/дл hint: {res['rejected']['uric_acid']}"

    # --- platelets marker save/reject ---
    res = save(conn, uid, "2026-09-10", {"platelets": 200})
    assert "platelets" in res["saved"], f"platelets 200 should be saved: {res}"

    # --- TyG: ТГ 1.7 ммоль/л, глюкоза 5.5 ммоль/л
    # ТГ мг/дл = 1.7 × 88.57 = 150.569
    # Глюкоза мг/дл = 5.5 × 18.0182 = 99.1
    # TyG = ln((150.569 × 99.1) / 2) = ln(7463.27) ≈ 8.92 (но это быстро, проверим)
    # На самом деле: ln(1.7*88.57*5.5*18.0182/2) = ln(7463.27) ≈ 8.918
    save(conn, uid, "2026-09-11", {"tg": 1.7, "glucose": 5.5})
    d = derived(conn, uid)
    # TyG должен быть примерно ln(1.7*88.57*5.5*18.0182/2) ≈ 8.918
    assert "tyg" in d, f"TyG should be calculated: {d.keys()}"
    assert d["tyg"]["value"] > 8.5, f"TyG value should be > 8.5: {d['tyg']['value']}"

    # --- TG/HDL: ТГ 1.8 ммоль/л, ЛПВП 1.2 ммоль/л -> 1.5 ---
    save(conn, uid, "2026-09-12", {"tg": 1.8, "hdl": 1.2})
    d = derived(conn, uid)
    assert "tg_hdl" in d, f"TG/HDL should be calculated: {d.keys()}"
    assert abs(d["tg_hdl"]["value"] - 1.5) < 0.01, f"TG/HDL should be ~1.5: {d['tg_hdl']['value']}"

    # --- AIP: log10(1.8/1.2) = log10(1.5) ≈ 0.176 ---
    d = derived(conn, uid)
    assert "aip" in d, f"AIP should be calculated: {d.keys()}"
    assert abs(d["aip"]["value"] - 0.176) < 0.01, f"AIP should be ~0.176: {d['aip']['value']}"

    # --- FIB-4: возраст 45, АСТ 35, АЛТ 40, тромбоциты 200
    # FIB-4 = (45 × 35) / (200 × √40) ≈ 1575 / (200 × 6.32) ≈ 1.245
    uid_fib4_user = conn.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]
    conn.execute(
        "UPDATE users SET birth_date='1981-09-14' WHERE id=?",
        (uid_fib4_user,)
    )
    save(conn, uid_fib4_user, "2026-09-13", {"ast": 35, "alt": 40, "platelets": 200})
    d = derived(conn, uid_fib4_user)
    assert "fib4" in d, f"FIB-4 should be calculated: {d.keys()}"
    assert abs(d["fib4"]["value"] - 1.24) < 0.1, f"FIB-4 should be ~1.24: {d['fib4']['value']}"

    conn.close()
    print("OK: labs.py — aliases, save/reject/upsert, HOMA-IR, eGFR (м/ж), non-HDL, eAG, TyG, TG/HDL, AIP, FIB-4, history/delete, new markers")
