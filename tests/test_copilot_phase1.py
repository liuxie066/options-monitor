from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.application.copilot.agent import AgentState
from src.application.copilot.contracts import (
    AppResult,
    CopilotRequest,
    CopilotScope,
    SceneManifest,
    new_id,
)
from src.application.copilot.host import run_contract
from src.application.copilot.local_harness import run_local_request
from src.application.copilot.model_client import ACTION_JSON_SCHEMA
from src.application.copilot.model_decider import ModelActionDecider
from src.application.copilot.rendering import render_user_response
from src.application.copilot.scene import build_scene_manifest
from src.application.copilot.service import prepare_contract
from src.application.copilot.result_admission import admit_result
from src.interfaces.cli.copilot_ops import handle_copilot_command


ROOT = Path(__file__).resolve().parents[1]
COPILOT_FIXTURES = ROOT / "tests" / "fixtures" / "copilot"


def _request(
    text: str,
    *,
    config_key: str | None = "us",
    symbol: str | None = None,
    month: str | None = None,
    environment: str = "local",
    scene: str | None = None,
    fixture: str | None = None,
) -> CopilotRequest:
    overrides = {}
    if scene:
        overrides["scene_name"] = scene
    if fixture:
        overrides["fixture_id"] = fixture
    return CopilotRequest(
        request_id=new_id("test_req"),
        source_entry="cli",
        user_message=text,
        explicit_scope=CopilotScope(config_key=config_key, symbol=symbol, month=month),
        execution_environment=environment,
        debug_overrides=overrides,
    )


def _model_action_fixture(name: str) -> str:
    return (COPILOT_FIXTURES / name).read_text(encoding="utf-8")


def test_action_json_schema_is_strict_provider_compatible() -> None:
    def assert_strict_objects(schema: object, path: str = "$") -> None:
        if isinstance(schema, dict):
            properties = schema.get("properties")
            if isinstance(properties, dict):
                required = schema.get("required")
                assert isinstance(required, list), path
                assert set(properties) <= set(required), path
            for key, value in schema.items():
                assert_strict_objects(value, f"{path}.{key}")
        elif isinstance(schema, list):
            for index, item in enumerate(schema):
                assert_strict_objects(item, f"{path}[{index}]")

    assert_strict_objects(ACTION_JSON_SCHEMA)


def test_service_projects_scene_to_host_manifest_without_answer_markers() -> None:
    prepared = prepare_contract(_request("NVDA 为什么没有通过筛选"), reference_year=2026)

    assert not isinstance(prepared, AppResult)
    assert prepared.scene_name == "operations_diagnostics"
    assert prepared.decision_trace["requested_capabilities"] == ["candidate_filter_diagnostics"]

    manifest = build_scene_manifest(prepared, "run_test")
    assert manifest.allowed_tools == ["runtime_status", "candidate_filter_explain"]
    assert "answer_focus_markers" not in manifest.task_guidance
    assert "answer_dimension_markers" not in manifest.task_guidance
    assert manifest.task_guidance["instructions"]


def test_write_like_request_is_refused_before_host() -> None:
    prepared = prepare_contract(_request("帮我把 0700 put 加到配置里"), reference_year=2026)

    assert isinstance(prepared, AppResult)
    assert prepared.status == "refused"
    assert "只读问题" in prepared.answer_report.conclusion
    assert prepared.decision_trace["safety_hits"]


def test_channel_environment_keeps_monthly_option_review_not_ready() -> None:
    prepared = prepare_contract(
        _request(
            "分析6月的期权操作有没有不合理，需要优化的地方",
            month="2026-06",
            environment="channel",
        ),
        reference_year=2026,
    )

    assert isinstance(prepared, AppResult)
    assert prepared.status == "not_ready"
    assert "渠道自由问答尚未开放" in prepared.answer_report.conclusion


def test_channel_environment_prepares_monthly_income_attribution() -> None:
    prepared = prepare_contract(
        _request("7月收益", environment="channel"),
        reference_year=2026,
    )

    assert not isinstance(prepared, AppResult)
    assert prepared.scene_name == "monthly_income_attribution"
    assert prepared.execution_environment == "channel"
    assert prepared.input["month"] == "2026-07"
    assert prepared.decision_trace["requested_capabilities"] == ["monthly_income_attribution"]
    assert prepared.decision_trace["scope_sources"]["month"] == "message.month"


def test_monthly_option_review_is_not_a_dedicated_copilot_capability() -> None:
    prepared = prepare_contract(
        _request("分析6月的期权操作有没有不合理，需要优化的地方", month="2026-06"),
        reference_year=2026,
    )

    assert isinstance(prepared, AppResult)
    assert prepared.status == "needs_clarification"
    assert prepared.decision_trace["selected_scene"] is None
    assert "monthly_option_review" not in prepared.decision_trace["candidate_scenes"]


def test_local_runtime_question_runs_service_host_agent_loop(monkeypatch) -> None:
    from src.application.copilot import tools as copilot_tools

    calls: list[tuple[str, dict]] = []

    def fake_call(tool_name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        assert tool_name in allowed_tools
        calls.append((tool_name, dict(payload)))
        return {
            "tool_name": tool_name,
            "ok": True,
            "data": {"status": "ok", "freshness": {"status": "fresh"}},
        }

    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)

    prepared = prepare_contract(_request("运行健康度怎么样"), reference_year=2026)
    assert not isinstance(prepared, AppResult)

    result = render_user_response(run_contract(prepared))

    assert calls == [("runtime_status", {"config_key": "us"})]
    assert result.status == "answered"
    assert result.ok is True
    assert result.answer_report.evidence_refs == ["obs_1"]
    assert result.user_response.startswith("结论")


def test_model_decider_falls_back_to_default_tool_collection_on_model_error() -> None:
    def broken_model(_request: dict) -> dict:
        raise RuntimeError("model unavailable")

    manifest = SceneManifest(
        run_id="run_test",
        scene_name="operations_diagnostics",
        execution_environment="local",
        messages=[{"role": "user", "content": "运行健康度怎么样"}],
        allowed_tools=["runtime_status"],
        limits={"max_model_turns": 3, "max_tool_calls": 3, "timeout_seconds": 30},
        output_schema={"type": "AnswerReport"},
    )

    action = ModelActionDecider(broken_model)(AgentState(manifest=manifest))

    assert action.kind == "tool"
    assert action.tool_name == "runtime_status"
    assert action.error_code == "MODEL_ERROR"


def test_result_admission_rejects_external_action_claim() -> None:
    from src.application.copilot.contracts import AnswerReport, AppResult

    result = admit_result(
        AppResult(
            status="answered",
            answer_report=AnswerReport(
                conclusion="结论：已发送通知。",
                attempted_checks=["runtime_status"],
                findings=[{"summary": "runtime ok", "evidence_refs": ["obs_1"]}],
                missing_data=[],
                evidence_refs=["obs_1"],
            ),
        )
    )

    assert result.status == "failed"
    assert result.ok is False
    assert result.answer_report.missing_data == ["mutation_claim"]


def test_cli_copilot_eval_accepts_model_action_file() -> None:
    payload = handle_copilot_command(
        argparse.Namespace(
            copilot_command="eval",
            scene="operations_diagnostics",
            fixture="candidate_filter_diagnostics_model_ready",
            text="NVDA 为什么没入选",
            config_key="us",
            symbol="NVDA",
            month=None,
            include_events=False,
            model_config_json=None,
            assistant_config=None,
            model_action_json=None,
            model_action_json_file=str(COPILOT_FIXTURES / "candidate_filter_diagnostics_model_action.json"),
        )
    )

    assert payload["status"] == "answered"
    assert payload["ok"] is True
    assert "Delta 过低" in payload["user_response"]


def test_cli_copilot_run_uses_local_tools(monkeypatch) -> None:
    from src.application.copilot import tools as copilot_tools

    calls: list[str] = []

    def fake_call(tool_name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        calls.append(tool_name)
        return {"tool_name": tool_name, "ok": True, "data": {"status": "ok"}}

    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)

    payload = handle_copilot_command(
        argparse.Namespace(
            copilot_command="run",
            text="运行健康度怎么样",
            config_key="us",
            symbol=None,
            month=None,
            include_events=False,
            model_config_json=None,
            assistant_config=None,
        )
    )

    assert calls == ["runtime_status"]
    assert payload["status"] == "answered"
    assert payload["ok"] is True


def test_cli_copilot_run_assistant_config_uses_model_synthesis(monkeypatch, tmp_path: Path) -> None:
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
    model_requests: list[dict] = []
    tool_calls: list[str] = []

    def fake_build_action_model(settings: CopilotModelSettings):
        assert settings.provider == "openai"
        assert settings.model == "gpt-test"
        assert settings.api_key_env == "OM_TEST_KEY"

        def model(request: dict) -> dict:
            model_requests.append(request)
            missing = request["finish_conditions"]["unattempted_tools_without_evidence"]
            if missing:
                return {"kind": "tool", "tool_name": missing[0], "reason": "need runtime evidence", "answer_report": None}
            return {
                "kind": "finish",
                "tool_name": None,
                "reason": "runtime evidence is enough",
                "answer_report": {
                    "conclusion": "结论：本地模型已基于 runtime_status 形成只读回答。",
                    "attempted_checks": ["runtime_status"],
                    "findings": [{"summary": "runtime_status 显示运行状态为 ok。", "evidence_refs": ["obs_1"]}],
                    "recommendations": [],
                    "missing_data": [],
                    "evidence_refs": ["obs_1"],
                },
            }

        return model

    def fake_call(tool_name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        assert tool_name in allowed_tools
        tool_calls.append(tool_name)
        return {"tool_name": tool_name, "ok": True, "data": {"status": "ok", "freshness": {"status": "fresh"}}}

    monkeypatch.setattr(local_harness, "build_action_model", fake_build_action_model)
    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)

    payload = handle_copilot_command(
        argparse.Namespace(
            copilot_command="run",
            text="运行健康度怎么样",
            config_key="us",
            symbol=None,
            month=None,
            include_events=False,
            model_config_json=None,
            assistant_config=str(assistant_config),
        )
    )

    assert tool_calls == ["runtime_status"]
    assert len(model_requests) == 2
    assert payload["status"] == "answered"
    assert payload["ok"] is True
    assert "本地模型已基于 runtime_status" in payload["user_response"]


def test_cli_copilot_run_rejects_invalid_model_synthesis(monkeypatch) -> None:
    from src.application.copilot import local_harness
    from src.application.copilot import tools as copilot_tools

    monkeypatch.setenv("OM_TEST_KEY", "sk-test")

    def fake_build_action_model(_settings):
        def model(request: dict) -> dict:
            missing = request["finish_conditions"]["unattempted_tools_without_evidence"]
            if missing:
                return {"kind": "tool", "tool_name": missing[0], "reason": "need runtime evidence", "answer_report": None}
            return {
                "kind": "finish",
                "tool_name": None,
                "reason": "invalid report without cited findings",
                "answer_report": {
                    "conclusion": "结论：运行正常。",
                    "attempted_checks": ["runtime_status"],
                    "findings": [],
                    "recommendations": [],
                    "missing_data": [],
                    "evidence_refs": [],
                },
            }

        return model

    def fake_call(tool_name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        return {"tool_name": tool_name, "ok": True, "data": {"status": "ok", "freshness": {"status": "fresh"}}}

    monkeypatch.setattr(local_harness, "build_action_model", fake_build_action_model)
    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)

    payload = handle_copilot_command(
        argparse.Namespace(
            copilot_command="run",
            text="运行健康度怎么样",
            config_key="us",
            symbol=None,
            month=None,
            include_events=False,
            model_config_json=json.dumps(
                {"provider": "openai", "model": "gpt-test", "api_key_env": "OM_TEST_KEY"},
                ensure_ascii=False,
            ),
            assistant_config=None,
        )
    )

    assert payload["status"] == "insufficient_evidence"
    assert payload["ok"] is True
    assert "cited findings" in payload["answer_report"]["missing_data"]


def test_cli_copilot_run_missing_model_key_fails_before_tools(monkeypatch) -> None:
    from src.application.copilot import tools as copilot_tools

    monkeypatch.delenv("OM_MISSING_TEST_KEY", raising=False)
    tool_calls: list[str] = []

    def fake_call(tool_name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        tool_calls.append(tool_name)
        return {"tool_name": tool_name, "ok": True, "data": {"status": "ok"}}

    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)

    payload = handle_copilot_command(
        argparse.Namespace(
            copilot_command="run",
            text="运行健康度怎么样",
            config_key="us",
            symbol=None,
            month=None,
            include_events=False,
            model_config_json=json.dumps(
                {"provider": "openai", "model": "gpt-test", "api_key_env": "OM_MISSING_TEST_KEY"},
                ensure_ascii=False,
            ),
            assistant_config=None,
        )
    )

    assert tool_calls == []
    assert payload["status"] == "failed"
    assert payload["ok"] is False
    assert payload["answer_report"]["missing_data"] == ["model_api_key_missing"]


def test_copilot_code_does_not_reintroduce_marker_based_answer_guard() -> None:
    forbidden = (
        "answer_focus_markers",
        "answer_dimension_markers",
        "claim_marker",
        "answer_quality",
        "has_conflicting_evidence_use",
        "has_recommendations_without",
        "has_conclusion_without",
    )
    for path in (ROOT / "src" / "application" / "copilot").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in text, f"{marker} leaked into {path}"
