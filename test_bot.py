"""Сквозная проверка цикла агента без сети и без телеграма.

Модульные самотесты у каждого модуля свои (`python -m bot.registry` и т.д.).
Здесь проверяется ровно то, что они проверить не могут: что четыре модуля
соединяются друг с другом так, как договорено в Docs/bot_design.md.

    python test_bot.py

Сеть не трогается: llm._post подменяется сценарием заранее заданных ответов.
Боевая БД не трогается: HEALTH_DB уводится во временный файл ДО импортов.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

_TMP = tempfile.mkdtemp(prefix="health_bot_test_")
os.environ["HEALTH_DB"] = str(Path(_TMP) / "health.db")   # до импорта health_core.db
os.environ["TELEGRAM_ALLOWED_USERS"] = "111,222"

from bot import history, knowledge, llm, main, registry   # noqa: E402
from health_core.db import connect, migrate               # noqa: E402

PASSED, FAILED = [], []


def check(name: str, fn):
    try:
        fn()
        PASSED.append(name)
        print(f"OK   {name}")
    except Exception as e:
        FAILED.append((name, e))
        print(f"FAIL {name}: {e}")


def scripted(*responses):
    """Подменяет сетевой шов llm._post очередью готовых ответов провайдера."""
    queue = list(responses)

    async def _post(session, url, headers, payload, timeout_s=None):
        return 200, {"choices": [{"message": queue.pop(0)}]}

    return _post


PROVIDERS = [{"base_url": "http://x", "api_key_env": "FAKE_KEY", "model": "fake"}]
os.environ["FAKE_KEY"] = "test"


# ---------------------------------------------------------------- сборка контекста

def test_tool_specs():
    specs = main.tool_specs()
    names = [s["function"]["name"] for s in specs]
    assert len(names) == 36, f"35 инструментов плагина + knowledge, получено {len(names)}"
    assert "knowledge" in names, "инструмент знаний не подключён"
    assert "log_food" in names and "get_status_bar" in names
    for s in specs:
        f = s["function"]
        assert f.get("description"), f"пустое описание у {f['name']}"
        assert isinstance(f.get("parameters"), dict), f"нет схемы у {f['name']}"


def test_system_prompt_has_knowledge_index():
    prompt = main.system_prompt()
    assert len(prompt) > 1000, "персона не прочиталась с диска"
    assert knowledge.index().strip()[:40] in prompt, "индекс знаний не попал в системный промпт"


def test_dispatch_routes_knowledge():
    out = main.dispatch("knowledge", {"topic": "клетчатка"})
    assert isinstance(out, str) and out.strip(), "knowledge вернул пустоту"
    assert "log_food" not in out[:50]


# ---------------------------------------------------------------- полный ход

def test_full_turn_with_tool_call():
    """Сообщение → модель зовёт инструмент → результат уходит модели → ответ."""
    llm._post = scripted(
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "get_status_bar", "arguments": "{}"},
        }]},
        {"role": "assistant", "content": "Статус готов."},
    )
    messages = [
        {"role": "system", "content": "persona"},
        {"role": "user", "content": "как дела"},
    ]
    answer, full = asyncio.run(llm.run_loop(
        None, messages, main.tool_specs(), PROVIDERS, main.dispatch, max_iters=6))
    assert answer == "Статус готов.", answer
    roles = [m["role"] for m in full]
    assert "tool" in roles, f"результат инструмента не дописан: {roles}"
    tool_msg = next(m for m in full if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "c1"
    json.loads(tool_msg["content"])          # инструмент обязан вернуть разбираемый JSON


def test_caller_identity_reaches_handler():
    """set_caller выставлен в основном потоке, хендлер исполняется в другом —
    ContextVar обязан доехать, иначе всё пишется в чужой профиль."""
    registry.set_caller("909909")
    seen = {}

    def spy(name, args):
        from plugin import tools
        seen["caller"] = tools._caller_telegram_id()
        return json.dumps({"ok": True}, ensure_ascii=False)

    llm._post = scripted(
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "help", "arguments": "{}"},
        }]},
        {"role": "assistant", "content": "готово"},
    )
    asyncio.run(llm.run_loop(None, [{"role": "user", "content": "?"}],
                             main.tool_specs(), PROVIDERS, spy, max_iters=3))
    assert seen.get("caller") == "909909", f"личность звонящего потерялась: {seen}"


# ---------------------------------------------------------------- история

def test_history_roundtrip_and_isolation():
    conn = connect()
    migrate(conn)
    try:
        history.clear(conn, "111")
        history.clear(conn, "222")
        history.append(conn, "111", {"role": "user", "content": "привет"})
        history.append(conn, "111", {"role": "assistant", "content": "здравствуй"})
        history.append(conn, "222", {"role": "user", "content": "чужое"})

        mine = history.load(conn, "111")
        assert [m["content"] for m in mine] == ["привет", "здравствуй"], mine
        assert all("чужое" != m["content"] for m in mine), "видна чужая история"

        history.clear(conn, "111")
        assert history.load(conn, "111") == [], "clear не очистил"
        assert len(history.load(conn, "222")) == 1, "clear задел чужого"
    finally:
        conn.close()


def test_history_window_never_starts_with_tool():
    """Обрезка не должна оставлять role:"tool" в голове списка — такой запрос
    провайдер отвергает целиком."""
    conn = connect()
    migrate(conn)
    try:
        history.clear(conn, "111")
        for i in range(15):
            history.append(conn, "111", {"role": "user", "content": f"вопрос {i}"})
            history.append(conn, "111", {"role": "assistant", "content": None,
                                         "tool_calls": [{"id": f"c{i}", "type": "function",
                                                         "function": {"name": "help", "arguments": "{}"}}]})
            history.append(conn, "111", {"role": "tool", "tool_call_id": f"c{i}",
                                         "content": "{}" + "x" * 400})
            history.append(conn, "111", {"role": "assistant", "content": f"ответ {i}"})
        loaded = history.load(conn, "111")
        assert loaded, "окно пустое"
        assert loaded[0]["role"] == "user", f"окно начинается с {loaded[0]['role']}"
        ids = {m["tool_call_id"] for m in loaded if m["role"] == "tool"}
        announced = {c["id"] for m in loaded if m.get("tool_calls") for c in m["tool_calls"]}
        assert ids <= announced, f"осиротевшие результаты инструментов: {ids - announced}"
    finally:
        conn.close()


# ---------------------------------------------------------------- доступ и команды

def test_allowlist():
    assert main.allowed_users() == {"111", "222"}
    os.environ["TELEGRAM_ALLOWED_USERS"] = ""
    assert main.allowed_users() == set(), "пустая переменная обязана закрывать доступ всем"
    os.environ["TELEGRAM_ALLOWED_USERS"] = "111,222"


def test_access_approval_flow():
    admin = next(iter(main.admin_user_ids()))
    assert main._check_access("900001", None) == "new_pending"
    assert main._check_access("900001", None) == "pending", "повторное сообщение не должно заново слать заявку"
    assert "только администраторам" in main._cmd_approve("900001", "900001"), "не-админ одобрил сам себя"
    main._PENDING_USER_NOTIFICATIONS.clear()
    main._cmd_approve(admin, "900001")
    assert main._check_access("900001", None) == "approved"
    assert main._PENDING_USER_NOTIFICATIONS[0][0] == "900001", "одобренному не ушло уведомление"
    main._PENDING_USER_NOTIFICATIONS.clear()
    conn = connect()
    try:
        assert conn.execute("SELECT 1 FROM users WHERE telegram_user_id='900001'").fetchone(), \
            "одобрение не создало строку users — первый вызов инструмента упадёт"
    finally:
        conn.close()
    assert "Нельзя" in main._cmd_revoke(admin, admin), "админ отозвал сам себя"
    main._cmd_revoke(admin, "900001")
    assert main._check_access("900001", None) == "denied"


def test_telegram_menu():
    user = {c.command for c in main.menu_commands(admin=False)}
    admin = {c.command for c in main.menu_commands(admin=True)}
    handled = set(main.BUILTIN_COMMANDS) | set(main.ADMIN_COMMANDS) | {n for n, _h, _d in registry.SLASH_COMMANDS}
    assert handled == admin, f"команды без пункта меню у админа: {handled - admin}"
    assert {"model", "approve", "deny", "revoke", "access", "users", "mode"} <= admin
    assert not {"model", "approve", "deny", "revoke", "access", "users", "mode"} & user, "админские команды в меню пользователя"
    assert {"new", "help", "status", "week", "wipe"} <= user


def test_personal_knowledge_isolated():
    kdir = Path(_TMP) / "Knowledge"
    (kdir / "personal" / "900002").mkdir(parents=True)
    (kdir / "общее.md").write_text("общий текст для всех", encoding="utf-8")
    (kdir / "personal" / "900002" / "мой_протокол.md").write_text("личный протокол", encoding="utf-8")
    old = knowledge.KNOWLEDGE_DIR
    knowledge.KNOWLEDGE_DIR = kdir
    try:
        registry.set_caller("900002")
        assert "мой_протокол" in knowledge.index() and "общее" in knowledge.index()
        assert knowledge.read("мой_протокол") == "личный протокол"
        registry.set_caller("900003")
        assert "мой_протокол" not in knowledge.index(), "чужой личный документ в индексе"
        assert "не найдена" in knowledge.read("мой_протокол")
        assert "не найдена" in knowledge.read("personal/900002/мой_протокол"), "обход пути к чужим документам"
        assert "не найдена" in knowledge.read("../Knowledge/personal/900002/мой_протокол")
    finally:
        knowledge.KNOWLEDGE_DIR = old
        registry.set_caller("111")


def test_slash_commands_run_without_model():
    registry.set_caller("111")
    out = main.run_command("111", "/help")
    assert out and "```" not in out[:5], out
    assert main.run_command("111", "/hebrew") is None, "чужая команда должна уходить модели"
    assert "Контекст забыт" in main.run_command("111", "/new")
    assert "только администраторам" in main.run_command("111", "/model")


def test_admin_switch_model():
    admin_id = "374939064"
    out = main.run_command(admin_id, "/model")
    assert "Цепочка моделей" in out, out


def test_tool_loop_limit_is_recorded():
    """Упор в max_iters: ответ обязан лечь в messages, а не только уехать
    человеку — иначе показанное разойдётся с сохранённым в истории."""
    looping = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "c1", "type": "function",
        "function": {"name": "help", "arguments": "{}"},
    }]}
    llm._post = scripted(*[dict(looping) for _ in range(4)])
    answer, full = asyncio.run(llm.run_loop(
        None, [{"role": "user", "content": "?"}], main.tool_specs(),
        PROVIDERS, main.dispatch, max_iters=3))
    assert answer, "пустой ответ вместо объяснения"
    assert full[-1]["role"] == "assistant", f"ход кончается на {full[-1]['role']}"
    assert full[-1]["content"] == answer, "показанный текст разошёлся с сохранённым"


def test_user_lock_serializes_same_user_only():
    """Два сообщения одного человека идут по очереди (иначе его же записи в
    chat_history перемешиваются и запрос становится невалидным), а разные
    люди друг друга не ждут."""
    order = []

    async def turn(uid, tag):
        async with main._user_lock(uid):
            order.append(f"{tag}-начало")
            await asyncio.sleep(0.02)
            order.append(f"{tag}-конец")

    async def same_user():
        order.clear()
        await asyncio.gather(turn("111", "a"), turn("111", "b"))

    asyncio.run(same_user())
    assert order in (["a-начало", "a-конец", "b-начало", "b-конец"],
                     ["b-начало", "b-конец", "a-начало", "a-конец"]), order

    async def different_users():
        order.clear()
        await asyncio.gather(turn("111", "a"), turn("222", "b"))

    asyncio.run(different_users())
    assert order[1].endswith("начало"), f"разные люди встали в очередь: {order}"


def test_slash_command_releases_connections():
    """Слэш-команды идут мимо registry.dispatch(), а соединения текут так же —
    уборка обязана срабатывать и на этом пути."""
    registry.set_caller("111")
    main.run_command("111", "/help")
    conns = getattr(registry._leaked, "conns", [])
    assert not conns, f"после слэш-команды осталось {len(conns)} соединений"


def test_baseline_prefers_earlier_measurement():
    """Регистрация вводит СЕГОДНЯШНИЙ вес, историю импортируют потом — если
    профиль побеждает безусловно, «Δ от старта» считается от середины пути
    (живой случай: 0.2 кг вместо 38)."""
    from health_core.report import baseline_weight
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM body_metrics WHERE user_id=777")
        conn.execute("DELETE FROM users WHERE id=777")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,base_weight_kg,"
            "base_weight_date,created_at) VALUES(777,'777',180,'1980-01-01','male',120.5,"
            "'2026-06-07','2026-08-22 00:00:00')")
        for i, (at, kg) in enumerate((("2026-02-19 21:36:15", 158.8), ("2026-08-22 09:29:17", 120.5))):
            conn.execute("INSERT INTO body_metrics(user_id,burst_key,measured_at,weight_kg) "
                         "VALUES(777,?,?,?)", (f"k{i}", at, kg))
        conn.commit()
        assert baseline_weight(conn, 777) == 158.8, "замер раньше даты профиля обязан побеждать"

        # Профиль ПОЗЖЕ первого замера — исходное правило в силе: человек мог
        # худеть до системы, и его цифра честнее первого взвешивания.
        conn.execute("UPDATE users SET base_weight_date='2026-01-01' WHERE id=777")
        conn.commit()
        assert baseline_weight(conn, 777) == 120.5, "профиль раньше замеров обязан оставаться стартом"

        # Профиль пуст — берём самое раннее взвешивание.
        conn.execute("UPDATE users SET base_weight_kg=NULL WHERE id=777")
        conn.commit()
        assert baseline_weight(conn, 777) == 158.8
    finally:
        conn.execute("DELETE FROM body_metrics WHERE user_id=777")
        conn.execute("DELETE FROM users WHERE id=777")
        conn.commit()
        conn.close()


def test_caller_sets_timezone():
    """Граница суток берётся из пояса человека, а не сервера. Машина в UTC−7,
    человек в Москве — девять часов разрыва: без этого запись, сделанная им
    утром, ложилась во вчера, и вода за день показывала 0 при выпитых 500 мл."""
    from health_core import config
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM users WHERE id=778")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(778,'778',180,'1980-01-01',"
            "'male','Asia/Tokyo',100,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()
    finally:
        conn.close()

    config.set_tz(None)
    server = config.local_now()
    registry.set_caller("778")
    tokyo = config.local_now()
    assert abs((tokyo - server).total_seconds()) > 3000, (
        f"пояс не применился: сервер {server}, Токио {tokyo}")

    # Незнакомый пояс не должен ронять запись — падаем на время сервера.
    config.set_tz("Мордор/Барад-Дур")
    assert abs((config.local_now() - server).total_seconds()) < 60
    config.set_tz(None)

    conn = connect()
    conn.execute("DELETE FROM users WHERE id=778")
    conn.commit()
    conn.close()


def test_log_food_without_meal_slot_assigns_by_window():
    """CONTEXT.md «Приём пищи»: не назвал приём словом — код определяет сам по
    окну (health_core.chrono.meal_slot). 13:00 попадает в окно обеда config.yaml
    meals.lunch (12:00-16:00) по умолчанию -> lunch."""
    conn = connect()
    conn.execute("DELETE FROM alerts WHERE user_id=779")
    conn.execute("DELETE FROM food_log WHERE user_id=779")
    conn.execute("DELETE FROM users WHERE id=779")
    conn.execute(
        "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
        "base_weight_kg,base_weight_date,created_at) VALUES(779,'779',170,'1990-01-01',"
        "'f','UTC',65,'2026-01-01','2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    registry.set_caller("779")  # предыдущий тест оставляет свой caller_id в ContextVar
    try:
        out = json.loads(main.dispatch("log_food", {
            "eaten_at": "2026-09-10 13:00:00",
            "items": [{"name": "Суп", "kcal": 300, "protein_g": 15, "fat_g": 10, "carbs_g": 30}],
        }))
        assert "error" not in out, f"log_food без meal_slot не должен быть ошибкой: {out}"
        assert out.get("meal_slot") == "lunch", f"13:00 без meal_slot должно дать lunch, получили {out}"
    finally:
        conn = connect()
        conn.execute("DELETE FROM alerts WHERE user_id=779")
        conn.execute("DELETE FROM food_log WHERE user_id=779")
        conn.execute("DELETE FROM users WHERE id=779")
        conn.commit()
        conn.close()


def test_long_answer_split():
    text = "\n\n".join(["абзац " + "я" * 300 for _ in range(40)])
    parts = main._chunks(text, main.TELEGRAM_LIMIT)
    assert len(parts) > 1, "длинный текст не разбит"
    assert all(len(p) <= main.TELEGRAM_LIMIT for p in parts), "кусок длиннее лимита телеграма"
    assert "".join(p.replace("\n\n", "") for p in parts).count("абзац") == 40, "текст потерян при разбиении"


def test_log_weight_with_id_list_delete():
    """Запись веса → id в ответе → list содержит этот id → delete без id удаляет."""
    registry.set_caller("999")
    conn = connect()
    migrate(conn)
    try:
        # Зарегистрируем пользователя
        conn.execute("DELETE FROM users WHERE id=999")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(999,'999',180,'1980-01-01',"
            "'male','UTC',75,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()

        # Добавляем вес и проверяем ID в ответе
        result_add = json.loads(main.dispatch("log_weight", {
            "weight_kg": 75.5,
            "measured_at": "2026-01-01 12:00:00"
        }))
        assert "weight_id" in result_add, f"нет weight_id в ответе: {result_add}"
        weight_id = result_add["weight_id"]
        assert weight_id > 0, f"weight_id должен быть положительным, получено {weight_id}"

        # Проверяем что запись есть в list
        result_list = json.loads(main.dispatch("log_weight", {
            "action": "list",
            "limit": 10
        }))
        assert "entries" in result_list, f"нет entries в list: {result_list}"
        assert len(result_list["entries"]) > 0, "list пуст"
        found = any(e["weight_id"] == weight_id for e in result_list["entries"])
        assert found, f"weight_id {weight_id} не найден в list: {result_list}"

        # Удаляем последнюю запись без ID и проверяем что удалилась нужная
        result_del = json.loads(main.dispatch("log_weight", {
            "action": "delete"
        }))
        assert "deleted" in result_del, f"нет deleted в ответе: {result_del}"
        assert result_del["deleted"]["weight_id"] == weight_id, \
            f"удалилась не та запись: {result_del['deleted']['weight_id']} != {weight_id}"

        # Проверяем что запись действительно удалена
        result_list2 = json.loads(main.dispatch("log_weight", {
            "action": "list",
            "limit": 10
        }))
        found2 = any(e["weight_id"] == weight_id for e in result_list2["entries"])
        assert not found2, f"weight_id {weight_id} всё ещё есть в list после удаления"

    finally:
        conn.close()


def test_pharma_dose_ladder_bounds():
    """docs/adr/0002: рамки дозы держит код, не модель. 12.5->15 раньше 4
    недель — ошибка; 12.5->10 (снижение) — ок; прыжок 10->15 через 12.5 —
    ошибка; 12.5->20 с by_doctor=true — ок (рамки сняты)."""
    from datetime import timedelta
    from health_core import config

    registry.set_caller("888")
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM alerts WHERE user_id=888")
        conn.execute("DELETE FROM med_log WHERE user_id=888")
        conn.execute("DELETE FROM med_schedule WHERE user_id=888")
        conn.execute("DELETE FROM users WHERE id=888")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(888,'888',175,'1985-01-01',"
            "'male','UTC',90,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()

        now = config.local_now()

        # Начало терапии без расписания и истории: выше стартовой ступени — только by_doctor.
        r = json.loads(main.dispatch("pharma", {
            "action": "schedule", "substance": "Тирзепатид", "dose": 12.5,
            "unit": "mg", "route": "injection",
        }))
        assert "error" in r, f"старт терапии с 12.5 без by_doctor должен быть ошибкой: {r}"
        r = json.loads(main.dispatch("pharma", {
            "action": "schedule", "substance": "Тирзепатид", "dose": 12.5,
            "unit": "mg", "route": "injection", "by_doctor": True,
        }))
        assert "ok" in r, f"уже назначенная врачом доза должна приниматься с by_doctor: {r}"

        # Приём этой дозы 5 дней назад — с этой даты отсчитывается минимальный срок ступени.
        r = json.loads(main.dispatch("log_med", {
            "drug": "Тирзепатид", "dose": "12.5", "route": "injection", "unit": "mg",
            "at": (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"),
        }))
        assert "error" not in r, f"log_med не должен падать: {r}"

        # 12.5 -> 15 (соседняя ступень) раньше 4 недель на ступени — ошибка.
        r = json.loads(main.dispatch("pharma", {
            "action": "schedule", "substance": "Тирзепатид", "dose": 15,
        }))
        assert "error" in r, f"повышение раньше 4 недель должно быть ошибкой: {r}"

        # 12.5 -> 10: снижение разрешено на любую ступень без ограничения по сроку.
        r = json.loads(main.dispatch("pharma", {
            "action": "schedule", "substance": "Тирзепатид", "dose": 10,
        }))
        assert "ok" in r, f"снижение дозы не должно блокироваться: {r}"

        # 10 -> 15: прыжок через ступень 12.5 — ошибка, даже если бы срок уже прошёл.
        r = json.loads(main.dispatch("pharma", {
            "action": "schedule", "substance": "Тирзепатид", "dose": 15,
        }))
        assert "error" in r, f"прыжок через ступень должен быть ошибкой: {r}"

        # 12.5 -> 20 с by_doctor=true: рамки сняты, 20 не ступень и выше максимума — всё равно ок.
        r = json.loads(main.dispatch("pharma", {
            "action": "schedule", "substance": "Тирзепатид", "dose": 20, "by_doctor": True,
        }))
        assert "ok" in r, f"by_doctor должен снимать рамки: {r}"

    finally:
        conn.execute("DELETE FROM alerts WHERE user_id=888")
        conn.execute("DELETE FROM med_log WHERE user_id=888")
        conn.execute("DELETE FROM med_schedule WHERE user_id=888")
        conn.execute("DELETE FROM users WHERE id=888")
        conn.commit()
        conn.close()


def test_log_side_effect_add_list_delete():
    """Запись побочного эффекта → id в ответе → list содержит этот id → delete без id удаляет."""
    registry.set_caller("998")
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM users WHERE id=998")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(998,'998',180,'1980-01-01',"
            "'male','UTC',75,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()

        result_add = json.loads(main.dispatch("log_side_effect", {
            "symptom": "тошнота",
            "severity": "mild",
            "at": "2026-01-01 12:00:00",
        }))
        assert "side_effect_id" in result_add, f"нет side_effect_id в ответе: {result_add}"
        side_effect_id = result_add["side_effect_id"]
        assert side_effect_id > 0, f"side_effect_id должен быть положительным, получено {side_effect_id}"

        result_list = json.loads(main.dispatch("log_side_effect", {"action": "list", "limit": 10}))
        assert "entries" in result_list, f"нет entries в list: {result_list}"
        found = any(e["side_effect_id"] == side_effect_id for e in result_list["entries"])
        assert found, f"side_effect_id {side_effect_id} не найден в list: {result_list}"

        result_del = json.loads(main.dispatch("log_side_effect", {"action": "delete"}))
        assert "deleted" in result_del, f"нет deleted в ответе: {result_del}"
        assert result_del["deleted"]["side_effect_id"] == side_effect_id, \
            f"удалилась не та запись: {result_del['deleted']['side_effect_id']} != {side_effect_id}"

        result_list2 = json.loads(main.dispatch("log_side_effect", {"action": "list", "limit": 10}))
        found2 = any(e["side_effect_id"] == side_effect_id for e in result_list2["entries"])
        assert not found2, f"side_effect_id {side_effect_id} всё ещё есть в list после удаления"
    finally:
        conn.close()


def test_drug_card_draft_save_queues_notification():
    """drug_card_draft save (docs/adr/0002, health_core/card_drafts.py) создаёт
    pending-черновик и ставит уведомление админам в ту же очередь, что заявки
    на доступ — _cmd_approve её уже использует (test_access_approval_flow)."""
    from health_core import card_drafts

    registry.set_caller("997")
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM users WHERE id=997")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(997,'997',170,'1990-01-01',"
            "'female','UTC',65,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()
        conn.execute("DELETE FROM card_drafts WHERE substance='Тестовый Смоук-Препарат'")
        conn.commit()

        main._PENDING_USER_NOTIFICATIONS.clear()
        admin = next(iter(main.admin_user_ids()))

        result = json.loads(main.dispatch("drug_card_draft", {
            "action": "save",
            "substance": "Тестовый Смоук-Препарат",
            "fields": {"status": "не зарегистрирован (данные исследований)", "ladder": "2, 4, 6 мг"},
            "sources": ["https://clinicaltrials.gov/study/NCT00000000"],
        }))
        assert "draft_id" in result, f"нет draft_id в ответе: {result}"
        assert result["status"] == "pending", result

        row = conn.execute(
            "SELECT status FROM card_drafts WHERE id=?", (result["draft_id"],)
        ).fetchone()
        assert row is not None and row["status"] == "pending", "черновик не создан или не pending"

        assert any(target == admin and "Новый черновик карты" in text and "/drafts" in text
                   for target, text in main._PENDING_USER_NOTIFICATIONS), \
            f"уведомление админу не поставлено в очередь: {main._PENDING_USER_NOTIFICATIONS}"

        # Повторный save по тому же препарату не плодит второй черновик и второе
        # уведомление — save_draft (health_core/card_drafts.py) отдаёт тот же id.
        main._PENDING_USER_NOTIFICATIONS.clear()
        result2 = json.loads(main.dispatch("drug_card_draft", {
            "action": "save",
            "substance": "Тестовый Смоук-Препарат",
            "fields": {}, "sources": [],
        }))
        assert result2["draft_id"] == result["draft_id"], "повторный save должен вернуть тот же черновик"
    finally:
        conn.execute("DELETE FROM card_drafts WHERE substance='Тестовый Смоук-Препарат'")
        conn.execute("DELETE FROM users WHERE id=997")
        conn.commit()
        conn.close()
        main._PENDING_USER_NOTIFICATIONS.clear()


def test_council_request_creates_running_and_answers_immediately():
    """council action=request не ждёт саму работу консилиума (минуты): создаёт
    running-запись и сразу отвечает, задача уходит в очередь фонового запуска
    (bot/main.py._flush_pending_council забирает её оттуда)."""
    registry.set_caller("777")
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM users WHERE id=777")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(777,'777',180,'1980-01-01',"
            "'male','UTC',75,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()

        from bot import council
        council._PENDING_RUNS.clear()

        result = json.loads(main.dispatch("council", {"action": "request", "reason": "manual"}))
        assert "ok" in result, f"нет 'ok' в ответе: {result}"
        run_id = result["run_id"]

        row = conn.execute("SELECT status, reason FROM council_runs WHERE id=?", (run_id,)).fetchone()
        assert row is not None and row["status"] == "running" and row["reason"] == "manual", \
            dict(row) if row else None

        assert any(r[2] == run_id for r in council._PENDING_RUNS), \
            "заявка не встала в очередь фонового запуска"
        council._PENDING_RUNS.clear()

        # повторный request раньше 6ч по той же причине — отказ, running не размножается
        result2 = json.loads(main.dispatch("council", {"action": "request", "reason": "manual"}))
        assert "error" in result2, f"повторный request должен быть отклонён: {result2}"

        # action=status видит последний прогон
        status = json.loads(main.dispatch("council", {"action": "status"}))
        assert status["id"] == run_id and status["status"] == "running", status
    finally:
        conn.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            check(name[5:], fn)
    print(f"\n{len(PASSED)} прошло, {len(FAILED)} упало")
    for name, err in FAILED:
        print(f"  {name}: {err}")
    sys.exit(1 if FAILED else 0)
