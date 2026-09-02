from __future__ import annotations

from typing import Any

MODEL_SYNTHESIS_REQUIRED_FIXTURES = {
    "opening_candidate_snapshot_diagnostics_model_ready",
    "close_advice_notification_diagnostics_model_ready",
    "current_option_exposure_model_ready",
}


def fixture_requires_model_synthesis(fixture_id: str | None) -> bool:
    return fixture_id in MODEL_SYNTHESIS_REQUIRED_FIXTURES


def fixture_observations(fixture_id: str | None) -> list[dict[str, Any]]:
    return [_with_tool_view_context(item) for item in _fixture_observations(fixture_id)]


def _fixture_observations(fixture_id: str | None) -> list[dict[str, Any]]:
    if fixture_id == "opening_candidate_snapshot_diagnostics_model_ready":
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
                "summary": "eval-only sealed opening snapshot rejects NVDA because earnings fall before expiration.",
                "facts": [
                    "symbol=NVDA canonical_symbol=NVDA account=lx trace_count=1",
                    "function=sell_put status=rejected rule=risk_earnings_event count=1",
                ],
                "data": {
                    "eval_only": True,
                    "symbol": "NVDA",
                    "canonical_symbol": "NVDA",
                    "account": "lx",
                    "opening_status": "complete",
                    "evidence_status": "available",
                    "conclusion_status": "supported",
                    "trace_count": 1,
                    "status_counts": {"rejected": 1},
                    "function_counts": {"sell_put": 1},
                    "functions": [
                        {
                            "function": "sell_put",
                            "status": "rejected",
                            "reason_counts": {"risk_earnings_event": 1},
                            "rejection_reason_counts": {"risk_earnings_event": 1},
                            "rejection_reasons": [
                                {
                                    "rule": "risk_earnings_event",
                                    "label": "到期前存在财报事件",
                                    "count": 1,
                                }
                            ],
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
                "summary": "eval-only analysis catalog exposes option performance attribution views.",
                "facts": [
                    "eval_only=true",
                    "views=option_period_performance,option_cash_components,symbol_performance_attribution",
                ],
                "data": {"eval_only": True, "view_count": 3},
                "error": None,
                "evidence_ok": True,
                "claimable": False,
            },
            {
                "tool_name": "analysis_query",
                "ok": True,
                "summary": "eval-only analysis query has canonical June MTD option-performance rows.",
                "facts": [
                    "period_kind=mtd as_of_date=2026-06-30 accounts=lx option_net_cashflow_by_currency.USD.total.amount=1200",
                    "period_kind=mtd currency=USD state=terminated amount=800 status=observed",
                    "symbol=NVDA option_net_cashflow_by_currency.USD.total.amount=800 sell_option_win_rate=0.75",
                ],
                "data": {"eval_only": True, "row_count": 3},
                "error": None,
                "evidence_ok": True,
            },
            {
                "tool_name": "option_performance_report",
                "ok": True,
                "summary": "eval-only option performance exposes canonical cashflow, win-rate, and return metrics.",
                "facts": [
                    "period.kind=mtd period.as_of_date=2026-06-30 scope.accounts=lx",
                    "option_net_cashflow.by_currency.USD.total.amount=1200 sell_option_win_rate.rate=0.75 option_return.by_currency.USD.rate=0.12",
                ],
                "data": {"eval_only": True, "row_count": 1},
                "error": None,
                "evidence_ok": True,
            },
        ]
    return [
        {
            "tool_name": "fixture",
            "ok": False,
            "summary": f"unknown eval fixture: {fixture_id or '<missing>'}",
            "data": {},
            "error": {"code": "INPUT_ERROR", "message": "unknown eval fixture"},
        },
    ]


def _with_tool_view_context(item: dict[str, Any]) -> dict[str, Any]:
    return dict(item)
