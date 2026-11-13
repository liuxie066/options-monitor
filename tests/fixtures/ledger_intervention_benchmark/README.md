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
readback. Memory SQLite uses a shared in-memory database with independently
opened real SQLite connections; disk SQLite uses real temporary files.

Run timing separately from diagnostic instrumentation, using the same Python
interpreter and host before and after a change:

```sh
python scripts/benchmark_ledger_interventions.py --output timing.json
python scripts/benchmark_ledger_interventions.py --mode counts --output counts.json
python scripts/benchmark_ledger_interventions.py --mode counts \
  --sizes 501 2001 --active-lots 1 --output single-lot-counts.json
python scripts/benchmark_ledger_interventions.py --mode profile \
  --sizes 2001 --storage disk --output profile.json
```

Defaults are two warmups and twenty measured samples per operation, scale and
storage mode. JSON preserves every timing sample; p50 is the median, p95 uses
the nearest rank. Counts distinguish ordinary history reads, the independent
post-write forced-full read, and domain conversion entry points/call sites.
Do not add nested conversion entry points together: the canonical decoder is
the total domain-construction count; public wrapper calls are a separate
historical comparison metric.

Acceptance for 20,001 events / 101 active lots is preview p50 <= 2 seconds and
apply p50 <= 5 seconds, with no p95 regression against the matching unchanged
baseline. Ordinary history reads must remain <= 1 / 2 / 4 for preview / void
apply / repair apply, plus <= 1 independent forced-full read for apply. These
are synthetic engineering budgets, not production latency guarantees.
