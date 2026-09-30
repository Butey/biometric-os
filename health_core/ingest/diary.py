"""Импорт дневников питания из Nutrition/ГГГГ-ММ-ДД.md.

Дневник ведётся человеком в markdown: заголовок приёма с временем, таблица
позиций с КБЖУ, отдельный блок гидратации. Разбор регулярками — формат
рукописный и поедет, поэтому любая непонятная строка пропускается, а не роняет
импорт: потерять один продукт лучше, чем не загрузить день целиком.

Строки «ИТОГО» и воду внутри таблиц еды пропускаем намеренно — иначе день
удвоится по калориям, а вода посчитается дважды.
"""
import re
import sqlite3
from pathlib import Path

DIARY_DIR = Path(__file__).resolve().parent.parent.parent / "Nutrition"
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_MEAL_RE = re.compile(r"^#{1,4}\s*\*{0,2}[^\w\n]*([А-Яа-яЁё\s]+?)\s*\((\d{1,2}:\d{2})\)", re.M)
_WATER_RE = re.compile(r"\*\*(\d{1,2}:\d{2})\*\*\s*[—–-]\s*[^\n|]*?(\d+)\s*мл")
_SLOTS = {"перекус": "snack", "завтрак": "breakfast", "обед": "lunch", "ужин": "dinner"}


def _clean(cell: str) -> str:
    """Снять markdown-экранирование и разметку: «\~215 г» -> «215 г»."""
    return re.sub(r"\s+", " ", re.sub(r"[\*_]", "", cell)).strip()


def _num(cell: str):
    m = re.search(r"(\d+(?:[.,]\d+)?)", _clean(cell))
    return float(m.group(1).replace(",", ".")) if m else None


def _grams(cell: str):
    """Вес позиции. Скобочное «(~215 г)» точнее, чем «3 шт.» перед ним."""
    c = _clean(cell)
    m = re.search(r"\((?:~)?\s*(\d+(?:[.,]\d+)?)\s*г\)", c) or re.search(r"(\d+(?:[.,]\d+)?)\s*г\b", c)
    return float(m.group(1).replace(",", ".")) if m else None


def parse(path: Path) -> dict | None:
    dm = _DATE_RE.search(path.name)
    if dm is None:
        return None
    text = path.read_text(encoding="utf-8")
    date = dm.group(1)

    heads = list(_MEAL_RE.finditer(text))
    meals = []
    for i, h in enumerate(heads):
        name = h.group(1).strip().lower()
        slot = next((v for k, v in _SLOTS.items() if k in name), "snack")
        chunk = text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        items = []
        for row in re.findall(r"^\|(.+)\|\s*$", chunk, re.M):
            cells = [c.strip() for c in row.split("|")]
            if len(cells) < 6:
                continue
            label = _clean(cells[0])
            # шапка таблицы, разделитель, подытог и вода внутри еды — не позиции
            if (not label or label.startswith(("Продукт", ":-", "-")) or "ИТОГО" in label.upper()
                    or "вода" in label.lower()):
                continue
            kcal = _num(cells[2])
            if kcal is None:
                continue
            items.append({
                "name": label, "grams": _grams(cells[1]), "kcal": kcal,
                "protein_g": _num(cells[3]), "fat_g": _num(cells[4]), "carbs_g": _num(cells[5]),
            })
        if items:
            meals.append({"slot": slot, "at": f"{date} {h.group(2).zfill(5)}:00", "items": items})

    water = [(f"{date} {t.zfill(5)}:00", float(ml)) for t, ml in _WATER_RE.findall(text)]
    return {"date": date, "meals": meals, "water": water}


def import_dir(conn: sqlite3.Connection, user_id: int, root: Path | None = None) -> dict:
    """UPSERT по дню: сначала сносим ранее импортированные приёмы этой даты,
    иначе повторный прогон удвоит день. Ручные записи из бота за тот же день
    тоже уйдут — дневник считается более полным источником."""
    root = root or DIARY_DIR
    days = meals_n = items_n = water_n = 0
    for path in sorted(root.glob("*.md")) if root.is_dir() else []:
        rec = parse(path)
        if rec is None or not rec["meals"]:
            continue
        conn.execute("DELETE FROM food_log WHERE user_id=? AND date(eaten_at)=?", (user_id, rec["date"]))
        for meal in rec["meals"]:
            cur = conn.execute("INSERT INTO food_log(user_id, eaten_at, meal_slot) VALUES (?,?,?)",
                               (user_id, meal["at"], meal["slot"]))
            for it in meal["items"]:
                conn.execute(
                    "INSERT INTO food_items(food_log_id, name, grams, kcal, protein_g, fat_g, carbs_g) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (cur.lastrowid, it["name"], it["grams"], it["kcal"],
                     it["protein_g"], it["fat_g"], it["carbs_g"]),
                )
                items_n += 1
            meals_n += 1
        if rec["water"]:
            conn.execute("DELETE FROM water_log WHERE user_id=? AND date(at)=?", (user_id, rec["date"]))
            for at, ml in rec["water"]:
                conn.execute("INSERT INTO water_log(user_id, at, volume_ml) VALUES (?,?,?)", (user_id, at, ml))
                water_n += 1
        days += 1
    conn.commit()
    return {"days": days, "meals": meals_n, "items": items_n, "water": water_n}


if __name__ == "__main__":
    p = DIARY_DIR / "2026-08-22.md"
    r = parse(p)
    assert r["date"] == "2026-08-22", r
    slots = [m["slot"] for m in r["meals"]]
    assert "breakfast" in slots and "snack" in slots, slots
    first = r["meals"][0]["items"][0]
    assert first["kcal"] == 338 and first["protein_g"] == 27.3, first
    assert first["grams"] == 215, first          # «3 шт. (~215 г)» -> 215, не 3
    assert all("ИТОГО" not in i["name"].upper() for m in r["meals"] for i in m["items"])
    assert all("вода" not in i["name"].lower() for m in r["meals"] for i in m["items"])
    assert len(r["water"]) == 3 and r["water"][0][1] == 500.0, r["water"]
    kcal = sum(i["kcal"] for m in r["meals"] for i in m["items"])
    assert 840 <= kcal <= 870, kcal               # свод дневника: ~853
    print(f"ingest.diary: ok — {len(r['meals'])} приёма, {kcal:.0f} ккал, вода {sum(w for _, w in r['water']):.0f} мл")
