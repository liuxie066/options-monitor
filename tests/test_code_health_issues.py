from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from scripts import code_health_issues as issues
from scripts import code_health_repair as repair
from scripts import code_health_review as review


A = "a" * 40
B = "b" * 40
REPORT = "已读取原报告、完整后续讨论与关联实现，并在冻结版本上明确当前残留、反证、验证范围、未完成事项和下一步。" * 8


def issue(number, **extra):
    return {"number": number, "title": "Original report claims a write succeeded", "state": "open",
            "body": "Original report; implementation: https://github.com/fixture/repo/pull/50",
            "updated_at": "2026-11-03T10:00:00Z", "assignees": [],
            "html_url": f"https://github.com/fixture/repo/issues/{number}", **extra}


def finding(key="example:sum"):
    return {"key": key, "title": "Wrong sum", "owner_path": "example.py", "failure_scenario": "add(2, 1)",
            "expected": "3", "actual": "1", "fix_scope": ["example.py", "tests/test_example.py"],
            "evidence_log": "probe.log"}


def response(number=1, outcome="not_reproduced", **extra):
    return {"outcome": "complete", "report_markdown": REPORT,
            "reviews": [{"number": number, "outcome": outcome, "report_markdown": REPORT,
                         "comment_draft": "待审核：当前版本的后续反证不能当成已修复或已发布。",
                         "finding": finding() if outcome == "confirmed" else None,
                         "closure_evidence": None,
                         "triage": {"decision": "worthwhile", "reason": "Project-owned defect with a small, verifiable fix.",
                                    "third_party_service": False}, **extra}]}


def stage_evidence(stage, text="expected=3 actual=1\nexit_code=0\n"):
    work = stage / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "probe.log").write_text(text)
    return work


def issue_work(run, number=1):
    return run / "items" / str(number) / "work"


def invoke_response(payload):
    def invoke(config, stage, prompt, **kwargs):
        assert stage.parent.name == "items"
        snapshot = json.loads((stage / "plan.json").read_text())["issues"]
        assert [row["number"] for row in snapshot] == [int(stage.name)]
        stage_evidence(stage)
        return json.loads(json.dumps(payload))
    return invoke


def make_plan(*numbers, **extra):
    return {"target": A, "issues": [{"number": n, "input_digest": str(n), "issue": issue(n),
                                    "comments": [], "timeline": [], "linked_pull_requests": []} for n in numbers],
            "open_pull_requests": [], "local_repairs": [], **extra}


def process(tmp_path, payload, plan=None, *, config=None, command=None):
    return issues.process_issues(tmp_path, {"timeout_seconds": 180, **(config or {})}, tmp_path / "run",
                                 tmp_path / "source", plan or make_plan(1), time.monotonic() + 180,
                                 invoke_response(payload), review.FINDING_SCHEMA, review.validate_finding, command=command)


def test_full_discussion_and_linked_work_are_paginated_before_review(tmp_path):
    calls, forbidden = [], []
    open_pr = {"number": 60, "title": "Fix #1", "body": "Fixes #1", "html_url": "https://github.com/fixture/repo/pull/60", "head": {"sha": A}}
    responses = {
        "issues?state=open&sort=created&direction=asc&per_page=100": [[issue(1), issue(2)], [issue(3), issue(4)]],
        "pulls?state=open&per_page=100": [[open_pr]],
        "pulls/60": {**open_pr, "state": "open"},
        "pulls/60/files?per_page=100": [[{"filename": "example.py"}]],
        "pulls/50": {"number": 50, "state": "closed", "merged_at": "2026-11-02", "title": "Partial implementation", "merge_commit_sha": A},
    }
    for n in (1, 2, 3, 4):
        responses.update({f"issues/{n}": issue(n), f"issues/{n}/comments?per_page=100": [[{"body": "Initial claim"}], [{"body": "Correction: no successful write; actual ValueError"}]],
                          f"issues/{n}/timeline?per_page=100": [[{"event": "cross-referenced", "source": {"issue": {"number": 50, "pull_request": {}, "repository_url": "https://api.github.com/repos/fixture/repo"}}}]]})

    def command(argv):
        calls.append(argv)
        if argv[:2] != ["gh", "api"] or any(x in argv for x in ("-X", "POST", "PATCH", "--method", "--input", "--field")) or argv[-1].split("repos/fixture/repo/")[-1] not in responses:
            forbidden.append(argv)
            raise AssertionError(argv)
        path = argv[-1].removeprefix("repos/fixture/repo/")
        if "?" in path:
            assert argv[2:4] == ["--paginate", "--slurp"]
        return json.dumps(responses[path])

    config = {"gh": "gh", "repository": "fixture/repo", "max_issues": 3}
    plan = issues.plan_issues(tmp_path, config, A, command)
    assert [row["number"] for row in plan["issues"]] == [1, 2, 3]
    assert plan["open_issue_count"] == 4
    assert "no successful write" in plan["issues"][0]["comments"][-1]["body"]
    assert [row["number"] for row in plan["issues"][0]["linked_pull_requests"]] == [50, 60]
    assert not forbidden
    assert not any(call[-1].endswith("/issues/4") for call in calls)
    assert any("issues/4/timeline?" in call[-1] for call in calls)
    repair.save(tmp_path / "issue-state.json", {"issues": {
        str(row["number"]): {"target": A, "input_digest": row["input_digest"], "outcome": "not_reproduced", "reviewed_at": "2026-11-03",
                                 "triage": {"decision": "worthwhile", "reason": "verified", "third_party_service": False}} for row in plan["issues"]}})
    assert [row["number"] for row in issues.plan_issues(tmp_path, config, A, command)["issues"]] == [4]
    assert [row["number"] for row in issues.plan_issues(tmp_path, config, "b" * 40, command)["issues"]] == [4, 1, 2]
    responses["issues?state=open&sort=created&direction=asc&per_page=100"][0][0]["updated_at"] = "later"
    responses["issues/1"]["updated_at"] = "later"
    assert [row["number"] for row in issues.plan_issues(tmp_path, config, A, command)["issues"]] == [4]
    responses["issues?state=open&sort=created&direction=asc&per_page=100"][0][0]["body"] += " Updated reproduction"
    responses["issues/1"]["body"] += " Updated reproduction"
    assert [row["number"] for row in issues.plan_issues(tmp_path, config, A, command)["issues"]] == [4, 1]
    pending = tmp_path / "repairs/local/task.json"
    pending.parent.mkdir(parents=True)
    repair.save(pending, {"id": "local", "status": "pending", "sources": [{"kind": "issue", "number": 3}],
                          "finding": {"key": "current-work", "fix_scope": ["example.py"]}})
    assert [row["number"] for row in issues.plan_issues(tmp_path, config, A, command)["issues"]] == [4, 1]
    assert issues.plan_issues(tmp_path, config, A, command)["local_repairs"][0]["id"] == "local"


def test_confirmed_issue_uses_shared_queue_and_keeps_review_cursor(tmp_path):
    cursor = b'{"cursor": "unchanged", "last_review": "previous"}\n'
    (tmp_path / "state.json").write_bytes(cursor)
    review_run = tmp_path / "earlier-review"
    (review_run / "work").mkdir(parents=True)
    (review_run / "work/probe.log").write_text("original evidence")
    original = {**finding(), "commit": A}
    task_id = repair.persist_findings(tmp_path, review_run, {"confirmed_findings": [original]}, A)[0]
    task_path = tmp_path / "repairs" / task_id / "task.json"
    before = json.loads(task_path.read_text())
    actual = process(tmp_path, response(outcome="confirmed"))
    assert actual["repair_tasks"] == [task_id]
    task = json.loads(task_path.read_text())
    assert task["finding"] == original and task["base_sha"] == before["base_sha"]
    assert task["evidence"] == before["evidence"] and task["attempts"] == before["attempts"]
    assert {row["kind"] for row in task["sources"]} == {"review", "issue"}
    assert task["sources"][-1]["number"] == 1
    assert (tmp_path / "state.json").read_bytes() == cursor
    assert json.loads((tmp_path / "issue-state.json").read_text())["issues"]["1"]["repair_task"] == task_id
    assert len(list((tmp_path / "repairs").glob("*/task.json"))) == 1


@pytest.mark.parametrize("first_source", ["issue", "review"])
def test_scope_conflict_preserves_distinct_findings_without_parallel_work(tmp_path, monkeypatch, first_source):
    first = process(tmp_path, response(outcome="confirmed"))["repair_tasks"][0]
    first_path = tmp_path / "repairs" / first / "task.json"
    task = json.loads(first_path.read_text())
    if first_source == "review":
        task["sources"] = [{"kind": "review", "commit": A}]
        repair.save(first_path, task)
    second_finding = {**finding("other-key"), "commit": A}
    second = repair.persist_findings(tmp_path, tmp_path / "run/items/1", {"confirmed_findings": [second_finding]}, A,
                                    source={"kind": "issue" if first_source == "review" else "review", "number": 2})[0]
    assert first != second
    second_task = json.loads((tmp_path / "repairs" / second / "task.json").read_text())
    assert second_task["finding"] == second_finding
    task["status"] = "awaiting_delivery_authorization"
    repair.save(first_path, task)
    calls = []
    monkeypatch.setattr(repair, "dependency_inputs_match", lambda *args: calls.append(args))
    receipt = repair.repair_one(tmp_path, {}, tmp_path / "run", time.monotonic() + 180, lambda *a, **k: pytest.fail("conflicting work started"))
    assert receipt["status"] == "deferred"
    assert first in receipt["tasks"][0]["reason"]
    assert not calls and not (tmp_path / "repairs" / second / "worktree").exists()
    assert json.loads((tmp_path / "repairs" / second / "task.json").read_text())["status"] == "pending"


@pytest.mark.parametrize("owner", ["assignee", "linked_pr", "overlapping_pr"])
def test_existing_ownership_blocks_issue_repair_admission(tmp_path, owner):
    plan = make_plan(1)
    if owner == "assignee":
        plan["issues"][0]["issue"]["assignees"] = [{"login": "existing-owner"}]
    elif owner == "linked_pr":
        plan["issues"][0]["linked_pull_requests"] = [{"number": 42, "state": "open"}]
    else:
        plan["open_pull_requests"] = [{"number": 42, "files": ["example.py"]}]
    receipt = process(tmp_path, response(outcome="confirmed"), plan)
    assert not receipt["repair_tasks"] and not (tmp_path / "repairs").exists()
    assert json.loads((tmp_path / "issue-state.json").read_text())["issues"]["1"]["outcome"] == "in_progress"


def test_issue_normalizes_absolute_work_evidence_in_finding_and_repair_task(tmp_path):
    payload = response(outcome="confirmed")
    payload["reviews"][0]["finding"]["evidence_log"] = str(issue_work(tmp_path / "run") / "probe.log")
    assert process(tmp_path, payload)["status"] == "complete"
    stage = tmp_path / "run/items/1"
    persisted = json.loads((stage / "result.json").read_text())["reviews"][0]["finding"]
    task_path, = (tmp_path / "repairs").glob("*/task.json")
    task = json.loads(task_path.read_text())
    assert persisted["evidence_log"] == task["finding"]["evidence_log"] == "probe.log"
    assert (stage / "comment-draft.md").read_text().strip() == payload["reviews"][0]["comment_draft"]
    assert (stage / "report.md").read_text().strip() == REPORT
    assert task["evidence"] == str((stage / "work/probe.log").resolve())
    assert task["finding"]["owner_path"] == "example.py"
    assert task["finding"]["fix_scope"] == ["example.py", "tests/test_example.py"]
    state = json.loads((tmp_path / "issue-state.json").read_text())
    assert state["issues"]["1"]["outcome"] == "confirmed"


def test_issue_rejects_absolute_evidence_outside_work_before_persistence(tmp_path):
    outside = tmp_path / "probe.log"
    outside.write_text("out of scope evidence")
    payload = response(outcome="confirmed")
    payload["reviews"][0]["finding"]["evidence_log"] = str(outside)
    receipt = process(tmp_path, payload)
    assert receipt["status"] == "partial" and receipt["summary"]["pending"] == [1]
    assert "unsafe" in receipt["summary"]["pending_reasons"]["1"]
    state = json.loads((tmp_path / "issue-state.json").read_text())["issues"]["1"]
    assert state["last_attempted_at"] and "reviewed_at" not in state
    assert not (tmp_path / "repairs").exists()


def test_issue_report_cannot_assert_confirmation_without_evidence(tmp_path):
    def missing_evidence(config, stage, prompt, **kwargs):
        (stage / "work").mkdir()
        return response(outcome="confirmed")
    receipt = issues.process_issues(tmp_path, {}, tmp_path / "run", tmp_path, make_plan(1),
                                   time.monotonic() + 180, missing_evidence,
                                   review.FINDING_SCHEMA, review.validate_finding)
    assert receipt["summary"]["pending"] == [1]
    assert "durable" in receipt["summary"]["pending_reasons"]["1"]
    state = json.loads((tmp_path / "issue-state.json").read_text())["issues"]["1"]
    assert "reviewed_at" not in state and state["last_attempted_at"]
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "repairs").exists()


@pytest.mark.parametrize("bad", [
    response(number=99), response(outcome="not_reproduced", finding=finding()),
    response(outcome="confirmed", finding={**finding(), "commit": A}),
    {**response(), "reviews": []}, {**response(), "outcome": "partial", "reviews": []},
    {**response(), "outcome": "blocked"},
    {**response(), "reviews": [{key: value for key, value in response()["reviews"][0].items() if key != "closure_evidence"}]},
])
def test_invalid_decisions_do_not_create_issue_progress(tmp_path, bad):
    receipt = process(tmp_path, bad)
    assert receipt["status"] == "partial" and receipt["reviewed"] == [] and receipt["summary"]["pending"] == [1]
    state = json.loads((tmp_path / "issue-state.json").read_text())["issues"]["1"]
    assert state["last_attempted_at"] and "reviewed_at" not in state
    assert "completed_reviews" not in state and "repair_task" not in state
    assert (tmp_path / "run/items/1/pending.json").is_file()
    assert not (tmp_path / "repairs").exists()


@pytest.mark.parametrize("new_outcome", ["in_progress", "already_fixed", "not_reproduced", "needs_decision"])
def test_changed_issue_decision_pauses_existing_pending_repair(tmp_path, new_outcome):
    task_id = process(tmp_path, response(outcome="confirmed"))["repair_tasks"][0]
    path = tmp_path / "repairs" / task_id / "task.json"
    task = json.loads(path.read_text())
    task["stage"] = "implement"
    task["worktree"] = "preserved-local-work"
    repair.save(path, task)
    before = path.read_bytes()
    process(tmp_path, response(outcome=new_outcome))
    state = json.loads((tmp_path / "issue-state.json").read_text())["issues"]["1"]
    expected = "needs_decision" if new_outcome in {"already_fixed", "not_reproduced"} else new_outcome
    assert state["repair_task"] == task_id and state["outcome"] == expected
    receipt = repair.repair_one(tmp_path, {"issue_intake_ready": True, "issue_target": A, "issue_inputs": {"1": {"input_digest": "1", "assigned": False}}},
                                tmp_path / "run", time.monotonic() + 180,
                                lambda *a, **k: pytest.fail("superseded issue repair resumed"))
    assert receipt["status"] == "deferred"
    assert path.read_bytes() == before


@pytest.mark.parametrize("changed_source", [None, "changed", "closed", "assigned"])
def test_four_issue_sources_can_resume_when_current_inputs_match(tmp_path, monkeypatch, changed_source):
    task_id = process(tmp_path, response(outcome="confirmed"))["repair_tasks"][0]
    path = tmp_path / "repairs" / task_id / "task.json"
    task = json.loads(path.read_text())
    task["sources"] = [{"kind": "issue", "number": n} for n in (1, 2, 3, 4)]
    repair.save(path, task)
    state = {str(n): {"outcome": "confirmed", "triage": {"decision": "worthwhile", "reason": "project defect", "third_party_service": False}, "target": A, "repair_task": task_id, "input_digest": str(n),
                       "run_dir": "earlier-round"} for n in (1, 2, 3, 4)}
    repair.save(tmp_path / "issue-state.json", {"issues": state})
    inputs = {str(n): {"input_digest": str(n), "assigned": False} for n in (1, 2, 3, 4)}
    if changed_source == "changed":
        inputs["4"]["input_digest"] = "new discussion"
    elif changed_source == "closed":
        del inputs["4"]
    elif changed_source == "assigned":
        inputs["4"]["assigned"] = True
    entered = []

    def reached(*args):
        entered.append(True)
        raise TimeoutError("accepted issue inputs; stop before fixture worktree creation")

    monkeypatch.setattr(repair, "dependency_inputs_match", reached)
    result = repair.repair_one(tmp_path, {"issue_intake_ready": True, "issue_target": A, "issue_inputs": inputs},
                               tmp_path / "run", time.monotonic() + 180, lambda *a, **k: pytest.fail("fixture must stop earlier"))
    assert result["status"] == ("pending" if changed_source is None else "deferred")
    assert bool(entered) == (changed_source is None)


def test_unchecked_pending_issue_is_not_resumed_after_intake_failure(tmp_path):
    process(tmp_path, response(outcome="confirmed"))
    receipt = repair.repair_one(tmp_path, {"issue_intake_ready": False}, tmp_path / "run", time.monotonic() + 180,
                                lambda *a, **k: pytest.fail("unchecked issue repair resumed"))
    assert receipt["status"] == "deferred"


def test_issue_snapshot_calls_use_remaining_total_budget(tmp_path, monkeypatch):
    clock, calls = [0], []
    monkeypatch.setattr(issues.time, "monotonic", lambda: clock[0])

    def command(argv, *, timeout):
        calls.append(timeout)
        path = argv[-1].removeprefix("repos/fixture/repo/")
        data = [[issue(1, body="")]] if path.startswith("issues?") else [[]] if path.startswith("pulls?") else issue(1)
        clock[0] += 135
        return json.dumps(data)

    with pytest.raises(TimeoutError, match="budget"):
        issues.plan_issues(tmp_path, {"gh": "gh", "repository": "fixture/repo"}, A, command, deadline=270)
    assert calls == [180, 135]
    assert not (tmp_path / "issue-state.json").exists()


def test_budget_and_model_timeout_preserve_pending_issue(tmp_path):
    def timeout(*args, **kwargs):
        raise TimeoutError("model budget exhausted")
    for deadline in (time.monotonic() - 1, time.monotonic() + 120):
        receipt = issues.process_issues(tmp_path, {}, tmp_path / "run", tmp_path, make_plan(1), deadline,
                                       timeout, review.FINDING_SCHEMA, review.validate_finding)
        assert receipt["status"] == "partial" and receipt["summary"]["pending"] == [1] and receipt["reviewed"] == []
    state = json.loads((tmp_path / "issue-state.json").read_text())["issues"]["1"]
    assert state["last_attempted_at"] and "reviewed_at" not in state
    assert not (tmp_path / "repairs").exists()


@pytest.mark.parametrize("has_commit", [False, True])
@pytest.mark.parametrize("mode", ["run", "dry_run", "smoke", "review_timeout", "intake_timeout", "budget_exhausted", "archive_budget", "reserved_budget"])
def test_daily_entry_processes_issues_independently_with_real_git_and_model_adapter(tmp_path, monkeypatch, has_commit, mode):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()

    git("init", "-q", "-b", "main")
    (source / "AGENTS.md").write_text("Frozen issue fixture instructions")
    (source / ".gitattributes").write_text("AGENTS.md export-ignore\n")
    (source / "example.py").write_text("def add(a, b): return a - b\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture base")
    base = git("rev-parse", "HEAD")
    if has_commit:
        (source / "note.md").write_text("new review unit")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture change")
    target = git("rev-parse", "HEAD")
    root = tmp_path / "state"
    root.mkdir()
    (root / "previous.md").write_text("previous report")
    (root / "state.json").write_text(json.dumps({"cursor": base, "last_review": str(root / "previous.md")}))
    (root / "policy.md").write_text("Full discussion before repair; no external writes")
    agent = tmp_path / "model-client"
    agent.write_text(f"#!{Path(sys.executable).resolve()}\n" + '''import json, pathlib, sys
args = sys.argv
prompt = sys.stdin.read()
output = pathlib.Path(args[args.index("--output-last-message") + 1])
work = pathlib.Path(args[args.index("--cd") + 1])
schema = json.loads(pathlib.Path(args[args.index("--output-schema") + 1]).read_text())
if "reviews" in schema["properties"]:
    plan = json.loads((work.parent / "plan.json").read_text())
    assert "Correction: no write" in plan["issues"][0]["comments"][-1]["body"]
    assert work.parent.parent.name == "items"
    source = work.parents[3] / "source"
    assert (source / "AGENTS.md").read_text().startswith("Frozen")
    assert "禁止网络" in prompt and "comment_draft" in prompt
    payload = {"outcome":"complete", "report_markdown":"issue evidence and limitations " * 15,
               "reviews":[{"number":1,"outcome":"not_reproduced", "report_markdown":"Later correction rejects the original write claim " * 12,
                           "comment_draft":"Current SHA only; not merged/released", "finding":None, "closure_evidence":None,
                           "triage":{"decision":"worthwhile","reason":"Review project defect", "third_party_service":False}}]}
else:
    plan = json.loads((work.parent / "plan.json").read_text())
    payload = {"outcome":"complete", "completed_commits":[row["sha"] for row in plan["commits"]],
               "report_markdown":"review evidence and coverage " * 15,"verification_gaps":[],"confirmed_findings":[]}
output.write_text(json.dumps(payload))
''')
    agent.chmod(0o700)
    (root / "config.json").write_text(json.dumps({
        "git": "git", "gh": "github-client", "codex": str(agent), "repository": "fixture/repo",
        "repository_url": str(source), "pull_requests_enabled": False, "comments_enabled": False, "release_enabled": False, "max_units": 3, "timeout_seconds": 180, "issue_timeout_seconds": 60,
        "issues_enabled": True, "repair_enabled": True, "repair_budget_seconds": 30, "model": "fixture",
        "model_base_url": "http://fixture.invalid", "model_catalog": str(root / "catalog.json"),
    }))
    if mode == "reserved_budget":
        config = json.loads((root / "config.json").read_text())
        config["issue_timeout_seconds"] = 180
        (root / "config.json").write_text(json.dumps(config))
    original = review.command
    calls, forbidden, repairs = [], [], []
    clock = [0]
    responses = {
        "issues?state=open&sort=created&direction=asc&per_page=100": [[issue(1)]],
        "pulls?state=open&per_page=100": [[]], "issues/1": issue(1),
        "issues/1/comments?per_page=100": [[{"body": "Initial claim"}], [{"body": "Correction: no write"}]],
        "issues/1/timeline?per_page=100": [[]], "pulls/50": {"number": 50, "state": "closed", "merged_at": "2026-11-02"},
        f"commits/{target}/pulls": [],
    }

    def external(argv, **kwargs):
        calls.append(tuple(argv))
        if argv[:2] == ["github-client", "api"]:
            path = argv[-1].removeprefix("repos/fixture/repo/")
            if path in responses and not set(argv) & {"--method", "-X", "--field", "--input"}:
                if mode == "archive_budget" and not has_commit and path == "pulls/50":
                    clock[0] = 179.5
                return json.dumps(responses[path])
        if argv == ["github-client", "issue", "list", "--repo", "fixture/repo", "--state", "all", "--limit", "100", "--json", "number,title,state,url"]:
            if mode == "archive_budget":
                clock[0] = 179.5
            return "[]"
        if argv[0] == "git" and not {"commit", "push", "merge", "reset", "clean"} & set(argv[1:]):
            return original(argv, **kwargs)
        forbidden.append(tuple(argv))
        raise AssertionError(f"forbidden call: {argv}")

    def repair_once(*args, **kwargs):
        repairs.append(args[0])
        status = "pending" if args[3] - time.monotonic() < 30 else "no_pending_repairs"
        return {"status": status, "entered": True, "attempted": []}

    monkeypatch.setattr(review, "command", external)
    monkeypatch.setattr(repair, "run_repairs", repair_once)
    original_invoke = review.invoke_agent
    if mode == "archive_budget":
        monkeypatch.setattr(review.time, "monotonic", lambda: clock[0])
    if mode == "review_timeout":
        def timeout_commit(config, stage, *args, **kwargs):
            if stage.parent.name != "items":
                raise TimeoutError("commit review stage exhausted its reserved budget")
            return original_invoke(config, stage, *args, **kwargs)
        monkeypatch.setattr(review, "invoke_agent", timeout_commit)
    if mode in {"intake_timeout", "budget_exhausted"}:
        clock = [0]
        if mode == "budget_exhausted":
            monkeypatch.setattr(review.time, "monotonic", lambda: clock[0])
        def timeout_intake(argv, **kwargs):
            if argv[0] == "github-client" and "issues?state=open" in argv[-1]:
                clock[0] = 180
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            if mode == "budget_exhausted" and clock[0] >= 180:
                forbidden.append(tuple(argv))
                raise AssertionError("external call after total deadline")
            return external(argv, **kwargs)
        monkeypatch.setattr(review, "command", timeout_intake)
    receipt = review.run_once(root, **({mode: True} if mode in {"dry_run", "smoke"} else {}))
    assert not forbidden
    assert len(repairs) == (0 if mode in {"dry_run", "smoke"} else 1)
    actual = json.loads((root / "state.json").read_text())
    assert actual["cursor"] == (target if mode in {"run", "intake_timeout", "budget_exhausted"} and has_commit else base)
    if mode in {"run", "review_timeout", "reserved_budget"}:
        assert receipt["issues"]["reviewed"] == [1]
        saved = json.loads((root / "issue-state.json").read_text())["issues"]["1"]
        assert saved["outcome"] == "needs_decision" and saved["target"] == target
        assert "closure evidence insufficient" in saved["reason"]
        stage = Path(receipt["run_dir"]) / "issues/items/1"
        assert (stage / "report.md").is_file() and (stage / "comment-draft.md").is_file()
        assert not (root / "repairs").exists()
        if mode == "reserved_budget":
            if has_commit:
                assert receipt["status"] == "partial" and receipt["completed_commits"] == []
                assert "budget exhausted" in receipt["review_error"]
            assert not (Path(receipt["run_dir"]) / "agent.jsonl").exists()
            assert not (Path(receipt["run_dir"]) / "units").exists()
        repeated = review.run_once(root)
        assert repeated["issues"]["status"] == "no_eligible_issues"
        if mode == "review_timeout" and has_commit:
            assert receipt["status"] == "partial" and receipt["completed_commits"] == []
    else:
        if mode == "archive_budget" and not has_commit:
            attempted = json.loads((root / "issue-state.json").read_text())["issues"]["1"]
            assert attempted["last_attempted_at"] and "reviewed_at" not in attempted
            assert receipt["issues"]["summary"]["pending"] == [1]
        else:
            assert not (root / "issue-state.json").exists()
        if mode == "archive_budget":
            assert receipt["status"] == ("partial" if has_commit else "no_changes")
            assert receipt["repair"]["entered"] is True and receipt["repair"]["attempted"] == []
            assert receipt["cursor_after"] == base
            assert receipt["repair"]["status"] == "pending"
            # Frozen source is prepared before metadata or issue intake can exhaust their budgets.
            assert any(call[0] == "git" and "archive" in call for call in calls)
        if mode in {"intake_timeout", "budget_exhausted"}:
            assert receipt["issues"]["status"] == "pending"
        if mode == "budget_exhausted":
            assert receipt["status"] == ("complete" if has_commit else "no_changes")
            assert receipt["repair"]["entered"] is True and receipt["repair"]["attempted"] == []
            assert receipt["repair"]["status"] == "pending"
    if mode == "smoke":
        assert not any(call[0] == "github-client" for call in calls)
    assert git("status", "--porcelain") == ""


def inventory_command(inventory, calls):
    def command(argv, **kwargs):
        calls.append(argv)
        assert argv[:2] == ["gh", "api"]
        assert not set(argv) & {"--method", "-X", "POST", "PATCH", "PUT", "DELETE", "--input", "--field", "-f", "-F"}
        path = argv[-1].removeprefix("repos/fixture/repo/")
        if path.startswith("issues?"):
            return json.dumps([inventory])
        if path.startswith("pulls?"):
            return "[[]]"
        if path == "pulls/50":
            return json.dumps({"number": 50, "state": "closed", "merged_at": "2026-11-02", "merge_commit_sha": A})
        if path.startswith("issues/"):
            number = int(path.split("/")[1])
            if "/comments?" in path:
                return json.dumps([[{"body": "Original trigger and later corrections must be checked"}]])
            if "/timeline?" in path:
                return "[[]]"
            return json.dumps(next(row for row in inventory if row["number"] == number))
        raise AssertionError(argv)
    return command


def run_plan(root, run, plan, invoke, *, config=None, command=None):
    return issues.process_issues(root, {"timeout_seconds": 180, **(config or {})}, run, root / "source",
                                 plan, time.monotonic() + 180, invoke,
                                 review.FINDING_SCHEMA, review.validate_finding, command=command)


@pytest.mark.parametrize(("overrides", "expected"), [({}, 5), ({"max_issues": 7}, 7)])
def test_default_five_and_configured_batch_above_three(tmp_path, overrides, expected):
    inventory, calls = [issue(n) for n in range(1, 10)], []
    plan = issues.plan_issues(tmp_path, {"gh": "gh", "repository": "fixture/repo", **overrides}, A,
                              inventory_command(inventory, calls), hydrate=False)
    assert [row["number"] for row in plan["issues"]] == list(range(1, expected + 1))
    assert plan["open_issue_count"] == 9


def test_sixty_four_issues_do_not_starve_when_sha_changes_and_repairs_are_active(tmp_path):
    inventory, calls, invoked = [issue(n) for n in range(1, 65)], [], []
    command = inventory_command(inventory, calls)
    config = {"gh": "gh", "repository": "fixture/repo"}
    for day in range(13):
        target = f"{day + 1:040x}"
        plan = issues.plan_issues(tmp_path, config, target, command, hydrate=False)
        if day:
            assert len(plan["local_repairs"]) == 5
            assert all(task["status"] == "pending" for task in plan["local_repairs"])

        def invoke(config, stage, prompt, **kwargs):
            number = int(stage.name)
            invoked.append(number)
            stage_evidence(stage)
            return response(number, "confirmed", finding=finding(f"inventory:{number}")) if day == 0 else response(number, "needs_decision")

        receipt = run_plan(tmp_path, tmp_path / "runs" / str(day), plan, invoke, command=command, config=config)
        assert receipt["status"] == "complete" and not receipt["summary"]["pending"]
    assert invoked[:64] == list(range(1, 65))
    state = issues.read_state(tmp_path)["issues"]
    assert set(state) == {str(n) for n in range(1, 65)}
    assert all(row["reviewed_at"] and row["completed_reviews"] for row in state.values())
    assert len(list((tmp_path / "repairs").glob("*/task.json"))) == 5


def test_completed_history_reuses_original_input_after_a_b_a_change(tmp_path):
    inventory, calls = [issue(1)], []
    command = inventory_command(inventory, calls)
    config = {"gh": "gh", "repository": "fixture/repo"}
    first = issues.plan_issues(tmp_path, config, A, command)
    run_plan(tmp_path, tmp_path / "first", first, invoke_response(response(outcome="needs_decision")))
    first_digest = first["issues"][0]["input_digest"]
    inventory[0] = issue(1, body="Changed discussion B", updated_at="2026-11-04T10:00:00Z")
    second = issues.plan_issues(tmp_path, config, A, command)
    run_plan(tmp_path, tmp_path / "second", second, invoke_response(response(outcome="needs_decision")))
    assert second["issues"][0]["input_digest"] != first_digest
    inventory[0] = issue(1)
    assert not issues.plan_issues(tmp_path, config, A, command)["issues"]
    replay = issues.plan_issues(tmp_path, config, A, command, full_scan=True)
    assert replay["issues"][0]["cached_review"]["run_dir"] == str(tmp_path / "first/items/1")
    receipt = run_plan(tmp_path, tmp_path / "replay", replay,
                       lambda *a, **k: pytest.fail("completed input A was invoked again"))
    assert receipt["reviewed"] == [1] and not receipt["summary"]["pending"]
    state = issues.read_state(tmp_path)["issues"]["1"]
    assert state["input_digest"] == first_digest and len(state["completed_reviews"]) == 2


def test_repair_only_changes_do_not_reinvoke_or_duplicate_completed_issue(tmp_path):
    inventory, calls = [issue(1)], []
    command = inventory_command(inventory, calls)
    config = {"gh": "gh", "repository": "fixture/repo"}
    plan = issues.plan_issues(tmp_path, config, A, command)
    result = run_plan(tmp_path, tmp_path / "initial", plan, invoke_response(response(outcome="confirmed")))
    task_id, = result["repair_tasks"]
    path = tmp_path / "repairs" / task_id / "task.json"
    task = json.loads(path.read_text())
    task.update({"stage": "implement", "status": "awaiting_delivery_authorization", "worktree": "preserved"})
    repair.save(path, task)
    before = path.read_bytes()
    assert not issues.plan_issues(tmp_path, config, A, command)["issues"]
    replay = issues.plan_issues(tmp_path, config, A, command, full_scan=True)
    assert replay["issues"][0]["input_digest"] == plan["issues"][0]["input_digest"]
    run_plan(tmp_path, tmp_path / "replay", replay, lambda *a, **k: pytest.fail("repair state caused issue re-review"))
    assert path.read_bytes() == before
    assert len(list((tmp_path / "repairs").glob("*/task.json"))) == 1
    assert issues.read_state(tmp_path)["issues"]["1"]["repair_task"] == task_id


@pytest.mark.parametrize("failure", ["timeout", "subprocess_timeout", "invalid"])
def test_one_failed_issue_persists_both_successful_neighbours(tmp_path, failure):
    invoked = []

    def invoke(config, stage, prompt, **kwargs):
        number = int(stage.name)
        invoked.append(number)
        stage_evidence(stage)
        if number == 2:
            if failure == "timeout":
                raise TimeoutError("only issue #2 timed out")
            if failure == "subprocess_timeout":
                raise subprocess.TimeoutExpired(["fixture-model"], 1)
            return response(number=99)
        return response(number, "confirmed", finding=finding(f"neighbour:{number}"))

    receipt = run_plan(tmp_path, tmp_path / "run", make_plan(1, 2, 3), invoke)
    assert invoked == [1, 2, 3]
    assert receipt["status"] == "partial" and receipt["reviewed"] == [1, 3] and receipt["summary"]["pending"] == [2]
    state = issues.read_state(tmp_path)["issues"]
    assert all(state[str(n)]["outcome"] == "confirmed" for n in (1, 3))
    assert state["2"]["last_attempted_at"] and "reviewed_at" not in state["2"]
    assert len(receipt["repair_tasks"]) == 2
    for number in (1, 3):
        stage = tmp_path / "run/items" / str(number)
        assert (stage / "result.json").is_file() and (stage / "report.md").is_file()
        assert (stage / "comment-draft.md").is_file()
    assert (tmp_path / "run/items/2/pending.json").is_file()
    assert receipt["summary"]["pending"] == [2]


CLOSURE_COMMAND = "python -I reproduce_original_issue.py"
CLOSURE_LOG = json.dumps({"command": CLOSURE_COMMAND, "exit_code": 0, "output": "original trigger now passes"}) + "\n"


def closure():
    return {"reason": "Original trigger and subsequent correction both pass at the frozen target",
            "fix_commit": "", "verification_log": "probe.log",
            "commands": [{"command": CLOSURE_COMMAND, "exit_code": 0}]}


@pytest.mark.parametrize("missing", ["structured", "log", "empty_log", "commands", "command_in_log", "exit_in_log", "failed", "boolean_exit"])
def test_closure_without_actual_successful_verification_needs_decision(tmp_path, missing):
    evidence, log = closure(), CLOSURE_LOG
    if missing == "structured":
        evidence = None
    elif missing == "log":
        evidence["verification_log"] = "not-executed.log"
    elif missing == "empty_log":
        log = ""
    elif missing == "commands":
        evidence["commands"] = []
    elif missing == "command_in_log":
        log = "generic tests passed\nexit_code=0\n"
    elif missing == "exit_in_log":
        log = CLOSURE_COMMAND + "\nall assertions passed\n"
    elif missing == "failed":
        evidence["commands"][0]["exit_code"] = 1
        log = CLOSURE_COMMAND + "\nexit_code=1\n"
    else:
        evidence["commands"][0]["exit_code"] = False

    def invoke(config, stage, prompt, **kwargs):
        stage_evidence(stage, log)
        return response(outcome="already_fixed", closure_evidence=evidence)

    receipt = run_plan(tmp_path, tmp_path / "run", make_plan(1), invoke)
    assert receipt["status"] == "complete" and receipt["summary"]["needs_decision"] == [1]
    assert not receipt["summary"]["close_recommended"] and not receipt["summary"]["closure_batches"]
    state = issues.read_state(tmp_path)["issues"]["1"]
    assert state["outcome"] == "needs_decision" and "closure evidence insufficient" in state["reason"]
    assert "close_recommendation" not in state


@pytest.mark.parametrize("structured", [False, True])
def test_failed_closure_command_cannot_borrow_another_commands_success(tmp_path, structured):
    log = (json.dumps({"command": CLOSURE_COMMAND, "exit_code": 1}) + "\n"
           + json.dumps({"command": "true", "exit_code": 0}) + "\n") if structured else (
               CLOSURE_COMMAND + "\nexit_code=1\ntrue\nexit_code=0\n")

    def invoke(config, stage, prompt, **kwargs):
        stage_evidence(stage, log)
        return response(outcome="already_fixed", closure_evidence=closure())

    receipt = run_plan(tmp_path, tmp_path / "run", make_plan(1), invoke)
    assert receipt["summary"]["needs_decision"] == [1]
    assert receipt["summary"]["close_recommended"] == []


def test_valid_closure_recommendations_batch_ten_without_github_writes(tmp_path):
    inventory, calls = [issue(n) for n in range(1, 24)], []
    command = inventory_command(inventory, calls)
    config = {"gh": "gh", "repository": "fixture/repo"}
    plan = issues.plan_issues(tmp_path, config, A, command, full_scan=True, hydrate=False)

    def invoke(config, stage, prompt, **kwargs):
        stage_evidence(stage, CLOSURE_LOG)
        number = int(stage.name)
        return response(number, "already_fixed" if number % 2 else "not_reproduced", closure_evidence=closure())

    receipt = run_plan(tmp_path, tmp_path / "run", plan, invoke, config=config, command=command)
    summary = receipt["summary"]
    assert receipt["status"] == "complete" and summary["close_recommended"] == list(range(1, 24))
    assert [len(batch) for batch in summary["closure_batches"]] == [10, 10, 3]
    assert [row["number"] for batch in summary["closure_batches"] for row in batch] == list(range(1, 24))
    assert summary["github_closed"] == [] and not (tmp_path / "repairs").exists()
    published = json.loads((tmp_path / "run/close-recommendations.json").read_text())
    assert published["action"] == "recommendation_only" and published["github_closed"] == []
    for batch in published["batches"]:
        for row in batch:
            assert row["target"] == A and row["input_digest"]
            assert Path(row["verification_log"]).read_text() == CLOSURE_LOG
            assert row["commands"] == closure()["commands"]
    assert calls and all(argv[:2] == ["gh", "api"] for argv in calls)


@pytest.mark.parametrize("is_ancestor", [True, False])
def test_closure_fix_commit_requires_verified_ancestry(tmp_path, is_ancestor):
    fix_commit, calls = "b" * 40, []
    config = {"git": "git", "mirror": str(tmp_path / "mirror.git")}

    def command(argv, *, timeout):
        calls.append(argv)
        assert 0 < timeout <= 180
        assert argv == ["git", "--git-dir", config["mirror"], "merge-base", "--is-ancestor", fix_commit, A]
        if not is_ancestor:
            raise RuntimeError("fix commit is not an ancestor of the frozen target")
        return ""

    def invoke(config, stage, prompt, **kwargs):
        stage_evidence(stage, CLOSURE_LOG)
        evidence = {**closure(), "fix_commit": fix_commit}
        return response(outcome="already_fixed", closure_evidence=evidence)

    receipt = run_plan(tmp_path, tmp_path / "run", make_plan(1), invoke, config=config, command=command)
    assert len(calls) == 1 and not receipt["summary"]["pending"]
    if is_ancestor:
        assert receipt["summary"]["close_recommended"] == [1]
        assert receipt["summary"]["closure_batches"][0][0]["fix_commit"] == fix_commit
    else:
        assert receipt["summary"]["needs_decision"] == [1]
        assert not receipt["summary"]["closure_batches"]


def test_outer_blocked_report_is_retained_as_needs_decision(tmp_path):
    payload = {"outcome": "blocked", "report_markdown": REPORT, "reviews": []}
    receipt = process(tmp_path, payload)
    assert receipt["reviewed"] == [1] and not receipt["summary"]["pending"]
    assert receipt["summary"]["needs_decision"] == [1]
    stage = tmp_path / "run/items/1"
    assert (stage / "report.md").read_text().strip() == REPORT
    assert json.loads((stage / "raw-result.json").read_text()) == payload
    result = json.loads((stage / "result.json").read_text())
    assert result["reviews"][0]["outcome"] == "needs_decision"
    assert set(result["reviews"][0]) == {"number", "outcome", "report_markdown", "comment_draft", "finding", "closure_evidence", "triage"}
    assert result["reviews"][0]["triage"]["decision"] == "needs_decision"


@pytest.fixture
def queue_inputs(tmp_path):
    issue = {"number": 1, "title": "Wrong result", "state": "open", "assignees": [],
             "body": "See https://github.com/fixture/repo/pull/50", "updated_at": "original",
             "html_url": "https://github.com/fixture/repo/issues/1"}
    pr = {"number": 50, "title": "Earlier fix", "state": "closed", "body": "", "updated_at": "original",
          "head": {"sha": A}, "merged_at": "yesterday", "merge_commit_sha": A,
          "html_url": "https://github.com/fixture/repo/pull/50"}
    data = {"issues": {1: issue}, "prs": {50: pr}, "open_prs": [], "comments": [], "timeline": [], "calls": []}
    config = {"gh": "fixture-gh", "repository": "fixture/repo", "max_issues": 1}

    def command(argv, **kwargs):
        data["calls"].append(argv)
        assert argv[:2] == ["fixture-gh", "api"] and not any(arg in argv for arg in ("PATCH", "POST", "--method"))
        path = argv[-1].removeprefix("repos/fixture/repo/")
        if path.startswith("issues?"):
            return json.dumps([[row for row in data["issues"].values() if row["state"] == "open"]])
        if path.startswith("pulls?"):
            return json.dumps([data["open_prs"]])
        if "/comments?" in path:
            return json.dumps([data["comments"]])
        if "/timeline?" in path:
            return json.dumps([data["timeline"]])
        if "/files?" in path:
            number = int(path.split("/")[1])
            return json.dumps([[{"filename": name} for name in data["prs"][number].get("files", ["elsewhere.py"])]])
        kind, number = path.split("/")
        return json.dumps(data["prs" if kind == "pulls" else kind][int(number)])

    def plan(**kwargs):
        return issues.plan_issues(tmp_path, config, A, command, **kwargs)

    def confirm():
        current = plan()
        stage = tmp_path / "evidence"
        (stage / "work").mkdir(parents=True, exist_ok=True)
        (stage / "work/probe.log").write_text("expected=3 actual=1\n")
        finding = {"key": "example:sum", "commit": A, "title": "Wrong sum", "owner_path": "example.py",
                   "failure_scenario": "add(2, 1)", "expected": "3", "actual": "1",
                   "fix_scope": ["example.py"], "evidence_log": "probe.log"}
        task_id, = repair.persist_findings(tmp_path, stage, {"confirmed_findings": [finding]}, A,
                                           source={"kind": "issue", "number": 1, "verification_sha": A})
        task_path = tmp_path / "repairs" / task_id / "task.json"
        repair.save(tmp_path / "issue-state.json", {"issues": {"1": {
            "target": A, "input_digest": current["issue_inputs"]["1"]["input_digest"],
            "outcome": "confirmed", "triage": {"decision": "worthwhile", "reason": "project defect", "third_party_service": False}, "repair_task": task_id, "reviewed_at": "2026-11-01"}}})
        return task_path, json.loads(task_path.read_text()), current

    return tmp_path, config, data, plan, confirm


def admission(root, config, plan, task):
    live = {**config, "issue_target": A, "issue_intake_ready": True, "issue_inputs": plan["issue_inputs"]}
    state = issues.read_state(root)["issues"]
    return repair._admission_reason(task, [(root / "repairs" / task["id"] / "task.json", task)], live, state)


@pytest.mark.parametrize("change", ["add", "merge", "edit"])
def test_unrelated_pr_changes_do_not_invalidate_confirmed_repair(queue_inputs, change):
    root, config, data, plan, confirm = queue_inputs
    other = {**data["prs"][50], "number": 99, "title": "Unrelated", "state": "open", "merged_at": None,
             "html_url": "https://github.com/fixture/repo/pull/99"}
    data["prs"][99] = other
    if change != "add":
        data["open_prs"] = [other]
    _, task, before = confirm()
    if change == "add":
        data["open_prs"] = [other]
    elif change == "merge":
        data["open_prs"] = []
    else:
        other.update({"body": "More unrelated explanation", "head": {"sha": B}})
    after = plan(hydrate=False)
    assert before["issue_inputs"]["1"]["input_digest"] == after["issue_inputs"]["1"]["input_digest"]
    assert admission(root, config, after, task) is None


@pytest.mark.parametrize("change", ["head", "merged", "body", "comment_link", "timeline_link"])
def test_related_pr_changes_require_revalidation(queue_inputs, change):
    root, config, data, plan, confirm = queue_inputs
    _, task, before = confirm()
    if change == "head":
        data["prs"][50]["head"] = {"sha": B}
    elif change == "merged":
        data["prs"][50]["merge_commit_sha"] = B
    elif change == "body":
        data["prs"][50]["body"] = "Corrected description"
    else:
        data["prs"][51] = {**data["prs"][50], "number": 51}
        if change == "comment_link":
            data["comments"] = [{"body": "See https://github.com/fixture/repo/pull/51"}]
        else:
            data["timeline"] = [{"source": {"issue": {"number": 51, "pull_request": {},
                                                       "repository_url": "https://api.github.com/repos/fixture/repo"}}}]
    after = plan(hydrate=False)
    assert before["issue_inputs"]["1"]["input_digest"] != after["issue_inputs"]["1"]["input_digest"]
    assert admission(root, config, after, task) == "issue changed, is unconfirmed, or awaits current intake verification"
    assert [item["number"] for item in after["issues"]] == [1]


def test_light_and_full_plans_share_exact_issue_identity(queue_inputs):
    _, _, data, plan, _ = queue_inputs
    data["open_prs"] = [{**data["prs"][50], "number": 99, "title": "Addresses #1", "state": "open"}]
    data["prs"][99] = data["open_prs"][0]
    light = plan(hydrate=False)
    full = plan()
    assert light["issue_inputs"] == full["issue_inputs"]
    assert light["issues"][0]["input_digest"] == full["issues"][0]["input_digest"]
    assert "comments" not in light["issues"][0] and "comments" in full["issues"][0]


@pytest.mark.parametrize("pr_side", [False, True])
def test_related_pr_change_during_hydration_is_rejected(queue_inputs, pr_side):
    _, config, data, plan, _ = queue_inputs
    if pr_side:
        data["issues"][1]["body"] = ""
        data["prs"][50].update({"state": "open", "title": "Fixes #1", "merged_at": None})
        data["open_prs"] = [data["prs"][50]]
    light = plan(hydrate=False)
    data["prs"][50]["head"] = {"sha": B}
    def command(argv, **kwargs):
        path = argv[-1].split("repos/fixture/repo/")[1]
        if path == "issues/1":
            return json.dumps(data["issues"][1])
        if "?" in path:
            return "[[]]"
        return json.dumps(data["prs"][50])
    with pytest.raises(ValueError, match="changed"):
        issues.hydrate_issue(light["issues"][0], light, config, command)


def test_unlinked_overlapping_pr_still_blocks_admission_without_changing_digest(queue_inputs):
    root, config, data, plan, confirm = queue_inputs
    _, task, before = confirm()
    data["prs"][99] = {**data["prs"][50], "number": 99, "title": "Unlinked work", "body": "", "state": "open",
                        "files": ["example.py"]}
    data["open_prs"] = [data["prs"][99]]
    after = plan(hydrate=False)
    assert before["issue_inputs"]["1"]["input_digest"] == after["issue_inputs"]["1"]["input_digest"]
    assert after["issue_inputs"]["1"]["overlapping_prs"] == [99]
    assert admission(root, config, after, task) is not None


@pytest.mark.parametrize("status", ["pending", "blocked", "awaiting_delivery_authorization"])
def test_closed_source_retires_task_preserves_evidence_and_releases_scope(queue_inputs, monkeypatch, status):
    root, config, data, plan, confirm = queue_inputs
    path, task, _ = confirm()
    task.update({"status": status, "attempts": ["preserved-attempt"]})
    repair.save(path, task)
    data["issues"][1]["state"] = "closed"
    current = plan(hydrate=False)
    assert current["issue_inputs"]["1"]["state"] == "closed"
    newer = deepcopy(task)
    newer.update({"id": "newer", "status": "pending", "sources": [], "created_at": "2026-12-01T00:00:00+00:00"})
    newer_path = root / "repairs/newer/task.json"
    newer_path.parent.mkdir()
    repair.save(newer_path, newer)
    calls = []
    def resume(*args, **kwargs):
        calls.append(kwargs["_task_id"])
        return {"id": kwargs["_task_id"], "status": "pending"}
    monkeypatch.setattr(repair, "repair_one", resume)
    result = repair.run_repairs(root, {**config, "max_repairs": 1, "issue_intake_ready": True,
                                       "issue_target": A, "issue_inputs": current["issue_inputs"]},
                                root, time.monotonic() + 300, lambda *args: pytest.fail("no agent"))
    saved = json.loads(path.read_text())
    assert saved["status"] == "source_closed" and saved["attempts"] == task["attempts"]
    assert saved["finding"] == task["finding"] and saved["evidence"] == task["evidence"]
    assert saved["source_closed"]["previous_status"] == status
    assert calls == ["newer"] and result["processed"] == ["newer"]
    assert result["inventory"]["source_closed"] == [task["id"]]
    assert all(item["id"] != task["id"] for item in result["manual_todo"])
    receipt = {"repair": result}
    review.save_daily_summary(root, receipt)
    assert receipt["daily_summary"]["source_closed"] == [task["id"]]


@pytest.mark.parametrize("case", ["missing", "not_ready", "mixed_open", "review_source"])
def test_absence_or_partial_closure_does_not_retire_task(queue_inputs, case):
    root, config, _, _, confirm = queue_inputs
    path, task, _ = confirm()
    current = {"1": {"state": "closed"}}
    if case == "missing":
        current = {}
    elif case == "mixed_open":
        task["sources"].append({"kind": "issue", "number": 2})
        current["2"] = {"state": "open"}
    elif case == "review_source":
        task["sources"].append({"kind": "review", "commit": A})
    repair.save(path, task)
    assert repair.reconcile_closed_sources(root, {**config, "issue_intake_ready": case != "not_ready",
                                                   "issue_inputs": current}) == []
    assert json.loads(path.read_text())["status"] == "pending"


@pytest.mark.parametrize("failure", [RuntimeError("association temporarily unavailable"),
                                     subprocess.TimeoutExpired("fixture-gh", 180)])
def test_one_failed_association_read_does_not_invalidate_other_issue_inputs(queue_inputs, monkeypatch, failure):
    _, config, data, plan, _ = queue_inputs
    config["max_issues"] = 2
    data["issues"][2] = {**data["issues"][1], "number": 2, "body": ""}
    original = issues.github_api
    def factory(*args, **kwargs):
        api = original(*args, **kwargs)
        def fetch(path, **options):
            if path.startswith("issues/1/comments?"):
                raise failure
            return api(path, **options)
        return fetch
    monkeypatch.setattr(issues, "github_api", factory)
    current = plan(hydrate=False, deadline=time.monotonic() + 600)
    assert current["issue_inputs"]["1"]["state"] == "unknown"
    assert current["issue_inputs"]["2"]["state"] == "open"
    assert current["issue_inputs"]["2"]["input_digest"]
    failed = next(item for item in current["issues"] if item["number"] == 1)
    with pytest.raises(ValueError, match="identity snapshot failed"):
        issues.hydrate_issue(failed, current, config, lambda *args: pytest.fail("no snapshot retry"))


@pytest.mark.parametrize("failure", [TimeoutError("issue budget exhausted"),
                                     subprocess.TimeoutExpired("fixture-gh", 180)])
def test_exhausted_budget_still_aborts_issue_planning(queue_inputs, monkeypatch, failure):
    _, _, _, plan, _ = queue_inputs
    clock = [0]
    monkeypatch.setattr(issues.time, "monotonic", lambda: clock[0])
    original = issues.github_api
    def factory(*args, **kwargs):
        api = original(*args, **kwargs)
        def fetch(path, **options):
            if path.startswith("issues/1/comments?"):
                clock[0] = 601
                raise failure
            return api(path, **options)
        return fetch
    monkeypatch.setattr(issues, "github_api", factory)
    with pytest.raises((TimeoutError, subprocess.TimeoutExpired)):
        plan(hydrate=False, deadline=600)


def test_missing_source_request_timeout_does_not_hide_other_issue_inputs(queue_inputs, monkeypatch):
    root, config, data, plan, confirm = queue_inputs
    path, _, _ = confirm()
    data["issues"][1]["state"] = "closed"
    data["issues"][2] = {**data["issues"][1], "number": 2, "state": "open", "body": ""}
    original = issues.github_api
    def factory(*args, **kwargs):
        api = original(*args, **kwargs)
        def fetch(path, **options):
            if path == "issues/1":
                raise subprocess.TimeoutExpired("fixture-gh", 180)
            return api(path, **options)
        return fetch
    monkeypatch.setattr(issues, "github_api", factory)
    current = plan(hydrate=False, deadline=time.monotonic() + 600)
    assert current["issue_inputs"]["1"]["state"] == "unknown"
    assert current["issue_inputs"]["2"]["state"] == "open"
    assert current["issue_inputs"]["2"]["input_digest"]
    live = {**config, "issue_intake_ready": True, "issue_inputs": current["issue_inputs"]}
    assert repair.reconcile_closed_sources(root, live) == []
    assert json.loads(path.read_text())["status"] == "pending"


def test_missing_open_issue_without_positive_closed_readback_is_not_retired(queue_inputs, monkeypatch):
    root, config, data, plan, confirm = queue_inputs
    path, _, _ = confirm()
    data["issues"][1]["state"] = "closed"
    original = issues.github_api
    def factory(*args, **kwargs):
        api = original(*args, **kwargs)
        return lambda path, **options: {"ok": False} if path == "issues/1" else api(path, **options)
    monkeypatch.setattr(issues, "github_api", factory)
    current = plan(hydrate=False)
    assert current["issue_inputs"]["1"]["state"] == "unknown"
    assert repair.reconcile_closed_sources(root, {**config, "issue_intake_ready": True,
                                                 "issue_inputs": current["issue_inputs"]}) == []
    assert json.loads(path.read_text())["status"] == "pending"


@pytest.mark.parametrize("change", ["digest", "task_id", "assigned", "linked_open", "overlap", "outcome", "unready"])
def test_closed_source_does_not_weaken_guards_for_remaining_open_source(queue_inputs, change):
    _, config, _, _, confirm = queue_inputs
    _, task, _ = confirm()
    task["sources"].append({"kind": "issue", "number": 2})
    current = {"1": {"state": "closed"}, "2": {"state": "open", "input_digest": "verified"}}
    latest = {"2": {"outcome": "confirmed", "triage": {"decision": "worthwhile", "reason": "project defect", "third_party_service": False}, "repair_task": task["id"], "input_digest": "verified"}}
    live = {**config, "issue_target": A, "issue_intake_ready": True, "issue_inputs": current}
    assert repair._issue_repair_matches(task, live, latest)
    if change == "digest":
        current["2"]["input_digest"] = "changed"
    elif change == "task_id":
        latest["2"]["repair_task"] = "another"
    elif change == "outcome":
        latest["2"]["outcome"] = "not_reproduced"
    elif change == "unready":
        live["issue_intake_ready"] = False
    else:
        current["2"]["overlapping_prs" if change == "overlap" else change] = [99] if change == "overlap" else True
    assert not repair._issue_repair_matches(task, live, latest)


def test_rebuilt_same_finding_keeps_task_identity_but_other_task_is_rejected(queue_inputs):
    root, config, _, plan, confirm = queue_inputs
    _, task, before = confirm()
    assert confirm()[1]["id"] == task["id"]
    current = plan(hydrate=False)
    assert before["issue_inputs"]["1"]["input_digest"] == current["issue_inputs"]["1"]["input_digest"]
    assert admission(root, config, current, task) is None
    other = {**task, "id": "different-task"}
    assert admission(root, config, current, other) is not None
