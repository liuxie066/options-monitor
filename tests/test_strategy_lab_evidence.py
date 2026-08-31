from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.fee_calc import calc_futu_hk_terminal_fee
from src.application.strategy_lab.evidence import (
    StrategyLabEvidenceError,
    build_expiry_close_query,
    build_single_recommendation_result,
    collect_research_fill_evidence,
    load_research_projection,
    next_missing_research_evidence,
    publish_evidence_artifact,
    resolve_expiry_outcome,
)


def _fx(rate: str, *, expiration: str | None = None) -> dict[str, object]:
    fact = {
        "fact_id": f"fx-{expiration or 'open'}",
        "base_currency": "HKD",
        "quote_currency": "CNY",
        "rate": rate,
        "rate_kind": "spot",
        "effective_at_ms": 1_777_593_600_000,
        "observed_at_ms": 1_777_593_600_000,
        "source": "fixture",
        "source_id": f"source-{expiration or 'open'}",
        "revision": 1,
        "supersedes_fact_id": None,
        "quality": {},
        "raw": {},
    }
    binding: dict[str, object] = {
        "fact": fact,
        "fact_ref": {
            "kind": "fx_rate" if expiration is not None else "formal_point_fx_rate",
            "fact_id": fact["fact_id"],
        },
        "fact_sha256": canonical_sha256(fact),
    }
    if expiration is not None:
        binding.update(
            {
                "expiration": expiration,
                "currency": "HKD",
                "observation_start_ms": 1_777_593_600_000,
            }
        )
    else:
        binding["source_fact_sha256"] = "a" * 64
    return binding


def _arm(arm_id: str = "baseline") -> dict[str, object]:
    return {
        "arm_id": arm_id,
        "kind": "baseline" if arm_id == "baseline" else "challenger",
        "near_return_threshold": None if arm_id == "baseline" else 0.002,
        "candidate_id": f"candidate-{arm_id}",
        "candidate": {
            "candidate_id": f"candidate-{arm_id}",
            "symbol": "9992.HK",
            "contract_symbol": "HK.POP260828P127500",
            "option_type": "put",
            "expiration": "2026-08-28",
            "strike": 127.5,
            "currency": "HKD",
            "sell_limit": 1.0,
            "price_tick": 0.01,
            "multiplier": 100,
            "net_premium": 85.0,
        },
    }


def _spec() -> dict[str, object]:
    return {
        "evaluator_behavior_sha256": "d" * 64,
        "fee_plan": {"receipt": _fee_plan()},
        "history_k_authority": {
            "probe_request": {"opend_binding": {"host": "127.0.0.1", "port": 11111}},
            "probe_sha256": "b" * 64,
            "receipt": {"content_sha256": "c" * 64},
        },
        "research_window": {
            "status": "available",
            "sessions": [
                {
                    "trading_date": "2026-08-03",
                    "market_calendar_binding": {
                        "session": {
                            "trading_date": "2026-08-03",
                            "trade_date_type": "WHOLE",
                        }
                    },
                    "points": [
                        {
                            "recommendation_point_id": "e" * 64,
                            "scheduled_scan_target_market": "2026-08-03T01:40:15Z",
                            "recommendation_available_at_utc": "2026-08-03T01:40:15Z",
                            "opening_fx_binding": _fx("0.92"),
                            "arms": [_arm(), _arm("challenger_0.002")],
                        }
                    ],
                }
            ],
        },
        "terminal_fx_bindings": [_fx("0.93", expiration="2026-08-28")],
    }


def _history_query() -> dict[str, object]:
    return load_research_projection(_spec())["history_k_queries"][0]["query"]


def _fee_plan() -> dict[str, object]:
    return {
        "commission_free": True,
        "platform_fee": 15.0,
        "fee_plan_ref": "fee-plan-fixture",
    }


def _evidence_ref(kind: str) -> dict[str, str]:
    return {
        "artifact_ref": f"evidence/{kind}.json",
        "artifact_sha256": "f" * 64,
    }


class _PagedGateway:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def request_history_kline(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        return self.pages[len(self.calls) - 1]


def test_projection_deduplicates_shared_queries_and_freezes_minute_bounds() -> None:
    projection = load_research_projection(_spec())

    assert projection["expected_points"] == [
        {
            "trading_day": "2026-08-03",
            "recommendation_point_id": "e" * 64,
        }
    ]
    assert len(projection["arms"]) == 2
    assert len(projection["history_k_queries"]) == 1
    assert len(projection["expiry_close_queries"]) == 1
    query = projection["history_k_queries"][0]["query"]
    assert query["window_start_utc"] == "2026-08-03T01:41:00Z"
    assert query["window_end_utc"] == "2026-08-03T08:00:00Z"


def test_projection_starts_after_recommendation_is_available() -> None:
    spec = _spec()
    point = spec["research_window"]["sessions"][0]["points"][0]
    point["recommendation_available_at_utc"] = "2026-08-03T01:42:15Z"

    query = load_research_projection(spec)["history_k_queries"][0]["query"]

    assert query["window_start_utc"] == "2026-08-03T01:43:00Z"


def test_pre_recommendation_crossing_cannot_fill(tmp_path: Path) -> None:
    spec = _spec()
    point = spec["research_window"]["sessions"][0]["points"][0]
    point["recommendation_available_at_utc"] = "2026-08-03T01:42:15Z"
    projection = load_research_projection(spec)
    query = projection["history_k_queries"][0]
    evidence = collect_research_fill_evidence(
        _PagedGateway(
            [
                {
                    "data": [
                        {"time_key": "2026-08-03 09:41:00", "high": 1.02, "volume": 10},
                        {"time_key": "2026-08-03 09:43:00", "high": 1.00, "volume": 10},
                    ],
                    "page_req_key": None,
                }
            ]
        ),
        query["query"],
        limiter_root=tmp_path,
        window_sec=30,
        max_calls=20,
    )

    action = next_missing_research_evidence(
        spec,
        [
            {
                "observation_key": query["observation_key"],
                "payload": evidence,
                **_evidence_ref("history"),
            }
        ],
        tmp_path,
    )

    assert evidence["bars"] == [{"time_utc": "2026-08-03T01:43:00Z", "high": 1.0, "volume": 10.0}]
    assert action["action"] == "derive_research_fill"
    assert action["payload"]["status"] == "no_fill"


def test_projection_rejects_availability_at_session_end() -> None:
    spec = _spec()
    point = spec["research_window"]["sessions"][0]["points"][0]
    point["recommendation_available_at_utc"] = "2026-08-03T08:00:00Z"

    with pytest.raises(StrategyLabEvidenceError) as raised:
        load_research_projection(spec)

    assert raised.value.reason_code == "research_evidence_invalid"


def test_provider_binding_is_part_of_query_and_artifact_identity(tmp_path: Path) -> None:
    first_spec = _spec()
    first_projection = load_research_projection(first_spec)
    first = first_projection["history_k_queries"][0]
    publish_evidence_artifact(
        tmp_path,
        "history_k",
        first["query_sha256"],
        {"status": "no_fill"},
        query=first["query"],
        observed_at_utc="2026-08-30T12:00:00Z",
        producer_source_commit_sha="1" * 40,
    )
    second_spec = deepcopy(first_spec)
    second_spec["history_k_authority"]["probe_request"]["opend_binding"]["port"] = 22222
    second_projection = load_research_projection(second_spec)
    action = next_missing_research_evidence(second_spec, [], tmp_path)

    assert action["action"] == "collect_history_k"
    assert action["query_sha256"] != first["query_sha256"]
    assert (
        second_projection["expiry_close_queries"][0]["query_sha256"]
        != first_projection["expiry_close_queries"][0]["query_sha256"]
    )


def test_evaluator_behavior_is_part_of_query_and_artifact_identity(tmp_path: Path) -> None:
    first_spec = _spec()
    first_projection = load_research_projection(first_spec)
    first = first_projection["history_k_queries"][0]
    publish_evidence_artifact(
        tmp_path,
        "history_k",
        first["query_sha256"],
        {"status": "no_fill"},
        query=first["query"],
        observed_at_utc="2026-08-30T12:00:00Z",
        producer_source_commit_sha="1" * 40,
    )
    second_spec = deepcopy(first_spec)
    second_spec["evaluator_behavior_sha256"] = "e" * 64
    second_projection = load_research_projection(second_spec)

    action = next_missing_research_evidence(second_spec, [], tmp_path)

    assert action["action"] == "collect_history_k"
    assert action["query_sha256"] != first["query_sha256"]
    assert (
        second_projection["expiry_close_queries"][0]["query_sha256"]
        != first_projection["expiry_close_queries"][0]["query_sha256"]
    )


def test_history_k_full_pagination_and_zero_volume_no_fill(tmp_path: Path) -> None:
    gateway = _PagedGateway(
        [
            {
                "data": [{"time_key": "2026-08-03 09:41:00", "high": 1.02, "volume": 0}],
                "page_req_key": "next",
            },
            {
                "data": [{"time_key": "2026-08-03 09:42:00", "high": 1.00, "volume": 20}],
                "page_req_key": None,
            },
        ]
    )

    evidence = collect_research_fill_evidence(
        gateway,
        _history_query(),
        limiter_root=tmp_path,
        window_sec=30,
        max_calls=20,
    )

    assert evidence["status"] == "available"
    assert evidence["page_count"] == 2
    assert evidence["bars"][0]["volume"] == 0.0
    assert len(gateway.calls) == 2


@pytest.mark.parametrize(
    "pages",
    [
        [
            {
                "data": [
                    {"time_key": "2026-08-03 09:42:00", "high": 1.02, "volume": 1},
                    {"time_key": "2026-08-03 09:41:00", "high": 1.02, "volume": 1},
                ],
                "page_req_key": None,
            }
        ],
        [
            {
                "data": [{"time_key": "2026-08-03 09:41:00", "high": 1.02, "volume": 1}],
                "page_req_key": "same",
            },
            {
                "data": [{"time_key": "2026-08-03 09:42:00", "high": 1.02, "volume": 1}],
                "page_req_key": "same",
            },
        ],
    ],
)
def test_history_k_invalid_order_or_unfinished_pagination_is_not_evaluable(
    tmp_path: Path,
    pages: list[dict[str, object]],
) -> None:
    result = collect_research_fill_evidence(
        _PagedGateway(pages),
        _history_query(),
        limiter_root=tmp_path,
        window_sec=30,
        max_calls=20,
    )
    assert result["status"] == "not_evaluable"


def test_next_action_adopts_existing_artifact_then_derives_fill(tmp_path: Path) -> None:
    projection = load_research_projection(_spec())
    query = projection["history_k_queries"][0]
    payload = {
        "status": "available",
        "pagination_complete": True,
        "page_count": 1,
        "bar_count": 1,
        "bars": [{"time_utc": "2026-08-03T01:41:00Z", "high": 1.02, "volume": 5.0}],
    }
    published = publish_evidence_artifact(
        tmp_path,
        "history_k",
        query["query_sha256"],
        payload,
        query=query["query"],
        observed_at_utc="2026-08-30T12:00:00Z",
        producer_source_commit_sha="1" * 40,
    )

    action = next_missing_research_evidence(_spec(), [], tmp_path)
    assert action["action"] == "bind_artifact"
    assert action["artifact"]["artifact_sha256"] == published["artifact_sha256"]

    observation = {
        "observation_key": query["observation_key"],
        "payload": published["payload"],
        "artifact_ref": published["artifact_ref"],
        "artifact_sha256": published["artifact_sha256"],
    }
    action = next_missing_research_evidence(_spec(), [observation], tmp_path)
    assert action["action"] == "derive_research_fill"
    assert action["payload"] == {
        "status": "simulated_fill",
        "fill_price": 1.0,
        "crossing_price": 1.01,
        "bar_time_utc": "2026-08-03T01:41:00Z",
        "fill_evidence_ref": {
            "artifact_ref": published["artifact_ref"],
            "artifact_sha256": published["artifact_sha256"],
        },
        "simulated_fill_not_real_trade": True,
    }


def test_zero_volume_crossing_does_not_simulate_a_fill(tmp_path: Path) -> None:
    projection = load_research_projection(_spec())
    query = projection["history_k_queries"][0]
    action = next_missing_research_evidence(
        _spec(),
        [
            {
                "observation_key": query["observation_key"],
                "payload": {
                    "status": "available",
                    "bars": [
                        {"time_utc": "2026-08-03T01:41:00Z", "high": 1.02, "volume": 0.0},
                        {"time_utc": "2026-08-03T01:42:00Z", "high": 1.00, "volume": 20.0},
                    ],
                },
                **_evidence_ref("history"),
            }
        ],
        tmp_path,
    )
    assert action["action"] == "derive_research_fill"
    assert action["payload"] == {
        "status": "no_fill",
        "fill_evidence_ref": _evidence_ref("history"),
        "simulated_fill_not_real_trade": True,
    }


def test_artifact_publish_is_idempotent_and_conflicting_content_fails(tmp_path: Path) -> None:
    query = _history_query()
    digest = canonical_sha256(query)
    kwargs = {
        "query": query,
        "observed_at_utc": "2026-08-30T12:00:00Z",
        "producer_source_commit_sha": "1" * 40,
    }
    first = publish_evidence_artifact(tmp_path, "history_k", digest, {"status": "no_fill"}, **kwargs)
    second = publish_evidence_artifact(tmp_path, "history_k", digest, {"status": "no_fill"}, **kwargs)
    assert first == second

    with pytest.raises(StrategyLabEvidenceError) as raised:
        publish_evidence_artifact(tmp_path, "history_k", digest, {"status": "available"}, **kwargs)
    assert raised.value.reason_code == "research_evidence_immutable_conflict"


def test_expiry_close_and_single_result_use_only_frozen_fx_and_fee(tmp_path: Path) -> None:
    projection = load_research_projection(_spec())
    query = projection["expiry_close_queries"][0]["query"]
    authority = {
        "provider": "futu_opend",
        "endpoint": "history_kline",
        "opend_binding": {"host": "127.0.0.1", "port": 11111},
    }
    assert query["provider_source"] == {
        **authority,
        "source_authority_sha256": canonical_sha256(authority),
    }
    gateway = SimpleNamespace(
        get_exact_expiration_close=lambda **_kwargs: {
            "code": "HK.09992",
            "expiration": "2026-08-28",
            "close": 120.0,
        }
    )

    outcome = resolve_expiry_outcome(
        gateway,
        query,
        _fee_plan(),
        query["terminal_fx_binding"],
        limiter_root=tmp_path,
        window_sec=30,
        max_calls=20,
    )
    result = build_single_recommendation_result(
        projection["arms"][0]["arm"],
        {
            "status": "simulated_fill",
            "fill_price": 1.0,
            "bar_time_utc": "2026-08-03T01:41:00Z",
            "fill_evidence_ref": _evidence_ref("history"),
        },
        {**outcome, "outcome_evidence_ref": _evidence_ref("expiry")},
    )

    assert outcome["status"] == "available"
    assert outcome["terminal_kind"] == "assignment"
    assert outcome["terminal_fee"] == calc_futu_hk_terminal_fee(
        "assignment",
        order_price=127.5,
        shares=100,
        contracts=1,
        account_fee_plan=_fee_plan(),
    )
    assert result["status"] == "available"
    assert result["economic_pnl_cny"] < 0
    assert result["return_capital_basis_cny"] > 0
    assert result["holding_calendar_days"] == 25


def test_validation_expiry_query_rebinds_snapshot_source_to_history_kline() -> None:
    source = {
        "provider": "futu_opend",
        "endpoint": "market_snapshot",
        "opend_binding": {"host": "127.0.0.1", "port": 11111},
    }
    source["source_authority_sha256"] = canonical_sha256(source)
    query = build_expiry_close_query(
        _arm(),
        _fx("0.92", expiration="2026-08-28"),
        _fee_plan(),
        source,
        "d" * 64,
    )
    authority = {
        "provider": "futu_opend",
        "endpoint": "history_kline",
        "opend_binding": {"host": "127.0.0.1", "port": 11111},
    }
    assert query["provider_source"] == {
        **authority,
        "source_authority_sha256": canonical_sha256(authority),
    }


def test_observed_fill_reuses_standard_result_without_simulation_declaration(
    tmp_path: Path,
) -> None:
    projection = load_research_projection(_spec())
    query = projection["expiry_close_queries"][0]["query"]
    outcome = resolve_expiry_outcome(
        SimpleNamespace(
            get_exact_expiration_close=lambda **_kwargs: {
                "code": "HK.09992",
                "expiration": "2026-08-28",
                "close": 130.0,
            }
        ),
        query,
        _fee_plan(),
        query["terminal_fx_binding"],
        limiter_root=tmp_path,
        window_sec=30,
        max_calls=20,
    )
    result = build_single_recommendation_result(
        projection["arms"][0]["arm"],
        {
            "status": "observed_fill",
            "fill_price": 1.0,
            "fill_time": "2026-08-03T01:41:00Z",
            "fill_evidence_ref": _evidence_ref("hidden"),
        },
        {**outcome, "outcome_evidence_ref": _evidence_ref("expiry")},
    )

    assert result["status"] == "available"
    assert result["fill_status"] == "observed_fill"
    assert result["quote_evidence_not_broker_execution"] is True
    assert "simulated_fill_not_real_trade" not in result


def test_no_fill_is_zero_and_terminal_gap_is_not_evaluable(tmp_path: Path) -> None:
    projection = load_research_projection(_spec())
    arm = projection["arms"][0]["arm"]
    assert build_single_recommendation_result(
        arm,
        {"status": "no_fill", "fill_evidence_ref": _evidence_ref("history")},
        None,
    ) == {
        "recommendation_point_id": "e" * 64,
        "trading_day": "2026-08-03",
        "arm": "baseline",
        "recipe_id": "sell_put_option_position_concentration",
        "variant_id": None,
        "near_return_threshold": None,
        "arm_id": "baseline",
        "candidate_id": "candidate-baseline",
        "contract_symbol": "HK.POP260828P127500",
        "candidate_ref": {
            "candidate_id": "candidate-baseline",
            "contract_symbol": "HK.POP260828P127500",
        },
        "safety_status": "pass",
        "fill_evidence_ref": _evidence_ref("history"),
        "status": "no_fill",
        "fill_status": "no_fill",
        "fill_price": None,
        "fill_time": None,
        "outcome_status": "not_applicable",
        "outcome_evidence_ref": None,
        "economic_pnl_cny": 0.0,
        "annualized_return": 0.0,
        "return_capital_basis_cny": None,
        "holding_calendar_days": None,
        "reason_codes": [],
        "simulated_fill_not_real_trade": True,
    }
    query = projection["expiry_close_queries"][0]["query"]
    outcome = resolve_expiry_outcome(
        SimpleNamespace(get_exact_expiration_close=lambda **_kwargs: None),
        query,
        _fee_plan(),
        query["terminal_fx_binding"],
        limiter_root=tmp_path,
        window_sec=30,
        max_calls=20,
    )
    result = build_single_recommendation_result(
        arm,
        {
            "status": "simulated_fill",
            "fill_price": 1.0,
            "bar_time_utc": "2026-08-03T01:41:00Z",
            "fill_evidence_ref": _evidence_ref("history"),
        },
        {**outcome, "outcome_evidence_ref": _evidence_ref("expiry")},
    )
    assert outcome == {"status": "not_evaluable", "reason_code": "research_terminal_gap"}
    assert result["status"] == "not_evaluable"


def test_low_priority_deferral_does_not_call_provider(tmp_path: Path) -> None:
    gateway = _PagedGateway([])
    with pytest.raises(StrategyLabEvidenceError) as raised:
        collect_research_fill_evidence(
            gateway,
            _history_query(),
            limiter_root=tmp_path,
            window_sec=30,
            max_calls=1,
        )
    assert raised.value.reason_code == "opend_low_priority_deferred"
    assert gateway.calls == []
