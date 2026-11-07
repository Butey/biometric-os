"""Улики подлинности моделей прокси TKBK. Запуск: python3 scripts/diagnostics/probe_proxy.py (ключ из .env)."""
import json, os, time, urllib.request, urllib.error
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from health_core.environment import load_env_file
load_env_file()

KEY = os.environ["TKBK_API_KEY"]
BASE = "https://api.tkbk.online"
Q = "Кто ты: какая модель и какая компания тебя создала? Ответь одной короткой фразой."


def call(url, payload, extra=None):
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}", "User-Agent": "curl/8.5.0", **(extra or {})}
    req = urllib.request.Request(url, json.dumps(payload).encode(), h)
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:200]}, time.time() - t
    except Exception as e:
        return 0, {"error": repr(e)}, time.time() - t


def openai(model):
    s, b, dt = call(f"{BASE}/openai/v1/chat/completions",
                    {"model": model, "max_tokens": 80, "messages": [{"role": "user", "content": Q}]})
    if s != 200:
        return f"{s} {b.get('error')}"
    u = b.get("usage") or {}
    return (f"{dt:.1f}s model_in_reply={b.get('model')} in={u.get('prompt_tokens')} out={u.get('completion_tokens')} "
            f"| {(b['choices'][0]['message'].get('content') or '')[:150]!r}")


def claude(model):
    s, b, dt = call(f"{BASE}/claude/v1/messages",
                    {"model": model, "max_tokens": 80, "messages": [{"role": "user", "content": Q}]},
                    {"anthropic-version": "2023-06-01"})
    if s != 200:
        return f"{s} {b.get('error')}"
    u = b.get("usage") or {}
    txt = "".join(x.get("text", "") for x in b.get("content", []))
    return (f"{dt:.1f}s model_in_reply={b.get('model')} id={str(b.get('id'))[:14]} in={u.get('input_tokens')} "
            f"out={u.get('output_tokens')} | {txt[:150]!r}")


import sys
ONLY = sys.argv[1] if len(sys.argv) > 1 else ""
for path in ("openai/v1/models", "claude/v1/models"):
    if ONLY and ONLY not in path:
        continue
    req = urllib.request.Request(f"{BASE}/{path}", headers={"Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
            print(f"== {path}:", [m.get("id") for m in d.get("data", [])] or str(d)[:200])
    except Exception as e:
        print(f"== {path}: {e!r}")

print("\n== OpenAI-эндпоинт")
for m in () if ONLY == "claude" else ("gpt-5.6-sol", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra", "gpt-6-luna", "gpt-99-fake"):
    print(f"{m:16}", openai(m))
print("\n== Claude-эндпоинт")
for m in () if ONLY == "openai" else ("claude-sonnet-5", "claude-opus-5-5", "claude-fable-5-1", "claude-opus-5", "claude-haiku-4-5-20251001", "claude-fake-9"):
    print(f"{m:27}", claude(m))
