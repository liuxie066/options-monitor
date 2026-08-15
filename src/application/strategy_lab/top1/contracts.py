from __future__ import annotations

import math
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import NoReturn, cast

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine import (
    SELL_PUT_RANKING_CONTRACT_VERSION,
    SELL_PUT_RANKING_PROFILES,
)
from domain.domain.fee_calc import FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION
from src.application.opening_candidate_snapshot import (
    OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
)
from src.application.strategy_lab.top1.ranking import (
    RANKING_PROJECTION_SCHEMA_VERSION,
)


EXPERIMENT_SPEC_SCHEMA_VERSION = "sell_put_top1_experiment_spec.v1"
BEHAVIOR_BINDING_SCHEMA_VERSION = "sell_put_top1_behavior_binding.v1"
ACCEPTED_SET_CONTRACT_VERSION = "same_point_producer_accepted_set.v1"
RESEARCH_SELECTION_CONTRACT_VERSION = "sell_put_top1_research_selection.v1"
RESEARCH_METRIC_CONTRACT_VERSION = "counterfactual_expiry_efficiency.v1"
VALIDATION_FILL_CONTRACT_VERSION = "scheduled_point_first_observed_cross.v1"
VALIDATION_METRIC_CONTRACT_VERSION = "sell_put_top1_paired_daily_efficiency.v1"
EXPIRY_OUTCOME_CONTRACT_VERSION = "expiry_outcome_at_underlier_close.v1"

_HASH_64 = re.compile(r"[0-9a-f]{64}\Z")
_BEHAVIOR_KEYS = frozenset(
    {
        "baseline_version",
        "opening_snapshot_schema_version",
        "accepted_set_contract_version",
        "ranking_projection_schema_version",
        "sell_put_ranking_contract_version",
        "research_selection_contract_version",
        "research_metric_contract_version",
        "validation_fill_contract_version",
        "validation_metric_contract_version",
        "fee_schedule_version",
        "market_calendar_version",
        "expiry_outcome_contract_version",
    }
)
_RESEARCH_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "topic_id",
        "experiment_id",
        "market",
        "account",
        "hypothesis",
        "baseline",
        "research_source",
        "research_evaluation",
        "variants",
        "frozen_safety",
        "economics_contracts",
        "expiry_outcome",
    }
)
_VALIDATION_ONLY_KEYS = frozenset(
    {
        "validation_evaluation",
        "fill_observation",
        "timer_binding",
        "validation_metrics",
    }
)
_RESEARCH_HASH_KEYS = (
    "schema_version",
    "hypothesis",
    "baseline",
    "research_source",
    "research_evaluation",
    "variants",
    "frozen_safety",
    "economics_contracts",
    "expiry_outcome",
)


class Top1CoreContractError(ValueError):
    """Stable fail-closed ExperimentSpec error."""

    reason_code: str

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason_code = "experiment_spec_invalid"


def _fail(message: str) -> NoReturn:
    raise Top1CoreContractError(message)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be a mapping")
    raw_mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw_mapping):
        _fail(f"{label} keys must be strings")
    return cast(Mapping[str, object], raw_mapping)


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    if set(value) != set(expected):
        _fail(f"{label} keys are incomplete or unexpected")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(f"{label} must be non-empty canonical text")
    return value


def _fixed(value: object, expected: object, label: str) -> None:
    if type(value) is not type(expected) or value != expected:
        _fail(f"{label} must equal {expected!r}")


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(f"{label} must be a positive integer")
    return value


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        _fail(f"{label} must be finite")
    return number


def _sha256(value: object, label: str) -> str:
    text = _text(value, label)
    if _HASH_64.fullmatch(text) is None:
        _fail(f"{label} must be a lowercase SHA-256")
    return text


def _iso_date(value: object, label: str) -> date:
    text = _text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        _fail(f"{label} must be an ISO date")
    if parsed.isoformat() != text:
        _fail(f"{label} must be a canonical ISO date")
    return parsed


def _utc_timestamp(value: object, label: str) -> str:
    text = _text(value, label)
    if not text.endswith("Z") or "T" not in text:
        _fail(f"{label} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(f"{text[:-1]}+00:00")
    except ValueError:
        _fail(f"{label} must be an ISO-8601 UTC timestamp")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail(f"{label} must be UTC")
    return text


def _relative_posix_path(value: object, label: str) -> str:
    text = _text(value, label)
    parts = text.split("/")
    if text.startswith("/") or "\\" in text or any(
        part in {"", ".", ".."} for part in parts
    ):
        _fail(f"{label} must be a safe relative POSIX path")
    return text


def build_behavior_binding(contract_versions: object) -> str:
    versions = _mapping(contract_versions, "contract_versions")
    _exact_keys(versions, _BEHAVIOR_KEYS, "contract_versions")
    payload: dict[str, str] = {"schema_version": BEHAVIOR_BINDING_SCHEMA_VERSION}
    for key in _BEHAVIOR_KEYS:
        payload[key] = _text(versions[key], f"contract_versions.{key}")
    return canonical_sha256(payload)


def _current_behavior_versions(spec: Mapping[str, object]) -> dict[str, str]:
    baseline = _mapping(spec["baseline"], "baseline")
    economics = _mapping(spec["economics_contracts"], "economics_contracts")
    return {
        "baseline_version": _text(baseline["version"], "baseline.version"),
        "opening_snapshot_schema_version": OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
        "accepted_set_contract_version": ACCEPTED_SET_CONTRACT_VERSION,
        "ranking_projection_schema_version": RANKING_PROJECTION_SCHEMA_VERSION,
        "sell_put_ranking_contract_version": SELL_PUT_RANKING_CONTRACT_VERSION,
        "research_selection_contract_version": RESEARCH_SELECTION_CONTRACT_VERSION,
        "research_metric_contract_version": RESEARCH_METRIC_CONTRACT_VERSION,
        "validation_fill_contract_version": VALIDATION_FILL_CONTRACT_VERSION,
        "validation_metric_contract_version": VALIDATION_METRIC_CONTRACT_VERSION,
        "fee_schedule_version": FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION,
        "market_calendar_version": _text(
            economics["market_calendar_version"],
            "economics_contracts.market_calendar_version",
        ),
        "expiry_outcome_contract_version": EXPIRY_OUTCOME_CONTRACT_VERSION,
    }


def _validate_hypothesis(value: object) -> None:
    item = _mapping(value, "hypothesis")
    _exact_keys(
        item,
        frozenset(
            {
                "hypothesis_type",
                "statement",
                "mechanism",
                "independent_variable",
                "expected_direction",
            }
        ),
        "hypothesis",
    )
    _fixed(item["hypothesis_type"], "sell_put_ranking", "hypothesis.hypothesis_type")
    _ = _text(item["statement"], "hypothesis.statement")
    _ = _text(item["mechanism"], "hypothesis.mechanism")
    _fixed(
        item["independent_variable"],
        "cross_symbol_concentration_priority",
        "hypothesis.independent_variable",
    )
    _fixed(
        item["expected_direction"],
        "higher_top1_efficiency_without_higher_concentration",
        "hypothesis.expected_direction",
    )


def _validate_baseline(value: object, spec: Mapping[str, object]) -> None:
    item = _mapping(value, "baseline")
    _exact_keys(
        item,
        frozenset(
            {
                "version",
                "opening_snapshot_schema",
                "accepted_set_contract_version",
                "ranking_projection_schema_version",
                "sell_put_ranking_contract_version",
                "behavior_binding_sha256",
            }
        ),
        "baseline",
    )
    _ = _text(item["version"], "baseline.version")
    _fixed(
        item["opening_snapshot_schema"],
        OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
        "baseline.opening_snapshot_schema",
    )
    _fixed(
        item["accepted_set_contract_version"],
        ACCEPTED_SET_CONTRACT_VERSION,
        "baseline.accepted_set_contract_version",
    )
    _fixed(
        item["ranking_projection_schema_version"],
        RANKING_PROJECTION_SCHEMA_VERSION,
        "baseline.ranking_projection_schema_version",
    )
    _fixed(
        item["sell_put_ranking_contract_version"],
        SELL_PUT_RANKING_CONTRACT_VERSION,
        "baseline.sell_put_ranking_contract_version",
    )
    supplied = _sha256(item["behavior_binding_sha256"], "baseline.behavior_binding_sha256")
    if supplied != build_behavior_binding(_current_behavior_versions(spec)):
        _fail("baseline.behavior_binding_sha256 does not match current contracts")


def _validate_research_source(value: object) -> None:
    item = _mapping(value, "research_source")
    _exact_keys(
        item,
        frozenset(
            {
                "mode",
                "dataset_ref",
                "dataset_sha256",
                "research_cutoff_at",
                "start_trading_date",
                "end_trading_date",
            }
        ),
        "research_source",
    )
    _fixed(item["mode"], "sealed_historical_dataset", "research_source.mode")
    _ = _relative_posix_path(item["dataset_ref"], "research_source.dataset_ref")
    _ = _sha256(item["dataset_sha256"], "research_source.dataset_sha256")
    _ = _utc_timestamp(item["research_cutoff_at"], "research_source.research_cutoff_at")
    start = _iso_date(item["start_trading_date"], "research_source.start_trading_date")
    end = _iso_date(item["end_trading_date"], "research_source.end_trading_date")
    if start > end:
        _fail("research_source trading-date range is reversed")


def _validate_research_evaluation(value: object) -> None:
    item = _mapping(value, "research_evaluation")
    _exact_keys(
        item,
        frozenset(
            {
                "contract_version",
                "metric_contract_version",
                "fill_assumption",
                "required_days",
                "window_mode",
                "visibility",
            }
        ),
        "research_evaluation",
    )
    _fixed(
        item["contract_version"],
        RESEARCH_SELECTION_CONTRACT_VERSION,
        "research_evaluation.contract_version",
    )
    _fixed(
        item["metric_contract_version"],
        RESEARCH_METRIC_CONTRACT_VERSION,
        "research_evaluation.metric_contract_version",
    )
    _fixed(item["fill_assumption"], "t0_sell_limit", "research_evaluation.fill_assumption")
    _fixed(item["required_days"], 40, "research_evaluation.required_days")
    _fixed(
        item["window_mode"],
        "fixed_consecutive_trading_days",
        "research_evaluation.window_mode",
    )
    _fixed(
        item["visibility"],
        "visible_after_research_seal",
        "research_evaluation.visibility",
    )


def _validate_variants(value: object) -> None:
    if not isinstance(value, list):
        _fail("variants must be a list")
    variants = cast(list[object], value)
    if len(variants) < 2:
        _fail("variants must contain baseline and at least one level")
    baseline = _mapping(variants[0], "variants[0]")
    _exact_keys(baseline, frozenset({"variant_id", "patch"}), "variants[0]")
    if baseline["variant_id"] != "baseline" or baseline["patch"] != {}:
        _fail("variants must begin with the exact baseline arm")

    variant_ids = {"baseline"}
    profiles: set[str] = set()
    for index, raw in enumerate(variants[1:], start=1):
        item = _mapping(raw, f"variants[{index}]")
        _exact_keys(item, frozenset({"variant_id", "patch"}), f"variants[{index}]")
        variant_id = _text(item["variant_id"], f"variants[{index}].variant_id")
        if variant_id in variant_ids:
            _fail("variant IDs must be unique")
        variant_ids.add(variant_id)
        patch = _mapping(item["patch"], f"variants[{index}].patch")
        _exact_keys(patch, frozenset({"ranking_profile"}), f"variants[{index}].patch")
        profile = _text(
            patch["ranking_profile"],
            f"variants[{index}].patch.ranking_profile",
        )
        if profile not in SELL_PUT_RANKING_PROFILES:
            _fail("variant ranking profile is unsupported")
        if profile in profiles:
            _fail("variant ranking profiles must be unique")
        profiles.add(profile)


def _validate_frozen_safety(value: object) -> None:
    item = _mapping(value, "frozen_safety")
    _exact_keys(
        item,
        frozenset({"mode", "variant_may_change_acceptance"}),
        "frozen_safety",
    )
    _fixed(
        item["mode"],
        "inherit_each_point_producer_accepted_set",
        "frozen_safety.mode",
    )
    _fixed(
        item["variant_may_change_acceptance"],
        False,
        "frozen_safety.variant_may_change_acceptance",
    )


def _validate_economics_contracts(value: object) -> None:
    item = _mapping(value, "economics_contracts")
    _exact_keys(
        item,
        frozenset({"fee_schedule_version", "market_calendar_version"}),
        "economics_contracts",
    )
    _fixed(
        item["fee_schedule_version"],
        FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION,
        "economics_contracts.fee_schedule_version",
    )
    _ = _text(
        item["market_calendar_version"],
        "economics_contracts.market_calendar_version",
    )


def _validate_expiry_outcome(value: object) -> None:
    item = _mapping(value, "expiry_outcome")
    _exact_keys(
        item,
        frozenset(
            {
                "contract_version",
                "spot_source",
                "ktype",
                "autype",
                "price_field",
                "due_boundary",
                "pending_elapsed_hours",
            }
        ),
        "expiry_outcome",
    )
    expected = {
        "contract_version": EXPIRY_OUTCOME_CONTRACT_VERSION,
        "spot_source": "opend_history_kline",
        "ktype": "K_DAY",
        "autype": "NONE",
        "price_field": "close",
        "due_boundary": "expiration_observation_start_ms",
        "pending_elapsed_hours": 72,
    }
    for key, fixed_value in expected.items():
        _fixed(item[key], fixed_value, f"expiry_outcome.{key}")


def _validate_validation_fields(spec: Mapping[str, object]) -> None:
    evaluation = _mapping(spec["validation_evaluation"], "validation_evaluation")
    _exact_keys(
        evaluation,
        frozenset({"required_days", "window_mode", "visibility"}),
        "validation_evaluation",
    )
    _fixed(evaluation["required_days"], 20, "validation_evaluation.required_days")
    _fixed(
        evaluation["window_mode"],
        "fixed_future_consecutive_trading_days",
        "validation_evaluation.window_mode",
    )
    _fixed(
        evaluation["visibility"],
        "hidden_until_final_seal",
        "validation_evaluation.visibility",
    )

    fill = _mapping(spec["fill_observation"], "fill_observation")
    _exact_keys(fill, frozenset({"applies_to", "contract_version"}), "fill_observation")
    _fixed(fill["applies_to"], "validation_only", "fill_observation.applies_to")
    _fixed(
        fill["contract_version"],
        VALIDATION_FILL_CONTRACT_VERSION,
        "fill_observation.contract_version",
    )

    timer = _mapping(spec["timer_binding"], "timer_binding")
    timer_keys = frozenset(
        {
            "revision",
            "producer_catchup_grace_seconds",
            "producer_run_timeout_upper_bound_seconds",
            "advance_cadence_seconds",
            "fill_observation_duration_upper_bound_seconds",
            "terms_capture_duration_upper_bound_seconds",
        }
    )
    _exact_keys(timer, timer_keys, "timer_binding")
    _ = _text(timer["revision"], "timer_binding.revision")
    for key in timer_keys - {"revision"}:
        _ = _positive_int(timer[key], f"timer_binding.{key}")

    metrics = _mapping(spec["validation_metrics"], "validation_metrics")
    _exact_keys(
        metrics,
        frozenset({"contract_version", "confidence_level", "worst_fraction"}),
        "validation_metrics",
    )
    _fixed(
        metrics["contract_version"],
        VALIDATION_METRIC_CONTRACT_VERSION,
        "validation_metrics.contract_version",
    )
    if _finite_number(metrics["confidence_level"], "validation_metrics.confidence_level") != 0.95:
        _fail("validation_metrics.confidence_level must equal 0.95")
    if _finite_number(metrics["worst_fraction"], "validation_metrics.worst_fraction") != 0.20:
        _fail("validation_metrics.worst_fraction must equal 0.20")


def validate_experiment_spec(payload: object) -> dict[str, object]:
    raw = _mapping(payload, "ExperimentSpec")
    keys = set(raw)
    research_keys = set(_RESEARCH_TOP_LEVEL_KEYS)
    validation_keys = research_keys | set(_VALIDATION_ONLY_KEYS)
    if keys not in (research_keys, validation_keys):
        _fail("ExperimentSpec keys are incomplete or unexpected")
    spec = deepcopy(dict(raw))

    _fixed(spec["schema_version"], EXPERIMENT_SPEC_SCHEMA_VERSION, "schema_version")
    _ = _text(spec["topic_id"], "topic_id")
    _ = _text(spec["experiment_id"], "experiment_id")
    _fixed(spec["market"], "HK", "market")
    account = _text(spec["account"], "account")
    if account != account.lower():
        _fail("account must be lowercase canonical text")

    _validate_hypothesis(spec["hypothesis"])
    _validate_research_source(spec["research_source"])
    _validate_research_evaluation(spec["research_evaluation"])
    _validate_variants(spec["variants"])
    _validate_frozen_safety(spec["frozen_safety"])
    _validate_economics_contracts(spec["economics_contracts"])
    _validate_expiry_outcome(spec["expiry_outcome"])
    _validate_baseline(spec["baseline"], spec)
    if keys == validation_keys:
        _validate_validation_fields(spec)
    return spec


def build_research_spec_sha256(validated_spec: object) -> str:
    spec = validate_experiment_spec(validated_spec)
    return canonical_sha256({key: spec[key] for key in _RESEARCH_HASH_KEYS})


def build_validation_spec_sha256(
    validated_spec: object,
    *,
    research_terminal_sha256: str,
    challenger_variant_id: str,
    hidden_window_commitment_sha256: str,
) -> str:
    spec = validate_experiment_spec(validated_spec)
    if not _VALIDATION_ONLY_KEYS.issubset(spec):
        _fail("validation-ready ExperimentSpec is required")
    research_terminal = _sha256(research_terminal_sha256, "research_terminal_sha256")
    hidden_commitment = _sha256(
        hidden_window_commitment_sha256,
        "hidden_window_commitment_sha256",
    )
    challenger = _text(challenger_variant_id, "challenger_variant_id")
    variants = cast(list[object], spec["variants"])
    valid_challengers: set[str] = set()
    for index, raw_variant in enumerate(variants):
        item = _mapping(raw_variant, f"variants[{index}]")
        variant_id = _text(item["variant_id"], f"variants[{index}].variant_id")
        if variant_id != "baseline":
            valid_challengers.add(variant_id)
    if challenger not in valid_challengers:
        _fail("challenger_variant_id must name a non-baseline variant")
    return canonical_sha256(
        {
            "schema_version": spec["schema_version"],
            "research_terminal_sha256": research_terminal,
            "challenger_variant_id": challenger,
            "hidden_window_commitment_sha256": hidden_commitment,
            "validation_evaluation": spec["validation_evaluation"],
            "fill_observation": spec["fill_observation"],
            "economics_contracts": spec["economics_contracts"],
            "timer_binding": spec["timer_binding"],
            "expiry_outcome": spec["expiry_outcome"],
            "validation_metrics": spec["validation_metrics"],
        }
    )
