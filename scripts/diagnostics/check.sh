#!/bin/bash
# Живая проверка Claude-провайдера через адаптер бота (с вызовом тула)
cd "$(dirname "$0")/../.." && set -a && . ./.env && set +a && HEALTH_DB=/tmp/check_claude.db PYTHONPATH=. .venv/bin/python - <<'PY'
import asyncio, aiohttp, yaml
from bot import llm
cfg = yaml.safe_load(open("config.yaml"))
prov = [p for p in cfg["bot"]["providers"] if p.get("api") == "anthropic"]
tools = [{"type": "function", "function": {"name": "log_water", "description": "записать воду", "parameters": {"type": "object", "properties": {"ml": {"type": "number"}}, "required": ["ml"]}}}]
async def main():
    async with aiohttp.ClientSession() as s:
        m = await llm.chat(s, [{"role": "system", "content": "Ты помощник."}, {"role": "user", "content": "я выпил 250 мл воды, запиши"}], tools, prov)
        print(m)
asyncio.run(main())
PY
