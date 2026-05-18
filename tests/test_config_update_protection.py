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
    assert not guardrails_check.is_root_runtime_config_path(Path("src/application/config_loader.py"))


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


def test_deploy_to_prod_skips_runtime_configs_by_default() -> None:
    from scripts import deploy_to_prod

    assert deploy_to_prod.should_skip(Path("config.us.json")) is True
    assert deploy_to_prod.should_skip(Path("config.hk.json")) is True
    assert deploy_to_prod.should_skip(Path("config.local.prod.json")) is True
    assert deploy_to_prod.should_skip(Path("config.us.json.bak.20260507-100000")) is True
    assert deploy_to_prod.should_skip(Path("configs/examples/user.example.us.json")) is False
    assert deploy_to_prod.should_skip(Path("src/application/config_loader.py")) is False
