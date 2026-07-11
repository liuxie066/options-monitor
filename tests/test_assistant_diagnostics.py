from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.diagnostics import check_assistant_llm
from src.application.copilot.model_config import model_api_key_configured


def _assistant_config(*, llm: dict[str, Any] | None = None) -> dict[str, Any]:
    llm_cfg = dict(llm or {"enabled": False})
    enabled = bool(llm_cfg.pop("enabled", False))
    return {
        "assistant": {
            "enabled": True,
            "context_window_messages": 8,
            "default_market_scope": "us",
            "copilot": {"enabled": enabled},
            "llm": llm_cfg,
        },
    }


def _write_config(tmp_path: Path, cfg: dict[str, Any]) -> Path:
    path = tmp_path / "config.assistant.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


def test_llm_check_allows_disabled_copilot_without_api_key(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, _assistant_config())

    out = check_assistant_llm(
        repo_root=tmp_path,
        config_path=cfg_path,
        include_local_env_file=False,
    )

    assert out["summary"]["ok"] is True
    assert out["summary"]["status"] == "disabled"
    assert out["llm"]["enabled"] is False
    assert "runtime_status" in out["capabilities"]["pure_read_tools"]
    assert "manual_trade_open" not in out["capabilities"]["pure_read_tools"]
    assert out["llm"]["api_key_configured"] is False
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["enabled"]["status"] == "warn"
    assert checks["provider"]["status"] == "skipped"
    assert checks["live_probe"]["status"] == "skipped"


def test_ollama_model_config_does_not_require_api_key() -> None:
    assert model_api_key_configured({"provider": "ollama", "model": "gpt-oss:20b"}, environ={}) == (True, None)


def test_llm_check_reports_ready_ollama_without_api_key(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "ollama",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "gpt-oss:20b",
                "api_key_env": "",
            }
        ),
    )

    out = check_assistant_llm(repo_root=tmp_path, config_path=cfg_path, include_local_env_file=False)

    assert out["summary"]["status"] == "ready"
    assert out["llm"]["api_key_configured"] is True
    checks = {item["name"]: item for item in out["checks"]}
    assert checks["api_key_env"]["message"] == "provider does not require an API key environment variable"
    assert checks["api_key"]["status"] == "ok"
    assert checks["api_key"]["message"] == "provider does not require an API key"


def test_llm_check_rejects_missing_explicit_assistant_config(tmp_path: Path) -> None:
    with pytest.raises(AgentToolError) as exc:
        check_assistant_llm(
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
        check_assistant_llm(
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
        check_assistant_llm(
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

    out = check_assistant_llm(
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

    out = check_assistant_llm(
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

    out = check_assistant_llm(
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

    out = check_assistant_llm(
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


def test_llm_check_live_probe_skips_removed_provider_planner(tmp_path: Path) -> None:
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

    out = check_assistant_llm(
        repo_root=tmp_path,
        config_path=cfg_path,
        env_file=env_file,
        include_local_env_file=False,
        live=True,
    )

    assert out["summary"]["ok"] is True
    assert out["summary"]["live_checked"] is True
    checks = {item["name"]: item for item in out["checks"]}
    live_probe = checks["live_probe"]
    assert live_probe["status"] == "skipped"
    assert live_probe["message"] == (
        "provider diagnostics are configuration-only; use Copilot execution for an end-to-end model probe"
    )
    assert live_probe["value"] == {"live_requested": True, "probe_count": 0, "copilot_runtime": True}
