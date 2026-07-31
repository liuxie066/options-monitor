from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.quality.ledger_checks import build_ledger_datasets
from src.application.quality.lifecycle_checks import build_lifecycle_datasets, lifecycle_deadline
from src.application.quality.position_checks import build_position_dataset
from src.application.quality.runtime_checks import build_runtime_checks
from src.infrastructure.quality.opend_position_adapter import OpenDOptionSnapshot


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
        snapshot=_snapshot(qty=2),
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
            position_side="short",
            strike=strike,
            expiration_ymd="2026-07-17",
        ),
        contracts=1,
        price=1,
        currency="USD",
        source="futu",
        multiplier=100,
        lot_id=f"lot-{event_id}",
        raw_payload={"deal_id": deal_id},
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
                            "name": "options-monitor-quality-http.service",
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
