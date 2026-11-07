# Repository Guidelines

## Project Structure & Module Organization

This Python health tracker uses SQLite as its source of truth. Keep health calculations deterministic in `health_core/`; the LLM handles conversation and tool selection.

- `health_core/`: domain logic, database, reports, and `ingest/` importers.
- `bot/`: aiogram transport, LLM adapters, history, and tool registry.
- `plugin/`: tool handlers and schemas; `register(ctx)` connects them to the bot.
- `admin/`: HTTP administration panel; `scripts/` and `systemd/`: scheduled jobs and deployment support.
- `Core/` and `Knowledge/`: runtime prompts and knowledge assets.
- `test_bot.py` and `test_e2e.py`: integration checks. `Metrics/` and `Nutrition/` contain ignored local data.
- `Docs/`, `SPEC.md`, and `DEPLOY.md`: architecture and operational documentation.

## Build, Test, and Development Commands

Run from the repository root with Python 3.10+:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install -e .
python test_bot.py
python test_e2e.py
```

The bot suite mocks provider responses and runs offline. The domain end-to-end suite requires local `Metrics/` fixtures. Both scripts select temporary databases.

With credentials configured, `python -m bot.main` starts Telegram polling; `python -m admin.server` starts the admin panel. Use `DEPLOY.md` for service setup.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions and modules, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Follow nearby typing and import conventions. Keep changes focused; avoid speculative abstractions and unrelated cleanup. No formatter or linter is configured in `pyproject.toml`.

## Testing Guidelines

Add `test_*` regression functions to `test_bot.py` for tool and conversation behavior. Module self-tests also use `python -m <module>`, such as `python -m bot.registry`. Pytest discovery is restricted to `test_bot.py`; no coverage threshold is configured. Set `HEALTH_DB` to a temporary path before importing database-dependent modules. Run relevant checks before committing.

## Commit & Pull Request Guidelines

Follow existing subjects: `fix(tools): ...`, `feat(bot): ...`, `refactor(guards): ...`, or `docs: ...`. Existing workflow uses `main`; the former `dev` branch is abandoned. For PRs, describe the behavior change, validation, and related issue when applicable; include screenshots for admin UI changes.

## Security & Operations

Keep credentials in `~/.hermes/.env`; never commit tokens or personal health records. Preserve Telegram allowlist checks. Restart running bot or admin services only on explicit user instruction, as restarts interrupt replies.
