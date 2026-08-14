# Gateflow S4 Implementation — Preflight and Writer Runtime Integration

- Work unit: `data-storage-runtime-projection-p3a`
- Slice: `S4`
- Gate: `implementation`
- Date: 2026-08-14
- Base: `S3@2127d0ac`
- Status: accepted after DeepReview fixes, full-suite fixes and final re-review
- Artifact path: `docs/gateflow/data-storage-runtime-projection-p3a/s4-implementation.md`

## Outcome

S4 routes every trade-event writer through the common transactional projection
runtime and removes duplicate full-history replay from eligible append-safe
preflight/write operations. Checkpoint mode remains disabled by default; no live
store was migrated or activated.

## Implementation

- Exposed one caller-owned SQLite transaction entrypoint. It preserves
  input-order idempotency flags and never commits or rolls back its caller.
- Routed ordinary open/close/expire/assignment/exercise/adjust/verification
  writers to fast-if-safe mode; control, lifecycle correction, Combo identity,
  reconciliation, rebuild and bootstrap paths remain explicitly force-full.
- Replaced full-ledger candidate id maps with primary-key batch lookups and
  routed canonical SQLite FIFO/explicit lot resolution through indexed active
  and record-id reads.
- Added checkpoint-backed read-only append preflight and preserved the existing
  current-history versus candidate-error codes and payloads.
- Added a read-only full-versus-resumed shadow comparison with bounded mismatch
  ids.
- Added an AST inventory that leaves direct `upsert_trade_event()` ownership
  only in the runtime and forbids global lot replacement.
- Preserved adapter/subclass read contracts: bounded repository-specific reads
  are used only for the canonical SQLite implementation.
- Refreshed the generated dependency graph for the final import/test inventory.

## Correctness evidence

```text
focused preflight/runtime after review fix: 54 passed
expanded S4/ledger/maintenance/manual-close suite: 349 passed
final S4/runtime/publication set: 101 passed
facade inventory: 3 passed
ruff: passed
compileall: passed
dependency graph: current; production_modules=576; cycles=0
git diff --check: passed
```

DeepReview evidence:

```text
initial review: docs/reviews/code-review-20260814-093216.md
final re-review: docs/reviews/code-review-20260814-095029.md
result: pass; all accepted findings fixed
```

Final repository suite:

```text
2 failed, 4707 passed, 10 skipped, 5 warnings in 91.99s
```

The localhost HTTP test is sandbox-only and passed outside the sandbox
(`1 passed in 0.97s`). The other failure is the same five pre-existing
research-to-ledger internal imports present at base `5930e5ce`; none of those
files is changed by S4.

## Directional time evidence

A 10,000-event local fixture exercised the same public close preflight and
single-writer facade after warmup:

```text
checkpoint-backed path: about 20.52 ms
forced-full path: about 1.097 s
directional ratio: about 53x
```

Under `cProfile`, the same path measured about 25.13 ms versus 3.258 s; profiler
overhead makes those absolute values non-acceptance evidence. The bounded path
is comfortably below the 500 ms threshold directionally. S5 still owns the
frozen 5-warmup/30-repetition wall/CPU, allocation, SQL-row, WAL, rotation and
reference-host acceptance run.

## Safety boundary

- No migration apply, checkpoint activation, production SQLite/config/service
  mutation, notification, broker write, release, deployment or deletion.
- Full replay remains the recovery/audit oracle and every unsafe tail falls
  back or fails closed.
- The dirty `main` worktree remains untouched; S4 is isolated in this worktree.
