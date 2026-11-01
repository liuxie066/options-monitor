from __future__ import annotations

from pathlib import Path

from src.application.agent_tool_config import repo_base
from src.application.runtime_paths import resolve_runtime_root


def default_quality_artifact_path() -> Path:
    root = resolve_runtime_root(repo_root=repo_base()).runtime_root
    return root / "output_shared" / "state" / "quality" / "status.v1.json"


def default_quality_control_path() -> Path:
    return default_quality_artifact_path().with_name("control_state.v1.json")


def default_quality_integrity_artifact_path() -> Path:
    return default_quality_artifact_path().with_name("integrity_status.v1.json")


def default_quality_hot_path_cutover_receipt_path() -> Path:
    return default_quality_artifact_path().with_name("current_hot_path_cutover.v1.json")


__all__ = [
    "default_quality_artifact_path",
    "default_quality_control_path",
    "default_quality_hot_path_cutover_receipt_path",
    "default_quality_integrity_artifact_path",
]
