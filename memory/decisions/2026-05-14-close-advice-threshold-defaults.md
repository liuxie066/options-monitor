# Close Advice remaining-annualized threshold defaults

- Date: 2026-05-14
- Scope: `close_advice`

## Decision

- System default `strong_remaining_annualized_max` is `0.045`.
- System default `medium_remaining_annualized_max` is `0.07`.
- The no-config code fallback in `domain/domain/close_advice.py` stays aligned with `configs/system.json`.

## Why

- Remaining annualized return must be lower before close advice escalates to `strong` or `medium`.
- Keeping system config and domain fallback aligned prevents different tiering when tests or tools instantiate `CloseAdviceConfig()` without layered config.
