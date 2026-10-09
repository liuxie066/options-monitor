"""S1: market-scoped automatic attribution recovery for one intake source.

The coordinator reads canonical open executions, discovers supported markets, and
binds each market to its own generated runtime config. These tests use a real
ledger repo, real generated runtime configs (authoring YAML -> build -> identity +
freshness), and the real public entry points; only the provider-bound observation
seams the existing reconcile tests already stub are stubbed here.
"""
from __future__ import annotations

from pathlib import Path
from threading import Event

import pytest
import yaml

from src.application.agent_tool_config import load_runtime_config
from src.application.config_yaml import build_yaml_runtime_config_file
from domain.domain.trade_execution import execution_identity_from_input
from src.application.trades import attribution as attribution_mod
from src.application.trades.auto_intake import _trade_attribution_diagnosis
from test_trade_attribution_view import _call_scope

REPO_ROOT = Path(__file__).resolve().parents[1]

_HK_EXECUTION = {"external_id_namespace": "futu.deal", "external_execution_id": "hk-deal-1",
                 "broker_account_ref": {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}}


def _source_document(*, account_id="1001", trd_env="REAL", hk_accounts=("lx",), combo_mode=None):
    accounts = {"lx": {"type": "futu", "futu_account_id": account_id,
                       "futu": {"host": "127.0.0.1", "port": 11111, "trd_env": trd_env}}}
    if "sy" in hk_accounts:
        accounts["sy"] = {"type": "futu", "futu_account_id": "2002",
                          "futu": {"host": "127.0.0.1", "port": 22222, "trd_env": "REAL"}}
    document = {"accounts": accounts,
                "markets": {"us": {"accounts": ["lx"], "symbols": ["NVDA"]},
                            "hk": {"accounts": list(hk_accounts), "symbols": ["0700.HK"]}}}
    if combo_mode:
        document["trade_intake"] = {"combo_reconciliation": {"accounts": {"lx": combo_mode}}}
    return document


def _write_runtime_config(tmp_path, runtime_root, *, market, document, name=None):
    source = tmp_path / (name or f"config.{market}.source.yaml")
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    path = runtime_root / f"config.{market}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    build_yaml_runtime_config_file(repo_root=REPO_ROOT, market=market, config_path=source,
                                   output_config_path=path)
    return source, path


def _hk_open_event():
    from domain.domain.ledger import ContractKey, TradeEvent
    return TradeEvent(event_id="hk-open-1", event_type="open", event_time_ms=2000,
        contract_key=ContractKey.from_values(broker="futu", account="lx", underlying_symbol="0700.HK",
            option_type="put", strike=400, expiration_ymd="2026-12-18"),
        contracts=1, price=1, multiplier=100, currency="HKD", source="test", lot_id="hk-lot-1",
        raw_payload={"side": "sell", "execution_input": _HK_EXECUTION,
            "execution_id": execution_identity_from_input(_HK_EXECUTION), "multiplier_source": "payload"})


def _ledger_fixture(tmp_path, monkeypatch, *, actions=False, foreign_ledger=False):
    """Real writer ledger holding the US wheel fixture plus one open HK fill."""
    from domain.domain.ledger import TradeEvent
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.ledger.trade_attribution import ATTRIBUTION_POLICY_VERSION
    from src.application.ledger.writer import persist_trade_event_objects_atomically
    from src.application.wheel.config import resolve_wheel_activation_descriptor

    runtime_root = tmp_path / "runtime"
    fixture_tmp = tmp_path / "fixture"
    fixture_tmp.mkdir()
    rows, config, _branch = _call_scope(fixture_tmp, monkeypatch)
    config["accounts"] = ["lx"]
    canonical = runtime_root / "output_shared" / "state" / "option_positions.sqlite3"
    repo_path = tmp_path / "foreign.sqlite3" if foreign_ledger else canonical
    if foreign_ledger:
        # A real ledger exists at the sibling config's canonical path, but it is a
        # different file than the source's.
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"")
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    repo = SQLiteOptionPositionsRepository(repo_path)
    monkeypatch.setattr("src.application.ledger.repository_assigned_stock.now_ms", lambda: 500)
    descriptor = resolve_wheel_activation_descriptor(config, market="us", account="lx")
    with repo._writer_connection(begin_immediate=True) as conn:
        repo.open_wheel_activation_window(market="us", account="lx", expected_current_generation=0,
            policy_hash=descriptor["policy_hash"], request_id="window", request_hash="b" * 64, conn=conn)
        conn.execute("""INSERT INTO trade_attribution_policy_enablings
            (broker, physical_account_id, environment, account, market, policy_version,
             effective_from_ms, created_at_ms, actor, request_id, request_hash)
            VALUES ('futu', '1001', 'REAL', 'lx', 'us', ?, 2500, 2000, 'fixture', 'cutover-v2', ?)""",
            (ATTRIBUTION_POLICY_VERSION, "a" * 64))
    events = []
    for event in rows["trade_events"]:
        if event["event_id"] == "unlinked-call-open-1":
            event["event_time_ms"] = 3000 if actions else 2000
        execution = event["raw_payload"].get("execution_input")
        if execution:
            event["raw_payload"]["execution_id"] = execution_identity_from_input(execution)
        events.append(TradeEvent.from_dict(event))
    events.append(_hk_open_event())
    persist_trade_event_objects_atomically(repo, events)
    monkeypatch.setattr("time.time", lambda: 4)
    return repo, config, runtime_root


def _stub_provider_boundaries(monkeypatch):
    """Existing reconcile tests stub these; the market binding stays real."""
    monkeypatch.setattr(attribution_mod, "read_attribution_combo_evidence",
                        lambda *args, **kwargs: {"complete": True, "exposures": []})
    monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity",
                        lambda **kwargs: {})
    cached = []
    monkeypatch.setattr("src.application.trades.inbox.cache_trade_attribution_result",
                        lambda _path, *, execution_key, result: cached.append(execution_key) or 0)
    return cached


def _run_coordinator(repo, config, runtime_root, *, combo_mode, cursors, stop_event=None, accounts=("lx",)):
    return attribution_mod.reconcile_trade_attribution_source(
        repo, config=config, accounts=list(accounts), runtime_root=runtime_root,
        inbox_path=runtime_root / "trade_intake_inbox.sqlite3", combo_mode=combo_mode,
        cursors=cursors, stop_event=stop_event if stop_event is not None else Event())


def test_source_coordinator_binds_each_market_to_its_own_generated_config(tmp_path, monkeypatch):
    repo, _fixture_config, runtime_root = _ledger_fixture(tmp_path, monkeypatch)
    cached = _stub_provider_boundaries(monkeypatch)
    source_yaml, us_path = _write_runtime_config(
        tmp_path, runtime_root, market="us", document=_source_document(combo_mode="confirm"))
    _, hk_path = _write_runtime_config(
        tmp_path, runtime_root, market="hk", document=_source_document(combo_mode="confirm"),
        name=source_yaml.name)
    _config_path, us_config = load_runtime_config(config_path=us_path, expected_market="us")
    hk_execution_key = execution_identity_from_input(_HK_EXECUTION)
    calls = []
    original = attribution_mod.reconcile_trade_attribution_account
    def capture(repo_arg, **kwargs):
        before = len(cached)
        result = original(repo_arg, **kwargs)
        calls.append((kwargs["account"], kwargs["market"], kwargs["config"], kwargs["combo_mode"],
                      list(cached[before:])))
        return result
    monkeypatch.setattr(attribution_mod, "reconcile_trade_attribution_account", capture)

    cursors = {}
    recovery = _run_coordinator(repo, us_config, runtime_root, combo_mode="off", cursors=cursors)

    assert [(account, market) for account, market, *_ in calls] == [("lx", "us"), ("lx", "hk")]
    us_call = next(call for call in calls if call[1] == "us")
    hk_call = next(call for call in calls if call[1] == "hk")
    # The source market reuses the caller's boot config and caller mode; the sibling
    # market is bound to its own generated config, which owns that market's Combo mode.
    assert us_call[2] == us_config and us_call[3] == "off"
    assert hk_call[2]["_generated"]["market"] == "hk"
    assert hk_call[2]["config_source_path"] == str(hk_path)
    assert hk_call[3] == "confirm"
    assert hk_execution_key in hk_call[4] and hk_execution_key not in us_call[4]
    assert set(recovery) == {"lx"} and set(recovery["lx"]) == {"us", "hk"}
    assert recovery["lx"]["us"]["errors"] == [] and recovery["lx"]["hk"]["errors"] == []
    assert recovery["lx"]["hk"]["checked"] >= 1
    assert set(cursors) == {"lx:us", "lx:hk"}


@pytest.mark.parametrize("failure,fragment", [
    ("missing", "runtime config not found"),
    ("stale", "runtime config is stale"),
    ("wrong_market", "market does not match requested market"),
    ("account", "does not configure account lx"),
    ("physical", "physical account or environment differs from the source"),
    ("environment", "physical account or environment differs from the source"),
    ("ledger", "ledger differs from the source ledger"),
])
def test_source_coordinator_fences_unproven_sibling_and_keeps_us_working(
    tmp_path, monkeypatch, failure, fragment,
):
    repo, source_config, runtime_root = _ledger_fixture(
        tmp_path, monkeypatch, foreign_ledger=failure == "ledger")
    _stub_provider_boundaries(monkeypatch)
    document = _source_document(account_id="2002" if failure == "physical" else "1001",
        trd_env="SIMULATE" if failure == "environment" else "REAL",
        hk_accounts=("sy",) if failure == "account" else ("lx",))
    source_yaml, hk_path = _write_runtime_config(tmp_path, runtime_root, market="hk", document=document)
    if failure == "missing":
        hk_path.unlink()
    elif failure == "stale":
        document["markets"]["hk"]["symbols"].append("9988.HK")
        source_yaml.write_text(yaml.safe_dump(document), encoding="utf-8")
    elif failure == "wrong_market":
        _us_source, us_path = _write_runtime_config(tmp_path, runtime_root, market="us",
            document=_source_document(), name="other.source.yaml")
        hk_path.write_bytes(us_path.read_bytes())

    cursors = {"lx:hk": "held-hk"}
    recovery = _run_coordinator(repo, source_config, runtime_root, combo_mode="confirm", cursors=cursors)

    assert recovery["lx"]["us"]["errors"] == []
    assert fragment in recovery["lx"]["hk"]["error"]
    assert cursors["lx:hk"] == "held-hk"  # the fenced market keeps its cursor
    assert "lx:us" in cursors


def test_source_coordinator_isolates_source_market_failure_and_keeps_its_cursor(tmp_path, monkeypatch):
    repo, source_config, runtime_root = _ledger_fixture(tmp_path, monkeypatch)
    _stub_provider_boundaries(monkeypatch)
    _write_runtime_config(tmp_path, runtime_root, market="hk", document=_source_document())
    original = attribution_mod.reconcile_trade_attribution_account
    def failing(repo_arg, **kwargs):
        if kwargs["market"] == "us":
            raise RuntimeError("source market down")
        return original(repo_arg, **kwargs)
    monkeypatch.setattr(attribution_mod, "reconcile_trade_attribution_account", failing)

    cursors = {"lx:us": "held-us"}
    recovery = _run_coordinator(repo, source_config, runtime_root, combo_mode="confirm", cursors=cursors)

    assert recovery["lx"]["us"] == {"error": "RuntimeError: source market down"}
    assert recovery["lx"]["hk"]["errors"] == [] and recovery["lx"]["hk"]["checked"] >= 1
    assert cursors["lx:us"] == "held-us"
    assert cursors["lx:hk"] == ""


def test_source_coordinator_cancellation_stops_before_remaining_markets(tmp_path, monkeypatch):
    repo, source_config, runtime_root = _ledger_fixture(tmp_path, monkeypatch)
    _stub_provider_boundaries(monkeypatch)
    _write_runtime_config(tmp_path, runtime_root, market="hk", document=_source_document())
    stop = Event()
    attempted = []
    original = attribution_mod.reconcile_trade_attribution_account
    def stop_after_source(repo_arg, **kwargs):
        attempted.append(kwargs["market"])
        result = original(repo_arg, **kwargs)
        stop.set()
        return result
    monkeypatch.setattr(attribution_mod, "reconcile_trade_attribution_account", stop_after_source)

    cursors = {}
    recovery = _run_coordinator(repo, source_config, runtime_root, combo_mode="confirm",
        cursors=cursors, stop_event=stop)
    assert attempted == ["us"]
    assert set(recovery["lx"]) == {"us"}
    assert "lx:hk" not in cursors

    stop_before = Event()
    stop_before.set()
    assert _run_coordinator(repo, source_config, runtime_root, combo_mode="confirm",
        cursors={}, stop_event=stop_before) == {}
    assert attempted == ["us"]


def test_source_coordinator_retry_keeps_one_durable_adoption(tmp_path, monkeypatch):
    repo, source_config, runtime_root = _ledger_fixture(tmp_path, monkeypatch, actions=True)
    _stub_provider_boundaries(monkeypatch)
    _write_runtime_config(tmp_path, runtime_root, market="hk", document=_source_document())

    cursors = {}
    first = _run_coordinator(repo, source_config, runtime_root, combo_mode="confirm", cursors=cursors)
    assert first["lx"]["us"]["linked"] == 1 and first["lx"]["us"]["errors"] == []
    adopted = len(repo.list_trade_events())
    second = _run_coordinator(repo, source_config, runtime_root, combo_mode="confirm", cursors=cursors)
    assert second["lx"]["us"]["linked"] == 0
    assert len(repo.list_trade_events()) == adopted


def test_per_fill_diagnosis_binds_the_fill_market_config(tmp_path, monkeypatch):
    repo, source_config, runtime_root = _ledger_fixture(tmp_path, monkeypatch)
    _source_yaml, hk_path = _write_runtime_config(tmp_path, runtime_root, market="hk",
        document=_source_document())
    observed = []
    original = attribution_mod.build_trade_attribution_view
    def capture(rows, **kwargs):
        observed.append(kwargs["config"])
        return original(rows, **kwargs)
    monkeypatch.setattr(attribution_mod, "build_trade_attribution_view", capture)
    hk_execution_key = execution_identity_from_input(_HK_EXECUTION)

    result = _trade_attribution_diagnosis(repo=repo, config=source_config, account="lx",
        symbol="0700.HK", execution=_HK_EXECUTION, runtime_root=runtime_root)

    assert set(result) == {"attribution_result"}
    assert result["attribution_result"]["execution_key"] == hk_execution_key
    assert observed[-1]["_generated"]["market"] == "hk"
    assert observed[-1]["config_source_path"] == str(hk_path)
    # The source market keeps the caller config and never loads a sibling.
    us_execution = {"external_id_namespace": "futu.deal", "external_execution_id": "unlinked-call-open-1",
                    "broker_account_ref": {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}}
    us_result = _trade_attribution_diagnosis(repo=repo, config=source_config, account="lx",
        symbol="NVDA", execution=us_execution, runtime_root=runtime_root)
    assert set(us_result) == {"attribution_result"} and observed[-1] == source_config


@pytest.mark.parametrize("failure,error_type", [
    ("missing", "AgentToolError"),
    ("stale", "RuntimeConfigFreshnessError"),
    ("wrong_market", "AgentToolError"),
    ("physical", "ValueError"),
    ("ledger", "ValueError"),
])
def test_per_fill_diagnosis_reports_failed_sibling_without_fallback(
    tmp_path, monkeypatch, failure, error_type,
):
    repo, source_config, runtime_root = _ledger_fixture(
        tmp_path, monkeypatch, foreign_ledger=failure == "ledger")
    document = _source_document(account_id="2002" if failure == "physical" else "1001")
    source_yaml, hk_path = _write_runtime_config(tmp_path, runtime_root, market="hk", document=document)
    if failure == "missing":
        hk_path.unlink()
    elif failure == "stale":
        document["markets"]["hk"]["symbols"].append("9988.HK")
        source_yaml.write_text(yaml.safe_dump(document), encoding="utf-8")
    elif failure == "wrong_market":
        _us_source, us_path = _write_runtime_config(tmp_path, runtime_root, market="us",
            document=_source_document(), name="other.source.yaml")
        hk_path.write_bytes(us_path.read_bytes())

    result = _trade_attribution_diagnosis(repo=repo, config=source_config, account="lx",
        symbol="0700.HK", execution=_HK_EXECUTION, runtime_root=runtime_root)

    assert set(result) == {"attribution_error"} and result["attribution_error"] == error_type


def test_sibling_ledger_binding_uses_effective_canonical_owner(tmp_path, monkeypatch):
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    repo, config, runtime_root = _ledger_fixture(tmp_path, monkeypatch)
    _stub_provider_boundaries(monkeypatch)
    _write_runtime_config(tmp_path, runtime_root, market='hk', document=_source_document())
    other_root = tmp_path / 'other-effective-runtime'
    other_path = other_root / 'output_shared/state/option_positions.sqlite3'
    other_path.parent.mkdir(parents=True)
    SQLiteOptionPositionsRepository(other_path).list_trade_events()
    monkeypatch.setenv('OM_RUNTIME_ROOT', str(other_root))
    result = _run_coordinator(repo, config, runtime_root, combo_mode='confirm', cursors={})
    assert 'ledger differs' in result['lx']['hk']['error']
    assert result['lx']['us']['errors'] == []


def test_source_account_outside_config_cannot_start_reconciliation(tmp_path, monkeypatch):
    repo, config, runtime_root = _ledger_fixture(tmp_path, monkeypatch)
    attempted = []
    monkeypatch.setattr(attribution_mod, 'reconcile_trade_attribution_account', lambda *a, **k: attempted.append(k))
    result = _run_coordinator(repo, config, runtime_root, combo_mode='confirm', cursors={}, accounts=('sy',))
    assert 'does not configure account sy' in result['sy']['error']
    assert attempted == []
