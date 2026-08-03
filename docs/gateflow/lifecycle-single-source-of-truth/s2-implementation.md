# Gateflow S2 Implementation — Account-scoped History Backfill

- Work unit: `lifecycle-single-source-of-truth`
- Gate: `implementation`
- Slice: `S2`
- Date: 2026-08-04
- Status: implementation and code-review fixes complete; accepted after re-review
- Accepted plan: `docs/gateflow/lifecycle-single-source-of-truth/accepted-plan.md`
- Prerequisite commit: `07934baa`
- Artifact path: `docs/gateflow/lifecycle-single-source-of-truth/s2-implementation.md`

## Objective and outcome

Bind history-backfill lifecycle discovery to the explicit accounts represented by the current Futu source instead of scanning all ledger accounts.

Outcome:

- the current `futu_account_ids` and canonical `account_mapping` are the only account-scope inputs;
- every configured Futu account ID must have a non-empty mapping before either discovery phase runs;
- single-account and legacy multi-account sources invoke discovery once per sorted lowercase account;
- no backfill lifecycle-discovery call passes `account=None`;
- incomplete scope fails both lifecycle phases without partially scanning a mapped account;
- per-account results and union summaries remain observable in diagnostics and the durable audit event;
- the existing `lifecycle_discovery_result.v2` schema marker remains unchanged while account evidence is additive;
- the durable audit envelope preserves its existing `ok` / `result` / `error` shape.

## Changed files

- `src/application/trades/backfill.py`
- `tests/test_trades_auto_intake_backfill.py`
- `docs/FUTU_TRADE_HOLDINGS_SYNC.md`
- `docs/gateflow/lifecycle-single-source-of-truth/s2-implementation.md`

## Implementation decisions

1. Added a private scope resolver that normalizes the current Futu IDs, validates complete canonical mappings, lowercases and deduplicates account labels, and returns stable sorted accounts.
2. The before/after lifecycle phases receive only that validated tuple and call `discover_lifecycle_cases()` once per account.
3. Phase results retain `lifecycle_discovery_result.v2` and add `accounts`, ordered `account_results`, and deduplicated union fields for create/discover/skip plus the empty refresh compatibility fields.
4. Missing mapping produces `lifecycle_account_scope_incomplete`, zero account calls, and the same typed phase evidence before and after payload processing.
5. A per-account runtime exception is captured in that account result and makes the aggregate phase fail while retaining successful account evidence for safe idempotent retry.
6. Existing history query, payload identity, Inbox, checkpoint, and top-level history/durable-queue completion semantics are unchanged.

## Validation

The repository `.venv` Python does not currently contain pytest. Validation used system Python 3.12 with pytest 9.0.3 and redirected bytecode to `/tmp`.

Targeted account-scope regressions:

```bash
PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_s2_target python3.12 -m pytest -q \
  tests/test_trades_auto_intake_backfill.py::test_backfill_lifecycle_discovery_is_scoped_to_single_mapped_account \
  tests/test_trades_auto_intake_backfill.py::test_backfill_lifecycle_discovery_scopes_legacy_source_per_account \
  tests/test_trades_auto_intake_backfill.py::test_backfill_lifecycle_discovery_rejects_incomplete_account_scope_without_partial_scan
```

Result: `3 passed in 1.17s`.

Focused slice suite after preserving the audit envelope:

```bash
PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_s2_compat python3.12 -m pytest -q \
  tests/test_trades_auto_intake_backfill.py
```

Result: `14 passed in 1.21s`.

After accepted review findings S2-01 and S2-02 were fixed:

```bash
PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_s2_rereview3 python3.12 -m pytest -q \
  tests/test_trades_auto_intake_backfill.py
```

Result: `14 passed in 1.12s`.

Ruff targeted check: `All checks passed!`.

Static checks:

```bash
git diff --check
rg -n "discover_lifecycle_cases\\(" src/application/trades/backfill.py
```

Result: pass; the sole call site supplies the loop's explicit `account` value.

## Docs decision

`docs/FUTU_TRADE_HOLDINGS_SYNC.md` now records the ownership boundary:

- discovery is create-only;
- canonical lifecycle read model plus account-scoped due reconciliation own derived-state transitions;
- backfill derives complete explicit account scope and never uses an unscoped discovery call.

## Residual risks and uncovered areas

- If a later per-account discovery raises after an earlier account has created cases, the aggregate reports failure but the earlier idempotent creates remain. Retrying is safe because discovery is create-only; cross-account rollback is intentionally not introduced.
- Existing production rows and deployment convergence remain assigned to a separately authorized operations step.
- The full lifecycle/backfill/tick matrix, compileall, and aggregate DeepReview remain required after the S2 review gate.
- Repository-local `.venv` dependency drift remains an environment issue; no dependency files are changed.

All residual risks are classified.

## Completion signal

S2 implementation and accepted review fixes are complete: focused tests pass, incomplete mappings produce zero discovery calls and an observable top-level failure, the v2 diagnostic contract is preserved, and backfill contains no lifecycle discovery call with `account=None`. The next entry point is the accepted S2 commit.
