from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from copy import deepcopy
from dataclasses import replace

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.api import derive_lifecycle_quality_view
from src.application.quality.intake_checks import build_trade_intake_datasets
from src.application.quality.ledger_checks import (
    build_current_ledger_dataset,
    build_ledger_datasets,
)
from src.application.quality.lifecycle_checks import (
    build_current_lifecycle_quality_dataset,
    build_lifecycle_datasets,
    build_lifecycle_quality_migration_summary,
    lifecycle_deadline,
)
from src.application.quality.position_checks import (
    build_position_dataset,
    normalize_opend_positions,
)
from src.application.quality.runtime_checks import build_runtime_checks
from src.application.quality.opend_position_adapter import OpenDOptionSnapshot


def test_current_quality_datasets_fail_closed_without_history() -> None:
    ledger = build_current_ledger_dataset(
        current_projection={"status": "absent", "reason": "missing"},
        account="lx",
        market="us",
        observed_at_utc="2026-08-16T00:00:00Z",
    )
    lifecycle = build_current_lifecycle_quality_dataset(
        current_quality={
            "aggregate_by_market": [
                {
                    "market": "US",
                    "total_case_count": 1,
                    "status_counts": {"needs_review": 1},
                    "trust_class_counts": {"trusted": 1},
                    "dataset_status_counts": {"untrusted": 1},
                    "blocked_consumer_counts": {"close_advice": 1},
                }
            ],
            "operational_cases": [],
        },
        projection_status="trusted",
        projection_reason=None,
        account="lx",
        market="us",
        observed_at_utc="2026-08-16T00:00:00Z",
    )

    assert ledger["status"] == "unavailable"
    assert ledger["blocked_by"] == ["OM-LED-001"]
    assert lifecycle["status"] == "untrusted"
    assert lifecycle["blocked_consumers"] == ["close_advice"]
    assert lifecycle["blocked_by"] == ["OM-LCY-CURRENT-001"]


class _LedgerRepo:
    def __init__(self, events: list[dict], lots: list[dict]) -> None:
        self.events = events
        self.lots = lots

    def list_trade_events(self) -> list[dict]:
        return list(self.events)

    def list_position_lots(self) -> list[dict]:
        return list(self.lots)


def _local_lot(*, contracts: int = 1) -> dict:
    return {
        "record_id": "lot-nvda",
        "fields": {
            "account": "lx",
            "broker": "富途",
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "contracts": contracts,
            "contracts_open": contracts,
            "contracts_closed": 0,
            "strike": 100,
            "multiplier": 100,
            "expiration_ymd": "2026-07-17",
            "status": "open",
        },
    }


def _snapshot(*, qty: int = 1, trading_days: list[date] | None = None) -> OpenDOptionSnapshot:
    return OpenDOptionSnapshot(
        account="lx",
        market="us",
        environment="REAL",
        account_fingerprint="sha256:" + ("a" * 64),
        observed_at_utc="2026-07-13T10:00:00Z",
        snapshot_id="snapshot-test",
        complete=True,
        refresh_cache=True,
        rows=[
            {
                "code": "US.NVDA260717P100000",
                "qty": qty,
                "position_side": "SHORT",
                "options_per_contract": 100,
                "sec_type": "DRVT",
            }
        ],
        trading_days=trading_days or [],
    )


def test_public_source_snapshot_allowlists_internal_position_input() -> None:
    snapshot = replace(
        _snapshot(),
        snapshot_input={
            "scope": {"markets": ["US"], "asset_types": ["option"]},
            "completeness": "complete",
            "quality": {"status": "ready"},
            "source_as_of_utc": "2026-07-13T09:59:00Z",
            "internal_sentinel": "must-not-cross-public-boundary",
        },
    )

    public = snapshot.public_source_snapshot()

    assert set(public) == {
        "provider",
        "snapshot_id",
        "observed_at_utc",
        "complete",
        "refresh_cache",
        "account_fingerprint",
        "environment",
        "market",
    }
    assert snapshot.snapshot_input["internal_sentinel"] == (
        "must-not-cross-public-boundary"
    )


def test_position_convergence_matches_exact_identity_and_quantity() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    dataset, state = build_position_dataset(
        snapshot=_snapshot(),
        local_lots=[_local_lot()],
        account="lx",
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
        control_state={"position_mismatches": {}},
    )
    assert dataset["status"] == "trusted"
    assert dataset["checks"][1]["reason_code"] == "POSITIONS_RECONCILED"
    assert state["position_mismatches"] == {}


def test_position_divergence_is_transient_then_persistent_without_rewrite() -> None:
    first = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    dataset, state = build_position_dataset(
        snapshot=_snapshot(qty=2),
        local_lots=[_local_lot()],
        account="lx",
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
        now=first,
        control_state={"position_mismatches": {}},
    )
    assert dataset["status"] == "partial"
    assert dataset["checks"][1]["reason_code"] == "POSITION_DIVERGENCE_TRANSIENT"
    assert state["position_mismatches"]["us:lx"]["next_recheck_at_utc"] == "2026-07-13T10:01:00Z"

    dataset, _state = build_position_dataset(
        snapshot=replace(_snapshot(qty=2), observed_at_utc="2026-07-13T10:05:01Z"),
        local_lots=[_local_lot()],
        account="lx",
        market="us",
        observed_at_utc="2026-07-13T10:05:01Z",
        now=first + timedelta(seconds=301),
        control_state=state,
    )
    assert dataset["status"] == "untrusted"
    assert dataset["checks"][1]["reason_code"] == "POSITION_DIVERGENCE_PERSISTENT"
    assert "close_advice" in dataset["blocked_consumers"]


def test_position_identity_errors_report_local_and_opend_sources() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    local = _local_lot()
    local["fields"].pop("multiplier")
    snapshot = _snapshot()
    snapshot.rows[0].pop("options_per_contract")
    hk_local = _local_lot()
    hk_local["record_id"] = "lot-hk"
    hk_local["fields"]["symbol"] = "0700.HK"
    hk_local["fields"].pop("multiplier")
    snapshot.rows.append(
        {
            "code": "HK.12345",
            "qty": 1,
            "position_side": "SHORT",
            "sec_type": "DRVT",
        }
    )
    dataset, _state = build_position_dataset(
        snapshot=snapshot,
        local_lots=[local, hk_local],
        account="lx",
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
        control_state={"position_mismatches": {}},
    )

    convergence = dataset["checks"][1]
    assert dataset["status"] == "unavailable"
    assert convergence["reason_code"] == "POSITION_IDENTITY_INCOMPLETE"
    assert convergence["observed"] == {
        "normalization_error_count": 2,
        "local_normalization_error_count": 1,
        "opend_normalization_error_count": 1,
    }


def test_position_market_filter_keeps_unknown_market_identity_errors() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    local = _local_lot()
    local["fields"]["symbol"] = ""
    snapshot = _snapshot()
    snapshot.rows[0]["code"] = ""
    dataset, _state = build_position_dataset(
        snapshot=snapshot,
        local_lots=[local],
        account="lx",
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
        control_state={"position_mismatches": {}},
    )

    convergence = dataset["checks"][1]
    assert dataset["status"] == "unavailable"
    assert convergence["observed"] == {
        "normalization_error_count": 2,
        "local_normalization_error_count": 1,
        "opend_normalization_error_count": 1,
    }


def test_zero_quantity_opend_row_does_not_require_contract_identity() -> None:
    normalized, errors = normalize_opend_positions(
        [
            {
                "code": "HK.TCH260731P440000",
                "qty": 0,
                "position_side": "SHORT",
            }
        ],
        market="hk",
    )

    assert normalized == {}
    assert errors == []


def test_opend_multiplier_uses_first_positive_authoritative_field() -> None:
    normalized, errors = normalize_opend_positions(
        [
            {
                "code": "HK.POP260828P145000",
                "qty": -1,
                "position_side": "SHORT",
                "options_per_contract": None,
                "option_contract_multiplier": 200,
            }
        ],
        market="hk",
    )

    assert normalized == {"9992.HK|put|2026-08-28|145|200": -1}
    assert errors == []


def test_opend_current_terms_override_option_code_terms() -> None:
    normalized, errors = normalize_opend_positions(
        [
            {
                "code": "US.NVDA260717P100000",
                "stock_owner": "US.NVDA",
                "option_type": "PUT",
                "strike_time": "2026-07-17",
                "option_strike_price": 99.5,
                "qty": -1,
                "position_side": "SHORT",
                "options_per_contract": 101,
            }
        ],
        market="us",
    )

    assert normalized == {"NVDA|put|2026-07-17|99.5|101": -1}
    assert errors == []


def test_position_contract_terms_drift_blocks_consumers_immediately() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    snapshot = _snapshot()
    snapshot.rows[0].update(
        {
            "stock_owner": "US.NVDA",
            "option_type": "PUT",
            "strike_time": "2026-07-17",
            "option_strike_price": 99.5,
            "options_per_contract": 101,
        }
    )

    dataset, state = build_position_dataset(
        snapshot=snapshot,
        local_lots=[_local_lot()],
        account="lx",
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
        control_state={"position_mismatches": {}},
    )

    convergence = dataset["checks"][1]
    assert dataset["status"] == "untrusted"
    assert set(dataset["blocked_consumers"]) == {
        "option_position_report",
        "lifecycle",
        "close_advice",
    }
    assert convergence["reason_code"] == "POSITION_CONTRACT_TERMS_DRIFT"
    assert convergence["observed"]["contract_terms_drifts"] == [
        {
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-07-17",
            "quantity": "-1",
            "local_contracts": "100@100:-1",
            "opend_contracts": "99.5@101:-1",
            "broker_code": "US.NVDA260717P100000",
            "mapping": "code_lineage",
        }
    ]
    assert state["position_mismatches"]["us:lx"]["kind"] == (
        "contract_terms_drift"
    )


def test_multi_strike_contract_terms_drift_uses_each_broker_code_lineage() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    second_lot = _local_lot(contracts=2)
    second_lot["record_id"] = "lot-nvda-110"
    second_lot["fields"]["strike"] = 110
    snapshot = _snapshot()
    snapshot.rows[0].update(
        {
            "stock_owner": "US.NVDA",
            "option_type": "PUT",
            "strike_time": "2026-07-17",
            "option_strike_price": 99.5,
        }
    )
    snapshot.rows.append(
        {
            "code": "US.NVDA260717P110000",
            "stock_owner": "US.NVDA",
            "option_type": "PUT",
            "strike_time": "2026-07-17",
            "option_strike_price": 109.5,
            "qty": 2,
            "position_side": "SHORT",
            "options_per_contract": 100,
            "sec_type": "DRVT",
        }
    )

    dataset, _state = build_position_dataset(
        snapshot=snapshot,
        local_lots=[_local_lot(), second_lot],
        account="lx",
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
        control_state={"position_mismatches": {}},
    )

    convergence = dataset["checks"][1]
    assert dataset["status"] == "untrusted"
    assert convergence["reason_code"] == "POSITION_CONTRACT_TERMS_DRIFT"
    assert convergence["observed"]["contract_terms_drifts"] == [
        {
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-07-17",
            "quantity": "-1",
            "local_contracts": "100@100:-1",
            "opend_contracts": "99.5@100:-1",
            "broker_code": "US.NVDA260717P100000",
            "mapping": "code_lineage",
        },
        {
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-07-17",
            "quantity": "-2",
            "local_contracts": "110@100:-2",
            "opend_contracts": "109.5@100:-2",
            "broker_code": "US.NVDA260717P110000",
            "mapping": "code_lineage",
        },
    ]


def test_same_quantity_close_and_open_is_not_classified_as_contract_terms_drift() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    snapshot = _snapshot()
    snapshot.rows[0]["code"] = "US.NVDA260717P090000"

    dataset, _state = build_position_dataset(
        snapshot=snapshot,
        local_lots=[_local_lot()],
        account="lx",
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
        control_state={"position_mismatches": {}},
    )

    assert dataset["status"] == "partial"
    assert dataset["checks"][1]["reason_code"] == (
        "POSITION_DIVERGENCE_TRANSIENT"
    )


def _pending_lifecycle_case(*, contracts: int = 1) -> tuple[dict, dict]:
    case = {
        "case_id": "case-nvda",
        "account": "lx",
        "market": "US",
        "status": "waiting_settlement_evidence",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": 100,
        "multiplier": 100,
        "expiration_ymd": "2026-07-17",
    }
    read_model = {
        "pending_until_ms": int(
            datetime(2026, 7, 13, 11, tzinfo=timezone.utc).timestamp()
            * 1000
        ),
        "remaining_contracts_by_lot": {"lot-nvda": contracts},
    }
    return case, read_model


def test_position_lifecycle_exact_coverage_is_partial_but_non_blocking() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    lifecycle_case, read_model = _pending_lifecycle_case()
    dataset, state = build_position_dataset(
        snapshot=_snapshot(qty=0),
        local_lots=[_local_lot()],
        account="lx",
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
        control_state={"position_mismatches": {}},
        lifecycle_cases=[lifecycle_case],
        lifecycle_read_models_by_case={"case-nvda": read_model},
        day_end_strict=True,
    )

    convergence = dataset["checks"][1]
    assert dataset["status"] == "partial"
    assert dataset["blocked_consumers"] == []
    assert convergence["reason_code"] == "POSITIONS_PENDING_LIFECYCLE"
    assert convergence["observed"] == {
        "mismatch_count": 0,
        "observed_mismatch_count": 1,
        "expected_lifecycle_pending_count": 1,
    }
    assert state["position_mismatches"] == {}


def test_contract_terms_drift_precedes_active_lifecycle_coverage() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    lifecycle_case, read_model = _pending_lifecycle_case()
    snapshot = _snapshot()
    snapshot.rows[0].update(
        {
            "stock_owner": "US.NVDA",
            "option_type": "PUT",
            "strike_time": "2026-07-17",
            "option_strike_price": 99.5,
        }
    )

    dataset, _state = build_position_dataset(
        snapshot=snapshot,
        local_lots=[_local_lot()],
        account="lx",
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
        control_state={"position_mismatches": {}},
        lifecycle_cases=[lifecycle_case],
        lifecycle_read_models_by_case={"case-nvda": read_model},
    )

    assert dataset["status"] == "untrusted"
    assert dataset["checks"][1]["reason_code"] == (
        "POSITION_CONTRACT_TERMS_DRIFT"
    )
    assert "close_advice" in dataset["blocked_consumers"]


def test_position_mismatch_fails_closed_when_coherent_lifecycle_read_is_unavailable() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    dataset, state = build_position_dataset(
        snapshot=_snapshot(qty=0),
        local_lots=[_local_lot()],
        account="lx",
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
        control_state={"position_mismatches": {}},
        lifecycle_coherent_read_available=False,
        day_end_strict=True,
    )

    convergence = dataset["checks"][1]
    assert dataset["status"] == "unavailable"
    assert convergence["status"] == "unknown"
    assert convergence["reason_code"] == (
        "POSITION_LIFECYCLE_COHERENT_READ_UNAVAILABLE"
    )
    assert state["position_mismatches"] == {}


def test_position_lifecycle_partial_quantity_does_not_hide_divergence() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    lifecycle_case, read_model = _pending_lifecycle_case(contracts=1)
    dataset, _state = build_position_dataset(
        snapshot=_snapshot(qty=0),
        local_lots=[_local_lot(contracts=2)],
        account="lx",
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
        control_state={"position_mismatches": {}},
        lifecycle_cases=[lifecycle_case],
        lifecycle_read_models_by_case={"case-nvda": read_model},
        day_end_strict=True,
    )

    assert dataset["status"] == "untrusted"
    assert dataset["checks"][1]["reason_code"] == (
        "POSITION_DIVERGENCE_PERSISTENT"
    )


def test_position_lifecycle_overdue_case_does_not_hide_divergence() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    lifecycle_case, read_model = _pending_lifecycle_case()
    read_model["pending_until_ms"] = int(
        datetime(2026, 7, 13, 9, tzinfo=timezone.utc).timestamp() * 1000
    )
    dataset, _state = build_position_dataset(
        snapshot=_snapshot(qty=0),
        local_lots=[_local_lot()],
        account="lx",
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
        control_state={"position_mismatches": {}},
        lifecycle_cases=[lifecycle_case],
        lifecycle_read_models_by_case={"case-nvda": read_model},
        day_end_strict=True,
    )

    assert dataset["status"] == "untrusted"
    assert dataset["checks"][1]["reason_code"] == (
        "POSITION_DIVERGENCE_PERSISTENT"
    )


def test_position_lifecycle_conflict_does_not_hide_divergence() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    lifecycle_case, read_model = _pending_lifecycle_case()
    read_model["lifecycle_state"] = "conflict"
    dataset, _state = build_position_dataset(
        snapshot=_snapshot(qty=0),
        local_lots=[_local_lot()],
        account="lx",
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
        control_state={"position_mismatches": {}},
        lifecycle_cases=[lifecycle_case],
        lifecycle_read_models_by_case={"case-nvda": read_model},
        day_end_strict=True,
    )

    assert dataset["status"] == "untrusted"
    assert dataset["checks"][1]["reason_code"] == (
        "POSITION_DIVERGENCE_PERSISTENT"
    )


def _open_event(*, event_id: str, deal_id: str, strike: float = 100) -> dict:
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=1_700_000_000_000,
        contract_key=ContractKey.from_values(
            broker="富途",
            account="lx",
            underlying_symbol="NVDA",
            option_type="put",
            strike=strike,
            expiration_ymd="2026-07-17",
                ),
        contracts=1,
        price=1,
        currency="USD",
        source="futu",
        multiplier=100,
        lot_id=f"lot-{event_id}",
        # §9.2 step 3: the contract key no longer carries the position side, so the
        # event must declare the trade side the lot direction is derived from.
        raw_payload={"deal_id": deal_id, "side": "sell"},
    ).to_dict()


def test_full_replay_mismatch_blocks_position_consumers() -> None:
    datasets = build_ledger_datasets(
        repo=_LedgerRepo([_open_event(event_id="event-1", deal_id="deal-1")], []),
        accounts=["lx"],
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
    )
    assert datasets[0]["status"] == "untrusted"
    assert datasets[0]["checks"][0]["reason_code"] == "LEDGER_REPLAY_MISMATCH"
    assert "close_advice" in datasets[0]["blocked_consumers"]


def _void_event(*, event_id: str, target_event_id: str) -> dict:
    return TradeEvent(
        event_id=event_id,
        event_type="void",
        event_time_ms=1_700_000_001_000,
        contract_key=ContractKey.from_values(
            broker="富途",
            account="lx",
            underlying_symbol="NVDA",
            option_type="put",
            strike=100,
            expiration_ymd="2026-07-17",
        ),
        contracts=0,
        price=0,
        currency="USD",
        source="futu",
        target_event_id=target_event_id,
    ).to_dict()


def test_void_cleared_account_is_not_a_vacuous_replay_failure() -> None:
    datasets = build_ledger_datasets(
        repo=_LedgerRepo(
            [
                _open_event(event_id="event-1", deal_id="deal-1"),
                _void_event(event_id="event-2", target_event_id="event-1"),
            ],
            [],
        ),
        accounts=["lx"],
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
    )
    assert datasets[0]["status"] == "trusted"
    assert datasets[0]["checks"][0]["status"] == "pass"
    assert datasets[0]["checks"][0]["reason_code"] == "LEDGER_REPLAY_MATCHED"


def test_non_lot_materializing_events_alone_do_not_fail_the_replay_check() -> None:
    """``open`` is the only lot-materializing event type.

    An account whose active events are only closes/adjustments/verifications
    compares empty-vs-empty legitimately: those types edit or observe a lot an
    open must have created. The vacuous guard must key off active opens, not
    every active event, or it blocks position consumers for such accounts.
    """
    datasets = build_ledger_datasets(
        repo=_LedgerRepo(
            [
                TradeEvent(
                    event_id="event-verify-1",
                    event_type="verification",
                    event_time_ms=1_700_000_000_000,
                    contract_key=ContractKey.from_values(
                        broker="富途",
                        account="lx",
                        underlying_symbol="NVDA",
                        option_type="put",
                        strike=100,
                        expiration_ymd="2026-07-17",
                    ),
                    contracts=0,
                    price=0,
                    currency="USD",
                    source="futu",
                    lot_id="lot-1",
                    raw_payload={"deal_id": "deal-1"},
                ).to_dict(),
            ],
            [],
        ),
        accounts=["lx"],
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
    )
    # OM-LED-002 has its own verdict for a lone verification event; the replay
    # check under test here must not be the one failing it.
    replay = _replay_check(datasets[0])
    assert replay["status"] == "pass"
    assert replay["reason_code"] == "LEDGER_REPLAY_MATCHED"


def _replay_check(dataset: dict) -> dict:
    return next(item for item in dataset["checks"] if item["check_id"] == "OM-LED-001")


def test_duplicate_broker_identity_with_economic_conflict_is_blocking() -> None:
    events = [
        _open_event(event_id="event-1", deal_id="same-deal", strike=100),
        _open_event(event_id="event-2", deal_id="same-deal", strike=101),
    ]
    datasets = build_ledger_datasets(
        repo=_LedgerRepo(events, []),
        accounts=["lx"],
        market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
    )
    conflict = datasets[0]["checks"][1]
    assert conflict["status"] == "fail"
    assert conflict["observed"]["economic_conflict_count"] == 1


def test_lifecycle_deadline_handles_friday_weekend_and_holiday() -> None:
    expiration = date(2026, 7, 3)
    trading_days = [date(2026, 7, 7), date(2026, 7, 8)]
    first_deep = datetime(2026, 7, 7, 13, tzinfo=timezone.utc)
    assert lifecycle_deadline(
        expiration=expiration,
        trading_days=trading_days,
        first_deep_reconcile_at=first_deep,
    ) == datetime(2026, 7, 7, 15, tzinfo=timezone.utc)


def test_regression_eleven_overdue_lifecycle_cases_are_classified_stale() -> None:
    now = datetime(2026, 7, 8, 16, tzinfo=timezone.utc)
    cases = [
        {
            "case_id": f"stale-{index:02d}",
            "account": "lx",
            "symbol": "NVDA",
            "expiration_ymd": "2026-07-03",
            "status": "waiting_settlement_evidence",
        }
        for index in range(1, 12)
    ]
    first_deep = {item["case_id"]: "2026-07-07T13:00:00Z" for item in cases}
    datasets = build_lifecycle_datasets(
        cases=cases,
        evidence_rows=[],
        account="lx",
        market="us",
        observed_at_utc="2026-07-08T16:00:00Z",
        now=now,
        trading_days=[date(2026, 7, 7), date(2026, 7, 8)],
        first_deep_by_case=first_deep,
        timing_policies_by_case={
            item["case_id"]: {
                "settlement_deadline_ms": int(
                    datetime(
                        2026,
                        7,
                        7,
                        15,
                        tzinfo=timezone.utc,
                    ).timestamp()
                    * 1000
                )
            }
            for item in cases
        },
    )
    assert len(datasets) == 11
    assert {item["status"] for item in datasets} == {"untrusted"}
    assert {item["checks"][0]["reason_code"] for item in datasets} == {
        "LIFECYCLE_EVIDENCE_OVERDUE"
    }


def test_lifecycle_external_adjustment_and_legacy_gap_are_separate() -> None:
    now = datetime(2026, 7, 8, 16, tzinfo=timezone.utc)
    datasets = build_lifecycle_datasets(
        cases=[
            {
                "case_id": "external",
                "account": "lx",
                "symbol": "NVDA",
                "expiration_ymd": "2026-07-03",
                "status": "external_adjustment_pending_review",
            },
            {
                "case_id": "legacy",
                "account": "lx",
                "symbol": "NVDA",
                "expiration_ymd": "2025-01-01",
                "status": "pending",
                "legacy_evidence_gap": True,
            },
        ],
        evidence_rows=[],
        account="lx",
        market="us",
        observed_at_utc="2026-07-08T16:00:00Z",
        now=now,
        trading_days=[date(2026, 7, 7)],
        first_deep_by_case={},
    )
    by_case = {item["scope"]["lifecycle_case_id"]: item for item in datasets}
    assert by_case["external"]["status"] == "unavailable"
    assert by_case["external"]["checks"][0]["check_id"] == "OM-LCY-002"
    assert by_case["legacy"]["dataset_id"] == "om.lifecycle_history"
    assert by_case["legacy"]["checks"][0]["check_id"] == "OM-LCY-003"


def test_lifecycle_excludes_superseded_and_other_market_cases() -> None:
    now = datetime(2026, 7, 8, 16, tzinfo=timezone.utc)
    datasets = build_lifecycle_datasets(
        cases=[
            {
                "case_id": "superseded-us",
                "account": "lx",
                "market": "US",
                "symbol": "NVDA",
                "status": "superseded",
            },
            {
                "case_id": "pending-hk",
                "account": "lx",
                "market": "HK",
                "symbol": "0700.HK",
                "status": "waiting_settlement_evidence",
            },
            {
                "case_id": "pending-us",
                "account": "lx",
                "market": "US",
                "symbol": "NVDA",
                "status": "waiting_settlement_evidence",
            },
        ],
        evidence_rows=[],
        account="lx",
        market="us",
        observed_at_utc="2026-07-08T16:00:00Z",
        now=now,
        trading_days=[],
        first_deep_by_case={},
    )

    assert [item["scope"]["lifecycle_case_id"] for item in datasets] == ["pending-us"]


def test_lifecycle_quality_shadow_matches_both_sides_of_deadline() -> None:
    deadline_ms = 1_800_000
    case = {
        "case_id": "pending-us",
        "account": "lx",
        "market": "US",
        "symbol": "NVDA",
        "status": "waiting_settlement_evidence",
    }
    read_model = {
        "pending_until_ms": deadline_ms,
        "reason_state": "cause_pending",
        "timing_policy_hash": "a" * 64,
    }
    detail = {
        "case_id": "pending-us",
        "market": "US",
        "status": "waiting_settlement_evidence",
        "trust_class": "trusted",
        "evidence_count": 0,
        "settlement_deadline_ms": deadline_ms,
        "reason_state": "cause_pending",
        "timing_policy_hash": "a" * 64,
    }
    current_quality = {
        "schema_version": "current_lifecycle_quality.v1",
        "account": "lx",
        "aggregate_by_market": [
            {
                "market": "US",
                "total_case_count": 1,
                "status_counts": {"waiting_settlement_evidence": 1},
                "trust_class_counts": {"trusted": 1},
            }
        ],
        "operational_cases": [detail],
    }
    current_quality["aggregate_fingerprint"] = canonical_sha256(
        current_quality["aggregate_by_market"]
    )
    current_quality["detail_fingerprint"] = canonical_sha256([detail])

    for now_ms, expected_status in (
        (deadline_ms, "partial"),
        (deadline_ms + 1, "untrusted"),
    ):
        now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        legacy = build_lifecycle_datasets(
            cases=[case],
            evidence_rows=[],
            account="lx",
            market="us",
            observed_at_utc=now.isoformat(),
            now=now,
            trading_days=[],
            first_deep_by_case={},
            read_models_by_case={"pending-us": read_model},
        )
        frozen_legacy = deepcopy(legacy)
        summary, comparison = build_lifecycle_quality_migration_summary(
            legacy_datasets=legacy,
            current_quality=derive_lifecycle_quality_view(
                current_quality,
                now_ms=now_ms,
            ),
            account="lx",
            market="us",
            observed_at_utc=now.isoformat(),
            now_ms=now_ms,
            case_status_by_id={
                "pending-us": "waiting_settlement_evidence"
            },
            read_models_by_case={"pending-us": read_model},
        )

        assert legacy == frozen_legacy
        assert comparison["status"] == "matched"
        assert comparison["mismatch_samples"] == []
        assert summary["status"] == expected_status
        assert len(summary["extensions"]["operational_cases"]) == 1

    mismatched = deepcopy(current_quality)
    mismatched["operational_cases"][0]["evidence_count"] = 1
    mismatched["detail_fingerprint"] = canonical_sha256(
        mismatched["operational_cases"]
    )
    summary, comparison = build_lifecycle_quality_migration_summary(
        legacy_datasets=legacy,
        current_quality=derive_lifecycle_quality_view(
            mismatched,
            now_ms=now_ms,
        ),
        account="lx",
        market="us",
        observed_at_utc=now.isoformat(),
        now_ms=now_ms,
        case_status_by_id={"pending-us": "waiting_settlement_evidence"},
        read_models_by_case={"pending-us": read_model},
    )
    assert comparison["status"] == "mismatch"
    assert len(comparison["mismatch_samples"]) <= 10
    assert summary["status"] == "unavailable"


def test_lifecycle_quality_shadow_keeps_terminal_aggregate_counts() -> None:
    now_ms = 1_800_000
    now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    cases = [
        {
            "case_id": "terminal-trusted",
            "account": "lx",
            "market": "US",
            "symbol": "NVDA",
            "status": "ledger_written",
        },
        {
            "case_id": "terminal-legacy",
            "account": "lx",
            "market": "HK",
            "symbol": "0700.HK",
            "status": "ledger_written",
            "legacy_evidence_gap": True,
        },
        {
            "case_id": "terminal-external",
            "account": "lx",
            "market": "US",
            "symbol": "NVDA",
            "status": "ledger_written",
            "decision_type": "external_adjustment",
        },
    ]
    aggregates = [
        {
            "market": "HK",
            "total_case_count": 1,
            "status_counts": {"ledger_written": 1},
            "trust_class_counts": {"legacy_gap": 1},
        },
        {
            "market": "US",
            "total_case_count": 2,
            "status_counts": {"ledger_written": 2},
            "trust_class_counts": {
                "external_review": 1,
                "trusted": 1,
            },
        }
    ]
    current_quality = {
        "schema_version": "current_lifecycle_quality.v1",
        "account": "lx",
        "aggregate_by_market": aggregates,
        "operational_cases": [],
        "aggregate_fingerprint": canonical_sha256(aggregates),
        "detail_fingerprint": canonical_sha256([]),
    }
    current_view = derive_lifecycle_quality_view(
        current_quality,
        now_ms=now_ms,
    )
    expected = {
        "hk": (
            {"untrusted": 1},
            {"option_performance": 1},
        ),
        "us": (
            {"trusted": 1, "unavailable": 1},
            {
                "close_advice": 1,
                "lifecycle_report": 1,
                "option_performance": 1,
            },
        ),
    }
    for market, (status_counts, blocked_counts) in expected.items():
        legacy = build_lifecycle_datasets(
            cases=cases,
            evidence_rows=[],
            account="lx",
            market=market,
            observed_at_utc=now.isoformat(),
            now=now,
            trading_days=[],
            first_deep_by_case={},
        )
        summary, comparison = build_lifecycle_quality_migration_summary(
            legacy_datasets=legacy,
            current_quality=current_view,
            account="lx",
            market=market,
            observed_at_utc=now.isoformat(),
            now_ms=now_ms,
            case_status_by_id={
                item["case_id"]: "ledger_written" for item in cases
            },
            read_models_by_case={},
        )

        aggregate = summary["extensions"]["aggregate"]
        assert comparison["status"] == "matched"
        assert aggregate["dataset_status_counts"] == status_counts
        assert aggregate["blocked_consumer_counts"] == blocked_counts
        assert summary["extensions"]["operational_cases"] == []


def test_lifecycle_quality_conflict_never_gets_deadline_grace() -> None:
    deadline_ms = 1_800_000
    case = {
        "case_id": "conflict-us",
        "account": "lx",
        "market": "US",
        "symbol": "NVDA",
        "status": "conflict",
    }
    read_model = {
        "pending_until_ms": deadline_ms,
        "reason_state": "conflict",
        "timing_policy_hash": "a" * 64,
    }
    detail = {
        "case_id": "conflict-us",
        "market": "US",
        "status": "conflict",
        "trust_class": "trusted",
        "evidence_count": 0,
        "settlement_deadline_ms": deadline_ms,
        "reason_state": "conflict",
        "timing_policy_hash": "a" * 64,
    }
    current_quality = {
        "schema_version": "current_lifecycle_quality.v1",
        "account": "lx",
        "aggregate_by_market": [
            {
                "market": "US",
                "total_case_count": 1,
                "status_counts": {"conflict": 1},
                "trust_class_counts": {"trusted": 1},
            }
        ],
        "operational_cases": [detail],
    }
    current_quality["aggregate_fingerprint"] = canonical_sha256(
        current_quality["aggregate_by_market"]
    )
    current_quality["detail_fingerprint"] = canonical_sha256([detail])

    for now_ms in (deadline_ms - 1, deadline_ms, deadline_ms + 1):
        now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        legacy = build_lifecycle_datasets(
            cases=[case],
            evidence_rows=[],
            account="lx",
            market="us",
            observed_at_utc=now.isoformat(),
            now=now,
            trading_days=[],
            first_deep_by_case={},
            read_models_by_case={"conflict-us": read_model},
        )
        summary, comparison = build_lifecycle_quality_migration_summary(
            legacy_datasets=legacy,
            current_quality=derive_lifecycle_quality_view(
                current_quality,
                now_ms=now_ms,
            ),
            account="lx",
            market="us",
            observed_at_utc=now.isoformat(),
            now_ms=now_ms,
            case_status_by_id={"conflict-us": "conflict"},
            read_models_by_case={"conflict-us": read_model},
        )

        assert comparison["status"] == "matched"
        assert summary["status"] == "untrusted"
        assert summary["blocked_consumers"] == [
            "close_advice",
            "lifecycle_report",
            "option_performance",
        ]


def test_runtime_service_and_timer_checks_require_checked_active_units() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    checks = build_runtime_checks(
        runtime_statuses=[
            {
                "service_profile": {
                    "loaded": True,
                    "status_checked": True,
                    "services": [
                        {"name": "options-monitor.service", "status": "ok"},
                        {"name": "options-monitor-us.timer", "status": "ok"},
                    ],
                },
                "trade_intake": {"enabled": False},
            }
        ],
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
    )
    by_id = {item["check_id"]: item for item in checks}
    assert by_id["RT-OM-001"]["status"] == "pass"
    assert by_id["RT-OM-002"]["reason_code"] == "LISTENER_NOT_APPLICABLE"
    assert by_id["RT-OM-003"]["status"] == "pass"

    failed = build_runtime_checks(
        runtime_statuses=[
            {
                "service_profile": {
                    "loaded": True,
                    "status_checked": True,
                    "services": [
                        {"name": "options-monitor.service", "status": "warn"},
                        {"name": "options-monitor-us.timer", "status": "warn"},
                    ],
                },
                "trade_intake": {"enabled": False},
            }
        ],
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
    )
    failed_by_id = {item["check_id"]: item for item in failed}
    assert failed_by_id["RT-OM-001"]["status"] == "fail"
    assert failed_by_id["RT-OM-003"]["status"] == "fail"


def test_runtime_service_check_accepts_inactive_timer_triggered_oneshot() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    checks = build_runtime_checks(
        runtime_statuses=[
            {
                "service_profile": {
                    "loaded": True,
                    "status_checked": True,
                    "services": [
                        {
                            "name": "options-monitor-quality-refresh.service",
                            "status": "warn",
                            "returncode": 3,
                            "stdout": "inactive",
                        },
                        {
                            "name": "options-monitor-quality-refresh.timer",
                            "status": "ok",
                            "returncode": 0,
                            "stdout": "active",
                        },
                        {
                            "name": "options-monitor-trade-intake.service",
                            "status": "ok",
                            "returncode": 0,
                            "stdout": "active",
                        },
                    ],
                },
                "trade_intake": {"enabled": False},
            }
        ],
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
    )

    by_id = {item["check_id"]: item for item in checks}
    assert by_id["RT-OM-001"]["status"] == "pass"
    assert by_id["RT-OM-001"]["reason_code"] == "OM_SERVICES_ACTIVE"
    assert by_id["RT-OM-001"]["observed"] == {
        "service_count": 2,
        "statuses": ["ok"],
        "normally_inactive_timer_service_count": 1,
    }
    assert by_id["RT-OM-003"]["status"] == "pass"


def test_runtime_service_check_rejects_failed_timer_triggered_oneshot() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    checks = build_runtime_checks(
        runtime_statuses=[
            {
                "service_profile": {
                    "loaded": True,
                    "status_checked": True,
                    "services": [
                        {
                            "name": "options-monitor-quality-refresh.service",
                            "status": "warn",
                            "returncode": 3,
                            "stdout": "failed",
                        },
                        {
                            "name": "options-monitor-quality-refresh.timer",
                            "status": "ok",
                            "returncode": 0,
                            "stdout": "active",
                        },
                    ],
                },
                "trade_intake": {"enabled": False},
            }
        ],
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
    )

    by_id = {item["check_id"]: item for item in checks}
    assert by_id["RT-OM-001"]["status"] == "fail"
    assert by_id["RT-OM-001"]["reason_code"] == "OM_SERVICE_INACTIVE"


def test_runtime_service_check_accepts_activating_timer_triggered_oneshot() -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    checks = build_runtime_checks(
        runtime_statuses=[
            {
                "service_profile": {
                    "loaded": True,
                    "status_checked": True,
                    "services": [
                        {
                            "name": "options-monitor-quality-refresh.service",
                            "status": "warn",
                            "returncode": 3,
                            "stdout": "activating",
                        },
                        {
                            "name": "options-monitor-quality-refresh.timer",
                            "status": "ok",
                            "returncode": 0,
                            "stdout": "active",
                        },
                    ],
                },
                "trade_intake": {"enabled": False},
            }
        ],
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
    )

    by_id = {item["check_id"]: item for item in checks}
    assert by_id["RT-OM-001"]["status"] == "pass"
    assert by_id["RT-OM-001"]["reason_code"] == "OM_SERVICES_ACTIVE"


def test_trade_intake_uses_embedded_state_for_pending_age(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    datasets = build_trade_intake_datasets(
        runtime_statuses=[
            {
                "trade_intake": {
                    "enabled": True,
                    "sources": [
                        {
                            "id": "lx",
                            "account": "lx",
                            "enabled": True,
                            "state": {
                                "path": ".../trade_intake_state.json",
                                "json": {
                                    "unresolved_deal_ids": {
                                        "deal-1": {
                                            "updated_at": "2026-07-13T09:50:00+00:00"
                                        },
                                        "legacy-deal": {
                                            "receipt": {
                                                "updated_at": "2026-07-13T09:55:00+00:00"
                                            }
                                        }
                                    }
                                },
                            },
                            "summary": {
                                "pending_count": 2,
                                "failed_count": 0,
                                "unresolved_count": 2,
                                "reconciliation_preview_available": True,
                                "pending_after_reconcile_count": 0,
                            },
                        }
                    ],
                }
            }
        ],
        accounts=["lx"],
        market="us",
        repo_root=tmp_path,
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
    )

    pending = datasets[0]["checks"][0]
    assert pending["status"] == "fail"
    assert pending["reason_code"] == "INTAKE_PENDING_OVERDUE"
    assert pending["observed"]["oldest_pending_age_seconds"] == 600


def test_trade_intake_does_not_trust_state_only_lifecycle_delegation(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    datasets = build_trade_intake_datasets(
        runtime_statuses=[
            {
                "trade_intake": {
                    "enabled": True,
                    "sources": [
                        {
                            "id": "lx",
                            "account": "lx",
                            "enabled": True,
                            "state": {
                                "json": {
                                    "unresolved_deal_ids": {
                                        "deal-1": {
                                            "reason": "waiting_settlement_evidence",
                                            "updated_at": "2026-07-13T09:00:00+00:00",
                                            "diagnostics": {
                                                "broker_evidence_accepted": True,
                                                "lifecycle_adoption": {
                                                    "status": "accepted",
                                                    "case_id": "case-1",
                                                },
                                            },
                                        }
                                    }
                                }
                            },
                            "summary": {
                                "pending_count": 1,
                                "failed_count": 0,
                                "unresolved_count": 1,
                                "reconciliation_preview_available": True,
                                "pending_after_reconcile_count": 1,
                            },
                        }
                    ],
                }
            }
        ],
        accounts=["lx"],
        market="us",
        repo_root=tmp_path,
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
    )

    dataset = datasets[0]
    assert dataset["status"] == "untrusted"
    assert set(dataset["blocked_consumers"]) == {
        "option_position_report",
        "lifecycle",
        "close_advice",
    }
    by_id = {item["check_id"]: item for item in dataset["checks"]}
    assert by_id["OM-INT-001"]["reason_code"] == "INTAKE_PENDING_OVERDUE"
    assert by_id["OM-INT-001"]["observed"]["delegated_lifecycle_pending_count"] == 0
    assert by_id["OM-INT-002"]["reason_code"] == "INTAKE_UNRESOLVED_ROWS"
    assert by_id["OM-INT-003"]["reason_code"] == "BROKER_DEAL_LOCAL_EVENT_MISSING"


def test_trade_intake_uses_bridge_aware_reconciliation_delegation(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    datasets = build_trade_intake_datasets(
        runtime_statuses=[
            {
                "trade_intake": {
                    "enabled": True,
                    "sources": [
                        {
                            "id": "lx",
                            "account": "lx",
                            "enabled": True,
                            "state": {
                                "json": {
                                    "unresolved_deal_ids": {
                                        "futu:lx:1001:deal-legacy": {
                                            "reason": "lifecycle_case_futu_account_mismatch",
                                            "updated_at": "2026-07-13T09:00:00+00:00",
                                        }
                                    }
                                }
                            },
                            "summary": {
                                "pending_count": 1,
                                "failed_count": 0,
                                "unresolved_count": 1,
                                "reconciliation_preview_available": True,
                                "delegated_lifecycle_pending_count": 1,
                                "delegated_lifecycle_pending_deal_ids": [
                                    "futu:lx:1001:deal-legacy"
                                ],
                                "pending_after_reconcile_count": 1,
                                "actionable_pending_after_reconcile_count": 0,
                            },
                        }
                    ],
                }
            }
        ],
        accounts=["lx"],
        market="us",
        repo_root=tmp_path,
        observed_at_utc="2026-07-13T10:00:00Z",
        now=now,
    )

    dataset = datasets[0]
    assert dataset["status"] == "trusted"
    assert dataset["blocked_consumers"] == []
    by_id = {item["check_id"]: item for item in dataset["checks"]}
    assert by_id["OM-INT-001"]["observed"]["delegated_lifecycle_pending_count"] == 1
    assert by_id["OM-INT-002"]["reason_code"] == "INTAKE_NO_UNRESOLVED_ROWS"
    assert by_id["OM-INT-003"]["observed"]["missing_local_terminal_count"] == 0


def _quality_split_events(*, quantities=(1, 2), account="lx", canonical=False):
    # Sanitized shapes of the confirmed lx HK 1+2 and sy US 1+1 close groups.
    events = []
    closes = []
    for index, quantity in enumerate(quantities, 1):
        opened = _open_event(event_id=f"open-{index}", deal_id=f"open-{index}")
        opened["contract_key"]["account"] = account
        opened["contracts"] = quantity
        opened["raw_payload"]["futu_account_id"] = "123"
        events.append(opened)
        closed = deepcopy(opened)
        closed.update(event_id=f"close-{index}", event_type="close", lot_id=None,
                      target_lot_id=opened["lot_id"], event_time_ms=opened["event_time_ms"] + 1)
        closed["raw_payload"] = {
            "source_deal_id": "split-close", "futu_account_id": "123", "qty": sum(quantities),
            "side": "buy", "internal_account": account,
            "broker_deal_completion": {
                "source_deal_id": "split-close", "split_index": index,
                "split_count": len(quantities), "allocated_contracts": quantity,
                "expected_contracts": sum(quantities),
            },
        }
        if canonical:
            closed["raw_payload"]["execution_input"] = {
                "broker_account_ref": {"broker_account_id": "futu:REAL:123", "broker_id": "futu",
                                       "external_account_id": "123", "environment": "REAL"},
                "instrument_ref": {"asset_type": "option", "symbol": "NVDA", "market": "US",
                                   "currency": "USD", "option_type": "put", "strike": "100",
                                   "expiration_ymd": "2026-07-17", "multiplier": "100"},
                "external_id_namespace": "futu.deal", "external_execution_id": "split-close",
                "quantity": str(sum(quantities)), "price": "1", "side": "buy",
                "currency": "USD", "position_effect": "close", "occurred_at_utc": "2023-11-14T22:13:20.001Z",
            }
        closes.append(closed)
    return events + closes


def _split_conservation_check(events, *, account="lx"):
    from src.application.ledger.api import project_trade_event_log

    projected = project_trade_event_log(events)
    dataset = build_ledger_datasets(
        repo=_LedgerRepo(events, projected.lots), accounts=[account], market="us",
        observed_at_utc="2026-07-13T10:00:00Z",
    )[0]
    return dataset, dataset["checks"][1]


@pytest.mark.parametrize("account,quantities", [("lx", (1, 2)), ("sy", (1, 1))])
@pytest.mark.parametrize("canonical", [False, True])
def test_valid_split_close_is_conserved_by_full_ledger_quality(account, quantities, canonical):
    dataset, check = _split_conservation_check(
        _quality_split_events(quantities=quantities, account=account, canonical=canonical), account=account,
    )
    assert check["observed"] == {
        "duplicate_broker_identity_count": 0, "economic_conflict_count": 0,
        "projection_error_count": 0,
    }
    assert dataset["status"] == "trusted"


@pytest.mark.parametrize("change", [
    "missing_leg", "void", "duplicate_index", "duplicate_target", "actual_quantity",
    "fractional_allocation", "boolean_allocation", "nonfinite_allocation", "price", "contract",
    "account", "physical", "environment", "namespace", "missing_identity", "broker_quantity",
    "raw_environment", "raw_namespace", "raw_account", "invalid_completion", "missing_all_identity",
    "fractional_quantity", "boolean_quantity", "nonfinite_quantity",
])
def test_invalid_split_close_remains_blocking_in_full_ledger_quality(change):
    events = _quality_split_events(quantities=(1, 1), canonical=True)
    row = events[-1]
    completion = row["raw_payload"]["broker_deal_completion"]
    if change == "missing_leg":
        events.pop()
    elif change == "void":
        events.append({**deepcopy(row), "event_id": "void", "event_type": "void",
                       "contracts": 0, "target_event_id": row["event_id"], "raw_payload": {},
                       "event_time_ms": row["event_time_ms"] + 1})
    elif change == "duplicate_index":
        completion["split_index"] = 1
    elif change == "duplicate_target":
        row["target_lot_id"] = events[-2]["target_lot_id"]
    elif change == "actual_quantity":
        row["contracts"] = 2
    elif change in {"fractional_quantity", "boolean_quantity", "nonfinite_quantity"}:
        row["contracts"] = {"fractional_quantity": 1.5, "boolean_quantity": True,
                            "nonfinite_quantity": float("inf")}[change]
    elif change.endswith("_allocation"):
        completion["allocated_contracts"] = {
            "fractional_allocation": 1.5, "boolean_allocation": True,
            "nonfinite_allocation": float("inf"),
        }[change]
    elif change == "price":
        row["price"] = 2
    elif change == "contract":
        row["contract_key"]["strike"] = 101
    elif change == "account":
        row["contract_key"]["account"] = "sy"
        row["raw_payload"]["internal_account"] = "sy"
    elif change == "raw_account":
        row["raw_payload"]["internal_account"] = "sy"
    elif change == "physical":
        row["raw_payload"]["execution_input"]["broker_account_ref"]["external_account_id"] = "999"
    elif change == "environment":
        row["raw_payload"]["execution_input"]["broker_account_ref"]["environment"] = "SIMULATE"
    elif change == "namespace":
        row["raw_payload"]["execution_input"]["external_id_namespace"] = "other.deal"
    elif change == "missing_identity":
        row["raw_payload"].pop("execution_input")
        row["raw_payload"].pop("futu_account_id")
    elif change == "missing_all_identity":
        for key in ("source_deal_id", "futu_account_id", "execution_input"):
            row["raw_payload"].pop(key)
    elif change == "broker_quantity":
        row["raw_payload"]["qty"] = 3
    elif change in {"raw_environment", "raw_namespace"}:
        row["raw_payload"]["environment" if change == "raw_environment" else "external_id_namespace"] = (
            "SIMULATE" if change == "raw_environment" else "other.deal"
        )
    else:
        row["raw_payload"]["broker_deal_completion"] = "invalid"
    dataset, check = _split_conservation_check(events)
    assert check["status"] == "fail"
    assert dataset["status"] == "untrusted"
    assert "OM-LED-002" in dataset["blocked_by"]


def test_scoped_execution_aliases_count_true_duplicate_once():
    events = _quality_split_events(quantities=(1, 1), canonical=True)
    events[-1]["target_lot_id"] = events[-2]["target_lot_id"]
    _dataset, check = _split_conservation_check(events)
    assert check["observed"]["duplicate_broker_identity_count"] == 1


def test_same_naked_deal_id_in_different_physical_scopes_is_not_duplicate():
    events = [_open_event(event_id=f"event-{index}", deal_id="shared") for index in (1, 2)]
    for index, event in enumerate(events, 1):
        event["raw_payload"]["futu_account_id"] = str(index)
    _dataset, check = _split_conservation_check(events)
    assert check["observed"]["duplicate_broker_identity_count"] == 0
    assert check["observed"]["economic_conflict_count"] == 0


def test_quality_uses_one_complete_group_for_mixed_canonical_and_legacy_aliases():
    events = _quality_split_events(canonical=True)
    events[-1]["raw_payload"].pop("execution_input")
    dataset, check = _split_conservation_check(events)
    assert check["status"] == "pass"
    assert dataset["status"] == "trusted"


def test_legacy_single_alias_does_not_exempt_canonical_duplicate_group():
    events = _quality_split_events(quantities=(1, 1), canonical=True)
    for row in events[-2:]:
        row["raw_payload"].pop("broker_deal_completion")
    events[-1]["raw_payload"].pop("execution_input")
    _dataset, check = _split_conservation_check(events)
    assert check["status"] == "fail"
    assert check["observed"]["duplicate_broker_identity_count"] == 1


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("change", ["price", "quantity_missing", "multiplier", "strike", "side", "currency"])
def test_split_quality_requires_source_economics_not_only_agreement_between_events(canonical, change):
    from src.application.trades.deal_identity import completed_ledger_deal_keys

    events = _quality_split_events(canonical=canonical)
    for row in events[-2:]:
        raw = row["raw_payload"]
        if change == "price":
            raw["price"] = "1"
            row["price"] = 2
        elif change == "multiplier":
            raw["multiplier"] = "100"
            row["multiplier"] = 10
        elif change in {"strike", "side", "currency"}:
            raw[change] = {"strike": "101", "side": "sell", "currency": "HKD"}[change]
            if canonical:
                target = raw["execution_input"]["instrument_ref"] if change == "strike" else raw["execution_input"]
                target[change] = raw[change]
        else:
            raw.pop("qty")
            if canonical:
                raw["execution_input"].pop("quantity")
    if change == "side":
        # §9.2 step 3: the close side is no longer redundant with the contract key,
        # so a coherent row pair declaring ``sell`` is a valid long close. Only the
        # target lot knows it is short, so the tamper surfaces as a projection-level
        # ``target_contract_mismatch`` and the dataset still fails closed. The exact
        # counts are the point: the economic layer is blind to this tamper by
        # construction (step 3 removed ``position_side`` from deal identity), so all
        # of the detection is projection-level and must cover both legs.
        dataset, check = _split_conservation_check(events)
        assert check["observed"] == {
            "duplicate_broker_identity_count": 0,
            "economic_conflict_count": 0,
            "projection_error_count": 2,
        }
        assert check["status"] == "fail"
        assert dataset["status"] == "untrusted"
        return
    assert completed_ledger_deal_keys(events[-2:]) == set()
    dataset, check = _split_conservation_check(events)
    assert check["status"] == "fail"
    assert dataset["status"] == "untrusted"


def test_complete_split_with_conflicting_canonical_and_source_ids_remains_untrusted():
    from src.application.trades.deal_identity import completed_ledger_deal_keys

    events = _quality_split_events(canonical=True)
    for row in events[-2:]:
        raw = row["raw_payload"]
        raw["source_deal_id"] = "different-execution"
        raw["broker_deal_completion"]["source_deal_id"] = "different-execution"
    assert completed_ledger_deal_keys(events[-2:]) == set()
    dataset, check = _split_conservation_check(events)
    assert check["status"] == "fail"
    assert dataset["status"] == "untrusted"


@pytest.mark.parametrize("namespace_field", ["external_id_namespace", "execution_id_namespace"])
def test_split_quality_treats_explicit_and_implicit_source_namespace_equally(namespace_field):
    events = _quality_split_events(canonical=True)
    events[-1]["raw_payload"][namespace_field] = "futu.deal"
    dataset, check = _split_conservation_check(events)
    assert check["status"] == "pass"
    assert dataset["status"] == "trusted"
    events[-1]["raw_payload"][namespace_field] = "other.deal"
    dataset, check = _split_conservation_check(events)
    assert check["status"] == "fail"
    assert dataset["status"] == "untrusted"


@pytest.mark.parametrize("second_identity", ["same", "legacy", "physical", "namespace", "environment"])
def test_persisted_split_quality_checks_physical_execution_before_account_scope(tmp_path, second_identity):
    from domain.domain.trade_execution import execution_identity_from_input
    from src.application.ledger.api import refresh_position_lot_projection
    from src.application.ledger.repository import SQLiteOptionPositionsRepository

    # One physical account assigned to two internal labels represents historical
    # misattribution, not a supported account mapping. Distinct scope is a control.
    events = []
    for account in ("lx", "sy"):
        group = _quality_split_events(canonical=account == "sy" and second_identity != "legacy", account=account)
        for event in group:
            for field in ("event_id", "lot_id", "target_lot_id"):
                if event.get(field):
                    event[field] = account + "-" + event[field]
            raw = event["raw_payload"]
            if event["event_type"] == "open":
                raw["deal_id"] = account + "-" + raw["deal_id"]
            execution = raw.get("execution_input")
            if execution:
                ref = execution["broker_account_ref"]
                if second_identity == "physical":
                    raw["futu_account_id"] = ref["external_account_id"] = "456"
                    ref["broker_account_id"] = "futu:REAL:456"
                elif second_identity == "namespace":
                    execution["external_id_namespace"] = "other.deal"
                elif second_identity == "environment":
                    raw["environment"] = ref["environment"] = "SIMULATE"
                    ref["broker_account_id"] = "futu:SIMULATE:123"
                raw["execution_id"] = execution_identity_from_input(execution)
        events.extend(group)
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    for event in events:
        assert repo.upsert_trade_event(event)
    refresh_position_lot_projection(repo)
    persisted = repo.list_trade_events()
    assert len(persisted) == 8
    assert sum(event["contracts"] for event in persisted if event["event_type"] == "close") == 6
    expected = "fail" if second_identity in {"same", "legacy"} else "pass"
    for accounts in (["lx", "sy"], ["lx"], ["sy"]):
        datasets = build_ledger_datasets(
            repo=repo, accounts=accounts, market="us", observed_at_utc="2026-09-13T14:00:00Z",
        )
        for dataset in datasets:
            assert dataset["checks"][0]["status"] == "pass"
            assert dataset["checks"][1]["status"] == expected
            assert dataset["status"] == ("untrusted" if expected == "fail" else "trusted")


def _public_assignment_repo(tmp_path):
    from domain.domain.option_lifecycle import expiration_observation_start_ms
    from src.application.ledger.manual_trades import persist_manual_open_event
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.lifecycle_reconciliation import discover_lifecycle_cases
    from src.application.trades.resolver import resolve_trade_deal
    from test_trades_resolver_close import _deal

    repo = SQLiteOptionPositionsRepository(tmp_path / "assignment.sqlite3")
    for index, count in enumerate((1, 2)):
        persist_manual_open_event(
            repo,
            broker="富途", account="lx", symbol="TIGR", option_type="put",
            side="short", contracts=count, currency="USD", strike=6.0,
            multiplier=100, expiration_ymd="2026-05-22", premium_per_share=0.2,
            opened_at_ms=1779129617118 + index * 1000,
        )
    observed = expiration_observation_start_ms("2026-05-22", "US")
    discovery = discover_lifecycle_cases(repo, account="lx", observed_at_ms=observed, apply_changes=True)
    assert len(discovery["created_case_ids"]) == 1
    option = resolve_trade_deal(_deal(
        deal_id="quality-option", symbol="TIGR", contracts=3, price=0.0,
        strike=6.0, expiration_ymd="2026-05-22", currency="USD",
        trade_time_ms=observed + 1000,
        raw_payload={"deal_id": "quality-option", "code": "US.TIGR260522P6000"},
    ), repo=repo, state={}, apply_changes=True)
    assert option.status == "unresolved"
    stock = resolve_trade_deal(_deal(
        deal_id="quality-stock", order_id="stock-order", symbol="TIGR",
        option_type=None, side="buy", position_effect=None, contracts=300,
        price=6.0, strike=None, multiplier=None, expiration_ymd=None,
        currency="USD", trade_time_ms=observed + 2000,
        raw_payload={"deal_id": "quality-stock", "code": "US.TIGR"},
    ), repo=repo, state={}, apply_changes=True)
    assert stock.status == "applied"
    assert stock.action == "assignment"
    terminal = [row for row in repo.list_trade_events() if row["event_type"] == "assignment"]
    assert len(terminal) == 2
    assert sorted(row["raw_payload"]["stock_settlement"]["shares"] for row in terminal) == [100, 200]
    return repo


def _assignment_conservation(repo):
    datasets = build_ledger_datasets(repo=repo, accounts=["lx"], market="us", observed_at_utc="2026-05-23T00:00:00Z")
    return next(check for check in datasets[0]["checks"] if check["check_id"] == "OM-LED-002")


def test_public_assignment_complete_stock_allocation_passes_ledger_quality(tmp_path):
    repo = _public_assignment_repo(tmp_path)
    check = _assignment_conservation(repo)
    assert check["status"] == "pass", check


class _AssignmentEvidenceRepo(_LedgerRepo):
    def __init__(self, repo):
        super().__init__(deepcopy(repo.list_trade_events()), deepcopy(repo.list_position_lots()))
        self.rows = deepcopy(repo.read_lifecycle_account_rows(account="lx"))
        self.read_accounts = []

    def read_lifecycle_account_rows(self, *, account):
        self.read_accounts.append(account)
        return deepcopy(self.rows)


def test_public_assignment_reads_one_coherent_snapshot_and_requires_history(tmp_path):
    repo = _public_assignment_repo(tmp_path)
    snapshot = _AssignmentEvidenceRepo(repo)
    assert _assignment_conservation(snapshot)["status"] == "pass"
    assert snapshot.read_accounts == ["lx"]
    assert _assignment_conservation(_LedgerRepo(snapshot.events, snapshot.lots))["status"] == "fail"


@pytest.mark.parametrize("mutation", [
    "missing_evidence", "missing_claim", "partial_allocation", "partial_log",
    "claim_hash", "claim_account", "claim_physical", "stock_shares", "stock_source",
    "stock_physical", "stock_price", "stock_malformed", "missing_stock_identity",
    "snapshot_content", "snapshot_void", "snapshot_extra_source", "other_account_source",
])
def test_public_assignment_quality_rejects_unproven_or_changed_stock_group(tmp_path, mutation):
    repo = _AssignmentEvidenceRepo(_public_assignment_repo(tmp_path))
    terminal = [row for row in repo.events if row["event_type"] == "assignment"]
    snapshots = [row for row in repo.rows["trade_events"] if row["event_type"] == "assignment"]
    claim = next(row for row in repo.rows["account_lifecycle_source_consumptions"] if row["source_role"] == "stock_settlement")
    if mutation == "missing_evidence":
        repo.rows["account_lifecycle_evidence"] = []
    elif mutation == "missing_claim":
        repo.rows["account_lifecycle_source_consumptions"].remove(claim)
    elif mutation == "partial_allocation":
        repo.rows["account_lifecycle_allocations"].pop()
    elif mutation == "partial_log":
        repo.events.remove(terminal[0])
    elif mutation == "claim_hash":
        claim["source_payload_hash"] = "invalid"
    elif mutation in {"claim_account", "claim_physical"}:
        claim["source_payload"]["account" if mutation == "claim_account" else "futu_account_id"] = "another"
    elif mutation in {"stock_shares", "stock_source", "stock_physical", "stock_price"}:
        field, value = {
            "stock_shares": ("shares", 3), "stock_source": ("source_event_id", "futu:sy:REAL_1:quality-stock"),
            "stock_physical": ("futu_account_id", "OTHER"), "stock_price": ("price", 7),
        }[mutation]
        for event in (*terminal, *snapshots):
            event["raw_payload"]["stock_settlement"][field] = value
    elif mutation == "stock_malformed":
        for event in (*terminal, *snapshots):
            event["raw_payload"]["stock_settlement"] = "invalid"
    elif mutation == "missing_stock_identity":
        for event in (*terminal, *snapshots):
            event["raw_payload"].pop("stock_settlement")
            event["raw_payload"].pop("source_event_id", None)
    elif mutation == "snapshot_content":
        snapshots[0]["raw_payload"]["concurrent_change"] = True
    elif mutation == "snapshot_void":
        void = deepcopy(snapshots[0])
        void.update(event_id="concurrent-void", event_type="void", contracts=0, target_event_id=snapshots[0]["event_id"])
        repo.rows["trade_events"].append(void)
    elif mutation in {"snapshot_extra_source", "other_account_source"}:
        extra = deepcopy(snapshots[0])
        extra["event_id"] = "extra-source-consumer"
        extra["raw_payload"]["case_id"] = "another-case"
        if mutation == "other_account_source":
            extra["account"] = "sy"
            extra["contract_key"]["account"] = "sy"
            extra["raw_payload"]["stock_settlement"]["source_event_id"] = "futu:sy:REAL_1:quality-stock"
            repo.events.append(deepcopy(extra))
        repo.rows["trade_events"].append(extra)
    check = _assignment_conservation(repo)
    assert check["status"] == "fail", (mutation, check)
    assert check["observed"]["duplicate_broker_identity_count"] > 0


@pytest.mark.parametrize("fixture_name", [
    "test_resolve_trade_lifecycle_long_call_exercise_records_exercise",
    "test_resolve_trade_lifecycle_stock_first_then_long_put_exercise_records_exercise",
    "test_resolve_trade_lifecycle_option_first_records_early_assignment_before_expiration",
])
def test_public_lifecycle_settlement_quality_supports_existing_public_flows(tmp_path, fixture_name):
    import test_trades_resolver_close
    from src.application.ledger.repository import SQLiteOptionPositionsRepository

    getattr(test_trades_resolver_close, fixture_name)(tmp_path)
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    assert _assignment_conservation(repo)["status"] == "pass"
