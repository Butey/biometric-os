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
os.environ["HEALTH_ADMIN_IDS"] = "900000001"   # админ для тестов, реальный id в git не хранится

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
    from bot.registry import _tools

    registry.set_caller("111222")
    _tools._set_mode("111222", "user")
    specs = main.tool_specs()
    names = [s["function"]["name"] for s in specs]
    assert len(names) == 40, f"38 инструментов плагина + knowledge + export_data, получено {len(names)}"
    assert "knowledge" in names, "инструмент знаний не подключён"
    assert "log_food" in names and "get_status_bar" in names
    assert "admin_cmd" not in names, "admin_cmd не должен быть виден не-админу"
    for s in specs:
        f = s["function"]
        assert f.get("description"), f"пустое описание у {f['name']}"
        assert isinstance(f.get("parameters"), dict), f"нет схемы у {f['name']}"

    _tools._set_mode("111222", "admin")
    admin_names = [s["function"]["name"] for s in main.tool_specs()]
    assert len(admin_names) == 41, f"39 инструментов плагина + knowledge + export_data для админа, получено {len(admin_names)}"
    assert "admin_cmd" in admin_names, "admin_cmd должен быть виден админу"
    _tools._set_mode("111222", "user")


def test_system_prompt_has_knowledge_index():
    prompt = main.system_prompt()
    assert len(prompt) > 1000, "персона не прочиталась с диска"
    assert knowledge.index().strip()[:40] in prompt, "индекс знаний не попал в системный промпт"


def test_dispatch_routes_knowledge():
    out = main.dispatch("knowledge", {"topic": "клетчатка"})
    assert isinstance(out, str) and out.strip(), "knowledge вернул пустоту"
    assert "log_food" not in out[:50]


def test_tool_rules_attach_and_strip():
    """with_tool_rules приклеивает правила из Core/tool_rules.md к результату
    инструмента, у которого есть секция (forecast), и не трогает тот, у
    которого её нет (log_glucose). strip_tool_rules снимает блок обратно, и
    снятый текст совпадает с исходным результатом инструмента дословно."""
    plain = json.dumps({"ok": True}, ensure_ascii=False)
    with_rules = main.with_tool_rules("forecast", plain)
    assert with_rules != plain and "[ПРАВИЛА forecast]" in with_rules
    assert main.with_tool_rules("log_glucose", plain) == plain, "у log_glucose нет секции правил"
    assert main.strip_tool_rules(with_rules) == plain, "strip должен вернуть исходный текст дословно"
    assert main.strip_tool_rules(plain) == plain, "strip не должен ничего ломать без блока правил"


def test_log_water_pace_only_when_behind():
    """water_pace появляется, когда воды меньше 85% линейной нормы к этому часу
    (08:00-20:00), и не появляется при норме, вне окна и для прошлых дат."""
    from unittest.mock import patch
    from datetime import datetime as _dt
    import plugin.tools as _pt
    registry.set_caller("987")
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM users WHERE id=987")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(987,'987',180,'1990-01-01',"
            "'male','UTC',75,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()
        today = _dt.now().strftime("%Y-%m-%d")

        def _log(clock, ml):
            with patch.object(_pt, "_now_iso", lambda *a, **k: f"{today} {clock}"):
                return json.loads(main.strip_tool_rules(main.dispatch("log_water", {"ml": ml})))

        r = _log("15:00:00", 100)
        assert r["water_pace"]["behind_ml"] > 0, r
        r = _log("15:00:00", 5000)
        assert "water_pace" not in r, r
        conn.execute("DELETE FROM water_log WHERE user_id=987")
        conn.commit()
        assert "water_pace" not in _log("22:00:00", 100), "после 20:00 темп не считаем"
        assert "water_pace" not in _log("07:00:00", 100), "до 08:00 темп не считаем"
    finally:
        conn.execute("DELETE FROM water_log WHERE user_id=987")
        conn.commit()
        conn.close()


def test_water_reminder_only_when_a_portion_behind():
    """Напоминание о воде: пусто при норме и вне 08-20, текст с недостающим объёмом при отставании."""
    from unittest.mock import patch
    from datetime import datetime as _dt
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "scripts"))
    import water_reminder as _wr
    registry.set_caller("986")
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM users WHERE id=986")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(986,'986',180,'1990-01-01',"
            "'male','UTC',75,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()
        today = _dt.now().strftime("%Y-%m-%d")

        def _at(clock):
            return patch.object(_wr, "user_now", lambda *a, **k: _dt.fromisoformat(f"{today} {clock}"))

        with _at("15:00:00"):
            msg = _wr.reminder(conn, 986)
            assert msg and "Добери" in msg, msg
            conn.execute("INSERT INTO water_log(user_id, at, volume_ml) VALUES (986, ?, 5000)", (f"{today} 14:00:00",))
            conn.commit()
            assert _wr.reminder(conn, 986) is None, "норма выполнена - тишина"
        conn.execute("DELETE FROM water_log WHERE user_id=986")
        conn.commit()
        with _at("20:30:00"):
            assert _wr.reminder(conn, 986) is None, "после 20:00 тишина"
        with _at("08:30:00"):
            assert _wr.reminder(conn, 986) is None, "в 08:30 отставание меньше порции"
    finally:
        conn.execute("DELETE FROM water_log WHERE user_id=986")
        conn.execute("DELETE FROM users WHERE id=986")
        conn.commit()
        conn.close()


def test_system_prompt_moved_rules_out():
    """Core/system_promt.md больше не тащит тела шести перенесённых секций —
    они переехали в Core/tool_rules.md и приезжают только с результатом
    инструмента, — но заголовки и триггеры (каким инструментом когда
    пользоваться) остаются на месте."""
    prompt = main.system_prompt()
    for header in ("[ПРОГНОЗ МАССЫ]", "[БОЛЕЗНЬ]", "[MILESTONES]",
                   "[ТРЕНИРОВКИ И ПЛАНЫ НА ДЕНЬ]", "[ХОЛОДИЛЬНИК И ПЛАН ПИТАНИЯ]", "[ПУЛЬС]"):
        assert header in prompt, f"заголовок {header} пропал из промпта"
    assert "forecast" in prompt and "sick" in prompt, "триггеры вызова инструментов пропали"
    for gone in ("Три правила, они важнее удобства ответа",   # ПРОГНОЗ МАССЫ
                 "Препараты GLP-1-класса",                     # БОЛЕЗНЬ
                 "достаточно факта",                           # MILESTONES
                 "VR-шлем и приложения",                       # ТРЕНИРОВКИ И ПЛАНЫ
                 "не подмешивая случайные продукты",           # ХОЛОДИЛЬНИК
                 "оценка по возрасту с ошибкой"):               # ПУЛЬС
        assert gone not in prompt, f"тело перенесённого правила осталось в промпте: {gone!r}"


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
    assert {"new", "help", "status", "week", "wipe", "export"} <= user


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
    assert "Контекст забыт" in main.run_command("111", "/clear")
    assert main._plain('{"day_summary": "сводка"}') == "сводка"
    assert "только администраторам" in main.run_command("111", "/model")


def test_plan_talk_is_not_logged_as_eaten():
    """Обсуждение плана/вопрос без «съел» не должно превращаться в запись еды."""
    for t in ("Тунец можем добавить к яйцам", "а если вместо хлеба гречка", "собери завтрак", "что на ужин?",
              "давай запланируем творог на завтра", "понял в чем проблема - ты ошибся ноликом. там 0,27 КГ",
              "почему 27 то грамм", "не записывай блин обед"):
        assert main.is_plan_only(t), t
    for t in ("съел тунец 100г", "яичница 2 яйца СВ XXL, кабачок 290г", "+30 белка", "да", "можем записать: я съел яйца",
              "выпил 0.5 воды", "запиши завтрак: творог 120г",
              "завтрак: 2 яйца С0, тунец 140гр без сока, картофель baby 150гр, фасоль 200гр 365 дней, капуста квашеная 200гр в 11:37",
              "на завтрак 2 яйца", "завтрак 100г творога", "хочу записать 2 яйца", "хочу внести 2 яйца"):
        assert not main.is_plan_only(t), t
    main._PLAN_ONLY.set(True)
    try:
        out = json.loads(main.strip_tool_rules(main.dispatch("log_food", {"items": [{"name": "тунец", "grams": 100}]})))
        assert "error" in out and "НЕ записывай" in out["error"], out
    finally:
        main._PLAN_ONLY.set(False)


def test_model_switch_note():
    prov = [{"model": "a"}, {"model": "b"}]
    assert main.model_switch_note(["a"], prov) == ""
    assert main.model_switch_note([], prov) == ""
    assert "a не ответила" in main.model_switch_note(["b"], prov)


def test_admin_switch_model():
    admin_id = "900000001"
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


def test_tool_loop_limit_does_not_publish_provisional_claims():
    """Гипотеза до проверки инструментом не становится итогом при лимите."""
    from unittest.mock import patch

    provisional = "Плато подтверждено, причина — неправильные пищевые привычки."
    call = {"id": "plateau-check", "type": "function", "function": {
        "name": "forecast", "arguments": '{"action":"plateau"}',
    }}
    dispatched = []

    def dispatch(name, args):
        dispatched.append((name, args))
        return json.dumps({"plateau": {"detected": False}})

    for field in ("content", "reasoning_content", "reasoning"):
        response = {"role": "assistant", "content": None, "tool_calls": [call],
                    field: provisional}
        with patch.object(llm, "_post", scripted(response)):
            answer, full = asyncio.run(llm.run_loop(
                None, [{"role": "user", "content": "Почему вес стоит?"}],
                [], PROVIDERS, dispatch, max_iters=1))
        assert provisional not in answer, (field, answer)
        assert "Не удалось завершить проверку" in answer, answer
        assert full[-1]["content"] == answer
        assert any(m["role"] == "tool" for m in full), "результат проверки потерян"
    assert len(dispatched) == 3, dispatched


def test_reasoning_only_final_response_is_not_published():
    """Внутренний reasoning провайдера не заменяет отсутствующий ответ."""
    from unittest.mock import patch

    for field in ("reasoning_content", "reasoning"):
        with patch.object(llm, "_post", scripted({
            "role": "assistant", "content": None, field: "Внутренняя гипотеза о плато",
        })):
            answer, full = asyncio.run(llm.run_loop(
                None, [{"role": "user", "content": "Почему вес стоит?"}],
                [], PROVIDERS, lambda name, args: "{}"))
        assert answer == "", (field, answer)
        assert field not in full[-1], full[-1]


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
        out = json.loads(main.strip_tool_rules(main.dispatch("log_food", {
            "eaten_at": "2026-09-10 13:00:00",
            "items": [{"name": "Суп", "kcal": 300, "protein_g": 15, "fat_g": 10, "carbs_g": 30}],
        })))
        assert "error" not in out, f"log_food без meal_slot не должен быть ошибкой: {out}"
        assert out.get("meal_slot") == "lunch", f"13:00 без meal_slot должно дать lunch, получили {out}"
    finally:
        conn = connect()
        conn.execute("DELETE FROM alerts WHERE user_id=779")
        conn.execute("DELETE FROM food_log WHERE user_id=779")
        conn.execute("DELETE FROM users WHERE id=779")
        conn.commit()
        conn.close()


def test_log_food_without_eaten_at_uses_user_timezone():
    """Запись еды без eaten_at и со сброшенным _TZ ContextVar обязана использовать
    часовой пояс пользователя из БД (users.timezone), а не системное время сервера."""
    conn = connect()
    conn.execute("DELETE FROM alerts WHERE user_id=780")
    conn.execute("DELETE FROM food_log WHERE user_id=780")
    conn.execute("DELETE FROM users WHERE id=780")
    conn.execute(
        "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
        "base_weight_kg,base_weight_date,created_at) VALUES(780,'780',170,'1990-01-01',"
        "'m','Asia/Tokyo',70,'2026-01-01','2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    from datetime import datetime
    from zoneinfo import ZoneInfo
    from health_core import config
    from bot.registry import _tools
    _tools._CALLER_FALLBACK.set(None)
    config.set_tz(None)

    try:
        from plugin.tools import handle_log_food
        out = json.loads(handle_log_food({
            "user_id": 780,
            "items": [{"name": "Тофу", "kcal": 100, "protein_g": 10, "fat_g": 5, "carbs_g": 2}],
        }))
        assert "error" not in out, f"Ошибка записи: {out}"
        conn = connect()
        row = conn.execute("SELECT eaten_at FROM food_log WHERE user_id=780 ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        eaten_dt = datetime.strptime(row["eaten_at"], "%Y-%m-%d %H:%M:%S")
        tokyo_now = datetime.now(ZoneInfo("Asia/Tokyo")).replace(tzinfo=None)
        assert abs((eaten_dt - tokyo_now).total_seconds()) < 60, (
            f"Запись должна быть в поясе Токио (~{tokyo_now}), получили {eaten_dt}"
        )
    finally:
        config.set_tz(None)
        conn = connect()
        conn.execute("DELETE FROM alerts WHERE user_id=780")
        conn.execute("DELETE FROM food_log WHERE user_id=780")
        conn.execute("DELETE FROM users WHERE id=780")
        conn.commit()
        conn.close()


def test_food_lookup_remember_match_and_log_food_per_100g():
    """CONTEXT.md «Состав продукта»/«Мой продукт»: food_lookup remember сохраняет
    продукт -> match по ДРУГОЙ формулировке находит его -> log_food с per_100g
    и grams считает kcal/БЖУ кодом (не моделью) и пишет source=my_product в
    food_items."""
    registry.set_caller("881")
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM alerts WHERE user_id=881")
        conn.execute("DELETE FROM food_log WHERE user_id=881")
        conn.execute("DELETE FROM my_products WHERE user_id=881")
        conn.execute("DELETE FROM users WHERE id=881")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(881,'881',170,'1990-01-01',"
            "'f','UTC',65,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()

        remembered = json.loads(main.strip_tool_rules(main.dispatch("food_lookup", {
            "action": "remember", "name": "бородинский", "source": "off", "off_code": "111",
            "kcal_100g": 208, "protein_100g": 6.8, "fat_100g": 1.3, "carbs_100g": 40.7,
        })))
        assert remembered.get("ok") is True and remembered.get("product_id"), remembered

        found = json.loads(main.strip_tool_rules(main.dispatch("food_lookup", {
            "action": "match", "name": "Бородинские тосты сухие",
        })))
        assert found.get("found") is True, f"match по другой формулировке должен найти: {found}"
        assert found["kcal_100g"] == 208 and found["source"] == "off", found

        result = json.loads(main.strip_tool_rules(main.dispatch("log_food", {
            "items": [{
                "name": "Бородинские тосты сухие", "grams": 80,
                "per_100g": {"kcal": found["kcal_100g"], "protein_g": found["protein_100g"],
                             "fat_g": found["fat_100g"], "carbs_g": found["carbs_100g"]},
                "source": "my_product",
            }],
        })))
        assert "error" not in result, result
        item = result["items"][0]
        assert item["source"] == "my_product", item
        assert abs(item["kcal"] - 208 * 0.8) < 1e-6, item

        row = conn.execute(
            "SELECT kcal, protein_g, source FROM food_items fi JOIN food_log fl ON fl.id=fi.food_log_id "
            "WHERE fl.user_id=881 ORDER BY fi.id DESC LIMIT 1"
        ).fetchone()
        assert abs(row["kcal"] - 166.4) < 1e-6, dict(row)
        assert abs(row["protein_g"] - 6.8 * 0.8) < 1e-6, dict(row)
        assert row["source"] == "my_product", dict(row)

        # GPT заполняет необязательные поля нулями: kcal модели не затирается.
        result = json.loads(main.strip_tool_rules(main.dispatch("log_food", {
            "items": [{
                "name": "Перец болгарский", "grams": 0, "kcal": 39,
                "protein_g": 1.3, "fat_g": 0.4, "carbs_g": 7.5, "source": "estimate",
                "per_100g": {"kcal": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0},
            }],
        })))
        assert "error" not in result, result
        assert result["items"][0]["kcal"] == 39, result

        # per_100g.kcal==0 легитимен для реального продукта (вода) и не
        # должен требовать прямого kcal — не путать с "не передано".
        result = json.loads(main.strip_tool_rules(main.dispatch("log_food", {
            "items": [{
                "name": "Вода", "grams": 300, "source": "off",
                "per_100g": {"kcal": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0},
            }],
        })))
        assert "error" not in result, result
        assert result["items"][0]["kcal"] == 0, result

        # Модель шлёт fiber_g=0 рядом с per_100g.fiber_g — этикетка побеждает ноль.
        result = json.loads(main.strip_tool_rules(main.dispatch("log_food", {
            "items": [{
                "name": "Хлеб Дарницкий", "grams": 100, "fiber_g": 0, "source": "label",
                "per_100g": {"kcal": 216, "protein_g": 7, "fat_g": 1, "carbs_g": 46,
                             "fiber_g": 5},
            }],
        })))
        assert "error" not in result, result
        fib = conn.execute(
            "SELECT fiber_g FROM food_items fi JOIN food_log fl ON fl.id=fi.food_log_id "
            "WHERE fl.user_id=881 ORDER BY fi.id DESC LIMIT 1"
        ).fetchone()["fiber_g"]
        assert abs(fib - 5.0) < 1e-6, f"клетчатка из per_100g затёрта нулём модели: {fib}"

        # Отрицательные grams через per_100g не должны дать отрицательные kcal/БЖУ.
        result = json.loads(main.strip_tool_rules(main.dispatch("log_food", {
            "items": [{
                "name": "Перец болгарский", "grams": -100, "source": "estimate",
                "per_100g": {"kcal": 30, "protein_g": 1.0, "fat_g": 0.2, "carbs_g": 6.0},
            }],
        })))
        assert "error" in result, f"отрицательные grams должны быть отклонены: {result}"
    finally:
        conn.execute("DELETE FROM alerts WHERE user_id=881")
        conn.execute("DELETE FROM food_log WHERE user_id=881")
        conn.execute("DELETE FROM my_products WHERE user_id=881")
        conn.execute("DELETE FROM users WHERE id=881")
        conn.commit()
        conn.close()


def test_panels_not_kept_in_history():
    """Готовый блок в ``` не должен оставаться в истории: модель копировала
    оттуда старый пульт вместо нового вызова get_day_summary."""
    panel = "```\n📊 МЕТАБОЛ. ПУЛЬТ — 16.09 10:23\nКлетч 0/25\n```"
    out = main.strip_panels(f"Вот пульт:\n{panel}\nВопросы?")
    assert "ПУЛЬТ" not in out and "Клетч" not in out, out
    assert out.startswith("Вот пульт:") and out.endswith("Вопросы?"), out
    assert main.strip_panels("обычный ответ без блока") == "обычный ответ без блока"


def test_long_answer_split():
    text = "\n\n".join(["абзац " + "я" * 300 for _ in range(40)])
    parts = main._chunks(text, main.TELEGRAM_LIMIT)
    assert len(parts) > 1, "длинный текст не разбит"
    assert all(len(p) <= main.TELEGRAM_LIMIT for p in parts), "кусок длиннее лимита телеграма"
    assert "".join(p.replace("\n\n", "") for p in parts).count("абзац") == 40, "текст потерян при разбиении"


def test_log_weight_with_id_list_delete():
    """Запись веса → id в ответе → list содержит этот id → delete без id удаляет.

    log_weight несёт правила [MILESTONES] (achieved_milestones) — main.dispatch
    приклеивает их текстом после JSON, поэтому здесь снимаем блок правил
    strip_tool_rules перед json.loads, как это делает и _close_turn для истории."""
    def _dispatch_json(name, args):
        return json.loads(main.strip_tool_rules(main.dispatch(name, args)))

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
        result_add = _dispatch_json("log_weight", {
            "weight_kg": 75.5,
            "measured_at": "2026-01-01 12:00:00"
        })
        assert "weight_id" in result_add, f"нет weight_id в ответе: {result_add}"
        weight_id = result_add["weight_id"]
        assert weight_id > 0, f"weight_id должен быть положительным, получено {weight_id}"

        # Проверяем что запись есть в list
        result_list = _dispatch_json("log_weight", {
            "action": "list",
            "limit": 10
        })
        assert "entries" in result_list, f"нет entries в list: {result_list}"
        assert len(result_list["entries"]) > 0, "list пуст"
        found = any(e["weight_id"] == weight_id for e in result_list["entries"])
        assert found, f"weight_id {weight_id} не найден в list: {result_list}"

        # Удаляем последнюю запись без ID и проверяем что удалилась нужная
        result_del = _dispatch_json("log_weight", {
            "action": "delete"
        })
        assert "deleted" in result_del, f"нет deleted в ответе: {result_del}"
        assert result_del["deleted"]["weight_id"] == weight_id, \
            f"удалилась не та запись: {result_del['deleted']['weight_id']} != {weight_id}"

        # Проверяем что запись действительно удалена
        result_list2 = _dispatch_json("log_weight", {
            "action": "list",
            "limit": 10
        })
        found2 = any(e["weight_id"] == weight_id for e in result_list2["entries"])
        assert not found2, f"weight_id {weight_id} всё ещё есть в list после удаления"

    finally:
        conn.close()


def test_log_weight_stale_year_is_fixed():
    """Регрессия: модель присылает measured_at с устаревшим годом (2024) —
    год исправляется на текущий, запись сохраняется. До фикса handle_log_weight
    не вызывал _year_is_stale, и _norm_event_ts отклоняла запись как
    «дата старше 400 дней»."""
    import json
    from datetime import date

    def _dispatch_json(name, args):
        return json.loads(main.strip_tool_rules(main.dispatch(name, args)))

    registry.set_caller("985")
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM alerts WHERE user_id=985")
        conn.execute("DELETE FROM body_metrics WHERE user_id=985")
        conn.execute("DELETE FROM users WHERE id=985")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(985,'985',175,'1985-01-01',"
            "'male','UTC',80,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()

        cur_year = date.today().year
        # Метка с устаревшим годом: 2024, тот же месяц и день что сегодня
        today_mmdd = date.today().strftime("%m-%d")
        stale_ts = f"2024-{today_mmdd} 08:00:00"

        result = _dispatch_json("log_weight", {
            "weight_kg": 82.3,
            "measured_at": stale_ts,
        })
        assert "error" not in result, (
            f"log_weight с устаревшим годом вернул ошибку вместо исправления: {result}"
        )
        assert "weight_id" in result, f"нет weight_id в ответе: {result}"

        # Запись должна лежать с текущим годом, не 2024
        saved = conn.execute(
            "SELECT measured_at FROM body_metrics WHERE user_id=985 AND id=?",
            (result["weight_id"],)
        ).fetchone()
        assert saved is not None, "запись не найдена в БД"
        assert saved["measured_at"].startswith(str(cur_year)), (
            f"год в базе должен быть {cur_year}, а не 2024: {saved['measured_at']}"
        )
    finally:
        conn.execute("DELETE FROM alerts WHERE user_id=985")
        conn.execute("DELETE FROM body_metrics WHERE user_id=985")
        conn.execute("DELETE FROM users WHERE id=985")
        conn.commit()
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


def test_pharma_schedule_dose_zero_no_ladder_does_not_wipe():
    """Препарат без карты лестницы (_check_dose_bounds не выполняется): модель
    заполняет непереданное поле dose нулём вместо пропуска — 0 не должен
    затирать уже сохранённую дозу через COALESCE(excluded.dose,dose)."""
    registry.set_caller("889")
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM med_schedule WHERE user_id=889")
        conn.execute("DELETE FROM users WHERE id=889")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(889,'889',175,'1985-01-01',"
            "'male','UTC',90,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()

        r = json.loads(main.dispatch("pharma", {
            "action": "schedule", "substance": "Незарегистрированный Препарат XYZ",
            "dose": 5, "unit": "mg", "route": "injection",
        }))
        assert "ok" in r, f"первичная запись дозы без карты должна пройти: {r}"

        # Модель хочет обновить только остаток, но шлёт dose=0 вместо пропуска.
        r = json.loads(main.dispatch("pharma", {
            "action": "schedule", "substance": "Незарегистрированный Препарат XYZ",
            "dose": 0, "stock_doses": 3,
        }))
        assert "ok" in r, r
        row = conn.execute(
            "SELECT dose, stock_doses FROM med_schedule WHERE user_id=889 AND substance=?",
            ("Незарегистрированный Препарат XYZ",)).fetchone()
        assert row["dose"] == 5, f"dose=0 затёр реальную дозу: {dict(row)}"
        assert row["stock_doses"] == 3, dict(row)
    finally:
        conn.execute("DELETE FROM med_schedule WHERE user_id=889")
        conn.execute("DELETE FROM users WHERE id=889")
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

        result_add = json.loads(main.strip_tool_rules(main.dispatch("log_side_effect", {
            "symptom": "тошнота",
            "severity": "mild",
            "at": "2026-01-01 12:00:00",
        })))
        assert "side_effect_id" in result_add, f"нет side_effect_id в ответе: {result_add}"
        side_effect_id = result_add["side_effect_id"]
        assert side_effect_id > 0, f"side_effect_id должен быть положительным, получено {side_effect_id}"

        result_list = json.loads(main.strip_tool_rules(main.dispatch("log_side_effect", {"action": "list", "limit": 10})))
        assert "entries" in result_list, f"нет entries в list: {result_list}"
        found = any(e["side_effect_id"] == side_effect_id for e in result_list["entries"])
        assert found, f"side_effect_id {side_effect_id} не найден в list: {result_list}"

        result_del = json.loads(main.strip_tool_rules(main.dispatch("log_side_effect", {"action": "delete"})))
        assert "deleted" in result_del, f"нет deleted в ответе: {result_del}"
        assert result_del["deleted"]["side_effect_id"] == side_effect_id, \
            f"удалилась не та запись: {result_del['deleted']['side_effect_id']} != {side_effect_id}"

        result_list2 = json.loads(main.strip_tool_rules(main.dispatch("log_side_effect", {"action": "list", "limit": 10})))
        found2 = any(e["side_effect_id"] == side_effect_id for e in result_list2["entries"])
        assert not found2, f"side_effect_id {side_effect_id} всё ещё есть в list после удаления"
    finally:
        conn.close()


def test_log_watch_day_upsert_validate_autoraise_trends():
    """День с часов (CONTEXT.md «День с часов», «Дневной пульс», «Максимальный
    пульс»): upsert дня ПО КАЖДОМУ ПОКАЗАТЕЛЮ отдельно, отказы на
    неправдоподобных значениях, авто-поднятие личного максимума (два дня
    подряд выше текущего, тест не перебивается, вниз не снижается), и наличие
    блока в get_trends.

    log_watch_day и get_trends несут секции в tool_rules.md — main.dispatch
    приклеивает их текстом после JSON, поэтому здесь снимаем блок правил
    strip_tool_rules перед json.loads (как test_log_weight_with_id_list_delete)."""
    def _dispatch_json(name, args):
        return json.loads(main.strip_tool_rules(main.dispatch(name, args)))

    registry.set_caller("996")
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM users WHERE id=996")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(996,'996',180,'1990-01-01',"
            "'male','UTC',75,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()

        # --- upsert: повторная запись дня заменяет прежнюю, не плодит вторую строку ---
        r1 = _dispatch_json("log_watch_day", {
            "days": [{"date": "2026-08-20", "hr_min": 55, "hr_avg": 70, "hr_max": 120}]})
        assert r1["days"][0]["action"] == "added", r1
        r2 = _dispatch_json("log_watch_day", {
            "days": [{"date": "2026-08-20", "hr_min": 50, "hr_avg": 68, "hr_max": 118}]})
        assert r2["days"][0]["action"] == "replaced", r2
        n = conn.execute("SELECT COUNT(*) c FROM daily_watch WHERE user_id=996 AND date='2026-08-20'").fetchone()["c"]
        assert n == 1, f"upsert должен оставить одну строку, получили {n}"

        # --- per-metric upsert: батч с одними шагами не стирает пульс того же дня ---
        r2b = _dispatch_json("log_watch_day", {"days": [{"date": "2026-08-20", "steps": 7000}]})
        assert r2b["days"][0]["action"] == "replaced", r2b
        row_pm = conn.execute(
            "SELECT hr_min, hr_avg, hr_max, steps FROM daily_watch WHERE user_id=996 AND date='2026-08-20'"
        ).fetchone()
        assert row_pm["hr_min"] == 50 and row_pm["hr_avg"] == 68 and row_pm["hr_max"] == 118, dict(row_pm)
        assert row_pm["steps"] == 7000, dict(row_pm)

        # --- валидация: min>max, диапазон шагов и дата из будущего отклоняются с понятной причиной ---
        r3 = _dispatch_json("log_watch_day", {
            "days": [{"date": "2026-08-21", "hr_min": 100, "hr_max": 90}]})
        assert "error" in r3["days"][0], r3

        r3b = _dispatch_json("log_watch_day", {"days": [{"date": "2026-08-21", "steps": 999999}]})
        assert "error" in r3b["days"][0], r3b

        from datetime import date as _date, timedelta as _td
        future = (_date.today() + _td(days=30)).isoformat()
        r4 = _dispatch_json("log_watch_day", {"days": [{"date": future, "hr_avg": 70}]})
        assert "error" in r4["days"][0] and "будущ" in r4["days"][0]["error"], r4

        # --- авто-поднятие максимума: один день выше формулы ничего не делает ---
        r5 = _dispatch_json("log_watch_day", {"days": [{"date": "2026-08-22", "hr_max": 230}]})
        assert r5["max_hr_update"] is None, "один всплеск не должен поднимать личный максимум"

        # --- второй день выше текущего — поднимает, source='watch' ---
        r6 = _dispatch_json("log_watch_day", {"days": [{"date": "2026-08-23", "hr_max": 225}]})
        assert r6["max_hr_update"] is not None and r6["max_hr_update"]["new"] == 225, r6
        urow = conn.execute("SELECT hr_max_bpm, hr_max_source FROM users WHERE id=996").fetchone()
        assert urow["hr_max_bpm"] == 225 and urow["hr_max_source"] == "watch", dict(urow)

        # --- источник 'test' часами не перебивается, даже двумя днями выше ---
        conn.execute("UPDATE users SET hr_max_bpm=210, hr_max_source='test' WHERE id=996")
        conn.commit()
        _dispatch_json("log_watch_day", {"days": [{"date": "2026-08-24", "hr_max": 235}]})
        _dispatch_json("log_watch_day", {"days": [{"date": "2026-08-25", "hr_max": 236}]})
        urow2 = conn.execute("SELECT hr_max_bpm, hr_max_source FROM users WHERE id=996").fetchone()
        assert urow2["hr_max_bpm"] == 210 and urow2["hr_max_source"] == "test", "тест не должен перебиваться часами"

        # --- вниз автоматически не снижается (чистая история без старых высоких дней) ---
        conn.execute("DELETE FROM daily_watch WHERE user_id=996")
        conn.execute("UPDATE users SET hr_max_bpm=210, hr_max_source='watch' WHERE id=996")
        conn.commit()
        # даты относительно сегодня: get_trends смотрит только последние 28 дней
        d1 = (_date.today() - _td(days=2)).isoformat()
        d2 = (_date.today() - _td(days=1)).isoformat()
        _dispatch_json("log_watch_day", {"days": [{"date": d1, "hr_min": 55, "hr_avg": 65, "hr_max": 100}]})
        _dispatch_json("log_watch_day", {"days": [{"date": d2, "hr_min": 56, "hr_avg": 66, "hr_max": 101}]})
        urow3 = conn.execute("SELECT hr_max_bpm FROM users WHERE id=996").fetchone()
        assert urow3["hr_max_bpm"] == 210, "максимум не должен снижаться от низких дней"

        # --- get_trends содержит блок дня с часов ---
        trends = _dispatch_json("get_trends", {})
        assert "daily_watch" in trends, f"нет поля daily_watch в get_trends: {trends}"
        assert trends["daily_watch"] is not None and trends["daily_watch"]["coverage_days"] == 2, trends["daily_watch"]
    finally:
        conn.close()


def test_log_watch_day_step_goal_and_steps_drop():
    """CONTEXT.md «Цель шагов»: медиана 28д до последнего понедельника +1000,
    потолок 10000, <7 дней с шагами в окне -> None. steps_drop: падение
    медианы шагов >= guards.steps_drop_pct между окнами 28/28."""
    from datetime import datetime as _dt, timedelta as _td
    from health_core import watch as _watch
    from health_core.config import user_today as _user_today

    registry.set_caller("995")
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM users WHERE id=995")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(995,'995',180,'1990-01-01',"
            "'male','UTC',75,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()
        uid = 995
        today = _dt.strptime(_user_today(conn, uid), "%Y-%m-%d").date()
        monday = today - _td(days=today.weekday())

        for i in range(5):
            d = (monday - _td(days=i)).isoformat()
            _watch.save_days(conn, uid, [{"date": d, "steps": 5000}])
        assert _watch.step_goal(conn, uid, today.isoformat()) is None, "меньше 7 дней с шагами в окне -> None"

        for i in range(5, 10):
            d = (monday - _td(days=i)).isoformat()
            _watch.save_days(conn, uid, [{"date": d, "steps": 5000}])
        assert _watch.step_goal(conn, uid, today.isoformat()) == 6000, "медиана 5000 + 1000 = 6000"

        for i in range(10):
            d = (monday - _td(days=i)).isoformat()
            _watch.save_days(conn, uid, [{"date": d, "steps": 20000}])
        assert _watch.step_goal(conn, uid, today.isoformat()) == 10000, "потолок цели шагов — 10000"

        # --- steps_drop: падение медианы >=15% между последними 28д и предыдущими 28д ---
        conn.execute("DELETE FROM daily_watch WHERE user_id=?", (uid,))
        conn.commit()
        assert _watch.steps_drop(conn, uid) is None, "нет данных -> None"
        for days_ago in range(7):
            d = (today - _td(days=days_ago)).isoformat()
            _watch.save_days(conn, uid, [{"date": d, "steps": 4000}])
        assert _watch.steps_drop(conn, uid) is None, "нет предыдущего окна -> None"
        for days_ago in range(28, 35):
            d = (today - _td(days=days_ago)).isoformat()
            _watch.save_days(conn, uid, [{"date": d, "steps": 10000}])
        sd = _watch.steps_drop(conn, uid)
        assert sd is not None and sd["drop_pct"] >= 15, sd
    finally:
        conn.close()


def test_recovery_low_fires_and_sick_silences():
    """RECOVERY_LOW (CONTEXT.md «Сигнал восстановления»): 3 дня подряд HRV
    заметно ниже своей медианы и hr_min заметно выше своей — срабатывает через
    check_all; молчит, если один из 3 дней помечен больным."""
    from datetime import timedelta as _td
    from health_core import watch as _watch, sick
    from health_core.guards import check_all, _now

    registry.set_caller("994")
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM users WHERE id=994")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(994,'994',180,'1990-01-01',"
            "'male','UTC',75,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()
        uid = 994
        today = _now().date()

        for days_ago in range(3, 31):
            d = (today - _td(days=days_ago)).isoformat()
            _watch.save_days(conn, uid, [{"date": d, "hrv_ms": 50, "hr_min": 55}])
        for days_ago in (2, 1, 0):
            d = (today - _td(days=days_ago)).isoformat()
            _watch.save_days(conn, uid, [{"date": d, "hrv_ms": 40, "hr_min": 62}])
        conn.commit()
        alerts = check_all(conn, uid)
        assert any(a["code"] == "RECOVERY_LOW" for a in alerts), f"RECOVERY_LOW должен сработать, получили {alerts}"

        sick.start(conn, uid, (today - _td(days=1)).isoformat(), 1, note="грипп")
        conn.commit()
        alerts_sick = check_all(conn, uid)
        assert not any(a["code"] == "RECOVERY_LOW" for a in alerts_sick), (
            f"RECOVERY_LOW должен молчать, если один из 3 дней болен, получили {alerts_sick}"
        )
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

        # running, оборванный перезапуском (старше часа), не держит лок вечно
        conn.execute("UPDATE council_runs SET started_at='2026-01-01 00:00:00' WHERE id=?", (run_id,))
        conn.commit()
        result3 = json.loads(main.dispatch("council", {"action": "request", "reason": "dose"}))
        assert "ok" in result3, f"зависший running должен сниматься: {result3}"
        old = conn.execute("SELECT status FROM council_runs WHERE id=?", (run_id,)).fetchone()
        assert old["status"] == "failed", dict(old)
        council._PENDING_RUNS.clear()
    finally:
        conn.close()


def test_council_timeout():
    """Консилиум не должен получать короткий чатовый timeout_s провайдера,
    а свой, намного больший (300с), переданный из config.yaml council.timeout_s."""
    import time
    from bot import council
    seen = []

    async def _post(session, url, headers, payload, timeout_s=None):
        seen.append((payload["model"], timeout_s))
        return 200, {"choices": [{"message": {"content": "ok"}}]}

    llm._post = _post
    llm._KEY_STATES.clear()
    base = {"base_url": "http://example", "api_key_env": "FAKE_KEY", "timeout_s": 15}
    asyncio.run(llm.chat(None, [{"role": "user", "content": "?"}], [], [{**base, "model": "test_model"}]))
    assert seen[-1][1] == 15, seen

    text = asyncio.run(council._call_one(None, "s", "u", {**base, "model": "test_council_model"},
                                         timeout_s=300, retry_delay_s=0, max_retries=0))
    assert text == "ok"
    assert seen[-1] == ("test_council_model", 300), seen
    llm._KEY_STATES.clear()


def test_daily_target_source_is_blend():
    """daily_target() шаг 1 — расход дня (ADR 0004, docs/adr/0004-расход-дня.md),
    взвешенное среднее оценок, а не одна адаптивная-или-затравочная база: тег
    источника в computed_from/source обязан начинаться с "blend", а не с
    прежних "adaptive"/"estimated_tdee"/"bmr_floor_seed"."""
    from health_core.energy import daily_target
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM users WHERE id=781")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "created_at) VALUES(781,'781',180,'1980-01-01','male','UTC','2026-01-01 00:00:00')")
        conn.execute(
            "INSERT INTO body_metrics(user_id,burst_key,measured_at,weight_kg) "
            "VALUES(781,'blend-src','2026-08-20 07:00:00',90.0)")
        conn.commit()
        result = daily_target(conn, 781, "2026-08-20")
        assert result["source"].startswith("blend"), \
            f"source обязан начинаться с 'blend' (ADR 0004), получили {result['source']}"
    finally:
        conn.execute("DELETE FROM body_metrics WHERE user_id=781")
        conn.execute("DELETE FROM daily_targets WHERE user_id=781")
        conn.execute("DELETE FROM users WHERE id=781")
        conn.commit()
        conn.close()


def test_steps_line():
    """steps_line: None input → None; with goal, below/at/above → formatted string; no goal → steps only."""
    from scripts.morning_checkin import steps_line

    # None steps
    assert steps_line(None, 8000) is None, "None steps should return None"
    assert steps_line(None, None) is None, "None steps should return None even without goal"

    # With goal, below
    result = steps_line(7000, 8000)
    assert result == "Шаги вчера: 7000 из 8000", result

    # With goal, at goal
    result = steps_line(8000, 8000)
    assert result == "Шаги вчера: 8000 из 8000 ✓", result

    # With goal, above
    result = steps_line(9000, 8000)
    assert result == "Шаги вчера: 9000 из 8000 ✓", result

    # Without goal
    result = steps_line(7500, None)
    assert result == "Шаги вчера: 7500", result


def test_steps_prompt():
    """steps_prompt: None → ask for screenshot; below goal → estimate walk time; at/above → None."""
    from scripts.meal_window_check import steps_prompt

    # None steps, no goal
    result = steps_prompt(None, None)
    assert result == "Пришли скриншот шагов за сегодня — посмотрю, сколько добрать до цели.", result

    # None steps, with goal
    result = steps_prompt(None, 8000)
    assert result == "Пришли скриншот шагов за сегодня — посмотрю, сколько добрать до цели. Цель: 8000.", result

    # Below goal: 6200 of 8000 → 18 мин (1800 steps / 100)
    result = steps_prompt(6200, 8000)
    assert result == "Шагов пока 6200 из 8000 — прогулка ~18 мин добирает.", result

    # At goal
    result = steps_prompt(8000, 8000)
    assert result is None, "At goal should return None"

    # Above goal
    result = steps_prompt(9000, 8000)
    assert result is None, "Above goal should return None"

    # Steps known, no goal
    result = steps_prompt(7500, None)
    assert result is None, "Known steps without goal should return None"


def test_injection_block():
    """injection_block: returns None for user with no scheduled injections."""
    from scripts.morning_checkin import injection_block

    registry.set_caller("991")
    conn = connect()
    migrate(conn)
    try:
        conn.execute("DELETE FROM users WHERE id=991")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(991,'991',170,'1990-01-01',"
            "'f','UTC',65,'2026-01-01','2026-01-01 00:00:00')")
        conn.commit()

        user_row = conn.execute("SELECT * FROM users WHERE id=991").fetchone()
        result = injection_block(conn, user_row, "2026-01-01")
        assert result is None, f"User without schedule should return None, got: {result}"
    finally:
        conn.execute("DELETE FROM users WHERE id=991")
        conn.commit()
        conn.close()


def test_forecast_calibrate():
    """forecast action=calibrate: подбор калорийности и согласование вехи."""
    from datetime import date, timedelta
    registry.set_caller("992")
    conn = connect()
    migrate(conn)
    today = date.today()
    try:
        conn.execute("DELETE FROM users WHERE id=992")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(992,'992',185,'1992-08-09',"
            "'m','UTC',125,'2026-01-01','2026-01-01 00:00:00')")
        for i in range(7):
            d = today - timedelta(days=6 - i)
            conn.execute(
                "INSERT INTO body_metrics(user_id,burst_key,measured_at,weight_kg,ffm_kg,fat_pct) "
                "VALUES(992, ?, ?, 120.0, 80.0, 33.3)", (f"k{i}", f"{d.isoformat()} 08:00:00"))
        conn.execute(
            "INSERT INTO milestones(user_id,name,metric,threshold,deadline) "
            "VALUES(992,'115 кг','weight_kg',115.0, ?)", ((today + timedelta(days=40)).isoformat(),))
        conn.commit()

        # Безопасность проверяем относительно действующей политики, не старого
        # фиксированного пола: policy/config могут менять его для этого профиля.
        from health_core.energy import daily_expenditure, kcal_floor
        floor, _ = kcal_floor(conn, 992, daily_expenditure(conn, 992, today.isoformat())["kcal"])
        below_floor = max(1, int(floor) - 100)
        above_floor = int(floor) + 200
        raw = main.dispatch("forecast", {"action": "calibrate", "target_kcal": below_floor, "user_id": 992})
        out = json.loads(main.strip_tool_rules(raw))
        assert out.get("mode") == "force_kcal", out
        assert out.get("safe") is False, out
        assert "ниже безопасного пола" in out.get("safety_warnings", [""])[0], out

        # Режим 2: безопасная калорийность + согласование и применение.
        raw_app = main.dispatch("forecast", {"action": "calibrate", "target_kcal": above_floor, "apply": True, "user_id": 992})
        out_app = json.loads(main.strip_tool_rules(raw_app))
        assert out_app.get("safe") is True, out_app
        assert out_app.get("applied") is True, out_app
        m_row = conn.execute("SELECT deadline FROM milestones WHERE user_id=992 AND name='115 кг'").fetchone()
        assert m_row["deadline"] == out_app.get("aligned_deadline"), (m_row["deadline"], out_app.get("aligned_deadline"))

        # Режим 3: подбор вариантов без калоража и дедлайна
        raw_opt = main.dispatch("forecast", {"action": "calibrate", "target_kg": 115.0, "user_id": 992})
        out_opt = json.loads(main.strip_tool_rules(raw_opt))
        assert out_opt.get("mode") == "options", out_opt
        assert len(out_opt.get("options", [])) == 3, out_opt
    finally:
        conn.execute("DELETE FROM body_metrics WHERE user_id=992")
        conn.execute("DELETE FROM milestones WHERE user_id=992")
        conn.execute("DELETE FROM users WHERE id=992")
        conn.commit()
        conn.close()


def test_forecast_plateau():
    """forecast action=plateau: прогноз и статус плато массы тела."""
    from datetime import date, timedelta
    registry.set_caller("993")
    conn = connect()
    migrate(conn)
    today = date.today()
    try:
        conn.execute("DELETE FROM users WHERE id=993")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(993,'993',180,'1990-01-01',"
            "'m','UTC',120,'2026-01-01','2026-01-01 00:00:00')")
        for i in range(7):
            d = today - timedelta(days=6 - i)
            conn.execute(
                "INSERT INTO body_metrics(user_id,burst_key,measured_at,weight_kg,ffm_kg,fat_pct) "
                "VALUES(993, ?, ?, 110.0, 75.0, 31.8)", (f"pk{i}", f"{d.isoformat()} 08:00:00"))
        conn.execute(
            "INSERT INTO anthropometry(user_id, measured_on, site, value_cm) VALUES(993, ?, 'талия', 102.0)",
            ((today - timedelta(days=20)).isoformat(),))
        conn.execute(
            "INSERT INTO anthropometry(user_id, measured_on, site, value_cm) VALUES(993, ?, 'талия', 100.0)",
            (today.isoformat(),))
        conn.commit()

        raw = main.dispatch("forecast", {"action": "plateau", "user_id": 993})
        out = json.loads(main.strip_tool_rules(raw))
        assert "pharmacological_plateau" in out, out
        ph = out["pharmacological_plateau"]
        assert ph.get("phase") == "популяционный ориентир", ph
        assert ph.get("expected_date") is None and ph.get("is_individual_prediction") is False, ph
        assert ph.get("median_plateau_weeks") is not None
        assert ph.get("weeks_on_program") is not None

        assert "metabolic_equilibrium" in out, out
        eq = out["metabolic_equilibrium"]
        assert eq.get("equilibrium_weight_kg") is not None
        assert eq.get("intake_kcal") is not None

        assert "current_stagnation" in out, out
        st = out["current_stagnation"]
        assert "verdict" in st
        assert len(out.get("recommendations", [])) > 0
    finally:
        conn.execute("DELETE FROM anthropometry WHERE user_id=993")
        conn.execute("DELETE FROM body_metrics WHERE user_id=993")
        conn.execute("DELETE FROM users WHERE id=993")
        conn.commit()
        conn.close()


def test_interactive_commands_target_plateau_forecast():
    """Интерактивные команды /target, /plateau, /forecast и форматирование с клавиатурами."""
    from datetime import date, timedelta
    registry.set_caller("988")
    conn = connect()
    migrate(conn)
    today = date.today()
    try:
        conn.execute("DELETE FROM body_metrics WHERE user_id=988")
        conn.execute("DELETE FROM milestones WHERE user_id=988")
        conn.execute("DELETE FROM users WHERE id=988")
        conn.execute(
            "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
            "base_weight_kg,base_weight_date,created_at) VALUES(988,'988',185,'1992-08-09',"
            "'m','UTC',125,'2026-01-01','2026-01-01 00:00:00')")
        for i in range(7):
            d = today - timedelta(days=6 - i)
            conn.execute(
                "INSERT INTO body_metrics(user_id,burst_key,measured_at,weight_kg,ffm_kg,fat_pct) "
                "VALUES(988, ?, ?, 120.0, 80.0, 33.3)", (f"k988_{i}", f"{d.isoformat()} 08:00:00"))
        conn.execute(
            "INSERT INTO milestones(user_id,name,metric,threshold,deadline) "
            "VALUES(988,'115 кг','weight_kg',115.0, ?)", ((today + timedelta(days=40)).isoformat(),))
        conn.commit()

        # 1. /target interactive
        msg, kb = main._format_target_interactive("988", "")
        assert "Подбор оптимальной" in msg
        assert kb is not None and len(kb.inline_keyboard) >= 2
        assert any(b.callback_data == "target_opt:optimal" for row in kb.inline_keyboard for b in row)

        # 2. Preview option
        prev_msg, prev_kb = main._format_target_option_preview("988", "optimal")
        assert "Оптимальный" in prev_msg
        assert "БЖУ" in prev_msg or "Белки" in prev_msg
        assert prev_kb is not None
        assert any(b.callback_data.startswith("target_apply:") for row in prev_kb.inline_keyboard for b in row)

        # 3. Apply target
        app_msg, app_kb = main._apply_target("988", 1850)
        assert "зафиксирована" in app_msg
        assert "1850" in app_msg

        # 4. /plateau interactive
        plat_msg, plat_kb = main._format_plateau_interactive("988", "")
        assert "плато" in plat_msg.lower()
        assert "SURMOUNT" in plat_msg
        assert "Ожидаемая дата плато на текущей дозе" not in plat_msg
        assert "осталось ~" not in plat_msg and "None" not in plat_msg
        assert "сценарий питания" in plat_msg.lower() or "Сценарий по целевому калоражу" in plat_msg or "полнота журнала не подтверждена" in plat_msg
        assert plat_kb is not None
        assert not any(b.callback_data == "plateau_refeed" for row in plat_kb.inline_keyboard for b in row), "нет длительной паузы и замеров талии — не предлагать рефид"
        assert any(b.callback_data == "plateau_council" for row in plat_kb.inline_keyboard for b in row)
        from unittest.mock import patch
        from health_core import forecast
        sustained = forecast.plateau_forecast(conn, 988)
        sustained["current_stagnation"]["sustained_weight_waist_pause"] = True
        with patch.object(forecast, "plateau_forecast", return_value=sustained):
            _, sustained_kb = main._format_plateau_interactive("988", "")
        assert any(b.callback_data == "plateau_refeed" for row in sustained_kb.inline_keyboard for b in row)

        # 5. /forecast interactive
        fc_msg, fc_kb = main._format_forecast_interactive("988", "")
        assert "Прогноз динамики" in fc_msg
        assert "Через 4 нед" in fc_msg
        assert fc_kb is not None

        # 6. run_command plain text fallbacks
        cmd_t = main.run_command("988", "/target")
        assert cmd_t and "Подбор" in cmd_t
        cmd_p = main.run_command("988", "/plateau")
        assert cmd_p and "плато" in cmd_p.lower()
        cmd_f = main.run_command("988", "/forecast")
        assert cmd_f and "Прогноз" in cmd_f
    finally:
        conn.execute("DELETE FROM body_metrics WHERE user_id=988")
        conn.execute("DELETE FROM milestones WHERE user_id=988")
        conn.execute("DELETE FROM users WHERE id=988")
        conn.commit()
        conn.close()


def test_daily_watch_spo2_support():
    from plugin import tools
    registry.set_caller("989")
    conn = connect()
    try:
        migrate(conn)
        conn.execute("DELETE FROM users WHERE id=989")
        conn.execute(
            "INSERT INTO users (id, telegram_user_id, height_cm, birth_date, sex, timezone, base_weight_kg, created_at) "
            "VALUES (989, '989', 180, '1990-01-01', 'm', 'UTC', 80.0, '2026-01-01 00:00:00')"
        )
        conn.commit()

        # Save day with spo2_avg, spo2_min, spo2_max
        res = json.loads(tools.handle_log_watch_day({
            "user_id": 989,
            "days": [{
                "date": "2026-09-16",
                "hr_min": 50,
                "hr_avg": 70,
                "hr_max": 100,
                "spo2_avg": 97,
                "spo2_min": 94,
                "spo2_max": 99,
            }]
        }))
        assert "days" in res
        assert res["days"][0]["action"] == "added"
        assert res["days"][0]["spo2_avg"] == 97

        # List days
        listing = json.loads(tools.handle_log_watch_day({"user_id": 989, "action": "list"}))
        assert len(listing["entries"]) >= 1
        entry = listing["entries"][0]
        assert entry["spo2_avg"] == 97
        assert entry["spo2_min"] == 94
        assert entry["spo2_max"] == 99
    finally:
        conn.execute("DELETE FROM daily_watch WHERE user_id=989")
        conn.execute("DELETE FROM users WHERE id=989")
        conn.commit()
        conn.close()


def test_dashboard_milestone_selection_consistency():
    from plugin import tools
    registry.set_caller("990")
    conn = connect()
    try:
        migrate(conn)
        conn.execute("DELETE FROM users WHERE id=990")
        conn.execute(
            "INSERT INTO users (id, telegram_user_id, height_cm, birth_date, sex, timezone, base_weight_kg, created_at) "
            "VALUES (990, '990', 185, '1992-08-09', 'm', 'UTC', 158.8, '2026-01-01 00:00:00')"
        )
        # Current weight 118.4
        conn.execute(
            "INSERT INTO body_metrics (user_id, burst_key, measured_at, weight_kg) "
            "VALUES (990, 'k990_0', '2026-09-17 08:00:00', 118.4)"
        )
        # Three milestones: 115 kg, 110 kg, 90 kg
        conn.execute("INSERT INTO milestones (user_id, name, metric, threshold, deadline) VALUES (990, '115 кг', 'weight_kg', 115.0, '2026-10-16')")
        conn.execute("INSERT INTO milestones (user_id, name, metric, threshold, deadline) VALUES (990, 'target_110', 'weight_kg', 110.0, '2026-11-02')")
        conn.execute("INSERT INTO milestones (user_id, name, metric, threshold, deadline) VALUES (990, 'NEW_YEAR', 'weight_kg', 90.0, '2027-01-01')")
        conn.commit()

        bar_res = json.loads(tools.handle_get_status_bar({"user_id": 990, "format": "bar"}))
        dash_res = json.loads(tools.handle_get_status_bar({"user_id": 990, "format": "dashboard"}))

        assert "115" in bar_res["status_bar"], bar_res["status_bar"]
        assert "115.0 кг" in dash_res["status_bar"], dash_res["status_bar"]
        assert "110.0 кг" not in dash_res["status_bar"], dash_res["status_bar"]
    finally:
        conn.execute("DELETE FROM daily_targets WHERE user_id=990")
        conn.execute("DELETE FROM user_targets WHERE user_id=990")
        conn.execute("DELETE FROM persona_styles WHERE user_id=990")
        conn.execute("DELETE FROM alerts WHERE user_id=990")
        conn.execute("DELETE FROM body_metrics WHERE user_id=990")
        conn.execute("DELETE FROM milestones WHERE user_id=990")
        conn.execute("DELETE FROM users WHERE id=990")
        conn.commit()
        conn.close()


def test_check_stale_calib_streak():
    from health_core.guards import check_stale_calib
    from datetime import datetime, timedelta

    conn = connect()
    try:
        migrate(conn)
        conn.execute("DELETE FROM users WHERE id=991")
        # User created 20 days ago
        conn.execute(
            "INSERT INTO users (id, telegram_user_id, height_cm, birth_date, sex, timezone, base_weight_kg, created_at) "
            "VALUES (991, '991', 180, '1990-01-01', 'm', 'UTC', 100.0, '2026-08-01 00:00:00')"
        )
        conn.commit()

        # Case 1: No food log -> streak 0 < 7 -> alert
        al = check_stale_calib(conn, 991)
        assert al is not None, "Должен сработать STALE_CALIB при 0 днях лога"
        assert al["code"] == "STALE_CALIB"
        assert "0 из 7 дней подряд" in al["message"]

        # Case 2: Only 3 days streak -> alert
        today = datetime.now()
        for d in range(3):
            dt = (today - timedelta(days=d)).strftime("%Y-%m-%d 12:00:00")
            conn.execute("INSERT INTO food_log (user_id, eaten_at, meal_slot) VALUES (991, ?, 'lunch')", (dt,))
        conn.commit()
        al = check_stale_calib(conn, 991)
        assert al is not None, "Должен сработать STALE_CALIB при серии 3 дня (< 7)"
        assert "3 из 7 дней подряд" in al["message"]

        # Case 3: 7 days continuous streak -> None (no alert)
        for d in range(3, 8):
            dt = (today - timedelta(days=d)).strftime("%Y-%m-%d 12:00:00")
            conn.execute("INSERT INTO food_log (user_id, eaten_at, meal_slot) VALUES (991, ?, 'lunch')", (dt,))
        conn.commit()
        al = check_stale_calib(conn, 991)
        assert al is None, f"При серии >= 7 дней STALE_CALIB не должен срабатывать, получено: {al}"

        # Case 4: Brand new user (< 7 days account age) -> None
        conn.execute("DELETE FROM users WHERE id=992")
        conn.execute(
            "INSERT INTO users (id, telegram_user_id, height_cm, birth_date, sex, timezone, base_weight_kg, created_at) "
            "VALUES (992, '992', 180, '1990-01-01', 'm', 'UTC', 100.0, ?)",
            ((today - timedelta(days=3)).strftime("%Y-%m-%d 00:00:00"),)
        )
        conn.commit()
        al_new = check_stale_calib(conn, 992)
        assert al_new is None, f"Новый аккаунт (< 7 дней) не должен получать алерт, получено: {al_new}"

    finally:
        conn.execute("DELETE FROM food_log WHERE user_id IN (991, 992)")
        conn.execute("DELETE FROM alerts WHERE user_id IN (991, 992)")
        conn.execute("DELETE FROM users WHERE id IN (991, 992)")
        conn.commit()
        conn.close()


def _water_user(uid: int):
    conn = connect()
    migrate(conn)
    conn.execute(f"DELETE FROM water_log WHERE user_id={uid}")
    conn.execute(f"DELETE FROM users WHERE id={uid}")
    conn.execute(
        "INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,"
        "base_weight_kg,base_weight_date,created_at) VALUES(?,?,170,'1990-01-01',"
        "'f','UTC',65,'2026-01-01','2026-01-01 00:00:00')", (uid, str(uid)))
    conn.commit()
    conn.close()
    registry.set_caller(str(uid))


def _water_rows(uid: int) -> int:
    conn = connect()
    n = conn.execute("SELECT COUNT(*) n FROM water_log WHERE user_id=?", (uid,)).fetchone()["n"]
    conn.execute("DELETE FROM water_log WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM alerts WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return n


def test_failure_after_commit_is_saved_with_warning():
    """Исключение после conn.commit() не должно выглядеть отказом: запись в базе,
    а {"error"} заставляет модель повторять вызов (log_water шесть раз за шесть секунд)."""
    from bot.registry import _tools
    _water_user(7801)
    real = _tools.day_summary

    def boom(*a, **kw):
        raise RuntimeError("summary exploded")
    _tools.day_summary = boom
    try:
        out = json.loads(registry.dispatch("log_water", {"ml": 250}))
    finally:
        _tools.day_summary = real
    rows = _water_rows(7801)
    assert rows == 1, f"запись должна лежать ровно одна, лежит {rows}"
    assert out.get("saved") is True and "summary exploded" in out.get("warning", ""), out
    assert "error" not in out, f"сохранённая запись не должна быть ошибкой: {out}"


def test_unnamed_time_ignores_model_at():
    """Человек времени не называл, модель подставила at из прошлых вызовов: пишем «сейчас»."""
    from health_core import config
    _water_user(7802)
    try:
        config.set_user_text("выпил 500 мл воды")
        a = json.loads(registry.dispatch("log_water", {"ml": 500, "at": "2026-10-08 22:11:00"}))
        config.set_user_text("выпил 500 мл в 9:30")
        b = json.loads(registry.dispatch("log_water", {"ml": 500, "at": "2026-10-08 09:30:00"}))
    finally:
        config.set_user_text(None)
    conn = connect()
    ats = [r["at"] for r in conn.execute("SELECT at FROM water_log WHERE user_id=7802 ORDER BY id")]
    conn.close()
    _water_rows(7802)
    assert ats[0][:10] == config.local_today() and ats[0] != "2026-10-08 22:11:00", ats
    assert ats[1] == "2026-10-08 09:30:00", ats


def test_med_past_weekday_keeps_event_date_and_requires_at():
    """Сообщённый в среду воскресный укол сохраняет дату события и одну дозу запаса."""
    from datetime import datetime
    from unittest.mock import patch
    from health_core import config
    uid = 7810
    _water_user(uid)
    conn = connect()
    conn.execute(
        "INSERT INTO med_schedule(user_id,substance,dose,unit,route,every_days,next_at,stock_doses) "
        "VALUES (?, 'Тирзепатид',12.5,'mg','injection',7,'2026-11-08 22:00:00',3)", (uid,))
    conn.commit()
    conn.close()
    try:
        with patch.object(config, "user_now", return_value=datetime(2026, 11, 4, 21, 51)):
            config.set_user_text("В прошлое воскресенье сделал укол, забыл записать")
            args = {"drug": "Тирзепатид", "dose": "12.5", "unit": "mg", "route": "injection"}
            missing = json.loads(registry.dispatch("log_med", args))
            assert "error" in missing, missing
            wrong_date = json.loads(registry.dispatch("log_med", {**args, "at": "2026-11-04 21:51:00"}))
            assert "error" in wrong_date and wrong_date.get("expected_date") == "2026-11-01", wrong_date
            conn = connect()
            assert conn.execute("SELECT COUNT(*) n FROM med_log WHERE user_id=?", (uid,)).fetchone()["n"] == 0
            assert conn.execute("SELECT stock_doses FROM med_schedule WHERE user_id=?", (uid,)).fetchone()[0] == 3
            conn.close()
            result = json.loads(registry.dispatch("log_med", {**args, "at": "2026-11-01 22:00:00"}))
            assert result.get("at") == "2026-11-01 22:00:00", result
        conn = connect()
        row = conn.execute("SELECT at FROM med_log WHERE user_id=?", (uid,)).fetchone()
        sched = conn.execute("SELECT next_at, stock_doses FROM med_schedule WHERE user_id=?", (uid,)).fetchone()
        conn.close()
        assert row["at"] == "2026-11-01 22:00:00"
        assert sched["next_at"] == "2026-11-08 22:00:00" and sched["stock_doses"] == 2, dict(sched)
    finally:
        config.set_user_text(None)
        conn = connect()
        conn.execute("DELETE FROM med_log WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM med_schedule WHERE user_id=?", (uid,))
        conn.commit()
        conn.close()
        _water_rows(uid)


def test_med_date_update_preserves_stock_schedule_and_user_boundary():
    """Исправление даты меняет существующую запись, без новой дозы и чужих записей."""
    from health_core import config
    uid = 7811
    _water_user(uid)
    conn = connect()
    med_id = conn.execute(
        "INSERT INTO med_log(user_id,substance,dose,unit,route,at) "
        "VALUES (?,'Тирзепатид','12.5','mg','injection','2026-11-04 21:51:27')", (uid,)).lastrowid
    conn.execute(
        "INSERT INTO med_schedule(user_id,substance,every_days,next_at,stock_doses) "
        "VALUES (?,'Тирзепатид',7,'2026-11-08 22:00:00',2)", (uid,))
    conn.commit()
    before = dict(conn.execute("SELECT * FROM med_schedule WHERE user_id=?", (uid,)).fetchone())
    conn.close()
    try:
        config.set_user_text("Да, исправь")
        missing = json.loads(registry.dispatch("log_med", {"action": "update", "med_id": med_id}))
        assert "error" in missing, missing
        missing_id = json.loads(registry.dispatch("log_med", {"action": "update", "at": "2026-11-01"}))
        assert "error" in missing_id, missing_id
        result = json.loads(registry.dispatch("log_med", {
            "action": "update", "med_id": med_id, "at": "2026-11-01 21:51:27"}))
        assert result.get("updated") and result["previous_at"] == "2026-11-04 21:51:27", result
        assert result["at"] == "2026-11-01 21:51:27" and not result["stock_changed"], result
        conn = connect()
        rows = conn.execute("SELECT id,at FROM med_log WHERE user_id=?", (uid,)).fetchall()
        after = dict(conn.execute("SELECT * FROM med_schedule WHERE user_id=?", (uid,)).fetchone())
        conn.close()
        assert len(rows) == 1 and rows[0]["id"] == med_id and rows[0]["at"] == result["at"]
        assert after == before, (before, after)
        registry.set_caller("111")
        foreign = json.loads(registry.dispatch("log_med", {
            "action": "update", "med_id": med_id, "at": "2026-11-02 21:51:27"}))
        assert "error" in foreign, foreign
        conn = connect()
        assert conn.execute("SELECT at FROM med_log WHERE id=?", (med_id,)).fetchone()[0] == result["at"]
        conn.close()
    finally:
        config.set_user_text(None)
        conn = connect()
        conn.execute("DELETE FROM med_log WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM med_schedule WHERE user_id=?", (uid,))
        conn.commit()
        conn.close()
        _water_rows(uid)


def test_failure_before_commit_stays_error():
    from bot.registry import _tools
    _water_user(7802)
    real = _tools._norm_event_ts

    def boom(*a, **kw):
        raise RuntimeError("bad timestamp")
    _tools._norm_event_ts = boom
    try:
        out = json.loads(registry.dispatch("log_water", {"ml": 250}))
    finally:
        _tools._norm_event_ts = real
    rows = _water_rows(7802)
    assert rows == 0, f"до коммита запись не должна появиться, лежит {rows}"
    assert "bad timestamp" in out.get("error", "") and "saved" not in out, out


def test_food_lookup_off_down_says_no_retry_and_offers_my_product():
    import urllib.error
    from health_core import foods
    _water_user(7803)
    conn = connect()
    foods.remember(conn, 7803, "тунец рубленый", source="label", kcal_100g=120, protein_100g=26)
    conn.commit()
    conn.close()
    real = foods._http_get_json

    def down(url):
        raise urllib.error.HTTPError(url, 503, "Service Temporarily Unavailable", {}, None)
    foods._http_get_json = down
    try:
        mine = json.loads(registry.dispatch("food_lookup", {"action": "search", "name": "тунец рубленый"}))
        none = json.loads(registry.dispatch("food_lookup", {"action": "search", "name": "неизвестный продукт"}))
    finally:
        foods._http_get_json = real
        conn = connect()
        conn.execute("DELETE FROM my_products WHERE user_id=7803")
        conn.commit()
        conn.close()
        _water_rows(7803)
    for out in (mine, none):
        assert out.get("retry") is False and "estimate" in out.get("instruction", ""), out
        assert "503" in out.get("error", ""), out
    assert mine.get("mine", {}).get("kcal_100g") == 120, f"свой продукт должен подсказываться: {mine}"
    assert "mine" not in none, none


def test_log_sleep_quality_zero_is_not_given():
    _water_user(7804)
    try:
        out = json.loads(registry.dispatch("log_sleep", {"night_date": "2026-09-10", "duration_min": 450, "quality": 0}))
        bad = json.loads(registry.dispatch("log_sleep", {"night_date": "2026-09-11", "duration_min": 450, "quality": 6}))
        conn = connect()
        q = conn.execute("SELECT quality FROM sleep_log WHERE user_id=7804 AND night_date='2026-09-10'").fetchone()
        conn.close()
    finally:
        conn = connect()
        conn.execute("DELETE FROM sleep_log WHERE user_id=7804")
        conn.commit()
        conn.close()
        _water_rows(7804)
    assert "error" not in out and "quality" not in out, f"quality=0 не ошибка и не запись: {out}"
    assert q is not None and q["quality"] is None, "в базе должен лечь NULL"
    assert "1..5" in bad.get("error", ""), f"6 остаётся ошибкой: {bad}"


def test_log_food_string_items_is_validation_error():
    _water_user(7805)
    try:
        out = json.loads(registry.dispatch("log_food", {"items": ["яйца СВ", "Тунец рубленый"]}))
        conn = connect()
        n = conn.execute("SELECT COUNT(*) n FROM food_log WHERE user_id=7805").fetchone()["n"]
        conn.close()
    finally:
        _water_rows(7805)
    assert "объектов" in out.get("error", "") and "'str'" not in out["error"], out
    assert n == 0, "строки в items не должны ничего записывать"


def test_pantry_tuna_inflected_stems_and_product_weight():
    _water_user(7806)
    try:
        # 1. "Филе тунца в собственном соке" matches inflected stem "тунц" -> typical weight 140g without need_weight
        out = json.loads(registry.dispatch("pantry", {"action": "add", "name": "Филе тунца в собственном соке", "qty": 4, "unit": "шт", "category": "Белковые", "user_id": 7806}))
        assert "ok" in out, f"Филе тунца должно добавиться без need_weight: {out}"

        # 2. meal_options should plan with it instead of putting into uncounted
        from health_core.meal_options import grams_per_unit
        g, assumed = grams_per_unit("шт", "Филе тунца в собственном соке")
        assert g == 140 and assumed is True

        # 3. Product with weight in display_name or piece_g in my_products is resolved automatically
        conn = connect()
        from health_core import foods
        foods.remember(conn, 7806, "Экспонента черника (банка 250г)", source="label", kcal_100g=60, protein_100g=12)
        conn.close()
        out2 = json.loads(registry.dispatch("pantry", {"action": "add", "name": "Экспонента черника", "qty": 2, "unit": "банка", "category": "Молочка/Сыры", "user_id": 7806}))
        assert "ok" in out2, f"Продукт из my_products с граммовкой в имени должен добавиться: {out2}"
    finally:
        conn = connect()
        conn.execute("DELETE FROM pantry WHERE user_id=7806")
        conn.execute("DELETE FROM my_products WHERE user_id=7806")
        conn.commit()
        conn.close()
        _water_rows(7806)


def test_pantry_no_egg_grams_conversion_and_loose_merge():
    _water_user(7807)
    try:
        # Adding eggs with piece_weight_g=60 keeps unit='шт' and does not convert to 600g
        out = json.loads(registry.dispatch("pantry", {"action": "add", "name": "Яйцо куриное со упаковка", "qty": 10, "unit": "шт", "piece_weight_g": 60, "category": "Белковые", "user_id": 7807}))
        assert "ok" in out, out
        conn = connect()
        r = conn.execute("SELECT qty, unit, piece_g FROM pantry WHERE user_id=7807 AND (name LIKE '%Яйц%' OR name LIKE '%яйц%')").fetchone()
        assert r["qty"] == 10 and r["unit"] == "шт" and r["piece_g"] == 60, dict(r)

        # Adding same eggs with near name merges into existing item instead of creating duplicate
        out2 = json.loads(registry.dispatch("pantry", {"action": "add", "name": "Яйца куриные С0", "qty": 10, "unit": "шт", "piece_weight_g": 60, "category": "Белковые", "user_id": 7807}))
        assert "ok" in out2, out2
        rows = conn.execute("SELECT qty, unit FROM pantry WHERE user_id=7807 AND (name LIKE '%Яйц%' OR name LIKE '%яйц%')").fetchall()
        assert len(rows) == 1, f"Не должно быть дублирования строк: {rows}"
        assert rows[0]["qty"] == 20 and rows[0]["unit"] == "шт", dict(rows[0])
        conn.close()
    finally:
        conn = connect()
        conn.execute("DELETE FROM pantry WHERE user_id=7807")
        conn.commit()
        conn.close()
        _water_rows(7807)


def test_pantry_remove_unit_conversion():
    _water_user(7808)
    try:
        # Добавляем 1 кг куриного филе
        out = json.loads(registry.dispatch("pantry", {"action": "add", "name": "Куриное филе", "qty": 1.0, "unit": "кг", "category": "Белковые", "user_id": 7808}))
        assert "ok" in out, out

        # Списываем 200 г (не должно удалять позицию полностью из-за 200 >= 1.0)
        out2 = json.loads(registry.dispatch("pantry", {"action": "remove", "name": "Куриное филе", "qty": 200, "unit": "г", "user_id": 7808}))
        assert "ok" in out2, out2
        assert "осталось 0.8" in out2["ok"], out2

        conn = connect()
        r = conn.execute("SELECT qty, unit FROM pantry WHERE user_id=7808 AND name='Куриное филе'").fetchone()
        assert r is not None, "позиция не должна удаляться"
        assert abs(r["qty"] - 0.8) < 1e-4, f"должно остаться 0.8 кг, получено {r['qty']}"

        # Добавляем 10 яиц
        out3 = json.loads(registry.dispatch("pantry", {"action": "add", "name": "Яйца куриные С0", "qty": 10, "unit": "шт", "category": "Белковые", "user_id": 7808}))
        assert "ok" in out3, out3

        # Списываем 120 г яиц (120 г / 60 г = 2 шт)
        out4 = json.loads(registry.dispatch("pantry", {"action": "remove", "name": "Яйца куриные С0", "qty": 120, "unit": "г", "user_id": 7808}))
        assert "ok" in out4, out4
        assert "осталось 8" in out4["ok"], out4
        r_egg = conn.execute("SELECT qty, unit FROM pantry WHERE user_id=7808 AND name='Яйца куриные С0'").fetchone()
        assert r_egg is not None, "яйца не должны удаляться"
        assert abs(r_egg["qty"] - 8.0) < 1e-4, f"должно остаться 8 шт, получено {r_egg['qty']}"
        conn.close()
    finally:
        conn = connect()
        conn.execute("DELETE FROM pantry WHERE user_id=7808")
        conn.commit()
        conn.close()
        _water_rows(7808)

def test_console_weight_line_chart():
    """График веса в пульте: непрерывная линия с заливкой снизу (_line_chart)."""
    from plugin import tools

    assert tools._line_chart([]) == ""
    one = tools._line_chart([80.0])
    assert "80.0┤" in one and "─" in one

    flat = tools._line_chart([80.0, 80.0, 80.0])
    assert "80.0┤" in flat and "─" in flat

    chart = tools._line_chart([117.2, 116.6, 117.0, 114.9, 116.2])
    assert "117.2┤" in chart
    assert "114.9┤" in chart
    for line in chart.split("\n"):
        assert len(line) <= tools._CONSOLE_W, f"Строка графика длиннее {tools._CONSOLE_W}: {line!r}"
    long = tools._line_chart([116.9, 117.2, 116.6, 117.0, 116.8, 116.4, 116.4, 115.9, 115.8, 115.7, 115.1, 114.9, 116.1, 115.0])
    assert all(len(l) <= tools._CONSOLE_W for l in long.split("\n")), long
    assert long.split("\n")[-1].partition("┤")[2] == "█" * 16 + long.split("\n")[-1].partition("┤")[2][16:], long
    assert "█" in chart, "под линией должна быть заливка"
    bottom = chart.split("\n")[-1]
    assert " " not in bottom.partition("┤")[2], f"нижняя строка заливки не должна иметь разрывов: {bottom!r}"

    # Проверка вызова через get_day_summary(format='console')
    _water_user(7809)
    conn = connect()
    try:
        conn.execute("INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg) VALUES (7809, 'k1', '2026-10-01 08:00:00', 82.0)")
        conn.execute("INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg) VALUES (7809, 'k2', '2026-10-02 08:00:00', 80.5)")
        conn.commit()
        res = json.loads(registry.dispatch("get_day_summary", {"format": "console", "user_id": 7809}))
        summary = res["day_summary"]
        assert "② ВЕС 2 дн  82.0→80.5" in summary
        assert "82.0┤" in summary
        assert "80.5┤" in summary
        weight_section = summary.partition("② ВЕС")[2].partition("③")[0]
        assert "█" in weight_section, "заливка под линией в блоке веса"
    finally:
        conn.execute("DELETE FROM body_metrics WHERE user_id=7809")
        conn.execute("DELETE FROM daily_targets WHERE user_id=7809")
        conn.execute("DELETE FROM alerts WHERE user_id=7809")
        conn.execute("DELETE FROM users WHERE id=7809")
        conn.commit()
        conn.close()
        _water_rows(7809)


def test_weight_history_chart():
    """График за всё время: нужен минимум 2 дня, строки не шире пульта, подписаны даты."""
    from plugin import tools
    _water_user(7810)
    conn = connect()
    try:
        assert "хотя бы 2" in tools._render_weight_history(conn, 7810)
        for day, w in [(1, 82.0), (2, 81.0), (3, 80.5)]:
            conn.execute("INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg) VALUES (7810, ?, ?, ?)",
                         (f"k{day}", f"2026-09-0{day} 08:00:00", w))
        conn.commit()
        out = tools._render_weight_history(conn, 7810)
        assert "3 дн" in out and "01.09.26" in out and "03.09.26" in out and "мин 80.5" in out, out
        assert all(len(l) <= tools._CONSOLE_W for l in out.split("\n")), out
    finally:
        conn.execute("DELETE FROM body_metrics WHERE user_id=7810")
        conn.execute("DELETE FROM users WHERE id=7810")
        conn.commit()
        conn.close()
        _water_rows(7810)


def test_knowledge_normalized_topics_and_query():
    import unicodedata
    with tempfile.TemporaryDirectory() as tmp:
        kdir = Path(tmp)
        stem = unicodedata.normalize("NFD", "здоровый сон")
        (kdir / f"{stem}.md").write_text(
            "# Сон\n\nРегулярный сон снижает стресс.\n\nСтресс требует внимания.\n\nЕщё рекомендации про сон.\n\nЧужой нерелевантный абзац.",
            encoding="utf-8")
        old = knowledge.KNOWLEDGE_DIR
        knowledge.KNOWLEDGE_DIR = kdir
        try:
            assert "Регулярный" in knowledge.read("здоровый сон"), "NFC запрос не находит NFD имя"
            found = knowledge.read("здоровый сон", "ЕЩЕ")
            assert "Ещё рекомендации" in found, "ё/е и регистр должны нормализоваться"
            assert "Чужой нерелевантный абзац" not in found, "query игнорируется для малого файла"
            found = knowledge.read("здоровый сон", "стресс сон")
            assert "Регулярный сон снижает стресс" in found, "слова запроса не обязаны идти подряд"
            assert found.index("Регулярный сон снижает стресс") < found.index("Стресс требует внимания"), "полное покрытие слов должно быть выше частичного"
            assert "Источник:" in found and "Раздел: Сон" in found, "утерян контекст фрагмента"
        finally:
            knowledge.KNOWLEDGE_DIR = old


def test_knowledge_cross_topic_search_acl_and_live_updates():
    with tempfile.TemporaryDirectory() as tmp:
        kdir = Path(tmp)
        mine = kdir / "personal" / "900002"
        other = kdir / "personal" / "900003"
        mine.mkdir(parents=True)
        other.mkdir(parents=True)
        (kdir / "общая.md").write_text("Поиск: питание, общий источник.", encoding="utf-8")
        (mine / "заметки.md").write_text("Поиск: питание, мой источник.", encoding="utf-8")
        (other / "заметки.md").write_text("Поиск: питание, ЧУЖОЙ_СЕКРЕТ.", encoding="utf-8")
        (kdir / "admin_commands.md").write_text("Поиск: питание, ADMIN_СЕКРЕТ.", encoding="utf-8")
        old = knowledge.KNOWLEDGE_DIR
        knowledge.KNOWLEDGE_DIR = kdir
        registry.set_caller("900002")
        try:
            found = knowledge.read("*", "питание")
            assert "общий источник" in found and "мой источник" in found, "нет поиска по всем доступным темам"
            assert "ЧУЖОЙ_СЕКРЕТ" not in found and "ADMIN_СЕКРЕТ" not in found, "поиск расширил права чтения"
            assert "общий источник" in knowledge.read("", "питание"), "empty topic+query несовместим с общим поиском"
            (kdir / "новая.md").write_text("Питание: свежий документ.", encoding="utf-8")
            assert "свежий документ" in knowledge.read("*", "питание"), "новый документ требует рестарт"
            (kdir / "новая.md").write_text("Питание: обновлённый документ.", encoding="utf-8")
            refreshed = knowledge.read("*", "питание")
            assert "обновлённый документ" in refreshed and "свежий документ" not in refreshed
            assert "query" in knowledge.read("*"), "глобальное чтение без query не должно выгружать весь корпус"
        finally:
            knowledge.KNOWLEDGE_DIR = old
            registry.set_caller("111")


def test_knowledge_search_hard_byte_limit_and_late_match():
    with tempfile.TemporaryDirectory() as tmp:
        kdir = Path(tmp)
        text = ("Шум " * 1000) + "уникальныймаркер " + ("Шум " * 10000)
        (kdir / "большой.md").write_text(text, encoding="utf-8")
        old = knowledge.KNOWLEDGE_DIR
        knowledge.KNOWLEDGE_DIR = kdir
        try:
            found = knowledge.read("большой", "уникальныймаркер")
            assert "уникальныймаркер" in found, "обрезка скрыла само совпадение"
            assert len(found.encode("utf-8")) <= knowledge._SEARCH_LIMIT, "один большой абзац обошёл бюджет"
            assert "сокращены" in found, "усечение должно быть явно обозначено"
            assert "ничего не найдено" in knowledge.read("большой", "отсутствует")
        finally:
            knowledge.KNOWLEDGE_DIR = old


def test_knowledge_search_rejects_oversized_or_malformed_query():
    with tempfile.TemporaryDirectory() as tmp:
        kdir = Path(tmp)
        (kdir / "тема.md").write_text("Небольшой документ о питании.", encoding="utf-8")
        old = knowledge.KNOWLEDGE_DIR
        knowledge.KNOWLEDGE_DIR = kdir
        try:
            found = knowledge.read("*", "несуществующий" * 10000)
            assert len(found.encode("utf-8")) <= knowledge._SEARCH_LIMIT
            assert "слишком длинный" in found and "сократите" in found
            assert "строкой" in knowledge.read("*", ["питание"])
        finally:
            knowledge.KNOWLEDGE_DIR = old


def test_plateau_shared_evidence_and_uncertainty():
    """A repeated weigh-in is not repeated days; missing waist is not a diagnosis."""
    import sqlite3
    from datetime import datetime, timedelta
    from unittest.mock import patch
    from health_core import plateau, guards, council_data
    from health_core.db import migrate

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    now = datetime(2026, 11, 7, 12, 0, 0)
    conn.execute("INSERT INTO users(id,telegram_user_id,timezone,created_at) VALUES (1,1,'Asia/Yekaterinburg','2026-01-01 00:00:00')")

    def weight(days_ago, value=100.0, n=0, hour=8):
        d = now - timedelta(days=days_ago)
        conn.execute("INSERT INTO body_metrics(user_id,burst_key,measured_at,weight_kg) VALUES(1,?,?,?)",
                     (f"{days_ago}-{n}", d.strftime(f"%Y-%m-%d {hour:02d}:00:00"), value))

    with patch.object(plateau, "user_now", return_value=now):
        for n in range(8):
            weight(0, n=n)
        result = plateau.assess(conn, 1)
        assert result["status"] == "insufficient_data", result
        assert guards.check_plateau(conn, 1) is None
        conn.execute("DELETE FROM body_metrics")
        for day in (0, 27):
            weight(day)
            conn.execute("INSERT INTO anthropometry(user_id,measured_on,site,value_cm) VALUES(1,?,'талия',100)",
                         ((now - timedelta(days=day)).date().isoformat(),))
        result = plateau.assess(conn, 1)
        assert result["status"] == "insufficient_data", "два конца окна не доказывают плато: " + str(result)
        assert council_data.plateau_3w(conn, 1) is False
        conn.execute("DELETE FROM body_metrics")
        conn.execute("DELETE FROM anthropometry")
        for day in range(28):
            weight(day)
        result = plateau.assess(conn, 1)
        assert result["short_weight_pause"] is True
        assert result["cause"] == "unknown"
        assert result["sustained_weight_waist_pause"] is False
        assert council_data.plateau_3w(conn, 1) is False
        assert guards.check_plateau(conn, 1)["code"] == "PLATEAU"
        # Waist measurements need distinct dates and an actual long interval.
        for days_ago in (0, 21):
            conn.execute("INSERT INTO anthropometry(user_id,measured_on,site,value_cm) VALUES(1,?,'талия',100)",
                         ((now - timedelta(days=days_ago)).date().isoformat(),))
        assert plateau.assess(conn, 1)["status"] == "sustained_flat_weight"
        assert council_data.plateau_3w(conn, 1) is True
        conn.execute("UPDATE anthropometry SET value_cm=99 WHERE measured_on=?", (now.date().isoformat(),))
        result = plateau.assess(conn, 1)
        assert result["status"] == "waist_decreasing", result
        assert result["cause"] == "unknown"
        assert council_data.plateau_3w(conn, 1) is False
        # An evening/future spike cannot turn a morning pause into no plateau.
        weight(0, 110, n=1, hour=22)
        weight(-1, 120)
        assert plateau.assess(conn, 1)["status"] == "waist_decreasing"
        # Intentional maintenance days are not evidence of a pause in deficit.
        for day in range(28):
            conn.execute("INSERT INTO refeed_days(user_id,date) VALUES(1,?)",
                         ((now - timedelta(days=day)).date().isoformat(),))
        result = plateau.assess(conn, 1)
        assert result["status"] == "insufficient_data", result
        assert guards.check_plateau(conn, 1) is None
        # Равные медианы двух недель не отменяют возобновившегося снижения.
        conn.execute("DELETE FROM refeed_days")
        conn.execute("DELETE FROM body_metrics")
        conn.execute("UPDATE anthropometry SET value_cm=100")
        for day in range(28):
            weight(day, 99.3 + day * 0.2 if day <= 10 else 100.0)
        result = plateau.assess(conn, 1)
        assert result["status"] == "decreasing", result
        assert result["sustained_weight_waist_pause"] is False, result
        conn.execute("DELETE FROM body_metrics")
        for day in range(28):
            weight(day, 100.7 - day * 0.2 if day <= 10 else 100.0)
        result = plateau.assess(conn, 1)
        assert result["status"] == "increasing", result
        assert result["sustained_weight_waist_pause"] is False, result
        assert council_data.plateau_3w(conn, 1) is False
    conn.close()


def test_plateau_timeline_does_not_diagnose_individual():
    """Crossing a trial median cannot diagnose plateau in a falling time series."""
    import sqlite3
    from datetime import datetime, timedelta
    from unittest.mock import patch
    from health_core import plateau, forecast
    from health_core.db import migrate

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    now = datetime(2026, 11, 7, 12, 0, 0)
    conn.execute("INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,base_weight_kg,base_weight_date,created_at) "
                 "VALUES(1,1,180,'1990-01-01','m','Asia/Yekaterinburg',140,'2025-01-01','2025-01-01 00:00:00')")
    for day in range(28):
        d = now - timedelta(days=day)
        conn.execute("INSERT INTO body_metrics(user_id,burst_key,measured_at,weight_kg,ffm_kg,fat_pct) VALUES(1,?,?,?,75,30)",
                     (str(day), d.strftime("%Y-%m-%d 08:00:00"), 110 + day * 0.1))
    with patch.object(plateau, "user_now", return_value=now), patch.object(forecast, "user_now", return_value=now):
        result = forecast.plateau_forecast(conn, 1, intake_kcal=2200)
        assert result["current_stagnation"]["status"] == "decreasing", result
        assert result["pharmacological_plateau"]["phase"] == "популяционный ориентир"
        assert result["pharmacological_plateau"]["expected_date"] is None
        assert result["pharmacological_plateau"]["is_individual_prediction"] is False
        assert result["metabolic_equilibrium"]["intake_source"] == "scenario"
    conn.close()


def test_morning_report_does_not_fill_missing_days():
    """Вчерашний вес — исторический контекст, а не сегодняшний факт."""
    import sqlite3
    from datetime import datetime
    from unittest.mock import patch
    from health_core import report

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    conn.execute("INSERT INTO users(id,telegram_user_id,timezone,created_at) "
                 "VALUES(1,'1','Asia/Yekaterinburg','2026-01-01 00:00:00')")
    conn.execute("INSERT INTO body_metrics(user_id,burst_key,measured_at,weight_kg) "
                 "VALUES(1,'old','2026-11-06 07:00:00',100)")
    conn.execute("INSERT INTO sleep_log(user_id,night_date,duration_min) VALUES(1,'2026-11-06',430)")
    conn.execute("INSERT INTO daily_watch(user_id,date,steps,hr_min,created_at) "
                 "VALUES(1,'2026-11-05',9000,60,'2026-11-05 20:00:00')")
    conn.execute("INSERT INTO bp_log(user_id,at,systolic,diastolic) VALUES(1,'2026-11-06 07:00:00',120,80)")
    conn.execute("INSERT INTO glucose_log(user_id,at,mmol_l,context) VALUES(1,'2026-11-06 07:00:00',5,'fasting')")
    now = datetime(2026, 11, 7, 8)
    try:
        with patch.object(report, "_now", return_value=now), \
                patch.object(report, "check_all", return_value=[]), \
                patch("health_core.config.user_today", return_value="2026-11-07"), \
                patch("health_core.energy.daily_target", return_value={}):
            summary = report.day_summary(conn, 1, "2026-11-07")
            assert summary["weight_kg"] is None and summary["weight_measured_at"] is None
            assert summary["food_recorded"] is False and summary["water_recorded"] is False
            historical = report.day_summary(conn, 1, "2026-11-06")
            assert historical["weight_kg"] == 100
            assert report.day_summary(conn, 1, "2026-11-05")["weight_kg"] is None
            bar = report.status_bar(conn, 1)
            assert "Вес сегодня не записан" in bar.splitlines()[0], bar
            assert "100.0" not in bar.splitlines()[0], bar
            assert "Последний замер 06.11.2026 07:00: 100.0 кг" in bar, bar
            assert "Питание сегодня не записано" in bar and "Вода сегодня не записана" in bar
            checklist = report.morning_checklist(conn, 1)
            assert "✓" not in checklist, checklist
            assert "☐ Сон за ночь" in checklist and "☐ Шаги за вчера" in checklist
    finally:
        conn.close()


def test_morning_report_uses_only_available_current_measurements():
    """Записи после времени отчёта не закрывают чек-лист; даты — пользовательские."""
    import sqlite3
    from datetime import datetime
    from unittest.mock import patch
    from health_core import report

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    conn.execute("INSERT INTO users(id,telegram_user_id,timezone,created_at) "
                 "VALUES(1,'1','Asia/Yekaterinburg','2026-01-01 00:00:00')")
    conn.execute("INSERT INTO body_metrics(user_id,burst_key,measured_at,weight_kg) "
                 "VALUES(1,'future','2026-11-07 09:00:00',150)")
    conn.execute("INSERT INTO bp_log(user_id,at,systolic,diastolic) VALUES(1,'2026-11-07 09:00:00',140,90)")
    conn.execute("INSERT INTO glucose_log(user_id,at,mmol_l,context) VALUES(1,'2026-11-07 09:00:00',6,'fasting')")
    now = datetime(2026, 11, 7, 8)
    try:
        with patch.object(report, "user_now", return_value=now), \
                patch("health_core.config.user_now", return_value=now), \
                patch.object(report, "check_all", return_value=[]), \
                patch("health_core.energy.daily_target", return_value={}):
            bar = report.status_bar(conn, 1)
            assert "Вес сегодня не записан" in bar and "150.0" not in bar, bar
            checklist = report.morning_checklist(conn, 1)
            assert "✓" not in checklist, checklist
            conn.execute("INSERT INTO body_metrics(user_id,burst_key,measured_at,weight_kg) "
                         "VALUES(1,'current','2026-11-07 07:15:00',99)")
            conn.execute("INSERT INTO sleep_log(user_id,night_date,duration_min) VALUES(1,'2026-11-07',450)")
            conn.execute("INSERT INTO daily_watch(user_id,date,steps,created_at) "
                         "VALUES(1,'2026-11-06',7000,'2026-11-06 20:00:00')")
            bar = report.status_bar(conn, 1)
            assert "Вес 99.0 (утро, 07:15)" in bar and "150.0" not in bar, bar
            checklist = report.morning_checklist(conn, 1)
            assert "✓ Вес 99.0" in checklist and "✓ Сон 7 ч 30 мин" in checklist, checklist
            assert "✓ Шаги вчера 7000" in checklist, checklist
            assert "☐ Давление" in checklist and "☐ Глюкоза натощак" in checklist
    finally:
        conn.close()


def test_knowledge_symlinks_cannot_expand_access():
    with tempfile.TemporaryDirectory() as tmp:
        kdir = Path(tmp)
        (kdir / 'admin_commands.md').write_text('ADMIN_PRIVATE_MARKER справочник', encoding='utf-8')
        (kdir / 'публичная.md').symlink_to(kdir / 'admin_commands.md')
        personal = kdir / 'personal'
        personal.mkdir()
        other = personal / '900003'
        other.mkdir()
        (other / 'заметки.md').write_text('OTHER_PRIVATE_MARKER справочник', encoding='utf-8')
        (personal / '900002').symlink_to(other, target_is_directory=True)
        old = knowledge.KNOWLEDGE_DIR
        knowledge.KNOWLEDGE_DIR = kdir
        registry.set_caller('900002')
        try:
            assert 'ADMIN_PRIVATE_MARKER' not in knowledge.read('публичная')
            assert 'ADMIN_PRIVATE_MARKER' not in knowledge.read('*', 'справочник')
            assert 'OTHER_PRIVATE_MARKER' not in knowledge.read('заметки')
            assert 'OTHER_PRIVATE_MARKER' not in knowledge.read('*', 'справочник')
            assert 'ADMIN_PRIVATE_MARKER' not in knowledge.index()
            assert 'OTHER_PRIVATE_MARKER' not in knowledge.index()
        finally:
            knowledge.KNOWLEDGE_DIR = old
            registry.set_caller('111')


def test_report_food_water_share_measurement_cutoff():
    import sqlite3
    from datetime import datetime
    from unittest.mock import patch
    from health_core import report
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    migrate(conn)
    conn.execute("INSERT INTO users(id,telegram_user_id,timezone,created_at) VALUES(1,1,'Asia/Yekaterinburg','2026-01-01')")
    food_id = conn.execute("INSERT INTO food_log(user_id,eaten_at,meal_slot) VALUES(1,'2026-11-07 09:00:00','breakfast')").lastrowid
    conn.execute("INSERT INTO food_items(food_log_id,name,kcal) VALUES(?,'future meal',500)", (food_id,))
    conn.execute("INSERT INTO water_log(user_id,at,volume_ml) VALUES(1,'2026-11-07 09:00:00',700)")
    try:
        with patch.object(report, '_now', return_value=datetime(2026,11,7,8)), \
                patch.object(report, 'check_all', return_value=[]), \
                patch('health_core.energy.daily_target', return_value={}):
            d = report.day_summary(conn, 1, '2026-11-07')
            assert d['kcal_eaten'] == 0 and not d['food_recorded'], d
            assert d['water_ml'] == 0 and not d['water_recorded'], d
            bar = report.status_bar(conn, 1)
            assert 'Питание сегодня не записано' in bar and 'Вода сегодня не записана' in bar, bar
            assert 'Завтрак не записан' in bar, bar
            # За завершённый день все фактические записи доступны.
            conn.execute("UPDATE food_log SET eaten_at='2026-11-06 09:00:00'")
            conn.execute("UPDATE water_log SET at='2026-11-06 09:00:00'")
            d = report.day_summary(conn, 1, '2026-11-06')
            assert d['kcal_eaten'] == 500 and d['food_recorded'], d
            assert d['water_ml'] == 700 and d['water_recorded'], d
    finally:
        conn.close()


def test_tool_argument_validation_and_normalized_retry_key():
    def call(raw):
        return {'id':'review_call', 'type':'function', 'function':{'name':'help','arguments':raw}}
    for raw in ('[]', 'null', '42', '"text"', {'bad':'type'}, ['bad'], 7):
        dispatched = []
        result = asyncio.run(llm._run_one(call(raw), lambda n,a: dispatched.append(a) or '{}'))
        assert 'error' in json.loads(result), result
        assert not dispatched, raw
        assert llm._call_key(call(raw)) is None
    assert llm._call_key(call('{"user_id":0,"ml":100}')) == llm._call_key(call('{"ml":100}'))
    assert llm._call_key(call('{"user_id":false}')) != llm._call_key(call('{}'))


def test_knowledge_excerpt_preserves_substring_and_unicode_offsets():
    # casefold('ß') расширяет один символ до двух; срез должен оставаться
    # привязанным к исходному тексту, включая совпадение внутри слова.
    text = "ß" * 1800 + " сверхкалорийность " + "хвост " * 100
    excerpt, clipped = knowledge._excerpt(text, "калорий", {"калорий"}, 300)
    assert clipped and "калорий" in excerpt, excerpt
    assert len(excerpt.encode("utf-8")) <= 300


def test_tool_retry_uses_effective_arguments():
    """Нулевой необязательный ID не позволяет повторить тот же упавший запрос."""
    from unittest.mock import patch

    def call(call_id, args):
        return {"id": call_id, "type": "function", "function": {
            "name": "help", "arguments": json.dumps(args),
        }}

    dispatched = []
    def dispatch(name, args):
        dispatched.append(args)
        return json.dumps({"error": "temporarily unavailable"})

    responses = scripted(
        {"role": "assistant", "content": "", "tool_calls": [call("a", {"user_id": 0})]},
        {"role": "assistant", "content": "", "tool_calls": [call("b", {})]},
        {"role": "assistant", "content": "Не удалось прочитать данные."},
    )
    with patch.object(llm, "_post", responses):
        answer, messages = asyncio.run(llm.run_loop(
            None, [{"role": "user", "content": "покажи данные"}], [], PROVIDERS, dispatch))
    assert len(dispatched) == 1 and dispatched[0] == {}, dispatched
    assert "Не удалось" in answer
    assert len([m for m in messages if m["role"] == "tool"]) == 2


def test_plateau_calendar_windows_and_ambiguous_direction():
    import sqlite3
    from datetime import datetime, timedelta
    from unittest.mock import patch
    from health_core import plateau
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    migrate(conn)
    conn.execute("INSERT INTO users(id,telegram_user_id,timezone,created_at) VALUES(1,1,'UTC','2026-01-01')")
    now = datetime(2026,11,7,12)
    def weights(values):
        conn.execute('DELETE FROM body_metrics')
        for day, value in enumerate(values):
            conn.execute('INSERT INTO body_metrics(user_id,burst_key,measured_at,weight_kg) VALUES(1,?,?,?)',
                         (str(day), (now-timedelta(days=day)).strftime('%Y-%m-%d 08:00:00'), value))
    try:
        with patch.object(plateau, 'user_now', return_value=now):
            weights([100]*29)
            d = plateau.assess(conn,1)
            assert d['weight_days'] == d['window_days'], d
            assert d['recent_weight_days'] == 7, d
            # Пять точек: размах велик, но конечный вес равен начальному.
            conn.execute('DELETE FROM body_metrics')
            for day, value in ((0,100),(2,101),(4,99),(6,102),(9,100)):
                conn.execute('INSERT INTO body_metrics(user_id,burst_key,measured_at,weight_kg) VALUES(1,?,?,?)',
                             (str(day),(now-timedelta(days=day)).strftime('%Y-%m-%d 08:00:00'),value))
            d = plateau.assess(conn,1)
            assert d['status'] not in ('increasing','decreasing'), d
            assert not d['sustained_weight_waist_pause'], d
    finally:
        conn.close()


def test_lean_loss_uses_same_daily_evidence():
    import sqlite3
    from datetime import datetime, timedelta
    from unittest.mock import patch
    from health_core import body_composition as body, energy, guards, report
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    migrate(conn)
    conn.execute("INSERT INTO users(id,telegram_user_id,height_cm,birth_date,sex,timezone,created_at) VALUES(1,1,180,'1990-01-01','m','UTC','2026-01-01')")
    now = datetime(2026,11,7,12)
    def add(day, w, f, key, hour=8):
        conn.execute('INSERT INTO body_metrics(user_id,burst_key,measured_at,weight_kg,ffm_kg) VALUES(1,?,?,?,?)',
                     (key,(now-timedelta(days=day)).strftime(f'%Y-%m-%d {hour:02}:00:00'),w,f))
    try:
        with patch.object(body,'user_now',return_value=now), patch.object(energy,'user_now',return_value=now), \
                patch.object(guards,'_now',return_value=now), patch.object(report,'_now',return_value=now):
            for day in range(8):
                add(day,100+day,70+day*.2,str(day))
            expected = body.lean_loss_ratio(conn,1,None)[0]
            assert abs(expected-.2)<1e-8
            assert abs(energy.lean_share(conn,1)-expected)<1e-8
            assert abs(guards._lean_loss_ratio(conn,1,None)[0]-expected)<1e-8
            assert abs(report.trends(conn,1,28)['lean_loss']['ratio']-expected)<1e-8
            assert guards.check_lbm_ratio(conn,1)['code']=='LBM_RATIO'
            # Повторы одного дня, вечерние и будущие точки не меняют оценку.
            for n in range(20):
                add(0,100,70,f'repeat{n}')
            add(1,80,50,'evening',18)
            add(-1,80,50,'future')
            assert abs(energy.lean_share(conn,1)-expected)<1e-8
            conn.execute('UPDATE body_metrics SET ffm_kg=90-weight_kg*.1')
            assert energy.lean_share(conn,1)==0
            assert guards.check_lbm_ratio(conn,1) is None
            conn.execute('UPDATE body_metrics SET weight_kg=200-weight_kg')
            assert energy.lean_share(conn,1) is None
            with patch.object(guards,'check_all',return_value=[]):
                status = {r['code']:r for r in guards.get_guards_status(conn,1)}
                assert status['LBM_RATIO']['status']=='nodata'
            conn.execute('DELETE FROM body_metrics')
            for n in range(10):
                add(0,100+n,70+n*.2,str(n))
            assert body.lean_loss_ratio(conn,1,None) is None
            conn.execute("UPDATE body_metrics SET measured_at='2026-09-01 08:00:00'")
            assert body.lean_loss_ratio(conn,1,None) is None
    finally:
        conn.close()


def test_export_archive_contains_only_own_records():
    import sqlite3
    import zipfile
    from health_core.export import export_user_archive
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    migrate(conn)
    for uid, marker in ((1,'OWN_RECORD'),(2,'OTHER_PRIVATE_RECORD')):
        conn.execute('INSERT INTO users(id,telegram_user_id,created_at,health_notes) VALUES(?,?,?,?)',
                     (uid,str(uid),'2026-01-01',marker))
        fid=conn.execute('INSERT INTO food_log(user_id,eaten_at) VALUES(?,?)',(uid,'2026-11-07 07:00:00')).lastrowid
        conn.execute('INSERT INTO food_items(food_log_id,name,kcal) VALUES(?,?,100)',(fid,marker))
        conn.execute('INSERT INTO water_log(user_id,at,volume_ml) VALUES(?,?,100)',(uid,'2026-11-07 07:00:00'))
    try:
        before=conn.total_changes
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'export.zip'
            result=export_user_archive(conn,1,str(path))
            with zipfile.ZipFile(path) as z:
                assert z.testzip() is None
                assert {'users.csv','food_log.csv','food_items.csv','water_log.csv'}<=set(z.namelist())
                data='\n'.join(z.read(n).decode('utf-8-sig') for n in z.namelist())
                assert 'OWN_RECORD' in data and 'OTHER_PRIVATE_RECORD' not in data
                assert 'OWN_RECORD' in z.read('food_items.csv').decode('utf-8-sig')
                assert result['files']==len(z.namelist()) and result['rows']>=4
            assert conn.total_changes==before, 'экспорт не должен пересчитывать и записывать цели'
    finally:
        conn.close()


def test_export_delivery_success_failure_and_private_scope():
    import zipfile
    from types import SimpleNamespace
    from unittest.mock import patch
    prepared=[]
    paths=[]
    class Message:
        chat=SimpleNamespace(type='private',id=75001)
        fail=False
        def __init__(self): self.sent=[]; self.answers=[]
        async def answer_document(self, document, **kwargs):
            path=Path(document.path)
            assert path.is_file()
            paths.append(path)
            if self.fail: raise RuntimeError('test Telegram failure')
            with zipfile.ZipFile(path) as z: assert z.read('sample.csv')==b'value\n1\n'
            self.sent.append(document.filename)
        async def answer(self, text, **kwargs): self.answers.append(text)
    def prepare(uid,path):
        prepared.append(uid)
        with zipfile.ZipFile(path,'w') as z: z.writestr('sample.csv','value\n1\n')
        return {'path':path,'files':1,'rows':1}
    async def scenario():
        msg=Message()
        result=json.loads(await main._send_export(msg,'75001'))
        assert result['sent'] is True and msg.sent==['health-data.zip']
        assert not paths[-1].exists(), 'временный архив должен удаляться'
        # Инструмент не принимает другого адресата/user_id из аргументов модели.
        result=json.loads(await llm._run_one({'id':'export1','function':{'name':'export_data','arguments':'{\"user_id\":999}'}}, main._message_dispatch(msg,'75001')))
        assert result['sent'] is True and prepared[-1]=='75001'
        msg.fail=True
        result=json.loads(await main._send_export(msg,'75001'))
        assert 'error' in result and not result.get('sent')
        assert not paths[-1].exists()
        count=len(prepared)
        msg.chat=SimpleNamespace(type='group',id=-123)
        assert 'error' in json.loads(await main._send_export(msg,'75001'))
        assert len(prepared)==count
        msg.chat=SimpleNamespace(type='private',id=75001)
        await main._handle_turn(msg,None,{},'75001','/export')
        assert msg.answers and 'не отправлен' in msg.answers[-1]
    with patch.object(main,'_prepare_export',prepare):
        asyncio.run(scenario())


def test_database_location_is_project_relative():
    import subprocess
    env = dict(os.environ)
    env.pop('HEALTH_DB', None)
    env['PYTHONPATH'] = str(ROOT)
    code = 'from health_core.db import DB_PATH; print(DB_PATH)'
    with tempfile.TemporaryDirectory() as tmp:
        def resolved():
            return subprocess.check_output([sys.executable,'-c',code],cwd=tmp,env=env,text=True).strip()
        assert Path(resolved()) == ROOT / 'health.db'
        env['HEALTH_DB'] = 'test-data/health.db'
        assert Path(resolved()) == ROOT / 'test-data' / 'health.db'
        env['HEALTH_DB'] = str(Path(tmp)/'override.db')
        assert Path(resolved()) == Path(tmp)/'override.db'


def test_legacy_database_cannot_be_silently_abandoned():
    from unittest.mock import patch
    from health_core import db
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)/'project'/'health.db'
        legacy = Path(tmp)/'legacy.db'
        legacy.touch()
        with patch.object(db,'DB_PATH',target), patch.object(db,'DEFAULT_DB_PATH',target), \
                patch.object(db,'LEGACY_DB_PATH',legacy):
            try:
                db.connect()
            except RuntimeError as e:
                assert 'install.sh' in str(e)
            else:
                raise AssertionError('старая база не должна заменяться пустой')
            assert not target.exists()


def test_installer_preserves_database_and_copies_wal():
    import sqlite3
    from unittest.mock import patch
    installer = (ROOT/'install.sh').read_text()
    code = installer.split("<<'PYDB'\n",1)[1].split('\nPYDB',1)[0]
    with tempfile.TemporaryDirectory() as tmp:
        home=Path(tmp)/'home'
        legacy=home/'.hermes'/'health.db'
        legacy.parent.mkdir(parents=True)
        project=Path(tmp)/'project'
        project.mkdir()
        src=sqlite3.connect(legacy)
        try:
            src.execute('PRAGMA journal_mode=WAL')
            src.execute('CREATE TABLE example(value INTEGER)')
            src.execute('INSERT INTO example VALUES(42)')
            src.commit()
            assert Path(str(legacy)+'-wal').exists()
            def restore():
                with patch.object(Path,'home',return_value=home), \
                        patch.object(sys,'argv',['install.sh',str(project)]), \
                        patch.dict(os.environ,{},clear=True):
                    exec(compile(code,'install.sh:database','exec'),{})
            restore()
            dst=sqlite3.connect(project/'health.db')
            try:
                assert dst.execute('SELECT value FROM example').fetchone()[0]==42
                dst.execute('INSERT INTO example VALUES(99)')
                dst.commit()
                restore()
                assert dst.execute('SELECT COUNT(*) FROM example').fetchone()[0]==2
                assert src.execute('SELECT COUNT(*) FROM example').fetchone()[0]==1
            finally:
                dst.close()
        finally:
            src.close()


def test_history_json_archive_preserves_originals_before_compaction():
    from unittest.mock import patch
    from health_core import db
    with tempfile.TemporaryDirectory() as tmp, patch.object(db,'DB_PATH',Path(tmp)/'health.db'):
        conn=db.connect()
        migrate(conn)
        for i in range(15):
            history.append(conn,'123',{'role':'user','content':f'Вопрос {i}'})
            history.append(conn,'123',{'role':'assistant','content':f'Ответ {i}'})
        history.append(conn,'123',{'role':'tool','tool_call_id':'source','content':'полный результат '*500})
        history.append(conn,'456',{'role':'user','content':'ЧУЖОЙ_ДИАЛОГ'})
        original=conn.execute("SELECT count(*) FROM chat_history WHERE telegram_user_id='123'").fetchone()[0]
        conn.close()
        async def summarize(*args,**kwargs):
            files=list((Path(tmp)/'history_archive'/'123').glob('*.json'))
            assert len(files)==1, 'JSON должен существовать ДО вызова модели'
            data=json.loads(files[0].read_text())
            assert len(data['messages'])==original
            assert data['messages'][-1]['message']['content']=='полный результат '*500
            assert 'ЧУЖОЙ_ДИАЛОГ' not in files[0].read_text()
            assert all('timestamp' in m and 'rowid' in m for m in data['messages'])
            return {'content':'Сводка'}
        with patch.object(llm,'chat',summarize):
            asyncio.run(main._compact_history(None,'123',PROVIDERS))
        files=list((Path(tmp)/'history_archive'/'123').glob('*.json'))
        assert len(files)==1, 'повторная защита перед удалением не должна дублировать снимок'
        conn=db.connect()
        try:
            assert conn.execute("SELECT count(*) FROM chat_history WHERE telegram_user_id='123'").fetchone()[0]<original
            assert len(history.load(conn,'456'))==1
            previous=files[0].read_bytes()
            history.append(conn,'123',{'role':'user','content':'Новый вопрос'})
            second=history.archive(conn,'123')
            assert second!=files[0] and files[0].read_bytes()==previous
        finally:
            conn.close()


def test_history_archive_failure_and_summary_insert_failure_preserve_messages():
    import sqlite3
    from unittest.mock import patch, AsyncMock
    from health_core import db
    with tempfile.TemporaryDirectory() as tmp, patch.object(db,'DB_PATH',Path(tmp)/'health.db'):
        conn=db.connect()
        migrate(conn)
        for i in range(30):
            history.append(conn,'123',{'role':'user','content':str(i)})
        before=conn.execute('SELECT rowid,content FROM chat_history ORDER BY rowid').fetchall()
        model=AsyncMock(return_value={'content':'summary'})
        with patch.object(history.os,'replace',side_effect=OSError('disk failure')), patch.object(llm,'chat',model):
            try:
                asyncio.run(main._compact_history(None,'123',PROVIDERS))
            except OSError:
                pass
            else:
                raise AssertionError('сжатие должно прерваться при ошибке архива')
        model.assert_not_awaited()
        assert [tuple(r) for r in conn.execute('SELECT rowid,content FROM chat_history ORDER BY rowid')]==[tuple(r) for r in before]
        assert not list((Path(tmp)/'history_archive').rglob('*.tmp'))
        conn.execute("CREATE TRIGGER fail_summary BEFORE INSERT ON chat_history BEGIN SELECT RAISE(ABORT,'insert failure'); END")
        conn.commit()
        try:
            history.replace(conn,'123',before[9]['rowid'],'summary')
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError('ожидался сбой вставки сводки')
        assert [tuple(r) for r in conn.execute('SELECT rowid,content FROM chat_history ORDER BY rowid')]==[tuple(r) for r in before]
        conn.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            check(name[5:], fn)
    print(f"\n{len(PASSED)} прошло, {len(FAILED)} упало")
    for name, err in FAILED:
        print(f"  {name}: {err}")
    sys.exit(1 if FAILED else 0)
