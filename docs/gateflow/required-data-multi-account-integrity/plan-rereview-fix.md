# Gateflow Plan Re-review Fix Artifact — Required Data and Multi-Account Integrity

- Gate: `plan re-review fix`
- Work unit: `required-data-multi-account-integrity`
- Revised plan: `docs/gateflow/required-data-multi-account-integrity/plan.md`
- Re-review inputs: `/root/plan_rereview_state`, `/root/plan_rereview_events_scope`,
  `/root/plan_rereview_contracts`
- Status: fixes applied; pending final plan re-review

## Finding disposition

### PRR-S-01 — accepted — 已修复

Config authority now belongs to the tick parent and is published before `prepare_portfolio_contexts()` or any account
child. S3 includes `tick_account_execution.py` and prepared-worker request plumbing. Existing same-run identical bytes
are adopted; different bytes fail before overwrite and preserve the original artifact. The exact bytes/path/SHA are
passed to all later consumers, and tests require zero prepared-worker/pipeline/Close Advice calls on conflict.

### PRR-E-01 — accepted — 已修复

`recovery_mode` is computed exactly once at barrier invocation entry, before any write, then frozen for that
invocation. A healthy fresh invocation does not self-transition after it publishes required-data records; a restarted
invocation observes pre-existing records and cannot silently produce a new generation.

### PRR-E-02 — accepted — 已修复

Event execution is now defined as one parent prefetch invocation per fresh run, with each union symbol resolving its
configured provider chain at most once. Legitimate cache/primary/fallback behavior is preserved, empty union performs
zero provider calls, and terminal re-entry performs zero provider-chain resolutions.

### PRR-S7-01 — accepted — 已修复

The shared event loader now has an explicit mode boundary: supplied `expected_event_identity` requires strict frozen
schema v2 and exact run/path/hash/union identity; identity-absent manual/account-less calls retain schema-v1
compatibility but return typed unknown for missing/malformed data. S7 now names `external_services.py`,
`pipeline_runtime.py`, `pipeline_watchlist.py`, CLI/request plumbing, and Daily Brief service tests.

## Residual risks

- Cached legacy required-data raw payloads without new completeness evidence must fail closed or refresh; classified
  as an S1 implementation acceptance condition.
- Multi-spec RV must be observed once and propagated consistently; classified as an S6 implementation acceptance
  condition.
- Expected fetch contract must bind spot tri-state/trading-date authority; classified as an S6 implementation
  acceptance condition.
- Provisional position requirements and finalized Close Advice plan v2 should share one internal core to avoid drift;
  classified as an S5 code-review concern.

All residual risks are covered by an approved slice and require deterministic tests; none is unclassified.

## Next gate

Run final PlanReview re-review. If no material finding remains, record `pass-with-risks`, accept the plan, and create
the protected plan commit.
