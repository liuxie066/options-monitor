from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast

from src.application.agent_tool_contracts import AgentToolError
from src.application.channels.status import build_channel_status
from src.application.environment_status import build_effective_env_with_status
from src.application.ledger.api import ledger_store_payload
from src.application.release_target import compare_versions
from src.application.runtime_config_freshness import (
    GENERATED_KEY,
    check_runtime_config_freshness,
    check_runtime_config_identity,
    infer_runtime_config_market,
)
from src.application.runtime_config_paths import resolve_data_config_ref
from src.application.runtime_trigger_context import build_trigger_context
from src.application.service_deploy import service_status_from_profile
from src.application.service_drift import service_drift_status
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
SERVICE_INJECTED_ENV_SENTINELS = (
    "DEEPSEEK_API_KEY",
    "MOONSHOT_API_KEY",
    "KIMI_API_KEY",
    "OM_LLM_API_KEY",
    "OM_FEISHU_APP_ID",
    "OM_FEISHU_APP_SECRET",
    "OM_FEISHU_BOT_APP_ID",
    "OM_FEISHU_BOT_APP_SECRET",
    "OM_INBOUND_OPERATION_HMAC_KEY",
)


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


def _is_env_file_permission_warning(message: str) -> bool:
    return "failed to read env file:" in message and "Permission denied" in message


def _has_service_injected_env(effective_env: Any) -> bool:
    for key in SERVICE_INJECTED_ENV_SENTINELS:
        value = str(effective_env.get(key) or "").strip()
        source = effective_env.source_of(key)
        if value and source is not None and source.source == "process_env":
            return True
    return False


def _runtime_status_env_file_warnings(effective_env: Any) -> list[str]:
    items = [str(item) for item in effective_env.warnings]
    if not items or not _has_service_injected_env(effective_env):
        return items
    return [item for item in items if not _is_env_file_permission_warning(item)]


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
        "last_push_received_utc": status_json.get("last_push_received_utc"),
        "last_push_deal_id": status_json.get("last_push_deal_id"),
        "last_backfill_check_utc": status_json.get("last_backfill_check_utc"),
        "last_backfill_window_start_utc": status_json.get("last_backfill_window_start_utc"),
        "last_backfill_window_end_utc": status_json.get("last_backfill_window_end_utc"),
        "last_backfill_deal_count": status_json.get("last_backfill_deal_count"),
        "last_backfill_applied_count": status_json.get("last_backfill_applied_count"),
        "last_backfill_skipped_duplicate_count": status_json.get("last_backfill_skipped_duplicate_count"),
        "last_backfill_failed_count": status_json.get("last_backfill_failed_count"),
        "last_backfill_unresolved_count": status_json.get("last_backfill_unresolved_count"),
        "missed_push_backfill_count": status_json.get("missed_push_backfill_count"),
        "last_backfill_error": status_json.get("last_backfill_error"),
        "last_deal_result": status_json.get("last_deal_result"),
        "last_backfill_result": status_json.get("last_backfill_result"),
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
            message=f"failed to parse service profile: {path.name}",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"service profile must be a JSON object: {path.name}")
    return payload


def _service_profile_path_from_payload(payload: dict[str, Any], *, base: Path) -> Path | None:
    if "openclaw_profile_path" in payload and str(payload.get("openclaw_profile_path") or "").strip():
        raise AgentToolError(
            code="INPUT_ERROR",
            message="openclaw_profile_path has been removed; use service profile_path",
        )
    raw = str(payload.get("profile_path") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (base / path).resolve()
        return path
    return None


def _profile_meta(profile: dict[str, Any], *, profile_path: Path, base: Path) -> dict[str, Any]:
    return {
        "path": _relative_path(profile_path, base=base),
        "loaded": True,
        "service_provider": profile.get("service_provider"),
        "service_count": len(profile.get("services") or []) if isinstance(profile.get("services"), list) else 0,
    }


def _merge_service_profile_payload(payload: dict[str, Any], *, profile: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload)
    paths_raw = profile.get("paths")
    paths: dict[str, Any] = paths_raw if isinstance(paths_raw, dict) else {}
    config_paths_raw = profile.get("config_paths")
    config_paths: dict[str, Any] = config_paths_raw if isinstance(config_paths_raw, dict) else {}
    if "config_path" not in merged and config_paths:
        market = str(merged.get("config_key") or profile.get("config_key") or "").strip().lower()
        if not market and "config_key" not in merged:
            profile_markets = profile.get("markets")
            if isinstance(profile_markets, list) and profile_markets:
                market = str(profile_markets[0]).strip().lower()
            else:
                available_markets = [key for key in ("us", "hk") if key in config_paths]
                if len(available_markets) == 1:
                    market = available_markets[0]
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
    for key in (
        "service_provider",
        "repo_root",
        "runtime_root",
        "services",
        "include_service_status",
        "markets",
        "config_paths",
        "env_file",
        "assistant_config_path",
        "deploy_user",
        "deploy_home",
        "auto_upgrade",
        "feishu_ws",
        "wechat_clawbot",
    ):
        if key not in merged and key in profile:
            merged[key] = profile[key]
    return merged


def _merge_explicit_service_profile(payload: dict[str, Any], *, base: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    profile_path = _service_profile_path_from_payload(payload, base=base)
    if profile_path is None:
        return dict(payload), None
    if not profile_path.exists():
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"service profile not found: {profile_path.name}",
            hint="Remove profile_path or create the referenced JSON profile.",
        )
    profile = _read_json_object(profile_path)
    return _merge_service_profile_payload(payload, profile=profile), _profile_meta(profile, profile_path=profile_path, base=base)


def _merge_runtime_service_profile(
    payload: dict[str, Any],
    *,
    base: Path,
    runtime_root: Path,
    existing_profile_meta: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if existing_profile_meta is not None:
        return payload, existing_profile_meta
    if str(payload.get("profile_path") or "").strip():
        return payload, existing_profile_meta
    profile_path = (runtime_root / "service.profile.json").resolve()
    if not profile_path.exists():
        return payload, existing_profile_meta
    profile = _read_json_object(profile_path)
    return _merge_service_profile_payload(payload, profile=profile), _profile_meta(profile, profile_path=profile_path, base=base)


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
        "wechat_clawbot",
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


def _assistant_runtime_summary(
    *,
    base: Path,
    runtime_root: Path,
    payload: dict[str, Any],
    mask_path: Callable[[Any], str | None],
) -> dict[str, Any]:
    try:
        from src.application.assistant.config_loader import load_assistant_config
        from src.application.assistant.settings import AssistantSettings
        from src.application.assistant.audit import InboundAuditStore
        from src.application.settings import build_effective_env
        from src.infrastructure.openai_chat_completions import resolve_chat_completions_url
        from src.infrastructure.openai_responses import resolve_responses_url
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    assistant_config_path, assistant_config_explicit = _assistant_config_path_from_runtime_status_payload(
        payload,
        base=base,
        runtime_root=runtime_root,
    )
    runtime_candidate = runtime_root / "resolved" / "config.assistant.json"
    repo_candidate = base / "config.assistant.json"
    if assistant_config_path is None and runtime_candidate.exists():
        assistant_config_path = runtime_candidate
    elif assistant_config_path is None and repo_candidate.exists():
        assistant_config_path = repo_candidate
    missing_ok = assistant_config_path is None
    if assistant_config_path is None:
        assistant_config_path = runtime_candidate
    elif assistant_config_explicit:
        missing_ok = False

    try:
        path, cfg = load_assistant_config(
            config_path=assistant_config_path,
            repo_root=base,
            missing_ok=missing_ok,
        )
        settings = AssistantSettings.from_runtime_config(cfg)
    except Exception as exc:
        return {
            "available": False,
            "config": {
                "path": mask_path(assistant_config_path) if assistant_config_path is not None else None,
                "loaded": False,
            },
            "error": f"{type(exc).__name__}: {exc}",
        }

    provider = str(settings.llm.provider or "").strip().lower()
    endpoint_url = None
    if settings.llm.enabled and provider == "deepseek":
        endpoint_url = resolve_chat_completions_url(settings.llm.base_url)
    elif settings.llm.enabled and provider == "openai":
        endpoint_url = resolve_responses_url(settings.llm.base_url)
    env_file_path = _assistant_env_file_path_from_payload(payload, base=base)
    env = build_effective_env(repo_root=base, env_file=env_file_path)
    audit_db_path = _assistant_audit_db_path_from_payload(payload, base=base, env=env.values)
    audit_summary: dict[str, Any] = {
        "path": mask_path(audit_db_path),
        "exists": audit_db_path.exists(),
        "latest": None,
    }
    if audit_db_path.exists():
        try:
            rows = InboundAuditStore(audit_db_path).list_recent(limit=1)
            if rows:
                audit_summary["latest"] = _assistant_audit_latest(rows[0])
        except Exception as exc:
            audit_summary["error"] = f"{type(exc).__name__}: {exc}"

    return {
        "available": True,
        "config": {
            "path": mask_path(path),
            "loaded": bool(cfg),
            "enabled": bool(settings.enabled),
            "planner": settings.planner.public_payload(),
            "context_window_messages": int(settings.context_window_messages),
            "default_market_scope": settings.default_market_scope,
        },
        "llm": {
            **settings.llm.public_payload(),
            "endpoint_url": endpoint_url,
            "api_key_configured": bool(env.get(settings.llm.api_key_env)),
            "env_file": mask_path(env.env_file) if env.env_file is not None else None,
            "env_file_loaded": bool(env.env_file_loaded),
        },
        "audit": audit_summary,
    }


def _assistant_config_path_from_runtime_status_payload(
    payload: dict[str, Any],
    *,
    base: Path,
    runtime_root: Path,
) -> tuple[Path | None, bool]:
    raw = str(payload.get("assistant_config_path") or "").strip()
    if not raw:
        feishu_ws = payload.get("feishu_ws")
        if isinstance(feishu_ws, dict):
            raw = str(feishu_ws.get("assistant_config_path") or "").strip()
    if not raw:
        wechat_clawbot = payload.get("wechat_clawbot")
        if isinstance(wechat_clawbot, dict):
            raw = str(wechat_clawbot.get("assistant_config_path") or "").strip()
    if raw:
        return _resolve_assistant_runtime_path(raw, base=base), True
    default_path = runtime_root / "resolved" / "config.assistant.json"
    return (default_path if default_path.exists() else None), False


def _assistant_env_file_path_from_payload(payload: dict[str, Any], *, base: Path) -> Path | None:
    raw = str(payload.get("env_file") or "").strip()
    if not raw:
        return None
    return _resolve_assistant_runtime_path(raw, base=base)


def _assistant_audit_db_path_from_payload(payload: dict[str, Any], *, base: Path, env: dict[str, str]) -> Path:
    raw = ""
    feishu_ws = payload.get("feishu_ws")
    if isinstance(feishu_ws, dict):
        raw = str(feishu_ws.get("audit_db") or "").strip()
    if not raw:
        wechat_clawbot = payload.get("wechat_clawbot")
        if isinstance(wechat_clawbot, dict):
            raw = str(wechat_clawbot.get("audit_db") or "").strip()
    if not raw:
        raw = str(env.get("OM_INBOUND_AUDIT_DB") or "").strip()
    if raw:
        return _resolve_assistant_runtime_path(raw, base=base)
    return (base / "output_shared" / "state" / "inbound_control.sqlite3").resolve()


def _resolve_assistant_runtime_path(raw: str, *, base: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _assistant_audit_latest(row: dict[str, Any]) -> dict[str, Any]:
    response: dict[str, Any] = {}
    try:
        parsed = json.loads(str(row.get("response_json") or "{}"))
        response = parsed if isinstance(parsed, dict) else {}
    except Exception:
        response = {}
    meta = response.get("meta")
    assistant = meta.get("assistant") if isinstance(meta, dict) else None
    assistant_payload = assistant if isinstance(assistant, dict) else {}
    llm = assistant_payload.get("llm")
    llm_payload = llm if isinstance(llm, dict) else {}
    context = assistant_payload.get("context")
    context_payload = context if isinstance(context, dict) else {}
    return {
        "created_at": row.get("created_at"),
        "channel": row.get("channel"),
        "sender_id": row.get("sender_id"),
        "conversation_id": row.get("conversation_id"),
        "parser": row.get("parser"),
        "intent_name": row.get("intent_name"),
        "tool_name": row.get("tool_name"),
        "decision": row.get("decision"),
        "result_ok": bool(row.get("result_ok")),
        "error_code": row.get("error_code"),
        "route": assistant_payload.get("route"),
        "mode": assistant_payload.get("mode"),
        "llm_reason": llm_payload.get("reason"),
        "llm_attempted": llm_payload.get("attempted"),
        "context": {
            "provided": bool(context_payload.get("provided")),
            "recent_count": context_payload.get("recent_count"),
            "pending_count": context_payload.get("pending_count"),
        },
    }


def _repo_version(base: Path) -> str | None:
    try:
        text = (base / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return None
    return text or None


def _upgrade_version_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("v") and len(text) > 1:
        return text[1:]
    return text


def _upgrade_target_version(upgrade_json: dict[str, Any]) -> str | None:
    target = _upgrade_version_text(upgrade_json.get("target_version"))
    if target:
        return target
    release_tag = _upgrade_version_text(upgrade_json.get("release_tag"))
    if release_tag:
        return release_tag
    target_dir = str(upgrade_json.get("target_dir") or "").strip()
    if target_dir:
        return _upgrade_version_text(Path(target_dir).name)
    return None


def _upgrade_failed_services(upgrade_json: dict[str, Any]) -> list[str]:
    out: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)

    raw = upgrade_json.get("restart_failed_services")
    if isinstance(raw, list):
        for item in raw:
            add(item)
    service_health = upgrade_json.get("service_health")
    health = service_health if isinstance(service_health, dict) else {}
    failed_checks = health.get("failed_checks")
    if isinstance(failed_checks, list):
        for item in failed_checks:
            if isinstance(item, dict):
                add(item.get("service"))
    return out


def _upgrade_remediation(upgrade_json: dict[str, Any]) -> list[str]:
    raw = upgrade_json.get("remediation") or upgrade_json.get("manual_remediation")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


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


def _upgrade_lock_info(lock_path: Path) -> dict[str, Any]:
    if not lock_path.exists():
        return {"exists": False, "pid": None, "active": False, "stale": False}
    pid: int | None = None
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
        pid = int(raw) if raw else None
    except Exception:
        pid = None
    active = False
    if pid and pid > 0:
        try:
            os.kill(pid, 0)
            active = True
        except ProcessLookupError:
            active = False
        except PermissionError:
            active = True
        except OSError:
            active = False
    return {
        "exists": True,
        "pid": pid,
        "active": active,
        "stale": not active,
    }


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
    target_version = _upgrade_target_version(upgrade_json)
    current_version = _repo_version(base)
    lock_path = runtime_root / "locks" / "upgrade.lock"
    lock_info = _upgrade_lock_info(lock_path)
    lock_exists = bool(lock_info.get("exists"))
    error = upgrade_json.get("error")
    failed_statuses = {"failed", "upgraded_restart_failed"}
    failed_services = _upgrade_failed_services(upgrade_json)
    remediation = _upgrade_remediation(upgrade_json)
    service_check: dict[str, Any] = {"checked": False, "services": []}

    out: dict[str, Any] = {
        "status": historical_status or None,
        "historical_status": historical_status or None,
        "target_version": target_version,
        "current_version": current_version,
        "error": error,
        "lock_exists": lock_exists,
        "lock_pid": lock_info.get("pid"),
        "lock_active": bool(lock_info.get("active")),
        "lock_stale": bool(lock_info.get("stale")),
        "failed_services": failed_services,
        "remediation": remediation,
        "runtime_failed": False,
        "warning": False,
        "checked": True,
    }

    if lock_exists and lock_info.get("active"):
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
        target_is_older = False
        if target_version and current_version:
            try:
                target_is_older = compare_versions(target_version, current_version) < 0
            except ValueError:
                target_is_older = False
        if not target_is_older:
            return {
                **out,
                "status": "failed",
                "runtime_failed": True,
                "warning": False,
                "reason": "upgrade_target_version_not_active",
            }
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


def _config_authority_payload(
    cfg: dict[str, Any],
    *,
    config_path: Path,
    config_key: Any,
    base: Path,
    mask_path: Callable[[Any], str | None],
) -> dict[str, Any]:
    market = infer_runtime_config_market(
        config_key=str(config_key or "").strip().lower() or None,
        config_path=config_path,
        config=cfg,
    )
    generated = cfg.get(GENERATED_KEY) if isinstance(cfg, dict) else None
    generated_dict = generated if isinstance(generated, dict) else {}
    identity = check_runtime_config_identity(
        cfg,
        config_key=str(config_key or "").strip().lower() or None,
        runtime_config_path=config_path,
    )
    freshness: dict[str, Any] = {"ok": False, "errors": [{"code": "market_not_inferred"}]}
    if market:
        freshness = check_runtime_config_freshness(
            cfg,
            repo_root=base,
            market=market,
            runtime_config_path=config_path,
        )
    errors = [
        item
        for item in [*(identity.get("errors") or []), *(freshness.get("errors") or [])]
        if isinstance(item, dict)
    ]
    source_summary = _generated_source_summary(generated_dict, base=base)
    first_error = errors[0] if errors else None
    yaml_source = _first_source_by_role(source_summary, "market_user")
    system_source = _first_source_by_role(source_summary, "system")
    return {
        "ok": bool(identity.get("ok")) and bool(freshness.get("ok")),
        "authoring_source": "config.yaml",
        "runtime_config_path": mask_path(config_path),
        "market": market,
        "source_format": str(generated_dict.get("source_format") or "").strip().lower() or None,
        "required_source_format": identity.get("required_source_format"),
        "config_yaml_path": yaml_source.get("path"),
        "config_yaml_sha256": yaml_source.get("sha256"),
        "system_config_path": system_source.get("path"),
        "system_config_sha256": system_source.get("sha256"),
        "stale_or_invalid_reason": (
            str(first_error.get("message") or first_error.get("code"))
            if isinstance(first_error, dict)
            else None
        ),
        "rebuild_command": identity.get("rebuild_command") or freshness.get("rebuild_command"),
        "identity": _authority_check_summary(identity),
        "freshness": _authority_check_summary(freshness),
        "sources": source_summary,
    }


def _authority_check_summary(payload: dict[str, Any]) -> dict[str, Any]:
    errors = [item for item in (payload.get("errors") or []) if isinstance(item, dict)]
    return {
        "ok": bool(payload.get("ok")),
        "error_count": len(errors),
        "errors": errors[:5],
    }


def _generated_source_summary(generated: dict[str, Any], *, base: Path) -> list[dict[str, Any]]:
    raw_sources = generated.get("sources")
    sources = raw_sources if isinstance(raw_sources, list) else []
    out: list[dict[str, Any]] = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path")
        path_display = None
        if raw_path:
            path = Path(str(raw_path)).expanduser()
            if not path.is_absolute():
                path = (base / path).resolve()
            path_display = _relative_path(path, base=base)
        out.append(
            {
                "role": str(item.get("role") or "").strip() or None,
                "loaded": bool(item.get("loaded")),
                "enabled": bool(item.get("enabled", True)),
                "optional": bool(item.get("optional", False)),
                "path": path_display,
                "sha256": str(item.get("sha256") or "").strip() or None,
                "inline": bool(item.get("inline", False)),
            }
        )
    return out


def _first_source_by_role(sources: list[dict[str, Any]], role: str) -> dict[str, Any]:
    for item in sources:
        if str(item.get("role") or "") == role:
            return item
    return {}


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


def _latest_run_event_prefetch_summary(latest_run_payload: dict[str, Any] | None) -> dict[str, Any]:
    state = latest_run_payload.get("state") if isinstance(latest_run_payload, dict) else {}
    state_payload: dict[str, Any] = state if isinstance(state, dict) else {}
    info_raw = state_payload.get("event_snapshot")
    info: dict[str, Any] = info_raw if isinstance(info_raw, dict) else {}
    out: dict[str, Any] = {
        "available": bool(info.get("exists")),
        "exists": bool(info.get("exists")),
        "path": info.get("path"),
    }
    payload_raw = info.get("json")
    payload: dict[str, Any] = payload_raw if isinstance(payload_raw, dict) else {}
    summary_raw = payload.get("summary")
    summary: dict[str, Any] = summary_raw if isinstance(summary_raw, dict) else {}
    out.update(summary)
    return out


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
    shared_notify_summary_raw = shared_payload.get("notify_summary")
    shared_notify_summary: dict[str, Any] = shared_notify_summary_raw if isinstance(shared_notify_summary_raw, dict) else {}
    route_summary = _resolve_notification_route_summary(cfg)

    no_send = bool(tick_metrics.get("no_send") or shared_payload.get("no_send"))
    account_messages_count = int(
        notify_summary.get("account_messages_count")
        or tick_metrics.get("account_messages_count")
        or shared_notify_summary.get("account_messages_count")
        or shared_payload.get("account_messages_count")
        or 0
    )
    send_attempted_count = int(
        notify_summary.get("send_attempted_count")
        or tick_metrics.get("send_attempted_count")
        or shared_notify_summary.get("send_attempted_count")
        or shared_payload.get("send_attempted_count")
        or 0
    )
    send_confirmed_count = int(
        notify_summary.get("send_confirmed_count")
        or tick_metrics.get("send_confirmed_count")
        or shared_notify_summary.get("send_confirmed_count")
        or shared_payload.get("send_confirmed_count")
        or 0
    )
    send_failed_count = int(
        notify_summary.get("send_failed_count")
        or tick_metrics.get("send_failed_count")
        or shared_notify_summary.get("send_failed_count")
        or shared_payload.get("send_failed_count")
        or 0
    )
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
    elif not bool(route_summary.get("configured")):
        status = "notification_route_missing"
        reason = "notifications route is missing or incomplete"
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
            "event_snapshot": _json_file_info(
                run_dir / "state" / "event_snapshot.json",
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


def _latest_run_payload_for_market(
    *,
    base: Path,
    pointer_path: Path,
    runs_root: Path,
    accounts: list[str],
    read_json_object_or_empty: Callable[[Path], dict[str, Any]],
    max_notification_chars: int,
    desired_market: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    skipped_market_mismatch_count = 0
    searched_count = 0
    seen_dirs: set[Path] = set()

    raw_pointer = _read_text(pointer_path)
    if raw_pointer:
        pointed = Path(raw_pointer).expanduser()
        if not pointed.is_absolute():
            pointed = (base / pointed).resolve()
        if pointed.exists() and pointed.is_dir():
            seen_dirs.add(pointed.resolve())
            candidate = _run_payload(
                pointed,
                accounts=accounts,
                base=base,
                read_json_object_or_empty=read_json_object_or_empty,
                max_notification_chars=max_notification_chars,
            )
            if _run_payload_matches_market(candidate, desired_market):
                return candidate, {
                    "requested": False,
                    "source": "last_run_dir_or_mtime",
                    "path": candidate.get("path"),
                    "found": True,
                    "market_filter": desired_market,
                    "searched_count": searched_count,
                    "skipped_market_mismatch_count": skipped_market_mismatch_count,
                }
            skipped_market_mismatch_count += 1

    for run_dir in _run_dirs_newest_first(runs_root):
        if run_dir.resolve() in seen_dirs:
            continue
        searched_count += 1
        candidate = _run_payload(
            run_dir,
            accounts=accounts,
            base=base,
            read_json_object_or_empty=read_json_object_or_empty,
            max_notification_chars=max_notification_chars,
        )
        if not _run_payload_matches_market(candidate, desired_market):
            skipped_market_mismatch_count += 1
            continue
        return candidate, {
            "requested": False,
            "source": "last_run_dir_or_mtime",
            "path": candidate.get("path"),
            "found": True,
            "market_filter": desired_market,
            "searched_count": searched_count,
            "skipped_market_mismatch_count": skipped_market_mismatch_count,
        }

    return None, {
        "requested": False,
        "source": "last_run_dir_or_mtime",
        "path": None,
        "found": False,
        "market_filter": desired_market,
        "searched_count": searched_count,
        "skipped_market_mismatch_count": skipped_market_mismatch_count,
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


def _normalize_market(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    if text in {"US", "USA"}:
        return "US"
    if text in {"HK", "HKG", "HKEX"}:
        return "HK"
    return None


def _collect_market_values(value: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, bool):
                if item:
                    market = _normalize_market(key)
                    if market:
                        out.add(market)
                continue
            out.update(_collect_market_values(item))
        return out
    if isinstance(value, (list, tuple, set)):
        for item in value:
            out.update(_collect_market_values(item))
        return out
    market = _normalize_market(value)
    if market:
        out.add(market)
    return out


def _desired_runtime_market(payload: dict[str, Any], cfg: dict[str, Any], *, config_path: Path) -> str | None:
    for value in (payload.get("config_key"),):
        market = _normalize_market(value)
        if market:
            return market

    payload_markets = _collect_market_values(payload.get("markets"))
    if len(payload_markets) == 1:
        return next(iter(payload_markets))

    symbol_markets: set[str] = set()
    symbols_raw = cfg.get("symbols")
    if isinstance(symbols_raw, list):
        for item in symbols_raw:
            if isinstance(item, dict):
                symbol_markets.update(_collect_market_values(item.get("market")))
    if len(symbol_markets) == 1:
        return next(iter(symbol_markets))

    name = config_path.name.lower()
    if "config.us" in name or name.startswith("us."):
        return "US"
    if "config.hk" in name or name.startswith("hk."):
        return "HK"
    return None


def _run_payload_markets(run_payload: dict[str, Any]) -> set[str]:
    state_raw = run_payload.get("state")
    state: dict[str, Any] = state_raw if isinstance(state_raw, dict) else {}
    payloads: list[dict[str, Any]] = [
        _json_payload(state.get("tick_metrics")),
        _json_payload(state.get("last_run")),
    ]
    tick_metrics = payloads[0]
    scheduler_decision = tick_metrics.get("scheduler_decision")
    if isinstance(scheduler_decision, dict):
        payloads.append(scheduler_decision)

    accounts_raw = run_payload.get("accounts")
    accounts: dict[str, Any] = accounts_raw if isinstance(accounts_raw, dict) else {}
    for item in accounts.values():
        if isinstance(item, dict):
            payloads.append(_json_payload(item.get("last_run")))

    markets: set[str] = set()
    for item in payloads:
        for key in (
            "market",
            "markets",
            "market_key",
            "config_key",
            "markets_to_run",
            "scheduler_markets",
            "scheduler_market",
        ):
            if key in item:
                markets.update(_collect_market_values(item.get(key)))
    return markets


def _run_payload_matches_market(run_payload: dict[str, Any], desired_market: str | None) -> bool:
    if desired_market is None:
        return True
    observed_markets = _run_payload_markets(run_payload)
    return not observed_markets or desired_market in observed_markets


def _nested(payload: Any, *keys: str) -> Any:
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _latest_run_expects_static_notification(latest_run_payload: dict[str, Any] | None) -> bool:
    if latest_run_payload is None:
        return True
    if not _run_payload_has_scan(latest_run_payload):
        return False
    tick_metrics = _json_payload(_nested(latest_run_payload, "state", "tick_metrics"))
    scheduler = _dict(tick_metrics.get("scheduler_decision"))
    if scheduler.get("should_run_scan") is False:
        return False
    if scheduler.get("should_notify") is False:
        return False
    if scheduler.get("is_notify_window_open") is False:
        return False
    if tick_metrics.get("no_send") is True:
        return False
    return True


def _run_payload_account_notification_exists(run_payload: dict[str, Any] | None) -> bool:
    if not isinstance(run_payload, dict):
        return False
    accounts = _dict(run_payload.get("accounts"))
    for item in accounts.values():
        payload = _dict(item)
        notification = _dict(payload.get("notification"))
        if notification.get("exists"):
            return True
    return False


def _latest_run_auto_close_failures(latest_run_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(latest_run_payload, dict):
        return []
    accounts = _dict(latest_run_payload.get("accounts"))
    failures: list[dict[str, Any]] = []
    for account, item in accounts.items():
        payload = _dict(item)
        maintenance = _json_payload(payload.get("expired_position_maintenance"))
        if not maintenance:
            continue
        raw_errors = maintenance.get("errors")
        errors = [str(err) for err in raw_errors if str(err).strip()] if isinstance(raw_errors, list) else []
        mode = str(maintenance.get("mode") or "").strip().lower()
        status = str(maintenance.get("status") or "").strip().lower()
        reason = str(maintenance.get("reason") or "").strip()
        if mode not in {"error", "failed"} and status != "failed" and not errors:
            continue
        failures.append(
            {
                "account": str(account),
                "reason": reason or (errors[0] if errors else mode or status or "unknown"),
                "errors": errors,
            }
        )
    return failures


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
    desired_market: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    searched_count = 0
    skipped_market_mismatch_count = 0
    for run_dir in _run_dirs_newest_first(runs_root):
        searched_count += 1
        candidate = _run_payload(
            run_dir,
            accounts=accounts,
            base=base,
            read_json_object_or_empty=read_json_object_or_empty,
            max_notification_chars=max_notification_chars,
        )
        if not _run_payload_matches_market(candidate, desired_market):
            skipped_market_mismatch_count += 1
            continue
        if _run_payload_has_scan(candidate):
            return candidate, {
                "source": "runs_root_mtime",
                "searched_count": searched_count,
                "market_filter": desired_market,
                "skipped_market_mismatch_count": skipped_market_mismatch_count,
                "found": True,
                "path": candidate.get("path"),
            }
    return None, {
        "source": "runs_root_mtime",
        "searched_count": searched_count,
        "market_filter": desired_market,
        "skipped_market_mismatch_count": skipped_market_mismatch_count,
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
    payload, profile_meta = _merge_explicit_service_profile(payload, base=base)
    config_path, cfg = load_runtime_config(
        config_key=payload.get("config_key"),
        config_path=payload.get("config_path"),
        require_identity=False,
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
    payload, profile_meta = _merge_runtime_service_profile(
        payload,
        base=base,
        runtime_root=ledger_runtime_root,
        existing_profile_meta=profile_meta,
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
        default=base / "output_shared" / "reports",
    )
    state_dir = _resolve_under_base(
        payload.get("state_dir"),
        base=base,
        default=base / "output_shared" / "state",
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
    desired_market = _desired_runtime_market(payload, cfg, config_path=config_path)
    requested_run, latest_run_selection = _requested_run_dir_from_payload(payload, base=base, runs_root=runs_root)
    latest_run_payload: dict[str, Any] | None = None
    if latest_run_selection.get("requested"):
        latest_run = requested_run
        if latest_run is not None:
            latest_run_payload = _run_payload(
                latest_run,
                accounts=accounts,
                base=base,
                read_json_object_or_empty=read_json_object_or_empty,
                max_notification_chars=max_notification_chars,
            )
    else:
        latest_run_payload, latest_run_selection = _latest_run_payload_for_market(
            base=base,
            pointer_path=pointer_path,
            runs_root=runs_root,
            accounts=accounts,
            read_json_object_or_empty=read_json_object_or_empty,
            max_notification_chars=max_notification_chars,
            desired_market=desired_market,
        )

    prefetch_summary = _latest_run_prefetch_summary(latest_run_payload)
    event_prefetch_summary = _latest_run_event_prefetch_summary(latest_run_payload)
    latest_scanned_run_payload, latest_scanned_run_selection = _latest_scanned_run_payload(
        runs_root=runs_root,
        accounts=accounts,
        base=base,
        read_json_object_or_empty=read_json_object_or_empty,
        max_notification_chars=max_notification_chars,
        desired_market=desired_market,
    )
    latest_scanned_prefetch_summary = _latest_run_prefetch_summary(latest_scanned_run_payload)
    latest_scanned_event_prefetch_summary = _latest_run_event_prefetch_summary(latest_scanned_run_payload)

    warnings: list[str] = []
    warning_codes: list[str] = []
    if latest_run_selection.get("requested") and not latest_run_selection.get("found"):
        source = latest_run_selection.get("source")
        value = latest_run_selection.get("value")
        warnings.append(f"Requested runtime run not found: {source}={value}.")
    if not shared_last_run.get("exists"):
        warnings.append("No last_run.json found under output_shared/state.")
    if (
        _latest_run_expects_static_notification(latest_run_payload)
        and not notification.get("exists")
        and not any(item["notification"].get("exists") for item in account_status.values())
        and not _run_payload_account_notification_exists(latest_run_payload)
    ):
        warnings.append("No symbols_notification.txt found for latest scanned run or legacy report paths.")
    auto_close_failures = _latest_run_auto_close_failures(latest_run_payload)
    if auto_close_failures:
        for item in auto_close_failures:
            warnings.append(f"Auto-close {item['account']} failed: {item['reason']}.")
        warning_codes.append("AUTO_CLOSE_FAILED")
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

    shared_last_run_json_raw = shared_last_run.get("json")
    shared_last_run_json: dict[str, Any] = shared_last_run_json_raw if isinstance(shared_last_run_json_raw, dict) else {}
    latest_status = shared_last_run_json.get("status") or shared_last_run_json.get("last_status")
    env_file_path = _assistant_env_file_path_from_payload(payload, base=base)
    effective_env, environment = build_effective_env_with_status(
        repo_root=base,
        env_file=env_file_path,
        mask_path=mask_path,
    )
    env_file_warnings = _runtime_status_env_file_warnings(effective_env)
    environment["warnings"] = env_file_warnings
    if env_file_warnings:
        warnings.extend(env_file_warnings)
        warning_codes.append("ENV_FILE")
    assistant_runtime = _assistant_runtime_summary(
        base=base,
        runtime_root=ledger_runtime_root,
        payload=payload,
        mask_path=mask_path,
    )
    channel_status = build_channel_status(
        base=base,
        runtime_root=ledger_runtime_root,
        payload=payload,
        environ=effective_env.values,
        mask_path=mask_path,
        include_service_status=bool(payload.get("include_service_status", False)),
    )
    channel_health_raw = channel_status.get("channels")
    channel_health = channel_health_raw if isinstance(channel_health_raw, dict) else {}

    config_authority = _config_authority_payload(
        cfg,
        config_path=config_path,
        config_key=payload.get("config_key"),
        base=base,
        mask_path=mask_path,
    )
    service_profile = _service_profile_summary(payload)
    if profile_meta is not None:
        service_profile["profile"] = profile_meta

    data: dict[str, Any] = {
        "config": {
            "config_path": mask_path(config_path),
            "accounts": accounts,
            "config_key": payload.get("config_key"),
        },
        "config_authority": config_authority,
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
        "event_prefetch": event_prefetch_summary,
        "latest_scanned_run_event_prefetch": latest_scanned_event_prefetch_summary,
        "trigger_context": trigger_context,
        "notification_diagnosis": notification_diagnosis,
        "environment": environment,
        "channel_status": channel_status,
        "channel_health": channel_health,
        "account_summary": {},
        "freshness": {},
        "service_profile": service_profile,
        "assistant_runtime": assistant_runtime,
        "summary": {
            "ok": not warnings,
            "warning_count": len(warnings),
            "warning_codes": warning_codes,
            "latest_status": latest_status,
        },
    }
    data["account_summary"] = _account_summary(data)
    data["freshness"] = _freshness_from_runtime_status(data, max_age_minutes=max_run_age_minutes)
    data["summary"]["config_authority_ok"] = bool(config_authority.get("ok"))
    data["summary"]["config_authority_reason"] = config_authority.get("stale_or_invalid_reason")
    data["summary"]["freshness_status"] = data["freshness"].get("status")
    data["summary"]["account_count"] = data["account_summary"].get("account_count")
    data["summary"]["latest_run_path"] = latest_run_payload.get("path") if latest_run_payload else None
    data["summary"]["latest_scanned_run_path"] = latest_scanned_run_payload.get("path") if latest_scanned_run_payload else None
    data["summary"]["prefetch_available"] = prefetch_summary.get("available")
    data["summary"]["prefetch_bottleneck"] = prefetch_summary.get("primary_bottleneck")
    data["summary"]["latest_scanned_run_prefetch_available"] = latest_scanned_prefetch_summary.get("available")
    data["summary"]["latest_scanned_run_prefetch_bottleneck"] = latest_scanned_prefetch_summary.get("primary_bottleneck")
    data["summary"]["event_prefetch_available"] = event_prefetch_summary.get("available")
    data["summary"]["event_prefetch_errors"] = event_prefetch_summary.get("errors")
    data["summary"]["latest_scanned_run_event_prefetch_available"] = latest_scanned_event_prefetch_summary.get("available")
    data["summary"]["latest_scanned_run_event_prefetch_errors"] = latest_scanned_event_prefetch_summary.get("errors")
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
    data["summary"]["service_upgrade_target_version"] = upgrade_evaluation.get("target_version") or (upgrade_json.get("target_version") if upgrade_json else None)
    data["summary"]["service_upgrade_current_version"] = upgrade_evaluation.get("current_version")
    data["summary"]["service_upgrade_error"] = upgrade_evaluation.get("error")
    data["summary"]["service_upgrade_runtime_failed"] = bool(upgrade_evaluation.get("runtime_failed"))
    data["summary"]["service_upgrade_reason"] = upgrade_evaluation.get("reason")
    data["summary"]["service_upgrade_failed_services"] = upgrade_evaluation.get("failed_services")
    data["summary"]["service_upgrade_remediation"] = upgrade_evaluation.get("remediation")
    data["summary"]["service_drift_status"] = service_drift_summary.get("status")
    data["summary"]["service_drift_missing_units"] = service_drift.get("missing_installed_units")
    data["summary"]["service_drift_missing_required_units"] = missing_required_units
    data["summary"]["env_file"] = environment.get("env_file")
    data["summary"]["env_file_loaded"] = bool(environment.get("env_file_loaded"))
    assistant_config_summary = assistant_runtime.get("config") if isinstance(assistant_runtime.get("config"), dict) else {}
    assistant_llm_summary = assistant_runtime.get("llm") if isinstance(assistant_runtime.get("llm"), dict) else {}
    assistant_audit_summary = assistant_runtime.get("audit") if isinstance(assistant_runtime.get("audit"), dict) else {}
    assistant_latest = assistant_audit_summary.get("latest") if isinstance(assistant_audit_summary.get("latest"), dict) else {}
    data["summary"]["assistant_enabled"] = bool(assistant_config_summary.get("enabled"))
    assistant_agent_loop = assistant_config_summary.get("agent_loop") if isinstance(assistant_config_summary.get("agent_loop"), dict) else {}
    assistant_planner = assistant_config_summary.get("planner") if isinstance(assistant_config_summary.get("planner"), dict) else {}
    data["summary"]["assistant_agent_loop_enabled"] = bool(assistant_agent_loop.get("enabled", assistant_planner.get("enabled")))
    data["summary"]["assistant_planner_enabled"] = bool(assistant_planner.get("enabled"))
    data["summary"]["assistant_llm_enabled"] = bool(assistant_llm_summary.get("enabled"))
    data["summary"]["assistant_llm_provider"] = assistant_llm_summary.get("provider")
    data["summary"]["assistant_latest_route"] = assistant_latest.get("route")
    data["summary"]["assistant_latest_intent"] = assistant_latest.get("intent_name")
    data["summary"]["assistant_latest_llm_reason"] = assistant_latest.get("llm_reason")
    wechat_health = channel_health.get("wechat_clawbot") if isinstance(channel_health.get("wechat_clawbot"), dict) else {}
    data["summary"]["wechat_clawbot_configured"] = bool(wechat_health.get("configured"))
    data["summary"]["wechat_clawbot_available"] = bool(wechat_health.get("available"))
    data["summary"]["wechat_clawbot_allowed_senders_configured"] = bool(wechat_health.get("allowed_senders_configured"))
    data["summary"]["wechat_clawbot_bot_token_configured"] = bool(wechat_health.get("bot_token_configured"))
    return data, warnings, {"config_path": mask_path(config_path)}


__all__ = [
    "runtime_status_tool",
]
