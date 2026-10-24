from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3
from types import SimpleNamespace

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.api import project_trade_event_log
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.repository_schema import initialize_ledger_connection
from src.application.quality import service as service_module
from src.application.quality.opend_position_adapter import OpenDOptionSnapshot
from src.application.quality.service import OMQualityService
from src.infrastructure.quality.artifact_repository import QualityArtifactRepository
from src.infrastructure.quality.control_state_repository import QualityControlStateRepository


class _Artifact(QualityArtifactRepository):
    """Only storage is in memory; before/after hooks distinguish failure outcomes."""

    def __init__(self, path):
        super().__init__(path)
        self.payload = None
        self.before = lambda payload: None
        self.after = lambda payload: None

    def read(self):
        return deepcopy(self.payload)

    def write_atomic(self, payload):
        self.before(payload)
        self.payload = deepcopy(payload)
        self.after(payload)
        return self.path


def _connection(path):
    if not path.exists():
        path.touch(mode=0o600)
    conn = initialize_ledger_connection(sqlite3.connect(path))
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.execute("PRAGMA user_version=690")
    conn.commit()
    return conn


@pytest.fixture
def context(tmp_path, monkeypatch):
    db = tmp_path / "ledger.sqlite3"
    conn = _connection(db)
    repo = SQLiteOptionPositionsRepository(db)
    ctx = SimpleNamespace(clock=datetime(2026, 7, 13, 14, tzinfo=timezone.utc),
                          count=0, mode="match", complete=True, calls=[])

    def add_open(account="atlas", symbol="NVDA", currency="USD"):
        ctx.count += account == "atlas"
        suffix = f"{account}-{ctx.count}"
        event = TradeEvent(
            event_id="open-" + suffix, event_type="open", event_time_ms=1783900800000 + ctx.count,
            contract_key=ContractKey.from_values(broker="futu", account=account,
                underlying_symbol=symbol, option_type="put", strike=100, expiration_ymd="2026-07-17"),
            contracts=1, price=Decimal("2"), currency=currency, source="synthetic-690",
            multiplier=100, lot_id="lot-" + suffix, raw_payload={"side": "sell"},
        )
        repo.upsert_trade_event(event)
        projected = project_trade_event_log([event.to_dict()])
        assert not projected.has_errors and len(projected.lots) == 1
        conn.execute("INSERT INTO position_lots(lot_id, account, fields_json, source_event_id, strike, multiplier, updated_at_ms) VALUES(?,?,?,?,?,?,?)",
            (event.lot_id, account, json.dumps(projected.lots[0].fields), event.event_id, 100, 100, event.event_time_ms))
        conn.commit()

    add_open()
    add_open("jade", "0700.HK", "HKD")
    configs = {}
    for market, account in (("us", "atlas"), ("hk", "jade")):
        path = tmp_path / f"config.{market}.json"
        path.write_text("{}", encoding="utf-8")
        configs[market] = (path, {"accounts": [account], "market": market,
            "account_settings": {account: {"type": "futu", "futu": {
                "host": "fixture.invalid", "port": 1, "account_id": "synthetic", "trd_env": "REAL"}}}})
    monkeypatch.setattr(service_module, "load_runtime_config", lambda *, config_key: configs[config_key])
    monkeypatch.setattr(service_module, "infer_runtime_config_market", lambda *, config, **kwargs: config["market"])

    class OpenD:
        def fetch(self, *, account, market, **kwargs):
            ctx.calls.append((account, market))
            us = market == "us"
            row = {"code": "US.NVDA260717P100000" if us else "HK.00700260717P100000",
                   "qty": ctx.count if us else 1, "position_side": "SHORT",
                   "options_per_contract": 100, "sec_type": "DRVT"}
            if ctx.mode == "terms" and us:
                row.update(stock_owner="US.NVDA", option_type="PUT", strike_time="2026-07-17",
                           option_strike_price=99.5, options_per_contract=101)
            if ctx.mode == "quantity" and us:
                row["qty"] += 1
            return OpenDOptionSnapshot(account=account, market=market, environment="REAL",
                account_fingerprint="sha256:" + "a" * 64, observed_at_utc=ctx.clock.isoformat(),
                snapshot_id="synthetic-690", complete=ctx.complete, refresh_cache=True, rows=[row],
                trading_days=[date(2026, 7, 13), date(2026, 7, 14)])

    def runtime(_tool, payload):
        return {"ok": True, "data": {"config": {"config_key": payload["config_key"]},
            "summary": {"ok": True}, "ledger_store": {"sqlite_path": str(db)},
            "trade_intake": {"enabled": False, "sources": []}}}

    normal, integrity, control_sink = [_Artifact(tmp_path / f"{role}.json")
                                      for role in ("normal", "integrity", "control")]
    control = QualityControlStateRepository(tmp_path / "control.json")
    control._artifact = control_sink
    state = control.read()
    state["lifecycle_first_deep_reconcile"] = {"retained-case": "2026-07-12T00:00:00Z"}
    control.write(state)
    service = OMQualityService(artifact_repository=normal, integrity_artifact_repository=integrity,
        control_repository=control, opend_adapter=OpenD(), runtime_status_fn=runtime,
        now_fn=lambda: ctx.clock, instance_id="synthetic-690", ledger_probe_path=db,
        cutover_receipt_path=tmp_path / "absent-cutover.json")
    ctx.service, ctx.normal, ctx.integrity = service, normal, integrity
    ctx.control, ctx.control_sink, ctx.add_open, ctx.conn = control, control_sink, add_open, conn
    yield ctx
    conn.close()


def _position(payload, market="us"):
    return next(d for d in payload["datasets"]
                if d["dataset_id"] == "om.option_positions" and d["scope"]["market"] == market)


def _baseline(ctx):
    payload = ctx.service.refresh(config_keys=["us", "hk"])
    assert _position(payload)["status"] == "trusted"
    return payload, ctx.control.read()["last_probe_ledger_revision"]


def _change(ctx, mode="terms"):
    ctx.add_open()
    ctx.mode = mode
    ctx.clock += timedelta(seconds=10)
    return ctx.service._ledger_revision()


def _fail(_payload):
    raise OSError("synthetic storage failure")


def test_integrity_cannot_acknowledge_ordinary_publication(context):
    ctx = context
    original, r0 = _baseline(ctx)
    r1 = _change(ctx)
    integrity = ctx.service.refresh_integrity(config_keys=["us"])
    assert _position(integrity)["reason_codes"] == ["POSITION_CONTRACT_TERMS_DRIFT"]
    assert ctx.normal.read() == original
    assert ctx.control.read()["last_probe_ledger_revision"] == r0 != r1
    assert ctx.control.read()["position_mismatches"]["us:atlas"]["next_recheck_at_utc"] is None
    refreshed = ctx.service.refresh_if_due(config_keys=["us"])
    assert _position(refreshed)["status"] == "untrusted"
    assert _position(refreshed, "hk") == _position(original, "hk")
    assert ctx.control.read()["last_probe_ledger_revision"] == r1
    assert ctx.service.refresh_if_due(config_keys=["us"])["status"] == "not_due"


@pytest.mark.parametrize("fault", ["schema", "before_publish", "after_publish"])
def test_failed_normal_publication_does_not_ack(context, fault):
    ctx = context
    original, r0 = _baseline(ctx)
    _change(ctx)
    if fault == "schema":
        ctx.service.instance_id = {"invalid": "type"}
    elif fault == "before_publish":
        ctx.normal.before = _fail
    else:
        ctx.normal.after = _fail
    with pytest.raises(ValueError if fault == "schema" else OSError):
        ctx.service.refresh(config_keys=["us"])
    assert ctx.control.read()["last_probe_ledger_revision"] == r0
    assert (ctx.normal.read() == original) is (fault != "after_publish")
    ctx.service.instance_id = "synthetic-690"
    ctx.normal.before = ctx.normal.after = lambda payload: None
    assert ctx.service.refresh_if_due(config_keys=["us"])["schema_version"] == "investment.quality_status.v1"
    assert ctx.service.refresh_if_due(config_keys=["us"])["status"] == "not_due"


@pytest.mark.parametrize("fault", ["timing_before", "ack_before", "ack_after"])
def test_control_failure_uses_readback_not_assumed_rollback(context, fault):
    ctx = context
    original, r0 = _baseline(ctx)
    r1 = _change(ctx)

    def injected(payload):
        if fault == "timing_before" or payload["last_probe_ledger_revision"] == r1:
            _fail(payload)

    if fault == "ack_after":
        ctx.control_sink.after = injected
    else:
        ctx.control_sink.before = injected
    with pytest.raises(OSError):
        ctx.service.refresh(config_keys=["us"])
    # The after-write seam simulates persistence followed by an error, not rollback.
    persisted = ctx.control.read()["last_probe_ledger_revision"]
    assert persisted == (r1 if fault == "ack_after" else r0)
    assert (ctx.normal.read() == original) is (fault == "timing_before")
    ctx.control_sink.before = ctx.control_sink.after = lambda payload: None
    retry = ctx.service.refresh_if_due(config_keys=["us"])
    if fault == "ack_after":
        assert retry["status"] == "not_due"
    else:
        assert retry["schema_version"] == "investment.quality_status.v1"
    assert ctx.service.refresh_if_due(config_keys=["us"])["status"] == "not_due"


@pytest.mark.parametrize("fault", ["before", "after"])
def test_integrity_publication_failure_keeps_normal_cursor(context, fault):
    ctx = context
    original, r0 = _baseline(ctx)
    _change(ctx)
    setattr(ctx.integrity, fault, _fail)
    with pytest.raises(OSError):
        ctx.service.refresh_integrity(config_keys=["us"])
    assert ctx.control.read()["last_probe_ledger_revision"] == r0
    assert ctx.control.read()["position_mismatches"]["us:atlas"]["kind"] == "contract_terms_drift"
    assert ctx.normal.read() == original
    assert ctx.service.refresh_if_due(config_keys=["us"])["schema_version"] == "investment.quality_status.v1"


@pytest.mark.parametrize("phase", ["collection", "publication"])
def test_revision_changes_after_start_remain_due(context, phase):
    ctx = context
    _baseline(ctx)
    start = _change(ctx, "match")
    armed = [True]

    def change_once(_payload=None):
        if armed[0]:
            armed[0] = False
            ctx.add_open()

    if phase == "collection":
        original = ctx.service.runtime_status_fn

        def runtime(*args):
            change_once()
            return original(*args)

        ctx.service.runtime_status_fn = runtime
    else:
        ctx.normal.before = change_once
    ctx.service.refresh(config_keys=["us"])
    assert ctx.control.read()["last_probe_ledger_revision"] == start != ctx.service._ledger_revision()
    assert ctx.service.refresh_if_due(config_keys=["us"])["schema_version"] == "investment.quality_status.v1"
    assert ctx.service.refresh_if_due(config_keys=["us"])["status"] == "not_due"


def test_transient_deadlines_and_lifecycle_calendar_survive_ack(context):
    ctx = context
    _baseline(ctx)
    saved = ctx.control.read()
    _change(ctx, "quantity")
    ctx.service.refresh_integrity(config_keys=["us"])
    first = ctx.control.read()["position_mismatches"]["us:atlas"]
    assert first["next_recheck_at_utc"] == "2026-07-13T14:01:10Z"
    assert ctx.service.refresh_if_due(config_keys=["us"])["schema_version"] == "investment.quality_status.v1"
    assert ctx.service.refresh_if_due(config_keys=["us"])["status"] == "not_due"
    ctx.clock += timedelta(seconds=61)
    assert ctx.service.refresh_if_due(config_keys=["us"])["schema_version"] == "investment.quality_status.v1"
    state = ctx.control.read()
    assert state["position_mismatches"]["us:atlas"]["first_seen_at_utc"] == first["first_seen_at_utc"]
    assert state["position_mismatches"]["us:atlas"]["next_recheck_at_utc"] == "2026-07-13T14:05:10Z"
    assert state["lifecycle_first_deep_reconcile"] == saved["lifecycle_first_deep_reconcile"]
    assert state["trading_days_by_market"] == saved["trading_days_by_market"]


def test_metadata_only_revision_retains_authority_carry_policy(context):
    ctx = context
    original, r0 = _baseline(ctx)
    ctx.conn.execute("UPDATE position_lots SET updated_at_ms=updated_at_ms+1 WHERE account='atlas'")
    ctx.conn.commit()
    ctx.mode = "terms"
    ctx.service.refresh_integrity(config_keys=["us"])
    assert ctx.control.read()["last_probe_ledger_revision"] == r0
    calls = list(ctx.calls)
    refreshed = ctx.service.refresh_if_due(config_keys=["us"])
    assert ctx.calls == calls
    assert _position(refreshed)["status"] == _position(original)["status"] == "trusted"
    assert _position(refreshed)["extensions"]["carried_forward"] is True
    assert ctx.control.read()["last_probe_ledger_revision"] == ctx.service._ledger_revision()


def test_successful_unavailable_publication_acknowledges_progress(context):
    ctx = context
    ctx.complete = False
    payload = ctx.service.refresh(config_keys=["us"])
    assert _position(payload)["status"] == "unavailable"
    assert ctx.control.read()["last_probe_ledger_revision"] == ctx.service._ledger_revision()
    assert ctx.service.refresh_if_due(config_keys=["us"])["status"] == "not_due"


def test_none_revision_preserves_existing_probe_behavior(context, tmp_path):
    ctx = context
    path = tmp_path / "unavailable.sqlite3"
    conn = _connection(path)
    try:
        ctx.service.ledger_probe_path = path
        assert ctx.service._ledger_revision() is None
        ctx.service.refresh(config_keys=["us"])
        assert ctx.control.read()["last_probe_ledger_revision"] is None
        assert ctx.service.refresh_if_due(config_keys=["us"])["status"] == "not_due"
    finally:
        conn.close()
