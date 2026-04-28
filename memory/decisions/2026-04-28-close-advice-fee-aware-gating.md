# Close Advice fee-aware gating and output consistency

- Date: 2026-04-28
- Scope: `close_advice`

## Decision

- `close_advice` now treats buy-to-close fee as decision-authoritative.
- If `realized_if_close` becomes `<= 0` after fee, the row is downgraded to `none` with `not_profitable_after_fee`.
- `notify_rows` now means rows actually rendered into `close_advice.txt`, after tier filtering and per-account truncation.
- Mixed-account standalone output is rendered in separate account sections instead of inheriting the first row's account header.
- Quote issue summaries now include spread-quality blockers (`spread_too_wide`, `invalid_spread`).

## Why

- Gross premium capture alone can produce false-positive close advice on low-premium contracts once Futu fees are applied.
- Previous markdown and counters could diverge from actual user-visible output.
- Spread-blocked rows are operationally equivalent to quote-quality failures and need to surface in fallback summaries.
