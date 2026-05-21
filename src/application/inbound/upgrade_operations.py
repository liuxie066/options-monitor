from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response, mask_path
from src.application.inbound.contracts import InboundIntent, InboundRequest
from src.application.inbound.operation_policy import enforce_upgrade_write_allowed
from src.application.inbound.operation_store import InboundOperationStore, operation_is_expired
from src.application.service_upgrade import compare_versions, default_releases_root, default_upgrade_cache_root, service_upgrade, service_upgrade_check
from src.application.settings import build_effective_env


PREVIEW_INTENTS = frozenset({"upgrade_now"})
CONFIRM_INTENTS = frozenset({"upgrade_confirm", "upgrade_cancel"})
UPGRADE_OPERATION_TYPES = PREVIEW_INTENTS


def is_upgrade_operation_intent(intent: InboundIntent) -> bool:
    return intent.name in PREVIEW_INTENTS or intent.name in CONFIRM_INTENTS


def handle_upgrade_operation(
    intent: InboundIntent,
    request: InboundRequest,
    *,
    command_id: str,
    store: InboundOperationStore,
) -> dict[str, Any]:
    policy = enforce_upgrade_write_allowed(channel=request.channel, sender_id=request.sender_id)
    if intent.name in PREVIEW_INTENTS:
        payload = _build_operation_payload(intent.name, dict(intent.arguments))
        return _preview_and_save(payload, request=request, command_id=command_id, store=store, ttl_seconds=policy.confirm_ttl_seconds)
    if intent.name == "upgrade_confirm":
        return _confirm_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    if intent.name == "upgrade_cancel":
        return _cancel_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported upgrade operation intent: {intent.name}")


def _preview_and_save(
    payload: dict[str, Any],
    *,
    request: InboundRequest,
    command_id: str,
    store: InboundOperationStore,
    ttl_seconds: int,
) -> dict[str, Any]:
    preview = _preview_operation(payload)
    payload_hash = hash_operation_payload(payload)
    operation = store.save_preview(
        operation_id=command_id,
        command_id=command_id,
        channel=request.channel,
        sender_id=request.sender_id,
        conversation_id=request.conversation_id,
        operation_type=str(payload["operation_type"]),
        payload_hash=payload_hash,
        payload=payload,
        preview=preview,
        ttl_seconds=ttl_seconds,
    )
    text = render_upgrade_response(
        status="previewed",
        operation_id=command_id,
        payload=payload,
        preview=preview,
        expires_at=str(operation.get("expires_at") or ""),
    )
    return build_response(
        tool_name="inbound.upgrade",
        ok=True,
        data={
            "operation_id": command_id,
            "operation_type": payload["operation_type"],
            "status": "previewed",
            "payload_hash": payload_hash,
            "payload": payload,
            "preview": preview,
            "expires_at": operation.get("expires_at"),
            "response_text": text,
        },
        meta={"audit_db": mask_path(store.path)},
    )


def _confirm_operation(*, operation_id: str | None, request: InboundRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_upgrade_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=False,
        action="确认",
    )
    if operation_is_expired(operation):
        result = {"operation_id": operation_id, "status": "expired"}
        store.mark_expired(operation_id, result=result)
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="这条升级确认已过期，未执行升级。", hint="请重新发送：立即升级", details={**result, **operation_resolution})
    payload = dict(operation["payload"])
    stored_hash = str(operation.get("payload_hash") or "")
    current_hash = hash_operation_payload(payload)
    if stored_hash != current_hash:
        result = {"operation_id": operation_id, "status": "failed", "reason": "payload_hash_mismatch"}
        store.mark_failed(operation_id, result=result)
        raise AgentToolError(code="INTERNAL_ERROR", message="pending upgrade operation payload hash mismatch; refusing to upgrade", details=result)
    if not store.mark_confirmed(operation_id):
        current = store.get(operation_id) or {}
        current_status = str(current.get("status") or "-")
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"这条升级操作不能再次确认，当前状态：{current_status}。",
            details={
                "operation_id": operation_id,
                "status": current_status,
                "reason": "operation_not_previewed",
                **operation_resolution,
            },
        )
    try:
        preview = _preview_operation(payload)
        result = _apply_operation(payload)
    except AgentToolError as exc:
        store.mark_failed(operation_id, result={"operation_id": operation_id, "status": "failed", "error": exc.code, "message": exc.message})
        raise
    except Exception as exc:
        failed = {"operation_id": operation_id, "status": "failed", "error": type(exc).__name__, "message": str(exc)}
        store.mark_failed(operation_id, result=failed)
        raise AgentToolError(code="INTERNAL_ERROR", message="upgrade operation failed after confirmation", details=failed) from exc
    if not bool(result.get("ok", False)):
        failed = {"operation_id": operation_id, "status": "failed", "result": result}
        store.mark_failed(operation_id, result=failed)
        error = AgentToolError(
            code="UPGRADE_FAILED",
            message=f"立即升级未成功：{result.get('status') or 'unknown'}",
            details=result,
        )
        return build_response(
            tool_name="inbound.upgrade",
            ok=False,
            data={
                "operation_id": operation_id,
                **operation_resolution,
                "operation_type": payload["operation_type"],
                "status": "failed",
                "payload_hash": current_hash,
                "payload": payload,
                "preview": preview,
                "result": result,
                "response_text": render_upgrade_response(status="failed", operation_id=operation_id, payload=payload, preview=preview, result=result),
            },
            error=build_error_payload(error),
            meta={"audit_db": mask_path(store.path)},
        )
    store.mark_applied(operation_id, result=result)
    text = render_upgrade_response(status="applied", operation_id=operation_id, payload=payload, preview=preview, result=result)
    return build_response(
        tool_name="inbound.upgrade",
        ok=True,
        data={
            "operation_id": operation_id,
            **operation_resolution,
            "operation_type": payload["operation_type"],
            "status": "applied",
            "payload_hash": current_hash,
            "payload": payload,
            "preview": preview,
            "result": result,
            "response_text": text,
        },
        meta={"audit_db": mask_path(store.path)},
    )


def _cancel_operation(*, operation_id: str | None, request: InboundRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_upgrade_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=True,
        action="取消",
    )
    result = {"operation_id": operation_id, "status": "cancelled"}
    store.mark_cancelled(operation_id, result=result)
    text = f"升级操作已取消，未执行升级。\ncommand_id: {operation_id}"
    return build_response(
        tool_name="inbound.upgrade",
        ok=True,
        data={
            "operation_id": operation_id,
            **operation_resolution,
            "operation_type": operation.get("operation_type"),
            "status": "cancelled",
            "result": result,
            "response_text": text,
        },
        meta={"audit_db": mask_path(store.path)},
    )


def _resolve_upgrade_operation(
    *,
    operation_id: str | None,
    request: InboundRequest,
    store: InboundOperationStore,
    allow_expired: bool,
    action: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    resolution = store.resolve_pending_operation(
        channel=request.channel,
        sender_id=request.sender_id,
        operation_types=UPGRADE_OPERATION_TYPES,
        conversation_id=request.conversation_id,
        explicit_operation_id=operation_id,
        allow_expired=allow_expired,
    )
    details = _operation_resolution_details(resolution)
    status = str(resolution.get("status") or "")
    resolved_operation_id = str(resolution.get("operation_id") or operation_id or "").strip()
    operation_raw = resolution.get("operation")
    operation = operation_raw if isinstance(operation_raw, dict) else {}
    if status == "resolved" and resolved_operation_id and operation:
        return resolved_operation_id, operation, details
    if status == "expired":
        result = {"operation_id": resolved_operation_id, "status": "expired"}
        if resolved_operation_id:
            store.mark_expired(resolved_operation_id, result=result)
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="这条升级确认已过期，未执行升级。", hint="请重新发送：立即升级", details={**result, **details})
    if status == "ambiguous":
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=f"有多条待{action}的升级操作，请带 operation_id。",
            hint=_candidate_hint("确认升级" if action == "确认" else "取消升级", details.get("candidate_operations")),
            details=details,
        )
    if status == "none":
        raise AgentToolError(code="NEEDS_CLARIFICATION", message=f"没有可{action}的升级操作。", hint="请先发送：立即升级", details=details)
    if status == "forbidden":
        raise AgentToolError(code="PERMISSION_DENIED", message=f"只能由创建该预览的同一 sender/对话 {action}。", details=details)
    if status == "wrong_family":
        raise AgentToolError(code="INPUT_ERROR", message="这不是升级操作，不能用确认升级/取消升级处理。", details=details)
    if status == "invalid_status":
        current_status = str(operation.get("status") or "-")
        raise AgentToolError(code="INPUT_ERROR", message=f"这条升级操作不能再次{action}，当前状态：{current_status}。", details=details)
    raise AgentToolError(code="INPUT_ERROR", message="找不到待确认的升级操作。", hint="请检查 operation_id，或重新发送：立即升级", details=details)


def _operation_resolution_details(resolution: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_resolution": resolution.get("operation_resolution"),
        "resolved_operation_id": resolution.get("operation_id"),
        "candidate_operations": resolution.get("candidate_operations") or [],
    }


def _candidate_hint(prefix: str, candidates: Any) -> str:
    rows = candidates if isinstance(candidates, list) else []
    lines: list[str] = []
    for idx, item_raw in enumerate(rows[:5], start=1):
        if not isinstance(item_raw, dict):
            continue
        operation_id = str(item_raw.get("operation_id") or "").strip()
        if not operation_id:
            continue
        summary = str(item_raw.get("summary") or item_raw.get("operation_type") or "-").strip()
        lines.append(f"{idx}. {operation_id} | {summary} | 回复：{prefix} {operation_id}")
    if not lines:
        return f"请回复：{prefix} <operation_id>"
    return "\n候选升级：\n" + "\n".join(lines)


def _build_operation_payload(operation_type: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": "1.0", "operation_type": operation_type, "arguments": arguments}


def _preview_operation(payload: dict[str, Any]) -> dict[str, Any]:
    args = _upgrade_defaults(dict(payload.get("arguments") or {}))
    out = _preview_upgrade(args)
    return {"summary": _upgrade_summary(out), "upgrade": out}


def _apply_operation(payload: dict[str, Any]) -> dict[str, Any]:
    args = _upgrade_defaults(dict(payload.get("arguments") or {}))
    return service_upgrade(confirm=True, **args)


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
        return {**base, "status": "no_target_version", "ok": False}
    cmp = compare_versions(current, target)
    if cmp == 0:
        return {**base, "status": "already_current", "ok": True}
    if cmp > 0:
        return {**base, "status": "target_older_than_current", "ok": False}
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
    source = result if result is not None else ((preview or {}).get("upgrade") if isinstance(preview, dict) else {})
    data = source if isinstance(source, dict) else {}
    summary = _upgrade_summary(data)
    current = summary.get("current_version") or "-"
    target = summary.get("target_version") or "-"
    upgrade_status = summary.get("status") or "-"
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
        return "\n".join(
            [
                "升级执行完成。",
                f"当前版本：{current}",
                f"目标版本：{target}",
                f"状态：{upgrade_status}",
                f"结果：{changed}",
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
    return f"升级操作状态：{status}\ncommand_id: {operation_id}"


def hash_operation_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
