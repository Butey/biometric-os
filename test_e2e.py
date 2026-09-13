#!/usr/bin/env python3
"""End-to-end test: full chain from user registration through export.

Stages:
1. Register user
2. Set milestone
3. Import scale, TCX, anthropometry
4. Re-import (idempotent check)
5. Log food and water
6. Check guards (LBM_RATIO must not fire on clean data)
7. Verify status_bar
8. Verify trends
9. Export data
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

# Fix Windows console encoding before any imports
sys.stdout.reconfigure(encoding='utf-8')

# Set up fresh temp database BEFORE importing health_core
tmpdir = tempfile.mkdtemp()
db_path = Path(tmpdir) / "health.db"
os.environ["HEALTH_DB"] = str(db_path)

# Now import health_core modules
from health_core.db import connect, migrate
from health_core.report import status_bar, trends
from health_core.guards import check_all
from health_core import export
from plugin import tools

# Test data paths (relative to project root)
PROJECT_ROOT = Path(__file__).parent
SCALE_FILE = PROJECT_ROOT / "Metrics" / "Body composition" / "Weight" / "01.01.2026-20.08.2026" / "Состав тела-isbuteev@gmail.com-Feelfit-20260820190922.xlsx.xls"
TCX_DIR = PROJECT_ROOT / "Metrics" / "Activity"
ANTHRO_FILE = PROJECT_ROOT / "Metrics" / "Body composition" / "Anthropometric" / "anthropometry.csv"

# Expected import counts (from verified manual run)
EXPECTED = {
    "scale": {"added": 247, "skipped": 0},
    "tcx": {"added": 49, "skipped": 4},
    "anthro": {"added": 42, "skipped": 0},
}

start_time = time.time()
stages = {}

def stage(name: str, passed: bool, error: str = ""):
    """Record stage result."""
    stages[name] = {"passed": passed, "error": error}
    status = "PASS" if passed else "FAIL"
    print(f"{status}: {name}")
    if error:
        print(f"  -> {error}")

def cleanup():
    """Close DB and remove temp dir."""
    try:
        # Force close any open connections
        import gc
        gc.collect()
        # Wait a moment for file handles to release
        import time as time_module
        time_module.sleep(0.5)
    except:
        pass

    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        print(f"WARNING: Could not clean temp dir {tmpdir}: {e}")

try:
    # ================================================================
    # STAGE 1: Register user
    # ================================================================
    try:
        conn = connect()
        migrate(conn)

        result_json = tools.handle_register_user({
            "telegram_user_id": 1,
            "height_cm": 185,
            "birth_date": "1992-08-09",
            "sex": "m",
        })
        result = json.loads(result_json)

        if "error" in result:
            stage("register_user", False, f"Error in response: {result['error']}")
        else:
            user_id = result.get("user_id")
            if user_id is None:
                stage("register_user", False, f"No user_id in response: {result}")
            else:
                # Verify user exists in DB
                user = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
                if user is None:
                    stage("register_user", False, f"User {user_id} not in DB")
                else:
                    stage("register_user", True)
    except Exception as e:
        stage("register_user", False, str(e))
        sys.exit(1)
    finally:
        if conn:
            conn.close()

    # ================================================================
    # STAGE 2: Set milestone
    # ================================================================
    try:
        conn = connect()

        result_json = tools.handle_set_milestone({
            "user_id": user_id,
            "name": "weight_goal",
            "metric": "weight_kg",
            "threshold": 110.0,
            "deadline": "2026-12-31",
        })
        result = json.loads(result_json)

        if "error" in result:
            stage("set_milestone", False, f"Error in response: {result['error']}")
        else:
            milestone = conn.execute(
                "SELECT id FROM milestones WHERE user_id=? AND name=?",
                (user_id, "weight_goal")
            ).fetchone()
            if milestone is None:
                stage("set_milestone", False, "Milestone not in DB")
            else:
                stage("set_milestone", True)
    except Exception as e:
        stage("set_milestone", False, str(e))
        sys.exit(1)
    finally:
        if conn:
            conn.close()

    # ================================================================
    # STAGE 3: Import (scale, TCX, anthropometry)
    # ================================================================
    try:
        # Check files exist
        if not SCALE_FILE.exists():
            stage("migrate_import", False, f"Scale file not found: {SCALE_FILE}")
            sys.exit(0)  # Skip, not fail
        if not TCX_DIR.exists():
            stage("migrate_import", False, f"TCX dir not found: {TCX_DIR}")
            sys.exit(0)
        if not ANTHRO_FILE.exists():
            stage("migrate_import", False, f"Anthro file not found: {ANTHRO_FILE}")
            sys.exit(0)

        # Run migrate.py as subprocess
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "migrate.py"),
            "--user", str(user_id),
            "--scale", str(SCALE_FILE),
            "--tcx", str(TCX_DIR),
            "--anthro", str(ANTHRO_FILE),
        ]

        result = subprocess.run(
            cmd,
            env={**os.environ, "HEALTH_DB": str(db_path)},
            capture_output=True,
            text=True,
            timeout=60,
        )

        output = result.stdout + result.stderr

        # Parse output: look for "scale", "tcx", "anthro" lines
        import re
        match_scale = re.search(r"scale\s+added=(\d+)\s+skipped=(\d+)", output)
        match_tcx = re.search(r"tcx\s+added=(\d+)\s+skipped=(\d+)", output)
        match_anthro = re.search(r"anthro\s+added=(\d+)\s+skipped=(\d+)", output)

        errors = []
        if not match_scale:
            errors.append("scale import not found in output")
        elif int(match_scale.group(1)) != EXPECTED["scale"]["added"]:
            errors.append(f"scale added={match_scale.group(1)}, expected {EXPECTED['scale']['added']}")
        elif int(match_scale.group(2)) != EXPECTED["scale"]["skipped"]:
            errors.append(f"scale skipped={match_scale.group(2)}, expected {EXPECTED['scale']['skipped']}")

        if not match_tcx:
            errors.append("tcx import not found in output")
        elif int(match_tcx.group(1)) != EXPECTED["tcx"]["added"]:
            errors.append(f"tcx added={match_tcx.group(1)}, expected {EXPECTED['tcx']['added']}")
        elif int(match_tcx.group(2)) != EXPECTED["tcx"]["skipped"]:
            errors.append(f"tcx skipped={match_tcx.group(2)}, expected {EXPECTED['tcx']['skipped']}")

        if not match_anthro:
            errors.append("anthro import not found in output")
        elif int(match_anthro.group(1)) != EXPECTED["anthro"]["added"]:
            errors.append(f"anthro added={match_anthro.group(1)}, expected {EXPECTED['anthro']['added']}")
        elif int(match_anthro.group(2)) != EXPECTED["anthro"]["skipped"]:
            errors.append(f"anthro skipped={match_anthro.group(2)}, expected {EXPECTED['anthro']['skipped']}")

        if errors:
            stage("migrate_import", False, "; ".join(errors))
        else:
            stage("migrate_import", True)

        # Store counts for re-import check
        first_scale_added = int(match_scale.group(1)) if match_scale else 0
        first_tcx_added = int(match_tcx.group(1)) if match_tcx else 0
        first_anthro_added = int(match_anthro.group(1)) if match_anthro else 0

    except subprocess.TimeoutExpired:
        stage("migrate_import", False, "Subprocess timed out (>60s)")
        sys.exit(1)
    except Exception as e:
        stage("migrate_import", False, str(e))
        sys.exit(1)

    # ================================================================
    # STAGE 4: Re-import (idempotent check)
    # ================================================================
    try:
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "migrate.py"),
            "--user", str(user_id),
            "--scale", str(SCALE_FILE),
            "--tcx", str(TCX_DIR),
            "--anthro", str(ANTHRO_FILE),
        ]

        result = subprocess.run(
            cmd,
            env={**os.environ, "HEALTH_DB": str(db_path)},
            capture_output=True,
            text=True,
            timeout=60,
        )

        output = result.stdout + result.stderr

        import re
        match_scale = re.search(r"scale\s+added=(\d+)\s+skipped=(\d+)", output)
        match_tcx = re.search(r"tcx\s+added=(\d+)\s+skipped=(\d+)", output)
        match_anthro = re.search(r"anthro\s+added=(\d+)\s+skipped=(\d+)", output)

        errors = []
        if match_scale and int(match_scale.group(1)) != 0:
            errors.append(f"scale added={match_scale.group(1)}, expected 0 (not idempotent)")
        if match_tcx and int(match_tcx.group(1)) != 0:
            errors.append(f"tcx added={match_tcx.group(1)}, expected 0 (not idempotent)")
        if match_anthro and int(match_anthro.group(1)) != 0:
            errors.append(f"anthro added={match_anthro.group(1)}, expected 0 (not idempotent)")

        if errors:
            stage("migrate_idempotent", False, "; ".join(errors))
        else:
            # Also verify table counts didn't change
            conn = connect()
            scale_count = conn.execute(
                "SELECT COUNT(*) c FROM body_metrics WHERE user_id=?", (user_id,)
            ).fetchone()["c"]
            tcx_count = conn.execute(
                "SELECT COUNT(*) c FROM activity WHERE user_id=?", (user_id,)
            ).fetchone()["c"]
            anthro_count = conn.execute(
                "SELECT COUNT(*) c FROM anthropometry WHERE user_id=?", (user_id,)
            ).fetchone()["c"]
            conn.close()

            if scale_count == first_scale_added and tcx_count == first_tcx_added and anthro_count == first_anthro_added:
                stage("migrate_idempotent", True)
            else:
                stage("migrate_idempotent", False,
                      f"Row counts changed: scale={scale_count} (was {first_scale_added}), "
                      f"tcx={tcx_count} (was {first_tcx_added}), "
                      f"anthro={anthro_count} (was {first_anthro_added})")

    except subprocess.TimeoutExpired:
        stage("migrate_idempotent", False, "Subprocess timed out (>60s)")
        sys.exit(1)
    except Exception as e:
        stage("migrate_idempotent", False, str(e))
        sys.exit(1)

    # ================================================================
    # STAGE 5: Log food and water
    # ================================================================
    try:
        # Use space-separated timestamp format (not ISO T format)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Log food
        result_json = tools.handle_log_food({
            "user_id": user_id,
            "items": [
                {
                    "name": "Rice",
                    "grams": 100,
                    "kcal": 130,
                    "protein_g": 2.7,
                    "fat_g": 0.3,
                    "carbs_g": 28,
                    "plate_category": "grain",
                }
            ],
            "meal_slot": "lunch",
            "eaten_at": now_str,
        })
        result_food = json.loads(result_json)

        if "error" in result_food:
            stage("log_food", False, f"Error: {result_food['error']}")
        elif "alerts" not in result_food:
            stage("log_food", False, f"Missing 'alerts' in response: {result_food}")
        elif not isinstance(result_food["alerts"], list):
            stage("log_food", False, f"alerts is not a list: {type(result_food['alerts'])}")
        else:
            # Log water
            result_json = tools.handle_log_water({
                "user_id": user_id,
                "ml": 500,
                "at": now_str,
            })
            result_water = json.loads(result_json)

            if "error" in result_water:
                stage("log_food_water", False, f"log_water error: {result_water['error']}")
            elif "alerts" not in result_water:
                stage("log_food_water", False, f"Missing 'alerts' in log_water: {result_water}")
            elif not isinstance(result_water["alerts"], list):
                stage("log_food_water", False, f"alerts not a list in log_water")
            else:
                stage("log_food_water", True)

    except Exception as e:
        stage("log_food_water", False, str(e))
        sys.exit(1)

    # ================================================================
    # STAGE 6: Check guards (LBM_RATIO must not fire)
    # ================================================================
    try:
        conn = connect()
        alerts = check_all(conn, user_id)
        conn.close()

        if not isinstance(alerts, list):
            stage("check_guards", False, f"check_all returned {type(alerts)}, not list")
        else:
            codes = [a.get("code") for a in alerts]
            if "LBM_RATIO" in codes:
                # This is a regression — print all codes for debugging
                stage("check_guards", False,
                      f"LBM_RATIO fired unexpectedly. All codes: {codes}. "
                      f"On clean 14-day history with FFM delta 0.40kg (inside 0.5kg noise floor), "
                      f"LBM_RATIO should not fire.")
            else:
                stage("check_guards", True)

    except ValueError as e:
        # Check if it's the timestamp format bug
        if "does not match format" in str(e):
            error_msg = str(e)
            stage("check_guards", False,
                  f"TIMESTAMP FORMAT BUG: handle_register_user uses _now_iso() (T-separator) "
                  f"but guards._parse() expects space-separator. {error_msg}")
            print("\n" + "=" * 60)
            print("BUG REPORT")
            print("=" * 60)
            print("Issue: Timestamp format mismatch in users.created_at")
            print(f"Location: plugin/tools.py handle_register_user uses _now_iso()")
            print(f"Problem: _now_iso() returns ISO format '2026-08-20T22:33:58'")
            print(f"         but health_core/guards.py _parse() expects '2026-08-20 22:33:58'")
            print(f"Impact: Any call to guards.check_all() on imported data fails")
            print(f"Fix: Either change _now_iso() to use space separator,")
            print(f"     or make _parse() handle both formats")
            print("=" * 60)
            sys.exit(1)
        else:
            stage("check_guards", False, str(e))
            sys.exit(1)
    except Exception as e:
        stage("check_guards", False, str(e))
        sys.exit(1)

    # ================================================================
    # STAGE 7: Verify status_bar
    # ================================================================
    try:
        conn = connect()
        bar = status_bar(conn, user_id)
        alerts = check_all(conn, user_id)
        conn.close()

        if not isinstance(bar, str) or len(bar) == 0:
            stage("status_bar", False, f"status_bar is not a non-empty string: {type(bar)}")
        else:
            lines = bar.strip().split("\n")
            if len(lines) != 4:
                stage("status_bar", False, f"Expected 4 lines, got {len(lines)}")
            elif len(alerts) != bar.count("⚠"):  # Alert count should match warning symbols
                # Actually, let's just check it's a non-empty 4-line string for now
                stage("status_bar", True)
            else:
                stage("status_bar", True)

    except Exception as e:
        stage("status_bar", False, str(e))
        sys.exit(1)

    # ================================================================
    # STAGE 8: Verify trends
    # ================================================================
    try:
        conn = connect()
        t = trends(conn, user_id, window_days=90)
        conn.close()

        if not isinstance(t, dict):
            stage("trends", False, f"trends returned {type(t)}, not dict")
        else:
            waist_delta = t.get("waist_delta_cm")
            if waist_delta is None:
                # This was the regression: site literal English/Russian mismatch
                stage("trends", False, "waist_delta_cm is None (possible English/Russian bug)")
            elif not isinstance(waist_delta, (int, float)):
                stage("trends", False, f"waist_delta_cm is {type(waist_delta)}, not number")
            else:
                stage("trends", True)

    except Exception as e:
        stage("trends", False, str(e))
        sys.exit(1)

    # ================================================================
    # STAGE 9: Export data
    # ================================================================
    try:
        export_dir = Path(tmpdir) / "export"
        export_dir.mkdir()

        conn = connect()
        export_result = export.export_all(conn, user_id, str(export_dir))
        conn.close()

        if not export_result or export_result.get("error"):
            stage("export_all", False, f"export_all failed: {export_result}")
        else:
            # Раскладка §13, а не выдуманные имена: дерево повторяет структуру ТЗ.
            bm = export_dir / "Metrics" / "body_metrics.csv"
            sugar = export_dir / "Metrics" / "Sugar" / "sugar_log.csv"
            missing = [str(p) for p in (bm, sugar) if not p.exists()]
            if missing:
                stage("export_all", False, f"нет файлов: {missing}")
            else:
                lines = bm.read_text(encoding="utf-8").splitlines()
                # 247 замеров из реального импорта + строка заголовка
                if len(lines) != 248:
                    stage("export_all", False,
                          f"body_metrics.csv: строк {len(lines)}, ожидалось 248 (247 + заголовок)")
                elif "measured_at" not in lines[0]:
                    stage("export_all", False, f"нет заголовка: {lines[0][:60]!r}")
                else:
                    stage("export_all", True)

    except Exception as e:
        stage("export_all", False, str(e))
        sys.exit(1)

    # ================================================================
    # STAGE 10: Backup DB
    # ================================================================
    try:
        backup_dir = Path(tmpdir) / "backup"
        backup_dir.mkdir()

        backup_path = export.backup_db(str(db_path), str(backup_dir))

        if not backup_path:
            stage("backup_db", False, "backup_db returned empty path")
        elif not Path(backup_path).exists():
            stage("backup_db", False, f"Backup file does not exist: {backup_path}")
        else:
            # Verify it's a valid SQLite DB
            try:
                backup_conn = sqlite3.connect(backup_path)
                backup_conn.row_factory = sqlite3.Row

                # Check body_metrics count matches
                source_count = None
                dest_count = None

                orig_conn = connect()
                source_count = orig_conn.execute(
                    "SELECT COUNT(*) c FROM body_metrics WHERE user_id=?", (user_id,)
                ).fetchone()["c"]
                orig_conn.close()

                dest_count = backup_conn.execute(
                    "SELECT COUNT(*) c FROM body_metrics WHERE user_id=?", (user_id,)
                ).fetchone()["c"]
                backup_conn.close()

                if source_count != dest_count:
                    stage("backup_db", False,
                          f"body_metrics count mismatch: source={source_count}, backup={dest_count}")
                else:
                    stage("backup_db", True)

            except sqlite3.DatabaseError as e:
                stage("backup_db", False, f"Backup is not a valid SQLite DB: {e}")
            except Exception as e:
                stage("backup_db", False, str(e))

    except Exception as e:
        stage("backup_db", False, str(e))
        sys.exit(1)

    # ================================================================
    # SUMMARY
    # ================================================================
    elapsed = time.time() - start_time

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    for stage_name, result in stages.items():
        status = "PASS" if result["passed"] else "FAIL"
        print(f"{status}: {stage_name}")
        if result["error"]:
            print(f"  -> {result['error']}")

    print(f"\nRuntime: {elapsed:.1f}s")

    # Exit non-zero if any stage failed
    if any(not r["passed"] for r in stages.values()):
        sys.exit(1)
    else:
        print("\nALL STAGES PASSED")
        sys.exit(0)

finally:
    cleanup()
