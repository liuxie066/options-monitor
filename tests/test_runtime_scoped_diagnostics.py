from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.agent_tools import project_reader
from src.application.bot import tools
from src.application.tool_execution import execute_tool


def build_runtime_diagnostic_fixture(
    directory: Path, *, typed_error: bool = True, run_id: str = "run-1",
) -> dict:
    """Generate A12 bytes through run_one_account and its real metrics/audit writers.

    Reuses the existing typed child-error fixture; only the child process is fake.
    The returned files can be reused unchanged by Host/Pi local acceptance runs.
    """
    from domain.storage.repositories import state_repo
    from src.application.account_run import run_one_account
    from tests.test_account_run import _FakeRunlog, _install_common_patches, _make_request

    directory.mkdir(parents=True, exist_ok=True)
    request = replace(_make_request(directory, prefetch_done=True), account_config_generation_frozen=True,
        allow_notifications=False, allow_mutations=False)
    if run_id != request.run_id:
        from src.application.tick_run_workspace import publish_account_run_config
        authority = publish_account_run_config(base=request.base, run_id=run_id, account=request.acct,
            config=json.loads(request.account_config_authority.canonical_bytes))
        run_dir = request.base / "output_runs" / run_id
        request = replace(request, run_id=run_id, run_dir=run_dir, accounts_root=run_dir / "accounts",
            account_config_authority=authority)
    write_metrics = state_repo.write_account_run_state
    append_audit = state_repo.append_run_audit_jsonl
    ensure_account_state = state_repo.run_repo.ensure_run_account_state_dir
    with pytest.MonkeyPatch.context() as patch:
        env = _install_common_patches(patch, request)
        patch.setattr(state_repo, "write_account_run_state", write_metrics)
        patch.setattr(state_repo, "append_run_audit_jsonl", append_audit)
        patch.setattr(state_repo.run_repo, "ensure_run_account_state_dir", ensure_account_state)
        patch.setattr(env["mod"], "decide_account_scan_gate", lambda **kwargs: {
            "run_pipeline": True, "ran_scan": True, "meaningful": True, "result_reason": "run"})
        patch.setattr(env["mod"], "run_pipeline_script", lambda **kwargs: SimpleNamespace(
            returncode=2, stdout="", stderr=("[CONFIG_ERROR] ACCOUNT_CONFIG_HASH_MISMATCH: "
                "retained account authority invalid" if typed_error else "private stderr token=" + "sk-" + "private-should-not-leak")))
        patch.setattr(env["mod"], "normalize_pipeline_subprocess_output",
            lambda **kwargs: {"returncode": kwargs["returncode"], "adapter": "pipeline"})
        patch.setattr(env["mod"], "decide_pipeline_execution_result", lambda **kwargs: {
            "ok": False, "ran_scan": True, "meaningful": False, "reason": "pipeline failed"})

        def audit(event_type, action, **kwargs):
            payload = state_repo.normalize_audit_event({"event_type": event_type, "action": action, **kwargs})
            append_audit(request.base, request.run_id, "audit_events.jsonl", payload)

        outcome = run_one_account(request=request, runlog=_FakeRunlog(), audit_fn=audit,
            fail_schema_validation=lambda **kwargs: None)
    metrics = request.base / f"output_runs/{request.run_id}/accounts/lx/state/account_metrics.json"
    audit_path = request.base / f"output_runs/{request.run_id}/state/audit_events.jsonl"
    return {"root": request.base, "run_id": request.run_id, "account": "lx", "metrics": metrics,
        "audit": audit_path, "reason": outcome.result.decision_reason,
        "hashes": {"metrics": hashlib.sha256(metrics.read_bytes()).hexdigest(),
            "audit": hashlib.sha256(audit_path.read_bytes()).hexdigest()}}


@pytest.fixture
def diagnostic_scope(tmp_path, example_config_path, monkeypatch):
    fixture = build_runtime_diagnostic_fixture(tmp_path / "diagnostic")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(fixture["root"]))
    monkeypatch.setattr(project_reader, "_key", lambda: "isolated-diagnostic-cursor-key")
    payload, error = tools.build_tool_payload("runtime_logs", {"account": "lx", "run_id": fixture["run_id"]},
        fixed_input={"config_path": str(example_config_path)})
    assert error is None
    return fixture, payload


def _read(payload, **kwargs):
    response = tools.call_read_tool("runtime_logs", payload, allowed_tools=("runtime_logs",), **kwargs)
    assert response["ok"], response
    observation = tools.model_observation("runtime_logs", response)
    assert tools.conservative_json_tokens(observation) <= 8000
    return response["data"], observation


@pytest.mark.parametrize("typed", [True, False])
def test_real_writer_diagnostic_reason_and_stage_only_public_projection(tmp_path, example_config_path, monkeypatch, typed):
    fixture = build_runtime_diagnostic_fixture(tmp_path / "writer", typed_error=typed,
        run_id="synthetic-writer-failure")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(fixture["root"]))
    payload, error = tools.build_tool_payload("runtime_logs", {"account": "lx", "run_id": fixture["run_id"]},
        fixed_input={"config_path": str(example_config_path)})
    assert error is None
    data, observation = _read(payload)
    assert payload["action"] == "scoped" and "kind" not in payload and "lines" not in payload
    data_for_model = observation["data"]
    assert data_for_model["freshness"]["status"] == "historical"
    assert data_for_model["coverage"]["status"] == "complete"
    assert data_for_model["record_relationship"] == "independent_records_no_causal_or_sequence_link"
    metrics = next(row for row in data_for_model["diagnostics"] if row["source_kind"] == "account_metrics")
    stage = next(row for row in data_for_model["diagnostics"] if row["source_kind"] == "run_audit")
    assert metrics["outcomes"] == {
        "usable_scan_result": not typed,
        "pipeline_completed_successfully": False,
    }
    assert "ran_scan" not in metrics and "ran_pipeline" not in metrics
    assert metrics["causal_link_to_other_records"] == "not_established"
    assert metrics["reason"] == ("account_config_hash_mismatch" if typed else None)
    assert metrics["cause_available"] is typed
    assert stage["failure_stage"] == "run_pipeline" and stage["cause_available"] is False
    assert stage["error_code"] is None  # The actual writer does not persist it on this audit row.
    assert "private stderr" not in json.dumps(observation)
    assert str(fixture["root"]) not in json.dumps(observation)
    assert data["pagination"]["matched_count"] == 2
    assert hashlib.sha256(fixture["metrics"].read_bytes()).hexdigest() == fixture["hashes"]["metrics"]
    assert hashlib.sha256(fixture["audit"].read_bytes()).hexdigest() == fixture["hashes"]["audit"]
    definition = tools.tool_descriptions(("runtime_logs",))[0]
    assert definition["default_input"]["action"] == "scoped"
    assert definition["output_contract"]["schema_version"] == "runtime_logs.scoped.v1"


def test_diagnostic_pages_bind_query_and_all_source_bytes(diagnostic_scope):
    fixture, payload = diagnostic_scope
    row = next(json.loads(line) for line in fixture["audit"].read_text().splitlines()
        if json.loads(line)["action"] == "run_pipeline_result")
    rows = [{**row, "event_at_utc": f"2026-04-25T00:00:{index:02}Z"} for index in range(27)]
    fixture["audit"].write_text("".join(json.dumps(item) + "\n" for item in rows))
    query, seen, first_cursor = {**payload, "limit": 4}, [], None
    while True:
        data, observation = _read(query)
        assert data["pagination"]["matched_count"] == 28
        seen.extend(row["event_at_utc"] for row in data["diagnostics"] if row["source_kind"] == "run_audit")
        assert observation["data"]["next_cursor"] == data["next_cursor"]
        if not data["next_cursor"]:
            break
        first_cursor = first_cursor or data["next_cursor"]
        query["cursor"] = data["next_cursor"]
    assert seen == [item["event_at_utc"] for item in rows]
    bad = execute_tool("runtime_logs", {**payload, "limit": 3, "cursor": first_cursor})
    assert not bad["ok"] and bad["error"]["message"] == "cursor_invalidated"
    with fixture["audit"].open("a") as stream:
        stream.write(json.dumps({**row, "account": "sy", "message": "OTHER_ACCOUNT_PRIVATE"}) + "\n")
    changed = execute_tool("runtime_logs", {**payload, "limit": 4, "cursor": first_cursor})
    assert not changed["ok"] and changed["error"]["message"] == "source_changed"
    clean, observation = _read(payload)
    assert clean["pagination"]["total_count"] == 28
    assert "OTHER_ACCOUNT_PRIVATE" not in json.dumps(observation)


@pytest.mark.parametrize("change", [
    {"account": "sy"}, {"run_id": "different"}, {"markets_to_run": ["US", "HK"]},
    {"markets_to_run": ["HK"]}, {"as_of_utc": "2026-04-25"}, {"ran_scan": "true"}, {"reason": ["bad"]},
])
def test_diagnostic_metrics_identity_is_required(diagnostic_scope, change):
    fixture, payload = diagnostic_scope
    original = json.loads(fixture["metrics"].read_text())
    fixture["metrics"].write_text(json.dumps({**original, **change}))
    response = execute_tool("runtime_logs", payload)
    assert response["ok"] is False
    assert "diagnostics" not in response.get("data", {})


def test_mixed_unknown_conflicting_and_secret_rows_are_never_projected(diagnostic_scope):
    fixture, payload = diagnostic_scope
    row = next(json.loads(line) for line in fixture["audit"].read_text().splitlines()
        if json.loads(line)["action"] == "run_pipeline_result")
    secret = "SY_PRIVATE_TOKEN_VALUE"
    rows = [row, {**row, "account": "sy", "message": secret},
        {**row, "account": None, "message": secret}, {**row, "run_id": "wrong", "message": secret},
        {**row, "schema_version": "future", "message": secret},
        {**row, "extra": {**row["extra"], "account": "sy", "message": secret}},
        {**row, "extra": {**row["extra"], "failure_stage": secret}},
        {**row, "extra": {**row["extra"], "failure_kind": [secret]}},
        {**row, "error_code": "ACCOUNT_CONFIG_" + secret, "message": "multiline\nsecret=" + secret}]
    fixture["audit"].write_text("".join(json.dumps(item) + "\n" for item in rows) + '{"broken":\n' + secret + '\n')
    data, observation = _read(payload)
    assert data["coverage"]["status"] == "partial"
    assert data["pagination"]["total_count"] is None
    assert data["pagination"]["matched_count"] is None
    assert secret not in json.dumps(observation)
    assert '"sy"' not in json.dumps(observation)
    assert data["skipped_authorized_count"] == 5


@pytest.mark.parametrize("source", ["metrics", "audit"])
def test_source_symlinks_cannot_be_resolved_before_safe_reader(diagnostic_scope, source):
    fixture, payload = diagnostic_scope
    path = fixture[source]
    target = path.with_name("private-target")
    path.rename(target)
    path.symlink_to(target)
    response = execute_tool("runtime_logs", payload)
    if source == "metrics":
        assert response["ok"] is False
    else:
        assert response["data"]["coverage"]["status"] == "partial"
        assert response["data"]["pagination"]["matched_count"] is None
        assert all(row["source_kind"] != "run_audit" for row in response["data"]["diagnostics"])
    assert "private-target" not in json.dumps(response)


def test_oversize_unavailable_signer_and_budget_recovery(diagnostic_scope, monkeypatch):
    import time
    fixture, payload = diagnostic_scope
    def unavailable():
        raise project_reader.ProjectReaderError("continuation_unavailable")
    monkeypatch.setattr(project_reader, "_key", unavailable)
    data, _ = _read({**payload, "limit": 1})
    assert data["next_cursor"] is None and data["continuation_status"] == "continuation_unavailable"
    assert data["coverage"]["status"] == "partial"
    for kwargs, code in (({"cancelled": lambda: True}, "CANCELLED"),
                         ({"deadline_monotonic": time.monotonic() - 1}, "BUDGET_EXHAUSTED")):
        failed = tools.call_read_tool("runtime_logs", payload, allowed_tools=("runtime_logs",), **kwargs)
        assert not failed["ok"] and failed["error"]["code"] == code
        recovered, _ = _read(payload)
        assert recovered["read_status"] == "ok"
    fixture["audit"].write_bytes(b"x" * (1024 * 1024 + 1))
    oversized, _ = _read(payload)
    assert oversized["coverage"]["status"] == "partial"
    assert "audit_file_too_large" in oversized["missing_data"]
    assert oversized["pagination"]["matched_count"] is None


def test_scoped_diagnostics_reject_arbitrary_roots_and_other_account(diagnostic_scope):
    _, payload = diagnostic_scope
    for patch in ({"log_file": "/private/path"}, {"runs_root": "/private/path"},
                  {"kind": "service"}, {"account": "not-authorized"}, {"run_id": "../../outside"}):
        response = execute_tool("runtime_logs", {**payload, **patch})
        assert response["ok"] is False
    _, error = tools.build_tool_payload("runtime_logs", {"log_file": "/private/path"},
        fixed_input={"config_path": payload["config_path"]})
    assert error is not None


def test_metrics_source_drift_and_corrupt_sources_recover(diagnostic_scope):
    fixture, payload = diagnostic_scope
    original_metrics = fixture["metrics"].read_bytes()
    first, _ = _read({**payload, "limit": 1})
    fixture["metrics"].write_bytes(original_metrics + b"\n")
    changed = execute_tool("runtime_logs", {**payload, "limit": 1, "cursor": first["next_cursor"]})
    assert not changed["ok"] and changed["error"]["message"] == "source_changed"
    fixture["metrics"].write_bytes(b"{broken")
    corrupt = execute_tool("runtime_logs", payload)
    assert not corrupt["ok"] and corrupt["error"]["message"] == "metrics_invalid"
    fixture["metrics"].write_bytes(original_metrics)
    audit_bytes = fixture["audit"].read_bytes()
    fixture["audit"].unlink()
    absent, _ = _read(payload)
    assert absent["coverage"]["status"] == "partial" and "audit_not_found" in absent["missing_data"]
    fixture["audit"].write_bytes(b"\xff")
    corrupt_audit, _ = _read(payload)
    assert "audit_invalid_encoding" in corrupt_audit["missing_data"]
    fixture["audit"].write_bytes(audit_bytes)
    recovered, _ = _read(payload)
    assert recovered["read_status"] == "ok"
    assert recovered["source_hash"] == first["source_hash"]
