"""Черновики карт препаратов (CONTEXT.md «Черновик карты», docs/adr/0002).

Когда у человека нашёлся препарат без карты, модель тянет официальные тексты
(openFDA, а если там пусто — ClinicalTrials.gov), составляет из них поля и
кладёт черновик в card_drafts — БЕЗ эффекта на pharma/card, пока админ не
одобрит его в панели (/drafts). Никакого веб-поиска и памяти модели: цифры
идут только из текста, который вернул fetch_sources().

Три сетевых поля не смешиваются с диском: fetch_sources — чистый HTTP-клиент,
save_draft/approve/reject — чистая работа с БД и Knowledge/drug_cards.md.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from health_core import config
from health_core import meds as _meds

_TIMEOUT_S = 20
_USER_AGENT = "health-agent-system/1.0 (drug-card-drafts; +https://github.com)"
_MAX_FIELD_CHARS = 4000  # инструкция FDA легко тянет на десятки тысяч символов — обрезаем на разумном

_FDA_URL = "https://api.fda.gov/drug/label.json"
_CTGOV_URL = "https://clinicaltrials.gov/api/v2/studies"


def _now_iso() -> str:
    return config.local_now().strftime("%Y-%m-%d %H:%M:%S")


def _http_get_json(url: str) -> dict:
    """Единственная точка сетевого доступа модуля — тесты подменяют её целиком
    (health_core.card_drafts._http_get_json = ...), а не urllib изнутри."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _truncate(text: str | None, limit: int = _MAX_FIELD_CHARS) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _first(value):
    """openFDA поля — почти все списки из одной строки; берём первую."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def fetch_sources(inn: str) -> dict:
    """Официальные тексты по МНН (латиницей): сперва инструкция FDA
    (dosage_and_administration, pharmacokinetics), а если препарата там нет —
    протоколы ClinicalTrials.gov (описания групп с дозами). Никакого веб-поиска.
    """
    inn = (inn or "").strip()
    if not inn:
        return {"error": "Нужно МНН латиницей (inn)"}

    query = urllib.parse.quote(f'openfda.generic_name:"{inn}"')
    fda_url = f"{_FDA_URL}?search={query}&limit=1"
    fda_error = None
    try:
        data = _http_get_json(fda_url)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        data = None
        fda_error = str(e)

    results = (data or {}).get("results") if isinstance(data, dict) else None
    if results:
        r = results[0] or {}
        dosage = _truncate(_first(r.get("dosage_and_administration")))
        pk = _truncate(_first(r.get("pharmacokinetics")) or _first(r.get("clinical_pharmacology")))
        eff = _first(r.get("effective_time"))
        return {
            "inn": inn,
            "source": "openfda",
            "dosage_and_administration": dosage,
            "pharmacokinetics": pk,
            "effective_time": eff,
            "url": fda_url,
        }

    ct_query = urllib.parse.urlencode({
        "query.intr": inn,
        "pageSize": 5,
        "fields": "NCTId,BriefTitle,Phase,ArmGroupDescription",
    })
    ct_url = f"{_CTGOV_URL}?{ct_query}"
    try:
        ct_data = _http_get_json(ct_url)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        return {"inn": inn, "source": None,
                "error": f"openFDA пуст ({fda_error or 'нет результатов'}), ClinicalTrials.gov недоступен: {e}"}

    studies = []
    for s in (ct_data or {}).get("studies", []):
        proto = s.get("protocolSection", {}) if isinstance(s, dict) else {}
        ident = proto.get("identificationModule", {})
        design = proto.get("designModule", {})
        arms = proto.get("armsInterventionsModule", {})
        nct_id = ident.get("nctId")
        descs = [a.get("description") for a in (arms.get("armGroups") or []) if a.get("description")]
        studies.append({
            "nct_id": nct_id,
            "title": ident.get("briefTitle"),
            "phase": design.get("phases"),
            "arm_group_description": _truncate("; ".join(descs)) if descs else None,
            "url": f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else None,
        })

    if not studies:
        return {"inn": inn, "source": None,
                "error": "Источники не найдены ни в openFDA, ни в ClinicalTrials.gov"}
    return {"inn": inn, "source": "clinicaltrials", "studies": studies}


def save_draft(conn, user_id: int, substance: str, fields: dict, sources: list[str]) -> int:
    """Черновик карты. Один pending на канонический препарат — повторный
    запрос (человек с тем же безкартным препаратом пишет снова) отдаёт id
    уже созданного черновика, а не плодит дубликаты, которые админу разгребать."""
    canonical = _meds.canon(substance)
    # SQLite's built-in NOCASE only folds ASCII — Cyrillic "Ретатрутид" vs
    # "ретатрутид" would compare unequal, so the dedup check runs in Python
    # with str.casefold(), same primitive meds._key() uses for alias lookup.
    for p in conn.execute("SELECT id, substance FROM card_drafts WHERE status='pending'"):
        if p["substance"].casefold() == canonical.casefold():
            return p["id"]
    cur = conn.execute(
        "INSERT INTO card_drafts(requested_by_user_id, substance, fields_json, sources_json, status, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (user_id, canonical, json.dumps(fields or {}, ensure_ascii=False),
         json.dumps(sources or [], ensure_ascii=False), _now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def get_draft(conn, draft_id: int):
    return conn.execute("SELECT * FROM card_drafts WHERE id=?", (draft_id,)).fetchone()


def pending_drafts(conn) -> list:
    """Для админ-панели (/drafts): все черновики, ждущие решения, старые первыми."""
    return conn.execute(
        "SELECT * FROM card_drafts WHERE status='pending' ORDER BY created_at"
    ).fetchall()


_CARD_FIELD_LINES = (
    ("status", "Статус"),
    ("ladder", "Лестница"),
    ("min_weeks", "Минимум недель на ступени"),
    ("interval_days", "Интервал приёма"),
    ("half_life_days", "Период полувыведения"),
    ("tmax_h", "Пик концентрации"),
    ("source", "Источник"),
)


_LADDER_RE = re.compile(r"\d+(?:\.\d+)?(?:\s*,\s*\d+(?:\.\d+)?)*(?:\s*(?:мг|mg))?", re.I)
_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)(?:\s*[a-zа-яё.]+)?", re.I)


def _one_line(value) -> str:
    return " ".join(str(value or "").split())


def _checked_fields(substance: str, synonyms: str, fields: dict) -> tuple[str, str, dict]:
    """Граница доверия: поля пишет модель и правит админ в веб-форме, а файл карт
    читает парсер построчно. Перевод строки в любом поле мог бы дописать чужой
    заголовок карты или второе поле, поэтому всё сводится в одну строку и
    проверяется по строгим шаблонам до записи."""
    substance, synonyms = _one_line(substance), _one_line(synonyms)
    clean = {key: _one_line(fields.get(key)) for key, _ in _CARD_FIELD_LINES}
    problems = []
    if not substance or substance.startswith("#"):
        problems.append("название препарата пустое или начинается с #")
    if clean["status"] and clean["status"] != "зарегистрирован" and not clean["status"].startswith("не зарегистрирован"):
        problems.append("статус — «зарегистрирован» или «не зарегистрирован (…)»")
    if clean["ladder"]:
        steps = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", clean["ladder"])]
        if not _LADDER_RE.fullmatch(clean["ladder"]) or any(s <= 0 for s in steps) or steps != sorted(set(steps)):
            problems.append("лестница — положительные дозы через запятую по возрастанию, например «2.5, 5, 7.5 мг»")
    for key, integer in (("min_weeks", True), ("interval_days", True), ("half_life_days", False), ("tmax_h", False)):
        m = _NUMBER_RE.fullmatch(clean[key]) if clean[key] else None
        if clean[key] and (m is None or float(m.group(1)) <= 0 or (integer and "." in m.group(1))):
            problems.append(f"{key} — {'целое ' if integer else ''}положительное число")
    if problems:
        raise ValueError("Черновик не прошёл проверку: " + "; ".join(problems))
    return substance, synonyms, clean


def _card_block(card_number: int, substance: str, synonyms: str, fields: dict) -> str:
    header = f"{substance} / {synonyms}".strip(" /") if synonyms else substance
    lines = [f"## 📇 Карта {card_number}: {header}"]
    for key, label in _CARD_FIELD_LINES:
        val = fields.get(key)
        val = str(val).strip() if val is not None else ""
        if val:
            lines.append(f"- **{label}:** {val}")
    return "\n".join(lines) + "\n"


def approve(conn, draft_id: int, fields: dict) -> dict:
    """Дописывает карту в Knowledge/drug_cards.md (в общем формате остальных
    карт — заголовок «## 📇 Карта N: ...» плюс поля-строки, которые читает
    health_core/meds.py::card()) и переводит черновик в approved.

    fields — версия админа из формы /drafts (он мог поправить цифры до
    одобрения): status/ladder/min_weeks/interval_days/half_life_days/tmax_h/
    synonyms, плюс необязательный source (иначе берём ссылки из черновика).
    Путь читаем через health_core.meds.DRUG_CARDS (не копируем константу),
    чтобы тест мог подменить его на временный файл, не трогая рабочий."""
    row = get_draft(conn, draft_id)
    if row is None:
        raise ValueError(f"Черновик #{draft_id} не найден")
    if row["status"] != "pending":
        raise ValueError(f"Черновик #{draft_id} уже {row['status']}")

    substance = row["substance"]
    fields = dict(fields or {})
    if not str(fields.get("source") or "").strip():
        sources = json.loads(row["sources_json"] or "[]")
        if sources:
            fields["source"] = "; ".join(sources)
    synonyms = fields.pop("synonyms", "") or ""
    substance, synonyms, fields = _checked_fields(substance, synonyms, fields)

    path = _meds.DRUG_CARDS
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    card_number = len(_meds._CARD_RE.findall(text)) + 1
    block = _card_block(card_number, substance, synonyms, fields)

    sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    with path.open("a", encoding="utf-8") as f:
        f.write(sep + block)

    conn.execute(
        "UPDATE card_drafts SET status='approved', decided_at=? WHERE id=?",
        (_now_iso(), draft_id),
    )
    conn.commit()

    # Оба кэша meds.py — lru_cache(maxsize=1), читают DRUG_CARDS только на первый
    # вызов после старта/сброса. Без сброса свежеодобренная карта не появится
    # ни в aliases(), ни в card() до перезапуска бота.
    _meds.aliases.cache_clear()
    _meds._cards.cache_clear()

    return {"card_number": card_number, "substance": substance}


def reject(conn, draft_id: int) -> None:
    conn.execute(
        "UPDATE card_drafts SET status='rejected', decided_at=? WHERE id=? AND status='pending'",
        (_now_iso(), draft_id),
    )
    conn.commit()


if __name__ == "__main__":
    import os
    import tempfile
    from pathlib import Path

    os.environ["HEALTH_DB"] = str(Path(tempfile.mkdtemp()) / "health.db")
    from health_core.db import connect, migrate

    conn = connect()
    migrate(conn)
    conn.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (1, '2026-08-20 00:00:00')")
    uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]

    # ---- fetch_sources: openFDA хиты ----
    import health_core.card_drafts as cd

    def fake_fda(url):
        assert "generic_name" in url and "retatrutide" in url
        return {"results": [{
            "dosage_and_administration": ["Initiate at 2 mg subcutaneously once weekly. May increase to 4, 6, 9, "
                                           "then 12 mg at 4-week intervals as tolerated."],
            "pharmacokinetics": ["Terminal half-life is approximately 6 days. Peak plasma concentration (Tmax) is "
                                  "reached at approximately 48 hours post-dose."],
            "effective_time": "20260101",
        }]}

    cd._http_get_json = fake_fda
    out = cd.fetch_sources("retatrutide")
    assert out["source"] == "openfda", out
    assert "2 mg" in out["dosage_and_administration"]
    assert "6 days" in out["pharmacokinetics"]
    assert out["url"].endswith('search=openfda.generic_name%3A%22retatrutide%22&limit=1')
    print("OK: fetch_sources — openFDA хит разобран")

    # ---- fetch_sources: openFDA пуст -> ClinicalTrials.gov ----
    def fake_fda_empty(url):
        return {"results": []}

    def fake_ct(url):
        assert "query.intr=retatrutide" in url
        return {"studies": [{
            "protocolSection": {
                "identificationModule": {"nctId": "NCT05929066", "briefTitle": "TRIUMPH-1"},
                "designModule": {"phases": ["PHASE3"]},
                "armsInterventionsModule": {"armGroups": [
                    {"label": "Retatrutide 12 mg", "description": "Retatrutide 12 mg subcutaneously once weekly."},
                ]},
            }
        }]}

    calls = iter([fake_fda_empty, fake_ct])
    cd._http_get_json = lambda url: next(calls)(url)
    out2 = cd.fetch_sources("retatrutide")
    assert out2["source"] == "clinicaltrials", out2
    assert out2["studies"][0]["nct_id"] == "NCT05929066"
    assert out2["studies"][0]["url"] == "https://clinicaltrials.gov/study/NCT05929066"
    assert "12 mg" in out2["studies"][0]["arm_group_description"]
    print("OK: fetch_sources — пустой openFDA падает на ClinicalTrials.gov")

    # ---- fetch_sources: оба источника пусты -> error, без веб-поиска ----
    cd._http_get_json = lambda url: {"results": []} if "fda" in url else {"studies": []}
    out3 = cd.fetch_sources("nonexistentdrugxyz")
    assert "error" in out3 and out3["source"] is None
    print("OK: fetch_sources — оба источника пусты -> error, не выдумка")

    assert cd.fetch_sources("")["error"]

    # ---- изолируем meds.py от рабочего Knowledge/drug_cards.md на всё
    # оставшееся: save_draft() зовёт meds.canon(), а он читает DRUG_CARDS,
    # где Ретатрутид уже есть картой 3 — временный файл с одной карты не
    # даёт save_draft/approve зацепиться за прод и не трогает его на диске.
    tmp_cards = Path(tempfile.mkdtemp()) / "drug_cards.md"
    tmp_cards.write_text(
        "# ФАРМАКОЛОГИЧЕСКИЕ КАРТЫ\n\n"
        "## 📇 Карта 1: Тирзепатид (Tirzepatide)\n"
        "- **Статус:** зарегистрирован\n"
        "- **Лестница:** 2.5, 5, 7.5, 10, 12.5, 15 мг\n",
        encoding="utf-8",
    )
    import health_core.meds as meds_mod
    _orig_drug_cards = meds_mod.DRUG_CARDS
    meds_mod.DRUG_CARDS = tmp_cards
    meds_mod.aliases.cache_clear()
    meds_mod._cards.cache_clear()
    try:
        # ---- save_draft: один pending на канонический препарат ----
        fields = {"status": "не зарегистрирован (данные исследований)", "ladder": "2, 4, 6, 9, 12 мг",
                  "min_weeks": "4", "interval_days": "7", "half_life_days": "6 сут", "tmax_h": "48 ч",
                  "synonyms": "LY3437943"}
        sources = ["https://clinicaltrials.gov/study/NCT05929066"]
        d1 = cd.save_draft(conn, uid, "Ретатрутид (Retatrutide)", fields, sources)
        d2 = cd.save_draft(conn, uid, "ретатрутид (retatrutide)", fields, sources)  # регистр/пробелы не важны
        assert d1 == d2, "повторный запрос по тому же препарату должен вернуть тот же черновик"
        row = cd.get_draft(conn, d1)
        assert row["status"] == "pending"
        assert len(cd.pending_drafts(conn)) == 1
        print(f"OK: save_draft — один pending-черновик (#{d1}) на повторные запросы")

        # ---- approve: дописывает временную копию drug_cards.md, meds.card() видит ----
        result = cd.approve(conn, d1, dict(fields))
        assert result == {"card_number": 2, "substance": "Ретатрутид (Retatrutide)"}, result

        new_text = tmp_cards.read_text(encoding="utf-8")
        assert "## 📇 Карта 2: Ретатрутид (Retatrutide) / LY3437943" in new_text, new_text
        assert "- **Лестница:** 2, 4, 6, 9, 12 мг" in new_text
        assert "- **Источник:** https://clinicaltrials.gov/study/NCT05929066" in new_text

        card = meds_mod.card("LY3437943")  # синоним из заголовка сводится к каноническому имени
        assert card is not None, "meds.card() должен увидеть свежеодобренную карту без перезапуска"
        assert card["ladder"] == [2.0, 4.0, 6.0, 9.0, 12.0], card
        assert card["half_life_days"] == 6.0 and card["tmax_h"] == 48.0
        assert card["unregistered"] is True
        print("OK: approve — карта дописана во временный файл, meds.card() видит её сразу (кэш сброшен)")

        row2 = cd.get_draft(conn, d1)
        assert row2["status"] == "approved" and row2["decided_at"]
        assert cd.pending_drafts(conn) == []

        # Повторное approve уже одобренного — отказ, не повторная дописка.
        try:
            cd.approve(conn, d1, dict(fields))
            raise AssertionError("approve дважды не должен проходить молча")
        except ValueError:
            pass
        print("OK: approve — повторное одобрение того же черновика отклонено")

        # ---- approve: граница доверия — перевод строки и мусор не пишутся в файл карт ----
        bad = cd.save_draft(conn, uid, "Кагрилинтид", {}, [])
        before = tmp_cards.read_text(encoding="utf-8")
        for bad_fields in (
            {"ladder": "0.25,\n## 📇 Карта 9: Подделка\n- **Лестница:** 99"},
            {"ladder": "5, 2.5 мг"},
            {"status": "одобрен"},
            {"min_weeks": "2.5"},
            {"half_life_days": "долго"},
        ):
            try:
                cd.approve(conn, bad, bad_fields)
                raise AssertionError(f"approve пропустил некорректные поля: {bad_fields}")
            except ValueError:
                pass
        assert tmp_cards.read_text(encoding="utf-8") == before, "отклонённый approve не должен трогать файл карт"
        assert cd.get_draft(conn, bad)["status"] == "pending"
        cd.reject(conn, bad)
        print("OK: approve — внедрение строк и некорректные поля отклоняются до записи")
    finally:
        meds_mod.DRUG_CARDS = _orig_drug_cards
        meds_mod.aliases.cache_clear()
        meds_mod._cards.cache_clear()

    # ---- reject ----
    d3 = cd.save_draft(conn, uid, "Оксинтомодулин", {}, [])
    cd.reject(conn, d3)
    assert cd.get_draft(conn, d3)["status"] == "rejected"
    assert cd.pending_drafts(conn) == []
    print("OK: reject — черновик отклонён, карта не пишется")

    conn.close()
    print("card_drafts: ok")
