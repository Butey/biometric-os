# План: Исправление взаимной блокировки ключей и таймаутов NVIDIA (Пункты 3 и 4)

## Goal Description
Устранить причину сбоя «Не могу связаться с моделью», когда все провайдеры падают по очереди:
1. **Пункт 3:** Исправить баг в `bot/llm.py`, где кулдаун API-ключа на 429 хранится глобально только по строке ключа. Из-за этого исчерпание квоты на `gemini-3.8-flash` блокировало все 3 Google-ключа для рабочих моделей `gemini-3.7-flash` и `gemini-3.6-flash`.
2. **Пункт 4:** Увеличить таймаут для NVIDIA reasoning-моделей (`moonshotai/kimi-k3` и `deepseek-ai/deepseek-v4-flash-0731`) с жестких 25 секунд до 90 секунд, чтобы модели успевали сгенерировать рассуждения и ответить.

---

## User Review Required

> [!IMPORTANT]
> - Изменения затрагивают файл логики вызовов [`bot/llm.py`](file:///opt/webapps/health_agent_system/bot/llm.py) и конфигурацию [`config.yaml`](file:///opt/webapps/health_agent_system/config.yaml).
> - После внесения изменений живая модель `gemini-3.6-flash` (которая отвечает 200 OK) сможет принимать запросы сразу после того, как `gemini-3.8-flash` отдаст 429, не блокируясь его кулдауном.
> - Потребуется перезапуск сервиса `systemctl restart health-agent` после применения правок.

---

## Proposed Changes

### Компонент 1: Ядро сетевого клиента LLM (`bot/llm.py`)

#### [MODIFY] `bot/llm.py`
1. **Привязка `KeyState` к модели и ключу (`model:key`):**
   ```python
   def _key_id(key: str, model: str = "") -> str:
       return f"{model}:{key}" if model else key

   def _get_key_state(key: str, model: str = "") -> KeyState:
       k = _key_id(key, model)
       state = _KEY_STATES.get(k)
       if state is None:
           state = _KEY_STATES[k] = KeyState()
       return state

   def _ordered_keys(env_name: str, keys: list[str], model: str = "") -> list[str]:
       ...
       healthy = [k for k in rotated if _get_key_state(k, model).cooldown_until <= now]
       cooling = [k for k in rotated if _get_key_state(k, model).cooldown_until > now]
       ...
   ```
2. **Поддержка индивидуального `timeout_s` для каждого провайдера в `chat()`:**
   ```python
   # В цикле for provider in providers:
   model = provider.get("model", "")
   ordered_keys = _ordered_keys(env_name, all_keys, model=model)
   
   # Таймаут запроса: индивидуальный для провайдера или 90с для NVIDIA по умолчанию
   prov_timeout = float(provider.get("timeout_s", timeout_s))
   if "nvidia" in provider.get("base_url", "").lower() and "timeout_s" not in provider:
       prov_timeout = max(prov_timeout, 90.0)
   ```
3. При ошибке 401/403 (невалидный ключ) помечать ключ невалидным глобально (`_get_key_state(key, "").cooldown_until = now + 86400`). При 429/500/503 помечать кулдаун строго для данной модели (`_get_key_state(key, model)`).

---

### Компонент 2: Конфигурация провайдеров (`config.yaml`)

#### [MODIFY] `config.yaml`
1. Для провайдеров NVIDIA явно прописать `timeout_s: 90`:
   ```yaml
       - base_url: https://integrate.api.nvidia.com/v1
         api_key_env: NVIDIA_API_KEY
         model: moonshotai/kimi-k3
         timeout_s: 90
         max_tokens: 16384
         temperature: 1
         reasoning_effort: max
       - base_url: https://integrate.api.nvidia.com/v1
         api_key_env: NVIDIA_API_KEY
         model: deepseek-ai/deepseek-v4-flash-0731
         timeout_s: 90
         temperature: 1
         top_p: 0.95
         max_tokens: 16384
         extra_body:
           chat_template_kwargs:
             thinking: true
             reasoning_effort: high
   ```

---

## Verification Plan

### Automated Tests
1. Запуск встроенных самотестов сетевого клиента:
   ```bash
   .venv/bin/python -m bot.llm
   ```
2. Запуск интеграционных тестов бота:
   ```bash
   .venv/bin/python test_bot.py
   ```
3. Скриптовая проверка: симуляция ошибки 429 на первой модели `gemini-3.8-flash` $\rightarrow$ проверка, что запрос ко второй модели `gemini-3.6-flash` с теми же ключами успешно выполняется и не пропускается.

### Manual Verification
1. Перезапуск службы:
   ```bash
   systemctl restart health-agent
   ```
2. Отправка тестового сообщения боту в Telegram и проверка ответа без ошибки «Не смог связаться с моделью».
