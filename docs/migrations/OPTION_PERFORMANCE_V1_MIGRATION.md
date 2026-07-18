# Option Performance v1 Migration

## Status and Scope

Option Performance v1 is the primary contract for option-period reporting. It replaces the ambiguous monthly-income contract for new consumers while retaining `monthly_income_report` and `./om option-positions report monthly-income` as deprecated adapters during the migration window.

The v1 period selector supports:

- `mtd` — month to date;
- `ytd` — year to date;
- `month` + `YYYY-MM` — complete natural month;
- `year` + `YYYY` — complete natural year;
- `range` + inclusive start/end local dates.

All reporting dates use `Asia/Shanghai`; the engine evaluates a half-open UTC millisecond interval. Historical reads do not fetch live evidence. Current partial periods may refresh quotes without persisting evidence.

## Metric Namespace Rule

The three top-level namespaces are parallel and must not be added or subtracted to manufacture a residual:

| Question | Primary namespace | Primary fields |
|---|---|---|
| “赚了多少 / 利润 / PnL” | `pnl` | `period_total_net`, `period_total_gross`, `realized_net`, `realized_gross` |
| “现金怎么变 / 现金流” | `cash` | `total_cash_change_net`, `option_trade_cash_gross`, `option_fee_cash`, `stock_settlement_cash_gross`, `assigned_stock_sale_cash_gross` |
| “收了多少权利金 / 交易活动” | `activity` | `premium_collected_gross`, `premium_paid_gross`, `contracts_opened`, `contracts_closed` |

`premium_collected_gross` is activity, not profit. Assignment/exercise stock principal is cash movement and an asset conversion, not option PnL. Net PnL is unavailable when required fee evidence is incomplete; missing evidence is not zero.

## Old-to-New Metric Matrix

| Legacy field/view | v1 replacement | Migration note |
|---|---|---|
| `monthly_income_report.return_summary[].net_income_cny` | `cash.option_trade_cash_gross.cny` for the narrow legacy cash meaning; `cash.total_cash_change_net.cny` for complete cash movement | Never describe the legacy value as profit. |
| `premium_income_cny` | `activity.premium_collected_gross.cny` | Activity only; do not add to PnL. |
| `realized_pnl_cny` | `pnl.realized_gross.cny` or `pnl.realized_net.cny` | Choose gross/net explicitly. |
| `realized_return_rate` / `net_return_rate` | `capital.period_realized_net_annualized_efficiency` or `capital.period_total_net_annualized_efficiency` | Legacy generic rates are intentionally unavailable. |
| `summary[].assignment_stock_net_cashflow_gross` | `cash.stock_settlement_cash_gross` | Settlement principal is cash movement. |
| `assignment_lifecycle_rows` | `assignment_lifecycle.ending_lots`, `.sales`, `.review` | Sell Put assigned-stock lifecycle only; unsupported inventory remains explicit. |
| `account_monthly_performance` | `option_monthly_performance` | Deprecated analysis alias remains queryable. |
| `account_monthly_income_components` | `option_activity_components`, `option_cash_components`, `option_pnl_components` | Legacy component rows are non-additive. |
| `symbol_income_attribution` | v1 fact-row attribution by namespace | Preserve `source_namespace`; never compute `cash - premium - PnL`. |

## Consumer Inventory and Decisions

| Consumer | Previous dependency | v1 decision |
|---|---|---|
| Agent public tool | `monthly_income_report` | `option_performance_report` is primary; old tool is a deprecated adapter. |
| Assistant `/income` | monthly-only intent/tool | Routes to `option_performance_report`; default MTD; accepts MTD/YTD/year/month and previous month. |
| Assistant renderer | legacy return summary and generic rates | Renders separate PnL, cash, activity, assignment-quality sections. |
| Analysis catalog/query | legacy monthly summary/component fields | Adds period/monthly performance plus activity/cash/PnL component views; legacy aliases are deprecated projections. |
| Copilot eval fixtures and answer-quality routing | preferred `monthly_income_report`; additive premium + realized example | Prefer `option_performance_report`; evidence and expected answers keep namespaces separate. |
| Human legacy CLI | `option-positions report monthly-income` legacy report builder | Deprecated alias calls the v1 service; omitted month means MTD; JSON is `option_performance_report.output.v1`. |
| Close advice | legacy generic return names | Migrated in S7 to explicit estimated fields plus deprecated alias. |
| Portfolio capital bridge | option cash carve-out via `net_income_cny` | Replaced in S9 by independent PnL and cash bridges; old bridge becomes deprecated. |
| Candidate ranking / strategy filters | candidate quote economics named `net_income` / `net_income_cny` | Intentionally retained: candidate-domain naming, not historical performance reporting. |
| Legacy position reporting/workflow | `build_monthly_income_report` | Retained only as adapter/rollback boundary until removal gate. |

## Compatibility and Deprecation

- `monthly_income_report` remains callable but is not primary for tool selection.
- `./om option-positions report monthly-income` remains callable and emits deprecation metadata.
- Deprecated analysis aliases remain available for migration queries but expose no generic return rates.
- Candidate-domain `net_income` names are not part of this migration.
- Removal requires a later explicit work unit after consumer search, reconciliation, and operational rollback evidence pass.

## Rollback Boundary

Rollback does not mutate or downgrade trade events, assigned-stock events, evidence facts, or performance facts. To roll back a consumer, point it temporarily to the deprecated adapter and retain v1 data. Do not restore residual arithmetic or reinterpret option cash as profit. Evidence schema/data remains forward-compatible and append-only.

## Validation Checklist

1. MTD/YTD/natural month/natural year/range parsing is covered.
2. Profit questions select PnL; cash questions select cash; premium questions select activity.
3. No consumer adds premium activity to PnL or subtracts these namespaces to make “other income”.
4. Missing fee/mark/FX facts remain explicit.
5. Deprecated aliases have migration warnings and a documented replacement.
6. Search results for `monthly_income_report`, `net_income_cny`, and `realized_return_rate` are classified as adapter/test/docs, candidate-domain naming, or a later approved bridge slice.
