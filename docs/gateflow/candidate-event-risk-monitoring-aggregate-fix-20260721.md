# Gateflow Fix Artifact — Aggregate Deepreview

- **Work unit**: `candidate-event-risk-monitoring`
- **Gate**: aggregate deepreview fix
- **Finding**: `ADR-01`
- **Review**: `docs/reviews/code-review-20260721-101725.md`
- **Artifact path**: `docs/gateflow/candidate-event-risk-monitoring-aggregate-fix-20260721.md`

## Fix

`_fetch_split_rows()` now distinguishes fully exhausted pagination from a configured page-limit truncation:

- terminal/empty/repeated `next_key` still returns the collected rows as complete;
- a non-terminal `next_key` after the last allowed page raises into the existing split-category error path;
- the event snapshot therefore records split coverage as `partial`, so candidate event projection remains `unknown` rather than confirming absence;
- the public `fetch_symbol_events_futu()` compatibility return type remains unchanged.

## Regression proof

Added `test_fetch_symbol_event_evidence_futu_marks_truncated_split_pagination_partial`, which simulates an endless non-terminal `next_key` chain and asserts partial split coverage with no accepted truncated split rows.

## Finding status

- `ADR-01`: **已修复**.

## Residual risk

- Live provider payload semantics remain assigned to separately authorized canary work.
- No unclassified code risk remains from this finding.

## Completion status

Fix complete; ready for aggregate re-review.
