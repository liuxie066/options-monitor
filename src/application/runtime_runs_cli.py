from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError


SCHEMA_VERSION = "runtime_runs.v1"


def collect_runtime_runs(
    *,
    repo_root: Path,
    runs_root: str | Path | None = None,
    profile_path: str | Path | None = None,
    limit: int = 10,
    run_id: str | None = None,
    run_dir: str | Path | None = None,
    scanned_only: bool = False,
) -> dict[str, Any]:
    base = repo_root.resolve()
    root = resolve_runtime_runs_root(base=base, runs_root=runs_root, profile_path=profile_path)
    requested = bool(str(run_id or "").strip() or str(run_dir or "").strip())
    selected_dir = _selected_run_dir(root=root, base=base, run_id=run_id, run_dir=run_dir)
    all_dirs = _run_dirs(root)
    runs = [_run_summary(path, base=base) for path in all_dirs]
    if scanned_only:
        runs = [item for item in runs if item.get("ran_scan") is True]

    selected_run = None
    requested_found = None
    requested_value = str(run_dir or run_id or "").strip() or None
    if requested:
        requested_found = selected_dir is not None and selected_dir.exists() and selected_dir.is_dir()
        selected_run = _run_summary(selected_dir, base=base) if requested_found and selected_dir is not None else None

    returned = runs if limit <= 0 else runs[: max(limit, 0)]
    ok = not requested or bool(requested_found)
    return {
        "schema_version": SCHEMA_VERSION,
        "runs_root": str(root),
        "runs_root_display": _display_path(root, base=base),
        "summary": {
            "ok": ok,
            "runs_root_exists": root.exists() and root.is_dir(),
            "total_count": len(all_dirs),
            "returned_count": len(returned),
            "limit": limit,
            "scanned_only": scanned_only,
            "requested": requested,
            "requested_value": requested_value,
            "requested_found": requested_found,
        },
        "runs": returned,
        "selected_run": selected_run,
    }


def format_runtime_runs(data: dict[str, Any]) -> str:
    summary = _dict(data.get("summary"))
    selected = _dict(data.get("selected_run"))
    if selected:
        return _format_selected_run(data, selected)

    lines = [
        "options-monitor runs",
        f"runs_root: {_value(data.get('runs_root_display') or data.get('runs_root'))}",
        (
            "count: "
            f"{_value(summary.get('returned_count'))}/{_value(summary.get('total_count'))} "
            f"limit={_value(summary.get('limit'))} "
            f"scanned_only={_yes_no(summary.get('scanned_only'))}"
        ),
        "",
    ]
    if summary.get("requested") and summary.get("requested_found") is False:
        lines.append(f"requested: not found {_value(summary.get('requested_value'))}")
        lines.append("")

    runs = _list(data.get("runs"))
    if not runs:
        lines.append("no runs found")
        return "\n".join(lines).rstrip() + "\n"

    for item in runs:
        run = _dict(item)
        lines.append(
            "- "
            f"{_value(run.get('run_id'))} "
            f"mtime={_value(run.get('mtime_utc'))} "
            f"status={_value(run.get('status'))} "
            f"scan={_yes_no(run.get('ran_scan'))} "
            f"sent={_yes_no(run.get('sent'))} "
            f"accounts={_csv(run.get('accounts'))} "
            f"reason={_value(run.get('reason'))}"
        )
    return "\n".join(lines).rstrip() + "\n"


def _format_selected_run(data: dict[str, Any], run: dict[str, Any]) -> str:
    files = _dict(run.get("files"))
    scheduler = _dict(run.get("scheduler"))
    lines = [
        f"options-monitor run {run.get('run_id')}",
        f"path: {_value(run.get('path_display') or run.get('path'))}",
        f"mtime: {_value(run.get('mtime_utc'))}",
        f"kind: {_value(run.get('kind'))}",
        f"status: {_value(run.get('status'))}",
        f"scan: {_yes_no(run.get('ran_scan'))}",
        f"sent: {_yes_no(run.get('sent'))}",
        f"accounts: {_csv(run.get('accounts'))}",
        f"reason: {_value(run.get('reason'))}",
        (
            "scheduler: "
            f"should_run_scan={_yes_no(scheduler.get('should_run_scan'))} "
            f"should_notify={_yes_no(scheduler.get('should_notify'))} "
            f"reason={_value(scheduler.get('reason'))}"
        ),
        (
            "files: "
            f"last_run={_yes_no(files.get('last_run'))} "
            f"tick_metrics={_yes_no(files.get('tick_metrics'))} "
            f"scheduler_decision={_yes_no(files.get('scheduler_decision'))} "
            f"audit={_yes_no(files.get('audit_events'))} "
            f"tool_audit={_yes_no(files.get('tool_execution_audit'))}"
        ),
        f"runs_root: {_value(data.get('runs_root_display') or data.get('runs_root'))}",
    ]
    return "\n".join(lines).rstrip() + "\n"


def resolve_runtime_runs_root(*, base: Path, runs_root: str | Path | None, profile_path: str | Path | None) -> Path:
    if runs_root:
        return _resolve_path(runs_root, base=base)
    profile = _load_profile(profile_path, base=base) if profile_path else {}
    paths = _dict(profile.get("paths"))
    raw = paths.get("runs_root") or profile.get("runs_root")
    if raw:
        return _resolve_path(raw, base=base)
    runtime_root = profile.get("runtime_root")
    if runtime_root:
        return (_resolve_path(runtime_root, base=base) / "output_runs").resolve()
    return (base / "output_runs").resolve()


def _load_profile(profile_path: str | Path, *, base: Path) -> dict[str, Any]:
    path = _resolve_path(profile_path, base=base)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"profile not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"profile is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"profile must be a JSON object: {path}")
    return payload


def _resolve_path(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path.resolve()


def _selected_run_dir(*, root: Path, base: Path, run_id: str | None, run_dir: str | Path | None) -> Path | None:
    raw_run_dir = str(run_dir or "").strip()
    if raw_run_dir:
        return _resolve_path(raw_run_dir, base=base)
    raw_run_id = str(run_id or "").strip()
    if not raw_run_id:
        return None
    candidate = Path(raw_run_id)
    if candidate.is_absolute() or candidate.name != raw_run_id:
        return None
    return (root / raw_run_id).resolve()


def _run_dirs(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    return sorted(
        [item for item in root.iterdir() if item.is_dir()],
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    )


def _run_summary(path: Path, *, base: Path) -> dict[str, Any]:
    state = path / "state"
    last_run = _read_json(state / "last_run.json")
    tick_metrics = _read_json(state / "tick_metrics.json")
    scheduler_file = _read_json(state / "scheduler_decision.json")
    scheduler = _scheduler_summary(tick_metrics=tick_metrics, scheduler_file=scheduler_file)
    accounts = _accounts(last_run=last_run, tick_metrics=tick_metrics, run_dir=path)
    ran_scan = _ran_scan(last_run=last_run, tick_metrics=tick_metrics)
    sent = tick_metrics.get("sent") if tick_metrics else last_run.get("sent")
    reason = _reason(last_run=last_run, tick_metrics=tick_metrics, scheduler=scheduler)
    return {
        "run_id": path.name,
        "path": str(path),
        "path_display": _display_path(path, base=base),
        "mtime_utc": _mtime_utc(path),
        "kind": _kind(last_run=last_run, tick_metrics=tick_metrics),
        "status": _status(last_run=last_run, tick_metrics=tick_metrics, scheduler=scheduler, ran_scan=ran_scan),
        "ran_scan": ran_scan,
        "sent": sent if isinstance(sent, bool) else None,
        "accounts": accounts,
        "reason": reason,
        "scheduler": scheduler,
        "files": {
            "last_run": (state / "last_run.json").exists(),
            "tick_metrics": (state / "tick_metrics.json").exists(),
            "scheduler_decision": (state / "scheduler_decision.json").exists(),
            "audit_events": (state / "audit_events.jsonl").exists(),
            "tool_execution_audit": (state / "tool_execution_audit.jsonl").exists(),
        },
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _scheduler_summary(*, tick_metrics: dict[str, Any], scheduler_file: dict[str, Any]) -> dict[str, Any]:
    scheduler = _dict(tick_metrics.get("scheduler_decision"))
    if not scheduler:
        scheduler = _dict(_dict(scheduler_file.get("payload")).get("decision"))
    return {
        "should_run_scan": scheduler.get("should_run_scan"),
        "should_notify": scheduler.get("should_notify"),
        "reason": scheduler.get("reason"),
        "next_run_utc": scheduler.get("next_run_utc"),
        "now_utc": scheduler.get("now_utc"),
    }


def _accounts(*, last_run: dict[str, Any], tick_metrics: dict[str, Any], run_dir: Path) -> list[str]:
    out: list[str] = []
    raw_tick_accounts = tick_metrics.get("accounts")
    if isinstance(raw_tick_accounts, list):
        for item in raw_tick_accounts:
            if isinstance(item, dict) and item.get("account"):
                out.append(str(item["account"]))
    raw_last_accounts = last_run.get("accounts")
    if isinstance(raw_last_accounts, list):
        out.extend(str(item) for item in raw_last_accounts if item)
    accounts_dir = run_dir / "accounts"
    if accounts_dir.exists() and accounts_dir.is_dir():
        out.extend(item.name for item in accounts_dir.iterdir() if item.is_dir())
    return sorted(dict.fromkeys(out))


def _ran_scan(*, last_run: dict[str, Any], tick_metrics: dict[str, Any]) -> bool:
    if tick_metrics.get("ran_scan") is True or last_run.get("ran_scan") is True:
        return True
    raw_accounts = tick_metrics.get("accounts")
    if isinstance(raw_accounts, list):
        return any(isinstance(item, dict) and item.get("ran_scan") is True for item in raw_accounts)
    if isinstance(raw_accounts, dict):
        return any(isinstance(item, dict) and item.get("ran_scan") is True for item in raw_accounts.values())
    return False


def _kind(*, last_run: dict[str, Any], tick_metrics: dict[str, Any]) -> str:
    if last_run.get("schema_kind"):
        return str(last_run["schema_kind"])
    if tick_metrics:
        return "tick"
    return "unknown"


def _status(
    *,
    last_run: dict[str, Any],
    tick_metrics: dict[str, Any],
    scheduler: dict[str, Any],
    ran_scan: bool,
) -> str | None:
    if last_run.get("status"):
        return str(last_run["status"])
    if tick_metrics.get("status"):
        return str(tick_metrics["status"])
    if ran_scan:
        return "scan"
    if scheduler.get("should_run_scan") is False:
        return "skipped"
    if tick_metrics.get("reason"):
        return str(tick_metrics["reason"])
    return None


def _reason(*, last_run: dict[str, Any], tick_metrics: dict[str, Any], scheduler: dict[str, Any]) -> str | None:
    for value in (
        last_run.get("reason"),
        _nested(last_run, "result", "reason"),
        tick_metrics.get("reason"),
        scheduler.get("reason"),
    ):
        if value:
            return str(value)
    account_results = last_run.get("account_results")
    if isinstance(account_results, list):
        for item in account_results:
            if isinstance(item, dict):
                reason = _nested(item, "result", "reason")
                if reason:
                    return str(reason)
    return None


def _mtime_utc(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _display_path(path: Path, *, base: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _nested(data: dict[str, Any], *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _csv(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "-"
    if value is None:
        return "-"
    return str(value)


def _value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def _yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "-"
