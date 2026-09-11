from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256


WHEEL_POLICY_SCHEMA = "wheel_policy.v2"
WHEEL_POLICY_FIELDS = frozenset(
    {
        "min_dte",
        "max_dte",
        "min_abs_delta",
        "max_abs_delta",
        "min_annualized_net_premium_return",
        "min_net_premium_cny",
        "min_open_interest",
        "min_volume",
        "max_spread_ratio",
        "min_iv_rv_ratio",
        "min_iv_minus_rv",
    }
)
WHEEL_LEGACY_POLICY_FIELDS = frozenset(
    {
        "min_dte",
        "max_dte",
        "min_annualized_net_premium_return",
        "min_net_premium_cny",
        "max_spread_ratio",
        "min_iv_rv_ratio",
        "min_iv_minus_rv",
    }
)
WHEEL_ACTIVATION_DESCRIPTOR_FIELDS = frozenset(
    {"generation", "activated_at_ms", "deactivated_at_ms"}
)

WHEEL_CALL_DEFAULTS: dict[str, Any] = {
    "min_dte": 30,
    "max_dte": 45,
    "min_abs_delta": 0.25,
    "max_abs_delta": 0.35,
    "min_annualized_net_premium_return": 0.10,
    "min_net_premium_cny": 50.0,
    "min_open_interest": 300.0,
    "min_volume": 10.0,
    "max_spread_ratio": 0.40,
    "min_iv_rv_ratio": 1.10,
    "min_iv_minus_rv": 0.05,
}

WHEEL_PUT_DEFAULTS: dict[str, Any] = {
    "min_dte": 7,
    "max_dte": 60,
    "min_abs_delta": 0.25,
    "max_abs_delta": 0.35,
    "min_annualized_net_premium_return": 0.10,
    "min_net_premium_cny": 50.0,
    "min_open_interest": 300.0,
    "min_volume": 10.0,
    "max_spread_ratio": 0.40,
    "min_iv_rv_ratio": 1.10,
    "min_iv_minus_rv": 0.05,
}

WHEEL_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "accounts": [],
    **{
        key: value
        for key, value in WHEEL_CALL_DEFAULTS.items()
        if key in WHEEL_LEGACY_POLICY_FIELDS
    },
    "min_delta": 0.30,
    "call": deepcopy(WHEEL_CALL_DEFAULTS),
    "put": deepcopy(WHEEL_PUT_DEFAULTS),
    "activation_by_account": {},
}


def _normalized_account(value: Any) -> str:
    account = str(value or "").strip().lower()
    if not account:
        raise ValueError("Wheel config requires account")
    return account


def _normalized_market(value: Any) -> str:
    market = str(value or "").strip().lower()
    if market not in {"us", "hk"}:
        raise ValueError("Wheel config requires market us or hk")
    return market


def _resolved_market(config: Mapping[str, Any], explicit_market: str | None) -> str | None:
    if explicit_market is not None and str(explicit_market).strip():
        return _normalized_market(explicit_market)
    for key in ("_resolved", "_generated"):
        metadata = config.get(key)
        if isinstance(metadata, Mapping) and str(metadata.get("market") or "").strip():
            return _normalized_market(metadata.get("market"))
    return None


def _wheel_raw(config: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = config.get("wheel")
    if raw is None and any(
        key in config
        for key in {"call", "put", "activation_by_account", *WHEEL_LEGACY_POLICY_FIELDS}
    ):
        raw = config
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("wheel must be an object")
    return raw


def resolve_wheel_policy(raw: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Resolve nested Wheel v2 policy without consulting runtime state."""

    source = raw or {}
    if not isinstance(source, Mapping):
        raise ValueError("wheel must be an object")
    legacy_call = {
        key: source[key]
        for key in WHEEL_LEGACY_POLICY_FIELDS
        if key in source
    }
    raw_call = source.get("call")
    raw_put = source.get("put")
    if raw_call is not None and not isinstance(raw_call, Mapping):
        raise ValueError("wheel.call must be an object")
    if raw_put is not None and not isinstance(raw_put, Mapping):
        raise ValueError("wheel.put must be an object")
    return {
        "call": {
            **WHEEL_CALL_DEFAULTS,
            **legacy_call,
            **dict(raw_call or {}),
        },
        "put": {
            **WHEEL_PUT_DEFAULTS,
            **dict(raw_put or {}),
        },
    }


def normalize_wheel_activation_descriptor(raw: Mapping[str, Any]) -> dict[str, int | None]:
    """Return one strict, JSON-stable static activation descriptor."""

    if not isinstance(raw, Mapping):
        raise ValueError("Wheel activation descriptor must be an object")
    unknown = sorted(set(raw) - WHEEL_ACTIVATION_DESCRIPTOR_FIELDS)
    missing = sorted(WHEEL_ACTIVATION_DESCRIPTOR_FIELDS - set(raw))
    if unknown:
        raise ValueError(f"Wheel activation descriptor contains unsupported keys: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"Wheel activation descriptor is missing keys: {', '.join(missing)}")
    generation = raw.get("generation")
    activated_at_ms = raw.get("activated_at_ms")
    deactivated_at_ms = raw.get("deactivated_at_ms")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise ValueError("Wheel activation generation must be a positive integer")
    if isinstance(activated_at_ms, bool) or not isinstance(activated_at_ms, int) or activated_at_ms <= 0:
        raise ValueError("Wheel activated_at_ms must be a positive integer")
    if deactivated_at_ms is not None and (
        isinstance(deactivated_at_ms, bool)
        or not isinstance(deactivated_at_ms, int)
        or deactivated_at_ms <= activated_at_ms
    ):
        raise ValueError("Wheel deactivated_at_ms must be null or greater than activated_at_ms")
    return {
        "generation": generation,
        "activated_at_ms": activated_at_ms,
        "deactivated_at_ms": deactivated_at_ms,
    }


def normalize_wheel_activation_by_account(raw: Any) -> dict[str, dict[str, int | None]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("wheel.activation_by_account must be an object")
    out: dict[str, dict[str, int | None]] = {}
    for raw_account, raw_descriptor in raw.items():
        account = _normalized_account(raw_account)
        if account in out:
            raise ValueError("wheel.activation_by_account contains duplicate normalized accounts")
        out[account] = normalize_wheel_activation_descriptor(raw_descriptor)
    return out


def normalize_wheel_accounts(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError("wheel.accounts must be a list")
    return [_normalized_account(value) for value in raw]


def materialize_wheel_config(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Materialize the static v2 config while preserving legacy Call fields."""

    source = raw or {}
    if not isinstance(source, Mapping):
        raise ValueError("wheel must be an object")
    policy = resolve_wheel_policy(source)
    accounts = normalize_wheel_accounts(source.get("accounts", []))
    return {
        **dict(source),
        "enabled": source.get("enabled", False),
        "accounts": accounts,
        "call": policy["call"],
        "put": policy["put"],
        "activation_by_account": normalize_wheel_activation_by_account(
            source.get("activation_by_account")
        ),
    }


def build_wheel_policy_payload(
    config: Mapping[str, Any],
    *,
    market: str,
    account: str,
) -> dict[str, Any]:
    policy = resolve_wheel_policy(_wheel_raw(config))
    return {
        "schema_version": WHEEL_POLICY_SCHEMA,
        "market": _normalized_market(market),
        "account": _normalized_account(account),
        "call": policy["call"],
        "put": policy["put"],
    }


def build_wheel_policy_hash(
    config: Mapping[str, Any],
    *,
    market: str,
    account: str,
) -> str:
    return canonical_sha256(
        build_wheel_policy_payload(config, market=market, account=account)
    )


wheel_policy_sha256 = build_wheel_policy_hash


def resolve_wheel_activation_descriptor(
    config: Mapping[str, Any],
    *,
    market: str,
    account: str,
) -> dict[str, Any] | None:
    account_value = _normalized_account(account)
    raw = _wheel_raw(config)
    by_account = normalize_wheel_activation_by_account(raw.get("activation_by_account"))
    descriptor = by_account.get(account_value)
    if descriptor is None:
        return None
    policy_hash = build_wheel_policy_hash(
        config,
        market=market,
        account=account_value,
    )
    return {
        "market": _normalized_market(market),
        "account": account_value,
        **descriptor,
        "policy_hash": policy_hash,
    }


def resolve_wheel_config(
    config: Mapping[str, Any],
    account: str,
    *,
    market: str | None = None,
) -> dict[str, Any]:
    """Return one account's pure Wheel policy and static activation expectation."""

    account_value = _normalized_account(account)
    raw = _wheel_raw(config)
    static = materialize_wheel_config(raw)
    market_value = _resolved_market(config, market)
    descriptor = (
        resolve_wheel_activation_descriptor(
            config,
            market=market_value,
            account=account_value,
        )
        if market_value is not None
        else None
    )
    call_policy = dict(static["call"])
    statically_open = bool(
        descriptor is not None
        and descriptor.get("deactivated_at_ms") is None
        and account_value in static["accounts"]
    )
    return {
        **static,
        **call_policy,
        "min_delta": call_policy["min_abs_delta"],
        "account": account_value,
        "market": market_value,
        "activation_descriptor": descriptor,
        "policy_hash": descriptor.get("policy_hash") if descriptor else None,
        "policy_sha256": descriptor.get("policy_hash") if descriptor else None,
        "enabled_for_new_lifecycle": statically_open,
    }


def _window_identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    policy_sha256 = str(
        raw.get("policy_sha256") or raw.get("policy_hash") or ""
    ).strip().lower()
    if len(policy_sha256) != 64 or any(value not in "0123456789abcdef" for value in policy_sha256):
        raise ValueError("Wheel policy hash must be 64 lowercase hexadecimal characters")
    return {
        "market": _normalized_market(raw.get("market")),
        "account": _normalized_account(raw.get("account")),
        **normalize_wheel_activation_descriptor(
            {
                key: raw.get(key)
                for key in WHEEL_ACTIVATION_DESCRIPTOR_FIELDS
            }
        ),
        "policy_sha256": policy_sha256,
    }


def evaluate_wheel_activation_readiness(
    descriptor: Mapping[str, Any] | None,
    durable_window: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Compare static config with the durable window without performing I/O."""

    if descriptor is None:
        return {
            "ready": False,
            "enabled_for_new_lifecycle": False,
            "monitoring_gate": "disabled",
            "reason_code": "missing_descriptor",
        }
    if durable_window is None:
        return {
            "ready": False,
            "enabled_for_new_lifecycle": False,
            "monitoring_gate": "disabled",
            "reason_code": "missing_window",
        }
    try:
        expected = _window_identity(descriptor)
        actual = _window_identity(durable_window)
    except (TypeError, ValueError):
        expected = actual = None
    if expected is None or actual is None:
        return {
            "ready": False,
            "enabled_for_new_lifecycle": False,
            "monitoring_gate": "config_mismatch",
            "reason_code": "descriptor_mismatch",
        }
    identity_fields = (
        "market",
        "account",
        "generation",
        "activated_at_ms",
        "deactivated_at_ms",
    )
    boundary_matches = all(expected[key] == actual[key] for key in identity_fields)
    policy_drift = expected["policy_sha256"] != actual["policy_sha256"]
    if not boundary_matches:
        return {
            "ready": False,
            "enabled_for_new_lifecycle": False,
            "monitoring_gate": "config_mismatch",
            "reason_code": "descriptor_mismatch",
            "policy_drift": policy_drift,
        }
    if expected["deactivated_at_ms"] is not None:
        return {
            "ready": False,
            "enabled_for_new_lifecycle": False,
            "monitoring_gate": "disabled",
            "reason_code": "closed_window",
            "policy_drift": policy_drift,
        }
    if policy_drift:
        return {
            "ready": False,
            "enabled_for_new_lifecycle": False,
            "monitoring_gate": "config_mismatch",
            "reason_code": "descriptor_mismatch",
            "policy_drift": True,
        }
    return {
        "ready": True,
        "enabled_for_new_lifecycle": True,
        "monitoring_gate": "enabled",
        "reason_code": None,
    }


__all__ = [
    "WHEEL_ACTIVATION_DESCRIPTOR_FIELDS",
    "WHEEL_CALL_DEFAULTS",
    "WHEEL_DEFAULTS",
    "WHEEL_LEGACY_POLICY_FIELDS",
    "WHEEL_POLICY_FIELDS",
    "WHEEL_POLICY_SCHEMA",
    "WHEEL_PUT_DEFAULTS",
    "build_wheel_policy_hash",
    "build_wheel_policy_payload",
    "evaluate_wheel_activation_readiness",
    "materialize_wheel_config",
    "normalize_wheel_activation_by_account",
    "normalize_wheel_activation_descriptor",
    "resolve_wheel_activation_descriptor",
    "resolve_wheel_config",
    "resolve_wheel_policy",
    "wheel_policy_sha256",
]
