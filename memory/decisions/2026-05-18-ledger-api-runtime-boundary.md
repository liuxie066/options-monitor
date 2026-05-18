# Ledger API Runtime Boundary

Date: 2026-05-18

Decision:

- `src/application/ledger/api.py` is now the public ledger boundary for all non-ledger runtime code, not only `positions` and `trades`.
- Agent tools, CLI modules, web UI modules, pipeline context, cash-headroom queries, position workflows, and trade workflows must import ledger functionality through semantic names in `ledger.api`.
- Runtime code outside `src/application/ledger/` must not import ledger internals such as `service`, `preflight`, `repository`, `publisher`, `reconciliation`, `lot_resolver`, or `read_model`.
- `ledger.api.__all__` should export business actions and read surfaces, not implementation-shaped `persist_*`, `preflight_*`, `require_*`, `load_*`, or legacy compatibility names.

Why:

- Earlier refactors changed namespaces but still left multiple legal entry points into the ledger internals.
- The core model can only stay clean if external callers cannot bypass the canonical `TradeEvent -> PositionLot -> RiskPositionView` boundary.

Validation:

- `tests/test_option_positions_legacy_retirement.py` enforces the non-ledger runtime import boundary and semantic public export surface.
- Focused regression covered option-position CLI, trade-events CLI, option intake, agent tools, pipeline context, cash headroom, Feishu sync, maintenance, and trade intake.
