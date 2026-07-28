from __future__ import annotations

from dataclasses import asdict
from typing import Any, Iterable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine import (
    REJECT_RISK_INSURANCE_UNDERWRITING,
    STAGE_RISK_FILTER,
    build_candidate_reject,
    build_replacement_candidate_decision,
    validate_candidate_decision_payload,
)
from domain.domain.insurance_underwriting import (
    InsuranceUnderwritingConfig,
    evaluate_underwriting_candidate,
)
from src.application.short_vol_risk_context import (
    enrich_short_vol_contract_cny_fields,
)
from src.infrastructure.exchange_rates import CurrencyConverter


def apply_insurance_underwriting_to_all_decisions(
    decisions: Iterable[Mapping[str, Any]],
    *,
    mode: str,
    cfg: InsuranceUnderwritingConfig,
    exchange_rate_converter: CurrencyConverter,
) -> list[dict[str, Any]]:
    """Apply the opening CSV's non-resource underwriting gate to its sidecar."""

    rows = [dict(item) for item in decisions]
    if not cfg.enabled:
        return rows

    policy_extension = {
        "schema": "insurance_underwriting_policy.v1",
        **asdict(cfg),
    }
    out: list[dict[str, Any]] = []
    for item in rows:
        normalized_input = dict(item.get("normalized_input") or {})
        normalized_input.update(
            enrich_short_vol_contract_cny_fields(
                normalized_input,
                exchange_rate_converter=exchange_rate_converter,
            )
        )
        underwriting = evaluate_underwriting_candidate(
            normalized_input,
            mode=mode,
            cfg=cfg,
        )
        normalized_input.update(dict(underwriting.get("fields") or {}))
        normalized_input["insurance_underwriting_accepted"] = bool(
            underwriting.get("accepted")
        )
        normalized_input["insurance_underwriting_rule"] = str(
            underwriting.get("rule") or ""
        )

        invariant_original = dict(item.get("invariant_decision") or {})
        risk_policy = dict(invariant_original.get("risk_policy") or {})
        risk_policy["insurance_underwriting"] = policy_extension
        risk_policy_hash = canonical_sha256(risk_policy)

        opening = _apply_underwriting_decision(
            item.get("opening_decision"),
            normalized_input=normalized_input,
            risk_policy_hash=risk_policy_hash,
            underwriting=underwriting,
            risk_policy=None,
        )
        invariant = _apply_underwriting_decision(
            invariant_original,
            normalized_input=normalized_input,
            risk_policy_hash=risk_policy_hash,
            underwriting=underwriting,
            risk_policy=risk_policy,
        )
        candidate_id = str(item.get("candidate_id") or "")
        replacement = build_replacement_candidate_decision(
            candidate_id=candidate_id,
            opening_decision=opening,
            invariant_decision=invariant,
        )
        updated = dict(item)
        updated.update(
            {
                "normalized_input": normalized_input,
                "normalized_input_hash": canonical_sha256(normalized_input),
                "risk_policy_hash": risk_policy_hash,
                "opening_decision": opening,
                "invariant_decision": invariant,
                "replacement_candidate_decision": replacement,
            }
        )
        out.append(updated)
    return sorted(
        out,
        key=lambda item: (
            str(item.get("strategy_mode") or ""),
            str(item.get("candidate_id") or ""),
        ),
    )


def _apply_underwriting_decision(
    candidate_decision: Mapping[str, Any] | Any,
    *,
    normalized_input: dict[str, Any],
    risk_policy_hash: str,
    underwriting: Mapping[str, Any],
    risk_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    decision = validate_candidate_decision_payload(dict(candidate_decision or {}))
    rejects = [dict(item) for item in decision.get("rejects", [])]
    underwriting_accepted = bool(underwriting.get("accepted"))
    if not underwriting_accepted:
        rejects.append(
            build_candidate_reject(
                stage=STAGE_RISK_FILTER,
                reason=REJECT_RISK_INSURANCE_UNDERWRITING,
                message=str(
                    underwriting.get("message")
                    or underwriting.get("rule")
                    or "insurance underwriting rejected"
                ),
                metric_value=underwriting.get("metric_value"),
                threshold=underwriting.get("threshold"),
            )
        )
    updated = dict(decision)
    updated.update(
        {
            "accepted": bool(decision.get("accepted")) and underwriting_accepted,
            "rejects": rejects,
            "normalized_input": normalized_input,
            "normalized_input_hash": canonical_sha256(normalized_input),
            "risk_policy_hash": risk_policy_hash,
        }
    )
    if risk_policy is not None:
        updated["risk_policy"] = risk_policy
        stage_hashes = dict(updated.get("stage_decision_hashes") or {})
        stage_hashes["stage3_insurance_underwriting"] = canonical_sha256(
            dict(underwriting)
        )
        updated["stage_decision_hashes"] = stage_hashes
    updated.pop("decision_hash", None)
    updated = validate_candidate_decision_payload(updated)
    updated["decision_hash"] = canonical_sha256(updated)
    return updated
