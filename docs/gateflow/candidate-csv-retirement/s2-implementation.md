# Gateflow Implementation Artifact — S2 Candidate Evidence Consumers

- Gate: `implementation`
- Work unit: `candidate-csv-retirement`
- Slice: `S2`
- Status: accepted
- Artifact path: `docs/gateflow/candidate-csv-retirement/s2-implementation.md`

## Accepted scope

- Classify historical account-run candidate evidence into the six approved compatibility states without opening
  candidate CSV bytes.
- Migrate Research, archive replay eligibility, Shadow Replay, candidate impact, and Strategy Lab to sealed owner
  snapshots plus supplementary trace JSONL.
- Carry classification coverage through run-window and dataset strict replay/promotion gates, including account and
  market scope.
- Replace the Shadow Combo underwritten-Put CSV with a versioned JSONL projection of manifest-bound Combo v2 terminal
  Funding Put decisions; do not rerun underwriting.
- Preserve S1 candidate CSV and v1 status producers until S3.

## Files and contracts changed

- Added `src/application/candidate_evidence_history.py` as the non-parsing historical classification/loader boundary.
- Reworked Research evidence/archive rendering and CLI payloads around sealed projections and explicit coverage.
- Reworked Shadow Replay capture/readiness/candidate-impact analysis and Strategy Lab handoff around sealed candidate
  facts; trace may supplement rejection evidence but cannot create a universe.
- Added `combo_owned_funding_put_decisions.v1.jsonl` and its v2 source receipt/facet contract, bound to the source
  candidate manifest and Combo v2 snapshot.
- Updated Strategy Architecture, Shadow Replay Runbook, and Agent Wiki to remove candidate-CSV consumer claims.

## Authority and compatibility behavior

- `supported` alone can satisfy strict candidate evidence authority.
- `supported_limited_legacy_snapshot` can contribute only its validated sealed facts and always blocks strict replay
  and promotion.
- CSV-only, missing, invalid-schema, and not-scanned histories remain explicitly classified; unsupported history is
  never converted to a successful empty dataset.
- Account/market scope is applied to coverage and rows together. Unknown-market unsupported history cannot be
  discarded by a market filter.
- Combo evaluation consumes accepted terminal Funding Put rows returned by one locked integrity/facet validation;
  rejected rows remain available for diagnostics but never become main pair candidates.

## Verification

- Complete S2 focused suite: `197 passed`.
- Ruff over all changed S2 Python and test files: pass.
- Compileall over changed production modules: pass.
- `git diff --check`: pass.
- Initial DeepReview: `docs/reviews/code-review-20260812-130109.md`.
- Fix artifact: `docs/gateflow/candidate-csv-retirement/s2-review-fix.md`.
- Re-review: `docs/reviews/code-review-20260812-224121.md`; no unresolved findings.

## Deferred exclusively to S3

- Candidate compatibility CSV writers and empty-file materialization.
- v1 strategy-status index dual publication and candidate CSV canonical-artifact rules.
- Combo `output_mode`, dead candidate file adapters/render paths, and the production static forbidden-pattern guard.
