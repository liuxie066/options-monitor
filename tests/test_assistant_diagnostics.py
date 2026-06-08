from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.agent_loop import TOOL_PLAN_SCHEMA_VERSION, tool_plan_json_schema
from src.application.assistant.diagnostics import check_llm_translator


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


def test_llm_check_allows_disabled_translator_without_api_key(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, _assistant_config())

    out = check_llm_translator(
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
        check_llm_translator(
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
        check_llm_translator(
            repo_root=tmp_path,
            config_path=cfg_path,
            include_local_env_file=False,
        )

    assert exc.value.code == "CONFIG_ERROR"
    assert "assistant config validation failed" in exc.value.message
    assert exc.value.details["error"] == "assistant.mode legacy modes are retired; use assistant.enabled and assistant.planner.enabled"


def test_llm_check_rejects_business_runtime_config_as_assistant_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(
        json.dumps({"accounts": ["sy"], "symbols": [{"symbol": "NVDA"}], "assistant": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(AgentToolError) as exc:
        check_llm_translator(
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

    out = check_llm_translator(
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

    out = check_llm_translator(
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


def test_llm_check_live_probe_uses_read_only_structured_translation(tmp_path: Path) -> None:
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

    def _create_response(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        assert kwargs["json_schema"] == tool_plan_json_schema()
        return {
            "output_text": json.dumps(
                {
                    "schema_version": TOOL_PLAN_SCHEMA_VERSION,
                    "goal": "check runtime status",
                    "response_mode": "canonical",
                    "required_capabilities": [],
                    "steps": [
                        {
                            "id": "status",
                            "tool_name": "runtime_status",
                            "arguments": {},
                            "purpose": "read runtime status",
                        }
                    ],
                }
            )
        }

    out = check_llm_translator(
        repo_root=tmp_path,
        config_path=cfg_path,
        env_file=env_file,
        include_local_env_file=False,
        live=True,
        live_text="状态",
        create_response_fn=_create_response,
    )

    assert out["summary"]["ok"] is True
    assert out["summary"]["live_checked"] is True
    assert calls[0]["api_key"] == "sk-test"
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["live_probe"]["status"] == "ok"
    assert checks["live_probe"]["value"]["plan"]["steps"][0]["tool_name"] == "runtime_status"
