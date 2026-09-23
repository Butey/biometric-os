"""Инструмент knowledge(topic) поверх Knowledge/ — Docs/bot_design.md.

Каталог читается ПРИ КАЖДОМ вызове (index/schema/read), а не кэшируется при
импорте: файлы добавляются через веб-админку на живой системе, перезапуск
бота ради нового файла недопустим.
"""
from pathlib import Path

from plugin import tools as _tools

# Переменная модуля, а не константа внутри функций — самотест подменяет её на
# временный каталог, боевой Knowledge/ не засоряется.
KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "Knowledge"

_EXTS = (".md", ".txt")
_ADMIN_ONLY = "admin_commands"
_FULL_LIMIT = 20 * 1024   # файл целиком, байт
_SEARCH_LIMIT = 8 * 1024  # выдача по query, байт максимум


def _is_admin() -> bool:
    return _tools._get_mode(_tools._caller_telegram_id() or "") == "admin"


def _personal_dir() -> Path | None:
    """Knowledge/personal/<telegram id>/ — личные документы звонящего (его
    протокол, вехи, заметки). Видны только ему; общий индекс их не содержит."""
    tg = _tools._caller_telegram_id()
    return KNOWLEDGE_DIR / "personal" / tg if tg and str(tg).isdigit() else None


def _dirs() -> list[Path]:
    personal = _personal_dir()
    return [d for d in (personal, KNOWLEDGE_DIR) if d is not None and d.is_dir()]


def _topics() -> list[Path]:
    """Файлы Knowledge/*.md и *.txt плюс личные файлы звонящего, по одному на
    тему (личный файл и .md приоритетнее при совпадении имени), отсортированы по
    имени темы для стабильного вывода."""
    by_stem: dict[str, Path] = {}
    for d in _dirs():
        for ext in _EXTS:
            for p in d.glob(f"*{ext}"):
                by_stem.setdefault(p.stem, p)
    admin = _is_admin()
    return [p for stem, p in sorted(by_stem.items()) if admin or stem != _ADMIN_ONLY]


def _describe(text: str) -> str:
    """Первая непустая содержательная строка файла: пропускаем YAML frontmatter
    (файл может открываться блоком --- ... ---, как admin_commands.md),
    markdown-заголовки, таблицы, и PDF-extraction artifacts, обрезаем до 100 символов."""
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                start = j + 1
                break
    for line in lines[start:]:
        s = line.strip()
        # Skip empty lines and markdown headings
        if not s or s.startswith("#"):
            continue
        # Skip markdown tables (contain |)
        if "|" in s:
            continue
        # Skip very short lines (likely PDF artifacts or single chars)
        if len(s) < 10:
            continue
        # Skip all-uppercase short lines (PDF extraction artifacts)
        if s.isupper() and len(s) < 40:
            continue
        return s[:100]
    return ""


def index() -> str:
    items = [
        f"- **{p.stem}** — {_describe(p.read_text(encoding='utf-8', errors='replace'))}"
        for p in _topics()
    ]
    return "\n".join(items) if items else "(база знаний пуста)"


def schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "enum": [p.stem for p in _topics()],
                "description": "Тема из базы знаний Knowledge/",
            },
            "query": {
                "type": "string",
                "description": "Подстрока поиска — обязательна для файлов больше 20 KB",
            },
        },
        "required": ["topic"],
    }


def _resolve(topic: str) -> Path | None:
    """Путь собирается от KNOWLEDGE_DIR + расширение; topic приходит от модели —
    граница доверия, поэтому итог обязан лежать ВНУТРИ каталога (resolve +
    сравнение), иначе "../../.env" утечёт файлом."""
    import re
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", topic):
        return None
    # Файл обязан лежать ПРЯМО в своём каталоге, а не где-то внутри: иначе
    # topic "personal/<чужой id>/protocol" прочитал бы чужие личные документы.
    for d in _dirs():
        base = d.resolve()
        for ext in _EXTS:
            candidate = (d / f"{topic}{ext}").resolve()
            if candidate.parent == base and candidate.is_file():
                return candidate
    return None


def read(topic: str, query: str | None = None) -> str:
    if topic == _ADMIN_ONLY and not _is_admin():
        return f"Тема «{topic}» недоступна."
    path = _resolve(topic)
    if path is None:
        return f"Тема «{topic}» не найдена. Список тем — в knowledge index."

    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text.encode("utf-8")) <= _FULL_LIMIT:
        return text
    if not query:
        return (f"Файл «{topic}» больше 20 KB — нужен query: подстрока, по которой "
                f"выбрать абзацы.")

    # ponytail: подстрочный поиск, не эмбеддинги — менять, когда книг станет больше десятка
    q = query.lower()
    hits = [p for p in text.split("\n\n") if q in p.lower()]
    if not hits:
        return f"По запросу «{query}» в «{topic}» ничего не найдено."

    out, total = [], 0
    for p in hits:
        b = len(p.encode("utf-8"))
        if out and total + b > _SEARCH_LIMIT:
            break
        out.append(p)
        total += b
    remaining = len(hits) - len(out)
    result = "\n\n".join(out)
    if remaining > 0:
        result += f"\n\n… ещё {remaining} совпадений не показано, уточните query."
    return result


if __name__ == "__main__":
    import os
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as tmp:
        # --- временная БД (режим admin/user хранится в ней через plugin.tools) ---
        os.environ["HEALTH_DB"] = str(_P(tmp) / "health.db")
        import health_core.db as db
        db.DB_PATH = _P(os.environ["HEALTH_DB"])

        # --- временный каталог знаний, боевой Knowledge/ не трогаем ---
        kdir = _P(tmp) / "Knowledge"
        kdir.mkdir()
        globals()["KNOWLEDGE_DIR"] = kdir

        (kdir / "topic_a.md").write_text(
            "# Тема A\n\nКороткое описание темы A для индекса, обрезается до 100 символов.\n"
            "Ещё немного текста ниже.\n",
            encoding="utf-8",
        )
        # большой файл: паровозик абзацев, часть содержит маркер "утка"
        paragraphs = []
        for i in range(400):
            marker = "утка" if i % 7 == 0 else "гусь"
            paragraphs.append(f"Абзац {i} про {marker}. " + ("текст " * 20))
        big_text = "\n\n".join(paragraphs)
        assert len(big_text.encode("utf-8")) > 20 * 1024, "тестовый файл должен быть больше 20 KB"
        (kdir / "topic_big.md").write_text(big_text, encoding="utf-8")

        (kdir / "admin_commands.md").write_text(
            "---\nname: Admin Commands\ndescription: служебное\n---\n\n"
            "# Admin Commands Grammar\n\nПодкоманды admin_cmd, парсятся в Python.\n",
            encoding="utf-8",
        )

        # --- режим user по умолчанию (нет caller id) ---
        idx_user = index()
        assert "topic_a" in idx_user and "admin_commands" not in idx_user, (
            "admin_commands не должен светиться в index() вне admin-режима"
        )
        assert "Короткое описание темы A" in idx_user, "описание должно брать первую содержательную строку"
        print("OK: index() в user-режиме прячет admin_commands, описание — первая строка")

        sch_user = schema()
        assert "admin_commands" not in sch_user["properties"]["topic"]["enum"]
        assert "topic_a" in sch_user["properties"]["topic"]["enum"]
        print("OK: schema() enum в user-режиме без admin_commands")

        assert read("admin_commands") == "Тема «admin_commands» недоступна."
        print("OK: read(admin_commands) в user-режиме — отказ строкой")

        # --- переключаемся в admin через plugin.tools (тот же путь, что и в бою) ---
        _tools._CALLER_FALLBACK.set("999")
        _tools._set_mode("999", "admin")
        assert "admin_commands" in index(), "admin_commands обязан появиться в index() в admin-режиме"
        assert "admin_commands" in schema()["properties"]["topic"]["enum"]
        admin_text = read("admin_commands")
        assert "Admin Commands Grammar" in admin_text
        print("OK: admin-режим открывает admin_commands в index/schema/read")

        _tools._set_mode("999", "user")
        _tools._CALLER_FALLBACK.set(None)

        # --- малый файл целиком ---
        full = read("topic_a")
        assert "Тема A" in full and "Короткое описание" in full
        print("OK: файл до 20 KB отдаётся целиком")

        # --- большой файл без query — отказ с подсказкой ---
        refusal = read("topic_big")
        assert "query" in refusal.lower() and "20 kb" in refusal.lower()
        print("OK: файл больше 20 KB без query — отказ с подсказкой")

        # --- большой файл с query — абзацы с маркером, обрезка по 8 KB, остаток посчитан ---
        found = read("topic_big", query="утка")
        assert "утка" in found.lower() and "гусь" not in found.lower()
        assert len(found.encode("utf-8")) <= _SEARCH_LIMIT + 500, "выдача не должна сильно превышать 8 KB"
        assert "ещё" in found and "не показано" in found, "должен быть явный счётчик пропущенных совпадений"
        print("OK: большой файл с query — совпадения по абзацам, обрезка ~8 KB, остаток указан")

        # --- запрос без совпадений ---
        empty = read("topic_big", query="жираф")
        assert "ничего не найдено" in empty
        print("OK: query без совпадений — понятный ответ, не пусто и не исключение")

        # --- traversal: "../secret" не должен утечь файлом ---
        outside = _P(tmp) / "secret.md"
        outside.write_text("СЕКРЕТ", encoding="utf-8")
        leaked = read("../secret")
        assert "СЕКРЕТ" not in leaked, "traversal utёк за пределы KNOWLEDGE_DIR!"
        assert "не найдена" in leaked
        print("OK: '../secret' отбивается, файл вне каталога не читается")

        # --- неизвестная тема — строка, не исключение ---
        unknown = read("no_such_topic")
        assert "не найдена" in unknown
        print("OK: неизвестная тема — понятная строка, не exception")

    print("ALL OK: bot/knowledge.py")
