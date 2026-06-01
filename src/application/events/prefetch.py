from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from domain.domain.symbol_identity import canonical_symbol
from src.application.config_loader import resolve_templates_config, resolve_watchlist_config
from src.application.config_profiles import apply_profiles
from src.application.events.orchestrator import (
    EventFetcher,
    normalize_event_source_provider,
    resolve_event_source_snapshot,
)
from src.application.events.store import EventFetchResult, EventStore
from src.application.pipeline_watchlist import resolve_watchlist_item_runtime_config
from src.application.runtime_config_paths import write_json_atomic
from src.application.yield_enhancement_config import derive_yield_enhancement_policy


def prefetch_event_data(
    *,
    base: Path,
    cfg: dict[str, Any],
    snapshot_path: Path,
    store_path: Path | None = None,
    fetcher: Callable[[str], list[dict[str, Any]]] | None = None,
    fetchers: dict[str, EventFetcher] | None = None,
    provider: str | None = None,
    force_refresh: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or datetime.now(timezone.utc)
    plan = build_event_prefetch_plan(cfg)
    resolved_store_path = store_path or default_event_store_path(base)
    store_kwargs = _store_kwargs(cfg)

    if fetcher is None:
        resolved = resolve_event_source_snapshot(
            symbols=plan["symbols"],
            cfg=cfg,
            store_path=resolved_store_path,
            store_kwargs=store_kwargs,
            fetchers=fetchers,
            provider_override=provider,
            force_refresh=bool(force_refresh),
            now=now_dt,
        )
        snapshot = {
            "schema_version": 1,
            "provider": resolved["provider"],
            "event_source_policy": resolved.get("event_source_policy", {}),
            "created_at": now_dt.isoformat(),
            "symbols": resolved["symbols"],
            "plan": plan,
            "summary": _resolved_summary(plan, resolved.get("summary", {})),
        }
        snapshot_path = Path(snapshot_path).resolve()
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(snapshot_path, snapshot)
        return snapshot

    provider_name = normalize_event_source_provider(provider or _event_source_provider(cfg))
    store = EventStore(resolved_store_path, provider=provider_name, **store_kwargs)
    results: dict[str, EventFetchResult] = {}

    for symbol in plan["symbols"]:
        results[symbol] = store.resolve(
            symbol,
            fetcher=fetcher,
            now=now_dt,
            force_refresh=bool(force_refresh),
        )

    status_counts = Counter(result.source_status for result in results.values())
    cache_counts = Counter(result.cache_status for result in results.values())
    error_counts = Counter(result.error_code for result in results.values() if result.error_code)
    snapshot = {
        "schema_version": 1,
        "provider": provider_name,
        "created_at": now_dt.isoformat(),
        "symbols": {symbol: result.to_snapshot_item() for symbol, result in results.items()},
        "plan": plan,
        "summary": {
            "symbols_total": int(plan["symbols_total"]),
            "unique_symbols_total": int(plan["unique_symbols_total"]),
            "deduped_count": int(plan["deduped_count"]),
            "fetch_attempts": int(cache_counts.get("fetched", 0) + cache_counts.get("fetch_error", 0) + cache_counts.get("stale_after_error", 0)),
            "cache_hit_ok": int(cache_counts.get("hit_ok", 0)),
            "cache_hit_stale": int(cache_counts.get("stale_after_error", 0) + cache_counts.get("stale_provider_cooldown", 0)),
            "provider_cooldown": int(cache_counts.get("provider_cooldown", 0)),
            "rate_limited": int(error_counts.get("rate_limited", 0)),
            "errors": int(status_counts.get("error", 0)),
            "stale": int(status_counts.get("stale", 0)),
            "ok": int(status_counts.get("ok", 0)),
            "source_status_counts": dict(status_counts),
            "cache_status_counts": dict(cache_counts),
            "error_code_counts": dict(error_counts),
        },
    }
    snapshot_path = Path(snapshot_path).resolve()
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(snapshot_path, snapshot)
    return snapshot


def build_event_prefetch_plan(cfg: dict[str, Any]) -> dict[str, Any]:
    profiles = resolve_templates_config(cfg)
    raw_items = [item for item in resolve_watchlist_config(cfg) if isinstance(item, dict) and item.get("symbol")]
    symbols: list[str] = []
    reasons: dict[str, list[str]] = {}
    skipped: list[dict[str, Any]] = []
    for item in raw_items:
        resolved = resolve_watchlist_item_runtime_config(
            item=item,
            profiles=profiles,
            apply_profiles_fn=apply_profiles,
        )
        symbol = _canonical(resolved.get("symbol") or item.get("symbol"))
        if not symbol:
            skipped.append({"symbol": item.get("symbol"), "reason": "symbol_not_canonical"})
            continue
        item_reasons = _event_reasons(resolved)
        if not item_reasons:
            skipped.append({"symbol": symbol, "reason": "event_risk_disabled_or_no_active_strategy"})
            continue
        symbols.append(symbol)
        reasons.setdefault(symbol, [])
        reasons[symbol].extend(item_reasons)

    unique_symbols = list(dict.fromkeys(symbols))
    return {
        "symbols_total": len(symbols),
        "unique_symbols_total": len(unique_symbols),
        "deduped_count": max(0, len(symbols) - len(unique_symbols)),
        "symbols": unique_symbols,
        "reasons": {key: sorted(set(value)) for key, value in reasons.items()},
        "skipped": skipped,
    }


def default_event_store_path(base: Path) -> Path:
    return (Path(base) / "output_shared" / "state" / "event_store.json").resolve()


def _event_source_provider(cfg: dict[str, Any]) -> str:
    runtime = cfg.get("runtime") if isinstance(cfg.get("runtime"), dict) else {}
    source_cfg = runtime.get("event_risk_source") if isinstance(runtime.get("event_risk_source"), dict) else {}
    return normalize_event_source_provider(
        source_cfg.get("provider")
        or source_cfg.get("source")
        or runtime.get("event_risk_provider")
        or "yfinance"
    )


def _event_reasons(item: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    sell_put = item.get("sell_put") if isinstance(item.get("sell_put"), dict) else {}
    sell_call = item.get("sell_call") if isinstance(item.get("sell_call"), dict) else {}
    yield_enhancement = item.get("yield_enhancement") if isinstance(item.get("yield_enhancement"), dict) else {}
    yield_policy = derive_yield_enhancement_policy(yield_enhancement, sell_put)
    put_event = item.get("_global_sell_put_event_risk") if isinstance(item.get("_global_sell_put_event_risk"), dict) else {}
    call_event = item.get("_global_sell_call_event_risk") if isinstance(item.get("_global_sell_call_event_risk"), dict) else {}
    if bool(sell_put.get("enabled", True)) and bool(put_event.get("enabled", True)):
        reasons.append("sell_put")
    if bool(yield_policy.enabled) and bool(put_event.get("enabled", True)):
        reasons.append("yield_enhancement")
    if bool(sell_call.get("enabled", False)) and bool(call_event.get("enabled", True)):
        reasons.append("sell_call")
    return reasons


def _canonical(value: Any) -> str:
    return canonical_symbol(value) or str(value or "").strip().upper()


def _store_kwargs(cfg: dict[str, Any]) -> dict[str, int]:
    runtime = cfg.get("runtime") if isinstance(cfg.get("runtime"), dict) else {}
    event_cfg = runtime.get("event_risk_cache") if isinstance(runtime.get("event_risk_cache"), dict) else {}
    return {
        "success_ttl_seconds": _positive_int(event_cfg.get("success_ttl_seconds"), 86400),
        "stale_ttl_seconds": _positive_int(event_cfg.get("stale_ttl_seconds"), 30 * 86400),
        "error_cooldown_seconds": _positive_int(event_cfg.get("error_cooldown_seconds"), 30 * 60),
        "rate_limit_cooldown_seconds": _positive_int(event_cfg.get("rate_limit_cooldown_seconds"), 60 * 60),
    }


def _resolved_summary(plan: dict[str, Any], resolved_summary: dict[str, Any]) -> dict[str, Any]:
    status_counts = resolved_summary.get("source_status_counts") if isinstance(resolved_summary.get("source_status_counts"), dict) else {}
    cache_counts = resolved_summary.get("cache_status_counts") if isinstance(resolved_summary.get("cache_status_counts"), dict) else {}
    error_counts = resolved_summary.get("error_code_counts") if isinstance(resolved_summary.get("error_code_counts"), dict) else {}
    return {
        "symbols_total": int(plan["symbols_total"]),
        "unique_symbols_total": int(plan["unique_symbols_total"]),
        "deduped_count": int(plan["deduped_count"]),
        "fetch_attempts": int(resolved_summary.get("provider_fetch_attempts") or 0),
        "cache_hit_ok": int(cache_counts.get("hit_ok", 0)),
        "cache_hit_stale": int(cache_counts.get("stale_after_error", 0) + cache_counts.get("stale_provider_cooldown", 0)),
        "provider_cooldown": int(cache_counts.get("provider_cooldown", 0)),
        "rate_limited": int(error_counts.get("rate_limited", 0)),
        "errors": int(resolved_summary.get("all_sources_failed") or 0),
        "stale": int(resolved_summary.get("stale") or 0),
        "ok": int(resolved_summary.get("resolved_ok") or 0),
        "fallback_used": int(resolved_summary.get("fallback_used") or 0),
        "all_sources_failed": int(resolved_summary.get("all_sources_failed") or 0),
        "source_status_counts": dict(status_counts),
        "cache_status_counts": dict(cache_counts),
        "error_code_counts": dict(error_counts),
        "provider_status_counts": resolved_summary.get("provider_status_counts") or {},
        "provider_cache_counts": resolved_summary.get("provider_cache_counts") or {},
    }


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    return parsed if parsed > 0 else int(default)
