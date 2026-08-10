from __future__ import annotations

"""Offline replay lifecycle for Research / Shadow Replay side-lane evidence."""

from src.application.shadow_replay.evidence import (
    analyze_shadow_replay_dataset,
    build_shadow_replay_dataset,
    collect_shadow_replay_marks,
    load_shadow_replay_observed_evidence,
    mark_shadow_replay_dataset,
    run_shadow_replay_candidate_impact,
    run_shadow_replay_data_plan,
    settle_shadow_replay_dataset,
    shadow_replay_dataset_status,
    summarize_shadow_replay_readiness,
)
from src.application.shadow_replay.combo_capture import capture_combo_variants
from src.application.shadow_replay.combo_evaluation import (
    evaluate_combo_variant_dataset,
    evaluate_combo_variant_pairs,
)
from src.application.shadow_replay.combo_funding import prepare_combo_funding_puts
from src.application.shadow_replay.combo_settlement import (
    build_combo_variant_scorecards,
    settle_combo_pair_dataset,
    settle_combo_pair_outcomes,
)
from src.application.shadow_replay.combo_variants import (
    attach_funding_put_rank_provenance,
    build_combo_pair_decisions,
    load_combo_pair_facet,
    load_combo_variant_spec,
    publish_combo_pair_facet,
)
from src.application.shadow_replay.opening_policy import (
    OPENING_POLICY_SHADOW_SCHEMA,
    compare_opening_policy_shadow,
)

__all__ = [
    "analyze_shadow_replay_dataset",
    "attach_funding_put_rank_provenance",
    "build_combo_pair_decisions",
    "build_combo_variant_scorecards",
    "build_shadow_replay_dataset",
    "capture_combo_variants",
    "evaluate_combo_variant_pairs",
    "evaluate_combo_variant_dataset",
    "prepare_combo_funding_puts",
    "collect_shadow_replay_marks",
    "load_shadow_replay_observed_evidence",
    "load_combo_pair_facet",
    "load_combo_variant_spec",
    "mark_shadow_replay_dataset",
    "OPENING_POLICY_SHADOW_SCHEMA",
    "compare_opening_policy_shadow",
    "publish_combo_pair_facet",
    "run_shadow_replay_candidate_impact",
    "run_shadow_replay_data_plan",
    "settle_shadow_replay_dataset",
    "settle_combo_pair_dataset",
    "settle_combo_pair_outcomes",
    "shadow_replay_dataset_status",
    "summarize_shadow_replay_readiness",
]
