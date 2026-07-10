from __future__ import annotations

from typing import Any

from src.application.copilot.tools import TOOL_VIEWS


MODEL_SYNTHESIS_REQUIRED_FIXTURES = {
    "candidate_filter_diagnostics_model_ready",
    "close_advice_notification_diagnostics_model_ready",
    "current_option_exposure_model_ready",
    "june_option_review_model_ready",
    "june_option_review_close_advice_missing",
    "june_option_review_income_missing_current_exposure",
    "june_option_review_snapshot_only",
}


def fixture_requires_model_synthesis(fixture_id: str | None) -> bool:
    return fixture_id in MODEL_SYNTHESIS_REQUIRED_FIXTURES


def fixture_observations(fixture_id: str | None) -> list[dict[str, Any]]:
    return [_with_tool_view_context(item) for item in _fixture_observations(fixture_id)]


def _fixture_observations(fixture_id: str | None) -> list[dict[str, Any]]:
    if fixture_id == "candidate_filter_diagnostics_model_ready":
        return [
            {
                "tool_name": "runtime_status",
                "ok": True,
                "summary": "eval-only runtime status is healthy.",
                "facts": [
                    "eval_only=true",
                    "runtime.status=ok",
                    "runtime.config_key=us",
                ],
                "data": {"eval_only": True, "status": "ok", "config_key": "us"},
                "error": None,
                "evidence_ok": True,
            },
            {
                "tool_name": "candidate_filter_explain",
                "ok": True,
                "summary": "eval-only candidate filter trace rejects NVDA on Delta.",
                "facts": [
                    "symbol=NVDA canonical_symbol=NVDA trace_count=1",
                    "function=sell_put status=rejected rule=min_delta label=Delta 过低 count=1",
                ],
                "data": {
                    "eval_only": True,
                    "symbol": "NVDA",
                    "canonical_symbol": "NVDA",
                    "trace_count": 1,
                    "functions": [
                        {
                            "function": "sell_put",
                            "status": "rejected",
                            "rejection_reasons": [{"rule": "min_delta", "label": "Delta 过低", "count": 1}],
                        }
                    ],
                },
                "error": None,
                "evidence_ok": True,
            },
        ]
    if fixture_id == "close_advice_notification_diagnostics_model_ready":
        return [
            {
                "tool_name": "runtime_status",
                "ok": True,
                "summary": "eval-only runtime status has no close-advice notification content.",
                "facts": [
                    "eval_only=true",
                    "runtime.status=ok",
                    "notification_diagnosis: status=no_notification_content, reason=scan produced no account notification content, scheduler_should_run_scan=true, scheduler_should_notify=true, no_send=false, account_messages_count=0, send_attempted_count=0, send_confirmed_count=0, send_failed_count=0",
                    "notification_route: configured=true, provider=feishu, channel=webhook, target_configured=true",
                ],
                "data": {
                    "eval_only": True,
                    "status": "ok",
                    "config_key": "us",
                    "notification_diagnosis": {
                        "status": "no_notification_content",
                        "reason": "scan produced no account notification content",
                        "scheduler_should_run_scan": True,
                        "scheduler_should_notify": True,
                        "scheduler_reason": "within_window",
                        "no_send": False,
                        "account_messages_count": 0,
                        "send_attempted_count": 0,
                        "send_confirmed_count": 0,
                        "send_failed_count": 0,
                        "notification_route": {
                            "configured": True,
                            "provider": "feishu",
                            "channel": "webhook",
                            "target_configured": True,
                        },
                    },
                },
                "error": None,
                "evidence_ok": True,
            },
            {
                "tool_name": "close_advice_read",
                "ok": True,
                "summary": "eval-only close advice returned no current rows.",
                "facts": [
                    "close_advice.scope: latest_available_snapshot",
                    "close_advice.record_type: exit_signal_not_monthly_transaction_history",
                    "advice_rows=0",
                    "matched_count=0",
                    "returned_count=0",
                ],
                "data": {
                    "eval_only": True,
                    "row_count": 0,
                    "matched_count": 0,
                    "returned_count": 0,
                    "rows": [],
                },
                "error": None,
                "evidence_ok": True,
            },
        ]
    if fixture_id == "current_option_exposure_model_ready":
        return [
            {
                "tool_name": "analysis_catalog",
                "ok": True,
                "summary": "eval-only analysis catalog exposes current exposure views.",
                "facts": [
                    "eval_only=true",
                    "views=open_option_exposure,expiration_risk_buckets",
                ],
                "data": {"eval_only": True, "view_count": 2},
                "error": None,
                "evidence_ok": True,
                "claimable": False,
            },
            {
                "tool_name": "analysis_query",
                "ok": True,
                "summary": "eval-only analysis query has current open exposure rows.",
                "facts": [
                    "open_option_exposure.cash_secured_amount_by_symbol_currency: 0700.HK/HKD=180000, FUTU/HKD=30000",
                    "open_option_exposure.contracts_open_by_symbol_currency_option_type_side: 0700.HK/HKD/put/short=4, FUTU/HKD/put/short=1",
                    "expiration_risk_buckets.cash_secured_amount_by_expiration_bucket_currency: 30-60d/HKD=210000",
                ],
                "data": {"eval_only": True, "row_count": 3},
                "error": None,
                "evidence_ok": True,
            },
            {
                "tool_name": "option_positions_read",
                "ok": True,
                "summary": "eval-only current open positions show 0700.HK and FUTU short put exposure.",
                "facts": [
                    "position.cash_secured_amount_by_symbol_currency: 0700.HK/HKD=180000, FUTU/HKD=30000",
                    "position.contracts_open_by_symbol_currency_option_type_side: 0700.HK/HKD/put/short=4, FUTU/HKD/put/short=1",
                    "position[1]: account=lx, symbol=0700.HK, option_type=put, side=short, contracts_open=4, cash_secured_amount=180000 HKD",
                    "position[2]: account=lx, symbol=FUTU, option_type=put, side=short, contracts_open=1, cash_secured_amount=30000 HKD",
                ],
                "data": {"eval_only": True, "row_count": 2},
                "error": None,
                "evidence_ok": True,
            },
        ]
    if fixture_id == "june_income_attribution_basic":
        return [
            {
                "tool_name": "analysis_catalog",
                "ok": True,
                "summary": "eval-only analysis catalog exposes monthly income attribution views.",
                "facts": [
                    "eval_only=true",
                    "views=account_monthly_performance,account_monthly_income_components,symbol_income_attribution",
                ],
                "data": {"eval_only": True, "view_count": 3},
                "error": None,
                "evidence_ok": True,
                "claimable": False,
            },
            {
                "tool_name": "analysis_query",
                "ok": True,
                "summary": "eval-only analysis query has June income attribution rows.",
                "facts": [
                    "month=2026-06 account=lx net_income_cny=1200 premium_income_cny=800 realized_pnl_cny=400",
                    "symbol=0700.HK component=assignment amount_gross=600",
                    "symbol=NVDA component=premium amount_gross=800",
                ],
                "data": {"eval_only": True, "row_count": 3},
                "error": None,
                "evidence_ok": True,
            },
            {
                "tool_name": "monthly_income_report",
                "ok": True,
                "summary": "eval-only monthly income shows premium and realized components.",
                "facts": [
                    "month=2026-06 account=lx net_income_cny=1200 premium_income_cny=800 realized_pnl_cny=400",
                    "premium_income_cny=800 realized_pnl_cny=400",
                ],
                "data": {"eval_only": True, "row_count": 1},
                "error": None,
                "evidence_ok": True,
            },
        ]
    if fixture_id == "june_option_review_model_ready":
        return [
            {
                "tool_name": "analysis_catalog",
                "ok": True,
                "summary": "eval-only analysis catalog exposes monthly income, attribution, exposure, and close-advice views.",
                "facts": [
                    "eval_only=true",
                    "views=account_monthly_income_components,account_monthly_performance,close_advice_snapshot,expiration_risk_buckets,monthly_income_summary,open_option_exposure,symbol_income_attribution,trade_events",
                ],
                "data": {"eval_only": True, "view_count": 8},
                "error": None,
                "evidence_ok": True,
                "claimable": False,
            },
            {
                "tool_name": "analysis_query",
                "ok": True,
                "summary": (
                    "eval-only production-shaped analysis query has June income components, "
                    "symbol attribution, and declared snapshot gaps."
                ),
                "facts": [
                    "coverage: views=[account_monthly_income_components, account_monthly_performance, close_advice_snapshot, expiration_risk_buckets, monthly_income_summary, open_option_exposure, symbol_income_attribution, trade_events], months=[2026-06], accounts=[lx, sy], symbols=[0700.HK, 0883.HK, 3690.HK, 9992.HK, FUTU, PDD], currencies=[HKD, USD]",
                    "account_monthly_income_components.amount_cny_by_component: excluded_assignment_stock_principal=-764909.500725, realized_pnl=36703.159021, other_net_income=-32418.719743, premium_income=9599.195554",
                    "account_monthly_performance.net_income_cny_total=13883.634832",
                    "account_monthly_performance.premium_income_cny_total=9599.195554",
                    "account_monthly_performance.realized_pnl_cny_total=36703.159021",
                    "account_monthly_performance.cash_secured_cny_total=668230.139016",
                    "symbol_income_attribution.amount_gross_by_symbol_currency_component: 0700.HK/HKD/net_cashflow=-310752, FUTU/USD/net_cashflow=-46490, PDD/USD/net_cashflow=-24828, 9992.HK/HKD/net_cashflow=12624",
                    "close_advice_snapshot.row_count=0",
                    "expiration_risk_buckets.row_count=0",
                    "open_option_exposure.row_count=0",
                    "trade_events.row_count=0",
                ],
                "data": {"eval_only": True, "row_count": 50},
                "error": None,
                "evidence_ok": True,
                "evidence_context": {
                    "time_scope": "requested_filters",
                    "record_type": "approved_analysis_view_rows",
                    "use_as": "materialized analysis cross-check evidence",
                    "snapshot_views": "open_option_exposure,expiration_risk_buckets,close_advice_snapshot",
                    "snapshot_note": "snapshot views are current/latest context, not requested-month transaction history",
                },
            },
            {
                "tool_name": "monthly_income_report",
                "ok": True,
                "summary": "eval-only production-shaped monthly income report shows positive income with large assignment cash outlay.",
                "facts": [
                    "return_summary.net_income_cny_total=13883.634832",
                    "return_summary.premium_income_cny_total=9599.195554",
                    "return_summary.realized_pnl_cny_total=36703.159021",
                    "return_summary.cash_secured_cny_total=668230.139016",
                    "summary.net_cashflow_gross_by_currency: HKD=-308155, USD=-71188",
                    "summary.premium_received_gross_by_currency: HKD=7716, USD=428",
                    "summary.realized_gross_by_currency: HKD=24427, USD=2284",
                    "summary.assignment_stock_net_cashflow_gross_by_currency: HKD=-321800, USD=-71490",
                    "summary.premium_contracts_by_currency: HKD=17, USD=3",
                    "summary.closed_contracts_by_currency: HKD=28, USD=9",
                    "cashflow.net_cashflow_gross_by_symbol_currency: 0700.HK/HKD=-310752, FUTU/USD=-46490, PDD/USD=-24828, 9992.HK/HKD=12624",
                    "premium.premium_received_gross_by_symbol_currency: 0700.HK/HKD=4401, 9992.HK/HKD=2812, PDD/USD=298, 3690.HK/HKD=150",
                    "realized.realized_gross_by_symbol_currency: 9992.HK/HKD=10686, 0700.HK/HKD=6958, 3690.HK/HKD=6300, FUTU/USD=1825",
                    "assignment_lifecycle.assignment_lifecycle_pnl_by_symbol_currency: FUTU/USD=-1640, 0700.HK/HKD=-694, PDD/USD=342",
                    "assignment_lifecycle.assigned_stock_realized_pnl_by_symbol_currency: 0700.HK/HKD=3240, FUTU/USD=-2000, PDD/USD=200, PDD/HKD=0",
                ],
                "data": {"eval_only": True, "row_count": 29},
                "error": None,
                "evidence_ok": True,
                "evidence_context": {
                    "time_scope": "requested_month",
                    "record_type": "monthly_income_and_trade_event_attribution",
                    "use_as": "monthly option operation history evidence",
                    "answer_dimensions": "profit quality, assignment cash outlay",
                },
            },
            {
                "tool_name": "option_positions_read",
                "ok": True,
                "summary": "eval-only production-shaped open positions show concentrated HK short-option exposure.",
                "facts": [
                    "position.scope: current_open_positions",
                    "position.record_type: current_position_snapshot_not_monthly_transaction_history",
                    "open_position_rows=29",
                    "position.contracts_open_total=34",
                    "position.contracts_open_by_symbol_currency: 9992.HK/HKD=12, 0700.HK/HKD=11, 3690.HK/HKD=4, PDD/USD=2",
                    "position.cash_secured_amount_by_symbol_currency: 0700.HK/HKD=344000, 9992.HK/HKD=163500, 3690.HK/HKD=135000, 0883.HK/HKD=18000",
                    "position.contracts_open_by_symbol_currency_option_type_side: 0700.HK/HKD/put/short=8, 9992.HK/HKD/call/short=6, 9992.HK/HKD/put/short=6, 3690.HK/HKD/put/short=4",
                    "position.cash_secured_amount_by_symbol_currency_option_type_side: 0700.HK/HKD/put/short=344000, 9992.HK/HKD/put/short=163500, 3690.HK/HKD/put/short=135000, 0883.HK/HKD/put/short=18000",
                ],
                "data": {"eval_only": True, "row_count": 29},
                "error": None,
                "evidence_ok": True,
                "evidence_context": {
                    "time_scope": "current_snapshot",
                    "record_type": "current_open_position_snapshot",
                    "use_as": "current exposure evidence",
                    "not_evidence_for": "monthly transaction history or closed-trade history",
                    "answer_dimensions": "open-exposure concentration",
                },
            },
            {
                "tool_name": "close_advice_read",
                "ok": True,
                "summary": "eval-only production-shaped close advice has only two close signals and several unevaluable rows.",
                "facts": [
                    "close_advice.scope: latest_available_snapshot",
                    "close_advice.record_type: exit_signal_not_monthly_transaction_history",
                    "advice_rows=29",
                    "close_advice.tier_counts: none=16, not_evaluable=11, weak=2",
                    "close_advice.action_counts: hold=14, not_evaluable=11, close=2, hold_call=1, hold_put_keep_call=1",
                    "close_advice.evaluation_counts: priced=18, not_evaluable=6, quote_unusable=5",
                    "close_advice[1]: symbol=9992.HK, close_action=close, tier=weak, reason=已锁定部分收益且剩余时间较长，适合进入观察, account=sy, option_type=call, side=short, contracts_open=1.0",
                    "close_advice[2]: symbol=0883.HK, close_action=close, tier=weak, reason=已锁定部分收益且剩余时间较长，适合进入观察, account=lx, option_type=put, side=short, contracts_open=1.0",
                    "close_advice[3]: symbol=0700.HK, close_action=hold, reason=Sell Put 默认可接货；当前未达到收益回收阈值，继续持有等待归零或接货；观察项：IV/RV edge 转弱，需观察承保补偿，不作为平仓提醒, account=sy, option_type=put, side=short, contracts_open=1.0, expiration=2026-07-30",
                ],
                "data": {"eval_only": True, "row_count": 29},
                "error": None,
                "evidence_ok": True,
                "evidence_context": {
                    "time_scope": "latest_available_snapshot",
                    "record_type": "exit_signal_snapshot",
                    "use_as": "current close-advice signal evidence",
                    "not_evidence_for": "monthly transaction history or realized trade history",
                    "answer_dimensions": "current close-advice signals",
                },
            },
        ]
    if fixture_id == "june_option_review_close_advice_missing":
        observations = _fixture_observations("june_option_review_model_ready")
        observations[-1] = {
            "tool_name": "close_advice_read",
            "ok": False,
            "summary": "eval-only close advice snapshot is missing.",
            "facts": [],
            "data": {"eval_only": True, "row_count": 0},
            "error": {"code": "DEPENDENCY_MISSING", "message": "close advice fixture missing"},
            "evidence_ok": False,
            "claimable": False,
            "missing_data": ["close_advice_read evidence unavailable: DEPENDENCY_MISSING"],
        }
        return observations
    if fixture_id == "june_option_review_income_missing_current_exposure":
        return [
            {
                "tool_name": "analysis_catalog",
                "ok": True,
                "summary": "eval-only analysis catalog exposes monthly and current exposure views.",
                "facts": [
                    "eval_only=true",
                    "views=account_monthly_performance,symbol_income_attribution,open_option_exposure,close_advice_snapshot",
                ],
                "data": {"eval_only": True, "view_count": 4},
                "error": None,
                "evidence_ok": True,
                "claimable": False,
            },
            {
                "tool_name": "analysis_query",
                "ok": True,
                "summary": "eval-only analysis query has current exposure rows but no requested-month income rows.",
                "facts": [
                    "query_filters: months=[2026-06]",
                    "account_monthly_performance.row_count=0",
                    "monthly_income_summary.row_count=0",
                    "symbol_income_attribution.row_count=0",
                    "open_option_exposure.row_count=1",
                    "open_option_exposure.contracts_open_by_symbol_currency_option_type_side: PLTR/USD/put/short=1",
                    "expiration_risk_buckets.cash_secured_amount_by_expiration_bucket_currency: expired/USD=3000",
                ],
                "data": {"eval_only": True, "row_count": 2},
                "error": None,
                "evidence_ok": False,
                "claimable": False,
                "missing_data": [
                    "analysis_query filtered view empty: account_monthly_performance",
                    "analysis_query filtered view empty: monthly_income_summary",
                    "analysis_query filtered view empty: symbol_income_attribution",
                ],
            },
            {
                "tool_name": "monthly_income_report",
                "ok": True,
                "summary": "eval-only monthly income report returned no requested-month income rows.",
                "facts": [
                    "return_summary_rows=0",
                    "summary_rows=0",
                    "premium_rows=0",
                    "realized_rows=0",
                    "assignment_lifecycle_rows=0",
                    "diagnostic[1]: account=lx, month=2026-06, status=empty, matched_trade_events_count=0, missing_fields=[closed_lots, income_rows, premium, trade_events]",
                ],
                "data": {"eval_only": True, "row_count": 0},
                "error": None,
                "evidence_ok": False,
                "claimable": False,
                "missing_data": [
                    "monthly_income_report evidence",
                    "monthly_income_report missing fields: closed_lots, income_rows, premium",
                    "monthly_income_report no matched trade_events",
                    "monthly_income_report diagnostic status: empty",
                ],
            },
            {
                "tool_name": "option_positions_read",
                "ok": True,
                "summary": "eval-only current open positions show one PLTR short put exposure.",
                "facts": [
                    "position.scope: current_open_positions",
                    "position.record_type: current_position_snapshot_not_monthly_transaction_history",
                    "position.contracts_open_by_symbol_currency_option_type_side: PLTR/USD/put/short=1",
                    "position.cash_secured_amount_by_symbol_currency_option_type_side: PLTR/USD/put/short=3000",
                    "position[1]: account=lx, symbol=PLTR, option_type=put, side=short, strike=30, expiration_ymd=2026-05-15, days_to_expiration=-55, contracts_open=1",
                ],
                "data": {"eval_only": True, "row_count": 1},
                "error": None,
                "evidence_ok": True,
                "evidence_context": {
                    "time_scope": "current_snapshot",
                    "record_type": "current_open_position_snapshot",
                    "use_as": "current exposure evidence",
                    "not_evidence_for": "monthly transaction history or closed-trade history",
                    "answer_dimensions": "open-exposure concentration",
                },
            },
            {
                "tool_name": "close_advice_read",
                "ok": False,
                "summary": "eval-only close advice snapshot is missing.",
                "facts": [],
                "data": {"eval_only": True, "row_count": 0},
                "error": {"code": "DEPENDENCY_MISSING", "message": "close advice fixture missing"},
                "evidence_ok": False,
                "claimable": False,
                "missing_data": ["close_advice_read evidence unavailable: DEPENDENCY_MISSING"],
            },
        ]
    if fixture_id == "june_option_review_snapshot_only":
        return [
            {
                "tool_name": "analysis_catalog",
                "ok": True,
                "summary": "eval-only analysis catalog exposes monthly and snapshot views.",
                "facts": [
                    "eval_only=true",
                    "views=account_monthly_performance,symbol_income_attribution,open_option_exposure,close_advice_snapshot",
                ],
                "data": {"eval_only": True, "view_count": 4},
                "error": None,
                "evidence_ok": True,
                "claimable": False,
            },
            {
                "tool_name": "analysis_query",
                "ok": True,
                "summary": "eval-only analysis query has only current snapshot rows, not requested-month operation rows.",
                "facts": [
                    "query_filters: months=[2026-06]",
                    "freshness[1]: view=open_option_exposure, freshness=snapshot, status=declared",
                    "open_option_exposure.row_count=1",
                    "close_advice_snapshot.row_count=1",
                ],
                "data": {"eval_only": True, "row_count": 2},
                "error": None,
                "evidence_ok": False,
                "claimable": False,
                "missing_data": ["analysis_query evidence unavailable: REQUESTED_MONTH_DATA_MISSING"],
            },
            {
                "tool_name": "monthly_income_report",
                "ok": False,
                "summary": "eval-only monthly income report is missing for the requested month.",
                "facts": [],
                "data": {"eval_only": True, "row_count": 0},
                "error": {"code": "DEPENDENCY_MISSING", "message": "monthly income fixture missing"},
                "evidence_ok": False,
                "claimable": False,
                "missing_data": ["monthly_income_report evidence unavailable: DEPENDENCY_MISSING"],
            },
            {
                "tool_name": "option_positions_read",
                "ok": True,
                "summary": "eval-only current snapshot shows 0700.HK short put exposure.",
                "facts": [
                    "position.scope: current_snapshot",
                    "position.record_type: current_position_snapshot_not_monthly_transaction_history",
                    "account=lx symbol=0700.HK option_type=put side=short contracts_open=4 cash_secured_amount=180000 currency=HKD",
                ],
                "data": {"eval_only": True, "row_count": 1},
                "error": None,
                "evidence_ok": True,
                "evidence_context": {
                    "time_scope": "current_snapshot",
                    "record_type": "current_open_position_snapshot",
                    "not_evidence_for": "monthly transaction history or closed-trade history",
                },
            },
            {
                "tool_name": "close_advice_read",
                "ok": True,
                "summary": "eval-only latest close advice flags 0700.HK for attention.",
                "facts": [
                    "close_advice.scope: latest_available_snapshot",
                    "close_advice.record_type: exit_signal_not_monthly_transaction_history",
                    "account=lx symbol=0700.HK close_action=consider_close tier=attention reason=concentrated exposure",
                ],
                "data": {"eval_only": True, "row_count": 1},
                "error": None,
                "evidence_ok": True,
                "evidence_context": {
                    "time_scope": "latest_available_snapshot",
                    "record_type": "exit_signal_snapshot",
                    "not_evidence_for": "monthly transaction history or realized trade history",
                },
            },
        ]
    if fixture_id != "june_option_review_basic":
        return [
            {
                "tool_name": "fixture",
                "ok": False,
                "summary": f"unknown eval fixture: {fixture_id or '<missing>'}",
                "data": {},
                "error": {"code": "INPUT_ERROR", "message": "unknown eval fixture"},
            }
        ]
    return [
        {
            "tool_name": "fixture",
            "ok": True,
            "summary": (
                "eval-only 月度复盘发现：样例显示 assignment 暴露较集中，尤其是 "
                "0700.HK；生产复盘必须用真实只读证据继续核对风险。"
            ),
            "data": {
                "month": "2026-06",
                "eval_only": True,
                "signal": "concentrated_assignment_exposure",
            },
            "error": None,
        },
        {
            "tool_name": "fixture",
            "ok": True,
            "summary": (
                "eval-only 月度复盘发现：样例显示 premium 本身不足以解释整月结果；"
                "生产复盘必须同时比较 premium、realized P/L、assignment、持仓暴露和 "
                "close-advice 证据。"
            ),
            "data": {
                "month": "2026-06",
                "eval_only": True,
                "signal": "premium_not_enough_for_attribution",
            },
            "error": None,
        },
        {
            "tool_name": "fixture",
            "ok": True,
            "summary": (
                "eval-only 建议形状：只有拿到真实只读收益、持仓和建议证据后才能生成"
                "优化建议；这个 fixture 只验证结论、发现和缺口的回答形状。"
            ),
            "data": {
                "month": "2026-06",
                "eval_only": True,
                "signal": "recommendation_requires_real_evidence",
            },
            "error": None,
        },
    ]


def _with_tool_view_context(item: dict[str, Any]) -> dict[str, Any]:
    tool_name = str(item.get("tool_name") or "").strip()
    view = TOOL_VIEWS.get(tool_name)
    if view is None:
        return item
    context = dict(view.evidence_context)
    if isinstance(item.get("evidence_context"), dict):
        context.update(item["evidence_context"])
    return {**item, "evidence_context": context}
