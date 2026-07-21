# Candidate Event Risk Monitoring — Slice B Implementation

- **Work unit**: `candidate-event-risk-monitoring`
- **Slice**: B — Candidate-bound Daily Brief projection
- **Gate**: implementation
- **Date**: 2026-07-21
- **Base commit**: `2de0c659`
- **Artifact path**: `docs/gateflow/candidate-event-risk-monitoring-slice-b-implementation-20260721.md`
- **Status**: implementation complete; ready for code review

## Scope

Implemented the approved Slice B projection only. The current run-level `state/event_snapshot.json` is the sole Daily Brief event authority; candidate CSV compatibility event fields are not read.

## Changed files

- `domain/domain/daily_decision_event_risk.py`
- `domain/domain/daily_decision_brief.py`
- `src/application/daily_decision_brief_service.py`
- `tests/test_daily_decision_event_risk.py`
- `tests/test_daily_decision_brief_service.py`

## Decisions

- Missing, malformed, stale, error, partial, unsupported, conflicting, and empty fallback evidence normalize to user state `unknown`.
- Complete primary evidence may confirm no upcoming important event.
- Complete fallback evidence may confirm a concrete event but cannot confirm absence.
- Candidate and opening action receive the same additive `event_risk`; stable action-ID fields are unchanged.
- Combo Yield uses both Put and Call expirations.
- Top-level `events` is generated only from candidate-bound event projections and includes the concrete candidate action identity.
- Event snapshot gaps degrade the brief but do not block candidate assembly.
- Old briefs without `event_risk` remain valid and normalize opening actions/candidate views to unknown/not-observed.

## Validation

```text
./.venv/bin/python -m pytest tests/test_daily_decision_brief_service.py tests/test_daily_decision_event_risk.py tests/test_daily_decision_brief_domain.py -q
55 passed

./.venv/bin/python -m ruff check domain/domain/daily_decision_event_risk.py domain/domain/daily_decision_brief.py src/application/daily_decision_brief_service.py tests/test_daily_decision_event_risk.py tests/test_daily_decision_brief_service.py
All checks passed

git diff --check
passed
```

## Uncovered areas

- Material diff transitions and Chinese rendering are intentionally deferred to approved Slice C.
- Full-suite and release checks are deferred to Slice D/final validation.

## Residual risks

- Event occurrence matching across Daily Brief revisions is not yet consumed by the diff engine; **covered by later approved Slice C**.
- User-visible event wording is not yet rendered; **covered by later approved Slice C**.
- Release documentation/version metadata is unchanged; **covered by later approved Slice D**.

## Completion status

Slice B implementation is complete and ready for the required Deepreview code-review gate.
