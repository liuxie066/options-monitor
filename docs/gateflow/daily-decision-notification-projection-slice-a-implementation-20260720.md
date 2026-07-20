# Gateflow Implementation Artifact — Slice A

- **Work unit**: `daily-decision-notification-projection`
- **Gate**: implementation
- **Slice**: A — Candidate/action semantic correction
- **Baseline**: `origin/main=bee60f201e1b` (`v1.3.4`)
- **Branch**: `codex/daily-decision-notification-a-plus`
- **Status**: implemented

## Scope implemented

- Preserved canonical `open_candidate` / `open_combo_yield` action records and stable action IDs.
- Added opening-candidate diff vocabulary: `candidate_added`, `candidate_invalidated`, `candidate_priority_upgraded_to_p0`, `candidate_priority_downgraded`, and `candidate_capacity_changed`.
- Kept close/blocker action vocabulary unchanged.
- Moved material capacity detection from top-level first-known capacity to the stable candidate action's structured `metrics.capacity.contracts_available`.
- Added structured option type, expiration, strike, and leg role to the audit change view.
- Added Combo Yield candidate priority.
- Added explicit position `strategy_family`, `evaluation_status`, `quote_status`, and `contract_symbol` fields for the later user projection. No renderer ID parsing is required.

## Changed files

- `domain/domain/daily_decision_brief.py`
- `src/application/daily_decision_brief_service.py`
- `tests/test_daily_decision_brief_domain.py`
- `tests/test_daily_decision_brief_repository.py`
- `tests/test_daily_decision_brief_scenarios.py`

## Validation

```text
python3.12 -m ruff check ...
All checks passed!

python3.12 -m pytest \
  tests/test_daily_decision_brief_domain.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_scenarios.py \
  tests/test_daily_decision_brief_repository.py -q
68 passed
```

## Docs decision

The accepted Revision 6 plan already documents the candidate/action compatibility strategy and the Combo Yield position-attribution invariant. No public user document is changed in Slice A; final examples belong to Slice E.

## Residual risks

- New candidate diff vocabulary is not yet rendered in user-safe Chinese. Classification: covered by approved Slice B.
- Structured position strategy/status fields are not yet consumed by the renderer. Classification: covered by approved Slice B.
- Scheduled batch/manual context is not part of Slice A. Classification: covered by approved Slices C and D.
