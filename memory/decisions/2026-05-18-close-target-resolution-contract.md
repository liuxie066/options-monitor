# Close target resolution contract

Date: 2026-05-18

Decision:

- `src.application.ledger.lot_resolver.CloseTargetResolution` is the canonical close target contract.
- Manual close resolves to one unique strict lot or fails closed.
- Broker close resolves a strict exact FIFO target set and splits writes per matched lot.
- Auto-close resolves the explicit current `record_id` target and verifies the supplied fields still match the current lot identity.
- The resolution payload is carried through preview, diagnostics, operations, and stored event raw payload.

Why:

- The original defect came from manual close, broker close, and auto-close resolving logical positions through different record-id paths.
- `position_key` and aggregated views are useful read identities but unsafe write targets in same strike, same expiry, multi-lot, and cross-expiry cases.

Validation:

- `python3 -m pytest tests/test_trades_resolver_close.py tests/test_positions_workflows_auto_sync.py tests/test_ledger_maintenance.py`
- `basedpyright --level error src/application/ledger/lot_resolver.py src/application/ledger/commands.py src/application/ledger/api.py src/application/ledger/maintenance.py src/application/ledger/service.py src/application/ledger/preflight.py src/application/ledger/writer.py src/application/positions/workflows.py src/application/trades/resolver.py src/application/trades/workflows.py`
- `python3 -m pytest`
