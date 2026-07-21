# Gateflow Slice 4 Implementation — Shared Receipt shell

- Gate: `implementation`
- Work unit: `channel-notification-renderer-consolidation`
- Slice: `4`
- Baseline: `528e10d2 gateflow: accept channel-notification-renderer-consolidation slice-3`
- Status: `implementation complete; pending code review`
- Artifact path: `docs/gateflow/channel-notification-renderer-consolidation/slice-4-implementation.md`

## Scope and outcome

Implemented the approved presentation-only Receipt consolidation:

- Added `render_receipt()` beside `render_system_notice()` in `src/application/notification_shells.py`.
- Trade intake receipts now use `# OM · 回执 · <account>` with `类型｜成交`.
- Position-maintenance receipts now use the same H1 with `类型｜持仓维护`.
- Both callers continue to select their own status text, field order, section rows, truncation counts, and business warnings.
- Multiline values and section rows are flattened by the shared shell, and empty sections are omitted.

## Preserved ownership

Trade intake still owns:

- applied/unresolved/failed/projection-verification status semantics;
- assigned-stock confirmation-before-write wording and candidate order;
- Combo Yield relation-pending warning;
- contract, premium, projection, ledger, diagnostic and deal-ID facts;
- receipt decision, route selection, delivery normalization and retry/dedupe behavior.

Position maintenance still owns:

- dry-run/applied/partial/failed/noop status semantics;
- broker/grace/result/time fields;
- completed/error sections and 6/5-row truncation counts;
- receipt identity, dedupe, persisted state, attempt count and provider error propagation.

The shared shell does not know deal, lot, ledger, projection, auto-close, dry-run, dedupe, provider, byte budget, send or persistence semantics.

## Changed files

- `src/application/notification_shells.py`
- `src/application/trades/receipt.py`
- `src/application/positions/maintenance_receipt.py`
- `tests/test_notification_shells.py`
- `tests/test_trades_receipt.py`
- `tests/test_positions_maintenance_receipt.py`

## Validation

```text
PYTHONPYCACHEPREFIX=/tmp/om-pycache python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_notification_shells.py \
  tests/test_trades_receipt.py \
  tests/test_positions_maintenance_receipt.py

34 passed
```

Coverage includes:

- exact shared Receipt shell layout, flattening and empty-section omission;
- trade applied, unresolved, assigned-stock confirmation, projection verification failure and Combo Yield relation pending;
- maintenance applied, preview, partial failure, full failure and no-op presentation;
- existing receipt route, confirmation, dedupe, persistence, retry-attempt and Feishu size-error tests.

Static validation:

- Ruff changed files: pass
- compileall changed source: pass
- `git diff --check 528e10d2`: pass

## Docs decision

`docs/AGENT_WIKI.md` now records `notification_shells.py` as the shared System Notice / Receipt presentation owner, the fixed family headers/types, and the caller-owned send/state/business boundaries. No command, config, storage, or provider contract changed.

## Residual risks

- Real Feishu/WeChat client rendering remains outside current authorization and is classified as `covered by aggregate validation and later authorized canary/deployment evidence`.
- End-to-end P0 transport identity across all five families is classified as `covered by approved aggregate validation`.
- Legacy Tick deletion and strict config cleanup remain classified as `assigned to explicitly hard-paused Slice 6`.
