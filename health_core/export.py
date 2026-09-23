"""Экспорт и бэкап §13. SQLite — единственный источник истины, экспорт только
читает из неё и пишет вовне (Диск-витрина), обратного чтения нет.

Структура вывода экспорта повторяет дерево из ТЗ:
    Metrics/body_metrics.csv
    Metrics/Sugar/sugar_log.csv
    Nutrition/daily_logs/YYYY-MM-DD.md   (один файл на дату с записями еды)

`health.db` в это дерево не входит — его копия делает backup_db() отдельно
(§13 «второй контур»), через sqlite3.Connection.backup(), а не copyfile: файл
может быть открыт демоном в это же время (WAL), и сырое копирование дало бы
рваный снимок.
"""
import csv
import shutil
import sqlite3
import subprocess
from datetime import date
from pathlib import Path

from health_core.config import load
from health_core.report import day_summary

_BODY_METRICS_COLS = [
    "measured_at", "weight_kg", "fat_pct", "bmi", "skeletal_muscle_pct",
    "muscle_mass_kg", "protein_pct", "device_bmr_kcal", "ffm_kg",
    "subcutaneous_fat_pct", "visceral_fat", "water_pct", "bone_mass_kg",
    "metabolic_age", "device_mac",
]
_SUGAR_COLS = ["at", "mmol_l", "context", "confirmed"]
_ACTIVITY_COLS = ["started_at", "duration_min", "kcal", "avg_hr", "sport", "notes", "source"]
_WATCH_COLS = ["date", "hr_min", "hr_avg", "hr_max", "steps", "active_kcal", "stress_avg", "hrv_ms", "spo2_avg", "spo2_min", "spo2_max", "source"]
_SLEEP_COLS = ["night_date", "bedtime", "wake_time", "duration_min", "deep_min", "rem_min", "awake_min", "quality", "efficiency_pct", "hr_avg", "spo2_avg", "source", "notes"]
_BATCH = 500  # ponytail: слабый VPS — стримим строки, не грузим таблицу целиком


def _write_csv_streamed(conn: sqlite3.Connection, path: Path, table: str, cols: list[str], user_id: int, order_by: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    cur = conn.execute(
        f"SELECT {','.join(cols)} FROM {table} WHERE user_id=? ORDER BY {order_by}", (user_id,)
    )
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        while True:
            batch = cur.fetchmany(_BATCH)
            if not batch:
                break
            for row in batch:
                w.writerow([row[c] for c in cols])
                n += 1
    return n


def _render_day_md(summary: dict) -> str:
    lines = [f"# {summary['date']}", ""]
    if summary["kcal_target"] is not None:
        lines.append(f"Ккал: {summary['kcal_eaten']:.0f} / {summary['kcal_target']:.0f}")
    else:
        lines.append(f"Ккал: {summary['kcal_eaten']:.0f} (цель не рассчитана)")
    lines.append(f"Белки: {summary['protein_g']:.0f} г")
    lines.append(f"Жиры: {summary['fat_g']:.0f} г")
    lines.append(f"Углеводы: {summary['carb_g']:.0f} г")
    water_target = summary.get("water_target_ml")
    if water_target:
        lines.append(f"Вода: {summary['water_ml'] / 1000:.1f} / {water_target / 1000:.1f} л")
    else:
        lines.append(f"Вода: {summary['water_ml'] / 1000:.1f} л")
    if summary["weight_kg"] is not None:
        lines.append(f"Вес: {summary['weight_kg']:.1f} кг")
    if summary["alerts"]:
        lines.append("")
        lines.append("Алерты:")
        for a in summary["alerts"]:
            lines.append(f"- {a['rule']}: {a['message']}")
    lines.append("")
    return "\n".join(lines)


def export_all(conn: sqlite3.Connection, user_id: int, out_dir: str) -> dict:
    """Выгружает дерево витрины §13 для одного пользователя. Пишет CSV/MD с нуля
    каждый раз (перезапись, не дозапись) — повторный вызов идемпотентен, файлы
    не дублируются и не растут. Возвращает {"files": [...], "rows": n}."""
    base = Path(out_dir)
    files: list[str] = []
    rows = 0

    try:
        from openpyxl import Workbook
        has_openpyxl = True
    except ImportError:
        has_openpyxl = False

    def _write_xlsx(path: Path, table: str, cols: list[str], uid: int, order: str):
        if not has_openpyxl: return
        path.parent.mkdir(parents=True, exist_ok=True)
        wb = Workbook(write_only=True)
        ws = wb.create_sheet(title=table)
        ws.append(cols)
        cur = conn.execute(f"SELECT {','.join(cols)} FROM {table} WHERE user_id=? ORDER BY {order}", (uid,))
        for row in cur:
            ws.append([row[c] for c in cols])
        wb.save(path)
        files.append(str(path))

    # Body metrics
    p_csv = base / "Metrics" / "Body composition" / "body_metrics.csv"
    p_xlsx = base / "Metrics" / "Body composition" / "body_metrics.xlsx"
    rows += _write_csv_streamed(conn, p_csv, "body_metrics", _BODY_METRICS_COLS, user_id, "measured_at")
    files.append(str(p_csv))
    _write_xlsx(p_xlsx, "body_metrics", _BODY_METRICS_COLS, user_id, "measured_at")

    # Sugar log
    p_csv = base / "Metrics" / "Sugar" / "sugar_log.csv"
    p_xlsx = base / "Metrics" / "Sugar" / "sugar_log.xlsx"
    rows += _write_csv_streamed(conn, p_csv, "glucose_log", _SUGAR_COLS, user_id, "at")
    files.append(str(p_csv))
    _write_xlsx(p_xlsx, "glucose_log", _SUGAR_COLS, user_id, "at")

    # Activity log
    p_csv = base / "Metrics" / "Activity" / "activity.csv"
    p_xlsx = base / "Metrics" / "Activity" / "activity.xlsx"
    rows += _write_csv_streamed(conn, p_csv, "activity", _ACTIVITY_COLS, user_id, "started_at")
    files.append(str(p_csv))
    _write_xlsx(p_xlsx, "activity", _ACTIVITY_COLS, user_id, "started_at")

    # Daily watch
    p_csv = base / "Metrics" / "Activity" / "daily_watch.csv"
    p_xlsx = base / "Metrics" / "Activity" / "daily_watch.xlsx"
    rows += _write_csv_streamed(conn, p_csv, "daily_watch", _WATCH_COLS, user_id, "date")
    files.append(str(p_csv))
    _write_xlsx(p_xlsx, "daily_watch", _WATCH_COLS, user_id, "date")

    # Sleep log
    p_csv = base / "Metrics" / "Sleep" / "sleep_log.csv"
    p_xlsx = base / "Metrics" / "Sleep" / "sleep_log.xlsx"
    rows += _write_csv_streamed(conn, p_csv, "sleep_log", _SLEEP_COLS, user_id, "night_date")
    files.append(str(p_csv))
    _write_xlsx(p_xlsx, "sleep_log", _SLEEP_COLS, user_id, "night_date")

    # Nutrition logs
    logs_dir = base / "Nutrition" / "daily_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    dates = [r["d"] for r in conn.execute(
        "SELECT DISTINCT date(eaten_at) d FROM food_log WHERE user_id=? ORDER BY d", (user_id,)
    ).fetchall()]
    for d in dates:
        summary = day_summary(conn, user_id, d)
        p = logs_dir / f"{d}.md"
        with open(p, "w", encoding="utf-8") as f:
            f.write(_render_day_md(summary))
        files.append(str(p))
        rows += 1

    return {"files": files, "rows": rows}


_BACKUP_GLOB = "health_????-??-??.db"


def backup_db(db_path: str, out_dir: str, keep: int | None = None) -> str:
    """Онлайн-бэкап через sqlite3 .backup() — безопасен при открытом WAL-соединении
    демона, в отличие от копирования файла (§13 «второй контур»).

    Имя несёт дату, хранится `backup.keep_copies` последних (по умолчанию 7 —
    неделя). Повторный запуск в те же сутки перезаписывает файл этого дня, так
    что ротация идёт по дням, а не по числу запусков."""
    if keep is None:
        keep = load().get("backup", {}).get("keep_copies", 7)
    dest_dir = Path(out_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"health_{date.today().isoformat()}.db"
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # Дата в имени по ISO — лексикографический порядок совпадает с хронологическим.
    stale = sorted(dest_dir.glob(_BACKUP_GLOB))[:-keep] if keep > 0 else []
    for old_file in stale:
        old_file.unlink()
    return str(dest)



# ------------------------------------------------------------------ RCLONE
# Диск как удалённый remote, а не как примонтированная папка: mount на слабом
# VPS — лишний FUSE-демон и ещё одна точка отказа в ночном скрипте. rclone
# ходит по HTTPS, ставится одним бинарником и умеет обе стороны.
#
# Направление по-прежнему одностороннее по смыслу (§13): push выгружает витрину,
# pull скачивает ИСХОДНИКИ (выгрузки весов, .tcx) для импорта в базу. Это не
# обратное чтение витрины — экспортированные CSV никто не читает назад, вторым
# источником истины они не становятся.

_RCLONE_TIMEOUT = 300  # ponytail: одна цифра на обе операции; файлы — сотни КБ


def rclone_available() -> bool:
    return shutil.which("rclone") is not None


def rclone_push(local_dir: str, remote: str) -> str:
    """Заливает каталог витрины в remote (например 'gdrive:Health').

    Бросает RuntimeError с текстом rclone: ночной скрипт обязан отличить
    "Диск не настроен" от "залилось" — молчаливый успех при неудачной заливке
    и есть худший вариант резервного копирования.
    """
    if not rclone_available():
        raise RuntimeError("rclone не установлен (проверьте: rclone version)")
    try:
        proc = subprocess.run(
            ["rclone", "copy", local_dir, remote, "--transfers", "4", "--quiet"],
            capture_output=True, text=True, timeout=_RCLONE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Таймаут rclone ({_RCLONE_TIMEOUT}с)")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip() or f"rclone exit {proc.returncode}")
    return remote


def rclone_pull(remote_path: str, dest_dir: str) -> str:
    """Скачивает ОДИН файл с remote и возвращает локальный путь.

    remote_path — вида 'gdrive:Health/Scale/export.xlsx'. Имя файла сохраняется:
    разбор импорта у нас смотрит на содержимое (magic bytes), но человеку в
    ответе нужно видеть то же имя, что он назвал.
    """
    if not rclone_available():
        raise RuntimeError("rclone не установлен (проверьте: rclone version)")
    name = remote_path.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if not name:
        raise ValueError(f"в пути нет имени файла: {remote_path}")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    local = dest / name
    try:
        proc = subprocess.run(
            ["rclone", "copyto", remote_path, str(local), "--quiet"],
            capture_output=True, text=True, timeout=_RCLONE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Таймаут rclone ({_RCLONE_TIMEOUT}с)")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip() or f"rclone exit {proc.returncode}")
    if not local.is_file():
        raise RuntimeError(f"rclone отработал, но файла нет: {local}")
    return str(local)


def is_remote_path(path: str) -> bool:
    """'gdrive:Health/x.xlsx' — да, 'C:/data/x.xlsx' — нет.

    Буква диска Windows тоже содержит двоеточие, поэтому одиночный символ
    перед ним remote'ом не считаем: иначе локальный путь уедет в rclone.
    """
    head, sep, _ = path.partition(":")
    return bool(sep) and len(head) > 1 and "/" not in head and "\\" not in head


if __name__ == "__main__":
    import os
    import sys
    import tempfile

    sys.stdout.reconfigure(encoding="utf-8")  # Windows cp1251 по умолчанию, тут кириллица

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        os.environ["HEALTH_DB"] = str(tmp_path / "health.db")

        import health_core.db as db
        db.DB_PATH = Path(os.environ["HEALTH_DB"])
        from health_core.db import connect, migrate

        # --- сценарий 1: backup_db() при открытом соединении и незачекпоинченным WAL ---
        conn = connect()
        migrate(conn)
        conn.execute(
            "INSERT INTO users(telegram_user_id, created_at) VALUES (1, '2026-08-20 00:00:00')"
        )
        uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]
        for i in range(5):
            conn.execute(
                "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg) VALUES (?,?,?,?)",
                (uid, f"b-{i}", f"2026-08-{10 + i:02d} 07:00:00", 80.0 + i),
            )
        conn.commit()  # коммит в WAL, БЕЗ checkpoint и БЕЗ закрытия соединения

        backup_dir = tmp_path / "backups"
        backup_path = backup_db(str(db.DB_PATH), str(backup_dir))
        assert Path(backup_path).is_file(), f"backup file missing: {backup_path}"

        check_conn = sqlite3.connect(backup_path)
        check_conn.row_factory = sqlite3.Row
        n = check_conn.execute("SELECT COUNT(*) c FROM body_metrics WHERE user_id=?", (uid,)).fetchone()["c"]
        check_conn.close()
        assert n == 5, f"backup missing pending WAL rows: expected 5, got {n}"
        print(f"OK: backup_db() captured {n} rows from an open connection with pending WAL writes")

        # --- сценарий 2: export_all() на пустой БД — валидные пустые файлы, без исключений ---
        conn.execute(
            "INSERT INTO users(telegram_user_id, created_at) VALUES (2, '2026-08-20 00:00:00')"
        )
        empty_uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=2").fetchone()["id"]
        conn.commit()

        empty_out = tmp_path / "export_empty"
        result_empty = export_all(conn, empty_uid, str(empty_out))
        assert result_empty["rows"] == 0, f"expected 0 rows for empty user, got {result_empty['rows']}"
        bm_csv = (empty_out / "Metrics" / "body_metrics.csv").read_text(encoding="utf-8")
        assert bm_csv.strip() == ",".join(_BODY_METRICS_COLS), f"unexpected empty body_metrics.csv: {bm_csv!r}"
        sugar_csv = (empty_out / "Metrics" / "Sugar" / "sugar_log.csv").read_text(encoding="utf-8")
        assert sugar_csv.strip() == ",".join(_SUGAR_COLS), f"unexpected empty sugar_log.csv: {sugar_csv!r}"
        assert not (empty_out / "Nutrition" / "daily_logs").exists() or \
               list((empty_out / "Nutrition" / "daily_logs").iterdir()) == [], "empty user should have no daily logs"
        print("OK: export_all() on empty DB produced valid empty CSVs and no daily logs, no exception")

        # --- сценарий 3: export_all() дважды в тот же каталог — без порчи/дублей ---
        conn.execute("INSERT INTO food_log(user_id, eaten_at) VALUES (?, '2026-08-20 08:00:00')", (uid,))
        flid = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        conn.execute(
            "INSERT INTO food_items(food_log_id, kcal, protein_g, fat_g, carbs_g) VALUES (?,?,?,?,?)",
            (flid, 500, 30, 20, 40),
        )
        conn.commit()

        dup_out = tmp_path / "export_dup"
        r1 = export_all(conn, uid, str(dup_out))
        r2 = export_all(conn, uid, str(dup_out))
        assert r1 == r2, f"repeated export_all() should be idempotent, got {r1} vs {r2}"
        assert sorted(r1["files"]) == sorted(str(p) for p in dup_out.rglob("*") if p.is_file()), (
            "export produced files not accounted for in the returned manifest (possible duplicates)"
        )
        bm_lines = (dup_out / "Metrics" / "body_metrics.csv").read_text(encoding="utf-8").splitlines()
        assert len(bm_lines) == 1 + 5, f"expected header + 5 rows after two exports, got {len(bm_lines)}"
        print("OK: export_all() called twice into the same directory did not duplicate or corrupt files")

        conn.close()
        check_conn = None
            # --- ротация: старше keep_copies суток не выживает ---
        rot_dir = Path(tmp) / "rotation"
        rot_dir.mkdir()
        for day in range(1, 11):
            (rot_dir / f"health_2026-08-{day:02d}.db").write_bytes(b"stub")
        fresh = backup_db(str(db.DB_PATH), str(rot_dir), keep=7)
        left = sorted(x.name for x in rot_dir.glob(_BACKUP_GLOB))
        assert len(left) == 7, f"ротация оставила {len(left)} копий вместо 7: {left}"
        assert Path(fresh).name in left, "свежая копия удалена ротацией"
        assert "health_2026-08-01.db" not in left, "самая старая копия не удалена"
        sqlite3.connect(fresh).close()
        print(f"OK: ротация оставила {len(left)} копий, самые старые удалены")

    # --- разбор remote-пути: буква диска Windows не должна уехать в rclone ---
    for path_, expected in [
        ("gdrive:Health/Scale/export.xlsx", True), ("gdrive:a.tcx", True),
        ("C:/data/a.xlsx", False), (r"C:\data\a.xlsx", False),
        ("/home/u/a.tcx", False), ("a.xlsx", False),
    ]:
        assert is_remote_path(path_) is expected, f"is_remote_path({path_!r}) != {expected}"
    print("OK: is_remote_path отличает gdrive: от буквы диска и локального пути")

    print("OK: export.py self-check passed")
