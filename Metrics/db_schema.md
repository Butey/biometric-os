# DB SCHEMA: BODY_METRICS

## Table: metrics
- **Format:** SQL-compatible (SQLite/PostgreSQL).
- **Timezone:** UTC+5 / Local (consistent).

## Fields Mapping:
- `Вес(kg)` -> `weight_kg`
- `Содержание жира(%)` -> `fat_pct`
- `Мышечная масса(kg)` -> `muscle_mass_kg`
- `Висцеральный жир` -> `visceral_fat`
- `Скорость обмена веществ(kcal)` -> `bmr_kcal`

## Processing Rules:
1. Replace "- -" with NULL.
2. Convert Date to ISO 8601.
3. Keep all records (including evening data) to track water retention.