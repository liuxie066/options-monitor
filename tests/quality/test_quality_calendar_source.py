"""Calendar source failures through the real quality publication and retry path."""
from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.quality import opend_position_adapter as adapter_module
from src.application.quality import service as service_module
from src.application.quality.gate import QualityGateBlocked, assert_quality_allows
from src.application.quality.opend_position_adapter import OpenDOptionPositionAdapter
from src.application.quality.service import OMQualityService
from src.infrastructure.futu_gateway import FutuGateway, _FutuAPIBackend, _FutuAPIClient
from src.infrastructure.quality.artifact_repository import QualityArtifactRepository
from src.infrastructure.quality.control_state_repository import QualityControlStateRepository
from src.interfaces.quality import cli as quality_cli


@pytest.fixture
def calendar_quality(monkeypatch, tmp_path):
    clock = [datetime(2026, 7, 13, 2, tzinfo=timezone.utc)]  # HK Monday, 10:00.
    accounts = {"alpha": "801001", "beta": "801002"}
    responses = {
        market: (0, [{"time": "2026-07-13", "trade_date_type": "WHOLE"}])
        for market in ("hk", "us")
    }
    calendar_calls, position_calls, contexts = [], [], []
    ledger = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    before = (ledger.list_trade_events(), ledger.list_position_lots())
    configs = {}
    for market, symbol in (("hk", "0700.HK"), ("us", "NVDA")):
        host = f"calendar-{market}.invalid"
        config = {
            "accounts": list(accounts),
            "account_settings": {
                account: {"type": "futu", "futu": {
                    "host": host, "port": 1, "account_id": physical, "trd_env": "REAL",
                }} for account, physical in accounts.items()
            },
            "symbols": [{"symbol": symbol, "fetch": {"source": "futu", "host": host, "port": 1}}],
        }
        path = tmp_path / f"config.{market}.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        configs[market] = (path, config)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    class SDKContext:
        def __init__(self, market):
            self.market = market
            self.closed = False
            contexts.append(self)

        def position_list_query(self, **kwargs):
            position_calls.append((self.market, kwargs))
            return 0, []

        def request_trading_days(self, **kwargs):
            assert kwargs["market"] == self.market.upper()
            assert kwargs["start"] <= "2026-07-13" <= kwargs["end"]
            calendar_calls.append((self.market, kwargs))
            response = responses[self.market]
            if isinstance(response, Exception):
                raise response
            return response

        def close(self):
            self.closed = True

    def gateway(**kwargs):
        host = kwargs["host"]
        market = "hk" if host == "calendar-hk.invalid" else "us"
        assert host == f"calendar-{market}.invalid"
        backend = _FutuAPIBackend(host=host, port=1)
        backend._quote_client = backend._trade_client = SDKContext(market)
        return FutuGateway(
            client=_FutuAPIClient(backend, is_option_chain_cache_enabled=False),
            backend=backend, host=host, port=1,
        )

    def load_config(**kwargs):
        return configs[kwargs["config_key"]]

    def runtime_status(_tool, payload):
        return {"ok": True, "data": {
            "config": {"config_key": payload["config_key"]},
            "summary": {"ok": True},
            "ledger_store": {"sqlite_path": str(ledger.db_path)},
            "trade_intake": {"enabled": False, "sources": []},
            "service_profile": {"loaded": True},
        }}

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "true")
    monkeypatch.setattr(service_module, "repo_base", lambda: tmp_path)
    monkeypatch.setattr(service_module, "load_runtime_config", load_config)
    monkeypatch.setattr(service_module, "infer_runtime_config_market", lambda **kw: kw["config_path"].stem[-2:].upper())
    monkeypatch.setattr(adapter_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(adapter_module, "build_ready_futu_broker_gateway", gateway)
    monkeypatch.setattr(adapter_module, "build_ready_futu_quote_gateway", gateway)
    artifact = QualityArtifactRepository(tmp_path / "quality.json")
    control = QualityControlStateRepository(tmp_path / "control.json")
    service = OMQualityService(
        artifact_repository=artifact, control_repository=control,
        opend_adapter=OpenDOptionPositionAdapter(), runtime_status_fn=runtime_status,
        instance_id="calendar-source-test", now_fn=lambda: clock[0], ledger_probe_path=ledger.db_path,
        integrity_artifact_repository=QualityArtifactRepository(tmp_path / "integrity.json"),
        cutover_receipt_path=tmp_path / "absent-cutover.json",
    )
    monkeypatch.setattr(quality_cli, "OMQualityService", lambda: service)

    def refresh(*keys):
        payload = quality_cli.handle_quality_command(Namespace(
            quality_command="refresh", config_keys=list(keys or ("hk",)), no_deep=True, day_end_strict=False,
        ))
        assert payload == artifact.read()
        return payload

    yield SimpleNamespace(
        clock=clock, responses=responses, refresh=refresh, service=service, artifact=artifact,
        control=control, calendar_calls=calendar_calls, position_calls=position_calls, accounts=accounts,
    )
    assert (ledger.list_trade_events(), ledger.list_position_lots()) == before
    assert contexts and all(context.closed for context in contexts)
    assert all(query["refresh_cache"] is True and query["trd_env"] == "REAL" for _, query in position_calls)
    assert {str(query["acc_id"]) for _, query in position_calls} == set(accounts.values())


def _positions(payload, market="hk"):
    return [row for row in payload["datasets"]
            if row["dataset_id"] == "om.option_positions" and row["scope"]["market"] == market]


@pytest.mark.parametrize("bad_response", [
    None, (0, None), (0, [None]), (0, [{}]),
    (0, [{"time": "2026-07-32"}]),
    (0, [{"time": "2026-07-13", "trade_date_type": "UNKNOWN"}]),
    (1, "offline calendar rejected"), TimeoutError("offline calendar timeout"),
])
def test_invalid_sdk_calendar_publishes_unknown_and_no_deep_retries(calendar_quality, bad_response):
    env = calendar_quality
    env.responses["hk"] = bad_response

    failed = env.refresh("hk", "us")

    assert {row["scope"]["account"] for row in _positions(failed)} == set(env.accounts)
    for row in _positions(failed):
        assert row["status"] == "unavailable"
        assert row["required_evidence_complete"] is False
        assert row["source_snapshots"][0]["complete"] is False
        assert not row.get("extensions", {}).get("next_authoritative_refresh_due_utc")
    assert all(row["status"] == "trusted" for row in _positions(failed, "us"))
    assert len({row["source_snapshots"][0]["account_fingerprint"] for row in _positions(failed)}) == 2
    checks = [row for row in failed["runtime"]["checks"] if row["check_id"] == "RT-OM-004"]
    assert len(checks) == 4
    assert all(row["status"] == ("unknown" if row["scope"]["market"] == "hk" else "pass") for row in checks)
    assert "hk" not in env.control.read()["trading_days_by_market"]

    retried = env.refresh("hk", "us")
    assert all(row["status"] == "unavailable" for row in _positions(retried))
    assert [market for market, _ in env.calendar_calls].count("hk") == 4
    assert [market for market, _ in env.calendar_calls].count("us") == 2

    env.responses["hk"] = (0, [{"time": "2026-07-13", "trade_date_type": "WHOLE"}])
    recovered = env.refresh()
    for row in _positions(recovered):
        assert row["status"] == "trusted"
        assert row["extensions"]["next_authoritative_refresh_due_utc"] == "2026-07-13T02:15:00Z"
    assert env.control.read()["trading_days_by_market"]["hk"] == ["2026-07-13"]
    calls = len(env.calendar_calls)
    env.clock[0] += timedelta(minutes=10)
    carried = env.refresh()
    assert len(env.calendar_calls) == calls
    assert all(row["extensions"]["carried_forward"] is True for row in _positions(carried))
    assert all(row["source_snapshots"][0]["observed_at_utc"] == "2026-07-13T02:00:00Z" for row in _positions(carried))
    env.clock[0] += timedelta(minutes=5)
    due = env.refresh()
    assert len(env.calendar_calls) == calls + 2
    assert all(row["extensions"]["next_authoritative_refresh_due_utc"] == "2026-07-13T02:30:00Z" for row in _positions(due))


@pytest.mark.parametrize("rows", [[], [{"time": "2026-07-13", "trade_date_type": "NON_TRADING"}]])
def test_complete_empty_calendar_keeps_fallback_but_not_stale_source_access(calendar_quality, rows):
    env = calendar_quality
    env.responses["hk"] = (0, rows)

    first = env.refresh()

    for row in _positions(first):
        assert row["status"] == "trusted"
        assert row["source_snapshots"][0]["complete"] is True
        assert row["extensions"]["next_authoritative_refresh_due_utc"] == "2026-07-14T00:30:00Z"
    assert_quality_allows("close_advice", account="alpha", market="hk", service=env.service, now=env.clock[0])
    calls = len(env.calendar_calls)
    env.clock[0] += timedelta(minutes=31)
    carried = env.refresh()
    assert len(env.calendar_calls) == calls
    assert all(row["extensions"]["carried_forward"] is True for row in _positions(carried))
    with pytest.raises(QualityGateBlocked) as exc:
        assert_quality_allows("close_advice", account="alpha", market="hk", service=env.service, now=env.clock[0])
    assert exc.value.reason_code == "QUALITY_DATASET_STALE"
    assert "OM-POS-001" in exc.value.blocked_by
