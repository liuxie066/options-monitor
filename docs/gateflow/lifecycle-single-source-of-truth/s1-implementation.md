# Gateflow S1 Implementation — Canonical Lifecycle State Ownership

- Work unit: `lifecycle-single-source-of-truth`
- Gate: `implementation`
- Slice: `S1`
- Date: 2026-08-04
- Status: implementation and code-review fix complete; accepted after re-review
- Accepted plan: `docs/gateflow/lifecycle-single-source-of-truth/accepted-plan.md`
- Artifact path: `docs/gateflow/lifecycle-single-source-of-truth/s1-implementation.md`

## Objective and outcome

Remove lifecycle projection write authority from discovery while preserving the existing fail-closed deadline behavior under the canonical account-scoped due reconciler.

Outcome:

- discovery now freezes/creates cases only;
- compatibility result fields `refreshed_case_ids` and `would_refresh_case_ids` remain and are always empty;
- canonical due reconciliation handles deadline-expired cases without effective pairing;
- canonical evidence status distinguishes truly missing anchors from evidence whose timing is unavailable;
- missing-anchor aging uses the atomic generation-token/fingerprint/revision writer with no notification Outbox side effect;
- effective-pairing behavior and provider observation remain unchanged.

## Changed files

- `src/application/ledger/writer.py`
- `src/application/trades/close_reason_reconciliation.py`
- `tests/test_position_advice_v2_lifecycle_reconciliation.py`
- `tests/test_settlement_observation.py`
- `docs/gateflow/lifecycle-single-source-of-truth/s1-implementation.md`

## Implementation decisions

1. Deleted the existing-case refresh loop from `discover_expired_lifecycle_cases_atomically()` and removed its obsolete fallback read-model import/state inputs.
2. Added a private due-reconciliation helper for cases whose canonical read model has no effective pairing timestamp after the canonical deadline.
3. The helper uses `lifecycle_evidence_status` as authority:
   - `missing` + canonical `needs_review`: materialize the canonical read model;
   - any evidence-present state: delegate to `reconcile_lifecycle_close_reason()` for typed classification.
4. Missing-anchor materialization writes through `advance_lifecycle_case_state()` with the canonical generation token and `public_transition=None`.
5. The summary records canonical reason state/codes, close reason, pairing/deadline, timing hash, and an explicit null observation hash; the atomic writer supplies canonical allocation and fingerprint/revision fields.

## State and error behavior

- Deadline absent or not reached: skip.
- Effective pairing present: unchanged existing due flow.
- Missing evidence at elapsed canonical deadline: deterministic `needs_review`, no broker query, no Outbox.
- Evidence present but effective pairing unavailable: existing close-reason resolver, no broker query.
- Stale lifecycle generation: existing CAS error propagates fail closed.
- Repeated missing-evidence apply: identical business fingerprint, unchanged revision, no Outbox.

## Validation

The repository `.venv` Python does not currently contain pytest (`./.venv/bin/python -m pytest` reports `No module named pytest`). The same tests were run with the available system Python 3.12 and pytest 9.0.3, with bytecode redirected to `/tmp`.

Targeted new regressions:

```bash
PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_s1 python3.12 -m pytest -q \
  tests/test_position_advice_v2_lifecycle_reconciliation.py::test_discovery_is_create_only_and_due_owner_reviews_missing_evidence_at_deadline \
  tests/test_settlement_observation.py::test_discovery_replay_does_not_override_canonical_timing_policy \
  tests/test_settlement_observation.py::test_due_reconciliation_routes_anchor_without_effective_pairing_to_close_reason
```

Result: `3 passed in 1.31s`.

Focused slice suite:

```bash
PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_s1_full python3.12 -m pytest -q \
  tests/test_position_advice_v2_lifecycle_reconciliation.py \
  tests/test_settlement_observation.py
```

Initial result: `25 passed in 1.46s`.

After accepted code-review finding S1-01 was fixed:

```bash
PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_s1_rereview python3.12 -m pytest -q \
  tests/test_position_advice_v2_lifecycle_reconciliation.py \
  tests/test_settlement_observation.py
```

Result: `26 passed in 1.57s`.

Static diff check:

```bash
git diff --check
```

Result: pass.

## Docs decision

The operator-facing lifecycle ownership documentation is intentionally deferred to approved Slice S2, where discovery account scoping is completed at the same boundary.

## Residual risks and uncovered areas

- Account-scoped backfill is covered by later approved Slice S2.
- Existing production rows and deployment convergence remain assigned to a separately authorized operations step.
- The full lifecycle/backfill/tick matrix and compileall are covered by the later aggregate validation gate.
- Repository-local `.venv` dependency drift is an environment issue; tests are currently reproducible with system Python 3.12. No dependency files are changed in this work unit.

All residual risks are classified.

## Completion signal

S1 implementation and accepted review fix are complete; focused tests pass and the next entry point is the accepted S1 commit.
