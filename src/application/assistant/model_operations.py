from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.agent_tool_config import repo_base, resolve_runtime_config_path
from src.application.agent_tool_contracts import AgentToolError, build_response, mask_path
from src.application.assistant.contracts import AssistantRequest, ControlCommand
from src.application.assistant.llm_model_profiles import (
    configured_model_profiles_payload,
    current_model_payload,
    switch_active_model_profile,
)
from src.application.assistant.operation_lifecycle import (
    build_cancelled_operation_response,
    build_previewed_operation_response,
    confirm_previewed_operation_or_raise,
    resolve_pending_operation_or_raise,
)
from src.application.assistant.operation_policy import enforce_model_write_allowed
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.operation_status_text import operation_candidate_hint
from src.application.config_authoring_transaction import config_source_sha256, publish_yaml_config_generation
from src.application.config_yaml import default_yaml_assistant_config_path, default_yaml_config_path, load_yaml_config_file
from src.application.runtime_config_freshness import GENERATED_KEY
from src.application.config_yaml import RESOLVED_KEY


LIST_INTENTS = frozenset({"model_list"})
PREVIEW_INTENTS = frozenset({"model_use"})
CONFIRM_INTENTS = frozenset({"model_confirm", "model_cancel"})
MODEL_OPERATION_TYPES = PREVIEW_INTENTS


def handle_model_operation(
    intent: ControlCommand,
    request: AssistantRequest,
    *,
    command_id: str,
    store: InboundOperationStore,
) -> dict[str, Any]:
    if intent.intent_name in LIST_INTENTS:
        return _list_models(request)
    policy = enforce_model_write_allowed(channel=request.channel, sender_id=request.sender_id)
    if intent.intent_name in PREVIEW_INTENTS:
        payload = _build_model_payload(intent.intent_name, dict(intent.arguments), request=request)
        return _preview_and_save(
            payload,
            request=request,
            command_id=command_id,
            store=store,
            ttl_seconds=policy.confirm_ttl_seconds,
        )
    if intent.intent_name == "model_confirm":
        return _confirm_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    if intent.intent_name == "model_cancel":
        return _cancel_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported assistant model operation intent: {intent.intent_name}")


def _list_models(request: AssistantRequest) -> dict[str, Any]:
    config_yaml_path = _resolve_config_yaml_path(request)
    config_doc = load_yaml_config_file(config_yaml_path)
    assistant_config_path = _resolve_assistant_config_path(request, config_yaml_path=config_yaml_path)
    runtime_assistant_cfg = _load_json_if_exists(assistant_config_path)
    payload = configured_model_profiles_payload(
        config_doc=config_doc,
        repo_root=repo_base(),
    )
    payload["config_yaml_path"] = str(config_yaml_path)
    payload["assistant_config_path"] = str(assistant_config_path)
    payload["current"] = current_model_payload(
        config_doc=config_doc,
        runtime_assistant_config=runtime_assistant_cfg,
    )
    text = render_model_response(status="listed", operation_id="", payload={}, preview=payload)
    return build_response(
        tool_name="inbound.model",
        ok=True,
        data={**payload, "status": "listed", "response_text": text},
    )


def _preview_and_save(
    payload: dict[str, Any],
    *,
    request: AssistantRequest,
    command_id: str,
    store: InboundOperationStore,
    ttl_seconds: int,
) -> dict[str, Any]:
    preview = _preview_operation(payload)
    return build_previewed_operation_response(
        tool_name="inbound.model",
        operation_id=command_id,
        request=request,
        store=store,
        payload=payload,
        preview=preview,
        ttl_seconds=ttl_seconds,
        response_text=lambda operation: render_model_response(
            status="previewed",
            operation_id=command_id,
            payload=payload,
            preview=preview,
            expires_at=str(operation.get("expires_at") or ""),
        ),
    )


def _confirm_operation(*, operation_id: str | None, request: AssistantRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_model_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=False,
        action="确认",
    )
    confirmed = confirm_previewed_operation_or_raise(
        operation_id=operation_id,
        operation=operation,
        operation_resolution=operation_resolution,
        store=store,
        subject="模型切换",
        expired_message="这条模型切换确认已过期，未写入配置。",
        expired_hint="请重新发送 /model use <name> 生成新的预览。",
        hash_mismatch_message="pending model operation payload hash mismatch; refusing to write config",
    )
    operation_id = confirmed.operation_id
    operation_resolution = confirmed.operation_resolution
    payload = confirmed.payload
    try:
        preview = _preview_operation(payload)
        result = _apply_operation(payload)
    except AgentToolError as exc:
        store.mark_failed(operation_id, result={"operation_id": operation_id, "status": "failed", "error": exc.code, "message": exc.message})
        raise
    except Exception as exc:
        failed = {"operation_id": operation_id, "status": "failed", "error": type(exc).__name__, "message": str(exc)}
        store.mark_failed(operation_id, result=failed)
        raise AgentToolError(code="INTERNAL_ERROR", message="model switch failed before config write could be confirmed", details=failed) from exc
    store.mark_applied(operation_id, result=result)
    text = render_model_response(status="applied", operation_id=operation_id, payload=payload, preview=preview, result=result)
    return build_response(
        tool_name="inbound.model",
        ok=True,
        data={
            "operation_id": operation_id,
            **operation_resolution,
            "operation_type": payload["operation_type"],
            "status": "applied",
            "payload_hash": confirmed.payload_hash,
            "payload": payload,
            "preview": preview,
            "result": result,
            "response_text": text,
        },
        meta={"audit_db": mask_path(store.path)},
    )


def _cancel_operation(*, operation_id: str | None, request: AssistantRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_model_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=True,
        action="取消",
    )
    text = f"模型切换已取消，未写入配置。\ncommand_id: {operation_id}"
    return build_cancelled_operation_response(
        tool_name="inbound.model",
        operation_id=operation_id,
        operation=operation,
        operation_resolution=operation_resolution,
        store=store,
        response_text=text,
    )


def _resolve_model_operation(
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
        operation_types=MODEL_OPERATION_TYPES,
        allow_expired=allow_expired,
        action=action,
        subject="模型切换",
        expired_message="这条模型切换确认已过期，未写入配置。",
        expired_hint="请重新发送 /model use <name> 生成新的预览。",
        none_hint="请先发送 /model use <name> 生成预览。",
        wrong_family_message="这不是模型切换，不能用确认模型/取消模型处理。",
        not_found_message="找不到待确认的模型切换。",
        not_found_hint="请检查 operation_id，或重新发送 /model use <name>。",
        candidate_hint=_model_candidate_hint,
    )


def _model_candidate_hint(action: str, candidates: Any) -> str:
    return operation_candidate_hint("/confirm model" if action == "确认" else "/cancel model", candidates, heading="候选切换")


def _build_model_payload(operation_type: str, arguments: dict[str, Any], *, request: AssistantRequest) -> dict[str, Any]:
    config_yaml_path = _resolve_config_yaml_path(request)
    assistant_config_path = _resolve_assistant_config_path(request, config_yaml_path=config_yaml_path)
    runtime_root = assistant_config_path.parent.parent if assistant_config_path.parent.name == "resolved" else config_yaml_path.parent
    return {
        "schema_version": "1.0",
        "operation_type": operation_type,
        "arguments": {
            "model_profile": _required_text(arguments.get("model_profile"), "model_profile"),
        },
        "config": {
            "config_yaml_path": str(config_yaml_path),
            "assistant_config_path": str(assistant_config_path),
            "runtime_root": str(runtime_root),
            "source_sha256": config_source_sha256(config_yaml_path),
        },
    }


def _preview_operation(payload: dict[str, Any]) -> dict[str, Any]:
    config_yaml_path, assistant_config_path, config_doc = _load_config_for_payload(payload)
    target = _target_profile(payload)
    before = current_model_payload(
        config_doc=config_doc,
        runtime_assistant_config=_load_json_if_exists(assistant_config_path),
    )
    after_doc, profile = switch_active_model_profile(config_doc, name=target)
    after = current_model_payload(config_doc=after_doc)
    return {
        "config_yaml_path": str(config_yaml_path),
        "assistant_config_path": str(assistant_config_path),
        "summary": {
            "action": "use",
            "from": _active_model_name(before),
            "to": profile.name,
            "changed": before.get("summary", {}).get("active_model") != profile.name if isinstance(before.get("summary"), dict) else True,
        },
        "target_profile": profile.public_payload(active=True),
        "before": before,
        "after": after,
        "source_revision": {
            "before_sha256": config_source_sha256(config_yaml_path),
        },
    }


def _apply_operation(payload: dict[str, Any]) -> dict[str, Any]:
    config_yaml_path, assistant_config_path, config_doc = _load_config_for_payload(payload)
    target = _target_profile(payload)
    after_doc, profile = switch_active_model_profile(config_doc, name=target)
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    runtime_root = _resolve_path(
        config.get("runtime_root"),
        default=assistant_config_path.parent.parent if assistant_config_path.parent.name == "resolved" else config_yaml_path.parent,
    )
    transaction = publish_yaml_config_generation(
        repo_root=repo_base(),
        config_yaml_path=config_yaml_path,
        config_doc=after_doc,
        runtime_root=runtime_root,
        markets=_markets_in_doc(after_doc),
        include_assistant=True,
        apply=True,
        backup=True,
        expected_source_sha256=_required_text(config.get("source_sha256"), "source_sha256"),
    )
    return {
        "status": "applied",
        "active_model": profile.name,
        "profile": profile.public_payload(active=True),
        "config_write": {
            "ok": True,
            "action": "use",
            "config_yaml_path": str(config_yaml_path),
            "changed": config_doc != after_doc,
            "write_applied": transaction.get("write_applied"),
            "backup_path": transaction.get("backup_path"),
            "rollback_hint": (
                f"restore {transaction.get('backup_path')} to {config_yaml_path}"
                if transaction.get("backup_path")
                else f"edit or restore {config_yaml_path}"
            ),
            "audit_id": transaction.get("audit_id"),
            "source_revision": transaction.get("source_revision"),
        },
        "assistant_rebuild": transaction.get("assistant"),
        "runtime_rebuild": transaction.get("markets"),
    }


def render_model_response(
    *,
    status: str,
    operation_id: str,
    payload: dict[str, Any],
    preview: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    expires_at: str | None = None,
) -> str:
    if status == "listed":
        rows = preview.get("models") if isinstance(preview, dict) else []
        summary = preview.get("summary") if isinstance(preview, dict) and isinstance(preview.get("summary"), dict) else {}
        current = preview.get("current") if isinstance(preview, dict) and isinstance(preview.get("current"), dict) else {}
        current_summary = current.get("summary") if isinstance(current.get("summary"), dict) else {}
        active = summary.get("active_model") or current_summary.get("active_model") or "-"
        lines = [f"当前模型：{active}", f"可用模型：{len(rows) if isinstance(rows, list) else 0} 个"]
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                mark = "*" if row.get("active") else " "
                configured = "yes" if row.get("api_key_configured") else "no"
                lines.append(
                    f"{mark} {row.get('name') or '-'} | {row.get('provider') or '-'}/{row.get('model') or '-'} "
                    f"| credential_configured={configured}"
                )
        hint = current.get("hint") if isinstance(current, dict) else None
        if hint:
            lines.append(str(hint))
        return "\n".join(lines)
    if status == "cancelled":
        return f"模型切换已取消，未写入配置。\ncommand_id: {operation_id}"

    summary = preview.get("summary") if isinstance(preview, dict) and isinstance(preview.get("summary"), dict) else {}
    target = preview.get("target_profile") if isinstance(preview, dict) and isinstance(preview.get("target_profile"), dict) else {}
    title = "模型切换预览" if status == "previewed" else "模型切换已写入配置"
    lines = [
        f"{title}：{summary.get('from') or '-'} -> {summary.get('to') or _target_profile(payload)}",
        f"目标：{target.get('provider') or '-'} / {target.get('model') or '-'}",
    ]
    if isinstance(preview, dict):
        lines.append(f"配置：{preview.get('config_yaml_path') or '-'}")
        lines.append(f"生成：{preview.get('assistant_config_path') or '-'}")
    if status == "previewed":
        lines.extend(
            [
                "",
                "未写入配置。",
                f"确认切换请回复：/confirm model {operation_id}",
                f"取消请回复：/cancel model {operation_id}",
                f"operation_id：{operation_id}",
                "同一对话只有一条待确认模型切换时，也可以回复：确认模型 / 取消模型",
            ]
        )
        if expires_at:
            lines.append("有效期：10 分钟。")
    else:
        config_write = result.get("config_write") if isinstance(result, dict) and isinstance(result.get("config_write"), dict) else {}
        if config_write.get("backup_path"):
            lines.append(f"备份：{config_write.get('backup_path')}")
        lines.append(f"command_id：{operation_id}")
    return "\n".join(str(line) for line in lines)


def _load_config_for_payload(payload: dict[str, Any]) -> tuple[Path, Path, dict[str, Any]]:
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    config_yaml_path = _resolve_path(config.get("config_yaml_path"), default=default_yaml_config_path(repo_root=repo_base()))
    assistant_config_path = _resolve_path(
        config.get("assistant_config_path"),
        default=default_yaml_assistant_config_path(repo_root=repo_base()),
    )
    return config_yaml_path, assistant_config_path, load_yaml_config_file(config_yaml_path)


def _resolve_config_yaml_path(request: AssistantRequest) -> Path:
    repo_root = repo_base()
    for raw_path in (request.assistant_config_path, request.config_path):
        path_text = str(raw_path or "").strip()
        if not path_text:
            continue
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        config_yaml = _config_yaml_path_from_json(path, repo_root=repo_root)
        if config_yaml is not None:
            return config_yaml
    if request.config_key:
        try:
            runtime_path = resolve_runtime_config_path(config_key=request.config_key, config_path=None)
        except AgentToolError:
            runtime_path = None
        if runtime_path is not None:
            config_yaml = _config_yaml_path_from_json(runtime_path, repo_root=repo_root)
            if config_yaml is not None:
                return config_yaml
    return default_yaml_config_path(repo_root=repo_root)


def _resolve_assistant_config_path(request: AssistantRequest, *, config_yaml_path: Path) -> Path:
    if request.assistant_config_path:
        return _resolve_path(request.assistant_config_path, default=default_yaml_assistant_config_path(repo_root=repo_base()))
    if request.config_path:
        runtime_path = _resolve_path(request.config_path, default=Path())
        return (runtime_path.parent / "resolved" / "config.assistant.json").resolve()
    del config_yaml_path
    return default_yaml_assistant_config_path(repo_root=repo_base())


def _config_yaml_path_from_json(path: Path, *, repo_root: Path) -> Path | None:
    payload = _load_json_if_exists(path)
    if not payload:
        return None
    for candidate in (
        _nested_text(payload, RESOLVED_KEY, "config_yaml_path"),
        _nested_text(payload, GENERATED_KEY, "config_yaml_path"),
        _generated_source_path(payload),
    ):
        if candidate:
            return _resolve_path(candidate, default=default_yaml_config_path(repo_root=repo_root), repo_root=repo_root)
    return None


def _generated_source_path(payload: dict[str, Any]) -> str | None:
    generated = payload.get(GENERATED_KEY)
    generated_map = generated if isinstance(generated, dict) else {}
    sources = generated_map.get("sources")
    if not isinstance(sources, list):
        return None
    for item in sources:
        source = item if isinstance(item, dict) else {}
        if str(source.get("role") or "").strip() == "config_yaml":
            return str(source.get("path") or "").strip() or None
    return None


def _nested_text(payload: dict[str, Any], *keys: str) -> str | None:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    text = str(current or "").strip()
    return text or None


def _load_json_if_exists(path: str | Path | None) -> dict[str, Any]:
    if path is None or not str(path).strip():
        return {}
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = resolved.resolve()
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"failed to parse assistant/runtime config metadata: {resolved}",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    return payload if isinstance(payload, dict) else {}


def _resolve_path(raw: Any, *, default: Path, repo_root: Path | None = None) -> Path:
    text = str(raw or "").strip()
    if not text:
        return default.resolve()
    path = Path(text).expanduser()
    if path.is_absolute():
        return path.resolve()
    root = repo_root if repo_root is not None else repo_base()
    return (root / path).resolve()


def _target_profile(payload: dict[str, Any]) -> str:
    args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    return _required_text(args.get("model_profile"), "model_profile")


def _active_model_name(payload: dict[str, Any]) -> str | None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    value = str(summary.get("active_model") or "").strip()
    return value or None


def _markets_in_doc(config_doc: dict[str, Any]) -> list[str]:
    markets = config_doc.get("markets")
    if not isinstance(markets, dict):
        return []
    return [market for market in ("us", "hk") if isinstance(markets.get(market), dict)]


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message=f"{field_name} is required")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = ["handle_model_operation", "render_model_response"]
