from __future__ import annotations

import builtins
import math
import statistics
from copy import deepcopy
from datetime import date, timedelta
from typing import Any

import pytest

from domain.domain.engine import (
    SELL_PUT_RANKING_CONTRACT_VERSION,
    SELL_PUT_RANKING_PROFILES,
)
from domain.domain.fee_calc import FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION
from src.application.opening_candidate_snapshot import (
    OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
)
from src.application.strategy_lab.top1 import economics as economics_module
from src.application.strategy_lab.top1.contracts import (
    ACCEPTED_SET_CONTRACT_VERSION,
    EXPERIMENT_SPEC_SCHEMA_VERSION,
    EXPIRY_OUTCOME_CONTRACT_VERSION,
    RESEARCH_METRIC_CONTRACT_VERSION,
    RESEARCH_REQUIRED_DAYS,
    RESEARCH_SELECTION_CONTRACT_VERSION,
    VALIDATION_FILL_CONTRACT_VERSION,
    VALIDATION_METRIC_CONTRACT_VERSION,
    VALIDATION_REQUIRED_DAYS,
    Top1CoreContractError,
    build_behavior_binding,
    build_research_spec_sha256,
    build_validation_spec_sha256,
    validate_experiment_spec,
)
from src.application.strategy_lab.top1.economics import calculate_expiry_efficiency
from src.application.strategy_lab.top1.ranking import (
    RANKING_PROJECTION_SCHEMA_VERSION,
)
from src.application.strategy_lab.top1.statistics import (
    summarize_paired_daily_deltas,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _behavior_versions(
    *, baseline_version: str = "sell-put-baseline.v1", calendar: str = "hk-calendar.v1"
) -> dict[str, str]:
    return {
        "baseline_version": baseline_version,
        "opening_snapshot_schema_version": OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
        "accepted_set_contract_version": ACCEPTED_SET_CONTRACT_VERSION,
        "ranking_projection_schema_version": RANKING_PROJECTION_SCHEMA_VERSION,
        "sell_put_ranking_contract_version": SELL_PUT_RANKING_CONTRACT_VERSION,
        "research_selection_contract_version": RESEARCH_SELECTION_CONTRACT_VERSION,
        "research_metric_contract_version": RESEARCH_METRIC_CONTRACT_VERSION,
        "validation_fill_contract_version": VALIDATION_FILL_CONTRACT_VERSION,
        "validation_metric_contract_version": VALIDATION_METRIC_CONTRACT_VERSION,
        "fee_schedule_version": FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION,
        "market_calendar_version": calendar,
        "expiry_outcome_contract_version": EXPIRY_OUTCOME_CONTRACT_VERSION,
    }


def _research_spec(
    profiles: tuple[str, ...] = ("without_concentration",),
) -> dict[str, Any]:
    baseline_version = "sell-put-baseline.v1"
    calendar = "hk-calendar.v1"
    return {
        "schema_version": EXPERIMENT_SPEC_SCHEMA_VERSION,
        "topic_id": "topic-concentration",
        "experiment_id": "experiment-001",
        "market": "HK",
        "account": "lx",
        "hypothesis": {
            "hypothesis_type": "sell_put_ranking",
            "statement": "Prefer lower cross-symbol concentration earlier.",
            "mechanism": "Move the existing concentration fact ahead of the return band.",
            "independent_variable": "cross_symbol_concentration_priority",
            "expected_direction": "higher_top1_efficiency_without_higher_concentration",
        },
        "baseline": {
            "version": baseline_version,
            "opening_snapshot_schema": OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
            "accepted_set_contract_version": ACCEPTED_SET_CONTRACT_VERSION,
            "ranking_projection_schema_version": RANKING_PROJECTION_SCHEMA_VERSION,
            "sell_put_ranking_contract_version": SELL_PUT_RANKING_CONTRACT_VERSION,
            "behavior_binding_sha256": build_behavior_binding(
                _behavior_versions(baseline_version=baseline_version, calendar=calendar)
            ),
        },
        "research_source": {
            "mode": "sealed_historical_dataset",
            "dataset_ref": "strategy_lab/top1/research-001.json",
            "dataset_sha256": SHA_A,
            "research_cutoff_at": "2026-08-14T16:00:00Z",
            "start_trading_date": "2026-06-19",
            "end_trading_date": "2026-08-14",
        },
        "research_evaluation": {
            "contract_version": RESEARCH_SELECTION_CONTRACT_VERSION,
            "metric_contract_version": RESEARCH_METRIC_CONTRACT_VERSION,
            "fill_assumption": "t0_sell_limit",
            "required_days": RESEARCH_REQUIRED_DAYS,
            "window_mode": "fixed_consecutive_trading_days",
            "visibility": "visible_after_research_seal",
        },
        "variants": [
            {"variant_id": "baseline", "patch": {}},
            *[
                {
                    "variant_id": f"level-{index}",
                    "patch": {"ranking_profile": profile},
                }
                for index, profile in enumerate(profiles, start=1)
            ],
        ],
        "frozen_safety": {
            "mode": "inherit_each_point_producer_accepted_set",
            "variant_may_change_acceptance": False,
        },
        "economics_contracts": {
            "fee_schedule_version": FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION,
            "market_calendar_version": calendar,
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


def _validation_spec(
    profiles: tuple[str, ...] = ("without_concentration", "concentration_first"),
) -> dict[str, Any]:
    spec = _research_spec(profiles)
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
            "timer_binding": {
                "revision": "top1-advance.v1",
                "producer_catchup_grace_seconds": 30,
                "producer_run_timeout_upper_bound_seconds": 120,
                "advance_cadence_seconds": 60,
                "fill_observation_duration_upper_bound_seconds": 120,
                "terms_capture_duration_upper_bound_seconds": 120,
            },
            "validation_metrics": {
                "contract_version": VALIDATION_METRIC_CONTRACT_VERSION,
                "confidence_level": 0.95,
                "worst_fraction": 0.20,
            },
        }
    )
    return spec


def _refresh_behavior(spec: dict[str, Any]) -> None:
    spec["baseline"]["behavior_binding_sha256"] = build_behavior_binding(
        _behavior_versions(
            baseline_version=spec["baseline"]["version"],
            calendar=spec["economics_contracts"]["market_calendar_version"],
        )
    )


def test_experiment_spec_golden_shapes_are_detached_and_accept_all_profiles() -> None:
    research = _research_spec(tuple(sorted(SELL_PUT_RANKING_PROFILES)))
    validation = _validation_spec()
    research_before = deepcopy(research)
    validation_before = deepcopy(validation)

    validated_research = validate_experiment_spec(research)
    validated_validation = validate_experiment_spec(validation)

    assert research == research_before
    assert validation == validation_before
    assert validated_research == research
    assert validated_validation == validation
    assert validated_research is not research
    validated_research["hypothesis"]["statement"] = "changed"
    assert research["hypothesis"]["statement"] != "changed"


def test_experiment_spec_rejects_bad_shapes_constants_and_values() -> None:
    bad_specs: list[dict[str, Any]] = []

    missing = _research_spec()
    missing.pop("hypothesis")
    bad_specs.append(missing)
    extra = _research_spec()
    extra["prompt_version"] = "v1"
    bad_specs.append(extra)
    partial_validation = _research_spec()
    partial_validation["validation_evaluation"] = _validation_spec()["validation_evaluation"]
    bad_specs.append(partial_validation)
    wrong_market = _research_spec()
    wrong_market["market"] = "hk"
    bad_specs.append(wrong_market)
    wrong_account = _research_spec()
    wrong_account["account"] = "LX"
    bad_specs.append(wrong_account)
    filtering_patch = _research_spec()
    filtering_patch["variants"][1]["patch"] = {"max_spread_ratio": 0.2}
    bad_specs.append(filtering_patch)
    duplicate_profile = _research_spec(("without_concentration", "without_concentration"))
    bad_specs.append(duplicate_profile)
    bad_path = _research_spec()
    bad_path["research_source"]["dataset_ref"] = "../secret.json"
    bad_specs.append(bad_path)
    reversed_dates = _research_spec()
    reversed_dates["research_source"]["start_trading_date"] = "2026-08-15"
    bad_specs.append(reversed_dates)
    forged_behavior = _research_spec()
    forged_behavior["baseline"]["behavior_binding_sha256"] = SHA_B
    bad_specs.append(forged_behavior)
    wrong_constant = _research_spec()
    wrong_constant["research_evaluation"]["required_days"] = RESEARCH_REQUIRED_DAYS + 1
    bad_specs.append(wrong_constant)
    wrong_numeric_type = _research_spec()
    wrong_numeric_type["research_evaluation"]["required_days"] = float(RESEARCH_REQUIRED_DAYS)
    bad_specs.append(wrong_numeric_type)
    non_finite = _validation_spec()
    non_finite["validation_metrics"]["confidence_level"] = math.nan
    bad_specs.append(non_finite)
    zero_timer = _validation_spec()
    zero_timer["timer_binding"]["advance_cadence_seconds"] = 0
    bad_specs.append(zero_timer)

    for bad in bad_specs:
        with pytest.raises(Top1CoreContractError) as exc_info:
            validate_experiment_spec(bad)
        assert exc_info.value.reason_code == "experiment_spec_invalid"


def test_behavior_binding_has_exact_domain_and_every_field_changes_digest() -> None:
    versions = _behavior_versions()
    original = build_behavior_binding(versions)
    for key in versions:
        changed = dict(versions)
        changed[key] = f"{changed[key]}.changed"
        assert build_behavior_binding(changed) != original

    with pytest.raises(Top1CoreContractError):
        build_behavior_binding({**versions, "source_commit_sha": "f" * 40})
    assert build_behavior_binding(versions) == original


def test_research_hash_projects_only_research_semantics() -> None:
    spec = _validation_spec()
    original = build_research_spec_sha256(spec)

    identity_only = deepcopy(spec)
    identity_only.update(
        {"topic_id": "another-topic", "experiment_id": "another-experiment", "account": "sy"}
    )
    identity_only["timer_binding"]["revision"] = "another-timer.v2"
    assert build_research_spec_sha256(identity_only) == original

    changed_hypothesis = deepcopy(spec)
    changed_hypothesis["hypothesis"]["statement"] = "A different research statement."
    assert build_research_spec_sha256(changed_hypothesis) != original

    changed_baseline = deepcopy(spec)
    changed_baseline["baseline"]["version"] = "sell-put-baseline.v2"
    _refresh_behavior(changed_baseline)
    assert build_research_spec_sha256(changed_baseline) != original

    changed_dataset = deepcopy(spec)
    changed_dataset["research_source"]["dataset_sha256"] = SHA_B
    assert build_research_spec_sha256(changed_dataset) != original

    changed_calendar = deepcopy(spec)
    changed_calendar["economics_contracts"]["market_calendar_version"] = "hk-calendar.v2"
    _refresh_behavior(changed_calendar)
    assert build_research_spec_sha256(changed_calendar) != original


def test_validation_hash_binds_terminal_challenger_commitment_and_validation_semantics() -> None:
    spec = _validation_spec()
    original = build_validation_spec_sha256(
        spec,
        research_terminal_sha256=SHA_A,
        challenger_variant_id="level-1",
        hidden_window_commitment_sha256=SHA_B,
    )
    assert (
        build_validation_spec_sha256(
            spec,
            research_terminal_sha256=SHA_C,
            challenger_variant_id="level-1",
            hidden_window_commitment_sha256=SHA_B,
        )
        != original
    )
    assert (
        build_validation_spec_sha256(
            spec,
            research_terminal_sha256=SHA_A,
            challenger_variant_id="level-2",
            hidden_window_commitment_sha256=SHA_B,
        )
        != original
    )
    assert (
        build_validation_spec_sha256(
            spec,
            research_terminal_sha256=SHA_A,
            challenger_variant_id="level-1",
            hidden_window_commitment_sha256=SHA_C,
        )
        != original
    )
    changed_timer = deepcopy(spec)
    changed_timer["timer_binding"]["revision"] = "top1-advance.v2"
    assert (
        build_validation_spec_sha256(
            changed_timer,
            research_terminal_sha256=SHA_A,
            challenger_variant_id="level-1",
            hidden_window_commitment_sha256=SHA_B,
        )
        != original
    )
    changed_calendar = deepcopy(spec)
    changed_calendar["economics_contracts"]["market_calendar_version"] = "hk-calendar.v2"
    _refresh_behavior(changed_calendar)
    assert (
        build_validation_spec_sha256(
            changed_calendar,
            research_terminal_sha256=SHA_A,
            challenger_variant_id="level-1",
            hidden_window_commitment_sha256=SHA_B,
        )
        != original
    )

    with pytest.raises(Top1CoreContractError):
        build_validation_spec_sha256(
            _research_spec(),
            research_terminal_sha256=SHA_A,
            challenger_variant_id="level-1",
            hidden_window_commitment_sha256=SHA_B,
        )
    for challenger in ("baseline", "unknown"):
        with pytest.raises(Top1CoreContractError):
            build_validation_spec_sha256(
                spec,
                research_terminal_sha256=SHA_A,
                challenger_variant_id=challenger,
                hidden_window_commitment_sha256=SHA_B,
            )


def _filled_facts(**overrides: Any) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "stage": "validation",
        "fill_status": "observed_fill",
        "holding_start_date": "2026-06-01",
        "expiration": "2026-07-01",
        "opening_net_premium": 500.0,
        "net_cash_basis": 10_000.0,
        "strike": 100.0,
        "multiplier": 100,
        "underlier_close": 110.0,
        "account_fee_plan": {
            "commission_free": True,
            "platform_fee": 0.0,
            "fee_plan_ref": "futu-hk-plan.v1",
        },
    }
    facts.update(overrides)
    return facts


def test_expiry_economics_no_fill_worthless_and_assignment_hand_calculations() -> None:
    no_fill = calculate_expiry_efficiency(
        {"stage": "validation", "fill_status": "no_observed_fill"}
    )
    assert no_fill["status"] == "evaluable"
    assert no_fill["economic_pnl"] == 0.0
    assert no_fill["efficiency"] == 0.0
    assert no_fill["holding_calendar_days"] is None
    assert no_fill["terminal_fee_amount"] is None

    worthless = calculate_expiry_efficiency(_filled_facts())
    assert worthless["assignment_proxy"] is False
    assert worthless["terminal_fee_amount"] == 0.0
    assert worthless["economic_pnl"] == 500.0
    assert worthless["efficiency"] == pytest.approx(500 / 10_000 / 30 * 365)

    assignment_loss = calculate_expiry_efficiency(_filled_facts(underlier_close=90.0))
    assert assignment_loss["assignment_proxy"] is True
    assert assignment_loss["intrinsic_per_share"] == 10.0
    assert assignment_loss["terminal_fee_amount"] == pytest.approx(11.27)
    assert assignment_loss["economic_pnl"] == pytest.approx(-511.27)
    assert assignment_loss["efficiency"] == pytest.approx(-511.27 / 10_000 / 30 * 365)

    assignment_profit = calculate_expiry_efficiency(_filled_facts(underlier_close=99.0))
    assert assignment_profit["economic_pnl"] == pytest.approx(388.73)


def test_expiry_economics_binds_canonical_fee_inputs_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_fee(kind: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((kind, kwargs))
        return {
            "currency": "HKD",
            "schedule_version": FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION,
            "complete": True,
            "basis": "estimated",
            "amount": 2.0,
            "reason": "test_fee",
        }

    monkeypatch.setattr(economics_module, "calc_futu_hk_terminal_fee", fake_fee)
    calculate_expiry_efficiency(_filled_facts(underlier_close=90.0))
    assert calls == [
        (
            "assignment",
            {
                "order_price": 100.0,
                "shares": 100,
                "contracts": 1,
                "account_fee_plan": {
                    "commission_free": True,
                    "platform_fee": 0.0,
                    "fee_plan_ref": "futu-hk-plan.v1",
                },
            },
        )
    ]
    monkeypatch.undo()

    incomplete = calculate_expiry_efficiency(
        _filled_facts(underlier_close=90.0, account_fee_plan={"commission_free": True})
    )
    assert incomplete["status"] == "not_evaluable"
    assert incomplete["reason_code"] == "required_outcome_missing"
    assert incomplete["reason_detail"] == "expiry_fee_unavailable"
    assert incomplete["economic_pnl"] is None
    assert incomplete["terminal_fee_amount"] is None

    invalid_allowed_fact = calculate_expiry_efficiency(
        _filled_facts(
            underlier_close=90.0,
            account_fee_plan={
                "commission_free": "yes",
                "platform_fee": 0.0,
                "fee_plan_ref": "futu-hk-plan.v1",
            },
        )
    )
    assert invalid_allowed_fact["status"] == "not_evaluable"

    with pytest.raises(ValueError):
        calculate_expiry_efficiency(
            _filled_facts(account_fee_plan={"unexpected": "fact"})
        )


def test_expiry_economics_rejects_fee_contract_drift_and_reports_nonpositive_holding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonpositive = calculate_expiry_efficiency(
        _filled_facts(expiration="2026-06-01")
    )
    assert nonpositive["status"] == "not_evaluable"
    assert nonpositive["reason_code"] == "holding_period_non_positive"
    assert nonpositive["economic_pnl"] is None

    def drifted_fee(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "currency": "HKD",
            "schedule_version": "future-fee.v2",
            "complete": True,
            "basis": "estimated",
            "amount": 0.0,
            "reason": "test",
        }

    monkeypatch.setattr(economics_module, "calc_futu_hk_terminal_fee", drifted_fee)
    with pytest.raises(ValueError, match="schedule version mismatch"):
        calculate_expiry_efficiency(_filled_facts())


def _policy(required_days: int, **overrides: Any) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "required_days": required_days,
        "confidence_level": 0.95,
        "worst_fraction": 0.20,
        "require_concentration_non_increase": True,
    }
    policy.update(overrides)
    return policy


def _point(
    index: int,
    trading_date: str,
    delta: float,
    **overrides: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "recommendation_point_id": f"point-{index}",
        "trading_date": trading_date,
        "baseline_candidate_id": f"baseline-{index}",
        "challenger_candidate_id": f"challenger-{index}",
        "baseline_efficiency": 1.0,
        "challenger_efficiency": 1.0 + delta,
        "hard_risk_status": "passed",
        "baseline_concentration": 0.20,
        "challenger_concentration": 0.10,
    }
    row.update(overrides)
    return row


def _daily_points(values: list[float]) -> list[dict[str, Any]]:
    return [
        _point(index, (date(2026, 1, 1) + timedelta(days=index - 1)).isoformat(), value)
        for index, value in enumerate(values, start=1)
    ]


@pytest.mark.parametrize(
    ("required_days", "expected_t", "expected_tail"),
    [
        (20, 1.729132811521367, 0.025),
        (40, 1.6848751217112248, 0.045),
    ],
)
def test_paired_daily_statistics_match_20_and_40_day_hand_calculations(
    required_days: int,
    expected_t: float,
    expected_tail: float,
) -> None:
    values = [index / 100 for index in range(1, required_days + 1)]
    result = summarize_paired_daily_deltas(_daily_points(values), _policy(required_days))

    expected_mean = (required_days + 1) / 200
    expected_std = math.sqrt(required_days * (required_days + 1) / 12) / 100
    expected_se = expected_std / math.sqrt(required_days)
    expected_lcb = expected_mean - expected_t * expected_se
    assert result["decision"] == "pass"
    assert result["mean_daily_delta"] == pytest.approx(expected_mean)
    assert result["sample_std"] == pytest.approx(expected_std)
    assert result["standard_error"] == pytest.approx(expected_se)
    assert result["t_critical"] == pytest.approx(expected_t)
    assert result["one_sided_lower_bound"] == pytest.approx(expected_lcb)
    assert result["worst_k"] == math.ceil(required_days * 0.20)
    assert result["worst_tail_mean"] == pytest.approx(expected_tail)
    assert result["serial_correlation_unadjusted"] is True


def test_points_are_averaged_within_day_and_no_candidate_day_is_not_extended() -> None:
    rows = [
        _point(1, "2026-07-01", 0.10),
        _point(2, "2026-07-01", 0.30),
        _point(
            3,
            "2026-07-02",
            0.0,
            baseline_candidate_id=None,
            challenger_candidate_id=None,
            baseline_efficiency=None,
            challenger_efficiency=None,
            baseline_concentration=None,
            challenger_concentration=None,
        ),
    ]
    before = deepcopy(rows)
    result = summarize_paired_daily_deltas(rows, _policy(2))

    assert rows == before
    assert result["decision"] == "insufficient_evidence"
    assert result["reason_codes"] == ["effective_days_below_required"]
    assert result["effective_days"] == 1
    assert result["daily_deltas"] == [
        {
            "trading_date": "2026-07-01",
            "effective_point_count": 2,
            "daily_delta": pytest.approx(0.20),
        }
    ]
    assert [row["status"] for row in result["point_results"]] == [
        "paired",
        "paired",
        "no_evidence",
    ]


def test_same_candidate_is_zero_without_outcome_values() -> None:
    rows = [
        _point(
            1,
            "2026-07-01",
            1.0,
            baseline_candidate_id="same",
            challenger_candidate_id="same",
            baseline_efficiency=None,
            challenger_efficiency=None,
            baseline_concentration=None,
            challenger_concentration=None,
        ),
        _point(
            2,
            "2026-07-02",
            1.0,
            baseline_candidate_id="same-2",
            challenger_candidate_id="same-2",
            baseline_efficiency=None,
            challenger_efficiency=None,
            baseline_concentration=None,
            challenger_concentration=None,
        ),
    ]
    result = summarize_paired_daily_deltas(rows, _policy(2))
    assert result["decision"] == "keep_baseline"
    assert result["reason_codes"] == ["non_positive_mean"]
    assert result["sample_std"] == 0.0
    assert result["one_sided_lower_bound"] == 0.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"challenger_candidate_id": None},
        {"challenger_efficiency": None},
    ],
)
def test_one_sided_or_missing_paired_outcome_fails_closed(overrides: dict[str, Any]) -> None:
    result = summarize_paired_daily_deltas(
        [_point(1, "2026-07-01", 0.1, **overrides)],
        _policy(2),
    )
    assert result["decision"] == "insufficient_evidence"
    assert result["reason_codes"] == ["official_decision_incomplete"]
    assert result["point_results"] == []


def test_statistical_gate_precedence_and_reason_codes() -> None:
    hard_risk = summarize_paired_daily_deltas(
        [
            _point(
                1,
                "2026-07-01",
                0.1,
                hard_risk_status="violated",
                challenger_candidate_id=None,
            )
        ],
        _policy(2),
    )
    assert hard_risk["reason_codes"] == ["hard_risk_violation"]

    missing_risk = summarize_paired_daily_deltas(
        [_point(1, "2026-07-01", 0.1, hard_risk_status="missing")],
        _policy(2),
    )
    assert missing_risk["reason_codes"] == ["risk_evidence_missing"]

    concentration = summarize_paired_daily_deltas(
        [
            _point(
                1,
                "2026-07-01",
                0.1,
                challenger_concentration=0.30,
            ),
            _point(2, "2026-07-02", 0.1),
        ],
        _policy(2),
    )
    assert concentration["decision"] == "keep_baseline"
    assert concentration["reason_codes"] == ["concentration_non_increase_failed"]

    negative_tail = summarize_paired_daily_deltas(
        _daily_points([-0.1, 0.2, 0.2, 0.2, 0.2]),
        _policy(5),
    )
    assert negative_tail["reason_codes"] == ["negative_worst_tail"]

    weak_lcb = summarize_paired_daily_deltas(
        _daily_points([0.0, 0.0, 0.0, 0.0, 1.0]),
        _policy(5),
    )
    assert weak_lcb["decision"] == "insufficient_evidence"
    assert weak_lcb["reason_codes"] == ["positive_mean_lcb_not_above_zero"]
    assert weak_lcb["one_sided_lower_bound"] is not None

    passed = summarize_paired_daily_deltas(
        _daily_points([0.1] * 5),
        _policy(5),
    )
    assert passed["reason_codes"] == [
        "positive_one_sided_lcb",
        "non_negative_worst_tail",
        "hard_risk_passed",
    ]
    assert passed["sample_std"] == 0.0
    assert passed["one_sided_lower_bound"] == pytest.approx(0.1)


def test_t_critical_tracks_required_days_and_backend_failure_has_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dynamic = summarize_paired_daily_deltas(
        _daily_points([0.1, 0.1, 0.1]),
        _policy(3),
    )
    assert dynamic["t_critical"] == pytest.approx(2.919985580353725)
    assert dynamic["worst_k"] == 1

    real_import = builtins.__import__

    def blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "scipy.stats":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    unavailable = summarize_paired_daily_deltas(
        _daily_points([0.1, 0.1, 0.1]),
        _policy(3),
    )
    assert unavailable["decision"] == "insufficient_evidence"
    assert unavailable["reason_codes"] == ["statistics_backend_unavailable"]
    assert unavailable["t_critical"] is None
    assert unavailable["one_sided_lower_bound"] is None


def test_statistics_reject_duplicate_points_and_too_many_dates() -> None:
    duplicate = [_point(1, "2026-07-01", 0.1), _point(1, "2026-07-02", 0.1)]
    with pytest.raises(ValueError, match="must be unique"):
        summarize_paired_daily_deltas(duplicate, _policy(2))

    with pytest.raises(ValueError, match="more trading dates"):
        summarize_paired_daily_deltas(
            _daily_points([0.1, 0.1, 0.1]),
            _policy(2),
        )


def test_sample_standard_deviation_uses_n_minus_one() -> None:
    values = [0.1, 0.2, 0.4]
    result = summarize_paired_daily_deltas(_daily_points(values), _policy(3))
    assert result["sample_std"] == pytest.approx(statistics.stdev(values))
