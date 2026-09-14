"""Ежедневное напоминание об инъекциях по расписанию. Запускается диспетчером в TZ
пользователя. Печатает строки вида "💉 Сегодня инъекция: ..." или "💉 Инъекция просрочена..."
только для пользователей, у которых есть инъекции в med_schedule.

    python scripts/injection_reminder.py
    python scripts/injection_reminder.py --user 3
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from health_core.config import user_today
from health_core.db import connect, migrate


def main() -> int:
    ap = argparse.ArgumentParser(description="Напоминание об инъекциях")
    ap.add_argument("--user", type=int, help="только этот db user id (без флага — все)")
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

    blocks = []

    for u in users:
        try:
            today = user_today(conn, u["id"])
            # Запрос инъекций, расписанных для этого пользователя
            rows = conn.execute(
                "SELECT substance, dose, unit, next_at FROM med_schedule "
                "WHERE user_id=? AND route='injection' AND next_at IS NOT NULL "
                "ORDER BY next_at",
                (u["id"],)
            ).fetchall()

            for row in rows:
                substance = row["substance"]
                dose = row["dose"]
                unit = row["unit"] or ""
                next_at = row["next_at"]

                # next_at формат: "2026-09-13 22:00:00"
                scheduled_date = next_at[:10]  # "2026-09-13"
                scheduled_time = next_at[11:16]  # "22:00"

                if scheduled_date == today:
                    # Инъекция сегодня
                    dose_str = f"{dose:g}" if isinstance(dose, (int, float)) else str(dose)
                    unit_str = f" {unit}" if unit else ""
                    blocks.append(f"💉 Сегодня инъекция: {substance} {dose_str}{unit_str} в {scheduled_time}.")
                elif scheduled_date < today:
                    # Инъекция просрочена
                    dd_mm = f"{scheduled_date[8:10]}.{scheduled_date[5:7]}"
                    blocks.append(f"💉 Инъекция {substance} просрочена с {dd_mm}. Отметь приём или сдвинь расписание.")
        except Exception as e:
            blocks.append(f"user_id={u['id']}: ошибка при проверке инъекций: {e}")

    conn.close()

    # Печать только если есть что печатать (иначе пустой stdout = нет сообщения)
    if blocks:
        print("\n".join(blocks))

    return 0


if __name__ == "__main__":
    sys.exit(main())
