from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from domain.domain.fee_calc import calc_futu_option_fee
from src.application.shadow_replay.marking import mark_shadow_replay_dataset
from src.application.shadow_replay.settlement import settle_shadow_replay_dataset


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _episode(
    *,
    episode_id: str = "episode-put",
    option_type: str = "put",
    decision_fee: float | None = 1.5,
    willingness: bool | None = True,
    with_replacement: bool = False,
) -> dict[str, object]:
    close_now_cost = None if decision_fee is None else 0.21 * 100 + decision_fee
    episode = {
        "schema_version": "shadow_replay_close_episode.v1",
        "episode_id": episode_id,
        "account": "lx",
        "position_lot_id": f"lot-{option_type}",
        "observed_at_utc": "2026-07-23T14:00:00Z",
        "normalized_decision_facts": {
            "tier": "medium",
            "continued_willingness": willingness,
        },
        "position_identity": {
            "symbol": "NVDA",
            "option_type": option_type,
            "side": "short",
            "expiration": "2026-08-21",
            "strike": 100,
            "contract_symbol": f"NVDA260821{'P' if option_type == 'put' else 'C'}00100000",
        },
        "decision_economics": {
            "decision_ask": 0.21,
            "contracts": 1,
            "multiplier": 100,
            "decision_close_fee": decision_fee,
            "close_now_cost": close_now_cost,
            "fee_calc_status": "schedule_estimate" if decision_fee is not None else "unavailable",
            "fee_calc_basis": "futu_us_fixed_package_2026-07-22",
            "currency": "USD",
            "evidence_status": "complete" if decision_fee is not None else "incomplete",
        },
        "shadow_policy_results": {
            "P0_current": {"recommendation_state": "close"},
            "P1_semantic_split": {"recommendation_state": "review"},
            "P2_profile_aware": {"recommendation_state": "hold"},
            "P3_opportunity_required": {"recommendation_state": "review"},
        },
    }
    if with_replacement:
        episode["replacement_evidence"] = {
            "status": "review_switch",
            "symbol": "AMD",
            "contract_symbol": "AMD260821P00090000",
            "option_type": "put",
            "expiration": "2026-08-21",
            "strike": 90,
            "entry_credit": 200,
            "contracts": 1,
            "multiplier": 100,
            "currency": "USD",
            "fee_calc_status": "candidate_futu_fee",
            "open_fee": 2,
            "entry_slippage": 5,
        }
    return episode


def _dataset(tmp_path: Path, episodes: list[dict[str, object]]) -> Path:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    for name in (
        "candidate_snapshots.jsonl",
        "filter_decisions.jsonl",
        "rank_snapshots.jsonl",
        "mark_path_snapshots.jsonl",
        "outcome_facts.jsonl",
        "close_decision_marks.jsonl",
        "close_decision_outcomes.jsonl",
    ):
        (dataset / name).write_text("", encoding="utf-8")
    _write_jsonl(dataset / "close_decision_episodes.jsonl", episodes)
    return dataset


def _quote_root(
    tmp_path: Path,
    *,
    option_type: str = "put",
    bid: float | None = 0.09,
    ask: float | None = 0.10,
    spot: float = 110,
    dte: int = 28,
) -> Path:
    root = tmp_path / "required_data"
    _write_csv(
        root / "parsed" / "NVDA_required_data.csv",
        [
            {
                "symbol": "NVDA",
                "contract_symbol": f"NVDA260821{'P' if option_type == 'put' else 'C'}00100000",
                "option_type": option_type,
                "expiration": "2026-08-21",
                "strike": 100,
                "bid": "" if bid is None else bid,
                "ask": "" if ask is None else ask,
                "mid": "" if bid is None or ask is None else (bid + ask) / 2,
                "spot": spot,
                "dte": dte,
                "multiplier": 100,
                "currency": "USD",
            }
        ],
    )
    return root


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        ("2026-07-24T14:00:00Z", "1d"),
        ("2026-07-25T14:00:00Z", "1d"),
        ("2026-07-26T14:00:00Z", "3d"),
        ("2026-07-27T14:00:00Z", "3d"),
        ("2026-07-28T14:00:00Z", None),
        ("2026-07-30T14:00:00Z", "7d"),
        ("2026-08-01T14:00:00Z", "7d"),
        ("2026-08-06T14:00:00Z", "14d"),
        ("2026-08-09T14:00:00Z", "14d"),
    ],
)
def test_close_mark_uses_exact_calendar_horizon_windows(
    tmp_path: Path,
    as_of: str,
    expected: str | None,
) -> None:
    dataset = _dataset(tmp_path, [_episode()])
    required = _quote_root(tmp_path)

    result = mark_shadow_replay_dataset(
        dataset=dataset,
        required_data_root=required,
        as_of=as_of,
        write=False,
    )

    marks = result["generated_close_marks"]
    assert [mark["horizon"] for mark in marks] == ([] if expected is None else [expected])
    assert result["summary"]["close_mark_outside_window_count"] == (1 if expected is None else 0)
    if marks:
        assert marks[0]["mark_time_basis"] == "operator_asserted_as_of"
        assert marks[0]["point_in_time_status"] == "unverified_operator_as_of"
    assert (dataset / "close_decision_marks.jsonl").read_text(encoding="utf-8") == ""


def test_close_mark_does_not_relabel_post_expiry_spot_as_expiry_evidence(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [_episode()])
    required = _quote_root(tmp_path, bid=None, ask=None, spot=110, dte=0)

    result = mark_shadow_replay_dataset(
        dataset=dataset,
        required_data_root=required,
        as_of="2026-08-22T14:00:00Z",
        write=False,
    )

    assert result["generated_close_marks"] == []
    assert result["summary"]["close_mark_outside_window_count"] == 1


def test_operator_asserted_historical_mark_cannot_settle_an_outcome(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [_episode()])
    required = _quote_root(tmp_path)
    mark_shadow_replay_dataset(
        dataset=dataset,
        required_data_root=required,
        as_of="2026-07-24T14:00:00Z",
        write=True,
    )

    one_day = next(
        row
        for row in settle_shadow_replay_dataset(dataset=dataset, write=False)[
            "generated_close_outcomes"
        ]
        if row["outcome_kind"] == "horizon_1d"
    )

    assert one_day["evidence_status"] == "inconclusive"
    assert one_day["inconclusive_reason"] == "mark_point_in_time_unverified"


def test_close_horizon_outcome_uses_incremental_cost_formula_and_shared_policy_facts(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path, [_episode()])
    required = _quote_root(tmp_path, ask=0.10, bid=0.09)
    marking = mark_shadow_replay_dataset(
        dataset=dataset,
        required_data_root=required,
        as_of="2026-07-24T14:00:00Z",
        write=True,
        mark_time_basis="collection_time",
        quote_collection_source="opend",
    )
    settlement = settle_shadow_replay_dataset(dataset=dataset, write=True)
    outcomes = _read_jsonl(dataset / "close_decision_outcomes.jsonl")
    one_day = next(row for row in outcomes if row["outcome_kind"] == "horizon_1d")
    future_fee = calc_futu_option_fee("USD", 0.10, contracts=1, multiplier=100, is_sell=False)
    expected = 22.5 - (10 + future_fee)

    assert marking["summary"]["usable_close_mark_count"] == 1
    assert one_day["evidence_status"] == "usable"
    assert one_day["hold_to_horizon_incremental"] == pytest.approx(expected)
    assert one_day["hold_vs_close_regret"] == pytest.approx(expected)
    assert one_day["close_now_incremental"] == 0
    assert one_day["policy_recommendations"] == {
        "P0_current": "close",
        "P1_semantic_split": "review",
        "P2_profile_aware": "hold",
        "P3_opportunity_required": "review",
    }
    assert settlement["summary"]["generated_close_outcome_count"] == 5
    assert settlement["summary"]["usable_close_outcome_count"] == 1
    assert settlement["summary"]["inconclusive_close_outcome_count"] == 4


def test_close_outcomes_keep_missing_mark_and_fee_explicitly_inconclusive(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [_episode(decision_fee=None)])
    required = _quote_root(tmp_path)
    mark_shadow_replay_dataset(
        dataset=dataset,
        required_data_root=required,
        as_of="2026-07-24T14:00:00Z",
        write=True,
        mark_time_basis="collection_time",
        quote_collection_source="opend",
    )

    settlement = settle_shadow_replay_dataset(dataset=dataset, write=False)
    by_kind = {
        row["outcome_kind"]: row
        for row in settlement["generated_close_outcomes"]
    }

    assert by_kind["horizon_1d"]["inconclusive_reason"] == "decision_close_cost_incomplete"
    assert by_kind["horizon_3d"]["inconclusive_reason"] == "no_usable_mark_in_window"
    assert all(row["hold_vs_close_regret"] is None for row in by_kind.values())


def test_p3_replacement_uses_same_horizon_and_all_switch_costs(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [_episode(with_replacement=True)])
    required = _quote_root(tmp_path, ask=0.10, bid=0.09)
    _write_csv(
        required / "parsed" / "AMD_required_data.csv",
        [
            {
                "symbol": "AMD",
                "contract_symbol": "AMD260821P00090000",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": 90,
                "bid": 0.95,
                "ask": 1.0,
                "mid": 0.975,
                "spot": 100,
                "dte": 28,
                "multiplier": 100,
                "currency": "USD",
            }
        ],
    )
    mark_shadow_replay_dataset(
        dataset=dataset,
        required_data_root=required,
        as_of="2026-07-24T14:00:00Z",
        write=True,
        mark_time_basis="collection_time",
        quote_collection_source="opend",
    )

    one_day = next(
        row
        for row in settle_shadow_replay_dataset(dataset=dataset, write=False)["generated_close_outcomes"]
        if row["outcome_kind"] == "horizon_1d"
    )
    current_future_fee = calc_futu_option_fee(
        "USD", 0.10, contracts=1, multiplier=100, is_sell=False
    )
    replacement_exit_fee = calc_futu_option_fee(
        "USD", 1.0, contracts=1, multiplier=100, is_sell=False
    )
    hold_incremental = 22.5 - (10 + current_future_fee)
    replacement_incremental = 200 - 100 - 2 - replacement_exit_fee - 5

    assert one_day["replacement_outcome_status"] == "usable"
    assert one_day["replacement_incremental"] == pytest.approx(replacement_incremental)
    assert one_day["switch_vs_close_incremental"] == pytest.approx(replacement_incremental)
    assert one_day["switch_vs_hold_incremental"] == pytest.approx(
        replacement_incremental - hold_incremental
    )


def test_expiry_mark_settles_only_expired_worthless_without_ledger_inference(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [_episode()])
    required = _quote_root(tmp_path, bid=None, ask=None, spot=110, dte=0)
    mark_shadow_replay_dataset(
        dataset=dataset,
        required_data_root=required,
        as_of="2026-08-21T14:00:00Z",
        write=True,
        mark_time_basis="collection_time",
        quote_collection_source="opend",
    )

    terminal = next(
        row
        for row in settle_shadow_replay_dataset(dataset=dataset, write=False)["generated_close_outcomes"]
        if row["outcome_kind"] == "terminal"
    )

    assert terminal["evidence_status"] == "usable"
    assert terminal["outcome"] == "expired_worthless"
    assert terminal["future_option_close_cost"] == 0
    assert terminal["hold_to_horizon_incremental"] == pytest.approx(22.5)


def test_expiry_outcome_uses_same_mark_for_p3_replacement(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [_episode(with_replacement=True)])
    required = _quote_root(tmp_path, bid=None, ask=None, spot=110, dte=0)
    _write_csv(
        required / "parsed" / "AMD_required_data.csv",
        [
            {
                "symbol": "AMD",
                "contract_symbol": "AMD260821P00090000",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": 90,
                "bid": 0.05,
                "ask": 0.06,
                "mid": 0.055,
                "spot": 100,
                "dte": 0,
                "multiplier": 100,
                "currency": "USD",
            }
        ],
    )
    mark_shadow_replay_dataset(
        dataset=dataset,
        required_data_root=required,
        as_of="2026-08-21T14:00:00Z",
        write=True,
        mark_time_basis="collection_time",
        quote_collection_source="opend",
    )

    terminal = next(
        row
        for row in settle_shadow_replay_dataset(dataset=dataset, write=False)[
            "generated_close_outcomes"
        ]
        if row["outcome_kind"] == "terminal"
    )
    replacement_exit_fee = calc_futu_option_fee(
        "USD", 0.06, contracts=1, multiplier=100, is_sell=False
    )

    assert terminal["replacement_outcome_status"] == "usable"
    assert terminal["replacement_incremental"] == pytest.approx(
        200 - 6 - 2 - replacement_exit_fee - 5
    )


@pytest.mark.parametrize(
    ("option_type", "event_type", "expected_outcome"),
    [
        ("put", "assignment", "assigned"),
        ("call", "exercise", "called_away"),
    ],
)
def test_assignment_and_called_away_require_lifecycle_incremental_pnl_for_money_outcome(
    tmp_path: Path,
    option_type: str,
    event_type: str,
    expected_outcome: str,
) -> None:
    dataset = _dataset(
        tmp_path,
        [_episode(episode_id=f"episode-{option_type}", option_type=option_type)],
    )
    lifecycle = tmp_path / "lifecycle.jsonl"
    observed_ms = int(datetime(2026, 7, 24, tzinfo=timezone.utc).timestamp() * 1000)
    _write_jsonl(
        lifecycle,
        [
            {
                "account": "lx",
                "target_lot_id": f"lot-{option_type}",
                "event_type": event_type,
                "event_time_ms": observed_ms,
                "contracts": 1,
                "lifecycle_pnl_net": 999,
            }
        ],
    )

    terminal = next(
        row
        for row in settle_shadow_replay_dataset(
            dataset=dataset,
            lifecycle_paths=[lifecycle],
            write=False,
        )["generated_close_outcomes"]
        if row["outcome_kind"] == "terminal"
    )

    assert terminal["outcome"] == expected_outcome
    assert terminal["evidence_status"] == "inconclusive"
    assert terminal["inconclusive_reason"] == "lifecycle_incremental_pnl_missing"
    assert terminal["willingness_alignment"] == "aligned"
    assert terminal["hold_vs_close_regret"] is None


def test_assignment_incremental_pnl_must_be_bound_to_the_episode(tmp_path: Path) -> None:
    episode = _episode()
    dataset = _dataset(tmp_path, [episode])
    lifecycle = tmp_path / "lifecycle.jsonl"
    observed_ms = int(datetime(2026, 7, 24, tzinfo=timezone.utc).timestamp() * 1000)
    event = {
        "account": "lx",
        "target_lot_id": "lot-put",
        "event_type": "assignment",
        "event_time_ms": observed_ms,
        "contracts": 1,
        "lifecycle_pnl_after_decision": 50,
    }
    _write_jsonl(lifecycle, [event])

    unbound = next(
        row
        for row in settle_shadow_replay_dataset(
            dataset=dataset,
            lifecycle_paths=[lifecycle],
            write=False,
        )["generated_close_outcomes"]
        if row["outcome_kind"] == "terminal"
    )
    assert unbound["evidence_status"] == "inconclusive"
    assert unbound["inconclusive_reason"] == "lifecycle_incremental_pnl_unbound"

    event["episode_id"] = episode["episode_id"]
    _write_jsonl(lifecycle, [event])
    bound = next(
        row
        for row in settle_shadow_replay_dataset(
            dataset=dataset,
            lifecycle_paths=[lifecycle],
            write=False,
        )["generated_close_outcomes"]
        if row["outcome_kind"] == "terminal"
    )
    assert bound["evidence_status"] == "usable"
    assert bound["hold_to_horizon_incremental"] == 50


def test_lifecycle_future_close_uses_actual_price_and_fee_without_sunk_premium(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path, [_episode()])
    lifecycle = tmp_path / "lifecycle.json"
    event_time_ms = int(datetime(2026, 7, 30, tzinfo=timezone.utc).timestamp() * 1000)
    lifecycle.write_text(
        json.dumps(
            [
                {
                    "account": "lx",
                    "target_lot_id": "lot-put",
                    "event_type": "close",
                    "event_time_ms": event_time_ms,
                    "contracts": 1,
                    "price": 0.30,
                    "fees": 2.0,
                    "multiplier": 100,
                }
            ]
        ),
        encoding="utf-8",
    )

    terminal = next(
        row
        for row in settle_shadow_replay_dataset(
            dataset=dataset,
            lifecycle_paths=[lifecycle],
            write=False,
        )["generated_close_outcomes"]
        if row["outcome_kind"] == "terminal"
    )

    assert terminal["evidence_status"] == "usable"
    assert terminal["outcome"] == "closed_later"
    assert terminal["future_option_close_cost"] == 32
    assert terminal["hold_to_horizon_incremental"] == pytest.approx(-9.5)
    assert "premium" not in terminal


def test_later_settlement_without_lifecycle_does_not_downgrade_usable_terminal(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path, [_episode()])
    lifecycle = tmp_path / "lifecycle.jsonl"
    event_time_ms = int(datetime(2026, 7, 30, tzinfo=timezone.utc).timestamp() * 1000)
    _write_jsonl(
        lifecycle,
        [
            {
                "account": "lx",
                "target_lot_id": "lot-put",
                "event_type": "close",
                "event_time_ms": event_time_ms,
                "contracts": 1,
                "price": 0.30,
                "fees": 2.0,
                "multiplier": 100,
            }
        ],
    )
    settle_shadow_replay_dataset(
        dataset=dataset,
        lifecycle_paths=[lifecycle],
        write=True,
    )

    settle_shadow_replay_dataset(dataset=dataset, write=True)
    terminal = next(
        row
        for row in _read_jsonl(dataset / "close_decision_outcomes.jsonl")
        if row["outcome_kind"] == "terminal"
    )

    assert terminal["evidence_status"] == "usable"
    assert terminal["outcome"] == "closed_later"
    assert terminal["future_option_close_cost"] == 32


def test_lifecycle_quantity_must_match_the_decision_lot_exactly(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [_episode()])
    lifecycle = tmp_path / "lifecycle.jsonl"
    event_time_ms = int(datetime(2026, 7, 30, tzinfo=timezone.utc).timestamp() * 1000)
    _write_jsonl(
        lifecycle,
        [
            {
                "account": "lx",
                "target_lot_id": "lot-put",
                "event_type": "close",
                "event_time_ms": event_time_ms,
                "contracts": 2,
                "price": 0.30,
                "fees": 2.0,
                "multiplier": 100,
            }
        ],
    )

    terminal = next(
        row
        for row in settle_shadow_replay_dataset(
            dataset=dataset,
            lifecycle_paths=[lifecycle],
            write=False,
        )["generated_close_outcomes"]
        if row["outcome_kind"] == "terminal"
    )

    assert terminal["evidence_status"] == "inconclusive"
    assert terminal["inconclusive_reason"] == "lifecycle_contract_quantity_incomplete"


def test_itm_expiry_without_canonical_lifecycle_is_inconclusive(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [_episode(option_type="call", episode_id="episode-call")])
    required = _quote_root(tmp_path, option_type="call", bid=None, ask=None, spot=120, dte=0)
    mark_shadow_replay_dataset(
        dataset=dataset,
        required_data_root=required,
        as_of="2026-08-21T14:00:00Z",
        write=True,
        mark_time_basis="collection_time",
        quote_collection_source="opend",
    )

    terminal = next(
        row
        for row in settle_shadow_replay_dataset(dataset=dataset, write=False)["generated_close_outcomes"]
        if row["outcome_kind"] == "terminal"
    )

    assert terminal["evidence_status"] == "inconclusive"
    assert terminal["inconclusive_reason"] == "itm_expiration_requires_canonical_lifecycle_fact"
    assert terminal["hold_vs_close_regret"] is None
