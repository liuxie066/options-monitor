from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import code_entropy_assessment as assessment
from scripts import code_entropy_review as review
from scripts import code_entropy_scan as scan


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def complex_source(name="route"):
    return f"def {name}(value):\n" + "".join(f"    if value == {n}:\n        return {n}\n" for n in range(12)) + "    return None\n"


def complexity_rows(root, count=1):
    paths = []
    for index in range(count):
        path = f"src/route{index}.py"
        put(root, path, complex_source(f"route{index}"))
        paths.append(path)
    return [row for row in scan.scan(root, paths)[1] if row["rule"] == "complexity"]


def verdict(row, decision="accept"):
    return {"key": row["key"], "evidence_digest": row["evidence_digest"], "decision": decision,
            "reason": "Independent routing rules mix in one function.",
            "proposal": "Extract independent rules while preserving the public contract."}


def test_measured_complexity_is_objective_but_requires_a_decision(tmp_path):
    row, = complexity_rows(tmp_path)
    assert assessment.objective(row)
    assert not review.eligible(row, {})


@pytest.mark.parametrize("gate", ["zero_references", "no_runtime_hits", "not_public_api", "not_entry_point"])
def test_model_cannot_upgrade_unknown_dead_code_gate(gate):
    facts = dict.fromkeys(("zero_references", "no_runtime_hits", "not_public_api", "not_entry_point"), True)
    facts[gate] = None
    row = scan.candidate("dead-code", "src/a.py", "_hidden", 1, "facts", high=True, facts=facts)
    assert not assessment.objective(row)


@pytest.mark.parametrize("rule", ["overdesign-wrapper", "overdesign-switch", "overdesign-parameter", "overdesign-abstraction", "ruff-F401"])
def test_proxies_cannot_be_promoted_by_a_model(rule):
    row = scan.candidate(rule, "src/a.py", "identity", 1, "proxy", high=True)
    queue = {}
    assessment.enqueue(queue, [row], {}, {})
    assert not queue


@pytest.mark.parametrize("mutation", ["foreign", "duplicate", "digest", "missing", "extra", "empty_reason", "empty_proposal"])
def test_response_validation_binds_exact_evidence(tmp_path, mutation):
    row, = complexity_rows(tmp_path)
    item = verdict(row)
    response = {"reviews": [item]}
    if mutation == "foreign":
        item["key"] = "foreign"
    elif mutation == "duplicate":
        response["reviews"].append(dict(item))
    elif mutation == "digest":
        item["evidence_digest"] = "stale"
    elif mutation == "missing":
        response["reviews"] = []
    elif mutation == "extra":
        item["high_confidence"] = True
    elif mutation == "empty_reason":
        item["reason"] = " "
    else:
        item["proposal"] = " "
    with pytest.raises(ValueError):
        assessment.validate(response, [row], 3)


def test_response_cannot_accept_more_than_issue_allowance(tmp_path):
    rows = complexity_rows(tmp_path, 4)
    with pytest.raises(ValueError, match="allowance"):
        assessment.validate({"reviews": [verdict(row) for row in rows]}, rows, 3)


def test_queue_drains_without_new_daily_candidates(tmp_path):
    rows = complexity_rows(tmp_path, 5)
    queue, decisions, ledger, seen = {}, {}, {}, []

    def invoke(packet, schema):
        assert schema == assessment.SCHEMA
        batch = [item["candidate"] for item in packet["candidates"]]
        seen.extend(row["key"] for row in batch)
        return {"reviews": [verdict(row) for row in batch]}

    ready, first = assessment.assess(tmp_path, rows, queue, decisions, ledger,
                                     {"max_candidates": 2}, invoke, sha="a" * 40)
    assert first["reviewed"] == 2 and len(ready) == 2
    ledger.update({row["key"]: {"status": "published"} for row in ready})
    ready, second = assessment.assess(tmp_path, [], queue, decisions, ledger,
                                      {"max_candidates": 2}, invoke, sha="b" * 40)
    assert second["reviewed"] == 2 and len(ready) == 2
    assert len(seen) == len(set(seen)) == 4


def test_muted_published_and_cached_rejections_skip_model(tmp_path):
    rows = complexity_rows(tmp_path, 3)
    decisions = {rows[0]["key"]: {"muted": True}, rows[2]["key"]: {"reviewed_evidence": rows[2]["evidence_digest"], "review_policy": assessment.POLICY_VERSION}}
    ledger = {rows[1]["key"]: {"status": "published"}}
    ready, summary = assessment.assess(tmp_path, rows, {}, decisions, ledger, {},
                                      lambda *args: pytest.fail("no model request allowed"), sha="a" * 40)
    assert ready == [] and summary["reviewed"] == 0
    assert decisions[rows[0]["key"]]["muted"] is True


def test_stale_queue_fact_is_remeasured(tmp_path):
    row, = complexity_rows(tmp_path)
    put(tmp_path, row["path"], "def route0(value):\n    return value\n")
    queue = {row["key"]: row}
    ready, summary = assessment.assess(tmp_path, [], queue, {}, {}, {},
                                      lambda *args: pytest.fail("removed smell must not reach model"), sha="b" * 40)
    assert ready == [] and summary["remaining_queue"] == 0


def test_invalid_model_batch_does_not_partially_accept(tmp_path):
    rows = complexity_rows(tmp_path, 2)
    decisions = {}
    with pytest.raises(ValueError):
        assessment.assess(tmp_path, rows, {}, decisions, {}, {},
            lambda packet, schema: {"reviews": [verdict(packet["candidates"][0]["candidate"])]}, sha="a" * 40)
    assert decisions == {}


def test_large_context_is_deferred_without_truncation(tmp_path):
    put(tmp_path, "src/a.py", complex_source() + "    text = " + repr("x" * 25000) + "\n")
    rows = [row for row in scan.scan(tmp_path, ["src/a.py"])[1] if row["rule"] == "complexity"]
    decisions = {}
    ready, summary = assessment.assess(tmp_path, rows, {}, decisions, {}, {},
                                      lambda *args: pytest.fail("do not truncate context"), sha="a" * 40)
    assert ready == [] and summary["local_deferred"] == 1
    assert decisions[rows[0]["key"]]["review_decision"] == "defer"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.mark.parametrize("outcome", ["accept", "stale_digest", "client_error"])
def test_real_cli_to_issue_and_failure_cursor_retention(tmp_path, monkeypatch, outcome):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    git(origin, "config", "user.email", "fixture@example.invalid")
    git(origin, "config", "user.name", "Fixture")
    put(origin, "src/seed.py", "value = 1\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture baseline")
    state = tmp_path / "code-entropy-review"
    state.mkdir()
    policy = put(tmp_path, "baseline.md", "fixture baseline version 1")
    review.initialize(state, "fixture/repo", str(origin), policy)
    monkeypatch.setattr(review, "tool_runs", lambda *args: ({}, [], {}))
    with review.run_lock(state):
        assert review.run_scan(state, "daily", timeout_seconds=30)["status"] == "complete"
    old_cursor = (state / "daily-cursor.json").read_bytes()
    put(origin, "src/route.py", complex_source())
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture complex route")
    script = f"#!{sys.executable}\n" + """import json,sys
from pathlib import Path
packet=json.load(sys.stdin)
Path('argv.json').write_text(json.dumps(sys.argv[1:]))
rows=[{'key':item['candidate']['key'],'evidence_digest':item['candidate']['evidence_digest'],'decision':'accept','reason':'Separate routing rules reduce maintenance cost.','proposal':'Keep the public contract while extracting independent rules.'} for item in packet['candidates']]
"""
    if outcome == "client_error":
        script += "sys.exit(4)\n"
    elif outcome == "stale_digest":
        script += "rows[0]['evidence_digest']='stale'\n"
    script += "print(json.dumps({'subtype':'success','is_error':False,'structured_output':{'reviews':rows}}))\n"
    client = put(tmp_path, "reviewer", script)
    client.chmod(0o700)
    config = review.read_json(state / "config.json")
    config["auto_review"] = {"enabled": True, "client": str(client), "model": "fixture-model", "max_candidates": 2}
    review.write_json(state / "config.json", config)
    actual = review.command
    calls, created = [], []

    def command(argv, cwd, log, timeout, **kwargs):
        if argv[0] != "gh":
            return actual(argv, cwd, log, timeout, **kwargs)
        calls.append(argv)
        if argv[1:3] == ["issue", "create"]:
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            created.append(body)
            value = "https://github.com/fixture/repo/issues/1"
        elif "issues?state=all" in argv[2]:
            value = "[[]]"
        elif "/labels/" in argv[2]:
            value = json.dumps({"name": "code-entropy"})
        else:
            value = json.dumps({"body": created[-1], "labels": [{"name": "code-entropy"}]})
        log.write_text(value)
        log.with_suffix(".stderr").write_text("")
        return {"command": argv, "exit_code": 0, "output": log.name, "stderr": log.with_suffix(".stderr").name}

    monkeypatch.setattr(review, "command", command)
    with review.run_lock(state):
        result = review.run_scan(state, "daily", publish=True, timeout_seconds=30)
    if outcome != "accept":
        assert result["status"] == "failed", result
        assert (state / "daily-cursor.json").read_bytes() == old_cursor
        assert not calls and not created
        assert review.read_json(state / "review-queue.json")
        assert not review.read_json(state / "decisions.json", {})
        return
    assert result["status"] == "complete", result["gaps"]
    assert result["auto_review"]["decisions"] == {"accept": 1}
    assert len(created) == 1 and result["published_issues"] == ["https://github.com/fixture/repo/issues/1"]
    assert (state / "daily-cursor.json").read_bytes() != old_cursor
    work = state / "runs" / result["run_id"] / "review-work"
    arguments = json.loads((work / "argv.json").read_text())
    assert arguments[arguments.index("--tools") + 1] == ""
    assert "--safe-mode" in arguments and "--strict-mcp-config" in arguments
    assert arguments[arguments.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    with review.run_lock(state):
        repeated = review.run_scan(state, "daily", publish=True, timeout_seconds=30)
    assert repeated["status"] == "complete" and len(created) == 1


def test_review_capacity_keeps_unseen_candidates_for_next_round(tmp_path):
    rows = complexity_rows(tmp_path, 6)
    queue, decisions, ledger = {}, {}, {}

    def invoke(packet, schema):
        return {"reviews": [verdict(item["candidate"], "accept" if index < packet["max_accepts"] else "defer")
                            for index, item in enumerate(packet["candidates"])]}

    ready, first = assessment.assess(tmp_path, rows, queue, decisions, ledger, {}, invoke, sha="a" * 40)
    assert first["reviewed"] == 3 and len(ready) == 3
    assert len(queue) == 6
    ledger.update({row["key"]: {"status": "published"} for row in ready})
    ready, second = assessment.assess(tmp_path, [], queue, decisions, ledger, {}, invoke, sha="a" * 40)
    assert second["reviewed"] == 3 and len(ready) == 3


def test_literal_false_scanner_rule_reaches_automatic_review(tmp_path):
    put(tmp_path, "src/a.py", "if False:\n    value = 1\n")
    row, = scan.scan(tmp_path, ["src/a.py"])[1]
    assert row["rule"] == "unreachable-branch" and assessment.objective(row)
    ready, summary = assessment.assess(tmp_path, [row], {}, {}, {}, {},
        lambda packet, schema: {"reviews": [verdict(item["candidate"]) for item in packet["candidates"]]}, sha="a" * 40)
    assert ready == [row] and summary["reviewed"] == 1


def dead_row(root, expression="1"):
    put(root, "src/a.py", f"def _old():\n    return {expression}\n")
    coverage = {"src/a.py": {"executed_lines": [1], "missing_lines": [2], "missing_branches": []}}
    row = next(row for row in scan.scan(root, ["src/a.py"], coverage)[1] if row["rule"] == "dead-code")
    assert row["high_confidence"]
    return row, coverage


def test_dead_queue_retains_unknown_coverage_without_publishing(tmp_path):
    row, coverage = dead_row(tmp_path)
    queue = {row["key"]: row}
    ready, summary = assessment.assess(tmp_path, [], queue, {}, {}, {},
        lambda *args: pytest.fail("unknown coverage must not reach model"), sha="a" * 40)
    assert not ready and summary["remaining_queue"] == 1
    assert summary["evidence_blocked"] == [row["key"]]
    ready, resumed = assessment.assess(tmp_path, [], queue, {}, {}, {},
        lambda packet, schema: {"reviews": [verdict(item["candidate"]) for item in packet["candidates"]]},
        sha="a" * 40, coverage=coverage)
    assert ready == [row] and resumed["reviewed"] == 1


def test_dead_source_change_invalidates_cached_evidence(tmp_path):
    first, _ = dead_row(tmp_path)
    second, _ = dead_row(tmp_path, "{'different': 2}")
    assert first["key"] == second["key"]
    assert first["evidence_digest"] != second["evidence_digest"]


def test_unmute_invalidates_auto_review_cache(tmp_path):
    row, = complexity_rows(tmp_path)
    state = tmp_path / "code-entropy-review"
    state.mkdir()
    review.write_json(state / "decisions.json", {row["key"]: {"muted": True,
        "accepted_evidence": row["evidence_digest"], "reviewed_evidence": row["evidence_digest"],
        "review_policy": assessment.POLICY_VERSION, "reason": "old decision"}})
    review.record_decision(state, "unmute", row["key"], "reconsider explicitly")
    decisions = review.read_json(state / "decisions.json")
    assert "accepted_evidence" not in decisions[row["key"]]
    assert "reviewed_evidence" not in decisions[row["key"]]
    ready, summary = assessment.assess(tmp_path, [row], {}, decisions, {}, {},
        lambda packet, schema: {"reviews": [verdict(item["candidate"]) for item in packet["candidates"]]}, sha="a" * 40)
    assert ready == [row] and summary["reviewed"] == 1


def test_same_complexity_with_changed_source_has_new_evidence(tmp_path):
    first, = complexity_rows(tmp_path)
    path = tmp_path / first["path"]
    path.write_text(path.read_text().replace("== 0", "== 100"))
    second = next(row for row in scan.scan(tmp_path, [first["path"]])[1] if row["rule"] == "complexity")
    assert first["key"] == second["key"] and first["raw"] == second["raw"]
    assert first["evidence_digest"] != second["evidence_digest"]
