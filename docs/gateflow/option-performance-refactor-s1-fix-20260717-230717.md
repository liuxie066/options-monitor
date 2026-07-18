# Gateflow Fix — Option Performance Refactor S1

- **Gate**: code review fix
- **Work unit**: `option-performance-refactor`
- **Slice**: S1
- **Created at**: 2026-07-17 23:07:17 CST（本机时钟）
- **Review source**: `docs/reviews/code-review-20260717-230608.md`
- **Artifact path**: `docs/gateflow/option-performance-refactor-s1-fix-20260717-230717.md`
- **Completion status**: fix-complete；ready-for-re-review

## Finding Decisions and Fixes

### CR-S1-01 — Direct PeriodRequest bypass

- **Decision**: accepted。
- **Fix**: added shared `PeriodRequest.validate()` and invoked it from both `from_mapping()` and `normalize_period()` so direct dataclass construction cannot bypass kind/conditional-field validation。
- **Test**: direct contradictory fields and invalid direct kind now raise。
- **Status**: 已修复。

### CR-S1-02 — Duplicate normalized currency overwrite

- **Decision**: accepted。
- **Fix**: `DecimalAmountEnvelope` now rejects a second source key resolving to an existing canonical currency before assignment。
- **Test**: `{"usd": 1, "USD": 2}` raises `duplicate canonical currency: USD`。
- **Status**: 已修复。

## Validation

```text
python3 -m pytest tests/test_performance_period.py tests/test_performance_models.py tests/test_performance_instrument_identity.py -q
40 passed in 0.23s

python3 -m ruff check domain/domain/performance tests/test_performance_period.py tests/test_performance_models.py tests/test_performance_instrument_identity.py
All checks passed!

git diff --check
pass
```

## Docs Decision

No additional docs edit: existing S1 design already promises fail-closed conditional fields and authoritative native-currency maps; the fixes make code match that contract。

## Residual Risks and Uncovered Areas

Unchanged from the implementation artifact and classified to later approved slices。No unclassified residual risk remains。

## Next Entry Point

S1 code re-review using `deepreview`。
