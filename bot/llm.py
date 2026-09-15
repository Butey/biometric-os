"""OpenAI-совместимый клиент: цепочка фолбэка провайдеров + ротация API-ключей + цикл вызова инструментов.

На aiohttp — его и так тянет aiogram, второй http-клиент в проект не заводим.
Поддерживает:
- Пул и ротацию API-ключей для каждого провайдера (через GOOGLE_API_KEYS, GOOGLE_API_KEY_1..N,
  comma-separated значения в .env или динамический список).
- Round-robin распределение нагрузки между активными ключами.
- Автоматический кулдаун на 429 (Resource Exhausted / Rate Limit) с мгновенным переключением
  на следующий доступный ключ.
- Фолбэк на следующую модель/провайдера, если исчерпаны все ключи текущей.
- Стриминга нет: телеграм всё равно отдаёт сообщение целиком.
"""
import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Callable, TypedDict

import aiohttp

log = logging.getLogger(__name__)


class Provider(TypedDict, total=False):
    base_url: str
    api_key_env: str
    model: str
    api_keys: list[str]


class KeyState:
    def __init__(self) -> None:
        self.cooldown_until: float = 0.0
        self.failure_count: int = 0
        self.last_status: int | None = None
        self.last_error: str | None = None


# Состояние ключей: key_hash/key -> KeyState
_KEY_STATES: dict[str, KeyState] = {}
# Индекс round-robin ротации для каждого env_var
_ROTATION_INDEX: dict[str, int] = {}


def _mask_key(key: str) -> str:
    """Маскирует ключ для безопасного вывода в лог (только последние 4 символа)."""
    if not key:
        return "none"
    if len(key) <= 6:
        return "***"
    return f"...{key[-4:]}"


def get_provider_keys(provider: Provider) -> list[str]:
    """Извлекает все доступные API-ключи для провайдера из прямого списка или переменных окружения.
    
    Поддерживает:
    1. Прямой список provider['api_keys']
    2. Имя переменной provider['api_key_env'] (например, GOOGLE_API_KEY)
    3. Множественные переменные (GOOGLE_API_KEYS, GOOGLE_API_KEY_LIST)
    4. Нумерованные переменные (GOOGLE_API_KEY_1, GOOGLE_API_KEY_2, ...)
    5. Разделители: запятая, точка с запятой, перенос строки.
    """
    if provider.get("api_keys"):
        return [k.strip() for k in provider["api_keys"] if k and k.strip()]

    env_var = provider.get("api_key_env", "").strip()
    if not env_var:
        return []

    candidates = [env_var]
    if not env_var.endswith("S"):
        candidates.append(env_var + "S")
        candidates.append(env_var + "_LIST")

    for i in range(1, 20):
        candidates.append(f"{env_var}_{i}")

    keys: list[str] = []
    for var_name in candidates:
        val = os.environ.get(var_name, "")
        if val:
            for part in re.split(r"[\n,;]+", val):
                k = part.strip()
                # Игнорируем комментарии #...
                if k and not k.startswith("#") and k not in keys:
                    keys.append(k)

    return keys


def _key_id(key: str, model: str = "") -> str:
    return f"{model}:{key}" if model else key


def _get_key_state(key: str, model: str = "") -> KeyState:
    k = _key_id(key, model)
    state = _KEY_STATES.get(k)
    if state is None:
        state = _KEY_STATES[k] = KeyState()
    return state


def _ordered_keys(env_name: str, keys: list[str], model: str = "") -> list[str]:
    """Сортирует ключи для запроса: сначала здоровые (не на кулдауне) с round-robin сдвигом,
    затем те, у кого кулдаун истекает раньше."""
    if not keys:
        return []
    now = time.time()

    idx = _ROTATION_INDEX.get(env_name, 0) % len(keys)
    _ROTATION_INDEX[env_name] = (idx + 1) % len(keys)
    rotated = keys[idx:] + keys[:idx]

    healthy = [k for k in rotated if _get_key_state(k, model).cooldown_until <= now]
    cooling = [k for k in rotated if _get_key_state(k, model).cooldown_until > now]
    cooling.sort(key=lambda k: _get_key_state(k, model).cooldown_until)

    return healthy + cooling


DEFAULT_TIMEOUT_S = 25.0  # чат: 25с хватает с запасом. Не меняется, если вызывающий
# не передал timeout_s — так поведение обычного чата не трогается.


async def _post(session: aiohttp.ClientSession, url: str, headers: dict, payload: dict,
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> tuple[int, dict]:
    # Вынесено отдельной функцией специально ради самотеста: подменяем её,
    # чтобы проверить фолбэк и цикл инструментов без похода в сеть.
    # timeout_s параметром: обычный чат передаёт 25с (или bot.request_timeout_s
    # из config.yaml), консилиум (bot/council.py) — свои council.timeout_s
    # (минуты): Kimi с глубоким рассуждением часто не укладывается в 25с.
    # sock_connect=5 всегда короткий — быстро отсекает мёртвый маршрут.
    timeout = aiohttp.ClientTimeout(total=timeout_s, sock_connect=5)
    async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
        try:
            body = await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
            body = {"raw": await resp.text()}
        return resp.status, body


def _parse_retry_delay(body: Any, default: float = 60.0) -> float:
    """Пытается извлечь время ожидания из ответа об ошибке 429."""
    if isinstance(body, dict):
        # 1. Google Gemini error structure: details -> retryDelay
        error_info = body.get("error", {})
        if isinstance(error_info, dict):
            details = error_info.get("details", [])
            if isinstance(details, list):
                for item in details:
                    if isinstance(item, dict) and "retryDelay" in item:
                        raw = str(item["retryDelay"]).rstrip("s")
                        try:
                            return max(5.0, float(raw))
                        except ValueError:
                            pass
            msg = str(error_info.get("message", ""))
            m = re.search(r"retry in ([\d\.]+)s", msg)
            if m:
                try:
                    return max(5.0, float(m.group(1)))
                except ValueError:
                    pass
    return default


async def chat(session: aiohttp.ClientSession, messages: list[dict], tools: list[dict],
                providers: list[Provider], timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Идёт по providers по порядку, ротирует API-ключи для каждого провайдера,
    возвращает choices[0].message первого, кто ответил 200.

    Фолбэк на следующий ключ: 429 (Resource Exhausted / Rate Limit), 401/403 (Invalid Key).
    Фолбэк на следующего провайдера: когда все ключи текущего провайдера исчерпаны,
    сетевая ошибка/таймаут, 5xx или битый ответ.

    timeout_s — таймаут одного HTTP-запроса (см. _post); по умолчанию как в
    обычном чате, консилиум (bot/council.py) передаёт свой для медленных моделей анализа.
    """
    failures: list[str] = []

    for provider in providers:
        env_name = provider.get("api_key_env", "DEFAULT_KEY")
        all_keys = get_provider_keys(provider)

        if not all_keys:
            msg = f"{provider.get('model')}: нет ключей в переменной {env_name}"
            log.warning(msg)
            failures.append(msg)
            continue

        # Кулдаун ключа привязан к модели: у каждой модели своя квота.
        scope = provider.get("model", "")
        ordered_keys = _ordered_keys(env_name, all_keys, model=scope)
        prov_timeout = float(provider.get("timeout_s", timeout_s))

        url = f"{provider['base_url'].rstrip('/')}/chat/completions"
        clean_messages = []
        for m in messages:
            if m.get("role") == "assistant":
                c = {"role": "assistant"}
                if m.get("content") is not None:
                    c["content"] = m["content"]
                elif not m.get("tool_calls"):
                    c["content"] = ""
                else:
                    c["content"] = None
                if m.get("tool_calls"):
                    c["tool_calls"] = m["tool_calls"]
                clean_messages.append(c)
            else:
                clean_messages.append(m)

        payload: dict[str, Any] = {
            "model": provider["model"],
            "messages": clean_messages,
            "tool_choice": "auto",
        }
        if "max_tokens" in provider:
            payload["max_tokens"] = provider["max_tokens"]
        if "temperature" in provider:
            payload["temperature"] = provider["temperature"]
        if "top_p" in provider:
            payload["top_p"] = provider["top_p"]
        if "reasoning_effort" in provider:
            payload["reasoning_effort"] = provider["reasoning_effort"]
        if "seed" in provider:
            payload["seed"] = provider["seed"]
        if "chat_template_kwargs" in provider and isinstance(provider["chat_template_kwargs"], dict):
            payload["chat_template_kwargs"] = provider["chat_template_kwargs"]
        if "extra_body" in provider and isinstance(provider["extra_body"], dict):
            payload.update(provider["extra_body"])
        if "extra_payload" in provider and isinstance(provider["extra_payload"], dict):
            payload.update(provider["extra_payload"])

        disable_tools = provider.get("disable_tools", False) or provider["model"].lower().startswith("gemma")
        if tools and not disable_tools:
            payload["tools"] = tools
        elif not tools or disable_tools:
            payload.pop("tool_choice", None)

        for key in ordered_keys:
            state = _get_key_state(key, model=scope)
            now = time.time()
            if state.cooldown_until > now and any(
                _get_key_state(k, model=scope).cooldown_until <= now for k in ordered_keys
            ):
                # Пропускаем ключ на кулдауне, только если СЕЙЧАС есть другой
                # реально здоровый — статичный снимок healthy_keys до цикла не
                # видел, что здоровые ключи сами ушли в кулдаун по ходу перебора.
                continue

            headers = {
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            }

            try:
                status, body = await _post(session, url, headers, payload, timeout_s=prov_timeout)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                msg = f"{provider['model']} (key {_mask_key(key)}): сетевая ошибка/таймаут — {exc!r}"
                log.warning(msg)
                failures.append(msg)
                # Сетевая ошибка часто на уровне хоста/провайдера — пробуем следующий ключ или провайдера
                continue

            if status == 200:
                # Успешный ответ: сбрасываем кулдаун ключа
                state.cooldown_until = 0.0
                state.failure_count = 0
                state.last_status = 200
                try:
                    return dict(body["choices"][0]["message"])
                except (KeyError, IndexError, TypeError) as exc:
                    msg = f"{provider['model']} (key {_mask_key(key)}): 200, но битый ответ — {exc!r}, тело={body!r}"
                    log.warning(msg)
                    failures.append(msg)
                    continue

            # Обработка ошибок по ключу
            state.last_status = status
            state.failure_count += 1

            if status == 404:
                # Модель не найдена / закрыта — кулдаун на 24 часа и сразу переход к следующему провайдеру
                state.cooldown_until = time.time() + 86400.0
                log.warning("%s (key %s): HTTP 404 модель не найдена, переход к следующему провайдеру — %s",
                            provider["model"], _mask_key(key), _short(body, 180))
                failures.append(f"{provider['model']}[{_mask_key(key)}]: HTTP 404 NotFound")
                break

            if status == 413:
                # Превышен лимит размера запроса/токенов провайдера (например, Groq TPM/ITPM)
                log.warning("%s (key %s): HTTP 413 превышен лимит размера/токенов, переход к следующему провайдеру — %s",
                            provider["model"], _mask_key(key), _short(body, 180))
                failures.append(f"{provider['model']}[{_mask_key(key)}]: HTTP 413 PayloadTooLarge")
                break

            if status == 429:
                delay = _parse_retry_delay(body, default=60.0)
                state.cooldown_until = time.time() + delay
                log.warning("%s (key %s): HTTP 429 лимит/квота (кулдаун %.0fс), ротация на следующий ключ — %s",
                            provider["model"], _mask_key(key), delay, _short(body, 180))
                failures.append(f"{provider['model']}[{_mask_key(key)}]: 429 RateLimit/Quota — {_short(body, 120)}")
                # Мгновенно пробуем следующий ключ для этой же модели!
                continue

            if status in (401, 403):
                # Неверный ключ или запрет доступа: ставим кулдаун на 1 час
                state.cooldown_until = time.time() + 3600.0
                log.warning("%s (key %s): HTTP %s ошибка авторизации, ротация на следующий ключ",
                            provider["model"], _mask_key(key), status)
                failures.append(f"{provider['model']}[{_mask_key(key)}]: HTTP {status} AuthError")
                continue

            if status in (400, 422):
                # Ошибка схемы/параметров (не поддерживаются tools или thinking) — переход к след. провайдеру
                log.warning("%s (key %s): HTTP %s ошибка запроса, переход к следующему провайдеру — %s",
                            provider["model"], _mask_key(key), status, _short(body, 200))
                failures.append(f"{provider['model']}[{_mask_key(key)}]: HTTP {status} — {_short(body, 150)}")
                break

            # Прочие 4xx / 5xx
            log.warning("%s (key %s): HTTP %s — %s", provider["model"], _mask_key(key), status, _short(body, 200))
            failures.append(f"{provider['model']}[{_mask_key(key)}]: HTTP {status} — {_short(body, 150)}")

    raise RuntimeError("Все провайдеры упали: " + "; ".join(failures))


def _short(value: Any, limit: int = 300) -> str:
    """Обрезка для лога: аргументы и результаты бывают на килобайты, а лог
    должен оставаться читаемым."""
    value_str = " ".join(str(value).split())
    return value_str if len(value_str) <= limit else value_str[:limit] + f"…(+{len(value_str) - limit})"


async def _run_one(call: dict, dispatch: Callable[[str, dict], str]) -> str:
    """Один вызов инструмента: разбор аргументов + исполнение в потоке."""
    name = call["function"]["name"]
    raw_args = call["function"].get("arguments") or "{}"
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        # Модель сама прислала битый JSON в аргументах — отдаём ей ошибку как результат инструмента
        log.warning("битые arguments у tool_call %s: %r (%s)", name, raw_args, exc)
        return json.dumps({"error": f"не удалось разобрать arguments: {exc}"}, ensure_ascii=False)
    result = await asyncio.to_thread(dispatch, name, args)
    log.info("tool %s(%s) -> %s", name, _short(raw_args), _short(result))
    return result


async def run_loop(session: aiohttp.ClientSession, messages: list[dict], tools: list[dict],
                    providers: list[Provider], dispatch: Callable[[str, dict], str],
                    max_iters: int = 6, timeout_s: float = DEFAULT_TIMEOUT_S) -> tuple[str, list[dict]]:
    """Цикл: chat → если есть tool_calls, исполнить все и повторить, иначе вернуть текст.

    dispatch синхронный и блокирующий (хендлеры дёргают sqlite и т.п.), поэтому вызываем
    его через asyncio.to_thread — это не только уводит блокировку с event loop, но и
    копирует contextvars (ContextVar с личностью звонящего registry.set_caller доезжает
    до хендлера в отдельном потоке).
    """
    messages = list(messages)
    last_text = ""

    for _ in range(max_iters):
        message = await chat(session, messages, tools, providers, timeout_s=timeout_s)
        messages.append(message)

        tool_calls = message.get("tool_calls") or []
        if message.get("content"):
            last_text = message["content"]
        elif not last_text:
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            if reasoning:
                last_text = reasoning

        if message.get("reasoning_content") or message.get("reasoning"):
            r = message.get("reasoning_content") or message.get("reasoning")
            log.info("модель вернула reasoning (%d симв): %s", len(r), _short(r, 150))

        if not tool_calls:
            # Текст/reasoning ЭТОГО хода, не last_text — иначе легитимно пустой
            # финальный ответ (content="", без reasoning) отдавал бы наружу
            # обрывок reasoning из более раннего хода tool_calls. last_text
            # остаётся только накопителем для аварийного возврата при max_iters.
            own_reasoning = message.get("reasoning_content") or message.get("reasoning")
            return message.get("content") or own_reasoning or "", messages

        results = []
        for call in tool_calls:
            results.append(await _run_one(call, dispatch))
        for call, result in zip(tool_calls, results):
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result,
            })

    answer = last_text or ("Не удалось получить ответ: цикл вызова инструментов "
                           "не сошёлся за отведённое число попыток.")
    messages.append({"role": "assistant", "content": answer})
    return answer, messages


if __name__ == "__main__":
    from unittest.mock import patch

    PROVIDERS: list[Provider] = [
        {"base_url": "https://p1.example/v1", "api_key_env": "TEST_KEY_1", "model": "model-1"},
        {"base_url": "https://p2.example/v1", "api_key_env": "TEST_KEY_2", "model": "model-2"},
    ]

    def _msg(**kw) -> dict:
        base = {"role": "assistant", "content": None, "tool_calls": None}
        base.update(kw)
        return base

    def _resp(status: int, message: dict | None = None, error: dict | None = None) -> dict:
        if message is not None:
            return {"status": status, "body": {"choices": [{"message": message}]}}
        return {"status": status, "body": error or {"error": "boom"}}

    async def main() -> None:
        _KEY_STATES.clear()
        _ROTATION_INDEX.clear()
        os.environ["TEST_KEY_1"] = "k1"
        os.environ["TEST_KEY_2"] = "k2"

        async with aiohttp.ClientSession() as session:

            # --- 1. успешный ответ без tool_calls с первого провайдера ---
            queue = [_resp(200, _msg(content="привет"))]

            async def _post_ok(session, url, headers, payload, timeout_s=None):
                r = queue.pop(0)
                return r["status"], r["body"]

            with patch(__name__ + "._post", _post_ok):
                text, msgs = await run_loop(session, [{"role": "user", "content": "hi"}], [], PROVIDERS, lambda n, a: "{}")
                assert text == "привет", text
                assert msgs[-1]["content"] == "привет"
            print("OK: 1) успешный ответ без tool_calls")

            # --- 2. первый провайдер 429 -> фолбэк на второй ---
            queue = [_resp(429, error={"error": "rate limited"}), _resp(200, _msg(content="ответ от второго"))]
            calls_seen = []

            async def _post_fallback(session, url, headers, payload, timeout_s=None):
                calls_seen.append(url)
                r = queue.pop(0)
                return r["status"], r["body"]

            with patch(__name__ + "._post", _post_fallback):
                message = await chat(session, [{"role": "user", "content": "hi"}], [], PROVIDERS)
                assert message["content"] == "ответ от второго", message
                assert len(calls_seen) == 2, calls_seen
            print("OK: 2) 429 у первого -> фолбэк на второго")

            # --- 2b. ротация ключей внутри одного провайдера при 429 ---
            _KEY_STATES.clear()
            os.environ["MULTI_KEYS"] = "key_alpha,key_beta"
            multi_provider: list[Provider] = [
                {"base_url": "https://multi.example/v1", "api_key_env": "MULTI_KEYS", "model": "gemini-multi"}
            ]
            auth_seen = []

            async def _post_multi_keys(session, url, headers, payload, timeout_s=None):
                auth_seen.append(headers.get("Authorization"))
                if len(auth_seen) == 1:
                    return 429, {"error": {"message": "Resource exhausted", "details": [{"retryDelay": "10s"}]}}
                return 200, {"choices": [{"message": _msg(content="ответ со второго ключа")}]}

            with patch(__name__ + "._post", _post_multi_keys):
                msg_rot = await chat(session, [{"role": "user", "content": "hi"}], [], multi_provider)
                assert msg_rot["content"] == "ответ со второго ключа", msg_rot
                assert len(auth_seen) == 2, auth_seen
                assert "key_alpha" in auth_seen[0] and "key_beta" in auth_seen[1], auth_seen
            print("OK: 2b) ротация ключей: первый ключ 429 -> второй ключ той же модели отработал успешно")

            # --- 3. все провайдеры упали -> RuntimeError с перечислением ---
            queue = [_resp(500, error={"error": "server1 down"}), _resp(503, error={"error": "server2 down"})]

            async def _post_all_fail(session, url, headers, payload, timeout_s=None):
                r = queue.pop(0)
                return r["status"], r["body"]

            with patch(__name__ + "._post", _post_all_fail):
                try:
                    await chat(session, [{"role": "user", "content": "hi"}], [], PROVIDERS)
                    assert False, "должен был бросить RuntimeError"
                except RuntimeError as exc:
                    assert "model-1" in str(exc) and "model-2" in str(exc), str(exc)
                    assert "500" in str(exc) and "503" in str(exc), str(exc)
            print("OK: 3) все провайдеры упали -> RuntimeError с перечислением")

            # --- 4. один tool_call исполняется, результат уходит role=tool, второй запрос даёт финальный текст ---
            tc = {"id": "call_1", "type": "function", "function": {"name": "log_food", "arguments": '{"kcal": 100}'}}
            queue = [
                _resp(200, _msg(content=None, tool_calls=[tc])),
                _resp(200, _msg(content="готово, записал")),
            ]

            def _dispatch_ok(name, args):
                assert name == "log_food" and args == {"kcal": 100}, (name, args)
                return json.dumps({"ok": True})

            with patch(__name__ + "._post", _post_ok):
                text, msgs = await run_loop(session, [{"role": "user", "content": "запиши"}], [{"type": "function"}],
                                             PROVIDERS, _dispatch_ok)
                assert text == "готово, записал", text
                tool_msgs = [m for m in msgs if m.get("role") == "tool"]
                assert len(tool_msgs) == 1, tool_msgs
                assert tool_msgs[0]["tool_call_id"] == "call_1", tool_msgs[0]
                assert tool_msgs[0]["content"] == '{"ok": true}', tool_msgs[0]
            print("OK: 4) tool_call исполняется, результат в messages ролью tool, второй запрос даёт финал")

            # --- 5. битый JSON в arguments не роняет цикл ---
            tc_bad = {"id": "call_2", "type": "function", "function": {"name": "log_food", "arguments": "{not json"}}
            queue = [
                _resp(200, _msg(content=None, tool_calls=[tc_bad])),
                _resp(200, _msg(content="разобрался")),
            ]

            def _dispatch_should_not_be_called(name, args):
                raise AssertionError("dispatch не должен вызываться на битом JSON")

            with patch(__name__ + "._post", _post_ok):
                text, msgs = await run_loop(session, [{"role": "user", "content": "запиши"}], [{"type": "function"}],
                                             PROVIDERS, _dispatch_should_not_be_called)
                assert text == "разобрался", text
                tool_msgs = [m for m in msgs if m.get("role") == "tool"]
                assert len(tool_msgs) == 1, tool_msgs
                error_payload = json.loads(tool_msgs[0]["content"])
                assert "error" in error_payload, error_payload
            print("OK: 5) битый JSON в arguments -> результат инструмента с ключом error, ход не падает")

            # --- 6. зацикливание на tool_calls обрывается по max_iters ---
            tc_loop = {"id": "call_x", "type": "function", "function": {"name": "noop", "arguments": "{}"}}

            async def _post_infinite_tool_calls(session, url, headers, payload, timeout_s=None):
                return 200, {"choices": [{"message": _msg(content=None, tool_calls=[tc_loop])}]}

            with patch(__name__ + "._post", _post_infinite_tool_calls):
                text, msgs = await run_loop(session, [{"role": "user", "content": "зациклись"}], [{"type": "function"}],
                                             PROVIDERS, lambda n, a: "{}", max_iters=3)
                assert isinstance(text, str) and text, text
                assert "цикл" in text.lower(), text
            print("OK: 6) max_iters обрывает зацикливание и возвращает строку, а не виснет")

            # --- 7. reasoning с промежуточного хода tool_calls не протекает в
            # легитимно пустой финальный ответ (утечка chain-of-thought) ---
            queue = [
                _resp(200, _msg(content=None, reasoning_content="думаю над вызовом...", tool_calls=[tc])),
                _resp(200, _msg(content="")),  # финал: пусто, без reasoning
            ]
            with patch(__name__ + "._post", _post_ok):
                text, msgs = await run_loop(session, [{"role": "user", "content": "запиши"}], [{"type": "function"}],
                                             PROVIDERS, _dispatch_ok)
                assert text == "", f"reasoning с прошлого хода протёк в ответ: {text!r}"
            print("OK: 7) reasoning промежуточного хода не протекает в пустой финальный ответ")

            # --- 8. устаревший снимок healthy_keys не обрывает провайдера
            # раньше времени: оба изначально здоровых ключа 429 в рамках
            # ОДНОГО запроса, третий (изначально остывающий, но уже отпустивший)
            # должен быть опробован, а не молча пропущен ---
            _KEY_STATES.clear()
            os.environ["TRIPLE_KEYS"] = "key_a,key_b,key_c"
            triple_provider: list[Provider] = [
                {"base_url": "https://triple.example/v1", "api_key_env": "TRIPLE_KEYS", "model": "gemini-triple"}
            ]
            # key_c был на кулдауне, но он уже истёк к началу этого запроса.
            _get_key_state("key_c", model="gemini-triple").cooldown_until = time.time() - 1
            seen = []

            async def _post_triple(session, url, headers, payload, timeout_s=None):
                seen.append(headers.get("Authorization"))
                if len(seen) <= 2:
                    return 429, {"error": {"message": "quota", "details": [{"retryDelay": "60s"}]}}
                return 200, {"choices": [{"message": _msg(content="ответ с третьего ключа")}]}

            with patch(__name__ + "._post", _post_triple):
                msg_triple = await chat(session, [{"role": "user", "content": "hi"}], [], triple_provider)
                assert msg_triple["content"] == "ответ с третьего ключа", (msg_triple, seen)
                assert len(seen) == 3, seen
            print("OK: 8) устаревший снимок healthy_keys не пропускает отпустивший ключ")

        del os.environ["TEST_KEY_1"]
        del os.environ["TEST_KEY_2"]
        os.environ.pop("MULTI_KEYS", None)

        print("=" * 60)
        print("ALL TESTS PASSED")
        print("=" * 60)

    asyncio.run(main())
