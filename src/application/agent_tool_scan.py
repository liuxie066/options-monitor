from __future__ import annotations

import csv
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
import json

from src.application.agent_tool_contracts import AgentToolError
from domain.domain.close_advice import TIER_PRIORITY
from scripts.option_positions_core.domain import normalize_account
from scripts.trade_contract_identity import (
    contract_key,
    normalize_contract_expiration,
    normalize_contract_option_type,
)
from src.application.expiration_normalization import find_unique_near_miss_expiration
from src.application.opend_fetch_config import opend_fetch_kwargs
from src.application.watchlist_mutations import normalize_symbol_read


def _normalize_expiration(value: Any) -> str:
    return normalize_contract_expiration(value, fallback_raw=True) or ""


def _normalize_option_type(value: Any) -> str:
    return normalize_contract_option_type(value)


def _as_float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _contract_key(symbol: Any, option_type: Any, expiration: Any, strike: Any) -> tuple[str, str, str, str]:
    return contract_key(symbol, option_type, expiration, strike, expiration_fallback_raw=True)


def _position_expiration_for_fetch(row: dict[str, Any]) -> str:
    for value in (
        row.get("expiration_ymd"),
        row.get("expiration"),
    ):
        exp = _normalize_expiration(value)
        if exp:
            return exp
    note = str(row.get("note") or "")
    for token in note.replace(";", " ").split():
        if token.startswith("exp="):
            return _normalize_expiration(token.split("=", 1)[1])
    return ""


def _extract_position_fetch_requirements(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    rows = ctx.get("open_positions_min") if isinstance(ctx, dict) else []
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol_read(row.get("symbol"))
        if not symbol:
            continue
        item = grouped.get(symbol)
        if item is None:
            item = {
                "symbol": symbol,
                "requested_expirations": set(),
                "option_types": set(),
                "strikes": [],
                "requested_contracts": set(),
                "position_count": 0,
            }
            grouped[symbol] = item
            order.append(symbol)
        item["position_count"] += 1
        option_type = _normalize_option_type(row.get("option_type"))
        expiration = _position_expiration_for_fetch(row)
        strike_num = _as_float_or_none(row.get("strike"))
        if option_type:
            item["option_types"].add(option_type)
        if expiration:
            item["requested_expirations"].add(expiration)
        if strike_num is not None:
            item["strikes"].append(strike_num)
        key = _contract_key(symbol, option_type, expiration, strike_num)
        if all(key):
            item["requested_contracts"].add(key)
    out: list[dict[str, Any]] = []
    for symbol in order:
        item = grouped[symbol]
        strikes = [float(v) for v in item["strikes"]]
        out.append(
            {
                "symbol": symbol,
                "requested_expirations": sorted(item["requested_expirations"]),
                "option_types": sorted(item["option_types"]),
                "min_strike": min(strikes) if strikes else None,
                "max_strike": max(strikes) if strikes else None,
                "requested_contracts": set(item["requested_contracts"]),
                "position_count": int(item["position_count"]),
            }
        )
    return out


def _read_required_data_coverage(csv_path: Path) -> tuple[set[tuple[str, str, str, str]], set[str]]:
    contract_keys: set[tuple[str, str, str, str]] = set()
    expirations: set[str] = set()
    if not csv_path.exists():
        return contract_keys, expirations
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                if not isinstance(row, dict):
                    continue
                key = _contract_key(
                    row.get("symbol"),
                    row.get("option_type"),
                    row.get("expiration"),
                    row.get("strike"),
                )
                if all(key):
                    contract_keys.add(key)
                    expirations.add(key[2])
    except Exception:
        return set(), set()
    return contract_keys, expirations


def _count_required_data_rows(csv_path: Path) -> int:
    if not csv_path.exists():
        return 0
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as fh:
            return sum(1 for row in csv.DictReader(fh) if isinstance(row, dict))
    except Exception:
        return 0


def _find_contract_expiration_near_misses(
    requested_contracts: set[tuple[str, str, str, str]],
    available_contracts: set[tuple[str, str, str, str]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in sorted(requested_contracts):
        if key in available_contracts or not all(key):
            continue
        symbol, option_type, expiration, strike = key
        candidate_expirations = [
            avail_exp
            for avail_symbol, avail_option_type, avail_exp, avail_strike in available_contracts
            if avail_symbol == symbol and avail_option_type == option_type and avail_strike == strike
        ]
        near_miss = find_unique_near_miss_expiration(expiration, candidate_expirations)
        if not near_miss:
            continue
        out.append(
            {
                "symbol": symbol,
                "option_type": option_type,
                "strike": _as_float_or_none(strike),
                "requested_expiration": expiration,
                "matched_expiration": near_miss,
                "quote_key": "|".join(key),
            }
        )
    return out


def _build_coverage_summary(symbol_rows: list[dict[str, Any]]) -> dict[str, Any]:
    missing_symbols = [
        str(item.get("symbol") or "")
        for item in symbol_rows
        if isinstance(item, dict) and not bool(item.get("position_coverage_ok"))
    ]
    return {
        "symbol_count": len(symbol_rows),
        "position_count": sum(int(item.get("position_count") or 0) for item in symbol_rows if isinstance(item, dict)),
        "covered_symbol_count": sum(1 for item in symbol_rows if isinstance(item, dict) and bool(item.get("position_coverage_ok"))),
        "symbols_with_missing_coverage": missing_symbols,
        "positions_missing_coverage": sum(int(item.get("missing_contract_count") or 0) for item in symbol_rows if isinstance(item, dict)),
        "expiration_near_miss_count": sum(len(item.get("expiration_near_misses") or []) for item in symbol_rows if isinstance(item, dict)),
    }


def scan_summary_rows(summary_rows: list[dict[str, Any]], *, as_float: Callable[[Any], float | None]) -> dict[str, Any]:
    strategy_counts = {"sell_put": 0, "sell_call": 0, "yield_enhancement": 0}
    account_counts: dict[str, int] = {}
    symbol_counts: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []
    for row in summary_rows:
        if not isinstance(row, dict):
            continue
        strategy = str(row.get("side") or row.get("strategy") or row.get("option_strategy") or "").strip().lower()
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
        if str(row.get("evaluation_status") or "priced").strip().lower() != "priced":
            continue
        tier = str(row.get("tier") or "").strip().lower() or "none"
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        account = normalize_account(row.get("account"))
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
            TIER_PRIORITY.get(str(item.get("tier") or "none"), 9),
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
    )
    return result, [], {"config_path": mask_path(config_path), "output_dir": mask_path(out_dir)}


def monthly_income_report_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config,
    resolve_public_data_config_path,
    normalize_broker,
    resolve_option_positions_repo,
    build_monthly_income_report,
    get_cached_exchange_rates,
    repo_base,
    mask_path,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    data_config_path = resolve_public_data_config_path(payload, portfolio_cfg)
    _resolved_data_config, repo = resolve_option_positions_repo(base=repo_base(), data_config=data_config_path)

    broker = normalize_broker(payload.get("broker") or portfolio_cfg.get("broker") or "富途")
    account = str(payload.get("account") or "").strip() or None
    month = str(payload.get("month") or "").strip() or None
    include_rows = bool(payload.get("include_rows", False))

    rate_cache_path = (repo_base() / "output" / "state" / "rate_cache.json").resolve()
    rates = get_cached_exchange_rates(cache_path=rate_cache_path)
    warnings: list[str] = []
    if rates is None:
        warnings.append("exchange_rate cache unavailable; *_cny fields may be null")

    report = build_monthly_income_report(
        repo.list_records(page_size=500),
        account=account,
        broker=broker,
        month=month,
        rates=rates,
    )
    report_warnings = [str(item) for item in (report.get("warnings") or []) if str(item).strip()]
    warnings.extend(report_warnings)

    rows = report.get("rows") if isinstance(report.get("rows"), list) else []
    premium_rows = report.get("premium_rows") if isinstance(report.get("premium_rows"), list) else []
    data: dict[str, Any] = {
        "summary": report.get("summary") if isinstance(report.get("summary"), list) else [],
        "filters": dict(report.get("filters") or {}),
        "row_count": len(rows),
        "premium_row_count": len(premium_rows),
        "report_warnings": report_warnings,
    }
    if include_rows:
        data["rows"] = rows
        data["premium_rows"] = premium_rows

    meta = {
        "config_path": mask_path(config_path),
        "data_config": mask_path(data_config_path),
        "rate_cache": mask_path(rate_cache_path),
    }
    return data, warnings, meta


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
    broker = normalize_broker(payload.get("broker") or portfolio_cfg.get("broker"))
    out_root = resolve_output_root(payload.get("output_dir"))
    state_dir = (out_root / "state").resolve()
    shared_dir = (out_root / "shared").resolve()
    required_data_root = (out_root / "required_data").resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    shared_dir.mkdir(parents=True, exist_ok=True)
    required_data_root.mkdir(parents=True, exist_ok=True)
    logs: list[str] = []
    context_path = state_dir / "option_positions_context.json"
    if not Path(data_config).exists():
        empty_ctx = {"open_positions_min": []}
        context_path.write_text(json.dumps(empty_ctx, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "account": account,
            "broker": broker,
            "context_rows": 0,
            "symbols": [],
            "symbol_count": 0,
        }, [f"[WARN] option positions data config not found: {mask_path(Path(data_config))}"], {
            "config_path": mask_path(config_path),
            "context_path": mask_path(context_path),
            "required_data_root": mask_path(required_data_root),
        }
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

    position_requirements = _extract_position_fetch_requirements(ctx)
    if not position_requirements:
        return {
            "account": account,
            "broker": broker,
            "context_rows": len(ctx.get("open_positions_min") or []),
            "symbols": [],
            "symbol_count": 0,
            "coverage_summary": _build_coverage_summary([]),
        }, [item for item in logs if item.startswith("[WARN]")], {
            "config_path": mask_path(config_path),
            "context_path": mask_path(context_path),
            "required_data_root": mask_path(required_data_root),
        }

    symbol_map = symbol_fetch_config_map_fn(cfg)
    fetched: list[dict[str, Any]] = []
    warnings = [item for item in logs if item.startswith("[WARN]")]
    force_required_data_refresh = bool(payload.get("force_required_data_refresh", False))
    for spec in position_requirements:
        symbol = str(spec.get("symbol") or "").strip()
        symbol_cfg = symbol_map.get(symbol) or {}
        fetch_cfg = symbol_cfg.get("fetch") if isinstance(symbol_cfg.get("fetch"), dict) else {}
        src, _decision = resolve_symbol_fetch_source(fetch_cfg)
        limit_expirations = int(fetch_cfg.get("limit_expirations") or 8)
        csv_path = (required_data_root / "parsed" / f"{symbol}_required_data.csv").resolve()
        requested_expirations = list(spec.get("requested_expirations") or [])
        requested_contracts = set(spec.get("requested_contracts") or set())
        if force_required_data_refresh:
            fetched_contracts: set[tuple[str, str, str, str]] = set()
            fetched_expirations: set[str] = set()
        else:
            fetched_contracts, fetched_expirations = _read_required_data_coverage(csv_path)

        cache_covers_all = (
            not force_required_data_refresh
            and bool(requested_contracts)
            and all(item in fetched_contracts for item in requested_contracts)
        )
        if not cache_covers_all:
            result = fetch_symbol_opend(
                symbol,
                limit_expirations=limit_expirations,
                host=str(fetch_cfg.get("host") or "127.0.0.1"),
                port=int(fetch_cfg.get("port") or 11111),
                base_dir=repo_base(),
                option_types=",".join(spec.get("option_types") or ["put", "call"]),
                min_strike=spec.get("min_strike"),
                max_strike=spec.get("max_strike"),
                explicit_expirations=requested_expirations,
                chain_cache=True,
                chain_cache_force_refresh=force_required_data_refresh,
                **opend_fetch_kwargs(cfg),
            )
            _raw_path, csv_path = save_required_data_opend(repo_base(), symbol, result, output_root=required_data_root)
            meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            if meta.get("error"):
                warnings.append(f"{symbol}: {meta['error']}")
            fetched_contracts, fetched_expirations = _read_required_data_coverage(csv_path)
            row_count = len(result.get("rows") or [])
            expiration_count = int(result.get("expiration_count") or 0)
        else:
            row_count = _count_required_data_rows(csv_path)
            expiration_count = len(fetched_expirations)
        missing_expirations = [exp for exp in requested_expirations if exp not in fetched_expirations]
        missing_contract_keys = [item for item in sorted(requested_contracts) if item not in fetched_contracts]
        missing_contracts = sorted(
            f"{item[2]} {item[3]}{'P' if item[1] == 'put' else 'C'}"
            for item in missing_contract_keys
        )
        near_misses = _find_contract_expiration_near_misses(requested_contracts, fetched_contracts)
        item = {
            "symbol": symbol,
            "source": src,
            "rows": row_count,
            "expiration_count": expiration_count,
            "csv": mask_path(csv_path),
            "position_count": int(spec.get("position_count") or 0),
            "requested_expirations": requested_expirations,
            "fetched_expirations": sorted(fetched_expirations),
            "missing_expirations": missing_expirations,
            "position_coverage_ok": not missing_contracts,
            "missing_contract_count": len(missing_contracts),
            "missing_contract_samples": missing_contracts[:3],
            "missing_contracts": [
                {
                    "symbol": key[0],
                    "option_type": key[1],
                    "expiration": key[2],
                    "strike": _as_float_or_none(key[3]),
                    "quote_key": "|".join(key),
                }
                for key in missing_contract_keys
            ],
            "expiration_near_misses": near_misses,
        }
        if missing_expirations:
            warnings.append(f"{symbol}: missing required expirations {', '.join(missing_expirations)}")
        elif missing_contracts:
            warnings.append(f"{symbol}: missing required contracts after fetch ({', '.join(missing_contracts[:3])})")
        for near_miss in near_misses:
            warnings.append(
                f"{symbol}: expiration near miss {near_miss['requested_expiration']} -> {near_miss['matched_expiration']} "
                f"for {near_miss['option_type']} {near_miss['strike']}"
            )
        fetched.append(item)

    return {
        "account": account,
        "broker": broker,
        "context_rows": len(ctx.get("open_positions_min") or []),
        "symbols": fetched,
        "symbol_count": len(fetched),
        "coverage_summary": _build_coverage_summary(fetched),
    }, warnings, {
        "config_path": mask_path(config_path),
        "context_path": mask_path(context_path),
        "required_data_root": mask_path(required_data_root),
        "force_required_data_refresh": force_required_data_refresh,
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
        "coverage_summary": dict(prepared_data.get("coverage_summary")) if isinstance(prepared_data.get("coverage_summary"), dict) else {},
    }
    return {
        "prepared": prepared_data,
        "close_advice": advice_data,
        "summary": combined_summary,
        "top_rows": list(advice_data.get("top_rows") or []),
        "notification_preview": advice_data.get("notification_preview"),
    }, [*prepare_warnings, *advice_warnings], {**prepare_meta, **advice_meta}
