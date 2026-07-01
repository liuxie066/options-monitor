from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from domain.domain.symbol_identity import symbol_market
from src.application.account_config import accounts_from_config, normalize_accounts
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response, mask_path
from src.application.assistant.contracts import AssistantRequest, PerceptionResult
from src.application.assistant.operation_lifecycle import (
    build_action_lifecycle,
    build_cancelled_operation_response,
    build_previewed_operation_response,
    confirm_previewed_operation_or_raise,
    resolve_pending_operation_or_raise,
)
from src.application.assistant.operation_policy import enforce_monitor_run_allowed
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.operation_status_text import cannot_repeat_message, operation_candidate_hint
from src.application.symbol_mutations import normalize_symbol_read


PREVIEW_INTENTS = frozenset({"monitor_run_now"})
CONFIRM_INTENTS = frozenset({"monitor_run_confirm", "monitor_run_cancel"})
MONITOR_RUN_OPERATION_TYPES = PREVIEW_INTENTS
MonitorRunRunner = Callable[..., subprocess.CompletedProcess[Any]]
MONITOR_RUNNER: MonitorRunRunner | None = None
_CONFIG_NAMES = {"config.us.json", "config.hk.json"}
_OUTPUT_LIMIT = 4000


def handle_monitor_run_operation(
    intent: PerceptionResult,
    request: AssistantRequest,
    *,
    command_id: str,
    store: InboundOperationStore,
) -> dict[str, Any]:
    policy = enforce_monitor_run_allowed(channel=request.channel, sender_id=request.sender_id)
    if intent.intent_name in PREVIEW_INTENTS:
        payload = _build_operation_payload(intent.intent_name, dict(intent.arguments), request=request)
        return _preview_and_save(
            payload,
            request=request,
            command_id=command_id,
            store=store,
            ttl_seconds=policy.confirm_ttl_seconds,
        )
    if intent.intent_name == "monitor_run_confirm":
        return _confirm_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    if intent.intent_name == "monitor_run_cancel":
        return _cancel_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported monitor run operation intent: {intent.intent_name}")


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
        tool_name="inbound.monitor_run",
        operation_id=command_id,
        request=request,
        store=store,
        payload=payload,
        preview=preview,
        ttl_seconds=ttl_seconds,
        response_text=lambda operation: render_monitor_run_response(
            status="previewed",
            operation_id=command_id,
            payload=payload,
            preview=preview,
            expires_at=str(operation.get("expires_at") or ""),
        ),
    )


def _confirm_operation(*, operation_id: str | None, request: AssistantRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_monitor_run_operation(
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
        subject="监控执行",
        expired_message="这条监控执行确认已过期，未运行 tick。",
        expired_hint="请重新发送：跑一次港股监控。",
        hash_mismatch_message="pending monitor run payload hash mismatch; refusing to run tick",
        confirmed_result={"operation_id": operation_id, "status": "confirmed", "task_status": "confirmed"},
    )
    operation_id = confirmed.operation_id
    operation_resolution = confirmed.operation_resolution
    payload = confirmed.payload
    preview = operation.get("preview") if isinstance(operation.get("preview"), dict) else _preview_operation(payload)
    running = {
        "operation_id": operation_id,
        "status": "running",
        "command": _preview_command(preview),
    }
    if not store.mark_running(operation_id, result=running):
        current = store.get(operation_id) or {}
        current_status = str(current.get("status") or "-")
        raise AgentToolError(
            code="INPUT_ERROR",
            message=cannot_repeat_message("监控执行", "执行", current_status),
            details={"operation_id": operation_id, "status": current_status},
        )
    try:
        result = _apply_operation(payload)
    except subprocess.TimeoutExpired as exc:
        failed = {
            "operation_id": operation_id,
            "status": "failed",
            "error": "TimeoutExpired",
            "message": str(exc),
        }
        store.mark_failed(operation_id, result=failed)
        raise AgentToolError(code="TOOL_RUNTIME_ERROR", message="监控执行超时。", details=failed) from exc
    except Exception as exc:
        failed = {
            "operation_id": operation_id,
            "status": "failed",
            "error": type(exc).__name__,
            "message": str(exc),
        }
        store.mark_failed(operation_id, result=failed)
        raise AgentToolError(code="TOOL_RUNTIME_ERROR", message="监控执行失败。", details=failed) from exc

    status = "applied" if int(result.get("returncode") or 0) == 0 else "failed"
    result = {**result, "operation_id": operation_id, "status": status}
    if status == "applied":
        store.mark_applied(operation_id, result=result)
    else:
        store.mark_failed(operation_id, result=result)
    text = render_monitor_run_response(status=status, operation_id=operation_id, payload=payload, preview=preview, result=result)
    lifecycle = build_action_lifecycle(
        operation_id=operation_id,
        operation_type=str(payload.get("operation_type") or ""),
        status=status,
        result=result,
        source="monitor_run_confirm_response",
    )
    return build_response(
        tool_name="inbound.monitor_run",
        ok=status == "applied",
        data={
            "operation_id": operation_id,
            **operation_resolution,
            "operation_type": payload["operation_type"],
            "status": status,
            "payload_hash": confirmed.payload_hash,
            "payload": payload,
            "preview": preview,
            "result": result,
            "action_lifecycle": lifecycle,
            "response_text": text,
        },
        error=None
        if status == "applied"
        else build_error_payload(
            AgentToolError(code="MONITOR_RUN_FAILED", message="监控执行失败。", details=result)
        ),
        meta={"audit_db": mask_path(store.path)},
    )


def _cancel_operation(*, operation_id: str | None, request: AssistantRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_monitor_run_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=True,
        action="取消",
    )
    text = f"监控执行已取消，未运行 tick。\ncommand_id: {operation_id}"
    return build_cancelled_operation_response(
        tool_name="inbound.monitor_run",
        operation_id=operation_id,
        operation=operation,
        operation_resolution=operation_resolution,
        store=store,
        response_text=text,
    )


def _resolve_monitor_run_operation(
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
        operation_types=MONITOR_RUN_OPERATION_TYPES,
        allow_expired=allow_expired,
        action=action,
        subject="监控执行",
        expired_message="这条监控执行确认已过期，未运行 tick。",
        expired_hint="请重新发送：跑一次港股监控。",
        none_hint="请先发送：跑一次港股监控。",
        wrong_family_message="这不是监控执行，不能用确认运行监控/取消运行监控处理。",
        not_found_message="找不到待确认的监控执行。",
        not_found_hint="请检查 operation_id，或重新发送：跑一次港股监控。",
        candidate_hint=_monitor_run_candidate_hint,
    )


def _monitor_run_candidate_hint(action: str, candidates: Any) -> str:
    return operation_candidate_hint(
        "/confirm monitor-run" if action == "确认" else "/cancel monitor-run",
        candidates,
        heading="候选监控执行",
    )


def _build_operation_payload(operation_type: str, arguments: dict[str, Any], *, request: AssistantRequest) -> dict[str, Any]:
    symbols = _normalize_symbols_arg(arguments.get("symbols") or arguments.get("symbol"))
    market = _resolve_market(arguments, request=request, symbols=symbols)
    timeout_seconds = _normalize_timeout(arguments.get("timeout_seconds") or arguments.get("timeout"))
    accounts = _normalize_accounts_arg(arguments.get("accounts"))
    config_path: str | None = None
    cfg: dict[str, Any] | None = None
    if not accounts or symbols:
        config_path, cfg = _load_market_runtime_config(market, request=request)
    if symbols and cfg is not None:
        symbols = _validate_monitor_symbols(symbols, cfg=cfg)
    if not accounts and cfg is not None:
        accounts = accounts_from_config(cfg, fallback=())
    if not accounts:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"{market} runtime config has no accounts for monitor run",
            hint="请在 runtime config 顶层 accounts 配置要执行的账号，或在命令中显式提供 accounts。",
        )
    no_send = bool(symbols)
    plan = _monitor_run_plan(
        market=market,
        accounts=accounts,
        symbols=symbols,
        timeout_seconds=timeout_seconds,
        no_send=no_send,
    )
    args: dict[str, Any] = {
        "market": market,
        "accounts": accounts,
        "symbols": symbols,
        "timeout_seconds": timeout_seconds,
        "no_send": no_send,
    }
    if config_path:
        args["config_path"] = config_path
    return {
        "schema_version": "1.0",
        "operation_type": operation_type,
        "arguments": args,
        "command": plan,
    }


def _preview_operation(payload: dict[str, Any]) -> dict[str, Any]:
    args = _payload_arguments(payload)
    plan = _monitor_run_plan(
        market=_required_text(args.get("market"), "market"),
        accounts=_normalize_accounts_arg(args.get("accounts")),
        symbols=_normalize_symbols_arg(args.get("symbols")),
        timeout_seconds=_normalize_timeout(args.get("timeout_seconds")),
        no_send=bool(args.get("no_send") or _normalize_symbols_arg(args.get("symbols"))),
    )
    return {
        "summary": {
            "operation": "tick-cron",
            "market": plan["market"],
            "accounts": list(plan["accounts"]),
            "symbols": list(plan["symbols"]),
            "timeout_seconds": plan["timeout_seconds"],
            "will_send_notifications": not bool(plan["no_send"]),
            "confirmed": False,
        },
        "command": plan,
    }


def _apply_operation(payload: dict[str, Any]) -> dict[str, Any]:
    args = _payload_arguments(payload)
    timeout_seconds = _normalize_timeout(args.get("timeout_seconds"))
    plan = _monitor_run_plan(
        market=_required_text(args.get("market"), "market"),
        accounts=_normalize_accounts_arg(args.get("accounts")),
        symbols=_normalize_symbols_arg(args.get("symbols")),
        timeout_seconds=timeout_seconds,
        no_send=bool(args.get("no_send") or _normalize_symbols_arg(args.get("symbols"))),
    )
    command = _actual_tick_cron_command(plan)
    root = repo_base()
    runner = MONITOR_RUNNER or _default_monitor_run_runner
    proc = runner(command, cwd=root, timeout_seconds=timeout_seconds + 30)
    return {
        "market": plan["market"],
        "accounts": list(plan["accounts"]),
        "symbols": list(plan["symbols"]),
        "no_send": bool(plan["no_send"]),
        "timeout_seconds": timeout_seconds,
        "command": plan["display_command"],
        "actual_command": command,
        "returncode": int(getattr(proc, "returncode", 1)),
        "stdout": _clip_output(getattr(proc, "stdout", "")),
        "stderr": _clip_output(getattr(proc, "stderr", "")),
    }


def _default_monitor_run_runner(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=max(1, int(timeout_seconds)),
        check=False,
    )


def render_monitor_run_response(
    *,
    status: str,
    operation_id: str,
    payload: dict[str, Any],
    preview: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    expires_at: str = "",
) -> str:
    data = _monitor_response_data(payload=payload, preview=preview, result=result)
    market = str(data.get("market") or "-")
    label = "港股" if market == "hk" else ("美股" if market == "us" else market)
    accounts = ", ".join(str(item) for item in data.get("accounts") or []) or "-"
    symbols = ", ".join(str(item) for item in data.get("symbols") or []) or "-"
    command = str(data.get("command") or "-")
    no_send = bool(data.get("no_send"))
    if status == "previewed":
        single_symbol = bool(data.get("symbols"))
        lines = [
            f"{label}{'单标' if single_symbol else ''}监控执行预览：tick-cron",
            f"市场：{market}",
            f"账户：{accounts}",
            f"通知：{'不会发送' if no_send else '确认后可能发送'}",
            f"命令：{command}",
            "",
            "未执行 tick，未发送通知。",
            "确认后会运行真实监控，写入运行产物。"
            if no_send
            else "确认后会运行真实监控，可能发送真实通知并写入运行产物。",
            f"确认执行请回复：/confirm monitor-run {operation_id}",
            f"取消请回复：/cancel monitor-run {operation_id}",
            "同一对话只有一条待确认监控执行时，也可以回复：确认运行监控 / 取消运行监控",
        ]
        if single_symbol:
            lines.insert(3, f"标的：{symbols}")
        if expires_at:
            lines.append("有效期：10 分钟。")
        return "\n".join(lines)
    if status == "applied":
        lines = [
            f"{label}监控执行完成。",
            f"市场：{market}",
            f"账户：{accounts}",
            f"通知：{'未发送' if no_send else '按监控规则处理'}",
            f"命令：{command}",
            f"returncode：{int((result or {}).get('returncode') or 0)}",
            f"command_id: {operation_id}",
        ]
        if symbols != "-":
            lines.insert(3, f"标的：{symbols}")
        return "\n".join(lines)
    if status == "failed":
        lines = [
            f"{label}监控执行失败。",
            f"市场：{market}",
            f"账户：{accounts}",
            f"命令：{command}",
            f"returncode：{(result or {}).get('returncode', '-')}",
            f"command_id: {operation_id}",
        ]
        if symbols != "-":
            lines.insert(3, f"标的：{symbols}")
        return "\n".join(lines)
    if status == "cancelled":
        return f"监控执行已取消，未运行 tick。\ncommand_id: {operation_id}"
    return f"监控执行状态：{status}\ncommand_id: {operation_id}"


def _monitor_response_data(
    *,
    payload: dict[str, Any],
    preview: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    args = _payload_arguments(payload)
    out.update({key: args.get(key) for key in ("market", "accounts", "symbols", "timeout_seconds", "no_send") if key in args})
    if isinstance(preview, dict):
        command = preview.get("command")
        if isinstance(command, dict):
            out.setdefault("command", command.get("display_command"))
            out.setdefault("market", command.get("market"))
            out.setdefault("accounts", command.get("accounts"))
            out.setdefault("symbols", command.get("symbols"))
            out.setdefault("no_send", command.get("no_send"))
            out.setdefault("timeout_seconds", command.get("timeout_seconds"))
    if isinstance(result, dict):
        out.update({key: result.get(key) for key in ("market", "accounts", "symbols", "no_send", "timeout_seconds", "command") if key in result})
    if "command" not in out and isinstance(payload.get("command"), dict):
        out["command"] = payload["command"].get("display_command")
    return out


def _monitor_run_plan(
    *,
    market: str,
    accounts: list[str],
    symbols: list[str],
    timeout_seconds: int,
    no_send: bool,
) -> dict[str, Any]:
    market_key = _market_from_value(market)
    if market_key is None:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="请明确要执行美股还是港股监控。")
    normalized_accounts = normalize_accounts(accounts, fallback=())
    normalized_symbols = _normalize_symbols_arg(symbols)
    no_send = bool(no_send or normalized_symbols)
    display_argv = ["./om", "run", "tick-cron", "--market", market_key]
    if normalized_accounts:
        display_argv.extend(["--accounts", *normalized_accounts])
    if normalized_symbols:
        display_argv.extend(["--symbols", ",".join(normalized_symbols)])
    display_argv.extend(["--timeout", str(timeout_seconds)])
    if no_send:
        display_argv.append("--no-send")
    return {
        "market": market_key,
        "accounts": normalized_accounts,
        "symbols": normalized_symbols,
        "no_send": no_send,
        "timeout_seconds": timeout_seconds,
        "display_argv": display_argv,
        "display_command": shlex.join(display_argv),
    }


def _actual_tick_cron_command(plan: dict[str, Any]) -> list[str]:
    root = repo_base()
    om_path = root / "om"
    if om_path.exists():
        return [str(om_path), *list(plan.get("display_argv") or [])[1:]]
    return [sys.executable, "-m", "src.interfaces.cli.main", *list(plan.get("display_argv") or [])[1:]]


def _load_market_runtime_config(market: str, *, request: AssistantRequest) -> tuple[str, dict[str, Any]]:
    config_path = _runtime_config_path_for_market(market, request=request)
    if config_path is not None:
        path, cfg = load_runtime_config(config_path=config_path, expected_market=market)
    else:
        path, cfg = load_runtime_config(config_key=market, expected_market=market)
    return str(path), cfg


def _runtime_config_path_for_market(market: str, *, request: AssistantRequest) -> str | None:
    raw_path = str(request.config_path or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if path.name in _CONFIG_NAMES:
        return str(path.with_name(f"config.{market}.json"))
    if str(request.config_key or "").strip().lower() == market:
        return str(path)
    return None


def _resolve_market(arguments: dict[str, Any], *, request: AssistantRequest, symbols: list[str]) -> str:
    explicit = _market_from_value(arguments.get("market") or arguments.get("config_key"))
    text_market = _market_from_text(str(arguments.get("raw_text") or request.text or ""))
    symbols_market = _market_from_symbols(symbols)
    if explicit and text_market and explicit != text_market:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="监控执行的市场不一致。",
            hint="请明确使用 hk/港股 或 us/美股。",
            details={"argument_market": explicit, "text_market": text_market},
        )
    if explicit and symbols_market and explicit != symbols_market:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="监控执行的标的市场和指定市场不一致。",
            hint="请明确使用 hk/港股 或 us/美股，并确认标的。",
            details={"argument_market": explicit, "symbols_market": symbols_market, "symbols": symbols},
        )
    if text_market and symbols_market and text_market != symbols_market:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="监控执行的标的市场和文本市场不一致。",
            hint="请明确使用 hk/港股 或 us/美股，并确认标的。",
            details={"text_market": text_market, "symbols_market": symbols_market, "symbols": symbols},
        )
    market = explicit or text_market or symbols_market
    if market is None:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="请明确要执行美股还是港股监控。",
            hint="例如：跑一次港股监控、单独跑一次 PDD 的监控，或 /monitor-run hk。",
        )
    return market


def _market_from_symbols(symbols: list[str]) -> str | None:
    markets = {
        str(symbol_market(symbol) or "").strip().lower()
        for symbol in symbols
        if str(symbol or "").strip()
    }
    markets.discard("")
    mapped = {"hk" if item == "hk" else "us" if item == "us" else item for item in markets}
    if not mapped:
        return None
    if mapped <= {"hk"}:
        return "hk"
    if mapped <= {"us"}:
        return "us"
    raise AgentToolError(
        code="NEEDS_CLARIFICATION",
        message="一次监控执行不能混合美股和港股标的。",
        hint="请拆成美股和港股两次执行。",
        details={"symbols": symbols, "markets": sorted(mapped)},
    )


def _market_from_text(text: str) -> str | None:
    raw = str(text or "").lower()
    compact = re.sub(r"\s+", "", raw)
    has_hk = "港股" in compact or "香港" in compact or bool(re.search(r"\b(hk|hong\s*kong|hongkong)\b", raw))
    has_us = "美股" in compact or "美国" in compact or bool(re.search(r"\b(us|usa|u\.s\.)\b", raw))
    if has_hk and has_us:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="同时提到了美股和港股，无法确定要执行哪个监控。",
            hint="请明确说：跑一次港股监控，或跑一次美股监控。",
        )
    if has_hk:
        return "hk"
    if has_us:
        return "us"
    return None


def _market_from_value(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"hk", "hongkong", "hong-kong", "hong_kong", "港股", "香港"}:
        return "hk"
    if text in {"us", "usa", "u.s.", "美股", "美国"}:
        return "us"
    return None


def _normalize_accounts_arg(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[\s,，]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []
    return normalize_accounts(raw_items, fallback=())


def _normalize_symbols_arg(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[\s,，]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []
    out: list[str] = []
    for raw in raw_items:
        symbol = normalize_symbol_read(raw)
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def _validate_monitor_symbols(symbols: list[str], *, cfg: dict[str, Any]) -> list[str]:
    requested = [normalize_symbol_read(item, config=cfg) for item in symbols if str(item).strip()]
    requested = [item for item in requested if item]
    configured = {
        normalize_symbol_read(item.get("symbol"), config=cfg): str(item.get("symbol") or "").strip()
        for item in (cfg.get("symbols") or [])
        if isinstance(item, dict)
    }
    missing = [item for item in requested if item not in configured]
    if missing:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="这些标的不在当前 runtime config 的监控列表中：" + ", ".join(missing),
            hint="请先增加监控标的，或确认要运行的市场/配置。",
            details={"missing_symbols": missing, "configured_symbols": sorted(k for k in configured if k)},
        )
    return list(dict.fromkeys(requested))


def _normalize_timeout(value: Any) -> int:
    try:
        parsed = int(str(value or "600").strip())
    except Exception:
        parsed = 600
    return max(1, min(parsed, 3600))


def _payload_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    return dict(args)


def _preview_command(preview: dict[str, Any]) -> str:
    command = preview.get("command") if isinstance(preview, dict) else {}
    return str(command.get("display_command") or "") if isinstance(command, dict) else ""


def _clip_output(value: Any) -> str:
    text = str(value or "")
    if len(text) <= _OUTPUT_LIMIT:
        return text
    return text[: _OUTPUT_LIMIT - 3] + "..."


def _required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AgentToolError(code="INPUT_ERROR", message=f"{name} is required")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = ["MONITOR_RUNNER", "handle_monitor_run_operation", "render_monitor_run_response"]
