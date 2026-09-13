"""§11 10:00/14:00/20:00 — напоминания о приёмах пищи, --no-agent.

Пустой вывод = запись уже есть (тихий тик, §11): скрипт печатает строку только
для приёма пищи, не записанного в своём окне.

Окно (какое напоминание считать) обычно определяется временем срабатывания
cron — без аргумента скрипт смотрит на текущий час и сам выбирает нужное окно,
так что реальный вызов из hermes cron остаётся без параметров, как в примере
§11 (`--script meal_window_check.py`). Для ручного прогона/самопроверки можно
передать окно явно:

    python scripts/meal_window_check.py [breakfast|lunch|dinner]
    python scripts/meal_window_check.py --user 3   # только этот пользователь (для notify.py --all)
"""
import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from health_core.db import connect, migrate

# fire_hour (когда срабатывает cron) -> (since_hour окна, метка приёма пищи)
_BY_FIRE_HOUR = {
    10: (8, "Завтрак"),
    14: (12, "Обед"),
    20: (18, "Ужин"),
}
_BY_NAME = {
    "breakfast": _BY_FIRE_HOUR[10],
    "lunch": _BY_FIRE_HOUR[14],
    "dinner": _BY_FIRE_HOUR[20],
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка окна приёма пищи")
    ap.add_argument("window", nargs="?", choices=list(_BY_NAME), help="окно вручную, иначе — по текущему часу")
    ap.add_argument("--user", type=int, help="только этот db user id (без флага — все одной простынёй)")
    args = ap.parse_args()

    if args.window:
        since_hour, label = _BY_NAME[args.window]
    else:
        window = _BY_FIRE_HOUR.get(datetime.now().hour)
        if window is None:
            return 0  # вне расписанных часов — тихий тик, не ошибка
        since_hour, label = window

    conn = connect()
    migrate(conn)
    if args.user is not None:
        users = conn.execute("SELECT id FROM users WHERE id=?", (args.user,)).fetchall()
    else:
        users = conn.execute("SELECT id FROM users ORDER BY id").fetchall()
    if not users:
        msg = f"user_id={args.user} не найден в БД." if args.user is not None else "Нет ни одного пользователя в БД."
        print(msg, file=sys.stderr)
        conn.close()
        return 1

    today = date.today().isoformat()
    since = f"{today} {since_hour:02d}:00:00"
    lines = []
    for u in users:
        n = conn.execute(
            "SELECT COUNT(*) c FROM food_log WHERE user_id=? AND eaten_at>=? AND date(eaten_at)=?",
            (u["id"], since, today),
        ).fetchone()["c"]
        if n == 0:
            lines.append(f"{label} не записан.")
    conn.close()

    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
