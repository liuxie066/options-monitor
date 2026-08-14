# Gateflow S3 DeepReview Fix

- Work unit: `data-storage-runtime-projection-p3a`
- Slice: `S3`
- Gate: `fix / re-review`
- Initial review: `docs/reviews/code-review-20260814-053516.md`
- Final re-review: `docs/reviews/code-review-20260814-060040.md`
- Status: accepted; all findings fixed
- Artifact path: `docs/gateflow/data-storage-runtime-projection-p3a/s3-review-fix.md`

## Finding decisions

### S3-DR-01 — accepted — 已修复

Collision candidates now come from every tail `open`. The runtime rejects a
suffix duplicate, any id already active in the checkpoint, and any retained row
whose source event is outside the same suffix. Regressions cover close/reopen
after a checkpoint and open/close/reopen within one suffix, including rollback.

### S3-DR-02 — accepted — 已修复

Checkpoint construction enforces the same 64 MiB bound as decoding. An oversized
cache is not inserted; canonical lot/head publication remains committed and
checkpoint mode becomes untrusted so future tail use fails closed.

### S3-DR-03 — accepted — 已修复

Canonical JSON validation now occurs before state construction. The decoder then
releases the original sections as it constructs domain and publication objects,
and publication state avoids a duplicate pre-constructor deepcopy. The 4,000-lot
fixture improved from 85,190,208 to 63,612,120 peak bytes, below 64 MiB.

### S3-DR-04 — accepted — 已修复

The INSERT trigger now owns an explicit append-safe event allowlist. Any unknown
type invalidates all trusted checkpoints with
`unclassified_event_insert`; a direct SQLite regression proves the durable state
change occurs before runtime projection.

### S3-DR-05 — accepted — 已修复

Crash coverage now executes real added, changed and removed lot DML, raises after
the repository write, and proves events, lots, heads, checkpoints and source state
all roll back exactly.

## Re-review validation

```text
runtime/checkpoint tests: 37 passed
focused S3 plus adjacent ledger/publication tests: 175 passed
4,000-lot checkpoint peak: 63,612,120 <= 67,108,864 bytes
ruff: passed
compileall: passed
dependency graph: current; cycles=0
semantic digest: exact
git diff --check: passed
```

## Residual risks

- S4 owns public writer/preflight integration and shadow parity.
- S5 owns explicit migration/activation, reference-host acceptance and operator
  documentation.
- The full oracle's O(E) cost is intentional fallback behavior.
- Existing research-to-ledger import violations remain assigned outside S3.
