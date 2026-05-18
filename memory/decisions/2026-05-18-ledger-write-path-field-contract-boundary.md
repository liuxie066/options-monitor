# Ledger write path field contract boundary

Date: 2026-05-18

Decision:

- `src.application.ledger` write paths must use `PositionLotFields` / `PositionLotPatch` contract builders internally.
- Dict conversion is allowed at storage, raw payload, CLI/API payload, and compatibility adapter boundaries.
- Structural tests reject importing the legacy dict helpers in ledger write-path modules.

Why:

- Keeping free-form dict builders in commands, preflight, manual trades, maintenance, and service would let close/open/adjust field semantics drift after the domain contract was introduced.
- The ledger application layer is the last boundary before persistent events and projected position lots, so it should carry typed contracts until a serialization boundary is reached.

Validation:

- `python3 -m pytest tests/test_option_positions_legacy_retirement.py tests/test_option_positions_domain.py tests/test_ledger_*.py`
- `basedpyright --level error src/application/ledger tests/test_option_positions_legacy_retirement.py tests/test_option_positions_domain.py`
- `python3 -m pytest tests/test_positions_*.py tests/test_trades_*.py tests/test_trade_workflows.py tests/test_trade_event_ledger_long_lifecycle.py tests/test_trade_events_cli.py tests/test_option_positions_cli.py`
