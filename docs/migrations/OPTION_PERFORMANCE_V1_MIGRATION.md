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

## Reconciliation Matrix

Shadow reconciliation is a validation layer, not another reporting pipeline. `src/application/performance/reconciliation.py` compares current legacy monthly output with v1 and keeps three result classes separate.

### Exact/native checks

| Check | Legacy source | v1 source | Rule |
|---|---|---|---|
| Premium collected | `summary[].premium_received_gross` by currency | `activity.premium_collected_gross.by_currency` | Exact after period attribution is aligned. |
| Option trade cash | `summary[].net_cashflow_gross - assignment_stock_net_cashflow_gross` by currency | `cash.option_trade_cash_gross.by_currency` | Exact; stock settlement/sale principal must not leak into option cash. |
| Option realized gross | `rows[].realized_pnl_gross` keyed by close event and currency | `rows[fact_kind=realized_gross, allocation_id!=null]` keyed by source event and currency | Exact by canonical close allocation identity; assigned-stock sale PnL is excluded from this comparison. |
| Contract quantities | legacy option cashflow rows | `activity.contracts_opened/contracts_closed` | Checked independently from money. |

Exact mismatches fail the gate. Detail-row identity checks are `not_applicable`, not guessed, when either report omits rows.

### Expected semantic deltas

These differences are classified explicitly and never silently added to an equality tolerance:

| Delta code | Meaning |
|---|---|
| `actual_fee_delta` / `fee_coverage_incomplete` | v1 net uses incurred actual fee evidence; legacy values are gross or lack fee provenance. |
| `effective_time_fx_vs_legacy_static_fx` | v1 CNY selects FX at each fact's effective time; legacy CNY may use one static/latest rate. |
| `opening_ending_valuation_and_assigned_stock_lifecycle` | v1 period total includes realized plus ending minus opening unrealized and supported assigned-stock economics; legacy realized is option-close oriented. |
| `asia_shanghai_vs_legacy_month_attribution` | v1 assigns periods in `Asia/Shanghai`; the legacy builder attributes event months in UTC. Boundary samples require source-fact review rather than weakening exact checks. |
| `intentional_removal_use_explicit_capital_efficiency` | generic legacy return rates are not reproduced; consumers must select an explicit notional-days efficiency field. |

### Replay and coverage gates

- `assess_replay_determinism` compares canonical JSON and SHA-256 hashes for two historical replays from identical facts.
- `assess_report_coverage` rejects invalid envelopes, partial metrics without missing evidence, missing FX represented as a CNY number, and missing evidence represented as zero.
- Missing fee preserves gross while net remains partial/null.
- Missing FX preserves native currency while CNY remains partial/null.
- Missing marks preserve realized metrics while unrealized/period-total remains partial/null.
- No events in a configured/proven account scope becomes observed zero. An arbitrary unconfigured account is not proof of completeness and remains `not_observed`.

## Legacy Reference Allowlist

The S10 source scan covers `monthly_income_report`, `net_income_cny`, and `realized_return_rate`. Every matching Python path under `src/` must appear in the exact allowlist in `src/application/performance/reconciliation.py`; new or stale paths fail the gate. The allowed ownership classes are:

1. `deprecated_adapter_rollback` — legacy report/workflow, ledger facade, old bridge, legacy CLI, and deprecated Agent surfaces;
2. `deprecated_compatibility_projection` — analysis/Assistant projections that remain only for compatibility;
3. `candidate_strategy_domain` — candidate quote and strategy-research fields whose `net_income_cny` name is not historical performance reporting.

This is path ownership, not permission to add new legacy semantics inside an allowed file. Any new reference still requires explicit review and an allowlist update with a documented owner.

Current exact path inventory:

- deprecated adapter / rollback: `application/agent_tools/operations_impl.py`, `application/agent_tools/portfolio.py`, `application/agent_tools/positions.py`, `application/ledger/api.py`, `application/ledger/queries.py`, `application/ledger/read_model.py`, `application/portfolio_capital_bridge.py`, `application/positions/reporting.py`, `application/positions/workflows.py`, `interfaces/cli/option_positions_report.py`;
- deprecated compatibility projection: `application/agent_tools/analysis.py`, `application/agent_tools/materialization_impl.py`, `application/assistant/inbound_control.py`, `application/assistant/renderer.py`, `application/assistant/tool_bindings.py`;
- candidate / strategy domain: `application/agent_tools/candidate_rank_impl.py`, `application/covered_call_strategy_risk.py`, `application/sell_call_steps.py`, `application/sell_put_steps.py`, `application/sell_put_strategy_risk.py`, `application/shadow_replay/analysis.py`, `application/shadow_replay/candidate_impact.py`, `application/shadow_replay/capture.py`, `application/short_vol_risk_context.py`, `application/strategy_lab/experiment.py`.

Paths are relative to `src/`.

## Compatibility and Deprecation

- `monthly_income_report` remains callable but is not primary for tool selection.
- `./om option-positions report monthly-income` remains callable and emits deprecation metadata.
- Deprecated analysis aliases remain available for migration queries but expose no generic return rates.
- Candidate-domain `net_income` names are not part of this migration.
- Removal requires a later explicit work unit after consumer search, reconciliation, and operational rollback evidence pass.

## Rollback Boundary

Rollback is consumer routing only; it does not mutate or downgrade trade events, assigned-stock events, evidence facts, or performance facts.

1. Keep the v1 ledger/evidence schema and all append-only facts in place. No data migration rollback is required.
2. Temporarily route only the affected consumer to `monthly_income_report`, `./om option-positions report monthly-income`, or `portfolio_capital_bridge`.
3. Preserve deprecation warnings and label legacy cash/gross semantics; do not restore residual arithmetic or reinterpret option cash as profit.
4. Capture the failed v1 request, legacy output, v1 output, reconciliation artifact, and evidence IDs before changing routing.
5. Fix the v1 owner, replay the same facts, pass exact/expected-delta/coverage gates, then route the consumer back to v1.

The deprecated adapters are code rollback boundaries, not alternate sources of truth. Evidence schema/data remains forward-compatible and append-only.

## Removal Gate

Legacy adapters are not removed in this refactor. A later explicit work unit may remove them only when all of the following are true:

- the exact legacy-reference allowlist contains no production consumer that still requires legacy output;
- historical replay determinism and coverage/null gates pass for representative US/HK, assignment, partial-close, missing-evidence, MTD, YTD, natural-month, and natural-year samples;
- exact native reconciliations pass and every remaining delta is one of the documented semantic classifications;
- operational rollback evidence proves consumers can be restored without data rollback;
- public docs, tool manifests, CLI help, Assistant bindings, analysis aliases, and portfolio integrations no longer advertise the removed surface;
- removal receives its own review, release note, and version bump.

## Validation Checklist

1. MTD/YTD/natural month/natural year/range parsing is covered.
2. Profit questions select PnL; cash questions select cash; premium questions select activity.
3. No consumer adds premium activity to PnL or subtracts these namespaces to make “other income”.
4. Missing fee/mark/FX facts remain explicit.
5. Deprecated aliases have migration warnings and a documented replacement.
6. The source scan exactly matches `LEGACY_REFERENCE_ALLOWLIST`; no unowned or stale path remains.
7. Old/new exact checks, expected-delta classifications, replay hashes, and coverage/null gates pass.
8. Rollback remains consumer routing only, and the removal gate is explicitly deferred to a versioned later work unit.
