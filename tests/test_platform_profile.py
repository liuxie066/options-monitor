from __future__ import annotations

from pathlib import Path

from src.application.platform_profile import (
    current_platform_profile,
    default_runtime_root_for_service_target,
)


def test_linux_platform_profile_defaults() -> None:
    profile = current_platform_profile(system="Linux", home=Path("/home/om"))

    assert profile.platform == "linux"
    assert profile.service_target == "systemd"
    assert profile.default_install_prefix == Path("/home/om/apps/options-monitor")
    assert profile.default_runtime_root == Path("/var/lib/options-monitor")
    assert profile.default_env_file == Path("/etc/options-monitor/options-monitor.env")


def test_macos_platform_profile_defaults() -> None:
    profile = current_platform_profile(system="Darwin", home=Path("/Users/liuxie"))

    assert profile.platform == "macos"
    assert profile.service_target == "launchd"
    assert profile.default_install_prefix == Path("/Users/liuxie/apps/options-monitor")
    assert profile.default_runtime_root == Path("/Users/liuxie/Library/Application Support/options-monitor")
    assert profile.default_env_file == Path("/Users/liuxie/Library/Application Support/options-monitor/options-monitor.env")


def test_service_target_runtime_root_defaults() -> None:
    assert default_runtime_root_for_service_target("systemd", home=Path("/Users/me")) == Path("/var/lib/options-monitor")
    assert default_runtime_root_for_service_target("launchd", home=Path("/Users/me")) == Path(
        "/Users/me/Library/Application Support/options-monitor"
    )
