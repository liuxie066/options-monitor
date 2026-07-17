from __future__ import annotations

from dataclasses import replace
import json

import pytest

from src.application.copilot import tools as copilot_tools
from src.application.copilot.agent import ModelRequest, ModelTurn, ToolCall
from src.application.copilot.contracts import AppResult, CopilotRequest, CopilotScope, SceneManifest, new_id
from src.application.copilot.control_handoff import (
    CONTROL_PREVIEW_TOOL,
    build_control_preview_request,
    control_preview_tool_description,
)
from src.application.assistant.capability_catalog import preview_operation_capabilities
from src.application.copilot.host import record_session_turn, run_contract, session_messages
from src.application.copilot.host_store import CopilotHostStore
from src.application.copilot import channel_facade
from src.application.copilot.model_client import CopilotModelSettings, build_model_runner
from src.infrastructure.openai_chat_completions import create_chat_completion
from src.application.copilot.scene import GENERAL_SCENE, build_scene_manifest, load_general_scene
from src.application.copilot.service import prepare_contract


def _request(text: str, *, context=(), environment: str = "local") -> CopilotRequest:
    return CopilotRequest(
        request_id=new_id("test_req"),
        source_entry="test",
        user_message=text,
        explicit_scope=CopilotScope(config_key="us"),
        context_messages=tuple(context),
        execution_environment=environment,
    )


def _contract(text: str = "最近有哪些值得关注的问题？"):
    prepared = prepare_contract(_request(text), reference_year=2026)
    assert not isinstance(prepared, AppResult)
    return prepared


def _call(name: str, arguments: dict, call_id: str = "call_1") -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments=arguments)


def test_service_is_thin_and_uses_one_general_scene() -> None:
    for text in ("7月收益", "结论呢", "最近有哪些值得关注的问题？", "分析平仓操作是否合理"):
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
        explicit_scope=CopilotScope(config_key="US", symbol="nvda", month="2026-07"),
    )
    prepared = prepare_contract(request, reference_year=2026)
    assert not isinstance(prepared, AppResult)

    assert prepared.input["config_key"] == "us"
    assert prepared.input["symbol"] == "NVDA"
    assert prepared.input["month"] == "2026-07"


def test_scene_manifest_owns_prompt_tools_and_runtime_limits() -> None:
    definition = load_general_scene()
    manifest = build_scene_manifest(_contract(), "run_test")

    assert definition["scene"] == GENERAL_SCENE
    assert manifest.messages[0]["role"] == "system"
    assert manifest.messages[0]["content"] == definition["system_prompt"]
    assert "reference_year: 2026" in manifest.messages[1]["content"]
    assert definition["prompt_fragments"] == [
        "prompts/base_behavior.md",
        "prompts/financial_fact_rules.md",
        "prompts/tool_rules.md",
        "prompts/om_chat.md",
    ]
    assert "Make the first non-empty line `结论：...`" in definition["system_prompt"]
    assert "supported judgments with pros/cons/actions" in definition["system_prompt"]
    assert "no summary/row dumps" in definition["system_prompt"]
    assert "pre-fee `realized_pnl_*` is primary" in definition["system_prompt"]
    assert "Assignment principal is asset conversion" in definition["system_prompt"]
    assert "Moneyness requires an observed underlying price" in definition["system_prompt"]
    assert "Treat non-empty runtime context fields as fixed scope" in definition["system_prompt"]
    assert "do not print tool-call syntax as text" in definition["system_prompt"]
    assert "Preserve account, market, symbol, currency, period, unit, and source" in definition["system_prompt"]
    assert "Keep recommendations temporally possible" in definition["system_prompt"]
    assert "A local ledger state warning proves a local consistency problem only" in definition["system_prompt"]
    assert "Treat an explicit `not_observed` evidence scope as a hard claim boundary" in definition["system_prompt"]
    assert "portfolio-management" in definition["system_prompt"]
    assert "Tool success results are flat JSON business data" in definition["system_prompt"]
    assert "A short follow-up such as" in definition["system_prompt"]
    assert "read-first options-monitor assistant" in definition["system_prompt"]
    assert "request a deterministic Control preview" in definition["system_prompt"]
    assert "never confirm, apply, or cancel" in definition["system_prompt"]
    assert "analysis_query" in manifest.allowed_tools
    assert "runtime_status" in manifest.allowed_tools
    assert "portfolio_query" not in manifest.allowed_tools
    assert "portfolio_capital_bridge" not in manifest.allowed_tools
    assert definition["tool_selection"]["optional_names"] == ["portfolio"]
    assert "symbol_config_update" not in manifest.allowed_tools
    assert manifest.limits["max_model_turns"] == definition["runtime"]["max_iterations"]


def test_scene_selects_canonical_read_only_toolsets() -> None:
    from src.application.agent_tool_registry import pure_read_toolsets

    definition = load_general_scene()
    optional = set(definition["tool_selection"]["optional_names"])
    selected = tuple(name for name in definition["tool_selection"]["names"] if name not in optional)
    manifest = build_scene_manifest(_contract(), "run_toolsets")
    expected = {
        name
        for toolset in selected
        for name in pure_read_toolsets()[toolset]
    }

    assert set(manifest.allowed_tools) == expected
    assert "portfolio" not in selected
    assert "portfolio_query" not in expected
    assert "portfolio_capital_bridge" not in expected
    assert "symbol_config_update" not in expected

    enabled_manifest = build_scene_manifest(
        _contract(),
        "run_toolsets_enabled",
        enabled_optional_toolsets=frozenset({"portfolio"}),
    )
    assert "portfolio_query" in enabled_manifest.allowed_tools
    assert "portfolio_capital_bridge" in enabled_manifest.allowed_tools


def test_agent_tool_view_exposes_result_contract() -> None:
    view = next(item for item in copilot_tools.tool_descriptions(("monthly_income_report",)))

    assert view["output_contract"]["primary_rows"] == "return_summary"
    assert "Key result fields:" in view["description"]
    observation = copilot_tools.compact_observation(
        "monthly_income_report",
        {"ok": True, "data": {"return_summary": [{"month": "2026-07"}], "row_count": 1}},
        {"month": "2026-07"},
    )
    assert "rows=1" in observation["summary"]
    assert observation["result_contract"]["source_label"] == "OM 本地账本"
    warning_observation = copilot_tools.compact_observation(
        "monthly_income_report",
        {
            "ok": True,
            "data": {"summary": [], "row_count": 0},
            "warnings": ["no source rows", ""],
        },
    )
    assert warning_observation["warnings"] == ["no source rows"]
    nested = copilot_tools.compact_observation(
        "monthly_income_report",
        {
            "ok": True,
            "data": {
                "diagnostics": [
                    {
                        "month_range": {"month": "2026-07"},
                        "missing_fields": ["trade_events"],
                    }
                ]
            },
        },
    )
    assert nested["value"]["diagnostics"][0]["month_range"]["month"] == "2026-07"
    assert nested["value"]["diagnostics"][0]["missing_fields"] == ["trade_events"]


def test_agent_tool_view_hides_paths_and_exposes_defaults() -> None:
    from src.application.agent_tool_registry import get_tool_definition

    candidate = next(item for item in copilot_tools.tool_descriptions(("candidate_rank_explain",)))
    properties = candidate["input_schema"]["properties"]

    assert properties["mode"]["default"] == "all"
    assert properties["top_n"]["default"] == 10
    assert "candidate_path" not in properties
    assert "report_dir" not in properties

    positions = next(item for item in copilot_tools.tool_descriptions(("option_positions_read",)))
    assert positions["input_schema"]["properties"]["action"]["type"] == "string"
    assert positions["input_schema"]["properties"]["status"]["type"] == "string"
    assert "quote_snapshots" not in positions["input_schema"]["properties"]
    assert "opend_host" not in positions["input_schema"]["properties"]

    external_positions = get_tool_definition("option_positions_read")
    assert external_positions is not None
    assert external_positions.input_json_schema()["properties"]["action"]["type"] == ["string", "array"]
    assert "quote_snapshots" in external_positions.input_json_schema()["properties"]
    assert "opend_host" in external_positions.input_json_schema()["properties"]


def test_symbol_inputs_are_structurally_required_without_fake_defaults() -> None:
    from src.application.agent_tool_registry import get_tool_definition

    for tool_name in ("symbol_resolve", "symbol_config_read"):
        definition = get_tool_definition(tool_name)
        assert definition is not None
        assert "symbol" in definition.input_json_schema()["required"]
        assert "symbol" not in definition.safe_default_input


def test_observation_projection_prioritizes_contract_facts_and_missing_boundaries() -> None:
    filler = {f"metadata_{index}": index for index in range(25)}
    response = {
        "ok": True,
        "data": {
            **filler,
            "summary": [{"metadata": "ignored", "month": "2026-07", "account": "lx"}],
            "diagnostics": [{"income_amount_status": "available", "missing_fields": []}],
            "row_count": 1,
        },
    }

    observation = copilot_tools.compact_observation("monthly_income_report", response)

    assert observation["status"] == "complete"
    assert observation["value"]["summary"][0]["month"] == "2026-07"
    assert "missing_data" not in observation

    response["data"]["diagnostics"] = [
        {"income_amount_status": "unavailable", "missing_fields": ["trade_events"]}
    ]
    partial = copilot_tools.compact_observation("monthly_income_report", response)
    assert partial["status"] == "partial"
    assert partial["missing_data"]["diagnostics[].missing_fields"] == ["trade_events"]


def test_observation_projection_preserves_source_scope_and_coverage() -> None:
    response = {
        "ok": True,
        "data": {
            "rows": [{"symbol": "NVDA"}],
            "row_count": 1,
            "source": {"label": "ledger", "as_of": "2026-07-11T10:00:00Z"},
            "scope": {"account": "lx", "market": "us"},
            "coverage": {"broker_settlement": "not_observed"},
        },
    }

    observation = copilot_tools.compact_observation("analysis_query", response)

    assert observation["source"]["label"] == "ledger"
    assert observation["scope"] == {"account": "lx", "market": "us"}
    assert observation["coverage"] == {"broker_settlement": "not_observed"}


def test_error_observation_is_structured_and_bounded() -> None:
    observation = copilot_tools.compact_observation(
        "analysis_query",
        {
            "ok": False,
            "error": {
                "code": "INPUT_ERROR",
                "message": "unknown column pnl",
                "hint": "Inspect analysis_catalog and retry.",
                "field": "sql",
                "details": {
                    "unknown_views": ["secret_view"],
                    "schema_errors": [{"path": "$.sql", "expected": "string", "actual": "array"}],
                    "config_path": "/secret/config.json",
                },
            },
        },
    )

    assert observation["status"] == "failed"
    assert observation["error"] == "INPUT_ERROR"
    assert observation["code"] == "INPUT_ERROR"
    assert observation["retryable"] is True
    assert observation["field"] == "sql"
    assert "config_path" not in observation["details"]
    assert observation["details"]["schema_errors"][0]["path"] == "$.sql"


def test_analysis_query_has_no_fake_default_query() -> None:
    from src.application.agent_tool_registry import get_tool_definition

    definition = get_tool_definition("analysis_query")
    assert definition is not None
    assert definition.safe_default_input == {}


def test_model_may_answer_directly_without_tools() -> None:
    requests: list[ModelRequest] = []

    def model(request: ModelRequest) -> ModelTurn:
        requests.append(request)
        return ModelTurn(text="OM 用于只读监控和分析期权运行数据。")

    result = run_contract(_contract("OM 是做什么的？"), model_runner=model)

    assert result.status == "answered"
    assert result.user_response == "OM 用于只读监控和分析期权运行数据。"
    assert result.error is None
    assert len(requests) == 1
    assert requests[0].messages[-1]["content"] == "OM 是做什么的？"


def test_tool_result_is_returned_as_standard_tool_message(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    requests: list[ModelRequest] = []

    def fake_call(name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        calls.append((name, dict(payload)))
        return {"ok": True, "data": {"status": "healthy", "latest_run": "run_1"}}

    def model(request: ModelRequest) -> ModelTurn:
        requests.append(request)
        if not any(item.get("role") == "tool" for item in request.messages):
            return ModelTurn(tool_calls=(_call("runtime_status", {"config_key": "us"}),))
        return ModelTurn(text="运行状态正常，最近一次运行是 run_1。")

    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)
    result = run_contract(_contract("运行健康度怎么样"), model_runner=model)

    assert result.user_response == "运行状态正常，最近一次运行是 run_1。"
    assert calls == [("runtime_status", {"config_key": "us"})]
    tool_message = next(item for item in requests[1].messages if item.get("role") == "tool")
    assert tool_message["tool_call_id"] == "call_1"
    projected = json.loads(tool_message["content"])
    assert projected["status"] == "healthy"
    assert "ok" not in projected
    assert "value" not in projected


def test_disabled_portfolio_toolset_blocks_model_attempt_before_tool_execution(monkeypatch) -> None:
    calls: list[str] = []
    turns = iter(
        (
            ModelTurn(tool_calls=(_call("portfolio_query", {"view": "health"}),)),
            ModelTurn(text="portfolio 工具未开放。"),
        )
    )

    def fake_call(name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        calls.append(name)
        return {"ok": True, "data": {"status": "healthy"}}

    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)
    result = run_contract(_contract("查询 portfolio"), model_runner=lambda _request: next(turns))

    assert result.status == "answered"
    assert calls == []
    assert any(
        event.type == "tool_result"
        and event.payload.get("error") == "POLICY_ERROR"
        for event in result.events
    )


def test_enabled_portfolio_toolset_reaches_tool_execution(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    turns = iter(
        (
            ModelTurn(tool_calls=(_call("portfolio_query", {"view": "health"}),)),
            ModelTurn(text="portfolio 服务正常。"),
        )
    )

    def fake_call(name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        calls.append((name, dict(payload)))
        return {"ok": True, "data": {"status": "healthy"}}

    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)
    result = run_contract(
        _contract("查询 portfolio"),
        model_runner=lambda _request: next(turns),
        enabled_optional_toolsets=frozenset({"portfolio"}),
    )

    assert result.status == "answered"
    assert calls == [("portfolio_query", {"view": "health"})]


def test_prepared_contract_reloads_current_portfolio_toolset_on_resume(monkeypatch, tmp_path) -> None:
    from src.application.copilot import local_harness

    config_path = tmp_path / "config.assistant.json"
    calls: list[str] = []

    def write_config(portfolio_enabled: bool) -> None:
        config_path.write_text(
            json.dumps(
                {
                    "assistant": {
                        "enabled": True,
                        "copilot": {
                            "enabled": True,
                            "toolsets": {"portfolio": portfolio_enabled},
                        },
                        "llm": {},
                    }
                }
            ),
            encoding="utf-8",
        )

    def model(request: ModelRequest) -> ModelTurn:
        if any(item.get("role") == "tool" for item in request.messages):
            return ModelTurn(text="portfolio 检查完成。")
        return ModelTurn(tool_calls=(_call("portfolio_query", {"view": "health"}),))

    def fake_call(name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        calls.append(name)
        return {"ok": True, "data": {"status": "healthy"}}

    monkeypatch.setattr(local_harness, "_resolve_model_runner", lambda **_kwargs: (model, None))
    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)
    prepared = _contract("查询 portfolio")

    write_config(True)
    first = local_harness.run_prepared_contract(prepared, assistant_config_path=str(config_path))
    write_config(False)
    resumed = local_harness.run_prepared_contract(
        prepared,
        assistant_config_path=str(config_path),
        resumed_from=first.run_id,
    )

    assert first.status == "answered"
    assert resumed.status == "answered"
    assert calls == ["portfolio_query"]


def test_model_arguments_are_not_dropped_by_tool_wrapper(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_call(name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        calls.append(dict(payload))
        return {"ok": True, "data": {"rows": []}}

    turns = iter(
        (
            ModelTurn(
                tool_calls=(
                    _call(
                        "analysis_query",
                        {"views": ["account_monthly_performance"], "month": "2026-07", "account": "lx"},
                    ),
                )
            ),
            ModelTurn(text="7月暂无可用收益行。"),
        )
    )
    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)

    result = run_contract(_contract("7月收益"), model_runner=lambda _request: next(turns))

    assert result.user_response == "7月暂无可用收益行。"
    assert calls[0]["views"] == ["account_monthly_performance"]
    assert calls[0]["month"] == "2026-07"
    assert calls[0]["account"] == "lx"


def test_explicit_scope_cannot_be_overridden_by_model_tool_arguments(monkeypatch) -> None:
    calls: list[dict] = []
    turns = iter(
        (
            ModelTurn(tool_calls=(_call("monthly_income_report", {"config_key": "hk", "month": "2026-07"}),)),
            ModelTurn(text="结论：已按明确的 us 范围查询。"),
        )
    )

    def fake_call(name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        calls.append(dict(payload))
        return {"ok": True, "data": {"summary": []}}

    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)
    result = run_contract(_contract("7月收益"), model_runner=lambda _request: next(turns))

    assert result.status == "answered"
    assert calls[0]["config_key"] == "us"


def test_budget_exhaustion_returns_tool_results_for_entire_native_batch(monkeypatch) -> None:
    contract = _contract("同时检查运行状态和收益")
    manifest = build_scene_manifest(contract, "run_budget_batch")
    manifest = replace(manifest, limits={**manifest.limits, "max_tool_calls": 1})
    requests: list[ModelRequest] = []

    def model(request: ModelRequest) -> ModelTurn:
        requests.append(request)
        if not any(item.get("role") == "tool" for item in request.messages):
            return ModelTurn(
                tool_calls=(
                    _call("runtime_status", {"config_key": "us"}, "batch_1"),
                    _call("monthly_income_report", {"month": "2026-07"}, "batch_2"),
                )
            )
        return ModelTurn(text="只完成了运行状态检查，收益工具因预算限制未执行。")

    monkeypatch.setattr(
        copilot_tools,
        "call_read_tool",
        lambda name, payload, *, allowed_tools: {"ok": True, "data": {"status": "healthy"}},
    )
    from src.application.copilot.engine import run_engine

    result = run_engine(
        manifest,
        scene_input=contract.input,
        record_event=lambda *_args: None,
        build_tool_payload=lambda name, payload: copilot_tools.build_tool_payload(name, payload),
        call_read_tool=lambda name, payload: copilot_tools.call_read_tool(
            name, payload, allowed_tools=tuple(manifest.allowed_tools)
        ),
        compact_observation=copilot_tools.compact_observation,
        fixture_observations=lambda _fixture: [],
        model_runner=model,
    )

    tool_messages = [item for item in requests[-1].messages if item.get("role") == "tool"]
    assert [item["tool_call_id"] for item in tool_messages] == ["batch_1", "batch_2"]
    assert json.loads(tool_messages[-1]["content"])["error"] == "BUDGET_EXHAUSTED"
    assert result.status == "answered"


def test_context_compaction_keeps_native_tool_call_pairs() -> None:
    contract = _contract("总结上下文")
    manifest = build_scene_manifest(contract, "run_context_budget")
    manifest = replace(manifest, limits={**manifest.limits, "max_context_chars": 8_000})
    requests: list[ModelRequest] = []
    turns = iter(
        (
            ModelTurn(tool_calls=(_call("runtime_status", {"config_key": "us"}, "context_1"),)),
            ModelTurn(text="结论：运行状态正常。"),
        )
    )

    def model(request: ModelRequest) -> ModelTurn:
        requests.append(request)
        return next(turns)

    from src.application.copilot.engine import run_engine

    run_engine(
        manifest,
        scene_input=contract.input,
        record_event=lambda *_args: None,
        build_tool_payload=lambda name, payload: copilot_tools.build_tool_payload(name, payload),
        call_read_tool=lambda _name, _payload: {"ok": True, "data": {"blob": "x" * 20_000}},
        compact_observation=copilot_tools.compact_observation,
        fixture_observations=lambda _fixture: [],
        model_runner=model,
    )

    final_messages = list(requests[-1].messages)
    assistant_index = next(index for index, item in enumerate(final_messages) if item.get("tool_calls"))
    assert final_messages[assistant_index + 1]["role"] == "tool"
    assert final_messages[assistant_index + 1]["tool_call_id"] == "context_1"
    assert sum(len(json.dumps(item, ensure_ascii=False)) for item in final_messages) <= 8_000


def test_context_compaction_preserves_authoritative_system_context() -> None:
    contract = _contract("总结上下文")
    manifest = build_scene_manifest(contract, "run_context_authority")
    manifest = replace(
        manifest,
        messages=[
            *manifest.messages[:1],
            {
                "role": "system",
                "content": (
                    "Authoritative pending Control operations for this conversation. "
                    'pending_operations=[{"operation_id":"in_upgrade","status":"previewed"}]'
                ),
            },
            *manifest.messages[1:],
        ],
        limits={**manifest.limits, "max_context_chars": 8_000},
    )
    requests: list[ModelRequest] = []
    turns = iter(
        (
            ModelTurn(tool_calls=(_call("runtime_status", {"config_key": "us"}, "context_authority_1"),)),
            ModelTurn(text="结论：已保留待确认操作上下文。"),
        )
    )

    def model(request: ModelRequest) -> ModelTurn:
        requests.append(request)
        return next(turns)

    from src.application.copilot.engine import run_engine

    result = run_engine(
        manifest,
        scene_input=contract.input,
        record_event=lambda *_args: None,
        build_tool_payload=lambda name, payload: copilot_tools.build_tool_payload(name, payload),
        call_read_tool=lambda _name, _payload: {"ok": True, "data": {"blob": "x" * 20_000}},
        compact_observation=copilot_tools.compact_observation,
        fixture_observations=lambda _fixture: [],
        model_runner=model,
    )

    assert result.status == "answered"
    final_messages = list(requests[-1].messages)
    assert any("in_upgrade" in str(item.get("content") or "") for item in final_messages if item.get("role") == "system")
    assert sum(len(json.dumps(item, ensure_ascii=False)) for item in final_messages) <= 8_000
    assert result.status == "answered"


def test_context_compaction_preserves_financial_identity_fields() -> None:
    contract = _contract("总结收益")
    manifest = build_scene_manifest(contract, "run_context_identity")
    manifest = replace(
        manifest,
        limits={**manifest.limits, "max_context_chars": 8_000, "max_context_tokens": 2_000},
    )
    requests: list[ModelRequest] = []

    def model(request: ModelRequest) -> ModelTurn:
        requests.append(request)
        if not any(item.get("role") == "tool" for item in request.messages):
            return ModelTurn(tool_calls=(_call("monthly_income_report", {"month": "2026-07"}, "identity_1"),))
        return ModelTurn(text="结论：lx 账户 7 月美元收益为正。", finish_reason="stop")

    from src.application.copilot.engine import run_engine

    result = run_engine(
        manifest,
        scene_input=contract.input,
        record_event=lambda *_args: None,
        build_tool_payload=lambda name, payload: copilot_tools.build_tool_payload(name, payload),
        call_read_tool=lambda _name, _payload: {
            "ok": True,
            "data": {
                "account": "lx",
                "currency": "USD",
                "month": "2026-07",
                "source": "ledger",
                "notes": "x" * 20_000,
                "rows": [{"symbol": f"SYM{index}", "premium": index} for index in range(500)],
            },
        },
        compact_observation=copilot_tools.compact_observation,
        fixture_observations=lambda _fixture: [],
        model_runner=model,
    )

    tool_message = next(item for item in requests[-1].messages if item.get("role") == "tool")
    projected = json.loads(tool_message["content"])
    assert projected["account"] == "lx"
    assert projected["currency"] == "USD"
    assert projected["month"] == "2026-07"
    assert projected["source"] == "ledger"
    assert projected["context_compacted"] is True
    assert result.status == "answered"


def test_truncated_model_answer_is_continued_and_joined() -> None:
    turns = iter(
        (
            ModelTurn(text="结论：7月收益", finish_reason="length", usage={"input_tokens": 100}),
            ModelTurn(text="为正，主要来自权利金。", finish_reason="stop", attempt_count=2),
        )
    )

    result = run_contract(_contract("7月收益"), model_runner=lambda _request: next(turns))

    assert result.status == "answered"
    assert result.user_response == "结论：7月收益为正，主要来自权利金。"
    continuation = next(event for event in result.events if event.type == "model_continuation_requested")
    assert continuation.payload["continuation_count"] == 1
    completed = [event for event in result.events if event.type == "model_turn_completed"]
    assert completed[0].payload["finish_reason"] == "length"
    assert completed[-1].payload["model_retry_count"] == 1
    terminated = next(event for event in result.events if event.type == "agent_terminated")
    assert terminated.payload["reason"] == "final_answer"


def test_same_tool_can_retry_with_changed_arguments(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_call(name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        calls.append(dict(payload))
        return {"ok": True, "data": {"rows": []}}

    def model(request: ModelRequest) -> ModelTurn:
        tool_count = sum(item.get("role") == "tool" for item in request.messages)
        if tool_count == 0:
            return ModelTurn(tool_calls=(_call("analysis_query", {"views": ["open_option_exposure"]}, "call_1"),))
        if tool_count == 1:
            return ModelTurn(
                tool_calls=(_call("analysis_query", {"views": ["expiration_risk_buckets"]}, "call_2"),)
            )
        return ModelTurn(text="检查了持仓集中度和到期风险，没有拿到可用行。")

    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)
    result = run_contract(_contract("当前风险集中在哪里"), model_runner=model)

    assert len(calls) == 2
    assert calls[0]["views"] != calls[1]["views"]
    assert "到期风险" in result.user_response


def test_identical_repeated_call_reuses_successful_observation(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_call(name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        calls.append(dict(payload))
        return {"ok": True, "data": {"status": "healthy"}}

    def model(request: ModelRequest) -> ModelTurn:
        tool_messages = [item for item in request.messages if item.get("role") == "tool"]
        if not tool_messages:
            return ModelTurn(tool_calls=(_call("runtime_status", {"config_key": "us"}, "call_1"),))
        if len(tool_messages) == 1:
            return ModelTurn(tool_calls=(_call("runtime_status", {"config_key": "us"}, "call_2"),))
        reused = json.loads(tool_messages[-1]["content"])
        assert reused["status"] == "healthy"
        assert reused["reused"] is True
        assert reused["reused_from_ref"] == "obs_1"
        return ModelTurn(text="重复检查没有必要，已有状态显示运行正常。")

    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)
    result = run_contract(_contract("检查运行状态"), model_runner=model)

    assert len(calls) == 1
    assert result.status == "answered"
    assert any(event.type == "tool_result_reused" for event in result.events)


def test_identical_call_can_retry_once_after_transient_tool_error(monkeypatch) -> None:
    calls = 0

    def fake_call(name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"ok": False, "error": {"code": "TOOL_EXCEPTION", "message": "temporary read failure"}}
        return {"ok": True, "data": {"status": "healthy"}}

    def model(request: ModelRequest) -> ModelTurn:
        tool_messages = [item for item in request.messages if item.get("role") == "tool"]
        if len(tool_messages) < 2:
            return ModelTurn(
                tool_calls=(_call("runtime_status", {"config_key": "us"}, f"transient_{len(tool_messages) + 1}"),)
            )
        return ModelTurn(text="结论：重试后确认运行正常。", finish_reason="stop")

    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)
    result = run_contract(_contract("检查运行状态"), model_runner=model)

    assert calls == 2
    assert result.status == "answered"
    assert "运行正常" in result.user_response


def test_tool_failure_is_recoverable(monkeypatch) -> None:
    calls: list[str] = []

    def fake_call(name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        calls.append(name)
        if name == "analysis_query":
            return {"ok": False, "error": {"code": "INPUT_ERROR", "message": "view is invalid"}}
        return {"ok": True, "data": {"status": "healthy"}}

    def model(request: ModelRequest) -> ModelTurn:
        count = sum(item.get("role") == "tool" for item in request.messages)
        if count == 0:
            return ModelTurn(tool_calls=(_call("analysis_query", {"views": ["bad"]}, "call_1"),))
        if count == 1:
            return ModelTurn(tool_calls=(_call("runtime_status", {"config_key": "us"}, "call_2"),))
        return ModelTurn(text="分析视图参数不可用，但运行状态正常。")

    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)
    result = run_contract(_contract("检查系统"), model_runner=model)

    assert calls == ["analysis_query", "runtime_status"]
    assert result.status == "answered"


def test_tool_system_exit_is_recoverable(monkeypatch) -> None:
    def fake_call(name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        raise SystemExit("invalid runtime config")

    def model(request: ModelRequest) -> ModelTurn:
        tool_messages = [item for item in request.messages if item.get("role") == "tool"]
        if not tool_messages:
            return ModelTurn(tool_calls=(_call("runtime_status", {"config_key": "us"}),))
        observation = json.loads(tool_messages[-1]["content"])
        assert observation["error"] == "CONFIG_ERROR"
        return ModelTurn(text="运行配置不可用，当前无法完成检查。")

    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)
    result = run_contract(_contract("检查系统"), model_runner=model)

    assert result.status == "answered"
    assert "配置不可用" in result.user_response


def test_budget_exhaustion_forces_final_text_without_tools(monkeypatch) -> None:
    contract = replace(_contract("7月收益"), policy={"read_only": True})
    requests: list[ModelRequest] = []

    def fake_manifest(_contract, run_id):
        return SceneManifest(
            run_id=run_id,
            scene_name=GENERAL_SCENE,
            execution_environment="local",
            messages=[{"role": "user", "content": "7月收益"}],
            allowed_tools=["runtime_status"],
            limits={"max_model_turns": 1, "max_tool_calls": 1, "timeout_seconds": 180},
            output_schema={"type": "text"},
        )

    def model(request: ModelRequest) -> ModelTurn:
        requests.append(request)
        if request.force_finish:
            assert request.tools == ()
            return ModelTurn(text="只拿到了运行状态，缺少收益数据，暂时不能计算7月收益。")
        return ModelTurn(tool_calls=(_call("runtime_status", {"config_key": "us"}),))

    monkeypatch.setattr("src.application.copilot.host.build_scene_manifest", fake_manifest)
    monkeypatch.setattr(
        copilot_tools,
        "call_read_tool",
        lambda name, payload, *, allowed_tools: {"ok": True, "data": {"status": "healthy"}},
    )
    result = run_contract(contract, model_runner=model)

    assert requests[-1].force_finish is True
    assert result.status == "answered"
    assert "缺少收益数据" in result.user_response


def test_model_failure_after_observation_attempts_forced_final_answer(monkeypatch) -> None:
    calls = 0

    def model(request: ModelRequest) -> ModelTurn:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelTurn(tool_calls=(_call("runtime_status", {"config_key": "us"}, "recover_1"),))
        if not request.force_finish:
            raise TimeoutError("temporary provider timeout")
        assert request.tools == ()
        return ModelTurn(text="结论：已取得运行状态，但模型中途超时；现有证据显示运行正常。", finish_reason="stop")

    monkeypatch.setattr(
        copilot_tools,
        "call_read_tool",
        lambda name, payload, *, allowed_tools: {"ok": True, "data": {"status": "healthy"}},
    )
    result = run_contract(_contract("检查运行状态"), model_runner=model)

    assert result.status == "answered"
    assert "模型中途超时" in result.user_response
    assert any(event.type == "model_error" for event in result.events)
    terminated = next(event for event in result.events if event.type == "agent_terminated")
    assert terminated.payload["reason"] == "forced_final_answer"


def test_non_read_tool_call_is_rejected_without_execution(monkeypatch) -> None:
    executed = False

    def fake_call(name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        nonlocal executed
        executed = True
        return {"ok": True, "data": {}}

    def model(request: ModelRequest) -> ModelTurn:
        tool_messages = [item for item in request.messages if item.get("role") == "tool"]
        if not tool_messages:
            return ModelTurn(tool_calls=(_call("symbol_config_update", {"symbol": "NVDA"}),))
        assert json.loads(tool_messages[-1]["content"])["error"] == "POLICY_ERROR"
        return ModelTurn(text="这个自由问答环境只能读取，不能修改配置。")

    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)
    result = run_contract(_contract("把 NVDA 加入配置"), model_runner=model)

    assert executed is False
    assert result.status == "answered"


def test_channel_manifest_exposes_catalog_driven_control_preview_only() -> None:
    captured: list[set[str]] = []
    preview_specs = preview_operation_capabilities()

    def model(request: ModelRequest) -> ModelTurn:
        captured.append({str(item.get("name") or "") for item in request.tools})
        return ModelTurn(text="结论：无需执行写操作。")

    local_result = run_contract(_contract("检查运行状态"), model_runner=model)
    channel_prepared = prepare_contract(_request("升级到最新版", environment="channel"), reference_year=2026)
    assert not isinstance(channel_prepared, AppResult)
    channel_result = run_contract(channel_prepared, model_runner=model, control_preview_specs=preview_specs)

    assert local_result.status == "answered"
    assert channel_result.status == "answered"
    assert CONTROL_PREVIEW_TOOL not in captured[0]
    assert CONTROL_PREVIEW_TOOL in captured[1]
    assert {spec["intent_name"] for spec in preview_specs}
    assert all(spec["risk_level"] in {"preview_write", "preview_admin"} for spec in preview_specs)
    assert all(spec["operation_action"] not in {"confirm", "cancel"} for spec in preview_specs)


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
        assert request["source"] == "copilot_control_preview"


def test_channel_control_preview_returns_structured_request_without_execution(monkeypatch) -> None:
    executed = False

    def fake_call(name: str, payload: dict, *, allowed_tools: tuple[str, ...]) -> dict:
        nonlocal executed
        executed = True
        return {"ok": True, "data": {}}

    def model(_request: ModelRequest) -> ModelTurn:
        return ModelTurn(
            tool_calls=(
                _call(
                    CONTROL_PREVIEW_TOOL,
                    {"intent_name": "upgrade_now", "arguments": {"target_version": "1.2.400"}},
                ),
            )
        )

    monkeypatch.setattr(copilot_tools, "call_read_tool", fake_call)
    prepared = prepare_contract(_request("升级到 1.2.400", environment="channel"), reference_year=2026)
    assert not isinstance(prepared, AppResult)
    result = run_contract(prepared, model_runner=model, control_preview_specs=preview_operation_capabilities())

    assert result.status == "control_requested"
    assert result.control_request == {
        "intent_name": "upgrade_now",
        "arguments": {"target_version": "1.2.400"},
        "source": "copilot_control_preview",
        "confidence": 1.0,
    }
    assert executed is False


@pytest.mark.parametrize("intent_name", ["upgrade_confirm", "manual_trade_confirm", "symbol_cancel"])
def test_channel_control_preview_rejects_confirm_and_cancel_intents(intent_name: str) -> None:
    calls = 0

    def model(request: ModelRequest) -> ModelTurn:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelTurn(
                tool_calls=(
                    _call(CONTROL_PREVIEW_TOOL, {"intent_name": intent_name, "arguments": {}}),
                )
            )
        error = json.loads(next(item for item in request.messages if item.get("role") == "tool")["content"])
        assert error["error"] == "INVALID_ACTION"
        return ModelTurn(text="结论：确认或取消必须由确定性权限流程处理。")

    prepared = prepare_contract(_request("确认执行", environment="channel"), reference_year=2026)
    assert not isinstance(prepared, AppResult)
    result = run_contract(prepared, model_runner=model, control_preview_specs=preview_operation_capabilities())

    assert result.status == "answered"
    assert result.control_request is None


def test_channel_control_preview_cannot_be_mixed_with_read_tools() -> None:
    calls = 0

    def model(request: ModelRequest) -> ModelTurn:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelTurn(
                tool_calls=(
                    _call(CONTROL_PREVIEW_TOOL, {"intent_name": "upgrade_now", "arguments": {}}, "control_1"),
                    _call("runtime_status", {"config_key": "us"}, "read_1"),
                )
            )
        tool_messages = [item for item in request.messages if item.get("role") == "tool"]
        assert len(tool_messages) == 1
        assert json.loads(tool_messages[0]["content"])["error"] == "INVALID_ACTION"
        return ModelTurn(text="结论：写操作预览需要单独请求。")

    prepared = prepare_contract(_request("检查状态并升级", environment="channel"), reference_year=2026)
    assert not isinstance(prepared, AppResult)
    result = run_contract(prepared, model_runner=model, control_preview_specs=preview_operation_capabilities())

    assert result.status == "answered"
    assert result.control_request is None


def test_host_preserves_conversation_context() -> None:
    context = (
        {"role": "user", "content": "分析7月收益"},
        {"role": "assistant", "content": "7月收益主要来自权利金。"},
    )
    prepared = prepare_contract(_request("结论呢", context=context), reference_year=2026)
    assert not isinstance(prepared, AppResult)
    manifest = build_scene_manifest(prepared, "run_context")
    assert manifest.messages[-3:] == [*context, {"role": "user", "content": "结论呢"}]


def test_channel_injects_authoritative_pending_snapshot_after_history(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(channel_facade, "_channel_model_gate", lambda _path: None)

    def fake_run(prepared, **_kwargs):  # type: ignore[no-untyped-def]
        captured["messages"] = prepared.input["messages"]
        return AppResult(status="answered", user_response="结论：请明确要修改哪条预览。")

    monkeypatch.setattr(channel_facade, "run_prepared_contract", fake_run)
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    store.record_session_turn(
        "wechat:conversation-1",
        "升级到最新版",
        "旧历史：升级预览等待确认。",
        max_messages=10,
    )
    result = channel_facade.run_channel_request(
        user_message="改成 1.2.400",
        config_key="us",
        assistant_config_path=str(tmp_path / "assistant.json"),
        channel="wechat",
        sender_id="ou_1",
        conversation_id="conversation-1",
        host_db_path=str(store.path),
        control_context=(
            {
                "operation_id": "in_upgrade",
                "operation_type": "upgrade_now",
                "status": "previewed",
                "summary": "升级到最新版",
            },
        ),
    )

    assert result.status == "answered"
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[-2]["role"] == "system"
    assert "Authoritative pending Control operations" in messages[-2]["content"]
    assert '"operation_id": "in_upgrade"' in messages[-2]["content"]
    assert messages[-1] == {"role": "user", "content": "改成 1.2.400"}


def test_channel_injects_empty_pending_snapshot_to_override_stale_history(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(channel_facade, "_channel_model_gate", lambda _path: None)

    def fake_run(prepared, **_kwargs):  # type: ignore[no-untyped-def]
        captured["messages"] = prepared.input["messages"]
        return AppResult(status="answered", user_response="结论：当前没有待确认操作。")

    monkeypatch.setattr(channel_facade, "run_prepared_contract", fake_run)
    result = channel_facade.run_channel_request(
        user_message="刚才那个还在吗",
        config_key="us",
        assistant_config_path=str(tmp_path / "assistant.json"),
        channel="wechat",
        sender_id="ou_1",
        conversation_id="conversation-empty",
        host_db_path=str(tmp_path / "copilot.sqlite3"),
        control_context=(),
    )

    assert result.status == "answered"
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert "pending_operations=[]" in messages[-2]["content"]


def test_session_store_keeps_bounded_recent_messages() -> None:
    key = f"test:{new_id('session')}"
    for index in range(15):
        record_session_turn(key, f"u{index}", f"a{index}")
    messages = session_messages(key)
    assert len(messages) == load_general_scene()["conversation"]["max_messages"]
    assert messages[-1] == {"role": "assistant", "content": "a14"}


def test_host_store_persists_sessions_and_run_events(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.db")
    key = "wechat:conversation-1"
    record_session_turn(key, "7月收益", "收益为正。", host_store=store)

    reopened = CopilotHostStore(tmp_path / "copilot.db")
    assert session_messages(key, host_store=reopened)[-1]["content"] == "收益为正。"

    result = run_contract(
        _contract("运行状态"),
        model_runner=lambda _request: ModelTurn(text="结论：运行正常。"),
        host_store=reopened,
        session_key=key,
    )
    record = reopened.run_record(result.run_id)
    assert record is not None
    assert record["status"] == "answered"
    events = json.loads(record["events_json"])
    assert events[-1]["type"] == "final_result"


def test_host_store_session_run_lease_is_cross_instance(tmp_path) -> None:
    path = tmp_path / "copilot.db"
    first = CopilotHostStore(path)
    second = CopilotHostStore(path)

    assert first.acquire_session_run("wechat:1", "run_1", ttl_seconds=60) is True
    assert second.acquire_session_run("wechat:1", "run_2", ttl_seconds=60) is False
    first.release_session_run("wechat:1", "run_1")
    assert second.acquire_session_run("wechat:1", "run_2", ttl_seconds=60) is True


def test_result_admission_does_not_use_keyword_answer_guard() -> None:
    from src.application.copilot.result_admission import admit_result

    result = admit_result(AppResult(status="answered", user_response="已修改配置并已发送通知。"))
    assert result.status == "answered"
    assert result.error is None


def test_result_admission_rejects_unparsed_tool_protocol() -> None:
    from src.application.copilot.result_admission import admit_result

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


def test_openai_responses_runner_uses_native_tools_and_parses_calls() -> None:
    captured: dict = {}

    def create_response_fn(**kwargs):
        captured.update(kwargs)
        return {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_7",
                    "name": "runtime_status",
                    "arguments": '{"config_key":"us"}',
                }
            ]
        }

    runner = build_model_runner(
        CopilotModelSettings(provider="openai", model="gpt-test", api_key_env="TEST_KEY"),
        environ={"TEST_KEY": "secret"},
        create_response_fn=create_response_fn,
    )
    turn = runner(
        ModelRequest(
            messages=(
                {"role": "system", "content": "system"},
                {"role": "user", "content": "status"},
            ),
            tools=(
                {
                    "name": "runtime_status",
                    "description": "status",
                    "input_schema": {"type": "object", "properties": {"config_key": {"type": "string"}}},
                },
            ),
            timeout_seconds=37,
        )
    )

    assert captured["instructions"] == "system"
    assert captured["tools"][0]["type"] == "function"
    assert captured["timeout"] == 37
    assert turn.tool_calls[0].arguments == {"config_key": "us"}
    assert turn.finish_reason == "length"
    assert turn.usage == {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}


def test_chat_completions_runner_uses_native_tool_messages() -> None:
    captured: dict = {}

    def create_chat_completion_fn(**kwargs):
        captured.update(kwargs)
        return {
            "choices": [{"message": {"content": "完成", "tool_calls": []}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }

    runner = build_model_runner(
        CopilotModelSettings(provider="deepseek", model="deepseek-chat", api_key_env="TEST_KEY"),
        environ={"TEST_KEY": "secret"},
        create_chat_completion_fn=create_chat_completion_fn,
    )
    turn = runner(
        ModelRequest(
            messages=(
                {"role": "user", "content": "status"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_1", "name": "runtime_status", "arguments": {"config_key": "us"}}
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "name": "runtime_status",
                    "content": '{"ok":true}',
                },
            ),
            tools=(
                {
                    "name": "runtime_status",
                    "description": "status",
                    "input_schema": {"type": "object", "properties": {}},
                },
            ),
        )
    )

    assert captured["tools"][0]["function"]["name"] == "runtime_status"
    assert captured["messages"][1]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "runtime_status", "arguments": '{"config_key": "us"}'},
        }
    ]
    assert captured["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"ok":true}',
    }
    assert turn.text == "完成"
    assert turn.finish_reason == "stop"
    assert turn.usage == {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}


def test_ollama_runner_does_not_require_api_key_or_send_thinking() -> None:
    captured: dict = {}

    def create_chat_completion_fn(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "完成"}, "finish_reason": "stop"}]}

    runner = build_model_runner(
        CopilotModelSettings.from_config({"provider": "ollama", "model": "gpt-oss:20b"}),
        environ={},
        create_chat_completion_fn=create_chat_completion_fn,
    )

    turn = runner(ModelRequest(messages=({"role": "user", "content": "status"},), tools=()))

    assert turn.text == "完成"
    assert captured["api_key"] == ""
    assert captured["base_url"] == "http://127.0.0.1:11434/v1"
    assert captured["thinking"] is None


def test_chat_completion_omits_authorization_without_api_key() -> None:
    captured: dict = {}

    def post(url, payload, *, headers, timeout):
        captured.update(url=url, payload=payload, headers=headers, timeout=timeout)
        return {"choices": []}

    create_chat_completion(
        model="gpt-oss:20b",
        base_url="http://127.0.0.1:11434/v1",
        messages=[{"role": "user", "content": "status"}],
        http_post_json_fn=post,
    )

    assert captured["headers"] == {"Content-Type": "application/json"}


def test_model_runner_retries_only_transient_errors() -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    class TransientError(Exception):
        http_status = 429

    def create_chat_completion_fn(**_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise TransientError("rate limited")
        return {"choices": [{"message": {"content": "完成"}, "finish_reason": "stop"}]}

    runner = build_model_runner(
        CopilotModelSettings(
            provider="deepseek",
            model="deepseek-chat",
            api_key_env="TEST_KEY",
            max_attempts=3,
        ),
        environ={"TEST_KEY": "secret"},
        create_chat_completion_fn=create_chat_completion_fn,
        sleep_fn=sleeps.append,
    )

    turn = runner(ModelRequest(messages=({"role": "user", "content": "status"},), tools=()))

    assert turn.text == "完成"
    assert turn.attempt_count == 2
    assert len(attempts) == 2
    assert sleeps == [0.25]


def test_model_runner_does_not_retry_non_transient_errors() -> None:
    attempts = 0

    def create_chat_completion_fn(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise ValueError("invalid request")

    runner = build_model_runner(
        CopilotModelSettings(
            provider="deepseek",
            model="deepseek-chat",
            api_key_env="TEST_KEY",
            max_attempts=3,
        ),
        environ={"TEST_KEY": "secret"},
        create_chat_completion_fn=create_chat_completion_fn,
        sleep_fn=lambda _seconds: None,
    )

    with pytest.raises(ValueError, match="invalid request"):
        runner(ModelRequest(messages=({"role": "user", "content": "status"},), tools=()))
    assert attempts == 1
