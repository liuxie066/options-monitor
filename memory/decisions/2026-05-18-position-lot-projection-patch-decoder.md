# Position lot projection patch decoder

Date: 2026-05-18

Decision:

- Stored adjust-event `raw_payload.patch` is decoded by `domain.domain.ledger.position_fields.decode_position_lot_patch`.
- `PositionLot.apply_adjust` consumes the decoded `PositionLotPatch` contract through accessors instead of reading free-form patch dicts.
- Unsupported patch fields are projection errors, not silently ignored state mutations.

Why:

- The write path now creates `PositionLotPatch`, but replay still accepted arbitrary patch dicts at the projection boundary.
- Projection is the safety-critical replay path for current position state; it must fail closed on unknown adjust fields.

Validation:

- `python3 -m pytest tests/test_ledger_projection.py tests/test_option_positions_domain.py tests/test_ledger_sqlite_workflows.py -q`
- `basedpyright --level error domain/domain/ledger/lots.py domain/domain/ledger/position_fields.py tests/test_ledger_projection.py tests/test_option_positions_domain.py`
- `python3 -m pytest tests/test_option_positions_legacy_retirement.py tests/test_ledger_projection.py tests/test_ledger_sqlite_workflows.py tests/test_ledger_maintenance.py tests/test_ledger_service.py tests/test_trade_event_ledger_long_lifecycle.py`
- `basedpyright --level error domain/domain/ledger tests/test_option_positions_legacy_retirement.py tests/test_ledger_projection.py`
- `python3 -m pytest tests/test_option_positions_domain.py tests/test_option_positions_legacy_retirement.py tests/test_ledger_*.py tests/test_positions_*.py tests/test_trades_*.py tests/test_trade_workflows.py tests/test_trade_event_ledger_long_lifecycle.py tests/test_trade_events_cli.py tests/test_option_positions_cli.py`
