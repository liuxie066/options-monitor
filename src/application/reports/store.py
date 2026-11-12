"""Run storage for report-center-owned reports.

Layout::

    <runtime_root>/reports/<report_id>/<run_id>/run.json
    <runtime_root>/reports/<report_id>/<run_id>/report.md
    <runtime_root>/reports/<report_id>/<run_id>/report.json

``run_id`` starts with a UTC timestamp so lexical ordering is chronological.
Legacy reports are not written here; their history stays with the existing
implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Any, Iterable
from uuid import uuid4

from src.application.agent_tool_contracts import AgentToolError
from src.infrastructure.io_utils import atomic_write_json, atomic_write_text


RUN_MANIFEST_NAME = "run.json"
RUN_SCHEMA_VERSION = "report_run.v1"
_RUN_ID_RE = re.compile(r"^[0-9A-Za-z._-]+$")


@dataclass(frozen=True)
class RunHandle:
    report_id: str
    run_id: str
    run_dir: Path
    manifest_path: Path
    started_at_utc: str


@dataclass(frozen=True)
class ReportArtifact:
    name: str
    content: str

    def __post_init__(self) -> None:
        if not self.name or Path(self.name).name != self.name or self.name in {".", ".."}:
            raise ValueError(f"artifact name must be a flat file name: {self.name!r}")


@dataclass(frozen=True)
class ArtifactContent:
    name: str
    path: Path
    content: str
    record: dict[str, Any]


def reports_root(runtime_root: str | Path) -> Path:
    return Path(runtime_root).expanduser().resolve() / "reports"


def report_state_dir(runtime_root: str | Path, report_id: str) -> Path:
    return reports_root(runtime_root) / str(report_id)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_run_id(now: datetime) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid4().hex[:8]}"


def create_run(
    *,
    runtime_root: str | Path,
    report_id: str,
    now: datetime,
    command: Iterable[str],
    trigger: str = "manual",
) -> RunHandle:
    run_id = new_run_id(now)
    state_dir = report_state_dir(runtime_root, report_id)
    run_dir = state_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    handle = RunHandle(
        report_id=report_id,
        run_id=run_id,
        run_dir=run_dir,
        manifest_path=run_dir / RUN_MANIFEST_NAME,
        started_at_utc=utc_iso(now),
    )
    _write_manifest(
        handle,
        status="running",
        finished_at_utc=None,
        command=list(command),
        trigger=trigger,
        artifacts=[],
        summary=None,
        delivery=None,
        error=None,
    )
    return handle


def write_artifacts(handle: RunHandle, artifacts: Iterable[ReportArtifact]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for artifact in artifacts:
        if artifact.name in seen:
            raise ValueError(f"duplicate report artifact: {artifact.name}")
        seen.add(artifact.name)
        path = handle.run_dir / artifact.name
        atomic_write_text(path, artifact.content, encoding="utf-8")
        payload = artifact.content.encode("utf-8")
        records.append(
            {
                "name": artifact.name,
                "path": artifact.name,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return records


def finalize_run(
    handle: RunHandle,
    *,
    status: str,
    artifacts: list[dict[str, Any]] | None = None,
    summary: dict[str, Any] | None = None,
    delivery: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    command: Iterable[str] | None = None,
    trigger: str | None = None,
    finished_at: datetime | None = None,
) -> dict[str, Any]:
    if status not in {"succeeded", "failed"}:
        raise ValueError(f"unsupported terminal run status: {status}")
    existing = _read_manifest_file(handle.manifest_path)
    return _write_manifest(
        handle,
        status=status,
        finished_at_utc=utc_iso(finished_at or datetime.now(timezone.utc)),
        command=list(command if command is not None else existing.get("command") or []),
        trigger=str(trigger if trigger is not None else existing.get("trigger") or "manual"),
        artifacts=list(artifacts or []),
        summary=summary,
        delivery=delivery,
        error=error,
    )


def list_runs(
    runtime_root: str | Path,
    report_id: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    state_dir = report_state_dir(runtime_root, report_id)
    if not state_dir.is_dir():
        return []
    entries = sorted(
        (entry for entry in state_dir.iterdir() if entry.is_dir() and not entry.is_symlink()),
        key=lambda item: item.name,
        reverse=True,
    )
    runs: list[dict[str, Any]] = []
    for entry in entries:
        manifest = _read_manifest_file(entry / RUN_MANIFEST_NAME)
        if not manifest or manifest.get("report_id") != report_id:
            continue
        runs.append(_run_view(entry, manifest))
        if limit is not None and len(runs) >= limit:
            break
    return runs


def get_run(runtime_root: str | Path, report_id: str, run_id: str) -> dict[str, Any]:
    if not _RUN_ID_RE.fullmatch(str(run_id or "")):
        raise AgentToolError(code="REPORT_RUN_NOT_FOUND", message=f"report run not found: {run_id}")
    run_dir = report_state_dir(runtime_root, report_id) / run_id
    manifest = _read_manifest_file(run_dir / RUN_MANIFEST_NAME)
    if not manifest or manifest.get("report_id") != report_id:
        raise AgentToolError(
            code="REPORT_RUN_NOT_FOUND",
            message=f"report run not found: {report_id}/{run_id}",
            hint="Use `om report history <report_id>` to list available runs.",
        )
    return _run_view(run_dir, manifest)


def read_run_artifact(
    run: dict[str, Any],
    *,
    artifact_name: str | None = None,
) -> ArtifactContent:
    run_dir = Path(str(run["run_dir"]))
    records = [item for item in (run.get("artifacts") or []) if isinstance(item, dict)]
    if artifact_name:
        record = next((item for item in records if item.get("name") == artifact_name), None)
    else:
        record = next((item for item in records if str(item.get("name") or "").endswith(".md")), None)
        record = record or (records[0] if records else None)
    if record is None:
        raise AgentToolError(
            code="REPORT_ARTIFACT_NOT_FOUND",
            message=f"report run has no artifact: {artifact_name or '<default>'}",
            details={"run_id": run.get("run_id"), "available": [item.get("name") for item in records]},
        )
    relpath = str(record.get("path") or record.get("name") or "")
    if not relpath or Path(relpath).name != relpath or relpath in {".", ".."}:
        raise AgentToolError(code="REPORT_ARTIFACT_NOT_FOUND", message=f"invalid artifact path: {relpath!r}")
    path = run_dir / relpath
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AgentToolError(
            code="REPORT_ARTIFACT_NOT_FOUND",
            message=f"artifact file is missing: {relpath}",
            details={"run_id": run.get("run_id")},
        ) from exc
    except UnicodeDecodeError as exc:
        raise AgentToolError(
            code="REPORT_ARTIFACT_NOT_TEXT",
            message=f"artifact is not UTF-8 text: {relpath}",
            details={"run_id": run.get("run_id")},
        ) from exc
    return ArtifactContent(name=str(record.get("name") or relpath), path=path, content=content, record=record)


def _write_manifest(
    handle: RunHandle,
    *,
    status: str,
    finished_at_utc: str | None,
    command: list[str],
    trigger: str,
    artifacts: list[dict[str, Any]],
    summary: dict[str, Any] | None,
    delivery: dict[str, Any] | None,
    error: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = {
        "schema_version": RUN_SCHEMA_VERSION,
        "report_id": handle.report_id,
        "run_id": handle.run_id,
        "status": status,
        "started_at_utc": handle.started_at_utc,
        "finished_at_utc": finished_at_utc,
        "trigger": trigger,
        "command": command,
        "artifacts": artifacts,
        "summary": summary,
        "delivery": delivery,
        "error": error,
    }
    atomic_write_json(handle.manifest_path, payload)
    return payload


def _read_manifest_file(path: Path) -> dict[str, Any] | None:
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _run_view(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    return {**manifest, "run_dir": str(run_dir.resolve())}


__all__ = [
    "ArtifactContent",
    "RUN_MANIFEST_NAME",
    "ReportArtifact",
    "RunHandle",
    "create_run",
    "finalize_run",
    "get_run",
    "list_runs",
    "new_run_id",
    "read_run_artifact",
    "report_state_dir",
    "reports_root",
    "utc_iso",
    "write_artifacts",
]
