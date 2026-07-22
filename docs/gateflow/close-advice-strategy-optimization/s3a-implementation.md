# Gateflow S3a Artifact — Close-decision facet schema and capture

- Gate: `implementation`
- Work unit: `close-advice-strategy-optimization`
- Slice: `S3a`
- Status: `implementation and slice review complete; pending commit`
- Review: `docs/reviews/code-review-20260723-012716.md`

## Delivered

- Registered the three optional Close Advice facet files without changing required `DATASET_FILES` or the dataset schema version.
- Added an explicit `include_close_decisions`/source-path capture path. Candidate-only builds retain their existing manifest/files and do not create optional facet artifacts.
- Captured formal P0 fields, immutable normalized decision facts, and P0/P1/P2/P3 shadow projections from the same close observation.
- Joined evidence only by canonical run, lowercase account, and stable `position_lot_id`; duplicate close rows, contexts, lot matches, reallocation rows, or decision audit events fail closed.
- Used the canonical run-ID prefix as a run-start/same-run anchor and the unique successful account-scoped `close_advice` audit event as the actual decision timestamp.
- Enforced point-in-time bounds for position context, native quote timestamps, reallocation source run, and any replacement/candidate run IDs.
- Added deterministic material fingerprints and episode IDs. Exact same-day reruns reuse the earliest episode and append source-run provenance; material, date, recommendation/tier, and account changes create distinct episodes.
- Rejected formal/P0 recommendation mismatches rather than admitting a false baseline.

## Plan correction

The accepted plan originally assigned `observed_at_utc` directly from the run-ID prefix. Runtime inspection proved that the run ID is allocated before position-context generation, so the original rule would classify normal context as future evidence. The durable plan and Close Advice contract now use the unique successful account-scoped Close Advice audit timestamp for the observation and keep the run prefix only as the run-start/same-run anchor.

## Verification

- Broad Close Advice, Shadow Replay, agent-contract, notification, and Daily Brief suite: `524 passed`.
- Ruff on touched Python files: passed.
- `git diff --check`: passed.
- DeepReview: no remaining material finding.

## Non-goals preserved

- No production policy selector or config change.
- No notification selector or wording change.
- No mark collection, settlement inference, or outcome synthesis.
- No live broker, Feishu, ledger, or runtime-state write.
