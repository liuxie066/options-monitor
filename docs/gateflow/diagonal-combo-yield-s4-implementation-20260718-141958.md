# Diagonal Combo Yield — Slice S4 Implementation

## Gate State

- Slice: S4 — Quantity-aware group Close Advice and compatibility replay
- Decision: accepted after mandatory deepreview / fix / re-review
- Next gate: accepted S4 slice commit, then aggregate validation and deepreview
- Artifact path: `docs/gateflow/diagonal-combo-yield-s4-implementation-20260718-141958.md`

## Implemented Scope

- Replaced the first-Call-per-group lookup with aggregation over the event-derived option inventory contract.
- Runs existing leg evaluators and leg action mapping first, then applies additive group synthesis.
- Added a domain-owned Combo group advice truth table for `active_combo`, `missing_call`, `residual_call`, `closed`, and `review_required`.
- Aggregates multi-lot Put/Call quantities, Call cost/current value/fees, and Put realized value only when the group is unambiguous.
- Preserves leg `strategy_exit_mode`, tier, reason, `close_action`, and evaluator outputs; group fields do not replace the leg thesis.
- Added read-only fields: `combo_group_classification`, `combo_group_status`, `combo_group_action`, `combo_group_reason`, `combo_group_issues`, open Put/Call quantities, group quote status, and evidence scope.
- Exposed the additive group fields through `close_advice_read`.
- Put-only rendering no longer says to retain a missing Call; Call-only rendering explicitly says “剩余 Call”.
- Residual Call group action is translated from the existing long-call evaluator using the actual current Call quote.
- Quantity mismatch, missing quote, missing group identity, unsupported/mixed inventory, or conflicting per-lot actions fail closed with explicit issues and no group action/economics.
- Preserved legacy `yield_enhancement` strategy aliases and opaque historical group IDs for read compatibility while retaining account validation for canonical `combo_yield:<account>:` IDs.

## Preserved Boundaries

- No assignment or assigned-stock inference from `close_type` / `last_close_type`.
- No automatic stock sale, Call exercise, roll, replacement, or order execution.
- No future Call residual-value or terminal-value prediction.
- No mutable Combo group state or database migration.
- No production configuration, notification enablement, runtime state, or broker-facing write changes.

## Validation

```text
python3 -m pytest \
  tests/test_close_advice_runner.py \
  tests/test_close_advice_action_policy.py \
  tests/test_close_advice_reallocation_shadow.py \
  tests/test_notify_symbols_markdown.py

92 passed
```

```text
python3 -m pytest \
  tests/test_combo_yield_steps.py \
  tests/test_strategy_policy.py \
  tests/test_strategy_lab.py \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py

152 passed
```

```text
git diff --check
passed
```

## Covered Scenarios

- Diagonal multi-lot 2 Put + 2 Call aggregation with deterministic quantities and group economics.
- Put/Call quantity mismatch → `review_required`, no group action/economics, leg advice preserved.
- Missing current Call quote → `review_required`, no invented value.
- Put-only inventory → `missing_call`, Put-only wording without “keep Call”.
- Call-only inventory → `residual_call`, current-quote long-call advice with residual wording.
- Missing group identity → `review_required`.
- Unsupported/mixed inventory in the same group → `review_required`.
- Existing same-expiry combo economics and optional close-both behavior remain covered.

## Documentation Decision

Updated:

- `README.md`
- `docs/CLOSE_ADVICE_CONTRACT.md`
- `docs/TOOL_REFERENCE.md`

The updates document the additive group fields, option-only truth table, fail-closed conditions, residual Call semantics, and the no-assignment-inference boundary.

## Review Decision

- Initial review: `docs/reviews/code-review-20260718-142206.md`
- Re-review: `docs/reviews/code-review-20260718-142507.md`
- Findings F1-F3: all fixed.
- Decision: pass.

## Residual Risks / Uncovered Areas

- Historical single-leg rows with no group identity remain `review_required` rather than being guessed — **accepted product boundary for this work unit**.
- Closed groups have no open option row in `close_advice.csv`; their terminal classification remains available in the separate full lifecycle report — **covered by S3 and intentionally outside option-row Close Advice output**.
- Group synthesis intentionally does not consume assigned-stock facts — **covered by the approved S4 boundary and S3 reporting workflow**.
