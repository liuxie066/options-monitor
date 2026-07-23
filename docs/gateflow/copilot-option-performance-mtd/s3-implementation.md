# Gateflow Slice 3 Implementation — Feishu MTD Answer Quality

- Work unit: `copilot-option-performance-mtd`
- Slice: `S3`
- Gate: `implementation`
- Date: 2026-07-23
- Status: complete
- Base: `9e84cb33`

## Model guidance

The compact OM Chat rules now require option-income/performance questions and corrections to:

- call `option_performance_report` first instead of generic analysis;
- express MTD as `period=mtd` without month/year/range pollution;
- keep omitted account as all-account scope;
- state period/status/scope, combined/pure-option/assigned-stock realized PnL, cash, premium,
  assignment inclusion, and evidence gaps;
- keep assignment/stock cash distinct from profit.

The full system prompt remains below the existing context-compaction threshold. Existing financial
and Control safety rules remain present.

## Exact conversation regression

The deterministic answer-quality suite now includes the reported Feishu conversation:

```text
7月 mtd 的期权收益
我写的是mtd
```

The first turn must call `option_performance_report` with `period=mtd`. The correction can reuse
the canonical evidence already in conversation; if it reads again, it must use the same canonical
tool and MTD/all-account input. The expected answer names:

- MTD and all-account scope;
- combined realized PnL;
- pure-option and assigned-stock realized PnL;
- total/major cash flows and premium activity;
- whether assignment is included;
- why assignment principal and stock-sale proceeds are not profit.

## Production-side P1 evaluation contract

The P1 evaluator now supports case-specific:

- required first tool;
- required first-tool inputs and forbidden scope fields;
- an optional canonical retry for follow-up corrections;
- required response terms.

For both MTD turns, a new generic `analysis_query` call fails structural evaluation, even though
it is a pure-read tool. Natural-month or silently narrowed `lx` input fails. Missing
MTD/all-account/realized/cash/assignment language also fails.

## Validation

```text
ruff: All checks passed.
pytest: 101 passed.
```

The focused suite includes prompt manifest/context compaction, Copilot engine/host behavior,
exact answer-quality scenarios, P1 evaluation, option-performance input normalization, and
deterministic rendering.

## Safety

- No live Feishu request or notification was sent.
- No production data, configuration, ledger state, or service was changed.
- The P1 evaluator remains read-only.

## DeepReview

- Initial review: `docs/reviews/code-review-20260723-171757.md`
- Accepted fixes: `docs/gateflow/copilot-option-performance-mtd/s3-review-fix.md`
- Passing re-review: `docs/reviews/code-review-20260723-172105.md`
- Gate record: `docs/gateflow/copilot-option-performance-mtd/s3-code-review.md`
