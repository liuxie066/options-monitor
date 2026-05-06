from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.application.opend_fetch_config import opend_discovery_kwargs, opend_fetch_kwargs
from src.application.required_data_planning import build_required_data_fetch_plan
from src.application.yield_enhancement_config import resolve_yield_enhancement_cfg


@dataclass(frozen=True)
class SymbolMonitoringInputs:
    py: str
    base: Path
    symbol_cfg: dict
    top_n: int
    portfolio_ctx: dict | None
    usd_per_cny_exchange_rate: float | None
    cny_per_hkd_exchange_rate: float | None
    timeout_sec: int | None
    required_data_dir: Path
    report_dir: Path
    state_dir: Path | None
    is_scheduled: bool
    runtime_config: dict | None = None


@dataclass(frozen=True)
class SymbolMonitoringDependencies:
    build_converter_fn: Callable[..., object]
    apply_prefilters_fn: Callable[..., object]
    apply_multiplier_cache_fn: Callable[..., None]
    ensure_required_data_fn: Callable[..., None]
    run_sell_put_scan_fn: Callable[..., dict]
    empty_sell_put_summary_fn: Callable[..., dict]
    run_sell_call_scan_fn: Callable[..., dict]
    empty_sell_call_summary_fn: Callable[..., dict]


def _append_summary_result(summary_rows: list[dict], result: object) -> None:
    if result is None:
        return
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                summary_rows.append(item)
        return
    if isinstance(result, dict):
        summary_rows.append(result)


def run_symbol_monitoring(
    *,
    inputs: SymbolMonitoringInputs,
    deps: SymbolMonitoringDependencies,
) -> list[dict]:
    symbol_cfg = dict(inputs.symbol_cfg or {})
    symbol = str(symbol_cfg["symbol"])
    symbol_lower = symbol.lower()
    limit_expirations = symbol_cfg.get("fetch", {}).get("limit_expirations", 8)

    sp = dict(symbol_cfg.get("sell_put", {}) or {})
    cc = dict(symbol_cfg.get("sell_call", {}) or {})
    yield_enhancement_cfg = resolve_yield_enhancement_cfg(symbol_cfg)
    want_put = bool(sp.get("enabled", False))
    want_call = bool(cc.get("enabled", False))

    exchange_rate_converter = deps.build_converter_fn(
        usd_per_cny_exchange_rate=inputs.usd_per_cny_exchange_rate,
        cny_per_hkd_exchange_rate=inputs.cny_per_hkd_exchange_rate,
    )

    prefilters = deps.apply_prefilters_fn(
        symbol=symbol,
        sp=sp,
        cc=cc,
        want_put=want_put,
        want_call=want_call,
        portfolio_ctx=inputs.portfolio_ctx,
        usd_per_cny_exchange_rate=inputs.usd_per_cny_exchange_rate,
        cny_per_hkd_exchange_rate=inputs.cny_per_hkd_exchange_rate,
    )
    want_put = bool(prefilters.want_put)
    want_call = bool(prefilters.want_call)
    sp = dict(prefilters.sp)
    cc = dict(prefilters.cc)
    symbol_cfg["sell_put"] = sp
    symbol_cfg["sell_call"] = cc
    yield_enhancement_cfg = resolve_yield_enhancement_cfg(symbol_cfg)
    if yield_enhancement_cfg:
        symbol_cfg["yield_enhancement"] = yield_enhancement_cfg
    stock = prefilters.stock
    fetch_want_put = bool(want_put)
    fetch_want_call = bool(want_call or (want_put and yield_enhancement_cfg.get("enabled", False)))

    try:
        deps.apply_multiplier_cache_fn(
            base=inputs.base,
            required_data_dir=inputs.required_data_dir,
            symbol=symbol,
        )
    except Exception:
        pass

    fetch_cfg = dict(symbol_cfg.get("fetch", {}) or {})
    runtime_config = (
        inputs.runtime_config
        if isinstance(inputs.runtime_config, dict)
        else symbol_cfg
    )
    discovery_fetch_kwargs = opend_discovery_kwargs(runtime_config)
    fetch_request_kwargs = opend_fetch_kwargs(runtime_config)
    fetch_plan = build_required_data_fetch_plan(
        base=inputs.base,
        required_data_dir=inputs.required_data_dir,
        symbol=symbol,
        limit_expirations=int(limit_expirations),
        want_put=want_put,
        want_call=want_call,
        sell_put_cfg=sp,
        sell_call_cfg=cc,
        yield_enhancement_cfg=yield_enhancement_cfg,
        fetch_host=str(fetch_cfg.get("host") or "127.0.0.1"),
        fetch_port=int(fetch_cfg.get("port") or 11111),
        **discovery_fetch_kwargs,
    )

    deps.ensure_required_data_fn(
        py=inputs.py,
        base=inputs.base,
        symbol=symbol,
        required_data_dir=inputs.required_data_dir,
        limit_expirations=limit_expirations,
        want_put=fetch_want_put,
        want_call=fetch_want_call,
        timeout_sec=inputs.timeout_sec,
        is_scheduled=bool(inputs.is_scheduled),
        state_dir=inputs.state_dir,
        fetch_source=str(fetch_cfg.get("source") or "opend"),
        fetch_host=str(fetch_cfg.get("host") or "127.0.0.1"),
        fetch_port=int(fetch_cfg.get("port") or 11111),
        max_strike=(float(sp.get("max_strike")) if (want_put and sp.get("max_strike") is not None) else None),
        min_dte=None,
        max_dte=None,
        fetch_plan=fetch_plan,
        report_dir=inputs.report_dir,
        opend_fetch_config=fetch_request_kwargs,
    )

    summary_rows: list[dict] = []

    if want_put:
        _append_summary_result(
            summary_rows,
            deps.run_sell_put_scan_fn(
                py=inputs.py,
                base=inputs.base,
                sym=symbol,
                symbol=symbol,
                symbol_lower=symbol_lower,
                symbol_cfg=symbol_cfg,
                sp=sp,
                top_n=inputs.top_n,
                required_data_dir=inputs.required_data_dir,
                report_dir=inputs.report_dir,
                timeout_sec=inputs.timeout_sec,
                is_scheduled=bool(inputs.is_scheduled),
                exchange_rate_converter=exchange_rate_converter,
                portfolio_ctx=inputs.portfolio_ctx,
                global_sell_put_liquidity=(symbol_cfg.get("_global_sell_put_liquidity") or {}),
                global_sell_put_event_risk=(symbol_cfg.get("_global_sell_put_event_risk") or {}),
            )
        )
    else:
        _append_summary_result(summary_rows, deps.empty_sell_put_summary_fn(symbol, symbol_cfg=symbol_cfg))

    if want_call:
        _append_summary_result(
            summary_rows,
            deps.run_sell_call_scan_fn(
                py=inputs.py,
                base=inputs.base,
                symbol=symbol,
                symbol_lower=symbol_lower,
                symbol_cfg=symbol_cfg,
                cc=cc,
                top_n=inputs.top_n,
                required_data_dir=inputs.required_data_dir,
                report_dir=inputs.report_dir,
                timeout_sec=inputs.timeout_sec,
                is_scheduled=bool(inputs.is_scheduled),
                stock=stock,
                exchange_rate_converter=exchange_rate_converter,
                locked_shares_by_symbol=((inputs.portfolio_ctx or {}).get("option_ctx") or {}).get("locked_shares_by_symbol"),
                global_sell_call_liquidity=(symbol_cfg.get("_global_sell_call_liquidity") or {}),
                global_sell_call_event_risk=(symbol_cfg.get("_global_sell_call_event_risk") or {}),
            )
        )
    else:
        _append_summary_result(summary_rows, deps.empty_sell_call_summary_fn(symbol, symbol_cfg=symbol_cfg))

    return summary_rows
