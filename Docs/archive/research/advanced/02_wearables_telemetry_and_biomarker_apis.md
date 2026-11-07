# ИССЛЕДОВАНИЕ 02: НОСИМЫЕ УСТРОЙСТВА, ТЕЛЕМЕТРИЯ СЕНСОРОВ, КЛИНИЧЕСКИЕ КАЛЬКУЛЯТОРЫ И NUTRITION APIS ДЛЯ 1GB RAM СЕРВЕРА

**Директория:** `/opt/webapps/health_agent_system/research/advanced`  
**Дата:** 2026-09-13  
**Целевая платформа:** Debian 12 (Bookworm), 1 vCPU, 1 GB RAM, SQLite 3 (WAL mode), Python 3.11  
**Исследовательский фокус:** Интеграция Withings API, Oura v2, Dexcom CGM, потоковый парсинг Garmin FIT и Apple Health XML, медицинские калькуляторы (eGFR 2021, Cystatin C, HOMA-IR, TyG, FIB-4, AIP), базы продуктов Open Food Facts и USDA FDC, оптимизация ZRAM и ядра Linux.

---

## РАЗДЕЛ 1. НОСИМЫЕ УСТРОЙСТВА И ТЕЛЕМЕТРИЯ: БИБЛИОТЕКИ И ПРОДАКШН-ИНТЕГРАЦИИ

### 1.1. Withings Health API (Весы, состав тела, давление)
Умные весы (Withings Body Scan / Body Comp) передают состав тела через биоимпеданс, а тонометры (Withings BPM Connect) — артериальное давление и пульсовую волну.

* **Рекомендуемая библиотека:** `aiowithings`
  * **GitHub:** [joostlek/python-aiowithings](https://github.com/joostlek/python-aiowithings) (используется в Home Assistant Core).
  * **Лицензия:** MIT | **PyPI:** `aiowithings` | Установка: `pip install aiowithings`
* **Формат данных и масштабирование:**
  Значения в API Withings нормализуются формулой: $\text{real\_value} = \text{value} \times 10^{\text{unit}}$ (например, `value: 7545`, `unit: -2` $\to 75.45\text{ кг}$).
* **Ключевые коды типов измерений (`meastype`):**
  * `1`: Масса тела (Weight, кг)
  * `5`: Тощая масса (Lean Mass, кг)
  * `6`: Процент жира (Fat Ratio, %)
  * `8`: Жировая масса (Fat Mass, кг)
  * `9`: Диастолическое АД (мм рт. ст.)
  * `10`: Систолическое АД (мм рт. ст.)
  * `11`: Пульс (bpm)
  * `76`: Мышечная масса (Muscle Mass, кг)
  * `77`: Гидратация (Hydration, кг)
  * `91`: Скорость пульсовой волны (PWV, м/с — жесткость артерий)
* **Вебхуки в реальном времени (`notify/subscribe`):**
  Сервер регистрирует эндпоинт на события `appli=1` (взвешивание) и `appli=4` (давление). Inbound-запрос содержит `userid`, `startdate`, `enddate`, после чего бот делает точечный запрос `getmeas` и немедленно логирует данные в SQLite.

---

### 1.2. Oura Cloud API v2 (Стадии сна, пульс покоя, HRV, готовность)
* **Рекомендуемый подход:** Прямой асинхронный HTTP-клиент с Personal Access Token (PAT) или OAuth2.
* **Библиотека-обертка:** `python-oura` ([github.com/turing-complet/python-oura](https://github.com/turing-complet/python-oura), MIT).
* **Эндпоинты Oura v2 (`https://api.ouraring.com/v2/usercollection/`):**
  1. `daily_sleep`: Оценка сна (Sleep Score), вклад фаз (глубокий, быстрый REM, легкий сон), латентность засыпания.
  2. `sleep`: Поминутные эпохи сна (`1` = deep, `2` = light, `3` = REM, `4` = awake), ночной трек ЧСС, `average_hrv` (rMSSD в мс).
  3. `daily_readiness`: Оценка восстановления (Readiness Score), температурное отклонение кожи ($\Delta^\circ\text{C}$ — маркер воспаления/овуляции).
  4. `heart_rate`: 5-минутные интервалы пульса днем и ночью.
* **Память:** Потребление чистого async-клиента на `httpx` — **менее 3 МБ RAM**.

---

### 1.3. Непрерывный мониторинг глюкозы (CGM): Dexcom Share & Nightscout
Для пациентов с инсулинорезистентностью, преддиабетом и на GLP-1 терапии сенсоры CGM (Dexcom G6/G7/ONE) дают поминутный трек гликемии.

* **Библиотека:** `pydexcom`
  * **GitHub:** [gagebenne/pydexcom](https://github.com/gagebenne/pydexcom) | Лицензия: MIT | PyPI: `pydexcom`
  * **Особенность серверов:** Для пользователей из Европы и СНГ обязателен флаг `ous=True` (сервер `shareous1.dexcom.com`), для США — `ous=False`.
  * **Трендовые стрелки:** `Flat` ($\to$), `FortyFiveUp` ($\nearrow$), `SingleUp` ($\uparrow$), `DoubleUp` ($\uparrow\uparrow$), `FortyFiveDown` ($\searrow$), `SingleDown` ($\downarrow$), `DoubleDown` ($\downarrow\downarrow$).
* **Платформа Nightscout:** Открытый стандарт REST API (`GET /api/v1/entries.json?count=288`) для чтения 24-часового профиля глюкозы из любых сенсоров (FreeStyle Libre, Medtronic, Dexcom).

---

### 1.4. Парсинг бинарных файлов тренировок Garmin (.FIT)
* **Проблема:** Бинарные файлы тренировок FIT содержат сотни тысяч точек (GPS, мощность, пульс, частота шагов).
* **Рекомендуемая библиотека:** `fitdecode`
  * **GitHub:** [polyvertex/fitdecode](https://github.com/polyvertex/fitdecode) | Лицензия: MIT | PyPI: `fitdecode`
  * **Архитектура:** Zero-copy потоковый ридер (`FitReader`). Потребление памяти строго стабильно на уровне **10–12 МБ RAM** даже при обработке 100-мегабайтных файлов ультрамарафонов. В отличие от тяжелого `fitparse`, не кэширует все записи в единый массив.

---

### 1.5. Потоковый парсинг выгрузок Apple Health (True $O(1)$ RAM Streaming)
* **Архитектурная ловушка:** Выгрузка `export.xml` за несколько лет весит от 500 МБ до 3 ГБ. Стандартные парсеры (`ET.parse`, `BeautifulSoup`, `lxml`) строят DOM-дерево в памяти, требуя от 4 до 16 ГБ RAM. На сервере с 1 ГБ RAM это гарантирует немедленный краш бота от ядра Linux (OOM Killer).
* **Утечка `elem.clear()`:** Стандартный вызов `elem.clear()` в `ET.iterparse` все равно накапливает ссылки на очищенные элементы в родительском узле корня (`root`).
* **Паттерн истинного $O(1)$ стриминга (<15 МБ RAM):**
  Обязательное удаление предыдущих узлов из корня `root.clear()` в связке с `elem.clear()`:

```python
import xml.etree.ElementTree as ET
from typing import Generator, Dict, Any, Set

def stream_apple_health_xml(
    xml_path: str,
    target_record_types: Set[str] = None
) -> Generator[Dict[str, Any], None, None]:
    """Потоковый парсер Apple Health export.xml с фиксированным расходом памяти <15 МБ RAM.
    
    Использует iterparse с непрерывным очищением корневого элемента.
    """
    context = ET.iterparse(xml_path, events=("start", "end"))
    event, root = next(context)

    for event, elem in context:
        if event == "end" and elem.tag == "Record":
            rec_type = elem.attrib.get("type")
            if target_record_types is None or rec_type in target_record_types:
                yield {
                    "type": rec_type,
                    "value": elem.attrib.get("value"),
                    "unit": elem.attrib.get("unit"),
                    "startDate": elem.attrib.get("startDate"),
                    "endDate": elem.attrib.get("endDate"),
                    "source": elem.attrib.get("sourceName"),
                }
            # Очищаем внутренности элемента
            elem.clear()
            # Очищаем корень от ссылок на обработанные дочерние узлы (ликвидирует утечку CPython)
            root.clear()
```

---

## РАЗДЕЛ 2. ВАЛИДИРОВАННЫЕ КЛИНИЧЕСКИЕ КАЛЬКУЛЯТОРЫ И БИОМЕДИЦИНСКИЕ ИНДЕКСЫ

Все формулы ниже реализованы на чистом Python (IEEE-754) без сторонних библиотек (0 МБ RAM).

### 2.1. Скорость клубочковой фильтрации: eGFR (CKD-EPI 2021 Race-Free & Cystatin C)
1. **CKD-EPI 2021 по креатинину (Race-Free, стандарт NKF/ASN):**
   * *Ссылка:* Inker LA et al. *N Engl J Med*, 2021; 385:1737–1749. DOI: [10.1056/NEJMoa2102953](https://doi.org/10.1056/NEJMoa2102953).
   * Формула:
     $$\text{eGFR}_{\text{cr}} = 142 \times \min\left(\frac{S_{\text{cr}}}{\kappa}, 1\right)^\alpha \times \max\left(\frac{S_{\text{cr}}}{\kappa}, 1\right)^{-1.200} \times 0.9938^{\text{Age}} \times [1.012 \text{ если Женщина}]$$
     где $S_{\text{cr}}$ в мг/дл (мкмоль/л / 88.4), $\kappa = 0.7$ (Ж) / $0.9$ (М), $\alpha = -0.241$ (Ж) / $-0.302$ (М).
2. **CKD-EPI 2012 / 2023 по Цистатину C (Независим от мышц и креатина):**
   * *Ссылка:* Inker LA et al. *N Engl J Med*, 2012; 367:20–29.
   * *Клиническая важность:* У лиц с высокой мышечной массой или принимающих креатин сывороточный креатинин ложно повышен (+15–25 мкмоль/л). Цистатин C свободен от этого артефакта.
   * Формула:
     $$\text{eGFR}_{\text{cys}} = 133 \times \min\left(\frac{S_{\text{cys}}}{0.8}, 1\right)^{-0.499} \times \max\left(\frac{S_{\text{cys}}}{0.8}, 1\right)^{-1.328} \times 0.996^{\text{Age}} \times [0.932 \text{ если Женщина}]$$
     где $S_{\text{cys}}$ в мг/л.

---

### 2.2. Индексы инсулинорезистентности и углеводного обмена
1. **HOMA-IR (Homeostatic Model Assessment of Insulin Resistance):**
   * *Ссылка:* Matthews DR et al. *Diabetologia*, 1985; 28:412–419.
   * Формула (СИ: глюкоза в ммоль/л, инсулин в мкЕд/мл):
     $$\text{HOMA-IR} = \frac{\text{Глюкоза (ммоль/л)} \times \text{Инсулин (мкЕд/мл)}}{22.5}$$
   * *Пороги:* $<2.0$ — оптимально; $2.0\text{--}2.7$ — пограничная зона; $>2.7$ — манифестная инсулинорезистентность.
2. **TyG Index (Триглицерид-глюкозный индекс):**
   * *Ссылка:* Simental-Mendía LE et al. *Eur J Endocrinol*, 2008; Guerrero-Romero F et al. *JCEM*, 2010.
   * *Назначение:* Суррогатный маркер стеатоза печени (MASLD) и резистентности миоцитов при отсутствии анализа на инсулин.
   * Формула (через концентрации в ммоль/л):
     $$\text{TyG} = \ln\left( \frac{(\text{ТГ}_{\text{ммоль/л}} \times 88.57) \times (\text{Глюкоза}_{\text{ммоль/л}} \times 18.0182)}{2} \right)$$
   * *Пороги:* $<8.5$ — норма; $8.5\text{--}8.8$ — пограничный риск; $\ge 8.8$ — высокий риск MASLD и кардиоваскулярных событий.

---

### 2.3. Индексы печеночного и кардиоваскулярного риска
1. **FIB-4 Score (Индекс фиброза печени Fibrosis-4):**
   * *Ссылка:* Sterling RK et al. *Hepatology*, 2006; 43(6):1317–1325 (рекомендован EASL/AASLD).
   * Формула:
     $$\text{FIB-4} = \frac{\text{Возраст (лет)} \times \text{АСТ (Ед/л)}}{\text{Тромбоциты } (10^9/\text{л}) \times \sqrt{\text{АЛТ (Ед/л)}}}$$
   * *Пороги:* $<1.30$ (для возраста $\ge 65$ лет: $<2.0$) — низкий риск фиброза (F0–F1); $1.30\text{--}2.67$ — серая зона; $>2.67$ — высокий риск выраженного фиброза/цирроза (F3–F4).
2. **AIP (Атерогенный индекс плазмы / Atherogenic Index of Plasma):**
   * *Ссылка:* Dobiášová M, Frohlich J. *Clin Biochem*, 2001; 34(7):583–588.
   * Формула:
     $$\text{AIP} = \log_{10}\left( \frac{\text{Триглицериды (ммоль/л)}}{\text{ЛПВП (ммоль/л)}} \right)$$
   * *Пороги:* $<0.11$ — низкий сердечно-сосудистый риск; $0.11\text{--}0.21$ — умеренный; $>0.21$ — высокий риск атеросклероза (преобладание малых плотных частиц LDL фенотипа B).

---

## РАЗДЕЛ 3. БАЗЫ ПРОДУКТОВ ПИТАНИЯ И СКАНЕРЫ ШТРИХКОДОВ

### 3.1. Open Food Facts (OFF) Python SDK & REST API
* **GitHub:** [openfoodfacts/openfoodfacts-python](https://github.com/openfoodfacts/openfoodfacts-python) (Apache-2.0, данные ODbL).
* **Покрытие:** Свыше 3.2 млн товаров. Идеальное покрытие розничных сетей РФ и СНГ (ВкусВилл, X5/Пятерочка/Перекресток, Магнит).
* **Ключевые извлекаемые поля:**
  * `nutriments`: калории, белки, жиры, насыщенные жиры, углеводы, клетчатка, сахар, натрий на 100 г.
  * `nova_group`: 1 (минимально обработанная пища) до 4 (ультра-переработанная пища / UPF).
  * `nutriscore_grade`: A, B, C, D, E.

### 3.2. USDA FoodData Central (FDC) API
* **Документация:** [fdc.nal.usda.gov/api-guide.html](https://fdc.nal.usda.gov/api-guide.html) (Public Domain, 1000 запросов/час).
* **Ценность:** Золотой стандарт для цельных продуктов (говядина, курица, яйца, крупы). В отличие от коммерческих этикеток, содержит **полный аминокислотный профиль**:
  * Nutrient `1213`: **L-Лейцин (г)** — проверка преодоления анаболического порога mTORC1 ($\ge 3.0\text{ г}$ на прием).
  * Микроэлементы: калий (1092), магний (1090), кальций (1087).

---

## РАЗДЕЛ 4. АРХИТЕКТУРНАЯ ОПТИМИЗАЦИЯ СЕРВЕРА 1GB RAM DEBIAN 12

### 4.1. Бюджет оперативной памяти (Strict RAM Allocation)

| Компонент системы | Выделенная память | Назначение / Механизм контроля |
| :--- | :---: | :--- |
| **Debian 12 OS + SSH + systemd** | $140\text{--}180\text{ МБ}$ | Базовое ядро ОС без GUI. |
| **Процесс Telegram-бота (aiogram 3)** | $130\text{--}160\text{ МБ}$ | Асинхронное ядро бота, чистый Python math. |
| **Кэш страниц SQLite WAL** | $16\text{--}30\text{ МБ}$ | `PRAGMA cache_size = -16000` (ровно 16 МБ). |
| **Буферы вебхуков и HTTP-клиентов** | $30\text{--}50\text{ МБ}$ | Пул соединений `httpx.AsyncClient`. |
| **Свободный безопасный буфер** | **$550\text{--}650\text{ МБ}$** | **Гарантия защиты от Linux OOM Killer.** |

---

### 4.2. Настройка сжатого Swap в оперативной памяти (ZRAM zstd)
На одноядерном сервере обращение к дисковому swap на NVMe/SSD вызывает I/O wait до 100% и заморозку бота. Установка ZRAM создает виртуальный сжатый swap прямо в RAM с алгоритмом `zstd` (коэффициент сжатия 3:1), делая подкачку мгновенной и без дискового троттлинга:

```bash
# Установка и настройка zram-tools
sudo apt update && sudo apt install -y zram-tools

sudo bash -c 'cat <<EOF > /etc/default/zramswap
ALGO=zstd
PERCENT=100
PRIORITY=100
EOF'

# Оптимизация ядра Linux для health-агента
sudo bash -c 'cat <<EOF > /etc/sysctl.d/99-health-agent.conf
vm.swappiness=15
vm.vfs_cache_pressure=50
vm.overcommit_memory=1
EOF'

sudo sysctl --system
sudo systemctl restart zramswap
```

---

### 4.3. Ограничение cgroups и приоритет OOMScoreAdjust в systemd
В файле сервиса `/etc/systemd/system/health-agent.service` прописываются жесткие рамки:
```ini
[Service]
MemoryHigh=650M
MemoryMax=800M
MemoryAccounting=true

# Повышенный приоритет: при дефиците памяти ядро сначала убьет тяжелый сабпроцесс, но сохранит бота
OOMScoreAdjust=-500
```

---

### 4.4. Настройка конкурентности SQLite 3 в режиме WAL
Чтобы вебхуки Withings, входящие сообщения Telegram-бота и фоновые задачи не вызывали ошибку `database is locked`:
```python
import sqlite3

def init_wal_sqlite(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")        # Параллельное чтение при активной записи
    conn.execute("PRAGMA busy_timeout = 10000;")       # Ожидание освобождения лока до 10 сек
    conn.execute("PRAGMA synchronous = NORMAL;")       # Снижение дисковых fsync без риска повреждения WAL
    conn.execute("PRAGMA cache_size = -16000;")        # Лимит кэша 16 МБ RAM
    conn.execute("PRAGMA temp_store = MEMORY;")        # Временные таблицы строго в RAM
    return conn
```
