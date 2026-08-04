from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import json
from pathlib import Path
import time
from typing import Any, Iterator

try:
    import fcntl
except Exception:  # pragma: no cover - non-POSIX fallback
    fcntl = None

from domain.domain.tool_boundary import SCHEMA_VERSION_V1, normalize_tool_execution_payload
from domain.services import (
    ToolExecutionIntent,
    ToolExecutionService,
    adapt_opend_tool_payload,
)
from domain.domain.fetch_source import resolve_symbol_fetch_source
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.storage.repositories import state_repo
from src.application.config_sections import (
    resolve_templates_config,
    resolve_watchlist_config,
)
from src.application.config_profiles import apply_profiles
from src.application.multi_tick.prefetch_coordinator import PrefetchCoordinator
from src.application.multi_tick.prefetch_coordinator import PrefetchCoordinatorResult
from src.application.opend_fetch_config import resolve_opend_batch_config, resolve_opend_fetch_config
from src.application.opend_symbol_fetching import fetch_symbol
from src.application.opend_symbol_outputs import (
    finalize_required_data_quote_candidate,
    find_fresh_required_data_quote_receipts,
    validate_required_data_payload_candidate,
    validate_required_data_quote_candidate,
)
from src.application.required_data_observability import (
    summarize_prefetch_fetch_metrics,
    summarize_required_data_prefetch_run,
)
from src.application.required_data_fetching import (
    RequiredDataFetchRequest,
    bind_merged_payload_evidence,
    bind_required_data_child_request_evidence,
    build_fetch_request_from_spec,
    merge_required_data_payloads,
)
from src.application.required_data_planning import (
    RequiredDataFetchPlanBundle,
    _merge_same_side_plans as _merge_required_data_side_plans,
    _merge_side_plans as _merge_required_data_fetch_specs,
    build_required_data_fetch_plan,
)
from src.application.required_data_plan_identity import (
    build_required_data_expected_fetch_contract,
)
from src.application.required_data_prefetch_planning import (
    build_prefetch_budget_plan,
    build_prefetch_symbol_plan,
    estimate_prefetch_option_chain_calls,
    required_data_plan_id,
)
from src.application.yield_enhancement_config import (
    derive_yield_enhancement_policy,
    resolve_yield_enhancement_cfg,
)
from src.infrastructure.futu_gateway_pool import ThreadLocalFutuGatewayPool
from src.infrastructure.io_utils import has_shared_required_data as _has_shared_required_data
from src.infrastructure.opend_retcodes import classify_opend_error


_gateway_pool = ThreadLocalFutuGatewayPool()
_DEFAULT_PREFETCH_MAX_WORKERS = 2

# Compatibility surface for older tests and operational monkeypatches.
has_shared_required_data = _has_shared_required_data


def _to_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _resolve_prefetch_max_workers(cfg: dict[str, Any]) -> int:
    runtime = _as_dict(cfg.get("runtime"))
    runtime_prefetch_cfg = _as_dict(runtime.get("prefetch"))
    prefetch_cfg = _as_dict(cfg.get("prefetch"))
    v = runtime.get("prefetch_max_workers")
    if v is None:
        v = runtime_prefetch_cfg.get("max_workers")
    if v is None:
        v = prefetch_cfg.get("max_workers")
    n = _to_int(v, _DEFAULT_PREFETCH_MAX_WORKERS)
    return n if n > 0 else _DEFAULT_PREFETCH_MAX_WORKERS


def _resolve_execution_mode(cfg: dict[str, Any]) -> str:
    runtime = _as_dict(cfg.get("runtime"))
    prefetch_cfg = _as_dict(runtime.get("prefetch"))
    mode = str(prefetch_cfg.get("execution_mode") or "inprocess").strip().lower()
    return mode if mode in {"inprocess", "subprocess"} else "inprocess"


def _resolve_failure_budget(cfg: dict[str, Any]) -> tuple[int, int]:
    runtime = _as_dict(cfg.get("runtime"))
    prefetch_cfg = _as_dict(cfg.get("prefetch"))
    max_consecutive = runtime.get("prefetch_fail_budget_consecutive")
    if max_consecutive is None:
        max_consecutive = prefetch_cfg.get("fail_budget_consecutive")
    max_total = runtime.get("prefetch_fail_budget_total")
    if max_total is None:
        max_total = prefetch_cfg.get("fail_budget_total")
    return (max(1, _to_int(max_consecutive, 3)), max(1, _to_int(max_total, 5)))


def _resolve_opend_fetch_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    resolved = resolve_opend_fetch_config(cfg)
    return {
        "option_chain": dict(resolved["option_chain"]),
        "market_snapshot": dict(resolved["market_snapshot"]),
        "option_expiration": dict(resolved["option_expiration"]),
    }


def _build_prefetch_fetch_plan(
    symbol_cfg: dict[str, Any],
    *,
    base: Path,
    shared_required: Path,
    opend_fetch_cfg: dict[str, Any],
    expiration_discovery_cache: dict[tuple[Any, ...], Any] | None = None,
) -> RequiredDataFetchPlanBundle:
    source_cfgs = symbol_cfg.get("_prefetch_source_symbol_cfgs")
    if isinstance(source_cfgs, list) and len(source_cfgs) > 1:
        bundles = [
            _build_single_prefetch_fetch_plan(
                item,
                base=base,
                shared_required=shared_required,
                opend_fetch_cfg=opend_fetch_cfg,
                expiration_discovery_cache=expiration_discovery_cache,
            )
            for item in source_cfgs
            if isinstance(item, dict)
        ]
        if bundles:
            if any(
                not isinstance(
                    bundle.require_realized_volatility,
                    bool,
                )
                for bundle in bundles
            ):
                raise RuntimeError(
                    "required-data source plan RV authority is invalid"
                )
            trading_dates = {
                raw_trading_date
                for bundle in bundles
                if (
                    raw_trading_date := _required_data_plan_trading_date(
                        bundle
                    )
                )
                is not None
            }
            if len(trading_dates) != 1 or len(trading_dates) != len(
                {
                    _required_data_plan_trading_date(bundle)
                    for bundle in bundles
                }
            ):
                raise RuntimeError(
                    "required-data source plans have inconsistent trading dates"
                )
            trading_date = next(iter(trading_dates))
            require_realized_volatility = any(
                bundle.require_realized_volatility for bundle in bundles
            )
            spot_reference = next(
                (bundle.spot_reference for bundle in bundles if bundle.spot_reference is not None),
                None,
            )
            merged_side_plans = _merge_required_data_side_plans([
                side_plan
                for bundle in bundles
                for side_plan in bundle.side_plans
            ])
            fetch_cfg = _as_dict(symbol_cfg.get("fetch"))
            expiration_discovery = bundles[0].expiration_discovery
            projected_expirations = sorted(
                {
                    str(expiration)
                    for bundle in bundles
                    for expiration in (
                        [
                            expiration
                            for side_plan in bundle.side_plans
                            for expiration in side_plan.explicit_expirations
                        ]
                        if bundle.side_plans
                        else bundle.projected_expirations
                    )
                    if str(expiration).strip()
                }
            )
            discovery_outcome = str(
                getattr(expiration_discovery, "outcome", "") or ""
            )
            if discovery_outcome == "success_empty":
                projection_outcome = "success_empty"
            elif discovery_outcome in {"provider_error", "parse_error"}:
                projection_outcome = discovery_outcome
            elif merged_side_plans and not projected_expirations:
                projection_outcome = "projection_empty"
            else:
                projection_outcome = "success_rows"
            return RequiredDataFetchPlanBundle(
                symbol=str(symbol_cfg.get("symbol") or bundles[0].symbol),
                spot_reference=spot_reference,
                side_plans=merged_side_plans,
                merged_specs=_merge_required_data_fetch_specs(
                    symbol=str(symbol_cfg.get("symbol") or bundles[0].symbol),
                    limit_expirations=0,
                    host=str(fetch_cfg.get("host") or "127.0.0.1"),
                    port=_to_int(fetch_cfg.get("port") or 11111, 11111),
                    side_plans=(
                        merged_side_plans
                        if projection_outcome == "success_rows"
                        else []
                    ),
                    trading_date=trading_date,
                    include_realized_volatility=(
                        require_realized_volatility
                    ),
                ),
                expiration_discovery_complete=all(
                    bundle.expiration_discovery_complete for bundle in bundles
                ),
                expiration_discovery_error="; ".join(
                    str(bundle.expiration_discovery_error)
                    for bundle in bundles
                    if bundle.expiration_discovery_error
                )
                or None,
                expiration_discovery=expiration_discovery,
                projection_outcome=projection_outcome,
                projected_expirations=projected_expirations,
                require_realized_volatility=(
                    require_realized_volatility
                ),
            )
    return _build_single_prefetch_fetch_plan(
        symbol_cfg,
        base=base,
        shared_required=shared_required,
        opend_fetch_cfg=opend_fetch_cfg,
        expiration_discovery_cache=expiration_discovery_cache,
    )


def _build_single_prefetch_fetch_plan(
    symbol_cfg: dict[str, Any],
    *,
    base: Path,
    shared_required: Path,
    opend_fetch_cfg: dict[str, Any],
    expiration_discovery_cache: dict[tuple[Any, ...], Any] | None = None,
) -> RequiredDataFetchPlanBundle:
    symbol = str(symbol_cfg.get("symbol") or "").strip()
    fetch_cfg = _as_dict(symbol_cfg.get("fetch"))
    # Strategy required-data planning is bounded by resolved DTE/expiry
    # requirements, not by a per-symbol "first N expirations" cap.
    limit_exp = 0
    sell_put_cfg = _as_dict(symbol_cfg.get("sell_put"))
    sell_call_cfg = _as_dict(symbol_cfg.get("sell_call"))
    yield_enhancement_cfg = resolve_yield_enhancement_cfg(symbol_cfg)
    yield_policy = derive_yield_enhancement_policy(yield_enhancement_cfg)
    want_put = bool(sell_put_cfg.get("enabled", False))
    want_call = bool(sell_call_cfg.get("enabled", False))
    want_yield_enhancement = bool(yield_policy.enabled)
    position_requirements = [
        dict(item)
        for item in list(
            symbol_cfg.get("_close_advice_position_requirements") or []
        )
        if isinstance(item, dict)
    ]
    snapshot_cfg = _as_dict(opend_fetch_cfg.get("market_snapshot"))
    expiration_cfg = _as_dict(opend_fetch_cfg.get("option_expiration"))
    return build_required_data_fetch_plan(
        base=base,
        required_data_dir=shared_required,
        symbol=symbol,
        limit_expirations=limit_exp,
        want_put=bool(want_put or want_yield_enhancement),
        want_call=want_call,
        sell_put_cfg=sell_put_cfg,
        sell_call_cfg=sell_call_cfg,
        yield_enhancement_cfg=yield_enhancement_cfg,
        position_requirements=position_requirements,
        symbol_cfg=symbol_cfg,
        fetch_host=str(fetch_cfg.get("host") or "127.0.0.1"),
        fetch_port=_to_int(fetch_cfg.get("port") or 11111, 11111),
        fetch_source=resolve_symbol_fetch_source(fetch_cfg)[0],
        expiration_discovery_cache=expiration_discovery_cache,
        snapshot_max_wait_sec=float(snapshot_cfg.get("max_wait_sec") or 30.0),
        snapshot_window_sec=float(snapshot_cfg.get("window_sec") or 30.0),
        snapshot_max_calls=int(snapshot_cfg.get("max_calls") or 60),
        expiration_max_wait_sec=float(expiration_cfg.get("max_wait_sec") or 30.0),
        expiration_window_sec=float(expiration_cfg.get("window_sec") or 30.0),
        expiration_max_calls=int(expiration_cfg.get("max_calls") or 60),
    )


def _prefetch_fetch_kwargs_from_plan(fetch_plan: RequiredDataFetchPlanBundle | None) -> dict[str, Any]:
    if fetch_plan is None:
        return {
            "option_types": "put,call",
            "min_dte": None,
            "max_dte": None,
            "side_strike_windows": None,
            "explicit_expirations": None,
            "spot_override": None,
            "include_realized_volatility": False,
            "trading_date": None,
        }
    if not isinstance(fetch_plan.require_realized_volatility, bool):
        raise RuntimeError("required-data plan RV authority is invalid")

    option_types: list[str] = []
    min_dtes: list[int] = []
    max_dtes: list[int] = []
    expirations: list[str] = []
    side_strike_windows: dict[str, dict[str, float | None]] = {}
    executable_side_plans = [
        side_plan
        for spec in fetch_plan.merged_specs
        for side_plan in spec.side_plans
    ]
    for side_plan in executable_side_plans:
        option_type = str(side_plan.option_type)
        if option_type not in option_types:
            option_types.append(option_type)
        if side_plan.min_dte is not None:
            min_dtes.append(int(side_plan.min_dte))
        if side_plan.max_dte is not None:
            max_dtes.append(int(side_plan.max_dte))
        for expiration in side_plan.explicit_expirations:
            exp = str(expiration)
            if exp and exp not in expirations:
                expirations.append(exp)
        side_strike_windows[option_type] = {
            "min_strike": side_plan.strike_window.min_strike,
            "max_strike": side_plan.strike_window.max_strike,
        }
    if not executable_side_plans:
        expirations = list(fetch_plan.projected_expirations)

    projection_outcome = str(
        fetch_plan.projection_outcome or ""
    ).strip()
    if projection_outcome == "success_rows" and not expirations:
        raise RuntimeError(
            "success-rows scheduled plan lacks exact expiration targets"
        )
    if projection_outcome not in {
        "success_rows",
        "success_empty",
        "projection_empty",
        "provider_error",
        "parse_error",
    }:
        raise RuntimeError(
            "scheduled plan lacks typed discovery evidence"
        )

    return {
        "option_types": ",".join([side for side in ("put", "call") if side in set(option_types)]) or "put,call",
        "min_dte": min(min_dtes) if min_dtes else None,
        "max_dte": max(max_dtes) if max_dtes else None,
        "side_strike_windows": side_strike_windows or None,
        "explicit_expirations": (
            expirations if projection_outcome == "success_rows" else []
        ),
        "scheduled_outcome": projection_outcome,
        "spot_override": fetch_plan.spot_reference,
        "include_realized_volatility": (
            fetch_plan.require_realized_volatility
        ),
        "trading_date": (
            trading_date.isoformat()
            if (
                trading_date := _required_data_plan_trading_date(fetch_plan)
            )
            is not None
            else None
        ),
    }


def _required_data_plan_trading_date(
    fetch_plan: RequiredDataFetchPlanBundle,
) -> date | None:
    discovery = fetch_plan.expiration_discovery
    identity = (
        discovery.request_identity
        if discovery is not None
        else None
    )
    raw_value = (
        identity.get("trading_date")
        if isinstance(identity, dict)
        else None
    )
    if not isinstance(raw_value, str) or raw_value != raw_value.strip():
        return None
    try:
        parsed = date.fromisoformat(raw_value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == raw_value else None


def _flat_opend_fetch_config(
    opend_fetch_cfg: dict[str, Any],
) -> dict[str, float | int]:
    option_chain = _as_dict(opend_fetch_cfg.get("option_chain"))
    snapshot = _as_dict(opend_fetch_cfg.get("market_snapshot"))
    expiration = _as_dict(opend_fetch_cfg.get("option_expiration"))
    return {
        "max_wait_sec": float(option_chain["max_wait_sec"]),
        "option_chain_window_sec": float(option_chain["window_sec"]),
        "option_chain_max_calls": int(option_chain["max_calls"]),
        "snapshot_max_wait_sec": float(snapshot["max_wait_sec"]),
        "snapshot_window_sec": float(snapshot["window_sec"]),
        "snapshot_max_calls": int(snapshot["max_calls"]),
        "expiration_max_wait_sec": float(expiration["max_wait_sec"]),
        "expiration_window_sec": float(expiration["window_sec"]),
        "expiration_max_calls": int(expiration["max_calls"]),
    }


def _fetch_symbol_for_required_data_request(
    *,
    request: RequiredDataFetchRequest,
    base: Path,
    gateway: Any,
    batch_cfg: Any,
) -> dict[str, Any]:
    return fetch_symbol(
        request.symbol,
        limit_expirations=request.limit_expirations,
        host=request.host,
        port=request.port,
        spot_override=request.spot_override,
        base_dir=base,
        option_types=request.option_types,
        min_strike=request.min_strike,
        max_strike=request.max_strike,
        side_strike_windows=request.side_strike_windows,
        min_dte=request.min_dte,
        max_dte=request.max_dte,
        explicit_expirations=request.explicit_expirations,
        trading_date=request.trading_date,
        no_retry=request.no_retry,
        chain_cache=request.chain_cache,
        chain_cache_force_refresh=request.chain_cache_force_refresh,
        freshness_policy=request.freshness_policy,
        gateway=gateway,
        snapshot_batch_size=int(getattr(batch_cfg, "market_snapshot", 0) or 0),
        snapshot_fallback_max_codes=int(
            getattr(batch_cfg, "market_snapshot_fallback_max_codes", 100)
            or 0
        ),
        snapshot_fallback_batch_size=int(
            getattr(batch_cfg, "market_snapshot_fallback_batch_size", 20)
            or 20
        ),
        max_wait_sec=request.max_wait_sec,
        option_chain_window_sec=request.option_chain_window_sec,
        option_chain_max_calls=request.option_chain_max_calls,
        snapshot_max_wait_sec=request.snapshot_max_wait_sec,
        snapshot_window_sec=request.snapshot_window_sec,
        snapshot_max_calls=request.snapshot_max_calls,
        expiration_max_wait_sec=request.expiration_max_wait_sec,
        expiration_window_sec=request.expiration_window_sec,
        expiration_max_calls=request.expiration_max_calls,
        include_realized_volatility=request.include_realized_volatility,
    )


def _prefetch_limit_expirations(
    symbol_cfg: dict[str, Any],
    fetch_plan: RequiredDataFetchPlanBundle | None,
) -> int:
    if fetch_plan is not None:
        return 0
    source_cfgs = symbol_cfg.get("_prefetch_source_symbol_cfgs")
    if isinstance(source_cfgs, list):
        configured = [
            _to_int(_as_dict(item.get("fetch")).get("limit_expirations"), 0)
            for item in source_cfgs
            if isinstance(item, dict)
        ]
        positive = [value for value in configured if value > 0]
        if positive:
            return max(positive)
    fetch_cfg = _as_dict(symbol_cfg.get("fetch"))
    return max(1, _to_int(fetch_cfg.get("limit_expirations") or 8, 8))


def _global_required_data_plan_summary(
    *,
    symbol_cfgs: list[dict[str, Any]],
    fetch_plans_by_config_id: dict[int, RequiredDataFetchPlanBundle],
    expected_contracts_by_config_id: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for symbol_cfg in symbol_cfgs:
        fetch_plan = fetch_plans_by_config_id[id(symbol_cfg)]
        fetch_cfg = _as_dict(symbol_cfg.get("fetch"))
        binding_source, _binding_decision = resolve_symbol_fetch_source(
            fetch_cfg
        )
        binding_payload = {
            "source": binding_source,
            "host": str(fetch_cfg.get("host") or "127.0.0.1").strip(),
            "port": _to_int(fetch_cfg.get("port") or 11111, 11111),
        }
        expected_fetch_contract = (
            (expected_contracts_by_config_id or {}).get(id(symbol_cfg))
            or _expected_fetch_contract(symbol_cfg, fetch_plan)
        )
        expirations = sorted({
            str(expiration)
            for side_plan in fetch_plan.side_plans
            for expiration in side_plan.explicit_expirations
            if str(expiration).strip()
        })
        has_strategy_requirements = bool(fetch_plan.side_plans)
        option_chain_calls = estimate_prefetch_option_chain_calls(
            symbol_cfg,
            fetch_plan=fetch_plan,
        )
        projection_outcome = str(
            fetch_plan.projection_outcome or ""
        ).strip()
        items.append(
            {
                "symbol": str(symbol_cfg.get("symbol") or "").strip(),
                "source": binding_source,
                "fetch_binding": {
                    **binding_payload,
                    "binding_id": canonical_sha256(binding_payload),
                },
                "close_advice_requirement_plan_hash": (
                    str(
                        symbol_cfg.get(
                            "_close_advice_requirement_plan_hash"
                        )
                        or ""
                    ).strip()
                    or None
                ),
                "planning_mode": (
                    "strategy_exact_expirations"
                    if has_strategy_requirements
                    else "generic_compatibility_window"
                ),
                "option_chain_calls": option_chain_calls,
                "expiration_count": len(expirations),
                "discovery_status": (
                    "complete"
                    if fetch_plan.expiration_discovery_complete
                    else "incomplete"
                ),
                "discovery_outcome": str(
                    getattr(
                        fetch_plan.expiration_discovery,
                        "outcome",
                        "",
                    )
                    or "missing"
                ),
                "discovery_error": fetch_plan.expiration_discovery_error,
                "projection_outcome": projection_outcome or "missing",
                "fetch_plan": fetch_plan.to_debug_dict(),
                "expected_fetch_contract": expected_fetch_contract,
            }
        )
    return {
        "plan_id": required_data_plan_id(items),
        "planning_scope": "run",
        "strategy_expiration_limit": None,
        "symbols": items,
        "symbols_count": len(items),
        "estimated_option_chain_calls": sum(
            int(item["option_chain_calls"])
            for item in items
        ),
        "discovery_complete": all(
            item["discovery_status"] == "complete"
            for item in items
        ),
        "planning_complete": all(
            item["projection_outcome"]
            in {"success_rows", "success_empty"}
            for item in items
        ),
    }


def _expected_fetch_contract(
    symbol_cfg: dict[str, Any],
    fetch_plan: RequiredDataFetchPlanBundle,
) -> dict[str, Any]:
    fetch_cfg = _as_dict(symbol_cfg.get("fetch"))
    source, _decision = resolve_symbol_fetch_source(fetch_cfg)
    return build_required_data_expected_fetch_contract(
        symbol=str(symbol_cfg.get("symbol") or fetch_plan.symbol),
        fetch_plan=fetch_plan.to_debug_dict(),
        source=source,
        host=str(fetch_cfg.get("host") or "127.0.0.1"),
        port=_to_int(fetch_cfg.get("port") or 11111, 11111),
    )


def _publish_planned_success_empty(
    symbol_cfg: dict[str, Any],
    *,
    base: Path,
    shared_required: Path,
    opend_fetch_cfg: dict[str, Any],
    fetch_plan: RequiredDataFetchPlanBundle,
    expected_fetch_contract: dict[str, Any],
    producer_run_id: str | None,
    execution_mode: str,
) -> dict[str, Any]:
    symbol = str(symbol_cfg.get("symbol") or "").strip()
    fetch_cfg = _as_dict(symbol_cfg.get("fetch"))
    source, _decision = resolve_symbol_fetch_source(fetch_cfg)
    discovery = fetch_plan.expiration_discovery
    if (
        fetch_plan.projection_outcome != "success_empty"
        or discovery is None
        or discovery.outcome != "success_empty"
        or discovery.reason_code != "no_expirations"
        or not str(discovery.observed_at_utc or "").strip()
        or discovery.expirations
    ):
        raise RuntimeError(
            "success-empty scheduled plan lacks valid discovery evidence"
        )
    host = str(fetch_cfg.get("host") or "127.0.0.1")
    port = _to_int(fetch_cfg.get("port") or 11111, 11111)
    payload0 = {
        "symbol": symbol,
        "underlier_code": discovery.request_identity.get("underlier"),
        "spot": fetch_plan.spot_reference,
        "expiration_count": 0,
        "expirations": [],
        "rows": [],
        "meta": {
            "source": "opend",
            "host": host,
            "port": port,
            "status": "ok",
            "source_outcome": "success_empty",
            "reason_code": "no_expirations",
            "error_code": None,
            "error": None,
            "expiration_statuses": {},
            "errors": [],
            "diagnostics": [
                {
                    "diagnostic_code": "NO_EXPIRATIONS",
                    "message": "no_expirations",
                }
            ],
            "expiration_opend_calls": 0,
            "expiration_cache_hits": 0,
            "opend_call_count": 0,
            "snapshot_requested_codes": 0,
            "snapshot_returned_codes": 0,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "snapshot_requested_code_set": [],
            "snapshot_returned_code_set": [],
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "snapshot_complete": True,
            "realized_volatility": {
                "status": "not_applicable_no_contracts",
                "reason": "not_applicable_no_contracts",
                "sample_count": 0,
                "realized_volatility_20": None,
                "realized_volatility_60": None,
                "realized_volatility_120": None,
                "realized_volatility_estimate": None,
            },
            "source_observed_at": discovery.observed_at_utc,
            "completed_at_utc": discovery.completed_at_utc,
            "trading_date": discovery.request_identity.get("trading_date"),
        },
    }
    finalized = finalize_required_data_quote_candidate(
        base=base,
        producer_root=shared_required,
        producer_run_id=producer_run_id,
        symbol=symbol,
        expected_fetch_contract=expected_fetch_contract,
        fetch_policy={
            "source": source,
            "host": host,
            "port": port,
            "limit_expirations": 0,
            "fetch_kwargs": _prefetch_fetch_kwargs_from_plan(fetch_plan),
            "opend_fetch": opend_fetch_cfg,
            "execution_mode": execution_mode,
        },
        mode="success_empty",
        payload=payload0,
    )
    quote_receipt_path = finalized.get("quote_receipt_path")
    quote_receipt_relpath = (
        Path(quote_receipt_path).resolve()
        .relative_to(shared_required.resolve())
        .as_posix()
        if quote_receipt_path is not None
        else None
    )
    payload = normalize_tool_execution_payload(
        tool_name="required_data_prefetch",
        symbol=symbol,
        source=source,
        limit_exp=0,
        status="fetched",
        ok=True,
        message="success_empty:no_expirations",
        returncode=0,
    )
    payload["payload"] = payload0
    if quote_receipt_relpath is not None:
        payload["quote_source_receipt"] = quote_receipt_relpath
    source_snapshot = adapt_opend_tool_payload(payload)
    payload["source_snapshot"] = source_snapshot
    try:
        state_repo.append_source_snapshot_event(base, source_snapshot)
    except Exception:
        pass
    return payload


def _fetch_one_inprocess(
    symbol_cfg: dict[str, Any],
    *,
    base: Path,
    shared_required: Path,
    opend_fetch_cfg: dict[str, Any],
    batch_cfg: Any,
    fetch_plan: RequiredDataFetchPlanBundle | None = None,
    expected_fetch_contract: dict[str, Any] | None = None,
    producer_run_id: str | None = None,
) -> dict[str, Any]:
    symbol = str(symbol_cfg.get('symbol')).strip()
    if not symbol:
        payload = normalize_tool_execution_payload(
            tool_name='required_data_prefetch',
            symbol='',
            source='unknown',
            limit_exp=0,
            status='error',
            ok=False,
            message='empty_symbol',
            returncode=None,
        )
        source_snapshot = adapt_opend_tool_payload(payload)
        payload["source_snapshot"] = source_snapshot
        try:
            state_repo.append_source_snapshot_event(base, source_snapshot)
        except Exception:
            pass
        return payload

    fetch_cfg = (symbol_cfg.get('fetch') or {}) if isinstance(symbol_cfg, dict) else {}
    src, _decision = resolve_symbol_fetch_source(fetch_cfg)
    limit_exp = _prefetch_limit_expirations(symbol_cfg, fetch_plan)
    host = str(fetch_cfg.get('host') or '127.0.0.1')
    port = _to_int(fetch_cfg.get('port') or 11111, 11111)
    fetch_kwargs = _prefetch_fetch_kwargs_from_plan(fetch_plan)
    provider_succeeded = False
    provider_payload: dict[str, Any] | None = None
    try:
        if (
            fetch_plan is not None
            and fetch_plan.projection_outcome == "success_empty"
        ):
            return _publish_planned_success_empty(
                symbol_cfg,
                base=base,
                shared_required=shared_required,
                opend_fetch_cfg=opend_fetch_cfg,
                fetch_plan=fetch_plan,
                expected_fetch_contract=(
                    expected_fetch_contract
                    or _expected_fetch_contract(symbol_cfg, fetch_plan)
                ),
                producer_run_id=producer_run_id,
                execution_mode="inprocess_short_circuit",
            )
        if fetch_plan is not None and fetch_plan.projection_outcome != "success_rows":
            raise RuntimeError(
                "scheduled fetch plan is not executable"
            )
        gateway = _gateway_pool.get_gateway(host=host, port=port, chain_cache=True)
        if fetch_plan is None:
            payload0 = fetch_symbol(
                symbol,
                limit_expirations=limit_exp,
                host=host,
                port=port,
                spot_override=fetch_kwargs.get("spot_override"),
                base_dir=base,
                option_types=str(fetch_kwargs["option_types"]),
                side_strike_windows=fetch_kwargs.get("side_strike_windows"),
                min_dte=fetch_kwargs.get("min_dte"),
                max_dte=fetch_kwargs.get("max_dte"),
                explicit_expirations=fetch_kwargs.get("explicit_expirations"),
                chain_cache=True,
                chain_cache_force_refresh=False,
                freshness_policy='cache_first',
                gateway=gateway,
                snapshot_batch_size=int(getattr(batch_cfg, 'market_snapshot', 0) or 0),
                snapshot_fallback_max_codes=int(getattr(batch_cfg, 'market_snapshot_fallback_max_codes', 100) or 0),
                snapshot_fallback_batch_size=int(getattr(batch_cfg, 'market_snapshot_fallback_batch_size', 20) or 20),
                max_wait_sec=float(opend_fetch_cfg['option_chain']['max_wait_sec']),
                option_chain_window_sec=float(opend_fetch_cfg['option_chain']['window_sec']),
                option_chain_max_calls=int(opend_fetch_cfg['option_chain']['max_calls']),
                snapshot_max_wait_sec=float(opend_fetch_cfg['market_snapshot']['max_wait_sec']),
                snapshot_window_sec=float(opend_fetch_cfg['market_snapshot']['window_sec']),
                snapshot_max_calls=int(opend_fetch_cfg['market_snapshot']['max_calls']),
                expiration_max_wait_sec=float(opend_fetch_cfg['option_expiration']['max_wait_sec']),
                expiration_window_sec=float(opend_fetch_cfg['option_expiration']['window_sec']),
                expiration_max_calls=int(opend_fetch_cfg['option_expiration']['max_calls']),
                include_realized_volatility=bool(fetch_kwargs.get("include_realized_volatility")),
            )
        else:
            specs = list(fetch_plan.merged_specs)
            if not specs:
                raise RuntimeError("success-rows scheduled plan has no fetch specs")
            child_payloads: list[dict[str, object]] = []
            for request_index, spec in enumerate(specs):
                request = build_fetch_request_from_spec(
                    spec=spec,
                    output_root=shared_required,
                    chain_cache=True,
                    chain_cache_force_refresh=False,
                    opend_fetch_config=_flat_opend_fetch_config(
                        opend_fetch_cfg
                    ),
                    spot_override=fetch_plan.spot_reference,
                )
                child_payload = _fetch_symbol_for_required_data_request(
                    request=request,
                    base=base,
                    gateway=gateway,
                    batch_cfg=batch_cfg,
                )
                if not isinstance(child_payload, dict):
                    raise RuntimeError(
                        "required-data provider returned an invalid child payload"
                    )
                child_meta = child_payload.get("meta")
                child_meta = child_meta if isinstance(child_meta, dict) else {}
                if str(child_meta.get("status") or "").strip().lower() != "ok":
                    provider_payload = child_payload
                    raise RuntimeError(
                        str(
                            child_meta.get("error")
                            or child_meta.get("message")
                            or child_meta.get("error_code")
                            or "required-data provider returned an error payload"
                        )
                    )
                if len(specs) > 1:
                    child_payload = bind_required_data_child_request_evidence(
                        payload=child_payload,
                        planned_request=spec.to_debug_dict(),
                        request_index=request_index,
                    )
                child_payloads.append(child_payload)
            if len(child_payloads) == 1:
                payload0 = child_payloads[0]
            else:
                payload0 = merge_required_data_payloads(
                    symbol=symbol,
                    payloads=child_payloads,
                )
                bind_merged_payload_evidence(
                    merged_payload=payload0,
                    payloads=child_payloads,
                )
        provider_payload = payload0 if isinstance(payload0, dict) else None
        raw_meta = payload0.get('meta')
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        ok = str(meta.get('status') or '').strip().lower() == 'ok'
        if not ok:
            raise RuntimeError(
                str(
                    meta.get("error")
                    or meta.get("error_code")
                    or "required-data provider returned an error payload"
                )
            )
        contract = expected_fetch_contract
        if contract is None:
            if fetch_plan is None:
                raise RuntimeError("required-data expected fetch contract is missing")
            contract = _expected_fetch_contract(symbol_cfg, fetch_plan)
        validate_required_data_payload_candidate(
            payload=payload0,
            expected_fetch_contract=contract,
            require_fresh=True,
        )
        provider_succeeded = True
        _gateway_pool.mark_success()
        message = str(meta.get('error') or meta.get('status') or 'fetched')
        finalized = finalize_required_data_quote_candidate(
            base=base,
            producer_root=shared_required,
            producer_run_id=producer_run_id,
            symbol=symbol,
            expected_fetch_contract=contract,
            fetch_policy={
                "source": src,
                "host": host,
                "port": port,
                "limit_expirations": limit_exp,
                "fetch_kwargs": fetch_kwargs,
                "opend_fetch": opend_fetch_cfg,
                "snapshot_batch_size": int(
                    getattr(batch_cfg, 'market_snapshot', 0) or 0
                ),
                "snapshot_fallback_max_codes": int(
                    getattr(
                        batch_cfg,
                        'market_snapshot_fallback_max_codes',
                        100,
                    )
                    or 0
                ),
                "snapshot_fallback_batch_size": int(
                    getattr(
                        batch_cfg,
                        'market_snapshot_fallback_batch_size',
                        20,
                    )
                    or 20
                ),
                "execution_mode": "inprocess",
            },
            mode="fresh",
            payload=payload0,
        )
        quote_receipt_path = finalized.get("quote_receipt_path")
        quote_receipt_relpath = (
            Path(quote_receipt_path).resolve()
            .relative_to(shared_required.resolve())
            .as_posix()
            if quote_receipt_path is not None
            else None
        )
        payload = normalize_tool_execution_payload(
            tool_name='required_data_prefetch',
            symbol=symbol,
            source=src,
            limit_exp=limit_exp,
            status=('fetched' if ok else 'error'),
            ok=ok,
            message=message,
            returncode=(0 if ok else 1),
        )
        if isinstance(payload0, dict):
            payload['payload'] = payload0
        if quote_receipt_relpath is not None:
            payload["quote_source_receipt"] = quote_receipt_relpath
    except Exception as exc:
        if not provider_succeeded:
            _gateway_pool.mark_failure(
                provider_payload if provider_payload is not None else exc
            )
        message = str(exc or '')
        payload = normalize_tool_execution_payload(
            tool_name='required_data_prefetch',
            symbol=symbol,
            source=src,
            limit_exp=limit_exp,
            status='error',
            ok=False,
            message=message,
            returncode=None,
        )
        if provider_payload is not None:
            payload["payload"] = provider_payload
        if classify_opend_error({"message": message}).is_rate_limit:
            payload['error_code'] = 'RATE_LIMIT'
    source_snapshot = adapt_opend_tool_payload(payload)
    payload["source_snapshot"] = source_snapshot
    try:
        state_repo.append_source_snapshot_event(base, source_snapshot)
    except Exception:
        pass
    return payload


def _merge_coordinator_results(
    results: list[PrefetchCoordinatorResult],
    *,
    fail_budget_consecutive: int,
    fail_budget_total: int,
) -> PrefetchCoordinatorResult:
    merged = PrefetchCoordinatorResult(
        fail_budget_consecutive=fail_budget_consecutive,
        fail_budget_total=fail_budget_total,
    )
    for result in results:
        merged.fetched_ok += int(result.fetched_ok)
        merged.errors += int(result.errors)
        merged.skipped += int(result.skipped)
        merged.submitted_count += int(result.submitted_count)
        merged.completed_count += int(result.completed_count)
        merged.budget_triggered = bool(merged.budget_triggered or result.budget_triggered)
        merged.opend_rate_limit_classes.update(result.opend_rate_limit_classes)
        merged.opend_rate_limit_items.extend(result.opend_rate_limit_items)
        merged.results.update(result.results)
        merged.audit_items.extend(result.audit_items)
    return merged


def _sleep_after_rate_limit_wave(wait_sec: float) -> None:
    time.sleep(max(0.0, float(wait_sec)))


def _has_option_chain_rate_limit(items: list[dict[str, Any]]) -> bool:
    for item in items:
        endpoint = str(item.get("endpoint") or "").strip().lower()
        if endpoint in {"option_chain", "opend", ""}:
            return True
    return False


@contextmanager
def _required_data_prefetch_file_lock(base: Path) -> Iterator[None]:
    lock_path = Path(base) / "output_shared" / "state" / "required_data_prefetch.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_fp:
        if fcntl is not None:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


def prefetch_required_data(
    *,
    vpy: Path,
    base: Path,
    repo_root: Path | None = None,
    cfg: dict[str, Any],
    shared_required: Path,
    force_refresh: bool = False,
    producer_run_id: str | None = None,
) -> dict[str, Any]:
    with _required_data_prefetch_file_lock(base):
        return _prefetch_required_data_unlocked(
            vpy=vpy,
            base=base,
            repo_root=repo_root,
            cfg=cfg,
            shared_required=shared_required,
            force_refresh=force_refresh,
            producer_run_id=producer_run_id,
        )


def _prefetch_required_data_unlocked(
    *,
    vpy: Path,
    base: Path,
    repo_root: Path | None = None,
    cfg: dict[str, Any],
    shared_required: Path,
    force_refresh: bool = False,
    producer_run_id: str | None = None,
) -> dict[str, Any]:
    profiles = resolve_templates_config(cfg)
    syms = [apply_profiles(it, profiles) for it in resolve_watchlist_config(cfg) if it.get('symbol')]
    symbols = [str(it.get('symbol')).strip() for it in syms if str(it.get('symbol')).strip()]
    symbol_plan = build_prefetch_symbol_plan(syms)
    fetch_syms = symbol_plan.symbol_cfgs

    raw_dir = (shared_required / 'raw').resolve()
    parsed_dir = (shared_required / 'parsed').resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    process_root = (repo_root or base).resolve()
    exec_service = ToolExecutionService(base=base)
    opend_fetch_cfg = _resolve_opend_fetch_cfg(cfg)
    batch_cfg = resolve_opend_batch_config(cfg)
    execution_mode = _resolve_execution_mode(cfg)
    option_chain_fetch_cfg = opend_fetch_cfg["option_chain"]
    snapshot_fetch_cfg = opend_fetch_cfg["market_snapshot"]
    expiration_fetch_cfg = opend_fetch_cfg["option_expiration"]
    expiration_discovery_cache: dict[tuple[Any, ...], Any] = {}
    fetch_plan_cache: dict[int, RequiredDataFetchPlanBundle] = {
        id(symbol_cfg): _build_prefetch_fetch_plan(
            symbol_cfg,
            base=base,
            shared_required=shared_required,
            opend_fetch_cfg=opend_fetch_cfg,
            expiration_discovery_cache=expiration_discovery_cache,
        )
        for symbol_cfg in fetch_syms
    }
    invalid_plan_symbols = [
        str(symbol_cfg.get("symbol") or "").strip()
        for symbol_cfg in fetch_syms
        if str(
            fetch_plan_cache[id(symbol_cfg)].projection_outcome or ""
        ).strip()
        not in {"success_rows", "success_empty"}
    ]
    if invalid_plan_symbols:
        raise RuntimeError(
            "global required-data plan incomplete; discovery or projection failed for: "
            + ", ".join(invalid_plan_symbols)
        )
    expected_contract_cache: dict[int, dict[str, Any]] = {
        id(symbol_cfg): _expected_fetch_contract(
            symbol_cfg,
            fetch_plan_cache[id(symbol_cfg)],
        )
        for symbol_cfg in fetch_syms
    }
    global_required_data_plan = _global_required_data_plan_summary(
        symbol_cfgs=fetch_syms,
        fetch_plans_by_config_id=fetch_plan_cache,
        expected_contracts_by_config_id=expected_contract_cache,
    )
    if not bool(global_required_data_plan["planning_complete"]):
        failed_symbols = [
            str(item.get("symbol") or "")
            for item in global_required_data_plan["symbols"]
            if item.get("projection_outcome")
            not in {"success_rows", "success_empty"}
        ]
        raise RuntimeError(
            "global required-data plan incomplete; discovery or projection failed for: "
            + ", ".join(failed_symbols)
        )

    def _get_fetch_plan(symbol_cfg: dict[str, Any]) -> RequiredDataFetchPlanBundle:
        cache_key = id(symbol_cfg)
        cached = fetch_plan_cache.get(cache_key)
        if cached is not None:
            return cached
        raise RuntimeError("symbol is missing from the global required-data plan")

    def _get_expected_contract(symbol_cfg: dict[str, Any]) -> dict[str, Any]:
        cached = expected_contract_cache.get(id(symbol_cfg))
        if cached is not None:
            return cached
        raise RuntimeError("symbol expected fetch contract is unavailable")

    def _need_fetch(symbol_cfg: dict[str, Any]) -> bool:
        symbol = str(symbol_cfg.get('symbol')).strip()
        if not symbol:
            return True
        if force_refresh:
            return True
        try:
            validate_required_data_quote_candidate(
                producer_root=shared_required,
                raw_path=(
                    shared_required
                    / "raw"
                    / f"{symbol}_required_data.json"
                ),
                csv_path=(
                    shared_required
                    / "parsed"
                    / f"{symbol}_required_data.csv"
                ),
                expected_fetch_contract=_get_expected_contract(symbol_cfg),
                require_fresh=True,
            )
            return False
        except Exception:
            return True

    def _fetch_one(symbol_cfg: dict[str, Any]) -> dict[str, Any]:
        symbol = str(symbol_cfg.get('symbol')).strip()
        if not symbol:
            return normalize_tool_execution_payload(
                tool_name='required_data_prefetch',
                symbol='',
                source='unknown',
                limit_exp=0,
                status='error',
                ok=False,
                message='empty_symbol',
                returncode=None,
            )
        fetch_cfg = (symbol_cfg.get('fetch') or {}) if isinstance(symbol_cfg, dict) else {}
        src, _decision = resolve_symbol_fetch_source(fetch_cfg)
        fetch_plan = _get_fetch_plan(symbol_cfg)
        if fetch_plan.projection_outcome == "success_empty":
            return _publish_planned_success_empty(
                symbol_cfg,
                base=base,
                shared_required=shared_required,
                opend_fetch_cfg=opend_fetch_cfg,
                fetch_plan=fetch_plan,
                expected_fetch_contract=_get_expected_contract(symbol_cfg),
                producer_run_id=producer_run_id,
                execution_mode="subprocess_short_circuit",
            )
        if fetch_plan.projection_outcome != "success_rows":
            raise RuntimeError(
                "scheduled fetch plan is not executable"
            )
        if len(fetch_plan.merged_specs) > 1:
            payload = normalize_tool_execution_payload(
                tool_name="required_data_prefetch",
                symbol=symbol,
                source=src,
                limit_exp=0,
                status="error",
                ok=False,
                message="required_data_multi_spec_subprocess_unsupported",
                returncode=None,
            )
            payload["error_code"] = (
                "REQUIRED_DATA_MULTI_SPEC_SUBPROCESS_UNSUPPORTED"
            )
            return payload
        fetch_kwargs = _prefetch_fetch_kwargs_from_plan(fetch_plan)
        limit_exp = _prefetch_limit_expirations(symbol_cfg, fetch_plan)
        opt_types = str(fetch_kwargs["option_types"])

        cmd = [
            str(vpy), '-m', 'src.application.opend_symbol_fetching_cli',
            '--symbols', symbol,
            '--limit-expirations', str(limit_exp),
            '--host', str(fetch_cfg.get('host') or '127.0.0.1'),
            '--port', str(int(fetch_cfg.get('port') or 11111)),
            '--option-types', opt_types,
            '--output-root', str(shared_required),
            '--chain-cache',
            '--option-chain-window-sec', str(option_chain_fetch_cfg["window_sec"]),
            '--option-chain-max-calls', str(option_chain_fetch_cfg["max_calls"]),
            '--option-chain-max-wait-sec', str(option_chain_fetch_cfg["max_wait_sec"]),
            '--snapshot-window-sec', str(snapshot_fetch_cfg["window_sec"]),
            '--snapshot-max-calls', str(snapshot_fetch_cfg["max_calls"]),
            '--snapshot-max-wait-sec', str(snapshot_fetch_cfg["max_wait_sec"]),
            '--snapshot-batch-size', str(int(getattr(batch_cfg, 'market_snapshot', 0) or 0)),
            '--snapshot-fallback-max-codes', str(int(getattr(batch_cfg, 'market_snapshot_fallback_max_codes', 100) or 0)),
            '--snapshot-fallback-batch-size', str(int(getattr(batch_cfg, 'market_snapshot_fallback_batch_size', 20) or 20)),
            '--expiration-window-sec', str(expiration_fetch_cfg["window_sec"]),
            '--expiration-max-calls', str(expiration_fetch_cfg["max_calls"]),
            '--expiration-max-wait-sec', str(expiration_fetch_cfg["max_wait_sec"]),
            '--quiet',
        ]
        if fetch_kwargs.get("spot_override") is not None:
            cmd.extend(['--spot', str(fetch_kwargs["spot_override"])])
        if fetch_kwargs.get("min_dte") is not None:
            cmd.extend(['--min-dte', str(fetch_kwargs["min_dte"])])
        if fetch_kwargs.get("max_dte") is not None:
            cmd.extend(['--max-dte', str(fetch_kwargs["max_dte"])])
        if fetch_kwargs.get("side_strike_windows"):
            cmd.extend(['--side-strike-windows-json', json.dumps(fetch_kwargs["side_strike_windows"])])
        if fetch_kwargs.get("explicit_expirations"):
            cmd.extend(['--explicit-expirations', *[str(exp) for exp in fetch_kwargs["explicit_expirations"]]])
        if fetch_kwargs.get("include_realized_volatility"):
            cmd.append('--include-realized-volatility')
        trading_date = fetch_kwargs.get("trading_date")
        if isinstance(trading_date, str) and trading_date:
            cmd.extend(['--trading-date', trading_date])

        payload = exec_service.execute(
            ToolExecutionIntent(
                tool_name='required_data_prefetch',
                symbol=symbol,
                source=src,
                limit_exp=limit_exp,
                cmd=cmd,
                cwd=process_root,
                capture_output=True,
                text=True,
                idempotency_scope='required_data_prefetch',
                force_refresh=bool(force_refresh),
            )
        )
        if bool(payload.get("ok")):
            finalized = finalize_required_data_quote_candidate(
                base=base,
                producer_root=shared_required,
                producer_run_id=producer_run_id,
                symbol=symbol,
                expected_fetch_contract=_get_expected_contract(symbol_cfg),
                fetch_policy={
                    "source": src,
                    "host": str(fetch_cfg.get('host') or '127.0.0.1'),
                    "port": int(fetch_cfg.get('port') or 11111),
                    "limit_expirations": limit_exp,
                    "fetch_kwargs": fetch_kwargs,
                    "opend_fetch": opend_fetch_cfg,
                    "execution_mode": "subprocess",
                },
                mode="subprocess",
            )
            quote_receipt_path = finalized.get("quote_receipt_path")
            if quote_receipt_path is not None:
                payload["quote_source_receipt"] = (
                    Path(quote_receipt_path).resolve()
                    .relative_to(shared_required.resolve())
                    .as_posix()
                )
        # Canonical adapter validation before entering next layer.
        source_snapshot = adapt_opend_tool_payload(payload)
        payload["source_snapshot"] = source_snapshot
        try:
            state_repo.append_source_snapshot_event(base, source_snapshot)
        except Exception:
            pass
        return payload

    todo_cfgs = [it for it in fetch_syms if _need_fetch(it)]
    todo_ids = {id(item) for item in todo_cfgs}
    cached_failure_result = PrefetchCoordinatorResult()
    for symbol_cfg in fetch_syms:
        if id(symbol_cfg) in todo_ids:
            continue
        symbol = str(symbol_cfg.get("symbol") or "").strip()
        fetch_cfg = _as_dict(symbol_cfg.get("fetch"))
        fetch_plan = _get_fetch_plan(symbol_cfg)
        source, _decision = resolve_symbol_fetch_source(fetch_cfg)
        try:
            finalize_required_data_quote_candidate(
                base=base,
                producer_root=shared_required,
                producer_run_id=producer_run_id,
                symbol=symbol,
                expected_fetch_contract=_get_expected_contract(symbol_cfg),
                fetch_policy={
                    "source": source,
                    "host": str(fetch_cfg.get("host") or "127.0.0.1"),
                    "port": _to_int(fetch_cfg.get("port") or 11111, 11111),
                    "limit_expirations": _prefetch_limit_expirations(
                        symbol_cfg,
                        fetch_plan,
                    ),
                    "fetch_kwargs": _prefetch_fetch_kwargs_from_plan(fetch_plan),
                    "opend_fetch": opend_fetch_cfg,
                    "execution_mode": "cached",
                },
                mode="cached",
            )
        except Exception as exc:
            cached_failure_result.errors += 1
            cached_failure_result.completed_count += 1
            cached_failure_result.results[symbol] = str(exc)
            cached_failure_result.audit_items.append(
                {
                    "symbol": symbol,
                    "status": "error",
                    "execution_mode": "cached",
                    "message": str(exc),
                    "error_type": type(exc).__name__,
                }
            )
    unique_cached_count = max(
        0,
        len(fetch_syms) - len(todo_cfgs) - cached_failure_result.errors,
    )
    budget_plan = build_prefetch_budget_plan(
        todo_cfgs,
        option_chain_cfg=option_chain_fetch_cfg,
        fetch_plans_by_config_id=fetch_plan_cache,
    )
    option_chain_fetch_cfg = dict(option_chain_fetch_cfg)
    option_chain_fetch_cfg["max_calls"] = int(budget_plan.safe_option_chain_calls_per_window)
    opend_fetch_cfg = dict(opend_fetch_cfg)
    opend_fetch_cfg["option_chain"] = option_chain_fetch_cfg

    if not todo_cfgs:
        fetch_metrics = summarize_prefetch_fetch_metrics(
            cached_failure_result.audit_items
        )
        run_fetch_summary = summarize_required_data_prefetch_run(
            symbols_total=len(symbols),
            unique_symbols_total=len(fetch_syms),
            to_fetch=0,
            cached_unique_symbols=unique_cached_count,
            submitted_count=0,
            completed_count=cached_failure_result.completed_count,
            skipped_count=0,
            failed_count=cached_failure_result.errors,
            fetch_metrics=fetch_metrics,
            dedupe=symbol_plan.summary(),
        )
        return {
            'schema_version': SCHEMA_VERSION_V1,
            'symbols_total': len(symbols),
            'unique_symbols_total': len(fetch_syms),
            'deduped_count': symbol_plan.deduped_count,
            'dedupe': symbol_plan.summary(),
            'to_fetch': 0,
            'fetched': 0,
            'fetched_ok': 0,
            'cached': max(0, len(symbols) - cached_failure_result.errors),
            'cached_unique_symbols': unique_cached_count,
            'errors': cached_failure_result.errors,
            'skipped': 0,
            'max_workers': 0,
            'prefetch_max_workers': _resolve_prefetch_max_workers(cfg),
            'effective_prefetch_workers': 0,
            'submitted_count': 0,
            'completed_count': cached_failure_result.completed_count,
            'skipped_count': 0,
            'failed_count': cached_failure_result.errors,
            'execution_mode': _resolve_execution_mode(cfg),
            'fetch_metrics': fetch_metrics,
            'run_fetch_summary': run_fetch_summary,
            'prefetch_budget_plan': budget_plan.summary(),
            'global_required_data_plan': global_required_data_plan,
            'opend_rate_limit_classes': [],
            'opend_rate_limit_items': [],
            'rate_limit_cooldowns': [],
            'symbols': cached_failure_result.symbol_items,
            'results': cached_failure_result.results,
            'audit': cached_failure_result.audit_items,
            'quote_receipts': find_fresh_required_data_quote_receipts(
                producer_root=shared_required,
                symbols=symbols,
            ),
        }

    configured_max_workers = _resolve_prefetch_max_workers(cfg)
    fail_budget_consecutive, fail_budget_total = _resolve_failure_budget(cfg)

    def _dispatch(symbol_cfg: dict[str, Any]) -> dict[str, Any]:
        if execution_mode == 'subprocess':
            return _fetch_one(symbol_cfg)
        return _fetch_one_inprocess(
            symbol_cfg,
            base=base,
            shared_required=shared_required,
            opend_fetch_cfg=opend_fetch_cfg,
            batch_cfg=batch_cfg,
            fetch_plan=_get_fetch_plan(symbol_cfg),
            expected_fetch_contract=_get_expected_contract(symbol_cfg),
            producer_run_id=producer_run_id,
        )

    wave_results: list[PrefetchCoordinatorResult] = (
        [cached_failure_result]
        if cached_failure_result.errors
        else []
    )
    rate_limit_cooldowns: list[dict[str, Any]] = []
    effective_max_workers = 0
    for wave_idx, wave in enumerate(budget_plan.waves):
        wave_workers = max(1, min(configured_max_workers, len(wave.symbol_cfgs)))
        effective_max_workers = max(effective_max_workers, wave_workers)
        coordinator = PrefetchCoordinator(
            symbol_cfgs=wave.symbol_cfgs,
            max_workers=wave_workers,
            execution_mode=execution_mode,
            fail_budget_consecutive=fail_budget_consecutive,
            fail_budget_total=fail_budget_total,
            dispatch_fn=_dispatch,
            cleanup_worker_fn=(_gateway_pool.close_current_thread if execution_mode == 'inprocess' else None),
            short_circuit_rate_limits=False,
            stop_on_failure_budget=False,
        )
        wave_result = coordinator.run()
        wave_results.append(wave_result)
        if wave_idx < len(budget_plan.waves) - 1 and _has_option_chain_rate_limit(wave_result.opend_rate_limit_items):
            wait_sec = float(option_chain_fetch_cfg.get("window_sec") or 30.0)
            rate_limit_cooldowns.append(
                {
                    "after_wave": int(wave.index),
                    "reason": "opend_rate_limit",
                    "wait_sec": wait_sec,
                }
            )
            _sleep_after_rate_limit_wave(wait_sec)
    max_workers = effective_max_workers
    coordinator_result = _merge_coordinator_results(
        wave_results,
        fail_budget_consecutive=fail_budget_consecutive,
        fail_budget_total=fail_budget_total,
    )
    fetch_metrics = summarize_prefetch_fetch_metrics(coordinator_result.audit_items)
    run_fetch_summary = summarize_required_data_prefetch_run(
        symbols_total=len(symbols),
        unique_symbols_total=len(fetch_syms),
        to_fetch=len(todo_cfgs),
        cached_unique_symbols=unique_cached_count,
        submitted_count=coordinator_result.submitted_count,
        completed_count=coordinator_result.completed_count,
        skipped_count=coordinator_result.skipped,
        failed_count=coordinator_result.errors,
        fetch_metrics=fetch_metrics,
        dedupe=symbol_plan.summary(),
    )

    if execution_mode == 'inprocess':
        _gateway_pool.close_registered()

    return {
        'schema_version': SCHEMA_VERSION_V1,
        'symbols_total': len(symbols),
        'unique_symbols_total': len(fetch_syms),
        'deduped_count': symbol_plan.deduped_count,
        'dedupe': symbol_plan.summary(),
        'to_fetch': len(todo_cfgs),
        'cached_unique_symbols': unique_cached_count,
        'max_workers': max_workers,
        'prefetch_max_workers': configured_max_workers,
        'effective_prefetch_workers': max_workers,
        'execution_mode': execution_mode,
        'fetched_ok': coordinator_result.fetched_ok,
        'errors': coordinator_result.errors,
        'skipped': coordinator_result.skipped,
        'submitted_count': coordinator_result.submitted_count,
        'completed_count': coordinator_result.completed_count,
        'skipped_count': coordinator_result.skipped,
        'failed_count': coordinator_result.errors,
        'fail_budget_consecutive': fail_budget_consecutive,
        'fail_budget_total': fail_budget_total,
        'budget_triggered': coordinator_result.budget_triggered,
        'opend_rate_limit_classes': sorted(coordinator_result.opend_rate_limit_classes),
        'opend_rate_limit_items': list(coordinator_result.opend_rate_limit_items),
        'prefetch_budget_plan': budget_plan.summary(),
        'global_required_data_plan': global_required_data_plan,
        'rate_limit_cooldowns': rate_limit_cooldowns,
        'fetch_metrics': fetch_metrics,
        'run_fetch_summary': run_fetch_summary,
        'force_refresh': bool(force_refresh),
        'results': coordinator_result.results,
        'symbols': coordinator_result.symbol_items,
        'audit': coordinator_result.audit_items,
        'quote_receipts': find_fresh_required_data_quote_receipts(
            producer_root=shared_required,
            symbols=symbols,
        ),
    }
