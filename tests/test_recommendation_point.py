from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine import build_candidate_decision
from src.application.candidate_snapshot_manifest import (
    CANDIDATE_SNAPSHOT_MANIFEST_FILE,
    load_candidate_snapshot_bundle,
    publish_candidate_snapshot_manifest,
)
from src.application.opening_candidate_snapshot import (
    dependency_from_hash,
    seal_opening_candidate_snapshot,
)
from src.application.recommendation_point import (
    RECOMMENDATION_POINT_FILE,
    RECOMMENDATION_POINT_SCHEMA_V1,
    RECOMMENDATION_POINT_SCHEMA_V2,
    RECOMMENDATION_POINT_SCHEMA_V3,
    RecommendationPointError,
    build_formal_point_time_coherence,
    build_option_position_evidence_binding,
    build_recommendation_point,
    build_recommendation_point_id,
    capture_scheduled_recommendation_point,
    load_recommendation_point,
    point_binding_from_recommendation_point,
    publish_recommendation_point,
    validate_option_position_evidence_binding,
    validate_recommendation_point,
)
from src.application.required_data_snapshot import FrozenRequiredDataUnavailable
from src.application.strategy_scan_status import (
    publish_strategy_scan_status,
    publish_strategy_scan_status_index_v2,
)
from src.application.tick_run_workspace import read_account_run_state_bytes_safely
from tests.candidate_evidence_helpers import (
    CONFIG_HASH,
    POLICY_HASH,
    _normalized_row,
    seal_opening_candidate_fixture,
)


SOURCE_SHA = "c" * 40
TARGET = "2026-07-21T10:00:00-04:00"
TARGET_UTC = "2026-07-21T14:00:00Z"


def _candidate(symbol: str = "NVDA") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "contract_symbol": f"{symbol}260821P00100000",
        "expiration": "2026-08-21",
        "strike": 100,
        "open_interest": 500,
        "period_net_return_on_cash_basis": 0.01,
        "net_assignment_discount_pct": 0.08,
        "symbol_concentration_after": 0.20,
        "sell_limit": 1.10,
        "net_premium": 105.0,
        "net_cash_basis": 9_895.0,
        "stock_owner": "none",
        "fee_schedule_version": "fixture.v1",
        "fee_basis": "fixture",
        "fee_schedule_url": "https://example.test/fees",
    }


def _scheduler(
    *,
    target: str = TARGET,
    now_utc: str = "2026-07-21T14:00:30Z",
    should_run_scan: bool = True,
) -> dict[str, Any]:
    return {
        "should_run_scan": should_run_scan,
        "scheduled_scan_target_market": target,
        "now_utc": now_utc,
    }


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _prepared_option_receipt(
    opening: Mapping[str, Any],
    *,
    received_at: str = "2026-06-01T00:00:00Z",
    position_lots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = {
        "prepared_authority": {
            "schema_version": "prepared_option_positions_context",
            "fx_status": "ready",
            "fx_observation_sha256": "f" * 64,
            "source_observed_at": "2026-07-21T14:00:00Z",
        },
        "exchange_rates": {
            "timestamp": "2026-07-21T14:00:00Z",
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        },
        "current_decision_read": {
            "status": "trusted",
            "position_lots": position_lots or [],
        },
        "decision_snapshot_actionable": True,
    }
    payload_bytes = _canonical_bytes(payload)
    manifest = {
        "schema_version": "prepared_option_positions_context",
        "status": "ready",
        "run_id": opening["run_id"],
        "account": opening["account"],
        "account_config_sha256": opening["account_config_sha256"],
        "application_received_at_utc": received_at,
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "ledger_generation_sha256": "d" * 64,
        "decision_state_fingerprint": "e" * 64,
    }
    return {
        "manifest": manifest,
        "payload": payload,
        "manifest_bytes": _canonical_bytes(manifest),
        "payload_bytes": payload_bytes,
    }


def test_formal_point_time_coherence_canonicalizes_aware_candidate_timestamp() -> None:
    opening = {
        "candidate_decisions": [
            {
                "normalized_input": {
                    "snapshot_received_at_utc": "2026-07-21T10:00:00-04:00",
                }
            }
        ]
    }
    required_data = {
        "symbols": {
            "NVDA": {
                "status": "ready",
                "source_observed_at": "2026-07-21T14:00:00Z",
            }
        }
    }
    binding = {
        "valuation_mark_facts": [
            {
                "effective_at_ms": 1_784_642_400_000,
                "observed_at_ms": 1_784_642_400_000,
            }
        ]
    }

    coherence = build_formal_point_time_coherence(opening, required_data, binding)

    assert coherence["status"] == "ready"
    assert coherence["minimum_observed_at_utc"] == "2026-07-21T14:00:00Z"
    assert coherence["maximum_observed_at_utc"] == "2026-07-21T14:00:00Z"
    assert coherence["observation_count"] == 4


@pytest.mark.parametrize(
    "candidate_timestamp",
    [None, "2026-07-21T14:00:00", "not-a-timestamp"],
)
def test_formal_point_time_coherence_rejects_non_aware_candidate_timestamp(
    candidate_timestamp: str | None,
) -> None:
    coherence = build_formal_point_time_coherence(
        {
            "candidate_decisions": [
                {
                    "normalized_input": {
                        "snapshot_received_at_utc": candidate_timestamp,
                    }
                }
            ]
        },
        {
            "symbols": {
                "NVDA": {
                    "status": "ready",
                    "source_observed_at": "2026-07-21T14:00:00Z",
                }
            }
        },
        {"valuation_mark_facts": []},
    )

    assert coherence["status"] == "not_evaluable"
    assert coherence["reason_code"] == "formal_point_time_skew"


def test_option_position_binding_uses_only_the_frozen_scan_batch() -> None:
    opening = {
        "run_id": "formal-binding",
        "account": "lx",
        "account_config_sha256": CONFIG_HASH,
    }
    fields = {
        "status": "open",
        "broker": "futu",
        "symbol": "NVDA",
        "option_type": "put",
        "strike": "100",
        "expiration_ymd": "2026-08-21",
        "currency": "USD",
        "multiplier": "100",
        "side": "short",
        "contracts_open": 1,
        "premium": "2",
        "opened_at": 1_700_000_000_000,
        "market_code": "US.NVDA260821P100000",
    }
    receipt = _prepared_option_receipt(
        opening,
        position_lots=[
            {"record_id": "lot-1", "fields": fields},
            {"record_id": "lot-2", "fields": fields},
        ],
    )
    csv_bytes = (
        "code,bid_price,ask_price,last_price,snapshot_requested_at_utc,"
        "snapshot_received_at_utc\n"
        "US.NVDA260821P100000,2.0,2.4,9.0,"
        "2026-07-21T13:59:59Z,2026-07-21T14:00:00Z\n"
    ).encode()
    point_id = "a" * 64
    binding = build_option_position_evidence_binding(
        run_id="formal-binding",
        account="lx",
        market="US",
        recommendation_point_id=point_id,
        account_config_sha256=CONFIG_HASH,
        evidence_at_utc="2026-07-21T14:00:02Z",
        prepared_receipt=receipt,
        required_data_entries={
            "NVDA": (
                {
                    "scan_blob_ref": {
                        "blob_relpath": "required/NVDA.csv",
                        "blob_sha256": "b" * 64,
                    }
                },
                csv_bytes,
            )
        },
        formal_time_bounds=(1_784_642_340_000, 1_784_642_405_000),
    )

    assert len(binding["open_option_positions"]) == 2
    assert len(binding["valuation_mark_facts"]) == 1
    assert binding["valuation_mark_facts"][0]["price"] == "2.2"
    assert binding["valuation_mark_facts"][0]["source"] == (
        "required_data_snapshot"
    )
    tampered = json.loads(json.dumps(binding))
    tampered["valuation_mark_facts"][0]["source_artifact_sha256"] = "c" * 64
    tampered["content_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in tampered.items()
            if key != "content_sha256"
        }
    )
    with pytest.raises(RecommendationPointError, match="option mark binding changed"):
        validate_option_position_evidence_binding(
            tampered,
            expected_run_id="formal-binding",
            expected_account="lx",
            expected_recommendation_point_id=point_id,
            expected_market="US",
        )
    with pytest.raises(
        RecommendationPointError,
        match="absent from the production snapshot batch",
    ):
        build_option_position_evidence_binding(
            run_id="formal-binding",
            account="lx",
            market="US",
            recommendation_point_id=point_id,
            account_config_sha256=CONFIG_HASH,
            evidence_at_utc="2026-07-21T14:00:02Z",
            prepared_receipt=receipt,
            required_data_entries={},
            formal_time_bounds=(1_784_642_340_000, 1_784_642_405_000),
        )


def _build_from_bundle(
    base: Path,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle = load_candidate_snapshot_bundle(base=base, run_id=run_id, account="lx")
    manifest_bytes = read_account_run_state_bytes_safely(
        base=base,
        run_id=run_id,
        account="lx",
        name=CANDIDATE_SNAPSHOT_MANIFEST_FILE,
    )
    point = build_recommendation_point(
        _scheduler(),
        bundle["manifest"],
        bundle["owners"]["opening"],
        terminal_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        source_commit_sha=SOURCE_SHA,
    )
    return point, bundle["owners"]["opening"]


def _seal_partial_fixture(
    base: Path,
    *,
    run_id: str,
    sibling_failed: bool,
) -> None:
    accepted = _normalized_row(_candidate(), accepted=True)
    scopes = [
        {
            "symbol": "NVDA",
            "status": "completed",
            "reason": None if sibling_failed else "partial_data",
            "candidate_count": 1,
        }
    ]
    if sibling_failed:
        scopes.append(
            {
                "symbol": "AMD",
                "status": "failed",
                "reason": "quote_unavailable",
                "candidate_count": None,
            }
        )
    account_dir = base / "output_runs" / run_id / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    statuses: list[dict[str, Any]] = []
    expected: list[dict[str, str]] = []
    for scope in scopes:
        symbol = str(scope["symbol"])
        publish_strategy_scan_status(
            report_dir=account_dir,
            run_id=run_id,
            account="lx",
            market="US",
            symbol=symbol,
            strategy_family="sell_put",
            status=str(scope["status"]),
            candidate_count=scope["candidate_count"],
            reason=str(scope["reason"] or "") or None,
            snapshot_id=f"fixture-{symbol}-put",
            receipt_relpath=f"quotes/{symbol}/put/receipt.json",
        )
        statuses.append(
            {
                "symbol": symbol,
                "strategy_mode": "put",
                "status": scope["status"],
                "reason": scope["reason"],
                "quote_snapshot_id": f"fixture-{symbol}-put",
                "quote_receipt_relpath": f"quotes/{symbol}/put/receipt.json",
            }
        )
        expected.append(
            {
                "market": "US",
                "symbol": symbol,
                "strategy_family": "sell_put",
                "strategy_mode": "put",
                "candidate_owner": "opening",
                "account_config_sha256": CONFIG_HASH,
            }
        )
    seal_opening_candidate_snapshot(
        base=base,
        run_id=run_id,
        account="lx",
        market="US",
        physical_account={
            "status": "available",
            "logical_account": "lx",
            "futu_account_id": "fixture-account",
            "trd_env": "REAL",
            "market": "US",
            "source": "opend",
        },
        account_config_sha256=CONFIG_HASH,
        strategy_policy_sha256=POLICY_HASH,
        dependencies=[
            dependency_from_hash(kind=kind, sha256=char * 64)
            for kind, char in (
                ("required_data", "1"),
                ("portfolio", "2"),
                ("ledger", "3"),
                ("fx", "4"),
                ("earnings_rv", "5"),
            )
        ],
        scan_statuses=statuses,
        final_candidates={"put": [accepted]},
        candidate_evaluations={
            "put": [
                {
                    "normalized_input": accepted,
                    "opening_decision": build_candidate_decision(
                        mode="put",
                        symbol="NVDA",
                        contract_symbol=str(accepted["contract_symbol"]),
                        accepted=True,
                        normalized_input=accepted,
                    ),
                }
            ]
        },
        sealed_at="2026-07-21T14:00:00Z",
    )
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id=run_id,
        account="lx",
        account_config_sha256=CONFIG_HASH,
        expected=expected,
    )
    publish_candidate_snapshot_manifest(
        base=base,
        run_id=run_id,
        account="lx",
        strategy_policy_sha256=POLICY_HASH,
        sealed_at="2026-07-21T14:00:01Z",
    )


def test_point_identity_canonicalizes_target() -> None:
    assert build_recommendation_point_id("US", "lx", TARGET) == (
        build_recommendation_point_id("US", "lx", TARGET_UTC)
    )
    assert build_recommendation_point_id("US", "lx", TARGET) != (
        build_recommendation_point_id("HK", "lx", TARGET)
    )
    assert build_recommendation_point_id("US", "lx", TARGET) != (
        build_recommendation_point_id("US", "sy", TARGET)
    )
    assert build_recommendation_point_id("US", "lx", TARGET) != (
        build_recommendation_point_id("US", "lx", "2026-07-21T14:30:00Z")
    )
    assert build_recommendation_point_id(
        "US", "lx", TARGET, schema_version=RECOMMENDATION_POINT_SCHEMA_V1
    ) == build_recommendation_point_id(
        "US", "lx", TARGET, schema_version=RECOMMENDATION_POINT_SCHEMA_V2
    )


def test_clean_point_capture_is_manifest_bound_rankable_and_idempotent(
    tmp_path: Path,
) -> None:
    run_id = "clean-point"
    seal_opening_candidate_fixture(
        tmp_path,
        run_id=run_id,
        accepted_rows=[_candidate()],
    )

    publication, point = capture_scheduled_recommendation_point(
        tmp_path,
        run_id,
        "lx",
        _scheduler(),
        source_commit_sha=SOURCE_SHA,
    )

    assert publication == "published"
    assert point["scheduled_scan_target_market"] == TARGET_UTC
    assert point["terminal_sell_put_status"] == "candidates_found"
    assert len(point["producer_accepted_candidate_ids"]) == 1
    assert load_recommendation_point(tmp_path, run_id, "lx") == point
    bundle = load_candidate_snapshot_bundle(base=tmp_path, run_id=run_id, account="lx")
    assert [row["candidate_id"] for row in bundle["owners"]["opening"]["ranked_candidates"]] == point[
        "producer_accepted_candidate_ids"
    ]
    assert capture_scheduled_recommendation_point(
        tmp_path,
        run_id,
        "lx",
        _scheduler(),
        source_commit_sha=SOURCE_SHA,
    )[0] == "idempotent"

    conflict = dict(point)
    conflict["decision_at_utc"] = "2026-07-21T14:00:31Z"
    conflict["content_sha256"] = canonical_sha256(
        {key: value for key, value in conflict.items() if key != "content_sha256"}
    )
    with pytest.raises(RecommendationPointError) as raised:
        publish_recommendation_point(tmp_path, conflict)
    assert raised.value.reason_code == "official_point_conflict"


def test_best_effort_capture_builds_v2_from_strict_prepared_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import recommendation_point as mod

    run_id = "strict-v2-point"
    seal_opening_candidate_fixture(
        tmp_path,
        run_id=run_id,
        accepted_rows=[_candidate()],
    )
    opening = load_candidate_snapshot_bundle(
        base=tmp_path,
        run_id=run_id,
        account="lx",
    )["owners"]["opening"]
    receipt = _prepared_option_receipt(opening)
    monkeypatch.setattr(
        mod,
        "find_prepared_option_positions_manifest",
        lambda **_kwargs: tmp_path / "prepared_option_positions_context.json",
    )
    monkeypatch.setattr(
        mod,
        "load_prepared_option_positions_context_receipt",
        lambda **_kwargs: receipt,
    )

    publication, point = capture_scheduled_recommendation_point(
        tmp_path,
        run_id,
        "lx",
        _scheduler(now_utc="2026-06-01T00:00:01Z"),
        source_commit_sha=SOURCE_SHA,
        require_option_market_evidence=True,
    )

    assert publication == "published"
    assert point["schema_version"] == "recommendation_point.v2"
    assert point["option_market_evidence_ref"].endswith(
        "/prepared_option_positions_context.json"
    )
    assert point["option_market_evidence_manifest_sha256"] == hashlib.sha256(
        receipt["manifest_bytes"]
    ).hexdigest()
    assert point["option_market_evidence_payload_sha256"] == receipt[
        "manifest"
    ]["payload_sha256"]

    late = _prepared_option_receipt(
        opening,
        received_at="2026-06-01T00:00:01Z",
    )
    with pytest.raises(RecommendationPointError) as raised:
        build_recommendation_point(
            _scheduler(),
            load_candidate_snapshot_bundle(
                base=tmp_path,
                run_id=run_id,
                account="lx",
            )["manifest"],
            opening,
            terminal_manifest_sha256=hashlib.sha256(
                read_account_run_state_bytes_safely(
                    base=tmp_path,
                    run_id=run_id,
                    account="lx",
                    name=CANDIDATE_SNAPSHOT_MANIFEST_FILE,
                )
            ).hexdigest(),
            source_commit_sha=SOURCE_SHA,
            prepared_option_receipt=late,
        )
    assert raised.value.reason_code == "option_market_evidence_time_conflict"


def test_formal_capture_preserves_frozen_required_data_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.application import recommendation_point as mod

    run_id = "frozen-required-data"
    seal_opening_candidate_fixture(tmp_path, run_id=run_id)
    required_bytes = b'{"fixture":true}\n'
    required_hash = hashlib.sha256(required_bytes).hexdigest()
    monkeypatch.setattr(
        mod,
        "find_prepared_option_positions_manifest",
        lambda **_kwargs: tmp_path / "prepared_option_positions_context.json",
    )
    monkeypatch.setattr(
        mod,
        "_required_data_binding",
        lambda _opening: ("required/manifest.json", required_hash),
    )
    monkeypatch.setattr(
        mod,
        "load_required_data_snapshot_manifest_snapshot",
        lambda **_kwargs: ({"run_id": run_id, "symbols": {}}, tmp_path, required_bytes),
    )

    def unavailable(**_kwargs: object) -> None:
        raise FrozenRequiredDataUnavailable(
            symbol="NVDA",
            reason="receipt_or_payload_mismatch",
        )

    monkeypatch.setattr(
        mod,
        "resolve_frozen_required_data_csv_bytes_batch",
        unavailable,
    )

    with pytest.raises(RecommendationPointError) as raised:
        capture_scheduled_recommendation_point(
            tmp_path,
            run_id,
            "lx",
            _scheduler(now_utc="2026-06-01T00:00:01Z"),
            source_commit_sha=SOURCE_SHA,
            require_formal_contract=True,
        )
    assert raised.value.reason_code == "required_data_snapshot_unavailable"


def test_clean_no_candidate_point_is_valid(tmp_path: Path) -> None:
    run_id = "no-candidate"
    seal_opening_candidate_fixture(tmp_path, run_id=run_id)

    point, opening = _build_from_bundle(tmp_path, run_id)

    assert point["terminal_sell_put_status"] == "no_candidate"
    assert point["producer_accepted_candidate_ids"] == []
    assert opening["ranked_candidates"] == []


def test_failed_sibling_preserves_candidate_but_is_not_rankable(tmp_path: Path) -> None:
    run_id = "failed-sibling"
    _seal_partial_fixture(tmp_path, run_id=run_id, sibling_failed=True)

    point, opening = _build_from_bundle(tmp_path, run_id)

    assert point["terminal_sell_put_status"] == "data_unavailable"
    assert len(point["producer_accepted_candidate_ids"]) == 1
    assert opening["ranked_candidates"]


def test_partial_completed_scope_stays_partial_when_subset_is_rankable(
    tmp_path: Path,
) -> None:
    run_id = "partial-completed"
    _seal_partial_fixture(tmp_path, run_id=run_id, sibling_failed=False)

    point, opening = _build_from_bundle(tmp_path, run_id)

    assert point["terminal_sell_put_status"] == "partial_data"
    assert len(point["producer_accepted_candidate_ids"]) == 1
    assert [row["candidate_id"] for row in opening["ranked_candidates"]] == point[
        "producer_accepted_candidate_ids"
    ]


def test_builder_rejects_missing_scheduler_identity_and_manifest_hash(
    tmp_path: Path,
) -> None:
    run_id = "invalid-builder"
    seal_opening_candidate_fixture(tmp_path, run_id=run_id)
    bundle = load_candidate_snapshot_bundle(base=tmp_path, run_id=run_id, account="lx")
    manifest = bundle["manifest"]
    opening = bundle["owners"]["opening"]
    manifest_bytes = read_account_run_state_bytes_safely(
        base=tmp_path,
        run_id=run_id,
        account="lx",
        name=CANDIDATE_SNAPSHOT_MANIFEST_FILE,
    )
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

    for scheduler in (
        _scheduler(should_run_scan=False),
        _scheduler(now_utc="2026-07-21T14:00:30"),
        _scheduler(target=""),
    ):
        with pytest.raises(RecommendationPointError) as raised:
            build_recommendation_point(
                scheduler,
                manifest,
                opening,
                terminal_manifest_sha256=manifest_sha,
                source_commit_sha=SOURCE_SHA,
            )
        assert raised.value.reason_code == "official_point_identity_missing"
    with pytest.raises(RecommendationPointError) as raised:
        build_recommendation_point(
            _scheduler(),
            manifest,
            opening,
            terminal_manifest_sha256="d" * 64,
            source_commit_sha=SOURCE_SHA,
        )
    assert raised.value.reason_code == "official_point_invalid"
    with pytest.raises(RecommendationPointError) as raised:
        build_recommendation_point(
            _scheduler(),
            manifest,
            opening,
            terminal_manifest_sha256=manifest_sha,
            source_commit_sha="not-a-commit",
        )
    assert raised.value.reason_code == "official_point_source_unavailable"
    for field in ("account_config_sha256", "strategy_policy_sha256"):
        mismatched_manifest = dict(manifest)
        mismatched_manifest[field] = "e" * 64
        mismatched_manifest["content_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in mismatched_manifest.items()
                if key != "content_sha256"
            }
        )
        with pytest.raises(RecommendationPointError) as raised:
            build_recommendation_point(
                _scheduler(),
                mismatched_manifest,
                opening,
                terminal_manifest_sha256=hashlib.sha256(
                    _canonical_bytes(mismatched_manifest)
                ).hexdigest(),
                source_commit_sha=SOURCE_SHA,
            )
        assert raised.value.reason_code == "official_point_invalid"


def test_validator_rejects_contract_and_content_drift(tmp_path: Path) -> None:
    run_id = "invalid-point"
    seal_opening_candidate_fixture(tmp_path, run_id=run_id, accepted_rows=[_candidate()])
    point, _opening = _build_from_bundle(tmp_path, run_id)

    extra = {**point, "unexpected": True}
    bad_ref = {**point, "opening_snapshot_ref": "../opening_candidate_snapshot.json"}
    bad_ref["content_sha256"] = canonical_sha256(
        {key: value for key, value in bad_ref.items() if key != "content_sha256"}
    )
    bad_status = {**point, "terminal_sell_put_status": "no_candidate"}
    bad_status["content_sha256"] = canonical_sha256(
        {key: value for key, value in bad_status.items() if key != "content_sha256"}
    )
    bad_identity = {**point, "recommendation_point_id": "e" * 64}
    bad_identity["content_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in bad_identity.items()
            if key != "content_sha256"
        }
    )
    drift = {**point, "content_sha256": "d" * 64}
    for invalid in (extra, bad_ref, bad_status, bad_identity, drift):
        with pytest.raises(RecommendationPointError) as raised:
            validate_recommendation_point(invalid)
        assert raised.value.reason_code == "official_point_invalid"


def test_v3_validator_recomputes_formal_point_time_coherence(tmp_path: Path) -> None:
    run_id = "invalid-coherence"
    seal_opening_candidate_fixture(tmp_path, run_id=run_id, accepted_rows=[_candidate()])
    point, _opening = _build_from_bundle(tmp_path, run_id)
    point.update(
        {
            "schema_version": RECOMMENDATION_POINT_SCHEMA_V3,
            "required_data_manifest_ref": "output_runs/run/required.json",
            "required_data_manifest_sha256": "1" * 64,
            "prepared_context_manifest_ref": "output_runs/run/prepared.json",
            "prepared_context_manifest_sha256": "2" * 64,
            "prepared_context_payload_sha256": "3" * 64,
            "formal_point_time_coherence": {
                "schema_version": "formal_point_time_coherence.v1",
                "status": "ready",
                "reason_code": None,
                "minimum_observed_at_utc": "2026-07-21T14:00:00Z",
                "maximum_observed_at_utc": "2026-07-21T14:00:01Z",
                "observation_count": 2,
                "skew_ms": 1,
                "max_skew_ms": 300_000,
            },
        }
    )
    point["content_sha256"] = canonical_sha256(
        {key: value for key, value in point.items() if key != "content_sha256"}
    )

    with pytest.raises(RecommendationPointError) as raised:
        validate_recommendation_point(point)
    assert raised.value.reason_code == "official_point_invalid"


def test_load_rejects_noncanonical_persisted_bytes(tmp_path: Path) -> None:
    run_id = "noncanonical-load"
    seal_opening_candidate_fixture(tmp_path, run_id=run_id)
    point, _opening = _build_from_bundle(tmp_path, run_id)
    assert publish_recommendation_point(tmp_path, point) == "published"
    path = (
        tmp_path
        / "output_runs"
        / run_id
        / "accounts"
        / "lx"
        / "state"
        / RECOMMENDATION_POINT_FILE
    )
    path.write_text(json.dumps(point), encoding="utf-8")

    with pytest.raises(RecommendationPointError) as raised:
        load_recommendation_point(tmp_path, run_id, "lx")
    assert raised.value.reason_code == "official_point_invalid"
