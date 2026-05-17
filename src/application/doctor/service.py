from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.doctor.ai_client import AiCompleteFn, maybe_run_ai_triage
from src.application.doctor.checks import run_deterministic_checks
from src.application.doctor.evidence import collect_evidence, redacted_evidence
from src.application.doctor.renderer import render_handoff


def doctor_tool(
    payload: dict[str, Any],
    *,
    runtime_status_tool_fn: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str], dict[str, Any]]],
    load_runtime_config: Callable[..., tuple[Path, dict[str, Any]]],
    repo_base: Callable[[], Path],
    mask_path: Callable[[Any], str | None],
    ai_complete_fn: AiCompleteFn | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    base = repo_base().resolve()
    _config_path, runtime_cfg = load_runtime_config(
        config_key=payload.get("config_key"),
        config_path=payload.get("config_path"),
    )
    evidence, warnings, meta = collect_evidence(
        payload,
        runtime_status_tool_fn=runtime_status_tool_fn,
        load_runtime_config=load_runtime_config,
        repo_base=repo_base,
        mask_path=mask_path,
        now_fn=now_fn,
    )
    diagnosis = run_deterministic_checks(evidence)
    safe_evidence = redacted_evidence(evidence)
    ai_result, ai_warnings, ai_meta = maybe_run_ai_triage(
        payload=payload,
        runtime_cfg=runtime_cfg,
        redacted_evidence=safe_evidence,
        diagnosis=diagnosis,
        ai_complete_fn=ai_complete_fn,
    )
    warnings.extend(ai_warnings)

    handoff_markdown = render_handoff(evidence=safe_evidence, diagnosis=diagnosis, ai_result=ai_result)
    outputs = _write_outputs(
        payload,
        base=base,
        evidence=safe_evidence,
        diagnosis=diagnosis,
        ai_result=ai_result,
        handoff_markdown=handoff_markdown,
        now_fn=now_fn,
    )

    result_status = _result_status(diagnosis=diagnosis, ai_result=ai_result)
    result_category = _result_category(diagnosis=diagnosis, ai_result=ai_result)
    data = {
        "schema_version": "doctor.v1",
        "status": result_status,
        "category": result_category,
        "deterministic": diagnosis,
        "ai": ai_result or {"status": "skipped", "category": "ai_skipped"},
        "handoff_markdown": handoff_markdown if _include_handoff(payload) else "",
        "outputs": outputs,
        "summary": {
            "status": result_status,
            "category": result_category,
            "finding_count": diagnosis.get("summary", {}).get("finding_count"),
            "issue_candidate_count": len(diagnosis.get("issue_candidates") or []),
            "ai_enabled": bool(ai_meta.get("enabled")),
            "ai_status": (ai_result or {}).get("status") or "skipped",
        },
    }
    meta.update({"ai": ai_meta, "outputs": outputs})
    return data, warnings, meta


def _write_outputs(
    payload: dict[str, Any],
    *,
    base: Path,
    evidence: dict[str, Any],
    diagnosis: dict[str, Any],
    ai_result: dict[str, Any] | None,
    handoff_markdown: str,
    now_fn: Callable[[], datetime] | None,
) -> dict[str, Any]:
    if not _truthy(payload.get("write_outputs")):
        return {"written": False}

    now = (now_fn or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    output_dir = _resolve_output_path(payload.get("doctor_output_dir") or payload.get("output_dir"), base=base, default=base / "output_shared" / "doctor")
    current_dir = _resolve_output_path(payload.get("doctor_current_dir"), base=base, default=base / "output_shared" / "state" / "current")
    output_dir.mkdir(parents=True, exist_ok=True)
    current_dir.mkdir(parents=True, exist_ok=True)

    config_key = str(_nested(evidence, "deployment", "config_key") or _nested(evidence, "input", "config_key") or "runtime").lower()
    run_id = str(_run_id(evidence) or now.strftime("%Y%m%dT%H%M%SZ")).replace("/", "_")
    stem = f"doctor-{config_key}-{run_id}"
    json_path = output_dir / f"{stem}.json"
    evidence_path = output_dir / f"{stem}.evidence.redacted.json"
    handoff_path = output_dir / f"{stem}.md"
    current_path = current_dir / "doctor.current.json"

    json_payload = {
        "schema_version": "doctor_output.v1",
        "status": _result_status(diagnosis=diagnosis, ai_result=ai_result),
        "category": _result_category(diagnosis=diagnosis, ai_result=ai_result),
        "diagnosis": diagnosis,
        "ai": ai_result,
        "handoff_path": _relative(handoff_path, base=base),
        "evidence_path": _relative(evidence_path, base=base),
    }
    _write_json(json_path, json_payload)
    _write_json(evidence_path, evidence)
    _write_json(current_path, {**json_payload, "doctor_path": _relative(json_path, base=base)})
    handoff_path.write_text(handoff_markdown, encoding="utf-8")
    return {
        "written": True,
        "doctor_path": _relative(json_path, base=base),
        "evidence_path": _relative(evidence_path, base=base),
        "handoff_path": _relative(handoff_path, base=base),
        "current_path": _relative(current_path, base=base),
    }


def _result_status(*, diagnosis: dict[str, Any], ai_result: dict[str, Any] | None) -> str:
    if isinstance(ai_result, dict) and ai_result.get("status") in {"ok", "warn", "fail"}:
        return str(ai_result["status"])
    return str(diagnosis.get("status") or "warn")


def _result_category(*, diagnosis: dict[str, Any], ai_result: dict[str, Any] | None) -> str:
    if isinstance(ai_result, dict) and ai_result.get("category") and ai_result.get("category") != "ai_unavailable":
        return str(ai_result["category"])
    return str(diagnosis.get("category") or "insufficient_evidence")


def _include_handoff(payload: dict[str, Any]) -> bool:
    output = str(payload.get("output") or "handoff").strip().lower()
    return output in {"handoff", "both", "markdown", "md"}


def _resolve_output_path(value: Any, *, base: Path, default: Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        path = default.resolve()
    else:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (base / path).resolve()
        else:
            path = path.resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="doctor output directories must stay under the repo root",
            details={"path": _relative(path, base=base)},
        ) from exc
    return path


def _relative(path: Path, *, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return f".../{path.name}" if path.name else "..."


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _nested(payload: Any, *keys: str) -> Any:
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _run_id(evidence: dict[str, Any]) -> Any:
    for path in (
        ("runtime_status", "latest_run", "state", "last_run", "json", "run_id"),
        ("runtime_status", "shared", "last_run", "json", "run_id"),
    ):
        value = _nested(evidence, *path)
        if value:
            return value
    latest_run_path = _nested(evidence, "runtime_status", "summary", "latest_run_path")
    if latest_run_path:
        return str(latest_run_path).rstrip("/").split("/")[-1]
    return None
