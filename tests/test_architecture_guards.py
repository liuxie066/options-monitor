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
                "assistant": {"enabled": True, "copilot": {"enabled": True}},
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


def test_old_assistant_evidence_architecture_is_removed() -> None:
    removed = (
        "answer_verifier.py",
        "evidence.py",
        "task_contract.py",
    )

    existing = [name for name in removed if (ROOT / "src" / "application" / "assistant" / name).exists()]
    assert existing == []

    production = ROOT / "src"
    offenders: list[str] = []
    for path in sorted(production.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "src.application.assistant.answer_verifier" in text:
            offenders.append(str(path.relative_to(ROOT)))
        if "src.application.assistant.evidence" in text:
            offenders.append(str(path.relative_to(ROOT)))
        if "src.application.assistant.task_contract" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_old_freeform_session_builder_is_removed() -> None:
    assistant_root = ROOT / "src" / "application" / "assistant"

    for filename in ("session.py", "session_store.py", "verifier_hooks.py"):
        assert not (assistant_root / filename).exists()


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
    for path in sorted(copilot_root.glob("*.py")):
        if path.name == "eval_fixtures.py":
            continue
        text = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}:{token}")

    assert offenders == []


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


def test_copilot_cli_does_not_accept_runtime_environment_paths() -> None:
    cli_text = (ROOT / "src" / "interfaces" / "cli" / "copilot_ops.py").read_text(encoding="utf-8")

    assert "--env-file" not in cli_text
    assert "--no-local-env-file" not in cli_text
    assert "--config-path" not in cli_text
    assert "--assistant-config" in cli_text


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
    assert "requires_answer_synthesis" not in host_text


def test_copilot_local_harness_is_phase1_composition_only() -> None:
    harness_path = ROOT / "src" / "application" / "copilot" / "local_harness.py"
    harness_text = harness_path.read_text(encoding="utf-8")
    imports = set(_imported_modules(harness_path))

    assert "src.application.copilot.service" in imports
    assert "src.application.copilot.host" in imports
    assert "src.application.copilot.model_config" in imports
    assert "src.application.copilot.model_client" in imports
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
        path = copilot_root / filename
        if not path.exists():
            continue
        imports = _imported_modules_with_from_names(path)
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
    assert 'event_log.record("result_rejected"' in host_text
    assert "def _admit" not in host_text
    assert "src.application.assistant" not in admission_text
    assert "answer_guard" not in admission_text
    assert "answer_verifier" not in admission_text
    assert "semantic" not in admission_text
    assert "llm" not in admission_text.lower()
    assert "import re" not in admission_text
    assert "raw_grouped_rows" not in admission_text
    assert "_looks_like" not in admission_text
    assert "safety_text" not in admission_text
    assert "FORBIDDEN_MUTATION_CLAIMS" not in admission_text
    assert "account=" not in admission_text
    assert "symbol=" not in admission_text
    assert "monthly_option_review" not in admission_text
    assert "operations_diagnostics" not in admission_text
    assert "Copilot 结果未通过结构或安全校验。" in admission_text


def test_copilot_shared_runtime_does_not_own_monthly_answer_dimensions() -> None:
    shared_files = sorted((ROOT / "src" / "application" / "copilot").glob("*.py"))
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

    assert "COPILOT_SAFE_ERROR_CODES" in contracts_text
    assert "def safe_error_code(" in contracts_text
    assert "COPILOT_SAFE_ERROR_CODES" not in tools_text
    assert "safe_error_code(error.get(\"code\"), default=\"TOOL_ERROR\")" in tools_text


def test_copilot_uses_model_final_text_without_application_answer_renderer() -> None:
    cli_text = (ROOT / "src" / "interfaces" / "cli" / "copilot_ops.py").read_text(encoding="utf-8")
    host_text = (ROOT / "src" / "application" / "copilot" / "host.py").read_text(encoding="utf-8")
    service_text = (ROOT / "src" / "application" / "copilot" / "service.py").read_text(encoding="utf-8")

    assert not (ROOT / "src" / "application" / "copilot" / "rendering.py").exists()
    assert "src.application.copilot.rendering" not in cli_text
    assert "def render_user_response(" not in cli_text
    assert "_render_report(" not in cli_text
    assert "user_response=result.user_response" not in cli_text
    assert "user_response=" in host_text
    assert 'user_response="请提供要查询或分析的问题。"' in service_text


def test_copilot_design_does_not_define_recommendation_output_schema() -> None:
    design_text = (ROOT / "docs" / "OM_COPILOT_V2_DESIGN.md").read_text(encoding="utf-8")

    assert "Prompt fragments define general behavior only" in design_text
    assert "Question-specific prompts, tool lists, and renderers are prohibited" in design_text
    assert "answer_dimensions" not in design_text
    assert "basis_refs" not in design_text


def test_copilot_keeps_generic_answer_quality_model_turn_fixtures() -> None:
    fixture_root = ROOT / "tests" / "fixtures" / "copilot"

    for name in (
        "candidate_filter_diagnostics_model_turns.json",
        "close_advice_notification_diagnostics_model_turns.json",
        "june_income_attribution_model_turns.json",
        "current_option_exposure_model_turns.json",
    ):
        assert (fixture_root / name).is_file(), name


def test_docs_describe_one_general_copilot_scene() -> None:
    checked_docs = (
        ROOT / "README.md",
        ROOT / "docs" / "AGENT_INTEGRATION.md",
        ROOT / "docs" / "AGENT_WIKI.md",
        ROOT / "docs" / "ARCHITECTURE.md",
        ROOT / "docs" / "INBOUND_CONTROL.md",
        ROOT / "docs" / "OM_AGENT_CAPABILITY_MAP.md",
        ROOT / "docs" / "TOOL_REFERENCE.md",
    )
    forbidden = (
        "NATURAL_LANGUAGE_REBUILDING",
        "assistant.copilot.channel_scenes",
        "operations_diagnostics",
        "monthly_income_attribution",
        "--scene current_option_exposure",
        "--scene monthly_income_attribution",
    )

    for path in checked_docs:
        text = path.read_text(encoding="utf-8")
        assert "om_chat" in text, path
        for token in forbidden:
            assert token not in text, path


def test_capability_map_presents_one_general_copilot_scene() -> None:
    text = (ROOT / "docs" / "OM_AGENT_CAPABILITY_MAP.md").read_text(encoding="utf-8")
    assert "single `om_chat` Scene" in text
    assert "channel_scenes" not in text
    assert "monthly_income_attribution" not in text
    assert "operations_diagnostics" not in text


def test_readme_presents_local_copilot_as_one_scene_surface() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "唯一的 read-first `om_chat` Copilot Scene" in text
    assert "最多请求一个确定性 Control preview" in text
    assert "--scene current_option_exposure" not in text
    assert "--scene monthly_income_attribution" not in text
    assert "--model-turn-json-file" in text


def test_phase2_design_requires_non_review_lanes_before_monthly_benchmark() -> None:
    text = (ROOT / "docs" / "OM_COPILOT_V2_DESIGN.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "monthly_option_review" not in text
    assert "monthly option review last as the benchmark" not in normalized


def test_design_phases_keep_real_answer_quality_as_the_cutover_gate() -> None:
    text = (ROOT / "docs" / "OM_COPILOT_V2_DESIGN.md").read_text(encoding="utf-8")
    for token in (
        "| P1 | Production answer-quality baseline |",
        "P1 is the gate for P7 and P8",
        "No benchmark may become runtime routing, a dedicated Scene, or an answer template",
        "converts canonical results into flat Agent-friendly observations",
    ):
        assert token in text


def test_answer_quality_phase_does_not_make_benchmarks_runtime_capabilities() -> None:
    text = (ROOT / "docs" / "OM_COPILOT_V2_DESIGN.md").read_text(encoding="utf-8")
    phase_text = text.split("## Evaluation", 1)[1].split("## Delivery Phases", 1)[0]

    assert "No benchmark may become runtime routing" in phase_text
    assert "monthly_option_review" not in phase_text
    assert "monthly review template" not in phase_text


def test_copilot_evals_cover_broad_free_form_question_families() -> None:
    design_text = (ROOT / "docs" / "OM_COPILOT_V2_DESIGN.md").read_text(encoding="utf-8")
    evaluation = design_text.split("## Evaluation", 1)[1].split("## Delivery Phases", 1)[0]

    for question_family in (
        "income and attribution follow-up",
        "exposure concentration",
        "option-operation review",
        "candidate diagnosis",
        "close-advice notification diagnosis",
        "conclusion follow-up",
    ):
        assert question_family in evaluation
    assert "No benchmark may become runtime routing, a dedicated Scene, or an answer template." in evaluation


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


def test_deleted_assistant_answer_helpers_do_not_return() -> None:
    assistant_root = ROOT / "src" / "application" / "assistant"

    for filename in (
        "deterministic_commands.py",
        "llm_common.py",
        "llm_provider_registry.py",
        "time_filters.py",
        "tool_contracts.py",
        "tool_policy.py",
        "user_profile.py",
    ):
        assert not (assistant_root / filename).exists()


def test_read_tool_allowlist_has_neutral_owner() -> None:
    from src.application import tool_allowlist
    from src.application.assistant import policy as assistant_policy

    assert assistant_policy.PURE_READ_TOOLS is tool_allowlist.PURE_READ_TOOLS
    assert not (ROOT / "src" / "application" / "assistant" / "tool_policy.py").exists()
    policy_text = (ROOT / "src" / "application" / "assistant" / "policy.py").read_text(encoding="utf-8")
    assert "from src.application.tool_allowlist import PURE_READ_TOOLS" in policy_text


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


def test_assistant_runtime_no_longer_owns_perception() -> None:
    runtime_text = (ROOT / "src" / "application" / "assistant" / "runtime.py").read_text(encoding="utf-8")

    assert "PerceptionEngine" not in runtime_text
    assert "perception_trace" not in runtime_text
    assert not (ROOT / "src" / "application" / "assistant" / "perception.py").exists()
    assert not (ROOT / "src" / "application" / "assistant" / "perception_trace.py").exists()


def test_assistant_inbound_service_uses_single_explicit_control_execution() -> None:
    inbound_text = (ROOT / "src" / "application" / "assistant" / "inbound_service.py").read_text(encoding="utf-8")
    control_text = (ROOT / "src" / "application" / "assistant" / "inbound_control.py").read_text(encoding="utf-8")
    contracts_text = (ROOT / "src" / "application" / "assistant" / "contracts.py").read_text(encoding="utf-8")

    assert not (ROOT / "src" / "application" / "assistant" / "router.py").exists()
    assert "execute_explicit_control(" in inbound_text
    assert '"control": control.public_payload()' in inbound_text
    assert "resolve_reasoning(" not in inbound_text
    assert "perform_action(" not in inbound_text
    assert "build_observation(" not in inbound_text
    assert "frame_planner" not in inbound_text
    assert "class ControlExecution" in control_text
    assert "def execute_explicit_control(" in control_text
    assert "class ControlCommand" in contracts_text
    assert "class ReasoningResolution" not in contracts_text
    assert "class ActionResult" not in contracts_text
    assert "class ObservationResponse" not in contracts_text
    for filename in ("reasoning.py", "action.py", "observation.py"):
        assert not (ROOT / "src" / "application" / "assistant" / filename).exists()
    assert "class AssistantFrame" not in contracts_text
    assert "class ToolPlan" not in contracts_text
    for module in ("manual_trade_operations.py", "symbol_operations.py", "upgrade_operations.py"):
        module_text = (ROOT / "src" / "application" / "assistant" / module).read_text(encoding="utf-8")
        assert "is_manual_trade_operation_intent" not in module_text
        assert "is_symbol_operation_intent" not in module_text
        assert "is_upgrade_operation_intent" not in module_text


def test_assistant_turn_result_has_no_retired_router_fallback() -> None:
    turn_result_text = (ROOT / "src" / "application" / "assistant" / "turn_result.py").read_text(
        encoding="utf-8"
    )

    assert 'route or "router"' not in turn_result_text
    assert 'route or "unknown"' in turn_result_text


def test_assistant_does_not_recreate_derived_agent_session_trace() -> None:
    assistant_root = ROOT / "src" / "application" / "assistant"
    runtime_text = (assistant_root / "runtime.py").read_text(encoding="utf-8")
    diagnostics_text = (ROOT / "src" / "application" / "agent_tools" / "diagnostics.py").read_text(encoding="utf-8")

    assert "AgentSession" not in runtime_text
    assert "memory_proposals" not in runtime_text
    assert "assistant_trace" not in diagnostics_text
    assert not (assistant_root / "llm_trace.py").exists()


def test_inbound_audit_does_not_recreate_old_answer_stages() -> None:
    audit_text = (ROOT / "src" / "application" / "assistant" / "audit.py").read_text(encoding="utf-8")
    diagnostics_text = (
        ROOT / "src" / "application" / "assistant" / "operation_diagnostics.py"
    ).read_text(encoding="utf-8")

    for token in (
        "perception_json",
        "reasoning_json",
        "action_json",
        "observation_json",
    ):
        assert token not in audit_text
        assert token not in diagnostics_text


def test_assistant_control_command_is_canonical_contract_name() -> None:
    from src.application.assistant.contracts import ControlCommand

    assert ControlCommand.__name__ == "ControlCommand"
    contracts_text = (ROOT / "src" / "application" / "assistant" / "contracts.py").read_text(
        encoding="utf-8"
    )
    assert "PerceptionResult" not in contracts_text
    assert "om-perception-result" not in contracts_text
    assert "evidence:" not in contracts_text
    assert "tool_calls:" not in contracts_text

    offenders: list[str] = []
    allowed = {"contracts.py", "__init__.py"}
    for path in sorted((ROOT / "src" / "application" / "assistant").glob("*.py")):
        if path.name in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if "AssistantIntent" in text or "SemanticFrame" in text:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_control_command_parser_does_not_plan_or_execute_tools() -> None:
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
    ]
    assert not (ROOT / "src" / "application" / "assistant" / "deterministic_commands.py").exists()
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
    provider_forbidden = ("monthly_option_review", "task_kind", "evidence_plan", "ReasoningResolution")
    provider_offenders: dict[str, list[str]] = {}
    for path in provider_files:
        text = path.read_text(encoding="utf-8")
        hits = [token for token in provider_forbidden if token in text]
        if hits:
            provider_offenders[str(path.relative_to(ROOT))] = hits
    assert provider_offenders == {}


def test_runtime_inbound_service_and_control_do_not_know_model_profiles() -> None:
    forbidden = (
        "active_model",
        "models",
        "LlmModelProfile",
        "llm_model_profiles",
    )
    checked = [
        ROOT / "src" / "application" / "assistant" / "runtime.py",
        ROOT / "src" / "application" / "assistant" / "inbound_service.py",
        ROOT / "src" / "application" / "assistant" / "inbound_control.py",
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
        "src.application.assistant.inbound_service",
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


def test_copilot_has_one_general_scene_and_no_business_activation_router() -> None:
    scene_text = (ROOT / "src" / "application" / "copilot" / "scene.py").read_text(encoding="utf-8")
    service_text = (ROOT / "src" / "application" / "copilot" / "service.py").read_text(encoding="utf-8")

    assert 'GENERAL_SCENE = "om_chat"' in scene_text
    assert "SCENE_CATALOG" not in scene_text
    assert "activation_terms" not in scene_text
    assert "select_scene" not in service_text
    assert "evaluate_safety" not in service_text
    assert "monthly_income_attribution" not in scene_text
    assert "operations_diagnostics" not in scene_text


def test_copilot_agent_loop_is_model_first_without_fixed_collection_fallback() -> None:
    agent_text = (ROOT / "src" / "application" / "copilot" / "agent.py").read_text(encoding="utf-8")
    engine_text = (ROOT / "src" / "application" / "copilot" / "engine.py").read_text(encoding="utf-8")

    assert not (ROOT / "src" / "application" / "copilot" / "model_decider.py").exists()
    assert "default_action_decider" not in agent_text + engine_text
    assert "call_signatures" in agent_text and "call_signatures" in engine_text
    assert "force_finish" in agent_text and "force_finish" in engine_text
    assert "__read_observation__" in engine_text


def test_copilot_tool_adapter_reuses_registry_without_business_evidence_recipes() -> None:
    tools_path = ROOT / "src" / "application" / "copilot" / "tools.py"
    tools_text = tools_path.read_text(encoding="utf-8")

    assert "pure_read_tool_names" in tools_text
    assert "get_tool_definition" in tools_text
    assert "TOOL_VIEWS" not in tools_text
    assert "monthly_income" not in tools_text
    assert "candidate_filter_facts" not in tools_text
    assert "src.application.agent_tools" not in tools_text


def test_copilot_result_admission_is_structural_and_safety_only() -> None:
    admission_text = (ROOT / "src" / "application" / "copilot" / "result_admission.py").read_text(encoding="utf-8")

    assert "VALID_STATUSES" in admission_text
    assert "invalid_status" in admission_text
    assert "empty_result" in admission_text
    assert "import re" not in admission_text
    assert "safety_text" not in admission_text
    assert "missing_conclusion_prefix" not in admission_text
    assert "malformed_findings" not in admission_text
    assert "evidence_refs" not in admission_text
    assert "attempted_checks" not in admission_text


def test_copilot_host_owns_session_serialization_and_context() -> None:
    host_text = (ROOT / "src" / "application" / "copilot" / "host.py").read_text(encoding="utf-8")
    channel_text = (ROOT / "src" / "application" / "copilot" / "channel_facade.py").read_text(encoding="utf-8")

    assert "def session_messages(" in host_text
    assert "def record_session_turn(" in host_text
    assert "def session_run_slot(" in host_text
    assert "session_messages(session_key, host_store=host_store)" in channel_text
    assert "record_session_turn(" in channel_text
    assert "host_store=host_store" in channel_text
    assert "_RUNNING_CHANNEL_KEYS" not in channel_text


def test_freeform_channel_bypasses_legacy_perception_pipeline() -> None:
    inbound_text = (ROOT / "src" / "application" / "assistant" / "inbound_service.py").read_text(encoding="utf-8")
    runtime_text = (ROOT / "src" / "application" / "assistant" / "runtime.py").read_text(encoding="utf-8")

    assert "if command is None:" in inbound_text
    assert "_copilot_response(" in inbound_text
    assert "PerceptionEngine" not in runtime_text
    assert not (ROOT / "src" / "application" / "assistant" / "perception.py").exists()
    assert not (ROOT / "src" / "application" / "assistant" / "perception_trace.py").exists()


def test_removed_evidence_architecture_cannot_return_under_copilot_names() -> None:
    copilot_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "src" / "application" / "copilot").glob("*.py"))
    )
    for token in (
        "answer_verifier",
        "EvidenceBundle",
        "required finding citation",
        "required recommendation citation",
        "claimable_refs_by_tool",
    ):
        assert token not in copilot_text
