# Gateflow Fix Artifact — Earnings Near-Expiry Window PlanReview

- Work unit: `earnings-near-expiry-window`
- Gate: `plan review fix`
- Date: 2026-08-11
- Reviewed plan: `docs/gateflow/earnings-near-expiry-window/plan.md`
- Failed review: `docs/reviews/plan-review-20260811-204658.md`
- Artifact path: `docs/gateflow/earnings-near-expiry-window/plan-review-fix.md`
- Status: fixes complete; pending PlanReview re-review

## Finding decisions and fixes

### PR-01 — accepted — fixed in plan

The plan no longer equates every earnings evidence gap with an outcome-unresolved contract. It now defines two
orthogonal facts:

- diagnostic gaps preserve every unavailable reason for audit and warnings;
- eligibility outcome is `accepted`, `definitive_reject`, or `unresolved`.

Only unavailable-only decisions are unresolved. A contract with an earnings gap plus any existing definitive
policy/ineligibility rejection remains a definitive rejection and does not poison candidate-universe completeness.
The plan also corrects the evaluated-contract count contract and adds mixed-reason regressions.

Final status: `已修复`.

### PR-02 — accepted — fixed in plan

The v2 earnings evidence contract now requires exact continuous interval coverage, valid interval state combinations,
event-to-successful-interval provenance, exact underlier/expiration projection keys, and canonical source-to-projection
equality. The consumer reprojects from validated top-level source facts instead of trusting stored derived state.
Missing/overlapping/duplicate intervals, coverage drift, projection mismatch, or a blocker backed only by a failed
interval fail closed. The provider test matrix includes each counterexample.

Final status: `已修复`.

### PR-03 — accepted — fixed in plan

The plan locks the minimal compatibility path: `opening_candidate_snapshot.v1` and its content-hashed `scope_results`
remain the single frozen authority. The validator is strengthened, and one shared pure projection derives the
candidate-universe summary for AI Advice and Daily Brief. The derived summary is included only in the existing hashed
AI candidates input; no duplicate opening-snapshot field or schema fork is added.

Final status: `已修复`.

## Additional plan readiness improvements

- Added exact slice prerequisites, call path, invariants, completion signal, and stop condition.
- Added direct goal-to-plan mapping and an explicit no-goal-drift statement.
- Closed schema/implementation choices and recorded no blocking open questions.
- Classified every residual risk and added the required final completion-report format.

## Validation

- Compared each accepted finding against the revised contract and test matrix.
- Confirmed the plan still uses one implementation slice and does not add a provider, general snapshot framework,
  config surface, database migration, Combo Advice adapter, or operational mutation.
- Pending: independent PlanReview re-review.

## Documentation decision

This artifact and the plan are the only files changed in this fix gate. Product/architecture documentation remains
part of implementation S1 after the plan checkpoint is accepted.

## Residual risks and uncovered areas

- **assigned to later work unit**: factually incomplete successful OpenD responses cannot be detected without an
  independent source.
- **intentional confirmed policy**: same-day events remain pending through market-local midnight.
- **covered by implementation S1**: all three accepted findings require code and deterministic tests after plan
  acceptance; this fix gate changes the plan only.

There are no unclassified residual risks or blocking open questions.
