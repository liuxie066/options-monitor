# Gateflow S5 Implementation — Migration, Acceptance, and Operator Evidence

- Work unit: `data-storage-runtime-projection-p3a`
- Slice: `S5`
- Gate: `implementation`
- Date: 2026-08-14
- Base: `S4@75d5dcc1`
- Status: implementation and DeepReview complete; formal evidence runs from
  the final clean S5 commit
- Artifact path: `docs/gateflow/data-storage-runtime-projection-p3a/s5-implementation.md`

## Outcome

S5 adds an explicit, fail-closed path to inventory, migrate, verify, benchmark,
activate, observe, and deactivate resumable position projection. Source
delivery performs none of those write transitions on a live store.

## Implementation

- Added read-only inventory and full/shadow verification with exact store,
  schema, implementation, source-commit, generation, event, and lot bindings.
- Added one transactional migration apply that rechecks the frozen inventory,
  backfills normalized columns, creates indexes, runs the full oracle, seeds a
  trusted checkpoint, and leaves checkpoint mode disabled.
- Added a high-risk activation transition that requires passing exact-host
  5×30 benchmark evidence plus passing current-store shadow evidence; added a
  bounded disable-only inverse that preserves all history/projection rows.
- Added structured status for source/head/checkpoint trust, K/space bounds,
  current fingerprint scope, and bounded process-local runtime telemetry.
- Extended the existing research benchmark with independent identical SQLite
  clones, real single/batch public facades, forced-full controls, exact parity,
  100-event/1-MiB rotation, SQL/call counts, WAL, wall/CPU, and allocation.
- Kept the 5,000 retained-lot case diagnostic with
  `retained_lots_10x_guarantee=false`; it cannot silently broaden activation.
- Routed non-ledger callers through `src.application.ledger.api` and refreshed
  the generated dependency graph.
- Documented authority versus cache, ordinary versus explicit full-history
  work, dry-run/read commands, write confirmations, and recovery boundaries.

## Correctness evidence

```text
S5 migration/benchmark/CLI focused set: 102 passed
ledger/research/CLI aggregate set: 297 passed
migration plus exact facade inventory: 20 passed
repository suite excluding sandbox-only localhost bind: 4734 passed, 10 skipped
localhost HTTP test outside sandbox: 1 passed
ruff: passed
compileall: passed
dependency graph: current; production_modules=577; cycles=0
git diff --check: passed
```

DeepReview evidence:

```text
initial review: docs/reviews/code-review-20260814-111520.md
fix record: docs/gateflow/data-storage-runtime-projection-p3a/s5-review-fix.md
final re-review: docs/reviews/code-review-20260814-112252.md
result: pass; all accepted findings fixed
```

## Performance evidence boundary

Smoke and preliminary exact-host runs showed roughly 94–96% p95 wall/CPU
improvement for the comparable single/batch writers, K=2 checkpoint retention,
and sub-100-ms rotation/fingerprint diagnostics. These are directional until
the final 5-warmup/30-repetition artifact is regenerated after the clean S5
commit. No source file may change after that run without regenerating its
source-bound shadow and acceptance manifests.

## Safety boundary

- No production apply, activation, runtime-store mutation, configuration or
  service change, notification, broker write, release, deployment, or deletion.
- Canonical `trade_events`, current `position_lots`, and permanent research
  history remain intact; checkpoints are bounded rebuildable cache only.
- The dirty `main` worktree remains untouched; S5 is isolated in this worktree.
