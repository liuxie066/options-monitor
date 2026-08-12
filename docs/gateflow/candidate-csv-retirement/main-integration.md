# Gateflow Artifact — Latest Main Integration

- Gate: `latest-main integration`
- Work unit: `candidate-csv-retirement`
- Integrated base: `origin/main@6940a96cc6e5`
- Review: `docs/reviews/code-review-20260813-031626.md`
- Status: accepted
- Artifact path: `docs/gateflow/candidate-csv-retirement/main-integration.md`

## Integration decision

The branch has integrated 30 commits published to `main` after the original
work-unit base. Fourteen paths were changed by both histories and were reviewed
at their ownership boundaries. Conflict resolution preserves both sides of the
current contract:

- candidate compatibility CSV production and consumption remain retired;
- current candidate facts remain terminal-manifest-bound JSON/JSONL evidence;
- current opening evidence requires the complete decision/scope contract;
- Daily Brief consumes the manifest owner set and v2 status scopes rather than
  reconstructing expected candidate files from config;
- current `main`'s fixed Daily Brief behavior, candidate evidence integrity,
  `net_premium_non_positive` classification, AI Decision Advice retirement, and
  other unrelated production fixes remain intact;
- stale-config override cannot bypass current schema validation or restore
  removed output fields.

The supplemental latest-main DeepReview found no new substantive issue.

## Validation

- Latest-main focused integration suite: `370 passed`.
- Corrected sandbox-compatible complete suite: `4540 passed, 10 skipped`.
- Loopback-only HTTP suite outside the network-restricted sandbox: `4 passed`.
- Ruff, compileall, dependency graph check, US/HK config validation/build
  dry-runs, `git diff --check`, and unmerged-path check: pass.

## Operational boundary

This gate authorizes the source merge commit and Draft PR creation only. It does
not authorize PR merge, release, deployment, production service changes,
notifications, or historical runtime-file cleanup.
