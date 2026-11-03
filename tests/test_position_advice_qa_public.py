"""Independent public/history QA. All transport and ledger reads are isolated.

Unresolved contract regressions deliberately fail; no production implementation
is changed by this reviewer. See the corresponding review artifact.
"""
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from src.application import position_advice as service
from src.application.agent_tools import close_advice as public
from src.application.tool_execution import execute_tool
from src.application.ledger.api import read_position_query_ledger as REAL_READ_LEDGER
from src.interfaces.cli import main as cli
from test_position_advice_history import NOW, position, report
from test_position_advice_inputs import manual
from test_position_advice_service import config
from test_position_advice_pipeline import pipeline  # noqa: F401
from test_market_data_provider import no_live_io  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.setattr("src.application.runtime_paths.runtime_root_record_path", lambda **_: tmp_path / "no-home-record")
    monkeypatch.setattr("src.application.quality.paths.repo_base", lambda: tmp_path)
    monkeypatch.setattr(cli, "repo_base", lambda: tmp_path)
    monkeypatch.setattr(public, "repo_base", lambda: ROOT)
    monkeypatch.setattr(public, "load_runtime_config", lambda **_: (tmp_path / "config.us.json", config()))
    monkeypatch.setattr("src.application.ledger.api.read_position_query_ledger", lambda **_: pytest.fail("unexpected ledger I/O"))
    monkeypatch.setattr(service, "_observe_sessions", lambda *_: {"NVDA": "CLOSED"})


def request(**fields):
    return {"account": "sample", "config_key": "us", "positions": [manual()], **fields}


def call_cli(tmp_path, capsys, payload, *extra):
    source = tmp_path / "request.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    status = cli.main(["position-advice", "evaluate", "--request-file", str(source), "--format", "json", *extra])
    return status, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("entry", ["cli", "tool"])
def test_closed_public_manual_is_unavailable_without_history(entry, tmp_path, capsys):
    payload = request()
    if entry == "cli":
        status, result = call_cli(tmp_path, capsys, payload)
        assert status == 0
    else:
        envelope = execute_tool("position_advice_evaluate", payload)
        assert envelope["ok"], envelope
        result = envelope["data"]
    assert result["status"] == "partial"
    row = result["rows"][0]
    assert row["recommendation_state"] == "not_evaluable"
    assert row["current_actionable"] is False
    assert row["dte"] is not None and isinstance(row["dte"], float)
    assert result["notify_rows"] == 0
    assert json.loads(Path(result["report_path"]).read_text()) == result
    assert not (tmp_path / "output_runs").exists()
    assert not list(tmp_path.rglob("*.sqlite*"))


@pytest.mark.parametrize("entry", ["cli", "tool"])
@pytest.mark.parametrize("change", [{"account": "other"}, {"positions": [manual(account="other")]}, {"positions": [manual(lot_id="fake")]}, {"symbols": ["NVDA"]}])
def test_public_invalid_account_or_manual_request_rejected_before_io(entry, change, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(service, "_observe_sessions", lambda *_: pytest.fail("invalid request reached provider"))
    if entry == "cli":
        status, envelope = call_cli(tmp_path, capsys, request(**change))
        assert status != 0
    else:
        envelope = execute_tool("position_advice_evaluate", request(**change))
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "INPUT_ERROR", envelope
    assert not (tmp_path / "output_shared").exists()


@pytest.mark.parametrize("override", [
    {"quote_mode": "legacy_mutable"},
    {"close_advice_required_data_plan_sha256": "c" * 64},
    {"required_data_snapshot_manifest_sha256": ""},
])
def test_closed_tool_rejects_reference_with_invalid_sealed_bindings(tmp_path, monkeypatch, override):
    evaluate = service.evaluate_position_advice
    monkeypatch.setattr(service, "evaluate_position_advice", lambda payload, **kw: evaluate(payload, **kw, now_utc=NOW))
    pos = position()
    path = report(tmp_path / "output_runs" / "old-run" / "accounts" / "sample", pos, **override)
    from src.application.close_advice_report_manifest import read_close_advice_report_snapshot
    validation = read_close_advice_report_snapshot(csv_path=path, account="sample", expected_quote_mode="frozen_snapshot")
    assert validation["validation"]["ok"] is False
    envelope = execute_tool("position_advice_evaluate", request(positions=[manual(input_as_of_utc="2026-07-30T15:00:00Z")]))
    assert envelope["ok"], envelope
    row = envelope["data"]["rows"][0]
    assert row["assessment_mode"] == "unavailable", row
    assert not row.get("reference_recommendation_state")


@pytest.mark.parametrize("entry", ["cli", "tool"])
def test_ledger_public_scope_and_untrusted_failure(entry, tmp_path, capsys, monkeypatch):
    calls = []
    def untrusted(**kwargs):
        calls.append(kwargs)
        return None, {"sample": {"snapshot_status": "unavailable"}}
    monkeypatch.setattr("src.application.ledger.api.read_position_query_ledger", untrusted)
    payload = {"account": "sample", "config_key": "us", "lot_ids": ["wanted"]}
    if entry == "cli":
        status, envelope = call_cli(tmp_path, capsys, payload)
        assert status != 0
    else:
        envelope = execute_tool("position_advice_evaluate", payload)
    assert not envelope["ok"] and envelope["error"]["code"] == "INPUT_ERROR"
    assert calls[0]["accounts"] == ["sample"]
    assert calls[0]["runtime_root"] == tmp_path
    assert not (tmp_path / "output_shared").exists()


@pytest.mark.parametrize("entry", ["cli", "tool"])
def test_missing_real_ledger_is_structured_unavailable(entry, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("src.application.ledger.api.read_position_query_ledger", REAL_READ_LEDGER)
    payload = {"account": "sample", "config_key": "us"}
    if entry == "cli":
        status, envelope = call_cli(tmp_path, capsys, payload)
        assert status != 0
    else:
        envelope = execute_tool("position_advice_evaluate", payload)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] in {"DEPENDENCY_MISSING", "INPUT_ERROR"}, envelope
    assert not list(tmp_path.rglob("*.sqlite*"))
    assert not (tmp_path / "output_shared").exists()


def test_ledger_public_excludes_foreign_account_and_broker(tmp_path, monkeypatch):
    from test_close_advice_required_data import _position
    expiry = "2099-12-31"
    records = [_position(account="sample", lot_id="wanted", expiration=expiry),
               _position(account="other", lot_id="foreign", expiration=expiry),
               _position(account="sample", lot_id="foreign-broker", broker="长桥", expiration=expiry)]
    monkeypatch.setattr("src.application.ledger.api.read_position_query_ledger", lambda **_: (None, {
        "sample": {"snapshot_status": "trusted", "account_position_lots": records, "trade_events": []}}))
    envelope = execute_tool("position_advice_evaluate", {"account": "sample", "config_key": "us"})
    assert envelope["ok"], envelope
    assert [row["position_lot_id"] for row in envelope["data"]["rows"]] == ["wanted"]
    assert all(row["account"] == "sample" and row["broker"] == "富途" for row in envelope["data"]["rows"])


def test_closed_ledger_unavailable_preserves_contract_expiration_and_dte(tmp_path, monkeypatch):
    from test_close_advice_required_data import _position
    expiry = "2099-12-31"
    records = [_position(account="sample", lot_id="wanted", expiration=expiry)]
    monkeypatch.setattr("src.application.ledger.api.read_position_query_ledger", lambda **_: (None, {
        "sample": {"snapshot_status": "trusted", "account_position_lots": records, "trade_events": []}}))
    envelope = execute_tool("position_advice_evaluate", {"account": "sample", "config_key": "us"})
    assert envelope["ok"], envelope
    row = envelope["data"]["rows"][0]
    assert row["assessment_mode"] == "unavailable"
    assert row["expiration"] == expiry, row
    assert row["dte"] > 0


@pytest.mark.parametrize("body", ["{", "[]", " " * 262145])
def test_cli_bad_request_file_returns_input_error(tmp_path, capsys, body):
    source = tmp_path / "invalid.json"
    source.write_text(body)
    status = cli.main(["position-advice", "evaluate", "--request-file", str(source), "--format", "json"])
    envelope = json.loads(capsys.readouterr().out)
    assert status != 0 and not envelope["ok"]
    assert envelope["error"]["code"] == "INPUT_ERROR"


@pytest.mark.parametrize("entry", ["tool", "cli-file", "cli-flag"])
def test_explicit_config_tool_uses_that_instance_when_no_runtime_selected(tmp_path, monkeypatch, capsys, entry):
    code_root = tmp_path / "code"
    instance = tmp_path / "instance"
    code_root.mkdir()
    instance.mkdir()
    monkeypatch.delenv("OM_RUNTIME_ROOT")
    monkeypatch.setattr(public, "repo_base", lambda: code_root)
    monkeypatch.setattr(cli, "repo_base", lambda: code_root)
    monkeypatch.setattr(public, "load_runtime_config", lambda **_: (instance / "config.us.json", config()))
    calls = []
    def trusted_empty(**kwargs):
        from src.application.ledger.api import resolve_position_ledger_sqlite_path
        calls.append({**kwargs, "sqlite_path": resolve_position_ledger_sqlite_path(
            **{key: value for key, value in kwargs.items() if key != "accounts"})})
        return None, {"sample": {"snapshot_status": "trusted", "account_position_lots": [], "trade_events": []}}
    monkeypatch.setattr("src.application.ledger.api.read_position_query_ledger", trusted_empty)
    payload = {"account": "sample", "config_path": str(instance / "config.us.json")}
    if entry == "tool":
        envelope = execute_tool("position_advice_evaluate", payload)
        assert envelope["ok"], envelope
        result = envelope["data"]
    else:
        extra = ["--config-path", payload["config_path"]] if entry == "cli-flag" else []
        status, result = call_cli(tmp_path, capsys, payload, *extra)
        assert status == 0
    assert calls[0]["runtime_root"] == instance, {"ledger": calls, "report_path": result["report_path"]}
    assert calls[0]["sqlite_path"].is_relative_to(instance)
    assert Path(result["report_path"]).is_relative_to(instance)


@pytest.mark.parametrize("binding", ["env", "scope", "record"])
def test_explicit_code_root_is_preserved_even_with_external_config(tmp_path, monkeypatch, binding):
    from contextlib import nullcontext
    from src.application.runtime_paths import runtime_root_scope
    code_root = tmp_path / "code"
    instance = tmp_path / "instance"
    code_root.mkdir()
    instance.mkdir()
    monkeypatch.setattr(public, "repo_base", lambda: code_root)
    monkeypatch.setattr(public, "load_runtime_config", lambda **_: (instance / "config.us.json", config()))
    monkeypatch.delenv("OM_RUNTIME_ROOT")
    if binding == "env":
        monkeypatch.setenv("OM_RUNTIME_ROOT", str(code_root))
    elif binding == "record":
        (code_root / "config.yaml").write_text("# isolated root binding")
        record = tmp_path / "no-home-record"
        record.write_text(str(code_root))
        record.chmod(0o600)
    calls = []
    monkeypatch.setattr("src.application.ledger.api.read_position_query_ledger", lambda **kw: (
        calls.append(kw), {"sample": {"snapshot_status": "trusted", "account_position_lots": [], "trade_events": []}}))
    with runtime_root_scope(code_root) if binding == "scope" else nullcontext():
        envelope = execute_tool("position_advice_evaluate", {"account": "sample", "config_path": str(instance / "config.us.json")})
    assert envelope["ok"], envelope
    assert calls[0]["runtime_root"] == code_root
    assert Path(envelope["data"]["report_path"]).is_relative_to(code_root)


def test_external_config_quality_gate_reads_same_selected_instance(tmp_path, monkeypatch):
    code_root = tmp_path / "code"
    instance = tmp_path / "instance"
    code_root.mkdir()
    instance.mkdir()
    monkeypatch.delenv("OM_RUNTIME_ROOT")
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "true")
    monkeypatch.setattr(public, "repo_base", lambda: code_root)
    monkeypatch.setattr("src.application.quality.paths.repo_base", lambda: code_root)
    monkeypatch.setattr(public, "load_runtime_config", lambda **_: (instance / "config.us.json", config()))
    reads = []
    class Repository:
        def __init__(self, path):
            reads.append(path)
        def read(self):
            return None
    monkeypatch.setattr("src.application.quality.gate.QualityArtifactRepository", Repository)
    envelope = execute_tool("position_advice_evaluate", request(config_path=str(instance / "config.us.json")))
    assert reads == [instance / "output_shared/state/quality/status.v1.json"], reads
    assert not envelope["ok"] and envelope["error"]["code"] == "QUALITY_GATE_BLOCKED", envelope


def test_quality_block_remains_structured_through_runtime_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "true")
    # Correct instance selected by env; its deliberately missing quality status
    # must become a public quality error, never a traceback-assignment error.
    envelope = execute_tool("position_advice_evaluate", request())
    assert not envelope["ok"]
    assert envelope["error"]["code"] == "QUALITY_GATE_BLOCKED", envelope


@pytest.mark.parametrize("entry", ["cli", "tool"])
@pytest.mark.parametrize("fail", [False, True])
def test_actual_public_entry_through_sealed_pipeline(pipeline, monkeypatch, tmp_path, capsys, entry, fail):  # noqa: F811
    payload, provider, root = bind_pipeline(pipeline, monkeypatch)
    provider.fail_fetch = fail
    if entry == "cli":
        status, result = call_cli(tmp_path, capsys, payload)
        assert status == 0
    else:
        envelope = execute_tool("position_advice_evaluate", payload)
        assert envelope["ok"], envelope
        result = envelope["data"]
    row = result["rows"][0]
    assert row["assessment_mode"] == ("unavailable" if fail else "current"), result
    assert row["current_actionable"] is (not fail)
    assert row["recommendation_state"] == ("not_evaluable" if fail else "close"), result
    if not fail:
        assert isinstance(row["ask"], float)
    assert result["notify_rows"] == 0
    assert result["evidence"]["report_validation"]["ok"]
    assert json.loads(Path(result["report_path"]).read_text()) == result
    assert not (root / "output_runs").exists()
    assert not list(root.rglob("*.sqlite*"))


def bind_pipeline(pipeline, monkeypatch):
    _, provider, root = pipeline
    # Restore the real session transport; provider is an offline fixture.
    monkeypatch.setattr(service, "_observe_sessions", _REAL_OBSERVE_SESSIONS)
    cfg = config()
    cfg["market_data"] = {"provider": provider.name}
    cfg["runtime"] = {"prefetch_max_workers": 1}
    monkeypatch.setattr(public, "load_runtime_config", lambda **_: (root / "config.us.json", cfg))
    payload = request(positions=[manual(input_as_of_utc=datetime.now(timezone.utc).isoformat(), expiration=provider.payload["expirations"][0])])
    return payload, provider, root


@pytest.mark.parametrize("missing", ["premium", "contracts_open", "multiplier", "currency"])
def test_public_manual_missing_economics_preserves_known_diagnostics(pipeline, monkeypatch, missing):  # noqa: F811
    payload, _, _ = bind_pipeline(pipeline, monkeypatch)
    del payload["positions"][0][missing]
    envelope = execute_tool("position_advice_evaluate", payload)
    assert envelope["ok"], envelope
    row = envelope["data"]["rows"][0]
    assert row["recommendation_state"] == "not_evaluable", row
    assert row["assessment_mode"] == "unavailable" and row["current_actionable"] is False
    assert row["bid"] == .1 and row["ask"] == .15 and row["spot"] == 110
    assert row["dte"] > 0
    assert envelope["data"]["notify_rows"] == 0


def test_actual_pipeline_then_closed_history_and_read_tool_with_plugin_unavailable(pipeline, monkeypatch):  # noqa: F811
    payload, provider, root = bind_pipeline(pipeline, monkeypatch)
    first = execute_tool("position_advice_evaluate", payload)
    assert first["ok"], first
    original = first["data"]["rows"][0]
    assert original["assessment_mode"] == "current"
    monkeypatch.setattr(provider, "get_market_state", lambda gateway, codes: [{"code": code, "market_state": "CLOSED"} for code in codes])
    monkeypatch.setattr(service, "_run_current_assessment", lambda **_: pytest.fail("closed session evaluated current advice"))
    second = execute_tool("position_advice_evaluate", payload)
    assert second["ok"], second
    row = second["data"]["rows"][0]
    assert row["assessment_mode"] == "historical_reference", second
    assert row["reference_recommendation_state"] == original["recommendation_state"]
    assert row["recommendation_state"] == "not_evaluable" and row["current_actionable"] is False
    for field in ("dte", "spot", "bid", "ask", "assessment_as_of_utc", "capital_basis", "net_capture_ratio"):
        assert row[field] == original[field], field
    csv_path = Path(row["reference_report_path"])
    before = {path: path.read_bytes() for path in csv_path.parent.iterdir() if path.is_file()}
    monkeypatch.setattr("src.application.position_advice_plugin.position_advice_plugin_status", lambda *_: "unavailable")
    monkeypatch.setattr("domain.domain.close_advice.require_position_advice_plugin", lambda: pytest.fail("read tool loaded evaluator"))
    read = execute_tool("close_advice_read", {"report_path": str(csv_path), "query": {"account": "sample"}})
    assert read["ok"], read
    read_row = read["data"]["rows"][0]
    assert read_row["recommendation_state"] == "close"
    assert read_row["current_actionable"] is True
    for field in ("dte", "spot", "bid", "ask", "capital_basis", "net_capture_ratio", "is_otm"):
        assert read_row[field] == original[field], field
    assert before == {path: path.read_bytes() for path in before}
    assert not (root / "output_runs").exists()


_REAL_OBSERVE_SESSIONS = service._observe_sessions
