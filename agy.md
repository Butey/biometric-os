# Заметки от Antigravity (Gemini) для Claude

## Сессия 2026-09-16

Claude начал, Antigravity доделал и закоммитил.

### Что было сделано Claude

Четыре связанных изменения, все незакоммичены:

1. **Streak-порог TDEE** (`health_core/energy.py`)
   - Старый порог «11 любых дней из 14» заменён на непрерывную серию последних дней.
   - Новая функция `logged_streak(conn, user_id, end)` ищет самую свежую непрерывную серию залогированных дней (не старше `_MAX_STREAK_STALE_DAYS=2` от end, lookback до 120 дней).
   - Константа `MIN_LOG_STREAK_DAYS = 7` — минимальная длина серии.
   - `adaptive_tdee()` теперь сужает окно до серии вместо отбрасывания при неполном заполнении.

2. **Защита от копирования пультов** (`bot/main.py`)
   - Функция `strip_panels()` — regex ```` ```...``` ```` заменяется заглушкой перед записью в историю.
   - Вызывается в `_close_turn()` для `role=assistant`.
   - Проблема: модель копировала из истории старый пульт (с устаревшим временем и нулевой клетчаткой) вместо вызова `get_day_summary`.

3. **Баг fiber_g=0** (`plugin/tools.py`)
   - `if per100.get("fiber_g") is not None and i.get("fiber_g") is None` -> `... and not i.get("fiber_g")`.
   - Модель шлёт `fiber_g=0` рядом с `per_100g.fiber_g=5`; старое `is None` пропускало ноль, клетчатка дня выходила 0.

4. **Синхронизация forecast.py** (`health_core/forecast.py`)
   - `_observed_intake()` переведён на `logged_streak()` + `MIN_LOG_STREAK_DAYS`.
   - Сообщение об ошибке в `project()` теперь ссылается на `MIN_LOG_STREAK_DAYS`.

5. **Тесты** (`test_bot.py`)
   - `test_food_lookup_remember_match_and_log_food_per_100g` — кейс fiber_g=0 из per_100g.
   - `test_panels_not_kept_in_history` — проверка strip_panels.

### Что доделал Antigravity

Claude оставил 4 устаревших комментария/docstrings, ссылавшихся на старый порог «11/14»:

- `energy.py:286-287` — docstring `_adaptive_tdee_core()`: «11/14 у adaptive_tdee» -> «MIN_LOG_STREAK_DAYS подряд у adaptive_tdee»
- `energy.py:517` — docstring `deadline_verdict()`: «меньше 11 дней из 14» -> «меньше MIN_LOG_STREAK_DAYS дней подряд»
- `energy.py:1137` — комментарий в self-test: «меньше 11/14 дней» -> «меньше MIN_LOG_STREAK_DAYS дней подряд»
- `config.yaml:84` — комментарий blend_min_logged_days: «порога adaptive_tdee 11/14» -> «порога adaptive_tdee MIN_LOG_STREAK_DAYS подряд»

### Результат

- 39/39 unit-тестов пройдено
- 10/10 e2e-стадий пройдено
- Закоммичено одним коммитом
