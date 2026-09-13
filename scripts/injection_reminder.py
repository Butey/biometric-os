"""§11 21:00 вс — напоминание об инъекции, еженедельно, без условия, --no-agent.

    python scripts/injection_reminder.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    print("Инъекция сегодня.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
