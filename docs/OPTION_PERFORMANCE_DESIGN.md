# Option Performance Contract

This document describes the current option-performance contract. Historical
Gateflow artifacts record how it was implemented, but current source, tests,
and the public `option_performance_report` schema remain authoritative.

## Period Contract

Public period kinds are `mtd`, `ytd`, `month`, `year`, and `range`. Operator-facing dates use `Asia/Shanghai`. Domain computation uses UTC Unix millisecond intervals with half-open semantics:

```text
[effective_start_at_ms, effective_end_exclusive_at_ms)
```

Past date ranges end at the next local midnight. A range ending on local today ends at `now_ms + 1`, so `valuation_end_at_ms == now_ms`. Opening valuation is the instant immediately before activity begins: `valuation_open_at_ms = effective_start_at_ms - 1`.

The public parser rejects future dates, inverted ranges, invalid calendar values, and period-specific fields that do not belong to the selected period. An internal `cutoff_ms_override` is not part of the public contract and must fall inside the normalized period.

## Instrument Identity

Valuation evidence uses account-independent market instrument identity. It never reuses `ContractKey.position_key`, which includes broker, account, and position side.

```text
option:v1|<pct(symbol)>|<option_type>|<strike>|<expiration_ymd>|<currency>|<multiplier>
stock:v1|<pct(symbol)>|<currency>
```

Symbols are canonical, currency is uppercase, dates are `YYYY-MM-DD`, and Decimal values use fixed notation without insignificant trailing zeros or exponent notation. Unknown codec versions and invalid fields fail closed. Adjusted/non-standard contracts must later prove unique market identity; they must not be silently matched as standard contracts.

## Money, Quality, and Fee Facts

- Domain money is `Decimal` quantized to `0.000001`.
- Native-currency maps are authoritative; CNY is derived only with evidence.
- Metric status is one of `observed`, `partial`, `not_observed`, or `not_applicable`.
- Missing inputs are explicit and are never coerced to zero.
- Fee basis is `actual`, `estimated`, or `missing`.
- `actual` amount zero is a real zero; a legacy zero without provenance becomes `missing`.
- Gross metrics ignore fees. Net metrics require all incurred fee components needed by that metric.

## Canonical Option Economic Allocations

The canonical ledger projection is the sole owner of option close matching and lifecycle PnL allocation. Every valid close-like event (`close`, `expire_close`, `assignment`, or `exercise`) targeting an open lot produces one stable `OptionEconomicAllocation`; downstream performance code consumes these allocations and must not independently rematch option lots.

Allocation identity is derived from the open event, close event, and deterministic projection sequence. Projection sorts immutable events by `(event_time_ms, event_id)`, excludes validly voided events before applying state transitions, and therefore produces stable allocation ordering and IDs across replay. A voided close contributes neither lot mutation nor economics; a replacement close is projected as a new allocation.

For short options, opening premium is positive cash and closing premium is negative cash. Long-option signs are the inverse. Gross realized PnL is the sum of those signed premium amounts. Assignment/exercise remains an option close allocation here; stock settlement principal and assigned-stock economics belong to later lifecycle facts and must not be treated as option loss.

Open fees are allocated proportionally by closed contracts. The final close absorbs the six-decimal rounding remainder, preserving fee conservation. When one broker close is split across multiple target lots, its close fee is also allocated proportionally and the final segment absorbs the remainder; the split fees must conserve the original event total. A legacy non-zero canonical fee is treated as actual with explicit legacy provenance; zero without provenance is missing; explicit actual zero remains complete; malformed explicit provenance fails closed as missing. Gross PnL remains available when a fee is missing or estimated, while production realized net PnL is null unless every incurred fee is actual. Estimated fees remain visible only as quality/evidence and must not enter production realized PnL.

Currency and multiplier are economic units even though they are not both part of legacy `ContractKey`. A close whose units differ from its target lot produces an error diagnostic and no economic allocation. Likewise, an otherwise valid close whose economics cannot be represented produces a diagnostic and no allocation. In both cases the pre-existing lot/risk close state transition still occurs, so reporting metadata cannot reopen production risk state. Downstream performance treats these effective closes as explicitly incomplete.

`PositionLot.realized_pnl` is retained for risk/read compatibility and is not the canonical gross or net performance amount: legacy lot behavior subtracts close-event fees but does not allocate opening fees. New reporting must use `ProjectionResult.allocations`, exposed through the application ledger API.

## Core Period Activity, Cash, and Realized PnL

The pure period engine consumes effective canonical trade events plus canonical option economic allocations. It never matches option lots. Events own direct activity and cash facts; allocations exclusively own realized option PnL.

- Short option opens create positive `premium_collected_gross` and positive option trade cash; short closes create negative option trade cash.
- Long option opens create positive `premium_paid_gross` and negative option trade cash; long closes create positive option trade cash.
- Premium activity is not PnL. Realized option PnL is recognized only at the allocation close timestamp.
- Assignment/exercise option close price zero is valid. Recorded stock settlement principal is a separate signed cash fact and never an option loss.
- Option fee cash is production-observed only from actual fee facts. Estimated or missing fees make affected net metrics partial/null while gross metrics remain available.
- Stock settlement fee must be explicitly recorded to make total cash change net complete. Missing or malformed settlement data fails closed without erasing valid option realized PnL.
- An effective close lacking a canonical allocation still counts as close activity and direct event cash, but realized gross/net are partial and explicitly missing.

The public `activity` object exposes `premium_collected_gross`,
`premium_paid_gross`, `contracts_opened`, `contracts_closed`,
`assigned_stock_shares_opened`, and `assigned_stock_shares_sold`. Close,
expiry, assignment, and exercise affect contracts and allocation PnL; they do
not manufacture a second premium-activity amount.

All authoritative amounts remain native-currency maps. A metric with an incomplete fact removes the affected currency from that metric rather than publishing a misleading partial subtotal.

Cash CNY is booked once at the event-write boundary. Canonical option
trade/assignment events store a `cash_conversion.v1` snapshot under
`raw_payload.cash_conversions`; assigned-stock sale events store the same
snapshot beside the sale fact. The snapshot records the native amount, CNY rate
and result, rate timestamp, event timestamp, and stable conversion ID in the
same transaction as the event. A nonzero foreign-currency cash fact is
converted only when the cached rate is within 24 hours of the event; otherwise
the event stores `status=pending` and `amount_cny=null`. Zero and CNY cash use
explicit identity conversions. Idempotent retries retain the original
snapshot. Report reads never re-price cash from later FX evidence, and legacy
events without a snapshot remain native-only/partial until the explicit
evidence-preserving migration is run:

```bash
./om option-performance cash-conversion backfill \
  --config-key us --account lx \
  --start-date 2026-04-01 --end-date 2026-07-24
./om option-performance cash-conversion backfill \
  --config-key us --account lx \
  --start-date 2026-04-01 --end-date 2026-07-24 --apply
```

The command defaults to dry-run, consumes only persisted performance FX facts
at or before each cash event, and enforces the same 24-hour event window.
`--apply` atomically enriches only missing/pending snapshots, never overwrites
an observed conversion, and records before/after hashes plus the selected FX
fact ID in `cash_conversion_backfill_audit`.

Premium activity, PnL, and valuation CNY use the separate
`option_performance_evidence.v1` FX facts. Therefore
`activity.premium_collected_gross.cny` and
`cash.option_trade_cash_gross.cny` deliberately have different evidence paths.

Period, monthly, account, and symbol summaries are all reductions over the same ordered fact stream. Fact order is `(effective_at_ms, fact_kind, source_event_id, allocation_id)`. Diagnostics are scoped to the requested period/account/broker so unrelated historical or cross-account errors do not degrade a selected report, while decode/projection errors inside the selected scope remain visible as partial quality.

Account breakdowns publish `pnl.option_realized_gross/net` and
`pnl.assigned_stock_realized_gross/net` from the same component fact
collections as the top-level report. Public consumers must use these fields
directly; they must not derive account option PnL by subtracting one aggregate
from another. `cash.option_trade_cash_gross` is independently reduced from
option-trade cash facts and excludes stock settlement principal/fees and
assigned-stock sale cash/fees.

Top-level `pnl.realized_gross/net` is the combined realized result of canonical
option allocations and assigned-stock facts. To keep assignment inclusion
auditable, the same engine pass also publishes additive components:

```text
pnl.option_realized_gross/net
pnl.assigned_stock_realized_gross/net

pnl.realized_* = pnl.option_realized_* + pnl.assigned_stock_realized_*
```

The components retain their own `observed|partial|not_observed` evidence
envelope. They come from the exact generation-time fact collections; the
renderer must not infer either component by subtracting totals. Period-total
PnL remains the combined option plus assigned-stock result and must be labelled
as combined in user-facing output.

The application service reads immutable events only through
`src.application.ledger.api`, rebuilds canonical projection without writes,
and passes domain facts to the pure engine. Its internal output is
`option_period_performance.core.v1`; Agent and CLI expose the public v1
envelope.

The public report also adds the deterministic
`option_performance_presentation.v1` view. It keeps gross option realized PnL
and gross option-trade cash as parallel primary metrics, exposes the same
fields per account, keeps premium as supporting activity, and places
assigned-stock PnL in a separate impact section. Every presentation amount
retains its own native-currency map, CNY value, and evidence status. Missing
evidence is reduced to category/count summaries; source event, allocation, FX,
and evidence IDs remain available only in the full audit report.

Copilot consumes a strict allowlist projection of this presentation plus
period, scope, and evidence schema state. The complete public v1 report remains
available to Agent/CLI callers, so the presentation does not replace or
recalculate canonical facts.

## Valuation and FX Evidence

Historical valuation is replayed from append-only `option_performance_evidence.v1` facts. Repository construction and report reads never run DDL. A missing schema returns `not_initialized`; only explicit evidence import/capture apply performs the idempotent v1 migration. Migration and the full evidence batch share one transaction, so any identity, correction, duplicate, or storage conflict rolls back both schema and data changes. Dry-run parses and validates the same envelope without creating or mutating the database.

Valuation facts use `OptionInstrumentKey` or `StockInstrumentKey`; FX facts use an exact base/quote pair. Structured SQLite identity columns must decode to the same canonical key as the stored `instrument_key`. Corrections are append-only and must reference an existing or earlier fact of the same identity. Self-reference, missing targets, cross-identity corrections, source-identity conflicts, and correction cycles fail closed.

Selection is deterministic at the requested valuation or event instant:

1. consider facts whose `effective_at_ms` is not after the instant;
2. remove facts superseded by an eligible correction;
3. choose the latest effective time;
4. at equal time use `manual_correction > official_close > broker_snapshot > realtime_snapshot > cache_snapshot`;
5. then choose the highest revision and stable fact ID;
6. reject evidence older than seven days.

The selected mark and FX fact IDs are returned in metric/report quality for
valuation and PnL translation. Native-currency amounts remain available when
FX is missing, while the CNY amount and affected quality become partial.
Direct cash metrics instead use the immutable event-level booking conversion
described above.

Opening and ending option inventory are projected only through the ledger application API. Boundary projection is restated using all valid canonical voids, including a later void of an earlier event, then applies economic state only through the requested boundary. This keeps historical realized activity and opening/ending inventory on the same canonical replay semantics. Remaining actual opening fee is derived from canonical close allocations rather than rematching lots.

For an open short option:

```text
unrealized_gross = (open_price - mark_price) * contracts_open * multiplier
unrealized_net   = unrealized_gross - remaining_actual_open_fee
```

The long-option price direction is reversed. Missing or non-actual opening fee leaves gross available and net partial. Period option PnL is:

```text
period_total = realized + ending_unrealized - opening_unrealized
```

Period, month, account, and symbol views reduce the same valuation facts. Valuation-only positions are included in scope, and opening/ending facts are assigned to the first/last report month respectively so monthly conservation remains explicit.

Current collection runs only for `partial_current` reports with `refresh_quotes=true`. It deduplicates account-independent option identities, rejects conflicting stored market codes, resolves missing exact codes from only the required expiration chains, and fetches the resulting exact codes in one batched snapshot path. Positive bid/ask midpoint is preferred; positive last price is the explicit fallback. Crossed, zero, ambiguous, or unavailable markets fail closed with diagnostics. Broker timestamps are accepted only when timezone-aware and not in the future; otherwise the injected `now_ms` is used with `timestamp_fallback=true`.

Current FX reuses the no-write exchange-rate adapter. A payload older than 24 hours is labelled `cache_snapshot`, and the common seven-day evidence rule still applies. Report generation never persists collected facts. It merges `live_unpersisted` facts only into the current ending valuation and exposes collection provenance. Explicit capture produces the same v1 evidence envelope for the evidence lifecycle commands.

## Sell Put Assigned-Stock Lifecycle

Supported assigned-stock inventory is projected once by
`domain.domain.assigned_stock.project_assigned_stock_lifecycle`. The
option-performance service and assigned-stock read surface consume that
projector. Application code reads assigned-stock sale events only through
`src.application.ledger.api.assigned_stock_event_log`; repository capability
probing and read failures are contained at that boundary and become explicit
diagnostics.

The supported inventory transition is deliberately narrow:

```text
short put assignment
  -> buy-side stock settlement
  -> assigned-stock lot
  -> zero or more partial/full assigned-stock sales
```

Assignment/exercise inventory outside that Sell Put buy-side transition is returned as `incomplete_inventory_basis`; the system does not invent a stock basis. External holdings are reconciliation evidence only. They never create, close, or resize a canonical assigned-stock lot.

Option premium remains owned by the canonical option allocation. Stock gross PnL uses settlement principal only:

```text
sale_realized_gross = gross_sale_proceeds - sold_settlement_principal
stock_unrealized_gross = market_value - remaining_settlement_principal
period_stock_total = sale_realized + ending_unrealized - opening_unrealized
```

Settlement and sale fees are separate facts. Production net metrics use only `actual` fee evidence, including explicit actual zero. Estimated or missing fees preserve gross and make the affected net metric partial. The settlement fee is recognized once at assignment; it is not embedded again in stock gross basis. This keeps option premium, settlement fee, sale fee, and stock price movement from being double counted.

`assigned_stock.period` consumes only the exact assigned-stock facts emitted by
the assigned-stock projector adapter. It does not infer membership from a
shared assignment source event ID, because the canonical option allocation for
the same assignment legitimately uses that ID too. This prevents option
premium realization from leaking into assigned-stock realized PnL.

Opening and ending assigned-stock projections use the same restated boundary semantics as option inventory: valid later voids are included when rebuilding an earlier boundary. Historical reports select persisted stock marks only and never fetch current stock prices. Current reports may collect deduplicated `StockInstrumentKey` marks through the current read-through collector.

Covered-call lifecycle attribution prefers an explicit `stock_lot_id` link. If no explicit link exists, FIFO attribution is allowed only when the available inventory is entirely attributable to assigned-stock lots; it is labelled `heuristic` and downgrades lifecycle quality. Mixed ordinary/assigned inventory fails closed. Reservations prevent one stock share from backing two overlapping calls. Closed-call realized PnL and open-call marked unrealized PnL are both visible in the lifecycle projection, but they are not added again to top-level performance because canonical option facts already own those economics.

## Capital Exposure and Efficiency

The report exposes capital only as continuous-time notional-days under the explicit basis
`notional_days_v1`. Each exposure is a half-open interval `[start_at_ms, end_at_ms)` and is
intersected with the normalized report window using exact milliseconds:

```text
capital_days = notional * overlap_ms / 86_400_000
```

Short puts use strike * multiplier * remaining contracts. Long options use the remaining
opening premium debit. Assigned stock uses remaining stock cost basis and reduces both shares
and basis at the exact sale timestamp. A Sell Put assignment closes put exposure and opens
assigned-stock exposure at the same timestamp. The shared assigned-stock projector publishes
the covered-call allocation identities it already validated, allowing attributed covered calls
to contribute an explicit zero-incremental segment without reimplementing attribution in the
performance engine. Naked or otherwise unallocated short calls remain unavailable.

`capital.period_total_net_annualized_efficiency` and
`capital.period_realized_net_annualized_efficiency` are reported per native currency only when
both the corresponding net PnL and a positive denominator are complete. Zero denominators,
missing net PnL, unsupported inventory basis, and unknown short-call capital are explicit in
`capital.coverage` or the efficiency envelope. No NAV, margin return, integer-day approximation,
or unqualified `return_rate` is introduced.

## Portfolio Bridge Boundary

The primary PnL and cash bridges are per-account accounting boundaries. Each PM fact payload and
each option performance report must prove the exact requested account, the same natural period end,
and `Asia/Shanghai` reporting timezone. Aggregate or missing account scope is never accepted as
single-account evidence. A nested metric is usable only when both that metric and the report-level
quality are `observed`; partial report diagnostics remain binding downstream even when a known
subtotal is still visible. Contract mismatches and partial evidence keep bridge amounts null rather
than attributing them to the requested account or treating them as zero.

## Current Implementation

- Period, instrument, money, quality and fee contracts are implemented.
- Canonical allocations own option realized PnL; assigned-stock facts publish a
  separate additive component.
- Event-time `cash_conversion.v1` snapshots own direct-cash CNY conversion.
- Historical direct-cash snapshots can be filled only through the audited,
  dry-run-first `option-performance cash-conversion backfill` entry point.
- Deterministic valuation evidence, no-write current quote collection,
  assigned-stock lifecycle, continuous-time capital-days and Combo Yield
  attribution are implemented.
- Public surfaces are `option_performance_report`, `om option-performance`,
  `analysis_catalog` / `analysis_query`, `portfolio_pnl_bridge` and
  `portfolio_cash_bridge`.
- `monthly_income_report`, `option-positions report monthly-income` and the
  mixed `portfolio_capital_bridge` have been removed. Historical migration
  notes are not callable rollback paths.

## Combo Yield Cross-Expiry Attribution

Option Performance keeps canonical accounting and management attribution separate. The existing
`activity`, `cash`, `pnl`, `capital`, assigned-stock and breakdown totals remain the source of truth.
The additive `attribution` object is a read model over those canonical facts:

```text
attribution.schema_version = option_strategy_attribution.v1
```

Three timelines must not be collapsed:

1. cash timing records the real Put credit and Call debit on their trade dates;
2. economic PnL recognizes option realized PnL from canonical close allocations and period
   unrealized changes from opening/ending valuation evidence;
3. management attribution groups those facts by strategy group and lifecycle without moving or
   duplicating them.

For staggered/diagonal Combo Yield, `strategy_group_id` owns the full structure. The current V1
requires exactly one `funding_put` lot and one `participation_call` lot. Stable identities are based
on canonical lot IDs, not expiration dates:

```text
funding_cycle:<funding-put-lot-id>
participation:<participation-call-lot-id>
assigned_stock:<stock-lot-id>
residual_tail:<strategy-group-id>:<put-close-event-id>
```

The Call opening premium remains the Participation Call cost basis. It is not an immediate loss of
the Funding Put cycle. The group-lifetime funding snapshot separately explains affordability:

```text
put_open_credit_gross
call_open_debit_gross
call_cost_funded_by_put = min(put_open_credit_gross, call_open_debit_gross)
funding_surplus = put_open_credit_gross - call_open_debit_gross
funding_ratio = put_open_credit_gross / call_open_debit_gross
```

This snapshot is informational. It is never added to period cash or PnL. Gross funding remains
visible when fee provenance is incomplete; net funding is not manufactured without actual fee
evidence.

Attribution reuses `notional_days_v1`:

```text
capital_days = sum(incremental_notional * exact_overlap_days)
average_incremental_capital = capital_days / exact_report_duration_days
annualized_efficiency = scope_matched_period_net_pnl / capital_days * 365
```

Funding Put efficiency uses only Funding Put PnL and Put strike-notional days. Participation Call
efficiency uses only Call PnL and Call premium-basis days. Group efficiency uses all attributable
group PnL and all group incremental capital-days. Assigned stock enters only with explicit canonical
`strategy_group_id` provenance and uses remaining stock cost basis.

When a report begins after the Put fully closes, the residual Call tail has isolated opening/ending
valuation boundaries and may report tail PnL and efficiency. A report that crosses the Put close
timestamp cannot split Call PnL between active-combo and residual-tail phases without an exact
transition mark; V1 reports `transition_mark_required` instead of interpolating.

Conservation is computed per native currency from Decimal source facts before serialization for
realized gross/net, opening and ending unrealized gross/net, and period total gross/net. Missing or
partial source evidence produces partial conservation, never synthetic zero. Malformed or legacy
untagged Combo groups remain in canonical totals and are explicit in attribution coverage.

`include_rows=false` only omits raw fact rows. Attribution summaries and conservation are computed
before serialization and remain identical. A proven scope with no Combo Yield group produces an
observed empty attribution object; ordinary Sell Put or Covered Call reports are not downgraded.
