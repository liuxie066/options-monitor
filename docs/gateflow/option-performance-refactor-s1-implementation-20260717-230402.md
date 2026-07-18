# Gateflow Implementation — Option Performance Refactor S1

- **Gate**: implementation
- **Work unit**: `option-performance-refactor`
- **Slice**: S1 — Period, Money, Instrument, Metric and Quality Contracts
- **Created at**: 2026-07-17 23:04:02 CST（本机时钟）
- **Approved plan**: `docs/gateflow/option-performance-refactor-plan-20260717-224048.md`
- **Artifact path**: `docs/gateflow/option-performance-refactor-s1-implementation-20260717-230402.md`
- **Completion status**: implementation-complete；ready-for-code-review

## Objective and Outcome

Implemented pure domain contracts for all approved period kinds, versioned account-independent option/stock instrument identity, Decimal money envelopes, explicit metric quality and fee provenance。No application I/O, DB, Agent or runtime config path was added。

## Changed Files

- `domain/domain/performance/__init__.py` — public domain exports。
- `domain/domain/performance/period.py` — `PeriodRequest`, `PeriodWindow`, strict parser and Asia/Shanghai half-open normalization。
- `domain/domain/performance/models.py` — instrument codecs, Decimal amount/quality envelopes and fee facts。
- `tests/test_performance_period.py` — MTD/YTD/month/year/range, current/past/cutoff and validation tests。
- `tests/test_performance_instrument_identity.py` — codec round-trip, canonical encoding, invalid decode and cross-account/side reuse tests。
- `tests/test_performance_models.py` — Decimal, status and actual/estimated/missing fee semantics。
- `docs/OPTION_PERFORMANCE_DESIGN.md` — authoritative S1 contract documentation。

## Decisions and Invariants

- Operator dates are fixed to `Asia/Shanghai`; internal intervals are `[start,end_exclusive)` Unix milliseconds。
- Current periods end at injected `now_ms + 1`; opening/end valuation instants are exactly outside/inside the half-open boundaries。
- Conditional period fields fail closed; future calendar inputs and out-of-period internal cutoffs are rejected。
- Evidence instrument identity excludes broker/account/position side and requires currency/multiplier for option conversion。
- Decoders accept only canonical v1 strings, preventing alternate encodings from creating duplicate identities。
- Money uses finite `Decimal` quantized to six decimals。Missing values and actual zero remain distinct。
- Gross/net computation is not implemented in S1; only the fee evidence contract needed by later slices exists。

## Validation

```text
python3 -m pytest tests/test_performance_period.py tests/test_performance_models.py tests/test_performance_instrument_identity.py -q
38 passed in 0.17s

python3 -m ruff check domain/domain/performance tests/test_performance_period.py tests/test_performance_models.py tests/test_performance_instrument_identity.py
All checks passed!

git diff --check
pass
```

## Docs Decision

Updated `docs/OPTION_PERFORMANCE_DESIGN.md` because S1 creates public domain contracts and deterministic identity formats。

## Residual Risks and Uncovered Areas

| Area | Classification |
|---|---|
| Ledger economic allocations and fee conservation | covered by later approved S2 |
| Report aggregation, cash and PnL | covered by later approved S3 |
| Evidence persistence/selectors/live collection | covered by later approved S4 |
| Adjusted/non-standard option market identity proof | covered by later approved S4 collection quality path；unsupported facts fail explicit |
| Public Agent/CLI validation parity | covered by later approved S7 |

No unclassified residual risk remains for S1 code review。

## Next Entry Point

S1 code review using `deepreview`。
