# Исследование перспективных Health & Metabolic AI скиллов и каталог готовых репозиториев

Данный документ содержит **концептуальное описание идей** для расширения возможностей метаболического и медицинского ИИ-ассистента, а также **подборку готовых open-source репозиториев и библиотек** для их реализации.

> **Формат:** Чистые идеи, математические/клинические обоснования и ссылки на проверенные открытые репозитории (без привязки к внутренней кодовой базе).

---

## 1. Лабораторная диагностика и анализ биомаркеров крови

### 💡 Концепция скилла
* **Суть:** Автоматический разбор медицинских бланков (PDF, фото, сканы) лабораторий (Инвитро, Гемотест, Хеликс, LabQuest), нормализация единиц измерения, проверка референсных диапазонов и расчет ключевых метаболических индексов.
* **Клинические индексы:**
  * **HOMA-IR** (инсулинорезистентность): $\frac{\text{Глюкоза (ммоль/л)} \times \text{Инсулин (мкЕд/мл)}}{22.5}$ (норма $<2.7$).
  * **TyG Index** (ранний маркер жирового гепатоза и инсулинорезистентности): $\ln\left(\frac{\text{Триглицериды (мг/дл)} \times \text{Глюкоза (мг/дл)}}{2}\right)$.
  * **Non-HDL и коэффициенты липидного профиля:** $\text{Non-HDL} = \text{Общий холестерин} - \text{HDL}$; соотношение $\text{Triglycerides} / \text{HDL}$ как маркер атерогенных мелких плотных LDL.
  * **eGFR (CKD-EPI 2021):** Скорость клубочковой фильтрации по креатинину для мониторинга функции почек при высокобелковом питании.
  * **eAG (Estimated Average Glucose):** Расчет среднего уровня глюкозы по гликированному гемоглобину $\text{HbA1c}$: $28.7 \times \text{HbA1c} - 46.7\text{ (мг/дл)}$.

### 🛠 Готовые репозитории и инструменты
* [PaddlePaddle/PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) — Высокоточный OCR для распознавания таблиц, бланков и многоязычного текста (включая русский).
* [Unstructured-IO/unstructured](https://github.com/Unstructured-IO/unstructured) — Библиотека для парсинга и извлечения структурированных данных из медицинских PDF-документов и сканов.
* [facebookresearch/nougat](https://github.com/facebookresearch/nougat) — Нейросетевая модель от Meta для извлечения формул, таблиц и структурированного текста из академических и медицинских форм.
* [mims-harvard/ToolUniverse](https://github.com/mims-harvard/ToolUniverse) — Набор из 698 валидированных биомедицинских калькуляторов и инструментов от Harvard MIMS.
* [Open-Medica/open-medical-skills](https://github.com/Open-Medica/open-medical-skills) — Каталог из 747 верифицированных врачами медицинских алгоритмов и калькуляторов.

---

## 2. Мультимодальный контур питания, распознавание штрих-кодов и голос

### 💡 Концепция скилла
* **Суть:** Устранение барьеров при логировании питания через фото тарелки, штрих-коды и голосовые заметки.
* **Функционал:**
  * **Метод тарелки (Vision):** Оценка пропорций блюда по фото ($1/2$ клетчатка/овощи, $1/4$ белок, $1/4$ сложные углеводы) и детекция скрытых жиров/масел (соусы, жарка).
  * **Сканер штрих-кодов:** Мгновенный запрос точного состава продукта по штрих-коду EAN-13/UPC.
  * **Голосовой ввод на бегу:** Быстрая транскрипция голосовых заметок о еде/тренировках в структурированные данные.

### 🛠 Готовые репозитории и инструменты
* [openfoodfacts/openfoodfacts-python](https://github.com/openfoodfacts/openfoodfacts-python) — Официальный Python SDK к глобальной открытой базе продуктов питания Open Food Facts (>3 млн товаров со штрих-кодами, составами и КБЖУ).
* [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) — Быстрый и оптимизированный инференс модели Whisper (CTranslate2) для локальной транскрипции речи.
* [openai/whisper](https://github.com/openai/whisper) — Стандартная модель распознавания многоязычной речи от OpenAI.
* [mindee/doctr](https://github.com/mindee/doctr) — Deep Learning библиотека для распознавания текста на этикетках, чеках и упаковках продуктов.

---

## 3. Спортивная физиология, периодизация и сохранение мышечной массы

### 💡 Концепция скилла
* **Суть:** Профессиональный контроль нагрузки, защита от перетренированности на дефиците калорий и объективное подтверждение сохранения мышечной массы (LBM).
* **Научные протоколы:**
  * **ACWR (Acute:Chronic Workload Ratio):** Соотношение острой нагрузки (7 дней) к хронической (28 дней) по модели Бэнистера (Banister Fitness-Fatigue Model). Защита от травм при $\text{ACWR} > 1.5$.
  * **1RM Autoregulation (Одноповторный максимум):** Расчет силового максимума по формулам Бризики ($1\text{RM} = \frac{W}{1.0278 - 0.0278 \times R}$) и Эпли. Мониторинг удержания силовых как критерий сохранения сухой мышечной массы.
  * **Heart Rate Recovery (HRR):** Оценка вегетативного баланса по скорости падения пульса за 1-ю минуту после кардио/интервальной нагрузки.

### 🛠 Готовые репозитории и инструменты
* [GoldenCheetah/GoldenCheetah](https://github.com/GoldenCheetah/GoldenCheetah) — Золотой стандарт open-source ПО для циклических видов спорта: расчет показателей TSS, CTL, ATL, TSB, мощности и тренировочного стресса.
* [dtcooper/python-fitparse](https://github.com/dtcooper/python-fitparse) — Библиотека для парсинга спортивных файлов .FIT (Garmin, Wahoo, Polar, Suunto) со всеми метриками пульса, темпа и мощности.
* [neuropsychology/NeuroKit](https://github.com/neuropsychology/NeuroKit) — NeuroKit2: мощный Python-тулбокс для обработки биосигналов (ЭКГ, пульсовая волна, вариабельность сердечного ритма, дыхание).
* [paulvangentcom/heartrate_analysis_python](https://github.com/paulvangentcom/heartrate_analysis_python) — HeartPy: специализированный пакет для анализа частоты пульса и кривых PPG/ECG.

---

## 4. Хрононутрициология, циркадные биоритмы и архитектура сна

### 💡 Концепция скилла
* **Суть:** Синхронизация питания, тренировок и стимуляторов с биологическими часами организма.
* **Методология:**
  * **Constrained Total Energy Expenditure Model (Модель Понтцера):** Учет нелинейной компенсации расхода калорий при экстремальных объемах кардио.
  * **TRE (Time-Restricted Eating) & Late Caloric Load:** Анализ длительности пищевого окна и доли вечерних калорий перед сном, влияющих на качество глубокого сна и ночную вариабельность пульса.
  * **Caffeine Halflife Simulator:** Кинетическая модель полувыведения кофеина ($t_{1/2} = 5\text{--}7$ ч) для расчета индивидуального времени отсечки («Caffeine Cutoff»).

### 🛠 Готовые репозитории и инструменты
* [raphaelvallat/yasa](https://github.com/raphaelvallat/yasa) — YASA (Yet Another Spindle Algorithm): открытая библиотека на Python для детального анализа полисомнографии, фаз сна (SWS, REM, N1, N2) и медленноволновой активности.
* [JuneYaooo/awesome-medical-ai-skills](https://github.com/JuneYaooo/awesome-medical-ai-skills) — Каталог навыков интеграции данных носимых устройств (Oura Ring, Apple HealthKit, Whoop, Withings).

---

## 5. Фармакокинетика, терапия GLP-1 и нутрицевтическая синергия

### 💡 Концепция скилла
* **Суть:** Сопровождение медикаментозной терапии для снижения веса (агонисты GLP-1/GIP) и оптимизация приема микронутриентов.
* **Функционал:**
  * **GLP-1 Titration & Lean Mass Floor Guard:** Моделирование периода полувыведения препаратов ($t_{1/2} \approx 7$ дней для семаглутида) и включение защитных порогов по белку ($\ge 1.8\text{--}2.0\text{ г/кг FFM}$) и силовым нагрузкам для предотвращения саркопенического ожирения.
  * **Нутрицевтическая матрица (Supplement Synergy & Antagonism):** Разнесение конкурирующих ионов (железо vs кальций, цинк vs медь) и тайминг синергетических компонентов (витамин D3 + K2 + жиры; магний глицинат перед сном).

### 🛠 Готовые репозитории и инструменты
* [pharmpy/pharmpy](https://github.com/pharmpy/pharmpy) — Комплексная библиотека для фармакометрики, симуляции фармакокинетики (PK) и фармакодинамики (PD).
* [FreedomIntelligence/OpenClaw-Medical-Skills](https://github.com/FreedomIntelligence/OpenClaw-Medical-Skills) — Репозиторий из 869 навыков, включающий модули проверки межлекарственных взаимодействий (DDI) и интеграции с ChEMBL/OpenFDA.

---

## 6. Поведенческая психология, анти-плато и предикторы срывов

### 💡 Концепция скилла
* **Суть:** Проактивное предупреждение компульсивных перееданий и психологическая поддержка на дефиците калорий.
* **Методы:**
  * **Binge Risk Triad Predictor:** Корреляционный анализ факторов риска (накопленный дефицит за 5–7 дней + дефицит сна $<6.5$ ч + пропуск утреннего белка) для своевременного назначения планового рефида.
  * **Дифференциация физиологического и эмоционального голода:** Интерактивные алгоритмы проверки истинного метаболического голода (по шкале сытости/голода и тяге к нейтральным белковым продуктам).

### 🛠 Готовые репозитории и инструменты
* [Open-Medica/open-medical-skills](https://github.com/Open-Medica/open-medical-skills) — Включает готовые протоколы валидированных клинических шкал (PHQ-9, GAD-7, AUDIT, шкалы оценки аппетита).

---

## Сводная таблица репозиториев по направлениям

| Домен | Основные репозитории на GitHub | Назначение |
|---|---|---|
| **Лабораторные анализы и OCR** | [PaddlePaddle/PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR), [Unstructured-IO/unstructured](https://github.com/Unstructured-IO/unstructured), [mims-harvard/ToolUniverse](https://github.com/mims-harvard/ToolUniverse) | Распознавание бланков и медицинские формулы |
| **Базы продуктов и штрих-коды** | [openfoodfacts/openfoodfacts-python](https://github.com/openfoodfacts/openfoodfacts-python) | Поиск продуктов по штрихкоду (>3M позиций) |
| **Голосовой ввод (STT)** | [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper), [openai/whisper](https://github.com/openai/whisper) | Быстрая транскрипция аудиосообщений |
| **Спорт и телеметрия** | [GoldenCheetah/GoldenCheetah](https://github.com/GoldenCheetah/GoldenCheetah), [dtcooper/python-fitparse](https://github.com/dtcooper/python-fitparse) | Парсинг .FIT/TCX, расчет ACWR, TSS и нагрузки |
| **Биосигналы и пульс (HRV)** | [neuropsychology/NeuroKit](https://github.com/neuropsychology/NeuroKit), [paulvangentcom/heartrate_analysis_python](https://github.com/paulvangentcom/heartrate_analysis_python) | Вариабельность пульса (RMSSD), вегетативный тонус |
| **Анализ сна** | [raphaelvallat/yasa](https://github.com/raphaelvallat/yasa) | Архитектура и стадии сна |
| **Фармакокинетика** | [pharmpy/pharmpy](https://github.com/pharmpy/pharmpy), [FreedomIntelligence/OpenClaw-Medical-Skills](https://github.com/FreedomIntelligence/OpenClaw-Medical-Skills) | Моделирование $t_{1/2}$, GLP-1 кинетика, DDI |
