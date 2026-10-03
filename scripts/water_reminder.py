"""Напоминание попить, --no-agent. Пустой вывод = тихий тик.

Шлёт, когда воды меньше плана бота (health_core.report.water_pace: цель растёт
линейно с 08:00 до 20:00) и до плана не хватает не меньше одной порции
(policy.water_reminder_portion_ml). Интервал между напоминаниями выходит из плана:
при цели 3 л порция 250 мл набегает за час, при 2 л - за полтора.

    python scripts/water_reminder.py --user 3
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from health_core.config import load, user_now, user_today
from health_core.db import connect, migrate, get_target_users
from health_core.report import day_summary, water_pace


def reminder(conn, user_id: int) -> str | None:
    now = user_now(conn, user_id)
    d = day_summary(conn, user_id, user_today(conn, user_id))
    pace = water_pace(d.get("water_ml", 0), d.get("water_target_ml"), now)
    portion = load()["policy"].get("water_reminder_portion_ml", 250)
    if not pace or pace["behind_ml"] < portion:
        return None
    return (f"Пора попить: к этому часу по плану ~{pace['expected_ml']} мл, выпито "
            f"{round(d['water_ml'])}. Добери ~{pace['behind_ml']} мл.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Напоминание попить")
    ap.add_argument("--user", type=int, help="только этот db user id")
    args = ap.parse_args()
    conn = connect()
    migrate(conn)
    lines = [m for u in get_target_users(conn, args.user) if (m := reminder(conn, u["id"]))]
    conn.close()
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
