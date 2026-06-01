from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.application.events.prefetch import normalize_event_source_provider
from src.application.events.source_futu import fetch_symbol_events_futu
from src.application.events.source_yfinance import EventSourceError, classify_event_source_error, fetch_symbol_events_yfinance
from src.infrastructure.futu_gateway import build_ready_futu_gateway


def probe_event_source(
    *,
    provider: str,
    symbols: list[str],
    host: str = "127.0.0.1",
    port: int = 11111,
) -> dict[str, Any]:
    provider_name = normalize_event_source_provider(provider)
    symbol_list = [str(symbol).strip() for symbol in symbols if str(symbol).strip()]
    if not symbol_list:
        return {
            "ok": False,
            "provider": provider_name,
            "error": {"code": "INPUT_ERROR", "message": "at least one symbol is required"},
            "symbols": {},
        }

    if provider_name == "all":
        return _probe_all(symbols=symbol_list, host=host, port=port)
    if provider_name == "futu":
        return _probe_futu(symbols=symbol_list, host=host, port=port)
    if provider_name == "yfinance":
        return _probe_yfinance(symbols=symbol_list)
    return {
        "ok": False,
        "provider": provider_name,
        "error": {"code": "INPUT_ERROR", "message": f"unsupported event source provider: {provider}"},
        "symbols": {},
    }


def _probe_all(*, symbols: list[str], host: str, port: int) -> dict[str, Any]:
    created_at = datetime.now(timezone.utc).isoformat()
    providers = {
        "futu": _probe_futu(symbols=symbols, host=host, port=port),
        "yfinance": _probe_yfinance(symbols=symbols),
    }
    rows: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        source_results = {
            provider: payload.get("symbols", {}).get(symbol, {})
            for provider, payload in providers.items()
            if isinstance(payload.get("symbols"), dict)
        }
        rows[symbol] = {
            "ok": any(bool(item.get("ok")) for item in source_results.values() if isinstance(item, dict)),
            "source_results": source_results,
        }
    errors = sum(1 for row in rows.values() if not row.get("ok"))
    return {
        "ok": errors == 0,
        "provider": "all",
        "created_at": created_at,
        "providers": providers,
        "symbols": rows,
        "summary": {
            "symbols_total": len(symbols),
            "ok": len(symbols) - errors,
            "errors": errors,
            "provider_ok": {provider: bool(payload.get("ok")) for provider, payload in providers.items()},
        },
    }


def _probe_futu(*, symbols: list[str], host: str, port: int) -> dict[str, Any]:
    created_at = datetime.now(timezone.utc).isoformat()
    rows: dict[str, dict[str, Any]] = {}
    gateway = None
    try:
        gateway = build_ready_futu_gateway(host=str(host), port=int(port), is_option_chain_cache_enabled=False)
    except Exception as exc:
        error = _error_payload(exc, provider="futu")
        return {
            "ok": False,
            "provider": "futu",
            "created_at": created_at,
            "endpoint": {"host": str(host), "port": int(port)},
            "symbols": {symbol: {"ok": False, **error} for symbol in symbols},
            "summary": {"symbols_total": len(symbols), "ok": 0, "errors": len(symbols)},
        }
    try:
        for symbol in symbols:
            try:
                events = fetch_symbol_events_futu(symbol, gateway=gateway, close_gateway=False)
                rows[symbol] = {"ok": True, "events": events, "event_count": len(events)}
            except Exception as exc:
                rows[symbol] = {"ok": False, **_error_payload(exc, provider="futu")}
    finally:
        try:
            gateway.close()
        except Exception:
            pass
    errors = sum(1 for row in rows.values() if not row.get("ok"))
    return {
        "ok": errors == 0,
        "provider": "futu",
        "created_at": created_at,
        "endpoint": {"host": str(host), "port": int(port)},
        "symbols": rows,
        "summary": {"symbols_total": len(symbols), "ok": len(symbols) - errors, "errors": errors},
    }


def _probe_yfinance(*, symbols: list[str]) -> dict[str, Any]:
    created_at = datetime.now(timezone.utc).isoformat()
    rows: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        try:
            events = fetch_symbol_events_yfinance(symbol)
            rows[symbol] = {"ok": True, "events": events, "event_count": len(events)}
        except Exception as exc:
            rows[symbol] = {"ok": False, **_error_payload(exc, provider="yfinance")}
    errors = sum(1 for row in rows.values() if not row.get("ok"))
    return {
        "ok": errors == 0,
        "provider": "yfinance",
        "created_at": created_at,
        "symbols": rows,
        "summary": {"symbols_total": len(symbols), "ok": len(symbols) - errors, "errors": errors},
    }


def _error_payload(exc: Exception, *, provider: str) -> dict[str, Any]:
    if isinstance(exc, EventSourceError):
        code = exc.error_code
    elif provider == "futu":
        from src.application.events.source_futu import classify_futu_event_error

        code = classify_futu_event_error(f"{type(exc).__name__}: {exc}")
    else:
        code = classify_event_source_error(f"{type(exc).__name__}: {exc}")
    return {
        "error_code": code or "source_error",
        "error_type": type(exc).__name__,
        "source_error": str(exc),
    }
