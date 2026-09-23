# Plan

## Overview
This plan establishes a clinically rigorous, defensively programmed execution strategy for integrating the 5-pillar research dossier into the Health Agent System under strict resource boundaries (1 vCPU, 1 GB RAM, SQLite). It enforces an absolute separation between non-deterministic linguistic reasoning (LLM semantic extraction and motivational interviewing) and deterministic physiological computations (pure Python 3.11 with Pydantic V2 schemas). The system is hardened to protect a single user undergoing Tirzepatide therapy from sarcopenia, catabolic muscle loss, and dehydration while systematically eliminating all mathematical and thermodynamic bugs in the core engine.

## Scope
- In:
  - Correction of core metabolic formulas: Alpert fat mobilization rate fixed to 69.3 kcal/kg fat/day (with safety factor 0.70 producing 48.5 kcal/kg/day), verification of signed delta subtraction in `adaptive_tdee`, sex-differentiated FFMI floors (19.0 male / 15.0 female), essential body fat limits (5% male / 12% female), and `d_lean >= 0` directional gate for LBM loss alerts.
  - Development of deterministic 1-compartment pharmacokinetic (PK) model for Tirzepatide ($t_{1/2} = 5.0\text{ days}$, accumulation factor 1.61, GI peak monitoring 24–48h post-dose, MATADOR refeed trough monitoring days 6–7).
  - Implementation of zero-dependency pure-Python Coggan Performance Management Chart (CTL, ATL, TSB, Banister TRIMP, ACWR) and time-domain HRV engine (RMSSD, SDNN, pNN50).
  - Deterministic clinical laboratory biomarker evaluation: HOMA-IR, TyG index, CKD-EPI 2021 eGFR without race coefficient, and Devine Cockcroft-Gault with Adjusted Body Weight (ABW) for BMI $\ge 30$, including renal protein guardrails.
  - Implementation of the Binge Risk Triad Score (BRTS) adherence telemetry engine integrating 5-day caloric deficit, sleep duration, breakfast protein content, and retrospective logging latency ($\Delta t_{\text{log}}$).
  - Master system prompt restructuring with Fairburn CBT-E regular eating rhythms, Marlatt ACT 15-minute Urge Surfing protocols, Miller & Rollnick OARS reflective listening, and the biophysical Whoosh Effect protocol.
  - Zero-heavy-ML ingestion: asynchronous Open Food Facts API v2 client with SQLite caching, streaming Garmin .FIT decoder (`fitdecode`), and streaming Apple Health XML parser (`iterparse` with `elem.clear()`).
  - Isolated subprocess PuLP MILP diet basket optimizer with bounded execution timeout, cardiovascular saturated fat limits (<10% kcal), and fiber targets ($\ge 30\text{ g}$).
- Out:
  - Any local neural network inference or heavy C-extensions (no local Whisper, PaddleOCR, Nougat, PyTorch, Transformers, MNE, or YASA).
  - Multi-user authentication, multi-tenancy, or external microservices requiring Node.js runtimes.
  - Floating-point calculations or physiological guardrail threshold evaluations inside LLM prompts.
  - Direct execution of unconstrained linear programming within the main bot daemon process.

## Phases
### Phase 1: Core Mathematical Defect Elimination & Guardrail Normalization
**Goal**: Correct fundamental thermodynamic constants, eliminate false-positive catabolism alerts, enforce sex-differentiated morphological baselines, and harden deterministic energy floor calculations.

#### Task 1.1: Alpert Constant Recalibration & Adaptive TDEE Regression Hardening
- Location: /opt/webapps/health_agent_system/config.yaml, /opt/webapps/health_agent_system/health_core/energy.py
- Description: Correct the Alpert fat mobilization rate from the historical 31 kcal/lb translation artifact to the validated physical constant of 69.3 kcal/kg fat/day (with safety factor 0.70 yielding 48.5 kcal/kg fat/day). Hard-code regression assertions confirming that `adaptive_tdee` subtracts signed weight delta (`mean_intake - (delta_weight * 7700 / window_days)`), correctly raising TDEE during weight loss.
- Estimated Tokens: 1600
- Dependencies: None
- Steps:
  - In `/opt/webapps/health_agent_system/config.yaml`, update `policy.fat_supply_kcal_per_kg` from `31` to `69.3`, retaining `policy.fat_supply_safety: 0.7`.
  - In `/opt/webapps/health_agent_system/health_core/energy.py`, update module docstrings and `kcal_floor` comments referencing Seymour Alpert (2005) $290\text{ kJ/kg} = 69.3\text{ kcal/kg}$ fat/day.
  - In `/opt/webapps/health_agent_system/health_core/energy.py`, update module self-tests at module base to validate `kcal_floor` calculations at 69.3 kcal/kg ($40.8\text{ kg fat} \times 69.3 \times 0.7 = 1979.2\text{ kcal safe deficit}$).
  - Verify that `adaptive_tdee` in `health_core/energy.py` returns `mean_intake + 550 kcal` when a user loses 1.0 kg over 14 days with 2000 kcal average intake.
- Acceptance Criteria:
  - `.venv/bin/python3 health_core/energy.py` executes cleanly without assertion errors.
  - Running `kcal_floor` with 40 kg fat mass and 2600 kcal TDEE produces a floor bounded by `macro_minimum_kcal` rather than artificially capping deficit at 868 kcal.

#### Task 1.2: Sex-Differentiated Morphological Floors & LBM Direction Gates
- Location: /opt/webapps/health_agent_system/config.yaml, /opt/webapps/health_agent_system/health_core/guards.py, /opt/webapps/health_agent_system/health_core/forecast.py
- Description: Establish sex-differentiated thresholds for Fat-Free Mass Index (`ffmi_floor_m: 19.0`, `ffmi_floor_f: 15.0`) and Essential Fat Percentage (5% for males, 12% for females). Introduce the explicit `ESSENTIAL_FAT_FLOOR` guardrail in `health_core/guards.py` and enforce directional filtering `if d_lean >= 0: return None` across all LBM loss guards.
- Estimated Tokens: 2200
- Dependencies: Task 1.1
- Steps:
  - In `/opt/webapps/health_agent_system/config.yaml`, register `guards.essential_fat_pct_m: 0.05` and `guards.essential_fat_pct_f: 0.12`.
  - In `/opt/webapps/health_agent_system/health_core/guards.py`, implement `check_essential_fat_floor(conn, user_id)` reading bioimpedance `fat_pct`, selecting floor by user sex, and dispatching a critical alert if `fat_pct < threshold`.
  - Add `check_essential_fat_floor` to `_CHECKS` tuple and register alert schema in `_INFO` dictionary in `health_core/guards.py`.
  - In `health_core/guards.py::check_lbm_ratio` and `check_lbm_drift`, ensure `d_lean >= 0` returns `None` prior to noise floor and ratio checks, preventing false alerts on muscle hypertrophy or fluid gain.
  - Synchronize `health_core/forecast.py` constants: `ESSENTIAL_FAT_PCT_M = 0.05` and `ESSENTIAL_FAT_PCT_F = 0.12`.
- Acceptance Criteria:
  - Unit tests verify that a male profile with FFMI 18.5 triggers `FFMI_FLOOR` while a female profile with FFMI 18.5 passes without alert.
  - Verification that bioimpedance readings showing muscle gain (`d_lean = +0.8 kg`) alongside fat loss (`d_weight = -1.2 kg`) produce zero alerts for `LBM_RATIO` or `LBM_DRIFT`.

### Phase 2: Deterministic Clinical & Pharmacokinetic Computation Engines
**Goal**: Build 100% deterministic Python modules for Tirzepatide pharmacokinetics, athletic load management, and renal/metabolic biomarker computation without external ML overhead.

#### Task 2.1: 1-Compartment Tirzepatide Pharmacokinetics Engine
- Location: /opt/webapps/health_agent_system/health_core/pk.py, /opt/webapps/health_agent_system/health_core/db.py, /opt/webapps/health_agent_system/bot/registry.py
- Description: Implement an analytical 1-compartment pharmacokinetic model with first-order absorption ($k_a = 1.0\text{ day}^{-1}$, $t_{1/2} = 5.0\text{ days}$, $V_d/F = 10.3\text{ L}$, $F = 0.80$) calculating blood concentration, accumulation across weekly injections ($R_{ac} \approx 1.61$), GI distress peak window (24–48 hours post-dose), and MATADOR refeed trough window (days 6–7).
- Estimated Tokens: 2400
- Dependencies: Task 1.2
- Steps:
  - Create `/opt/webapps/health_agent_system/health_core/pk.py` with `TirzepatidePK` class implementing analytical closed-form superposition: $C(t) = \sum_j \frac{F \cdot D_j \cdot k_a}{V_d (k_a - k_e)} (e^{-k_e(t-t_j)} - e^{-k_a(t-t_j)})$.
  - Add methods `current_concentration`, `is_peak_window` (24–48h post-dose), and `is_trough_window` (144–168h post-dose).
  - In `/opt/webapps/health_agent_system/health_core/db.py`, query existing `med_log` table for Tirzepatide injections (`route = 'injection'`).
  - Register read-only tool `get_glp1_status` in `/opt/webapps/health_agent_system/bot/registry.py` returning strict Pydantic V2 schema (`effective_concentration_ng_ml`, `hours_since_last_dose`, `window_status`, `nutrition_guidance`).
- Acceptance Criteria:
  - PK engine matches FDA NDA 215866 reference values: single 5 mg dose produces peak concentration at ~48 hours of ~300–350 ng/mL, decaying with 5-day half-life.
  - Multi-dose simulation across 4 weekly 5 mg doses demonstrates steady-state concentration approaching ~1.61x single-dose peak.

#### Task 2.2: Pure-Python PMC (Coggan / Banister) & HRV Telemetry Engine
- Location: /opt/webapps/health_agent_system/health_core/pmc.py, /opt/webapps/health_agent_system/health_core/hrv.py, /opt/webapps/health_agent_system/health_core/guards.py
- Description: Deploy memory-efficient (<2 MB RAM), zero-dependency PMC engine calculating Chronic Training Load (CTL, $\tau=42\text{d}$), Acute Training Load (ATL, $\tau=7\text{d}$), Training Stress Balance (TSB), Banister TRIMP from heart rate, and time-domain HRV metrics (RMSSD, SDNN, pNN50) directly from RR intervals.
- Estimated Tokens: 2500
- Dependencies: Task 1.2
- Steps:
  - Create `/opt/webapps/health_agent_system/health_core/pmc.py` implementing EWMA filters for CTL, ATL, TSB, and Acute:Chronic Workload Ratio ($ACWR = ATL / CTL$).
  - Implement `calculate_banister_trimp(duration_min, hr_avg, hr_rest, hr_max, sex)` using sex-calibrated exponential weighting.
  - Create `/opt/webapps/health_agent_system/health_core/hrv.py` computing RMSSD, SDNN, and pNN50 over valid RR intervals (300–2000 ms filter) using pure Python `math.sqrt` without NumPy or SciPy.
  - Add guardrail `check_overtraining_risk` in `health_core/guards.py` firing a warning when $ACWR > 1.5$ or when 7-day rolling RMSSD drops $>1.5$ standard deviations below baseline.
- Acceptance Criteria:
  - Verification that PMC computations require zero external C-extensions and execute in <1 ms per annual timeline.
  - Unit tests verify that RMSSD correctly calculates $\sqrt{\frac{1}{N-1}\sum \Delta RR^2}$ with zero memory leakage.

#### Task 2.3: Clinical Laboratory Biomarkers & Renal Safety Engine
- Location: /opt/webapps/health_agent_system/health_core/labs.py, /opt/webapps/health_agent_system/bot/registry.py, /opt/webapps/health_agent_system/health_core/guards.py
- Description: Implement deterministic laboratory biomarker evaluation utilizing Pydantic V2 schemas for HOMA-IR, TyG index, eGFR (CKD-EPI 2021 without race factor), and Cockcroft-Gault CrCl using Devine Adjusted Body Weight (ABW) when BMI $\ge 30$, preventing protein-induced renal stress on high-protein regimens (1.6–2.2 g/kg FFM).
- Estimated Tokens: 2600
- Dependencies: Task 1.2
- Steps:
  - Implement `calculate_homa_ir(glucose_mmol, insulin_uU)`: $(\text{glucose} \times \text{insulin}) / 22.5$.
  - Implement `calculate_tyg_index(glucose_mmol, tg_mmol)`: $\ln((tg \times 88.57 \times glucose \times 18.0182) / 2)$.
  - Implement `calculate_egfr_ckd_epi_2021(creatinine_umol, age, sex)` according to official NKF-ASN / KDIGO 2021 non-race equations.
  - Implement `calculate_cockcroft_gault_abw(creatinine_umol, age, sex, weight_kg, height_cm)`: calculate Devine IBW; if weight $> 1.2 \times \text{IBW}$, substitute $ABW = IBW + 0.4 \times (Weight - IBW)$ to eliminate pseudo-hyperfiltration artifacts in obesity.
  - Register tool `log_clinical_labs` in `/opt/webapps/health_agent_system/bot/registry.py` with strict Pydantic validation. If eGFR drops below 60 mL/min/1.73m², trigger automatic protein ceiling clamping at $\le 1.2\text{ g/kg FFM}$.
- Acceptance Criteria:
  - For 122 kg male, 185 cm, age 40, creatinine 90 umol/L: Cockcroft-Gault uses ABW (~95 kg) yielding safe clearance rather than inflated clearance from 122 kg.
  - HOMA-IR and TyG tests validate across both mmol/L and mg/dL input formats without unit confusion.

### Phase 3: Behavioral Psychology, Adherence Telemetry & Triad Guardrails
**Goal**: Integrate CBT-E, ACT, and Motivational Interviewing (OARS) into conversational prompts, deploying the Binge Risk Triad Score (BRTS) to pre-emptively catch diet fatigue and trigger maintenance refeeds.

#### Task 3.1: Binge Risk Triad Score (BRTS) Engine & Proactive Telemetry
- Location: /opt/webapps/health_agent_system/health_core/brts.py, /opt/webapps/health_agent_system/health_core/guards.py, /opt/webapps/health_agent_system/scripts/notify.py
- Description: Implement the 3-factor BRTS index ($0.4 S_{deficit} + 0.3 S_{sleep} + 0.3 S_{behavior} \in [0, 100]$). Monitor accumulated 5-day deficit ($>3500\text{ kcal}$), sleep deprivation ($<6\text{ hours}$), breakfast protein omission ($<25\text{ g}$), and logging latency ($\Delta t_{\text{log}} > 6\text{h}$ retrospective dumping). Automate proactive MATADOR refeed recommendations when BRTS indicates critical fatigue.
- Estimated Tokens: 2500
- Dependencies: Tasks 1.1, 2.1
- Steps:
  - Create `/opt/webapps/health_agent_system/health_core/brts.py` computing $S_{deficit}$, $S_{sleep}$, and $S_{behavior}$ from SQLite tables (`food_log`, `activity`, `sleep_log`).
  - Calculate logging latency: $\Delta t_{\text{log}} = t_{server\_log} - t_{eaten\_time}$. Flag retrospective dumping if 3-day rolling latency exceeds 6 hours.
  - Incorporate Tirzepatide PK attenuation factor: damp the deficit subscore by 40% when serum concentration is within the peak window (24–48h post-dose).
  - Implement guardrail `check_binge_risk_triad` in `health_core/guards.py`:
    - Yellow alert ($35 \le BRTS < 65$): recommend +200 kcal complex carbs and bedtime advancement.
    - Red alert ($BRTS \ge 65$ across $\ge 2$ consecutive days): dispatch critical alert triggering 48h MATADOR maintenance refeed (100% TDEE) and ACT coaching intervention.
  - Connect BRTS evaluation into morning cron pipeline `/opt/webapps/health_agent_system/scripts/notify.py`.
- Acceptance Criteria:
  - Synthetic test simulating 4 consecutive days of 800 kcal deficit, 5 hours sleep, and delayed evening logging yields BRTS $> 70$, firing `BINGE_RISK_CRITICAL` and auto-proposing a maintenance refeed.
  - Zero-data days safely produce a neutral score without triggering false alarms.

#### Task 3.2: CBT-E, ACT & OARS Dialectical Alignment in Core System Prompt
- Location: /opt/webapps/health_agent_system/Core/system_promt.md, /opt/webapps/health_agent_system/bot/llm.py
- Description: Update the master agent system prompt to embed Fairburn's regular eating anchor (3 meals + 2 snacks every 3–4 hours), Marlatt's 15-minute ACT Urge Surfing protocol, Miller & Rollnick OARS reflective listening, and the biophysical Whoosh Effect explanation. Eliminate patronizing praise and toxic positivity.
- Estimated Tokens: 2800
- Dependencies: Task 3.1
- Steps:
  - In `Core/system_promt.md`, ban judgmental feedback, patronizing praise ("Молодец!", "Умница!"), and emoji cheerleading; mandate OARS validation of autonomy and objective effort.
  - Embed the structured 4-step ACT Urge Surfing protocol for food cravings: 1) Somatization/Location, 2) Grounding breath (4-in, 6-out), 3) Wave observation (1–10 scale), 4) Strict 15-minute exposure timer before caloric decisions.
  - Formulate the Lapse Debriefing protocol: forbid compensatory fasting or excessive cardio after an overeating episode; guide user to immediate CBT-E meal rhythm resumption.
  - Formalize the biophysical Whoosh Effect protocol: explain transient cortisol-mediated adipocyte water retention during continuous deficits to eliminate scale-induced panic.
- Acceptance Criteria:
  - Test prompts with simulated user lapses ("Я сорвался на пиццу") elicit empathetic OARS reflective listening without judgment or compensatory workout orders.
  - Requests regarding intense cravings trigger the 15-minute Urge Surfing dialogue flow.

### Phase 4: Lightweight Ingestion Pipelines & Optimization Subprocesses
**Goal**: Implement zero-RAM-overhead external data ingestion (Open Food Facts, streaming FIT/XML parsers) and memory-bounded diet optimization on Debian VPS.

#### Task 4.1: Asynchronous Open Food Facts & Barcode Ingestion Client
- Location: /opt/webapps/health_agent_system/health_core/ingest/openfoodfacts.py, /opt/webapps/health_agent_system/bot/registry.py
- Description: Deploy a lightweight async HTTP client using `httpx` for barcode (EAN-13/UPC) lookups via Open Food Facts API v2. Parse macronutrients, dietary fiber, and NOVA ultra-processing category into SQLite cache with <5 MB RAM footprint.
- Estimated Tokens: 1800
- Dependencies: Task 1.2
- Steps:
  - Create `/opt/webapps/health_agent_system/health_core/ingest/openfoodfacts.py` using `httpx.AsyncClient(timeout=5.0)`.
  - Fetch product by barcode: `https://world.openfoodfacts.org/api/v2/product/{barcode}.json`.
  - Extract `product_name_ru`, `proteins_100g`, `fat_100g`, `carbohydrates_100g`, `fiber_100g`, and `nova_group` (1–4).
  - Cache results in SQLite `food_database` table to minimize external HTTP calls.
  - Expose tool `lookup_barcode` in `/opt/webapps/health_agent_system/bot/registry.py`.
- Acceptance Criteria:
  - Valid EAN barcode returns validated Pydantic model with protein, fiber, and NOVA class in <300 ms.
  - Invalid or offline lookups degrade gracefully without raising unhandled exceptions in the bot event loop.

#### Task 4.2: Streaming Telemetry Parsers (FIT & Apple Health / TCX XML)
- Location: /opt/webapps/health_agent_system/health_core/ingest/fit.py, /opt/webapps/health_agent_system/health_core/ingest/apple_health.py, /opt/webapps/health_agent_system/health_core/ingest/tcx.py
- Description: Implement streaming data ingestion for Garmin .FIT files via `fitdecode` and Apple Health / TCX XML files using `xml.etree.ElementTree.iterparse` with explicit `elem.clear()` to enforce strict $O(1)$ memory usage (<15 MB RAM) when parsing multi-gigabyte exports.
- Estimated Tokens: 2200
- Dependencies: Task 1.2
- Steps:
  - Integrate `fitdecode` in `health_core/ingest/fit.py` reading records via generator without buffering binary chunks into RAM.
  - In `health_core/ingest/apple_health.py`, build `stream_apple_health(xml_path, target_types)` using `iterparse(events=('end',))` and invoking `elem.clear()` on every processed element.
  - Verify `health_core/ingest/tcx.py` enforces the `in_lap` reset fix so workout calories and heart rates are not dropped.
- Acceptance Criteria:
  - Ingestion of a simulated 500 MB Apple Health XML file executes with resident process memory remaining strictly below 40 MB RAM throughout parsing.

#### Task 4.3: Memory-Bounded PuLP Diet Basket Optimizer Subprocess
- Location: /opt/webapps/health_agent_system/health_core/nutrition_optimizer.py, /opt/webapps/health_agent_system/scripts/optimize_pantry.py
- Description: Implement a mixed-integer linear programming (MILP) food basket optimizer in PuLP using the CBC solver. Enforce protein target (1.6–2.2 g/kg FFM), fiber floor ($\ge 30\text{ g}$), potassium floor ($\ge 3500\text{ mg}$), and strict cardiovascular saturated fatty acid limit ($<10\%$ of daily calories). Execute solver via isolated `subprocess.run` to guarantee instantaneous OS memory reclamation.
- Estimated Tokens: 2400
- Dependencies: Tasks 1.1, 2.3
- Steps:
  - Build `/opt/webapps/health_agent_system/health_core/nutrition_optimizer.py` formulating the Stigler diet problem with constraints: energy corridor ($\pm 5\%$), protein floor, SFA upper bound, and fiber minimum.
  - Wrap execution in `/opt/webapps/health_agent_system/scripts/optimize_pantry.py` invoked via `subprocess.run([sys.executable, ...], timeout=15)` to prevent Python `pymalloc` heap fragmentation in the main bot process.
  - Add explicit `timeLimit=10` parameter to `PULP_CBC_CMD(msg=False)`.
  - Fall back cleanly to a heuristic whole-food pantry template if the linear program returns infeasible status or times out.
- Acceptance Criteria:
  - Solver successfully outputs an optimal grocery basket meeting protein and fiber targets while keeping saturated fats $<10\%$ of calories.
  - Complete execution cycle uses <25 MB RAM and releases 100% of memory back to Debian upon subprocess termination.

## Testing Strategy
- Mathematical and Guardrail Unit Tests:
  - Run existing test suites: `.venv/bin/python3 test_e2e.py` and `.venv/bin/python3 test_bot.py`.
  - Execute module self-checks: `.venv/bin/python3 health_core/energy.py`, `.venv/bin/python3 health_core/guards.py`, and `.venv/bin/python3 health_core/forecast.py`.
  - Add dedicated test suite `tests/test_clinical_math.py` asserting exact numerical outputs for HOMA-IR, TyG index, CKD-EPI 2021, Cockcroft-Gault ABW, and Tirzepatide PK accumulation formulas against validated clinical datasets.
- Memory and Resource Profiling:
  - Profile resident set size (RSS) during execution using `psutil` or `/usr/bin/time -v`.
  - Assert total Telegram bot daemon memory consumption remains strictly under 350 MB RAM under maximum simulated load.
  - Stress test streaming XML parser against multi-gigabyte synthetic files, verifying peak RAM never exceeds 45 MB.
- Dual-System Interface & Tool Calling Tests:
  - Assert that all tool responses return strictly formatted JSON matching Pydantic V2 schemas.
  - Verify sequential execution of tool calls in `bot/llm.py` under simulated concurrent triggers, verifying zero `sqlite3.OperationalError: database is locked` exceptions.
- Behavioral Dialogue Verification:
  - Test LLM prompt responses against overeating, binge urges, and weight plateaus to confirm strict adherence to OARS, ACT 15-minute Urge Surfing, and zero judgmental feedback.

## Risks
- Risk: Memory exhaustion and Linux OOM Killer invocation on 1 GB RAM VPS due to concurrent tool calls or temporary memory fragmentation during MILP optimization.
  - Mitigation: Heavy operations (PuLP CBC solver, large XML imports) are quarantined in ephemeral CLI subprocesses (`subprocess.run`) with strict execution timeouts; all core math engines use pure-Python data structures without NumPy/Pandas.
- Risk: SQLite database concurrency collisions (`database is locked`) when Telegram polling, background cron notifications, and Withings webhooks write simultaneously.
  - Mitigation: Maintain SQLite in WAL (Write-Ahead Logging) mode with `PRAGMA busy_timeout = 5000` and enforce sequential tool execution via per-user asyncio locks in `bot/llm.py`.
- Risk: Under-fueling or severe hypocaloric stress during Tirzepatide dose escalation due to extreme appetite suppression masking real energy needs.
  - Mitigation: Enforce deterministic `UNDEREATING` guardrail and `BMR_FLOOR` hard limit; trigger automatic protein and fluid reminders even when user reports zero subjective hunger.
- Self-critique: The plan delegates diet basket optimization to PuLP's CBC solver via an isolated subprocess to prevent Python heap memory fragmentation, but fails to enforce an explicit CPU time limit (`timeLimit=10`) or handle the scenario where the precompiled CBC binary is absent or dynamically incompatible on minimal Debian architectures. On a single vCPU VM, a stalled integer branch-and-bound search would peg CPU utilization at 100%, causing Telegram webhook timeouts and dropping user chat messages until the OS watchdog intervenes.
  - Mitigation: Add explicit `timeLimit=10` parameter to `PULP_CBC_CMD(msg=False)` and implement an immediate try/except fallback to deterministic rule-based pantry templates if CBC fails or times out.
- Self-critique: The Binge Risk Triad Score (BRTS) weights (0.4 deficit, 0.3 sleep, 0.3 behavioral) and threshold cutoffs (35/65) are derived from clinical literature heuristics rather than calibrated on historical telemetry from this specific patient. This creates a concrete failure mode of "alert fatigue" where early false-positive RED alarms force unwarranted +500 kcal refeeds, stalling fat loss and degrading trust in the agent's coaching.
  - Mitigation: Introduce a configurable damping factor in `config.yaml` (`brts_sensitivity: 0.8`), require at least 2 consecutive days of elevated scores before triggering an automated refeed recommendation, and provide a transparent status breakdown showing exact factor scores in the admin panel.

## Rollback Plan
- Code and Schema Versioning:
  - All modifications managed via atomic Git commits tagged per task (e.g., `git tag phase1-core-fixes`).
  - Prior to applying database migrations or schema adjustments, execute an atomic SQLite backup: `cp /opt/webapps/health_agent_system/health.db /opt/webapps/health_agent_system/health.db.bak.$(date +%Y%m%d%H%M%S)`.
- Reversion Procedures:
  - If a newly deployed module (`health_core/pk.py` or `health_core/brts.py`) causes runtime degradation, revert via `git revert <commit_hash>` and restart systemd service `systemctl restart health-agent.service`.
  - If `config.yaml` policy adjustments cause unexpected calorie target shifts, restore from backup: `cp config.bak config.yaml` (the mtime-based config loader automatically hot-reloads within 1 second without bot downtime).
- Operational Safety Fallback:
  - If any mathematical engine fails in runtime, the system falls back to standard static targets (`bmr_floor` and conservative 500 kcal deficit) while logging the complete traceback for developer analysis.

## Edge Cases
- Rapid Weight Loss on Tirzepatide Exceeding 1.5% Mass/Week:
  - Handled by `RATE_HIGH` guardrail; triggers immediate caloric floor increase and prompts user to consult prescribing endocrinologist regarding dose down-titration.
- Weight Plateau Accompanied by Stable or Rising Phase Angle ($PhA$):
  - Differentiates true metabolic stagnation from cortisol-induced water retention (Whoosh Effect); suppresses panic alerts and advises continuation of current protocol.
- Significant Fall in Phase Angle ($PhA > 0.5^\circ$ drop) or Albumin:
  - Flags acute lean mass catabolism; triggers immediate protein increase to 2.2 g/kg FFM, initiates a 48-hour MATADOR refeed, and restricts heavy cardiovascular exercise.
- Severely Elevated Serum Creatinine or Impaired Renal Clearance (eGFR < 60 mL/min/1.73m²):
  - Automatically disables high-protein targets (>1.6 g/kg FFM), caps protein target at 1.2 g/kg FFM, and issues an urgent clinical advisory for nephrology follow-up.
- Long-Distance Time-Zone Travel Disrupting Circadian Rhythms:
  - Telemetry parser normalizes timestamps using ISO-8601 UTC offsets; caffeine curfew and evening carbohydrate restrictions dynamically re-anchor to local sleep onset time.
- Zero-Data Days (User Skips Food Logging for Multiple Consecutive Days):
  - Handled by SPEC.md principle: "No data, no judgment". BRTS deficit component safely degrades to neutral zero instead of hallucinating infinite deficit or triggering false alarms.

## Open Questions
- None: all architectural patterns, mathematical equations, clinical thresholds, and memory constraints are fully resolved from the 5-pillar research dossier.
