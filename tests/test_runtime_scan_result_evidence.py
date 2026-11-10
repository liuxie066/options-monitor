"""Result availability uses verified scoped evidence, never attempt metadata.

Scope fixtures serialize canonical bytes directly; zero-scope regressions also
exercise immutable production publishers in isolated runtime roots.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.agent_tools import project_runs
from src.application.agent_tools.project_reader import ProjectReaderError
from src.application.bot import tools
from src.application.candidate_snapshot_contract import (
    combo_opening_status, normalize_combo_scope_results, normalize_dependencies,
)
from src.application.candidate_snapshot_manifest import _expected_scopes, _snapshot_strategy_scopes
from src.application.combo_yield_candidate_snapshot import COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA
from src.application.experience_mode import experience_fields
from src.application.runtime_paths import RuntimeRootResolution
from src.application.strategy_scan_status import _canonical_index_content, _validate_current_status_core

RUN = "run-802"
ACCOUNT = "lx"
NOW = "2026-10-21T00:00:00Z"
CONFIG = "a" * 64
POLICY = "b" * 64


def _write(path: Path, payload: dict) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode()
    with path.open("xb") as stream:
        stream.write(raw)
    return raw


def _signed(payload: dict, *, index: bool = False) -> dict:
    return {**payload, "content_sha256": (
        hashlib.sha256(_canonical_index_content(payload)).hexdigest() if index
        else canonical_sha256(payload)
    )}


def _bundle(root: Path, states: tuple[str, ...], *, version: int = 4, positive: bool = False) -> Path:
    account_dir = root / f"output_runs/{RUN}/accounts/{ACCOUNT}"
    mode = experience_fields("Synthetic account") if version == 2 else {"scan_mode": "standard", "executable": True}
    items = []
    for symbol, state in zip(("AAPL", "NVDA"), states):
        source = {"schema_version": "strategy_scan_status.v3" if version == 4 else "strategy_scan_status.v1",
                  "run_id": RUN, "account": ACCOUNT, "market": "US", "symbol": symbol,
                  "strategy_family": "combo_yield", "status": state, "published_at_utc": NOW,
                  "reason": "no_candidate" if state == "completed" else "fixture_source_failed"}
        if state == "completed":
            source["candidate_count"] = int(positive)
            if positive:
                source["reason"] = ""
        if version == 4:
            source = _signed(source, index=True)
        suffix = ".v3" if version == 4 else ""
        path = account_dir / f"{symbol.lower()}_combo_yield_scan_status{suffix}.json"
        raw = _write(path, source)
        core = (_validate_current_status_core(path=path, run_id=RUN, account=ACCOUNT,
                market="US", symbol=symbol, strategy_family="combo_yield", direction=None)
                if version == 4 else {**source, "source_status_schema": source["schema_version"]})
        items.append({**core, "strategy_mode": "combo_yield", "candidate_owner": "sp_lc",
                      "account_config_sha256": CONFIG, "source_status_path": path.name,
                      "source_status_sha256": hashlib.sha256(raw).hexdigest()})
    index_version = {1: 2, 2: 3, 3: 4, 4: 5}[version]
    index_name = f"strategy_scan_status_index.v{index_version}.json"
    index = _signed({"schema_version": f"strategy_scan_status_index.v{index_version}",
        "run_id": RUN, "account": ACCOUNT, "account_config_sha256": CONFIG,
        "published_at_utc": NOW, "expected_count": len(items), "items": items,
        "counts": {state: sum(item["status"] == state for item in items)
                   for state in ("completed", "unavailable", "failed", "not_applicable")},
        **(mode if version in (2, 4) else {})}, index=True)
    index_raw = _write(account_dir / index_name, index)
    scopes = normalize_combo_scope_results([
        {"symbol": item["symbol"], "strategy_mode": "combo_yield", "status": item["status"],
         "reason": item.get("reason")} for item in items], owner="sp_lc")
    if version == 2:
        for scope, item in zip(scopes, items):
            scope["candidate_count"] = item.get("candidate_count")
    dependencies = normalize_dependencies([
        {"kind": kind, "relpath": None, "sha256": char * 64} for kind, char in
        (("required_data", "1"), ("portfolio", "2"), ("ledger", "3"), ("fx", "4"), ("earnings_rv", "5"))])
    pair = {"candidate_pair_id": "combo_yield:AAPL:P:C", "symbol": "AAPL",
            "put_contract_symbol": "P", "call_contract_symbol": "C"}
    owner_schema = (COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA if version == 4
                    else f"combo_yield_candidate_snapshot.v{3 if version == 2 else 2}")
    owner = _signed({"schema_version": owner_schema, "run_id": RUN, "account": ACCOUNT,
        "market": "US", "candidate_owner": "sp_lc", "account_config_sha256": CONFIG,
        "strategy_policy_sha256": POLICY, "required_data_manifest_sha256": "1" * 64,
        "dependencies": dependencies, "sealed_at_utc": NOW,
        "opening_status": combo_opening_status(scopes, selected_count=int(positive)),
        "scope_results": scopes, "funding_put_decisions": [],
        "pair_evaluations": ([{**pair, "diagnostic_scope": "pair", "diagnostic_stage": "pair_filter",
                               "accepted": True, "reject_reasons": ""}] if positive else []),
        "rank_records": ([{**pair, "baseline_rank": 1, "shadow_rank": 1,
                           "baseline_selected": True, "shadow_selected": True, "rank_changed": False}] if positive else []),
        "ranked_pairs": [pair] if positive else [], **(mode if version in (2, 4) else {})})
    if version == 2:
        owner["dependencies"] = [row for row in dependencies if row["kind"] in {"required_data", "fx", "earnings_rv"}]
    if positive:
        from src.application.combo_yield_candidate_snapshot import _pairs, _pair_evaluations, _rank_records, _bind_selection_states
        owner["ranked_pairs"] = _pairs(owner["ranked_pairs"])
        owner["rank_records"] = _rank_records(owner["rank_records"])
        owner["pair_evaluations"] = _bind_selection_states(_pair_evaluations(owner["pair_evaluations"]),
            rank_records=owner["rank_records"], ranked_pairs=owner["ranked_pairs"])
    owner = _signed({key: value for key, value in owner.items() if key != "content_sha256"})
    owner_path = account_dir / "state/combo_yield_candidate_snapshot.json"
    owner_raw = _write(owner_path, owner)
    expected = _expected_scopes(index, require_wheel_direction=version == 4)
    covered = _snapshot_strategy_scopes(owner, owner="sp_lc", index_items=items, require_wheel_direction=version == 4)
    manifest = _signed({"schema_version": f"candidate_snapshot_manifest.v{version}",
        "run_id": RUN, "account": ACCOUNT, "markets": ["US"], "account_config_sha256": CONFIG,
        "strategy_policy_sha256": POLICY, "sealed_at_utc": NOW, "completion_reason": "complete",
        "expected_scopes": expected, "expected_owners": ["sp_lc"],
        "status_index": {"schema_version": index["schema_version"], "relpath": index_name,
                         "sha256": hashlib.sha256(index_raw).hexdigest(), "content_sha256": index["content_sha256"]},
        "owner_snapshots": [{"candidate_owner": "sp_lc", "schema_version": owner_schema,
            "relpath": "state/" + owner_path.name, "sha256": hashlib.sha256(owner_raw).hexdigest(),
            "content_sha256": owner["content_sha256"], "opening_status": owner["opening_status"],
            "covered_scopes": covered, **(mode if version in (2, 4) else {})}],
        **(mode if version in (2, 4) else {})})
    if version == 2:
        # Native historical experience manifests do not require expected_scopes.
        manifest = _signed({key: value for key, value in manifest.items()
                            if key not in {"content_sha256", "expected_scopes"}})
    _write(account_dir / f"state/candidate_snapshot_manifest.v{version}.json", manifest)
    return owner_path


def _metrics(root: Path, *, scanned: bool = True, pipeline: bool = False) -> None:
    _write(root / f"output_runs/{RUN}/accounts/{ACCOUNT}/state/account_metrics.json",
           {"run_id": RUN, "account": ACCOUNT, "markets_to_run": ["US"], "as_of_utc": NOW,
            "account_config_sha256": CONFIG,
            "ran_scan": scanned, "ran_pipeline": pipeline, "reason": "pipeline failed"})


def _options(root: Path) -> dict:
    return {"runtime_root": RuntimeRootResolution(root, "argument"), "run_id": RUN,
            "account": ACCOUNT, "market": "US", "authorized_accounts": [ACCOUNT]}


def _public(tool: str, config: Path) -> tuple[dict, dict]:
    payload, error = tools.build_tool_payload(tool, {"account": ACCOUNT, "run_id": RUN},
                                             fixed_input={"config_path": str(config)})
    assert error is None
    response = tools.call_read_tool(tool, payload, allowed_tools=(tool,))
    assert response["ok"], response
    return response, tools.model_observation(tool, response)


@pytest.mark.parametrize("version", [1, 2, 3, 4])
@pytest.mark.parametrize("case", [
    "failure", "success", "unknown_reason", "missing",
    "wrong_run", "wrong_account", "wrong_market", "wrong_config",
])
def test_runtime_runs_preserves_verified_account_execution(
    tmp_path, monkeypatch, example_config_path, version, case,
):
    import socket

    _bundle(tmp_path, ("completed", "failed"), version=version)
    metrics = {
        "run_id": RUN, "account": ACCOUNT, "markets_to_run": ["US"],
        "account_config_sha256": CONFIG, "as_of_utc": "2026-10-21T00:01:00Z",
        "ran_scan": False, "ran_pipeline": case == "success",
        "reason": "SYNTHETIC_DETAIL_DO_NOT_EXPOSE" if case == "unknown_reason"
        else "prepared_option_context_integrity_failed" if case != "success" else None,
    }
    conflicts = {
        "wrong_run": ("run_id", "other-run"),
        "wrong_account": ("account", "sy"),
        "wrong_market": ("markets_to_run", ["HK"]),
        "wrong_config": ("account_config_sha256", "c" * 64),
    }
    if case in conflicts:
        field, value = conflicts[case]
        metrics[field] = value
    raw_metrics = None
    if case != "missing":
        raw_metrics = _write(tmp_path / f"output_runs/{RUN}/accounts/{ACCOUNT}/state/account_metrics.json", metrics)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    forbidden_calls = []

    def forbidden_network(*args, **kwargs):
        forbidden_calls.append("network.connect")
        raise AssertionError("runtime evidence reads must not connect to a real service")

    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in tmp_path.rglob("*") if path.is_file()}
    config_before = example_config_path.read_bytes()
    snapshot = project_runs.load_run_bundle(**_options(tmp_path))["manifest"]
    assert snapshot["outcomes"] == {"usable_scan_result": True, "pipeline_completed_successfully": None}
    for explicit in (True, False):
        arguments = {"account": ACCOUNT, "scanned_only": True}
        if explicit:
            arguments["run_id"] = RUN
        payload, error = tools.build_tool_payload(
            "runtime_runs", arguments, fixed_input={"config_path": str(example_config_path)},
        )
        assert error is None
        response = tools.call_read_tool("runtime_runs", payload, allowed_tools=("runtime_runs",))
        if case in conflicts:
            if explicit:
                assert response["ok"] is False, response
            else:
                assert response["ok"] is True, response
                assert response["data"]["runs"] == []
                assert response["data"]["unverified_newer_count"] == 1
            continue
        assert response["ok"] is True, response
        observation = tools.model_observation("runtime_runs", response)
        row, = response["data"]["runs"]
        observed, = observation["data"]["runs"]
        expected_outcomes = {"usable_scan_result": True,
                             "pipeline_completed_successfully": None if case == "missing" else metrics["ran_pipeline"]}
        assert row["outcomes"] == observed["outcomes"] == expected_outcomes
        assert row["terminal"] is True and row["scanned"] is True
        assert row["as_of"] == snapshot["as_of"] == NOW
        assert row["revision"] == snapshot["revision"]
        if case == "missing":
            assert "account_execution" not in row and "account_execution" not in observed
            assert row["resource_categories"] == ["candidate_snapshot", "status_index"]
        else:
            expected_execution = {
                "source_kind": "account_metrics", "as_of": metrics["as_of_utc"],
                "revision": hashlib.sha256(raw_metrics).hexdigest(), "ran_pipeline": metrics["ran_pipeline"],
                "reason": "prepared_option_context_integrity_failed" if case == "failure" else None,
                "reason_description": "该次预备期权上下文未通过完整性校验。" if case == "failure" else None,
                "cause_available": case == "failure", "causal_link_to_other_records": "not_established",
            }
            assert row["account_execution"] == expected_execution
            assert observed["account_execution"] == {
                key: value for key, value in expected_execution.items() if key != "revision"
            }
            assert row["resource_categories"] == ["candidate_snapshot", "status_index", "account_diagnostics"]
        assert "scanned" not in observed and "terminal" not in observed
        assert "SYNTHETIC_DETAIL_DO_NOT_EXPOSE" not in json.dumps(observation)
    assert forbidden_calls == []
    assert example_config_path.read_bytes() == config_before
    assert {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("scanned_only", [False, True])
def test_runtime_runs_reads_verified_zero_scope_without_claiming_scan(
    tmp_path, monkeypatch, example_config_path, explicit, scanned_only,
):
    from test_project_runs import _hashes, _publish_zero_scope_bundle
    from src.application.tool_execution import execute_tool

    _publish_zero_scope_bundle(tmp_path, example_config_path)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    payload = {"action": "scoped", "account": "lx", "config_path": str(example_config_path),
               "scanned_only": scanned_only}
    if explicit:
        payload["run_id"] = "run-1"
    before = _hashes(tmp_path)
    response = execute_tool("runtime_runs", payload)
    assert response["ok"], response
    data = response["data"]
    assert data["unverified_newer_count"] == 0
    assert len(data["runs"]) == int(not scanned_only)
    if not scanned_only:
        row = data["runs"][0]
        assert (row["run_id"], row["account"], row["market"]) == ("run-1", "lx", "US")
        assert row["scanned"] is False
        assert row["outcomes"] == {"usable_scan_result": False, "pipeline_completed_successfully": True}
        assert row["account_execution"]["ran_pipeline"] is True
        assert row["account_execution"]["source_kind"] == "account_metrics"
        assert row["resource_categories"] == ["candidate_snapshot", "status_index", "account_diagnostics"]
    assert _hashes(tmp_path) == before


@pytest.mark.parametrize("corruption", ["metrics-account", "metrics-config", "index"])
def test_runtime_runs_does_not_salvage_invalid_zero_scope_record(
    tmp_path, monkeypatch, example_config_path, corruption,
):
    from test_project_runs import _hashes, _publish_zero_scope_bundle
    from src.application.tool_execution import execute_tool

    manifest, metrics_path = _publish_zero_scope_bundle(tmp_path, example_config_path)
    if corruption == "index":
        target = tmp_path / "output_runs/run-1/accounts/lx" / manifest["status_index"]["relpath"]
        target.write_bytes(target.read_bytes() + b" ")
    else:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        metrics["account" if corruption == "metrics-account" else "account_config_sha256"] = "sy"
        metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    payload = {"action": "scoped", "account": "lx", "config_path": str(example_config_path)}
    before = _hashes(tmp_path)
    direct = execute_tool("runtime_runs", {**payload, "run_id": "run-1"})
    assert direct["ok"] is False
    listed = execute_tool("runtime_runs", payload)
    assert listed["ok"] and listed["data"]["runs"] == []
    assert listed["data"]["unverified_newer_count"] == 1
    assert _hashes(tmp_path) == before


@pytest.mark.parametrize("scanned,pipeline", [(True, False), (False, False), (True, True)])
def test_metrics_attempt_or_pipeline_is_not_result_evidence(tmp_path, monkeypatch, example_config_path, scanned, pipeline):
    _metrics(tmp_path, scanned=scanned, pipeline=pipeline)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    from src.application.tool_execution import execute_tool
    filtered = execute_tool("runtime_runs", {"action": "scoped", "run_id": RUN, "account": ACCOUNT,
                            "scanned_only": True, "config_path": str(example_config_path)})
    assert filtered["ok"] and len(filtered["data"]["runs"]) == int(scanned)
    metrics = project_runs.load_run_metrics(**_options(tmp_path))
    assert metrics["scanned"] is scanned and metrics["ran_pipeline"] is pipeline
    for tool in ("runtime_runs", "runtime_logs"):
        _, observation = _public(tool, example_config_path)
        key = "runs" if tool == "runtime_runs" else "diagnostics"
        row = observation["data"][key][0]
        assert row["outcomes"] == {"usable_scan_result": None, "pipeline_completed_successfully": pipeline}
        if tool == "runtime_logs":
            assert row["usable_scan_result_source"] is None


@pytest.mark.parametrize("version", [1, 2, 3, 4])
@pytest.mark.parametrize("states,expected", [
    (("completed",), True), (("completed", "failed"), True), (("failed",), False),
    (("unavailable",), False), (("not_applicable",), False), (("failed", "not_applicable"), False),
])
def test_verified_scopes_decide_availability(tmp_path, monkeypatch, example_config_path, version, states, expected):
    _bundle(tmp_path, states, version=version)
    _metrics(tmp_path, scanned=False)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*.json")}
    bundle = project_runs.load_run_bundle(**_options(tmp_path))
    assert bundle["manifest"]["scanned"] is True
    assert bundle["manifest"]["outcomes"] == {"usable_scan_result": expected, "pipeline_completed_successfully": None}
    response, run_observation = _public("runtime_runs", example_config_path)
    assert response["data"]["runs"][0]["scanned"] is True
    from src.application.tool_execution import execute_tool
    filtered = execute_tool("runtime_runs", {"action": "scoped", "run_id": RUN, "account": ACCOUNT,
                            "scanned_only": True, "config_path": str(example_config_path)})
    assert filtered["ok"] and len(filtered["data"]["runs"]) == 1
    assert run_observation["data"]["runs"][0]["outcomes"] == {
        "usable_scan_result": expected, "pipeline_completed_successfully": False,
    }
    assert bundle["manifest"]["outcomes"]["pipeline_completed_successfully"] is None
    assert run_observation["data"]["runs"][0]["account_execution"]["source_kind"] == "account_metrics"
    _, log_observation = _public("runtime_logs", example_config_path)
    row = log_observation["data"]["diagnostics"][0]
    assert row["outcomes"] == {"usable_scan_result": expected, "pipeline_completed_successfully": False}
    assert row["source_kind"] == row["record_scope"] == "account_metrics"
    assert row["usable_scan_result_source"] == "validated_candidate_bundle"
    assert row["causal_link_to_other_records"] == "not_established"
    assert log_observation["data"]["record_relationship"] == "independent_records_no_causal_or_sequence_link"
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*.json")}


def test_positive_bundle_without_metrics_and_legacy_bot_fallback(tmp_path):
    _bundle(tmp_path, ("completed",), positive=True)
    record = project_runs.load_run_record(**_options(tmp_path))
    assert record["outcomes"] == {"usable_scan_result": True, "pipeline_completed_successfully": None}
    assert "account_execution" not in record
    legacy = tools.model_observation("runtime_runs", {"ok": True, "data": {"runs": [{"scanned": True, "ran_pipeline": False}]}})
    assert legacy["data"]["runs"][0]["outcomes"] == {"usable_scan_result": None, "pipeline_completed_successfully": False}


def test_present_corrupt_bundle_cannot_fall_back_to_metrics(tmp_path):
    owner = _bundle(tmp_path, ("completed",))
    _metrics(tmp_path)
    with owner.open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ProjectReaderError, match="bundle_invalid"):
        project_runs.load_run_record(**_options(tmp_path))
    with pytest.raises(ProjectReaderError, match="bundle_invalid"):
        project_runs.load_run_diagnostics(runtime_root=_options(tmp_path)["runtime_root"],
            scope={"market": "US", "accounts": [ACCOUNT]}, account=ACCOUNT, run_id=RUN)


def test_public_scope_cancellation_budgets_and_recovery(tmp_path, monkeypatch, example_config_path):
    import time

    _bundle(tmp_path, ("completed", "failed"))
    _metrics(tmp_path)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    for tool in ("runtime_runs", "runtime_logs"):
        payload, error = tools.build_tool_payload(tool, {"account": ACCOUNT, "run_id": RUN},
                                                 fixed_input={"config_path": str(example_config_path)})
        assert error is None
        for kwargs, code in (({"cancelled": lambda: True}, "CANCELLED"),
                             ({"deadline_monotonic": time.monotonic() - 1}, "BUDGET_EXHAUSTED")):
            response = tools.call_read_tool(tool, payload, allowed_tools=(tool,), **kwargs)
            assert not response["ok"]
            # Runs keeps its historical READ_ERROR envelope; logs has typed codes.
            assert response["error"]["code"] == ("READ_ERROR" if tool == "runtime_runs" else code)
            if tool == "runtime_runs":
                assert response["error"]["message"] == ("cancelled" if code == "CANCELLED" else "time_deadline")
        from src.application.tool_execution import execute_tool
        unauthorized = execute_tool(tool, {**payload, "account": "unauthorized802"})
        assert not unauthorized["ok"]
        assert unauthorized["error"]["code"] == "PERMISSION_DENIED"
        _public(tool, example_config_path)
    for change, error in (({"max_bytes": 1}, "file_too_large"),
                          ({"_budget": {"bytes": 10000, "metadata": 0}}, "scope_too_broad"),
                          ({"market": "HK"}, "permission_denied")):
        with pytest.raises(ProjectReaderError, match=error):
            project_runs.load_run_bundle(**{**_options(tmp_path), **change})
    assert project_runs.load_run_bundle(**_options(tmp_path))["manifest"]["outcomes"]["usable_scan_result"] is True


def test_diagnostic_cursor_binds_bundle_and_preserves_rows(tmp_path, monkeypatch, example_config_path):
    _bundle(tmp_path, ("completed",))
    _metrics(tmp_path)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("OM_INBOUND_OPERATION_HMAC_KEY", "synthetic-cursor-802")
    audit = tmp_path / f"output_runs/{RUN}/state/audit_events.jsonl"
    from domain.storage.repositories.state_repo import normalize_audit_event
    row = normalize_audit_event({"event_type": "tool_call", "action": "run_pipeline_result",
        "run_id": RUN, "account": ACCOUNT, "status": "error", "tool_name": "run_pipeline",
        "extra": {"failure_stage": "run_pipeline", "failure_kind": "io_error"}})
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("x") as stream:
        stream.write(json.dumps(row) + "\n")
    payload, error = tools.build_tool_payload("runtime_logs", {"account": ACCOUNT, "run_id": RUN, "limit": 1},
                                              fixed_input={"config_path": str(example_config_path)})
    assert error is None
    first = tools.call_read_tool("runtime_logs", payload, allowed_tools=("runtime_logs",))
    assert first["ok"] and first["data"]["next_cursor"], first
    second = tools.call_read_tool("runtime_logs", {**payload, "cursor": first["data"]["next_cursor"]}, allowed_tools=("runtime_logs",))
    assert second["ok"] and not second["data"]["next_cursor"]
    rows = first["data"]["diagnostics"] + second["data"]["diagnostics"]
    assert {item["source_kind"] for item in rows} == {"account_metrics", "run_audit"}
    assert first["data"]["pagination"]["matched_count"] == 2
    manifest = tmp_path / f"output_runs/{RUN}/accounts/{ACCOUNT}/state/candidate_snapshot_manifest.v4.json"
    # Change a valid manifest revision; the cursor must not mix old and new facts.
    data = json.loads(manifest.read_bytes())
    data["sealed_at_utc"] = "2026-10-21T00:00:01Z"
    updated = _signed({key: value for key, value in data.items() if key != "content_sha256"})
    manifest.write_text(json.dumps(updated))
    changed = tools.call_read_tool("runtime_logs", {**payload, "cursor": first["data"]["next_cursor"]}, allowed_tools=("runtime_logs",))
    assert not changed["ok"] and changed["error"]["message"] == "source_changed"


@pytest.mark.parametrize("change,code", [
    ({"account": "other802"}, "scope_conflict"),
    ({"run_id": "different-run"}, "scope_conflict"),
    ({"markets_to_run": ["HK"]}, "market_unverifiable"),
    ({"markets_to_run": ["US", "HK"]}, "market_unverifiable"),
])
def test_metrics_identity_conflicts_still_fail_closed(tmp_path, monkeypatch, example_config_path, change, code):
    _metrics(tmp_path)
    path = tmp_path / f"output_runs/{RUN}/accounts/{ACCOUNT}/state/account_metrics.json"
    path.write_text(json.dumps({**json.loads(path.read_bytes()), **change}))
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    for tool in ("runtime_runs", "runtime_logs"):
        payload, error = tools.build_tool_payload(tool, {"account": ACCOUNT, "run_id": RUN},
                                                 fixed_input={"config_path": str(example_config_path)})
        assert error is None
        response = tools.call_read_tool(tool, payload, allowed_tools=(tool,))
        assert not response["ok"] and response["error"]["message"] == code
        assert not response.get("data")
