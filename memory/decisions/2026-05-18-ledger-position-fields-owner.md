# Ledger position fields owner

Date: 2026-05-18

Decision:

- `domain.domain.ledger.position_fields` owns canonical lot record fields, open commands, effective field readers, and open/close/adjust patch helpers.
- `domain.domain.option_position_lots` remains only as a compatibility re-export.
- Core `src.application.ledger`, `src.application.positions`, and `src.application.trades` code must import the ledger-domain owner, not the legacy module.

Why:

- The v2/service/facade layers were retired, but the core write model still depended on a legacy option-position module name.
- Keeping lot field construction under `domain.domain.ledger` makes the root position/trade model explicit and reduces the chance of a parallel legacy write model returning.

Validation:

- `python3 -m pytest tests/test_option_positions_domain.py tests/test_option_positions_legacy_retirement.py`
- `basedpyright --level error domain/domain/ledger/position_fields.py domain/domain/option_position_lots.py domain/domain/ledger/__init__.py src/application/ledger src/application/positions src/application/trades src/interfaces/cli/option_positions.py tests/test_option_positions_legacy_retirement.py tests/test_option_positions_domain.py`
