from __future__ import annotations

"""Offline evidence collection side lane for Research / Shadow Replay."""

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base


def research_tool(
    payload: dict[str, Any],
    *,
    runtime_status_tool_fn: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str], dict[str, Any]]],
    load_runtime_config: Callable[..., tuple[Path, dict[str, Any]]],
    repo_base: Callable[[], Path],
    mask_path: Callable[[Any], str | None],
    healthcheck_tool_fn: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str], dict[str, Any]]] | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.research.service import research_tool as _research_tool

    return _research_tool(
        payload,
        runtime_status_tool_fn=runtime_status_tool_fn,
        load_runtime_config=load_runtime_config,
        repo_base=repo_base,
        mask_path=mask_path,
        healthcheck_tool_fn=healthcheck_tool_fn,
        now_fn=now_fn,
    )


def run_research_collect(
    payload: dict[str, Any],
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
) -> dict[str, Any]:
    from src.application.research.facade import run_research_collect as _run_research_collect

    return _run_research_collect(payload, repo_base_fn=repo_base_fn)

__all__ = ["research_tool", "run_research_collect"]
