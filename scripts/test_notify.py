"""Проверка приватности рассылки: notify.py --all не должен смешивать
получателей. Без сети — send() подменяется на сборщик (chat_id, text).

    PYTHONIOENCODING=utf-8 python scripts/test_notify.py

HEALTH_DB указывает на временный файл ДО импорта health_core.db — боевую
~/.hermes/health.db не трогаем.
"""
import io
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["HEALTH_DB"] = _tmp_db.name
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")  # notify.main() требует непустой токен

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))
sys.stdout.reconfigure(encoding="utf-8")

import notify  # noqa: E402
from health_core.db import connect, migrate  # noqa: E402

conn = connect()
migrate(conn)
conn.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (111, '2026-08-20 00:00:00')")
conn.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (222, '2026-08-20 00:00:00')")
conn.commit()
uid1 = conn.execute("SELECT id FROM users WHERE telegram_user_id=111").fetchone()["id"]
uid2 = conn.execute("SELECT id FROM users WHERE telegram_user_id=222").fetchone()["id"]
conn.execute(
    "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg) VALUES (?, 'b1', '2026-08-22 07:00:00', 70.0)",
    (uid1,),
)
conn.execute(
    "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg) VALUES (?, 'b2', '2026-08-22 07:00:00', 90.0)",
    (uid2,),
)
conn.commit()
conn.close()

sent = []
notify.send = lambda token, chat_id, text: sent.append((chat_id, text))


def run(argv):
    sent.clear()
    sys.argv = ["notify.py"] + argv
    return notify.main()


# --- A: главный тест — реальный cron-скрипт, --all не путает пользователей ---
rc = run(["scripts/morning_checkin.py", "--all"])
assert rc == 0, f"morning_checkin --all упал: rc={rc}"
assert len(sent) == 2, f"ожидали 2 отправки, получили {len(sent)}: {sent}"
by_chat = dict(sent)
assert set(by_chat) == {"111", "222"}, by_chat
assert "70.0" in by_chat["111"] and "90.0" not in by_chat["111"], by_chat["111"]
assert "90.0" in by_chat["222"] and "70.0" not in by_chat["222"], by_chat["222"]
print("OK: --all — каждому только свой текст, чужие данные не утекают")

# --- фикстуры для остальных случаев: свои маленькие скрипты, ROOT подменяется ---
fixtures_dir = Path(tempfile.mkdtemp())
counter_path = fixtures_dir / "calls.txt"

(fixtures_dir / "empty_for_one.py").write_text(
    "import argparse\n"
    "ap = argparse.ArgumentParser()\n"
    "ap.add_argument('--user', type=int)\n"
    "a = ap.parse_args()\n"
    f"if a.user != {uid1}:\n"
    "    print(f'text for user {a.user}')\n",
    encoding="utf-8",
)
(fixtures_dir / "no_user_flag.py").write_text(
    "import argparse\n"
    "argparse.ArgumentParser().parse_args()\n"  # --user не объявлен -> argparse сам откажет кодом 2
    "print('shared text, no per-user split')\n",
    encoding="utf-8",
)
(fixtures_dir / "admin_report.py").write_text(
    "from pathlib import Path\n"
    f"p = Path(r'{counter_path}')\n"
    "p.write_text((p.read_text(encoding='utf-8') if p.exists() else '') + 'x', encoding='utf-8')\n"
    "print('admin report')\n",
    encoding="utf-8",
)

orig_root = notify.ROOT
notify.ROOT = fixtures_dir
try:
    # --- B: пустой вывод для одного пользователя не порождает отправку ему ---
    rc = run(["empty_for_one.py", "--all"])
    assert rc == 0, f"empty_for_one --all упал: rc={rc}"
    assert len(sent) == 1, f"ожидали 1 отправку (второй пуст), получили: {sent}"
    assert sent[0][0] == "222", sent
    assert sent[0][1] == f"text for user {uid2}", sent
    print("OK: пустой вывод для получателя — тишина именно ему, не всем")

    # --- D: скрипт без --user всё равно доставляется (не тишина) ---
    rc = run(["no_user_flag.py", "--all"])
    assert rc == 0, f"no_user_flag --all упал: rc={rc}"
    assert len(sent) == 2, f"ожидали 2 отправки, получили {len(sent)}: {sent}"
    texts = {t for _, t in sent}
    assert texts == {"shared text, no per-user split"}, sent
    assert {c for c, _ in sent} == {"111", "222"}, sent
    print("OK: скрипт без --user доставляется всем общим текстом, не молчит")

    # --- C: --admins — один общий текст всем админам, скрипт гоняется один раз ---
    notify.load_config = lambda: {"admin": {"telegram_admin_ids": [501, 502]}}
    rc = run(["admin_report.py", "--admins"])
    assert rc == 0, f"admin_report --admins упал: rc={rc}"
    assert len(sent) == 2, f"ожидали 2 отправки админам, получили {len(sent)}: {sent}"
    assert {c for c, _ in sent} == {"501", "502"}, sent
    assert {t for _, t in sent} == {"admin report"}, sent
    assert counter_path.read_text(encoding="utf-8") == "x", "скрипт должен был выполниться ровно один раз на --admins"
    print("OK: --admins — общий текст, один прогон скрипта на всех админов")

    # --- E: упавший скрипт - админам уходит и его stdout (причина, напечатанная до падения) ---
    (fixtures_dir / "loud_fail.py").write_text(
        "print('ВНИМАНИЕ: громкая строка')\nraise SystemExit(1)\n", encoding="utf-8")
    rc = run(["loud_fail.py", "--admins"])
    assert rc == 1, f"loud_fail --admins должен вернуть 1, rc={rc}"
    assert len(sent) == 2 and all("ВНИМАНИЕ: громкая строка" in t for _, t in sent), sent
    print("OK: упавший скрипт - stdout доходит админам")

    # --- F: notify.py - --admins шлёт текст со stdin (health-alert@.service) ---
    sys.stdin = io.TextIOWrapper(io.BytesIO("Сбой юнита x\nстрока журнала".encode("utf-8")), encoding="utf-8")
    rc = run(["-", "--admins"])
    assert rc == 0 and {c for c, _ in sent} == {"501", "502"}, (rc, sent)
    assert {t for _, t in sent} == {"Сбой юнита x\nстрока журнала"}, sent
    print("OK: '-' --admins - текст со stdin уходит админам")
finally:
    notify.ROOT = orig_root

# --- G: сводка журнала для export_backup.py (чистая функция, без journalctl) ---
import export_backup  # noqa: E402

P = "2026-10-02 01:20:49,531 INFO bot.llm tool "
sample = "\n".join([
    P + 'log_water({"action":"add","ml":500}) -> {"water_id": 114, "water_ml": 1000.0}',
    P + 'food_lookup({"action":"search","name":"тушёная индейка"}) -> {"error": "Open Food Facts недоступен: HTTP Error 503"}',
    P + 'food_lookup({"action":"search","name":"творог"}) -> {"error": "Open Food Facts недоступен: HTTP Error 503"}',
    P + 'pantry({"action":"remove","name":"X"}) -> {"error": "\'X\' нет в холодильнике"} [ПРАВИЛА pantry] текст...(+192)',
    P + 'log_sleep({"action":"add","notes":"сон"}) -> {"saved": true, "warning": "сон короче 4 ч"}',
    "2026-10-02 02:00:00,000 ERROR bot config.yaml отклонён: нет bot.providers",
    "2026-10-02 02:00:01,000 ERROR aiogram.dispatcher Failed to fetch updates - TelegramConflictError: Conflict",
    "2026-10-02 02:00:02,000 ERROR aiogram.dispatcher Failed to fetch updates - TelegramConflictError: Conflict",
    "2026-10-02 02:00:03,000 INFO bot отправлено chat=1 message_id=2 (3 симв)",
])
got = export_backup.journal_summary(sample)
assert got[0] == ("Журнал за 24 ч: вызовов инструментов 5, ошибок 3, "
                  "config.yaml отклонён 1, TelegramConflictError 2."), got[0]
assert got[1] == ("Частые ошибки: food_lookup | Open Food Facts недоступен: HTTP Error 503 (2); "
                  "pantry | 'X' нет в холодильнике (1)"), got[1]
assert len(got) == 2 and "saved" not in got[1], got
assert len(export_backup.journal_summary("")) == 1
print("OK: сводка журнала - вызовы, ошибки, топ, отклонённый конфиг, Conflict; saved+warning не ошибка")

ok, verdict = export_backup.check_copy(_tmp_db.name)
assert ok and verdict == "ok, users 2, food_log 0", (ok, verdict)
_bad = Path(tempfile.mkdtemp()) / "bad.db"
_bad.write_bytes(b"not a sqlite file" * 100)
ok, verdict = export_backup.check_copy(str(_bad))
assert not ok and verdict, (ok, verdict)
ok, verdict = export_backup.check_copy(str(_bad.parent / "missing.db"))
assert not ok and "не открывается" in verdict, (ok, verdict)
print("OK: проверка копии - здоровая ok, мусор и отсутствующий файл не ok")

os.unlink(_tmp_db.name)
print("ВСЕ ПРОВЕРКИ ПРОШЛИ")
