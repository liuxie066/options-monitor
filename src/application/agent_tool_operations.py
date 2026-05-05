from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from scripts.agent_plugin.contracts import AgentToolError


def _as_int(value: Any, *, default: int, minimum: int = 1, maximum: int = 500) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    return max(int(minimum), min(int(maximum), out))


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception as exc:
        raise AgentToolError(code="INPUT_ERROR", message=f"expected integer value, got: {value}") from exc


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception as exc:
        raise AgentToolError(code="INPUT_ERROR", message=f"expected numeric value, got: {value}") from exc


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _resolve_local_path(value: Any, *, base: Path, default: Path) -> Path:
    if value in (None, ""):
        return default.resolve()
    path = Path(str(value))
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def version_check_tool(
    payload: dict[str, Any],
    *,
    check_version_update: Callable[..., dict[str, Any]],
    repo_base: Callable[[], Path],
    mask_path: Callable[[Any], str],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    remote_name = str(payload.get("remote_name") or "origin").strip() or "origin"
    result = check_version_update(base_dir=repo_base(), remote_name=remote_name)
    warnings: list[str] = []
    if not bool(result.get("ok", True)):
        message = str(result.get("message") or "version check failed").strip()
        error = str(result.get("error") or "").strip()
        warnings.append(f"{message}: {error}" if error else message)
    return result, warnings, {"repo_base": mask_path(repo_base()), "remote_name": remote_name}


def config_validate_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config: Callable[..., tuple[Path, dict[str, Any]]],
    validate_runtime_config: Callable[..., list[str]],
    accounts_from_config: Callable[[dict[str, Any]], list[str]],
    resolve_watchlist_config: Callable[[dict[str, Any]], list[dict[str, Any]]],
    mask_path: Callable[[Any], str],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    warnings = validate_runtime_config(cfg, allow_empty_symbols=bool(payload.get("allow_empty_symbols", False)))
    accounts = accounts_from_config(cfg)
    symbols = resolve_watchlist_config(cfg)
    data = {
        "ok": True,
        "config_path": mask_path(config_path),
        "config_key": _optional_text(payload.get("config_key")),
        "account_count": len(accounts),
        "accounts": accounts,
        "symbol_count": len(symbols),
        "warnings": warnings,
    }
    return data, warnings, {"config_path": mask_path(config_path)}


def scheduler_status_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config: Callable[..., tuple[Path, dict[str, Any]]],
    read_state: Callable[[Path], dict[str, Any]],
    decide: Callable[..., Any],
    repo_base: Callable[[], Path],
    mask_path: Callable[[Any], str],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    base = repo_base()
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    schedule_key = str(payload.get("schedule_key") or "schedule").strip() or "schedule"
    schedule_cfg = cfg.get(schedule_key) if isinstance(cfg.get(schedule_key), dict) else {}
    schedule_enabled = bool((schedule_cfg or {}).get("enabled", True))

    default_state_dir = (base / "output" / "state").resolve()
    state_dir = _resolve_local_path(payload.get("state_dir"), base=base, default=default_state_dir)
    default_state = (state_dir / "scheduler_state.json").resolve()
    state_path = _resolve_local_path(payload.get("state"), base=base, default=default_state)

    try:
        state_data = read_state(state_path)
    except Exception as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message="scheduler state is unreadable",
            details={"state_path": mask_path(state_path), "error": str(exc)},
        ) from exc

    account = _optional_text(payload.get("account"))
    decision = decide(
        schedule_cfg or {},
        state_data,
        datetime.now(timezone.utc),
        account=account,
        schedule_key=schedule_key,
        force=bool(payload.get("force", False)),
    )
    decision_payload = asdict(decision)
    decision_payload["should_notify"] = bool(decision_payload.get("is_notify_window_open"))
    decision_payload["schedule_enabled"] = schedule_enabled

    last_scan_by_account = state_data.get("last_scan_utc_by_account")
    last_notify_by_account = state_data.get("last_notify_utc_by_account")
    data = {
        "decision": decision_payload,
        "state": {
            "state_path": mask_path(state_path),
            "last_scan_utc": state_data.get("last_scan_utc"),
            "last_notify_utc": state_data.get("last_notify_utc"),
            "last_scan_utc_for_account": (
                last_scan_by_account.get(account) if account and isinstance(last_scan_by_account, dict) else None
            ),
            "last_notify_utc_for_account": (
                last_notify_by_account.get(account) if account and isinstance(last_notify_by_account, dict) else None
            ),
        },
        "filters": {
            "account": account,
            "schedule_key": schedule_key,
            "force": bool(payload.get("force", False)),
        },
    }
    return data, [], {"config_path": mask_path(config_path), "state_path": mask_path(state_path)}


def _event_row(event: dict[str, Any], *, normalize_broker: Callable[[Any], str], normalize_account: Callable[[Any], str]) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id"),
        "trade_time_ms": event.get("trade_time_ms"),
        "source_type": event.get("source_type"),
        "source_name": event.get("source_name"),
        "broker": normalize_broker(event.get("broker")),
        "account": normalize_account(event.get("account")) if event.get("account") else None,
        "symbol": event.get("symbol"),
        "option_type": event.get("option_type"),
        "side": event.get("side"),
        "position_effect": event.get("position_effect"),
        "contracts": event.get("contracts"),
        "price": event.get("price"),
        "strike": event.get("strike"),
        "expiration_ymd": event.get("expiration_ymd"),
        "currency": event.get("currency"),
    }


def _events_action(
    repo: Any,
    payload: dict[str, Any],
    *,
    normalize_broker: Callable[[Any], str],
    normalize_account: Callable[[Any], str],
) -> dict[str, Any]:
    list_trade_events = getattr(repo, "list_trade_events", None)
    if not callable(list_trade_events):
        raise AgentToolError(code="DEPENDENCY_MISSING", message="option positions repository does not expose trade events")

    broker = _optional_text(payload.get("broker"))
    broker = normalize_broker(broker) if broker else None
    account = normalize_account(payload.get("account")) if payload.get("account") else None
    symbol = _optional_text(payload.get("symbol"))
    symbol = symbol.upper() if symbol else None
    option_type = _optional_text(payload.get("option_type"))
    option_type = option_type.lower() if option_type else None
    strike = _optional_float(payload.get("strike"))
    expiration_ymd = _optional_text(payload.get("exp") or payload.get("expiration_ymd"))
    limit = _as_int(payload.get("limit"), default=50)

    rows: list[dict[str, Any]] = []
    for event in reversed(list_trade_events()):
        if not isinstance(event, dict):
            continue
        event_broker = normalize_broker(event.get("broker"))
        event_account = normalize_account(event.get("account")) if event.get("account") else None
        if broker and event_broker != broker:
            continue
        if account and event_account != account:
            continue
        if symbol and str(event.get("symbol") or "").strip().upper() != symbol:
            continue
        if option_type and str(event.get("option_type") or "").strip().lower() != option_type:
            continue
        if strike is not None:
            current_strike = _optional_float(event.get("strike"))
            if current_strike is None or abs(current_strike - strike) >= 1e-9:
                continue
        if expiration_ymd and str(event.get("expiration_ymd") or "").strip() != expiration_ymd:
            continue
        rows.append(_event_row(event, normalize_broker=normalize_broker, normalize_account=normalize_account))
        if len(rows) >= limit:
            break
    return {
        "rows": rows,
        "row_count": len(rows),
        "filters": {
            "broker": broker,
            "account": account,
            "symbol": symbol,
            "option_type": option_type,
            "strike": strike,
            "expiration_ymd": expiration_ymd,
            "limit": limit,
        },
    }


def option_positions_read_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config: Callable[..., tuple[Path, dict[str, Any]]],
    resolve_public_data_config_path: Callable[[dict[str, Any], dict[str, Any]], Path],
    normalize_broker: Callable[[Any], str],
    normalize_account: Callable[[Any], str],
    resolve_option_positions_repo: Callable[..., tuple[Path, Any]],
    list_position_rows: Callable[..., list[dict[str, Any]]],
    build_lot_event_history: Callable[..., list[dict[str, Any]]],
    inspect_projection_state: Callable[..., dict[str, Any]],
    repo_base: Callable[[], Path],
    mask_path: Callable[[Any], str],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    action = str(payload.get("action") or "list").strip().lower()
    if action not in {"list", "events", "history", "inspect"}:
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported option_positions_read action: {action}")

    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    data_config_path = resolve_public_data_config_path(payload, portfolio_cfg)
    _resolved_data_config, repo = resolve_option_positions_repo(base=repo_base(), data_config=data_config_path)

    warnings: list[str] = []
    bootstrap_status = getattr(repo, "bootstrap_status", None)
    bootstrap_message = getattr(repo, "bootstrap_message", None)
    if bootstrap_status and str(bootstrap_status).startswith("degraded"):
        warnings.append(str(bootstrap_message or bootstrap_status))

    data: dict[str, Any]
    if action == "list":
        broker = normalize_broker(payload.get("broker") or portfolio_cfg.get("broker") or "富途")
        account = _optional_text(payload.get("account"))
        status = str(payload.get("status") or "open").strip().lower()
        if status not in {"open", "close", "all"}:
            raise AgentToolError(code="INPUT_ERROR", message="status must be one of: open, close, all")
        limit = _as_int(payload.get("limit"), default=50)
        expiration_within_days = _optional_int(payload.get("exp_within_days") or payload.get("expiration_within_days"))
        rows = list_position_rows(
            repo,
            broker=broker,
            account=account,
            status=status,
            limit=limit,
            expiration_within_days=expiration_within_days,
        )
        data = {
            "action": action,
            "rows": rows,
            "row_count": len(rows),
            "filters": {
                "broker": broker,
                "account": normalize_account(account) if account else None,
                "status": status,
                "limit": limit,
                "expiration_within_days": expiration_within_days,
            },
        }
    elif action == "events":
        event_data = _events_action(repo, payload, normalize_broker=normalize_broker, normalize_account=normalize_account)
        data = {"action": action, **event_data}
    elif action == "history":
        record_id = _optional_text(payload.get("record_id"))
        if not record_id:
            raise AgentToolError(code="INPUT_ERROR", message="record_id is required for option_positions_read history")
        try:
            history = build_lot_event_history(repo, record_id=record_id)
        except ValueError as exc:
            raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
        data = {
            "action": action,
            "record_id": record_id,
            "events": history,
            "event_count": len(history),
        }
    else:
        selectors = {
            "record_id": _optional_text(payload.get("record_id")),
            "feishu_record_id": _optional_text(payload.get("feishu_record_id")),
            "account": _optional_text(payload.get("account")),
            "symbol": _optional_text(payload.get("symbol")),
            "option_type": _optional_text(payload.get("option_type")),
            "strike": _optional_float(payload.get("strike")),
            "expiration_ymd": _optional_text(payload.get("exp") or payload.get("expiration_ymd")),
        }
        if not any(value not in (None, "") for value in selectors.values()):
            raise AgentToolError(code="INPUT_ERROR", message="inspect requires at least one selector")
        inspected = inspect_projection_state(repo, **selectors)
        data = {"action": action, **inspected}

    data["bootstrap"] = {
        "status": bootstrap_status,
        "message": bootstrap_message,
    }
    return data, warnings, {"config_path": mask_path(config_path), "data_config": mask_path(data_config_path)}
