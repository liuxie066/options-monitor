from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from scripts.agent_plugin.contracts import AgentToolError


def _resolve_under_base(value: Any, *, base: Path, default: Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return default.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _relative_path(path: Path, *, base: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _mtime_utc(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _file_info(path: Path, *, base: Path) -> dict[str, Any]:
    out: dict[str, Any] = {
        "path": _relative_path(path, base=base),
        "exists": path.exists(),
    }
    if not path.exists():
        return out
    try:
        stat = path.stat()
    except Exception as exc:
        out["read_error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["size_bytes"] = int(stat.st_size)
    out["mtime_utc"] = _mtime_utc(path)
    out["is_file"] = path.is_file()
    return out


def _json_file_info(path: Path, *, base: Path, read_json_object_or_empty: Callable[[Path], dict[str, Any]]) -> dict[str, Any]:
    out = _file_info(path, base=base)
    if not out.get("exists") or not out.get("is_file", False):
        return out
    payload = read_json_object_or_empty(path)
    if payload:
        out["json"] = payload
    else:
        out["json"] = {}
    return out


def _text_file_info(path: Path, *, base: Path, max_chars: int) -> dict[str, Any]:
    out = _file_info(path, base=base)
    if not out.get("exists") or not out.get("is_file", False):
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        out["read_error"] = f"{type(exc).__name__}: {exc}"
        return out
    limit = max(0, min(int(max_chars), 20000))
    out["text"] = text[:limit]
    out["truncated"] = len(text) > limit
    out["line_count"] = len(text.splitlines())
    return out


def _read_text(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return None


def _latest_run_dir(base: Path, *, pointer_path: Path, runs_root: Path) -> Path | None:
    raw_pointer = _read_text(pointer_path)
    if raw_pointer:
        pointed = Path(raw_pointer).expanduser()
        if not pointed.is_absolute():
            pointed = (base / pointed).resolve()
        if pointed.exists() and pointed.is_dir():
            return pointed

    if not runs_root.exists() or not runs_root.is_dir():
        return None
    dirs = [item for item in runs_root.iterdir() if item.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda item: item.stat().st_mtime)


def _accounts_from_runtime(
    payload: dict[str, Any],
    cfg: dict[str, Any],
    *,
    normalize_accounts: Callable[..., list[str]],
    accounts_from_config: Callable[[dict[str, Any]], list[str]],
) -> list[str]:
    return normalize_accounts(payload.get("accounts"), fallback=tuple(accounts_from_config(cfg)))


def runtime_status_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config: Callable[..., tuple[Path, dict[str, Any]]],
    normalize_accounts: Callable[..., list[str]],
    accounts_from_config: Callable[[dict[str, Any]], list[str]],
    read_json_object_or_empty: Callable[[Path], dict[str, Any]],
    repo_base: Callable[[], Path],
    mask_path: Callable[[Any], str | None],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    base = repo_base().resolve()
    config_path, cfg = load_runtime_config(
        config_key=payload.get("config_key"),
        config_path=payload.get("config_path"),
    )
    accounts = _accounts_from_runtime(
        payload,
        cfg,
        normalize_accounts=normalize_accounts,
        accounts_from_config=accounts_from_config,
    )

    report_dir = _resolve_under_base(
        payload.get("report_dir"),
        base=base,
        default=base / "output" / "reports",
    )
    state_dir = _resolve_under_base(
        payload.get("state_dir"),
        base=base,
        default=base / "output" / "state",
    )
    shared_state_dir = _resolve_under_base(
        payload.get("shared_state_dir"),
        base=base,
        default=base / "output_shared" / "state",
    )
    accounts_root = _resolve_under_base(
        payload.get("accounts_root"),
        base=base,
        default=base / "output_accounts",
    )
    runs_root = _resolve_under_base(
        payload.get("runs_root"),
        base=base,
        default=base / "output_runs",
    )
    max_notification_chars = int(payload.get("max_notification_chars") or 4000)

    shared_last_run = _json_file_info(
        shared_state_dir / "last_run.json",
        base=base,
        read_json_object_or_empty=read_json_object_or_empty,
    )
    legacy_last_run = _json_file_info(
        state_dir / "last_run.json",
        base=base,
        read_json_object_or_empty=read_json_object_or_empty,
    )
    notification = _text_file_info(
        report_dir / "symbols_notification.txt",
        base=base,
        max_chars=max_notification_chars,
    )

    account_status: dict[str, Any] = {}
    for account in accounts:
        account_root = (accounts_root / account).resolve()
        account_status[account] = {
            "last_run": _json_file_info(
                account_root / "state" / "last_run.json",
                base=base,
                read_json_object_or_empty=read_json_object_or_empty,
            ),
            "notification": _text_file_info(
                account_root / "reports" / "symbols_notification.txt",
                base=base,
                max_chars=max_notification_chars,
            ),
        }

    pointer_path = shared_state_dir / "last_run_dir.txt"
    latest_run = _latest_run_dir(base, pointer_path=pointer_path, runs_root=runs_root)
    latest_run_payload: dict[str, Any] | None = None
    if latest_run is not None:
        run_accounts: dict[str, Any] = {}
        for account in accounts:
            run_account_root = latest_run / "accounts" / account
            run_accounts[account] = {
                "last_run": _json_file_info(
                    run_account_root / "state" / "last_run.json",
                    base=base,
                    read_json_object_or_empty=read_json_object_or_empty,
                ),
                "notification": _text_file_info(
                    run_account_root / "symbols_notification.txt",
                    base=base,
                    max_chars=max_notification_chars,
                ),
            }
        latest_run_payload = {
            "path": _relative_path(latest_run, base=base),
            "state": {
                "last_run": _json_file_info(
                    latest_run / "state" / "last_run.json",
                    base=base,
                    read_json_object_or_empty=read_json_object_or_empty,
                ),
                "tick_metrics": _json_file_info(
                    latest_run / "state" / "tick_metrics.json",
                    base=base,
                    read_json_object_or_empty=read_json_object_or_empty,
                ),
            },
            "accounts": run_accounts,
        }

    warnings: list[str] = []
    if not shared_last_run.get("exists") and not legacy_last_run.get("exists"):
        warnings.append("No last_run.json found under output_shared/state or output/state.")
    if not notification.get("exists") and not any(item["notification"].get("exists") for item in account_status.values()):
        warnings.append("No symbols_notification.txt found under output/reports or output_accounts/<account>/reports.")

    latest_status = None
    for candidate in (shared_last_run, legacy_last_run):
        payload_json = candidate.get("json") if isinstance(candidate.get("json"), dict) else {}
        latest_status = payload_json.get("status") or payload_json.get("last_status") or latest_status
        if latest_status:
            break

    data = {
        "config": {
            "config_path": mask_path(config_path),
            "accounts": accounts,
        },
        "paths": {
            "report_dir": _relative_path(report_dir, base=base),
            "state_dir": _relative_path(state_dir, base=base),
            "shared_state_dir": _relative_path(shared_state_dir, base=base),
            "accounts_root": _relative_path(accounts_root, base=base),
            "runs_root": _relative_path(runs_root, base=base),
        },
        "shared": {
            "last_run": shared_last_run,
            "legacy_last_run": legacy_last_run,
            "last_run_dir": _text_file_info(pointer_path, base=base, max_chars=1000),
            "notification": notification,
        },
        "accounts": account_status,
        "latest_run": latest_run_payload,
        "summary": {
            "ok": not warnings,
            "warning_count": len(warnings),
            "latest_status": latest_status,
        },
    }
    return data, warnings, {"config_path": mask_path(config_path)}


def _check_from_tuple(name: str, result: tuple[dict[str, Any], list[str], dict[str, Any]]) -> dict[str, Any]:
    data, warnings, meta = result
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    return {
        "name": name,
        "status": "ok" if bool(summary.get("ok", True)) and not warnings else ("warn" if warnings else "ok"),
        "message": "ok" if not warnings else "; ".join(warnings),
        "value": {
            "summary": summary,
            "meta": meta,
        },
    }


def openclaw_readiness_tool(
    payload: dict[str, Any],
    *,
    runtime_status_tool_fn: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str], dict[str, Any]]],
    healthcheck_tool_fn: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str], dict[str, Any]]],
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    openclaw_path = which("openclaw")
    if openclaw_path:
        checks.append(
            {
                "name": "openclaw_binary",
                "status": "ok",
                "message": "openclaw command found",
                "value": {"path": openclaw_path},
            }
        )
    else:
        checks.append(
            {
                "name": "openclaw_binary",
                "status": "warn",
                "message": "openclaw command not found on PATH",
            }
        )
        warnings.append("openclaw command not found on PATH; cron/message inspection may not be available.")

    runtime_status_data: dict[str, Any] = {}
    try:
        runtime_result = runtime_status_tool_fn(payload)
        runtime_status_data = runtime_result[0]
        runtime_check = _check_from_tuple("runtime_status", runtime_result)
        checks.append(runtime_check)
        if runtime_result[1]:
            warnings.extend(runtime_result[1])
    except AgentToolError as exc:
        checks.append(
            {
                "name": "runtime_status",
                "status": "error",
                "message": str(exc.message),
                "value": {"code": exc.code, "hint": exc.hint},
            }
        )
    except Exception as exc:
        checks.append(
            {
                "name": "runtime_status",
                "status": "error",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )

    try:
        healthcheck_result = healthcheck_tool_fn(payload)
        healthcheck_check = _check_from_tuple("healthcheck", healthcheck_result)
        healthcheck_summary = healthcheck_result[0].get("summary") if isinstance(healthcheck_result[0].get("summary"), dict) else {}
        if not bool(healthcheck_summary.get("ok", True)):
            healthcheck_check["status"] = "error"
            healthcheck_check["message"] = "healthcheck summary is not ok"
        checks.append(healthcheck_check)
        if healthcheck_result[1]:
            warnings.extend(healthcheck_result[1])
    except AgentToolError as exc:
        checks.append(
            {
                "name": "healthcheck",
                "status": "error",
                "message": str(exc.message),
                "value": {"code": exc.code, "hint": exc.hint},
            }
        )
    except Exception as exc:
        checks.append(
            {
                "name": "healthcheck",
                "status": "error",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )

    error_count = sum(1 for item in checks if item.get("status") == "error")
    warn_count = sum(1 for item in checks if item.get("status") == "warn")
    data = {
        "checks": checks,
        "runtime_status": runtime_status_data,
        "summary": {
            "ok": error_count == 0,
            "ready": error_count == 0,
            "error_count": error_count,
            "warning_count": warn_count + len(warnings),
        },
    }
    meta_config_path = None
    if isinstance(runtime_status_data.get("config"), dict):
        meta_config_path = runtime_status_data["config"].get("config_path")
    return data, warnings, {"config_path": meta_config_path}


__all__ = [
    "openclaw_readiness_tool",
    "runtime_status_tool",
]
