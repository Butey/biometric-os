"""§11 03:00 — экспорт и бэкап, всегда, --no-agent.

Экспорт (дерево-витрина §13) идёт в HEALTH_EXPORT_DIR — обычно точка
монтирования Google Диска на VPS. Если переменная не задана или каталог
недоступен, экспорт этой ночи молча пропускается (§13: "работа бота от него
не зависит") — второй контур (локальный бэкап + копия в Telegram) от Диска не
зависит и выполняется в любом случае.

Бэкап пишется через health_core.export.backup_db() (sqlite3 .backup(), не
copyfile — безопасно при открытом WAL-соединении демона) в HEALTH_BACKUP_DIR,
по умолчанию рядом с health.db. Путь бэкапа печатается строкой `MEDIA:<path>`
— Hermes подхватывает такие строки и отправляет файл вложением в Telegram
(§13 «второй контур на случай потери VPS»); .db — из поддерживаемых
расширений (см. Docs/hermes_ref/telegram.md).

    python scripts/export_backup.py
"""
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from health_core.db import DB_PATH, connect, migrate
from health_core.export import backup_db, export_all, rclone_push


def check_copy(path: str) -> tuple[bool, str]:
    """Копия открывается read-only (immutable: иначе рядом остаются -wal/-shm), проходит integrity_check; считаем users и food_log.
    Бэкап, который не проверяли, - это бэкап, возможно, не восстановимый."""
    try:
        c = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            res = c.execute("PRAGMA integrity_check").fetchone()[0]
            users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            food = c.execute("SELECT COUNT(*) FROM food_log").fetchone()[0]
        finally:
            c.close()
    except Exception as e:
        return False, f"не открывается: {e}"
    if res != "ok":
        return False, f"integrity_check: {res}"
    return True, f"ok, users {users}, food_log {food}"


_TOOL_LINE = re.compile(r"bot\.llm tool (\w+)\(.*?\) -> (.*)")
_TOOL_ERR = re.compile(r'\{"error": "((?:[^"\\]|\\.){0,50})')


def journal_summary(text: str) -> list[str]:
    """Строки сводки за сутки из текста журнала бота (journalctl -o cat)."""
    calls = errors = rejected = conflicts = 0
    top = Counter()
    for line in text.splitlines():
        m = _TOOL_LINE.search(line)
        if m:
            calls += 1
            e = _TOOL_ERR.match(m.group(2))
            if e:
                errors += 1
                top[f"{m.group(1)} | {e.group(1)}"] += 1
        rejected += "config.yaml отклонён:" in line
        conflicts += "TelegramConflictError" in line
    out = [f"Журнал за 24 ч: вызовов инструментов {calls}, ошибок {errors}, "
           f"config.yaml отклонён {rejected}, TelegramConflictError {conflicts}."]
    if top:
        out.append("Частые ошибки: " + "; ".join(f"{k} ({n})" for k, n in top.most_common(3)))
    return out


def journal_lines() -> list[str]:
    try:
        r = subprocess.run(["journalctl", "-u", "health-agent", "--since=-24 hours", "--no-pager", "-o", "cat"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"Журнал за 24 ч недоступен: {e}."]
    if r.returncode != 0 or not r.stdout.strip():
        return [f"Журнал за 24 ч недоступен или пуст (код {r.returncode})."]
    return journal_summary(r.stdout)


def main() -> int:
    from admin.auth import load_env_file
    load_env_file()
    conn = connect()
    migrate(conn)
    users = conn.execute("SELECT id FROM users ORDER BY id").fetchall()
    if not users:
        print("Нет ни одного пользователя в БД.", file=sys.stderr)
        conn.close()
        return 1

    lines = []

    # HEALTH_RCLONE_REMOTE задан -> витрина собирается в локальный staging и
    # уходит на Диск по HTTPS. Не задан -> прежнее поведение: пишем прямо в
    # HEALTH_EXPORT_DIR (примонтированная папка), и без неё молча пропускаем.
    remote = os.environ.get("HEALTH_RCLONE_REMOTE")
    export_dir = os.environ.get("HEALTH_EXPORT_DIR")
    if remote and not export_dir:
        export_dir = str(DB_PATH.parent / "export")
        Path(export_dir).mkdir(parents=True, exist_ok=True)

    # Витрина уезжает на личный Google Диск, поэтому синхронизируется НЕ вся
    # база, а только те пользователи, кого включили явно. По умолчанию — один
    # основной (id=1): чужие медицинские выгрузки не должны попадать на чужой
    # диск молча, по факту появления строки в users. Включение остальным —
    # перечислением в ~/.hermes/.env: HEALTH_GDRIVE_USERS=1,3
    raw = os.environ.get("HEALTH_GDRIVE_USERS", "1").replace(",", " ").split()
    sync_ids = {int(x) for x in raw if x.lstrip("-").isdigit()}
    sync_users = [u for u in users if u["id"] in sync_ids]
    skipped = len(users) - len(sync_users)

    if not sync_users:
        lines.append("Экспорт пропущен: в HEALTH_GDRIVE_USERS нет ни одного из пользователей БД.")
    elif export_dir and Path(export_dir).is_dir():
        # Сбой экспорта не должен отменять бэкап этой ночи
        try:
            total_rows = 0
            for u in sync_users:
                result = export_all(conn, u["id"], export_dir)
                total_rows += result["rows"]
            tail = f" Не синхронизируются: {skipped} польз." if skipped else ""
            lines.append(f"Экспорт: {total_rows} строк в {export_dir}.{tail}")
        except Exception as e:
            print(f"Экспорт не удался: {e}", file=sys.stderr)
            lines.append(f"⚠️ Экспорт не удался: {e}")
    else:
        lines.append("Экспорт пропущен: HEALTH_EXPORT_DIR не задан или недоступен (§13).")

    backup_dir = os.environ.get("HEALTH_BACKUP_DIR", str(DB_PATH.parent / "backups"))
    try:
        backup_path = backup_db(str(DB_PATH), backup_dir)
    except Exception as e:
        conn.close()
        print(f"Бэкап не удался: {e}", file=sys.stderr)
        return 1

    lines.append(f"Бэкап: {backup_path}.")
    ok, verdict = check_copy(backup_path)
    lines.append(f"Проверка копии: {verdict}.")
    if not ok:
        lines.insert(0, "ВНИМАНИЕ: БЭКАП НЕ ПРОШЁЛ ПРОВЕРКУ, ВОССТАНОВИТЬ ИЗ НЕГО НЕЛЬЗЯ.")

    # Заливка на Диск — последней: провал rclone не должен отменить ни экспорт,
    # ни бэкап, ни отправку копии в Telegram. Но и молчать о провале нельзя,
    # иначе "бэкап есть" будет означать "бэкап, возможно, есть".
    if remote:
        try:
            if export_dir and Path(export_dir).is_dir():
                rclone_push(export_dir, remote)
            if ok:  # битая копия не должна затереть целую на Диске
                rclone_push(str(Path(backup_path).parent), remote + "/backups")
            lines.append(f"Диск: витрина{' и бэкап' if ok else ''} залиты в {remote}.")
        except Exception as e:
            lines.append(f"⚠️ Диск: заливка не удалась — {e}")

    lines += journal_lines()
    if ok:  # битую копию в телеграм не шлём
        lines.append(f"MEDIA:{backup_path}")

    conn.close()
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
