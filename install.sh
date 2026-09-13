#!/bin/bash
set -euo pipefail

# Health Agent System automated install for Debian VPS
# Idempotent: safe to run multiple times
#
# Раскладка после перехода с Hermes на собственный цикл агента (bot/,
# Docs/bot_design.md): venv живёт В КАТАЛОГЕ ПРОЕКТА, а не в ~/.hermes/.
# От ~/.hermes/ остаются только .env (секреты, общий канал с admin/) и
# health.db. Копий кода, персоны и config.yaml туда больше не кладём.

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
mkdir -p "$HOME/.hermes"

# 1. Database: restore if present in package, or initialize empty
if [ ! -f "$HOME/.hermes/health.db" ]; then
    if [ -f "$PROJECT_DIR/data/health.db" ]; then
        printf "Found database in %s/data/health.db -> copying to ~/.hermes/health.db...\n" "$PROJECT_DIR"
        cp "$PROJECT_DIR/data/health.db" "$HOME/.hermes/health.db"
        printf "${GREEN}OK${NC}: Restored database from package\n"
    elif [ -f "$PROJECT_DIR/../data/health.db" ]; then
        printf "Found database in data/health.db -> copying to ~/.hermes/health.db...\n"
        cp "$PROJECT_DIR/../data/health.db" "$HOME/.hermes/health.db"
        printf "${GREEN}OK${NC}: Restored database from package\n"
    else
        printf "${YELLOW}NOTE${NC}: No existing health.db found, will create empty schema\n"
    fi
else
    printf "${GREEN}OK${NC}: Existing ~/.hermes/health.db found\n"
fi

# 2. Secrets (.env): restore template if needed
if [ ! -f "$HOME/.hermes/.env" ]; then
    if [ -f "$PROJECT_DIR/data/env.template" ]; then
        cp "$PROJECT_DIR/data/env.template" "$HOME/.hermes/.env"
        chmod 600 "$HOME/.hermes/.env"
        printf "${GREEN}OK${NC}: ~/.hermes/.env created from env.template\n"
    elif [ -f "$PROJECT_DIR/../data/env.template" ]; then
        cp "$PROJECT_DIR/../data/env.template" "$HOME/.hermes/.env"
        chmod 600 "$HOME/.hermes/.env"
        printf "${GREEN}OK${NC}: ~/.hermes/.env created from env.template\n"
    else
        cat > "$HOME/.hermes/.env" << 'EOF'
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=
GOOGLE_API_KEY=
EOF
        chmod 600 "$HOME/.hermes/.env"
        printf "${GREEN}OK${NC}: ~/.hermes/.env created with empty placeholders\n"
    fi
    printf "${YELLOW}        Edit ~/.hermes/.env and fill in real values${NC}\n"
else
    printf "${YELLOW}SKIPPED${NC}: ~/.hermes/.env already exists\n"
fi
printf "\n"

# ================================================================
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
printf "1. Edit ~/.hermes/.env and fill in:\n"
printf "   - TELEGRAM_BOT_TOKEN (from BotFather)\n"
printf "   - TELEGRAM_ALLOWED_USERS (comma-separated Telegram IDs)\n"
printf "   - GOOGLE_API_KEY (or whichever api_key_env is named in config.yaml, bot.providers)\n\n"
printf "2. Enable autostart (systemd, user unit):\n"
printf "   mkdir -p ~/.config/systemd/user\n"
printf "   cp %s/health-agent.service ~/.config/systemd/user/\n" "$PROJECT_DIR"
printf "   systemctl --user daemon-reload\n"
printf "   loginctl enable-linger \$(whoami)   # once, as root — otherwise systemd kills the bot on ssh logout\n"
printf "   systemctl --user enable --now health-agent.service\n\n"
printf "3. Register via Telegram: send /start to your bot\n\n"
printf "4. After user registration, enable the schedule (systemd, as root; jobs run on each user's local time):\n"
printf "   systemctl link %s/systemd/health-dispatch.service %s/systemd/health-backup.service\n" "$PROJECT_DIR" "$PROJECT_DIR"
printf "   systemctl enable --now %s/systemd/health-dispatch.timer %s/systemd/health-backup.timer\n" "$PROJECT_DIR" "$PROJECT_DIR"
printf "   Job times: config.yaml -> schedule.jobs; preview: .venv/bin/python scripts/dispatch.py --dry-run\n\n"
printf "Note: most of these fail silently (non-zero exit -> error sent to admins, not users)\n"
printf "until a user is registered in the database. This is expected before step 3.\n\n"
printf "Details and rationale: DEPLOY.md, Docs/bot_design.md, Docs/llm_config.md\n\n"
printf "Database location: ~/.hermes/health.db\n"
printf "Secrets location:  ~/.hermes/.env\n"
printf "Venv location:     %s\n\n" "$VENV_DIR"
