import hashlib
from types import SimpleNamespace

import pytest

from test_daily_decision_brief_repository_v2 import _brief, _action
from src.application.daily_decision_brief_repository import persist_daily_decision_brief_success
from src.application.decision_history import read_decision_history
from src.infrastructure.decision_history_sqlite import history_path

SCOPE = {"futu_account_id": "1001", "trade_env": "REAL"}


def save(base, run, *, scope=None, account="lx", symbol="NVDA"):
    source = _brief(run_id=run, account=account, actions=[{**_action(symbol=symbol), "account": account}])
    source["decision_scope"] = SCOPE if scope is None else scope
    return persist_daily_decision_brief_success(base=base, brief=source)


def read(base, **overrides):
    return read_decision_history(**{**dict(base=base, account="lx", market="US", scope=SCOPE,
        start_date="2026-07-01", end_date="2026-07-31", cursor_key=lambda: "isolated-test-key"), **overrides})


def test_fixed_pages_isolation_symbol_and_refresh(tmp_path):
    for i in range(4):
        save(tmp_path, str(i))
    save(tmp_path, "other-env", scope={**SCOPE, "trade_env": "SIMULATE"})
    save(tmp_path, "other-physical", scope={**SCOPE, "futu_account_id": "1002"})
    save(tmp_path, "other-account", account="sy")
    first = read(tmp_path, limit=2)
    save(tmp_path, "new")
    second = read(tmp_path, limit=2, cursor=first["next_cursor"])
    assert [x["brief"]["run_id"] for x in first["rows"] + second["rows"]] == ["0", "1", "2", "3"]
    assert second["next_cursor"] is None
    assert len(read(tmp_path)["rows"]) == 5
    assert read(tmp_path, symbol="AMD")["status"] == "empty"
    with pytest.raises(ValueError, match="cursor_invalid"):
        read(tmp_path, symbol="AMD", cursor=first["next_cursor"])
    with pytest.raises(ValueError, match="cursor_invalid"):
        read(tmp_path, scope={**SCOPE, "trade_env": "SIMULATE"}, cursor=first["next_cursor"])


def test_notification_retry_uses_original_tuple_not_attempt(tmp_path):
    saved = save(tmp_path, "original")
    save(tmp_path, "later")
    ref = {"account": "lx", "market": "US", "market_date": "2026-07-21", "revision": 0,
           "source_run_id": "original", "source_digest": saved["current_brief_digest"], "run_id": "retry-attempt"}
    assert read(tmp_path, reference=ref)["rows"][0]["brief"]["run_id"] == "original"
    for change in ({"source_run_id": "retry-attempt"}, {"source_digest": "0" * 64}, {"revision": 99}, {"account": "sy"}):
        result = read(tmp_path, reference={**ref, **change})
        assert result["status"] == "unavailable" and not result["rows"]
        assert result["history_query_available"] is True
    assert read(tmp_path, reference={})["status"] == "unavailable"


def test_read_missing_partial_and_cancel_never_write(tmp_path):
    assert read(tmp_path)["status"] == "unavailable"
    assert not history_path(tmp_path).exists()
    save(tmp_path, "unknown", scope={})
    result = read(tmp_path)
    assert result["status"] == "unavailable" and result["missing"][0]["reason"] == "historical_scope_unproven"
    save(tmp_path, "known")
    def files():
        return {str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob('*') if p.is_file()}
    before = files()
    assert read(tmp_path)["status"] == "partial"
    assert read(tmp_path)["rows"] == read(tmp_path)["rows"]
    with pytest.raises(ValueError, match="cancelled"):
        read(tmp_path, cancelled=lambda: True)
    assert files() == before


def test_registered_facade_checks_authority_and_has_no_external_effects(tmp_path, monkeypatch):
    from src.application.agent_tools import decision_history as tool
    from src.application.agent_tool_registry import get_tool_definition
    from src.application.agent_tool_contracts import AgentToolError
    save(tmp_path, "one")
    cfg = {"accounts": ["lx"], "market": "us", "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}}}
    monkeypatch.setattr(tool, "load_runtime_config", lambda **_: (tmp_path / "config.us.json", cfg))
    monkeypatch.setattr(tool, "repo_base", lambda: tmp_path)
    monkeypatch.setattr(tool, "resolve_runtime_root", lambda **_: SimpleNamespace(runtime_root=tmp_path))
    definition = get_tool_definition("decision_history_read")
    assert definition.is_pure_read()
    payload = {"account": "lx", "market": "US", "start_date": "2026-07-01", "end_date": "2026-07-31"}
    definition.validate_input(payload)
    result, _, meta = definition.handler(payload)
    assert result["status"] == "ok" and meta["read_only"]
    assert result["rows"][0]["trade_results"][0]["status"] == "unlinked"
    for change in ({"account": "sy"}, {"market": "HK"}, {"trade_env": "SIMULATE"}, {"futu_account_id": "1002"}):
        with pytest.raises(AgentToolError):
            definition.handler({**payload, **change})
    with pytest.raises(AgentToolError):
        definition.validate_input({**payload, "refresh": True})
