# Архив материалов и планов сессий Gemini от 14.09.2026

Данная папка содержит структурированный архив всех рабочих материалов, исследовательских отчётов, планов архитектурных улучшений и карты проекта за сессии **14 сентября 2026 года**. Все материалы разложены по тематическим подпапкам.

Для удобства навигации в корне репозитория настроен симлинк `gemini -> gemini_edited`, поэтому пути `gemini/14092026/` и `gemini_edited/14092026/` полностью эквивалентны.

---

## 📁 Структура архива

```
gemini_edited/14092026/
├── 01_economy_and_tokens/     # Аудит токенов, Prompt Caching и оптимизация схем
├── 02_failover_and_bugfixes/   # Анализ сбоев провайдеров LLM, фикс таймаутов и ключей
├── 03_claude_project_map/      # Архитектурная карта проекта и рекомендации для Claude
├── 04_meta_and_archive/        # Планы архивации сессий
└── README.md                   # Данное оглавление
```

---

## 📋 Оглавление архива

### 1. Экономика токенов и Prompt Caching (`01_economy_and_tokens/`)

| Файл | Описание |
| :--- | :--- |
| [**`research_prompt_caching_and_optimization.md`**](./01_economy_and_tokens/research_prompt_caching_and_optimization.md) | **Главный исследовательский отчёт**: детальный аудит расхода токенов на базе 2 510 сообщений из `health.db`, анатомия базового контекста (23 312 токенов), причины сброса кэша каждую минуту, математика Prompt Caching и расчёт тарифов моделей (Claude 3.5/3.7 Sonnet, Haiku, Opus). |
| [**`caching_and_fastpath_plan.md`**](./01_economy_and_tokens/caching_and_fastpath_plan.md) | **Инженерный план Prompt Caching & Fast-Path**: технический проект стабилизации префикса, добавления `cache_control` для Anthropic/OpenRouter и перехвата рутинных команд («пульт», «статус», «журнал») без похода в LLM (0 токенов, 50 мс). |
| [**`token_optimization_plan.md`**](./01_economy_and_tokens/token_optimization_plan.md) | **План оптимизации схем инструментов**: компактизация 35 спецификаций инструментов и сжатие системного промпта бота. |
| [**`transfer_economy_reports_plan.md`**](./01_economy_and_tokens/transfer_economy_reports_plan.md) | План переноса документации в директорию `economy/`. |

---

### 2. Отказоустойчивость провайдеров LLM (`02_failover_and_bugfixes/`)

| Файл | Описание |
| :--- | :--- |
| [**`provider_failure_analysis_plan.md`**](./02_failover_and_bugfixes/provider_failure_analysis_plan.md) | **Анализ причин сбоя «Не могу связаться с моделью»**: пошаговый разбор отказа каждого из 8 провайдеров цепочки (лимит 7k ITPM у Groq, 429 квота Google, баг взаимной блокировки ключей `_KEY_STATES`, таймаут 25с для NVIDIA). |
| [**`fix_provider_failover_and_timeouts_plan.md`**](./02_failover_and_bugfixes/fix_provider_failover_and_timeouts_plan.md) | **План реализации пунктов 3 и 4**: исправление бага в `bot/llm.py` с привязкой кулдауна к `model:key` и увеличение таймаута NVIDIA reasoning-моделей до 90 секунд в `config.yaml`. |
| [**`walkthrough_failover.md`**](./02_failover_and_bugfixes/walkthrough_failover.md) | **Итоговый отчёт о внедрении фиксов**: описание внесённых правок в `bot/llm.py` и `config.yaml`, результаты прогона всех тестов (25/25 passed) и статус перезапуска службы `health-agent`. |

---

### 3. Карта проекта и оптимизация токенов Claude (`03_claude_project_map/`)

| Файл | Описание |
| :--- | :--- |
| [**`PROJECT_MAP.md`**](./03_claude_project_map/PROJECT_MAP.md) | **Эталонная карта проекта и руководство**: полная раскладка каталогов (`health_core/`, `bot/`, `plugin/`, `scripts/`, `admin/`, `Core/`, `Knowledge/`), таблица 35 инструментов, фундаментальные инварианты (детерминированная арифметика Python, SQLite, изоляция), правила токеномики для Claude и готовый сниппет инструкций. *(Актуальная рабочая копия также находится в корне репозитория).* |
| [**`claude_token_optimization_plan.md`**](./03_claude_project_map/claude_token_optimization_plan.md) | Первичный план создания `CLAUDE.md` и `.claudeignore`. |
| [**`project_map_and_claude_proposals_plan.md`**](./03_claude_project_map/project_map_and_claude_proposals_plan.md) | План отката служебных файлов Claude и создания нейтральной карты `PROJECT_MAP.md` с предложениями для Claude. |
| [**`walkthrough_project_map.md`**](./03_claude_project_map/walkthrough_project_map.md) | Итоговый отчёт о создании карты проекта, рекомендаций для Claude и прохождении тестов. |

---

### 4. Мета-планы и управление архивом (`04_meta_and_archive/`)

| Файл | Описание |
| :--- | :--- |
| [**`archive_session_artifacts_plan.md`**](./04_meta_and_archive/archive_session_artifacts_plan.md) | Первичный план архивации артефактов в каталог `gemini_edited/14092026/`. |
| [**`archive_to_gemini_folders_plan.md`**](./04_meta_and_archive/archive_to_gemini_folders_plan.md) | План структурирования архива по 4 тематическим подпапкам и создания симлинка `gemini`. |

---

## 💡 Ключевые итоги сессий 14.09.2026

1. **Экономика токенов бота:**
   - В обычный день расходуется **~2,0 млн токенов** (40 сообщений / 76 вызовов LLM), в активные дни — **~2,8 млн токенов**.
   - 15 млн токенов без кэширования хватает на **~5–7 дней**, а с включённым Prompt Caching — на **~1.5–2 месяца**.
   - Самая выгодная связка для чата — **Claude 3.5 Haiku** ($9–10/мес) или **Claude 3.5/3.7 Sonnet с Prompt Caching** ($37–40/мес).

2. **Отказоустойчивость цепочки провайдеров:**
   - В [`bot/llm.py`](file:///opt/webapps/health_agent_system/bot/llm.py) кулдаун ключей изолирован по паре `(модель, ключ)`. Ошибка 429 на одной модели Google больше не блокирует другие рабочие модели на тех же ключах.
   - В [`config.yaml`](file:///opt/webapps/health_agent_system/config.yaml) и `bot/llm.py` для моделей NVIDIA (`moonshotai/kimi-k3` и `deepseek-ai/deepseek-v4-flash-0731`) установлен таймаут 90 секунд вместо 25 секунд.
   - Служба `health-agent.service` успешно перезапущена и работает стабильно.

3. **Оптимизация работы Claude с проектом:**
   - Подготовлен исчерпывающий справочник [**`PROJECT_MAP.md`**](file:///opt/webapps/health_agent_system/PROJECT_MAP.md), позволяющий Claude мгновенно ориентироваться в репозитории без чтения сотен килобайт спецификаций и логов.
   - Сформулированы строгие правила токеномики (запрет чтения `plugin/tools.py` и `health_core/guards.py` целиком, исключение тяжелых директорий, навигация по карте).
   - Подготовлен готовый шаблон инструкций, который Claude может использовать для настройки своих правил.
