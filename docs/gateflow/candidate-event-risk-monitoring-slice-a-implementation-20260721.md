# Gateflow Implementation Artifact — Slice A

- **Work unit**: `candidate-event-risk-monitoring`
- **Slice**: A — authoritative event evidence contract
- **Gate**: implementation
- **Date**: 2026-07-21
- **Plan**: `docs/plans/candidate-event-risk-monitoring-plan-20260721.md` revision 2
- **Artifact path**: `docs/gateflow/candidate-event-risk-monitoring-slice-a-implementation-20260721.md`
- **Status**: implemented; pending code review

## Objective and Outcome

Added additive per-event-category coverage metadata to the existing event acquisition/cache/run-snapshot chain without changing provider selection, fetch cadence, public event-list functions, or event-source probe response shape.

## Changed Files

- `src/application/events/source_futu.py`
  - public `fetch_symbol_events_futu()` remains list-returning;
  - internal evidence function reports complete/partial coverage for earnings, ex-dividend, and split calls.
- `src/application/events/source_yfinance.py`
  - public list facade preserved;
  - internal evidence function reports earnings/ex-dividend completeness and explicitly marks forward split coverage unsupported.
- `src/application/events/store.py`
  - accepts structured evidence or legacy list fetchers;
  - persists and restores additive coverage metadata through fresh and stale cache results.
- `src/application/events/orchestrator.py`
  - first-party runtime fetchers use the internal evidence functions;
  - resolved and error snapshot items carry additive coverage.
- `tests/test_event_source_futu.py`
  - proves public list compatibility, complete coverage, and partial sub-source preservation.
- `tests/test_event_prefetch.py`
  - proves legacy list evidence remains unknown-for-absence and structured coverage survives snapshot/cache reuse.

## Contracts and Invariants

- Public direct fetch APIs still return `list[dict]`.
- Legacy injected list fetchers remain supported and produce empty/unknown coverage.
- Partial provider sub-call failures are preserved instead of becoming an apparently complete empty result.
- yfinance cannot confirm split absence.
- Provider source status, cooldown, stale-cache, and fallback selection behavior are unchanged.

## Validation

- `python -m py_compile` on all four touched production modules: passed.
- Focused event suite: `24 passed`.
- Ruff on touched event source/store/orchestrator/tests: passed.
- `git diff --check`: passed.

## Docs Decision

No public docs in this slice. User semantics and authority documentation are reserved for Slice D after lifecycle behavior is complete.

## Residual Risks and Uncovered Areas

- Malformed structured fetch payload behavior needs adversarial code review before acceptance.
  - Classification: current slice review/fix loop.
- Candidate-level confirmed/unknown semantics do not exist yet.
  - Classification: covered by approved Slice B.
- Provider-neutral identity and material diff do not exist yet.
  - Classification: covered by approved Slices B/C.
- Live provider drift is not exercised.
  - Classification: assigned to later authorized operational canary.

## Completion Signal

Slice A implementation is complete and ready for code review. No production config, state, service, or notification was mutated.
