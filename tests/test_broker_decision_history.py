"""Broker identity survives brief persistence and read-only history queries."""
from types import SimpleNamespace

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools import decision_history as tool
from src.application.daily_decision_brief_repository import persist_daily_decision_brief_success
from src.application.decision_history import read_decision_history
from src.application.agent_tool_registry import get_tool_definition
from test_daily_decision_brief_repository_v2 import _brief
from test_daily_decision_brief_service import _assemble, _config


SCOPE = {"broker_id": "longbridge", "broker_account_id": "LB-123", "trade_env": "REAL",
         "credential_binding_sha256": "e" * 64}


def config():
    return {**_config(), "accounts": ["lx"], "market": "us", "portfolio": {"account": "lx", "source": "longbridge"}, "account_settings": {"lx": {
        "type": "longbridge", "longbridge": {"account_id": "LB-123", "region": "ap", "trd_env": "REAL",
            "credentials_env_prefix": "TEST_LB", "credential_binding_sha256": "e" * 64}}}}


def save(base, run, scope):
    source = _brief(run_id=run)
    source["decision_scope"] = scope
    persist_daily_decision_brief_success(base=base, brief=source)


def test_longbridge_brief_records_generic_identity(tmp_path):
    assert _assemble(tmp_path, config=config())["decision_scope"] == SCOPE


def test_longbridge_history_facade_reads_only_matching_binding(tmp_path, monkeypatch):
    save(tmp_path, "current", SCOPE)
    save(tmp_path, "other-account", {**SCOPE, "broker_account_id": "other"})
    save(tmp_path, "old-credentials", {**SCOPE, "credential_binding_sha256": "f" * 64})
    save(tmp_path, "futu", {"futu_account_id": "LB-123", "trade_env": "REAL"})
    monkeypatch.setattr(tool, "load_runtime_config", lambda **_: (tmp_path / "config.us.json", config()))
    monkeypatch.setattr(tool, "repo_base", lambda: tmp_path)
    monkeypatch.setattr(tool, "resolve_runtime_root", lambda **_: SimpleNamespace(runtime_root=tmp_path))
    payload = {"account": "lx", "market": "US", "start_date": "2026-07-01", "end_date": "2026-07-31"}
    get_tool_definition("decision_history_read").validate_input({**payload, "broker_id": "longbridge", "broker_account_id": "LB-123"})
    result, _, _ = tool._read(payload)
    assert [row["brief"]["run_id"] for row in result["rows"]] == ["current"]
    for field, value in (("broker_id", "futu"), ("broker_account_id", "other"),
                         ("futu_account_id", "LB-123"), ("trade_env", "SIMULATE")):
        with pytest.raises(AgentToolError, match="授权范围"):
            tool._read({**payload, field: value})


def test_longbridge_history_cursor_and_legacy_scope_isolation(tmp_path):
    for run in ("one", "two", "three"):
        save(tmp_path, run, SCOPE)
    save(tmp_path, "unknown", {"futu_account_id": None, "trade_env": None})
    def read(**overrides):
        return read_decision_history(**{**dict(base=tmp_path, account="lx", market="US", scope=SCOPE,
            start_date="2026-07-01", end_date="2026-07-31", limit=1, cursor_key=lambda: "fixture"), **overrides})
    first = read()
    assert first["status"] == "partial"
    assert first["missing"] == [{"reason": "historical_scope_unproven", "count": 1}]
    save(tmp_path, "new-after-page", SCOPE)
    second = read(cursor=first["next_cursor"])
    assert [x["brief"]["run_id"] for x in first["rows"] + second["rows"]] == ["one", "two"]
    with pytest.raises(ValueError, match="cursor_invalid"):
        read(scope={**SCOPE, "credential_binding_sha256": "f" * 64}, cursor=first["next_cursor"])


def test_longbridge_legacy_import_uses_frozen_config_identity(tmp_path):
    import json
    from domain.domain.daily_decision_brief import normalize_daily_decision_brief
    from src.application.tick_run_workspace import publish_account_run_config
    from src.application.strategy_scan_status import publish_strategy_scan_status_index
    from src.application.candidate_snapshot_manifest import publish_candidate_snapshot_manifest
    from src.application.decision_history_import import preview_history_import, apply_history_import
    cfg = config()
    authority = publish_account_run_config(base=tmp_path, run_id="old", account="lx", config=cfg)
    run_dir = tmp_path / "output_runs/old/accounts/lx"
    publish_strategy_scan_status_index(report_dir=run_dir, run_id="old", account="lx",
        account_config_sha256=authority.account_config_sha256, expected=[],
        run_mode={"scan_mode": "standard", "executable": True})
    publish_candidate_snapshot_manifest(base=tmp_path, run_id="old", account="lx", strategy_policy_sha256="b" * 64)
    raw = normalize_daily_decision_brief({**_brief(run_id="old"), "revision": 0})
    raw["decision_scope"] = SCOPE
    path = tmp_path / "output_accounts/lx/state/daily_decision_brief.US.2026-07-21.r0000.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(raw))
    (run_dir / "state/daily_decision_brief.US.json").write_text(json.dumps(raw))
    report = preview_history_import(base=tmp_path, account="lx", market="US")
    assert report["ready_count"] == 1, report
    assert report["rows"][0]["scope"] == SCOPE
    assert apply_history_import(base=tmp_path, account="lx", market="US", preview_hash=report["preview_hash"])["readback"] == "passed"


def test_longbridge_history_does_not_attempt_futu_trade_linkage(tmp_path):
    from test_decision_history_results import result
    from src.application.decision_history_results import attach_trade_results
    value = result()
    path = tmp_path / "not-a-ledger.sqlite3"
    attach_trade_results(value, ledger_path=path, scope=SCOPE, account="lx")
    assert value["status"] == "partial"
    assert value["rows"][0]["trade_results"][0]["reason"] == "unsupported_broker_trade_linkage"
    assert not path.exists()


@pytest.mark.parametrize("change", [{"credential_binding_sha256": None}, {"broker_account_id": ""},
                                    {"trade_env": "SIMULATE"}, {"broker_id": "unknown"}])
def test_unproven_longbridge_history_scope_is_rejected(tmp_path, change):
    with pytest.raises(ValueError, match="history_scope_unproven"):
        read_decision_history(base=tmp_path, account="lx", market="US", scope={**SCOPE, **change},
            start_date="2026-07-01", end_date="2026-07-31", cursor_key=lambda: "fixture")
