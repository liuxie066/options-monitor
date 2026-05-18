# Ledger Semantic Core Actions

Date: 2026-05-18

Decision:

- Core position/trade workflows call semantic ledger API actions instead of composing internal primitives.
- Manual position workflows use `preview_manual_position_*`, `record_manual_position_*`, and `resolve_manual_position_close_lot`.
- Broker trade workflows use `record_broker_trade_open`, `preview_broker_trade_close`, `record_broker_trade_close`, and `resolve_broker_trade_close_lots`.
- Expiry maintenance uses `open_position_ledger`, `plan_expired_position_closes`, `record_expired_position_closes`, `list_expiry_close_position_lots`, and `refresh_position_lot_projection`.
- Structural tests reject lower-level `persist_*`, `preflight_*`, `LotCloseSelector`, and legacy expire-close primitive imports in these core workflows.

Why:

- The previous public API boundary still exposed implementation-shaped names.
- The next layer of simplification requires callers to express business actions, not ledger internals.

Validation:

- `python3 -m pytest tests/test_option_positions_legacy_retirement.py tests/test_config_import_ownership_application.py tests/test_positions_*.py tests/test_trades_*.py tests/test_trade_workflows.py tests/test_ledger_*.py`
- `basedpyright --level error` on the ledger API, core position/trade workflows, and structural boundary tests.
