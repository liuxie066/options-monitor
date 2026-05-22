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
    from src.application.agent_runtime.command_catalog import llm_allowed_specs
    from src.application.agent_runtime.llm_intent_schema import llm_intent_json_schema, llm_intent_schema
    from src.application.agent_tool_registry import get_tool_definition
    from src.application.inbound.policy import PURE_READ_TOOLS

    schema = llm_intent_schema()
    json_schema = llm_intent_json_schema()
    allowed_names = {spec.intent_name for spec in llm_allowed_specs()}

    assert schema["write_intents_allowed"] is False
    assert set(json_schema["properties"]["intent"]["enum"]) == allowed_names
    assert not any(name.endswith(("_confirm", "_cancel")) for name in allowed_names)

    pure_read_router_tools = {"inbound.pending", "inbound.symbols"}
    for spec in llm_allowed_specs():
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


def test_feishu_ws_cli_keeps_runtime_and_assistant_config_flags_separate() -> None:
    text = (ROOT / "src" / "interfaces" / "cli" / "main.py").read_text(encoding="utf-8")

    assert 'inbound_ws.add_argument("--config-path"' in text
    assert 'inbound_ws.add_argument("--assistant-config"' in text
    assert "build_feishu_ws_settings(" in text
    assert "config_path=args.config_path" in text
    assert "assistant_config_path=args.assistant_config" in text
