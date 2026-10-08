import json
import shutil

import pytest

from test_daily_decision_brief_repository_v2 import _brief
from src.application.decision_history_import import preview_history_import, apply_history_import
from src.application.tick_run_workspace import publish_account_run_config
from src.infrastructure.decision_history_sqlite import DecisionHistoryStore, history_path, DecisionHistoryError
from domain.domain.daily_decision_brief import normalize_daily_decision_brief


def legacy(base, monkeypatch):
    import test_candidate_snapshot_manifest as fixture
    cfg = {"accounts": ["lx"], "market": "us", "portfolio": {"account": "lx", "source": "futu"},
           "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}}}
    authority = publish_account_run_config(base=base, run_id="run-1", account="lx", config=cfg)
    monkeypatch.setattr(fixture, "CONFIG_HASH", authority.account_config_sha256)
    fixture._seal_combo_bundle(base)
    raw = normalize_daily_decision_brief({**_brief(run_id="run-1"), "revision": 0})
    # Optional decision fields can be absent: never reconstruct with today's policy.
    raw.pop("strategy_summary")
    path = base / "output_accounts/lx/state/daily_decision_brief.US.2026-07-21.r0000.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw))
    (base / "output_runs/run-1/accounts/lx/state/daily_decision_brief.US.json").write_text(json.dumps(raw))
    return path, raw


def preview(base):
    return preview_history_import(base=base, account="lx", market="US")


def apply(base, report):
    return apply_history_import(base=base, account="lx", market="US", preview_hash=report["preview_hash"])


def test_verified_import_idempotent_survives_source_removal(tmp_path, monkeypatch):
    source, raw = legacy(tmp_path, monkeypatch)
    report = preview(tmp_path)
    assert report["ready_count"] == 1, report
    assert not history_path(tmp_path).exists()
    assert apply(tmp_path, report)["readback"] == "passed"
    assert apply(tmp_path, report)["already_applied"] is True
    again = preview(tmp_path)
    assert again["rows"][0]["status"] == "already_imported"
    assert apply(tmp_path, again)["applied_count"] == 0
    source.unlink()
    shutil.rmtree(tmp_path / "output_runs")
    assert DecisionHistoryStore(history_path(tmp_path)).get(account="lx", market="US", run_id="run-1")["payload"] == raw


def test_changed_preview_and_unproven_sources_do_not_write(tmp_path, monkeypatch):
    source, raw = legacy(tmp_path, monkeypatch)
    report = preview(tmp_path)
    source.write_text(json.dumps({**raw, "run_id": "wrong"}))
    with pytest.raises(DecisionHistoryError, match="preview_changed"):
        apply(tmp_path, report)
    assert not history_path(tmp_path).exists()
    rejected = preview(tmp_path)
    assert rejected["ready_count"] == 0 and rejected["rows"][0]["status"] == "rejected"
    assert apply(tmp_path, rejected)["applied_count"] == 0
    store = DecisionHistoryStore(history_path(tmp_path))
    assert store.get(account="lx", market="US") is None
    from src.application.daily_decision_brief_repository import persist_daily_decision_brief_success
    saved = persist_daily_decision_brief_success(base=tmp_path, brief=_brief(run_id="fresh"))
    assert saved["current_revision"] == 1  # rejected r0000 must never be reused
    assert source.is_file()


def test_human_cli_preview_and_apply_contract(tmp_path, monkeypatch, capsys):
    from src.interfaces.cli.main import parse_args
    from src.interfaces.cli.decision_history_ops import handle_decision_history_command
    from src.application.agent_tool_contracts import AgentToolError
    import src.interfaces.cli.decision_history_ops as ops
    from types import SimpleNamespace
    legacy(tmp_path, monkeypatch)
    monkeypatch.setattr(ops, "resolve_runtime_root", lambda **_: SimpleNamespace(runtime_root=tmp_path))
    args = parse_args(["decision-history", "import", "--account", "lx", "--market", "US"])
    assert handle_decision_history_command(args, repo_base_fn=lambda: tmp_path) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ready_count"] == 1 and not history_path(tmp_path).exists()
    args.apply = True
    with pytest.raises(AgentToolError):
        handle_decision_history_command(args, repo_base_fn=lambda: tmp_path)
    args.preview_hash = report["preview_hash"]
    assert handle_decision_history_command(args, repo_base_fn=lambda: tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["applied_count"] == 1


def test_rejected_source_reserves_version_and_coverage_for_future_runs(tmp_path):
    path = tmp_path / "output_accounts/lx/state/daily_decision_brief.US.2026-07-21.r0007.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    report = preview(tmp_path)
    assert report["reserved_revisions"] == {"2026-07-21": 7}
    assert apply(tmp_path, report)["readback"] == "passed"
    assert apply(tmp_path, preview(tmp_path))["readback"] == "no_changes"
    from test_decision_history_query import save, read
    assert save(tmp_path, "new")["current_revision"] == 8
    result = read(tmp_path)
    assert result["status"] == "partial"
    assert result["missing"][0]["reason"] == "historical_sources_rejected"
    extra = path.with_name("daily_decision_brief.US.2026-07-21.r0012.json")
    extra.write_text("{}")
    assert apply(tmp_path, preview(tmp_path))["readback"] == "passed"
    assert save(tmp_path, "next")["current_revision"] == 13


def test_backfill_does_not_rewind_latest_success(tmp_path, monkeypatch):
    from src.application.daily_decision_brief_repository import persist_daily_decision_brief_success, read_latest_daily_decision_brief
    current = persist_daily_decision_brief_success(base=tmp_path,
        brief={**_brief(run_id="current"), "market_trading_date": "2026-07-22"})
    legacy(tmp_path, monkeypatch)
    report = preview(tmp_path)
    assert report["ready_count"] == 1, report
    apply(tmp_path, report)
    assert read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["brief"] == current["brief"]
    # A same-day new decision still reconciles against the latest successful revision.
    next_run = persist_daily_decision_brief_success(base=tmp_path,
        brief={**_brief(run_id="next"), "market_trading_date": "2026-07-22"})
    assert next_run["current_revision"] == 1
    assert read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["brief"] == next_run["brief"]


def historical(base, monkeypatch, *, version=1, futu=None):
    import test_candidate_evidence_history as fixture
    from src.application.tick_run_workspace import publish_account_run_config
    cfg = {"accounts": ["lx"], "market": "us", "portfolio": {"account": "lx",
           "source_by_account": {"lx": "futu"}},
           "account_settings": {"lx": {"futu": futu if futu is not None else
                                {"account_id": "1001", "trd_env": "REAL"}}}}
    authority = publish_account_run_config(base=base, run_id="run-1", account="lx", config=cfg)
    monkeypatch.setattr(fixture, "CONFIG_HASH", authority.account_config_sha256)
    fixture._write_empty_formal_history_bundle(base, manifest_version=version, include_opening=True)
    raw = normalize_daily_decision_brief({**_brief(run_id="run-1"), "revision": 0})
    path = base / "output_accounts/lx/state/daily_decision_brief.US.2026-07-21.r0000.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw))
    (base / "output_runs/run-1/accounts/lx/state/daily_decision_brief.US.json").write_text(json.dumps(raw))
    return path, raw


@pytest.mark.parametrize("version", [1, 3])
def test_strict_historical_import_preserves_original_with_retired_config(tmp_path, monkeypatch, version):
    source, raw = historical(tmp_path, monkeypatch, version=version)
    report = preview_history_import(base=tmp_path, account="lx", market="US",
                                    market_dates=["2026-07-21"])
    assert report["ready_count"] == 1, report
    assert report["rows"][0]["scope"] == {"futu_account_id": "1001", "trade_env": "REAL"}
    result = apply_history_import(base=tmp_path, account="lx", market="US",
                                 market_dates=["2026-07-21"], preview_hash=report["preview_hash"])
    assert result["applied_count"] == 1 and result["readback"] == "passed"
    assert json.loads(source.read_text()) == raw
    assert DecisionHistoryStore(history_path(tmp_path)).get(account="lx", market="US", run_id="run-1")["payload"] == raw
    shutil.rmtree(tmp_path / "output_runs")
    assert apply_history_import(base=tmp_path, account="lx", market="US",
        market_dates=["2026-07-21"], preview_hash=report["preview_hash"])["already_applied"]
    with pytest.raises(DecisionHistoryError, match="scope_changed"):
        apply_history_import(base=tmp_path, account="lx", market="US", preview_hash=report["preview_hash"])


@pytest.mark.parametrize("futu", [{}, {"account_id": "1001"},
                                   {"account_id": "1001", "trd_env": "UNKNOWN"},
                                   {"trd_env": "REAL"}, {"account_id": {}, "trd_env": "REAL"},
                                   {"account_id": True, "trd_env": "REAL"}])
def test_historical_identity_must_be_explicit(tmp_path, monkeypatch, futu):
    historical(tmp_path, monkeypatch, futu=futu)
    report = preview(tmp_path)
    assert report["ready_count"] == 0
    assert report["rows"][0]["reason"] == "historical_scope_unproven"


def test_historical_tamper_is_rejected_and_runtime_loader_stays_strict(tmp_path, monkeypatch):
    historical(tmp_path, monkeypatch)
    from src.application.candidate_snapshot_manifest import load_candidate_snapshot_bundle, CandidateSnapshotManifestError
    with pytest.raises(CandidateSnapshotManifestError, match="schema mismatch"):
        load_candidate_snapshot_bundle(base=tmp_path, run_id="run-1", account="lx")
    state = tmp_path / "output_runs/run-1/accounts/lx/state"
    owner = state / "opening_candidate_snapshot.json"
    owner.write_text("{}")
    report = preview(tmp_path)
    assert report["ready_count"] == 0 and report["rows"][0]["status"] == "rejected"
    assert not history_path(tmp_path).exists()


def test_bounded_dates_exclude_unrelated_sources_and_bind_apply(tmp_path, monkeypatch, capsys):
    from src.interfaces.cli.main import parse_args
    from src.interfaces.cli.decision_history_ops import handle_decision_history_command
    import src.interfaces.cli.decision_history_ops as ops
    from types import SimpleNamespace
    source, raw = historical(tmp_path, monkeypatch)
    other = source.with_name("daily_decision_brief.US.2026-07-22.r0099.json")
    other.write_text("{}")
    monkeypatch.setattr(ops, "resolve_runtime_root", lambda **_: SimpleNamespace(runtime_root=tmp_path))
    args = parse_args(["decision-history", "import", "--account", "lx", "--market", "US",
                      "--market-date", "2026-07-21", "--market-date", "2026-07-20"])
    assert handle_decision_history_command(args, repo_base_fn=lambda: tmp_path) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["market_dates"] == ["2026-07-20", "2026-07-21"]
    assert len(report["rows"]) == 1 and report["reserved_revisions"] == {"2026-07-21": 0}
    other.write_text('{"changed": true}')
    with pytest.raises(DecisionHistoryError, match="preview_changed"):
        apply_history_import(base=tmp_path, account="lx", market="US",
                             market_dates=["2026-07-21"], preview_hash=report["preview_hash"])
    args.apply, args.preview_hash = True, report["preview_hash"]
    assert handle_decision_history_command(args, repo_base_fn=lambda: tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["applied_count"] == 1


@pytest.mark.parametrize("day", ["2026-7-21", "2026-02-30", "../2026-07-21", "20260721"])
def test_invalid_date_never_writes(tmp_path, day):
    with pytest.raises(ValueError):
        preview_history_import(base=tmp_path, account="lx", market="US", market_dates=[day])
    with pytest.raises(ValueError):
        apply_history_import(base=tmp_path, account="lx", market="US", market_dates=[day], preview_hash="a"*64)
    assert not history_path(tmp_path).exists()


@pytest.mark.parametrize("option_type,expected_strategy", [("put", "csp"), ("call", "cc")])
def test_recovered_history_allows_canonical_ordinary_decision_without_economic_changes(
    tmp_path, monkeypatch, option_type, expected_strategy,
):
    from datetime import datetime
    from domain.domain.ledger import ContractKey, TradeEvent
    from domain.domain.trade_execution import execution_identity_from_input
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.ledger.writer import persist_trade_event_object
    from src.application.ledger.api import read_trade_attribution_snapshot, read_trade_attribution_facts
    from src.application.trades.attribution import (
        read_attribution_combo_evidence, build_trade_attribution_view, apply_trade_attribution,
    )
    import src.application.daily_decision_brief_repository as delivery
    historical(tmp_path, monkeypatch)
    # A verified v2 empty delivery record means no historical sends, not unavailable.
    state = tmp_path / "output_accounts/lx/state/daily_decision_brief.US.delivery.json"
    state.write_text(json.dumps({"schema_version": delivery.DELIVERY_STATE_SCHEMA_VERSION,
                                "account": "lx", "market": "US", "days": {}}))
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    now = int(datetime.fromisoformat("2026-07-21T15:00:00+00:00").timestamp() * 1000)
    identity = {"external_id_namespace": "futu.deal", "external_execution_id": "123",
                "broker_account_ref": {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}}
    event = TradeEvent(event_id="fill", event_type="open", event_time_ms=now,
        contract_key=ContractKey.from_values(broker="futu", account="lx", underlying_symbol="NVDA",
            option_type=option_type, strike=100, expiration_ymd="2026-08-21"),
        contracts=1, price=2, multiplier=100, currency="USD", source="futu",
        raw_payload={"side": "sell", "execution_input": identity,
                     "execution_id": execution_identity_from_input(identity)})
    persist_trade_event_object(repo, event)
    original = repo.list_trade_events()
    rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
    before = read_attribution_combo_evidence(rows, account="lx", runtime_root=tmp_path, now_ms=now)
    assert before["complete"] is False
    apply(tmp_path, preview(tmp_path))
    evidence = read_attribution_combo_evidence(rows, account="lx", runtime_root=tmp_path, now_ms=now)
    assert evidence["complete"] is True, evidence
    context = {"config": {"accounts": ["lx"], "market": "us",
                "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}}},
               "market": "us", "combo_evidence": evidence, "capacity_observation": {}, "combo_mode": "confirm"}
    view = build_trade_attribution_view(rows, account="lx", now_ms=now, **context)
    row, = view["rows"]
    args = dict(account="lx", execution_key=row["execution_key"], candidate_id="ordinary",
                expected_input_hash=row["input_hash"], request_id="recovered:ordinary",
                actor="operator:lx", manual=True, **context)
    assert not apply_trade_attribution(repo, **args, apply_changes=False)["write_applied"]
    applied = apply_trade_attribution(repo, **args)
    assert applied["write_applied"]
    assert not apply_trade_attribution(repo, **args)["write_applied"]
    fact, = read_trade_attribution_facts(repo, account="lx")
    assert fact["status"] == "ordinary" and fact["origin"] == "manual"
    from domain.domain.wheel import lot_strategy_metadata_from_trade_events
    metadata = lot_strategy_metadata_from_trade_events(repo.list_trade_events())
    assert metadata[fact["lot_id"]]["strategy"] == expected_strategy
    proof = repo.list_trade_events()[-1]
    assert proof["raw_payload"]["patch"]["strategy"] == expected_strategy
    assert repo.list_trade_events()[0] == original[0]


def test_current_experience_snapshot_cannot_be_imported_as_formal_history(tmp_path):
    import test_experience_mode as fixture
    fixture._seal_opening_bundle(tmp_path)
    raw = normalize_daily_decision_brief({**_brief(run_id=fixture.RUN_ID, account=fixture.ACCOUNT),
                                         "revision": 0})
    path = tmp_path / f"output_accounts/{fixture.ACCOUNT}/state/daily_decision_brief.US.2026-07-21.r0000.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(raw))
    (tmp_path / f"output_runs/{fixture.RUN_ID}/accounts/{fixture.ACCOUNT}/state/daily_decision_brief.US.json").write_text(json.dumps(raw))
    report = preview_history_import(base=tmp_path, account=fixture.ACCOUNT, market="US")
    assert report["ready_count"] == 0
    assert report["rows"][0]["reason"] == "historical_formal_evidence_required"
