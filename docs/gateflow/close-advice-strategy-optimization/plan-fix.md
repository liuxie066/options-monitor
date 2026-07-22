# Gateflow Plan Fix — Close Advice Strategy Optimization

- Gate: plan fix
- Work unit: `close-advice-strategy-optimization`
- Source review: `docs/reviews/plan-review-20260723-004251.md`
- Revised plan: `docs/gateflow/close-advice-strategy-optimization-plan-20260723.md`
- Status: complete; plan re-review passed with classified risks

## Finding decisions

### PR-01 — accepted — 已修复

- Removed unprovable `weakened/lost` states.
- P2 now uses only current `valid|observe|not_evaluable` evidence.
- Underwriting medium can only hold or review; IV/RV, delta, event, stress, or willingness observations never produce a P2 close.
- Strong with incomplete thesis evidence downgrades to review.

### PR-02 — accepted — 已修复

- Removed post-run reallocation as a P2 production dependency.
- P3 alone composes P2 with reallocation evidence offline.
- Promotion of a two-stage P3 runtime is explicitly assigned to a later work unit.

### PR-03 — accepted — 已修复

- Added material-fact fingerprint, timestamped `episode_id`, separate `episode_date`, and explicit same-day/cross-day/dedup identity tests.

### PR-04 — accepted — 已修复

- Defined three optional close facet files and schemas.
- Kept candidate dataset files and behavior unchanged when the facet is absent.
- Required facet-specific mark/settle/readiness/analyze dispatch.
- Split S3 into S3a schema/capture, S3b marks/outcomes, and S3c readiness/status.

### PR-05 — accepted — 已修复

- Fixed decision-time incremental value as the comparison basis.
- Added short-option horizon formulas, fee occurrence, lifecycle precedence, no-double-counting rule, and inconclusive handling.
- Required hand-calculated Put/Call fixtures.

### PR-06 — accepted — 已修复

- Kept only mechanical dataset readiness as a hard machine gate.
- Replaced undefined quality pass/fail words with paired descriptive metrics, exact close-precision denominator, coverage, and explicit CEO trade-off decisions.

## Validation

- `git diff --check`: pass.
- Direct-source facts rechecked:
  - current short-vol states are `valid|observe|not_evaluable`;
  - current reallocation shadow runs after formal Close Advice;
  - existing Shadow Replay files and mark/settle paths are candidate-centric.
- No production source, config, notification, ledger, or runtime artifact changed in this gate.

## Residual risks

- Evidence readiness may take multiple natural days: requiring explicit user decision at S5.
- Covered Call stock/tax thesis remains unavailable: assigned to the bounded P2 willingness/observation contract.
- P3 production two-stage sequencing: assigned to a later work unit; P3 remains offline here.
- Historical marks that do not exist cannot be reconstructed: assigned to forward evidence collection.

## Next entry point

S1 implementation.
