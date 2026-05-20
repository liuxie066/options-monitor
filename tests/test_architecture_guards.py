from __future__ import annotations

import inspect
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
