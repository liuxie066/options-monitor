# Application Position/Trade Namespace Consolidation

## Context

Position and trade application workflows had grown as many root-level
`src.application.*` modules. After the ledger refactor, that root namespace made
ownership unclear and allowed trade-facing helpers to drift back into
position-facing code.

## Decision

Keep `src.application.ledger` as the canonical storage/projection/preflight
application boundary, and consolidate outward-facing application workflows into:

- `src.application.positions`: manual lot workflows, auto-close maintenance,
  maintenance receipts, Feishu mirror sync, sync config, risk context,
  inspection, and reporting.
- `src.application.trades`: OpenD trade intake, normalization, account mapping,
  idempotency state, receipts, resolver, review/replay/repair, and trade
  workflows.

Runtime code should not import the retired root-level position/trade modules.
Structure tests in `tests/test_option_positions_legacy_retirement.py` and
ownership tests in `tests/test_config_import_ownership_application.py` enforce
that boundary.

## Verification

- `python3 -m pytest`: 1307 passed.
- Focused `basedpyright --level error` on ledger-adjacent trade/position write
  surfaces: 0 errors.
- `python3 -m compileall` over ledger, positions, trades, and CLI wrappers:
  passed.
- `git diff --check`: passed.
