from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from src.application.bot import local_harness, tools as bot_tools
from src.application.bot import scene as bot_scene
from src.application.bot.contracts import AppResult, BotRequest, BotScope, SceneManifest, new_id
from src.application.bot.control_handoff import (
    CONTROL_PREVIEW_TOOL,
    build_control_preview_request,
    control_preview_tool_description,
)
from src.application.assistant.capability_catalog import preview_operation_capabilities
from src.application.bot.host_store import BotHostStore
from src.application.bot import channel_facade
from src.infrastructure.pi_agent_process import derive_pi_session_id
from src.application.bot.scene import GENERAL_SCENE, build_scene_manifest, load_general_scene
from src.application.bot.service import prepare_contract
from src.application.agent_tool_contracts import AgentToolError
from tests.bot_pi_test_support import (
    _TEST_MODEL,
    ModelRequest,
    ModelTurn,
    ToolCall,
    run_contract,
)


def _request(text: str, *, context=(), environment: str = "local") -> BotRequest:
    return BotRequest(
        request_id=new_id("test_req"),
        source_entry="test",
        user_message=text,
        explicit_scope=BotScope(config_key="us"),
        context_messages=tuple(context),
        execution_environment=environment,
        trusted_tool_scope=(
            {
                "authenticated_channel": "test",
                "authenticated_sender_id": "test-user",
                "authenticated_conversation_id": "test-conversation",
            }
            if environment == "channel"
            else {}
        ),
    )


def _contract(text: str = "最近有哪些值得关注的问题？"):
    prepared = prepare_contract(
        _request(text),
        reference_year=2026,
        report_now_ms=1788319188212,
    )
    assert not isinstance(prepared, AppResult)
    return prepared


def _call(name: str, arguments: dict, call_id: str = "call_1") -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments=arguments)


def _channel_session_id() -> str:
    return derive_pi_session_id("test", "test-user", "test-conversation", "key:us")


def _model_config(**overrides: object) -> str:
    """Serialize one model config.

    The defaults are the ollama config this module repeats most often, so a call
    site spells out only the fields that differ from it.
    """
    base = {
        "provider": "ollama",
        "model": "om-test",
        "context_window_tokens": 24_000,
    }
    base.update(overrides)
    return json.dumps(base)


def _run_prepared(prepared, **overrides: object):
    """Run one prepared contract with the Pi-ready assistant turn this module repeats.

    A call site that deliberately has no turn passes ``model_turn_json=None``.
    """
    base: dict[str, object] = {"model_turn_json": json.dumps({"text": "Pi runtime ready."})}
    base.update(overrides)
    return local_harness.run_prepared_contract(prepared, **base)


def _channel_request(
    monkeypatch,
    tmp_path,
    example_config_path,
    user_response: str = "结论：请明确要修改哪条预览。",
    **overrides: object,
):
    """Run one channel request through a stubbed model gate.

    The defaults are the pending-Control snapshot request this module repeats most
    often, so a call site spells out only the fields that differ from it. Returns
    the answered result and the messages the contract handed to the model.
    """
    captured: dict[str, object] = {}

    def fake_run(prepared, **_kwargs):  # type: ignore[no-untyped-def]
        captured["messages"] = prepared.input["messages"]
        return AppResult(status="answered", user_response=user_response)

    monkeypatch.setattr(channel_facade, "_channel_model_gate", lambda _path: None)
    monkeypatch.setattr(channel_facade, "run_prepared_contract", fake_run)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(example_config_path.parent))
    base: dict[str, object] = {
        "user_message": "改成 1.2.400",
        "config_key": "us",
        "assistant_config_path": str(tmp_path / "assistant.json"),
        "channel": "wechat",
        "sender_id": "ou_1",
        "conversation_id": "conversation-1",
        "host_db_path": str(tmp_path / "bot.sqlite3"),
        "control_context": (
            {
                "operation_id": "in_upgrade",
                "operation_type": "upgrade_now",
                "status": "previewed",
                "summary": "升级到最新版",
            },
        ),
    }
    base.update(overrides)
    result = channel_facade.run_channel_request(**base)
    return result, captured["messages"]


def _build_payload(name: str, arguments: dict, **fixed_input: object):
    """Build one Bot tool payload.

    Keyword arguments are the host-fixed input, so a call site spells out exactly
    the fixed fields it sets and nothing is added for it.
    """
    base: dict[str, object] = {}
    base.update(fixed_input)
    return bot_tools.build_tool_payload(name, arguments, fixed_input=base)


def test_service_is_thin_and_uses_one_general_scene() -> None:
    for text in (
        "7月收益",
        "结论呢",
        "最近有哪些值得关注的问题？",
        "分析平仓操作是否合理",
        "检查 OM 当前运行状态和配置",
    ):
        prepared = prepare_contract(_request(text), reference_year=2026)
        assert not isinstance(prepared, AppResult)
        assert prepared.scene_name == GENERAL_SCENE
        assert prepared.policy == {"read_only": True}
        assert prepared.decision_trace["selection_reason"] == "entry_surface_default"


def test_service_does_not_parse_business_scope_from_free_text() -> None:
    prepared = prepare_contract(_request("分析 0700.HK 的 7月收益"), reference_year=2026)
    assert not isinstance(prepared, AppResult)

    assert prepared.input["symbol"] is None
    assert prepared.input["month"] is None
    assert prepared.input["reference_year"] == 2026
    assert prepared.decision_trace["scope_sources"] == {"config_key": "explicit_scope.config_key"}


def test_service_preserves_explicit_scope_only() -> None:
    request = replace(
        _request("分析收益"),
        explicit_scope=BotScope(config_key="US", symbol="nvda", month="2026-07"),
    )
    prepared = prepare_contract(request, reference_year=2026)
    assert not isinstance(prepared, AppResult)

    assert prepared.input["config_key"] == "us"
    assert prepared.input["symbol"] == "NVDA"
    assert prepared.input["month"] == "2026-07"


def test_scene_manifest_owns_prompt_tools_and_runtime_limits():
    definition = load_general_scene()
    manifest = build_scene_manifest(_contract(), "test")
    assert definition["version"] == manifest.scene_version == "v7"
    assert manifest.messages[0]["content"] == definition["system_prompt"]
    assert manifest.limits["max_model_turns"] == 16
    assert manifest.limits["max_tool_calls"] == 12
    assert "candidate_filter_explain" in manifest.allowed_tools
    assert "runtime_logs" in manifest.allowed_tools
    assert "project_files" in manifest.allowed_tools
    assert "request_control_preview" not in manifest.allowed_tools
    assert "tool_directory" not in manifest.allowed_tools


def test_scene_selects_only_the_required_read_tools() -> None:
    definition = load_general_scene()
    manifest = build_scene_manifest(_contract(), "run_tools")
    expected = definition["tool_selection"]["names"]

    assert manifest.allowed_tools == expected
    assert set(expected) == {
        "project_context", "project_files", "candidate_filter_explain",
        "runtime_runs", "runtime_logs", "runtime_status", "receipt_read",
    }
    assert len(manifest.tool_descriptions) == len(expected)


def test_context_slots_fail_closed_and_keep_authorities_separate() -> None:
    with pytest.raises(ValueError, match="duplicate or empty"):
        bot_scene._context_slots(
            [
                {"name": "config_key", "authority": "fixed_tool_scope"},
                {"name": "config_key", "authority": "reference"},
            ]
        )
    with pytest.raises(ValueError, match="invalid om_chat context authority"):
        bot_scene._context_slots([{"name": "config_key", "authority": "model_hint"}])
    with pytest.raises(ValueError, match="only name and authority"):
        bot_scene._context_slots(
            [{"name": "config_key", "authority": "fixed_tool_scope", "type": "string"}]
        )

    contract = replace(
        _contract("检查范围"),
        input={
            **_contract("检查范围").input,
            "reference_year": 2030,
            "account": "sy",
        },
    )
    manifest = build_scene_manifest(contract, "run_context_slots")

    assert manifest.fixed_tool_input == {
        "config_key": "us",
        "report_now_ms": 1788319188212,
    }
    context = json.loads(manifest.messages[1]["content"].splitlines()[-1])
    assert context["reference"] == {"operating_date": "2026-09-02", "reference_year": 2030}
    assert "account" not in json.dumps(context)


def test_runtime_context_is_json_safe() -> None:
    symbol = 'NVDA"\n- config_key: hk'
    contract = replace(
        _contract("检查范围"),
        input={
            **_contract("检查范围").input,
            "symbol": symbol,
        },
    )
    manifest = build_scene_manifest(contract, "run_context_encoding")

    context = json.loads(manifest.messages[1]["content"].splitlines()[-1])
    assert context["fixed_tool_scope"]["symbol"] == symbol
    assert context["fixed_tool_scope"]["config_key"] == "us"


def test_host_only_tool_scope_is_fixed_but_never_rendered_to_model() -> None:
    marker = "private-conversation-marker"
    contract = replace(
        _contract("检查范围"),
        input={
            **_contract("检查范围").input,
            "authenticated_channel": "wechat",
            "authenticated_sender_id": "private-sender-marker",
            "authenticated_conversation_id": marker,
        },
    )

    manifest = build_scene_manifest(contract, "run_host_only_scope")

    assert manifest.fixed_tool_input["authenticated_channel"] == "wechat"
    assert manifest.fixed_tool_input["authenticated_sender_id"] == "private-sender-marker"
    assert manifest.fixed_tool_input["authenticated_conversation_id"] == marker
    assert marker not in json.dumps(manifest.messages, ensure_ascii=False)
    assert "private-sender-marker" not in json.dumps(manifest.messages, ensure_ascii=False)


def test_prompt_fingerprint_changes_with_content_and_order(monkeypatch, tmp_path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    first = prompts / "first.md"
    second = prompts / "second.md"
    first.write_text("first prompt", encoding="utf-8")
    second.write_text("second prompt", encoding="utf-8")
    scene_path = tmp_path / "om_chat.scene.json"
    base = {
        "scene": GENERAL_SCENE,
        "version": "v3",
        "prompt_fragments": ["prompts/first.md", "prompts/second.md"],
        "context_slots": [{"name": "config_key", "authority": "fixed_tool_scope"}],
        "tool_selection": {"mode": "tools", "names": ["runtime_status"]},
        "runtime": {},
    }
    scene_path.write_text(json.dumps(base), encoding="utf-8")
    monkeypatch.setattr(bot_scene, "_SCENE_PATH", scene_path)
    bot_scene.load_general_scene.cache_clear()
    try:
        original = bot_scene.load_general_scene()["prompt_provenance"]
        first.write_text("first prompt changed", encoding="utf-8")
        bot_scene.load_general_scene.cache_clear()
        changed = bot_scene.load_general_scene()["prompt_provenance"]
        scene_path.write_text(
            json.dumps(
                {
                    **base,
                    "prompt_fragments": ["prompts/second.md", "prompts/first.md"],
                }
            ),
            encoding="utf-8",
        )
        bot_scene.load_general_scene.cache_clear()
        reordered = bot_scene.load_general_scene()["prompt_provenance"]
    finally:
        bot_scene.load_general_scene.cache_clear()

    assert original["compiled_prompt_sha256"] != changed["compiled_prompt_sha256"]
    assert changed["compiled_prompt_sha256"] != reordered["compiled_prompt_sha256"]
    assert all("text" not in item for item in reordered["fragments"])


def test_tool_input_audit_keeps_supported_fields_and_hashes_free_form_values() -> None:
    audit = bot_tools.audit_tool_input(
        "option_positions_read",
        {
            "action": "list",
            "query": {"account": "lx", "symbol": "private_value"},
            "unsupported_secret": "do-not-store",
        },
        model_proposal=True,
    )

    assert audit["action"] == "list"
    assert set(audit["query"]) == {"type", "length", "sha256"}
    assert set(audit["unsupported_secret"]) == {"type", "length", "sha256"}
    serialized = json.dumps(audit, sort_keys=True)
    assert "private_value" not in serialized
    assert "do-not-store" not in serialized
    assert bot_tools.conservative_json_tokens(audit) <= bot_tools.MAX_OBSERVATION_TOKENS
    oversized = bot_tools.audit_tool_input(
        "option_positions_read",
        {"query": {"symbols": ["x" * 1_000 for _ in range(30)]}},
        model_proposal=True,
    )
    assert set(oversized["query"]) == {"type", "length", "sha256"}
    assert bot_tools.conservative_json_tokens(oversized) <= bot_tools.MAX_OBSERVATION_TOKENS


def test_event_cursor_only_input_selects_events_and_enforces_its_limit() -> None:
    from jsonschema import Draft202012Validator

    description = bot_tools.tool_descriptions(("option_positions_read",))[0]
    schema_errors = list(
        Draft202012Validator(description["input_schema"]).iter_errors(
            {"cursor": "opaque-cursor", "limit": 21}
        )
    )
    assert any(error.validator == "maximum" for error in schema_errors)

    payload, error = _build_payload("option_positions_read", {"cursor": "opaque-cursor", "limit": 20}, config_key="us")
    assert error is None
    assert payload is not None
    assert payload["action"] == "events"

    rejected, error = _build_payload("option_positions_read", {"action": "events", "limit": 21}, config_key="us")
    assert rejected is None
    assert error == "单次最多查询 20 条交易事件，请将数量设为 1 到 20。"

    listed, error = _build_payload("option_positions_read", {"action": "list", "limit": 500}, config_key="us")
    assert error is None
    assert listed is not None and listed["limit"] == 500

    rejected, error = _build_payload(
        "option_positions_read", {"action": "events", "query": {"account": "lx"}}, config_key="us"
    )
    assert rejected is None
    assert error == "unsupported fields for action=events: query"


def test_agent_tool_view_hides_paths_and_exposes_defaults() -> None:
    from src.application.agent_tool_registry import get_tool_definition

    candidate = next(item for item in bot_tools.tool_descriptions(("candidate_rank_explain",)))
    properties = candidate["input_schema"]["properties"]

    assert properties["mode"]["default"] == "all"
    assert properties["top_n"]["default"] == 10
    assert "candidate_path" not in properties
    assert "report_dir" not in properties

    positions = next(item for item in bot_tools.tool_descriptions(("option_positions_read",)))
    assert "latest N closed trades" in positions["description"]
    assert positions["input_schema"]["properties"]["action"]["type"] == "string"
    assert positions["input_schema"]["properties"]["status"]["type"] == "string"
    assert "quote_snapshots" not in positions["input_schema"]["properties"]
    assert "opend_host" not in positions["input_schema"]["properties"]

    performance = next(item for item in bot_tools.tool_descriptions(("option_performance_report",)))
    assert performance["default_input"] == {
        "config_key": "us",
        "period": "mtd",
        "view": "summary",
    }
    assert performance["input_schema"]["properties"]["period"]["default"] == "mtd"
    assert all(value is not None for value in performance["default_input"].values())
    assert "config_path" not in performance["input_schema"]["properties"]
    assert "data_config" not in performance["input_schema"]["properties"]

    external_positions = get_tool_definition("option_positions_read")
    assert external_positions is not None
    assert external_positions.input_json_schema()["properties"]["action"]["type"] == ["string", "array"]
    assert "quote_snapshots" in external_positions.input_json_schema()["properties"]
    assert "opend_host" in external_positions.input_json_schema()["properties"]
@pytest.mark.parametrize("period", ["mtd", "ytd"])
def test_option_performance_payload_accepts_mtd_ytd_cutoff(period: str) -> None:
    payload, error = _build_payload(
        "option_performance_report", {"period": period, "as_of_date": "2026-07-23"}
    )

    assert error is None
    assert payload is not None
    assert payload["period"] == period
    assert payload["as_of_date"] == "2026-07-23"
    assert "config_path" not in payload
    assert "data_config" not in payload


def test_option_performance_payload_accepts_natural_period_fields_but_not_rows() -> None:
    payload, error = _build_payload("option_performance_report", {"period": "month", "month": "2026-07"})

    assert error is None
    assert payload is not None
    assert payload["period"] == "month"
    assert payload["month"] == "2026-07"

    rejected, rejected_error = _build_payload(
        "option_performance_report", {"period": "month", "month": "2026-07", "include_rows": True}
    )
    assert rejected is None
    assert rejected_error == "unsupported Bot input fields for option_performance_report: include_rows"

    invalid_payload, invalid_error = _build_payload(
        "option_performance_report", {"period": "mtd", "account": ""}
    )
    assert invalid_payload is None
    assert invalid_error == "account must be non-empty when provided"

    explicit_null, null_error = _build_payload(
        "option_performance_report", {"period": "mtd", "config_path": None}
    )
    assert explicit_null is None
    assert null_error == (
        "unsupported Bot input fields for option_performance_report: config_path"
    )


@pytest.mark.parametrize(
    "hidden_input",
    ["log_file", "runs_root", "logs_root", "profile_path", "run_dir"],
)
def test_bot_rejects_hidden_runtime_log_path_inputs(hidden_input: str) -> None:
    payload, error = _build_payload("runtime_logs", {"run_id": "run-x", hidden_input: "/private/secret.txt"})

    assert payload is None
    assert error == f"unsupported Bot input fields for runtime_logs: {hidden_input}"


def test_bot_runtime_logs_binds_config_but_rejects_arbitrary_log_roots() -> None:
    payload, error = _build_payload("runtime_logs", {"run_id": "run-x", "limit": 5}, config_key="us")

    assert error is None
    assert payload == {"action": "scoped", "run_id": "run-x", "limit": 5, "config_key": "us"}
    rejected = bot_tools.call_read_tool("runtime_logs", {**payload, "logs_root": "/private/logs"}, allowed_tools=("runtime_logs",))
    assert rejected["ok"] is False and rejected["error"]["code"] == "INPUT_ERROR"


def test_bot_tool_description_never_exposes_host_owned_paths() -> None:
    descriptions = bot_tools.tool_descriptions(
        ("runtime_logs",),
        static_payloads={
            "runtime_logs": {
                "kind": "service",
                "logs_root": "/private/host-user/runtime/logs",
            }
        },
    )

    serialized = json.dumps(descriptions, ensure_ascii=False)
    assert descriptions[0]["default_input"] == {"action": "scoped"}
    assert "logs_root" not in serialized
    assert "/private/host-user" not in serialized


def test_bot_binds_operation_diagnostics_to_host_authenticated_scope() -> None:
    payload, error = _build_payload(
        "operation_timeline", {"operation_id": "op_1", "limit": 2},
        authenticated_channel="wechat", authenticated_sender_id="sender-a",
        authenticated_conversation_id="conversation-a",
    )

    assert error is None
    assert payload == {
        "limit": 2,
        "operation_id": "op_1",
        "authenticated_channel": "wechat",
        "authenticated_sender_id": "sender-a",
        "authenticated_conversation_id": "conversation-a",
    }
    rejected, rejected_error = _build_payload(
        "operation_timeline", {"sender_id": "sender-b"}, authenticated_sender_id="sender-a"
    )
    assert rejected is None
    assert rejected_error == "unsupported Bot input fields for operation_timeline: sender_id"


@pytest.mark.parametrize("marker", ["all", " ALL ", ":all", "__omit__"])
def test_option_performance_payload_omits_hosted_all_scope_markers(marker: str) -> None:
    payload, error = _build_payload(
        "option_performance_report", {"period": "mtd", "account": marker, "broker": marker},
        config_key="us",
    )

    assert error is None
    assert payload is not None
    assert payload["config_key"] == "us"
    assert payload["period"] == "mtd"
    assert "account" not in payload
    assert "broker" not in payload


def test_option_performance_payload_preserves_real_scope_filters(example_config_path) -> None:
    payload, error = _build_payload(
        "option_performance_report",
        {"period": "mtd", "account": " lx ", "broker": " 富途 "},
        config_path=str(example_config_path),
    )

    assert error is None
    assert payload is not None
    assert payload["account"] == "lx"
    assert payload["broker"] == "富途"


def test_option_performance_internal_clock_is_reset_after_each_read(monkeypatch) -> None:
    from src.application.agent_tools import materialization_impl

    seen: list[int | None] = []

    def fake_execute(_name: str, _payload: dict) -> dict:
        seen.append(materialization_impl._OPTION_PERFORMANCE_REPORT_NOW_MS.get())
        return {"ok": True, "data": {}}

    monkeypatch.setattr(bot_tools, "execute_tool", fake_execute)

    bot_tools.call_read_tool(
        "option_performance_report",
        {"period": "mtd"},
        allowed_tools=("option_performance_report",),
        now_ms=123,
    )
    bot_tools.call_read_tool(
        "option_performance_report",
        {"period": "mtd"},
        allowed_tools=("option_performance_report",),
    )
    assert seen == [123, None]


def test_symbol_inputs_are_structurally_required_without_fake_defaults() -> None:
    from src.application.agent_tool_registry import get_tool_definition

    for tool_name in ("symbol_resolve", "symbol_config_read"):
        definition = get_tool_definition(tool_name)
        assert definition is not None
        assert "symbol" in definition.input_json_schema()["required"]
        assert "symbol" not in definition.safe_default_input


def test_option_monitor_query_binding_exposes_plain_language_scenarios() -> None:
    from src.application.assistant.tool_bindings import binding_for_intent

    binding = binding_for_intent("daily_decision_brief_read")

    assert binding is not None
    assert binding.direct_executable is True
    assert binding.display_name == "期权监控"
    assert set(("期权监控", "最新期权报告", "港股期权", "美股期权", "lx 期权", "sy 期权")).issubset(
        set(binding.examples)
    )


def test_option_period_tool_parameters_explain_valid_combinations() -> None:
    descriptions = {
        item["name"]: item
        for item in bot_tools.tool_descriptions(("option_performance_report",))
    }
    report_properties = descriptions["option_performance_report"]["input_schema"]["properties"]

    assert "month requires month" in report_properties["period"]["description"]
    assert "only when period is mtd or ytd" in report_properties["as_of_date"]["description"]


def test_eval_model_turn_skips_implicit_assistant_toolset_loading(monkeypatch) -> None:
    prepared = prepare_contract(_request("检查入口", environment="eval"), reference_year=2026)
    assert not isinstance(prepared, AppResult)
    captured: dict[str, object] = {}

    def unexpected_load(**_kwargs):
        raise AssertionError("implicit Assistant config must not be read")

    def fake_run(_prepared, **kwargs):
        captured.update(kwargs)
        return AppResult(status="answered", user_response="Pi runtime ready.")

    monkeypatch.setattr(local_harness, "load_assistant_bot_settings", unexpected_load)
    monkeypatch.setattr(local_harness, "run_contract", fake_run)

    result = _run_prepared(prepared)

    assert result.status == "answered"
    assert "enabled_optional_toolsets" not in captured


def test_ordinary_run_still_rejects_invalid_implicit_assistant_toolsets(monkeypatch) -> None:
    calls = 0

    def invalid_load(**_kwargs):
        nonlocal calls
        calls += 1
        return None, "eager", "invalid_assistant_config"

    monkeypatch.setattr(local_harness, "load_assistant_bot_settings", invalid_load)
    result = _run_prepared(
        _contract("检查入口"), model_turn_json=None, model_config_json=_model_config()
    )

    assert calls == 1
    assert result.error == {"code": "MODEL_CONFIG_ERROR", "reason": "invalid_assistant_config"}


def test_eval_model_turn_with_explicit_assistant_config_fails_closed(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.assistant.json"
    calls: list[str | None] = []

    def valid_load(*, config_path, require_config):
        calls.append(config_path)
        assert require_config is True
        return frozenset(), "eager", None

    monkeypatch.setattr(local_harness, "load_assistant_bot_settings", valid_load)
    prepared = prepare_contract(_request("检查入口", environment="eval"), reference_year=2026)
    assert not isinstance(prepared, AppResult)

    result = _run_prepared(prepared, assistant_config_path=str(config_path))

    assert calls == [str(config_path)]
    assert result.error == {
        "code": "MODEL_CONFIG_ERROR",
        "reason": "model_turn_conflicts_with_model_config",
    }


def test_eval_model_turn_with_model_config_still_fails_closed(monkeypatch) -> None:
    def unexpected_load(**_kwargs):
        raise AssertionError("implicit Assistant config must not be read")

    monkeypatch.setattr(local_harness, "load_assistant_bot_settings", unexpected_load)
    prepared = prepare_contract(_request("检查入口", environment="eval"), reference_year=2026)
    assert not isinstance(prepared, AppResult)

    result = _run_prepared(prepared, model_config_json=_model_config())

    assert result.error == {
        "code": "MODEL_CONFIG_ERROR",
        "reason": "model_turn_conflicts_with_model_config",
    }


def test_every_catalog_preview_capability_uses_the_generic_control_handoff() -> None:
    preview_specs = preview_operation_capabilities()
    definition = control_preview_tool_description(preview_specs)

    assert set(definition["input_schema"]["properties"]["intent_name"]["enum"]) == {
        str(spec["intent_name"])
        for spec in preview_specs
    }
    for spec in preview_specs:
        arguments = {str(name): "test" for name in spec.get("arguments") or ()}
        request, error = build_control_preview_request(
            {"intent_name": spec["intent_name"], "arguments": arguments},
            user_message="测试写操作预览",
            specs=preview_specs,
        )

        assert error is None
        assert request is not None
        assert request["intent_name"] == spec["intent_name"]
        assert request["source"] == "bot_control_preview"


@pytest.mark.parametrize("intent_name", ["upgrade_confirm", "manual_trade_confirm", "symbol_cancel"])
def test_channel_control_preview_rejects_confirm_and_cancel_intents(intent_name: str) -> None:
    request, error = build_control_preview_request(
        {"intent_name": intent_name, "arguments": {}},
        user_message="确认执行",
        specs=preview_operation_capabilities(),
    )

    assert request is None
    assert error


def test_host_preserves_conversation_context() -> None:
    context = (
        {"role": "user", "content": "分析7月收益"},
        {"role": "assistant", "content": "7月收益主要来自权利金。"},
    )
    prepared = prepare_contract(_request("结论呢", context=context), reference_year=2026)
    assert not isinstance(prepared, AppResult)
    manifest = build_scene_manifest(prepared, "run_context")
    assert manifest.messages[-3:] == [*context, {"role": "user", "content": "结论呢"}]


def test_channel_injects_only_current_authoritative_pending_snapshot(monkeypatch, tmp_path, example_config_path) -> None:
    result, messages = _channel_request(monkeypatch, tmp_path, example_config_path)

    assert result.status == "answered"
    assert isinstance(messages, list)
    assert len(messages) == 2
    assert messages[-2]["role"] == "system"
    assert "Authoritative pending Control operations" in messages[-2]["content"]
    assert '"operation_id": "in_upgrade"' in messages[-2]["content"]
    assert messages[-1] == {"role": "user", "content": "改成 1.2.400"}


def test_channel_injects_empty_pending_snapshot_to_override_stale_history(monkeypatch, tmp_path, example_config_path) -> None:
    result, messages = _channel_request(
        monkeypatch, tmp_path, example_config_path,
        "结论：当前没有待确认操作。",
        user_message="刚才那个还在吗",
        conversation_id="conversation-empty",
        control_context=(),
    )

    assert result.status == "answered"
    assert isinstance(messages, list)
    assert len(messages) == 2
    assert "pending_operations=[]" in messages[-2]["content"]


def test_host_store_session_run_lease_is_cross_instance(tmp_path) -> None:
    path = tmp_path / "bot.db"
    first = BotHostStore(path)
    second = BotHostStore(path)

    assert first.acquire_session_run("wechat:1", "run_1", ttl_seconds=60) is True
    assert second.acquire_session_run("wechat:1", "run_2", ttl_seconds=60) is False
    first.release_session_run("wechat:1", "run_1")
    assert second.acquire_session_run("wechat:1", "run_2", ttl_seconds=60) is True


def test_result_admission_does_not_use_keyword_answer_guard() -> None:
    from src.application.bot.result_admission import admit_result

    result = admit_result(AppResult(status="answered", user_response="已修改配置并已发送通知。"))
    assert result.status == "answered"
    assert result.error is None


def test_result_admission_rejects_unparsed_tool_protocol() -> None:
    from src.application.bot.result_admission import admit_result

    result = admit_result(
        AppResult(
            status="answered",
            user_response='<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="runtime_status">',
        )
    )

    assert result.status == "failed"
    assert result.error == {"code": "RESULT_REJECTED", "reason": "unparsed_tool_protocol"}


def test_empty_request_needs_clarification() -> None:
    result = prepare_contract(_request("  "), reference_year=2026)
    assert isinstance(result, AppResult)
    assert result.status == "needs_clarification"
    assert result.user_response
