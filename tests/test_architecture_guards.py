from __future__ import annotations

import ast
import inspect
import re
import pytest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_infrastructure_does_not_import_application_layer() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "src" / "infrastructure").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "from src.application" in text or "import src.application" in text:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_feishu_ws_cli_has_no_secret_override_flags() -> None:
    text = (ROOT / "src" / "interfaces" / "cli" / "main.py").read_text(encoding="utf-8")

    for flag in ("--app-id", "--app-secret", "--encrypt-key", "--verification-token"):
        assert flag not in text


def test_feishu_https_callback_gateway_does_not_regress() -> None:
    offenders: list[str] = []
    needle_dash = "feishu" + "-gateway"
    needle_module = "feishu" + "_gateway"
    for path in [ROOT / "src", ROOT / "tests"]:
        for item in sorted(path.rglob("*.py")):
            text = item.read_text(encoding="utf-8")
            if needle_dash in text or needle_module in text:
                offenders.append(str(item.relative_to(ROOT)))

    assert offenders == []


def test_feishu_server_dependencies_do_not_restore_callback_stack() -> None:
    combined = "\n".join(
        [
            (ROOT / "requirements" / "server.txt").read_text(encoding="utf-8"),
            (ROOT / "constraints" / "server.txt").read_text(encoding="utf-8"),
        ]
    )

    for package in ("fastapi", "uvicorn", "cryptography"):
        assert package not in combined


def test_feishu_bot_resolver_uses_fixed_env_names_only() -> None:
    from src.application.secret_resolver import resolve_feishu_bot_config

    source = inspect.getsource(resolve_feishu_bot_config)

    assert "del notifications" in source
    assert ".get(" not in source


def test_inbound_and_secret_paths_use_settings_for_environment_reads() -> None:
    offenders: list[str] = []
    checked_roots = [
        ROOT / "src" / "application" / "inbound",
        ROOT / "src" / "application" / "secret_resolver.py",
        ROOT / "src" / "application" / "runtime_paths.py",
        ROOT / "src" / "application" / "runtime_config_paths.py",
        ROOT / "src" / "application" / "config_loader.py",
        ROOT / "src" / "application" / "ledger" / "store_resolution.py",
        ROOT / "src" / "application" / "ledger" / "read_model.py",
        ROOT / "src" / "application" / "notification_delivery_adapter.py",
    ]
    for root in checked_roots:
        paths = sorted(root.rglob("*.py")) if root.is_dir() else [root]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            if "os.environ" in text or "os.getenv" in text:
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_feishu_ws_behavior_does_not_regress_to_env_settings() -> None:
    forbidden = ("OM_FEISHU_ACK_REACTION", "OM_FEISHU_WS_QUEUE_SIZE", "OM_FEISHU_REPLY_MAX_CHARS")
    offenders: list[str] = []
    for root in [ROOT / "src", ROOT / "configs" / "examples"]:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".json", ".example"}:
                continue
            if path == ROOT / "src" / "application" / "settings" / "effective.py":
                continue
            text = path.read_text(encoding="utf-8")
            if any(item in text for item in forbidden):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_runtime_config_generation_excludes_assistant_control_plane() -> None:
    from src.application.config_yaml import PASSTHROUGH_KEYS, resolve_yaml_runtime_config

    assert "assistant" not in PASSTHROUGH_KEYS
    assert "inbound" not in PASSTHROUGH_KEYS

    cfg, _meta = resolve_yaml_runtime_config(
        repo_root=ROOT,
        market="us",
        config_path=ROOT / "configs" / "examples" / "config.yaml.example",
    )

    assert "assistant" not in cfg
    assert "inbound" not in cfg


def test_strategy_mode_interpretation_stays_in_strategy_policy() -> None:
    checked = [
        ROOT / "src" / "application" / "required_data_planning.py",
        ROOT / "src" / "application" / "required_data_prefetch_planning.py",
        ROOT / "src" / "application" / "sell_put_steps.py",
        ROOT / "src" / "application" / "sell_call_steps.py",
        ROOT / "src" / "application" / "pipeline_context.py",
        ROOT / "src" / "application" / "sell_put_call_helper.py",
    ]
    forbidden_patterns = {
        "direct_short_vol_branch": re.compile(r"==\s*['\"]short_vol['\"]"),
        "direct_return_first_branch": re.compile(r"==\s*['\"]return_first['\"]"),
        "local_short_vol_helper": re.compile(r"def\s+_wants_short_vol\b"),
        "yield_mode_branch": re.compile(r"yield_enhancement_policy\.mode\s*=="),
        "vol_convexity_constant_branch": re.compile(r"YIELD_ENHANCEMENT_VOL_CONVEXITY_MODE"),
    }
    offenders: list[str] = []
    for path in checked:
        text = path.read_text(encoding="utf-8")
        for label, pattern in forbidden_patterns.items():
            if pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT)}:{label}")

    assert offenders == []


def test_assistant_config_rejects_business_runtime_shape() -> None:
    from src.application.config_validator import validate_assistant_config

    with pytest.raises(SystemExit) as exc:
        validate_assistant_config(
            {
                "accounts": ["lx"],
                "portfolio": {"broker": "富途"},
                "symbols": [{"symbol": "NVDA"}],
                "assistant": {"enabled": True, "planner": {"enabled": True}},
            }
        )

    assert "use config.assistant.json, not config.<market>.json" in str(exc.value)


def test_agent_loop_copilot_surface_keeps_read_only_and_preview_limited() -> None:
    from src.application.agent_tool_registry import get_tool_definition
    from src.application.assistant.agent_loop import AGENT_LOOP_PREVIEW_CAPABILITIES, AGENT_LOOP_READ_TOOLS
    from src.application.assistant.capability_catalog import planner_preview_specs, planner_read_specs
    from src.application.tool_allowlist import PURE_READ_TOOLS

    read_tool_names = {str(spec.tool_name) for spec in planner_read_specs() if spec.tool_name}
    preview_names = {spec.intent_name for spec in planner_preview_specs()}

    assert AGENT_LOOP_READ_TOOLS == read_tool_names - {"inbound.pending", "inbound.symbols"}
    assert "close_advice_read" in AGENT_LOOP_READ_TOOLS
    assert "symbol_config_read" in AGENT_LOOP_READ_TOOLS
    assert "query_cash_headroom" in AGENT_LOOP_READ_TOOLS
    assert "symbol_edit" in AGENT_LOOP_PREVIEW_CAPABILITIES
    assert "manual_trade_open" in AGENT_LOOP_PREVIEW_CAPABILITIES
    assert "manual_trade_confirm" not in AGENT_LOOP_PREVIEW_CAPABILITIES
    assert "symbol_confirm" not in AGENT_LOOP_PREVIEW_CAPABILITIES
    assert AGENT_LOOP_PREVIEW_CAPABILITIES <= preview_names

    for tool_name in AGENT_LOOP_READ_TOOLS:
        definition = get_tool_definition(tool_name)
        assert definition is not None, tool_name
        assert tool_name in PURE_READ_TOOLS
        assert definition.risk_level == "read_only"
        assert not definition.side_effects
        assert definition.requires_confirm is False


def test_copilot_tool_metadata_lives_on_agent_tool_definitions() -> None:
    from dataclasses import fields

    from src.application.agent_tool_registry import get_tool_definition
    from src.application.assistant.agent_loop import _copilot_tool_manifest
    from src.application.assistant.tool_bindings import AssistantToolBinding

    binding_fields = {field.name for field in fields(AssistantToolBinding)}
    assert "description" not in binding_fields
    assert "input_schema" not in binding_fields
    assert "output_contract" not in binding_fields
    assert "planner_notes" not in binding_fields
    assert "planner_semantics" not in binding_fields
    assert "copilot_notes" not in binding_fields

    manifest_by_name = {str(item["name"]): item for item in _copilot_tool_manifest()}
    for tool_name in (
        "monthly_income_report",
        "analysis_catalog",
        "analysis_query",
        "option_positions_read",
        "query_cash_headroom",
        "symbol_config_read",
        "symbol_resolve",
        "candidate_filter_explain",
    ):
        definition = get_tool_definition(tool_name)
        assert definition is not None, tool_name
        assert definition.planner_notes, tool_name
        assert definition.resolve_planner_semantics({"analysis_view_names": None}), tool_name
        assert manifest_by_name[tool_name]["copilot_notes"] == list(definition.planner_notes)
        assert manifest_by_name[tool_name]["semantics"] == definition.resolve_planner_semantics(
            {"analysis_view_names": None}
        )


def test_read_tool_allowlist_has_neutral_owner() -> None:
    from src.application import tool_allowlist
    from src.application.assistant import policy as assistant_policy
    from src.application.assistant import tool_policy

    assert assistant_policy.PURE_READ_TOOLS is tool_allowlist.PURE_READ_TOOLS
    assert tool_policy.PURE_READ_TOOLS is tool_allowlist.PURE_READ_TOOLS

    tool_policy_text = (ROOT / "src" / "application" / "assistant" / "tool_policy.py").read_text(encoding="utf-8")
    assert "from src.application.inbound.policy import PURE_READ_TOOLS" not in tool_policy_text
    assert "from src.application.assistant.policy import PURE_READ_TOOLS" not in tool_policy_text


def test_agent_write_gate_delegates_to_registry_policy() -> None:
    tool_execution_text = (ROOT / "src" / "application" / "tool_execution.py").read_text(encoding="utf-8")
    permissions_text = (ROOT / "src" / "application" / "agent_tools" / "permissions.py").read_text(encoding="utf-8")

    assert "def _write_gate_error" not in tool_execution_text
    assert "from src.application.agent_tool_handlers" not in tool_execution_text
    assert "TOOL_HANDLERS" not in tool_execution_text
    assert 'name == "version_update"' not in tool_execution_text
    assert 'name == "manage_symbols"' not in tool_execution_text
    assert "write_gate_error(definition, payload_dict)" in tool_execution_text
    assert "def write_gate_error(" in permissions_text
    assert "tool_write_requested(tool, payload)" in permissions_text


def test_agent_registry_collects_domain_modules_instead_of_tool_tuple() -> None:
    registry_text = (ROOT / "src" / "application" / "agent_tool_registry.py").read_text(encoding="utf-8")

    assert "AGENT_TOOL_MODULES" in registry_text
    assert "pkgutil.iter_modules" in registry_text
    assert "importlib.import_module" in registry_text
    assert "_collect_tool_definitions" in registry_text
    assert "for module in AGENT_TOOL_MODULES" in registry_text
    assert 'module_name.endswith("_impl")' in registry_text
    assert 'module_name.endswith("_helpers")' in registry_text
    assert "AgentToolDefinition(" not in registry_text
    assert "from src.application.agent_tools.candidate import" not in registry_text


def test_legacy_agent_tool_modules_are_compatibility_shims() -> None:
    allowed_owner_modules = {
        "agent_tool_config.py",
        "agent_tool_contracts.py",
        "agent_tool_init_local.py",
        "agent_tool_registry.py",
    }
    allowed_shim_wrappers = {
        "agent_tool_runtime_status.py": {"runtime_status_tool"},
    }
    offenders: list[str] = []
    for path in sorted((ROOT / "src" / "application").glob("agent_tool_*.py")):
        if path.name in allowed_owner_modules:
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        local_defs = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        imported_modules = [
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module is not None
        ]
        allowed_defs = allowed_shim_wrappers.get(path.name, set())
        unexpected_defs = [name for name in local_defs if name not in allowed_defs]
        if unexpected_defs:
            offenders.append(f"{path.relative_to(ROOT)} defines {unexpected_defs}")
        if allowed_defs and not all(f"_impl.{name}" in text for name in allowed_defs):
            offenders.append(f"{path.relative_to(ROOT)} wrapper does not forward to agent_tools impl")
        if local_defs and not allowed_defs:
            offenders.append(f"{path.relative_to(ROOT)} defines {local_defs}")
        if not any(
            str(module) == "src.application.agent_tools"
            or str(module).startswith("src.application.agent_tools.")
            for module in imported_modules
        ):
            offenders.append(f"{path.relative_to(ROOT)} does not re-export from agent_tools")

    assert offenders == []


def test_assistant_tool_names_are_registry_or_inbound_surfaces() -> None:
    from src.application.agent_tool_registry import tool_names
    from src.application.assistant.capability_catalog import command_specs

    registry_names = set(tool_names())
    inbound_operation_surfaces = {
        "inbound.manual_trade",
        "inbound.model",
        "inbound.monitor_run",
        "inbound.pending",
        "inbound.symbols",
        "inbound.upgrade",
    }
    unknown = sorted(
        {
            str(spec.tool_name)
            for spec in command_specs()
            if spec.tool_name is not None
            and str(spec.tool_name) not in registry_names
            and str(spec.tool_name) not in inbound_operation_surfaces
        }
    )

    assert unknown == []


def test_assistant_owns_command_catalog_and_interaction_contracts() -> None:
    from src.application.assistant.capability_catalog import capability_catalog_text
    from src.application.assistant.capability_catalog import capability_specs
    from src.application.assistant.capability_catalog import command_specs

    assert capability_catalog_text()
    assert capability_specs()
    assert command_specs()

    contracts_text = (ROOT / "src" / "application" / "assistant" / "contracts.py").read_text(encoding="utf-8")
    assert "InboundRequest" not in contracts_text
    assert "InboundIntent" not in contracts_text
    assert "InboundToolCall" not in contracts_text

    feishu_text = (ROOT / "src" / "application" / "inbound" / "feishu.py").read_text(encoding="utf-8")
    assert "src.application.agent_runtime.command_catalog" not in feishu_text
    assert "src.application.agent_runtime import handle_agent_message" not in feishu_text
    assert "src.application.assistant.runtime import handle_assistant_turn" in feishu_text

    wechat_text = (ROOT / "src" / "application" / "channels" / "wechat_clawbot" / "inbound.py").read_text(encoding="utf-8")
    assert "src.application.assistant.runtime import handle_assistant_turn" in wechat_text

    main_text = (ROOT / "src" / "interfaces" / "cli" / "main.py").read_text(encoding="utf-8")
    assistant_cli_text = (ROOT / "src" / "interfaces" / "cli" / "assistant_ops.py").read_text(encoding="utf-8")
    assert "handle_assistant_turn_fn" in assistant_cli_text
    assert "handle_assistant_message_fn" not in assistant_cli_text
    assert "handle_assistant_message" not in main_text


def test_assistant_runtime_delegates_perception() -> None:
    runtime_text = (ROOT / "src" / "application" / "assistant" / "runtime.py").read_text(encoding="utf-8")
    perception_text = (ROOT / "src" / "application" / "assistant" / "perception.py").read_text(encoding="utf-8")

    assert "PerceptionEngine(" in runtime_text
    forbidden_runtime_tokens = (
        "parse_assistant_command",
        "parse_deterministic_text",
        "run_read_only_agent_loop",
        "generate_general_reply",
        "build_conversation_context",
        "context_trace",
    )
    offenders = [token for token in forbidden_runtime_tokens if token in runtime_text]
    assert offenders == []

    perception_tokens = tuple(token for token in forbidden_runtime_tokens if token != "parse_deterministic_text") + (
        "parse_permission_response",
    )
    for token in perception_tokens:
        assert token in perception_text
    assert "translate_inbound_intent" not in perception_text


def test_assistant_router_uses_perception_reasoning_action_observation_chain() -> None:
    router_text = (ROOT / "src" / "application" / "assistant" / "router.py").read_text(encoding="utf-8")
    reasoning_text = (ROOT / "src" / "application" / "assistant" / "reasoning.py").read_text(encoding="utf-8")
    contracts_text = (ROOT / "src" / "application" / "assistant" / "contracts.py").read_text(encoding="utf-8")

    assert "resolve_reasoning(" in router_text
    assert "perform_action(" in router_text
    assert "build_observation(" in router_text
    assert "frame_planner" not in router_text
    assert "def _tool_call_from_intent" not in router_text
    assert "is_manual_trade_operation_intent" not in router_text
    assert "is_symbol_operation_intent" not in router_text
    assert "is_upgrade_operation_intent" not in router_text
    assert "def resolve_reasoning(" in reasoning_text
    assert "ReasoningResolution(" in reasoning_text
    assert "class PerceptionResult" in contracts_text
    assert "class ReasoningResolution" in contracts_text
    assert "class ActionResult" in contracts_text
    assert "class ObservationResponse" in contracts_text
    assert "class AssistantFrame" not in contracts_text
    assert "class ToolPlan" not in contracts_text
    for module in ("manual_trade_operations.py", "symbol_operations.py", "upgrade_operations.py"):
        module_text = (ROOT / "src" / "application" / "assistant" / module).read_text(encoding="utf-8")
        assert "is_manual_trade_operation_intent" not in module_text
        assert "is_symbol_operation_intent" not in module_text
        assert "is_upgrade_operation_intent" not in module_text


def test_assistant_perception_result_is_canonical_contract_name() -> None:
    from src.application.assistant.contracts import PerceptionResult

    assert PerceptionResult.__name__ == "PerceptionResult"

    offenders: list[str] = []
    allowed = {"contracts.py", "__init__.py"}
    for path in sorted((ROOT / "src" / "application" / "assistant").glob("*.py")):
        if path.name in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if "AssistantIntent" in text or "SemanticFrame" in text:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_perception_producers_do_not_plan_or_execute_tools() -> None:
    forbidden = (
        "ToolCall",
        "ReasoningResolution",
        "frame_from_intent",
        "frame_from_semantic_frame",
        "tool_plan_from_frame",
        "execute_tool",
        "enforce_tool_allowed",
        "handle_manual_trade_operation",
        "handle_symbol_operation",
        "handle_upgrade_operation",
        "tool_name=",
    )
    checked = [
        ROOT / "src" / "application" / "assistant" / "command_parser.py",
        ROOT / "src" / "application" / "assistant" / "deterministic_commands.py",
    ]
    assert not (ROOT / "src" / "application" / "assistant" / "llm_intent_schema.py").exists()
    assert not (ROOT / "src" / "application" / "assistant" / "llm_translator.py").exists()
    offenders: dict[str, list[str]] = {}
    for path in checked:
        text = path.read_text(encoding="utf-8")
        hits = [token for token in forbidden if token in text]
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits

    assert offenders == {}


def test_notification_perception_path_does_not_call_assistant_message_runtime() -> None:
    checked = [
        ROOT / "src" / "application" / "tick_notification_flow.py",
        ROOT / "src" / "application" / "multi_tick" / "assistant_perception_event.py",
    ]
    offenders: list[str] = []
    for path in checked:
        text = path.read_text(encoding="utf-8")
        if "handle_assistant_message" in text:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_legacy_assistant_frame_and_tool_plan_are_removed() -> None:
    assert not (ROOT / "src" / "application" / "assistant" / "commands.py").exists()
    assert not (ROOT / "src" / "application" / "assistant" / "parser.py").exists()
    assert not (ROOT / "src" / "application" / "assistant" / "frame_planner.py").exists()
    assert not (ROOT / "src" / "application" / "assistant" / "intent_arbitrator.py").exists()
    assert not (ROOT / "src" / "application" / "assistant" / "intent_arbitration.py").exists()
    assert not (ROOT / "src" / "application" / "assistant" / "semantic_frames.py").exists()

    legacy_offenders: list[str] = []
    for path in sorted((ROOT / "src" / "application" / "assistant").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if any(
            token in text
            for token in (
                "AssistantFrame",
                "ToolPlan",
                "SemanticFrame",
                "AssistantIntent",
                "LlmTranslatorSettings",
            )
        ):
            legacy_offenders.append(str(path.relative_to(ROOT)))

    assert legacy_offenders == []

    settings_text = (ROOT / "src" / "application" / "assistant" / "settings.py").read_text(encoding="utf-8")
    assert "DEFAULT_ASSISTANT_MODE" not in settings_text
    assert "ASSISTANT_MODES" not in settings_text
    assert "mode: str" not in settings_text
    assert '"mode": self.mode' not in settings_text


def test_legacy_provider_planner_runtime_is_removed() -> None:
    removed_files = (
        "evidence_planner.py",
        "model_continuation.py",
        "task_runtime.py",
    )
    for filename in removed_files:
        assert not (ROOT / "src" / "application" / "assistant" / filename).exists()

    removed_tests = (
        "test_assistant_event_executor.py",
        "test_assistant_model_continuation.py",
    )
    for filename in removed_tests:
        assert not (ROOT / "tests" / filename).exists()

    forbidden_source_tokens = (
        "assistant.evidence_planner",
        "assistant.model_continuation",
        "assistant.task_runtime",
        "PlannerPlan",
        "execute_tool_plan(",
    )
    offenders: dict[str, list[str]] = {}
    for path in sorted((ROOT / "src" / "application" / "assistant").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        hits = [token for token in forbidden_source_tokens if token in text]
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    assert offenders == {}

    provider_files = (
        ROOT / "src" / "infrastructure" / "openai_chat_completions.py",
        ROOT / "src" / "infrastructure" / "openai_responses.py",
    )
    provider_forbidden = ("tool_calls", '"tools"', "'tools'", "function_call")
    provider_offenders: dict[str, list[str]] = {}
    for path in provider_files:
        text = path.read_text(encoding="utf-8")
        hits = [token for token in provider_forbidden if token in text]
        if hits:
            provider_offenders[str(path.relative_to(ROOT))] = hits
    assert provider_offenders == {}


def test_runtime_router_and_arbitrator_do_not_know_model_profiles() -> None:
    forbidden = (
        "active_model",
        "models",
        "LlmModelProfile",
        "llm_model_profiles",
    )
    checked = [
        ROOT / "src" / "application" / "assistant" / "runtime.py",
        ROOT / "src" / "application" / "assistant" / "router.py",
        ROOT / "src" / "application" / "assistant" / "perception.py",
        ROOT / "src" / "application" / "assistant" / "reasoning.py",
    ]
    offenders: dict[str, list[str]] = {}
    for path in checked:
        text = path.read_text(encoding="utf-8")
        hits = [token for token in forbidden if token in text]
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits

    assert offenders == {}


def test_assistant_model_cli_does_not_accept_secret_values() -> None:
    text = (ROOT / "src" / "interfaces" / "cli" / "assistant_ops.py").read_text(encoding="utf-8")

    assert '"--api-key"' not in text
    assert "'--api-key'" not in text
    assert '"--api-key-env"' in text


def test_assistant_package_does_not_import_inbound_package() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "src" / "application" / "assistant").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "src.application.inbound" in text:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_application_uses_assistant_control_plane_not_agent_runtime_backend() -> None:
    offenders: list[str] = []
    checked_roots = [ROOT / "src" / "application", ROOT / "src" / "interfaces"]
    for root in checked_roots:
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "src.application.agent_runtime" in text:
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_agent_runtime_package_is_removed() -> None:
    assert not (ROOT / "src" / "application" / "agent_runtime").exists()

    runtime_text = (ROOT / "src" / "application" / "assistant" / "runtime.py").read_text(encoding="utf-8")
    settings_text = (ROOT / "src" / "application" / "assistant" / "settings.py").read_text(encoding="utf-8")
    assert "handle_agent_message" not in runtime_text
    assert "AgentRuntimeSettings" not in settings_text


def test_inbound_package_exposes_transport_only() -> None:
    inbound_files = {
        path.name
        for path in sorted((ROOT / "src" / "application" / "inbound").glob("*.py"))
    }
    assert inbound_files == {"__init__.py", "feishu.py", "feishu_ws.py"}

    root_text = (ROOT / "src" / "application" / "inbound" / "__init__.py").read_text(encoding="utf-8")
    assert "handle_assistant_request" not in root_text
    assert "parse_deterministic_text" not in root_text
    assert "render_inbound_text" not in root_text
    assert "AssistantRequest" not in root_text


def test_inbound_transport_does_not_import_assistant_control_plane_details() -> None:
    forbidden = (
        "src.application.assistant.agent_loop",
        "src.application.assistant.command_parser",
        "src.application.assistant.capability_catalog",
        "src.application.assistant.frame_planner",
        "src.application.assistant.intent_arbitrator",
        "src.application.assistant.perception",
        "src.application.assistant.reasoning",
        "src.application.assistant.llm_reply",
        "src.application.assistant.llm_translator",
        "src.application.assistant.deterministic_commands",
        "src.application.assistant.router",
    )
    offenders: list[str] = []
    for path in sorted((ROOT / "src" / "application" / "inbound").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if any(item in text for item in forbidden):
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_config_section_helpers_have_neutral_owner() -> None:
    from src.application import config_loader, config_sections

    assert config_loader.resolve_templates_config is config_sections.resolve_templates_config
    assert config_loader.resolve_watchlist_config is config_sections.resolve_watchlist_config
    assert config_loader.set_watchlist_config is config_sections.set_watchlist_config

    validator_text = (ROOT / "src" / "application" / "config_validator.py").read_text(encoding="utf-8")
    assert "from src.application.config_loader import" not in validator_text


def test_feishu_ws_cli_keeps_runtime_and_assistant_config_flags_separate() -> None:
    main_text = (ROOT / "src" / "interfaces" / "cli" / "main.py").read_text(encoding="utf-8")
    inbound_text = (ROOT / "src" / "interfaces" / "cli" / "inbound_ops.py").read_text(encoding="utf-8")

    assert 'inbound_ws.add_argument("--config-path"' in inbound_text
    assert 'inbound_ws.add_argument("--assistant-config"' in inbound_text
    assert "handle_inbound_command(" in main_text
    assert "build_feishu_ws_settings_fn(" in inbound_text
    assert "config_path=args.config_path" in inbound_text
    assert "assistant_config_path=args.assistant_config" in inbound_text


def test_cli_public_surface_keeps_assistant_control_and_inbound_transport_separate() -> None:
    main_text = (ROOT / "src" / "interfaces" / "cli" / "main.py").read_text(encoding="utf-8")
    assistant_text = (ROOT / "src" / "interfaces" / "cli" / "assistant_ops.py").read_text(encoding="utf-8")
    inbound_text = (ROOT / "src" / "interfaces" / "cli" / "inbound_ops.py").read_text(encoding="utf-8")
    cli_text = "\n".join([main_text, assistant_text, inbound_text])

    assert 'assistant_sub.add_parser("handle"' in assistant_text
    assert "handle_assistant_command(" in main_text
    assert 'inbound_sub.add_parser("handle"' not in inbound_text
    assert 'inbound_sub.add_parser("pending"' not in inbound_text
    assert 'inbound_sub.add_parser("audit"' not in inbound_text
    assert 'inbound_sub.add_parser("upgrade-worker"' not in inbound_text
    assert "AgentRuntime" not in cli_text
    assert '"--agent-runtime"' not in cli_text
    assert '"--no-agent-runtime"' not in cli_text


def test_feishu_payload_adapter_public_signature_uses_assistant_naming() -> None:
    from src.application.inbound.feishu import handle_feishu_payload

    params = inspect.signature(handle_feishu_payload).parameters
    assert "assistant_settings" in params
    assert "use_assistant" not in params
    assert "use_agent_runtime" not in params
    assert "agent_runtime_settings" not in params
    source = inspect.getsource(handle_feishu_payload)
    assert "legacy_kwargs" not in source
    assert "agent_runtime" not in source


def test_openclaw_readiness_tool_is_retired() -> None:
    runtime_text = (ROOT / "src" / "application" / "agent_tools" / "runtime_status_impl.py").read_text(encoding="utf-8")
    runtime_shim_text = (ROOT / "src" / "application" / "agent_tool_runtime_status.py").read_text(encoding="utf-8")
    diagnostics_text = (ROOT / "src" / "application" / "agent_tools" / "diagnostics.py").read_text(encoding="utf-8")

    assert not (ROOT / "src" / "application" / "agent_tools" / "openclaw_impl.py").exists()
    assert not (ROOT / "src" / "application" / "agent_tool_openclaw.py").exists()
    assert "def runtime_status_tool(" in runtime_text
    assert "openclaw_readiness" not in diagnostics_text
    assert "_impl.runtime_status_tool" in runtime_shim_text
    assert "service_status_from_profile = _impl.service_status_from_profile" in runtime_shim_text
    assert "from src.application.agent_tools.runtime_status_impl import runtime_status_tool" in diagnostics_text
    assert not (ROOT / "src" / "application" / "agent_tool_handlers.py").exists()
