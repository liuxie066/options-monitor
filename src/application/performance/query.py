from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from src.application.performance.query_contracts import PerformanceQueryError
from src.application.query_cursor import CursorError, decode_cursor, digest, encode_cursor
from domain.domain.ledger.position_fields import normalize_account
from domain.domain.performance.period import PeriodRequest, PeriodWindow, normalize_performance_period
from src.application.account_config import accounts_from_config

_OPTION_PERFORMANCE_INPUT_FIELDS = frozenset(
    {
        "config_key",
        "config_path",
        "data_config",
        "account",
        "broker",
        "period",
        "as_of_date",
        "month",
        "year",
        "include_rows",
        "view",
        "group_by",
        "symbol",
        "limit",
        "cursor",
    }
)


def normalize_option_performance_request(
    payload: dict[str, Any],
    *,
    normalize_broker,
    now_ms: int | None = None,
) -> tuple[dict[str, Any], PeriodWindow]:
    extras = sorted(str(key) for key in payload if key not in _OPTION_PERFORMANCE_INPUT_FIELDS)
    if extras:
        raise PerformanceQueryError(
            "INPUT_ERROR",
            f"option_performance_report does not accept: {', '.join(extras)}",
        )
    config_key = str(payload.get("config_key") or "us").strip().lower()
    if config_key not in {"us", "hk"}:
        raise PerformanceQueryError("INPUT_ERROR", "config_key must be us or hk")
    include_rows = payload.get("include_rows")
    if include_rows is not None and not isinstance(include_rows, bool):
        raise PerformanceQueryError("INPUT_ERROR", "include_rows must be a boolean")
    try:
        period_request = PeriodRequest.from_mapping(
            {name: payload[name] for name in ("period", "as_of_date", "month", "year") if name in payload}
        )
        window = normalize_performance_period(
            period_request,
            report_now_ms=now_ms,
        )
    except ValueError as exc:
        raise PerformanceQueryError("INPUT_ERROR", str(exc)) from exc

    raw_account = str(payload.get("account") or "").strip()
    account = normalize_account(raw_account) if raw_account else None
    if raw_account and not account:
        raise PerformanceQueryError("INPUT_ERROR", "account is invalid")
    raw_broker = str(payload.get("broker") or "").strip()
    broker = normalize_broker(raw_broker) if raw_broker else None
    if raw_broker and not broker:
        raise PerformanceQueryError("INPUT_ERROR", "broker is invalid")
    normalized = {
        "config_key": config_key,
        "config_path": payload.get("config_path"),
        "data_config": payload.get("data_config"),
        "account": account,
        "broker": broker,
        "period": period_request.period,
        "as_of_date": period_request.as_of_date,
        "month": period_request.month,
        "year": period_request.year,
        "include_rows": bool(include_rows),
    }
    normalized.update(
        {name: payload[name] for name in ("view", "group_by", "symbol", "limit", "cursor") if name in payload}
    )
    return normalized, window


_OPTION_PERFORMANCE_REPORT_NOW_MS: ContextVar[int | None] = ContextVar(
    "option_performance_report_now_ms",
    default=None,
)


@contextmanager
def option_performance_report_now_ms(now_ms: int):
    token = _OPTION_PERFORMANCE_REPORT_NOW_MS.set(int(now_ms))
    try:
        yield
    finally:
        _OPTION_PERFORMANCE_REPORT_NOW_MS.reset(token)


_PERFORMANCE_GROUPS = (
    "opening_years",
    "opening_months",
    "accounts",
    "currencies",
    "leg_types",
    "attribution_strategies",
    "parent_universes",
    "symbols",
)


def _performance_page_request(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    view = str(
        request.get("view")
        or ("breakdowns" if any(name in request for name in ("group_by", "symbol", "limit", "cursor")) else "summary")
    )
    group = str(request.get("group_by") or "symbols")
    limit = request.get("limit", 20)
    if view not in {"summary", "breakdowns", "rows"} or group not in _PERFORMANCE_GROUPS:
        raise PerformanceQueryError("INPUT_ERROR", "Choose view=summary|breakdowns|rows and a declared group_by.")
    if type(limit) is not int or not 1 <= limit <= 40:
        raise PerformanceQueryError("INPUT_ERROR", "limit must be between 1 and 40")
    if view == "summary" and any(name in request for name in ("group_by", "symbol", "limit", "cursor")):
        raise PerformanceQueryError("INPUT_ERROR", "Summary has no filters; use view=breakdowns or rows.")
    if view == "rows" and "group_by" in request:
        raise PerformanceQueryError("INPUT_ERROR", "group_by requires view=breakdowns")
    symbol = str(request.get("symbol") or "").strip().upper() or None
    if symbol and view == "breakdowns" and group != "symbols":
        raise PerformanceQueryError("INPUT_ERROR", "symbol filters require group_by=symbols or view=rows")
    query = {name: value for name, value in request.items() if name not in {"cursor", "include_rows"}}
    query.update(view=view, group_by=group if view == "breakdowns" else None, symbol=symbol, limit=limit)
    binding = digest({"tool": "option_performance_report", "query": query})
    try:
        state = decode_cursor(request.get("cursor"), binding)
        if state and state.get("body_cursor") is not None:
            raise CursorError("cursor_invalidated")
    except CursorError as exc:
        raise PerformanceQueryError("INPUT_ERROR", exc.code) from exc
    return {"view": view, "group_by": group, "symbol": symbol, "limit": limit, "binding": binding}, state


def _page_option_performance(
    data: dict[str, Any], query: dict[str, Any], state: dict[str, Any] | None, *, report_now_ms: int
) -> dict[str, Any]:
    view, group = query["view"], query["group_by"]
    if view == "summary":
        return {**data, "view": "summary", "available_breakdowns": list(data.get("breakdowns") or {})}
    all_rows = list(data.get("rows") or []) if view == "rows" else list((data.get("breakdowns") or {}).get(group) or [])
    rows = [
        row
        for row in all_rows
        if not query["symbol"] or str(row.get("symbol" if view == "rows" else "key") or "").upper() == query["symbol"]
    ]
    source_hash = digest(
        {
            "ledger_input_hash": data["quality"]["ledger_input_hash"],
            "schema_version": data.get("schema_version"),
            "metric_semantics": data.get("metric_semantics"),
            "period": data["period"],
            "scope": data["scope"],
            "rows": all_rows,
        }
    )
    if state and state.get("hash") != source_hash:
        raise PerformanceQueryError(
            "READ_ERROR", "source_changed", hint="Discard the old cursor and query the updated ledger."
        )
    offset = state.get("offset", 0) if state else 0
    if type(offset) is not int or not 0 <= offset <= len(rows):
        raise PerformanceQueryError("INPUT_ERROR", "cursor_invalidated")
    end = min(len(rows), offset + query["limit"])
    result = {name: data[name] for name in ("period", "scope", "freshness", "quality")}
    result.update({name: data[name] for name in ("schema_version", "metric_semantics") if name in data})
    result.update(
        view=view,
        group_by=group if view == "breakdowns" else None,
        source={
            "label": "OM canonical option performance",
            "content_hash": source_hash,
            "ledger_input_hash": data["quality"]["ledger_input_hash"],
        },
    )
    result["scope"] = {**data["scope"], "view": view, "group_by": result["group_by"], "symbol": query["symbol"]}
    selected = rows[offset:end]
    result["rows"] = selected
    result["breakdowns"] = {group: selected} if view == "breakdowns" else {}
    has_more = end < len(rows)
    result["pagination"] = {
        "offset": offset,
        "total_count": len(all_rows),
        "matched_count": len(rows),
        "returned_count": len(result["rows"]),
        "scanned_count": len(all_rows),
        "has_more": has_more,
    }
    result["coverage"] = {
        "status": "complete",
        "complete_for": "requested_page",
        "included_count": len(result["rows"]),
        "total_count": len(rows),
        "omitted_count": len(rows) - len(result["rows"]),
        "has_more": has_more,
    }
    def continuation(at: int) -> str | None:
        try:
            return encode_cursor({"binding": query["binding"], "hash": source_hash,
                                  "offset": at, "report_now_ms": report_now_ms})
        except CursorError as exc:
            if exc.code != "continuation_unavailable":
                raise PerformanceQueryError("READ_ERROR", exc.code) from exc
            return None

    result["pagination"]["page_cursor"] = continuation(offset)
    result["next_cursor"] = continuation(end) if has_more else None
    if has_more and result["next_cursor"] is None:
        result["continuation_status"] = "continuation_unavailable"
        result["coverage"].update(status="partial", complete=False)
    result["pagination"]["next_cursor"] = result["next_cursor"]
    return result


def option_performance_report_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config,
    resolve_public_data_config_path,
    normalize_broker,
    resolve_option_positions_repo,
    build_option_period_performance,
    repo_base,
    mask_path,
    now_ms: int | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.performance.service import OptionPerformanceReadError

    contextual_now_ms = _OPTION_PERFORMANCE_REPORT_NOW_MS.get()
    report_now_ms = int(
        now_ms
        if now_ms is not None
        else contextual_now_ms
        if contextual_now_ms is not None
        else datetime.now(timezone.utc).timestamp() * 1000
    )
    request, window = normalize_option_performance_request(
        payload,
        normalize_broker=normalize_broker,
        now_ms=report_now_ms,
    )
    paged = any(name in payload for name in ("view", "group_by", "symbol", "limit", "cursor"))
    query, page_state = _performance_page_request(request) if paged else ({}, None)
    if page_state:
        instant = page_state.get("report_now_ms")
        if type(instant) is not int or instant <= 0 or instant > report_now_ms:
            raise PerformanceQueryError("INPUT_ERROR", "cursor_invalidated")
        report_now_ms = instant
        request, window = normalize_option_performance_request(
            payload, normalize_broker=normalize_broker, now_ms=instant
        )
    if paged and query["view"] == "rows":
        request["include_rows"] = True
    try:
        config_path, cfg = load_runtime_config(
            config_key=request["config_key"],
            config_path=request.get("config_path"),
        )
        portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
        data_config_path = resolve_public_data_config_path(request, portfolio_cfg)
        _resolved_data_config, repo = resolve_option_positions_repo(
            base=repo_base(),
            data_config=data_config_path,
        )
    except Exception as exc:
        raise PerformanceQueryError(
            code="READ_ERROR",
            message="option performance ledger input is unavailable",
            details={"reason_codes": ["ledger_read_failed"]},
        ) from exc
    try:
        data = build_option_period_performance(
            repo,
            period=window,
            config_key=request["config_key"],
            configured_accounts=accounts_from_config(cfg, fallback=()),
            account=request.get("account"),
            broker=request.get("broker"),
            include_rows=request["include_rows"],
        )
    except OptionPerformanceReadError as exc:
        raise PerformanceQueryError(
            code="READ_ERROR",
            message="option performance ledger input is unavailable",
            details={"reason_codes": list(exc.reason_codes)},
        ) from exc
    if paged:
        data = _page_option_performance(data, query, page_state, report_now_ms=report_now_ms)
    return (
        data,
        [],
        {
            "config_path": mask_path(config_path),
            "data_config": mask_path(data_config_path),
            "freshness_status": data["period"]["freshness_status"],
        },
    )
