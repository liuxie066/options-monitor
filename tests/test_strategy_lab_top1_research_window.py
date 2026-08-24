from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.option_lifecycle import expiration_observation_start_ms
from src.application.shadow_replay.common import (
    DATASET_FILES,
    refresh_dataset_manifest,
    render_json_text,
    write_json,
    write_jsonl,
)
from src.application.strategy_lab.top1.research_artifacts import (
    load_materialized_research_input,
)
from src.application.strategy_lab.top1.research import (
    RESEARCH_CLOSE_RECEIPT_SCHEMA,
    ResearchEvaluationError,
    evaluate_research,
    required_research_close_keys,
)
from src.application.strategy_lab.top1.research_window import (
    ResearchWindowError,
    build_research_window,
    load_research_window,
)
from src.infrastructure.private_storage import atomic_write_private_text
from tests.candidate_evidence_helpers import seal_market_calendar_fixture
from tests.test_strategy_lab_top1_research import _fee_contract, _spec


def _trading_days(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start)
    values: list[str] = []
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _candidate(
    symbol: str,
    contract: str,
    *,
    expiration: str,
    strike: float,
    spot: float,
    net_income: float,
) -> dict[str, object]:
    return {
        "schema_version": "shadow_replay_candidate_snapshot.v1",
        "account": "lx",
        "status": "accepted",
        "strategy": "sell_put",
        "strategy_family": "sell_put",
        "mode": "put",
        "option_type": "put",
        "symbol": symbol,
        "contract_symbol": contract,
        "expiration": expiration,
        "strike": strike,
        "spot": spot,
        "dte": 25,
        "mid": 10.0,
        "multiplier": 100.0,
        "currency": "HKD",
        "net_income": net_income,
        "open_interest": 500.0,
        "volume": 50.0,
        "spread_ratio": 0.10,
        "symbol_concentration_after": None,
        "source_kind": "candidate_csv",
    }


def _seal_dataset(
    research_root: Path,
    *,
    run_id: str,
    trading_date: str,
    dataset_id: str | None = None,
    legacy_unverified: bool = False,
    account: str = "lx",
    expiration: str = "2026-01-30",
    include_candidates: bool = True,
) -> None:
    dataset = (
        research_root / "remote_archive/prod/output_shared/research/shadow_replay/datasets" / (dataset_id or run_id)
    )
    dataset.mkdir(parents=True)
    candidates = (
        [
            _candidate(
                "0700.HK",
                "HK.TCH260130P300000",
                expiration=expiration,
                strike=300.0,
                spot=450.0,
                net_income=1_500.0,
            ),
            _candidate(
                "3690.HK",
                "HK.MET260130P400000",
                expiration=expiration,
                strike=400.0,
                spot=450.0,
                net_income=1_960.0,
            ),
        ]
        if include_candidates
        else []
    )
    for candidate in candidates:
        candidate["account"] = account
        candidate["source_path"] = f"/archive/output_runs/{run_id}/accounts/{account}/0700.hk_sell_put_candidates.csv"
    if candidates:
        labeled_duplicate = dict(candidates[0])
        labeled_duplicate["source_path"] = labeled_duplicate["source_path"].replace(".csv", "_labeled.csv")
        candidates.append(labeled_duplicate)
    for name in DATASET_FILES:
        write_jsonl(dataset / name, candidates if name == "candidate_snapshots.jsonl" else [])
    manifest = refresh_dataset_manifest(dataset)
    manifest["source"] = {
        "run_id": run_id,
        "candidate_paths": (
            [f"/archive/output_runs/{run_id}/accounts/{account}/0700.hk_sell_put_candidates.csv"]
            if include_candidates
            else []
        ),
        "candidate_evidence_coverage": {
            "accounts": [
                {
                    "account": account,
                    "markets": ["hk"],
                    "status": "supported",
                    "strict_replay_authority": True,
                    "contributes_snapshot_facts": True,
                    "owner_snapshots": ["opening"],
                }
            ]
        },
    }
    write_json(dataset / "manifest.json", manifest)
    refresh_dataset_manifest(dataset)
    if legacy_unverified:
        manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
        manifest.pop("generation", None)
        manifest.pop("integrity", None)
        write_json(dataset / "manifest.json", manifest)

    state = research_root / "remote_archive/prod/output_runs" / run_id / f"accounts/{account}/state"
    ledger_hash = "6" * 64
    decision_hash = "7" * 64
    config_hash = "8" * 64
    evidence = {
        "schema_version": "option_market_evidence.v1",
        "status": "ready",
        "reason_code": None,
        "run_id": run_id,
        "account": account,
        "account_config_sha256": config_hash,
        "evidence_at_utc": f"{trading_date}T01:59:00Z",
        "selection_policy_version": "performance_evidence.latest_at_or_before.v1",
        "ledger_generation_sha256_a": ledger_hash,
        "ledger_generation_sha256_b": ledger_hash,
        "decision_state_fingerprint_a": decision_hash,
        "decision_state_fingerprint_b": decision_hash,
        "open_option_positions": [
            {
                "lot_id": "lot-0700",
                "account": account,
                "broker": "futu",
                "instrument_key": "0700-position",
                "symbol": "0700.HK",
                "option_type": "put",
                "strike": "300",
                "expiration_ymd": expiration,
                "currency": "HKD",
                "multiplier": 100,
                "position_side": "short",
                "contracts_open": 1,
                "market_code": "HK",
            }
        ],
        "valuation_mark_facts": [
            {
                "fact_id": "mark-0700",
                "instrument_key": "0700-position",
                "price": "1",
                "mark_kind": "fixture",
                "effective_at_ms": 1,
                "observed_at_ms": 1,
                "source": "fixture",
                "source_id": "mark-0700",
                "revision": 1,
                "supersedes_fact_id": None,
                "source_fact_sha256": "9" * 64,
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
                "source_fact_sha256": "a" * 64,
            }
        ],
    }
    evidence["content_sha256"] = canonical_sha256(evidence)
    payload_path = state / "option_positions_context.json"
    write_json(payload_path, {"strategy_lab_option_market_evidence": evidence})
    write_json(
        state / "prepared_option_positions_context.v2.json",
        {
            "schema_version": "prepared_option_positions_context.v2",
            "run_id": run_id,
            "account": account,
            "status": "ready",
            "account_config_sha256": config_hash,
            "payload_relpath": payload_path.name,
            "payload_sha256": hashlib.sha256(payload_path.read_bytes()).hexdigest(),
            "ledger_generation_sha256": ledger_hash,
            "decision_state_fingerprint": decision_hash,
        },
    )


def _window_fixture(
    tmp_path: Path,
    *,
    cutoff_at_utc: str = "2026-02-02T08:00:00Z",
    expiration: str = "2026-01-30",
    last_account: str = "lx",
    last_run_time: str = "020000",
    omit_last: bool = False,
) -> tuple[Path, Path, list[str], dict[str, object]]:
    artifact_root = tmp_path / "output_shared/research/strategy_lab"
    research_root = artifact_root.parent
    days = _trading_days("2025-12-01", 20)
    calendar = seal_market_calendar_fixture(
        artifact_root,
        days,
        coverage_start=days[0],
        coverage_end=days[-1],
    )
    for index, trading_date in enumerate(days):
        if omit_last and index == len(days) - 1:
            continue
        run_time = last_run_time if index == len(days) - 1 else "020000"
        run_id = f"{trading_date.replace('-', '')}T{run_time}Z-{index:06d}"
        _seal_dataset(
            research_root,
            run_id=run_id,
            trading_date=trading_date,
            dataset_id=(f"prod-hk-{run_id.lower()}" if index == 0 else None),
            legacy_unverified=index == 0,
            account=last_account if index == len(days) - 1 else "lx",
            expiration=expiration,
        )
    return (
        artifact_root,
        research_root,
        days,
        {
            "market": "HK",
            "account": "lx",
            "cutoff_at_utc": cutoff_at_utc,
            "latest_mature_trading_date": days[-1],
            "market_calendar": calendar,
            "datasets_root_ref": ("remote_archive/prod/output_shared/research/shadow_replay/datasets"),
            "runs_root_ref": "remote_archive/prod/output_runs",
        },
    )


def _close_receipt(stock_owner: str, close: float) -> dict[str, object]:
    terminal_at_ms = expiration_observation_start_ms("2026-01-30", "HK")
    assert terminal_at_ms is not None
    return {
        "schema_version": RESEARCH_CLOSE_RECEIPT_SCHEMA,
        "market": "HK",
        "account": "lx",
        "stock_owner": stock_owner,
        "expiration": "2026-01-30",
        "spot_source": "opend_history_kline",
        "ktype": "K_DAY",
        "autype": "NONE",
        "price_field": "close",
        "status": "available",
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
            "source_fact_sha256": "b" * 64,
        },
        "reason_detail": None,
    }


def test_historical_window_reuses_shadow_replay_without_copying_candidates(
    tmp_path: Path,
) -> None:
    artifact_root, research_root, days, kwargs = _window_fixture(tmp_path)
    _seal_dataset(
        research_root,
        run_id=f"{days[-1].replace('-', '')}T030000Z-unrelated",
        trading_date=days[-1],
        account="sy",
    )
    _seal_dataset(
        research_root,
        run_id=f"{days[-1].replace('-', '')}T040000Z-empty",
        trading_date=days[-1],
        include_candidates=False,
    )
    window = build_research_window(artifact_root, **kwargs)
    assert build_research_window(artifact_root, **kwargs) == window
    assert len(window["days"]) == 20
    assert '"candidates"' not in render_json_text(window)
    assert window["days"][0]["points"][0]["source_kind"] == "shadow_replay_legacy_hash_bound_point"

    points = load_research_window(artifact_root, window)
    assert len(points) == 21
    assert points[-1]["candidates"] == []
    assert all(
        candidate["option_market_concentration_after"] is not None
        for point in points
        for candidate in point["candidates"]
    )

    window_ref = "top1/windows/fixture.json"
    window_text = render_json_text(window)
    atomic_write_private_text(artifact_root / window_ref, window_text)
    spec = _spec(
        window,
        variants=(
            ("without", "without_concentration"),
            ("concentration", "concentration_first"),
        ),
    )
    spec["research_source"]["mode"] = "historical_research_window"
    spec["research_source"]["dataset_ref"] = window_ref
    loaded = load_materialized_research_input(
        artifact_root,
        spec,
    )
    assert loaded["research_window"] == window
    assert loaded["observed_points"] == points
    assert required_research_close_keys(loaded, _fee_contract()) == [
        ("HK.00700", "2026-01-30"),
        ("HK.03690", "2026-01-30"),
    ]
    receipts = [
        _close_receipt("HK.00700", 250.0),
        _close_receipt("HK.03690", 500.0),
    ]
    evaluation = evaluate_research(loaded, receipts, _fee_contract())
    assert evaluate_research(loaded, list(reversed(receipts)), _fee_contract()) == evaluation
    assert evaluation["selection"] == "research_leader"
    assert evaluation["leader_variant_id"] == "concentration-0.002"
    assert [item["variant_id"] for item in evaluation["variant_results"]] == [
        "concentration-0.002",
        "concentration-0.004",
        "concentration-0.006",
    ]

    tampered = deepcopy(loaded)
    tampered["observed_points"][0]["candidates"][0]["net_premium"] = 1.0
    with pytest.raises(ResearchEvaluationError, match="historical point is invalid"):
        required_research_close_keys(tampered, _fee_contract())

    candidate_file = research_root / window["days"][0]["points"][0]["dataset_ref"] / "candidate_snapshots.jsonl"
    write_jsonl(candidate_file, [])
    with pytest.raises(ResearchWindowError, match="binding changed"):
        load_research_window(artifact_root, window)


@pytest.mark.parametrize(
    ("omit_last", "last_account"),
    ((True, "lx"), (False, "sy")),
    ids=("missing-day", "account-mismatch"),
)
def test_historical_window_requires_every_scoped_day(
    tmp_path: Path,
    omit_last: bool,
    last_account: str,
) -> None:
    artifact_root, _research_root, _days, kwargs = _window_fixture(
        tmp_path,
        omit_last=omit_last,
        last_account=last_account,
    )

    with pytest.raises(ResearchWindowError) as exc_info:
        build_research_window(artifact_root, **kwargs)

    assert exc_info.value.reason_code == "research_window_coverage_missing"


def test_historical_window_requires_calendar_coverage(tmp_path: Path) -> None:
    artifact_root, _research_root, days, kwargs = _window_fixture(tmp_path)
    kwargs["latest_mature_trading_date"] = (date.fromisoformat(days[0]) - timedelta(days=1)).isoformat()

    with pytest.raises(ResearchWindowError, match="does not cover") as exc_info:
        build_research_window(artifact_root, **kwargs)

    assert exc_info.value.reason_code == "research_window_coverage_missing"


@pytest.mark.parametrize(
    ("cutoff_at_utc", "expiration", "last_run_time", "message"),
    (
        (
            "2025-12-26T01:00:00Z",
            "2025-12-26",
            "020000",
            "after cutoff",
        ),
        (
            "2026-02-02T08:00:00Z",
            "2026-03-27",
            "020000",
            "not mature",
        ),
    ),
    ids=("after-cutoff", "immature-candidate"),
)
def test_historical_window_rejects_unavailable_outcomes(
    tmp_path: Path,
    cutoff_at_utc: str,
    expiration: str,
    last_run_time: str,
    message: str,
) -> None:
    artifact_root, _research_root, _days, kwargs = _window_fixture(
        tmp_path,
        cutoff_at_utc=cutoff_at_utc,
        expiration=expiration,
        last_run_time=last_run_time,
    )

    with pytest.raises(ResearchWindowError, match=message) as exc_info:
        build_research_window(artifact_root, **kwargs)

    assert exc_info.value.reason_code == "research_window_coverage_missing"


def test_historical_window_rejects_duplicate_run(tmp_path: Path) -> None:
    artifact_root, research_root, days, kwargs = _window_fixture(tmp_path)
    run_id = f"{days[-1].replace('-', '')}T020000Z-{len(days) - 1:06d}"
    _seal_dataset(
        research_root,
        run_id=run_id,
        trading_date=days[-1],
        dataset_id=f"duplicate-{run_id}",
    )

    with pytest.raises(ResearchWindowError, match="run is duplicated") as exc_info:
        build_research_window(artifact_root, **kwargs)

    assert exc_info.value.reason_code == "research_window_conflict"
