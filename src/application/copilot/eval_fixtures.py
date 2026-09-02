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
            _fixture_observation(
                "option_positions_read",
                {
                    "source": {"label": "eval-only canonical position fixture"},
                    "scope": {"action": "list", "account": "lx", "status": "open"},
                    "evidence_scope": {
                        "ledger_positions": "observed",
                        "broker_settlement": "not_observed",
                        "market_price": "not_observed",
                        "margin_state": "not_observed",
                    },
                    "freshness": {"kind": "historical"},
                    "row_count": 2,
                    "rows": [
                        {
                            "account": "lx",
                            "symbol": "0700.HK",
                            "option_type": "put",
                            "side": "short",
                            "strike": 450,
                            "expiration_ymd": "2026-10-30",
                            "expiration_state": "future",
                            "state_warning": None,
                            "contracts_open": 4,
                            "status": "open",
                            "cash_secured_amount_role": "assignment_collateral_not_profit",
                        },
                        {
                            "account": "lx",
                            "symbol": "FUTU",
                            "option_type": "put",
                            "side": "short",
                            "strike": 30,
                            "expiration_ymd": "2026-10-30",
                            "expiration_state": "future",
                            "state_warning": None,
                            "contracts_open": 1,
                            "status": "open",
                            "cash_secured_amount_role": "assignment_collateral_not_profit",
                        },
                    ],
                },
                {"action": "list", "account": "lx", "status": "open"},
            ),
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


def _fixture_observation(
    tool_name: str,
    data: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    from src.application.copilot.tools import compact_observation

    observation = compact_observation(tool_name, {"ok": True, "data": data}, payload)
    observation["eval_only"] = True
    return observation
