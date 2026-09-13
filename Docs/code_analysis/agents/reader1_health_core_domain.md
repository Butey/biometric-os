# Глубокий анализ доменного ядра `health_core`

Документ содержит полный детальный анализ всех 11 модулей доменного ядра системы мониторинга здоровья и управления составом тела `health_core`. 
Анализ выполнен построчно на основе исходных кодов репозитория по состоянию на сентябрь 2026 года.

---

## Оглавление
1. [health_core/energy.py](#1-health_coreenergypy)
2. [health_core/nutrition.py](#2-health_corenutritionpy)
3. [health_core/guards.py](#3-health_coreguardspy)
4. [health_core/report.py](#4-health_corereportpy)
5. [health_core/forecast.py](#5-health_coreforecastpy)
6. [health_core/refeed.py](#6-health_corerefeedpy)
7. [health_core/config.py](#7-health_coreconfigpy)
8. [health_core/models.py](#8-health_coremodelspy)
9. [health_core/db.py](#9-health_coredbpy)
10. [health_core/meds.py](#10-health_coremedspy)
11. [health_core/plans.py](#11-health_coreplanspy)
12. [Сводный реестр выявленных дефектов, рисков и edge cases](#12-сводный-реестр-выявленных-дефектов-рисков-и-edge-cases)

---

## 1. health_core/energy.py

### Назначение модуля
Модуль реализует математическое ядро расчета энергобаланса (§09 спецификации): формулы базового метаболизма (BMR), адаптивный расчет суточного расхода (TDEE) на основе пищевого дневника и динамики веса, расчет безопасного пола калоража (модель Alpert), учет тренировочных энергозатрат со скидками на погрешность приборов, суточный дефицит под сроки вех (milestones), недельное распределение и рефиды.

### Сигнатуры функций и docstring
1. `bmr_katch(ffm_kg: float) -> float`
   - *Docstring:* Отсутствует (формула Кэтча-МакАрдла).
2. `bmr_mifflin(weight_kg: float, height_cm: float, age_years: int, sex: str = "m") -> float`
   - *Docstring:* Отсутствует (формула Миффлина-Сан Жеора).
3. `_age_years(birth_date: str, on_date: str) -> int`
   - *Docstring:* `"""Наивная разница лет (год_замера - год_рождения), без учёта месяца/дня..."""`
4. `_latest_metric(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None`
   - *Docstring:* Отсутствует.
5. `bmr_floor(conn: sqlite3.Connection, user_id: int) -> float`
   - *Docstring:* `"""BMR_floor = max(Mifflin, Katch) — затравка §09, берётся по максимуму..."""`
6. `fat_mass_kg(conn: sqlite3.Connection, user_id: int) -> float | None`
   - *Docstring:* `"""Жировая масса из последнего замера с процентом жира..."""`
7. `macro_minimum_kcal(conn: sqlite3.Connection, user_id: int) -> float | None`
   - *Docstring:* `"""Сколько калорий физически занимают обязательные белок и жир из целей..."""`
8. `kcal_floor(conn: sqlite3.Connection, user_id: int, full_tdee: float) -> tuple[float, str]`
   - *Docstring:* `"""Нижняя граница калоража. Возвращает (ккал, причина)..."""`
9. `ffmi(ffm_kg: float, height_cm: float) -> float`
   - *Docstring:* Отсутствует.
10. `_morning_weights(conn: sqlite3.Connection, user_id: int, start: str, end: str) -> list[float]`
    - *Docstring:* Отсутствует.
11. `adaptive_tdee(conn: sqlite3.Connection, user_id: int, window_days: int = 14) -> float | None`
    - *Docstring:* `"""TDEE_факт = средний_intake_Nд + (Δвес_Nд · 7700 / N)..."""`
12. `_tcx_net(conn: sqlite3.Connection, user_id: int, date_: str) -> float`
    - *Docstring:* `"""Тренировочная добавка: Σ(калории лапа) × 0.75, и ещё × 0.9 без пульса."""`
13. `_target_kcal(base: float, tcx_net: float, floor: float) -> float`
    - *Docstring:* `"""Гардрейл шага 3: цель никогда не опускается ниже BMR_floor."""`
14. `_active_milestone(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None`
    - *Docstring:* `"""§09 шаг 2: "ближайшая недостигнутая веха, у которой проставлен срок"..."""`
15. `_deadline_deficit(conn: sqlite3.Connection, user_id: int, milestone: sqlite3.Row, today: str) -> float | None`
    - *Docstring:* `"""§09 шаг 2: срок до вехи -> требуемый дневной дефицит в ккал..."""`
16. `_is_refeed(conn: sqlite3.Connection, user_id: int, date_: str) -> bool`
    - *Docstring:* `"""§09 шаг 5: рефид активен на эту дату?..."""`
17. `_weekday_multiplier(date_: str) -> float`
    - *Docstring:* `"""§09 шаг 4: "распределение по дням недели, недельная сумма неизменна"..."""`
18. `daily_target(conn: sqlite3.Connection, user_id: int, date: str) -> dict`
    - *Docstring:* Отсутствует (основной 5-шаговый пайплайн).

### Алгоритмы и формулы
1. **BMR Katch-McArdle:**
   $$\text{BMR}_{\text{katch}} = 370 + 21.6 \times \text{FFM}_{\text{kg}}$$
2. **BMR Mifflin-St Jeor:**
   $$\text{BMR}_{\text{mifflin}} = 10 \times \text{Weight}_{\text{kg}} + 6.25 \times \text{Height}_{\text{cm}} - 5 \times \text{Age} + (5 \text{ if male else } -161)$$
3. **Затравка BMR floor:**
   $$\text{BMR}_{\text{floor}} = \max(\text{BMR}_{\text{katch}}, \text{BMR}_{\text{mifflin}})$$
   При отсутствии FFM в последнем замере вызывается `latest_ffm(conn, user_id)`. Если данных о FFM нет вообще, берется `weight_kg`.
4. **Физиологический пол калоража (модель Alpert):**
   $$\text{SafeDeficit} = \text{FatMass}_{\text{kg}} \times \text{fat\_supply\_kcal\_per\_kg} \times \text{fat\_supply\_safety} = \text{FatMass}_{\text{kg}} \times 31 \times 0.7$$
   $$\text{Floor}_{\text{fat}} = \text{TDEE}_{\text{full}} - \text{SafeDeficit}$$
   Нижняя граница подпирается макро-минимумом:
   $$\text{Floor}_{\text{macro}} = \frac{\text{Protein}_{\text{g}} \times 4}{1 - \text{fat\_pct\_min}}$$
   $$\text{Floor} = \max(\text{Floor}_{\text{fat}}, \text{Floor}_{\text{macro}})$$
   Если биоимпеданс отсутствует (`fat_mass_kg is None`), возврат к `BMR_floor`.
5. **Адаптивный TDEE:**
   $$\text{TDEE}_{\text{adaptive}} = \overline{\text{Intake}}_{14\text{d}} + \frac{\Delta\text{Weight}_{14\text{d}} \times 7700}{14}$$
   Критерий валидности: логирование еды минимум $11$ из $14$ дней (формула: `-(-window_days * 11 // 14)`), а также $\ge 2$ утренних замеров веса (строго с 06:00 до 11:00).
6. **Тренировочные калории (TCX net):**
   $$\text{TCX}_{\text{net}} = \sum (\text{kcal} \times \text{discount} \times (\text{no\_hr if avg\_hr is None else } 1.0))$$
   По умолчанию: $\text{discount} = 0.75$, $\text{no\_hr} = 0.90$.
7. **Дефицит под веху:**
   $$\text{Deficit} = \frac{(\text{CurrentWeight} - \text{MilestoneThreshold}) \times 7700}{\text{DaysRemaining}}$$
   Если $\text{DaysRemaining} \le 0 \implies \infty$ (недостижим). Если вес уже достигнут $\implies 0.0$.
8. **Пятишаговый пайплайн `daily_target()`:**
   - Шаг 1: $\text{Base} = \text{TDEE}_{\text{adaptive}}$ (если откалиброван) иначе $\text{BMR}_{\text{floor}} \times \text{activity\_factor}$ (1.20). $\text{TDEE}_{\text{full}} = \text{Base} + \text{TCX}_{\text{net}}$.
   - Шаг 2: $\text{Deficit} = \text{MilestoneDeficit}$ (или $\text{default\_deficit\_kcal}$). $\text{Target}_{\text{s2}} = \text{TDEE}_{\text{full}} - \text{Deficit}$.
   - Шаг 3: Клампа: $\text{Target}_{\text{s3}} = \max(\text{Target}_{\text{s2}}, \text{Floor})$. Если $\text{Target}_{\text{s2}} < \text{Floor} \implies \text{deadline\_unreachable} = \text{True}$.
   - Шаг 4: Распределение по дням недели: $\text{Target}_{\text{s4}} = \text{Target}_{\text{s3}} \times \text{Multiplier}_{\text{weekday}}$.
   - Повторная клампа (Safety invariant): $\text{Kcal} = \max(\text{Target}_{\text{s4}}, \text{Floor})$.
   - Шаг 5: Рефид: если день отмечен в `refeed_days`, шаги 2-4 отключаются: $\text{Kcal} = \max(\text{TDEE}_{\text{full}}, \text{Floor})$.
   - Макронутриенты:
     $$\text{Fat}_{\text{g}} = \text{round}\left(\frac{\text{Kcal} \times \text{fat\_pct}}{9}, 1\right)$$
     $$\text{Carbs}_{\text{g}} = \text{round}\left(\frac{\max(0, \text{Kcal} - \text{Protein}_{\text{g}} \times 4 - \text{Fat}_{\text{g}} \times 9)}{4}, 1\right)$$
   - Сохранение в `daily_targets` через `ON CONFLICT(user_id, date) DO UPDATE`.

### SQL-запросы
- `SELECT weight_kg, ffm_kg, measured_at FROM body_metrics WHERE user_id=? ORDER BY measured_at DESC LIMIT 1`
- `SELECT height_cm, birth_date, sex FROM users WHERE id=?`
- `SELECT weight_kg, fat_pct FROM body_metrics WHERE user_id=? AND fat_pct IS NOT NULL AND fat_pct > 0 ORDER BY measured_at DESC LIMIT 1`
- `SELECT weight_kg FROM body_metrics WHERE user_id=? AND date(measured_at) BETWEEN ? AND ? AND time(measured_at) BETWEEN '06:00:00' AND '11:00:00' ORDER BY measured_at`
- `SELECT COUNT(DISTINCT date(eaten_at)) c FROM food_log WHERE user_id=? AND date(eaten_at) BETWEEN ? AND ?`
- `SELECT SUM(fi.kcal) kcal FROM food_log fl JOIN food_items fi ON fi.food_log_id = fl.id WHERE fl.user_id=? AND date(fl.eaten_at) BETWEEN ? AND ? GROUP BY date(fl.eaten_at)`
- `SELECT kcal, avg_hr FROM activity WHERE user_id=? AND date(started_at)=?`
- `SELECT id, name, metric, threshold, deadline FROM milestones WHERE user_id=? AND achieved_at IS NULL AND deadline IS NOT NULL ORDER BY deadline ASC, id ASC LIMIT 1`
- `SELECT 1 FROM refeed_days WHERE user_id=? AND date=?`
- `INSERT INTO daily_targets(user_id, date, kcal_target, protein_g_target, fat_g_target, carbs_g_target, fiber_g_target, water_ml_target, computed_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, date) DO UPDATE SET ...`

### Константы и пороговые значения
- `_KCAL_PER_KG = 7700` (энергетическая стоимость 1 кг массы тела при похудении).
- `fat_supply_kcal_per_kg = 31` ккал/сут на 1 кг жира.
- `fat_supply_safety = 0.7` (коэффициент консервативного запаса).
- `fat_pct_min = 0.20` (нижняя граница AMDR для жиров).
- `tcx_discount_factor = 0.75`, `no_hr_discount = 0.9`.
- Окно взвешиваний для тренда: с `06:00:00` до `11:00:00`.
- Окно калибровки TDEE: 14 дней, минимум 11 залогированных дней.

### Потенциальные проблемы и дефекты
1. **Жесткая привязка к системному времени в `adaptive_tdee`:** Функция `adaptive_tdee` использует `end = local_now().date()`. Если вызывается `daily_target(conn, user_id, "2026-08-01")` для ретроспективной даты или на дату вперед, `adaptive_tdee` все равно рассчитывается относительно сегодняшнего дня сервера, а не переданного `date`.
2. **Наивный расчет возраста `_age_years`:** Разница берется только по годам (`date.year - birth.year`). Для человека, родившегося в декабре, в январе возраст завышается на 1 год, что занижает Mifflin BMR на 5 ккал. В коде указано, что это сделано ради воспроизведения таблицы §09, но для реальных пользователей это источник погрешности.
3. **Неконтролируемый `conn.commit()` внутри `daily_target()`:** Функция неявно коммитит внешнюю транзакцию, разрушая атомарность вызывающего кода.
4. **Обнуление углеводов:** Если калораж мал или белок высок, углеводы клампятся нулем (`max(0.0, ...)`), что скрывает дефицит бюджета энергии под целевой белок и жиры.

---

## 2. health_core/nutrition.py

### Назначение модуля
Модуль рассчитывает фактические суточные макронутриенты из пищевого дневника (`day_macros`), анализирует распределение категорий тарелки (`plate_balance`) и вычисляет индивидуальную норму белка (`protein_target`).

### Сигнатуры функций и docstring
1. `day_macros(conn: sqlite3.Connection, user_id: int, date: str) -> dict`
   - *Docstring:* Отсутствует. Возвращает агрегаты `kcal`, `protein_g`, `fat_g`, `carb_g`, `fiber_g`, `fiber_n`.
2. `plate_balance(conn: sqlite3.Connection, user_id: int, date: str) -> dict`
   - *Docstring:* `"""Доля каждой категории тарелки (food_items.plate_category) в дне..."""`
3. `protein_target(conn: sqlite3.Connection, user_id: int) -> float`
   - *Docstring:* `"""protein_g_per_kg_ffm (config.yaml, уровень «Политика») × текущий FFM пользователя."""`

### Алгоритмы и формулы
1. **Агрегация макронутриентов дня:**
   $$\text{Macro}_{\text{total}} = \sum \text{food\_items.macro} \quad \forall \text{ items on date}$$
2. **Баланс тарелки:**
   $$\text{Share}_{\text{category}} = \frac{\sum \text{kcal}_{\text{category}}}{\sum \text{kcal}_{\text{all categories}}}$$
   Целевые доли `target_share` установлены в `None`, так как нормативная модель в спецификации не формализована.
3. **Целевой белок:**
   $$\text{ProteinTarget} = \text{FFM}_{\text{kg}} \times \text{protein\_g\_per\_kg\_ffm}$$

### SQL-запросы
- `SELECT SUM(fi.kcal) kcal, SUM(fi.protein_g) protein_g, SUM(fi.fat_g) fat_g, SUM(fi.carbs_g) carb_g, SUM(fi.fiber_g) fiber_g, COUNT(fi.fiber_g) fiber_n FROM food_log fl JOIN food_items fi ON fi.food_log_id = fl.id WHERE fl.user_id=? AND date(fl.eaten_at)=?`
- `SELECT fi.plate_category cat, SUM(fi.kcal) kcal FROM food_log fl JOIN food_items fi ON fi.food_log_id = fl.id WHERE fl.user_id=? AND date(fl.eaten_at)=? AND fi.plate_category IS NOT NULL GROUP BY fi.plate_category`

### Константы и пороговые значения
- Зависят от `targets_for`: `protein_g_per_kg_ffm = 1.8` г/кг FFM.

### Потенциальные проблемы и дефекты
1. **Дублирование логики с `report._day_macros`:** В `report.py` написан практически идентичный запрос, но с `COALESCE(SUM(...), 0)` и `COUNT(*) n`. Несогласованность двух агрегаторов повышает риск рассинхронизации при добавлении новых полей.
2. **NULL в агрегатах SQL:** Если записей нет, SQL возвращает строку со значениями `None`. В коде используется `row["kcal"] or 0.0`. Это работает, но если калорийность блюда отрицательная (ошибочный ввод), `or 0.0` не сработает, хотя для `None` значение заменится на ноль.

---

## 3. health_core/guards.py

### Назначение модуля
Модуль реализует 14 детерминированных клинических гардрейлов (§10), диспетчер проверок `check_all()`, сохранение алертов `record()` и статусную модель `get_guards_status()` для дашборда и пульта управления.

### Сигнатуры функций и docstring
1. `_cfg() -> dict`
2. `_now() -> datetime`
3. `_parse(ts: str) -> datetime`
4. `_alert(code: str, severity: str, message: str, value, threshold) -> dict`
5. `_lean_loss_ratio(conn: sqlite3.Connection, user_id: int, since: str | None)`
   - *Docstring:* `"""(ratio, d_weight, d_lean, n_точек) от медианы начала до медианы конца окна..."""`
6. `check_lbm_ratio(conn: sqlite3.Connection, user_id: int)`
7. `check_lbm_drift(conn: sqlite3.Connection, user_id: int)`
8. `check_ffmi_floor(conn: sqlite3.Connection, user_id: int)`
9. `check_undereating(conn: sqlite3.Connection, user_id: int)`
10. `check_plateau(conn: sqlite3.Connection, user_id: int)`
11. `check_bmr_floor(conn: sqlite3.Connection, user_id: int)`
12. `_gap_days(ts: str) -> int`
    - *Docstring:* `"""Разрыв в КАЛЕНДАРНЫХ днях, а не в полных сутках..."""`
13. `check_no_measure(conn: sqlite3.Connection, user_id: int)`
14. `check_lipid_guard(conn: sqlite3.Connection, user_id: int)`
15. `check_whr(conn: sqlite3.Connection, user_id: int)`
16. `check_rate_high(conn: sqlite3.Connection, user_id: int)`
17. `check_stale_calib(conn: sqlite3.Connection, user_id: int)`
18. `_protein_target(conn: sqlite3.Connection, user_id: int, today: str) -> float | None`
19. `check_protein_skew(conn: sqlite3.Connection, user_id: int)`
20. `check_glucose_volatility(conn: sqlite3.Connection, user_id: int)`
21. `check_measure_soon(conn: sqlite3.Connection, user_id: int)`
22. `check_all(conn: sqlite3.Connection, user_id: int) -> list[dict]`
23. `record(conn: sqlite3.Connection, user_id: int, alerts: list[dict]) -> None`
24. `get_guards_status(conn: sqlite3.Connection, user_id: int) -> list[dict]`

### Алгоритмы, формулы и 14 гардрейлов
1. **LBM_RATIO (Критический):**
   - Доля потери FFM в потере общего веса за 14 дней.
   - Алгоритм: берется срез `body_metrics` с FFM за 14 дней. Чтобы устранить шум гидратации биоимпеданса, вычисляется медиана первых $k$ и последних $k$ точек ($k=3$ при $N \ge 6$, $k=2$ при $N \in [4, 5]$, $k=1$ при $N \in [2, 3]$).
   - $\text{ratio} = |\Delta\text{FFM}_{\text{median}}| / |\Delta\text{Weight}_{\text{median}}|$.
   - Срабатывает, если: $N \ge \text{lbm\_ratio\_min\_points}$ (4), $|\Delta\text{FFM}| \ge \text{ffm\_noise\_floor\_kg}$ (0.5 кг) и $\text{ratio} > \text{lbm\_ratio\_threshold}$ (0.15 = 15%).
2. **LBM_DRIFT (Критический):**
   - Накопительный дрейф тощей массы за всю историю (`since=None`).
   - Формула аналогична, порог $\text{lbm\_drift\_threshold} = 0.20$ (20%). При $N < 4$ генерирует информационный статус `INSUFFICIENT`.
3. **FFMI_FLOOR (Критический):**
   - $\text{FFMI} = \frac{\text{FFM}_{\text{kg}}}{(\text{Height}_{\text{m}})^2}$.
   - Фильтр замера: строго до 11:00 утра (`CAST(strftime('%H', measured_at) AS INTEGER) < 11`).
   - Срабатывает, если $\text{FFMI} < \text{ffmi\_floor}$ (19.0 кг/м²).
4. **UNDEREATING (Предупреждение):**
   - Проверяет $N$ дней подряд (по умолчанию 3 дня).
   - Для каждого дня: в дневнике есть хотя бы 1 запись (`food_log.n > 0`), и $\text{Kcal}_{\text{fact}} < \text{undereating\_ratio} \times \text{Kcal}_{\text{target}}$ ($0.80 \times \text{Target}$).
   - При отсутствии записей хотя бы за один день молчит (не путает пропуск логирования с голоданием).
5. **PLATEAU (Предупреждение):**
   - Размах веса $\max(W) - \min(W) \le \text{plateau\_range\_kg}$ (0.5 кг) за $\text{plateau\_window\_days}$ (10 дней) при числе точек $\ge \text{plateau\_min\_points}$ (5 точек).
6. **BMR_FLOOR (Критический):**
   - Вторая линия защиты: проверяет, чтобы установленный в `daily_targets.kcal_target` калораж не оказался ниже `kcal_floor` из `energy.daily_target()`.
7. **NO_MEASURE (Предупреждение):**
   - Проверяет календарный разрыв: $\text{gap} = (\text{now}.\text{date}() - \text{last\_measured}.\text{date}()).\text{days} \ge \text{no\_measure\_days}$ (7 дней).
8. **LIPID_GUARD (Предупреждение):**
   - Оральные препараты (`route='oral'`) из `med_log` на текущую дату.
   - Ищет приемы пищи в окне $\pm 3$ часа от момента приема препарата:
     $$\text{window\_fat} = \sum \text{fat\_g} \quad \text{for } \text{eaten\_at} \in [\text{at} - 3\text{h}, \text{at} + 3\text{h}]$$
   - Срабатывает, если в окне была еда (`n > 0`), но $\text{window\_fat} < \text{lipid\_guard\_fat\_g}$ (10 г).
9. **WHR_HIGH (Предупреждение):**
   - Индекс висцерального ожирения: $\text{WHR} = \text{талия} / \text{таз}$.
   - Порог: $0.90$ для мужчин, $0.85$ для женщин.
10. **RATE_HIGH (Предупреждение):**
    - Темп снижения веса в окне 14 дней (минимум 4 замера):
      $$\text{Rate}_{\%/\text{week}} = \frac{|\Delta\text{Weight}|}{\text{Weight}_{\text{first}}} \times 100 \times \frac{7}{\text{DaysElapsed}}$$
    - Срабатывает только при снижении веса ($\Delta\text{Weight} < 0$) и превышении порога $\text{weight\_rate\_pct\_week\_max} = 1.0\%$ в неделю.
11. **STALE_CALIB (Предупреждение):**
    - В окне 14 дней логирования еды меньше $\text{stale\_calib\_min\_days} = 11$ уникальных дней. Оценивается только для пользователей, зарегистрированных $\ge 14$ дней назад.
12. **PROTEIN_SKEW (Предупреждение):**
    - Перекос распределения белка по приемам (`meal_slot`).
    - Требования к валидности: число приемов пищи за день $\ge 3$, суммарный белок за день $\ge 60$ г.
    - База расчета: $\text{basis} = \max(\text{ProteinTarget}, \text{ProteinDayTotal})$.
    - Доля крупнейшего приема: $\text{share} = \text{TopMealProtein} / \text{basis}$.
    - Срабатывает, если $\text{share} > \text{protein\_skew\_threshold}$ (0.50 = 50%).
13. **GLUCOSE_VOLATILITY (Предупреждение):**
    - Окно 14 дней, минимум 5 замеров сахара.
    - Выборочное стандартное отклонение $\text{SD} > \text{glucose\_sd\_threshold}$ (0.8 ммоль/л) ИЛИ тренд (разница средних второй и первой половины окна) $> \text{glucose\_trend\_threshold}$ (0.1 ммоль/л/нед).
    - При одновременном срабатывании выбирается более выраженный фактор:
      $$\frac{\text{trend}}{\text{trend\_threshold}} > \frac{\text{sd}}{\text{sd\_threshold}}$$
14. **MEASURE_SOON (Информационный):**
    - Мягкое напоминание о взвешивании: $\text{measure\_soon\_after\_days} \le \text{gap} < \text{no\_measure\_days}$ ($5 \le \text{gap} < 7$). Взаимоисключающ с `NO_MEASURE`.

### SQL-запросы
- Множественные выборки из `body_metrics`, `users`, `daily_targets`, `food_log`, `food_items`, `med_log`, `glucose_log`, `anthropometry`, `alerts`.
- Запись алертов:
  `INSERT INTO alerts(user_id, created_at, rule, message) VALUES (?, ?, ?, ?)`
  с дедупликацией: не чаще одного раза в сутки для каждого правила `rule`.

### Потенциальные проблемы и дефекты
1. **Жесткий парсинг даты `_parse`:** Использует `datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")`. Если дата записана в ISO формате с разделителем `T` (например, `2026-08-20T07:00:00`), `_parse` упадет с `ValueError`.
2. **Опасность деления на дни в `check_rate_high`:** `days_elapsed = (_parse(last) - _parse(first)).days`. При замерах с интервалом, например, 30 часов `days_elapsed = 1`. Множитель $7 / 1$ превратит суточное колебание воды в 0.8 кг в огромный недельный темп. Хотя $N \ge 4$ сглаживает это, крайние точки могут быть близко по датам.
3. **Рассинхрон расчета в `get_guards_status` и `check_protein_skew`:** В `get_guards_status` (строка 889) выполняется запрос с `GROUP BY fl.id`, а в `check_protein_skew` (строка 407) группировка делается в Python без `GROUP BY` в SQL.
4. **Регулярные выражения и регистр в WHR:** Поиск сайтов антропометрии жестко завязан на `'талия'` и `'таз'`. Опечатка или синоним приведет к молчаливому возврату `None`.

---

## 4. health_core/report.py

### Назначение модуля
Модуль форматирует текстовые отчеты системы: мгновенный статус-бар (`status_bar`, §07), вечерний отчет (`evening_report`, §11), понедельный сводный отчет (`weekly_summary`), детализацию приемов пищи (`meals_of_day`), расчет эффективности тренировок (`training_efficiency`), динамику трендов (`trends`) и автозакрытие вех (`mark_achieved_milestones`).

### Сигнатуры функций и docstring
1. `_now() -> datetime`
2. `_time_tag(hour: int) -> str`
3. `_water_target_ml(conn: sqlite3.Connection, user_id: int) -> float | None`
4. `_day_macros(conn: sqlite3.Connection, user_id: int, date: str) -> dict`
5. `_day_water_ml(conn: sqlite3.Connection, user_id: int, date: str) -> float`
6. `_latest_metric(conn: sqlite3.Connection, user_id: int)`
7. `_weight_trend(conn: sqlite3.Connection, user_id: int, window_days: int, latest_weight: float)`
   - *Docstring:* `"""Дельта веса за окно: медиана конца минус медиана начала..."""`
8. `_next_milestone(conn: sqlite3.Connection, user_id: int, latest_weight: float)`
9. `_active_alerts_today(conn: sqlite3.Connection, user_id: int, date: str) -> list[sqlite3.Row]`
10. `_meal_clause(conn: sqlite3.Connection, user_id: int, date: str, now: datetime) -> str | None`
11. `day_summary(conn: sqlite3.Connection, user_id: int, date: str) -> dict`
    - *Docstring:* `"""Все числа дня одним словарём — источник и для status_bar, и для evening_report."""`
12. `status_bar(conn: sqlite3.Connection, user_id: int) -> str`
    - *Docstring:* `"""Готовая строка §07. Модель дописывает её как есть, не перефразируя."""`
13. `evening_report(conn: sqlite3.Connection, user_id: int, date: str) -> str`
    - *Docstring:* `"""Вечерний отчёт 21:30 (§11) — данные для единственного вызова LLM за этот тик."""`
14. `meals_of_day(conn: sqlite3.Connection, user_id: int, date: str) -> list[dict]`
    - *Docstring:* `"""One entry per food_log row for date, ordered by eaten_at..."""`
15. `whr(conn: sqlite3.Connection, user_id: int) -> float | None`
    - *Docstring:* `"""Waist-to-Hip Ratio: latest талия / latest таз. Returns None if either is missing."""`
16. `trends(conn: sqlite3.Connection, user_id: int, window_days: int = 30) -> dict`
    - *Docstring:* `"""Вес, LBM, талия, WHR, фактический TDEE за окно (§07 query_metrics/get_progress)."""`
17. `baseline_weight(conn: sqlite3.Connection, user_id: int) -> float | None`
    - *Docstring:* `"""Точка отсчёта «Δ от старта» — одна на всю систему..."""`
18. `weight_series(conn: sqlite3.Connection, user_id: int, days: int = 90) -> list[tuple[str, float]]`
    - *Docstring:* `"""Ряд (дата, вес) за последние days дней — данные для спарклайна дашборда..."""`
19. `_milestone_values(conn: sqlite3.Connection, user_id: int, metric: str)`
20. `mark_achieved_milestones(conn: sqlite3.Connection, user_id: int) -> list[dict]`
    - *Docstring:* `"""Проставляет achieved_at вехам, порог которых уже пройден..."""`
21. `training_efficiency(conn: sqlite3.Connection, user_id: int, window_days: int = 60) -> dict`
    - *Docstring:* `"""Эффективность тренировок по видам спорта за окно (§07/§11 расширение)..."""`
22. `_window_delta(conn: sqlite3.Connection, user_id: int, table: str, col: str, date_col: str, start_ts: str, end_ts: str, extra_where: str = "") -> float | None`
23. `weekly_summary(conn: sqlite3.Connection, user_id: int, end_date: str | None = None) -> str`
    - *Docstring:* `"""Готовый markdown-блок за 7 дней, заканчивающихся end_date..."""`
24. `_parse(ts: str) -> datetime`

### Алгоритмы и логика отчетов
1. **Статус-бар (§07):** 4 строки:
   - Стр. 1: `Сегодня DD.MM, HH:MM. Вес X.X (утро|днём|вечером). Цель KKKK ккал.`
   - Стр. 2: `Съедено KKKK / Б BB / Ж ЖЖ / У УУ. Вода W.W / TT.T л.`
   - Стр. 3: Напоминание о пропущенном приеме пищи (`Завтрак|Обед|Ужин не записан.`) + статус гардрейлов (`Активных алертов нет.` или `Активных алертов: N.`).
   - Стр. 4: `Тренд 7 дней: [−]X.X кг. Ближайшая веха MM кг, осталось R.R.` (используется типографский минус `−`).
2. **Медианный тренд веса (`_weight_trend`):**
   - Находит точку-якорь за пределами окна (`measured_at <= since`) и все точки внутри окна.
   - Сравнивает медиану первых 1-3 точек с медианой последних 1-3 точек.
3. **Автоматическое закрытие вех (`mark_achieved_milestones`):**
   - Вычисляет спан $\text{span} = \text{threshold} - \text{baseline}$.
   - Если $\text{span} < 0$ (вес, жир, талия идут вниз), цель достигнута при $\text{current} \le \text{threshold}$.
   - Если $\text{span} > 0$ (мышечная масса идет вверх), цель достигнута при $\text{current} \ge \text{threshold}$.
   - Проставляет `achieved_at = now` в БД и коммитит.
4. **Эффективность тренировок (`training_efficiency`):**
   - Метрики: $\text{kcal\_per\_min} = \text{kcal} / \text{duration\_min}$, $\text{kcal\_per\_hr\_min} = \text{kcal\_per\_min} / \text{avg\_hr}$.
   - При $\ge 4$ сессиях рассчитывает тренд: сравнивает среднее старой и новой половины сессий.
5. **Недельная сводка (`weekly_summary`):**
   - Формирует markdown-блок из 5 разделов: Вес, Состав, Дисциплина (еда и взвешивания X/7 дн), План/факт (совпало X/Y дней в коридоре $\pm 10\%$ ккал), Гардрейлы (исторические алерты из таблицы `alerts`, без повторного запуска проверок).

### SQL-запросы
- Запросы агрегации дневных показателей из `food_log`, `food_items`, `water_log`, `body_metrics`, `daily_targets`, `anthropometry`, `activity`, `milestones`, `alerts`.
- Пометка вех: `UPDATE milestones SET achieved_at=? WHERE id=?`.

### Константы и пороговые значения
- Временные пороги дня: утро $< 11:00$, день $< 17:00$, вечер $\ge 17:00$.
- Контрольные часы приемов: завтрак до $10:00$, обед до $15:00$, ужин до $21:00$.
- Типографский минус: `MINUS = "−"`.
- Коридор совпадения плана и факта: $\pm 10\%$ по калоражу.

### Потенциальные проблемы и дефекты
1. **Рассинхрон количества алертов в `status_bar`:** В строке 213 вызывается живой `check_all(conn, user_id)`, тогда как `day_summary()` читает сохраненные записи из таблицы `alerts`. Если алерт возник только что и еще не был записан в БД хендлером, статус-бар покажет наличие алертов, но в структурированном словаре `day_summary` их не будет.
2. **Деградация `_weight_trend` при редких взвешиваниях:** Если точек меньше 2, возвращает `None`, что приводит к отображению «недостаточно данных».

---

## 5. health_core/forecast.py

### Назначение модуля
Модуль реализует математическую симуляцию динамического баланса энергии и изменения массы тела на основе дифференциальной модели Кевина Холла (Hall KD, Lancet 2011) и правила Форбса (Forbes, 1987). Принцип: отказ от линейной экстраполяции в пользу физиологической модели с учетом падения BMR и затухания дефицита по мере снижения массы тела.

### Сигнатуры функций и docstring
1. `_policy() -> dict`
2. `_forecast_cfg() -> dict`
3. `smoothed_weight(conn: sqlite3.Connection, user_id: int, days: int = 7) -> tuple[float, str] | None`
   - *Docstring:* `"""Сглаженная масса и дата последнего замера..."""`
4. `_observed_intake(conn: sqlite3.Connection, user_id: int, window_days: int = 14) -> float | None`
   - *Docstring:* `"""Средний залогированный приход за окно..."""`
5. `_plateau_note(conn: sqlite3.Connection, user_id: int, weight_kg: float, height_cm: float) -> str | None`
   - *Docstring:* `"""Где человек находится относительно медианы времени до плато в SURMOUNT..."""`
6. `_simulate(weight_kg: float, fat_kg: float, height_cm: float, age_years: int, sex: str, intake_kcal: float, days: int, activity_factor: float, adaptive_pct: float) -> tuple[list[float], int | None]`
   - *Docstring:* `"""Посуточная траектория массы и день, на котором модель перестала работать..."""`
7. `project(conn: sqlite3.Connection, user_id: int, horizon_days: int = 84, intake_kcal: float | None = None) -> dict`
   - *Docstring:* `"""Прогноз массы на горизонт. Отказ вместо догадки, если данных мало..."""`
8. `reach(conn: sqlite3.Connection, user_id: int, target_kg: float, intake_kcal: float | None = None) -> dict`
   - *Docstring:* `"""Когда масса дойдёт до target_kg. Диапазон дат, не дата..."""`

### Алгоритмы и формулы
1. **Посуточная симуляция (`_simulate`):**
   На каждый шаг $t \in [1, \text{days}]$:
   - Пересчет $\text{BMR} = \text{Mifflin}(W_t, \text{Height}, \text{Age}, \text{Sex})$.
   - $\text{TDEE}_t = \text{BMR} \times \text{activity\_factor} \times (1 - \text{adaptive\_pct})$.
   - $\text{Deficit}_t = \text{TDEE}_t - \text{Intake}$.
   - **Дифференциальное правило Форбса:**
     $$\text{LeanShare} = \frac{C}{C + \text{FM}_t} = \frac{10.4}{10.4 + \text{FM}_t}$$
   - Энергетическая плотность изменяемой ткани:
     $$\rho = \text{LeanShare} \times 1816 + (1 - \text{LeanShare}) \times 9441 \quad (\text{ккал/кг})$$
   - Изменение массы ткани за сутки:
     $$\Delta W = \frac{\text{Deficit}_t}{\rho}$$
     $$\text{FM}_{t+1} = \text{FM}_t - \Delta W \times (1 - \text{LeanShare})$$
     $$\text{FFM}_{t+1} = \text{FFM}_t - \Delta W \times \text{LeanShare}$$
     $$W_{t+1} = \text{FM}_{t+1} + \text{FFM}_{t+1}$$
2. **Обрыв симуляции (Safety clamp):**
   Если жировая масса падает до критического предела:
   $$\text{FM}_t \le W_t \times \text{ESSENTIAL\_FAT\_PCT} \quad (5\%)$$
   Симуляция замораживается на достигнутом весе, предотвращая математический артефакт правила Форбса (уход в бесконечное сжигание чистой мышечной массы и отрицательный вес).
3. **Комбинированная полоса неопределенности:**
   Модель объединяет разброс пищевого дневника ($\pm 300$ ккал/сут) и ошибку моделирования NIH Body Weight Planner ($-6.2\% \dots +3.7\%$ на горизонте 182 дней):
   $$f = \frac{t}{182}$$
   $$W_{\text{lo}}(t) = W_{\text{lo\_intake}}(t) \times (1 - 0.062 \times f)$$
   $$W_{\text{hi}}(t) = W_{\text{hi\_intake}}(t) \times (1 + 0.037 \times f)$$
4. **Оценка выхода на плато (SURMOUNT-1):**
   Медиана времени выхода на плато по классам ИМТ:
   - ИМТ $\ge 40$: 36.1 недель
   - ИМТ $\ge 35$: 36.1 недель
   - ИМТ $\ge 30$: 26.0 недель
   - ИМТ $\ge 25$: 24.3 недель
   Если срок на программе превышает 80% от медианы, выдается предупреждение о скором выходе на плато.

### Константы и пороговые значения
- `KCAL_PER_KG_FAT = 9441` ккал/кг (39.5 МДж/кг).
- `KCAL_PER_KG_LEAN = 1816` ккал/кг (7.6 МДж/кг).
- `FORBES_C = 10.4` (константа Форбса).
- `MAX_HORIZON_DAYS = 182` (6 месяцев — предельный горизонт).
- `MAX_WEIGHT_AGE_DAYS = 7` (максимальный возраст последнего замера для построения прогноза).
- `DEFAULT_INTAKE_UNCERTAINTY = 300.0` ккал/сут.
- `MODEL_ERR_LO_PCT = 0.062`, `MODEL_ERR_HI_PCT = 0.037`, `MODEL_ERR_REF_DAYS = 182`.
- `ESSENTIAL_FAT_PCT = 0.05` (5% массы тела — порог эссенциального жира).
- Диапазон сценариев калоража: `MIN_SCENARIO_KCAL = 400`, `MAX_SCENARIO_KCAL = 10000` ккал/сут.

### SQL-запросы
- `SELECT date(measured_at) d, weight_kg w FROM body_metrics WHERE user_id=? AND weight_kg IS NOT NULL ORDER BY measured_at`
- `SELECT date(fl.eaten_at) d, SUM(fi.kcal) kcal FROM food_log fl JOIN food_items fi ON fi.food_log_id = fl.id WHERE fl.user_id=? AND date(fl.eaten_at) BETWEEN ? AND ? GROUP BY 1`
- `SELECT height_cm, birth_date, sex FROM users WHERE id=?`
- `SELECT base_weight_date FROM users WHERE id=?`

### Потенциальные проблемы и дефекты
1. **Зеркальное применение Форбса к профициту:** Правило Форбса выведено на катаболизме (похудении). При профиците модель начисляет ткани по тем же коэффициентам, хотя композиция набора веса у людей подчиняется другим закономерностям (зависит от тренировочного стимула и анаболического статуса).
2. **Отказ функции `reach` при наборе веса:** `reach()` жестко требует `target_kg < current_weight`. При попытке рассчитать выход из дефицита или набор массы в профиците функция возвращает ошибку.
3. **Линейная интерполяция ошибки модели:** Линейная экстраполяция перцентилей погрешности NIH BWP от 0 до 182 дней — эмпирическое допущение авторов кода, не имеющее прямого подтверждения в исследовании Томаса (в статье опубликована только финальная точка).

---

## 6. health_core/refeed.py

### Назначение модуля
Модуль управляет графиком плановых диетических перерывов (diet breaks / refeed) по протоколу MATADOR (Byrne et al., 2018): 2 недели умеренного дефицита чередуются с 2 неделями поддержания (нормокалорийный рацион на уровне полного TDEE) для предотвращения адаптивного термогенеза и снижения лептина.

### Сигнатуры функций и docstring
1. `_d(s)`
2. `cycle_days(start, horizon_weeks: int = 12, deficit_weeks: int = 2, break_weeks: int = 2) -> list[str]`
   - *Docstring:* `"""Даты ПЕРЕРЫВОВ на горизонте. Цикл начинается с фазы дефицита."""`
3. `schedule(conn: sqlite3.Connection, user_id: int, start, horizon_weeks: int = 12) -> dict`
   - *Docstring:* `"""Расставить перерывы начиная со start. Существующие даты не дублируются."""`
4. `clear(conn: sqlite3.Connection, user_id: int, frm=None) -> int`
   - *Docstring:* `"""Снять будущие перерывы. Прошлые не трогаем — это история, а не план."""`
5. `status(conn: sqlite3.Connection, user_id: int, today) -> dict`
   - *Docstring:* `"""Фаза на сегодня и когда она сменится."""`

### Алгоритмы и логика
1. **Генератор цикла MATADOR (`cycle_days`):**
   - Период цикла: $T = (\text{deficit\_weeks} + \text{break\_weeks}) \times 7 = (2 + 2) \times 7 = 28$ дней.
   - Условие дня перерыва: $\text{day} \pmod T \ge \text{deficit\_weeks} \times 7$.
   - Цикл всегда строго стартует с фазы дефицита (дни $0 \dots 13$ — дефицит, дни $14 \dots 27$ — перерыв).
2. **Статус текущей фазы (`status`):**
   - Проверяет, входит ли `today` в множество дат таблицы `refeed_days`.
   - Ищет день смены фазы вперед по календарю на глубину до 60 дней (`limit = 60`).

### Константы и пороговые значения
- `DEFICIT_WEEKS = 2` (14 дней дефицита).
- `BREAK_WEEKS = 2` (14 дней нормокалорийного перерыва).
- Минимальный порог эффективного перерыва по данным Peos: не менее 7 дней.

### SQL-запросы
- `INSERT OR IGNORE INTO refeed_days(user_id, date) VALUES (?,?)`
- `DELETE FROM refeed_days WHERE user_id=? AND date >= ?`
- `SELECT date FROM refeed_days WHERE user_id=?`

### Потенциальные проблемы и дефекты
1. **Зависимость `clear` от часового пояса сервера:** `frm = (frm or _date.today())`. При вызове без аргументов используется `date.today()` машины сервера, а не часовой пояс пользователя (`local_now()`). При разнице UTC и локального времени человека утренний вызов может удалить дату сегодняшнего рефида или оставить вчерашний.
2. **Ограничение поиска смены фазы:** Если горизонт перерыва или дефицита запланирован более чем на 60 дней вперед, `status()` возвращает `changes_in_days: None`, скрывая дату следующего переключения.

---

## 7. health_core/config.py

### Назначение модуля
Модуль обеспечивает трехуровневую иерархию настроек (§03 спецификации):
- Уровень 1: глобальная политика `config.yaml` (кешируется в памяти).
- Уровень 2: профиль пользователя и индивидуальные цели в БД (`users`, `user_targets`).
- Уровень 3: вехи и дедлайны пользователя (`milestones`).
Также модуль инкапсулирует работу с локальным временем пользователя через `ContextVar` и часовые пояса IANA.

### Сигнатуры функций и docstring
1. `load() -> dict`
2. `set_tz(name: str | None) -> None`
   - *Docstring:* `"""Вызывается один раз на входящее сообщение, до исполнения инструментов."""`
3. `local_now() -> datetime`
   - *Docstring:* `"""«Сейчас» глазами обслуживаемого человека..."""`
4. `local_today() -> str`
5. `user_now(conn: sqlite3.Connection, user_id: int) -> datetime`
   - *Docstring:* `"""Текущее время В ЧАСОВОМ ПОЯСЕ ЧЕЛОВЕКА, а не сервера..."""`
6. `user_today(conn: sqlite3.Connection, user_id: int) -> str`
7. `latest_ffm(conn: sqlite3.Connection, user_id: int) -> float | None`
   - *Docstring:* `"""Последняя ИЗМЕРЕННАЯ тощая масса, а не обязательно из последней строки..."""`
8. `targets_for(conn: sqlite3.Connection, user_id: int) -> dict`

### Алгоритмы и логика
1. **Изоляция часового пояса:**
   Используется `ContextVar("health_user_tz", default=None)`. Это гарантирует потокобезопасность в асинхронной среде телеграм-бота: запросы двух разных пользователей в разных часовых поясах не перетирают глобальное состояние друг друга.
2. **Инвариант тощей массы (`latest_ffm`):**
   При ручном взвешивании на обычных весах колонка `ffm_kg` пуста. `latest_ffm` берет последний замер, где `ffm_kg IS NOT NULL`. Это защищает систему от катастрофического завышения BMR и белка, когда вес тела ошибочно принимался за чистую сухую массу.
3. **Расчет профильных таргетов (`targets_for`):**
   $$\text{ProteinTarget} = \text{FFM}_{\text{kg}} \times 1.8$$
   $$\text{WaterTarget} = \text{Weight}_{\text{kg}} \times 23 \quad (\text{мл})$$
   $$\text{FatPct} = 0.30, \quad \text{FatPctMin} = 0.20, \quad \text{FiberTarget} = 25 \quad (\text{г})$$

### SQL-запросы
- `SELECT timezone FROM users WHERE id=?`
- `SELECT ffm_kg FROM body_metrics WHERE user_id=? AND ffm_kg IS NOT NULL ORDER BY measured_at DESC LIMIT 1`
- `SELECT weight_kg, ffm_kg FROM body_metrics WHERE user_id=? ORDER BY measured_at DESC LIMIT 1`
- `SELECT base_weight_kg FROM users WHERE id=?`

### Потенциальные проблемы и дефекты
1. **Несбрасываемый кеш `load()`:** Функция декоратора `@lru_cache(maxsize=1)` намертво кеширует `config.yaml` при первом вызове. Изменения в `config.yaml`, внесенные администратором на лету, не применяются до полного перезапуска процесса.
2. **Опасный фолбэк для FFM при отсутствии биоимпеданса:** В строке 107: `ffm_kg = row["ffm_kg"] or latest_ffm(conn, user_id) or weight_kg`. Если пользователь ни разу не делал биоимпеданс, его FFM принимается равным полной массе тела ($100\%$ сухой массы). Для человека весом 120 кг это дает норму белка $120 \times 1.8 = 216$ г вместо реальных $\approx 140$ г.

---

## 8. health_core/models.py

### Назначение модуля
Модуль содержит описание сущностей базы данных в виде неизменяемых датаклассов (`frozen dataclass`). Предоставляет вспомогательную функцию `row_to(cls, row)` для гидратации строк `sqlite3.Row` в типизированные объекты.

### Сигнатуры датаклассов
1. `User` (поля: `id`, `telegram_user_id`, `height_cm`, `birth_date`, `sex`, `timezone`, `base_weight_kg`, `base_weight_date`, `created_at`)
2. `UserTarget` (поля: `id`, `user_id`, `valid_from`, `protein_g`, `water_ml`, `kcal_floor`)
3. `Milestone` (поля: `id`, `user_id`, `name`, `metric`, `threshold`, `deadline`, `achieved_at`)
4. `BodyMetric` (поля: `id`, `user_id`, `burst_key`, `measured_at`, `weight_kg`, `fat_pct`, `bmi`, `skeletal_muscle_pct`, `muscle_mass_kg`, `protein_pct`, `device_bmr_kcal`, `ffm_kg`, `subcutaneous_fat_pct`, `visceral_fat`, `water_pct`, `bone_mass_kg`, `metabolic_age`, `device_mac`)
5. `Anthropometry` (поля: `id`, `user_id`, `measured_on`, `site`, `value_cm`)
6. `FoodLog` (поля: `id`, `user_id`, `eaten_at`, `notes`)
7. `FoodItem` (поля: `id`, `food_log_id`, `name`, `grams`, `kcal`, `protein_g`, `fat_g`, `carbs_g`, `plate_category`)
8. `WaterLog` (поля: `id`, `user_id`, `at`, `volume_ml`)
9. `GlucoseLog` (поля: `id`, `user_id`, `at`, `mmol_l`, `context`, `confirmed`)
10. `Activity` (поля: `id`, `user_id`, `started_at`, `duration_min`, `kcal`, `avg_hr`, `sport`, `file_hash`)
11. `DailyTarget` (поля: `id`, `user_id`, `date`, `kcal_target`, `protein_g_target`, `water_ml_target`, `computed_from`)
12. `Alert` (поля: `id`, `user_id`, `created_at`, `rule`, `message`)
13. `MedLog` (поля: `id`, `user_id`, `at`, `substance`, `dose`)
14. `LlmCall` (поля: `id`, `user_id`, `created_at`, `model`, `tokens_in`, `tokens_out`, `cost_usd`)
15. `ImportLog` (поля: `id`, `user_id`, `imported_at`, `file_hash`)

### Потенциальные проблемы и дефекты
1. **Критическое расхождение схемы БД и моделей данных (Schema Drift):**
   Датаклассы в `models.py` отстали от эволюции миграций базы данных:
   - В `FoodLog` отсутствует поле `meal_slot` (добавлено в v5).
   - В `FoodItem` отсутствует поле `fiber_g` (добавлено в v11).
   - В `DailyTarget` отсутствуют поля `fat_g_target`, `carbs_g_target` (добавлены в v6) и `fiber_g_target` (добавлено в v11).
   - В `MedLog` отсутствуют поля `route`, `unit`, `site`, `notes` (добавлены в v3).
   - В `Activity` отсутствуют поля `notes`, `source` (добавлены в v14).
   - Полностью отсутствуют классы для новых таблиц: `SleepLog`, `Pantry`, `MedSchedule`, `RefeedDay`, `Equipment`, `PlanLog`, `MealPlan`, `WorkoutPlan`, `PersonaStyle`.
   **Следствие:** Вызов `row_to(DailyTarget, row)` или `row_to(FoodLog, row)` на данных актуальной схемы БД вызовет мгновенный фатальный сбой:
   `TypeError: DailyTarget.__init__() got an unexpected keyword argument 'fat_g_target'`.

---

## 9. health_core/db.py

### Назначение модуля
Модуль управляет жизненным циклом базы данных SQLite: установка соединения в режиме WAL с контролем таймаутов, первичная инициализация схемы DDL (25 таблиц) и пошаговые миграции структуры данных с v1 до актуальной v14.

### Сигнатуры функций и константы
- `DB_PATH = Path(os.environ.get("HEALTH_DB", str(Path.home() / ".hermes" / "health.db")))`
- `SCHEMA_VERSION = 14`
1. `connect() -> sqlite3.Connection`
2. `_migrate_v2_to_v3(conn: sqlite3.Connection) -> None`
3. `_migrate_v4_to_v5(conn: sqlite3.Connection) -> None`
4. `_migrate_v5_to_v6(conn: sqlite3.Connection) -> None`
5. `_add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None`
6. `_migrate_v10_to_v11(conn: sqlite3.Connection) -> None`
7. `_migrate_v9_to_v10(conn: sqlite3.Connection) -> None`
8. `_migrate_v13_to_v14(conn: sqlite3.Connection) -> None`
9. `migrate(conn: sqlite3.Connection) -> None`

### Настройки подключения SQLite
- `PRAGMA journal_mode=WAL` (Write-Ahead Logging для параллельного чтения и записи).
- `PRAGMA synchronous=NORMAL` (баланс надежности и скорости ввода-вывода).
- `PRAGMA foreign_keys=ON` (активация внешних ключей и каскадного удаления).
- `PRAGMA busy_timeout=5000` (таймаут ожидания блокировки 5 секунд).

### Полная структура таблиц и история миграций (v14)
1. `schema_version`: хранение текущей версии схемы.
2. `users`: мастер-профиль пользователя (телеграм ID, рост, дата рождения, пол, пояс, базовый вес для отсчета дельты).
3. `user_targets`: переопределения целей калоража и макросов 2-го уровня.
4. `milestones`: цели и вехи пользователя со сроками и отметкой достижения `achieved_at`.
5. `body_metrics`: 16 параметров с биоимпедансных весов. `burst_key` защищает от дублирования внутри одной серии взвешиваний.
6. `anthropometry`: замеры объемов тела (талия, таз и др.).
7. `food_log`: приемы пищи с привязкой к слотам (`meal_slot`: breakfast/lunch/dinner/snack).
8. `food_items`: детализация блюд КБЖУ + клетчатка `fiber_g` + категория тарелки `plate_category`.
9. `water_log`: журнал гидратации.
10. `glucose_log`: замеры глюкозы крови (ммоль/л) с контекстом.
11. `activity`: тренировочные сессии (калории, длительность, пульс, вид спорта, хеш файла).
12. `med_schedule`: расписание приема препаратов, путь введения (`route`), интервал и складской остаток доз (`stock_doses`).
13. `sleep_log`: сон за ночь пробуждения `night_date` (фазы, пульс, SpO2, эффективность).
14. `pantry`: склад продуктов дома.
15. `daily_targets`: рассчитанный суточный план калорий и макросов с аудитом источника `computed_from`.
16. `alerts`: журнал сработавших клинических гардрейлов.
17. `med_log`: журнал фактического приема лекарств (`route`: oral/injection/etc., `site`, `dose`).
18. `llm_calls`: аудит расходов токенов и стоимости вызовов нейросетей.
19. `import_log`: дедупликация импортированных внешних файлов по хешу.
20. `refeed_days`: календарь дней плановых диетических перерывов.
21. `equipment`: домашний спортивный инвентарь.
22. `plan_log`: готовые сгенерированные текстовые планы на день с обоснованием `rationale`.
23. `meal_plan`: еженедельный повторяющийся шаблон меню по дням недели (0-6).
24. `workout_plan`: еженедельный повторяющийся шаблон тренировок по дням недели (0-6).
25. `persona_styles`: кастомные промпты персоны ассистента.

### Потенциальные проблемы и дефекты
1. **Дефект в миграции v13->v14 (Ограничение NOT NULL на `file_hash`):**
   В комментарии к миграции v14 сказано: «делает file_hash необязательным (для ручных записей)». Однако в DDL строка 134 по-прежнему содержит:
   `file_hash TEXT NOT NULL UNIQUE`.
   В функции `_migrate_v13_to_v14` добавляются только колонки `notes` и `source`. Ограничение `NOT NULL` с `file_hash` снято не было (так как SQLite требует пересоздания таблицы для изменения ограничений существующих колонок). В результате любая ручная запись активности без генерации фиктивного хеша завершится ошибкой `sqlite3.IntegrityError: NOT NULL constraint failed: activity.file_hash`.
2. **Опасность `_add_column` при прерывании:** Вызов `_add_column` не обернут в транзакцию, хотя SQLite DDL поддерживает транзакционность.

---

## 10. health_core/meds.py

### Назначение модуля
Модуль обеспечивает канонизацию названий лекарственных препаратов и слияние дублирующихся расписаний в таблице `med_schedule`. Решает проблему синонимов (например, МНН «Тирзепатид» и торговое наименование «Тирзетта», «Тесторил» и «Андрокомплекс»), предотвращая задвоение дозировок и просрочку таймеров.

### Сигнатуры функций и docstring
1. `_key(name: str) -> str`
2. `aliases() -> dict[str, str]`
   - *Docstring:* `"""алиас -> каноническое имя. Файла нет — пустая карта, не исключение..."""`
3. `canon(name: str) -> str`
   - *Docstring:* `"""Каноническое имя препарата. Незнакомое возвращается как есть..."""`
4. `merge_duplicate_schedules(conn) -> list[tuple[str, str]]`
   - *Docstring:* `"""Свести строки med_schedule, чьи имена — алиасы одного препарата..."""`

### Алгоритмы и логика
1. **Парсинг справочника синонимов из базы знаний:**
   Карта читается напрямую из `Knowledge/drug_cards.md` с помощью регулярного выражения:
   `^#{1,6}[^\S\n]*\S*[^\S\n]*Карта[^\S\n]*\d+[^\S\n]*:[^\S\n]*(.+?)[^\S\n]*$`
   Слова, разделенные слешем `/`, и латинские названия в скобках `(...)` маппятся на первое указанное русское каноническое имя.
2. **Правила объединения расписаний (`merge_duplicate_schedules`):**
   - Остаток препарата на складе (`stock_doses`) выбирается по **максимуму** ($\max$), а не суммируется (чтобы не задвоить один и тот же купленный флакон, записанный под разными именами).
   - Срок следующего приема (`next_at`) выбирается по **минимуму** ($\min$) — самый ранний срок, чтобы не скрыть пропущенную инъекцию.
   - Записи в журнале фактов `med_log` намеренно не модифицируются для сохранения исторической достоверности.

### SQL-запросы
- `SELECT id, user_id, substance, next_at, stock_doses FROM med_schedule ORDER BY id`
- `DELETE FROM med_schedule WHERE id=?`
- `UPDATE med_schedule SET substance=?, next_at=?, stock_doses=? WHERE id=?`
- `UPDATE med_schedule SET substance=? WHERE id=?`

### Потенциальные проблемы и дефекты
1. **Кеширование `aliases()` через `@lru_cache(maxsize=1)`:** Если пользователь или бот добавляет новую карту препарата в `drug_cards.md`, кеш не сбрасывается автоматически. Новые алиасы не подхватываются до перезапуска процесса бота.
2. **Хрупкость регулярного выражения `_CARD_RE`:** Если заголовок карты будет оформлен без слова «Карта N:» (например, «## Тирзепатид / Мунджаро»), регулярное выражение пропустит его, и синонимы не попадут в словарь.

---

## 11. health_core/plans.py

### Назначение модуля
Модуль управляет недельными циклическими шаблонами питания (`meal_plan`) и тренировок (`workout_plan`), а также выполняет объективный план-факт анализ за выбранную календарную дату (`plan_vs_actual`).

### Сигнатуры функций и docstring
1. `_upsert(conn: sqlite3.Connection, table: str, key: dict, fields: dict, allowed: tuple) -> int`
2. `set_meal_plan(conn: sqlite3.Connection, user_id: int, day_of_week: int, meal_slot: str, **fields) -> int`
3. `set_workout_plan(conn: sqlite3.Connection, user_id: int, day_of_week: int, name: str, **fields) -> int`
4. `remove_meal_plan(conn: sqlite3.Connection, user_id: int, day_of_week: int | None = None, meal_slot: str | None = None) -> int`
5. `remove_workout_plan(conn: sqlite3.Connection, user_id: int, day_of_week: int | None = None, name: str | None = None) -> int`
6. `delete_plan_day(conn: sqlite3.Connection, user_id: int, date: str | None = None, kind: str | None = None) -> int`
7. `get_plan(conn: sqlite3.Connection, user_id: int, day_of_week: int | None = None) -> dict`
8. `_meals_vs_actual(conn: sqlite3.Connection, user_id: int, dow: int, date: str) -> dict`
9. `_workouts_vs_actual(conn: sqlite3.Connection, user_id: int, dow: int, date: str) -> dict`
10. `plan_vs_actual(conn: sqlite3.Connection, user_id: int, date: str) -> dict`
    - *Docstring:* `"""Planned (weekly template for date's weekday) vs actual (that calendar date)..."""`

### Алгоритмы и логика
1. **Недельные шаблоны:** Дни недели индексируются строго $0 \dots 6$ ($0$ — понедельник, $6$ — воскресенье в соответствии со стандартом `datetime.weekday()`).
2. **Строгая валидация полей в `_upsert`:** Передача любого неизвестного именованного аргумента вызывает немедленный `TypeError`, что защищает от опечаток в именах нутриентов.
3. **План-факт сопоставление (`plan_vs_actual`):**
   - Находит день недели для переданной даты.
   - Агрегирует запланированные показатели из `meal_plan` / `workout_plan`.
   - Агрегирует фактические показатели из `food_log` + `food_items` и `activity`.
   - Вычисляет дельту $\text{Delta} = \text{Planned} - \text{Actual}$.
   - Принцип честности данных: если плана не было, `planned = None` и `delta = None` (никогда не подставляется $0$).

### SQL-запросы
- `INSERT INTO meal_plan(...) VALUES (...) ON CONFLICT(...) DO UPDATE SET ...`
- `INSERT INTO workout_plan(...) VALUES (...) ON CONFLICT(...) DO UPDATE SET ...`
- `DELETE FROM meal_plan WHERE ...`
- `DELETE FROM workout_plan WHERE ...`
- `DELETE FROM plan_log WHERE ...`
- `SELECT * FROM meal_plan WHERE ...`
- `SELECT * FROM workout_plan WHERE ...`
- `SELECT COUNT(*) n, SUM(kcal) kcal, SUM(protein_g) protein_g, SUM(fat_g) fat_g, SUM(carbs_g) carbs_g FROM meal_plan WHERE user_id=? AND day_of_week=?`
- `SELECT COUNT(DISTINCT fl.id) n, COALESCE(SUM(fi.kcal),0) kcal, COALESCE(SUM(fi.protein_g),0) protein_g, COALESCE(SUM(fi.fat_g),0) fat_g, COALESCE(SUM(fi.carbs_g),0) carbs_g FROM food_log fl LEFT JOIN food_items fi ON fi.food_log_id=fl.id WHERE fl.user_id=? AND date(fl.eaten_at)=?`
- `SELECT COUNT(*) n, SUM(duration_min) duration_min FROM workout_plan WHERE user_id=? AND day_of_week=?`
- `SELECT COUNT(*) n, COALESCE(SUM(duration_min),0) duration_min, COALESCE(SUM(kcal),0) kcal FROM activity WHERE user_id=? AND date(started_at)=?`

### Потенциальные проблемы и дефекты
1. **Неустойчивость парсинга даты:** В `dow = datetime.strptime(date, "%Y-%m-%d").weekday()`. Если в функцию `plan_vs_actual` попадает строка с временем (например, `2026-08-25 10:00:00`), выполнение упадет с ошибкой формата даты.
2. **Смешение нескольких тренировок одного вида в `_workouts_vs_actual`:** Тренировки сопоставляются только по общей сумме минут за день без привязки к конкретному виду активности (`kind` / `sport`), что может маскировать выполнение кардио вместо запланированной силовой тренировки.

---

## 12. Сводный реестр выявленных дефектов, рисков и edge cases

| № | Файл | Компонент / Строка | Категория дефекта | Описание проблемы и последствия | Рекомендация по исправлению |
|---|---|---|---|---|---|
| 1 | `db.py` | строка 134, 409-412 | **Блокирующий баг (БД)** | В таблице `activity` колонка `file_hash` объявлена как `TEXT NOT NULL UNIQUE`. Миграция v14 не изменила ограничение `NOT NULL`. Ручные записи активности падают с `IntegrityError`. | Выполнить полноценную миграцию с пересозданием таблицы `activity` без ограничения `NOT NULL` на `file_hash`. |
| 2 | `models.py` | строки 76-154 | **Архитектурный рассинхрон (Schema Drift)** | Датаклассы `FoodLog`, `FoodItem`, `DailyTarget`, `MedLog`, `Activity` не содержат колонок, добавленных в миграциях v3, v5, v6, v11, v14. Вызов `row_to()` выбрасывает `TypeError`. | Актуализировать датаклассы под полную схему SQLite v14, добавить недостающие модели (`SleepLog` и др.). |
| 3 | `config.py` | строки 15-19 | **Неконсистентность рантайма** | `@lru_cache(maxsize=1)` на функции `load()` блокирует горячее обновление параметров из `config.yaml` без рестарта процесса бота. | Добавить метод инвалидации кеша или проверку `mtime` файла конфигурации при вызове `load()`. |
| 4 | `config.py` | строки 107-111 | **Опасный физиологический edge-case** | Если у пользователя нет замеров состава тела (нет биоимпеданса), FFM приравнивается к общей массе тела (`weight_kg`). Норма белка завышается в 1.5–2 раза. | При отсутствии FFM оценивать тощую массу консервативно по формуле Бура (Boer) или Хьюма (Hume), либо от веса при нормативном ИМТ 22-24. |
| 5 | `energy.py` | строки 173-175 | **Логическая ошибка во временной шкале** | В `adaptive_tdee` правая граница окна всегда берется из `local_now().date()`. При расчете калоража на произвольную дату в `daily_target(conn, uid, date)` TDEE всегда рассчитывается относительно сегодняшнего дня. | Передавать целевую дату `date` аргументом в `adaptive_tdee(..., target_date=None)`. |
| 6 | `guards.py` | строки 26-27 | **Хрупкость парсинга дат** | Функция `_parse` ожидает строго формат `%Y-%m-%d %H:%M:%S`. Любая дата в ISO 8601 с разделителем `T` приведет к падению проверок гардрейлов. | Заменить на `datetime.fromisoformat(ts.replace("Z", "+00:00")[:19])`. |
| 7 | `forecast.py` | строки 349-354 | **Функциональное ограничение** | Функция `reach()` отклоняет любую цель, если `target_kg >= start_weight_kg`. Расчет выхода на поддержание или набора массы невозможен. | Добавить поддержку сценария профицита/набора веса в `reach()`. |
| 8 | `report.py` | строки 213, 138 | **Рассинхрон отображения данных** | В `status_bar` количество алертов вычисляется живым запуском `check_all()`, а в `day_summary()` берутся сохраненные строки из таблицы `alerts`. | Синхронизировать отображение: либо везде запускать живую проверку, либо считывать зафиксированные алерты за день. |
| 9 | `refeed.py` | строки 49-51 | **Временной сдвиг границы суток** | Функция `clear()` использует `_date.today()` (время сервера), а не часовой пояс пользователя, что может стирать перерывы не того дня. | Использовать `local_now().date()` для определения текущей границы суток пользователя. |
| 10 | `meds.py` | строки 33-36 | **Кеширование справочника** | Кеш `aliases()` не инвалидируется при редактировании `Knowledge/drug_cards.md` оператором. | Добавить проверку даты модификации файла базы знаний перед отдачей закешированного словаря. |

