from __future__ import annotations

import sqlite3

import pytest

from src.application.config_validator import validate_config
from src.application.wheel.config import (
    build_wheel_policy_hash,
    detect_wheel_policy_drift,
    evaluate_wheel_activation_readiness,
    resolve_wheel_activation_descriptor,
    resolve_wheel_config,
)


def _v2_config() -> dict:
    return {
        "_resolved": {"market": "us"},
        "wheel": {
            "accounts": ["lx"],
            "call": {"min_dte": 31, "max_dte": 46, "min_abs_delta": 0.27},
            "put": {"min_dte": 14, "max_dte": 35},
            "activation_by_account": {
                "lx": {"generation": 2, "activated_at_ms": 1_700_000_000_000, "deactivated_at_ms": None}
            },
        },
    }


def _legacy_config() -> dict:
    return {
        "_resolved": {"market": "us"},
        "wheel": {"accounts": ["LX"], "min_dte": 35, "max_dte": 50, "min_delta": 0.90},
    }


def test_resolve_wheel_config_maps_legacy_only_to_call_and_fails_closed_without_descriptor() -> None:
    resolved = resolve_wheel_config(_legacy_config(), "lx")

    assert resolved["call"]["min_dte"] == 35
    assert resolved["call"]["max_dte"] == 50
    assert resolved["call"]["min_abs_delta"] == 0.25
    assert resolved["call"]["max_abs_delta"] == 0.35
    assert resolved["put"]["min_dte"] == 7
    assert resolved["put"]["max_dte"] == 60
    assert resolved["min_delta"] == 0.25
    assert resolved["activation_descriptor"] is None
    assert resolved["enabled_for_new_lifecycle"] is False


def test_nested_policy_overrides_defaults_and_builds_account_bound_descriptor() -> None:
    config = _v2_config()
    resolved = resolve_wheel_config(config, "LX")
    descriptor = resolve_wheel_activation_descriptor(config, market="us", account="lx")

    assert resolved["call"]["min_dte"] == 31
    assert resolved["call"]["min_abs_delta"] == 0.27
    assert resolved["call"]["max_abs_delta"] == 0.35
    assert resolved["put"]["min_dte"] == 14
    assert resolved["put"]["min_abs_delta"] == 0.25
    assert resolved["enabled_for_new_lifecycle"] is True
    assert resolved["account_configured"] is True
    assert descriptor == resolved["activation_descriptor"]
    assert descriptor == {
        "market": "us",
        "account": "lx",
        "generation": 2,
        "activated_at_ms": 1_700_000_000_000,
        "deactivated_at_ms": None,
        "policy_hash": build_wheel_policy_hash(config, market="us", account="lx"),
    }

    config["wheel"]["accounts"] = []
    removed = resolve_wheel_config(config, "lx")
    assert removed["account_configured"] is False
    assert removed["enabled_for_new_lifecycle"] is False
    assert evaluate_wheel_activation_readiness(
        descriptor, descriptor, account_configured=removed["account_configured"],
    )["reason_code"] == "account_not_configured"


def test_policy_hash_excludes_activation_descriptor_and_deprecated_min_delta() -> None:
    first = _v2_config()
    second = _v2_config()
    second["wheel"]["min_delta"] = 0.99
    second["wheel"]["activation_by_account"]["lx"]["generation"] = 3
    second["wheel"]["activation_by_account"]["lx"]["activated_at_ms"] += 1000

    assert build_wheel_policy_hash(first, market="us", account="lx") == build_wheel_policy_hash(
        second,
        market="us",
        account="lx",
    )
    assert build_wheel_policy_hash(first, market="us", account="lx") != build_wheel_policy_hash(
        first,
        market="hk",
        account="lx",
    )


def test_activation_readiness_requires_exact_open_descriptor_match() -> None:
    descriptor = resolve_wheel_activation_descriptor(_v2_config(), market="us", account="lx")
    assert descriptor is not None

    no_descriptor = evaluate_wheel_activation_readiness(None, None, account_configured=True)
    assert no_descriptor["reason_code"] == "missing_descriptor"
    no_window = evaluate_wheel_activation_readiness(descriptor, None, account_configured=True)
    assert no_window["reason_code"] == "missing_window"

    mismatch = {**descriptor, "policy_hash": "f" * 64}
    mismatch_result = evaluate_wheel_activation_readiness(descriptor, mismatch, account_configured=True)
    assert mismatch_result["monitoring_gate"] == "config_mismatch"
    assert mismatch_result["reason_code"] == "descriptor_mismatch"

    # `policy_drift` is stated on every refusal, so `False` reads as "not a policy rebind".
    assert no_descriptor["policy_drift"] is False
    assert no_window["policy_drift"] is False
    assert mismatch_result["policy_drift"] is True
    boundary_result = evaluate_wheel_activation_readiness(
        descriptor, {**mismatch, "generation": descriptor["generation"] + 1},
        account_configured=True,
    )
    assert boundary_result["reason_code"] == "descriptor_mismatch"
    assert boundary_result["policy_drift"] is False

    ready = evaluate_wheel_activation_readiness(descriptor, descriptor, account_configured=True)
    assert ready == {
        "ready": True,
        "enabled_for_new_lifecycle": True,
        "monitoring_gate": "enabled",
        "reason_code": None,
    }

    closed = {**descriptor, "deactivated_at_ms": descriptor["activated_at_ms"] + 1}
    closed_result = evaluate_wheel_activation_readiness(closed, closed, account_configured=True)
    assert closed_result["monitoring_gate"] == "disabled"
    assert closed_result["reason_code"] == "closed_window"


def test_wheel_validation_is_pure_and_rejects_invalid_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: pytest.fail("static Wheel validation touched SQLite"),
    )
    config = {"accounts": ["lx"], "symbols": [{"symbol": "NVDA"}], "wheel": _v2_config()["wheel"]}
    validate_config(config)

    config["wheel"]["activation_by_account"]["lx"]["deactivated_at_ms"] = 1
    with pytest.raises(SystemExit, match="deactivated_at_ms must be >"):
        validate_config(config)


def test_readiness_uses_only_durable_effective_policy_and_preserves_boundaries() -> None:
    descriptor = resolve_wheel_activation_descriptor(_v2_config(), market="us", account="lx")
    window = {**descriptor, "policy_hash": "a" * 64,
              "effective_policy_hash": descriptor["policy_hash"], "policy_binding_revision": 1}
    assert evaluate_wheel_activation_readiness(descriptor, window, account_configured=True)["ready"] is True
    assert window["policy_hash"] == "a" * 64
    for field, value in (("market", "hk"), ("account", "sy"), ("generation", 3),
                         ("activated_at_ms", descriptor["activated_at_ms"] + 1)):
        result = evaluate_wheel_activation_readiness(
            descriptor, {**window, field: value}, account_configured=True,
        )
        assert result["ready"] is False
        assert result["reason_code"] == "descriptor_mismatch"
    closed_at = descriptor["activated_at_ms"] + 1
    result = evaluate_wheel_activation_readiness({**descriptor, "deactivated_at_ms": closed_at},
                                                 {**window, "deactivated_at_ms": closed_at},
                                                 account_configured=True)
    assert result["reason_code"] == "closed_window"
    assert result["policy_drift"] is False
    # Config metadata cannot substitute for the config's actual policy hash.
    assert evaluate_wheel_activation_readiness(
        {**descriptor, "policy_hash": "b" * 64, "effective_policy_hash": descriptor["policy_hash"]}, window,
        account_configured=True,
    )["ready"] is False


@pytest.mark.parametrize("binding", [
    {"effective_policy_hash": "a" * 64},
    {"policy_binding_revision": 1},
    {"effective_policy_hash": None, "policy_binding_revision": 1},
    {"effective_policy_hash": "A" * 64, "policy_binding_revision": 1},
    {"effective_policy_hash": "a" * 63, "policy_binding_revision": 1},
    {"effective_policy_hash": "a" * 64, "policy_binding_revision": True},
    {"effective_policy_hash": "a" * 64, "policy_binding_revision": "1"},
    {"effective_policy_hash": "a" * 64, "policy_binding_revision": -1},
    {"effective_policy_hash": "a" * 64, "policy_binding_revision": 0},
])
def test_malformed_effective_binding_fails_closed(binding: dict) -> None:
    descriptor = resolve_wheel_activation_descriptor(_v2_config(), market="us", account="lx")
    result = evaluate_wheel_activation_readiness(
        descriptor, {**descriptor, **binding}, account_configured=True,
    )
    assert result["ready"] is False
    assert result["reason_code"] == "descriptor_mismatch"


def test_revision_zero_matches_legacy_readiness() -> None:
    descriptor = resolve_wheel_activation_descriptor(_v2_config(), market="us", account="lx")
    assert evaluate_wheel_activation_readiness(descriptor, {
        **descriptor, "effective_policy_hash": descriptor["policy_hash"], "policy_binding_revision": 0,
    }, account_configured=True) == evaluate_wheel_activation_readiness(
        descriptor, descriptor, account_configured=True,
    )


def test_detect_wheel_policy_drift_separates_policy_from_boundary_edits() -> None:
    prior = _v2_config()
    current = _v2_config()
    assert detect_wheel_policy_drift(prior, current, market="us") is None

    current["wheel"]["put"]["min_dte"] = 21
    drift = detect_wheel_policy_drift(prior, current, market="us")
    assert drift == {
        "accounts": [{"account": "lx", "policy_changed": True, "boundary_changed": False}],
        "policy_accounts": ["lx"],
        "boundary_accounts": [],
    }

    current["wheel"]["activation_by_account"]["lx"]["generation"] = 3
    drift = detect_wheel_policy_drift(prior, current, market="us")
    assert drift["policy_accounts"] == ["lx"]
    assert drift["boundary_accounts"] == ["lx"]

    # Accounts with no descriptor on either side are never compared: no window, no binding.
    fresh = _v2_config()
    fresh["wheel"]["accounts"] = ["lx", "sy"]
    assert detect_wheel_policy_drift(fresh, {**fresh, "symbols": ["NVDA"]}, market="us") is None

    # A boundary-only edit is reported as boundary drift, not policy drift.
    boundary_only = _v2_config()
    boundary_only["wheel"]["activation_by_account"]["lx"]["activated_at_ms"] += 1
    drift = detect_wheel_policy_drift(prior, boundary_only, market="us")
    assert drift["policy_accounts"] == []
    assert drift["boundary_accounts"] == ["lx"]


def test_detect_wheel_policy_drift_ignores_unusable_inputs() -> None:
    assert detect_wheel_policy_drift(None, _v2_config(), market="us") is None
    assert detect_wheel_policy_drift(_v2_config(), None, market="us") is None
    assert detect_wheel_policy_drift(_v2_config(), _v2_config(), market="eu") is None
