"""Настройка соединения с LLM и чтение каталога моделей для админ-панели."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request


def from_form(form: dict, *, require_model: bool = True) -> dict:
    base_url = form.get("base_url", "").strip().rstrip("/")
    try:
        url = urllib.parse.urlsplit(base_url)
        valid = (url.scheme in {"http", "https"} and url.hostname and url.port != 0
                 and not url.username and not url.password and not url.query and not url.fragment
                 and not any(ch.isspace() or ord(ch) < 32 for ch in base_url))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Base URL должен быть HTTP(S)-адресом без пароля, параметров и фрагмента.")
    env_var = form.get("api_key_env", "").strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,65}_API_KEY", env_var):
        raise ValueError("Имя переменной ключа должно оканчиваться на _API_KEY, например CUSTOM_API_KEY.")
    api = form.get("api", "openai")
    if api not in {"openai", "anthropic"}:
        raise ValueError("Неизвестный формат API.")
    if api == "anthropic" and base_url.endswith("/v1"):
        base_url = base_url[:-3]
    if base_url.endswith(("/chat/completions", "/messages")):
        raise ValueError("Укажите Base URL без /chat/completions или /messages.")
    model = (form.get("custom_model", "").strip() or form.get("model", "").strip())
    if require_model and (not model or len(model) > 250 or any(ch.isspace() for ch in model)):
        raise ValueError("Выберите модель или введите её точный ID без пробелов.")
    key = form.get("new_api_key", "").strip()
    if any(ch.isspace() or ord(ch) < 32 for ch in key):
        raise ValueError("API-ключ не должен содержать пробелы или переводы строк.")
    provider = {"base_url": base_url, "model": model, "api_key_env": env_var}
    if api == "anthropic":
        provider["api"] = api
    return provider


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Не пересылаем авторизацию на другой адрес по редиректу каталога.
        return None


def list_models(provider: dict, new_key: str = "") -> list[str]:
    from bot.llm import get_provider_keys
    keys = get_provider_keys(provider)
    key = new_key.strip() or next(iter(keys), "")
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if provider.get("api") == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    suffix = "/v1/models" if provider.get("api") == "anthropic" else "/models"
    req = urllib.request.Request(provider["base_url"] + suffix, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=10) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Каталог моделей слишком большой; введите ID модели вручную.")
        payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Каталог моделей: HTTP {exc.code}. Проверьте Base URL и ключ или введите модель вручную.") from None
    except (OSError, json.JSONDecodeError, UnicodeError):
        # URL и текст удалённого ответа могут содержать ключ — не возвращаем их браузеру.
        raise ValueError("Не удалось прочитать каталог моделей. Проверьте соединение или введите ID вручную.") from None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Провайдер не вернул список моделей; введите ID вручную.")
    models = sorted({row["id"] for row in rows if isinstance(row, dict)
                     and isinstance(row.get("id"), str) and 0 < len(row["id"]) <= 250
                     and not any(ch.isspace() for ch in row["id"])})
    if not models:
        raise ValueError("Каталог пуст; введите ID модели вручную.")
    return models
