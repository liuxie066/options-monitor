from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from src.application.expiration_normalization import normalize_expiration_ymd
from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.opend_fetch_config import OpenDEndpointRateLimit, OpenDFetchLimits
from src.application.opend_utils import get_trading_date, normalize_underlier
from src.application.opend_expiration_cache import (
    load_option_expiration_cache,
    option_expiration_cache_path,
    save_option_expiration_cache,
)
from src.application.option_chain_fetching import (
    OptionChainFetchRequest,
    fetch_option_chains,
    prune_option_chain_cache,
)
from src.infrastructure.futu_gateway import build_ready_futu_gateway, retry_futu_gateway_call


REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class SymbolOptionChainResult:
    rows: list[dict[str, Any]]
    expirations_all: list[str]
    expirations_pick: list[str]
    fetch_meta: dict[str, Any]


def prune_chain_cache(base_dir: Path, keep_days: int) -> None:
    try:
        prune_option_chain_cache(base_dir, keep_days)
    except Exception:
        pass


def list_option_expirations(
    symbol: str,
    *,
    host: str = "127.0.0.1",
    port: int = 11111,
    base_dir: Path | None = None,
    expiration_max_wait_sec: float = 30.0,
    expiration_window_sec: float = 30.0,
    expiration_max_calls: int = 60,
) -> list[str]:
    gateway = build_ready_futu_gateway(
        host=host,
        port=int(port),
        is_option_chain_cache_enabled=False,
    )
    try:
        effective_base_dir = Path(base_dir) if base_dir is not None else REPO_ROOT
        underlier = normalize_underlier(symbol, base_dir=effective_base_dir)
        expiration_limit = OpenDFetchLimits.from_flat_kwargs(
            expiration_max_wait_sec=expiration_max_wait_sec,
            expiration_window_sec=expiration_window_sec,
            expiration_max_calls=expiration_max_calls,
        ).option_expiration
        return list_option_expirations_with_gateway(
            gateway,
            underlier_code=underlier.code,
            base_dir=effective_base_dir,
            asof_date=get_trading_date(underlier.market).isoformat(),
            expiration_limit=expiration_limit,
            retry_call=retry_futu_gateway_call,
            rate_limited_call=rate_limited_opend_call,
        )
    finally:
        try:
            gateway.close()
        except Exception:
            pass


def list_option_expirations_with_gateway(
    gateway: Any,
    *,
    underlier_code: str,
    base_dir: Path,
    expiration_limit: OpenDEndpointRateLimit,
    no_retry: bool = False,
    retry_max_attempts: int = 4,
    retry_time_budget_sec: float = 8.0,
    retry_base_delay_sec: float = 0.8,
    retry_max_delay_sec: float = 6.0,
    retry_call: Callable[..., Any] = retry_futu_gateway_call,
    rate_limited_call: Callable[..., Any] = rate_limited_opend_call,
    asof_date: str | None = None,
    use_cache: bool = True,
    metrics: dict[str, Any] | None = None,
) -> list[str]:
    cache_path = None
    if use_cache and asof_date:
        cache_path = option_expiration_cache_path(base_dir, underlier_code, str(asof_date))
        cached = load_option_expiration_cache(cache_path, asof_date=str(asof_date))
        if cached is not None:
            _increment_metric(metrics, "expiration_cache_hits")
            return cached

    def _call_expiration_dates() -> Any:
        _increment_metric(metrics, "expiration_opend_calls")
        return gateway.get_option_expiration_dates(underlier_code)

    df_e = retry_call(
        "get_option_expiration_date",
        lambda: rate_limited_call(
            base_dir=base_dir,
            endpoint="option_expiration",
            **expiration_limit.call_kwargs(),
            call=_call_expiration_dates,
        ),
        no_retry=no_retry,
        retry_max_attempts=retry_max_attempts,
        retry_time_budget_sec=retry_time_budget_sec,
        retry_base_delay_sec=retry_base_delay_sec,
        retry_max_delay_sec=retry_max_delay_sec,
        quiet=False,
    )
    if df_e is None or df_e.empty:
        return []
    expirations = sorted({
        exp
        for exp in (normalize_expiration_ymd(value) for value in df_e.get("strike_time").tolist())
        if exp
    })
    if cache_path is not None and asof_date:
        save_option_expiration_cache(
            cache_path,
            asof_date=str(asof_date),
            underlier_code=underlier_code,
            expirations=expirations,
        )
    return expirations


def select_symbol_expirations(
    *,
    expirations_all: list[str],
    explicit_expirations_norm: list[str],
    limit_expirations: int | None,
    min_dte: int | None,
    max_dte: int | None,
    today: date,
) -> list[str]:
    expirations_pick0 = list(expirations_all)
    if expirations_all and (not explicit_expirations_norm) and ((min_dte is not None) or (max_dte is not None)):
        try:
            filtered = []
            for exp in expirations_all:
                try:
                    exp_date = datetime.fromisoformat(str(exp)[:10]).date()
                    dte = int((exp_date - today).days)
                    if (min_dte is not None) and (dte < int(min_dte)):
                        continue
                    if (max_dte is not None) and (dte > int(max_dte)):
                        continue
                    filtered.append(str(exp)[:10])
                except Exception:
                    continue
            expirations_pick0 = filtered if filtered else list(expirations_all)
        except Exception:
            expirations_pick0 = list(expirations_all)

    if explicit_expirations_norm:
        return expirations_pick0
    if limit_expirations and expirations_pick0:
        return expirations_pick0[: int(limit_expirations)]
    return expirations_pick0


def fetch_symbol_option_chain(
    *,
    gateway: Any,
    request: Any,
    underlier_code: str,
    today: date,
    explicit_expirations_norm: list[str],
    limits: OpenDFetchLimits,
    retry_call: Callable[..., Any] = retry_futu_gateway_call,
    rate_limited_call: Callable[..., Any] = rate_limited_opend_call,
) -> SymbolOptionChainResult:
    expiration_fetch_meta: dict[str, Any] = {
        "expiration_opend_calls": 0,
        "expiration_cache_hits": 0,
    }
    if explicit_expirations_norm:
        expirations_all = list(explicit_expirations_norm)
    else:
        try:
            expirations_all = list_option_expirations_with_gateway(
                gateway,
                underlier_code=underlier_code,
                base_dir=request.effective_base_dir,
                expiration_limit=limits.option_expiration,
                no_retry=bool(request.no_retry),
                retry_max_attempts=int(request.retry_max_attempts),
                retry_time_budget_sec=float(request.retry_time_budget_sec),
                retry_base_delay_sec=float(request.retry_base_delay_sec),
                retry_max_delay_sec=float(request.retry_max_delay_sec),
                retry_call=retry_call,
                rate_limited_call=rate_limited_call,
                asof_date=today.isoformat(),
                metrics=expiration_fetch_meta,
            )
        except Exception:
            expirations_all = []

    expirations_pick = select_symbol_expirations(
        expirations_all=expirations_all,
        explicit_expirations_norm=explicit_expirations_norm,
        limit_expirations=request.limit_expirations,
        min_dte=request.min_dte,
        max_dte=request.max_dte,
        today=today,
    )

    effective_policy = "force_refresh" if request.chain_cache_force_refresh else str(request.freshness_policy or "cache_first")
    fetch_result = fetch_option_chains(
        gateway=gateway,
        request=OptionChainFetchRequest(
            symbol=request.symbol,
            underlier_code=underlier_code,
            expirations=list(expirations_pick),
            host=request.host,
            port=int(request.port),
            option_types=request.option_types,
            strike_windows=request.side_strike_windows or {},
            base_dir=request.effective_base_dir,
            asof_date=today.isoformat(),
            freshness_policy=effective_policy if effective_policy in {"cache_first", "refresh_missing", "force_refresh"} else "cache_first",
            chain_cache=bool(request.chain_cache),
            max_wait_sec=limits.option_chain.max_wait_sec,
            window_sec=limits.option_chain.window_sec,
            max_calls=limits.option_chain.max_calls,
            is_force_refresh=bool(request.chain_cache_force_refresh or effective_policy == "force_refresh"),
            no_retry=bool(request.no_retry),
            retry_max_attempts=int(request.retry_max_attempts),
            retry_time_budget_sec=float(request.retry_time_budget_sec),
            retry_base_delay_sec=float(request.retry_base_delay_sec),
            retry_max_delay_sec=float(request.retry_max_delay_sec),
        ),
        retry_call=retry_call,
    )

    fetch_meta = dict(fetch_result.to_meta())
    fetch_meta.update(expiration_fetch_meta)
    return SymbolOptionChainResult(
        rows=fetch_result.rows,
        expirations_all=expirations_all,
        expirations_pick=expirations_pick,
        fetch_meta=fetch_meta,
    )


def _increment_metric(metrics: dict[str, Any] | None, key: str, value: int = 1) -> None:
    if metrics is None:
        return
    try:
        metrics[key] = int(metrics.get(key) or 0) + int(value)
    except Exception:
        metrics[key] = int(value)
