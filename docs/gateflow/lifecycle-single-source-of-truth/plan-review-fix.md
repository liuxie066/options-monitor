# Gateflow Plan Review Fix — Lifecycle Single Source of Truth

- Work unit: `lifecycle-single-source-of-truth`
- Gate: `plan review -> fix`
- Date: 2026-08-04
- Reviewed target: `docs/gateflow/lifecycle-single-source-of-truth/plan.md`
- Review artifact: `docs/reviews/plan-review-20260804-001846.md`
- Status: fixes applied; pending re-review
- Artifact path: `docs/gateflow/lifecycle-single-source-of-truth/plan-review-fix.md`

## Finding decisions and fixes

### PR-01 — accepted — 已修复

The plan no longer infers anchor absence from `pairing_until_ms is None`.

- Canonical `lifecycle_evidence_status == missing` is required for the no-anchor materialization path.
- Evidence-present/no-effective-pairing cases go through the existing close-reason reconciler for typed timing/evidence failure classification.
- The plan adds an anchor-present/timing-unavailable regression.

### PR-02 — accepted — 已修复

The proposed required `account` argument was removed.

- `run_history_backfill()` keeps its current signature.
- Discovery accounts are derived only from the current `futu_account_ids` and canonical `account_mapping`.
- Modern single-account and supported legacy multi-account sources are both handled with explicit per-account discovery calls; `account=None` is never passed.
- Incomplete mapping produces a typed all-or-nothing lifecycle-discovery scope failure instead of partial scanning.

### PR-03 — accepted — 已修复

The no-anchor deadline materialization now uses the existing atomic state writer with `public_transition=None`.

- State fingerprint, revision, and generation-token CAS remain canonical.
- The historical no-anchor aging path does not gain a new lifecycle notification Outbox side effect.
- Apply and replay tests must assert no Outbox rows.

## Validation

- Plan now specifies the distinct missing-evidence, evidence-without-effective-pairing, and effective-pairing branches.
- S2 no longer changes a supported source signature or requires a new config field.
- All accepted findings have concrete regression assertions and no unclassified residual risk.

## Docs decision

No additional public documentation beyond the already planned lifecycle ownership/account-scope clarification is required for the plan fix.

## Residual risks

- Existing production rows and deployment verification remain assigned to a separately authorized operations step.
- The additive multi-account backfill diagnostics shape will be documented by implementation tests and the S2 artifact.

## Completion status

Plan fixes complete; next entry point is adversarial plan re-review.

