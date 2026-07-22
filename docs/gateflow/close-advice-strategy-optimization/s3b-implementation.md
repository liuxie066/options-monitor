# Gateflow S3b Implementation — Close Advice Strategy Optimization

- Gate: `implementation`
- Slice: `S3b — Close-decision marks and outcomes`
- Status: `accepted after DeepReview`
- Production selector/notification mutation: `none`

## Delivered

- Added separate marking for the optional Close Advice facet at deterministic
  1d, 3d, 7d, 14d, and expiration horizons.
- Added current-contract and decision-time replacement quotes to the same mark
  row, including dated future-close fee estimates.
- Added decision close economics and replacement entry/open-fee/slippage facts
  to immutable episodes; those facts participate in the material fingerprint.
- Added five outcomes per episode: four horizon outcomes and one terminal
  outcome, using decision-time incremental value and no sunk opening premium.
- Added canonical lifecycle JSON/JSONL/CSV input, exact account + lot + time
  joins, exact contract-quantity checks, and fail-closed handling for ambiguous
  or incomplete terminal facts.
- Added P3 same-horizon replacement economics, including terminal expiry marks
  when available.
- Added explicit temporal provenance. Only fresh OpenD collection at actual
  collection time is usable; manual `--as-of`, local timestamp-less data, and
  post-expiry spot relabeling cannot settle usable outcomes.
- Preserved candidate facet files and settlement behavior when the optional
  Close Advice facet is absent.

## Safety and policy boundaries

- Mark and collect remain dry-run by default; `--write` is required.
- All writes stay in the local Shadow Replay dataset or explicit OpenD cache
  path already owned by the collection command.
- No runtime config, position/trade state, production selector, notification,
  or broker-facing state is changed.
- Full-lifecycle P&L is never substituted for decision-time-sliced assignment
  or called-away P&L, and a sliced value must be explicitly bound to the episode.

## Verification

- Focused Close Advice and Shadow Replay tests: `65 passed`.
- Broad Close Advice/Shadow Replay/CLI/agent/notification regression: `593 passed`
  with six pre-existing deprecation warnings.
- Ruff on all changed Python/test files: passed.
- `git diff --check`: passed.
- DeepReview: `docs/reviews/code-review-20260723-015252.md`; no unresolved
  material findings.

## Review notes resolved before acceptance

- A post-expiry spot was initially eligible to become an expiry spot; expiry
  marking is now exact-date only.
- Manual `--as-of` initially labeled required-data quotes without proving their
  historical time; such marks are now unverified and settlement fails closed.
- Lifecycle events larger than the decision lot could misallocate fees; exact
  contract quantity is now required.
- Assignment/called-away initially allowed full-lifecycle P&L as a fallback;
  only decision-time-sliced P&L is now usable.
- A decision-time-sliced assignment P&L without an episode/time binding could
  be reused across multiple observations of the same lot; numeric lifecycle
  outcomes now require an exact episode binding.
- A later settlement without the original lifecycle input could initially
  overwrite a usable terminal result with an inconclusive row; close outcome
  merging now preserves usable evidence against missing-evidence downgrades.
