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
