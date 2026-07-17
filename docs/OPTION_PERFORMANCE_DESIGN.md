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

## Slice Status

- S1: period, instrument, money, quality, and fee contracts implemented.
- S2: canonical option close allocations, signed premium economics, fee provenance/allocation, replay-stable identity, and legal ledger API implemented.
- S3-S10: pending their Gateflow implementation/review gates.
