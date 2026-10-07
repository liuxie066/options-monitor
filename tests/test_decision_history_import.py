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
