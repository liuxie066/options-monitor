from __future__ import annotations

import inspect
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


def test_assistant_config_rejects_business_runtime_shape() -> None:
    from src.application.config_validator import validate_assistant_config

    with pytest.raises(SystemExit) as exc:
        validate_assistant_config(
            {
                "accounts": ["lx"],
                "portfolio": {"broker": "富途"},
                "symbols": [{"symbol": "NVDA"}],
                "assistant": {"mode": "deterministic"},
            }
        )

    assert "use config.assistant.json, not config.<market>.json" in str(exc.value)


def test_llm_intent_surface_is_read_only_only() -> None:
    from src.application.assistant.llm_intent_schema import llm_intent_json_schema, llm_intent_schema
    from src.application.agent_tool_registry import get_tool_definition
    from src.application.assistant.commands import llm_executable_specs
    from src.application.tool_allowlist import PURE_READ_TOOLS

    schema = llm_intent_schema()
    json_schema = llm_intent_json_schema()
    allowed_names = {spec.intent_name for spec in llm_executable_specs()}

    assert schema["write_intents_allowed"] is False
    assert set(json_schema["properties"]["intent"]["enum"]) == allowed_names
    assert not any(name.endswith(("_confirm", "_cancel")) for name in allowed_names)

    pure_read_router_tools = {"inbound.pending", "inbound.symbols"}
    for spec in llm_executable_specs():
        assert spec.read_only is True
        if spec.tool_name is None:
            continue
        if spec.tool_name in pure_read_router_tools:
            assert spec.intent_name in {"pending_operations", "symbol_list"}
            continue
        definition = get_tool_definition(spec.tool_name)
        assert definition is not None, spec.tool_name
        assert spec.tool_name in PURE_READ_TOOLS
        assert definition.risk_level == "read_only"
        assert not definition.side_effects
        assert definition.requires_confirm is False


def test_read_tool_allowlist_has_neutral_owner() -> None:
    from src.application import tool_allowlist
    from src.application.assistant import policy as assistant_policy
    from src.application.assistant import tool_policy

    assert assistant_policy.PURE_READ_TOOLS is tool_allowlist.PURE_READ_TOOLS
    assert tool_policy.PURE_READ_TOOLS is tool_allowlist.PURE_READ_TOOLS

    tool_policy_text = (ROOT / "src" / "application" / "assistant" / "tool_policy.py").read_text(encoding="utf-8")
    assert "from src.application.inbound.policy import PURE_READ_TOOLS" not in tool_policy_text
    assert "from src.application.assistant.policy import PURE_READ_TOOLS" not in tool_policy_text


def test_assistant_owns_command_catalog_and_interaction_contracts() -> None:
    from src.application.assistant.commands import capability_catalog_text
    from src.application.assistant.commands import capability_specs
    from src.application.assistant.commands import command_specs

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
    assert "src.application.assistant.runtime import handle_assistant_message" in feishu_text


def test_assistant_runtime_delegates_intent_arbitration() -> None:
    runtime_text = (ROOT / "src" / "application" / "assistant" / "runtime.py").read_text(encoding="utf-8")
    arbitrator_text = (ROOT / "src" / "application" / "assistant" / "intent_arbitrator.py").read_text(encoding="utf-8")

    assert "IntentArbitrator(" in runtime_text
    forbidden_runtime_tokens = (
        "parse_assistant_command",
        "parse_inbound_text",
        "translate_inbound_intent",
        "run_read_only_agent_loop",
        "generate_general_reply",
        "build_conversation_context",
        "context_trace",
    )
    offenders = [token for token in forbidden_runtime_tokens if token in runtime_text]
    assert offenders == []

    for token in forbidden_runtime_tokens:
        assert token in arbitrator_text


def test_assistant_router_delegates_tool_planning() -> None:
    router_text = (ROOT / "src" / "application" / "assistant" / "router.py").read_text(encoding="utf-8")
    planner_text = (ROOT / "src" / "application" / "assistant" / "frame_planner.py").read_text(encoding="utf-8")
    contracts_text = (ROOT / "src" / "application" / "assistant" / "contracts.py").read_text(encoding="utf-8")

    assert "tool_plan_from_frame(" in router_text
    assert "def _tool_call_from_intent" not in router_text
    assert "is_manual_trade_operation_intent" not in router_text
    assert "is_symbol_operation_intent" not in router_text
    assert "is_upgrade_operation_intent" not in router_text
    assert "def frame_from_intent(" in planner_text
    assert "def tool_plan_from_frame(" in planner_text
    assert "PLANNED_TOOL_INTENTS" in planner_text
    assert "class AssistantFrame" in contracts_text
    assert "class ToolPlan" in contracts_text
    for module in ("manual_trade_operations.py", "symbol_operations.py", "upgrade_operations.py"):
        module_text = (ROOT / "src" / "application" / "assistant" / module).read_text(encoding="utf-8")
        assert "is_manual_trade_operation_intent" not in module_text
        assert "is_symbol_operation_intent" not in module_text
        assert "is_upgrade_operation_intent" not in module_text


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
    assert "parse_inbound_text" not in root_text
    assert "render_inbound_text" not in root_text
    assert "AssistantRequest" not in root_text


def test_inbound_transport_does_not_import_assistant_control_plane_details() -> None:
    forbidden = (
        "src.application.assistant.agent_loop",
        "src.application.assistant.command_parser",
        "src.application.assistant.commands",
        "src.application.assistant.frame_planner",
        "src.application.assistant.intent_arbitrator",
        "src.application.assistant.llm_reply",
        "src.application.assistant.llm_translator",
        "src.application.assistant.parser",
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
    text = (ROOT / "src" / "interfaces" / "cli" / "main.py").read_text(encoding="utf-8")

    assert 'inbound_ws.add_argument("--config-path"' in text
    assert 'inbound_ws.add_argument("--assistant-config"' in text
    assert "build_feishu_ws_settings(" in text
    assert "config_path=args.config_path" in text
    assert "assistant_config_path=args.assistant_config" in text


def test_cli_public_surface_keeps_assistant_control_and_inbound_transport_separate() -> None:
    text = (ROOT / "src" / "interfaces" / "cli" / "main.py").read_text(encoding="utf-8")

    assert 'assistant_sub.add_parser("handle"' in text
    assert 'inbound_sub.add_parser("handle"' not in text
    assert 'inbound_sub.add_parser("pending"' not in text
    assert 'inbound_sub.add_parser("audit"' not in text
    assert 'inbound_sub.add_parser("upgrade-worker"' not in text
    assert "AgentRuntime" not in text
    assert '"--agent-runtime"' not in text
    assert '"--no-agent-runtime"' not in text


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


def test_runtime_status_tool_is_not_owned_by_openclaw_module() -> None:
    openclaw_text = (ROOT / "src" / "application" / "agent_tool_openclaw.py").read_text(encoding="utf-8")
    runtime_text = (ROOT / "src" / "application" / "agent_tool_runtime_status.py").read_text(encoding="utf-8")
    handlers_text = (ROOT / "src" / "application" / "agent_tool_handlers.py").read_text(encoding="utf-8")

    assert "def runtime_status_tool(" not in openclaw_text
    assert "def runtime_status_tool(" in runtime_text
    assert "from src.application.agent_tool_runtime_status import runtime_status_tool" in handlers_text
