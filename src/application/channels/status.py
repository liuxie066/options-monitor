from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from domain.domain.multi_tick import FEISHU_APP_NOTIFICATION_PROVIDER, WECHAT_CLAWBOT_NOTIFICATION_PROVIDER
from src.application.agent_tool_contracts import build_response, mask_path as default_mask_path
from src.application.assistant.audit import default_audit_db_path
from src.application.secret_resolver import (
    DEFAULT_FEISHU_BOT_ALLOWED_OPEN_IDS_ENV,
    DEFAULT_FEISHU_BOT_APP_ID_ENV,
    DEFAULT_FEISHU_BOT_APP_SECRET_ENV,
    DEFAULT_FEISHU_BOT_USER_OPEN_ID_ENV,
)
from src.application.secret_store import FEISHU_BOT_APP_SECRET, SecretError, resolve_secret_status
from src.application.service_deploy import service_status_from_profile


MaskPathFn = Callable[[Any], str | None]
FEISHU_CHANNEL = "feishu"


def channel_status_response(
    *,
    base: Path,
    payload: dict[str, Any] | None = None,
    runtime_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    mask_path: MaskPathFn = default_mask_path,
    include_service_status: bool = False,
    run_cmd: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    data = build_channel_status(
        base=base,
        payload=payload,
        runtime_root=runtime_root,
        environ=environ,
        mask_path=mask_path,
        include_service_status=include_service_status,
        run_cmd=run_cmd,
    )
    return build_response(tool_name="channel.status", ok=True, data=data)


def build_channel_status(
    *,
    base: Path,
    payload: dict[str, Any] | None = None,
    runtime_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    mask_path: MaskPathFn = default_mask_path,
    include_service_status: bool = False,
    run_cmd: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    raw_payload = dict(payload or {})
    runtime = _runtime_root(raw_payload, base=base, runtime_root=runtime_root)
    merged_payload, profile_meta = _merge_service_profile_payload(raw_payload, base=base, runtime_root=runtime)
    if profile_meta is not None and profile_meta.get("path"):
        profile_meta = {**profile_meta, "path": mask_path(profile_meta.get("path"))}
    env = dict(os.environ if environ is None else environ)
    service_statuses, service_status_error = _service_statuses_from_payload(
        merged_payload,
        include_service_status=include_service_status,
        run_cmd=run_cmd,
    )
    channels = {
        FEISHU_CHANNEL: _feishu_channel_health(
            base=base,
            runtime_root=runtime,
            payload=merged_payload,
            environ=env,
            mask_path=mask_path,
            services=service_statuses,
            service_status_checked=include_service_status,
            service_status_error=service_status_error,
        ),
        WECHAT_CLAWBOT_NOTIFICATION_PROVIDER: _wechat_clawbot_channel_health(
            base=base,
            runtime_root=runtime,
            payload=merged_payload,
            mask_path=mask_path,
            services=service_statuses,
            service_status_checked=include_service_status,
            service_status_error=service_status_error,
        ),
    }
    return {
        "runtime_root": mask_path(runtime),
        "profile": profile_meta or {"loaded": False},
        "channels": channels,
        "summary": _summary(channels),
    }


def _summary(channels: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "configured_channels": sorted(name for name, status in channels.items() if bool(status.get("configured"))),
        "available_channels": sorted(name for name, status in channels.items() if bool(status.get("available"))),
        "feishu_available": bool(channels.get(FEISHU_CHANNEL, {}).get("available")),
        "wechat_clawbot_available": bool(channels.get(WECHAT_CLAWBOT_NOTIFICATION_PROVIDER, {}).get("available")),
    }


def _feishu_channel_health(
    *,
    base: Path,
    runtime_root: Path,
    payload: dict[str, Any],
    environ: Mapping[str, str],
    mask_path: MaskPathFn,
    services: dict[str, dict[str, Any]],
    service_status_checked: bool,
    service_status_error: str | None,
) -> dict[str, Any]:
    del runtime_root
    profile = _dict(payload.get("feishu_ws"))
    assistant_config_path, _assistant_config_explicit = _assistant_config_path_from_payload(payload)
    cfg, config_error = _load_assistant_config(assistant_config_path, base=base)
    inbound = _dict(_dict(cfg.get("inbound")).get("feishu_ws"))
    app_id_configured = bool(_first_text(environ.get(DEFAULT_FEISHU_BOT_APP_ID_ENV)))
    user_open_id = _first_text(environ.get(DEFAULT_FEISHU_BOT_USER_OPEN_ID_ENV))
    allowed_open_ids = _split_csv(
        _first_text(environ.get(DEFAULT_FEISHU_BOT_ALLOWED_OPEN_IDS_ENV))
    ) or ((user_open_id,) if user_open_id else ())
    credential_status = None
    credential_error = None
    try:
        credential_status = resolve_secret_status(
            FEISHU_BOT_APP_SECRET,
            environ=environ,
            legacy_env_name=DEFAULT_FEISHU_BOT_APP_SECRET_ENV,
        )
    except (SecretError, ValueError) as exc:
        credential_error = f"{type(exc).__name__}: {exc}"
    credentials_ready = bool(
        app_id_configured
        and credential_status is not None
        and credential_status.configured
    )
    audit_db = _first_text(profile.get("audit_db"), payload.get("audit_db"), payload.get("inbound_audit_db"), environ.get("OM_INBOUND_AUDIT_DB"))
    audit_path = _resolve_path(audit_db, base=base) if audit_db else default_audit_db_path()
    service = _channel_service_status(
        services,
        names=("options-monitor-feishu-ws.service", "com.options-monitor.feishu-ws"),
        status_checked=service_status_checked,
    )
    service_present = bool(service.get("present"))
    allowed_senders_configured = bool(allowed_open_ids)
    configured = bool(
        profile.get("enabled")
        or inbound
        or service_present
        or credentials_ready
        or allowed_senders_configured
        or audit_path.exists()
    )
    out: dict[str, Any] = {
        "configured": configured,
        "provider": FEISHU_APP_NOTIFICATION_PROVIDER,
        "available": configured and credentials_ready and allowed_senders_configured,
        "profile_enabled": bool(profile.get("enabled")),
        "service_present": service_present,
        "service_status_checked": service_status_checked,
        "service": service.get("service"),
        "service_active": service.get("active"),
        "service_enabled": service.get("enabled"),
        "assistant_config_path": mask_path(assistant_config_path) if assistant_config_path is not None else None,
        "assistant_config_loaded": bool(cfg),
        "audit_db": mask_path(audit_path),
        "audit_db_exists": audit_path.exists(),
        "credentials_configured": credentials_ready,
        "allowed_senders_configured": allowed_senders_configured,
        "allowed_open_ids_count": len(allowed_open_ids),
        "reply_enabled": _config_bool_or_default(inbound.get("reply_enabled"), default=True),
        "max_reply_chars": _int_or_default(inbound.get("max_reply_chars"), default=3500),
    }
    if config_error:
        out["config_error"] = config_error
    if credential_error:
        out["credential_error"] = credential_error
    if service_status_error:
        out["service_status_error"] = service_status_error
    return out


def _wechat_clawbot_channel_health(
    *,
    base: Path,
    runtime_root: Path,
    payload: dict[str, Any],
    mask_path: MaskPathFn,
    services: dict[str, dict[str, Any]],
    service_status_checked: bool,
    service_status_error: str | None,
) -> dict[str, Any]:
    profile = _dict(payload.get("wechat_clawbot"))
    assistant_config_path, _assistant_config_explicit = _assistant_config_path_from_payload(payload)
    cfg, config_error = _load_assistant_config(assistant_config_path, base=base)
    inbound = _dict(_dict(cfg.get("inbound")).get("wechat_clawbot"))
    label = _first_text(profile.get("label"), inbound.get("label")) or "default"
    state_dir_raw = _first_text(profile.get("state_dir"), inbound.get("state_dir"))
    if state_dir_raw:
        state_dir = _resolve_path(state_dir_raw, base=base)
    else:
        state_dir = runtime_root / "output_shared" / "state" / "channels" / "wechat_clawbot" / label
    state_payload, state_error = _read_wechat_clawbot_state(state_dir)
    bindings_payload, bindings_error = _read_wechat_clawbot_bindings(state_dir)
    bindings_status = _wechat_clawbot_bindings_status(bindings_payload, label=label)
    allowed_senders_configured = bool(
        profile.get("allowed_senders_configured")
        or _first_text(profile.get("allowed_senders"), inbound.get("allowed_senders"))
    )
    service = _channel_service_status(
        services,
        names=("options-monitor-wechat-clawbot.service", "com.options-monitor.wechat-clawbot"),
        status_checked=service_status_checked,
    )
    service_present = bool(service.get("present"))
    cursor = str(state_payload.get("get_updates_buf") or "")
    configured = bool(profile.get("enabled")) or bool(inbound) or allowed_senders_configured or state_dir.exists() or service_present
    out: dict[str, Any] = {
        "configured": configured,
        "available": configured and allowed_senders_configured and bool(str(state_payload.get("bot_token") or "").strip()),
        "profile_enabled": bool(profile.get("enabled")),
        "service_present": service_present,
        "service_status_checked": service_status_checked,
        "service": service.get("service"),
        "service_active": service.get("active"),
        "service_enabled": service.get("enabled"),
        "label": label,
        "state_dir": mask_path(state_dir),
        "state_exists": state_dir.exists(),
        "state_loaded": bool(state_payload),
        "bindings_path": mask_path(state_dir / "bindings.json"),
        "bindings_exists": (state_dir / "bindings.json").exists(),
        "bindings_loaded": bool(bindings_payload),
        **bindings_status,
        "bot_token_configured": bool(str(state_payload.get("bot_token") or "").strip()),
        "base_url_configured": bool(str(state_payload.get("base_url") or "").strip()),
        "cursor_configured": bool(cursor),
        "cursor_length": len(cursor),
        "assistant_config_path": mask_path(assistant_config_path) if assistant_config_path is not None else None,
        "assistant_config_loaded": bool(cfg),
        "allowed_senders_configured": allowed_senders_configured,
        "allowed_senders_source": _first_text(profile.get("allowed_senders_source")),
        "reply_enabled": _config_bool_or_default(inbound.get("reply_enabled"), default=True),
        "max_reply_chars": _int_or_default(inbound.get("max_reply_chars"), default=3500),
        "poll_interval_sec": _float_or_default(inbound.get("poll_interval_sec"), default=3.0),
        "timeout_sec": _int_or_default(inbound.get("timeout_sec"), default=20),
    }
    if config_error:
        out["config_error"] = config_error
    if state_error:
        out["state_error"] = state_error
    if bindings_error:
        out["bindings_error"] = bindings_error
    if service_status_error:
        out["service_status_error"] = service_status_error
    return out


def _service_statuses_from_payload(
    payload: dict[str, Any],
    *,
    include_service_status: bool,
    run_cmd: Callable[..., Any],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    services = payload.get("services")
    if not isinstance(services, list) or not services:
        return {}, None
    profile = dict(payload)
    try:
        status = service_status_from_profile(
            profile,
            include_status=include_service_status,
            include_enabled=include_service_status,
            run_cmd=run_cmd,
        )
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    out: dict[str, dict[str, Any]] = {}
    for item in status.get("services") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            out[name] = item
    return out, None


def _channel_service_status(
    services: dict[str, dict[str, Any]],
    *,
    names: tuple[str, ...],
    status_checked: bool,
) -> dict[str, Any]:
    selected = next((services[name] for name in names if name in services), None)
    if selected is None:
        return {
            "present": False,
            "status_checked": status_checked,
            "service": None,
            "active": None,
            "enabled": None,
        }
    return {
        "present": True,
        "status_checked": status_checked,
        "service": selected,
        "active": _command_status_ok(selected.get("active") if isinstance(selected.get("active"), dict) else selected),
        "enabled": _command_status_ok(selected.get("enabled") if isinstance(selected.get("enabled"), dict) else None),
    }


def _command_status_ok(value: Any) -> bool | None:
    if not isinstance(value, dict) or "status" not in value:
        return None
    return str(value.get("status") or "").strip().lower() == "ok"


def _load_assistant_config(path: Path | None, *, base: Path) -> tuple[dict[str, Any], str | None]:
    if path is None:
        return {}, None
    try:
        from src.application.assistant.config_loader import load_assistant_config

        _path, loaded = load_assistant_config(config_path=path, repo_root=base, missing_ok=True)
        return (loaded if isinstance(loaded, dict) else {}), None
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def _read_wechat_clawbot_state(state_dir: Path) -> tuple[dict[str, Any], str | None]:
    path = state_dir / "state.json"
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return {}, "state.json is not an object"
    return payload, None


def _read_wechat_clawbot_bindings(state_dir: Path) -> tuple[dict[str, Any], str | None]:
    path = state_dir / "bindings.json"
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return {}, "bindings.json is not an object"
    return payload, None


def _wechat_clawbot_bindings_status(payload: dict[str, Any], *, label: str) -> dict[str, Any]:
    bindings_raw = payload.get("bindings")
    bindings = bindings_raw if isinstance(bindings_raw, dict) else {}
    now_utc = datetime.now(timezone.utc)
    items: dict[str, dict[str, Any]] = {}
    newest_updated_at: str | None = None
    newest_age_seconds: int | None = None
    for name, raw in sorted(bindings.items(), key=lambda item: str(item[0])):
        if not isinstance(raw, dict):
            continue
        updated_at = _first_text(raw.get("updated_at_utc"), raw.get("updated_at")) or None
        age_seconds = _age_seconds(updated_at, now_utc=now_utc) if updated_at else None
        if age_seconds is not None and (newest_age_seconds is None or age_seconds < newest_age_seconds):
            newest_age_seconds = age_seconds
            newest_updated_at = updated_at
        group_id = _first_text(raw.get("group_id"))
        chat_key = _first_text(raw.get("chat_key"))
        last_text = _first_text(raw.get("last_text"))
        items[str(name)] = {
            "target": f"wechat:{label}:{name}",
            "updated_at_utc": updated_at,
            "age_seconds": age_seconds,
            "has_to_user_id": bool(_first_text(raw.get("to_user_id"))),
            "has_context_token": bool(_first_text(raw.get("context_token"))),
            "has_group_id": bool(group_id),
            "has_chat_key": bool(chat_key),
            "last_message_id": _first_text(raw.get("last_message_id")) or None,
            "last_text_present": bool(last_text),
            "last_text_length": len(last_text),
        }
    return {
        "binding_count": len(items),
        "binding_names": sorted(items),
        "bindings": items,
        "newest_binding_updated_at_utc": newest_updated_at,
        "newest_binding_age_seconds": newest_age_seconds,
    }


def _age_seconds(value: str | None, *, now_utc: datetime) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = now_utc - parsed.astimezone(timezone.utc)
    return max(0, int(delta.total_seconds()))


def _merge_service_profile_payload(
    payload: dict[str, Any],
    *,
    base: Path,
    runtime_root: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    profile_path = _profile_path(payload, base=base, runtime_root=runtime_root)
    if profile_path is None or not profile_path.exists():
        return payload, None
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return payload, {"loaded": False, "path": str(profile_path), "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(profile, dict):
        return payload, {"loaded": False, "path": str(profile_path), "error": "service profile is not an object"}
    merged = dict(payload)
    for key in (
        "service_provider",
        "repo_root",
        "runtime_root",
        "services",
        "env_file",
        "assistant_config_path",
        "feishu_ws",
        "wechat_clawbot",
    ):
        if key not in merged and key in profile:
            merged[key] = profile[key]
    return merged, {"loaded": True, "path": str(profile_path), "service_provider": profile.get("service_provider") or profile.get("provider")}


def _profile_path(payload: dict[str, Any], *, base: Path, runtime_root: Path) -> Path | None:
    raw = str(payload.get("profile_path") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else (base / path).resolve()
    candidate = runtime_root / "service.profile.json"
    return candidate if candidate.exists() else None


def _runtime_root(payload: dict[str, Any], *, base: Path, runtime_root: Path | None) -> Path:
    raw = str(payload.get("runtime_root") or "").strip()
    if runtime_root is not None:
        return runtime_root.expanduser().resolve()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else (base / path).resolve()
    return base


def _assistant_config_path_from_payload(payload: dict[str, Any]) -> tuple[Path | None, bool]:
    for source in (
        payload,
        _dict(payload.get("feishu_ws")),
        _dict(payload.get("wechat_clawbot")),
    ):
        raw = str(source.get("assistant_config_path") or "").strip()
        if raw:
            return Path(raw).expanduser(), True
    return None, False


def _resolve_path(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _split_csv(value: Any) -> tuple[str, ...]:
    out: list[str] = []
    for raw in str(value or "").split(","):
        item = raw.strip()
        if item and item not in out:
            out.append(item)
    return tuple(out)


def _config_bool_or_default(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _int_or_default(value: Any, *, default: int) -> int:
    try:
        out = int(value)
    except Exception:
        return default
    return out if out > 0 else default


def _float_or_default(value: Any, *, default: float) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return out if out >= 0 else default


__all__ = [
    "build_channel_status",
    "channel_status_response",
]
