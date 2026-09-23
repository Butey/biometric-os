"""§11 — напоминания об окне приёма пищи, только что закончившемся, --no-agent.

Пустой вывод = запись уже есть (тихий тик, §11): скрипт печатает строку только
для приёма пищи, не записанного (по meal_slot, CONTEXT.md «Приём пищи») в своём
только что закончившемся окне (CONTEXT.md «Окно приёма пищи», health_core/chrono.py).

Какое окно проверять определяется САМИМ скриптом по местному времени каждого
человека (health_core.config.user_now + его личные/общие health_core.chrono.
meal_windows) — окно, чей конец наступил не раньше schedule.grace_minutes назад.
Реальный вызов из dispatch (`--script meal_window_check.py`) идёт без окна
аргументом, всегда с `--user`, как в §11 (`--script meal_window_check.py`). Для
ручного прогона/самопроверки можно задать окно явно:

    python scripts/meal_window_check.py [breakfast|lunch|dinner]
    python scripts/meal_window_check.py --user 3   # только этот пользователь (для notify.py --all)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from health_core.config import load, user_now, user_today
from health_core.chrono import meal_windows
from health_core.db import connect, migrate, get_target_users
from health_core import sick
from health_core.watch import steps_on, step_goal
from math import ceil

_MEAL_NAMES = ("breakfast", "lunch", "dinner")
_LABELS = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин"}


def _just_ended_window(conn, user_id: int) -> str | None:
    """Имя окна (breakfast/lunch/dinner), чей конец наступил в последние
    schedule.grace_minutes минут по местному времени этого человека, иначе None."""
    grace = (load().get("schedule") or {}).get("grace_minutes", 25)
    local = user_now(conn, user_id)
    windows = meal_windows(conn, user_id)
    for name in _MEAL_NAMES:
        end_h, end_m = map(int, windows[name]["end"].split(":"))
        end_dt = local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if 0 <= (local - end_dt).total_seconds() / 60 < grace:
            return name
    return None


def steps_prompt(steps: int | None, goal: int | None) -> str | None:
    """Returns a prompt for logging steps, or None if steps are on goal or unknown.

    Args:
        steps: number of steps today, or None
        goal: step goal, or None

    Returns:
        Prompt string or None
    """
    if steps is None:
        # No steps logged yet
        msg = "Пришли скриншот шагов за сегодня — посмотрю, сколько добрать до цели."
        if goal is not None:
            msg += f" Цель: {goal}."
        return msg

    if goal is not None and steps < goal:
        # Steps below goal
        minutes = ceil((goal - steps) / 100)
        return f"Шагов пока {steps} из {goal} — прогулка ~{minutes} мин добирает."

    # Steps >= goal, or no goal but steps known
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка окна приёма пищи")
    ap.add_argument("window", nargs="?", choices=_MEAL_NAMES,
                     help="окно вручную, иначе — то, что только что закончилось по местному времени человека")
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

    lines = []
    for u in users:
        today = user_today(conn, u["id"])
        if sick.is_sick(conn, u["id"], today):
            continue  # болен — напоминаний о еде не шлём
        name = args.window or _just_ended_window(conn, u["id"])
        if name is None:
            continue  # для этого человека сейчас не конец ни одного его окна — тихий тик
        n = conn.execute(
            "SELECT COUNT(*) c FROM food_log WHERE user_id=? AND date(eaten_at)=? AND meal_slot=?",
            (u["id"], today, name),
        ).fetchone()["c"]
        if n == 0:
            lines.append(f"{_LABELS[name]} не записан.")
        # Steps prompt for lunch window
        if name == "lunch":
            steps = steps_on(conn, u["id"], today)
            goal = step_goal(conn, u["id"], today)
            prompt = steps_prompt(steps, goal)
            if prompt:
                if n == 0:
                    # Combine with meal reminder into one message
                    lines[-1] = lines[-1] + " " + prompt
                else:
                    # Meal is logged, send steps text alone
                    lines.append(prompt)
    conn.close()

    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
