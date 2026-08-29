# Required Data Storage Design

> Status: source contract implemented. Commit, release, deployment, and any
> production history cleanup remain separate operator actions.

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
