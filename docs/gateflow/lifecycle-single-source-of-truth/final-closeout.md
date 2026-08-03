# Gateflow Final Closeout

- Gate: `final closeout`
- Work unit: `lifecycle-single-source-of-truth`
- Completion status: `final closeout pass`
- Draft PR: `https://github.com/liuxie066/options-monitor/pull/132`
- Accepted PR-review commit: `0acf5fd1`
- Base: `main@ed2531e9`
- Artifact path: `docs/gateflow/lifecycle-single-source-of-truth/final-closeout.md`

## Root cause closed

The history-backfill discovery path discarded current-source account identity and independently refreshed existing lifecycle cases with a historical fixed-72-hour calculation. The canonical read model and due path used the bound broker timing policy. That dual ownership could oscillate an `lx` lifecycle projection after a notification was prepared, changing the option-position context fingerprint and blocking `lx` before provider delivery while `sy` remained deliverable.

## What changed

- Lifecycle discovery freezes expired lots and inserts missing immutable cases only; it no longer refreshes an existing case status or derived summary.
- The canonical lifecycle read model remains the single calculation source for allocation, evidence, broker timing policy, deadline, and derived reason state.
- Account-scoped due reconciliation owns deadline transitions and writes through the existing atomic generation-token/CAS/fingerprint/revision boundary.
- Missing evidence at the canonical deadline fails closed as `needs_review` with `public_transition=None`, preserving the historical no-notification behavior.
- Evidence present without effective timing uses the existing typed close-reason reconciler and never triggers broker observation collection.
- History backfill derives complete sorted explicit accounts from the current Futu IDs and canonical mapping, then invokes discovery once per account in both phases; it never passes `account=None`.
- Incomplete mapping performs zero partial discovery calls, preserves payload unresolved/audit handling, and produces an observable top-level lifecycle failure.
- Existing v2 discovery result, audit envelope, compatibility fields, Inbox/checkpoint, CLI/tick, and notification-rendering contracts remain intact.

## Verification

Focused slice evidence:

- S1 final: `26 passed in 1.57s`.
- S2 final: `14 passed in 1.12s`.

Aggregate command:

```bash
PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_aggregate_pytest python3.12 -m pytest -q \
  tests/test_position_advice_v2_lifecycle_reconciliation.py \
  tests/test_settlement_observation.py \
  tests/test_trades_auto_intake_backfill.py \
  tests/test_trades_auto_intake_cli.py \
  tests/test_unified_tick_entrypoint.py \
  tests/test_multi_tick_notify_format.py
```

Result: `86 passed, 4 warnings in 5.68s`; warnings are classified pre-existing Legacy Tick renderer deprecations.

Additional gates:

- `PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_aggregate_compile python3.12 -m compileall -q domain src scripts`: pass.
- Ruff across every changed Python production/test file: pass.
- Full-branch and working-tree `git diff --check`: pass.
- Backfill search: one discovery call with explicit loop account; no `account=None` discovery call.
- GitHub at accepted PR-review head `0acf5fd1`: Agent Plugin, Guardrails, CodeQL actions, CodeQL Python, and CodeQL summary all passed.
- PR #132: open, Draft, CLEAN, and mergeable at final recheck.

The repository `.venv` lacks pytest, so deterministic tests used available system Python 3.12 with bytecode redirected to `/tmp`; no dependency files were changed.

## Review finding status

- PlanReview PR-01, PR-02, PR-03: accepted, fixed, and re-reviewed.
- S1 DeepReview S1-01: accepted, fixed, and re-reviewed.
- S2 DeepReview S2-01, S2-02: accepted, fixed, and re-reviewed.
- Aggregate DeepReview: passed with no new findings.
- PR #132 DeepReview: passed with no new findings.
- Unclassified findings: none.

## Accepted commits

- `ac366c5a` — accepted plan.
- `07934baa` — accepted S1.
- `35b5c3ba` — accepted S2.
- `c9cd7ce2` — accepted aggregate validation/review.
- `0acf5fd1` — accepted PR #132 review evidence.

## Documentation

- `docs/FUTU_TRADE_HOLDINGS_SYNC.md` documents create-only discovery, canonical due ownership, and explicit account-scoped backfill.
- Goal, plan, PlanReview, slice implementation/review/fix, aggregate validation/review, PR review, and this closeout are preserved under `docs/gateflow/lifecycle-single-source-of-truth/` and `docs/reviews/`.
- No public CLI example or config schema changed.

## Remaining risks and owners

- Existing production rows and actual runtime convergence are not verified by source delivery. Owner: separately authorized deployment/operations step; verify deployed ancestry, service/runtime fingerprints, natural due convergence, per-account prepared context, delivery batches, provider attempts, and delivery confirmation before declaring production closure.
- Multi-account create-only discovery is atomic per account rather than one cross-account transaction. Owner: existing idempotent retry path; any later-account failure is surfaced at the top level while earlier inserts remain safe.
- Lifecycle-only failure does not rewind a successful history checkpoint. Owner: backfill runtime; discovery reruns independently on every interval.
- Existing due cadence is at most 60 seconds. Owner: current runtime scheduler; accepted for this work unit.
- Repository-local `.venv` dependency drift remains. Owner: separate environment-maintenance work; it does not block the validated source change.

## Issue link status

This work unit was initiated from the confirmed operator investigation and is not tied to a GitHub issue, so no issue closing keyword or closeout comment is required.

## Safety boundary

- Tests used temporary stores and fake or absent providers; no real notification or broker request occurred.
- No production config, ledger, position, trade event, service, release, deployment, or remote environment was changed.
- No merge, approval, reviewer request, Ready transition, release, deployment, or upgrade was performed.

## Next entry point

Draft PR #132 is ready for user review. Merge remains a separate authorization. Release and production upgrade remain later independent boundaries; production follow-up must start read-only and prove per-account convergence and delivery confirmation rather than relying on service health alone.
