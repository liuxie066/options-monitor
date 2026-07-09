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
                "summary": (
                    "eval-only analysis query has June income, assignment cashflow, "
                    "open exposure, and close-advice rows."
                ),
                "facts": [
                    "query_filters: months=[2026-06]",
                    "freshness[1]: view=open_option_exposure, freshness=snapshot, status=declared",
                    "month=2026-06 account=lx net_income_cny=1200 premium_income_cny=800 realized_pnl_cny=400",
                    "account=lx symbol=0700.HK assignment_buy_cash_hkd=45000 assigned_contracts=1 premium_hkd=1467 realized_hkd=1947",
                    "account=sy symbol=0700.HK assignment_buy_cash_hkd=269000 assigned_contracts=4 premium_hkd=2934 realized_hkd=5011",
                    "risk_signal=assignment_buy_cash_concentrated_in_0700_HK",
                    "symbol=0700.HK exposure_signal=concentrated_assignment_and_open_puts",
                ],
                "data": {"eval_only": True, "row_count": 5},
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
                "summary": "eval-only monthly income shows positive net income with premium and realized components.",
                "facts": [
                    "month=2026-06 account=lx net_income_cny=1200 premium_income_cny=800 realized_pnl_cny=400",
                    "premium.premium_received_gross_by_symbol_currency: 0700.HK/HKD=4401",
                    "premium.contracts_by_symbol_currency: 0700.HK/HKD=9",
                    "realized.realized_gross_by_symbol_currency: 0700.HK/HKD=6958",
                    "realized.contracts_closed_by_symbol_currency: 0700.HK/HKD=13",
                    "enhancement.realized_gross_by_symbol_currency: 0700.HK/HKD=620",
                    "enhancement.contracts_closed_by_symbol_currency: 0700.HK/HKD=1",
                    "premium_income_cny=800 is not the whole monthly result",
                    "assignment_buy_cash_hkd_total=314000 exceeds monthly premium_hkd_total=4401",
                    "yield_enhancement_realized_gross_hkd=620",
                    "review_signal=positive_income_with_large_assignment_cash_outlay",
                ],
                "data": {"eval_only": True, "row_count": 1},
                "error": None,
                "evidence_ok": True,
            },
            {
                "tool_name": "option_positions_read",
                "ok": True,
                "summary": "eval-only open positions show 0700.HK short put exposure.",
                "facts": [
                    "account=lx symbol=0700.HK option_type=put side=short contracts_open=4 cash_secured_amount=180000 currency=HKD",
                    "position.contracts_open_by_symbol_currency_option_type_side: 0700.HK/HKD/put/short=4",
                    "position.cash_secured_amount_by_symbol_currency_option_type_side: 0700.HK/HKD/put/short=180000",
                    "account=lx symbol=0700.HK expiration_ymd=2026-07-31 strike=450 status=open",
                    "exposure_concentration_symbol=0700.HK",
                ],
                "data": {"eval_only": True, "row_count": 1},
                "error": None,
                "evidence_ok": True,
            },
            {
                "tool_name": "close_advice_read",
                "ok": True,
                "summary": "eval-only close advice flags 0700.HK for attention.",
                "facts": [
                    "close_advice.scope: latest_available_snapshot",
                    "close_advice.record_type: exit_signal_not_monthly_transaction_history",
                    "close_advice.action_counts: consider_close=1",
                    "close_advice.tier_counts: attention=1",
                    "close_advice.evaluation_counts: evaluated=1",
                    "account=lx symbol=0700.HK close_action=consider_close tier=attention reason=concentrated exposure",
                    "close_advice_basis=assignment concentration plus open short put exposure",
                    "close_advice_supports_reviewing_0700_HK_exposure=true",
                ],
                "data": {"eval_only": True, "row_count": 1},
                "error": None,
                "evidence_ok": True,
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
