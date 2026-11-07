"""Инструмент knowledge(topic) поверх Knowledge/ — Docs/bot_design.md.

Каталог читается ПРИ КАЖДОМ вызове (index/schema/read), а не кэшируется при
импорте: файлы добавляются через веб-админку на живой системе, перезапуск
бота ради нового файла недопустим.
"""
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import math
import re
import unicodedata

from plugin import tools as _tools

# Переменная модуля, а не константа внутри функций — самотест подменяет её на
# временный каталог, боевой Knowledge/ не засоряется.
KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "Knowledge"

_EXTS = (".md", ".txt")
_ADMIN_ONLY = "admin_commands"
_FULL_LIMIT = 20 * 1024   # файл целиком, байт
_SEARCH_LIMIT = 8 * 1024  # выдача по query, байт максимум
_QUERY_LIMIT = 1024


def _normalize(text: str) -> str:
    """Одинаковый поиск для NFC/NFD, регистра и е/ё; без удалённых embeddings."""
    return unicodedata.normalize("NFC", text).casefold().replace("ё", "е")


def _is_admin() -> bool:
    return _tools._get_mode(_tools._caller_telegram_id() or "") == "admin"


def _personal_dir() -> Path | None:
    """Knowledge/personal/<telegram id>/ — личные документы звонящего (его
    протокол, вехи, заметки). Видны только ему; общий индекс их не содержит."""
    tg = _tools._caller_telegram_id()
    return KNOWLEDGE_DIR / "personal" / tg if tg and str(tg).isdigit() else None


def _dirs() -> list[Path]:
    personal = _personal_dir()
    dirs = []
    if (personal is not None and personal.is_dir() and not personal.is_symlink()
            and not personal.parent.is_symlink()
            and personal.resolve().parent == KNOWLEDGE_DIR.resolve() / "personal"):
        dirs.append(personal)
    if KNOWLEDGE_DIR.is_dir():
        dirs.append(KNOWLEDGE_DIR)
    return dirs


def _topics() -> list[Path]:
    """Файлы Knowledge/*.md и *.txt плюс личные файлы звонящего, по одному на
    тему (личный файл и .md приоритетнее при совпадении имени), отсортированы по
    имени темы для стабильного вывода."""
    by_stem: dict[str, Path] = {}
    for d in _dirs():
        for ext in _EXTS:
            for p in d.glob(f"*{ext}"):
                # В индекс и поиск попадают только файлы внутри разрешённого
                # каталога: симлинк не должен расширять область чтения.
                if p.is_file() and not p.is_symlink() and p.resolve().parent == d.resolve():
                    by_stem.setdefault(_normalize(p.stem), p)
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
    topic = {"type": "string", "description": "Тема из базы знаний Knowledge/; '*' — поиск query по всем доступным темам"}
    stems = [p.stem for p in _topics()]
    if stems:                       # пустой enum провайдеры отвергают
        topic["enum"] = [*stems, "*"]
    return {
        "type": "object",
        "properties": {
            "topic": topic,
            "query": {
                "type": "string",
                "description": "Слова или фраза для локального поиска; обязательны для topic='*' и файлов больше 20 KB. Результаты с источниками ограничены 8 KB",
            },
        },
        "required": ["topic"],
    }


def _resolve(topic: str) -> Path | None:
    """Тема от модели выбирает только файл из разрешённого каталога _topics().

    Разделители пути и traversal не являются темами; NFC/NFD и е/ё
    сопоставляются одинаково на разных файловых системах.
    """
    if not isinstance(topic, str) or not topic or any(c in topic for c in "/\\\0") or ".." in topic:
        return None
    # Файл обязан лежать ПРЯМО в своём каталоге, а не где-то внутри: иначе
    # topic "personal/<чужой id>/protocol" прочитал бы чужие личные документы.
    # macOS хранит часть имён в NFD, модель может вернуть тот же текст в NFC.
    for candidate in _topics():
        if _normalize(candidate.stem) == _normalize(topic):
            return candidate
    return None


def _clip_bytes(text: str, limit: int) -> str:
    return text.encode("utf-8")[:max(0, limit)].decode("utf-8", errors="ignore")


@dataclass(frozen=True)
class _SearchChunk:
    path: Path
    heading: str
    text: str
    normalized: str
    token_count: int
    matches: dict[str, int]


def _search_chunks(paths: list[Path], phrase: str, terms: set[str]) -> list[_SearchChunk]:
    chunks = []
    for path in paths:
        heading = ""
        text = path.read_text(encoding="utf-8", errors="replace")
        for paragraph in re.split(r"\n\s*\n", text):
            if not paragraph.strip():
                continue
            headings = re.findall(r"^#{1,6}\s+(.+)$", paragraph, re.MULTILINE)
            if headings:
                heading = headings[-1]
            paragraph = unicodedata.normalize("NFC", paragraph)
            # Перекрытие сохраняет контекст совпадения у границы фрагмента.
            for offset in range(0, len(paragraph), 2100):
                part = paragraph[offset:offset + 2400]
                normalized = _normalize(part)
                counts = Counter(re.findall(r"\w+", normalized))
                matches = {term: counts[term] for term in terms}
                if any(matches.values()) or phrase in normalized:
                    chunks.append(_SearchChunk(path, heading, part, normalized,
                                               counts.total(), matches))
    return chunks


def _rank_chunks(chunks: list[_SearchChunk], phrase: str, terms: set[str]) -> list[_SearchChunk]:
    """BM25 по кандидатам; сначала точная фраза и покрытие слов запроса."""
    size = len(chunks)
    average = sum(c.token_count for c in chunks) / size or 1
    frequency = {t: sum(bool(c.matches[t]) for c in chunks) for t in terms}

    def score(chunk: _SearchChunk):
        matched = sum(bool(n) for n in chunk.matches.values())
        rank = 0.0
        for term, count in chunk.matches.items():
            if count:
                idf = math.log(1 + (size - frequency[term] + .5) / (frequency[term] + .5))
                rank += idf * count * 2.2 / (count + 1.2 * (.25 + .75 * chunk.token_count / average))
        return (phrase in chunk.normalized, matched / len(terms), rank)

    return sorted(chunks, key=score, reverse=True)


def _excerpt(text: str, phrase: str, terms: set[str], limit: int) -> tuple[str, bool]:
    if len(text.encode("utf-8")) <= limit:
        return text, False
    # Позиции считаем в исходном тексте: casefold может менять длину Unicode.
    normalized_position = _normalize(text).find(phrase)
    if normalized_position >= 0:
        cursor = 0
        position = 0
        for position, char in enumerate(text):
            cursor += len(_normalize(char))
            if cursor > normalized_position:
                break
    else:
        position = next((m.start() for m in re.finditer(r"\w+", text)
                         if _normalize(m.group()) in terms), 0)
    start = max(0, position - 100)
    prefix = "… " if start else ""
    return prefix + _clip_bytes(text[start:], limit - len(prefix.encode("utf-8"))), True


def _render_search(chunks: list[_SearchChunk], phrase: str, terms: set[str]) -> str:
    out = []
    total = 0
    shown = 0
    note_template = "\n\n… ещё {count} совпадений не показано; длинные фрагменты сокращены, уточните query."
    budget = _SEARCH_LIMIT - len(note_template.format(count=len(chunks)).encode("utf-8"))
    shortened = False
    for chunk in chunks:
        source = chunk.path.relative_to(KNOWLEDGE_DIR).as_posix()
        header = f"[Источник: {source}; тема: {chunk.path.stem}]"
        if chunk.heading:
            header += "\nРаздел: " + _clip_bytes(chunk.heading, 300)
        separator = "\n\n" if out else ""
        available = budget - total - len((separator + header + "\n").encode("utf-8"))
        if available < 120:
            break
        excerpt, clipped = _excerpt(chunk.text, phrase, terms, min(3600, available))
        shortened |= clipped
        block = separator + header + "\n" + excerpt
        out.append(block)
        total += len(block.encode("utf-8"))
        shown += 1
    result = "".join(out)
    if shown < len(chunks) or shortened:
        result += note_template.format(count=len(chunks) - shown)
    return result


def _search(paths: list[Path], query: str) -> str:
    """Локальный лексический поиск; без стемминга и семантической модели.

    Документы перечитываются при вызове. Сбор фрагментов, ранжирование и
    ограничение выдачи разделены, чтобы изменения поиска не затронули ACL.
    """
    phrase = _normalize(query.strip())
    terms = set(re.findall(r"\w+", phrase))
    if not terms:
        return "Для поиска нужны слова или фраза в query."
    chunks = _search_chunks(paths, phrase, terms)
    if not chunks:
        return f"По запросу «{query}» ничего не найдено. Уточните слова или тему."
    return _render_search(_rank_chunks(chunks, phrase, terms), phrase, terms)


def read(topic: str, query: str | None = None) -> str:
    if query is not None and not isinstance(query, str):
        return "query должен быть строкой: слова или фраза для поиска."
    if query and len(query.encode("utf-8")) > _QUERY_LIMIT:
        return "Запрос query слишком длинный (максимум 1024 байта); сократите его до ключевых слов."
    if topic == _ADMIN_ONLY and not _is_admin():
        return f"Тема «{topic}» недоступна."
    if topic in ("*", "") and query:
        return _search(_topics(), query)
    if topic == "*":
        return "Для поиска по всем темам нужен query: слова или фраза."
    path = _resolve(topic)
    if path is None:
        return f"Тема «{topic}» не найдена. Список тем — в knowledge index."

    if query:
        return _search([path], query)
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text.encode("utf-8")) <= _FULL_LIMIT:
        return text
    return (f"Файл «{topic}» больше 20 KB — нужен query: слова или фраза, по которым "
            f"выбрать фрагменты.")


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

        # --- пунктуация в имени темы допустима, разделители пути и None - нет ---
        (kdir / "topic, with (punct).md").write_text("ПУНКТ", encoding="utf-8")
        assert read("topic, with (punct)") == "ПУНКТ"
        assert _resolve("a/b") is None and _resolve("..") is None and _resolve(None) is None
        print("OK: тема с запятой находится, '/', '..', None отбиваются")

        # --- пустой каталог -> schema() без enum ---
        _kd = KNOWLEDGE_DIR
        KNOWLEDGE_DIR = _P(tmp) / "empty_kb"
        KNOWLEDGE_DIR.mkdir()
        assert "enum" not in schema()["properties"]["topic"]
        KNOWLEDGE_DIR = _kd
        print("OK: schema() без тем не отдаёт пустой enum")

        # --- неизвестная тема — строка, не исключение ---
        unknown = read("no_such_topic")
        assert "не найдена" in unknown
        print("OK: неизвестная тема — понятная строка, не exception")

    print("ALL OK: bot/knowledge.py")
