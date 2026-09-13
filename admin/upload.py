"""Разбор multipart/form-data и защита загрузки файлов знаний (Knowledge/) —
отдельный модуль, чтобы обе части протестировать без поднятия HTTP-сервера
(см. блок __main__ ниже).

Модуля cgi в python 3.13+ больше нет, поэтому multipart разбирается вручную
через email.parser: тело оборачивается в MIME-заголовки с тем же boundary,
что пришёл в Content-Type, и email.message_from_bytes сам режет его на part'ы.
Это единственный вменяемый способ сделать это в stdlib после выпиливания cgi.

Загрузка файла — граница доверия, поэтому здесь два независимых слоя защиты
пути: sanitize_filename() выбрасывает всё, что не [A-Za-z0-9_-], а
safe_knowledge_path() отдельно проверяет resolve()-ом, что итоговый путь не
выбрался за пределы Knowledge/ — так что даже ослабленный в будущем
санитайзер не откроет path traversal.
"""
import email.policy
import io
import re
from email import message_from_bytes
from pathlib import Path

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB — потолок из ТЗ

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_ALLOWED_SUFFIXES = (".md", ".txt", ".pdf")


def parse_multipart(content_type: str, body: bytes) -> dict:
    """Разбирает тело multipart/form-data. Возвращает {field_name: value}, где
    value — str для обычных полей и (filename, bytes) для файловых полей.
    Пустой словарь, если в Content-Type нет boundary или тело не multipart."""
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        return {}
    boundary = m.group(1)
    # email.message_from_bytes ждёт целый MIME-документ с заголовками —
    # подставляем ему свой Content-Type как заголовок письма перед телом.
    raw = b"Content-Type: multipart/form-data; boundary=" + boundary.encode("ascii", "ignore") + b"\r\n\r\n" + body
    # policy=HTTP, а НЕ политика по умолчанию (compat32). На не-ASCII в
    # Content-Disposition — кириллица в имени файла, а выгрузка весов только
    # так и называется, «Состав тела-...xls» — compat32 отдаёт из get()
    # объект email.header.Header вместо строки, и разбор падал TypeError'ом
    # ещё до единой проверки: пятисотка вместо загрузки. Восстановить байты
    # из такого Header уже нельзя (не-ASCII в нём заменён на «?»), поэтому
    # лечится только выбором политики на входе. HTTP — политика ровно для
    # HTTP-заголовков: разбирает параметры сама и отдаёт имя декодированным.
    msg = message_from_bytes(raw, policy=email.policy.HTTP)
    if not msg.is_multipart():
        return {}
    fields: dict = {}
    for part in msg.iter_parts():
        field_name = part.get_param("name", header="content-disposition")
        if not field_name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            fields[field_name] = (filename, payload)
        else:
            fields[field_name] = payload.decode("utf-8", errors="replace")
    return fields


def sanitize_filename(raw_filename: str, is_pdf: bool) -> str | None:
    """Санирует имя загруженного файла: только [A-Za-z0-9_-] в основе имени,
    расширение принудительно .md (для .pdf — тоже .md, туда пишется извлечённый
    текст) или .txt. Возвращает None, если после очистки имя пустое или
    расширение исходного файла не .md/.txt/.pdf.

    Path(...).name/.stem/.suffix берут только последний компонент пути, так что
    "../../.env" схлопывается до имени ".env" ещё до regex-очистки — traversal
    через сам regex не проходит; safe_knowledge_path() ниже — вторая, не
    полагающаяся на это поведение проверка."""
    p = Path(raw_filename.replace("\\", "/"))  # на случай windows-путей от чужого клиента
    suffix = p.suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        return None
    target_ext = ".md" if is_pdf else suffix
    if is_pdf and suffix != ".pdf":
        return None
    stem = _SAFE_CHARS.sub("", p.stem)
    if not stem:
        return None
    return stem + target_ext


def safe_knowledge_path(knowledge_dir: Path, filename: str) -> Path | None:
    """Строит путь knowledge_dir/filename и проверяет resolve()-ом, что он
    остаётся внутри knowledge_dir. Независима от sanitize_filename — вызывается
    и на загрузке, и на удалении, где имя приходит из формы, а не с диска."""
    base = knowledge_dir.resolve()
    candidate = (base / filename).resolve()
    if candidate != base and base not in candidate.parents:
        return None
    return candidate


def extract_pdf_text(data: bytes) -> str:
    """pypdf — опциональная зависимость: импорт внутри функции, чтобы
    отсутствие пакета не роняло всю панель, а давало внятный отказ только на
    загрузке PDF. Ошибки самого разбора (битый файл) тоже гасятся здесь —
    единственный способ прочитать PDF в этой панели, поэтому любой сбой должен
    вернуться текстом, а не необработанным исключением наружу."""
    try:
        import pypdf
    except ImportError:
        raise RuntimeError("Для загрузки PDF поставьте пакет: pip install pypdf")
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        parts = [page.extract_text() or "" for page in reader.pages]
    except Exception as e:
        raise RuntimeError(f"Не удалось прочитать PDF: {e}")
    text = "\n\n".join(parts).strip()
    if not text:
        raise RuntimeError("Не удалось извлечь текст из PDF (пустой результат).")
    return text


def save_knowledge_file(knowledge_dir: Path, raw_filename: str, data: bytes, overwrite: bool) -> tuple[bool, str]:
    """Полный цикл сохранения одного файла знаний. Возвращает
    (успех, сообщение_для_пользователя) — сообщение отдаётся и при отказе,
    чтобы страница показала внятную причину."""
    if len(data) > MAX_UPLOAD_BYTES:
        return False, f"Файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ — отклонён."

    suffix = Path(raw_filename.replace("\\", "/")).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        return False, f"Расширение «{suffix or '(нет)'}» не поддерживается — только .md, .txt, .pdf."
    is_pdf = suffix == ".pdf"

    final_name = sanitize_filename(raw_filename, is_pdf=is_pdf)
    if not final_name:
        return False, "Имя файла пустое после очистки — переименуйте файл (латиница/цифры/_/-)."

    target = safe_knowledge_path(knowledge_dir, final_name)
    if target is None:
        return False, "Некорректное имя файла (попытка выйти за пределы каталога Knowledge/)."

    if is_pdf:
        try:
            text = extract_pdf_text(data)
        except RuntimeError as e:
            return False, str(e)
        payload = text.encode("utf-8")
    else:
        payload = data

    if target.exists() and not overwrite:
        return False, f"Тема «{target.stem}» уже существует. Отметьте «перезаписать», чтобы заменить."

    knowledge_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return True, f"Сохранено: {target.name} ({len(payload)} байт)."


if __name__ == "__main__":
    import sys
    import tempfile

    sys.stdout.reconfigure(encoding="utf-8")

    # --- 1. корректный multipart разбирается: обычное поле + файл (имя + содержимое) ---
    boundary = "----boundary123"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="overwrite"\r\n\r\n'
        f"on\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="notes.md"\r\n'
        f"Content-Type: text/markdown\r\n\r\n"
        f"# Заголовок\r\nТекст темы.\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    fields = parse_multipart(f"multipart/form-data; boundary={boundary}", body)
    assert fields["overwrite"] == "on"
    fname, data = fields["file"]
    assert fname == "notes.md"
    # Финальный \r\n перед boundary — это framing multipart, а не часть содержимого,
    # поэтому его в разобранных данных быть не должно (RFC 2046).
    assert data == "# Заголовок\r\nТекст темы.".encode("utf-8")
    print("OK: multipart разбирается — обычное поле и файл (имя + содержимое) распознаны")

    # --- 1b. кириллица в имени файла: реальная выгрузка весов зовётся именно так ---
    # Раньше здесь была пятисотка: compat32 отдавал Header вместо строки и
    # разбор падал TypeError'ом. Латинское имя баг не ловит — нужен не-ASCII.
    cyr_name = "Состав тела-isbuteev-Feelfit-20260824.xlsx.xls"
    cyr_body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="csrf_token"\r\n\r\n'
        f"tok\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{cyr_name}"\r\n'
        f"Content-Type: application/vnd.ms-excel\r\n\r\n"
        f"BINARYDATA\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    cyr_fields = parse_multipart(f"multipart/form-data; boundary={boundary}", cyr_body)
    assert cyr_fields["csrf_token"] == "tok"
    got_name, got_data = cyr_fields["file"]
    assert got_name == cyr_name, f"имя потеряно при разборе: {got_name!r}"
    assert got_data == b"BINARYDATA", got_data
    print(f"OK: кириллическое имя файла разбирается без падения и не искажается: {got_name!r}")

    # --- 2. "../../.env" отбивается (нет допустимого расширения после схлопывания пути) ---
    assert sanitize_filename("../../.env", is_pdf=False) is None
    print("OK: '../../.env' отбивается санитайзером")

    # --- 3. ".exe" отбивается ---
    assert sanitize_filename("virus.exe", is_pdf=False) is None
    print("OK: '.exe' отбивается санитайзером")

    with tempfile.TemporaryDirectory() as tmp:
        kdir = Path(tmp) / "Knowledge"
        kdir.mkdir()

        # --- 4. safe_knowledge_path держит путь внутри каталога, включая ручной обход санитайзера ---
        assert safe_knowledge_path(kdir, "topic.md") == (kdir / "topic.md").resolve()
        assert safe_knowledge_path(kdir, "../evil.md") is None
        print("OK: safe_knowledge_path не выпускает путь за пределы Knowledge/")

        # --- 5. файл больше потолка отбивается, на диск не пишется ---
        big = b"x" * (MAX_UPLOAD_BYTES + 1)
        ok, msg = save_knowledge_file(kdir, "big.md", big, overwrite=False)
        assert not ok and "МБ" in msg
        assert not (kdir / "big.md").exists()
        print("OK: файл больше потолка отклонён и не записан на диск")

        # --- 6. существующая тема без чекбокса не перезаписывается ---
        ok, msg = save_knowledge_file(kdir, "topic.md", b"v1", overwrite=False)
        assert ok, msg
        before = (kdir / "topic.md").read_bytes()
        ok, msg = save_knowledge_file(kdir, "topic.md", b"v2-should-not-land", overwrite=False)
        assert not ok
        assert (kdir / "topic.md").read_bytes() == before == b"v1"
        print("OK: существующая тема без явного overwrite не перезаписана")

        # --- 7. overwrite=True перезаписывает ---
        ok, msg = save_knowledge_file(kdir, "topic.md", b"v2", overwrite=True)
        assert ok, msg
        assert (kdir / "topic.md").read_bytes() == b"v2"
        print("OK: overwrite=True перезаписывает существующую тему")

        # --- 8. .txt принимается как есть; битый/отсутствующий .pdf отклоняется с сообщением, не падением ---
        ok, msg = save_knowledge_file(kdir, "plain.txt", b"hello", overwrite=False)
        assert ok and (kdir / "plain.txt").read_bytes() == b"hello"
        ok, msg = save_knowledge_file(kdir, "doc.pdf", b"%PDF-fake-not-real", overwrite=False)
        assert not ok and msg
        print(f"OK: .txt сохраняется как есть; невалидный .pdf отклонён сообщением, не исключением: {msg!r}")

    print("OK: admin.upload self-check passed")
