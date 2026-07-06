from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import build_response
from src.application.assistant.agent_loop import run_read_only_agent_loop
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.runtime import handle_assistant_turn
from src.application.assistant.settings import AssistantSettings


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


def _analysis_response(payload: dict[str, Any], *, rows: bool) -> dict[str, Any]:
    view_datasets: dict[str, dict[str, Any]]
    if rows:
        view_datasets = {
            "account_monthly_performance": {"rows": [{"account": "sy", "realized": 5011}]},
            "account_monthly_income_components": {"rows": [{"component": "premium", "premium": 2934}]},
            "monthly_income_cashflow_rows": {
                "rows": [{"symbol": "0700.HK", "trade_action": "assignment", "assignment_buy_cash": 269000}]
            },
            "trade_events": {"rows": [{"symbol": "0700.HK", "action": "sell_put"}]},
            "open_option_exposure": {"rows": [{"symbol": "0700.HK", "notional": 300000}]},
        }
    else:
        view_datasets = {str(view): {"rows": [], "row_count": 0} for view in payload.get("views") or []}
    return build_response(
        tool_name="analysis_query",
        ok=True,
        data={
            "schema_version": "analysis.query.output.v2",
            "source_label": "OM read-only analysis workspace",
            "views_used": list(payload.get("views") or []),
            "view_datasets": view_datasets,
            "evidence": {
                "diagnostics": [
                    {"answer_boundary": "cannot infer absence of problem from empty diagnostic result"},
                ]
            },
        },
    )


def test_assistant_turn_answers_option_review_from_copilot_evidence(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(payload)))
        return _analysis_response(payload, rows=True)

    result = handle_assistant_turn(
        _request(tmp_path, "分析6月的期权操作有没有不合理，需要优化的地方"),
        execute_tool_fn=execute_tool,
        allowed_senders="u_runtime",
        settings=AssistantSettings(),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result.ok is True
    assert result.render_route == "copilot_answer"
    assert result.trace["route"] == "agent_loop"
    assert result.trace["answer_route"] == "copilot_answer"
    assert result.trace["final_response"]["reason"] == "copilot_completed"
    assert result.trace["final_response"]["copilot_composed"] is True
    assert result.trace["final_response"]["llm_may_summarize"] is False
    assert calls == [
        (
            "analysis_query",
            {
                "views": [
                    "account_monthly_performance",
                    "account_monthly_income_components",
                    "monthly_income_cashflow_rows",
                    "trade_events",
                    "open_option_exposure",
                    "strategy_config_by_symbol_account",
                    "strategy_replay_read_surface",
                ],
                "limit": 200,
                "month": "2026-06",
                "config_key": "us",
            },
        )
    ]
    assert "结论：2026-06期权操作不够理想" in result.response_text
    assert "问题模式：" in result.response_text
    assert "优化建议：" in result.response_text
    assert "证据边界：" in result.response_text


def test_assistant_turn_refuses_judgement_when_analysis_has_no_rows(tmp_path: Path) -> None:
    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert tool_name == "analysis_query"
        return _analysis_response(payload, rows=False)

    result = handle_assistant_turn(
        _request(tmp_path, "分析6月的期权操作有没有不合理，需要优化的地方"),
        execute_tool_fn=execute_tool,
        allowed_senders="u_runtime",
        settings=AssistantSettings(),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result.ok is True
    assert result.render_route == "copilot_answer"
    assert "不能判断期权操作是否不合理" in result.response_text
    assert "行级记录为 0" in result.response_text
    assert "空结果不能证明没有问题" in result.response_text
    assert "偏保守" not in result.response_text
    assert "未发现单一异常模式" not in result.response_text


def test_read_only_agent_loop_returns_single_copilot_tool_loop(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    request = _request(tmp_path, "6月收益主要来自哪里")

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(payload)))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "schema_version": "analysis.query.output.v2",
                "views_used": list(payload.get("views") or []),
                "view_datasets": {
                    "account_monthly_income_components": {
                        "rows": [{"component": "premium", "amount_cny": 1200}]
                    },
                    "symbol_income_attribution": {
                        "rows": [{"symbol": "0700.HK", "amount_cny": 900}]
                    },
                },
            },
        )

    result = run_read_only_agent_loop(
        request.text,
        settings=AssistantSettings(),
        conversation_context=None,
        request=request,
        execute_tool_fn=execute_tool,
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result.planning.perception is not None
    assert result.planning.perception.intent_name == "tool_loop"
    assert result.trace["runtime"] == "copilot"
    assert result.trace["attempted"] is False
    assert result.trace["copilot"]["attempted"] is True
    assert result.trace["agent_loop"]["runtime"] == "copilot"
    assert result.trace["agent_loop"]["steps_used"] == 1
    assert result.tool_loop_result is not None
    assert result.tool_loop_result["data"]["final_response"]["status"] == "synthesized"
    assert calls[0][0] == "analysis_query"
    assert calls[0][1]["month"] == "2026-06"


def test_copilot_preview_request_stops_before_write_execution(tmp_path: Path) -> None:
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
    assert result.trace["route"] == "agent_loop"
    assert result.trace["answer_route"] == "preview_lifecycle"
    assert result.trace["final_response"]["reason"] == "preview_gate"
    assert "write operations are disabled" in result.response_text
