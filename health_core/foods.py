"""Состав продукта: поиск по названию в Open Food Facts + личный справочник
«Мои продукты» (CONTEXT.md «Состав продукта», «Мой продукт»).

Сеть — только в search()/fetch_by_code(); match/remember/list/forget — чистая
работа с БД. Единственная точка HTTP — _http_get_json (тот же приём, что
health_core/card_drafts.py._http_get_json), тесты подменяют её целиком, а не
urllib изнутри.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from health_core import config

_TIMEOUT_S = 15
_USER_AGENT = "health-agent-system/1.0"
_MAX_CANDIDATES = 5

_OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
_OFF_PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product"
_OFF_FIELDS = "code,product_name,brands,nutriments"


def _now_iso() -> str:
    return config.local_now().strftime("%Y-%m-%d %H:%M:%S")


def _http_get_json(url: str) -> dict:
    """Единственная точка сетевого доступа модуля — тесты подменяют её целиком
    (health_core.foods._http_get_json = ...)."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _candidate(p: dict, fallback_name: str) -> dict | None:
    """Один продукт OFF -> кандидат. Без калорий — не кандидат: считать нечего,
    а хранить продукт без базовой цифры хуже, чем не найти его вовсе."""
    nutr = p.get("nutriments") or {}
    kcal = nutr.get("energy-kcal_100g")
    if kcal is None:
        return None
    return {
        "off_code": p.get("code"),
        "name": p.get("product_name") or fallback_name,
        "brand": p.get("brands"),
        "kcal_100g": kcal,
        "protein_100g": nutr.get("proteins_100g"),
        "fat_100g": nutr.get("fat_100g"),
        "carbs_100g": nutr.get("carbohydrates_100g"),
        "fiber_100g": nutr.get("fiber_100g"),
    }


def search(name: str) -> dict:
    """До 5 кандидатов из Open Food Facts по названию. Ошибка сети -> {"error":
    ...}, не исключение: вызывающий (food_lookup) должен ответить человеку
    текстом, а не упасть."""
    name = (name or "").strip()
    if not name:
        return {"error": "Нужно название продукта"}
    qs = urllib.parse.urlencode({
        "search_terms": name,
        "search_simple": 1,
        "action": "process",
        "json": 1,
        "page_size": _MAX_CANDIDATES,
        "fields": _OFF_FIELDS,
    })
    url = f"{_OFF_SEARCH_URL}?{qs}"
    try:
        data = _http_get_json(url)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        return {"error": f"Open Food Facts недоступен: {e}"}

    candidates = []
    for p in (data or {}).get("products", []):
        c = _candidate(p, name)
        if c is not None:
            candidates.append(c)
        if len(candidates) >= _MAX_CANDIDATES:
            break
    return {"candidates": candidates}


def fetch_by_code(off_code: str) -> dict:
    """Состав одного продукта по штрихкоду OFF — для remember(off_code=...) без
    явных чисел, когда модель уже выбрала кандидата из search() по коду, но
    числа передать не удосужилась."""
    off_code = str(off_code or "").strip()
    if not off_code:
        return {"error": "Нужен off_code"}
    qs = urllib.parse.urlencode({"fields": _OFF_FIELDS})
    url = f"{_OFF_PRODUCT_URL}/{urllib.parse.quote(off_code)}.json?{qs}"
    try:
        data = _http_get_json(url)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        return {"error": f"Open Food Facts недоступен: {e}"}
    p = (data or {}).get("product") or {}
    c = _candidate(p, off_code)
    if c is None:
        return {"error": f"Продукт {off_code} без калорий или не найден"}
    return c


# ── Ключ названия: сопоставление «того же продукта в другой формулировке» ──
#
# casefold -> скобки с содержимым долой -> из остатка берём только слова из
# букв (числа и единицы вроде "г"/"мл"/"шт"/"кг" сами не попадают в этот
# список ИЛИ короче 3 букв — либо то, либо другое) -> служебные слова долой ->
# каждое слово обрезаем до первых 5 букв (грубая основа) -> отсортированное
# множество основ.
_PAREN_RE = re.compile(r"\([^)]*\)")
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_STOPWORDS = {"для", "без", "при", "над", "под", "или"}
_STEM_LEN = 5
_MIN_WORD_LEN = 3


def name_stems(name: str) -> set[str]:
    s = _PAREN_RE.sub(" ", (name or "").casefold())
    stems = set()
    for w in _WORD_RE.findall(s):
        if len(w) < _MIN_WORD_LEN or w in _STOPWORDS:
            continue
        stems.add(w[:_STEM_LEN])
    return stems


def name_key(name: str) -> str:
    """Строковый ключ для my_products.name_key / UNIQUE(user_id, name_key):
    отсортированные основы через пробел."""
    return " ".join(sorted(name_stems(name)))


def keys_match(saved_key: str, query_name: str) -> bool:
    """Сохранённый продукт совпадает с запросом, если ВСЕ основы сохранённого
    ключа есть среди основ запроса (запрос волен быть длиннее и подробнее)."""
    saved = set((saved_key or "").split())
    if not saved:
        return False
    return saved <= name_stems(query_name)


# ── my_products: личный справочник (CONTEXT.md «Мой продукт») ──

def find_mine(conn, user_id: int, name: str):
    """Свой сохранённый продукт, чей ключ — подмножество основ запроса.
    Подходит несколько — берём самый специфичный (больше совпавших основ),
    при равенстве — заведённый раньше."""
    q_stems = name_stems(name)
    if not q_stems:
        return None
    best, best_n = None, -1
    for r in conn.execute(
        "SELECT * FROM my_products WHERE user_id=? ORDER BY id", (user_id,)
    ).fetchall():
        saved = set((r["name_key"] or "").split())
        if saved and saved <= q_stems and len(saved) > best_n:
            best, best_n = r, len(saved)
    return best


def remember(conn, user_id: int, name: str, *, source: str, off_code=None,
             kcal_100g=None, protein_100g=None, fat_100g=None, carbs_100g=None,
             fiber_100g=None) -> int:
    if source not in ("off", "label", "estimate"):
        raise ValueError(f"source должен быть off/label/estimate, получено {source!r}")
    if kcal_100g is None:
        raise ValueError("Нужен kcal_100g (цифры на 100 г)")
    display_name = (name or "").strip()
    key = name_key(display_name)
    if not display_name or not key:
        raise ValueError("Название продукта пустое")
    conn.execute(
        "INSERT INTO my_products(user_id, name_key, display_name, off_code, kcal_100g, "
        "protein_100g, fat_100g, carbs_100g, fiber_100g, source, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id, name_key) DO UPDATE SET "
        "display_name=excluded.display_name, off_code=excluded.off_code, "
        "kcal_100g=excluded.kcal_100g, protein_100g=excluded.protein_100g, "
        "fat_100g=excluded.fat_100g, carbs_100g=excluded.carbs_100g, "
        "fiber_100g=excluded.fiber_100g, source=excluded.source",
        (user_id, key, display_name, off_code, kcal_100g, protein_100g, fat_100g,
         carbs_100g, fiber_100g, source, _now_iso()),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM my_products WHERE user_id=? AND name_key=?", (user_id, key)
    ).fetchone()["id"]


def list_mine(conn, user_id: int):
    return conn.execute(
        "SELECT * FROM my_products WHERE user_id=? ORDER BY display_name", (user_id,)
    ).fetchall()


def forget(conn, user_id: int, name: str) -> bool:
    row = find_mine(conn, user_id, name)
    if row is None:
        return False
    conn.execute("DELETE FROM my_products WHERE id=?", (row["id"],))
    conn.commit()
    return True


if __name__ == "__main__":
    import os
    import tempfile
    from pathlib import Path

    os.environ["HEALTH_DB"] = str(Path(tempfile.mkdtemp()) / "health.db")
    from health_core.db import connect, migrate
    import health_core.foods as foods_mod

    # ---- search(): разбор ответа OFF, отбрасывание кандидатов без калорий ----
    def fake_off(url):
        assert "search_terms=" in url and "page_size=5" in url
        return {"products": [
            {"code": "111", "product_name": "Бородинский хлеб", "brands": "Каравай",
             "nutriments": {"energy-kcal_100g": 208, "proteins_100g": 6.8,
                             "fat_100g": 1.3, "carbohydrates_100g": 40.7, "fiber_100g": 6.7}},
            {"code": "222", "product_name": "Хлеб без данных", "brands": None,
             "nutriments": {}},  # без energy-kcal_100g -> отброшен
            {"code": "333", "product_name": "Хлеб бородинский тостовый", "brands": "Другой",
             "nutriments": {"energy-kcal_100g": 250}},
        ]}

    foods_mod._http_get_json = fake_off
    out = foods_mod.search("бородинский хлеб")
    assert "error" not in out, out
    cands = out["candidates"]
    assert len(cands) == 2, f"кандидат без калорий должен быть отброшен: {cands}"
    assert cands[0]["off_code"] == "111" and cands[0]["kcal_100g"] == 208
    assert cands[0]["brand"] == "Каравай" and cands[0]["fiber_100g"] == 6.7
    assert cands[1]["off_code"] == "333" and cands[1]["protein_100g"] is None
    print("OK: search() разбирает ответ OFF, отбрасывает кандидатов без калорий")

    assert foods_mod.search("")  == {"error": "Нужно название продукта"}
    print("OK: search('') -> error, без сети")

    def boom(url):
        raise foods_mod.urllib.error.URLError("no route to host")

    foods_mod._http_get_json = boom
    err = foods_mod.search("что угодно")
    assert "error" in err, err
    print("OK: сетевая ошибка -> {'error': ...}, не исключение")

    # ---- name_stems/keys_match: самопроверка из ТЗ ----
    a = foods_mod.name_stems("Хлеб Бородинский 80 г (в тостере)")
    b = foods_mod.name_stems("бородинский хлеб")
    assert a == b == {"хлеб", "бород"}, (a, b)
    print("OK: 'Хлеб Бородинский 80 г (в тостере)' и 'бородинский хлеб' -> один ключ")

    saved_key = foods_mod.name_key("бородинский")
    assert foods_mod.keys_match(saved_key, "Бородинские тосты сухие"), \
        "сохранённый 'бородинский' должен найтись в 'Бородинские тосты сухие'"
    print("OK: сохранённый 'бородинский' совпадает с 'Бородинские тосты сухие'")

    saved_key2 = foods_mod.name_key("творожный сыр")
    assert not foods_mod.keys_match(saved_key2, "творог 5%"), \
        "'творог 5%' не должен совпадать с сохранённым 'творожный сыр'"
    print("OK: 'творог 5%' не совпадает с сохранённым 'творожный сыр'")

    # ---- my_products: remember/find_mine/list_mine/forget на временной БД ----
    conn = connect()
    migrate(conn)
    conn.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (1, '2026-08-20 00:00:00')")
    uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]

    pid = foods_mod.remember(conn, uid, "Бородинский", source="off", off_code="111",
                              kcal_100g=208, protein_100g=6.8, fat_100g=1.3, carbs_100g=40.7,
                              fiber_100g=6.7)
    row = foods_mod.find_mine(conn, uid, "Бородинские тосты сухие")
    assert row is not None and row["id"] == pid and row["kcal_100g"] == 208, row
    print("OK: remember() -> find_mine() находит по другой формулировке")

    # remember того же продукта другой формулировкой -> апдейт, не дубликат
    pid2 = foods_mod.remember(conn, uid, "бородинский хлеб (обновлено)", source="off",
                               off_code="111", kcal_100g=210, protein_100g=7, fat_100g=1.3,
                               carbs_100g=40, fiber_100g=6.5)
    assert pid2 != pid, "'бородинский хлеб' — другой ключ (два слова), не апдейт однословного"
    assert len(foods_mod.list_mine(conn, uid)) == 2
    foods_mod.forget(conn, uid, "бородинский хлеб (обновлено)")

    pid3 = foods_mod.remember(conn, uid, "бородинский (обновлено)", source="off",
                               off_code="111", kcal_100g=210, protein_100g=7, fat_100g=1.3,
                               carbs_100g=40, fiber_100g=6.5)
    assert pid3 == pid, "повторный remember того же однословного ключа должен обновить ту же строку"
    assert len(foods_mod.list_mine(conn, uid)) == 1
    assert foods_mod.find_mine(conn, uid, "бородинский")["kcal_100g"] == 210
    print("OK: remember() того же продукта — апдейт по UNIQUE(user_id, name_key), не дубликат")

    assert foods_mod.forget(conn, uid, "бородинский") is True
    assert foods_mod.list_mine(conn, uid) == []
    assert foods_mod.forget(conn, uid, "бородинский") is False
    print("OK: forget() убирает продукт, повторный forget — False")

    conn.close()
    print("foods: ok")
