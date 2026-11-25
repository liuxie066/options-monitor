from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from domain.domain.ledger.position_fields import norm_symbol
from src.application.market_data_provider import (
    resolve_market_data_provider,
    resolve_quote_route as resolve_futu_quote_route,
    open_quote_gateway as build_ready_futu_quote_gateway,
    get_stock_spot as get_spot_opend,
    get_underlier_observations,
)
from src.application.opend_fetch_config import resolve_opend_fetch_limits
from src.application.opend_utils import normalize_underlier


@dataclass(frozen=True)
class AssignedStockQuoteRefreshResult:
    quote_snapshots: list[dict[str, Any]]
    diagnostics: dict[str, Any]
    warnings: list[str]


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _positive_int(value: Any) -> int:
    try:
        out = int(float(value or 0))
    except Exception:
        return 0
    return max(0, out)


def _open_symbols(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _positive_int(row.get("shares_remaining")) <= 0:
            continue
        symbol = norm_symbol(row.get("symbol") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def refresh_assigned_stock_quote_snapshots(
    lifecycle_rows: list[dict[str, Any]],
    *,
    cfg: Mapping[str, Any] | Any,
    account: str | None = None,
    host: str | None = None,
    port: int | None = None,
    base_dir: Path | None = None,
    state_base_dir: Path | None = None,
    now_ms: Callable[[], int] = _now_ms,
    preserve_evidence: bool = False,
) -> AssignedStockQuoteRefreshResult:
    """Fetch realtime stock spot snapshots for open assigned-stock lots.

    The returned snapshots are valuation inputs only. They are not persisted and
    do not alter trade events, assigned-stock events, or projections.
    """

    symbols = _open_symbols(lifecycle_rows)
    diagnostics: dict[str, Any] = {
        "enabled": True,
        "quote_source": "opend_realtime",
        "requested_symbols": symbols,
        "refreshed_symbols": [],
        "missing_symbols": [],
        "errors": [],
    }
    if not symbols:
        diagnostics["status"] = "skipped_no_open_assigned_stock"
        return AssignedStockQuoteRefreshResult([], diagnostics, [])

    config = dict(cfg) if isinstance(cfg, Mapping) else {}
    provider = resolve_market_data_provider(config)
    provider_kwargs = {} if provider.name == "opend" else {"market_data_provider": provider.name}
    quote_source = "opend_realtime" if provider.name == "opend" else f"{provider.name}_realtime"
    diagnostics["quote_source"] = quote_source
    quote_route = resolve_futu_quote_route(config)
    explicit_override = host is not None or port is not None
    if host is not None and port is not None:
        effective_host = str(host).strip()
        try:
            effective_port = int(port)
        except Exception:
            effective_port = 0
    elif explicit_override:
        effective_host = str(host if host is not None else quote_route.host or "").strip()
        try:
            effective_port = int(port if port is not None else quote_route.port or 0)
        except Exception:
            effective_port = 0
    else:
        effective_host = str(quote_route.host or "").strip()
        effective_port = int(quote_route.port or 0)
    diagnostics["route_source"] = (
        "explicit_diagnostic_override" if explicit_override else "canonical_fetch_binding"
    )
    if not effective_host or effective_port <= 0 or (
        explicit_override and (host is None or port is None) and not quote_route.ok
    ) or (not explicit_override and not quote_route.ok):
        diagnostics["status"] = "source_unavailable"
        diagnostics["missing_symbols"] = list(symbols)
        diagnostics["errors"].append(
            {
                "stage": "route",
                "error_code": "FUTU_QUOTE_ROUTE_UNAVAILABLE",
                "message": "canonical Futu quote route is missing or conflicting",
            }
        )
        return AssignedStockQuoteRefreshResult(
            [], diagnostics, ["assigned stock quote refresh unavailable: canonical Futu quote route"]
        )
    effective_base_dir = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
    effective_state_base_dir = Path(state_base_dir) if state_base_dir is not None else effective_base_dir
    limits = resolve_opend_fetch_limits(dict(cfg) if isinstance(cfg, Mapping) else None).market_snapshot
    diagnostics["host"] = effective_host
    diagnostics["port"] = effective_port

    try:
        gateway = build_ready_futu_quote_gateway(
            host=effective_host,
            port=effective_port,
            is_option_chain_cache_enabled=False,
            **provider_kwargs,
        )
    except Exception as exc:
        diagnostics["status"] = "source_unavailable"
        for symbol in symbols:
            diagnostics["missing_symbols"].append(symbol)
        diagnostics["errors"].append(
            {
                "stage": "gateway",
                "error_code": type(exc).__name__,
                "message": str(exc),
            }
        )
        warnings = [f"assigned stock quote refresh failed: {type(exc).__name__}: {exc}"]
        return AssignedStockQuoteRefreshResult([], diagnostics, warnings)

    quote_snapshots: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    try:
        for symbol in symbols:
            try:
                underlier = normalize_underlier(symbol, base_dir=effective_base_dir)
                if preserve_evidence:
                    observations = get_underlier_observations(
                        gateway, [underlier.code], market=underlier.market,
                        base_dir=effective_state_base_dir, snapshot_limit=limits,
                        snapshot_batch_size=1, **provider_kwargs,
                    )
                    observation = observations.get(underlier.code)
                    if observation is None:
                        diagnostics["missing_symbols"].append(symbol)
                        continue
                    evidence = observation.to_dict()
                    market_at = evidence.get("observed_at_utc")
                    market_ms = int(datetime.fromisoformat(str(market_at)).timestamp() * 1000) if market_at else None
                    quote_snapshots.append({
                        "symbol": symbol, "spot": evidence.get("last_price"),
                        "quote_time_ms": market_ms, "quote_observed_at_ms": int(now_ms()),
                        "quote_source": quote_source, "quote_status": evidence.get("status"),
                        "price_field": "last_price", "quote_observation": evidence,
                    })
                    diagnostics["refreshed_symbols"].append(symbol)
                    continue
                spot = get_spot_opend(
                    gateway,
                    underlier.code,
                    base_dir=effective_state_base_dir,
                    snapshot_max_wait_sec=limits.max_wait_sec,
                    snapshot_window_sec=limits.window_sec,
                    snapshot_max_calls=limits.max_calls,
                    errors=errors,
                    **provider_kwargs,
                )
            except Exception as exc:
                errors.append(
                    {
                        "stage": "underlier_snapshot",
                        "code": symbol,
                        "error_code": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                spot = None
            if spot is None:
                diagnostics["missing_symbols"].append(symbol)
                continue
            quote_snapshots.append(
                {
                    "symbol": symbol,
                    "spot": float(spot),
                    "quote_time_ms": int(now_ms()),
                    "quote_source": quote_source,
                    "quote_status": "fresh",
                }
            )
            diagnostics["refreshed_symbols"].append(symbol)
    finally:
        try:
            gateway.close()
        except Exception:
            pass

    diagnostics["errors"] = errors
    diagnostics["quote_count"] = len(quote_snapshots)
    diagnostics["status"] = "ok" if quote_snapshots else "missing_quote"
    warnings: list[str] = []
    if diagnostics["missing_symbols"]:
        warnings.append(
            "assigned stock quote refresh missing: "
            + ", ".join(str(symbol) for symbol in diagnostics["missing_symbols"])
        )
    return AssignedStockQuoteRefreshResult(quote_snapshots, diagnostics, warnings)


__all__ = ["AssignedStockQuoteRefreshResult", "refresh_assigned_stock_quote_snapshots"]


def assigned_stock_quote_is_usable(
    snapshot: Mapping[str, Any], valuation_at_ms: int, *, expected_currency: str | None = None,
) -> bool:
    """Revalidate original observations with the canonical quote-age policy.

    Closed quotes without an explicit validated carry receipt remain unavailable;
    receiving them today must not make their market time fresh.
    """
    from src.application.opening_quote_evidence import normalize_underlier_observation

    try:
        spot = Decimal(str(snapshot.get("spot")))
        if not spot.is_finite() or spot <= 0 or not norm_symbol(snapshot.get("symbol")):
            return False
    except (InvalidOperation, ValueError, TypeError):
        return False
    from domain.domain.symbol_identity import symbol_currency

    currency = expected_currency or symbol_currency(norm_symbol(snapshot.get("symbol")))
    if snapshot.get("currency") and snapshot["currency"] != currency:
        return False
    market_evidence = snapshot.get("market_evidence")
    if isinstance(market_evidence, dict):
        from domain.domain.performance.strategy_evaluation import eligible_stock_marks

        try:
            marks = eligible_stock_marks(market=market_evidence, at_ms=valuation_at_ms)
            return any(
                norm_symbol(mark.get("symbol")) == norm_symbol(snapshot.get("symbol"))
                and mark.get("currency") == currency
                and Decimal(str(mark.get("price"))) == Decimal(str(snapshot.get("spot")))
                and int(snapshot.get("quote_time_ms")) == int(
                    mark["effective_at_ms"] if mark.get("effective_at_ms") is not None
                    else mark["available_at_ms"])
                for mark in marks
            )
        except (ValueError, TypeError, InvalidOperation):
            return False
    evidence = snapshot.get("quote_observation")
    if not isinstance(evidence, Mapping):
        # Frozen inputs must explicitly carry a qualification receipt, not just
        # a scalar with a caller-selected 'fresh' label.
        return False
    try:
        if (symbol_currency(norm_symbol(evidence.get("code"))) != currency
                or norm_symbol(evidence.get("code")) != norm_symbol(snapshot.get("symbol"))
                or Decimal(str(evidence.get("last_price"))) != spot):
            return False
        market_stamp = datetime.fromisoformat(str(evidence.get("observed_at_utc")))
        if market_stamp.utcoffset() is None or int(market_stamp.timestamp() * 1000) != int(snapshot.get("quote_time_ms")):
            return False
    except (ValueError, TypeError, InvalidOperation):
        return False
    observation = normalize_underlier_observation(
        code=str(evidence.get("code") or ""), market=str(evidence.get("market") or ""),
        snapshot_row={"code": evidence.get("code"), "last_price": evidence.get("last_price"),
                      "update_time": evidence.get("update_time"), "sec_status": evidence.get("sec_status"),
                      "suspension": evidence.get("suspension")},
        market_state_row={"code": evidence.get("code"), "market_state": evidence.get("market_state")},
        now_utc=datetime.fromtimestamp(valuation_at_ms / 1000, timezone.utc),
    )
    if observation.status != "ready" or observation.observed_at_utc is None:
        return False
    return int(datetime.fromisoformat(observation.observed_at_utc).timestamp() * 1000) == int(snapshot["quote_time_ms"])
