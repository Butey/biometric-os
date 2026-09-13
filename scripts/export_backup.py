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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from health_core.db import DB_PATH, connect, migrate
from health_core.export import backup_db, export_all, rclone_push


def main() -> int:
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
        total_rows = 0
        for u in sync_users:
            result = export_all(conn, u["id"], export_dir)
            total_rows += result["rows"]
        tail = f" Не синхронизируются: {skipped} польз." if skipped else ""
        lines.append(f"Экспорт: {total_rows} строк в {export_dir}.{tail}")
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

    # Заливка на Диск — последней: провал rclone не должен отменить ни экспорт,
    # ни бэкап, ни отправку копии в Telegram. Но и молчать о провале нельзя,
    # иначе "бэкап есть" будет означать "бэкап, возможно, есть".
    if remote:
        try:
            if export_dir and Path(export_dir).is_dir():
                rclone_push(export_dir, remote)
            rclone_push(str(Path(backup_path).parent), remote + "/backups")
            lines.append(f"Диск: витрина и бэкап залиты в {remote}.")
        except Exception as e:
            lines.append(f"⚠️ Диск: заливка не удалась — {e}")

    lines.append(f"MEDIA:{backup_path}")

    conn.close()
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
