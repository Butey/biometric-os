# Plan

## Overview
Validate the Health Agent System Council pipeline via an automated, deterministic smoke test suite. The suite tests multi-agent deliberation, consensus aggregation, safety guardrails, and timeout fallback mechanisms using mock agent adapters.

## Scope
- In:
  - Execution of Council deliberation flow using mock agent adapters.
  - Validation of consensus aggregation, tie-breaking policy, and safety guardrail enforcement.
  - Verification of error handling and fallback defaults under mock agent timeout or failure conditions.
  - Schema validation of structured clinical output (`HealthCouncilResponse`) and audit logs.
- Out:
  - Direct integration with production EHR systems or live clinical databases.
  - Live model API calls, prompt engineering, or clinical LLM fine-tuning.
  - Distributed multi-node load and stress testing.

## Phases
### Phase 1: Pipeline Smoke Test & Validation
**Goal**: Verify deterministic execution, schema conformance, safety policy enforcement, and resilient failure handling for the Council pipeline.

#### Task 1.1: Council Deliberation and Safety Smoke Suite
- Location: `tests/council/test_pipeline_validation.py`, `src/health_agent/council/pipeline.py`, `tests/council/fixtures/mock_health_queries.json`
- Description: Implement and run automated integration tests verifying council invocation, voting, timeout fallbacks, and output compliance.
- Estimated Tokens: 1100
- Dependencies: None
- Steps:
  - Populate `tests/council/fixtures/mock_health_queries.json` with deterministic scenarios: routine triage, contraindication conflict, safety policy refusal, agent timeout, and 50/50 consensus tie.
  - Configure `HealthCouncilPipeline.run()` with mock adapters and fixed seeds in `tests/council/test_pipeline_validation.py`.
  - Assert that safety guardrails trigger rejection on harmful input, ties resolve via established default triage precedence, and dropped/timed-out agents fall back gracefully.
  - Validate that responses match `HealthCouncilResponse` schema and generate a valid deliberation audit log.
- Acceptance Criteria:
  - 100% pass rate across all defined synthetic scenarios in `pytest tests/council/test_pipeline_validation.py`.
  - Zero schema validation errors against `HealthCouncilResponse` JSON schema.
  - Mock agent timeout fallback triggers cleanly within 2.0s without crashing deliberation.
  - Total local execution time remains under 3.5s per test scenario.

## Testing Strategy
- Run `pytest tests/council/test_pipeline_validation.py` using mock agent adapters for deterministic execution.
- Validate council JSON outputs against `schemas/council_output.json` with `jsonschema`.
- Execute negative safety assertions to verify immediate rejection on harmful prompts.
- Execute failure-injection tests simulating member timeouts and verify fallback to default clinical safety thresholds.

## Risks
- Mock agent fixtures may miss malformed JSON outputs produced by real LLMs. Mitigation: Include malformed and partial JSON payloads in synthetic test fixtures.
- Parallel deliberation calls could hang indefinitely on slow dependencies. Mitigation: Enforce strict 2.0s per-member timeouts with safe defaults.
- Consensus deadlocks could stall triage pipelines. Mitigation: Implement and assert an explicit clinical safety-first tie-breaking rule.

## Rollback Plan
- Revert test files and fixtures via `git checkout HEAD -- tests/council/`.
- Revert pipeline configuration changes via `git checkout HEAD -- src/health_agent/council/pipeline.py`.

## Edge Cases
- Split consensus (tie vote) between clinical triage severity levels.
- Partial member failure or dropped connection during deliberation.
- Safety validator refusal overriding a unanimous clinical recommendation.
- Blank, malformed, or whitespace-only clinical prompt inputs.

## Open Questions
- Is local mock execution sufficient for pipeline sign-off prior to staging deployment?
- Should clinical safety defaults automatically escalate triage severity when a council member drops out?