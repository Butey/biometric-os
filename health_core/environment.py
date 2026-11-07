"""Настройки окружения проекта; процессы и панель используют один .env."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

def load_env_file(path: Path = ENV_PATH, force: bool = False) -> None:
    """Populate os.environ from a KEY=VALUE file. If force=True, updates os.environ
    even if the variable was already present (used after saving keys in admin panel)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Пустую переменную тоже заполняем: ключ, добавленный в админке, иначе
        # не дойдёт до уже запущенного бота без перезапуска.
        if key and (force or not os.environ.get(key)):
            os.environ[key] = _strip_inline_comment(value)


def read_env_vars(path: Path = ENV_PATH) -> dict[str, str]:
    """Read all key-value pairs from .env without mutating os.environ."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            out[key] = _strip_inline_comment(value)
    return out


def update_env_vars(updates: dict[str, str | None], path: Path = ENV_PATH) -> None:
    """Update or append variables in .env atomically with 0600 permissions,
    preserving comments and existing variables. Also updates os.environ."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()

    remaining_updates = dict(updates)
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key, _, old_val = stripped.partition("=")
        key = key.strip()
        if key in remaining_updates:
            new_val = remaining_updates.pop(key)
            if new_val is not None:
                new_lines.append(f"{key}={new_val}")
                os.environ[key] = new_val
            else:
                os.environ.pop(key, None)
        else:
            new_lines.append(line)

    for key, new_val in remaining_updates.items():
        if new_val is not None:
            new_lines.append(f"{key}={new_val}")
            os.environ[key] = new_val
        else:
            os.environ.pop(key, None)

    # Write atomically via temp file with 0600 permissions
    import tempfile
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tf:
        tf.write("\n".join(new_lines) + "\n")
        tmp_name = tf.name
    os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, path)


def _strip_inline_comment(value: str) -> str:
    """Отрезает хвостовой комментарий: `374939064   # Comma-separated IDs`.

    Без этого весь хвост уезжает в значение. Живой случай: TELEGRAM_ALLOWED_USERS
    превратился в строку с комментарием, ни один telegram id с ней не совпал, и
    бот молча игнорировал единственного пользователя — снаружи это выглядит как
    «бот сломался», а в логе ровно ничего.

    Режем только `#` после пробела — таково правило .env-файлов и единственное
    безопасное: `#` внутри значения (пароль, токен) остаётся на месте."""
    for i, ch in enumerate(value):
        if ch == "#" and i > 0 and value[i - 1] in " \t":
            return value[:i].strip()
    return value.strip()



load_env_file()
