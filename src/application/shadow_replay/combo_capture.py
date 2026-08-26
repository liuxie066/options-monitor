from __future__ import annotations

"""Isolated required-data capture for Combo Yield Shadow variants."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Callable
import uuid

from src.application.account_run import build_account_runtime_config
from src.application.agent_tool_config import load_runtime_config
from src.application.config_profiles import apply_profiles
from src.application.config_sections import resolve_templates_config, resolve_watchlist_config
from src.application.opend_fetch_config import opend_discovery_kwargs, opend_fetch_kwargs
from src.application.opend_symbol_outputs import save_outputs
from src.application.pipeline_watchlist import resolve_watchlist_item_runtime_config
from src.application.required_data_fetching import (
    build_fetch_request_from_spec,
    execute_required_data_opend,
    merge_required_data_payloads,
)
from src.application.required_data_planning import (
    RequiredDataFetchPlanBundle,
    _merge_same_side_plans,
    _merge_side_plans,
    build_required_data_fetch_plan,
)
from src.application.required_data_prefetch_planning import build_prefetch_budget_plan
from src.application.shadow_replay.combo_variants import (
    COMBO_CAPTURE_MANIFEST_SCHEMA_VERSION,
    COMBO_PAIR_DATASET_FILES,
    combo_variant_spec_hash,
    load_combo_variant_spec,
    refresh_combo_pair_facet_manifest,
)
from src.application.shadow_replay.common import (
    DATASET_FILES,
    DATASET_SCHEMA_VERSION,
    default_dataset_id,
    refresh_dataset_manifest,
    safety_payload,
    utc_now,
    write_json,
    write_jsonl,
)
from src.application.combo_yield_config import (
    derive_combo_yield_policy,
    resolve_combo_yield_cfg,
)


def capture_combo_variants(
    *,
    repo_root: str | Path,
    config_key: str,
    account: str,
    symbols: list[str],
    variant_spec_path: str | Path,
    dataset_root: str | Path | None = None,
    dataset_id: str | None = None,
    write: bool = False,
    opend_host: str | None = None,
    opend_port: int | None = None,
    chain_cache: bool = True,
    chain_cache_force_refresh: bool = False,
    plan_builder: Callable[..., RequiredDataFetchPlanBundle] = build_required_data_fetch_plan,
    fetch_executor: Callable[..., dict[str, object]] = execute_required_data_opend,
) -> dict[str, Any]:
    """Plan or persist an isolated Combo variant required-data universe."""

    base = Path(repo_root).expanduser().resolve()
    market = str(config_key or "").strip().lower()
    if market not in {"us", "hk"}:
        raise ValueError("config_key must be us or hk")
    account_key = str(account or "").strip().lower()
    if not account_key:
        raise ValueError("account is required")
    normalized_symbols = sorted(
        {
            str(symbol or "").strip().upper()
            for symbol in symbols
            if str(symbol or "").strip()
        }
    )
    if not normalized_symbols:
        raise ValueError("at least one symbol is required")
    variant_spec = load_combo_variant_spec(variant_spec_path)
    variant_hash = combo_variant_spec_hash(variant_spec)

    cfg_path, runtime_cfg = load_runtime_config(
        config_key=market,
        expected_market=market,
    )
    account_cfg = build_account_runtime_config(
        base_cfg=runtime_cfg,
        cfg_path=cfg_path,
        account=account_key,
        markets_to_run=[market.upper()],
        symbols_arg=",".join(normalized_symbols),
    )
    symbol_cfgs = _resolved_symbol_configs(
        account_cfg,
        requested_symbols=normalized_symbols,
    )
    missing_symbols = sorted(set(normalized_symbols) - set(symbol_cfgs))
    if missing_symbols:
        raise ValueError(f"symbols are not present in runtime config: {', '.join(missing_symbols)}")

    resolved_dataset_id = str(dataset_id or default_dataset_id()).strip()
    root = _resolve_path_from_base(
        dataset_root,
        base=base,
        default=base / "output_shared" / "research" / "shadow_replay" / "datasets",
    )
    published_target = (root / resolved_dataset_id).resolve()
    if published_target.exists():
        raise ValueError(f"Combo Shadow dataset target already exists: {published_target}")
    target = (
        (root / f".{resolved_dataset_id}.staging-{uuid.uuid4().hex}").resolve()
        if write
        else published_target
    )
    research_required_data = target / "required_data"

    runtime = account_cfg.get("runtime") if isinstance(account_cfg.get("runtime"), dict) else {}
    futu = (
        (account_cfg.get("portfolio") or {}).get("futu")
        if isinstance(account_cfg.get("portfolio"), dict)
        else {}
    )
    futu = futu if isinstance(futu, dict) else {}
    host = str(opend_host or futu.get("host") or "127.0.0.1")
    port = int(opend_port or futu.get("port") or 11111)
    fetch_kwargs = opend_fetch_kwargs(account_cfg)
    discovery_kwargs = opend_discovery_kwargs(account_cfg)

    bundles: dict[str, RequiredDataFetchPlanBundle] = {}
    production_reference_plans: dict[str, RequiredDataFetchPlanBundle] = {}
    policy_payloads: dict[str, dict[str, Any]] = {}
    variant_plans: dict[str, dict[str, RequiredDataFetchPlanBundle]] = {}
    for symbol, symbol_cfg in symbol_cfgs.items():
        baseline_cfg = resolve_combo_yield_cfg(symbol_cfg)
        baseline_policy = derive_combo_yield_policy(baseline_cfg, market=market)
        policy_payloads[symbol] = baseline_policy.to_config()
        production_plan = plan_builder(
                base=base,
                required_data_dir=research_required_data,
                symbol=symbol,
                limit_expirations=0,
                want_put=False,
                want_call=False,
                sell_put_cfg=dict(symbol_cfg.get("sell_put") or {}),
                sell_call_cfg={},
                combo_yield_cfg=_force_combo_enabled(baseline_cfg),
                symbol_cfg=symbol_cfg,
                fetch_host=host,
                fetch_port=port,
                **discovery_kwargs,
            )
        production_reference_plans[symbol] = production_plan
        plans = [production_plan]
        variant_plans[symbol] = {}
        for raw_variant in variant_spec["variants"]:
            variant_cfg = _variant_combo_config(
                baseline_cfg,
                raw_variant,
            )
            plan = plan_builder(
                base=base,
                required_data_dir=research_required_data,
                symbol=symbol,
                limit_expirations=0,
                want_put=False,
                want_call=False,
                sell_put_cfg=dict(symbol_cfg.get("sell_put") or {}),
                sell_call_cfg={},
                combo_yield_cfg=variant_cfg,
                symbol_cfg=symbol_cfg,
                fetch_host=host,
                fetch_port=port,
                **discovery_kwargs,
            )
            plans.append(plan)
            variant_plans[symbol][str(raw_variant["variant_id"])] = plan
        bundles[symbol] = _union_bundles(
            symbol=symbol,
            bundles=plans,
            host=host,
            port=port,
        )

    option_chain_rate = (
        runtime.get("opend_rate_limits", {}).get("option_chain", {})
        if isinstance(runtime.get("opend_rate_limits"), dict)
        else {}
    )
    option_chain_rate = option_chain_rate if isinstance(option_chain_rate, dict) else {}
    budget = build_prefetch_budget_plan(
        list(symbol_cfgs.values()),
        option_chain_cfg=option_chain_rate,
        fetch_plans_by_config_id={
            id(symbol_cfg): bundles[symbol]
            for symbol, symbol_cfg in symbol_cfgs.items()
        },
    )
    authored_cap = int(variant_spec["max_estimated_option_chain_calls"])
    budget_exceeded = budget.estimated_option_chain_calls > authored_cap
    planning_complete = all(bundle.expiration_discovery_complete for bundle in bundles.values())
    if write and not planning_complete:
        failures = {
            symbol: bundle.expiration_discovery_error
            for symbol, bundle in bundles.items()
            if not bundle.expiration_discovery_complete
        }
        raise ValueError(f"Combo research expiration discovery incomplete: {failures}")
    if write and budget_exceeded:
        raise ValueError(
            "Combo research option-chain estimate exceeds authored cap: "
            f"{budget.estimated_option_chain_calls}>{authored_cap}"
        )
    captured_payloads: dict[str, dict[str, object]] = {}
    capture_observations: dict[str, list[dict[str, Any]]] = {}
    saved_paths: dict[str, list[Path]] = {}
    fetch_errors: dict[str, str] = {}

    if write:
        target.mkdir(parents=True, exist_ok=False)
        for name in DATASET_FILES + COMBO_PAIR_DATASET_FILES:
            write_jsonl(target / name, [])
        for symbol, bundle in bundles.items():
            try:
                payloads: list[dict[str, object]] = []
                observations: list[dict[str, Any]] = []
                for spec in bundle.merged_specs:
                    payload = fetch_executor(
                        base=base,
                        request=build_fetch_request_from_spec(
                            spec=spec,
                            output_root=research_required_data,
                            chain_cache=chain_cache,
                            chain_cache_force_refresh=chain_cache_force_refresh,
                            opend_fetch_config=fetch_kwargs,
                            spot_override=bundle.spot_reference,
                            underlier_observation=(
                                bundle.underlier_observation.to_dict()
                                if bundle.underlier_observation is not None
                                else None
                            ),
                        ),
                    )
                    observed_at = utc_now()
                    payloads.append(payload)
                    observations.append(
                        {
                            "option_types": list(spec.option_types),
                            "expirations": list(spec.explicit_expirations),
                            "observed_at_utc": observed_at,
                            "source": "opend",
                            "timestamp_basis": "fetch_completed_at_utc",
                        }
                    )
                merged = merge_required_data_payloads(symbol=symbol, payloads=payloads)
                captured_payloads[symbol] = merged
                capture_observations[symbol] = observations
                raw_path, csv_path = save_outputs(
                    base,
                    symbol,
                    merged,
                    output_root=research_required_data,
                )
                saved_paths[symbol] = [raw_path.resolve(), csv_path.resolve()]
            except Exception as exc:
                fetch_errors[symbol] = f"{type(exc).__name__}: {exc}"

    file_hashes = {
        str(path.relative_to(target)): _file_sha256(path)
        for paths in saved_paths.values()
        for path in paths
        if path.is_file()
    }
    variant_completeness = _variant_completeness(
        symbols=normalized_symbols,
        variant_plans=variant_plans,
        captured_payloads=captured_payloads,
        fetch_errors=fetch_errors,
        write=write,
    )
    if write and fetch_errors:
        shutil.rmtree(target)
        raise ValueError(f"Combo research fetch incomplete: {fetch_errors}")
    effective_policy_payload = {
        symbol: _normalized_json(policy)
        for symbol, policy in sorted(policy_payloads.items())
    }
    manifest = {
        "schema_version": COMBO_CAPTURE_MANIFEST_SCHEMA_VERSION,
        "dataset_schema_version": DATASET_SCHEMA_VERSION,
        "dataset_id": resolved_dataset_id,
        "capture_observed_at_utc": utc_now(),
        "market": market,
        "account": account_key,
        "symbols": normalized_symbols,
        "normalized_effective_combo_policy": effective_policy_payload,
        "research_enabled_override": {
            symbol: not bool(policy.get("enabled", False))
            for symbol, policy in sorted(effective_policy_payload.items())
        },
        "normalized_sell_put_policy": {
            symbol: _normalized_json(symbol_cfg.get("sell_put") or {})
            for symbol, symbol_cfg in sorted(symbol_cfgs.items())
        },
        "normalized_global_combo_liquidity": {
            symbol: _normalized_json(
                symbol_cfg.get("_global_combo_yield_liquidity") or {}
            )
            for symbol, symbol_cfg in sorted(symbol_cfgs.items())
        },
        "normalized_global_sell_put_liquidity": {
            symbol: _normalized_json(
                symbol_cfg.get("_global_sell_put_liquidity") or {}
            )
            for symbol, symbol_cfg in sorted(symbol_cfgs.items())
        },
        "normalized_global_sell_put_event_risk": {
            symbol: _normalized_json(
                symbol_cfg.get("_global_sell_put_event_risk") or {}
            )
            for symbol, symbol_cfg in sorted(symbol_cfgs.items())
        },
        "effective_combo_policy_hash": _canonical_hash(effective_policy_payload),
        "normalized_variant_spec": variant_spec,
        "variant_spec_hash": variant_hash,
        "planned_put_expirations": {
            symbol: _side_expirations(bundle, "put")
            for symbol, bundle in bundles.items()
        },
        "planned_call_expirations_by_variant": {
            symbol: {
                variant_id: _side_expirations(plan, "call")
                for variant_id, plan in plans.items()
            }
            for symbol, plans in variant_plans.items()
        },
        "discovered_expirations": {
            symbol: sorted(
                {
                    expiration
                    for bundle in [bundles[symbol]]
                    for side in bundle.side_plans
                    for expiration in side.explicit_expirations
                }
            )
            for symbol in normalized_symbols
        },
        "fetched_expirations": {
            symbol: sorted(str(value) for value in payload.get("expirations") or [])
            for symbol, payload in captured_payloads.items()
        },
        "source_quote_observations": capture_observations,
        "required_data_file_sha256": file_hashes,
        "variant_completeness": variant_completeness,
        "planning": {
            "complete": planning_complete,
            "errors": {
                symbol: bundle.expiration_discovery_error
                for symbol, bundle in bundles.items()
                if bundle.expiration_discovery_error
            },
            "plans": {
                symbol: bundle.to_debug_dict()
                for symbol, bundle in bundles.items()
            },
        },
        "production_reference_planning": {
            "complete": all(
                plan.expiration_discovery_complete
                for plan in production_reference_plans.values()
            ),
            "errors": {
                symbol: plan.expiration_discovery_error
                for symbol, plan in production_reference_plans.items()
                if plan.expiration_discovery_error
            },
            "plans": {
                symbol: plan.to_debug_dict()
                for symbol, plan in production_reference_plans.items()
            },
        },
        "research_supplement_planning": {
            "complete": all(
                plan.expiration_discovery_complete
                for plans in variant_plans.values()
                for plan in plans.values()
            ),
            "variants": {
                symbol: {
                    variant_id: {
                        "complete": plan.expiration_discovery_complete,
                        "error": plan.expiration_discovery_error,
                        "plan": plan.to_debug_dict(),
                    }
                    for variant_id, plan in plans.items()
                }
                for symbol, plans in variant_plans.items()
            },
        },
        "budget": {
            **budget.summary(),
            "max_estimated_option_chain_calls": authored_cap,
            "within_authored_cap": not budget_exceeded,
        },
        "fetch_errors": fetch_errors,
        "written": bool(write),
        "dataset_dir": str(published_target),
        "files": {
            name: str((published_target / name).resolve())
            for name in DATASET_FILES + COMBO_PAIR_DATASET_FILES
        },
        "safety": {
            **safety_payload(writes_local_dataset=bool(write)),
            "writes_quote_rate_limit_cache": bool(write and chain_cache),
            "writes_production_outputs": False,
            "broker_order_allowed": False,
            "pair_intent_allowed": False,
        },
    }
    if write:
        (target / ".dataset.lock").touch()
        write_json(target / "manifest.json", manifest)
        refresh_combo_pair_facet_manifest(target)
        manifest = refresh_dataset_manifest(target)
        manifest["dataset_dir"] = str(published_target)
        manifest["files"] = {
            name: str((published_target / name).resolve())
            for name in DATASET_FILES + COMBO_PAIR_DATASET_FILES
        }
        combo_facet = manifest.get("combo_pair_facet")
        if isinstance(combo_facet, dict):
            combo_facet["files"] = {
                name: str((published_target / name).resolve())
                for name in COMBO_PAIR_DATASET_FILES
            }
        write_json(target / "manifest.json", manifest)
        os.replace(target, published_target)
    return manifest


def _resolved_symbol_configs(
    cfg: dict[str, Any],
    *,
    requested_symbols: list[str],
) -> dict[str, dict[str, Any]]:
    profiles = resolve_templates_config(cfg)
    wanted = set(requested_symbols)
    out: dict[str, dict[str, Any]] = {}
    for raw in resolve_watchlist_config(cfg):
        symbol = str(raw.get("symbol") or "").strip().upper()
        if symbol not in wanted:
            continue
        resolved = resolve_watchlist_item_runtime_config(
            item=raw,
            profiles=profiles,
            apply_profiles_fn=apply_profiles,
        )
        out[symbol] = resolved
    return out


def _variant_combo_config(
    baseline: dict[str, Any],
    variant: dict[str, Any],
) -> dict[str, Any]:
    cfg = deepcopy(baseline)
    cfg["enabled"] = True
    cfg["structure_mode"] = str(variant["structure_mode"])
    cfg["min_net_credit_retention"] = float(variant["min_net_credit_retention"])
    if variant.get("max_call_cost_to_put_credit") is not None:
        cfg["max_call_cost_to_put_credit"] = float(variant["max_call_cost_to_put_credit"])
    call_cfg = dict(cfg.get("call") or {})
    call_cfg["min_delta"] = float(variant["min_abs_call_delta"])
    call_cfg["max_delta"] = float(variant["max_abs_call_delta"])
    cfg["call"] = call_cfg
    explicit = {
        str(field)
        for field in cfg.get("_explicit_fields") or []
        if str(field).strip()
    }
    explicit.update(
        {
            "enabled",
            "structure_mode",
            "min_net_credit_retention",
            "call",
        }
    )
    if variant.get("max_call_cost_to_put_credit") is not None:
        explicit.add("max_call_cost_to_put_credit")
    cfg["_explicit_fields"] = tuple(sorted(explicit))
    cfg["_explicit_call_fields"] = ("min_delta", "max_delta")
    return cfg


def _force_combo_enabled(source: dict[str, Any]) -> dict[str, Any]:
    cfg = deepcopy(source)
    cfg["enabled"] = True
    explicit = {
        str(field)
        for field in cfg.get("_explicit_fields") or []
        if str(field).strip()
    }
    explicit.add("enabled")
    cfg["_explicit_fields"] = tuple(sorted(explicit))
    return cfg


def _union_bundles(
    *,
    symbol: str,
    bundles: list[RequiredDataFetchPlanBundle],
    host: str,
    port: int,
) -> RequiredDataFetchPlanBundle:
    side_plans = _merge_same_side_plans(
        [
            side
            for bundle in bundles
            for side in bundle.side_plans
        ]
    )
    return RequiredDataFetchPlanBundle(
        symbol=symbol,
        spot_reference=next(
            (bundle.spot_reference for bundle in bundles if bundle.spot_reference is not None),
            None,
        ),
        side_plans=side_plans,
        merged_specs=_merge_side_plans(
            symbol=symbol,
            limit_expirations=0,
            host=host,
            port=port,
            side_plans=side_plans,
            include_realized_volatility=True,
        ),
        expiration_discovery_complete=all(
            bundle.expiration_discovery_complete
            for bundle in bundles
        ),
        expiration_discovery_error="; ".join(
            str(bundle.expiration_discovery_error)
            for bundle in bundles
            if bundle.expiration_discovery_error
        )
        or None,
    )


def _variant_completeness(
    *,
    symbols: list[str],
    variant_plans: dict[str, dict[str, RequiredDataFetchPlanBundle]],
    captured_payloads: dict[str, dict[str, object]],
    fetch_errors: dict[str, str],
    write: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    variant_ids = sorted(
        {
            variant_id
            for plans in variant_plans.values()
            for variant_id in plans
        }
    )
    for variant_id in variant_ids:
        missing: list[dict[str, Any]] = []
        for symbol in symbols:
            plan = variant_plans[symbol][variant_id]
            payload = captured_payloads.get(symbol) or {}
            rows = [row for row in payload.get("rows") or [] if isinstance(row, dict)]
            available = {
                (
                    str(row.get("option_type") or "").strip().lower(),
                    str(row.get("expiration") or "")[:10],
                )
                for row in rows
            }
            for side in plan.side_plans:
                for expiration in side.explicit_expirations:
                    if (side.option_type, str(expiration)[:10]) not in available:
                        missing.append(
                            {
                                "symbol": symbol,
                                "option_type": side.option_type,
                                "expiration": str(expiration)[:10],
                                "reason": (
                                    "capture_not_run"
                                    if not write
                                    else "fetch_error"
                                    if symbol in fetch_errors
                                    else "contract_unavailable"
                                ),
                            }
                        )
        out.append(
            {
                "variant_id": variant_id,
                "status": (
                    "not_captured"
                    if not write
                    else "complete"
                    if not missing
                    else "unavailable"
                ),
                "missing_expirations_or_contracts": missing,
            }
        )
    return out


def _side_expirations(bundle: RequiredDataFetchPlanBundle, option_type: str) -> list[str]:
    return sorted(
        {
            str(expiration)
            for side in bundle.side_plans
            if side.option_type == option_type
            for expiration in side.explicit_expirations
        }
    )


def _normalized_json(payload: Any) -> Any:
    return json.loads(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    )


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_path_from_base(
    value: str | Path | None,
    *,
    base: Path,
    default: Path,
) -> Path:
    path = Path(value).expanduser() if value is not None else default
    if not path.is_absolute():
        path = base / path
    return path.resolve()


__all__ = ["capture_combo_variants"]
