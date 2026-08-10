# Gateflow Accepted Plan — HK Combo Capture / Failure Notification

- Gate: `accepted plan commit`
- Work unit: `hk-combo-capture-failure-notification`
- Artifact path: `docs/gateflow/hk-combo-capture-failure-notification/accepted-plan.md`
- Branch: `fix/hk-combo-capture-failure-notification`
- Base: `main@0d635e11`
- Status: pass; ready for Slice S1 implementation

## Accepted scope

- S1: owner-aware capture/pair routing and independent opening, SP+LC Combo, and CC+LP snapshot status.
- S2: prefetch-before-failure current-run portfolio receipt with owner-local exact-byte reuse and fixed-failure preparation proof.
- Focused/integration tests and Gateflow artifacts only; public schema/config/CLI/notification policy are unchanged.

## Review evidence

- Goal confirmation: `docs/gateflow/hk-combo-capture-failure-notification/goal-confirmation.md`
- Accepted plan: `docs/gateflow/hk-combo-capture-failure-notification/plan.md`
- Initial plan review: `docs/reviews/plan-review-20260810-111309.md` — `fail`, five accepted findings.
- Plan review fix: `docs/gateflow/hk-combo-capture-failure-notification/plan-review-fix.md`.
- Plan re-review: `docs/reviews/plan-review-20260810-111928.md` — `pass-with-risks`, no remaining material findings.

## Finding status

- PR-01 fixed in plan: legacy variant-less SP+LC pairs are canonical `sp_lc`; unknown explicit variants fail closed.
- PR-02 fixed in plan: CC+LP `not_applicable` survives the producer boundary.
- PR-03 fixed in plan: per-symbol quote binding remains consistent across owners.
- PR-04 fixed in plan: source owner owns idempotent locate/publish/validate/reuse; no receipt field is added to `AccountRunRequest`.
- PR-05 fixed in plan: Daily Brief authority and no-send fixed-failure preparation tests are mandatory.

## Validation

- Plan artifacts checked with `git diff --check`.
- No production code or runtime data was changed at this gate.
- Commit preflight must stage only the six files listed in this artifact and leave all pre-existing dirty files untouched.

## Docs decision

The Gateflow artifacts are the only docs in scope. No public command, config, schema, notification copy, or operator workflow changed.

## Residual risks

- Scheduler processed-target/retry semantics: assigned to a later scheduler reliability work unit.
- OpenD expiration lookup timeout/retry: assigned to a later required-data/OpenD reliability work unit.
- Prepared portfolio receipt freshness during unusually long runs: covered by the existing fail-closed freshness validator; track operational evidence.
- Release and production upgrade: require separate explicit user authorization after this Draft PR.

## Next entry point

`implementation` — Slice S1 owner-aware capture routing and snapshot status.
