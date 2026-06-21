from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.application.account_config import normalize_accounts
from src.application.agent_tool_config import repo_base, resolve_runtime_config_path
from src.application.agent_tool_contracts import AgentToolError, build_response, mask_path
from src.application.config_loader import resolve_watchlist_config, set_watchlist_config
from src.application.config_validator import validate_config
from src.application.config_yaml import RESOLVED_KEY, load_yaml_config_file
from src.application.config_yaml_symbols import set_yaml_symbol_config
from src.application.runtime_config_freshness import GENERATED_KEY, infer_runtime_config_market
from src.application.assistant.contracts import AssistantRequest, PerceptionResult
from src.application.assistant.operation_lifecycle import (
    build_cancelled_operation_response,
    build_previewed_operation_response,
    confirm_previewed_operation_or_raise,
    resolve_pending_operation_or_raise,
)
from src.application.assistant.operation_policy import enforce_symbol_write_allowed
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.operation_status_text import operation_candidate_hint
from src.application.runtime_config_paths import write_json_atomic
from src.application.symbol_calibration import calibrate_symbol
from src.application.symbol_mutations import add_symbol_entry, edit_symbol_entry, remove_symbol_entry


LIST_INTENTS = frozenset({"symbol_list"})
PREVIEW_INTENTS = frozenset({"symbol_add", "symbol_edit", "symbol_remove"})
CONFIRM_INTENTS = frozenset({"symbol_confirm", "symbol_cancel"})
SYMBOL_OPERATION_TYPES = PREVIEW_INTENTS


def handle_symbol_operation(
    intent: PerceptionResult,
    request: AssistantRequest,
    *,
    command_id: str,
    store: InboundOperationStore,
) -> dict[str, Any]:
    if intent.intent_name == "symbol_list":
        return _list_symbols(request)
    policy = enforce_symbol_write_allowed(channel=request.channel, sender_id=request.sender_id)
    if intent.intent_name in PREVIEW_INTENTS:
        arguments = dict(intent.arguments)
        yaml_payload = _build_yaml_operation_payload(intent.intent_name, arguments, request=request)
        if yaml_payload is not None:
            return _preview_and_save(
                yaml_payload,
                request=request,
                command_id=command_id,
                store=store,
                ttl_seconds=policy.confirm_ttl_seconds,
            )
        config_path, _cfg, config_key = _load_config_for_symbol_request(request, arguments=arguments)
        payload = _build_operation_payload(intent.intent_name, arguments, request=request, config_path=config_path, config_key=config_key)
        return _preview_and_save(payload, request=request, command_id=command_id, store=store, ttl_seconds=policy.confirm_ttl_seconds)
    if intent.intent_name == "symbol_confirm":
        return _confirm_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    if intent.intent_name == "symbol_cancel":
        return _cancel_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported symbol operation intent: {intent.intent_name}")


def _list_symbols(request: AssistantRequest) -> dict[str, Any]:
    config_path, cfg = _load_config(request)
    rows = _symbol_rows(cfg)
    text = render_symbol_response(status="listed", operation_id="", payload={}, preview={"symbols": rows, "config_path": str(config_path)})
    return build_response(tool_name="inbound.symbols", ok=True, data={"status": "listed", "config_path": str(config_path), "symbols": rows, "symbol_count": len(rows), "response_text": text})


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
        tool_name="inbound.symbols",
        operation_id=command_id,
        request=request,
        store=store,
        payload=payload,
        preview=preview,
        ttl_seconds=ttl_seconds,
        response_text=lambda operation: render_symbol_response(
            status="previewed",
            operation_id=command_id,
            payload=payload,
            preview=preview,
            expires_at=str(operation.get("expires_at") or ""),
        ),
    )


def _confirm_operation(*, operation_id: str | None, request: AssistantRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_symbol_operation(
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
        subject="监控标的变更",
        expired_message="这条监控标的变更确认已过期，未写入配置。",
        expired_hint="请重新发送监控标的命令生成新的预览。",
        hash_mismatch_message="pending symbol operation payload hash mismatch; refusing to write config",
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
        raise AgentToolError(code="INTERNAL_ERROR", message="symbol operation failed before config write could be confirmed", details=failed) from exc
    store.mark_applied(operation_id, result=result)
    text = render_symbol_response(status="applied", operation_id=operation_id, payload=payload, preview=preview, result=result)
    return build_response(
        tool_name="inbound.symbols",
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
    operation_id, operation, operation_resolution = _resolve_symbol_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=True,
        action="取消",
    )
    text = f"监控标的变更已取消，未写入配置。\ncommand_id: {operation_id}"
    return build_cancelled_operation_response(
        tool_name="inbound.symbols",
        operation_id=operation_id,
        operation=operation,
        operation_resolution=operation_resolution,
        store=store,
        response_text=text,
    )


def _resolve_symbol_operation(
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
        operation_types=SYMBOL_OPERATION_TYPES,
        allow_expired=allow_expired,
        action=action,
        subject="监控标的变更",
        expired_message="这条监控标的变更确认已过期，未写入配置。",
        expired_hint="请重新发送监控标的命令生成新的预览。",
        none_hint="请先发送监控标的变更命令生成预览。",
        wrong_family_message="这不是监控标的变更，不能用确认监控/取消监控处理。",
        not_found_message="找不到待确认的监控标的变更。",
        not_found_hint="请检查 operation_id，或重新发送监控标的命令。",
        candidate_hint=_symbol_candidate_hint,
    )


def _candidate_hint(prefix: str, candidates: Any) -> str:
    return operation_candidate_hint(prefix, candidates, heading="候选变更")


def _symbol_candidate_hint(action: str, candidates: Any) -> str:
    return _candidate_hint("/confirm symbol" if action == "确认" else "/cancel symbol", candidates)


def _build_operation_payload(
    operation_type: str,
    arguments: dict[str, Any],
    *,
    request: AssistantRequest,
    config_path: Any | None = None,
    config_key: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "operation_type": operation_type,
        "arguments": arguments,
        "config": {
            "config_key": config_key if config_key is not None else request.config_key,
            "config_path": str(config_path) if config_path else request.config_path,
        },
    }


def _build_yaml_operation_payload(
    operation_type: str,
    arguments: dict[str, Any],
    *,
    request: AssistantRequest,
) -> dict[str, Any] | None:
    if operation_type != "symbol_edit":
        return None
    config_yaml_path = _discover_config_yaml_path(request)
    if config_yaml_path is None:
        return None
    settings = _yaml_symbol_settings_from_edit(arguments)
    if settings is None:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="IM config.yaml 设置目前只支持 covered call 开关、covered call 最低行权价、sell put 开关。",
            hint="示例：设置 09898 covered call min strike 85，或使用 covered_call.min_strike=85 / sell_put.enabled=false。",
            details={"supported_fields": ["sell_call.enabled", "covered_call.enabled", "sell_call.min_strike", "covered_call.min_strike", "sell_put.enabled"]},
        )
    config_doc = load_yaml_config_file(config_yaml_path)
    market = _yaml_symbol_market(arguments, config_doc=config_doc, request=request)
    if market is None:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="监控标的设置前需要明确市场。",
            hint="请明确美股或港股，或使用可以校准市场的 symbol，例如 09898 / 9898.HK / NVDA。",
        )
    runtime_root = _runtime_root_for_rebuild(request)
    return {
        "schema_version": "1.0",
        "operation_type": operation_type,
        "arguments": arguments,
        "config": {
            "source_format": "yaml",
            "config_yaml_path": str(config_yaml_path),
            "market": market,
            "runtime_root": str(runtime_root) if runtime_root else None,
        },
        "yaml_symbol_set": {
            "symbol": _required_text(arguments.get("symbol"), "symbol"),
            **settings,
        },
    }


def _preview_operation(payload: dict[str, Any]) -> dict[str, Any]:
    if _payload_source_format(payload) == "yaml":
        result = _run_yaml_symbol_set(payload, apply=False)
        summary = result["summary"] if isinstance(result.get("summary"), dict) else {}
        return {
            "config_path": result.get("config_yaml_path"),
            "summary": summary,
            "validation": result.get("validation"),
            "source_format": "yaml",
        }
    config_path, cfg = _load_config_for_payload(payload)
    mutated = deepcopy(cfg)
    summary = _apply_symbol_payload(mutated, payload)
    _validate_symbols_config(mutated)
    return {"config_path": str(config_path), "summary": summary, "symbol_count_before": len(_symbol_rows(cfg)), "symbol_count_after": len(_symbol_rows(mutated)), "symbols": _symbol_rows(mutated)}


def _apply_operation(payload: dict[str, Any]) -> dict[str, Any]:
    if _payload_source_format(payload) == "yaml":
        result = _run_yaml_symbol_set(payload, apply=True)
        return {
            "status": "applied",
            "config_path": result.get("config_yaml_path"),
            "summary": result.get("summary"),
            "validation": result.get("validation"),
            "rebuild": result.get("rebuild"),
            "source_format": "yaml",
            "backup_path": result.get("backup_path"),
            "audit_id": result.get("audit_id"),
            "rollback_hint": result.get("rollback_hint"),
        }
    config_path, cfg = _load_config_for_payload(payload)
    summary = _apply_symbol_payload(cfg, payload)
    canonical = set_watchlist_config(cfg, resolve_watchlist_config(cfg))
    _validate_symbols_config(canonical)
    write_json_atomic(config_path, canonical)
    return {"status": "applied", "config_path": str(config_path), "summary": summary, "symbol_count": len(_symbol_rows(canonical))}


def _apply_symbol_payload(cfg: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    operation_type = str(payload.get("operation_type") or "")
    args = dict(payload.get("arguments") or {})
    if operation_type == "symbol_add":
        summary = add_symbol_entry(
            cfg,
            symbol=_required_text(args.get("symbol"), "symbol"),
            use=str(args["use"]) if args.get("use") is not None else None,
            limit_expirations=int(args.get("limit_expirations") or 8),
            sell_put_enabled=bool(args.get("sell_put_enabled", False)),
            sell_call_enabled=bool(args.get("sell_call_enabled", False)),
            accounts=args.get("accounts") if isinstance(args.get("accounts"), list) else None,
            normalize_accounts=lambda value: normalize_accounts(value, fallback=()),
            error_factory=_input_error,
        )
        return summary.public_payload()
    if operation_type == "symbol_edit":
        sets = args.get("set")
        if not isinstance(sets, dict) or not sets:
            raise AgentToolError(code="NEEDS_CLARIFICATION", message="修改监控标的需要提供 field=value。")
        if any(str(key).strip() == "symbol" or str(key).strip().startswith("symbol.") for key in sets):
            raise AgentToolError(code="INPUT_ERROR", message="不能通过 edit 修改 symbol 本身；请删除后重新新增。")
        ensure_use = args.get("ensure_use") if isinstance(args.get("ensure_use"), list) else None
        return edit_symbol_entry(
            cfg,
            symbol=_required_text(args.get("symbol"), "symbol"),
            sets=sets,
            ensure_use=ensure_use,
            error_factory=_input_error,
        ).public_payload()
    if operation_type == "symbol_remove":
        return remove_symbol_entry(cfg, symbol=_required_text(args.get("symbol"), "symbol"), error_factory=_input_error).public_payload()
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported symbol operation_type: {operation_type}")


def _payload_source_format(payload: dict[str, Any]) -> str:
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    return str(config.get("source_format") or "").strip().lower()


def _run_yaml_symbol_set(payload: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    settings = payload.get("yaml_symbol_set") if isinstance(payload.get("yaml_symbol_set"), dict) else {}
    config_yaml_path = _resolve_path(config.get("config_yaml_path"))
    runtime_root = _optional_path(config.get("runtime_root"))
    return set_yaml_symbol_config(
        repo_root=repo_base(),
        market=_required_text(config.get("market"), "market"),
        symbol=_required_text(settings.get("symbol"), "symbol"),
        config_path=config_yaml_path,
        covered_call_enabled=_optional_bool(settings.get("covered_call_enabled"), "covered_call_enabled"),
        covered_call_min_strike=_optional_float(settings.get("covered_call_min_strike"), "covered_call_min_strike"),
        sell_put_enabled=_optional_bool(settings.get("sell_put_enabled"), "sell_put_enabled"),
        rebuild_runtime_root=runtime_root,
        apply=apply,
        backup=True,
    )


def _load_config_for_payload(payload: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    config_key = str(config.get("config_key") or "").strip().lower() or None
    return _load_config(AssistantRequest(text="", sender_id="", channel="local", config_key=config_key, config_path=config.get("config_path")))


def _load_config_for_symbol_request(request: AssistantRequest, *, arguments: dict[str, Any]) -> tuple[Any, dict[str, Any], str | None]:
    _require_runtime_config_scope(request)
    current_path, current_cfg = _load_config(request)
    target_market = _symbol_market_from_arguments(arguments, config=current_cfg)
    if target_market is None:
        return current_path, current_cfg, request.config_key

    current_market = infer_runtime_config_market(
        config_key=request.config_key,
        config_path=current_path,
        config=current_cfg,
    )
    if current_market == target_market:
        return current_path, current_cfg, target_market

    target_path = _runtime_config_path_for_market(request=request, current_path=current_path, target_market=target_market)
    target_cfg = _read_runtime_config(target_path)
    return target_path, target_cfg, target_market


def _load_config(request: AssistantRequest) -> tuple[Any, dict[str, Any]]:
    _require_runtime_config_scope(request)
    config_path = resolve_runtime_config_path(config_key=request.config_key, config_path=request.config_path)
    return config_path, _read_runtime_config(config_path)


def _read_runtime_config(config_path: Any) -> dict[str, Any]:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"failed to load runtime config: {config_path.name}", details={"error": f"{type(exc).__name__}: {exc}"}) from exc
    if not isinstance(data, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="runtime config must be a JSON object")
    return data


def _discover_config_yaml_path(request: AssistantRequest) -> Path | None:
    repo_root = repo_base()
    for raw_path in (request.assistant_config_path, request.config_path):
        path_text = str(raw_path or "").strip()
        if not path_text:
            continue
        discovered = _config_yaml_path_from_json(_resolve_path(path_text), repo_root=repo_root)
        if discovered is not None:
            return discovered
    if request.config_key:
        try:
            runtime_path = resolve_runtime_config_path(config_key=request.config_key, config_path=None)
        except AgentToolError:
            runtime_path = None
        if runtime_path is not None:
            discovered = _config_yaml_path_from_json(runtime_path, repo_root=repo_root)
            if discovered is not None:
                return discovered
    return None


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
            return _resolve_path(candidate, repo_root=repo_root)
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


def _load_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _runtime_root_for_rebuild(request: AssistantRequest) -> Path | None:
    if request.config_path:
        return _resolve_path(request.config_path).parent
    if request.config_key:
        try:
            return resolve_runtime_config_path(config_key=request.config_key, config_path=None).parent
        except AgentToolError:
            return None
    if request.assistant_config_path:
        path = _resolve_path(request.assistant_config_path)
        if path.parent.name == "resolved":
            return path.parent.parent
    return None


def _resolve_path(raw: Any, *, repo_root: Path | None = None) -> Path:
    path = Path(str(raw or "")).expanduser()
    if not path.is_absolute():
        base = repo_root or Path.cwd()
        path = base / path
    return path.resolve()


def _optional_path(raw: Any) -> Path | None:
    text = str(raw or "").strip()
    return _resolve_path(text) if text else None


def _symbol_market_from_arguments(arguments: dict[str, Any], *, config: dict[str, Any]) -> str | None:
    raw_symbol = str(arguments.get("symbol") or "").strip()
    if not raw_symbol:
        return None
    calibrated = calibrate_symbol(raw_symbol, config=config)
    if calibrated.status != "ok":
        calibrated = calibrate_symbol(raw_symbol)
    market = str(calibrated.market or "").strip().upper()
    if market == "US":
        return "us"
    if market == "HK":
        return "hk"
    return None


def _yaml_symbol_market(
    arguments: dict[str, Any],
    *,
    config_doc: dict[str, Any],
    request: AssistantRequest,
) -> str | None:
    raw_symbol = str(arguments.get("symbol") or "").strip()
    if raw_symbol:
        calibrated = calibrate_symbol(raw_symbol, config=config_doc)
        if calibrated.status != "ok":
            calibrated = calibrate_symbol(raw_symbol)
        market = str(calibrated.market or "").strip().upper()
        if market == "US":
            return "us"
        if market == "HK":
            return "hk"
    if request.config_key in {"us", "hk"}:
        return request.config_key
    if request.config_path:
        config = _load_json_if_exists(_resolve_path(request.config_path))
        inferred = infer_runtime_config_market(config_path=_resolve_path(request.config_path), config=config)
        if inferred in {"us", "hk"}:
            return inferred
    return None


def _runtime_config_path_for_market(
    *,
    request: AssistantRequest,
    current_path: Any,
    target_market: str,
) -> Any:
    if request.config_path:
        sibling = current_path.with_name(f"config.{target_market}.json")
        if sibling.exists():
            return sibling
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=f"监控标的属于 {target_market.upper()}，但没有找到对应 runtime config。",
            hint=f"请传入 config.{target_market}.json 的 --config-path，或先构建对应市场配置。",
            details={"requested_market": target_market, "current_config_path": str(current_path)},
        )
    return resolve_runtime_config_path(config_key=target_market, config_path=None)


def _require_runtime_config_scope(request: AssistantRequest) -> None:
    if request.config_path or request.config_key:
        return
    raise AgentToolError(
        code="NEEDS_CLARIFICATION",
        message="监控标的操作前需要先指定市场。",
        hint="请明确美股或港股，或通过 --config-key us/hk、--config-path、assistant.default_market_scope 配置默认市场。",
        details={"required": "config_key_or_config_path"},
    )


def _validate_symbols_config(cfg: dict[str, Any]) -> None:
    try:
        validate_config(dict(cfg))
    except SystemExit as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=str(exc)) from exc


def _symbol_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "symbol": item.get("symbol"),
            "use": item.get("use"),
            "accounts": item.get("accounts"),
            "sell_put_enabled": bool((item.get("sell_put") or {}).get("enabled", False)),
            "sell_call_enabled": bool((item.get("sell_call") or {}).get("enabled", False)),
        }
        for item in resolve_watchlist_config(cfg)
    ]


def render_symbol_response(
    *,
    status: str,
    operation_id: str,
    payload: dict[str, Any],
    preview: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    expires_at: str | None = None,
) -> str:
    del result
    if status == "listed":
        rows = preview.get("symbols") if isinstance(preview, dict) else []
        if not isinstance(rows, list) or not rows:
            return "当前没有配置监控标的。"
        lines = [f"当前监控标的：{len(rows)} 个"]
        for row in rows[:20]:
            if isinstance(row, dict):
                modes = []
                if row.get("sell_put_enabled"):
                    modes.append("put")
                if row.get("sell_call_enabled"):
                    modes.append("call")
                lines.append(f"- {row.get('symbol') or '-'} | {','.join(modes) if modes else 'off'} | use={row.get('use') or '-'}")
        return "\n".join(lines)
    if status == "cancelled":
        return f"监控标的变更已取消，未写入配置。\ncommand_id: {operation_id}"
    summary = preview.get("summary") if isinstance(preview, dict) and isinstance(preview.get("summary"), dict) else {}
    cal = summary.get("calibration") if isinstance(summary.get("calibration"), dict) else {}
    action = str(summary.get("action") or str(payload.get("operation_type") or "").removeprefix("symbol_"))
    action_label = {"add": "新增", "edit": "修改", "remove": "删除"}.get(action, action)
    title = "监控标的变更预览" if status == "previewed" else "监控标的变更已写入配置"
    lines = [
        f"{title}：{action_label}",
        f"输入：{summary.get('raw_symbol') or '-'}",
        f"校准为：{summary.get('canonical_symbol') or '-'}",
        f"市场：{cal.get('market') or '-'}",
        f"Futu code：{cal.get('futu_code') or '-'}",
        f"来源：{cal.get('source_kind') or '-'}",
    ]
    changed_paths = summary.get("changed_paths")
    if isinstance(changed_paths, list) and changed_paths:
        lines.append("变更：" + "、".join(str(item) for item in changed_paths))
    if isinstance(preview, dict) and preview.get("config_path"):
        lines.append(f"配置：{preview.get('config_path')}")
    if status == "previewed":
        lines.extend(
            [
                "",
                "未写入配置。",
                f"确认写入请回复：/confirm symbol {operation_id}",
                f"取消请回复：/cancel symbol {operation_id}",
                f"operation_id：{operation_id}",
                "同一对话只有一条待确认监控变更时，也可以回复：确认监控 / 取消监控",
            ]
        )
        if expires_at:
            lines.append("有效期：10 分钟。")
    else:
        lines.append(f"command_id：{operation_id}")
    return "\n".join(str(line) for line in lines)


def _input_error(message: str) -> AgentToolError:
    return AgentToolError(code="INPUT_ERROR", message=message)


def _yaml_symbol_settings_from_edit(arguments: dict[str, Any]) -> dict[str, Any] | None:
    sets = arguments.get("set")
    if not isinstance(sets, dict) or not sets:
        return None
    normalized = {str(key).strip(): value for key, value in sets.items()}
    supported = {
        "sell_call.enabled",
        "covered_call.enabled",
        "sell_call.min_strike",
        "covered_call.min_strike",
        "sell_put.enabled",
    }
    if any(key not in supported for key in normalized):
        return None
    out: dict[str, Any] = {}
    if "sell_call.enabled" in normalized:
        out["covered_call_enabled"] = _optional_bool(normalized["sell_call.enabled"], "sell_call.enabled")
    if "covered_call.enabled" in normalized:
        out["covered_call_enabled"] = _optional_bool(normalized["covered_call.enabled"], "covered_call.enabled")
    if "sell_call.min_strike" in normalized:
        out["covered_call_min_strike"] = _optional_float(normalized["sell_call.min_strike"], "sell_call.min_strike")
    if "covered_call.min_strike" in normalized:
        out["covered_call_min_strike"] = _optional_float(normalized["covered_call.min_strike"], "covered_call.min_strike")
    if "sell_put.enabled" in normalized:
        out["sell_put_enabled"] = _optional_bool(normalized["sell_put.enabled"], "sell_put.enabled")
    return out or None


def _optional_bool(value: Any, field_name: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise AgentToolError(code="INPUT_ERROR", message=f"{field_name} must be true/false")


def _optional_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception as exc:
        raise AgentToolError(code="INPUT_ERROR", message=f"{field_name} must be a number") from exc


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message=f"{field_name} is required")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
