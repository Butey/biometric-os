"""Разовая CLI-миграция истории в health.db (§12): весы, тренировки, антропометрия.
Запускается при остановленном демоне. Идемпотентна — повторный запуск ничего не меняет.

    python migrate.py --user 1 --scale <path_or_dir> --tcx <dir> --anthro <path.csv>

Каждый из --scale/--tcx/--anthro необязателен — выполняются только переданные стадии.
"""
import argparse
import sys
from pathlib import Path

from datetime import datetime

from health_core.config import load
from health_core.db import connect, migrate
from health_core.ingest import anthro, scale, tcx


def _print_stage(name: str, added: int, skipped: int) -> None:
    print(f"{name:<8} added={added:<6} skipped={skipped}")


def run_scale(conn, user_id: int, path: str) -> None:
    p = Path(path)
    files = sorted(p.rglob("*")) if p.is_dir() else [p]
    files = [f for f in files if f.is_file() and f.suffix.lower() in (".xlsx", ".xls", ".csv")]
    added = skipped = 0
    for f in files:
        r = scale.import_export(conn, user_id, str(f))
        added += r["added"]
        skipped += r["skipped"]
    _print_stage("scale", added, skipped)
    _check_daily_jumps(conn, user_id)


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")


def _check_daily_jumps(conn, user_id: int) -> None:
    """§12 «Проверки после импорта»: суточный скачок веса больше порога — признак
    не схлопнутого дубля серии."""
    limit = load().get("ingest", {}).get("max_daily_weight_jump_kg", 3)
    rows = conn.execute(
        "SELECT measured_at, weight_kg FROM body_metrics WHERE user_id=? ORDER BY measured_at",
        (user_id,),
    ).fetchall()
    prev = None
    for row in rows:
        if prev is not None:
            # Порог суточный, а замеры идут с любым разрывом. Нормируем на сутки,
            # иначе честные 3.8 кг за четыре дня читаются как ошибка данных.
            # Разрыв меньше суток зажимаем в 1.0 — несхлопнутый дубль серии
            # отстоит на минуты, и делить на него нельзя.
            gap_days = max(
                (_parse_ts(row["measured_at"]) - _parse_ts(prev["measured_at"])).total_seconds() / 86400.0,
                1.0,
            )
            delta = abs(row["weight_kg"] - prev["weight_kg"])
            if delta / gap_days > limit:
                print(f"WARNING: скачок веса {prev['weight_kg']}->{row['weight_kg']} кг "
                      f"между {prev['measured_at']} и {row['measured_at']} "
                      f"({delta / gap_days:.1f} кг/сут > {limit})")
        prev = row


def run_tcx(conn, user_id: int, path: str) -> None:
    p = Path(path)
    files = sorted(p.rglob("*.tcx")) if p.is_dir() else [p]
    added = skipped = 0
    for f in files:
        r = tcx.import_tcx(conn, user_id, str(f))
        added += r["added"]
        skipped += r["skipped"]
    _print_stage("tcx", added, skipped)


def run_anthro(conn, user_id: int, path: str) -> None:
    text = Path(path).read_text(encoding="utf-8")
    records = anthro.parse_text(text)
    r = anthro.import_anthro(conn, user_id, records)
    _print_stage("anthro", r["added"], r["skipped"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", type=int, default=1, help="id из таблицы users (по умолчанию 1)")
    ap.add_argument("--scale", help="файл или каталог с выгрузками весов (.xlsx/.csv)")
    ap.add_argument("--tcx", help="каталог с .tcx тренировками")
    ap.add_argument("--anthro", help="файл с антропометрией лентой")
    args = ap.parse_args()

    if not (args.scale or args.tcx or args.anthro):
        ap.error("укажите хотя бы одну стадию: --scale, --tcx или --anthro")

    conn = connect()
    migrate(conn)

    row = conn.execute("SELECT id FROM users WHERE id=?", (args.user,)).fetchone()
    if row is None:
        print(f"user_id={args.user} не найден в users — сначала зарегистрируйте пользователя", file=sys.stderr)
        sys.exit(1)

    if args.scale:
        run_scale(conn, args.user, args.scale)
    if args.tcx:
        run_tcx(conn, args.user, args.tcx)
    if args.anthro:
        run_anthro(conn, args.user, args.anthro)

    conn.close()


if __name__ == "__main__":
    main()
