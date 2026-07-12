from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from src.application.shadow_replay.common import (
    first_float,
    normal_status,
    resolve_output_path,
    safety_payload,
    text,
    utc_now,
    write_json,
)
from src.application.shadow_replay.parameter_sets import (
    ALLOWED_PARAMETERS,
    CURRENT_UNDERWRITING_PROFILE,
    LEGACY_SHORT_VOL_PROFILE,
    ParameterSet,
    ParameterVariant,
)
from src.application.strategy_lab.decisions import (
    build_decision_instances,
    strategy_family,
    summarize_decision_instances,
)
from src.application.strategy_lab.domains import list_domain_adapters
from src.application.strategy_lab.evidence import load_strategy_lab_evidence


HYPOTHESES_SCHEMA_VERSION = "strategy_lab_hypotheses.v1"
UNDERWRITING_PARAMETERS = tuple(sorted(ALLOWED_PARAMETERS[CURRENT_UNDERWRITING_PROFILE]))
ACCEPTED_STATUSES = {"accepted", "notified"}
FILTER_HINTS = {
    "delta": ("delta", "abs_delta"),
    "dte": ("dte", "expiration"),
    "iv_rv": ("iv_rv", "iv_minus_rv", "rv"),
    "annualized_return": ("annualized", "return", "yield"),
}


def generate_strategy_lab_hypotheses(
    *,
    dataset: str | Path | None = None,
    repo_root: str | Path | None = None,
    runs_root: str | Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    accounts: list[str] | tuple[str, ...] | None = None,
    market: str | None = None,
    min_sample: int = 30,
    output: str | Path | None = None,
) -> dict[str, Any]:
    evidence = load_strategy_lab_evidence(
        repo_root=repo_root,
        dataset=dataset,
        runs_root=runs_root,
        start_date=start_date,
        end_date=end_date,
        accounts=accounts,
        market=market,
    )
    sample_floor = max(1, int(min_sample))
    candidates = list(evidence["candidate_snapshots"])
    filter_decisions = list(evidence["filter_decisions"])
    decisions = build_decision_instances(candidates)
    domain_hypotheses: list[dict[str, Any]] = []
    variants: list[ParameterVariant] = []
    used_names: set[str] = set()

    for adapter in list_domain_adapters():
        family_candidates = [
            row
            for row in candidates
            if strategy_family(row) == adapter.strategy_family
            and _candidate_profile(row) == CURRENT_UNDERWRITING_PROFILE
        ]
        family_decisions = [row for row in decisions if row.get("strategy_family") == adapter.strategy_family]
        family_filters = [
            row for row in filter_decisions if strategy_family(row) == adapter.strategy_family
        ]
        hypothesis = _domain_hypothesis(
            adapter=adapter.to_payload(),
            candidates=family_candidates,
            decisions=family_decisions,
            filter_decisions=family_filters,
            min_sample=sample_floor,
        )
        domain_hypotheses.append(hypothesis)
        for variant in hypothesis.pop("_variants", []):
            if variant.name in used_names:
                continue
            used_names.add(variant.name)
            variants.append(variant)

    parameter_set = ParameterSet(baseline="production_observed", variants=tuple(variants)) if variants else None
    blocker_counts = Counter()
    for item in domain_hypotheses:
        blocker_counts.update(str(blocker) for blocker in item.get("blockers") or [] if str(blocker))
    status = "ready_for_experiment" if parameter_set else "not_ready"
    if blocker_counts and parameter_set:
        status = "partial_ready"

    result: dict[str, Any] = {
        "schema_version": HYPOTHESES_SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "dataset_dir": evidence["dataset_dir"],
        "input_scope": {
            "source": evidence.get("source") or {},
            "coverage": evidence.get("coverage") or {},
            "filters": evidence.get("filters") or {},
        },
        "summary": {
            "status": status,
            "min_sample": sample_floor,
            "domain_count": len(domain_hypotheses),
            "variant_count": len(variants),
            "parameter_set_ready": parameter_set is not None,
        },
        "decision_instances": {
            "summary": summarize_decision_instances(decisions),
        },
        "domain_hypotheses": domain_hypotheses,
        "parameter_set": parameter_set.to_payload() if parameter_set else None,
        "candidate_impact_parameter_set": _parameter_set_input_payload(parameter_set) if parameter_set else None,
        "blockers": dict(blocker_counts.most_common(20)),
        "safety": {
            **safety_payload(writes_local_dataset=False),
            "runtime_config_write_allowed": False,
        },
    }
    if output:
        write_json(resolve_output_path(output), result)
    return result


def _domain_hypothesis(
    *,
    adapter: dict[str, Any],
    candidates: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    filter_decisions: list[dict[str, Any]],
    min_sample: int,
) -> dict[str, Any]:
    family = text(adapter.get("strategy_family"))
    filter_pressure = _filter_pressure(filter_decisions)
    if not bool(adapter.get("hypothesis_enabled")):
        delegated = family == "combo_yield"
        return {
            "strategy_family": family,
            "adapter": adapter,
            "status": "group_experiment_delegated" if delegated else "readiness_only",
            "candidate_count": len(candidates),
            "decision_instance_count": len(decisions),
            "filter_pressure": filter_pressure,
            "baseline_parameters": None,
            "variants": [],
            "blockers": [],
            "limitations": (
                [
                    "single_leg_parameter_set_not_supported",
                    "combo_yield_group_optimizer_runs_in_strategy_lab_experiment",
                ]
                if delegated
                else ["single_leg_hypothesis_disabled"]
            ),
            "_variants": [],
        }

    blockers: list[str] = []
    limitations: list[str] = []
    if len(decisions) < min_sample:
        blockers.append("decision_sample_below_min_sample")
    if len(candidates) < min_sample:
        blockers.append("candidate_sample_below_min_sample")
    baseline = _empirical_baseline(candidates)
    if not baseline:
        blockers.append("parameter_field_evidence_missing")
    variants = _variants_for_family(family=family, baseline=baseline) if baseline and not blockers else []
    status = "ready" if variants else "not_ready"
    if blockers and variants:
        status = "partial_ready"
    return {
        "strategy_family": family,
        "adapter": adapter,
        "status": status,
        "candidate_count": len(candidates),
        "decision_instance_count": len(decisions),
        "accepted_candidate_count": sum(1 for row in candidates if normal_status(row.get("status")) in ACCEPTED_STATUSES),
        "filter_pressure": filter_pressure,
        "baseline_parameters": baseline or None,
        "variants": [variant.to_payload() for variant in variants],
        "blockers": blockers,
        "limitations": limitations,
        "_variants": variants,
    }


def _candidate_profile(row: dict[str, Any]) -> str:
    raw = text(row.get("strategy_profile") or row.get("profile") or row.get("strategy_mode")).lower()
    if raw in {CURRENT_UNDERWRITING_PROFILE, LEGACY_SHORT_VOL_PROFILE, ""}:
        return CURRENT_UNDERWRITING_PROFILE
    return raw


def _empirical_baseline(candidates: list[dict[str, Any]]) -> dict[str, float]:
    accepted = [row for row in candidates if normal_status(row.get("status")) in ACCEPTED_STATUSES]
    source = accepted or candidates
    params: dict[str, float] = {}
    values = {
        "min_iv_rv_ratio": [value for row in source if (value := first_float(row, "iv_rv_ratio")) is not None],
        "min_iv_minus_rv": [value for row in source if (value := first_float(row, "iv_minus_rv")) is not None],
        "min_dte": [value for row in source if (value := first_float(row, "dte")) is not None],
        "max_dte": [value for row in source if (value := first_float(row, "dte")) is not None],
        "min_annualized_return": [
            value for row in source if (value := first_float(row, "annualized_return")) is not None
        ],
    }
    for key, field_values in values.items():
        if not field_values:
            continue
        value = max(field_values) if key.startswith("max_") else min(field_values)
        params[key] = round(float(value), 6)
    if params.get("min_dte") is not None and params.get("max_dte") is not None:
        if params["min_dte"] > params["max_dte"]:
            params["min_dte"], params["max_dte"] = params["max_dte"], params["min_dte"]
    return params


def _variants_for_family(*, family: str, baseline: dict[str, float]) -> list[ParameterVariant]:
    variants: list[ParameterVariant] = []
    seen: set[tuple[tuple[str, float], ...]] = set()

    def add(name: str, params: dict[str, float]) -> None:
        clean = {key: round(float(value), 6) for key, value in params.items() if key in UNDERWRITING_PARAMETERS}
        if not clean:
            return
        if "min_dte" in clean and "max_dte" in clean and clean["min_dte"] > clean["max_dte"]:
            return
        signature = tuple(sorted(clean.items()))
        if signature in seen:
            return
        seen.add(signature)
        variants.append(ParameterVariant(name=f"{family}_{name}", profiles={CURRENT_UNDERWRITING_PROFILE: clean}))

    if {"min_dte", "max_dte"} & set(baseline):
        params = dict(baseline)
        if "min_dte" in params:
            params["min_dte"] = max(1.0, params["min_dte"] - 5.0)
        if "max_dte" in params:
            params["max_dte"] = min(365.0, params["max_dte"] + 5.0)
        add("extend_dte_window", params)

    if {"min_iv_rv_ratio", "min_iv_minus_rv"} & set(baseline):
        params = dict(baseline)
        if "min_iv_rv_ratio" in params:
            params["min_iv_rv_ratio"] = max(0.0, params["min_iv_rv_ratio"] - 0.05)
        if "min_iv_minus_rv" in params:
            params["min_iv_minus_rv"] = max(0.0, params["min_iv_minus_rv"] - 0.01)
        add("relax_iv_rv_floor", params)

    if "min_annualized_return" in baseline:
        params = dict(baseline)
        params["min_annualized_return"] = max(0.0, params["min_annualized_return"] - 0.01)
        add("relax_return_floor", params)

    params = dict(baseline)
    if "min_dte" in params:
        params["min_dte"] = max(1.0, params["min_dte"] - 5.0)
    if "max_dte" in params:
        params["max_dte"] = min(365.0, params["max_dte"] + 5.0)
    if "min_iv_rv_ratio" in params:
        params["min_iv_rv_ratio"] = max(0.0, params["min_iv_rv_ratio"] - 0.05)
    if "min_iv_minus_rv" in params:
        params["min_iv_minus_rv"] = max(0.0, params["min_iv_minus_rv"] - 0.01)
    if "min_annualized_return" in params:
        params["min_annualized_return"] = max(0.0, params["min_annualized_return"] - 0.01)
    add("relax_primary_filters", params)

    return variants


def _filter_pressure(filter_decisions: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in filter_decisions:
        blob = " ".join(
            text(row.get(key)).lower()
            for key in ("rule", "filter_rule", "stage", "reason", "reasons", "message")
        )
        for bucket, hints in FILTER_HINTS.items():
            if any(hint in blob for hint in hints):
                counts[bucket] += 1
                break
    return dict(counts.most_common())


def _parameter_set_input_payload(parameter_set: ParameterSet) -> dict[str, Any]:
    return {
        "baseline": parameter_set.baseline,
        "variants": [
            {
                "name": variant.name,
                **variant.profiles,
            }
            for variant in parameter_set.variants
        ],
    }
