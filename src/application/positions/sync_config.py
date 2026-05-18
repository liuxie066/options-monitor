from __future__ import annotations

from pathlib import Path
from typing import Any

from src.application.ledger.api import (
    SYNC_TO_FEISHU_OVERRIDE_ATTR,
    apply_position_ledger_runtime_config,
    effective_position_lot_mirror_sync_enabled,
    position_lot_mirror_sync_override_from_runtime_config,
)


def option_positions_sync_to_feishu_override_from_runtime_config(cfg: dict[str, Any] | None) -> bool | None:
    return position_lot_mirror_sync_override_from_runtime_config(cfg)


def apply_option_positions_runtime_config(repo: Any, cfg: dict[str, Any] | None) -> Any:
    return apply_position_ledger_runtime_config(repo, cfg)


def effective_option_positions_sync_to_feishu_enabled(
    *,
    data_config: Path,
    runtime_config: dict[str, Any] | None = None,
    repo: Any | None = None,
) -> bool:
    return effective_position_lot_mirror_sync_enabled(
        data_config=Path(data_config),
        runtime_config=runtime_config,
        repo=repo,
    )
