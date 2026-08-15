# Gateflow Plan Fix — Sell Put Top1 W2

- Gate: `plan review -> fix`
- Work unit: `sell-put-top1-w2`
- Plan: `docs/gateflow/sell-put-top1-w2/plan.md`
- Review: `docs/reviews/plan-review-20260815-100136.md`
- Artifact path: `docs/gateflow/sell-put-top1-w2/plan-fix.md`

## Finding decision

### PR-W2-01 — accepted — fixed

The plan no longer assumes that `partial_data` or `data_unavailable` implies an empty producer accepted set.

Changes:

- Every point now preserves the ordered accepted Sell Put IDs from the validated opening snapshot.
- Clean `candidates_found/no_candidate` points must pass W1A projection and match its accepted IDs exactly.
- Incomplete `partial_data/data_unavailable` points must remain non-evaluable by W1A, but may still retain accepted IDs from usable sibling scopes.
- The point validator only derives hard cardinality rules for clean statuses: `candidates_found` is non-empty and `no_candidate` is empty.
- Added an explicit mixed-scope fixture: one successful Sell Put scope with a candidate plus one failed sibling scope must publish an incomplete point, retain the accepted ID, and fail W1A projection.
- Clarified that the builder recomputes the terminal manifest byte hash from its canonical payload.

Final status: `已修复`.

## Validation

- The correction maps directly to the confirmed goal of preserving every official scheduled producer decision.
- It adds no schema key, state, store, workflow, or later-module behavior.
- W1A remains the evaluability boundary and does not become the point publisher.

### PR-W2-02 — accepted — fixed

The plan now keeps candidate rankability and Sell Put universe completeness separate:

- It reuses `candidate_universe_summary()` and filters affected scopes to `strategy_mode=put`.
- Aggregate `data_unavailable` remains the strongest status; otherwise any affected Sell Put scope forces point `partial_data`.
- Every point attempts the W1A seam and compares IDs whenever W1A succeeds.
- Only clean statuses require W1A success; an incomplete point stays non-evaluable even when its usable accepted subset can be projected.
- Added a completed/`partial_data` scope with an accepted candidate fixture so W1A success cannot erase the incomplete point status.

Final status: `已修复`.

## Residual risks

- Cross-run duplicate reconciliation: assigned to W4.
- Post-watermark crash gap: assigned to W4 consumption semantics.
- Service/profile delivery and account opt-in: assigned to W7 and W3.

## Decision

`fix complete`; next gate: `plan re-review`.
