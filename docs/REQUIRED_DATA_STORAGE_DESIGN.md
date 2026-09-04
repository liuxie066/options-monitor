# Required Data Storage Design

> Status: storage contract and the first shared projection-validation optimization
> are implemented. The scheduled Tick latency design passed Planreview with
> residual risks and is frozen for implementation. Commit, release, deployment,
> and any production history cleanup remain separate operator actions.

## Goal

Stop scheduled required-data runs from durably retaining the same provider
payload as a canonical blob, loose raw JSON, loose CSV, and inline base64.
Keep the existing CSV projection and fail-closed evidence contract while making
the canonical blob the only durable payload representation for new sealed
entries.

### Success signals

- A new scheduled ready quote receipt contains `scan_blob_ref` and omits
  `raw_json_base64` and `required_data_csv_base64`.
- After manifest seal and cleanup durability eligibility succeed, loose raw JSON
  and CSV shadows for new compact canonical entries are absent.
- The shared frozen-batch facade, Shadow Replay, and archive dataset marking
  continue to consume sealed blob bytes without a legacy read.
- Failed, legacy-only, mismatched, unsafe, or unsealed entries are never
  deleted.
- Re-entry is idempotent, and a cleanup failure cannot change candidate,
  notification, or financial-state outcomes.

## Non-goals

- Do not replace the required-data CSV projection with Parquet, SQLite, Arrow,
  or another schema.
- Do not change quote freshness, option normalization, multiplier enrichment,
  coverage, candidate filtering, ranking, or strategy policy.
- Do not remove read-only support for historical receipts without a blob ref.
- Do not migrate or delete existing production history as part of the source
  change.
- Do not add a background cleaner, database table, configuration switch, new
  retention policy, or recovery service.
- Do not extend this trusted local run-root cleanup into protection against a
  hostile same-user process racing file replacement.
- Do not couple this source contract to release, deployment, or production
  mutation.

## Current facts and constraints

The scheduled tick creates a run-scoped producer workspace at
`output_runs/<run_id>/required_data/{raw,parsed}`. Prefetch, coverage,
multiplier enrichment, quote-cache validation, and receipt publication use
those files before sealing.

Before this change, `publish_required_data_quote_snapshot()` published a
deterministic gzip scan blob when `runtime_root` was available, but the same
receipt also embedded raw JSON and CSV as base64 and retained their loose paths.
The terminal
`required_data_snapshot_manifest.json` binds the exact blob ref for ready
entries.

Blob-backed readers already treat inline base64 and loose shadow files as
optional comparisons. A present shadow must match the blob; an absent shadow
does not trigger a legacy fallback. A receipt without `scan_blob_ref` still
requires both inline bytes and matching loose files. Corrupt or missing
canonical blobs fail closed and must never fall back to legacy data.

Formal account scans resolve one `FrozenRequiredDataBatch` and materialize CSV
bytes into in-memory frames. Close Advice and Wheel consume the resolved
snapshot or batch path; Shadow Replay and archive marking already prefer the
sealed manifest/blob. Direct loose-CSV readers remain valid only for pre-seal
producer work and explicit legacy/manual flows.

The 2026-08-29 read-only production inventory found 1,254 blob-backed and 158
legacy-only ready entries. Run-scoped required-data occupied about 1.0 GiB,
while the shared canonical blob store occupied about 13 MiB. These figures are
design-time evidence, not a runtime threshold or retention contract.

The checked-in metadata-only p99 fixture drives the deterministic canonical
profile without reading production payloads. Formal evidence exercises the
implemented receipt, durability, cleanup, and blob-only resolver path; absolute
timing remains host-specific diagnostic evidence.

## Chosen design

“Compatibility retirement” applies to new writes, not historical reads.

### 1. Compact canonical receipts

`src/application/opend_symbol_outputs.py` remains the quote-receipt producer.
It first validates the loose producer files, reads each payload once, and
publishes the canonical blob from those exact bytes.

- When blob publication succeeds, the receipt keeps `scan_blob_ref`, fetch
  contract, policy, hashes, timestamps, and the existing provenance relpaths,
  but omits both inline base64 fields.
- When no `runtime_root` is supplied, the existing legacy/manual receipt keeps
  inline base64 and loose-file binding unchanged.
- Blob publication failure remains fatal for the scheduled canonical path; it
  does not silently produce a legacy scheduled receipt.

Keeping the relpaths preserves receipt and manifest compatibility. For a
blob-backed sealed entry they name the former producer shadows and are not a
promise that those files remain durable.

### 2. Establish cleanup durability eligibility

`seal_required_data_snapshot()` keeps its existing authority: validation or
readback failure remains fatal, while a valid terminal manifest remains usable
by account consumers. A separate non-fatal helper decides only whether shadow
deletion is safe. After a new seal or recovery load succeeds, that helper:

1. existing validators bind every ready entry to its exact quote payload,
   receipt, canonical blob ref, and optional Close Advice plan;
2. the validated quote payload, receipt, optional bound plan, and their required
   parent directories are flushed without following symlinks;
3. the already published terminal manifest and its parent directory are
   flushed;
4. the manifest bytes and hash are confirmed unchanged from the successful seal
   or recovery load.

The canonical blob writer flushes the blob and every directory needed to reach
it. Cleanup flushes receipt, payload, optional plan, and manifest from the
runtime root so their complete directory chains are also durable. Any failure
preserves every shadow and records degraded storage maintenance without
changing manifest authority, prefetch state, barrier reason, or notification
eligibility. The widely used generic `atomic_write_json()` semantics remain
unchanged.

This check is intentionally scoped to artifacts bound by this manifest. It is
not a whole-repository persistence refactor.

### 3. Retire verified shadows after sealing

`src/application/required_data_snapshot.py` remains the owner of manifest and
sealed-entry validation. A narrow cleanup operation accepts the already
validated manifest returned by seal or recovery load, plus the required-data
root; it does not perform another full manifest/blob validation pass. It then:

1. selects only `status=ready` entries whose validated compact receipt has a
   valid `scan_blob_ref` and neither inline base64 field;
2. derives exactly `raw/<symbol>_required_data.json` and
   `parsed/<symbol>_required_data.csv`, rejecting any contradictory relpath;
3. resolves one symbol's raw JSON and CSV bytes from the canonical blob, retires
   that pair, then releases it before loading the next symbol;
4. opens each path component and leaf through directory descriptors without
   following symlinks, requires a regular single-link file, and hashes the open
   leaf;
5. immediately before unlink, compares the basename's `st_dev`, `st_ino`,
   `st_size`, and link count with the open leaf, then unlinks through the same
   parent descriptor only when identity and bytes still match;
6. treats an already absent file as an idempotent success.

Unsafe paths, symlinks, non-regular files, content mismatches, legacy-only
entries, and failed entries are preserved and reported. Empty `raw` and
`parsed` directories may remain; removing directories has no material benefit
and broadens the destructive surface.

The low-level compare-and-unlink operation belongs beside the existing
no-follow shadow comparison in `required_data_blobs.py`; no new module or class
is needed. Cleanup runs only after all producer workers have completed and the
run root has no other legitimate writer. A hostile same-user rename race is
outside this work unit; any detected identity change or unavailable platform
primitive preserves the file.

### 4. Invoke at new-seal and recovery boundaries

`src/application/tick_account_execution.py` invokes the same helper after a new
manifest is sealed and after the `prefetch_done` recovery path successfully
loads an existing terminal manifest. Each entry first establishes cleanup
durability eligibility, then retires shadows only when eligible. The recovery
entry closes the crash window between seal and cleanup without adding a
scheduler or state machine. Historical dual-output and legacy-only receipts are
not cleanup targets.

The orchestration path first fixes manifest path, hash, status, and prefetch
state. Cleanup then runs inside its own non-fatal exception boundary and cannot
change those fields, the barrier reason, or notification eligibility. It
returns only `removed_files`, `removed_bytes`, `absent_files`, and
`failed_files`; `failed_files > 0` means degraded. One dedicated
`required_data_shadow_cleanup` event is emitted on the existing audit and runlog
surfaces with `trigger=new_seal|recovery` and those four scalars. This event is
observability, not a new state machine, dataclass, receipt, state file, schema,
or configuration key.

### Data flow

```text
run-scoped raw/CSV producer workspace
  -> canonical scan blob publication
  -> compact quote receipt with blob ref
  -> terminal manifest seal and readback
  -> cleanup durability eligibility
  -> verified shadow retirement
  -> FrozenRequiredDataBatch
  -> in-memory CSV frame consumers
```

### State and failure behavior

| State or failure | Required behavior |
|---|---|
| Blob publication fails | No canonical receipt; existing fail-closed behavior; no cleanup |
| Receipt or manifest validation fails | Entry fails or sealing fails as today; no cleanup for that entry |
| Cleanup durability eligibility fails | Preserve every shadow, record degraded cleanup, and continue the sealed account pipeline |
| Manifest is partial | Clean only ready compact blob-backed entries; preserve failed entries |
| Shadow is already absent | Count as idempotently retired |
| Shadow is unsafe, changed, or unreadable | Preserve it and record a degraded cleanup result |
| Cleanup raises or partially fails | Preserve undecided files, record degraded evidence, continue the sealed account pipeline |
| Recovery loads a compact terminal manifest | Re-establish cleanup eligibility, then retry idempotent cleanup before account consumers |
| Canonical blob later becomes corrupt or missing | Consumer fails closed; never fall back to any remaining legacy shadow |
| Receipt has inline base64 or no blob ref | Preserve inline bytes and loose files; historical reader remains available |

Cleanup is storage maintenance after a validated manifest and a successful
cleanup-specific durability check, not decision authority. It cannot suppress a
valid scan or notification. The dedicated event and existing storage baseline
make persistent cleanup failures visible as capacity risk.

## Owners and contracts

| Owner | Contract |
|---|---|
| `src/application/opend_symbol_outputs.py` | New blob-backed receipts omit inline base64; manual receipts without a runtime root retain it |
| `src/application/required_data_blobs.py` | Blob publication is durable and shadow retirement uses the verified no-follow primitive |
| `src/application/required_data_snapshot.py` | Cleanup is manifest-bound, non-fatal, payload-bounded, and returns four scalar counts |
| `src/application/tick_account_execution.py` | New-seal and recovery cleanup are isolated and emit one audit/runlog event |
| `scripts/benchmark_required_data_scan_blobs.py` | Canonical formal evidence covers compact receipt, seal, durability, cleanup, and blob-only resolution |
| `docs/AGENT_WIKI.md` | Defines canonical durable payloads and the remaining legacy read boundary |
| `docs/SHADOW_REPLAY_RUNBOOK.md` | Modern archived runs mark from manifest/blob without parsed CSV |
| `src/interfaces/cli/research.py` | Archive help names sealed required-data instead of a parsed-CSV requirement |

No domain strategy module, Candidate Engine contract, public command, runtime
configuration, or blob/manifest schema changes.

## Rejected alternatives

### Remove only inline base64

This is a useful first implementation slice but not the completed outcome:
run-local raw JSON and CSV would continue accumulating after they stop serving
formal consumers.

### Eliminate producer files before sealing

This would require rewriting prefetch, coverage, multiplier, quote-cache, and
manual compatibility flows around a new in-memory interface. The canonical blob
already solves durable storage, so this extra migration has no demonstrated
benefit.

### Remove legacy readers or rewrite historical manifests

Historical entries without a blob ref still need their original receipt and
files. Rewriting immutable evidence adds risk without reducing new writes.
They leave storage through the existing whole-run retention process.

### Add a background cleanup service or configuration flag

The safe retirement point is already known at manifest sealing. A second
scheduler, state machine, retry database, or opt-in configuration would add
more failure modes than the storage optimization warrants.

## Validation requirements

Minimum focused evidence:

- canonical receipt contains a blob ref and no inline base64;
- legacy receipt still binds inline bytes and loose files;
- fsync spies and publication fault injection prove cleanup cannot start before
  durability eligibility succeeds; failures preserve shadows without changing
  manifest availability;
- canonical seal and resolver work after both shadows are absent;
- corrupt/missing blob still fails closed without fallback;
- cleanup rejects contradictory role paths, traversal, parent/leaf symlinks,
  hardlinks, non-regular files, replacements, and changed bytes;
- cleanup is idempotent and handles partial manifests symbol by symbol;
- cleanup failure leaves the sealed manifest available to account consumers;
- both new-seal and `prefetch_done` recovery paths establish cleanup eligibility,
  retire compact shadows, and emit one truthful trigger-labelled event;
- the shared frozen-batch facade and existing Shadow Replay/archive canonical-
  only integration tests remain green;
- `benchmark_required_data_scan_blobs.py --profile canonical` exercises compact
  receipt -> seal -> durability eligibility -> cleanup -> blob-only resolution,
  requires exactly two retired shadows and no surviving raw/CSV shadow, and
  counts the blob plus required receipt/manifest metadata as retained bytes;
- formal canonical acceptance enforces deterministic fixture integrity,
  bounded retained bytes, bounded Python allocation, exact cleanup counts, and
  blob-only resolution; timing is reported but is not a cross-host gate.

After focused tests, run Ruff, dependency-graph validation if imports change,
the relevant tick/research integration tests, and the full project test suite.
No production tick is required for source validation. A later natural scheduled
run may verify storage and read-source evidence only after separate release and
upgrade authorization.

## Shared projection-validation performance

### Goal, success signals, and non-goals

Reduce the CPU cost of the shared JSON-to-CSV projection validator without
changing its evidence or failure contract. The deterministic 254-row, 60-column
fixture must produce the same canonical comparison result while removing the
per-cell pandas row indexing hotspot. On the same host and fixture, the fallback
loop should be at least 80% faster than the recorded baseline, and the canonical
benchmark must retain zero violations and the existing exact cleanup and
blob-only-resolution evidence.

This optimization does not remove or cache validation across receipt
publication, manifest sealing, recovery, or blob-only resolution. It does not
change CSV columns, numeric equivalence, null and boolean handling, multiplier
enrichment or attestation, schemas, public commands, persisted state, or
production behavior.

### Current facts and chosen design

`_validate_consumer_csv_projection()` is the shared owner used by quote-receipt
validation, the unplanned/finalization path, and canonical blob-byte resolution
through `validate_required_data_quote_bytes()`. It first validates column order
and row count, constructs the round-tripped expected frame, and returns early
when `DataFrame.equals()` succeeds. Before the optimization, the fallback
performed two `DataFrame.iloc` row lookups per cell and repeated both lookups for
an allowed multiplier enrichment.

The canonical benchmark profile reaches this fallback at multiple trust
boundaries. A read-only profile attributed 94.2% of its cumulative time to the
shared validator and recorded 152,847 `iloc` calls. On the same deterministic
fixture, caching each expected and actual row Series produced identical
canonical value pairs while reducing median loop time from about 1.11 seconds to
about 0.026 seconds, a 97.6% reduction. These timings are host-specific
diagnostic evidence, not a cross-host acceptance threshold.

Keep the existing fast path, row-major validation order, and mixed-dtype row
coercion. For each row, construct the expected and actual Series once with
`iloc`, then iterate their scalar values positionally with
`REQUIRED_DATA_COLUMNS` in a strict zip for the existing canonical comparison
and multiplier-enrichment check. This removes per-cell Series construction and
label indexing, creates no new abstraction, and preserves the existing error
and metadata-binding paths.

```text
JSON rows -> CSV round-trip frame -> column/row checks -> frame.equals fast path
  -> row-cached fallback -> canonical scalar comparison
  -> optional multiplier metadata binding -> return or SourceReceiptError
```

The function remains stateless: success returns without mutation, and any
invalid projection or unattested enrichment raises before receipt publication.
No manifest, cleanup, notification, ledger, broker, or runtime state transition
changes.

| Owner | Contract |
|---|---|
| `src/application/opend_symbol_outputs.py` | Owns the shared fallback loop and preserves fail-closed projection semantics |
| `tests/test_required_data_snapshot.py` | Covers the unchanged fast path and mixed-dtype row canonicalization |
| `tests/test_required_data_output_integrity.py` | Covers value drift rejection and multiplier attestation |
| `scripts/benchmark_required_data_scan_blobs.py` | Provides deterministic end-to-end correctness, storage, allocation, and corroborating host-local timing evidence |

### Rejected alternatives

- `itertuples()` or whole-frame coercion avoids every Series construction but
  changes mixed-dtype boolean scalar classification on the supported pandas
  runtime. The faster option is rejected because this work unit does not change
  canonical projection semantics.
- Caching or removing repeated validation across lifecycle stages would weaken
  independent trust boundaries and expand the change beyond the hotspot.
- Adding a helper, configuration switch, dependency, or timing gate would add
  ownership and maintenance without improving the loop.

### Implementation slice

Replace only the fallback indexing loop in
`_validate_consumer_csv_projection()` by caching its two row Series per row and
iterating their values positionally, then add one focused mixed-dtype regression
covering the existing boolean canonicalization. Keep the fast path, canonical
helpers, enrichment rule, metadata binding, and errors unchanged.

### Validation plan

- In one process on the deterministic fixture, use `perf_counter_ns()` to run
  one warmup and at least five paired repetitions of the repeated-index and
  row-cached fallback loops. Require identical canonical value pairs and at
  least 80% reduction in median elapsed time. Record the command, Python and
  pandas versions, samples, medians, and ratio in the implementation Deepreview
  artifact; do not add a checked-in helper or timing gate.
- Run the focused projection and integrity tests and Ruff on the changed Python
  files.
- Run the canonical benchmark smoke profile as corroborating end-to-end timing
  evidence, then the formal canonical profile for deterministic fixture,
  storage, allocation, cleanup, and blob-only-resolution acceptance. Its timing
  is not the fallback performance gate.
- Run the project-required broader tests and guardrails. Regenerate the
  dependency graph only if imports change.

The implementation is acceptable only if existing drift and unattested-
multiplier failures remain fail-closed, canonical benchmark violations remain
empty, and the host-local fallback improves by at least 80%. Timing remains
reported evidence rather than a portable CI gate.

### Risks and open questions

The fallback still constructs two Series per row, so its remaining ceiling is
linear in row count. The measured reduction exceeds the target while retaining
mixed-dtype row coercion; remove the Series only if a separately approved
canonical-type contract change makes that safe. The integrity tests remain the
authority for fail-closed behavior. No open product, schema, architecture,
permission, or production question remains.

## Scheduled Tick required-data latency

### Goal and success signals

Reduce the three largest confirmed required-data delays in the scheduled US Tick
without changing strategy coverage, data freshness, evidence authority, or hard
timeout safety:

1. one opening warmup planning failure must not prevent later symbols from warming;
2. underlier snapshot and market-state observations must be fetched in bounded
   batches per OpenD binding instead of opening one gateway and making two calls
   per symbol;
3. the common multiplier-only projection mismatch must avoid the full row-by-row
   fallback while preserving its exact fail-closed semantics for every other case.

Source-level success requires deterministic tests proving warmup continuation,
batched provider call counts and response reconciliation, unchanged projection
failure behavior, and an additional material reduction in the projection
validator's host-local median. The initial target is at least 60% below the
current 254-row common-path median on the same host; the observed prototype moved
from about 28.4 ms to 5.9 ms. This timing is diagnostic evidence, not a portable
CI gate.

After a separately authorized release and production upgrade, one natural
two-account US scheduled run should reach `delivery_confirmed` within 480 seconds
and finish within 540 seconds, leaving at least 60 seconds before the existing
600-second wrapper deadline. A source change or fixture benchmark cannot claim
that operational target in advance.

### Non-goals and safety boundaries

- Do not increase Symbol, prefetch, or account concurrency and do not weaken the
  existing killable hard-timeout boundary.
- Do not increase the 600-second wrapper deadline in this work unit.
- Do not reduce expiration, strike, option-type, position, or strategy coverage.
- Do not weaken formal prefetch failure, quote freshness, snapshot completeness,
  manifest, receipt, multiplier-attestation, or evidence checks.
- Do not add a provider fallback, retry hierarchy, cache layer, configuration key,
  worker pool, scheduler, or new persisted schema.
- Do not change candidate ranking, Close Advice policy, IV/Greeks availability
  semantics, pending-delivery recovery, notification content, ledger, trades, or
  broker-facing behavior.
- Do not refactor account pipelines or post-delivery sidecars in this work unit.
- Do not modify runtime configuration, send a notification, commit, push, release,
  deploy, or run a production Tick without separate authorization.

### Current facts and constraints

The 2026-09-04 production run `20260904T134008Z-6031b7` reached the wrapper's
600-second deadline before its already-committed `lx` delivery attempt. Its
required-data prefetch took about 414 seconds and recorded about 221 seconds of
option-chain rate-gate wait across 69 OpenD option-chain calls. The opening
warmup had stopped after the first symbol planning failure, so later symbols did
not receive the intended chain-cache warming.

The warm-cache run `20260904T140012Z-80d3e8` made no option-chain OpenD calls but
still took about 444 seconds. Required-data prefetch took about 205 seconds,
including roughly 95 seconds of planning and 46 seconds around seal, publication,
and validation. Account pipelines and downstream delivery work remain measurable
but are not among the three selected required-data root causes.

`_warm_required_data_chain_cache_inprocess()` currently builds every symbol plan
inside one outer exception boundary. A projection failure or unresolved symbol
identity returns a degraded summary immediately, even though later symbols are
independent and the warmup is best-effort work before the formal run.

`build_required_data_fetch_plan()` resolves one underlier observation per symbol.
The current OpenD facade creates and closes a gateway for that symbol, calls
`get_snapshot([code])`, then calls `get_market_state([code])`. The prefetch
orchestrator already owns a run-scoped `spot_observation_cache`, but it is filled
only as each symbol plan is built. Nine symbols therefore cause nine gateway
lifecycles and eighteen serial provider calls before option-chain work begins.
Expiration discovery separately opens one gateway and makes one provider call
per symbol; those calls remain serial in this work unit. The selected batch
change removes the underlier-observation calls, not all planning I/O.

`_validate_consumer_csv_projection()` already keeps the exact `DataFrame.equals()`
fast path and caches each mixed-dtype row before its canonical fallback. A current
CPU smoke still attributes about 41% of cumulative time to this validator. The
dominant normal mismatch is an attested `multiplier` enrichment; all other
columns normally match exactly. An `itertuples()` replacement was rejected by an
existing mixed-dtype boolean regression and is not semantically safe.

Relevant source paths are unchanged between the deployed v3.4.7 behavior used
for the timing evidence and current `origin/main` at design time. Provider wall
time remains environment-dependent; deterministic tests can prove request count
and behavior, not the production latency target.

### Chosen design

#### 1. Continue warmup planning after one symbol fails

Keep account/config assembly and the union symbol plan under the existing outer
failure boundary because those failures invalidate the whole warmup scope. Move
only per-symbol plan construction, projection validation, identity resolution,
and shard creation into a per-symbol exception boundary.

For a failed symbol:

- mark the existing summary `degraded` and retain the existing bounded
  `reason_codes` mechanism;
- build every request and expiration shard in a local `symbol_tasks` list, then
  extend the global task list only after the whole symbol succeeds;
- add no partial tasks for a failed symbol and continue with the next symbol
  while the planning deadline has not been reached;
- do not manufacture expirations, reuse another symbol's plan, or convert a
  provider/planning failure into `success_empty`;
- leave the later formal prefetch unchanged and fail closed there if required
  data is still unavailable.

Check the existing worker deadline between symbol plans as well as between
fetch shards. When planning reaches it, preserve the current
`deadline_reached` status and skip the remaining work. At finalization, any
planning reason or failed shard keeps the summary `degraded`; later successful
symbols must not overwrite an earlier failure with `ready`.

No new summary field, state, retry, thread, process, or persistent artifact is
needed. Shard fetch failures and the supervisor deadline retain their current
behavior.

#### 2. Batch run-scoped underlier observations by physical binding

Freeze one in-memory planning identity for each symbol before provider I/O. The
identity contains the normalized symbol, canonical `(source, host, port)`
physical binding, trading date, provider code, and market. The same identity is
used to group batch requests, construct the `SpotObservationCacheKey`, and build
the later per-symbol fetch plan; the planner must not read the clock again for
that symbol.

Identity construction is isolated per symbol. An invalid symbol or trading-date
failure is recorded for warmup and does not prevent other identities from being
grouped. Formal prefetch retains its existing fail-closed behavior for any
required symbol whose planning identity cannot be established.

Only an identity whose source satisfies the existing
`is_futu_fetch_source()` contract enters the OpenD batch path. A non-OpenD
compatibility source keeps its current single-symbol routing and is outside this
work unit's provider-call and latency acceptance; the batch facade must not
reinterpret it as OpenD.

Add one batch facade beside `get_underlier_observation_opend()` in
`src/application/opend_market_snapshot_fetching.py`. For each canonical physical
binding, the caller opens one ready gateway and partitions requested codes by
market as a provider batch constraint; market is not part of the physical
binding identity. Each code chunk uses the existing market-snapshot rate limiter
for two independently attempted calls:

```text
get_snapshot(requested_codes)
get_market_state(requested_codes)
```

Both endpoints are attempted when the other fails and the caller's absolute
deadline still permits another call. After the allowed calls finish, the facade
freezes one normalization time for that chunk, indexes responses by exact
normalized code, rejects duplicate rows for that code, ignores unexpected rows
as non-authoritative, and calls the existing
`normalize_underlier_observation()` once per requested code. A missing snapshot
or market-state row therefore produces a typed unavailable observation rather
than borrowing another row or a stale price.

The existing `resolve_opend_batch_config()` is the only batch-size authority.
Formal prefetch passes its already resolved `market_snapshot` size; warmup
resolves the same configuration and passes the same scalar. Requests larger
than that value are split without adding another default or configuration key.

Gateway construction and execution are isolated per physical binding. Failure
of one binding creates typed unavailable observations only for its exact frozen
identities, closes that gateway best-effort, and continues with the next binding.
Partial valid rows within a successful binding remain usable per code.

Warmup passes its existing `stop_monotonic` into prefill. Before opening a
binding, starting a chunk, or starting either endpoint, prefill checks the
remaining time and caps that rate-limited call's `max_wait_sec` to the smaller of
the configured limit and the remaining duration. It starts no new provider call
after the deadline; the deadline overrides the ordinary two-endpoint attempt
rule. Formal prefetch has no new local deadline and retains the existing wrapper
as its killable process boundary.

The pre-planning path returns the existing
`SpotObservationCacheKey -> OpeningUnderlierObservation | None` mapping. Its
cache reader accepts an identity-valid typed unavailable observation; it requires
a positive finite price only when the cached observation claims `status=ready`.
Every identity attempted by the batch path receives a typed observation, so the
later planner consumes that authority and the fetch request carries it even when
unavailable. It must not perform a second spot request for a batch-attempted
identity. The legacy single-symbol resolution path remains only for a symbol
that never obtained a valid frozen batch identity; it is not a retry for a batch
exception or partial response.

Both formal prefetch and opening warmup run the prefill after the union symbol
plan is resolved and before per-symbol strategy/expiration planning. Their
projection and expiration contracts remain unchanged. The implementation adds
no production metric or persisted summary field; fake-gateway tests directly
assert provider call counts and requested codes. Production validation uses the
existing stage latency. More detailed planning metrics belong to a separate
observability work unit if post-change evidence shows they are necessary.

#### 3. Short-circuit the attested multiplier-only projection path

Keep the current full-frame `equals()` fast path first. When it fails, compare
the non-`multiplier` columns with pandas' exact frame equality. If those columns
are exactly equal, inspect only the single resolved `multiplier` column using
the existing `_canonical_csv_value()` and
`_is_valid_multiplier_enrichment()` functions. A
valid enrichment still invokes the existing metadata-binding check before
returning when `csv is not None`; blob-byte validation with `csv=None` preserves
the current behavior. `chain_multiplier` and `snapshot_multiplier` stay in the
exactly equal non-`multiplier` subset and are never accepted as enrichment.

If any non-multiplier column is not exactly equal, or multiplier values do not
all satisfy the current equal-or-attested-enrichment rule, use the existing
row-cached canonical fallback unchanged. The new branch is therefore only a
shortcut for the common case; mixed dtypes, canonical-but-not-exact values,
invalid enrichment, row identity, error messages, and trust-boundary validation
remain owned by the current fallback and tests.

Do not cache validation results or remove validation at receipt publication,
manifest sealing, recovery, or blob resolution.

### Owners, data flow, and state transitions

| Owner | Contract in this work unit |
|---|---|
| `src/application/multi_tick/required_data_prefetch.py` | Continue best-effort warmup with atomic per-symbol tasks; group frozen identities, isolate gateway lifetime per binding, and prefill the cache before warmup and formal planning |
| `src/application/required_data_planning.py` | Freeze each symbol's planning identity once, build and consume the existing spot-observation cache, accept identity-valid typed unavailable values, and keep formal projection fail closed |
| `src/application/opend_market_snapshot_fetching.py` | Own batched OpenD snapshot/state calls, independent endpoint failure handling, exact code reconciliation, and normalization |
| `src/application/opend_symbol_outputs.py` | Add only the multiplier-only shortcut and retain the complete canonical fallback |
| Existing receipt, manifest, frozen batch, strategy, and notification owners | Unchanged; consume the same planned and validated facts |

```text
union symbol plan
  -> freeze per-symbol identity and trading date once
  -> batch underlier observations per canonical physical binding and market chunk
  -> existing run-scoped spot_observation_cache
  -> per-symbol expiration and strategy planning
  -> opening warmup: continue independent symbols on planning failure
  -> formal prefetch: existing fail-closed required-data barrier
  -> quote receipt and manifest validation
  -> exact frame equality
       -> multiplier-only shortcut when all other columns exactly match
       -> existing row-cached canonical fallback otherwise
  -> unchanged account pipelines and delivery authority
```

No durable state transition changes. Warmup remains best effort and reports
`degraded`; formal prefetch still determines readiness; sealing and notification
eligibility still depend on the existing validated terminal manifest.

### Failure behavior

| Scenario | Required behavior |
|---|---|
| Warmup configuration or union-plan failure | Preserve current whole-warmup degraded return |
| One warmup symbol plan or identity fails | Discard its local task list, retain degraded status, and continue later symbols while planning time remains |
| Warmup planning reaches its deadline | Preserve `deadline_reached`; do not begin another symbol or any expired shard |
| One prefill symbol identity fails | Exclude only that symbol from batching; continue grouping other symbols; formal planning remains fail closed |
| A prefill identity has a non-OpenD source | Keep its existing single-symbol route; never send it to the OpenD batch facade |
| One gateway binding fails | Cache typed unavailable values only for that binding and continue independent bindings |
| Batch snapshot or market-state call raises | Attempt the other endpoint, then cache affected typed unavailable observations; no batch retry or stale price |
| Warmup batch deadline is exhausted | Start no new binding, chunk, or endpoint; leave unattempted identities unfilled and let the existing supervisor remain the final kill boundary |
| Batch response omits a requested code | Only that code is unavailable; other exact single rows remain usable |
| Batch response duplicates a requested code | Reject that code as unavailable; never choose an arbitrary row |
| Batch response includes an unexpected code | Ignore it as non-authoritative; never populate another symbol's cache entry |
| Observation is stale, closed, suspended, or invalid | Preserve `normalize_underlier_observation()` status and the formal planner's existing fail-closed outcome |
| Batch-attempted observation is unavailable | Pass the typed observation through planning and fetch; perform zero secondary spot calls |
| Non-multiplier projection differs | Execute the unchanged canonical fallback and reject any real drift |
| Multiplier enrichment lacks valid attestation | Preserve the existing `SourceReceiptError`; no receipt or manifest authority is created |
| Optimization raises unexpectedly | Preserve the existing exception boundary; do not turn failure into an empty candidate result |

### Rejected alternatives

- Reusing one gateway while retaining eighteen serial observation calls is a
  smaller diff but does not remove the measured planning bottleneck and is
  unlikely to create enough deadline margin.
- Increasing worker concurrency or the wrapper timeout hides the slow call shape
  and conflicts with the current hard-timeout safety boundary.
- Serial retry after a batch error restores the old worst-case delay and adds
  retry behavior without evidence that it is needed. The existing single-symbol
  path remains only for identities that could not enter the batch path.
- Batching option-contract snapshots, changing pipeline architecture, moving
  sidecars out of process, or adding a global deadline is broader work with
  different owners and failure contracts.
- Replacing the projection fallback with `itertuples()` is faster but changes
  supported mixed-dtype canonicalization.

### Implementation slices

1. **Warmup isolation:** change only the per-symbol planning boundary and add a
   local task commit plus a planning deadline check. Regressions cover failure in
   a symbol's second request after its first request was built, later-symbol
   continuation, degraded final status, and unchanged shard-count invariants.
2. **Batched underlier observations:** first add the batch facade and exact-code
   reconciliation tests, then add frozen-identity/cache prefill and wire it into
   warmup and formal prefetch. Tests cover per-symbol identity failure, one and
   multiple bindings, mixed OpenD/non-OpenD sources, market partitions,
   configured batch-size splitting, absolute warmup deadline enforcement,
   independent endpoint failures, partial/duplicate/unexpected responses,
   unavailable-cache hits, zero secondary spot calls, exact gateway call/code
   counts, closure, and formal fail-closed behavior.
3. **Projection shortcut:** add the multiplier-only branch before the existing
   fallback. Tests cover valid enrichment, invalid/unattested enrichment,
   `csv=Path` and `csv=None`, exact `chain_multiplier` and
   `snapshot_multiplier`, non-multiplier drift, null/numeric equivalence, and the
   existing mixed-dtype boolean case.

Each slice reuses current owners and adds no new module, dependency, schema,
configuration, command, or service.

### Validation plan

- Run focused tests for warmup, planning, market-snapshot reconciliation, and
  projection integrity:

  ```bash
  ./.venv/bin/python -m pytest \
    tests/test_required_data_prefetch_inprocess.py \
    tests/test_required_data_fetch_planning.py \
    tests/test_market_snapshot_fetching.py \
    tests/test_required_data_snapshot.py \
    tests/test_required_data_output_integrity.py
  ```

- Run Ruff on every changed Python and test file. Regenerate and verify the
  dependency graph only if imports change.
- Repeat the paired host-local 254-row validator measurement against the current
  row-cached baseline; require identical results and at least 60% lower median on
  the multiplier-only common path.
- Run the canonical required-data benchmark first as a reduced smoke for timing
  diagnosis and then with its formal warmup/repetition counts for deterministic
  fixture, retained-byte, allocation, cleanup, and blob-resolution acceptance.
  Do not treat smoke timing as production acceptance.
- Run the relevant Tick integration tests, repository wording/sensitive-artifact
  guardrails, `git diff --check`, and the full pytest suite before implementation
  is called complete.
- Production timing and delivery confirmation require a later, separately
  authorized release, upgrade, and natural scheduled run.

### Risks and open questions

- A provider-level batch exception has a wider observation blast radius than a
  single-symbol call. The chosen bounded behavior is per-binding fail-closed
  without a serial retry; the other endpoint, other bindings, and partial valid
  rows still survive. Owner: batched observation facade and focused
  reconciliation tests.
- Warmup's absolute stop time can truncate a batch between its two endpoints.
  The partial rows normalize as unavailable and no new call starts after the
  deadline. Owner: warmup supervisor contract and deterministic clock tests.
- The expected wall-time gain from fewer OpenD calls is not provable on fixtures.
  Owner: later production verification after separate release and upgrade
  authorization.
- Batch size follows the existing OpenD snapshot limit. One gateway is reused per
  canonical physical binding, while codes are conservatively partitioned by
  market because live mixed-market batch behavior is not established. Owner:
  current OpenD fetch configuration and gateway contract.
- Option-expiration discovery still opens one gateway and makes one serial call
  per symbol. Owner: required-data planning; revisit only if post-change planning
  remains a top-three hotspot.
- The projection shortcut addresses the confirmed CPU hotspot but not every
  operation inside the observed 46-second seal/publication interval. Owner:
  canonical benchmark and any later profile based on post-change evidence.
- Account pipelines and post-delivery work remain outside this scope. Revisit
  only if the three fixes pass and a natural production run still misses the
  540-second target.
- Performance work lowers timeout probability but does not repair the existing
  scheduler-target-commit-before-provider-attempt window. Canonical notification
  success remains `delivery_confirmed`; timeout recovery belongs to the
  notification/scheduler owner and requires a separate work unit.

## Risks and deferred work

- Cleanup failure can leave duplicate files and consume disk, but failing the
  financial pipeline would be worse. Owner: existing runtime storage baseline
  and operator runbook.
- This change reduces future accumulation only. Existing canonical shadows and
  legacy-only history remain until a separately authorized retention or cleanup
  operation. Owner: operator workflow.
- Historical tools that bypass the sealed resolver remain compatibility-only;
  they are not expanded or repaired in this work unit. Owner: the tool that
  still exposes the unsupported direct path.
- Cleanup assumes the trusted run root has no legitimate writer after prefetch.
  Hostile same-user mutation needs a stronger isolation boundary and is
  deferred; detected races and unsupported descriptor primitives fail safe by
  preserving files.
- The existing numeric-equivalence and multiplier-enrichment validation stays
  unchanged; storage compaction must not weaken those checks.
