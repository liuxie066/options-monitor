# Aggregate Deepreview Fix — Daily Decision Brief

- **Gate**: aggregate deepreview fix
- **Work unit**: `daily-decision-brief`
- **Date**: 2026-07-19
- **Findings fixed**: CR-AGG-1, CR-AGG-2
- **Status**: implemented; pending re-review
- **Artifact path**: `docs/gateflow/daily-decision-brief-aggregate-fix-20260719.md`

## Changes

### CR-AGG-1 — interrupted revision publication recovery

- Added one private revision-list helper and reused it for allocation and read listing.
- Under the existing account+market lock, allocation now uses `max(existing same-day immutable revisions) + 1`.
- Orphan immutable revision files are preserved; no state deletion, rewrite, migration, journal, queue or new store was introduced.
- Added injected-interruption regressions for both the first publication (no current pointer yet) and a later same-day publication.

### CR-AGG-2 — stable high-priority action activation

- Added a material `action_added` transition when an existing action enters the active P0/P1 set from blocked, observe, invalidated, or P2.
- Suppressed duplicate `action_added` when `priority_upgraded_to_p0` already communicates the transition.
- Kept already-active same-tier P0/P1 rank/price noise non-material.
- Added domain regressions plus an end-to-end repository lifecycle scenario using a stable action ID and confirmed prior revision.

## Changed files

- `domain/domain/daily_decision_brief.py`
- `src/application/daily_decision_brief_repository.py`
- `tests/test_daily_decision_brief_domain.py`
- `tests/test_daily_decision_brief_repository.py`
- `tests/test_daily_decision_brief_scenarios.py`
- `docs/DEPENDENCY_GRAPH.md`

## Validation

```text
Direct focused regressions
46 passed

Daily Brief + notification + CLI/Agent/config/scheduler aggregate
260 passed

Full repository suite
2800 passed, 10 skipped

python3 scripts/generate_dependency_graph.py --check
475 production modules, 0 cycles

python3 scripts/guardrails_check.py --check-runtime-config-tracking
OK

python3 scripts/release_check.py
release metadata valid for 1.2.420

ruff / compileall / git diff --check
passed
```

## Residual risks

- Provider idempotency semantics still require later production canary observation; no real send was performed.
- Multi-file publication remains per-file atomic, but the proven permanent allocation wedge now recovers deterministically without deleting history.
- Exchange early-close calendar support remains a later scheduler work unit.
- No unclassified residual risk.

## Next entry point

Aggregate deepreview re-review against base `5aecee73`.
