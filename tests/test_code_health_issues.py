from __future__ import annotations

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
                         "finding": finding() if outcome == "confirmed" else None, **extra}]}


def make_plan(*numbers, **extra):
    return {"target": A, "issues": [{"number": n, "input_digest": str(n), "issue": issue(n),
                                    "comments": [], "timeline": [], "linked_pull_requests": []} for n in numbers],
            "open_pull_requests": [], "local_repairs": [], **extra}


def process(tmp_path, payload, plan=None):
    run = tmp_path / "run"
    (run / "work").mkdir(parents=True, exist_ok=True)
    (run / "work/probe.log").write_text("assert add(2, 1) == 3; actual=1\n")
    return issues.process_issues(tmp_path, {"timeout_seconds": 180}, run, tmp_path / "source", plan or make_plan(1),
                                 time.monotonic() + 180, lambda *a, **k: payload, review.FINDING_SCHEMA, review.validate_finding)


def test_full_discussion_and_linked_work_are_paginated_before_review(tmp_path):
    calls, forbidden = [], []
    open_pr = {"number": 60, "title": "Fix #1", "body": "Fixes #1", "html_url": "https://github.com/fixture/repo/pull/60", "head": {"sha": A}}
    responses = {
        "issues?state=open&sort=created&direction=asc&per_page=100": [[issue(1), issue(2)], [issue(3), issue(4)]],
        "pulls?state=open&per_page=100": [[open_pr]],
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
    assert not any("issues/4" in call[-1] for call in calls)
    repair.save(tmp_path / "issue-state.json", {"issues": {
        str(row["number"]): {"target": A, "input_digest": row["input_digest"], "selection_digest": row["selection_digest"], "outcome": "not_reproduced", "reviewed_at": "2026-11-03"} for row in plan["issues"]}})
    assert [row["number"] for row in issues.plan_issues(tmp_path, config, A, command)["issues"]] == [4]
    assert [row["number"] for row in issues.plan_issues(tmp_path, config, "b" * 40, command)["issues"]] == [4, 1, 2]
    responses["issues?state=open&sort=created&direction=asc&per_page=100"][0][0]["updated_at"] = "later"
    assert [row["number"] for row in issues.plan_issues(tmp_path, config, A, command)["issues"]] == [4, 1]
    pending = tmp_path / "repairs/local/task.json"
    pending.parent.mkdir(parents=True)
    repair.save(pending, {"id": "local", "status": "pending", "sources": [{"kind": "issue", "number": 3}],
                          "finding": {"key": "current-work", "fix_scope": ["example.py"]}})
    assert issues.plan_issues(tmp_path, config, A, command)["issues"][0]["number"] == 3


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
    second = repair.persist_findings(tmp_path, tmp_path / "run", {"confirmed_findings": [second_finding]}, A,
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
    payload["reviews"][0]["finding"]["evidence_log"] = str(tmp_path / "run" / "work" / "probe.log")
    assert process(tmp_path, payload)["status"] == "complete"
    persisted = json.loads((tmp_path / "run" / "result.json").read_text())["reviews"][0]["finding"]
    task_path, = (tmp_path / "repairs").glob("*/task.json")
    task = json.loads(task_path.read_text())
    assert persisted["evidence_log"] == task["finding"]["evidence_log"] == "probe.log"
    assert payload["reviews"][0]["finding"]["evidence_log"] == "probe.log"
    assert task["finding"]["owner_path"] == "example.py"
    assert task["finding"]["fix_scope"] == ["example.py", "tests/test_example.py"]
    state = json.loads((tmp_path / "issue-state.json").read_text())
    assert state["issues"]["1"]["outcome"] == "confirmed"


def test_issue_rejects_absolute_evidence_outside_work_before_persistence(tmp_path):
    outside = tmp_path / "probe.log"
    outside.write_text("out of scope evidence")
    payload = response(outcome="confirmed")
    payload["reviews"][0]["finding"]["evidence_log"] = str(outside)
    with pytest.raises(ValueError, match="unsafe"):
        process(tmp_path, payload)
    assert not (tmp_path / "issue-state.json").exists()
    assert not (tmp_path / "repairs").exists()


def test_issue_report_cannot_assert_confirmation_without_evidence(tmp_path):
    run = tmp_path / "run/work"
    run.mkdir(parents=True)
    with pytest.raises(ValueError, match="durable"):
        issues.process_issues(tmp_path, {}, run.parent, tmp_path, make_plan(1), time.monotonic() + 180,
                              lambda *a, **k: response(outcome="confirmed"), review.FINDING_SCHEMA, review.validate_finding)
    assert not (tmp_path / "issue-state.json").exists()
    assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize("bad", [
    response(number=99), response(outcome="not_reproduced", finding=finding()),
    response(outcome="confirmed", finding={**finding(), "commit": A}),
    {**response(), "reviews": []}, {**response(), "outcome": "blocked"},
])
def test_invalid_decisions_do_not_create_issue_progress(tmp_path, bad):
    with pytest.raises(ValueError):
        process(tmp_path, bad)
    assert not (tmp_path / "issue-state.json").exists()
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
    assert state["repair_task"] == task_id and state["outcome"] == new_outcome
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
    state = {str(n): {"outcome": "confirmed", "target": A, "repair_task": task_id, "input_digest": str(n),
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
        with pytest.raises(TimeoutError):
            issues.process_issues(tmp_path, {}, tmp_path, tmp_path, make_plan(1), deadline, timeout,
                                  review.FINDING_SCHEMA, review.validate_finding)
    assert not (tmp_path / "issue-state.json").exists()


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
    source = work.parent.parent / "source"
    assert (source / "AGENTS.md").read_text().startswith("Frozen")
    assert "禁止网络" in prompt and "comment_draft" in prompt
    payload = {"outcome":"complete", "report_markdown":"issue evidence and limitations " * 15,
               "reviews":[{"number":1,"outcome":"not_reproduced", "report_markdown":"Later correction rejects the original write claim " * 12,
                           "comment_draft":"Current SHA only; not merged/released", "finding":None}]}
else:
    plan = json.loads((work.parent / "plan.json").read_text())
    payload = {"outcome":"complete", "completed_commits":[row["sha"] for row in plan["commits"]],
               "report_markdown":"review evidence and coverage " * 15,"verification_gaps":[],"confirmed_findings":[]}
output.write_text(json.dumps(payload))
''')
    agent.chmod(0o700)
    (root / "config.json").write_text(json.dumps({
        "git": "git", "gh": "github-client", "codex": str(agent), "repository": "fixture/repo",
        "repository_url": str(source), "max_units": 3, "timeout_seconds": 180, "issue_timeout_seconds": 60,
        "issues_enabled": True, "repair_enabled": True, "model": "fixture",
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

    def repair_once(*args):
        repairs.append(args[0])
        return {"status": "no_pending_repairs"}

    monkeypatch.setattr(review, "command", external)
    monkeypatch.setattr(repair, "repair_one", repair_once)
    original_invoke = review.invoke_agent
    if mode == "archive_budget":
        monkeypatch.setattr(review.time, "monotonic", lambda: clock[0])
    if mode == "review_timeout":
        def timeout_commit(config, stage, *args, **kwargs):
            if stage.name != "issues":
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
    assert len(repairs) == (1 if mode in {"run", "review_timeout", "intake_timeout", "reserved_budget"} else 0)
    actual = json.loads((root / "state.json").read_text())
    assert actual["cursor"] == (target if mode in {"run", "intake_timeout", "budget_exhausted"} and has_commit else base)
    if mode in {"run", "review_timeout", "reserved_budget"}:
        assert receipt["issues"]["reviewed"] == [1]
        saved = json.loads((root / "issue-state.json").read_text())["issues"]["1"]
        assert saved["outcome"] == "not_reproduced" and saved["target"] == target
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
        assert not (root / "issue-state.json").exists()
        if mode == "archive_budget":
            assert receipt["status"] == ("partial" if has_commit else "no_changes") and not repairs
            assert receipt["cursor_after"] == base
            assert receipt["repair"]["status"] == "pending"
            # Frozen source is prepared before metadata or issue intake can exhaust their budgets.
            assert any(call[0] == "git" and "archive" in call for call in calls)
        if mode in {"intake_timeout", "budget_exhausted"}:
            assert receipt["issues"]["status"] == "pending"
        if mode == "budget_exhausted":
            assert receipt["status"] == ("complete" if has_commit else "no_changes") and not repairs
            assert receipt["repair"]["status"] == "pending"
    if mode == "smoke":
        assert not any(call[0] == "github-client" for call in calls)
    assert git("status", "--porcelain") == ""
