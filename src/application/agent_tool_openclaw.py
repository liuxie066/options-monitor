from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tool_runtime_status import _merge_openclaw_profile, _relative_path
from src.application.notification_delivery_route import resolve_notification_delivery_route
from domain.domain.multi_tick import (
    FEISHU_APP_NOTIFICATION_PROVIDER,
    OPENCLAW_NOTIFICATION_PROVIDER,
    is_supported_notification_provider,
    resolve_openclaw_transport_channel,
)


def _check_from_tuple(name: str, result: tuple[dict[str, Any], list[str], dict[str, Any]]) -> dict[str, Any]:
    data, warnings, meta = result
    summary_raw = data.get("summary")
    summary: dict[str, Any] = summary_raw if isinstance(summary_raw, dict) else {}
    return {
        "name": name,
        "status": "ok" if bool(summary.get("ok", True)) and not warnings else ("warn" if warnings else "ok"),
        "message": "ok" if not warnings else "; ".join(warnings),
        "value": {
            "summary": summary,
            "meta": meta,
        },
    }


def _snippet(value: Any, *, max_chars: int = 2000) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...[truncated]"


def _normalize_cron_jobs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    jobs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        schedule = str(item.get("schedule") or "").strip()
        if job_id or name:
            jobs.append({k: v for k, v in {"id": job_id, "name": name, "schedule": schedule}.items() if v})
    return jobs


def _run_openclaw_command(
    args: list[str],
    *,
    run_cmd: Callable[..., Any],
    timeout_sec: int,
) -> dict[str, Any]:
    try:
        proc = run_cmd(
            ["openclaw", *args],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": _snippet(exc.stdout),
            "stderr": _snippet(exc.stderr),
            "error": "timeout",
        }
    except Exception as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": int(getattr(proc, "returncode", 1)) == 0,
        "returncode": int(getattr(proc, "returncode", 1)),
        "stdout": _snippet(getattr(proc, "stdout", "")),
        "stderr": _snippet(getattr(proc, "stderr", "")),
        "error": None,
    }


def _cron_check(
    payload: dict[str, Any],
    *,
    openclaw_path: str | None,
    run_cmd: Callable[..., Any],
) -> dict[str, Any]:
    jobs = _normalize_cron_jobs(payload.get("cron_jobs"))
    include = bool(payload.get("include_cron_status", False)) or bool(jobs)
    if not include:
        return {
            "name": "openclaw_cron",
            "status": "skipped",
            "message": "cron status skipped; set include_cron_status=true or provide cron_jobs in the OpenClaw profile",
            "value": {"configured_jobs": jobs},
        }
    if not openclaw_path:
        return {
            "name": "openclaw_cron",
            "status": "warn",
            "message": "openclaw command not found; cron status unavailable",
            "value": {"configured_jobs": jobs},
        }

    timeout_sec = max(1, min(int(payload.get("openclaw_command_timeout_sec") or 20), 120))
    list_result = _run_openclaw_command(["cron", "list"], run_cmd=run_cmd, timeout_sec=timeout_sec)
    runs_result = _run_openclaw_command(["cron", "runs"], run_cmd=run_cmd, timeout_sec=timeout_sec)
    list_text = f"{list_result.get('stdout') or ''}\n{list_result.get('stderr') or ''}"
    matched: list[dict[str, Any]] = []
    for job in jobs:
        job_id = str(job.get("id") or "")
        name = str(job.get("name") or "")
        found = bool((job_id and job_id in list_text) or (name and name in list_text))
        matched.append({**job, "found": found})
    missing = [item for item in matched if not item.get("found")]
    status = "ok" if list_result.get("ok") and runs_result.get("ok") and not missing else "warn"
    message = "cron list/runs available"
    if missing:
        message = "configured cron job not found in openclaw cron list output"
    elif not list_result.get("ok") or not runs_result.get("ok"):
        message = "openclaw cron command returned a non-zero status"
    return {
        "name": "openclaw_cron",
        "status": status,
        "message": message,
        "value": {
            "configured_jobs": matched,
            "list": list_result,
            "runs": runs_result,
        },
    }


def _notification_route_check(cfg: dict[str, Any], *, openclaw_path: str | None) -> dict[str, Any]:
    notifications = cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
    if not notifications:
        return {
            "name": "notification_route",
            "status": "warn",
            "message": "notifications config is absent; live tick can generate reports but cannot send notifications",
            "value": {"configured": False},
        }
    route = resolve_notification_delivery_route(config=cfg)
    provider = str(route.get("provider") or "")
    channel = str(route.get("channel") or "")
    target = str(route.get("target") or "").strip()
    if not is_supported_notification_provider(provider):
        return {
            "name": "notification_route",
            "status": "error",
            "message": f"unsupported notifications.provider: {provider}",
            "value": {"configured": True, "provider": provider, "channel": channel, "target_configured": bool(target)},
        }
    if not target:
        message = "Feishu bot user open_id is missing" if provider == FEISHU_APP_NOTIFICATION_PROVIDER else "notifications.target is missing"
        return {
            "name": "notification_route",
            "status": "error",
            "message": message,
            "value": {"configured": True, "provider": provider, "channel": channel, "target_configured": False},
        }
    transport_channel = resolve_openclaw_transport_channel(channel) if provider == OPENCLAW_NOTIFICATION_PROVIDER else channel
    status = "ok"
    message = "notification route configured"
    if provider == OPENCLAW_NOTIFICATION_PROVIDER and not openclaw_path:
        status = "warn"
        message = "openclaw notification provider is configured but openclaw command is not on PATH"
    return {
        "name": "notification_route",
        "status": status,
        "message": message,
        "value": {
            "configured": True,
            "provider": provider,
            "channel": channel,
            "transport_channel": transport_channel,
            "target_configured": True,
        },
    }


def _freshness_check(runtime_status_data: dict[str, Any]) -> dict[str, Any]:
    freshness_raw = runtime_status_data.get("freshness")
    freshness: dict[str, Any] = freshness_raw if isinstance(freshness_raw, dict) else {}
    status = str(freshness.get("status") or "unknown")
    if status == "fresh":
        check_status = "ok"
        message = "runtime output is fresh"
    elif status == "stale":
        check_status = "warn"
        message = "runtime output is stale"
    else:
        check_status = "warn"
        message = "runtime output freshness is unknown"
    return {
        "name": "runtime_freshness",
        "status": check_status,
        "message": message,
        "value": freshness,
    }


def _command_input(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    profile_path = str(payload.get("profile_path") or "").strip()
    if profile_path:
        out["profile_path"] = profile_path
    if str(payload.get("config_key") or "").strip():
        out["config_key"] = str(payload.get("config_key")).strip()
    elif payload.get("config_path"):
        out["config_path"] = str(payload.get("config_path"))
    return out


def _tick_command(payload: dict[str, Any]) -> list[str]:
    command = ["./om", "run", "tick"]
    if payload.get("config_path"):
        command.extend(["--config", str(payload.get("config_path"))])
    else:
        config_key = str(payload.get("config_key") or "").strip().lower()
        config_ref = f"config.{config_key}.json" if config_key in {"us", "hk"} else "<runtime-config>"
        command.extend(["--config", config_ref])
    accounts = payload.get("accounts")
    account_values = [str(item).strip() for item in accounts if str(item).strip()] if isinstance(accounts, list) else []
    if account_values:
        command.append("--accounts")
        command.extend(account_values)
    else:
        command.extend(["--accounts", "<accounts>"])
    return command


def _safe_agent_command(tool_name: str, payload: dict[str, Any]) -> list[str]:
    return ["./om-agent", "run", "--tool", tool_name, "--input-json", json.dumps(_command_input(payload), ensure_ascii=False)]


def _build_next_actions(checks: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    by_name = {str(item.get("name")): item for item in checks}
    safe: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = [
        {
            "action": "live_tick",
            "reason": "requires explicit user request because it writes runtime output and may send notifications",
            "command": _tick_command(payload),
        },
        {
            "action": "notification_send",
            "reason": "requires explicit user request because it sends a real message",
        },
    ]

    if by_name.get("runtime_status", {}).get("status") in {"error", "warn"}:
        safe.append(
            {
                "action": "inspect_runtime_status",
                "reason": by_name["runtime_status"].get("message"),
                "command": _safe_agent_command("runtime_status", payload),
            }
        )
    if by_name.get("healthcheck", {}).get("status") in {"error", "warn"}:
        safe.append(
            {
                "action": "run_healthcheck",
                "reason": by_name["healthcheck"].get("message"),
                "command": _safe_agent_command("healthcheck", payload),
            }
        )
    if by_name.get("openclaw_cron", {}).get("status") == "warn":
        safe.append(
            {
                "action": "inspect_openclaw_cron",
                "reason": by_name["openclaw_cron"].get("message"),
                "command": ["openclaw", "cron", "list"],
            }
        )
    if by_name.get("notification_route", {}).get("status") in {"error", "warn"}:
        safe.append(
            {
                "action": "fix_notification_config",
                "reason": by_name["notification_route"].get("message"),
                "command": _safe_agent_command("config_validate", payload),
            }
        )
    if by_name.get("runtime_freshness", {}).get("status") == "warn":
        safe.append(
            {
                "action": "review_last_runtime_output",
                "reason": by_name["runtime_freshness"].get("message"),
                "command": _safe_agent_command("runtime_status", payload),
            }
        )
    if not safe:
        safe.append(
            {
                "action": "no_read_only_followup_needed",
                "reason": "readiness checks did not identify a required safe follow-up",
            }
        )
    return {"safe_next_actions": safe, "blocked_actions": blocked}


def openclaw_readiness_tool(
    payload: dict[str, Any],
    *,
    runtime_status_tool_fn: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str], dict[str, Any]]],
    healthcheck_tool_fn: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str], dict[str, Any]]],
    load_runtime_config: Callable[..., tuple[Path, dict[str, Any]]] | None = None,
    repo_base: Callable[[], Path] | None = None,
    mask_path: Callable[[Any], str | None] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run_cmd: Callable[..., Any] = subprocess.run,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    base = (repo_base() if repo_base is not None else Path.cwd()).resolve()
    payload, profile_meta = _merge_openclaw_profile(payload, base=base)
    mask_path = mask_path or (lambda value: _relative_path(Path(value), base=base) if value is not None else None)
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    openclaw_path = which("openclaw")
    if openclaw_path:
        checks.append(
            {
                "name": "openclaw_binary",
                "status": "ok",
                "message": "openclaw command found",
                "value": {"path": _relative_path(Path(openclaw_path), base=base)},
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
        healthcheck_summary_raw = healthcheck_result[0].get("summary")
        healthcheck_summary: dict[str, Any] = healthcheck_summary_raw if isinstance(healthcheck_summary_raw, dict) else {}
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

    if runtime_status_data:
        checks.append(_freshness_check(runtime_status_data))

    if load_runtime_config is not None:
        try:
            config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
            checks.append(_notification_route_check(cfg, openclaw_path=openclaw_path))
        except AgentToolError as exc:
            checks.append(
                {
                    "name": "notification_route",
                    "status": "error",
                    "message": str(exc.message),
                    "value": {"code": exc.code, "hint": exc.hint},
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "name": "notification_route",
                    "status": "error",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )

    checks.append(_cron_check(payload, openclaw_path=openclaw_path, run_cmd=run_cmd))

    error_count = sum(1 for item in checks if item.get("status") == "error")
    warn_count = sum(1 for item in checks if item.get("status") == "warn")
    next_actions = _build_next_actions(checks, payload)
    data = {
        "checks": checks,
        "runtime_status": runtime_status_data,
        "openclaw_profile": profile_meta or {"loaded": False},
        "next_actions": next_actions,
        "summary": {
            "ok": error_count == 0,
            "ready": error_count == 0,
            "error_count": error_count,
            "warning_count": warn_count + len(warnings),
            "safe_next_action_count": len(next_actions["safe_next_actions"]),
        },
    }
    meta_config_path = None
    if isinstance(runtime_status_data.get("config"), dict):
        meta_config_path = runtime_status_data["config"].get("config_path")
    return data, warnings, {"config_path": meta_config_path}


__all__ = [
    "openclaw_readiness_tool",
]
