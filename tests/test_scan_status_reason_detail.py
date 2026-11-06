from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.application.agent_tools.candidate_filter_impl import candidate_filter_explain_tool
from src.application.candidate_snapshot_contract import candidate_run_mode_fields
from src.application.candidate_snapshot_manifest import (
    CandidateSnapshotManifestError,
    load_candidate_snapshot_bundle,
    publish_candidate_snapshot_manifest,
)
from src.application.pipeline_watchlist import _index_owner_statuses
from src.application.required_data_snapshot import FrozenRequiredDataBatch, FrozenRequiredDataUnavailable
from src.application.source_receipts import SourceReceiptError, sha256_bytes
from src.application.strategy_scan_status import (
    STRATEGY_SCAN_STATUS_INDEX_V5_FILE,
    StrategyScanStatusError,
    load_strategy_scan_status_index,
    publish_strategy_scan_status,
    publish_strategy_scan_status_index,
)
from tests.test_opening_candidate_snapshot import _seal_snapshot
from tests.test_symbol_monitoring_fetch_spec_merge import _inputs, _prefilters_fn


DETAIL = 'invalid_row_identity: requested expiration has no lower_bound; token="private-token"'
CONFIG = {
    "portfolio": {"account": "lx"},
    "symbols": [{
        "symbol": "NVDA", "sell_put": {"enabled": True},
        "sell_call": {"enabled": False}, "combo_yield": {"enabled": False},
    }],
}


def _expected(symbol="NVDA", config_hash="a" * 64):
    return [{
        "market": "US", "symbol": symbol, "strategy_family": "sell_put",
        "strategy_mode": "put", "candidate_owner": "opening",
        "account_config_sha256": config_hash,
    }]


def _explain(base, run_id=None, **payload):
    return candidate_filter_explain_tool(
        {"account": "lx", "symbol": "NVDA", **({"run_id": run_id} if run_id else {}), **payload},
        repo_base=lambda: base, mask_path=lambda path: str(path) if path else None,
    )[0]


def _group(result, function="sell_put"):
    return next(row for row in result["functions"] if row["function"] == function)


def _events(result):
    return [event for group in result["functions"] for event in group["events"]]


def _forbid_jsonl_reads(monkeypatch):
    reads = []
    original = Path.open

    def guarded(path, *args, **kwargs):
        if path.suffix == ".jsonl":
            reads.append(path)
            raise AssertionError("explain must not read JSONL")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    return reads


@pytest.mark.parametrize("detail", [None, "", "   ", 23])
def test_optional_missing_or_nontext_detail_keeps_old_payload(tmp_path, detail):
    row = publish_strategy_scan_status(
        report_dir=tmp_path, run_id="run-1", account="lx", market="US",
        symbol="NVDA", strategy_family="sell_put", status="completed",
        candidate_count=0, reason="no_candidate", detail=detail,
    )
    assert row["status"] == "completed"
    assert "detail" not in row


def test_detail_is_redacted_bounded_hashed_and_does_not_change_completion(tmp_path):
    row = publish_strategy_scan_status(
        report_dir=tmp_path, run_id="run-1", account="lx", market="US",
        symbol="NVDA", strategy_family="sell_put", status="completed",
        candidate_count=1,
        detail=DETAIL + "\n/Users/example/private/quotes.csv\x00\x1b " + "x" * 4000,
    )
    assert row["status"] == "completed" and row["candidate_count"] == 1
    assert len(row["detail"]) == 2048
    assert "private-token" not in row["detail"]
    assert "/Users/" not in row["detail"]
    assert "\n" not in row["detail"] and "\x00" not in row["detail"] and "\x1b" not in row["detail"]
    index = publish_strategy_scan_status_index(
        report_dir=tmp_path, run_id="run-1", account="lx", account_config_sha256="a" * 64,
        expected=_expected(), run_mode=candidate_run_mode_fields(experience=False),
    )
    assert index["items"][0]["detail"] == row["detail"]
    # Repeat publication with absent/changed diagnostics retains the original
    # immutable status and must not fail an already completed strategy.
    repeated = publish_strategy_scan_status(
        report_dir=tmp_path, run_id="run-1", account="lx", market="US",
        symbol="NVDA", strategy_family="sell_put", status="completed",
        candidate_count=1, detail="different optional diagnostic",
    )
    assert repeated == row
    status_path = Path(row["status_path"])
    stored = json.loads(status_path.read_text())
    stored["detail"] += "tampered"
    status_path.write_text(json.dumps(stored))
    with pytest.raises(StrategyScanStatusError, match="hash mismatch"):
        publish_strategy_scan_status_index(
            report_dir=tmp_path, run_id="run-1", account="lx", account_config_sha256="a" * 64,
            expected=_expected(), run_mode=candidate_run_mode_fields(experience=False),
        )


@pytest.mark.parametrize("detail", [None, DETAIL])
def test_tick_failure_detail_reaches_manifest_bound_explain(tmp_path, monkeypatch, detail):
    from src.application import symbol_monitoring as mod

    run_id = "tick-detail"
    report_dir = tmp_path / "output_runs" / run_id / "accounts" / "lx"
    batch = FrozenRequiredDataBatch(
        manifest={}, manifest_bytes=b"{}", entries={}, unavailable={
            "NVDA": FrozenRequiredDataUnavailable(
                symbol="NVDA", reason="invalid_row_identity", detail=detail,
            ),
        },
    )
    forbidden_calls = []

    def no_fetch_or_scan(**kwargs):
        forbidden_calls.append(kwargs)
        raise AssertionError("frozen failure must not fetch or scan")

    deps = mod.SymbolMonitoringDependencies(
        build_converter_fn=lambda **_: None, apply_prefilters_fn=_prefilters_fn,
        apply_multiplier_cache_fn=no_fetch_or_scan, ensure_required_data_fn=no_fetch_or_scan,
        run_sell_put_scan_fn=no_fetch_or_scan, run_sell_call_scan_fn=no_fetch_or_scan,
        run_combo_yield_scan_fn=no_fetch_or_scan,
        empty_sell_put_summary_fn=lambda symbol, **_: {"symbol": symbol, "candidate_count": 0},
        empty_sell_call_summary_fn=lambda symbol, **_: {},
        empty_combo_yield_summary_fn=lambda symbol, **_: {},
    )
    mod.run_symbol_monitoring(inputs=_inputs(
        tmp_path, symbol_cfg=deepcopy(CONFIG["symbols"][0]), runtime_config=CONFIG,
        report_dir=report_dir, source_producer_run_id=run_id,
        required_data_snapshot_manifest=tmp_path / "manifest.json",
        required_data_snapshot_batch=batch,
    ), deps=deps)
    assert forbidden_calls == []
    index = publish_strategy_scan_status_index(
        report_dir=report_dir, run_id=run_id, account="lx", account_config_sha256="a" * 64,
        expected=_expected(), run_mode=candidate_run_mode_fields(experience=False),
    )
    statuses = _index_owner_statuses(index)["opening"]
    _seal_snapshot(
        tmp_path, run_id=run_id, scan_statuses=statuses,
        final_candidates={"put": [], "call": []},
    )
    publish_candidate_snapshot_manifest(
        base=tmp_path, run_id=run_id, account="lx", strategy_policy_sha256="b" * 64,
    )
    reads = _forbid_jsonl_reads(monkeypatch)
    result = _explain(tmp_path, run_id)
    event = _events(result)[0]
    assert event["rule"] == "required_data_snapshot_unavailable"
    if detail:
        assert event["message"].startswith("invalid_row_identity")
    else:
        assert event["message"] == event["rule"]
    assert "private-token" not in event["message"]
    example = result["summary"][0]["examples"][0]
    if detail:
        assert example["message"] == event["message"]
    else:
        assert "message" not in example
    assert _group(result)["status"] == "unavailable"
    assert "candidate_evidence_available" not in result
    assert load_candidate_snapshot_bundle(
        base=tmp_path, run_id=run_id, account="lx",
    )["manifest"]["run_id"] == run_id
    assert reads == []


def _patch_direct_process(monkeypatch, *, error=SourceReceiptError(DETAIL), completed=False):
    from src.application import pipeline_symbol, symbol_monitoring

    monkeypatch.setattr(symbol_monitoring, "build_required_data_fetch_plan", lambda **_: None)
    monkeypatch.setattr(pipeline_symbol, "build_converter", lambda **_: None)
    monkeypatch.setattr(pipeline_symbol, "apply_prefilters", _prefilters_fn)
    monkeypatch.setattr(pipeline_symbol, "apply_multiplier_cache_to_required_data_csv", lambda **_: None)

    def fetch(**_):
        if error is not None:
            raise error
        return None

    monkeypatch.setattr(pipeline_symbol, "ensure_required_data", fetch)
    monkeypatch.setattr(pipeline_symbol, "empty_sell_put_summary", lambda symbol, **_: {
        "symbol": symbol, "strategy": "sell_put", "candidate_count": 0,
    })
    if completed:
        def scan(**kwargs):
            kwargs["final_candidates_sink_fn"]("put", [{"symbol": "NVDA"}])
            return {"symbol": "NVDA", "strategy": "sell_put", "candidate_count": 1,
                    "_strategy_status": "completed", "_strategy_reason": None}
        monkeypatch.setattr(pipeline_symbol, "run_sell_put_scan_and_summarize", scan)
    return pipeline_symbol.process_symbol


def test_direct_process_symbol_publishes_failure_and_latest_explain(tmp_path, monkeypatch):
    process_symbol = _patch_direct_process(monkeypatch)
    process_symbol("python", tmp_path, deepcopy(CONFIG["symbols"][0]), 3, runtime_config=CONFIG)
    indexes = list((tmp_path / "output_runs").glob("*/accounts/lx/strategy_scan_status_index.v5.json"))
    assert len(indexes) == 1
    index = load_strategy_scan_status_index(indexes[0], allow_status_only=True)
    assert index["evidence_authority"] == "scan_status_only"
    assert index["items"][0]["status"] == "unavailable"
    with pytest.raises(StrategyScanStatusError, match="no candidate authority"):
        load_strategy_scan_status_index(indexes[0])
    with pytest.raises(CandidateSnapshotManifestError):
        load_candidate_snapshot_bundle(base=tmp_path, run_id=index["run_id"], account="lx")
    reads = _forbid_jsonl_reads(monkeypatch)
    result = _explain(tmp_path)
    assert result["scope"]["run_id"] == index["run_id"]
    assert result["authority"] == "scan_status_only"
    assert result["candidate_evidence_available"] is False
    assert result["source"]["candidate_evidence_available"] is False
    assert result["summary"][0]["examples"][0]["message"].startswith("invalid_row_identity")
    assert _events(result)[0]["message"].startswith("invalid_row_identity")
    assert "private-token" not in _events(result)[0]["message"]
    assert _events(result)[0]["evidence_path"] == STRATEGY_SCAN_STATUS_INDEX_V5_FILE
    assert not any(event["rule"] == "candidate_accepted" for event in _events(result))
    from src.application.tool_execution import execute_tool
    public = execute_tool("candidate_filter_explain", {
        "runtime_root": str(tmp_path), "account": "lx", "symbol": "NVDA",
    })
    assert public["ok"] is True
    assert _events(public["data"])[0]["message"].startswith("invalid_row_identity")
    assert public["data"]["summary"][0]["examples"][0]["message"].startswith("invalid_row_identity")
    assert public["data"]["source"]["candidate_evidence_available"] is False
    assert reads == []


def test_direct_success_diagnostics_do_not_create_accepted_candidates(tmp_path, monkeypatch):
    process_symbol = _patch_direct_process(monkeypatch, error=None, completed=True)
    process_symbol("python", tmp_path, deepcopy(CONFIG["symbols"][0]), 3, runtime_config=CONFIG)
    result = _explain(tmp_path)
    assert result["authority"] == "scan_status_only"
    assert result["candidate_evidence_available"] is False
    assert not any(event["rule"] == "candidate_accepted" for event in _events(result))
    assert _group(result)["status"] == "completed"


def test_symbols_pipeline_uses_one_diagnostic_run_and_only_selected_symbols(tmp_path, monkeypatch):
    from src.application import pipeline_context, pipeline_watchlist, report_builders

    _patch_direct_process(monkeypatch)
    monkeypatch.setattr(pipeline_context, "build_pipeline_context", lambda **_: ({}, {}, None, None))
    monkeypatch.setattr(pipeline_watchlist, "resolve_watchlist_item_runtime_config",
                        lambda *, item, **_: dict(item))
    monkeypatch.setattr(report_builders, "build_symbols_summary", lambda *_, **__: None)
    monkeypatch.setattr(report_builders, "build_symbols_digest", lambda *_, **__: None)
    cfg = deepcopy(CONFIG)
    cfg["symbols"].extend([
        {**deepcopy(CONFIG["symbols"][0]), "symbol": symbol} for symbol in ("AMD", "MSFT")
    ])
    pipeline_watchlist.run_watchlist_pipeline_default(
        py="python", base=tmp_path, cfg=cfg, report_dir=tmp_path / "reports",
        state_dir=tmp_path / "state", shared_state_dir=None,
        required_data_dir=tmp_path / "required", is_scheduled=False, top_n=3,
        symbol_timeout_sec=10, portfolio_timeout_sec=10, want_scan=True, no_context=True,
        symbols_arg="NVDA,AMD", log=lambda _: None, want_fn=lambda name: name == "scan",
    )
    indexes = list((tmp_path / "output_runs").glob("*/accounts/lx/strategy_scan_status_index.v5.json"))
    assert len(indexes) == 1
    index = load_strategy_scan_status_index(indexes[0], allow_status_only=True)
    assert {row["symbol"] for row in index["items"]} == {"NVDA", "AMD"}
    assert _events(_explain(tmp_path))[0]["message"].startswith("invalid_row_identity")


def test_absent_detail_explain_message_remains_reason_code(tmp_path):
    from tests.candidate_evidence_helpers import seal_opening_candidate_fixture

    seal_opening_candidate_fixture(base=tmp_path, run_id="old", account="lx")
    result = _explain(tmp_path, "old")
    assert _events(result)
    assert all(event["message"] == event["rule"] for event in _events(result))


def test_latest_direct_scan_supersedes_scheduler_pointer_without_moving_it(tmp_path, monkeypatch):
    from tests.candidate_evidence_helpers import seal_opening_candidate_fixture

    seal_opening_candidate_fixture(base=tmp_path, run_id="old", account="lx")
    pointer = tmp_path / "output_shared" / "state" / "last_run_dir.txt"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(str(tmp_path / "output_runs" / "old"))
    old_pointer = pointer.read_bytes()
    process_symbol = _patch_direct_process(monkeypatch)
    process_symbol("python", tmp_path, deepcopy(CONFIG["symbols"][0]), 3, runtime_config=CONFIG)
    result = _explain(tmp_path)
    assert result["scope"]["run_id"] != "old"
    assert result["authority"] == "scan_status_only"
    assert pointer.read_bytes() == old_pointer


def test_direct_real_fetch_adapter_failure_reaches_explain(tmp_path, monkeypatch):
    from src.application import pipeline_symbol, required_data_steps

    _patch_direct_process(monkeypatch)
    monkeypatch.setattr(pipeline_symbol, "ensure_required_data", required_data_steps.ensure_required_data)
    calls = []

    def failed_external_client(*, base, request):
        calls.append((base, request))
        raise SourceReceiptError(DETAIL)

    monkeypatch.setattr(required_data_steps, "execute_required_data_opend", failed_external_client)
    pipeline_symbol.process_symbol(
        "python", tmp_path, deepcopy(CONFIG["symbols"][0]), 3, runtime_config=CONFIG,
    )
    assert len(calls) == 1
    assert calls[0][0] == tmp_path
    result = _explain(tmp_path)
    assert _events(result)[0]["message"].startswith("invalid_row_identity")
    assert "private-token" not in _events(result)[0]["message"]
    assert result["candidate_evidence_available"] is False


def test_status_only_index_cannot_mask_a_missing_or_invalid_terminal_manifest(tmp_path, monkeypatch):
    process_symbol = _patch_direct_process(monkeypatch)
    process_symbol("python", tmp_path, deepcopy(CONFIG["symbols"][0]), 3, runtime_config=CONFIG)
    result = _explain(tmp_path)
    run_id = result["scope"]["run_id"]
    account_dir = tmp_path / "output_runs" / run_id / "accounts" / "lx"
    # A declared current terminal manifest must always pass its own validation.
    state = account_dir / "state"
    state.mkdir()
    (state / "candidate_snapshot_manifest.v4.json").write_text("{}")
    from src.application.agent_tool_contracts import AgentToolError
    with pytest.raises(AgentToolError):
        _explain(tmp_path, run_id)
    (state / "candidate_snapshot_manifest.v4.json").unlink()
    index_path = account_dir / STRATEGY_SCAN_STATUS_INDEX_V5_FILE
    index = json.loads(index_path.read_text())
    index.pop("evidence_authority")
    content = {key: value for key, value in index.items() if key != "content_sha256"}
    index["content_sha256"] = sha256_bytes(json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode())
    index_path.write_text(json.dumps(index))
    with pytest.raises(AgentToolError):
        _explain(tmp_path, run_id)
