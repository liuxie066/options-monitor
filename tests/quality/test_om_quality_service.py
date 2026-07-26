from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

import src.application.quality.service as service_module
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.quality.service import OMQualityService
from src.infrastructure.quality.artifact_repository import QualityArtifactRepository
from src.infrastructure.quality.control_state_repository import QualityControlStateRepository
from src.infrastructure.quality.opend_position_adapter import OpenDOptionSnapshot


class _OpenD:
    def fetch(self, *, account: str, market: str, **_kwargs) -> OpenDOptionSnapshot:
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
