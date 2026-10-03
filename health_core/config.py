"""Три уровня настроек (§03): config.yaml (политика) -> users/user_targets (профиль)
-> milestones (вехи, задаёт и правит сам пользователь)."""
import logging
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
# База для проверки при горячей перезагрузке: из какого файла загружен кэш,
# его ключи (пути вида guards.bp_high_sys) и mtime последнего отклонённого файла.
_config_path: Path | None = None
_config_keys: set = set()
_config_bad_mtime: float | None = None


def _key_paths(d: dict, prefix: str = "") -> set:
    out = set()
    for k, v in d.items():
        out.add(f"{prefix}{k}")
        if isinstance(v, dict):
            out |= _key_paths(v, f"{prefix}{k}.")
    return out


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
    global _config_cache, _config_mtime, _config_path, _config_keys, _config_bad_mtime
    mtime = CONFIG_PATH.stat().st_mtime
    # Пустой кэш или подменённый CONFIG_PATH (тесты) - первая загрузка со своей базой.
    first = _config_cache is None or CONFIG_PATH != _config_path
    if not first and mtime in (_config_mtime, _config_bad_mtime):
        return _config_cache
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            new = yaml.safe_load(f)
        if not isinstance(new, dict):
            raise ValueError(f"корень файла - {type(new).__name__}, а не словарь")
        # Админы задаются окружением (~/.hermes/.env), а не config.yaml: id не должен лежать в git.
        admin_ids = os.environ.get("HEALTH_ADMIN_IDS", "").replace(",", " ").split()
        if admin_ids:
            new.setdefault("admin", {})["telegram_admin_ids"] = [int(i) for i in admin_ids]
        keys = _key_paths(new)
        # Пропавший ключ ломает код, который уже загружен и обращается к нему по индексу
        # (cfg["guards"]["x"]): такой файл вступит в силу только после рестарта процесса.
        missing = sorted(_config_keys - keys) if not first else []
        if missing:
            raise ValueError("пропали ключи: " + ", ".join(missing[:10])
                             + (f" (и ещё {len(missing) - 10})" if len(missing) > 10 else ""))
    except Exception as e:
        if first:
            raise
        _config_bad_mtime = mtime
        logging.getLogger(__name__).error("config.yaml отклонён: %s", e)
        return _config_cache
    if first:
        _config_keys = keys
    _config_cache, _config_mtime, _config_path, _config_bad_mtime = new, mtime, CONFIG_PATH, None
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


if __name__ == "__main__":
    import tempfile

    class _Grab(logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs = []

        def emit(self, record):
            self.msgs.append(record.getMessage())

    grab = _Grab()
    logging.getLogger(__name__).addHandler(grab)
    logging.getLogger(__name__).propagate = False
    os.environ.pop("HEALTH_ADMIN_IDS", None)

    with tempfile.TemporaryDirectory() as tmp:
        CONFIG_PATH = Path(tmp) / "config.yaml"
        _config_cache = None
        tick = [1000]

        def put(text):
            CONFIG_PATH.write_text(text, encoding="utf-8")
            tick[0] += 10
            os.utime(CONFIG_PATH, (tick[0], tick[0]))

        base = "guards:\n  bp_high_sys: 140\n  glucose_sd_threshold: 2.5\npolicy:\n  water: 30\n"
        put(base)
        assert load()["guards"]["bp_high_sys"] == 140
        print("OK: первая загрузка запоминает базу ключей")

        put(base.replace("140", "150"))
        assert load()["guards"]["bp_high_sys"] == 150 and not grab.msgs
        print("OK: изменение значения принято")

        put(base.replace("140", "150") + "extra:\n  new_key: 1\n")
        assert load()["extra"]["new_key"] == 1 and not grab.msgs
        print("OK: новый ключ принят")

        put("guards:\n  bp_high_sys: 99\npolicy:\n  water: 30\nextra:\n  new_key: 1\n")
        assert load()["guards"]["bp_high_sys"] == 150 and "glucose_sd_threshold" in load()["guards"]
        load(); load()
        assert len(grab.msgs) == 1, grab.msgs
        assert grab.msgs[0].startswith("config.yaml отклонён:") and "guards.glucose_sd_threshold" in grab.msgs[0]
        print("OK: пропавший вложенный ключ отклонён, старый конфиг остался, ошибка залогирована один раз")

        grab.msgs.clear()
        put("guards: [unclosed\n  x: : :")
        assert load()["guards"]["bp_high_sys"] == 150
        load()
        assert len(grab.msgs) == 1 and grab.msgs[0].startswith("config.yaml отклонён:"), grab.msgs
        print("OK: битый YAML отклонён, старый конфиг остался")

        grab.msgs.clear()
        put("- not\n- a dict\n")
        assert load()["guards"]["bp_high_sys"] == 150 and len(grab.msgs) == 1
        print("OK: YAML не словарь отклонён")

        grab.msgs.clear()
        put(base.replace("140", "160"))
        assert load()["guards"]["bp_high_sys"] == 160 and not grab.msgs
        print("OK: после починки файл снова принимается")

        other = Path(tmp) / "other.yaml"
        other.write_text("only: 1\n", encoding="utf-8")
        CONFIG_PATH = other
        assert load() == {"only": 1} and not grab.msgs
        print("OK: подмена CONFIG_PATH - свежая база, а не отклонённая перезагрузка")

    print("OK: config.py self-check passed")
