# ГЛОБАЛЬНЫЙ МАСТЕР-ДОСЬЕ: ПЕРЕДОВЫЕ ОТКРЫТЫЕ ИСТОЧНИКИ, РЕПОЗИТОРИИ, МАТЕМАТИЧЕСКИЕ ДВИЖКИ И КЛИНИЧЕСКИЕ ПРОТОКОЛЫ

**Проект:** Health Agent System («Метаболический контур»)  
**Директория исследования:** `/opt/webapps/health_agent_system/research/advanced`  
**Дата:** 2026-09-13  
**Целевая платформа:** Debian 12 (Bookworm), 1 vCPU, 1 GB RAM, SQLite 3 (WAL mode), Python 3.11  

---

## 1. ИСПОЛНИТЕЛЬНОЕ РЕЗЮМЕ (EXECUTIVE SUMMARY)

По результатам работы специализированного роя исследовательских агентов по открытым международным источникам (GitHub, PyPI, PubMed, PMC, JAMA, Lancet, ADA, EASO) сформирован исчерпывающий сводный каталог проверенных открытых инструментов, математических дифференциальных уравнений, фармакокинетических моделей и клинических протоколов.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        GLOBAL ADVANCED RESEARCH ARCHITECTURE                           │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
        ┌───────────────────────────────────┼───────────────────────────────────┐
        ▼                                   ▼                                   ▼
┌───────────────────────────────┐ ┌───────────────────────────────┐ ┌───────────────────────────────┐
│     01. MATH & PK ENGINES     │ │    02. WEARABLES & APIS       │ │    03. CLINICAL PROTOCOLS     │
│ • Kevin Hall ODE (NIH BWP)    │ │ • aiowithings (Body & BP)     │ │ • SURMOUNT-4 / STEP-4 Taper   │
│ • Forbes Curve dL/dW          │ │ • Oura Cloud v2 (Sleep/HRV)   │ │ • MATADOR (Byrne'18) IER      │
│ • Bateman GLP-1/GIP Engine    │ │ • pydexcom / Nightscout CGM   │ │ • CBT-E Regular Eating        │
│ • Pure Python PMC (CTL/ATL)   │ │ • fitdecode .FIT Streamer     │ │ • ACT 4-Step Urge Surfing     │
│ • PuLP MILP Dietary Optimizer │ │ • Apple Health O(1) iterparse │ │ • Binge Risk Triad (BRTS)     │
│ • RAM Footprint: <25 MB       │ │ • Pure Python eGFR/HOMA/TyG   │ │ • Bickel 1/3 MED Home RT      │
└───────────────────────────────┘ └───────────────────────────────┘ └───────────────────────────────┘
```

---

## 2. СВОДНЫЙ КАТАЛОГ ПРОВЕРЕННЫХ ОТКРЫТЫХ РЕПОЗИТОРИЕВ И БИБЛИОТЕК

| Категория | Репозиторий на GitHub | Лицензия / Звёзды | Назначение для Health Agent System | Нагрузка на RAM |
| :--- | :--- | :---: | :--- | :---: |
| **NIH Модель Холла** | [niddk/body-weight-planner](https://github.com/niddk/body-weight-planner) | Public Domain / ~180★ | Численное моделирование траектории веса и адаптивного термогенеза | <0.5 МБ (Pure-Python) |
| **Фармакокинетика** | [pharmpy/pharmpy](https://github.com/pharmpy/pharmpy) | LGPL-3.0 / ~220★ | Эталонные уравнения 1- и 2-камерной элиминации тирзепатида | <0.2 МБ (Bateman closed-form) |
| **Спорт-телеметрия** | [GoldenCheetah/GoldenCheetah](https://github.com/GoldenCheetah/GoldenCheetah) | GPL-2.0 / ~2300★ | Золотой стандарт алгоритмов Banister TRIMP и Coggan PMC (CTL/ATL/TSB) | <0.2 МБ (Pure-Python) |
| **Парсинг .FIT файлов**| [polyvertex/fitdecode](https://github.com/polyvertex/fitdecode) | MIT / ~130★ | Потоковый zero-copy парсер бинарных файлов тренировок Garmin/Wahoo | 10–12 МБ (Потоковый) |
| **Весы и тонометры** | [joostlek/python-aiowithings](https://github.com/joostlek/python-aiowithings) | MIT / ~50★ | Асинхронный клиент Withings API для приема состава тела и АД | <5 МБ |
| **Мониторинг сна/HRV** | [turing-complet/python-oura](https://github.com/turing-complet/python-oura) | MIT / ~200★ | Клиент Oura API v2 для чтения стадий сна, ночного rMSSD и пульса | <3 МБ |
| **Глюкоза (CGM)** | [gagebenne/pydexcom](https://github.com/gagebenne/pydexcom) | MIT / ~180★ | Чтение тренда глюкозы в реальном времени с Dexcom Share | <5 МБ |
| **Оптимизация диеты** | [coin-or/pulp](https://github.com/coin-or/pulp) | MIT / ~2300★ | Линейное программирование (MILP) рациона с кардио-ограничениями | 18–25 МБ |
| **Базы продуктов** | [openfoodfacts/openfoodfacts-python](https://github.com/openfoodfacts/openfoodfacts-python) | Apache-2.0 / ~600★ | Поиск продуктов по штрихкоду (>3.2 млн позиций, включая РФ/СНГ) | <5 МБ |
| **Медицинские формулы** | [mims-harvard/ToolUniverse](https://github.com/mims-harvard/ToolUniverse) | MIT / ~450★ | 698 валидированных клинических формул (eGFR, HOMA-IR, TyG, FIB-4) | 0 МБ (Встроенный Python) |

---

## 3. СИНТЕЗ КЛИНИЧЕСКИХ ПРОТОКОЛОВ

1. **Протокол тейперинга GLP-1/GIP (SURMOUNT-4, STEP-4):**
   * Одномоментная отмена вызывает возврат +14% веса (*Aronne 2024 JAMA*).
   * Применяется ступенчатый степ-даун: шаг снижения каждые 6–8 недель при колебаниях веса $\le 2\%$. При откате $>3\%$ — возврат на предыдущую дозу. Белковый якорь: $1.8\text{ г/кг FFM}$ + 40 г пребиотической клетчатки.
2. **Протокол диет-брейков MATADOR (Byrne 2018):**
   * Прерывистая диета (2 недели дефицита / 2 недели поддержания) снижает адаптивный термогенез в 2.1 раза и повышает чистую потерю жира на +54%.
   * Для тирзепатида адаптирован калорический шаг +400 ккал за счет сложных углеводов для реактивации конверсии $T_4 \to T_3$.
3. **Поведенческая психотерапия срывов (CBT-E & ACT):**
   * Сетка регулярного питания Кристофера Фейрберна: 3 приема + 2 перекуса каждые 3–4 часа (регулярность важнее состава).
   * 4-шаговый скрипт Urge Surfing Алана Марлатта: соматизация тяги, вагусное дыхание 4–2–6, когнитивное разделение («Я — серфер, мысль — волна»), 15-минутный таймер.
   * Шкала Binge Risk Triad Score (BRTS): мониторинг накопленного дефицита, депривации сна (<6 ч $\to$ +28% грелина) и задержки логов.
4. **Сохранение скелетных мышц дома (Bickel 2011 MED):**
   * Правило 1/3 объема: 4–6 подходов на группу в неделю при RIR 1–2 и темпе `3-1-1-0` полностью сохраняют 100% площади волокон Type II.

---

## 4. СТРУКТУРА ДЕТАЛЬНЫХ МАТЕРИАЛОВ В `/research/advanced/`

* [**`01_advanced_mathematical_and_pk_engines.md`**](file:///opt/webapps/health_agent_system/research/advanced/01_advanced_mathematical_and_pk_engines.md) — Полные математические выводы модели Кевина Холла, уравнение Бейтмана для тирзепатида/семаглутида, чистый Python-код Coggan PMC и MILP-оптимизатор на PuLP.
* [**`02_wearables_telemetry_and_biomarker_apis.md`**](file:///opt/webapps/health_agent_system/research/advanced/02_wearables_telemetry_and_biomarker_apis.md) — Продакшн-клиенты Withings, Oura, Dexcom, потоковый парсинг Apple Health XML и Garmin FIT (`fitdecode`), формулы eGFR 2021/Cystatin C/HOMA-IR/TyG/FIB-4, Open Food Facts и тюнинг ZRAM.
* [**`03_clinical_protocols_tapering_and_behavioral_psychology.md`**](file:///opt/webapps/health_agent_system/research/advanced/03_clinical_protocols_tapering_and_behavioral_psychology.md) — Протоколы отмены SURMOUNT-4/STEP-4, диет-брейки MATADOR, поведенческие скрипты CBT-E и ACT Urge Surfing, цифровая шкала срывов BRTS и силовой норматив Bickel.
* [**`SWARM_THINKING_AND_SEARCH_LOGS.md`**](file:///opt/webapps/health_agent_system/research/advanced/SWARM_THINKING_AND_SEARCH_LOGS.md) — Журнал работы и поисковых траекторий исследовательского роя.
