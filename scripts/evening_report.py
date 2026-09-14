"""§11 21:30 — вечерний отчёт, --no-agent. Печатает report.evening_report()
для каждого пользователя.

    python scripts/evening_report.py
    python scripts/evening_report.py --user 3   # только этот пользователь (для notify.py --all)

Раньше это была единственная задача расписания с вызовом LLM: Hermes брал этот
текст и пересказывал его персоной. Пересказ убран вместе с Hermes — отчёт и так
готовый текст, а ночной вызов модели ради стилистики стоил токенов каждый день.
Вернуть просто: прогнать вывод через bot.llm.run_loop без инструментов.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # VPS-локаль не гарантирована, тут кириллица

from health_core.config import user_today
from health_core.db import connect, migrate
from health_core.report import evening_report


def main() -> int:
    ap = argparse.ArgumentParser(description="Вечерний отчёт")
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
            today = user_today(conn, u["id"])
            blocks.append(evening_report(conn, u["id"], today))
        except Exception as e:
            # Отчёт одного человека не должен отменять отчёт остальных.
            blocks.append(f"user_id={u['id']}: отчёт не собрался: {e}")
    conn.close()

    print("\n\n".join(blocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
