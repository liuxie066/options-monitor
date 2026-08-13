# Gateflow Slice 2 Review — deterministic projection performance baseline

- Gate: `code review -> fix -> re-review`
- Work unit: `data-storage-runtime-projection-p1`
- Slice: 2 of 2
- Initial review: `docs/reviews/code-review-20260813-152057.md`
- Re-review: `docs/reviews/code-review-20260813-154154.md`
- Artifact path: `docs/gateflow/data-storage-runtime-projection-p1/slice-2-review.md`
- Status: accepted; ready for Slice 2 local commit

## Delivered behavior

- Added a deterministic synthetic replay fixture with orthogonal current,
  event-history, current-state, and account-fanout axes.
- Split history into fixed-output and retained-closed-lot cases, each with a
  10,000-event floor.
- Measured the canonical stored-event projector and the existing SQLite
  `rebuild_position_lots_from_trade_events()` writer separately.
- Ran uninstrumented timing, `cProfile`, and `tracemalloc` in three separate
  child-process modes.
- Bound every worker result to the same fixture hash and exact canonical lot /
  diagnostic parity.
- Added writer SQL row/statement accounting plus temporary SQLite db/wal/shm
  before, peak-observed, post-replay, and checkpointed steady-state bytes.
- Atomically publishes exactly five versioned artifacts to an explicit absent
  or empty output directory; no production ledger is opened.

## Finding disposition

### DR-S2-01 — accepted — fixed

The first implementation could default missing timing p95 fields to zero and
therefore fabricate a reference-host pass. The parent now validates exact
warmup/repetition/run-label identity, non-profiled timing mode, complete sample
arrays, non-negative integer values, and recomputed summary statistics before
publication. Decision logic consumes only validated explicit values. Reduced
repetition smoke runs remain `not_evaluable` even when the host fingerprint
matches.

## Validation

```text
PYTHONPYCACHEPREFIX=/tmp/om-data-storage-p1-s2-full \
  ./.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_research_performance_baseline.py \
  tests/test_research_storage_baseline.py \
  tests/test_research.py \
  tests/test_ledger_event_codec.py \
  tests/test_ledger_sqlite_workflows.py

143 passed
```

```text
./.venv/bin/ruff check \
  src/application/research/performance_baseline.py \
  scripts/benchmark_data_storage_projection.py \
  tests/test_research_performance_baseline.py

All checks passed!
```

`compileall` and `git diff --check` passed.

The required non-acceptance history smoke completed with two 10,000-event
fixtures. On this host it observed approximately:

| Fixture | Projected lots | Writer p95 wall | Writer p95 CPU | Decision |
|---|---:|---:|---:|---|
| `history_10x.fixed_output` | 50 | 0.337 s | 0.337 s | `not_evaluable/non_acceptance_smoke` |
| `history_10x.retained_closed_lots` | 5,000 closed | 2.662 s | 2.651 s | `not_evaluable/non_acceptance_smoke` |

This is plumbing evidence, not the formal 5/30 reference-host judgment. It
already isolates the material amplification from retained closed lots and
global replace from the lower event-history-only cost.

## Documentation decision

`docs/AGENT_WIKI.md` now documents the command, artifact set, synthetic-only
safety boundary, reference-host fingerprint, smoke label, and honest
`lot_diff_publication=not_implemented` / combined `not_ready` decision.

## Residual risks

- Production distributions may differ from synthetic fixtures; assigned to a
  later separately authorized read-only calibration work unit.
- Full-table lot replacement remains in current production code; covered by
  the later approved Phase 3A implementation work unit.
- Formal 5/30 reference evidence is not a Slice 2 smoke artifact; covered by
  this work unit's aggregate validation gate.

## Completion status

All accepted Slice 2 findings are `已修复`; there are no blocking open
questions or unclassified residual risks. Next entry point:
`accepted slice commit`.
