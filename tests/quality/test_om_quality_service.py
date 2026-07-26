from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

import src.application.quality.service as service_module
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.position_records import PositionLotRecord
from src.application.quality.service import OMQualityService
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
