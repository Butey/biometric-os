# ФИНАЛЬНОЕ РЕШЕНИЕ КОЛЛЕГИИ (FINAL COUNCIL JUDGMENT)
## АРХИТЕКТУРА И ПЛАН РЕАЛИЗАЦИИ МОДУЛЯ ДОМАШНИХ СИЛОВЫХ И АЭРОБНЫХ ТРЕНИРОВОК ПРИ ПОХУДЕНИИ НА ТИРЗЕПАТИДЕ С СУСТАВНЫМ ОГРАНИЧЕНИЕМ

**Статус:** УТВЕРЖДЕНО К РЕАЛИЗАЦИИ  
**Окружение:** Debian 12 (Bookworm), 1 vCPU, 1 GB RAM, SQLite 3 (WAL mode), Python 3.11, aiogram 3  
**Целевая директория исследования:** `/opt/webapps/health_agent_system/research/train`  
**Целевой модуль системы:** `health_core/training/` (будет создан при переносе в код)  

---

## 1. СВОДНЫЕ ТЕХНИЧЕСКИЕ И КЛИНИЧЕСКИЕ ТРЕБОВАНИЯ

1. **Клинический профиль субъекта:**
   * Мужчина 34 года, рост 185 см, вес 121 кг (ИМТ 35.4), тощая масса ~79 кг, жировая масса ~42 кг.
   * Терапия: Еженедельная инъекция Тирзепатида + андрогенная поддержка.
   * Калораж: Фактический приход 900–1300 ккал/сут при целевом 1750 ккал/сут (глубокий дефицит).
   * **Анатомическое ограничение:** В анамнезе перелом колена и разрыв передней крестообразной связки (ПКС / ACL).
2. **Аппаратно-системные рамки:**
   * 1 vCPU / 1 GB RAM — категорический запрет на импорт тяжелых библиотек (pandas, torch, scipy). Использовать только стандартную библиотеку Python (`math`, `sqlite3`, `datetime`, `enum`) и легковесный `pydantic`.
   * Детерминированное вычисление тренировок: генерация сессий, прогрессии и валидация суставов выполняются за 0 мс на CPU без обращения к LLM API.
   * Модель разделения: Семантика и эмпатия (Telegram-бот aiogram 3) отделены от математического ядра.

---

## 2. СХЕМА БАЗЫ ДАННЫХ (SQLITE 3 MIGRATION)

Файл миграции: `migrations/005_training_engine.sql`

```sql
-- ====================================================================
-- MIGRATION 005: Home Resistance Training & Cardio Module
-- Compatible with SQLite 3 (WAL mode, foreign keys enabled)
-- ====================================================================

PRAGMA foreign_keys = ON;

-- 1. Таблица тренировочных сессий
CREATE TABLE IF NOT EXISTS training_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    session_date TEXT NOT NULL,               -- Формат ISO: YYYY-MM-DD
    days_post_injection INTEGER NOT NULL,     -- 0 to 6
    pk_phase TEXT NOT NULL,                   -- 'cmax_peak', 'elimination', 'trough', 'injection'
    session_type TEXT NOT NULL,               -- 'full_body_a', 'full_body_b', 'cardio_deload', 'rest'
    duration_minutes INTEGER DEFAULT 0,
    subjective_energy_score INTEGER,          -- 1 to 5 (утренняя энергия)
    knee_discomfort_flag INTEGER DEFAULT 0,   -- 0 = норма, 1 = дискомфорт в колене
    intra_workout_fueling INTEGER DEFAULT 0,  -- 1 = принят Cluster Dextrin/электролиты
    completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Таблица детальных логов подходов упражнений
CREATE TABLE IF NOT EXISTS exercise_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES training_sessions(id) ON DELETE CASCADE,
    exercise_key TEXT NOT NULL,               -- 'push_up', 'bulgarian_split_squat', 'rdl', etc.
    tier_level INTEGER NOT NULL DEFAULT 2,    -- 1 to 5
    set_index INTEGER NOT NULL,               -- 1, 2, 3
    weight_kg REAL NOT NULL DEFAULT 0.0,
    reps_completed INTEGER NOT NULL,
    rir_actual INTEGER NOT NULL,              -- Reps In Reserve (цель: 1-2)
    tempo_followed TEXT NOT NULL DEFAULT '3-1-1-0',
    knee_pain_score INTEGER DEFAULT 0,        -- 0 (нет боли) до 10 (острая боль)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. Таблица ежедневной кардио- и восстановительной готовности
CREATE TABLE IF NOT EXISTS training_readiness_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    log_date TEXT NOT NULL UNIQUE,            -- YYYY-MM-DD
    resting_hr INTEGER,                       -- ЧСС покоя (уд/мин)
    grip_strength_kg REAL,                    -- Сила хвата (опционально)
    waist_circ_cm REAL,                       -- Окружность талии (см)
    daily_steps INTEGER NOT NULL DEFAULT 0,
    zone2_minutes INTEGER NOT NULL DEFAULT 0,
    orthostatic_dizziness INTEGER DEFAULT 0,  -- 0 = нет, 1 = головокружение при вставании
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Индексы для O(1) выборок в условиях 1 vCPU
CREATE INDEX IF NOT EXISTS idx_sessions_user_date ON training_sessions(user_id, session_date);
CREATE INDEX IF NOT EXISTS idx_exercise_logs_session ON exercise_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_readiness_user_date ON training_readiness_logs(user_id, log_date);
```

---

## 3. СТРУКТУРА МОДУЛЯ В `health_core/training/`

```
health_core/training/
├── __init__.py
├── domain/
│   ├── __init__.py
│   ├── enums.py             # PKPhase, MovementPattern, TierLevel
│   ├── models.py            # Pydantic-модели запросов и логов
│   └── exercise_catalog.py  # Реестр безопасных домашних упражнений
├── services/
│   ├── __init__.py
│   ├── pk_scheduler.py      # Расчет 7-дневного цикла тирзепатида
│   ├── knee_guardrails.py   # Жесткая валидация суставной безопасности
│   ├── cardio_math.py       # Зона 2 Карвонена и пороги шагов Понтцера
│   └── progression_engine.py# Динамическая двойная прогрессия (3-1-1-0)
└── training_service.py      # Оркестратор и работа с БД SQLite
```

---

## 4. КЛЮЧЕВЫЕ КОДОВЫЕ РЕШЕНИЯ И АЛГОРИТМЫ

### 4.1. Реестр безопасных упражнений с суставными гвардрейлами (`exercise_catalog.py`)
```python
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class ExerciseDefinition:
    key: str
    name_ru: str
    tier: int
    movement_pattern: str  # 'horizontal_push', 'knee_dominant', 'hip_hinge', 'vertical_pull', 'horizontal_pull'
    is_knee_safe: bool
    patellar_load_factor: float  # 0.0 (нет нагрузки) до 1.0 (высокая нагрузка)
    default_tempo: str = "3-1-1-0"
    cue_ru: str = ""

EXERCISE_CATALOG = {
    # ГОРИЗОНТАЛЬНЫЙ ЖИМ (Грудь / Трицепс)
    "pushup_incline": ExerciseDefinition("pushup_incline", "Отжимания с упором рук на стол (45°)", 1, "horizontal_push", True, 0.0, "3-1-1-0", "Локти под 45 градусов, корпус в струну"),
    "pushup_floor": ExerciseDefinition("pushup_floor", "Отжимания от пола классические", 2, "horizontal_push", True, 0.0, "3-1-1-0", "Пауза 1 секунда в нижней точке растяжения"),
    "pushup_deficit": ExerciseDefinition("pushup_deficit", "Отжимания с дефицитом (руки на книгах/блоках +10 см)", 3, "horizontal_push", True, 0.0, "3-1-1-0", "Глубокая растяжка грудных, без прогиба в пояснице"),
    "pushup_banded": ExerciseDefinition("pushup_banded", "Отжимания с кольцевой резиновой петлей на спине", 4, "horizontal_push", True, 0.0, "3-1-1-0", "Максимальное напряжение в верхней трети"),

    # КОЛЕНО-ДОМИНАНТНЫЕ (Квадрицепс / Ягодицы) - СТРОГО С ВЕРТИКАЛЬНОЙ ГОЛЕНЬЮ
    "split_squat_supported": ExerciseDefinition("split_squat_supported", "Сплит-присед с упором рукой в стену", 1, "knee_dominant", True, 0.4, "3-1-1-0", "Голень строго вертикальна, колено не уходит вперед"),
    "bulgarian_split_squat_bw": ExerciseDefinition("bulgarian_split_squat_bw", "Болгарский сплит-присед (нога на диване) без веса", 2, "knee_dominant", True, 0.6, "3-1-1-0", "Наклон торса вперед на 20°, вес на передней пятке"),
    "bulgarian_split_squat_db": ExerciseDefinition("bulgarian_split_squat_db", "Болгарский сплит-присед с гантелями", 3, "knee_dominant", True, 0.7, "3-1-1-0", "Опускание 3 сек, заднее колено почти касается пола"),
    "step_up_low": ExerciseDefinition("step_up_low", "Шаги на невысокую платформу (25-30 см)", 2, "knee_dominant", True, 0.5, "3-1-1-0", "Подъем строго силой передней ноги без толчка сзади"),

    # ТАЗОВО-ДОМИНАНТНЫЕ (Бицепс бедра / Ягодицы) - ХАМСТРИНГ К СУСТАВУ = 0 СДВИГА
    "rdl_banded": ExerciseDefinition("rdl_banded", "Румынская тяга с кольцевой резиной", 1, "hip_hinge", True, 0.1, "3-1-1-0", "Отвод таза назад, колени согнуты лишь на 15°"),
    "rdl_dumbbells": ExerciseDefinition("rdl_dumbbells", "Румынская тяга с двумя гантелями", 2, "hip_hinge", True, 0.1, "3-1-1-0", "Гантели скользят по бедрам, спина прямая"),
    "rdl_b_stance": ExerciseDefinition("rdl_b_stance", "B-Stance румынская тяга с гантелями (разножка)", 3, "hip_hinge", True, 0.1, "3-1-1-0", "85% веса на опорной ноге, 3 сек опускание"),
    "glute_bridge_single_leg": ExerciseDefinition("glute_bridge_single_leg", "Одноногий ягодичный мост с пола", 2, "hip_hinge", True, 0.0, "2-2-1-0", "Пауза 2 сек в пиковом сжатии ягодицы, колено под 90°"),

    # ТЯГИ (Спина / Ромбовидные / Бицепс)
    "inverted_row_incline": ExerciseDefinition("inverted_row_incline", "Австралийские подтягивания под 45° на петлях/турнике", 1, "horizontal_pull", True, 0.0, "2-1-1-1", "Сведение лопаток, грудь к рукояткам"),
    "inverted_row_horizontal": ExerciseDefinition("inverted_row_horizontal", "Горизонтальные австралийские подтягивания", 2, "horizontal_pull", True, 0.0, "2-1-1-1", "Тело в струну параллельно полу, фиксация 1 сек"),
    "inverted_row_elevated": ExerciseDefinition("inverted_row_elevated", "Австралийские подтягивания с ногами на стуле", 3, "horizontal_pull", True, 0.0, "2-1-1-1", "Максимальная нагрузка собственного веса"),
    "db_row_one_arm": ExerciseDefinition("db_row_one_arm", "Тяга одной гантели к поясу с упором в диван/стол", 2, "horizontal_pull", True, 0.0, "3-1-1-1", "Упор рукой и коленом разгружает поясницу")
}
```

### 4.2. Фармакокинетический синхронизатор (`pk_scheduler.py`)
```python
from datetime import date
from enum import Enum

class PKPhase(str, Enum):
    INJECTION_DAY = "injection_day"    # День 0: Вечерний укол
    CMAX_PEAK = "cmax_peak"            # Дни 1-2: 24-48 ч пик (задержка ЖКТ, тошнота)
    ELIMINATION = "elimination"        # Дни 3-4: Стабилизация моторики
    TROUGH_NADIR = "trough_nadir"      # Дни 5-6: Надир тирзепатида (пик аппетита и сил)
    PREP_DAY = "prep_day"              # День 7: Подготовка к следующей дозе

class SessionType(str, Enum):
    FULL_BODY_A = "full_body_a"        # День 4
    FULL_BODY_B = "full_body_b"        # День 6
    CARDIO_DELOAD = "cardio_deload"    # Дни 1-2 (только шаги, 0 внутрибрюшного давления)
    REST_MOBILITY = "rest_mobility"    # Дни 0, 3, 7

def get_day_prescription(last_injection_date: date, today: date) -> tuple[int, PKPhase, SessionType, str]:
    delta_days = (today - last_injection_date).days % 7

    if delta_days == 0:
        return (delta_days, PKPhase.INJECTION_DAY, SessionType.REST_MOBILITY, "Вечерняя инъекция. Отдых, 7,000 шагов.")
    elif delta_days in (1, 2):
        return (delta_days, PKPhase.CMAX_PEAK, SessionType.CARDIO_DELOAD, "Пик Cmax тирзепатида. Жесткий запрет на натуживание и наклоны. Только ходьба на дорожке 2.5-3.5 км/ч.")
    elif delta_days == 3:
        return (delta_days, PKPhase.ELIMINATION, SessionType.REST_MOBILITY, "Снижение концентрации. Легкая растяжка, шаги 8,000.")
    elif delta_days == 4:
        return (delta_days, PKPhase.ELIMINATION, SessionType.FULL_BODY_A, "Стабилизация ЖКТ. Силовая тренировка А (35 мин, темп 3-1-1-0).")
    elif delta_days == 5:
        return (delta_days, PKPhase.TROUGH_NADIR, SessionType.REST_MOBILITY, "Восстановление. Подстольная дорожка, закрытие нормы белка 140 г.")
    elif delta_days == 6:
        return (delta_days, PKPhase.TROUGH_NADIR, SessionType.FULL_BODY_B, "Надир препарата! Максимальный ресурс недели. Ключевая силовая тренировка B (RIR 1-2).")
    else:
        return (delta_days, PKPhase.PREP_DAY, SessionType.REST_MOBILITY, "Подготовка к новому циклу. Спокойная ходьба.")
```

### 4.3. Жесткий перехватчик биомеханики колена (`knee_guardrails.py`)
```python
BANNED_EXERCISE_KEYWORDS = (
    "jump", "plyo", "burpee", "run", "box jump", "skipping",
    "deep squat", "pistol", "sissy", "leg extension", "rucking",
    "weighted vest", "back squat", "lunge twist"
)

class KneePathologyViolation(ValueError):
    """Исключение при попытке назначить опасное для ПКС упражнение."""
    pass

def verify_knee_safety(exercise_key: str, knee_complaint: bool) -> str:
    # 1. Проверка стоп-слов
    for word in BANNED_EXERCISE_KEYWORDS:
        if word in exercise_key.lower():
            raise KneePathologyViolation(f"Упражнение {exercise_key} категорически запрещено при переломе колена и разрыве ПКС!")

    # 2. Если у пользователя активная жалоба на колено (дискомфорт утром):
    # Коленно-доминантные упражнения автоматически заменяются на Hip-Hinge
    if knee_complaint:
        if "split_squat" in exercise_key or "step_up" in exercise_key:
            return "glute_bridge_single_leg"  # Безопасная тазовая замена с нулевой нагрузкой на связку
    return exercise_key
```

### 4.4. Калькулятор Зоны 2 и шагов Понтцера (`cardio_math.py`)
```python
def calculate_karvonen_target_hr(resting_hr: int, age: int = 34) -> tuple[int, int]:
    # Формула Танака для HRmax
    hr_max = int(208 - (0.7 * age)) # 184 bpm для 34 лет
    hrr = hr_max - resting_hr       # Резерв ЧСС с учетом медикаментозного сдвига HCN4
    # Зона 2 (FATmax): 60-70% от резерва ЧСС
    z2_low = int(resting_hr + (0.60 * hrr))
    z2_high = int(resting_hr + (0.70 * hrr))
    return (z2_low, z2_high)

def evaluate_pontzer_steps(daily_steps: int) -> dict:
    if daily_steps < 6500:
        return {
            "status": "DEFICIT",
            "message": "Шаги ниже оптимума. Добавьте 15 минут спокойной постпрандиальной ходьбы.",
            "neat_risk": "Low"
        }
    elif 7500 <= daily_steps <= 9000:
        return {
            "status": "OPTIMAL",
            "message": "Идеальный коридор! Максимум окисления жиров без эволюционной компенсации NEAT.",
            "neat_risk": "Optimal"
        }
    elif daily_steps > 12000:
        return {
            "status": "OVERUSE_WARNING",
            "message": "Внимание: при дефиците 1200 ккал объем свыше 12 000 шагов вызовет коллапс спонтанного NEAT и катаболизм. Ограничьте ходьбу!",
            "neat_risk": "High Compensation"
        }
    return {"status": "ACCEPTABLE", "message": "Приемлемый уровень активности.", "neat_risk": "Moderate"}
```

---

## 5. БИЗНЕС-ЛОГИКА: ШАБЛОНЫ СЕССИЙ И ДВОЙНАЯ ПРОГРЕССИЯ

### Сессия А (День 4 после инъекции)
* **Разминка (4 мин):** Вращения в плечах, «кошка-собака», мостик без веса, 10 медленных подъемов на носки.
1. **Отжимания (Push-ups, Tier 2/3):** 3 подхода $\times$ 8–12 повторений | Темп `3-1-1-0` | RIR 1–2 | Отдых 2.0 мин.
2. **Болгарские сплит-приседы (RFESS, Tier 2/3):** 3 подхода $\times$ 8–10 повторений на ногу | Темп `3-1-1-0` | Наклон торса 20°, вертикальная голень | RIR 2 | Отдых 2.5 мин.
3. **Австралийские подтягивания (Inverted Rows, Tier 1/2/3):** 3 подхода $\times$ 8–12 повторений | Темп `2-1-1-1` | Фиксация лопаток 1 сек | RIR 1–2 | Отдых 2.0 мин.
4. **B-Stance румынская тяга (Kickstand RDL, Tier 3):** 3 подхода $\times$ 8–10 повторений на ногу | Темп `3-1-1-0` | 85% веса на передней ноге | RIR 2 | Отдых 2.0 мин.

### Сессия B (День 6 после инъекции)
* **Разминка (4 мин):** Мобильность грудного отдела, мостик, разведение рук с резинкой.
1. **Жим гантелей сидя со спинкой 75–80°:** 3 подхода $\times$ 8–12 повторений | Темп `3-1-1-0` | RIR 1–2 | Отдых 2.0 мин.
2. **Ягодичный мост на одной ноге / с гантелью:** 3 подхода $\times$ 10–12 повторений | Темп `2-2-1-0` | Пауза 2 сек в пике | RIR 1 | Отдых 2.0 мин.
3. **Тяга гантели к поясу с упором (DB Row):** 3 подхода $\times$ 8–12 повторений на руку | Темп `3-1-1-1` | RIR 1–2 | Отдых 2.0 мин.
4. **Шаги на возвышение (Step-ups 25 см):** 3 подхода $\times$ 8–10 повторений на ногу | Темп `3-1-1-0` | Без толчка задней ногой | RIR 2 | Отдых 2.0 мин.

*Суммарный недельный объем:* Ровно по **6 рабочих подходов** на каждую мышечную группу (строгое соответствие норме MED Bickel 2011). Суммарное время нагрузки в неделю: **~70 минут**.

---

## 6. ДЕТАЛЬНЫЙ ТЕСТОВЫЙ ПЛАН (UNIT TESTS)

Тестовый модуль: `tests/test_training_engine.py`

```python
import pytest
from datetime import date, timedelta
from health_core.training.domain.enums import PKPhase, SessionType
from health_core.training.services.pk_scheduler import get_day_prescription
from health_core.training.services.knee_guardrails import verify_knee_safety, KneePathologyViolation
from health_core.training.services.cardio_math import calculate_karvonen_target_hr, evaluate_pontzer_steps

def test_pk_cycle_transitions():
    injection_date = date(2026, 9, 1) # День 0
    
    # День 1 и 2 должны быть Cmax Peak и Cardio Deload
    d1, phase1, session1, _ = get_day_prescription(injection_date, injection_date + timedelta(days=1))
    assert phase1 == PKPhase.CMAX_PEAK
    assert session1 == SessionType.CARDIO_DELOAD

    d2, phase2, session2, _ = get_day_prescription(injection_date, injection_date + timedelta(days=2))
    assert phase2 == PKPhase.CMAX_PEAK
    assert session2 == SessionType.CARDIO_DELOAD

    # День 4 - Силовая А
    d4, phase4, session4, _ = get_day_prescription(injection_date, injection_date + timedelta(days=4))
    assert phase4 == PKPhase.ELIMINATION
    assert session4 == SessionType.FULL_BODY_A

    # День 6 - Силовая B (Надир)
    d6, phase6, session6, _ = get_day_prescription(injection_date, injection_date + timedelta(days=6))
    assert phase6 == PKPhase.TROUGH_NADIR
    assert session6 == SessionType.FULL_BODY_B

def test_knee_safety_hard_interceptor():
    # Запрещенные движения должны бросать исключение
    with pytest.raises(KneePathologyViolation):
        verify_knee_safety("box jump 60cm", False)

    with pytest.raises(KneePathologyViolation):
        verify_knee_safety("rucking with 15kg vest", False)

    with pytest.raises(KneePathologyViolation):
        verify_knee_safety("deep barbell back squat", False)

    with pytest.raises(KneePathologyViolation):
        verify_knee_safety("seated leg extension machine", False)

    # Безопасные движения должны проходить
    assert verify_knee_safety("bulgarian_split_squat_db", False) == "bulgarian_split_squat_db"

    # При жалобе на колено сплит-присед должен автоматически заменяться на ягодичный мост
    safe_swap = verify_knee_safety("bulgarian_split_squat_db", True)
    assert safe_swap == "glute_bridge_single_leg"

def test_karvonen_with_elevated_resting_hr():
    # Проверяем для мужчины 34 лет с пульсом покоя 76 уд/мин (сдвиг +6 на тирзепатиде)
    z2_low, z2_high = calculate_karvonen_target_hr(resting_hr=76, age=34)
    # HRmax = 208 - (0.7 * 34) = 184
    # HRR = 184 - 76 = 108
    # Z2 low = 76 + 0.60 * 108 = 76 + 64 = 140
    # Z2 high = 76 + 0.70 * 108 = 76 + 75 = 151
    assert z2_low == 140
    assert z2_high == 151

def test_pontzer_compensation_guardrails():
    opt = evaluate_pontzer_steps(8200)
    assert opt["status"] == "OPTIMAL"

    over = evaluate_pontzer_steps(14000)
    assert over["status"] == "OVERUSE_WARNING"
    assert over["neat_risk"] == "High Compensation"
```

---

## 7. РИСКИ, КРАЕВЫЕ СЛУЧАИ И ПЛАН ОТКАТА (ROLLBACK STRATEGY)

### Краевые случаи и их компенсация:
1. **Сдвиг дня укола (например, задержал инъекцию на 2 дня):**
   * Система не привязана к дням недели (понедельник/среда), а отсчитывает фазу strictly от `last_injection_date` в таблице инъекций. При переносе укола весь тренировочный цикл сдвигается автоматически.
2. **Острая ортостатическая гипотензия при вставании:**
   * Если пользователь отмечает в боте головокружение (`orthostatic_dizziness = 1`), бот немедленно выводит памятку: «Отдых сидя, досолить воду (+1 г соли), исключить переход в стойку без 15-секундной подготовки стопами».
3. **Падение веса тела и пересчет белка:**
   * По мере снижения веса со 121 кг норма белка пересчитывается не от общей массы (чтобы не перегружать ЖКТ), а строго от тощей массы: $1.8 \times 79\text{ кг} \approx 142\text{ г/сут}$.

### Стратегия отката (Rollback):
Миграция `005_training_engine.sql` является чисто аддитивной (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`).  
В случае необходимости деактивации модуля:
1. Флаг `TRAINING_MODULE_ENABLED = False` в `config.yaml`.
2. Бот скрывает кнопки раздела «Тренировки» в меню и возвращается к исходному нутрициологическому режиму.
3. Откат БД (при необходимости) выполняется тривиальным дропом трех добавленных таблиц:
   `DROP TABLE IF EXISTS exercise_logs; DROP TABLE IF EXISTS training_sessions; DROP TABLE IF EXISTS training_readiness_logs;`

---
*Документ утвержден Коллегией планировщиков и Арбитражным судьей 2026-09-13.*
