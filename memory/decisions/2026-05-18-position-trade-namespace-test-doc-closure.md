# Position/Trade Namespace Test and Doc Closure

Date: 2026-05-18

Decision:

- Keep `src/application/ledger/` as the canonical storage/projection/preflight/write boundary.
- Keep `src/application/positions/` as the position-facing workflow namespace.
- Keep `src/application/trades/` as the trade-facing workflow namespace.
- Rename focused regression tests to `test_positions_*` and `test_trades_*` so the test surface mirrors the application boundary.
- Keep old root module names only inside retirement/ownership tests and historical deletion notes, where they act as regression guards.

Validation:

- `python3 -m pytest tests/test_option_positions_legacy_retirement.py tests/test_config_import_ownership_application.py tests/test_positions_*.py tests/test_trades_*.py tests/test_trade_workflows.py tests/test_ledger_*.py`
- `python3 -m pytest`
- `python3 -m compileall domain/domain/ledger src/application/ledger src/application/positions src/application/trades src/interfaces/cli scripts/auto_close_expired_positions.py`
- `basedpyright --level error` on the core ledger, positions, trades, CLI, and intake surfaces.
- `git diff --check`
