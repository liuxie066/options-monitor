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

## Slice Status

- S1: period, instrument, money, quality, and fee contracts implemented.
- S2-S10: pending their Gateflow implementation/review gates.
