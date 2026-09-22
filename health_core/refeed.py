"""Плановые перерывы в дефиците.

Рефид назначается **по показаниям**, а не по расписанию:
  - плато (вес + талия стоят ≥ 2–3 нед.)   → reason='plateau'
  - сигнал восстановления (HRV↓ + пульс↑)  → reason='recovery_low'
  - решение консилиума                       → reason='council'
  - ручное решение пользователя              → reason='manual'

Byrne 2018 (MATADOR): 2 нед. дефицита / 2 нед. maintenance дало лучшие
результаты, чем непрерывный дефицит. Однако на GLP-1 терапии метаболическая
адаптация частично компенсирована препаратом, и автоматический 2/2-цикл
избыточен. Длительность рефида: 3–7 дней (не 14); минимум 7 дней нужен
для метаболического эффекта (Peos), но при GLP-1 хватает 3–5 для
восстановления лептина и гликогена.

Механизм: energy.daily_target() видит дату в refeed_days и ставит цель
равной полному TDEE. Здесь — расстановка дат и хранение причины.
"""
import sqlite3
from datetime import date as _date, timedelta

# Дефолты для MATADOR-цикла (оставлены для обратной совместимости,
# но schedule() больше не рекомендуется — предпочтительнее schedule_once).
DEFICIT_WEEKS = 2
BREAK_WEEKS = 2

# Дефолтная длительность единичного рефида
DEFAULT_REFEED_DAYS = 4


def _d(s):
    return s if isinstance(s, _date) else _date.fromisoformat(s)


def schedule_once(conn: sqlite3.Connection, user_id: int, start,
                  days: int = DEFAULT_REFEED_DAYS,
                  reason: str = "manual") -> dict:
    """Назначить единичный блок рефида с указанием причины.

    start  — первый день рефида
    days   — длительность (по умолчанию 4)
    reason — причина: 'plateau', 'recovery_low', 'council', 'manual'

    Возвращает {scheduled: [список дат], days: N, reason: str}.
    """
    start = _d(start)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]

    for d in dates:
        conn.execute(
            "INSERT OR IGNORE INTO refeed_days(user_id, date, reason) VALUES (?, ?, ?)",
            (user_id, d, reason),
        )
    conn.commit()
    return {"scheduled": dates, "days": days, "reason": reason}


def cycle_days(start, horizon_weeks: int = 12,
               deficit_weeks: int = DEFICIT_WEEKS, break_weeks: int = BREAK_WEEKS) -> list[str]:
    """Даты ПЕРЕРЫВОВ на горизонте. Цикл начинается с фазы дефицита.

    Оставлен для обратной совместимости — предпочтительнее schedule_once().
    """
    start = _d(start)
    out, day, total = [], 0, horizon_weeks * 7
    period = (deficit_weeks + break_weeks) * 7
    while day < total:
        if day % period >= deficit_weeks * 7:
            out.append((start + timedelta(days=day)).isoformat())
        day += 1
    return out


def schedule(conn: sqlite3.Connection, user_id: int, start, horizon_weeks: int = 12) -> dict:
    """Расставить перерывы по MATADOR-циклу. Существующие даты не дублируются.

    ⚠️ Предпочтительнее schedule_once() — рефид по показаниям, а не по расписанию.
    """
    days = cycle_days(start, horizon_weeks)
    conn.executemany("INSERT OR IGNORE INTO refeed_days(user_id, date) VALUES (?,?)",
                     [(user_id, d) for d in days])
    conn.commit()
    return {"scheduled": len(days), "first": days[0] if days else None, "last": days[-1] if days else None}


def clear(conn: sqlite3.Connection, user_id: int, frm=None) -> int:
    """Снять будущие перерывы. Прошлые не трогаем — это история, а не план."""
    from health_core.config import user_today
    frm = frm or user_today(conn, user_id)
    frm = frm if isinstance(frm, str) else frm.isoformat()
    cur = conn.execute("DELETE FROM refeed_days WHERE user_id=? AND date >= ?", (user_id, frm))
    conn.commit()
    return cur.rowcount


def status(conn: sqlite3.Connection, user_id: int, today) -> dict:
    """Фаза на сегодня, когда она сменится, причина и уведомления.

    Возвращает:
      phase        — 'break' | 'deficit' | None
      reason       — причина текущего/ближайшего рефида (или None)
      changes_in_days — через сколько дней сменится фаза
      changes_on   — дата смены фазы
      notify       — текст уведомления для morning checkin (или None)
    """
    today = _d(today)
    today_s = today.isoformat()

    # Все рефидные дни с причинами
    rows = conn.execute(
        "SELECT date, reason FROM refeed_days WHERE user_id=? ORDER BY date",
        (user_id,),
    ).fetchall()
    marked = {r["date"]: r["reason"] for r in rows} if rows else {}

    if not marked:
        return {"phase": None, "reason": None, "notify": None}

    in_break = today_s in marked
    reason = marked.get(today_s)

    # Найти конец текущей фазы
    day, limit = 1, 60
    while day <= limit and ((today + timedelta(days=day)).isoformat() in marked) == in_break:
        day += 1
    changes_in = day if day <= limit else None
    changes_on = (today + timedelta(days=day)).isoformat() if day <= limit else None

    # Найти ближайший будущий рефид (для уведомлений за 2 дня)
    upcoming = None
    upcoming_reason = None
    if not in_break:
        for d_s, r in sorted(marked.items()):
            if d_s > today_s:
                upcoming = d_s
                upcoming_reason = r
                break

    # Формировать уведомление
    notify = None
    reason_text = _reason_label(reason or upcoming_reason)

    if in_break:
        # Найти первый день текущего блока рефида
        block_start = today_s
        check = today
        while (check - timedelta(days=1)).isoformat() in marked:
            check -= timedelta(days=1)
            block_start = check.isoformat()

        # Последний день рефида?
        tomorrow = (today + timedelta(days=1)).isoformat()
        if tomorrow not in marked:
            # Завтра возвращаемся к дефициту
            notify = (
                f"🍽 Сегодня **последний день рефида**. "
                f"Завтра возвращаемся к дефициту.\n"
                f"Вес может быть на +1.5–2.5 кг — это вода, сойдёт за 2–3 дня."
            )
        else:
            days_left = changes_in - 1 if changes_in else None
            notify = (
                f"🍽 Сегодня **рефид** — цель на уровне TDEE, дефицит на паузе.\n"
                f"Причина: {reason_text}."
            )
            if days_left is not None:
                notify += f" Осталось {days_left} дн."
            notify += "\nПриоритет: углеводы. Белок на обычном уровне."
    else:
        # На дефиците — проверяем ближайший рефид
        if upcoming and changes_in is not None and changes_in <= 2:
            notify = (
                f"🍽 Через {changes_in} дн. начнётся рефид ({upcoming}).\n"
                f"Причина: {reason_text}.\n"
                f"Напомню: вес прибавит 1.5–2.5 кг (гликоген + вода), это нормально."
            )

    return {
        "phase": "break" if in_break else "deficit",
        "reason": reason or upcoming_reason,
        "changes_in_days": changes_in,
        "changes_on": changes_on,
        "notify": notify,
    }


def _reason_label(reason: str | None) -> str:
    """Человекочитаемое название причины рефида."""
    labels = {
        "plateau": "плато — вес и объёмы без динамики",
        "recovery_low": "сигнал восстановления — HRV↓ + пульс↑",
        "council": "решение консилиума",
        "manual": "назначен вручную",
    }
    return labels.get(reason, reason or "не указана")


if __name__ == "__main__":
    days = cycle_days("2026-08-25", horizon_weeks=8)
    assert "2026-08-25" not in days, "цикл начинается с дефицита"
    assert "2026-09-07" not in days, "14-й день ещё дефицит"
    assert "2026-09-08" in days and "2026-09-21" in days, "перерыв — дни 15..28"
    assert "2026-09-22" not in days, "после перерыва снова дефицит"
    assert len(days) == 28, len(days)          # 8 недель -> два перерыва по 14 дней

    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE refeed_days(id INTEGER PRIMARY KEY, user_id INT, date TEXT,"
                 " reason TEXT, UNIQUE(user_id, date))")

    # Тест MATADOR-цикла (обратная совместимость)
    r = schedule(conn, 1, "2026-08-25", horizon_weeks=8)
    assert r["scheduled"] == 28, r
    assert schedule(conn, 1, "2026-08-25", horizon_weeks=8)["scheduled"] == 28, "повтор не дублирует"
    assert conn.execute("SELECT COUNT(*) FROM refeed_days").fetchone()[0] == 28

    st = status(conn, 1, "2026-08-25")
    assert st["phase"] == "deficit" and st["changes_on"] == "2026-09-08", st
    st = status(conn, 1, "2026-09-10")
    assert st["phase"] == "break" and st["changes_on"] == "2026-09-22", st
    assert clear(conn, 1, "2026-09-22") == 14, "снимаем только будущее"

    # Тест schedule_once с причиной
    conn.execute("DELETE FROM refeed_days WHERE user_id=1")
    r = schedule_once(conn, 1, "2026-10-01", days=4, reason="plateau")
    assert r["days"] == 4 and r["reason"] == "plateau"
    assert len(r["scheduled"]) == 4
    st = status(conn, 1, "2026-10-01")
    assert st["phase"] == "break" and st["reason"] == "plateau", st
    assert st["notify"] is not None and "рефид" in st["notify"], st["notify"]

    # Уведомление за 2 дня
    st = status(conn, 1, "2026-09-29")
    assert st["phase"] == "deficit", st
    assert st["notify"] is not None and "Через 2 дн." in st["notify"], st["notify"]

    # Уведомление в последний день
    st = status(conn, 1, "2026-10-04")
    assert st["phase"] == "break", st
    assert st["notify"] is not None and "последний день" in st["notify"], st["notify"]

    print("refeed: ok — цикл 2/2, schedule_once, статус с причиной, уведомления, отмена")
