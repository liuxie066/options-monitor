from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import build_response
from src.application.assistant.operation_diagnostics import collect_recent_audit
from src.application.assistant.perception import NATURAL_LANGUAGE_REBUILDING_CODE
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.runtime import handle_assistant_turn
from src.application.assistant.settings import AssistantSettings, CopilotSettings


def _request(tmp_path: Path, text: str, *, config_key: str = "us") -> AssistantRequest:
    return AssistantRequest(
        text=text,
        sender_id="u_runtime",
        channel="test",
        conversation_id="c_runtime",
        message_id="m_runtime",
        audit_db=str(tmp_path / "assistant_audit.db"),
        config_key=config_key,
    )


def test_assistant_turn_disables_free_form_natural_language(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(payload)))
        return build_response(tool_name=tool_name, ok=True, data={})

    result = handle_assistant_turn(
        _request(tmp_path, "分析6月的期权操作有没有不合理，需要优化的地方"),
        execute_tool_fn=execute_tool,
        allowed_senders="u_runtime",
        settings=AssistantSettings(),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert calls == []
    assert result.ok is False
    assert result.render_route == "error"
    assert result.error is not None
    assert result.error["code"] == NATURAL_LANGUAGE_REBUILDING_CODE
    assert result.trace["route"] == "natural_language_rebuilding"
    assert result.meta["assistant"]["copilot"]["enabled"] is False
    assert result.meta["assistant"]["freeform_execution"] == "disabled"
    assert "自由问答正在重建中" in result.response_text


def test_assistant_turn_copilot_gate_requires_explicit_channel_scene(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(payload)))
        return build_response(tool_name=tool_name, ok=True, data={})

    result = handle_assistant_turn(
        _request(tmp_path, "分析6月的期权操作有没有不合理，需要优化的地方"),
        execute_tool_fn=execute_tool,
        allowed_senders="u_runtime",
        settings=AssistantSettings(copilot=CopilotSettings(enabled=True)),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert calls == []
    assert result.ok is True
    assert result.render_route == "copilot"
    assert result.trace["route"] == "copilot"
    assert result.meta["assistant"]["copilot"]["enabled"] is True
    assert result.meta["assistant"]["copilot"]["channel_scenes"] == []
    assert result.meta["assistant"]["freeform_execution"] == "copilot_gate"
    assert result.data["reasoning"]["action_kind"] == "copilot"
    assert result.data["action"]["tool_name"] == "copilot.channel"
    copilot = result.data["action"]["result"]["data"]["copilot"]
    assert result.trace["copilot"] == {"status": "not_ready"}
    assert copilot["status"] == "not_ready"
    assert copilot["decision_trace"]["selected_scene"] is None
    assert copilot["decision_trace"]["selection_environment"] == "channel"
    assert "Copilot 渠道自由问答尚未开放到可执行场景" in result.response_text

    audit = collect_recent_audit(audit_db=str(tmp_path / "assistant_audit.db"), channel="test", sender_id="u_runtime")
    row = audit["audit_rows"][0]
    assert row["copilot"] == result.trace["copilot"]
    assert "copilot: status=not_ready" in audit["response_text"]


def test_assistant_turn_copilot_scene_allowlist_does_not_open_non_channel_scene(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(payload)))
        return build_response(tool_name=tool_name, ok=True, data={})

    result = handle_assistant_turn(
        _request(tmp_path, "分析6月的期权操作有没有不合理，需要优化的地方"),
        execute_tool_fn=execute_tool,
        allowed_senders="u_runtime",
        settings=AssistantSettings(copilot=CopilotSettings(enabled=True, channel_scenes=("channel_smoke",))),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert calls == []
    assert result.ok is True
    assert result.render_route == "copilot"
    assert result.meta["assistant"]["copilot"]["channel_scenes"] == ["channel_smoke"]
    copilot = result.data["action"]["result"]["data"]["copilot"]
    assert result.trace["copilot"] == {"status": "not_ready"}
    assert copilot["status"] == "not_ready"
    assert copilot["decision_trace"]["selected_scene"] is None
    assert "Copilot 渠道自由问答尚未开放到可执行场景" in result.response_text


def test_assistant_turn_channel_copilot_model_config_does_not_open_non_channel_scene(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.copilot import local_harness
    from src.application.copilot import tools as copilot_tools
    from src.application.copilot.model_client import CopilotModelSettings

    assistant_config = tmp_path / "config.assistant.json"
    assistant_config.write_text(
        json.dumps(
            {
                "assistant": {
                    "enabled": True,
                    "llm": {
                        "provider": "openai",
                        "model": "gpt-test",
                        "api_key_env": "OM_TEST_KEY",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OM_TEST_KEY", "sk-test")
    model_requests: list[dict[str, Any]] = []
    tool_calls: list[str] = []

    def fake_build_action_model(settings: CopilotModelSettings):
        assert settings.provider == "openai"
        assert settings.model == "gpt-test"
        assert settings.api_key_env == "OM_TEST_KEY"

        def model(request: dict[str, Any]) -> dict[str, Any]:
            model_requests.append(request)
            missing = request["finish_conditions"]["unattempted_tools_without_evidence"]
            if missing:
                return {"kind": "tool", "tool_name": missing[0], "reason": "need channel evidence", "answer_report": None}
            return {
                "kind": "finish",
                "tool_name": None,
                "reason": "enough channel evidence",
                "answer_report": {
                    "conclusion": "结论：6月期权复盘收益为正，但 0700.HK assignment 现金占用和 short put 暴露集中，需要复核。",
                    "attempted_checks": [
                        "analysis_catalog",
                        "analysis_query",
                        "monthly_income_report",
                        "option_positions_read",
                        "close_advice_read",
                    ],
                    "findings": [
                        {
                            "summary": "6月 lx 账户 0700.HK 净收入 1200 CNY，premium 贡献 800 CNY。",
                            "evidence_refs": ["obs_2", "obs_3"],
                        },
                        {
                            "summary": "0700.HK assignment 买入现金流为 45000 HKD，premium 为 1467 HKD。",
                            "evidence_refs": ["obs_3"],
                        },
                        {
                            "summary": "当前 lx 账户 0700.HK 有 4 张 short put，现金占用 180000 HKD。",
                            "evidence_refs": ["obs_4"],
                        },
                        {
                            "summary": "当前 0700.HK close advice 为 consider_close/attention。",
                            "evidence_refs": ["obs_5"],
                        },
                    ],
                    "recommendations": [
                        {
                            "summary": "基于 0700.HK 当前 short put 暴露集中和 close advice 信号，复核是否减仓或滚动。",
                            "action": "复核是否减仓或滚动",
                            "target_scope": "0700.HK short put 暴露",
                            "review_dimension": "open-exposure concentration",
                            "basis_refs": ["obs_2", "obs_3", "obs_4", "obs_5"],
                        }
                    ],
                    "missing_data": [],
                    "evidence_refs": ["obs_2", "obs_3", "obs_4", "obs_5"],
                },
            }

        return model

    def fake_call(tool_name: str, payload: dict[str, Any], *, allowed_tools: tuple[str, ...]) -> dict[str, Any]:
        assert tool_name in allowed_tools
        tool_calls.append(tool_name)
        if tool_name == "analysis_catalog":
            return {"tool_name": tool_name, "ok": True, "data": {"view_count": 8, "view_names": _monthly_views()}}
        if tool_name == "analysis_query":
            view_datasets = {
                "account_monthly_performance": {
                    "row_count": 1,
                    "rows": [{"month": "2026-06", "account": "lx", "net_income_cny": 1200, "premium_income_cny": 800}],
                },
                "account_monthly_income_components": {
                    "row_count": 1,
                    "rows": [{"month": "2026-06", "account": "lx", "component": "premium", "amount_cny": 800}],
                },
                "monthly_income_summary": {
                    "row_count": 1,
                    "rows": [{"month": "2026-06", "account": "lx", "net_income_cny": 1200, "premium_income_cny": 800}],
                },
                "symbol_income_attribution": {
                    "row_count": 1,
                    "rows": [
                        {
                            "month": "2026-06",
                            "account": "lx",
                            "symbol": "0700.HK",
                            "currency": "HKD",
                            "component": "premium",
                            "amount_gross": 1467,
                        }
                    ],
                },
                "trade_events": {
                    "row_count": 1,
                    "rows": [
                        {
                            "month": "2026-06",
                            "account": "lx",
                            "symbol": "0700.HK",
                            "currency": "HKD",
                            "position_effect": "open",
                            "side": "sell",
                            "option_type": "put",
                            "contracts": 4,
                        }
                    ],
                },
                "open_option_exposure": {
                    "row_count": 1,
                    "rows": [
                        {
                            "account": "lx",
                            "symbol": "0700.HK",
                            "currency": "HKD",
                            "option_type": "put",
                            "side": "short",
                            "contracts_open": 4,
                            "cash_secured_amount": 180000,
                        }
                    ],
                },
                "expiration_risk_buckets": {
                    "row_count": 1,
                    "rows": [
                        {
                            "account": "lx",
                            "expiration_bucket": "30-60d",
                            "currency": "HKD",
                            "contracts_open": 4,
                            "cash_secured_amount": 180000,
                        }
                    ],
                },
                "close_advice_snapshot": {
                    "row_count": 1,
                    "rows": [{"account": "lx", "symbol": "0700.HK", "close_action": "consider_close", "tier": "attention"}],
                },
            }
            return {
                "tool_name": tool_name,
                "ok": True,
                "data": {
                    "query": {"mode": "views", "filters": {"months": ["2026-06"]}},
                    "row_count": 8,
                    "views_used": _monthly_views(),
                    "view_datasets": view_datasets,
                    "evidence": {"coverage": {"row_count": 8, "account_count": 1, "symbol_count": 1}},
                },
            }
        if tool_name == "monthly_income_report":
            return {
                "tool_name": tool_name,
                "ok": True,
                "data": {
                    "premium_rows": [
                        {"account": "lx", "symbol": "0700.HK", "currency": "HKD", "contracts": 3, "premium_received_gross": 1467}
                    ],
                    "realized_rows": [
                        {"account": "lx", "symbol": "0700.HK", "currency": "HKD", "contracts_closed": 4, "realized_gross": 1947}
                    ],
                    "assignment_lifecycle_rows": [
                        {
                            "account": "lx",
                            "symbol": "0700.HK",
                            "currency": "HKD",
                            "assigned_contracts": 1,
                            "assignment_buy_cash_hkd": 45000,
                            "premium_hkd": 1467,
                        }
                    ],
                },
            }
        if tool_name == "option_positions_read":
            return {
                "tool_name": tool_name,
                "ok": True,
                "data": {
                    "row_count": 1,
                    "rows": [
                        {
                            "account": "lx",
                            "symbol": "0700.HK",
                            "option_type": "put",
                            "side": "short",
                            "contracts_open": 4,
                            "currency": "HKD",
                            "cash_secured_amount": 180000,
                            "status": "open",
                        }
                    ],
                },
            }
        if tool_name == "close_advice_read":
            return {
                "tool_name": tool_name,
                "ok": True,
                "data": {
                    "row_count": 1,
                    "rows": [
                        {"account": "lx", "symbol": "0700.HK", "close_action": "consider_close", "tier": "attention"}
                    ],
                },
            }
        raise AssertionError(f"unexpected tool: {tool_name}")

    monkeypatch.setattr(local_harness, "build_action_model", fake_build_action_model)
    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)

    request = AssistantRequest(
        text="分析6月的期权操作有没有不合理，需要优化的地方",
        sender_id="u_runtime",
        channel="test",
        conversation_id="c_runtime",
        message_id="m_runtime",
        audit_db=str(tmp_path / "assistant_audit.db"),
        config_key="us",
        assistant_config_path=str(assistant_config),
    )
    result = handle_assistant_turn(
        request,
        allowed_senders="u_runtime",
        settings=AssistantSettings(copilot=CopilotSettings(enabled=True, channel_scenes=("channel_smoke",))),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result.ok is True
    assert result.render_route == "copilot"
    copilot = result.data["action"]["result"]["data"]["copilot"]
    assert result.trace["copilot"] == {"status": "not_ready"}
    assert copilot["status"] == "not_ready"
    assert copilot["decision_trace"]["selected_scene"] is None
    assert copilot["decision_trace"]["selection_environment"] == "channel"
    assert tool_calls == []
    assert model_requests == []
    assert "Copilot 渠道自由问答尚未开放到可执行场景" in result.response_text

    audit = collect_recent_audit(audit_db=str(tmp_path / "assistant_audit.db"), channel="test", sender_id="u_runtime")
    row = audit["audit_rows"][0]
    assert row["copilot"] == result.trace["copilot"]
    assert "copilot: status=not_ready" in audit["response_text"]


def test_assistant_turn_channel_copilot_persists_failed_event_summary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.copilot.contracts import AnswerReport, AppEvent, AppResult, utc_now_iso
    import src.application.assistant.action as assistant_action

    def fake_run_channel_request(**kwargs: Any) -> AppResult:
        run_id = "run_failed"
        return AppResult(
            status="failed",
            ok=False,
            user_response="结论：Copilot 执行失败。",
            answer_report=AnswerReport(conclusion="结论：Copilot 执行失败。"),
            request_id=str(kwargs.get("request_id") or ""),
            contract_id="contract_failed",
            run_id=run_id,
            decision_trace={"selected_scene": "channel_smoke"},
            events=[
                AppEvent(
                    event_id="evt_1",
                    run_id=run_id,
                    type="contract_received",
                    timestamp=utc_now_iso(),
                    payload={"contract_id": "contract_failed", "scene": "channel_smoke"},
                ),
                AppEvent(
                    event_id="evt_2",
                    run_id=run_id,
                    type="engine_failed",
                    timestamp=utc_now_iso(),
                    payload={"reason": "engine_error"},
                ),
                AppEvent(
                    event_id="evt_3",
                    run_id=run_id,
                    type="final_result",
                    timestamp=utc_now_iso(),
                    payload={"status": "failed", "ok": False, "missing_data": ["engine_failed"]},
                ),
            ],
        )

    monkeypatch.setattr(assistant_action, "run_channel_request", fake_run_channel_request)

    result = handle_assistant_turn(
        _request(tmp_path, "分析6月的期权操作有没有不合理，需要优化的地方"),
        allowed_senders="u_runtime",
        settings=AssistantSettings(copilot=CopilotSettings(enabled=True, channel_scenes=("channel_smoke",))),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result.ok is False
    copilot_events = result.data["action"]["result"]["data"]["copilot_events"]
    assert copilot_events["event_types"] == ["contract_received", "engine_failed", "final_result"]
    assert copilot_events["failure_reasons"] == ["engine_error", "failed"]
    assert result.trace["copilot_events"]["failure_reasons"] == ["engine_error", "failed"]

    audit = collect_recent_audit(audit_db=str(tmp_path / "assistant_audit.db"), channel="test", sender_id="u_runtime")
    row = audit["audit_rows"][0]
    assert row["copilot_events"]["failure_reasons"] == ["engine_error", "failed"]
    assert "failures=engine_error,failed" in audit["response_text"]


def test_assistant_turn_channel_copilot_event_summary_stabilizes_failure_reasons(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.copilot.contracts import AnswerReport, AppEvent, AppResult, utc_now_iso
    import src.application.assistant.action as assistant_action

    def fake_run_channel_request(**kwargs: Any) -> AppResult:
        run_id = "run_secret_failure"
        return AppResult(
            status="failed",
            ok=False,
            user_response="结论：Copilot 执行失败。",
            answer_report=AnswerReport(conclusion="结论：Copilot 执行失败。"),
            request_id=str(kwargs.get("request_id") or ""),
            contract_id="contract_secret_failure",
            run_id=run_id,
            decision_trace={"selected_scene": "channel_smoke"},
            events=[
                AppEvent(
                    event_id="evt_1",
                    run_id=run_id,
                    type="engine_failed",
                    timestamp=utc_now_iso(),
                    payload={"reason": "SECRET_ENGINE_FAILURE /private/tmp/raw-detail"},
                ),
                AppEvent(
                    event_id="evt_2",
                    run_id=run_id,
                    type="tool_failed",
                    timestamp=utc_now_iso(),
                    payload={"tool_name": "analysis_query", "error_code": "SECRET_TOOL_CODE /tmp/raw-detail"},
                ),
                AppEvent(
                    event_id="evt_3",
                    run_id=run_id,
                    type="final_result",
                    timestamp=utc_now_iso(),
                    payload={"status": "failed", "ok": False},
                ),
            ],
        )

    monkeypatch.setattr(assistant_action, "run_channel_request", fake_run_channel_request)

    result = handle_assistant_turn(
        _request(tmp_path, "分析6月的期权操作有没有不合理，需要优化的地方"),
        allowed_senders="u_runtime",
        settings=AssistantSettings(copilot=CopilotSettings(enabled=True, channel_scenes=("channel_smoke",))),
        now_fn=lambda: date(2026, 7, 6),
    )

    copilot_events = result.data["action"]["result"]["data"]["copilot_events"]
    assert copilot_events["failure_reasons"] == ["engine_failed", "tool_failed", "failed"]
    assert "SECRET" not in json.dumps(copilot_events, ensure_ascii=False)

    audit = collect_recent_audit(audit_db=str(tmp_path / "assistant_audit.db"), channel="test", sender_id="u_runtime")
    row = audit["audit_rows"][0]
    assert row["copilot_events"]["failure_reasons"] == ["engine_failed", "tool_failed", "failed"]
    assert "SECRET" not in json.dumps(row["copilot_events"], ensure_ascii=False)
    assert "SECRET" not in audit["response_text"]


def test_assistant_turn_channel_copilot_human_review_holds_channel_answer(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.copilot.contracts import AnswerReport, AppEvent, AppResult, utc_now_iso
    import src.application.assistant.action as assistant_action

    def fake_run_channel_request(**kwargs: Any) -> AppResult:
        run_id = "run_review"
        return AppResult(
            status="answered",
            ok=True,
            user_response="结论：渠道分析可回答。\n- 建议：等待人工确认。",
            answer_report=AnswerReport(conclusion="结论：渠道分析可回答。"),
            request_id=str(kwargs.get("request_id") or ""),
            contract_id="contract_review",
            run_id=run_id,
            decision_trace={"selected_scene": "channel_smoke"},
            events=[
                AppEvent(
                    event_id="evt_1",
                    run_id=run_id,
                    type="contract_received",
                    timestamp=utc_now_iso(),
                    payload={"contract_id": "contract_review", "scene": "channel_smoke"},
                ),
                AppEvent(
                    event_id="evt_2",
                    run_id=run_id,
                    type="final_result",
                    timestamp=utc_now_iso(),
                    payload={"status": "answered", "ok": True},
                ),
            ],
        )

    monkeypatch.setattr(assistant_action, "run_channel_request", fake_run_channel_request)

    result = handle_assistant_turn(
        _request(tmp_path, "分析6月的期权操作有没有不合理，需要优化的地方"),
        allowed_senders="u_runtime",
        settings=AssistantSettings(
            copilot=CopilotSettings(
                enabled=True,
                channel_scenes=("channel_smoke",),
                human_review=True,
            )
        ),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result.ok is True
    assert "等待人工复核" in result.response_text
    assert "建议：等待人工确认" not in result.response_text
    result_data = result.data["action"]["result"]["data"]
    assert result_data["copilot"]["user_response"] == result.response_text
    assert result_data["copilot"]["answer_report"] is None
    assert result_data["copilot"]["human_review_held"] is True
    assert result_data["copilot_human_review"] == {"enabled": True, "held": True}
    assert "建议：等待人工确认" not in json.dumps(result.data, ensure_ascii=False)

    audit = collect_recent_audit(audit_db=str(tmp_path / "assistant_audit.db"), channel="test", sender_id="u_runtime")
    row = audit["audit_rows"][0]
    assert row["response_text"] == result.response_text
    assert row["copilot_events"]["event_count"] == 2
    assert "建议：等待人工确认" not in json.dumps(row["action"], ensure_ascii=False)
    assert "建议：等待人工确认" not in audit["response_text"]


def _monthly_views() -> list[str]:
    return [
        "account_monthly_performance",
        "account_monthly_income_components",
        "monthly_income_summary",
        "symbol_income_attribution",
        "trade_events",
        "open_option_exposure",
        "expiration_risk_buckets",
        "close_advice_snapshot",
    ]


def test_assistant_turn_copilot_gate_refuses_write_like_free_form(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(payload)))
        return build_response(tool_name=tool_name, ok=True, data={})

    result = handle_assistant_turn(
        _request(tmp_path, "把 0700 put 加到监控里", config_key="hk"),
        execute_tool_fn=execute_tool,
        allowed_senders="u_runtime",
        settings=AssistantSettings(copilot=CopilotSettings(enabled=True)),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert calls == []
    assert result.ok is True
    assert result.render_route == "copilot"
    copilot = result.data["action"]["result"]["data"]["copilot"]
    assert copilot["status"] == "refused"
    assert "只接受只读问题" in result.response_text


def test_assistant_turn_keeps_slash_help_available(tmp_path: Path) -> None:
    result = handle_assistant_turn(
        _request(tmp_path, "/help"),
        allowed_senders="u_runtime",
        settings=AssistantSettings(),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result.ok is True
    assert result.render_route == "command"
    assert "/help" in result.response_text


def test_assistant_turn_keeps_explicit_read_command_available(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(payload)))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "status": "ok",
                "summary": "runtime ok",
            },
        )

    result = handle_assistant_turn(
        _request(tmp_path, "/status"),
        execute_tool_fn=execute_tool,
        allowed_senders="u_runtime",
        settings=AssistantSettings(),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result.ok is True
    assert calls == [("runtime_status", {"config_key": "us"})]


def test_free_form_trade_message_does_not_preview_or_write(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(payload)))
        return build_response(tool_name=tool_name, ok=True, data={})

    result = handle_assistant_turn(
        _request(
            tmp_path,
            "sy 成交提醒: 【成交提醒】成功卖出2张$腾讯 260605 440.00 沽$，成交价格：0.86，此笔订单委托已全部成交",
            config_key="hk",
        ),
        execute_tool_fn=execute_tool,
        allowed_senders="u_runtime",
        settings=AssistantSettings(),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert calls == []
    assert result.ok is False
    assert result.error is not None
    assert result.error["code"] == NATURAL_LANGUAGE_REBUILDING_CODE
