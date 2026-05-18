# Position lot field and patch contract

Date: 2026-05-18

Decision:

- `domain.domain.ledger.position_fields.PositionLotFields` is the typed open-lot field contract.
- `domain.domain.ledger.position_fields.PositionLotPatch` is the typed patch contract for open adjustment, manual close, and expire auto-close field changes.
- Existing `build_open_fields`, `build_open_adjustment_patch`, `build_close_patch`, `build_buy_to_close_patch`, and `build_expire_auto_close_patch` dict helpers remain only as compatibility output adapters.

Why:

- Free-form patch dicts made it too easy for open, close, adjust, and auto-close paths to drift in field names and semantics.
- The ledger core needs a stable field contract before write paths can be migrated to typed inputs without reintroducing parallel v1/v2 style mappings.

Validation:

- `python3 -m pytest tests/test_option_positions_domain.py`
- `basedpyright --level error domain/domain/ledger/position_fields.py tests/test_option_positions_domain.py tests/test_sell_put_yield_enhancement_required_data_planning.py`
- `python3 -m pytest tests/test_option_positions_domain.py tests/test_option_positions_legacy_retirement.py tests/test_ledger_*.py tests/test_positions_*.py tests/test_trades_*.py tests/test_trade_workflows.py`
- `python3 -m pytest`
- `git diff --check`
