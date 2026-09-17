"""Импорт сводки тренировки из TCX в activity — §12. Трекпоинты не читаются в БД, только
Activity/Lap-уровень: время старта, длительность, калории устройства, средний пульс, вид спорта.

Поток: ET.iterparse, elem.clear() после каждого узла. Файл ходьбы 862 KB на диске даёт
~2 KB в базе — трекпоинты (Track/Trackpoint/Position/...) никогда не накапливаются.
"""
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# YYYYMMDD + название вида спорта + необязательный _NN суффикс (§12). Sport-атрибут в
# самом XML почти всегда пуст, поэтому источник истины — имя файла.
_SPORT_RE = re.compile(r"^\d{8}(.+?)(?:_\d+)?$")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sport_from_filename(path: str) -> str | None:
    m = _SPORT_RE.match(Path(path).stem)
    return m.group(1).strip() if m else None


def _parse_summary(path: str) -> dict:
    started_at = kcal = lap_kcal = avg_hr = duration_sec = None
    in_lap = False  # в датасете ровно один Lap на файл; Calories вне Lap — итог тренировки,
    # но у части файлов (без GPS-дубля вне Lap) итог указан только внутри Lap — тогда берём его.
    for event, el in ET.iterparse(path, events=("start", "end")):
        tag = el.tag.split("}")[-1]
        if event == "start":
            if tag == "Lap":
                in_lap = True
            continue
        if tag == "Id" and started_at is None:
            started_at = (el.text or "").strip()
        elif tag == "Calories" and not in_lap and kcal is None:
            kcal = el.text
        elif tag == "Calories" and in_lap and lap_kcal is None:
            lap_kcal = el.text
        elif tag == "TotalTimeSeconds" and duration_sec is None:
            duration_sec = el.text
        elif tag == "HeartRateBpm" and avg_hr is None:
            avg_hr = el.text
        elif tag == "Lap":
            in_lap = False
        el.clear()  # освобождаем содержимое узла (включая трекпоинты) сразу после чтения

    kcal = kcal if kcal is not None else lap_kcal
    return {
        "started_at": started_at,
        "duration_sec": float(duration_sec) if duration_sec else None,
        "kcal": float(kcal) if kcal else None,
        "avg_hr": int(float(avg_hr)) if avg_hr else None,
    }


def import_tcx(conn, user_id: int, path: str) -> dict:
    """Импорт одной тренировки. Возвращает {"added": 0|1, "skipped": 0|1}."""
    file_hash = _sha256(path)
    if conn.execute("SELECT 1 FROM activity WHERE file_hash=?", (file_hash,)).fetchone():
        return {"added": 0, "skipped": 1}

    summary = _parse_summary(path)
    if not summary["started_at"]:
        return {"added": 0, "skipped": 1}  # битый файл — не помечаем как импортированный

    started_at = datetime.strptime(
        summary["started_at"], "%Y-%m-%dT%H:%M:%S.%fZ"
    ).strftime("%Y-%m-%d %H:%M:%S")
    duration_min = summary["duration_sec"] / 60 if summary["duration_sec"] else None
    sport = _sport_from_filename(path)

    cur = conn.execute(
        "INSERT INTO activity(user_id, started_at, duration_min, kcal, avg_hr, sport, file_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(file_hash) DO NOTHING",
        (user_id, started_at, duration_min, summary["kcal"], summary["avg_hr"], sport, file_hash),
    )
    added = 1 if cur.rowcount else 0

    conn.execute(
        "INSERT INTO import_log(user_id, imported_at, file_hash) VALUES (?, ?, ?) "
        "ON CONFLICT(file_hash) DO UPDATE SET imported_at=excluded.imported_at",
        (user_id, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), file_hash),
    )
    conn.commit()
    return {
        "added": added,
        "skipped": 1 - added,
        "activity": {
            "sport": sport,
            "duration_min": round(duration_min, 1) if duration_min else None,
            "kcal": summary["kcal"],
            "avg_hr": summary["avg_hr"],
            "started_at": started_at,
        } if added else None,
    }


if __name__ == "__main__":
    # Регрессия: файлы без GPS-дубля Calories вне Lap — итог только внутри
    # Lap, раньше терялся молча (kcal=None на реальных тренировках).
    _f = "Metrics/Activity/20260730Бег на улице.tcx"
    _s = _parse_summary(_f)
    assert _s["kcal"] == 421.0, _s
    print("OK: tcx._parse_summary — kcal только внутри Lap не теряется")
