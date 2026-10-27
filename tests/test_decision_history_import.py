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


def sealed_legacy(base, *, run_id, revision=0, account="lx", market="US", scope=None, raw=None):
    """Publish real frozen evidence for an isolated run with no applicable strategy scope."""
    from src.application.candidate_snapshot_manifest import publish_candidate_snapshot_manifest
    from src.application.strategy_scan_status import publish_strategy_scan_status_index
    scope = scope or {"futu_account_id": "1001", "trade_env": "REAL"}
    cfg = {"accounts": [account], "market": market.lower(),
           "portfolio": {"account": account, "source": "futu"},
           "account_settings": {account: {"futu": {"account_id": scope["futu_account_id"],
                                                  "trd_env": scope["trade_env"]}}}}
    authority = publish_account_run_config(base=base, run_id=run_id, account=account, config=cfg)
    run_dir = base / "output_runs" / run_id / "accounts" / account
    publish_strategy_scan_status_index(report_dir=run_dir, run_id=run_id, account=account,
        account_config_sha256=authority.account_config_sha256, expected=[],
        run_mode={"scan_mode": "standard", "executable": True})
    publish_candidate_snapshot_manifest(base=base, run_id=run_id, account=account,
        strategy_policy_sha256="b" * 64)
    if raw is None:
        raw = normalize_daily_decision_brief({**_brief(run_id=run_id, account=account, market=market),
                                             "revision": revision})
    path = base / "output_accounts" / account / "state" / (
        f"daily_decision_brief.{market}.{raw['market_trading_date']}.r{raw['revision']:04d}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(raw)
    path.write_text(encoded)
    (run_dir / "state" / f"daily_decision_brief.{market}.json").write_text(encoded)
    return path, raw


def cli_import(base, monkeypatch, capsys, *, account="lx", market="US", apply_preview=None):
    from types import SimpleNamespace
    from src.interfaces.cli.main import parse_args
    from src.interfaces.cli import decision_history_ops as ops
    monkeypatch.setattr(ops, "resolve_runtime_root", lambda **_: SimpleNamespace(runtime_root=base))
    argv = ["decision-history", "import", "--account", account, "--market", market]
    if apply_preview is not None:
        argv.extend(["--apply", "--preview-hash", apply_preview["preview_hash"]])
    assert ops.handle_decision_history_command(parse_args(argv), repo_base_fn=lambda: base) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("status,actionability,successful", [
    ("ready", None, False),
    ("ready", "", False),
    ("ready", "blocked", False),
    ("ready", " BLOCKED ", False),
    ("ready", "live_actionable", True),
    ("degraded", "live_actionable", True),
    (" READY ", "live_actionable", True),
    ("failed", "live_actionable", False),
])
def test_public_import_success_uses_validated_brief_without_rewriting_history(
        tmp_path, monkeypatch, capsys, status, actionability, successful):
    from src.application.daily_decision_brief_repository import (
        persist_daily_decision_brief_success, read_daily_decision_brief, read_latest_daily_decision_brief,
    )
    from domain.domain.daily_decision_brief import (
        normalize_persisted_daily_decision_brief, daily_brief_compatible_digests,
    )
    from src.infrastructure.decision_history_sqlite import content_hash
    from test_decision_history_query import read
    previous = persist_daily_decision_brief_success(base=tmp_path,
        brief=_brief(run_id="earlier-success", market_date="2026-07-20"))
    raw = normalize_daily_decision_brief({**_brief(run_id="legacy"), "revision": 0})
    raw["status"] = status
    if actionability is None:
        raw.pop("actionability")
    else:
        raw["actionability"] = actionability
    raw.pop("strategy_summary")
    source, _ = sealed_legacy(tmp_path, run_id="legacy", raw=raw)
    before = source.read_bytes()
    report = cli_import(tmp_path, monkeypatch, capsys)
    assert report["ready_count"] == 1, report
    assert cli_import(tmp_path, monkeypatch, capsys, apply_preview=report)["applied_count"] == 1
    saved = DecisionHistoryStore(history_path(tmp_path)).get(account="lx", market="US", run_id="legacy")
    assert saved["successful"] == int(successful)
    assert saved["payload"] == saved["input"] == raw
    assert saved["input_hash"] == saved["payload_hash"] == content_hash(raw)
    assert source.read_bytes() == before
    normalized = normalize_persisted_daily_decision_brief(raw)
    assert read_daily_decision_brief(base=tmp_path, account="lx", market="US",
        market_trading_date="2026-07-21", revision=0)["brief"] == normalized
    latest = read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["brief"]
    assert latest == (normalized if successful else previous["brief"])
    imported = next(row for row in read(tmp_path)["rows"] if row["brief"]["run_id"] == "legacy")
    assert imported["brief"] == raw
    assert imported["source_digest"] in daily_brief_compatible_digests(raw)
    assert cli_import(tmp_path, monkeypatch, capsys, apply_preview=report)["already_applied"] is True
    assert DecisionHistoryStore(history_path(tmp_path)).get(
        account="lx", market="US", run_id="legacy")["successful"] == int(successful)


def test_public_import_retains_removed_rejections_until_matching_imports(tmp_path, monkeypatch, capsys):
    from test_decision_history_query import read
    state = tmp_path / "output_accounts/lx/state"
    state.mkdir(parents=True)
    rejected = [state / f"daily_decision_brief.US.2026-07-21.r{revision:04d}.json" for revision in (0, 7)]
    for source in rejected:
        source.write_text("{}")
    report = cli_import(tmp_path, monkeypatch, capsys)
    cli_import(tmp_path, monkeypatch, capsys, apply_preview=report)
    store = DecisionHistoryStore(history_path(tmp_path))
    with store.connect() as conn:
        original_receipts = store.import_reports(conn, account="lx", market="US")
    assert original_receipts == [report]
    assert apply(tmp_path, preview(tmp_path))["readback"] == "no_changes"
    rejected[0].rename(tmp_path / "retained-rejected-r0000.json")
    sealed_legacy(tmp_path, run_id="unrelated", revision=1)
    report = cli_import(tmp_path, monkeypatch, capsys)
    assert report["ready_count"] == 1
    cli_import(tmp_path, monkeypatch, capsys, apply_preview=report)
    result = read(tmp_path)
    assert result["status"] == "partial" and result["coverage"]["status"] == "partial"
    assert result["missing"] == [{"reason": "historical_sources_rejected", "count": 2}]
    for revision, remaining in ((0, 1), (7, 0)):
        sealed_legacy(tmp_path, run_id=f"repaired-{revision}", revision=revision)
        report = cli_import(tmp_path, monkeypatch, capsys)
        assert report["ready_count"] == 1
        # Merely fixing sources and previewing must not erase durable rejection evidence.
        assert read(tmp_path)["missing"] == [{"reason": "historical_sources_rejected", "count": remaining + 1}]
        assert cli_import(tmp_path, monkeypatch, capsys, apply_preview=report)["applied_count"] == 1
        assert store.get(account="lx", market="US", run_id=f"repaired-{revision}") is not None
        result = read(tmp_path)
        assert result["missing"] == ([{"reason": "historical_sources_rejected", "count": remaining}] if remaining else [])
    assert result["status"] == "ok" and result["coverage"]["status"] == "complete"
    with store.connect() as conn:
        receipts = store.import_reports(conn, account="lx", market="US")
        assert receipts[:1] == original_receipts
        assert len(receipts) == 4
    assert apply(tmp_path, original_receipts[0])["already_applied"] is True
    assert read(tmp_path)["missing"] == []  # Replaying an old rejection receipt is not a new rejection.


@pytest.mark.parametrize("rejected_scope", [
    {"futu_account_id": "1001", "trade_env": "REAL"},
    {"futu_account_id": "1002", "trade_env": "REAL"},
    {"futu_account_id": "1001", "trade_env": "SIMULATE"},
])
def test_imported_same_source_cannot_resolve_different_proven_identity(tmp_path, rejected_scope):
    from test_decision_history_query import save, read, SCOPE
    saved = save(tmp_path, "existing")["brief"]
    source, _ = sealed_legacy(tmp_path, run_id="conflicting", scope=rejected_scope)
    rejected = preview(tmp_path)
    assert rejected["rows"][0]["reason"] == "history_import_conflict"
    assert rejected["rows"][0]["scope"] == rejected_scope
    apply(tmp_path, rejected)
    assert read(tmp_path, scope=rejected_scope)["missing"] == [{"reason": "historical_sources_rejected", "count": 1}]
    replacement, _ = sealed_legacy(tmp_path, run_id="existing", raw=saved)
    assert replacement == source
    report = preview(tmp_path)
    assert report["rows"][0]["status"] == "already_imported", report
    apply(tmp_path, report)
    assert read(tmp_path, scope=rejected_scope)["missing"] == [{"reason": "historical_sources_rejected", "count": 1}]
    if rejected_scope != SCOPE:
        assert read(tmp_path)["missing"] == []


def test_import_rejection_account_market_isolation(tmp_path, monkeypatch, capsys):
    from test_decision_history_query import read
    source = tmp_path / "output_accounts/lx/state/daily_decision_brief.US.2026-07-21.r0000.json"
    source.parent.mkdir(parents=True)
    source.write_text("{}")
    apply(tmp_path, preview(tmp_path))
    source.rename(tmp_path / "retained-unresolved.json")
    for account, market in (("sy", "US"), ("lx", "HK")):
        sealed_legacy(tmp_path, run_id=f"{account}-{market}", account=account, market=market)
        report = cli_import(tmp_path, monkeypatch, capsys, account=account, market=market)
        assert report["ready_count"] == 1
        cli_import(tmp_path, monkeypatch, capsys, account=account, market=market, apply_preview=report)
        result = read(tmp_path, account=account, market=market)
        assert result["status"] == "ok" and result["missing"] == []
        assert read(tmp_path)["missing"] == [{"reason": "historical_sources_rejected", "count": 1}]


def test_repaired_proven_rejection_resolves_with_matching_imported_identity(tmp_path):
    from test_decision_history_query import save, read
    saved = save(tmp_path, "existing")["brief"]
    source, _ = sealed_legacy(tmp_path, run_id="existing",
        raw={**saved, "strategy_summary": "conflicting historical content"})
    report = preview(tmp_path)
    assert report["rows"][0]["reason"] == "history_import_conflict"
    apply(tmp_path, report)
    assert read(tmp_path)["missing"] == [{"reason": "historical_sources_rejected", "count": 1}]
    encoded = json.dumps(saved)
    source.write_text(encoded)
    (tmp_path / "output_runs/existing/accounts/lx/state/daily_decision_brief.US.json").write_text(encoded)
    report = preview(tmp_path)
    assert report["rows"][0]["status"] == "already_imported"
    assert read(tmp_path)["missing"]  # A preview is not a committed import receipt.
    assert apply(tmp_path, report)["applied_count"] == 0
    result = read(tmp_path)
    assert result["status"] == "ok" and result["missing"] == []
    assert result["rows"][0]["brief"] == saved
