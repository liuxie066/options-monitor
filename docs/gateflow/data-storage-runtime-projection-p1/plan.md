# Gateflow Plan — data-storage-runtime-projection-p1

- Gate: `plan`
- Work unit: `data-storage-runtime-projection-p1`
- Date: 2026-08-13
- Branch: `perf/data-storage-runtime-projection-p1`
- Base: `main@421591ddb5e298cda064ec414c8b87f62b0811b2`
- Goal artifact: `docs/gateflow/data-storage-runtime-projection-p1/goal-confirmation.md`
- Design contract: `docs/plans/data-storage-runtime-projection-phase1-contract-20260813.md`
- Prior design reviews: `docs/reviews/plan-review-20260813-092047.md`, `docs/reviews/plan-review-20260813-103939.md`
- Artifact path: `docs/gateflow/data-storage-runtime-projection-p1/plan.md`
- Status: `accepted after PlanReview re-review (pass-with-risks)`

## Goal, motivation, and success signals

Deliver Phase 1 only: a read-only inventory/capacity surface and a deterministic
benchmark harness that make later storage and projection choices measurable.
The implementation must answer four questions with machine-readable evidence:

1. Which current source paths read or rewrite lifetime ledger/lifecycle data,
   and which runtime roots retain CSV, base64, manifests, SQLite, run, and
   research artifacts?
2. How many rows and bytes exist per canonical SQLite table and bounded runtime
   root without decoding production payloads?
3. How does the existing canonical full replay behave at `current_scale` and
   the frozen growth axes, especially `history_10x`?
4. At the current unique-byte growth rate, when do warning/critical capacity
   boundaries become relevant, without moving or deleting anything?

Success is the five signals in the goal artifact: deterministic inventories,
synthetic fixtures, separate timing/CPU/allocation outputs, read-only research
capacity status, and an explicit full-replay gate result that cannot activate a
checkpoint.

## Non-goals and scope boundary

- No SQLite schema, trigger, migration, or production-data write.
- No lifecycle audit sidecar, lot-diff publisher, generation head, current
  decision projection, checkpoint, blob store, research generation, or cold
  backend implementation.
- No automatic file movement, eviction, cleanup, deletion, notification,
  broker call, service change, config mutation, release, deployment, or merge.
- No production payload export into fixtures or checked-in reports.
- No general observability framework and no third-party benchmark dependency.
- Phase 2 and later remain separate work units after this evidence is reviewed.

## Goal alignment

| Plan element | Confirmed goal / success signal |
|---|---|
| Slice 1 read-only inventory and capacity status | inventories; logical/physical bytes; growth and 90-day forecast |
| SQLite `mode=ro` and bounded root traversal | no authority mutation; reproducible safe metadata collection |
| Slice 2 deterministic fixtures and benchmark runner | current/growth axes; wall/CPU/allocation profiles; full-replay gate |
| JSON schemas, fixture identity, reference-host identity | comparable, auditable measurements |
| Focused tests plus filesystem/SQLite immutability assertions | read-only safety and deterministic evidence |

## Design-document alignment

- Contract sections 3–9 remain authority and complexity constraints; this work
  unit observes them but creates no new persistence authority.
- Section 10.1 supplies fixture axes, repetition rules, timing authorities, and
  separation of timing from profiling.
- Section 10.2 supplies the existing full-replay `history_10x` wall/CPU gate.
  Budgets for paths not implemented until later phases are recorded as
  `not_applicable`, never fabricated from a substitute benchmark.
- Section 11 Phase 1 requires inventory, metadata baseline, fixtures, benchmark
  JSON/profiles, and research-capacity status. Every slice below maps only to
  those deliverables.
- Section 7.4 capacity thresholds are reported as status/preview only. The
  implementation has no movement or deletion function.

## First-principles judgment and direct code evidence

1. `src/application/ledger/writer.py` repeatedly executes
   `list_trade_events()` before `replace_position_lots()`, so current trade
   writers have a real O(E) projection and O(current lots) replacement path.
2. `src/application/ledger/projection_verify.py` reads all events and stored
   lots to prove canonical projection equality; that remains the offline oracle.
3. `src/application/ledger/repository.py::SQLiteOptionPositionsRepository`
   initializes schema and WAL in its constructor, so Phase 1 inventory must not
   instantiate it for a production database.
4. `src/application/ledger/read_only_evidence.py` already establishes the safe
   SQLite contract: URI `mode=ro` plus `PRAGMA query_only=ON`. Phase 1 extends
   that pattern with aggregate SQL only, rather than decoding every JSON row.
   Because opening a live WAL database can touch shared-memory reader state,
   Phase 1 queries a stable temporary copy rather than the source files.
5. `src/application/runtime_paths.py` owns explicit runtime-root resolution.
   Inventory will traverse only caller-selected canonical subroots and will not
   crawl the repository parent, home directory, mounts, or symlink targets.
6. `src/interfaces/cli/research.py` is the existing offline advisory surface.
   A `research storage-baseline` subcommand is smaller and safer than a new
   top-level CLI or orchestration layer.
7. `domain/domain/ledger/projection.py` and
   `src/application/ledger/publisher.py` are the canonical deterministic replay
   path. `src/application/ledger/writer.py::rebuild_position_lots_from_trade_events()`
   is the existing end-to-end replay writer and must be measured separately
   from the pure projector on synthetic temporary SQLite.

## Public contracts and schemas

### Read-only CLI

Add:

```text
./om research storage-baseline \
  --runtime-root <root> \
  [--ledger-sqlite <path>] \
  [--history-report <prior-storage-runtime-baseline.json>]... \
  [--output <json-path>]
```

Behavior:

- without `--output`, emit the normal structured CLI response to stdout only;
- with `--output`, write exactly one local JSON report chosen by the operator;
- input roots must exist and resolve to directories; an explicit ledger path
  must be a regular file inside the runtime root unless
  `--allow-external-ledger` is explicitly present;
- traversal does not follow directory symlinks; symlinks are reported as
  metadata with zero traversed children;
- no `--write`, move, delete, compact, vacuum, checkpoint, or repair option
  exists.

The structured data schema is `storage_runtime_baseline.v1` and contains:

- `identity`: UTC observation time, runtime root, ledger path, Python/SQLite/
  platform/Git identity, and collection options;
- `source_inventory`: checked-in discovery rules and owned call-site
  classifications, plus stale-declaration and unclassified-discovery status;
- `sqlite`: db/wal/shm physical bytes, page metadata, and for an allowlisted
  table set only, row count plus `SUM(length(json_column))`; missing tables are
  explicit and SQL never selects JSON payload values. The query target is a
  stable temporary `db/wal/shm` snapshot, never the source SQLite files;
- `runtime_storage`: bounded-root file counts/bytes by root, class, suffix,
  month, and tier candidate; largest files expose runtime-relative paths only;
- `research_storage`: manifest-declared logical referenced bytes,
  declared-hash unique bytes and dedup ratio, physical bytes, unmanifested/
  unknown-unique bytes, root/generation counts, reference presence/size status,
  prior-report growth observations, 90-day forecast, and hot/warm/cold-candidate
  preview. `content_verification` is explicitly `not_performed`; a prior
  verifier receipt may be reported as historical evidence but is not refreshed
  or relabeled by this command;
- `thresholds`: deterministic warning/critical facts from contract section 7.4;
- `safety`: query-only SQLite, no-follow traversal, payload-content reads, and
  mutation operations, with expected values `true`, `true`, `0`, and `0`.

`--output` is a local evidence write, not a runtime authority. It uses atomic
temp-file replace, refuses a path inside an inventoried runtime root by default,
and tests prove the runtime tree and SQLite sidecar set are unchanged.

### Checked-in source inventory

Add `docs/architecture/data-storage-runtime-source-inventory.v1.json`. Each
entry binds an owner category, file, symbol or literal pattern, operation
(`read`, `write`, `encode`, `decode`, `root`, `manifest`), history dimension,
hot/offline classification, and later contract phase. The same file contains
versioned discovery rules for a bounded AST/text scan of `domain/`, `src/`, and
`scripts/`. The collector validates every declaration and fails inventory when
a discovered production match is unclassified. Explicit ignores require a
reason and owner. It does not claim runtime call frequency or mathematical
coverage of APIs outside the versioned discovery vocabulary.

The manifest covers at least:

- ledger event reads and lot replacement writers;
- lifecycle evidence/case/allocation/source-consumption readers and writers;
- decision snapshot/context consumers;
- required-data CSV and base64 producers/consumers;
- run, account, shared, state, research, Shadow Replay, and archive roots;
- manifest/root readers that protect replay evidence.

### Benchmark artifacts

The repository-local runner is:

```text
./.venv/bin/python scripts/benchmark_data_storage_projection.py \
  --baseline <storage-runtime-baseline.json> \
  --scenario all \
  --output-dir <empty-local-dir>
```

It accepts metadata only from the baseline. If omitted, deterministic safe
defaults are used and the manifest records `dimension_source=defaults`.
It writes into an explicit output directory only:

- `fixture-manifest.json` (`data_storage_projection_fixture.v1`);
- `timing.json` (`data_storage_projection_timing.v1`);
- `cpu-profile.json` (`data_storage_projection_cpu_profile.v1`);
- `allocation-profile.json` (`data_storage_projection_allocation_profile.v1`);
- `decision.json` (`data_storage_projection_gate_decision.v1`).

Scenarios are `current_scale`, `history_10x`, `current_state_10x`, and
`account_fanout`. Fixture generation is seeded, uses only synthetic canonical
trade-event payloads, clamps hostile baseline dimensions to documented safe
limits, and records requested versus effective dimensions. A clamp that prevents
the contract-required axis makes that axis `not_evaluable`, never `pass`.
`history_10x` has two explicit subcases, each with at least 10,000 events:

- `fixed_output` combines a fixed deterministic open-lot set with valid
  read-only `verification` events. Projected lot/view/allocation counts remain
  fixed, isolating event load/decode/validation/sort overhead.
- `retained_closed_lots` uses deterministic open/close pairs. Its closed lots
  and allocations intentionally grow because that is the current canonical
  projection and persistence behavior; it exposes replay plus global-replace
  coupling instead of pretending the state is fixed.

The artifact records event, projected-lot, open-lot, risk-view, and allocation
counts for every scenario. It labels `fixed_output` as a complexity-isolation
fixture rather than a production event-mix sample. On a comparable reference
host, `existing_full_replay_writer` passes the history component only when both
subcases meet the frozen wall/CPU threshold.

Every replay scenario reports two measured components on synthetic data:

- `projector_only` invokes the canonical stored-event codec/projector;
- `existing_full_replay_writer` uses a temporary SQLite ledger and calls
  `rebuild_position_lots_from_trade_events()`, thereby including SQLite event
  loading/decoding, publishability checks, and the current global lot replace.

Both components bind the same canonical lots/diagnostic fingerprint. The writer
component additionally records temporary `db + wal + shm` bytes before, peak,
and after the replay. Because Phase 3A diff publication does not yet exist, the
gate decision reports three separate facts:

- `projector_only`: diagnostic evidence, never sufficient for writer pass;
- `existing_full_replay_writer`: measured against p95 wall <= 2 s and CPU <= 1.5 s
  on an explicitly matching reference-host profile;
- `lot_diff_publication`: `not_implemented`, making the combined Phase 3A gate
  `not_ready` rather than a false pass.

Timing uses five warmups and thirty measured repetitions with
`perf_counter_ns()` and `process_time_ns()` and no profiler. CPU profile uses
`cProfile` in a separate invocation. Allocation profile uses `tracemalloc` in a
separate invocation. Peak RSS is reported when `resource.getrusage` exists.
Non-reference hosts report timing but set absolute decisions to
`not_comparable`. The runner never opens a production SQLite file.

## State transitions and error handling

This work unit creates evidence files only when an explicit output path is
provided. It has no persistent runtime state machine.

- Missing runtime root or invalid explicit ledger path: `INPUT_ERROR`, no scan.
- Missing ledger: overall report remains usable with SQLite status `missing`.
- SQLite source snapshotting records `(exists, size, mtime_ns, inode)` before
  and after copying the exact `db/wal/shm` set, retries at most three times when
  the tuple changes, and opens only the temporary copy. If no stable copy is
  obtained, or the copy is corrupt/unreadable, SQLite status is
  `data_unavailable`; filesystem inventory continues and overall status is
  `partial_data`. This is a bounded coherence check, not an atomicity claim.
- Unknown table/schema revision: only allowlisted present tables are counted;
  unknown tables are reported by name/count metadata but never dynamically
  interpolated into content queries.
- Manifest parse failure, missing reference, or declared-size mismatch becomes
  `data_unavailable` or `critical`; same-size content tampering remains
  `not_verified` because this command does not hash payloads.
- Prior reports are accepted only when schema, resolved runtime-root identity,
  and observation ordering match. With fewer than two compatible observations,
  forecast status is `insufficient_history` and forecast bytes are null.
- Output path collision: refuse unless `--overwrite` is explicitly supplied;
  overwrite affects only the named local report path.
- Benchmark output directory must be absent or empty. Partial artifacts use a
  temporary sibling directory and are atomically published only after all
  schemas validate.

## Implementation slices

Two slices are the smallest behavior-oriented split. Slice 1 independently
delivers safe operational evidence. Slice 2 independently answers the
performance question using that metadata. Splitting source inventory, SQLite,
and filesystem accounting into separate slices would add gate cost without a
useful operator outcome; combining profiling with the read-only collector would
make filesystem safety regressions harder to isolate from CPU-heavy fixtures.

### Slice 1 — read-only inventory and capacity status

**Objective:** Add one bounded, payload-free command that inventories source
ownership, SQLite metadata, runtime storage, research roots, growth, forecast,
and thresholds without mutating the runtime root.

**Expected outcome:** `./om research storage-baseline --runtime-root <fixture>`
returns `storage_runtime_baseline.v1`; an optional report is atomically written
outside the runtime root; before/after filesystem and SQLite hashes match.

**Allowed files/modules:**

- `src/application/research/storage_baseline.py` (new)
- `src/interfaces/cli/research.py`
- `docs/architecture/data-storage-runtime-source-inventory.v1.json` (new)
- `tests/test_research_storage_baseline.py` (new)
- `tests/test_research.py` for narrow CLI dispatch assertions only
- `docs/AGENT_WIKI.md` for the new public read-only command
- Gateflow artifacts for this work unit

**Exact allowed changes and data flow:**

1. CLI parses explicit roots/options and delegates to
   `collect_storage_runtime_baseline()`; CLI formatting remains the existing
   `build_response()` JSON contract.
2. Collector resolves/validates bounded paths, loads and validates the checked-
   in source inventory, executes its bounded discovery rules and rejects stale
   or unclassified matches, copies the exact ledger `db/wal/shm` set into a
   temporary directory with bounded before/after source-stat validation, opens
   only that copy via URI `mode=ro` plus `query_only=ON`, performs allowlisted
   aggregate SQL, traverses fixed runtime
   subroots with `os.scandir(..., follow_symlinks=False)` semantics, resolves
   manifest-declared references without opening referenced payload bodies,
   classifies tiers, validates compatible prior reports, computes observation
   deltas/forecast/threshold facts, and returns one dict.
3. Optional output validates it is outside runtime root, serializes canonical
   JSON to a sibling temp file, fsyncs, and atomically replaces only the named
   report.
4. Errors are classified as specified above; partial sources do not fabricate
   zero values.

**Invariants:** no production repository constructor or source SQLite
connection; no SQLite PRAGMA that can checkpoint/change source journal state;
no followed symlink; no payload JSON/CSV/base64
decode or hash; manifest metadata may be parsed but never upgrades content to
`verified`; no absolute runtime path inside largest-file/source entries except
the single masked/declared root identity; no mutation callback exists.

**Non-goals:** no content-addressed store, generation manifest writer, cold
backend, cleanup preview execution, or live production collection.

**Validation:**

```bash
PYTHONPYCACHEPREFIX=/tmp/om-data-storage-p1-s1 ./.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_research_storage_baseline.py tests/test_research.py
./.venv/bin/python -m compileall -q src/application/research src/interfaces/cli/research.py
git diff --check
```

Expected assertions include deterministic schema, aggregate-only SQLite trace,
WAL/SHM/database byte identity, runtime-tree identity, no-follow symlinks,
stale/unclassified source discovery, manifest-declared dedup, missing/size-
mismatch versus same-size-unverified behavior, incompatible/out-of-order prior
reports, stable-copy retry/exhaustion under a simulated concurrent writer,
two/three-observation forecast boundaries, alert-with-zero-action behavior, and
CLI output rules.

**Completion signal / stop condition:** complete when focused tests and safety
assertions pass. Stop if accurate logical-byte accounting requires reading raw
payload contents or a persistence authority not named by the design contract.

### Slice 2 — deterministic fixture and performance baseline harness

**Prerequisite:** accepted Slice 1 commit.

**Objective:** Generate synthetic fixture axes and reproducible uninstrumented,
CPU, and allocation evidence for the existing canonical replay path, with an
honest readiness decision.

**Expected outcome:** one runner produces the five versioned artifacts; repeated
runs with the same seed have identical fixture/content hashes; timing and
profiles are separate; `history_10x` contains at least 10,000 events and reports
full-replay result without claiming diff/checkpoint readiness.

**Allowed files/modules:**

- `src/application/research/performance_baseline.py` (new)
- `scripts/benchmark_data_storage_projection.py` (new, thin wrapper)
- `tests/test_research_performance_baseline.py` (new)
- `docs/AGENT_WIKI.md` for the benchmark command and interpretation
- Gateflow artifacts for this work unit

**Exact allowed changes and data flow:**

1. Load and schema-check optional Slice 1 metadata; discard paths and any
   unexpected payload-like fields before deriving dimensions.
2. Build canonical synthetic event sequences with a fixed seed and bounded
   low/median/high entropy metadata. Preserve fixed current state in
   `history_10x.fixed_output`; separately model retained closed-lot coupling in
   `history_10x.retained_closed_lots`; scale current state and accounts only in
   their named orthogonal scenarios.
3. Measure `project_stored_trade_events_to_position_lots()` and, on a synthetic
   temporary ledger, `rebuild_position_lots_from_trade_events()` as distinct
   components. Record output fingerprint/count/diagnostic hash and assert exact
   equality; for the writer also capture SQLite db/wal/shm bytes and the known
   global-replace behavior.
4. Run timing, cProfile, and tracemalloc in separate child-process modes of the
   same script. The parent validates and atomically publishes the complete
   artifact set to an empty explicit directory.
5. Evaluate reference-host comparability and emit `pass`/`fail`,
   `not_comparable`, or `not_implemented` per gate component. Never recommend or
   activate checkpoint/tiering behavior.

**Invariants:** no production ledger/runtime read; deterministic events and
hashes; no profiler in timing; no wall/CPU threshold decision on a mismatched
host; no projector-only result presented as writer pass; no benchmark
dependency; output bounded to explicit directory.

**Non-goals:** no lot diff writer benchmark pretending to be implemented, no
Phase 2 audit-sidecar benchmark, no checkpoint benchmark, and no optimization
of the projector during measurement.

**Validation:**

```bash
PYTHONPYCACHEPREFIX=/tmp/om-data-storage-p1-s2 ./.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_research_performance_baseline.py tests/test_research_storage_baseline.py
./.venv/bin/python scripts/benchmark_data_storage_projection.py \
  --scenario history_10x --warmups 1 --repetitions 2 \
  --output-dir /tmp/om-data-storage-p1-smoke
./.venv/bin/python -m compileall -q src/application/research scripts/benchmark_data_storage_projection.py
git diff --check
```

The smoke run uses reduced repetitions only for plumbing validation and must
label itself `non_acceptance_smoke`; acceptance evidence uses 5/30. Tests assert
fixture hashes, fixed-output axis independence, retained-closed-lot counts,
canonical output equality, profiler
separation, reference-host gating, hostile-baseline clamping, atomic output,
writer/projector parity, temporary SQLite byte accounting, and
`lot_diff_publication=not_implemented`.

**Completion signal / stop condition:** complete when tests and smoke run pass
and a 5/30 reference artifact can be generated within the recorded resource
budget. Stop if 10,000 canonical events exceed available local resources or if
the current projector cannot consume a deterministic contract-valid sequence;
do not weaken the fixture or threshold silently.

## Aggregate validation

After both accepted slice commits:

```bash
PYTHONPYCACHEPREFIX=/tmp/om-data-storage-p1-aggregate ./.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_research_storage_baseline.py \
  tests/test_research_performance_baseline.py \
  tests/test_research.py \
  tests/test_ledger_event_codec.py \
  tests/test_ledger_sqlite_workflows.py
./.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_research.py
./.venv/bin/python -m compileall -q domain src scripts tests
./.venv/bin/python scripts/benchmark_data_storage_projection.py \
  --scenario all --output-dir /tmp/om-data-storage-p1-acceptance
git diff --check
```

The final benchmark uses the default 5 warmups/30 repetitions and records
whether the current host is comparable; timing failure is an evidence outcome,
not a reason to alter the test. The aggregate review also checks `git status`,
the exact source-inventory validation result, absence of forbidden mutation
symbols in the new modules, and that the original shared worktree remains
unchanged.

## Documentation decision

- Check in the confirmed design contract and its two prior review artifacts so
  the implementation and later work units have a durable authority boundary.
- Add Gateflow artifacts under
  `docs/gateflow/data-storage-runtime-projection-p1/`.
- Update `docs/AGENT_WIKI.md` with the read-only baseline command, benchmark
  invocation, output schemas, and interpretation boundaries.
- Do not update release notes or `CHANGELOG.md`: this work unit adds an offline
  diagnostic/research surface and does not release it.

## Risks and residual-risk classification

| Risk | Classification / handling |
|---|---|
| Static source inventory can drift | Fixed in Slice 1 by stale-locator plus unclassified-discovery failure; novel APIs outside the vocabulary remain an aggregate-review residual risk |
| Filesystem scan races concurrent writers | Fixed in Slice 1 by descriptive observation timestamps and partial/coherence status; no atomic snapshot claim |
| Logical dedup/content integrity cannot be known for unmanifested or unhashed files without reading payloads | Fixed in Slice 1 by separating declared hashes, presence/size, historical verifier status, and unknown/unverified bytes |
| Synthetic distributions differ from production | Assigned to later explicit read-only sampling; baseline identity exposes metadata source/defaults |
| Projector passes while current SQLite writer fails | Fixed in Slice 2 by measuring both and forbidding projector-only writer pass |
| Closed-lot retention couples history to publication size | Fixed in Slice 2 by reporting fixed-output and retained-closed-lot subcases separately and requiring both for the comparable writer result |
| Current writer passes but future diff publication is absent | Fixed in Slice 2 by separate component status and combined `not_ready` |
| Full replay fails `history_10x` | Covered by the already-approved later focused checkpoint planreview; no checkpoint work in this unit |
| Concrete cold backend/restore cost is unknown | Assigned to later Phase 7 work unit requiring reviewed backend contract |
| Existing admitted evidence and duplicate run copies continue growing | Covered by later approved Phase 2/6/7 work units; this unit only quantifies them |

No residual risk is unclassified. A discovery that requires payload reads,
schema mutation, runtime write, or a new authority triggers the Gateflow stop
condition instead of expanding this work unit.

## No overdesign and no goal drift

Two small modules reuse existing boundaries: Research CLI/runtime-root ownership,
SQLite query-only access, and the canonical ledger projector. Versioned JSON is
necessary because later gates consume the evidence; it is not a new database or
service. The source inventory is data, not a second policy engine. The runner
uses the standard library only. Every output maps directly to a confirmed Phase
1 success signal, and every later optimization remains explicitly absent.

## Completion report format

Final closeout reports accepted commits, changed files, exact focused/full and
benchmark results, output schemas, documentation changes, all review findings
and final status, residual risks/owners, Draft PR URL, confirmation that no
runtime data was changed, and the next entry point: user review/merge followed
by a separately confirmed Phase 2 work unit.

## PlanReview finding decisions

- `PR-P1-01`: accepted; fixed by splitting manifest-declared identity,
  presence/size, historical verifier evidence, and current content verification.
- `PR-P1-02`: accepted; fixed by deriving growth only from compatible prior
  baseline reports and returning `insufficient_history` otherwise.
- `PR-P1-03`: accepted; fixed by measuring projector-only and the real existing
  replay writer on synthetic temporary SQLite as separate components.
- `PR-P1-04`: accepted; fixed by versioned bounded discovery rules plus failure
  on stale declarations and unclassified production matches.
- `PR-P1-05`: accepted; fixed by separate fixed-output and retained-closed-lot
  history subcases with explicit event/lot/view/allocation cardinalities.

## Next entry point

`accepted plan commit`
