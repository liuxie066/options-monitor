from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application import position_advice as service
from test_position_advice_inputs import manual
from test_position_advice_history import report, position

NOW = datetime(2026, 8, 2, 15, tzinfo=timezone.utc)


def config():
    return {"accounts": ["sample"], "portfolio": {"account": "sample", "broker": "富途"}, "close_advice": {"enabled": True}, "symbols": [{"symbol": "NVDA", "broker": "US", "fetch": {"source": "opend", "host": "offline.invalid", "port": 11111}}]}


@pytest.fixture(autouse=True)
def no_live(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("attempted unexpected external I/O")
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("src.application.market_data_provider.OpenDMarketDataProvider.open_quote_gateway", forbidden)


def evaluate(tmp_path, payload, **kwargs):
    return service.evaluate_position_advice(payload, config=config(), config_path=tmp_path / "config.us.json", base_dir=Path(__file__).resolve().parents[1], runtime_root=tmp_path, now_utc=NOW, **kwargs)


def test_closed_reference_no_fetch_ledger_or_scheduled_report_write(tmp_path, monkeypatch):
    pos = position()
    path = report(tmp_path / "output_runs" / "old-run" / "accounts" / "sample", pos)
    original = path.read_bytes()
    monkeypatch.setattr(service, "_observe_sessions", lambda *_: {"NVDA": "CLOSED"})
    monkeypatch.setattr(service, "_read_holdings", lambda **_: pytest.fail("manual request read ledger"))
    monkeypatch.setattr(service, "_run_current_assessment", lambda **_: pytest.fail("closed session fetched option quotes"))
    data = evaluate(tmp_path, {"account": "sample", "positions": [manual(input_as_of_utc="2026-07-30T15:00:00Z")]})
    assert data["rows"][0]["assessment_mode"] == "historical_reference"
    assert data["rows"][0]["position_lot_id"] is None
    assert data["notify_rows"] == 0
    assert data["rows"][0]["recommendation_state"] == "not_evaluable"
    assert path.read_bytes() == original
    assert Path(data["report_path"]).is_relative_to(tmp_path / "output_shared" / "position_advice_requests")


@pytest.mark.parametrize("session", ["CLOSED", "REST", "PRE_MARKET_BEGIN", "AFTER_HOURS_BEGIN", "UNKNOWN"])
def test_no_compatible_snapshot_explicit_unavailable(tmp_path, monkeypatch, session):
    monkeypatch.setattr(service, "_observe_sessions", lambda *_: {"NVDA": session})
    data = evaluate(tmp_path, {"account": "sample", "positions": [manual()]})
    assert data["status"] == "partial"
    assert data["rows"][0]["recommendation_state"] == "not_evaluable"
    assert data["rows"][0]["assessment_mode"] == "unavailable"
    assert data["notify_rows"] == 0


@pytest.mark.parametrize("input_id", ["null", "None", "NaN"])
def test_public_result_and_saved_report_preserve_literal_manual_identity(tmp_path, monkeypatch, input_id):
    monkeypatch.setattr(service, "_observe_sessions", lambda *_: {"NVDA": "CLOSED"})
    data = evaluate(tmp_path, {"account": "sample", "positions": [manual(input_id=input_id)]})
    assert data["rows"][0]["input_id"] == input_id
    assert data["rows"][0]["current_actionable"] is False
    assert isinstance(data["rows"][0]["dte"], float)
    assert json.loads(Path(data["report_path"]).read_text())["rows"][0]["input_id"] == input_id


def test_multiple_manual_active_requests_share_selected_inputs_and_isolated_artifacts(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_observe_sessions", lambda *_: {"NVDA": "MORNING", "AAPL": "MORNING"})
    def current(**kwargs):
        calls.append(kwargs)
        return [{**p, "position_lot_id": None, "assessment_mode": "current", "current_actionable": True, "recommendation_state": "hold"} for p in kwargs["positions"]], {"fixture": True}
    monkeypatch.setattr(service, "_run_current_assessment", current)
    request = {"account": "sample", "positions": [manual(), manual(input_id="second", symbol="AAPL", option_type="call", strike=200)]}
    first = evaluate(tmp_path, request)
    second = evaluate(tmp_path, request)
    assert first["returned_count"] == 2
    assert [row["symbol"] for row in first["rows"]] == ["NVDA", "AAPL"]
    assert first["request_id"] != second["request_id"]
    assert calls[0]["manual"] and calls[0]["records"] == []
    assert calls[0]["workspace"] != calls[1]["workspace"]
    assert first["notify_rows"] == 0


def test_ledger_selected_lots_only_flow_to_current_assessment(tmp_path, monkeypatch):
    from test_close_advice_required_data import _position
    monkeypatch.setattr(service, "_read_holdings", lambda **_: [_position(account="sample", lot_id="wanted"), _position(account="sample", lot_id="other")])
    monkeypatch.setattr(service, "_observe_sessions", lambda *_: {"NVDA": "MORNING"})
    calls = []
    def current(**kwargs):
        calls.append(kwargs)
        return [{**p, "position_lot_id": p["lot_id"], "assessment_mode": "current", "current_actionable": True} for p in kwargs["positions"]], {}
    monkeypatch.setattr(service, "_run_current_assessment", current)
    data = evaluate(tmp_path, {"account": "sample", "lot_ids": ["wanted"]})
    assert data["returned_count"] == 1
    assert [row["record_id"] for row in calls[0]["records"]] == ["wanted"]
    assert calls[0]["manual"] is False


@pytest.mark.parametrize("state", ["disabled", "unavailable"])
def test_plugin_unavailable_or_disabled_has_no_io(tmp_path, monkeypatch, state):
    monkeypatch.setattr("src.application.position_advice_plugin.position_advice_plugin_status", lambda *_: state)
    monkeypatch.setattr(service, "_read_holdings", lambda **_: pytest.fail("disabled plugin read ledger"))
    data = evaluate(tmp_path, {"account": "sample"})
    assert data["status"] == state and data["rows"] == []
    assert list(tmp_path.iterdir()) == []


def test_bad_requests_rejected_before_io(tmp_path):
    with pytest.raises(ValueError, match="configured"):
        evaluate(tmp_path, {"account": "other", "positions": [manual()]})
    with pytest.raises(ValueError, match="mixed"):
        evaluate(tmp_path, {"account": "sample", "positions": [manual()], "symbols": ["NVDA"]})
    assert list(tmp_path.iterdir()) == []


def test_public_tool_routes_to_same_service_and_declares_effects(tmp_path, monkeypatch):
    from src.application.agent_tools import close_advice
    tool = close_advice.POSITION_ADVICE_EVALUATE_TOOL
    assert not tool.is_pure_read()
    assert "fetches_market_data" in tool.side_effects
    assert "writes_isolated_request_reports" in tool.side_effects
    monkeypatch.setattr(close_advice, "load_runtime_config", lambda **_: (tmp_path / "config.us.json", config()))
    monkeypatch.setattr(close_advice, "repo_base", lambda: tmp_path)
    monkeypatch.setattr("src.application.runtime_paths.resolve_runtime_root", lambda **_: SimpleNamespace(runtime_root=tmp_path))
    monkeypatch.setattr(service, "_observe_sessions", lambda *_: {"NVDA": "CLOSED"})
    data, _warnings, meta = tool.call({"config_key": "us", "account": "sample", "positions": [manual()]})
    assert data["returned_count"] == 1 and data["notify_rows"] == 0
    assert meta["notifications_sent"] is False
    from src.application.agent_tool_contracts import AgentToolError
    with pytest.raises(AgentToolError):
        tool.call({"account": "sample", "positions": [manual(lot_id="fake")]})
