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
_FIELD_RE = re.compile(r"^-\s*\*\*([^*:]+):\*\*\s*(.+?)\s*$", re.M)
_NUM_RE = re.compile(r"[-+]?\d*[.,]?\d+")


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


def _num(text: str) -> float | None:
    m = _NUM_RE.search(text)
    return float(m.group().replace(",", ".")) if m else None


@lru_cache(maxsize=1)
def _cards() -> dict[str, dict]:
    """Каноническое имя (ключ casefold) -> поля карты. Кэш и парсинг тем же
    файлом и тем же приёмом, что aliases() — второго справочника не заводим."""
    try:
        text = DRUG_CARDS.read_text(encoding="utf-8")
    except OSError:
        return {}
    headers = list(_CARD_RE.finditer(text))
    out: dict[str, dict] = {}
    for i, m in enumerate(headers):
        first = m.group(1).split("/")[0].strip()
        canonical = _PAREN_RE.sub("", first).strip()
        if not canonical:
            continue
        block_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        fields = {k.strip(): v.strip() for k, v in _FIELD_RE.findall(text[m.end():block_end])}
        if not fields:
            continue
        status = fields.get("Статус")
        ladder_raw = fields.get("Лестница")
        ladder = [_num(x) for x in ladder_raw.split(",")] if ladder_raw else None
        if ladder and any(v is None for v in ladder):
            ladder = None  # неразборчивая лестница — как будто её нет, не половинка
        min_weeks_raw = fields.get("Минимум недель на ступени")
        interval_raw = fields.get("Интервал приёма")
        half_life_raw = fields.get("Период полувыведения")
        tmax_raw = fields.get("Пик концентрации")
        out[_key(canonical)] = {
            "status": status,
            "unregistered": bool(status and "не зарегистрирован" in status),
            "ladder": ladder,
            "min_weeks": int(_num(min_weeks_raw)) if min_weeks_raw and _num(min_weeks_raw) is not None else None,
            "interval_days": int(_num(interval_raw)) if interval_raw and _num(interval_raw) is not None else None,
            "half_life_days": _num(half_life_raw) if half_life_raw else None,
            "tmax_h": _num(tmax_raw) if tmax_raw else None,
            "source": fields.get("Источник"),
        }
    return out


def card(name: str) -> dict | None:
    """Карта препарата по каноническому имени (или алиасу — сводится сам).

    None, если карты нет или в ней нет полей-строк (напр. карта только с
    механизмом действия, без лестницы/PK — «Тесторил»). Поля есть, но
    какие-то из них не заданы (нет лестницы у не полностью описанного
    препарата) — dict возвращается, просто с None в этих ключах; вызывающий
    код (рамки дозы, модель концентрации) сам решает, что делать без них."""
    return _cards().get(_key(canon(name)))


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


def stock_runs_out(conn, user_id: int) -> list[dict]:
    """Запас препарата (CONTEXT.md «Запас препарата»): для каждой строки
    med_schedule с известными every_days/next_at/stock_doses — дата, до
    которой хватит текущего остатка при неизменном интервале приёма.

    Последняя покрытая остатком доза = next_at + (stock_doses-1)*every_days;
    stock_doses=0 значит покрытия нет вовсе — запас уже кончился на next_at.
    Дата «хватит до» = последняя покрытая доза + один интервал (первая доза
    без препарата) = next_at + stock_doses*every_days — обе записи формулы
    совпадают, вторая короче.

    Возвращает только строки, где до даты «хватит до» осталось не больше
    guards.stock_warn_days (config.yaml) — вызывающему (morning_checkin,
    pharma status) не нужно повторять порог."""
    from datetime import datetime, timedelta

    from health_core.config import load, local_now

    warn_days = load()["guards"]["stock_warn_days"]
    now = local_now()
    rows = conn.execute(
        "SELECT substance, every_days, next_at, stock_doses FROM med_schedule "
        "WHERE user_id=? AND every_days IS NOT NULL AND next_at IS NOT NULL AND stock_doses IS NOT NULL",
        (user_id,),
    ).fetchall()
    out = []
    for r in rows:
        next_at = datetime.strptime(r["next_at"][:19], "%Y-%m-%d %H:%M:%S")
        runs_out = next_at + timedelta(days=r["every_days"] * max(r["stock_doses"], 0))
        days_left = (runs_out.date() - now.date()).days
        if days_left <= warn_days:
            out.append({
                "substance": r["substance"],
                "runs_out_at": runs_out.strftime("%Y-%m-%d %H:%M:%S"),
                "days_left": days_left,
            })
    return out


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

    tz = card("Тирзетта")  # алиас -> карта канонического имени
    assert tz is not None and tz["ladder"] == [2.5, 5.0, 7.5, 10.0, 12.5, 15.0], tz
    assert tz["min_weeks"] == 4 and tz["interval_days"] == 7
    assert tz["half_life_days"] == 5.0 and tz["tmax_h"] == 24.0
    assert tz["unregistered"] is False and tz["status"] == "зарегистрирован"

    sm = card("Оземпик")
    assert sm is not None and sm["ladder"] == [0.25, 0.5, 1.0, 1.7, 2.4], sm
    assert sm["half_life_days"] == 7.0 and sm["tmax_h"] == 36.0, "семаглутид получает свой профиль, не тирзепатида"

    rt = card("Ретатрутид")
    assert rt is not None and rt["unregistered"] is True, "ретатрутид помечен незарегистрированным"
    assert rt["ladder"] == [2.0, 4.0, 6.0, 9.0, 12.0]

    assert card("Тесторил") is None or card("Тесторил").get("ladder") is None, "у Тесторила нет лестницы"
    assert card("Ноотропил") is None, "без карты — None"

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

    # stock_runs_out: пример из задания — stock_doses=1, every_days=7,
    # next_at=2026-09-20 22:00 -> хватит до 2026-09-27; при today=2026-09-14
    # до даты 13 дней -> предупреждение (порог guards.stock_warn_days=14).
    # stock_doses=0 -> покрытия нет, предупреждение сразу; stock_doses=5 ->
    # запаса на 35 дней, предупреждения быть не должно.
    import health_core.config as _cfg_mod
    from datetime import datetime as _dt

    _orig_local_now = _cfg_mod.local_now
    _cfg_mod.local_now = lambda: _dt(2026, 9, 14, 8, 0, 0)
    try:
        conn.execute("ALTER TABLE med_schedule ADD COLUMN every_days INTEGER")
        conn.execute("DELETE FROM med_schedule")
        conn.executemany(
            "INSERT INTO med_schedule(user_id, substance, every_days, next_at, stock_doses) VALUES (?,?,?,?,?)",
            [(3, "Тирзепатид", 7, "2026-09-20 22:00:00", 1.0),
             (3, "Андрокомплекс", 7, "2026-09-20 22:00:00", 0.0),
             (3, "Тесторил", 7, "2026-09-20 22:00:00", 5.0)],
        )
        warns = {w["substance"]: w for w in stock_runs_out(conn, 3)}
        assert warns["Тирзепатид"]["runs_out_at"][:10] == "2026-09-27", warns["Тирзепатид"]
        assert warns["Тирзепатид"]["days_left"] == 13, warns["Тирзепатид"]
        assert "Андрокомплекс" in warns, "stock_doses=0 — покрытия нет, предупреждение сразу"
        assert "Тесторил" not in warns, (
            f"stock_doses=5 — запас на 35 дней, предупреждения быть не должно: {warns.get('Тесторил')}"
        )
    finally:
        _cfg_mod.local_now = _orig_local_now

    print("meds: ok", len(a), "алиасов;", "stock_runs_out: пример/0/5 доз проверены")
    sys.exit(0)
