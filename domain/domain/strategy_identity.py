"""Pure strategy provenance transport; no engine or policy-evaluation imports."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from domain.domain.symbol_identity import symbol_market


def strategy_policy_hash(parameters: dict[str, Any], ranking_profile: str) -> str:
    payload = json.dumps({"parameters": parameters, "ranking_profile": ranking_profile},
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


IDENTITY_FORMAT = "strategy_identity.v2"


def implementation_policy_hash(identity: dict[str, Any]) -> str:
    payload = {key: identity[key] for key in (
        "identity_format", "template", "template_version", "effective_parameters", "strike_inputs", "ranking_profile",
    )}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_strategy_identity(
    row: dict[str, Any], *, mode: str, account: str | None = None, market: str | None = None,
) -> dict[str, Any] | None:
    """Validate transported hash and scope before consuming its ranking profile."""
    if "strategy_core" not in row:
        return None
    identity = row["strategy_core"]
    try:
        json.dumps(identity, sort_keys=True, allow_nan=False)
        if not isinstance(identity["effective_parameters"], dict) or not isinstance(identity["core_version"], str):
            raise ValueError("invalid strategy policy identity")
        multiplier = identity["effective_parameters"]["min_strike_cost_multiplier"]
        if (isinstance(multiplier, bool) or not isinstance(multiplier, (int, float))
                or not math.isfinite(multiplier) or multiplier <= 0):
            raise ValueError("min_strike_cost_multiplier must be positive finite numeric policy")
        scope = identity["scope"]
        if scope["mode"] != mode or mode not in ("put", "call"):
            raise ValueError("strategy mode conflicts with candidate scope")
        if scope["account"] is not None and (
            not isinstance(scope["account"], str) or not scope["account"]
            or scope["account"] != scope["account"].strip().lower()
        ):
            raise ValueError("invalid strategy account scope")
        if scope["market"] not in (None, "us", "hk"):
            raise ValueError("invalid strategy market scope")
        profile = identity["ranking_profile"]
        if profile not in ("current_tie_break", "concentration_first") or (mode == "call" and profile != "current_tie_break"):
            raise ValueError("invalid strategy ranking profile")
        new_format = identity.get("identity_format")
        if new_format is not None and new_format != IDENTITY_FORMAT:
            raise ValueError("unsupported strategy identity format")
        expected_hash = (implementation_policy_hash(identity) if new_format else
                         strategy_policy_hash(identity["effective_parameters"], profile))
        if identity["effective_policy_hash"] != expected_hash:
            raise ValueError("strategy identity does not match effective policy")
        if (not identity["core_version"]
                or not isinstance(identity["template"], str) or not identity["template"]
                or not isinstance(identity["template_version"], str) or not identity["template_version"]
                or (not new_format and (identity["template_version"] != identity["core_version"]
                    or identity["template"] != f"{'csp' if mode == 'put' else 'cc'}_baseline"))
                or identity["policy_kind"] not in ("baseline", "custom")
                or (identity["policy_kind"] == "baseline"
                    and identity["template"] != f"{'csp' if mode == 'put' else 'cc'}_baseline")
                or (profile != "current_tie_break" and identity["policy_kind"] != "custom")
                or set(identity["strike_inputs"]) != {"min_strike", "max_strike"}):
            raise ValueError("invalid strategy template identity")
        runtime = identity.get("runtime")
        if runtime is not None and runtime != {
            "mode": "source", "core_version": identity["core_version"], "build_digest": None,
        }:
            raise ValueError("invalid source strategy runtime identity")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid strategy identity") from exc
    symbol_scope = (symbol_market(row.get("symbol")) or "").lower() or None
    if symbol_scope is not None and any(
        expected is not None and symbol_scope != str(expected).lower() for expected in (scope["market"], market)
    ):
        raise ValueError("candidate symbol market conflicts with strategy identity")
    for field, expected in (("account", account), ("market", market)):
        bound, actual = scope[field], row.get(field)
        if expected is not None and bound is not None and str(expected).lower() != bound:
            raise ValueError(f"strategy {field} conflicts with candidate scope")
        if actual is not None and bound is not None and str(actual).lower() != bound:
            raise ValueError(f"candidate {field} conflicts with strategy identity")
        if actual is not None and expected is not None and str(actual).lower() != str(expected).lower():
            raise ValueError(f"candidate {field} conflicts with bound scope")
    if row.get("option_type") is not None and str(row["option_type"]).lower() != mode:
        raise ValueError("candidate option_type conflicts with strategy identity")
    return identity


def candidate_ranking_profile(
    rows: list[dict[str, Any]], *, mode: str, explicit_profile: str | None = None,
) -> str:
    """One scope/rank per universe; symbol-specific policy and strike may differ."""
    identities = [validate_strategy_identity(row, mode=mode) for row in rows if isinstance(row, dict)]
    bound = [identity for identity in identities if identity is not None]
    if not bound:
        return explicit_profile if explicit_profile is not None else "current_tie_break"
    if len(bound) != len(identities):
        raise ValueError("mixed legacy and resolved strategy identities")
    scopes = {tuple(identity["scope"][key] for key in ("account", "market", "mode"))
              + (identity["ranking_profile"], identity.get("identity_format"), identity["template"], identity["template_version"]) for identity in bound}
    if len(scopes) != 1:
        raise ValueError("mixed strategy account/market/mode/ranking scope")
    profile = bound[0]["ranking_profile"]
    if explicit_profile is not None and explicit_profile != profile:
        raise ValueError("explicit ranking profile conflicts with strategy identity")
    return profile
