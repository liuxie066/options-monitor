from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from domain.domain.engine import (
    EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
    EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
)
from src.application.opening_candidate_snapshot import (
    OPENING_CANDIDATE_SNAPSHOT_FILE,
    dependency_from_hash,
    seal_opening_candidate_snapshot,
)
from src.application.shadow_replay.common import attach_artifact_provenance
from src.application.strategy_lab.top1.ranking import (
    RANKING_PROJECTION_ARTIFACT_KIND,
    Top1RankingError,
    build_ranking_projection,
    rerank_recommendation_point,
    validate_ranking_projection,
)


NOW = datetime(2026, 8, 15, 1, 0, tzinfo=timezone.utc)
POINT_KEYS = (
    "recommendation_point_id",
    "market",
    "account",
    "run_id",
    "opening_snapshot_ref",
    "opening_snapshot_sha256",
    "decision_at_utc",
    "source_commit_sha",
)
CANDIDATE_KEYS = (
    "candidate_id",
    "symbol",
    "contract_symbol",
    "producer_rank",
    "period_net_return_on_cash_basis",
    "net_assignment_discount_pct",
    "spread_ratio",
    "open_interest",
    "net_income_cny",
    "net_income",
    "symbol_concentration_after",
    "sell_limit",
    "net_premium",
    "net_cash_basis",
    "expiration",
    "strike",
    "multiplier",
    "currency",
    "stock_owner",
    "fee_schedule_version",
    "fee_basis",
    "fee_schedule_url",
)


def _candidate(
    *,
    symbol: str,
    contract_symbol: str,
    period_return: float,
    concentration: float | None,
    discount: float,
) -> dict:
    return {
        "symbol": symbol,
        "contract_symbol": contract_symbol,
        "expiration": "2026-09-18",
        "option_type": "put",
        "option_standard_type": "STANDARD",
        "stock_owner": f"US.{symbol}",
        "strike": 90.0,
        "spot": 100.0,
        "dte": 34,
        "bid": 2.9,
        "ask": 3.1,
        "mid": 3.0,
        "sell_limit": 3.0,
        "multiplier": 100,
        "currency": "USD",
        "open_interest": 500,
        "volume": 50,
        "spread_ratio": 0.0667,
        "period_net_return_on_cash_basis": period_return,
        "annualized_net_return_on_cash_basis": period_return * 365 / 34,
        "net_assignment_discount_pct": discount,
        "symbol_concentration_after": concentration,
        "net_income": 295.0,
        "net_premium": 295.0,
        "net_cash_basis": 8705.0,
        "net_income_cny": 2124.0,
        "net_premium_cny": 2124.0,
        "fee_schedule_version": "futu_option_sell_fee.v1",
        "fee_basis": "futu_us_candidate_upper_bound_2026-08-06",
        "fee_schedule_url": "https://www.futuhk.com/support/topic2_108",
        "implied_volatility": 0.42,
        "term_matched_rv": 0.30,
        "iv_rv_ratio": 1.4,
        "iv_minus_rv": 0.12,
        "earnings_evidence_status": "ready",
        "earnings_reason_code": None,
        "earnings_policy_version": EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
        "earnings_window_days": EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
        "earnings_market_date": "2026-08-15",
        "earnings_hard_window_start": "2026-09-12",
        "earnings_hard_window_end": "2026-09-18",
        "earnings_hard_coverage_status": "complete",
        "earnings_soft_coverage_status": "complete",
        "earnings_has_event": False,
        "earnings_blocking_has_event": False,
        "earnings_events": [],
        "earnings_blocking_events": [],
        "earnings_nonblocking_events": [],
        "max_new_contracts": 1,
        "policy_min_dte": 21,
        "policy_max_dte": 60,
        "policy_max_strike": 100.0,
        "policy_max_spread_ratio": 0.30,
    }


def _dependencies() -> list[dict]:
    return [
        dependency_from_hash(kind=kind, sha256=character * 64)
        for kind, character in (
            ("required_data", "1"),
            ("portfolio", "2"),
            ("ledger", "3"),
            ("fx", "4"),
            ("earnings_rv", "5"),
        )
    ]


def _seal(
    base: Path,
    *,
    run_id: str = "run-top1",
    candidates: list[dict] | None = None,
    scan_status: str = "completed",
    scan_reason: str | None = None,
) -> dict:
    (base / "output_runs" / run_id / "accounts" / "lx").mkdir(
        parents=True,
        exist_ok=True,
    )
    rows = candidates
    if rows is None:
        rows = [
            _candidate(
                symbol="NVDA",
                contract_symbol="NVDA260918P00090000",
                period_return=0.0200,
                concentration=0.50,
                discount=0.05,
            ),
            _candidate(
                symbol="AMD",
                contract_symbol="AMD260918P00090000",
                period_return=0.0185,
                concentration=0.10,
                discount=0.04,
            ),
            _candidate(
                symbol="TSLA",
                contract_symbol="TSLA260918P00090000",
                period_return=0.0300,
                concentration=None,
                discount=0.20,
            ),
        ]
    symbols = [str(row["symbol"]) for row in rows] or ["NVDA"]
    return seal_opening_candidate_snapshot(
        base=base,
        run_id=run_id,
        account="lx",
        market="US",
        physical_account={
            "status": "available",
            "logical_account": "lx",
            "futu_account_id": "12345",
            "trd_env": "REAL",
            "market": "US",
            "source": "opend",
        },
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(),
        scan_statuses=[
            {
                "symbol": symbol,
                "strategy_mode": "put",
                "status": scan_status,
                "reason": scan_reason if scan_reason is not None else (
                    "no_candidate" if not rows else None
                ),
                "quote_snapshot_id": "c" * 64,
                "quote_receipt_relpath": f"quotes/{symbol}/receipt.json",
            }
            for symbol in symbols
        ],
        final_candidates={"put": rows},
        sealed_at=NOW,
    )


def _binding(snapshot: dict) -> dict:
    return {
        "recommendation_point_id": "d" * 64,
        "market": snapshot["market"],
        "account": snapshot["account"],
        "run_id": snapshot["run_id"],
        "opening_snapshot_ref": (
            f"output_runs/{snapshot['run_id']}/accounts/{snapshot['account']}/state/"
            f"{OPENING_CANDIDATE_SNAPSHOT_FILE}"
        ),
        "opening_snapshot_sha256": snapshot["content_sha256"],
        "decision_at_utc": "2026-08-15T01:05:00Z",
        "source_commit_sha": "e" * 40,
    }


def _projection(tmp_path: Path) -> tuple[dict, dict]:
    snapshot = _seal(tmp_path)
    return snapshot, build_ranking_projection(snapshot, point_binding=_binding(snapshot))


def _rehash(payload: dict) -> dict:
    source = deepcopy(payload["artifact_provenance"]["source_generation"])
    payload.pop("artifact_provenance")
    return attach_artifact_provenance(
        payload,
        artifact_kind=RANKING_PROJECTION_ARTIFACT_KIND,
        source_generation=source,
    )


def test_build_projection_is_strict_and_reranks_without_source(tmp_path: Path) -> None:
    snapshot, projection = _projection(tmp_path)
    producer_ids = projection["producer_accepted_candidate_ids"]
    assert [row["symbol"] for row in projection["candidates"]] == [
        "TSLA",
        "AMD",
        "NVDA",
    ]
    assert [row["producer_rank"] for row in projection["candidates"]] == [1, 2, 3]
    assert set(projection["candidates"][0]) == set(CANDIDATE_KEYS)
    assert projection["artifact_provenance"]["source_generation"] == {
        "generation_id": f"opening_candidate_snapshot:{snapshot['content_sha256']}",
        "revision": 1,
        "source_ref": projection["opening_snapshot_ref"],
        "source_sha256": snapshot["content_sha256"],
    }

    snapshot_path = (
        tmp_path
        / "output_runs"
        / snapshot["run_id"]
        / "accounts"
        / snapshot["account"]
        / "state"
        / OPENING_CANDIDATE_SNAPSHOT_FILE
    )
    snapshot_path.unlink()
    del snapshot

    baseline = rerank_recommendation_point(projection)
    without = rerank_recommendation_point(
        projection,
        ranking_profile="without_concentration",
    )
    concentration_first = rerank_recommendation_point(
        projection,
        ranking_profile="concentration_first",
    )
    assert baseline == {
        "schema_version": "sell_put_recommendation_ranking_result.v1",
        "ranking_profile": "current_tie_break",
        "ranking_projection_sha256": projection["artifact_provenance"]["content_sha256"],
        "ordered_candidate_ids": producer_ids,
        "top1_candidate_id": producer_ids[0],
        "parity_status": "matched",
    }
    assert [
        next(row["symbol"] for row in projection["candidates"] if row["candidate_id"] == item)
        for item in without["ordered_candidate_ids"]
    ] == ["TSLA", "NVDA", "AMD"]
    assert [
        next(row["symbol"] for row in projection["candidates"] if row["candidate_id"] == item)
        for item in concentration_first["ordered_candidate_ids"]
    ] == ["AMD", "NVDA", "TSLA"]


def test_empty_accepted_set_is_a_valid_projection(tmp_path: Path) -> None:
    snapshot = _seal(tmp_path, run_id="run-empty", candidates=[])
    projection = build_ranking_projection(snapshot, point_binding=_binding(snapshot))

    assert projection["producer_accepted_candidate_ids"] == []
    assert projection["candidates"] == []
    assert rerank_recommendation_point(projection)["top1_candidate_id"] is None


@pytest.mark.parametrize(
    ("scan_status", "scan_reason"),
    (("failed", "data_unavailable"), ("completed", "partial_data")),
)
def test_empty_projection_rejects_unavailable_strategy_evidence(
    tmp_path: Path,
    scan_status: str,
    scan_reason: str,
) -> None:
    snapshot = _seal(
        tmp_path,
        run_id=f"run-{scan_reason}",
        candidates=[],
        scan_status=scan_status,
        scan_reason=scan_reason,
    )

    with pytest.raises(Top1RankingError, match="strategy status"):
        build_ranking_projection(snapshot, point_binding=_binding(snapshot))


@pytest.mark.parametrize("missing", POINT_KEYS)
def test_point_binding_requires_every_exact_key(tmp_path: Path, missing: str) -> None:
    snapshot = _seal(tmp_path)
    binding = _binding(snapshot)
    binding.pop(missing)

    with pytest.raises(Top1RankingError, match="point_binding") as exc_info:
        build_ranking_projection(snapshot, point_binding=binding)
    assert exc_info.value.reason_code == "ranking_projection_incomplete"


def test_point_binding_rejects_extra_keys_and_snapshot_mismatches(tmp_path: Path) -> None:
    snapshot = _seal(tmp_path)
    binding = _binding(snapshot)
    binding["extra"] = True
    with pytest.raises(Top1RankingError, match="point_binding"):
        build_ranking_projection(snapshot, point_binding=binding)

    for field, value in (
        ("market", "HK"),
        ("account", "sy"),
        ("run_id", "other-run"),
        ("opening_snapshot_sha256", "f" * 64),
    ):
        mismatch = _binding(snapshot)
        mismatch[field] = value
        with pytest.raises(Top1RankingError):
            build_ranking_projection(snapshot, point_binding=mismatch)


@pytest.mark.parametrize(
    "path",
    (
        "/output_runs/run/state/snapshot.json",
        "output_runs/../snapshot.json",
        "output_runs/./snapshot.json",
        "output_runs//snapshot.json",
        "output_runs\\snapshot.json",
    ),
)
def test_point_binding_rejects_unsafe_snapshot_refs(tmp_path: Path, path: str) -> None:
    snapshot = _seal(tmp_path)
    binding = _binding(snapshot)
    binding["opening_snapshot_ref"] = path

    with pytest.raises(Top1RankingError, match="safe relative POSIX path"):
        build_ranking_projection(snapshot, point_binding=binding)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("recommendation_point_id", "A" * 64),
        ("source_commit_sha", "e" * 39),
        ("decision_at_utc", "2026-08-15T01:05:00+00:00"),
        ("account", "LX"),
    ),
)
def test_point_binding_rejects_noncanonical_values(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    snapshot = _seal(tmp_path)
    binding = _binding(snapshot)
    binding[field] = value

    with pytest.raises(Top1RankingError):
        build_ranking_projection(snapshot, point_binding=binding)


@pytest.mark.parametrize(
    "missing",
    (
        "schema_version",
        *POINT_KEYS,
        "account_config_sha256",
        "strategy_policy_sha256",
        "sell_put_ranking_contract_version",
        "producer_accepted_candidate_ids",
        "candidates",
        "artifact_provenance",
    ),
)
def test_projection_requires_every_top_level_key(tmp_path: Path, missing: str) -> None:
    _snapshot, projection = _projection(tmp_path)
    projection.pop(missing)

    with pytest.raises(Top1RankingError):
        validate_ranking_projection(projection)


@pytest.mark.parametrize("missing", CANDIDATE_KEYS)
def test_projection_requires_every_candidate_key(tmp_path: Path, missing: str) -> None:
    _snapshot, projection = _projection(tmp_path)
    projection["candidates"][0].pop(missing)

    with pytest.raises(Top1RankingError):
        validate_ranking_projection(projection)


def test_projection_rejects_extra_keys_invalid_numbers_and_duplicate_ids(
    tmp_path: Path,
) -> None:
    _snapshot, projection = _projection(tmp_path)
    extra = deepcopy(projection)
    extra["unexpected"] = True
    with pytest.raises(Top1RankingError):
        validate_ranking_projection(extra)

    extra_candidate = deepcopy(projection)
    extra_candidate["candidates"][0]["unexpected"] = True
    with pytest.raises(Top1RankingError):
        validate_ranking_projection(extra_candidate)

    not_finite = deepcopy(projection)
    not_finite["candidates"][0]["spread_ratio"] = float("nan")
    with pytest.raises(Top1RankingError):
        validate_ranking_projection(not_finite)

    non_positive = deepcopy(projection)
    non_positive["candidates"][0]["sell_limit"] = 0
    with pytest.raises(Top1RankingError):
        validate_ranking_projection(non_positive)

    duplicate = deepcopy(projection)
    duplicate_id = duplicate["producer_accepted_candidate_ids"][0]
    duplicate["producer_accepted_candidate_ids"][1] = duplicate_id
    duplicate["candidates"][1]["candidate_id"] = duplicate_id
    with pytest.raises(Top1RankingError):
        validate_ranking_projection(duplicate)

    bad_rank = deepcopy(projection)
    bad_rank["candidates"][0]["producer_rank"] = 2
    with pytest.raises(Top1RankingError):
        validate_ranking_projection(bad_rank)


@pytest.mark.parametrize(
    ("container", "missing"),
    (
        ("provenance", "schema_version"),
        ("provenance", "artifact_kind"),
        ("provenance", "artifact_id"),
        ("provenance", "content_sha256"),
        ("provenance", "source_generation"),
        ("source", "generation_id"),
        ("source", "revision"),
        ("source", "source_ref"),
        ("source", "source_sha256"),
    ),
)
def test_projection_requires_exact_provenance_keys(
    tmp_path: Path,
    container: str,
    missing: str,
) -> None:
    _snapshot, projection = _projection(tmp_path)
    target = projection["artifact_provenance"]
    if container == "source":
        target = target["source_generation"]
    target.pop(missing)

    with pytest.raises(Top1RankingError):
        validate_ranking_projection(projection)


def test_projection_rejects_hash_tampering(tmp_path: Path) -> None:
    _snapshot, projection = _projection(tmp_path)
    projection["candidates"][0]["symbol_concentration_after"] = 0.75

    with pytest.raises(Top1RankingError, match="artifact provenance"):
        validate_ranking_projection(projection)


def test_default_rerank_fails_closed_on_producer_parity_mismatch(tmp_path: Path) -> None:
    _snapshot, projection = _projection(tmp_path)
    projection["candidates"][0]["period_net_return_on_cash_basis"] = 0.001
    projection["candidates"][0]["symbol_concentration_after"] = 0.90
    _rehash(projection)

    with pytest.raises(Top1RankingError) as exc_info:
        rerank_recommendation_point(projection)
    assert exc_info.value.reason_code == "baseline_rank_parity_mismatch"
