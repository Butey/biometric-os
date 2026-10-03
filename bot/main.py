"""Точка входа: телеграм-бот вместо шлюза Hermes. Контракт — Docs/bot_design.md.

Запускается ИЗ КАТАЛОГА ПРОЕКТА. Копий кода, персоны и config.yaml в
~/.hermes/ больше нет: правка файла здесь — правка боевого кода. От ~/.hermes/
остались только .env (общий канал секретов с админкой) и health.db.

    python -m bot.main
"""
try:
    import defusedxml
    defusedxml.defuse_stdlib()
except ImportError:
    pass

import asyncio
import base64
import contextlib
import contextvars
import hashlib
import io
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat,
                           CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message)
from aiogram.utils.chat_action import ChatActionSender

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    # Сеть с TLS-перехватом (антивирус, корпоративный прокси, провайдер) подсовывает
    # свой корневой сертификат: он лежит в хранилище ОС, но не в бандле certifi,
    # которым python проверяет цепочку по умолчанию — и api.telegram.org отваливается
    # с CERTIFICATE_VERIFY_FAILED. truststore переключает проверку на хранилище ОС.
    # Проверку сертификатов НЕ отключает — это было бы дырой ради удобства.
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass          # чистая сеть (обычный VPS) — работает и без него

from admin.auth import load_env_file          # тот же .env, что у панели — второй читалки не заводим
from bot import council, history, knowledge, llm, registry
from health_core import config
from health_core.config import load as load_config
from health_core.report import morning_checklist
from health_core.db import connect, migrate

log = logging.getLogger("bot")

SOUL_PATH = ROOT / "Core" / "system_promt.md"
TOOL_RULES_PATH = ROOT / "Core" / "tool_rules.md"
TELEGRAM_LIMIT = 4096

# Встроенные команды шлюза, которых мы лишились вместе с Hermes. Плагин их не
# даёт: у него инструменты, а не команды.
BUILTIN_COMMANDS = {
    "new": "Забыть контекст разговора",
    "help": "Что я умею",
    "status": "Статус-бар: вес, калории, гарды",
    "checklist": "Утренние замеры: что записано, чего не хватает",
    "compact": "Сжать историю диалога в краткую сводку",
    "target": "Интерактивный подбор калорийности и вехи",
    "plateau": "Прогноз и разбор плато массы тела",
    "forecast": "Прогноз динамики веса",
}

# Видны в меню только в чатах администраторов (BotCommandScopeChat); у остальных
# их нет — вызов всё равно отбивается проверкой прав в run_command.
ADMIN_COMMANDS = {
    "model": "Смена активной модели",
    "access": "Заявки и допущенные пользователи",
    "approve": "Одобрить доступ: /approve <tg_id>",
    "deny": "Отклонить заявку: /deny <tg_id>",
    "revoke": "Отозвать доступ: /revoke <tg_id>",
}
_ADMIN_ONLY_SLASH = {"users", "mode"}


def menu_commands(admin: bool) -> list[BotCommand]:
    """Меню Telegram: встроенные команды + слэш-команды плагина (+ админские)."""
    items = dict(BUILTIN_COMMANDS)
    items.update({n: d for n, _h, d in registry.SLASH_COMMANDS if admin or n not in _ADMIN_ONLY_SLASH})
    if admin:
        items.update(ADMIN_COMMANDS)
    return [BotCommand(command=c, description=d[:256]) for c, d in items.items()]

_soul_cache: tuple[float, str] | None = None

# Замок на пользователя, см. _user_lock. Словарь не чистится: ключей ровно
# столько, сколько людей написало боту за жизнь процесса.
_USER_LOCKS: dict[str, asyncio.Lock] = {}


def system_prompt() -> str:
    """Персона с диска + индекс знаний. Перечитывается по mtime: персону правят
    на живой системе, и перезапуск ради одной строки — это ровно то трение,
    из-за которого правки копятся неделями."""
    global _soul_cache
    mtime = SOUL_PATH.stat().st_mtime
    if _soul_cache is None or _soul_cache[0] != mtime:
        _soul_cache = (mtime, SOUL_PATH.read_text(encoding="utf-8"))
    # Индекс знаний НЕ кэшируем вместе с персоной: файлы добавляются через
    # админку в любой момент, и модель должна увидеть их сразу.
    return _soul_cache[1] + "\n\n" + knowledge.index()


def tool_specs() -> list[dict]:
    """Инструменты плагина + knowledge. Собирается на каждый ход: состав тем
    знаний зависит от каталога, admin_cmd виден только в режиме admin."""
    tools = registry.openai_tools()
    if not knowledge._is_admin():
        tools = [t for t in tools if t["function"]["name"] != "admin_cmd"]
    return tools + [{
        "type": "function",
        "function": {
            "name": "knowledge",
            "description": "Прочитать справочный материал: протокол, препараты, вехи, книги по питанию",
            "parameters": knowledge.schema(),
        },
    }]


_tool_rules_cache: tuple[float, dict[str, str]] | None = None
_TOOL_RULES_MARK = "\n\n[ПРАВИЛА "


def _tool_rules() -> dict[str, str]:
    """Core/tool_rules.md -> {имя инструмента: текст правил}, секции по `## имя`.
    Перечитывается по mtime, тот же приём, что у system_prompt()."""
    global _tool_rules_cache
    mtime = TOOL_RULES_PATH.stat().st_mtime
    if _tool_rules_cache is None or _tool_rules_cache[0] != mtime:
        rules: dict[str, str] = {}
        name, buf = None, []
        for line in TOOL_RULES_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                if name is not None:
                    rules[name] = "\n".join(buf).strip()
                name, buf = line[3:].strip(), []
            elif name is not None:
                buf.append(line)
        if name is not None:
            rules[name] = "\n".join(buf).strip()
        _tool_rules_cache = (mtime, rules)
    return _tool_rules_cache[1]


def with_tool_rules(name: str, result: str) -> str:
    """Приклеить к результату инструмента его правила из Core/tool_rules.md —
    модель видит их в ходе, где вызвала инструмент, а не в каждом системном
    промпте (см. Core/tool_rules.md — так экономится ~2.3к токенов на вызов).
    Инструментов без секции в tool_rules.md (большинство) не касается."""
    rule = _tool_rules().get(name)
    if not rule:
        return result
    return f"{result}{_TOOL_RULES_MARK}{name}]\n{rule}"


def strip_tool_rules(text: str) -> str:
    """Обратное к with_tool_rules: убрать приклеенный блок правил перед тем,
    как сохранить результат инструмента в chat_history — иначе он платит
    токенами в каждом последующем ходе, а не только в том, где был нужен."""
    idx = text.find(_TOOL_RULES_MARK)
    return text[:idx] if idx != -1 else text


# Пульт/отчёт уходят человеку готовым блоком в ``` — и остаются такими же в
# истории. Модель их оттуда копирует дословно вместо нового вызова инструмента:
# наблюдалось на проде (в ответе на «пульт» пришёл пульт четырёхчасовой
# давности, со старым временем в шапке и нулевой клетчаткой). В историю кладём
# заглушку: скопировать нечего, придётся звать инструмент.
_PANEL_RE = re.compile(r"```.*?```", re.DOTALL)
_PANEL_STUB = ("<!-- METABOLIC_PANEL_SHOWN: блок пульта передан человеку. "
               "Числа устарели — вызови инструмент заново, из истории не копируй -->")


def strip_panels(text: str) -> str:
    return _PANEL_RE.sub(_PANEL_STUB, text)



# Сообщение обсуждает план/вариант или задаёт вопрос, и в нём нет слов «съел/выпил/запиши».
# Тогда log_food(add) отклоняется кодом: правило промпта «записывать только съеденное»
# модель нарушала («тунец можем добавить к яйцам» -> завтрак записан как съеденный).
_PLAN_RE = re.compile(
    r"можем|можно|давай|может\b|предлаг|планир|план\b|собери|придума|что\s+(на|если|взять|приготов)|"
    r"а\s+если|стоит\s+ли|как\s+насчёт|вариант|собираюсь|хочу|хотел|буду|будем|завтра|\?", re.IGNORECASE)
_EATEN_RE = re.compile(
    r"съел|поел|позавтракал|пообедал|поужинал|перекусил|выпил|доел|\bел[а]?\b|\bпил[а]?\b|записыв|запиши|записал|внеси",
    re.IGNORECASE)
_PLAN_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar("plan_only", default=False)


def is_plan_only(text: str) -> bool:
    return bool(_PLAN_RE.search(text)) and not _EATEN_RE.search(text)


def dispatch(name: str, args: dict) -> str:
    """Единая точка исполнения инструмента. knowledge живёт не в плагине,
    поэтому маршрутизируется здесь, а не внутри registry. Дергается только
    из цикла LLM (llm.run_loop) — слэш-команды и скрипты идут через
    registry.dispatch напрямую и правил инструментов не видят."""
    if name == "knowledge":
        result = knowledge.read(args.get("topic", ""), args.get("query"))
    elif name == "log_food" and (args.get("action") or "add") == "add" and _PLAN_ONLY.get():
        return json.dumps({"error": "Сообщение человека - обсуждение плана или вопрос, а не «съел». НЕ записывай приём пищи. "
                                    "Ответь по сути и спроси одной строкой: записать как съеденное?"}, ensure_ascii=False)
    else:
        result = registry.dispatch(name, args)
    return with_tool_rules(name, result)


def allowed_users() -> set[str]:
    """TELEGRAM_ALLOWED_USERS — разовый бутстрап со старой (однопользовательской)
    схемы, не источник правды. На старте _bootstrap_env_allowlist заводит этим id
    approved-строку в access_list, и дальше доступ живёт там; переменную можно
    не заполнять вовсе."""
    raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    return {p.strip() for p in raw.replace(";", ",").split(",") if p.strip()}


def _now_iso() -> str:
    return config.local_now().strftime("%Y-%m-%d %H:%M:%S")


def _bootstrap_env_allowlist(conn) -> None:

    """Разовая миграция: каждому id из TELEGRAM_ALLOWED_USERS заводим approved-
    строку в access_list, если её ещё нет — так существующие до этой правки
    пользователи не теряют доступ. Идемпотентно: повторный запуск ничего не
    портит, строка заводится только при её полном отсутствии."""
    now = _now_iso()
    for uid in allowed_users():
        row = conn.execute(
            "SELECT 1 FROM access_list WHERE telegram_user_id=?", (uid,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO access_list(telegram_user_id, status, requested_at, decided_at) "
                "VALUES (?, 'approved', ?, ?)",
                (uid, now, now),
            )
    conn.commit()


def _display_name(from_user) -> str:
    if from_user and from_user.username:
        return f"@{from_user.username}"
    if from_user and from_user.full_name:
        return from_user.full_name
    return str(from_user.id) if from_user else "?"


def _check_access(uid: str, from_user) -> str:
    """Синхронный, для asyncio.to_thread. Возвращает статус ('approved',
    'pending', 'denied') или 'new_pending', когда строки ещё не было и мы её
    только что завели этим сообщением — отдельное значение нужно, чтобы
    вызывающий код отправил заявку администратору и ответ человеку РОВНО
    один раз, а не при каждом следующем сообщении того же pending-человека."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status FROM access_list WHERE telegram_user_id=?", (uid,)
        ).fetchone()
        if row is not None:
            return row["status"]
        conn.execute(
            "INSERT INTO access_list(telegram_user_id, status, username, requested_at) "
            "VALUES (?, 'pending', ?, ?)",
            (uid, _display_name(from_user), _now_iso()),
        )
        conn.commit()
        return "new_pending"
    finally:
        conn.close()


def _chunks(text: str, limit: int) -> list[str]:
    """Режем по границам абзацев: статус-бар и отчёты — таблицы, разрыв
    посередине строки делает их нечитаемыми."""
    if len(text) <= limit:
        return [text]
    limit -= 8                            # запас под закрывающий/открывающий ``` на стыках
    out, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > limit:
            out.append(cur)
            cur = ""
        while len(para) > limit:          # один абзац длиннее лимита — режем как есть
            out.append(para[:limit])
            para = para[limit:]
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    # граница внутри ``` блока: закрываем в конце куска и открываем в начале следующего
    in_fence = False
    for i, chunk in enumerate(out):
        reopen = in_fence
        for line in chunk.split("\n"):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
        out[i] = ("```\n" if reopen else "") + chunk + ("\n```" if in_fence else "")
    return out


async def send_long(message: Message, text: str) -> None:
    """Разметка приходит от модели и бывает битой (незакрытая звёздочка). Когда
    телеграм отказывается её разбирать, шлём тот же кусок без разметки:
    молчание в ответ на вопрос хуже сырого текста."""
    if "METABOLIC_PANEL_SHOWN" in text or "готовый блок показан человеку" in text:
        text = text.replace(_PANEL_STUB, "").replace(
            "[готовый блок показан человеку; его числа устарели — вызови инструмент заново, из истории не копируй]", ""
        ).strip()
        if not text:
            text = "Сводку обновил в базе данных. Чтобы посмотреть свежий пульт, напиши «пульт» или «статус»."
    if not text.strip():
        # Пустой ответ телеграм отвергает, и вместо ошибки человек видит тишину.
        # Молчание — худший из возможных ответов: непонятно, дошло ли вообще.
        text = "Готово."
    for chunk in _chunks(text, TELEGRAM_LIMIT):
        try:
            sent = await message.answer(chunk)
            log.info("отправлено chat=%s message_id=%s (%d симв)",
                     message.chat.id, sent.message_id, len(chunk))
        except TelegramBadRequest as e:
            # Разметка от модели бывает битой. Логируем причину: без неё
            # «бот молчит» неотличимо от «бот не понял».
            log.warning("разметка отвергнута (%s), шлём без неё", e)
            sent = await message.answer(chunk, parse_mode=None)
            log.info("отправлено без разметки chat=%s message_id=%s",
                     message.chat.id, sent.message_id)


async def _answer_md(message: Message, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except TelegramBadRequest as e:
        log.warning("разметка отвергнута (%s), шлём без неё", e)
        await message.answer(text, reply_markup=kb, parse_mode=None)


def _plain(raw: str) -> str:
    """Инструменты отдают JSON — человеку он не нужен. Достаём текстовое поле,
    а нет его — показываем как есть: молча проглотить ответ хуже."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if not isinstance(data, dict):
        return raw
    for key in ("text", "help", "status_bar", "error"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(data, ensure_ascii=False, indent=2)


def admin_user_ids() -> set[str]:
    """Список telegram id администраторов из config.yaml."""
    cfg = load_config()
    raw_ids = (cfg.get("admin") or {}).get("telegram_admin_ids") or []
    return {str(i).strip() for i in raw_ids if str(i).strip()}


def _format_models_message(providers: list[dict]) -> str:
    lines = ["🤖 **Цепочка моделей нейросетей:**\n"]
    for i, p in enumerate(providers):
        m = p.get("model", f"model-{i+1}")
        env = p.get("api_key_env", "")
        if i == 0:
            lines.append(f"**{i+1}. 🟢 `{m}`** — *основная (активна)*")
        else:
            lines.append(f"{i+1}. `{m}` `[{env}]`")
    lines.append("\nДля смены нажмите кнопку ниже или введите:\n`/model <номер или название>` (например `/model 2` или `/model gemini`)")
    return "\n".join(lines)


def _provider_id(p: dict) -> str:
    # короткий хэш: имя модели может повторяться, а callback_data <= 64 байт
    raw = f"{p.get('model')}|{p.get('base_url')}|{p.get('api_key_env')}"
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def _model_keyboard(providers: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for i, p in enumerate(providers):
        m_name = p.get("model", f"model-{i+1}")
        short_name = m_name.split("/")[-1]
        prefix = "🟢 " if i == 0 else ""
        text = f"{prefix}{i+1}. {short_name}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"switch_model:{_provider_id(p)}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def switch_model_cmd(args: str) -> str:
    """Обработчик текстовой команды /model <args>."""
    from admin.server import _update_providers_in_config
    current_cfg = load_config()
    providers = list((current_cfg.get("bot") or {}).get("providers", []))
    if not providers:
        return "В config.yaml нет зарегистрированных моделей."

    args = args.strip()
    if not args:
        return _format_models_message(providers)

    target_idx = None
    if args.isdigit():
        idx = int(args) - 1
        if 0 <= idx < len(providers):
            target_idx = idx
        else:
            return f"❌ Неверный номер модели (доступно от 1 до {len(providers)})."
    else:
        q = args.lower()
        for i, p in enumerate(providers):
            if q in p.get("model", "").lower():
                target_idx = i
                break
        if target_idx is None:
            avail = ", ".join(f"{i+1}. {p.get('model', '').split('/')[-1]}" for i, p in enumerate(providers))
            return f"❌ Модель '{args}' не найдена.\nДоступные модели:\n{avail}"

    if target_idx == 0:
        return f"ℹ️ Модель `{providers[0].get('model')}` уже является основной (приоритет #1)."

    chosen = providers.pop(target_idx)
    new_providers = [chosen] + providers
    try:
        _update_providers_in_config(new_providers)
    except Exception as exc:
        return f"❌ Ошибка сохранения config.yaml: {exc}"

    return (
        f"✅ Основной моделью установлена:\n"
        f"**`{chosen.get('model')}`** (приоритет #1)\n\n"
        + _format_models_message(new_providers)
    )


async def _cb_allowed(callback: CallbackQuery) -> bool:
    """Тот же доступ, что и у сообщений: админ или approved. Новых заявок
    из callback не заводим - только проверяем."""
    uid = str(callback.from_user.id)
    if uid not in admin_user_ids():
        def _status() -> str | None:
            conn = connect()
            try:
                row = conn.execute(
                    "SELECT status FROM access_list WHERE telegram_user_id=?", (uid,)
                ).fetchone()
                return row["status"] if row else None
            finally:
                conn.close()
        if await asyncio.to_thread(_status) != "approved":
            await callback.answer("Нет доступа", show_alert=True)
            return False
    return True


_NOT_REGISTERED = "Профиль ещё не зарегистрирован. Сначала напиши боту о себе."


def _resolve_uid_to_user_id(conn, uid: str) -> int | None:
    # Нет строки - None: подстановка чужого users.id=1 читала и писала данные другого человека.
    row = conn.execute("SELECT id FROM users WHERE telegram_user_id=?", (uid,)).fetchone()
    return row["id"] if row else None


def _format_target_interactive(uid: str, args: str = "") -> tuple[str, InlineKeyboardMarkup | None]:
    from health_core import forecast as _fc
    conn = connect()
    try:
        user_id = _resolve_uid_to_user_id(conn, uid)
        if user_id is None:
            return _NOT_REGISTERED, None

        args = args.strip()
        target_kcal = None
        target_kg = None
        deadline = None

        if args:
            parts = args.split()
            if len(parts) == 1:
                try:
                    val = float(parts[0])
                    if val >= 500:
                        target_kcal = val
                    else:
                        target_kg = val
                except ValueError:
                    if "-" in parts[0]:
                        deadline = parts[0]
            elif len(parts) >= 2:
                try:
                    target_kg = float(parts[0])
                except ValueError:
                    target_kg = None
                if parts[1].replace(".", "", 1).isdigit():
                    target_kcal = float(parts[1])
                else:
                    deadline = parts[1]

        res = _fc.calibrate(
            conn, user_id, target_kg=target_kg, deadline=deadline,
            target_kcal=target_kcal, apply=False
        )

        if "error" in res:
            err = res["error"]
            if "нет активной вехи" in err.lower():
                msg = (
                    "🎯 **Интерактивный подбор калорийности**\n\n"
                    "У вас пока нет активной вехи по весу.\n"
                    "Задайте целевой вес командой:\n"
                    "`/target <целевой вес в кг>` (например, `/target 80`)\n"
                    "или с желаемой датой: `/target 80 2026-12-31`."
                )
                return msg, None
            return f"ℹ️ {err}", None

        mode = res.get("mode")

        if mode == "force_kcal":
            is_safe = res.get("safe", False)
            sw = res.get("start_weight_kg")
            t_kg = res.get("target_kg")
            t_kcal = res.get("target_kcal")
            def_kcal = res.get("deficit_kcal")
            floor = res.get("kcal_floor")
            floor_r = res.get("floor_reason")
            aligned = res.get("aligned_deadline")
            macros = res.get("macros") or {}
            p = macros.get("protein_g", "-")
            f = macros.get("fat_g", "-")
            c = macros.get("carbs_g", "-")

            if not is_safe:
                warns = "\n".join(f"• ⚠️ {w}" for w in res.get("safety_warnings", []))
                msg = (
                    f"🎯 **Проверка безопасности калорийности**\n\n"
                    f"Запрошено: **{t_kcal} ккал/сут** (дефицит {def_kcal} ккал)\n"
                    f"Текущий вес: **{sw} кг** ➔ Цель: **{t_kg} кг**\n"
                    f"Безопасный минимум (пол): **{floor} ккал** ({floor_r})\n\n"
                    f"{warns}\n\n"
                    f"Снижение ниже {floor} ккал сопряжено с рисками сжигания мышц. "
                    f"Выберите безопасную цель или воспользуйтесь подбором."
                )
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Выбрать безопасный вариант", callback_data="target_back")],
                ])
                return msg, kb

            cur_ms = res.get("current_milestone") or {}
            cur_dl = cur_ms.get("deadline")
            dl_info = f" (текущий срок: {cur_dl})" if cur_dl else ""
            msg = (
                f"🎯 **Согласование калорийности и вехи**\n\n"
                f"• Калорийность: **{t_kcal} ккал/сут** (дефицит {def_kcal} ккал)\n"
                f"• Безопасность: ✅ в безопасном коридоре (пол {floor} ккал)\n"
                f"• Расчётный срок достижения цели **{t_kg} кг**: **{aligned}**{dl_info}\n"
                f"• Макронутриенты: 🥩 Б **{p} г** | 🥑 Ж **{f} г** | 🍞 У **{c} г**\n\n"
                f"Зафиксировать суточную цель и обновить срок вехи?"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Зафиксировать {t_kcal} ккал и срок {aligned}", callback_data=f"target_apply:{t_kcal}")],
                [InlineKeyboardButton(text="◀️ Назад к вариантам", callback_data="target_back")],
            ])
            return msg, kb

        sw = res.get("start_weight_kg")
        t_kg = res.get("target_kg")
        tdee = res.get("daily_expenditure_kcal")
        floor = res.get("kcal_floor")
        floor_r = res.get("floor_reason")
        options = res.get("options") or []

        lines = [
            "🎯 **Подбор оптимальной целевой калорийности**\n",
            f"Текущий вес: **{sw} кг** ➔ Цель (веха): **{t_kg} кг**",
            f"Расход (TDEE): **{tdee} ккал/сут** | Безопасный пол: **{floor} ккал** ({floor_r})\n",
        ]

        buttons = []

        if mode == "target_and_deadline":
            dl = res.get("deadline")
            rem = res.get("days_remaining")
            req_kcal = res.get("required_intake_kcal")
            req_def = res.get("required_deficit_kcal")
            dl_safe = res.get("deadline_safe")
            unreach = res.get("unreachable_reason")
            opt_rec = res.get("optimal_recommendation") or {}
            exp_date = opt_rec.get("realistic_expected")

            lines.append("📅 **Анализ срока текущей вехи:**")
            lines.append(f"• Срок: **{dl}** (осталось {rem} дн.)")
            lines.append(f"• Требуемый калораж: **{req_kcal} ккал/сут** (дефицит {req_def} ккал)")
            if dl_safe:
                lines.append("• Статус: ✅ **Реалистичен и безопасен**")
                buttons.append([InlineKeyboardButton(text=f"✅ Зафиксировать цель вехи ({req_kcal} ккал)", callback_data=f"target_apply:{req_kcal}")])
            else:
                lines.append(f"• Статус: ⚠️ **Недостижим безопасно** ({unreach})")
                if exp_date:
                    lines.append(f"• Реалистичный срок при безопасном дефиците: **{exp_date}**")
            lines.append("")

        lines.append("⚡ **Варианты темпа:**")
        opt_btns = []
        for opt in options:
            name = opt.get("name")
            lbl = opt.get("label")
            kcal = opt.get("intake_kcal")
            def_k = opt.get("deficit_kcal")
            rate = opt.get("weekly_rate_kg")
            exp_d = opt.get("expected_date")
            lines.append(f"• **{lbl}**: **{kcal} ккал/сут** (дефицит {def_k} ккал, ~{rate} кг/нед) 📅 Срок: **{exp_d}**")

            if name == "optimal":
                buttons.insert(0 if not buttons else 1, [InlineKeyboardButton(text=f"🟢 Оптимальный ({kcal} ккал)", callback_data=f"target_opt:{name}")])
            else:
                emoji = "🛋" if name == "comfort" else "⚡"
                opt_btns.append(InlineKeyboardButton(text=f"{emoji} {lbl.split()[0]} ({kcal})", callback_data=f"target_opt:{name}"))

        if opt_btns:
            buttons.append(opt_btns)

        buttons.append([
            InlineKeyboardButton(text="📊 Прогноз плато", callback_data="nav_plateau"),
            InlineKeyboardButton(text="📈 Траектория", callback_data="nav_forecast"),
        ])

        lines.append("\n*Нажмите кнопку для выбора варианта или введите `/target <ккал>`.*")
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)
    finally:
        conn.close()


def _format_target_option_preview(uid: str, opt_name: str) -> tuple[str, InlineKeyboardMarkup | None]:
    from health_core import forecast as _fc
    conn = connect()
    try:
        user_id = _resolve_uid_to_user_id(conn, uid)
        if user_id is None:
            return _NOT_REGISTERED, None
        res = _fc.calibrate(conn, user_id)
        if "error" in res:
            return f"ℹ️ {res['error']}", None

        target_kg = res.get("target_kg")
        options = res.get("options") or []
        chosen = next((o for o in options if o.get("name") == opt_name), None)
        if not chosen:
            return "Вариант не найден.", None

        lbl = chosen.get("label")
        kcal = chosen.get("intake_kcal")
        def_k = chosen.get("deficit_kcal")
        rate = chosen.get("weekly_rate_kg")
        exp_d = chosen.get("expected_date")
        earliest_d = chosen.get("earliest_date")
        macros = chosen.get("macros") or {}
        p = macros.get("protein_g", "-")
        f = macros.get("fat_g", "-")
        c = macros.get("carbs_g", "-")

        earliest_info = f" (самый ранний: {earliest_d})" if earliest_d else ""

        msg = (
            f"🎯 **Вариант «{lbl}»**\n\n"
            f"• Целевая калорийность: **{kcal} ккал/сут** (дефицит {def_k} ккал)\n"
            f"• Ожидаемый темп сброса: **~{rate} кг в неделю**\n"
            f"• Расчётный срок вехи ({target_kg} кг): **{exp_d}**{earliest_info}\n"
            f"• Рекомендуемые макросы:\n"
            f"  🥩 Белки: **{p} г** | 🥑 Жиры: **{f} г** | 🍞 Углеводы: **{c} г**\n\n"
            f"Зафиксировать суточную цель и обновить срок активной вехи?"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Зафиксировать {kcal} ккал и срок {exp_d}", callback_data=f"target_apply:{kcal}")],
            [InlineKeyboardButton(text="◀️ Назад к вариантам", callback_data="target_back")],
        ])
        return msg, kb
    finally:
        conn.close()


def _apply_target(uid: str, kcal: float) -> tuple[str, InlineKeyboardMarkup | None]:
    from health_core import forecast as _fc
    conn = connect()
    try:
        user_id = _resolve_uid_to_user_id(conn, uid)
        if user_id is None:
            return _NOT_REGISTERED, None
        res = _fc.calibrate(conn, user_id, target_kcal=kcal, apply=True)
        if "error" in res:
            return f"❌ {res['error']}", None

        t_kcal = res.get("target_kcal")
        def_k = res.get("deficit_kcal")
        t_kg = res.get("target_kg")
        dl = res.get("aligned_deadline")
        macros = res.get("macros") or {}
        p = macros.get("protein_g", "-")
        f = macros.get("fat_g", "-")
        c = macros.get("carbs_g", "-")

        msg = (
            f"✅ **Целевая калорийность зафиксирована!**\n\n"
            f"• Суточная норма: **{t_kcal} ккал/сут** (дефицит {def_k} ккал)\n"
            f"• Веха ({t_kg} кг): согласованный срок **{dl}**\n"
            f"• БЖУ: 🥩 **{p} г** | 🥑 **{f} г** | 🍞 **{c} г**\n\n"
            f"Дневная норма в журнале питания синхронизирована с графиком вехи."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Изменить калораж", callback_data="target_back")],
            [
                InlineKeyboardButton(text="📊 Прогноз плато", callback_data="nav_plateau"),
                InlineKeyboardButton(text="📈 Траектория", callback_data="nav_forecast"),
            ],
        ])
        return msg, kb
    finally:
        conn.close()


def _format_plateau_interactive(uid: str, args: str = "") -> tuple[str, InlineKeyboardMarkup | None]:
    from health_core import forecast as _fc
    conn = connect()
    try:
        user_id = _resolve_uid_to_user_id(conn, uid)
        if user_id is None:
            return _NOT_REGISTERED, None
        res = _fc.plateau_forecast(conn, user_id)
        if "error" in res:
            return f"ℹ️ {res['error']}", None

        stagnation = res.get("current_stagnation") or {}
        pharma = res.get("pharmacological_plateau") or {}
        eq = res.get("metabolic_equilibrium") or {}
        recs = res.get("recommendations") or []

        st_verdict = stagnation.get("verdict", "нормальная динамика")
        st_details = stagnation.get("details", "")
        spread = stagnation.get("weight_spread_10d_kg")
        waist = stagnation.get("waist_change_21d_cm")

        ph_phase = pharma.get("phase", "")
        weeks_on = pharma.get("weeks_on_program", 0)
        med_weeks = pharma.get("median_plateau_weeks", 0)
        weeks_rem = pharma.get("weeks_remaining", 0)
        exp_date = pharma.get("expected_date", "")
        ph_desc = pharma.get("description", "")

        intake = eq.get("intake_kcal", 0)
        w_eq = eq.get("equilibrium_weight_kg", 0)
        slow_date = eq.get("slowdown_date")
        slow_w = eq.get("slowdown_weight_kg")

        lines = [
            "⏳ **Прогноз и клинический анализ плато массы тела**\n",
            "📊 **Текущий статус (динамика за 2–3 недели):**",
            f"• Статус: **{st_verdict}**",
            f"• {st_details}",
        ]
        if spread is not None:
            lines.append(f"• Колебания веса за 10 дней: **{spread} кг**")
        if waist is not None:
            lines.append(f"• Изменение талии за 3 недели: **{waist:+.1f} см**")
        lines.append("")

        lines.extend([
            "💊 **Фармакологическое плато (SURMOUNT timeline):**",
            f"• Фаза: **{ph_phase}**",
            f"• Неделя программы: **{weeks_on:.0f} из {med_weeks:.0f} нед** (осталось ~{weeks_rem:.0f} нед)",
            f"• Ожидаемая дата плато на текущей дозе: **{exp_date}**",
            f"• {ph_desc}\n",
        ])

        lines.extend([
            "⚖️ **Метаболическое равновесие (модель Hall):**",
            f"• Приход: **{intake} ккал/сут**",
            f"• Равновесная масса W_eq: **{w_eq} кг** *(вес, на котором расход сравняется с приходом)*",
        ])
        if slow_date and slow_w:
            lines.append(f"• Замедление потери (<100 г/нед): ориентировочно **{slow_date}** (при весе **{slow_w} кг**)")
        lines.append("")

        if recs:
            lines.append("💡 **Клинические рекомендации:**")
            for r in recs:
                lines.append(f"• {r}")

        buttons = [
            [InlineKeyboardButton(text="🍽 Назначить рефид (4 дня)", callback_data="plateau_refeed")],
            [InlineKeyboardButton(text="🩺 Созвать консилиум (/council)", callback_data="plateau_council")],
            [
                InlineKeyboardButton(text="🎯 Подобрать калораж", callback_data="nav_target"),
                InlineKeyboardButton(text="📈 Траектория веса", callback_data="nav_forecast"),
            ],
        ]
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)
    finally:
        conn.close()


def _format_forecast_interactive(uid: str, args: str = "") -> tuple[str, InlineKeyboardMarkup | None]:
    from health_core import forecast as _fc
    conn = connect()
    try:
        user_id = _resolve_uid_to_user_id(conn, uid)
        if user_id is None:
            return _NOT_REGISTERED, None
        intake = None
        args = args.strip()
        if args and args.replace(".", "", 1).isdigit():
            intake = float(args)

        proj = _fc.project(conn, user_id, 84, intake_kcal=intake)
        if "error" in proj and "меньше 7 дней" in proj.get("error", ""):
            today = config.user_now(conn, user_id).date()
            t_row = conn.execute(
                "SELECT kcal_target FROM daily_targets WHERE user_id=? AND date=?",
                (user_id, today.isoformat()),
            ).fetchone()
            if t_row and t_row["kcal_target"]:
                fallback_intake = float(t_row["kcal_target"])
            else:
                from health_core.energy import daily_expenditure, kcal_floor
                exp_dict = daily_expenditure(conn, user_id, today.isoformat())
                floor, _ = kcal_floor(conn, user_id, exp_dict["kcal"])
                fallback_intake = max(floor, exp_dict["kcal"] - 500.0)
            proj = _fc.project(conn, user_id, 84, intake_kcal=fallback_intake)

        if "error" in proj:
            return f"ℹ️ {proj['error']}", None

        start_w = proj.get("start_weight_kg")
        intake = proj.get("intake_kcal")
        tdee = proj.get("measured_tdee")
        traj = proj.get("trajectory") or []

        def _get_point(days: int) -> dict | None:
            if len(traj) > days:
                return traj[days]
            return traj[-1] if traj else None

        p_4w = _get_point(28)
        p_8w = _get_point(56)
        p_12w = _get_point(84)

        lines = [
            "📈 **Прогноз динамики массы тела (модель Hall)**\n",
            f"Исходный вес: **{start_w} кг** | Приход: **{intake} ккал/сут** | TDEE: **{tdee} ккал**\n",
            "📅 **Динамика по неделям (оценка с доверительным коридором):**",
        ]
        if p_4w:
            lines.append(f"• Через 4 нед ({p_4w['date']}): **{p_4w['mid']} кг** ({p_4w['lo']}–{p_4w['hi']} кг)")
        if p_8w:
            lines.append(f"• Через 8 нед ({p_8w['date']}): **{p_8w['mid']} кг** ({p_8w['lo']}–{p_8w['hi']} кг)")
        if p_12w:
            lines.append(f"• Через 12 нед ({p_12w['date']}): **{p_12w['mid']} кг** ({p_12w['lo']}–{p_12w['hi']} кг)")
        lines.append("")

        from health_core.energy import _active_milestone
        active_ms = _active_milestone(conn, user_id)
        if active_ms and active_ms["threshold"]:
            t_kg = float(active_ms["threshold"])
            reach_res = _fc.reach(conn, user_id, t_kg, intake_kcal=intake)
            ms_name = active_ms["name"]
            ms_dl = active_ms["deadline"] if active_ms["deadline"] else "не задан"
            lines.append("🎯 **Активная веха:**")
            lines.append(f"• «{ms_name}»: цель **{t_kg} кг** (дедлайн: {ms_dl})")
            if reach_res.get("reached"):
                lines.append(f"• Ожидаемая дата достижения: **{reach_res.get('expected')}** (коридор {reach_res.get('earliest')}–{reach_res.get('latest')})")
            else:
                lines.append("• Достижение за пределами 6-месячного горизонта при текущем приходе.")
        else:
            lines.append("🎯 **Активная веха:** не установлена. Задайте через /target.")

        lines.append("\n*Модель учитывает метаболическую адаптацию и замедление снижения массы.*")

        buttons = [
            [
                InlineKeyboardButton(text="🎯 Подобрать калораж", callback_data="nav_target"),
                InlineKeyboardButton(text="⏳ Прогноз плато", callback_data="nav_plateau"),
            ],
        ]
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)
    finally:
        conn.close()


async def _notify_admins_of_request(bot: Bot, uid: str, from_user) -> None:
    text = (
        f"🆕 Заявка на доступ: tg {uid} {_display_name(from_user)}. "
        f"Одобрить: /approve {uid} · Отклонить: /deny {uid}"
    )
    for admin_id in admin_user_ids():
        try:
            await bot.send_message(int(admin_id), text, parse_mode=None)
        except Exception:
            log.exception("не удалось уведомить администратора %s о заявке %s", admin_id, uid)


# run_command исполняется в потоке (asyncio.to_thread) и не видит объект Bot,
# а /approve обязан не только ответить админу, но и написать одобренному
# человеку. Вместо протаскивания Bot через сигнатуру run_command (её же зовут
# тесты напрямую, синхронно) — очередь уведомлений, которую _handle_turn
# опустошает сразу после run_command, там, где Bot уже под рукой.
_PENDING_USER_NOTIFICATIONS: list[tuple[str, str]] = []


async def _flush_pending_notifications(bot: Bot) -> None:
    while _PENDING_USER_NOTIFICATIONS:
        target, text = _PENDING_USER_NOTIFICATIONS.pop(0)
        try:
            await bot.send_message(int(target), text)
        except Exception:
            log.exception("не удалось уведомить пользователя %s", target)


def _set_user_tz(conn, user_id: int) -> None:
    # reserve()/execute() пишут started_at/finished_at через local_now(), а он зависит от ContextVar пояса
    row = conn.execute("SELECT timezone FROM users WHERE id=?", (user_id,)).fetchone()
    config.set_tz(row["timezone"] if row else None)


async def _run_council_task(bot: Bot, telegram_uid: str, user_id: int, run_id: int, reason: str) -> None:
    """Сама работа консилиума (минуты) — отдельной asyncio-задачей, не держит
    ход разговора. Своё соединение с БД: то, что открыл тул-хендлер, уже
    закрыто registry.release_connections к моменту, когда эта задача стартует."""
    conn = connect()
    try:
        _set_user_tz(conn, user_id)
        result = await council.execute(conn, user_id, run_id, reason)
    except Exception:
        log.exception("консилиум упал целиком, run_id=%s", run_id)
        try:
            council._finish(conn, run_id, "failed", 0, "Консилиум не состоялся: внутренняя ошибка.")
        except Exception:
            log.exception("не удалось пометить run_id=%s как failed", run_id)
        try:
            await bot.send_message(int(telegram_uid), "Консилиум не состоялся: внутренняя ошибка. Попробуйте позже.",
                                   parse_mode=None)
        except Exception:
            log.exception("не удалось сообщить пользователю %s о сбое консилиума", telegram_uid)
        return
    finally:
        conn.close()
    for chunk in _chunks(result["text"], TELEGRAM_LIMIT):
        try:
            try:
                await bot.send_message(int(telegram_uid), chunk)
            except TelegramBadRequest:
                await bot.send_message(int(telegram_uid), chunk, parse_mode=None)
        except Exception:
            log.exception("не удалось отправить итог консилиума пользователю %s", telegram_uid)
            return


_COUNCIL_TASKS: set = set()


async def _flush_pending_council(bot: Bot) -> None:
    """Заявки, которые council.reserve() уже зарезервировал синхронно (тул
    в plugin/tools.py) — запускаем фоновой asyncio-задачей в цикле бота, а не
    ждём здесь: ответ модели («консилиум запущен») уже ушёл или вот-вот уйдёт,
    и обработку следующих сообщений это держать не должно."""
    while council._PENDING_RUNS:
        telegram_uid, user_id, run_id, reason = council._PENDING_RUNS.pop(0)
        task = asyncio.create_task(_run_council_task(bot, telegram_uid, user_id, run_id, reason))
        _COUNCIL_TASKS.add(task)  # без сильной ссылки цикл может собрать задачу посреди работы
        task.add_done_callback(_COUNCIL_TASKS.discard)


def _require_admin(uid: str, cmd: str) -> str | None:
    if uid not in admin_user_ids():
        return f"⚠ Команда /{cmd} доступна только администраторам."
    return None


def _parse_tg_id(args: str, cmd: str) -> tuple[str | None, str | None]:
    tg_id = args.strip()
    if not tg_id.isdigit():
        return None, f"Использование: /{cmd} <tg_id>"
    return tg_id, None


def _access_upsert_status(conn, tg_id: str, status: str) -> None:
    now = _now_iso()
    cur = conn.execute(
        "UPDATE access_list SET status=?, decided_at=? WHERE telegram_user_id=?",
        (status, now, tg_id),
    )
    if cur.rowcount == 0:
        conn.execute(
            "INSERT INTO access_list(telegram_user_id, status, requested_at, decided_at) "
            "VALUES (?, ?, ?, ?)",
            (tg_id, status, now, now),
        )
    conn.commit()


def _cmd_approve(uid: str, args: str) -> str:
    err = _require_admin(uid, "approve")
    if err:
        return err
    tg_id, err = _parse_tg_id(args, "approve")
    if err:
        return err
    conn = connect()
    try:
        _access_upsert_status(conn, tg_id, "approved")
        # Строка users нужна ДО первого сообщения человека: _get_user_id в
        # plugin/tools.py бросает для незнакомого telegram id, а без неё
        # первый же вызов инструмента моделью падает.
        conn.execute(
            "INSERT OR IGNORE INTO users(telegram_user_id, created_at) VALUES (?, ?)",
            (tg_id, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    _PENDING_USER_NOTIFICATIONS.append((
        tg_id,
        "Доступ открыт. Напиши о себе: рост, дата рождения, пол, город или "
        "часовой пояс и стартовый вес — настрою цели.",
    ))
    return f"✅ Доступ для {tg_id} открыт."


def _cmd_deny(uid: str, args: str) -> str:
    err = _require_admin(uid, "deny")
    if err:
        return err
    tg_id, err = _parse_tg_id(args, "deny")
    if err:
        return err
    conn = connect()
    try:
        _access_upsert_status(conn, tg_id, "denied")
    finally:
        conn.close()
    return f"⛔ Доступ для {tg_id} отклонён."


def _cmd_revoke(uid: str, args: str) -> str:
    err = _require_admin(uid, "revoke")
    if err:
        return err
    tg_id, err = _parse_tg_id(args, "revoke")
    if err:
        return err
    if tg_id in admin_user_ids():
        return "⚠ Нельзя отозвать доступ администратору."
    conn = connect()
    try:
        _access_upsert_status(conn, tg_id, "denied")
    finally:
        conn.close()
    return f"⛔ Доступ для {tg_id} отозван. Данные человека остаются в базе."


def _cmd_access_list(uid: str) -> str:
    err = _require_admin(uid, "access")
    if err:
        return err
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT telegram_user_id, status, username, requested_at, decided_at "
            "FROM access_list ORDER BY requested_at"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return "Список доступа пуст."
    by_status: dict[str, list] = {"pending": [], "approved": [], "denied": []}
    for r in rows:
        by_status.setdefault(r["status"], []).append(r)
    labels = {"pending": "⏳ Ожидают", "approved": "✅ Одобрены", "denied": "⛔ Отклонены"}
    lines = ["📋 **Доступ:**"]
    for status in ("pending", "approved", "denied"):
        entries = by_status.get(status, [])
        lines.append(f"\n{labels[status]} ({len(entries)}):")
        if not entries:
            lines.append("  —")
        for r in entries:
            uname = r["username"] or "—"
            when = r["decided_at"] or r["requested_at"] or "—"
            lines.append(f"  `{r['telegram_user_id']}` {uname} — {when}")
    return "\n".join(lines)


def run_command(uid: str, text: str) -> str | None:
    """Слэш-команды исполняются без модели — в этом их смысл. None означает
    «не наша команда»: сообщение уходит дальше в обычный разбор."""
    name, _, args = text[1:].partition(" ")
    name = name.split("@")[0].lower()          # /help@botname в группах

    if name == "new":
        conn = connect()
        try:
            history.clear(conn, uid)
        finally:
            conn.close()
        return "Контекст забыт. Записи в базе на месте."
    if name == "status":
        return _plain(registry.dispatch("get_status_bar", {}))
    if name == "help":
        return _plain(registry.dispatch("help", {}))
    if name == "checklist":
        conn = connect()
        try:
            user_id = _resolve_uid_to_user_id(conn, uid)
            if user_id is None:
                return _NOT_REGISTERED
            _set_user_tz(conn, user_id)
            return morning_checklist(conn, user_id)
        finally:
            conn.close()
    if name in ("model", "models"):
        if uid not in admin_user_ids():
            return "⚠ Команда /model доступна только администраторам."
        return switch_model_cmd(args.strip())
    if name == "approve":
        return _cmd_approve(uid, args)
    if name == "deny":
        return _cmd_deny(uid, args)
    if name == "revoke":
        return _cmd_revoke(uid, args)
    if name == "access":
        return _cmd_access_list(uid)
    if name in ("target", "calibrate"):
        msg, _ = _format_target_interactive(uid, args)
        return msg
    if name == "plateau":
        msg, _ = _format_plateau_interactive(uid, args)
        return msg
    if name == "forecast":
        msg, _ = _format_forecast_interactive(uid, args)
        return msg

    for cmd_name, handler, _desc in registry.SLASH_COMMANDS:
        if cmd_name == name:
            try:
                return _plain(handler(args.strip()))
            finally:
                # Хендлеры слэш-команд текут соединениями ровно так же, как
                # хендлеры инструментов, — и мимо registry.dispatch() уборка
                # не срабатывает. См. registry.release_connections.
                registry.release_connections()
    return None


def _user_time_context(conn, uid: str) -> str:
    user_row = conn.execute(
        "SELECT id, timezone, health_notes FROM users WHERE telegram_user_id=?", (uid,)
    ).fetchone()
    health_notes = ""
    if user_row:
        config.set_tz(user_row["timezone"])
        u_now = config.user_now(conn, user_row["id"])
        tz_info = f" ({user_row['timezone']})" if user_row["timezone"] else ""
        health_notes = (user_row["health_notes"] or "").strip()
    else:
        u_now = config.local_now()
        tz_info = ""
    ctx = (
        f"\n\n**[CURRENT TIME & DATE]**\n"
        f"Текущая дата и время пользователя: {u_now.strftime('%Y-%m-%d %H:%M')}{tz_info}.\n"
        f"Сегодня: {u_now.strftime('%d.%m.%Y')} (ТЕКУЩИЙ ГОД: {u_now.year}).\n"
        f"ВАЖНО: Текущий год — {u_now.year}! Ни в коем случае не используй 2025 или 2024 при логировании еды или запросах за сегодня."
    )
    if health_notes:
        # Персональные ограничения по здоровью (MU-2): раньше жили общим блоком
        # в system_promt.md под пометкой user_id=1 и применялись ко всем —
        # теперь приходят в контексте именно этого человека.
        ctx += f"\n\nОграничения по здоровью: {health_notes}"
    return ctx


def _open_turn(uid: str, text: str) -> list[dict]:
    """Собрать запрос и сразу записать реплику человека. Синхронная работа с
    sqlite и чтение персоны с диска — блокирующие, поэтому вызывается через
    asyncio.to_thread: иначе один медленный диск останавливает поллинг и
    вместе с ним переписку всех остальных."""
    conn = connect()
    try:
        time_ctx = _user_time_context(conn, uid)
        prefix = [{"role": "system", "content": system_prompt() + time_ctx}]
        prefix += history.load(conn, uid)
        prefix.append({"role": "user", "content": text})
        history.append(conn, uid, {"role": "user", "content": text})
        return prefix
    finally:
        conn.close()


def _open_turn_vision(uid: str, content: list[dict], text_for_history: str) -> list[dict]:
    """Как _open_turn, но для vision-запроса с картинкой. В историю пишем
    только текстовую часть — base64 занимает мегабайты и убивает окно."""
    conn = connect()
    try:
        time_ctx = _user_time_context(conn, uid)
        prefix = [{"role": "system", "content": system_prompt() + time_ctx}]
        prefix += history.load(conn, uid)
        prefix.append({"role": "user", "content": content})
        # В историю — только текст, не base64: он занял бы всё окно целиком
        history.append(conn, uid, {"role": "user", "content": text_for_history})
        return prefix
    finally:
        conn.close()


def _close_turn(uid: str, new_messages: list[dict]) -> None:
    """Дописать в историю то, что цикл добавил после реплики человека."""
    conn = connect()
    try:
        for m in new_messages:
            if m.get("role") == "tool" and isinstance(m.get("content"), str):
                m = {**m, "content": strip_tool_rules(m["content"])}
            elif m.get("role") == "assistant" and isinstance(m.get("content"), str):
                m = {**m, "content": strip_panels(m["content"])}
            history.append(conn, uid, m)
    finally:
        conn.close()


_COMPACT_PROMPT = (
    "Сожми историю диалога health-бота в краткую сводку на русском (до 1200 символов). "
    "Оставь только то, что нужно для продолжения разговора: договорённости, предпочтения, "
    "незакрытые вопросы, контекст текущей темы. Цифры замеров и записи не пересказывай - "
    "они в базе. Только текст сводки, без вступлений."
)


async def _compact_history(session: aiohttp.ClientSession, uid: str, providers: list) -> str:
    """Старые сообщения (за пределами окна) -> одна сводка от модели. Свежие остаются как есть."""
    def _load():
        conn = connect()
        try:
            return history.split(conn, uid)
        finally:
            conn.close()

    old = (await asyncio.to_thread(_load))[0]
    if not old:
        return "Сжимать нечего: вся история в окне."
    msg = await llm.chat(session, [{"role": "system", "content": _COMPACT_PROMPT},
                                   {"role": "user", "content": history.render(old)}], [], providers)
    summary = (msg.get("content") or "").strip()
    if not summary:
        raise RuntimeError("модель вернула пустую сводку")

    def _save():
        conn = connect()
        try:
            history.replace(conn, uid, old[-1]["rowid"], summary)
        finally:
            conn.close()

    await asyncio.to_thread(_save)
    return f"История сжата: {len(old)} старых сообщений -> сводка. Записи в базе на месте."


def _user_lock(uid: str) -> asyncio.Lock:
    """Один замок на человека. Без него два его же сообщения подряд
    перемешивают записи в chat_history: реплика второго хода вклинивается
    между assistant с tool_calls и его результатами, и следующий запрос
    провайдер отвергает целиком как невалидный. Замки разных людей
    независимы — очередь одного не тормозит другого."""
    lock = _USER_LOCKS.get(uid)
    if lock is None:
        lock = _USER_LOCKS[uid] = asyncio.Lock()
    return lock


async def _handle_document(message: Message, uid: str) -> None:
    doc = message.document
    if not doc:
        return
    filename = Path(doc.file_name or "uploaded_file").name or "uploaded_file"
    log.info("получен файл %s (%d байт) от user %s", filename, doc.file_size or 0, uid)
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / filename
        await message.bot.download(doc, destination=tmp_path)
        
        # Если zip-архив (например, из Mi Fitness или архив с tcx/csv)
        if filename.lower().endswith(".zip") or tmp_path.suffix.lower() == ".zip":
            import zipfile
            import migrate as _mig
            extract_dir = Path(tmp_dir) / "unzipped"
            extract_dir.mkdir(parents=True, exist_ok=True)

            def _import_zip() -> str | None:
                # Блокирующее (распаковка + sqlite) - в потоке, не на event loop.
                with zipfile.ZipFile(tmp_path, "r") as zf:
                    zf.extractall(extract_dir)
                conn = connect()
                try:
                    user_id = _resolve_uid_to_user_id(conn, uid)
                    if user_id is None:
                        return None
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        _mig.run_scale(conn, user_id, str(extract_dir))
                        _mig.run_tcx(conn, user_id, str(extract_dir))
                    conn.commit()
                    return buf.getvalue().strip() or "Файлы из zip обработаны."
                finally:
                    conn.close()

            out = await asyncio.to_thread(_import_zip)
            if out is None:
                await message.answer(_NOT_REGISTERED, parse_mode=None)
            else:
                await message.answer(f"Разобран zip-архив {filename}:\n\n{out}", parse_mode=None)
            return

        # Иначе пробуем стандартный импорт файла (TCX, XLSX, CSV)
        registry.set_caller(uid)
        try:
            raw_res = await asyncio.to_thread(
                registry.dispatch, "import_scale_export", {"file_path": str(tmp_path)}
            )
            data = json.loads(raw_res)
            if "error" in data:
                await message.answer(f"❌ Ошибка импорта: {data['error']}")
            else:
                added = data.get("added", 0)
                skipped = data.get("skipped", 0)
                act = data.get("activity")
                if added > 0:
                    details_str = ""
                    if act and isinstance(act, dict):
                        sport = act.get("sport") or "Тренировка"
                        parts = []
                        if act.get("duration_min"):
                            parts.append(f"{act['duration_min']:.0f} мин")
                        if act.get("kcal"):
                            parts.append(f"{act['kcal']:.0f} ккал")
                        if act.get("avg_hr"):
                            parts.append(f"пульс {act['avg_hr']} уд/мин")
                        summary_act = ", ".join(parts)
                        details_str = f"\n\n🏃 **{sport}**: {summary_act}\nКалории учтены в дневном расходе (TDEE)."
                    await message.answer(
                        f"✅ Файл `{filename}` успешно импортирован! (добавлено: {added}, пропущено дублей: {skipped}){details_str}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                elif skipped > 0:
                    await message.answer(f"ℹ️ Данные из файла `{filename}` уже есть в базе (пропущено существующих записей: {skipped}).", parse_mode=ParseMode.MARKDOWN)
                else:
                    await message.answer(f"ℹ️ В файле `{filename}` новых данных не найдено.", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            log.exception("ошибка при импорте файла")
            await message.answer(f"❌ Не удалось обработать файл: {e}")


# Присланный чек = покупка состоялась: запас пополняется сам, без вопроса
_RECEIPT_RULE = (
    "Чек из магазина: человек это купил, поэтому СРАЗУ, без вопросов, добавь каждую пищевую позицию чека в запас: "
    "pantry(action='add', name=<название без артикулов и скидок>, qty=<количество>, unit=<г/кг/мл/л/шт/банка>, category=<категория>). "
    "Вес и объём бери из названия позиции (код сам переведёт «ст.250мл», «185 г»). Если инструмент ответил need_weight - "
    "позицию пока не добавляй, добавь остальные, а в конце ОДНИМ сообщением спроси вес одной штуки/банки у всех таких позиций списком; "
    "ответил - add с piece_weight_g, не знает или не хочет - add с no_weight=true (останется в штуках). "
    "Не еду (пакеты, бытовое, хозтовары) пропусти. В ответе перечисли, что добавлено."
)


async def _handle_photo(message: Message, session: aiohttp.ClientSession, cfg: dict, uid: str) -> None:
    """Скачивает фото, кодирует в base64 и отправляет модели как vision-запрос.
    Сценарии: фото еды для оценки КБЖУ, фото глюкометра, фото этикетки."""
    # Telegram sends multiple sizes; take the largest (last in array)
    photo = message.photo[-1]

    # Download photo to bytes
    bio = io.BytesIO()
    await message.bot.download(photo, destination=bio)
    bio.seek(0)
    image_bytes = bio.read()

    b64 = base64.b64encode(image_bytes).decode("ascii")

    # Build multimodal content for OpenAI vision format
    caption = (message.caption or "").strip()
    if caption:
        text_part = (
            f"{caption}\n\n"
            "Внимательно распознай изображение. Если на фото прибор или показатели (глюкометр, сон, часы/браслет, пульс, шаги, кислород, еда) — "
            "ОБЯЗАТЕЛЬНО извлеки точные цифры и вызови соответствующий инструмент для сохранения в базу "
            "(log_glucose, log_bp, log_sleep, log_watch_day, log_food). "
            + _RECEIPT_RULE
        )
    else:
        text_part = (
            "Что на этом фото? Внимательно распознай изображение и ОБЯЗАТЕЛЬНО вызови инструмент для сохранения данных:\n"
            "- Глюкометр: вызови log_glucose(action='add', mmol_l=<число на дисплее>, context='замер по фото глюкометра').\n"
            "- Скриншот сна (Xiaomi Band, часы, приложение): вызови log_sleep(action='add', bedtime=..., wake_time=..., "
            "duration_min=..., deep_min=..., rem_min=..., awake_min=..., spo2_avg=..., source='скриншот часов').\n"
            "- Экран активности/часов (пульс, шаги, калории активности, стресс, SpO2/кислород): вызови log_watch_day(action='add', ...).\n"
            "- Тонометр (давление): вызови log_bp(action='add', systolic=<верхнее>, diastolic=<нижнее>, pulse=<пульс, если виден>).\n"
            "- Еда: оцени КБЖУ, назови оценку и вызови log_food или предложи запись.\n"
            f"- {_RECEIPT_RULE}\n"
            "- Этикетка/состав: разбери состав и КБЖУ на 100 г."
        )

    content = [
        {"type": "text", "text": text_part},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]

    # Use _open_turn_vision with special image content
    registry.set_caller(uid)
    _PLAN_ONLY.set(False)
    prefix = await asyncio.to_thread(_open_turn_vision, uid, content, caption or "[фото]")

    load_env_file()
    current_cfg = load_config()
    bot_cfg = current_cfg.get("bot", cfg.get("bot", {}))
    try:
        answer, full = await llm.run_loop(
            session, prefix, tool_specs(), bot_cfg["providers"],
            dispatch, max_iters=bot_cfg.get("max_tool_iters", 6),
            timeout_s=bot_cfg.get("request_timeout_s", llm.DEFAULT_TIMEOUT_S),
        )
    except RuntimeError as e:
        log.error("все провайдеры недоступны: %s", e)
        await message.answer("Не смог связаться с моделью. Повтори через минуту.")
        return
    finally:
        await _flush_pending_council(message.bot)  # см. _handle_turn

    await asyncio.to_thread(_close_turn, uid, full[len(prefix):])
    await _flush_pending_notifications(message.bot)
    await send_long(message, answer)


async def handle_message(message: Message, session: aiohttp.ClientSession, cfg: dict) -> None:
    uid = str(message.from_user.id)
    if uid not in admin_user_ids():
        status = await asyncio.to_thread(_check_access, uid, message.from_user)
        if status == "new_pending":
            await message.answer("Заявка на доступ отправлена администратору. Ответ придёт сюда.")
            await _notify_admins_of_request(message.bot, uid, message.from_user)
            return
        if status != "approved":
            # pending/denied — молчим: повторный ответ учит слать сообщения снова.
            log.warning("нет доступа у telegram id %s (%s)", uid, status)
            return

    if message.document:
        async with _user_lock(uid):
            async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
                await _handle_document(message, uid)
        return

    if message.photo:
        async with _user_lock(uid):
            async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
                await _handle_photo(message, session, cfg, uid)
        return

    text = (message.text or message.caption or "").strip()
    if not text:
        return

    async with _user_lock(uid):
        # «печатает…» на всё время хода. Телеграм гасит статус через 5 секунд,
        # ChatActionSender переотправляет его сам, пока блок не закончится.
        # Ответ идёт 5-20 секунд, и без этого статуса бот неотличим от мёртвого —
        # именно так это и выглядело после ухода Hermes, который статус слал.
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            await _handle_turn(message, session, cfg, uid, text)


async def _handle_turn(message: Message, session: aiohttp.ClientSession,
                       cfg: dict, uid: str, text: str) -> None:
    # Личность звонящего выставляем до любого исполнения: без неё инструменты
    # отработают над user_id=1, кто бы ни прислал сообщение.
    registry.set_caller(uid)
    _PLAN_ONLY.set(is_plan_only(text))

    if text.startswith("/"):
        cmd_name = text[1:].partition(" ")[0].split("@")[0].lower()
        if cmd_name in ("model", "models"):
            if uid not in admin_user_ids():
                await message.answer("⚠ Команда /model доступна только администраторам.")
                return
            cmd_args = text[1:].partition(" ")[2].strip()
            if not cmd_args:
                current_cfg = load_config()
                providers = list((current_cfg.get("bot") or {}).get("providers", []))
                kb = _model_keyboard(providers)
                msg_text = _format_models_message(providers)
                await _answer_md(message, msg_text, kb)
                return

        if cmd_name in ("target", "calibrate"):
            cmd_args = text[1:].partition(" ")[2].strip()
            msg_text, kb = await asyncio.to_thread(_format_target_interactive, uid, cmd_args)
            await _answer_md(message, msg_text, kb)
            return

        if cmd_name == "balance":
            if uid not in admin_user_ids():
                await message.answer("⚠ Команда /balance доступна только администраторам.")
                return
            current_cfg = load_config()
            providers = list((current_cfg.get("bot") or {}).get("providers", []))
            
            await message.answer("🔄 Проверяю статус провайдеров...")
            res = await llm.check_balances(session, providers)
            await message.answer(f"📊 Статус моделей:\n{res}")
            return

        if cmd_name == "plateau":
            cmd_args = text[1:].partition(" ")[2].strip()
            msg_text, kb = await asyncio.to_thread(_format_plateau_interactive, uid, cmd_args)
            await _answer_md(message, msg_text, kb)
            return

        if cmd_name == "forecast":
            cmd_args = text[1:].partition(" ")[2].strip()
            msg_text, kb = await asyncio.to_thread(_format_forecast_interactive, uid, cmd_args)
            await _answer_md(message, msg_text, kb)
            return

        if cmd_name == "compact":
            providers = list((load_config().get("bot") or {}).get("providers", []))
            try:
                await message.answer(await _compact_history(session, uid, providers))
            except RuntimeError as e:
                log.error("compact не удался: %s", e)
                await message.answer("Не смог сжать историю: модель недоступна. Попробуй позже.")
            return

        # Хендлеры команд синхронные и лезут в sqlite — в поток, чтобы не
        # блокировать polling. to_thread копирует контекст, ContextVar доедет.
        answer = await asyncio.to_thread(run_command, uid, text)
        await _flush_pending_notifications(message.bot)
        if answer is not None:
            await send_long(message, answer)
            return

    rewritten = registry.quick_macro(text)
    if rewritten:
        text = rewritten

    load_env_file()
    current_cfg = load_config()
    bot_cfg = current_cfg.get("bot", cfg.get("bot", {}))
    # Автосжатие: истории накопилось больше compact_rows записей - старое в сводку
    # до хода, чтобы не раздувать хвост. Сбой не блокирует ход.
    def _rows() -> int:
        conn = connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM chat_history WHERE telegram_user_id=?", (uid,)).fetchone()[0]
        finally:
            conn.close()
    if await asyncio.to_thread(_rows) > bot_cfg.get("compact_rows", 80):
        try:
            await _compact_history(session, uid, bot_cfg["providers"])
        except RuntimeError as e:
            log.warning("автосжатие истории не удалось: %s", e)

    prefix = await asyncio.to_thread(_open_turn, uid, text)
    try:
        answer, full = await llm.run_loop(
            session, prefix, tool_specs(), bot_cfg["providers"],
            dispatch, max_iters=bot_cfg.get("max_tool_iters", 6),
            timeout_s=bot_cfg.get("request_timeout_s", llm.DEFAULT_TIMEOUT_S),
        )
    except RuntimeError as e:
        log.error("все провайдеры недоступны: %s", e)
        await message.answer("Не смог связаться с моделью. Записи в базе не потеряны, повтори через минуту.")
        return
    finally:
        # Инструмент council мог зарезервировать фоновый прогон (plugin/tools.py::
        # handle_council) — запускаем его задачей цикла бота, не блокируя обработку
        # следующих сообщений. В finally: если run_loop упал ПОСЛЕ резерва, заявка
        # иначе зависла бы в очереди, а run — в running до часового сброса.
        await _flush_pending_council(message.bot)

    await asyncio.to_thread(_close_turn, uid, full[len(prefix):])
    # drug_card_draft save (plugin/tools.py) ставит уведомление админам в ту же
    # очередь, что заявки на доступ (_notify_admins_of_request) — здесь, а не
    # только после run_command, потому что модель зовёт этот тул из обычного
    # хода диалога, не только из слэш-команды.
    await _flush_pending_notifications(message.bot)
    await send_long(message, answer)


def _admin_port_busy(host: str, port: int) -> bool:
    """Кто-то уже слушает этот порт (панель подняли руками через start-admin.sh)?
    Без проверки дочерний процесс просто упал бы с EADDRINUSE в лог бота."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _start_admin_panel() -> subprocess.Popen | None:
    """Поднимает админку вместе с ботом. Возвращает процесс или None, если
    запуск пропущен — пропуск НИКОГДА не валит бота: панель это удобство, а
    телеграм — работа.

    Отдельным процессом, а не потоком в боте, хотя поток был бы короче:
    bot/registry.py подменяет plugin.tools.connect трекером соединений, а
    админка дёргает те же хендлеры (_admin_set, handle_set_milestone). В общем
    процессе её HTTP-потоки попали бы под чужую заплатку, которая рассчитана на
    цикл dispatch бота и в потоках панели никогда не вызывается. Отдельный
    процесс — ровно та конфигурация, что уже проверена (python -m admin.server).

    Выключается HEALTH_ADMIN_AUTOSTART=0 в ~/.hermes/.env.
    """
    if os.environ.get("HEALTH_ADMIN_AUTOSTART", "1") == "0":
        log.info("админка не запущена: HEALTH_ADMIN_AUTOSTART=0")
        return None
    if not (os.environ.get("ADMIN_PASSWORD_HASH") and os.environ.get("ADMIN_PASSWORD_SALT")):
        log.warning("админка не запущена: нет ADMIN_PASSWORD_HASH/SALT "
                    "(python -m admin.server --set-password)")
        return None

    # Один try на всё остальное, включая разбор порта: опечатка в
    # HEALTH_ADMIN_PORT — это ValueError, а непрошенный хост — gaierror, и
    # любое из них, вылетев наружу, убило бы бота из-за неработающего
    # удобства. Панель не имеет права ронять телеграм ничем.
    try:
        host = os.environ.get("HEALTH_ADMIN_HOST", "127.0.0.1")
        port = int(os.environ.get("HEALTH_ADMIN_PORT", "8765"))
        if _admin_port_busy(host, port):
            log.info("админка уже слушает %s:%d — второй экземпляр не поднимаю", host, port)
            return None
        proc = subprocess.Popen(
            [sys.executable, "-m", "admin.server", "--host", host, "--port", str(port)],
            cwd=str(ROOT),
        )
    except Exception:
        log.exception("админка не запустилась — бот продолжает работу без неё")
        return None
    log.info("админка запущена: http://%s:%d/ (pid %d)", host, port, proc.pid)
    return proc


def _stop_admin_panel(proc: subprocess.Popen | None) -> None:
    """Гасит панель на выходе бота. terminate, а не kill: у панели есть
    server_close(). Не дождались за 5 с — добиваем, иначе порт останется занят
    и следующий старт бота решит, что панель уже поднята."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.info("админка остановлена")


_instance_lock = None   # держим открытым до конца процесса: flock снимается при закрытии файла


def _acquire_instance_lock(token: str) -> bool:
    """Один поллер на токен. Второй getUpdates с тем же токеном делит апдейты с
    первым (TelegramConflictError), и оба пишут в БД. Файл зависит только от
    токена: dev-бот с другим токеном и БД рядом с боевым не мешает. В имя идёт
    хэш, не сам токен. Вызывать из main(), не при импорте: test_bot.py импортирует
    этот модуль. Дочерние процессы (админка) замок не наследуют: файл открыт с CLOEXEC."""
    global _instance_lock
    path = f"/tmp/health-agent-{hashlib.sha256(token.encode()).hexdigest()[:12]}.lock"
    try:
        import fcntl   # только Unix; на Windows замка нет
    except ImportError:
        return True
    try:
        fh = open(path, "a")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        log.error("бот с этим токеном уже запущен или замок недоступен (%s: %s) - "
                  "второй поллер не стартует", path, e)
        return False
    _instance_lock = fh
    return True


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        # force: если кто-то из импортированных пакетов уже настроил logging,
        # обычный basicConfig молча становится пустышкой — и лог бота исчезает
        # целиком. Это уже случилось: бот работал, а файл лога был пуст, и
        # отладка «почему молчит» шла вслепую.
        force=True,
    )
    load_env_file()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error("TELEGRAM_BOT_TOKEN не задан (~/.hermes/.env)")
        return 1
    if not _acquire_instance_lock(token):
        return 1
    if not admin_user_ids():
        log.error("admin.telegram_admin_ids пуст — некому одобрять доступ. Заполните config.yaml")
        return 1
    conn = connect()
    try:
        migrate(conn)
        _bootstrap_env_allowlist(conn)
    finally:
        conn.close()

    cfg = load_config()
    # Проверяем раздел bot ДО первого сообщения. Иначе опечатка в config.yaml
    # всплывает KeyError внутри обработчика, человек видит "Внутренняя ошибка"
    # на каждое сообщение, а причина — только в логе.
    providers = (cfg.get("bot") or {}).get("providers")
    if not providers:
        log.error("в config.yaml нет bot.providers — некого спрашивать, см. Docs/llm_config.md")
        return 1
    for i, p in enumerate(providers):
        missing = [k for k in ("base_url", "api_key_env", "model") if not p.get(k)]
        if missing:
            log.error("bot.providers[%d]: не хватает полей %s", i, ", ".join(missing))
            return 1
        if not os.environ.get(p["api_key_env"]):
            # Не отказ: цепочка фолбэка на то и цепочка. Но молчать нельзя —
            # именно так «бот перестал отвечать» превращается в поиск вслепую.
            log.warning("bot.providers[%d] (%s): переменная %s пуста, провайдер будет пропущен",
                        i, p["model"], p["api_key_env"])

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp = Dispatcher()

    admin_proc = _start_admin_panel()
    try:
        return await _run_polling(bot, dp, cfg)
    finally:
        _stop_admin_panel(admin_proc)


async def _run_polling(bot: Bot, dp: Dispatcher, cfg: dict) -> int:
    async with aiohttp.ClientSession() as session:
        @dp.message()
        async def _on_message(message: Message) -> None:
            try:
                await handle_message(message, session, cfg)
            except Exception:
                # Падение одного сообщения не должно ронять polling: молчащий
                # бот выглядит поломкой, а перезапуск теряет очередь.
                log.exception("сообщение не обработано")
                await message.answer("Внутренняя ошибка, она записана в лог. Повтори иначе.")

        @dp.callback_query(lambda c: bool(c.data and c.data.startswith("switch_model:")))
        async def _on_switch_model(callback: CallbackQuery) -> None:
            cb_uid = str(callback.from_user.id)
            if cb_uid not in admin_user_ids():
                await callback.answer("⚠ Только для администраторов.", show_alert=True)
                return
            pid = (callback.data or "").split(":", 1)[1]

            current_cfg = load_config()
            providers = list((current_cfg.get("bot") or {}).get("providers", []))
            # список переставляется после каждого переключения: ищем по стабильному id, не по позиции
            idx = next((i for i, p in enumerate(providers) if _provider_id(p) == pid), -1)
            if idx < 0:
                await callback.answer("Модель не найдена, обновите список (/model).", show_alert=True)
                return

            if idx == 0:
                await callback.answer(f"Модель {providers[0].get('model')} уже активна.", show_alert=False)
                return

            from admin.server import _update_providers_in_config
            chosen = providers.pop(idx)
            new_providers = [chosen] + providers
            try:
                _update_providers_in_config(new_providers)
            except Exception as exc:
                await callback.answer(f"Ошибка: {exc}", show_alert=True)
                return

            short_name = chosen.get("model", "").split("/")[-1]
            await callback.answer(f"✅ Выбрана: {short_name}")
            new_kb = _model_keyboard(new_providers)
            new_text = _format_models_message(new_providers)
            try:
                if callback.message:
                    await callback.message.edit_text(new_text, reply_markup=new_kb, parse_mode=ParseMode.MARKDOWN)
            except TelegramBadRequest:
                try:
                    if callback.message:
                        await callback.message.edit_text(new_text, reply_markup=new_kb, parse_mode=None)
                except Exception:
                    pass
            except Exception:
                pass

        @dp.callback_query(lambda c: bool(c.data and c.data.startswith("target_opt:")))
        async def _on_target_opt(callback: CallbackQuery) -> None:
            if not await _cb_allowed(callback):
                return
            cb_uid = str(callback.from_user.id)
            opt_name = (callback.data or "").split(":", 1)[1]
            await callback.answer()
            msg_text, kb = await asyncio.to_thread(_format_target_option_preview, cb_uid, opt_name)
            try:
                if callback.message:
                    await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            except TelegramBadRequest:
                try:
                    if callback.message:
                        await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode=None)
                except Exception:
                    pass
            except Exception:
                pass

        @dp.callback_query(lambda c: bool(c.data and c.data.startswith("target_apply:")))
        async def _on_target_apply(callback: CallbackQuery) -> None:
            if not await _cb_allowed(callback):
                return
            cb_uid = str(callback.from_user.id)
            try:
                kcal_val = float((callback.data or "").split(":", 1)[1])
            except (IndexError, ValueError):
                await callback.answer("Неверное значение калорий.")
                return
            msg_text, kb = await asyncio.to_thread(_apply_target, cb_uid, kcal_val)
            if msg_text == _NOT_REGISTERED:
                await callback.answer(_NOT_REGISTERED, show_alert=True)
                return
            if kb is None:  # _apply_target вернул ошибку, ничего не записано
                await callback.answer(msg_text[:200], show_alert=True)
                return
            await callback.answer(f"✅ Цель {kcal_val:.0f} ккал зафиксирована!")
            try:
                if callback.message:
                    await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            except TelegramBadRequest:
                try:
                    if callback.message:
                        await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode=None)
                except Exception:
                    pass
            except Exception:
                pass

        @dp.callback_query(lambda c: bool(c.data in ("target_back", "nav_target")))
        async def _on_nav_target(callback: CallbackQuery) -> None:
            if not await _cb_allowed(callback):
                return
            cb_uid = str(callback.from_user.id)
            await callback.answer()
            msg_text, kb = await asyncio.to_thread(_format_target_interactive, cb_uid, "")
            try:
                if callback.message:
                    await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            except TelegramBadRequest:
                try:
                    if callback.message:
                        await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode=None)
                except Exception:
                    pass
            except Exception:
                pass

        @dp.callback_query(lambda c: bool(c.data == "nav_plateau"))
        async def _on_nav_plateau(callback: CallbackQuery) -> None:
            if not await _cb_allowed(callback):
                return
            cb_uid = str(callback.from_user.id)
            await callback.answer()
            msg_text, kb = await asyncio.to_thread(_format_plateau_interactive, cb_uid, "")
            try:
                if callback.message:
                    await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            except TelegramBadRequest:
                try:
                    if callback.message:
                        await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode=None)
                except Exception:
                    pass
            except Exception:
                pass

        @dp.callback_query(lambda c: bool(c.data == "nav_forecast"))
        async def _on_nav_forecast(callback: CallbackQuery) -> None:
            if not await _cb_allowed(callback):
                return
            cb_uid = str(callback.from_user.id)
            await callback.answer()
            msg_text, kb = await asyncio.to_thread(_format_forecast_interactive, cb_uid, "")
            try:
                if callback.message:
                    await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            except TelegramBadRequest:
                try:
                    if callback.message:
                        await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode=None)
                except Exception:
                    pass
            except Exception:
                pass

        @dp.callback_query(lambda c: bool(c.data == "plateau_refeed"))
        async def _on_plateau_refeed(callback: CallbackQuery) -> None:
            if not await _cb_allowed(callback):
                return
            cb_uid = str(callback.from_user.id)
            from health_core import refeed as _refeed
            conn = connect()
            try:
                user_id = _resolve_uid_to_user_id(conn, cb_uid)
                if user_id is None:
                    await callback.answer(_NOT_REGISTERED, show_alert=True)
                    return
                tomorrow = (config.user_now(conn, user_id).date() + __import__('datetime').timedelta(days=1))
                res = _refeed.schedule_once(conn, user_id, tomorrow, days=4, reason="plateau")
            finally:
                conn.close()
            await callback.answer("✅ Рефид запланирован!", show_alert=True)
            msg_text, kb = await asyncio.to_thread(_format_plateau_interactive, cb_uid, "")
            msg_text += f"\n\n🍽 **Рефид запланирован** ({res['days']} дней на уровне TDEE, причина: плато)."
            try:
                if callback.message:
                    await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            except TelegramBadRequest:
                try:
                    if callback.message:
                        await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode=None)
                except Exception:
                    pass
            except Exception:
                pass

        @dp.callback_query(lambda c: bool(c.data == "plateau_council"))
        async def _on_plateau_council(callback: CallbackQuery) -> None:
            if not await _cb_allowed(callback):
                return
            cb_uid = str(callback.from_user.id)
            conn = connect()
            try:
                user_id = _resolve_uid_to_user_id(conn, cb_uid)
                if user_id is None:
                    await callback.answer(_NOT_REGISTERED, show_alert=True)
                    return
                _set_user_tz(conn, user_id)
                try:
                    run_id = council.reserve(conn, user_id, "plateau")
                except ValueError as e:
                    await callback.answer(f"ℹ️ {e}", show_alert=True)
                    return
            finally:
                conn.close()

            task = asyncio.create_task(_run_council_task(callback.bot, cb_uid, user_id, run_id, "plateau"))
            _COUNCIL_TASKS.add(task)
            task.add_done_callback(_COUNCIL_TASKS.discard)
            await callback.answer("🩺 Консилиум запущен в фоне. Результат придёт в чат по готовности.", show_alert=True)

        log.info("бот запущен, разрешено пользователей: %d", len(allowed_users()))

        # Меню команд — косметика, и запуск на нём висеть не должен. Сеть тут
        # рвётся урывками: этот вызов молча съедал старт целиком, бот не доходил
        # ни до строки лога, ни до поллинга, и снаружи выглядел мёртвым при живом
        # процессе. Не вышло — работаем без меню, слэш-команды всё равно живы.
        # all_private_chats перекрывает default в личке: там годами висело меню
        # Hermes без /model, поэтому пишем обе области, а админам — своё меню.
        async def _set_menus():
            user_menu = menu_commands(admin=False)
            await bot.set_my_commands(user_menu)
            await bot.set_my_commands(user_menu, scope=BotCommandScopeAllPrivateChats())
            for admin_id in admin_user_ids():
                await bot.set_my_commands(menu_commands(admin=True),
                                          scope=BotCommandScopeChat(chat_id=int(admin_id)))

        try:
            await asyncio.wait_for(_set_menus(), timeout=20)
        except Exception as e:
            log.warning("меню команд не зарегистрировано (%s) — не мешает работе", type(e).__name__)
        await dp.start_polling(bot)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
