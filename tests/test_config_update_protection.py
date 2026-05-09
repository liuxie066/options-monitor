from __future__ import annotations

from pathlib import Path

from scripts import guardrails_check


def test_guardrails_classifies_only_root_runtime_configs() -> None:
    assert guardrails_check.is_root_runtime_config_path(Path("config.us.json"))
    assert guardrails_check.is_root_runtime_config_path(Path("config.hk.json"))
    assert guardrails_check.is_root_runtime_config_path(Path("config.json"))
    assert guardrails_check.is_root_runtime_config_path(Path("config.market_us.json"))
    assert guardrails_check.is_root_runtime_config_path(Path("config.local.prod.json"))
    assert guardrails_check.is_root_runtime_config_path(Path("config.us.json.bak.20260507-100000"))

    assert not guardrails_check.is_root_runtime_config_path(Path("configs/examples/user.example.us.json"))
    assert not guardrails_check.is_root_runtime_config_path(Path("scripts/config_loader.py"))


def test_guardrails_rejects_tracked_root_runtime_configs() -> None:
    issues = guardrails_check.check_runtime_config_tracking(
        [
            Path("config.us.json"),
            Path("config.hk.json"),
            Path("configs/examples/user.example.us.json"),
        ]
    )

    assert [issue.path.as_posix() for issue in issues] == ["config.us.json", "config.hk.json"]
    assert all("root runtime config must stay untracked" in issue.reason for issue in issues)


def test_publish_to_prod_hard_skips_runtime_configs_even_if_tracked() -> None:
    src = Path("scripts/publish_to_prod.sh").read_text(encoding="utf-8")

    assert "Never copy root runtime configs" in src
    assert "config.json|config.us.json|config.hk.json|config.scheduled.json" in src
    assert "config.market_*.json" in src
    assert "config.local*.json" in src
    assert "config.*.bak.*" in src
