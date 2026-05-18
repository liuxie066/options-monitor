# Ledger Result Contract Boundary

Decision: manual open/close/adjust no longer pass raw preflight/write/preview dicts across the central ledger command boundary.

Implementation:

- `src/application/ledger/results.py` owns `LedgerPreflightResult`, `LedgerWriteResult`, and manual preview/write result contracts.
- `src/application/ledger/preflight.py` returns `LedgerPreflightResult` for manual open/close/adjust and related preflight paths.
- `src/application/ledger/service.py` imports result contracts instead of defining local result dataclasses.
- `src/application/ledger/commands.py` returns typed manual preview/write results internally.
- `src/application/positions/workflows.py` converts typed results to dict only at the workflow output boundary with `to_payload()`.

Reason: keeping raw dicts in the center of the write path makes the core model look typed while still allowing accidental payload shape drift. The canonical chain remains `TradeEvent -> projection -> PositionLotRecord -> read model`; dicts are allowed only at CLI/JSON, SQLite serialization, migration/reconciliation adapters, and external API payloads.

Validation:

- `python3 -m pytest tests/test_ledger_service.py tests/test_ledger_sqlite_workflows.py tests/test_option_positions_cli.py tests/test_option_positions_legacy_retirement.py -q`
- `basedpyright --level error src/application/ledger src/application/positions/workflows.py`
- `python3 -m pytest tests/test_option_positions_domain.py tests/test_option_positions_legacy_retirement.py tests/test_ledger_*.py tests/test_positions_*.py tests/test_trades_*.py tests/test_trade_workflows.py tests/test_trade_event_ledger_long_lifecycle.py tests/test_trade_events_cli.py tests/test_option_positions_cli.py -q`
- `python3 -m pytest -q`
- `git diff --check`
