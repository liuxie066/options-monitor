from __future__ import annotations

import hashlib
import json
import shutil

import pytest

from src.application.agent_tools import project_reader
from src.application.tool_execution import execute_tool
from tests.candidate_evidence_helpers import seal_opening_candidate_fixture
from tests.test_notification_perception_read_tool import _row


@pytest.fixture(autouse=True)
def signed_pages(monkeypatch):
    monkeypatch.setattr(project_reader, "_key", lambda: "isolated-test-cursor-key")


def test_filter_rule_counts_match_filtered_page(tmp_path):
    seal_opening_candidate_fixture(tmp_path, run_id="run", rejected_rows=[
        {"symbol": "NVDA", "contract_symbol": f"NVDA-{i}", "rule": "risk_spread" if i < 25 else "risk_open_interest"}
        for i in range(27)])
    response = execute_tool("candidate_filter_explain", {"runtime_root": str(tmp_path),
        "account": "lx", "symbol": "NVDA", "rule": "risk_open_interest"})
    assert response["ok"], response
    assert response["data"]["pagination"]["total_count"] == 28
    assert response["data"]["pagination"]["matched_count"] == 2
    assert response["data"]["coverage"]["total_count"] == 2


def test_notification_bounded_source_and_unavailable_signing(tmp_path, monkeypatch):
    path = tmp_path / "output_shared/state/audit_events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(_row(f"run-{i}", "notification_prepared", "chat")) + "\n" for i in range(25)))
    def no_key():
        raise project_reader.ProjectReaderError("continuation_unavailable")
    monkeypatch.setattr(project_reader, "_key", no_key)
    response = execute_tool("notification_perception_read", {"runtime_root": str(tmp_path), "limit": 1})
    assert response["ok"], response
    assert response["data"]["next_cursor"] is None
    assert response["data"]["continuation_status"] == "continuation_unavailable"
    assert response["data"]["coverage"]["status"] == "partial"
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    response = execute_tool("notification_perception_read", {"runtime_root": str(tmp_path)})
    assert response["data"]["summary"]["status"] == "failed"
    assert response["data"]["coverage"]["status"] == "unknown"
    assert response["data"]["pagination"]["matched_count"] is None
    assert response["data"]["read_statuses"][0]["reason"] == "file_too_large"


def test_notification_actual_symlink_rejected(tmp_path):
    path = tmp_path / "output_shared/state/audit_events.jsonl"
    path.parent.mkdir(parents=True)
    target = tmp_path / "private.jsonl"
    target.write_text(json.dumps(_row("private", "notification_prepared", "chat")))
    path.symlink_to(target)
    response = execute_tool("notification_perception_read", {"runtime_root": str(tmp_path)})
    assert response["data"]["events"] == []
    assert response["data"]["read_statuses"][0]["reason"] == "permission_denied"


@pytest.mark.parametrize("tool", ["candidate_filter_explain", "candidate_rank_explain"])
def test_candidate_config_market_is_enforced(tool, monkeypatch, tmp_path):
    import src.application.agent_tools.candidate as candidate
    seal_opening_candidate_fixture(tmp_path, run_id="hk-run", market="HK", rejected_rows=[
        {"symbol": "0700.HK", "contract_symbol": "HK-PUT", "rule": "risk_spread"}])
    monkeypatch.setattr(candidate, "load_runtime_config", lambda **kw: (tmp_path / "config.us.json", {"accounts": [{"id": "lx"}]}))
    monkeypatch.setattr(candidate, "accounts_from_config", lambda cfg, **kw: ["lx"])
    monkeypatch.setattr(candidate, "infer_runtime_config_market", lambda **kw: "us")
    response = execute_tool(tool, {"runtime_root": str(tmp_path), "account": "lx", "config_key": "us",
        "run_id": "hk-run", **({"symbol": "0700.HK"} if tool == "candidate_filter_explain" else {})})
    assert response["ok"] is False
    assert response["error"]["code"] == "PERMISSION_DENIED"


def test_notification_conflicting_conversation_reference_is_not_visible(tmp_path):
    from src.application.conversation_scope import conversation_reference
    path = tmp_path / "output_shared/state/audit_events.jsonl"
    path.parent.mkdir(parents=True)
    row = _row("conflicting-run", "notification_prepared", "chat-a")
    row["extra"]["conversation_scope"]["conversation_ref"] = conversation_reference("chat-b")
    path.write_text(json.dumps(row) + "\n")
    response = execute_tool("notification_perception_read", {"runtime_root": str(tmp_path),
        "authenticated_conversation_id": "chat-a"})
    assert response["data"]["events"] == []


@pytest.mark.parametrize("counts,top_n", [((3, 3), 1), ((1, 3), 2), ((3, 1), 2),
                                            ((0, 5), 2), ((5, 0), 2), ((0, 0), 2)])
@pytest.mark.parametrize("limit", [None, 4])
def test_rank_legacy_all_modes_retains_per_mode_top_n(tmp_path, counts, top_n, limit):
    modes = ("put", "call")
    expected = {mode: [f"{mode}-{i}" for i in range(count)] for mode, count in zip(modes, counts)}
    seal_opening_candidate_fixture(tmp_path, run_id="run", accepted_rows=[
        {"symbol": "NVDA", "contract_symbol": contract, "mode": mode}
        for mode in modes for contract in expected[mode]])
    query = {"runtime_root": str(tmp_path), "account": "lx", "top_n": top_n}
    if limit is not None:
        query["limit"] = limit
    seen = {mode: [] for mode in modes}
    cursors = set()
    for page_number in range(sum(counts) + 1):
        response = execute_tool("candidate_rank_explain", query)
        assert response["ok"], response
        data = response["data"]
        page_counts = [len(group["ranked"]) for group in data["groups"]]
        if limit is None:
            assert all(count <= top_n for count in page_counts)
            if page_number == 0:
                assert page_counts == [min(count, top_n) for count in counts]
        else:
            remaining = sum(counts) - sum(len(rows) for rows in seen.values())
            assert sum(page_counts) == min(limit, remaining)
        for group in data["groups"]:
            seen[group["mode"]].extend(row["contract_symbol"] for row in group["ranked"])
        cursor = data["next_cursor"]
        if cursor is None:
            break
        assert cursor not in cursors
        cursors.add(cursor)
        query["cursor"] = cursor
    else:
        pytest.fail("candidate pagination did not finish")
    assert seen == expected


@pytest.mark.parametrize("tool", ["candidate_filter_explain", "candidate_rank_explain"])
def test_candidate_pages_report_unavailable_signer(tool, monkeypatch, tmp_path):
    rows = [{"symbol": "NVDA", "contract_symbol": f"NVDA-{i}", "rule": "risk_spread"} for i in range(3)]
    seal_opening_candidate_fixture(tmp_path, run_id="run", **{
        "rejected_rows" if tool == "candidate_filter_explain" else "accepted_rows": rows})
    def unavailable():
        raise project_reader.ProjectReaderError("continuation_unavailable")
    monkeypatch.setattr(project_reader, "_key", unavailable)
    response = execute_tool(tool, {"runtime_root": str(tmp_path), "account": "lx", "limit": 1,
        **({"symbol": "NVDA"} if tool == "candidate_filter_explain" else {})})
    assert response["ok"], response
    assert response["data"]["next_cursor"] is None
    assert response["data"]["continuation_status"] == "continuation_unavailable"
    assert response["data"]["coverage"]["status"] == "partial"


@pytest.mark.parametrize("tool", ["candidate_filter_explain", "candidate_rank_explain"])
def test_candidate_bot_payload_reads_actual_trusted_config(tool, example_config_path, tmp_path):
    from src.application.bot import tools
    account = json.loads(example_config_path.read_text())["accounts"][0]
    seal_opening_candidate_fixture(tmp_path, run_id="run", account=account, rejected_rows=[
        {"symbol": "NVDA", "rule": "risk_spread"}])
    arguments = {"account": account, "run_id": "run"}
    if tool == "candidate_filter_explain":
        arguments["symbol"] = "NVDA"
    payload, error = tools.build_tool_payload(tool, arguments,
        fixed_input={"config_path": str(example_config_path)})
    assert error is None
    payload["runtime_root"] = str(tmp_path)
    response = tools.call_read_tool(tool, payload, allowed_tools=(tool,))
    assert response["ok"], response
    assert response["data"]["scope"]["market"] == "US"


@pytest.mark.parametrize("stop", ["deadline", "cancelled", "cancel_during_read"])
def test_notification_host_budget_stops_real_file_read_and_context_recovers(tmp_path, monkeypatch, stop):
    import os
    import threading
    import time

    from src.application.bot.tools import call_read_tool

    path = tmp_path / "output_shared/state/audit_events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_row("visible-run", "notification_prepared", "chat")) + "\n")
    payload = {"runtime_root": str(tmp_path), "authenticated_conversation_id": "chat"}
    tool = "notification_perception_read"
    cancelled = threading.Event()
    expected_code = "CANCELLED"
    kwargs = {"cancelled": cancelled.is_set}
    if stop == "deadline":
        kwargs = {"deadline_monotonic": time.monotonic() - 1}
        expected_code = "BUDGET_EXHAUSTED"
    elif stop == "cancelled":
        cancelled.set()
    else:
        original_read = os.read

        def cancel_after_read(fd, size):
            result = original_read(fd, size)
            cancelled.set()
            return result

        monkeypatch.setattr(os, "read", cancel_after_read)
    failed = call_read_tool(tool, payload, allowed_tools=(tool,), **kwargs)
    assert failed["ok"] is False, failed
    assert failed["error"]["code"] == expected_code
    # The next Host call must not inherit the previous request's context.
    recovered = call_read_tool(tool, payload, allowed_tools=(tool,))
    assert recovered["ok"], recovered
    assert [event["run_id"] for event in recovered["data"]["events"]] == ["visible-run"]


def test_notification_relative_audit_path_preserves_leaf_nofollow(tmp_path):
    from src.application.notification_perception_read import read_notification_perception_events

    target = tmp_path / "private.jsonl"
    target.write_text(json.dumps(_row("private-target", "notification_prepared", "chat")) + "\n")
    link = tmp_path / "relative.jsonl"
    link.symlink_to(target)
    response = read_notification_perception_events(repo_root=tmp_path, audit_path="relative.jsonl")
    assert response["events"] == []
    assert response["coverage"]["status"] == "unknown"
    assert response["read_statuses"][0]["reason"] == "permission_denied"
    assert response["read_statuses"][0]["path"] == "relative.jsonl"
    assert "private-target" not in json.dumps(response)
    assert "private.jsonl" not in json.dumps(response)
    link.unlink()
    link.write_text(json.dumps(_row("regular", "notification_prepared", "chat")) + "\n")
    recovered = read_notification_perception_events(repo_root=tmp_path, audit_path="relative.jsonl")
    assert [event["run_id"] for event in recovered["events"]] == ["regular"]
