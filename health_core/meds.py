"""Каноническое имя препарата.

Торговое название и МНН — один и тот же препарат. Без сведения имён
«Тирзетта» и «Тирзепатид» заводят ДВА расписания в med_schedule (там
UNIQUE(user_id, substance)), приём, залогированный под одним именем, не
двигает next_at второго, и вторая строка висит просроченной вечно, хотя
доза принята.

Источник алиасов — заголовки карт в Knowledge/drug_cards.md: тот же файл,
который правит человек и читает модель через knowledge(). Второго списка
внутри кода нет намеренно — разъехавшиеся справочники хуже отсутствующего.
Формат заголовка задан самим файлом:

    ## 📇 Карта 2: Тесторил / Андрокомплекс

Первое имя каноническое, остальные — алиасы. Латиница в скобках
(«Тирзепатид (Tirzepatide)») тоже считается алиасом.
"""
import re
from functools import lru_cache
from pathlib import Path

DRUG_CARDS = Path(__file__).resolve().parent.parent / "Knowledge" / "drug_cards.md"

_CARD_RE = re.compile(r"^#{1,6}[^\S\n]*\S*[^\S\n]*Карта[^\S\n]*\d+[^\S\n]*:[^\S\n]*(.+?)[^\S\n]*$", re.M)
_PAREN_RE = re.compile(r"\(([^)]*)\)")


def _key(name: str) -> str:
    return " ".join(name.split()).casefold()


@lru_cache(maxsize=1)
def aliases() -> dict[str, str]:
    """алиас -> каноническое имя. Файла нет — пустая карта, не исключение:
    сведение имён приятно, но ради него не стоит ронять запись приёма."""
    try:
        text = DRUG_CARDS.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for m in _CARD_RE.finditer(text):
        names: list[str] = []
        for part in m.group(1).split("/"):
            part = part.strip()
            if not part:
                continue
            names.append(_PAREN_RE.sub("", part).strip())
            names.extend(inner.strip() for inner in _PAREN_RE.findall(part))
        names = [n for n in names if n]
        for n in names:
            out.setdefault(_key(n), names[0])
    return out


def canon(name: str) -> str:
    """Каноническое имя препарата. Незнакомое возвращается как есть — карт на
    всё не напасёшься, а отказ записать приём хуже неканоничного имени."""
    clean = " ".join((name or "").split())
    return aliases().get(_key(clean), clean)


def merge_duplicate_schedules(conn) -> list[tuple[str, str]]:
    """Свести строки med_schedule, чьи имена — алиасы одного препарата.
    Возвращает пары (было, стало) для отчёта миграции.

    Остаток берём МАКСИМАЛЬНЫЙ, не сумму: дубль обычно возникал оттого, что
    один и тот же запас записали дважды под разными именами, и сумма выдумала
    бы дозы, которых нет. Занижение человек видит сразу и правит `pharma
    restock`; завышение обнаруживается тем, что препарат кончился раньше, чем
    обещала панель, — на инъекционной терапии это дороже.

    next_at берём САМЫЙ РАННИЙ: пропущенную дозу лучше показать, чем скрыть.
    """
    rows = conn.execute(
        "SELECT id, user_id, substance, next_at, stock_doses FROM med_schedule ORDER BY id"
    ).fetchall()
    groups: dict[tuple[int, str], list] = {}
    for r in rows:
        groups.setdefault((r["user_id"], canon(r["substance"])), []).append(r)

    renamed: list[tuple[str, str]] = []
    for (_user_id, name), group in groups.items():
        keep = group[0]
        if len(group) > 1:
            nexts = [g["next_at"] for g in group if g["next_at"]]
            stocks = [g["stock_doses"] for g in group if g["stock_doses"] is not None]
            # Сначала удаляем лишние строки, потом переименовываем оставшуюся:
            # обратный порядок упёрся бы в UNIQUE(user_id, substance).
            for g in group[1:]:
                conn.execute("DELETE FROM med_schedule WHERE id=?", (g["id"],))
                renamed.append((g["substance"], name))
            conn.execute(
                "UPDATE med_schedule SET substance=?, next_at=?, stock_doses=? WHERE id=?",
                (name, min(nexts) if nexts else None, max(stocks) if stocks else None, keep["id"]),
            )
        elif keep["substance"] != name:
            conn.execute("UPDATE med_schedule SET substance=? WHERE id=?", (name, keep["id"]))
            renamed.append((keep["substance"], name))

    # med_log НЕ трогаем. Гарды читают его по route='oral' (health_core/
    # guards.py), а не по тексту имени, так что сводить там нечего — а
    # переписывать задним числом уже сделанную запись о приёме препарата
    # значит править медицинскую историю ради косметики. Новые записи и так
    # ложатся каноничными: canon() применяется на входе в log_med.
    return renamed


if __name__ == "__main__":
    import sqlite3
    import sys

    a = aliases()
    assert canon("  тирзепатид ") == "Тирзепатид", canon("тирзепатид")
    assert canon("Tirzepatide") == "Тирзепатид", "латиница в скобках — алиас"
    assert canon("Андрокомплекс") == "Тесторил", "второе имя через / сводится к первому"
    assert canon("Тесторил") == "Тесторил"
    assert canon("Ноотропил") == "Ноотропил", "незнакомое имя не трогаем"
    assert canon("") == ""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE med_schedule(id INTEGER PRIMARY KEY, user_id INT, substance TEXT,"
        " next_at TEXT, stock_doses REAL, UNIQUE(user_id, substance));"
        "CREATE TABLE med_log(id INTEGER PRIMARY KEY, substance TEXT);"
    )
    conn.executemany(
        "INSERT INTO med_schedule(user_id, substance, next_at, stock_doses) VALUES (?,?,?,?)",
        [(1, "Тирзепатид", "2026-08-23 22:00:00", 5.0),
         (1, "Тирзетта", "2026-08-30 22:00:00", 4.0),
         (1, "Тесторил", None, 3.0),
         (2, "Тирзетта", "2026-09-01 22:00:00", 8.0)],
    )
    conn.execute("INSERT INTO med_log(substance) VALUES ('Тирзетта')")
    merge_duplicate_schedules(conn)

    left = conn.execute("SELECT user_id, substance, next_at, stock_doses FROM med_schedule"
                        " ORDER BY user_id, substance").fetchall()
    got = [(r["user_id"], r["substance"], r["next_at"], r["stock_doses"]) for r in left]
    assert got == [(1, "Тесторил", None, 3.0),
                   (1, "Тирзепатид", "2026-08-23 22:00:00", 5.0),
                   (2, "Тирзепатид", "2026-09-01 22:00:00", 8.0)], got
    assert conn.execute("SELECT substance FROM med_log").fetchone()["substance"] == "Тирзетта",         "историю приёмов не переписываем — гарды читают route, а не имя"
    print("meds: ok", len(a), "алиасов")
    sys.exit(0)
