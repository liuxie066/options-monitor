# Option Performance v1 Migration — Completed

## Status

The migration is complete. Runtime code, public tools, CLI commands, Assistant bindings,
analysis views, portfolio bridges, and tests use the canonical option-performance model.
The former monthly-income model is historical documentation only and is not a rollback path.

## Canonical model

| Question | Namespace | Primary fields |
|---|---|---|
| Profit / PnL | `pnl` | `period_total_net`, `period_total_gross`, `realized_net`, `realized_gross` |
| Cash movement | `cash` | `total_cash_change_net`, `option_trade_cash_gross`, fees, settlement and assigned-stock sale cash |
| Premium / trading activity | `activity` | `premium_collected_gross`, `premium_paid_gross`, opened/closed contracts |
| Capital efficiency | `capital` | explicitly named notional-days annualized efficiency fields |

`cash.option_trade_cash_gross` remains a canonical fact because trades really move cash.
It is not profit, total cash change, or a generic return numerator. Assignment/exercise
stock principal is an asset conversion reported separately from option PnL.

## Historical mapping

The following names describe the removed model and appear here only to help operators
interpret archived reports:

| Historical surface | Canonical replacement |
|---|---|
| `monthly_income_report` | `option_performance_report` |
| `om option-positions report monthly-income` | `om option-performance report` |
| `net_income_cny` in the old report | `cash.option_trade_cash_gross.cny` for the narrow trade-cash fact; `cash.total_cash_change_net.cny` for complete cash movement |
| generic `net_return_rate` / `realized_return_rate` | explicit `capital.*_annualized_efficiency` fields |
| `account_monthly_performance` | `option_monthly_performance` |
| `account_monthly_income_components` | `option_activity_components`, `option_cash_components`, `option_pnl_components` |
| `symbol_income_attribution` | `symbol_performance_attribution` |
| `portfolio_capital_bridge` | independent `portfolio_pnl_bridge` and `portfolio_cash_bridge` |

Candidate ranking and strategy research may still use `net_income` names for quoted
candidate economics. Those fields are a separate domain and are not historical
performance reporting.

## Removed boundaries

- The old Agent tool and human CLI command are not registered.
- `/income` routes to `option_performance_report`.
- Analysis exposes only canonical performance views.
- Portfolio integration keeps independent PnL and cash equations.
- Assigned-stock inspection uses the canonical performance adapters.
- The old report builder, capital bridge, compatibility renderer, and legacy
  reconciliation/allowlist have been removed.

## Validation contract

- MTD, YTD, natural month, natural year, and range parsing stay covered.
- Profit, cash, activity, and capital remain separate namespaces.
- Missing fee, mark, or FX evidence remains explicit rather than becoming zero.
- Historical replay determinism and report-coverage checks remain available in
  `src/application/performance/reconciliation.py`; they validate canonical reports only.
- Archived changelog and migration references are history, not callable compatibility.
