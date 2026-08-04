from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from domain.domain.fetch_source import is_futu_fetch_source, resolve_symbol_fetch_source
from src.application.config_profiles import apply_profiles
from src.application.config_sections import resolve_templates_config, resolve_watchlist_config


DEFAULT_FUTU_HOST = "127.0.0.1"
DEFAULT_FUTU_PORT = 11111


@dataclass(frozen=True)
class FutuQuoteRouteMember:
    config_key: str | None
    market: str
    symbol: str
    source: str
    host: str
    port: int


@dataclass(frozen=True)
class ResolvedFutuQuoteRoute:
    status: str
    host: str | None
    port: int | None
    members: tuple[FutuQuoteRouteMember, ...]
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def runtime_config_market(config: Mapping[str, Any], *, fallback: str | None = None) -> str:
    generated = config.get("_generated")
    raw = generated.get("market") if isinstance(generated, Mapping) else None
    value = str(raw or config.get("market") or fallback or "").strip().upper()
    return value


def normalize_futu_quote_endpoint(fetch: Mapping[str, Any]) -> tuple[str, int]:
    host = str(fetch.get("host") or DEFAULT_FUTU_HOST).strip().lower()
    raw_port = fetch.get("port") if fetch.get("port") not in (None, "") else DEFAULT_FUTU_PORT
    if not host:
        raise ValueError("Futu quote host is empty")
    if isinstance(raw_port, bool):
        raise ValueError("Futu quote port is invalid")
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("Futu quote port is invalid") from exc
    if str(port) != str(raw_port).strip() or not 1 <= port <= 65535:
        raise ValueError("Futu quote port is invalid")
    return host, port


def resolve_futu_quote_route(
    config: Mapping[str, Any],
    *,
    config_key: str | None = None,
    market: str | None = None,
) -> ResolvedFutuQuoteRoute:
    cfg = dict(config) if isinstance(config, Mapping) else {}
    resolved_market = runtime_config_market(cfg, fallback=market)
    profiles = resolve_templates_config(cfg)
    members: list[FutuQuoteRouteMember] = []
    errors: list[str] = []
    for raw in resolve_watchlist_config(cfg):
        try:
            item = apply_profiles(raw, profiles)
        except Exception as exc:
            errors.append(f"symbol profile resolution failed: {exc}")
            continue
        fetch = item.get("fetch") if isinstance(item.get("fetch"), Mapping) else {}
        source, _decision = resolve_symbol_fetch_source(fetch)
        if not is_futu_fetch_source(source):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        try:
            host, port = normalize_futu_quote_endpoint(fetch)
        except ValueError as exc:
            errors.append(f"{symbol or '<unknown>'}: {exc}")
            continue
        members.append(
            FutuQuoteRouteMember(
                config_key=str(config_key or "").strip().lower() or None,
                market=resolved_market,
                symbol=symbol,
                source="opend",
                host=host,
                port=port,
            )
        )
    endpoints = {(member.host, member.port) for member in members}
    if errors:
        return ResolvedFutuQuoteRoute("conflict", None, None, tuple(members), tuple(errors))
    if not members:
        return ResolvedFutuQuoteRoute("missing", None, None, (), ("no effective Futu fetch binding",))
    if len(endpoints) != 1:
        return ResolvedFutuQuoteRoute(
            "conflict",
            None,
            None,
            tuple(members),
            ("effective Futu fetch bindings use multiple endpoints",),
        )
    host, port = next(iter(endpoints))
    return ResolvedFutuQuoteRoute("ok", host, port, tuple(members))


def resolve_shared_futu_quote_route(
    configs: Iterable[tuple[str | None, Mapping[str, Any]]],
) -> ResolvedFutuQuoteRoute:
    members: list[FutuQuoteRouteMember] = []
    errors: list[str] = []
    routes: list[ResolvedFutuQuoteRoute] = []
    for config_key, config in configs:
        route = resolve_futu_quote_route(config, config_key=config_key)
        routes.append(route)
        members.extend(route.members)
        if not route.ok:
            errors.extend(f"{config_key or '<runtime>'}: {item}" for item in route.errors)
    if errors:
        status = "missing" if routes and all(route.status == "missing" for route in routes) else "conflict"
        return ResolvedFutuQuoteRoute(status, None, None, tuple(members), tuple(errors))
    endpoints = {(route.host, route.port) for route in routes}
    if len(endpoints) != 1:
        return ResolvedFutuQuoteRoute(
            "conflict", None, None, tuple(members), ("runtime markets do not share one Futu quote endpoint",)
        )
    host, port = next(iter(endpoints))
    return ResolvedFutuQuoteRoute("ok", host, port, tuple(members))


__all__ = [
    "FutuQuoteRouteMember",
    "ResolvedFutuQuoteRoute",
    "normalize_futu_quote_endpoint",
    "resolve_futu_quote_route",
    "resolve_shared_futu_quote_route",
    "runtime_config_market",
]
