"""Execution wrapper shared by every migrated report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
import json

from src.application.agent_tool_contracts import AgentToolError
from src.application.reports.config_review import build_config_review
from src.application.reports.registry import ReportDefinition
from src.application.reports.store import (
    ReportArtifact,
    RunHandle,
    create_run,
    finalize_run,
    write_artifacts,
)


@dataclass(frozen=True)
class ReportRunContext:
    runtime_root: Path
    configs_root: Path
    prices_file: Path | None
    now: datetime


@dataclass(frozen=True)
class ReportBuild:
    artifacts: tuple[ReportArtifact, ...]
    summary: dict[str, Any]


ReportBuilder = Callable[[ReportRunContext], ReportBuild]


def _build_config_review(context: ReportRunContext) -> ReportBuild:
    result = build_config_review(
        configs_root=context.configs_root,
        runtime_root=context.runtime_root,
        prices_file=context.prices_file,
        now=context.now,
    )
    payload = json.dumps(result.payload, ensure_ascii=False, indent=2) + "\n"
    return ReportBuild(
        artifacts=(
            ReportArtifact(name="report.md", content=result.markdown),
            ReportArtifact(name="report.json", content=payload),
        ),
        summary=result.summary,
    )


RUNNERS: dict[str, ReportBuilder] = {
    "config_review": _build_config_review,
}


def execute_report(
    definition: ReportDefinition,
    context: ReportRunContext,
    *,
    command: Iterable[str],
    trigger: str = "manual",
    dry_run: bool = False,
) -> dict[str, Any]:
    if not definition.migrated:
        raise AgentToolError(
            code="REPORT_NOT_MIGRATED",
            message=f"report is registered but not migrated: {definition.report_id}",
            hint=f"Existing implementation: {definition.run_entry}",
            details={"report_id": definition.report_id, "migrated": False, "run_entry": definition.run_entry},
        )
    builder = RUNNERS.get(definition.report_id)
    if builder is None:
        raise AgentToolError(
            code="REPORT_RUNNER_MISSING",
            message=f"no runner is registered for migrated report: {definition.report_id}",
        )

    command_list = [str(item) for item in command]
    if dry_run:
        build = builder(context)
        return {
            "ok": True,
            "dry_run": True,
            "report_id": definition.report_id,
            "display_name": definition.display_name,
            "artifacts": [{"name": item.name, "content": item.content} for item in build.artifacts],
            "summary": build.summary,
        }

    try:
        handle = create_run(
            runtime_root=context.runtime_root,
            report_id=definition.report_id,
            now=context.now,
            command=command_list,
            trigger=trigger,
        )
    except OSError as exc:
        raise AgentToolError(
            code="REPORT_RUN_FAILED",
            message=f"cannot create report run directory: {definition.report_id}",
            details={"runtime_root": str(context.runtime_root), "error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    try:
        build = builder(context)
        records = write_artifacts(handle, build.artifacts)
        manifest = finalize_run(
            handle,
            status="succeeded",
            artifacts=records,
            summary=build.summary,
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        _record_failure(handle, exc)
        if isinstance(exc, AgentToolError):
            raise AgentToolError(
                code=exc.code,
                message=exc.message,
                hint=exc.hint,
                details={**(exc.details or {}), "run_id": handle.run_id, "run_dir": str(handle.run_dir)},
            ) from exc
        raise AgentToolError(
            code="REPORT_RUN_FAILED",
            message=f"report run failed: {definition.report_id}",
            details={
                "run_id": handle.run_id,
                "run_dir": str(handle.run_dir),
                "error": f"{type(exc).__name__}: {exc}",
            },
        ) from exc
    return {
        "ok": True,
        "dry_run": False,
        "report_id": definition.report_id,
        "display_name": definition.display_name,
        "run_id": handle.run_id,
        "run_dir": str(handle.run_dir.resolve()),
        "status": manifest["status"],
        "started_at_utc": manifest["started_at_utc"],
        "finished_at_utc": manifest["finished_at_utc"],
        "artifacts": manifest["artifacts"],
        "summary": manifest["summary"],
    }


def _record_failure(handle: RunHandle, exc: Exception) -> None:
    code = exc.code if isinstance(exc, AgentToolError) else "REPORT_RUN_FAILED"
    message = exc.message if isinstance(exc, AgentToolError) else f"{type(exc).__name__}: {exc}"
    try:
        finalize_run(
            handle,
            status="failed",
            artifacts=[],
            summary=None,
            error={"code": code, "message": message},
            finished_at=datetime.now().astimezone(),
        )
    except Exception:
        # Preserve the original failure; a broken manifest write must not mask it.
        pass


__all__ = [
    "RUNNERS",
    "ReportBuild",
    "ReportRunContext",
    "execute_report",
]
