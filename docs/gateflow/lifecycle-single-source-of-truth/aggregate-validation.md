# Gateflow Aggregate Validation

- Gate: `aggregate validation`
- Work unit: `lifecycle-single-source-of-truth`
- Base: `origin/main@ed2531e9`
- Accepted S1 commit: `07934baa`
- Accepted S2 commit: `35b5c3ba`
- Date: 2026-08-04
- Status: pass
- Artifact path: `docs/gateflow/lifecycle-single-source-of-truth/aggregate-validation.md`

## Deterministic acceptance evidence

- Discovery is create-only and does not refresh existing lifecycle status or derived summary.
- Canonical account-scoped due reconciliation owns deadline aging, including missing-evidence/no-pairing fail-closed materialization.
- Missing-anchor apply/replay retains fingerprint/revision idempotency and creates no notification Outbox rows.
- Evidence-present/no-effective-pairing cases use the existing typed close-reason reconciler without a broker observation call.
- Canonical timing policy is not overwritten by the historical fixed-72-hour discovery path.
- Backfill validates complete current Futu-ID mappings and invokes lifecycle discovery once per sorted explicit account in both phases.
- Incomplete mapping performs zero partial discovery calls, preserves payload unresolved/audit handling, and exposes a top-level lifecycle failure.
- Existing `lifecycle_discovery_result.v2`, audit envelope, union fields, history query, checkpoint, Inbox, CLI, unified tick, and notification-format contracts remain covered.

## Aggregate test gate

The repository `.venv` Python does not contain pytest (`No module named pytest`). The accepted matrix used system Python 3.12 with pytest 9.0.3 and redirected bytecode to `/tmp`.

```bash
PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_aggregate_pytest python3.12 -m pytest -q \
  tests/test_position_advice_v2_lifecycle_reconciliation.py \
  tests/test_settlement_observation.py \
  tests/test_trades_auto_intake_backfill.py \
  tests/test_trades_auto_intake_cli.py \
  tests/test_unified_tick_entrypoint.py \
  tests/test_multi_tick_notify_format.py
```

Result: `86 passed, 4 warnings in 5.68s`.

The four warnings are existing `Legacy Tick full-message renderer is deprecated` warnings from `tests/test_multi_tick_notify_format.py`; scheduled delivery continues to use Daily Brief.

## Static gates

```bash
PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_aggregate_compile python3.12 \
  -m compileall -q domain src scripts
```

Result: pass with no output.

```bash
python3.12 -m ruff check \
  src/application/ledger/writer.py \
  src/application/trades/close_reason_reconciliation.py \
  src/application/trades/backfill.py \
  tests/test_position_advice_v2_lifecycle_reconciliation.py \
  tests/test_settlement_observation.py \
  tests/test_trades_auto_intake_backfill.py
```

Result: `All checks passed!`.

The first full-branch `git diff --check origin/main...HEAD` found only seven extra blank lines at EOF in earlier Gateflow Markdown evidence. They were removed mechanically without changing content. Final `git diff --check origin/main` and working-tree `git diff --check` both pass.

Repository search confirms `src/application/trades/backfill.py` has one `discover_lifecycle_cases()` call supplied with the explicit loop `account`, and no `account=None` discovery call.

## Safety evidence

- Tests use temporary repositories/stores and fake or absent providers.
- No broker request, real notification, production config mutation, ledger/business-data write, service change, release, deployment, or remote upgrade was performed.
- Aggregate validation did not modify production runtime artifacts.

## Residual risks

- Existing production rows and runtime convergence require a separately authorized deployment/operations step.
- A later account discovery failure can retain earlier idempotent create-only inserts; retry is safe and cross-account rollback is intentionally absent.
- Lifecycle-only failure does not rewind a successful history checkpoint because lifecycle discovery reruns independently on every backfill cycle.
- Repository-local `.venv` dependency drift remains an environment issue; no dependency files are changed.

All residual risks are classified. The next gate is Aggregate DeepReview of the complete `origin/main...HEAD` work-unit diff plus this validation evidence.
