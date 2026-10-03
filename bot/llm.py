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

from bot.history import clean_message
from health_core.config import local_now

log = logging.getLogger(__name__)

_COOLDOWN_NOT_FOUND_S = 86400   # 24 ч — модель не найдена / удалена
_COOLDOWN_RATE_LIMIT_S = 3600   # 1 ч — rate limit
_COOLDOWN_DEFAULT_S = 300       # 5 мин — прочие ошибки

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
        if isinstance(body, dict):
            body["_headers"] = dict(resp.headers)
        return resp.status, body


def _parse_retry_delay(body: Any, default: float = 60.0) -> float:
    """Пытается извлечь время ожидания из ответа об ошибке 429."""
    if isinstance(body, dict):
        headers = body.get("_headers", {})
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
        if retry_after:
            try:
                return max(5.0, float(retry_after))
            except ValueError:
                pass
        
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


def _has_thought_signature(m: dict) -> bool:
    """Проверяет наличие thought_signature (Gemini 3.x) в сообщении или вызовах инструментов."""
    if m.get("thought_signature") or m.get("extra_content"):
        return True
    tool_calls = m.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict):
                if tc.get("thought_signature") or tc.get("extra_content"):
                    return True
                fn = tc.get("function")
                if isinstance(fn, dict) and (fn.get("thought_signature") or fn.get("extra_content")):
                    return True
    return False


def _convert_tool_calls_to_text(messages: list[dict], force_all: bool = False) -> list[dict]:
    """Конвертирует tool_calls и tool-ответы в обычные текстовые сообщения для Google Gemini,
    если они чужие (без thought_signature) или принудительно (force_all)."""
    foreign_tc_ids: set[str] = set()
    tool_id_to_name: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "assistant":
            is_foreign = force_all or (bool(m.get("tool_calls")) and not _has_thought_signature(m))
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict):
                    call_id = tc.get("id")
                    fn = tc.get("function")
                    name = fn.get("name") if isinstance(fn, dict) else None
                    if call_id and name:
                        tool_id_to_name[call_id] = name
                    if is_foreign and call_id:
                        foreign_tc_ids.add(call_id)

    has_foreign_assistant = force_all or any(
        m.get("role") == "assistant" and m.get("tool_calls") and not _has_thought_signature(m)
        for m in messages
    )
    if not has_foreign_assistant and not foreign_tc_ids:
        return messages

    converted: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls") and (force_all or not _has_thought_signature(m)):
            call_lines = []
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = fn.get("name", "tool")
                args = fn.get("arguments", "")
                call_lines.append(f"Вызов функции {name}({args})")
            calls_desc = "\n".join(call_lines)

            parts = []
            if m.get("content"):
                parts.append(str(m["content"]))
            if calls_desc:
                parts.append(calls_desc)

            c = dict(m)
            c.pop("tool_calls", None)
            c["content"] = "\n".join(parts) if parts else ""
            converted.append(c)
        elif role == "tool" and (force_all or m.get("tool_call_id") in foreign_tc_ids or not foreign_tc_ids):
            call_id = m.get("tool_call_id", "")
            fn_name = tool_id_to_name.get(call_id, "")
            res_content = m.get("content", "")
            if fn_name:
                text = f"Результат вызова {fn_name}: {res_content}"
            else:
                text = f"Результат вызова: {res_content}"
            converted.append({
                "role": "user",
                "content": text,
            })
        else:
            converted.append(m)

    return converted


def _build_clean_messages(messages: list[dict], is_google: bool = False, force_text_tools: bool = False) -> list[dict]:
    """Формирует очищенные сообщения для API. Сохраняет ВСЕ поля assistant-сообщения
    (включая thought_signature/extra_content). Для Google Gemini конвертирует чужие
    tool_calls (без thought_signature) в текстовый формат."""
    source = _convert_tool_calls_to_text(messages, force_all=force_text_tools) if (is_google or force_text_tools) else messages
    clean = []
    for m in source:
        if m.get("role") == "assistant":
            c = clean_message(m)
            if m.get("content") is not None:
                c["content"] = m["content"]
            elif not m.get("tool_calls"):
                c["content"] = ""
            else:
                c["content"] = None
            if not c.get("tool_calls"):
                c.pop("tool_calls", None)
            clean.append(c)
        else:
            clean.append(m)
    return clean


def _record_usage(model: str, body: dict) -> None:
    """Одна строка llm_calls на каждый успешный ответ провайдера. Живёт в chat(), а не в
    run_loop: через chat идут и цикл агента, и консилиум (bot/council.py), и сжатие
    истории — так считаются все вызовы, и ровно по разу (фолбэк на другого провайдера
    не успевает записать: запись только после 200 с валидным message).
    Синхронная, вызывается через to_thread: тот копирует ContextVar, а из него берём
    звонящего и пояс (local_now). Никогда не бросает: учёт не должен ломать ход."""
    try:
        from health_core.db import connect
        from plugin import tools as _tools

        usage = body.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        caller = _tools._CALLER_FALLBACK.get()
        conn = connect()
        try:
            conn.execute("PRAGMA busy_timeout=500")  # не держим ход из-за занятой БД
            user_id = None
            if caller:
                row = conn.execute("SELECT id FROM users WHERE telegram_user_id=?", (caller,)).fetchone()
                user_id = row["id"] if row else None
            conn.execute(
                "INSERT INTO llm_calls(user_id, created_at, model, tokens_in, tokens_out) VALUES (?, ?, ?, ?, ?)",
                (user_id, local_now().strftime("%Y-%m-%d %H:%M:%S"), model,
                 usage.get("prompt_tokens"), usage.get("completion_tokens")),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.warning("не удалось записать llm_calls", exc_info=True)


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
        is_google = "googleapis.com" in provider.get("base_url", "")
        clean_messages = _build_clean_messages(messages, is_google=is_google)

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
                state.cooldown_until = time.time() + _COOLDOWN_DEFAULT_S
                state.failure_count += 1
                # Сетевая ошибка часто на уровне хоста/провайдера — пробуем следующий ключ или провайдера
                continue

            if status == 200:
                # Успешный ответ: сбрасываем кулдаун ключа
                state.cooldown_until = 0.0
                state.failure_count = 0
                state.last_status = 200
                try:
                    message = dict(body["choices"][0]["message"])
                except (KeyError, IndexError, TypeError) as exc:
                    msg = f"{provider['model']} (key {_mask_key(key)}): 200, но битый ответ — {exc!r}, тело={body!r}"
                    log.warning(msg)
                    failures.append(msg)
                    continue
                await asyncio.to_thread(_record_usage, provider["model"], body)
                return message

            # Обработка ошибок по ключу
            state.last_status = status
            state.failure_count += 1

            if status == 404:
                # Модель не найдена / закрыта — кулдаун на 24 часа и сразу переход к следующему провайдеру
                state.cooldown_until = time.time() + _COOLDOWN_NOT_FOUND_S
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

            if status in (401, 402, 403):
                # Неверный ключ, баланс или запрет доступа: ставим кулдаун на 1 час
                state.cooldown_until = time.time() + _COOLDOWN_RATE_LIMIT_S
                log.warning("%s (key %s): HTTP %s ошибка авторизации/баланса, ротация на следующий ключ",
                            provider["model"], _mask_key(key), status)
                failures.append(f"{provider['model']}[{_mask_key(key)}]: HTTP {status} Auth/BalanceError")
                continue

            if status in (400, 422):
                if status == 400 and is_google and "thought_signature" in str(body).lower():
                    # Google отклонил запрос из-за thought_signature:
                    # повторяем запрос к этому же провайдеру со всеми tool_calls, переведёнными в текст
                    log.warning("%s (key %s): HTTP 400 thought_signature, повтор с принудительной конвертацией tool_calls в текст",
                                provider["model"], _mask_key(key))
                    retry_payload = dict(payload)
                    retry_payload["messages"] = _build_clean_messages(messages, is_google=True, force_text_tools=True)
                    try:
                        retry_status, retry_body = await _post(session, url, headers, retry_payload, timeout_s=prov_timeout)
                    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                        log.warning("%s (key %s): сетевая ошибка при повторе 400 — %r", provider["model"], _mask_key(key), exc)
                    else:
                        if retry_status == 200:
                            state.cooldown_until = 0.0
                            state.failure_count = 0
                            state.last_status = 200
                            try:
                                message = dict(retry_body["choices"][0]["message"])
                            except (KeyError, IndexError, TypeError) as exc:
                                log.warning("%s (key %s): 200 на повторе, но битый ответ — %r", provider["model"], _mask_key(key), exc)
                            else:
                                await asyncio.to_thread(_record_usage, provider["model"], retry_body)
                                return message

                # Ошибка схемы/параметров (не поддерживаются tools или thinking) — переход к след. провайдеру
                log.warning("%s (key %s): HTTP %s ошибка запроса, переход к следующему провайдеру — %s",
                            provider["model"], _mask_key(key), status, _short(body, 200))
                failures.append(f"{provider['model']}[{_mask_key(key)}]: HTTP {status} — {_short(body, 150)}")
                break

            # Прочие 4xx / 5xx
            state.cooldown_until = time.time() + _COOLDOWN_DEFAULT_S
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
    try:
        result = await asyncio.to_thread(dispatch, name, args)
    except Exception as e:
        # Ранние инструменты хода уже закоммитили в БД: не роняем ход, иначе история
        # теряет их tool_calls и повтор пользователя дублирует запись.
        log.exception("tool %s упал", name)
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
    log.info("tool %s(%s) -> %s", name, _short(raw_args), _short(result))
    return result


def _call_key(call: dict) -> tuple[str, str] | None:
    """(имя, канонические аргументы) — порядок ключей модель между повторами меняет.
    Битый JSON -> None: такой вызов handler всё равно не запускает."""
    try:
        args = json.loads(call["function"].get("arguments") or "{}")
    except json.JSONDecodeError:
        return None
    return call["function"]["name"], json.dumps(args, sort_keys=True, ensure_ascii=False)


def _error_text(result: str) -> str | None:
    """Текст ошибки, если результат инструмента - JSON-объект с ключом "error".
    {"saved": true, "warning": ...} ошибкой не считается."""
    try:
        data = json.loads(result)
    except (ValueError, TypeError):
        return None
    return str(data["error"]) if isinstance(data, dict) and "error" in data else None


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
    failed: dict[tuple[str, str], str] = {}  # вызовы с ошибкой в ЭТОМ ходе -> текст ошибки

    for _ in range(max_iters):
        message = await chat(session, messages, tools, providers, timeout_s=timeout_s)
        messages.append(clean_message(message))  # reasoning и пр. в историю не тащим

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
            key = _call_key(call)
            if key in failed:
                # Модель долбит упавший вызов (12 раз food_lookup по лежащему сервису): не исполняем повторно
                result = json.dumps({"error": (
                    f"Вызов {key[0]} с этими же аргументами уже завершился ошибкой в этом ходе: {failed[key]}. "
                    "Повтор не поможет: измени аргументы или скажи человеку, что пошло не так.")}, ensure_ascii=False)
                log.info("tool %s(%s) -> подавлен повтор упавшего вызова: %s",
                         key[0], _short(call["function"].get("arguments")), _short(failed[key]))
                results.append(result)
                continue
            results.append(await _run_one(call, dispatch))
            err = _error_text(results[-1])
            if key and err is not None:
                failed[key] = err
        for call, result in zip(tool_calls, results, strict=True):
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
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    # chat() пишет в llm_calls: БД строго временная, ДО первого обращения к health_core.db
    # (его DB_PATH считается при импорте; присваиваем и явно на случай раннего импорта).
    os.environ["HEALTH_DB"] = str(Path(tempfile.mkdtemp()) / "health.db")
    import health_core.db as db
    db.DB_PATH = Path(os.environ["HEALTH_DB"])
    assert db.DB_PATH != Path.home() / ".hermes" / "health.db", db.DB_PATH
    _c = db.connect()
    db.migrate(_c)
    _c.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (777, '2026-08-20 00:00:00')")
    _c.commit()
    _c.close()
    from plugin import tools as _plugin_tools

    def _usage_rows() -> list:
        c = db.connect()
        try:
            return c.execute("SELECT * FROM llm_calls ORDER BY id").fetchall()
        finally:
            c.close()

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

            # --- 5b. исключение в dispatch -> error как результат инструмента, ход не падает ---
            queue = [
                _resp(200, _msg(content=None, tool_calls=[tc])),
                _resp(200, _msg(content="ок")),
            ]

            def _dispatch_raises(name, args):
                raise TypeError("boom")

            with patch(__name__ + "._post", _post_ok):
                text, msgs = await run_loop(session, [{"role": "user", "content": "запиши"}], [{"type": "function"}],
                                             PROVIDERS, _dispatch_raises)
                assert text == "ок", text
                tool_msgs = [m for m in msgs if m.get("role") == "tool"]
                assert "TypeError: boom" in json.loads(tool_msgs[0]["content"])["error"], tool_msgs
            print("OK: 5b) исключение в dispatch -> error в результате инструмента, ход не падает")

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

            # --- 9. Bug 1: сохранение thought_signature и других полей в clean_messages ---
            tc_with_ts = {
                "id": "call_gemini",
                "type": "function",
                "function": {"name": "log_water", "arguments": '{"ml": 250}'},
                "thought_signature": "gemini_ts_token_123",
            }
            assistant_with_ts = {
                "role": "assistant",
                "content": None,
                "tool_calls": [tc_with_ts],
                "thought_signature": "gemini_ts_msg_token",
                "extra_content": "some_extra_data",
                "reasoning": "внутренние мысли",
                "refusal": None,
            }
            payloads_received = []

            async def _post_inspect_payload(session, url, headers, payload, timeout_s=None):
                payloads_received.append(payload)
                return 200, {"choices": [{"message": _msg(content="финал")}]}

            with patch(__name__ + "._post", _post_inspect_payload):
                await chat(session, [{"role": "user", "content": "запиши воду"}, assistant_with_ts], [], PROVIDERS)
                assert len(payloads_received) == 1
                sent_msgs = payloads_received[0]["messages"]
                sent_assistant = sent_msgs[1]
                assert sent_assistant["thought_signature"] == "gemini_ts_msg_token", sent_assistant
                assert sent_assistant["extra_content"] == "some_extra_data", sent_assistant
                assert sent_assistant["tool_calls"][0]["thought_signature"] == "gemini_ts_token_123", sent_assistant
                assert "reasoning" not in sent_assistant and "refusal" not in sent_assistant, sent_assistant
            print("OK: 9) thought_signature и extra_content сохраняются в clean_messages")

            # --- 10. Bug 2: конвертация чужих tool_calls без thought_signature перед запросом к Google ---
            google_provider: list[Provider] = [
                {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "api_key_env": "TEST_KEY_1", "model": "gemini-3.5-flash-lite"}
            ]
            foreign_tc = {"id": "gpt_call_1", "type": "function", "function": {"name": "log_weight", "arguments": '{"kg": 75}'}}
            history_with_gpt = [
                {"role": "user", "content": "запиши вес"},
                {"role": "assistant", "content": None, "tool_calls": [foreign_tc]},
                {"role": "tool", "tool_call_id": "gpt_call_1", "content": '{"ok": true}'},
            ]
            google_payloads = []

            async def _post_google(session, url, headers, payload, timeout_s=None):
                google_payloads.append(payload)
                return 200, {"choices": [{"message": _msg(content="вес записан")}]}

            with patch(__name__ + "._post", _post_google):
                resp = await chat(session, history_with_gpt, [], google_provider)
                assert resp["content"] == "вес записан"
                assert len(google_payloads) == 1
                sent_msgs = google_payloads[0]["messages"]
                assert not any(m.get("tool_calls") for m in sent_msgs), sent_msgs
                assert not any(m.get("role") == "tool" for m in sent_msgs), sent_msgs
                assert "log_weight" in sent_msgs[1]["content"]
                assert sent_msgs[2]["role"] == "user"
                assert "log_weight" in sent_msgs[2]["content"]
                assert '{"ok": true}' in sent_msgs[2]["content"]
            print("OK: 10) чужие tool_calls конвертируются в текст перед отправкой в Google Gemini")

            # --- 11. Bug 2 (fallback): HTTP 400 с thought_signature от Google перезапрашивается со сконвертированными tool_calls ---
            google_400_calls = []

            async def _post_google_400_then_ok(session, url, headers, payload, timeout_s=None):
                google_400_calls.append(payload)
                if len(google_400_calls) == 1:
                    return 400, {"error": {"message": "Function call is missing a thought_signature in functionCall parts."}}
                return 200, {"choices": [{"message": _msg(content="успешно после 400")}]}

            with patch(__name__ + "._post", _post_google_400_then_ok):
                ts_msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tc_with_ts],
                    "thought_signature": "corrupted_sig",
                }
                hist_400 = [
                    {"role": "user", "content": "запиши"},
                    ts_msg,
                    {"role": "tool", "tool_call_id": "call_gemini", "content": '{"ok": true}'},
                ]
                resp_400 = await chat(session, hist_400, [], google_provider)
                assert resp_400["content"] == "успешно после 400"
                assert len(google_400_calls) == 2
                assert google_400_calls[0]["messages"][1].get("tool_calls")
                assert not any(m.get("tool_calls") for m in google_400_calls[1]["messages"])
            print("OK: 11) HTTP 400 thought_signature от Google успешно перезапрашивается с конвертацией в текст")

            # --- 12. повтор упавшего вызова (те же аргументы, другой порядок ключей) не исполняется ---
            def _tcw(i: int, args: str) -> dict:
                return {"id": f"w{i}", "type": "function", "function": {"name": "log_water", "arguments": args}}

            queue = [
                _resp(200, _msg(tool_calls=[_tcw(1, '{"ml": 250, "note": "x"}')])),
                _resp(200, _msg(tool_calls=[_tcw(2, '{"note": "x", "ml": 250}')])),
                _resp(200, _msg(tool_calls=[_tcw(3, '{"ml":250,"note":"x"}'), _tcw(4, '{"ml": 300}')])),
                _resp(200, _msg(content="не вышло, скажу человеку")),
            ]
            ran: list = []

            def _dispatch_fails(name, args):
                ran.append(args)
                return json.dumps({"error": "БД недоступна"}, ensure_ascii=False)

            with patch(__name__ + "._post", _post_ok):
                text, msgs = await run_loop(session, [{"role": "user", "content": "выпил"}], [{"type": "function"}],
                                             PROVIDERS, _dispatch_fails)
                assert text == "не вышло, скажу человеку", text
                assert ran == [{"ml": 250, "note": "x"}, {"ml": 300}], ran  # 2 и 3 вызовы не исполнялись, другие аргументы - да
                tool_msgs = [m["content"] for m in msgs if m.get("role") == "tool"]
                assert len(tool_msgs) == 4, tool_msgs
                assert json.loads(tool_msgs[0]) == {"error": "БД недоступна"}, tool_msgs[0]
                for suppressed in (tool_msgs[1], tool_msgs[2]):
                    err = json.loads(suppressed)["error"]
                    assert "уже завершился ошибкой" in err and "БД недоступна" in err and "Повтор не поможет" in err, err
                assert json.loads(tool_msgs[3]) == {"error": "БД недоступна"}, tool_msgs[3]
            print("OK: 12) повтор упавшего вызова подавлен, модель получила пояснение с исходной ошибкой, ход завершён")

            # --- 13. успешные (и saved+warning) повторы исполняются все ---
            for ok_result in ({"ok": True}, {"saved": True, "warning": "частично"}):
                queue = [
                    _resp(200, _msg(tool_calls=[_tcw(5, '{"ml": 250}'), _tcw(6, '{"ml": 250}')])),
                    _resp(200, _msg(tool_calls=[_tcw(7, '{"ml": 250}')])),
                    _resp(200, _msg(content="записал")),
                ]
                ran = []

                def _dispatch_saves(name, args, _r=ok_result):
                    ran.append(args)
                    return json.dumps(_r)

                with patch(__name__ + "._post", _post_ok):
                    text, msgs = await run_loop(session, [{"role": "user", "content": "выпил"}], [{"type": "function"}],
                                                 PROVIDERS, _dispatch_saves)
                    assert text == "записал", text
                    assert len(ran) == 3, ran
            print("OK: 13) одинаковые успешные вызовы (в том числе saved+warning) исполняются каждый раз")

            # --- 14. llm_calls: usage, фолбэк-модель, отсутствие usage, NULL-пользователь ---
            _plugin_tools._CALLER_FALLBACK.set("777")
            before = len(_usage_rows())
            queue = [
                _resp(500, error={"error": "down"}),
                {"status": 200, "body": {"choices": [{"message": _msg(content="привет")}],
                                         "usage": {"prompt_tokens": 123, "completion_tokens": 45}}},
            ]
            with patch(__name__ + "._post", _post_ok):
                text, _ = await run_loop(session, [{"role": "user", "content": "hi"}], [], PROVIDERS, lambda n, a: "{}")
            rows = _usage_rows()[before:]
            assert text == "привет" and len(rows) == 1, (text, [dict(r) for r in rows])
            r = rows[0]
            assert (r["model"], r["tokens_in"], r["tokens_out"], r["cost_usd"]) == ("model-2", 123, 45, None), dict(r)
            assert r["user_id"] == 1 and len(r["created_at"]) == 19, dict(r)

            queue = [_resp(200, _msg(content="без usage"))]
            with patch(__name__ + "._post", _post_ok):
                await run_loop(session, [{"role": "user", "content": "hi"}], [], PROVIDERS, lambda n, a: "{}")
            r = _usage_rows()[-1]
            assert (r["model"], r["tokens_in"], r["tokens_out"]) == ("model-1", None, None), dict(r)

            _plugin_tools._CALLER_FALLBACK.set(None)
            queue = [_resp(200, _msg(content="аноним"))]
            with patch(__name__ + "._post", _post_ok):
                await run_loop(session, [{"role": "user", "content": "hi"}], [], PROVIDERS, lambda n, a: "{}")
            assert _usage_rows()[-1]["user_id"] is None, dict(_usage_rows()[-1])

            # одна строка на каждый ответ модели: tool_call + финал = 2
            before = len(_usage_rows())
            queue = [_resp(200, _msg(tool_calls=[tc])), _resp(200, _msg(content="готово"))]
            with patch(__name__ + "._post", _post_ok):
                await run_loop(session, [{"role": "user", "content": "hi"}], [{"type": "function"}], PROVIDERS, _dispatch_ok)
            assert len(_usage_rows()) - before == 2, len(_usage_rows()) - before
            print("OK: 14) llm_calls: токены, модель после фолбэка, NULL без usage, NULL без пользователя, по строке на ответ")

            # --- 15. сбой записи usage не ломает ход ---
            before = len(_usage_rows())
            queue = [_resp(200, _msg(content="ответ при сломанной БД"))]
            with patch(__name__ + "._post", _post_ok), patch("health_core.db.connect", side_effect=RuntimeError("disk")):
                text, _ = await run_loop(session, [{"role": "user", "content": "hi"}], [], PROVIDERS, lambda n, a: "{}")
            assert text == "ответ при сломанной БД", text
            assert len(_usage_rows()) == before
            print("OK: 15) исключение при записи llm_calls проглочено, ход вернул ответ")

        del os.environ["TEST_KEY_1"]
        del os.environ["TEST_KEY_2"]
        os.environ.pop("MULTI_KEYS", None)

        print("=" * 60)
        print("ALL TESTS PASSED")
        print("=" * 60)

    asyncio.run(main())


async def check_balances(session: aiohttp.ClientSession, providers: list[Provider]) -> str:
    lines = []
    for p in providers:
        keys = get_provider_keys(p)
        if not keys:
            lines.append(f"❌ {p['model']}: нет ключей в {p.get('api_key_env')}")
            continue
            
        key = keys[0]
        url = f"{p['base_url'].rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        
        # openrouter native billing endpoint if applicable
        if "openrouter.ai" in url:
            try:
                auth_url = "https://openrouter.ai/api/v1/auth/key"
                async with session.get(auth_url, headers=headers, timeout=5) as r:
                    if r.status == 200:
                        data = await r.json()
                        remain = data.get("data", {}).get("limit_remaining")
                        if remain is not None:
                            lines.append(f"✅ {p['model']}: баланс {remain:.3f} USD")
                            continue
            except Exception:
                pass

        # Google Gemini API
        if "generativelanguage.googleapis.com" in url:
            lines.append(f"✅ {p['model']}: квоты Google проверяются в Cloud Console (обычно free tier работает ок)")
            continue

        payload = {
            "model": p["model"],
            "messages": [{"role": "user", "content": "1"}],
            "max_tokens": 1
        }
        try:
            status, body = await _post(session, url, headers, payload, timeout_s=10.0)
            if status == 200:
                lines.append(f"✅ {p['model']}: API доступен (баланс в норме)")
            elif status == 402:
                lines.append(f"❌ {p['model']}: 402 Insufficient balance (кончились деньги)")
            elif status in (401, 403):
                lines.append(f"❌ {p['model']}: {status} Неверный ключ или отказ в доступе")
            elif status == 429:
                lines.append(f"⚠️ {p['model']}: 429 Превышен лимит запросов (Rate Limit)")
            else:
                lines.append(f"❓ {p['model']}: статус {status}")
        except Exception as e:
            lines.append(f"❌ {p['model']}: сетевая ошибка ({e})")
            
    return "\n".join(lines)

