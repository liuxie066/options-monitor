from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from scripts.agent_plugin.contracts import AgentToolError


def scan_summary_rows(summary_rows: list[dict[str, Any]], *, as_float: Callable[[Any], float | None]) -> dict[str, Any]:
    strategy_counts = {"sell_put": 0, "sell_call": 0}
    account_counts: dict[str, int] = {}
    symbol_counts: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []
    for row in summary_rows:
        if not isinstance(row, dict):
            continue
        strategy = str(row.get("side") or row.get("strategy") or row.get("option_strategy") or "").strip().lower()
        if strategy in strategy_counts:
            strategy_counts[strategy] += 1
        account = str(row.get("account") or row.get("account_label") or "").strip().lower()
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
                "annualized_return": as_float(row.get("annualized_net_return") or row.get("annualized_return") or row.get("annualized")),
                "strike": as_float(row.get("strike")),
                "expiration": (str(row.get("expiration") or "").strip() or None),
            }
        )
    top_candidates = sorted(
        candidates,
        key=lambda item: (
            -(item["net_income"] if item["net_income"] is not None else -10**12),
            -(item["annualized_return"] if item["annualized_return"] is not None else -10**12),
        ),
    )[:5]
    return {
        "row_count": len(summary_rows),
        "symbol_count": len(symbol_counts),
        "strategy_counts": strategy_counts,
        "account_counts": account_counts,
        "top_candidates": top_candidates,
    }


def close_advice_rows_summary(csv_path: Path, text_path: Path, *, safe_read_csv: Callable[[Path], Any], as_float: Callable[[Any], float | None]) -> dict[str, Any]:
    df = safe_read_csv(csv_path)
    rows = df.to_dict(orient="records") if not df.empty else []
    tier_counts: dict[str, int] = {}
    account_counts: dict[str, int] = {}
    top_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        tier = str(row.get("tier") or "").strip().lower() or "none"
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        account = str(row.get("account") or "").strip().lower()
        if account:
            account_counts[account] = account_counts.get(account, 0) + 1
        top_rows.append(
            {
                "account": account or None,
                "symbol": (str(row.get("symbol") or "").strip().upper() or None),
                "option_type": (str(row.get("option_type") or "").strip().lower() or None),
                "expiration": (str(row.get("expiration") or "").strip() or None),
                "strike": as_float(row.get("strike")),
                "tier": tier,
                "tier_label": (str(row.get("tier_label") or "").strip() or None),
                "remaining_annualized_return": as_float(row.get("remaining_annualized_return")),
                "realized_if_close": as_float(row.get("realized_if_close")),
            }
        )
    top_rows = sorted(
        top_rows,
        key=lambda item: (
            {"strong": 0, "medium": 1, "weak": 2, "none": 9}.get(str(item.get("tier") or "none"), 9),
            -(item["realized_if_close"] if item["realized_if_close"] is not None else -10**12),
        ),
    )[:5]
    try:
        notification_preview = text_path.read_text(encoding="utf-8").strip()
    except Exception:
        notification_preview = ""
    return {
        "row_count": len(rows),
        "tier_counts": tier_counts,
        "account_counts": account_counts,
        "top_rows": top_rows,
        "notification_preview": notification_preview,
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
    broker = normalize_broker(payload.get("broker") or payload.get("market") or portfolio_cfg.get("broker") or portfolio_cfg.get("market"))
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
    )
    return result, [], {"config_path": mask_path(config_path), "output_dir": mask_path(out_dir)}


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
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    account = str(payload.get("account") or portfolio_cfg.get("account") or "").strip() or None
    broker = normalize_broker(payload.get("broker") or payload.get("market") or portfolio_cfg.get("broker") or portfolio_cfg.get("market"))
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
        ttl_sec=int(payload.get("ttl_sec") or 0),
        state_dir=state_dir,
        shared_state_dir=shared_dir,
        log=logs.append,
        runtime_config=cfg,
    )
    if not isinstance(ctx, dict):
        raise AgentToolError(code="DEPENDENCY_MISSING", message="portfolio context is unavailable", details={"logs": logs[-5:]})
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

    cfg_loaded = load_config(base=repo_base(), config_path=config_path, is_scheduled=False, log=_log, state_dir=state_dir)
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
        portfolio_timeout_sec=int(payload.get("portfolio_timeout_sec") or runtime.get("portfolio_timeout_sec", 60) or 60),
        want_scan=True,
        no_context=bool(payload.get("no_context", False)),
        symbols_arg=(",".join(payload.get("symbols")) if isinstance(payload.get("symbols"), list) else payload.get("symbols")),
        log=_log,
        want_fn=lambda _step: True,
    )
    summary = scan_summary_rows_fn(summary_rows)
    return {
        "summary_rows": summary_rows,
        "symbol_count": len({str(r.get("symbol") or "").strip() for r in summary_rows if str(r.get("symbol") or "").strip()}),
        "row_count": len(summary_rows),
        "summary": summary,
        "top_candidates": summary["top_candidates"],
    }, [], {"config_path": str(config_path), "report_dir": str(report_dir)}


def prepare_close_advice_inputs_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config,
    resolve_public_data_config_path,
    normalize_broker,
    resolve_output_root,
    load_option_positions_context,
    symbol_fetch_config_map_fn,
    extract_context_symbols_fn,
    resolve_symbol_fetch_source,
    fetch_symbol_opend,
    save_required_data_opend,
    repo_base,
    mask_path,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    data_config = str(resolve_public_data_config_path(payload, portfolio_cfg))
    account = str(payload.get("account") or portfolio_cfg.get("account") or "").strip() or None
    broker = normalize_broker(payload.get("broker") or payload.get("market") or portfolio_cfg.get("broker") or portfolio_cfg.get("market"))
    out_root = resolve_output_root(payload.get("output_dir"))
    state_dir = (out_root / "state").resolve()
    shared_dir = (out_root / "shared").resolve()
    required_data_root = (out_root / "required_data").resolve()
    required_data_root.mkdir(parents=True, exist_ok=True)
    logs: list[str] = []
    try:
        ctx, _refreshed = load_option_positions_context(
            base=repo_base(),
            data_config=data_config,
            market=broker,
            account=account,
            ttl_sec=int(payload.get("ttl_sec") or 0),
            state_dir=state_dir,
            shared_state_dir=shared_dir,
            log=logs.append,
        )
    except SystemExit as exc:
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="option positions context refresh failed",
            hint="Check portfolio.data_config / SQLite position-lot setup before preparing close_advice inputs.",
            details={"exit_code": str(exc)},
        ) from exc
    if not isinstance(ctx, dict):
        raise AgentToolError(code="DEPENDENCY_MISSING", message="option positions context is unavailable", details={"logs": logs[-5:]})

    symbol_map = symbol_fetch_config_map_fn(cfg)
    fetched: list[dict[str, Any]] = []
    warnings = [item for item in logs if item.startswith("[WARN]")]
    for symbol in extract_context_symbols_fn(ctx):
        symbol_cfg = symbol_map.get(symbol) or {}
        fetch_cfg = symbol_cfg.get("fetch") if isinstance(symbol_cfg.get("fetch"), dict) else {}
        src, _decision = resolve_symbol_fetch_source(fetch_cfg)
        limit_expirations = int(fetch_cfg.get("limit_expirations") or 8)
        result = fetch_symbol_opend(
            symbol,
            limit_expirations=limit_expirations,
            host=str(fetch_cfg.get("host") or "127.0.0.1"),
            port=int(fetch_cfg.get("port") or 11111),
            base_dir=repo_base(),
            option_types="put,call",
            chain_cache=True,
        )
        _raw_path, csv_path = save_required_data_opend(repo_base(), symbol, result, output_root=required_data_root)
        meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
        if meta.get("error"):
            warnings.append(f"{symbol}: {meta['error']}")
        fetched.append({"symbol": symbol, "source": src, "rows": len(result.get("rows") or []), "expiration_count": int(result.get("expiration_count") or 0), "csv": mask_path(csv_path)})

    return {
        "account": account,
        "broker": broker,
        "context_rows": len(ctx.get("open_positions_min") or []),
        "symbols": fetched,
        "symbol_count": len(fetched),
    }, warnings, {
        "config_path": mask_path(config_path),
        "context_path": mask_path(state_dir / "option_positions_context.json"),
        "required_data_root": mask_path(required_data_root),
    }


def close_advice_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config,
    resolve_output_root,
    resolve_local_path,
    run_close_advice,
    close_advice_rows_summary_fn,
    repo_base,
    mask_path,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    out_root = resolve_output_root(payload.get("output_dir"))
    context_path = resolve_local_path(payload.get("context_path"), default=(out_root / "state" / "option_positions_context.json"))
    required_data_root = resolve_local_path(payload.get("required_data_root"), default=(out_root / "required_data"))
    report_dir = (out_root / "reports").resolve()
    if not context_path.exists():
        raise AgentToolError(code="DEPENDENCY_MISSING", message="close_advice requires a local option_positions_context.json", hint="Run the scan/context pipeline first, or pass context_path explicitly.", details={"context_path": mask_path(context_path)})
    if not required_data_root.exists():
        raise AgentToolError(code="DEPENDENCY_MISSING", message="close_advice requires a local required_data directory", hint="Run the scan pipeline first, or pass required_data_root explicitly.", details={"required_data_root": mask_path(required_data_root)})
    result = run_close_advice(config=cfg, context_path=context_path, required_data_root=required_data_root, output_dir=report_dir, base_dir=repo_base())
    advice_summary = close_advice_rows_summary_fn(report_dir / "close_advice.csv", report_dir / "close_advice.txt")
    return {
        **result,
        "summary": {
            "row_count": advice_summary["row_count"],
            "tier_counts": advice_summary["tier_counts"],
            "account_counts": advice_summary["account_counts"],
        },
        "top_rows": advice_summary["top_rows"],
        "notification_preview": advice_summary["notification_preview"],
    }, [], {
        "config_path": mask_path(config_path),
        "context_path": mask_path(context_path),
        "required_data_root": mask_path(required_data_root),
        "output_dir": mask_path(report_dir),
    }


def get_close_advice_tool(
    payload: dict[str, Any],
    *,
    prepare_close_advice_inputs_tool_fn,
    close_advice_tool_fn,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    prepared_data, prepare_warnings, prepare_meta = prepare_close_advice_inputs_tool_fn(payload)
    advice_data, advice_warnings, advice_meta = close_advice_tool_fn(payload)
    combined_summary = {
        "prepared_symbol_count": int(prepared_data.get("symbol_count") or 0),
        "prepared_context_rows": int(prepared_data.get("context_rows") or 0),
        "advice_row_count": int(advice_data.get("rows") or advice_data.get("summary", {}).get("row_count") or 0),
        "notify_row_count": int(advice_data.get("notify_rows") or 0),
        "tier_counts": dict(advice_data.get("summary", {}).get("tier_counts")) if isinstance(advice_data.get("summary"), dict) and isinstance(advice_data.get("summary", {}).get("tier_counts"), dict) else {},
    }
    return {
        "prepared": prepared_data,
        "close_advice": advice_data,
        "summary": combined_summary,
        "top_rows": list(advice_data.get("top_rows") or []),
        "notification_preview": advice_data.get("notification_preview"),
    }, [*prepare_warnings, *advice_warnings], {**prepare_meta, **advice_meta}
