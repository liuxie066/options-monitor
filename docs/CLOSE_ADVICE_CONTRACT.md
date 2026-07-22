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

## Recommendation Contract

`tier` describes how strongly the current threshold matched;
`recommendation_state` is the action authority. They are deliberately separate.
Every newly generated row exposes:

- `policy_version`: policy that produced the recommendation;
- `recommendation_state`: `hold`, `review`, `close`, or `not_evaluable`;
- `decision_basis`: stable reason token(s) for the recommendation;
- `decision_evidence_status`: completeness of the facts used by the policy.

The current production policy is `p0_current.v1`. Its projection preserves the
pre-existing behavior exactly: actionable profit-capture/take-profit/salvage
states map to `close`, existing holds remain `hold`, and insufficient pricing
remains `not_evaluable`. `review` is part of the additive contract but is not
emitted by the current production policy. Older CSV artifacts are projected as
`legacy_p0` by read surfaces only; that compatibility projection does not rewrite
the artifact or make it executable.

### Shadow policy variants

Shadow Replay may evaluate immutable `CloseDecisionFacts` with these named
variants; none is imported by the production runner or notification selector:

- `P0_current`: exact current exit/tier baseline;
- `P1_semantic_split`: strong closes, medium requests review, lower tiers hold;
- `P2_profile_aware`: applies the approved return-first and underwriting truth
  tables;
- `P3_opportunity_required`: application-layer-only composition of P2 with the
  post-run capital-reallocation shadow.

For `insurance_underwriting` (and the legacy-equivalent `short_vol` profile), a
medium profit-capture tier with valid thesis evidence and unchanged
assignment/called-away willingness is `hold`, not `close`. An observed thesis
condition or revoked willingness requests `review`; it never becomes an
automatic loss/risk exit. Strong capture closes only with a valid thesis,
complete willingness/execution evidence, usable fees, and positive net-close
economics. Incomplete paired
Combo Yield evidence downgrades an otherwise-close result to `review`.

P3 cannot be selected by `evaluate_close_policy`; the Shadow Replay adapter
composes it only after formal Close Advice and reallocation artifacts exist. A
replacement opportunity cannot upgrade a P2 hold to close.

### Shadow Replay close-decision facet

Close decisions are captured into the existing `shadow_replay_dataset.v1` as
an optional facet. Candidate replay remains unchanged when the facet is not
explicitly requested. An enabled facet adds:

- `close_decision_episodes.jsonl` (`shadow_replay_close_episode.v1`);
- `close_decision_marks.jsonl` (`shadow_replay_close_mark.v1`);
- `close_decision_outcomes.jsonl` (`shadow_replay_close_outcome.v1`).

Capture joins `close_advice.csv`, `option_positions_context.json`, the optional
reallocation shadow, and the run-scoped audit by account and stable
`position_lot_id`. The canonical run-ID timestamp is the run-start anchor;
`observed_at_utc` is the unique successful account-scoped `close_advice` audit
timestamp, because the position context is created after the run starts. A
missing or ambiguous audit timestamp, a future context/quote/replacement, or a
non-unique lot match rejects capture. Filesystem mtime and contract-field lot
inference are never fallbacks.

The material fingerprint excludes run IDs, timestamps, and rendered reason
text. Exact same-day reruns reuse the earliest episode and append source run
IDs; a date, recommendation, tier, economic bucket, thesis/willingness, or
replacement change creates a new episode. P0/P1/P2/P3 projections share the
same immutable observation, and the facet remains local/offline only.

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

## Position Lifecycle

The runner obtains one business date per run and classifies every open lot from
its canonical expiration before quote planning or strategy evaluation:

| `position_lifecycle_state` | Rule | Runtime behavior |
|---|---|---|
| `active` | DTE is greater than zero | Existing quote, evaluation, action, and notification behavior is unchanged. |
| `expiry_day` | DTE is zero | Emits the existing diagnostic `not_evaluable` contract and does not request or consume a quote for a decision. |
| `expired_open` | DTE is negative | Emits diagnostic `not_evaluable` output and is excluded from required-data planning, OpenD fallback, and event enrichment. |
| `unknown` | Canonical expiration is missing or malformed | Coverage diagnostics may run, but quote-provided DTE cannot promote the lot to active; the row remains `not_evaluable`. |

Lifecycle classification does not infer exercise, assignment, called-away, or
settlement state. Those outcomes remain ledger/reconciliation facts.

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
already-evaluated `recommendation_state` to a user-facing `close_action`, using
`exit_state` only to select the strategy-specific long-call wording. It must not
change the thesis evaluation result. During the P0 compatibility window, the
runner first projects current exit/tier facts into `recommendation_state`, so
selected rows and actions remain unchanged.

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

### Close-decision replay marks and outcomes

The optional Shadow Replay close facet keeps Close Advice evidence separate
from candidate evidence:

- `close_decision_episodes.jsonl` stores immutable decision-time facts and the
  P0/P1/P2/P3 projections;
- `close_decision_marks.jsonl` stores the current contract and, when P3 selected
  one at decision time, its replacement on the same horizon row;
- `close_decision_outcomes.jsonl` stores four horizon results plus one terminal
  result per episode.

Horizon windows are calendar-day windows relative to `observed_at_utc`:
1d=[1,2], 3d=[3,4], 7d=[7,9], and 14d=[14,17]. The first verified mark in a
window is used. An expiry quote is accepted only on the contract expiration
date; a later spot cannot be relabeled as the expiry spot.

Only a fresh OpenD fetch using its actual collection time has
`point_in_time_status=verified_fresh_collection`. A manual `--as-of` value or a
local required-data CSV without a native quote timestamp is retained with an
unverified status and settlement fails closed with
`mark_point_in_time_unverified`. This prevents a current quote from being
backdated into historical evidence.

Short-option outcomes use decision-time incremental value:

```text
close_now_cost = decision_ask * multiplier * contracts + decision_close_fee
hold_to_horizon_incremental = close_now_cost
                              - (future_ask * multiplier * contracts
                                 + future_close_fee)
close_now_incremental = 0
```

P3 replacement results use the same horizon and subtract replacement open and
exit fees plus observed entry slippage. Missing entry, exit, fee, mark, or
point-in-time evidence remains explicitly inconclusive.

Terminal precedence is canonical lifecycle evidence first, then a verified
expiration-date mark for an expired-worthless result. Assignment or
called-away rows require an explicitly decision-time-sliced lifecycle P&L for a
money outcome, bound to the episode by `episode_id` or the exact
`decision_observed_at_utc`; full-lifecycle or unbound P&L is not substituted.
Multiple lifecycle events, mismatched contract quantities, ITM expiry without canonical lifecycle,
or missing fees/prices remain inconclusive.

`om research shadow-replay mark` and `collect` remain dry-run by default;
`--write` is required for local dataset mutation. Dataset build adds the close
facet only with `--include-close-decisions`, and settlement accepts repeatable
canonical projected lifecycle evidence through `--lifecycle-path`.

### Close-decision replay readiness

Readiness is mechanical evidence accounting, not a strategy-quality verdict.
When the optional close facet exists, dataset status and replay readiness report
the following independently:

- unique decision episodes and repeated source observations;
- verified 1d, 3d, 7d, 14d, and expiry mark-window coverage;
- terminal lifecycle coverage and every inconclusive reason;
- decision and future-close fee coverage;
- complete P0/P1/P2/P3 projections and paired outcome coverage;
- decision quote, strategy context, replacement, mark, and outcome point-in-time
  provenance;
- promotion-usable coverage by `(strategy_profile, strategy_family)`.

An episode is promotion-usable only when it has a usable outcome, all four
policy projections, the exact five-row outcome matrix, fee-complete economics,
verified point-in-time evidence, and a complete profile/family segment. A P3
`review_switch` episode additionally requires a replacement sourced from a
validated same-decision run. Receipt time is never substituted for quote time.

The mechanical floor is 30 settled unique episodes overall, at least 10
promotion-usable episodes in an eligible profile/family segment, and at least
80% promotion-usable coverage within that segment. A segment below either
segment floor remains shadow-only; it does not borrow evidence from another
profile or family. Meeting the floor permits paired policy analysis only. It
does not select a winner, change production policy, or authorize notification
changes. Candidate-only Shadow Replay datasets retain their prior status shape.

### Paired close-policy analysis

`om research shadow-replay analyze` adds a close-policy report only when the
optional close facet exists. The report consumes the exact episode/outcome keys
selected by mechanical readiness; it does not recreate a second eligibility
rule. Under-sampled segments and mechanically incomplete outcomes remain in
coverage and inconclusive counts but do not enter promotion metrics.

Episode comparison uses terminal evidence when it is promotion-usable,
otherwise the longest promotion-usable fixed horizon. Close precision follows
its separate definition and uses the best promotion-usable hold horizon. P3
always compares current and replacement contracts on the same outcome row; if
that row is unavailable, switch metrics remain inconclusive.

Policy metrics report P0/P1/P2/P3 action counts, net incremental outcomes for
determinate close/hold actions, premature-close regret, avoided-loss benefit,
close and switch transaction costs, close precision, path tails,
assignment/called-away willingness alignment, repeated reminders, and action
transitions. They are emitted at aggregate, profile/family, market, account,
episode, and earliest-episode-per-unique-lot grains. Every metric includes its
population, usable count, and inconclusive count. `review` is never imputed as
either a close or a hold transaction.

Threshold sensitivity is one-factor-at-a-time and descriptive only: strong
remaining annualized maximum at 3%/4.5%/6%, medium at 5%/7%/9%, and every
current capture threshold at minus five/current/plus five percentage points.
It uses the decision-time DTE, capture ratio, and remaining annualized return
persisted in the episode. Missing historical threshold inputs remain
inconclusive; expiration and UTC timestamps are not used to reconstruct DTE.
The analysis emits no policy winner or parameter recommendation and leaves
production promotion false pending the durable CEO decision artifact.

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
