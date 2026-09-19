from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import src.application.quality.service as service_module
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.option_lifecycle import build_lifecycle_case
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.position_records import PositionLotRecord
from src.application.quality.service import OMQualityService
from src.application.trades.close_reason_evidence import (
    build_lifecycle_timing_policy,
)
from src.infrastructure.quality.artifact_repository import QualityArtifactRepository
from src.infrastructure.quality.control_state_repository import QualityControlStateRepository
from src.application.quality.opend_position_adapter import OpenDOptionSnapshot


class _OpenD:
    def __init__(
        self,
        *,
        complete: bool = True,
        environment: str = "REAL",
        error_code: str | None = None,
        snapshot_input_factory=None,
        account_fingerprint: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.complete = complete
        self.environment = environment
        self.error_code = error_code
        self.snapshot_input_factory = snapshot_input_factory
        self.account_fingerprint = account_fingerprint

    def fetch(self, *, account: str, market: str, **_kwargs) -> OpenDOptionSnapshot:
        self.calls.append((account, market))
        return OpenDOptionSnapshot(
            account=account,
            market=market,
            environment=self.environment,
            account_fingerprint=self.account_fingerprint or "sha256:" + ("b" * 64),
            observed_at_utc="2026-07-13T10:00:00Z",
            snapshot_id=f"snapshot-{account}",
            complete=self.complete,
            refresh_cache=True,
            rows=[],
            trading_days=[date(2026, 7, 13), date(2026, 7, 14)],
            error_code=self.error_code,
            snapshot_input=(
                self.snapshot_input_factory(account, market)
                if self.snapshot_input_factory is not None
                else {}
            ),
        )


def _empty_current_quality() -> dict:
    return {
        "schema_version": "current_lifecycle_quality.v1",
        "account": "lx",
        "aggregate_by_market": [],
        "operational_cases": [],
        "aggregate_fingerprint": canonical_sha256([]),
        "detail_fingerprint": canonical_sha256([]),
        "operational_status_counts": {},
        "blocked_consumer_counts": {},
    }


def _account_config(*accounts: str) -> dict:
    """The futu account settings the service resolves for each configured account."""
    return {
        "accounts": list(accounts),
        "account_settings": {
            account: {
                "type": "futu",
                "futu": {
                    "host": "127.0.0.1",
                    "port": 11111,
                    "account_id": "123456",
                    "trd_env": "REAL",
                },
            }
            for account in accounts
        },
    }


def _stub_single_market_config(monkeypatch, config_path: Path, cfg: dict) -> None:
    """Stub the single-market config seams; `path.write_text('{}')` stays with the caller."""
    monkeypatch.setattr(service_module, "load_runtime_config", lambda **_kwargs: (config_path, cfg))
    monkeypatch.setattr(service_module, "infer_runtime_config_market", lambda **_kwargs: "US")


def _runtime(
    ledger_path: Path,
    *,
    config_key: str = "us",
    trade_intake: dict | None = None,
) -> dict:
    """The `runtime_status` data payload the service reads for one market."""
    return {
        "config": {"config_key": config_key},
        "summary": {"ok": True},
        "ledger_store": {"sqlite_path": str(ledger_path)},
        "trade_intake": trade_intake or {"sources": []},
        "service_profile": {"loaded": True},
    }


def _runtime_status_for(runtime: dict):
    """A `runtime_status_fn` that always returns `runtime` as the data payload."""

    def _runtime_status(*_args) -> dict:
        return {"ok": True, "data": runtime}

    return _runtime_status


def _runtime_status_by_config_key(ledger_path: Path):
    """A `runtime_status_fn` that echoes the requested config_key back into the runtime payload."""

    def _runtime_status(_tool, payload) -> dict:
        return {"ok": True, "data": _runtime(ledger_path, config_key=payload["config_key"])}

    return _runtime_status


def _service(
    *,
    artifact: QualityArtifactRepository,
    control: QualityControlStateRepository,
    runtime_status_fn,
    opend: _OpenD | None = None,
    now=None,
    instance_id: str = "test-instance",
    **kwargs,
) -> OMQualityService:  # type: ignore[no-untyped-def]
    """Build the service with the shared adapter/clock defaults; `**kwargs` reach the constructor."""
    return OMQualityService(
        artifact_repository=artifact,
        control_repository=control,
        opend_adapter=opend or _OpenD(),
        runtime_status_fn=runtime_status_fn,
        now_fn=now or (lambda: datetime(2026, 7, 13, 10, tzinfo=timezone.utc)),
        instance_id=instance_id,
        **kwargs,
    )


def _trusted_empty_current_projection() -> dict:
    return {
        "status": "trusted",
        "reason": None,
        "payload": {
            "position_binding": {},
            "lifecycle": {"operational_cases": []},
        },
        "position_lots": [],
        "lot_count": 0,
        "lifecycle_by_case": {},
        "lifecycle_quality": _empty_current_quality(),
    }


def _enriched_snapshot_input(account: str, market: str, *, complete: bool) -> dict:
    return {
        "schema_version": "position_snapshot.v1",
        "snapshot_id": f"internal-{account}-{market}",
        "source_id": "futu-opend.positions",
        "broker_account_ref": {
            "broker_account_id": "futu:REAL:123456",
            "broker_id": "futu",
            "external_account_id": "123456",
            "environment": "REAL",
            "account_label": account,
        },
        "scope": {
            "markets": [market.upper()],
            "asset_types": ["option"],
            "filtered": False,
        },
        "observed_at_utc": "2026-07-13T10:00:00Z",
        "source_as_of_utc": "2026-07-13T09:59:00Z",
        "completeness": "complete" if complete else "partial",
        "quality": {"status": "ready" if complete else "unknown"},
        "rows": [],
        "evidence_refs": [],
        "source_evidence": [],
        "errors": [],
        "internal_sentinel": "must-not-cross-public-boundary",
    }


@pytest.mark.parametrize("complete", [True, False], ids=["complete", "incomplete"])
def test_service_publishes_schema_valid_artifact_without_business_writes(
    monkeypatch,
    tmp_path: Path,
    complete: bool,
) -> None:
    ledger_path = tmp_path / "option_positions.sqlite3"
    SQLiteOptionPositionsRepository(ledger_path)
    config_path = tmp_path / "config.us.json"
    config_path.write_text("{}", encoding="utf-8")
    _stub_single_market_config(monkeypatch, config_path, _account_config("lx"))
    current_reads: list[str] = []

    def _current_projection(_repo, *, account: str, now_ms: int) -> dict:
        assert now_ms > 0
        current_reads.append(account)
        return {
            "status": "trusted",
            "lifecycle_quality": _empty_current_quality(),
        }

    monkeypatch.setattr(
        service_module,
        "read_current_decision_projection",
        _current_projection,
    )
    monkeypatch.setattr(
        service_module,
        "quality_consumer_telemetry_snapshot",
        lambda: {
            "coverage_status": "unexplained",
            "entries": [
                {
                    "consumer": "unexplained",
                    "legacy_rows_requested": True,
                }
            ],
        },
    )
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    runtime = _runtime(
        ledger_path,
        trade_intake={
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
    )
    artifact = QualityArtifactRepository(tmp_path / "status.v1.json")
    service = _service(
        artifact=artifact,
        control=QualityControlStateRepository(tmp_path / "control.v1.json"),
        opend=_OpenD(
            complete=complete,
            environment="REAL" if complete else "UNKNOWN",
            error_code=None if complete else "OPEND_TEST_INCOMPLETE",
            snapshot_input_factory=lambda account, market: _enriched_snapshot_input(
                account, market, complete=complete
            ),
            account_fingerprint=(
                "sha256:8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92"
            ),
        ),
        runtime_status_fn=_runtime_status_for(runtime),
        now=lambda: now,
    )
    payload = service.refresh(config_keys=["us"])
    assert artifact.read() == payload
    assert payload["producer"]["service"] == "options-monitor"
    position_snapshots = [
        snapshot
        for dataset in payload["datasets"]
        for snapshot in dataset.get("source_snapshots") or []
    ]
    assert position_snapshots
    assert all(
        set(snapshot)
        == {
            "provider",
            "snapshot_id",
            "observed_at_utc",
            "complete",
            "refresh_cache",
            "account_fingerprint",
            "environment",
            "market",
        }
        for snapshot in position_snapshots
    )
    assert all(
        "internal_sentinel" not in snapshot for snapshot in position_snapshots
    )
    position_dataset = next(
        dataset
        for dataset in payload["datasets"]
        if dataset["dataset_id"] == "om.option_positions"
    )
    assert position_dataset["status"] == ("trusted" if complete else "unavailable")
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
    } <= check_ids
    runtime_ids = {item["check_id"] for item in payload["runtime"]["checks"]}
    assert {"RT-OM-001", "RT-OM-002", "RT-OM-003", "RT-OM-004"} <= runtime_ids
    lifecycle_summary = next(
        item
        for item in payload["datasets"]
        if item["dataset_id"] == "om.lifecycle_evidence_summary"
    )
    assert current_reads == ["lx"]
    assert lifecycle_summary["status"] == "trusted"
    assert lifecycle_summary["extensions"]["comparison"]["status"] == "matched"
    assert payload["extensions"]["current_decision_migration"]["status"] == "not_ready"
    assert sum(payload["summary"]["dataset_counts"].values()) == len(
        [
            item
            for item in payload["datasets"]
            if item["dataset_id"] != "om.lifecycle_evidence_summary"
        ]
    )

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
    _stub_single_market_config(monkeypatch, config_path, _account_config("lx"))
    runtime = _runtime(ledger_path)
    payload = _service(
        artifact=QualityArtifactRepository(tmp_path / "status.v1.json"),
        control=QualityControlStateRepository(tmp_path / "control.v1.json"),
        runtime_status_fn=_runtime_status_for(runtime),
        now=lambda: now,
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
    _stub_single_market_config(monkeypatch, config_path, _account_config("lx"))
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    runtime = _runtime(ledger_path)
    artifact = QualityArtifactRepository(tmp_path / "status.v1.json")
    control = QualityControlStateRepository(tmp_path / "control.v1.json")
    opend = _OpenD()
    service = _service(
        artifact=artifact,
        control=control,
        opend=opend,
        runtime_status_fn=_runtime_status_for(runtime),
        now=lambda: now,
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


def test_authoritative_position_terms_refresh_every_fifteen_minutes_in_window() -> None:
    trading_day = date(2026, 7, 13)

    before_window = OMQualityService._next_authoritative_refresh_due(
        now=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        market="us",
        trading_days=[trading_day],
    )
    during_window = OMQualityService._next_authoritative_refresh_due(
        now=datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc),
        market="us",
        trading_days=[trading_day],
    )
    after_window = OMQualityService._next_authoritative_refresh_due(
        now=datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc),
        market="us",
        trading_days=[trading_day],
    )

    assert before_window == datetime(2026, 7, 13, 12, 30, tzinfo=timezone.utc)
    assert during_window == datetime(2026, 7, 13, 14, 15, tzinfo=timezone.utc)
    assert after_window == datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc)


def test_single_market_day_end_refresh_preserves_other_market(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "option_positions.sqlite3"
    SQLiteOptionPositionsRepository(ledger_path)
    configs = {}
    cfg = _account_config("lx")
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
    monkeypatch.setattr(
        service_module,
        "read_current_decision_projection",
        lambda *_args, **_kwargs: {
            "status": "trusted",
            "lifecycle_quality": _empty_current_quality(),
        },
    )
    monkeypatch.setattr(
        service_module,
        "quality_consumer_telemetry_snapshot",
        lambda: {
            "coverage_status": "observed",
            "entries": [
                {
                    "consumer": "close_advice",
                    "legacy_rows_requested": True,
                }
            ],
        },
    )
    now = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
    opend = _OpenD()
    service = _service(
        artifact=QualityArtifactRepository(tmp_path / "status.v1.json"),
        control=QualityControlStateRepository(tmp_path / "control.v1.json"),
        opend=opend,
        runtime_status_fn=_runtime_status_by_config_key(ledger_path),
        now=lambda: now,
        ledger_probe_path=ledger_path,
    )
    baseline = service.refresh(config_keys=["us", "hk"])
    assert baseline["extensions"]["current_decision_migration"]["status"] == (
        "shadow_ready"
    )
    service.artifact_repository.write_atomic(
        {
            **baseline,
            "datasets": [
                item
                for item in baseline["datasets"]
                if item["dataset_id"] != "om.lifecycle_evidence_summary"
            ],
        }
    )
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
    assert us_only["extensions"]["current_decision_migration"]["status"] == (
        "not_ready"
    )
    assert service.refresh(config_keys=["us", "hk"])["extensions"][
        "current_decision_migration"
    ]["status"] == "shadow_ready"


def test_active_cutover_refresh_uses_current_projection_without_history_reads(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "option_positions.sqlite3"
    ledger_path.write_bytes(b"")
    config_paths = {}
    for key in ("us", "hk"):
        config_path = tmp_path / f"config.{key}.json"
        config_path.write_text("{}", encoding="utf-8")
        config_paths[key] = config_path
    cfg = {"accounts": ["lx"]}
    monkeypatch.setattr(
        service_module,
        "load_runtime_config",
        lambda *, config_key: (config_paths[config_key], cfg),
    )
    monkeypatch.setattr(
        service_module,
        "infer_runtime_config_market",
        lambda *, config_path, **_kwargs: config_path.stem.split(".")[-1],
    )
    monkeypatch.setattr(
        service_module,
        "read_quality_hot_path_cutover_receipt",
        lambda _path: {"schema_version": "receipt.v1", "status": "active"},
    )
    fake_repo = object()
    monkeypatch.setattr(
        service_module,
        "open_trade_reconciliation_evidence_repo",
        lambda _path: fake_repo,
    )
    current_reads: list[str] = []

    def read_current(_repo, *, account: str, now_ms: int) -> dict:
        assert _repo is fake_repo
        assert now_ms > 0
        current_reads.append(account)
        return _trusted_empty_current_projection()

    monkeypatch.setattr(
        service_module,
        "read_current_decision_projection",
        read_current,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("ordinary current-only refresh read lifetime history")

    monkeypatch.setattr(service_module, "build_ledger_datasets", forbidden)
    monkeypatch.setattr(
        service_module,
        "lifecycle_account_coherent_facts",
        forbidden,
    )
    monkeypatch.setattr(service_module, "build_lifecycle_datasets", forbidden)

    service = _service(
        artifact=QualityArtifactRepository(tmp_path / "status.json"),
        control=QualityControlStateRepository(tmp_path / "control.json"),
        runtime_status_fn=_runtime_status_by_config_key(ledger_path),
        ledger_probe_path=tmp_path / "missing-probe.sqlite3",
        cutover_receipt_path=tmp_path / "cutover.json",
    )
    with pytest.raises(
        ValueError,
        match="first current-only quality refresh must publish both markets",
    ):
        service.refresh(config_keys=["us"])
    assert service.artifact_repository.read() is None

    payload = service.refresh(config_keys=["us", "hk"])

    assert current_reads == ["lx", "lx"]
    ids = [item["dataset_id"] for item in payload["datasets"]]
    assert "om.lifecycle_evidence_summary" in ids
    assert "om.lifecycle_evidence" not in ids
    assert "om.lifecycle_history" not in ids
    assert payload["extensions"]["current_decision_migration"]["status"] == (
        "cutover_active"
    )
    assert payload["extensions"]["quality_hot_path_cutover"]["status"] == (
        "active"
    )

    partial = service.refresh(config_keys=["us"])
    lifecycle_markets = {
        item["scope"]["market"]
        for item in partial["datasets"]
        if item["dataset_id"] == "om.lifecycle_evidence_summary"
    }
    assert lifecycle_markets == {"us", "hk"}
    assert not {
        item["dataset_id"]
        for item in partial["datasets"]
    } & {"om.lifecycle_evidence", "om.lifecycle_history"}


def test_integrity_refresh_keeps_full_replay_in_separate_artifact(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "option_positions.sqlite3"
    SQLiteOptionPositionsRepository(ledger_path)
    config_path = tmp_path / "config.us.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        service_module,
        "load_runtime_config",
        lambda **_kwargs: (config_path, {"accounts": ["lx"]}),
    )
    monkeypatch.setattr(
        service_module,
        "infer_runtime_config_market",
        lambda **_kwargs: "US",
    )
    monkeypatch.setattr(
        service_module,
        "read_quality_hot_path_cutover_receipt",
        lambda _path: {"schema_version": "receipt.v1", "status": "active"},
    )
    replay_calls: list[int] = []
    original = service_module.build_ledger_datasets

    def counted_replay(**kwargs):
        replay_calls.append(1)
        return original(**kwargs)

    monkeypatch.setattr(service_module, "build_ledger_datasets", counted_replay)

    main = QualityArtifactRepository(tmp_path / "status.json")
    integrity = QualityArtifactRepository(tmp_path / "integrity.json")
    service = _service(
        artifact=main,
        integrity_artifact_repository=integrity,
        control=QualityControlStateRepository(tmp_path / "control.json"),
        runtime_status_fn=_runtime_status_for(_runtime(ledger_path)),
        ledger_probe_path=ledger_path,
    )
    payload = service.refresh_integrity(config_keys=["us"])

    assert replay_calls == [1]
    assert payload["extensions"]["integrity_refresh"] is True
    assert payload["extensions"]["quality_hot_path_cutover"]["reason"] == (
        "integrity_refresh"
    )
    assert main.read() is None
    assert service.read_integrity_published() == payload


@pytest.mark.parametrize('source_offset_seconds', [0, 1, -301])
def test_refresh_validates_each_scope_after_collection_with_trusted_clock(
    tmp_path, monkeypatch, source_offset_seconds,
) -> None:
    from datetime import timedelta
    from dataclasses import replace
    from src.application.quality.model import utc_iso

    ledger_path = tmp_path / 'ledger.sqlite3'
    SQLiteOptionPositionsRepository(ledger_path)
    clock = [datetime(2026, 7, 13, 10, tzinfo=timezone.utc)]
    cfg = {'accounts': ['lx', 'sy'], 'account_settings': {
        a: {'type': 'futu', 'futu': {
            'host': '127.0.0.1', 'port': 11111, 'account_id': '123456', 'trd_env': 'REAL',
        }} for a in ['lx', 'sy']
    }}
    configs = []
    for market in ['us', 'hk']:
        path = tmp_path / f'config.{market}.json'
        path.write_text('{}')
        configs.append((market, path, cfg, market))
    monkeypatch.setattr(OMQualityService, '_load_configs', lambda *args: configs)
    observed = {}

    class DelayedOpenD(_OpenD):
        def fetch(self, *, account, market, **kwargs):
            clock[0] += timedelta(seconds=10)
            stamp = utc_iso(clock[0] + timedelta(seconds=source_offset_seconds))
            observed[(account, market)] = utc_iso(clock[0])
            standard = _enriched_snapshot_input(account, market, complete=True)
            standard.update(observed_at_utc=stamp, source_as_of_utc=None)
            return replace(
                super().fetch(account=account, market=market),
                observed_at_utc=stamp, snapshot_input=standard,
            )

    adapter = DelayedOpenD(account_fingerprint=(
        'sha256:8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92'
    ))
    service = OMQualityService(
        artifact_repository=QualityArtifactRepository(tmp_path / 'quality.json'),
        control_repository=QualityControlStateRepository(tmp_path / 'control.json'),
        opend_adapter=adapter, now_fn=lambda: clock[0], instance_id='test-delayed',
        runtime_status_fn=lambda *args: {'ok': True, 'data': {
            'ledger_store': {'sqlite_path': str(ledger_path)},
            'trade_intake': {'sources': []}, 'service_profile': {'loaded': True},
        }},
    )
    result = service.refresh(config_keys=['us', 'hk'])
    positions = [x for x in result['datasets'] if x['dataset_id'] == 'om.option_positions']
    assert len(positions) == 4
    assert len(adapter.calls) == 4
    assert result['observed_at_utc'] == utc_iso(clock[0])
    for position in positions:
        scope = position['scope']
        assert position['checks'][0]['observed_at_utc'] == observed[(scope['account'], scope['market'])]
        assert position['status'] == ('trusted' if source_offset_seconds == 0 else 'unavailable')
        if source_offset_seconds:
            assert position['usable_for'] == []
