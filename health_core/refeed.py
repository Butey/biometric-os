"""Плановые перерывы в дефиците — протокол MATADOR.

Byrne 2018 (Int J Obes, 51 мужчина с ожирением): чередование 2 недель
ограничения и 2 недель поддержания дало 14.1 кг потери веса против 9.1 кг при
непрерывном ограничении той же глубины, жира 12.3 против 8.0 кг, а снижение
RMR с поправкой на состав тела было вдвое меньше (-86 против -179 ккал/сут).

Механизм в коде уже был: energy.daily_target() видит дату в refeed_days и
ставит цель равной полному TDEE. Здесь только расстановка этих дат.

Обзор Peos: чтобы притупить адаптацию, перерыв должен длиться НЕ МЕНЕЕ 7 дней.
Поэтому «день послаблений» протоколом не считается — он возвращает гликоген и
даёт передышку, но метаболизм не трогает.
"""
import sqlite3
from datetime import date as _date, timedelta

DEFICIT_WEEKS = 2
BREAK_WEEKS = 2


def _d(s):
    return s if isinstance(s, _date) else _date.fromisoformat(s)


def cycle_days(start, horizon_weeks: int = 12,
               deficit_weeks: int = DEFICIT_WEEKS, break_weeks: int = BREAK_WEEKS) -> list[str]:
    """Даты ПЕРЕРЫВОВ на горизонте. Цикл начинается с фазы дефицита."""
    start = _d(start)
    out, day, total = [], 0, horizon_weeks * 7
    period = (deficit_weeks + break_weeks) * 7
    while day < total:
        if day % period >= deficit_weeks * 7:
            out.append((start + timedelta(days=day)).isoformat())
        day += 1
    return out


def schedule(conn: sqlite3.Connection, user_id: int, start, horizon_weeks: int = 12) -> dict:
    """Расставить перерывы начиная со start. Существующие даты не дублируются."""
    days = cycle_days(start, horizon_weeks)
    conn.executemany("INSERT OR IGNORE INTO refeed_days(user_id, date) VALUES (?,?)",
                     [(user_id, d) for d in days])
    conn.commit()
    return {"scheduled": len(days), "first": days[0] if days else None, "last": days[-1] if days else None}


def clear(conn: sqlite3.Connection, user_id: int, frm=None) -> int:
    """Снять будущие перерывы. Прошлые не трогаем — это история, а не план."""
    frm = (frm or _date.today()).isoformat() if not isinstance(frm, str) else frm
    cur = conn.execute("DELETE FROM refeed_days WHERE user_id=? AND date >= ?", (user_id, frm))
    conn.commit()
    return cur.rowcount


def status(conn: sqlite3.Connection, user_id: int, today) -> dict:
    """Фаза на сегодня и когда она сменится."""
    today = _d(today)
    marked = {r[0] for r in conn.execute("SELECT date FROM refeed_days WHERE user_id=?", (user_id,))}
    if not marked:
        return {"phase": None}
    in_break = today.isoformat() in marked
    day, limit = 1, 60
    while day <= limit and ((today + timedelta(days=day)).isoformat() in marked) == in_break:
        day += 1
    return {"phase": "break" if in_break else "deficit",
            "changes_in_days": day if day <= limit else None,
            "changes_on": (today + timedelta(days=day)).isoformat() if day <= limit else None}


if __name__ == "__main__":
    days = cycle_days("2026-08-25", horizon_weeks=8)
    assert "2026-08-25" not in days, "цикл начинается с дефицита"
    assert "2026-09-07" not in days, "14-й день ещё дефицит"
    assert "2026-09-08" in days and "2026-09-21" in days, "перерыв — дни 15..28"
    assert "2026-09-22" not in days, "после перерыва снова дефицит"
    assert len(days) == 28, len(days)          # 8 недель -> два перерыва по 14 дней

    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE refeed_days(id INTEGER PRIMARY KEY, user_id INT, date TEXT,"
                 " UNIQUE(user_id, date))")
    r = schedule(conn, 1, "2026-08-25", horizon_weeks=8)
    assert r["scheduled"] == 28, r
    assert schedule(conn, 1, "2026-08-25", horizon_weeks=8)["scheduled"] == 28, "повтор не дублирует"
    assert conn.execute("SELECT COUNT(*) FROM refeed_days").fetchone()[0] == 28

    st = status(conn, 1, "2026-08-25")
    assert st["phase"] == "deficit" and st["changes_on"] == "2026-09-08", st
    st = status(conn, 1, "2026-09-10")
    assert st["phase"] == "break" and st["changes_on"] == "2026-09-22", st
    assert clear(conn, 1, "2026-09-22") == 14, "снимаем только будущее"
    print("refeed: ok — цикл 2/2, статус и отмена")
