"""Thin wrappers over health_core — parameter unpacking and response serialization."""
import functools
import json
import os
import re
import shutil
import sqlite3
import tempfile
import textwrap
from datetime import datetime, date, timedelta
from pathlib import Path

from health_core.db import connect, migrate
from health_core.guards import check_all, record
from health_core.energy import daily_target, bmr_floor, bmr_katch, bmr_mifflin
from health_core.nutrition import day_macros, plate_balance
from health_core.report import (baseline_weight, status_bar, day_summary, trends, evening_report, whr, meals_of_day,
                               mark_achieved_milestones, weekly_summary, training_efficiency, weight_series, MINUS)
from health_core.ingest.scale import import_export
from health_core.ingest.tcx import import_tcx
from health_core.ingest.anthro import SITES as _ANTHRO_SITES
from health_core.plans import get_plan, set_meal_plan, set_workout_plan, plan_vs_actual
from health_core.export import is_remote_path, rclone_pull
from health_core.meds import canon as _canon_med, card as _med_card
from health_core import config


# Личность звонящего для слэш-команд. Шлюз разбирает слэш-команды в
# _handle_message и возвращает ответ РАНЬШЕ, чем _handle_message_with_agent
# выставит session-контекст (gateway/run.py: диспетчер ~17776, _set_session_env
# ~18979) — то есть внутри команды HERMES_SESSION_USER_ID пуст, а _get_user_id
# молча падает на user_id=1 и работает не над тем человеком. Хук
# pre_gateway_dispatch срабатывает на каждом входящем сообщении до диспетчера и
# несёт event.user_id (gateway/platforms/base.py:2313) — запоминаем его здесь.
# ContextVar, а не глобальная переменная: два человека пишут одновременно, и
# глобалка отдала бы одному личность другого. Внутри одной asyncio-задачи
# значение, выставленное в хуке, видно и в хендлере команды.
from contextvars import ContextVar

_CALLER_FALLBACK: ContextVar[str | None] = ContextVar("health_caller_fallback", default=None)


# Быстрый ярлык добора БЖУ/воды в конце дня: "+30 белка" вместо полного ввода
# блюда. Анкерённый regex — хук сидит на пути КАЖДОГО входящего сообщения (не
# только наших), не только Health-плагина, поэтому нежадный паттерн обязателен:
# должен совпасть текст целиком (^...$), иначе "2+2" или "съел 30 г белка"
# рискуют зацепиться за подстроку и молча испортить чужое сообщение.
_QUICK_MACRO_RE = re.compile(
    r"^\+\s*(\d+(?:[.,]\d+)?)\s*(белка|белок|б|углеводов|углеводы|у|жиров|жира|ж|мл|л|воды)\s*$",
    re.IGNORECASE,
)

_QUICK_MACRO_UNITS = {
    "белка": "protein", "белок": "protein", "б": "protein",
    "углеводов": "carbs", "углеводы": "carbs", "у": "carbs",
    "жиров": "fat", "жира": "fat", "ж": "fat",
}
_MACRO_KCAL_PER_G = {"protein": 4, "carbs": 4, "fat": 9}
_MACRO_RU = {"protein": "белка", "carbs": "углеводов", "fat": "жиров"}
_MACRO_FIELD = {"protein": "protein_g", "carbs": "carbs_g", "fat": "fat_g"}


def _quick_macro_rewrite(text: str) -> str | None:
    """Разбирает ярлык добора БЖУ/воды ("+30 белка", "+500 мл", "+0.5 л", "+1,5 л")
    в явную русскую инструкцию модели. Чистая функция: строка на входе, строка
    или None на выходе, без сайд-эффектов — тестируется без шлюза.

    Возвращаем REWRITE, а не skip. skip проглатывает сообщение без ответа
    (см. контракт pre_gateway_dispatch в комментарии у _remember_caller) —
    пользователь напишет "+30 белка" и получит тишину, это хуже, чем сейчас.
    rewrite хотя бы гарантирует, что модель ответит.

    Не совпало — None, сообщение идёт дальше нетронутым."""
    if not text:
        return None
    m = _QUICK_MACRO_RE.match(text.strip())
    if not m:
        return None
    raw_value, unit = m.groups()
    try:
        value = float(raw_value.replace(",", "."))
    except ValueError:
        return None
    unit_l = unit.lower()

    if unit_l in ("мл", "л", "воды"):
        ml = value * 1000 if unit_l == "л" else value
        return (
            f'Пользователь запросил быстрый добор воды. Вызови инструмент log_water '
            f'с параметром ml={ml:g}, без уточняющих вопросов.'
        )

    macro = _QUICK_MACRO_UNITS[unit_l]
    field = _MACRO_FIELD[macro]
    kcal = value * _MACRO_KCAL_PER_G[macro]
    ru = _MACRO_RU[macro]
    # handle_log_food (plugin/tools.py) жёстко требует у каждой позиции items[]
    # непустые kcal, protein_g, fat_g и carbs_g — иначе отдаёт ошибку и ничего
    # не пишет. Раз пользователь назвал только один макрос, остальные два
    # неизвестны и берутся нулём, а kcal оценивается по калорийности макроса
    # (белок/углеводы 4 ккал/г, жир 9 ккал/г) и помечается как оценка.
    return (
        f'Пользователь запросил быстрый добор {ru} в конце дня. Вызови инструмент '
        f'log_food с одной позицией items[0], meal_slot: "snack", name: "добор {ru} '
        f'(оценка)", {field}={value:g}, остальные два поля КБЖУ = 0, '
        f'kcal={kcal:g} (оценка по {_MACRO_KCAL_PER_G[macro]} ккал/г {ru}). '
        f'Без уточняющих вопросов.'
    )


def _remember_caller(event=None, **_kwargs):
    """Хук pre_gateway_dispatch: запомнить автора входящего сообщения и,
    отдельно, распознать быстрый ярлык добора БЖУ/воды ("+30 белка").

    Ничего не решает и никогда не бросает: хук стоит на пути КАЖДОГО сообщения,
    и падение здесь уронило бы обработку чужих сообщений ради нашей справки.
    Запоминание звонящего — это база, ярлык — надстройка сверху; сбой в
    разборе ярлыка не должен ломать запоминание звонящего, поэтому у него
    свой try/except."""
    try:
        uid = getattr(event, "user_id", None)
        if uid:
            _CALLER_FALLBACK.set(str(uid))
    except Exception:
        pass

    try:
        text = getattr(event, "text", None)
        rewrite = _quick_macro_rewrite(text) if text else None
        if rewrite:
            return {"action": "rewrite", "text": rewrite}
    except Exception:
        pass
    return None


def _caller_telegram_id() -> str | None:
    """Настоящий telegram id звонящего. Сначала session-контекст шлюза (обычный
    путь агента), затем запомненный хуком автор входящего сообщения (путь
    слэш-команды, где контекста ещё нет). None вне Hermes."""
    try:
        from gateway.session_context import get_session_env
        caller = get_session_env("HERMES_SESSION_USER_ID", "")
        if caller:
            return caller
    except ImportError:
        pass
    return _CALLER_FALLBACK.get()


def _get_user_id(params: dict, conn=None) -> int:
    """Extract user_id. In Hermes: resolve from Telegram caller. Otherwise: from params or default.

    If caller_telegram_id() returns an id AND a connection is available, resolve it via:
        SELECT id FROM users WHERE telegram_user_id = ?
    If the caller id is present but matches no user row, raise error.
    If there is no caller id at all (self-check, cron, CLI), fall back to params.get("user_id", 1).
    """
    caller_id = _caller_telegram_id()
    if caller_id and conn:
        # Try to resolve Telegram ID to database user_id
        row = conn.execute(
            "SELECT id, timezone FROM users WHERE telegram_user_id=?",
            (caller_id,),
        ).fetchone()
        if row:
            if row["timezone"] and not config._TZ.get():
                config.set_tz(row["timezone"])
            return row["id"]
        else:
            raise ValueError(f"Telegram user {caller_id} not found in database")
    # Fall back when no caller id (CLI, cron, self-check) or no connection
    uid = params.get("user_id", 1)
    if conn and not config._TZ.get():
        row = conn.execute("SELECT timezone FROM users WHERE id=?", (uid,)).fetchone()
        if row and row["timezone"]:
            config.set_tz(row["timezone"])
    return uid


def _now_iso(conn: sqlite3.Connection | None = None, user_id: int | None = None) -> str:
    """Время в каноническом формате схемы: YYYY-MM-DD HH:MM:SS, через пробел.
    Не ISO с 'T' — guards и report парсят по пробелу, а база хранит так же.
    Если переданы conn и user_id — гарантированно вычисляет время в поясе пользователя."""
    if conn is not None and user_id is not None:
        return config.user_now(conn, user_id).strftime("%Y-%m-%d %H:%M:%S")
    return config.local_now().strftime("%Y-%m-%d %H:%M:%S")


def _norm_ts(value: str | None) -> str | None:
    """Граница доверия: метку времени присылает модель, а она пишет ISO с 'T'.
    Приводим к формату схемы. Неразбираемое — наверх ошибкой, не молча."""
    if value is None:
        return None
    v = value.strip().replace("T", " ")
    if len(v) == 10:                      # только дата — доклеиваем полночь
        v += " 00:00:00"
    if len(v) == 16:                      # без секунд
        v += ":00"
    datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S")   # бросит ValueError, если мусор
    return v[:19]


# Окно правдоподобия для метки СЛУЧИВШЕГОСЯ события. Нарочно широкое и
# несимметричное: пояс у config.local_now() зависит от вызывающего (в боте —
# часовой пояс человека, в cron/CLI — серверный), и узкая верхняя граница
# ловила бы законные записи как «из будущего».
_EVENT_TS_PAST_DAYS = 400
_EVENT_TS_FUTURE_DAYS = 2


def _norm_event_ts(value: str | None, conn: sqlite3.Connection | None = None, user_id: int | None = None) -> str | None:
    """Метка события, которое уже произошло: еда, вода, глюкоза, замер, приём.

    Формат приводит _norm_ts, здесь проверяется правдоподобность. Модель
    ошибается годом: в базу легла запись о воде с at='2024-08-24' вместо 2026,
    формат безупречный — и суточная сумма её не увидела. Человек пил, панель
    показывала недобор, и никто не узнал бы, не сойдись цифры вручную.

    Молчащая запись хуже отказа: отказ модель видит в ответе инструмента и
    переспрашивает дату, а принятая мимо суток теряется навсегда.

    next_at у расписания препаратов сюда НЕ идёт — он будущий по смыслу и
    остаётся на _norm_ts.
    """
    ts = _norm_ts(value)
    if ts is None:
        return None
    if conn is not None and user_id is not None:
        now = config.user_now(conn, user_id).replace(tzinfo=None)
    else:
        now = config.local_now().replace(tzinfo=None)
    delta_days = (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") - now).days
    if delta_days > _EVENT_TS_FUTURE_DAYS:
        raise ValueError(f"Метка времени {ts} в будущем — событие ещё не произошло. "
                         f"Сейчас {now:%Y-%m-%d %H:%M}, проверь год и дату.")
    if delta_days < -_EVENT_TS_PAST_DAYS:
        raise ValueError(f"Метка времени {ts} старше {_EVENT_TS_PAST_DAYS} дней — "
                         f"похоже на ошибку в годе. Сейчас {now:%Y-%m-%d %H:%M}.")
    return ts


def _today_iso(conn: sqlite3.Connection | None = None, user_id: int | None = None) -> str:
    """Today as ISO date string in user's timezone."""
    if conn is not None and user_id is not None:
        return config.user_today(conn, user_id)
    return config.local_now().date().isoformat()


def _as_float(name: str, value) -> float:
    """Числовое поле, обязанное быть числом. SQLite не отказывает сама на TEXT
    в REAL-колонке — она просто хранит мусор, и он тихо всплывает при первом
    арифметическом использовании (delta, тренд), уже после того как строка
    записана. Гейт — здесь, до INSERT."""
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} должен быть числом, получено {value!r}")


_DEFAULT_MAX_ITEM_KCAL = 1200


def _max_item_kcal() -> float:
    """config.yaml log_food.max_item_kcal, с запасным значением, если ключ
    ещё не завезли (старый config.yaml на деплое отставания)."""
    return config.load().get("log_food", {}).get("max_item_kcal", _DEFAULT_MAX_ITEM_KCAL)


_DEFAULT_STYLE_NAME = "debian"
_DEFAULT_STYLE_TEXT = "Технический лаконизм в стиле Debian: коротко, по делу, без эмоций и оценочных суждений."


def _active_style(conn, user_id: int) -> dict:
    """Активный стиль персоны. Нет ни одного — заводим debian и делаем активным:
    у ответа всегда есть манера, и лучше явная строка в базе, чем неявное
    поведение, которого не видно в /style list."""
    row = conn.execute(
        "SELECT name, instruction FROM persona_styles WHERE user_id=? AND is_active=1",
        (user_id,),
    ).fetchone()
    if row is not None:
        return {"name": row["name"], "instruction": row["instruction"]}

    any_row = conn.execute(
        "SELECT name, instruction FROM persona_styles WHERE user_id=? ORDER BY created_at LIMIT 1",
        (user_id,),
    ).fetchone()
    if any_row is None:
        # Ни одной строки вообще — лениво заводим дефолт debian и сразу активируем.
        conn.execute(
            "INSERT INTO persona_styles(user_id, name, instruction, is_active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (user_id, _DEFAULT_STYLE_NAME, _DEFAULT_STYLE_TEXT, _now_iso()),
        )
        conn.commit()
        return {"name": _DEFAULT_STYLE_NAME, "instruction": _DEFAULT_STYLE_TEXT}

    # Строки есть, активной нет (не должно бывать в норме, но не гадаем молча):
    # debian, если он среди них, иначе самый старый.
    debian_row = conn.execute(
        "SELECT name, instruction FROM persona_styles WHERE user_id=? AND name=?",
        (user_id, _DEFAULT_STYLE_NAME),
    ).fetchone()
    chosen = debian_row if debian_row is not None else any_row
    conn.execute(
        "UPDATE persona_styles SET is_active=1 WHERE user_id=? AND name=?",
        (user_id, chosen["name"]),
    )
    conn.commit()
    return {"name": chosen["name"], "instruction": chosen["instruction"]}


# Личность звонящего подмешивает гейтвей в каждый вызов (см. _remember_caller),
# это не контракт отдельного инструмента. Разрешены везде.
_IDENTITY_PARAMS = frozenset({"user_id", "telegram_user_id"})


@functools.lru_cache(maxsize=None)
def _allowed_params(fn_name: str) -> frozenset | None:
    """Ключи JSON-схемы инструмента. None — схемы нет, проверять нечем.

    Пара выводится из имени: handle_log_glucose -> schemas.log_glucose_schema.
    Так же они спарены в register(), так что расхождение имён сломает и там."""
    try:
        from . import schemas
    except ImportError:
        import schemas
    schema = getattr(schemas, fn_name.removeprefix("handle_") + "_schema", None)
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return None
    return frozenset(props) | _IDENTITY_PARAMS


def _handler_wrapper(fn):
    """Ловит исключения в JSON и отбивает неизвестные параметры.

    Молчаливое умолчание на неизвестный ключ уже дало неверную медицинскую
    запись: measured_at вместо at -> глюкоза легла на now() вместо названного
    времени, значение правдоподобное, сигнала нет. Лучше отказ."""
    def wrapper(params=None, task_id=None, **kwargs):
        try:
            if not isinstance(params, dict):
                params = {}
            allowed = _allowed_params(fn.__name__)
            if allowed is not None:
                unknown = sorted(set(params) - allowed)
                if unknown:
                    return json.dumps({
                        "error": f"Неизвестные параметры: {', '.join(unknown)}. "
                                 f"Допустимые: {', '.join(sorted(allowed))}",
                    }, ensure_ascii=False)
            return fn(params)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
    return wrapper


# ---------------------------------------------------------------- WRITE TOOLS

_MEAL_SLOTS = {"breakfast", "lunch", "dinner", "snack"}


def _delete_food(conn, user_id: int, params: dict) -> str:
    """Удалить ошибочно записанный приём или отдельные продукты из него.

    Всегда фильтрует по user_id: номер приёма приходит из текста модели, и без
    этой проверки чужая запись удалялась бы по опечатке в цифре. Возвращает то,
    что удалено, поимённо — молчаливое "ок" на удалении данных не годится,
    человек должен увидеть, что ушло именно то.
    """
    try:
        log_id = int(params.get("food_log_id"))
    except (TypeError, ValueError):
        conn.close()
        return json.dumps({"error": "Нужен food_log_id — номер приёма. "
                                    "Он показан в журнале как #N: "
                                    "get_day_summary format=journal"}, ensure_ascii=False)

    row = conn.execute("SELECT eaten_at, meal_slot FROM food_log WHERE id=? AND user_id=?",
                       (log_id, user_id)).fetchone()
    if row is None:
        conn.close()
        return json.dumps({"error": f"Приёма #{log_id} нет"}, ensure_ascii=False)

    existing = conn.execute(
        "SELECT id, name, grams, kcal FROM food_items WHERE food_log_id=?", (log_id,)).fetchall()
    names = params.get("names") or []

    if names:
        wanted = [n.strip().lower() for n in names if str(n).strip()]
        doomed = [r for r in existing
                  if any(w in (r["name"] or "").lower() for w in wanted)]
        if not doomed:
            conn.close()
            return json.dumps({"error": f"В приёме #{log_id} нет продуктов по запросу "
                                        f"{names}. Есть: {[r['name'] for r in existing]}"},
                              ensure_ascii=False)
    else:
        doomed = list(existing)

    removed = [{"name": r["name"], "grams": r["grams"], "kcal": r["kcal"]} for r in doomed]
    conn.executemany("DELETE FROM food_items WHERE id=?", [(r["id"],) for r in doomed])
    # Приём без позиций — это "0 ккал" в сводке при живой строке food_log.
    # Такой пустой остаток убираем, иначе он тихо ломает журнал.
    left = conn.execute("SELECT COUNT(*) n FROM food_items WHERE food_log_id=?",
                        (log_id,)).fetchone()["n"]
    if not left:
        conn.execute("DELETE FROM food_log WHERE id=? AND user_id=?", (log_id, user_id))
    conn.commit()

    date_str = (row["eaten_at"] or "")[:10] or _today_iso(conn, user_id)
    summary = day_summary(conn, user_id, date_str)
    conn.close()
    return json.dumps({
        "deleted": removed,
        "meal_removed": not left,
        "food_log_id": log_id,
        "date": date_str,
        "kcal_now": round(summary["kcal_eaten"]),
        "protein_now": round(summary["protein_g"], 1),
    }, ensure_ascii=False)


@_handler_wrapper
def handle_log_food(params: dict) -> str:
    """Log food items and return day summary with status bar and alerts."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)

    if (params.get("action") or "add").lower() == "delete":
        return _delete_food(conn, user_id, params)

    items = params.get("items") or []
    meal_slot = params.get("meal_slot")

    raw_eaten = params.get("eaten_at")
    if raw_eaten:
        cur_year = str(config.user_now(conn, user_id).year)
        if raw_eaten.startswith(("2024-", "2025-")):
            raw_eaten = cur_year + raw_eaten[4:]
    eaten_at = _norm_event_ts(raw_eaten, conn, user_id) or _now_iso(conn, user_id)

    # Приём без позиций бессмысленен: калории и КБЖУ живут в food_items, и пустой
    # food_log даёт "0 ккал" в сводке при бодром "записано" в ответе. Это ровно тот
    # молчаливый провал, который уже дважды прятал сбой записи. Отказываем ДО вставки.
    if not items:
        conn.close()
        return json.dumps(
            {"error": "Нужен непустой items[]: приём без позиций не записывается. "
                      "Укажите блюдо, граммы и КБЖУ."},
            ensure_ascii=False)

    for i in items:
        nm = str(i.get("name") or "").strip()
        i["name"] = nm if nm else "Блюдо"

    # per_100g + grams (CONTEXT.md «Состав продукта»): код, а не модель, считает
    # kcal/protein_g/fat_g/carbs_g позиции из состава на 100 г (food_lookup
    # match/search/remember) и граммовки. Заполняем эти поля здесь, ДО проверки
    # обязательных КБЖУ ниже — старый формат (модель сама прислала числа) через
    # этот блок просто не проходит (per_100g отсутствует) и работает как раньше.
    _ITEM_SOURCES = ("off", "my_product", "label", "estimate")
    for i in items:
        per100 = i.get("per_100g")
        src = i.get("source")
        if src is not None and src not in _ITEM_SOURCES:
            conn.close()
            return json.dumps(
                {"error": f"{i['name']}: source должен быть одним из {_ITEM_SOURCES}, получено {src!r}"},
                ensure_ascii=False)
        if per100 is None:
            continue
        if not isinstance(per100, dict):
            conn.close()
            return json.dumps({"error": f"{i['name']}: per_100g должен быть объектом"}, ensure_ascii=False)
        if i.get("grams") is None:
            conn.close()
            return json.dumps({"error": f"{i['name']}: per_100g требует grams"}, ensure_ascii=False)
        try:
            factor = _as_float("grams", i["grams"]) / 100.0
            i["kcal"] = _as_float("per_100g.kcal", per100.get("kcal")) * factor
            i["protein_g"] = _as_float("per_100g.protein_g", per100.get("protein_g")) * factor
            i["fat_g"] = _as_float("per_100g.fat_g", per100.get("fat_g")) * factor
            i["carbs_g"] = _as_float("per_100g.carbs_g", per100.get("carbs_g")) * factor
            if per100.get("fiber_g") is not None and i.get("fiber_g") is None:
                i["fiber_g"] = _as_float("per_100g.fiber_g", per100["fiber_g"]) * factor
        except ValueError as e:
            conn.close()
            return json.dumps({"error": f"{i['name']}: {e}"}, ensure_ascii=False)

    # meal_slot необязателен (CONTEXT.md «Приём пищи»): передан — используется
    # как есть, после проверки enum (прямое слово человека побеждает всегда).
    # Не передан — код сам определяет приём по окнам (health_core.chrono.
    # meal_slot): в окне и без более раннего приёма этого типа — основной
    # приём этого окна; иначе (вне окон или позже snack_after_main_minutes
    # после начала уже состоявшегося приёма) — snack. Больше не подсказка без
    # гейта: модель либо называет приём словом, либо не передаёт его вовсе.
    if meal_slot is not None and meal_slot not in _MEAL_SLOTS:
        conn.close()
        return json.dumps(
            {"error": f"meal_slot должен быть одним из {sorted(_MEAL_SLOTS)}, получено {meal_slot!r}"},
            ensure_ascii=False)
    if meal_slot is None:
        from health_core.chrono import meal_slot as _assign_meal_slot
        meal_slot = _assign_meal_slot(conn, user_id, datetime.strptime(eaten_at, "%Y-%m-%d %H:%M:%S"))
    # required в схеме — только подсказка модели: Gemini соблюдает её не всегда
    # (наблюдалось на проде — grams в required, а модель всё равно прислала
    # None). Реальный гейт — здесь. kcal/protein_g/fat_g/carbs_g обязательны:
    # protein_g кормит LBM_RATIO и RATE_HIGH, fat_g — LIPID_GUARD, carbs_g
    # закрывает сумму макросов. grams НЕ гейтим: "омлет, 320 ккал" — уже полная
    # информация, а требование граммов заставило бы модель выдумывать число.
    _MACRO_FIELDS = ("kcal", "protein_g", "fat_g", "carbs_g")
    _bad = []
    for i in items:
        missing = [f for f in _MACRO_FIELDS if i.get(f) is None]
        if missing:
            _bad.append(f"{i.get('name') or '?'} (нет: {', '.join(missing)})")
    if _bad:
        conn.close()
        return json.dumps(
            {"error": f"Не хватает КБЖУ у позиций: {'; '.join(_bad)}. "
                      f"Оценку тоже можно, но числа обязательны."},
            ensure_ascii=False)

    # Одна позиция = один приём пищи. Модель иногда схлопывает весь день в один
    # item (наблюдалось на проде: 'Полный рацион за 21.08 (Завтрак, перекус,
    # обед...)' на 1440 ккал одной строкой) — это стирает meal_slot и делает
    # окно LIPID_GUARD (±3ч от одной точки) бессмысленным. Потолок ккал на
    # позицию из config.yaml log_food.max_item_kcal, не эвристика по названию.
    _ceiling = _max_item_kcal()
    _over = []
    for i in items:
        try:
            _k = float(i.get("kcal"))
        except (TypeError, ValueError):
            continue  # нечисловой kcal — не забота этого гейта, свалится ниже явной ошибкой
        if _k > _ceiling:
            _over.append(f"{i.get('name') or '?'} ({_k:g} ккал)")
    if _over:
        conn.close()
        return json.dumps(
            {"error": f"Превышен лимит {_ceiling:g} ккал на позицию: {'; '.join(_over)}. "
                      f"Разбейте приём на отдельные items по блюдам, не одной строкой на весь день."},
            ensure_ascii=False)

    # Insert food log entry
    cur = conn.execute(
        "INSERT INTO food_log(user_id, eaten_at, meal_slot) VALUES (?, ?, ?)",
        (user_id, eaten_at, meal_slot),
    )
    food_log_id = cur.lastrowid

    # Insert food items
    _written = 0
    for item in items:
        icur = conn.execute(
            "INSERT INTO food_items(food_log_id, name, grams, kcal, protein_g, fat_g, carbs_g, "
            "fiber_g, plate_category, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                food_log_id,
                item.get("name"),
                item.get("grams"),
                item.get("kcal"),
                item.get("protein_g"),
                item.get("fat_g"),
                item.get("carbs_g"),
                # Клетчатка необязательна: модель знает её не для всякого продукта,
                # а отказ записать приём из-за неизвестной клетчатки хуже пробела.
                item.get("fiber_g"),
                item.get("plate_category"),
                # off|my_product|label|estimate (CONTEXT.md «Состав продукта»); NULL —
                # старый формат, модель прислала числа сама, без food_lookup.
                item.get("source"),
            ),
        )
        _written += icur.rowcount

    # Запись проверяет собственный эффект. Не сошлось — откатываем ВСЁ вместе с
    # родительской строкой, иначе в базе останется приём-призрак без позиций.
    if _written != len(items):
        conn.rollback()
        conn.close()
        return json.dumps(
            {"error": f"Записано {_written} позиций из {len(items)} — приём отменён"},
            ensure_ascii=False)
    conn.commit()

    # Check guards and record alerts
    alerts = check_all(conn, user_id)
    record(conn, user_id, alerts)

    # Return status bar with alerts
    bar = status_bar(conn, user_id)
    conn.close()

    return json.dumps(
        {"status_bar": bar, "alerts": alerts, "meal_slot": meal_slot,
         # source по каждой позиции — видно, откуда взят состав (CONTEXT.md
         # «Состав продукта»): off/my_product/label — из базы, estimate/NULL — оценка.
         "items": [{"name": i["name"], "kcal": i.get("kcal"), "source": i.get("source")} for i in items]},
        ensure_ascii=False,
    )


def _product_row(r) -> dict:
    return {
        "product_id": r["id"], "name": r["display_name"], "off_code": r["off_code"],
        "kcal_100g": r["kcal_100g"], "protein_100g": r["protein_100g"], "fat_100g": r["fat_100g"],
        "carbs_100g": r["carbs_100g"], "fiber_100g": r["fiber_100g"], "source": r["source"],
    }


@_handler_wrapper
def handle_food_lookup(params: dict) -> str:
    """Состав продукта (CONTEXT.md «Состав продукта», «Мой продукт»,
    health_core/foods.py): match — найти СВОЙ сохранённый продукт по названию,
    в любой формулировке; search — до 5 кандидатов из Open Food Facts;
    remember — сохранить/обновить свой продукт (off_code — код сам подтянет
    состав, либо отдай числа на 100 г напрямую, например с этикетки); list —
    свои продукты; forget — убрать сохранённый продукт."""
    from health_core import foods

    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    action = (params.get("action") or "match").lower()

    if action in ("match", "forget"):
        name = (params.get("name") or "").strip()
        if not name:
            conn.close()
            return json.dumps({"error": "Нужно name"}, ensure_ascii=False)
        if action == "forget":
            ok = foods.forget(conn, user_id, name)
            conn.close()
            return json.dumps({"ok": ok}, ensure_ascii=False)
        row = foods.find_mine(conn, user_id, name)
        conn.close()
        if row is None:
            return json.dumps({"found": False}, ensure_ascii=False)
        return json.dumps({"found": True, **_product_row(row)}, ensure_ascii=False)

    if action == "search":
        name = (params.get("name") or "").strip()
        conn.close()
        if not name:
            return json.dumps({"error": "Нужно name"}, ensure_ascii=False)
        return json.dumps(foods.search(name), ensure_ascii=False)

    if action == "list":
        rows = foods.list_mine(conn, user_id)
        conn.close()
        return json.dumps({"products": [_product_row(r) for r in rows]}, ensure_ascii=False)

    if action == "remember":
        name = (params.get("name") or "").strip()
        source = params.get("source")
        if not name:
            conn.close()
            return json.dumps({"error": "Нужно name"}, ensure_ascii=False)
        if source not in ("off", "label", "estimate"):
            conn.close()
            return json.dumps({"error": "source должен быть off, label или estimate"}, ensure_ascii=False)

        off_code = params.get("off_code")
        nums = {k: params.get(f"{k}_100g") for k in ("kcal", "protein", "fat", "carbs", "fiber")}
        # Числа не даны, но есть код OFF — код сам подтягивает состав по коду
        # (та же единая точка сети, что у search()), а не заставляет модель
        # переспрашивать то, что уже показал предыдущий search().
        if nums["kcal"] is None and off_code:
            fetched = foods.fetch_by_code(off_code)
            if fetched.get("error"):
                conn.close()
                return json.dumps(fetched, ensure_ascii=False)
            for k in nums:
                if nums[k] is None:
                    nums[k] = fetched.get(f"{k}_100g")

        try:
            product_id = foods.remember(
                conn, user_id, name, source=source, off_code=off_code,
                kcal_100g=_as_float("kcal_100g", nums["kcal"]) if nums["kcal"] is not None else None,
                protein_100g=_as_float("protein_100g", nums["protein"]) if nums["protein"] is not None else None,
                fat_100g=_as_float("fat_100g", nums["fat"]) if nums["fat"] is not None else None,
                carbs_100g=_as_float("carbs_100g", nums["carbs"]) if nums["carbs"] is not None else None,
                fiber_100g=_as_float("fiber_100g", nums["fiber"]) if nums["fiber"] is not None else None,
            )
        except ValueError as e:
            conn.close()
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        conn.close()
        return json.dumps({"ok": True, "product_id": product_id}, ensure_ascii=False)

    conn.close()
    return json.dumps(
        {"error": f"Неизвестное действие: {action}. Допустимо: match, search, remember, list, forget"},
        ensure_ascii=False)


@_handler_wrapper
def handle_log_water(params: dict) -> str:
    """Log water intake and return total with target."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)

    action = (params.get("action") or "add").lower()

    # ── Просмотр записей воды за день / диапазон ──
    if action == "list":
        date_str = params.get("date") or _today_iso()
        rows = conn.execute(
            "SELECT id, volume_ml, at FROM water_log WHERE user_id=? AND date(at)=? ORDER BY at ASC",
            (user_id, date_str)
        ).fetchall()
        entries = [{"water_id": r["id"], "volume_ml": r["volume_ml"], "at": r["at"]} for r in rows]
        d = day_summary(conn, user_id, date_str)
        conn.close()
        return json.dumps({
            "date": date_str,
            "entries": entries,
            "total_water_ml": d.get("water_ml", 0),
            "water_target_ml": d.get("water_target_ml", 0),
        }, ensure_ascii=False)

    # ── Удаление ошибочной записи воды ──
    if action == "delete":
        date_str = params.get("date") or _today_iso()
        clear_day = bool(params.get("clear_day") or params.get("all"))
        water_id = params.get("water_id")

        if clear_day or (water_id is None and params.get("date")):
            # Удалить всю воду за указанную дату
            deleted_rows = conn.execute(
                "SELECT id, volume_ml, at FROM water_log WHERE user_id=? AND date(at)=?",
                (user_id, date_str)
            ).fetchall()
            deleted = [{"water_id": r["id"], "volume_ml": r["volume_ml"], "at": r["at"]} for r in deleted_rows]
            conn.execute("DELETE FROM water_log WHERE user_id=? AND date(at)=?", (user_id, date_str))
            conn.commit()
            d = day_summary(conn, user_id, date_str)
            conn.close()
            return json.dumps({
                "deleted": deleted,
                "count": len(deleted),
                "date": date_str,
                "water_ml": d.get("water_ml", 0),
                "water_target_ml": d.get("water_target_ml", 0),
            }, ensure_ascii=False)

        # Удаление конкретной записи по water_id или последней записи, если water_id не указан
        if water_id is None:
            # Если water_id не указан явно, удаляем последнюю запись воды за сегодня
            row = conn.execute(
                "SELECT id, volume_ml, at FROM water_log WHERE user_id=? AND date(at)=? ORDER BY at DESC, id DESC LIMIT 1",
                (user_id, date_str)
            ).fetchone()
            if row is None:
                conn.close()
                return json.dumps({"error": f"Записей воды за {date_str} нет"}, ensure_ascii=False)
            record_id = row["id"]
        else:
            try:
                record_id = int(water_id)
            except (TypeError, ValueError):
                conn.close()
                return json.dumps({"error": "Нужен water_id — номер записи воды"}, ensure_ascii=False)
            row = conn.execute(
                "SELECT volume_ml, at FROM water_log WHERE id=? AND user_id=?",
                (record_id, user_id)).fetchone()
            if row is None:
                conn.close()
                return json.dumps({"error": f"Записи #{record_id} нет"}, ensure_ascii=False)

        conn.execute("DELETE FROM water_log WHERE id=? AND user_id=?", (record_id, user_id))
        conn.commit()
        d = day_summary(conn, user_id, (row["at"] or "")[:10] or _today_iso(conn, user_id))
        conn.close()
        return json.dumps({
            "deleted": {"water_id": record_id, "volume_ml": row["volume_ml"], "at": row["at"]},
            "id": record_id,
            "water_ml": d.get("water_ml", 0),
            "water_target_ml": d.get("water_target_ml", 0),
        }, ensure_ascii=False)

    ml = params.get("ml")
    at = _norm_event_ts(params.get("at"), conn, user_id) or _now_iso(conn, user_id)

    # Ноль и отрицательные миллилитры — не запись, а мусор в логе: они портят
    # суточную сумму и выглядят как выполненное действие. В базе уже лежала
    # строка на 0.0 мл, попавшая туда молча.
    try:
        ml = float(ml)
    except (TypeError, ValueError):
        conn.close()
        return json.dumps({"error": "Нужен объём в мл числом"}, ensure_ascii=False)
    if ml <= 0:
        conn.close()
        return json.dumps({"error": f"Объём должен быть больше нуля, получено {ml:g}"},
                          ensure_ascii=False)

    # Normalize timestamp
    if "T" in at and at.count(":") == 1:
        at += ":00"

    # Insert water log
    cur = conn.execute(
        "INSERT INTO water_log(user_id, at, volume_ml) VALUES (?, ?, ?)",
        (user_id, at, ml),
    )
    water_id = cur.lastrowid
    conn.commit()

    # Check guards and record alerts
    alerts = check_all(conn, user_id)
    record(conn, user_id, alerts)

    # Get day summary for water info — за дату записи (at), не за сегодня
    record_date = at[:10] if at else _today_iso()
    d = day_summary(conn, user_id, record_date)
    water_ml = d.get("water_ml", 0)
    water_target_ml = d.get("water_target_ml", 0)

    conn.close()

    return json.dumps(
        {
            "water_id": water_id,
            "water_ml": water_ml,
            "water_target_ml": water_target_ml,
            "alerts": alerts,
        },
        ensure_ascii=False,
    )


@_handler_wrapper
def handle_log_sleep(params: dict) -> str:
    """Одна ночь сна. Дата ночи = дата утра, чтобы 23:40 и 00:20 не разъезжались."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)

    action = (params.get("action") or "add").lower()

    # ── Просмотр записей сна ──
    if action == "list":
        try:
            limit = min(int(params.get("limit") or 10), 50)
        except (TypeError, ValueError):
            limit = 10
        rows = conn.execute(
            "SELECT id, night_date, duration_min FROM sleep_log WHERE user_id=? ORDER BY night_date DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        entries = [{"sleep_id": r["id"], "night_date": r["night_date"], "duration_min": r["duration_min"]} for r in rows]
        conn.close()
        return json.dumps({
            "entries": entries,
            "count": len(entries),
        }, ensure_ascii=False)

    # ── Удаление ошибочной записи сна ──
    if action == "delete":
        sleep_id = params.get("sleep_id")

        if sleep_id is None:
            # Если sleep_id не указан явно, удаляем последнюю запись сна
            row = conn.execute(
                "SELECT id, night_date, duration_min FROM sleep_log WHERE user_id=? ORDER BY night_date DESC, id DESC LIMIT 1",
                (user_id,)
            ).fetchone()
            if row is None:
                conn.close()
                return json.dumps({"error": "Записей сна нет"}, ensure_ascii=False)
            record_id = row["id"]
        else:
            try:
                record_id = int(sleep_id)
            except (TypeError, ValueError):
                conn.close()
                return json.dumps({"error": "Нужен sleep_id — номер записи сна"}, ensure_ascii=False)
            row = conn.execute(
                "SELECT night_date, duration_min FROM sleep_log WHERE id=? AND user_id=?",
                (record_id, user_id)).fetchone()
            if row is None:
                conn.close()
                return json.dumps({"error": f"Записи #{record_id} нет"}, ensure_ascii=False)

        conn.execute("DELETE FROM sleep_log WHERE id=? AND user_id=?", (record_id, user_id))
        conn.commit()
        conn.close()
        return json.dumps({
            "deleted": {"sleep_id": record_id, "night_date": row["night_date"], "duration_min": row["duration_min"]},
            "id": record_id,
        }, ensure_ascii=False)

    night = params.get("night_date") or _today_iso()
    try:
        datetime.strptime(night, "%Y-%m-%d")
    except (TypeError, ValueError):
        conn.close()
        return json.dumps({"error": f"night_date должна быть YYYY-MM-DD, получено {night!r}"},
                          ensure_ascii=False)

    try:
        duration = int(params.get("duration_min"))
    except (TypeError, ValueError):
        conn.close()
        return json.dumps({"error": "Нужна длительность сна в минутах (duration_min)"}, ensure_ascii=False)
    # Сутки — 1440 минут. Всё вне (0, 1440] это опечатка или часы вместо минут,
    # а не сон: такая строка тихо перекосила бы среднее по неделе.
    if not 0 < duration <= 1440:
        conn.close()
        return json.dumps({"error": f"duration_min должна быть 1..1440, получено {duration}"},
                          ensure_ascii=False)

    quality = params.get("quality")
    if quality is not None:
        try:
            quality = int(quality)
        except (TypeError, ValueError):
            quality = None
        if quality is not None and not 1 <= quality <= 5:
            conn.close()
            return json.dumps({"error": f"quality — оценка 1..5, получено {quality}"}, ensure_ascii=False)

    def _stage(name):
        v = params.get(name)
        if v is None:
            return None            # пусто = не знаем, а не «этой фазы не было»
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None
        return v if 0 <= v <= duration else None

    spo2_avg = params.get("spo2_avg")
    if spo2_avg is not None:
        try:
            spo2_avg = int(spo2_avg)
        except (TypeError, ValueError):
            spo2_avg = None
        if spo2_avg is not None and not 70 <= spo2_avg <= 100:
            spo2_avg = None

    conn.execute(
        "INSERT INTO sleep_log(user_id, night_date, bedtime, wake_time, duration_min, "
        "deep_min, rem_min, awake_min, quality, spo2_avg, source, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id, night_date) DO UPDATE SET "
        "bedtime=COALESCE(excluded.bedtime,bedtime), wake_time=COALESCE(excluded.wake_time,wake_time), "
        "duration_min=excluded.duration_min, deep_min=COALESCE(excluded.deep_min,deep_min), "
        "rem_min=COALESCE(excluded.rem_min,rem_min), awake_min=COALESCE(excluded.awake_min,awake_min), "
        "quality=COALESCE(excluded.quality,quality), spo2_avg=COALESCE(excluded.spo2_avg,spo2_avg), "
        "source=COALESCE(excluded.source,source), notes=COALESCE(excluded.notes,notes)",
        (user_id, night, params.get("bedtime"), params.get("wake_time"), duration,
         _stage("deep_min"), _stage("rem_min"), _stage("awake_min"), quality, spo2_avg,
         params.get("source"), params.get("notes")),
    )
    conn.commit()

    # Получим ID записи сна
    sleep_row = conn.execute(
        "SELECT id FROM sleep_log WHERE user_id=? AND night_date=?",
        (user_id, night),
    ).fetchone()
    sleep_id = sleep_row["id"] if sleep_row else None

    avg = conn.execute(
        "SELECT AVG(duration_min) d, COUNT(*) n FROM sleep_log WHERE user_id=? AND night_date >= date(?, '-6 day')",
        (user_id, night),
    ).fetchone()
    conn.close()
    out = {"sleep_id": sleep_id,
           "ok": f"Сон за ночь {night}: {duration // 60} ч {duration % 60:02d} мин",
           "duration_min": duration}
    if quality is not None:
        out["quality"] = quality
    if avg["n"]:
        out["avg_7d_min"] = round(avg["d"])
    return json.dumps(out, ensure_ascii=False)


@_handler_wrapper
def handle_log_glucose(params: dict) -> str:
    """Log glucose reading and return confirmation with trend."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)

    action = (params.get("action") or "add").lower()

    # ── Просмотр записей глюкозы ──
    if action == "list":
        try:
            limit = min(int(params.get("limit") or 10), 50)
        except (TypeError, ValueError):
            limit = 10
        rows = conn.execute(
            "SELECT id, mmol_l, context, at FROM glucose_log WHERE user_id=? ORDER BY at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        entries = [{"glucose_id": r["id"], "mmol_l": r["mmol_l"], "context": r["context"], "at": r["at"]} for r in rows]
        conn.close()
        return json.dumps({
            "entries": entries,
            "count": len(entries),
        }, ensure_ascii=False)

    # ── Удаление ошибочной записи глюкозы ──
    if action == "delete":
        glucose_id = params.get("glucose_id")

        if glucose_id is None:
            # Если glucose_id не указан явно, удаляем последнюю запись глюкозы
            row = conn.execute(
                "SELECT id, mmol_l, context, at FROM glucose_log WHERE user_id=? ORDER BY at DESC, id DESC LIMIT 1",
                (user_id,)
            ).fetchone()
            if row is None:
                conn.close()
                return json.dumps({"error": "Записей глюкозы нет"}, ensure_ascii=False)
            record_id = row["id"]
        else:
            try:
                record_id = int(glucose_id)
            except (TypeError, ValueError):
                conn.close()
                return json.dumps({"error": "Нужен glucose_id — номер записи сахара"}, ensure_ascii=False)
            row = conn.execute(
                "SELECT mmol_l, context, at FROM glucose_log WHERE id=? AND user_id=?",
                (record_id, user_id)).fetchone()
            if row is None:
                conn.close()
                return json.dumps({"error": f"Записи #{record_id} нет"}, ensure_ascii=False)

        conn.execute("DELETE FROM glucose_log WHERE id=? AND user_id=?", (record_id, user_id))
        conn.commit()
        conn.close()
        return json.dumps({
            "deleted": {"glucose_id": record_id, "mmol_l": row["mmol_l"], "context": row["context"], "at": row["at"]},
            "id": record_id,
        }, ensure_ascii=False)

    mmol_l = params.get("mmol_l")
    context = params.get("context")
    at = _norm_event_ts(params.get("at"), conn, user_id) or _now_iso(conn, user_id)
    confirmed = params.get("confirmed", False)

    # mmol_l required в схеме — подсказка, не гарантия: без гейта здесь NULL-запись
    # проходит молча (glucose_log.mmol_l нет NOT NULL) и выглядит как успех.
    try:
        mmol_l = _as_float("mmol_l", mmol_l)
    except ValueError as e:
        conn.close()
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    # Normalize timestamp
    if "T" in at and at.count(":") == 1:
        at += ":00"

    # Insert glucose log
    cur = conn.execute(
        "INSERT INTO glucose_log(user_id, at, mmol_l, context, confirmed) VALUES (?, ?, ?, ?, ?)",
        (user_id, at, mmol_l, context, 1 if confirmed else 0),
    )
    glucose_id = cur.lastrowid
    conn.commit()

    # Check guards and record alerts
    alerts = check_all(conn, user_id)
    record(conn, user_id, alerts)

    # Get last few readings for trend
    rows = conn.execute(
        "SELECT mmol_l, at FROM glucose_log WHERE user_id=? ORDER BY at DESC LIMIT 2",
        (user_id,),
    ).fetchall()

    trend = None
    if len(rows) >= 2:
        trend = round(rows[1]["mmol_l"] - rows[0]["mmol_l"], 2)

    conn.close()

    return json.dumps(
        {
            "glucose_id": glucose_id,
            "confirmed": True,
            "mmol_l": mmol_l,
            "trend": trend,
            "alerts": alerts,
        },
        ensure_ascii=False,
    )


@_handler_wrapper
def handle_log_side_effect(params: dict) -> str:
    """Журнал побочных эффектов (CONTEXT.md «Побочный эффект»): дата, симптом,
    тяжесть. Без привязки к препарату — связь по времени приёма устанавливает
    модель, не код. action=add|list|delete."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)

    action = (params.get("action") or "add").lower()

    # ── Просмотр записей ──
    if action == "list":
        try:
            limit = min(int(params.get("limit") or 20), 100)
        except (TypeError, ValueError):
            limit = 20
        q = "SELECT id, symptom, severity, at, notes FROM side_effects WHERE user_id=?"
        args: list = [user_id]
        since_days = params.get("since_days")
        if since_days is not None:
            try:
                since = (config.local_now() - timedelta(days=int(since_days))).strftime("%Y-%m-%d %H:%M:%S")
                q += " AND at>=?"
                args.append(since)
            except (TypeError, ValueError):
                pass
        q += " ORDER BY at DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(q, args).fetchall()
        entries = [
            {"side_effect_id": r["id"], "symptom": r["symptom"], "severity": r["severity"],
             "at": r["at"], "notes": r["notes"]}
            for r in rows
        ]
        conn.close()
        return json.dumps({"entries": entries, "count": len(entries)}, ensure_ascii=False)

    # ── Удаление ошибочной записи ──
    if action == "delete":
        side_effect_id = params.get("side_effect_id")

        if side_effect_id is None:
            row = conn.execute(
                "SELECT id, symptom, severity, at, notes FROM side_effects WHERE user_id=? "
                "ORDER BY at DESC, id DESC LIMIT 1",
                (user_id,)
            ).fetchone()
            if row is None:
                conn.close()
                return json.dumps({"error": "Записей побочных эффектов нет"}, ensure_ascii=False)
            record_id = row["id"]
        else:
            try:
                record_id = int(side_effect_id)
            except (TypeError, ValueError):
                conn.close()
                return json.dumps({"error": "Нужен side_effect_id — номер записи"}, ensure_ascii=False)
            row = conn.execute(
                "SELECT symptom, severity, at, notes FROM side_effects WHERE id=? AND user_id=?",
                (record_id, user_id)).fetchone()
            if row is None:
                conn.close()
                return json.dumps({"error": f"Записи #{record_id} нет"}, ensure_ascii=False)

        conn.execute("DELETE FROM side_effects WHERE id=? AND user_id=?", (record_id, user_id))
        conn.commit()
        conn.close()
        return json.dumps({
            "deleted": {"side_effect_id": record_id, "symptom": row["symptom"], "severity": row["severity"],
                       "at": row["at"], "notes": row["notes"]},
            "id": record_id,
        }, ensure_ascii=False)

    symptom = (params.get("symptom") or "").strip()
    if not symptom:
        conn.close()
        return json.dumps({"error": "Нужен symptom — название симптома"}, ensure_ascii=False)
    severity = params.get("severity")
    if severity is not None and severity not in ("mild", "moderate", "severe"):
        conn.close()
        return json.dumps({"error": "severity должен быть mild, moderate или severe"}, ensure_ascii=False)
    at = _norm_event_ts(params.get("at"), conn, user_id) or _now_iso(conn, user_id)

    cur = conn.execute(
        "INSERT INTO side_effects(user_id, at, symptom, severity, notes) VALUES (?, ?, ?, ?, ?)",
        (user_id, at, symptom, severity, params.get("notes")),
    )
    side_effect_id = cur.lastrowid
    conn.commit()
    conn.close()

    return json.dumps(
        {"side_effect_id": side_effect_id, "symptom": symptom, "severity": severity, "at": at},
        ensure_ascii=False,
    )


@_handler_wrapper
def handle_log_watch_day(params: dict) -> str:
    """День с часов (CONTEXT.md «День с часов»): пульс (min/avg/max), шаги,
    калории активности, стресс, HRV за КОНКРЕТНЫЙ календарный день,
    action=add|list|delete. Только один день за раз в каждом элементе days —
    сводки за неделю/месяц не пишем, их правдоподобие код проверить не может.
    Батч days принимает сразу несколько дней (например, скриншоты за месяц).
    Запись дня заменяет ТОЛЬКО присланные показатели, остальные не трогает."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)

    action = (params.get("action") or "add").lower()
    from health_core import watch as _watch

    # ── Просмотр записей ──
    if action == "list":
        try:
            limit = min(int(params.get("limit") or 30), 100)
        except (TypeError, ValueError):
            limit = 30
        since_days = params.get("since_days")
        try:
            since_days = int(since_days) if since_days is not None else None
        except (TypeError, ValueError):
            since_days = None
        entries = _watch.list_days(conn, user_id, limit=limit, since_days=since_days)
        conn.close()
        return json.dumps({"entries": entries, "count": len(entries)}, ensure_ascii=False)

    # ── Удаление ошибочной записи ──
    if action == "delete":
        date = params.get("date")
        deleted = _watch.delete_day(conn, user_id, date)
        conn.close()
        if deleted is None:
            msg = "Записей дня с часов нет" if date is None else f"Записи за {date} нет"
            return json.dumps({"error": msg}, ensure_ascii=False)
        return json.dumps({"deleted": {"date": deleted}}, ensure_ascii=False)

    # ── Добавление (по умолчанию) ──
    days = params.get("days")
    if not days:
        conn.close()
        return json.dumps(
            {"error": "Нужен days: [{date, hr_min/hr_avg/hr_max/steps/active_kcal/stress_avg/hrv_ms}] — хотя бы один день"},
            ensure_ascii=False,
        )

    result = _watch.save_days(conn, user_id, days)
    conn.close()
    return json.dumps(result, ensure_ascii=False)


@_handler_wrapper
def handle_council(params: dict) -> str:
    """Консилиум (docs/adr/0003-консилиум.md): честный разбор всех данных
    человека несколькими независимыми моделями в фоне. action=request с
    reason=manual|dose резервирует прогон (лок + частота — bot.council.reserve)
    и ставит его в очередь фонового запуска ботом (bot/main.py), отвечая сразу
    — сама работа занимает минуты и не должна держать ход разговора. action=status
    — последний результат/статус этого человека."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    action = (params.get("action") or "status").lower()

    from bot import council

    if action == "status":
        row = council.latest_run(conn, user_id)
        conn.close()
        if row is None:
            return json.dumps({"status": "none", "text": "Консилиум ещё не созывался."}, ensure_ascii=False)
        return json.dumps(row, ensure_ascii=False)

    if action == "request":
        reason = (params.get("reason") or "").lower()
        if reason not in ("manual", "dose"):
            conn.close()
            return json.dumps({"error": "reason должен быть manual или dose"}, ensure_ascii=False)
        telegram_uid = _caller_telegram_id()
        if not telegram_uid:
            conn.close()
            return json.dumps({"error": "Не удалось определить telegram-пользователя"}, ensure_ascii=False)
        try:
            run_id = council.reserve(conn, user_id, reason)
        except ValueError as e:
            conn.close()
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        conn.close()
        council.queue_run(telegram_uid, user_id, run_id, reason)
        return json.dumps({
            "ok": "Консилиум запущен, итог придёт отдельным сообщением.",
            "run_id": run_id,
        }, ensure_ascii=False)

    conn.close()
    return json.dumps({"error": f"Неизвестное действие: {action}. Допустимо: request, status"},
                      ensure_ascii=False)


@_handler_wrapper
def handle_log_labs(params: dict) -> str:
    """Log lab results with automatic validation and derived calculations."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)

    action = (params.get("action") or "add").lower()

    # ── Delete action ──
    if action == "delete":
        try:
            lab_id = int(params.get("lab_id"))
        except (TypeError, ValueError):
            conn.close()
            return json.dumps(
                {"error": "Нужен lab_id — номер записи анализа"},
                ensure_ascii=False
            )
        from health_core import labs as _labs
        deleted = _labs.delete(conn, user_id, lab_id)
        conn.close()
        if not deleted:
            return json.dumps(
                {"error": f"Записи #{lab_id} нет"},
                ensure_ascii=False
            )
        return json.dumps(
            {"ok": f"Удалена запись анализа #{lab_id}"},
            ensure_ascii=False
        )

    # ── List action ──
    if action == "list":
        limit = int(params.get("limit") or 30)
        if limit < 1:
            limit = 30
        from health_core import labs as _labs
        history = _labs.history(conn, user_id, limit=limit)
        conn.close()
        return json.dumps({"labs": history}, ensure_ascii=False)

    # ── Derived action ──
    if action == "derived":
        from health_core import labs as _labs
        d = _labs.derived(conn, user_id)
        conn.close()
        if not d:
            return json.dumps({
                "derived": {},
                "hint": "Нужны: глюкоза и инсулин в один день; креатинин плюс пол и дата рождения в профиле; общий холестерин и ЛПВП в один день; HbA1c."
            }, ensure_ascii=False)
        return json.dumps({"derived": d}, ensure_ascii=False)

    # ── Add action (default) ──
    if action != "add":
        conn.close()
        return json.dumps(
            {"error": f"Неизвестное действие {action!r}. Допустимо: add, list, delete, derived"},
            ensure_ascii=False
        )

    markers = params.get("markers") or {}
    if not markers:
        conn.close()
        return json.dumps(
            {"error": "Нужны показатели markers: {название: число}"},
            ensure_ascii=False
        )

    taken_on = params.get("taken_on") or _today_iso()
    notes = params.get("notes")

    from health_core import labs as _labs
    try:
        result = _labs.save(conn, user_id, taken_on, markers, notes=notes)
    except ValueError as e:
        conn.close()
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    derived = _labs.derived(conn, user_id)
    conn.close()

    return json.dumps({
        "saved": result["saved"],
        "rejected": result["rejected"],
        "derived": derived
    }, ensure_ascii=False)


@_handler_wrapper
def handle_log_weight(params: dict) -> str:
    """Log body metrics and return delta to previous measurement."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)

    action = (params.get("action") or "add").lower()

    # ── Просмотр записей веса за день / диапазон ──
    if action == "list":
        try:
            limit = min(int(params.get("limit") or 10), 50)
        except (TypeError, ValueError):
            limit = 10
        rows = conn.execute(
            "SELECT id, weight_kg, measured_at FROM body_metrics WHERE user_id=? ORDER BY measured_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        entries = [{"weight_id": r["id"], "weight_kg": r["weight_kg"], "measured_at": r["measured_at"]} for r in rows]
        conn.close()
        return json.dumps({
            "entries": entries,
            "count": len(entries),
        }, ensure_ascii=False)

    # ── Удаление ошибочной записи веса/состава тела ──
    if action == "delete":
        weight_id = params.get("weight_id")

        if weight_id is None:
            # Если weight_id не указан явно, удаляем последнюю запись веса
            row = conn.execute(
                "SELECT id, weight_kg, measured_at FROM body_metrics WHERE user_id=? ORDER BY measured_at DESC, id DESC LIMIT 1",
                (user_id,)
            ).fetchone()
            if row is None:
                conn.close()
                return json.dumps({"error": "Записей веса нет"}, ensure_ascii=False)
            record_id = row["id"]
        else:
            try:
                record_id = int(weight_id)
            except (TypeError, ValueError):
                conn.close()
                return json.dumps({"error": "Нужен weight_id — номер записи веса"}, ensure_ascii=False)
            row = conn.execute(
                "SELECT weight_kg, measured_at FROM body_metrics WHERE id=? AND user_id=?",
                (record_id, user_id)).fetchone()
            if row is None:
                conn.close()
                return json.dumps({"error": f"Записи #{record_id} нет"}, ensure_ascii=False)

        conn.execute("DELETE FROM body_metrics WHERE id=? AND user_id=?", (record_id, user_id))
        conn.commit()
        conn.close()
        return json.dumps({
            "deleted": {"weight_id": record_id, "weight_kg": row["weight_kg"], "measured_at": row["measured_at"]},
            "id": record_id,
        }, ensure_ascii=False)

    weight_kg = params.get("weight_kg")
    measured_at = _norm_event_ts(params.get("measured_at"), conn, user_id) or _now_iso(conn, user_id)

    # weight_kg REAL NOT NULL в body_metrics отказывает на None, но не на мусорную
    # строку — SQLite хранит TEXT в REAL-колонке как есть, и она тихо всплывает
    # только при вычитании дельты НИЖЕ, уже после INSERT. Гейт — до вставки.
    try:
        weight_kg = _as_float("weight_kg", weight_kg)
    except ValueError as e:
        conn.close()
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    # Normalize timestamp
    if "T" in measured_at and measured_at.count(":") == 1:
        measured_at += ":00"

    # Get previous weight for delta
    prev = conn.execute(
        "SELECT weight_kg FROM body_metrics WHERE user_id=? AND measured_at < ? "
        "ORDER BY measured_at DESC LIMIT 1",
        (user_id, measured_at),
    ).fetchone()

    # Generate burst key from timestamp
    burst_key = measured_at

    # Build values dict, including only non-None fields
    insert_fields = ["user_id", "burst_key", "measured_at", "weight_kg"]
    insert_values = [user_id, burst_key, measured_at, weight_kg]

    numeric_fields = [
        "fat_pct", "bmi", "skeletal_muscle_pct", "muscle_mass_kg",
        "protein_pct", "device_bmr_kcal", "ffm_kg", "subcutaneous_fat_pct",
        "visceral_fat", "water_pct", "bone_mass_kg", "metabolic_age"
    ]
    for field in numeric_fields:
        if field in params and params[field] is not None:
            try:
                val = _as_float(field, params[field])
            except ValueError as e:
                conn.close()
                return json.dumps({"error": str(e)}, ensure_ascii=False)
            insert_fields.append(field)
            insert_values.append(val)

    if "device_mac" in params and params["device_mac"]:
        insert_fields.append("device_mac")
        insert_values.append(params["device_mac"])

    # Insert body metrics
    placeholders = ", ".join("?" * len(insert_values))
    sql = f"INSERT INTO body_metrics({', '.join(insert_fields)}) VALUES ({placeholders}) ON CONFLICT(user_id, burst_key) DO NOTHING"
    cur = conn.execute(sql, insert_values)
    weight_id = cur.lastrowid
    conn.commit()

    # Check guards and record alerts
    alerts = check_all(conn, user_id)
    record(conn, user_id, alerts)

    delta = None
    if prev:
        delta = round(weight_kg - prev["weight_kg"], 2)

    # Взвешивание — единственный момент, когда веха по весу/составу может
    # закрыться. Раз закрытую веху объявляем один раз: повторный вызов вернёт
    # пустой список, потому что achieved_at уже проставлен.
    achieved = mark_achieved_milestones(conn, user_id)

    conn.close()

    return json.dumps(
        {
            "weight_id": weight_id,
            "weight_kg": weight_kg,
            "delta_kg": delta,
            "alerts": alerts,
            "achieved_milestones": achieved,
        },
        ensure_ascii=False,
    )


@_handler_wrapper
def handle_log_anthropometry(params: dict) -> str:
    """Log anthropometry measurement and return delta to previous."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)

    action = (params.get("action") or "add").lower()

    # ── Просмотр записей антропометрии ──
    if action == "list":
        try:
            limit = min(int(params.get("limit") or 10), 50)
        except (TypeError, ValueError):
            limit = 10
        rows = conn.execute(
            "SELECT id, site, value_cm, measured_on FROM anthropometry WHERE user_id=? ORDER BY measured_on DESC, site LIMIT ?",
            (user_id, limit),
        ).fetchall()
        entries = [{"anthropometry_id": r["id"], "site": r["site"], "value_cm": r["value_cm"], "measured_on": r["measured_on"]} for r in rows]
        conn.close()
        return json.dumps({
            "entries": entries,
            "count": len(entries),
        }, ensure_ascii=False)

    # ── Удаление ошибочной записи антропометрии ──
    if action == "delete":
        anthropometry_id = params.get("anthropometry_id")

        if anthropometry_id is None:
            # Если anthropometry_id не указан явно, удаляем последнюю запись антропометрии
            row = conn.execute(
                "SELECT id, site, value_cm, measured_on FROM anthropometry WHERE user_id=? ORDER BY measured_on DESC, id DESC LIMIT 1",
                (user_id,)
            ).fetchone()
            if row is None:
                conn.close()
                return json.dumps({"error": "Записей антропометрии нет"}, ensure_ascii=False)
            record_id = row["id"]
        else:
            try:
                record_id = int(anthropometry_id)
            except (TypeError, ValueError):
                conn.close()
                return json.dumps({"error": "Нужен anthropometry_id — номер записи замера"}, ensure_ascii=False)
            row = conn.execute(
                "SELECT site, value_cm, measured_on FROM anthropometry WHERE id=? AND user_id=?",
                (record_id, user_id)).fetchone()
            if row is None:
                conn.close()
                return json.dumps({"error": f"Записи #{record_id} нет"}, ensure_ascii=False)

        conn.execute("DELETE FROM anthropometry WHERE id=? AND user_id=?", (record_id, user_id))
        conn.commit()
        conn.close()
        return json.dumps({
            "deleted": {"anthropometry_id": record_id, "site": row["site"], "value_cm": row["value_cm"], "measured_on": row["measured_on"]},
            "id": record_id,
        }, ensure_ascii=False)

    site = params.get("site")
    value_cm = params.get("value_cm")
    measured_on = params.get("measured_on") or _today_iso(conn, user_id)

    # site — реальный гейт, не только enum в схеме: report.whr()/trends() ищут
    # ЛИТЕРАЛЬНО site='талия'/'таз' (health_core/ingest/anthro.SITES). Английское
    # значение прошло бы мимо этой проверки раньше — запись легла бы в таблицу,
    # но Т/Б и тренд талии её никогда бы не увидели. Молча.
    if site not in _ANTHRO_SITES:
        conn.close()
        return json.dumps(
            {"error": f"site обязателен и должен быть одним из {sorted(_ANTHRO_SITES)}, получено {site!r}"},
            ensure_ascii=False)
    try:
        value_cm = _as_float("value_cm", value_cm)
    except ValueError as e:
        conn.close()
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    # Get previous measurement
    prev = conn.execute(
        "SELECT value_cm FROM anthropometry WHERE user_id=? AND site=? AND measured_on < ? "
        "ORDER BY measured_on DESC LIMIT 1",
        (user_id, site, measured_on),
    ).fetchone()

    # Insert anthropometry
    cur = conn.execute(
        "INSERT INTO anthropometry(user_id, measured_on, site, value_cm) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, measured_on, site) DO NOTHING",
        (user_id, measured_on, site, value_cm),
    )
    anthropometry_id = cur.lastrowid
    conn.commit()

    # Check guards and record alerts
    alerts = check_all(conn, user_id)
    record(conn, user_id, alerts)
    achieved = mark_achieved_milestones(conn, user_id)

    delta = None
    if prev:
        delta = round(value_cm - prev["value_cm"], 2)

    conn.close()

    return json.dumps(
        {
            "anthropometry_id": anthropometry_id,
            "site": site,
            "value_cm": value_cm,
            "delta_cm": delta,
            "alerts": alerts,
            # Замер талии закрывает вехи по метрике waist — тот же разовый анонс,
            # что и при взвешивании.
            "achieved_milestones": achieved,
        },
        ensure_ascii=False,
    )


_MED_ROUTES = {"injection", "oral", "topical"}
_MED_UNITS = {"mg", "ml", "IU", "mcg"}

_METRIC_MAP = {
    "weight": "weight_kg", "fat": "fat_pct", "muscle_mass": "muscle_mass_kg",
    "visceral_fat": "visceral_fat", "ffm": "ffm_kg", "bmi": "bmi",
}


@_handler_wrapper
def handle_log_med(params: dict) -> str:
    """Log medication intake and return confirmation."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)

    action = (params.get("action") or "add").lower()

    # ── Просмотр записей препаратов ──
    if action == "list":
        try:
            limit = min(int(params.get("limit") or 10), 50)
        except (TypeError, ValueError):
            limit = 10
        rows = conn.execute(
            "SELECT id, substance, dose, at FROM med_log WHERE user_id=? ORDER BY at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        entries = [{"med_id": r["id"], "substance": r["substance"], "dose": r["dose"], "at": r["at"]} for r in rows]
        conn.close()
        return json.dumps({
            "entries": entries,
            "count": len(entries),
        }, ensure_ascii=False)

    # ── Удаление ошибочной записи препарата/инъекции ──
    if action == "delete":
        med_id = params.get("med_id")

        if med_id is None:
            # Если med_id не указан явно, удаляем последнюю запись препарата
            row = conn.execute(
                "SELECT id, substance, dose, at FROM med_log WHERE user_id=? ORDER BY at DESC, id DESC LIMIT 1",
                (user_id,)
            ).fetchone()
            if row is None:
                conn.close()
                return json.dumps({"error": "Записей препаратов нет"}, ensure_ascii=False)
            record_id = row["id"]
        else:
            try:
                record_id = int(med_id)
            except (TypeError, ValueError):
                conn.close()
                return json.dumps({"error": "Нужен med_id — номер записи препарата"}, ensure_ascii=False)
            row = conn.execute(
                "SELECT substance, dose, at FROM med_log WHERE id=? AND user_id=?",
                (record_id, user_id)).fetchone()
            if row is None:
                conn.close()
                return json.dumps({"error": f"Записи #{record_id} нет"}, ensure_ascii=False)

        conn.execute("DELETE FROM med_log WHERE id=? AND user_id=?", (record_id, user_id))
        conn.commit()
        conn.close()
        return json.dumps({
            "deleted": {"med_id": record_id, "substance": row["substance"], "dose": row["dose"], "at": row["at"]},
            "id": record_id,
        }, ensure_ascii=False)

    # Бренд и МНН — один препарат: без сведения имени доза, записанная как
    # «Тирзетта», не сдвинет расписание «Тирзепатид», и оно останется висеть
    # просроченным. См. health_core/meds.py.
    drug = _canon_med(params.get("drug")) or None
    dose = params.get("dose")
    route = params.get("route")
    unit = params.get("unit")
    site = params.get("site")
    notes = params.get("notes")
    at = _norm_event_ts(params.get("at"), conn, user_id) or _now_iso(conn, user_id)

    # Normalize timestamp
    if "T" in at and at.count(":") == 1:
        at += ":00"

    # substance/dose в med_log — TEXT без NOT NULL: без этого гейта пустое имя
    # препарата проходит молча и оседает в med_log бесполезной строкой.
    if not drug or not str(drug).strip():
        conn.close()
        return json.dumps({"error": "drug обязателен: без названия препарата запись бессмысленна"},
                          ensure_ascii=False)
    if not dose or not str(dose).strip():
        conn.close()
        return json.dumps({"error": "dose обязателен: без дозировки запись бессмысленна"},
                          ensure_ascii=False)

    # route обязателен для новых записей — LIPID_GUARD и любой будущий гардрейл
    # по пути введения читают эту колонку буквально, без эвристик по тексту.
    # Неизвестное значение отклоняем явной ошибкой, а не тихо пишем как есть.
    if route not in _MED_ROUTES:
        conn.close()
        raise ValueError(
            f"route обязателен и должен быть одним из {sorted(_MED_ROUTES)}, получено {route!r}"
        )
    if unit is not None and unit not in _MED_UNITS:
        conn.close()
        raise ValueError(f"unit должен быть одним из {sorted(_MED_UNITS)}, получено {unit!r}")

    # Insert med log
    cur = conn.execute(
        "INSERT INTO med_log(user_id, at, substance, dose, route, unit, site, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, at, drug, dose, route, unit, site, notes),
    )
    med_id = cur.lastrowid
    conn.commit()

    # Есть расписание по этому препарату — списываем дозу из остатка и двигаем
    # следующую дозу на каденцию. «Учёт остатков»: факт приёма уменьшает запас.
    sched = conn.execute(
        "SELECT id, every_days, next_at, stock_doses, dose FROM med_schedule WHERE user_id=? AND substance=?",
        (user_id, drug),
    ).fetchone()
    dose_differs = False
    if sched is not None:
        new_stock = None if sched["stock_doses"] is None else max(0.0, sched["stock_doses"] - 1)
        new_next = sched["next_at"]
        if sched["every_days"]:
            new_next = _advance_next(sched["next_at"] or at, sched["every_days"])
        conn.execute("UPDATE med_schedule SET stock_doses=?, next_at=?, updated_at=? WHERE id=?",
                     (new_stock, new_next, _now_iso(), sched["id"]))
        conn.commit()
        # Фактический приём записывается всегда; несовпадение с расписанием —
        # просто пометка в ответе, не отказ (docs/adr/0002).
        actual = _dose_num(dose)
        if sched["dose"] is not None and actual is not None and abs(actual - sched["dose"]) > 1e-6:
            dose_differs = True

    # Check guards and record alerts
    alerts = check_all(conn, user_id)
    record(conn, user_id, alerts)

    conn.close()

    resp = {
        "med_id": med_id,
        "confirmed": True,
        "drug": drug,
        "dose": dose,
        "route": route,
        "alerts": alerts,
    }
    if dose_differs:
        resp["dose_differs_from_schedule"] = True
    return json.dumps(resp, ensure_ascii=False)


@_handler_wrapper
def handle_log_workout(params: dict) -> str:
    """Записать тренировку/активность, удалить ошибочную запись или посмотреть историю."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    action = (params.get("action") or "add").lower()

    if action == "list":
        try:
            limit = min(int(params.get("limit") or 10), 50)
        except (TypeError, ValueError):
            limit = 10
        rows = conn.execute(
            "SELECT id, started_at, sport, duration_min, kcal, avg_hr, notes, source FROM activity "
            "WHERE user_id=? ORDER BY started_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        conn.close()
        return json.dumps({
            "workouts": [dict(r) for r in rows],
            "count": len(rows),
        }, ensure_ascii=False)

    if action == "delete":
        workout_id = params.get("workout_id")
        try:
            record_id = int(workout_id)
        except (TypeError, ValueError):
            conn.close()
            return json.dumps({"error": "Нужен workout_id — номер записи тренировки"}, ensure_ascii=False)
        row = conn.execute(
            "SELECT id, started_at, sport, duration_min, kcal, file_hash FROM activity WHERE id=? AND user_id=?",
            (record_id, user_id),
        ).fetchone()
        if row is None:
            conn.close()
            return json.dumps({"error": f"Тренировки #{record_id} нет в базе"}, ensure_ascii=False)
        conn.execute("DELETE FROM activity WHERE id=? AND user_id=?", (record_id, user_id))
        if row["file_hash"]:
            conn.execute("DELETE FROM import_log WHERE file_hash=?", (row["file_hash"],))
        conn.commit()
        conn.close()
        return json.dumps({
            "ok": f"Тренировка #{record_id} ({row['sport'] or 'активность'} от {row['started_at']}) удалена",
            "deleted": dict(row),
        }, ensure_ascii=False)

    # ── action == "add" ──
    sport = (params.get("sport") or "Тренировка").strip()
    duration_min = params.get("duration_min")
    if duration_min is not None:
        try:
            duration_min = float(duration_min)
            if duration_min <= 0:
                duration_min = None
        except (TypeError, ValueError):
            duration_min = None

    kcal = params.get("kcal")
    if kcal is not None:
        try:
            kcal = float(kcal)
            if kcal < 0:
                kcal = None
        except (TypeError, ValueError):
            kcal = None

    avg_hr = params.get("avg_hr")
    if avg_hr is not None:
        try:
            avg_hr = int(float(avg_hr))
            if avg_hr <= 0:
                avg_hr = None
        except (TypeError, ValueError):
            avg_hr = None

    started_at = _norm_event_ts(params.get("started_at"), conn, user_id) or _now_iso(conn, user_id)
    notes = (params.get("notes") or "").strip() or None
    source = params.get("source") or "manual"

    # Синтетический hash для ручных записей (гарантия уникальности и соответствия DDL)
    import hashlib
    file_hash = hashlib.sha256(f"manual_{user_id}_{started_at}_{sport}_{duration_min}".encode()).hexdigest()

    cur = conn.execute(
        "INSERT INTO activity(user_id, started_at, duration_min, kcal, avg_hr, sport, file_hash, notes, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, started_at, duration_min, kcal, avg_hr, sport, file_hash, notes, source),
    )
    new_id = cur.lastrowid
    conn.commit()

    # Add heart rate zone info if avg_hr is provided
    hr_zone = None
    hr_zones_info = None
    if avg_hr is not None:
        try:
            from health_core import hr_zones as _hz
            z = _hz.zones(conn, user_id, started_at[:10])
            if z:
                hr_zone = _hz.classify(avg_hr, z)
                hr_zones_info = z
        except Exception:
            pass

    alerts = check_all(conn, user_id)
    record(conn, user_id, alerts)
    conn.close()

    result = {
        "ok": f"Тренировка записана: {sport}, {duration_min or '—'} мин, {kcal or '—'} ккал (#{new_id})",
        "workout_id": new_id,
        "sport": sport,
        "duration_min": duration_min,
        "kcal": kcal,
        "avg_hr": avg_hr,
        "started_at": started_at,
        "alerts": alerts,
    }
    if hr_zone:
        result["hr_zone"] = hr_zone
    if hr_zones_info:
        result["hr_zones"] = hr_zones_info
    return json.dumps(result, ensure_ascii=False)


_MAX_IMPORT_BYTES = 20 * 1024 * 1024  # 20 МБ — жёсткий отказ, файл больше в память не грузим


def _sniff_import_format(head: bytes) -> str:
    """Определяет реальный формат по магическим байтам заголовка файла, а не по
    расширению пути — расширение лжёт в проде (настоящий экспорт весов называется
    *.xlsx.xls, но содержимое — обычный OOXML zip)."""
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "xlsx"  # OOXML zip: .xlsx или .xls с обманным расширением
    stripped = head.lstrip(b"\xef\xbb\xbf").lstrip()  # BOM + пробелы
    if stripped[:5] == b"<?xml" or b"TrainingCenterDatabase" in head:
        return "tcx"
    return "csv"  # текстовый экспорт весов по умолчанию


def _dispatch_import(conn, user_id: int, file_path: str) -> dict:
    """Sniff формата по содержимому + вызов РЕАЛЬНОГО парсера из health_core.ingest
    (scale.import_export / tcx.import_tcx) — второй парсер здесь не пишем.

    scale.py выбирает CSV- или xlsx-ветку по подстроке ".csv" в конце пути, а не по
    содержимому. Если sniff и расширение расходятся, копируем байты во временный
    файл с верным суффиксом — контент не меняется, значит file_hash (и идемпотентность
    через import_log) остаются теми же, что и для оригинального пути."""
    if not os.path.exists(file_path):
        raise ValueError(f"Файл не найден: {file_path}")
    size = os.path.getsize(file_path)
    if size > _MAX_IMPORT_BYTES:
        raise ValueError(f"Файл {size / 1024 / 1024:.1f} МБ больше лимита 20 МБ, не читаем")
    with open(file_path, "rb") as f:
        head = f.read(4096)

    fmt = _sniff_import_format(head)

    if fmt == "tcx":
        return import_tcx(conn, user_id, file_path)

    is_csv_ext = file_path.lower().endswith(".csv")
    use_path = file_path
    tmp_path = None
    if fmt == "csv" and not is_csv_ext:
        fd, tmp_path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        shutil.copyfile(file_path, tmp_path)
        use_path = tmp_path
    elif fmt == "xlsx" and is_csv_ext:
        fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        shutil.copyfile(file_path, tmp_path)
        use_path = tmp_path
    try:
        result = import_export(conn, user_id, use_path)
    finally:
        if tmp_path:
            os.remove(tmp_path)
    return result


@_handler_wrapper
def handle_import_scale_export(params: dict) -> str:
    """Import a health-data file (xlsx/xls/csv scale export or tcx workout) and return stats."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    file_path = params.get("file_path")

    if not file_path:
        conn.close()
        raise ValueError("file_path обязателен")

    # 'gdrive:Health/Scale/export.xlsx' — сначала скачиваем во временный каталог.
    # Это не чтение витрины назад (§13), а забор ИСХОДНИКА: выгрузки весов и .tcx
    # лежат на Диске, потому что туда их кладёт телефон, а не наш экспорт.
    pulled_dir = None
    if is_remote_path(file_path):
        pulled_dir = tempfile.mkdtemp(prefix="health_pull_")
        try:
            file_path = rclone_pull(file_path, pulled_dir)
        except Exception:
            shutil.rmtree(pulled_dir, ignore_errors=True)
            conn.close()
            raise

    # Import the file — dispatch by content (magic bytes), not extension
    try:
        result = _dispatch_import(conn, user_id, file_path)
    finally:
        if pulled_dir:
            shutil.rmtree(pulled_dir, ignore_errors=True)

    # Check guards and record alerts
    alerts = check_all(conn, user_id)
    record(conn, user_id, alerts)

    conn.close()

    # added/skipped всегда явные числа в ответе — молчаливое "готово" на нулевом
    # импорте уже дважды приводило к потерянным данным в этом проекте.
    return json.dumps(
        {
            "added": result.get("added", 0),
            "skipped": result.get("skipped", 0),
            "bursts": result.get("bursts", 0),
            "alerts": alerts,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------- READ TOOLS

_MEAL_LABELS = {
    "breakfast": ("🍳", "Завтрак"),
    "lunch": ("🍲", "Обед"),
    "dinner": ("🍛", "Ужин"),
    "snack": ("🍎", "Перекус"),
}


def _meal_labels_for_day(meals: list[dict]) -> list[tuple[str, str]]:
    """Ярлык каждому приёму дня, в порядке meals_of_day. Слот приходит из базы —
    его пишет log_food: по прямому слову человека или, если слова не было,
    определяет health_core.chrono.meal_slot по окнам приёма пищи. Часы и
    калории здесь ни при чём — правило живёт в chrono.meal_slot, а не тут.

    Перекусы нумеруются: два «Перекуса» за день иначе неразличимы в журнале.
    None приходит только от строк старше schema v5, когда колонки ещё не было.
    """
    out, snack_n = [], 0
    for m in meals:
        if m["meal_slot"] == "snack":
            snack_n += 1
            emoji, label = _MEAL_LABELS["snack"]
            out.append((emoji, f"{label} {snack_n}"))
        else:
            out.append(_MEAL_LABELS.get(m["meal_slot"], ("🍽", "Приём")))
    return out


_CONSOLE_W = 34  # ширина под телефон: моноширинный блок в Telegram не переносится
_SPARK = "▁▂▃▄▅▆▇█"


def _sparkline(vals: list[float]) -> str:
    """Однострочный трендовый спарклайн из блочных символов. Пусто — пусто."""
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return _SPARK[0] * len(vals)
    return "".join(_SPARK[round((v - lo) / (hi - lo) * (len(_SPARK) - 1))] for v in vals)


def _wide_bar(value: float, target: float, cells: int = 10) -> str:
    """Бар на `cells` клеток + процент, для строк бюджета КБЖУ в консоли."""
    frac = max(0.0, min(1.0, value / target)) if target else 0.0
    filled = round(frac * cells)
    pct = round(value / target * 100) if target else 0
    return "█" * filled + "░" * (cells - filled) + f" {pct:>3}%"


def _daily_weights(conn, user_id: int, days: int = 14) -> list[tuple[str, float]]:
    """(iso-день, вес) — последнее взвешивание за каждый из последних `days` дней,
    по возрастанию даты. Для спарклайна и дневной дельты."""
    rows = conn.execute(
        "SELECT date(measured_at) d, weight_kg w FROM body_metrics "
        "WHERE user_id=? AND weight_kg IS NOT NULL "
        "GROUP BY date(measured_at) HAVING measured_at=MAX(measured_at) "
        "ORDER BY d DESC LIMIT ?",
        (user_id, days),
    ).fetchall()
    return [(r["d"], r["w"]) for r in reversed(rows)]


def _render_console(conn, user_id: int, date_str: str) -> str:
    """Единый метаболический пульт под телефон (~34 симв): тело, тренд веса,
    бюджет КБЖУ/вода, лог еды, гарды. Ничего не считает заново — собирает из
    day_summary/meals_of_day/guards/bmr_floor и последней строки body_metrics.
    Пустые секции (нет фармы, нет плана) показываются честно, не выдумываются."""
    rule = "━" * _CONSOLE_W
    now = config.user_now(conn, user_id)
    m = conn.execute(
        "SELECT weight_kg, fat_pct, bmi, muscle_mass_kg, ffm_kg, visceral_fat, water_pct, metabolic_age "
        "FROM body_metrics WHERE user_id=? ORDER BY measured_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    lines = [f"📊 МЕТАБОЛ. ПУЛЬТ — {now.strftime('%d.%m %H:%M')}", rule]

    # ① ТЕЛО
    if m is not None:
        base = _base_weight(conn, user_id)
        weights = _daily_weights(conn, user_id)
        day_delta = None
        if len(weights) >= 2:
            day_delta = weights[-1][1] - weights[-2][1]
        body = [("Вес", f"{m['weight_kg']:.1f} кг")]
        if day_delta is not None:
            body.append(("Δ день", _signed(day_delta, 1, "кг")))
        if base is not None:
            body.append(("Δ старт", _signed(m["weight_kg"] - base, 1, "кг")))
        if m["fat_pct"]:
            body.append(("Жир", f"{m['fat_pct']:.1f} %"))
        if m["muscle_mass_kg"]:
            body.append(("Мышцы", f"{m['muscle_mass_kg']:.1f} кг"))
        if m["ffm_kg"]:
            body.append(("Сухая", f"{m['ffm_kg']:.1f} кг"))
        if m["visceral_fat"] is not None:
            body.append(("Висц. жир", f"{m['visceral_fat']:.0f}"))
        if m["water_pct"]:
            body.append(("Вода тела", f"{m['water_pct']:.1f} %"))
        if m["bmi"]:
            body.append(("BMI", f"{m['bmi']:.1f}"))
        try:
            body.append(("BMR", f"{bmr_floor(conn, user_id):.0f} ккал"))
        except ValueError:
            pass
        lines.append("① ТЕЛО")
        lines.extend(_kv_box(body))

        # ② ТРЕНД ВЕСА
        if len(weights) >= 2:
            vals = [w for _, w in weights]
            lines += [rule, f"② ВЕС {len(vals)} дн  {vals[0]:.1f}→{vals[-1]:.1f}",
                      _sparkline(vals)]

    # ③ БЮДЖЕТ КБЖУ / ВОДА
    d = day_summary(conn, user_id, date_str)
    try:
        daily_target(conn, user_id, date_str)
    except ValueError:
        pass
    tgt = conn.execute(
        "SELECT kcal_target, protein_g_target, fat_g_target, carbs_g_target, fiber_g_target, water_ml_target "
        "FROM daily_targets WHERE user_id=? AND date=?",
        (user_id, date_str),
    ).fetchone()
    lines += [rule, "③ КБЖУ / ВОДА"]
    budget = [
        ("Кал", d["kcal_eaten"], tgt and tgt["kcal_target"], 0),
        ("Белок", d["protein_g"], tgt and tgt["protein_g_target"], 0),
        ("Жиры", d["fat_g"], tgt and tgt["fat_g_target"], 0),
        ("Углев", d["carb_g"], tgt and tgt["carbs_g_target"], 0),
    ]
    if d.get("fiber_known"):
        budget.append(("Клетч", d["fiber_g"], tgt and tgt["fiber_g_target"], 0))
    budget.append(("Вода", (d["water_ml"] or 0) / 1000, (tgt["water_ml_target"] / 1000) if tgt and tgt["water_ml_target"] else None, 1))

    lw = max(len(b[0]) for b in budget)
    for name, val, target, dec in budget:
        if target:
            rem = max(0.0, target - val)
            lines.append(f"{name.ljust(lw)} {val:.{dec}f}/{target:.{dec}f} ост {rem:.{dec}f}")
            lines.append(f"{' ' * lw} {_wide_bar(val, target)}")
        else:
            lines.append(f"{name.ljust(lw)} {val:.{dec}f}")

    if not d.get("fiber_known"):
        lines.append("Клетч — нет данных")

    # ④ ЛОГ ЕДЫ
    meals = meals_of_day(conn, user_id, date_str)
    if meals:
        lines += [rule, "④ ЛОГ ЕДЫ"]
        for mm, (emoji, label) in zip(meals, _meal_labels_for_day(meals)):
            clock = (mm["eaten_at"] or "")[11:16]
            names = ", ".join(i.get("name") or "Блюдо" for i in mm["items"]) or "—"
            lines.append(f"{emoji} {clock} {label} · {mm['kcal']:.0f} ккал")
            lines.extend("  " + w for w in textwrap.wrap(names, width=_CONSOLE_W - 2) or ["  —"])

    # ⑤ ГАРДЫ / ФАРМА
    fired = check_all(conn, user_id)
    lines += [rule, "⑤ ГАРДЫ / ФАРМА"]
    if fired:
        seen = set()
        for a in fired:
            if a["code"] in seen:
                continue
            seen.add(a["code"])
            lines.extend("⚠ " + w for w in textwrap.wrap(a["message"], width=_CONSOLE_W - 2))
    else:
        lines.append("✓ Все гарды в норме")
    sched = conn.execute(
        "SELECT substance, dose, unit, next_at, stock_doses FROM med_schedule WHERE user_id=? "
        "ORDER BY next_at IS NULL, next_at",
        (user_id,),
    ).fetchall()
    if sched:
        for s in sched:
            dose = f"{s['dose']:g}{s['unit'] or ''}" if s["dose"] is not None else ""
            lines.append(f"💊 {s['substance']} {dose}")
            if s["next_at"]:
                lines.append(f"  → {_pharma_due(s['next_at'])}")
            if s["stock_doses"] is not None:
                warn = " ⚠" if s["stock_doses"] <= 2 else ""
                lines.append(f"  ост {s['stock_doses']:g} доз{warn}")
    else:
        med = conn.execute(
            "SELECT at, substance, dose, unit FROM med_log WHERE user_id=? ORDER BY at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if med is not None:
            dose = f"{med['dose']}{med['unit'] or ''}" if med["dose"] is not None else ""
            lines.append(f"💊 {med['substance']} {dose} ({med['at'][:10]})")
        else:
            lines.append("💊 Фарма: нет записей")

    # Сон: средняя длительность за неделю. Стадии сознательно не показываем —
    # бытовой браслет их оценивает ненадёжно, а цифра в отчёте выглядит фактом.
    slp = conn.execute(
        "SELECT duration_min, efficiency_pct FROM sleep_log WHERE user_id=? "
        "ORDER BY night_date DESC LIMIT 7",
        (user_id,),
    ).fetchall()
    if slp:
        avg = round(sum(r["duration_min"] for r in slp) / len(slp))
        last = slp[0]["duration_min"]
        eff = slp[0]["efficiency_pct"]
        eff_s = f" · эфф {eff:.0f}%" if eff else ""
        lines.append(f"😴 Сон {last // 60}ч{last % 60:02d}{eff_s} · среднее за {len(slp)} ноч. "
                     f"{avg // 60}ч{avg % 60:02d}")

    return "```\n" + "\n".join(lines) + "\n```"


def _kv_box(rows: list[tuple[str, str]]) -> list[str]:
    """Двухколоночная таблица ключ-значение в рамке, ширины от данных."""
    kw = max(len(r[0]) for r in rows)
    vw = max(len(r[1]) for r in rows)

    def _sep(l, m, r):
        return l + "─" * (kw + 2) + m + "─" * (vw + 2) + r

    out = [_sep("┌", "┬", "┐")]
    for k, v in rows:
        out.append(f"│ {k.ljust(kw)} │ {v.ljust(vw)} │")
    out.append(_sep("└", "┴", "┘"))
    return out


_JOURNAL_SEP = "───────────────"


def _render_food_log(conn, user_id: int, date_str: str) -> str:
    """Журнал питания: список конкретно съеденного, разбитый по приёмам пищи, с
    граммовкой (если модель её записала) и КБЖУ каждого продукта + подытог приёма,
    затем прогресс дня по всем 4 нутриентам. Раньше «покажи еду» отдавала только
    сухие суммарные цифры — детализации по продуктам и приёмам не было.
    Markdown, не моноширинный блок — 40-символьный забор не вмещает иконки и
    статус-колонку, которые попросил продакт-оун."""
    meals = meals_of_day(conn, user_id, date_str)
    day_label = datetime.strptime(date_str[:10], "%Y-%m-%d").strftime("%d.%m")
    blocks = [f"🍽 **Журнал питания — {day_label}**"]
    if not meals:
        blocks.append("Пусто — за день ничего не записано.")
        metrics_line = _metrics_line(conn, user_id)
        if metrics_line is not None:
            blocks.append(metrics_line)
        return "\n\n".join(blocks)

    for m, (emoji, label) in zip(meals, _meal_labels_for_day(meals)):
        eaten = m["eaten_at"] or ""
        clock = eaten[11:16] if len(eaten) >= 16 else ""
        # #N — то, чем адресуется удаление (log_food action=delete food_log_id=N).
        # Без него ошибочную запись нечем назвать.
        header = (f"**{emoji} {label}**" + (f" · {clock}" if clock else "")
                  + f" · **{m['kcal']:.0f} ккал** · `#{m['food_log_id']}`")
        item_lines = []
        for it in m["items"]:
            grams = f" — {it['grams']:.0f} г" if it.get("grams") else ""
            item_lines.append(f"• {it.get('name') or 'Блюдо'}{grams} · {(it['kcal'] or 0.0):.0f} ккал")
        if not m["items"]:
            item_lines.append("• (пусто)")
        macro_line = f"_Б {m['protein_g']:.0f} · Ж {m['fat_g']:.0f} · У {m['carbs_g']:.0f}_"
        blocks.append("\n".join([header] + item_lines + [macro_line]))
        blocks.append(_JOURNAL_SEP)  # после последнего приёма — разделитель перед ИТОГО

    water_rows = conn.execute(
        "SELECT id, volume_ml, at FROM water_log WHERE user_id=? AND date(at)=? ORDER BY at ASC",
        (user_id, date_str),
    ).fetchall()
    if water_rows:
        water_lines = ["💧 **Вода за день:**"]
        for wr in water_rows:
            w_clock = (wr["at"] or "")[11:16]
            water_lines.append(f"• {w_clock} — {wr['volume_ml']:.0f} мл (id: {wr['id']})")
        blocks.append("\n".join(water_lines))
        blocks.append(_JOURNAL_SEP)

    d = day_summary(conn, user_id, date_str)
    try:
        # апсертит daily_targets (в т.ч. жир/углеводы) — как в _render_day_dashboard.
        daily_target(conn, user_id, date_str)
    except ValueError:
        pass
    tgt = conn.execute(
        "SELECT kcal_target, protein_g_target, fat_g_target, carbs_g_target, fiber_g_target "
        "FROM daily_targets WHERE user_id=? AND date=?",
        (user_id, date_str),
    ).fetchone()
    blocks.append(f"**ИТОГО ЗА СУТКИ · {d['kcal_eaten']:.0f} ккал**")

    prog = [
        ("Калории", d["kcal_eaten"], tgt["kcal_target"] if tgt else None, "ккал"),
        ("Белок", d["protein_g"], tgt["protein_g_target"] if tgt else None, "г"),
        ("Жиры", d["fat_g"], tgt["fat_g_target"] if tgt else None, "г"),
        ("Углеводы", d["carb_g"], tgt["carbs_g_target"] if tgt else None, "г"),
    ]
    if d.get("fiber_known"):
        prog.append(("Клетчатка", d["fiber_g"], tgt["fiber_g_target"] if tgt else None, "г"))

    nutrient_lines = []
    for name, val, target, unit in prog:
        icon = _NUTRIENT_ICONS[name]
        if target:
            nutrient_lines.append(
                f"{icon} {name} {val:.0f}/{target:.0f} {unit} · {_dash_bar(val, target)} · "
                f"{_nutrient_status(val, target, name in _FLOOR_ONLY)}"
            )
        else:
            nutrient_lines.append(f"{icon} {name} {val:.0f} {unit}")

    if not d.get("fiber_known"):
        nutrient_lines.append(f"{_NUTRIENT_ICONS['Клетчатка']} Клетчатка — нет данных")

    blocks.append("\n".join(nutrient_lines))

    metrics_line = _metrics_line(conn, user_id)
    if metrics_line is not None:
        blocks.append(metrics_line)

    return "\n\n".join(blocks)


@_handler_wrapper
def handle_get_day_summary(params: dict) -> str:
    """Get daily summary with КБЖУ, water, remaining calories, and plate balance.
    format="bar" (default до этой правки) отдаёт исходный многополевой payload без
    изменений — байт в байт. format="dashboard" (умолчание теперь) заменяет его
    визуальной сводкой под одним ключом "day_summary", тем же контрактом, что и
    get_status_bar использует для "status_bar": второй ключ рядом снова дал бы
    модели выбор, а не решению формата быть на стороне инструмента."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    raw_date = params.get("date")
    cur_today = _today_iso(conn, user_id)
    if not raw_date:
        date_str = cur_today
    elif raw_date.startswith(("2024-", "2025-")) and raw_date[5:10] == cur_today[5:10]:
        date_str = cur_today
    else:
        date_str = raw_date

    fmt = params.get("format", "dashboard")
    if fmt == "console":
        console = _render_console(conn, user_id, date_str)
        conn.close()
        return json.dumps({"day_summary": console}, ensure_ascii=False)
    if fmt == "journal":
        journal = _render_food_log(conn, user_id, date_str)
        conn.close()
        return json.dumps({"day_summary": journal}, ensure_ascii=False)
    if fmt == "dashboard":
        dashboard = _render_day_dashboard(conn, user_id, date_str)
        conn.close()
        return json.dumps({"day_summary": dashboard}, ensure_ascii=False)

    # format="bar": исходный payload, ничего не меняем.
    # Get day summary
    d = day_summary(conn, user_id, date_str)

    # Get target
    target_row = conn.execute(
        "SELECT kcal_target, protein_g_target, water_ml_target FROM daily_targets WHERE user_id=? AND date=?",
        (user_id, date_str),
    ).fetchone()

    target = {
        "kcal": target_row["kcal_target"] if target_row else None,
        "protein_g": target_row["protein_g_target"] if target_row else None,
        "water_ml": target_row["water_ml_target"] if target_row else None,
    }

    # Calculate remaining
    remaining_kcal = None
    if target["kcal"] is not None:
        remaining_kcal = round(target["kcal"] - d["kcal_eaten"], 1)

    # Get plate balance
    balance = plate_balance(conn, user_id, date_str)

    conn.close()

    return json.dumps(
        {
            "date": date_str,
            "kcal_eaten": d["kcal_eaten"],
            "protein_g": d["protein_g"],
            "fat_g": d["fat_g"],
            "carb_g": d["carb_g"],
            "water_ml": d["water_ml"],
            "target": target,
            "remaining_kcal": remaining_kcal,
            "plate_balance": balance,
        },
        ensure_ascii=False,
    )


@_handler_wrapper
def handle_get_trends(params: dict) -> str:
    """Get trends: weight, LBM, waist, actual TDEE."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    window_days = params.get("window_days", 30)

    # Get trends
    t = trends(conn, user_id, window_days=window_days)
    # Эффективность тренировок живёт тут же: это тренд, а не суточный показатель,
    # и отдельного инструмента ради одного словаря заводить незачем. Пустой
    # sports={} — честное "TCX ещё не загружали", а не поломка.
    t["training_efficiency"] = training_efficiency(conn, user_id)

    # Add heart rate zones
    try:
        from health_core import hr_zones as _hz
        t["hr_zones"] = _hz.zones(conn, user_id)
    except Exception:
        pass

    # День с часов (CONTEXT.md «День с часов»): пульс/шаги/стресс/HRV, медиана за 28д против прошлых 28д
    from health_core import watch as _watch
    t["daily_watch"] = _watch.trend_block(conn, user_id)

    conn.close()

    return json.dumps(t, ensure_ascii=False)


@_handler_wrapper
def handle_get_status_bar(params: dict) -> str:
    """Get status bar string. Must be byte-identical to report.status_bar().
    format="dashboard" additionally renders a boxed visual summary — the bar itself
    never changes, the dashboard is a pure additional rendering from the same data."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)

    # Get status bar — returned verbatim, no reformatting
    bar = status_bar(conn, user_id)

    # Активный стиль персоны — отдельным полем "style", не внутри status_bar.
    # get_status_bar — единственное место, которое модель вызывает в начале ЛЮБОГО
    # диалога (это прописано в system_promt.md), а Hermes не варьирует системный
    # промпт по Telegram-пользователю (секции промпта не знают личность и заморожены
    # на сессию) — поэтому персональная манера ответа доходит до модели только тут,
    # не как срезание угла, а как единственный доступный канал.
    style = _active_style(conn, user_id)["instruction"]

    # По умолчанию дашборд: на вопрос о состоянии человек ждёт панель, а не строку.
    # Короткий бар остаётся доступен явным format="bar" — им подписывают записи еды.
    fmt = params.get("format", "dashboard")
    if fmt == "dashboard":
        dashboard = _render_dashboard(conn, user_id)
        conn.close()
        # status_bar — ОДИН ключ, и он же status_bar: персона велит приписывать
        # status_bar дословно, второй соседний ключ дал бы модели выбор, и она
        # стабильно выбирала бы короткий бар. style — не про этот выбор, добавляем
        # его тем же контрактом.
        return json.dumps({"status_bar": dashboard, "style": style}, ensure_ascii=False)

    conn.close()

    # Return as JSON string (bar is already a multiline string)
    return json.dumps({"status_bar": bar, "style": style}, ensure_ascii=False)


_DASH_RULE = "━" * 25

# Поля профиля, без которых определённые строки дашборда невозможны — не потому
# что метрику не мерили, а потому что не хватает базы для расчёта (BMR) или
# точки отсчёта (Δ от старта). Ключ — колонка users, значение — как назвать её
# в тексте "Нет в профиле: …".
_PROFILE_FIELD_LABELS = {
    "height_cm": "рост",
    "birth_date": "дата рождения",
    "sex": "пол",
    "base_weight_kg": "стартовый вес",
}


def _base_weight(conn, user_id: int) -> float | None:
    """Точка отсчёта живёт в health_core.report.baseline_weight — здесь только
    делегирование. Своя копия этой логики тут уже была и разошлась с оригиналом:
    профиль побеждал безусловно, и после импорта истории Δ считалась от середины
    пути. Одна реализация на систему, чтобы не разошлась снова."""
    return baseline_weight(conn, user_id)


def _missing_profile_fields(user_row, base_weight=None) -> list[str]:
    """NULL-поля профиля человеком-читаемо. Статическая проверка по users, а не
    по факту, заблокировала ли она конкретную строку — так проще и одинаково
    работает для обоих дашбордов, а сообщение всегда честное (поля правда нет).
    base_weight, если передан не-None, замещает колонку base_weight_kg: точка
    отсчёта берётся из взвешиваний, и повторно её у человека не спрашиваем."""
    row = {f: None for f in _PROFILE_FIELD_LABELS} if user_row is None else user_row
    missing = []
    for field, label in _PROFILE_FIELD_LABELS.items():
        resolved = base_weight if field == "base_weight_kg" and base_weight is not None else row[field]
        if resolved is None:
            missing.append(label)
    return missing


def _missing_profile_lines(missing: list[str]) -> list[str]:
    """Одна строка про пробел в профиле, максимум 38 символов на строку.
    Пусто, если пробелов нет — печатать нечего, и печатать не нужно."""
    if not missing:
        return []
    text = "Нет в профиле: " + ", ".join(missing)
    return [_DASH_RULE] + (textwrap.wrap(text, width=38) or [text])


_NUTRIENT_ICONS = {
    "Калории": "🔥", "Белок": "🥩", "Жиры": "🥑",
    "Углеводы": "🍞", "Клетчатка": "🥬", "Вода": "💧", "Тренировка": "🏋",
}


def _nutrient_status(value: float, target: float | None, over_ok: bool = False) -> str:
    """Итоговый статус нутриента за сутки. Без цели статуса нет — не выдумываем.

    over_ok — цель является нижней границей, а не коридором: вода сверх нормы
    не нарушение, и красный на 115% выпитого учит игнорировать красный там,
    где он значит настоящий перебор калорий.
    """
    if not target:
        return ""
    pct = value / target * 100
    if pct < 90:
        return "⚠️ недобор"
    if pct <= 105 or over_ok:
        return "✅ норма"
    return "🔴 перебор"


# Нутриенты, у которых цель — минимум, а не коридор. Клетчатка здесь же:
# Knowledge/клетчатка.md:189 даёт 25-30 г как порог здоровья и отдельно
# рекомендует 50-60 г при снижении веса, то есть перебор — не нарушение.
_FLOOR_ONLY = ("Вода", "Клетчатка")


def _signed(value: float, decimals: int, unit: str) -> str:
    """Настоящий минус (U+2212) для отрицательных — конвенция report.py (MINUS),
    не ASCII-дефис, который туда подставил бы f-string по умолчанию."""
    sign = MINUS if value < 0 else ""
    return f"{sign}{abs(value):.{decimals}f} {unit}"


def _dash_bar(value: float, target: float, cells: int = 5) -> str:
    """5-клеточный прогресс-бар + процент, напр. '███░░ 64%'. Без цели — 0%."""
    frac = max(0.0, min(1.0, value / target)) if target else 0.0
    filled = round(frac * cells)
    pct = round(value / target * 100) if target else 0
    return "█" * filled + "░" * (cells - filled) + f" {pct}%"


def _dash_bar_frac(frac: float, cells: int = 5) -> str:
    """Бар от ГОТОВОЙ доли 0..1 — для целей, где прогресс не равен факт/цель.
    Похудение идёт вниз: вес 120.5 при цели 120 — это не 100% от цели, а доля
    пройденного пути от стартового веса. Через _dash_bar вышло бы ровно 100%
    в момент, когда цель ещё не достигнута."""
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * cells)
    return "█" * filled + "░" * (cells - filled) + f" {round(frac * 100)}%"


def _goal_progress(baseline, current, target):
    """Доля пути старт → цель, 0..1. Работает в обе стороны: снижение веса и
    набор сухой массы. None, если старта нет или он совпал с целью (делить не на
    что) — тогда прогресс честно не показываем, а не рисуем ноль."""
    if baseline is None or current is None or target is None:
        return None
    span = target - baseline
    if abs(span) < 1e-9:
        return None
    return max(0.0, min(1.0, (current - baseline) / span))


# Метрика вехи -> колонка в body_metrics. Явный словарь, а не подстановка строки
# из БД в SQL: имя метрики приходит из milestones, в запрос идёт только значение
# отсюда. muscle_mass_kg/visceral_fat вех не имеют, но тот же словарь и та же
# _goal_baseline() ниже дают им точку отсчёта для раздела "Состав тела" — незачем
# заводить вторую функцию первого-непустого-значения ради динамики.
_GOAL_METRIC_COLUMNS = {
    "weight_kg": "weight_kg", "ffm_kg": "ffm_kg", "fat_pct": "fat_pct",
    "muscle_mass_kg": "muscle_mass_kg", "visceral_fat": "visceral_fat",
}

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float]) -> str:
    """Блочный спарклайн: высота клетки — относительное положение значения между
    min и max ряда. 0 точек — пустая строка, 1 — одна клетка (сравнивать не с
    чем), плоский ряд (min==max) — ровная линия сверху, а не пустая: значения
    есть, просто без разброса."""
    if not values:
        return ""
    if len(values) == 1:
        return _SPARK_BLOCKS[-1]
    lo, hi = min(values), max(values)
    span = hi - lo
    top = len(_SPARK_BLOCKS) - 1
    if span < 1e-9:
        return _SPARK_BLOCKS[-1] * len(values)
    return "".join(_SPARK_BLOCKS[round((v - lo) / span * top)] for v in values)


def _stride_sample(seq: list, max_points: int) -> list:
    """Прореживание перед отрисовкой: 90 ежедневных точек не должны расползаться
    на несколько строк в Telegram. Берём max_points точек, гарантированно включая
    первую и последнюю — форма тренда важнее внутренних колебаний."""
    if len(seq) <= max_points:
        return seq
    step = (len(seq) - 1) / (max_points - 1)
    idx = sorted({round(i * step) for i in range(max_points)})
    return [seq[i] for i in idx]


def _bmr_for(conn, user_id: int, weight_kg: float | None, ffm_kg: float | None) -> float | None:
    """BMR_floor (energy.bmr_floor) для явно заданных вес/FFM вместо последнего
    замера — нужно посчитать «BMR на старте». Возраст/рост/пол берём ТЕКУЩИЕ
    для обеих точек сравнения: так Δ показывает чистый эффект похудения, не
    смешивая его со старением между первым и последним замером."""
    if weight_kg is None:
        return None
    user = conn.execute(
        "SELECT height_cm, birth_date, sex FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if user is None or user["height_cm"] is None or user["birth_date"] is None:
        return None
    age = config.local_now().year - date.fromisoformat(user["birth_date"][:10]).year
    ffm = ffm_kg or weight_kg
    return max(bmr_katch(ffm), bmr_mifflin(weight_kg, user["height_cm"], age, user["sex"] or "m"))


def _latest_body_fields(conn, user_id: int, fields: tuple[str, ...]) -> dict | None:
    """Последнее НЕПУСТОЕ значение по каждому полю отдельно, а не последняя строка
    целиком. Ручное взвешивание несёт только вес, и если брать строку целиком,
    свежий вес затирает состав тела, снятый импедансом днём раньше. Состав тела
    не исчезает от того, что человек встал на весы без замера импеданса."""
    metric = {}
    for f in fields:
        row = conn.execute(
            f"SELECT {f} AS v FROM body_metrics WHERE user_id=? AND {f} IS NOT NULL "
            f"ORDER BY measured_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        metric[f] = row["v"] if row else None
    if not any(v is not None for v in metric.values()):
        return None
    return metric


def _metrics_line(conn, user_id: int) -> str | None:
    """Обязательная строка метрик тела для журнала питания и итога дня — формат
    задан продакт-оуном буквально: [W: ... | Δ_total: ... | M: ... | LBM: ... |
    V_fat: ... | BMR: ...], плюс эмодзи-метка перед каждым полем. 0 трактуем как
    «не измерено» — тот же ноль-трап, что и в _render_dashboard — кроме
    висцерального жира, где 0 реален (db.py).

    Стрелка Δ_total смотрит по знаку: снижение веса — 📉, набор — 📈. Рисовать
    падение при росте было бы враньём в самой заметной строке отчёта."""
    metric = _latest_body_fields(conn, user_id, ("weight_kg", "muscle_mass_kg", "ffm_kg", "visceral_fat"))
    try:
        bmr = bmr_floor(conn, user_id)
    except ValueError:
        bmr = None

    parts = []
    if metric is not None:
        if metric["weight_kg"]:
            parts.append(f"⚖️ W: {metric['weight_kg']:.1f} kg")
            base = _base_weight(conn, user_id)
            if base is not None:
                delta = metric["weight_kg"] - base
                arrow = "📉" if delta < 0 else "📈" if delta > 0 else "➖"
                parts.append(f"{arrow} Δ_total: {_signed(delta, 1, 'kg')}")
        if metric["muscle_mass_kg"]:
            parts.append(f"💪 M: {metric['muscle_mass_kg']:.1f} kg")
        if metric["ffm_kg"]:
            parts.append(f"🧬 LBM: {metric['ffm_kg']:.1f} kg")
        if metric["visceral_fat"] is not None:
            parts.append(f"🧈 V_fat: {metric['visceral_fat']:.0f}")
    if bmr is not None:
        parts.append(f"⚡ BMR: {bmr:.0f} kcal")

    if not parts:
        return None
    return "[" + " | ".join(parts) + "]"


def _render_dashboard(conn, user_id: int) -> str:
    """Доп. визуальная сводка поверх той же БД — ничего не пересчитывает заново,
    только читает то, что уже возвращают report.day_summary/whr и guards.check_all,
    energy.bmr_floor, плюс последнюю строку body_metrics/anthropometry/users
    (там, где отчёт её не отдаёт). Markdown, не моноширинный блок — тот же
    формат, что уже приняли _render_day_dashboard и _render_food_log."""
    now = config.local_now()
    today = now.date().isoformat()
    try:
        # апсертит daily_targets (kcal/белок/жир/углеводы) ДО day_summary — иначе
        # первый рендер свежей даты читает пустые цели, а второй уже полные, и
        # умолчание расходится с format='dashboard'.
        daily_target(conn, user_id, today)
    except ValueError:
        pass
    d = day_summary(conn, user_id, today)
    ratio = whr(conn, user_id)
    fired = check_all(conn, user_id)

    _fields = ("weight_kg", "fat_pct", "muscle_mass_kg", "ffm_kg", "visceral_fat")
    metric = _latest_body_fields(conn, user_id, _fields)
    user_row = conn.execute(
        "SELECT height_cm, birth_date, sex, base_weight_kg FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    waist_row = conn.execute(
        "SELECT value_cm FROM anthropometry WHERE user_id=? AND site='талия' "
        "ORDER BY measured_on DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    tgt_row = conn.execute(
        "SELECT protein_g_target, fat_g_target, carbs_g_target, fiber_g_target "
        "FROM daily_targets WHERE user_id=? AND date=?",
        (user_id, today),
    ).fetchone()
    protein_target = tgt_row["protein_g_target"] if tgt_row else None
    fat_target = tgt_row["fat_g_target"] if tgt_row else None
    carbs_target = tgt_row["carbs_g_target"] if tgt_row else None
    fiber_target = tgt_row["fiber_g_target"] if tgt_row else None

    blocks = [f"📊 **Статус — {now.strftime('%d.%m')}, {now.strftime('%H:%M')}**"]

    # Цели строк тела — активные вехи (их задаёт человек, своих чисел у системы
    # нет). Без вехи у метрики цели не существует: показываем один факт.
    goals = {
        r["metric"]: r["threshold"]
        for r in conn.execute(
            "SELECT metric, threshold FROM milestones WHERE user_id=? AND achieved_at IS NULL",
            (user_id,),
        )
    }

    def _goal_baseline(metric_key: str):
        """Точка отсчёта метрики: для веса — стартовый (профиль или первое
        взвешивание, см. report.baseline_weight), для остальных — первое
        непустое значение той же колонки body_metrics."""
        if metric_key == "weight_kg":
            return _base_weight(conn, user_id)
        col = _GOAL_METRIC_COLUMNS.get(metric_key)
        if col is None:
            return None
        row = conn.execute(
            f"SELECT {col} AS v FROM body_metrics WHERE user_id=? AND {col} IS NOT NULL "
            "ORDER BY measured_at ASC LIMIT 1",
            (user_id,),
        ).fetchone()
        return row["v"] if row else None

    # --- 1) Вес и движение к ближайшей вехе -----------------------------
    # Одна иконка на весь раздел (⚖️, на первой строке) — дальше только текст,
    # владелец явно просил убрать иконку с каждой строки.
    # Считаем один раз и используем везде (строка Δ, "Нет в профиле", Δ BMR) —
    # _goal_baseline("weight_kg") делегирует в report.baseline_weight и не
    # зависит от того, есть ли вообще замер веса у этого пользователя (профиль
    # мог задать стартовый вес и без единого взвешивания).
    base_weight_kg = _goal_baseline("weight_kg")

    weight_lines = []
    if metric is not None and metric["weight_kg"]:
        weight_lines.append(f"⚖️ **Вес** {metric['weight_kg']:.1f} кг")
        if base_weight_kg is not None:
            delta = metric["weight_kg"] - base_weight_kg
            weight_lines.append(f"Δ от старта: {_signed(delta, 1, 'кг')}")
        # Δ за неделю — тот же trends(), что отдаёт get_trends, окно 7 дней;
        # None, если в последнюю неделю меньше двух замеров — тогда молчим,
        # а не рисуем нулевую динамику, которой не было.
        week_delta = trends(conn, user_id, window_days=7)["weight_delta_kg"]
        if week_delta is not None:
            weight_lines.append(f"Δ за неделю: {_signed(week_delta, 1, 'кг')}")
        weight_goal = goals.get("weight_kg")
        if weight_goal is not None and base_weight_kg is not None:
            frac = _goal_progress(base_weight_kg, metric["weight_kg"], weight_goal)
            if frac is not None:
                remaining = abs(metric["weight_kg"] - weight_goal)
                weight_lines.append(
                    f"До вехи {weight_goal:.1f} кг: {_dash_bar_frac(frac)} · осталось {remaining:.1f} кг"
                )
    else:
        weight_lines.append("⚖️ **Вес** —")

    if waist_row is not None:
        # Талия живёт в anthropometry, не в body_metrics — точку отсчёта берём оттуда же.
        waist_goal = goals.get("waist")
        if waist_goal is None:
            weight_lines.append(f"Талия: {waist_row['value_cm']:.0f} см")
        else:
            w_base = conn.execute(
                "SELECT value_cm FROM anthropometry WHERE user_id=? AND site='талия' "
                "ORDER BY measured_on ASC LIMIT 1",
                (user_id,),
            ).fetchone()
            w_frac = _goal_progress(w_base["value_cm"] if w_base else None,
                                    waist_row["value_cm"], waist_goal)
            text = f"{waist_row['value_cm']:.0f}/{waist_goal:.0f} см"
            bar = f" · {_dash_bar_frac(w_frac)}" if w_frac is not None else ""
            weight_lines.append(f"Талия: {text}{bar}")
    if ratio is not None:
        weight_lines.append(f"Т/Б: {ratio:.2f}")

    blocks.append("\n".join(weight_lines))

    missing_lines = _missing_profile_lines(_missing_profile_fields(user_row, base_weight_kg))
    if missing_lines:
        blocks.append("\n".join(missing_lines))

    # --- 2) КБЖУ и вода за день ------------------------------------------
    # Один раз 🍽 на раздел; статус-эмодзи (✅/⚠️/🔴) остаются на каждой строке
    # с целью — это не декоративная иконка, а сам ответ "норма/недобор/перебор".
    nutrient_rows = [
        ("Калории", d["kcal_eaten"], d["kcal_target"], "ккал", 0, d["kcal_target"] is not None),
        ("Белок", d["protein_g"], protein_target, "г", 0, bool(protein_target)),
        ("Жиры", d["fat_g"], fat_target, "г", 0, bool(fat_target)),
        ("Углеводы", d["carb_g"], carbs_target, "г", 0, bool(carbs_target)),
        # fiber_known, а не сам fiber_g: у продуктов дня клетчатка может быть
        # не проставлена, и тогда сумма 0 значит "не записано", а не "ноль".
        ("Клетчатка", d["fiber_g"], fiber_target, "г", 0,
         bool(fiber_target) and d.get("fiber_known")),
        ("Вода", d["water_ml"] / 1000,
         d["water_target_ml"] / 1000 if d["water_target_ml"] else None, "л", 1, bool(d["water_target_ml"])),
    ]
    nutrient_lines = ["🍽 **КБЖУ и вода**"]
    for name, val, target, unit, dec, has_target in nutrient_rows:
        if has_target:
            nutrient_lines.append(
                f"{name}: {val:.{dec}f}/{target:.{dec}f} {unit} · "
                f"{_dash_bar(val, target)} · {_nutrient_status(val, target, name in _FLOOR_ONLY)}"
            )
        else:
            nutrient_lines.append(
                "Клетчатка: нет данных" if name == "Клетчатка"
                else f"{name}: {val:.{dec}f} {unit} (без цели)")
    blocks.append("\n".join(nutrient_lines))

    # --- 3) Состав тела — С ДИНАМИКОЙ, не только последней цифрой -------
    # Раздел не про вехи (те остались в разделе "Вес") — тут Δ от первого
    # непустого замера каждого поля, независимо, задана ли цель.
    def _dyn(metric_key: str, current: float, dec: int, unit: str) -> str:
        base = _goal_baseline(metric_key)
        if base is None:
            return ""
        delta = current - base
        if round(delta, dec) == 0:
            return " (без изменений от старта)"
        return f" (Δ {_signed(delta, dec, unit).rstrip()} от старта)"

    body_comp_lines = ["🧬 **Состав тела**"]
    if metric is not None:
        # Ловушка нуля: visceral_fat=0 — реальный замер (см. db.py), остальные
        # поля ниже трактуют 0 как "не измерено" — тот же случай, что при первом
        # взвешивании без полной композиции.
        if metric["fat_pct"]:
            body_comp_lines.append(f"Жир: {metric['fat_pct']:.1f} %{_dyn('fat_pct', metric['fat_pct'], 1, '%')}")
        if metric["muscle_mass_kg"]:
            body_comp_lines.append(
                f"Мышцы: {metric['muscle_mass_kg']:.1f} кг"
                f"{_dyn('muscle_mass_kg', metric['muscle_mass_kg'], 1, 'кг')}"
            )
        if metric["ffm_kg"]:
            body_comp_lines.append(
                f"Сухая масса: {metric['ffm_kg']:.1f} кг{_dyn('ffm_kg', metric['ffm_kg'], 1, 'кг')}"
            )
        if metric["visceral_fat"] is not None:
            body_comp_lines.append(
                f"Висцеральный жир: {metric['visceral_fat']:.0f}"
                f"{_dyn('visceral_fat', metric['visceral_fat'], 0, '')}"
            )
        try:
            # bmr_floor бросает ValueError без единого body_metrics или без
            # роста/даты рождения в профиле — тогда просто не показываем строку,
            # причина уже названа выше блоком "Нет в профиле".
            bmr = bmr_floor(conn, user_id)
            bmr_line = f"BMR: {bmr:.0f} ккал"
            base_bmr = _bmr_for(conn, user_id, base_weight_kg, _goal_baseline("ffm_kg"))
            if base_bmr is not None and round(bmr - base_bmr) != 0:
                bmr_line += f" (Δ {_signed(bmr - base_bmr, 0, 'ккал')} от старта)"
            body_comp_lines.append(bmr_line)
        except ValueError:
            pass
    if len(body_comp_lines) > 1:
        blocks.append("\n".join(body_comp_lines))

    # --- 4) График веса за период — спарклайн блоками --------------------
    series = weight_series(conn, user_id, days=90)
    if series:
        dates = [d for d, _ in series]
        weights = [w for _, w in series]
        spark = _sparkline(_stride_sample(weights, 30))
        lo, hi = min(weights), max(weights)
        period = f"{dates[0][8:10]}.{dates[0][5:7]}–{dates[-1][8:10]}.{dates[-1][5:7]}"
        blocks.append(
            f"📈 **Вес за период**\n{spark}\n"
            f"{period} · мин {lo:.1f} кг · макс {hi:.1f} кг"
        )

    if fired:
        blocks.append("\n".join(f"⚠️ {a['message']}" for a in fired[:5]))

    metrics_line = _metrics_line(conn, user_id)
    if metrics_line is not None:
        blocks.append(metrics_line)

    return "\n\n".join(blocks)


def _render_day_dashboard(conn, user_id: int, date_str: str) -> str:
    """Итог дня markdown-строкой поверх report.day_summary/nutrition.plate_balance/
    energy.daily_target — ничего не пересчитывает заново, только читает и форматирует.
    "alerts" в d — уже записанные за дату алерты (report.day_summary), не новый
    вызов guards.check_all. Без моноширинного блока и без рамочной таблицы —
    иконки и статус-колонка (продакт-оун) не влезали в 40-символьный забор."""
    d = day_summary(conn, user_id, date_str)
    balance = plate_balance(conn, user_id, date_str)
    user_row = conn.execute(
        "SELECT height_cm, birth_date, sex, base_weight_kg FROM users WHERE id=?",
        (user_id,),
    ).fetchone()

    # kcal_target по умолчанию — то, что уже лежит в daily_targets (day_summary).
    # daily_target() тут же пересчитывает и апсертит ВСЮ строку daily_targets
    # (kcal_target И protein_g_target) — тот же побочный эффект, что уже принят
    # у admin recalc. Поэтому и цель по калориям, и чтение protein_g_target
    # ниже стоят ПОСЛЕ этого вызова: иначе первый рендер читает старое число,
    # апсерт его меняет, и повторный вызов в тот же день рисует другую таблицу
    # на тех же данных. bmr_floor внутри бросает ValueError без body_metrics/
    # роста/даты рождения — тогда тренировочную строку не показываем и
    # остаёмся на уже сохранённой цели, причина — в блоке "Нет в профиле" ниже.
    kcal_target = d["kcal_target"]
    tcx_net = None
    try:
        result = daily_target(conn, user_id, date_str)
        kcal_target = result["kcal"]
        tcx_net = result["tcx_net"]
    except ValueError:
        pass

    # Читаем цели ПОСЛЕ daily_target() выше: он апсертит всю строку daily_targets,
    # включая жир/углеводы. Иначе первый рендер для свежей даты берёт из day_summary
    # ещё пустые fat/carbs_g_target и не рисует их прогресс-бары.
    tgt_row = conn.execute(
        "SELECT protein_g_target, fat_g_target, carbs_g_target, fiber_g_target "
        "FROM daily_targets WHERE user_id=? AND date=?",
        (user_id, date_str),
    ).fetchone()
    protein_target = tgt_row["protein_g_target"] if tgt_row else None
    fat_target = tgt_row["fat_g_target"] if tgt_row else None
    carbs_target = tgt_row["carbs_g_target"] if tgt_row else None
    fiber_target = tgt_row["fiber_g_target"] if tgt_row else None

    day_label = datetime.strptime(date_str[:10], "%Y-%m-%d").strftime("%d.%m")
    blocks = [f"🍽 **Итог дня — {day_label}**"]

    kcal_eaten = d["kcal_eaten"]
    water_l = d["water_ml"] / 1000
    water_target_l = d["water_target_ml"] / 1000 if d["water_target_ml"] else None
    # (иконка, имя, значение, цель, единица, знаков после точки, есть ли цель) —
    # флаг цели у каждого нутриента считается тем же условием, что и раньше
    # (Калории — is not None, остальные — truthy: 0 в цели равносилен её отсутствию).
    nutrient_rows = [
        ("🔥", "Калории", kcal_eaten, kcal_target, "ккал", 0, kcal_target is not None),
        ("🥩", "Белок", d["protein_g"], protein_target, "г", 0, bool(protein_target)),
        ("🥑", "Жиры", d["fat_g"], fat_target, "г", 0, bool(fat_target)),
        ("🍞", "Углеводы", d["carb_g"], carbs_target, "г", 0, bool(carbs_target)),
        # см. коммент у dashboard-формата — тот же пробел в данных.
        ("🥬", "Клетчатка", d["fiber_g"], fiber_target, "г", 0,
         bool(fiber_target) and d.get("fiber_known")),
        ("💧", "Вода", water_l, water_target_l, "л", 1, bool(d["water_target_ml"])),
    ]
    target_lines = []
    no_target = []
    for icon, name, val, target, unit, dec, has_target in nutrient_rows:
        if has_target:
            target_lines.append(
                f"{icon} **{name}** {val:.{dec}f}/{target:.{dec}f} {unit} · "
                f"{_dash_bar(val, target)} · {_nutrient_status(val, target, name in _FLOOR_ONLY)}"
            )
        else:
            no_target.append(
                "🥬 Клетчатка нет данных" if name == "Клетчатка"
                else f"{icon} {name} {val:.{dec}f} {unit}")
    # Тренировка (tcx_net) цели не имеет никогда — сразу в общую строку "Без цели".
    if tcx_net:
        no_target.append(f"🏋 Тренировка {tcx_net:.0f} ккал")

    if target_lines:
        blocks.append("\n".join(target_lines))
    # Метрики без цели — одной строкой (явная просьба продакт-оуна), а не по
    # отдельной строке на каждую, как раньше в табличном варианте.
    if no_target:
        blocks.append("Без цели: " + " · ".join(no_target))

    missing_lines = _missing_profile_lines(_missing_profile_fields(user_row, _base_weight(conn, user_id)))
    if missing_lines:
        blocks.append("\n".join(missing_lines))

    if balance:
        parts = []
        for cat, info in balance.items():
            pct_str = f"{info['share'] * 100:.0f}%"
            if info["target_share"] is not None:
                pct_str += f"/{info['target_share'] * 100:.0f}%"
            parts.append(f"{cat} {pct_str}")
        blocks.append("Тарелка: " + " · ".join(parts))

    if d["alerts"]:
        blocks.append("\n".join(f"⚠️ {a['message']}" for a in d["alerts"][:5]))

    metrics_line = _metrics_line(conn, user_id)
    if metrics_line is not None:
        blocks.append(metrics_line)

    return "\n\n".join(blocks)


@_handler_wrapper
def handle_query_metrics(params: dict) -> str:
    """Query historical metrics (weight, fat, visceral_fat, etc.) over a window."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    metric = params.get("metric")
    window_days = params.get("window_days", 90)

    # enum в схеме — подсказка, не гарантия. Без этого гейта неизвестный metric
    # раньше падал через db_field=metric напрямую в f-string SQL: не только
    # ломающийся запрос, а сырая интерполяция имени колонки из параметра модели.
    db_field = _METRIC_MAP.get(metric)
    if db_field is None:
        conn.close()
        return json.dumps(
            {"error": f"metric должен быть одним из {sorted(_METRIC_MAP)}, получено {metric!r}"},
            ensure_ascii=False)

    from datetime import datetime, timedelta
    since = (config.local_now() - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M:%S")

    # Query historical data
    rows = conn.execute(
        f"SELECT measured_at, {db_field} FROM body_metrics "
        f"WHERE user_id=? AND measured_at>=? AND {db_field} IS NOT NULL "
        f"ORDER BY measured_at ASC",
        (user_id, since),
    ).fetchall()

    data = []
    for row in rows:
        data.append({
            "date": row["measured_at"][:10],
            "value": row[db_field],
        })

    conn.close()

    return json.dumps(
        {
            "metric": metric,
            "window_days": window_days,
            "data": data,
        },
        ensure_ascii=False,
    )


@_handler_wrapper
def handle_query_food(params: dict) -> str:
    """Query food log for a date range."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    start_date = params.get("start_date")
    end_date = params.get("end_date")

    # Query food logs
    rows = conn.execute(
        "SELECT fl.eaten_at, fi.name, fi.grams, fi.kcal, fi.protein_g, fi.fat_g, fi.carbs_g, fi.plate_category "
        "FROM food_log fl JOIN food_items fi ON fi.food_log_id=fl.id "
        "WHERE fl.user_id=? AND date(fl.eaten_at) BETWEEN ? AND ? "
        "ORDER BY fl.eaten_at",
        (user_id, start_date, end_date),
    ).fetchall()

    foods = []
    for row in rows:
        foods.append({
            "eaten_at": row["eaten_at"],
            "name": row["name"],
            "grams": row["grams"],
            "kcal": row["kcal"],
            "protein_g": row["protein_g"],
            "fat_g": row["fat_g"],
            "carbs_g": row["carbs_g"],
            "plate_category": row["plate_category"],
        })

    conn.close()

    return json.dumps(
        {
            "start_date": start_date,
            "end_date": end_date,
            "foods": foods,
        },
        ensure_ascii=False,
    )


_PHARMA_DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _dt(s: str) -> datetime:
    """Терпимый разбор ISO-метки (пробел или 'T', с временем или без)."""
    s = _norm_ts(s) or s
    if len(s) == 10:
        s += " 00:00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


def _advance_next(base: str, every_days: int) -> str:
    """Следующая доза = base + каденция, ISO 'YYYY-MM-DD HH:MM:SS'."""
    return (_dt(base) + timedelta(days=every_days)).strftime("%Y-%m-%d %H:%M:%S")


def _dose_num(text) -> float | None:
    """Число из TEXT дозы ('12.5', '12.5 мг', ...). None, если не нашли."""
    if text is None:
        return None
    m = re.search(r"[-+]?\d*\.?\d+", str(text))
    return float(m.group()) if m else None


def _ladder_step(ladder: list[float], value: float, eps: float = 1e-6) -> int | None:
    """Индекс ступени лестницы, совпадающей с value (с допуском на float)."""
    for i, step in enumerate(ladder):
        if abs(step - value) < eps:
            return i
    return None


def _ladder_note(ladder: list[float]) -> str:
    return "Ступени лестницы: " + ", ".join(f"{s:g}" for s in ladder) + "."


def _dose_start_date(conn, user_id: int, substance: str, dose_f: float):
    """Самая ранняя запись med_log с дозой dose_f, идущая от конца истории
    приёма этого препарата и не прерванная записью другой дозы (docs/adr/
    0002: «повышение... не раньше минимального срока с даты, когда начался
    приём текущей дозы»). None, если истории нет вовсе — тогда по сроку не
    блокируем, а не считаем срок нулевым."""
    rows = conn.execute(
        "SELECT at, dose FROM med_log WHERE user_id=? AND substance=? ORDER BY at",
        (user_id, substance),
    ).fetchall()
    start = None
    for r in rows:
        d = _dose_num(r["dose"])
        if d is not None and abs(d - dose_f) < 1e-6:
            if start is None:
                start = _dt(r["at"])
        else:
            start = None
    return start


def _check_dose_bounds(conn, user_id: int, substance: str, dose_f: float, card_data: dict, now: datetime):
    """Рамки плана дозы из карты препарата (docs/adr/0002-рекомендация-дозы).

    Возвращает (текст_ошибки_или_None, resume_after_break). Модель предлагает
    дозу, код только держит рамки: ступень лестницы, не выше максимума,
    повышение на соседнюю ступень не раньше min_weeks, а после перерыва в
    терапии (>14 дней без приёма) — не выше прежней дозы."""
    ladder = card_data["ladder"]
    note = _ladder_note(ladder)
    idx_new = _ladder_step(ladder, dose_f)
    if idx_new is None:
        return f"{dose_f:g} — не ступень лестницы титрации. {note}", False
    if dose_f > max(ladder) + 1e-6:
        return f"{dose_f:g} выше максимума {max(ladder):g}. {note}", False

    last_log = conn.execute(
        "SELECT at, dose FROM med_log WHERE user_id=? AND substance=? ORDER BY at DESC LIMIT 1",
        (user_id, substance),
    ).fetchone()
    if last_log is None and idx_new != 0 and conn.execute(
        "SELECT 1 FROM med_schedule WHERE user_id=? AND substance=?", (user_id, substance)
    ).fetchone() is None:
        # без расписания и истории приёма — это начало терапии, а не повышение, но старт с максимума опасен
        return (f"Начало терапии — со стартовой ступени {ladder[0]:g}. Если доза {dose_f:g} уже назначена "
                f"врачом или уже принимается — укажи by_doctor. {note}"), False
    if last_log is not None and (now - _dt(last_log["at"])).days > 14:
        last_dose = _dose_num(last_log["dose"])
        if last_dose is not None and dose_f > last_dose + 1e-6:
            return (f"Перерыв в терапии больше 2 недель (последний приём {_dt(last_log['at']):%Y-%m-%d}) "
                    f"— новая доза не выше последней принятой ({last_dose:g}). {note}"), False
        return None, True  # возобновление не выше прежней дозы — resume_after_break

    current = conn.execute(
        "SELECT dose FROM med_schedule WHERE user_id=? AND substance=?", (user_id, substance)
    ).fetchone()
    current_dose = current["dose"] if current else None
    if current_dose is None or dose_f <= current_dose + 1e-6:
        return None, False  # снижение/та же ступень/первое расписание — без ограничений

    idx_current = _ladder_step(ladder, current_dose)
    if idx_current is None or idx_new != idx_current + 1:
        return f"Повышение только на соседнюю ступень выше {current_dose:g}. {note}", False

    min_weeks = card_data.get("min_weeks")
    if min_weeks:
        start = _dose_start_date(conn, user_id, substance, current_dose)
        if start is not None:
            weeks = (now - start).days / 7
            if weeks < min_weeks:
                can_at = start + timedelta(weeks=min_weeks)
                return (f"Минимум {min_weeks} нед на ступени {current_dose:g} — повысить можно с "
                        f"{can_at:%Y-%m-%d}. {note}"), False
    return None, False


def _pharma_due(next_at: str) -> str:
    """Человекочитаемая следующая доза: 'Вс 24.08 22:00 (через 2д)'.

    Просрочку считаем по времени, а не по календарным датам: доза на 22:00
    вчера, увиденная в 03:00, просрочена на пять часов, а не «на 1д» —
    округление вверх до суток превращало свежий пропуск в застарелый.
    """
    dt = _dt(next_at)
    # next_at хранится локальным настенным временем; пояс снимаем, чтобы
    # вычитание не упало на naive/aware.
    now = config.local_now().replace(tzinfo=None)
    label = f"{_PHARMA_DOW[dt.weekday()]} {dt.strftime('%d.%m %H:%M')}"
    late = now - dt
    if late.total_seconds() > 0:
        if late.days:
            rel = f"просрочено {late.days}д"
        elif late.seconds >= 3600:
            rel = f"просрочено {late.seconds // 3600}ч"
        else:
            rel = "пора"
    else:
        days = (dt.date() - now.date()).days
        rel = "сегодня" if days == 0 else "завтра" if days == 1 else f"через {days}д"
    return f"{label} ({rel})"


def _glp1_line(glp: dict) -> str:
    """Строка про оценочный уровень препарата (GLP-1-класса) для _render_pharma.

    Формат оценки, не факта: цифры из health_core.glp1 — фармакокинетическая
    прикидка по вкладышу препарата, а не измерение. Никаких калорий/таргетов
    отсюда не считается — только ориентир для планирования дня."""
    parts = [f"Уровень ≈ {glp['level_pct']}% ({glp['phase']})"]
    if glp.get("peak_at"):
        pk = _dt(glp["peak_at"])
        parts.append(f"пик {_PHARMA_DOW[pk.weekday()]} {pk.strftime('%d.%m')}")
    if glp.get("trough_at"):
        tr = _dt(glp["trough_at"])
        parts.append(f"минимум {_PHARMA_DOW[tr.weekday()]} {tr.strftime('%d.%m %H:%M')}")
    return " · ".join(parts) + f" — оценка по T½ {glp.get('half_life_days', 5)} дн"


def _render_pharma(rows, last, glp_by_substance=None, stock_warn=None) -> str:
    glp_by_substance = glp_by_substance or {}
    lines = ["💊 Фарма"]
    if not rows:
        lines.append("Расписаний нет. Задай: pharma schedule.")
    for r in rows:
        dose = f"{r['dose']:g}{r['unit'] or ''}" if r["dose"] is not None else "—"
        route = f" ({r['route']})" if r["route"] else ""
        lines.append(f"\n{r['substance']} · {dose}{route}")
        if r["next_at"]:
            lines.append(f"  Следующая: {_pharma_due(r['next_at'])}")
            glp = glp_by_substance.get(_canon_med(r["substance"]))
            if glp is not None:
                lines.append(f"  {_glp1_line(glp)}")
        if r["every_days"]:
            lines.append(f"  Каждые {r['every_days']} дн")
        if r["stock_doses"] is not None:
            warn = " ⚠ мало" if r["stock_doses"] <= 2 else ""
            lines.append(f"  Остаток: {r['stock_doses']:g} доз{warn}")
            runs_out_at = (stock_warn or {}).get(r["substance"])
            if runs_out_at:
                lines.append(f"  Запаса хватит до {runs_out_at[8:10]}.{runs_out_at[5:7]}")
    if last is not None:
        # med_log.dose — TEXT (модель пишет '10' или '10mg'), не число: без :g.
        d = f"{last['dose']}{last['unit'] or ''}" if last["dose"] is not None else ""
        lines.append(f"\nПоследний приём: {last['substance']} {d} ({last['at'][:16]})")
    return "```\n" + "\n".join(lines) + "\n```"


@_handler_wrapper
def handle_pharma(params: dict) -> str:
    """Фарма-контур: расписание приёма, рекомендованная доза (её задаёт модель), остаток
    доз. action=status|schedule|restock|remove. schedule держит рамки лестницы титрации
    из карты препарата (docs/adr/0002): доза — ступень лестницы, не выше максимума,
    повышение только на соседнюю ступень не раньше минимального срока, после перерыва
    в терапии (>14 дней) — не выше прежней дозы. by_doctor=true снимает рамки. Фактический
    приём — отдельный инструмент log_med; он же списывает дозу и двигает next_at."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    action = (params.get("action") or "status").lower()

    if action == "status":
        rows = conn.execute(
            "SELECT substance, dose, unit, route, every_days, next_at, stock_doses, notes "
            "FROM med_schedule WHERE user_id=? ORDER BY next_at IS NULL, next_at",
            (user_id,),
        ).fetchall()
        last = conn.execute(
            "SELECT at, substance, dose, unit FROM med_log WHERE user_id=? ORDER BY at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        # Оценка уровня — вспомогательная и отдельная по каждому препарату
        # расписания, у которого в карте есть t½ и пик (health_core.glp1.profile);
        # сбой в ней не должен рушить статус фармы.
        glp_by_substance = {}
        try:
            from health_core import glp1
            for r in rows:
                c = _med_card(r["substance"])
                if c and c.get("half_life_days") and c.get("tmax_h"):
                    p = glp1.profile(conn, user_id, substance=r["substance"])
                    if p is not None:
                        glp_by_substance[_canon_med(r["substance"])] = p
        except Exception:
            glp_by_substance = {}
        from health_core.meds import stock_runs_out
        stock_warn = {w["substance"]: w["runs_out_at"] for w in stock_runs_out(conn, user_id)}
        conn.close()
        return json.dumps({"pharma": _render_pharma(rows, last, glp_by_substance, stock_warn=stock_warn)}, ensure_ascii=False)

    substance = _canon_med(params.get("substance"))
    if not substance:
        conn.close()
        return json.dumps({"error": "Нужно название препарата (substance)"}, ensure_ascii=False)

    if action == "schedule":
        dose_param = params.get("dose")
        by_doctor = bool(params.get("by_doctor"))
        resume_after_break = False

        # Рамки из карты препарата (docs/adr/0002) — только когда есть новая
        # доза, лестница в карте и приём не по назначению врача. Без карты/
        # лестницы или с by_doctor=true — как раньше, чистый учёт.
        if dose_param is not None and not by_doctor:
            try:
                dose_f = float(dose_param)
            except (TypeError, ValueError):
                conn.close()
                return json.dumps({"error": "dose должен быть числом"}, ensure_ascii=False)
            card_data = _med_card(substance)
            if card_data and card_data.get("ladder"):
                now = config.local_now().replace(tzinfo=None)
                err, resume_after_break = _check_dose_bounds(conn, user_id, substance, dose_f, card_data, now)
                if err:
                    conn.close()
                    return json.dumps({"error": err}, ensure_ascii=False)

        # dose_by_doctor привязан к устанавливаемой дозе: не трогаем колонку,
        # если dose в этом вызове не задаётся (COALESCE с NULL оставит как было).
        dose_by_doctor_val = (1 if by_doctor else 0) if dose_param is not None else None

        conn.execute(
            "INSERT INTO med_schedule(user_id, substance, dose, unit, route, every_days, next_at, "
            "stock_doses, notes, dose_by_doctor, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id, substance) DO UPDATE SET "
            "dose=COALESCE(excluded.dose,dose), unit=COALESCE(excluded.unit,unit), "
            "route=COALESCE(excluded.route,route), every_days=COALESCE(excluded.every_days,every_days), "
            "next_at=COALESCE(excluded.next_at,next_at), stock_doses=COALESCE(excluded.stock_doses,stock_doses), "
            "notes=COALESCE(excluded.notes,notes), dose_by_doctor=COALESCE(excluded.dose_by_doctor,dose_by_doctor), "
            "updated_at=excluded.updated_at",
            (user_id, substance, dose_param, params.get("unit"), params.get("route"),
             params.get("every_days"), _norm_ts(params.get("next_at")), params.get("stock_doses"),
             params.get("notes"), dose_by_doctor_val, _now_iso()),
        )
        conn.commit()
        conn.close()
        resp = {"ok": f"Расписание обновлено: {substance}"}
        if resume_after_break:
            resp["resume_after_break"] = True
        return json.dumps(resp, ensure_ascii=False)

    if action == "restock":
        row = conn.execute(
            "SELECT id, stock_doses FROM med_schedule WHERE user_id=? AND substance=?", (user_id, substance)
        ).fetchone()
        if row is None:
            conn.close()
            return json.dumps({"error": f"Нет расписания '{substance}'. Сначала pharma schedule."},
                              ensure_ascii=False)
        try:
            add = float(params.get("add_doses"))
        except (TypeError, ValueError):
            conn.close()
            return json.dumps({"error": "add_doses должно быть числом"}, ensure_ascii=False)
        new = (row["stock_doses"] or 0.0) + add
        conn.execute("UPDATE med_schedule SET stock_doses=?, updated_at=? WHERE id=?",
                     (new, _now_iso(), row["id"]))
        conn.commit()
        conn.close()
        return json.dumps({"ok": f"Остаток {substance}: {new:g} доз"}, ensure_ascii=False)

    if action == "remove":
        cur = conn.execute("DELETE FROM med_schedule WHERE user_id=? AND substance=?", (user_id, substance))
        conn.commit()
        conn.close()
        if cur.rowcount == 0:
            return json.dumps({"error": f"'{substance}' нет в расписании"}, ensure_ascii=False)
        return json.dumps({"ok": f"Убрано из расписания: {substance}"}, ensure_ascii=False)

    conn.close()
    return json.dumps({"error": f"Неизвестное действие: {action}. Допустимо: status, schedule, restock, remove"},
                      ensure_ascii=False)


@_handler_wrapper
def handle_drug_card_draft(params: dict) -> str:
    """Черновик карты препарата (CONTEXT.md «Черновик карты», docs/adr/0002):
    для препарата без карты (pharma/card не находит лестницу). action=fetch —
    официальные тексты по МНН латиницей (openFDA, а если там пусто —
    ClinicalTrials.gov), без веб-поиска и без памяти модели. action=save —
    черновик ИЗ ЭТИХ ТЕКСТОВ на одобрение админу; до одобрения в панели
    (/drafts) карта не действует — работает только учёт приёма."""
    from health_core import card_drafts

    action = (params.get("action") or "fetch").lower()

    if action == "fetch":
        inn = (params.get("inn") or "").strip()
        if not inn:
            return json.dumps({"error": "Нужен inn — МНН препарата латиницей"}, ensure_ascii=False)
        return json.dumps(card_drafts.fetch_sources(inn), ensure_ascii=False)

    if action == "save":
        substance = (params.get("substance") or "").strip()
        if not substance:
            return json.dumps({"error": "Нужно substance — имя препарата для заголовка карты"}, ensure_ascii=False)
        fields = params.get("fields")
        if fields is not None and not isinstance(fields, dict):
            return json.dumps({"error": "fields должен быть объектом"}, ensure_ascii=False)
        sources = params.get("sources")
        if sources is not None and not isinstance(sources, list):
            return json.dumps({"error": "sources должен быть списком ссылок"}, ensure_ascii=False)

        conn = connect()
        migrate(conn)
        user_id = _get_user_id(params, conn)
        who = conn.execute("SELECT telegram_user_id FROM users WHERE id=?", (user_id,)).fetchone()
        telegram_id = who["telegram_user_id"] if who else user_id
        draft_id = card_drafts.save_draft(conn, user_id, substance, fields or {}, sources or [])
        conn.close()

        # Тот же _PENDING_USER_NOTIFICATIONS, которым бот уведомляет админов о
        # заявках на доступ (bot/main.py._notify_admins_of_request) — второй
        # очереди не заводим. Ленивый импорт: bot.main тянет aiogram и сам
        # импортирует registry -> plugin.tools, прямой импорт наверху модуля
        # был бы циклом.
        try:
            from bot.main import _PENDING_USER_NOTIFICATIONS, admin_user_ids
            text = (f"Новый черновик карты: {substance} (от пользователя {telegram_id}). "
                    f"Сверь с источниками и одобри в панели: /drafts")
            for admin_id in admin_user_ids():
                _PENDING_USER_NOTIFICATIONS.append((admin_id, text))
        except ImportError:
            pass  # standalone-запуск (самопроверка) без бота — уведомлять некого

        return json.dumps({
            "draft_id": draft_id,
            "status": "pending",
            "note": "Черновик отправлен на одобрение админу. До одобрения по этому препарату "
                    "работает только учёт приёма, без рекомендаций дозы.",
        }, ensure_ascii=False)

    return json.dumps({"error": f"Неизвестное действие: {action}. Допустимо: fetch, save"}, ensure_ascii=False)


_PLAN_SLOT_LABELS = {"breakfast": "🍳 Завтрак", "lunch": "🍲 Обед", "dinner": "🍛 Ужин", "snack": "🍎 Перекус"}


def _render_plan(plan: dict, dow) -> str:
    by_day: dict[int, list[str]] = {}
    for mm in plan["meals"]:
        slot = _PLAN_SLOT_LABELS.get(mm["meal_slot"], mm["meal_slot"] or "Приём")
        macro = []
        if mm.get("kcal"):
            macro.append(f"{mm['kcal']:.0f} ккал")
        if mm.get("protein_g"):
            macro.append(f"Б{mm['protein_g']:.0f}")
        tail = (" · " + " ".join(macro)) if macro else ""
        by_day.setdefault(mm["day_of_week"], []).append(f"  {slot} — {mm['name'] or '—'}{tail}")
    for wk in plan["workouts"]:
        dur = f" · {wk['duration_min']:.0f} мин" if wk.get("duration_min") else ""
        by_day.setdefault(wk["day_of_week"], []).append(f"  🏋 {wk['name'] or '—'}{dur}")
    title = "🗓 План — " + (_PHARMA_DOW[dow] if dow is not None else "неделя")
    lines = [title]
    if not by_day:
        lines.append("План пуст. Задай: plans set_meal / set_workout.")
    for d in sorted(by_day):
        lines.append(f"\n{_PHARMA_DOW[d]}:")
        lines.extend(by_day[d])
    return "```\n" + "\n".join(lines) + "\n```"


def _render_plan_vs(diff: dict, date_str: str) -> str:
    dow = _PHARMA_DOW[_dt(date_str).weekday()]
    day_label = _dt(date_str).strftime("%d.%m")
    lines = [f"📋 План vs факт — {day_label} ({dow})"]
    mp, ma = diff["meals"]["planned"], diff["meals"]["actual"]
    if mp is None:
        lines.append("Еда: план на этот день не задан")
    else:
        lines.append(f"Еда: план {mp['kcal']:.0f} / факт {ma['kcal']:.0f} ккал "
                     f"(Δ {mp['kcal'] - ma['kcal']:+.0f})")
        lines.append(f"  Белок: план {mp['protein_g']:.0f} / факт {ma['protein_g']:.0f} г")
    wp, wa = diff["workouts"]["planned"], diff["workouts"]["actual"]
    if wp is None:
        lines.append("Тренировки: план на этот день не задан")
    else:
        lines.append(f"Тренировки: план {wp['duration_min']:.0f} / факт {wa['duration_min']:.0f} мин")
    return "```\n" + "\n".join(lines) + "\n```"


@_handler_wrapper
def handle_plans(params: dict) -> str:
    """Недельный шаблон питания и тренировок (0=Пн..6=Вс). action=show|set_meal|
    set_workout|vs_actual. Числа плана задаёт модель, код только хранит и сверяет."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    action = (params.get("action") or "show").lower()
    dow = params.get("day_of_week")

    if action == "show":
        plan = get_plan(conn, user_id, int(dow) if dow is not None else None)
        conn.close()
        return json.dumps({"plans": _render_plan(plan, int(dow) if dow is not None else None)},
                          ensure_ascii=False)

    if action == "vs_actual":
        date_str = params.get("date") or _today_iso()
        diff = plan_vs_actual(conn, user_id, date_str)
        conn.close()
        return json.dumps({"plans": _render_plan_vs(diff, date_str)}, ensure_ascii=False)

    if action == "set_meal":
        slot = params.get("meal_slot")
        if dow is None or slot not in _MEAL_SLOTS:
            conn.close()
            return json.dumps({"error": f"Нужны day_of_week (0-6) и meal_slot из {sorted(_MEAL_SLOTS)}"},
                              ensure_ascii=False)
        fields = {k: params[k] for k in ("name", "kcal", "protein_g", "fat_g", "carbs_g", "notes")
                  if params.get(k) is not None}
        set_meal_plan(conn, user_id, int(dow), slot, **fields)
        conn.close()
        return json.dumps({"ok": f"План еды сохранён: {_PHARMA_DOW[int(dow)]} {slot}"}, ensure_ascii=False)

    if action == "set_workout":
        name = (params.get("name") or "").strip()
        if dow is None or not name:
            conn.close()
            return json.dumps({"error": "Нужны day_of_week (0-6) и name тренировки"}, ensure_ascii=False)
        fields = {k: params[k] for k in ("kind", "duration_min", "notes") if params.get(k) is not None}
        set_workout_plan(conn, user_id, int(dow), name, **fields)
        conn.close()
        return json.dumps({"ok": f"План тренировки сохранён: {_PHARMA_DOW[int(dow)]} {name}"}, ensure_ascii=False)

    if action in ("remove_meal", "delete_meal"):
        slot = params.get("meal_slot")
        from health_core.plans import remove_meal_plan
        count = remove_meal_plan(conn, user_id, int(dow) if dow is not None else None, slot)
        conn.close()
        target = f"{_PHARMA_DOW[int(dow)]} {slot or 'весь день'}" if dow is not None else "все дни"
        return json.dumps({"ok": f"Удалено позиций питания ({target}): {count}"}, ensure_ascii=False)

    if action in ("remove_workout", "delete_workout"):
        name = params.get("name")
        from health_core.plans import remove_workout_plan
        count = remove_workout_plan(conn, user_id, int(dow) if dow is not None else None, name)
        conn.close()
        target = f"{_PHARMA_DOW[int(dow)]} {name or 'весь день'}" if dow is not None else "все дни"
        return json.dumps({"ok": f"Удалено тренировок ({target}): {count}"}, ensure_ascii=False)

    if action in ("clear", "delete", "remove"):
        from health_core.plans import remove_meal_plan, remove_workout_plan
        m_count = remove_meal_plan(conn, user_id, int(dow) if dow is not None else None)
        w_count = remove_workout_plan(conn, user_id, int(dow) if dow is not None else None)
        conn.close()
        target = _PHARMA_DOW[int(dow)] if dow is not None else "вся неделя"
        return json.dumps({"ok": f"Недельный шаблон ({target}) очищен: удалено {m_count} приёмов пищи, {w_count} тренировок"}, ensure_ascii=False)

    conn.close()
    return json.dumps({"error": f"Неизвестное действие: {action}. Допустимо: show, set_meal, set_workout, vs_actual, remove_meal, remove_workout, clear"},
                      ensure_ascii=False)


_PANTRY_CATEGORY_ORDER = ["Белковые", "Молочка/Сыры", "Овощи/Фрукты", "Сложные углеводы", "Прочее"]


@_handler_wrapper
def handle_equipment(params: dict) -> str:
    """Инвентарь для тренировок: что вообще есть под рукой. Тот же принцип, что
    у pantry с холодильником — код хранит, подбор делает модель."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    action = (params.get("action") or "list").lower()

    if action == "list":
        rows = conn.execute(
            "SELECT name, kind, detail FROM equipment WHERE user_id=? AND available=1 "
            "ORDER BY kind, name", (user_id,)
        ).fetchall()
        conn.close()
        if not rows:
            return json.dumps({"equipment": [], "empty": True,
                               "hint": "Инвентарь пуст. Спроси человека, что у него есть "
                                       "(гантели и их вес, турник, резины, дорожка, VR-шлем, "
                                       "коврик), и запиши через equipment add. Без этого "
                                       "тренировку не составить."}, ensure_ascii=False)
        return json.dumps({"equipment": [dict(r) for r in rows]}, ensure_ascii=False)

    name = (params.get("name") or "").strip()
    if not name:
        conn.close()
        return json.dumps({"error": "Нужно название (name)"}, ensure_ascii=False)

    if action == "add":
        conn.execute(
            "INSERT INTO equipment(user_id, name, kind, detail, available, updated_at) "
            "VALUES (?,?,?,?,1,?) ON CONFLICT(user_id, name) DO UPDATE SET "
            "kind=COALESCE(excluded.kind,kind), detail=COALESCE(excluded.detail,detail), "
            "available=1, updated_at=excluded.updated_at",
            (user_id, name, params.get("kind"), params.get("detail"), _now_iso()),
        )
        conn.commit()
        conn.close()
        return json.dumps({"ok": f"Записано: {name}"}, ensure_ascii=False)

    if action == "remove":
        cur = conn.execute("DELETE FROM equipment WHERE user_id=? AND name=?", (user_id, name))
        conn.commit()
        conn.close()
        return json.dumps({"ok": f"Убрано: {name}"} if cur.rowcount else
                          {"error": f"'{name}' нет в инвентаре"}, ensure_ascii=False)

    conn.close()
    return json.dumps({"error": f"Неизвестное действие {action!r}"}, ensure_ascii=False)


@_handler_wrapper
def handle_plan_day(params: dict) -> str:
    """План на конкретную дату: тренировка или питание. Текст пишет модель,
    код только хранит и отдаёт — в том числе задним числом, для истории."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    action = (params.get("action") or "get").lower()
    kind = (params.get("kind") or "workout").lower()
    if kind not in ("workout", "meal"):
        conn.close()
        return json.dumps({"error": "kind должен быть workout или meal"}, ensure_ascii=False)
    ds = _norm_ts(params.get("date"))
    ds = ds[:10] if ds else _today_iso()

    if action == "list":
        try:
            limit = min(int(params.get("limit") or 14), 60)
        except (TypeError, ValueError):
            limit = 14
        rows = conn.execute(
            "SELECT date, kind, substr(body,1,160) preview FROM plan_log "
            "WHERE user_id=? AND kind=? ORDER BY date DESC LIMIT ?", (user_id, kind, limit)
        ).fetchall()
        conn.close()
        return json.dumps({"plans": [dict(r) for r in rows]}, ensure_ascii=False)

    if action == "get":
        row = conn.execute("SELECT date, kind, body, rationale FROM plan_log "
                           "WHERE user_id=? AND date=? AND kind=?", (user_id, ds, kind)).fetchone()
        conn.close()
        return json.dumps(dict(row) if row else {"empty": True, "date": ds, "kind": kind},
                          ensure_ascii=False)

    if action == "save":
        body = (params.get("body") or "").strip()
        if not body:
            conn.close()
            return json.dumps({"error": "Нужен текст плана (body)"}, ensure_ascii=False)
        # Тренировку без инвентаря не сохраняем: план «от балды» выглядит как
        # настоящий и попадает в историю наравне с обоснованными. Опрос
        # инструментов — предусловие в коде, а не пожелание в промпте.
        if kind == "workout":
            have = conn.execute(
                "SELECT COUNT(*) n FROM equipment WHERE user_id=? AND available=1",
                (user_id,)).fetchone()["n"]
            if not have:
                conn.close()
                return json.dumps({"error": "Инвентарь пуст — сначала спроси, какое оборудование "
                                            "есть, и запиши через equipment add. Тренировка без "
                                            "этого не сохраняется."}, ensure_ascii=False)
        conn.execute(
            "INSERT INTO plan_log(user_id, date, kind, body, rationale, created_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id, date, kind) DO UPDATE SET "
            "body=excluded.body, rationale=COALESCE(excluded.rationale,rationale), "
            "created_at=excluded.created_at",
            (user_id, ds, kind, body, params.get("rationale"), _now_iso()),
        )
        conn.commit()
        conn.close()
        return json.dumps({"ok": f"План ({kind}) на {ds} сохранён"}, ensure_ascii=False)

    if action in ("delete", "remove", "clear"):
        from health_core.plans import delete_plan_day
        clear_all = bool(params.get("all"))
        date_to_del = None if clear_all else ds
        kind_to_del = None if clear_all and params.get("kind") is None else kind
        count = delete_plan_day(conn, user_id, date_to_del, kind_to_del)
        conn.close()
        target = f"{kind} на {ds}" if date_to_del else f"все планы {kind_to_del or ''}"
        return json.dumps({"ok": f"Удалено планов ({target}): {count}", "deleted_count": count}, ensure_ascii=False)

    conn.close()
    return json.dumps({"error": f"Неизвестное действие {action!r}. Допустимо: get, list, save, delete"}, ensure_ascii=False)


@_handler_wrapper
def handle_forecast(params: dict) -> str:
    """Прогноз массы: динамический баланс энергии (health_core/forecast.py).

    Не экстраполяция тренда — по мере падения массы падает расход, и потеря
    замедляется. Всё, что отсюда выходит, помечено как оценка и несёт полосу,
    а не одну кривую."""
    from health_core import forecast as _fc

    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    action = (params.get("action") or "project").lower()

    intake = params.get("intake_kcal")
    intake = float(intake) if intake is not None else None

    if action == "reach":
        target = params.get("target_kg")
        if target is None:
            conn.close()
            return json.dumps({"error": "Для reach нужен target_kg"}, ensure_ascii=False)
        res = _fc.reach(conn, user_id, float(target), intake)
        conn.close()
        return json.dumps(res, ensure_ascii=False)

    if action == "project":
        try:
            horizon = int(params.get("horizon_days") or 84)
        except (TypeError, ValueError):
            horizon = 84
        res = _fc.project(conn, user_id, horizon, intake)
        conn.close()
        # Посуточная траектория целиком модели не нужна — это сотни точек в
        # контексте ради двух чисел. Отдаём вехи по неделям.
        traj = res.pop("trajectory", None)
        if traj:
            res["weekly"] = [traj[i] for i in range(0, len(traj), 7)]
            if traj[-1] not in res["weekly"]:
                res["weekly"].append(traj[-1])
        return json.dumps(res, ensure_ascii=False)

    conn.close()
    return json.dumps({"error": f"Неизвестное действие {action!r}"}, ensure_ascii=False)


@_handler_wrapper
def handle_refeed(params: dict) -> str:
    """Плановые перерывы в дефиците, протокол MATADOR (health_core/refeed.py)."""
    from health_core import refeed as _refeed

    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    action = (params.get("action") or "status").lower()
    today = config.local_now().replace(tzinfo=None).date().isoformat()

    if action == "status":
        st = _refeed.status(conn, user_id, today)
        conn.close()
        if st.get("phase") is None:
            return json.dumps({"phase": None,
                               "hint": "Цикл не запущен. refeed schedule расставит "
                                       "2 недели дефицита / 2 недели поддержания."},
                              ensure_ascii=False)
        return json.dumps(st, ensure_ascii=False)

    if action == "schedule":
        start = _norm_ts(params.get("start"))
        start = start[:10] if start else today
        try:
            weeks = min(max(int(params.get("horizon_weeks") or 12), 4), 52)
        except (TypeError, ValueError):
            weeks = 12
        res = _refeed.schedule(conn, user_id, start, weeks)
        conn.close()
        res["note"] = ("В первые дни перерыва вес прибавит 1.5-2.5 кг — это гликоген "
                       "с водой, не жир. Перерыв не отменять.")
        return json.dumps(res, ensure_ascii=False)

    if action == "clear":
        n = _refeed.clear(conn, user_id, today)
        conn.close()
        return json.dumps({"ok": f"Снято будущих перерывов: {n}"}, ensure_ascii=False)

    conn.close()
    return json.dumps({"error": f"Неизвестное действие {action!r}"}, ensure_ascii=False)


@_handler_wrapper
def handle_sick(params: dict) -> str:
    """Режим болезни: дефицит отключается, шумные гарды молчат."""
    from health_core import sick as _sick

    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    action = (params.get("action") or "status").lower()
    today = config.local_now().replace(tzinfo=None).date().isoformat()

    if action == "status":
        st = _sick.status(conn, user_id, today)
        conn.close()
        return json.dumps(st, ensure_ascii=False)

    if action == "start":
        days = params.get("days")
        if days is not None:
            try:
                days = int(days)
            except (TypeError, ValueError):
                conn.close()
                return json.dumps({"error": "days — число дней 1..30"}, ensure_ascii=False)
        else:
            days = config.load()["sick"]["default_days"]

        from_date = params.get("from_date") or today
        note = params.get("note")

        try:
            result = _sick.start(conn, user_id, from_date, days, note=note)
        except ValueError as e:
            conn.close()
            return json.dumps({"error": str(e)}, ensure_ascii=False)

        # Refresh today's calorie target
        kcal_target = None
        try:
            from health_core.energy import daily_target
            tgt = daily_target(conn, user_id, today)
            kcal_target = round(tgt["kcal"])
        except Exception:
            pass

        conn.close()
        result["kcal_target"] = kcal_target
        quiet_days = config.load().get("sick", {}).get("quiet_after_days", 2)
        result["note"] = (f"Цель без дефицита, напоминания о еде выключены, шумные гарды молчат "
                          f"ещё {quiet_days} дн. после выздоровления.")
        return json.dumps(result, ensure_ascii=False)

    if action == "stop":
        n = _sick.stop(conn, user_id, today)

        # Refresh today's calorie target
        kcal_target = None
        try:
            from health_core.energy import daily_target
            tgt = daily_target(conn, user_id, today)
            kcal_target = round(tgt["kcal"])
        except Exception:
            pass

        conn.close()
        return json.dumps({
            "ok": f"Режим болезни снят, убрано дней: {n}",
            "kcal_target": kcal_target
        }, ensure_ascii=False)

    conn.close()
    return json.dumps({"error": f"Неизвестное действие {action!r}. Допустимо: start, stop, status"}, ensure_ascii=False)


@_handler_wrapper
def handle_pantry(params: dict) -> str:
    """Холодильник: учёт продуктовых запасов. action=list|add|remove.
    Хранилище детерминированное; умный подбор блюд из запасов и списание при
    логировании еды — задача модели (она уже разбирает еду на продукты), поэтому
    здесь только CRUD, без нечёткого сопоставления имён в коде."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    action = (params.get("action") or "list").lower()

    if action == "list":
        rows = conn.execute(
            "SELECT name, qty, unit, category FROM pantry WHERE user_id=? ORDER BY category, name",
            (user_id,),
        ).fetchall()
        conn.close()
        if not rows:
            return json.dumps({"pantry": "🧊 Холодильник пуст."}, ensure_ascii=False)
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r["category"] or "Прочее", []).append(r)
        order = _PANTRY_CATEGORY_ORDER + sorted(k for k in groups if k not in _PANTRY_CATEGORY_ORDER)
        lines = ["🧊 Холодильник:"]
        for cat in order:
            if cat not in groups:
                continue
            lines.append(f"\n{cat}:")
            for r in groups[cat]:
                unit = f" {r['unit']}" if r["unit"] else ""
                qty = f" — {r['qty']:g}{unit}" if r["qty"] is not None else ""
                lines.append(f"  • {r['name']}{qty}")
        return json.dumps({"pantry": "```\n" + "\n".join(lines) + "\n```"}, ensure_ascii=False)

    name = (params.get("name") or "").strip()
    if not name:
        conn.close()
        return json.dumps({"error": "Нужно название продукта"}, ensure_ascii=False)

    def _qty_or_none():
        if params.get("qty") is None:
            return None
        return float(params["qty"])

    if action == "add":
        # Валидация category, если передана
        category = params.get("category")
        if category is not None and category not in _PANTRY_CATEGORY_ORDER:
            conn.close()
            return json.dumps({
                "error": f"Неизвестная категория '{category}'. Допустимые: {', '.join(_PANTRY_CATEGORY_ORDER)}"
            }, ensure_ascii=False)
        try:
            qty = _qty_or_none()
        except (TypeError, ValueError):
            conn.close()
            return json.dumps({"error": "Количество должно быть числом"}, ensure_ascii=False)
        existing = conn.execute(
            "SELECT id, qty FROM pantry WHERE user_id=? AND name=?", (user_id, name)
        ).fetchone()
        if existing is not None:
            # Пополнение существующего: складываем количества; None-qty у любой из
            # сторон означает «без счёта», тогда просто сохраняем что есть.
            new_qty = existing["qty"] if qty is None else (existing["qty"] or 0.0) + qty
            conn.execute(
                "UPDATE pantry SET qty=?, unit=COALESCE(?,unit), category=COALESCE(?,category), updated_at=? WHERE id=?",
                (new_qty, params.get("unit"), params.get("category"), _now_iso(), existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO pantry(user_id, name, qty, unit, category, updated_at) VALUES (?,?,?,?,?,?)",
                (user_id, name, qty, params.get("unit"), params.get("category") or "Прочее", _now_iso()),
            )
        conn.commit()
        conn.close()
        return json.dumps({"ok": f"В холодильник: {name}"}, ensure_ascii=False)

    if action == "remove":
        existing = conn.execute(
            "SELECT id, qty FROM pantry WHERE user_id=? AND name=?", (user_id, name)
        ).fetchone()
        if existing is None:
            conn.close()
            return json.dumps({"error": f"'{name}' нет в холодильнике"}, ensure_ascii=False)
        try:
            amount = _qty_or_none()
        except (TypeError, ValueError):
            conn.close()
            return json.dumps({"error": "Количество должно быть числом"}, ensure_ascii=False)
        # Без количества, либо без учёта остатка, либо списываем больше чем есть —
        # убираем позицию целиком; иначе уменьшаем остаток.
        if amount is None or existing["qty"] is None or amount >= existing["qty"]:
            conn.execute("DELETE FROM pantry WHERE id=?", (existing["id"],))
            msg = f"Списано полностью: {name}"
        else:
            left = existing["qty"] - amount
            conn.execute("UPDATE pantry SET qty=?, updated_at=? WHERE id=?", (left, _now_iso(), existing["id"]))
            msg = f"Списано {amount:g}, осталось {left:g}: {name}"
        conn.commit()
        conn.close()
        return json.dumps({"ok": msg}, ensure_ascii=False)

    conn.close()
    return json.dumps({"error": f"Неизвестное действие: {action}. Допустимо: list, add, remove"}, ensure_ascii=False)


@_handler_wrapper
def handle_style(params: dict) -> str:
    """Стили персоны: короткая инструкция о тоне ответов. action=list|add|use|del.
    Ровно один стиль активен на персону — его текст get_status_bar кладёт в поле
    "style" на каждый диалог (см. handle_get_status_bar): это единственный канал,
    которым персональная манера доходит до модели у Hermes."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    action = (params.get("action") or "").lower()

    if action == "list":
        _active_style(conn, user_id)  # гарантирует хотя бы debian и одну активную строку
        rows = conn.execute(
            "SELECT name, instruction, is_active FROM persona_styles WHERE user_id=? ORDER BY created_at",
            (user_id,),
        ).fetchall()
        conn.close()
        lines = ["🎨 **Стили**", ""]
        for r in rows:
            mark = "✅" if r["is_active"] else "•"
            lines.append(f"{mark} **{r['name']}** — {r['instruction']}")
        return json.dumps({"styles": "\n".join(lines)}, ensure_ascii=False)

    if action == "add":
        name = (params.get("name") or "").strip()
        instruction = (params.get("instruction") or "").strip()
        if not name or not instruction:
            conn.close()
            return json.dumps({"error": "Нужны name и instruction"}, ensure_ascii=False)
        conn.execute(
            "INSERT INTO persona_styles(user_id, name, instruction, is_active, created_at) "
            "VALUES (?, ?, ?, 0, ?) "
            "ON CONFLICT(user_id, name) DO UPDATE SET instruction=excluded.instruction",
            (user_id, name, instruction, _now_iso()),
        )
        conn.commit()
        conn.close()
        return json.dumps({"result": f"Стиль сохранён: {name}"}, ensure_ascii=False)

    if action == "use":
        name = (params.get("name") or "").strip()
        row = conn.execute(
            "SELECT id FROM persona_styles WHERE user_id=? AND name=?", (user_id, name)
        ).fetchone()
        if row is None:
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM persona_styles WHERE user_id=? ORDER BY created_at", (user_id,)
            ).fetchall()]
            conn.close()
            return json.dumps(
                {"error": f"Стиля '{name}' нет. Доступны: {', '.join(names) or 'нет ни одного'}"},
                ensure_ascii=False)
        # Одна транзакция на деактивацию всех + активацию одной: две активных
        # строки — испорченное состояние, а не промежуточный шаг.
        conn.execute("UPDATE persona_styles SET is_active=0 WHERE user_id=?", (user_id,))
        conn.execute("UPDATE persona_styles SET is_active=1 WHERE id=?", (row["id"],))
        conn.commit()
        conn.close()
        return json.dumps({"result": f"Стиль переключён: {name}"}, ensure_ascii=False)

    if action == "del":
        name = (params.get("name") or "").strip()
        row = conn.execute(
            "SELECT is_active FROM persona_styles WHERE user_id=? AND name=?", (user_id, name)
        ).fetchone()
        if row is None:
            conn.close()
            return json.dumps({"error": f"Стиля '{name}' нет"}, ensure_ascii=False)
        # Оба гарда держатся ДО проверки confirm: подтверждение не имеет права
        # обойти их, это не «ещё один шаг», а запрещённая операция вовсе.
        if name == _DEFAULT_STYLE_NAME:
            conn.close()
            return json.dumps(
                {"error": f"Стиль '{_DEFAULT_STYLE_NAME}' — дефолт, на нём держится вся система, удалить нельзя"},
                ensure_ascii=False)
        if row["is_active"]:
            conn.close()
            return json.dumps(
                {"error": f"Стиль '{name}' сейчас активен, сначала переключитесь на другой через use"},
                ensure_ascii=False)
        if params.get("confirm") != "УДАЛИТЬ":
            conn.close()
            return json.dumps(
                {"result": f"Будет удалён стиль '{name}'. Для подтверждения передайте confirm: \"УДАЛИТЬ\""},
                ensure_ascii=False)
        conn.execute("DELETE FROM persona_styles WHERE user_id=? AND name=?", (user_id, name))
        conn.commit()
        conn.close()
        return json.dumps({"result": f"Стиль удалён: {name}"}, ensure_ascii=False)

    conn.close()
    return json.dumps({"error": f"Неизвестное действие: {action}. Допустимо: list, add, use, del"},
                      ensure_ascii=False)


@_handler_wrapper
def handle_explain_target(params: dict) -> str:
    """Explain why the daily calorie target is what it is."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    date_str = params.get("date", _today_iso())

    # Get the target row
    target_row = conn.execute(
        "SELECT kcal_target, computed_from FROM daily_targets WHERE user_id=? AND date=?",
        (user_id, date_str),
    ).fetchone()

    explanation = {}
    if target_row:
        explanation["kcal_target"] = target_row["kcal_target"]
        explanation["computed_from"] = target_row["computed_from"]
    else:
        explanation["kcal_target"] = None
        explanation["computed_from"] = "not calculated"

    # Честный вердикт срока (CONTEXT.md «Недостижимый срок») — computed_from
    # несёт только тег "deadline_unreachable" (план ниже пола), не сам вердикт
    # о недостижимости, который требует ещё и прогноза по факту.
    from health_core.energy import deadline_verdict, daily_expenditure
    explanation["deadline_verdict"] = deadline_verdict(conn, user_id, date_str)

    # Расход дня (ADR 0004): части блендера и их доли — модель объясняет, из
    # чего собралась цель, а не только видит итоговый тег "blend:...".
    explanation["expenditure"] = daily_expenditure(conn, user_id, date_str)

    conn.close()

    return json.dumps(
        {
            "date": date_str,
            "explanation": explanation,
        },
        ensure_ascii=False,
    )


@_handler_wrapper
def handle_get_progress(params: dict) -> str:
    """Прогресс к вехе: текущее значение, цель, остаток, срок."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    name = params.get("milestone_name")

    # required в схеме не гарантирует присутствие: без явного гейта отсутствующее
    # имя тихо превращалось в "Веха 'None' не найдена" — техническая ошибка,
    # а не понятный отказ.
    if not name or not str(name).strip():
        conn.close()
        return json.dumps({"error": "milestone_name обязателен"}, ensure_ascii=False)

    m = conn.execute(
        "SELECT name, metric, threshold, deadline, achieved_at "
        "FROM milestones WHERE user_id=? AND name=?",
        (user_id, name),
    ).fetchone()
    if not m:
        conn.close()
        return json.dumps({"error": f"Веха '{name}' не найдена"}, ensure_ascii=False)

    # Метрика вехи задаётся пользователем, а не подразумевается весом.
    metric = m["metric"]
    _SOURCES = {
        "weight_kg": ("body_metrics", "weight_kg", "measured_at"),
        "ffm_kg": ("body_metrics", "ffm_kg", "measured_at"),
        "fat_pct": ("body_metrics", "fat_pct", "measured_at"),
        "waist": ("anthropometry", "value_cm", "measured_on"),
    }
    current = None
    if metric in _SOURCES:
        table, col, ts = _SOURCES[metric]
        where = "user_id=?" + (" AND site='талия'" if metric == "waist" else "")
        row = conn.execute(
            f"SELECT {col} AS v FROM {table} WHERE {where} "
            f"AND {col} IS NOT NULL ORDER BY {ts} DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        current = row["v"] if row else None

    # deadline_verdict честен только про АКТИВНУЮ веху (ближайший срок среди
    # недостигнутых, см. energy._active_milestone) — если спросили про другую,
    # вердикт ей не принадлежит, отдавать чужой было бы враньём.
    from health_core.energy import deadline_verdict
    verdict = deadline_verdict(conn, user_id)
    if verdict is not None and verdict["milestone"] != m["name"]:
        verdict = None

    conn.close()
    return json.dumps(
        {
            "milestone": m["name"],
            "metric": metric,
            "target": m["threshold"],
            "current": current,
            "remaining": round(current - m["threshold"], 2) if current is not None else None,
            "deadline": m["deadline"],
            "achieved_at": m["achieved_at"],
            "deadline_verdict": verdict,
        },
        ensure_ascii=False,
    )
    # ponytail: прогноз срока сюда не кладём — дефицит знает energy.daily_target,
    # обёртка не имеет права его выдумывать.


@_handler_wrapper
def handle_get_weekly_summary(params: dict) -> str:
    """Недельная сводка: вес, состав, дисциплина, план/факт, гардрейлы за 7 дней.
    Готовый блок — модель приписывает дословно. Гардрейлы читаются из таблицы
    alerts, а не пересчитываются: check_all при вызове ЗАПИСЫВАЕТ новые алерты,
    и отчёт, меняющий данные в момент чтения, сломал бы правило "один алерт в сутки"."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    text = weekly_summary(conn, user_id, params.get("end_date"))
    conn.close()
    return json.dumps({"weekly_summary": text}, ensure_ascii=False)


def handle_get_evening_report(params: dict) -> str:
    """Вечерний отчёт §11 — готовая строка, агентный cron в 21:30 её пересказывает."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id(params, conn)
    date_str = params.get("date", _today_iso())
    text = evening_report(conn, user_id, date_str)
    conn.close()
    return json.dumps({"date": date_str, "evening_report": text}, ensure_ascii=False)


_HELP_USER = """🧭 **Что я умею**

**🍽 Еда**
Пиши как говоришь: «завтрак — горбуша 188 г, 3 яйца» или «съел творог 130 г».
Слово «завтрак/обед/ужин» ставит приём; без него это перекус.
• «журнал» / «что я съел» — продукты по приёмам за день
• «итог дня» — калории и БЖУ с прогрессом и статусом
• «пульт» — всё разом: тело, вес, КБЖУ, еда, гарды, фарма

**💧 Вода**
«выпил 500 мл», «стакан воды».

**⚖️ Вес и состав тела**
«вес 120.5» — или пришли файл выгрузки с весов, разберу сам.
«как дела» / «мой статус» — вес, тренд, цель дня.

**📏 Замеры и сахар**
«талия 118», «таз 130» · «сахар 5.4 натощак».

**💊 Фарма**
«когда колоть» / «расписание» — что, когда, сколько осталось.
«поставил 5 мг» — зафиксирую приём, остаток и следующая доза посчитаются сами.

**🧊 Холодильник и план**
«что в холодильнике» · «добавь курицу 1 кг» · «списал творог 130 г»
«что приготовить» / «составь ужин» — соберу из того, что есть.
«мой план» — недельный план питания и тренировок.

**🩺 Анализы**
«анализы 12.09: глюкоза 5.1, инсулин 9, креатинин 88» — сохраню и посчитаю HOMA-IR, eGFR и другие показатели.

**🤒 Болезнь**
«заболел» / «температура 38» — уберу дефицит и напоминания о еде; «выздоровел» — верну как было.

**🎯 Цели и прогресс**
«почему такая цель» — разложу расчёт калорий по шагам.
«прогресс» / «тренды» — вес, сухая масса, талия за период, пульсовые зоны тренировок.
«веха 110» — поставить цель. Число цели ставишь только ты.

Специальных команд заучивать не надо — говори обычными словами, инструмент я выберу сам."""

_HELP_ADMIN = """🛠 **Админ**

`admin_cmd`: mode · status · guards · targets · set <ключ> <значение> · milestone add/del · milestones · recalc [дата] · export · backup · alerts [N]

Доступ (в телеграме): /approve <tg_id> · /deny <tg_id> · /revoke <tg_id> · /access — заявки и допущенные

Вход — «mode admin» (только allowlist), выход — «mode user»."""


@_handler_wrapper
def handle_help(params: dict) -> str:
    """Справка «что умею и как со мной говорить». Готовый блок — модель
    приписывает его дословно, а не пересказывает своими словами: у слэш-команд
    плагина нет (меню в Telegram — встроенные команды Hermes), и справка,
    пересказанная по памяти, разъезжается с реальным набором инструментов.
    Админский раздел показывается только в режиме admin — в user он не нужен и
    противоречит [MODE]."""
    text = _HELP_USER
    if _get_mode(_caller_telegram_id() or "") == "admin":
        text += "\n\n" + _DASH_RULE + "\n\n" + _HELP_ADMIN
    return json.dumps({"help": text}, ensure_ascii=False)


def _zero_write_error(cur, msg: str) -> str | None:
    """rowcount==0 после INSERT/UPDATE — запись не произошла молча. JSON-ошибка или None."""
    if cur.rowcount == 0:
        return json.dumps({"error": msg}, ensure_ascii=False)
    return None


@_handler_wrapper
def handle_register_user(params: dict) -> str:
    """Регистрация нового пользователя или обновление профиля. UPSERT по telegram_user_id.

    Identity ТОЛЬКО из _caller_telegram_id() (ContextVar шлюза Hermes) — модель её не
    подделает. params["telegram_user_id"] используется, только когда caller_id нет вовсе
    (self-check/CLI/cron вне Hermes) — единственный путь, где эта ветка вообще достижима,
    потому что внутри Hermes caller_id есть всегда. Внутри Hermes params-значение игнорируется.
    """
    conn = connect()
    migrate(conn)

    caller_id = _caller_telegram_id()
    if caller_id:
        telegram_user_id = caller_id  # реальная личность, params для identity не смотрим
    else:
        # Вне Hermes ContextVar в принципе недоступен — тестовый/CLI путь.
        telegram_user_id = params.get("telegram_user_id")
        if telegram_user_id is None:
            conn.close()
            return json.dumps({"error": "Caller identity could not be identified"}, ensure_ascii=False)

    height_cm = params.get("height_cm")
    birth_date = params.get("birth_date")
    sex = params.get("sex")
    timezone = params.get("timezone")
    base_weight_kg = params.get("base_weight_kg")
    base_weight_date = params.get("base_weight_date")
    health_notes = params.get("health_notes")
    # Личные окна приёмов пищи (CONTEXT.md «Окно приёма пищи»), тот же механизм,
    # что и часовой пояс выше: JSON-объект с ключами breakfast/lunch/dinner ->
    # {"start": "HH:MM", "end": "HH:MM"}, частичный — health_core.chrono.meal_windows
    # мержит его с config.yaml meals.* по каждому приёму отдельно.
    meal_windows_param = params.get("meal_windows")
    if meal_windows_param is not None:
        # окна сравниваются строками ЧЧ:ММ, а скрипт напоминаний делает int(); "7:00" сломал бы оба
        _hhmm = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
        if not (isinstance(meal_windows_param, dict) and meal_windows_param and all(
            name in ("breakfast", "lunch", "dinner") and isinstance(w, dict) and w and set(w) <= {"start", "end"}
            and all(isinstance(v, str) and _hhmm.fullmatch(v) for v in w.values())
            and not ("start" in w and "end" in w and w["start"] >= w["end"])
            for name, w in meal_windows_param.items()
        )):
            conn.close()
            return json.dumps({"error": "meal_windows: объект {breakfast|lunch|dinner: {start, end}}, "
                                        "время строго ЧЧ:ММ (например \"07:00\"), start раньше end"},
                              ensure_ascii=False)
    meal_windows_json = (
        json.dumps(meal_windows_param, ensure_ascii=False) if meal_windows_param is not None else None
    )

    # Личный максимальный пульс (CONTEXT.md «Максимальный пульс»): из
    # нагрузочного теста или с часов, заменяет формулу Tanaka в hr_zones целиком.
    # Источник обязателен ИМЕННО в этом вызове — не подтягивается из старой
    # записи, чтобы значение без источника никогда не легло в базу.
    hr_max_bpm = params.get("hr_max_bpm")
    hr_max_source = params.get("hr_max_source")
    if hr_max_bpm is not None:
        try:
            hr_max_bpm = int(hr_max_bpm)
        except (TypeError, ValueError):
            conn.close()
            return json.dumps({"error": f"hr_max_bpm должен быть целым числом, получено {hr_max_bpm!r}"},
                              ensure_ascii=False)
        if not (100 <= hr_max_bpm <= 230):
            conn.close()
            return json.dumps({"error": f"hr_max_bpm вне диапазона 100–230 уд/мин: {hr_max_bpm}"},
                              ensure_ascii=False)
        if hr_max_source not in ("test", "watch"):
            conn.close()
            return json.dumps({"error": "hr_max_source обязателен вместе с hr_max_bpm: 'test' или 'watch'"},
                              ensure_ascii=False)
    elif hr_max_source is not None and hr_max_source not in ("test", "watch"):
        conn.close()
        return json.dumps({"error": "hr_max_source: 'test' или 'watch'"}, ensure_ascii=False)

    # Профиль без роста, даты рождения и пола бесполезен: BMR по Mifflin считается
    # именно по ним, а без BMR нет ни пола цели, ни гардрейла BMR_FLOOR. Запись,
    # которая проходит и оставляет NULL, — это ложный успех: инструмент отвечает
    # «готово», а система остаётся без цели. Требуем их на создании.
    existing_row = conn.execute(
        "SELECT height_cm, birth_date, sex FROM users WHERE telegram_user_id=?",
        (telegram_user_id,),
    ).fetchone()
    _missing = [
        n for n, v in (("height_cm", height_cm), ("birth_date", birth_date), ("sex", sex))
        if v is None and (existing_row is None or existing_row[n] is None)
    ]
    if _missing:
        conn.close()
        return json.dumps(
            {"error": f"Не хватает полей профиля: {', '.join(_missing)}. "
                      f"Без них не посчитать BMR и целевой калораж."},
            ensure_ascii=False)

    # UPDATE first: rowcount тут и есть проверка "пользователь уже существует",
    # без отдельного SELECT-чека, который может разойтись с реальной записью.
    # COALESCE: не required-поля (timezone/base_weight_*) необязательны в схеме
    # именно потому, что здесь сохраняется старое значение, если параметр не
    # прислан — без этого частичное обновление профиля молча стирало бы их в
    # NULL при каждом повторном вызове register_user без полного набора полей.
    cur = conn.execute(
        "UPDATE users SET height_cm=COALESCE(?,height_cm), birth_date=COALESCE(?,birth_date), "
        "sex=COALESCE(?,sex), timezone=COALESCE(?,timezone), "
        "base_weight_kg=COALESCE(?,base_weight_kg), base_weight_date=COALESCE(?,base_weight_date), "
        "health_notes=COALESCE(?,health_notes), meal_windows=COALESCE(?,meal_windows), "
        "hr_max_bpm=COALESCE(?,hr_max_bpm), hr_max_source=COALESCE(?,hr_max_source) "
        "WHERE telegram_user_id=?",
        (height_cm, birth_date, sex, timezone, base_weight_kg, base_weight_date, health_notes,
         meal_windows_json, hr_max_bpm, hr_max_source, telegram_user_id),
    )
    created = cur.rowcount == 0

    if created:
        cur = conn.execute(
            "INSERT INTO users(telegram_user_id, height_cm, birth_date, sex, timezone, base_weight_kg, base_weight_date, health_notes, meal_windows, hr_max_bpm, hr_max_source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (telegram_user_id, height_cm, birth_date, sex, timezone, base_weight_kg, base_weight_date, health_notes,
             meal_windows_json, hr_max_bpm, hr_max_source, _now_iso()),
        )
        err = _zero_write_error(cur, "register_user: INSERT had no effect")
        if err:
            conn.close()
            return err

    conn.commit()

    # Get user_id — подтверждаем, что запись реально видна, а не молча пропала
    user_row = conn.execute(
        "SELECT id FROM users WHERE telegram_user_id=?",
        (telegram_user_id,),
    ).fetchone()
    if user_row is None:
        conn.close()
        return json.dumps({"error": "register_user: write did not persist"}, ensure_ascii=False)
    user_id = user_row["id"]

    # Seed user_targets via config.targets_for()
    targets = config.targets_for(conn, user_id)

    if targets:
        today = _today_iso()
        conn.execute(
            "INSERT OR IGNORE INTO user_targets(user_id, valid_from, protein_g, water_ml) "
            "VALUES (?, ?, ?, ?)",
            (user_id, today, targets.get("protein_g"), targets.get("water_ml")),
        )
        conn.commit()

    conn.close()

    return json.dumps(
        {"user_id": user_id, "created": created, "targets": targets},
        ensure_ascii=False,
    )


@_handler_wrapper
def handle_set_milestone(params: dict) -> str:
    """Установить или обновить веху. UPSERT по (user_id, name)."""
    conn = connect()
    migrate(conn)

    user_id = _get_user_id(params, conn)
    name = params.get("name")
    metric = params.get("metric")
    threshold = params.get("threshold")
    deadline = params.get("deadline")

    # Validate metric
    valid_metrics = ["weight_kg", "ffm_kg", "fat_pct", "waist"]
    if metric not in valid_metrics:
        conn.close()
        return json.dumps(
            {"error": f"Metric '{metric}' not recognized. Valid: {', '.join(valid_metrics)}"},
            ensure_ascii=False,
        )

    # threshold REAL допускает NULL — без гейта веха пишется без цели, и
    # get_progress падает на current - None только при следующем обращении.
    try:
        threshold = _as_float("threshold", threshold)
    except ValueError as e:
        conn.close()
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    # UPSERT milestone
    conn.execute(
        "INSERT INTO milestones(user_id, name, metric, threshold, deadline) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, name) DO UPDATE SET metric=?, threshold=?, deadline=?",
        (user_id, name, metric, threshold, deadline, metric, threshold, deadline),
    )
    conn.commit()

    # Fetch the milestone to return
    m = conn.execute(
        "SELECT id, user_id, name, metric, threshold, deadline, achieved_at FROM milestones "
        "WHERE user_id=? AND name=?",
        (user_id, name),
    ).fetchone()

    conn.close()

    return json.dumps(
        {
            "id": m["id"],
            "user_id": m["user_id"],
            "name": m["name"],
            "metric": m["metric"],
            "threshold": m["threshold"],
            "deadline": m["deadline"],
            "achieved_at": m["achieved_at"],
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------- ADMIN TOOLS

def _admin_denial(caller_id: str, is_admin_caller: bool) -> str | None:
    """Единственная проверка админских прав: allowlist И текущий режим admin.
    None — доступ есть, строка — готовый текст отказа.

    Вынесена отдельно, потому что её нужна не только handle_admin_cmd: слэш-команда
    /users сначала гейтилась «зондом» — звала admin_cmd с безобидной подкомандой и
    смотрела, отказала ли та. Так право на список персон висело на том, что зондовая
    подкоманда останется админской: разгейтить её как безобидную — и /users молча
    открывается всем. Проверка должна быть одна и называться своим именем."""
    if not is_admin_caller:
        return "Not authorized"
    if _get_mode(caller_id) != "admin":
        return "Режим пользователя. Сначала: admin_cmd(command: \"mode admin\")"
    return None


@_handler_wrapper
def handle_admin_cmd(params: dict) -> str:
    """Admin command handler. Real telegram_user_id from Hermes gateway, never from params."""
    import yaml
    import shutil
    from pathlib import Path

    # Get REAL caller identity from gateway ContextVar, NOT from params
    caller_id = _caller_telegram_id()
    command = params.get("command", "").strip()

    # Fail closed: no identity means caller could not be identified
    if not caller_id:
        return json.dumps({"error": "Caller identity could not be identified"}, ensure_ascii=False)

    # Load config to check admin allowlist
    cfg_path = Path(config.CONFIG_PATH)
    try:
        with open(cfg_path, encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        return json.dumps({"error": f"Failed to load config: {e}"}, ensure_ascii=False)

    admin_ids = cfg.get("admin", {}).get("telegram_admin_ids", [])
    # Normalize both to strings for comparison: config may have ints, ContextVar yields string
    admin_ids_str = [str(id_) for id_ in admin_ids]
    is_admin_caller = caller_id in admin_ids_str

    # Parse command
    parts = command.split()
    if not parts:
        return json.dumps({"error": "Empty command"}, ensure_ascii=False)

    subcmd = parts[0]

    # mode / mode user: dropping OUT of admin never needs admin rights, any resolved
    # caller may run it. mode admin: allowlist-gated inside _admin_mode.
    if subcmd == "mode":
        return _admin_mode(caller_id, parts[1:], is_admin_caller)

    # Every other subcommand needs BOTH allowlist membership AND current mode == admin.
    denial = _admin_denial(caller_id, is_admin_caller)
    if denial is not None:
        return json.dumps({"error": denial}, ensure_ascii=False)

    # Route subcommands
    if subcmd == "status":
        return _admin_status()
    elif subcmd == "guards":
        return _admin_guards()
    elif subcmd == "targets":
        return _admin_targets()
    elif subcmd == "set":
        if len(parts) < 3:
            return json.dumps({"error": "set: requires <key> <value>"}, ensure_ascii=False)
        key, value = parts[1], " ".join(parts[2:])
        return _admin_set(key, value)
    elif subcmd == "milestone":
        if len(parts) < 2:
            return json.dumps({"error": "milestone: requires add|del <name> ..."}, ensure_ascii=False)
        action = parts[1]
        if action == "add":
            if len(parts) < 4:
                return json.dumps({"error": "milestone add: requires <name> <metric> <threshold> [deadline]"}, ensure_ascii=False)
            name, metric, threshold = parts[2], parts[3], parts[4]
            deadline = parts[5] if len(parts) > 5 else None
            return _admin_milestone_add(name, metric, threshold, deadline)
        elif action == "del":
            if len(parts) < 3:
                return json.dumps({"error": "milestone del: requires <name>"}, ensure_ascii=False)
            name = parts[2]
            return _admin_milestone_del(name)
        else:
            return json.dumps({"error": f"Unknown milestone action: {action}"}, ensure_ascii=False)
    elif subcmd == "milestones":
        return _admin_milestones()
    elif subcmd == "recalc":
        recalc_date = parts[1] if len(parts) > 1 else None
        return _admin_recalc(recalc_date)
    elif subcmd == "export":
        return _admin_export()
    elif subcmd == "backup":
        return _admin_backup()
    elif subcmd == "alerts":
        n = int(parts[1]) if len(parts) > 1 else 10
        return _admin_alerts(n)
    else:
        valid = ["mode", "status", "guards", "targets", "set", "milestone", "milestones", "recalc", "export", "backup", "alerts"]
        return json.dumps({"error": f"Unknown command: {subcmd}. Valid: {', '.join(valid)}"}, ensure_ascii=False)


def _mode_table_ready(conn) -> None:
    """Режим — рантайм-состояние на пользователя, живёт в БД, не в config.yaml (deployment policy)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_mode ("
        "telegram_user_id TEXT PRIMARY KEY, mode TEXT NOT NULL, set_at TEXT NOT NULL)"
    )


def _get_mode(caller_id: str) -> str:
    """Без строки в таблице режим — user. Никогда не по умолчанию admin."""
    conn = connect()
    try:
        _mode_table_ready(conn)
        row = conn.execute(
            "SELECT mode FROM user_mode WHERE telegram_user_id=?", (caller_id,)
        ).fetchone()
        return row["mode"] if row else "user"
    finally:
        conn.close()


def _set_mode(caller_id: str, mode: str) -> None:
    conn = connect()
    try:
        _mode_table_ready(conn)
        conn.execute(
            "INSERT INTO user_mode(telegram_user_id, mode, set_at) VALUES (?, ?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET mode=excluded.mode, set_at=excluded.set_at",
            (caller_id, mode, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def _admin_mode(caller_id: str, args: list, is_admin_caller: bool) -> str:
    """mode | mode user | mode admin. Модель не может передать режим параметром —
    он всегда читается/пишется по caller_id из ContextVar."""
    action = args[0] if args else None
    if action is None:
        return json.dumps({"text": f"Режим: {_get_mode(caller_id)}"}, ensure_ascii=False)
    if action == "user":
        _set_mode(caller_id, "user")
        return json.dumps({"text": "Режим: user"}, ensure_ascii=False)
    if action == "admin":
        if not is_admin_caller:
            return json.dumps({"error": "Not authorized"}, ensure_ascii=False)
        _set_mode(caller_id, "admin")
        return json.dumps({"text": "Режим: admin"}, ensure_ascii=False)
    return json.dumps({"error": f"Unknown mode action: {action}. Valid: mode, mode user, mode admin"}, ensure_ascii=False)


def _admin_status() -> str:
    """User count, table rows, schema version."""
    conn = None
    try:
        conn = connect()
        user_count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]

        tables = ["users", "body_metrics", "food_log", "water_log", "glucose_log", "anthropometry", "med_log", "alerts", "milestones"]
        rows = {}
        for table in tables:
            c = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            rows[table] = c

        text = f"Users: {user_count}\n"
        for table in tables:
            text += f"{table}: {rows[table]}\n"
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        text += f"Schema: v{row['version'] if row else '?'}"

        return json.dumps({"text": text}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    finally:
        if conn:
            conn.close()


def _admin_guards() -> str:
    """Детальный статус по всем гардрейлам с обоснованиями."""
    conn = None
    try:
        conn = connect()
        user_id = _get_user_id({}, conn=conn)
        from health_core.guards import get_guards_status
        status_list = get_guards_status(conn, user_id)

        active = [g for g in status_list if g["status"] in ("critical", "warning")]
        ok_count = sum(1 for g in status_list if g["status"] == "ok")
        nodata_count = sum(1 for g in status_list if g["status"] == "nodata")

        lines = ["🛡️ Мониторинг гардрейлов безопасности:"]
        if active:
            lines.append(f"\n⚠️ Активные предупреждения ({len(active)}):")
            for a in active:
                badge = "🔴 КРИТИЧНО" if a["status"] == "critical" else "🟡 ВНИМАНИЕ"
                lines.append(f"\n{badge}: {a['name']} ({a['code']})")
                lines.append(f"  • Зафиксировано: {a['current_val']} (порог: {a['threshold_val']})")
                lines.append(f"  • Сообщение: {a['message']}")
                lines.append(f"  • 🧬 Обоснование: {a['rationale']}")
                lines.append(f"  • 🎯 Действие: {a['action']}")
        else:
            lines.append("\n🟢 Все активные правила в пределах безопасных норм.")

        lines.append(f"\nСводка: 🟢 В норме: {ok_count} | ⚠️ Отклонений: {len(active)} | ⚪ Нет данных: {nodata_count}")
        return json.dumps({"text": "\n".join(lines)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    finally:
        if conn:
            conn.close()


def _admin_targets() -> str:
    """Current thresholds from config.yaml."""
    try:
        import yaml
        cfg_path = Path(config.CONFIG_PATH)
        with open(cfg_path, encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}

        text = "Targets from config.yaml:\n"
        for block_name in ["policy", "guards"]:
            if block_name in cfg:
                text += f"\n{block_name}:\n"
                for k, v in cfg[block_name].items():
                    text += f"  {k}: {v}\n"

        return json.dumps({"text": text}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _admin_set(key: str, value: str) -> str:
    """Atomic write to config.yaml, validate, and keep .bak."""
    try:
        import yaml
        from pathlib import Path
        import re
        import shutil
        import tempfile

        cfg_path = Path(config.CONFIG_PATH)
        raw = cfg_path.read_text(encoding="utf-8")
        cfg = yaml.safe_load(raw) or {}

        # Правим только ключи, которые уже есть. Новых из чата не заводим.
        section_to_update = None
        for section in ("policy", "guards", "ingest", "backup", "admin"):
            if section in cfg and key in cfg[section]:
                section_to_update = section
                break
        if section_to_update is None:
            return json.dumps({"error": f"Key '{key}' not found in config.yaml"}, ensure_ascii=False)

        try:
            parsed_value = yaml.safe_load(value)
        except yaml.YAMLError:
            parsed_value = value

        # Построчная правка, а не yaml.dump: dump пересобирает файл и уничтожает
        # ВСЕ комментарии, а в них описан смысл и единица каждого порога.
        # Меняем значение в найденной строке, инлайновый комментарий сохраняем.
        pat = re.compile(r"^(\s*" + re.escape(key) + r":[ \t]*)([^#\n]*)(#.*)?$")
        lines = raw.split("\n")
        hits = [i for i, ln in enumerate(lines) if pat.match(ln)]
        if len(hits) != 1:
            return json.dumps(
                {"error": f"Key '{key}' matched {len(hits)} lines, refusing to guess"},
                ensure_ascii=False)
        m = pat.match(lines[hits[0]])
        tail = ("  " + m.group(3)) if m.group(3) else ""
        lines[hits[0]] = f"{m.group(1)}{value}{tail}".rstrip()
        new_raw = "\n".join(lines)

        # Проверяем ДО подмены: испорченный config.yaml кладёт систему целиком.
        try:
            check = yaml.safe_load(new_raw)
        except yaml.YAMLError as e:
            return json.dumps({"error": f"Invalid YAML after update: {e}"}, ensure_ascii=False)
        if check.get(section_to_update, {}).get(key) != parsed_value:
            return json.dumps(
                {"error": f"Post-write check failed: {key} != {parsed_value!r}"},
                ensure_ascii=False)

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cfg_path.parent,
                                         delete=False, suffix=".tmp") as tmp:
            tmp.write(new_raw)
            tmp_path = tmp.name
        try:
            shutil.copy2(cfg_path, cfg_path.with_suffix(".bak"))
            shutil.move(tmp_path, cfg_path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        config.load.cache_clear()   # load() под lru_cache, иначе правка не применится

        return json.dumps({"text": f"Updated {key} to {parsed_value}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _admin_milestone_add(name: str, metric: str, threshold: str, deadline: str = None) -> str:
    """Add or update milestone using existing set_milestone."""
    conn = None
    try:
        threshold_val = float(threshold)
        conn = connect()
        user_id = _get_user_id({}, conn=conn)
        params = {
            "user_id": user_id,
            "name": name,
            "metric": metric,
            "threshold": threshold_val,
        }
        if deadline:
            params["deadline"] = deadline
        conn.close()
        return handle_set_milestone(params)
    except Exception as e:
        if conn:
            conn.close()
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _admin_milestone_del(name: str) -> str:
    """Delete milestone."""
    conn = None
    try:
        conn = connect()
        user_id = _get_user_id({}, conn=conn)
        conn.execute("DELETE FROM milestones WHERE user_id=? AND name=?", (user_id, name))
        conn.commit()
        count = conn.total_changes

        if count == 0:
            return json.dumps({"text": f"Milestone '{name}' not found"}, ensure_ascii=False)
        return json.dumps({"text": f"Deleted milestone '{name}'"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    finally:
        if conn:
            conn.close()


def _admin_milestones() -> str:
    """List all milestones."""
    conn = None
    try:
        conn = connect()
        user_id = _get_user_id({}, conn=conn)
        rows = conn.execute(
            "SELECT name, metric, threshold, deadline, achieved_at FROM milestones WHERE user_id=? ORDER BY name",
            (user_id,)
        ).fetchall()

        if not rows:
            return json.dumps({"text": "No milestones"}, ensure_ascii=False)

        text = "Milestones:\n"
        for r in rows:
            text += f"  {r['name']}: {r['metric']}={r['threshold']}"
            if r['deadline']:
                text += f" (deadline {r['deadline']})"
            if r['achieved_at']:
                text += f" [achieved {r['achieved_at']}]"
            text += "\n"

        return json.dumps({"text": text}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    finally:
        if conn:
            conn.close()


def _admin_recalc(recalc_date: str = None) -> str:
    """Recalculate daily_target for a date."""
    conn = None
    try:
        from health_core.energy import daily_target

        conn = connect()
        date_str = recalc_date or _today_iso()

        # Resolve caller's user id
        user_id = _get_user_id({}, conn=conn)
        # Recalculate calls the energy module which updates daily_targets
        result = daily_target(conn, user_id, date_str)
        conn.commit()

        return json.dumps({"text": f"Recalculated targets for {date_str}: {result}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    finally:
        if conn:
            conn.close()


def _admin_export() -> str:
    """Run export_all."""
    conn = None
    try:
        import os
        from health_core.export import export_all
        from health_core.db import DB_PATH

        conn = connect()
        user_id = _get_user_id({}, conn=conn)
        out_dir = os.environ.get("HEALTH_EXPORT_DIR", str(DB_PATH.parent / "export"))
        result = export_all(conn, user_id, out_dir)
        return json.dumps({"text": f"Export complete: {result}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    finally:
        if conn:
            conn.close()


def _admin_backup() -> str:
    """Run backup_db."""
    try:
        from health_core.export import backup_db
        result = backup_db()
        return json.dumps({"text": f"Backup complete: {result}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _admin_alerts(n: int = 10) -> str:
    """Last n alerts."""
    conn = None
    try:
        conn = connect()
        user_id = _get_user_id({}, conn=conn)
        rows = conn.execute(
            "SELECT created_at, rule, message FROM alerts WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, n)
        ).fetchall()

        if not rows:
            return json.dumps({"text": f"No alerts (last {n})"}, ensure_ascii=False)

        text = f"Last {min(n, len(rows))} alerts:\n"
        for r in rows:
            text += f"  {r['created_at']} {r['rule']}: {r['message']}\n"

        return json.dumps({"text": text}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------- SLASH COMMANDS
#
# Отдельный API Hermes (ctx.register_command), не register_tool: хендлер берёт
# ОДНУ строку raw_args и обязан вернуть человеческий текст, а не JSON — modель
# тут не участвует. Ниже — тонкие обёртки над уже существующими handle_*:
# разбираем raw_args, зовём handle_*, разворачиваем JSON в текст.


def _unwrap(json_str: str, key: str | None = None):
    """JSON-ответ handle_* -> человеческий текст. Ошибка — всегда как есть,
    "⚠ …". key задан — отдаёт это поле строкой. key не задан — отдаёт весь
    dict (вызывающий сам решает, что с ним делать, как в /importdata)."""
    d = json.loads(json_str)
    if "error" in d:
        return f"⚠ {d['error']}"
    if key is not None:
        return str(d.get(key, d))
    return d


def cmd_health(raw_args: str) -> str:
    """/health — что умеет система. Не /health, а не /help: /help — встроенная
    команда Hermes, её имя занято и регистрация под ним молча пропускается
    (см. STEP registration guard в register())."""
    return _unwrap(handle_help({}), "help")


_STYLE_USAGE = (
    "Использование:\n"
    "/style — список стилей\n"
    "/style use <имя> — переключить активный\n"
    "/style add <имя> <текст инструкции> — добавить или обновить\n"
    "/style del <имя> [УДАЛИТЬ] — удалить (сначала без слова — подтверждение)"
)


def cmd_style(raw_args: str) -> str:
    """/style list|use|add|del — та же логика, что и у handle_style, просто
    raw_args разобран в params."""
    parts = (raw_args or "").strip().split(maxsplit=1)
    head = parts[0].lower() if parts else "list"
    rest = parts[1] if len(parts) > 1 else ""

    if head == "list":
        return _unwrap(handle_style({"action": "list"}), "styles")
    if head == "use":
        name = rest.strip()
        if not name:
            return _STYLE_USAGE
        return _unwrap(handle_style({"action": "use", "name": name}), "result")
    if head == "add":
        name_instr = rest.split(maxsplit=1)
        if len(name_instr) < 2:
            return _STYLE_USAGE
        name, instruction = name_instr
        return _unwrap(
            handle_style({"action": "add", "name": name, "instruction": instruction}), "result")
    if head == "del":
        del_parts = rest.split()
        if not del_parts:
            return _STYLE_USAGE
        name = del_parts[0]
        params = {"action": "del", "name": name}
        if len(del_parts) > 1 and del_parts[1] == "УДАЛИТЬ":
            params["confirm"] = "УДАЛИТЬ"
        return _unwrap(handle_style(params), "result")
    return _STYLE_USAGE


def cmd_mode(raw_args: str) -> str:
    """/mode [user|admin] — делегирует allowlist-гейт handle_admin_cmd целиком,
    он security-relevant и живёт ровно в одном месте."""
    args = (raw_args or "").strip()
    command = "mode" if not args else f"mode {args}"
    return _unwrap(handle_admin_cmd({"command": command}), "text")


def cmd_users(raw_args: str) -> str:
    """/users — персоны и объёмы их логов, только admin. Гейт тот же самый, что и
    у admin_cmd, — общая _admin_denial, а не зонд чужой подкомандой."""
    caller_id = _caller_telegram_id()
    if not caller_id:
        return "⚠ Не удалось определить, кто спрашивает."
    admin_ids = [str(i) for i in (config.load().get("admin", {}) or {}).get("telegram_admin_ids", [])]
    denial = _admin_denial(caller_id, caller_id in admin_ids)
    if denial is not None:
        return f"⚠ {denial}"

    conn = connect()
    migrate(conn)
    rows = conn.execute(
        "SELECT u.id, u.telegram_user_id, u.created_at, "
        "(SELECT COUNT(*) FROM food_log f WHERE f.user_id=u.id) AS food_n, "
        "(SELECT COUNT(*) FROM body_metrics b WHERE b.user_id=u.id) AS body_n "
        "FROM users u ORDER BY u.id"
    ).fetchall()
    conn.close()

    if not rows:
        return "Персон нет."
    lines = ["👥 **Персоны**", ""]
    for r in rows:
        lines.append(
            f"#{r['id']} · tg {r['telegram_user_id']} · рег. {r['created_at']} · "
            f"еда {r['food_n']} · тело {r['body_n']}"
        )
    return "\n".join(lines)


# Логи стираются, конфигурация остаётся: только то, что человек НАЛОГИРОВАЛ во
# времени (события), не то, что он НАСТРОИЛ (профиль users, user_targets,
# вехи, стили, холодильник, планы, расписание лекарств) — настройки переживают
# /wipe. Персона (users) не удаляется вовсе: удаление персоны — web-admin.
_WIPE_TABLES = (
    "food_log", "water_log", "glucose_log", "body_metrics", "anthropometry",
    "activity", "med_log", "alerts", "daily_targets", "import_log",
    "llm_calls", "refeed_days", "sick_days", "lab_results", "plan_log",
    "side_effects", "my_products",
)
_WIPE_CONFIRM = "УДАЛИТЬ"


def _wipe_counts(conn, user_id: int) -> dict:
    counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t} WHERE user_id=?", (user_id,))
                          .fetchone()["c"]
              for t in _WIPE_TABLES}
    # food_items у своей user_id-колонки нет — считаем через join по food_log_id.
    # ON DELETE CASCADE (foreign_keys=ON, health_core/db.py) стирает их вместе
    # с food_log, отдельного DELETE не нужно — только отдельный счётчик для отчёта.
    counts["food_items"] = conn.execute(
        "SELECT COUNT(*) c FROM food_items fi JOIN food_log f ON f.id=fi.food_log_id "
        "WHERE f.user_id=?", (user_id,),
    ).fetchone()["c"]
    return counts


def cmd_wipe(raw_args: str) -> str:
    """/wipe [УДАЛИТЬ] — стереть СВОИ логи. Без слова подтверждения — только
    предпросмотр счётчиков, ничего не удаляется."""
    conn = connect()
    migrate(conn)
    user_id = _get_user_id({}, conn)
    counts = _wipe_counts(conn, user_id)
    total = sum(counts.values())

    if (raw_args or "").strip() != _WIPE_CONFIRM:
        conn.close()
        if total == 0:
            return "Стирать нечего — логи уже пусты."
        lines = ["⚠ Будет удалено:", ""]
        lines += [f"• {t}: {c}" for t, c in counts.items() if c]
        lines += ["", f"Для подтверждения: /wipe {_WIPE_CONFIRM}"]
        return "\n".join(lines)

    try:
        for t in _WIPE_TABLES:
            conn.execute(f"DELETE FROM {t} WHERE user_id=?", (user_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return f"⚠ Удаление отменено из-за ошибки: {e}"
    conn.close()

    if total == 0:
        return "Удалено: 0 (логов и не было)."
    lines = ["✅ Удалено:", ""] + [f"• {t}: {c}" for t, c in counts.items() if c]
    return "\n".join(lines)


def cmd_week(raw_args: str) -> str:
    """/week — недельная сводка одним блоком."""
    return _unwrap(handle_get_weekly_summary({}), "weekly_summary")


def cmd_importdata(raw_args: str) -> str:
    """/importdata <путь> — импорт выгрузки весов/тренировок с диска СЕРВЕРА,
    не файла, присланного в чат Telegram (Hermes не подкладывает такие файлы
    боту на диск сам)."""
    path = (raw_args or "").strip()
    if not path:
        return ("Использование: /importdata <путь к файлу>\n"
                 "Путь — на СЕРВЕРЕ, где работает бот, либо на Google Диске: "
                 "gdrive:Health/Scale/export.xlsx. Файл, присланный в чат "
                 "Telegram, так не сработает — сначала положите его на диск сервера.")
    result = _unwrap(handle_import_scale_export({"file_path": path}))
    if isinstance(result, str):
        return result
    return (f"Импорт: добавлено {result.get('added', 0)}, "
            f"пропущено {result.get('skipped', 0)}, пачек {result.get('bursts', 0)}")


_SLASH_COMMANDS = [
    ("health", cmd_health, "Что умеет система и как с ней говорить"),
    ("style", cmd_style, "Стили ответа: list / add / use / del"),
    ("mode", cmd_mode, "Режим: user или admin"),
    ("users", cmd_users, "Список персон (только admin)"),
    ("wipe", cmd_wipe, "Очистить мои записи (нужно подтверждение)"),
    ("importdata", cmd_importdata, "Импорт выгрузки весов: /importdata <путь или gdrive:...>"),
    ("week", cmd_week, "Недельная сводка: вес, состав, дисциплина, гарды"),
]


def register(ctx):
    """Register all tools with Hermes."""
    try:
        from . import schemas
    except ImportError:
        # Fallback for standalone execution
        import schemas

    tools = [
        ("log_food", handle_log_food, schemas.log_food_schema),
        ("food_lookup", handle_food_lookup, schemas.food_lookup_schema),
        ("log_water", handle_log_water, schemas.log_water_schema),
        ("log_glucose", handle_log_glucose, schemas.log_glucose_schema),
        ("log_side_effect", handle_log_side_effect, schemas.log_side_effect_schema),
        ("log_watch_day", handle_log_watch_day, schemas.log_watch_day_schema),
        ("council", handle_council, schemas.council_schema),
        ("log_labs", handle_log_labs, schemas.log_labs_schema),
        ("log_sleep", handle_log_sleep, schemas.log_sleep_schema),
        ("log_weight", handle_log_weight, schemas.log_weight_schema),
        ("log_anthropometry", handle_log_anthropometry, schemas.log_anthropometry_schema),
        ("log_med", handle_log_med, schemas.log_med_schema),
        ("pharma", handle_pharma, schemas.pharma_schema),
        ("drug_card_draft", handle_drug_card_draft, schemas.drug_card_draft_schema),
        ("plans", handle_plans, schemas.plans_schema),
        ("import_scale_export", handle_import_scale_export, schemas.import_scale_export_schema),
        ("get_day_summary", handle_get_day_summary, schemas.get_day_summary_schema),
        ("get_trends", handle_get_trends, schemas.get_trends_schema),
        ("get_status_bar", handle_get_status_bar, schemas.get_status_bar_schema),
        ("get_evening_report", handle_get_evening_report, schemas.get_evening_report_schema),
        ("help", handle_help, schemas.help_schema),
        ("get_weekly_summary", handle_get_weekly_summary, schemas.get_weekly_summary_schema),
        ("query_metrics", handle_query_metrics, schemas.query_metrics_schema),
        ("query_food", handle_query_food, schemas.query_food_schema),
        ("pantry", handle_pantry, schemas.pantry_schema),
        ("equipment", handle_equipment, schemas.equipment_schema),
        ("plan_day", handle_plan_day, schemas.plan_day_schema),
        ("log_workout", handle_log_workout, schemas.log_workout_schema),
        ("refeed", handle_refeed, schemas.refeed_schema),
        ("sick", handle_sick, schemas.sick_schema),
        ("forecast", handle_forecast, schemas.forecast_schema),
        ("style", handle_style, schemas.style_schema),
        ("explain_target", handle_explain_target, schemas.explain_target_schema),
        ("get_progress", handle_get_progress, schemas.get_progress_schema),
        ("register_user", handle_register_user, schemas.register_user_schema),
        ("set_milestone", handle_set_milestone, schemas.set_milestone_schema),
        ("admin_cmd", handle_admin_cmd, schemas.admin_cmd_schema),
    ]

    for name, handler, schema in tools:
        ctx.register_tool(name=name, toolset="health", schema=schema, handler=handler)

    # Личность звонящего для слэш-команд — см. _remember_caller. Без этого хука
    # любая слэш-команда работает над user_id=1, кто бы её ни позвал.
    ctx.register_hook("pre_gateway_dispatch", _remember_caller)

    # register_command — отдельный, более новый API, чем register_tool. Сборка
    # Hermes без него не должна ронять регистрацию обычных тулов выше, поэтому
    # гейт hasattr, а не жёсткий вызов.
    if hasattr(ctx, "register_command"):
        for name, handler, description in _SLASH_COMMANDS:
            ctx.register_command(name, handler, description=description)


# ---------------------------------------------------------------- VERIFICATION

if __name__ == "__main__":
    import os
    import tempfile
    from pathlib import Path

    # Setup temp database (Windows sometimes has issues releasing file handles)
    tmp = tempfile.mkdtemp()
    try:
        os.environ["HEALTH_DB"] = str(Path(tmp) / "health.db")

        # Reimport to pick up the temp DB path
        import importlib
        import health_core.db as db
        importlib.reload(db)
        db.DB_PATH = Path(os.environ["HEALTH_DB"])

        # Create test database
        conn = db.connect()
        db.migrate(conn)

        # Create test user
        conn.execute(
            "INSERT INTO users(telegram_user_id, height_cm, birth_date, sex, created_at) "
            "VALUES (1, 185, '1992-08-09', 'm', '2026-08-20 00:00:00')"
        )
        uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]

        # Create initial body metric
        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg) "
            "VALUES (?, 'burst-0', '2026-08-20 07:00:00', 80.0)",
            (uid,),
        )

        # Create daily targets
        from datetime import date
        today = date.today().isoformat()
        conn.execute(
            "INSERT INTO daily_targets(user_id, date, kcal_target, protein_g_target, water_ml_target) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, today, 2200, 150, 3000),
        )

        # Create user targets
        conn.execute(
            "INSERT INTO user_targets(user_id, valid_from, water_ml) VALUES (?, ?, ?)",
            (uid, today, 3000),
        )
        conn.commit()

        print("\n" + "="*60)
        print("TEST 1: Registration")
        print("="*60)

        # Test registration
        class FakeCtx:
            def __init__(self):
                self.tools = {}
                self.hooks = {}
                self.commands = {}

            def register_tool(self, name, toolset, schema, handler):
                self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}

            def register_hook(self, name, handler):
                self.hooks[name] = handler

            def register_command(self, name, handler, description="", args_hint=""):
                self.commands[name] = {"handler": handler, "description": description}

        ctx = FakeCtx()
        register(ctx)

        expected_tools = {
            "log_food", "food_lookup", "log_water", "log_glucose", "log_side_effect", "log_watch_day", "log_labs", "log_sleep", "log_weight",
            "equipment", "plan_day", "log_workout", "refeed", "sick", "forecast",
            "log_anthropometry", "log_med", "pharma", "drug_card_draft", "plans", "import_scale_export",
            "get_day_summary", "get_trends", "get_status_bar",
            "query_metrics", "query_food", "pantry", "style", "explain_target", "get_progress",
            "get_evening_report", "register_user", "set_milestone", "admin_cmd", "help",
            "get_weekly_summary", "council",
        }

        registered_tools = set(ctx.tools.keys())
        assert registered_tools == expected_tools, (
            f"не хватает: {expected_tools - registered_tools}, "
            f"лишние: {registered_tools - expected_tools}")

        for name, info in ctx.tools.items():
            assert info["schema"] is not None, f"{name} has no schema"
            assert callable(info["handler"]), f"{name} handler is not callable"

        print(f"OK: All {len(expected_tools)} tools registered with schemas and handlers")
        print(f"Tools: {', '.join(sorted(expected_tools))}")

        print("\n" + "="*60)
        print("TEST 2: log_water handler")
        print("="*60)

        handler = ctx.tools["log_water"]["handler"]
        result_json = handler({"ml": 500, "user_id": uid})
        result = json.loads(result_json)

        assert "alerts" in result, f"Missing 'alerts' key in result: {result}"
        assert isinstance(result["alerts"], list), f"alerts should be list, got {type(result['alerts'])}"
        assert "water_ml" in result, f"Missing 'water_ml' in result: {result}"
        assert result["water_ml"] == 500, f"Expected water_ml=500, got {result['water_ml']}"

        print(f"OK: log_water returned valid JSON with alerts")
        print(f"Result: {result}")

        print("\n" + "="*60)
        print("TEST 2b: log_labs handler")
        print("="*60)

        handler = ctx.tools["log_labs"]["handler"]
        result_json = handler({
            "action": "add",
            "taken_on": "2026-09-01",
            "markers": {"глюкоза": 5.0, "инсулин": 10, "foo": 1},
            "user_id": uid
        })
        result = json.loads(result_json)

        assert "saved" in result, f"Missing 'saved' key in result: {result}"
        assert "rejected" in result, f"Missing 'rejected' key in result: {result}"
        assert "derived" in result, f"Missing 'derived' key in result: {result}"
        assert result["saved"].get("glucose") == 5.0, f"Expected glucose saved, got {result['saved']}"
        assert result["saved"].get("insulin") == 10, f"Expected insulin saved, got {result['saved']}"
        assert "foo" in result["rejected"], f"Expected 'foo' to be rejected, got {result['rejected']}"
        assert "homa_ir" in result["derived"], f"Expected homa_ir in derived, got {result['derived']}"
        homa_value = result["derived"]["homa_ir"]["value"]
        assert abs(homa_value - 2.22) < 0.02, f"Expected HOMA-IR ≈ 2.22, got {homa_value}"

        print(f"OK: log_labs add with valid/invalid markers and derived HOMA-IR")

        # Test list action
        result_json = handler({"action": "list", "limit": 10, "user_id": uid})
        result = json.loads(result_json)
        assert "labs" in result, f"Missing 'labs' key in list result: {result}"
        assert isinstance(result["labs"], list), f"Expected labs to be list, got {type(result['labs'])}"
        assert len(result["labs"]) >= 1, f"Expected at least 1 lab record, got {len(result['labs'])}"

        lab_id = result["labs"][0]["id"]
        print(f"OK: log_labs list returns history")

        # Test delete action
        result_json = handler({"action": "delete", "lab_id": lab_id, "user_id": uid})
        result = json.loads(result_json)
        assert "ok" in result, f"Expected 'ok' in delete result, got {result}"
        assert f"#{lab_id}" in result["ok"], f"Expected lab_id in ok message"

        print(f"OK: log_labs delete removes a record")

        print("\n" + "="*60)
        print("TEST 3: get_status_bar byte-identity")
        print("="*60)

        # Get status bar from handler.
        # format="bar" ЯВНО: умолчание теперь dashboard, а побайтовую сверку с §07
        # имеет смысл делать только против короткого бара.
        handler = ctx.tools["get_status_bar"]["handler"]
        result_json = handler({"user_id": uid, "format": "bar"})
        result = json.loads(result_json)
        if "error" in result:
            print(f"ERROR in get_status_bar: {result['error']}")
            raise AssertionError(f"get_status_bar returned error: {result['error']}")
        handler_bar = result["status_bar"]

        # Get status bar directly from report module
        from health_core.report import status_bar as direct_status_bar
        direct_bar = direct_status_bar(conn, uid)

        # Compare bytes
        handler_bytes = handler_bar.encode("utf-8")
        direct_bytes = direct_bar.encode("utf-8")

        assert handler_bytes == direct_bytes, (
            f"Status bars not byte-identical!\n"
            f"Handler:\n{handler_bar!r}\n"
            f"Direct:\n{direct_bar!r}"
        )

        print("OK: get_status_bar is byte-identical to report.status_bar()")
        print(f"Status bar:\n{handler_bar}")

        print("\n" + "="*60)
        print("TEST 3b: get_status_bar format=dashboard / bar / default")
        print("="*60)

        # Явный format="bar" — побайтово §07.
        result_bar_json = handler({"user_id": uid, "format": "bar"})
        result_bar = json.loads(result_bar_json)
        assert result_bar["status_bar"].encode("utf-8") == direct_bytes, \
            "format='bar' must stay byte-identical to report.status_bar()"
        assert "dashboard" not in result_bar, \
            "format='bar' не должен отдавать второй ключ — выбор формата за инструментом"

        # format="dashboard" ЗАМЕНЯЕТ содержимое status_bar, а не добавляет соседний
        # ключ. Иначе модель получает выбор и стабильно берёт короткий бар.
        result_dash_json = handler({"user_id": uid, "format": "dashboard"})
        result_dash = json.loads(result_dash_json)
        assert set(result_dash) == {"status_bar", "style"}, \
            f"dashboard должен отдавать status_bar + style, получено {list(result_dash)}"
        dashboard = result_dash["status_bar"]
        assert dashboard.encode("utf-8") != direct_bytes, \
            "format='dashboard' обязан отличаться от короткого бара"

        # Умолчание = dashboard: человек, спросивший состояние, ждёт панель.
        result_def = json.loads(handler({"user_id": uid}))
        assert result_def["status_bar"] == dashboard, \
            "умолчание должно совпадать с format='dashboard'"

        assert "```" not in dashboard, f"dashboard must be plain markdown, not fenced: {dashboard!r}"
        assert "📊" in dashboard and "**Вес**" in dashboard, \
            f"dashboard must open with the status header and a bold Вес line: {dashboard!r}"

        print("OK: bar побайтово §07, dashboard заменяет его и стоит умолчанием")
        print(f"Dashboard:\n{dashboard}")

        print("\n" + "="*60)
        print("TEST 3c: get_status_bar format=dashboard on an EMPTY user")
        print("="*60)

        conn.execute(
            "INSERT INTO users(telegram_user_id, created_at) VALUES (424242, ?)",
            (_now_iso(),),
        )
        conn.commit()
        empty_uid = conn.execute(
            "SELECT id FROM users WHERE telegram_user_id=424242"
        ).fetchone()["id"]

        result_empty_json = handler({"user_id": empty_uid, "format": "dashboard"})
        result_empty = json.loads(result_empty_json)
        assert "error" not in result_empty, f"Empty-user dashboard raised: {result_empty}"
        empty_dash = result_empty["status_bar"]  # dashboard приходит под этим ключом
        assert "—" in empty_dash, "Missing weight on empty user should render as —, not be silently dropped"
        # "0 ккал" от съеденного за день — правда (ничего не съедено), не выдумка,
        # и по правилу 4 нутриент без цели уходит в "Без цели" со своим реальным
        # значением. Фабрикацией был бы 0 у ИЗМЕРЕНИЯ тела, которого не было —
        # это и проверяем: вес/жир%/BMR не должны появиться как нулевые заглушки.
        assert "0.0 кг" not in empty_dash and "0 %" not in empty_dash and "BMR 0" not in empty_dash, \
            f"Empty user must never show a fabricated 0 in place of a missing measurement: {empty_dash!r}"

        print("OK: dashboard on empty user renders — with no fabricated 0, does not raise")
        print(f"Dashboard:\n{empty_dash}")

        print("\n" + "="*60)
        print("TEST 3d: full body composition, complete profile, base_weight set")
        print("="*60)

        conn.execute(
            "INSERT INTO users(telegram_user_id, height_cm, birth_date, sex, base_weight_kg, created_at) "
            "VALUES (555001, 170, '1990-05-05', 'f', 90.0, ?)",
            (_now_iso(),),
        )
        conn.commit()
        full_uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=555001").fetchone()["id"]
        # Первый (более ранний) замер — точка отсчёта для динамики состава тела.
        # Разные числа, чтобы Δ в разделе "Состав тела" были реально ненулевыми,
        # а не "без изменений от старта" на единственной точке.
        earlier = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, fat_pct, "
            "muscle_mass_kg, ffm_kg, visceral_fat) VALUES (?, 'full-0', ?, 90.0, 33.0, 42.0, 51.0, 15)",
            (full_uid, earlier),
        )
        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, fat_pct, "
            "muscle_mass_kg, ffm_kg, visceral_fat) VALUES (?, 'full-1', ?, 75.3, 28.4, 45.2, 54.0, 12)",
            (full_uid, _now_iso()),
        )
        conn.commit()

        result_full = json.loads(handler({"user_id": full_uid, "format": "dashboard"}))
        assert "error" not in result_full, f"Full-composition dashboard raised: {result_full}"
        full_dash = result_full["status_bar"]
        for expected in ("75.3 кг", f"{MINUS}14.7 кг", "28.4 %", "45.2 кг", "54.0 кг", "12"):
            assert expected in full_dash, f"Missing '{expected}' in full-composition dashboard:\n{full_dash}"
        assert "Нет в профиле" not in full_dash, f"Complete profile must not show missing-fields line:\n{full_dash}"
        # Раздел "Состав тела" обязан показывать динамику (Δ от старта), а не
        # только последнюю цифру — это и было явной жалобой владельца.
        assert "🧬 **Состав тела**" in full_dash, f"Body-composition section header missing:\n{full_dash}"
        # 28.4-33.0, 45.2-42.0, 54.0-51.0, 12-15 — числа реальные (2 разных замера), не заглушки.
        for expected_dyn in (_signed(-4.6, 1, "%"), _signed(3.2, 1, "кг"), _signed(3.0, 1, "кг"),
                             _signed(-3, 0, "").rstrip()):
            assert expected_dyn in full_dash, f"Missing Δ '{expected_dyn}' in Состав тела:\n{full_dash}"
        assert "(Δ " in full_dash and "от старта)" in full_dash, \
            f"Состав тела must show a numeric Δ-from-baseline, not just the latest reading:\n{full_dash}"
        # Спарклайн по двум разным дням: минимум/максимум различаются, период назван.
        assert "📈 **Вес за период**" in full_dash and "мин 75.3 кг" in full_dash and "макс 90.0 кг" in full_dash, \
            f"Weight sparkline section with min/max labels missing:\n{full_dash}"

        print("OK: every seeded body field renders, Δ от старта = −14.7 кг, состав тела с динамикой, "
              "спарклайн с мин/макс, no missing-profile line")
        print(f"Dashboard:\n{full_dash}")

        print("\n" + "="*60)
        print("TEST 3e: zero-trap — visceral_fat=0 is real, fat_pct=0 is treated as absent")
        print("="*60)

        conn.execute(
            "INSERT INTO users(telegram_user_id, height_cm, birth_date, sex, created_at) "
            "VALUES (555002, 170, '1990-05-05', 'f', ?)",
            (_now_iso(),),
        )
        conn.commit()
        zero_uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=555002").fetchone()["id"]
        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, fat_pct, visceral_fat) "
            "VALUES (?, 'zero-1', ?, 70.0, 0, 0)",
            (zero_uid, _now_iso()),
        )
        conn.commit()

        result_zero = json.loads(handler({"user_id": zero_uid, "format": "dashboard"}))
        zero_dash = result_zero["status_bar"]
        # Формат сменился (icon-per-line убран), но правило то же: fat_pct=0 не
        # рисует строку "Жир", visceral_fat=0 рисует "0" буквально.
        assert "Жир:" not in zero_dash, f"fat_pct=0 must be treated as absent, not shown: {zero_dash!r}"
        assert "Висцеральный жир: 0" in zero_dash, \
            f"visceral_fat=0 must render literal 0, not be omitted: {zero_dash!r}"

        print("OK: visceral_fat=0 renders as 0, fat_pct=0 omits its row")

        print("\n" + "="*60)
        print("TEST 3f: BMR/Δ omitted for a gap profile, missing-fields line names exactly the gap")
        print("="*60)

        conn.execute(
            "INSERT INTO users(telegram_user_id, sex, created_at) VALUES (555003, 'm', ?)",
            (_now_iso(),),
        )
        conn.commit()
        gap_uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=555003").fetchone()["id"]
        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg) VALUES (?, 'gap-1', ?, 88.0)",
            (gap_uid, _now_iso()),
        )
        conn.commit()

        result_gap = json.loads(handler({"user_id": gap_uid, "format": "dashboard"}))
        gap_dash = result_gap["status_bar"]
        assert "BMR" not in gap_dash, f"BMR must be omitted without height/birth_date: {gap_dash!r}"
        # Стартовый вес выводится из единственного взвешивания (88.0) — точка
        # отсчёта совпадает с ним самим, поэтому Δ показывается и равна 0.0.
        assert "Δ от старта" in gap_dash, f"Δ must derive from the sole weigh-in: {gap_dash!r}"
        assert "Нет в профиле:" in gap_dash, f"missing-fields line must appear: {gap_dash!r}"
        for word in ("рост", "дата рождения"):
            assert word in gap_dash, f"missing-fields line must name '{word}': {gap_dash!r}"
        assert "стартовый вес" not in gap_dash, \
            f"base weight resolved from body_metrics — must not be listed missing: {gap_dash!r}"
        gap_user_row = conn.execute(
            "SELECT height_cm, birth_date, sex, base_weight_kg FROM users WHERE id=?", (gap_uid,)
        ).fetchone()
        # Статическая проверка (без разрешённого веса) по-прежнему числит стартовый вес.
        assert _missing_profile_fields(gap_user_row) == ["рост", "дата рождения", "стартовый вес"], \
            f"sex is set here — must not be listed as missing: {_missing_profile_fields(gap_user_row)}"
        # С разрешённым из body_metrics весом — остаются только рост и дата рождения.
        assert _missing_profile_fields(gap_user_row, 88.0) == ["рост", "дата рождения"], \
            f"resolved base weight must drop 'стартовый вес': {_missing_profile_fields(gap_user_row, 88.0)}"

        print("OK: BMR/Δ omitted without raising, missing-fields line names exactly height/birth/base_weight")
        print(f"Dashboard:\n{gap_dash}")

        print("\n" + "="*60)
        print("TEST 3g: _missing_profile_fields — exact per-field detection")
        print("="*60)

        all_null = {"height_cm": None, "birth_date": None, "sex": None, "base_weight_kg": None}
        assert _missing_profile_fields(all_null) == ["рост", "дата рождения", "пол", "стартовый вес"]

        only_sex = {"height_cm": 170, "birth_date": "1990-01-01", "sex": None, "base_weight_kg": 80.0}
        assert _missing_profile_fields(only_sex) == ["пол"], _missing_profile_fields(only_sex)

        complete = {"height_cm": 170, "birth_date": "1990-01-01", "sex": "f", "base_weight_kg": 80.0}
        assert _missing_profile_fields(complete) == []
        assert _missing_profile_lines(_missing_profile_fields(complete)) == [], \
            "complete profile must render no missing-fields line at all"

        only_base = {"height_cm": 170, "birth_date": "1990-01-01", "sex": "f", "base_weight_kg": None}
        assert _missing_profile_fields(only_base) == ["стартовый вес"]

        for case in (all_null, only_sex, only_base):
            for line in _missing_profile_lines(_missing_profile_fields(case)):
                assert len(line) < 40, f"missing-fields line too wide ({len(line)}): {line!r}"

        print("OK: missing-fields detection exact per field, absent entirely on a complete profile")

        print("\n" + "="*60)
        print("TEST 4: register_user twice with same telegram_user_id")
        print("="*60)

        handler = ctx.tools["register_user"]["handler"]

        # First registration
        result1_json = handler({
            "telegram_user_id": 999,
            "height_cm": 180.0,
            "birth_date": "1990-01-15",
            "sex": "m",
            "timezone": "Europe/Moscow",
            "base_weight_kg": 85.0,
            "base_weight_date": "2026-08-20",
        })
        result1 = json.loads(result1_json)
        assert result1.get("created") is True, f"First registration should have created=true, got {result1}"
        user_id_1 = result1["user_id"]
        targets_1 = result1.get("targets", {})

        # Check user_id exists
        user_check = conn.execute(
            "SELECT id, telegram_user_id FROM users WHERE telegram_user_id=?",
            (999,),
        ).fetchone()
        assert user_check is not None, "User not inserted after register_user"
        assert user_check["id"] == user_id_1, f"User ID mismatch: expected {user_id_1}, got {user_check['id']}"

        print(f"OK: First registration created user_id={user_id_1}")

        print("\n" + "="*60)
        print("TEST 5: register_user returns targets matching config.targets_for()")
        print("="*60)

        # Get targets from handler
        handler_targets = result1.get("targets", {})

        # Get targets from config directly
        config_targets = config.targets_for(conn, user_id_1)

        assert handler_targets == config_targets, (
            f"Targets mismatch:\n"
            f"Handler: {handler_targets}\n"
            f"Config:  {config_targets}"
        )

        print(f"OK: Targets match config.targets_for()")
        print(f"Targets: {config_targets}")

        print("\n" + "="*60)
        print("TEST 6: register_user updates existing user")
        print("="*60)

        # Second registration with same telegram_user_id
        result2_json = handler({
            "telegram_user_id": 999,
            "height_cm": 181.0,
            "birth_date": "1990-01-16",
            "sex": "m",
            "timezone": "Europe/London",
            "base_weight_kg": 86.0,
            "base_weight_date": "2026-08-21",
        })
        result2 = json.loads(result2_json)
        assert result2.get("created") is False, f"Second registration should have created=false, got {result2}"
        user_id_2 = result2["user_id"]
        assert user_id_2 == user_id_1, f"User ID changed on update: {user_id_1} -> {user_id_2}"

        # Verify only one row exists
        count = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE telegram_user_id=?",
            (999,),
        ).fetchone()["c"]
        assert count == 1, f"Expected 1 user row after re-registration, got {count}"

        # Verify update happened
        updated_user = conn.execute(
            "SELECT height_cm FROM users WHERE id=?",
            (user_id_1,),
        ).fetchone()
        assert updated_user["height_cm"] == 181.0, f"User profile not updated"

        print(f"OK: UPSERT works. Created={result1['created']}, Updated={result2['created']}")

        print("\n" + "="*60)
        print("TEST 6b: register_user — no caller id, no params fallback -> error, users unchanged")
        print("="*60)

        count_before = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        result_noid = json.loads(handler({
            "height_cm": 170.0, "birth_date": "1995-01-01", "sex": "f",
            "timezone": "UTC", "base_weight_kg": 60.0, "base_weight_date": "2026-01-01",
        }))
        assert "error" in result_noid and "identity" in result_noid["error"].lower(), \
            f"Expected identity error, got {result_noid}"
        count_after = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        assert count_after == count_before, "users table must stay unchanged on identity failure"
        print(f"OK: refused, users unchanged ({count_after})")

        print("\n" + "="*60)
        print("TEST 6c: register_user takes identity from caller_id, ignores params telegram_user_id")
        print("="*60)

        import plugin.tools as tools_module
        _orig_caller_fn = tools_module._caller_telegram_id
        _real_uid = "777001"
        def mock_real_caller():
            return _real_uid
        tools_module._caller_telegram_id = mock_real_caller
        globals()['_caller_telegram_id'] = mock_real_caller

        result_real = json.loads(handler({
            # No telegram_user_id in params — must still register under caller_id.
            "height_cm": 175.0, "birth_date": "1988-05-05", "sex": "m",
            "timezone": "UTC", "base_weight_kg": 90.0, "base_weight_date": "2026-01-01",
        }))
        assert "error" not in result_real, f"Registration via caller_id failed: {result_real}"
        row = conn.execute("SELECT telegram_user_id FROM users WHERE id=?", (result_real["user_id"],)).fetchone()
        assert str(row["telegram_user_id"]) == _real_uid, \
            f"Row should be keyed by caller_id {_real_uid}, got {row['telegram_user_id']}"
        print(f"OK: register_user keyed row by ContextVar caller_id={_real_uid}")

        print("\n" + "="*60)
        print("TEST 6d: register_user — caller_id + conflicting params telegram_user_id -> ContextVar wins")
        print("="*60)

        result_conflict = json.loads(handler({
            "telegram_user_id": 999999,  # attacker/model-supplied, must be ignored
            "height_cm": 176.0, "birth_date": "1988-05-06", "sex": "m",
            "timezone": "UTC", "base_weight_kg": 91.0, "base_weight_date": "2026-01-02",
        }))
        assert "error" not in result_conflict, f"Registration failed: {result_conflict}"
        assert result_conflict["user_id"] == result_real["user_id"], \
            "Same caller_id must resolve to the same user, regardless of params telegram_user_id"
        conflict_row = conn.execute("SELECT id FROM users WHERE telegram_user_id=999999").fetchone()
        assert conflict_row is None, "params telegram_user_id must never be written when caller_id is present"
        print(f"OK: conflicting params telegram_user_id ignored, ContextVar identity {_real_uid} won")

        tools_module._caller_telegram_id = _orig_caller_fn
        globals()['_caller_telegram_id'] = _orig_caller_fn

        print("\n" + "="*60)
        print("TEST 6e: write handlers must not report success on a zero-effect write")
        print("="*60)

        class _FakeCursor:
            def __init__(self, rowcount):
                self.rowcount = rowcount

        _err = _zero_write_error(_FakeCursor(0), "boom: nothing written")
        assert _err is not None and json.loads(_err)["error"] == "boom: nothing written", \
            f"rowcount=0 must produce an error, got {_err}"
        assert _zero_write_error(_FakeCursor(1), "boom: nothing written") is None, \
            "rowcount=1 must not error"
        print("OK: _zero_write_error refuses to report success for a rowcount=0 write")

        print("\n" + "="*60)
        print("TEST 7: set_milestone twice with same name")
        print("="*60)

        handler = ctx.tools["set_milestone"]["handler"]

        # First milestone
        result1_json = handler({
            "user_id": uid,
            "name": "goal_weight",
            "metric": "weight_kg",
            "threshold": 75.0,
            "deadline": "2026-12-31",
        })
        result1 = json.loads(result1_json)
        assert "error" not in result1, f"First set_milestone failed: {result1}"
        assert result1["name"] == "goal_weight"
        assert result1["metric"] == "weight_kg"
        assert result1["threshold"] == 75.0
        milestone_id_1 = result1["id"]

        # Second milestone with same name
        result2_json = handler({
            "user_id": uid,
            "name": "goal_weight",
            "metric": "weight_kg",
            "threshold": 74.0,
            "deadline": "2026-12-15",
        })
        result2 = json.loads(result2_json)
        assert "error" not in result2, f"Second set_milestone failed: {result2}"
        milestone_id_2 = result2["id"]

        # Same row should exist
        assert milestone_id_1 == milestone_id_2, f"Milestone ID changed on update"

        # Verify only one row
        count = conn.execute(
            "SELECT COUNT(*) c FROM milestones WHERE user_id=? AND name=?",
            (uid, "goal_weight"),
        ).fetchone()["c"]
        assert count == 1, f"Expected 1 milestone row, got {count}"

        # Verify update happened
        updated = conn.execute(
            "SELECT threshold FROM milestones WHERE id=?",
            (milestone_id_1,),
        ).fetchone()
        assert updated["threshold"] == 74.0, f"Milestone not updated"

        print(f"OK: Milestone UPSERT works. ID={milestone_id_1}, threshold updated to 74.0")

        print("\n" + "="*60)
        print("TEST 8: set_milestone with garbage metric")
        print("="*60)

        handler = ctx.tools["set_milestone"]["handler"]

        result_json = handler({
            "user_id": uid,
            "name": "bad_metric",
            "metric": "foobar_metric",
            "threshold": 100.0,
        })
        result = json.loads(result_json)

        assert "error" in result, f"Expected error for invalid metric, got {result}"
        assert "foobar_metric" in result["error"], f"Error message should mention metric name"

        # Verify no row was written
        count = conn.execute(
            "SELECT COUNT(*) c FROM milestones WHERE user_id=? AND name=?",
            (uid, "bad_metric"),
        ).fetchone()["c"]
        assert count == 0, f"Expected 0 milestone rows for bad metric, got {count}"

        print(f"OK: Invalid metric rejected with error JSON, no row written")
        print(f"Error: {result['error']}")

        print("\n" + "="*60)
        print("TEST 9: get_status_bar runs after register_user without raising")
        print("="*60)

        # Use the registered user (uid from earlier in the script)
        handler = ctx.tools["get_status_bar"]["handler"]

        try:
            result_json = handler({"user_id": uid})
            result = json.loads(result_json)
            assert "status_bar" in result, f"Missing status_bar in result: {result}"
            print(f"OK: get_status_bar ran without raising")
            print(f"Status bar:\n{result['status_bar']}")
        except Exception as e:
            raise AssertionError(f"get_status_bar raised after user exists: {e}")

        print("\n" + "="*60)
        print("TEST 10: _caller_telegram_id() returns None outside Hermes")
        print("="*60)

        caller_id = _caller_telegram_id()
        assert caller_id is None, f"Expected None (gateway not importable), got {caller_id}"
        print(f"OK: _caller_telegram_id() correctly returns None when gateway module not available")

        print("\n" + "="*60)
        print("TEST 11: admin_cmd with NO identity (fails closed)")
        print("="*60)

        handler = ctx.tools["admin_cmd"]["handler"]

        # Even if params supplies a telegram_user_id, it should be IGNORED
        # because we're outside Hermes (no caller_id from ContextVar)
        result_json = handler({"command": "status", "telegram_user_id": 999})
        result = json.loads(result_json)
        assert "error" in result and "identity" in result["error"].lower(), \
            f"Expected error about identity, got {result}"

        print(f"OK: Command rejected when no caller identity (params value ignored)")
        print(f"Error: {result['error']}")

        print("\n" + "="*60)
        print("TEST 11b: admin_cmd with NO identity refuses every subcommand, including mode")
        print("="*60)

        for _cmd in ["mode", "mode user", "mode admin", "status", "guards", "targets", "alerts"]:
            _r = json.loads(handler({"command": _cmd}))
            assert "error" in _r and "identity" in _r["error"].lower(), \
                f"'{_cmd}' should be refused with no caller id, got {_r}"
        print(f"OK: every subcommand refused with no caller identity")

        print("\n" + "="*60)
        print("TEST 12: admin_cmd refuses when admin list is empty")
        print("="*60)

        import yaml
        # Monkeypatch _caller_telegram_id to simulate being inside Hermes
        import plugin.tools as tools_module
        original_caller = tools_module._caller_telegram_id
        _test_caller_id = "12345"
        def mock_caller():
            return _test_caller_id

        tools_module._caller_telegram_id = mock_caller
        # Also update the global in this scope for consistency
        globals()['_caller_telegram_id'] = mock_caller

        import re

        # Пустой список админов проверяем на СВОЕЙ копии конфига, а не на живом
        # файле: в развёрнутой системе туда вписан настоящий id, и тест, читающий
        # рабочую конфигурацию, ломается от факта развёртывания.
        _empty_cfg = Path(tmp) / "config_empty_admin.yaml"
        _src = Path(config.CONFIG_PATH).read_text(encoding="utf-8")
        _empty_cfg.write_text(
            re.sub(r"telegram_admin_ids:.*", "telegram_admin_ids: []", _src),
            encoding="utf-8")
        _saved_cfg_path = config.CONFIG_PATH
        config.CONFIG_PATH = _empty_cfg
        config.load.cache_clear()
        admin_list = (yaml.safe_load(_empty_cfg.read_text(encoding="utf-8")) or {}) \
            .get("admin", {}).get("telegram_admin_ids", [])
        assert admin_list == [], f"Expected empty admin list, got {admin_list}"

        result_json = handler({"command": "status"})
        result = json.loads(result_json)
        assert "error" in result and "Not authorized" in result["error"], \
            f"Expected authorization error with empty admin list, got {result}"

        print(f"OK: Command rejected when admin list is empty")
        print(f"Error: {result['error']}")

        # Restore
        tools_module._caller_telegram_id = original_caller
        globals()['_caller_telegram_id'] = original_caller

        print("\n" + "="*60)
        print("TEST 13: admin_cmd with non-admin caller (identity present but not authorized)")
        print("="*60)

        # Monkeypatch again with different id
        _non_admin_id = "54321"
        def mock_non_admin():
            return _non_admin_id
        tools_module._caller_telegram_id = mock_non_admin
        globals()['_caller_telegram_id'] = mock_non_admin

        # At this point, admin list is still empty from the config
        result_json = handler({"command": "status"})
        result = json.loads(result_json)
        assert "error" in result and "Not authorized" in result["error"], \
            f"Expected authorization error, got {result}"

        print(f"OK: Non-admin caller rejected")
        print(f"Error: {result['error']}")

        # Restore
        tools_module._caller_telegram_id = original_caller
        globals()['_caller_telegram_id'] = original_caller

        print("\n" + "="*60)
        print("TEST 14: _get_user_id falls back to params when no caller id")
        print("="*60)

        result_user_id = _get_user_id({"user_id": 42}, conn)
        assert result_user_id == 42, f"Expected fallback to params user_id=42, got {result_user_id}"
        print(f"OK: _get_user_id falls back to params.user_id when no caller identity")

        print("\n" + "="*60)
        print("TEST 15: _get_user_id resolves from caller_id (monkeypatch success)")
        print("="*60)

        # Monkeypatch _caller_telegram_id to return a known id
        _monkeypatch_id = "12345"
        def mock_monkeypatch():
            return _monkeypatch_id
        tools_module._caller_telegram_id = mock_monkeypatch
        globals()['_caller_telegram_id'] = mock_monkeypatch

        # Seed a matching user
        conn.execute(
            "INSERT INTO users(id, telegram_user_id, height_cm, birth_date, sex, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (999, _monkeypatch_id, 180, "1990-01-01", "m", _now_iso()),
        )
        conn.commit()

        # Call _get_user_id with conn and a different user_id in params
        # It should resolve to 999 (from monkeypatched caller), NOT the params value
        result_user_id = _get_user_id({"user_id": 555}, conn)
        assert result_user_id == 999, \
            f"Expected resolved id=999 from caller, not params value 555, got {result_user_id}"
        print(f"OK: _get_user_id resolved monkeypatched caller id to user_id=999")

        # Clean up the test user
        conn.execute("DELETE FROM users WHERE id=?", (999,))
        conn.commit()

        print("\n" + "="*60)
        print("TEST 16: _get_user_id with unknown caller_id → error (fail closed)")
        print("="*60)

        # Monkeypatch to an id that doesn't exist in database
        _unknown_id = "99999"
        def mock_unknown():
            return _unknown_id
        tools_module._caller_telegram_id = mock_unknown
        globals()['_caller_telegram_id'] = mock_unknown

        try:
            _get_user_id({"user_id": 555}, conn)
            raise AssertionError("Expected ValueError for unknown caller_id, but no error was raised")
        except ValueError as e:
            assert "not found" in str(e).lower(), \
                f"Expected error mentioning 'not found', got: {e}"
            print(f"OK: _get_user_id raises error for unknown caller_id")
            print(f"Error: {e}")

        # Restore
        tools_module._caller_telegram_id = original_caller
        globals()['_caller_telegram_id'] = original_caller

        print("\n" + "="*60)
        print("TEST 17: admin_cmd mode switch + unknown subcommand")
        print("="*60)

        # Самотест НИКОГДА не пишет в живой config.yaml: раньше здесь стоял
        # yaml.dump поверх настоящего файла — он уничтожал все комментарии и
        # оставлял админа 999 в рабочей конфигурации навсегда.
        # Работаем на временной копии и возвращаем путь обратно.
        import re
        import shutil
        import yaml
        _real_cfg = config.CONFIG_PATH
        _tmp_cfg = Path(tmp) / "config.yaml"
        shutil.copy2(_real_cfg, _tmp_cfg)
        _txt = _tmp_cfg.read_text(encoding="utf-8")
        _txt_modified = _txt.replace("telegram_admin_ids: []",
                                     "telegram_admin_ids: [999]")
        _tmp_cfg.write_text(_txt_modified, encoding="utf-8")
        config.CONFIG_PATH = _tmp_cfg
        config.load.cache_clear()
        # Дальнейшие TEST 18/19 работают с этой копией, не с живым файлом.
        cfg_path = _tmp_cfg
        cfg = yaml.safe_load(_tmp_cfg.read_text(encoding="utf-8")) or {}
        _comments_before = sum(1 for l in _tmp_cfg.read_text(encoding="utf-8").splitlines()
                               if l.strip().startswith("#"))

        # Monkeypatch to admin id for testing
        def mock_admin():
            return "999"
        tools_module._caller_telegram_id = mock_admin
        globals()['_caller_telegram_id'] = mock_admin

        print("\n" + "="*60)
        print("TEST 17b: mode defaults to user, even for an allowlisted caller, until set")
        print("="*60)

        result_mode0 = json.loads(handler({"command": "mode"}))
        assert result_mode0.get("text") == "Режим: user", f"Default mode must be user, got {result_mode0}"
        print(f"OK: default mode for caller 999 (no row yet) = user")

        print("\n" + "="*60)
        print("TEST 17c: status refused while in user mode, even for an allowlisted caller")
        print("="*60)

        result_blocked = json.loads(handler({"command": "status"}))
        assert "error" in result_blocked, f"status must be refused in user mode, got {result_blocked}"
        print(f"OK: refused — {result_blocked['error']}")

        print("\n" + "="*60)
        print("TEST 17d: mode admin refused for a caller not in the allowlist")
        print("="*60)

        def mock_non_admin_mode():
            return "111222"
        tools_module._caller_telegram_id = mock_non_admin_mode
        globals()['_caller_telegram_id'] = mock_non_admin_mode

        result_mode_admin_denied = json.loads(handler({"command": "mode admin"}))
        assert "error" in result_mode_admin_denied, \
            f"mode admin must be refused for non-admin caller, got {result_mode_admin_denied}"
        print(f"OK: refused — {result_mode_admin_denied['error']}")

        print("\n" + "="*60)
        print("TEST 17e: mode user is accepted without admin rights")
        print("="*60)

        result_mode_user_ok = json.loads(handler({"command": "mode user"}))
        assert result_mode_user_ok.get("text") == "Режим: user", \
            f"mode user must succeed for anyone, got {result_mode_user_ok}"
        print(f"OK: mode user accepted for a non-allowlisted caller")

        # Back to the allowlisted caller for the rest of the admin flow.
        tools_module._caller_telegram_id = mock_admin
        globals()['_caller_telegram_id'] = mock_admin

        print("\n" + "="*60)
        print("TEST 17f: mode admin accepted for an allowlisted caller, persists, reads back")
        print("="*60)

        result_mode_admin_ok = json.loads(handler({"command": "mode admin"}))
        assert result_mode_admin_ok.get("text") == "Режим: admin", \
            f"mode admin should succeed for allowlisted caller, got {result_mode_admin_ok}"
        result_mode_read = json.loads(handler({"command": "mode"}))
        assert result_mode_read.get("text") == "Режим: admin", f"mode should read back admin, got {result_mode_read}"
        print(f"OK: mode admin set and persisted for caller 999")

        print("\n" + "="*60)
        print("TEST 17g: unknown subcommand (now in admin mode)")
        print("="*60)

        result_json = handler({"command": "foobar_cmd"})
        result = json.loads(result_json)
        assert "error" in result and "Unknown command" in result["error"], \
            f"Expected unknown command error, got {result}"
        assert "foobar_cmd" in result["error"], f"Error should mention the bad command"
        assert "Valid:" in result["error"], f"Error should list valid commands"

        print(f"OK: Unknown subcommand rejected with command list")
        print(f"Error: {result['error']}")

        print("\n" + "="*60)
        print("TEST 18: admin_cmd status")
        print("="*60)

        result_json = handler({"command": "status"})
        result = json.loads(result_json)
        assert "text" in result and "Users:" in result["text"], \
            f"Expected status text with user count, got {result}"

        print(f"OK: Status command succeeded")
        print(f"Output:\n{result['text']}")

        print("\n" + "="*60)
        print("TEST 19: admin_cmd set on unknown key")
        print("="*60)

        result_json = handler({"command": "set foobar_key 123"})
        result = json.loads(result_json)
        assert "error" in result and "not found" in result["error"], \
            f"Expected key not found error, got {result}"

        # Verify config.yaml is unchanged
        with open(cfg_path, encoding='utf-8') as f:
            cfg_after = yaml.safe_load(f) or {}
        assert cfg_after == cfg, "config.yaml should not be modified"

        print(f"OK: Unknown key rejected, config unchanged")
        print(f"Error: {result['error']}")

        print("\n" + "="*60)
        print("TEST 20: admin_cmd set on known key")
        print("="*60)

        # Test setting a known key
        result_json = handler({"command": "set keep_copies 99"})
        result = json.loads(result_json)
        assert "text" in result, f"Expected success, got {result}"

        # Verify backup was created
        bak_path = cfg_path.with_suffix('.bak')
        assert bak_path.exists(), "Backup (.bak) should be created"

        # Verify config.yaml was updated
        with open(cfg_path, encoding='utf-8') as f:
            cfg_updated = yaml.safe_load(f) or {}
        assert cfg_updated.get("backup", {}).get("keep_copies") == 99, \
            f"keep_copies should be updated to 99, got {cfg_updated.get('backup', {}).get('keep_copies')}"

        _comments_after = sum(1 for l in cfg_path.read_text(encoding="utf-8").splitlines()
                              if l.strip().startswith("#"))
        assert _comments_after == _comments_before, (
            f"set уничтожил комментарии: было {_comments_before}, стало {_comments_after}. "
            f"Правка обязана быть построчной, а не через yaml.dump.")

        # Verify it still parses as YAML
        with open(cfg_path, encoding='utf-8') as f:
            yaml.safe_load(f)  # Will raise if invalid

        print(f"OK: Known key updated, backup created, YAML valid")
        print(f"Output: {result['text']}")

        # Restore config for subsequent tests
        shutil.move(bak_path, cfg_path)

        # Restore caller. Оба места: tools_module — атрибут отдельного экземпляра
        # модуля (self-import), globals() — та же функция, которую реально вызывают
        # зарегистрированные обработчики этого процесса. Забыть второе — следующий
        # тест без явного user_id тихо резолвится через caller_id="999" (admin из
        # TEST 17f), а не через params, как ожидают TEST 21/22 ниже.
        tools_module._caller_telegram_id = original_caller
        globals()['_caller_telegram_id'] = original_caller

        print("\n" + "="*60)
        print("TEST 21: get_day_summary format=bar / dashboard / default")
        print("="*60)

        today_ds = _today_iso()
        conn.execute(
            "INSERT INTO food_log(user_id, eaten_at, meal_slot) VALUES (?, ?, 'breakfast')",
            (uid, f"{today_ds} 08:00:00"),
        )
        flid_ds = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        conn.execute(
            "INSERT INTO food_items(food_log_id, name, grams, kcal, protein_g, fat_g, carbs_g, plate_category) "
            "VALUES (?, 'завтрак', 300, 500, 30, 15, 60, 'белки')",
            (flid_ds,),
        )
        conn.commit()

        handler_ds = ctx.tools["get_day_summary"]["handler"]

        # format="bar" — исходный многополевой payload, форма не должна была измениться.
        result_bar_ds = json.loads(handler_ds({"user_id": uid, "date": today_ds, "format": "bar"}))
        assert "error" not in result_bar_ds, f"format=bar raised: {result_bar_ds}"
        assert set(result_bar_ds.keys()) == {
            "date", "kcal_eaten", "protein_g", "fat_g", "carb_g", "water_ml",
            "target", "remaining_kcal", "plate_balance",
        }, f"format=bar payload shape changed: {list(result_bar_ds.keys())}"
        assert result_bar_ds["kcal_eaten"] == 500, result_bar_ds

        # format="dashboard" (умолчание) — ровно один ключ "day_summary".
        result_dash_ds = json.loads(handler_ds({"user_id": uid, "date": today_ds}))
        assert list(result_dash_ds) == ["day_summary"], \
            f"day_summary dashboard должен отдавать ровно один ключ, получено {list(result_dash_ds)}"
        day_dash = result_dash_ds["day_summary"]
        assert "```" not in day_dash, f"day_summary dashboard must be plain markdown, not fenced: {day_dash!r}"
        assert "500" in day_dash, f"day_summary dashboard missing content: {day_dash!r}"
        assert "🔥" in day_dash and "**Калории**" in day_dash, \
            f"day_summary dashboard missing markdown nutrient line: {day_dash!r}"

        result_explicit_ds = json.loads(handler_ds({"user_id": uid, "date": today_ds, "format": "dashboard"}))
        assert result_explicit_ds["day_summary"] == day_dash, (
            "умолчание должно совпадать с format='dashboard'\n---default---\n"
            f"{day_dash}\n---explicit---\n{result_explicit_ds['day_summary']}"
        )

        print("OK: format=bar payload unchanged, dashboard is exactly one key, default matches dashboard")
        print(f"Day dashboard:\n{day_dash}")

        print("\n" + "="*60)
        print("TEST 22: get_day_summary dashboard on a user with nothing logged")
        print("="*60)

        result_empty_ds = json.loads(handler_ds({"user_id": empty_uid, "date": today_ds}))
        assert "error" not in result_empty_ds, f"empty-user day_summary raised: {result_empty_ds}"
        empty_day_dash = result_empty_ds["day_summary"]
        assert "%" not in empty_day_dash, f"no target set — must not fabricate a percentage: {empty_day_dash!r}"
        assert "0/0" not in empty_day_dash, f"must not fabricate a zero target: {empty_day_dash!r}"

        print("OK: day_summary dashboard renders for a user with nothing logged, raises nothing")
        print(f"Dashboard:\n{empty_day_dash}")

        print("\n" + "="*60)
        print("TEST 22b: get_day_summary format=journal — markdown, no fence")
        print("="*60)

        result_journal_ds = json.loads(handler_ds({"user_id": uid, "date": today_ds, "format": "journal"}))
        assert "error" not in result_journal_ds, f"format=journal raised: {result_journal_ds}"
        journal = result_journal_ds["day_summary"]
        assert "```" not in journal, f"journal must be plain markdown, not fenced: {journal!r}"
        assert "Журнал питания" in journal, f"journal missing header: {journal!r}"
        assert "ИТОГО ЗА СУТКИ" in journal, f"journal missing totals line: {journal!r}"
        assert "•" in journal, f"journal missing item bullets: {journal!r}"
        # Приёмы должны быть названы, а не «Приём»: meal_slot в схеме v4 нет, ярлык
        # подставляет _meal_labels_for_day — без него журнал безликий.
        assert "Приём" not in journal, f"journal left an unnamed meal: {journal!r}"
        assert "Завтрак" in journal, f"journal missing named meal slots: {journal!r}"

        # Ярлык идёт из сохранённого meal_slot, а не из часа или калорий: слот
        # ставит log_food по прямому слову человека, иначе snack.
        # Перекусы нумеруются, чтобы два «Перекуса» за день различались.
        _day = [{"meal_slot": sl, "eaten_at": f"2026-08-21 {t}:00", "kcal": k}
                for sl, t, k in [("breakfast", "09:50", 445), ("snack", "10:30", 173),
                                 ("lunch", "14:56", 357), ("snack", "17:28", 145),
                                 ("dinner", "21:20", 157)]]
        _labels = [l for _, l in _meal_labels_for_day(_day)]
        assert _labels == ["Завтрак", "Перекус 1", "Обед", "Перекус 2", "Ужин"], \
            f"meal labels must come from meal_slot: {_labels}"
        # Строка старше schema v5 (слота нет) не должна ронять рендер.
        assert _meal_labels_for_day([{"meal_slot": None, "eaten_at": "", "kcal": 0}]) == [("🍽", "Приём")], \
            "legacy row without meal_slot must fall back to «Приём»"

        # Номер приёма — единственное, чем адресуется удаление. Без него
        # ошибочную запись назвать нечем, и delete бесполезен.
        assert re.search(r"`#\d+`", journal), f"journal must show meal id for delete: {journal!r}"

        print("OK: format=journal is markdown, no fence, has header/totals/item bullets/named meals")
        print(f"Journal:\n{journal}")

        print("\n" + "="*60)
        print("TEST 22c: log_food action=delete — убрать ошибочно записанное")
        print("="*60)
        _h_food = ctx.tools["log_food"]["handler"]

        # Повод для этого теста: бот записал продукт, который человек только
        # обсуждал, и удалить его было нечем — удаления не существовало вовсе.
        _add = json.loads(_h_food({
            "user_id": uid, "meal_slot": "snack", "eaten_at": f"{today_ds} 16:45",
            "items": [{"name": "Бородинский хлеб", "grams": 90, "kcal": 220,
                       "protein_g": 7, "fat_g": 1, "carbs_g": 42},
                      {"name": "Творог", "grams": 100, "kcal": 79,
                       "protein_g": 16.7, "fat_g": 1, "carbs_g": 3.3}]}))
        assert "error" not in _add, _add
        _jr = json.loads(handler_ds({"user_id": uid, "date": today_ds, "format": "journal"}))["day_summary"]
        _mid = int(re.findall(r"`#(\d+)`", _jr)[-1])

        # Точечное удаление по имени: остальное в приёме не трогается.
        _del = json.loads(_h_food({"user_id": uid, "action": "delete",
                                   "food_log_id": _mid, "names": ["бородинс"]}))
        assert "error" not in _del, _del
        assert [d["name"] for d in _del["deleted"]] == ["Бородинский хлеб"], _del
        assert _del["meal_removed"] is False, "приём с оставшимся продуктом удалять нельзя"
        _jr2 = json.loads(handler_ds({"user_id": uid, "date": today_ds, "format": "journal"}))["day_summary"]
        assert "Бородинский хлеб" not in _jr2 and "Творог" in _jr2, _jr2
        print(f"OK: точечное удаление убрало только названное, приём #{_mid} жив")

        # Удаление приёма целиком: пустой food_log не должен остаться —
        # он даёт «0 ккал» в журнале при живой строке.
        _del2 = json.loads(_h_food({"user_id": uid, "action": "delete", "food_log_id": _mid}))
        assert _del2["meal_removed"] is True, _del2
        _left = conn.execute("SELECT COUNT(*) n FROM food_log WHERE id=?", (_mid,)).fetchone()["n"]
        assert _left == 0, "пустой приём обязан удаляться вместе с последней позицией"
        print("OK: удаление приёма целиком не оставляет пустой строки food_log")

        # Чужой приём по номеру не удаляется: food_log_id приходит из текста
        # модели, и опечатка в цифре не должна стирать данные другого человека.
        conn.execute(
            "INSERT INTO users(telegram_user_id, height_cm, birth_date, sex, created_at) "
            "VALUES (990099, 180, '1990-01-01', 'm', ?)", (_now_iso(),))
        conn.commit()
        _uid2 = conn.execute("SELECT id FROM users WHERE telegram_user_id=990099").fetchone()["id"]
        _other = json.loads(_h_food({
            "user_id": _uid2, "meal_slot": "snack", "eaten_at": f"{today_ds} 12:00",
            "items": [{"name": "Яблоко", "grams": 150, "kcal": 78,
                       "protein_g": 0.4, "fat_g": 0.3, "carbs_g": 20.7}]}))
        assert "error" not in _other, _other
        _oid = conn.execute("SELECT id FROM food_log WHERE user_id=? ORDER BY id DESC LIMIT 1",
                            (_uid2,)).fetchone()["id"]
        _cross = json.loads(_h_food({"user_id": uid, "action": "delete", "food_log_id": _oid}))
        assert "error" in _cross, f"чужой приём удалился: {_cross}"
        assert conn.execute("SELECT COUNT(*) n FROM food_log WHERE id=?",
                            (_oid,)).fetchone()["n"] == 1, "чужая запись пострадала"
        print("OK: приём другого пользователя по номеру не удаляется")

        # Несуществующий номер и отсутствие номера — отказ, а не тихое ничего.
        assert "error" in json.loads(_h_food({"user_id": uid, "action": "delete", "food_log_id": 999999}))
        assert "error" in json.loads(_h_food({"user_id": uid, "action": "delete"}))
        print("OK: несуществующий и пропущенный food_log_id отклоняются")

        print("\n" + "="*60)
        print("TEST 23: log_med route validation (Gap 1)")
        print("="*60)

        handler_med = ctx.tools["log_med"]["handler"]
        med_before = conn.execute("SELECT COUNT(*) c FROM med_log").fetchone()["c"]

        r_missing_route = json.loads(handler_med({"drug": "Тирзепатид", "dose": "10 мг", "user_id": uid}))
        assert "error" in r_missing_route, f"log_med без route должен вернуть error, получили {r_missing_route}"
        med_after_missing = conn.execute("SELECT COUNT(*) c FROM med_log").fetchone()["c"]
        assert med_after_missing == med_before, "log_med без route не должен писать строку в med_log"

        r_bad_route = json.loads(handler_med(
            {"drug": "Тирзепатид", "dose": "10 мг", "route": "intravenous", "user_id": uid}
        ))
        assert "error" in r_bad_route, f"log_med с неизвестным route должен вернуть error, получили {r_bad_route}"
        med_after_bad = conn.execute("SELECT COUNT(*) c FROM med_log").fetchone()["c"]
        assert med_after_bad == med_before, "log_med с неизвестным route не должен писать строку в med_log"

        r_ok_route = json.loads(handler_med(
            {"drug": "Андрокомплекс", "dose": "1 капс", "route": "oral", "user_id": uid}
        ))
        assert "error" not in r_ok_route and r_ok_route.get("route") == "oral", \
            f"валидный log_med с route='oral' сломан: {r_ok_route}"
        med_after_ok = conn.execute("SELECT COUNT(*) c FROM med_log").fetchone()["c"]
        assert med_after_ok == med_before + 1, "валидный log_med должен записать ровно одну новую строку"

        print("OK: log_med отклоняет отсутствующий и неизвестный route без записи строки, валидный route проходит")

        print("\n" + "="*60)
        print("TEST 23b: log_food meal_slot — optional, code assigns by window, enum-gated when passed (schema v18)")
        print("="*60)

        handler_food = ctx.tools["log_food"]["handler"]
        _food_item = {"name": "Овсянка", "kcal": 300, "protein_g": 10, "fat_g": 8, "carbs_g": 45}
        food_before = conn.execute("SELECT COUNT(*) c FROM food_log").fetchone()["c"]

        # Без meal_slot — код определяет сам по окну (health_core.chrono.meal_slot):
        # 07:30 без прежнего завтрака в этот день -> breakfast.
        r_missing_slot = json.loads(handler_food(
            {"items": [_food_item], "eaten_at": "2026-09-10 07:30:00", "user_id": uid}
        ))
        assert "error" not in r_missing_slot, f"log_food без meal_slot не должен быть ошибкой: {r_missing_slot}"
        assert r_missing_slot.get("meal_slot") == "breakfast", \
            f"log_food без meal_slot в 07:30 должен назначить breakfast, получили {r_missing_slot}"
        assert conn.execute("SELECT COUNT(*) c FROM food_log").fetchone()["c"] == food_before + 1, \
            "log_food без meal_slot обязан писать строку с назначенным приёмом"

        food_after_missing = conn.execute("SELECT COUNT(*) c FROM food_log").fetchone()["c"]

        r_bad_slot = json.loads(handler_food(
            {"items": [_food_item], "meal_slot": "brunch", "user_id": uid}
        ))
        assert "error" in r_bad_slot, f"log_food с неизвестным meal_slot должен вернуть error, получили {r_bad_slot}"
        assert conn.execute("SELECT COUNT(*) c FROM food_log").fetchone()["c"] == food_after_missing, \
            "log_food с неизвестным meal_slot не должен писать строку"

        r_ok_slot = json.loads(handler_food(
            {"items": [_food_item], "meal_slot": "breakfast", "user_id": uid}
        ))
        assert "error" not in r_ok_slot, f"валидный log_food с meal_slot='breakfast' сломан: {r_ok_slot}"
        assert r_ok_slot.get("meal_slot") == "breakfast", \
            f"переданный meal_slot должен вернуться как есть, получили {r_ok_slot}"
        stored_slot = conn.execute(
            "SELECT meal_slot FROM food_log WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)
        ).fetchone()["meal_slot"]
        assert stored_slot == "breakfast", \
            f"meal_slot должен читаться обратно из food_log как 'breakfast', получили {stored_slot!r}"

        print("OK: log_food без meal_slot назначает приём сам и пишет строку, "
              "неизвестный meal_slot отклоняется без записи, переданный валидный сохраняется как есть")

        print("\n" + "="*60)
        print("TEST 24: import_scale_export magic-byte dispatch (Gap 2)")
        print("="*60)

        import csv as _csv_mod
        import io as _io
        import openpyxl as _openpyxl
        from health_core.ingest.scale import COLUMNS as _SCALE_COLUMNS

        handler_import = ctx.tools["import_scale_export"]["handler"]

        def _scale_row(measured_at: str, weight: str):
            out = {}
            for col, field in _SCALE_COLUMNS.items():
                if field == "measured_at":
                    out[col] = measured_at
                elif field == "weight_kg":
                    out[col] = weight
                elif field == "device_mac":
                    out[col] = "AA:BB:CC"
                else:
                    out[col] = "10.0"
            return out

        # 24a: genuine OOXML zip saved with the production's misleading double
        # extension (*.xlsx.xls) — sniff must go by content (PK header), not by
        # the ".xls" suffix that openpyxl would otherwise refuse.
        wb = _openpyxl.Workbook()
        ws = wb.active
        header = list(_SCALE_COLUMNS.keys())
        ws.append(header)
        row = _scale_row("21/08/2026 08:00:00", "79.5")
        ws.append([row[c] for c in header])
        xlsx_buf = _io.BytesIO()
        wb.save(xlsx_buf)
        xlsx_path = Path(tmp) / "scale_export.xlsx.xls"
        xlsx_path.write_bytes(xlsx_buf.getvalue())

        r_xlsx = json.loads(handler_import({"file_path": str(xlsx_path), "user_id": uid}))
        assert "error" not in r_xlsx, f"xlsx-как-.xls импорт упал: {r_xlsx}"
        assert r_xlsx["added"] == 1 and r_xlsx["bursts"] == 1, f"ожидали added=1 bursts=1, получили {r_xlsx}"

        # 24b: same file again — file_hash already in import_log, so scale.py's
        # own dedup short-circuits the whole file (added=0 skipped=0 bursts=0,
        # per its own self-check). The point here is the response must carry
        # those explicit numbers, not a bare "готово" that hides a zero-row import.
        r_xlsx_dup = json.loads(handler_import({"file_path": str(xlsx_path), "user_id": uid}))
        assert "error" not in r_xlsx_dup, f"повторный импорт упал: {r_xlsx_dup}"
        assert r_xlsx_dup["added"] == 0 and r_xlsx_dup["skipped"] == 0 and r_xlsx_dup["bursts"] == 0, \
            f"повторный импорт (уже в import_log) должен явно отдать added=0 skipped=0 bursts=0, получили {r_xlsx_dup}"
        assert set(r_xlsx_dup.keys()) >= {"added", "skipped", "bursts"}, \
            f"ответ обязан нести явные числа added/skipped/bursts, а не голое 'готово': {r_xlsx_dup}"

        # 24c: plain CSV content with NO recognizable extension — sniff must
        # detect it as text/csv (not zip, not xml) and route it correctly even
        # though scale.py's own branch logic keys off a literal ".csv" suffix.
        csv_buf = _io.StringIO()
        writer = _csv_mod.DictWriter(csv_buf, fieldnames=header)
        writer.writeheader()
        writer.writerow(_scale_row("21/08/2026 09:00:00", "80.1"))
        csv_path = Path(tmp) / "scale_export.dat"
        csv_path.write_text(csv_buf.getvalue(), encoding="utf-8-sig")

        r_csv = json.loads(handler_import({"file_path": str(csv_path), "user_id": uid}))
        assert "error" not in r_csv, f"csv-без-расширения импорт упал: {r_csv}"
        assert r_csv["added"] == 1, f"ожидали added=1 для csv-без-расширения, получили {r_csv}"

        # 24d: file over the 20 MB cap is rejected with an explicit error and
        # never reaches the parser — verified by temporarily lowering the cap
        # instead of writing an actual 20 MB fixture.
        original_cap = tools_module._MAX_IMPORT_BYTES
        tools_module._MAX_IMPORT_BYTES = 10
        globals()['_MAX_IMPORT_BYTES'] = 10
        try:
            big_path = Path(tmp) / "too_big.csv"
            big_path.write_text("x" * 100, encoding="utf-8")
            r_big = json.loads(handler_import({"file_path": str(big_path), "user_id": uid}))
            assert "error" in r_big, f"файл больше лимита должен вернуть error, получили {r_big}"
        finally:
            tools_module._MAX_IMPORT_BYTES = original_cap
            globals()['_MAX_IMPORT_BYTES'] = original_cap

        # 24e: missing file path is a clear error, not a crash or a silent no-op.
        r_missing_file = json.loads(handler_import({"file_path": str(Path(tmp) / "nope.xlsx"), "user_id": uid}))
        assert "error" in r_missing_file, f"несуществующий файл должен вернуть error, получили {r_missing_file}"

        print("OK: import_scale_export sniff'ает xlsx-как-.xls и csv-без-расширения по магическим байтам, "
              "явно отдаёт added=0 на повторном импорте, отклоняет файл >20МБ и отсутствующий путь")

        print("\n" + "="*60)
        print("TEST 25: generic contract check — every tool's own schema-required payload must be accepted")
        print("="*60)
        # required в JSON-схеме — подсказка модели, не гарантия (на проде Gemini
        # прислал log_food без grams, хотя grams был в required). Единственный
        # настоящий гейт — обработчик. Этот тест не проверяет, что required
        # ПОЛНЫЙ (для этого нужен бы был оракул независимый от схемы) — он
        # проверяет, что то, что required УЖЕ обещает, обработчик реально
        # принимает. Расхождение в любую сторону — контрактный баг по определению.

        def _dummy_for(prop: dict):
            """Значение, годное под тип/enum одного свойства схемы. Для enum —
            первое разрешённое значение: контракт обещает, что оно всегда валидно."""
            if "enum" in prop:
                return prop["enum"][0]
            t = prop.get("type")
            if t == "integer":
                return 1
            if t == "number":
                return 1.0
            if t == "boolean":
                return True
            if t == "array":
                item_schema = prop.get("items", {})
                if item_schema.get("type") == "object":
                    iprops = item_schema.get("properties", {})
                    ireq = item_schema.get("required", [])
                    return [{k: _dummy_for(iprops[k]) for k in ireq if k in iprops}]
                return []
            return "x"

        def _payload_from_schema(schema: dict) -> dict:
            props = schema.get("properties", {})
            required = schema.get("required", [])
            return {k: _dummy_for(props[k]) for k in required if k in props}

        # Плюс-данные, которые схема сама не может выразить (существующая запись,
        # реальный файл на диске) — не бизнес-логика конкретного инструмента,
        # а то, чего строгий дамми в принципе не может syntheзировать.
        _GEN_EXTRAS = {
            "import_scale_export": {"file_path": str(csv_path)},
            # kcal/protein_g/fat_g/carbs_g обязательны только если НЕТ per_100g —
            # это ветвление кода, которое дамми по одному "required": ["name"] не
            # выразит (та же причина, что у log_water ниже: ml не required схемой,
            # но нужен для реального add).
            "food_lookup": {"name": "Тестовый продукт для контракта"},
            "log_food": {"items": [{"name": "Тест", "kcal": 300, "protein_g": 10,
                                     "fat_g": 10, "carbs_g": 30}]},
            "get_progress": {"milestone_name": "goal_weight"},
            # Прогнозу нужен средний приход за 14 дней; фикстура столько еды не
            # логирует, а отказ "мало данных" — не нарушение контракта полей.
            "forecast": {"intake_kcal": 1500},
            "log_water": {"ml": 250},
            "log_glucose": {"mmol_l": 5.5},
            "log_side_effect": {"symptom": "тошнота"},
            # days не required схемой (add() валидный без него был бы ошибкой
            # контракта в другую сторону), дата — заведомо прошлая.
            "log_watch_day": {"days": [{"date": "2020-01-01", "hr_avg": 65}]},
            "log_labs": {"markers": {"glucose": 5.0, "insulin": 10}},
            "log_sleep": {"duration_min": 480},
            "log_weight": {"weight_kg": 80.0},
            "log_anthropometry": {"site": "талия", "value_cm": 85.0},
            "log_med": {"drug": "Тест", "dose": "1", "route": "oral"},
            "log_workout": {"sport": "Бег", "duration_min": 30, "kcal": 250},
            # action=fetch бьёт в сеть (openFDA/ClinicalTrials.gov) — самотесты сеть
            # не трогают (см. TEST в card_drafts.py, где HTTP подменяется). save не
            # сетевой, им и проверяем контракт полей.
            "drug_card_draft": {"action": "save", "substance": "Тестовый Драфт-Смоук",
                                 "fields": {"status": "не зарегистрирован"}, "sources": ["https://example.com"]},
        }

        _gen_saved_caller = tools_module._caller_telegram_id
        _gen_saved_cfg_path = config.CONFIG_PATH
        _gen_failures = []

        for _name, _info in ctx.tools.items():
            _handler = _info["handler"]
            _schema = _info["schema"]
            _payload = _payload_from_schema(_schema)
            _payload.setdefault("user_id", uid)
            _payload.update(_GEN_EXTRAS.get(_name, {}))

            if _name == "register_user":
                # Identity приходит из ContextVar, не из схемы — вне Hermes нужен
                # caller_id, иначе валится на "identity", а не на контракт полей.
                def _reg_caller():
                    return "888001"
                tools_module._caller_telegram_id = _reg_caller
                globals()['_caller_telegram_id'] = _reg_caller
                try:
                    _result = json.loads(_handler(_payload))
                finally:
                    tools_module._caller_telegram_id = _gen_saved_caller
                    globals()['_caller_telegram_id'] = _gen_saved_caller
            elif _name == "admin_cmd":
                # allowlist + admin mode на песочной копии конфига — никогда на
                # живом config.yaml.
                _payload["command"] = "status"
                _gen_cfg = Path(tmp) / "config_gencheck.yaml"
                shutil.copy2(_gen_saved_cfg_path, _gen_cfg)
                _gen_cfg.write_text(
                    re.sub(r"telegram_admin_ids:.*", "telegram_admin_ids: [777777]",
                           _gen_cfg.read_text(encoding="utf-8")),
                    encoding="utf-8")
                config.CONFIG_PATH = _gen_cfg
                config.load.cache_clear()
                _set_mode("777777", "admin")

                def _admin_caller():
                    return "777777"
                tools_module._caller_telegram_id = _admin_caller
                globals()['_caller_telegram_id'] = _admin_caller
                try:
                    _result = json.loads(_handler(_payload))
                finally:
                    tools_module._caller_telegram_id = _gen_saved_caller
                    globals()['_caller_telegram_id'] = _gen_saved_caller
                    config.CONFIG_PATH = _gen_saved_cfg_path
                    config.load.cache_clear()
            else:
                _result = json.loads(_handler(_payload))

            if "error" in _result:
                _gen_failures.append(f"{_name}: payload={_payload} -> {_result['error']}")

        assert not _gen_failures, (
            "contract violations — schema's own required payload rejected by handler:\n"
            + "\n".join(_gen_failures)
        )
        print(f"OK: all {len(ctx.tools)} tools accepted their own schema-required payload")

        print("\n" + "="*60)
        print("TEST 26: pantry (Холодильник) CRUD — upsert-сумма, декремент, удаление на нуле")
        print("="*60)
        conn.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (556001, ?)", (_now_iso(),))
        conn.commit()
        p_uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=556001").fetchone()["id"]

        def _pan(**kw):
            return json.loads(handle_pantry({"user_id": p_uid, **kw}))

        assert _pan(action="list")["pantry"] == "🧊 Холодильник пуст.", "пустой холодильник"
        assert "ok" in _pan(action="add", name="Куриное филе", qty=400, unit="г", category="Белковые")
        # Пополнение того же продукта складывает количества, не плодит вторую строку.
        _pan(action="add", name="Куриное филе", qty=100, unit="г", category="Белковые")
        row = conn.execute("SELECT qty FROM pantry WHERE user_id=? AND name='Куриное филе'", (p_uid,)).fetchone()
        assert row["qty"] == 500.0, f"upsert должен сложить 400+100=500, получили {row['qty']}"
        assert conn.execute("SELECT COUNT(*) c FROM pantry WHERE user_id=?", (p_uid,)).fetchone()["c"] == 1, \
            "пополнение не должно создавать вторую строку"
        # Частичное списание уменьшает остаток.
        _pan(action="remove", name="Куриное филе", qty=200)
        row = conn.execute("SELECT qty FROM pantry WHERE user_id=? AND name='Куриное филе'", (p_uid,)).fetchone()
        assert row["qty"] == 300.0, f"после списания 200 должно остаться 300, получили {row['qty']}"
        # Списание сверх остатка убирает позицию целиком.
        _pan(action="remove", name="Куриное филе", qty=999)
        assert conn.execute("SELECT COUNT(*) c FROM pantry WHERE user_id=? AND name='Куриное филе'",
                            (p_uid,)).fetchone()["c"] == 0, "списание сверх остатка удаляет позицию"
        # Списание отсутствующего — ошибка, а не молчаливый успех.
        assert "error" in _pan(action="remove", name="Нет такого"), "remove несуществующего должен вернуть error"
        # Группировка в списке по категориям.
        _pan(action="add", name="Огурец", qty=2, unit="шт", category="Овощи/Фрукты")
        _pan(action="add", name="Творог", qty=200, unit="г", category="Молочка/Сыры")
        listing = _pan(action="list")["pantry"]
        assert "Молочка/Сыры" in listing and "Овощи/Фрукты" in listing, "список группируется по категориям"
        # Порядок: Молочка/Сыры (индекс 1) раньше Овощи/Фрукты (индекс 2) по _PANTRY_CATEGORY_ORDER.
        assert listing.index("Молочка/Сыры") < listing.index("Овощи/Фрукты"), "категории в заданном порядке"
        print("OK: pantry list/add/remove — сумма при пополнении, декремент, удаление на нуле, ошибка на отсутствующем, группировка")

        print("\n" + "="*60)
        print("TEST 27: pharma расписание+остаток и plans шаблон")
        print("="*60)
        conn.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (557001, ?)", (_now_iso(),))
        conn.commit()
        rx_uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=557001").fetchone()["id"]

        def _rx(**kw):
            return json.loads(handle_pharma({"user_id": rx_uid, **kw}))

        # Каждое действие ниже заходит под РАЗНЫМ именем одного препарата
        # (латиница из карты, бренд, МНН). Все они обязаны попасть в одну
        # строку med_schedule: раздвоение имени и было тем багом, из-за
        # которого принятая доза не двигала next_at и расписание вечно висело
        # просроченным. См. health_core/meds.py.
        assert "Расписаний нет" in _rx(action="status")["pharma"]
        assert "ok" in _rx(action="schedule", substance="Tirzepatide", dose=10, unit="mg", route="injection", by_doctor=True,
                           every_days=7, next_at="2026-08-24 22:00:00", stock_doses=4)
        st = _rx(action="status")["pharma"]
        assert "Тирзепатид" in st and "10mg" in st and "4 доз" in st, st
        assert "Tirzepatide" not in st, f"имя должно храниться каноничным: {st}"
        # Приём под торговым именем списывает дозу и двигает следующую на каденцию.
        json.loads(handle_log_med({"user_id": rx_uid, "drug": "Тирзетта", "dose": "10",
                                   "route": "injection", "unit": "mg", "at": "2026-08-24 22:05:00"}))
        rows = conn.execute("SELECT substance, stock_doses, next_at FROM med_schedule WHERE user_id=?",
                            (rx_uid,)).fetchall()
        assert len(rows) == 1, f"алиас не должен заводить вторую строку, получили {[dict(r) for r in rows]}"
        row = rows[0]
        assert row["substance"] == "Тирзепатид", row["substance"]
        assert row["stock_doses"] == 3.0, f"остаток должен списаться 4->3, получили {row['stock_doses']}"
        assert row["next_at"] == "2026-08-31 22:00:00", f"next_at должен уйти на +7 дней, получили {row['next_at']}"
        # restock пополняет; remove убирает — тоже через алиасы.
        _rx(action="restock", substance="тирзетта", add_doses=2)
        assert conn.execute("SELECT stock_doses FROM med_schedule WHERE user_id=? AND substance='Тирзепатид'",
                            (rx_uid,)).fetchone()["stock_doses"] == 5.0, "restock 3+2=5"
        assert "error" in _rx(action="restock", substance="Нет", add_doses=1), "restock без расписания — error"
        assert "ok" in _rx(action="remove", substance="Tirzepatide")
        assert conn.execute("SELECT COUNT(*) c FROM med_schedule WHERE user_id=?", (rx_uid,)).fetchone()["c"] == 0

        def _pl(**kw):
            return json.loads(handle_plans({"user_id": rx_uid, **kw}))

        assert "План пуст" in _pl(action="show")["plans"]
        assert "ok" in _pl(action="set_meal", day_of_week=0, meal_slot="breakfast", name="Овсянка", kcal=350, protein_g=20)
        assert "error" in _pl(action="set_meal", day_of_week=0, meal_slot="brunch"), "неизвестный слот — error"
        assert "ok" in _pl(action="set_workout", day_of_week=0, name="Силовая", duration_min=60)
        show = _pl(action="show")["plans"]
        assert "Овсянка" in show and "Силовая" in show and "Пн" in show, show
        vs = _pl(action="vs_actual", date="2026-08-24")["plans"]  # 24.08.2026 = понедельник
        assert "План vs факт" in vs and "Еда: план 350" in vs, vs
        print("OK: pharma schedule/restock/remove + log_med списывает и двигает дозу; plans set/show/vs_actual")

        print("\n" + "="*60)
        print("TEST 28: _goal_progress — доля пути старт→цель в обе стороны")
        print("="*60)
        # Снижение веса: старт 158.7, цель 120.0, сейчас 120.5 -> 38.2/38.7.
        assert round(_goal_progress(158.7, 120.5, 120.0), 3) == 0.987, _goal_progress(158.7, 120.5, 120.0)
        # факт/цель дало бы ровно 100% при недостигнутой цели — ради этого и helper.
        assert round(120.5 / 120.0, 2) == 1.0
        # Набор сухой массы: старт 70, цель 80, сейчас 75 -> половина пути.
        assert _goal_progress(70.0, 75.0, 80.0) == 0.5
        # Перелёт цели клампится в 1.0, откат назад — в 0.0.
        assert _goal_progress(158.7, 118.0, 120.0) == 1.0
        assert _goal_progress(158.7, 160.0, 120.0) == 0.0
        # Нет старта / цель равна старту -> прогресса нет, а не ноль.
        assert _goal_progress(None, 120.5, 120.0) is None
        assert _goal_progress(120.0, 120.5, 120.0) is None
        assert _dash_bar_frac(0.987) == "█████ 99%", _dash_bar_frac(0.987)
        assert _dash_bar_frac(0.0) == "░░░░░ 0%"
        print("OK: прогресс считается от пути старт→цель, клампится, молчит без точки отсчёта")

        print("\n" + "="*60)
        print("TEST 28b: _sparkline/_stride_sample — 0/1/2 точки не роняют рендер, форма верна")
        print("="*60)

        assert _sparkline([]) == "", "пустой ряд — пустая строка, не исключение"
        assert _sparkline([70.0]) == "█", "одна точка — верхний блок, сравнивать не с чем"
        two = _sparkline([80.0, 90.0])
        assert len(two) == 2 and two[0] == "▁" and two[-1] == "█", \
            f"две разные точки — низкая и высокая клетки по краям: {two!r}"
        assert _sparkline([75.0, 75.0, 75.0]) == "███", \
            "плоский ряд (min==max) — ровная линия сверху, не пустая и не деление на ноль"
        assert set(_sparkline([90.0, 85.0, 80.0, 75.0])) <= set(_SPARK_BLOCKS), \
            "спарклайн собран только из объявленных блоков"

        long_seq = list(range(90))
        sampled = _stride_sample(long_seq, 30)
        assert len(sampled) <= 30 and sampled[0] == 0 and sampled[-1] == 89, \
            f"прореживание держит первую/последнюю точку и не превышает потолок: {sampled}"
        short_seq = [1, 2, 3]
        assert _stride_sample(short_seq, 30) == short_seq, "короче потолка — вернуть как есть"

        print("OK: спарклайн не падает на 0/1/2/плоском ряде, прореживание держит края")

        print("\n" + "="*60)
        print("TEST 29: help — готовый блок, admin-раздел только в admin-режиме")
        print("="*60)

        # Вне Hermes caller_id нет (см. TEST 10) -> режим по умолчанию user.
        help_result = json.loads(handle_help({}))
        assert list(help_result.keys()) == ["help"], f"Expected only 'help' key, got {help_result}"
        help_text = help_result["help"]
        assert "Что я умею" in help_text, "Справка должна содержать заголовок пользовательской части"
        assert "журнал" in help_text, "Справка должна упоминать «журнал»"
        assert "Админ" not in help_text, (
            "Без caller_id режим обязан резолвиться в user — админский раздел не должен "
            "просачиваться по умолчанию")
        print("OK: справка user-режима без caller_id — без админского раздела")

        # Режим ставим явно, а не полагаемся на то, что TEST 17f оставил caller 999
        # в admin: скрытая зависимость от порядка тестов молча превратила бы этот
        # ассерт в проверку user-справки, то есть в вечнозелёный тест ни о чём.
        _set_mode("999", "admin")
        tools_module._caller_telegram_id = mock_admin
        globals()['_caller_telegram_id'] = mock_admin
        help_admin_text = json.loads(handle_help({}))["help"]
        assert "Админ" in help_admin_text and "admin_cmd" in help_admin_text, \
            f"В admin-режиме справка должна содержать админский раздел, got {help_admin_text!r}"
        tools_module._caller_telegram_id = original_caller
        globals()['_caller_telegram_id'] = original_caller
        print("OK: справка admin-режима включает секцию admin_cmd")

        print("\n" + "="*60)
        print("TEST 30: личность звонящего в слэш-команде (хук pre_gateway_dispatch)")
        print("="*60)

        # Хук зарегистрирован: без него слэш-команда работает над user_id=1.
        assert "pre_gateway_dispatch" in ctx.hooks, \
            f"хук личности не зарегистрирован, есть: {list(ctx.hooks)}"

        class _FakeEvent:
            user_id = "374939064"

        # Хук и читатель берутся из ОДНОГО объекта модуля: `python -m plugin.tools`
        # даёт два разных (`__main__` и `plugin.tools`) со своими ContextVar, и
        # смешивать их — тест, который проверяет не то, что кажется.
        assert tools_module._CALLER_FALLBACK.get() is None, "фолбэк должен стартовать пустым"
        try:
            tools_module._remember_caller(event=_FakeEvent())
            assert tools_module._CALLER_FALLBACK.get() == "374939064", \
                f"хук обязан запомнить автора, получено {tools_module._CALLER_FALLBACK.get()!r}"
            # Вне Hermes session-контекста нет, значит читается именно фолбэк.
            assert _orig_caller_fn() == "374939064", \
                "в слэш-команде личность должна браться из запомненного автора"

            # Хук стоит на пути КАЖДОГО сообщения: мусорное событие не должно
            # ронять обработку чужих сообщений и не должно затирать личность.
            tools_module._remember_caller(event=None)
            tools_module._remember_caller()
            assert tools_module._CALLER_FALLBACK.get() == "374939064", \
                "мусорное событие не должно затирать запомненного звонящего"
        finally:
            # ContextVar переживает тест и увёл бы _get_user_id последующих
            # тестов на несуществующего пользователя.
            tools_module._CALLER_FALLBACK.set(None)

        assert _orig_caller_fn() is None, "после сброса личности снова нет"
        print("OK: хук запоминает автора, мусор игнорирует, фолбэк сбрасывается")

        print("\n" + "="*60)
        print("TEST 31: style — дефолт debian, use/del, гарды на удалении")
        print("="*60)
        conn.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (558001, ?)", (_now_iso(),))
        conn.commit()
        st_uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=558001").fetchone()["id"]

        def _st(**kw):
            return json.loads(handle_style({"user_id": st_uid, **kw}))

        # Нет ни одной строки — _active_style лениво заводит debian и активирует его.
        first = _active_style(conn, st_uid)
        assert first == {"name": "debian", "instruction": _DEFAULT_STYLE_TEXT}, first
        second = _active_style(conn, st_uid)  # повторный вызов не плодит вторую строку
        assert second == first
        assert conn.execute("SELECT COUNT(*) c FROM persona_styles WHERE user_id=?",
                            (st_uid,)).fetchone()["c"] == 1, "повторный _active_style создал дубль"

        # add не активирует; use переключает, и активна ровно одна строка.
        assert "result" in _st(action="add", name="дружелюбный", instruction="Тепло, с юмором, без канцелярита.")
        assert conn.execute("SELECT is_active FROM persona_styles WHERE user_id=? AND name='debian'",
                            (st_uid,)).fetchone()["is_active"] == 1, "add не должен активировать новый стиль"
        assert "result" in _st(action="use", name="дружелюбный")
        active_rows = conn.execute("SELECT name FROM persona_styles WHERE user_id=? AND is_active=1",
                                   (st_uid,)).fetchall()
        assert [r["name"] for r in active_rows] == ["дружелюбный"], \
            f"после use активна должна быть ровно одна строка, получили {active_rows}"

        assert "error" in _st(action="use", name="нет такого"), "use неизвестного стиля — error"

        # debian нельзя удалить — гард держится и БЕЗ confirm, и С ним.
        assert "error" in _st(action="del", name="debian")
        assert "error" in _st(action="del", name="debian", confirm="УДАЛИТЬ")
        assert conn.execute("SELECT COUNT(*) c FROM persona_styles WHERE user_id=? AND name='debian'",
                            (st_uid,)).fetchone()["c"] == 1

        # активный стиль ("дружелюбный") нельзя удалить — тоже без confirm и с ним.
        assert "error" in _st(action="del", name="дружелюбный")
        assert "error" in _st(action="del", name="дружелюбный", confirm="УДАЛИТЬ")
        assert conn.execute("SELECT COUNT(*) c FROM persona_styles WHERE user_id=? AND name='дружелюбный'",
                            (st_uid,)).fetchone()["c"] == 1

        # неактивный, не-debian стиль: del без confirm ничего не удаляет и называет
        # стиль в сообщении; с confirm удаляет по-настоящему.
        _st(action="add", name="строгий", instruction="Только факты и числа.")
        pending = _st(action="del", name="строгий")
        assert "строгий" in pending.get("result", ""), pending
        assert conn.execute("SELECT COUNT(*) c FROM persona_styles WHERE user_id=? AND name='строгий'",
                            (st_uid,)).fetchone()["c"] == 1, "del без confirm не должен удалять"
        assert "result" in _st(action="del", name="строгий", confirm="УДАЛИТЬ")
        assert conn.execute("SELECT COUNT(*) c FROM persona_styles WHERE user_id=? AND name='строгий'",
                            (st_uid,)).fetchone()["c"] == 0

        # get_status_bar отдаёт поле style, равное инструкции активного стиля.
        status_result = json.loads(handle_get_status_bar({"user_id": st_uid, "format": "bar"}))
        assert status_result["style"] == "Тепло, с юмором, без канцелярита.", status_result["style"]

        print("OK: debian сеется лениво и без дублей, use/add/del работают, "
              "оба гарда del держатся даже с confirm, get_status_bar отдаёт активный style")

        print("\n" + "="*60)
        print("TEST 32: вехи закрываются сами и объявляются один раз")
        print("="*60)

        ms_uid = conn.execute(
            "INSERT INTO users(telegram_user_id, height_cm, birth_date, sex, base_weight_kg, created_at) "
            "VALUES (787654, 178, '1985-03-03', 'm', 158.8, '2026-01-01 00:00:00') RETURNING id"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg) "
            "VALUES (?, 'ms_a', '2026-08-22 07:00:00', 120.5, 79.4)", (ms_uid,))
        conn.execute("INSERT INTO milestones(user_id, name, metric, threshold) "
                     "VALUES (?, 'вес 120', 'weight_kg', 120.0)", (ms_uid,))
        conn.execute("INSERT INTO milestones(user_id, name, metric, threshold) "
                     "VALUES (?, 'сухая 82', 'ffm_kg', 82.0)", (ms_uid,))
        conn.commit()

        assert mark_achieved_milestones(conn, ms_uid) == [], \
            "веха не должна закрываться, пока порог не пройден (120.5 при цели 120)"

        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg, ffm_kg) "
            "VALUES (?, 'ms_b', '2026-08-25 07:00:00', 119.4, 79.5)", (ms_uid,))
        conn.commit()
        _closed = mark_achieved_milestones(conn, ms_uid)
        assert [m["name"] for m in _closed] == ["вес 120"], \
            f"должна закрыться ровно веха по весу, получено {_closed}"
        assert mark_achieved_milestones(conn, ms_uid) == [], \
            "закрытая веха обязана объявляться один раз, achieved_at уже проставлен"

        # Направление вехи берётся от точки отсчёта: сухая масса растёт, и
        # наивное «текущее <= порога» закрыло бы её в момент постановки.
        assert conn.execute(
            "SELECT achieved_at FROM milestones WHERE user_id=? AND name='сухая 82'", (ms_uid,)
        ).fetchone()["achieved_at"] is None, "растущая веха (сухая масса) закрыта ошибочно"
        print("OK: веха закрывается по пересечению порога, один раз, с учётом направления")

        print("\n" + "="*60)
        print("TEST 33: слэш-команды — регистрация, /health, /style, /wipe, /users")
        print("="*60)

        register(ctx)  # повторная регистрация в тот же FakeCtx — commands тоже пополнились

        _EXPECTED_CMDS = {"health", "style", "mode", "users", "wipe", "importdata", "week"}
        assert set(ctx.commands.keys()) == _EXPECTED_CMDS, \
            f"не хватает: {_EXPECTED_CMDS - set(ctx.commands)}, лишние: {set(ctx.commands) - _EXPECTED_CMDS}"
        for _n, _info in ctx.commands.items():
            _d = _info["description"]
            assert _d and any("а" <= ch <= "я" or "А" <= ch <= "Я" for ch in _d), \
                f"описание /{_n} пустое или не по-русски: {_d!r}"

        _TAKEN = {"help", "status", "new", "export", "profile", "whoami", "reset", "clear", "voice"}
        _collisions = _EXPECTED_CMDS & _TAKEN
        assert not _collisions, f"имена слэш-команд пересекаются с занятыми Hermes: {_collisions}"
        print(f"OK: все 6 слэш-команд зарегистрированы, без коллизий с {sorted(_TAKEN)}")

        assert "Что я умею" in cmd_health(""), "cmd_health должен вернуть текст help"
        print("OK: /health отдаёт справку")

        # /style под своим caller_id — изолированный пользователь, чтобы не путаться
        # со стилями, которые уже насеяли TEST 31.
        conn.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (556700, ?)", (_now_iso(),))
        conn.commit()
        cmd_uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=556700").fetchone()["id"]

        def _cmd_caller():
            return "556700"
        tools_module._caller_telegram_id = _cmd_caller
        globals()['_caller_telegram_id'] = _cmd_caller

        _style_list = cmd_style("")
        assert "Стили" in _style_list and _DEFAULT_STYLE_NAME in _style_list, _style_list
        _style_bad = cmd_style("use несуществующий")
        assert "⚠" in _style_bad and "Доступны" in _style_bad, _style_bad
        print("OK: /style без аргументов отдаёт список, /style use <нет такого> — отказ со списком доступных")

        # --- /wipe: без подтверждения ничего не удаляет ---
        wp_uid = cmd_uid
        conn.execute("INSERT INTO water_log(user_id, at, volume_ml) VALUES (?, ?, ?)",
                     (wp_uid, _now_iso(), 300))
        conn.execute(
            "INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg) "
            "VALUES (?, 'wipe-burst', ?, 77.7)", (wp_uid, _now_iso()))
        conn.execute("INSERT INTO milestones(user_id, name, metric, threshold) "
                     "VALUES (?, 'wipe-milestone', 'weight_kg', 70.0)", (wp_uid,))
        conn.commit()

        _preview = cmd_wipe("")
        assert "water_log: 1" in _preview and "body_metrics: 1" in _preview, _preview
        assert conn.execute("SELECT COUNT(*) c FROM water_log WHERE user_id=?", (wp_uid,)).fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM body_metrics WHERE user_id=?", (wp_uid,)).fetchone()["c"] == 1
        print("OK: /wipe без подтверждения ничего не удаляет, показывает счётчики")

        # --- /wipe УДАЛИТЬ: логи стёрты, users/milestones/persona_styles целы ---
        _done = cmd_wipe(_WIPE_CONFIRM)
        assert "water_log: 1" in _done and "body_metrics: 1" in _done, _done
        assert conn.execute("SELECT COUNT(*) c FROM water_log WHERE user_id=?", (wp_uid,)).fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM body_metrics WHERE user_id=?", (wp_uid,)).fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM users WHERE id=?", (wp_uid,)).fetchone()["c"] == 1, \
            "/wipe не должен удалять персону"
        assert conn.execute("SELECT COUNT(*) c FROM milestones WHERE user_id=?", (wp_uid,)).fetchone()["c"] == 1, \
            "/wipe не должен трогать milestones"
        assert conn.execute("SELECT COUNT(*) c FROM persona_styles WHERE user_id=?", (wp_uid,)).fetchone()["c"] >= 1, \
            "/wipe не должен трогать persona_styles"
        print("OK: /wipe УДАЛИТЬ стирает логи одной транзакцией, users/milestones/persona_styles целы")

        tools_module._caller_telegram_id = original_caller
        globals()['_caller_telegram_id'] = original_caller

        # --- /users: отказ не-админу ---
        _users_cfg = Path(tmp) / "config_users_cmd.yaml"
        shutil.copy2(config.CONFIG_PATH, _users_cfg)
        _users_cfg.write_text(
            re.sub(r"telegram_admin_ids:.*", "telegram_admin_ids: [900001]",
                   _users_cfg.read_text(encoding="utf-8")),
            encoding="utf-8")
        _saved_cfg_for_users = config.CONFIG_PATH
        config.CONFIG_PATH = _users_cfg
        config.load.cache_clear()
        try:
            def _non_admin_users_caller():
                return "556700"  # существует в БД, но не в allowlist
            tools_module._caller_telegram_id = _non_admin_users_caller
            globals()['_caller_telegram_id'] = _non_admin_users_caller

            _refused = cmd_users("")
            assert "Персоны" not in _refused, f"/users не должен отдать список не-админу: {_refused}"
            assert "⚠" in _refused, _refused
            print("OK: /users отказывает не-админу")
        finally:
            tools_module._caller_telegram_id = original_caller
            globals()['_caller_telegram_id'] = original_caller
            config.CONFIG_PATH = _saved_cfg_for_users
            config.load.cache_clear()

        print("\n" + "="*60)
        print("TEST 34: быстрый ярлык добора БЖУ/воды (+30 белка) в pre_gateway_dispatch")
        print("="*60)

        _qmr = tools_module._quick_macro_rewrite

        # --- позитивные: макросы, варианты написания ---
        for _txt in ("+30 белка", "+30 б", "+30 белок", "+30белка"):
            _r = _qmr(_txt)
            assert _r and "30" in _r and "log_food" in _r and "protein_g" in _r, (_txt, _r)
        for _txt in ("+40 углеводов", "+40 у", "+40 углеводы"):
            _r = _qmr(_txt)
            assert _r and "40" in _r and "log_food" in _r and "carbs_g" in _r, (_txt, _r)
        for _txt in ("+15 жиров", "+15 ж", "+15 жира"):
            _r = _qmr(_txt)
            assert _r and "15" in _r and "log_food" in _r and "fat_g" in _r, (_txt, _r)
        print("OK: белок/углеводы/жир — все варианты написания дают rewrite с нужным числом и полем")

        # --- позитивные: вода, включая литры и запятую ---
        for _txt in ("+500 мл", "+500 воды"):
            _r = _qmr(_txt)
            assert _r and "500" in _r and "log_water" in _r and "ml=500" in _r, (_txt, _r)
        _r_l = _qmr("+0.5 л")
        assert _r_l and "log_water" in _r_l and "ml=500" in _r_l, _r_l
        _r_comma = _qmr("+1,5 л")
        assert _r_comma and "ml=1500" in _r_comma, _r_comma
        print("OK: вода — мл, литры (точка/запятая) конвертируются в мл верно")

        # --- негативные: обычные сообщения проходят нетронутыми ---
        for _txt in (
            "съел 30 г белка",
            "вес 120.5",
            "2+2",
            "+ добавил зелень",
            "",
            "+30",
            "+ добро пожаловать",
            "+30 непонятно",
            "просто текст",
        ):
            assert _qmr(_txt) is None, f"ложное совпадение на: {_txt!r}"
        print("OK: обычные сообщения (в т.ч. с + и цифрами) остаются None — не тронуты")

        # --- хук: rewrite не ломает запоминание звонящего ---
        class _QuickEvent:
            def __init__(self, user_id, text):
                self.user_id = user_id
                self.text = text

        _CALLER_FALLBACK_var = tools_module._CALLER_FALLBACK
        _tok = _CALLER_FALLBACK_var.set(None)
        try:
            _res_macro = tools_module._remember_caller(event=_QuickEvent(778899, "+30 белка"))
            assert _res_macro == {"action": "rewrite", "text": _res_macro["text"]}, _res_macro
            assert "protein_g" in _res_macro["text"], _res_macro
            assert _CALLER_FALLBACK_var.get() == "778899", "ярлык не должен мешать запоминанию звонящего"

            _res_plain = tools_module._remember_caller(event=_QuickEvent(778900, "привет"))
            assert _res_plain is None, _res_plain
            assert _CALLER_FALLBACK_var.get() == "778900", "обычное сообщение тоже должно запомнить звонящего"
        finally:
            _CALLER_FALLBACK_var.reset(_tok)
        print("OK: хук запоминает звонящего и на ярлыке, и на обычном сообщении")

        # --- хук: мусорный event не роняет хук ---
        _res_none = tools_module._remember_caller(event=None)
        assert _res_none is None, _res_none
        _res_no_args = tools_module._remember_caller()
        assert _res_no_args is None, _res_no_args
        print("OK: event=None и вызов без аргументов не роняют хук")

        # --- правдоподобность метки события ---
        # Реальный случай: модель записала воду с at на два года назад. Формат
        # безупречный, суточная сумма её не увидела, человек увидел недобор.
        print()
        print("="*60)
        print("TEST 28: метка события проверяется на правдоподобность")
        print("="*60)
        _wrong_year = (config.local_now().replace(tzinfo=None)
                       - timedelta(days=730)).strftime("%Y-%m-%d %H:%M:%S")
        _r = json.loads(handle_log_water({"user_id": 1, "ml": 500, "at": _wrong_year}))
        assert "error" in _r and "год" in _r["error"], f"ошибка в годе должна отклоняться: {_r}"
        _future = (config.local_now().replace(tzinfo=None)
                   + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        _r = json.loads(handle_log_water({"user_id": 1, "ml": 500, "at": _future}))
        assert "error" in _r and "будущем" in _r["error"], f"будущее должно отклоняться: {_r}"
        # Вчерашняя доливка — законная запись, отказывать нельзя.
        _yesterday = (config.local_now().replace(tzinfo=None)
                      - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        assert _norm_event_ts(_yesterday) == _yesterday, "вчера — нормальная метка"
        # next_at расписания будущий по смыслу и идёт мимо этой проверки.
        _next = (config.local_now().replace(tzinfo=None)
                 + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        assert _norm_ts(_next) == _next, "next_at не должен попадать под окно события"
        print("OK: неверный год и будущее отклонены, вчера и next_at пропущены")

        print("="*60)
        print("TEST 29: sick mode (start/status/stop) and hr_zones in log_workout")
        print("="*60)

        # Test sick status (should be not sick initially)
        handler_sick = ctx.tools["sick"]["handler"]
        result_json = handler_sick({"action": "status", "user_id": uid})
        result = json.loads(result_json)
        assert result.get("sick") is False, f"User should not be sick initially, got {result}"
        print("OK: sick status returns False initially")

        # Test sick start
        result_json = handler_sick({"action": "start", "days": 2, "user_id": uid})
        result = json.loads(result_json)
        assert "from" in result and "to" in result, f"sick start should return from/to, got {result}"
        assert "kcal_target" in result, f"sick start should return kcal_target, got {result}"
        print(f"OK: sick start returns from={result['from']}, to={result['to']}, kcal_target={result.get('kcal_target')}")

        # Test sick status (should be sick now)
        result_json = handler_sick({"action": "status", "user_id": uid})
        result = json.loads(result_json)
        assert result.get("sick") is True, f"User should be sick after start, got {result}"
        print(f"OK: sick status returns True after start, until={result.get('until')}")

        # Test sick stop
        result_json = handler_sick({"action": "stop", "user_id": uid})
        result = json.loads(result_json)
        assert "ok" in result, f"sick stop should return ok, got {result}"
        assert "kcal_target" in result, f"sick stop should return kcal_target, got {result}"
        print(f"OK: sick stop returns ok={result['ok']}, kcal_target={result.get('kcal_target')}")

        # Test log_workout with hr_zones
        handler_workout = ctx.tools["log_workout"]["handler"]
        result_json = handler_workout({
            "action": "add",
            # не «Бег 30», как в контрактном тесте выше: file_hash ручной записи —
            # user+секунда+вид+длительность, в одну секунду они совпадали
            "sport": "VR-кардио",
            "duration_min": 41,
            "kcal": 450,
            "avg_hr": 128,
            "user_id": uid
        })
        result = json.loads(result_json)
        assert "ok" in result, f"log_workout should return ok, got {result}"
        assert "hr_zone" in result, f"log_workout with avg_hr should return hr_zone, got {result}"
        assert "hr_zones" in result, f"log_workout with avg_hr should return hr_zones dict, got {result}"
        hr_zone_value = result.get("hr_zone")
        print(f"OK: log_workout returns hr_zone={hr_zone_value}, hr_zones={result.get('hr_zones')}")

        print("="*60)
        print("TEST 35: Multi-user isolation — milestone/register_user/health_notes")
        print("="*60)

        # Create two users
        conn.execute(
            "INSERT INTO users(telegram_user_id, height_cm, birth_date, sex, created_at) "
            "VALUES (2, 170, '1990-01-01', 'f', '2026-08-20 00:00:00')"
        )
        user_a_id = uid  # user with telegram_user_id=1
        user_b_row = conn.execute(
            "SELECT id FROM users WHERE telegram_user_id=2"
        ).fetchone()
        user_b_id = user_b_row["id"]
        conn.commit()

        # Test milestone isolation via direct handle_set_milestone (not admin_cmd)
        import plugin.tools as tools_module
        tools_module._CALLER_FALLBACK.set("1")  # Set caller to user A
        try:
            # Add milestone directly via handle_set_milestone for user A
            result_json = ctx.tools["set_milestone"]["handler"]({
                "user_id": user_a_id,
                "name": "vega",
                "metric": "weight_kg",
                "threshold": 75
            })
            result = json.loads(result_json)
            assert "id" in result, f"set_milestone should return milestone object with id, got {result}"

            # Verify A's milestone is in DB
            a_milestones = conn.execute(
                "SELECT name FROM milestones WHERE user_id=?",
                (user_a_id,)
            ).fetchall()
            assert any(m["name"] == "vega" for m in a_milestones), "Milestone should be created for user A"

            # Verify B has no milestone
            b_milestones = conn.execute(
                "SELECT name FROM milestones WHERE user_id=?",
                (user_b_id,)
            ).fetchall()
            assert not any(m["name"] == "vega" for m in b_milestones), "User B should not have A's milestone"

            print("OK: Milestones isolated per user")

            # Test health_notes via register_user
            tools_module._CALLER_FALLBACK.set("1")  # User A
            result_json = ctx.tools["register_user"]["handler"]({
                "height_cm": 185,
                "birth_date": "1992-08-09",
                "sex": "m",
                "health_notes": "Восстановление после травмы колена"
            })
            result = json.loads(result_json)
            assert result.get("user_id") == user_a_id, f"Should be registered as user A, got {result}"

            # Verify health_notes persists
            a_notes = conn.execute(
                "SELECT health_notes FROM users WHERE id=?",
                (user_a_id,)
            ).fetchone()["health_notes"]
            assert a_notes == "Восстановление после травмы колена", f"health_notes should persist, got {a_notes}"

            # Re-register without health_notes — should preserve old value (COALESCE)
            result_json = ctx.tools["register_user"]["handler"]({
                "height_cm": 185,
                "birth_date": "1992-08-09",
                "sex": "m"
            })
            result = json.loads(result_json)
            a_notes_after = conn.execute(
                "SELECT health_notes FROM users WHERE id=?",
                (user_a_id,)
            ).fetchone()["health_notes"]
            assert a_notes_after == "Восстановление после травмы колена", \
                f"COALESCE should preserve health_notes when not provided, got {a_notes_after}"

            print("OK: health_notes stored and preserved with COALESCE")

            # Test meal_windows via register_user — тот же механизм, что timezone/health_notes.
            result_json = ctx.tools["register_user"]["handler"]({
                "height_cm": 185,
                "birth_date": "1992-08-09",
                "sex": "m",
                "meal_windows": {"breakfast": {"start": "06:00", "end": "09:00"}}
            })
            result = json.loads(result_json)
            assert "error" not in result, f"register_user с meal_windows сломан: {result}"
            from health_core.chrono import meal_windows as _mw
            a_windows = _mw(conn, user_a_id)
            assert a_windows["breakfast"] == {"start": "06:00", "end": "09:00"}, \
                f"личное окно завтрака должно сохраниться, получили {a_windows['breakfast']}"
            assert a_windows["lunch"]["start"] == "12:00", "частичное переопределение не трогает lunch"
            print("OK: meal_windows задаётся через register_user, частично переопределяя умолчание")

            bad = json.loads(ctx.tools["register_user"]["handler"]({
                "height_cm": 185, "birth_date": "1992-08-09", "sex": "m",
                "meal_windows": {"breakfast": {"start": "7:00", "end": "11:00"}},
            }))
            assert "error" in bad, f"окно без ведущего нуля должно отклоняться: {bad}"
            assert _mw(conn, user_a_id)["breakfast"] == {"start": "06:00", "end": "09:00"}, \
                "отклонённый запрос не должен менять сохранённые окна"
            print("OK: некорректные meal_windows отклоняются, сохранённые окна не трогаются")

            # Личный максимальный пульс через register_user (CONTEXT.md
            # «Максимальный пульс»): hr_max_bpm требует hr_max_source в том же вызове.
            missing_source = json.loads(ctx.tools["register_user"]["handler"]({
                "height_cm": 185, "birth_date": "1992-08-09", "sex": "m",
                "hr_max_bpm": 185,
            }))
            assert "error" in missing_source, f"hr_max_bpm без источника должен отклоняться: {missing_source}"

            out_of_range = json.loads(ctx.tools["register_user"]["handler"]({
                "height_cm": 185, "birth_date": "1992-08-09", "sex": "m",
                "hr_max_bpm": 300, "hr_max_source": "test",
            }))
            assert "error" in out_of_range, f"hr_max_bpm=300 должен отклоняться: {out_of_range}"

            result_json = ctx.tools["register_user"]["handler"]({
                "height_cm": 185, "birth_date": "1992-08-09", "sex": "m",
                "hr_max_bpm": 185, "hr_max_source": "test",
            })
            result = json.loads(result_json)
            assert "error" not in result, f"register_user с hr_max_bpm сломан: {result}"
            a_hr = conn.execute(
                "SELECT hr_max_bpm, hr_max_source FROM users WHERE id=?", (user_a_id,)
            ).fetchone()
            assert a_hr["hr_max_bpm"] == 185 and a_hr["hr_max_source"] == "test", dict(a_hr)
            print("OK: hr_max_bpm=185/source=test сохраняется через register_user, без источника и вне 100-230 отклоняется")

            # Зоны в log_workout и get_trends теперь считаются от личного максимума 185, не от формулы.
            from health_core import hr_zones as _hz
            z_personal = _hz.zones(conn, user_a_id, "2026-09-13")
            assert z_personal["hr_max"] == 185 and z_personal["hr_max_source"] == "test", z_personal

            workout_json = ctx.tools["log_workout"]["handler"]({
                "action": "add", "sport": "бег-личный-максимум", "duration_min": 30,
                "kcal": 300, "avg_hr": 130, "user_id": user_a_id,
            })
            workout_result = json.loads(workout_json)
            assert workout_result["hr_zones"]["hr_max"] == 185, \
                f"log_workout должен считать зоны от личного максимума 185, получили {workout_result.get('hr_zones')}"
            assert workout_result["hr_zones"]["hr_max_source"] == "test", workout_result["hr_zones"]

            trends_json = ctx.tools["get_trends"]["handler"]({"user_id": user_a_id})
            trends_result = json.loads(trends_json)
            assert trends_result["hr_zones"]["hr_max"] == 185, \
                f"get_trends должен считать зоны от личного максимума 185, получили {trends_result.get('hr_zones')}"
            print("OK: log_workout и get_trends считают пульсовые зоны от личного максимума 185 (source=test)")

        finally:
            tools_module._CALLER_FALLBACK.set(None)

        print("="*60)
        print("ALL TESTS PASSED")
        print("="*60)

        # Ensure all connections to the temp database are closed
        if conn:
            try:
                conn.close()
            except:
                pass
        del conn

        # Force garbage collection to ensure file handles are released
        import gc
        gc.collect()

    finally:
        # Clean up temp directory (Windows file locking can cause issues)
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except:
            pass
