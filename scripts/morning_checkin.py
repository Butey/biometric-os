"""§11 08:00 — утренний чек-ин, всегда, --no-agent. Печатает report.status_bar()
для каждого пользователя; сама строка не пересказывается моделью (§07).

    python scripts/morning_checkin.py
    python scripts/morning_checkin.py --user 3   # только этот пользователь (для notify.py --all)
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # VPS-локаль не гарантирована, тут кириллица

from health_core.db import connect, migrate
from health_core.report import status_bar
from health_core.guards import check_all
from health_core.chrono import caffeine_cutoff
from health_core.meds import stock_runs_out
from health_core import sick
from health_core import refeed
from health_core.config import user_today
from health_core.watch import steps_on, step_goal

sys.path.insert(0, str(Path(__file__).resolve().parent))  # соседние скрипты
from injection_reminder import injection_block  # noqa: E402


def steps_line(steps: int | None, goal: int | None) -> str | None:
    """Returns a formatted line about yesterday's steps, or None if steps data is missing.

    Args:
        steps: number of steps, or None
        goal: step goal, or None

    Returns:
        Formatted string or None
    """
    if steps is None:
        return None

    if goal is not None:
        checkmark = " ✓" if steps >= goal else ""
        return f"Шаги вчера: {steps} из {goal}{checkmark}"

    return f"Шаги вчера: {steps}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Утренний чек-ин")
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

    blocks = []
    for u in users:
        try:
            block_lines = [status_bar(conn, u["id"])]
            today = user_today(conn, u["id"])
            # Шаги вчера
            yesterday_date = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            steps = steps_on(conn, u["id"], yesterday_date)
            goal = step_goal(conn, u["id"], yesterday_date)
            steps_txt = steps_line(steps, goal)
            if steps_txt:
                block_lines.append(steps_txt)
            # Кофеиновое окно
            cutoff = caffeine_cutoff(conn, u["id"])
            if cutoff is not None:
                block_lines.append(f"☕ Кофеин — последний не позже {cutoff}.")
            # BINGE_RISK alert
            alerts = check_all(conn, u["id"])
            for alert in alerts:
                if alert.get("code") == "BINGE_RISK":
                    block_lines.append(f"⚠ {alert['message']}")
            # Запас препарата (CONTEXT.md «Запас препарата»)
            for w in stock_runs_out(conn, u["id"]):
                ra = w["runs_out_at"]
                block_lines.append(f"💊 {w['substance']}: запаса хватит до {ra[8:10]}.{ra[5:7]} — пополни")
            st = sick.status(conn, u["id"], today)
            if st["sick"]:
                until = st["until"]
                until_dd_mm = f"{until[8:10]}.{until[5:7]}" if until else "?"
                block_lines.append(
                    f"🤒 Режим болезни до {until_dd_mm}: цель без дефицита, "
                    "напоминания о еде выключены."
                )
            # Рефид: уведомление за 2 дня, в день, в последний день
            ref_st = refeed.status(conn, u["id"], today)
            if ref_st.get("notify"):
                block_lines.append(ref_st["notify"])
            # Инъекции
            inj_block = injection_block(conn, u, today)
            if inj_block:
                block_lines.append(inj_block)
            blocks.append("\n".join(block_lines))
        except Exception as e:
            blocks.append(f"user_id={u['id']}: чек-ин не удался: {e}")
    conn.close()

    print("\n\n".join(blocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
