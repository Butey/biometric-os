#!/bin/bash
set -euo pipefail

# Health Agent System automated install for Debian VPS
# Idempotent: safe to run multiple times
#
# Код, окружение, секреты и SQLite находятся в каталоге проекта.

PROJECT_DIR="${1:-.}"
PYTHON="${PYTHON:-python3}"

# Color codes for output (portable)
RED=''
GREEN=''
YELLOW=''
NC=''
if [ -t 1 ]; then
    RED=$(printf '\033[0;31m')
    GREEN=$(printf '\033[0;32m')
    YELLOW=$(printf '\033[1;33m')
    NC=$(printf '\033[0m')
fi

printf "${GREEN}=== Health Agent System Install ===${NC}\n\n"

# ================================================================
# Preflight: Check Python version
# ================================================================
printf "Checking Python version...\n"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    printf "${RED}ERROR: Python not found at '%s'${NC}\n" "$PYTHON"
    printf "Use: install.sh --python /path/to/python3\n"
    exit 1
fi

PYTHON_VERSION=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 10 ]); then
    printf "${RED}ERROR: Python %s found, but 3.10+ required${NC}\n" "$PYTHON_VERSION"
    exit 1
fi
printf "${GREEN}OK${NC}: Python $PYTHON_VERSION\n\n"

# ================================================================
# Resolve absolute paths
# ================================================================
PROJECT_DIR=$(cd "$PROJECT_DIR" && pwd)
if [ ! -f "$PROJECT_DIR/pyproject.toml" ]; then
    printf "${RED}ERROR: pyproject.toml not found in %s${NC}\n" "$PROJECT_DIR"
    exit 1
fi
printf "Project directory: $PROJECT_DIR\n\n"

# ================================================================
# Create venv in the project directory (idempotent: skip if present)
# ================================================================
VENV_DIR="$PROJECT_DIR/.venv"
TARGET_PY="$VENV_DIR/bin/python"

if [ -x "$TARGET_PY" ]; then
    printf "${YELLOW}SKIPPED${NC}: .venv already exists at %s\n\n" "$VENV_DIR"
else
    printf "Creating venv at %s...\n" "$VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR" || {
        printf "${RED}ERROR: python -m venv failed${NC}\n"
        exit 1
    }
    printf "${GREEN}OK${NC}: venv created (%s)\n\n" "$("$TARGET_PY" --version 2>&1)"
fi

# ================================================================
# Install editable package (brings in aiogram, aiohttp — see pyproject.toml)
# ================================================================
printf "Installing health-agent package (editable)...\n"
"$TARGET_PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
"$TARGET_PY" -m pip install -e "$PROJECT_DIR" >/dev/null 2>&1 || {
    printf "${RED}ERROR: pip install failed${NC}\n"
    exit 1
}
printf "${GREEN}OK${NC}: Installed\n\n"

# ================================================================
# Database & Secrets Setup (Data First!)
# ================================================================

# Старое стандартное переопределение больше не должно направлять дочерние процессы домой.
if [ "${HEALTH_DB:-}" = "$HOME/.hermes/health.db" ]; then
    export HEALTH_DB="$PROJECT_DIR/health.db"
fi

# 2. Секреты остаются в проекте; существующий .env никогда не перезаписываем.
"$TARGET_PY" - "$PROJECT_DIR" <<'PYENV'
import os
import sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
target = root / ".env"
if not target.exists():
    sources = [Path.home() / ".hermes" / ".env", root / ".env.example", root / "data" / "env.template", root.parent / "data" / "env.template"]
    source = next((p for p in sources if p.is_file()), None)
    data = source.read_bytes() if source else b"TELEGRAM_BOT_TOKEN=\n"
    if source and source.parent == Path.home() / ".hermes":
        lines = data.decode("utf-8").splitlines()
        for i, line in enumerate(lines):
            key, sep, value = line.partition("=")
            value = value.split(" #", 1)[0].strip().strip("\"'")
            if sep and key.strip() == "HEALTH_DB" and Path(value).expanduser() == Path.home() / ".hermes" / "health.db":
                lines[i] = "HEALTH_DB=health.db"
        data = ("\n".join(lines) + "\n").encode("utf-8")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    print("Project .env created; legacy source retained")
os.chmod(target, 0o600)
PYENV
printf "Secrets location: %s/.env\n" "$PROJECT_DIR"

# ================================================================

# 1. Существующая база проекта приоритетнее восстановления из старого пути/пакета.
# На обновлении сервисы должны быть остановлены до переноса (см. DEPLOY.md).
"$TARGET_PY" - "$PROJECT_DIR" <<'PYDB'
import os
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
from health_core.environment import load_env_file
load_env_file(root / ".env")
target = Path(os.environ.get("HEALTH_DB") or root / "health.db").expanduser()
if not target.is_absolute():
    target = root / target
if target == Path.home() / ".hermes" / "health.db":
    target = root / "health.db"
if target.exists():
    print(f"Existing database retained: {target}")
else:
    sources = [Path.home() / ".hermes" / "health.db", root / "data" / "health.db", root.parent / "data" / "health.db"]
    source = next((p for p in sources if p.is_file()), None)
    if source:
        target.parent.mkdir(parents=True, exist_ok=True)
        # SQLite backup включает WAL; исходная база сохраняется для отката.
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            src.close()
        print(f"Database copied: {source} -> {target}; original retained")
    else:
        print(f"No existing database; schema will be created at {target}")
PYDB

# Initialize / Migrate database
# ================================================================
printf "Applying database migrations...\n"
"$TARGET_PY" -c "from health_core.db import connect, migrate; c=connect(); migrate(c); c.close()" 2>/dev/null || {
    printf "${RED}ERROR: Database migration failed${NC}\n"
    exit 1
}
printf "${GREEN}OK${NC}: Database migrated and ready\n\n"

# ================================================================
# Verify all self-checks
# ================================================================
printf "Verifying installation...\n"
# health_core.* проверяются из /tmp — доказывает, что пакет реально
# импортируется из чужой директории, а не только рядом с исходниками.
# plugin.tools и bot.* запускаются из каталога проекта: bot/ читает
# Core/system_promt.md и Knowledge/ относительными путями от ROOT.
CHECKS_FROM_TMP=(
    health_core.db
    health_core.energy
    health_core.guards
    health_core.ingest.scale
    health_core.export
)
CHECKS_FROM_PROJECT=(
    plugin.tools
    bot.registry
    bot.llm
    bot.history
    bot.knowledge
)

cd /tmp || exit 1
for mod in "${CHECKS_FROM_TMP[@]}"; do
    if ! "$TARGET_PY" -m "$mod" >/dev/null 2>&1; then
        printf "${RED}FAILED${NC}: %s -m %s\n" "$TARGET_PY" "$mod"
        exit 1
    fi
    printf "${GREEN}OK${NC}: %s\n" "$mod"
done

cd "$PROJECT_DIR" || exit 1
for mod in "${CHECKS_FROM_PROJECT[@]}"; do
    if ! "$TARGET_PY" -m "$mod" >/dev/null 2>&1; then
        printf "${RED}FAILED${NC}: %s -m %s\n" "$TARGET_PY" "$mod"
        exit 1
    fi
    printf "${GREEN}OK${NC}: %s\n" "$mod"
done

if ! "$TARGET_PY" test_bot.py >/dev/null 2>&1; then
    printf "${RED}FAILED${NC}: test_bot.py\n"
    exit 1
fi
printf "${GREEN}OK${NC}: test_bot.py\n\n"

# ================================================================
# Print next steps
# ================================================================
printf "\n${GREEN}=== Installation Complete ===${NC}\n\n"
printf "Manual steps remaining:\n\n"
printf "1. Edit $PROJECT_DIR/.env and fill in:\n"
printf "   - TELEGRAM_BOT_TOKEN (from BotFather)\n"
printf "   - TELEGRAM_ALLOWED_USERS (comma-separated Telegram IDs)\n"
printf "   - GOOGLE_API_KEY (or whichever api_key_env is named in config.yaml, bot.providers)\n\n"
printf "2. Enable autostart (systemd, system units):\n"
printf "   sudo mkdir -p /etc/systemd/system\n"
printf "   sudo cp %s/systemd/*.service %s/systemd/*.timer /etc/systemd/system/\n" "$PROJECT_DIR" "$PROJECT_DIR"
printf "   sudo systemctl daemon-reload\n"
printf "   sudo systemctl enable --now health-agent.service health-admin.service\n\n"
printf "3. Register via Telegram: send /start to your bot\n\n"
printf "4. After user registration, enable the schedule (systemd, as root; jobs run on each user's local time):\n"
printf "   sudo systemctl enable --now health-dispatch.timer health-backup.timer\n"
printf "   Job times: config.yaml -> schedule.jobs; preview: .venv/bin/python scripts/dispatch.py --dry-run\n\n"
printf "Note: most of these fail silently (non-zero exit -> error sent to admins, not users)\n"
printf "until a user is registered in the database. This is expected before step 3.\n\n"
printf "Details and rationale: DEPLOY.md, Docs/bot_design.md, Docs/llm_config.md\n\n"
printf "Database location: %s/health.db (or HEALTH_DB override)\n" "$PROJECT_DIR"
printf "Secrets location:  $PROJECT_DIR/.env\n"
printf "Venv location:     %s\n\n" "$VENV_DIR"
