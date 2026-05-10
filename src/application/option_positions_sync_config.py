from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.option_positions_core.service import option_positions_sync_to_feishu_enabled


SYNC_TO_FEISHU_OVERRIDE_ATTR = "option_positions_sync_to_feishu_enabled_override"


def option_positions_sync_to_feishu_override_from_runtime_config(cfg: dict[str, Any] | None) -> bool | None:
    data = cfg if isinstance(cfg, dict) else {}
    option_positions = data.get("option_positions")
    if option_positions is None:
        return None
    if not isinstance(option_positions, dict):
        raise ValueError("option_positions must be an object")
    sync_to_feishu = option_positions.get("sync_to_feishu")
    if sync_to_feishu is None:
        return None
    if not isinstance(sync_to_feishu, dict):
        raise ValueError("option_positions.sync_to_feishu must be an object")
    if "enabled" not in sync_to_feishu or sync_to_feishu.get("enabled") is None:
        return None
    enabled = sync_to_feishu.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("option_positions.sync_to_feishu.enabled must be a boolean")
    return bool(enabled)


def apply_option_positions_runtime_config(repo: Any, cfg: dict[str, Any] | None) -> Any:
    override = option_positions_sync_to_feishu_override_from_runtime_config(cfg)
    if override is not None:
        setattr(repo, SYNC_TO_FEISHU_OVERRIDE_ATTR, bool(override))
    return repo


def effective_option_positions_sync_to_feishu_enabled(
    *,
    data_config: Path,
    runtime_config: dict[str, Any] | None = None,
    repo: Any | None = None,
) -> bool:
    runtime_override = option_positions_sync_to_feishu_override_from_runtime_config(runtime_config)
    if runtime_override is not None:
        return bool(runtime_override)

    if repo is not None:
        repo_override = getattr(repo, SYNC_TO_FEISHU_OVERRIDE_ATTR, None)
        if isinstance(repo_override, bool):
            return bool(repo_override)

    return option_positions_sync_to_feishu_enabled(Path(data_config))
