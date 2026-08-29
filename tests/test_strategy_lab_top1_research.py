from __future__ import annotations

import hashlib
import json
import math
import shutil
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.fee_calc import FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION
from domain.domain.option_lifecycle import expiration_observation_start_ms
from src.application.opening_candidate_snapshot import (
    OPENING_CANDIDATE_SNAPSHOT_FILE,
    load_opening_candidate_snapshot,
)
from src.application.recommendation_point import (
    RECOMMENDATION_POINT_FILE,
    capture_scheduled_recommendation_point,
)
from src.application.shadow_replay.common import (
    attach_artifact_provenance,
    render_json_text,
)
from src.application.strategy_lab.top1 import research as research_module
from src.application.strategy_lab.top1.contracts import (
    RESEARCH_REQUIRED_DAYS,
    VALIDATION_FILL_CONTRACT_VERSION,
    VALIDATION_METRIC_CONTRACT_VERSION,
    VALIDATION_REQUIRED_DAYS,
    build_current_behavior_binding,
    build_sell_put_top1_research_spec,
)
from src.application.strategy_lab.top1.corpus import (
    RESEARCH_WINDOW_FACTS_SCHEMA,
    capture_recommendation_point,
    freeze_research_dataset,
    seal_day_expectation,
)
from src.application.strategy_lab.top1.lifecycle import (
    Top1LifecycleError,
    authorize_research,
    lock_challenger,
    prepare_experiment,
    record_generation_revision,
    seal_generation,
    start_research,
    start_validation,
)
from src.application.strategy_lab.top1.ranking import (
    RANKING_PROJECTION_ARTIFACT_KIND,
    RANKING_PROJECTION_SCHEMA_V2,
    RANKING_PROJECTION_SCHEMA_V3,
    build_ranking_projection,
    recipe_projection_behavior_sha256,
)
from src.application.strategy_lab.top1.research import (
    INTERNAL_RESEARCH_QUOTA_DECISION_SCHEMA,
    RESEARCH_CLOSE_RECEIPT_SCHEMA,
    RESEARCH_EVALUATION_INPUT_SCHEMA,
    RESEARCH_EVALUATION_SCHEMA,
    ResearchEvaluationError,
    build_internal_research_revision,
    evaluate_research,
)
from src.application.strategy_lab.top1.terminal_projection import (
    publish_exact_text,
    recover_terminal_projection,
)
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore
from tests.candidate_evidence_helpers import (
    seal_market_calendar_fixture,
    seal_opening_candidate_fixture,
    top1_hk_schedule_fixture,
)


AVAILABLE = {"OM_STRATEGY_LAB_TOP1_AVAILABLE": "1"}
CALENDAR = "hk-calendar.fixture.v1"
CALENDAR_SHA = "a" * 64
SOURCE_SHA = "c" * 40
EXPIRATION = "2026-09-18"
TOP_LEVEL_KEYS = {
    "schema_version",
    "experiment_id",
    "research_spec_sha256",
    "dataset_ref",
    "dataset_sha256",
    "dataset_content_sha256",
    "required_days",
    "effective_days",
    "research_fill_assumption",
    "research_is_counterfactual",
    "contract_terms_revalidated",
    "selection",
    "leader_variant_id",
    "reason_codes",
    "reason_details",
    "variant_results",
    "missing_receipts",
}
VARIANT_KEYS = {
    "variant_id",
    "ranking_profile",
    "near_return_threshold",
    "decision",
    "reason_codes",
    "required_days",
    "effective_days",
    "effective_point_count",
    "mean_daily_annualized_return_delta",
    "mean_daily_pnl_delta_cny",
    "mean_daily_return_capital_basis_delta_cny",
    "sample_std",
    "standard_error",
    "t_critical",
    "one_sided_lower_bound",
    "worst_k",
    "worst_tail_mean",
    "serial_correlation_unadjusted",
    "top1_change_count",
    "point_results",
    "daily_results",
}


def _trading_days(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start)
    days: list[str] = []
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _candidate(
    symbol: str,
    *,
    period_return: float,
    concentration: float,
    discount: float,
    premium: float,
    cash_basis: float,
    currency: str = "HKD",
) -> dict[str, Any]:
    code = symbol.removesuffix(".HK")
    return {
        "symbol": symbol,
        "contract_symbol": f"{code}260918P00400000",
        "expiration": EXPIRATION,
        "option_type": "put",
        "stock_owner": f"HK.{code}",
        "strike": 400.0,
        "spot": 450.0,
        "dte": 70,
        "bid": premium / 100,
        "ask": premium / 100,
        "mid": premium / 100,
        "sell_limit": premium / 100,
        "multiplier": 100,
        "currency": currency,
        "open_interest": 500,
        "volume": 50,
        "spread_ratio": 0.10,
        "period_net_return_on_cash_basis": period_return,
        "annualized_net_return_on_cash_basis": period_return * 365 / 70,
        "net_assignment_discount_pct": discount,
        "symbol_concentration_after": concentration,
        "net_income": premium,
        "net_premium": premium,
        "net_cash_basis": cash_basis,
        "net_income_cny": premium,
        "fee_schedule_version": "fixture.v1",
        "fee_basis": "fixture",
        "fee_schedule_url": "https://example.test/fees",
    }


def _candidates(*, boosted: bool = False) -> list[dict[str, Any]]:
    return [
        _candidate(
            "0700.HK",
            period_return=0.020,
            concentration=0.30,
            discount=0.10,
            premium=1500.0,
            cash_basis=38_500.0,
        ),
        _candidate(
            "3690.HK",
            period_return=0.019,
            concentration=0.50,
            discount=0.20,
            premium=1800.0,
            cash_basis=38_200.0,
        ),
        _candidate(
            "9988.HK",
            period_return=0.015,
            concentration=0.10,
            discount=0.05,
            premium=3200.0 if boosted else 3000.0,
            cash_basis=37_000.0,
        ),
    ]


def _base_projection(
    root: Path, *, run_id: str, candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    seal_opening_candidate_fixture(
        root,
        run_id=run_id,
        market="HK",
        accepted_rows=candidates,
    )
    snapshot = load_opening_candidate_snapshot(
        base=root,
        run_id=run_id,
        account="lx",
        require_current_contract=True,
    )
    evidence = {
        "status": "ready",
        "run_id": run_id,
        "account": "lx",
        "account_config_sha256": snapshot["account_config_sha256"],
        "evidence_at_utc": "2026-06-08T01:59:00Z",
        "open_option_positions": [
            {
                "lot_id": "lot-0700",
                "instrument_key": "0700-position",
                "symbol": "0700.HK",
                "currency": "HKD",
                "multiplier": 100,
                "contracts_open": 1,
            }
        ],
        "valuation_mark_facts": [
            {
                "fact_id": "mark-0700",
                "instrument_key": "0700-position",
                "price": "1",
            }
        ],
        "fx_rate_facts": [
            {
                "fact_id": "hkd-opening",
                "base_currency": "HKD",
                "quote_currency": "CNY",
                "rate": "1",
                "rate_kind": "fixture",
                "effective_at_ms": 1,
                "observed_at_ms": 1,
                "source": "fixture",
                "source_id": "hkd-opening",
                "revision": 1,
                "supersedes_fact_id": None,
                "source_fact_sha256": "8" * 64,
            }
        ],
    }
    evidence["content_sha256"] = canonical_sha256(evidence)
    return build_ranking_projection(
        snapshot,
        point_binding={
            "recommendation_point_id": canonical_sha256({"run_id": run_id}),
            "market": "HK",
            "account": "lx",
            "run_id": run_id,
            "opening_snapshot_ref": (
                f"output_runs/{run_id}/accounts/lx/state/"
                f"{OPENING_CANDIDATE_SNAPSHOT_FILE}"
            ),
            "opening_snapshot_sha256": snapshot["content_sha256"],
            "decision_at_utc": "2026-06-08T02:00:00Z",
            "source_commit_sha": SOURCE_SHA,
            "option_market_evidence_ref": "fixture/option-market.json",
            "option_market_evidence_manifest_sha256": "6" * 64,
            "option_market_evidence_payload_sha256": "7" * 64,
        },
        option_market_evidence=evidence,
        require_option_market_evidence=True,
    )


def _rebind_projection(
    template: dict[str, Any], *, trading_date: str, sequence: int
) -> dict[str, Any]:
    projection = deepcopy(template)
    source = deepcopy(projection.pop("artifact_provenance")["source_generation"])
    projection["recommendation_point_id"] = canonical_sha256(
        {"trading_date": trading_date, "sequence": sequence}
    )
    projection["decision_at_utc"] = f"{trading_date}T02:{sequence:02d}:00Z"
    return attach_artifact_provenance(
        projection,
        artifact_kind=RANKING_PROJECTION_ARTIFACT_KIND,
        source_generation=source,
    )


def _file_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(render_json_text(payload).encode("utf-8")).hexdigest()


def _spec(
    manifest: dict[str, Any],
    *,
    variants: tuple[tuple[str, str], ...],
    validation: bool = False,
) -> dict[str, Any]:
    _ = variants
    spec = build_sell_put_top1_research_spec(
        topic_id="topic-concentration",
        experiment_id="experiment-w5-evaluator",
        market_calendar_version=manifest["market_calendar_version"],
        ranking_projection_schema_version=RANKING_PROJECTION_SCHEMA_V2,
        research_source={
            "mode": "sealed_historical_dataset",
            "dataset_ref": "strategy_lab/top1/research-w5.json",
            "dataset_sha256": _file_sha256(manifest),
            "research_cutoff_at": manifest["cutoff_at_utc"],
            "start_trading_date": manifest["selected_trading_dates"][0],
            "end_trading_date": manifest["selected_trading_dates"][-1],
        },
    )
    if validation:
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


def _build_case(root: Path) -> dict[str, Any]:
    days = _trading_days("2026-06-08", RESEARCH_REQUIRED_DAYS)
    standard = _base_projection(root, run_id="run-standard", candidates=_candidates())
    boosted = _base_projection(
        root,
        run_id="run-boosted",
        candidates=_candidates(boosted=True),
    )
    materialized: list[dict[str, Any]] = []
    dataset_days: list[dict[str, Any]] = []
    for day_index, trading_date in enumerate(days):
        projections = [_rebind_projection(standard, trading_date=trading_date, sequence=0)]
        if day_index == 0:
            projections.append(
                _rebind_projection(boosted, trading_date=trading_date, sequence=1)
            )
        points: list[dict[str, Any]] = []
        for projection in projections:
            point_id = projection["recommendation_point_id"]
            ref = f"strategy_lab/top1/projections/{point_id}.json"
            points.append(
                {
                    "recommendation_point_id": point_id,
                    "projection_ref": ref,
                    "projection_content_sha256": projection["artifact_provenance"][
                        "content_sha256"
                    ],
                    "projection_file_sha256": _file_sha256(projection),
                }
            )
            materialized.append({"projection_ref": ref, "projection": projection})
        dataset_days.append(
            {
                "trading_date": trading_date,
                "expectation_ref": f"strategy_lab/top1/days/{trading_date}.json",
                "expectation_content_sha256": canonical_sha256(
                    {"trading_date": trading_date}
                ),
                "expectation_file_sha256": canonical_sha256(
                    {"trading_date": trading_date, "file": True}
                ),
                "points": points,
            }
        )
    manifest: dict[str, Any] = {
        "schema_version": "sealed_historical_dataset.v1",
        "market": "HK",
        "account": "lx",
        "cutoff_at_utc": "2026-10-01T08:00:00Z",
        "cutoff_trading_date": "2026-10-01",
        "required_days": RESEARCH_REQUIRED_DAYS,
        "window_facts_content_sha256": canonical_sha256({"window": days}),
        "market_calendar_version": CALENDAR,
        "market_calendar_ref": "evidence/hk-calendar.fixture.json",
        "market_calendar_sha256": CALENDAR_SHA,
        "trading_calendar_dates_sha256": canonical_sha256(days),
        "latest_mature_trading_date": days[-1],
        "maturity_evidence_ref": "evidence/hk-maturity.fixture.json",
        "maturity_evidence_sha256": "b" * 64,
        "recommendation_point_selector": "official_scheduled_sell_put.v1",
        "ranking_projection_schema_version": RANKING_PROJECTION_SCHEMA_V2,
        "selected_trading_dates": days,
        "days": dataset_days,
    }
    manifest["content_sha256"] = canonical_sha256(manifest)
    spec = _spec(
        manifest,
        variants=(
            ("without", "without_concentration"),
            ("concentration", "concentration_first"),
        ),
    )
    return {
        "schema_version": RESEARCH_EVALUATION_INPUT_SCHEMA,
        "experiment_spec": spec,
        "dataset_ref": spec["research_source"]["dataset_ref"],
        "sealed_dataset": manifest,
        "ranking_projections": materialized,
    }


@pytest.fixture(scope="module")
def research_case(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _build_case(tmp_path_factory.mktemp("w5-research"))


def _fee_contract(*, complete: bool = True) -> dict[str, Any]:
    return {
        "market": "HK",
        "account": "lx",
        "fee_schedule_version": FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION,
        "account_fee_plan": (
            {
                "commission_free": True,
                "platform_fee": 0.0,
                "fee_plan_ref": "fixture://lx/hk-fee-plan",
            }
            if complete
            else {"commission_free": True}
        ),
    }


def _receipt(
    owner: str,
    *,
    close: float | None = 390.0,
    reason: str | None = None,
) -> dict[str, Any]:
    terminal_at_ms = expiration_observation_start_ms(EXPIRATION, "HK")
    assert terminal_at_ms is not None
    return {
        "schema_version": RESEARCH_CLOSE_RECEIPT_SCHEMA,
        "market": "HK",
        "account": "lx",
        "stock_owner": owner,
        "expiration": EXPIRATION,
        "spot_source": "opend_history_kline",
        "ktype": "K_DAY",
        "autype": "NONE",
        "price_field": "close",
        "status": "available" if close is not None else "unavailable",
        "underlier_close": close,
        "currency": "HKD",
        "terminal_at_ms": terminal_at_ms,
        "terminal_fx_binding": {
            "schema_version": "fx_rate_binding.v1",
            "selected_at_ms": terminal_at_ms,
            "fact_id": "hkd-terminal",
            "base_currency": "HKD",
            "quote_currency": "CNY",
            "rate": "1",
            "rate_kind": "fixture",
            "effective_at_ms": 1,
            "observed_at_ms": 1,
            "source": "fixture",
            "source_id": "hkd-terminal",
            "revision": 1,
            "supersedes_fact_id": None,
            "source_fact_sha256": "9" * 64,
        },
        "reason_detail": reason,
    }


def _receipts() -> list[dict[str, Any]]:
    return [_receipt(owner) for owner in ("HK.0700", "HK.3690")]


def _refresh_case(case: dict[str, Any]) -> None:
    manifest = case["sealed_dataset"]
    manifest["content_sha256"] = canonical_sha256(
        {key: value for key, value in manifest.items() if key != "content_sha256"}
    )
    spec = case["experiment_spec"]
    spec["research_source"]["dataset_sha256"] = _file_sha256(manifest)


def _replace_point_projection(
    case: dict[str, Any], *, day_index: int, projection: dict[str, Any]
) -> None:
    point = case["sealed_dataset"]["days"][day_index]["points"][0]
    old_ref = point["projection_ref"]
    projection = _rebind_projection(
        projection,
        trading_date=case["sealed_dataset"]["selected_trading_dates"][day_index],
        sequence=0,
    )
    point.update(
        {
            "recommendation_point_id": projection["recommendation_point_id"],
            "projection_content_sha256": projection["artifact_provenance"][
                "content_sha256"
            ],
            "projection_file_sha256": _file_sha256(projection),
        }
    )
    materialized = next(
        item for item in case["ranking_projections"] if item["projection_ref"] == old_ref
    )
    materialized["projection"] = projection
    _refresh_case(case)


def _force_same_top1(case: dict[str, Any]) -> None:
    points = {
        point["projection_ref"]: point
        for day in case["sealed_dataset"]["days"]
        for point in day["points"]
    }
    for materialized in case["ranking_projections"]:
        projection = materialized["projection"]
        source = deepcopy(projection.pop("artifact_provenance")["source_generation"])
        for candidate in projection["candidates"]:
            candidate["option_market_concentration_after"] = (
                0.1 if candidate["producer_rank"] == 1 else 0.5
            )
        attach_artifact_provenance(
            projection,
            artifact_kind=RANKING_PROJECTION_ARTIFACT_KIND,
            source_generation=source,
        )
        point = points[materialized["projection_ref"]]
        point["projection_content_sha256"] = projection["artifact_provenance"][
            "content_sha256"
        ]
        point["projection_file_sha256"] = _file_sha256(projection)
    _refresh_case(case)


def test_selects_unique_leader_and_aggregates_two_points_by_day(
    research_case: dict[str, Any],
) -> None:
    result = evaluate_research(research_case, _receipts(), _fee_contract())

    assert set(result) == TOP_LEVEL_KEYS
    assert result["schema_version"] == RESEARCH_EVALUATION_SCHEMA
    assert result["selection"] == "research_leader"
    assert result["leader_variant_id"] == "concentration-0.002"
    assert result["effective_days"] == RESEARCH_REQUIRED_DAYS
    assert result["research_fill_assumption"] == "t0_sell_limit"
    assert result["research_is_counterfactual"] is True
    assert result["contract_terms_revalidated"] is False
    variants = {item["variant_id"]: item for item in result["variant_results"]}
    assert all(set(item) == VARIANT_KEYS for item in variants.values())
    assert set(variants) == {
        "concentration-0.002",
        "concentration-0.004",
        "concentration-0.006",
    }
    assert all(item["decision"] == "pass" for item in variants.values())
    selected = variants["concentration-0.002"]
    assert selected["top1_change_count"] == (RESEARCH_REQUIRED_DAYS + 1)

    first_day = selected["daily_results"][0]
    holding_days = (date.fromisoformat(EXPIRATION) - date(2026, 6, 8)).days
    terminal_fee = 45.08
    baseline_efficiency = (1500.0 - 1000.0 - terminal_fee) / 38_500.0 / holding_days * 365
    challenger = (1800.0 - 1000.0 - terminal_fee) / 38_200.0 / holding_days * 365
    assert first_day == {
        "trading_date": "2026-06-08",
        "effective_point_count": 2,
        "annualized_return_delta": pytest.approx(challenger - baseline_efficiency),
        "pnl_delta_cny": pytest.approx(300.0),
        "return_capital_basis_delta_cny": pytest.approx(-300.0),
    }
    assert result["reason_codes"] == [
        "positive_one_sided_lcb",
        "non_negative_worst_tail",
        "pnl_non_inferiority_passed",
        "hard_risk_passed",
    ]


def test_compact_v3_recipe_projection_produces_same_research_result(
    research_case: dict[str, Any],
) -> None:
    case = deepcopy(research_case)
    points = {
        point["projection_ref"]: point
        for day in case["sealed_dataset"]["days"]
        for point in day["points"]
    }
    for item in case["ranking_projections"]:
        projection = item["projection"]
        old_ref = item["projection_ref"]
        point = points[old_ref]
        formal_hash = canonical_sha256({"projection_ref": old_ref})
        formal_ref = (
            "output_shared/research/formal_corpus/v1/hk/lx/points/"
            f"{point['recommendation_point_id']}/{formal_hash}.json.gz"
        )
        compact = {
            "schema_version": RANKING_PROJECTION_SCHEMA_V3,
            "formal_point_ref": formal_ref,
            "formal_point_content_sha256": formal_hash,
            "recipe_id": "sell_put_top1_option_market_concentration",
            "recipe_version": "v1",
            "behavior_binding_sha256": recipe_projection_behavior_sha256(),
            "materialized_input_content_sha256": projection[
                "artifact_provenance"
            ]["content_sha256"],
            "producer_accepted_candidate_ids": list(
                projection["producer_accepted_candidate_ids"]
            ),
            "candidates": [
                {
                    key: deepcopy(candidate[key])
                    for key in (
                        "candidate_id",
                        "option_market_concentration_after",
                        "option_market_value_cny",
                        "option_market_concentration_metric_version",
                        "option_market_evidence_refs",
                        "opening_fx_binding",
                    )
                }
                for candidate in projection["candidates"]
            ],
        }
        attach_artifact_provenance(
            compact,
            artifact_kind=RANKING_PROJECTION_ARTIFACT_KIND,
            source_generation={
                "generation_id": f"formal_point:{formal_hash}",
                "revision": 1,
                "source_ref": formal_ref,
                "source_sha256": formal_hash,
            },
        )
        point.update(
            {
                "projection_ref": formal_ref,
                "projection_content_sha256": compact["artifact_provenance"][
                    "content_sha256"
                ],
                "projection_file_sha256": _file_sha256(compact),
            }
        )
        item.update(
            {
                "projection_ref": formal_ref,
                "recipe_projection": compact,
            }
        )
    case["sealed_dataset"][
        "ranking_projection_schema_version"
    ] = RANKING_PROJECTION_SCHEMA_V3
    _refresh_case(case)
    spec = case["experiment_spec"]
    spec["baseline"][
        "ranking_projection_schema_version"
    ] = RANKING_PROJECTION_SCHEMA_V3
    spec["baseline"]["behavior_binding_sha256"] = build_current_behavior_binding(spec)

    result = evaluate_research(case, _receipts(), _fee_contract())

    assert result["selection"] == "research_leader"
    assert result["leader_variant_id"] == "concentration-0.002"
    assert result["effective_days"] == RESEARCH_REQUIRED_DAYS

    projection = case["ranking_projections"][0]["projection"]
    source_generation = deepcopy(
        projection.pop("artifact_provenance")["source_generation"]
    )
    projection["candidates"][0]["sell_limit"] += 0.01
    attach_artifact_provenance(
        projection,
        artifact_kind=RANKING_PROJECTION_ARTIFACT_KIND,
        source_generation=source_generation,
    )
    with pytest.raises(ResearchEvaluationError) as exc_info:
        evaluate_research(case, _receipts(), _fee_contract())
    assert exc_info.value.reason_code == "research_corpus_conflict"


def test_same_or_empty_top1_needs_no_close_or_fee_plan(
    research_case: dict[str, Any],
) -> None:
    case = deepcopy(research_case)
    _force_same_top1(case)

    result = evaluate_research(case, [], _fee_contract(complete=False))

    assert result["selection"] == "no_research_winner"
    assert result["leader_variant_id"] is None
    assert result["effective_days"] == RESEARCH_REQUIRED_DAYS
    assert result["reason_codes"] == ["no_research_winner"]
    assert all(
        item["mean_daily_annualized_return_delta"] == 0.0
        for item in result["variant_results"]
    )


@pytest.mark.parametrize("mode", ["missing", "unavailable", "duplicate"])
def test_required_close_failures_block_every_variant_and_order_is_stable(
    research_case: dict[str, Any], mode: str
) -> None:
    receipts = _receipts()
    target = next(item for item in receipts if item["stock_owner"] == "HK.3690")
    if mode == "missing":
        receipts.remove(target)
    elif mode == "unavailable":
        target.update(
            {
                "status": "unavailable",
                "underlier_close": None,
                "reason_detail": "expiry_source_unavailable_after_deadline",
            }
        )
    else:
        receipts.append(deepcopy(target))
    unused = _receipt("HK.00001")
    receipts.extend((unused, deepcopy(unused)))

    forward = evaluate_research(research_case, receipts, _fee_contract())
    reverse = evaluate_research(research_case, list(reversed(receipts)), _fee_contract())

    assert forward == reverse
    assert forward["selection"] == "insufficient_evidence"
    assert forward["leader_variant_id"] is None
    assert forward["effective_days"] is None
    assert forward["variant_results"] == []
    assert [item["stock_owner"] for item in forward["missing_receipts"]] == [
        "HK.3690"
    ]
    expected = {
        "missing": (
            "research_expiry_close_missing",
            "expiry_close_missing_after_deadline",
        ),
        "unavailable": (
            "required_outcome_missing",
            "expiry_source_unavailable_after_deadline",
        ),
        "duplicate": (
            "required_outcome_missing",
            "expiry_close_receipt_conflict",
        ),
    }[mode]
    assert forward["reason_codes"] == [expected[0]]
    assert forward["reason_details"] == [expected[1]]


def test_incomplete_assignment_fee_and_short_window_fail_closed(
    research_case: dict[str, Any],
) -> None:
    fee_result = evaluate_research(research_case, _receipts(), _fee_contract(complete=False))
    assert fee_result["selection"] == "insufficient_evidence"
    assert fee_result["reason_codes"] == ["required_outcome_missing"]
    assert fee_result["reason_details"] == []

    missing_plan = _fee_contract()
    missing_plan["account_fee_plan"] = None
    missing_plan_result = evaluate_research(research_case, _receipts(), missing_plan)
    assert missing_plan_result["selection"] == "insufficient_evidence"
    assert missing_plan_result["reason_codes"] == ["required_outcome_missing"]
    assert missing_plan_result["reason_details"] == []

    short = deepcopy(research_case)
    empty = deepcopy(short["ranking_projections"][0]["projection"])
    source = deepcopy(empty.pop("artifact_provenance")["source_generation"])
    empty["producer_accepted_candidate_ids"] = []
    empty["candidates"] = []
    attach_artifact_provenance(
        empty,
        artifact_kind=RANKING_PROJECTION_ARTIFACT_KIND,
        source_generation=source,
    )
    _replace_point_projection(
        short,
        day_index=1,
        projection=empty,
    )
    short_result = evaluate_research(short, _receipts(), _fee_contract())
    assert short_result["selection"] == "insufficient_evidence"
    assert short_result["effective_days"] == RESEARCH_REQUIRED_DAYS - 1
    assert short_result["reason_codes"] == ["effective_days_below_required"]


def test_currency_and_materialization_tampering_fail_closed(
    research_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    currency = deepcopy(research_case)
    projection = currency["ranking_projections"][0]["projection"]
    source = deepcopy(projection.pop("artifact_provenance")["source_generation"])
    next(
        row for row in projection["candidates"] if row["stock_owner"] == "HK.3690"
    )["currency"] = "USD"
    attach_artifact_provenance(
        projection,
        artifact_kind=RANKING_PROJECTION_ARTIFACT_KIND,
        source_generation=source,
    )
    point = currency["sealed_dataset"]["days"][0]["points"][0]
    point["projection_content_sha256"] = projection["artifact_provenance"][
        "content_sha256"
    ]
    point["projection_file_sha256"] = _file_sha256(projection)
    _refresh_case(currency)
    monkeypatch.setattr(
        research_module,
        "calculate_sell_put_top1_economic_result",
        lambda _facts: pytest.fail("cross-currency candidate reached economics"),
    )
    result = evaluate_research(currency, _receipts(), _fee_contract())
    assert result["selection"] == "insufficient_evidence"
    assert result["reason_codes"] == ["ranking_projection_incomplete"]
    assert result["reason_details"] == ["candidate_currency_mismatch"]

    corpus = deepcopy(research_case)
    corpus["sealed_dataset"]["cutoff_trading_date"] = "2026-09-30"
    with pytest.raises(ResearchEvaluationError) as exc_info:
        evaluate_research(corpus, _receipts(), _fee_contract())
    assert exc_info.value.reason_code == "research_corpus_conflict"


def test_baseline_parity_tampering_is_rejected(
    research_case: dict[str, Any],
) -> None:
    case = deepcopy(research_case)
    projection = case["ranking_projections"][0]["projection"]
    source = deepcopy(projection.pop("artifact_provenance")["source_generation"])
    projection["candidates"][0], projection["candidates"][1] = (
        projection["candidates"][1],
        projection["candidates"][0],
    )
    for rank, row in enumerate(projection["candidates"], start=1):
        row["producer_rank"] = rank
    projection["producer_accepted_candidate_ids"] = [
        row["candidate_id"] for row in projection["candidates"]
    ]
    attach_artifact_provenance(
        projection,
        artifact_kind=RANKING_PROJECTION_ARTIFACT_KIND,
        source_generation=source,
    )
    point = case["sealed_dataset"]["days"][0]["points"][0]
    point["projection_content_sha256"] = projection["artifact_provenance"][
        "content_sha256"
    ]
    point["projection_file_sha256"] = _file_sha256(projection)
    _refresh_case(case)

    with pytest.raises(ResearchEvaluationError) as exc_info:
        evaluate_research(case, _receipts(), _fee_contract())
    assert exc_info.value.reason_code == "baseline_rank_parity_mismatch"


@pytest.mark.parametrize(
    ("summaries", "expected"),
    [
        (
            ((2.0, 1.0, 1.0, 1.0), (1.0, 9.0, 9.0, 9.0), (0.0, 9.0, 9.0, 9.0)),
            "concentration-0.002",
        ),
        (
            ((2.0, 2.0, 1.0, 1.0), (2.0, 1.0, 9.0, 9.0), (2.0, 0.0, 9.0, 9.0)),
            "concentration-0.002",
        ),
        (
            ((2.0, 2.0, 2.0, 1.0), (2.0, 2.0, 1.0, 9.0), (2.0, 2.0, 0.0, 9.0)),
            "concentration-0.002",
        ),
        (
            ((2.0, 2.0, 2.0, 2.0), (2.0, 2.0, 2.0, 1.0), (2.0, 2.0, 2.0, 0.0)),
            "concentration-0.002",
        ),
    ],
)
def test_passing_leader_uses_every_deterministic_tie_break(
    research_case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    summaries: tuple[tuple[float, float, float, float], ...],
    expected: str,
) -> None:
    case = deepcopy(research_case)
    values = iter(summaries)

    def fake_summary(_rows: object, _policy: object) -> dict[str, Any]:
        mean, pnl, lower, tail = next(values)
        return {
            "decision": "pass",
            "reason_codes": [
                "positive_one_sided_lcb",
                "non_negative_worst_tail",
                "hard_risk_passed",
            ],
            "required_days": RESEARCH_REQUIRED_DAYS,
            "effective_days": RESEARCH_REQUIRED_DAYS,
            "effective_point_count": RESEARCH_REQUIRED_DAYS,
            "top1_change_count": RESEARCH_REQUIRED_DAYS,
            "point_results": [],
            "daily_results": [],
            "mean_daily_annualized_return_delta": mean,
            "mean_daily_pnl_delta_cny": pnl,
            "mean_daily_return_capital_basis_delta_cny": 0.0,
            "sample_std": 0.0,
            "standard_error": 0.0,
            "t_critical": 1.0,
            "one_sided_lower_bound": lower,
            "worst_k": math.ceil(RESEARCH_REQUIRED_DAYS * 0.20),
            "worst_tail_mean": tail,
            "serial_correlation_unadjusted": True,
        }

    monkeypatch.setattr(
        research_module,
        "evaluate_top1_paired_daily_results",
        fake_summary,
    )
    result = evaluate_research(case, _receipts(), _fee_contract())
    assert result["leader_variant_id"] == expected


def _store(path: Path) -> ExperimentStore:
    store = ExperimentStore(path)
    store.migrate(migrated_at_utc="2026-08-15T03:00:00Z")
    return store


def _enable(store: ExperimentStore, root: Path, *, key: str) -> None:
    del store, root, key


def test_evaluator_leader_crosses_existing_m3_human_authorization_gate(
    tmp_path: Path, research_case: dict[str, Any]
) -> None:
    evaluation = evaluate_research(research_case, _receipts(), _fee_contract())
    leader = evaluation["leader_variant_id"]
    assert leader == "concentration-0.002"
    store = _store(tmp_path / "lab.sqlite3")
    _enable(store, tmp_path, key="enable-m3-seam")
    spec = research_case["experiment_spec"]
    prepared = prepare_experiment(
        store,
        spec,
        provenance={"source_commit_sha": SOURCE_SHA},
        actor="human",
        occurred_at_utc="2026-08-15T03:01:00Z",
        idempotency_key="prepare-m3-seam",
        artifact_root=tmp_path,
        environ=AVAILABLE,
    )
    authorize_research(
        store,
        experiment_id=spec["experiment_id"],
        research_spec_sha256=prepared["research_spec_sha256"],
        actor="human",
        occurred_at_utc="2026-08-15T03:02:00Z",
        idempotency_key="authorize-research-m3-seam",
        artifact_root=tmp_path,
        environ=AVAILABLE,
    )
    start_research(
        store,
        experiment_id=spec["experiment_id"],
        research_spec_sha256=prepared["research_spec_sha256"],
        actor="human",
        occurred_at_utc="2026-08-15T03:03:00Z",
        idempotency_key="start-research-m3-seam",
        artifact_root=tmp_path,
        environ=AVAILABLE,
    )
    publish_exact_text(
        tmp_path,
        research_case["dataset_ref"],
        render_json_text(research_case["sealed_dataset"]).encode("utf-8"),
    )
    for item in research_case["ranking_projections"]:
        publish_exact_text(
            tmp_path,
            item["projection_ref"],
            render_json_text(item["projection"]).encode("utf-8"),
        )
    revision = build_internal_research_revision(
        research_case,
        evaluation=evaluation,
        fee_contract=_fee_contract(),
        close_receipts=_receipts(),
        quota_decision={
            "schema_version": INTERNAL_RESEARCH_QUOTA_DECISION_SCHEMA,
            "required_stock_owners": ["HK.0700", "HK.3690"],
            "already_counted_stock_owners": ["HK.0700", "HK.3690"],
            "new_stock_owners": [],
            "remain_quota": 0,
        },
        observed_at_utc="2026-08-15T03:03:00Z",
    )
    revision_ref = (
        "strategy_lab/top1/experiments/experiment-w5-evaluator/generations/"
        "research.revision.1.json"
    )
    revision_content = render_json_text(revision).encode("utf-8")
    publish_exact_text(tmp_path, revision_ref, revision_content)
    generation = store.generations(spec["experiment_id"])[0]
    record_generation_revision(
        store,
        experiment_id=spec["experiment_id"],
        generation_kind="research",
        revision=1,
        revision_ref=revision_ref,
        revision_file_sha256=hashlib.sha256(revision_content).hexdigest(),
        frozen_row_sha256=str(generation["frozen_row_content_sha256"]),
        actor="runner",
        occurred_at_utc="2026-08-15T03:03:30Z",
        idempotency_key="record-research-m3-seam",
        artifact_root=tmp_path,
        environ=AVAILABLE,
    )
    seal_generation(
        store,
        experiment_id=spec["experiment_id"],
        generation_kind="research",
        actor="runner",
        occurred_at_utc="2026-08-15T03:04:00Z",
        idempotency_key="seal-research-m3-seam",
        artifact_root=tmp_path,
        environ=AVAILABLE,
    )
    recover_terminal_projection(store, tmp_path)
    generation = store.generations(spec["experiment_id"])[0]
    validation_spec = _spec(
        research_case["sealed_dataset"],
        variants=(
            ("without", "without_concentration"),
            ("concentration", "concentration_first"),
        ),
        validation=True,
    )
    hidden_days = _trading_days("2026-09-01", VALIDATION_REQUIRED_DAYS)
    with pytest.raises(Top1LifecycleError) as wrong_leader:
        lock_challenger(
            store,
            validation_spec,
            challenger_variant_id="without",
            validation_start_trading_date=hidden_days[0],
            schedule=top1_hk_schedule_fixture(),
            actor="human",
            occurred_at_utc="2026-08-15T03:05:00Z",
            idempotency_key="wrong-leader-m3-seam",
            artifact_root=tmp_path,
            environ=AVAILABLE,
        )
    assert wrong_leader.value.reason_code == "experiment_invalid", str(
        wrong_leader.value
    )
    revision_path = tmp_path.joinpath(*revision_ref.split("/"))
    revision_path.write_bytes(b"{}\n")
    with pytest.raises(Top1LifecycleError) as tampered_revision:
        lock_challenger(
            store,
            validation_spec,
            challenger_variant_id=str(leader),
            validation_start_trading_date=hidden_days[0],
            schedule=top1_hk_schedule_fixture(),
            actor="human",
            occurred_at_utc="2026-08-15T03:05:30Z",
            idempotency_key="tampered-revision-m3-seam",
            artifact_root=tmp_path,
            environ=AVAILABLE,
        )
    assert tampered_revision.value.reason_code == "experiment_conflict"
    revision_path.write_bytes(revision_content)
    calendar_days = _trading_days("2026-09-01", 60)
    seal_market_calendar_fixture(tmp_path, calendar_days, version=CALENDAR)
    with pytest.raises(Top1LifecycleError) as missed_start:
        lock_challenger(
            store,
            validation_spec,
            challenger_variant_id=str(leader),
            validation_start_trading_date=hidden_days[0],
            schedule=top1_hk_schedule_fixture(),
            actor="human",
            occurred_at_utc="2026-09-01T03:00:00Z",
            idempotency_key="missed-start-m3-seam",
            artifact_root=tmp_path,
            environ=AVAILABLE,
        )
    assert missed_start.value.reason_code == "experiment_invalid"
    locked = lock_challenger(
        store,
        validation_spec,
        challenger_variant_id=str(leader),
        validation_start_trading_date=hidden_days[0],
        schedule=top1_hk_schedule_fixture(),
        actor="human",
        occurred_at_utc="2026-08-15T03:06:00Z",
        idempotency_key="lock-leader-m3-seam",
        artifact_root=tmp_path,
        environ=AVAILABLE,
    )
    assert locked["validation_authorization_status"] == "unconfirmed"
    assert locked["research_receipt_ref"] == generation["last_revision_ref"]
    assert locked["research_receipt_file_sha256"] == generation[
        "last_revision_file_sha256"
    ]
    relocked = lock_challenger(
        store,
        validation_spec,
        challenger_variant_id=str(leader),
        validation_start_trading_date=_trading_days("2026-10-01", 1)[0],
        schedule=top1_hk_schedule_fixture(),
        actor="human",
        occurred_at_utc="2026-08-15T03:06:30Z",
        idempotency_key="relock-leader-m3-seam",
        artifact_root=tmp_path,
        environ=AVAILABLE,
    )
    assert relocked["research_receipt_ref"] == generation["last_revision_ref"]
    locked = relocked
    with pytest.raises(Top1LifecycleError) as exc_info:
        start_validation(
            store,
            experiment_id=spec["experiment_id"],
            validation_spec_sha256=str(locked["validation_spec_sha256"]),
            actor="runner",
            occurred_at_utc="2026-08-15T03:07:00Z",
            idempotency_key="start-without-human-authorization",
            artifact_root=tmp_path,
            environ=AVAILABLE,
        )
    assert exc_info.value.reason_code == "authorization_required"


def _schedule() -> dict[str, Any]:
    return {
        "enabled": True,
        "timezone": "Asia/Hong_Kong",
        "run_window": {"start": "09:50", "end": "10:10"},
        "run_points": {"start_plus_min": 10},
    }


def _scheduler(day: str) -> dict[str, Any]:
    target = datetime.fromisoformat(f"{day}T10:00:00+08:00")
    now_utc = target.astimezone(timezone.utc) + timedelta(seconds=30)
    return {
        "should_run_scan": True,
        "scheduled_scan_target_market": target.isoformat(),
        "now_utc": now_utc.isoformat().replace("+00:00", "Z"),
    }


def test_accepts_real_w4_manifest_after_source_run_deletion(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    artifact_root = tmp_path / "artifacts"
    store = _store(tmp_path / "w4.sqlite3")
    _enable(store, artifact_root, key="enable-w4-seam")
    days = _trading_days("2026-06-08", RESEARCH_REQUIRED_DAYS)
    for index, trading_date in enumerate(days):
        seal_day_expectation(
            store,
            artifact_root,
            market="HK",
            account="lx",
            schedule=_schedule(),
            trading_date=trading_date,
            market_calendar_version=CALENDAR,
            market_calendar_sha256=CALENDAR_SHA,
            sealed_at_utc=f"{trading_date}T01:00:00Z",
            environ=AVAILABLE,
        )
        run_id = f"w4-seam-{index:02d}"
        seal_opening_candidate_fixture(
            source_root,
            run_id=run_id,
            market="HK",
            accepted_rows=[],
        )
        publication, _point = capture_scheduled_recommendation_point(
            source_root,
            run_id,
            "lx",
            _scheduler(trading_date),
            source_commit_sha=SOURCE_SHA,
        )
        assert publication == "published"
        captured = capture_recommendation_point(
            store,
            source_root,
            artifact_root,
            point_ref=(
                f"output_runs/{run_id}/accounts/lx/state/{RECOMMENDATION_POINT_FILE}"
            ),
            trading_date=trading_date,
            captured_at_utc=f"{trading_date}T02:01:00Z",
            environ=AVAILABLE,
        )
        assert captured["status"] == "published"
    facts: dict[str, Any] = {
        "schema_version": RESEARCH_WINDOW_FACTS_SCHEMA,
        "market": "HK",
        "account": "lx",
        "cutoff_at_utc": f"{days[-1]}T08:00:00Z",
        "cutoff_trading_date": days[-1],
        "market_calendar_version": CALENDAR,
        "market_calendar_ref": "evidence/hk-calendar.fixture.json",
        "market_calendar_sha256": CALENDAR_SHA,
        "trading_calendar_dates": days,
        "trading_calendar_dates_sha256": canonical_sha256(days),
        "latest_mature_trading_date": days[-1],
        "maturity_evidence_ref": "evidence/hk-maturity.fixture.json",
        "maturity_evidence_sha256": "b" * 64,
        "recommendation_point_selector": "official_scheduled_sell_put.v1",
    }
    facts["content_sha256"] = canonical_sha256(facts)
    frozen = freeze_research_dataset(
        store,
        artifact_root,
        window_facts=facts,
        environ=AVAILABLE,
    )
    assert frozen["status"] == "ready"
    manifest = json.loads(
        (artifact_root / str(frozen["dataset_ref"])).read_text(encoding="utf-8")
    )
    materialized = [
        {
            "projection_ref": point["projection_ref"],
            "projection": json.loads(
                (artifact_root / point["projection_ref"]).read_text(encoding="utf-8")
            ),
        }
        for day in manifest["days"]
        for point in day["points"]
    ]
    shutil.rmtree(source_root / "output_runs")
    spec = _spec(manifest, variants=(("same", "current_tie_break"),))
    spec["research_source"]["dataset_ref"] = frozen["dataset_ref"]
    spec["research_source"]["dataset_sha256"] = frozen["dataset_sha256"]
    envelope = {
        "schema_version": RESEARCH_EVALUATION_INPUT_SCHEMA,
        "experiment_spec": spec,
        "dataset_ref": frozen["dataset_ref"],
        "sealed_dataset": manifest,
        "ranking_projections": materialized,
    }

    with pytest.raises(ResearchEvaluationError) as exc_info:
        evaluate_research(envelope, [], _fee_contract(complete=False))
    assert exc_info.value.reason_code == "research_corpus_conflict"
