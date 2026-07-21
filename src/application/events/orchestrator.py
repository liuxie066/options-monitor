from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from domain.domain.symbol_identity import canonical_symbol
from src.application.events.source_futu import fetch_symbol_event_evidence_futu
from src.application.events.source_yfinance import fetch_symbol_event_evidence_yfinance
from src.application.events.store import EventFetchResult, EventStore


EventFetcher = Callable[[str], Any]
DEFAULT_EVENT_SOURCE_PROVIDER = "futu"


def normalize_event_source_provider(value: Any) -> str:
    provider = str(value or "").strip().lower()
    if not provider:
        return DEFAULT_EVENT_SOURCE_PROVIDER
    if provider in {"yf", "yahoo", "yahoo_finance"}:
        return "yfinance"
    if provider in {"futu", "opend", "futu_opend", "futu-openapi"}:
        return "futu"
    return provider


def resolve_event_source_snapshot(
    *,
    symbols: list[str],
    cfg: dict[str, Any],
    store_path: Path,
    store_kwargs: dict[str, int],
    fetchers: dict[str, EventFetcher] | None = None,
    provider_override: str | None = None,
    force_refresh: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or datetime.now(timezone.utc)
    policy = build_event_source_policy(cfg, provider_override=provider_override)
    results: dict[str, dict[str, Any]] = {}

    for symbol in symbols:
        results[symbol] = resolve_symbol_events(
            symbol,
            cfg=cfg,
            policy=policy,
            store_path=store_path,
            store_kwargs=store_kwargs,
            fetchers=fetchers,
            force_refresh=force_refresh,
            now=now_dt,
        )

    return {
        "schema_version": 1,
        "provider": "resolved",
        "event_source_policy": {
            "mode": policy["mode"],
            "default_provider": policy["default_provider"],
        },
        "symbols": results,
        "summary": summarize_resolved_results(results),
    }


def resolve_symbol_events(
    symbol: str,
    *,
    cfg: dict[str, Any],
    policy: dict[str, Any],
    store_path: Path,
    store_kwargs: dict[str, int],
    fetchers: dict[str, EventFetcher] | None = None,
    force_refresh: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or datetime.now(timezone.utc)
    sym = _canonical(symbol)
    chain = provider_chain_for_symbol(sym, policy=policy)
    if not chain:
        chain = [policy["default_provider"]]

    source_results: dict[str, dict[str, Any]] = {}
    stale_candidate: tuple[str, EventFetchResult] | None = None
    selected: tuple[str, EventFetchResult] | None = None

    for provider in chain:
        fetcher = (fetchers or {}).get(provider) or build_event_fetcher(provider, cfg)
        store = EventStore(store_path, provider=provider, **store_kwargs)
        result = store.resolve(sym, fetcher=fetcher, now=now_dt, force_refresh=force_refresh)
        source_results[provider] = result.to_snapshot_item()
        if result.source_status == "ok":
            if selected is None:
                selected = (provider, result)
            if policy["mode"] != "shadow":
                break
            continue
        if result.source_status == "stale" and stale_candidate is None:
            stale_candidate = (provider, result)

    if selected:
        selected_provider, selected_result = selected
        first_provider = chain[0] if chain else selected_provider
        status = "ok" if selected_provider == first_provider else "ok_with_fallback"
        if policy["mode"] == "shadow":
            status = "ok"
        return _resolved_item(
            symbol=sym,
            provider_chain=chain,
            selected_provider=selected_provider,
            selected_result=selected_result,
            source_results=source_results,
            source_status=status,
        )

    if stale_candidate:
        selected_provider, selected_result = stale_candidate
        return _resolved_item(
            symbol=sym,
            provider_chain=chain,
            selected_provider=selected_provider,
            selected_result=selected_result,
            source_results=source_results,
            source_status="stale",
        )

    return {
        "provider": "resolved",
        "symbol": sym,
        "selected_provider": "",
        "provider_chain": chain,
        "events": [],
        "coverage": {},
        "source_status": "error",
        "source_error": _join_source_errors(source_results),
        "error_code": _dominant_error_code(source_results),
        "source_results": source_results,
        "cache_status": "all_sources_failed",
    }


def build_event_source_policy(cfg: dict[str, Any], *, provider_override: str | None = None) -> dict[str, Any]:
    source_cfg = _event_source_cfg(cfg)
    if provider_override:
        provider = normalize_event_source_provider(provider_override)
        return {
            "mode": "single",
            "default_provider": provider,
            "providers": {provider: {"enabled": True}},
            "market_rules": {},
            "chain": [provider],
        }

    default_provider = normalize_event_source_provider(
        source_cfg.get("default_provider")
        or source_cfg.get("provider")
        or source_cfg.get("source")
        or _legacy_event_source_provider(cfg)
        or DEFAULT_EVENT_SOURCE_PROVIDER
    )
    mode = str(source_cfg.get("mode") or "").strip().lower()
    if not mode:
        mode = "primary_fallback" if isinstance(source_cfg.get("providers"), dict) else "single"
    if mode in {"fallback", "primary-fallback"}:
        mode = "primary_fallback"
    if mode not in {"single", "primary_fallback", "shadow"}:
        mode = "single"

    providers = _providers_cfg(source_cfg, default_provider=default_provider)
    chain = _normalize_provider_chain(source_cfg.get("chain") or source_cfg.get("provider_chain"))
    if not chain and mode == "single":
        chain = [default_provider]
    if not chain:
        chain = _chain_from_provider_roles(providers, default_provider=default_provider)

    return {
        "mode": mode,
        "default_provider": default_provider,
        "providers": providers,
        "market_rules": _market_rules(source_cfg),
        "chain": [p for p in chain if _provider_enabled(providers, p)] or [default_provider],
    }


def provider_chain_for_symbol(symbol: str, *, policy: dict[str, Any]) -> list[str]:
    market = _symbol_market(symbol)
    market_rules = policy.get("market_rules") if isinstance(policy.get("market_rules"), dict) else {}
    market_rule = market_rules.get(market) if isinstance(market_rules.get(market), dict) else {}
    chain = _normalize_provider_chain(market_rule.get("chain") or market_rule.get("providers"))
    if not chain:
        chain = list(policy.get("chain") or [])
    providers = policy.get("providers") if isinstance(policy.get("providers"), dict) else {}
    return [p for p in chain if _provider_enabled(providers, p)]


def build_event_fetcher(provider: str, cfg: dict[str, Any]) -> EventFetcher:
    provider_name = normalize_event_source_provider(provider)
    if provider_name == "yfinance":
        return fetch_symbol_event_evidence_yfinance
    if provider_name == "futu":
        futu_cfg = event_source_futu_cfg(cfg)
        host = str(futu_cfg.get("host") or "127.0.0.1")
        port = _positive_int(futu_cfg.get("port"), 11111)
        return lambda symbol: fetch_symbol_event_evidence_futu(symbol, host=host, port=port)
    raise ValueError(f"unsupported event source provider: {provider}")


def event_source_futu_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    source_cfg = _event_source_cfg(cfg)
    futu_cfg = source_cfg.get("futu") if isinstance(source_cfg.get("futu"), dict) else {}
    providers = source_cfg.get("providers") if isinstance(source_cfg.get("providers"), dict) else {}
    provider_cfg = providers.get("futu") if isinstance(providers.get("futu"), dict) else {}
    portfolio = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    portfolio_futu = portfolio.get("futu") if isinstance(portfolio.get("futu"), dict) else {}
    defaults = cfg.get("defaults") if isinstance(cfg.get("defaults"), dict) else {}
    default_portfolio = defaults.get("portfolio") if isinstance(defaults.get("portfolio"), dict) else {}
    default_futu = default_portfolio.get("futu") if isinstance(default_portfolio.get("futu"), dict) else {}
    out: dict[str, Any] = {}
    out.update(default_futu)
    out.update(portfolio_futu)
    out.update(provider_cfg)
    out.update(futu_cfg)
    return out


def summarize_resolved_results(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(str(item.get("source_status") or "") for item in results.values())
    cache_counts = Counter(str(item.get("cache_status") or "") for item in results.values())
    error_counts = Counter(str(item.get("error_code") or "") for item in results.values() if item.get("error_code"))
    provider_status_counts: dict[str, dict[str, int]] = {}
    provider_cache_counts: dict[str, dict[str, int]] = {}
    provider_fetch_attempts = 0

    for item in results.values():
        source_results = item.get("source_results") if isinstance(item.get("source_results"), dict) else {}
        for provider, result in source_results.items():
            if not isinstance(result, dict):
                continue
            status = str(result.get("source_status") or "")
            cache_status = str(result.get("cache_status") or "")
            provider_status_counts.setdefault(str(provider), {})
            provider_cache_counts.setdefault(str(provider), {})
            provider_status_counts[str(provider)][status] = provider_status_counts[str(provider)].get(status, 0) + 1
            provider_cache_counts[str(provider)][cache_status] = provider_cache_counts[str(provider)].get(cache_status, 0) + 1
            if cache_status in {"fetched", "fetch_error", "stale_after_error"}:
                provider_fetch_attempts += 1

    return {
        "resolved_ok": int(status_counts.get("ok", 0) + status_counts.get("ok_with_fallback", 0)),
        "fallback_used": int(status_counts.get("ok_with_fallback", 0)),
        "all_sources_failed": int(status_counts.get("error", 0)),
        "stale": int(status_counts.get("stale", 0)),
        "provider_fetch_attempts": int(provider_fetch_attempts),
        "source_status_counts": dict(status_counts),
        "cache_status_counts": dict(cache_counts),
        "error_code_counts": dict(error_counts),
        "provider_status_counts": provider_status_counts,
        "provider_cache_counts": provider_cache_counts,
    }


def _resolved_item(
    *,
    symbol: str,
    provider_chain: list[str],
    selected_provider: str,
    selected_result: EventFetchResult,
    source_results: dict[str, dict[str, Any]],
    source_status: str,
) -> dict[str, Any]:
    return {
        "provider": "resolved",
        "symbol": symbol,
        "selected_provider": selected_provider,
        "provider_chain": provider_chain,
        "events": list(selected_result.events),
        "coverage": dict(selected_result.coverage),
        "source_status": source_status,
        "source_error": selected_result.source_error,
        "error_code": selected_result.error_code,
        "fetched_at": selected_result.fetched_at,
        "last_success_at": selected_result.last_success_at,
        "last_error_at": selected_result.last_error_at,
        "blocked_until": selected_result.blocked_until,
        "cache_status": selected_result.cache_status,
        "source_results": source_results,
    }


def _providers_cfg(source_cfg: dict[str, Any], *, default_provider: str) -> dict[str, dict[str, Any]]:
    raw = source_cfg.get("providers") if isinstance(source_cfg.get("providers"), dict) else {}
    providers: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        provider = normalize_event_source_provider(key)
        providers[provider] = dict(value) if isinstance(value, dict) else {"enabled": bool(value)}
    providers.setdefault(default_provider, {"enabled": True, "role": "primary"})
    for provider, cfg in providers.items():
        cfg.setdefault("enabled", True)
        if provider == default_provider:
            cfg.setdefault("role", "primary")
    return providers


def _market_rules(source_cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = source_cfg.get("market_rules") if isinstance(source_cfg.get("market_rules"), dict) else {}
    rules: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        market = str(key or "").strip().lower()
        if market and isinstance(value, dict):
            rules[market] = value
    return rules


def _chain_from_provider_roles(providers: dict[str, dict[str, Any]], *, default_provider: str) -> list[str]:
    chain = [default_provider]
    for role in ("primary", "fallback", "shadow"):
        for provider, cfg in providers.items():
            if provider in chain:
                continue
            if str(cfg.get("role") or "").strip().lower() == role:
                chain.append(provider)
    for provider in providers:
        if provider not in chain:
            chain.append(provider)
    return chain


def _normalize_provider_chain(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_values = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple)):
        raw_values = [str(item or "").strip() for item in value]
    else:
        raw_values = []
    out: list[str] = []
    for raw in raw_values:
        provider = normalize_event_source_provider(raw)
        if provider and provider not in out:
            out.append(provider)
    return out


def _provider_enabled(providers: dict[str, dict[str, Any]], provider: str) -> bool:
    cfg = providers.get(provider)
    if cfg is None:
        return True
    return bool(cfg.get("enabled", True))


def _event_source_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    runtime = cfg.get("runtime") if isinstance(cfg.get("runtime"), dict) else {}
    source_cfg = runtime.get("event_risk_source") if isinstance(runtime.get("event_risk_source"), dict) else {}
    return source_cfg


def _legacy_event_source_provider(cfg: dict[str, Any]) -> str:
    runtime = cfg.get("runtime") if isinstance(cfg.get("runtime"), dict) else {}
    return normalize_event_source_provider(runtime.get("event_risk_provider") or DEFAULT_EVENT_SOURCE_PROVIDER)


def _symbol_market(symbol: str) -> str:
    sym = _canonical(symbol)
    if sym.endswith(".HK"):
        return "hk"
    return "us"


def _join_source_errors(source_results: dict[str, dict[str, Any]]) -> str:
    parts: list[str] = []
    for provider, result in source_results.items():
        if not isinstance(result, dict):
            continue
        err = str(result.get("source_error") or "").strip()
        if err:
            parts.append(f"{provider}:{err}")
    return "; ".join(parts) or "all event sources failed"


def _dominant_error_code(source_results: dict[str, dict[str, Any]]) -> str:
    for result in source_results.values():
        if isinstance(result, dict) and result.get("error_code") == "rate_limited":
            return "rate_limited"
    for result in source_results.values():
        if isinstance(result, dict) and result.get("error_code"):
            return str(result.get("error_code"))
    return "source_error"


def _canonical(value: Any) -> str:
    return canonical_symbol(value) or str(value or "").strip().upper()


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    return parsed if parsed > 0 else int(default)
