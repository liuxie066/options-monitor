from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.position_advice_authority import (
    AUTHORITY_MODES,
    AuthorityResolution,
    build_authority_policy,
    normalize_account_label,
    normalize_portfolio_source,
    resolve_authority,
    scope_for,
    validate_authority_policy,
    validate_first_use_uniqueness,
)
from domain.domain.position_advice_promotion import evaluate_promotion_gate
from src.infrastructure.io_utils import atomic_write_json
from src.infrastructure.position_advice_manifest_lock import (
    portfolio_scope_state_dir,
    position_advice_manifest_locks,
    position_advice_state_root,
)


AUTHORITY_CHANGE_RECEIPT_SCHEMA = "position_advice_authority_change_intent.v1"
AUTHORITY_CHANGE_PLAN_SCHEMA = "position_advice_authority_change_plan.v1"
IDENTITY_BINDING_SCHEMA = "position_advice_first_use_identity_binding.v1"
NOTIFICATION_AUTHORITY_RECEIPT_SCHEMA = (
    "position_advice_notification_authority_receipt.v1"
)
NOTIFICATION_AUTHORITY_RESOLUTION_SCHEMA = (
    "position_advice_notification_authority_resolution.v1"
)


class PositionAdviceAuthorityError(RuntimeError):
    """Raised when the shared authority control plane cannot change safely."""


def authority_policy_path(base: Path, portfolio_scope_id: str) -> Path:
    return portfolio_scope_state_dir(base, portfolio_scope_id) / "authority_policy.v1.json"


def authority_change_dir(base: Path, portfolio_scope_id: str) -> Path:
    return portfolio_scope_state_dir(base, portfolio_scope_id) / "authority_changes"


def authority_identity_binding_dir(base: Path, portfolio_scope_id: str) -> Path:
    return portfolio_scope_state_dir(base, portfolio_scope_id) / "identity_bindings"


def authority_promotion_evidence_dir(base: Path, portfolio_scope_id: str) -> Path:
    return portfolio_scope_state_dir(base, portfolio_scope_id) / "promotion_evidence"


def authority_promotion_gate_dir(base: Path, portfolio_scope_id: str) -> Path:
    return portfolio_scope_state_dir(base, portfolio_scope_id) / "promotion_gates"


def build_identity_binding_evidence(
    *,
    normalized_account: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    authoring_config_hash: str,
    market_bindings: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    account = normalize_account_label(normalized_account)
    source = normalize_portfolio_source(normalized_portfolio_source)
    identity_hash = _sha256(
        portfolio_account_identity_hash,
        "portfolio_account_identity_hash",
    )
    bindings = sorted(
        [_normalize_market_binding(item) for item in market_bindings],
        key=lambda item: item["market"],
    )
    if not bindings or len({item["market"] for item in bindings}) != len(bindings):
        raise ValueError("identity binding markets are missing or duplicated")
    payload = {
        "schema_version": IDENTITY_BINDING_SCHEMA,
        "normalized_account": account,
        "normalized_portfolio_source": source,
        "portfolio_account_identity_hash": identity_hash,
        "authoring_config_hash": _sha256(
            authoring_config_hash,
            "authoring_config_hash",
        ),
        "enabled_markets": [item["market"] for item in bindings],
        "market_bindings": bindings,
        "completed": True,
    }
    return {**payload, "identity_binding_hash": canonical_sha256(payload)}


def validate_identity_binding_evidence(
    evidence: Mapping[str, Any] | None,
    *,
    normalized_account: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
) -> tuple[str, ...]:
    payload = dict(evidence or {})
    reasons: list[str] = []
    evidence_hash = payload.pop("identity_binding_hash", None)
    if evidence_hash != canonical_sha256(payload):
        reasons.append("identity_binding_hash_mismatch")
    if payload.get("schema_version") != IDENTITY_BINDING_SCHEMA:
        reasons.append("identity_binding_schema_invalid")
    if payload.get("completed") is not True:
        reasons.append("identity_binding_incomplete")
    try:
        account = normalize_account_label(normalized_account)
        source = normalize_portfolio_source(normalized_portfolio_source)
        identity_hash = _sha256(
            portfolio_account_identity_hash,
            "portfolio_account_identity_hash",
        )
    except ValueError:
        return ("identity_binding_caller_invalid",)
    if payload.get("normalized_account") != account:
        reasons.append("identity_binding_account_mismatch")
    if payload.get("normalized_portfolio_source") != source:
        reasons.append("identity_binding_source_mismatch")
    if payload.get("portfolio_account_identity_hash") != identity_hash:
        reasons.append("identity_binding_identity_mismatch")
    try:
        _sha256(payload.get("authoring_config_hash"), "authoring_config_hash")
    except ValueError:
        reasons.append("identity_binding_authoring_config_missing")

    raw_bindings = payload.get("market_bindings")
    normalized_bindings: list[dict[str, Any]] = []
    if not isinstance(raw_bindings, list):
        reasons.append("identity_binding_markets_missing")
    else:
        for item in raw_bindings:
            try:
                normalized_bindings.append(_normalize_market_binding(item))
            except (TypeError, ValueError):
                reasons.append("identity_binding_market_invalid")
    markets = [item["market"] for item in normalized_bindings]
    if not markets or len(markets) != len(set(markets)):
        reasons.append("identity_binding_markets_missing_or_duplicated")
    if payload.get("enabled_markets") != sorted(markets):
        reasons.append("identity_binding_enabled_markets_mismatch")
    for item in normalized_bindings:
        if item["normalized_account"] != account:
            reasons.append(f"identity_binding_account_mismatch:{item['market']}")
        if item["normalized_portfolio_source"] != source:
            reasons.append(f"identity_binding_source_mismatch:{item['market']}")
        if item["portfolio_account_identity_hash"] != identity_hash:
            reasons.append(f"identity_binding_identity_mismatch:{item['market']}")
        if item["source_receipt_fresh"] is not True:
            reasons.append(f"identity_binding_source_stale:{item['market']}")
    return tuple(sorted(set(reasons)))


def read_authority_resolution(
    *,
    base: Path,
    normalized_account: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    timeout_seconds: float = 5.0,
) -> AuthorityResolution:
    account = normalize_account_label(normalized_account)
    scope_id = scope_for(account)
    with position_advice_manifest_locks(
        base=base,
        portfolio_scope_id=scope_id,
        global_mode="shared",
        scope_mode="shared",
        timeout_seconds=timeout_seconds,
    ):
        return _read_authority_resolution_locked(
            base=base,
            normalized_account=account,
            normalized_portfolio_source=normalized_portfolio_source,
            portfolio_account_identity_hash=portfolio_account_identity_hash,
        )


def read_authority_resolution_under_lock(
    *,
    base: Path,
    normalized_account: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
) -> AuthorityResolution:
    """Read authority when the caller already holds global then scope lock."""

    return _read_authority_resolution_locked(
        base=base,
        normalized_account=normalized_account,
        normalized_portfolio_source=normalized_portfolio_source,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
    )


def plan_authority_change(
    *,
    base: Path,
    normalized_account: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    target_mode: str,
    expected_policy_hash: str,
    actor: str,
    requested_at: datetime | str,
    identity_binding_evidence: Mapping[str, Any] | None = None,
    promotion_evidence: Mapping[str, Any] | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    account = normalize_account_label(normalized_account)
    scope_id = scope_for(account)
    with position_advice_manifest_locks(
        base=base,
        portfolio_scope_id=scope_id,
        global_mode="shared",
        scope_mode="shared",
        timeout_seconds=timeout_seconds,
    ):
        return _plan_authority_change_locked(
            base=base,
            normalized_account=account,
            normalized_portfolio_source=normalized_portfolio_source,
            portfolio_account_identity_hash=portfolio_account_identity_hash,
            target_mode=target_mode,
            expected_policy_hash=expected_policy_hash,
            actor=actor,
            requested_at=requested_at,
            identity_binding_evidence=identity_binding_evidence,
            promotion_evidence=promotion_evidence,
        )


def apply_authority_change(
    *,
    base: Path,
    normalized_account: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    target_mode: str,
    expected_policy_hash: str,
    actor: str,
    requested_at: datetime | str,
    confirm: bool,
    identity_binding_evidence: Mapping[str, Any] | None = None,
    promotion_evidence: Mapping[str, Any] | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    if confirm is not True:
        raise PositionAdviceAuthorityError("authority apply requires explicit confirm")
    account = normalize_account_label(normalized_account)
    source = normalize_portfolio_source(normalized_portfolio_source)
    identity_hash = _sha256(
        portfolio_account_identity_hash,
        "portfolio_account_identity_hash",
    )
    scope_id = scope_for(account)
    first_use = expected_policy_hash == "absent"
    with position_advice_manifest_locks(
        base=base,
        portfolio_scope_id=scope_id,
        global_mode="exclusive" if first_use else "shared",
        scope_mode="exclusive",
        timeout_seconds=timeout_seconds,
    ):
        plan = _plan_authority_change_locked(
            base=base,
            normalized_account=account,
            normalized_portfolio_source=source,
            portfolio_account_identity_hash=identity_hash,
            target_mode=target_mode,
            expected_policy_hash=expected_policy_hash,
            actor=actor,
            requested_at=requested_at,
            identity_binding_evidence=identity_binding_evidence,
            promotion_evidence=promotion_evidence,
        )
        if plan["status"] != "ready":
            raise PositionAdviceAuthorityError(
                "authority change blocked: " + ",".join(plan["reason_codes"])
            )
        if plan["would_change"] is not True:
            return {**plan, "status": "unchanged", "applied": False}

        identity_binding_path: Path | None = None
        if first_use:
            binding_payload = dict(identity_binding_evidence or {})
            binding_hash = str(binding_payload.get("identity_binding_hash") or "")
            identity_binding_path = (
                authority_identity_binding_dir(base, scope_id)
                / f"{binding_hash}.json"
            )
            _write_json_once_or_verify(identity_binding_path, binding_payload)

        promotion_hash = plan.get("promotion_evidence_hash")
        if promotion_hash:
            evidence_path = (
                authority_promotion_evidence_dir(base, scope_id)
                / f"{promotion_hash}.json"
            )
            _write_json_once_or_verify(evidence_path, dict(promotion_evidence or {}))
            gate_payload = dict(plan.get("promotion_gate") or {})
            gate_artifact_hash = str(
                gate_payload.get("artifact_hash") or ""
            )
            if (
                len(gate_artifact_hash) != 64
                or canonical_sha256(
                    {
                        key: value
                        for key, value in gate_payload.items()
                        if key != "artifact_hash"
                    }
                )
                != gate_artifact_hash
            ):
                raise PositionAdviceAuthorityError(
                    "promotion gate artifact binding mismatch"
                )
            gate_path = (
                authority_promotion_gate_dir(base, scope_id)
                / f"{gate_artifact_hash}.json"
            )
            _write_json_once_or_verify(gate_path, gate_payload)

        receipt = dict(plan["change_receipt"])
        receipt_hash = canonical_sha256(receipt)
        receipt_path = authority_change_dir(base, scope_id) / f"{receipt_hash}.json"
        _write_json_once_or_verify(receipt_path, receipt)
        policy = build_authority_policy(
            normalized_account=account,
            normalized_portfolio_source=source,
            portfolio_account_identity_hash_value=identity_hash,
            mode=plan["target_mode"],
            generation=plan["next_generation"],
            updated_at=plan["requested_at"],
            change_receipt_hash=receipt_hash,
            promotion_evidence_hash=promotion_hash,
            covered_strategy_families=plan["covered_strategy_families"],
        )
        if canonical_sha256(_authority_state_payload(policy)) != receipt["after_state_hash"]:
            raise PositionAdviceAuthorityError("authority after-state binding mismatch")
        atomic_write_json(authority_policy_path(base, scope_id), policy, sort_keys=True)
        verified = _read_authority_resolution_locked(
            base=base,
            normalized_account=account,
            normalized_portfolio_source=source,
            portfolio_account_identity_hash=identity_hash,
        )
        if (
            verified.resolution_status != "resolved"
            or verified.policy_hash != policy["policy_hash"]
        ):
            raise PositionAdviceAuthorityError("authority policy readback failed")
        return {
            **plan,
            "status": "applied",
            "applied": True,
            "policy": policy,
            "policy_path": str(authority_policy_path(base, scope_id)),
            "change_receipt_path": str(receipt_path),
            "identity_binding_path": (
                str(identity_binding_path) if identity_binding_path else None
            ),
        }


def _read_authority_resolution_locked(
    *,
    base: Path,
    normalized_account: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
) -> AuthorityResolution:
    account = normalize_account_label(normalized_account)
    scope_id = scope_for(account)
    path = authority_policy_path(base, scope_id)
    if not path.exists():
        history_status = _classify_authority_history(
            base=base,
            portfolio_scope_id=scope_id,
            normalized_account=account,
        )
        return resolve_authority(
            normalized_account_label=account,
            normalized_portfolio_source=normalized_portfolio_source,
            portfolio_account_identity_hash_value=portfolio_account_identity_hash,
            policy=None,
            historical_authority_state_exists=history_status == "conflict",
        )
    if path.is_symlink():
        return resolve_authority(
            normalized_account_label=account,
            normalized_portfolio_source=normalized_portfolio_source,
            portfolio_account_identity_hash_value=portfolio_account_identity_hash,
            policy=None,
            historical_authority_state_exists=True,
            policy_read_error=True,
        )
    try:
        policy = _read_json_object(path)
        receipt_hash = str(policy.get("change_receipt_hash") or "")
        receipt_path = authority_change_dir(base, scope_id) / f"{receipt_hash}.json"
        receipt = _read_json_object(receipt_path)
        if canonical_sha256(receipt) != receipt_hash:
            raise PositionAdviceAuthorityError("authority change receipt hash mismatch")
        _validate_change_receipt_binding(receipt, policy, base=base)
    except (OSError, ValueError, PositionAdviceAuthorityError):
        return resolve_authority(
            normalized_account_label=account,
            normalized_portfolio_source=normalized_portfolio_source,
            portfolio_account_identity_hash_value=portfolio_account_identity_hash,
            policy=None,
            historical_authority_state_exists=True,
            policy_read_error=True,
        )
    return resolve_authority(
        normalized_account_label=account,
        normalized_portfolio_source=normalized_portfolio_source,
        portfolio_account_identity_hash_value=portfolio_account_identity_hash,
        policy=policy,
        historical_authority_state_exists=True,
    )


def _plan_authority_change_locked(
    *,
    base: Path,
    normalized_account: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    target_mode: str,
    expected_policy_hash: str,
    actor: str,
    requested_at: datetime | str,
    identity_binding_evidence: Mapping[str, Any] | None,
    promotion_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    account = normalize_account_label(normalized_account)
    source = normalize_portfolio_source(normalized_portfolio_source)
    identity_hash = _sha256(
        portfolio_account_identity_hash,
        "portfolio_account_identity_hash",
    )
    mode = str(target_mode or "").strip()
    if mode not in AUTHORITY_MODES:
        raise ValueError(f"unsupported authority mode: {target_mode}")
    expected_hash = str(expected_policy_hash or "").strip()
    if expected_hash != "absent":
        expected_hash = _sha256(expected_hash, "expected_policy_hash")
    actor_value = str(actor or "").strip()
    if not actor_value:
        raise ValueError("actor is required")
    requested_at_value = _timestamp(requested_at)
    scope_id = scope_for(account)
    policy_path = authority_policy_path(base, scope_id)
    reasons: list[str] = []
    current_policy: dict[str, Any] | None = None
    if policy_path.exists():
        try:
            current_policy = _read_json_object(policy_path)
            policy_reasons = validate_authority_policy(
                current_policy,
                expected_scope_id=scope_id,
            )
            if policy_reasons:
                reasons.extend(policy_reasons)
            resolution = _read_authority_resolution_locked(
                base=base,
                normalized_account=account,
                normalized_portfolio_source=source,
                portfolio_account_identity_hash=identity_hash,
            )
            if resolution.resolution_status != "resolved":
                reasons.extend(resolution.reason_codes)
        except (OSError, ValueError, PositionAdviceAuthorityError):
            reasons.append("authority_policy_unreadable")
    else:
        history_status = _classify_authority_history(
            base=base,
            portfolio_scope_id=scope_id,
            normalized_account=account,
        )
        if history_status == "conflict":
            reasons.append("authority_policy_missing_with_history")
        elif history_status == "implicit_v1_notifications" and mode != "v1":
            reasons.append("authority_implicit_v1_history_requires_v1_bootstrap")

    first_use = current_policy is None
    if first_use:
        if expected_hash != "absent":
            reasons.append("authority_expected_hash_mismatch")
        binding_reasons = validate_identity_binding_evidence(
            identity_binding_evidence,
            normalized_account=account,
            normalized_portfolio_source=source,
            portfolio_account_identity_hash=identity_hash,
        )
        reasons.extend(binding_reasons)
        existing_policies, scan_reasons = _scan_existing_policies(
            base=base,
            exclude_scope_id=scope_id,
        )
        reasons.extend(scan_reasons)
        reasons.extend(
            validate_first_use_uniqueness(
                target_scope_id=scope_id,
                target_identity_hash=identity_hash,
                existing_policies=existing_policies,
            )
        )
        current_hash: str | None = None
        current_mode: str | None = None
        current_generation = 0
    else:
        current_hash = str(current_policy.get("policy_hash") or "")
        current_mode = str(current_policy.get("mode") or "")
        current_generation = int(current_policy.get("generation") or 0)
        if expected_hash == "absent" or expected_hash != current_hash:
            reasons.append("authority_expected_hash_mismatch")
        if current_policy.get("normalized_account") != account:
            reasons.append("authority_account_identity_immutable")
        if current_policy.get("normalized_portfolio_source") != source:
            reasons.append("authority_source_identity_immutable")
        if current_policy.get("portfolio_account_identity_hash") != identity_hash:
            reasons.append("authority_portfolio_identity_immutable")

    promotion_hash: str | None = None
    promotion_gate: dict[str, Any] | None = None
    covered_families: list[str] = []
    outstanding_notification_receipt_ids = (
        _unresolved_notification_receipt_ids(base, scope_id)
    )
    if mode == "v2":
        if first_use or current_mode != "v2_shadow":
            reasons.append("authority_v2_requires_current_v2_shadow")
        if not isinstance(promotion_evidence, Mapping):
            reasons.append("authority_v2_promotion_evidence_missing")
        else:
            evidence_payload = dict(promotion_evidence)
            evidence_bindings = {
                "normalized_account": account,
                "portfolio_scope_id": scope_id,
                "normalized_portfolio_source": source,
                "portfolio_account_identity_hash": identity_hash,
                "authority_mode": "v2_shadow",
                "authority_generation": current_generation,
                "authority_policy_hash": current_hash,
            }
            for field, expected in evidence_bindings.items():
                if evidence_payload.get(field) != expected:
                    reasons.append(f"promotion_evidence_binding_mismatch:{field}")
            gate = evaluate_promotion_gate(evidence_payload)
            if gate["status"] != "pass":
                reasons.extend(f"promotion:{item}" for item in gate["reason_codes"])
            else:
                promotion_hash = canonical_sha256(evidence_payload)
                gate_payload = {
                    **gate,
                    "promotion_evidence_hash": promotion_hash,
                }
                promotion_gate = {
                    **gate_payload,
                    "artifact_hash": canonical_sha256(gate_payload),
                }
                if not _published_promotion_artifacts_match(
                    base=base,
                    portfolio_scope_id=scope_id,
                    promotion_evidence=evidence_payload,
                    promotion_evidence_hash=promotion_hash,
                    promotion_gate=promotion_gate,
                ):
                    reasons.append(
                        "promotion_evidence_not_published_or_conflicted"
                    )
                covered_families = sorted(
                    {
                        str(item)
                        for item in evidence_payload.get(
                            "covered_strategy_families",
                            [],
                        )
                    }
                )
        if outstanding_notification_receipt_ids:
            reasons.append("notification_authority_unknown_unresolved")
    elif mode == "v2_shadow" and outstanding_notification_receipt_ids:
        reasons.append("notification_authority_unknown_unresolved")

    would_change = not first_use and current_mode == mode
    would_change = not would_change
    if first_use:
        would_change = True
    next_generation = current_generation + 1 if would_change else current_generation
    identity_binding_hash = (
        str(dict(identity_binding_evidence or {}).get("identity_binding_hash") or "")
        or None
    )
    after_state = {
        "normalized_account": account,
        "portfolio_scope_id": scope_id,
        "normalized_portfolio_source": source,
        "portfolio_account_identity_hash": identity_hash,
        "mode": mode,
        "generation": next_generation,
        "promotion_evidence_hash": promotion_hash,
        "covered_strategy_families": covered_families,
    }
    change_receipt = {
        "schema_version": AUTHORITY_CHANGE_RECEIPT_SCHEMA,
        "portfolio_scope_id": scope_id,
        "normalized_account": account,
        "normalized_portfolio_source": source,
        "portfolio_account_identity_hash": identity_hash,
        "before_policy_hash": current_hash,
        "expected_policy_hash": expected_hash,
        "target_mode": mode,
        "next_generation": next_generation,
        "after_state_hash": canonical_sha256(after_state),
        "actor": actor_value,
        "requested_at": requested_at_value,
        "identity_binding_hash": identity_binding_hash,
        "promotion_evidence_hash": promotion_hash,
        "covered_strategy_families": covered_families,
        "outstanding_notification_receipt_ids": (
            outstanding_notification_receipt_ids
        ),
    }
    plan_payload = {
        "schema_version": AUTHORITY_CHANGE_PLAN_SCHEMA,
        "status": "ready" if not reasons else "blocked",
        "reason_codes": sorted(set(reasons)),
        "portfolio_scope_id": scope_id,
        "current_policy_hash": current_hash,
        "current_mode": current_mode,
        "target_mode": mode,
        "next_generation": next_generation,
        "requested_at": requested_at_value,
        "would_change": would_change and not reasons,
        "promotion_evidence_hash": promotion_hash,
        "promotion_gate": promotion_gate,
        "covered_strategy_families": covered_families,
        "outstanding_notification_receipt_ids": (
            outstanding_notification_receipt_ids
        ),
        "change_receipt": change_receipt,
    }
    return {**plan_payload, "plan_hash": canonical_sha256(plan_payload)}


def _authority_state_payload(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "normalized_account": policy.get("normalized_account"),
        "portfolio_scope_id": policy.get("portfolio_scope_id"),
        "normalized_portfolio_source": policy.get("normalized_portfolio_source"),
        "portfolio_account_identity_hash": policy.get(
            "portfolio_account_identity_hash"
        ),
        "mode": policy.get("mode"),
        "generation": policy.get("generation"),
        "promotion_evidence_hash": policy.get("promotion_evidence_hash"),
        "covered_strategy_families": policy.get("covered_strategy_families"),
    }


def _validate_change_receipt_binding(
    receipt: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    base: Path,
) -> None:
    if receipt.get("schema_version") != AUTHORITY_CHANGE_RECEIPT_SCHEMA:
        raise PositionAdviceAuthorityError("authority change receipt schema invalid")
    if receipt.get("portfolio_scope_id") != policy.get("portfolio_scope_id"):
        raise PositionAdviceAuthorityError("authority change receipt scope mismatch")
    if receipt.get("normalized_account") != policy.get("normalized_account"):
        raise PositionAdviceAuthorityError("authority change receipt account mismatch")
    if receipt.get("normalized_portfolio_source") != policy.get(
        "normalized_portfolio_source"
    ):
        raise PositionAdviceAuthorityError("authority change receipt source mismatch")
    if receipt.get("portfolio_account_identity_hash") != policy.get(
        "portfolio_account_identity_hash"
    ):
        raise PositionAdviceAuthorityError("authority change receipt identity mismatch")
    if receipt.get("target_mode") != policy.get("mode"):
        raise PositionAdviceAuthorityError("authority change receipt mode mismatch")
    if receipt.get("next_generation") != policy.get("generation"):
        raise PositionAdviceAuthorityError("authority change receipt generation mismatch")
    if receipt.get("promotion_evidence_hash") != policy.get("promotion_evidence_hash"):
        raise PositionAdviceAuthorityError("authority change receipt evidence mismatch")
    if receipt.get("covered_strategy_families") != policy.get(
        "covered_strategy_families"
    ):
        raise PositionAdviceAuthorityError("authority change receipt families mismatch")
    if receipt.get("after_state_hash") != canonical_sha256(
        _authority_state_payload(policy)
    ):
        raise PositionAdviceAuthorityError("authority change receipt state mismatch")
    outstanding = receipt.get("outstanding_notification_receipt_ids")
    if (
        not isinstance(outstanding, list)
        or outstanding
        != sorted(
            {
                str(item).strip()
                for item in outstanding
                if str(item).strip()
            }
        )
    ):
        raise PositionAdviceAuthorityError(
            "authority change receipt notification audit invalid"
        )
    binding_hash = str(receipt.get("identity_binding_hash") or "")
    if receipt.get("before_policy_hash") is None and len(binding_hash) != 64:
        raise PositionAdviceAuthorityError(
            "first-use identity binding receipt is missing"
        )
    if binding_hash:
        binding = _read_json_object(
            authority_identity_binding_dir(
                base,
                str(policy.get("portfolio_scope_id") or ""),
            )
            / f"{binding_hash}.json"
        )
        if canonical_sha256(
            {
                key: value
                for key, value in binding.items()
                if key != "identity_binding_hash"
            }
        ) != binding_hash:
            raise PositionAdviceAuthorityError(
                "first-use identity binding hash mismatch"
            )
        binding_reasons = validate_identity_binding_evidence(
            binding,
            normalized_account=str(policy.get("normalized_account") or ""),
            normalized_portfolio_source=str(
                policy.get("normalized_portfolio_source") or ""
            ),
            portfolio_account_identity_hash=str(
                policy.get("portfolio_account_identity_hash") or ""
            ),
        )
        if binding_reasons:
            raise PositionAdviceAuthorityError(
                "first-use identity binding invalid: "
                + ",".join(binding_reasons)
            )


def _scan_existing_policies(
    *,
    base: Path,
    exclude_scope_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    root = position_advice_state_root(base)
    if not root.exists():
        return [], []
    policies: list[dict[str, Any]] = []
    reasons: list[str] = []
    for path in sorted(root.glob("*/authority_policy.v1.json")):
        if path.parent.name == exclude_scope_id:
            continue
        try:
            policy = _read_json_object(path)
            if validate_authority_policy(policy, expected_scope_id=path.parent.name):
                raise PositionAdviceAuthorityError("malformed authority policy")
            receipt_hash = str(policy.get("change_receipt_hash") or "")
            receipt = _read_json_object(
                path.parent / "authority_changes" / f"{receipt_hash}.json"
            )
            if canonical_sha256(receipt) != receipt_hash:
                raise PositionAdviceAuthorityError("malformed authority receipt")
            _validate_change_receipt_binding(receipt, policy, base=base)
            policies.append(policy)
        except (OSError, ValueError, PositionAdviceAuthorityError):
            reasons.append("existing_authority_policy_malformed")
    return policies, reasons


def _published_promotion_artifacts_match(
    *,
    base: Path,
    portfolio_scope_id: str,
    promotion_evidence: Mapping[str, Any],
    promotion_evidence_hash: str,
    promotion_gate: Mapping[str, Any],
) -> bool:
    gate_hash = str(promotion_gate.get("artifact_hash") or "")
    if len(gate_hash) != 64:
        return False
    try:
        published_evidence = _read_json_object(
            authority_promotion_evidence_dir(base, portfolio_scope_id)
            / f"{promotion_evidence_hash}.json"
        )
        published_gate = _read_json_object(
            authority_promotion_gate_dir(base, portfolio_scope_id)
            / f"{gate_hash}.json"
        )
    except (OSError, ValueError, PositionAdviceAuthorityError):
        return False
    return (
        published_evidence == dict(promotion_evidence)
        and published_gate == dict(promotion_gate)
    )


def _classify_authority_history(
    *,
    base: Path,
    portfolio_scope_id: str,
    normalized_account: str,
) -> str:
    scope_dir = portfolio_scope_state_dir(base, portfolio_scope_id)
    if not scope_dir.exists():
        return "empty"
    if scope_dir.is_symlink() or not scope_dir.is_dir():
        return "conflict"
    ignored = {".current.lock"}
    try:
        entries = [path for path in scope_dir.iterdir() if path.name not in ignored]
    except OSError:
        return "conflict"
    if not entries:
        return "empty"
    if len(entries) != 1 or entries[0].name != "notification_authority":
        return "conflict"
    if _implicit_v1_notification_history_is_valid(
        state_dir=entries[0],
        portfolio_scope_id=portfolio_scope_id,
        normalized_account=normalized_account,
    ):
        return "implicit_v1_notifications"
    return "conflict"


def _implicit_v1_notification_history_is_valid(
    *,
    state_dir: Path,
    portfolio_scope_id: str,
    normalized_account: str,
) -> bool:
    if state_dir.is_symlink() or not state_dir.is_dir():
        return False
    allowed_statuses = {
        "accepted",
        "failed",
        "inflight",
        "resolutions",
        "unknown",
    }
    receipts: dict[tuple[str, str, int], dict[str, Any]] = {}
    resolutions: list[tuple[Path, dict[str, Any]]] = []
    receipt_count = 0
    try:
        for entry in state_dir.iterdir():
            if entry.name == ".send.lock":
                if entry.is_symlink() or not entry.is_file():
                    return False
                continue
            if (
                entry.name not in allowed_statuses
                or entry.is_symlink()
                or not entry.is_dir()
            ):
                return False
            for path in entry.iterdir():
                if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                    return False
                payload = _read_json_object(path)
                if entry.name == "resolutions":
                    resolutions.append((path, payload))
                    continue
                validated = _validate_implicit_v1_notification_receipt(
                    path=path,
                    status=entry.name,
                    payload=payload,
                    portfolio_scope_id=portfolio_scope_id,
                    normalized_account=normalized_account,
                )
                if validated is None:
                    return False
                receipt_id, attempt = validated
                key = (entry.name, receipt_id, attempt)
                if key in receipts:
                    return False
                receipts[key] = payload
                receipt_count += 1
    except (OSError, ValueError, PositionAdviceAuthorityError):
        return False

    if receipt_count == 0:
        return False
    terminal_statuses = ("accepted", "failed", "unknown")
    terminal_ids = {
        status: {
            receipt_id
            for receipt_status, receipt_id, _attempt in receipts
            if receipt_status == status
        }
        for status in terminal_statuses
    }
    if terminal_ids["accepted"] & terminal_ids["unknown"]:
        return False
    terminal_attempts: set[tuple[str, int]] = set()
    for (status, receipt_id, attempt), terminal in receipts.items():
        if status not in terminal_statuses:
            continue
        terminal_attempt = (receipt_id, attempt)
        if terminal_attempt in terminal_attempts:
            return False
        terminal_attempts.add(terminal_attempt)
        intent = receipts.get(("inflight", receipt_id, attempt))
        if intent is None or not _notification_receipt_pair_matches(intent, terminal):
            return False
    unknown_receipts = {
        receipt_id: payload
        for (status, receipt_id, _attempt), payload in receipts.items()
        if status == "unknown"
    }
    return all(
        _implicit_v1_notification_resolution_is_valid(
            path=path,
            payload=payload,
            unknown_receipts=unknown_receipts,
        )
        for path, payload in resolutions
    )


def _validate_implicit_v1_notification_receipt(
    *,
    path: Path,
    status: str,
    payload: Mapping[str, Any],
    portfolio_scope_id: str,
    normalized_account: str,
) -> tuple[str, int] | None:
    receipt_id = str(payload.get("receipt_id") or "").strip()
    attempt_number = payload.get("attempt_number")
    if not isinstance(attempt_number, int) or isinstance(attempt_number, bool):
        return None
    attempt = attempt_number
    authority_generation = payload.get("authority_generation")
    if (
        not isinstance(authority_generation, int)
        or isinstance(authority_generation, bool)
        or authority_generation != 0
    ):
        return None
    expected_name = (
        f"{receipt_id}.{attempt}.json"
        if status in {"failed", "inflight"}
        else f"{receipt_id}.json"
    )
    required = {
        "schema_version": NOTIFICATION_AUTHORITY_RECEIPT_SCHEMA,
        "status": status,
        "portfolio_scope_id": portfolio_scope_id,
        "normalized_account": normalized_account,
        "selected_advice_contract": "v1",
        "resolved_mode": "v1",
        "authority_policy_hash": None,
    }
    if (
        not _is_sha256(receipt_id)
        or attempt < 1
        or path.name != expected_name
        or any(payload.get(field) != value for field, value in required.items())
        or not _is_sha256(payload.get("token_hash"))
        or not str(payload.get("account_run_id") or "").strip()
        or not str(payload.get("channel") or "").strip()
        or not _is_timestamp(payload.get("recorded_at"))
    ):
        return None
    if status == "inflight":
        if payload.get("receipt_hash") is not None:
            return None
    elif not _is_timestamp(payload.get("completed_at")) or payload.get(
        "receipt_hash"
    ) != canonical_sha256(
        {key: value for key, value in payload.items() if key != "receipt_hash"}
    ):
        return None
    return receipt_id, attempt


def _notification_receipt_pair_matches(
    intent: Mapping[str, Any],
    terminal: Mapping[str, Any],
) -> bool:
    fields = (
        "schema_version",
        "receipt_id",
        "attempt_number",
        "portfolio_scope_id",
        "normalized_account",
        "selected_advice_contract",
        "resolved_mode",
        "authority_generation",
        "authority_policy_hash",
        "account_run_id",
        "channel",
        "token_hash",
        "recorded_at",
    )
    return all(intent.get(field) == terminal.get(field) for field in fields)


def _implicit_v1_notification_resolution_is_valid(
    *,
    path: Path,
    payload: Mapping[str, Any],
    unknown_receipts: Mapping[str, Mapping[str, Any]],
) -> bool:
    receipt_id = str(payload.get("receipt_id") or "").strip()
    unknown = unknown_receipts.get(receipt_id)
    evidence = payload.get("evidence")
    if (
        unknown is None
        or path.name != f"{receipt_id}.json"
        or payload.get("schema_version") != NOTIFICATION_AUTHORITY_RESOLUTION_SCHEMA
        or payload.get("unknown_receipt_hash") != unknown.get("receipt_hash")
        or payload.get("resolution") not in {"delivered", "failed"}
        or not isinstance(evidence, Mapping)
        or not evidence
        or payload.get("evidence_hash") != canonical_sha256(dict(evidence))
        or not str(payload.get("actor") or "").strip()
        or not _is_timestamp(payload.get("resolved_at"))
    ):
        return False
    return payload.get("resolution_hash") == canonical_sha256(
        {key: value for key, value in payload.items() if key != "resolution_hash"}
    )


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _is_timestamp(value: Any) -> bool:
    try:
        _timestamp(str(value or ""))
    except (TypeError, ValueError):
        return False
    return True


def _unresolved_notification_receipt_ids(
    base: Path,
    portfolio_scope_id: str,
) -> list[str]:
    state_dir = (
        portfolio_scope_state_dir(base, portfolio_scope_id)
        / "notification_authority"
    )
    unknown_dir = state_dir / "unknown"
    resolution_dir = state_dir / "resolutions"
    inflight_dir = state_dir / "inflight"
    outstanding: set[str] = set()
    for path in inflight_dir.glob("*.json"):
        try:
            intent = _read_json_object(path)
        except (OSError, ValueError, PositionAdviceAuthorityError):
            outstanding.add(f"inflight:{path.name}")
            continue
        receipt_id = str(intent.get("receipt_id") or "").strip()
        if len(receipt_id) != 64:
            outstanding.add(f"inflight:{path.name}")
            continue
        final_exists = any(
            (state_dir / status / f"{receipt_id}.json").is_file()
            for status in ("accepted", "unknown")
        )
        attempt_number = intent.get("attempt_number")
        if isinstance(attempt_number, bool):
            outstanding.add(receipt_id)
            continue
        try:
            attempt = int(attempt_number)
        except (TypeError, ValueError, OverflowError):
            outstanding.add(receipt_id)
            continue
        failed_exists = (
            state_dir
            / "failed"
            / f"{receipt_id}.{attempt}.json"
        ).is_file()
        if not final_exists and not failed_exists:
            outstanding.add(receipt_id)
    for path in unknown_dir.glob("*.json"):
        receipt_id = path.stem
        if not (resolution_dir / f"{receipt_id}.json").exists():
            outstanding.add(receipt_id)
    return sorted(outstanding)


def _normalize_market_binding(raw: Mapping[str, Any] | Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("market identity binding must be an object")
    item = dict(raw)
    market = str(item.get("market") or "").strip().upper()
    if market not in {"US", "HK"}:
        raise ValueError("market identity binding market is invalid")
    return {
        "market": market,
        "generated_config_hash": _sha256(
            item.get("generated_config_hash"),
            "generated_config_hash",
        ),
        "source_receipt_hash": _sha256(
            item.get("source_receipt_hash"),
            "source_receipt_hash",
        ),
        "normalized_account": normalize_account_label(
            item.get("normalized_account")
        ),
        "normalized_portfolio_source": normalize_portfolio_source(
            item.get("normalized_portfolio_source")
        ),
        "portfolio_account_identity_hash": _sha256(
            item.get("portfolio_account_identity_hash"),
            "portfolio_account_identity_hash",
        ),
        "source_receipt_fresh": item.get("source_receipt_fresh") is True,
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file() or path.is_symlink():
        raise PositionAdviceAuthorityError(f"authority state is unavailable: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PositionAdviceAuthorityError(f"authority state is not an object: {path}")
    return payload


def _write_json_once_or_verify(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        if path.is_symlink() or _read_json_object(path) != dict(payload):
            raise PositionAdviceAuthorityError(f"authority receipt conflict: {path}")
        return
    atomic_write_json(path, dict(payload), sort_keys=True)


def _sha256(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{field} must be SHA-256")
    return text


def _timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("requested_at must be timezone aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "AUTHORITY_CHANGE_PLAN_SCHEMA",
    "AUTHORITY_CHANGE_RECEIPT_SCHEMA",
    "IDENTITY_BINDING_SCHEMA",
    "PositionAdviceAuthorityError",
    "apply_authority_change",
    "authority_change_dir",
    "authority_identity_binding_dir",
    "authority_policy_path",
    "authority_promotion_gate_dir",
    "build_identity_binding_evidence",
    "plan_authority_change",
    "read_authority_resolution",
    "read_authority_resolution_under_lock",
    "validate_identity_binding_evidence",
]
