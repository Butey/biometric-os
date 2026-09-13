"""Диспетчер уведомлений по часовым поясам людей.

Таймер запускает его раз в 10 минут. Для каждого зарегистрированного человека
берётся его локальное время (users.timezone, иначе schedule.default_timezone),
и отправляются задачи из config.yaml::schedule.jobs, чей слот наступил не
позже grace_minutes назад. dispatch_log гарантирует одну отправку на слот.

    python scripts/dispatch.py              # боевой прогон
    python scripts/dispatch.py --dry-run    # что ушло бы сейчас, без отправки и записи
    python scripts/dispatch.py --selftest
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

import notify  # noqa: E402 — рядом лежащий скрипт; он же добавляет корень проекта в sys.path

from health_core.config import load  # noqa: E402
from health_core.db import connect, migrate  # noqa: E402

DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def zone(name: str | None) -> ZoneInfo | None:
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def due(jobs: list[dict], local_now: datetime, grace_min: int) -> list[tuple[str, str]]:
    """(script, slot_key) задач, чей слот в [слот, слот + grace)."""
    out = []
    for job in jobs:
        hh, mm = map(int, job["at"].split(":"))
        slot = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if job.get("days") and DOW[slot.weekday()] not in job["days"]:
            continue
        if slot <= local_now < slot + timedelta(minutes=grace_min):
            out.append((job["script"], f"{job['script']}@{slot:%Y-%m-%d %H:%M}"))
    return out


def claim(conn, user_id: int, slot_key: str) -> bool:
    """Застолбить слот ДО отправки. ponytail: упавшая отправка не повторяется —
    сбой уходит админам через notify.report_failure; ретраи — если начнут терять."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO dispatch_log(user_id, slot_key, sent_at) VALUES (?, ?, ?)",
        (user_id, slot_key, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    return cur.rowcount == 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Уведомления по локальному времени каждого человека")
    ap.add_argument("--dry-run", action="store_true", help="показать, что ушло бы, ничего не отправляя")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    cfg = load().get("schedule", {})
    default_tz = zone(cfg.get("default_timezone"))

    token = None
    if not args.dry_run:
        notify.load_env_file()
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            print("TELEGRAM_BOT_TOKEN не задан (~/.hermes/.env)", file=sys.stderr)
            return 1

    conn = connect()
    migrate(conn)
    conn.execute("DELETE FROM dispatch_log WHERE sent_at < datetime('now', '-30 day')")
    users = conn.execute(
        "SELECT id, telegram_user_id, timezone FROM users "
        "WHERE telegram_user_id IS NOT NULL AND telegram_user_id != ''"
    ).fetchall()

    had_error = False
    for u in users:
        tz = zone(u["timezone"]) or default_tz
        if tz is None:
            print(f"user_id={u['id']}: нет пояса и нет schedule.default_timezone — пропуск", file=sys.stderr)
            continue
        local = datetime.now(tz).replace(tzinfo=None)
        for script, slot_key in due(cfg.get("jobs", []), local, cfg.get("grace_minutes", 25)):
            if args.dry_run:
                print(f"user_id={u['id']} {tz.key} {local:%H:%M}: {slot_key}")
                continue
            if not claim(conn, u["id"], slot_key):
                continue
            had_error |= notify.deliver(token, f"scripts/{script}.py", [(u["id"], str(u["telegram_user_id"]))])
    conn.close()
    return 1 if had_error else 0


def selftest() -> int:
    import sqlite3

    import health_core.db as db

    jobs = [
        {"script": "morning_checkin", "at": "08:00"},
        {"script": "injection_reminder", "at": "21:00", "days": ["sun"]},
    ]
    sun = datetime(2026, 9, 13, 8, 7)  # воскресенье
    assert due(jobs, sun, 25) == [("morning_checkin", "morning_checkin@2026-09-13 08:00")]
    assert due(jobs, sun.replace(minute=25), 25) == [], "за окном grace слот не шлём"
    assert due(jobs, sun.replace(hour=7, minute=59), 25) == [], "раньше слота не шлём"
    assert due(jobs, sun.replace(hour=21, minute=10), 25) == [("injection_reminder", "injection_reminder@2026-09-13 21:00")]
    assert due(jobs, datetime(2026, 9, 14, 21, 10), 25) == [], "в понедельник воскресной задачи нет"

    # один и тот же UTC-момент — у разных поясов разные слоты
    utc = datetime(2026, 9, 13, 3, 5, tzinfo=timezone.utc)
    yekt = utc.astimezone(ZoneInfo("Asia/Yekaterinburg")).replace(tzinfo=None)  # 08:05
    msk = utc.astimezone(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)        # 06:05
    assert due(jobs, yekt, 25) and not due(jobs, msk, 25)

    assert zone("Nowhere/City") is None and zone(None) is None

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.DDL)
    conn.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (1, '2026-09-01 00:00:00')")
    assert claim(conn, 1, "morning_checkin@2026-09-13 08:00") is True
    assert claim(conn, 1, "morning_checkin@2026-09-13 08:00") is False, "второй запуск в окне не шлёт повторно"
    print("dispatch: ok — слоты, дни недели, пояса, дедупликация")
    return 0


if __name__ == "__main__":
    sys.exit(main())
