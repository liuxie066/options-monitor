from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

from domain.domain.decision_state_fingerprint import canonical_sha256


AUTHORITY_POLICY_SCHEMA = "position_advice_authority_policy.v1"
SCOPE_DERIVATION_VERSION = "options-monitor.position-advice.scope.v2"
AUTHORITY_MODES = frozenset({"v1", "v2_shadow", "v2"})
PROMOTABLE_STRATEGY_FAMILIES = frozenset({"short_put", "covered_call"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class AuthorityResolution:
    portfolio_scope_id: str
    mode: str | None
    generation: int | None
    policy_hash: str | None
    resolution_status: str
    reason_codes: tuple[str, ...] = ()
    covered_strategy_families: tuple[str, ...] = ()

    @property
    def notifications_allowed(self) -> bool:
        return self.resolution_status in {"resolved", "first_use_default_v1"}


def normalize_account_label(value: Any) -> str:
    label = str(value or "").strip().lower()
    if not label:
        raise ValueError("normalized account label is required")
    return label


def normalize_portfolio_source(value: Any) -> str:
    source = str(value or "").strip().lower()
    if not source:
        raise ValueError("normalized portfolio source is required")
    return source


def scope_for(normalized_account_label: Any) -> str:
    label = normalize_account_label(normalized_account_label)
    return canonical_sha256(
        {
            "schema": SCOPE_DERIVATION_VERSION,
            "normalized_account_label": label,
        }
    )


def portfolio_account_identity_hash(
    *,
    normalized_portfolio_source: Any,
    broker_account_identifiers: Iterable[Any],
) -> str:
    source = normalize_portfolio_source(normalized_portfolio_source)
    identifiers = [str(item or "").strip().lower() for item in broker_account_identifiers]
    if not identifiers or any(not item for item in identifiers):
        raise ValueError("broker account identity is unavailable")
    unique = sorted(set(identifiers))
    return canonical_sha256(
        {
            "normalized_portfolio_source": source,
            "normalized_broker_account_identifiers": unique,
        }
    )


def capacity_pool_authority_id(
    *,
    normalized_portfolio_source: Any,
    broker_account_identifiers: Iterable[Any],
    cash_scope_semantics_version: str,
) -> str:
    source = normalize_portfolio_source(normalized_portfolio_source)
    identifiers = sorted({str(item or "").strip().lower() for item in broker_account_identifiers})
    semantics = str(cash_scope_semantics_version or "").strip()
    if not identifiers or any(not item for item in identifiers) or not semantics:
        raise ValueError("capacity authority identity is unavailable")
    return canonical_sha256(
        {
            "normalized_portfolio_source": source,
            "normalized_broker_account_identifiers": identifiers,
            "cash_scope_semantics_version": semantics,
        }
    )


def _policy_hash_payload(policy: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in policy.items() if key != "policy_hash"}


def _is_sha256(value: Any) -> bool:
    return bool(_SHA256_RE.fullmatch(str(value or "").strip()))


def _positive_generation(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("authority generation must be a positive integer")
    try:
        generation = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("authority generation must be a positive integer") from exc
    if generation < 1 or generation != value:
        raise ValueError("authority generation must be a positive integer")
    return generation


def build_authority_policy(
    *,
    normalized_account: Any,
    normalized_portfolio_source: Any,
    portfolio_account_identity_hash_value: str,
    mode: str,
    generation: int,
    updated_at: str,
    change_receipt_hash: str,
    promotion_evidence_hash: str | None = None,
    covered_strategy_families: Iterable[str] = (),
) -> dict[str, Any]:
    account = normalize_account_label(normalized_account)
    source = normalize_portfolio_source(normalized_portfolio_source)
    mode_value = str(mode or "").strip()
    identity_hash = str(portfolio_account_identity_hash_value or "").strip()
    receipt_hash = str(change_receipt_hash or "").strip()
    evidence_hash = str(promotion_evidence_hash or "").strip() or None
    updated = str(updated_at or "").strip()
    generation_value = _positive_generation(generation)
    strategy_families = sorted(
        {str(item or "").strip() for item in covered_strategy_families if str(item or "").strip()}
    )
    if mode_value not in AUTHORITY_MODES:
        raise ValueError(f"unsupported authority mode: {mode}")
    if not _is_sha256(identity_hash) or not _is_sha256(receipt_hash) or not updated:
        raise ValueError("authority policy identity, generation and change receipt are required")
    if any(item not in PROMOTABLE_STRATEGY_FAMILIES for item in strategy_families):
        raise ValueError("authority policy contains unsupported strategy family")
    if mode_value == "v2" and (not evidence_hash or not _is_sha256(evidence_hash) or not strategy_families):
        raise ValueError("v2 authority requires promotion evidence and covered strategy families")
    if evidence_hash and not _is_sha256(evidence_hash):
        raise ValueError("promotion evidence hash must be SHA-256")
    payload = {
        "schema_version": AUTHORITY_POLICY_SCHEMA,
        "scope_derivation_version": SCOPE_DERIVATION_VERSION,
        "portfolio_scope_id": scope_for(account),
        "normalized_portfolio_source": source,
        "portfolio_account_identity_hash": identity_hash,
        "normalized_account": account,
        "mode": mode_value,
        "generation": generation_value,
        "updated_at": updated,
        "change_receipt_hash": receipt_hash,
        "promotion_evidence_hash": evidence_hash,
        "covered_strategy_families": strategy_families,
    }
    return {**payload, "policy_hash": canonical_sha256(payload)}


def validate_authority_policy(policy: dict[str, Any], *, expected_scope_id: str | None = None) -> tuple[str, ...]:
    payload = dict(policy or {})
    reasons: list[str] = []
    if payload.get("schema_version") != AUTHORITY_POLICY_SCHEMA:
        reasons.append("authority_schema_invalid")
    if payload.get("scope_derivation_version") != SCOPE_DERIVATION_VERSION:
        reasons.append("scope_derivation_version_mismatch")
    try:
        account = normalize_account_label(payload.get("normalized_account"))
        derived_scope = scope_for(account)
    except ValueError:
        account = ""
        derived_scope = ""
        reasons.append("authority_account_invalid")
    if payload.get("normalized_account") != account:
        reasons.append("authority_account_not_normalized")
    if not derived_scope or payload.get("portfolio_scope_id") != derived_scope:
        reasons.append("authority_scope_mismatch")
    if expected_scope_id and derived_scope != expected_scope_id:
        reasons.append("authority_path_scope_mismatch")
    if payload.get("mode") not in AUTHORITY_MODES:
        reasons.append("authority_mode_unknown")
    try:
        generation = _positive_generation(payload.get("generation"))
    except ValueError:
        reasons.append("authority_generation_invalid")
        generation = None
    try:
        source = normalize_portfolio_source(payload.get("normalized_portfolio_source"))
    except ValueError:
        source = ""
        reasons.append("authority_source_missing")
    if payload.get("normalized_portfolio_source") != source:
        reasons.append("authority_source_not_normalized")
    if not _is_sha256(payload.get("portfolio_account_identity_hash")):
        reasons.append("authority_identity_missing")
    if not _is_sha256(payload.get("change_receipt_hash")):
        reasons.append("authority_change_receipt_missing")
    if not str(payload.get("updated_at") or "").strip():
        reasons.append("authority_updated_at_missing")
    families = payload.get("covered_strategy_families")
    if not isinstance(families, list) or families != sorted(set(families)):
        reasons.append("authority_strategy_families_invalid")
        families = []
    if any(item not in PROMOTABLE_STRATEGY_FAMILIES for item in families):
        reasons.append("authority_strategy_families_invalid")
    evidence_hash = payload.get("promotion_evidence_hash")
    if evidence_hash is not None and not _is_sha256(evidence_hash):
        reasons.append("authority_promotion_evidence_invalid")
    if payload.get("mode") == "v2" and (not evidence_hash or not families):
        reasons.append("authority_v2_promotion_evidence_missing")
    if payload.get("policy_hash") != canonical_sha256(_policy_hash_payload(payload)):
        reasons.append("authority_policy_hash_mismatch")
    return tuple(sorted(set(reasons)))


def resolve_authority(
    *,
    normalized_account_label: Any,
    normalized_portfolio_source: Any,
    portfolio_account_identity_hash_value: str,
    policy: dict[str, Any] | None,
    historical_authority_state_exists: bool = False,
    policy_read_error: bool = False,
) -> AuthorityResolution:
    scope_id = scope_for(normalized_account_label)
    if policy_read_error:
        return AuthorityResolution(scope_id, None, None, None, "authority_conflict", ("authority_policy_unreadable",))
    if policy is None:
        if historical_authority_state_exists:
            return AuthorityResolution(
                scope_id,
                None,
                None,
                None,
                "authority_conflict",
                ("authority_policy_missing_with_history",),
            )
        return AuthorityResolution(scope_id, "v1", 0, None, "first_use_default_v1")
    reasons = list(validate_authority_policy(policy, expected_scope_id=scope_id))
    try:
        current_source = normalize_portfolio_source(normalized_portfolio_source)
    except ValueError:
        current_source = ""
        reasons.append("caller_source_unavailable")
    current_identity = str(portfolio_account_identity_hash_value or "").strip()
    if policy.get("normalized_portfolio_source") != current_source:
        reasons.append("portfolio_source_identity_conflict")
    if not current_identity or policy.get("portfolio_account_identity_hash") != current_identity:
        reasons.append("portfolio_account_identity_conflict")
    if reasons:
        return AuthorityResolution(
            scope_id,
            None,
            None,
            None,
            "authority_conflict",
            tuple(sorted(set(reasons))),
        )
    return AuthorityResolution(
        scope_id,
        str(policy["mode"]),
        int(policy["generation"]),
        str(policy["policy_hash"]),
        "resolved",
        (),
        tuple(str(item) for item in policy.get("covered_strategy_families") or ()),
    )


def validate_first_use_uniqueness(
    *,
    target_scope_id: str,
    target_identity_hash: str,
    existing_policies: Iterable[dict[str, Any]],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not _is_sha256(target_identity_hash):
        reasons.append("target_portfolio_identity_invalid")
    for policy in existing_policies:
        policy_reasons = validate_authority_policy(policy)
        if policy_reasons:
            reasons.append("existing_authority_policy_malformed")
            continue
        if (
            policy.get("portfolio_account_identity_hash") == target_identity_hash
            and policy.get("portfolio_scope_id") != target_scope_id
        ):
            reasons.append("portfolio_identity_already_bound_to_other_scope")
    return tuple(sorted(set(reasons)))


__all__ = [
    "AUTHORITY_MODES",
    "AUTHORITY_POLICY_SCHEMA",
    "PROMOTABLE_STRATEGY_FAMILIES",
    "SCOPE_DERIVATION_VERSION",
    "AuthorityResolution",
    "build_authority_policy",
    "capacity_pool_authority_id",
    "normalize_account_label",
    "normalize_portfolio_source",
    "portfolio_account_identity_hash",
    "resolve_authority",
    "scope_for",
    "validate_authority_policy",
    "validate_first_use_uniqueness",
]
