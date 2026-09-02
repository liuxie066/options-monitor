# Option Performance Design

Status: target design; plan re-review and implementation are pending.

This document is the canonical owner of the option-performance contract. Current source and tests
remain the authority for behavior until the implementation described here is complete. The target is
an in-place replacement: there is no parallel version, compatibility payload, dual read, dual write,
or historical backfill requirement.

## Goal

Replace the mixed performance report with one option-only statistical module that:

- calculates four agreed metrics from one canonical ledger source;
- applies one calculation path to Short Put, Short Call, Long Put, and Long Call legs;
- treats strategy as attribution and grouping rather than a second calculation system;
- supports MTD and YTD opening cohorts only;
- keeps the existing public entry names and read-only behavior while replacing the business payload;
- removes PnL, stock, valuation, FX, and legacy compatibility behavior from the report path.

## Non-goals

- Realized, unrealized, gross, net, period-total, or combined PnL.
- Stock settlement principal, stock trades, stock fees, assigned-stock return, dividends, interest,
  margin movements, NAV, buying power, broker margin, or current holdings value.
- Natural-month, natural-year, or custom-range queries.
- CNY conversion or cross-currency aggregation. Native currencies remain separate.
- Market marks, quote refresh, valuation evidence, or current-price collection.
- Strategy-specific formulas or inference from matching symbol, trade date, strike, or expiration.
- Data migration, report-version selection, legacy field aliases, or preservation of the old response
  shape.
- VERSION, release, deployment, service changes, production writes, or ledger repair.

Assigned-stock reporting remains owned by its independent read model. Assignment is retained here
only as a terminal option outcome needed by the Short-option win rate.

## Success Signals

The refactor is complete only when all of the following are true:

1. The report calculation materializes one current `trade_events` snapshot through the ledger
   application API and projects it once. Position lots, economic allocations, fees, lifecycle state,
   and strategy attribution are derived from that snapshot rather than joined from independent report
   sources.
2. Every metric is reduced from the same contract-share facts. A public adapter, renderer, bridge, or
   strategy view does not recalculate cash, capital-days, or win rates.
3. For a complete currency, `option_net_cashflow.total.amount =
   option_net_cashflow.open.amount + option_net_cashflow.terminated.amount`. Each component carries
   its own status. An expired share with unresolved terminal state may leave `total` observed while
   making `open` and `terminated` partial rather than being forced into either side.
4. Parent totals and every dimension conserve contract counts, cash, and capital-days before rates are
   calculated.
5. The report contains no stock, PnL, valuation, quote, FX, or legacy-period fields.
6. `./om option-performance report`, `option_performance_report`, and `/income` keep their names and
   produce the same canonical facts for equal inputs.
7. Unsupported old parameters fail explicitly; none is silently ignored or translated.
8. Missing evidence fails closed only for metrics that depend on it, without an estimated or
   synthetic result. Missing fees make affected cash flow, return, and close-based win outcomes
   partial; they do not suppress an independently proven lifecycle outcome.
9. Every public entry preserves the same `INPUT_ERROR` or `READ_ERROR` classification for the same
   report failure and publishes no business data on error.

## Current Facts and Constraints

The existing code is physically grouped under `domain/domain/performance` and
`src/application/performance`, but its responsibilities are not isolated:

- the application service reads trade events, assigned-stock events, persisted marks, FX rates, and
  optional live quotes;
- the domain engine mixes activity, cash, realized/unrealized PnL, assigned stock, capital efficiency,
  strategy attribution, and multiple period kinds;
- the public materializer constructs another presentation projection;
- assistant renderers, analysis tools, and portfolio bridges depend directly on old nested fields;
- current strategy attribution covers only part of the target strategy vocabulary.

The canonical ledger projection already owns event ordering, void handling, lot close matching,
partial-close allocation, and stable `OptionEconomicAllocation` identities. The refactor must reuse
that owner rather than rematching trades inside performance.

Two current ownership defects must be removed before the old statistics code can be deleted:

- `domain.domain.ledger.economics` imports fee and money primitives from
  `domain.domain.performance.models`; these primitives belong to the ledger or a lower neutral domain
  module, and the ledger must not depend on performance;
- canonical allocation currently lacks one effective attribution contract after economic `adjust`,
  attribution-only `adjust`, and `void` replay, so the old performance service compensates by
  interpreting patches itself.

Historical rows may not contain complete actual-fee or strategy evidence. The report exposes this as
partial quality; it does not repair, estimate, infer, or backfill facts during a read.

## Ownership and Data Flow

```text
SQLite trade_events
  -> src.application.ledger.api: materialize once + canonical projection once
  -> effective option-lot and weighted economic-allocation facts
  -> domain.domain.performance: contract-share facts and all metric reductions
  -> src.application.performance: request/scope orchestration only
  -> option_performance_report
  -> CLI / Tool Gateway / Control / Copilot / Feishu renderers
```

Ownership boundaries:

- `src.application.ledger.api` is the only repository boundary used by the report.
- The ledger projection owns event validity, event order, effective opening economics after valid
  adjustments and voids, target-lot matching, remaining contracts, explicit strategy linkage, and
  proportional allocation with deterministic rounding.
- Fee provenance, fee facts, and domain money precision are ledger-owned or lower-level primitives.
  `domain.domain.ledger` must not import `domain.domain.performance`.
- `domain.domain.performance` owns cohort selection, contract-share state, net cash flow,
  capital-days, win counts, dimensions, aggregation, and quality.
- `src.application.performance` loads the frozen event stream, normalizes scope, and calls the domain
  owner. It does not load assigned-stock rows, evidence repositories, marks, rates, or quotes.
- Public adapters validate inputs and project the domain result. They do not calculate business facts.
- Renderers translate field labels only.

A frozen read is deliberately small: one request materializes one ordered event tuple, computes one
deterministic `ledger_input_hash`, and projects that tuple once. It does not require a snapshot table,
long-lived database transaction, new repository interface, or persisted report revision.

The report-facing full projection has two deterministic phases inside the ledger owner:

1. Validate and deduplicate the complete materialized tuple, resolve the current valid void graph, and
   fold surviving opening-economic and attribution adjustments by `(event_time_ms, event_id)` into
   their target opening facts.
2. Admit economic open/close/expiry/assignment/exercise events before `end_exclusive_at_ms`, then replay
   them once from the effective opening facts to produce lots and allocations.

The complete tuple is resolved before request scoping so a late void or adjustment cannot be detached
from its target. Tuple identity, deduplication, and the control-target graph are structural checks.
Projection facts and recoverable diagnostics carry the account and broker of their economic event or
resolved target. After that graph is resolved, `domain.domain.performance` applies the normalized
account/broker scope to both facts and diagnostics before metric quality is aggregated. A fully
resolved diagnostic outside the requested scope is ignored. A missing scope dimension that prevents
safe exclusion remains relevant to the intersecting known scope; a diagnostic that cannot be linked to
any account, broker, or safe target is a structural projection failure.

Valid void and adjust controls recorded after the cutoff may restate an already admitted target, but
they cannot admit post-cutoff cash or lifecycle events. A voided control has no effect. Opening fields
eligible for pre-replay restatement are opening time, original contracts, premium, strike, expiration,
multiplier, currency, and explicit strategy attribution. Derived-state repair fields such as
`contracts_open`, `contracts_closed`, `status`, and close metadata do not manufacture performance
allocations; if they cannot be reconciled to admitted lifecycle events, allocation coverage is
`partial`.

After adjustment folding, the effective `opened_at_ms` must be positive and no later than every
admitted terminal event for that opening lot. Its `expiration_ymd` must not precede the effective
opening date in `Asia/Shanghai`. Every occupied-capital segment is a half-open interval with
`start_at_ms <= end_exclusive_at_ms`; equality is a valid zero-duration segment. A temporal violation
makes the whole affected opening allocation unavailable with `economic_adjust_invalid` before the
performance reducer runs.

Original contracts must also be at least the admitted terminated contracts, all allocations plus the
residual-open segment must conserve original contracts, and opening cash and fee allocations must
conserve the effective opening facts. Conservation failure leaves the affected allocation unavailable
with `economic_adjust_non_conserving`; performance never repairs either failure downstream.

`PositionLot` and `OptionEconomicAllocation` are derived projections, not additional data sources.
Event-level actual fee provenance and explicit strategy linkage remain part of the canonical event
stream.

## Period and Cohort Contract

Public period kinds are exactly `mtd` and `ytd`. Dates use `Asia/Shanghai`.

- MTD starts on the first calendar day of the `as_of_date` month.
- YTD starts on January 1 of the `as_of_date` year.
- `as_of_date` is inclusive and may be T-1.
- T-1 means an explicitly selected latest complete source/trading date; the report never implements it
  as `today - 1 calendar day`.
- One `report_now_ms` is frozen at request start. When `as_of_date` is omitted or equals the current
  operating date, `end_exclusive_at_ms = report_now_ms + 1` and freshness is `current`.
- For a past `as_of_date`, `end_exclusive_at_ms` is the next Asia/Shanghai local midnight and freshness
  is `historical`.
- `start_at_ms` is the Asia/Shanghai local midnight at the MTD/YTD start date. Event admission uses the
  half-open window `[start_at_ms, end_exclusive_at_ms)`.
- `statistic_days = (end_exclusive_at_ms - start_at_ms) / 86,400,000`. It is an exact decimal duration:
  a past complete date produces whole days, while a current partial date produces fractional days.
- Automatic latest-complete-day discovery is outside this module. A caller that requires completed
  data passes the selected T-1 `as_of_date` explicitly.

Selection is by each option leg's own opening date. A leg is admitted when its opening date falls in
the requested MTD/YTD window. For an admitted leg, all canonical lifecycle cash and state transitions
before `end_exclusive_at_ms` participate. A close inside the period for a leg opened before the period
is not admitted.

Strategies do not have a separate opening date. Each member leg independently passes or fails the
cohort test.

## Canonical Statistical Leg Facts

The calculation first produces one logical fact stream at opening-lot and weighted contract-share
granularity. One fact represents an allocation segment with a positive `contracts` weight; it is not
one object per physical contract. The implementation reuses `OptionEconomicAllocation` and emits at
most one residual-open segment per lot. It does not add a persisted table or a parallel rematching
model.

Each fact must prove:

- open lot and source event identity;
- account, broker, underlying symbol, currency, multiplier, option type, and position side;
- one of four leg types: `sell_put`, `sell_call`, `buy_put`, `buy_call`;
- effective opening time, opening price, original contracts, and remaining contracts at
  `as_of_date` after canonical adjustment and void replay;
- terminal event, terminal kind, and terminal time for a terminated share;
- allocated opening cash, opening actual fee, terminal cash, and terminal actual fee;
- occupied-capital segments and capital-days;
- effective exclusive attribution strategy and canonical group/link identities;
- completeness and missing reason codes.

Opening cash and opening fees are allocated proportionally when part of a lot terminates. The final
terminated share absorbs the rounding remainder. A close spanning multiple lots allocates close cash
and its actual fee through the canonical ledger allocation; the final allocation absorbs its rounding
remainder. Cash and fee conservation must hold exactly at the domain money precision.

## Contract-Share State Transitions

```text
open
  -> partially terminated + remaining open
  -> fully terminated
  -> unresolved_after_expiry
```

Terminal kinds are buy-to-close, sell-to-close, expiry, assignment, and exercise. Valid voids remove
their superseded economic transition during canonical replay; a replacement event is a new transition.

- An open, unexpired share at `as_of_date` contributes to `open` net cash flow.
- A closed, expired, assigned, or exercised share contributes to `terminated` net cash flow.
- A partial close splits the opening cash, opening fee, capital, and contracts between those states.
- A share past its expiration date without complete, non-conflicting terminal evidence becomes
  `unresolved_after_expiry`. It is neither normal open nor terminated, is excluded from both win-rate
  sides, and makes the affected open/terminated cash split and return partial.
- Attribution binds to the opening lot. Close, expiry, assignment, and exercise inherit it.
- An economic adjustment may restate opening time, contracts, premium, strike, multiplier, currency,
  or expiration only through canonical ledger replay. It must preserve contract, opening cash, and fee
  conservation across already allocated and remaining shares and satisfy the projection's temporal
  invariants; otherwise the complete affected opening allocation fails closed.
- A later confirmed attribution-only correction may restate historical strategy grouping, but it
  never changes transaction dates, cash signs, or the leg's opening cohort.

Lifecycle admission is bounded by economic time through `end_exclusive_at_ms`. A currently valid
administrative void or correction may restate an already admitted historical event even when the
correction was recorded later; it must not introduce post-cutoff economic cash or lifecycle activity.
The request-wide `ledger_input_hash` identifies the exact current-ledger snapshot used for that
restatement.

Broker-authoritative lifecycle evidence is required. A missing or ambiguous expiration, exercise, or
assignment state is unresolved rather than guessed from expiration or holdings. Until the canonical
source proves the exact capital stop boundary for `unresolved_after_expiry`, its affected return is
partial/null rather than continuing capital to `as_of_date` or inventing a settlement delay.

## Metric Contract

### Option Net Cash Flow

The public Chinese name is **期权净现金流**.

```text
期权净现金流
  = 卖出期权成交金额
  - 买入期权成交金额
  - 实际手续费

期权净现金流
  = 持仓中期权净现金流
  + 已终结期权净现金流
```

Each native-currency component is an independent metric value:

```text
total       -> amount, status, missing
open        -> amount, status, missing
terminated  -> amount, status, missing
```

`amount` is null only when that component cannot be proved exactly. Component status does not
implicitly downgrade a complete sibling component.

Sell-to-open and sell-to-close are positive option cash. Buy-to-open and buy-to-close are negative
option cash. Fees are subtracted exactly once. Stock principal and every stock-side fee are excluded.

Only actual fee evidence, including explicit actual zero, is admissible. Estimated or missing fees do
not enter the metric. Every cash component containing a share with a required non-actual fee has null
`amount` and `partial` status; a complete sibling component remains observed. Known gross cash may
remain in audit rows but is not published as net cash flow.

Exercise follows the same fee-provenance rule: the canonical lifecycle event carries an actual option
fee, an explicit broker-authoritative zero, or a missing status. Exercise settlement is not assumed to
be fee-free merely because its stock principal is excluded.

For `unresolved_after_expiry`, complete recorded option cash may still contribute to the total net cash
flow. Because the share cannot be classified as open or terminated, the affected currency's
`total` component may remain `observed`, while `open` and `terminated` are `partial` with null amounts.
The complete-state conservation equality is not claimed.

### Short-option Win Rate

The public Chinese name is **卖出期权胜率**.

```text
卖出期权胜率
  = (到期未指派张数 + 净现金流 > 0 的买入平仓张数)
    / 已终结且结果完整的卖出期权张数
```

The bought-to-close share's net cash includes its allocated opening credit, allocated opening actual
fee, closing debit, and closing actual fee.

### Long-option Win Rate

The public Chinese name is **买入期权胜率**.

```text
买入期权胜率
  = 净收益 > 0 的卖出平仓张数
    / (结果完整的卖出平仓张数 + 到期作废张数)

卖出平仓份额净收益
  = 卖出回款
  - 分摊的买入开仓成本
  - 分摊的开仓实际手续费
  - 平仓实际手续费
```

Exercise classification uses broker-authoritative evidence only.

### Win-rate Evidence Matrix

Win-rate eligibility is determined by terminal outcome, not by one report-wide fee gate:

| Leg | Terminal outcome | Required evidence | Eligible | Win rule |
|---|---|---|---|---|
| `sell_put` / `sell_call` | expiry without assignment | complete, non-conflicting lifecycle evidence | yes | win |
| `sell_put` / `sell_call` | assignment | complete, non-conflicting lifecycle evidence | yes | not a win |
| `sell_put` / `sell_call` | buy to close | allocated opening credit, actual opening fee, closing debit, and actual closing fee | only when complete | win iff allocated net cash is greater than zero |
| `buy_put` / `buy_call` | sell to close | allocated opening cost, actual opening fee, closing proceeds, and actual closing fee | only when complete | win iff net income is greater than zero |
| `buy_put` / `buy_call` | expiry worthless | complete, non-conflicting lifecycle evidence | yes | not a win |
| `buy_put` / `buy_call` | exercise | broker-authoritative exercise evidence | no | excluded because stock economics are outside this module |
| any | open or unresolved | complete terminal outcome is absent | no | excluded |

The public value/status contract is deterministic:

| Outcome coverage in the admitted opening cohort | Proven eligible contracts | Published counts | `rate` | `status` |
|---|---:|---|---|---|
| complete | greater than zero | proven winning and eligible contracts | winning / eligible | `observed` |
| complete | zero | `winning_contracts=0`, `eligible_contracts=0` | `null` | `not_applicable` |
| incomplete because an outcome's required evidence is missing or conflicting | any | proven winning and eligible subset only | `null` | `partial` |

An unexpired open share and an authoritatively proven Long exercise are intentional exclusions and do
not make outcome coverage incomplete. `unresolved_after_expiry`, conflicting terminal evidence, or a
close-based terminal outcome missing evidence required by its net-cash rule does make coverage
incomplete. Counts remain audit facts for the proven subset, but no partial-subset rate is published.

Actual fees are therefore required for buy-to-close and sell-to-close outcomes because those win
rules depend on net cash. They are not required for expiry-without-assignment, assignment, or
worthless-expiry classification. An affected cash-flow component may be partial because a fee is
missing while an independently proven lifecycle-based eligibility and win flag remain available; the
aggregate win-rate value and status still follow the table above.

### Option Return

The public Chinese name remains **期权收益率**. The annualized companion is **年化收益率**.

```text
资本天数 = Σ(每段占用资金 × 实际占用天数)
平均占用资金 = 资本天数 / 统计天数
期权收益率 = 期权净现金流 / 平均占用资金
           = 期权净现金流 × 统计天数 / 资本天数
年化收益率 = 期权净现金流 / 资本天数 × 365
```

Actual occupied days use exact event-time overlap divided by 86,400,000 milliseconds. A partial close
reduces capital at its exact close time. A terminal share stops capital at its terminal time; an open
share runs until `end_exclusive_at_ms`.

The return numerator is the native currency's `option_net_cashflow.total.amount`; it never substitutes
the open/terminated split.

Occupied-capital basis:

- `sell_put`: strike × multiplier × remaining contracts;
- `sell_call`: strike × multiplier × remaining contracts;
- `buy_put`: opening premium × multiplier × remaining contracts;
- `buy_call`: opening premium × multiplier × remaining contracts.

The Short Call basis is a standardized statistical denominator. It is not maximum loss and does not
claim to equal broker margin. Fees affect only the numerator. Stock value, stock cost, broker margin,
buying power, collateral netting, and strategy-level margin offsets are excluded.

Returns are native-currency metrics. A complete positive capital denominator with complete net cash
produces a rate. Missing net cash or capital evidence produces `partial` with null rate. No admitted
cash and no admitted capital produces `not_applicable`. Aggregate returns sum cash and capital-days
first; rates are never averaged.

## Dimensions and Strategy Attribution

The report supports these dimensions:

- opening year and opening month, derived from the leg opening date;
- account;
- native currency;
- leg type;
- exclusive attribution strategy;
- underlying symbol.

Opening year/month are dimensions, not additional period query kinds.

Exclusive attribution strategy values are:

| Attribution | Required membership |
|---|---|
| `csp` | ordinary Short Put, including the Put that starts a later Wheel |
| `cc` | ordinary Short Call |
| `csp_lc` | explicit Short Put + Long Call group |
| `cc_lp` | explicit Short Call + Long Put group |
| `wheel` | subsequent Short Call explicitly linked to assigned stock |
| `unassigned` | bought leg without an explicit supported combination |

Default attribution is deterministic:

- an ungrouped Short Put is `csp`;
- an ungrouped Short Call is `cc`;
- an ungrouped Long Put or Long Call is `unassigned`;
- a combination is never inferred from matching symbol, date, strike, or expiration.

Combination identity:

- CSP+LC requires one explicit `strategy_group_id`, equal positive contracts, the same
  account/symbol/currency/multiplier, Put strike below Call strike, and Put expiry on or before Call
  expiry. A confirmed cross-expiry group remains valid for statistics.
- CC+LP requires one explicit `strategy_group_id`, equal positive contracts, the same
  account/symbol/currency/multiplier/expiry, and Call strike above Put strike.
- Wheel attribution requires an explicit `source_stock_lot_id`; the starting Short Put and stock are
  not members of the Wheel group.

The existing `match_post_trade_combo_pairs` reconciliation owner produces confirmable inferences for
two disjoint signed-leg families:

- Short Put + Long Call for CSP+LC;
- Long Put + Short Call for CC+LP.

The persisted inference shape remains unchanged: `put_*` fields identify the Put leg and `call_*`
fields identify the Call leg. No `pair_kind` column, new inference table, or schema version is added;
the exact family is derived from `option_type + position_side` in the two signed lot snapshots. Those
snapshots already participate in `input_snapshot_hash`, so a side or leg change invalidates
confirmation. Matching, current-ledger revalidation, confirmation, and supersede/void must accept only
the two families above and reject every other signed combination. Candidate exposure may strengthen
CSP+LC evidence; CC+LP never adopts a CSP+LC candidate-exposure grade and its structural match still
requires the same explicit human confirmation before any ledger adjustment is written.

The existing `confirm-combo` write boundary remains the only public confirmation path. It is extended
minimally rather than adding another command or identity schema:

- existing CSP+LC confirmation, role names, `combo_identity.v2` payload, and identity hash remain
  unchanged;
- CC+LP confirmation accepts exactly one Short Call and one Long Put with equal positive contracts,
  the same account/symbol/currency/multiplier/expiry, and Call strike above Put strike;
- after validating the caller's expected input hash, that same confirmation transaction writes two
  idempotent `adjust` events to `trade_events`. Both carry `strategy=combo_yield`, the same
  `strategy_group_id` from the confirmed inference and its inference identity, with roles `short_call`
  and `long_put` respectively. Event/idempotency identities are deterministic from inference identity
  plus role, and the transaction records those event identities while transitioning the inference to
  `user_confirmed`;
- CC+LP does not create or mutate `combo_identity.v2`. Its canonical proof is the surviving confirmed
  adjustment-event pair in the one ledger source.

The full ledger projection recognizes CC+LP membership only when both effective adjustment events
survive and still prove the exact pair. A missing, voided, or conflicting member leaves the Short Call
as `cc`, the Long Put as `unassigned`, and the affected strategy breakdown `partial`; valid leg totals
remain available. Repeated confirmation is idempotent, a stale expected hash is rejected, and existing
adjust/void semantics supersede or remove the pair. The read-only report consumes only the effective
`trade_events` projection; it never reads proposal or inference tables, infers historical pairs, or
backfills them.

CSP and CC parent universes are derived from leg type, not exclusive attribution:

- CSP universe contains every `sell_put`, including the Put leg attributed to CSP+LC.
- CC universe contains every `sell_call`, including CC+LP and Wheel Calls.
- bought legs never enter the CSP or CC parent-universe metrics.

One leg may therefore appear once in its parent universe and once in its exclusive strategy detail.
Those views are overlapping filters, not additive siblings.

Every breakdown sums member net cash, capital-days, winning contracts, and eligible contracts, then
recomputes return and win rates. It never averages member rates.

## Shared Metric and Row Contract

Metric status reuses the existing domain vocabulary exactly:

- `observed`: complete evidence and a defined value;
- `partial`: admitted scope exists but required evidence is incomplete;
- `not_applicable`: facts are complete but the metric has no denominator.

The shared enum also contains `not_observed`, but a successful target report does not emit it.
Failure to prove the requested ledger scope is an unavailable/error result, not a successful business
payload with an unobserved metric, bundle, or root quality.

`missing` is a deterministically sorted list of stable reason-code strings. The initial public reason
codes are `fee_missing`, `fee_estimated`, `exercise_fee_missing`, `terminal_evidence_missing`,
`terminal_evidence_conflict`, `capital_identity_missing`, `capital_non_positive`, `currency_conflict`,
`economic_adjust_invalid`, `economic_adjust_non_conserving`, and `strategy_attribution_conflict`.
Diagnostics may add event/lot identities and details, but public adapters do not rename reason codes.

Public JSON uses strings for identities, dates, enums, currencies, and reason codes; integers for
timestamps and contract counts; JSON numbers for money, capital, `statistic_days`, and rates; booleans
for eligibility/outcome flags; and `null` for unproved nullable values. Domain calculations remain
Decimal and apply domain precision before the one public serializer converts them. Objects use sorted
keys and lists use the deterministic ordering defined here.

Every breakdown member uses this metric bundle. The root publishes the same four metric objects; its
bundle-level `status` and `missing` are serialized as `quality.status` and `quality.missing`:

```text
option_net_cashflow
  by_currency[currency]
    total       -> amount, status, missing
    open        -> amount, status, missing
    terminated  -> amount, status, missing
sell_option_win_rate
  winning_contracts, eligible_contracts, rate, status, missing
buy_option_win_rate
  winning_contracts, eligible_contracts, rate, status, missing
option_return
  by_currency[currency]
    capital_days, average_occupied_capital, rate, annualized_rate, status, missing
status
missing
```

Child metric/component statuses remain authoritative and are not replaced by the bundle status. A
bundle uses this fixed aggregation:

| Bundle evidence | Bundle `status` | Bundle `missing` |
|---|---|---|
| any owned child is `partial`, or an in-scope diagnostic makes the bundle incomplete | `partial` | sorted unique union of its child and bundle-scoped reason codes |
| the scope is proven and every child is `observed` or `not_applicable` | `observed` | empty |

`not_applicable` is a child-metric result, not a bundle or root-quality result. A proven empty scope
therefore has observed zero cash/count facts, not-applicable rates, and an `observed` bundle. Every
bundle in a successful report is exactly `observed` or `partial`.

Root `quality` covers the complete requested payload: the root metric bundle and every emitted
breakdown bundle. Its `status` is `partial` when any of those bundles is `partial`; it is
otherwise `observed`. `quality.missing` is the sorted unique union of all requested bundle reason
codes and relevant scoped diagnostic reason codes. Detailed event/lot context stays in
`quality.diagnostics`. A partial breakdown may therefore make root quality partial without changing
an independently observed root metric. A structurally unsafe or unproved request scope returns an
unavailable/error result and no business payload.

Each independent breakdown is a deterministically sorted list of
`{key, <metric bundle>}` objects. `key` is the opening year, opening month, account, currency, leg type,
attribution strategy, parent universe, or symbol for that breakdown. Parent-universe lists overlap
exclusive strategy lists by design and are not additive siblings.

When `include_rows=true`, `rows` contains the weighted contract-share facts consumed by the reducer.
Each row has exactly these public fields:

```text
fact_id, open_lot_id, open_event_id, terminal_event_id,
account, broker, symbol, currency, leg_type,
attribution_strategy, strategy_group_id, source_stock_lot_id,
opened_at_ms, terminal_at_ms, expiration_ymd, terminal_kind, state,
strike, multiplier, contracts,
opening_option_cash, opening_actual_fee,
terminal_option_cash, terminal_actual_fee, option_net_cashflow,
occupied_capital, capital_days,
win_eligible, win,
status, missing
```

Nullable identities, terminal fields, money, rate, and capital fields serialize as `null` when not
proven. `state` is `open`, `terminated`, or `unresolved_after_expiry`. Rows are audit projections only;
public consumers must use the already reduced bundles rather than recalculate metrics from rows.

## Public Contract

The public entry names remain:

```text
./om option-performance report
option_performance_report
/income
```

The report remains read-only. Supported public inputs are `config_key`, optional `config_path` and
`data_config`, optional `account` and `broker`, `period=mtd|ytd`, optional `as_of_date`, and optional
`include_rows`. Old `month`, `year`, `start_date`, `end_date`, and `refresh_quotes` inputs are removed
and rejected as invalid. `/income` accepts account plus MTD or YTD only.

The business payload is replaced in place and does not expose a report-version selector or legacy
aliases. If the generic Tool Gateway envelope requires framework schema metadata, that metadata stays
outside the business calculation contract and must not create a second report implementation.

The one shared option-performance public adapter owns error translation and reuses the existing
`AgentToolError` contract:

- an invalid or removed input is `INPUT_ERROR`;
- a ledger read failure, invalid tuple identity/deduplication, unresolved control-target graph, or
  request scope that cannot be proved safely is `READ_ERROR`;
- `READ_ERROR.details.reason_codes` is a sorted unique list containing only
  `ledger_read_failed`, `ledger_tuple_invalid`, `ledger_control_graph_invalid`, or `scope_unproven`;
- raw exception text, paths, event payloads, and partial business results are not exposed in the error
  details.

Tool Gateway, CLI, Control, Copilot, and `/income` preserve that error code instead of wrapping it as
`INTERNAL_ERROR`, `TOOL_EXCEPTION`, or a successful unavailable metric. Their presentation text may
differ, but a failed framework envelope has `ok=false`, empty business `data`, and no report payload.

Minimal result shape:

```text
period
  kind, start_date, as_of_date, start_at_ms, end_exclusive_at_ms,
  statistic_days, reporting_timezone, freshness_status
scope
  config_key, accounts, brokers
option_net_cashflow
  by_currency
    total       -> amount, status, missing
    open        -> amount, status, missing
    terminated  -> amount, status, missing
sell_option_win_rate
  winning_contracts, eligible_contracts, rate, status, missing
buy_option_win_rate
  winning_contracts, eligible_contracts, rate, status, missing
option_return
  by_currency -> capital_days, average_occupied_capital, rate, annualized_rate, status, missing
breakdowns
  opening_years[], opening_months[], accounts[], currencies[], leg_types[],
  attribution_strategies[], parent_universes[], symbols[]
  # every item is {key, <shared metric bundle>}
quality
  status, missing, diagnostics, ledger_input_hash
rows[]                         # exact weighted-row contract; only when include_rows=true
```

`freshness_status` is `current` only when `as_of_date` is the current operating date. A past date,
including T-1, is `historical`. Public adapters receive the same frozen `report_now_ms`; equal inputs
and equal `report_now_ms` produce equal metric facts across Agent, CLI, Control, Copilot, and Feishu.
Presentation wording may differ.

## Failure Behavior

- Invalid period, date, account, broker, or removed input: reject with `INPUT_ERROR` before reading the
  ledger.
- Ledger read failure, invalid tuple identity/deduplication, or a control-target graph that cannot be
  resolved safely, and a request scope that cannot be proved safely: reject with `READ_ERROR` through
  the shared public adapter; publish no business payload and do not reuse cached report output. A fact
  or diagnostic that resolves to one account/broker only affects a request whose normalized scope
  includes it.
- One request must use one materialized event tuple and its `ledger_input_hash`; concurrent later
  writes may affect the next request but must not split one report across two ledger snapshots.
- Missing actual fee: null only the affected cash-flow components and dependent return. Exclude only
  buy-to-close or sell-to-close win outcomes whose net-cash test needs that fee; do not exclude a
  complete expiry-without-assignment, assignment, or worthless-expiry lifecycle outcome. A complete
  independent component remains observed. If an admitted close-based outcome is excluded, its metric
  publishes the proven counts but has `rate=null` and `status=partial`.
- Missing or conflicting terminal evidence after expiration: classify the share as
  `unresolved_after_expiry`; exclude it from win-rate eligibility, retain an independently complete
  `total`, and fail closed for `open`, `terminated`, and the affected return.
- Missing exercise fee provenance: treat the affected cash and dependent return as partial; never
  substitute zero.
- An economic adjustment that makes opening time non-positive, opening later than an admitted
  terminal event, expiration earlier than the opening operating date, or a capital segment negative
  in duration: fail the whole affected opening allocation as `economic_adjust_invalid`. A
  non-conserving adjustment fails it as `economic_adjust_non_conserving`; performance must not
  reinterpret either patch or repair it during a read.
- Missing capital identity or non-positive required economic units: retain valid cash when complete,
  but return a partial/null return.
- Strategy metadata missing or conflicting: preserve valid leg totals; make only the affected strategy
  view partial or unassigned as allowed by the deterministic defaults.
- Currency conflict between an opening lot and a lifecycle event: fail the affected economic share;
  never convert or combine currencies.
- Zero eligible contracts with complete outcome coverage: win rate is `not_applicable`, not zero
  percent. Zero eligible contracts caused by missing required outcome evidence is `partial` with a
  null rate.
- Zero capital with nonzero complete cash: return is partial/null, not infinite.
- Empty proven scope: cash and counts are observed empty/zero where mathematically defined; rates with
  no denominator are `not_applicable`; bundle and root quality are `observed`.

Reason codes and dimension rows must be stable and deterministically sorted. Quality degradation in
one resolved account, broker, currency, or strategy group must not suppress complete independent
groups. An unfiltered aggregate includes every relevant scoped degradation.

## External Consumers

All in-repository consumers must switch atomically to the new canonical fields. No consumer may derive
deleted PnL, stock cash, or CNY amounts from option net cash flow.

- The option-performance renderer, Control `/income`, Copilot projection, Tool contract, and analysis
  catalog must use the new fields without recalculation.
- `portfolio_pnl_bridge` cannot use this report after `pnl.period_total_net` is removed. Its public
  route may remain, but option PnL must be explicitly unavailable until a separate authoritative PnL
  source is designed.
- `portfolio_cash_bridge` currently requires combined option/assignment lifecycle cash in CNY. Native
  option net cash flow is not an equivalent replacement. Its public route may remain, but this report
  must not satisfy that evidence slot.
- Existing evidence-import and cash-conversion maintenance commands are outside the new report
  calculation. Their public invocation may remain for ledger audit needs, but the report must not
  read their evidence repositories. Performance-only code proven unused after the cutover is deleted.
- Assigned-stock and Wheel read models remain independent and unchanged except where their callers
  currently depend on deleted performance fields.

The analysis catalog has an explicit cutover disposition; it must not preserve an old view by filling
it with a differently named metric:

| Current analysis view | Cutover disposition |
|---|---|
| `option_period_performance` | Keep the view name; replace its rows with the canonical MTD/YTD opening-cohort metrics. |
| `option_cash_components` | Keep the view name; expose native-currency option net cash-flow components only, with no CNY or assignment cash. |
| `symbol_performance_attribution` | Keep the view name; project the canonical symbol breakdown and its cash, capital-days, and eligible/winning contract counts. |
| `option_monthly_performance` | Remove from the catalog; natural-month performance is unsupported. Opening month remains only a report dimension. |
| `option_activity_components` | Remove from the catalog; activity is not one of the agreed metrics. |
| `option_pnl_components` | Remove from the catalog; PnL is explicitly outside this module. |
| `assigned_stock_position_pnl`, `assigned_stock_sale_events`, `assigned_stock_lifecycle`, `assigned_stock_sales`, `assigned_stock_review` | Detach from option performance and source only from the independent assigned-stock owner. If that owner cannot satisfy a view at cutover, reject that view as unavailable rather than synthesizing it. |

The three retained analysis views are projections, not calculators. Their grains and exact fields are:

- `option_period_performance`: one row per requested period and scope, with
  `config_key`, `period_kind`, `period_start_date`, `as_of_date`, `start_at_ms`,
  `end_exclusive_at_ms`, `statistic_days`, `accounts`, `brokers`,
  `option_net_cashflow_by_currency`, `option_return_by_currency`,
  `sell_option_winning_contracts`, `sell_option_eligible_contracts`,
  `sell_option_win_rate`, `sell_option_win_rate_status`, `buy_option_winning_contracts`,
  `buy_option_eligible_contracts`, `buy_option_win_rate`, `buy_option_win_rate_status`,
  `quality_status`, `missing`, and `ledger_input_hash`;
- `option_cash_components`: one row per requested period, scope, native currency, and state, with
  `config_key`, `period_kind`, `period_start_date`, `as_of_date`, `start_at_ms`,
  `end_exclusive_at_ms`, `statistic_days`, `accounts`, `brokers`, `currency`, `state`, `amount`,
  `status`, `missing`, and `ledger_input_hash`. `state` is exactly `total`, `open`, or `terminated`;
- `symbol_performance_attribution`: one row per requested period and canonical symbol breakdown, with
  `config_key`, `period_kind`, `period_start_date`, `as_of_date`, `start_at_ms`,
  `end_exclusive_at_ms`, `statistic_days`, `accounts`, `brokers`, `symbol`,
  `option_net_cashflow_by_currency`, `option_return_by_currency`,
  `sell_option_winning_contracts`, `sell_option_eligible_contracts`,
  `sell_option_win_rate`, `sell_option_win_rate_status`, `buy_option_winning_contracts`,
  `buy_option_eligible_contracts`, `buy_option_win_rate`, `buy_option_win_rate_status`, `status`,
  `missing`, and `ledger_input_hash`.

In the period and symbol views, `option_net_cashflow_by_currency` is exactly
`currency -> {total, open, terminated}`, where every component is `{amount, status, missing}`. Each
`option_cash_components` row directly selects one of those three component objects; its `amount`,
`status`, and `missing` are copied without reinterpretation.

Analysis serialization stores arrays and maps as canonical JSON text with sorted keys. Null and metric
status semantics are the shared public semantics above. No retained view exposes an old field, creates
account/currency cross-products absent from its source breakdown, or recomputes a metric from rows.
The logical request key is (`config_key`, `period_kind`, `period_start_date`, `as_of_date`,
`start_at_ms`, `end_exclusive_at_ms`, canonical `accounts`, canonical `brokers`, `ledger_input_hash`).
It is the `option_period_performance` row key; `option_cash_components` adds (`currency`, `state`) and
`symbol_performance_attribution` adds `symbol`. Cross-view joins are safe only on the complete logical
request key; neither dates nor `ledger_input_hash` alone identify the requested scope and cutoff.

`quote_freshness` must likewise stop loading option performance or triggering report-side quote work.
It remains owned by quote/assigned-stock evidence or becomes explicitly unavailable. Removed analysis
views receive no aliases or compatibility rows.

## Implementation Slices

The implementation plan may refine file lists, but each slice must leave the repository testable and
preserve this order of responsibility:

1. **Ledger prerequisites, public report unchanged.** Move fee/money primitives out of performance,
   make economic allocations reflect canonical economic/attribution adjustments and voids, extend
   existing `match_post_trade_combo_pairs` reconciliation and `confirm-combo` with the CC+LP
   signed-leg inference/adjustment path while leaving the inference persistence shape and
   `combo_identity.v2` unchanged, admit exercise fee provenance, enforce the temporal invariants, and
   emit account/broker-addressable diagnostics. Update the projection semantic fingerprint and
   boundary tests.
2. **Private reducer, public report unchanged.** Build the minimum weighted contract-share reducer on
   `OptionEconomicAllocation`, one residual-open segment per lot, MTD/YTD cohort selection,
   `unresolved_after_expiry`, post-graph account/broker filtering of facts and diagnostics,
   deterministic aggregation, and domain tests. No old public test is replaced before the public path
   switches.
3. **Private consumer preparation, public report unchanged.** Make serializers, renderers, analysis
   mappers, and bridge unavailable responses accept a canonical reducer fixture. Keep every public
   entry bound to the old service while contract tests prove the prepared projections.
4. **Narrow atomic public pointer cutover.** Replace the application-service owner and switch CLI,
   Tool Gateway, `/income`, Control, Copilot, renderer, and analysis entry bindings to that one owner
   in the same slice. Reject removed inputs and expose bridge unavailable states. Do not delete the old
   implementation in this slice.
5. **Cleanup after facade proof.** After all public facade tests pass against the new owner, delete the
   old report engine, presentation code, and dependencies proven unused. Update indexed living docs
   and generated tool descriptions.

No slice creates a second public report, migration layer, feature flag, or release artifact.

Deletion uses an explicit keep-set. Ledger evidence-import and cash-conversion maintenance commands,
their public invocations, and any shared code they still require remain in place unless this work unit
proves them unused independently of the report. A misleading directory name alone is not deletion
evidence; relocating those maintenance capabilities is a separate work unit.

## Validation Plan

Domain checks must cover:

- all four leg types and cash signs;
- open, full close, multiple partial closes, expiry, assignment, and exercise;
- expiry passed with missing or conflicting terminal evidence, proving `unresolved_after_expiry` is
  neither normal open nor terminated and never accumulates invented capital-days;
- proportional opening cash/fee allocation and final-segment rounding conservation;
- actual, explicit-zero, estimated, missing, and malformed fee evidence;
- exercise with actual, explicit-zero, and missing fee provenance;
- economic adjustments to opening time, contracts, premium, multiplier, currency, strike, and expiry,
  including multiple/voided adjustments after partial close, opening moved after a partial or full
  close, expiration moved before opening, a valid adjustment across an MTD/YTD cohort boundary, a
  same-millisecond open/close with zero capital-days, and conservation failure;
- attribution-only correction, superseded correction, correction after `as_of_date`, and current-ledger
  restatement without post-cutoff economic activity;
- opening-cohort MTD/YTD admission and exclusion of pre-period openings;
- a past as-of date ending at the next Asia/Shanghai midnight, a current as-of date ending at the
  frozen `report_now_ms + 1`, and exact whole/fractional `statistic_days` respectively;
- open plus terminated cash conservation;
- unresolved terminal evidence with complete recorded option cash, proving `total` remains observed
  while `open` and `terminated` are independently partial/null through every facade and analysis row;
- the terminal-outcome and fee-evidence cross-product for Short expiry without assignment, Short
  assignment, Short buy to close, Long sell to close, Long worthless expiry, and Long exercise,
  proving fee-incomplete close-based outcomes are excluded while fee-independent lifecycle outcomes
  retain proven counts; mixed complete/incomplete and only-incomplete samples must publish a null,
  partial rate, while complete zero-eligible samples are `not_applicable`, identically through every
  facade and analysis row;
- all four occupied-capital bases, exact close-time reduction, and zero/missing denominator behavior;
- native-currency isolation and aggregate conservation;
- two accounts and two brokers with one scoped event/allocation diagnostic, proving an unrelated
  filtered request stays observed, the unfiltered aggregate is partial, a missing dimension cannot be
  silently excluded, and an in-scope late adjust/void still reaches its target before scope filtering;
- bundle/root quality aggregation for complete children plus `not_applicable`, a proven empty scope,
  one partial breakdown with complete root metrics, and multiple duplicate missing reasons,
  identically through every facade and retained analysis row;
- CSP, CC, CSP+LC, CC+LP, Wheel, and unassigned attribution;
- CSP/CC parent-universe overlap without additive double counting;
- unchanged CSP+LC `combo_identity.v2` payload/hash, cross-expiry CSP+LC, and an end-to-end same-expiry
  CC+LP path from reconciliation inference/preview through the two-event confirmation pair, including
  signed-leg hash changes, stale-hash rejection, repeat idempotency, supersede, one-member
  void/conflict fallback, and no second public command, heuristic grouping, or report-side writes;
- weighted multi-contract allocations and one residual-open segment without per-contract expansion;
- one request-wide event snapshot and stable `ledger_input_hash` under a concurrent later write.

Facade checks must prove equal facts through domain, application service, Tool Gateway, CLI, and
`/income`, including propagation of one frozen `report_now_ms`; removed inputs and fields must be
absent or explicitly rejected. Consumer checks must prove that bridges do not relabel net cash flow
as PnL, combined lifecycle cash, or CNY. Analysis checks must prove the three retained views expose
only their exact schemas above, removed views reject explicitly, and assigned-stock/quote views do not
call option performance.

Failure-contract checks must inject an invalid or removed input, ledger read failure, invalid tuple,
unresolved control-target graph, and unproved scope through Tool Gateway, CLI, Control, Copilot, and
`/income`. Every matching failure must preserve `INPUT_ERROR` or `READ_ERROR`, publish `ok=false` with
empty business `data`, and never reach a renderer as a successful unavailable report. A recursive
contract check must prove that no successful report payload, breakdown, audit row, or retained
analysis row contains `not_observed`, including proven-empty, partial, and child `not_applicable`
cases.

Run focused performance/ledger/CLI/Tool/assistant tests first, then the repository-required analyze,
full test, documentation guardrail, and `git diff --check` gates. Report generation tests must remain
read-only and must not fetch quotes, write evidence, modify the ledger, or send notifications.

## Rejected Alternatives

### Patch the existing engine

Rejected because the old engine's central model is event-period activity plus valuation/PnL. Removing
fields at the serializer would retain conflicting calculations, multiple sources, and direct consumer
coupling.

### Add a second report or compatibility adapter

Rejected because a v2 module, feature flag, alias payload, or dual-read path would preserve two owners
for the same money facts and make external callers diverge.

### Build calculators per strategy

Rejected because strategies share the same four leg types. Separate CSP, CC, combination, and Wheel
calculators would duplicate cash, fee, capital, lifecycle, and partial-close rules.

### Use positions, holdings, quotes, or assigned stock as report sources

Rejected because they are snapshots or different economic domains. They cannot replace the one
current canonical event snapshot for opening cohorts and lifecycle cash.

### Expand one row per physical contract

Rejected because existing economic allocations already carry a contract weight and deterministic
cash/fee allocation. Weighted segments preserve the agreed contract-share semantics without object
explosion.

### Persist report snapshots or revisions

Rejected because one materialized event tuple and its deterministic input hash provide request
consistency and audit identity. A report table, long transaction, or schema-version workflow adds a
second owner without improving the metric.

### Defer CC+LP by labeling it unassigned

Rejected for newly confirmed combinations because CC+LP is part of the target strategy vocabulary.
Its confirmed two-event adjustment pair is a prerequisite. Historical legs without that explicit
proof still follow the deterministic `cc` / `unassigned` defaults and are never inferred.

## Risks and Open Evidence

- Historical actual-fee coverage may be incomplete, so some cash flows, returns, and close-based
  win outcomes will legitimately remain partial after cutover. Lifecycle-based win counts remain
  included when their terminal evidence is complete and non-conflicting.
- The exact capital stop boundary for an expired share without terminal evidence is not yet proven by
  the current ledger contract. The application ledger lifecycle-evidence owner in
  `src/application/ledger/lifecycle_overlay.py` and its lifecycle intake path own that follow-up work
  unit. Until they publish broker-authoritative evidence, the share remains
  `unresolved_after_expiry` with partial/null return; this refactor must not invent a grace period.
- Historical CC+LP pairs without an explicit canonical group stay ungrouped; the ledger prerequisite
  supports the confirmed adjustment-event pair going forward but does not infer or backfill history.
- Existing CSP+LC post-trade and candidate validation differ on cross-expiry support. Statistics trust
  a confirmed canonical group and must not reimpose a same-expiry restriction.
- The generic Tool Gateway may currently assume a `schema_version` field. The implementation must
  separate framework metadata from the one business contract without adding report versions.
- Analysis and portfolio bridge dependencies are broad. Deletion is safe only after all current
  callers are enumerated and the disposition table is exercised through facade tests.
- The active development branch must be based on current `origin/main` before implementation; no
  design decision depends on the stale local branch state.

The only unresolved source-evidence question is the lifecycle owner's exact capital stop boundary for
`unresolved_after_expiry`; it is a named follow-up risk, not permission to guess during this work unit.
The current contract remains deliberately fail-closed. Any implementation discovery that changes
metric meaning, source authority, public invocation, or failure behavior returns to the design and
review gates before code proceeds.
