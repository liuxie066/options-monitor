# Close Advice Contract

Close advice is an exit-decision system. It does not open positions, roll
contracts, tune strategy parameters, or change ledger state.

## Goal

For each open option lot, close advice answers:

1. Can this lot be priced reliably?
2. Does the original opening thesis still hold?
3. If not, what exit action improves the position or combo risk/reward?

Every actionable row must expose the exit nature explicitly:

```text
profit_capture  lock in a profitable short-premium exit
risk_exit       reduce short-vol / short-gamma risk after the thesis weakens
take_profit     realize a long-call convexity gain
salvage         recover residual long-call value
let_expire      residual long-call value is too small to sell
hold            original thesis still holds or exit is not worthwhile
not_evaluable   pricing or thesis data is insufficient
```

## Architecture

```text
PositionLot + Quote + StrategySnapshot
-> StrategyResolver
-> SourceDataReadiness
-> PricingQuality
-> ThesisEvaluator
-> LegAdapter
-> ComboEconomics
-> ActionMapper
-> Renderer / CSV / Trace
```

## Ownership

| Component | Owns | Must not own |
|---|---|---|
| `domain.domain.close_advice` | Deterministic thesis evaluation and exit-state contract | Runtime file I/O, current config lookup, notification text |
| `src.application.strategy_policy` | Strategy resolution from lot metadata and symbol config | Exit decisions |
| `src.application.close_advice_runner` | Loading positions/quotes, pairing combo legs, action-policy mapping, CSV/text rendering | Inventing strategy thesis |
| Renderer | Human labels for already-decided actions | Changing exit decisions |

## Source Data Readiness

The runner prepares data according to the resolved strategy. The domain layer
receives already-assembled inputs and never fetches market data or event data.

| Resolved strategy | Required source data |
|---|---|
| `return_first` short put/call | Usable quote price, premium, contracts, multiplier, DTE |
| `short_vol` short put/call | Usable quote price plus IV, delta, realized volatility estimate, and event source status |
| Yield-enhancement short put | Same as its resolved sell-put profile |
| Yield-enhancement long call | Usable quote price plus long-call cost/value inputs; RV and event source are not required |

If a short-vol quote row exists but lacks IV, delta, or realized volatility, the
runner treats the source data as incomplete and refreshes the contract through
OpenD with `include_realized_volatility=true` when the symbol uses a Futu source.
Event fields are merged from the run-level event snapshot; they are not a
required_data CSV cache contract.

## Strategy Source

Strategy resolution is deterministic:

1. Position lot strategy snapshot / lot metadata.
2. Current symbol config only when the lot has no strategy metadata.
3. Template defaults only as a final fallback.

`close_advice.strategy` is not a supported control. `yield_enhancement` derives
from `sell_put.strategy`; it does not define an independent strategy.

## Scenario Matrix

| Scenario | Thesis evaluator | Domain exit states | Action policy | Default action |
|---|---|---|---|---|
| Sell Put / `return_first` | Return capture | `profit_capture`, `hold`, `not_evaluable` | `standard_short_option` | `close` / `hold` |
| Sell Put / `short_vol` | Short-vol thesis | `profit_capture`, `risk_exit`, `hold`, `not_evaluable` | `standard_short_option` | `close` / `hold` |
| Covered Call / `return_first` | Return capture | `profit_capture`, `hold`, `not_evaluable` | `standard_short_option` | `close` / `hold` |
| Covered Call / `short_vol` | Short-vol thesis | `profit_capture`, `risk_exit`, `hold`, `not_evaluable` | `standard_short_option` | `close` / `hold` |
| YE short put / `income_upside_enhancement` | Return capture + YE adapter | `profit_capture`, `hold`, `not_evaluable` | `yield_enhancement_put_leg` | `close_put_keep_call` / `hold_put_keep_call` |
| YE short put / `vol_convexity_enhancement` | Short-vol thesis + YE adapter | `profit_capture`, `risk_exit`, `hold`, `not_evaluable` | `yield_enhancement_put_leg` | `close_put_keep_call` / `hold_put_keep_call` |
| YE long call / `income_upside_enhancement` | Long-call convexity | `take_profit`, `hold`, `salvage`, `let_expire`, `not_evaluable` | `yield_enhancement_long_call_leg` | `sell_call_take_profit` / `hold_call` / `sell_call_salvage` / `hold_to_expiry_or_expire` |
| YE long call / `vol_convexity_enhancement` | Long-call convexity | `take_profit`, `hold`, `salvage`, `let_expire`, `not_evaluable` | `yield_enhancement_long_call_leg` | `sell_call_take_profit` / `hold_call_as_convexity` / `sell_call_salvage` / `hold_to_expiry_or_expire` |

The action policy is resolved by a small registry in the runner. It maps an
already-evaluated `exit_state` to a user-facing `close_action`; it must not
change the thesis evaluation result.

## Combo Economics

Yield-enhancement rows must keep put-leg decisions separate from combo reporting.
The short-put buyback decision is based on the put leg thesis. Combo reporting
then deducts the long-call cost.

```text
put_leg_realized_if_close
= put_premium_received - put_buyback_cost - put_close_fee

combo_net_locked_if_close_put_keep_call
= put_premium_received - call_premium_paid - put_buyback_cost - fees

combo_net_if_close_both
= put_premium_received - call_premium_paid - put_buyback_cost + call_sell_value - fees
```

When a paired call or its cost basis cannot be resolved, the system exposes
`combo_cost_basis_status` instead of assuming zero cost. The optional
`close_both_optional` action is only emitted when the paired call exists and
`combo_net_if_close_both` is computable.

## Acceptance Matrix

| Area | Acceptance standard |
|---|---|
| Strategy priority | Lot strategy metadata has priority over current symbol config. |
| Return-first exit | Actionable exits require positive fee-adjusted profit. |
| Short-vol hold thesis | Short-vol Sell Put defaults to assignment-acceptable and Covered Call defaults to called-away-acceptable; a non-profitable buyback stays `hold` with `hold_reason_type=assignment_acceptable` or `called_away_acceptable` unless a separate risk-budget exit is explicit. |
| Short-vol risk exit | Profitable short-vol exits or explicit risk-budget cases can produce `risk_exit`; non-profitable Sell Put / Covered Call IV/RV/event signals are risk observation by default. |
| YE short put | Action is `close_put_keep_call` / `hold_put_keep_call`, never plain `close`. |
| YE long call | Action is based on convexity state, not short-premium capture rules. |
| Combo cost | Missing paired call cost is explicit and never treated as zero. |
| Combo action | `close_both_optional` requires a paired call with computable combo economics. |
| Pricing quality | Wide spreads and missing core pricing fields produce `not_evaluable`, including YE long-call legs. |
| Short-vol source data | Missing RV/IV/delta is explicit; RV refresh uses OpenD only for short-vol lots. |
| Event source data | Short-vol event source is read from the run-level event snapshot and fails closed when required by strategy config. |
| Renderer | User-facing text shows the action and exit nature. |
