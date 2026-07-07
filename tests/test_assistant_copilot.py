from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import build_response
from src.application.assistant.settings import AssistantLlmSettings, AssistantSettings
from src.application.assistant_copilot.runtime import (
    AssistantLlmCopilotModelClient,
    CopilotRuntimeLimits,
    run_copilot_task,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeModelClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        *,
        stage: str,
        instructions: str,
        input_payload: dict[str, Any],
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append({
            "stage": stage,
            "instructions": instructions,
            "input_payload": input_payload,
            "json_schema": json_schema,
        })
        assert self.responses, stage
        return self.responses.pop(0)


def _frame_response() -> dict[str, Any]:
    return {
        "schema_version": "om-copilot-task-frame-v1",
        "user_goal": "分析6月的期权操作有没有不合理，需要优化的地方",
        "task_kind": "analysis",
        "scope": {
            "market": "us",
            "accounts": [],
            "symbols": [],
            "months": ["2026-06"],
            "date_range": None,
        },
        "constraints": {
            "read_only": True,
            "allow_realtime_quote_refresh": False,
            "allow_write_preview": False,
        },
        "answer_shape": {
            "requires_conclusion": True,
            "requires_recommendations": True,
            "requires_evidence": True,
            "allow_table": True,
        },
        "missing_slots": [],
    }


def _plan_response(tool_name: str = "analysis_query") -> dict[str, Any]:
    return {
        "schema_version": "om-copilot-evidence-plan-v1",
        "steps": [
            {
                "tool_name": tool_name,
                "purpose": "summarize June option operations and assignment exposure",
                "payload": {
                    "sql": (
                        "select month, account, symbol, net_income_cny "
                        "from monthly_symbol_income_attribution where month = '2026-06'"
                    ),
                    "month": "2026-06",
                },
            }
        ],
        "expected_evidence": ["monthly performance", "symbol attribution", "assignment exposure"],
    }


def _answer_response(*, evidence_ref: str = "obs_2.rows.0.net_income_cny") -> dict[str, Any]:
    return {
        "schema_version": "om-copilot-answer-v1",
        "status": "answered",
        "conclusion": "6月期权操作整体有收益，但收益集中且存在个别标的指派暴露，需要优化仓位分散。",
        "findings": [
            {
                "claim": "6月存在可归因的正收益记录，不能只看明细行数。",
                "evidence_refs": [evidence_ref],
            }
        ],
        "recommendations": [
            {
                "text": "后续复盘应同时看收益、指派现金占用和未平仓暴露，避免单一标的贡献掩盖风险。",
                "basis_refs": [evidence_ref],
            }
        ],
        "missing_data": [],
        "response_text": (
            "结论：6月期权操作整体有收益，但收益集中且存在个别标的指派暴露，需要优化仓位分散。\n"
            "依据：6月收益归因记录显示存在正收益。\n"
            "建议：复盘时同时看收益、指派现金占用和未平仓暴露。"
        ),
    }


def test_copilot_runtime_collects_read_only_evidence_and_returns_verified_answer() -> None:
    model = FakeModelClient([_frame_response(), _plan_response(), _answer_response()])
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(payload)))
        if tool_name == "analysis_catalog":
            return build_response(
                tool_name=tool_name,
                ok=True,
                data={
                    "view_count": 1,
                    "views": [{"name": "monthly_symbol_income_attribution", "fields": ["month", "account", "symbol", "net_income_cny"]}],
                    "sql_rules": {"writes_allowed": False, "allowed_statements": ["select", "with"]},
                },
            )
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "row_count": 1,
                "columns": ["month", "account", "symbol", "net_income_cny"],
                "rows": [
                    {
                        "month": "2026-06",
                        "account": "lx",
                        "symbol": "FUTU",
                        "net_income_cny": 1280.0,
                    }
                ],
            },
        )

    result = run_copilot_task(
        text="分析6月的期权操作有没有不合理，需要优化的地方",
        config_key="us",
        assistant_settings=AssistantSettings(),
        limits=CopilotRuntimeLimits(max_tool_calls=4),
        model_client=model,
        execute_tool_fn=execute_tool,
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result["ok"] is True
    assert result["status"] == "answered"
    assert "结论" in result["response_text"]
    assert "建议" in result["response_text"]
    assert calls[0] == ("analysis_catalog", {"config_key": "us"})
    assert calls[1][0] == "analysis_query"
    assert calls[1][1]["config_key"] == "us"
    assert result["verification"]["ok"] is True
    assert [call["stage"] for call in model.calls] == ["task_frame", "evidence_plan", "answer"]


def test_copilot_runtime_rejects_non_allowlisted_tool_before_execution() -> None:
    model = FakeModelClient([_frame_response(), _plan_response(tool_name="preview_notification")])
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(payload)))
        return build_response(tool_name=tool_name, ok=True, data={"row_count": 0})

    result = run_copilot_task(
        text="分析6月的期权操作有没有不合理，需要优化的地方",
        config_key="us",
        assistant_settings=AssistantSettings(),
        limits=CopilotRuntimeLimits(max_tool_calls=4),
        model_client=model,
        execute_tool_fn=execute_tool,
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert calls == [("analysis_catalog", {"config_key": "us"})]


def test_copilot_runtime_blocks_answer_with_unknown_evidence_ref() -> None:
    model = FakeModelClient([
        _frame_response(),
        _plan_response(),
        _answer_response(evidence_ref="obs_999.rows.0.net_income_cny"),
    ])

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "analysis_catalog":
            return build_response(tool_name=tool_name, ok=True, data={"view_count": 1, "views": []})
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={"row_count": 1, "rows": [{"month": "2026-06", "net_income_cny": 1280.0}]},
        )

    result = run_copilot_task(
        text="分析6月的期权操作有没有不合理，需要优化的地方",
        config_key="us",
        assistant_settings=AssistantSettings(),
        limits=CopilotRuntimeLimits(max_tool_calls=4),
        model_client=model,
        execute_tool_fn=execute_tool,
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["verification"]["ok"] is False
    assert result["verification"]["failures"][0]["code"] == "unknown_evidence_ref"


def test_copilot_runtime_enforces_model_turn_budget() -> None:
    model = FakeModelClient([_frame_response(), _plan_response(), _answer_response()])

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "analysis_catalog":
            return build_response(tool_name=tool_name, ok=True, data={"view_count": 1, "views": []})
        return build_response(tool_name=tool_name, ok=True, data={"row_count": 1, "rows": [{"net_income_cny": 1280.0}]})

    result = run_copilot_task(
        text="分析6月的期权操作有没有不合理，需要优化的地方",
        config_key="us",
        assistant_settings=AssistantSettings(),
        limits=CopilotRuntimeLimits(max_tool_calls=4, max_model_turns=2),
        model_client=model,
        execute_tool_fn=execute_tool,
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"]["code"] == "COPILOT_BUDGET_EXCEEDED"
    assert [call["stage"] for call in model.calls] == ["task_frame", "evidence_plan"]


def test_copilot_runtime_returns_structured_error_when_model_call_fails() -> None:
    def _raise_network_error(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise RuntimeError("network unavailable")

    client = AssistantLlmCopilotModelClient(
        AssistantLlmSettings(
            enabled=True,
            provider="openai",
            model="gpt-test",
            api_key_env="OM_LLM_API_KEY",
        ),
        environ={"OM_LLM_API_KEY": "test-key"},
        create_response_fn=_raise_network_error,
    )

    result = run_copilot_task(
        text="分析6月的期权操作有没有不合理，需要优化的地方",
        config_key="us",
        assistant_settings=AssistantSettings(),
        limits=CopilotRuntimeLimits(max_tool_calls=4),
        model_client=client,
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"]["code"] == "LLM_UNAVAILABLE"


def test_copilot_runtime_rejects_unsupported_model_provider() -> None:
    client = AssistantLlmCopilotModelClient(
        AssistantLlmSettings(
            enabled=True,
            provider="unknown-provider",
            model="model",
            api_key_env="OM_LLM_API_KEY",
        ),
        environ={"OM_LLM_API_KEY": "test-key"},
        create_response_fn=lambda **kwargs: {"output_text": "{}"},
    )

    result = run_copilot_task(
        text="分析6月的期权操作有没有不合理，需要优化的地方",
        config_key="us",
        assistant_settings=AssistantSettings(),
        limits=CopilotRuntimeLimits(max_tool_calls=4),
        model_client=client,
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"]["code"] == "LLM_UNAVAILABLE"
    assert result["error"]["details"]["provider"] == "unknown-provider"


def test_copilot_source_does_not_hardcode_target_business_question() -> None:
    forbidden = (
        "分析6月的期权操作有没有不合理",
        "0700.HK",
        "FUTU",
        "PDD",
        "腾讯",
    )
    offenders: list[str] = []
    for path in sorted((ROOT / "src" / "application" / "assistant_copilot").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for item in forbidden:
            if item in text:
                offenders.append(f"{path.relative_to(ROOT)}:{item}")
    assert offenders == []


def test_assistant_copilot_run_cli_command(monkeypatch, capsys) -> None:
    import src.interfaces.cli.assistant_ops as assistant_ops
    import src.interfaces.cli.main as cli

    calls: list[dict[str, Any]] = []

    def _run_copilot_task(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "schema_version": "om-copilot-run-v1",
            "ok": True,
            "status": "answered",
            "response_text": "结论：测试回答。",
        }

    monkeypatch.setattr(assistant_ops, "run_copilot_task", _run_copilot_task)

    rc = cli.main([
        "assistant",
        "copilot-run",
        "--text",
        "分析6月的期权操作有没有不合理，需要优化的地方",
        "--config-key",
        "us",
        "--dry-run",
    ])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "assistant.copilot_run"
    assert payload["ok"] is True
    assert calls[0]["config_key"] == "us"
    assert calls[0]["dry_run"] is True
    assert calls[0]["limits"].max_tool_calls == 6
    assert calls[0]["limits"].max_model_turns == 4
