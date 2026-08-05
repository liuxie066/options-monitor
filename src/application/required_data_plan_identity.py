from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
import hashlib
import json
import math
from typing import Any

from domain.domain.fetch_source import normalize_fetch_source
from domain.domain.symbol_identity import resolve_symbol_identity


REQUIRED_DATA_EXPECTED_FETCH_CONTRACT_SCHEMA = (
    "required_data_expected_fetch_contract.v1"
)


def required_data_expiration_dtes(
    *,
    trading_date: date,
    expirations: list[str],
) -> dict[str, int]:
    """Resolve the canonical calendar-day DTE for explicit expirations."""

    if isinstance(trading_date, datetime) or not isinstance(trading_date, date):
        raise ValueError("required-data expiration DTE trading date is invalid")
    if not isinstance(expirations, list):
        raise ValueError("required-data expiration DTE values are invalid")
    normalized_expirations = _validated_expiration_list(
        expirations,
        field="required-data expiration DTE values",
    )
    resolved: dict[str, int] = {}
    for expiration in normalized_expirations:
        expiration_date = _validated_iso_date(
            expiration,
            field="required-data expiration DTE value",
        )
        dte = (expiration_date - trading_date).days
        if dte < 0:
            raise ValueError(
                "required-data expiration DTE precedes trading date"
            )
        resolved[expiration] = dte
    return resolved


def required_data_plan_id(symbols: list[Mapping[str, Any]]) -> str:
    """Hash the ordered canonical symbol-plan payload used by producer and seal."""

    canonical = json.dumps(
        [dict(item) for item in symbols],
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def required_data_request_sha256(request: Mapping[str, Any]) -> str:
    """Hash one exact planned request using the canonical plan convention."""

    return required_data_plan_id([request])


def build_required_data_fetch_binding(
    *,
    source: str,
    host: str,
    port: int,
) -> dict[str, Any]:
    """Build the canonical physical binding shared by plan and contract."""

    source_norm = _normalize_source(source)
    if not source_norm:
        raise ValueError("required-data expected fetch source is missing")
    port_value = _validated_port(
        port,
        error="required-data expected fetch port is invalid",
    )
    binding = {
        "source": source_norm,
        "host": str(host or "127.0.0.1").strip() or "127.0.0.1",
        "port": port_value,
    }
    return {
        **binding,
        "binding_id": _canonical_sha256(binding),
    }


def build_required_data_expected_fetch_contract(
    *,
    symbol: str,
    fetch_plan: Mapping[str, Any],
    source: str,
    host: str,
    port: int,
) -> dict[str, Any]:
    """Build the plan-owned fetch identity used by publication and seal."""

    symbol_norm = str(symbol or "").strip().upper()
    if not symbol_norm:
        raise ValueError("required-data expected fetch contract symbol is missing")
    binding = build_required_data_fetch_binding(
        source=source,
        host=host,
        port=port,
    )
    plan_payload = dict(fetch_plan or {})
    merged_requests = plan_payload.get("merged_requests")
    require_rv = plan_payload.get("require_realized_volatility")
    if not isinstance(require_rv, bool):
        raise ValueError(
            "required-data plan RV authority is missing or invalid"
        )
    contract = {
        "schema_version": REQUIRED_DATA_EXPECTED_FETCH_CONTRACT_SCHEMA,
        "symbol": symbol_norm,
        "fetch_plan": plan_payload,
        "fetch_binding": binding,
        "coverage_policy": {
            "schema_version": "required_data_coverage_policy.v2",
            "projection_outcome": str(
                plan_payload.get("projection_outcome") or "success_rows"
            ).strip(),
            "require_realized_volatility": require_rv,
            "coverage_evaluator": "required_data_frame_covers_fetch_plan.v2",
            "completion_unit": "request_option_type_expiration",
            "allow_proven_empty_scopes": True,
        },
    }
    contract["contract_sha256"] = _canonical_sha256(contract)
    return validate_required_data_expected_fetch_contract(
        contract,
        expected_symbol=symbol_norm,
    )


def validate_required_data_expected_fetch_contract(
    value: Mapping[str, Any],
    *,
    expected_symbol: str | None = None,
) -> dict[str, Any]:
    contract = dict(value or {})
    supplied_hash = str(contract.pop("contract_sha256", "") or "").strip()
    if contract.get("schema_version") != REQUIRED_DATA_EXPECTED_FETCH_CONTRACT_SCHEMA:
        raise ValueError("required-data expected fetch contract schema mismatch")
    symbol = str(contract.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("required-data expected fetch contract symbol is missing")
    if expected_symbol is not None and symbol != str(expected_symbol).strip().upper():
        raise ValueError("required-data expected fetch contract symbol mismatch")
    fetch_plan = contract.get("fetch_plan")
    binding = contract.get("fetch_binding")
    coverage = contract.get("coverage_policy")
    if not isinstance(fetch_plan, Mapping):
        raise ValueError("required-data expected fetch plan is invalid")
    if not isinstance(binding, Mapping):
        raise ValueError("required-data expected fetch binding is invalid")
    if not isinstance(coverage, Mapping):
        raise ValueError("required-data coverage policy is invalid")
    binding_payload = build_required_data_fetch_binding(
        source=str(binding.get("source") or ""),
        host=str(binding.get("host") or ""),
        port=binding.get("port"),  # type: ignore[arg-type]
    )
    if str(binding.get("binding_id") or "") != str(binding_payload["binding_id"]):
        raise ValueError("required-data expected fetch binding hash mismatch")
    plan_payload = dict(fetch_plan)
    plan_symbol = str(plan_payload.get("symbol") or "").strip().upper()
    if plan_symbol != symbol:
        raise ValueError("required-data expected fetch plan symbol mismatch")
    raw_projection_outcome = plan_payload.get("projection_outcome")
    if not isinstance(raw_projection_outcome, str):
        raise ValueError("required-data expected projection outcome is missing")
    projection_outcome = raw_projection_outcome.strip()
    if projection_outcome not in {"success_rows", "success_empty"}:
        raise ValueError("required-data expected projection outcome is invalid")
    (
        discovery_expirations,
        projected_expirations,
        trading_date,
    ) = _validate_expiration_discovery(
        fetch_plan=plan_payload,
        projection_outcome=projection_outcome,
        symbol=symbol,
        binding=binding_payload,
    )
    spot_reference = _validated_optional_positive_number(
        plan_payload.get("spot_reference"),
        field="required-data expected spot reference",
    )
    top_level_side_plans = plan_payload.get("side_plans")
    if not isinstance(top_level_side_plans, list) or any(
        not isinstance(item, Mapping) for item in top_level_side_plans
    ):
        raise ValueError("required-data expected top-level side plans are invalid")
    validated_top_side_plans = [
        _validate_side_plan_shape(
            side_plan=item,
            spot_reference=spot_reference,
            trading_date=trading_date,
        )
        for item in top_level_side_plans
    ]
    top_option_types = [item["option_type"] for item in validated_top_side_plans]
    if len(top_option_types) != len(set(top_option_types)):
        raise ValueError("required-data expected top-level side plans are duplicated")
    active_top_side_plans = [
        item
        for item in validated_top_side_plans
        if item["explicit_expirations"]
    ]
    merged_requests = plan_payload.get("merged_requests")
    if not isinstance(merged_requests, list):
        raise ValueError("required-data expected merged requests are invalid")
    request_items = list(merged_requests)
    if any(not isinstance(item, Mapping) for item in request_items):
        raise ValueError("required-data expected merged requests are invalid")
    if projection_outcome == "success_rows" and not request_items:
        raise ValueError("success-rows required-data plan lacks fetch requests")
    has_expiration_target = False
    validated_nested_side_plans: list[dict[str, Any]] = []
    for request in request_items:
        request_symbol = str(request.get("symbol") or "").strip().upper()
        if request_symbol != symbol:
            raise ValueError("required-data expected request symbol mismatch")
        if str(request.get("host") or "").strip() != binding_payload["host"]:
            raise ValueError("required-data expected request host mismatch")
        request_port = _validated_port(
            request.get("port"),
            error="required-data expected request port is invalid",
        )
        if request_port != binding_payload["port"]:
            raise ValueError("required-data expected request port mismatch")
        request_has_target, request_side_plans = _validate_fetch_request_shape(
            request=request,
            spot_reference=spot_reference,
            trading_date=trading_date,
        )
        has_expiration_target = request_has_target or has_expiration_target
        validated_nested_side_plans.extend(request_side_plans)
    if projection_outcome == "success_rows" and not has_expiration_target:
        raise ValueError(
            "success-rows required-data plan lacks expiration targets"
        )
    if not _canonical_values_equal(
        validated_nested_side_plans,
        active_top_side_plans,
    ):
        raise ValueError(
            "required-data nested and top-level side plans contradict"
        )
    plan_expirations = _unique_preserve_order(
        expiration
        for side_plan in validated_top_side_plans
        for expiration in side_plan["explicit_expirations"]
    )
    if projection_outcome == "success_empty" and (
        has_expiration_target or plan_expirations
    ):
        raise ValueError(
            "success-empty required-data plan contains fetch demand"
        )
    if plan_expirations != projected_expirations:
        raise ValueError(
            "required-data projected expirations contradict side plans"
        )
    if not set(projected_expirations).issubset(discovery_expirations):
        raise ValueError(
            "required-data projected expirations contradict discovery"
        )
    require_rv = plan_payload.get("require_realized_volatility")
    if not isinstance(require_rv, bool):
        raise ValueError(
            "required-data plan RV authority is missing or invalid"
        )
    if any(
        item.get("include_realized_volatility") is not require_rv
        for item in request_items
    ):
        raise ValueError(
            "required-data request RV flags contradict plan authority"
        )
    coverage_payload = dict(coverage)
    expected_coverage = {
        "schema_version": "required_data_coverage_policy.v2",
        "projection_outcome": projection_outcome,
        "require_realized_volatility": require_rv,
        "coverage_evaluator": "required_data_frame_covers_fetch_plan.v2",
        "completion_unit": "request_option_type_expiration",
        "allow_proven_empty_scopes": True,
    }
    if coverage_payload != expected_coverage:
        raise ValueError("required-data coverage policy contradicts fetch plan")
    normalized = {
        **contract,
        "symbol": symbol,
        "fetch_plan": plan_payload,
        "fetch_binding": {
            **binding_payload,
        },
        "coverage_policy": expected_coverage,
    }
    expected_hash = _canonical_sha256(normalized)
    if supplied_hash != expected_hash:
        raise ValueError("required-data expected fetch contract hash mismatch")
    normalized["contract_sha256"] = supplied_hash
    return normalized


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(value),
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_fetch_request_shape(
    *,
    request: Mapping[str, Any],
    spot_reference: float | None,
    trading_date: date,
) -> tuple[bool, list[dict[str, Any]]]:
    option_types = request.get("option_types")
    if (
        not isinstance(option_types, list)
        or not option_types
        or any(item not in {"put", "call"} for item in option_types)
        or len(option_types) != len(set(option_types))
    ):
        raise ValueError("required-data expected request option types are invalid")
    expirations = request.get("explicit_expirations")
    if not isinstance(expirations, list):
        raise ValueError("required-data expected request expirations are invalid")
    normalized_expirations = _validated_expiration_list(
        expirations,
        field="required-data expected request expirations",
    )
    if not normalized_expirations:
        raise ValueError(
            "required-data executable request lacks expiration targets"
        )
    request_trading_date = _validated_iso_date(
        request.get("trading_date"),
        field="required-data expected request trading date",
    )
    if request_trading_date != trading_date:
        raise ValueError(
            "required-data request trading date contradicts discovery"
        )
    nested_side_plans = request.get("side_plans")
    if not isinstance(nested_side_plans, list) or not nested_side_plans or any(
        not isinstance(item, Mapping) for item in nested_side_plans
    ):
        raise ValueError("required-data expected nested side plans are invalid")
    validated_side_plans = [
        _validate_side_plan_shape(
            side_plan=item,
            spot_reference=spot_reference,
            trading_date=trading_date,
        )
        for item in nested_side_plans
    ]
    nested_option_types = [item["option_type"] for item in validated_side_plans]
    if option_types != nested_option_types:
        raise ValueError(
            "required-data request option types contradict nested side plans"
        )
    if any(
        item["explicit_expirations"] != normalized_expirations
        for item in validated_side_plans
    ):
        if not normalized_expirations:
            raise ValueError(
                "success-rows required-data plan lacks expiration targets: "
                "request expirations contradict nested side plans"
            )
        raise ValueError(
            "required-data request expirations contradict nested side plans"
        )
    strike_windows = request.get("side_strike_windows")
    if (
        not isinstance(strike_windows, Mapping)
        or set(strike_windows) != set(option_types)
    ):
        raise ValueError(
            "required-data expected request strike windows are invalid"
        )
    for option_type in option_types:
        window = strike_windows.get(option_type)
        if (
            not isinstance(window, Mapping)
            or "min_strike" not in window
            or "max_strike" not in window
        ):
            raise ValueError(
                "required-data expected request strike windows are invalid"
            )
        expected_side_plan = next(
            item
            for item in validated_side_plans
            if item["option_type"] == option_type
        )
        expected_window = {
            "min_strike": expected_side_plan["strike_window"]["min_strike"],
            "max_strike": expected_side_plan["strike_window"]["max_strike"],
        }
        actual_window = {
            "min_strike": _validated_optional_positive_number(
                window.get("min_strike"),
                field="required-data expected request minimum strike",
            ),
            "max_strike": _validated_optional_positive_number(
                window.get("max_strike"),
                field="required-data expected request maximum strike",
            ),
        }
        _validate_ordered_optional_range(
            actual_window["min_strike"],
            actual_window["max_strike"],
            field="required-data expected request strike window",
        )
        if not _canonical_values_equal(actual_window, expected_window):
            raise ValueError(
                "required-data request strike windows contradict nested side plans"
            )
    min_dte = _validated_optional_nonnegative_int(
        request.get("min_dte"),
        field="required-data expected request minimum DTE",
    )
    max_dte = _validated_optional_nonnegative_int(
        request.get("max_dte"),
        field="required-data expected request maximum DTE",
    )
    _validate_ordered_optional_range(
        min_dte,
        max_dte,
        field="required-data expected request DTE range",
    )
    _validate_dte_range_covers_expirations(
        expiration_dtes=required_data_expiration_dtes(
            trading_date=trading_date,
            expirations=normalized_expirations,
        ),
        min_dte=min_dte,
        max_dte=max_dte,
        field="required-data expected request DTE range",
    )
    expected_min_dte = min(
        (
            item["min_dte"]
            for item in validated_side_plans
            if item["min_dte"] is not None
        ),
        default=None,
    )
    expected_max_dte = max(
        (
            item["max_dte"]
            for item in validated_side_plans
            if item["max_dte"] is not None
        ),
        default=None,
    )
    if min_dte != expected_min_dte or max_dte != expected_max_dte:
        raise ValueError(
            "required-data request DTE range contradicts nested side plans"
        )
    if not isinstance(request.get("include_realized_volatility"), bool):
        raise ValueError("required-data expected request RV flag is invalid")
    if "limit_expirations" in request:
        _validated_nonnegative_int(
            request.get("limit_expirations"),
            field="required-data expected request expiration limit",
        )
    return True, validated_side_plans


def _validate_expiration_discovery(
    *,
    fetch_plan: Mapping[str, Any],
    projection_outcome: str,
    symbol: str,
    binding: Mapping[str, Any],
) -> tuple[list[str], list[str], date]:
    if fetch_plan.get("expiration_discovery_complete") is not True:
        raise ValueError("required-data expiration discovery is incomplete")
    if fetch_plan.get("expiration_discovery_error") not in (None, ""):
        raise ValueError("required-data expiration discovery has an error")
    discovery = fetch_plan.get("expiration_discovery")
    if not isinstance(discovery, Mapping):
        raise ValueError("required-data expiration discovery evidence is missing")
    expected_outcome = (
        "success_empty" if projection_outcome == "success_empty" else "success_rows"
    )
    if str(discovery.get("outcome") or "").strip().lower() != expected_outcome:
        raise ValueError("required-data expiration discovery outcome mismatch")
    if discovery.get("error") not in (None, ""):
        raise ValueError("required-data expiration discovery error mismatch")
    expirations = discovery.get("expirations")
    if not isinstance(expirations, list):
        raise ValueError("required-data expiration discovery values are invalid")
    normalized_expirations = _validated_expiration_list(
        expirations,
        field="required-data expiration discovery values",
        require_sorted=True,
    )
    normalized_projected: list[str] = []
    if expected_outcome == "success_empty":
        if normalized_expirations:
            raise ValueError("success-empty expiration discovery contains values")
        if str(discovery.get("reason_code") or "").strip().lower() not in {
            "no_expirations",
            "no_contract_rows",
        }:
            raise ValueError("success-empty expiration discovery reason is invalid")
        projected = fetch_plan.get("projected_expirations")
        if not isinstance(projected, list) or projected:
            raise ValueError("success-empty projected expirations are invalid")
    else:
        if not normalized_expirations or discovery.get("reason_code") not in (None, ""):
            raise ValueError("success-rows expiration discovery is invalid")
        projected = fetch_plan.get("projected_expirations")
        if not isinstance(projected, list):
            raise ValueError("success-rows projected expirations are invalid")
        normalized_projected = _validated_expiration_list(
            projected,
            field="success-rows projected expirations",
        )
        if not normalized_projected or not set(normalized_projected).issubset(
            normalized_expirations
        ):
            raise ValueError("success-rows projected expirations are invalid")

    identity = discovery.get("request_identity")
    if not isinstance(identity, Mapping):
        raise ValueError("required-data expiration discovery identity is missing")
    identity_binding = {
        "source": _normalize_source(identity.get("source")),
        "host": str(identity.get("host") or "").strip(),
        "port": _validated_port(
            identity.get("port"),
            error="required-data expiration discovery identity is invalid",
        ),
    }
    expected_binding = {
        "source": str(binding.get("source") or ""),
        "host": str(binding.get("host") or ""),
        "port": binding.get("port"),
    }
    trading_date = _validated_iso_date(
        identity.get("trading_date"),
        field="required-data expiration discovery trading date",
    )
    symbol_identity = resolve_symbol_identity(symbol)
    if symbol_identity is None or symbol_identity.canonical != symbol:
        raise ValueError(
            "required-data expiration discovery symbol identity is invalid"
        )
    if (
        str(identity.get("symbol") or "").strip().upper() != symbol
        or identity_binding != expected_binding
        or str(identity.get("underlier") or "").strip()
        != symbol_identity.futu_code
    ):
        raise ValueError("required-data expiration discovery identity mismatch")
    try:
        observed = _parse_timestamp(discovery.get("observed_at_utc"))
        completed = _parse_timestamp(discovery.get("completed_at_utc"))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "required-data expiration discovery timestamps are invalid"
        ) from exc
    if completed < observed:
        raise ValueError(
            "required-data expiration discovery completion precedes observation"
        )
    return normalized_expirations, normalized_projected, trading_date


def _validate_side_plan_shape(
    *,
    side_plan: Mapping[str, Any],
    spot_reference: float | None,
    trading_date: date,
) -> dict[str, Any]:
    payload = dict(side_plan)
    option_type = payload.get("option_type")
    if option_type not in {"put", "call"}:
        raise ValueError("required-data expected side plan option type is invalid")
    min_dte = _validated_optional_nonnegative_int(
        payload.get("min_dte"),
        field="required-data expected side plan minimum DTE",
    )
    max_dte = _validated_optional_nonnegative_int(
        payload.get("max_dte"),
        field="required-data expected side plan maximum DTE",
    )
    _validate_ordered_optional_range(
        min_dte,
        max_dte,
        field="required-data expected side plan DTE range",
    )
    expirations = payload.get("explicit_expirations")
    if not isinstance(expirations, list):
        raise ValueError("required-data expected side plan expirations are invalid")
    normalized_expirations = _validated_expiration_list(
        expirations,
        field="required-data expected side plan expirations",
    )
    _validate_dte_range_covers_expirations(
        expiration_dtes=required_data_expiration_dtes(
            trading_date=trading_date,
            expirations=normalized_expirations,
        ),
        min_dte=min_dte,
        max_dte=max_dte,
        field="required-data expected side plan DTE range",
    )
    window = payload.get("strike_window")
    if not isinstance(window, Mapping):
        raise ValueError("required-data expected side plan strike window is invalid")
    required_window_fields = {
        "min_strike",
        "max_strike",
        "source",
        "buffer_applied",
        "buffer_pct",
        "base_min_strike",
        "base_max_strike",
    }
    if not required_window_fields.issubset(window):
        raise ValueError("required-data expected side plan strike window is invalid")
    min_strike = _validated_optional_positive_number(
        window.get("min_strike"),
        field="required-data expected side plan minimum strike",
    )
    max_strike = _validated_optional_positive_number(
        window.get("max_strike"),
        field="required-data expected side plan maximum strike",
    )
    base_min_strike = _validated_optional_positive_number(
        window.get("base_min_strike"),
        field="required-data expected side plan base minimum strike",
    )
    base_max_strike = _validated_optional_positive_number(
        window.get("base_max_strike"),
        field="required-data expected side plan base maximum strike",
    )
    _validate_ordered_optional_range(
        min_strike,
        max_strike,
        field="required-data expected side plan strike window",
    )
    _validate_ordered_optional_range(
        base_min_strike,
        base_max_strike,
        field="required-data expected side plan base strike window",
    )
    if (
        base_min_strike is not None
        and min_strike is not None
        and min_strike > base_min_strike
    ) or (
        base_max_strike is not None
        and max_strike is not None
        and max_strike < base_max_strike
    ):
        raise ValueError(
            "required-data side plan strike window does not cover base window"
        )
    effective_min_strike = (
        base_min_strike
        if base_min_strike is not None
        else min_strike
    )
    effective_max_strike = (
        base_max_strike
        if base_max_strike is not None
        else max_strike
    )
    if (
        effective_min_strike is not None
        and effective_max_strike is not None
        and effective_min_strike > effective_max_strike
    ):
        raise ValueError(
            "required-data side plan effective strike window is inverted"
        )
    source = str(window.get("source") or "").strip()
    if not source or not isinstance(window.get("buffer_applied"), bool):
        raise ValueError("required-data expected side plan strike window is invalid")
    buffer_pct = _validated_nonnegative_number(
        window.get("buffer_pct"),
        field="required-data expected side plan strike buffer",
    )
    side_spot_reference = _validated_optional_positive_number(
        payload.get("spot_reference"),
        field="required-data expected side plan spot reference",
    )
    if (
        side_spot_reference is not None
        and side_spot_reference != spot_reference
    ):
        raise ValueError(
            "required-data side plan spot reference contradicts fetch plan"
        )
    if not isinstance(payload.get("planning_reason"), str) or not str(
        payload.get("planning_reason")
    ).strip():
        raise ValueError("required-data expected side plan reason is invalid")
    source_fields = payload.get("source_fields")
    if not isinstance(source_fields, list) or any(
        not isinstance(item, str) or not item.strip() for item in source_fields
    ):
        raise ValueError("required-data expected side plan source fields are invalid")
    if "min_strike" not in payload or "max_strike" not in payload:
        raise ValueError("required-data expected side plan effective strikes are missing")
    side_min_strike = _validated_optional_positive_number(
        payload.get("min_strike"),
        field="required-data expected side plan effective minimum strike",
    )
    side_max_strike = _validated_optional_positive_number(
        payload.get("max_strike"),
        field="required-data expected side plan effective maximum strike",
    )
    if side_min_strike != min_strike or side_max_strike != max_strike:
        raise ValueError(
            "required-data side plan effective strikes contradict strike window"
        )
    if "expiration_count" not in payload:
        raise ValueError("required-data expected side plan expiration count is missing")
    expiration_count = _validated_nonnegative_int(
        payload.get("expiration_count"),
        field="required-data expected side plan expiration count",
    )
    if expiration_count != len(normalized_expirations):
        raise ValueError(
            "required-data side plan expiration count contradicts expirations"
        )
    exact_strikes = _validate_required_exact_strikes(
        payload.get("required_exact_strikes_by_expiration"),
        expirations=normalized_expirations,
        min_strike=min_strike,
        max_strike=max_strike,
    )
    normalized_window = {
        **dict(window),
        "min_strike": min_strike,
        "max_strike": max_strike,
        "source": source,
        "buffer_applied": window.get("buffer_applied"),
        "buffer_pct": buffer_pct,
        "base_min_strike": base_min_strike,
        "base_max_strike": base_max_strike,
    }
    return {
        **payload,
        "option_type": option_type,
        "min_dte": min_dte,
        "max_dte": max_dte,
        "explicit_expirations": normalized_expirations,
        "strike_window": normalized_window,
        "planning_reason": str(payload.get("planning_reason")).strip(),
        "source_fields": [item.strip() for item in source_fields],
        "spot_reference": side_spot_reference,
        "min_strike": side_min_strike,
        "max_strike": side_max_strike,
        "expiration_count": expiration_count,
        "required_exact_strikes_by_expiration": exact_strikes,
    }


def _validate_required_exact_strikes(
    value: Any,
    *,
    expirations: list[str],
    min_strike: float | None,
    max_strike: float | None,
) -> dict[str, list[float]]:
    if not isinstance(value, Mapping):
        raise ValueError(
            "required-data expected exact strike identity is missing or invalid"
        )
    raw_keys = list(value.keys())
    if any(not isinstance(key, str) for key in raw_keys):
        raise ValueError("required-data expected exact strike expirations are invalid")
    normalized_keys = [
        _validated_iso_date(
            key,
            field="required-data expected exact strike expiration",
        ).isoformat()
        for key in raw_keys
    ]
    if normalized_keys != sorted(normalized_keys) or not set(
        normalized_keys
    ).issubset(expirations):
        raise ValueError("required-data expected exact strike expirations are invalid")
    normalized: dict[str, list[float]] = {}
    for key, expiration in zip(raw_keys, normalized_keys, strict=True):
        strikes = value.get(key)
        if not isinstance(strikes, list) or not strikes:
            raise ValueError("required-data expected exact strikes are invalid")
        normalized_strikes = [
            _validated_number(
                strike,
                field="required-data expected exact strike",
            )
            for strike in strikes
        ]
        if (
            any(strike <= 0 for strike in normalized_strikes)
            or normalized_strikes != sorted(normalized_strikes)
            or len(normalized_strikes) != len(set(normalized_strikes))
            or any(
                (min_strike is not None and strike < min_strike)
                or (max_strike is not None and strike > max_strike)
                for strike in normalized_strikes
            )
        ):
            raise ValueError("required-data expected exact strikes are invalid")
        normalized[expiration] = normalized_strikes
    return normalized


def _validated_expiration_list(
    values: list[Any],
    *,
    field: str,
    require_sorted: bool = False,
) -> list[str]:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or value != value.strip():
            raise ValueError(f"{field} are invalid")
        parsed = _validated_iso_date(value, field=field)
        normalized.append(parsed.isoformat())
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} are invalid")
    if require_sorted and normalized != sorted(normalized):
        raise ValueError(f"{field} are invalid")
    return normalized


def _validated_iso_date(value: Any, *, field: str) -> date:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError(f"{field} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} is invalid")
    return parsed


def _validated_port(value: Any, *, error: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(error)
    if value <= 0 or value > 65535:
        raise ValueError(error)
    return value


def _validated_optional_positive_number(
    value: Any,
    *,
    field: str,
) -> float | None:
    if value is None:
        return None
    normalized = _validated_number(value, field=field)
    if normalized <= 0:
        raise ValueError(f"{field} is invalid")
    return normalized


def _validated_nonnegative_number(value: Any, *, field: str) -> float:
    normalized = _validated_number(value, field=field)
    if normalized < 0:
        raise ValueError(f"{field} is invalid")
    return normalized


def _validated_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} is invalid")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field} is invalid")
    return normalized


def _validated_optional_nonnegative_int(
    value: Any,
    *,
    field: str,
) -> int | None:
    if value is None:
        return None
    return _validated_nonnegative_int(value, field=field)


def _validated_nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} is invalid")
    return value


def _validate_ordered_optional_range(
    minimum: int | float | None,
    maximum: int | float | None,
    *,
    field: str,
) -> None:
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"{field} is inverted")


def _validate_dte_range_covers_expirations(
    *,
    expiration_dtes: Mapping[str, int],
    min_dte: int | None,
    max_dte: int | None,
    field: str,
) -> None:
    if any(
        (min_dte is not None and dte < min_dte)
        or (max_dte is not None and dte > max_dte)
        for dte in expiration_dtes.values()
    ):
        raise ValueError(f"{field} does not cover explicit expirations")


def _unique_preserve_order(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _canonical_values_equal(left: Any, right: Any) -> bool:
    return json.dumps(
        left,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) == json.dumps(
        right,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp timezone is missing")
    return parsed.astimezone(timezone.utc)


def _normalize_source(value: Any) -> str:
    if not str(value or "").strip():
        return ""
    return normalize_fetch_source(value)


__all__ = [
    "REQUIRED_DATA_EXPECTED_FETCH_CONTRACT_SCHEMA",
    "build_required_data_fetch_binding",
    "build_required_data_expected_fetch_contract",
    "required_data_expiration_dtes",
    "required_data_plan_id",
    "validate_required_data_expected_fetch_contract",
]
