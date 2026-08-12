# Gateflow Implementation Artifact — S3 Candidate Compatibility Surface Retirement

- Gate: `implementation`
- Work unit: `candidate-csv-retirement`
- Slice: `S3`
- Status: accepted after DeepReview fix and re-review
- Artifact path: `docs/gateflow/candidate-csv-retirement/s3-implementation.md`

## Accepted scope

- Stop all candidate/universe/reject/diagnostic/rank CSV production for Sell Put, Covered Call, SP+LC, and CC+LP.
- Keep candidate calculation in memory and preserve terminal candidate facts in the manifest-bound JSON snapshot bundle
  plus append-only JSONL trace.
- Retire v1 strategy-status dual publication, candidate CSV artifact rules, Combo `output_mode`, and dead CSV-only
  adapters.
- Preserve required-data CSV, Close Advice CSV, symbols summary, mark/outcome compatibility inputs, unrelated
  exports, and existing historical candidate CSV bytes.
- Keep historical candidate filenames only in the bounded non-parsing classification/archive metadata surfaces.

## Implementation

- Sell Put and Covered Call scanners now return DataFrames and diagnostics without output/reject-log paths or CSV
  CLI flags. Their orchestration steps remain calculation-only.
- Combo Yield and CC+LP operate on in-memory rows. Combo manual alert text renders directly from typed rows; candidate
  evidence flows through the existing Combo v2 sink and sealed owner snapshot.
- Cash enrichment and candidate labeling are pure DataFrame operations. Removed the three file-only application
  adapters and their obsolete tests.
- Removed v1 strategy-status index publication/loading and candidate CSV canonical artifact rules. Per-symbol JSON
  statuses, the v2 index, owner snapshots, and terminal manifest remain authoritative.
- Removed Combo `output_mode` defaults, resolver helpers, runtime branching, and generated config output. Authored or
  stale runtime use receives a targeted rebuild error. `--allow-stale-config` skips source freshness only and still
  validates the current schema before any run artifact is created.
- Updated current operator documentation and regenerated the checked dependency graph.
- Migrated three CLI Shadow Replay tests from fabricated candidate CSV fixtures to manifest-bound opening snapshot
  fixtures.

## Verification

- Initial DeepReview: `docs/reviews/code-review-20260813-003008.md`.
- Fix artifact: `docs/gateflow/candidate-csv-retirement/s3-review-fix.md`.
- Accepted re-review: `docs/reviews/code-review-20260813-012542.md`; no unresolved findings.
- Review-fix regression suite: `67 passed`.
- Complete changed-test suite plus the new static guard: `569 passed`.
- Complete repository suite excluding the four loopback HTTP cases:
  `4779 passed, 10 skipped, 4 deselected`.
- The four loopback HTTP cases were rerun outside the network-restricted sandbox: `4 passed`.
- Full Ruff check over `src domain scripts tests`: pass.
- Compileall over `src domain scripts tests`: pass.
- Generated dependency graph refreshed and checked: pass, 585 production modules and zero cycles.
- `git diff --check`: pass.
- Static candidate CSV retirement guard: pass.
- Production reference scan: no deleted adapter imports or candidate CSV readers/writers; remaining legacy names are
  confined to filename-only history classifiers. The similarly named domain capacity allocation function is an
  active calculation owner and is not the deleted file adapter.

## Residual risks and uncovered areas

- Aggregate cross-slice behavior and contract consistency are classified as covered by the next approved aggregate
  DeepReview gate.
- No live OpenD scan, notification delivery, release, deployment, runtime file rewrite, or historical artifact
  mutation was performed. These are outside this source-only slice and do not block its deterministic test contract.
- Historical CSV-only runs remain intentionally unsupported for automated replay; this is the accepted S2/S3
  compatibility classification, not a migration gap.
