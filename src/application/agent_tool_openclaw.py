from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from domain.domain.multi_tick import (
    OPENCLAW_NOTIFICATION_PROVIDER,
    is_supported_notification_provider,
    resolve_notification_route_from_config,
    resolve_openclaw_transport_channel,
)
from src.application.trade_account_mapping import resolve_trade_intake_config


PROFILE_PATH_KEYS = ("report_dir", "state_dir", "shared_state_dir", "accounts_root", "runs_root")
DEFAULT_PROFILE_NAMES = ("openclaw.profile.json", ".openclaw-profile.json")


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
        name = resolved.name
        return f".../{name}" if name else "..."


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


def _path_from_config(value: Any, *, base: Path) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _trade_intake_summary(state_json: dict[str, Any], status_json: dict[str, Any]) -> dict[str, Any]:
    processed_raw = state_json.get("processed_deal_ids")
    failed_raw = state_json.get("failed_deal_ids")
    unresolved_raw = state_json.get("unresolved_deal_ids")
    processed: dict[str, Any] = processed_raw if isinstance(processed_raw, dict) else {}
    failed: dict[str, Any] = failed_raw if isinstance(failed_raw, dict) else {}
    unresolved: dict[str, Any] = unresolved_raw if isinstance(unresolved_raw, dict) else {}
    receipt_items: list[dict[str, Any]] = []
    for bucket in (processed, failed, unresolved):
        for item in bucket.values():
            receipt = item.get("receipt") if isinstance(item, dict) else None
            if isinstance(receipt, dict):
                receipt_items.append(receipt)
    return {
        "listener_status": status_json.get("status"),
        "listener_stage": status_json.get("stage"),
        "last_heartbeat_utc": status_json.get("last_heartbeat_utc"),
        "last_deal_result": status_json.get("last_deal_result"),
        "last_receipt_result": status_json.get("last_receipt_result"),
        "processed_count": len(processed),
        "failed_count": len(failed),
        "unresolved_count": len(unresolved),
        "receipt_count": len(receipt_items),
        "receipt_confirmed_count": sum(1 for item in receipt_items if bool(item.get("delivery_confirmed"))),
        "receipt_failed_count": sum(1 for item in receipt_items if str(item.get("status") or "") in {"failed", "unconfirmed"}),
    }


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


def _path_pointer_file_info(path: Path, *, base: Path) -> dict[str, Any]:
    out = _text_file_info(path, base=base, max_chars=1000)
    if "text" not in out:
        return out
    raw = str(out.get("text") or "").strip()
    if raw:
        pointed = Path(raw).expanduser()
        if not pointed.is_absolute():
            pointed = (base / pointed).resolve()
        out["text"] = _relative_path(pointed, base=base)
    return out


def _read_text(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return None


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"failed to parse OpenClaw profile: {path.name}",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"OpenClaw profile must be a JSON object: {path.name}")
    return payload


def _profile_path_from_payload(payload: dict[str, Any], *, base: Path) -> Path | None:
    raw = str(payload.get("openclaw_profile_path") or payload.get("profile_path") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (base / path).resolve()
        return path
    for name in DEFAULT_PROFILE_NAMES:
        candidate = (base / name).resolve()
        if candidate.exists():
            return candidate
    return None


def _merge_openclaw_profile(payload: dict[str, Any], *, base: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    merged = dict(payload)
    profile_path = _profile_path_from_payload(merged, base=base)
    if profile_path is None:
        return merged, None
    if not profile_path.exists():
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"OpenClaw profile not found: {profile_path.name}",
            hint="Remove profile_path/openclaw_profile_path or create the referenced JSON profile.",
        )
    profile = _read_json_object(profile_path)
    paths_raw = profile.get("paths")
    paths: dict[str, Any] = paths_raw if isinstance(paths_raw, dict) else {}
    for key in ("config_key", "config_path", "accounts", "max_notification_chars", "max_run_age_minutes"):
        if key not in merged and key in profile:
            merged[key] = profile[key]
    for key in PROFILE_PATH_KEYS:
        if key not in merged:
            if key in paths:
                merged[key] = paths[key]
            elif key in profile:
                merged[key] = profile[key]
    if "cron_jobs" not in merged and isinstance(profile.get("cron_jobs"), list):
        merged["cron_jobs"] = profile["cron_jobs"]
    if "include_cron_status" not in merged and "include_cron_status" in profile:
        merged["include_cron_status"] = profile["include_cron_status"]
    return merged, {
        "path": _relative_path(profile_path, base=base),
        "loaded": True,
        "cron_job_count": len(profile.get("cron_jobs") or []) if isinstance(profile.get("cron_jobs"), list) else 0,
    }


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(text)
    except ValueError:
        return None
    if out.tzinfo is None:
        return out.replace(tzinfo=timezone.utc)
    return out.astimezone(timezone.utc)


def _freshness_from_runtime_status(
    data: dict[str, Any],
    *,
    max_age_minutes: int,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    now = (now_fn or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    candidates: list[tuple[str, datetime]] = []

    def collect(label: str, item: Any) -> None:
        if isinstance(item, dict):
            parsed = _parse_utc(item.get("mtime_utc"))
            if parsed is not None:
                candidates.append((label, parsed))

    shared_raw = data.get("shared")
    shared: dict[str, Any] = shared_raw if isinstance(shared_raw, dict) else {}
    collect("shared.last_run", shared.get("last_run"))
    collect("shared.legacy_last_run", shared.get("legacy_last_run"))
    latest_run_raw = data.get("latest_run")
    latest_run: dict[str, Any] = latest_run_raw if isinstance(latest_run_raw, dict) else {}
    latest_state_raw = latest_run.get("state")
    latest_state: dict[str, Any] = latest_state_raw if isinstance(latest_state_raw, dict) else {}
    collect("latest_run.last_run", latest_state.get("last_run"))
    accounts_raw = data.get("accounts")
    accounts: dict[str, Any] = accounts_raw if isinstance(accounts_raw, dict) else {}
    for account, item in accounts.items():
        if isinstance(item, dict):
            collect(f"accounts.{account}.last_run", item.get("last_run"))

    if not candidates:
        return {
            "status": "unknown",
            "latest_mtime_utc": None,
            "latest_source": None,
            "age_seconds": None,
            "max_age_minutes": int(max_age_minutes),
            "stale": True,
        }
    latest_source, latest_mtime = max(candidates, key=lambda item: item[1])
    age_seconds = max(0, int((now - latest_mtime).total_seconds()))
    max_age_seconds = max(60, int(max_age_minutes) * 60)
    return {
        "status": "stale" if age_seconds > max_age_seconds else "fresh",
        "latest_mtime_utc": latest_mtime.isoformat().replace("+00:00", "Z"),
        "latest_source": latest_source,
        "age_seconds": age_seconds,
        "max_age_minutes": int(max_age_minutes),
        "stale": age_seconds > max_age_seconds,
    }


def _account_summary(data: dict[str, Any]) -> dict[str, Any]:
    accounts_raw = data.get("accounts")
    accounts: dict[str, Any] = accounts_raw if isinstance(accounts_raw, dict) else {}
    rows: dict[str, Any] = {}
    for account, item in accounts.items():
        if not isinstance(item, dict):
            continue
        last_run_raw = item.get("last_run")
        notification_raw = item.get("notification")
        last_run: dict[str, Any] = last_run_raw if isinstance(last_run_raw, dict) else {}
        notification: dict[str, Any] = notification_raw if isinstance(notification_raw, dict) else {}
        last_run_json_raw = last_run.get("json")
        last_run_json: dict[str, Any] = last_run_json_raw if isinstance(last_run_json_raw, dict) else {}
        rows[str(account)] = {
            "last_run_exists": bool(last_run.get("exists")),
            "notification_exists": bool(notification.get("exists")),
            "last_status": last_run_json.get("status") or last_run_json.get("last_status"),
            "last_run_mtime_utc": last_run.get("mtime_utc"),
            "notification_mtime_utc": notification.get("mtime_utc"),
        }
    return {
        "accounts": rows,
        "account_count": len(rows),
        "accounts_with_last_run": sum(1 for item in rows.values() if item.get("last_run_exists")),
        "accounts_with_notification": sum(1 for item in rows.values() if item.get("notification_exists")),
    }


def _number_or_none(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number.is_integer():
        return int(number)
    return number


def _numeric_dict(value: Any) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int | float] = {}
    for key, item in value.items():
        parsed = _number_or_none(item)
        if parsed is not None:
            out[str(key)] = parsed
    return out


def _prefetch_account_summary(info: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "exists": bool(info.get("exists")),
        "path": info.get("path"),
    }
    if not info.get("exists"):
        return out

    payload_raw = info.get("json")
    payload: dict[str, Any] = payload_raw if isinstance(payload_raw, dict) else {}
    run_summary_raw = payload.get("run_fetch_summary")
    run_summary: dict[str, Any] = run_summary_raw if isinstance(run_summary_raw, dict) else {}
    out.update(
        {
            "bottleneck": run_summary.get("bottleneck"),
            "to_fetch": _number_or_none(payload.get("to_fetch")),
            "deduped_count": _number_or_none(payload.get("deduped_count")),
            "errors": _number_or_none(payload.get("errors")),
            "opend_calls": _numeric_dict(run_summary.get("opend_calls")),
            "cache": _numeric_dict(run_summary.get("cache")),
            "rate_gate_wait_sec": _numeric_dict(run_summary.get("rate_gate_wait_sec")),
        }
    )
    snapshot = run_summary.get("snapshot")
    if isinstance(snapshot, dict):
        out["snapshot"] = snapshot
    return out


def _latest_run_prefetch_summary(latest_run_payload: dict[str, Any] | None) -> dict[str, Any]:
    latest_accounts_raw = latest_run_payload.get("accounts") if isinstance(latest_run_payload, dict) else {}
    latest_accounts: dict[str, Any] = latest_accounts_raw if isinstance(latest_accounts_raw, dict) else {}

    accounts: dict[str, Any] = {}
    bottlenecks: dict[str, int] = {}
    total_opend_calls = 0
    total_rate_gate_wait_sec = 0.0
    total_errors = 0
    available_account_count = 0

    for account, item in latest_accounts.items():
        if not isinstance(item, dict):
            continue
        info_raw = item.get("required_data_prefetch")
        info: dict[str, Any] = info_raw if isinstance(info_raw, dict) else {}
        account_summary = _prefetch_account_summary(info)
        accounts[str(account)] = account_summary
        if not account_summary.get("exists"):
            continue
        available_account_count += 1
        bottleneck = str(account_summary.get("bottleneck") or "unknown")
        bottlenecks[bottleneck] = bottlenecks.get(bottleneck, 0) + 1
        opend_calls_raw = account_summary.get("opend_calls")
        opend_calls: dict[str, Any] = opend_calls_raw if isinstance(opend_calls_raw, dict) else {}
        total_opend_calls += int(opend_calls.get("total") or 0)
        waits_raw = account_summary.get("rate_gate_wait_sec")
        waits: dict[str, Any] = waits_raw if isinstance(waits_raw, dict) else {}
        total_rate_gate_wait_sec += sum(float(value) for value in waits.values())
        total_errors += int(account_summary.get("errors") or 0)

    primary_bottleneck = None
    if bottlenecks:
        primary_bottleneck = max(bottlenecks.items(), key=lambda item: (item[1], item[0]))[0]

    return {
        "available": available_account_count > 0,
        "account_count": len(accounts),
        "available_account_count": available_account_count,
        "primary_bottleneck": primary_bottleneck,
        "bottlenecks": bottlenecks,
        "total_opend_calls": total_opend_calls,
        "total_rate_gate_wait_sec": round(total_rate_gate_wait_sec, 3),
        "total_errors": total_errors,
        "accounts": accounts,
    }


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
    payload, profile_meta = _merge_openclaw_profile(payload, base=base)
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
    max_run_age_minutes = int(payload.get("max_run_age_minutes") or 60)

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
    try:
        intake_cfg = resolve_trade_intake_config(cfg)
        intake_section_raw = cfg.get("trade_intake")
        intake_section: dict[str, Any] = intake_section_raw if isinstance(intake_section_raw, dict) else {}
        default_intake_status_path = (
            _path_from_config(intake_cfg["status_path"], base=base)
            if "status_path" in intake_section
            else state_dir / "auto_trade_intake_status.json"
        )
        default_intake_state_path = (
            _path_from_config(intake_cfg["state_path"], base=base)
            if "state_path" in intake_section
            else state_dir / "auto_trade_intake_state.json"
        )
        default_intake_audit_path = (
            _path_from_config(intake_cfg["audit_path"], base=base)
            if "audit_path" in intake_section
            else state_dir / "auto_trade_intake_audit.jsonl"
        )
        trade_intake_status = _json_file_info(
            _path_from_config(payload.get("trade_intake_status_path"), base=base) if payload.get("trade_intake_status_path") else default_intake_status_path,
            base=base,
            read_json_object_or_empty=read_json_object_or_empty,
        )
        trade_intake_state = _json_file_info(
            _path_from_config(payload.get("trade_intake_state_path"), base=base) if payload.get("trade_intake_state_path") else default_intake_state_path,
            base=base,
            read_json_object_or_empty=read_json_object_or_empty,
        )
        trade_intake_audit = _file_info(
            _path_from_config(payload.get("trade_intake_audit_path"), base=base) if payload.get("trade_intake_audit_path") else default_intake_audit_path,
            base=base,
        )
        trade_intake_state_json = trade_intake_state.get("json")
        trade_intake_status_json = trade_intake_status.get("json")
        trade_intake_state_payload: dict[str, Any] = trade_intake_state_json if isinstance(trade_intake_state_json, dict) else {}
        trade_intake_status_payload: dict[str, Any] = trade_intake_status_json if isinstance(trade_intake_status_json, dict) else {}
        trade_intake = {
            "enabled": bool(intake_cfg["enabled"]),
            "mode": intake_cfg["mode"],
            "receipt": dict(intake_cfg.get("receipt") or {}),
            "status": trade_intake_status,
            "state": trade_intake_state,
            "audit": trade_intake_audit,
            "summary": _trade_intake_summary(trade_intake_state_payload, trade_intake_status_payload),
        }
    except ValueError as exc:
        trade_intake = {
            "enabled": False,
            "config_error": str(exc),
            "summary": {"listener_status": None},
        }

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
                "required_data_prefetch": _json_file_info(
                    run_account_root / "state" / "required_data_prefetch_summary.json",
                    base=base,
                    read_json_object_or_empty=read_json_object_or_empty,
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

    prefetch_summary = _latest_run_prefetch_summary(latest_run_payload)

    warnings: list[str] = []
    if not shared_last_run.get("exists") and not legacy_last_run.get("exists"):
        warnings.append("No last_run.json found under output_shared/state or output/state.")
    if not notification.get("exists") and not any(item["notification"].get("exists") for item in account_status.values()):
        warnings.append("No symbols_notification.txt found under output/reports or output_accounts/<account>/reports.")

    latest_status = None
    for candidate in (shared_last_run, legacy_last_run):
        payload_raw = candidate.get("json")
        payload_json: dict[str, Any] = payload_raw if isinstance(payload_raw, dict) else {}
        latest_status = payload_json.get("status") or payload_json.get("last_status") or latest_status
        if latest_status:
            break

    data: dict[str, Any] = {
        "config": {
            "config_path": mask_path(config_path),
            "accounts": accounts,
            "config_key": payload.get("config_key"),
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
            "last_run_dir": _path_pointer_file_info(pointer_path, base=base),
            "notification": notification,
        },
        "trade_intake": trade_intake,
        "accounts": account_status,
        "latest_run": latest_run_payload,
        "required_data_prefetch": prefetch_summary,
        "account_summary": {},
        "freshness": {},
        "openclaw_profile": profile_meta or {"loaded": False},
        "summary": {
            "ok": not warnings,
            "warning_count": len(warnings),
            "latest_status": latest_status,
        },
    }
    data["account_summary"] = _account_summary(data)
    data["freshness"] = _freshness_from_runtime_status(data, max_age_minutes=max_run_age_minutes)
    data["summary"]["freshness_status"] = data["freshness"].get("status")
    data["summary"]["account_count"] = data["account_summary"].get("account_count")
    data["summary"]["prefetch_available"] = prefetch_summary.get("available")
    data["summary"]["prefetch_bottleneck"] = prefetch_summary.get("primary_bottleneck")
    return data, warnings, {"config_path": mask_path(config_path)}


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
    route = resolve_notification_route_from_config(config=cfg)
    provider = str(route.get("provider") or "")
    channel = str(route.get("channel") or "")
    target = str(notifications.get("target") or "").strip()
    if not is_supported_notification_provider(provider):
        return {
            "name": "notification_route",
            "status": "error",
            "message": f"unsupported notifications.provider: {provider}",
            "value": {"configured": True, "provider": provider, "channel": channel, "target_configured": bool(target)},
        }
    if not target:
        return {
            "name": "notification_route",
            "status": "error",
            "message": "notifications.target is missing",
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
    openclaw_profile_path = str(payload.get("openclaw_profile_path") or "").strip()
    if profile_path:
        out["profile_path"] = profile_path
    elif openclaw_profile_path:
        out["openclaw_profile_path"] = openclaw_profile_path
    if str(payload.get("config_key") or "").strip():
        out["config_key"] = str(payload.get("config_key")).strip()
    elif payload.get("config_path"):
        out["config_path"] = str(payload.get("config_path"))
    if out:
        return out
    return {"config_key": "us"}


def _tick_command(payload: dict[str, Any]) -> list[str]:
    command = ["./om", "run", "tick"]
    if payload.get("config_path"):
        command.extend(["--config", str(payload.get("config_path"))])
    else:
        config_key = str(payload.get("config_key") or "us").strip().lower()
        command.extend(["--config", f"config.{config_key}.json"])
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
    "runtime_status_tool",
]
