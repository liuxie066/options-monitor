# Gateflow Fix Artifact — PlanReview

- Work unit: `candidate-brief-evidence-integrity`
- Gate: `fix`
- Review artifact: `docs/reviews/plan-review-20260812-081937.md`
- Changed artifact: `docs/gateflow/candidate-brief-evidence-integrity/plan.md`
- Status: fixed and accepted by PlanReview re-review
- Artifact path: `docs/gateflow/candidate-brief-evidence-integrity/plan-review-fix.md`

## Finding decisions and fixes

### PR-01 — accepted — fixed

The plan no longer proposes adding `net_premium_non_positive` as a top-level Candidate Engine reject reason. It now
requires the existing canonical `REJECT_POLICY_REJECTED` top-level category and retains the stable calculation code in
`metric_value.reason_code`. Tests must prove Candidate Engine payload validation succeeds and no reject-vocabulary
change occurs.

Final status: `已修复`.

### PR-02 — accepted — fixed

The plan records the user-owned in-progress files and moves implementation to an isolated clean clone after the
accepted-plan checkpoint. User compact-card hunks remain outside the work unit and must not be staged, stashed,
reverted, copied into the clone, or committed. Focused validation must run against committed clean code.

Final status: `已修复`.

## Validation

- Re-read `CANDIDATE_REJECT_REASONS` and `normalize_candidate_reject()` to verify `policy_rejected` is canonical and
  `net_premium_non_positive` is not a valid top-level reason.
- Re-read the initially observed four-file user diff and recorded its historical pre-fix SHA-256:
  `831f85decdb331ddd6abc7658ea6326f433409038501b6ca339e168760133d5f`.
- Checked that the revised plan adds no implementation slice, public schema, new fact source, release action, or goal.

## Docs decision

Only Gateflow artifacts changed in this fix gate. Public documentation remains out of scope.

## Residual risks

- Continued concurrent changes to the five protected user files: `fixed in the current plan` through isolated clone
  execution after the accepted-plan checkpoint.
- Exact manual symbol-subset CC+LP config propagation: `assigned to later work unit`.
- DeepSeek provider reliability: `assigned to later work unit`.

## Post-fix ownership update

Before re-review, two additional workspace facts appeared and were resolved explicitly by the user:

- `docs/DEPENDENCY_GRAPH.md` is included in the protected unrelated patch set;
- the existing `fetched` status hunk in `daily_decision_brief_service.py` and its focused test are authorized as
  unreviewed Slice 2 implementation input.

The plan records the five-file protected fingerprint and the separate two-file authorized implementation fingerprint.
This update changes neither the confirmed goal nor the two-slice design.

Further saves proved that the protected patch was not actually frozen. The plan therefore replaces overlapping hunk
staging with an isolated-clean-clone protocol after the accepted-plan checkpoint. This removes the concurrency and
dirty-tree test dependency instead of repeatedly asking the user to freeze unrelated work.

## Completion status

Both accepted PlanReview findings are fixed and the re-review concluded `pass-with-risks` in
`docs/reviews/plan-review-20260812-083249.md`. Current gate / next entry point: `accepted plan commit`.
