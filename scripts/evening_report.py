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
import asyncio
import os
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # для импорта соседних скриптов
sys.stdout.reconfigure(encoding="utf-8")  # VPS-локаль не гарантирована, тут кириллица

from health_core.config import user_today
from health_core.db import connect, migrate, get_target_users
from health_core.report import evening_report
from health_core import council_data
from bot import council as bot_council
from admin.auth import load_env_file
import notify  # рядом лежащий скрипт (scripts/), тот же способ доставки, что и у него


def main() -> int:
    ap = argparse.ArgumentParser(description="Вечерний отчёт")
    ap.add_argument("--user", type=int, help="только этот db user id (без флага — все одной простынёй)")
    args = ap.parse_args()

    conn = connect()
    migrate(conn)
    users = get_target_users(conn, args.user)
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
            # Плато 3+ недели (CONTEXT.md «Консилиум», docs/adr/0003) — созываем
            # консилиум, если по этой причине он не собирался последние 21 день
            # (council.reserve сам держит частоту и лок). Итог — ОТДЕЛЬНОЕ
            # сообщение, тем же способом (notify.send), каким доставляется и
            # обычный вывод этого скрипта.
            if council_data.plateau_3w(conn, u["id"]):
                try:
                    result = asyncio.run(bot_council.run(conn, u["id"], "plateau"))
                    tg_row = conn.execute(
                        "SELECT telegram_user_id FROM users WHERE id=?", (u["id"],)
                    ).fetchone()
                    load_env_file()
                    token = os.environ.get("TELEGRAM_BOT_TOKEN")
                    if tg_row and tg_row["telegram_user_id"] and token:
                        try:
                            notify.send(token, str(tg_row["telegram_user_id"]), result["text"])
                        except (urllib.error.URLError, TimeoutError, OSError) as send_err:
                            print(f"не доставлен совет {u['id']}: {send_err}", file=sys.stderr)
                except ValueError:
                    pass  # уже собирался за последние 21 день или уже идёт — не спамим
        except Exception as e:
            # Отчёт одного человека не должен отменять отчёт остальных.
            blocks.append(f"user_id={u['id']}: отчёт не собрался: {e}")
    conn.close()

    print("\n\n".join(blocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
