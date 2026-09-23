# Plan

## Overview
Validate the end-to-end execution of the Council pipeline in the Health Agent System through an automated smoke test suite. The approach tests multi-agent deliberation, consensus aggregation, and safety guardrails across council agents using deterministic synthetic scenarios.

## Scope
- In:
  - Execution of Council deliberation flow using mock agent adapters.
  - Verification of consensus scoring, safety policy enforcement, and structured output schema compliance.
  - Validation of latency and error handling under mock agent timeout conditions.
- Out:
  - Integration with production EHR backends or live patient data stores.
  - Fine-tuning or prompt engineering of underlying clinical LLMs.
  - Multi-node stress or load testing beyond single-instance smoke validation.

## Phases
### Phase 1: Pipeline Smoke Test & Validation
**Goal**: Execute deterministic health agent council flow and assert schema compliance, consensus aggregation, and safety guardrails.

#### Task 1.1: Council Pipeline End-to-End Smoke Test
- Location: `tests/council/test_pipeline_validation.py`, `src/health_agent/council/pipeline.py`
- Description: Implement and execute an automated integration test validating council member invocation, vote aggregation, and triage output structure.
- Estimated Tokens: 1200
- Dependencies: None
- Steps:
  - Define synthetic clinical test cases (routine triage, conflicting contraindication, safety refusal) in `tests/council/fixtures/mock_health_queries.json`.
  - Invoke `HealthCouncilPipeline.run()` against mock council agents with fixed random seeds.
  - Assert that consensus scoring and safety validator hooks execute and produce valid structured responses.
- Acceptance Criteria:
  - Smoke test suite passes with 100% success on all synthetic scenarios.
  - Deliberation output validates against `HealthCouncilResponse` schema with zero validation errors.
  - Pipeline completes execution under 3.5s per synthetic query locally.

## Testing Strategy
- Run `pytest tests/council/test_pipeline_validation.py` using mock agent adapters to verify deterministic voting logic.
- Validate output JSON schemas against `schemas/council_output.json` using `jsonschema`.
- Execute negative safety assertions to verify queries with harmful medical guidance trigger immediate council rejection.

## Risks
- Mock agent responses may fail to capture non-deterministic JSON syntax errors common in production LLM outputs. Mitigation: Seed test fixtures with malformed and partial JSON payloads.
- Multi-agent deliberation loops could exceed execution timeouts if parallel invocation stalls. Mitigation: Enforce strict 2.0s timeouts per council member with fallback defaults.
- Self-critique: The plan relies strictly on synthetic mock inputs and assumes consensus convergence, completely failing to test pipeline behavior when agents enter an unresolvable voting deadlock or tie.
- Self-critique: The validation tests single-turn execution only, leaving open the failure mode of context window exhaustion during extended multi-turn council debates.

## Rollback Plan
- Revert test additions via `git checkout HEAD -- tests/council/` and remove newly added mock fixtures.
- Revert any pipeline configuration changes via `git checkout HEAD -- src/health_agent/council/pipeline.py`.

## Edge Cases
- Exact tie votes among council members on diagnosis triage severity.
- Partial member failure where one council agent drops connection mid-deliberation.
- Safety validator rejects a consensus output that passed all clinical member checks.
- Empty or whitespace-only clinical prompt passed into the council pipeline.

## Open Questions
- Is local mock execution sufficient for pipeline sign-off, or must validation execute against staging LLM endpoints?
- What is the definitive tie-breaking policy when the Medical Ethics agent and Clinical Triage agent disagree?