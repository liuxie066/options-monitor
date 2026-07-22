from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _close_row(*, account: str, lot_id: str, tier: str = "medium") -> dict[str, object]:
    return {
        "account": account,
        "position_lot_id": lot_id,
        "symbol": "NVDA",
        "option_type": "put",
        "expiration": "2026-08-21",
        "strike": 100,
        "position_side": "short",
        "strategy_family": "sell_put",
        "strategy_profile": "insurance_underwriting",
        "tier": tier,
        "exit_state": "profit_capture",
        "evaluation_status": "priced",
        "fee_calc_status": "schedule_estimate",
        "estimated_pnl_if_close_net": 80,
        "short_vol_thesis_status": "valid",
        "continued_willingness": "true",
        "close_calibration_status": "complete",
        "capture_ratio": 0.85,
        "remaining_annualized_return": 0.07,
        "close_mid": 0.2,
        "bid": 0.19,
        "ask": 0.21,
        "remaining_premium": 20,
        "estimated_close_fee": 1.5,
        "fee_calc_basis": "futu_us_fixed_package_2026-07-22",
        "contracts_open": 1,
        "multiplier": 100,
        "currency": "USD",
        "policy_version": "p0_current.v1",
        "recommendation_state": "close",
        "decision_basis": f"profit_capture_{tier}",
        "decision_evidence_status": "complete",
    }


def _make_run(
    root: Path,
    *,
    run_id: str,
    account: str = "lx",
    lot_id: str = "lot-1",
    tier: str = "medium",
    context_as_of: str = "2026-07-23T00:00:00Z",
    duplicate_lot: bool = False,
    replacement_run_id: str | None = None,
    contracts: int = 1,
) -> tuple[Path, Path, Path]:
    account_dir = root / "output_runs" / run_id / "accounts" / account
    close_path = account_dir / "close_advice.csv"
    context_path = account_dir / "state" / "option_positions_context.json"
    reallocation_path = account_dir / "close_advice_reallocation_shadow.csv"
    row = _close_row(account=account, lot_id=lot_id, tier=tier)
    row["contracts_open"] = contracts
    _write_csv(close_path, [row])
    position = {
        "record_id": lot_id,
        "account": account,
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "expiration": "2026-08-21",
        "strike": 100,
        "contracts": contracts,
        "contracts_open": contracts,
        "multiplier": 100,
        "currency": "USD",
    }
    positions = [position, dict(position)] if duplicate_lot else [position]
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(
        json.dumps({"as_of_utc": context_as_of, "open_positions_min": positions}),
        encoding="utf-8",
    )
    reallocation = {
        "account": account,
        "position_lot_id": lot_id,
        "reallocation_status": "review_switch",
        "reallocation_reason": "higher_efficiency_recovers_switch_cost_within_horizon",
        "replacement_contract_symbol": "AMD260821P00090000",
        "replacement_symbol": "AMD",
        "replacement_option_type": "put",
        "replacement_expiration": "2026-08-21",
        "replacement_strike": 90,
        "replacement_rank": 1,
        "replacement_entry_credit": 200,
        "replacement_contracts": 1,
        "replacement_multiplier": 100,
        "replacement_currency": "USD",
        "replacement_fee_calc_status": "candidate_futu_fee",
        "replacement_open_fee": 2,
        "replacement_spread_slippage": 5,
    }
    if replacement_run_id:
        reallocation["replacement_run_id"] = replacement_run_id
    _write_csv(reallocation_path, [reallocation])
    audit_path = root / "output_runs" / run_id / "state" / "audit_events.jsonl"
    try:
        run_started = datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
        event_at = (run_started + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    except ValueError:
        event_at = "2026-07-23T01:01:00Z"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "account": account,
                "action": "close_advice",
                "status": "ok",
                "event_at_utc": event_at,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return close_path, context_path, reallocation_path


def test_candidate_dataset_build_is_unchanged_without_close_facet(tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset
    from src.application.shadow_replay.common import DATASET_FILES, OPTIONAL_CLOSE_DATASET_FILES

    manifest = build_shadow_replay_dataset(
        repo_root=tmp_path,
        run_id="legacy-run-id",
        dataset_id="candidate-only",
    )
    dataset_dir = Path(manifest["dataset_dir"])

    assert tuple(manifest["files"]) == DATASET_FILES
    assert "close_decision_facet" not in manifest
    assert "close_advice_paths" not in manifest["source"]
    assert all(not (dataset_dir / name).exists() for name in OPTIONAL_CLOSE_DATASET_FILES)


def test_close_facet_captures_formal_and_all_shadow_policy_results(tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset
    from src.application.shadow_replay.common import OPTIONAL_CLOSE_DATASET_FILES

    _make_run(
        tmp_path,
        run_id="20260723T010000Z-run",
        context_as_of="2026-07-23T01:00:30Z",
    )
    manifest = build_shadow_replay_dataset(
        repo_root=tmp_path,
        run_id="20260723T010000Z-run",
        include_close_decisions=True,
        dataset_id="close-facet",
    )
    dataset_dir = Path(manifest["dataset_dir"])
    episodes = _jsonl(dataset_dir / "close_decision_episodes.jsonl")

    assert set(OPTIONAL_CLOSE_DATASET_FILES).issubset(manifest["files"])
    assert manifest["summary"]["close_decision_episode_count"] == 1
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode["schema_version"] == "shadow_replay_close_episode.v1"
    assert episode["observed_at_utc"] == "2026-07-23T01:01:00Z"
    assert episode["quote_time_basis"] == "run_anchor"
    assert episode["strategy_context_at_utc"] == "2026-07-23T01:00:30Z"
    assert episode["strategy_time_basis"] == "position_context_as_of_utc"
    assert episode["formal_policy_result"]["recommendation_state"] == "close"
    assert {
        key: value["recommendation_state"]
        for key, value in episode["shadow_policy_results"].items()
    } == {
        "P0_current": "close",
        "P1_semantic_split": "review",
        "P2_profile_aware": "hold",
        "P3_opportunity_required": "review",
    }
    assert episode["p0_parity"]["recommendation_matches"] is True
    assert episode["decision_economics"]["close_now_cost"] == 22.5
    assert episode["replacement_evidence"]["entry_credit"] == 200
    assert episode["replacement_evidence"]["fee_calc_status"] == "candidate_futu_fee"
    assert episode["replacement_provenance"] == {
        "status": "validated_same_decision_run",
        "source_run_id": "20260723T010000Z-run",
        "source_run_at_utc": "2026-07-23T01:00:00Z",
    }
    assert len(episode["episode_id"]) == 64
    assert len(episode["material_fact_fingerprint"]) == 64


def test_close_episode_identity_dedupes_exact_reruns_but_splits_material_changes(
    tmp_path: Path,
) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset

    sources = [
        _make_run(tmp_path, run_id="20260723T010000Z-a"),
        _make_run(tmp_path, run_id="20260723T020000Z-b"),
        _make_run(tmp_path, run_id="20260723T030000Z-c", tier="strong"),
        _make_run(tmp_path, run_id="20260723T040000Z-f", contracts=2),
        _make_run(tmp_path, run_id="20260724T010000Z-d", context_as_of="2026-07-24T00:00:00Z"),
        _make_run(tmp_path, run_id="20260723T010500Z-e", account="sy"),
    ]
    kwargs = {
        "repo_root": tmp_path,
        "close_advice_paths": [item[0] for item in sources],
        "position_context_paths": [item[1] for item in sources],
        "reallocation_paths": [item[2] for item in sources],
    }
    first = build_shadow_replay_dataset(**kwargs, dataset_id="identity-a")
    second = build_shadow_replay_dataset(**kwargs, dataset_id="identity-b")
    first_rows = _jsonl(Path(first["dataset_dir"]) / "close_decision_episodes.jsonl")
    second_rows = _jsonl(Path(second["dataset_dir"]) / "close_decision_episodes.jsonl")

    assert len(first_rows) == 5
    assert [row["episode_id"] for row in first_rows] == [row["episode_id"] for row in second_rows]
    deduped = next(
        row
        for row in first_rows
        if row["account"] == "lx"
        and row["episode_date"] == "2026-07-23"
        and row["normalized_decision_facts"]["tier"] == "medium"
        and row["source_observation_count"] == 2
    )
    assert deduped["observed_at_utc"] == "2026-07-23T01:01:00Z"
    assert deduped["source_run_ids"] == ["20260723T010000Z-a", "20260723T020000Z-b"]
    assert deduped["source_observation_count"] == 2
    assert len(
        {
            row["material_fact_fingerprint"]
            for row in first_rows
            if row["account"] == "lx"
            and row["episode_date"] == "2026-07-23"
            and row["normalized_decision_facts"]["tier"] == "medium"
        }
    ) == 2


@pytest.mark.parametrize(
    ("run_id", "context_as_of", "duplicate_lot", "match"),
    [
        ("run-without-time", "2026-07-23T00:00:00Z", False, "canonical UTC run ID"),
        ("20260723T010000Z-run", "2026-07-23T02:00:00Z", False, "newer than close decision"),
        ("20260723T010000Z-run", "2026-07-23T00:00:00Z", True, "resolve exactly once"),
    ],
)
def test_close_capture_rejects_unanchored_future_or_ambiguous_evidence(
    tmp_path: Path,
    run_id: str,
    context_as_of: str,
    duplicate_lot: bool,
    match: str,
) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset

    _make_run(
        tmp_path,
        run_id=run_id,
        context_as_of=context_as_of,
        duplicate_lot=duplicate_lot,
    )

    with pytest.raises(ValueError, match=match):
        build_shadow_replay_dataset(
            repo_root=tmp_path,
            run_id=run_id,
            include_close_decisions=True,
            dataset_id="rejected",
        )


def test_close_capture_rejects_future_replacement_evidence(tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset

    _make_run(
        tmp_path,
        run_id="20260723T010000Z-run",
        replacement_run_id="20260723T020000Z-future",
    )

    with pytest.raises(ValueError, match="replacement evidence is newer"):
        build_shadow_replay_dataset(
            repo_root=tmp_path,
            run_id="20260723T010000Z-run",
            include_close_decisions=True,
            dataset_id="future-replacement",
        )


def test_close_capture_rejects_formal_p0_projection_mismatch(tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset

    close_path, _context_path, _reallocation_path = _make_run(
        tmp_path,
        run_id="20260723T010000Z-run",
    )
    row = _close_row(account="lx", lot_id="lot-1")
    row["recommendation_state"] = "hold"
    _write_csv(close_path, [row])

    with pytest.raises(ValueError, match="does not match P0_current"):
        build_shadow_replay_dataset(
            repo_root=tmp_path,
            run_id="20260723T010000Z-run",
            include_close_decisions=True,
            dataset_id="p0-mismatch",
        )
