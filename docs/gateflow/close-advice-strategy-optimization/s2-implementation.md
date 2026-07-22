# Gateflow S2 Implementation — Shadow Profile Policies

- Work unit: `close-advice-strategy-optimization`
- Slice: `S2`
- Status: accepted; deep review fix and re-review passed
- Runtime boundary: P1/P2/P3 are shadow-only and unreachable from production

## Changes

- Added immutable domain `CloseDecisionFacts` and `ClosePolicyResult`.
- Added pure named evaluators for `P0_current`, `P1_semantic_split`, and
  `P2_profile_aware`.
- Kept `P3_opportunity_required` out of the domain selector and composed it in
  a narrow Shadow Replay adapter from the P2 result plus post-run reallocation
  evidence.
- Added deterministic normalization from formal Close Advice rows into the
  domain facts contract.
- Added explicit execution, fee, positive-net-economics, strategy-family,
  willingness, thesis, and Combo Yield evidence gates.

## Policy decisions encoded

- P0 preserves current exit/action semantics, including old weak/optional
  profit-capture actions and legacy `risk_exit` read-only holds.
- P1 maps strong to close, medium to review, and lower tiers to hold.
- P2 return-first follows the same strong/medium semantic split.
- P2 underwriting medium with valid thesis and unchanged willingness is hold.
  This is the exact 7%-boundary problem reported by the operator.
- Underwriting observations or revoked willingness request review at most;
  they do not invent a risk/loss exit.
- Incomplete thesis evidence cannot produce close.
- Incomplete Combo Yield group evidence downgrades an otherwise-close result
  to review.
- Long-call take-profit/salvage/hold/let-expire behavior remains unchanged.
- P3 cannot upgrade a P2 hold to close. A feasible replacement can preserve a
  P2 close or request review; an opportunity-required P2 close without a
  feasible replacement becomes hold, while inconclusive evidence is explicit.

## Production-unreachability proof

- `domain.evaluate_close_policy` rejects `P3_opportunity_required`.
- `src.application.close_advice_runner` imports neither the Shadow Replay
  adapter nor P1/P2/P3 tokens.
- No runtime config or production selector was added or changed in S2.
- Architecture tests assert the production runner source contains no shadow
  policy selector/import.

## Validation

- New shadow-policy truth table: `42 passed`.
- Close Advice domain/contract/action/reallocation/runner suite: `153 passed`.
- Notification/Daily Brief and agent contract parity: `105 passed`.
- Ruff and `git diff --check`: passed.

## Next gate

- Initial review: `docs/reviews/code-review-20260723-011017.md`.
- Accepted fix: `docs/gateflow/close-advice-strategy-optimization/s2-fix.md`.
- Re-review: `docs/reviews/code-review-20260723-011228.md`; no remaining findings.
- Next: accepted slice commit.
- S3a may consume this adapter only to capture optional point-in-time close
  episodes; it must not alter required candidate dataset files.
