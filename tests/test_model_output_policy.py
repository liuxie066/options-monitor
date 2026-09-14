from __future__ import annotations

import json

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.diagnostics import check_assistant_llm
from src.application.assistant.llm_model_profiles import parse_model_profile, resolve_authoring_assistant_config
from src.application.assistant.settings import AssistantSettings
from src.application.bot.model_config import ModelSettings
from src.application.config_defaults import default_config
from src.application.config_validator import validate_assistant_config
from src.application.llm_provider_registry import resolve_output_reservation
from src.infrastructure.openai_chat_completions import create_chat_completion
from src.infrastructure.openai_responses import create_response


def model_config(**changes):
    return {"provider": "deepseek", "model": "deepseek-v4-pro", "context_window_tokens": 1_000_000, **changes}


@pytest.mark.parametrize("override", [{}, {"max_output_tokens": None}, {"max_output_tokens": 8192}])
def test_output_policy_survives_profile_selection_and_public_readback(tmp_path, override):
    raw = model_config(**override)
    expected = override.get("max_output_tokens")
    profile = parse_model_profile("native", raw)
    assistant, _ = resolve_authoring_assistant_config({
        "enabled": True, "bot": {"enabled": True}, "active_model": "native", "models": {"native": raw},
    })
    cfg = {"assistant": assistant}
    validate_assistant_config(cfg)
    settings = ModelSettings.from_config(assistant["llm"])
    assert profile.public_payload()["max_output_tokens"] == expected
    assert settings.max_output_tokens == expected
    assert settings.output_reservation_tokens == (384_000 if expected is None else expected)
    assert settings.process_payload()["output_reservation_tokens"] == settings.output_reservation_tokens
    assert AssistantSettings.from_runtime_config(cfg).public_payload()["llm"]["max_output_tokens"] == expected
    path = tmp_path / "config.assistant.json"
    path.write_text(json.dumps(cfg))
    result = check_assistant_llm(repo_root=tmp_path, config_path=path, include_local_env_file=False)
    assert result["llm"]["max_output_tokens"] == expected
    limits = next(check for check in result["checks"] if check["name"] == "limits")
    assert limits["value"]["max_output_tokens"] == expected
    assert limits["message"] == ("provider default output limit" if expected is None else "configured output token limit")


@pytest.mark.parametrize("changes, message", [
    ({"model": "deepseek-v4-pro-preview"}, "specified explicitly"),
    ({"provider": "openai"}, "specified explicitly"),
    ({"model": "deepseek-chat"}, "specified explicitly"),
    ({"context_window_tokens": 128_000}, "output reservation 384000"),
    ({"context_window_tokens": 1_000_001}, "maximum 1000000"),
    ({"max_output_tokens": 384_001}, "maximum 384000"),
    ({"model": "unknown", "context_window_tokens": 10_000, "max_output_tokens": 8192}, "output reservation 8192"),
    *[({"max_output_tokens": value}, "integer >= 64") for value in [True, 0, 63, -1, 8192.0, "8192", ""]],
])
def test_output_policy_rejects_unverified_or_invalid_configuration(changes, message):
    raw = model_config(**changes)
    with pytest.raises(ValueError, match=message):
        ModelSettings.from_config(raw)
    with pytest.raises(AgentToolError, match=message):
        parse_model_profile("test", raw)
    with pytest.raises(SystemExit):
        validate_assistant_config({"assistant": {"enabled": True, "bot": {"enabled": True}, "llm": raw}})


def test_explicit_legacy_and_exact_native_capabilities():
    assert resolve_output_reservation("deepseek", "deepseek-v4-flash", 1_000_000, None) == 384_000
    settings = ModelSettings.from_config(model_config(model="unknown", context_window_tokens=24_000, max_output_tokens=8192))
    assert settings.max_output_tokens == settings.output_reservation_tokens == 8192
    # Direct construction remains compatible with existing explicit settings fixtures.
    direct = ModelSettings("openai", "openai-responses", "test", "http://127.0.0.1", "KEY", "", 90, 24000, 2048, 1)
    assert direct.process_payload()["output_reservation_tokens"] == 2048
    assert default_config()["defaults"]["assistant"]["llm"]["max_output_tokens"] is None


@pytest.mark.parametrize("cap", [None, 8192])
@pytest.mark.parametrize("api", ["chat", "responses"])
def test_legacy_transports_preserve_optional_wire_cap(cap, api):
    calls = []
    def post(url, payload, **kwargs):
        calls.append(payload)
        return {}
    if api == "chat":
        create_chat_completion(api_key="fixture", model="fixture", messages=[], max_output_tokens=cap, http_post_json_fn=post)
        key = "max_tokens"
    else:
        create_response(api_key="fixture", model="fixture", input_items=[], instructions="", max_output_tokens=cap, http_post_json_fn=post)
        key = "max_output_tokens"
    assert calls[0].get(key) == cap
    assert (key in calls[0]) == (cap is not None)
