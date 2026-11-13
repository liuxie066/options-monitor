# Ledger intervention benchmark

The offline runner `scripts/benchmark_ledger_interventions.py` exercises the four
`ledger.api` entry points: void/repair preview and apply. It only creates
synthetic databases in a fresh temporary directory and accepts no ledger path.

The main fixture contains 1,001 / 8,001 / 20,001 events and 101 active lots. The
remaining events are paired opens/closes with explicit lot identity. Account /
contract assignment alternates between synthetic `lx` / NVDA and `sy` / MSFT
USD puts, strike 100, expiry 2027-03-19. The first active open is the target;
repair changes its premium from 2 to 2.5. Event order and intervention clock
are fixed. The single-active-lot comparison uses 501 and 2,001 events.

Each invocation starts from the same prepared database. Database restoration,
initial full projection and obtaining the repair confirmation hash occur
outside the measured interval. The interval includes the complete public API,
including fresh transactional checks, writes, forced-full projection and
readback. Disk SQLite uses real temporary files and the normal repository
connection path, including file identity checks, WAL enforcement, writer-lock
coordination and private artifact permissions.

The memory adapter opens independent real SQLite connections on one shared
in-memory URI and initializes foreign keys, recursive triggers and writer
functions. It omits the per-connection file identity/WAL/artifact-permission
steps and uses SQLite connection defaults; its measured pragmas are recorded.
Memory results describe API/transaction work. The disk rows also cover the
repository's filesystem connection costs. These are separate storage cases.

Run timing separately from diagnostic instrumentation, using the same Python
interpreter and host before and after a change:

```sh
python scripts/benchmark_ledger_interventions.py --output timing.json \
  --baseline tests/fixtures/ledger_intervention_benchmark/baseline-timing.json
python scripts/benchmark_ledger_interventions.py --mode counts --output counts.json \
  --baseline tests/fixtures/ledger_intervention_benchmark/baseline-counts.json
python scripts/benchmark_ledger_interventions.py --mode counts \
  --sizes 501 2001 --active-lots 1 --output single-lot-counts.json \
  --baseline tests/fixtures/ledger_intervention_benchmark/baseline-single-lot-counts.json
python scripts/benchmark_ledger_interventions.py --mode profile \
  --sizes 2001 --storage disk --output profile.json
```

For a new host, first capture a local baseline with `--baseline` omitted. To
repeat the pre-fix measurement, copy the CLI script and
`tests/ledger_intervention_benchmark.py` into the matching directories of an
isolated checkout at `5fdb5890d` and use the same interpreter and arguments.
Do not use the committed baseline as the comparator on a different host.

Timing defaults to two warmups and twenty measured samples per operation,
scale and storage mode. JSON preserves every timing sample; p50 is the median,
p95 uses the nearest rank. A timing comparison rejects a partial matrix,
fewer warmups/samples, fixture/environment differences, p95 regression at any
scale, and preview/apply p50 above 2/5 seconds at 20,001 events. Reduced sample
counts without a baseline are diagnostic smoke runs.

Counts run once per entry, independently of timing. Counts mode always gates
ordinary history reads at <= 1 / 2 / 4 for preview / void apply / repair apply,
and requires one independent post-write forced-full read for apply, zero for
preview. It also checks all-history totals and the stored-payload conversion
ceilings below. With a baseline it checks matching fixtures/environment and
no read or canonical conversion regression. Non-timing reports are rejected
by the timing comparator before timing fields are accessed.

The canonical decoder counts all canonical stored-payload-to-domain
conversions, including the internal import and application-view paths.
Public `stored_trade_event_to_ledger_event` wrapper calls are a separate
historical comparison metric. Do not add nested entry points together.
Direct creation of new intervention events is outside the stored-payload
conversion metric. Call sites are preserved in both count reports.

## Recorded acceptance, 2026-11-13

Baseline API source: `5fdb5890df9e5b94273c7f03fca2e6b44835016f`.
Optimized API source: `be34be010f5eb7235fd97db70c720d0c26515b52`.
The later runner changes add acceptance checks and emitted metadata; they do
not change fixture construction, connection setup, API invocation or timing.
Python 3.12.13, SQLite 3.54.0, macOS 27.2 arm64; interpreter binary digest,
host profile, fixture digests and projector fingerprints are in the JSON.
Both cases use synchronous=1, temp_store=0 and cache_size=2000; journal mode is
`memory` / `wal`. Complete raw samples cover all three main scales and both
storage cases: 24 groups, 480 measured samples in each revision.

20,001 events / 101 active lots, seconds:

| Storage | API | Before p50 | After p50 | Before p95 | After p95 |
| --- | --- | ---: | ---: | ---: | ---: |
| Memory | void preview | 3.567 | 1.723 | 3.630 | 1.750 |
| Memory | void apply | 6.741 | 3.629 | 6.813 | 3.656 |
| Memory | repair preview | 3.914 | 1.772 | 4.001 | 1.799 |
| Memory | repair apply | 8.843 | 4.375 | 9.073 | 4.411 |
| Disk | void preview | 3.639 | 1.728 | 3.764 | 1.754 |
| Disk | void apply | 6.738 | 3.661 | 6.818 | 3.731 |
| Disk | repair preview | 3.901 | 1.788 | 3.999 | 1.834 |
| Disk | repair apply | 8.775 | 4.433 | 8.839 | 4.461 |

All 24 timing comparisons pass; p95 improves at every scale/storage/API.
These are synthetic engineering budgets, not production latency guarantees.

Counts are identical in the two storage cases. For main fixtures, optimized
canonical conversion ceilings hold for each N:

| API | N=20,001 before | After | Optimized ceiling | Ordinary / forced / all history reads |
| --- | ---: | ---: | --- | --- |
| void preview | 60,005 | 40,004 | 2N+2 | 1 / 0 / 1 |
| void apply | 140,014 | 100,012 | 5N+7 | 2 / 1 / 3 |
| repair preview | 80,008 | 40,006 | 2N+4 | 1 / 0 / 1 |
| repair apply | 260,031 | 140,025 | 7N+18 | 4 / 1 / 5 |

The ordinary/forced/all read counts are unchanged. Single-active-lot repair
public-wrapper comparisons (preview / apply) are 501 events: 1,003 / 3,015 ->
1 / 9; 2,001 events: 4,003 / 12,015 -> 1 / 9, below the 2N+1 / 6N+9 limits.
All 24 main and 16 single-active-lot count rows pass their budgets.

The implementation retains complete historical validation and full
post-write projection. It removes non-void history conversions from void
lookup, resolves allocation strategy metadata once per event, reuses frozen
contract identities by all seven immutable scalar inputs (bounded to 256),
avoids text roundtrips for Decimal values and repeated alias reconstruction
for canonical spellings, and skips rebuilding unchanged realized-PnL
snapshots. Event validation and transaction-fresh state are still checked.
