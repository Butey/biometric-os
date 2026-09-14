"""§11 пн 08:00 — пересчёт BMR и порогов при ≥3 утренних замерах, --no-agent.

Сам пересчёт делегирован health_core.energy.daily_target() (владеет другой
агент этой волны) — здесь только гейт по числу утренних (06:00–11:00) замеров
и печать результата. Гейт без ограничения по окну дат: считаем все замеры за
всё время, симметрично тому, как §10 считает "точки" в остальных гардрейлах.

    python scripts/weekly_recalc.py
    python scripts/weekly_recalc.py --user 3   # только этот пользователь (для notify.py --all)
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from health_core.config import load
from health_core.db import connect, migrate
from health_core.energy import daily_target


def main() -> int:
    ap = argparse.ArgumentParser(description="Еженедельный пересчёт BMR и порогов")
    ap.add_argument("--user", type=int, help="только этот db user id (без флага — все одной простынёй)")
    args = ap.parse_args()

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
    cfg = load()
    min_morning = cfg.get("policy", {}).get("weekly_recalc_min_morning_measures", 3)
    # При --user получатель и так один человек, префикс user_id=N: ему не нужен.
    prefix_tpl = "" if args.user is not None else "user_id={id}: "
    lines = []
    for u in users:
        n = conn.execute(
            "SELECT COUNT(*) c FROM body_metrics WHERE user_id=? "
            "AND time(measured_at) BETWEEN '06:00:00' AND '11:00:00'",
            (u["id"],),
        ).fetchone()["c"]
        if n < min_morning:
            continue
        prefix = prefix_tpl.format(id=u["id"])
        try:
            result = daily_target(conn, u["id"], today)
            lines.append(f"{prefix}цель {result['kcal']:.0f} ккал ({result['source']}).")
        except Exception as e:
            lines.append(f"{prefix}пересчёт не удался: {e}")

    conn.close()
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
