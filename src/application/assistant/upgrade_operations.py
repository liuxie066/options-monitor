from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response, mask_path
from src.application.assistant.contracts import AssistantRequest, PerceptionResult
from src.application.assistant.operation_lifecycle import (
    build_cancelled_operation_response,
    build_previewed_operation_response,
    confirm_previewed_operation_or_raise,
    resolve_pending_operation_or_raise,
)
from src.application.assistant.operation_policy import enforce_upgrade_write_allowed
from src.application.assistant.operation_signature import hash_operation_payload, verify_operation_signature
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.operation_status_text import cannot_repeat_message, operation_candidate_hint, user_facing_operation_status
from src.application.service_upgrade import compare_versions, default_releases_root, default_upgrade_cache_root, service_upgrade, service_upgrade_check
from src.application.settings import build_effective_env
from src.application.secret_resolver import resolve_feishu_bot_config
from src.infrastructure.feishu_bot import reply_text_message


PREVIEW_INTENTS = frozenset({"upgrade_now"})
CONFIRM_INTENTS = frozenset({"upgrade_confirm", "upgrade_cancel"})
UPGRADE_OPERATION_TYPES = PREVIEW_INTENTS
UpgradeWorkerLauncher = Callable[[str, Path], dict[str, Any]]
ReplyFn = Callable[..., dict[str, Any]]
UPGRADE_WORKER_LAUNCHER: UpgradeWorkerLauncher | None = None
_DEFAULT_RUNTIME_ROOT = Path("/var/lib/options-monitor")


def handle_upgrade_operation(
    intent: PerceptionResult,
    request: AssistantRequest,
    *,
    command_id: str,
    store: InboundOperationStore,
) -> dict[str, Any]:
    policy = enforce_upgrade_write_allowed(channel=request.channel, sender_id=request.sender_id)
    if intent.intent_name in PREVIEW_INTENTS:
        payload = _build_operation_payload(intent.intent_name, dict(intent.arguments))
        return _preview_and_save(payload, request=request, command_id=command_id, store=store, ttl_seconds=policy.confirm_ttl_seconds)
    if intent.intent_name == "upgrade_confirm":
        return _confirm_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    if intent.intent_name == "upgrade_cancel":
        return _cancel_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported upgrade operation intent: {intent.intent_name}")


def _preview_and_save(
    payload: dict[str, Any],
    *,
    request: AssistantRequest,
    command_id: str,
    store: InboundOperationStore,
    ttl_seconds: int,
) -> dict[str, Any]:
    _attach_receipt_target(payload, request)
    preview = _preview_operation(payload)
    if _preview_has_no_upgrade(preview):
        return build_response(
            tool_name="inbound.upgrade",
            ok=True,
            data={
                "operation_type": payload["operation_type"],
                "status": "no_upgrade_available",
                "payload": payload,
                "preview": preview,
                "response_text": render_upgrade_response(
                    status="no_upgrade_available",
                    operation_id=command_id,
                    payload=payload,
                    preview=preview,
                ),
            },
            meta={"audit_db": mask_path(store.path)},
        )
    _freeze_preview_target(payload, preview)
    return build_previewed_operation_response(
        tool_name="inbound.upgrade",
        operation_id=command_id,
        request=request,
        store=store,
        payload=payload,
        preview=preview,
        ttl_seconds=ttl_seconds,
        response_text=lambda operation: render_upgrade_response(
            status="previewed",
            operation_id=command_id,
            payload=payload,
            preview=preview,
            expires_at=str(operation.get("expires_at") or ""),
        ),
    )


def _confirm_operation(*, operation_id: str | None, request: AssistantRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_upgrade_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=False,
        action="确认",
    )
    queued = {
        "operation_id": operation_id,
        "status": "confirmed",
        "task_status": "queued",
        "receipt_target": _receipt_target_from_request(request),
    }
    confirmed = confirm_previewed_operation_or_raise(
        operation_id=operation_id,
        operation=operation,
        operation_resolution=operation_resolution,
        store=store,
        subject="升级操作",
        expired_message="这条升级确认已过期，未执行升级。",
        expired_hint="请重新发送：立即升级",
        hash_mismatch_message="pending upgrade operation payload hash mismatch; refusing to upgrade",
        confirmed_result=queued,
    )
    operation_id = confirmed.operation_id
    operation_resolution = confirmed.operation_resolution
    payload = confirmed.payload
    try:
        launch = _launch_upgrade_worker(operation_id=operation_id, audit_db=store.path)
    except AgentToolError as exc:
        store.mark_failed(operation_id, result={"operation_id": operation_id, "status": "failed", "error": exc.code, "message": exc.message})
        raise
    except Exception as exc:
        failed = {"operation_id": operation_id, "status": "failed", "error": type(exc).__name__, "message": str(exc)}
        store.mark_failed(operation_id, result=failed)
        raise AgentToolError(code="INTERNAL_ERROR", message="升级确认已收到，但后台升级任务启动失败。", details=failed) from exc
    text = render_upgrade_response(status="confirmed", operation_id=operation_id, payload=payload, preview=operation.get("preview"), result=launch)
    return build_response(
        tool_name="inbound.upgrade",
        ok=True,
        data={
            "operation_id": operation_id,
            **operation_resolution,
            "operation_type": payload["operation_type"],
            "status": "confirmed",
            "payload_hash": confirmed.payload_hash,
            "payload": payload,
            "preview": operation.get("preview"),
            "result": launch,
            "response_text": text,
        },
        meta={"audit_db": mask_path(store.path)},
    )


def _cancel_operation(*, operation_id: str | None, request: AssistantRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_upgrade_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=True,
        action="取消",
    )
    text = f"升级操作已取消，未执行升级。\ncommand_id: {operation_id}"
    return build_cancelled_operation_response(
        tool_name="inbound.upgrade",
        operation_id=operation_id,
        operation=operation,
        operation_resolution=operation_resolution,
        store=store,
        response_text=text,
    )


def _resolve_upgrade_operation(
    *,
    operation_id: str | None,
    request: AssistantRequest,
    store: InboundOperationStore,
    allow_expired: bool,
    action: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return resolve_pending_operation_or_raise(
        operation_id=operation_id,
        request=request,
        store=store,
        operation_types=UPGRADE_OPERATION_TYPES,
        allow_expired=allow_expired,
        action=action,
        subject="升级操作",
        expired_message="这条升级确认已过期，未执行升级。",
        expired_hint="请重新发送：立即升级",
        none_hint="请先发送：立即升级",
        wrong_family_message="这不是升级操作，不能用确认升级/取消升级处理。",
        not_found_message="找不到待确认的升级操作。",
        not_found_hint="请检查 operation_id，或重新发送：立即升级",
        candidate_hint=_upgrade_candidate_hint,
    )


def _candidate_hint(prefix: str, candidates: Any) -> str:
    return operation_candidate_hint(prefix, candidates, heading="候选升级")


def _upgrade_candidate_hint(action: str, candidates: Any) -> str:
    return _candidate_hint("确认升级" if action == "确认" else "取消升级", candidates)


def _build_operation_payload(operation_type: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": "1.0", "operation_type": operation_type, "arguments": arguments}


def _attach_receipt_target(payload: dict[str, Any], request: AssistantRequest) -> None:
    payload["receipt_target"] = _receipt_target_from_request(request)


def _receipt_target_from_request(request: AssistantRequest) -> dict[str, Any]:
    receipt = {
        "channel": request.channel,
        "sender_id": request.sender_id,
        "message_id": request.message_id,
        "conversation_id": request.conversation_id,
        "config_key": request.config_key,
        "config_path": request.config_path,
    }
    return {key: value for key, value in receipt.items() if value}


def _receipt_target_from_operation(operation: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    result = operation.get("result")
    result_map = result if isinstance(result, dict) else {}
    target = result_map.get("receipt_target")
    if isinstance(target, dict) and target:
        return target
    payload_target = payload.get("receipt_target")
    return payload_target if isinstance(payload_target, dict) else {}


def _freeze_preview_target(payload: dict[str, Any], preview: dict[str, Any]) -> None:
    args = payload.setdefault("arguments", {})
    if not isinstance(args, dict) or str(args.get("target_version") or "").strip():
        return
    upgrade = preview.get("upgrade") if isinstance(preview, dict) else {}
    if not isinstance(upgrade, dict):
        return
    target = str(upgrade.get("target_version") or "").strip()
    if target:
        args["target_version"] = target
    release_tag = str(upgrade.get("release_tag") or "").strip()
    if release_tag:
        args["release_tag"] = release_tag
    target_source = str(upgrade.get("target_source") or "").strip()
    if target_source:
        args["target_source"] = target_source


def _preview_operation(payload: dict[str, Any]) -> dict[str, Any]:
    args = _upgrade_defaults(dict(payload.get("arguments") or {}))
    out = _preview_upgrade(args)
    return {"summary": _upgrade_summary(out), "upgrade": out}


def _preview_has_no_upgrade(preview: dict[str, Any]) -> bool:
    upgrade = preview.get("upgrade") if isinstance(preview, dict) else {}
    if not isinstance(upgrade, dict):
        return False
    status = str(upgrade.get("status") or "").strip().lower()
    if status == "no_upgrade_available":
        return True
    version_check = upgrade.get("version_check")
    if status == "already_current" and isinstance(version_check, dict):
        return not bool(version_check.get("update_available"))
    return False


def _apply_operation(payload: dict[str, Any]) -> dict[str, Any]:
    args = _upgrade_defaults(dict(payload.get("arguments") or {}))
    return service_upgrade(confirm=True, **args)


def run_confirmed_upgrade_operation(
    *,
    operation_id: str,
    audit_db: str | Path | None = None,
    send_receipt: bool = True,
    reply_fn: ReplyFn = reply_text_message,
) -> dict[str, Any]:
    store = InboundOperationStore(audit_db)
    operation = store.get(operation_id)
    if operation is None:
        raise AgentToolError(code="INPUT_ERROR", message="找不到待执行的升级操作。", details={"operation_id": operation_id})
    status = str(operation.get("status") or "").strip()
    if status in {"applied", "failed", "cancelled", "expired"}:
        existing = operation.get("result")
        result = existing if isinstance(existing, dict) else {}
        return build_response(
            tool_name="inbound.upgrade.worker",
            ok=status == "applied",
            data={
                "operation_id": operation_id,
                "status": status,
                "status_text": user_facing_operation_status(status),
                "result": result,
                "response_text": render_upgrade_response(
                    status=status,
                    operation_id=operation_id,
                    payload=dict(operation.get("payload") or {}),
                    preview=operation.get("preview") if isinstance(operation.get("preview"), dict) else None,
                    result=result,
                ),
            },
            meta={"audit_db": mask_path(store.path)},
        )
    if status != "confirmed":
        raise AgentToolError(
            code="INPUT_ERROR",
            message=cannot_repeat_message("升级操作", "执行", status),
            details={"operation_id": operation_id, "status": status},
        )

    payload = dict(operation.get("payload") or {})
    stored_hash = str(operation.get("payload_hash") or "")
    current_hash = hash_operation_payload(payload)
    if stored_hash != current_hash:
        failed = {"operation_id": operation_id, "status": "failed", "reason": "payload_hash_mismatch"}
        store.mark_failed(operation_id, result=failed)
        raise AgentToolError(code="INTERNAL_ERROR", message="pending upgrade operation payload hash mismatch; refusing to upgrade", details=failed)
    verify_operation_signature(operation)

    running = {
        "operation_id": operation_id,
        "status": "running",
        "task_status": "running",
        "worker_pid": os.getpid(),
    }
    if not store.mark_running(operation_id, result=running):
        current = store.get(operation_id) or {}
        current_status = str(current.get("status") or "-")
        raise AgentToolError(
            code="INPUT_ERROR",
            message=cannot_repeat_message("升级操作", "执行", current_status),
            details={"operation_id": operation_id, "status": current_status},
        )

    preview = operation.get("preview") if isinstance(operation.get("preview"), dict) else _preview_operation(payload)
    receipt_target = _receipt_target_from_operation(operation, payload)
    try:
        result = _apply_operation(payload)
    except AgentToolError as exc:
        failed = {"operation_id": operation_id, "status": "failed", "error": exc.code, "message": exc.message}
        failed["final_receipt"] = _send_final_receipt(operation_id=operation_id, receipt_target=receipt_target, payload=payload, preview=preview, result=failed, status="failed", enabled=send_receipt, reply_fn=reply_fn)
        store.mark_failed(operation_id, result=failed)
        raise
    except Exception as exc:
        failed = {"operation_id": operation_id, "status": "failed", "error": type(exc).__name__, "message": str(exc)}
        failed["final_receipt"] = _send_final_receipt(operation_id=operation_id, receipt_target=receipt_target, payload=payload, preview=preview, result=failed, status="failed", enabled=send_receipt, reply_fn=reply_fn)
        store.mark_failed(operation_id, result=failed)
        raise AgentToolError(code="INTERNAL_ERROR", message="upgrade operation failed in background worker", details=failed) from exc

    if not bool(result.get("ok", False)):
        failed = {"operation_id": operation_id, "status": "failed", "result": result}
        failed["final_receipt"] = _send_final_receipt(operation_id=operation_id, receipt_target=receipt_target, payload=payload, preview=preview, result=result, status="failed", enabled=send_receipt, reply_fn=reply_fn)
        store.mark_failed(operation_id, result=failed)
        return build_response(
            tool_name="inbound.upgrade.worker",
            ok=False,
            data={
                "operation_id": operation_id,
                "status": "failed",
                "result": result,
                "response_text": render_upgrade_response(status="failed", operation_id=operation_id, payload=payload, preview=preview, result=result),
            },
            error=build_error_payload(AgentToolError(code="UPGRADE_FAILED", message=f"立即升级未成功：{result.get('status') or 'unknown'}", details=result)),
            meta={"audit_db": mask_path(store.path)},
        )

    applied = dict(result)
    applied["operation_id"] = operation_id
    applied["status"] = "applied"
    applied["final_receipt"] = _send_final_receipt(operation_id=operation_id, receipt_target=receipt_target, payload=payload, preview=preview, result=applied, status="applied", enabled=send_receipt, reply_fn=reply_fn)
    store.mark_applied(operation_id, result=applied)
    return build_response(
        tool_name="inbound.upgrade.worker",
        ok=True,
        data={
            "operation_id": operation_id,
            "status": "applied",
            "result": applied,
            "response_text": render_upgrade_response(status="applied", operation_id=operation_id, payload=payload, preview=preview, result=applied),
        },
        meta={"audit_db": mask_path(store.path)},
    )


def _launch_upgrade_worker(*, operation_id: str, audit_db: Path) -> dict[str, Any]:
    launcher = UPGRADE_WORKER_LAUNCHER or _default_upgrade_worker_launcher
    result = launcher(operation_id, audit_db)
    return {
        "operation_id": operation_id,
        "status": "confirmed",
        "task_status": "queued",
        "worker": result,
    }


def _default_upgrade_worker_launcher(operation_id: str, audit_db: Path) -> dict[str, Any]:
    root = repo_base()
    om_path = root / "om"
    command = [str(om_path if om_path.exists() else sys.executable), "assistant", "upgrade-worker", "--operation-id", operation_id, "--audit-db", str(audit_db)]
    if not om_path.exists():
        command = [sys.executable, "-m", "src.interfaces.cli.main", "assistant", "upgrade-worker", "--operation-id", operation_id, "--audit-db", str(audit_db)]

    systemd_run = shutil.which("systemd-run")
    worker_env = _upgrade_worker_child_env(root=root)
    systemd_env_args = _systemd_setenv_args(worker_env)
    if systemd_run and sys.platform.startswith("linux"):
        unit = "options-monitor-assistant-upgrade-" + "".join(ch if ch.isalnum() else "-" for ch in operation_id.lower())[:48]
        attempts = [
            [systemd_run, "--user", "--unit", unit, "--collect", "--working-directory", str(root), *systemd_env_args, *command],
            ["sudo", "-n", systemd_run, "--unit", unit, "--collect", "--working-directory", str(root), *systemd_env_args, *command],
        ]
        errors: list[str] = []
        for attempt in attempts:
            if attempt[0] == "sudo" and shutil.which("sudo") is None:
                continue
            proc = subprocess.run(attempt, cwd=str(root), text=True, capture_output=True, timeout=20, check=False)
            if proc.returncode == 0:
                return {"launcher": "systemd-run", "unit": unit, "command": command, "env_keys": sorted(worker_env)}
            errors.append((proc.stderr or proc.stdout or f"exit {proc.returncode}").strip())
        raise AgentToolError(
            code="UPGRADE_WORKER_LAUNCH_FAILED",
            message="升级确认已收到，但无法启动独立升级任务。",
            hint="请检查 systemd-run 或 sudo -n systemd-run 权限。",
            details={"operation_id": operation_id, "launcher_errors": errors},
        )

    proc = subprocess.Popen(command, cwd=str(root), env={**build_effective_env(repo_root=root).values, **worker_env}, start_new_session=True)
    return {"launcher": "popen", "pid": proc.pid, "command": command, "env_keys": sorted(worker_env)}


def _upgrade_worker_child_env(*, root: Path) -> dict[str, str]:
    effective = build_effective_env(repo_root=root)
    values = effective.values
    profile: dict[str, Any] = {}
    runtime_root = str(values.get("OM_RUNTIME_ROOT") or "").strip()
    if runtime_root:
        profile = _read_upgrade_worker_service_profile(Path(runtime_root).expanduser())
    else:
        profile = _read_upgrade_worker_service_profile(_DEFAULT_RUNTIME_ROOT)
        runtime_root = str(profile.get("runtime_root") or "").strip() if profile else ""

    out: dict[str, str] = {"PYTHONUNBUFFERED": "1"}
    if runtime_root:
        out["OM_RUNTIME_ROOT"] = str(Path(runtime_root).expanduser())

    env_file = str(values.get("OM_ENV_FILE") or "").strip()
    if not env_file and effective.env_file is not None:
        env_file = str(effective.env_file)
    if not env_file and profile:
        env_file = str(profile.get("env_file") or "").strip()
    if env_file:
        out["OM_ENV_FILE"] = str(Path(env_file).expanduser())

    current_pythonpath = str(values.get("PYTHONPATH") or "").strip()
    out["PYTHONPATH"] = f"{root}{os.pathsep}{current_pythonpath}" if current_pythonpath else str(root)
    return out


def _read_upgrade_worker_service_profile(runtime_root: Path) -> dict[str, Any]:
    profile_path = runtime_root / "service.profile.json"
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return profile if isinstance(profile, dict) else {}


def _systemd_setenv_args(env: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key in sorted(env):
        value = str(env.get(key) or "")
        if value:
            args.extend(["--setenv", f"{key}={value}"])
    return args


def _send_final_receipt(
    *,
    operation_id: str,
    receipt_target: dict[str, Any],
    payload: dict[str, Any],
    preview: dict[str, Any],
    result: dict[str, Any],
    status: str,
    enabled: bool,
    reply_fn: ReplyFn,
) -> dict[str, Any]:
    message_id = str(receipt_target.get("message_id") or "").strip()
    if not enabled:
        return {"attempted": False, "ok": True, "reason": "disabled"}
    if not message_id:
        return {"attempted": False, "ok": True, "reason": "missing_message_id"}
    if str(receipt_target.get("channel") or "").strip().lower() != "feishu":
        return {"attempted": False, "ok": True, "reason": "not_feishu"}
    env = build_effective_env().values
    bot = resolve_feishu_bot_config(environ=env)
    if not (bot.app_id and bot.app_secret):
        return {"attempted": True, "ok": False, "reason": "missing_app_credentials"}
    text = render_upgrade_response(status=status, operation_id=operation_id, payload=payload, preview=preview, result=result)
    try:
        api_response = reply_fn(
            app_id=bot.app_id,
            app_secret=bot.app_secret,
            message_id=message_id,
            text=text,
            uuid=f"{operation_id}:upgrade-final",
        )
    except Exception as exc:
        return {"attempted": True, "ok": False, "reason": "reply_failed", "message_id": message_id, "error": f"{type(exc).__name__}: {exc}"}
    return {"attempted": True, "ok": True, "reason": "sent", "message_id": message_id, "api_response": api_response}


def _upgrade_defaults(arguments: dict[str, Any]) -> dict[str, Any]:
    env = build_effective_env().values
    runtime_root = str(arguments.get("runtime_root") or env.get("OM_RUNTIME_ROOT") or "/var/lib/options-monitor").strip()
    return {
        "repo_root": Path(str(arguments.get("repo_root") or repo_base())).expanduser(),
        "runtime_root": Path(runtime_root).expanduser(),
        "releases_root": _optional_path(arguments.get("releases_root")),
        "cache_root": _optional_path(arguments.get("cache_root")),
        "target_version": _optional_text(arguments.get("target_version")),
        "remote_name": str(arguments.get("remote_name") or "origin"),
        "auto": True,
        "allow_major": bool(arguments.get("allow_major", False)),
        "restart_services": not bool(arguments.get("no_restart_services", False)),
        "cleanup_after_upgrade": bool(arguments.get("cleanup_after_upgrade", False)),
        "cleanup_keep_releases": int(arguments.get("cleanup_keep_releases") or 2),
    }


def _optional_path(value: Any) -> Path | None:
    text = _optional_text(value)
    return Path(text).expanduser() if text else None


def _upgrade_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "current_version": result.get("current_version"),
        "target_version": result.get("target_version") or result.get("latest_version"),
        "release_tag": result.get("release_tag"),
        "changed": bool(result.get("changed", False)),
        "planned_operations_count": len(result.get("planned_operations") or []),
        "repo_root": result.get("repo_root"),
        "runtime_root": result.get("runtime_root"),
    }


def _preview_upgrade(args: dict[str, Any]) -> dict[str, Any]:
    check = service_upgrade_check(
        repo_root=args["repo_root"],
        runtime_root=args["runtime_root"],
        cache_root=args.get("cache_root"),
        remote_name=str(args.get("remote_name") or "origin"),
    )
    current = str(check.get("current_version") or "")
    target = str(args.get("target_version") or check.get("latest_version") or "").strip()
    repo_root = Path(str(check.get("repo_root") or args["repo_root"])).expanduser()
    runtime_root = Path(str(check.get("runtime_root") or args["runtime_root"])).expanduser()
    releases_root = Path(args["releases_root"]).expanduser() if args.get("releases_root") else default_releases_root(repo_root)
    cache_root = Path(args["cache_root"]).expanduser() if args.get("cache_root") else default_upgrade_cache_root(repo_root)
    base = {
        "schema_version": 1,
        "operation": "upgrade",
        "ok": bool(check.get("ok")),
        "repo_root": str(repo_root),
        "repo_root_resolved": check.get("repo_root_resolved"),
        "repo_root_resolution": check.get("repo_root_resolution"),
        "runtime_root": str(runtime_root),
        "releases_root": str(releases_root),
        "upgrade_cache_root": str(cache_root),
        "current_version": current or None,
        "target_version": target or None,
        "release_tag": f"v{target}" if target else None,
        "auto": True,
        "confirmed": False,
        "allow_major": bool(args.get("allow_major", False)),
        "cleanup_after_upgrade": bool(args.get("cleanup_after_upgrade", False)),
        "cleanup_keep_releases": int(args.get("cleanup_keep_releases") or 2),
        "version_check": check,
        "changed": False,
        "operations": [],
    }
    if not bool(check.get("ok")):
        return {**base, "status": "upgrade_check_failed"}
    if not target:
        return {**base, "status": "no_target_version", "ok": False, "message": check.get("message") or "没有可升级版本。"}
    cmp = compare_versions(current, target)
    if cmp == 0:
        no_upgrade = not str(args.get("target_version") or "").strip() and not bool(check.get("upgrade_available"))
        return {
            **base,
            "status": "no_upgrade_available" if no_upgrade else "already_current",
            "ok": True,
            "message": check.get("message") or f"没有可升级版本。当前已是最新版本 {current}",
        }
    if cmp > 0:
        return {
            **base,
            "status": "target_older_than_current",
            "ok": False,
            "message": check.get("message") or f"当前版本 {current} 高于目标版本 {target}",
        }
    target_dir = releases_root / target
    planned = [
        f"materialize v{target} into {target_dir} from git cache {cache_root / 'git' / 'options-monitor.git'}"
        if not target_dir.exists()
        else f"reuse existing release dir {target_dir}",
        f"prepare release runtime at {target_dir / '.venv'}",
        f"validate {target_dir}",
        f"switch {repo_root} -> {target_dir}",
        "reconcile service drift from current release",
        "restart long-running services" if bool(args.get("restart_services", True)) else "skip service restart",
    ]
    if bool(args.get("cleanup_after_upgrade", False)):
        planned.append(f"cleanup old releases after successful upgrade, keep {int(args.get('cleanup_keep_releases') or 2)} releases")
    return {
        **base,
        "status": "dry_run",
        "ok": True,
        "target_dir": str(target_dir),
        "previous_dir": str(check.get("repo_root_resolved") or repo_root),
        "warnings": [] if repo_root.is_symlink() else ["confirmed upgrade requires repo_root to be a current symlink"],
        "planned_operations": planned,
    }


def render_upgrade_response(
    *,
    status: str,
    operation_id: str,
    payload: dict[str, Any],
    preview: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    expires_at: str = "",
) -> str:
    del payload
    data = _upgrade_response_data(preview=preview, result=result)
    summary = _upgrade_summary(data)
    current = summary.get("current_version") or "-"
    target = summary.get("target_version") or "-"
    upgrade_status = summary.get("status") or "-"
    if status == "no_upgrade_available" or upgrade_status == "no_upgrade_available":
        return "\n".join(
            [
                "没有可升级版本。",
                f"当前版本：{current}",
                f"远端最新版本：{target}",
                "未执行升级。",
            ]
        )
    if status == "previewed":
        lines = [
            "升级预览：立即升级",
            f"当前版本：{current}",
            f"目标版本：{target}",
            f"状态：{upgrade_status}",
        ]
        planned = data.get("planned_operations")
        if isinstance(planned, list) and planned:
            lines.append("将执行：")
            lines.extend(f"- {item}" for item in planned[:6])
            if len(planned) > 6:
                lines.append(f"... 还有 {len(planned) - 6} 项")
        if data.get("warnings"):
            lines.append("警告：" + "；".join(str(item) for item in data.get("warnings")[:3]))
        lines.extend(
            [
                "",
                "未执行升级。",
                f"确认执行请回复：确认升级 {operation_id}",
                f"取消请回复：取消升级 {operation_id}",
            ]
        )
        if expires_at:
            lines.append("有效期：10 分钟。")
        return "\n".join(lines)
    if status == "applied":
        changed = "已切换" if bool(data.get("changed")) else "未切换"
        active_version = target if bool(data.get("changed")) and target != "-" else current
        return "\n".join(
            [
                "升级执行完成。",
                f"升级前版本：{current}",
                f"当前版本：{active_version}",
                f"目标版本：{target}",
                f"状态：{user_facing_operation_status(status)}",
                f"结果：{changed}",
                f"command_id: {operation_id}",
            ]
        )
    if status in {"confirmed", "running"}:
        return "\n".join(
            [
                "已收到升级确认，开始执行升级。",
                f"当前版本：{current}",
                f"目标版本：{target}",
                "升级期间飞书服务可能短暂重启，完成后会发送最终结果。",
                f"command_id: {operation_id}",
            ]
        )
    if status == "failed":
        return "\n".join(
            [
                "升级执行失败。",
                f"当前版本：{current}",
                f"目标版本：{target}",
                f"状态：{upgrade_status}",
                f"command_id: {operation_id}",
            ]
        )
    return f"升级操作进度：{user_facing_operation_status(status)}\ncommand_id: {operation_id}"


def _upgrade_response_data(*, preview: dict[str, Any] | None, result: dict[str, Any] | None) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if isinstance(preview, dict):
        upgrade = preview.get("upgrade")
        if isinstance(upgrade, dict):
            data.update(upgrade)
        preview_summary = preview.get("summary")
        if isinstance(preview_summary, dict):
            for key, value in preview_summary.items():
                data.setdefault(key, value)
    if isinstance(result, dict):
        for key, value in result.items():
            if _upgrade_response_has_value(value) or key not in data:
                data[key] = value
    return data


def _upgrade_response_has_value(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
