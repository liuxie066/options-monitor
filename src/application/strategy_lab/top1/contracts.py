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
)
from domain.domain.fee_calc import FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION
from src.application.opening_candidate_snapshot import (
    OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
)
from src.application.strategy_lab.top1.ranking import (
    RANKING_PROJECTION_SCHEMA_V3,
    RANKING_PROJECTION_SCHEMA_V2,
    RECIPE_ID,
    RECIPE_VERSION,
)
from src.application.strategy_lab.top1.economics import (
    SELL_PUT_TOP1_ECONOMIC_RESULT_VERSION,
)
from src.application.strategy_lab.top1.statistics import (
    TOP1_PAIRED_EVALUATION_VERSION,
)
from domain.domain.short_vol_assessment import (
    OPTION_MARKET_CONCENTRATION_METRIC_VERSION,
)


EXPERIMENT_SPEC_SCHEMA_VERSION = "sell_put_top1_experiment_spec.v2"
BEHAVIOR_BINDING_SCHEMA_VERSION = "sell_put_top1_behavior_binding.v2"
ACCEPTED_SET_CONTRACT_VERSION = "same_point_producer_accepted_set.v1"
RESEARCH_SELECTION_CONTRACT_VERSION = "sell_put_top1_research_selection.v2"
RESEARCH_METRIC_CONTRACT_VERSION = SELL_PUT_TOP1_ECONOMIC_RESULT_VERSION
VALIDATION_FILL_CONTRACT_VERSION = "scheduled_point_first_observed_cross.v1"
VALIDATION_METRIC_CONTRACT_VERSION = TOP1_PAIRED_EVALUATION_VERSION
EXPIRY_OUTCOME_CONTRACT_VERSION = "expiry_outcome_at_underlier_close.v2"
EVIDENCE_SELECTION_CONTRACT_VERSION = "performance_evidence_selection.v1"
SEALED_HISTORICAL_DATASET_SCHEMA = "sealed_historical_dataset.v1"
RECOMMENDATION_POINT_SELECTOR = "official_scheduled_sell_put.v1"
RESEARCH_REQUIRED_DAYS = 20
VALIDATION_REQUIRED_DAYS = 10
PREVIEW_SCHEMA_VERSION = "sell_put_top1_preview.v1"
CONFIRMED_START_COMMAND_SCHEMA_VERSION = "sell_put_top1_confirmed_start.v1"

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
        "economic_result_contract_version",
        "evaluation_contract_version",
        "option_market_concentration_metric_version",
        "evidence_selection_contract_version",
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
        "recipe",
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
    "recipe",
    "baseline",
    "research_source",
    "research_evaluation",
    "variants",
    "frozen_safety",
    "economics_contracts",
    "expiry_outcome",
)
_CONFIRMED_START_KEYS = frozenset(
    {
        "schema_version",
        "stage",
        "market",
        "account",
        "experiment_id",
        "confirmed_preview_sha256",
        "idempotency_key",
        "actor",
        "confirmed_at_utc",
    }
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
        "ranking_projection_schema_version": _text(
            baseline["ranking_projection_schema_version"],
            "baseline.ranking_projection_schema_version",
        ),
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
        "economic_result_contract_version": SELL_PUT_TOP1_ECONOMIC_RESULT_VERSION,
        "evaluation_contract_version": TOP1_PAIRED_EVALUATION_VERSION,
        "option_market_concentration_metric_version": (
            OPTION_MARKET_CONCENTRATION_METRIC_VERSION
        ),
        "evidence_selection_contract_version": EVIDENCE_SELECTION_CONTRACT_VERSION,
    }


def build_current_behavior_binding(payload: object) -> str:
    """Calculate the installed binding without accepting a stored baseline hash."""

    spec = _mapping(payload, "ExperimentSpec")
    return build_behavior_binding(_current_behavior_versions(spec))


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
        "option_market_concentration_near_return_threshold",
        "hypothesis.independent_variable",
    )
    _fixed(
        item["expected_direction"],
        "higher_annualized_return_without_lower_cny_pnl",
        "hypothesis.expected_direction",
    )


def _validate_recipe(value: object) -> None:
    item = _mapping(value, "recipe")
    _exact_keys(item, frozenset({"recipe_id", "recipe_version"}), "recipe")
    _fixed(item["recipe_id"], RECIPE_ID, "recipe.recipe_id")
    _fixed(item["recipe_version"], RECIPE_VERSION, "recipe.recipe_version")


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
                "ranking_profile",
                "near_return_threshold",
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
    if item["ranking_projection_schema_version"] not in {
        RANKING_PROJECTION_SCHEMA_V2,
        RANKING_PROJECTION_SCHEMA_V3,
    }:
        _fail("baseline.ranking_projection_schema_version is unsupported")
    _fixed(
        item["sell_put_ranking_contract_version"],
        SELL_PUT_RANKING_CONTRACT_VERSION,
        "baseline.sell_put_ranking_contract_version",
    )
    _fixed(item["ranking_profile"], "current_tie_break", "baseline.ranking_profile")
    if _finite_number(
        item["near_return_threshold"],
        "baseline.near_return_threshold",
    ) != 0.002:
        _fail("baseline.near_return_threshold must equal 0.002")
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
    mode = _text(item["mode"], "research_source.mode")
    if mode != "sealed_historical_dataset":
        _fail("research_source.mode is unsupported")
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
                "evaluation_contract_version",
                "fill_assumption",
                "required_days",
                "window_mode",
                "visibility",
                "confidence_level",
                "worst_fraction",
                "minimum_mean_daily_pnl_delta_cny",
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
    _fixed(
        item["evaluation_contract_version"],
        TOP1_PAIRED_EVALUATION_VERSION,
        "research_evaluation.evaluation_contract_version",
    )
    _fixed(item["fill_assumption"], "t0_sell_limit", "research_evaluation.fill_assumption")
    _fixed(
        item["required_days"],
        RESEARCH_REQUIRED_DAYS,
        "research_evaluation.required_days",
    )
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
    if _finite_number(
        item["confidence_level"],
        "research_evaluation.confidence_level",
    ) != 0.95:
        _fail("research_evaluation.confidence_level must equal 0.95")
    if _finite_number(
        item["worst_fraction"],
        "research_evaluation.worst_fraction",
    ) != 0.20:
        _fail("research_evaluation.worst_fraction must equal 0.20")
    if _finite_number(
        item["minimum_mean_daily_pnl_delta_cny"],
        "research_evaluation.minimum_mean_daily_pnl_delta_cny",
    ) != 0.0:
        _fail("research_evaluation minimum CNY PnL delta must equal zero")


def _validate_variants(value: object) -> None:
    if not isinstance(value, list):
        _fail("variants must be a list")
    variants = cast(list[object], value)
    if len(variants) != 4:
        _fail("variants must contain the fixed baseline and three challengers")
    baseline = _mapping(variants[0], "variants[0]")
    _exact_keys(baseline, frozenset({"variant_id", "patch"}), "variants[0]")
    if baseline["variant_id"] != "baseline" or baseline["patch"] != {}:
        _fail("variants must begin with the exact baseline arm")

    expected = (
        ("concentration-0.002", 0.002),
        ("concentration-0.004", 0.004),
        ("concentration-0.006", 0.006),
    )
    for index, (raw, expected_variant) in enumerate(
        zip(variants[1:], expected, strict=True),
        start=1,
    ):
        item = _mapping(raw, f"variants[{index}]")
        _exact_keys(item, frozenset({"variant_id", "patch"}), f"variants[{index}]")
        variant_id = _text(item["variant_id"], f"variants[{index}].variant_id")
        if variant_id != expected_variant[0]:
            _fail("variant IDs must match the fixed recipe")
        patch = _mapping(item["patch"], f"variants[{index}].patch")
        _exact_keys(
            patch,
            frozenset({"ranking_profile", "near_return_threshold"}),
            f"variants[{index}].patch",
        )
        _fixed(
            patch["ranking_profile"],
            "option_market_concentration",
            f"variants[{index}].patch.ranking_profile",
        )
        if _finite_number(
            patch["near_return_threshold"],
            f"variants[{index}].patch.near_return_threshold",
        ) != expected_variant[1]:
            _fail("variant near-return threshold does not match the fixed recipe")


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
        frozenset(
            {
                "fee_schedule_version",
                "market_calendar_version",
                "comparison_currency",
                "contract_quantity",
                "option_market_concentration_metric_version",
                "evidence_selection_contract_version",
                "economic_result_contract_version",
                "return_capital_basis",
            }
        ),
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
    _fixed(
        item["comparison_currency"],
        "CNY",
        "economics_contracts.comparison_currency",
    )
    _fixed(
        item["contract_quantity"],
        1,
        "economics_contracts.contract_quantity",
    )
    _fixed(
        item["option_market_concentration_metric_version"],
        OPTION_MARKET_CONCENTRATION_METRIC_VERSION,
        "economics_contracts.option_market_concentration_metric_version",
    )
    _fixed(
        item["evidence_selection_contract_version"],
        EVIDENCE_SELECTION_CONTRACT_VERSION,
        "economics_contracts.evidence_selection_contract_version",
    )
    _fixed(
        item["economic_result_contract_version"],
        SELL_PUT_TOP1_ECONOMIC_RESULT_VERSION,
        "economics_contracts.economic_result_contract_version",
    )
    _fixed(
        item["return_capital_basis"],
        "strike_x_multiplier_minus_opening_net_premium",
        "economics_contracts.return_capital_basis",
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
    _fixed(
        evaluation["required_days"],
        VALIDATION_REQUIRED_DAYS,
        "validation_evaluation.required_days",
    )
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
    _validate_recipe(spec["recipe"])
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


def build_sell_put_top1_research_spec(
    *,
    topic_id: str,
    experiment_id: str,
    research_source: Mapping[str, object],
    market_calendar_version: str,
    baseline_version: str = "sell_put_top1_current.v1",
    ranking_projection_schema_version: str = RANKING_PROJECTION_SCHEMA_V3,
) -> dict[str, object]:
    if ranking_projection_schema_version not in {
        RANKING_PROJECTION_SCHEMA_V2,
        RANKING_PROJECTION_SCHEMA_V3,
    }:
        _fail("ranking_projection_schema_version is unsupported")
    spec: dict[str, object] = {
        "schema_version": EXPERIMENT_SPEC_SCHEMA_VERSION,
        "topic_id": _text(topic_id, "topic_id"),
        "experiment_id": _text(experiment_id, "experiment_id"),
        "market": "HK",
        "account": "lx",
        "hypothesis": {
            "hypothesis_type": "sell_put_ranking",
            "statement": (
                "Prefer lower option-market concentration within a frozen "
                "near-return threshold."
            ),
            "mechanism": (
                "Re-rank the same accepted Sell Put candidates by frozen option "
                "market concentration."
            ),
            "independent_variable": (
                "option_market_concentration_near_return_threshold"
            ),
            "expected_direction": (
                "higher_annualized_return_without_lower_cny_pnl"
            ),
        },
        "recipe": {"recipe_id": RECIPE_ID, "recipe_version": RECIPE_VERSION},
        "baseline": {
            "version": _text(baseline_version, "baseline_version"),
            "opening_snapshot_schema": OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
            "accepted_set_contract_version": ACCEPTED_SET_CONTRACT_VERSION,
            "ranking_projection_schema_version": ranking_projection_schema_version,
            "sell_put_ranking_contract_version": SELL_PUT_RANKING_CONTRACT_VERSION,
            "ranking_profile": "current_tie_break",
            "near_return_threshold": 0.002,
            "behavior_binding_sha256": "0" * 64,
        },
        "research_source": deepcopy(dict(research_source)),
        "research_evaluation": {
            "contract_version": RESEARCH_SELECTION_CONTRACT_VERSION,
            "metric_contract_version": RESEARCH_METRIC_CONTRACT_VERSION,
            "evaluation_contract_version": TOP1_PAIRED_EVALUATION_VERSION,
            "fill_assumption": "t0_sell_limit",
            "required_days": RESEARCH_REQUIRED_DAYS,
            "window_mode": "fixed_consecutive_trading_days",
            "visibility": "visible_after_research_seal",
            "confidence_level": 0.95,
            "worst_fraction": 0.20,
            "minimum_mean_daily_pnl_delta_cny": 0.0,
        },
        "variants": [
            {"variant_id": "baseline", "patch": {}},
            *[
                {
                    "variant_id": f"concentration-{threshold:.3f}",
                    "patch": {
                        "ranking_profile": "option_market_concentration",
                        "near_return_threshold": threshold,
                    },
                }
                for threshold in (0.002, 0.004, 0.006)
            ],
        ],
        "frozen_safety": {
            "mode": "inherit_each_point_producer_accepted_set",
            "variant_may_change_acceptance": False,
        },
        "economics_contracts": {
            "fee_schedule_version": FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION,
            "market_calendar_version": _text(
                market_calendar_version,
                "market_calendar_version",
            ),
            "comparison_currency": "CNY",
            "contract_quantity": 1,
            "option_market_concentration_metric_version": (
                OPTION_MARKET_CONCENTRATION_METRIC_VERSION
            ),
            "evidence_selection_contract_version": (
                EVIDENCE_SELECTION_CONTRACT_VERSION
            ),
            "economic_result_contract_version": (
                SELL_PUT_TOP1_ECONOMIC_RESULT_VERSION
            ),
            "return_capital_basis": (
                "strike_x_multiplier_minus_opening_net_premium"
            ),
        },
        "expiry_outcome": {
            "contract_version": EXPIRY_OUTCOME_CONTRACT_VERSION,
            "spot_source": "opend_history_kline",
            "ktype": "K_DAY",
            "autype": "NONE",
            "price_field": "close",
            "due_boundary": "expiration_observation_start_ms",
            "pending_elapsed_hours": 72,
        },
    }
    baseline = cast(dict[str, object], spec["baseline"])
    baseline["behavior_binding_sha256"] = build_current_behavior_binding(spec)
    return validate_experiment_spec(spec)


def build_research_spec_sha256(validated_spec: object) -> str:
    spec = validate_experiment_spec(validated_spec)
    return canonical_sha256({key: spec[key] for key in _RESEARCH_HASH_KEYS})


def build_sell_put_top1_validation_spec(
    research_spec: object,
    *,
    timer_binding: Mapping[str, object],
) -> dict[str, object]:
    spec = validate_experiment_spec(research_spec)
    for key in _VALIDATION_ONLY_KEYS:
        spec.pop(key, None)
    spec.update(
        {
            "validation_evaluation": {
                "required_days": VALIDATION_REQUIRED_DAYS,
                "window_mode": "fixed_future_consecutive_trading_days",
                "visibility": "hidden_until_final_seal",
            },
            "fill_observation": {
                "applies_to": "validation_only",
                "contract_version": VALIDATION_FILL_CONTRACT_VERSION,
            },
            "timer_binding": deepcopy(dict(timer_binding)),
            "validation_metrics": {
                "contract_version": VALIDATION_METRIC_CONTRACT_VERSION,
                "confidence_level": 0.95,
                "worst_fraction": 0.20,
            },
        }
    )
    return validate_experiment_spec(spec)


def build_sell_put_top1_research_preview_sha256(
    *,
    experiment_id: str,
    stage_spec_sha256: str,
    source_bindings: object,
) -> str:
    bindings = _mapping(source_bindings, "source_bindings")
    return canonical_sha256(
        {
            "schema_version": PREVIEW_SCHEMA_VERSION,
            "stage": "research",
            "experiment_id": _text(experiment_id, "experiment_id"),
            "stage_spec_sha256": _sha256(
                stage_spec_sha256, "stage_spec_sha256"
            ),
            "source_bindings": dict(bindings),
        }
    )


def validate_confirmed_start_command(value: object) -> dict[str, object]:
    command = dict(_mapping(value, "confirmed_start"))
    _exact_keys(command, _CONFIRMED_START_KEYS, "confirmed_start")
    _fixed(
        command["schema_version"],
        CONFIRMED_START_COMMAND_SCHEMA_VERSION,
        "confirmed_start.schema_version",
    )
    stage = _text(command["stage"], "confirmed_start.stage")
    if stage not in {"research", "validation"}:
        _fail("confirmed_start.stage is unsupported")
    _fixed(command["market"], "HK", "confirmed_start.market")
    _fixed(command["account"], "lx", "confirmed_start.account")
    _ = _text(command["experiment_id"], "confirmed_start.experiment_id")
    _ = _sha256(
        command["confirmed_preview_sha256"],
        "confirmed_start.confirmed_preview_sha256",
    )
    _ = _text(command["idempotency_key"], "confirmed_start.idempotency_key")
    _ = _text(command["actor"], "confirmed_start.actor")
    _ = _utc_timestamp(
        command["confirmed_at_utc"], "confirmed_start.confirmed_at_utc"
    )
    return command


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
