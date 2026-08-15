# Gateflow Plan Fix — Sell Put Top1 W1B

- Gate: `fix`
- Work unit: `sell-put-top1-w1b`
- Review artifact: `docs/reviews/plan-review-20260815-043833.md`
- Fixed target: `docs/gateflow/sell-put-top1-w1b/plan.md`
- Artifact path: `docs/gateflow/sell-put-top1-w1b/plan-fix.md`
- Status: `fix complete; pending plan re-review`

## Finding decisions and fixes

### PR-W1B-01 — accepted — fixed

Removed caller-supplied terminal-fee amounts. `calculate_expiry_efficiency()` now plans to accept exact account fee-plan facts and call `calc_futu_hk_terminal_fee()` itself with derived terminal kind, `order_price=strike`, `shares=multiplier`, and `contracts=1`. Incomplete fee-plan evidence remains fail-closed and `estimated_amount` is never substituted.

The plan now distinguishes complete, partial, invalidly typed, and extra-key fee-plan payloads: only extra keys are structural errors; absent/invalid allowed facts remain explicit incomplete evidence for the canonical calculator.

### PR-W1B-02 — accepted — fixed

Restored all three W1A ranking profiles as legal unique research levels. `current_tie_break` remains a baseline-equivalent control arm and is expected to produce zero delta, so it cannot pass the positive-improvement gate.

### PR-W1B-03 — accepted — fixed

Removed the contracts import from economics/statistics. The shared reason-bearing exception remains local to ExperimentSpec validation; malformed economics/statistics programmer input uses built-in `ValueError`, while evidence outcomes stay structured.

### PR-W1B-04 — accepted — fixed

Added the product document's single canonical market correction to the implementation allowlist: ExperimentSpec uses producer-compatible uppercase `HK`, while account stays lowercase. No broader product rewrite is authorized.

## Re-review clarity checks

- Research hashing now explicitly accepts either valid spec shape and projects the same research subset.
- Point/daily result row schemas, ordering, early-exit behavior, and concentration-gate position are explicit so implementation does not need to invent output semantics.

## Validation

- `git diff --check`: pending re-review checkpoint.
- No source implementation was started.
- PR #156/#157 merge remains an explicit pre-implementation prerequisite.

## Residual risks

- Runtime account fee-plan truth remains W0R-owned.
- The planning branch still lacks the unmerged prerequisite design/code files; the accepted-plan commit and implementation remain blocked until the merged-base transition.

## Decision

All accepted plan findings are addressed; next entry point: `plan re-review`.
