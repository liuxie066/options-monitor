# Gateflow Fix — Sell Put Top1 W4 S2 DeepReview

- Gate: `fix`
- Work unit: `sell-put-top1-w4-s2`
- Review artifact: `docs/reviews/code-review-20260815-143855.md`
- Status: both findings accepted and fixed; pending re-review

## Finding 1 — accepted — fixed

Point capture now rejects a supplied capture timestamp that precedes the canonical official decision before any Corpus write. Dataset freeze additionally requires every selected expectation seal, projection decision, and point capture timestamp to be strictly earlier than the bound `cutoff_at_utc`; otherwise it returns `research_dataset_coverage_missing` and publishes no dataset. The regression moves the cutoff before the final selected day's evidence and proves the ready result is blocked.

## Finding 2 — accepted — fixed

Freeze now checks the indexed projection schema before invoking the strict current-schema artifact reader. A clean older-schema point is classified as `research_dataset_coverage_missing`, allowing new compatible evidence to warm normally; disagreement within a row that claims the current schema remains `research_corpus_conflict`. The regression temporarily presents a clean v0 index row and verifies the coverage result.

## Additional coverage

- The 40-day fixture now includes one day with three official recommendation points and proves the frozen point refs remain in expectation order.
- Calendar drift, baseline parity failure, latest-window gap without older fallback, source-run deletion, and conflict-over-gap priority remain covered.
- A selected expectation is strictly read before late/empty/calendar coverage classification, so immutable artifact disagreement still wins as `research_corpus_conflict`.
