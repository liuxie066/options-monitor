from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
import math
from statistics import mean, median
from typing import Any, Iterable

from domain.domain.close_advice import (
    DEFAULT_TIER_RULES,
    POLICY_VARIANT_P0_CURRENT,
    POLICY_VARIANT_P1_SEMANTIC_SPLIT,
    POLICY_VARIANT_P2_PROFILE_AWARE,
    POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED,
    CloseAdviceConfig,
    CloseAdviceTierRule,
    CloseDecisionFacts,
    evaluate_close_policy,
)
from domain.domain.symbol_identity import symbol_market
from src.application.shadow_replay.close_decision_policy import (
    ReallocationPolicyEvidence,
    compose_opportunity_required_policy,
)
from src.application.shadow_replay.common import safety_payload, text


CLOSE_POLICY_ANALYSIS_SCHEMA_VERSION = "shadow_replay_close_policy_analysis.v1"
_POLICIES = (
    POLICY_VARIANT_P0_CURRENT,
    POLICY_VARIANT_P1_SEMANTIC_SPLIT,
    POLICY_VARIANT_P2_PROFILE_AWARE,
    POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED,
)
_OUTCOME_PRECEDENCE = {
    "horizon_1d": 1,
    "horizon_3d": 3,
    "horizon_7d": 7,
    "horizon_14d": 14,
    "terminal": 100,
}
_DETERMINATE_ACTIONS = {"close", "hold"}
_ACTIONABLE_REMINDERS = {"close", "review"}
_FACT_FIELDS = tuple(CloseDecisionFacts.__dataclass_fields__)


def analyze_close_policy_rows(
    *,
    episodes: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    readiness: dict[str, Any],
) -> dict[str, Any]:
    """Build a descriptive paired policy report for mechanically ready segments."""

    episode_by_id = _unique_episode_index(episodes)
    all_usable_outcomes = _usable_outcomes_by_episode(
        outcomes,
        episode_by_id=episode_by_id,
    )
    evidence_outcomes_by_episode = _all_outcomes_by_episode(
        outcomes,
        episode_by_id=episode_by_id,
    )
    all_reasons = Counter(
        text(row.get("inconclusive_reason")).lower() or "unspecified"
        for row in outcomes
        if text(row.get("evidence_status")).lower() == "inconclusive"
    )
    promotable_segments = {
        (text(row.get("strategy_profile")).lower(), text(row.get("strategy_family")).lower())
        for row in readiness.get("promotable_segments", [])
        if isinstance(row, dict)
    }
    mechanically_usable_ids = {
        text(item)
        for item in (readiness.get("analysis_eligibility") or {}).get(
            "promotion_usable_episode_ids", []
        )
        if text(item)
    }
    mechanically_usable_keys = {
        (text(row.get("episode_id")), text(row.get("outcome_kind")).lower())
        for row in (readiness.get("analysis_eligibility") or {}).get(
            "promotion_usable_outcome_keys", []
        )
        if isinstance(row, dict)
    }
    selected_ids = {
        episode_id
        for episode_id, episode in episode_by_id.items()
        if episode_id in mechanically_usable_ids
        and _segment(episode) in promotable_segments
    }
    outcomes_by_episode = {
        episode_id: [
            row
            for row in rows
            if (episode_id, text(row.get("outcome_kind")).lower())
            in mechanically_usable_keys
        ]
        for episode_id, rows in all_usable_outcomes.items()
        if episode_id in selected_ids
    }
    shadow_only_segments = [
        {
            "strategy_profile": row.get("strategy_profile"),
            "strategy_family": row.get("strategy_family"),
            "episode_count": row.get("episode_count"),
            "promotion_usable_episode_count": row.get("promotion_usable_episode_count"),
            "promotion_usable_ratio": row.get("promotion_usable_ratio"),
        }
        for row in readiness.get("segments", [])
        if isinstance(row, dict) and not row.get("mechanically_ready")
    ]
    base = {
        "schema_version": CLOSE_POLICY_ANALYSIS_SCHEMA_VERSION,
        "mechanical_readiness_status": readiness.get("status"),
        "promotable_segments": [
            {"strategy_profile": profile, "strategy_family": family}
            for profile, family in sorted(promotable_segments)
        ],
        "shadow_only_segments": shadow_only_segments,
        "all_evidence_coverage": {
            "unique_episode_count": len(episode_by_id),
            "selected_episode_count": len(selected_ids),
            "excluded_from_paired_analysis_episode_count": (
                len(episode_by_id) - len(selected_ids)
            ),
            "usable_outcome_episode_count": len(all_usable_outcomes),
            "mechanically_eligible_episode_count": len(selected_ids),
            "inconclusive_outcome_count": sum(all_reasons.values()),
            "inconclusive_reasons": dict(sorted(all_reasons.items())),
        },
        "automatic_policy_winner": None,
        "automatic_parameter_recommendation": None,
        "policy_quality_judgment": "ceo_required",
        "review_action_imputation": "none",
        "production_promotion_allowed": False,
        "safety": safety_payload(writes_local_dataset=False),
    }
    if readiness.get("status") != "ready_for_paired_policy_analysis" or not selected_ids:
        return {
            **base,
            "status": "blocked_mechanical_readiness",
            "reason": readiness.get("reason") or "no_promotable_segment",
            "reports": None,
            "bounded_threshold_sensitivity": None,
        }

    preferred = {
        episode_id: _preferred_evaluation_outcome(rows)
        for episode_id, rows in outcomes_by_episode.items()
        if episode_id in selected_ids and rows
    }
    report = _comparison_report(
        episode_ids=selected_ids,
        episode_by_id=episode_by_id,
        outcomes_by_episode=outcomes_by_episode,
        evidence_outcomes_by_episode=evidence_outcomes_by_episode,
        preferred_outcome=preferred,
    )
    return {
        **base,
        "status": "ready_for_ceo_review",
        "reason": "paired_policy_facts_available",
        "episode_evaluation_rule": (
            "terminal_if_usable_else_longest_usable_fixed_horizon; "
            "close_precision_uses_best_usable_hold_horizon"
        ),
        "reports": {
            "aggregate": report,
            "by_profile_family": _group_reports(
                selected_ids,
                key_fn=lambda episode: _segment(episode),
                key_names=("strategy_profile", "strategy_family"),
                episode_by_id=episode_by_id,
                outcomes_by_episode=outcomes_by_episode,
                evidence_outcomes_by_episode=evidence_outcomes_by_episode,
                preferred_outcome=preferred,
            ),
            "by_market": _group_reports(
                selected_ids,
                key_fn=lambda episode: (_market(episode),),
                key_names=("market",),
                episode_by_id=episode_by_id,
                outcomes_by_episode=outcomes_by_episode,
                evidence_outcomes_by_episode=evidence_outcomes_by_episode,
                preferred_outcome=preferred,
            ),
            "by_account": _group_reports(
                selected_ids,
                key_fn=lambda episode: (text(episode.get("account")).lower() or "unknown",),
                key_names=("account",),
                episode_by_id=episode_by_id,
                outcomes_by_episode=outcomes_by_episode,
                evidence_outcomes_by_episode=evidence_outcomes_by_episode,
                preferred_outcome=preferred,
            ),
        },
        "bounded_threshold_sensitivity": _threshold_sensitivity(
            episode_ids=selected_ids,
            episode_by_id=episode_by_id,
            preferred_outcome=preferred,
        ),
    }


def _comparison_report(
    *,
    episode_ids: set[str],
    episode_by_id: dict[str, dict[str, Any]],
    outcomes_by_episode: dict[str, list[dict[str, Any]]],
    evidence_outcomes_by_episode: dict[str, list[dict[str, Any]]],
    preferred_outcome: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    ordered_ids = sorted(episode_ids)
    p3_preferred_outcome = dict(preferred_outcome)
    p3_preferred_outcome.update(
        {
            episode_id: _preferred_evaluation_outcome(replacement_rows)
            for episode_id in episode_ids
            if (
                replacement_rows := [
                    row
                    for row in outcomes_by_episode.get(episode_id, [])
                    if text(row.get("replacement_outcome_status")).lower() == "usable"
                ]
            )
        }
    )
    actions = {
        policy: {
            episode_id: _policy_action(episode_by_id[episode_id], policy)
            for episode_id in ordered_ids
        }
        for policy in _POLICIES
    }
    action_counts = {
        policy: dict(sorted(Counter(mapping.values()).items()))
        for policy, mapping in actions.items()
    }
    policy_metrics = {
        policy: _policy_metrics(
            policy=policy,
            episode_ids=episode_ids,
            episode_by_id=episode_by_id,
            outcomes_by_episode=outcomes_by_episode,
            preferred_outcome=(
                p3_preferred_outcome
                if policy == POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED
                else preferred_outcome
            ),
            actions=actions[policy],
        )
        for policy in _POLICIES
    }
    paired = {
        f"{policy}_vs_{POLICY_VARIANT_P0_CURRENT}": _paired_policy_metrics(
            baseline_policy=POLICY_VARIANT_P0_CURRENT,
            proposed_policy=policy,
            episode_ids=episode_ids,
            preferred_outcome=(
                p3_preferred_outcome
                if policy == POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED
                else preferred_outcome
            ),
            baseline_actions=actions[POLICY_VARIANT_P0_CURRENT],
            proposed_actions=actions[policy],
        )
        for policy in _POLICIES[1:]
    }
    return {
        "coverage": {
            "episode_count": len(episode_ids),
            "preferred_outcome_episode_count": len(episode_ids & set(preferred_outcome)),
            "preferred_outcome_inconclusive_episode_count": len(
                episode_ids - set(preferred_outcome)
            ),
            "unique_lot_count": len(
                {_lot_key(episode_by_id[item]) for item in episode_ids}
            ),
        },
        "action_counts": action_counts,
        "policy_metrics": policy_metrics,
        "paired_comparisons": paired,
        "outcome_path_risk": _path_risk(
            episode_ids=episode_ids,
            outcomes_by_episode=outcomes_by_episode,
        ),
        "terminal_willingness_alignment": _terminal_alignment(
            episode_ids=episode_ids,
            outcomes_by_episode=evidence_outcomes_by_episode,
        ),
        "p3_switch_opportunity": _p3_switch_opportunity(
            episode_ids=episode_ids,
            episode_by_id=episode_by_id,
            outcomes_by_episode=outcomes_by_episode,
        ),
        "operational": _operational_metrics(
            episode_ids=episode_ids,
            episode_by_id=episode_by_id,
            actions=actions,
        ),
        "unique_lot_rollup": _unique_lot_rollup(
            episode_ids=episode_ids,
            episode_by_id=episode_by_id,
            preferred_outcome=preferred_outcome,
            p3_preferred_outcome=p3_preferred_outcome,
            actions=actions,
        ),
    }


def _policy_metrics(
    *,
    policy: str,
    episode_ids: set[str],
    episode_by_id: dict[str, dict[str, Any]],
    outcomes_by_episode: dict[str, list[dict[str, Any]]],
    preferred_outcome: dict[str, dict[str, Any]],
    actions: dict[str, str],
) -> dict[str, Any]:
    regret: list[float] = []
    benefit: list[float] = []
    net_incremental: list[float] = []
    paired_population = 0
    net_population = 0
    close_costs: list[float] = []
    close_cost_population = 0
    switch_costs: list[float] = []
    switch_population = 0
    precision_population = 0
    precision_hits = 0
    known_deterioration_hold_count = 0
    known_deterioration_adverse_losses: list[float] = []
    for episode_id in episode_ids:
        action = actions[episode_id]
        outcome = preferred_outcome.get(episode_id)
        if outcome is not None and action != "not_evaluable":
            pair = _regret_benefit(policy=policy, action=action, outcome=outcome)
            if pair is not None:
                paired_population += 1
                regret.append(pair[0])
                benefit.append(pair[1])
            net = _policy_net_incremental(policy=policy, action=action, outcome=outcome)
            if action in _DETERMINATE_ACTIONS:
                net_population += 1
                if net is not None:
                    net_incremental.append(net)
            if action == "hold" and _known_thesis_or_willingness_deterioration(
                episode_by_id[episode_id]
            ):
                known_deterioration_hold_count += 1
                hold = _number(outcome.get("hold_to_horizon_incremental"))
                if hold is not None and hold < 0:
                    known_deterioration_adverse_losses.append(-hold)
        if action == "close":
            close_cost_population += 1
            transaction_cost = _decision_close_transaction_cost(
                episode_by_id[episode_id]
            )
            if transaction_cost is not None:
                close_costs.append(transaction_cost)
            best_hold = _best_hold_incremental(outcomes_by_episode.get(episode_id, []))
            if best_hold is not None:
                precision_population += 1
                if best_hold <= 0:
                    precision_hits += 1
            if policy == POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED:
                switch_population += 1
                cost = _switch_transaction_cost(
                    episode_by_id[episode_id],
                    outcome,
                )
                if cost is not None:
                    switch_costs.append(cost)
    return {
        "premature_close_regret_per_paired_episode": {
            "definition": (
                "max(hold minus recommended close/switch, 0) for close; "
                "zero for hold/review because no close is recommended"
            ),
            **_metric_summary(regret, population_count=len(episode_ids)),
        },
        "avoided_loss_benefit_per_paired_episode": {
            "definition": (
                "max(recommended close/switch minus hold, 0) for close; "
                "zero for hold/review because no close is recommended"
            ),
            **_metric_summary(benefit, population_count=len(episode_ids)),
        },
        "policy_net_incremental_for_determinate_actions": _metric_summary(
            net_incremental, population_count=net_population
        ),
        "close_transaction_cost": _cost_summary(
            close_costs, population_count=close_cost_population
        ),
        "switch_transaction_cost": _cost_summary(
            switch_costs, population_count=switch_population
        ),
        "close_precision": {
            "definition": (
                "close episodes whose best usable hold horizon does not beat "
                "close-now after costs / close episodes with a usable paired outcome"
            ),
            "actionable_close_episode_count": close_cost_population,
            "usable_paired_outcome_count": precision_population,
            "inconclusive_episode_count": close_cost_population - precision_population,
            "precise_close_episode_count": precision_hits,
            "value": _ratio(precision_hits, precision_population),
        },
        "false_urgency": {
            "definition": "actionable close later dominated by the best usable hold horizon",
            "usable_close_episode_count": precision_population,
            "inconclusive_close_episode_count": close_cost_population - precision_population,
            "false_urgency_episode_count": precision_population - precision_hits,
            "rate": _ratio(precision_population - precision_hits, precision_population),
        },
        "missed_review_diagnostic": {
            "status": "descriptive_no_approved_large_adverse_threshold",
            "known_deterioration_hold_episode_count": known_deterioration_hold_count,
            "adverse_outcome_distribution": _metric_summary(
                known_deterioration_adverse_losses,
                population_count=known_deterioration_hold_count,
            ),
        },
        "coverage": {
            "episode_count": len(episode_ids),
            "regret_benefit_usable_episode_count": paired_population,
            "regret_benefit_inconclusive_episode_count": len(episode_ids) - paired_population,
        },
    }


def _paired_policy_metrics(
    *,
    baseline_policy: str,
    proposed_policy: str,
    episode_ids: set[str],
    preferred_outcome: dict[str, dict[str, Any]],
    baseline_actions: dict[str, str],
    proposed_actions: dict[str, str],
) -> dict[str, Any]:
    baseline_regret: list[float] = []
    proposed_regret: list[float] = []
    regret_delta: list[float] = []
    baseline_benefit: list[float] = []
    proposed_benefit: list[float] = []
    benefit_delta: list[float] = []
    for episode_id in sorted(episode_ids):
        outcome = preferred_outcome.get(episode_id)
        if outcome is None:
            continue
        baseline = _regret_benefit(
            policy=baseline_policy,
            action=baseline_actions[episode_id],
            outcome=outcome,
        )
        proposed = _regret_benefit(
            policy=proposed_policy,
            action=proposed_actions[episode_id],
            outcome=outcome,
        )
        if baseline is None or proposed is None:
            continue
        baseline_regret.append(baseline[0])
        proposed_regret.append(proposed[0])
        regret_delta.append(proposed[0] - baseline[0])
        baseline_benefit.append(baseline[1])
        proposed_benefit.append(proposed[1])
        benefit_delta.append(proposed[1] - baseline[1])
    population = len(episode_ids)
    return {
        "measurement_basis": (
            "close-recommendation attribution only; review is not imputed as a trade"
        ),
        "coverage": {
            "paired_episode_count": len(regret_delta),
            "population_episode_count": population,
            "inconclusive_episode_count": population - len(regret_delta),
        },
        "premature_close_regret": {
            "baseline": _metric_summary(baseline_regret, population_count=population),
            "proposed": _metric_summary(proposed_regret, population_count=population),
            "paired_delta_proposed_minus_baseline": _metric_summary(
                regret_delta, population_count=population
            ),
        },
        "avoided_loss_benefit": {
            "baseline": _metric_summary(baseline_benefit, population_count=population),
            "proposed": _metric_summary(proposed_benefit, population_count=population),
            "paired_delta_proposed_minus_baseline": _metric_summary(
                benefit_delta, population_count=population
            ),
        },
    }


def _group_reports(
    episode_ids: set[str],
    *,
    key_fn,
    key_names: tuple[str, ...],
    episode_by_id: dict[str, dict[str, Any]],
    outcomes_by_episode: dict[str, list[dict[str, Any]]],
    evidence_outcomes_by_episode: dict[str, list[dict[str, Any]]],
    preferred_outcome: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for episode_id in episode_ids:
        key = tuple(text(item).lower() or "unknown" for item in key_fn(episode_by_id[episode_id]))
        grouped[key].add(episode_id)
    return [
        {
            **dict(zip(key_names, key, strict=True)),
            "report": _comparison_report(
                episode_ids=ids,
                episode_by_id=episode_by_id,
                outcomes_by_episode=outcomes_by_episode,
                evidence_outcomes_by_episode=evidence_outcomes_by_episode,
                preferred_outcome=preferred_outcome,
            ),
        }
        for key, ids in sorted(grouped.items())
    ]


def _path_risk(
    *,
    episode_ids: set[str],
    outcomes_by_episode: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    adverse_losses: list[float] = []
    for episode_id in episode_ids:
        values = [
            value
            for row in outcomes_by_episode.get(episode_id, [])
            if (value := _number(row.get("hold_to_horizon_incremental"))) is not None
        ]
        if values:
            adverse_losses.append(max(0.0, -min(values)))
    summary = _metric_summary(adverse_losses, population_count=len(episode_ids))
    return {
        "definition": "max(0, -minimum usable hold incremental) per episode",
        "maximum_adverse_excursion": max(adverse_losses) if adverse_losses else None,
        "p95_adverse_path": summary["p95"],
        "distribution": summary,
    }


def _terminal_alignment(
    *,
    episode_ids: set[str],
    outcomes_by_episode: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    terminal_count = 0
    for episode_id in episode_ids:
        terminals = [
            row
            for row in outcomes_by_episode.get(episode_id, [])
            if text(row.get("outcome_kind")).lower() == "terminal"
        ]
        if not terminals:
            continue
        terminal_count += 1
        counts[text(terminals[0].get("willingness_alignment")).lower() or "not_reported"] += 1
    return {
        "episode_count": len(episode_ids),
        "terminal_outcome_episode_count": terminal_count,
        "terminal_outcome_inconclusive_episode_count": len(episode_ids) - terminal_count,
        "by_alignment": dict(sorted(counts.items())),
    }


def _p3_switch_opportunity(
    *,
    episode_ids: set[str],
    episode_by_id: dict[str, dict[str, Any]],
    outcomes_by_episode: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    opportunity_ids = {
        episode_id
        for episode_id in episode_ids
        if _replacement_status(episode_by_id[episode_id]) == "review_switch"
    }
    replacement_incremental: list[float] = []
    switch_vs_close: list[float] = []
    switch_vs_hold: list[float] = []
    transaction_costs: list[float] = []
    inconclusive_reasons: Counter[str] = Counter()
    for episode_id in sorted(opportunity_ids):
        rows = outcomes_by_episode.get(episode_id, [])
        usable = [
            row
            for row in rows
            if text(row.get("replacement_outcome_status")).lower() == "usable"
        ]
        if not usable:
            reasons = {
                text(row.get("replacement_inconclusive_reason")).lower()
                for row in rows
                if text(row.get("replacement_inconclusive_reason"))
            }
            for reason in reasons or {"same_horizon_replacement_outcome_missing"}:
                inconclusive_reasons[reason] += 1
            continue
        selected = _preferred_evaluation_outcome(usable)
        for target, field in (
            (replacement_incremental, "replacement_incremental"),
            (switch_vs_close, "switch_vs_close_incremental"),
            (switch_vs_hold, "switch_vs_hold_incremental"),
        ):
            value = _number(selected.get(field))
            if value is not None:
                target.append(value)
        cost = _switch_transaction_cost(episode_by_id[episode_id], selected)
        if cost is not None:
            transaction_costs.append(cost)
    population = len(opportunity_ids)
    return {
        "status": "descriptive_manual_review_only",
        "opportunity_episode_count": population,
        "same_horizon_usable_episode_count": len(replacement_incremental),
        "same_horizon_inconclusive_episode_count": population - len(replacement_incremental),
        "inconclusive_reasons": dict(sorted(inconclusive_reasons.items())),
        "replacement_incremental": _metric_summary(
            replacement_incremental, population_count=population
        ),
        "switch_vs_close_incremental": _metric_summary(
            switch_vs_close, population_count=population
        ),
        "switch_vs_hold_incremental": _metric_summary(
            switch_vs_hold, population_count=population
        ),
        "switch_transaction_cost": _cost_summary(
            transaction_costs, population_count=population
        ),
    }


def _operational_metrics(
    *,
    episode_ids: set[str],
    episode_by_id: dict[str, dict[str, Any]],
    actions: dict[str, dict[str, str]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for policy, mapping in actions.items():
        by_lot: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for episode_id in episode_ids:
            episode = episode_by_id[episode_id]
            by_lot[_lot_key(episode)].append(
                (text(episode.get("observed_at_utc")), mapping[episode_id])
            )
        repeat_counts: list[int] = []
        transition_counts: Counter[str] = Counter()
        unchanged_actionable = 0
        actionable_lots = 0
        for rows in by_lot.values():
            ordered = sorted(rows)
            actionable = [state for _at, state in ordered if state in _ACTIONABLE_REMINDERS]
            if actionable:
                actionable_lots += 1
                repeat_counts.append(max(0, len(actionable) - 1))
            for (_left_at, left), (_right_at, right) in zip(ordered, ordered[1:]):
                if left == right and right in _ACTIONABLE_REMINDERS:
                    unchanged_actionable += 1
                elif left != right:
                    transition_counts[f"{left}->{right}"] += 1
        result[policy] = {
            "unique_lot_count": len(by_lot),
            "unique_actionable_lot_count": actionable_lots,
            "repeated_actionable_reminder_count": sum(repeat_counts),
            "repeated_actionable_per_notified_lot": _metric_summary(
                repeat_counts, population_count=actionable_lots
            ),
            "unchanged_actionable_transition_count": unchanged_actionable,
            "action_state_transitions": dict(sorted(transition_counts.items())),
        }
    return result


def _unique_lot_rollup(
    *,
    episode_ids: set[str],
    episode_by_id: dict[str, dict[str, Any]],
    preferred_outcome: dict[str, dict[str, Any]],
    p3_preferred_outcome: dict[str, dict[str, Any]],
    actions: dict[str, dict[str, str]],
) -> dict[str, Any]:
    earliest: dict[tuple[str, str], str] = {}
    for episode_id in episode_ids:
        episode = episode_by_id[episode_id]
        lot = _lot_key(episode)
        current = earliest.get(lot)
        if current is None or text(episode.get("observed_at_utc")) < text(
            episode_by_id[current].get("observed_at_utc")
        ):
            earliest[lot] = episode_id
    by_policy: dict[str, Any] = {}
    for policy in _POLICIES:
        values: list[float] = []
        determinate = 0
        for episode_id in earliest.values():
            action = actions[policy][episode_id]
            if action not in _DETERMINATE_ACTIONS:
                continue
            determinate += 1
            outcome = (
                p3_preferred_outcome.get(episode_id)
                if policy == POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED
                else preferred_outcome.get(episode_id)
            )
            if outcome is None:
                continue
            value = _policy_net_incremental(policy=policy, action=action, outcome=outcome)
            if value is not None:
                values.append(value)
        by_policy[policy] = _metric_summary(values, population_count=determinate)
    return {
        "rule": "earliest promotion-scope episode per (account, position_lot_id)",
        "unique_lot_count": len(earliest),
        "policy_net_incremental": by_policy,
    }


def _threshold_sensitivity(
    *,
    episode_ids: set[str],
    episode_by_id: dict[str, dict[str, Any]],
    preferred_outcome: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    scenarios = [
        ("strong_remaining_annualized_max", value, 0.0)
        for value in (0.03, 0.045, 0.06)
    ] + [
        ("medium_remaining_annualized_max", value, 0.0)
        for value in (0.05, 0.07, 0.09)
    ] + [
        ("capture_threshold_delta", None, value)
        for value in (-0.05, 0.0, 0.05)
    ]
    rows: list[dict[str, Any]] = []
    for dimension, value, capture_delta in scenarios:
        actions: dict[str, dict[str, str]] = {policy: {} for policy in _POLICIES}
        missing = 0
        for episode_id in episode_ids:
            projections = _sensitivity_actions(
                episode_by_id[episode_id],
                strong_max=(value if dimension == "strong_remaining_annualized_max" else 0.045),
                medium_max=(value if dimension == "medium_remaining_annualized_max" else 0.07),
                capture_delta=capture_delta,
            )
            if projections is None:
                missing += 1
                continue
            for policy, action in projections.items():
                actions[policy][episode_id] = action
        evaluable_ids = set(actions[POLICY_VARIANT_P0_CURRENT])
        paired = _paired_policy_metrics(
            baseline_policy=POLICY_VARIANT_P0_CURRENT,
            proposed_policy=POLICY_VARIANT_P2_PROFILE_AWARE,
            episode_ids=evaluable_ids,
            preferred_outcome=preferred_outcome,
            baseline_actions=actions[POLICY_VARIANT_P0_CURRENT],
            proposed_actions=actions[POLICY_VARIANT_P2_PROFILE_AWARE],
        )
        rows.append(
            {
                "dimension": dimension,
                "value": value if dimension != "capture_threshold_delta" else capture_delta,
                "one_factor_at_a_time": True,
                "input_coverage": {
                    "episode_count": len(episode_ids),
                    "evaluable_episode_count": len(evaluable_ids),
                    "inconclusive_episode_count": missing,
                },
                "action_counts": {
                    policy: dict(sorted(Counter(mapping.values()).items()))
                    for policy, mapping in actions.items()
                },
                "P2_vs_P0": paired,
            }
        )
    return {
        "status": "descriptive_only",
        "automatic_winner": None,
        "automatic_parameter_recommendation": None,
        "baseline": {
            "strong_remaining_annualized_max": 0.045,
            "medium_remaining_annualized_max": 0.07,
            "capture_rules": [
                {
                    "level": rule.level,
                    "min_dte": rule.min_dte,
                    "max_dte": rule.max_dte,
                    "min_capture": rule.min_capture,
                }
                for rule in DEFAULT_TIER_RULES
            ],
        },
        "scenario_count": len(rows),
        "scenarios": rows,
    }


def _sensitivity_actions(
    episode: dict[str, Any],
    *,
    strong_max: float,
    medium_max: float,
    capture_delta: float,
) -> dict[str, str] | None:
    facts_raw = episode.get("normalized_decision_facts")
    buckets = episode.get("threshold_inputs")
    identity = episode.get("position_identity")
    if not isinstance(facts_raw, dict) or not isinstance(buckets, dict) or not isinstance(identity, dict):
        return None
    if any(name not in facts_raw for name in _FACT_FIELDS if name != "combo_evidence_status"):
        return None
    try:
        facts = CloseDecisionFacts(**{name: facts_raw.get(name) for name in _FACT_FIELDS})
    except (TypeError, ValueError):
        return None
    if text(facts.side).lower() != "short" or text(facts.option_type).lower() not in {"put", "call"}:
        return None
    capture_ratio = _number(buckets.get("capture_ratio"))
    remaining = _number(buckets.get("remaining_annualized_return"))
    dte = _episode_dte(episode)
    if capture_ratio is None or dte is None:
        return None
    config = CloseAdviceConfig(
        strong_remaining_annualized_max=strong_max,
        medium_remaining_annualized_max=medium_max,
    )
    tier = _sensitivity_tier(
        capture_ratio=capture_ratio,
        dte=dte,
        remaining_annualized_return=remaining,
        config=config,
        capture_delta=capture_delta,
    )
    adjusted = replace(
        facts,
        tier=tier,
        exit_state="profit_capture" if tier != "none" else "hold",
    )
    p0 = evaluate_close_policy(adjusted, POLICY_VARIANT_P0_CURRENT)
    p1 = evaluate_close_policy(adjusted, POLICY_VARIANT_P1_SEMANTIC_SPLIT)
    p2 = evaluate_close_policy(adjusted, POLICY_VARIANT_P2_PROFILE_AWARE)
    replacement = episode.get("replacement_evidence")
    replacement = replacement if isinstance(replacement, dict) else {}
    p3 = compose_opportunity_required_policy(
        p2,
        ReallocationPolicyEvidence(
            status=text(replacement.get("status")) or "not_evaluable",
            reason=text(replacement.get("reason")),
        ),
    )
    return {
        policy: result.recommendation_state
        for policy, result in zip(_POLICIES, (p0, p1, p2, p3), strict=True)
    }


def _sensitivity_tier(
    *,
    capture_ratio: float,
    dte: int,
    remaining_annualized_return: float | None,
    config: CloseAdviceConfig,
    capture_delta: float,
) -> str:
    for rule in DEFAULT_TIER_RULES:
        adjusted = CloseAdviceTierRule(
            level=rule.level,
            reason=rule.reason,
            min_capture=max(0.0, min(1.0, rule.min_capture + capture_delta)),
            min_dte=rule.min_dte,
            max_dte=rule.max_dte,
            remaining_annualized_attr=rule.remaining_annualized_attr,
        )
        if adjusted.matches(
            capture_ratio=capture_ratio,
            dte=dte,
            remaining_annualized_return=remaining_annualized_return,
            config=config,
        ):
            return adjusted.level
    return "none"


def _regret_benefit(
    *,
    policy: str,
    action: str,
    outcome: dict[str, Any],
) -> tuple[float, float] | None:
    if action == "not_evaluable":
        return None
    if action != "close":
        return 0.0, 0.0
    if policy == POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED:
        advantage = _number(outcome.get("switch_vs_hold_incremental"))
        if advantage is None:
            return None
        return max(0.0, -advantage), max(0.0, advantage)
    hold = _number(outcome.get("hold_to_horizon_incremental"))
    if hold is None:
        return None
    return max(0.0, hold), max(0.0, -hold)


def _policy_net_incremental(
    *,
    policy: str,
    action: str,
    outcome: dict[str, Any],
) -> float | None:
    if action == "hold":
        return _number(outcome.get("hold_to_horizon_incremental"))
    if action != "close":
        return None
    if policy == POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED:
        return _number(outcome.get("replacement_incremental"))
    return 0.0


def _switch_transaction_cost(
    episode: dict[str, Any],
    outcome: dict[str, Any] | None,
) -> float | None:
    if not isinstance(outcome, dict):
        return None
    evidence = episode.get("replacement_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    values = (
        _decision_close_transaction_cost(episode),
        _number(evidence.get("open_fee")),
        _number(evidence.get("entry_slippage")),
        _number(outcome.get("replacement_future_close_fee")),
    )
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _best_hold_incremental(rows: Iterable[dict[str, Any]]) -> float | None:
    values = [
        value
        for row in rows
        if (value := _number(row.get("hold_to_horizon_incremental"))) is not None
    ]
    return max(values) if values else None


def _preferred_evaluation_outcome(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        rows,
        key=lambda row: (
            _OUTCOME_PRECEDENCE.get(text(row.get("outcome_kind")).lower(), 0),
            text(row.get("marked_at_utc") or row.get("lifecycle_at_utc")),
        ),
    )


def _usable_outcomes_by_episode(
    outcomes: list[dict[str, Any]],
    *,
    episode_by_id: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in outcomes:
        episode_id = text(row.get("episode_id"))
        kind = text(row.get("outcome_kind")).lower()
        if (
            episode_id not in episode_by_id
            or kind not in _OUTCOME_PRECEDENCE
            or text(row.get("evidence_status")).lower() != "usable"
        ):
            continue
        grouped[(episode_id, kind)].append(row)
    by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (episode_id, _kind), rows in grouped.items():
        by_episode[episode_id].append(
            max(
                rows,
                key=lambda row: text(
                    row.get("marked_at_utc") or row.get("lifecycle_at_utc")
                ),
            )
        )
    return dict(by_episode)


def _all_outcomes_by_episode(
    outcomes: list[dict[str, Any]],
    *,
    episode_by_id: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in outcomes:
        episode_id = text(row.get("episode_id"))
        if episode_id in episode_by_id:
            by_episode[episode_id].append(row)
    return dict(by_episode)


def _unique_episode_index(episodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for episode in episodes:
        episode_id = text(episode.get("episode_id"))
        if episode_id and episode_id not in out:
            out[episode_id] = episode
    return out


def _policy_action(episode: dict[str, Any], policy: str) -> str:
    projections = episode.get("shadow_policy_results")
    projections = projections if isinstance(projections, dict) else {}
    row = projections.get(policy)
    row = row if isinstance(row, dict) else {}
    return text(row.get("recommendation_state")).lower() or "not_evaluable"


def _segment(episode: dict[str, Any]) -> tuple[str, str]:
    facts = episode.get("normalized_decision_facts")
    facts = facts if isinstance(facts, dict) else {}
    return (
        text(facts.get("strategy_profile")).lower() or "unknown",
        text(facts.get("strategy_family")).lower() or "unknown",
    )


def _market(episode: dict[str, Any]) -> str:
    identity = episode.get("position_identity")
    identity = identity if isinstance(identity, dict) else {}
    return text(
        symbol_market(identity.get("symbol") or identity.get("contract_symbol"))
    ).upper() or "UNKNOWN"


def _lot_key(episode: dict[str, Any]) -> tuple[str, str]:
    return (
        text(episode.get("account")).lower() or "unknown",
        text(episode.get("position_lot_id")) or "unknown",
    )


def _decision_close_transaction_cost(episode: dict[str, Any]) -> float | None:
    economics = episode.get("decision_economics")
    economics = economics if isinstance(economics, dict) else {}
    fee = _number(economics.get("decision_close_fee"))
    slippage = _number(economics.get("decision_close_slippage"))
    if fee is None or slippage is None:
        return None
    return fee + slippage


def _replacement_status(episode: dict[str, Any]) -> str:
    evidence = episode.get("replacement_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    return text(evidence.get("status")).lower()


def _known_thesis_or_willingness_deterioration(episode: dict[str, Any]) -> bool:
    facts = episode.get("normalized_decision_facts")
    facts = facts if isinstance(facts, dict) else {}
    return (
        text(facts.get("thesis_status")).lower() == "observe"
        or facts.get("continued_willingness") is False
    )


def _episode_dte(episode: dict[str, Any]) -> int | None:
    buckets = episode.get("threshold_inputs")
    buckets = buckets if isinstance(buckets, dict) else {}
    dte = _number(buckets.get("dte"))
    if dte is None or dte < 0 or not dte.is_integer():
        return None
    return int(dte)


def _cost_summary(values: list[float], *, population_count: int) -> dict[str, Any]:
    return {
        "definition": "sum of explicitly captured fee and slippage components",
        "total": round(sum(values), 6) if values else 0.0,
        "per_episode": _metric_summary(values, population_count=population_count),
    }


def _metric_summary(values: list[float], *, population_count: int) -> dict[str, Any]:
    ordered = sorted(values)
    count = len(ordered)
    return {
        "population_count": population_count,
        "count": count,
        "inconclusive_count": max(0, population_count - count),
        "mean": round(mean(ordered), 6) if ordered else None,
        "median": round(median(ordered), 6) if ordered else None,
        "p5": _percentile(ordered, 0.05),
        "p95": _percentile(ordered, 0.95),
    }


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 6)
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(values[lower], 6)
    weight = position - lower
    return round(values[lower] * (1 - weight) + values[upper] * weight, 6)


def _number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator > 0 else None
