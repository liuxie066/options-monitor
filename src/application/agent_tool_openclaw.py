from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast

from src.application.agent_tool_contracts import AgentToolError
from src.application.ledger.api import ledger_store_payload
from src.application.runtime_config_paths import resolve_data_config_ref
from src.application.runtime_trigger_context import build_trigger_context
from src.application.service_deploy import service_status_from_profile
from src.application.service_drift import service_drift_status
from domain.domain.multi_tick import (
    FEISHU_APP_NOTIFICATION_PROVIDER,
    OPENCLAW_NOTIFICATION_PROVIDER,
    is_supported_notification_provider,
    resolve_openclaw_transport_channel,
)
from src.application.notification_delivery_route import resolve_notification_delivery_route
from src.application.trades.account_mapping import resolve_trade_intake_config


PROFILE_PATH_KEYS = ("report_dir", "state_dir", "shared_state_dir", "accounts_root", "runs_root")
PROFILE_TRIGGER_KEYS = (
    "trigger_source",
    "trigger_job_id",
    "trigger_job_name",
    "trigger_schedule",
    "trigger_timezone",
    "delivery",
    "delivery_mode",
    "deliveryMode",
    "timeout_seconds",
    "timeoutSeconds",
)
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


def _auto_close_receipt_summary(maintenance_json: dict[str, Any] | Any) -> dict[str, Any] | None:
    if not isinstance(maintenance_json, dict):
        return None
    receipt = maintenance_json.get("receipt")
    if not isinstance(receipt, dict):
        return None
    return {
        "status": receipt.get("status"),
        "reason": receipt.get("reason"),
        "delivery_confirmed": bool(receipt.get("delivery_confirmed")),
        "message_id": receipt.get("message_id"),
        "error_code": receipt.get("error_code"),
        "attempt_count": receipt.get("attempt_count"),
        "receipt_key": receipt.get("receipt_key"),
        "updated_at": receipt.get("updated_at"),
    }


def _ledger_context_summary(context_info: dict[str, Any] | Any) -> dict[str, Any]:
    if not isinstance(context_info, dict):
        return {"available": False, "status": "unknown", "fail_closed": False}
    payload = context_info.get("json")
    context: dict[str, Any] = payload if isinstance(payload, dict) else {}
    ledger_raw = context.get("ledger")
    ledger: dict[str, Any] = ledger_raw if isinstance(ledger_raw, dict) else {}
    if not ledger:
        return {
            "available": bool(context_info.get("exists")),
            "status": "unknown",
            "fail_closed": False,
        }
    return {
        "available": True,
        "status": ledger.get("status") or "unknown",
        "reason": ledger.get("reason"),
        "read_model": ledger.get("read_model"),
        "fail_closed": bool(ledger.get("fail_closed")),
        "source_record_count": ledger.get("source_record_count"),
        "imported_event_count": ledger.get("imported_event_count"),
        "lot_count": ledger.get("lot_count"),
        "open_lot_count": ledger.get("open_lot_count"),
        "view_count": ledger.get("view_count"),
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
    config_paths_raw = profile.get("config_paths")
    config_paths: dict[str, Any] = config_paths_raw if isinstance(config_paths_raw, dict) else {}
    if "config_path" not in merged and "config_key" not in merged and config_paths:
        profile_markets = profile.get("markets")
        market = "us"
        if isinstance(profile_markets, list) and profile_markets:
            market = str(profile_markets[0]).strip().lower() or "us"
        if market in config_paths:
            merged["config_path"] = config_paths[market]
    for key in ("config_key", "config_path", "accounts", "max_notification_chars", "max_run_age_minutes"):
        if key not in merged and key in profile:
            merged[key] = profile[key]
    for key in PROFILE_TRIGGER_KEYS:
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
    for key in (
        "service_provider",
        "repo_root",
        "runtime_root",
        "services",
        "include_service_status",
        "markets",
        "config_paths",
        "env_file",
        "deploy_user",
        "deploy_home",
        "auto_upgrade",
        "feishu_ws",
    ):
        if key not in merged and key in profile:
            merged[key] = profile[key]
    return merged, {
        "path": _relative_path(profile_path, base=base),
        "loaded": True,
        "cron_job_count": len(profile.get("cron_jobs") or []) if isinstance(profile.get("cron_jobs"), list) else 0,
        "service_provider": profile.get("service_provider"),
        "service_count": len(profile.get("services") or []) if isinstance(profile.get("services"), list) else 0,
    }


def _service_profile_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    services = payload.get("services")
    profile = {
        "service_provider": payload.get("service_provider") or payload.get("provider"),
        "repo_root": payload.get("repo_root"),
        "runtime_root": payload.get("runtime_root"),
        "services": services if isinstance(services, list) else [],
    }
    for key in (
        "accounts",
        "markets",
        "config_paths",
        "env_file",
        "deploy_user",
        "deploy_home",
        "auto_upgrade",
        "feishu_ws",
    ):
        if key in payload:
            profile[key] = payload[key]
    return profile


def _load_runtime_service_profile(runtime_root: Path) -> dict[str, Any]:
    profile_path = runtime_root / "service.profile.json"
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _service_profile_summary(payload: dict[str, Any]) -> dict[str, Any]:
    profile = _service_profile_from_payload(payload)
    if not profile.get("service_provider") and not profile.get("services"):
        return {"loaded": False}
    summary = service_status_from_profile(
        profile,
        include_status=bool(payload.get("include_service_status", False)),
    )
    summary["loaded"] = True
    return summary


def _repo_version(base: Path) -> str | None:
    try:
        text = (base / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return None
    return text or None


def _upgrade_service_profile(payload: dict[str, Any], *, runtime_root: Path) -> dict[str, Any]:
    profile = _load_runtime_service_profile(runtime_root)
    if profile:
        return profile
    payload_profile = _service_profile_from_payload(payload)
    if payload_profile.get("service_provider") or payload_profile.get("services"):
        return payload_profile
    return {}


def _check_upgrade_services(profile: dict[str, Any], *, failed_services: list[str]) -> dict[str, Any]:
    if not profile:
        return {"checked": False, "reason": "service_profile_missing", "all_active": False, "services": []}
    status = service_status_from_profile(profile, include_status=True)
    services_raw = status.get("services")
    services = services_raw if isinstance(services_raw, list) else []
    wanted = {name for name in failed_services if name}
    checked_services: list[dict[str, Any]] = []
    for item in services:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if wanted and name not in wanted:
            continue
        checked_services.append(item)
    if not checked_services and not wanted:
        checked_services = [item for item in services if isinstance(item, dict)]
    if not checked_services:
        return {**status, "checked": False, "reason": "restart_services_missing", "all_active": False}
    all_active = all(str(item.get("status") or "").strip().lower() == "ok" for item in checked_services)
    return {**status, "checked": True, "services": checked_services, "all_active": all_active}


def _upgrade_status_evaluation(
    upgrade_info: dict[str, Any],
    *,
    base: Path,
    runtime_root: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    upgrade_json = upgrade_info.get("json") if isinstance(upgrade_info.get("json"), dict) else {}
    if not upgrade_json:
        return {"status": None, "runtime_failed": False, "warning": False, "checked": False}

    historical_status = str(upgrade_json.get("status") or "").strip()
    target_version = str(upgrade_json.get("target_version") or "").strip() or None
    current_version = _repo_version(base)
    lock_path = runtime_root / "locks" / "upgrade.lock"
    lock_exists = lock_path.exists()
    error = upgrade_json.get("error")
    failed_statuses = {"failed", "upgraded_restart_failed"}
    failed_services_raw = upgrade_json.get("restart_failed_services")
    failed_services = [str(item).strip() for item in failed_services_raw] if isinstance(failed_services_raw, list) else []
    failed_services = [item for item in failed_services if item]
    service_check: dict[str, Any] = {"checked": False, "services": []}

    out: dict[str, Any] = {
        "status": historical_status or None,
        "historical_status": historical_status or None,
        "target_version": target_version,
        "current_version": current_version,
        "error": error,
        "lock_exists": lock_exists,
        "runtime_failed": False,
        "warning": False,
        "checked": True,
    }

    if lock_exists:
        return {
            **out,
            "status": "in_progress",
            "runtime_failed": True,
            "reason": "upgrade_lock_exists",
            "lock_path": _relative_path(lock_path, base=base),
        }

    if historical_status not in failed_statuses:
        return out

    target_is_current = bool(target_version and current_version and target_version == current_version)
    symlink_switched = bool(upgrade_json.get("symlink_switched") or upgrade_json.get("changed"))
    if not target_is_current:
        return {
            **out,
            "status": "historical_failed",
            "runtime_failed": False,
            "warning": True,
            "reason": "upgrade_failure_target_is_not_current_version",
        }

    profile = _upgrade_service_profile(payload, runtime_root=runtime_root)
    service_check = _check_upgrade_services(profile, failed_services=failed_services)
    services_active = bool(service_check.get("checked")) and bool(service_check.get("all_active"))
    if symlink_switched and services_active:
        return {
            **out,
            "status": "remediated",
            "runtime_failed": False,
            "warning": True,
            "reason": "target_version_active_and_restart_services_active",
            "service_check": service_check,
        }

    return {
        **out,
        "status": "failed",
        "runtime_failed": True,
        "warning": False,
        "reason": "upgrade_failure_still_requires_remediation",
        "service_check": service_check,
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
    latest_scanned_raw = data.get("latest_scanned_run")
    latest_scanned: dict[str, Any] = latest_scanned_raw if isinstance(latest_scanned_raw, dict) else {}
    latest_scanned_state_raw = latest_scanned.get("state")
    latest_scanned_state: dict[str, Any] = latest_scanned_state_raw if isinstance(latest_scanned_state_raw, dict) else {}
    collect("latest_scanned_run.last_run", latest_scanned_state.get("last_run"))
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
    opend_calls = _numeric_dict(run_summary.get("opend_calls"))
    cache = _numeric_dict(run_summary.get("cache"))
    rate_gate_wait_sec = _numeric_dict(run_summary.get("rate_gate_wait_sec"))
    force_refresh = payload.get("force_refresh")
    out.update(
        {
            "bottleneck": run_summary.get("bottleneck"),
            "to_fetch": _number_or_none(payload.get("to_fetch")),
            "deduped_count": _number_or_none(payload.get("deduped_count")),
            "cached_unique_symbols": _number_or_none(payload.get("cached_unique_symbols")),
            "skipped": _number_or_none(payload.get("skipped")),
            "force_refresh": force_refresh if isinstance(force_refresh, bool) else None,
            "errors": _number_or_none(payload.get("errors")),
            "opend_calls": opend_calls,
            "opend_calls_reported": bool(opend_calls),
            "cache": cache,
            "rate_gate_wait_sec": rate_gate_wait_sec,
            "rate_gate_wait_reported": bool(rate_gate_wait_sec),
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
    total_to_fetch = 0
    total_deduped_count = 0
    total_cached_unique_symbols = 0
    total_skipped = 0
    available_account_count = 0
    opend_calls_reported_account_count = 0
    rate_gate_wait_reported_account_count = 0
    force_refresh_account_count = 0
    first_available_account: str | None = None

    for account, item in latest_accounts.items():
        if not isinstance(item, dict):
            continue
        account_name = str(account)
        info_raw = item.get("required_data_prefetch")
        info: dict[str, Any] = info_raw if isinstance(info_raw, dict) else {}
        account_summary = _prefetch_account_summary(info)
        accounts[account_name] = account_summary
        if not account_summary.get("exists"):
            continue
        available_account_count += 1
        if first_available_account is None:
            first_available_account = account_name
        bottleneck = str(account_summary.get("bottleneck") or "unknown")
        bottlenecks[bottleneck] = bottlenecks.get(bottleneck, 0) + 1
        opend_calls_raw = account_summary.get("opend_calls")
        opend_calls: dict[str, Any] = opend_calls_raw if isinstance(opend_calls_raw, dict) else {}
        if account_summary.get("opend_calls_reported"):
            opend_calls_reported_account_count += 1
        total_opend_calls += int(opend_calls.get("total") or 0)
        waits_raw = account_summary.get("rate_gate_wait_sec")
        waits: dict[str, Any] = waits_raw if isinstance(waits_raw, dict) else {}
        if account_summary.get("rate_gate_wait_reported"):
            rate_gate_wait_reported_account_count += 1
        total_rate_gate_wait_sec += sum(float(value) for value in waits.values())
        total_errors += int(account_summary.get("errors") or 0)
        total_to_fetch += int(account_summary.get("to_fetch") or 0)
        total_deduped_count += int(account_summary.get("deduped_count") or 0)
        total_cached_unique_symbols += int(account_summary.get("cached_unique_symbols") or 0)
        total_skipped += int(account_summary.get("skipped") or 0)
        if account_summary.get("force_refresh") is True:
            force_refresh_account_count += 1

    primary_bottleneck = None
    if bottlenecks:
        primary_bottleneck = max(bottlenecks.items(), key=lambda item: (item[1], item[0]))[0]
    missing_account_count = len(accounts) - available_account_count
    shared_run_summary = bool(available_account_count and missing_account_count and force_refresh_account_count)

    summary = {
        "available": available_account_count > 0,
        "account_count": len(accounts),
        "available_account_count": available_account_count,
        "missing_account_count": missing_account_count,
        "primary_bottleneck": primary_bottleneck,
        "bottlenecks": bottlenecks,
        "total_opend_calls": total_opend_calls,
        "opend_calls_reported_account_count": opend_calls_reported_account_count,
        "total_rate_gate_wait_sec": round(total_rate_gate_wait_sec, 3),
        "rate_gate_wait_reported_account_count": rate_gate_wait_reported_account_count,
        "total_errors": total_errors,
        "total_to_fetch": total_to_fetch,
        "total_deduped_count": total_deduped_count,
        "total_cached_unique_symbols": total_cached_unique_symbols,
        "total_skipped": total_skipped,
        "force_refresh_account_count": force_refresh_account_count,
        "shared_run_summary": shared_run_summary,
        "shared_summary_account": first_available_account if shared_run_summary else None,
        "accounts": accounts,
    }
    if shared_run_summary:
        summary["note"] = "Prefetch summary may be shared across accounts when force_refresh prefetch runs once."
    return summary


def _json_payload(file_info: Any) -> dict[str, Any]:
    if not isinstance(file_info, dict):
        return {}
    payload = file_info.get("json")
    return payload if isinstance(payload, dict) else {}


def _resolve_notification_route_summary(cfg: dict[str, Any]) -> dict[str, Any]:
    notifications = cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
    if not notifications:
        return {"configured": False, "target_configured": False}
    try:
        route = resolve_notification_delivery_route(config=cfg)
    except Exception as exc:
        return {
            "configured": False,
            "target_configured": bool(str(notifications.get("target") or "").strip()),
            "error": f"{type(exc).__name__}: {exc}",
        }
    provider = str(route.get("provider") or "")
    channel = str(route.get("channel") or "")
    target = str(route.get("target") or "").strip()
    return {
        "configured": bool(provider and channel and target),
        "provider": provider or None,
        "channel": channel or None,
        "target_configured": bool(target),
    }


def _notification_diagnosis(
    *,
    cfg: dict[str, Any],
    shared_last_run: dict[str, Any],
    latest_run_payload: dict[str, Any] | None,
    trigger_context: dict[str, Any],
) -> dict[str, Any]:
    latest_state = latest_run_payload.get("state") if isinstance(latest_run_payload, dict) else {}
    state: dict[str, Any] = latest_state if isinstance(latest_state, dict) else {}
    tick_metrics = _json_payload(state.get("tick_metrics"))
    shared_payload = _json_payload(shared_last_run)
    scheduler_raw = tick_metrics.get("scheduler_decision")
    scheduler: dict[str, Any] = scheduler_raw if isinstance(scheduler_raw, dict) else {}
    notify_summary_raw = tick_metrics.get("notify_summary")
    notify_summary: dict[str, Any] = notify_summary_raw if isinstance(notify_summary_raw, dict) else {}
    route_summary = _resolve_notification_route_summary(cfg)

    no_send = bool(tick_metrics.get("no_send") or shared_payload.get("no_send"))
    account_messages_count = int(notify_summary.get("account_messages_count") or tick_metrics.get("account_messages_count") or 0)
    send_attempted_count = int(notify_summary.get("send_attempted_count") or tick_metrics.get("send_attempted_count") or 0)
    send_confirmed_count = int(notify_summary.get("send_confirmed_count") or tick_metrics.get("send_confirmed_count") or 0)
    send_failed_count = int(notify_summary.get("send_failed_count") or tick_metrics.get("send_failed_count") or 0)
    scheduler_should_run = scheduler.get("should_run_scan")
    scheduler_should_notify = scheduler.get("is_notify_window_open")
    if scheduler_should_notify is None:
        scheduler_should_notify = scheduler.get("should_notify")

    status = "unknown"
    reason = "insufficient runtime output"
    if str(trigger_context.get("delivery_mode") or "").lower() == "none":
        status = "outer_delivery_disabled"
        reason = "outer delivery.mode is none; task output will not be announced by the runner"
    elif scheduler_should_run is False:
        status = "scheduler_skipped"
        reason = str(scheduler.get("reason") or "scheduler decided not to run")
    elif no_send:
        status = "no_send"
        reason = "--no-send suppressed repository notification delivery"
    elif not bool(route_summary.get("configured")):
        status = "notification_route_missing"
        reason = "notifications route is missing or incomplete"
    elif account_messages_count <= 0 and str(tick_metrics.get("reason") or "") == "no_account_notification":
        status = "no_notification_content"
        reason = "scan produced no account notification content"
    elif send_confirmed_count > 0 and send_failed_count > 0:
        status = "sent_partial"
        reason = "some account notifications were confirmed and some failed"
    elif send_confirmed_count > 0:
        status = "sent"
        reason = "repository notification delivery was confirmed for at least one account"
    elif send_attempted_count > 0:
        status = "send_failed_or_unconfirmed"
        reason = "repository attempted notification delivery but no account send was confirmed"
    elif tick_metrics:
        status = str(tick_metrics.get("reason") or "not_sent")
        reason = "latest tick metrics did not record a confirmed send"

    return {
        "status": status,
        "reason": reason,
        "trigger_observed": bool(trigger_context.get("observed")),
        "trigger_source": trigger_context.get("source"),
        "trigger_job_id": trigger_context.get("job_id"),
        "timeout_seconds": trigger_context.get("timeout_seconds"),
        "outer_delivery_mode": trigger_context.get("delivery_mode"),
        "outer_announce_expected": trigger_context.get("announce_expected"),
        "scheduler_should_run_scan": scheduler_should_run,
        "scheduler_should_notify": scheduler_should_notify,
        "scheduler_reason": scheduler.get("reason"),
        "no_send": no_send,
        "notification_route": route_summary,
        "account_messages_count": account_messages_count,
        "send_attempted_count": send_attempted_count,
        "send_confirmed_count": send_confirmed_count,
        "send_failed_count": send_failed_count,
        "sent_accounts": tick_metrics.get("sent_accounts") or shared_payload.get("sent_accounts") or [],
        "final_reason": tick_metrics.get("reason") or shared_payload.get("reason") or shared_payload.get("status"),
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


def _run_payload(
    run_dir: Path,
    *,
    accounts: list[str],
    base: Path,
    read_json_object_or_empty: Callable[[Path], dict[str, Any]],
    max_notification_chars: int,
) -> dict[str, Any]:
    run_accounts: dict[str, Any] = {}
    for account in accounts:
        run_account_root = run_dir / "accounts" / account
        expired_position_maintenance = _json_file_info(
            run_account_root / "state" / "expired_position_maintenance.json",
            base=base,
            read_json_object_or_empty=read_json_object_or_empty,
        )
        run_accounts[account] = {
            "last_run": _json_file_info(
                run_account_root / "state" / "last_run.json",
                base=base,
                read_json_object_or_empty=read_json_object_or_empty,
            ),
            "expired_position_maintenance": expired_position_maintenance,
            "auto_close_receipt": _auto_close_receipt_summary(expired_position_maintenance.get("json")),
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
    return {
        "path": _relative_path(run_dir, base=base),
        "state": {
            "last_run": _json_file_info(
                run_dir / "state" / "last_run.json",
                base=base,
                read_json_object_or_empty=read_json_object_or_empty,
            ),
            "tick_metrics": _json_file_info(
                run_dir / "state" / "tick_metrics.json",
                base=base,
                read_json_object_or_empty=read_json_object_or_empty,
            ),
        },
        "accounts": run_accounts,
    }


def _requested_run_dir_from_payload(
    payload: dict[str, Any],
    *,
    base: Path,
    runs_root: Path,
) -> tuple[Path | None, dict[str, Any]]:
    raw_run_dir = str(payload.get("run_dir") or "").strip()
    if raw_run_dir:
        run_dir = Path(raw_run_dir).expanduser()
        if not run_dir.is_absolute():
            run_dir = (base / run_dir).resolve()
        found = run_dir.exists() and run_dir.is_dir()
        return (
            run_dir if found else None,
            {
                "requested": True,
                "source": "run_dir",
                "value": raw_run_dir,
                "path": _relative_path(run_dir, base=base),
                "found": found,
            },
        )

    raw_run_id = str(payload.get("run_id") or "").strip()
    if raw_run_id:
        run_id = Path(raw_run_id)
        direct_child = not run_id.is_absolute() and run_id.name == raw_run_id
        run_dir = (runs_root / raw_run_id).resolve() if direct_child else (runs_root / run_id.name).resolve()
        found = direct_child and run_dir.exists() and run_dir.is_dir()
        selection: dict[str, Any] = {
            "requested": True,
            "source": "run_id",
            "value": raw_run_id,
            "path": _relative_path(run_dir, base=base),
            "found": found,
        }
        if not direct_child:
            selection["error"] = "run_id must be a direct child of runs_root"
        return run_dir if found else None, selection

    return None, {"requested": False, "source": "last_run_dir_or_mtime"}


def _latest_run_selection(*, latest_run: Path | None, base: Path) -> dict[str, Any]:
    return {
        "requested": False,
        "source": "last_run_dir_or_mtime",
        "path": _relative_path(latest_run, base=base) if latest_run is not None else None,
        "found": latest_run is not None,
    }


def _run_payload_has_scan(run_payload: dict[str, Any]) -> bool:
    state_raw = run_payload.get("state")
    state: dict[str, Any] = state_raw if isinstance(state_raw, dict) else {}
    tick_metrics = _json_payload(state.get("tick_metrics"))
    if tick_metrics.get("ran_scan") is True:
        return True

    tick_accounts_raw = tick_metrics.get("accounts")
    tick_account_items: list[Any] = []
    if isinstance(tick_accounts_raw, dict):
        tick_account_items = list(tick_accounts_raw.values())
    elif isinstance(tick_accounts_raw, list):
        tick_account_items = tick_accounts_raw
    if any(isinstance(item, dict) and item.get("ran_scan") is True for item in tick_account_items):
        return True

    if _json_payload(state.get("last_run")).get("ran_scan") is True:
        return True

    accounts_raw = run_payload.get("accounts")
    accounts: dict[str, Any] = accounts_raw if isinstance(accounts_raw, dict) else {}
    for item in accounts.values():
        if isinstance(item, dict) and _json_payload(item.get("last_run")).get("ran_scan") is True:
            return True
    return False


def _run_dirs_newest_first(runs_root: Path) -> list[Path]:
    if not runs_root.exists() or not runs_root.is_dir():
        return []
    dirs: list[Path] = []
    for item in runs_root.iterdir():
        if item.is_dir():
            dirs.append(item)
    return sorted(dirs, key=lambda item: (item.stat().st_mtime, item.name), reverse=True)


def _latest_scanned_run_payload(
    *,
    runs_root: Path,
    accounts: list[str],
    base: Path,
    read_json_object_or_empty: Callable[[Path], dict[str, Any]],
    max_notification_chars: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    searched_count = 0
    for run_dir in _run_dirs_newest_first(runs_root):
        searched_count += 1
        candidate = _run_payload(
            run_dir,
            accounts=accounts,
            base=base,
            read_json_object_or_empty=read_json_object_or_empty,
            max_notification_chars=max_notification_chars,
        )
        if _run_payload_has_scan(candidate):
            return candidate, {
                "source": "runs_root_mtime",
                "searched_count": searched_count,
                "found": True,
                "path": candidate.get("path"),
            }
    return None, {
        "source": "runs_root_mtime",
        "searched_count": searched_count,
        "found": False,
        "path": None,
    }


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
    portfolio_raw = cfg.get("portfolio")
    portfolio_cfg = cast(dict[str, Any], portfolio_raw) if isinstance(portfolio_raw, dict) else {}
    data_config_ref = resolve_data_config_ref(payload, portfolio_cfg)
    if data_config_ref:
        data_config_path = Path(data_config_ref).expanduser()
        if not data_config_path.is_absolute():
            data_config_path = (config_path.parent / data_config_path).resolve()
    else:
        data_config_path = (config_path.parent / "portfolio.runtime.json").resolve()
    ledger_store = ledger_store_payload(data_config_path)
    ledger_runtime_root = Path(str(ledger_store.get("runtime_root") or base)).expanduser()
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
    trigger_context = build_trigger_context(payload, environ={})

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
    option_positions_context = _json_file_info(
        state_dir / "option_positions_context.json",
        base=base,
        read_json_object_or_empty=read_json_object_or_empty,
    )
    projection_verify = _json_file_info(
        ledger_runtime_root / "output_shared" / "state" / "option_positions" / "current" / "projection_verify.latest.json",
        base=base,
        read_json_object_or_empty=read_json_object_or_empty,
    )
    upgrade_status = _json_file_info(
        ledger_runtime_root / "upgrade_status.json",
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
            "option_positions_context": _json_file_info(
                account_root / "state" / "option_positions_context.json",
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
    requested_run, latest_run_selection = _requested_run_dir_from_payload(payload, base=base, runs_root=runs_root)
    if latest_run_selection.get("requested"):
        latest_run = requested_run
    else:
        latest_run = _latest_run_dir(base, pointer_path=pointer_path, runs_root=runs_root)
        latest_run_selection = _latest_run_selection(latest_run=latest_run, base=base)
    latest_run_payload: dict[str, Any] | None = None
    if latest_run is not None:
        latest_run_payload = _run_payload(
            latest_run,
            accounts=accounts,
            base=base,
            read_json_object_or_empty=read_json_object_or_empty,
            max_notification_chars=max_notification_chars,
        )

    prefetch_summary = _latest_run_prefetch_summary(latest_run_payload)
    latest_scanned_run_payload, latest_scanned_run_selection = _latest_scanned_run_payload(
        runs_root=runs_root,
        accounts=accounts,
        base=base,
        read_json_object_or_empty=read_json_object_or_empty,
        max_notification_chars=max_notification_chars,
    )
    latest_scanned_prefetch_summary = _latest_run_prefetch_summary(latest_scanned_run_payload)

    warnings: list[str] = []
    if latest_run_selection.get("requested") and not latest_run_selection.get("found"):
        source = latest_run_selection.get("source")
        value = latest_run_selection.get("value")
        warnings.append(f"Requested runtime run not found: {source}={value}.")
    if not shared_last_run.get("exists") and not legacy_last_run.get("exists"):
        warnings.append("No last_run.json found under output_shared/state or output/state.")
    if not notification.get("exists") and not any(item["notification"].get("exists") for item in account_status.values()):
        warnings.append("No symbols_notification.txt found under output/reports or output_accounts/<account>/reports.")
    if str(trigger_context.get("delivery_mode") or "").lower() == "none":
        warnings.append("Outer delivery.mode is none; the task runner will not announce run output.")
    ledger_context_summary = _ledger_context_summary(option_positions_context)
    if ledger_context_summary.get("fail_closed"):
        warnings.append("Ledger shadow context is fail-closed; risk reads should be blocked until repaired.")
    notification_diagnosis = _notification_diagnosis(
        cfg=cfg,
        shared_last_run=shared_last_run,
        latest_run_payload=latest_run_payload,
        trigger_context=trigger_context,
    )
    upgrade_evaluation = _upgrade_status_evaluation(
        upgrade_status,
        base=base,
        runtime_root=ledger_runtime_root,
        payload=payload,
    )
    warning_codes: list[str] = []
    if upgrade_evaluation.get("status") == "remediated":
        warnings.append("Service upgrade previously failed but current release and restart services look remediated.")
        warning_codes.append("SERVICE_UPGRADE_REMEDIATED")
    elif upgrade_evaluation.get("status") == "historical_failed":
        warnings.append("Service upgrade status file contains a historical failure for a non-current target version.")
        warning_codes.append("SERVICE_UPGRADE_HISTORICAL_FAILED")
    elif upgrade_evaluation.get("runtime_failed"):
        warnings.append("Service upgrade status still indicates an unrecovered runtime failure.")
        warning_codes.append("SERVICE_UPGRADE_FAILED")

    service_profile_for_drift = _upgrade_service_profile(payload, runtime_root=ledger_runtime_root)
    service_profile_for_drift_map = service_profile_for_drift if isinstance(service_profile_for_drift, dict) else {}
    drift_profile_path = None
    if payload.get("profile_path"):
        drift_profile_path = Path(str(payload["profile_path"])).expanduser()
        if not drift_profile_path.is_absolute():
            drift_profile_path = (base / drift_profile_path).resolve()
    service_drift = service_drift_status(
        repo_root=service_profile_for_drift_map.get("repo_root") if service_profile_for_drift_map else None,
        runtime_root=service_profile_for_drift_map.get("runtime_root") or ledger_runtime_root,
        profile_path=drift_profile_path,
        profile=service_profile_for_drift_map if service_profile_for_drift_map else None,
    )
    service_drift_summary_raw = service_drift.get("summary")
    service_drift_summary: dict[str, Any] = service_drift_summary_raw if isinstance(service_drift_summary_raw, dict) else {}
    missing_required_units = [
        str(item)
        for item in service_drift_summary.get("missing_required_units") or service_drift.get("missing_required_units") or []
        if str(item).strip()
    ]
    if missing_required_units:
        warnings.append("Service drift detected: required maintenance units are missing: " + ", ".join(missing_required_units) + ".")
        warning_codes.append("SERVICE_DRIFT_REQUIRED_UNIT_MISSING")
    elif service_drift_summary.get("status") == "warn":
        warnings.append("Service drift detected between current release, service profile, and installed units.")
        warning_codes.append("SERVICE_DRIFT")

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
        "ledger_store": ledger_store,
        "shared": {
            "last_run": shared_last_run,
            "legacy_last_run": legacy_last_run,
            "last_run_dir": _path_pointer_file_info(pointer_path, base=base),
            "notification": notification,
        },
        "trade_intake": trade_intake,
        "option_positions_context": {
            "last": option_positions_context,
            "ledger": ledger_context_summary,
        },
        "projection_verify": projection_verify,
        "service_upgrade": {**upgrade_status, "evaluation": upgrade_evaluation},
        "service_drift": service_drift,
        "accounts": account_status,
        "latest_run_selection": latest_run_selection,
        "latest_run": latest_run_payload,
        "latest_scanned_run_selection": latest_scanned_run_selection,
        "latest_scanned_run": latest_scanned_run_payload,
        "required_data_prefetch": prefetch_summary,
        "latest_scanned_run_required_data_prefetch": latest_scanned_prefetch_summary,
        "trigger_context": trigger_context,
        "notification_diagnosis": notification_diagnosis,
        "account_summary": {},
        "freshness": {},
        "openclaw_profile": profile_meta or {"loaded": False},
        "service_profile": _service_profile_summary(payload),
        "summary": {
            "ok": not warnings,
            "warning_count": len(warnings),
            "warning_codes": warning_codes,
            "latest_status": latest_status,
        },
    }
    data["account_summary"] = _account_summary(data)
    data["freshness"] = _freshness_from_runtime_status(data, max_age_minutes=max_run_age_minutes)
    data["summary"]["freshness_status"] = data["freshness"].get("status")
    data["summary"]["account_count"] = data["account_summary"].get("account_count")
    data["summary"]["latest_run_path"] = latest_run_payload.get("path") if latest_run_payload else None
    data["summary"]["latest_scanned_run_path"] = latest_scanned_run_payload.get("path") if latest_scanned_run_payload else None
    data["summary"]["prefetch_available"] = prefetch_summary.get("available")
    data["summary"]["prefetch_bottleneck"] = prefetch_summary.get("primary_bottleneck")
    data["summary"]["latest_scanned_run_prefetch_available"] = latest_scanned_prefetch_summary.get("available")
    data["summary"]["latest_scanned_run_prefetch_bottleneck"] = latest_scanned_prefetch_summary.get("primary_bottleneck")
    data["summary"]["ledger_status"] = ledger_context_summary.get("status")
    data["summary"]["ledger_fail_closed"] = bool(ledger_context_summary.get("fail_closed"))
    data["summary"]["ledger_sqlite_path"] = ledger_store.get("sqlite_path")
    data["summary"]["ledger_trade_event_count"] = ledger_store.get("trade_event_count")
    data["summary"]["ledger_position_lot_count"] = ledger_store.get("position_lot_count")
    projection_verify_json = projection_verify.get("json") if isinstance(projection_verify.get("json"), dict) else {}
    data["summary"]["projection_verify_ok"] = projection_verify_json.get("ok") if projection_verify_json else None
    data["summary"]["projection_verify_mode"] = projection_verify_json.get("mode_used") if projection_verify_json else None
    upgrade_json = upgrade_status.get("json") if isinstance(upgrade_status.get("json"), dict) else {}
    data["summary"]["service_upgrade_status"] = upgrade_evaluation.get("status") or (upgrade_json.get("status") if upgrade_json else None)
    data["summary"]["service_upgrade_historical_status"] = upgrade_evaluation.get("historical_status")
    data["summary"]["service_upgrade_target_version"] = upgrade_json.get("target_version") if upgrade_json else None
    data["summary"]["service_upgrade_current_version"] = upgrade_evaluation.get("current_version")
    data["summary"]["service_upgrade_error"] = upgrade_evaluation.get("error")
    data["summary"]["service_upgrade_runtime_failed"] = bool(upgrade_evaluation.get("runtime_failed"))
    data["summary"]["service_drift_status"] = service_drift_summary.get("status")
    data["summary"]["service_drift_missing_units"] = service_drift.get("missing_installed_units")
    data["summary"]["service_drift_missing_required_units"] = missing_required_units
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
