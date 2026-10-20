from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.symbol_mutations import split_symbol_whitelist
from src.application.performance.query_contracts import PerformanceQueryError
from domain.domain.ledger.position_fields import normalize_account
from domain.domain.performance.period import (
    normalize_performance_period,
)
from domain.domain.strategy_vocab import (
    STRATEGY_COMBO_YIELD,
    STRATEGY_COVERED_CALL,
    STRATEGY_SELL_PUT,
    canonical_strategy_id,
)
from src.application.performance.adapters import (
    assigned_stock_instruments,
    load_assigned_stock_projection,
    load_ledger_performance_inputs,
    load_option_valuation_inputs,
)
from src.application.performance.evidence_collection import collect_current_performance_evidence
from src.application.performance.query import (
    _OPTION_PERFORMANCE_REPORT_NOW_MS as _OPTION_PERFORMANCE_REPORT_NOW_MS,
    normalize_option_performance_request as normalize_option_performance_request,
    option_performance_report_now_ms as option_performance_report_now_ms,
    option_performance_report_tool as query_local_option_performance,
)


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
    url = os.environ.get("OM_PERFORMANCE_SERVICE_URL", "").strip()
    if url:
        from src.infrastructure.option_performance_client import query_option_performance

        return query_option_performance(payload, url=url, token=os.environ.get("OM_PERFORMANCE_SERVICE_TOKEN", ""))
    return query_local_option_performance(
        payload, load_runtime_config=load_runtime_config,
        resolve_public_data_config_path=resolve_public_data_config_path,
        normalize_broker=normalize_broker, resolve_option_positions_repo=resolve_option_positions_repo,
        build_option_period_performance=build_option_period_performance,
        repo_base=repo_base, mask_path=mask_path, now_ms=now_ms,
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
    try:
        request, window = normalize_option_performance_request(
            report_payload, normalize_broker=normalize_broker, now_ms=now_ms,
        )
    except PerformanceQueryError as exc:
        raise AgentToolError(exc.code, exc.message, hint=exc.hint, details=exc.details) from exc
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
    raw_symbols = payload.get("symbols")
    symbols_arg = (
        (",".join(str(item) for item in raw_symbols) if raw_symbols else None)
        if isinstance(raw_symbols, list)
        else (str(raw_symbols) if raw_symbols is not None else None)
    )
    try:
        split_symbol_whitelist(symbols_arg)
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
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
