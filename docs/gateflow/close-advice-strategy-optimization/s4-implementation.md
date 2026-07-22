# Gateflow S4 Implementation — Close Advice Strategy Optimization

- Gate: `implementation`
- Slice: `S4 — Paired policy analysis and review report`
- Status: `accepted after DeepReview`
- Production selector/notification mutation: `none`

## Delivered

- Extended the existing Shadow Replay `analyze` read surface with an optional
  `shadow_replay_close_policy_analysis.v1` report. Candidate-only analysis keeps
  its prior shape.
- Consumes the exact promotion-usable episode/outcome keys emitted by S3c
  readiness; analysis has no parallel eligibility rule.
- Compares P0/P1/P2/P3 at episode grain and reports P1/P2/P3 paired deltas
  against P0 on identical outcome rows.
- Reports count, mean, median, P5/P95 and coverage for premature-close regret,
  avoided-loss benefit, determinate-action net incremental value, close/switch
  transaction cost, and P3 same-horizon switch outcomes.
- Reports close precision, false urgency, path maximum-adverse/P95 tail,
  assignment/called-away willingness alignment, repeated actionable reminders,
  action transitions, and a threshold-free missed-review diagnostic.
- Emits aggregate, profile/family, market, account, and earliest-episode per
  unique-lot rollups while retaining hold/review/not-evaluable evidence.
- Adds nine one-factor-at-a-time sensitivity scenarios: strong annualized
  3%/4.5%/6%, medium 5%/7%/9%, and capture thresholds at -5/current/+5 points.
- Explicitly emits no automatic policy winner or parameter recommendation;
  `review` is never imputed as a trade.

## Evidence integrity additions

- Readiness now publishes its exact mechanical analysis-eligibility keys.
- New episodes preserve exact decision-time DTE/capture/remaining-annualized
  inputs for sensitivity. Old episodes missing those facts stay inconclusive;
  DTE is never reconstructed from UTC date and expiration.
- New episodes preserve current close spread slippage. Close transaction cost
  is fee plus spread slippage; switch cost additionally includes replacement
  open/exit fees and observed entry slippage.
- P3 selects the longest mechanically usable row with a same-horizon
  replacement result and compares P0 on that identical row.

## Safety and compatibility

- Analysis is read-only and offline. It does not write runtime config, trade or
  position state, notifications, or broker-facing data.
- No production selector or notification behavior changed.
- Under-ready datasets stop at `blocked_mechanical_readiness`; under-sampled
  segments remain listed as shadow-only.
- Output remains descriptive and keeps all inconclusive reasons and coverage
  denominators visible.

## Verification

- Focused Close Advice analysis/readiness/capture/outcomes and Shadow Replay:
  `73 passed`.
- Broad Close Advice/Shadow Replay/CLI/agent/notification regression:
  `606 passed` with six pre-existing deprecation warnings.
- Ruff on all changed Python/test files: passed.
- `git diff --check`: passed.
- DeepReview: `docs/reviews/code-review-20260723-023150.md`; no unresolved
  material findings.

## Completion signal

A deterministic paired report now shows current-vs-proposed actions and
outcomes without selecting a winner or reconstructing unavailable historical
facts. The next gate is S5 evidence collection and the durable CEO policy
decision; production implementation remains blocked until that approval.
