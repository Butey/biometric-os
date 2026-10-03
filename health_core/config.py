"""Три уровня настроек (§03): config.yaml (политика) -> users/user_targets (профиль)
-> milestones (вехи, задаёт и правит сам пользователь)."""
import os
import sqlite3
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

_config_cache: dict | None = None
_config_mtime: float | None = None


def load() -> dict:
    """Читает config.yaml, перечитывая его при изменении mtime файла.

    Раньше был @lru_cache(maxsize=1): кэш никогда не сбрасывался сам по себе,
    а admin-панель работает отдельным ОС-процессом (bot/main.py запускает её
    через subprocess.Popen), так что config.load.cache_clear() в её хендлере
    очищал кэш только в памяти admin-процесса — процесс бота о правке не
    узнавал и жил на старых порогах, пока его не перезапустят вручную.
    Проверка mtime работает через файловую систему, а не через сигнал в
    памяти, поэтому видна из обоих процессов одинаково.
    """
    global _config_cache, _config_mtime
    mtime = CONFIG_PATH.stat().st_mtime
    if _config_cache is None or mtime != _config_mtime:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _config_cache = yaml.safe_load(f)
        # Админы задаются окружением (~/.hermes/.env), а не config.yaml: id не должен лежать в git.
        admin_ids = os.environ.get("HEALTH_ADMIN_IDS", "").replace(",", " ").split()
        if admin_ids:
            _config_cache.setdefault("admin", {})["telegram_admin_ids"] = [int(i) for i in admin_ids]
        _config_mtime = mtime
    return _config_cache


# Совместимость: старые вызовы config.load.cache_clear() (admin/server.py,
# admin/auth.py, plugin/tools.py) больше не нужны — mtime к моменту вызова
# уже изменится сам — но пусть остаются безопасным no-op, а не падают с
# AttributeError на функции, у которой больше нет .cache_clear().
load.cache_clear = lambda: None


# Часовой пояс того, кого сейчас обслуживаем. ContextVar, а не глобалка: два
# человека пишут боту одновременно, и глобальная переменная отдала бы одному
# пояс другого. asyncio.to_thread копирует контекст, поэтому значение доезжает
# и до синхронных хендлеров в потоке.
_TZ: ContextVar[str | None] = ContextVar("health_user_tz", default=None)


def set_tz(name: str | None) -> None:
    """Вызывается один раз на входящее сообщение, до исполнения инструментов."""
    _TZ.set(name or None)


def local_now() -> datetime:
    """«Сейчас» глазами обслуживаемого человека. Пояс не выставлен (cron, CLI,
    самотест) — время сервера: предсказуемо и ничего не роняет."""
    name = _TZ.get()
    if name:
        try:
            return datetime.now(ZoneInfo(name)).replace(tzinfo=None)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now()


def local_today() -> str:
    return local_now().date().isoformat()


def user_now(conn: sqlite3.Connection, user_id: int) -> datetime:
    """Текущее время В ЧАСОВОМ ПОЯСЕ ЧЕЛОВЕКА, а не сервера.

    Это не педантизм, а граница суток. Машина разработки стоит в UTC−7, VPS
    обычно в UTC, человек — Europe/Moscow: разрыв до девяти часов. Запись,
    сделанная им в 09:08 утра, при отсчёте по серверу попадает во вчера, вода
    за день показывает 0 при выпитых 500 мл, дневная цель считается не за тот
    день, а гардрейлы недоедания срабатывают на пустом месте. Живой случай,
    пойман 2026-08-22.

    Пояс берём из профиля (users.timezone). Не задан или неизвестен — берём
    default_timezone из config.yaml, и только при его отсутствии падаем на
    время сервера.
    """
    row = conn.execute("SELECT timezone FROM users WHERE id=?", (user_id,)).fetchone()
    name = row["timezone"] if row is not None else None
    if not name:
        name = (load().get("schedule") or {}).get("default_timezone")
    if name:
        try:
            return datetime.now(ZoneInfo(name)).replace(tzinfo=None)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now()


def user_today(conn: sqlite3.Connection, user_id: int) -> str:
    """Сегодняшняя дата глазами человека, YYYY-MM-DD."""
    return user_now(conn, user_id).date().isoformat()


def latest_ffm(conn: sqlite3.Connection, user_id: int) -> float | None:
    """Последняя ИЗМЕРЕННАЯ тощая масса, а не обязательно из последней строки.

    Ручное взвешивание (`log_weight`) пишет строку без ffm_kg. Подстановка
    веса тела вместо неё — не "консервативная оценка", а утверждение, что жира
    нет вовсе: Katch на 121.8 "тощих" кг завышает BMR почти на 900 ккал, а
    норма белка 1.8 г/кг превращается в 219 г вместо 143. Цель прыгала на эти
    900 ккал в обе стороны в зависимости от того, чья строка легла последней.
    Состав тела меняется медленно — замер трёхдневной давности честнее.
    """
    row = conn.execute(
        "SELECT ffm_kg FROM body_metrics WHERE user_id=? AND ffm_kg IS NOT NULL "
        "ORDER BY measured_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return row["ffm_kg"] if row else None


def targets_for(conn: sqlite3.Connection, user_id: int) -> dict:
    policy = load()["policy"]

    row = conn.execute(
        "SELECT weight_kg, ffm_kg FROM body_metrics WHERE user_id=? "
        "ORDER BY measured_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is not None:
        weight_kg = row["weight_kg"]
        # FFM не снят этими весами -> последний известный замер; его нет вовсе -> вес
        # (единственный честный вариант на старте, до первого взвешивания составом).
        ffm_kg = row["ffm_kg"] or latest_ffm(conn, user_id) or weight_kg
    else:
        urow = conn.execute("SELECT base_weight_kg FROM users WHERE id=?", (user_id,)).fetchone()
        weight_kg = urow["base_weight_kg"] if urow else None
        ffm_kg = weight_kg

    if weight_kg is None:
        return {}

    return {
        "protein_g": round(policy["protein_g_per_kg_ffm"] * ffm_kg, 1),
        # Абсолютных граммов жира тут нет: он доля от калоража, а калораж
        # считается позже (energy.daily_target). Отдаём доли.
        "fat_pct": float(policy.get("fat_pct_of_kcal", 0.30)),
        "fat_pct_min": float(policy.get("fat_pct_of_kcal_min", 0.20)),
        # Не масштабируется от массы: цель клетчатки абсолютная (см. config.yaml).
        "fiber_g": float(policy.get("fiber_g_per_day", 25)),
        "water_ml": round(policy["water_ml_per_kg"] * weight_kg, 1),
    }
