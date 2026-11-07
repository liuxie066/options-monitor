from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from scripts import code_health_repair as repair
from scripts import code_health_review as review


A = "a" * 40


def finding():
    return {
        "key": "example:add:wrong-sum", "title": "add returns a subtraction",
        "commit": A, "owner_path": "src/example.py", "failure_scenario": "add(2, 1)",
        "expected": "3", "actual": "1", "evidence_log": "probe.log",
        "fix_scope": ["src/example.py", "tests/test_example.py"],
    }


def test_only_confirmed_findings_with_persisted_evidence_enter_queue(tmp_path):
    run = tmp_path / "run"
    (run / "work").mkdir(parents=True)
    payload = {"confirmed_findings": [finding()]}
    with pytest.raises(ValueError, match="evidence"):
        repair.persist_findings(tmp_path, run, payload, A)
    (run / "work/probe.log").write_text("assert add(2, 1) == 3; actual=1\n")
    first = repair.persist_findings(tmp_path, run, payload, A)
    second = repair.persist_findings(tmp_path, run, payload, A)
    assert first == second
    assert len(list((tmp_path / "repairs").glob("*/task.json"))) == 1
    assert (tmp_path / "repairs" / first[0] / "issue-draft.md").is_file()


def test_confirmed_result_cannot_reference_an_unreviewed_commit():
    with pytest.raises(ValueError, match="completed"):
        review.validate_finding(finding(), [])


@pytest.mark.parametrize("bad_path", ["../outside", "/absolute", ".git/config"])
def test_finding_paths_cannot_escape_the_scoped_task(bad_path):
    data = finding()
    data["evidence_log"] = bad_path
    with pytest.raises(ValueError, match="unsafe"):
        review.validate_finding(data, [A])


@pytest.mark.parametrize("absolute_evidence", [False, True])
def test_commit_findings_normalize_work_evidence_before_persistence(tmp_path, absolute_evidence):
    stage = tmp_path / "Application Support" / "run"
    work = stage / "work"
    (work / "logs").mkdir(parents=True)
    evidence = work / "logs" / "probe.log"
    evidence.write_text("assert add(2, 1) == 3; actual=1\n")
    data = finding()
    data["evidence_log"] = str(evidence) if absolute_evidence else "logs/probe.log"
    payload = {"outcome": "complete", "completed_commits": [A], "report_markdown": "evidence report " * 30,
               "verification_gaps": [], "confirmed_findings": [data]}
    assert review.validate_result(payload, [A], work=work) == [A]
    task_ids = repair.persist_findings(tmp_path, stage, payload, A)
    task = json.loads((tmp_path / "repairs" / task_ids[0] / "task.json").read_text())
    assert data["evidence_log"] == task["finding"]["evidence_log"] == "logs/probe.log"
    assert task["finding"]["owner_path"] == "src/example.py"
    assert task["finding"]["fix_scope"] == ["src/example.py", "tests/test_example.py"]


@pytest.mark.parametrize("kind", ["outside", "sibling_prefix", "parent", "git", "escape_symlink", "git_symlink"])
def test_absolute_evidence_rejects_unsafe_paths_even_with_work_context(tmp_path, kind):
    work = tmp_path / "work"
    work.mkdir()
    if kind == "outside":
        evidence = tmp_path / "outside.log"
    elif kind == "sibling_prefix":
        evidence = tmp_path / "work-other" / "probe.log"
    elif kind == "parent":
        evidence = work / "logs" / ".." / "probe.log"
    elif kind == "git":
        evidence = work / ".git" / "probe.log"
    elif kind == "escape_symlink":
        (work / "logs").symlink_to(tmp_path, target_is_directory=True)
        evidence = work / "logs" / "outside.log"
    else:
        (work / ".git").mkdir()
        (work / "logs").symlink_to(work / ".git", target_is_directory=True)
        evidence = work / "logs" / "probe.log"
    data = finding()
    data["evidence_log"] = str(evidence)
    with pytest.raises(ValueError, match="unsafe"):
        review.validate_finding(data, [A], work=work)
    assert data["evidence_log"] == str(evidence)


@pytest.mark.parametrize("field", ["owner_path", "fix_scope"])
def test_work_context_does_not_allow_absolute_owner_or_scope(tmp_path, field):
    data = finding()
    absolute = str(tmp_path / "example.py")
    data[field] = [absolute] if field == "fix_scope" else absolute
    with pytest.raises(ValueError, match="unsafe|relative"):
        review.validate_finding(data, [A], work=tmp_path)


def test_absolute_evidence_resolves_work_and_file_symlinks(tmp_path):
    work = tmp_path / "work"
    (work / "logs").mkdir(parents=True)
    (work / "logs" / "probe.log").write_text("persisted evidence")
    (work / "alias.log").symlink_to(work / "logs" / "probe.log")
    alias = tmp_path / "work-alias"
    alias.symlink_to(work, target_is_directory=True)
    data = finding()
    data["evidence_log"] = str(alias / "alias.log")
    review.validate_finding(data, [A], work=alias)
    assert data["evidence_log"] == "logs/probe.log"


def test_repair_rejects_changed_scope_and_regression_options():
    with pytest.raises(ValueError, match="outside"):
        repair.check_scope(["VERSION"], finding()["fix_scope"])
    bad = {"outcome": "reproduced", "report_markdown": "done", "test_paths": ["tests/test_example.py"], "selectors": ["--ignore=tests"]}
    with pytest.raises(ValueError, match="selectors"):
        repair.validate_reproduction(bad, finding()["fix_scope"])


def test_test_pair_requires_same_cases_and_actual_behavior_failure():
    baseline = {"cases": [("tests.test_example", "test_add")], "failures": 1, "errors": 0, "skipped": 0}
    fixed = {**baseline, "failures": 0}
    repair.validate_test_pair(baseline, fixed)
    for broken in [{**baseline, "errors": 1}, {**baseline, "skipped": 1}, {**baseline, "failures": 0}]:
        with pytest.raises(ValueError, match="baseline"):
            repair.validate_test_pair(broken, fixed)
    with pytest.raises(ValueError, match="identical"):
        repair.validate_test_pair(baseline, {**fixed, "cases": []})


def git(tree, *args):
    return subprocess.check_output(["git", "-C", str(tree), *args], text=True).strip()


def local_repository(tmp_path, *, advance_target=False):
    source = tmp_path / "seed"
    source.mkdir()
    git(source, "init", "-q")
    (source / "src").mkdir()
    (source / "src/__init__.py").write_text("")
    (source / "src/example.py").write_text("def add(a, b):\n    return a - b\n")
    git(source, "add", ".")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "test fixture")
    base = git(source, "rev-parse", "HEAD")
    if advance_target:
        (source / "unrelated.txt").write_text("a newer main target with the same defect\n")
        git(source, "add", "unrelated.txt")
        git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "advance target")
    root = tmp_path / "state"
    root.mkdir()
    subprocess.run(["git", "clone", "--bare", "-q", str(source), str(root / "repository.git")], check=True)
    config = {"git": "git", "verification_python": sys.executable, "verification_source_sha": base,
              "timeout_seconds": 120, "sandbox_exec": "/usr/bin/sandbox-exec"}
    run = root / "review"
    (run / "work").mkdir(parents=True)
    (run / "work/probe.log").write_text("assert add(2, 1) == 3, actual=1\n")
    item = finding()
    item["commit"] = base
    task_id = repair.persist_findings(root, run, {"confirmed_findings": [item]}, base)[0]
    return root, config, run, task_id


@pytest.mark.parametrize("interrupt_stage", [None, "implement", "verify"])
@pytest.mark.parametrize("issue_origin", [False, True, "stale"])
def test_same_run_reproduces_repairs_verifies_and_independently_reviews_without_delivery(tmp_path, monkeypatch, interrupt_stage, issue_origin):
    root, config, run, task_id = local_repository(tmp_path, advance_target=issue_origin == "stale")
    if issue_origin:
        record = root / "repairs" / task_id / "task.json"
        task = json.loads(record.read_text())
        task["sources"] = [{"kind": "issue", "number": 1, "verification_sha": task["base_sha"]}]
        if issue_origin == "stale":
            task["attempts"] = [str(record.parent / "earlier-preparation")]
        repair.save(record, task)
        repair.save(root / "issue-state.json", {"issues": {"1": {
            "outcome": "confirmed", "target": task["base_sha"], "repair_task": task_id, "run_dir": str(run), "input_digest": "unchanged",
        }}})
        config.update({"issue_intake_ready": True, "issue_target": git(root / "repository.git", "rev-parse", "HEAD"),
                       "issue_inputs": {"1": {"input_digest": "unchanged", "assigned": False}}})
    record = root / "repairs" / task_id / "task.json"
    original_task = json.loads(record.read_text())
    revalidations = []

    def revalidate(number):
        revalidations.append(number)
        assert number == 1
        assert json.loads(record.read_text())["base_sha"] == original_task["base_sha"]
        stage = run / "revalidation"
        (stage / "work").mkdir(parents=True)
        (stage / "work/probe.log").write_text("the defect still reproduces at the current target\n")
        repair.persist_findings(root, stage, {"confirmed_findings": [original_task["finding"]]}, config["issue_target"],
                                source={"kind": "issue", "number": number, "verification_sha": config["issue_target"]})
        repair.save(root / "issue-state.json", {"issues": {"1": {
            "outcome": "confirmed", "target": config["issue_target"], "repair_task": task_id, "input_digest": "unchanged",
        }}})

    calls, commands = [], []
    original_run = repair.run

    def observe_command(argv, **kwargs):
        commands.append(argv)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(repair, "run", observe_command)
    # Only this integration fixture replaces the OS sandbox. Production command construction is tested below.
    monkeypatch.setattr(repair, "sandbox_command", lambda config, tree, stage, argv: argv)

    def invoke(_config, stage, _prompt, *, result_schema, writable=None):
        stage.mkdir(parents=True)
        calls.append((result_schema, writable))
        tree = root / "repairs" / task_id / "worktree"
        if result_schema is repair.REPRO_SCHEMA:
            (tree / "tests").mkdir()
            (tree / "tests/test_example.py").write_text("from src.example import add\n\ndef test_add():\n    assert add(2, 1) == 3\n")
            return {"outcome": "reproduced", "report_markdown": "real regression", "test_paths": ["tests/test_example.py"], "selectors": ["tests/test_example.py::test_add"]}
        if result_schema is repair.FIX_SCHEMA:
            (tree / "src/example.py").write_text("def add(a, b):\n    return a + b\n")
            return {"outcome": "fixed", "report_markdown": "owner fixed", "pr_title": "fix: correct add", "pr_body": "Adds regression and fixes operator."}
        assert result_schema is repair.AUDIT_SCHEMA
        assert writable is None
        assert (tree / "tests/test_example.py").read_text().endswith("assert add(2, 1) == 3\n")
        return {"verdict": "approve", "report_markdown": "independent review", "findings": []}

    interrupted = False
    original_verify = repair.verify_tests

    def interrupted_verify(*args, **kwargs):
        nonlocal interrupted
        if interrupt_stage == "verify" and not interrupted and args[2].name == "fixed":
            interrupted = True
            raise TimeoutError("daily budget expired during verification")
        return original_verify(*args, **kwargs)

    def interrupted_invoke(*args, **kwargs):
        nonlocal interrupted
        if interrupt_stage == "implement" and not interrupted and kwargs.get("result_schema") is repair.FIX_SCHEMA:
            interrupted = True
            raise TimeoutError("daily budget expired during model execution")
        return invoke(*args, **kwargs)

    monkeypatch.setattr(repair, "verify_tests", interrupted_verify)
    outcome = repair.repair_one(root, config, run, time.monotonic() + 120, interrupted_invoke, revalidate_issue=revalidate)
    if interrupt_stage:
        assert outcome["status"] == "pending"
        assert interrupted
        outcome = repair.repair_one(root, config, run, time.monotonic() + 120, interrupted_invoke, revalidate_issue=revalidate)
    assert outcome["status"] == "awaiting_delivery_authorization"
    task = json.loads((root / "repairs" / task_id / "task.json").read_text())
    assert task["baseline"]["failures"] == 1 and task["fixed_verification"]["failures"] == 0
    assert task["finding"] == original_task["finding"] and task["evidence"] == original_task["evidence"]
    assert task["attempts"][:len(original_task["attempts"])] == original_task["attempts"]
    assert revalidations == ([1] if issue_origin == "stale" else [])
    if issue_origin == "stale":
        assert task["base_sha"] == config["issue_target"] != original_task["base_sha"]
        assert task["related_evidence"][-1] == str(run / "revalidation/work/probe.log")
        assert task["sources"][-1]["verification_sha"] == config["issue_target"]
        assert task["revalidations"] == [{
            "from_base_sha": original_task["base_sha"], "target": config["issue_target"], "issues": [1],
            "revalidated_at": task["revalidations"][0]["revalidated_at"], "run_dir": str(run),
        }]
    assert (root / "repairs" / task_id / "pr-draft.md").is_file()
    assert git(Path(task["worktree"]), "rev-parse", "HEAD") == task["base_sha"]
    assert len(calls) == 3
    assert not any(any(arg in {"push", "commit", "merge", "add"} for arg in argv[2:]) for argv in commands if "worktree" not in argv)
    assert repair.repair_one(root, config, run, time.monotonic() + 120, invoke)["status"] == "no_pending_repairs"


@pytest.fixture
def stale_issue_task(tmp_path):
    root, config, run, task_id = local_repository(tmp_path, advance_target=True)
    record = root / "repairs" / task_id / "task.json"
    task = json.loads(record.read_text())
    task["sources"] = [{"kind": "issue", "number": 1, "verification_sha": task["base_sha"]}]
    task["attempts"] = [str(record.parent / "earlier-preparation")]
    repair.save(record, task)
    repair.save(root / "issue-state.json", {"issues": {"1": {
        "outcome": "confirmed", "target": task["base_sha"], "repair_task": task_id, "input_digest": "unchanged",
    }}})
    config.update({"issue_intake_ready": True, "issue_target": git(root / "repository.git", "rev-parse", "HEAD"),
                   "issue_inputs": {"1": {"input_digest": "unchanged", "assigned": False}}})
    return root, config, run, record


def existing_issue_worktree(root, config, record, *, elsewhere=False, dirty=True):
    task = json.loads(record.read_text())
    tree = root / "preserved-work" if elsewhere else record.parent / "worktree"
    repair.run([config["git"], "--git-dir", str(root / "repository.git"), "worktree", "add", "--detach", str(tree), task["base_sha"]])
    if dirty:
        (tree / "src/example.py").write_text("local implementation in progress\n")
        (tree / "local-notes.txt").write_text("untracked work must survive\n")
    task["worktree"] = str(tree)
    repair.save(record, task)
    return tree


def unexpected_repair(*args, **kwargs):
    pytest.fail("deferred work must not invoke repair or revalidation")


def test_stale_issue_without_revalidator_defers_without_changing_task(stale_issue_task):
    root, config, run, record = stale_issue_task
    before = record.read_bytes()
    result = repair.repair_one(root, config, run, time.monotonic() + 120, unexpected_repair)
    assert result["status"] == "deferred" and "revalidation" in result["tasks"][0]["reason"]
    assert record.read_bytes() == before and not (record.parent / "worktree").exists()


@pytest.mark.parametrize("failure", ["error", "timeout", "unchanged", "budget"])
@pytest.mark.parametrize("has_worktree", [False, True])
def test_revalidation_failure_defers_and_preserves_task_and_worktree(stale_issue_task, monkeypatch, failure, has_worktree):
    root, config, run, record = stale_issue_task
    tree = existing_issue_worktree(root, config, record) if has_worktree else None
    before = record.read_bytes()
    previous_head = git(tree, "rev-parse", "HEAD") if tree else None
    calls = []
    deadline = time.monotonic() + 120

    def revalidate(number):
        calls.append(number)
        if failure == "error":
            raise RuntimeError("issue validation failed")
        if failure == "timeout":
            raise TimeoutError("issue validation timed out")
        if failure == "budget":
            state = json.loads((root / "issue-state.json").read_text())
            state["issues"]["1"]["target"] = config["issue_target"]
            repair.save(root / "issue-state.json", state)
            monkeypatch.setattr(repair.time, "monotonic", lambda: deadline + 1)

    result = repair.repair_one(root, config, run, deadline, unexpected_repair, revalidate_issue=revalidate)
    assert result["status"] == "deferred" and calls == [1]
    assert record.read_bytes() == before
    if tree:
        assert git(tree, "rev-parse", "HEAD") == previous_head
        assert (tree / "src/example.py").read_text() == "local implementation in progress\n"
        assert (tree / "local-notes.txt").read_text() == "untracked work must survive\n"
    else:
        assert not (record.parent / "worktree").exists()


@pytest.mark.parametrize("change", ["in_progress", "needs_decision", "blocked", "already_fixed", "not_reproduced", "duplicate", "owner", "digest", "assigned"])
def test_revalidation_reloads_verdict_ownership_and_inputs(stale_issue_task, change):
    root, config, run, record = stale_issue_task
    before = record.read_bytes()
    calls = []

    def revalidate(number):
        calls.append(number)
        state = json.loads((root / "issue-state.json").read_text())
        latest = state["issues"][str(number)]
        latest["target"] = config["issue_target"]
        if change == "owner":
            latest["repair_task"] = "another-repair"
        elif change == "digest":
            latest["input_digest"] = "changed"
        elif change == "assigned":
            config["issue_inputs"]["1"]["assigned"] = True
        else:
            latest["outcome"] = change
        repair.save(root / "issue-state.json", state)

    result = repair.repair_one(root, config, run, time.monotonic() + 120, unexpected_repair, revalidate_issue=revalidate)
    assert result["status"] == "deferred" and calls == [1]
    assert record.read_bytes() == before and not (record.parent / "worktree").exists()


@pytest.mark.parametrize("change", ["no_intake", "assigned", "digest", "owner", "unconfirmed", "no_input", "no_target", "second_source"])
def test_revalidator_requires_unchanged_confirmed_issue_identity(stale_issue_task, change):
    root, config, run, record = stale_issue_task
    state = json.loads((root / "issue-state.json").read_text())
    if change == "no_intake":
        config["issue_intake_ready"] = False
    elif change == "assigned":
        config["issue_inputs"]["1"]["assigned"] = True
    elif change == "digest":
        config["issue_inputs"]["1"]["input_digest"] = "changed"
    elif change == "owner":
        state["issues"]["1"]["repair_task"] = "another-repair"
    elif change == "unconfirmed":
        state["issues"]["1"]["outcome"] = "needs_decision"
    elif change == "no_input":
        config["issue_inputs"] = {}
    elif change == "no_target":
        config.pop("issue_target")
    else:
        task = json.loads(record.read_text())
        task["sources"].append({"kind": "issue", "number": 2})
        repair.save(record, task)
    repair.save(root / "issue-state.json", state)
    before = record.read_bytes()
    result = repair.repair_one(root, config, run, time.monotonic() + 120, unexpected_repair, revalidate_issue=unexpected_repair)
    assert result["status"] == "deferred" and record.read_bytes() == before


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("elsewhere,dirty", [(False, False), (False, True), (True, True)])
def test_old_worktree_requires_human_baseline_migration(stale_issue_task, stale, elsewhere, dirty):
    root, config, run, record = stale_issue_task
    tree = existing_issue_worktree(root, config, record, elsewhere=elsewhere, dirty=dirty)
    before = record.read_bytes()
    previous_head = git(tree, "rev-parse", "HEAD")
    previous_status = git(tree, "status", "--porcelain")
    calls = []

    def revalidate(number):
        calls.append(number)
        state = json.loads((root / "issue-state.json").read_text())
        state["issues"]["1"]["target"] = config["issue_target"]
        repair.save(root / "issue-state.json", state)

    if not stale:
        revalidate(1)
        calls.clear()
    result = repair.repair_one(root, config, run, time.monotonic() + 120, unexpected_repair, revalidate_issue=revalidate)
    assert result["status"] == "deferred"
    assert "human baseline migration required" in result["tasks"][0]["reason"]
    assert calls == ([1] if stale else []) and record.read_bytes() == before
    assert git(tree, "rev-parse", "HEAD") == previous_head
    assert git(tree, "status", "--porcelain") == previous_status
    if dirty:
        assert (tree / "src/example.py").read_text() == "local implementation in progress\n"
        assert (tree / "local-notes.txt").read_text() == "untracked work must survive\n"


@pytest.mark.parametrize("stage,implementation", [("implement", None), ("verify", None), ("independent_review", None), ("reproduce", {"outcome": "fixed"})])
def test_implementation_without_worktree_cannot_automatically_change_baseline(stale_issue_task, stage, implementation):
    root, config, run, record = stale_issue_task
    task = json.loads(record.read_text())
    task["stage"] = stage
    if implementation:
        task["implementation"] = implementation
    repair.save(record, task)
    state = json.loads((root / "issue-state.json").read_text())
    state["issues"]["1"]["target"] = config["issue_target"]
    repair.save(root / "issue-state.json", state)
    before = record.read_bytes()
    result = repair.repair_one(root, config, run, time.monotonic() + 120, unexpected_repair, revalidate_issue=unexpected_repair)
    assert result["status"] == "deferred" and "human baseline migration required" in result["tasks"][0]["reason"]
    assert record.read_bytes() == before


def test_current_confirmation_updates_unused_baseline_without_revalidation(stale_issue_task, monkeypatch):
    root, config, run, record = stale_issue_task
    before = json.loads(record.read_text())
    state = json.loads((root / "issue-state.json").read_text())
    state["issues"]["1"]["target"] = config["issue_target"]
    repair.save(root / "issue-state.json", state)

    def stop_before_worktree(*args):
        raise TimeoutError("stop after admission")

    monkeypatch.setattr(repair, "dependency_inputs_match", stop_before_worktree)
    result = repair.repair_one(root, config, run, time.monotonic() + 120, unexpected_repair, revalidate_issue=unexpected_repair)
    after = json.loads(record.read_text())
    assert result["status"] == "pending" and after["base_sha"] == config["issue_target"]
    assert all(after[key] == before[key] for key in ("finding", "evidence", "created_at", "review_run", "sources"))
    assert after["attempts"][:-1] == before["attempts"] and after["revalidations"][0]["from_base_sha"] == before["base_sha"]
    assert not (record.parent / "worktree").exists()


@pytest.mark.parametrize("status", sorted(repair.TERMINAL))
def test_terminal_tasks_are_not_reopened_by_issue_revalidation(stale_issue_task, status):
    root, config, run, record = stale_issue_task
    task = json.loads(record.read_text())
    task["status"] = status
    repair.save(record, task)
    before = record.read_bytes()
    result = repair.repair_one(root, config, run, time.monotonic() + 120, unexpected_repair, revalidate_issue=unexpected_repair)
    assert result["status"] == "no_pending_repairs" and record.read_bytes() == before


def test_scope_conflict_prevents_issue_revalidation(stale_issue_task):
    root, config, run, record = stale_issue_task
    older_id = repair.persist_findings(root, run, {"confirmed_findings": [{**finding(), "key": "older-conflict"}]}, A)[0]
    older_record = root / "repairs" / older_id / "task.json"
    older = json.loads(older_record.read_text())
    older.update({"created_at": "2000-01-01T00:00:00+00:00", "status": "awaiting_delivery_authorization"})
    repair.save(older_record, older)
    before = record.read_bytes()
    result = repair.repair_one(root, config, run, time.monotonic() + 120, unexpected_repair, revalidate_issue=unexpected_repair)
    assert result["status"] == "deferred" and "overlapping earlier local work" in result["tasks"][0]["reason"]
    assert record.read_bytes() == before


def test_revalidation_does_not_start_after_repair_budget_expires(stale_issue_task):
    root, config, run, record = stale_issue_task
    before = record.read_bytes()
    result = repair.repair_one(root, config, run, time.monotonic() - 1, unexpected_repair, revalidate_issue=unexpected_repair)
    assert result["status"] == "deferred" and "budget" in result["tasks"][0]["reason"]
    assert record.read_bytes() == before


def test_expired_budget_preserves_task_for_next_daily_run(tmp_path):
    root, config, run, task_id = local_repository(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("no agent should start without remaining budget")
    outcome = repair.repair_one(root, config, run, time.monotonic() - 1, forbidden)
    task = json.loads((root / "repairs" / task_id / "task.json").read_text())
    assert outcome["status"] == task["status"] == "pending"
    assert task["stage"] == "reproduce"


def test_process_timeout_is_a_resumable_budget_exhaustion(tmp_path, monkeypatch):
    class TimedOutProcess:
        pid = 12345
        returncode = -9

        def __init__(self, *args, **kwargs):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("pytest", timeout)
            return -9

    monkeypatch.setattr(repair.subprocess, "Popen", TimedOutProcess)
    monkeypatch.setattr(repair.os, "killpg", lambda *_: None)
    monkeypatch.setattr(repair, "sandbox_command", lambda config, tree, stage, argv: argv)
    with pytest.raises(TimeoutError, match="budget"):
        repair.verify_tests({"verification_python": sys.executable}, tmp_path, tmp_path / "verify", ["test.py"], 1)


def test_sandbox_denies_network_and_writes_outside_verification_stage(tmp_path):
    config = {"sandbox_exec": "/usr/bin/sandbox-exec"}
    argv = repair.sandbox_command(config, tmp_path, tmp_path, ["python", "-m", "pytest"])
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-f"]
    policy = (tmp_path / "verification.sb").read_text()
    assert "(deny network*)" in policy and "(deny file-write*)" in policy
    assert str(tmp_path.resolve()) in policy
