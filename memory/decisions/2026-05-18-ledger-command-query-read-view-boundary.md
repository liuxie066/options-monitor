# Ledger Command/Query And Read View Boundary

Date: 2026-05-18

Decision:

- `src/application/ledger/api.py` remains the only public runtime import surface for non-ledger code, but it is now a thin facade.
- Ledger write and maintenance use cases live in `src/application/ledger/commands.py`.
- Ledger read/query use cases live in `src/application/ledger/queries.py`.
- Typed read DTOs live in `src/application/ledger/views.py`; the first committed DTOs are `PositionLotSnapshot` and `RiskPositionView`.
- `src/application/positions/context_builder.py` consumes `RiskPositionView` internally and converts back to dict only at JSON/report boundaries.

Why:

- The previous `ledger.api` boundary stopped external bypasses but was becoming a new god module.
- Risk code should not keep unpacking ad hoc `{record_id, fields}` dicts because that recreates the old record-id mapping ambiguity.
- Command/query separation keeps the core model direction explicit: `TradeEvent -> PositionLot -> RiskPositionView`.

Validation:

- `tests/test_option_positions_legacy_retirement.py` asserts that `ledger.api` has no executable definitions and that the risk context uses typed ledger views.
- `tests/test_positions_context_builder_partial_close.py` covers the typed risk view conversion and symbol canonicalization.
- `python3 -m pytest` passed: 1316 tests.
