from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant import diagnostics as assistant_diagnostics
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.diagnostics import check_llm_planner
from src.application.assistant.agent_loop import ModelTurnResult
from src.application.assistant.session_store import AgentSessionStore, collect_assistant_trace


def _assistant_config(*, llm: dict[str, Any] | None = None) -> dict[str, Any]:
    llm_cfg = dict(llm or {"enabled": False})
    enabled = bool(llm_cfg.pop("enabled", False))
    return {
        "assistant": {
            "enabled": True,
            "context_window_messages": 8,
            "default_market_scope": "us",
            "planner": {"enabled": enabled},
            "llm": llm_cfg,
        },
    }


def _write_config(tmp_path: Path, cfg: dict[str, Any]) -> Path:
    path = tmp_path / "config.assistant.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


def test_llm_check_allows_disabled_planner_without_api_key(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, _assistant_config())

    out = check_llm_planner(
        repo_root=tmp_path,
        config_path=cfg_path,
        include_local_env_file=False,
    )

    assert out["summary"]["ok"] is True
    assert out["summary"]["status"] == "disabled"
    assert out["llm"]["enabled"] is False
    assert "runtime_status" in out["capabilities"]["llm_executable_intents"]
    assert "manual_trade_open" in out["capabilities"]["known_non_executable_intents"]
    assert out["llm"]["api_key_configured"] is False
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["enabled"]["status"] == "warn"
    assert checks["provider"]["status"] == "skipped"
    assert checks["live_probe"]["status"] == "skipped"


def test_llm_check_rejects_missing_explicit_assistant_config(tmp_path: Path) -> None:
    with pytest.raises(AgentToolError) as exc:
        check_llm_planner(
            repo_root=tmp_path,
            config_path=tmp_path / "missing.assistant.json",
            include_local_env_file=False,
        )

    assert exc.value.code == "CONFIG_ERROR"
    assert "assistant config not found" in exc.value.message


def test_llm_check_rejects_invalid_assistant_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.assistant.json"
    cfg_path.write_text(
        json.dumps({"assistant": {"mode": "unknown"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(AgentToolError) as exc:
        check_llm_planner(
            repo_root=tmp_path,
            config_path=cfg_path,
            include_local_env_file=False,
        )

    assert exc.value.code == "CONFIG_ERROR"
    assert "assistant config validation failed" in exc.value.message
    assert exc.value.details["error"] == "assistant has unsupported keys: mode"


def test_llm_check_rejects_business_runtime_config_as_assistant_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(
        json.dumps({"accounts": ["sy"], "symbols": [{"symbol": "NVDA"}], "assistant": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(AgentToolError) as exc:
        check_llm_planner(
            repo_root=tmp_path,
            config_path=cfg_path,
            include_local_env_file=False,
        )

    assert exc.value.code == "CONFIG_ERROR"
    assert "use config.assistant.json, not config.<market>.json" in exc.value.details["error"]


def test_llm_check_reports_ready_custom_openai_compatible_endpoint(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "openai",
                "base_url": "https://llm.example/v1",
                "model": "gpt-5.2",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
                "timeout_seconds": 9,
                "max_output_tokens": 777,
            }
        ),
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_LLM_API_KEY=sk-test\n", encoding="utf-8")

    out = check_llm_planner(
        repo_root=tmp_path,
        config_path=cfg_path,
        env_file=env_file,
        include_local_env_file=False,
    )

    assert out["summary"]["ok"] is True
    assert out["summary"]["status"] == "ready"
    assert out["env"]["env_file_loaded"] is True
    assert out["llm"]["endpoint_url"] == "https://llm.example/v1/responses"
    assert out["llm"]["responses_url"] == "https://llm.example/v1/responses"
    assert out["llm"]["chat_completions_url"] is None
    assert out["llm"]["api_key_configured"] is True
    assert out["llm"]["api_key_source"] == f"env_file:{env_file.resolve()}"
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["api_key"]["value"]["configured"] is True
    assert checks["live_probe"]["status"] == "skipped"


def test_llm_check_reports_ready_deepseek_endpoint(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-v4-flash",
                "api_key_env": "DEEPSEEK_API_KEY",
                "confidence_min": 0.75,
                "timeout_seconds": 9,
                "max_output_tokens": 777,
            }
        ),
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")

    out = check_llm_planner(
        repo_root=tmp_path,
        config_path=cfg_path,
        env_file=env_file,
        include_local_env_file=False,
    )

    assert out["summary"]["ok"] is True
    assert out["summary"]["status"] == "ready"
    assert out["llm"]["endpoint_url"] == "https://api.deepseek.com/chat/completions"
    assert out["llm"]["responses_url"] is None
    assert out["llm"]["chat_completions_url"] == "https://api.deepseek.com/chat/completions"
    assert out["llm"]["api_key_configured"] is True
    assert out["llm"]["api_key_source"] == f"env_file:{env_file.resolve()}"
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["provider"]["value"] == "deepseek"
    assert checks["base_url"]["value"]["endpoint_url"] == "https://api.deepseek.com/chat/completions"
    assert checks["live_probe"]["status"] == "skipped"


def test_llm_check_reports_ready_kimi_endpoint(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "kimi",
                "base_url": "https://api.moonshot.ai/v1",
                "model": "kimi-k2.7-code",
                "api_key_env": "MOONSHOT_API_KEY",
                "confidence_min": 0.75,
                "timeout_seconds": 9,
                "max_output_tokens": 777,
            }
        ),
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("MOONSHOT_API_KEY=sk-test\n", encoding="utf-8")

    out = check_llm_planner(
        repo_root=tmp_path,
        config_path=cfg_path,
        env_file=env_file,
        include_local_env_file=False,
    )

    assert out["summary"]["ok"] is True
    assert out["summary"]["status"] == "ready"
    assert out["llm"]["endpoint_url"] == "https://api.moonshot.ai/v1/chat/completions"
    assert out["llm"]["responses_url"] is None
    assert out["llm"]["chat_completions_url"] == "https://api.moonshot.ai/v1/chat/completions"
    assert out["llm"]["api_key_configured"] is True
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["provider"]["value"] == "kimi"
    assert checks["base_url"]["value"]["endpoint_url"] == "https://api.moonshot.ai/v1/chat/completions"


def test_llm_check_reports_ready_kimi_code_endpoint(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "kimi-code",
                "base_url": "https://api.kimi.com/coding/v1",
                "model": "kimi-for-coding",
                "api_key_env": "KIMI_API_KEY",
                "confidence_min": 0.75,
                "timeout_seconds": 9,
                "max_output_tokens": 777,
            }
        ),
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("KIMI_API_KEY=sk-test\n", encoding="utf-8")

    out = check_llm_planner(
        repo_root=tmp_path,
        config_path=cfg_path,
        env_file=env_file,
        include_local_env_file=False,
    )

    assert out["summary"]["ok"] is True
    assert out["summary"]["status"] == "ready"
    assert out["llm"]["endpoint_url"] == "https://api.kimi.com/coding/v1/chat/completions"
    assert out["llm"]["responses_url"] is None
    assert out["llm"]["chat_completions_url"] == "https://api.kimi.com/coding/v1/chat/completions"
    assert out["llm"]["api_key_configured"] is True
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["provider"]["value"] == "kimi-code"
    assert checks["base_url"]["value"]["endpoint_url"] == "https://api.kimi.com/coding/v1/chat/completions"


def test_llm_check_live_probe_uses_read_only_tool_call_planning(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "openai",
                "model": "gpt-5.2",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
            }
        ),
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_LLM_API_KEY=sk-test\n", encoding="utf-8")

    def _create_tool_call_response(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_status_1",
                    "name": "runtime_status",
                    "arguments": "{}",
                }
            ]
        }

    out = check_llm_planner(
        repo_root=tmp_path,
        config_path=cfg_path,
        env_file=env_file,
        include_local_env_file=False,
        live=True,
        live_text="状态",
        create_tool_call_response_fn=_create_tool_call_response,
    )

    assert out["summary"]["ok"] is True
    assert out["summary"]["live_checked"] is True
    assert calls[0]["api_key"] == "sk-test"
    assert "tools" in calls[0]
    assert calls[0]["tools"][0]["type"] == "function"
    assert "json_schema" not in calls[0]
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["live_probe"]["status"] == "ok"
    assert checks["live_probe"]["value"]["plan"] is None
    assert checks["live_probe"]["value"]["event_plan"]["steps"][0]["tool_name"] == "runtime_status"
    assert checks["live_probe"]["value"]["event_plan"]["events"][0]["event_type"] == "model_tool_call"


def test_llm_check_live_probe_supports_multiple_read_only_probe_texts(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "openai",
                "model": "gpt-5.2",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
            }
        ),
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_LLM_API_KEY=sk-test\n", encoding="utf-8")

    responses = [
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_status_1",
                    "name": "runtime_status",
                    "arguments": "{}",
                }
            ]
        },
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_income_1",
                    "name": "monthly_income_report",
                    "arguments": '{"account":"sy","month":"2026-06","include_rows":true}',
                }
            ]
        },
    ]

    def _create_tool_call_response(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return responses[len(calls) - 1]

    out = check_llm_planner(
        repo_root=tmp_path,
        config_path=cfg_path,
        env_file=env_file,
        include_local_env_file=False,
        live=True,
        live_texts=["状态", "sy 6月收益来源拆一下"],
        create_tool_call_response_fn=_create_tool_call_response,
    )

    checks = {item["name"]: item for item in out["checks"]}
    live_probe = checks["live_probe"]
    assert out["summary"]["ok"] is True
    assert len(calls) == 2
    assert live_probe["status"] == "ok"
    assert live_probe["value"]["probe_count"] == 2
    assert [probe["text"] for probe in live_probe["value"]["probes"]] == ["状态", "sy 6月收益来源拆一下"]
    assert [probe["selected_tool"] for probe in live_probe["value"]["probes"]] == [
        "runtime_status",
        "monthly_income_report",
    ]
    assert [probe["event_type"] for probe in live_probe["value"]["probes"]] == [
        "model_tool_call",
        "model_tool_call",
    ]
    serialized = json.dumps(live_probe["value"], ensure_ascii=False).lower()
    assert "api_key" not in serialized
    assert "sk-test" not in serialized
    assert "raw_provider_payload" not in serialized


def test_llm_check_live_probe_marks_expected_tool_mismatch(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "openai",
                "model": "gpt-5.2",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
            }
        ),
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_LLM_API_KEY=sk-test\n", encoding="utf-8")

    def _create_tool_call_response(**_kwargs: Any) -> dict[str, Any]:
        return {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_status_1",
                    "name": "runtime_status",
                    "arguments": "{}",
                }
            ]
        }

    out = check_llm_planner(
        repo_root=tmp_path,
        config_path=cfg_path,
        env_file=env_file,
        include_local_env_file=False,
        live=True,
        live_text="状态",
        live_expected_tools=["monthly_income_report"],
        create_tool_call_response_fn=_create_tool_call_response,
    )

    live_probe = {item["name"]: item for item in out["checks"]}["live_probe"]
    probe = live_probe["value"]["probes"][0]
    assert out["summary"]["ok"] is False
    assert live_probe["status"] == "error"
    assert probe["expected_tool"] == "monthly_income_report"
    assert probe["selected_tool"] == "runtime_status"
    assert probe["tool_match"] is False


def test_llm_check_live_probe_marks_expected_event_type_mismatch(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "openai",
                "model": "gpt-5.2",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
            }
        ),
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_LLM_API_KEY=sk-test\n", encoding="utf-8")

    def _create_tool_call_response(**_kwargs: Any) -> dict[str, Any]:
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "你好，我可以帮你查询 OM 状态。"}],
                }
            ]
        }

    out = check_llm_planner(
        repo_root=tmp_path,
        config_path=cfg_path,
        env_file=env_file,
        include_local_env_file=False,
        live=True,
        live_text="你好",
        live_expected_event_types=["model_tool_call"],
        create_tool_call_response_fn=_create_tool_call_response,
    )

    live_probe = {item["name"]: item for item in out["checks"]}["live_probe"]
    probe = live_probe["value"]["probes"][0]
    assert out["summary"]["ok"] is False
    assert live_probe["status"] == "error"
    assert probe["expected_event_type"] == "model_tool_call"
    assert probe["event_type"] == "model_final_answer"
    assert probe["event_type_match"] is False


def test_llm_check_live_probe_marks_expected_argument_mismatch(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "openai",
                "model": "gpt-5.2",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
            }
        ),
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_LLM_API_KEY=sk-test\n", encoding="utf-8")

    def _create_tool_call_response(**_kwargs: Any) -> dict[str, Any]:
        return {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_income_1",
                    "name": "monthly_income_report",
                    "arguments": '{"account":"lx","month":"2026-06","include_rows":true}',
                }
            ]
        }

    out = check_llm_planner(
        repo_root=tmp_path,
        config_path=cfg_path,
        env_file=env_file,
        include_local_env_file=False,
        live=True,
        live_text="sy 6月收益来源拆一下",
        live_expected_tools=["monthly_income_report"],
        live_expected_event_types=["model_tool_call"],
        live_expected_arguments=[{"account": "sy", "month": "2026-06"}],
        create_tool_call_response_fn=_create_tool_call_response,
    )

    live_probe = {item["name"]: item for item in out["checks"]}["live_probe"]
    probe = live_probe["value"]["probes"][0]
    assert out["summary"]["ok"] is False
    assert live_probe["status"] == "error"
    assert probe["expected_arguments"] == {"account": "sy", "month": "2026-06"}
    assert probe["selected_arguments"]["account"] == "lx"
    assert probe["argument_match"] is False


def test_llm_check_live_probe_rejects_extra_expected_arguments(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "openai",
                "model": "gpt-5.2",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
            }
        ),
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_LLM_API_KEY=sk-test\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    def _create_tool_call_response(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_status_1",
                    "name": "runtime_status",
                    "arguments": "{}",
                }
            ]
        }

    with pytest.raises(AgentToolError) as exc:
        check_llm_planner(
            repo_root=tmp_path,
            config_path=cfg_path,
            env_file=env_file,
            include_local_env_file=False,
            live=True,
            live_texts=["状态"],
            live_expected_arguments=[
                {"config_key": "us"},
                {"account": "sy"},
            ],
            create_tool_call_response_fn=_create_tool_call_response,
        )

    assert exc.value.code == "INPUT_ERROR"
    assert "more expected arguments than live probe texts" in exc.value.message
    assert calls == []


def test_llm_check_rejects_live_expectations_without_live_probe(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "openai",
                "model": "gpt-5.2",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
            }
        ),
    )

    with pytest.raises(AgentToolError) as exc:
        check_llm_planner(
            repo_root=tmp_path,
            config_path=cfg_path,
            include_local_env_file=False,
            live=False,
            live_expected_tools=["runtime_status"],
        )

    assert exc.value.code == "INPUT_ERROR"
    assert "pass --live when using live probe expectations" in exc.value.message


def test_llm_check_live_probe_rejects_missing_event_native_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "openai",
                "model": "gpt-5.2",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
            }
        ),
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_LLM_API_KEY=sk-test\n", encoding="utf-8")

    def _create_model_turn_events_without_event_plan(*args: Any, **kwargs: Any) -> ModelTurnResult:
        return ModelTurnResult(trace={"event_model": {"event_count": 0}}, event_plan=None)

    monkeypatch.setattr(assistant_diagnostics, "create_model_turn_events", _create_model_turn_events_without_event_plan)

    out = check_llm_planner(
        repo_root=tmp_path,
        config_path=cfg_path,
        env_file=env_file,
        include_local_env_file=False,
        live=True,
        live_text="状态",
    )

    assert out["summary"]["ok"] is False
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["live_probe"]["status"] == "error"
    assert checks["live_probe"]["message"] == "provider did not return an event-native plan"
    assert checks["live_probe"]["value"]["plan"] is None
    assert checks["live_probe"]["value"]["event_plan"] is None


def test_assistant_trace_exposes_compact_diagnostics_without_raw_provider_payload(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"
    request = AssistantRequest(
        text="看一下状态",
        sender_id="u1",
        channel="local",
        message_id="msg_trace_diag",
        audit_db=str(audit_db),
    )
    snapshot = {
        "session_id": "session_trace_diag",
        "request": request.public_payload(),
        "goal": "看一下状态",
        "task_state": "done",
        "capability_selection": {
            "selected_tools": ["runtime_status"],
            "selected": [{"tool_name": "runtime_status", "effect": "read"}],
        },
        "progress": {
            "state": "done",
            "tool_call_count": 1,
            "blocked_by": [
                {
                    "kind": "evidence_gap",
                    "gap_kind": "analysis_breakdown_needed",
                    "suggested_tool": "analysis_query",
                }
            ],
        },
        "plan_revisions": [],
        "tool_transcript": [
            {
                "index": 1,
                "tool_name": "runtime_status",
                "payload": {"config_key": "us"},
                "authorized": True,
                "precheck": {"decision": "allow", "risk_class": "READ_AUTO"},
                "evidence_summary": {"row_count": 1},
                "ok": True,
            }
        ],
        "evidence_bundle": {"fact_count": 1, "dataset_count": 1},
        "answer_trace": {
            "answer_route": "llm_from_tool_observation",
            "final_response": {"status": "rendered", "reason": "model_final_answer"},
            "synthesis": {
                "event_loop": {
                    "loop_stop_reason": "model_final_answer",
                    "continuation_count": 1,
                    "raw_provider_payload": {"api_key": "sk-secret"},
                }
            },
        },
    }
    AgentSessionStore(audit_db).upsert_snapshot(
        snapshot=snapshot,
        command_id="cmd_trace_diag",
        request=request,
        response={"ok": True, "data": {"response_text": "状态正常。"}},
    )

    out = collect_assistant_trace(audit_db=str(audit_db), command_id="cmd_trace_diag")

    compact_trace = out["traces"][0]["compact_trace"]
    for key in {
        "selected_capability",
        "model_turns",
        "tool_observations",
        "evidence_gaps",
        "stop_reason",
        "answer_route",
    }:
        assert key in compact_trace
    serialized = json.dumps(compact_trace, ensure_ascii=False).lower()
    assert "raw_provider_payload" not in serialized
    assert "api_key" not in serialized
    assert compact_trace["model_turns"]["continuation_count"] == 1
    assert compact_trace["stop_reason"] == "model_final_answer"
