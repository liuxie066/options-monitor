from __future__ import annotations

import os
from pathlib import Path

from src.application.agent_tool_config import repo_base


def default_quality_artifact_path() -> Path:
    runtime_root = str(os.environ.get("OM_RUNTIME_ROOT") or "").strip()
    root = Path(runtime_root).expanduser().resolve() if runtime_root else repo_base()
    return root / "output_shared" / "state" / "quality" / "status.v1.json"


def default_quality_control_path() -> Path:
    return default_quality_artifact_path().with_name("control_state.v1.json")


__all__ = ["default_quality_artifact_path", "default_quality_control_path"]
