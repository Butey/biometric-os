"""Импорт выгрузок сна из Metrics/Sleep/<ДД.ММ.ГГГГ>/sleep_statistics_ГГГГ-ММ-ДД.md.

Файлы пишет приложение браслета в человекочитаемом markdown, поэтому разбор
регулярками, а не парсером: формат чужой и может поехать. Любое поле, которое
не нашлось, остаётся NULL — пустое значит «прибор не дал», а не «нуль».

Дата ночи берётся из ИМЕНИ файла (ISO), а не из русского заголовка: месяцы
прописью — лишний источник поломки на ровном месте.
"""
import re
import sqlite3
from pathlib import Path

SLEEP_DIR = Path(__file__).resolve().parent.parent.parent / "Metrics" / "Sleep"
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_H_RE = re.compile(r"(\d+)\s*ч")
_M_RE = re.compile(r"(\d+)\s*мин")


def _minutes(text: str) -> int | None:
    """«8 ч 25 мин» -> 505. Ни часов, ни минут — None, а не ноль.

    Два отдельных поиска, а не один шаблон с необязательными группами: такой
    шаблон успешно матчит пустую строку в нулевой позиции и возвращает None
    там, где число есть.
    """
    h, m = _H_RE.search(text), _M_RE.search(text)
    if h is None and m is None:
        return None
    return (int(h.group(1)) if h else 0) * 60 + (int(m.group(1)) if m else 0)


def _field(text: str, label: str) -> str | None:
    m = re.search(rf"\*\*{label}[^:*]*:\*\*\s*([^\n*]+)", text)
    return m.group(1).strip() if m else None


def _num(text: str, label: str) -> float | None:
    raw = _field(text, label)
    if not raw:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", raw)
    return float(m.group(1).replace(",", ".")) if m else None


def parse(path: Path) -> dict | None:
    """Одна выгрузка -> словарь для sleep_log. Без даты в имени — None."""
    dm = _DATE_RE.search(path.name)
    if dm is None:
        return None
    text = path.read_text(encoding="utf-8")
    dur = _field(text, "Общая продолжительность сна")
    stages = {}
    for name, key in (("REM-фаза", "rem_min"), ("Медленный", "deep_min")):
        row = re.search(rf"\*\*{name}\*\*[^|]*\|[^|]*\|([^|]*)\|", text)
        if row:
            stages[key] = _minutes(row.group(1))
    hr = _num(text, "Средний пульс")
    spo2 = _num(text, "Средний уровень кислорода")
    return {
        "night_date": dm.group(1),
        "bedtime": _field(text, "Лег в постель"),
        "wake_time": _field(text, "Пробуждение"),
        "duration_min": _minutes(dur) if dur else None,
        "deep_min": stages.get("deep_min"),
        "rem_min": stages.get("rem_min"),
        "efficiency_pct": _num(text, "Эффективность сна"),
        "hr_avg": int(hr) if hr else None,
        "spo2_avg": int(spo2) if spo2 else None,
        "source": "xiaomi_band",
    }


def import_dir(conn: sqlite3.Connection, user_id: int, root: Path | None = None) -> dict:
    """Все выгрузки из каталога. UPSERT по (user_id, night_date) — повторный
    прогон не плодит дубли и не теряет то, что уже проставлено вручную."""
    root = root or SLEEP_DIR
    rows, skipped = 0, 0
    for path in sorted(root.rglob("sleep_statistics_*.md")) if root.is_dir() else []:
        rec = parse(path)
        if rec is None or not rec["duration_min"]:
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO sleep_log(user_id, night_date, bedtime, wake_time, duration_min, "
            "deep_min, rem_min, efficiency_pct, hr_avg, spo2_avg, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id, night_date) DO UPDATE SET "
            "bedtime=COALESCE(excluded.bedtime,bedtime), wake_time=COALESCE(excluded.wake_time,wake_time), "
            "duration_min=excluded.duration_min, deep_min=COALESCE(excluded.deep_min,deep_min), "
            "rem_min=COALESCE(excluded.rem_min,rem_min), "
            "efficiency_pct=COALESCE(excluded.efficiency_pct,efficiency_pct), "
            "hr_avg=COALESCE(excluded.hr_avg,hr_avg), spo2_avg=COALESCE(excluded.spo2_avg,spo2_avg), "
            "source=COALESCE(excluded.source,source)",
            (user_id, rec["night_date"], rec["bedtime"], rec["wake_time"], rec["duration_min"],
             rec["deep_min"], rec["rem_min"], rec["efficiency_pct"], rec["hr_avg"],
             rec["spo2_avg"], rec["source"]),
        )
        rows += 1
    conn.commit()
    return {"imported": rows, "skipped": skipped}


if __name__ == "__main__":
    sample = """# Статистика сна от 21 мая 2026 г.
* **Общая продолжительность сна:** 8 ч 25 мин.
* **Эффективность сна:** 98% (Контрольный диапазон: 85–100%).
* **Лег в постель:** 23:04
* **Пробуждение:** 07:38
| **REM-фаза** (Быстрый сон) | 25% | 2 ч 4 мин | 10%–30% |
| **Медленный** (Глубокий сон) | 30% | 2 ч 31 мин | 20%–40% |
* **Средний пульс:** 55 BPM
* **Средний уровень кислорода (SpO2):** 98%
"""
    import tempfile
    d = Path(tempfile.mkdtemp())
    f = d / "sleep_statistics_2026-05-21.md"
    f.write_text(sample, encoding="utf-8")
    r = parse(f)
    assert r["night_date"] == "2026-05-21", r
    assert r["duration_min"] == 505, r          # 8ч25м
    assert r["rem_min"] == 124 and r["deep_min"] == 151, r
    assert r["efficiency_pct"] == 98.0 and r["hr_avg"] == 55 and r["spo2_avg"] == 98, r
    assert r["bedtime"] == "23:04" and r["wake_time"] == "07:38", r
    # Обрезанный файл не должен падать — недостающее становится None.
    f2 = d / "sleep_statistics_2026-05-22.md"
    f2.write_text("# Статистика\n* **Общая продолжительность сна:** 7 ч.\n", encoding="utf-8")
    r2 = parse(f2)
    assert r2["duration_min"] == 420 and r2["hr_avg"] is None and r2["rem_min"] is None, r2
    assert parse(d / "мусор.md") is None
    print("ingest.sleep: ok")
