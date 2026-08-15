# Gateflow Plan Fix — Sell Put Top1 W4

- Gate: `fix`
- Work unit: `sell-put-top1-w4`
- Reviewed artifact: `docs/reviews/plan-review-20260815-134811.md`
- Fixed target: `docs/gateflow/sell-put-top1-w4/plan.md`
- Artifact path: `docs/gateflow/sell-put-top1-w4/plan-fix.md`
- Completion status: complete; re-review pending

## Finding decisions

### PR-W4-01 — accepted — 已修复

`capture_recommendation_point()` now requires `trading_date` and must validate the exact indexed expectation artifact plus point-ID membership before any projection or point-index write. Wrong date, missing/late/conflicting expectation, and unexpected point are explicit no-write branches with tests.

### PR-W4-02 — accepted — 已修复

The plan now defines exact-key `sell_put_top1_research_window_facts.v1`. Its canonical content hash binds identity, cutoff, ordered date-list hash, calendar evidence ref/hash, latest mature date, maturity evidence ref/hash, and selector. W4 validates the binding while leaving provider truth to W0R/W5.

### PR-W4-03 — accepted — 已修复

The public signature retains `required_days=40` only for design compatibility and rejects every non-40 value. Tests build a real lightweight 40-date fixture.

### PR-W4-04 — accepted — 已修复

The plan now freezes exact command, freeze, and status result schemas, allowed status values, blocker reasons, and the small exception reason set.

### PR-W4-04R — accepted — 已修复

The re-review correctly found that two schemas still used descriptive nouns. The plan now lists literal field identifiers for freeze/status results, adds exact file-byte hashes where later contracts consume them, and requires exact-key tests.

## Validation

- Goal scope unchanged: only W4 Corpus behavior was clarified.
- Slice count remains two and behavior-based.
- No provider, W5/W6 evaluator, timer, generic abstraction, or future table was added.
- `git diff --check` remains the immediate syntax/whitespace gate before re-review.

## Residual risks

- Provider truth remains assigned to W0R/W5.
- Timer ordering/retention remains assigned to W7.
- Inert artifact-first orphans remain an accepted W4 tradeoff; cleanup requires measured growth.

All residual risks are classified to later approved work units.

## Next gate

`plan re-review`
