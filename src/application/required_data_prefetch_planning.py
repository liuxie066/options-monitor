from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from domain.domain.sell_call_config import resolve_effective_sell_call_min_strike
from domain.domain.candidate_defaults import (
    DEFAULT_SELL_CALL_WINDOW,
    DEFAULT_SELL_PUT_WINDOW,
    resolve_candidate_window,
)
from domain.domain.fetch_source import is_futu_fetch_source, resolve_symbol_fetch_source
from src.application.strategy_policy import (
    SELL_CALL_FAMILY,
    SELL_PUT_FAMILY,
    assert_strategy_config_resolved,
    strategy_semantics_for_side_config,
)
from src.application.config_profiles import apply_profiles
from src.application.config_sections import (
    resolve_templates_config,
    resolve_watchlist_config,
    set_watchlist_config,
)
from src.application.pipeline_watchlist import resolve_watchlist_item_runtime_config
from src.application.prefilters import apply_prefilters
from src.application.close_advice_required_data import (
    finalize_close_advice_required_data_plan,
    resolve_position_fetch_binding,
)
from src.application.wheel.config import resolve_wheel_config
from src.application.required_data_plan_identity import required_data_plan_id
from src.application.combo_yield_config import (
    derive_combo_yield_policy,
    resolve_combo_yield_cfg,
)


DEFAULT_STRIKE_EXPAND_PCT = 0.20
DEFAULT_CALL_STRIKE_BUFFER_PCT = 0.02


def build_cross_account_prefetch_config(
    *,
    base_config: dict[str, Any],
    account_configs: dict[str, dict[str, Any]],
    prepared_portfolio_contexts: dict[str, dict[str, Any] | None],
) -> dict[str, Any]:
    """Build an order-independent union of effective per-account market demand."""

    source_items: list[dict[str, Any]] = []
    configs_by_account = {
        str(account or "").strip().lower(): cfg
        for account, cfg in account_configs.items()
        if str(account or "").strip() and isinstance(cfg, dict)
    }
    requested_symbols = {
        symbol
        for cfg in configs_by_account.values()
        for raw in resolve_watchlist_config(cfg)
        if isinstance(raw, dict)
        if (symbol := _symbol_key(str(raw.get("symbol") or "")))
    }
    base_profiles = resolve_templates_config(base_config)
    for raw in resolve_watchlist_config(base_config):
        if not isinstance(raw, dict):
            continue
        resolved = resolve_watchlist_item_runtime_config(
            item=raw,
            profiles=base_profiles,
            apply_profiles_fn=apply_profiles,
        )
        if _symbol_key(str(resolved.get("symbol") or "")) not in requested_symbols:
            continue
        base_item = deepcopy(resolved)
        call_cfg = dict(_as_dict(base_item.get("sell_call")))
        call_cfg["enabled"] = False
        base_item["sell_call"] = call_cfg
        if _has_non_account_market_demand(base_item):
            source_items.append(base_item)

    contexts_by_account = {
        str(account or "").strip().lower(): context
        for account, context in prepared_portfolio_contexts.items()
        if str(account or "").strip()
    }
    for account in sorted(configs_by_account):
        cfg = configs_by_account[account]
        context = contexts_by_account.get(account)
        profiles = resolve_templates_config(cfg)
        for raw in resolve_watchlist_config(cfg):
            if not isinstance(raw, dict):
                continue
            resolved = resolve_watchlist_item_runtime_config(
                item=raw,
                profiles=profiles,
                apply_profiles_fn=apply_profiles,
            )
            symbol = str(resolved.get("symbol") or "").strip()
            sp = dict(_as_dict(resolved.get("sell_put")))
            cc = dict(_as_dict(resolved.get("sell_call")))
            filtered = apply_prefilters(
                symbol=symbol,
                sp=sp,
                cc=cc,
                want_put=bool(sp.get("enabled", False)),
                want_call=bool(cc.get("enabled", False)),
                portfolio_ctx=context,
            )
            effective = deepcopy(resolved)
            sp = dict(filtered.sp)
            sp["enabled"] = bool(filtered.want_put)
            cc = dict(filtered.cc)
            cc["enabled"] = bool(filtered.want_call)
            if filtered.want_call and isinstance(filtered.stock, dict):
                min_strike = resolve_effective_sell_call_min_strike(
                    min_strike=cc.get("min_strike"),
                    avg_cost=filtered.stock.get("avg_cost"),
                    cost_multiplier=cc.get("min_strike_cost_multiplier", 1.02),
                )
                if min_strike is not None:
                    cc["min_strike"] = min_strike
            effective["sell_put"] = sp
            effective["sell_call"] = cc
            if _has_any_market_demand(effective):
                source_items.append(effective)

    source_items.sort(key=_stable_symbol_config_key)
    out = deepcopy(base_config)
    set_watchlist_config(out, source_items)
    return out


def merge_close_advice_requirements_into_prefetch_config(
    *,
    candidate_config: dict[str, Any],
    requirements_plan: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge ready position requirements without weakening candidate demand."""

    source_items = [
        deepcopy(item)
        for item in resolve_watchlist_config(candidate_config)
        if isinstance(item, dict)
    ]
    plan = deepcopy(requirements_plan)
    accounts = (
        plan.get("accounts")
        if isinstance(plan.get("accounts"), dict)
        else {}
    )
    requirements_by_symbol: dict[str, list[dict[str, Any]]] = {}
    owner_by_requirement_id: dict[str, dict[str, Any]] = {}
    for account in sorted(accounts):
        account_payload = accounts.get(account)
        if not isinstance(account_payload, dict):
            continue
        for requirement in list(account_payload.get("requirements") or []):
            if not isinstance(requirement, dict):
                continue
            requirement_id = str(requirement.get("requirement_id") or "").strip()
            symbol = _symbol_key(str(requirement.get("symbol") or ""))
            if not (requirement_id and symbol):
                continue
            owner_by_requirement_id[requirement_id] = account_payload
            requirements_by_symbol.setdefault(symbol, []).append(requirement)

    candidate_indexes_by_symbol: dict[str, list[int]] = {}
    candidate_routes_by_symbol: dict[str, set[tuple[str, str, int]]] = {}
    for index, item in enumerate(source_items):
        symbol = _symbol_key(str(item.get("symbol") or ""))
        if not symbol:
            continue
        candidate_indexes_by_symbol.setdefault(symbol, []).append(index)
        candidate_routes_by_symbol.setdefault(symbol, set()).add(
            _candidate_binding_tuple(item)
        )

    diagnostics: list[dict[str, Any]] = []
    for symbol in sorted(requirements_by_symbol):
        requirements = sorted(
            requirements_by_symbol[symbol],
            key=lambda value: str(value.get("requirement_id") or ""),
        )
        ready_requirements = [
            item
            for item in requirements
            if str(item.get("planning_status") or "") == "ready"
            and isinstance(item.get("fetch_binding"), dict)
        ]
        candidate_routes = candidate_routes_by_symbol.get(symbol, set())
        indexes = candidate_indexes_by_symbol.get(symbol, [])
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        preserved_binding: tuple[str, str, int] | None = None
        position_only_conflict = False
        candidate_route_ambiguous = len(candidate_routes) > 1

        if len(candidate_routes) == 1:
            preserved_binding = next(iter(candidate_routes))
            for requirement in ready_requirements:
                if _requirement_binding_tuple(requirement) == preserved_binding:
                    accepted.append(requirement)
                else:
                    rejected.append(requirement)
            if accepted and indexes:
                _append_position_requirements(
                    source_items[indexes[0]],
                    accepted,
                )
        elif not candidate_routes:
            position_routes = {
                _requirement_binding_tuple(requirement)
                for requirement in ready_requirements
            }
            if len(position_routes) == 1 and ready_requirements:
                binding = dict(ready_requirements[0]["fetch_binding"])
                position_only = {
                    "symbol": symbol,
                    "fetch": {
                        "source": binding["source"],
                        "host": binding["host"],
                        "port": int(binding["port"]),
                    },
                    "sell_put": {"enabled": False},
                    "sell_call": {"enabled": False},
                    "combo_yield": {"enabled": False},
                }
                _append_position_requirements(
                    position_only,
                    ready_requirements,
                )
                source_items.append(position_only)
                accepted.extend(ready_requirements)
            elif len(position_routes) > 1:
                position_only_conflict = True
                rejected.extend(ready_requirements)
        else:
            rejected.extend(ready_requirements)

        for requirement in rejected:
            _reject_position_requirement(
                requirement=requirement,
                owner=owner_by_requirement_id.get(
                    str(requirement.get("requirement_id") or "")
                ),
                reason="required_data_route_conflict",
            )
        diagnostics.append(
            {
                "symbol": symbol,
                "preserved_candidate_binding": (
                    {
                        "source": preserved_binding[0],
                        "host": preserved_binding[1],
                        "port": preserved_binding[2],
                    }
                    if preserved_binding is not None
                    else None
                ),
                "accepted_requirement_ids": sorted(
                    str(item.get("requirement_id") or "")
                    for item in accepted
                ),
                "rejected_requirement_ids": sorted(
                    str(item.get("requirement_id") or "")
                    for item in rejected
                ),
                "position_only_conflict": position_only_conflict,
                "candidate_route_ambiguous": candidate_route_ambiguous,
            }
        )

    plan = finalize_close_advice_required_data_plan(plan)
    plan_hash = str(plan["content_sha256"])
    for item in source_items:
        if list(item.get("_close_advice_position_requirements") or []):
            item["_close_advice_requirement_plan_hash"] = plan_hash
    source_items.sort(key=_stable_symbol_config_key)
    out = deepcopy(candidate_config)
    set_watchlist_config(out, source_items)
    out["_close_advice_required_data_diagnostics"] = diagnostics
    out["_close_advice_requirement_plan_hash"] = plan_hash
    return out, plan


def merge_wheel_requirements_into_prefetch_config(
    *,
    base_config: dict[str, Any],
    candidate_config: dict[str, Any],
    account_configs: dict[str, dict[str, Any]],
    wheel_read_models: dict[str, dict[str, Any]],
    allowed_symbols: set[str] | None = None,
) -> dict[str, Any]:
    """Add active Wheel Call demand to the shared Required Data plan."""

    items = [
        deepcopy(item)
        for item in resolve_watchlist_config(candidate_config)
        if isinstance(item, dict)
    ]
    by_route = {
        (_symbol_key(str(item.get("symbol") or "")), _candidate_binding_tuple(item)): item
        for item in items
    }
    normalized_allow = (
        {_symbol_key(item) for item in allowed_symbols}
        if allowed_symbols is not None
        else None
    )
    demands: dict[tuple[str, tuple[str, str, int]], dict[str, Any]] = {}
    for account in sorted(account_configs):
        config = account_configs[account]
        policy = resolve_wheel_config(config, account)
        if not policy["enabled_for_new_lifecycle"]:
            continue
        model = wheel_read_models.get(account) or {}
        for batch in model.get("batches") or []:
            if not isinstance(batch, dict) or any(
                (
                    batch.get("lifecycle_status") != "active",
                    batch.get("integrity_status") != "trusted",
                    batch.get("phase") != "ready",
                )
            ):
                continue
            symbol = _symbol_key(str(batch.get("symbol") or ""))
            binding, error = resolve_position_fetch_binding(
                symbol=symbol,
                account_config=config,
                base_config=base_config,
            )
            if (
                not symbol
                or (normalized_allow is not None and symbol not in normalized_allow)
                or binding is None
                or error is not None
            ):
                continue
            route = (
                str(binding["source"]),
                _physical_host(binding["host"]),
                int(binding["port"]),
            )
            key = (symbol, route)
            current = demands.setdefault(
                key,
                {
                    "enabled": True,
                    "min_dte": int(policy["min_dte"]),
                    "max_dte": int(policy["max_dte"]),
                    "requires_realized_volatility": True,
                },
            )
            current["min_dte"] = min(int(current["min_dte"]), int(policy["min_dte"]))
            current["max_dte"] = max(int(current["max_dte"]), int(policy["max_dte"]))
    for (symbol, route), demand in sorted(demands.items()):
        item = by_route.get((symbol, route))
        if item is None:
            item = {
                "symbol": symbol,
                "fetch": {"source": route[0], "host": route[1], "port": route[2]},
                "sell_put": {"enabled": False},
                "sell_call": {"enabled": False},
                "combo_yield": {"enabled": False},
            }
            items.append(item)
            by_route[(symbol, route)] = item
        item["_wheel_call"] = demand
    items.sort(key=_stable_symbol_config_key)
    out = deepcopy(candidate_config)
    set_watchlist_config(out, items)
    return out


def _candidate_binding_tuple(
    symbol_cfg: dict[str, Any],
) -> tuple[str, str, int]:
    fetch_cfg = _as_dict(symbol_cfg.get("fetch"))
    source, _decision = resolve_symbol_fetch_source(fetch_cfg)
    return (
        source,
        _physical_host(fetch_cfg.get("host") or "127.0.0.1"),
        _to_int(fetch_cfg.get("port") or 11111, 11111),
    )


def _requirement_binding_tuple(
    requirement: dict[str, Any],
) -> tuple[str, str, int]:
    binding = _as_dict(requirement.get("fetch_binding"))
    return (
        str(binding.get("source") or "").strip().lower(),
        _physical_host(binding.get("host")),
        _to_int(binding.get("port"), 0),
    )


def _append_position_requirements(
    symbol_cfg: dict[str, Any],
    requirements: list[dict[str, Any]],
) -> None:
    current = [
        deepcopy(item)
        for item in list(
            symbol_cfg.get("_close_advice_position_requirements") or []
        )
        if isinstance(item, dict)
    ]
    by_id = {
        str(item.get("requirement_id") or ""): item
        for item in current
        if str(item.get("requirement_id") or "")
    }
    for requirement in requirements:
        requirement_id = str(requirement.get("requirement_id") or "").strip()
        if requirement_id:
            by_id[requirement_id] = deepcopy(requirement)
    symbol_cfg["_close_advice_position_requirements"] = [
        by_id[key] for key in sorted(by_id)
    ]


def _reject_position_requirement(
    *,
    requirement: dict[str, Any],
    owner: dict[str, Any] | None,
    reason: str,
) -> None:
    requirement["planning_status"] = "unavailable"
    requirement["planning_reason"] = reason
    if owner is None:
        return
    errors = owner.setdefault("planning_errors", [])
    if not isinstance(errors, list):
        errors = []
        owner["planning_errors"] = errors
    payload = {
        "reason": reason,
        "position_lot_id": requirement.get("position_lot_id"),
        "quote_key": requirement.get("quote_key"),
        "requirement_id": requirement.get("requirement_id"),
    }
    identity = (
        str(payload["reason"] or ""),
        str(payload["position_lot_id"] or ""),
        str(payload["quote_key"] or ""),
    )
    if not any(
        (
            str(item.get("reason") or ""),
            str(item.get("position_lot_id") or ""),
            str(item.get("quote_key") or ""),
        )
        == identity
        for item in errors
        if isinstance(item, dict)
    ):
        errors.append(payload)


def _has_non_account_market_demand(symbol_cfg: dict[str, Any]) -> bool:
    return bool(
        _as_dict(symbol_cfg.get("sell_put")).get("enabled", False)
        or derive_combo_yield_policy(
            resolve_combo_yield_cfg(symbol_cfg)
        ).enabled
    )


def _has_any_market_demand(symbol_cfg: dict[str, Any]) -> bool:
    return bool(
        _has_non_account_market_demand(symbol_cfg)
        or _as_dict(symbol_cfg.get("sell_call")).get("enabled", False)
        or _as_dict(symbol_cfg.get("_wheel_call")).get("enabled", False)
    )


def _stable_symbol_config_key(symbol_cfg: dict[str, Any]) -> tuple[str, str, str]:
    fetch = _as_dict(symbol_cfg.get("fetch"))
    canonical = json.dumps(
        symbol_cfg,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return (
        str(symbol_cfg.get("symbol") or "").strip().upper(),
        f"{fetch.get('source') or ''}:{fetch.get('host') or ''}:{fetch.get('port') or ''}",
        canonical,
    )


@dataclass(frozen=True)
class PrefetchSymbolPlan:
    symbol_cfgs: list[dict[str, Any]]
    requested_symbols: list[str]
    deduped_groups: list[dict[str, Any]]

    @property
    def requested_count(self) -> int:
        return len(self.requested_symbols)

    @property
    def unique_count(self) -> int:
        return len(self.symbol_cfgs)

    @property
    def deduped_count(self) -> int:
        return max(0, self.requested_count - self.unique_count)

    def summary(self) -> dict[str, Any]:
        return {
            "requested_count": self.requested_count,
            "unique_count": self.unique_count,
            "deduped_count": self.deduped_count,
            "deduped_groups": [dict(group) for group in self.deduped_groups],
        }


@dataclass(frozen=True)
class PrefetchBudgetWave:
    index: int
    symbol_cfgs: list[dict[str, Any]]
    estimated_option_chain_calls: int

    @property
    def symbols(self) -> list[str]:
        return [
            str(item.get("symbol") or "").strip()
            for item in self.symbol_cfgs
            if isinstance(item, dict) and str(item.get("symbol") or "").strip()
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "symbols": list(self.symbols),
            "symbols_count": len(self.symbol_cfgs),
            "estimated_option_chain_calls": int(self.estimated_option_chain_calls),
        }


@dataclass(frozen=True)
class PrefetchBudgetPlan:
    waves: list[PrefetchBudgetWave]
    estimated_option_chain_calls: int
    safe_option_chain_calls_per_window: int
    configured_option_chain_max_calls: int
    option_chain_window_sec: float
    oversized_symbols: list[dict[str, Any]]

    @property
    def waves_count(self) -> int:
        return len(self.waves)

    def summary(self) -> dict[str, Any]:
        return {
            "estimated_option_chain_calls": int(self.estimated_option_chain_calls),
            "safe_option_chain_calls_per_window": int(self.safe_option_chain_calls_per_window),
            "configured_option_chain_max_calls": int(self.configured_option_chain_max_calls),
            "option_chain_window_sec": float(self.option_chain_window_sec),
            "waves_count": int(self.waves_count),
            "waves": [wave.summary() for wave in self.waves],
            "oversized_symbols": [dict(item) for item in self.oversized_symbols],
        }


def build_prefetch_symbol_plan(symbol_cfgs: list[dict[str, Any]]) -> PrefetchSymbolPlan:
    requested_symbols = [
        str(item.get("symbol") or "").strip()
        for item in symbol_cfgs
        if isinstance(item, dict) and str(item.get("symbol") or "").strip()
    ]
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for idx, cfg in enumerate(symbol_cfgs):
        key = _dedupe_key(cfg, idx=idx)
        groups.setdefault(key, []).append(cfg)

    merged_cfgs: list[dict[str, Any]] = []
    deduped_groups: list[dict[str, Any]] = []
    for items in groups.values():
        merged = merge_prefetch_symbol_configs(items)
        merged_cfgs.append(merged)
        if len(items) > 1:
            deduped_groups.append(
                {
                    "symbol": str(merged.get("symbol") or "").strip(),
                    "requested_count": len(items),
                    "symbols": [
                        str(item.get("symbol") or "").strip()
                        for item in items
                        if isinstance(item, dict) and str(item.get("symbol") or "").strip()
                    ],
                }
            )

    return PrefetchSymbolPlan(
        symbol_cfgs=merged_cfgs,
        requested_symbols=requested_symbols,
        deduped_groups=deduped_groups,
    )


def build_prefetch_budget_plan(
    symbol_cfgs: list[dict[str, Any]],
    *,
    option_chain_cfg: dict[str, Any],
    fetch_plans_by_config_id: dict[int, Any] | None = None,
) -> PrefetchBudgetPlan:
    configured_max_calls = max(1, _to_int(option_chain_cfg.get("max_calls") or 10, 10))
    window_sec = max(0.001, _to_float(option_chain_cfg.get("window_sec")) or 30.0)
    safe_calls = _safe_option_chain_calls(configured_max_calls)
    waves: list[PrefetchBudgetWave] = []
    oversized_symbols: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_calls = 0
    estimated_total = 0

    def flush_current() -> None:
        nonlocal current, current_calls
        if not current:
            return
        waves.append(
            PrefetchBudgetWave(
                index=len(waves) + 1,
                symbol_cfgs=list(current),
                estimated_option_chain_calls=int(current_calls),
            )
        )
        current = []
        current_calls = 0

    for cfg in symbol_cfgs:
        est = estimate_prefetch_option_chain_calls(
            cfg,
            fetch_plan=(fetch_plans_by_config_id or {}).get(id(cfg)),
        )
        estimated_total += est
        symbol = str((cfg or {}).get("symbol") or "").strip()
        if est > safe_calls:
            flush_current()
            waves.append(
                PrefetchBudgetWave(
                    index=len(waves) + 1,
                    symbol_cfgs=[cfg],
                    estimated_option_chain_calls=est,
                )
            )
            oversized_symbols.append(
                {
                    "symbol": symbol,
                    "estimated_option_chain_calls": est,
                    "safe_option_chain_calls_per_window": safe_calls,
                }
            )
            continue
        if current and est > 0 and current_calls + est > safe_calls:
            flush_current()
        current.append(cfg)
        current_calls += est

    flush_current()
    return PrefetchBudgetPlan(
        waves=waves,
        estimated_option_chain_calls=estimated_total,
        safe_option_chain_calls_per_window=safe_calls,
        configured_option_chain_max_calls=configured_max_calls,
        option_chain_window_sec=window_sec,
        oversized_symbols=oversized_symbols,
    )


def estimate_prefetch_option_chain_calls(
    symbol_cfg: dict[str, Any],
    *,
    fetch_plan: Any | None = None,
) -> int:
    fetch_cfg = _as_dict((symbol_cfg or {}).get("fetch"))
    source, _decision = resolve_symbol_fetch_source(fetch_cfg)
    if not is_futu_fetch_source(source):
        return 0
    if fetch_plan is not None:
        side_plans = list(getattr(fetch_plan, "side_plans", []) or [])
        projection_outcome = str(
            getattr(fetch_plan, "projection_outcome", "") or ""
        ).strip()
        if projection_outcome in {
            "success_empty",
            "projection_empty",
            "provider_error",
            "parse_error",
        }:
            return 0
        if projection_outcome != "success_rows":
            raise ValueError(
                "scheduled fetch plan lacks typed discovery evidence"
            )
        expirations = {
            str(expiration)
            for side_plan in side_plans
            for expiration in getattr(side_plan, "explicit_expirations", []) or []
            if str(expiration).strip()
        } or {
            str(expiration)
            for expiration in getattr(
                fetch_plan,
                "projected_expirations",
                [],
            )
            or []
            if str(expiration).strip()
        }
        if not expirations:
            raise ValueError(
                "success-rows fetch plan lacks exact projected targets"
            )
        return len(expirations)
    return _limit_expirations(symbol_cfg)


def merge_prefetch_symbol_configs(symbol_cfgs: list[dict[str, Any]]) -> dict[str, Any]:
    items = [cfg for cfg in symbol_cfgs if isinstance(cfg, dict)]
    if not items:
        return {}
    merged = deepcopy(items[0])
    fetch_cfg = dict(_as_dict(merged.get("fetch")))
    fetch_cfg.pop("limit_expirations", None)
    if str(fetch_cfg.get("host") or "").strip():
        fetch_cfg["host"] = _physical_host(fetch_cfg.get("host"))
    merged["fetch"] = fetch_cfg
    merged["_prefetch_strategy_kwargs"] = _merge_strategy_prefetch_kwargs(
        [strategy_prefetch_kwargs(item, enabled=True) for item in items]
    )
    merged["_prefetch_source_symbol_cfgs"] = [deepcopy(item) for item in items]
    merged["_prefetch_requested_count"] = len(items)
    return merged


def strategy_prefetch_kwargs(symbol_cfg: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    precomputed = symbol_cfg.get("_prefetch_strategy_kwargs") if isinstance(symbol_cfg, dict) else None
    if enabled and isinstance(precomputed, dict):
        return _clone_strategy_kwargs(precomputed)
    if not enabled:
        return {"option_types": "put,call"}
    assert_strategy_config_resolved(symbol_cfg)

    sp = _as_dict(symbol_cfg.get("sell_put"))
    cc = _as_dict(symbol_cfg.get("sell_call"))
    ye = resolve_combo_yield_cfg(symbol_cfg)
    want_put = bool(sp.get("enabled", False))
    want_direct_call = bool(cc.get("enabled", False))
    yield_policy = derive_combo_yield_policy(ye)
    want_yield_call = bool(yield_policy.enabled)
    sell_put_semantics = strategy_semantics_for_side_config(family=SELL_PUT_FAMILY, side_cfg=sp)
    sell_call_semantics = strategy_semantics_for_side_config(family=SELL_CALL_FAMILY, side_cfg=cc)
    include_realized_volatility = bool(
        (want_put and sell_put_semantics.scan_requires_rv)
        or (want_direct_call and sell_call_semantics.scan_requires_rv)
        or (want_yield_call and yield_policy.requires_realized_volatility)
    )

    option_types: list[str] = []
    min_dtes: list[int] = []
    max_dtes: list[int] = []
    side_strike_windows: dict[str, dict[str, float | None]] = {}

    if want_put or want_yield_call:
        min_dte, max_dte = _window_values(sp, defaults=DEFAULT_SELL_PUT_WINDOW)
        min_dtes.append(min_dte)
        max_dtes.append(max_dte)
        option_types.append("put")
        side_strike_windows["put"] = _put_strike_window(sp)

    if want_direct_call:
        min_dte, max_dte = _window_values(cc, defaults=DEFAULT_SELL_CALL_WINDOW)
        min_dtes.append(min_dte)
        max_dtes.append(max_dte)
        option_types.append("call")
        side_strike_windows["call"] = _call_strike_window(cc)

    if want_yield_call:
        call_cfg = dict(_as_dict(ye.get("call")))
        call_cfg.pop("min_dte", None)
        call_cfg.pop("max_dte", None)
        for key in ("min_dte", "max_dte"):
            if key in sp:
                call_cfg[key] = sp.get(key)
        min_dte, max_dte = _window_values(call_cfg, defaults=DEFAULT_SELL_PUT_WINDOW)
        min_dtes.append(min_dte)
        max_dtes.append(max_dte)
        if "call" not in option_types:
            option_types.append("call")
        yield_window = _call_strike_window(call_cfg)
        existing_call = side_strike_windows.get("call")
        if existing_call is None:
            side_strike_windows["call"] = yield_window
        else:
            side_strike_windows["call"] = _merge_strike_windows(existing_call, yield_window)

    if not option_types:
        option_types = ["put", "call"]

    return _strategy_payload(
        option_types=option_types,
        min_dtes=min_dtes,
        max_dtes=max_dtes,
        side_strike_windows=side_strike_windows,
        include_realized_volatility=include_realized_volatility,
    )


def _merge_strategy_prefetch_kwargs(items: list[dict[str, Any]]) -> dict[str, Any]:
    option_types: list[str] = []
    min_dtes: list[int] = []
    max_dtes: list[int] = []
    side_strike_windows: dict[str, dict[str, float | None]] = {}
    include_realized_volatility = False

    for item in items:
        include_realized_volatility = include_realized_volatility or bool(item.get("include_realized_volatility"))
        for option_type in _parse_option_types(item.get("option_types")):
            if option_type not in option_types:
                option_types.append(option_type)
        if item.get("min_dte") is not None:
            min_dtes.append(int(item["min_dte"]))
        if item.get("max_dte") is not None:
            max_dtes.append(int(item["max_dte"]))
        raw_windows = item.get("side_strike_windows")
        if not isinstance(raw_windows, dict):
            continue
        for side in ("put", "call"):
            raw_window = raw_windows.get(side)
            if not isinstance(raw_window, dict):
                continue
            incoming = {
                "min_strike": _to_float(raw_window.get("min_strike")),
                "max_strike": _to_float(raw_window.get("max_strike")),
            }
            existing = side_strike_windows.get(side)
            side_strike_windows[side] = incoming if existing is None else _merge_strike_windows(existing, incoming)

    if not option_types:
        option_types = ["put", "call"]
    ordered_option_types = [side for side in ("put", "call") if side in set(option_types)]
    return _strategy_payload(
        option_types=ordered_option_types,
        min_dtes=min_dtes,
        max_dtes=max_dtes,
        side_strike_windows=side_strike_windows,
        include_realized_volatility=include_realized_volatility,
    )


def _strategy_payload(
    *,
    option_types: list[str],
    min_dtes: list[int],
    max_dtes: list[int],
    side_strike_windows: dict[str, dict[str, float | None]],
    include_realized_volatility: bool = False,
) -> dict[str, Any]:
    all_mins = [
        value
        for value in (_to_float(window.get("min_strike")) for window in side_strike_windows.values())
        if value is not None
    ]
    all_maxs = [
        value
        for value in (_to_float(window.get("max_strike")) for window in side_strike_windows.values())
        if value is not None
    ]
    return {
        "option_types": ",".join(dict.fromkeys(option_types)),
        "min_dte": min(min_dtes) if min_dtes else None,
        "max_dte": max(max_dtes) if max_dtes else None,
        "min_strike": min(all_mins) if all_mins else None,
        "max_strike": max(all_maxs) if all_maxs else None,
        "side_strike_windows": side_strike_windows,
        "include_realized_volatility": bool(include_realized_volatility),
    }


def _dedupe_key(symbol_cfg: dict[str, Any], *, idx: int) -> tuple[Any, ...]:
    symbol = str((symbol_cfg or {}).get("symbol") or "").strip()
    if not symbol:
        return ("empty", idx)
    fetch_cfg = _as_dict((symbol_cfg or {}).get("fetch"))
    source, _decision = resolve_symbol_fetch_source(fetch_cfg)
    host = _physical_host(fetch_cfg.get("host") or "127.0.0.1")
    port = _to_int(fetch_cfg.get("port") or 11111, 11111)
    return ("symbol", _symbol_key(symbol), source, host, int(port))


def _symbol_key(symbol: str) -> str:
    raw = str(symbol or "").strip()
    if not raw:
        return ""
    return raw.upper()


def _physical_host(value: Any) -> str:
    return str(value or "").strip().lower()


def _limit_expirations(symbol_cfg: dict[str, Any]) -> int:
    source_cfgs = (symbol_cfg or {}).get("_prefetch_source_symbol_cfgs")
    if isinstance(source_cfgs, list):
        configured = [
            _to_int(_as_dict(item.get("fetch")).get("limit_expirations"), 0)
            for item in source_cfgs
            if isinstance(item, dict)
        ]
        positive = [value for value in configured if value > 0]
        if positive:
            return max(positive)
    fetch_cfg = _as_dict((symbol_cfg or {}).get("fetch"))
    return max(1, _to_int(fetch_cfg.get("limit_expirations") or 8, 8))


def _safe_option_chain_calls(configured_max_calls: int) -> int:
    max_calls = max(1, int(configured_max_calls))
    if max_calls <= 1:
        return 1
    return max(1, min(max_calls, int(max_calls * 0.8)))


def _window_values(raw: dict[str, Any], *, defaults: Any) -> tuple[int, int]:
    window = resolve_candidate_window(raw, defaults=defaults)
    return int(window.min_dte), int(window.max_dte)


def _put_strike_window(sp: dict[str, Any]) -> dict[str, float | None]:
    min_strike = _to_float(sp.get("min_strike"))
    max_strike = _to_float(sp.get("max_strike"))
    if min_strike is None and max_strike is not None:
        min_strike = max_strike * (1.0 - DEFAULT_STRIKE_EXPAND_PCT)
    return {"min_strike": min_strike, "max_strike": max_strike}


def _call_strike_window(cc: dict[str, Any]) -> dict[str, float | None]:
    min_strike = _to_float(cc.get("min_strike"))
    max_strike = _to_float(cc.get("max_strike"))
    if min_strike is not None and max_strike is None:
        max_strike = min_strike * (1.0 + DEFAULT_STRIKE_EXPAND_PCT)
    if min_strike is not None and max_strike is not None and max_strike < min_strike:
        max_strike = min_strike
    if max_strike is not None:
        max_strike = max_strike * (1.0 + DEFAULT_CALL_STRIKE_BUFFER_PCT)
    return {"min_strike": min_strike, "max_strike": max_strike}


def _merge_strike_windows(
    left: dict[str, float | None],
    right: dict[str, float | None],
) -> dict[str, float | None]:
    mins = [v for v in (_to_float(left.get("min_strike")), _to_float(right.get("min_strike"))) if v is not None]
    maxs = [v for v in (_to_float(left.get("max_strike")), _to_float(right.get("max_strike"))) if v is not None]
    return {
        "min_strike": min(mins) if mins else None,
        "max_strike": max(maxs) if maxs else None,
    }


def _parse_option_types(value: Any) -> list[str]:
    out: list[str] = []
    for item in str(value or "").split(","):
        raw = str(item or "").strip().lower()
        if raw in {"put", "call"} and raw not in out:
            out.append(raw)
    return out


def _clone_strategy_kwargs(value: dict[str, Any]) -> dict[str, Any]:
    cloned = dict(value)
    raw_windows = cloned.get("side_strike_windows")
    if isinstance(raw_windows, dict):
        cloned["side_strike_windows"] = {
            str(side): dict(window)
            for side, window in raw_windows.items()
            if isinstance(window, dict)
        }
    return cloned


def _to_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _to_float(v: Any) -> float | None:
    try:
        if v in (None, ""):
            return None
        return float(v)
    except Exception:
        return None


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}
