# Gateflow S2 Implementation — Shared Resumable Projection

- Work unit: `data-storage-runtime-projection-p3a`
- Slice: `S2`
- Gate: `implementation`
- Date: 2026-08-14
- Base: `S1@82e8d982`
- Status: accepted; DeepReview re-review passed with planned later-slice risks

## Outcome

S2 adds one shared domain transition path for full history and strict append-only
tails. Existing full-oracle APIs still return complete historical lots,
allocations, diagnostics and public fields. The new resumable state contains only
active lots and the minimum facts needed by future economics and publication.
Checkpoint persistence, selection and writer fast-path routing remain disabled.

## Implementation

- `project_trade_events()` now delegates to the shared transition engine while a
  separate full collector retains historical `close_event_ids` for compatibility.
- Added canonical `resumable_projection_state.v1` with exact schemas, duplicate-key
  rejection, finite-number checks, zero-diagnostic sentinel and active-lot-only
  payloads.
- Resumable lots retain scalar last-event identifiers, aggregate balances/economics,
  normalized fee provenance and only strategy facts consumed by later allocation.
  Arbitrary open-event payload and close-id vectors are excluded.
- Added a stateful public-field fold. Open initializes complete fields, adjust and
  partial close update them, and final close emits the exact touched row before the
  active state evicts it.
- Added a streaming stored-event import adapter. Full projection builds public raw
  payload context only for event types that can affect public fields; verification
  events do not create transitions or retained payload copies.
- Tail controls (`void`/`repair`), diagnostics, missing active targets, state-lot
  mismatch and invalid resumable state fail closed with an explicit full-replay
  reason.
- Updated the closed semantic implementation manifest and digest. The generated
  dependency graph was refreshed because the new domain module is a production
  architecture input.

## Compatibility evidence

Focused S2 plus adjacent ledger/publisher/SQLite/lifecycle regression set:

```text
183 passed in 2.33s
```

Serialization, semantic and generated-contract checks:

```text
focused resumable state tests: 17 passed
ruff: all checks passed
compileall: passed
dependency graph: current; production_modules=575; cycles=0
checked-in source inventory: passed
semantic implementation digest: 6fc3cd7918b66f6072b5f973bb613850eff4d8ed34dbacf6948e4fdffa39a2d6
git diff --check: passed
```

The deterministic suite covers full-versus-tail equality at every prefix, twelve
seeded multi-lot close sequences, fee remainder and explicit zero-fee provenance,
strategy attribution, multi-account views, verification events, invalid input,
publication field parity/order, final-close emit-and-evict, closed-lot targeting,
control invalidation, canonical decoding and state-object isolation.

Full repository suite:

```text
2 failed, 4657 passed, 10 skipped in 87.80s
```

The HTTP bind failure passed outside the restricted sandbox (`1 passed in 0.88s`).
The other failure reports the same five pre-existing research-to-ledger imports as
the accepted S1/main baseline.

## Time and space evidence

Local fixture: one active lot plus 10,000 verification events for full history;
100 verification events for the tail. Measurements use 5 warmups and 30 samples,
with a separate `tracemalloc` run:

```text
full 10,001 events: wall median 121.195 ms; wall p95 133.844 ms
                    CPU median 120.983 ms; CPU p95 133.833 ms
                    peak allocation 7.393 MiB
tail 100 events:    wall median 1.211 ms; wall p95 1.338 ms
                    CPU median 1.210 ms; CPU p95 1.338 ms
                    peak allocation 0.063 MiB; zero transitions
```

Bounded-state fixtures:

```text
one partial close state: 1,254 bytes
ninety-nine partial closes state: 1,254 bytes
50,000-byte opaque open payload -> 1,243-byte resumable state
one fully closed lot state == one hundred fully closed lots state
```

These measurements are diagnostic evidence for the S2 design boundary. Final
reference-host acceptance and retained-history scale gates remain owned by S5.

## Safety and boundary

- No checkpoint store, tail selection, runtime cutover, migration or activation.
- No live SQLite/config/service mutation, release/deploy, notification, broker write,
  data deletion or production data write.
- The original dirty `main` worktree remains untouched; all work is in the isolated
  `/private/tmp/options-monitor-data-storage-p3a-20260813` worktree.

## Residual risks

- Full oracle remains O(E) by design; S3/S4 must route only proven strict tails to the
  bounded path.
- Resumable publication state stores complete active public fields, so its size is
  O(active lots plus current field payload), not O(1).
- Full projection retains applied transitions and historical closed lots for exact
  compatibility; only tail/runtime checkpoint memory is bounded.
- Checkpoint durability, corruption handling, selection cadence, generation fencing
  and writer integration are intentionally deferred to S3/S4.
