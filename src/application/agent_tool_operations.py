from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast

from src.application.agent_tool_contracts import AgentToolError
from src.application.ledger.api import ledger_store_payload
from src.application.positions.reporting import build_monthly_income_report
from src.application.trade_time_format import add_trade_time_beijing


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


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _bool_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _optional_path(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(text).expanduser()


def _quote_snapshot_rows(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        rows: list[dict[str, Any]] = []
        for key, item in value.items():
            if isinstance(item, dict):
                row = dict(item)
                row.setdefault("symbol", key)
                rows.append(row)
            else:
                rows.append({"symbol": key, "spot": item})
        return rows
    return []


def _assigned_stock_row_matches(
    row: dict[str, Any],
    *,
    symbol: str | None,
    stock_lot_id: str | None,
    status: str | None,
) -> bool:
    if symbol and str(row.get("symbol") or "").strip().upper() != symbol:
        return False
    if stock_lot_id and str(row.get("stock_lot_id") or "") != stock_lot_id:
        return False
    if status:
        row_status = str(row.get("status") or "").strip().lower()
        if status == "open":
            try:
                shares_remaining = float(row.get("shares_remaining"))
            except Exception:
                shares_remaining = None
            if shares_remaining is not None:
                return shares_remaining > 0
            return row_status in {"open", "partially_sold"}
        if row_status != status:
            return False
    return True


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


def version_update_tool(
    payload: dict[str, Any],
    *,
    update_local_version: Callable[..., dict[str, Any]],
    repo_base: Callable[[], Path],
    mask_path: Callable[[Any], str],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    if _optional_text(payload.get("version")):
        raise AgentToolError(
            code="INPUT_ERROR",
            message="version_update.version has been removed; use target_version",
        )
    target_version = _optional_text(payload.get("target_version"))
    bump = _optional_text(payload.get("bump"))
    apply_mode = bool(payload.get("apply", False))
    allow_downgrade = bool(payload.get("allow_downgrade", False))
    try:
        result = update_local_version(
            base_dir=repo_base(),
            target_version=target_version,
            bump=bump,
            apply=apply_mode,
            allow_downgrade=allow_downgrade,
        )
    except ValueError as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=str(exc),
            hint="Use a semver value like 1.2.3 or a bump value: major, minor, patch.",
        ) from exc

    data = dict(result)
    data["version_path"] = mask_path(data.get("version_path"))
    warnings: list[str] = []
    if not apply_mode and bool(data.get("would_change")):
        warnings.append("dry-run only; pass apply=true to write VERSION")
    return data, warnings, {"repo_base": mask_path(repo_base()), "version_path": data["version_path"]}


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

    default_state_dir = (base / "output_shared" / "state").resolve()
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

    last_run_by_account = state_data.get("last_run_utc_by_account")
    last_notify_by_account = state_data.get("last_notify_utc_by_account")
    data = {
        "decision": decision_payload,
        "state": {
            "state_path": mask_path(state_path),
            "last_run_utc_for_account": (
                last_run_by_account.get(account) if account and isinstance(last_run_by_account, dict) else None
            ),
            "last_notify_utc": state_data.get("last_notify_utc"),
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
    return add_trade_time_beijing({
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
    })


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

    raw_events = list_trade_events()
    trade_events = raw_events if isinstance(raw_events, list) else []
    rows: list[dict[str, Any]] = []
    for event in reversed(trade_events):
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


def _assigned_stock_action(
    repo: Any,
    payload: dict[str, Any],
    *,
    cfg: dict[str, Any],
    repo_base: Callable[[], Path],
    quote_state_base_dir: Path | None,
    normalize_broker: Callable[[Any], str],
    normalize_account: Callable[[Any], str],
    refresh_assigned_stock_quotes: Callable[..., Any],
) -> dict[str, Any]:
    list_trade_events = getattr(repo, "list_trade_events", None)
    raw_trade_events = list_trade_events() if callable(list_trade_events) else []
    trade_events = raw_trade_events if isinstance(raw_trade_events, list) else []
    list_assigned_stock_events = getattr(repo, "list_assigned_stock_events", None)
    raw_assigned_stock_events = list_assigned_stock_events() if callable(list_assigned_stock_events) else []
    assigned_stock_events = raw_assigned_stock_events if isinstance(raw_assigned_stock_events, list) else []

    broker = _optional_text(payload.get("broker"))
    broker = normalize_broker(broker) if broker else None
    account = normalize_account(payload.get("account")) if payload.get("account") else None
    symbol = _optional_text(payload.get("symbol"))
    symbol = symbol.upper() if symbol else None
    stock_lot_id = _optional_text(payload.get("stock_lot_id") or payload.get("target_stock_lot_id"))
    status = _optional_text(payload.get("status"))
    status = status.lower() if status else None
    if status == "all":
        status = None
    elif status in {"close", "closed_sold", "sold"}:
        status = "closed"
    elif status in {"partial", "partially-sold"}:
        status = "partially_sold"
    quote_snapshots = payload.get("quote_snapshots")
    as_of_ms = _optional_int(payload.get("as_of_ms")) if payload.get("as_of_ms") not in (None, "") else None
    refresh_quotes = _bool_flag(payload.get("refresh_quotes"))

    report = build_monthly_income_report(
        [],
        account=account,
        broker=broker,
        trade_events=trade_events,
        assigned_stock_events=assigned_stock_events,
        quote_snapshots=quote_snapshots,
        as_of_ms=as_of_ms,
    )
    selected_report_rows = [
        row
        for row in (report.get("assignment_lifecycle_rows") or [])
        if isinstance(row, dict)
        and _assigned_stock_row_matches(row, symbol=symbol, stock_lot_id=stock_lot_id, status=status)
    ]
    quote_refresh: dict[str, Any] = {"enabled": False}
    quote_refresh_warnings: list[str] = []
    if refresh_quotes:
        if as_of_ms is not None:
            quote_refresh = {
                "enabled": False,
                "status": "skipped_historical_as_of",
                "reason": "historical as-of queries require supplied quote_snapshots or saved marks",
            }
            quote_refresh_warnings.append(
                "refresh_quotes ignored because as_of_ms was provided; historical as-of requires supplied quote_snapshots"
            )
        elif not selected_report_rows:
            quote_refresh = {
                "enabled": False,
                "status": "skipped_no_matching_assigned_stock",
                "reason": "no assigned-stock row matched the query filters",
            }
        else:
            try:
                port_value = payload.get("opend_port") or payload.get("port")
                refresh = refresh_assigned_stock_quotes(
                    selected_report_rows,
                    cfg=cfg,
                    account=account,
                    host=_optional_text(payload.get("opend_host") or payload.get("host")),
                    port=_optional_int(port_value) if port_value not in (None, "") else None,
                    base_dir=repo_base(),
                    state_base_dir=quote_state_base_dir,
                )
            except Exception as exc:
                quote_refresh = {
                    "enabled": True,
                    "status": "source_error",
                    "quote_source": "opend_realtime",
                    "errors": [{"error_code": type(exc).__name__, "message": str(exc)}],
                }
                quote_refresh_warnings.append(f"assigned stock quote refresh failed: {type(exc).__name__}: {exc}")
            else:
                quote_refresh = dict(getattr(refresh, "diagnostics", {}) or {})
                quote_refresh.setdefault("enabled", True)
                quote_refresh_warnings.extend(
                    str(item) for item in (getattr(refresh, "warnings", []) or []) if str(item).strip()
                )
                refreshed_snapshots = list(getattr(refresh, "quote_snapshots", []) or [])
                if refreshed_snapshots:
                    quote_snapshots = [*_quote_snapshot_rows(payload.get("quote_snapshots")), *refreshed_snapshots]
                    report = build_monthly_income_report(
                        [],
                        account=account,
                        broker=broker,
                        trade_events=trade_events,
                        assigned_stock_events=assigned_stock_events,
                        quote_snapshots=quote_snapshots,
                        as_of_ms=as_of_ms,
                    )
                    selected_report_rows = [
                        row
                        for row in (report.get("assignment_lifecycle_rows") or [])
                        if isinstance(row, dict)
                        and _assigned_stock_row_matches(
                            row,
                            symbol=symbol,
                            stock_lot_id=stock_lot_id,
                            status=status,
                        )
                    ]
    rows: list[dict[str, Any]] = []
    for row in selected_report_rows:
        rows.append(row)
    return {
        "action": "assigned-stock",
        "rows": rows,
        "row_count": len(rows),
        "assigned_stock_lots": rows,
        "assigned_stock_sale_rows": report.get("assigned_stock_sale_rows") or [],
        "assigned_stock_review_rows": report.get("assigned_stock_review_rows") or [],
        "filters": {
            "broker": broker,
            "account": account,
            "symbol": symbol,
            "stock_lot_id": stock_lot_id,
            "status": status,
            "as_of_ms": as_of_ms,
            "refresh_quotes": refresh_quotes,
        },
        "quote_refresh": quote_refresh,
        "warnings": quote_refresh_warnings,
    }


def option_positions_read_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config: Callable[..., tuple[Path, dict[str, Any]]],
    resolve_public_data_config_path: Callable[[dict[str, Any], dict[str, Any]], Path],
    normalize_broker: Callable[[Any], str],
    normalize_account: Callable[[Any], str],
    refresh_assigned_stock_quotes: Callable[..., Any],
    resolve_option_positions_repo: Callable[..., tuple[Path, Any]],
    list_position_rows: Callable[..., list[dict[str, Any]]],
    build_lot_event_history: Callable[..., list[dict[str, Any]]],
    inspect_projection_state: Callable[..., dict[str, Any]],
    repo_base: Callable[[], Path],
    mask_path: Callable[[Any], str],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    action = str(payload.get("action") or "list").strip().lower()
    if action not in {"list", "events", "history", "inspect", "assigned-stock"}:
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported option_positions_read action: {action}")

    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    portfolio_raw = cfg.get("portfolio")
    portfolio_cfg = cast(dict[str, Any], portfolio_raw) if isinstance(portfolio_raw, dict) else {}
    data_config_path = resolve_public_data_config_path(payload, portfolio_cfg)
    _resolved_data_config, repo = resolve_option_positions_repo(base=repo_base(), data_config=data_config_path)
    ledger_store = ledger_store_payload(data_config_path, repo)

    warnings: list[str] = []
    bootstrap_status = getattr(repo, "bootstrap_status", None)
    bootstrap_message = getattr(repo, "bootstrap_message", None)
    if bootstrap_status and str(bootstrap_status).startswith("degraded"):
        warnings.append(str(bootstrap_message or bootstrap_status))

    data: dict[str, Any]
    if action == "list":
        query = _dict(payload.get("query"))
        expiration_query = _dict(query.get("expiration"))
        broker = normalize_broker(payload.get("broker") or portfolio_cfg.get("broker") or "富途")
        account = _optional_text(query.get("account") if "account" in query else payload.get("account"))
        status = str(query.get("status") if "status" in query else payload.get("status") or "open").strip().lower()
        if status == "closed":
            status = "close"
        if status not in {"open", "close", "all"}:
            raise AgentToolError(code="INPUT_ERROR", message="status must be one of: open, close, all")
        limit = _as_int(query.get("limit") if "limit" in query else payload.get("limit"), default=50)
        expiration_within_days = _optional_int(
            expiration_query.get("within_days")
            or payload.get("exp_within_days")
            or payload.get("expiration_within_days")
        )
        symbol = _optional_text(query.get("symbol") if "symbol" in query else payload.get("symbol"))
        option_type = _optional_text(query.get("option_type") if "option_type" in query else payload.get("option_type"))
        side = _optional_text(query.get("side") if "side" in query else payload.get("side"))
        strike = _optional_float(query.get("strike") if "strike" in query else payload.get("strike"))
        expiration_exact = _optional_text(expiration_query.get("exact") or payload.get("expiration_exact"))
        expiration_month = _optional_text(expiration_query.get("month") or payload.get("expiration_month"))
        expiration_before = _optional_text(expiration_query.get("before") or payload.get("expiration_before"))
        expiration_after = _optional_text(expiration_query.get("after") or payload.get("expiration_after"))
        rows = list_position_rows(
            repo,
            broker=broker,
            account=account,
            status=status,
            limit=limit,
            expiration_within_days=expiration_within_days,
            symbol=symbol,
            option_type=option_type,
            side=side,
            strike=strike,
            expiration_exact=expiration_exact,
            expiration_month=expiration_month,
            expiration_before=expiration_before,
            expiration_after=expiration_after,
        )
        effective_query = {
            "account": normalize_account(account) if account else None,
            "status": status,
            "symbol": symbol,
            "option_type": option_type,
            "side": side,
            "strike": strike,
            "expiration": {
                "exact": expiration_exact,
                "month": expiration_month,
                "before": expiration_before,
                "after": expiration_after,
                "within_days": expiration_within_days,
            },
            "limit": limit,
        }
        data = {
            "action": action,
            "rows": rows,
            "row_count": len(rows),
            "filters": {
                "broker": broker,
                "query": effective_query,
                "account": effective_query["account"],
                "status": status,
                "limit": limit,
                "expiration_within_days": expiration_within_days,
            },
        }
    elif action == "events":
        event_data = _events_action(repo, payload, normalize_broker=normalize_broker, normalize_account=normalize_account)
        data = {"action": action, **event_data}
    elif action == "assigned-stock":
        data = _assigned_stock_action(
            repo,
            payload,
            cfg=cfg,
            repo_base=repo_base,
            quote_state_base_dir=_optional_path(ledger_store.get("runtime_root")),
            normalize_broker=normalize_broker,
            normalize_account=normalize_account,
            refresh_assigned_stock_quotes=refresh_assigned_stock_quotes,
        )
    elif action == "history":
        record_id = _optional_text(payload.get("record_id"))
        if not record_id:
            raise AgentToolError(code="INPUT_ERROR", message="record_id is required for option_positions_read history")
        try:
            history = build_lot_event_history(repo, base=repo_base(), record_id=record_id)
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
            "account": _optional_text(payload.get("account")),
            "symbol": _optional_text(payload.get("symbol")),
            "option_type": _optional_text(payload.get("option_type")),
            "strike": _optional_float(payload.get("strike")),
            "expiration_ymd": _optional_text(payload.get("exp") or payload.get("expiration_ymd")),
        }
        if not any(value not in (None, "") for value in selectors.values()):
            raise AgentToolError(code="INPUT_ERROR", message="inspect requires at least one selector")
        inspected = inspect_projection_state(repo, base=repo_base(), **selectors)
        data = {"action": action, **inspected}

    data_warnings = data.get("warnings")
    if isinstance(data_warnings, list):
        warnings.extend(str(item) for item in data_warnings if str(item).strip())

    data["bootstrap"] = {
        "status": bootstrap_status,
        "message": bootstrap_message,
    }
    return data, warnings, {
        "config_path": mask_path(config_path),
        "data_config": mask_path(data_config_path),
        "ledger_store": ledger_store,
    }
