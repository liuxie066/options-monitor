# Gateflow Accepted Plan — Lifecycle Single Source of Truth

- Work unit: `lifecycle-single-source-of-truth`
- Gate: `accepted plan`
- Date: 2026-08-04
- Plan: `docs/gateflow/lifecycle-single-source-of-truth/plan.md`
- Initial review: `docs/reviews/plan-review-20260804-001846.md`
- Fix artifact: `docs/gateflow/lifecycle-single-source-of-truth/plan-review-fix.md`
- Accepted re-review: `docs/reviews/plan-review-20260804-002343.md`
- Decision: accepted
- Next entry point: implementation Slice S1
- Artifact path: `docs/gateflow/lifecycle-single-source-of-truth/accepted-plan.md`

## Accepted decisions

- Ledger discovery creates cases only and never refreshes an existing case projection.
- Canonical due reconciliation owns deadline aging and distinguishes missing evidence from evidence with unavailable effective timing.
- The missing-evidence deadline path uses the atomic writer without a public transition, preserving the existing no-Outbox behavior.
- Backfill derives a complete explicit account set from the current Futu IDs and mapping; every discovery call is account-scoped, including legacy multi-account sources.
- No schema, config, public CLI argument, lifecycle state, provider rule, or notification retry workflow is added.

## Finding status

- PR-01: accepted, fixed, re-reviewed.
- PR-02: accepted, fixed, re-reviewed.
- PR-03: accepted, fixed, re-reviewed.
- New findings: none.

## Validation decision

Implement and review S1 and S2 separately, then run the aggregate lifecycle/backfill/tick matrix before aggregate deepreview.

## Docs decision

Update `docs/FUTU_TRADE_HOLDINGS_SYNC.md` in S2 to record create-only discovery and account-scoped due ownership.

## Residual risks

- Production convergence and deployment verification remain assigned to a separately authorized operations step.
- The existing at-most-60-second due cadence is accepted.

## Completion status

Plan gate passed with all findings fixed and classified residual risks.

