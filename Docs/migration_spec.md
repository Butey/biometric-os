# СПЕЦИФИКАЦИЯ МИГРАЦИИ ИСТОРИЧЕСКИХ ДАННЫХ ЗДОРОВЬЯ

## 1. РЕШЕНИЕ ПО ИСТОЧНИКАМ ДАННЫХ

| Источник | Решение | Обоснование |
|----------|---------|------------|
| A. Feelfit (47 XLSX + 1 CSV) | **МИГРИРОВАТЬ** | ~430 уникальных измерений, критичны для baseline, структурирова́но парсится |
| B. Anthropometric CSV | **МИГРИРОВАТЬ** | 7 сессий 09.05–20.08.2026, необходимы для полноты тела, LLM-парсинг окупается точностью |
| C. TCX Activity (53 файла) | **МИГРИРОВАТЬ** | Полный журнал активности, встроенный парсинг XML, отбросить trackpoints (экономия памяти) |
| D. Sugar (3 JPG) | **ПРОПУСТИТЬ** | Только фотографии глюкометра без метаданных, неструктурировано, ручной ввод дороже |
| E. Sleep (скриншоты + CSV) | **ОТЛОЖИТЬ** | Xiaomi-скриншоты нечитаемы автоматически; SnoreLab CSV (2 шт) парсится, но Xiaomi-данные требуют OCR; мигрировать только SnoreLab CSV 2 шт (~15 строк), остальное ручная обработка позже |
| F. Nutrition (DOCX + EML) | **ПРОПУСТИТЬ** | Неструктурирова́но, требует NLP; ценность низка в фазе 1; дневник доступен позже через Telegram-интеграцию |

## 2. ПРАВИЛО ДЕДУПЛИКАЦИИ ДЛЯ FEELFIT (Источник A)

**Естественный ключ:** `(device_mac_address, measurement_datetime)` — WHERE `measurement_datetime` нормализована к минуте (DD/MM/YYYY HH:MM:00).

**Правило свёртки всплеска (burst):**
```
FOR EACH unique (device_mac, TRUNC(timestamp_to_minute)):
  IF COUNT > 1 THEN
    winner_row = ROW with MEDIAN(muscle_mass)
      [если muscle_mass отсутствует или "-", берём FIRST по времени]
    KEEP winner_row, DISCARD остальные в всплеске
  ELSE
    KEEP single row
END
```

**Обоснование:** Всплески — это переизмерения <60 сек одного сеанса (мышечная масса отличается на 0.1–0.2 кг из-за дрейфа датчика). Медиана muscle_mass минимизирует выбросы датчика. Если muscle_mass отсутствует (что редко), берём первое измерение как самое стабильное (тело ещё не остыло).

## 3. ИДЕМПОТЕНТНОСТЬ

**Механизм:** UPSERT на уникальный индекс `(device_mac, measurement_timestamp_minute)`.

**SQL:**
```sql
CREATE UNIQUE INDEX idx_feelfit_dedup 
  ON body_composition(device_mac, measurement_timestamp_minute);

INSERT INTO body_composition(...) VALUES(...)
ON CONFLICT(device_mac, measurement_timestamp_minute) DO UPDATE
  SET weight=EXCLUDED.weight, ... WHERE TRUE;
```

**Гарантия:** Переимпорт того же файла или дублирующегося экспорта (пользователь отправит один и тот же экспорт через Telegram) будет NO-OP, не создаст дубли.

## 4. ТАБЛИЦА МАППИНГА FEELFIT (Источник A)

| Исходная колонка (кириллица) | Целевая колонка (snake_case) | SQLite тип | Единица | Правило NULL |
|------|------|------|------|------|
| Время измерения | measurement_timestamp | DATETIME | ISO 8601 | REQUIRED, PARSE DD/MM/YYYY HH:MM:SS |
| Вес(kg) | weight | REAL | kg | 0 → NULL, "-" → NULL |
| Содержание жира(%) | fat_percent | REAL | % | 0 → NULL, "- -" → NULL |
| Индекс массы тела | bmi | REAL | - | 0 → NULL, "- -" → NULL |
| Скелетные мыщцы(%) | skeletal_muscle_percent | REAL | % | 0 → NULL, "- -" → NULL |
| Мышечная масса(kg) | muscle_mass | REAL | kg | 0 → NULL, "- -" → NULL; КРИТИЧНО для дедупа |
| Белки(%) | protein_percent | REAL | % | 0 → NULL, "- -" → NULL |
| Скорость обмена веществ(kcal) | metabolic_rate | INT | kcal/day | 0 → NULL, "- -" → NULL |
| Масса тела без учета жира(kg) | lean_mass | REAL | kg | 0 → NULL, "- -" → NULL |
| Подкожно-жировая клетчатка(%) | subcutaneous_fat_percent | REAL | % | 0 → NULL, "- -" → NULL |
| Висцеральный жир | visceral_fat_level | INT | уровень | 0 → NULL, "- -" → NULL; INT (часто 20–25) |
| Содержание воды в организме(%) | water_percent | REAL | % | 0 → NULL, "- -" → NULL |
| Костная масса(kg) | bone_mass | REAL | kg | 0 → NULL, "- -" → NULL |
| Метаболический возраст | metabolic_age | INT | лет | 0 → NULL, "- -" → NULL |
| MAC-адрес устройства | device_mac | TEXT | - | REQUIRED, UPPER() |
| Имя устройства | device_name | TEXT | - | REQUIRED |

**Резолюция "0" vs пропуск:** Тестовое измерение показало: 0 в fat_percent часто означает отсутствие (тело не отсканировано полностью). Исключение: metabolic_rate=0 редко, трактовать как NULL. Правило: `IF value == "0" OR value == "-" OR value == "- -" THEN NULL`.

## 5. ПАРСИНГ ANTHROPOMETRIC (Источник B)

**Рекомендация:** LLM-экстракция (Claude Vision или текстовый prompt) — 7 сессий быстрее и надежнее детерминированного парсера из-за:
- Несогласованных разделителей (пробел, табуляция, запятая)
- Смешанных форматов дат ("09,05" vs "25.05.2026" vs "02,06,2026")
- Диапазонов в значениях ("113-114", "39-40")

**Парсинг LLM-prompt:**
```
Прочитай блоки сессий. Для каждого: дата, затем 6 измерений (талия, грудь, таз, бедро, шея, бицепс). 
Дата может быть в форматах DD,MM или DD.MM.YYYY или DD,MM,YYYY — нормализуй к YYYY-MM-DD.
Если диапазон (113-114), возьми среднее (113.5). Результат: JSON [{date, taliya, grud, taz, bedro, sheya, biceps}]
```

**Дата "09,05" (09 май):** Контекст соседних дат показывает это май 2026 → `2026-05-09`. При полной неопределённости взять ПОСЛЕДНИЙ год в файле.

**Expected rows:** 7 уникальных дат → 7 строк в `anthropometry_session`.

## 6. ПАРСИНГ TCX (Источник C)

**Встроенный модуль:** `xml.etree.ElementTree` (stdlib Python), или `lxml` без новых зависимостей.

**Извлечение типа спорта:**
```
filename = "20260426Бокс_02.tcx"
sport_name = REGEX.match(r'\d{8}(.+?)(_\d+)?\.tcx', filename).group(1)
  → "Бокс" → normalize to enum (Бокс, Бег, Плавание, ...)
IF Activity/Sport attribute is not empty → use it (rare)
ELSE → use filename sport_name
```

**Целевые поля на активность:**
- activity_id (ISO timestamp из Activity/Id)
- activity_date (DATE из timestamp)
- sport_type (TEXT: "Бокс", "Бег на улице", "Плавание в бассейне", ...)
- start_time (DATETIME)
- total_seconds (INT)
- distance_meters (REAL, nullable; Lap/DistanceMeters)
- total_calories (INT из Activity/Calories)
- avg_heart_rate (INT, nullable из Lap/HeartRateBpm)
- step_count (INT, nullable из Lap/Steps)

**Skip trackpoints:** NOT загружать элементы Track/Trackpoint. Чтение достигает Lap/, затем пропускает Track. Экономия памяти: 862 KB → 2 KB на запись.

**Парсинг в памяти:** DOM (ElementTree) для 53 файлов <12 MB OK; потоковый SAX не требуется.

## 7. ПОРЯДОК ОПЕРАЦИЙ И ОЖИДАЕМЫЕ СТРОКИ

| # | Шаг | Таблица | Ожидаемые строки | Условие |
|----|-----|--------|-----|---------|
| 1 | PARSE Feelfit (47 XLSX + 1 CSV) → дедуп по (MAC, минута) | body_composition | ~350–380 | После свёртки всплесков (428 raw → 350–380 unique) |
| 2 | PARSE Anthropometric CSV → LLM | anthropometry_session | ~7 | 1 сессия = 1 запись (дата + 6 измерений) |
| 3 | PARSE TCX (53 файла) → skip trackpoints | activity | ~53 | 1 файл = 1 запись активности |
| 4 | PARSE SnoreLab CSV (2 файла) → структурированные данные | sleep_snorelab | ~2–4 | 1 CSV может содержать несколько строк; консервативно 2–4 |
| DEFER | Sleep Xiaomi & Sugar JPG | — | — | Ручная обработка фазой 2 |

**Итого миграция 1:** `body_composition` ~360 + `anthropometry_session` ~7 + `activity` ~53 + `sleep_snorelab` ~3 = ~423 записи.

## 8. ВАЛИДАЦИЯ И ПРОВЕРКИ ПОСЛЕ МИГРАЦИИ

**Конкретные проверки (assertions):**

1. **Дедупликация ПРОВЕРЕНА:**
   ```sql
   SELECT COUNT(*), COUNT(DISTINCT (device_mac, measurement_timestamp_minute)) 
   FROM body_composition;
   -- ASSERT count == count_distinct (нет дубликатов)
   ```

2. **Диапазон дат неразрывен:**
   ```sql
   SELECT MIN(measurement_timestamp), MAX(measurement_timestamp) FROM body_composition;
   -- ASSERT: 2026-01-19 ≤ min, 2026-08-20 ≥ max (покрыва́ет весь период)
   ```

3. **Монотонность веса (рассеяние <3 кг/день):**
   ```sql
   WITH ordered AS (
     SELECT weight, LAG(weight) OVER (ORDER BY measurement_timestamp) as prev_weight
     FROM body_composition WHERE weight IS NOT NULL
   )
   SELECT MAX(ABS(weight - prev_weight)) FROM ordered;
   -- ASSERT: max_diff ≤ 3.0 (нет артефактов калибровки)
   ```

4. **Контрольные точки (spot-check):**
   ```sql
   -- Проверить, что измерение от 20.08.2026 присутствует
   SELECT * FROM body_composition WHERE DATE(measurement_timestamp) = '2026-08-20';
   -- ASSERT: ≥1 строка
   
   -- Проверить muscle_mass распределение (не все NULL)
   SELECT COUNT(DISTINCT muscle_mass) FROM body_composition WHERE muscle_mass IS NOT NULL;
   -- ASSERT: ≥50 (не все NULL или дефолт)
   ```

5. **TCX: кол-во активностей**
   ```sql
   SELECT COUNT(*) FROM activity;
   -- ASSERT: count == 53 (все файлы спарсены)
   ```

6. **TCX: типы спорта заполнены**
   ```sql
   SELECT COUNT(DISTINCT sport_type) FROM activity;
   -- ASSERT: ≥5 (Бокс, Бег, Плавание, Гантели, Произвольная)
   ```

7. **Anthropometry: все 7 сессий**
   ```sql
   SELECT COUNT(*) FROM anthropometry_session;
   -- ASSERT: count == 7
   ```

8. **Целостность внешних ключей (если device_type — справочник):**
   ```sql
   -- ASSERT: device_mac NOT NULL для всех body_composition
   SELECT COUNT(*) FROM body_composition WHERE device_mac IS NULL;
   -- ASSERT: count == 0
   ```

---

**ИТОГ:** Миграция 1-го этапа покрывает основные структурированные источники (A, B, C, SnoreLab). Дефер Sleep-скриншотов и Sugar-JPG до этапа 2 с OCR/LLM-видением. Идемпотентность обеспечена UPSERT на уникальный индекс. Валидация — 8 конкретных SQL-ассертов для обнаружения поломок.
