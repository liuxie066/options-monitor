# Gateflow Goal Confirmation — data-storage-runtime-projection-p1

- Gate: `goal confirmation`
- Work unit: `data-storage-runtime-projection-p1`
- Base: `main@421591ddb5e298cda064ec414c8b87f62b0811b2`
- Branch: `perf/data-storage-runtime-projection-p1`
- User confirmation: 2026-08-13, after the Phase 1 boundary and isolated-worktree proposal
- Status: `pass`

## Goal and motivation

Produce the read-only inventories, deterministic fixtures, benchmark baselines,
profiles, and research-capacity status needed to decide which later storage and
runtime-projection optimizations are justified. The work unit must expose the
current time, CPU, memory, call-count, and physical-storage costs without
changing their persistence authorities.

## Success signals

- Writer, reader, CSV/base64, run-root, lifecycle-evidence, and research-root
  inventories are machine-readable and reproducible.
- Current-scale and growth-axis fixtures are deterministic and contain no live
  account or broker data.
- Benchmark output separates uninstrumented wall-time measurement from CPU and
  allocation profiling and records the reference environment.
- Research storage status reports logical versus unique physical bytes,
  deduplication ratio, growth observations, a 90-day forecast, and candidate
  hot/warm/cold classification without moving or deleting files.
- The Phase 1 artifact can decide whether the existing full-replay writer meets
  the frozen `history_10x` gate; it does not activate a checkpoint path.

## Scope boundary

Allowed: read-only source/runtime inventory logic, synthetic fixtures,
benchmark/profile tooling, deterministic report schemas, tests, and operator
documentation for those surfaces.

Excluded: schema changes, SQLite triggers, lifecycle sidecar implementation,
position/decision projection implementation, checkpoint activation, shared
blob cutover, cold-storage backend activation, migration, eviction, deletion,
runtime-config changes, notifications, broker writes, and production-service
changes.

## Direct code evidence

- `src/application/ledger/writer.py` repeatedly calls `list_trade_events()` and
  republishes via `replace_position_lots()`, so the full-replay writer cost is a
  real measurement target.
- `src/application/ledger/projection_verify.py` reads all trade events and all
  current lots for canonical verification.
- `src/application/positions/context_builder.py` consumes a decision snapshot
  containing account lots, lifecycle, assigned-stock, and combo facts.
- `src/interfaces/cli/research.py` and `src/application/research/` already own
  offline evidence collection, making a read-only inventory/benchmark surface
  preferable to a new orchestration layer.

## First-principles judgment

The work unit is justified because later optimization choices depend on which
cost scales with historical events, current positions, account fan-out, or
unique stored content. Measuring those axes is lower-risk than implementing a
checkpoint or storage tier based on aggregate directory size alone.

## Parsimony

This unit will not introduce a general monitoring framework, a new authority,
an incremental event projector, or a cold-storage service. It adds only the
evidence needed by the already-confirmed implementation gates.

## Blocking open questions

None. The user confirmed the Phase 1 boundary and creation of an isolated
worktree. Later checkpoint, movement, eviction, and deletion decisions remain
separately gated.

## Residual risks

- Production-scale distributions may differ from synthetic fixtures. This work
  unit records reference-host and fixture identity so later read-only production
  sampling can be separately authorized if required.
- Phase 2 and later implementation remain separate work units/gates and are not
  implied by this goal confirmation.

## Next entry point

`plan`
