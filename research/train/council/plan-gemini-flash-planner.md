# IMPLEMENTATION PLAN: HOME RESISTANCE TRAINING & CARDIOVASCULAR ENGINE (GEMINI-FLASH-PLANNER)

**Role:** Planner 1 (Systems Engineering, Deterministic Algorithms & Low-Footprint Architecture)  
**Target:** Health Agent System (`/opt/webapps/health_agent_system`)  
**Context:** Debian 12, 1 vCPU, 1 GB RAM, SQLite 3 (WAL mode), aiogram 3, Pydantic v2.

---

## 1. ARCHITECTURAL OVERVIEW & PRINCIPLES

The Training Module must integrate seamlessly into the existing `health_core/` infrastructure without introducing high RAM overhead or non-deterministic LLM behavior into safety-critical training prescriptions.

```
                  SYSTEM INTERACTION & DATA FLOW PIPELINE
 ┌────────────────┐         ┌───────────────────────┐         ┌─────────────────────────┐
 │  Telegram Bot  │ ──(1)─► │ Exercise Router / API │ ──(2)─► │ Training Engine Core    │
 │ (aiogram 3 UI) │         │ (health_core/train/)  │         │ - PK Synchronizer       │
 └────────────────┘         └───────────────────────┘         │ - Knee Biomechanics Gate│
         ▲                             │                      │ - Dynamic Progression   │
         │                             ▼                      │ - Karvonen HR Z2 Math   │
         │ (4) Render Markdown  ┌───────────────────────┐     └─────────────────────────┘
         └───────────────────── │ SQLite DB (Tables:    │                  │
                                │  training_sessions,   │ ◄────────────────┘ (3) Persist
                                │  exercise_logs, etc.) │
                                └───────────────────────┘
```

### Core Design Constraints:
1. **O(1) Memory Footprint:** Zero heavy dataframes (Pandas/Polars) or ML frameworks. Pure standard library `math`, `sqlite3`, and lightweight Pydantic schemas.
2. **Knee Safety Interceptor (Biomechanical Gate):** A hard-coded validation layer that intercepts and rejects any banned exercise pattern (knee flexion $>90^\circ$, axial loading, plyometrics) before generating training templates.
3. **PK State Engine:** Synchronizes daily training prescription with days elapsed since the latest Tirzepatide subcutaneous injection.

---

## 2. DATABASE SCHEMA EXTENSIONS (SQLITE)

Three new tables are added via a clean migration script (`migrations/005_training_engine.sql`):

```sql
-- Migration 005: Home Resistance Training & Cardio Module

-- 1. Table for training sessions
CREATE TABLE IF NOT EXISTS training_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    session_date TEXT NOT NULL,          -- YYYY-MM-DD
    days_post_injection INTEGER NOT NULL,-- 0 to 6
    pk_phase TEXT NOT NULL,              -- 'cmax_peak', 'elimination', 'trough', 'injection'
    session_type TEXT NOT NULL,          -- 'full_body_a', 'full_body_b', 'cardio_deload', 'rest'
    duration_minutes INTEGER,
    subjective_rpe REAL,                 -- 1.0 to 10.0
    intra_workout_fueling_logged INTEGER DEFAULT 0, -- 1 = Cluster Dextrin/Electrolytes
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Table for granular exercise logs
CREATE TABLE IF NOT EXISTS exercise_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES training_sessions(id) ON DELETE CASCADE,
    exercise_name TEXT NOT NULL,
    tier_level INTEGER NOT NULL DEFAULT 1,
    set_number INTEGER NOT NULL,
    weight_kg REAL NOT NULL DEFAULT 0.0,
    reps_completed INTEGER NOT NULL,
    rir INTEGER NOT NULL,                -- Reps in reserve (target 1-2)
    tempo_followed TEXT NOT NULL DEFAULT '3-1-1-0',
    joint_discomfort_score INTEGER DEFAULT 0, -- 0-10 (0 = zero knee pain)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. Table for daily cardiovascular & readiness telemetry
CREATE TABLE IF NOT EXISTS training_readiness_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    log_date TEXT NOT NULL UNIQUE,       -- YYYY-MM-DD
    resting_hr INTEGER,                  -- bpm (elevated +4-8 bpm on GLP-1)
    grip_strength_kg REAL,               -- handgrip dynamometer
    waist_circ_cm REAL,
    step_count INTEGER NOT NULL DEFAULT 0,
    zone2_active_minutes INTEGER DEFAULT 0,
    orthostatic_dizziness_flag INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_date ON training_sessions(user_id, session_date);
CREATE INDEX IF NOT EXISTS idx_logs_session_id ON exercise_logs(session_id);
```

---

## 3. CORE PYTHON MODULE ARCHITECTURE (`health_core/training/`)

```
health_core/training/
├── __init__.py
├── schemas.py              # Pydantic models for workout logging & generation
├── pk_scheduler.py         # 7-day Tirzepatide cycle calculator
├── safety_guardrails.py    # ACL & knee pathology biomechanical rules
├── progression_engine.py   # Dynamic double progression (8-12 reps @ 3-1-1-0)
├── cardio_math.py          # Karvonen HR calculation & Pontzer step gating
└── training_service.py     # Main business logic & SQLite interface
```

### 3.1. `pk_scheduler.py` (Pharmacokinetic Cycle Synchronization)
```python
from datetime import date, datetime
from enum import Enum

class PKPhase(str, Enum):
    INJECTION = "injection"        # Day 0
    CMAX_PEAK = "cmax_peak"        # Days 1-2 (24-48h, severe GI delay)
    ELIMINATION = "elimination"    # Days 3-4 (motility recovering)
    TROUGH = "trough"              # Days 5-6 (peak energy, primary RT)
    PREP = "prep"                  # Day 7

def get_pk_phase(last_injection_date: date, target_date: date) -> tuple[int, PKPhase]:
    delta_days = (target_date - last_injection_date).days % 7
    if delta_days == 0:
        return (0, PKPhase.INJECTION)
    elif delta_days in (1, 2):
        return (delta_days, PKPhase.CMAX_PEAK)
    elif delta_days in (3, 4):
        return (delta_days, PKPhase.ELIMINATION)
    elif delta_days in (5, 6):
        return (delta_days, PKPhase.TROUGH)
    return (delta_days, PKPhase.PREP)
```

### 3.2. `safety_guardrails.py` (Knee & ACL Hard Interceptor)
```python
class KneeSafetyViolation(Exception):
    pass

BANNED_EXERCISE_KEYWORDS = [
    "jump", "plyo", "burpee", "run", "box jump", "skipping",
    "deep squat", "pistol", "sissy", "leg extension", "rucking",
    "weighted vest", "back squat", "lunge twist"
]

def validate_exercise_safety(exercise_name: str, flexion_angle_deg: float, axial_extra_load_kg: float) -> bool:
    name_lower = exercise_name.lower()
    for keyword in BANNED_EXERCISE_KEYWORDS:
        if keyword in name_lower:
            raise KneeSafetyViolation(f"Exercise '{exercise_name}' is contraindicated for ACL/knee fracture pathology.")
    if flexion_angle_deg > 90.0:
        raise KneeSafetyViolation(f"Knee flexion angle {flexion_angle_deg}° exceeds the 90° clinical safety threshold.")
    if axial_extra_load_kg > 0.0:
        raise KneeSafetyViolation(f"Spinal axial loading ({axial_extra_load_kg} kg) prohibited for BMI > 35 with prior knee fracture.")
    return True
```

### 3.3. `cardio_math.py` (Karvonen & Step Gating)
```python
def calculate_karvonen_zone2(resting_hr: int, age: int = 34) -> tuple[int, int]:
    # Tanaka formula for HRmax: 208 - (0.7 * age)
    hr_max = 208 - int(0.7 * age) # ~184 bpm
    hrr = hr_max - resting_hr
    # Zone 2 intensity corridor: 60% to 70% HRR
    z2_low = int(resting_hr + (0.60 * hrr))
    z2_high = int(resting_hr + (0.70 * hrr))
    return (z2_low, z2_high)

def evaluate_daily_steps(steps: int) -> str:
    if steps < 6000:
        return "STEPS_DEFICIT: Add a 15-minute postprandial walking session."
    elif 7500 <= steps <= 9000:
        return "STEPS_OPTIMAL: FATmax metabolic expenditure achieved without NEAT collapse."
    elif steps > 12000:
        return "STEPS_EXCESSIVE_WARNING: Risk of Pontzer compensatory energy collapse. Restrict additional cardio."
    return "STEPS_ACCEPTABLE"
```

---

## 4. DETERMINISTIC WORKOUT GENERATION ENGINE

Workout generation occurs without invoking LLM tokens, ensuring 0ms latency and 0 token cost.
* On Day 4 post-injection: System delivers **Full Body A** (Push-ups Tier 2-3, Bulgarian Split Squats Tier 2-3 with $20^\circ$ forward lean, Inverted Rows Tier 1-3, B-Stance RDL Tier 2-3).
* On Day 6 post-injection: System delivers **Full Body B** (Incline DB Press Tier 3, Goblet Box Squat to 40cm chair, Single-arm DB Row, Banded Glute Bridges).
* On Days 1–2 post-injection: System automatically flags **Cardio Deload**: Walking Pad (2.5–3.5 km/h, 30–45 min), zero high IAP exercises.

---

## 5. RISKS & SELF-CRITIQUE (GEMINI-FLASH-PLANNER)

1. **Self-critique (Rigidity vs. Progression Nuance):**
   * *Gap:* A purely deterministic template engine might feel robotic to the user if their recovery fluctuates unpredictably (e.g., severe sleep disruption or unexpected work stress).
   * *Mitigation:* Include an explicit `user_energy_status` check in the Telegram bot prompt before generating the session. If energy is $\le 3/10$, automatically reduce working sets from 3 to 2 (down to 4 sets/muscle group/week).
2. **Self-critique (Knee Discomfort Feedback Lag):**
   * *Gap:* The database logs `joint_discomfort_score` after the workout is finished, which cannot prevent joint irritation *during* the sets if the user pushes through pain.
   * *Mitigation:* Enforce an immediate in-bot pre-set check: "Any knee stiffness today?" If answered "Yes", the system automatically downshifts from Bulgarian Split Squat to Glute Bridges and wall-assisted static split squat.

---

## 6. UNIT TEST & VERIFICATION PLAN

1. `test_pk_phase_calculation()`: Test all 7 days offset from an arbitrary injection timestamp; ensure Days 1–2 map to `CMAX_PEAK` and Days 5–6 map to `TROUGH`.
2. `test_knee_safety_interceptor()`: Inject banned strings ("burpee", "jump rope", "barbell squat", "rucking") and assert `KneeSafetyViolation` is raised.
3. `test_karvonen_formula()`: Test resting HR range (65 to 85 bpm) for 34yo; verify Zone 2 targets stay strictly within aerobic physiological thresholds (135–150 bpm).
4. `test_db_migration_idempotency()`: Execute `005_training_engine.sql` twice in SQLite in-memory DB to ensure `IF NOT EXISTS` constructs prevent runtime crashes.
