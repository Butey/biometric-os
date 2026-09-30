"""Антропометрия (§08, §12): талия/грудь/таз/бедро/шея/бицепс. Диапазон 'A-B' сводится к
среднему. Один и тот же разбор годится и для живой ленты замеров, и для миграции истории.
"""
import re
from datetime import date

SITES = {"талия", "грудь", "таз", "бедро", "шея", "бицепс"}

_DATE_RE = re.compile(r"^(\d{1,2})[.,](\d{1,2})(?:[.,](\d{4}))?$")
_LINE_RE = re.compile(r"^(\S+)\s+(.+)$")


def _parse_value(raw) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().replace(",", ".")
    if "-" in s:
        lo, hi = s.split("-", 1)
        return round((float(lo) + float(hi)) / 2, 2)  # диапазон -> середина
    return float(s)


def parse_text(text: str, default_year: int | None = None) -> list[dict]:
    """Свободный текст: строка-дата открывает сессию, дальше идут строки 'site значение'
    до следующей строки-даты. Блоки могут быть разделены произвольным числом пустых строк
    (в т.ч. пустой строкой сразу после самой даты) — поэтому разбор построчный, а не по
    группам строк. Год у даты может отсутствовать (напр. '09,05') — берём год предыдущей
    даты, для первой — default_year или текущий."""
    records = []
    last_year = default_year or date.today().year
    measured_on = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _DATE_RE.match(line)
        if m:
            day, month, year = m.groups()
            last_year = int(year) if year else last_year
            measured_on = f"{last_year:04d}-{int(month):02d}-{int(day):02d}"
            continue
        if measured_on is None:
            continue  # строки до первой даты в файле — пропускаем
        lm = _LINE_RE.match(line)
        if not lm:
            continue
        site, value_raw = lm.groups()
        if site not in SITES:
            continue
        try:
            records.append({"measured_on": measured_on, "site": site, "value": _parse_value(value_raw)})
        except ValueError:
            continue
    return records


def import_anthro(conn, user_id: int, records: list[dict]) -> dict:
    """records: [{"measured_on": "YYYY-MM-DD", "site": "талия", "value": 119 | "113-114"}, ...]
    Возвращает {"added": n, "skipped": n}."""
    added = skipped = 0
    for r in records:
        site = r["site"]
        if site not in SITES:
            skipped += 1
            continue
        try:
            value = _parse_value(r["value"])
        except ValueError:
            skipped += 1
            continue
        cur = conn.execute(
            "INSERT INTO anthropometry(user_id, measured_on, site, value_cm) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, measured_on, site) DO NOTHING",
            (user_id, r["measured_on"], site, value),
        )
        if cur.rowcount:
            added += 1
        else:
            skipped += 1
    conn.commit()
    return {"added": added, "skipped": skipped}
