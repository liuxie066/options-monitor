# Gateflow Artifact — Aggregate DeepReview

- Gate: `aggregate deepreview -> fix -> re-review`
- Work unit: `data-storage-runtime-projection-p1`
- Branch: `perf/data-storage-runtime-projection-p1`
- Base: `main@421591ddb5e298cda064ec414c8b87f62b0811b2`
- Initial review artifact: `docs/reviews/code-review-20260813-160956.md`
- Re-review artifact: `docs/reviews/code-review-20260813-163436.md`
- Artifact path: `docs/gateflow/data-storage-runtime-projection-p1/aggregate-deepreview.md`
- Status: `pass; accepted findings fixed and re-reviewed`

## Scope and changed files

The aggregate review covered the complete Phase 1 branch. Its fix loop changed:

- `src/application/research/storage_baseline.py`
- `src/application/research/performance_baseline.py`
- `tests/test_research_storage_baseline.py`
- `tests/test_research_performance_baseline.py`
- `docs/AGENT_WIKI.md`
- the aggregate review and Gateflow artifacts

No schema, runtime config, production database, notification, broker, service,
release, or deployment path changed.

## Finding decisions

- `DR-AGG-01`: `accepted`; final state `已修复`. Payload-free account cardinality
  now comes from immediate non-symlink `output_accounts` directories; unavailable
  or malformed facts make the fanout axis fail closed.
- `DR-AGG-02`: `accepted`; final state `已修复`. Reference identity now binds
  separate CPU and machine-model facts and no longer collapses the reviewed Mac
  to generic `arm`.

No findings were rejected, deferred, or left needing evidence.

## Validation and performance decision

- Aggregate focused suite: `158 passed`; final post-fix focused suite:
  `54 passed`; independent research regression: `30 passed`.
- `ruff check`, `compileall`, and `git diff --check` passed.
- Source-inventory validation has no stale or unclassified matches.
- Formal default 5/30 benchmark completed with all five schemas and exact
  projector/writer parity.
- `fixed_output` passes at 0.357 s wall / 0.357 s CPU p95.
- `retained_closed_lots` fails at 2.849 s wall / 2.739 s CPU p95.
- Writer result is `fail`; diff publication is `not_implemented`; combined
  Phase 3A is `not_ready`. This is accepted evidence, not a validation failure.

## Documentation decision

`docs/AGENT_WIKI.md` now explains the exact payload-free account-count basis and
that reference fingerprints bind separate CPU and hardware-model facts. Release
notes remain out of scope because no release is authorized.

## Residual risks

- Current writer O(E) replay plus O(current retained lots) global replacement:
  `assigned to later work unit` — Phase 3A diff publication / checkpoint
  planreview only after this evidence is reviewed.
- Non-atomic live SQLite snapshot edge: `assigned to later work unit` — storage
  hardening.
- Same-size content tampering: `assigned to later work unit` — explicit verifier.
- Synthetic versus production distribution: `assigned to later work unit` —
  separately authorized read-only calibration.
- Allocation-worker RSS is a labeled cumulative process high-water mark rather
  than an isolated per-scenario peak: `assigned to later work unit` — split
  allocation scenarios into fresh processes if a later gate needs absolute RSS.

No residual risk is unclassified.

## Completion status

All aggregate findings are fixed and re-reviewed. Next entry point:
`accepted deepreview commit`.
