from __future__ import annotations

import ast
import pytest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def _imported_modules_with_from_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                modules.append(module)
                modules.extend(f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def test_position_lot_does_not_read_compatibility_fee_field() -> None:
    assert "event.fees" not in (ROOT / "domain" / "domain" / "ledger" / "lots.py").read_text(
        encoding="utf-8"
    )


def test_executed_fee_readers_do_not_import_fee_formulas() -> None:
    readers = (
        ROOT / "domain" / "domain" / "assigned_stock.py",
        ROOT / "domain" / "domain" / "ledger" / "lots.py",
        ROOT / "domain" / "domain" / "ledger" / "projection.py",
        ROOT / "domain" / "domain" / "performance" / "engine.py",
        ROOT / "src" / "application" / "cash_conversion.py",
        ROOT / "src" / "application" / "ledger" / "current_decision_projection.py",
    )
    offenders = [
        str(path.relative_to(ROOT))
        for path in readers
        if any(
            module == "domain.domain.fee_calc"
            for module in _imported_modules(path)
        )
    ]

    assert offenders == []


def test_runtime_config_generation_excludes_assistant_control_plane() -> None:
    from src.application.config_yaml import PASSTHROUGH_KEYS, resolve_yaml_runtime_config

    assert "assistant" not in PASSTHROUGH_KEYS
    assert "inbound" not in PASSTHROUGH_KEYS
    assert "trade_intake" not in PASSTHROUGH_KEYS

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
                "assistant": {"enabled": True, "copilot": {"enabled": True}},
            }
        )

    assert "use config.assistant.json, not config.<market>.json" in str(exc.value)


def test_copilot_runtime_does_not_import_old_assistant_or_shell_tool_gateway() -> None:
    copilot_root = ROOT / "src" / "application" / "copilot"
    assert copilot_root.exists()

    import_offenders: list[str] = []
    shell_offenders: list[str] = []
    for path in sorted(copilot_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("src.application.assistant") or module.startswith("scripts"):
                    import_offenders.append(f"{path.relative_to(ROOT)}:{module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("src.application.assistant") or alias.name.startswith("scripts"):
                        import_offenders.append(f"{path.relative_to(ROOT)}:{alias.name}")
        if "./om-agent" in text or "subprocess" in text or "os.system" in text:
            shell_offenders.append(str(path.relative_to(ROOT)))

    assert import_offenders == []
    assert shell_offenders == []


def test_public_tool_gateway_does_not_import_or_expose_copilot_runtime() -> None:
    import json

    checked_python_files = [
        ROOT / "src" / "interfaces" / "agent" / "cli.py",
        ROOT / "src" / "application" / "agent_tool_registry.py",
        ROOT / "src" / "application" / "tool_execution.py",
    ]
    import_offenders: list[str] = []
    reference_offenders: list[str] = []

    for path in checked_python_files:
        imports = _imported_modules(path)
        for module in imports:
            if module.startswith("src.application.copilot"):
                import_offenders.append(f"{path.relative_to(ROOT)}:{module}")
        text = path.read_text(encoding="utf-8")
        if "src.application.copilot" in text or "CopilotRequest" in text or "ExecutionContract" in text:
            reference_offenders.append(str(path.relative_to(ROOT)))

    om_agent_text = (ROOT / "om-agent").read_text(encoding="utf-8")
    assert "copilot" not in om_agent_text.lower()
    assert import_offenders == []
    assert reference_offenders == []

    from src.application.tool_execution import build_tool_manifest

    manifest = build_tool_manifest()
    manifest_text = json.dumps(manifest, ensure_ascii=False)
    for forbidden in (
        "copilot",
        "Copilot",
        "SceneManifest",
        "ExecutionContract",
        "SceneDefinition",
        "SceneCatalog",
        "scene_name",
        "answer guard",
        "answer_guard",
    ):
        assert forbidden not in manifest_text
    assert manifest["launcher"]["command"] == [
        "./om-agent",
        "run",
        "--tool",
        "<tool-name>",
        "--input-json",
        "<json>",
    ]


def test_copilot_cli_entry_wires_service_to_host_without_agent_internals() -> None:
    cli_text = (ROOT / "src" / "interfaces" / "cli" / "copilot_ops.py").read_text(encoding="utf-8")
    tree = ast.parse(cli_text)
    forbidden_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in {
                "src.application.copilot.agent",
                "src.application.copilot.engine",
                "src.application.copilot.model_client",
                "src.application.copilot.model_decider",
                "src.application.copilot.result_projection",
                "src.application.copilot.scene",
                "src.application.copilot.tools",
            }:
                forbidden_modules.append(module)

    assert forbidden_modules == []
    assert "from src.application.copilot.local_harness import run_local_request" in cli_text
    assert "from src.application.copilot.service import prepare_contract" not in cli_text
    assert "from src.application.copilot.host import run_contract" not in cli_text
    assert "CopilotRequest(" in cli_text
    assert "ExecutionContract" not in cli_text
    assert "return run_local_request(" in cli_text
    assert "model_turn_json=model_turn_json" in cli_text
    assert "model_config_json" not in cli_text.split("CopilotRequest(", 1)[1].split(")", 1)[0]
    assert "assistant_config_path" not in cli_text.split("CopilotRequest(", 1)[1].split(")", 1)[0]
    assert "model_turn_json" not in cli_text.split("CopilotRequest(", 1)[1].split(")", 1)[0]
    assert "use_default_assistant_config" not in cli_text
    assert "build_action_model" not in cli_text
    assert "ModelActionDecider" not in cli_text
    assert "run_engine(" not in cli_text
    assert 'run.add_argument("--scene"' not in cli_text
    assert 'run.add_argument("--model-action-json-file"' not in cli_text
    assert 'eval_cmd.add_argument("--scene", required=True)' not in cli_text
    assert '"--model-turn-json-file"' in cli_text
    assert 'default="分析 2026-06 的期权操作有没有不合理，需要优化的地方"' not in cli_text


def test_copilot_local_harness_is_phase1_composition_only() -> None:
    harness_path = ROOT / "src" / "application" / "copilot" / "local_harness.py"
    harness_text = harness_path.read_text(encoding="utf-8")
    imports = set(_imported_modules(harness_path))

    assert "src.application.copilot.service" in imports
    assert "src.application.copilot.host" in imports
    assert "src.application.copilot.model_config" in imports
    assert "src.application.copilot.model_client" not in imports
    assert "src.application.copilot.conversation_memory" not in imports
    assert "src.application.copilot.model_decider" not in imports
    assert "src.application.copilot.scene" not in imports
    assert "src.application.copilot.tools" not in imports
    assert "src.application.copilot.engine" not in imports
    assert "src.application.agent_tool_registry" not in imports
    assert "src.application.tool_execution" not in imports
    assert "monthly_option_review" not in harness_text
    assert "operations_diagnostics" not in harness_text
    assert "candidate_filter_diagnostics" not in harness_text
    assert "analysis_query" not in harness_text
    assert "PiModelSettings" in harness_text
    assert "def _resolve_pi_model(" in harness_text


def test_copilot_has_no_retired_python_agent_runtime() -> None:
    copilot_root = ROOT / "src" / "application" / "copilot"
    retired_modules = {
        "src.application.copilot.agent",
        "src.application.copilot.conversation_memory",
        "src.application.copilot.engine",
        "src.application.copilot.model_client",
    }
    assert not any((copilot_root / f"{name.rsplit('.', 1)[-1]}.py").exists() for name in retired_modules)

    offenders: list[str] = []
    for path in sorted(copilot_root.glob("*.py")):
        relative = str(path.relative_to(ROOT))
        for module in _imported_modules(path):
            if module in retired_modules:
                offenders.append(f"{relative}:import:{module}")
        for line in path.read_text(encoding="utf-8").splitlines():
            if "run_engine(" in line or "build_model_runner(" in line:
                offenders.append(f"{relative}:call:{line.strip()}")
            if "OM_" in line and ("ENGINE" in line or "LEGACY" in line):
                offenders.append(f"{relative}:runtime-selector:{line.strip()}")

    assert offenders == []


def test_copilot_internal_layers_do_not_reverse_dayu_dependencies() -> None:
    copilot_root = ROOT / "src" / "application" / "copilot"
    forbidden_by_file = {
        "agent.py": {
            "src.application.copilot.host",
            "src.application.copilot.service",
            "src.application.copilot.engine",
            "src.application.copilot.tools",
            "src.application.copilot.scene",
            "src.application.copilot.result_projection",
            "src.application.copilot.result_admission",
            "src.application.copilot.event_store",
            "src.application.agent_tool_registry",
            "src.application.tool_execution",
        },
        "engine.py": {
            "src.application.copilot.host",
            "src.application.copilot.service",
            "src.application.copilot.scene",
            "src.application.copilot.tools",
            "src.application.copilot.model_decider",
            "src.application.agent_tool_registry",
            "src.application.tool_execution",
        },
        "model_decider.py": {
            "src.application.copilot.host",
            "src.application.copilot.service",
            "src.application.copilot.scene",
            "src.application.copilot.tools",
            "src.application.copilot.engine",
            "src.application.agent_tool_registry",
            "src.application.tool_execution",
        },
        "model_client.py": {
            "src.application.copilot.host",
            "src.application.copilot.service",
            "src.application.copilot.scene",
            "src.application.copilot.tools",
            "src.application.copilot.engine",
            "src.application.agent_tool_registry",
            "src.application.tool_execution",
            "src.application.assistant",
        },
        "result_projection.py": {
            "src.application.copilot.host",
            "src.application.copilot.service",
            "src.application.copilot.scene",
            "src.application.copilot.tools",
            "src.application.copilot.engine",
            "src.application.copilot.agent",
            "src.application.agent_tool_registry",
            "src.application.tool_execution",
        },
        "result_admission.py": {
            "src.application.copilot.host",
            "src.application.copilot.service",
            "src.application.copilot.scene",
            "src.application.copilot.tools",
            "src.application.copilot.engine",
            "src.application.copilot.agent",
            "src.application.agent_tool_registry",
            "src.application.tool_execution",
        },
        "event_store.py": {
            "src.application.copilot.host",
            "src.application.copilot.service",
            "src.application.copilot.scene",
            "src.application.copilot.tools",
            "src.application.copilot.engine",
            "src.application.copilot.agent",
            "src.application.agent_tool_registry",
            "src.application.tool_execution",
        },
    }

    offenders: list[str] = []
    for filename, forbidden_modules in forbidden_by_file.items():
        path = copilot_root / filename
        if not path.exists():
            continue
        imports = _imported_modules_with_from_names(path)
        for module in imports:
            for forbidden in forbidden_modules:
                if module == forbidden or module.startswith(f"{forbidden}."):
                    offenders.append(f"{filename}:{module}")

    assert offenders == []


def test_copilot_keeps_generic_answer_quality_model_turn_fixtures() -> None:
    fixture_root = ROOT / "tests" / "fixtures" / "copilot"

    for name in (
        "opening_candidate_snapshot_diagnostics_model_turns.json",
        "close_advice_notification_diagnostics_model_turns.json",
        "june_income_attribution_model_turns.json",
        "current_option_exposure_model_turns.json",
    ):
        assert (fixture_root / name).is_file(), name


def test_tool_contracts_do_not_carry_planner_routing_metadata() -> None:
    from dataclasses import fields

    from src.application.agent_tools.base import AgentTool
    from src.application.assistant.tool_bindings import AssistantToolBinding

    binding_fields = {field.name for field in fields(AssistantToolBinding)}
    tool_fields = {field.name for field in fields(AgentTool)}
    assert "description" not in binding_fields
    assert "input_schema" not in binding_fields
    assert "output_contract" not in binding_fields
    for field_name in ("planner_notes", "planner_semantics", "planner_semantics_resolver", "copilot_notes"):
        assert field_name not in binding_fields
        assert field_name not in tool_fields


def test_retired_ai_advice_handoff_fields_are_absent_from_runtime_contracts() -> None:
    from dataclasses import fields
    from inspect import signature

    from src.application.daily_decision_brief_service import (
        assemble_daily_decision_brief,
        assemble_daily_decision_briefs,
    )
    from src.application.tick_account_execution import TickAccountExecutionOutcome
    from src.application.tick_notification_flow import TickNotificationRequest

    retired_brief_parameters = {
        "prepared_portfolio_distribution",
        "portfolio_distribution_unavailable_reason",
        "prepared_option_positions_context",
        "option_positions_unavailable_reason",
    }
    retired_tick_fields = {
        "opening_candidate_snapshot_by_account",
        "opening_candidate_snapshot_unavailable_by_account",
        "prepared_portfolio_distribution_by_account",
        "prepared_portfolio_distribution_artifact_path_by_account",
        "prepared_portfolio_distribution_artifact_sha256_by_account",
        "prepared_portfolio_distribution_status_by_account",
        "prepared_option_positions_context_by_account",
        "prepared_option_positions_context_unavailable_by_account",
        "prepared_option_positions_context_manifest_by_account",
        "prepared_option_positions_context_manifest_sha256_by_account",
    }

    for assembler in (
        assemble_daily_decision_brief,
        assemble_daily_decision_briefs,
    ):
        assert retired_brief_parameters.isdisjoint(signature(assembler).parameters)
    assert retired_tick_fields.isdisjoint(
        field.name for field in fields(TickNotificationRequest)
    )
    assert retired_tick_fields.isdisjoint(
        field.name for field in fields(TickAccountExecutionOutcome)
    )


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


def test_feishu_ws_transport_does_not_own_allowlist_policy() -> None:
    ws_path = ROOT / "src" / "application" / "inbound" / "feishu_ws.py"
    adapter_path = ROOT / "src" / "application" / "inbound" / "feishu.py"
    ws_text = ws_path.read_text(encoding="utf-8")
    adapter_text = adapter_path.read_text(encoding="utf-8")
    ws_imports = set(_imported_modules_with_from_names(ws_path))
    adapter_imports = set(_imported_modules_with_from_names(adapter_path))

    assert "_parse_allowed_entries" not in ws_text
    assert "_parse_allowed_entries" not in adapter_text
    assert "OM_FEISHU_BOT_ALLOWED_OPEN_IDS" not in adapter_text
    assert "OM_FEISHU_BOT_USER_OPEN_ID" not in adapter_text

    assert "src.application.assistant.policy" not in ws_imports
    assert "src.application.assistant.policy.check_sender_allowed" in adapter_imports
    assert "check_sender_allowed(" in adapter_text
