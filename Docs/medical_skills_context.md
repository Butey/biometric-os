# Comprehensive Medical & Metabolic AI Skills Context

A consolidated, structured reference catalog of specialized clinical, metabolic, pharmacological, genomic, and diagnostic AI skills synthesized from:
1. **Weight Loss & Metabolic Health Analyzer**: https://mcpmarket.com/tools/skills/weight-loss-metabolic-analyzer
2. **OpenClaw Medical Skills (FreedomIntelligence)**: https://github.com/FreedomIntelligence/OpenClaw-Medical-Skills
3. **Awesome Medical AI Skills (JuneYaooo)**: https://github.com/JuneYaooo/awesome-medical-ai-skills
4. **Open Medical Skills (Open-Medica / Harvard MIMS)**: https://github.com/Open-Medica/open-medical-skills

### Primary Repositories & Reference Sources
* **Weight Loss & Metabolic Health Analyzer**: [MCP Market - Weight Loss & Metabolic Analyzer](https://mcpmarket.com/tools/skills/weight-loss-metabolic-analyzer)
* **OpenClaw Medical Skills**: [GitHub - FreedomIntelligence/OpenClaw-Medical-Skills](https://github.com/FreedomIntelligence/OpenClaw-Medical-Skills) (869 clinical, pharma, genomic, and bioinformatics agent skills)
* **Awesome Medical AI Skills**: [GitHub - JuneYaooo/awesome-medical-ai-skills](https://github.com/JuneYaooo/awesome-medical-ai-skills) (Curated medical skills, MCP servers, wearable telemetry, and health assistants)
* **Open Medical Skills**: [GitHub - Open-Medica/open-medical-skills](https://github.com/Open-Medica/open-medical-skills) (747 physician-reviewed clinical tools including Harvard MIMS ToolUniverse)
* **Harvard MIMS ToolUniverse**: [GitHub - mims-harvard/ToolUniverse](https://github.com/mims-harvard/ToolUniverse) (Underlying biomedical tool database for clinical calculators and lab analytics)

> **Target Use:** Modular context injection for LLM system prompts, medical multi-agent orchestrators, diagnostic assistants, and health copilot systems.

---

## 1. Weight Loss, Metabolism & Body Composition

### 1.1 Anthropometry & Body Composition
* **BMI & Adiposity Profiling:**
  * Global WHO classification vs. Asian-specific criteria (Overweight ≥23, Obese ≥25).
  * Body Fat Percentage ($BF\%$) estimation via Jackson-Pollock skinfold formulas, Navy tape circumference method, or DXA/BIA data ingestion.
  * Lean Body Mass (LBM) and Fat-Free Mass (FFM) derivation: $\text{FFM} = \text{Weight} \times (1 - BF\%)$.
  * Central Adiposity Indices: Waist-to-Hip Ratio ($\text{WHR} > 0.90 \text{ M}, > 0.85 \text{ F}$) and Waist-to-Height Ratio ($\text{WHtR} > 0.5$ visceral risk threshold).
  * Broca & Devine formulas for Ideal Body Weight (IBW) and Adjusted Body Weight ($ABW = IBW + 0.4 \times (Actual - IBW)$) for clinical dosing.

### 1.2 Basal Metabolic Rate (BMR) & Daily Energy Expenditure (TDEE)
* **BMR Calculation Engines:**
  * **Mifflin-St Jeor:** Standard gold standard for general and overweight populations ($10 \times W + 6.25 \times H - 5 \times A + s$, where $s = +5 \text{ male}, -161 \text{ female}$).
  * **Katch-McArdle:** FFM-based equation preferred when body fat is known ($370 + 21.6 \times \text{FFM}_{\text{kg}}$).
  * **Cunningham:** High-muscle/athletic population formula ($500 + 22 \times \text{FFM}_{\text{kg}}$).
  * **Harris-Benedict (Revised 1984):** Historical baseline comparison.
* **TDEE Physical Activity Level (PAL) Multipliers:**
  * Sedentary ($1.2$), Lightly Active ($1.375$), Moderately Active ($1.55$), Very Active ($1.725$), Extremely Active / Athlete ($1.9$).
  * Non-Exercise Activity Thermogenesis (NEAT) and Exercise Activity Thermogenesis (EAT) modular adjustments.

### 1.3 Caloric Deficit, Target Velocity & Metabolic Safety Floors
* **Deficit Modeling:**
  * Classical Wishnofsky Rule ($\approx 7,700 \text{ kcal/kg fat loss}$).
  * Hall Dynamic Energy Balance Model: Non-linear compensation reflecting metabolic slowdown, leptin reduction, and decreasing thermic effect of food.
  * Recommended Safe Loss Rate: $0.5\% - 1.0\%$ body weight per week ($0.25 - 1.0 \text{ kg/week}$).
* **Metabolic Safety Guardrails:**
  * Absolute Caloric Floors: Minimum $1,500 \text{ kcal/day}$ (males), $1,200 \text{ kcal/day}$ (females).
  * Relative Caloric Floor: $\text{Intake} \ge \text{BMR} \times 1.15$ to prevent severe hormonal dysregulation (thyroid T3 downregulation, hypothalamic amenorrhea, excessive cortisol).

### 1.4 Macronutrient Optimization & Satiety
* **Protein Target Calculation:**
  * Deficit preservation targets: $1.6 - 2.4 \text{ g/kg FFM}$ (or $1.2 - 2.0 \text{ g/kg total weight}$).
  * Minimum Leucine Threshold: $\ge 2.5 - 3.0 \text{ g leucine/meal}$ for maximal Muscle Protein Synthesis (MPS).
* **Dietary Splits:**
  * Fat requirement: Minimum $0.6 - 0.8 \text{ g/kg}$ for steroid hormone synthesis and fat-soluble vitamin absorption.
  * Carbohydrates: Remainder of caloric allocation based on training volume, glycogen needs, and insulin sensitivity.

### 1.5 Adaptation, Stalls & Refeed Protocols
* **Plateau Detection:** Weight stagnation $>14\text{ days}$ with $<0.5\text{ kg}$ variance despite verified caloric deficit.
* **Differential Classification:** Water retention (cortisol-induced fluid retention) vs. true metabolic adaptation (adaptive thermogenesis) vs. unrecorded intake.
* **Diet Break & Refeed Protocols:** 48-hour carbohydrate-focused refeeds at maintenance calories to restore leptin, glycogen, and thyroid output.

---

## 2. Clinical Medicine, Diagnostics & Critical Care

### 2.1 Clinical Calculators & Risk Scoring Engines
* **Cardiology & Hemodynamics:**
  * $\text{CHA}_2\text{DS}_2\text{-VASc}$ (Atrial Fibrillation stroke risk & anticoagulation indication).
  * ASCVD 10-Year Risk & Framingham Risk Score.
  * TIMI & GRACE Scores for Acute Coronary Syndromes (ACS).
  * Shock Index ($\text{HR} / \text{SBP} > 0.9$ indicating occult hypoperfusion).
* **Pulmonary & Critical Care:**
  * Wells Criteria & Geneva Score for Pulmonary Embolism (PE) and DVT; PERC rule.
  * CURB-65 & PSI/PORT for Community-Acquired Pneumonia mortality risk.
  * SOFA & qSOFA (Sequential Organ Failure Assessment) for sepsis screening.
  * APACHE II & SAPS II ICU mortality prediction systems.
  * Berlin Definition criteria for ARDS severity ($P/F\text{ ratio}$).
* **Gastroenterology, Nephrology & Neurology:**
  * MELD & MELD-Na (Model for End-Stage Liver Disease) score.
  * CKD-EPI (2021) & MDRD equations for eGFR estimation.
  * Glasgow Coma Scale (GCS) and NIH Stroke Scale (NIHSS).
  * Maddrey's Discriminant Function (Alcoholic Hepatitis).

### 2.2 Differential Diagnosis (DDx) & Clinical Reasoning
* **Symptom Triage & Red-Flag Filtering:**
  * Emergency acuity grading (ESI levels 1–5: Resuscitation to Non-urgent).
  * Red-flag symptom detection for chest pain, acute abdominal pain, thunderclap headache, focal neurological deficits, and cauda equina syndrome.
* **SOAP Note Generation & Documentation:**
  * Structured generation of Subjective (HPI, ROS), Objective (Vitals, Physical Exam, Labs, Imaging), Assessment (Primary Dx, Problem List, DDx), and Plan (Workup, Rx, Monitoring, Patient Education).
* **Emergency & Resuscitation Protocols:**
  * ACLS / PALS algorithms (VF/VTach, PEA/Asystole, Bradycardia, Tachycardia with pulse).
  * Acute Toxicology & Antidote Finder (Acetaminophen $\to$ NAC, Opioids $\to$ Naloxone, Beta-blockers $\to$ Glucagon/High-dose insulin, Anticholinergics $\to$ Physostigmine).

### 2.3 Laboratory Interpretation & Acid-Base Analytics
* **Arterial Blood Gas (ABG) Interpreter:**
  * Stepwise acid-base analysis: Primary disorder identification ($\text{pH}, \text{pCO}_2, \text{HCO}_3^-$).
  * Anion Gap ($\text{AG} = \text{Na}^+ - (\text{Cl}^- + \text{HCO}_3^-)$, normal $12 \pm 4$).
  * Winter's Formula for metabolic acidosis respiratory compensation: $\text{Expected } \text{pCO}_2 = 1.5 \times [\text{HCO}_3^-] + 8 \pm 2$.
  * Delta-Delta Ratio ($\Delta\text{AG} / \Delta\text{HCO}_3^-$) for mixed acid-base disorders.
* **Hematology & Chemistry Diagnostic Matrices:**
  * CBC differential (Microcytic/Normocytic/Macrocytic anemia, Left shift, Leukemoid vs Leukaemia).
  * Liver Function Tests (R-ratio for hepatocellular vs. cholestatic vs. mixed injury).
  * Electrolyte Osmolality & Fractional Excretion of Sodium ($\text{FeNa}$) / Urea ($\text{FeUrea}$) in acute kidney injury.

---

## 3. Pharmacology, Pharmacokinetics & Drug Discovery

### 3.1 Clinical Pharmacology & Pharmacokinetics (PK/PD)
* **Renal & Hepatic Dose Adjustment:**
  * Cockcroft-Gault CrCl calculations for drug labeling adjustments.
  * Direct Oral Anticoagulants (DOAC: Apixaban, Rivaroxaban, Dabigatran) renal cutoff matrices.
  * Child-Pugh class adjustments for hepatic metabolism.
* **Therapeutic Drug Monitoring (TDM):**
  * Vancomycin AUC/MIC target ($400 - 600 \text{ mg}\cdot\text{h/L}$) pharmacokinetic Bayesian dosing.
  * Digoxin, Lithium, Phenytoin (Winter-Tozer albumin correction), Theophylline, and Aminoglycoside peak/trough calculators.
* **Drug-Drug Interaction (DDI) & Adverse Effects:**
  * Cytochrome P450 (CYP1A2, CYP2C9, CYP2C19, CYP2D6, CYP3A4) substrate, inhibitor, and inducer interaction matrices.
  * QTc prolongation risk scoring (Tisdale score) and additive torsadogenic risk.
  * Serotonin toxicity screening (Hunter criteria).
  * FDA FAERS pharmacovigilance query and signal detection.

### 3.2 Cheminformatics & Drug Discovery Tools
* **Molecular Analytics & Screening:**
  * SMILES / InChI parsing, molecular weight, LogP, TPSA, hydrogen bond donors/acceptors (Lipinski's Rule of 5).
  * ChEMBL & PubChem bioactivity, binding affinity ($IC_{50}, K_i, EC_{50}$) retrieval.
  * ADMET property prediction (Absorption, Distribution, Metabolism, Excretion, Toxicity).
  * Target identification, pocket analysis, and docking score integration.

---

## 4. Genomics, Bioinformatics & Molecular Medicine

### 4.1 Variant Annotation & Clinical Genomics
* **Variant Interpretation (ACMG/AMP Guidelines):**
  * Criteria evaluation: PVS1 (null variant), PS1-4 (strong evidence), PM1-6 (moderate), PP1-5 (supporting), BP1-7 (benign).
  * Classification: Pathogenic, Likely Pathogenic, Variant of Uncertain Significance (VUS), Likely Benign, Benign.
* **Database & Tool Connectors:**
  * VEP (Variant Effect Predictor), ClinVar accession, gnomAD allele frequencies.
  * Functional impact scores: CADD, REVEL, AlphaMissense, SpliceAI.

### 4.2 Transcriptomics & Precision Oncology
* **RNA-Seq & Single-Cell Analysis Pipelines:**
  * Differential Gene Expression (DESeq2, edgeR) interpretation.
  * Single-cell marker gene exploration (Seurat context), cell type annotation.
  * Pathway enrichment & functional gene set analysis (GSEA, KEGG, Reactome, Gene Ontology).
* **Oncology & Precision Therapy:**
  * Tumor Mutational Burden (TMB) and Microsatellite Instability (MSI-H/dMMR) analysis.
  * Actionable somatic mutations (EGFR, KRAS, BRAF, ALK, ROS1, HER2) mapped to NCCN-approved targeted therapies.
  * Polygenic Risk Score (PRS) interpretation for complex cardiometabolic phenotypes.

---

## 5. Wearables, Continuous Health Telemetry & Remote Monitoring

### 5.1 Biometric Telemetry & Wearables Integration
* **Data Sources:** Apple HealthKit, Health Connect, Garmin, Oura, Withings, Whoop, Fitbit.
* **Heart Rate Variability (HRV) & Autonomic Balance:**
  * Time-domain metrics: RMSSD (parasympathetic recovery), SDNN (overall autonomic variability).
  * Frequency-domain metrics: LF/HF ratio (sympathovagal balance).
  * Baseline drift detection for systemic illness, overtraining syndrome, and high physiological strain.
* **Sleep Architecture & Circadian Metrics:**
  * Deep (SWS), REM, and Light sleep stage distribution analysis.
  * Sleep efficiency, latency, WASO (Wake After Sleep Onset), and oxygen desaturation events.
* **Continuous Glucose Monitoring (CGM) Analytics:**
  * Time in Range (TIR: $70 - 180 \text{ mg/dL}$), Time Below Range (TBR $<70 \text{ mg/dL}$), Time Above Range (TAR $>180 \text{ mg/dL}$).
  * Glucose Management Indicator (GMI) and Glycemic Variability ($\text{CV} < 36\%$ stability target).

---

## 6. Nutrition, Lifestyle & Behavioral Health

### 6.1 Clinical & Specialized Nutrition
* **Micronutrient & Biomarker Evaluation:**
  * Dietary reference intakes (RDA, AI, UL) for 30+ micronutrients (Vitamin D, B12, Iron/Ferritin, Magnesium, Zinc, Potassium).
  * Electrolyte replenishment protocols for refeeding syndrome prevention (Phosphate, Potassium, Magnesium monitoring).
* **Therapeutic Diets:**
  * Renal Diet (Sodium $<2000\text{mg}$, Potassium/Phosphorus restriction).
  * Diabetic Diet (Glycemic Index/Load moderation, carbohydrate counting).
  * Low-FODMAP protocol for Irritable Bowel Syndrome (IBS) elimination/reintroduction.
  * Therapeutic Ketogenic diet (Beta-hydroxybutyrate target $0.5 - 3.0 \text{ mmol/L}$).

### 6.2 Psychometrics & Mental Health Screening
* **Standardized Clinical Inventories:**
  * **PHQ-9:** Patient Health Questionnaire for depression severity ($0-27$).
  * **GAD-7:** Generalized Anxiety Disorder assessment ($0-21$).
  * **ISI:** Insomnia Severity Index ($0-28$).
  * **AUDIT / CAGE:** Alcohol use disorder screening.
* **Behavioral Therapy & Habit Tracking:**
  * Cognitive Behavioral Therapy (CBT) thought log parsing and cognitive distortion detection (catastrophizing, all-or-nothing thinking, emotional reasoning).
  * Crisis triage and suicide risk detection triggers (immediate hotlines, emergent escalation).

---

## 7. Healthcare Interoperability & Medical Device Regulations

### 7.1 Interoperability & EHR Data Standards
* **HL7 FHIR (Fast Healthcare Interoperability Resources) Core Resources:**
  * `Patient`, `Observation` (Vitals, Labs, Wearable data), `Condition` (Problem list, ICD-10/SNOMED-CT).
  * `MedicationRequest`, `MedicationStatement` (RxNorm), `DiagnosticReport` (LOINC).
  * `Encounter`, `AllergyIntolerance`, `Immunization`.
* **Standard Terminologies:**
  * ICD-10-CM / ICD-11 (Diagnoses), SNOMED CT (Clinical terms), LOINC (Lab and clinical observations), RxNorm (Clinical drugs), CPT (Procedures).

### 7.2 Regulatory, Compliance & AI Safety
* **Software as a Medical Device (SaMD) & Quality Systems:**
  * FDA classification (Class I, Class II 510(k), Class III PMA, De Novo).
  * IEC 62304 (Medical device software lifecycle) & ISO 14971 (Risk management for medical devices).
  * HIPAA / GDPR compliance (Safe Harbor 18 PHI identifier de-identification).

---

## 8. Multi-Tier Medical Safety & Guardrail Architecture

For all downstream LLM agent orchestrators, skills must operate within a strict 5-tier autonomy framework:

| Level | Classification | Action Scope | Human Oversight Required |
|---|---|---|---|
| **Level 1** | **General Information & Wellness** | Education, fitness calculations, general dietary guidelines | None (Automated) |
| **Level 2** | **Diagnostic & Clinical Support** | Differential diagnosis suggestions, lab interpretation, risk scoring | Clinician Review Required |
| **Level 3** | **Therapeutic Protocols** | Drug dosing calculation, nutrition plans for pathology, triage | Mandatory Physician Approval |
| **Level 4** | **High-Risk Decisions** | Chemotherapy regimens, critical care titration, invasive orders | Dual Specialist Verification |
| **Level 5** | **Emergency Escalation** | Red-flag symptom triggers, acute MI, stroke, suicidal ideation | Immediate Emergency Triage Alert |

### Mandatory Safety Guardrails
1. **Never Prescribe or Modify Prescription Medications** autonomously without explicit physician in the loop.
2. **Immediate Red-Flag Triage:** If critical life-threatening conditions (chest pain with radiation, acute neurological deficit, acute abdomen, anaphylaxis, severe hypotension, suicidal ideation) are detected, immediately prioritize emergency medical evaluation.
3. **Caloric & Nutritional Minimums:** Never generate or approve caloric targets below safe physiological floors ($1200\text{ kcal/d}$ women, $1500\text{ kcal/d}$ men, or $\text{BMR} \times 1.15$).
4. **Evidence Grading:** All clinical claims should reference standard evidence grades (e.g., USPSTF, ACC/AHA, ADA, KDIGO, NICE).
