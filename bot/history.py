"""История диалога на пользователя в SQLite + обрезка окна (Docs/bot_design.md).

Своя таблица, свой CREATE TABLE IF NOT EXISTS — health_core/db.py не трогаем.
Соединение приходит снаружи (health_core.db.connect(), row_factory=Row).
"""
import json
from datetime import datetime

from health_core.config import load as _load_config

_DEFAULT_WINDOW = 20
_DEFAULT_CHARS = 8000


def _now_iso() -> str:
    # тот же формат, что и в plugin/tools.py._now_iso: "YYYY-MM-DD HH:MM:SS",
    # без 'T' — единообразие с остальной базой, хотя тут это отдельная таблица.
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat_history "
        "(telegram_user_id TEXT, ts TEXT, role TEXT, content TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_history_user ON chat_history(telegram_user_id)"
    )


def _bot_config() -> tuple[int, int]:
    # раздела bot: в config.yaml может не быть вовсе (пишется параллельно) —
    # тогда умолчания, без падения.
    cfg = _load_config().get("bot") or {}
    return (
        int(cfg.get("history_window", _DEFAULT_WINDOW)),
        int(cfg.get("history_chars", _DEFAULT_CHARS)),
    )


def _trim(rows: list, window: int, chars: int) -> list:
    """rows — в хронологическом порядке (старые первыми)."""
    tail = rows[-window:] if window > 0 else []
    total = sum(len(r["content"]) for r in tail)
    while tail and total > chars:
        total -= len(tail[0]["content"])
        tail = tail[1:]
    # Граница хода: список обязан начинаться с role "user". Иначе возможен
    # "tool" без своего предшествующего "assistant" с tool_calls в начале —
    # такой список провайдер отвергнет. Сдвигаемся вперёд до ближайшего "user".
    while tail and tail[0]["role"] != "user":
        tail = tail[1:]
    return tail


def load(conn, telegram_id: str) -> list[dict]:
    """Окно последних сообщений: до history_window штук И до history_chars символов,
    что жёстче, срезано строго по границе хода (см. _trim)."""
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT ts, role, content FROM chat_history WHERE telegram_user_id=? ORDER BY rowid ASC",
        (str(telegram_id),),
    ).fetchall()
    window, chars = _bot_config()
    return [json.loads(r["content"]) for r in _trim(rows, window, chars)]


def append(conn, telegram_id: str, message: dict) -> None:
    """message — dict формата OpenAI chat (role/content, возможны tool_calls /
    tool_call_id) — хранится сериализованным целиком, чтобы ничего не потерять."""
    _ensure_table(conn)
    conn.execute(
        "INSERT INTO chat_history(telegram_user_id, ts, role, content) VALUES (?, ?, ?, ?)",
        (str(telegram_id), _now_iso(), message.get("role", ""),
         json.dumps(message, ensure_ascii=False)),
    )
    conn.commit()


def clear(conn, telegram_id: str) -> None:
    _ensure_table(conn)
    conn.execute("DELETE FROM chat_history WHERE telegram_user_id=?", (str(telegram_id),))
    conn.commit()


if __name__ == "__main__":
    import os
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HEALTH_DB"] = str(Path(tmp) / "health.db")
        import health_core.db as db
        db.DB_PATH = Path(os.environ["HEALTH_DB"])
        conn = db.connect()

        # --- round-trip: tool_calls переживают append -> load ---
        append(conn, "1", {"role": "user", "content": "привет"})
        append(conn, "1", {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "log_food", "arguments": "{}"}}],
        })
        append(conn, "1", {"role": "tool", "tool_call_id": "c1", "content": "ok"})
        append(conn, "1", {"role": "assistant", "content": "готово"})
        hist = load(conn, "1")
        assert len(hist) == 4
        assert hist[1]["tool_calls"][0]["function"]["name"] == "log_food", "tool_calls не пережили round-trip"
        assert hist[2]["tool_call_id"] == "c1"
        print("OK: tool_calls переживают append -> load")

        # --- clear чистит только своего пользователя ---
        append(conn, "2", {"role": "user", "content": "чужое сообщение"})
        clear(conn, "1")
        assert load(conn, "1") == [], "clear не должен оставлять сообщения своего пользователя"
        assert len(load(conn, "2")) == 1, "clear задел чужого пользователя"
        print("OK: clear чистит только своего пользователя")

        # --- обрезка по количеству: не больше history_window, и не оставляет tool в голове ---
        clear(conn, "3")
        for i in range(30):
            append(conn, "3", {"role": "user", "content": f"вопрос {i}"})
            append(conn, "3", {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": f"c{i}", "type": "function",
                                 "function": {"name": "noop", "arguments": "{}"}}],
            })
            append(conn, "3", {"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})
        hist3 = load(conn, "3")
        assert len(hist3) <= 20, f"окно не должно превышать history_window, получили {len(hist3)}"
        assert hist3[0]["role"] == "user", (
            f"обрезка не должна оставлять '{hist3[0]['role']}' в голове списка"
        )
        print(f"OK: обрезка по count держит окно ({len(hist3)} сообщений), голова — 'user'")

        # --- обрезка по символам: длинные сообщения давят лимит раньше, чем count ---
        clear(conn, "4")
        big = "x" * 3000
        for i in range(10):
            append(conn, "4", {"role": "user", "content": f"{big}-{i}"})
            append(conn, "4", {"role": "assistant", "content": "ответ"})
        hist4 = load(conn, "4")
        total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in hist4)
        assert len(hist4) < 20, "лимит символов должен был обрезать раньше, чем 20 сообщений"
        assert hist4[0]["role"] == "user"
        print(f"OK: обрезка по chars ({len(hist4)} сообщений, ~{total_chars} симв.), голова — 'user'")

        # --- умолчания при отсутствии bot: в config.yaml (не падаем) ---
        window, chars = _bot_config()
        assert isinstance(window, int) and isinstance(chars, int)
        print(f"OK: _bot_config() не падает без секции bot: -> window={window}, chars={chars}")

        conn.close()  # на Windows temp dir не удалится, пока файл БД открыт

    print("ALL OK: bot/history.py")
