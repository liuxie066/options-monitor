# Option Performance v1 Migration — Completed

## Status

The migration completed in v1.4.11 on 2026-07-23. This file is a historical
field-mapping note, not an active migration or rollback procedure.

Current public authority:

- Tool Gateway: `option_performance_report`
- Portfolio integrations: `portfolio_pnl_bridge` and `portfolio_cash_bridge`
- Namespaces: `activity`, `cash`, `pnl`, `capital`, and
  `assignment_lifecycle`

Removed public surfaces:

- `monthly_income_report`
- `./om option-positions report monthly-income`
- `portfolio_capital_bridge`
- legacy Assistant and analysis projections built around ambiguous
  `net_income_cny` or generic return fields

Do not recreate these adapters for rollback. Runtime facts remain in the
canonical ledger and are read through the current Option Performance contract.

## Current Period Contract

The public period selector supports:

- `mtd`
- `ytd`
- `month` plus `YYYY-MM`
- `year` plus `YYYY`
- `range` plus inclusive local start/end dates

Reporting dates use `Asia/Shanghai`; the engine evaluates a half-open UTC
millisecond interval. Historical reads do not fill missing evidence with
current values.

## Namespace Rule

The namespaces answer different questions and must not be added or subtracted
to manufacture a residual:

| Question | Current namespace | Primary fields |
|---|---|---|
| Profit / PnL | `pnl` | `period_total_net`, `period_total_gross`, `realized_net`, `realized_gross` |
| Cash movement | `cash` | `total_cash_change_net`, `option_trade_cash_gross`, fees, settlement and sale cash |
| Premium / activity | `activity` | `premium_collected_gross`, `premium_paid_gross`, opened/closed contracts |
| Capital efficiency | `capital` | Explicit realized or period-total annualized efficiency fields |
| Assigned stock | `assignment_lifecycle` | Ending lots, sales, review and lifecycle evidence |

`premium_collected_gross` is activity, not additional profit. Assignment or
exercise stock principal is cash movement and an asset conversion, not option
PnL.

## Historical Field Mapping

The left-hand names below may appear in old artifacts only.

| Historical field/view | Current interpretation |
|---|---|
| `monthly_income_report.return_summary[].net_income_cny` | Use `cash.option_trade_cash_gross.cny` for the narrow trade-cash question, or `cash.total_cash_change_net.cny` for complete cash movement; never call it profit |
| `premium_income_cny` / `premium_received_gross` | `activity.premium_collected_gross` |
| `realized_pnl_cny` | Choose `pnl.realized_gross` or `pnl.realized_net` explicitly |
| `realized_return_rate` / `net_return_rate` | Choose an explicit `capital.*_annualized_efficiency` field |
| `assignment_stock_net_cashflow_gross` | `cash.stock_settlement_cash_gross` |
| `assignment_lifecycle_rows` | `assignment_lifecycle.ending_lots`, `.sales`, and `.review` |
| `account_monthly_income_components` | Separate activity, cash, and PnL component views |
| `symbol_income_attribution` | Current fact-row attribution with an explicit source namespace |

## Evidence Rules Preserved by the Migration

- Native-currency facts are authoritative.
- Cash CNY snapshots use event-time FX evidence captured when the event is
  written.
- Missing event-time FX keeps CNY `null/partial`; current rates must not
  backfill history.
- Missing fees preserve gross metrics but keep affected net metrics
  `null/partial`.
- Missing marks preserve realized metrics but keep unrealized and period-total
  metrics incomplete.
- A configured account scope with no events can be observed zero; an arbitrary
  unconfigured scope is not proof of completeness.

## Current References

- [Option Performance Contract](../OPTION_PERFORMANCE_DESIGN.md)
- [Assigned Stock Return Design](../ASSIGNED_STOCK_RETURN_DESIGN.md)
- [Tool Reference](../TOOL_REFERENCE.md)
- [Ledger Architecture](../LEDGER_ARCHITECTURE.md)

The original phased implementation and review evidence remains under
`docs/gateflow/` and `docs/reviews/`. Those artifacts explain history but do
not override current source, tests, or public schemas.
