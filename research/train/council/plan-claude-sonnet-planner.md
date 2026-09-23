# IMPLEMENTATION PLAN: HOME RESISTANCE TRAINING & CARDIOVASCULAR ENGINE (CLAUDE-SONNET-PLANNER)

**Role:** Planner 2 (Clinical Endocrinology, Behavioral Adherence & Biomechanical Adaptation)  
**Target:** Health Agent System (`/opt/webapps/health_agent_system`)  
**Context:** Debian 12, 1 vCPU, 1 GB RAM, SQLite 3 (WAL mode), aiogram 3.

---

## 1. STRATEGIC CLINICAL ARCHITECTURE

Preserving skeletal muscle during pharmacological hypophagia (Tirzepatide) requires more than fixed exercise templates. It demands **biochemical synchronization, acute-to-chronic workload regulation (ACWR) to protect a vulnerable knee, and behavioral habit anchoring**.

```
                HOLISTIC CLINICAL REGULATION LOOP
┌───────────────────────────┐         ┌───────────────────────────────┐
│ Tirzepatide PK Tracker    │         │ Sarcopenia Monitoring Triad   │
│ - Days post-injection     │         │ - Grip strength vs Baseline   │
│ - Gastro-intestinal stage │         │ - Weight x Reps at 3-1-1-0    │
└─────────────┬─────────────┘         └───────────────┬───────────────┘
              │                                       │
              ▼                                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│              AUTOTUNING TRAINING REGULATOR                          │
│ - Acute:Chronic Workload Ratio (ACWR knee strain < 1.3)             │
│ - Calorie Deficit Depth Modulation (MATADOR Refeed Alignment)       │
│ - Decoupled HRV Diagnostic Filter (HCN4 bypass)                     │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│          BEHAVIORAL BOT INTERACTION (aiogram 3)                     │
│ - Micro-Commitment Scripts ("Just 2 sets today")                    │
│ - Intra-workout cluster dextrin & hydration reminders               │
│ - Postural blood pressure alerts (anti-orthostatic pauses)          │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. ADVANCED ADAPTIVE ALGORITHMS

### 2.1. Dynamic Volume Autotuning based on Energy Availability
The user operates under a fluctuating caloric deficit (actual 900–1300 kcal vs target 1750 kcal). High training volume during extreme deficits triggers elevated muscle protein breakdown (MPB).
* **Rule:**
  * If yesterday's calorie intake was $< 1000\text{ kcal}$: Set volume to **Strict Minimum Effective Dose (MED)**: exactly 2 sets per exercise (4 sets per muscle group weekly).
  * If yesterday was a planned refeed ($\ge 1750\text{ kcal}$): Expand volume to **Preservation Plus**: 3 to 4 sets per exercise (6–8 sets per muscle group weekly).
  * If sleep was $< 6.0\text{ hours}$: Reduce load by 10% or restrict RIR strictly to 2–3 (zero near-failure strain).

### 2.2. Joint Strain & Knee Load ACWR (Acute-to-Chronic Workload Ratio)
For an individual with an ACL reconstruction and prior fracture at 121 kg, sudden spikes in knee flexion repetitions cause joint effusion.
* Define **Knee Mechanical Workload Units ($KMWU$)**:
  $$KMWU = \sum (\text{Sets} \times \text{Reps} \times \text{Patellar Load Factor})$$
  Where Patellar Load Factors:
  * Glute Bridge / B-Stance RDL (pure hip hinge): $0.1$
  * Inverted Rows / Push-ups (upper body): $0.0$
  * Step-ups (25 cm): $0.5$
  * Bulgarian Split Squat (vertical shin, $20^\circ$ torso lean): $0.7$
* The Acute Workload (7-day rolling average) divided by Chronic Workload (28-day rolling average) must stay strictly between **$0.8\text{--}1.3$**. If $\text{ACWR} > 1.3$, the system automatically swaps knee-dominant movements for hip-dominant glute bridges.

### 2.3. Decoupled HRV & Handgrip Fatigue Filtering
Commercial wearables flag low HRV on Tirzepatide because the peptide activates SA node HCN4 pacemaker channels directly.
* The system ignores wearable "central fatigue" warnings.
* Instead, it implements **Weekly Baseline Grip Dynamometry**:
  * Baseline $G_{\text{base}}$ = median of 3 weekly measurements.
  * If morning grip $< 0.90 \times G_{\text{base}}$: Trigger `FatigueLevel = Moderate`. The workout is truncated to 2 sets per movement.
  * If morning grip $< 0.80 \times G_{\text{base}}$: Trigger `FatigueLevel = Severe`. System cancels resistance session and prescribes 30 min gentle walk + sleep optimization.

---

## 3. TELEGRAM BOT CONVERSATIONAL FLOW & BEHAVIORAL NUDGES

### 3.1. PK-Informed Morning Check-in
* **Days 1–2 Post-Injection:**
  > «Доброе утро! Сегодня 2-й день после укола (пик концентрации тирзепатида). Замедление моторики желудка максимальное. Силовые тренировки и любые наклоны сегодня исключены, чтобы избежать изжоги. План на день: спокойные шаги на дорожке (до 7,500) и 3.5 литра воды. Пейте по стакану каждый час!»
* **Day 4 Post-Injection:**
  > «Сегодня день 4: концентрация снижается, ЖКТ спокоен. Время первой силовой сессии А (30 минут). Мы сохраняем волокна Type II. Помните: темп 3-1-1-0 (3 секунды опускаемся). Начнем с легкой разминки?»
* **Day 6 Post-Injection:**
  > «День 6: надир тирзепатида. Ваш естественный ресурс и сила на пике недели! Сегодня ключевая сессия B. За 40 минут до старта: 300 мл воды + щепотка соли. После тренировки — запланированный белковый прием!»

### 3.2. Orthostatic Blood Pressure Safety Prompts
During workout execution, between sets of B-Stance RDL or Push-ups:
* The bot sends:
  > «⏸ **Отдых 2.5 минуты.** Не вставайте резко! Посидите 10 секунд, сделайте 5 круговых движений стопами (венозная помпа). На тирзепатиде давление при подъеме может кратковременно падать.»

---

## 4. COMPONENT ARCHITECTURE IN `health_core/`

```
health_core/training/
├── domain/
│   ├── models.py             # Pydantic domain models
│   ├── exercise_catalog.py   # Catalog of safe home exercises with KMWU factors
│   └── pk_matrix.py          # 7-day PK state transitions
├── services/
│   ├── workout_generator.py  # Adaptive workout synthesizer
│   ├── knee_safety_filter.py # Hard knee constraint validator
│   ├── acwr_calculator.py    # Joint workload tracking
│   └── adherence_coach.py    # Habit anchoring & behavioral nudges
└── storage/
    └── training_repo.py      # SQLite async queries
```

---

## 5. RISKS & SELF-CRITIQUE (CLAUDE-SONNET-PLANNER)

1. **Self-critique (User Friction with Manual Dynamometry):**
   * *Gap:* Requiring the user to buy a digital handgrip dynamometer and input kg readings every morning creates behavioral friction and risks abandonment.
   * *Mitigation:* Make grip dynamometry optional. If no dynamometer is available, fall back to a 3-question morning subjective readiness score (Sleep quality 1–5, Muscle soreness 1–5, Perceived energy 1–5). A composite score $< 8/15$ triggers down-titration of training sets.
2. **Self-critique (Over-engineering ACWR on Low Session Frequency):**
   * *Gap:* ACWR (Acute:Chronic Workload Ratio) is traditionally validated in athletes training 4–6 times per week. With only 2 resistance sessions per week, a rolling 7-day vs 28-day ratio has high discrete volatility.
   * *Mitigation:* Simplify the KMWU calculation: cap weekly knee-dominant sets at a deterministic maximum of **6 sets per week** (e.g., 3 sets of Bulgarian Split Squats on Session A, 3 sets of Step-ups on Session B), making ACWR violation mathematically impossible by design.

---

## 6. UNIT TEST SUITE
1. `test_autotuning_volume_on_severe_deficit()`: Verify that when caloric intake is $<1000$ kcal, prescribed sets per exercise drop to 2.
2. `test_knee_load_factor_computation()`: Validate that purely hip-dominant sessions yield low KMWU ($< 2.0$), while knee-dominant sessions with illegal angles are blocked.
3. `test_orthostatic_pause_prompt_generation()`: Ensure that rest interval advice includes postural dizziness warnings.
