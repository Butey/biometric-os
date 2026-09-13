"""Пульсовые зоны по формуле Tanaka: HRmax = 208 − 0.7 × возраст.

После 30 лет эта формула точнее, чем классическая «220 − возраст». Все зоны —
проценты от HRmax. Калибровка через hr_max_offset (измеренный максимум − формула).
"""

from datetime import datetime

from health_core.config import load, local_now


def hr_max(age: int, offset: int = 0) -> float:
    """HRmax по Tanaka (208 − 0.7 × возраст), плюс калибровка.

    Args:
        age: Полные годы.
        offset: Поправка к формуле (разница между измеренным и расчётным максимумом).

    Returns:
        Максимальная ЧСС, не округлено.
    """
    return 208 - 0.7 * age + offset


def zones(conn, user_id, on_date: str | None = None) -> dict | None:
    """Пульсовые зоны для пользователя на конкретную дату.

    Args:
        conn: Соединение с БД.
        user_id: ID пользователя.
        on_date: Дата в формате YYYY-MM-DD. По умолчанию — текущая дата.

    Returns:
        Словарь {"age": int, "hr_max": int, "zone2": [int, int], "ceiling": int}
        или None, если дата рождения отсутствует в профиле.
    """
    if on_date is None:
        on_date = local_now().date().isoformat()

    user = conn.execute(
        "SELECT birth_date FROM users WHERE id=?", (user_id,)
    ).fetchone()

    if user is None or user["birth_date"] is None:
        return None

    # Полные годы на on_date: вычитаем 1, если ДР ещё не был на этот год
    birth = datetime.strptime(user["birth_date"][:10], "%Y-%m-%d").date()
    target = datetime.strptime(on_date[:10], "%Y-%m-%d").date()
    age = target.year - birth.year
    if (target.month, target.day) < (birth.month, birth.day):
        age -= 1

    cfg = load()["training"]
    hr_max_offset = cfg.get("hr_max_offset", 0)
    m = hr_max(age, hr_max_offset)

    zone2_low_pct = cfg["zone2_low_pct"]
    zone2_high_pct = cfg["zone2_high_pct"]
    hr_ceiling_pct = cfg["hr_ceiling_pct"]

    return {
        "age": age,
        "hr_max": round(m),
        "zone2": [round(m * zone2_low_pct), round(m * zone2_high_pct)],
        "ceiling": round(m * hr_ceiling_pct),
    }


def classify(avg_hr, z: dict) -> str | None:
    """Классификация среднего пульса по зонам.

    Args:
        avg_hr: Средний пульс (уд/мин) или None.
        z: Словарь из zones(), содержащий zone2 и ceiling.

    Returns:
        Одна из: "ниже зоны 2", "зона 2", "выше зоны 2", "выше потолка", или None.
    """
    if avg_hr is None:
        return None

    zone2_low, zone2_high = z["zone2"]
    ceiling = z["ceiling"]

    if avg_hr < zone2_low:
        return "ниже зоны 2"
    elif avg_hr <= zone2_high:
        return "зона 2"
    elif avg_hr <= ceiling:
        return "выше зоны 2"
    else:
        return "выше потолка"


if __name__ == "__main__":
    import sqlite3
    import health_core.db as db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.DDL)

    # Пользователь, рождённый 1992-08-09
    conn.execute(
        "INSERT INTO users(telegram_user_id, birth_date, created_at) "
        "VALUES (1, '1992-08-09', '2026-08-20 00:00:00')"
    )
    uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=1").fetchone()["id"]

    # zones() на 2026-09-13 -> возраст 34
    z = zones(conn, uid, "2026-09-13")
    assert z is not None
    assert z["age"] == 34, f"expected age 34, got {z['age']}"
    assert z["hr_max"] == 184, f"expected hr_max 184, got {z['hr_max']}"
    assert z["zone2"] == [120, 134], f"expected zone2 [120, 134], got {z['zone2']}"
    assert z["ceiling"] == 140, f"expected ceiling 140, got {z['ceiling']}"

    # zones() на 2026-08-01 -> возраст ещё 33 (ДР 08-09, ещё не наступил)
    z2 = zones(conn, uid, "2026-08-01")
    assert z2["age"] == 33, f"expected age 33, got {z2['age']}"

    # classify() с разными значениями
    assert classify(110, z) == "ниже зоны 2"
    assert classify(128, z) == "зона 2"
    assert classify(134, z) == "зона 2"
    assert classify(138, z) == "выше зоны 2"
    assert classify(150, z) == "выше потолка"
    assert classify(None, z) is None

    # Пользователь без birth_date -> zones() возвращает None
    conn.execute(
        "INSERT INTO users(telegram_user_id, created_at) VALUES (2, '2026-08-20 00:00:00')"
    )
    uid_no_bd = conn.execute(
        "SELECT id FROM users WHERE telegram_user_id=2"
    ).fetchone()["id"]
    z_none = zones(conn, uid_no_bd, "2026-09-13")
    assert z_none is None, "zones() should return None if birth_date is missing"

    conn.close()
    print("OK: hr_zones — hr_max, zones (age calc, date logic), classify")
