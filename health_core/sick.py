"""Режим болезни.

Во время острой болезни дефицит не нужен, а шумные поведенческие гардрейлы (недоедание, риск срыва, перекос белка, плато,
скорость похудения, протухшая калибровка, пропуск замеров) только мешают:
человек и так ест как получится и не взвешивается по расписанию. Безопасность
(BMR/FFMI/липиды/глюкоза/WHR) остаётся включённой — болезнь сама по себе
поднимает глюкозу, и именно тут её отслеживание важнее всего.

Механизм в коде уже был: energy.daily_target() умеет ставить цель на полный
TDEE для дат из отдельной таблицы дат (см. refeed_days/_is_refeed). Здесь то
же самое для sick_days: плоский набор дат без смысла сверх самого факта
«болен», как и refeed_days — кто и на сколько дней его включает, решается
снаружи (plugin/tools.py, вне этого модуля).
"""
import sqlite3
from datetime import date as _date, timedelta

from health_core.config import load


def _d(s):
    return s if isinstance(s, _date) else _date.fromisoformat(s)


def start(conn: sqlite3.Connection, user_id: int, from_date: str, days: int, note: str | None = None) -> dict:
    """Отметить дни болезни from_date .. from_date+days-1. Повторный вызов на
    те же даты не дублирует строки; note обновляется только если передан новый
    (COALESCE — не затираем существующую заметку пустым вызовом)."""
    if not 1 <= days <= 30:
        raise ValueError(f"days должен быть 1..30, получили {days}")
    start_date = _d(from_date)
    dates = [(start_date + timedelta(days=i)).isoformat() for i in range(days)]
    conn.executemany(
        "INSERT INTO sick_days(user_id, date, note) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, date) DO UPDATE SET note=COALESCE(excluded.note, note)",
        [(user_id, d, note) for d in dates],
    )
    conn.commit()
    return {"from": dates[0], "to": dates[-1], "days": days}


def stop(conn: sqlite3.Connection, user_id: int, today: str) -> int:
    """Снять режим болезни начиная с сегодня. Прошлые дни болезни остаются —
    это история, а не план (симметрично refeed.clear)."""
    cur = conn.execute("DELETE FROM sick_days WHERE user_id=? AND date>=?", (user_id, today))
    conn.commit()
    return cur.rowcount


def is_sick(conn: sqlite3.Connection, user_id: int, date: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sick_days WHERE user_id=? AND date=?", (user_id, date)
    ).fetchone() is not None


def status(conn: sqlite3.Connection, user_id: int, today: str) -> dict:
    """Болен ли сегодня, и если да — по какую дату включительно тянется
    непрерывный отрезок болезни, начатый сегодня."""
    marked = {r[0] for r in conn.execute("SELECT date FROM sick_days WHERE user_id=?", (user_id,))}
    if today not in marked:
        return {"sick": False, "until": None, "note": None}
    d = _d(today)
    limit = 60
    n = 0
    while n <= limit and (d + timedelta(days=n)).isoformat() in marked:
        n += 1
    until = (d + timedelta(days=n - 1)).isoformat()
    note_row = conn.execute(
        "SELECT note FROM sick_days WHERE user_id=? AND date=?", (user_id, today)
    ).fetchone()
    return {"sick": True, "until": until, "note": note_row["note"] if note_row else None}


def quiet(conn: sqlite3.Connection, user_id: int, today: str) -> bool:
    """Тише ли себя вести с гардрейлами: сегодня болен ИЛИ болезнь закончилась
    недавно (quiet_after_days, по умолчанию 2).

    После болезни вес/БИА ещё пару дней искажены задержкой жидкости, а аппетит
    не сразу возвращается к норме — судить по этим дням так же строго, как по
    здоровым, значит хвалить/ругать за шум восстановления, а не за поведение.
    """
    if is_sick(conn, user_id, today):
        return True
    quiet_after = load().get("sick", {}).get("quiet_after_days", 2)
    if quiet_after <= 0:
        return False
    last_sick = conn.execute(
        "SELECT date FROM sick_days WHERE user_id=? AND date<? ORDER BY date DESC LIMIT 1",
        (user_id, today),
    ).fetchone()
    if last_sick is None:
        return False
    gap = (_d(today) - _d(last_sick["date"])).days
    return 0 < gap <= quiet_after


if __name__ == "__main__":
    import health_core.db as db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.DDL)

    uid = 1

    r = start(conn, uid, "2026-09-10", 3, note="грипп")
    assert r == {"from": "2026-09-10", "to": "2026-09-12", "days": 3}, r
    for d in ("2026-09-10", "2026-09-11", "2026-09-12"):
        assert is_sick(conn, uid, d), d
    assert not is_sick(conn, uid, "2026-09-13")

    st = status(conn, uid, "2026-09-10")
    assert st == {"sick": True, "until": "2026-09-12", "note": "грипп"}, st
    assert status(conn, uid, "2026-09-13") == {"sick": False, "until": None, "note": None}

    # stop мид-ран удаляет только будущие дни (>= today), прошлые остаются
    removed = stop(conn, uid, "2026-09-11")
    assert removed == 2, removed
    assert is_sick(conn, uid, "2026-09-10"), "прошлый день болезни должен остаться"
    assert not is_sick(conn, uid, "2026-09-11")
    assert not is_sick(conn, uid, "2026-09-12")

    # quiet(): на следующий день после последнего дня болезни — True, через 3 — False
    assert quiet(conn, uid, "2026-09-11"), "день сразу после болезни должен быть тихим"
    assert not quiet(conn, uid, "2026-09-13"), "через 3 дня после болезни тишина должна закончиться"

    try:
        start(conn, uid, "2026-09-20", 0)
        assert False, "days=0 должен кидать ValueError"
    except ValueError:
        pass

    print("sick: ok — start/is_sick/status/stop/quiet и days=0 -> ValueError")
