from __future__ import annotations

from typing import Any, Mapping

from domain.domain.engine import evaluate_opening_candidate_policy


OPENING_POLICY_SHADOW_SCHEMA = "opening_policy_shadow_comparison.v1"
_MISSING = object()

_DIFFERENCE_AXES: dict[str, tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]] = {
    "data_unavailable_scope": (
        (
            "status",
            ("opening_contract_status", "data_status"),
            ("opening_contract_status",),
        ),
        (
            "reason",
            ("opening_contract_reason_codes", "data_reason"),
            ("opening_contract_reason_codes",),
        ),
    ),
    "recall_boundary": (
        ("min_dte", ("min_dte",), ("policy_min_dte",)),
        ("max_dte", ("max_dte",), ("policy_max_dte",)),
        ("max_strike", ("max_strike",), ("policy_max_strike",)),
        ("min_strike", ("min_strike",), ("policy_min_strike",)),
    ),
    "fee_and_tick": (
        ("sell_limit", ("sell_limit",), ("sell_limit",)),
        (
            "estimated_full_sell_fees",
            ("estimated_full_sell_fees",),
            ("estimated_full_sell_fees",),
        ),
        ("price_tick", ("price_tick",), ("price_tick",)),
    ),
    "return_basis": (
        (
            "period_return",
            ("period_return", "annualized_return"),
            (
                "period_net_return_on_cash_basis",
                "period_net_premium_return",
            ),
        ),
        ("net_cash_basis", ("cash_basis",), ("net_cash_basis",)),
        (
            "current_market_value",
            ("stock_cost_basis",),
            ("current_market_value",),
        ),
    ),
    "realized_volatility": (
        (
            "rv",
            ("realized_volatility_estimate",),
            ("term_matched_rv",),
        ),
    ),
    "earnings": (
        (
            "source",
            ("event_source", "earnings_source"),
            ("earnings_source",),
        ),
        (
            "has_event",
            ("earnings_has_event",),
            ("earnings_has_event",),
        ),
    ),
    "capacity": (
        (
            "max_new_contracts",
            ("max_new_contracts", "max_contracts"),
            ("max_new_contracts",),
        ),
        (
            "covered_contracts_available",
            ("covered_contracts_available",),
            ("covered_contracts_available",),
        ),
    ),
    "ranking": (
        ("rank", ("rank",), ("rank",)),
        (
            "primary_return",
            ("strategy_score", "premium_edge_score"),
            (
                "period_net_return_on_cash_basis",
                "period_net_premium_return",
            ),
        ),
    ),
}
_CLASSIFIED_INPUT_KEYS = frozenset(
    key
    for specs in _DIFFERENCE_AXES.values()
    for _label, legacy_keys, opening_keys in specs
    for key in (*legacy_keys, *opening_keys)
)
_LEGACY_OUTCOME_KEYS = frozenset({"accepted", "status"})


def compare_opening_policy_shadow(
    *,
    legacy_candidate: Mapping[str, Any],
    opening_candidate: Mapping[str, Any],
    mode: str,
) -> dict[str, Any]:
    """Classify an offline legacy/new replay without reviving legacy authority."""

    legacy = dict(legacy_candidate)
    opening = dict(opening_candidate)
    legacy_accepted = _legacy_accepted(legacy)
    opening_decision = evaluate_opening_candidate_policy(opening, mode=mode)
    differences: list[dict[str, Any]] = []
    for category, specs in _DIFFERENCE_AXES.items():
        fields: list[dict[str, Any]] = []
        for label, legacy_keys, opening_keys in specs:
            legacy_value = _first_present(legacy, legacy_keys)
            opening_value = _first_present(opening, opening_keys)
            if (
                legacy_value is _MISSING
                and opening_value is _MISSING
            ) or legacy_value == opening_value:
                continue
            fields.append(
                {
                    "field": label,
                    "legacy": None if legacy_value is _MISSING else legacy_value,
                    "opening": None if opening_value is _MISSING else opening_value,
                }
            )
        if fields:
            differences.append({"category": category, "fields": fields})

    opening_accepted = opening_decision.get("accepted") is True
    unclassified = [
        f"field:{key}"
        for key in sorted(set(legacy) | set(opening))
        if key not in _CLASSIFIED_INPUT_KEYS
        and key not in _LEGACY_OUTCOME_KEYS
        and legacy.get(key, _MISSING) != opening.get(key, _MISSING)
    ]
    if legacy_accepted != opening_accepted and not differences:
        unclassified.append("acceptance_change_without_classified_evidence")
    return {
        "schema_version": OPENING_POLICY_SHADOW_SCHEMA,
        "mode": str(mode).strip().lower(),
        "legacy": {"accepted": legacy_accepted},
        "opening": {
            "accepted": opening_accepted,
            "decision_hash": opening_decision.get("decision_hash"),
            "reject_reasons": [
                str(item.get("reason") or "")
                for item in opening_decision.get("rejects") or []
            ],
        },
        "differences": differences,
        "unclassified_differences": unclassified,
        "promotion_ready": not unclassified,
    }


def _legacy_accepted(candidate: Mapping[str, Any]) -> bool:
    if isinstance(candidate.get("accepted"), bool):
        return bool(candidate["accepted"])
    status = str(candidate.get("status") or "").strip().lower()
    if status in {"accepted", "notified", "candidates_found"}:
        return True
    if status in {"rejected", "post_filtered", "ranked_below", "no_candidate"}:
        return False
    raise ValueError("legacy candidate acceptance is unavailable")


def _first_present(candidate: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in candidate:
            return candidate[key]
    return _MISSING


__all__ = [
    "OPENING_POLICY_SHADOW_SCHEMA",
    "compare_opening_policy_shadow",
]
