# Gateflow Implementation — Sell Put Top1 W4 S2

- Gate: `implementation`
- Work unit: `sell-put-top1-w4-s2`
- Accepted plan commit: `de7932bc`
- S1 accepted commit: `ce5f0759`
- Branch: `feat/sell-put-top1-w4`
- Status: implemented; DeepReview findings fixed; final re-review passed; ready for accepted S2 commit

## Implemented scope

- Added exact-key `sell_put_top1_corpus_status.v1` aggregation over the v2 SQLite indexes only.
- Added strict `sell_put_top1_research_window_facts.v1` validation with exact identity, cutoff, calendar sequence, evidence refs/hashes, fixed selector, and whole-object hash binding.
- Selected only the latest mature calendar entry and its preceding 39 entries; fewer entries warm, and a gap never falls back to an older clean window.
- Revalidated current immutable expectation/projection bytes, index bindings, cutoff chronology, projection schema, and baseline rank parity without reading source runs.
- Published only a compact content-addressed `sealed_historical_dataset.v1` containing 40 ordered date/expectation/point refs and hashes; no candidate rows or dataset table were added.
- Kept feature-off freeze side-effect free and aligned its explicit `feature_disabled` result with the existing plan feature-gate requirement.

## Review closure

- Initial DeepReview: `docs/reviews/code-review-20260815-143855.md` — one high and one medium finding.
- Fix decisions: `docs/gateflow/sell-put-top1-w4/s2-deepreview-fix.md` — both accepted and fixed.
- Final re-review: `docs/reviews/code-review-20260815-144718.md` — zero unresolved findings.

## Verification

- Focused W1-W4 S2 plus producer/scheduler/architecture suite: `120 passed`.
- The real 40-date fixture includes multiple official points on one day, deletes source `output_runs`, reruns baseline parity, and returns the same dataset.
- Ruff: pass.
- BasedPyright over `corpus.py`: `0 errors` at error level.
- Dependency graph: current; production modules `589`; cycles `0`.
- `git diff --check`: pass.

## Remaining boundary

W4 does not calculate provider calendar/maturity truth, run the 40-day research, create an experiment, collect the independent 20-day hidden window, or install timers. Those remain W0R/W5/W6/W7 responsibilities. No production config/service/provider/live-data write, release, or deployment occurred.
