# План переноса отчётов и исследований в папку /economy

## Goal Description
Сохранить результаты проведённого аудита токенов, ценового анализа моделей и архитектурных планов оптимизации в постоянную структуру репозитория — каталог `/opt/webapps/health_agent_system/economy/`.

---

## User Review Required

> [!NOTE]
> Все материалы будут размещены в новой директории `economy/` в корне проекта. Кодовая база бота (`bot/`, `plugin/`, `Core/`, `health_core/`) не модифицируется.

Структура папки `economy/`:
1. `economy/README.md` — навигационный обзор, ключевые выводы по экономике токенов, сводная таблица тарифов моделей (Sonnet, Haiku, Opus) и расчёт 15 млн токенов.
2. `economy/01_research_token_usage_and_caching.md` — полный исследовательский отчёт с замерами из `health.db` (2 510 сообщений), анатомией базового контекста (23 312 токенов), анализом префиксной нестабильности и механикой Prompt Caching.
3. `economy/02_prompt_caching_and_fastpath_plan.md` — инженерный план внедрения стабильного префикса, маркеров `cache_control` для Anthropic/OpenRouter и Fast-Path для частых команд без обращения к LLM.
4. `economy/03_token_optimization_plan.md` — план компактизации схем инструментов и системного промпта.

---

## Proposed Changes

### Документация проекта

#### [NEW] `economy/README.md`
Сводный документ с ключевыми метриками:
- Расход в день: ~2.0M токенов (обычный день), ~2.8M (активный день), ~7.8M (пик).
- На сколько хватит 15 млн токенов (без кэша: ~5–7 дней, с кэшем: ~1.5–2 месяца).
- Сравнительная таблица расходов на Claude 3.5/3.7 Sonnet, Haiku, Opus.
- Индекс файлов папки.

#### [NEW] `economy/01_research_token_usage_and_caching.md`
Полная копия исследовательского отчёта `research_prompt_caching_and_optimization.md`.

#### [NEW] `economy/02_prompt_caching_and_fastpath_plan.md`
Полная копия инженерного плана `caching_and_fastpath_plan.md`.

#### [NEW] `economy/03_token_optimization_plan.md`
Полная копия плана `token_optimization_plan.md`.

---

## Verification Plan

### Automated Verification
1. Проверка создания каталога и наличия всех 4 файлов:
   ```bash
   ls -la /opt/webapps/health_agent_system/economy/
   ```
2. Проверка целостности содержимого файлов (размеры, отсутствие пустых файлов).
3. Проверка `git status` — убедиться, что изменены только добавленные файлы в `economy/`, а код бота не затронут:
   ```bash
   git status --short
   ```
