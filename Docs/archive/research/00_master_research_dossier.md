# МАСТЕР-ДОСЬЕ И СИНТЕЗ ИССЛЕДОВАНИЙ: HEALTH AGENT SYSTEM («МЕТАБОЛИЧЕСКИЙ КОНТУР»)

**Оркестратор:** Deep Research Swarm Orchestrator  
**Дата:** 2026-09-13  
**Целевая платформа:** Debian Linux 12, 1 vCPU, 1 GB RAM, 2 GB Swap, Python 3.11, SQLite (WAL mode)  
**Клинический фокус:** Автономный Telegram-ассистент снижения веса, сохранения тощей массы (LBM) и метаболической оптимизации для пациента на терапии тирзепатидом (GIP/GLP-1).

---

## 1. Исполнительное резюме (Executive Summary)

Рой из 5 специализированных исследовательских агентов провел независимое глубокое исследование по ключевым направлениям развития проекта:

```
                               ┌────────────────────────────────────────────────────────┐
                               │           DEEP RESEARCH SWARM SYNTHESIS                │
                               └───────────────────────────┬────────────────────────────┘
                                                           │
         ┌──────────────────┬──────────────────────────────┼─────────────────────────────┬──────────────────┐
         ▼                  ▼                              ▼                             ▼                  ▼
┌──────────────────┐┌──────────────────┐        ┌──────────────────────┐        ┌──────────────────┐┌──────────────────┐
│  01. PLUGINS &   ││  02. SKILLS &    │        │ 03. CLINICAL MODELS  │        │ 04. OPEN SOURCE  ││ 05. BEHAVIORAL   │
│  HARDWARE APIS   ││  ARCHITECTURES   │        │ & ENDOCRINOLOGY      │        │ ENGINES (MATH)   ││ PSYCHOLOGY (ADR) │
│ • OpenFoodFacts  ││ • ToolUniverse   │        │ • Hall / Forbes model│        │ • Pure Python PMC││ • CBT-E Protocols│
│ • Withings / Oura││ • Open-Medica    │        │ • Pontzer Compensat. │        │ • Tirzepatide PK ││ • ACT Defusion   │
│ • Dexcom / CGM   ││ • Dual-System    │        │ • Alpert 69.3 kcal/kg│        │ • PuLP Optimizer ││ • Binge Triad    │
│ • PyMuPDF+Vision ││ • Pydantic V2    │        │ • MATADOR (Byrne'18) │        │ • fitdecode / XML││ • Whoosh effect  │
└──────────────────┘└──────────────────┘        └──────────────────────┘        └──────────────────┘└──────────────────┘
```

### Ключевые выводы исследования:

1. **Жесткий аппаратный инвариант (1 vCPU / 1 GB RAM):**
   - Доступный бюджет свободной памяти для расширений — **не более 200–300 МБ**.
   - Попытка запуска локальных нейросетей (PaddleOCR, Nougat, Whisper, PyTorch, Spacy, YASA) гарантированно вызывает OOM Killer и падение Telegram-бота.
   - **Единственно верная стратегия:** детерминированная арифметика на чистом Python внутри процесса ядра + асинхронные HTTP-клиенты + делегирование тяжелых мультимодальных задач (фото тарелок, сканы анализов) внешним облачным Vision API ($0.001 за вызов).

2. **Ликвидация критических дефектов ядра:**
   - *Инверсия знака в `adaptive_tdee`* (`health_core/energy.py:201`): `mean_intake - (delta_weight * 7700 / window_days)`.
   - *Исправление константы Альперта*: замена `31 ккал/кг` (ошибка перевода из фунтов) на валидированные $69.3 \pm 6.0\text{ ккал/кг жировой массы/сутки}$ с коэффициентом безопасности 0.7 ($48.5\text{ ккал/кг}$).
   - *Половая дифференциация гардрейлов*: введение раздельных порогов `FFMI_FLOOR` (19.0 для мужчин, 15.0 для женщин) и `ESSENTIAL_FAT_PCT` (5% для мужчин, 12% для женщин).
   - *Направление дельты в LBM-гардрейлах*: проверка `if d_lean < 0:` во избежание ложных алертов при росте мышечной массы.

3. **Внедрение топ-приоритетных скиллов:**
   - **Binge Risk Triad Score (BRTS):** проактивный расчет угрозы срыва на основе накопленного дефицита, сна и белка на завтрак.
   - **Tirzepatide Pharmacokinetics (PK Engine):** 1-камерная аналитическая модель с $t_{1/2} = 5$ суток, рассчитывающая текущую концентрацию в крови, пик ЖКТ-нагрузки (24–48 ч) и окно рефида (дни 6–7).
   - **Лабораторные биомаркеры (log_labs):** HOMA-IR, TyG Index, eGFR (CKD-EPI 2021) и липидные коэффициенты через Pydantic-контракты.

---

## 2. Сводная матрица исследовательских направлений

| Направление | Исследованные источники и библиотеки | Ключевые формулы и стандарты | Нагрузка на RAM | Статус для Health Agent System |
|---|---|---|---|---|
| **01. Плагины и API** | `openfoodfacts-python`, USDA FDC, Withings API, Oura v2, Dexcom Share (`pydexcom`), PyMuPDF (`fitz`), Cloud Vision | EAN-13, NOVA 4, TIR (3.9–7.8 ммоль/л), CV гликемии <36% | <25 МБ суммарно | 🟢 **Готово к интеграции** (HTTP async) |
| **02. Скиллы и Архитектура** | Harvard `ToolUniverse` (698), `Open-Medica` (747), `OpenClaw` (869), `Instructor`, NeMo Guardrails | HOMA-IR, TyG, eGFR CKD-EPI 2021, Cockcroft-Gault ABW | 0 МБ (чистый Python) | 🟢 **Архитектура утверждена** (Dual-System) |
| **03. Клинические модели** | SURMOUNT-1, STEP-1, Kevin Hall (NIH 2011), Herman Pontzer (Science 2021), Seymour Alpert (2005), MATADOR | $\rho_F \frac{dF}{dt} + \rho_L \frac{dL}{dt} = \Delta I$, $\alpha(F) = \frac{10.4}{10.4+F}$, $k=69.3$ ккал/кг жира/сут | 0 МБ | 🟢 **Калибровка ядра** (energy.py, guards.py) |
| **04. Вычислительные движки** | GoldenCheetah, Banister TRIMP, `fitdecode`, Pure-Python HRV (RMSSD/SDNN), `PuLP` CBC, Tirzepatide PK | $P(t) = \text{Fitness} - \text{Fatigue}$, $C(t) = \text{Analytic closed-form}$, MILP simplex | 15–25 МБ | 🟢 **Модули разработаны** (`pmc.py`, `pk.py`) |
| **05. Поведенческая терапия** | CBT-E (Fairburn), ACT Urge Surfing (Marlatt), OARS (Miller & Rollnick), Spiegel Sleep Debt | $\text{BRTS} = 0.4 S_{\text{def}} + 0.3 S_{\text{sleep}} + 0.3 S_{\text{beh}}$, Logging Latency $\Delta t_{\text{log}}$ | 0 МБ | 🟢 **Внедрение в Core** (промпты + cron) |

---

## 3. Архитектурные решения и детерминированные контракты (ADRs)

### ADR-01: Dual-System Architecture (Запрет арифметики в LLM)
* **Контекст:** Языковые модели оперируют BPE-токенами, галлюцинируют константы формул и накапливают 15–30% погрешности при сложении порций еды. В фармакотерапии и расчете почечного клиренса ошибка в разряд смертельно опасна.
* **Решение:** Вся арифметика, сравнения с порогами, применение гардрейлов и запись в БД выполняются строго детерминированным кодом Python 3.11 с валидацией через Pydantic V2. LLM выполняет только NLU (извлечение параметров) и синтез поддерживающего ответа на основе детерминированного JSON-ответа ядра.

### ADR-02: Микросервисная изоляция vs In-Process Python
* **Контекст:** Использование внешних MCP-серверов на Node.js или запуск тяжелых Python-пакетов в едином процессе приводит к утечкам кучи (`pymalloc`) и OOM на сервере с 1 GB RAM.
* **Решение:** Все стандартные интеграции (OpenFoodFacts, весы, фармакокинетика, PMC) встраиваются как легковесные модули `health_core/` прямо в память процесса бота (<15 МБ RAM). Если требуется ресурсоемкий разовый расчет (MILP-оптимизация PuLP или парсинг XML), он выносится в изолированный эфемерный подпроцесс (`subprocess.run`), чья память гарантированно освобождается ядром ОС.

### ADR-03: Исправление математики модели Альперта
* **Контекст:** В `config.yaml` и `health_core/energy.py` указана величина `fat_supply_kcal_per_kg: 31`. Это артефакт ошибки перевода 31.4 ккал на 1 **фунт** жира. Истинная константа Сеймура Альперта составляет $290 \text{ кДж/кг} \approx 69.3 \text{ ккал/кг жира/сутки}$.
* **Решение:** Обновить `fat_supply_kcal_per_kg` до 69.3, сохранив коэффициент безопасности `fat_supply_safety: 0.70`. Это дает безопасный суточный дефицит $48.5 \times M_{\text{fat}}$, предотвращая ложное обрушение калоража у пациентов с большим жировым депо.

---

## 4. Спецификация приоритетного роадмапа внедрения

```
Этап 1 (Hotfix): Устранение дефектов безопасности ядра
├── Исправление знака adaptive_tdee: mean_intake - (delta_w * 7700 / days)
├── Исправление LBM_RATIO / LBM_DRIFT (проверка d_lean < 0)
├── Разделение FFMI_FLOOR и ESSENTIAL_FAT_PCT по биологическому полу
└── Исправление константы Alpert fat_supply (69.3 kcal/kg)

Этап 2 (Core Skills): Проактивная защита и кинетика
├── Интеграция модуля Binge Risk Triad Score (BRTS) в scripts/notify.py
├── Внедрение 1-камерного движка TirzepatidePK в health_core/pk.py
└── Обновление системного промпта Core/system_promt.md (OARS, Urge Surfing, Whoosh)

Этап 3 (Ingestion & Integrations): Внешние источники данных
├── Легковесный клиент Open Food Facts (поиск по EAN штрихкоду)
├── Асинхронный вебхук Withings / Shortcuts для автоматического взвешивания
└── Парсинг бланков через PyMuPDF (векторный текст) + Cloud Vision LLM API

Этап 4 (Advanced Telemetry & Analytics): Лаборатория и оптимизация
├── Модуль log_labs (HOMA-IR, TyG, eGFR CKD-EPI 2021, Lipids)
├── Движок PMC (CTL, ATL, TSB, Banister TRIMP) на чистом Python
└── Генератор корзины питания на базе PuLP (MILP) с учетом лимита насыщенных жиров
```

---

## 5. Документы детальных отчетов

Полные тексты исследований узкопрофильных агентов размещены в следующих файлах каталога `/research`:
* **[01_agent_plugins_hardware_api.md](file:///opt/webapps/health_agent_system/research/01_agent_plugins_hardware_api.md)** — Плагины, штрих-коды, весы, носимые трекеры, CGM, OCR и аудит RAM.
* **[02_agent_skills_and_frameworks.md](file:///opt/webapps/health_agent_system/research/02_agent_skills_and_frameworks.md)** — Каталоги ИИ-скиллов, Dual-System архитектура, Pydantic V2 контракты и Guardrails.
* **[03_agent_clinical_literature_and_models.md](file:///opt/webapps/health_agent_system/research/03_agent_clinical_literature_and_models.md)** — Эндокринология GLP-1, модели Холла, Понтцера, Альперта, MATADOR и саркопения.
* **[04_agent_opensource_engines_and_libraries.md](file:///opt/webapps/health_agent_system/research/04_agent_opensource_engines_and_libraries.md)** — Готовые библиотеки, код фармакокинетики, PMC-движок, PuLP и ZRAM.
* **[05_agent_behavioral_psychology_and_adherence.md](file:///opt/webapps/health_agent_system/research/05_agent_behavioral_psychology_and_adherence.md)** — CBT-E, ACT, OARS, предикторы срывов, шкала BRTS и эффект Whoosh.
