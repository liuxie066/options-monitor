# Gateflow Fix 2 — Option Performance Refactor S2

- **Gate**: code re-review fix
- **Work unit**: `option-performance-refactor`
- **Slice**: S2
- **Created at**: 2026-07-17 23:33:22 CST（本机时钟）
- **Review source**: `docs/reviews/code-review-20260717-233159.md`
- **Artifact path**: `docs/gateflow/option-performance-refactor-s2-fix2-20260717-233322.md`
- **Completion status**: fix-complete；ready-for-second-re-review

## Finding Decision and Fix

### CR-S2-06 — Writer sanitized invalid explicit fee amount

- **Decision**: accepted.
- **Fix**: writer now validates any pre-existing provenance amount before replacing it with a split amount. An unparseable explicit amount is preserved verbatim so `fee_fact_for_event` continues to fail closed as missing. Numeric legacy `event.fees` may still be split for compatibility state, but it cannot upgrade malformed explicit provenance to actual evidence. If both provenance and numeric fee are unrepresentable, splitter uses zero only as the canonical numeric placeholder while provenance remains missing/invalid.
- **Validation**: public auto-target writer path with `basis=actual, amount=bad, fees=1` closes the lot, preserves gross 100, returns missing close fee with invalid-amount reason, and leaves production net null.
- **Status**: 已修复.

## Validation

```text
python3 -m pytest tests/test_ledger_projection.py tests/test_ledger_economics.py tests/test_ledger_sqlite_workflows.py -q
87 passed in 0.68s

python3 -m ruff check domain/domain/ledger src/application/ledger tests/test_ledger_economics.py tests/test_ledger_sqlite_workflows.py
All checks passed!

git diff --check
pass
```

## Docs Decision

No additional design edit: the existing S2 design already states malformed explicit provenance fails closed as missing.

## Residual Risks and Uncovered Areas

Unchanged from the first fix artifact and fully classified to S3 or later work. No unclassified residual risk remains.

## Next Entry Point

S2 second code re-review using `deepreview`.
