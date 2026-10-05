"""Варианты приёма пищи из запасов (CONTEXT.md «Запас», «Мой продукт»): белок +
овощи + при нехватке калорий гарнир, граммовки и итоги считает код. Модель
только оформляет готовые варианты и ничего не пересчитывает."""
import sqlite3

from health_core import foods
from health_core.chrono import meal_slot
from health_core.config import user_now

_UNIT_G = {"г": 1, "гр": 1, "g": 1, "кг": 1000, "kg": 1000, "мл": 1, "ml": 1, "л": 1000, "l": 1000}
_SHARE = {"breakfast": 0.30, "lunch": 0.35, "dinner": 0.25, "snack": 0.10}
_PROTEIN_CATS = ("Белковые", "Молочка/Сыры")


# Вес одной штуки/банки, когда запас записан в штуках: ключевые слова названия -> граммы.
# Это допущение, оно помечается в варианте (assumed_weight), пока человек не скажет точнее.
# Консервы рыбные: банка 185 г нетто, съедобное без жидкости ~140 г (замер на тунце). Планируем
# по съедобному, как для круп и макарон считают сухой/готовый вес; точный вес банки человек
# сохраняет сам (food_lookup piece), и он главнее этой таблицы.
_PIECE_G = (("яйц", 60), ("яиц", 60), ("тунец", 140), ("тунц", 140), ("макрел", 140),
            ("горбуш", 140), ("язь", 140), ("язя", 140), ("сардин", 140),
            ("шпрот", 140), ("скумбр", 140), ("сельд", 140))


def grams_per_unit(unit, name="", piece_g=None):
    """(граммов в одной единице запаса, предположен ли вес). Масса/объём - точно;
    штуки и банки - по таблице типовых весов, иначе (None, False)."""
    if piece_g:
        return piece_g, False
    k = _UNIT_G.get((unit or "").strip().lower().rstrip("."))
    if k:
        return k, False
    low = name.lower()
    if (unit or "").strip().lower() in ("шт", "банка", "банки", "банок", "уп", ""):
        for key, g in _PIECE_G:
            if key in low:
                return g, True
    return None, False


def _grams(qty, unit, name="", piece_g=None):
    """(граммы всего запаса, предположен ли вес) или (None, False)."""
    g, assumed = grams_per_unit(unit, name, piece_g)
    return (qty * g, assumed) if qty and g else (None, False)


def _round5(x: float) -> int:
    return int(round(x / 5.0) * 5)


def _line(item: dict, grams: int) -> dict:
    f = grams / 100.0
    return {"name": item["name"], "grams": grams, **({"assumed_weight": True} if item["assumed"] else {}), "kcal": round(item["kcal"] * f),
            "protein_g": round(item["protein"] * f, 1), "fat_g": round(item["fat"] * f, 1),
            "carbs_g": round(item["carbs"] * f, 1)}


def build(conn: sqlite3.Connection, user_id: int, slot: str | None = None, n: int = 3) -> dict:
    from health_core.report import day_summary
    now = user_now(conn, user_id)
    slot = slot if slot in _SHARE else meal_slot(conn, user_id, now)
    rows = conn.execute("SELECT name, qty, unit, category, piece_g FROM pantry WHERE user_id=? ORDER BY name", (user_id,)).fetchall()
    if not rows:
        return {"slot": slot, "options": [], "note": "Запасы пусты. Запиши покупки (фото чека добавит их само)."}

    items, no_macros, uncounted = [], [], []
    for r in rows:
        grams, assumed = _grams(r["qty"], r["unit"], r["name"], r["piece_g"])
        if grams is None:
            uncounted.append(f"{r['name']} ({r['qty'] if r['qty'] is not None else '?'} {r['unit'] or ''})".strip())
            continue
        p = foods.find_mine(conn, user_id, r["name"])
        if p is None or not p["kcal_100g"]:
            no_macros.append(r["name"])
            continue
        items.append({"name": r["name"], "assumed": assumed, "cat": r["category"] or "Прочее", "qty_g": grams, "kcal": p["kcal_100g"],
                      "protein": p["protein_100g"] or 0, "fat": p["fat_100g"] or 0, "carbs": p["carbs_100g"] or 0})

    day = day_summary(conn, user_id, now.strftime("%Y-%m-%d"))
    share = _SHARE[slot]
    kcal_t, prot_t = day["kcal_target"], day["protein_g_target"]
    if not kcal_t or not prot_t:
        return {"slot": slot, "options": [], "note": "Нет цели дня: нужен хотя бы один замер веса."}
    kcal_goal = max(150.0, min(kcal_t * share, kcal_t - day["kcal_eaten"]))
    prot_goal = max(15.0, min(prot_t * share, prot_t - day["protein_g"]))

    prot = sorted((i for i in items if i["cat"] in _PROTEIN_CATS and i["protein"] >= 8), key=lambda i: -i["protein"])
    veg = [i for i in items if i["cat"] == "Овощи/Фрукты"]
    carb = [i for i in items if i["cat"] == "Сложные углеводы"]
    out = {"slot": slot, "target": {"kcal": round(kcal_goal), "protein_g": round(prot_goal)},
           "options": [], "no_macros": no_macros, "uncounted": uncounted,
           "note": "Граммовки круп и макарон - по сухому весу. Овощи и мясо - как в запасе (сырые, если не сказано иное)."}
    if not prot:
        out["note"] += " Белка с известным составом в запасах нет: докупи белковый продукт или внеси цифры (food_lookup remember)."
        return out

    for idx, p in enumerate(prot[:max(1, min(n, 5))]):
        g = max(min(80, p["qty_g"]), min(p["qty_g"], _round5(prot_goal * 0.8 / (p["protein"] / 100.0))))
        g = min(g, 300)
        lines = [_line(p, _round5(g))]
        if veg:
            v = veg[idx % len(veg)]
            lines.append(_line(v, _round5(min(v["qty_g"], 200))))
        short = kcal_goal - sum(x["kcal"] for x in lines)
        if carb and short > 60:
            c = carb[idx % len(carb)]
            cg = _round5(min(c["qty_g"], 120, short / (c["kcal"] / 100.0)))
            if cg >= 20:
                lines.append(_line(c, cg))
        total = {k: round(sum(x[k] for x in lines), 1) for k in ("kcal", "protein_g", "fat_g", "carbs_g")}
        out["options"].append({"items": lines, "total": total})
    return out


if __name__ == "__main__":
    import os
    import tempfile
    from pathlib import Path

    os.environ["HEALTH_DB"] = str(Path(tempfile.mkdtemp()) / "health.db")
    from health_core.db import connect, migrate
    conn = connect()
    migrate(conn)
    conn.execute("INSERT INTO users(telegram_user_id, created_at, height_cm, sex, birth_date) VALUES (1, '2026-08-20 00:00:00', 180, 'm', '1985-01-01')")
    uid = conn.execute("SELECT id FROM users").fetchone()["id"]
    assert build(conn, uid)["options"] == [], "пустой запас - пустые варианты"
    conn.execute("INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg) VALUES (?, 'b', '2026-10-01 08:00:00', 100)", (uid,))
    for name, qty, unit, cat in [("Куриное филе", 500, "г", "Белковые"), ("Творог Анфискино 5%", 0.4, "кг", "Молочка/Сыры"),
                                 ("Кабачок", 300, "г", "Овощи/Фрукты"), ("Греча", 1, "кг", "Сложные углеводы"),
                                 ("Тунец рубленый", 2, "шт", "Белковые"), ("Неизвестный сыр", 200, "г", "Молочка/Сыры")]:
        conn.execute("INSERT INTO pantry(user_id, name, qty, unit, category) VALUES (?,?,?,?,?)", (uid, name, qty, unit, cat))
    for name, k, p, f, c in [("Куриное филе", 113, 23, 2, 0), ("Творог 5%", 121, 16, 5, 3), ("Кабачок", 24, 0.6, 0.3, 4.6),
                             ("Греча сухая", 329, 12.6, 3.3, 62)]:
        foods.remember(conn, uid, name, source="label", kcal_100g=k, protein_100g=p, fat_100g=f, carbs_100g=c)
    out = build(conn, uid, "breakfast")
    assert out["options"], out
    first = out["options"][0]
    assert first["items"][0]["name"] == "Творог Анфискино 5%" or first["items"][0]["name"] == "Куриное филе"
    assert abs(first["total"]["kcal"] - sum(i["kcal"] for i in first["items"])) < 1, "итог считает код"
    assert all(i["grams"] <= 500 for o in out["options"] for i in o["items"])
    assert "Неизвестный сыр" in out["no_macros"] and True, out
    assert _grams(10, "шт", "Яйца СВ XXL") == (600, True) and _grams(2, "кг", "Греча") == (2000, False)
    assert _grams(1, "шт", "Хлеб бородинский") == (None, False), "неизвестную штуку не угадываем"
    print("OK: meal_options - варианты из запасов с граммовками, без цифр и без веса позиции вынесены отдельно")
    conn.close()
