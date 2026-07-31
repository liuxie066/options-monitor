from __future__ import annotations

from copy import deepcopy

from domain.domain.combo_reconciliation import (
    AMBIGUOUS,
    EXACT_DELIVERED_CANDIDATE,
    EXACT_LIVE_CANDIDATE,
    PROPOSAL_READY,
    STRUCTURAL_ONLY,
    match_post_trade_combo_pairs,
)


BASE_TIME_MS = 1_785_312_000_000


def _lot(
    record_id: str,
    *,
    option_type: str,
    strike: int,
    trade_time_ms: int,
    expiration_ymd: str = "2026-08-21",
    contracts: int = 1,
    contracts_open: int | None = None,
    account: str = "lx",
    symbol: str = "NVDA",
    market_date: str = "2026-07-31",
) -> dict:
    return {
        "record_id": record_id,
        "open_event_id": f"open-{record_id}",
        "account": account,
        "broker": "futu",
        "runtime_environment": "test",
        "market": "US",
        "market_date": market_date,
        "symbol": symbol,
        "option_type": option_type,
        "position_side": "short" if option_type == "put" else "long",
        "contracts_original": contracts,
        "contracts_open": contracts if contracts_open is None else contracts_open,
        "currency": "USD",
        "multiplier": 100,
        "strike": strike,
        "expiration_ymd": expiration_ymd,
        "trade_time_ms": trade_time_ms,
        "strategy": "sell_put" if option_type == "put" else "long_call",
        "strategy_group_id": "",
        "leg_role": "",
    }


def _exposure(
    *,
    put_strike: int = 100,
    call_strike: int = 110,
    generated_at_ms: int = BASE_TIME_MS - 1_000,
    valid_until_ms: int = BASE_TIME_MS + 60_000,
    delivery_confirmed: bool = False,
    occurrence_id: str = "occ-1",
    exposure_id: str = "exp-1",
) -> dict:
    return {
        "candidate_exposure_id": exposure_id,
        "candidate_occurrence_id": occurrence_id,
        "account": "lx",
        "market": "US",
        "put_contract_key": {
            "underlying_symbol": "NVDA",
            "option_type": "put",
            "expiration_ymd": "2026-08-21",
            "strike": put_strike,
        },
        "call_contract_key": {
            "underlying_symbol": "NVDA",
            "option_type": "call",
            "expiration_ymd": "2026-08-21",
            "strike": call_strike,
        },
        "currency": "USD",
        "multiplier": 100,
        "generated_at_ms": generated_at_ms,
        "valid_until_ms": valid_until_ms,
        "delivery_confirmed": delivery_confirmed,
    }


def _pair() -> list[dict]:
    return [
        _lot(
            "put-1",
            option_type="put",
            strike=100,
            trade_time_ms=BASE_TIME_MS + 1_000,
        ),
        _lot(
            "call-1",
            option_type="call",
            strike=110,
            trade_time_ms=BASE_TIME_MS + 2_000,
        ),
    ]


def test_long_call_first_waits_then_reconciles_without_arrival_state() -> None:
    call = _pair()[1]
    waiting = match_post_trade_combo_pairs(lots=[call])

    assert waiting["inferences"] == []
    assert waiting["waiting_for_counterpart"] == [
        {
            "record_id": "call-1",
            "open_event_id": "open-call-1",
            "account": "lx",
            "market": "US",
            "market_date": "2026-07-31",
            "symbol": "NVDA",
            "option_type": "call",
            "position_side": "long",
            "status": "waiting_for_counterpart",
        }
    ]

    call_first = match_post_trade_combo_pairs(lots=[call, _pair()[0]])
    put_first = match_post_trade_combo_pairs(lots=list(reversed(_pair())))

    assert call_first == put_first
    assert call_first["proposal_ready_count"] == 1
    assert call_first["inferences"][0]["status"] == PROPOSAL_READY
    assert call_first["inferences"][0]["evidence_grade"] == STRUCTURAL_ONLY


def test_candidate_exposure_is_exact_only_when_it_precedes_both_fills() -> None:
    live = match_post_trade_combo_pairs(lots=_pair(), exposures=[_exposure()])
    delivered = match_post_trade_combo_pairs(
        lots=_pair(),
        exposures=[_exposure(delivery_confirmed=True)],
    )
    future = match_post_trade_combo_pairs(
        lots=_pair(),
        exposures=[
            _exposure(
                generated_at_ms=BASE_TIME_MS + 1_500,
                valid_until_ms=BASE_TIME_MS + 60_000,
            )
        ],
    )
    expired = match_post_trade_combo_pairs(
        lots=_pair(),
        exposures=[_exposure(valid_until_ms=BASE_TIME_MS + 1_500)],
    )

    assert live["inferences"][0]["evidence_grade"] == EXACT_LIVE_CANDIDATE
    assert delivered["inferences"][0]["evidence_grade"] == EXACT_DELIVERED_CANDIDATE
    assert future["inferences"][0]["evidence_grade"] == STRUCTURAL_ONLY
    assert expired["inferences"][0]["evidence_grade"] == STRUCTURAL_ONLY


def test_same_contract_multi_lot_is_ambiguous_without_sort_tie_break() -> None:
    put = _pair()[0]
    call_one = _pair()[1]
    call_two = {**call_one, "record_id": "call-2", "open_event_id": "open-call-2"}

    result = match_post_trade_combo_pairs(lots=[call_two, put, call_one])

    assert result["proposal_ready_count"] == 0
    assert result["ambiguous_count"] == 2
    assert {item["status"] for item in result["inferences"]} == {AMBIGUOUS}
    assert all(len(item["alternative_inference_ids"]) == 1 for item in result["inferences"])


def test_discrete_candidate_priority_can_outweigh_more_structural_pairs() -> None:
    put_one = _pair()[0]
    put_two = {
        **put_one,
        "record_id": "put-2",
        "open_event_id": "open-put-2",
        "strike": 105,
    }
    call_one = _pair()[1]
    call_two = {
        **call_one,
        "record_id": "call-2",
        "open_event_id": "open-call-2",
        "strike": 120,
    }
    exact = _exposure(delivery_confirmed=True)

    result = match_post_trade_combo_pairs(
        lots=[put_one, put_two, call_one, call_two],
        exposures=[exact],
    )

    exact_edge = next(
        item
        for item in result["inferences"]
        if item["put_record_id"] == "put-1" and item["call_record_id"] == "call-1"
    )
    assert exact_edge["evidence_grade"] == EXACT_DELIVERED_CANDIDATE
    assert exact_edge["status"] == PROPOSAL_READY
    assert exact_edge["selected_in_one_optimum"] is True


def test_partial_or_grouped_lot_fails_closed() -> None:
    partial = deepcopy(_pair()[0])
    partial["contracts_original"] = 2
    partial["contracts_open"] = 1
    grouped = deepcopy(_pair()[1])
    grouped["strategy_group_id"] = "existing-group"

    result = match_post_trade_combo_pairs(lots=[partial, grouped])

    assert result["eligible_lot_count"] == 0
    reasons = {
        reason
        for item in result["excluded_lots"]
        for reason in item["reason_codes"]
    }
    assert "combo_lot_not_fully_open" in reasons
    assert "combo_lot_already_grouped" in reasons


def test_staggered_pair_is_supported_but_cross_day_is_not() -> None:
    put = _lot(
        "put-1",
        option_type="put",
        strike=100,
        expiration_ymd="2026-08-21",
        trade_time_ms=BASE_TIME_MS + 1_000,
    )
    call = _lot(
        "call-1",
        option_type="call",
        strike=110,
        expiration_ymd="2026-09-18",
        trade_time_ms=BASE_TIME_MS + 2_000,
    )

    staggered = match_post_trade_combo_pairs(lots=[put, call])
    cross_day = match_post_trade_combo_pairs(
        lots=[put, {**call, "market_date": "2026-08-01"}]
    )

    assert staggered["inferences"][0]["structure_mode"] == "staggered_expiry_pair"
    assert cross_day["inferences"] == []
    assert len(cross_day["waiting_for_counterpart"]) == 2


def test_snapshot_hash_changes_when_alternatives_change() -> None:
    pair = _pair()
    unique = match_post_trade_combo_pairs(lots=pair)["inferences"][0]
    another_call = {
        **pair[1],
        "record_id": "call-2",
        "open_event_id": "open-call-2",
    }
    ambiguous = match_post_trade_combo_pairs(
        lots=[*pair, another_call]
    )["inferences"]
    same_pair = next(
        item
        for item in ambiguous
        if item["put_record_id"] == unique["put_record_id"]
        and item["call_record_id"] == unique["call_record_id"]
    )

    assert unique["input_snapshot_hash"] != same_pair["input_snapshot_hash"]
    assert unique["status"] == PROPOSAL_READY
    assert same_pair["status"] == AMBIGUOUS
