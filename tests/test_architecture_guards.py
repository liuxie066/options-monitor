from __future__ import annotations

import ast
import inspect
import re
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


def test_old_agent_loop_copilot_modules_are_removed() -> None:
    removed = (
        "agent_loop.py",
        "action_policy.py",
        "action_safety.py",
        "copilot.py",
        "task_profiles.py",
        "model_events.py",
        "model_evidence.py",
        "coverage_verifier.py",
        "task_completion.py",
        "llm_reply.py",
        "context_eval.py",
        "conversation_context.py",
        "context_projection.py",
        "context_validation.py",
    )

    existing = [name for name in removed if (ROOT / "src" / "application" / "assistant" / name).exists()]
    assert existing == []


def test_old_freeform_session_builder_is_removed() -> None:
    session_source = (ROOT / "src" / "application" / "assistant" / "session.py").read_text(encoding="utf-8")

    assert "def build_agent_session_snapshot(" not in session_source
    assert "build_agent_session_snapshot" not in session_source


def test_old_freeform_eval_fixtures_are_removed() -> None:
    removed = (
        "assistant_agent_eval.jsonl",
        "assistant_nlu_eval.jsonl",
        "assistant_trace_route_samples.jsonl",
        "assistant_context_projection.jsonl",
        "assistant_context_validation.jsonl",
        "assistant_context_scenarios.jsonl",
    )

    existing = [name for name in removed if (ROOT / "tests" / "fixtures" / name).exists()]
    assert existing == []
    assert not (ROOT / "tests" / "test_assistant_nlu_eval.py").exists()
    assert not (ROOT / "tests" / "test_assistant_context_projection.py").exists()
    assert not (ROOT / "tests" / "test_assistant_context_validation.py").exists()


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


def test_copilot_shared_runtime_does_not_hardcode_monthly_review_scene() -> None:
    shared_runtime_files = [
        "agent.py",
        "contracts.py",
        "engine.py",
        "host.py",
        "local_harness.py",
        "model_client.py",
        "model_config.py",
        "model_decider.py",
        "result_admission.py",
        "result_projection.py",
        "rendering.py",
        "safety_policy.py",
        "safety_text.py",
        "service.py",
    ]
    forbidden_tokens = (
        "monthly_option_review",
        "june_option_review",
        "june_income_attribution",
        "current_option_exposure_model_ready",
        "candidate_filter_diagnostics_model_ready",
        "close_advice_notification_diagnostics_model_ready",
        "复盘维度",
        "REVIEW_DIMENSION_LABELS",
    )
    offenders: list[str] = []
    copilot_root = ROOT / "src" / "application" / "copilot"
    for name in shared_runtime_files:
        path = copilot_root / name
        text = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}:{token}")

    assert offenders == []


def test_copilot_phase2_answer_synthesis_is_not_monthly_review_only() -> None:
    from src.application.copilot.scene import SCENE_CATALOG

    scenes = {scene.name: scene for scene in SCENE_CATALOG}
    synthesis_scenes = {name for name, scene in scenes.items() if scene.requires_answer_synthesis}

    assert "monthly_option_review" not in scenes
    assert {"monthly_income_attribution", "current_option_exposure"} <= synthesis_scenes
    assert all(not scene.requires_recommendations for scene in scenes.values())
    assert scenes["operations_diagnostics"].fixture_ids
    assert not scenes["operations_diagnostics"].requires_recommendations


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
    assert (
        "return run_local_request(\n"
        "        request,\n"
        "        reference_year=_reference_year(),\n"
        "        model_config_json=model_config_json,\n"
        "        assistant_config_path=assistant_config_path,\n"
        "        model_action_json=model_action_json,\n"
        "    )"
    ) in cli_text
    assert "model_config_json" not in cli_text.split("CopilotRequest(", 1)[1].split(")", 1)[0]
    assert "assistant_config_path" not in cli_text.split("CopilotRequest(", 1)[1].split(")", 1)[0]
    assert "model_action_json" not in cli_text.split("CopilotRequest(", 1)[1].split(")", 1)[0]
    assert "use_default_assistant_config" not in cli_text
    assert "build_action_model" not in cli_text
    assert "ModelActionDecider" not in cli_text
    assert "run_engine(" not in cli_text
    assert 'run.add_argument("--scene"' not in cli_text
    assert 'run.add_argument("--model-action-json-file"' not in cli_text
    assert 'eval_cmd.add_argument("--scene", required=True)' in cli_text
    assert 'eval_model.add_argument(\n        "--model-action-json-file",' in cli_text
    assert 'eval_cmd.add_argument("--text", default="请根据 eval fixture 回答这个只读问题")' in cli_text
    assert 'default="分析 2026-06 的期权操作有没有不合理，需要优化的地方"' not in cli_text


def test_copilot_cli_does_not_accept_runtime_environment_paths() -> None:
    cli_text = (ROOT / "src" / "interfaces" / "cli" / "copilot_ops.py").read_text(encoding="utf-8")

    assert "--env-file" not in cli_text
    assert "--no-local-env-file" not in cli_text
    assert "--config-path" not in cli_text
    assert "--assistant-config" in cli_text


def test_channel_copilot_gate_uses_only_facade_not_host_internals() -> None:
    checked_roots = [
        ROOT / "src" / "application" / "channels",
        ROOT / "src" / "application" / "inbound",
    ]
    checked_files = [
        ROOT / "src" / "interfaces" / "cli" / "assistant_ops.py",
        ROOT / "src" / "interfaces" / "cli" / "channel_ops.py",
        ROOT / "src" / "interfaces" / "cli" / "inbound_ops.py",
        ROOT / "src" / "application" / "assistant" / "runtime.py",
        ROOT / "src" / "application" / "assistant" / "router.py",
    ]

    offenders: list[str] = []
    for root in checked_roots:
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "src.application.copilot" in text or "handle_copilot_request" in text or "CopilotRequest" in text:
                offenders.append(str(path.relative_to(ROOT)))
    for path in checked_files:
        text = path.read_text(encoding="utf-8")
        if "src.application.copilot" in text or "handle_copilot_request" in text or "CopilotRequest" in text:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []

    action_path = ROOT / "src" / "application" / "assistant" / "action.py"
    action_imports = set(_imported_modules(action_path))
    assert "src.application.copilot.channel_facade" in action_imports
    assert "src.application.copilot.host" not in action_imports
    assert "src.application.copilot.agent" not in action_imports
    assert "src.application.copilot.engine" not in action_imports
    assert "src.application.copilot.tools" not in action_imports
    assert "src.application.copilot.service" not in action_imports


def test_channel_copilot_facade_does_not_reenter_local_request_harness() -> None:
    channel_text = (ROOT / "src" / "application" / "copilot" / "channel_facade.py").read_text(encoding="utf-8")

    assert "run_local_request" not in channel_text
    assert "run_prepared_contract" in channel_text
    assert "月度复盘" not in channel_text
    assert "monthly_option_review" not in channel_text


def test_channel_copilot_facade_catches_service_prepare_errors() -> None:
    channel_text = (ROOT / "src" / "application" / "copilot" / "channel_facade.py").read_text(encoding="utf-8")

    assert "except Exception:" in channel_text
    assert "_channel_prepare_failed(request)" in channel_text
    assert '"channel_prepare_contract_failed"' in channel_text


def test_copilot_tools_do_not_define_parallel_tool_allowlist() -> None:
    from src.application.copilot.scene import SCENE_CATALOG
    from src.application.copilot.tools import TOOL_VIEWS

    tools_text = (ROOT / "src" / "application" / "copilot" / "tools.py").read_text(encoding="utf-8")
    scene_text = (ROOT / "src" / "application" / "copilot" / "scene.py").read_text(encoding="utf-8")
    host_text = (ROOT / "src" / "application" / "copilot" / "host.py").read_text(encoding="utf-8")
    fixtures_text = (ROOT / "src" / "application" / "copilot" / "eval_fixtures.py").read_text(encoding="utf-8")
    scene_tools = {tool_name for scene in SCENE_CATALOG for tool_name in scene.allowed_tools}

    assert set(TOOL_VIEWS) == scene_tools
    assert "READ_TOOL_NAMES" not in tools_text
    assert "MONTHLY_REVIEW_ANALYSIS_VIEWS" not in tools_text
    assert "tool_static_payloads" in scene_text
    assert "tool_static_payloads" in host_text
    assert "class AgentToolView" in tools_text
    assert "TOOL_VIEWS" in tools_text
    assert "static_payload:" not in tools_text
    assert "static_payload=" not in tools_text
    assert "get_tool_definition(" in tools_text
    assert "definition.is_pure_read()" in tools_text
    assert "observation_summary" in tools_text
    assert "compact_summary: Any" not in tools_text
    assert "compact_summary=" not in tools_text
    assert "evidence_available" in tools_text
    assert "missing_evidence" in tools_text
    assert "evidence_ok: Any" not in tools_text
    assert "evidence_ok=lambda" not in tools_text
    assert "missing_data: Any" not in tools_text
    assert "missing_data=lambda" not in tools_text
    assert "definition.resolve_output_contract(" in tools_text
    assert '"output_contract": _resolved_output_contract_preview(definition, description_payload)' in tools_text
    assert '"data": data' not in tools_text
    assert '"data": response.get("data")' not in tools_text
    assert '"value_preview": _value_preview' in tools_text
    assert '"error": error' not in tools_text
    assert '"has_message"' in tools_text
    assert "summarize:" not in tools_text
    assert "_summarize_" not in tools_text
    assert 'error.get("message")' not in tools_text
    assert "warning_count=" in tools_text
    assert "join(str(item)" not in tools_text
    assert "if tool_name ==" not in tools_text
    assert "elif tool_name ==" not in tools_text
    assert "fixture_observations" not in tools_text
    assert "june_option_review_basic" not in tools_text
    assert "def fixture_observations(" not in scene_text
    assert "eval-only 月度复盘发现" not in scene_text
    assert "def fixture_observations(" in fixtures_text
    assert "june_option_review_basic" not in fixtures_text
    assert "from src.application.copilot.eval_fixtures import" not in host_text
    assert "fixture_observations_loader" in host_text
    assert "fixture_synthesis_policy" in host_text
    assert "fixture_observations_loader=fixture_observations" in (
        ROOT / "src" / "application" / "copilot" / "local_harness.py"
    ).read_text(encoding="utf-8")


def test_copilot_tool_views_are_observation_compactors_not_answer_recipes() -> None:
    tools_text = (ROOT / "src" / "application" / "copilot" / "tools.py").read_text(encoding="utf-8")

    forbidden_answer_terms = (
        "结论",
        "建议",
        "优化",
        "合理",
        "不合理",
        "recommend",
        "recommendation",
        "should ",
    )

    offenders = [term for term in forbidden_answer_terms if term in tools_text]
    assert offenders == []

    forbidden_row_aggregate_terms = (
        "from collections import Counter",
        "def _top_counts(",
        "def _sum_number(",
        "def _sum_first_number(",
        "def _mapping_counts(",
        "contracts_open_total",
        "net_income_cny_total",
        "premium_symbols",
        "rows_by_view=",
    )

    aggregate_offenders = [term for term in forbidden_row_aggregate_terms if term in tools_text]
    assert aggregate_offenders == []


def test_copilot_host_does_not_branch_on_business_scene_names() -> None:
    host_text = (ROOT / "src" / "application" / "copilot" / "host.py").read_text(encoding="utf-8")

    assert "monthly_option_review" not in host_text
    assert "operations_diagnostics" not in host_text
    assert "_build_manifest" not in host_text
    assert "build_scene_manifest(" in host_text
    assert "scene_policy_rejection_reason(" in host_text
    assert 'f"结论' not in host_text
    assert "：{rejection}" not in host_text
    assert 'contract.policy.get("allowed_tools")' not in host_text
    assert 'contract.policy.get("requires_answer_synthesis")' not in host_text
    assert "for tool_name in manifest.allowed_tools" not in host_text
    assert "requires_answer_synthesis=_manifest_requires_answer_synthesis(manifest)" in host_text


def test_copilot_scene_catalog_has_single_owner() -> None:
    contracts_text = (ROOT / "src" / "application" / "copilot" / "contracts.py").read_text(encoding="utf-8")
    scene_text = (ROOT / "src" / "application" / "copilot" / "scene.py").read_text(encoding="utf-8")
    service_text = (ROOT / "src" / "application" / "copilot" / "service.py").read_text(encoding="utf-8")

    assert "SCENE_CATALOG" in scene_text
    assert "SceneDefinition(" in scene_text
    assert "CapabilityHintDefinition(" in scene_text
    assert "activation_terms" in contracts_text
    assert "activation_reason" in contracts_text
    assert "output_schema:" in contracts_text
    assert "activation_pattern" not in contracts_text
    assert "message_pattern" not in contracts_text
    assert "message_reason" not in contracts_text
    assert "activation_terms=" in scene_text
    assert "activation_reason=" in scene_text
    assert "activation_pattern=" not in scene_text
    assert "message_pattern=" not in scene_text
    assert "message_reason=" not in scene_text
    assert "def capability_hint_definitions(" in scene_text
    assert "contract missing required scope" in scene_text
    assert "contract missing capability scope" in scene_text
    assert "missing_required_scope(scene, contract.input)" in scene_text
    assert "missing_capability_scope(scene, requested_capabilities, contract.input)" in scene_text
    assert "class SceneSelectionDecision" in scene_text
    assert "def select_scene(" in scene_text
    assert "-> SceneSelectionDecision" in scene_text
    assert "output_schema=_scene_output_schema(scene)" in scene_text
    assert "output_schema={\"type\": \"AnswerReport\"}" not in scene_text
    assert '"allowed_tools": list(scene.allowed_tools)' not in scene_text
    assert '"allowed_environments": list(scene.environments)' not in scene_text
    assert '"phase_readiness": scene.phase_readiness' not in scene_text
    assert '"mock_environments": list(scene.mock_environments)' not in scene_text
    assert '"fixture_ids": list(scene.fixture_ids)' not in scene_text
    assert '_optional_policy_tuple(contract.policy, "allowed_tools")' in scene_text
    assert '_optional_policy_tuple(contract.policy, "allowed_environments")' in scene_text
    assert "lower_priority_match" not in scene_text
    assert "ambiguous_scene_match" in scene_text
    assert "Start the answer" not in scene_text
    assert "Start the answer with a conclusion" not in scene_text
    assert "Select the next action using only allowed read-only OM tools." in scene_text
    assert "SCENE_CATALOG" not in service_text
    assert "SceneDefinition" not in service_text
    assert "SceneSelectionDecision" not in service_text
    assert "policy_for_scene" not in service_text
    assert "rejected_scenes" not in service_text
    assert "candidate_scenes" not in service_text
    assert "monthly_option_review" not in service_text
    assert "operations_diagnostics" not in service_text
    assert "candidate_filter_diagnostics" not in service_text
    assert "月度期权复盘" not in service_text
    assert "NVDA 为什么" not in service_text
    forbidden_service_business_terms = (
        "runtime_status",
        "candidate_filter",
        "monthly_income",
        "analysis_query",
        "close_advice",
        "option_positions",
        "期权",
        "候选",
        "筛选",
        "收益",
        "风险",
        "不合理",
        "优化",
    )
    service_business_offenders = [term for term in forbidden_service_business_terms if term in service_text]
    assert service_business_offenders == []


def test_copilot_scene_guidance_is_not_an_answer_template() -> None:
    from src.application.copilot.scene import SCENE_CATALOG

    forbidden_guidance_terms = (
        "结论",
        "建议",
        "不合理",
        "优化",
        "recommend",
        "recommendation",
        "finding",
        "evidence_refs",
        "account=",
        "symbol=",
        "premium=",
        "realized=",
        "assignment=",
    )
    forbidden_tool_names = (
        "runtime_status",
        "candidate_filter_explain",
        "analysis_catalog",
        "analysis_query",
        "monthly_income_report",
        "option_positions_read",
        "close_advice_read",
    )

    offenders: list[str] = []
    for scene in SCENE_CATALOG:
        guidance = "\n".join(scene.task_guidance)
        for term in forbidden_guidance_terms + forbidden_tool_names:
            if term.lower() in guidance.lower():
                offenders.append(f"{scene.name}:{term}")

    assert offenders == []


def test_copilot_capability_activation_hints_are_not_answer_templates() -> None:
    from src.application.copilot.scene import SCENE_CATALOG

    forbidden_activation_terms = (
        "结论",
        "建议",
        "不合理",
        "合理",
        "优化",
        "recommend",
        "recommendation",
        "should",
        "finding",
        "evidence",
        "account=",
        "symbol=",
        "premium=",
        "realized=",
        "assignment=",
    )

    offenders: list[str] = []
    for scene in SCENE_CATALOG:
        for hint in scene.capability_hints:
            flattened_terms = [
                term
                for group in hint.activation_terms
                for term in group
                if isinstance(term, str) and term.strip()
            ]
            if not flattened_terms:
                offenders.append(f"{scene.name}:{hint.capability}:missing_activation_terms")
                continue
            for term in forbidden_activation_terms:
                if any(term.lower() in item.lower() for item in flattened_terms):
                    offenders.append(f"{scene.name}:{hint.capability}:{term}")

    assert offenders == []


def test_copilot_service_does_not_inline_request_understanding_regexes() -> None:
    service_text = (ROOT / "src" / "application" / "copilot" / "service.py").read_text(encoding="utf-8")
    understanding_text = (ROOT / "src" / "application" / "copilot" / "request_understanding.py").read_text(
        encoding="utf-8"
    )
    safety_text = (ROOT / "src" / "application" / "copilot" / "safety_policy.py").read_text(encoding="utf-8")

    assert "understand_request(" in service_text
    assert "evaluate_safety(" in service_text
    assert "re.compile" not in service_text
    assert "_infer_capabilities" not in service_text
    assert "_CANDIDATE_HINT_RE" not in service_text
    assert "_WRITE_LIKE_RE" not in service_text
    assert "capability_hint_definitions(" in service_text
    assert "capability_hints=capability_hint_definitions()" in service_text
    assert "src.application.copilot.scene" not in understanding_text
    assert "_CANDIDATE_HINT_RE" not in understanding_text
    assert "_RUNTIME_HINT_RE" not in understanding_text
    assert "_MONTHLY_REVIEW_RE" not in understanding_text
    assert "phase1_candidate_filter_hint" not in understanding_text
    assert "phase1_runtime_hint" not in understanding_text
    assert "phase1_monthly_option_review_hint" not in understanding_text
    assert "thin_request_understanding" in understanding_text
    assert "_matches_activation_terms(" in understanding_text
    assert "activation_terms" in understanding_text
    assert "activation_pattern" not in understanding_text
    assert "re.search" not in understanding_text
    assert "select_scene(" not in understanding_text
    assert "SceneDefinition" not in understanding_text
    assert "date.today" not in understanding_text
    assert "from datetime import date" not in understanding_text
    assert "reference_year" in understanding_text
    assert "_WRITE_LIKE_RE" not in understanding_text
    assert "write_like" not in understanding_text
    assert "safety_hits" not in understanding_text
    assert "def evaluate_safety(" in safety_text
    assert "class SafetyDecision" in safety_text
    assert "class SafetyRule" in safety_text
    assert "SAFETY_RULES" in safety_text
    assert "_is_read_like_mutation_question" in safety_text
    assert "是否" not in safety_text
    assert "要不要" not in safety_text
    assert "需不需要" not in safety_text
    assert "should" not in safety_text
    assert "_WRITE_LIKE_RE" not in safety_text
    assert "src.application.copilot.scene" not in safety_text
    assert "src.application.copilot.host" not in safety_text
    assert "src.application.copilot.agent" not in safety_text
    assert "src.application.copilot.tools" not in safety_text
    assert "select_scene(" not in safety_text
    assert "tool_name" not in safety_text
    for rule_name in (
        "config_mutation_request",
        "notification_send_request",
        "broker_trade_request",
        "release_or_service_change_request",
        "state_mutation_request",
    ):
        assert rule_name in safety_text


def test_copilot_safety_policy_is_not_business_intent_routing() -> None:
    safety_text = (ROOT / "src" / "application" / "copilot" / "safety_policy.py").read_text(encoding="utf-8")

    forbidden_business_terms = (
        "monthly_option_review",
        "operations_diagnostics",
        "candidate_filter",
        "runtime_status",
        "monthly_income_report",
        "analysis_query",
        "close_advice",
        "option_positions",
        "期权",
        "复盘",
        "候选",
        "筛选",
        "收益",
        "风险",
        "NVDA",
        "0700",
    )

    offenders = [term for term in forbidden_business_terms if term in safety_text]
    assert offenders == []


def test_copilot_service_and_host_keep_dayu_import_boundary() -> None:
    service_path = ROOT / "src" / "application" / "copilot" / "service.py"
    host_path = ROOT / "src" / "application" / "copilot" / "host.py"
    service_text = service_path.read_text(encoding="utf-8")
    host_text = host_path.read_text(encoding="utf-8")
    service_imports = set(_imported_modules(service_path))
    host_imports = set(_imported_modules(host_path))

    service_forbidden = {
        "src.application.copilot.host",
        "src.application.copilot.agent",
        "src.application.copilot.engine",
        "src.application.copilot.event_store",
        "src.application.copilot.model_decider",
        "src.application.copilot.result_admission",
        "src.application.copilot.result_projection",
        "src.application.copilot.tools",
        "src.application.agent_tool_registry",
        "src.application.tool_execution",
    }
    host_forbidden = {
        "src.application.copilot.service",
        "src.application.copilot.request_understanding",
        "src.application.copilot.model_client",
        "src.application.copilot.model_decider",
        "src.application.agent_tool_registry",
        "src.application.tool_execution",
    }

    assert sorted(service_imports & service_forbidden) == []
    assert sorted(host_imports & host_forbidden) == []
    assert "HostRunner" not in service_text
    assert "host_runner" not in service_text
    assert "handle_copilot_request" not in service_text
    assert "from src.application.copilot.host import run_contract" not in service_text
    assert "def handle_request(" not in service_text
    assert "run_contract(" not in service_text
    assert "def prepare_contract(" in service_text
    assert "date.today" not in service_text
    assert "reference_year: int" in service_text
    assert "from src.application.copilot.scene import" in service_text
    assert "src.application.copilot.scene" in host_imports
    assert "build_scene_manifest(" in host_text
    assert "model_config_json" not in host_text
    assert "build_action_model" not in host_text


def test_copilot_local_harness_is_phase1_composition_only() -> None:
    harness_path = ROOT / "src" / "application" / "copilot" / "local_harness.py"
    harness_text = harness_path.read_text(encoding="utf-8")
    imports = set(_imported_modules(harness_path))

    assert "src.application.copilot.service" in imports
    assert "src.application.copilot.host" in imports
    assert "src.application.copilot.model_config" in imports
    assert "src.application.copilot.model_client" in imports
    assert "src.application.copilot.model_decider" in imports
    assert "src.application.copilot.scene" not in imports
    assert "src.application.copilot.tools" not in imports
    assert "src.application.copilot.engine" not in imports
    assert "src.application.agent_tool_registry" not in imports
    assert "src.application.tool_execution" not in imports
    assert "monthly_option_review" not in harness_text
    assert "operations_diagnostics" not in harness_text
    assert "candidate_filter_diagnostics" not in harness_text
    assert "analysis_query" not in harness_text


def test_copilot_cli_does_not_auto_load_default_assistant_model_config() -> None:
    main_text = (ROOT / "src" / "interfaces" / "cli" / "main.py").read_text(encoding="utf-8")
    cli_text = (ROOT / "src" / "interfaces" / "cli" / "copilot_ops.py").read_text(encoding="utf-8")
    harness_text = (ROOT / "src" / "application" / "copilot" / "local_harness.py").read_text(encoding="utf-8")

    assert 'setattr(args, "use_default_assistant_config", True)' not in main_text
    assert "use_default_assistant_config" not in main_text
    assert "use_default_assistant_config" not in cli_text
    assert "use_default_assistant_config" not in harness_text


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
        imports = _imported_modules_with_from_names(copilot_root / filename)
        for module in imports:
            for forbidden in forbidden_modules:
                if module == forbidden or module.startswith(f"{forbidden}."):
                    offenders.append(f"{filename}:{module}")

    assert offenders == []


def test_copilot_event_store_is_host_lifecycle_only() -> None:
    host_text = (ROOT / "src" / "application" / "copilot" / "host.py").read_text(encoding="utf-8")
    event_store_text = (ROOT / "src" / "application" / "copilot" / "event_store.py").read_text(encoding="utf-8")

    assert "CopilotEventLog" in host_text
    assert "AppEvent(" not in host_text
    assert "utc_now_iso" not in host_text
    assert "AppEvent(" in event_store_text
    assert "src.application.assistant" not in event_store_text
    assert "monthly_option_review" not in event_store_text
    assert "operations_diagnostics" not in event_store_text
    assert "if self._final_recorded:" in event_store_text
    assert "def _append(" in event_store_text


def test_copilot_result_admission_is_not_answer_guard() -> None:
    host_text = (ROOT / "src" / "application" / "copilot" / "host.py").read_text(encoding="utf-8")
    admission_text = (ROOT / "src" / "application" / "copilot" / "result_admission.py").read_text(encoding="utf-8")

    assert "admit_result_with_decision(" in host_text
    assert "result_admission_rejected" in host_text
    assert "def _admit" not in host_text
    assert "src.application.assistant" not in admission_text
    assert "answer_guard" not in admission_text
    assert "answer_verifier" not in admission_text
    assert "semantic" not in admission_text
    assert "llm" not in admission_text.lower()
    assert "import re" not in admission_text
    assert "raw_grouped_rows" not in admission_text
    assert "_looks_like" not in admission_text
    assert "from src.application.copilot.safety_text import contains_forbidden_external_action_claim" in admission_text
    assert "FORBIDDEN_MUTATION_CLAIMS" not in admission_text
    assert "account=" not in admission_text
    assert "symbol=" not in admission_text
    assert "monthly_option_review" not in admission_text
    assert "operations_diagnostics" not in admission_text
    assert "user_response=" not in admission_text


def test_copilot_result_projection_does_not_own_tools_or_old_assistant() -> None:
    projection_text = (ROOT / "src" / "application" / "copilot" / "result_projection.py").read_text(encoding="utf-8")

    assert "src.application.assistant" not in projection_text
    assert "ExecutionContract" not in projection_text
    assert "contract.input" not in projection_text
    assert "contract.policy" not in projection_text
    assert "uses_mock_observations" not in projection_text
    assert "agent_tool_registry" not in projection_text
    assert "tool_execution" not in projection_text
    assert "src.infrastructure" not in projection_text
    assert "openai" not in projection_text.lower()
    assert "src.application.copilot.answer_quality" not in projection_text
    assert "has_raw_field_dump" not in projection_text
    assert "has_conflicting_snapshot_view_use" not in projection_text
    assert "has_conflicting_valid_empty_result_use" not in projection_text
    assert "def _has_raw_field_dump(" not in projection_text
    assert "def _raw_assignment_token_count(" not in projection_text
    assert "def _receipt_like_row_dump(" not in projection_text
    assert "monthly_option_review" not in projection_text
    assert "operations_diagnostics" not in projection_text
    assert "月度" not in projection_text
    assert "期权" not in projection_text
    assert "诊断" not in projection_text
    assert "分析完成" not in projection_text
    assert "完成初步" not in projection_text
    assert "已基于" not in projection_text
    assert '"data": item.get("data")' not in projection_text
    assert '"value_preview": _observation_value_preview(item)' in projection_text
    assert "def _observation_value_preview(" in projection_text
    assert '"error": item.get("error")' not in projection_text
    assert '"error": _error_preview(item.get("error"))' in projection_text
    assert "missing.extend(_missing_data_preview(item_missing))" in projection_text
    assert "missing.extend(str(value)" not in projection_text
    assert "user_response=" not in projection_text
    assert "已尝试检查" not in projection_text
    assert "MAX_OBSERVATION_SUMMARY_CHARS" in projection_text
    assert '"summary": _summary_preview(item.get("summary"))' in projection_text
    assert '"summary": str(item.get("summary") or "")' not in projection_text
    assert '"facts_omitted"' in projection_text
    assert '"evidence_context": _string_map_preview(item.get("evidence_context"))' in projection_text
    assert "def _string_map_preview(" in projection_text
    assert "def _bounded_count(" in projection_text
    assert "def _missing_data(eval_only: bool" in projection_text
    assert "def _missing_data(contract" not in projection_text
    assert "eval_synthesis_missing = _eval_model_synthesis_missing(" in projection_text
    assert "suppress_observation_findings = synthesis_missing or eval_synthesis_missing" in projection_text
    assert "findings=[] if suppress_observation_findings else _observation_findings(observations)" in projection_text
    assert "def _eval_model_synthesis_missing(" in projection_text
    assert "def _observation_findings(" in projection_text
    assert 'raw_report.get("attempted_checks")' not in projection_text
    assert "def _missing_required_evidence(" not in projection_text
    assert "weak_tools = {" not in projection_text
    assert "required evidence missing: " not in projection_text
    assert "required finding citation missing: " not in projection_text
    assert "def _missing_required_finding_refs(" not in projection_text
    assert "claimable_refs = _claimable_refs(observations)" in projection_text
    assert "elif _has_non_claimable_report_refs(raw_report, claimable_refs):" in projection_text
    assert "elif not _report_has_visible_refs(report):" in projection_text
    assert "has_observation_gap = _has_observation_gap(observations)" in projection_text
    assert "if context.requires_recommendations and not has_observation_gap and not report.recommendations:" in projection_text
    assert "missing = _dedupe(_safe_report_missing_data(report) + _report_observation_missing_data(observations))" in projection_text
    assert "if context.requires_answer_synthesis is not True:" not in projection_text


def test_copilot_shared_runtime_does_not_own_monthly_answer_dimensions() -> None:
    shared_files = (
        ROOT / "src" / "application" / "copilot" / "contracts.py",
        ROOT / "src" / "application" / "copilot" / "local_harness.py",
        ROOT / "src" / "application" / "copilot" / "model_client.py",
        ROOT / "src" / "application" / "copilot" / "model_decider.py",
        ROOT / "src" / "application" / "copilot" / "result_admission.py",
        ROOT / "src" / "application" / "copilot" / "result_projection.py",
        ROOT / "src" / "application" / "copilot" / "service.py",
        ROOT / "src" / "application" / "copilot" / "host.py",
        ROOT / "src" / "application" / "copilot" / "agent.py",
        ROOT / "src" / "application" / "copilot" / "engine.py",
        ROOT / "src" / "application" / "copilot" / "safety_text.py",
    )
    forbidden_labels = (
        "profit quality",
        "assignment cash outlay",
        "open-exposure concentration",
        "current close-advice signals",
        "requested_month_transaction_history",
        "external_broker_activity_outside_local_ledger",
        "valid_current_negative_evidence",
        "valid_requested_period_negative_evidence",
        "monthly transaction history",
        "closed-trade history",
        "monthly option operation history evidence",
    )

    for path in shared_files:
        text = path.read_text(encoding="utf-8")
        for label in forbidden_labels:
            assert label not in text, path


def test_copilot_error_code_protocol_has_single_contract_owner() -> None:
    contracts_text = (ROOT / "src" / "application" / "copilot" / "contracts.py").read_text(encoding="utf-8")
    tools_text = (ROOT / "src" / "application" / "copilot" / "tools.py").read_text(encoding="utf-8")
    projection_text = (ROOT / "src" / "application" / "copilot" / "result_projection.py").read_text(encoding="utf-8")

    assert "COPILOT_SAFE_ERROR_CODES" in contracts_text
    assert "def safe_error_code(" in contracts_text
    assert "COPILOT_SAFE_ERROR_CODES" not in tools_text
    assert "COPILOT_SAFE_ERROR_CODES" not in projection_text
    assert "safe_error_code(error.get(\"code\"), default=\"TOOL_ERROR\")" in tools_text
    assert "safe_error_code(error.get(\"code\"), default=\"\")" in projection_text


def test_copilot_application_owns_user_response_rendering() -> None:
    rendering_text = (ROOT / "src" / "application" / "copilot" / "rendering.py").read_text(encoding="utf-8")
    cli_text = (ROOT / "src" / "interfaces" / "cli" / "copilot_ops.py").read_text(encoding="utf-8")
    host_text = (ROOT / "src" / "application" / "copilot" / "host.py").read_text(encoding="utf-8")
    service_text = (ROOT / "src" / "application" / "copilot" / "service.py").read_text(encoding="utf-8")

    assert "def render_user_response(" in rendering_text
    assert "_render_report(" in rendering_text
    assert "已尝试检查" in rendering_text
    assert "from src.application.copilot.rendering import render_user_response" in cli_text
    assert "def render_user_response(" not in cli_text
    assert "_render_report(" not in cli_text
    assert "已尝试检查" not in cli_text
    assert "user_response=" not in host_text
    assert "user_response=" not in service_text


def test_copilot_agent_uses_host_supplied_tool_interface() -> None:
    agent_text = (ROOT / "src" / "application" / "copilot" / "agent.py").read_text(encoding="utf-8")

    assert "ActionDecider" in agent_text
    assert "default_action_decider" in agent_text
    assert "ExecutionContract" not in agent_text
    assert "contract:" not in agent_text
    assert "AppResult" not in agent_text
    assert "AnswerReport" not in agent_text
    assert "AppEvent" not in agent_text
    assert "result_from_observations" not in agent_text
    assert "observation_event_payload" not in agent_text
    assert "结论" not in agent_text
    assert "evidence_refs" not in agent_text
    assert "agent_tool_registry" not in agent_text
    assert "tool_execution" not in agent_text
    assert "execute_tool(" not in agent_text
    assert "src.application.copilot import tools" not in agent_text
    assert "while state.turns" not in agent_text
    assert "tool_attempt" not in agent_text
    assert "budget_exhausted" not in agent_text
    assert "run_cancelled" not in agent_text
    assert "monthly_option_review" not in agent_text
    assert "operations_diagnostics" not in agent_text


def test_copilot_engine_is_host_internal_and_business_neutral() -> None:
    engine_text = (ROOT / "src" / "application" / "copilot" / "engine.py").read_text(encoding="utf-8")
    host_text = (ROOT / "src" / "application" / "copilot" / "host.py").read_text(encoding="utf-8")

    assert "run_engine" in engine_text
    assert "ExecutionContract" not in engine_text
    assert "AppEvent" not in engine_text
    assert "AppResult" not in engine_text
    assert "events:" not in engine_text
    assert "contract.input" not in engine_text
    assert "uses_mock_observations(" not in engine_text
    assert "src.application.copilot.result_projection" not in engine_text
    assert "observation_event_payload" not in engine_text
    assert "result_from_observations(" not in engine_text
    assert "result_from_agent_report(" not in engine_text
    assert "project_observations=lambda" in host_text
    assert "project_agent_report=lambda" in host_text
    assert "build_observation_event=observation_event_payload" in host_text
    assert "fixture_id = _fixture_id(contract)" in host_text
    assert "use_mock_observations=fixture_id is not None" in host_text
    assert "requires_fixture_synthesis = fixture_synthesis_policy or _default_fixture_requires_model_synthesis" in host_text
    assert "require_mock_model_synthesis=requires_fixture_synthesis(fixture_id)" in host_text
    assert "scene_input=contract.input" in host_text
    assert "agent_tool_registry" not in engine_text
    assert "tool_execution" not in engine_text
    assert "src.application.assistant" not in engine_text
    assert "src.infrastructure" not in engine_text
    assert "monthly_option_review" not in engine_text
    assert "operations_diagnostics" not in engine_text
    assert "default_action_decider(state)" not in engine_text
    assert "_has_unattempted_manifest_tools(" in engine_text
    assert 'tool_call_id = new_id("toolcall")' in engine_text
    assert "_tool_attempt_payload(action.tool_name, payload, tool_call_id, state.turns)" in engine_text
    assert 'observation["tool_call_id"] = tool_call_id' in engine_text
    assert '"tool_call_id": tool_call_id' in engine_text
    assert '"payload": payload' not in engine_text
    assert '"reason": action.reason' not in engine_text
    assert '"error": observation.get("error")' not in engine_text
    assert '"error_code": _error_code(observation)' in engine_text
    assert '"model_error"' in engine_text
    assert "MODEL_ERROR" in engine_text
    assert "MODEL_ACTION_INVALID" in engine_text
    assert "_agent_action_payload(state.turns, action, manifest.allowed_tools)" in engine_text
    for forbidden in (
        "Agent returned",
        "scene manifest",
        "Tool payload",
        "before running",
        "Agent turn budget",
        "next agent action",
    ):
        assert forbidden not in engine_text


def test_copilot_model_decider_has_no_live_provider_dependency() -> None:
    decider_text = (ROOT / "src" / "application" / "copilot" / "model_decider.py").read_text(encoding="utf-8")

    assert "src.application.assistant" not in decider_text
    assert "src.infrastructure" not in decider_text
    assert "openai" not in decider_text.lower()
    assert "urllib" not in decider_text
    assert "requests" not in decider_text
    assert '"scene"' not in decider_text
    assert '"scene_name"' not in decider_text
    assert '"task_guidance": _model_task_guidance(state.manifest.task_guidance)' in decider_text
    assert "def _model_task_guidance(" in decider_text
    assert '"observations": [_model_observation(item) for item in state.observations]' in decider_text
    assert "def _model_observation(" in decider_text
    assert '"facts_omitted"' in decider_text
    assert '"evidence_context": _model_string_map(item.get("evidence_context"))' in decider_text
    assert "def _model_string_map(" in decider_text
    assert "INTERNAL_EVIDENCE_CONTEXT_KEYS" not in decider_text
    assert "def _bounded_count(" in decider_text
    assert '"remaining_budget": _remaining_budget(state)' in decider_text
    assert '"quality_contract": _quality_contract(state)' in decider_text
    assert "def _quality_contract(" in decider_text
    assert '"limits": dict(state.manifest.limits)' not in decider_text
    assert '"requires_all_allowed_tool_evidence"' not in decider_text
    assert '"missing_allowed_tool_evidence": _missing_allowed_tool_evidence(' in decider_text
    assert "def _missing_allowed_tool_evidence(" in decider_text
    assert '"attempted_tools_without_evidence": _attempted_tools_without_evidence(state)' in decider_text
    assert "def _attempted_tools_without_evidence(" in decider_text
    assert '"summary": _model_summary(item.get("summary"))' in decider_text
    assert '"summary": item.get("summary")' not in decider_text
    assert '"output_contract": _model_output_contract(item.get("output_contract"))' in decider_text
    assert "static_payload_keys" not in decider_text
    assert "required_scene_fields" not in decider_text
    assert "payload_fields" not in decider_text
    assert "def _model_output_contract(" in decider_text
    assert "MAX_MODEL_SUMMARY_CHARS" in decider_text
    assert '"tool_name": "required when kind=tool; null when kind=finish"' in decider_text
    assert '"answer_report": (' in decider_text
    assert "cite only claimable observation refs" in decider_text
    assert "finish action requires answer_report" in decider_text
    assert "finish action requires null tool_name" in decider_text
    assert "tool action requires null answer_report" in decider_text
    assert "optional when kind=finish" not in decider_text
    assert "tool action uses disallowed tool_name" in decider_text
    assert "tool action repeats attempted tool" in decider_text
    assert "tool action uses disallowed tool_name:" not in decider_text
    assert "_parse_model_action(" in decider_text
    assert "requires_recommendations=_requires_recommendations(state)" in decider_text
    assert "claimable_refs=_claimable_refs(state.observations)" in decider_text
    assert '"unattempted_tools_without_evidence": _unattempted_tools_without_evidence(state)' in decider_text
    assert '"attempted_tools_without_evidence": _attempted_tools_without_evidence(state)' in decider_text
    assert "unattempted_tools_without_evidence=_unattempted_tools_without_evidence(state)" in decider_text
    assert 'attempted.update(str(item.get("tool_name") or "") for item in state.observations)' in decider_text
    assert "missing_allowed_tool_evidence=_missing_allowed_tool_evidence(" in decider_text
    assert "finish action requires tool evidence" in decider_text
    assert "finish action requires missing evidence" in decider_text
    assert "finish action requires conclusion" in decider_text
    assert "finish action requires cited findings" in decider_text
    assert "finish action requires recommendations" in decider_text
    assert "finish action requires finding evidence" not in decider_text
    assert "def _has_conclusion(" in decider_text
    assert "from src.application.copilot.safety_text import contains_forbidden_external_action_claim" in decider_text
    assert "finish action claims external action" in decider_text
    assert "contains_forbidden_external_action_claim(final_report)" in decider_text
    assert "has_conflicting_snapshot_view_use(" not in decider_text
    assert "def _has_raw_field_dump(" not in decider_text
    assert "def _raw_assignment_token_count(" not in decider_text
    assert "def _receipt_like_row_dump(" not in decider_text
    assert "def _has_findings(" in decider_text
    assert "def _has_recommendations(" in decider_text
    assert "def _findings_cover_claimable_tools(" not in decider_text
    assert "finish action uses non-claimable evidence refs" in decider_text
    assert "def _all_report_refs_are_claimable(" in decider_text
    assert "def _iter_report_ref_fields(" in decider_text
    assert "def _reports_missing_tool_evidence(" in decider_text
    assert "allowed_refs = {ref for ref in claimable_refs or [] if ref}" in decider_text
    assert 'kind="finish", reason=f"model action invalid' not in decider_text
    assert 'kind="invalid"' in decider_text
    assert 'reason=f"model action invalid' in decider_text
    assert 'error_code="MODEL_ERROR"' in decider_text
    assert 'error_code="MODEL_ACTION_INVALID"' in decider_text
    assert '"value_preview"' not in decider_text
    assert '"data"' not in decider_text
    assert 'item.get("error")' not in decider_text
    assert '"error": repair_error' in decider_text
    assert "config_key" not in decider_text
    assert "state.contract" not in decider_text
    assert "_user_message_from_manifest(state.manifest.messages)" in decider_text
    assert "except Exception as exc" in decider_text
    assert "exc.__class__.__name__" in decider_text
    assert "str(exc)" not in decider_text
    assert '"previous_response": _repair_previous_response(previous_response)' in decider_text
    assert '"previous_response": previous_response or {}' not in decider_text
    assert '"has_tool_name": bool(tool_name)' in decider_text
    assert '"tool_name": previous_response.get("tool_name")' not in decider_text


def test_copilot_model_client_uses_shared_provider_boundary() -> None:
    model_client_text = (ROOT / "src" / "application" / "copilot" / "model_client.py").read_text(encoding="utf-8")
    assistant_provider_text = (ROOT / "src" / "application" / "assistant" / "llm_provider_registry.py").read_text(encoding="utf-8")

    assert "src.application.llm_provider_registry" in model_client_text
    assert "src.application.assistant" not in model_client_text
    assert "src.application.agent_tool_registry" not in model_client_text
    assert "src.application.tool_execution" not in model_client_text
    assert "monthly_option_review" not in model_client_text
    assert "operations_diagnostics" not in model_client_text
    assert "exposure" not in model_client_text
    assert "contract counts" not in model_client_text
    assert "config_key" not in model_client_text
    assert "final answer shape" not in model_client_text
    assert "Use task_guidance only for scene evidence expectations and stopping conditions." in model_client_text
    assert "Use quality_contract as the output contract" in model_client_text
    assert "Use answer_quality as the output quality contract" not in model_client_text
    assert '"required": ["summary", "action", "target_scope", "answer_dimension", "basis_refs"]' in model_client_text
    assert '"type": ["string", "null"]' in model_client_text
    assert "Use one task_guidance.answer_dimensions value when present; otherwise null." in model_client_text
    assert "set answer_dimension to one task_guidance.answer_dimensions value when present, otherwise null" in model_client_text
    assert "Use each observation's evidence_context to distinguish requested-scope evidence, current context" in model_client_text
    assert "finish_conditions.refs_with_omitted_facts" not in model_client_text
    assert "finish_conditions.snapshot_view_boundaries" not in model_client_text
    assert "finish_conditions.valid_empty_result_boundaries" not in model_client_text
    assert "If an observation has facts_omitted" in model_client_text
    assert "If unattempted_tools_without_evidence is non-empty, choose a tool from that list before finishing." in model_client_text
    assert "Do not choose a tool already listed in attempted_tools" in model_client_text
    assert "Treat allowed_tools_without_evidence as a status summary only" not in model_client_text
    assert "Use attempted_tools_without_evidence only to understand which already-attempted tools still lack usable evidence." in model_client_text
    assert "missing evidence, not retry targets" in model_client_text
    assert "findings must cite each tool listed in finish_conditions.claimable_refs_by_tool" not in model_client_text
    assert "no useful budget remains" not in model_client_text
    assert "Follow task_guidance when deciding evidence sufficiency" not in model_client_text
    assert "from src.application.llm_provider_registry import *" in assistant_provider_text


def test_copilot_design_keeps_recommendation_answer_dimension_scene_conditional() -> None:
    design_text = (ROOT / "docs" / "OM_COPILOT_V2_DESIGN.md").read_text(encoding="utf-8")

    assert "is not a shared recommendation field" in design_text
    assert "required only when the selected scene" in design_text
    assert "declares `answer_dimensions`" in design_text
    assert "`target_scope`, `answer_dimension`, and `basis_refs` as explicit fields" not in design_text
    assert "`action`, `target_scope`, and `answer_dimension` must be non-empty" not in design_text


def test_copilot_phase2_keeps_non_review_model_action_fixtures() -> None:
    fixture_root = ROOT / "tests" / "fixtures" / "copilot"

    for name in (
        "candidate_filter_diagnostics_model_action.json",
        "close_advice_notification_diagnostics_model_action.json",
        "june_income_attribution_model_action.json",
        "current_option_exposure_model_action.json",
    ):
        assert (fixture_root / name).is_file(), name


def test_copilot_current_slice_exposes_declared_channel_scenes() -> None:
    from src.application.copilot.scene import SCENE_CATALOG

    channel_ready = [scene.name for scene in SCENE_CATALOG if scene.phase_readiness == "channel_ready"]

    assert channel_ready == ["operations_diagnostics", "monthly_income_attribution"]


def test_copilot_channel_tests_do_not_use_monthly_review_as_smoke_scene() -> None:
    checked_files = (
        ROOT / "tests" / "test_copilot_phase1.py",
        ROOT / "tests" / "test_assistant_runtime.py",
    )
    forbidden = (
        'channel_scenes=("monthly_option_review"',
        '"channel_scenes": ["monthly_option_review"]',
    )

    for path in checked_files:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, path


def test_docs_do_not_claim_monthly_review_is_channel_ready() -> None:
    checked_docs = (
        ROOT / "docs" / "AGENT_INTEGRATION.md",
        ROOT / "docs" / "AGENT_WIKI.md",
        ROOT / "docs" / "ARCHITECTURE.md",
        ROOT / "docs" / "INBOUND_CONTROL.md",
        ROOT / "docs" / "OM_AGENT_CAPABILITY_MAP.md",
        ROOT / "docs" / "TOOL_REFERENCE.md",
    )
    forbidden = (
        "monthly_option_review` is the first channel-ready",
        "monthly_option_review` as the first channel-ready",
        "monthly_option_review` is channel-ready",
        "Only `monthly_option_review` is channel-ready",
        "当前只有\n`monthly_option_review` 可作为渠道场景",
    )

    for path in checked_docs:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, path


def test_capability_map_presents_copilot_as_multi_scene_surface() -> None:
    text = (ROOT / "docs" / "OM_AGENT_CAPABILITY_MAP.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    for token in (
        "operations diagnostics",
        "income attribution",
        "current exposure",
    ):
        assert token in normalized
    for token in (
        "--scene current_option_exposure",
        "--scene monthly_income_attribution",
    ):
        assert token in text

    assert "monthly_option_review" not in text


def test_readme_presents_local_copilot_as_multi_scene_surface() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    for token in (
        "诊断",
        "收益归因",
        "当前暴露",
        "--scene current_option_exposure",
        "--scene monthly_income_attribution",
    ):
        assert token in normalized

    assert "monthly_option_review" not in text


def test_copilot_scene_catalog_has_no_dedicated_monthly_option_review() -> None:
    from src.application.copilot.scene import SCENE_CATALOG

    scene_names = [scene.name for scene in SCENE_CATALOG]

    assert "monthly_option_review" not in scene_names


def test_phase2_design_requires_non_review_lanes_before_monthly_benchmark() -> None:
    text = (ROOT / "docs" / "OM_COPILOT_V2_DESIGN.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "monthly_option_review" not in text
    assert "monthly option review last as the benchmark" not in normalized


def test_phase1_design_main_blueprint_is_lane_based_not_monthly_command_dump() -> None:
    text = (ROOT / "docs" / "OM_COPILOT_V2_DESIGN.md").read_text(encoding="utf-8")
    section = text.split("### Phase 1 Outcome", 1)[1].split("`./om copilot run` is deterministic", 1)[0]
    main_table = section.split("Additional monthly-review benchmark fixtures", 1)[0]

    for lane in (
        "| Candidate diagnosis |",
        "| Close-advice notification diagnosis |",
        "| Monthly income attribution |",
        "| Current exposure analysis |",
    ):
        assert lane in main_table

    assert "monthly_option_review" not in main_table


def test_phase2_design_does_not_make_monthly_review_rules_the_runtime_blueprint() -> None:
    text = (ROOT / "docs" / "OM_COPILOT_V2_DESIGN.md").read_text(encoding="utf-8")
    phase2_text = text.split("### Phase 2: Model-Backed Answer-Quality Loop", 1)[1].split(
        "### Phase 3: Channel Rollout", 1
    )[0]

    for token in (
        "monthly option-review recommendations must",
        "every monthly option-review recommendation",
        "symbol-specific monthly option-review recommendations",
        "account-specific monthly option-review recommendations",
        "monthly option-review recommendations that state concrete amounts",
    ):
        assert token not in phase2_text


def test_copilot_phase2_completion_checklist_has_test_evidence() -> None:
    design_text = (ROOT / "docs" / "OM_COPILOT_V2_DESIGN.md").read_text(encoding="utf-8")
    tests_text = (ROOT / "tests" / "test_copilot_phase1.py").read_text(encoding="utf-8")
    fixture_root = ROOT / "tests" / "fixtures" / "copilot"

    for lane in (
        "Diagnostics",
        "Income attribution",
        "Current exposure",
        "Missing/stale evidence",
        "Shared runtime boundary",
        "Channel boundary",
    ):
        assert f"| {lane} |" in design_text

    for local_model_test in (
        "test_service_projects_scene_to_host_manifest_without_answer_markers",
        "test_write_like_request_is_refused_before_host",
        "test_channel_environment_keeps_monthly_option_review_not_ready",
        "test_local_runtime_question_runs_service_host_agent_loop",
        "test_monthly_option_review_is_not_a_dedicated_copilot_capability",
        "test_model_decider_collects_required_tools_before_calling_model",
        "test_model_decider_degrades_after_required_tool_evidence_is_collected",
        "test_monthly_income_model_error_still_collects_all_required_evidence",
        "test_result_admission_rejects_external_action_claim",
        "test_cli_copilot_eval_accepts_model_action_file",
        "test_cli_copilot_run_uses_local_tools",
        "test_copilot_code_does_not_reintroduce_marker_based_answer_guard",
    ):
        assert local_model_test in tests_text

    for action_fixture in (
        "candidate_filter_diagnostics_model_action.json",
        "close_advice_notification_diagnostics_model_action.json",
        "june_income_attribution_model_action.json",
        "current_option_exposure_model_action.json",
    ):
        assert (fixture_root / action_fixture).is_file(), action_fixture


def test_copilot_follow_on_evals_start_with_non_review_lanes() -> None:
    design_text = (ROOT / "docs" / "OM_COPILOT_V2_DESIGN.md").read_text(encoding="utf-8")
    match = re.search(
        r"Follow-on answer-quality eval cases for the broader Copilot surface:\n\n"
        r"(?P<items>(?:\d+\. .+\n)+)",
        design_text,
    )
    assert match is not None

    items = [line.split(". ", 1)[1].strip() for line in match.group("items").splitlines()]

    assert items[:2] == ["Candidate rejection diagnosis.", "Close-advice notification diagnosis."]
    assert items[0] != "Monthly option-operation review."


def test_planner_tool_metadata_lives_on_agent_tool_definitions() -> None:
    from dataclasses import fields

    from src.application.agent_tool_registry import get_tool_definition
    from src.application.assistant.tool_bindings import AssistantToolBinding

    binding_fields = {field.name for field in fields(AssistantToolBinding)}
    assert "description" not in binding_fields
    assert "input_schema" not in binding_fields
    assert "output_contract" not in binding_fields
    assert "planner_notes" not in binding_fields
    assert "planner_semantics" not in binding_fields
    assert "copilot_notes" not in binding_fields

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

    perception_tokens = (
        "parse_assistant_command",
        "parse_permission_response",
        "natural_language_rebuilding_error",
    )
    for token in perception_tokens:
        assert token in perception_text
    removed_tokens = (
        "run_read_only_agent_loop",
        "generate_general_reply",
        "build_conversation_context",
        "context_trace",
    )
    offenders = [token for token in removed_tokens if token in perception_text]
    assert offenders == []
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


def test_assistant_session_trace_does_not_surface_old_answer_guard() -> None:
    session_store_text = (ROOT / "src" / "application" / "assistant" / "session_store.py").read_text(encoding="utf-8")

    assert "answer_guard" not in session_store_text
    assert "synthesized_after_answer_guard" not in session_store_text


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
