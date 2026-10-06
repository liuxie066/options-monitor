from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable
import json

from src.application.agent_tool_contracts import AgentToolError
from domain.domain.ledger.position_fields import normalize_account
from domain.domain.performance.period import (
    PeriodRequest,
    PeriodWindow,
    normalize_performance_period,
)
from domain.domain.strategy_vocab import (
    STRATEGY_COMBO_YIELD,
    STRATEGY_COVERED_CALL,
    STRATEGY_SELL_PUT,
    canonical_strategy_id,
)
from src.application.account_config import accounts_from_config
from src.application.performance.adapters import (
    assigned_stock_instruments,
    load_assigned_stock_projection,
    load_ledger_performance_inputs,
    load_option_valuation_inputs,
)
from src.application.performance.evidence_collection import collect_current_performance_evidence


def scan_summary_rows(summary_rows: list[dict[str, Any]], *, as_float: Callable[[Any], float | None]) -> dict[str, Any]:
    strategy_counts = {STRATEGY_SELL_PUT: 0, STRATEGY_COVERED_CALL: 0, STRATEGY_COMBO_YIELD: 0}
    account_counts: dict[str, int] = {}
    symbol_counts: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []
    for row in summary_rows:
        if not isinstance(row, dict):
            continue
        raw_strategy = str(row.get("side") or row.get("strategy") or row.get("option_strategy") or "").strip()
        strategy = canonical_strategy_id(raw_strategy) if raw_strategy else ""
        if strategy in strategy_counts:
            strategy_counts[strategy] += 1
        account = normalize_account(row.get("account") or row.get("account_label"))
        if account:
            account_counts[account] = account_counts.get(account, 0) + 1
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        candidates.append(
            {
                "symbol": symbol or None,
                "account": account or None,
                "strategy": strategy or None,
                "net_income": as_float(row.get("net_income")),
                "annualized_return": as_float(
                    row.get("annualized_net_return") or row.get("annualized_return") or row.get("annualized")
                ),
                "strike": as_float(row.get("strike")),
                "expiration": (str(row.get("expiration") or "").strip() or None),
            }
        )
    top_candidates = sorted(
        candidates,
        key=lambda item: (
            -(item["net_income"] if item["net_income"] is not None else -(10**12)),
            -(item["annualized_return"] if item["annualized_return"] is not None else -(10**12)),
        ),
    )[:5]
    return {
        "row_count": len(summary_rows),
        "symbol_count": len(symbol_counts),
        "strategy_counts": strategy_counts,
        "account_counts": account_counts,
        "top_candidates": top_candidates,
    }


def query_cash_headroom_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config,
    resolve_public_data_config_path,
    normalize_broker,
    resolve_output_root,
    query_sell_put_cash,
    repo_base,
    mask_path,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    data_config_path = resolve_public_data_config_path(payload, portfolio_cfg)
    broker = normalize_broker(payload.get("broker") or portfolio_cfg.get("broker"))
    out_root = resolve_output_root(payload.get("output_dir"))
    out_dir = (out_root / "query_cash_headroom").resolve()
    result = query_sell_put_cash(
        config=str(config_path),
        data_config=str(data_config_path),
        market=broker,
        account=(str(payload.get("account")).strip() if payload.get("account") else None),
        output_format="json",
        top=int(payload.get("top") or 10),
        no_exchange_rates=bool(payload.get("no_exchange_rates", False)),
        out_dir=str(out_dir),
        base_dir=repo_base(),
        runtime_config=cfg,
        write_cache=False,
    )
    return result, [], {"config_path": mask_path(config_path), "output_dir": mask_path(out_dir)}


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
        raise AgentToolError(
            "INPUT_ERROR",
            f"option_performance_report does not accept: {', '.join(extras)}",
        )
    config_key = str(payload.get("config_key") or "us").strip().lower()
    if config_key not in {"us", "hk"}:
        raise AgentToolError("INPUT_ERROR", "config_key must be us or hk")
    include_rows = payload.get("include_rows")
    if include_rows is not None and not isinstance(include_rows, bool):
        raise AgentToolError("INPUT_ERROR", "include_rows must be a boolean")
    try:
        period_request = PeriodRequest.from_mapping(
            {name: payload[name] for name in ("period", "as_of_date", "month", "year") if name in payload}
        )
        window = normalize_performance_period(
            period_request,
            report_now_ms=now_ms,
        )
    except ValueError as exc:
        raise AgentToolError("INPUT_ERROR", str(exc)) from exc

    raw_account = str(payload.get("account") or "").strip()
    account = normalize_account(raw_account) if raw_account else None
    if raw_account and not account:
        raise AgentToolError("INPUT_ERROR", "account is invalid")
    raw_broker = str(payload.get("broker") or "").strip()
    broker = normalize_broker(raw_broker) if raw_broker else None
    if raw_broker and not broker:
        raise AgentToolError("INPUT_ERROR", "broker is invalid")
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
    from src.application.agent_tools.project_reader import ProjectReaderError, decode_cursor, digest

    view = str(
        request.get("view")
        or ("breakdowns" if any(name in request for name in ("group_by", "symbol", "limit", "cursor")) else "summary")
    )
    group = str(request.get("group_by") or "symbols")
    limit = request.get("limit", 20)
    if view not in {"summary", "breakdowns", "rows"} or group not in _PERFORMANCE_GROUPS:
        raise AgentToolError("INPUT_ERROR", "Choose view=summary|breakdowns|rows and a declared group_by.")
    if type(limit) is not int or not 1 <= limit <= 40:
        raise AgentToolError("INPUT_ERROR", "limit must be between 1 and 40")
    if view == "summary" and any(name in request for name in ("group_by", "symbol", "limit", "cursor")):
        raise AgentToolError("INPUT_ERROR", "Summary has no filters; use view=breakdowns or rows.")
    if view == "rows" and "group_by" in request:
        raise AgentToolError("INPUT_ERROR", "group_by requires view=breakdowns")
    symbol = str(request.get("symbol") or "").strip().upper() or None
    if symbol and view == "breakdowns" and group != "symbols":
        raise AgentToolError("INPUT_ERROR", "symbol filters require group_by=symbols or view=rows")
    query = {name: value for name, value in request.items() if name not in {"cursor", "include_rows"}}
    query.update(view=view, group_by=group if view == "breakdowns" else None, symbol=symbol, limit=limit)
    binding = digest({"tool": "option_performance_report", "query": query})
    try:
        state = decode_cursor(request.get("cursor"), binding)
    except ProjectReaderError as exc:
        raise AgentToolError("INPUT_ERROR", exc.code) from exc
    return {"view": view, "group_by": group, "symbol": symbol, "limit": limit, "binding": binding}, state


def _page_option_performance(
    data: dict[str, Any], query: dict[str, Any], state: dict[str, Any] | None, *, report_now_ms: int
) -> dict[str, Any]:
    from src.application.agent_tools.project_reader import ProjectReaderError, digest, page_text, set_continuation

    view, group = query["view"], query["group_by"]
    if view == "summary":
        result = {**data, "view": "summary", "available_breakdowns": list(data.get("breakdowns") or {})}
        if len(json.dumps(result, ensure_ascii=False).encode()) > 6000:
            # Keep whole existing groups when small; never truncate a financial row.
            breakdowns = result.pop("breakdowns", {})
            result["breakdowns"] = {}
            for name in (
                "currencies",
                "leg_types",
                "accounts",
                "symbols",
                "opening_months",
                "opening_years",
                "attribution_strategies",
                "parent_universes",
            ):
                candidate = {**result["breakdowns"], name: breakdowns.get(name, [])}
                if len(json.dumps({**result, "breakdowns": candidate}, ensure_ascii=False).encode()) <= 6000:
                    result["breakdowns"] = candidate
            result["detail_query"] = {"view": "breakdowns", "group_by": "symbols", "limit": 20}
        return result
    all_rows = list(data.get("rows") or []) if view == "rows" else list((data.get("breakdowns") or {}).get(group) or [])
    rows = [
        row
        for row in all_rows
        if not query["symbol"] or str(row.get("symbol" if view == "rows" else "key") or "").upper() == query["symbol"]
    ]
    source_hash = digest(
        {
            "ledger_input_hash": data["quality"]["ledger_input_hash"],
            "period": data["period"],
            "scope": data["scope"],
            "rows": all_rows,
        }
    )
    if state and state.get("hash") != source_hash:
        raise AgentToolError(
            "READ_ERROR", "source_changed", hint="Discard the old cursor and query the updated ledger."
        )
    offset = state.get("offset", 0) if state else 0
    if type(offset) is not int or not 0 <= offset <= len(rows):
        raise AgentToolError("INPUT_ERROR", "cursor_invalidated")
    end = min(len(rows), offset + query["limit"])
    result = {name: data[name] for name in ("period", "scope", "freshness", "quality")}
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
    while True:
        selected = rows[offset:end]
        result["rows"] = selected
        result["breakdowns"] = {group: selected} if view == "breakdowns" else {}
        if len(json.dumps(result, ensure_ascii=False).encode()) <= 5000 or end <= offset + 1:
            break
        end -= 1
    next_body = None
    if selected and len(json.dumps(result, ensure_ascii=False).encode()) > 5000:
        try:
            fragment = page_text(
                json.dumps(selected[0], ensure_ascii=False, sort_keys=True).encode(),
                relative_name=view + "/" + str(offset),
                resource="option_performance_report",
                scope={**result["scope"], "source_hash": source_hash},
                cursor=state.get("body_cursor") if state else None,
            )
        except ProjectReaderError as exc:
            raise AgentToolError("READ_ERROR", exc.code) from exc
        result.update({name: fragment[name] for name in ("text", "body_range", "body_complete")})
        result.update(rows=[], breakdowns={})
        next_body = fragment.get("next_cursor")
        if not fragment["body_complete"]:
            end = offset
        if fragment.get("continuation_status"):
            result["continuation_status"] = fragment["continuation_status"]
    has_more = end < len(rows)
    result["pagination"] = {
        "total_count": len(all_rows),
        "matched_count": len(rows),
        "returned_count": len(result["rows"]),
        "scanned_count": len(all_rows),
        "has_more": has_more,
    }
    result["coverage"] = {
        "status": "complete",
        "complete_for": "point" if "text" in result else "requested_page",
        "included_count": len(result["rows"]),
        "total_count": len(rows),
        "omitted_count": len(rows) - len(result["rows"]),
        "has_more": has_more,
    }
    try:
        next_state = (
            {
                "binding": query["binding"],
                "hash": source_hash,
                "offset": end,
                "report_now_ms": report_now_ms,
                "body_cursor": next_body,
            }
            if has_more
            else None
        )
        if result.get("continuation_status"):
            result["next_cursor"] = None
            result["coverage"]["status"] = "partial"
        else:
            set_continuation(result, next_state)
    except ProjectReaderError as exc:
        raise AgentToolError("READ_ERROR", exc.code) from exc
    result["pagination"]["next_cursor"] = result.get("next_cursor")
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
            raise AgentToolError("INPUT_ERROR", "cursor_invalidated")
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
        raise AgentToolError(
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
        raise AgentToolError(
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


def capture_option_performance_evidence(
    payload: dict[str, Any],
    *,
    apply: bool,
    load_runtime_config,
    resolve_public_data_config_path,
    normalize_broker,
    resolve_option_positions_repo,
    open_performance_evidence_repository,
    repo_base,
    mask_path,
    now_ms: int | None = None,
    evidence_collector=collect_current_performance_evidence,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    report_payload = {
        "config_key": payload.get("config_key") or "us",
        "config_path": payload.get("config_path"),
        "data_config": payload.get("data_config"),
        "account": payload.get("account"),
        "broker": payload.get("broker"),
        "period": "mtd",
        "include_rows": False,
    }
    request, window = normalize_option_performance_request(
        report_payload,
        normalize_broker=normalize_broker,
        now_ms=now_ms,
    )
    if window.status != "partial_current":
        raise AgentToolError("INVALID_ARGUMENT", "evidence capture only supports the current period")
    config_path, cfg = load_runtime_config(
        config_key=request["config_key"],
        config_path=request.get("config_path"),
    )
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    data_config_path = resolve_public_data_config_path(request, portfolio_cfg)
    _resolved_data_config, repo = resolve_option_positions_repo(base=repo_base(), data_config=data_config_path)
    evidence_repo = open_performance_evidence_repository(repo)
    inputs = load_ledger_performance_inputs(repo)
    ending = load_option_valuation_inputs(
        inputs,
        as_of_ms=window.valuation_end_at_ms,
        account=request.get("account"),
        broker=request.get("broker"),
    )
    existing = evidence_repo.read_all()
    ending_assigned_stock = load_assigned_stock_projection(
        inputs,
        as_of_ms=window.valuation_end_at_ms,
        valuation_marks=existing.valuation_marks,
        account=request.get("account"),
        broker=request.get("broker"),
    )
    collection = evidence_collector(
        period_status=window.status,
        refresh_quotes=True,
        option_positions=ending.positions,
        stock_instruments=assigned_stock_instruments(ending_assigned_stock),
        now_ms=int(now_ms if now_ms is not None else window.valuation_end_at_ms),
        cfg=cfg,
        base_dir=config_path.parent,
    )
    migrated_at_ms = int(now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
    imported = evidence_repo.import_envelope(
        collection.envelope,
        apply=bool(apply),
        migrated_at_ms=migrated_at_ms,
    )
    data = imported.to_dict()
    data["schema_version"] = "option_performance_evidence_capture.output.v1"
    data["dry_run"] = not bool(apply)
    data["collection"] = collection.to_dict()
    data["scope"] = {
        "config_key": request["config_key"],
        "account": request.get("account"),
        "broker": request.get("broker"),
    }
    return (
        data,
        [],
        {
            "config_path": mask_path(config_path),
            "data_config": mask_path(data_config_path),
        },
    )


def get_portfolio_context_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config,
    resolve_public_data_config_path,
    normalize_broker,
    resolve_output_root,
    load_portfolio_context,
    repo_base,
    mask_path,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    if "ttl_sec" in payload:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="现金有效期统一使用 runtime.portfolio_context_ttl_sec；get_portfolio_context 不再接受 ttl_sec。",
        )
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    account = str(payload.get("account") or portfolio_cfg.get("account") or "").strip() or None
    broker = normalize_broker(payload.get("broker") or portfolio_cfg.get("broker"))
    data_config = str(resolve_public_data_config_path(payload, portfolio_cfg))
    out_root = resolve_output_root(payload.get("output_dir"))
    state_dir = (out_root / "portfolio_context_state").resolve()
    shared_dir = (out_root / "shared").resolve()
    logs: list[str] = []
    ctx = load_portfolio_context(
        base=repo_base(),
        data_config=data_config,
        market=broker,
        account=account,
        state_dir=state_dir,
        shared_state_dir=shared_dir,
        log=logs.append,
        runtime_config=cfg,
    )
    if not isinstance(ctx, dict):
        raise AgentToolError(
            code="DEPENDENCY_MISSING", message="portfolio context is unavailable", details={"logs": logs[-5:]}
        )
    warnings = [item for item in logs if item.startswith("[WARN]")]
    return ctx, warnings, {"config_path": mask_path(config_path), "state_dir": mask_path(state_dir)}


def scan_opportunities_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config,
    resolve_data_config_ref,
    resolve_output_root,
    repo_base,
    load_config,
    run_watchlist_pipeline_default,
    scan_summary_rows_fn,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))

    def _log(_msg: str) -> None:
        return None

    out_root = resolve_output_root(payload.get("output_dir"))
    report_dir = (out_root / "reports").resolve()
    state_dir = (out_root / "state").resolve()
    shared_state_dir = (out_root / "shared").resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    shared_state_dir.mkdir(parents=True, exist_ok=True)

    cfg_loaded = load_config(
        base=repo_base(), config_path=config_path, is_scheduled=False, log=_log, state_dir=state_dir
    )
    if isinstance(cfg.get("portfolio"), dict):
        cfg_loaded["portfolio"] = deepcopy(cfg["portfolio"])
    if isinstance(cfg_loaded.get("portfolio"), dict):
        data_config_ref = resolve_data_config_ref(payload, cfg_loaded["portfolio"])
        if data_config_ref:
            cfg_loaded["portfolio"]["data_config"] = data_config_ref

    top_n = int(payload.get("top_n") or (cfg_loaded.get("outputs", {}) or {}).get("top_n_alerts", 3) or 3)
    runtime = cfg_loaded.get("runtime", {}) or {}
    raw_symbols = payload.get("symbols")
    symbols_arg = (
        ",".join(str(item) for item in raw_symbols)
        if isinstance(raw_symbols, list)
        else (str(raw_symbols) if raw_symbols is not None else None)
    )
    summary_rows = run_watchlist_pipeline_default(
        py=str((repo_base() / ".venv" / "bin" / "python").resolve()),
        base=repo_base(),
        cfg=cfg_loaded,
        report_dir=report_dir,
        state_dir=state_dir,
        shared_state_dir=shared_state_dir,
        required_data_dir=out_root,
        is_scheduled=False,
        top_n=top_n,
        symbol_timeout_sec=int(payload.get("symbol_timeout_sec") or runtime.get("symbol_timeout_sec", 120) or 120),
        portfolio_timeout_sec=int(
            payload.get("portfolio_timeout_sec") or runtime.get("portfolio_timeout_sec", 60) or 60
        ),
        want_scan=True,
        no_context=bool(payload.get("no_context", False)),
        symbols_arg=symbols_arg,
        log=_log,
        want_fn=lambda _step: True,
    )
    summary = scan_summary_rows_fn(summary_rows)
    return (
        {
            "summary_rows": summary_rows,
            "symbol_count": len(
                {str(r.get("symbol") or "").strip() for r in summary_rows if str(r.get("symbol") or "").strip()}
            ),
            "row_count": len(summary_rows),
            "summary": summary,
            "top_candidates": summary["top_candidates"],
        },
        [],
        {"config_path": str(config_path), "report_dir": str(report_dir)},
    )
