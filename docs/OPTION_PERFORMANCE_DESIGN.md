# Option Performance v1 Design

This document is the contract source for the option-performance refactor. Implementation lands in reviewed Gateflow slices; sections are expanded as each slice is accepted.

## Period Contract

Public period kinds are `mtd`, `ytd`, `month`, `year`, and `range`. Operator-facing dates use `Asia/Shanghai`. Domain computation uses UTC Unix millisecond intervals with half-open semantics:

```text
[effective_start_at_ms, effective_end_exclusive_at_ms)
```

Past date ranges end at the next local midnight. A range ending on local today ends at `now_ms + 1`, so `valuation_end_at_ms == now_ms`. Opening valuation is the instant immediately before activity begins: `valuation_open_at_ms = effective_start_at_ms - 1`.

The public parser rejects future dates, inverted ranges, invalid calendar values, and period-specific fields that do not belong to the selected period. An internal `cutoff_ms_override` exists only for the deprecated adapter and must fall inside the normalized period.

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

All authoritative amounts remain native-currency maps. A metric with an incomplete fact removes the affected currency from that metric rather than publishing a misleading partial subtotal. No CNY conversion occurs before the valuation/FX slice.

Period, monthly, account, and symbol summaries are all reductions over the same ordered fact stream. Fact order is `(effective_at_ms, fact_kind, source_event_id, allocation_id)`. Diagnostics are scoped to the requested period/account/broker so unrelated historical or cross-account errors do not degrade a selected report, while decode/projection errors inside the selected scope remain visible as partial quality.

The application service reads immutable events only through `src.application.ledger.api`, rebuilds canonical projection without writes, and passes domain facts to the pure engine. Its S3 output is the internal `option_period_performance.core.v1` contract; the public Agent/CLI v1 envelope is added in S7.

## Slice Status

- S1: period, instrument, money, quality, and fee contracts implemented.
- S2: canonical option close allocations, signed premium economics, fee provenance/allocation, replay-stable identity, and legal ledger API implemented.
- S3: native-currency activity, option/settlement cash, realized gross/net, quality, and period/month/account/symbol reductions implemented.
- S4-S10: pending their Gateflow implementation/review gates.
