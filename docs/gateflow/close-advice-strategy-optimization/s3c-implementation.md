# Gateflow S3c Implementation — Close Advice Strategy Optimization

- Gate: `implementation`
- Slice: `S3c — Mechanical close-evidence readiness`
- Status: `accepted after DeepReview`
- Production selector/notification mutation: `none`

## Delivered

- Added optional Close Advice readiness to Shadow Replay dataset status and
  readiness output without changing candidate-only response shapes.
- Reports unique episodes, repeated observations, verified 1d/3d/7d/14d/expiry
  mark windows, terminal/lifecycle coverage, fee coverage, point-in-time
  coverage, the exact five-outcome matrix, and all inconclusive reason counts.
- Reports P0/P1/P2/P3 projection and outcome-pair coverage separately, including
  P3 same-horizon replacement coverage.
- Enforces the accepted mechanical floors: 30 settled unique episodes overall,
  10 promotion-usable episodes per eligible profile/family segment, and 80%
  usable coverage in that segment. Under-sampled segments remain shadow-only.
- Keeps policy quality explicitly `not_evaluated` and production promotion
  `false`; readiness only authorizes the later paired analysis.
- Records and audits decision-time strategy context and replacement provenance.
  Future, malformed, or missing timestamps fail closed.
- Corrected data-plan collection so receipt time is not passed as an asserted
  quote `as_of`; fresh OpenD collection retains its actual collection time.

## Safety and compatibility

- No runtime config, position/trade state, production selector, notification,
  or broker-facing state is changed.
- Candidate-only datasets keep their existing status and readiness structures.
- Suggested commands remain explicit local Shadow Replay collection,
  settlement, or analysis commands; no command is executed by status.
- All readiness decisions are deterministic evidence checks. There is no
  automatic winner, recommendation, or confidence score.

## Verification

- Focused Close Advice readiness/capture/outcomes and Shadow Replay tests:
  `67 passed`.
- Broad Close Advice/Shadow Replay/CLI/agent/notification regression:
  `600 passed` with six pre-existing deprecation warnings.
- Ruff on all changed Python/test files: passed.
- `git diff --check`: passed.
- DeepReview: `docs/reviews/code-review-20260723-021149.md`; no unresolved
  material findings.

## Review notes resolved before acceptance

- Readiness initially treated non-empty strategy and replacement timestamps as
  sufficient. It now parses timezone-aware UTC values and rejects decision
  quotes, strategy contexts, or replacement runs that occur after the decision.
- Close episodes initially retained the strategy/replacement facts but not the
  timestamps needed for an independent readiness audit. Capture now preserves
  the validated position-context time and same-run replacement provenance.
- Data-plan receipt time initially flowed into collection as an operator
  `as_of`, which made fresh OpenD marks appear asserted rather than freshly
  collected. Collection now obtains and records its real quote time.
