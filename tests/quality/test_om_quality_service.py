from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

import src.application.quality.service as service_module
from domain.domain.option_lifecycle import build_lifecycle_case
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.position_records import PositionLotRecord
from src.application.quality.service import OMQualityService
from src.application.trades.close_reason_evidence import (
    build_lifecycle_timing_policy,
)
from src.infrastructure.quality.artifact_repository import QualityArtifactRepository
from src.infrastructure.quality.control_state_repository import QualityControlStateRepository
from src.infrastructure.quality.opend_position_adapter import OpenDOptionSnapshot


class _OpenD:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def fetch(self, *, account: str, market: str, **_kwargs) -> OpenDOptionSnapshot:
        self.calls.append((account, market))
        return OpenDOptionSnapshot(
            account=account,
            market=market,
            environment="REAL",
            account_fingerprint="sha256:" + ("b" * 64),
            observed_at_utc="2026-07-13T10:00:00Z",
            snapshot_id=f"snapshot-{account}",
            complete=True,
            refresh_cache=True,
            rows=[],
            trading_days=[date(2026, 7, 13), date(2026, 7, 14)],
        )


def test_holdings_sync_quality_treats_no_activity_as_not_triggered() -> None:
    runtime = {
        "trade_intake": {
            "holdings_sync": {"enabled": True},
            "sources": [
                {
                    "account": "lx",
                    "summary": {
                        "last_push_received_utc": None,
                        "last_backfill_deal_count": 0,
                        "last_backfill_applied_count": 0,
                        "last_stock_holdings_sync_intent": None,
                    },
                }
            ],
        }
    }

    dataset = OMQualityService._holdings_sync_dataset(
        runtime_for_config=[runtime],
        account="lx",
        market="us",
        observed_at="2026-08-01T00:00:00Z",
    )

    assert dataset["status"] == "trusted"
    assert dataset["reason_codes"] == []
    assert dataset["checks"][0]["status"] == "pass"
    assert dataset["checks"][0]["reason_code"] == "STOCK_REFRESH_INTENT_NOT_TRIGGERED"
    assert dataset["checks"][0]["observed"] == {
        "intent_count": 0,
        "activity_observed": False,
    }


def test_holdings_sync_quality_preserves_missing_and_failed_evidence() -> None:
    def _dataset(summary: dict) -> dict:
        return OMQualityService._holdings_sync_dataset(
            runtime_for_config=[
                {
                    "trade_intake": {
                        "holdings_sync": {"enabled": True},
                        "sources": [{"account": "lx", "summary": summary}],
                    }
                }
            ],
            account="lx",
            market="us",
            observed_at="2026-08-01T00:00:00Z",
        )

    missing = _dataset(
        {
            "last_push_received_utc": "2026-08-01T00:00:00Z",
            "last_stock_holdings_sync_intent": None,
        }
    )
    not_applicable = _dataset(
        {
            "last_push_received_utc": "2026-08-01T00:00:00Z",
            "last_stock_holdings_sync_intent": {
                "status": "skipped",
                "reason": "option_deal",
            },
        }
    )
    failed = _dataset(
        {
            "last_push_received_utc": "2026-08-01T00:00:00Z",
            "last_stock_holdings_sync_intent": {
                "status": "rejected",
                "reason": "queue_full",
            },
        }
    )

    assert missing["status"] == "unavailable"
    assert missing["checks"][0]["reason_code"] == "STOCK_REFRESH_INTENT_EVIDENCE_MISSING"
    assert not_applicable["status"] == "trusted"
    assert not_applicable["checks"][0]["reason_code"] == "STOCK_REFRESH_INTENT_NOT_TRIGGERED"
    assert failed["status"] == "partial"
    assert failed["checks"][0]["reason_code"] == "STOCK_REFRESH_INTENT_DELAYED"


def test_service_publishes_schema_valid_artifact_without_business_writes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "option_positions.sqlite3"
    SQLiteOptionPositionsRepository(ledger_path)
    config_path = tmp_path / "config.us.json"
    config_path.write_text("{}", encoding="utf-8")
    cfg = {
        "accounts": ["lx"],
        "account_settings": {
            "lx": {
                "type": "futu",
                "futu": {
                    "host": "127.0.0.1",
                    "port": 11111,
                    "account_id": "123456",
                    "trd_env": "REAL",
                },
            }
        },
    }
    monkeypatch.setattr(service_module, "load_runtime_config", lambda **_kwargs: (config_path, cfg))
    monkeypatch.setattr(service_module, "infer_runtime_config_market", lambda **_kwargs: "US")
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    runtime = {
        "config": {"config_key": "us"},
        "summary": {"ok": True},
        "ledger_store": {"sqlite_path": str(ledger_path)},
        "trade_intake": {
            "holdings_sync": {"enabled": False},
            "sources": [
                {
                    "id": "lx",
                    "account": "lx",
                    "state": {"path": "missing-state.json"},
                    "summary": {
                        "last_heartbeat_utc": "2026-07-13T09:59:00Z",
                        "listener_status": "listening",
                        "pending_count": 0,
                        "failed_count": 0,
                        "unresolved_count": 0,
                        "reconciliation_preview_available": True,
                        "pending_after_reconcile_count": 0,
                    },
                }
            ],
        },
        "service_profile": {"loaded": True},
    }
    artifact = QualityArtifactRepository(tmp_path / "status.v1.json")
    service = OMQualityService(
        artifact_repository=artifact,
        control_repository=QualityControlStateRepository(tmp_path / "control.v1.json"),
        opend_adapter=_OpenD(),
        runtime_status_fn=lambda *_args: {"ok": True, "data": runtime},
        now_fn=lambda: now,
        instance_id="test-instance",
    )
    payload = service.refresh(config_keys=["us"])
    assert artifact.read() == payload
    assert payload["producer"]["service"] == "options-monitor"
    check_ids = {
        check["check_id"]
        for dataset in payload["datasets"]
        for check in dataset["checks"]
    }
    assert {
        "OM-INT-001",
        "OM-INT-002",
        "OM-INT-003",
        "OM-LED-001",
        "OM-LED-002",
        "OM-POS-001",
        "OM-POS-002",
        "OM-HSYNC-001",
    } <= check_ids
    runtime_ids = {item["check_id"] for item in payload["runtime"]["checks"]}
    assert {"RT-OM-001", "RT-OM-002", "RT-OM-003", "RT-OM-004"} <= runtime_ids

    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts/quality-monitoring/quality_status.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


def test_service_uses_account_coherent_lifecycle_read_for_position_coverage(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "option_positions.sqlite3"
    repo = SQLiteOptionPositionsRepository(ledger_path)
    repo.replace_position_lots(
        [
            PositionLotRecord(
                record_id="lot-nvda",
                fields={
                    "account": "lx",
                    "broker": "futu",
                    "symbol": "NVDA",
                    "option_type": "put",
                    "side": "short",
                    "contracts": 1,
                    "contracts_open": 1,
                    "contracts_closed": 0,
                    "currency": "USD",
                    "strike": 100,
                    "multiplier": 100,
                    "expiration": 1784246400000,
                    "expiration_ymd": "2026-07-17",
                    "status": "open",
                },
            )
        ]
    )
    lifecycle_case = build_lifecycle_case(
        account="lx",
        broker="futu",
        contract_key="futu|lx|NVDA|put|short|100|2026-07-17",
        position_side="short",
        expiration_ymd="2026-07-17",
        market="US",
        target_contracts_by_lot={"lot-nvda": 1},
    )
    lifecycle_case.update(
        {
            "market": "US",
            "symbol": "NVDA",
            "option_type": "put",
            "strike": 100,
            "multiplier": 100,
        }
    )
    assert repo.upsert_trade_lifecycle_case(lifecycle_case)
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    assert repo.insert_trade_lifecycle_timing_policy_once(
        build_lifecycle_timing_policy(
            case_id=str(lifecycle_case["case_id"]),
            market="US",
            expiration_ymd="2026-07-17",
            contract_metadata={
                "settlement_style": "physical",
                "underlying_security_type": "equity",
                "last_trade_cutoff_ms": int(now.timestamp() * 1000),
                "last_trade_cutoff_source": "instrument_policy_registry",
            },
            trading_days=[
                {"date": "2026-07-17", "type": "TRADING"},
                {"date": "2026-07-20", "type": "TRADING"},
                {"date": "2026-07-21", "type": "TRADING"},
            ],
            calendar_source="test_calendar",
            calendar_observed_at_ms=int(now.timestamp() * 1000),
        )
    )
    config_path = tmp_path / "config.us.json"
    config_path.write_text("{}", encoding="utf-8")
    cfg = {
        "accounts": ["lx"],
        "account_settings": {
            "lx": {
                "type": "futu",
                "futu": {
                    "host": "127.0.0.1",
                    "port": 11111,
                    "account_id": "123456",
                    "trd_env": "REAL",
                },
            }
        },
    }
    monkeypatch.setattr(
        service_module,
        "load_runtime_config",
        lambda **_kwargs: (config_path, cfg),
    )
    monkeypatch.setattr(
        service_module,
        "infer_runtime_config_market",
        lambda **_kwargs: "US",
    )
    runtime = {
        "config": {"config_key": "us"},
        "summary": {"ok": True},
        "ledger_store": {"sqlite_path": str(ledger_path)},
        "trade_intake": {
            "holdings_sync": {"enabled": False},
            "sources": [],
        },
        "service_profile": {"loaded": True},
    }
    payload = OMQualityService(
        artifact_repository=QualityArtifactRepository(
            tmp_path / "status.v1.json"
        ),
        control_repository=QualityControlStateRepository(
            tmp_path / "control.v1.json"
        ),
        opend_adapter=_OpenD(),
        runtime_status_fn=lambda *_args: {"ok": True, "data": runtime},
        now_fn=lambda: now,
        instance_id="test-instance",
    ).refresh(config_keys=["us"], day_end_strict=True)

    position = next(
        item
        for item in payload["datasets"]
        if item["dataset_id"] == "om.option_positions"
    )
    assert position["status"] == "partial"
    assert position["checks"][1]["reason_code"] == (
        "POSITIONS_PENDING_LIFECYCLE"
    )
    assert position["blocked_consumers"] == []


def test_no_deep_refresh_carries_current_snapshot_and_due_probe_rechecks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "option_positions.sqlite3"
    SQLiteOptionPositionsRepository(ledger_path)
    config_path = tmp_path / "config.us.json"
    config_path.write_text("{}", encoding="utf-8")
    cfg = {
        "accounts": ["lx"],
        "account_settings": {
            "lx": {
                "type": "futu",
                "futu": {
                    "host": "127.0.0.1",
                    "port": 11111,
                    "account_id": "123456",
                    "trd_env": "REAL",
                },
            }
        },
    }
    monkeypatch.setattr(
        service_module,
        "load_runtime_config",
        lambda **_kwargs: (config_path, cfg),
    )
    monkeypatch.setattr(
        service_module,
        "infer_runtime_config_market",
        lambda **_kwargs: "US",
    )
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    runtime = {
        "config": {"config_key": "us"},
        "summary": {"ok": True},
        "ledger_store": {"sqlite_path": str(ledger_path)},
        "trade_intake": {"holdings_sync": {"enabled": False}, "sources": []},
        "service_profile": {"loaded": True},
    }
    artifact = QualityArtifactRepository(tmp_path / "status.v1.json")
    control = QualityControlStateRepository(tmp_path / "control.v1.json")
    opend = _OpenD()
    service = OMQualityService(
        artifact_repository=artifact,
        control_repository=control,
        opend_adapter=opend,
        runtime_status_fn=lambda *_args: {"ok": True, "data": runtime},
        now_fn=lambda: now,
        instance_id="test-instance",
        ledger_probe_path=ledger_path,
    )

    baseline = service.refresh(config_keys=["us"])
    legacy_position = next(
        item
        for item in baseline["datasets"]
        if item["dataset_id"] == "om.option_positions"
    )
    legacy_position["extensions"].pop(
        "next_authoritative_refresh_due_utc",
    )
    artifact.write_atomic(baseline)

    migrated = service.refresh(config_keys=["us"], deep=False)
    migrated_position = next(
        item
        for item in migrated["datasets"]
        if item["dataset_id"] == "om.option_positions"
    )
    assert opend.calls == [("lx", "us"), ("lx", "us")]
    assert migrated_position["extensions"][
        "next_authoritative_refresh_due_utc"
    ]

    carried = service.refresh(config_keys=["us"], deep=False)
    position = next(
        item
        for item in carried["datasets"]
        if item["dataset_id"] == "om.option_positions"
    )
    assert opend.calls == [("lx", "us"), ("lx", "us")]
    assert position["status"] == "trusted"
    assert position["extensions"]["carried_forward"] is True
    assert carried["extensions"]["deep_refresh"] is False
    assert control.read()["trading_days_by_market"]["us"] == [
        "2026-07-13",
        "2026-07-14",
    ]

    assert service.refresh_if_due(config_keys=["us"])["status"] == "not_due"
    SQLiteOptionPositionsRepository(ledger_path).replace_position_lots(
        [
            PositionLotRecord(
                record_id="rec-nvda",
                fields={
                    "account": "lx",
                    "symbol": "NVDA",
                    "option_type": "put",
                    "side": "short",
                    "contracts_open": 1,
                    "expiration": 1784246400000,
                    "expiration_ymd": "2026-07-17",
                    "strike": 100,
                    "multiplier": 100,
                },
            )
        ]
    )
    ledger_triggered = service.refresh_if_due(config_keys=["us"])
    assert ledger_triggered["schema_version"] == "investment.quality_status.v1"
    assert opend.calls == [("lx", "us"), ("lx", "us"), ("lx", "us")]

    state = control.read()
    state["position_mismatches"]["us:lx"] = {
        "fingerprint": "pending",
        "first_seen_at_utc": "2026-07-13T09:58:00Z",
        "last_seen_at_utc": "2026-07-13T09:58:00Z",
        "next_recheck_at_utc": "2026-07-13T09:59:00Z",
        "mismatch_count": 1,
    }
    control.write(state)

    refreshed = service.refresh_if_due(config_keys=["us"])
    assert refreshed["schema_version"] == "investment.quality_status.v1"
    assert refreshed["extensions"]["authoritative_refresh_scopes"] == [
        {"account": "lx", "market": "us"}
    ]
    assert opend.calls == [
        ("lx", "us"),
        ("lx", "us"),
        ("lx", "us"),
        ("lx", "us"),
    ]


def test_single_market_day_end_refresh_preserves_other_market(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "option_positions.sqlite3"
    SQLiteOptionPositionsRepository(ledger_path)
    configs = {}
    cfg = {
        "accounts": ["lx"],
        "account_settings": {
            "lx": {
                "type": "futu",
                "futu": {
                    "host": "127.0.0.1",
                    "port": 11111,
                    "account_id": "123456",
                    "trd_env": "REAL",
                },
            }
        },
    }
    for key in ("us", "hk"):
        path = tmp_path / f"config.{key}.json"
        path.write_text("{}", encoding="utf-8")
        configs[key] = path
    monkeypatch.setattr(
        service_module,
        "load_runtime_config",
        lambda *, config_key: (configs[config_key], cfg),
    )
    monkeypatch.setattr(
        service_module,
        "infer_runtime_config_market",
        lambda *, config_path, **_kwargs: config_path.stem.split(".")[-1],
    )
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)

    def runtime_status(_tool, payload):
        return {
            "ok": True,
            "data": {
                "config": {"config_key": payload["config_key"]},
                "summary": {"ok": True},
                "ledger_store": {"sqlite_path": str(ledger_path)},
                "trade_intake": {
                    "holdings_sync": {"enabled": False},
                    "sources": [],
                },
                "service_profile": {"loaded": True},
            },
        }

    opend = _OpenD()
    service = OMQualityService(
        artifact_repository=QualityArtifactRepository(tmp_path / "status.v1.json"),
        control_repository=QualityControlStateRepository(tmp_path / "control.v1.json"),
        opend_adapter=opend,
        runtime_status_fn=runtime_status,
        now_fn=lambda: now,
        instance_id="test-instance",
        ledger_probe_path=ledger_path,
    )
    service.refresh(config_keys=["us", "hk"])
    us_only = service.refresh(
        config_keys=["us"],
        deep=True,
        day_end_strict=True,
    )

    position_markets = {
        item["scope"]["market"]
        for item in us_only["datasets"]
        if item["dataset_id"] == "om.option_positions"
    }
    runtime_markets = {
        item["scope"]["market"]
        for item in us_only["runtime"]["checks"]
        if item["check_id"] == "RT-OM-004"
    }
    assert position_markets == {"us", "hk"}
    assert runtime_markets == {"us", "hk"}
