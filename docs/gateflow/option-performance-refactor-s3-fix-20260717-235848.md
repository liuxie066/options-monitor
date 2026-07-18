# Gateflow Fix — Option Performance Refactor S3

- **Gate**: code review fix
- **Work unit**: `option-performance-refactor`
- **Slice**: S3 — Core Period Performance Engine
- **Created at**: 2026-07-17 23:58:48 CST（本机时钟）
- **Finding source**: `docs/reviews/code-review-20260717-235513.md`
- **Artifact path**: `docs/gateflow/option-performance-refactor-s3-fix-20260717-235848.md`
- **Decision**: accept and fix S3-01 and S3-02

## Finding Disposition

### S3-01 — Illegal currency crashes the full report

- **Decision**: accepted。
- **Fix**:
  - monetary missing facts may carry `currency=None` only when `amount=None` and an explicit `missing_reason` exists；
  - event cash, option fee cash, allocation realized PnL and stock settlement paths now normalize currency through one fail-closed helper；
  - invalid currency nulls only the affected monetary facts and preserves activity plus known-currency subtotals；
  - added a mixed valid/invalid currency regression proving the report returns partial instead of raising and preserves the valid USD subtotal。
- **Status**: fixed。

### S3-02 — Negative settlement shares silently become positive

- **Decision**: accepted。
- **Fix**:
  - removed `abs()` from settlement share conversion；
  - settlement shares now must be an integer strictly greater than zero；
  - added a negative-shares regression proving settlement cash and total net cash fail closed while option realized PnL remains observed。
- **Status**: fixed。

## Validation

```text
python3 -m pytest tests/test_performance_engine.py tests/test_performance_service.py tests/test_ledger_economics.py -q
25 passed in 0.56s

python3 -m ruff check domain/domain/performance src/application/performance tests/test_performance_engine.py tests/test_performance_service.py
All checks passed!

git diff --check
pass
```

## Residual Risks

| Risk | Classification |
|---|---|
| Unknown-currency fact cannot attribute its missing amount to a native currency bucket | accepted contract: global/metric status is partial and known currency subtotals remain visible；no fabricated bucket |
| Valuation/FX/assigned-stock/capital semantics remain absent | covered by approved S4-S6 |
| Full repository tests not run at slice fix gate | covered by later slice/aggregate verification |

No unclassified residual risk remains。

## Next Entry Point

S3 code re-review using `deepreview`。
