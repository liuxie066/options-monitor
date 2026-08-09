# Gateflow Slice S6 Implementation

- Work unit: `ai-decision-advice-drift-remediation`
- Slice: `S6 - Explicit authority handoff and end-to-end Advice orchestration`
- Base checkpoint: `ae175d4a feat(ai-advice): accept drift remediation S5`
- Status: implementation complete; slice DeepReview complete

## Implemented contract

- Replaced Advice path guessing and legacy `portfolio_context` / option-lot
  adapters with explicit candidate snapshot, prepared PM distribution and
  prepared option-context arguments.
- Loaded and strictly validated the prepared option manifest/payload once for
  the Advice handoff, preserving legal empty positions while turning invalid or
  unavailable authority into a deterministic soft gap.
- Added internal-only per-account authority maps to the Tick notification
  request. The notification flow selects exactly one account and validates the
  PM artifact and option manifest bindings before passing objects to Daily Brief.
- Reused the already validated opening candidate snapshot in both Daily Brief
  rendering and Advice; the Advice orchestration no longer reads account state
  artifacts.
- Kept operational `portfolio_context` and `option_positions_context` reads only
  for existing funds/capacity rendering. Neither legacy object enters Advice.
- Froze the evidence index at Advice start for the complete relevant symbol
  union: accepted candidates, verified PM stock assets and verified prepared
  option underlyings. Missing identity bindings fail closed.
- Published an anonymous current-market observation partition from configured
  scan symbols, PM stocks, open option underlyings and accepted candidates. The
  locked publisher preserves other-market partitions and persists no account,
  source, quantity or weight.
- Preserved the legal zero-candidate no-model path and added defensive failure
  isolation at both orchestration and Daily Brief boundaries so Advice failure
  cannot suppress the ordinary receipt.
- Kept delivery-only notification retries independent from all Advice authority
  maps and model work.

## Focused validation evidence

```text
python3 -m pytest -q \
  tests/test_ai_decision_advice_*.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_notification_flow.py \
  tests/test_tick_account_execution_barrier.py \
  tests/test_multi_account_tick.py
349 passed in 5.51s

ruff: passed
py_compile: passed
git diff --check: passed
```

Coverage includes explicit no-reread behavior, account isolation, missing PM
authority maps, delivery-only non-consumption, one validated option handoff,
legal zero candidates, provider timeout isolation, evidence immutability and the
four-source cross-market observation contract.

## DeepReview repair

The initial aggregate review recorded three accepted findings. The repair now:

- treats only explicit, count-consistent Candidate Engine `no_candidate`
  outcomes as legal zero candidates; market closure, partial data and
  unavailable/not-applicable families remain unavailable;
- carries the earliest actual usable evidence `last_success_at` as
  `evidence_as_of`, while keeping refresh-only timestamps outside semantic
  reuse hashes;
- replaces an observation partition only from successful accounts with an
  accepted same-run candidate snapshot, preserving the prior generation when
  the Tick failed, skipped or produced no accepted seal.

## Residual boundary

- Account pipeline consumers retain their existing prepared-option reads for
  operational scan and close-advice work. S6 adds no Advice-side reread and does
  not change those pre-existing consumers.
- No provider call, real notification, runtime configuration mutation, release,
  remote upgrade or deployment is part of S6.
- Receipt material-diff policy, Agent read parity and final design/operator
  documentation remain S7-owned.
