# Ledger Writer Result Contract

Decision: the bottom writer functions no longer return raw dicts for core write results.

Implementation:

- `src/application/ledger/writer.py::persist_trade_event_object` and `persist_trade_event` return `LedgerWriteResult`.
- `src/application/ledger/writer.py::rebuild_position_lots_from_trade_events` returns `ProjectionRefreshResult`.
- Manual trade writers now return `LedgerWriteResult`; manual adjust adds patch metadata through `with_details` instead of mutating a dict.
- CLI / trade review / auto-close maintenance convert writer results to dict only at their output or report boundary.

Reason: after command/service result contracts, leaving writer results as free dicts kept the core event write path open to accidental shape drift. Writer return types now match the canonical chain: trade-event write result and projection-refresh result are explicit application contracts.

Validation:

- `basedpyright --level error src/application/ledger src/application/positions/workflows.py src/application/positions/maintenance.py src/application/trades/review.py src/interfaces/cli/option_positions.py`
- `python3 -m pytest tests/test_ledger_service.py tests/test_ledger_sqlite_workflows.py tests/test_ledger_maintenance.py tests/test_option_positions_cli.py tests/test_option_positions_legacy_retirement.py tests/test_positions_maintenance.py tests/test_trade_events_cli.py tests/test_trades_resolver_open.py tests/test_trades_resolver_close.py -q`
