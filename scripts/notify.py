"""Доставка вывода cron-скрипта в телеграм. Раньше это делал шлюз Hermes,
теперь — двадцать строк на stdlib.

    python scripts/notify.py scripts/morning_checkin.py --all
    python scripts/notify.py scripts/injection_reminder.py --to 374939064

Сами cron-скрипты не меняются: они как печатали в stdout, так и печатают.
Здесь только запуск, сбор вывода и sendMessage.
"""
import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import truststore          # TLS-перехват в сети, см. комментарий в bot/main.py

    truststore.inject_into_ssl()
except ImportError:
    pass

import os

from admin.auth import load_env_file
from health_core.config import load as load_config
from health_core.db import connect

TELEGRAM_LIMIT = 4096


def send(token: str, chat_id: str, text: str) -> None:
    """Один POST. Ошибку телеграма не глотаем: cron пишет stderr в лог, и это
    единственный способ узнать, что напоминания перестали доходить."""
    for start in range(0, len(text), TELEGRAM_LIMIT):
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text[start:start + TELEGRAM_LIMIT],
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        if not body.get("ok"):
            print(f"telegram отказал для {chat_id}: {body}", file=sys.stderr)


def admin_ids() -> list[str]:
    return [str(i) for i in (load_config().get("admin", {}).get("telegram_admin_ids") or [])]


def all_registered_users() -> list[tuple[int, str]]:
    """(db user id, telegram id) для всех зарегистрированных — основа --all:
    каждому свой прогон скрипта с --user, чтобы чужие данные не утекали."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, telegram_user_id FROM users "
            "WHERE telegram_user_id IS NOT NULL AND telegram_user_id != ''"
        ).fetchall()
    finally:
        conn.close()
    return [(r["id"], str(r["telegram_user_id"])) for r in rows]


def target_for(telegram_id: str) -> tuple[int | None, str]:
    """--to: если этот telegram id зарегистрирован — гоняем скрипт с его
    --user, а не общей простынёй (иначе получатель увидит чужие данные)."""
    conn = connect()
    try:
        row = conn.execute("SELECT id FROM users WHERE telegram_user_id=?", (telegram_id,)).fetchone()
    finally:
        conn.close()
    return (row["id"] if row else None, telegram_id)


def run_script(script: str, user_id: int | None) -> subprocess.CompletedProcess:
    """Один прогон cron-скрипта. С user_id пробуем --user; если скрипт этот
    флаг не знает, argparse дочернего процесса отвечает кодом 2 и упоминает
    --user в stderr — это не сбой скрипта, а просто «не умеет делить вывод по
    людям» (injection_reminder.py). Тогда гоняем как есть, без --user: тишина
    хуже, чем разовая общая простыня для скрипта, где деления по людям и так нет.
    Отличаем так, а не по имени файла, чтобы новый cron-скрипт без --user не
    провалился молча."""
    cmd = [sys.executable, str(ROOT / script)]
    env = None
    if user_id is not None:
        cmd += ["--user", str(user_id)]
        tz = _user_tz(user_id)
        if tz:
            # Скрипты считают «сегодня» и час окна через datetime.now(): TZ в
            # окружении переводит весь дочерний процесс на пояс человека.
            env = {**os.environ, "TZ": tz}
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT), env=env)
    if user_id is not None and proc.returncode == 2 and "--user" in proc.stderr:
        proc = subprocess.run(cmd[:1] + [str(ROOT / script)],
                               capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT), env=env)
    return proc


def _user_tz(user_id: int) -> str | None:
    """Пояс из профиля; неизвестное имя glibc молча превратил бы в UTC — отбрасываем."""
    conn = connect()
    try:
        row = conn.execute("SELECT timezone FROM users WHERE id=?", (user_id,)).fetchone()
    finally:
        conn.close()
    name = (row["timezone"] if row else None) or load_config().get("schedule", {}).get("default_timezone")
    try:
        return str(ZoneInfo(name)) if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def report_failure(script: str, proc: subprocess.CompletedProcess, token: str) -> None:
    # Сбой уходит админам, а не пользователям: человеку на диете незачем
    # видеть трейсбек, а админу без него не починить.
    text = f"{script} упал (код {proc.returncode}):\n{proc.stderr.strip()[:3000]}"
    print(text, file=sys.stderr)
    for admin_id in admin_ids():
        send(token, admin_id, text)


def deliver(token: str, script: str, targets: list[tuple[int | None, str]]) -> bool:
    """targets — (db user id или None, telegram id). Один и тот же user_id
    (например None у всех админов) гоняем один раз и переиспользуем — иначе
    export_backup.py под --admins сделает по бэкапу на каждого админа."""
    had_error = False
    cache: dict[int | None, subprocess.CompletedProcess] = {}
    for user_id, chat_id in targets:
        if user_id not in cache:
            proc = run_script(script, user_id)
            cache[user_id] = proc
            if proc.returncode != 0:
                had_error = True
                report_failure(script, proc, token)
        proc = cache[user_id]
        if proc.returncode != 0:
            continue
        text = proc.stdout.strip()
        if not text:
            # Пустой вывод — штатное «сегодня сказать нечего» (например,
            # meal_window_check вне окна). Слать пустое сообщение нельзя.
            continue
        try:
            send(token, chat_id, text)
        except urllib.error.URLError as e:
            print(f"не доставлено {chat_id}: {e}", file=sys.stderr)
    return had_error


def main() -> int:
    ap = argparse.ArgumentParser(description="Запустить cron-скрипт и отправить его вывод в телеграм")
    ap.add_argument("script", help="путь к скрипту, например scripts/morning_checkin.py")
    ap.add_argument("--to", help="telegram id одного получателя")
    ap.add_argument("--all", action="store_true", help="всем зарегистрированным пользователям")
    ap.add_argument("--admins", action="store_true", help="только администраторам из config.yaml")
    args = ap.parse_args()
    if not (args.to or args.all or args.admins):
        ap.error("нужен --to <id>, --all или --admins")

    load_env_file()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN не задан (~/.hermes/.env)", file=sys.stderr)
        return 1

    if args.admins:
        # Технические отчёты (бэкап) человеку на диете не нужны и выглядят
        # как поломка. Один прогон без --user, общий текст — всем админам.
        targets = [(None, admin_id) for admin_id in admin_ids()]
    elif args.all:
        targets = all_registered_users()
    else:
        targets = [target_for(args.to)]

    had_error = deliver(token, args.script, targets)
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
