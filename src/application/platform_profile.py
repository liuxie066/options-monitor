from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


PlatformName = Literal["linux", "macos", "other"]
ServiceTargetName = Literal["systemd", "launchd", "manual"]


@dataclass(frozen=True)
class PlatformProfile:
    platform: PlatformName
    system: str
    service_target: ServiceTargetName
    default_install_prefix: Path
    default_runtime_root: Path
    default_env_file: Path
    local_env_file: Path
    prerequisite_hints: tuple[str, ...]
    service_notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "system": self.system,
            "service_target": self.service_target,
            "default_install_prefix": str(self.default_install_prefix),
            "default_runtime_root": str(self.default_runtime_root),
            "default_env_file": str(self.default_env_file),
            "local_env_file": str(self.local_env_file),
            "prerequisite_hints": list(self.prerequisite_hints),
            "service_notes": list(self.service_notes),
        }


def current_platform_profile(*, system: str | None = None, home: str | Path | None = None) -> PlatformProfile:
    raw_system = (system or platform.system() or "").strip()
    key = raw_system.lower()
    home_dir = Path(home).expanduser() if home is not None else Path.home()
    local_env_file = Path(".env/options-monitor.env")
    default_install_prefix = home_dir / "apps" / "options-monitor"

    if key == "linux":
        return PlatformProfile(
            platform="linux",
            system=raw_system or "Linux",
            service_target="systemd",
            default_install_prefix=default_install_prefix,
            default_runtime_root=Path("/var/lib/options-monitor"),
            default_env_file=Path("/etc/options-monitor/options-monitor.env"),
            local_env_file=local_env_file,
            prerequisite_hints=(
                "Install git, python3, and python3 venv support before running scripts/install.sh.",
                "Use --with-server when this host will run Feishu long-connection inbound.",
            ),
            service_notes=(
                "Linux production services are rendered for systemd.",
                "systemd services should use an external env file such as /etc/options-monitor/options-monitor.env.",
            ),
        )

    if key == "darwin":
        runtime_root = home_dir / "Library" / "Application Support" / "options-monitor"
        return PlatformProfile(
            platform="macos",
            system=raw_system or "Darwin",
            service_target="launchd",
            default_install_prefix=default_install_prefix,
            default_runtime_root=runtime_root,
            default_env_file=runtime_root / "options-monitor.env",
            local_env_file=local_env_file,
            prerequisite_hints=(
                "Install Apple Command Line Tools with xcode-select --install, or install git/python through Homebrew.",
                "Use --with-server when this Mac will run Feishu long-connection inbound.",
            ),
            service_notes=(
                "Mac long-running jobs are rendered for launchd.",
                "launchd does not read shell profiles; pass the env file through OM_ENV_FILE via service render.",
                "Mac sleep and local timezone affect launchd reliability.",
            ),
        )

    fallback_runtime = home_dir / ".options-monitor"
    return PlatformProfile(
        platform="other",
        system=raw_system or "unknown",
        service_target="manual",
        default_install_prefix=default_install_prefix,
        default_runtime_root=fallback_runtime,
        default_env_file=fallback_runtime / "options-monitor.env",
        local_env_file=local_env_file,
        prerequisite_hints=(
            "Only Linux and macOS are first-class service platforms; run commands manually on this platform.",
        ),
        service_notes=("No managed service target is selected for this platform.",),
    )


def default_runtime_root_for_service_target(target: str, *, home: str | Path | None = None) -> Path:
    normalized = str(target or "").strip().lower()
    home_dir = Path(home).expanduser() if home is not None else Path.home()
    if normalized == "systemd":
        return Path("/var/lib/options-monitor")
    if normalized == "launchd":
        return home_dir / "Library" / "Application Support" / "options-monitor"
    raise ValueError(f"unsupported service target: {target}")


__all__ = [
    "PlatformName",
    "PlatformProfile",
    "ServiceTargetName",
    "current_platform_profile",
    "default_runtime_root_for_service_target",
]
