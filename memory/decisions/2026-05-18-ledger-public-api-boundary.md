# Ledger Public API Boundary

Date: 2026-05-18

Decision:

- `src/application/ledger/api.py` is the public application boundary for position-facing and trade-facing workflows.
- `src/application/positions/` and `src/application/trades/` must not directly import ledger internals such as `service`, `preflight`, `lot_resolver`, `publisher`, `repository`, or `read_model`.
- `ledger.api` must not import `positions`, `trades`, or `ledger.read_model` at module load time. Read-model access remains lazy to avoid circular dependencies.

Why:

- Previous refactors moved files but still allowed too many legal paths into the core ledger.
- A single public boundary is required before the core model can be simplified safely.

Validation:

- Structural tests in `tests/test_option_positions_legacy_retirement.py` enforce the API boundary.
- Full regression: `python3 -m pytest`.
- Focused static check: `basedpyright --level error` on ledger API, positions/trades workflows, and boundary tests.
