"""Консилиум (docs/adr/0003-консилиум.md, CONTEXT.md «Консилиум»): честный
разбор пакета данных health_core.council_data несколькими независимыми
моделями в фоне. Два аналитика (разные модели Gemini) вызываются ПО ОЧЕРЕДИ,
не параллельно, чтобы не выбивать общую бесплатную квоту Google; судья видит
их разборы анонимно, в случайном порядке. Хранение — таблица council_runs
(health_core/db.py).

Публичный вход:
    reserve(conn, user_id, reason) -> run_id        синхронно, быстро: лок + частота
    execute(conn, user_id, run_id, reason) -> dict   асинхронно: сама работа
    run(conn, user_id, reason) -> dict               reserve()+execute() одним вызовом
    latest_run(conn, user_id) -> dict | None

queue_run()/_PENDING_RUNS — очередь для фонового запуска ботом: тул
(plugin/tools.py) резервирует run_id синхронно и кладёт заявку сюда,
bot/main.py забирает её после ответа модели и запускает execute() отдельной
asyncio-задачей, не блокируя обработку сообщений (тот же приём, что и очередь
уведомлений bot/main.py::_PENDING_USER_NOTIFICATIONS).

bot/council.py НЕ импортирует bot.registry/bot.main/plugin.tools — это тупиковый
модуль, на него можно ссылаться из plugin/tools.py без риска цикличного импорта.
"""
import asyncio
import json
import logging
import random
import sqlite3
from datetime import timedelta

import aiohttp

from bot import llm
from health_core import council_data
from health_core.config import load, local_now

log = logging.getLogger(__name__)

_ANALYST_SYSTEM = (
    "Ты — честный клинический аналитик в консилиуме по снижению веса на терапии "
    "GLP-1. Разбирай только присланные данные: выводы делай из чисел, "
    "неопределённость там, где данных мало, называй прямо. Числа не выдумывай — "
    "только те, что есть в пакете. Дозу не назначай: только соображения в "
    "рамках лестницы титрации, с оговоркой, что решение остаётся за врачом."
)

_JUDGE_SYSTEM = (
    "Ты — судья консилиума по снижению веса на терапии GLP-1. Тебе присылают "
    "один или два анонимных разбора одних и тех же данных («Разбор A», «Разбор "
    "B» — порядок случайный, авторов не называют). Сведи их в итог: общие "
    "выводы; в чём разборы согласны и в чём расходятся; что стоит проверить "
    "дальше; соображения по дозе (только в рамках лестницы титрации, с "
    "оговоркой про врача) и по рефиду, если это применимо. Числа не выдумывай — "
    "только те, что видны в присланных данных и разборах."
)

_RATE_LIMIT_HOURS = {"manual": 6, "dose": 6}
_PLATEAU_DAYS = 21
_LABELS = "ABCDEFGH"

# (telegram_uid, user_id, run_id, reason) — см. докстринг модуля.
_PENDING_RUNS: list[tuple[str, int, int, str]] = []


def queue_run(telegram_uid: str, user_id: int, run_id: int, reason: str) -> None:
    _PENDING_RUNS.append((telegram_uid, user_id, run_id, reason))


def _cfg() -> dict:
    return load()["council"]


def _provider(model: str) -> dict:
    """Провайдер консилиума — ссылка по имени модели на bot.providers, а не
    отдельный дубль base_url/api_key_env (единый источник правды о том, как
    достучаться до модели)."""
    for p in load()["bot"]["providers"]:
        if p.get("model") == model:
            return p
    raise ValueError(f"провайдер с моделью {model!r} не найден в bot.providers")


def _now_iso() -> str:
    return local_now().strftime("%Y-%m-%d %H:%M:%S")


def reserve(conn: sqlite3.Connection, user_id: int, reason: str) -> int:
    """Лок + частота (docs/adr/0003): не больше одного running на пользователя;
    manual/dose — не чаще раза в 6 часов; plateau — не чаще раза в 21 день.
    Заводит running-строку и возвращает её id, либо бросает ValueError с
    человекочитаемой причиной отказа — вызывающий (тул, morning_checkin)
    решает, что с этим делать."""
    if reason not in ("manual", "dose", "plateau"):
        raise ValueError(f"неизвестная причина консилиума: {reason!r}")
    # running старше часа — прогон, оборванный перезапуском бота: иначе он навсегда занял бы лок
    stale_before = (local_now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE council_runs SET status='failed', complete=0, finished_at=?, "
        "result_text='Консилиум прерван (перезапуск или сбой).' "
        "WHERE user_id=? AND status='running' AND started_at<?",
        (_now_iso(), user_id, stale_before),
    )
    if conn.execute(
        "SELECT 1 FROM council_runs WHERE user_id=? AND status='running'", (user_id,)
    ).fetchone():
        raise ValueError("Консилиум уже идёт — дождитесь итога.")

    if reason == "plateau":
        since = (local_now() - timedelta(days=_PLATEAU_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        window_msg = f"{_PLATEAU_DAYS} дней"
    else:
        hours = _RATE_LIMIT_HOURS[reason]
        since = (local_now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        window_msg = f"{hours} часов"
    if conn.execute(
        "SELECT 1 FROM council_runs WHERE user_id=? AND reason=? AND started_at>=?",
        (user_id, reason, since),
    ).fetchone():
        raise ValueError(f"Консилиум по причине «{reason}» уже собирался за последние {window_msg}.")

    cur = conn.execute(
        "INSERT INTO council_runs(user_id, reason, status, complete, started_at) VALUES (?,?,?,?,?)",
        (user_id, reason, "running", None, _now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def _finish(conn: sqlite3.Connection, run_id: int, status: str, complete: int, text: str) -> None:
    conn.execute(
        "UPDATE council_runs SET status=?, complete=?, finished_at=?, result_text=? WHERE id=?",
        (status, complete, _now_iso(), text, run_id),
    )
    conn.commit()


async def _retry_wait(delay_s: float) -> None:
    """Пауза перед повтором — отдельной функцией ради самотеста: подменяем её,
    чтобы не спать реальные retry_delay_s секунд (по умолчанию 60) на каждый
    сценарий сбоя (тот же приём, что llm._post ради сети)."""
    await asyncio.sleep(delay_s)


async def _call_one(session: aiohttp.ClientSession, system: str, user_text: str,
                     provider: dict, timeout_s: float, retry_delay_s: float, max_retries: int) -> str | None:
    """Один провайдер, БЕЗ фолбэка на другую модель — аналитик/судья это
    конкретная модель, не цепочка. Сбой -> до max_retries повторов через
    retry_delay_s, иначе None."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_text}]
    for attempt in range(max_retries + 1):
        try:
            # timeout_s провайдера — короткий чатовый (быстрый фолбэк), консилиуму
            # нужен свой в минуты, иначе он перебивает council.timeout_s.
            message = await llm.chat(session, messages, [], [{**provider, "timeout_s": timeout_s}],
                                     timeout_s=timeout_s)
            text = (message.get("content") or "").strip()
            if text:
                return text
            log.warning("консилиум: %s вернул пустой ответ", provider.get("model"))
        except Exception:
            log.warning("консилиум: %s упал (попытка %d/%d)",
                        provider.get("model"), attempt + 1, max_retries + 1, exc_info=True)
        if attempt < max_retries:
            await _retry_wait(retry_delay_s)
    return None


def _format_data_pack(data: dict, reason: str) -> str:
    return (
        f"Причина созыва консилиума: {reason}.\n\n"
        "Данные (JSON, посчитаны кодом, ничего сверх них не выдумывай):\n"
        f"{json.dumps(data, ensure_ascii=False, default=str)}"
    )


async def execute(conn: sqlite3.Connection, user_id: int, run_id: int, reason: str) -> dict:
    """Сама работа консилиума — run_id уже зарезервирован через reserve().
    Пишет итог в council_runs и возвращает {"status", "complete", "text"};
    "text" — готовое сообщение для отправки человеку отдельным сообщением."""
    cfg = _cfg()
    timeout_s, retry_delay_s, max_retries = cfg["timeout_s"], cfg["retry_delay_s"], cfg["max_retries"]

    data = await asyncio.to_thread(council_data.build, conn, user_id)
    pack_text = _format_data_pack(data, reason)

    async with aiohttp.ClientSession() as session:
        analyst_texts: list[str] = []
        analyst_failures: list[str] = []
        for model in cfg["analysts"]:
            provider = _provider(model)
            text = await _call_one(session, _ANALYST_SYSTEM, pack_text, provider,
                                   timeout_s, retry_delay_s, max_retries)
            if text:
                analyst_texts.append(text)
            else:
                analyst_failures.append(model)

        if not analyst_texts:
            result_text = (
                "Консилиум не состоялся: ни один аналитик не ответил "
                f"({', '.join(analyst_failures)})."
            )
            _finish(conn, run_id, "failed", 0, result_text)
            return {"status": "failed", "complete": 0, "text": result_text}

        order = list(range(len(analyst_texts)))
        random.shuffle(order)
        judge_input = "\n\n".join(
            f"Разбор {_LABELS[i]}:\n{analyst_texts[idx]}" for i, idx in enumerate(order)
        )
        judge_prompt = pack_text + "\n\n" + judge_input

        judge_text = None
        for model in (cfg["judge"], cfg["judge_fallback"]):
            provider = _provider(model)
            judge_text = await _call_one(session, _JUDGE_SYSTEM, judge_prompt, provider,
                                         timeout_s, retry_delay_s, max_retries)
            if judge_text:
                break

    complete = 1 if len(analyst_texts) == len(cfg["analysts"]) else 0
    header = "⚠ Консилиум неполный: ответил не весь состав аналитиков.\n\n" if not complete else ""

    if judge_text is None:
        result_text = header + "Судья недоступен, итог не сформирован. Разборы аналитиков:\n\n" + judge_input
        _finish(conn, run_id, "failed", complete, result_text)
        return {"status": "failed", "complete": complete, "text": result_text}

    result_text = header + judge_text
    _finish(conn, run_id, "done", complete, result_text)
    return {"status": "done", "complete": complete, "text": result_text}


async def run(conn: sqlite3.Connection, user_id: int, reason: str) -> dict:
    """reserve() + execute() одним вызовом — для скриптов (morning_checkin.py)
    и самотестов. Блокирован (лок/частота) -> ValueError, ничего не запускается."""
    run_id = reserve(conn, user_id, reason)
    return await execute(conn, user_id, run_id, reason)


def latest_run(conn: sqlite3.Connection, user_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, reason, status, complete, started_at, finished_at, result_text "
        "FROM council_runs WHERE user_id=? ORDER BY started_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


if __name__ == "__main__":
    import os
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    tmp = tempfile.mkdtemp()
    os.environ["HEALTH_DB"] = str(Path(tmp) / "health.db")
    import health_core.db as db
    db.DB_PATH = Path(os.environ["HEALTH_DB"])
    conn = db.connect()
    db.migrate(conn)
    conn.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (1, '2026-08-01 00:00:00')")
    conn.commit()
    uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]

    os.environ["GOOGLE_API_KEY"] = "test"

    def _msg(text: str) -> dict:
        return {"role": "assistant", "content": text, "tool_calls": None}

    async def _fast_wait(_delay_s: float) -> None:
        return None

    async def main() -> None:
        with patch(__name__ + "._retry_wait", _fast_wait):
            cfg = _cfg()
            assert cfg["analysts"] == ["gemini-3.8-flash", "gemini-3.7-flash"], cfg
            assert cfg["judge"] == "gemini-3.6-flash" and cfg["judge_fallback"] == "gemini-3.5-flash-lite" and cfg["max_retries"] == 1, cfg

            # --- 1) оба аналитика ответили, судья ответил -> complete=1, status=done ---
            queue = [_msg("разбор первого аналитика"), _msg("разбор второго аналитика"), _msg("итог судьи")]

            async def _post_ok(session, url, headers, payload, timeout_s=None):
                return 200, {"choices": [{"message": queue.pop(0)}]}

            with patch("bot.llm._post", _post_ok):
                result = await run(conn, uid, "manual")
            assert result["status"] == "done" and result["complete"] == 1, result
            assert result["text"] == "итог судьи", result
            row = latest_run(conn, uid)
            assert row["status"] == "done" and row["complete"] == 1 and row["reason"] == "manual", row
            print("OK: 1) оба аналитика + судья ответили -> complete=1")

            # --- 2) второй аналитик недоступен весь запас попыток -> судья работает с одним, "неполный" ---
            queue2 = [
                _msg("разбор первого аналитика"),
                Exception("сеть легла"), Exception("сеть легла"),  # второй аналитик: 2 попытки (max_retries=1)
                _msg("итог по одному разбору"),
            ]

            async def _post_partial(session, url, headers, payload, timeout_s=None):
                item = queue2.pop(0)
                if isinstance(item, Exception):
                    raise item
                return 200, {"choices": [{"message": item}]}

            with patch("bot.llm._post", _post_partial):
                result2 = await run(conn, uid, "dose")
            assert result2["status"] == "done" and result2["complete"] == 0, result2
            assert "неполный" in result2["text"], result2["text"]
            assert result2["text"].endswith("итог по одному разбору"), result2["text"]
            print("OK: 2) один аналитик недоступен -> судья работает с одним, итог помечен неполным")

            # --- 3) ни один аналитик не ответил -> "консилиум не состоялся", судью не зовём ---
            conn.execute("DELETE FROM council_runs WHERE user_id=?", (uid,))
            conn.commit()
            queue3 = [Exception("сеть легла")] * 4  # 2 аналитика x 2 попытки

            async def _post_all_down(session, url, headers, payload, timeout_s=None):
                raise queue3.pop(0)

            with patch("bot.llm._post", _post_all_down):
                result3 = await run(conn, uid, "manual")
            assert result3["status"] == "failed" and result3["complete"] == 0, result3
            assert "не состоялся" in result3["text"], result3["text"]
            print("OK: 3) ни один аналитик не ответил -> «консилиум не состоялся»")

            # --- 4) судья недоступен -> judge_fallback подхватывает ---
            conn.execute("DELETE FROM council_runs WHERE user_id=?", (uid,))
            conn.commit()
            queue4 = [
                _msg("разбор первого аналитика"), _msg("разбор второго аналитика"),
                Exception("судья лёг"), Exception("судья лёг"),  # судья: 2 попытки
                _msg("итог от fallback-судьи"),
            ]

            async def _post_judge_fallback(session, url, headers, payload, timeout_s=None):
                item = queue4.pop(0)
                if isinstance(item, Exception):
                    raise item
                return 200, {"choices": [{"message": item}]}

            with patch("bot.llm._post", _post_judge_fallback):
                result4 = await run(conn, uid, "dose")
            assert result4["status"] == "done" and result4["complete"] == 1, result4
            assert result4["text"] == "итог от fallback-судьи", result4
            print("OK: 4) судья недоступен -> judge_fallback подхватывает")

        # --- 5) лок: второй request раньше 6ч по той же причине -> отказ ---
        try:
            reserve(conn, uid, "dose")
            assert False, "повторный dose-request раньше 6ч должен быть отклонён"
        except ValueError as e:
            assert "6 часов" in str(e) or "часов" in str(e), str(e)
        print("OK: 5) повторный request раньше 6ч отклонён")

        # --- 6) лок: нельзя завести второй running одновременно ---
        conn.execute("DELETE FROM council_runs WHERE user_id=?", (uid,))
        conn.commit()
        run_id = reserve(conn, uid, "manual")
        try:
            reserve(conn, uid, "dose")
            assert False, "второй running на того же пользователя должен быть отклонён"
        except ValueError as e:
            assert "идёт" in str(e), str(e)
        conn.execute("UPDATE council_runs SET status='done' WHERE id=?", (run_id,))
        conn.commit()
        print("OK: 6) второй running на того же пользователя отклонён")

    asyncio.run(main())
    conn.close()
    print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
