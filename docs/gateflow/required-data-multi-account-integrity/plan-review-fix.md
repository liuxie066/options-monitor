# Gateflow PlanReview Fix Artifact — Required Data and Multi-Account Integrity

- Gate: `plan fix`
- Work unit: `required-data-multi-account-integrity`
- Reviewed artifact: `docs/reviews/plan-review-20260804-100017.md`
- Revised plan: `docs/gateflow/required-data-multi-account-integrity/plan.md`
- Status: fix complete; pending plan re-review

## Finding disposition

### PLR-01 — accepted — 已修复

S1 now includes `opend_symbol_outputs.py`, `required_data_snapshot.py`, and `required_data_steps.py`. The plan makes
the final publication postcondition mandatory at the actual publisher/caller/seal boundaries and adds direct
publisher plus bundle/plan mismatch regressions.

### PLR-02 — accepted — 已修复

The revised plan defines a canonical expected fetch contract, limits exact matching to plan-owned policy, separates
per-symbol failure from corrupt global-plan failure, defines no-contract RV N/A semantics, and adds exact receipt
adoption with stable evidence timestamps for crash re-entry.

### PLR-03 — accepted — 已修复

S3 now serializes once, atomically writes both consumed and compatibility artifacts from the same bytes, verifies
both hashes, and only then starts the child. Archive failure explicitly has zero pipeline/Close Advice calls.

### PLR-04 — accepted — 已修复

S5 replaces sequential account reads with a narrow repository `read_decision_state_rows_many(accounts)` operation
using one connection and one SQLite `BEGIN`; snapshots are built from the supplied group rows. Lifecycle/identity-only
concurrency is an explicit regression.

### PLR-05 — accepted — 已修复

S5 now requires complete trusted snapshot postconditions, binds one time anchor, closes position-hash fields and
sorting, distinguishes unavailable from trusted zero-lot, and makes ledger-group terminal re-entry all-or-none with
zero ledger reads.

### PLR-06 — accepted — 已修复

Prepared mode now loads exact validated bytes, prohibits all live/cache/FX/global-risk fallbacks, binds canonical
config/broker/ledger authority, preserves stable lot-local requirement IDs, and requires promotion-time revalidation
plus prepared provenance in the report manifest. Any required path-risk aggregate comes from the same batch facts.

### PLR-07 — accepted — 已修复

S6 now includes the real spot planner and CLI owner, defines observed-`None` tri-state plumbing, assigns the canonical
exact-spec parser, rejects legacy/spec mixing, forces every subprocess todo execution, and adds idempotency/gateway/
single-finalize tests.

### PLR-08 — accepted — 已修复

S7 now separates event artifact integrity from provider observation outcome, includes annotator/Daily Brief/Close
Advice consumers, requires one strict loader, and defines terminal re-entry as validate/reuse with zero provider
calls or fail-closed corruption.

### PLR-09 — accepted — 已修复

FX work is explicitly split between S4 manual shared-path fallback and S5 immutable parent observation. The revised
contract defines ready/stale-fallback/unavailable states, finite-positive validation, conditional account dependency,
same-run reuse, and focused exchange-rate tests.

### PLR-10 — accepted — 已修复

S2 now includes `tests/test_cli_runtime_paths.py` and `tests/test_cli_domain_split.py` and requires zero calls to
runtime-root/config/state/adapter/subprocess owners before returning unsupported-operation.

## Scope and risk check

- No persistent schema, service, provider, production config, notification, release, deployment, or merge action was
  added.
- The only new contract is the narrow prepared option-context authority already required to close the two-read
  defect; batch reads, FX, and event terminal facts extend existing owners.
- Deferred TOCTOU, manual multiplier drift, telemetry/performance, dead-code, and future multi-binding filename work
  remain explicit non-goals.

## Next gate

Run PlanReview against the revised plan. Do not enter implementation until the re-review has no material unresolved
finding.
