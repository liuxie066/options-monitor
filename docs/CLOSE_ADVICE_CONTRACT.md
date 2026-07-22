# Close Advice Contract

Close advice is an exit-decision system. It does not open positions, roll
contracts, tune strategy parameters, or change ledger state.

## Goal

For each open option lot, close advice answers:

1. Can this lot be priced reliably?
2. Has enough short-premium profit been captured to justify buying back?
3. What strategy-specific action follows from that priced decision?

Every actionable row must expose the exit nature explicitly:

```text
profit_capture  lock in a profitable short-premium exit
take_profit     realize a long-call convexity gain
salvage         recover residual long-call value
let_expire      residual long-call value is too small to sell
hold            priced short-option exit is not worthwhile
not_evaluable   core pricing data is insufficient
```

Historical reports may contain `risk_exit`. It remains readable and renderable
for artifact compatibility, but current production evaluation does not emit it
or map it to an executable close action.

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
| `short_vol` short put/call | Same pricing inputs as `return_first`; IV, delta, RV, and event context are optional observations |
| Yield-enhancement short put | Same as its resolved sell-put profile |
| Yield-enhancement long call | Usable quote price plus long-call cost/value inputs; RV and event source are not required |

When available, the runner refreshes IV, delta, and realized volatility through
OpenD for short-vol observation. Missing observation data is explicit but does
not invalidate a priced `profit_capture` or `hold` decision. Event fields are
merged from the run-level event snapshot; they are not a required_data CSV cache
contract.

## Strategy Source

Strategy resolution is deterministic:

1. Position lot strategy snapshot / lot metadata.
2. Current symbol config only when the lot has no strategy metadata.
3. Template defaults only as a final fallback.

`close_advice.strategy` is not a supported control. `yield_enhancement` derives
from `sell_put.strategy`; it does not define an independent strategy.

## Lot Identity

Every newly generated `close_advice.csv` row carries `position_lot_id`, copied
from the canonical `position_lots.record_id`. This is the stable identity for a
specific open lot. `position_id` is not used for this purpose because multiple
lots of the same contract may share it.

`close_advice_read` preserves `position_lot_id`, and the capital-reallocation
shadow uses it for exact position-context matching whenever both inputs provide
the field. Contract fields are only a compatibility fallback for older
artifacts that do not contain a lot ID.

## Scenario Matrix

| Scenario | Thesis evaluator | Domain exit states | Action policy | Default action |
|---|---|---|---|---|
| Sell Put / `return_first` | Return capture | `profit_capture`, `hold`, `not_evaluable` | `standard_short_option` | `close` / `hold` |
| Sell Put / `short_vol` | Return capture + short-vol observation | `profit_capture`, `hold`, `not_evaluable` | `standard_short_option` | `close` / `hold` |
| Covered Call / `return_first` | Return capture | `profit_capture`, `hold`, `not_evaluable` | `standard_short_option` | `close` / `hold` |
| Covered Call / `short_vol` | Return capture + short-vol observation | `profit_capture`, `hold`, `not_evaluable` | `standard_short_option` | `close` / `hold` |
| YE short put / `income_upside_enhancement` | Return capture + YE adapter | `profit_capture`, `hold`, `not_evaluable` | `yield_enhancement_put_leg` | `close_put_keep_call` / `hold_put_keep_call` |
| YE short put / `vol_convexity_enhancement` | Return capture + short-vol observation + YE adapter | `profit_capture`, `hold`, `not_evaluable` | `yield_enhancement_put_leg` | `close_put_keep_call` / `hold_put_keep_call` |
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

buy_to_close_cost
= remaining_premium + buy_to_close_fee

close_fee_to_remaining_premium
= buy_to_close_fee / remaining_premium

combo_net_locked_if_close_put_keep_call
= put_premium_received - call_premium_paid - put_buyback_cost - fees

combo_net_if_close_both
= put_premium_received - call_premium_paid - put_buyback_cost + call_sell_value - fees
```

When a paired call or its cost basis cannot be resolved, the system exposes
`combo_cost_basis_status` instead of assuming zero cost. The optional
`close_both_optional` action is only emitted when the paired call exists, open
Put/Call quantities match, current quotes are usable, and
`combo_net_if_close_both` is computable.

Group synthesis runs only after every option leg has its own advice. It adds
`combo_group_classification`, `combo_group_status`, `combo_group_action`,
`combo_group_reason`, `combo_group_issues`, open Put/Call quantities, quote
status, and evidence scope without replacing the leg tier, reason,
`close_action`, or `strategy_exit_mode`. The option-only truth table is:

- equal open Put/Call quantities: `active_combo`;
- open Put only: `missing_call`, with Put-only display wording;
- open Call only: `residual_call`, evaluated from the current Call quote;
- quantity mismatch, missing quote/group identity, or mixed facts:
  `review_required`, with no group action or group economics;
- no open option legs: `closed`.

This synthesis never infers assignment from a close type and never invents an
assigned-stock sale, Call exercise, or future Call terminal value. Assignment
semantics remain in the separate full-lifecycle reporting path.

## Fee Evidence and Action Safety

Close Advice uses `domain/domain/fee_calc.py` as its only option-fee authority.
The dated assumptions are intentionally visible rather than presented as
account-level exact fees:

- USD uses Futu HK's fixed-package schedule and reports
  `fee_calc_status=schedule_estimate` with
  `fee_calc_basis=futu_us_fixed_package_2026-07-22`. The position contract does
  not currently carry the account's fixed/tiered platform-package selection.
- HKD uses the Tier-1 exchange tariff as an upper bound and reports
  `fee_calc_status=conservative_estimate` with
  `fee_calc_basis=futu_hk_tier1_upper_bound_2026-07-22`. The tariff is waived
  when the option price is exactly HKD 0.01.
- Missing or non-Futu broker evidence, unsupported currency, and invalid fee
  inputs are explicit non-authoritative statuses; they never silently fall back
  to USD.

`estimated_pnl_if_close_gross`, `estimated_close_fee`, and
`estimated_pnl_if_close_net` retain lifetime-P&L meaning. Long positions also
expose `net_close_proceeds`, the sell-to-close value less the estimated fee.
An existing actionable short close or long take-profit requires positive net
lifetime P&L. A long-call salvage action instead requires positive
`net_close_proceeds`, so a valid residual-value sale may still have negative
lifetime P&L. Missing fee evidence makes only an otherwise-actionable row
`not_evaluable`; it does not manufacture an action from an existing hold.

## Calibration Evidence

Short-vol rows expose the evidence needed to calibrate the existing thresholds
without changing the current action policy:

- `remaining_premium`, `buy_to_close_fee`, and `buy_to_close_cost` separate the
  reward left in the contract from the transaction cost of closing it.
- `remaining_stress_loss` is the incremental liability versus closing now under
  the existing Put downside or Covered Call upside-gap scenario;
  `remaining_reward_to_stress_loss` keeps the comparison in the lot currency.
- A replacement opportunity is used only when the lot or its
  `strategy_snapshot` explicitly supplies `replacement_annualized_return` and
  optional `replacement_source`. The runner never invents a replacement from
  candidate counts or stale reports.
- `assignment_acceptable` / `called_away_acceptable` may explicitly revoke the
  strategy default. Revocation produces `close_calibration_status=review_required`
  but does not silently create an executable close action.

`close_calibration_status` is `complete`, `partial`, `review_required`, or
`not_evaluable`. These fields are replay and manual-review evidence; production
thresholds remain unchanged until lifecycle outcomes support calibration.

## Capital Reallocation Shadow

Capital reallocation is evaluated in a separate shadow artifact after the
formal close-advice report is written:

```text
close_advice.csv
+ portfolio_capacity_shadow.csv
+ option_positions_context.json
-> close_advice_reallocation_shadow.csv
```

This path is advisory only. It does not change `close_advice.csv`, notification
text, candidate rank, position state, or runtime config. A shadow failure marks
the account run degraded but never blocks or rewrites formal Close Advice.

Replacement selection intentionally stays simple:

- Sell Put may use a different symbol, but must keep the same account, market,
  strategy family, and strategy profile. Released cash must have a reliable CNY
  conversion.
- Covered Call must additionally keep the same symbol, because closing a call
  only releases that symbol's covered shares.
- Existing candidate order is authoritative. The first matching candidate that
  becomes feasible after released capacity is used; no optimizer or reranking is
  introduced.
- The current contract is excluded. A single Combo Yield leg is not eligible for
  replacement analysis because combo economics require a group-level decision.
- Position context is joined by `position_lot_id` when available. Same-contract
  multi-lot positions must not be collapsed or reported as ambiguous merely
  because their account, symbol, expiry, strike, and option type are identical.

The shadow status is one of:

| Status | Meaning |
|---|---|
| `review_switch` | Replacement efficiency is higher and incremental yield recovers close fee, replacement open fee, and spread slippage within `min(current_dte, replacement_dte)`. |
| `hold_more_efficient` | A replacement exists, but its incremental efficiency does not recover switching cost inside the comparison horizon. |
| `no_feasible_replacement` | Formal advice is not an exit and no matching candidate fits after released capacity. |
| `exit_without_replacement` | Formal advice already supports closing, but no matching replacement is feasible. |
| `not_evaluable` | Pricing, position identity, capacity, FX, or switch-economics evidence is incomplete. |

`review_switch` is a manual-review signal, not an executable close/open pair.
Promotion into production requires closed-lifecycle replay evidence and an
explicit policy decision.

## Acceptance Matrix

| Area | Acceptance standard |
|---|---|
| Strategy priority | Lot strategy metadata has priority over current symbol config. |
| Return-first exit | Actionable exits require positive fee-adjusted profit. |
| Short-vol hold thesis | Short-vol Sell Put defaults to assignment-acceptable and Covered Call defaults to called-away-acceptable; a non-profitable buyback stays `hold` with `hold_reason_type=assignment_acceptable` or `called_away_acceptable`. Any future risk-budget exit requires a separate explicit contract. |
| Short-vol observation | IV/RV, delta, event, and path fields are explanatory only; they do not create a close action. |
| Close calibration | Remaining reward, transaction cost, stress loss, explicit replacement return, and continued willingness are exposed separately; incomplete evidence never becomes an inferred recommendation. |
| Reallocation shadow | Formal Close Advice and notifications remain unchanged; replacement analysis is written only to `close_advice_reallocation_shadow.csv`. |
| Lot identity | New formal and shadow rows expose `position_lot_id=position_lots.record_id`; legacy rows without it retain contract-key fallback. |
| Replacement selection | Existing candidate rank is preserved; no optimizer or hidden reranking is allowed. |
| Legacy risk exit | Historical `risk_exit` artifacts remain readable/renderable but are not actionable in the current policy. |
| YE short put | Action is `close_put_keep_call` / `hold_put_keep_call`, never plain `close`. |
| YE long call | Action is based on convexity state, not short-premium capture rules. |
| Combo cost | Missing paired call cost is explicit and never treated as zero. |
| Combo action | `close_both_optional` requires a paired call with computable combo economics. |
| Pricing quality | Wide spreads and missing core pricing fields produce `not_evaluable`, including YE long-call legs. |
| Short-vol source data | Missing RV/IV/delta is explicit but preserves the priced profit-capture or hold action. |
| Event source data | Short-vol event context is read from the run-level snapshot and remains observational for close actions. |
| Renderer | User-facing text shows the action and exit nature. |
