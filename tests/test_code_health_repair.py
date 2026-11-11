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
            "outcome": "confirmed", "triage": {"decision": "worthwhile", "reason": "project defect", "third_party_service": False}, "target": task["base_sha"], "repair_task": task_id, "run_dir": str(run), "input_digest": "unchanged",
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
            "outcome": "confirmed", "triage": {"decision": "worthwhile", "reason": "project defect", "third_party_service": False}, "target": config["issue_target"], "repair_task": task_id, "input_digest": "unchanged",
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
        return {"verdict": "approve", "report_markdown": "independent review", "findings": [], "third_party_service": False}

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
        "outcome": "confirmed", "triage": {"decision": "worthwhile", "reason": "project defect", "third_party_service": False}, "target": task["base_sha"], "repair_task": task_id, "input_digest": "unchanged",
    }}})
    config.update({"issue_intake_ready": True, "issue_target": git(root / "repository.git", "rev-parse", "HEAD"),
                   "issue_inputs": {"1": {"input_digest": "unchanged", "assigned": False}}})
    return root, config, run, record


@pytest.mark.parametrize("active_current", [True, False])
def test_closed_source_target_does_not_override_active_source_target(stale_issue_task, monkeypatch, active_current):
    root, config, run, record = stale_issue_task
    task = json.loads(record.read_text())
    task["sources"].append({"kind": "issue", "number": 2, "verification_sha": config["issue_target"]})
    repair.save(record, task)
    state = json.loads((root / "issue-state.json").read_text())
    state["issues"]["2"] = {"outcome": "confirmed", "triage": {"decision": "worthwhile", "reason": "project defect", "third_party_service": False}, "repair_task": task["id"], "input_digest": "active",
                             "target": config["issue_target"] if active_current else task["base_sha"]}
    repair.save(root / "issue-state.json", state)
    config["issue_inputs"] = {"1": {"state": "closed"},
                              "2": {"state": "open", "input_digest": "active", "assigned": False}}
    checked = []
    def stop_at_dependency_check(config, root, target):
        checked.append(target)
        raise ValueError("dependency verification reached")
    monkeypatch.setattr(repair, "dependency_inputs_match", stop_at_dependency_check)
    result = repair.repair_one(root, config, run, time.monotonic() + 120, unexpected_repair)
    assert checked == ([config["issue_target"]] if active_current else [])
    assert result["status"] == ("blocked" if active_current else "deferred")
    if active_current:
        assert json.loads(record.read_text())["revalidations"][-1]["issues"] == [2]


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


def test_revalidation_no_output_propagates_instead_of_deferring(stale_issue_task):
    root, config, run, record = stale_issue_task
    before = record.read_bytes()
    diagnostics = {"failure_kind": "agent_no_output", "model_output_events": 0}

    def revalidate(number):
        raise repair.AgentNoOutput(f"issue #{number} revalidation produced no model output", diagnostics=diagnostics)

    with pytest.raises(repair.AgentNoOutput) as excinfo:
        repair.repair_one(root, config, run, time.monotonic() + 120, unexpected_repair, revalidate_issue=revalidate)
    assert excinfo.value.diagnostics == diagnostics
    assert record.read_bytes() == before and not (record.parent / "worktree").exists()


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


def queued_repairs(tmp_path, count=4):
    """Existing local records only: no repository history, subprocesses or agent execution."""
    root = tmp_path / "state"
    run = root / "run"
    (run / "work").mkdir(parents=True)
    (run / "work/probe.log").write_text("recorded reproduction evidence\n")
    paths = []
    for index in range(count):
        item = {**finding(), "key": f"task-{index}", "owner_path": f"src/task_{index}.py",
                "fix_scope": [f"src/task_{index}.py", f"tests/test_task_{index}.py"]}
        task_id = repair.persist_findings(root, run, {"confirmed_findings": [item]}, A)[0]
        path = root / "repairs" / task_id / "task.json"
        task = json.loads(path.read_text())
        task["created_at"] = f"2026-01-{index + 1:02d}T00:00:00+00:00"
        repair.save(path, task)
        paths.append(path)
    return root, {"timeout_seconds": 900, "verification_python": sys.executable}, run, paths


def fake_local_attempt(monkeypatch, *, status="pending"):
    def run_one(root, config, run_dir, deadline, invoke, *, revalidate_issue=None, _task_id=None):
        path = root / "repairs" / _task_id / "task.json"
        task = json.loads(path.read_text())
        task["attempts"].append(f"retained-attempt-{len(task['attempts']) + 1}")
        task["status"] = status
        repair.save(path, task)
        return {"id": _task_id, "status": status, "stage": task["stage"], "record": str(path)}
    monkeypatch.setattr(repair, "repair_one", run_one)


def test_run_repairs_defaults_prioritize_unattempted_then_oldest_attempt_without_recreating_tasks(tmp_path, monkeypatch):
    root, config, run, paths = queued_repairs(tmp_path, 5)
    for index, at in [(0, "2026-03-03"), (2, "2026-03-01"), (4, "2026-03-02")]:
        task = json.loads(paths[index].read_text())
        task.update({"attempts": [f"kept-{index}"], "last_attempted_at": at + "T00:00:00+00:00"})
        repair.save(paths[index], task)
    before = [json.loads(path.read_text()) for path in paths]
    fake_local_attempt(monkeypatch)
    result = repair.run_repairs(root, config, run, time.monotonic() + 3000, unexpected_repair)
    expected = [before[index]["id"] for index in (1, 3, 2)]
    assert result["entered"] is True and result["status"] == "partial"
    assert result["processed"] == result["attempted"] == expected
    assert [row["id"] for row in result["results"]] == expected
    assert result["local_verified"] == [] and len(result["inventory"]["pending"]) == 5
    assert len(list((root / "repairs").glob("*/task.json"))) == 5
    for path, original in zip(paths, before):
        latest = json.loads(path.read_text())
        for field in ("id", "finding", "sources", "created_at", "branch", "base_sha", "evidence"):
            assert (field in latest) == (field in original)
            assert latest.get(field) == original.get(field)
        assert latest["attempts"][:len(original["attempts"])] == original["attempts"]


@pytest.mark.parametrize("limit", [0, 1, 5])
def test_run_repairs_configured_max_never_selects_a_task_twice(tmp_path, monkeypatch, limit):
    root, config, run, paths = queued_repairs(tmp_path)
    fake_local_attempt(monkeypatch)
    result = repair.run_repairs(root, {**config, "max_repairs": limit}, run, time.monotonic() + 3000, unexpected_repair)
    assert len(result["results"]) == len(result["attempted"]) == min(limit, len(paths))
    assert len(set(result["processed"])) == len(result["processed"])
    assert all(len(json.loads(path.read_text())["attempts"]) <= 1 for path in paths)


def test_attempt_fairness_reads_legacy_attempt_timestamp_instead_of_later_intake_update():
    task = {"id": "legacy", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-11-07T00:00:00+00:00",
            "attempts": ["/retained/attempts/20260201T010203000004Z-123"]}
    assert repair._attempt_order((None, task))[1].isoformat() == "2026-02-01T01:02:03.000004+00:00"


def fake_repair_tree(root, monkeypatch):
    for record in (root / "repairs").glob("*/task.json"):
        task = json.loads(record.read_text())
        tree = record.parent / "worktree"
        tree.mkdir()
        source = tree / task["finding"]["owner_path"]
        source.parent.mkdir()
        source.write_text("def add(a, b):\n    return a - b\n")
        task.update({"worktree": str(tree), "branch": "codex/health-fix-" + task["id"]})
        repair.save(record, task)

    def git(config, tree, *args):
        if args == ("rev-parse", "HEAD"):
            return json.loads((tree.parent / "task.json").read_text())["base_sha"]
        return "local diff evidence"

    monkeypatch.setattr(repair, "dependency_inputs_match", lambda *args: None)
    monkeypatch.setattr(repair, "git", git)
    monkeypatch.setattr(repair, "changed_paths", lambda config, tree, **kwargs: sorted(
        str(p.relative_to(tree)) for p in tree.rglob("*.py")
        if str(p.relative_to(tree)).startswith("tests/") or p.read_text() != "def add(a, b):\n    return a - b\n"))


def test_run_repairs_agent_no_output_aborts_queue_after_first_unit(tmp_path, monkeypatch):
    root, config, run, paths = queued_repairs(tmp_path, 3)
    fake_repair_tree(root, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(repair.time, "monotonic", lambda: clock[0])
    calls = []

    def no_output(config, *args, **kwargs):
        calls.append(config["timeout_seconds"])
        raise repair.AgentTimeout(
            "no output", diagnostics={"failure_kind": "agent_no_output", "model_output_events": 0})

    result = repair.run_repairs(root, {**config, "repair_unit_timeout_seconds": 60}, run, 1300, no_output)
    assert calls == [60]
    assert len(result["processed"]) == 1
    assert result["attempted"] == result["processed"]
    assert result["agent_unavailable"]["task"] == result["processed"][0]
    assert result["results"][0]["failure_kind"] == "agent_no_output"
    assert result["results"][0]["diagnostics"]["failure_kind"] == "agent_no_output"
    reasons = {item["id"]: item["reason"] for item in result["deferred"]}
    assert len(reasons) == 2 and all("agent_no_output" in reason for reason in reasons.values())


@pytest.mark.parametrize("process_timeout", [False, True])
def test_run_repairs_unit_timeouts_leave_later_tasks_their_own_budget(tmp_path, monkeypatch, process_timeout):
    root, config, run, paths = queued_repairs(tmp_path, 3)
    fake_repair_tree(root, monkeypatch)
    clock, budgets = [1000.0], []
    monkeypatch.setattr(repair.time, "monotonic", lambda: clock[0])

    def timeout(config, *args, **kwargs):
        budgets.append(config["timeout_seconds"])
        clock[0] += 60
        if process_timeout:
            raise subprocess.TimeoutExpired("local verification", 60)
        raise TimeoutError("unit budget exhausted")

    result = repair.run_repairs(root, {**config, "repair_unit_timeout_seconds": 60}, run, 1300, timeout)
    assert budgets == [60, 60, 60]
    assert len(result["attempted"]) == len(result["processed"]) == 3
    assert all(item["status"] == "pending" for item in result["results"])
    assert all(json.loads(path.read_text())["status"] == "pending" for path in paths)
    assert result["inventory"]["blocked"] == result["manual_todo"] == []


def test_run_repairs_bounds_outer_revalidation_and_keeps_direct_single_argument_callback(tmp_path, monkeypatch):
    root, config, run, paths = queued_repairs(tmp_path, 2)
    fake_repair_tree(root, monkeypatch)
    clock, deadlines = [1000.0], []
    monkeypatch.setattr(repair.time, "monotonic", lambda: clock[0])
    config.update({"issue_target": "b" * 40, "issue_intake_ready": True, "issue_inputs": {}})
    state = {"issues": {}}
    for number, path in enumerate(paths, 1):
        task = json.loads(path.read_text())
        task["sources"] = [{"kind": "issue", "number": number, "verification_sha": A}]
        task["base_sha"] = config["issue_target"]
        repair.save(path, task)
        config["issue_inputs"][str(number)] = {"input_digest": "same", "assigned": False, "open": True}
        state["issues"][str(number)] = {"outcome": "confirmed", "triage": {"decision": "worthwhile", "reason": "project defect", "third_party_service": False}, "input_digest": "same", "repair_task": task["id"], "target": A}
    repair.save(root / "issue-state.json", state)
    before_first = paths[0].read_bytes()

    def revalidate(number, *, deadline):
        deadlines.append((number, deadline))
        if number == 1:
            clock[0] = deadline
            raise TimeoutError("issue unit exhausted")
        latest = json.loads((root / "issue-state.json").read_text())
        latest["issues"][str(number)]["target"] = config["issue_target"]
        repair.save(root / "issue-state.json", latest)

    def timeout(*args, **kwargs):
        raise TimeoutError("stop after local preparation")

    result = repair.run_repairs(root, config, run, 3000, timeout, revalidate_issue=revalidate)
    assert deadlines == [(1, 1600), (2, 2200)]
    assert paths[0].read_bytes() == before_first
    assert result["attempted"] == [paths[1].parent.name]
    assert result["results"][0]["status"] == "deferred"
    assert result["results"][1]["status"] == "pending"
    direct_calls = []

    def direct_revalidate(number):
        direct_calls.append(number)
        latest = json.loads((root / "issue-state.json").read_text())
        latest["issues"][str(number)]["target"] = config["issue_target"]
        repair.save(root / "issue-state.json", latest)

    direct = repair.repair_one(root, config, run, 3000, timeout, revalidate_issue=direct_revalidate,
                               _task_id=paths[0].parent.name)
    assert direct_calls == [1] and direct["status"] == "pending"


def test_run_repairs_aborts_queue_when_revalidation_reports_no_output(tmp_path, monkeypatch):
    root, config, run, paths = queued_repairs(tmp_path, 2)
    monkeypatch.setattr(repair.time, "monotonic", lambda: 1000.0)
    config.update({"issue_target": "b" * 40, "issue_intake_ready": True, "issue_inputs": {}})
    state = {"issues": {}}
    for number, path in enumerate(paths, 1):
        task = json.loads(path.read_text())
        task["sources"] = [{"kind": "issue", "number": number, "verification_sha": A}]
        task["base_sha"] = config["issue_target"]
        repair.save(path, task)
        config["issue_inputs"][str(number)] = {"input_digest": "same", "assigned": False, "open": True}
        state["issues"][str(number)] = {"outcome": "confirmed", "triage": {"decision": "worthwhile", "reason": "project defect", "third_party_service": False}, "input_digest": "same", "repair_task": task["id"], "target": A}
    repair.save(root / "issue-state.json", state)
    before = [path.read_bytes() for path in paths]

    def revalidate(number, *, deadline):
        raise repair.AgentNoOutput(f"issue #{number} revalidation produced no model output",
                                   diagnostics={"failure_kind": "agent_no_output", "model_output_events": 0})

    result = repair.run_repairs(root, config, run, 3000, unexpected_repair, revalidate_issue=revalidate)
    assert result["agent_unavailable"]["task"] == paths[0].parent.name
    assert result["agent_unavailable"]["diagnostics"]["failure_kind"] == "agent_no_output"
    assert result["processed"] == [paths[0].parent.name]
    assert [row["failure_kind"] for row in result["results"]] == ["agent_no_output"]
    assert result["attempted"] == []
    assert [path.read_bytes() for path in paths] == before
    deferred = {item["id"]: item["reason"] for item in result["deferred"]}
    assert "agent_no_output" in deferred[paths[1].parent.name]


def test_run_repairs_preserves_manual_tasks_and_earlier_scope_gates(tmp_path, monkeypatch):
    root, config, run, paths = queued_repairs(tmp_path, 5)
    for index, status in [(0, "awaiting_delivery_authorization"), (2, "blocked"), (3, "needs_triage")]:
        task = json.loads(paths[index].read_text())
        task.update({"status": status, "last_error": f"recorded reason {index}", "attempts": [f"retained-{index}"]})
        repair.save(paths[index], task)
    task = json.loads(paths[1].read_text())
    task["finding"]["fix_scope"] = json.loads(paths[0].read_text())["finding"]["fix_scope"]
    repair.save(paths[1], task)
    preserved = {path: path.read_bytes() for path in paths[:4]}
    fake_local_attempt(monkeypatch)
    result = repair.run_repairs(root, config, run, time.monotonic() + 3000, unexpected_repair)
    assert result["processed"] == result["attempted"] == [paths[4].parent.name]
    assert all(path.read_bytes() == before for path, before in preserved.items())
    manual = {item["id"]: item for item in result["manual_todo"]}
    assert "overlapping earlier" in manual[paths[1].parent.name]["reason"]
    assert manual[paths[2].parent.name]["reason"] == "recorded reason 2"
    assert manual[paths[3].parent.name]["reason"] == "recorded reason 3"
    for index in range(4):
        item = manual[paths[index].parent.name]
        assert item["needed_action"] and item["evidence"]["record"] == str(paths[index])
        assert item["evidence"]["initial"] == json.loads(paths[index].read_text())["evidence"]
    assert result["inventory"]["ready"] == [paths[0].parent.name]
    assert result["inventory"]["blocked"] == [paths[2].parent.name]
    assert result["inventory"]["needs_triage"] == [paths[3].parent.name]


def test_run_repairs_expired_deadline_still_returns_inventory_without_mutation(tmp_path):
    root, config, run, paths = queued_repairs(tmp_path, 1)
    before = paths[0].read_bytes()
    result = repair.run_repairs(root, config, run, time.monotonic() - 1, unexpected_repair)
    assert result["entered"] and result["processed"] == result["attempted"] == []
    assert result["inventory"]["pending"] == [paths[0].parent.name] and paths[0].read_bytes() == before


@pytest.mark.parametrize("verdict,findings,tamper,status", [
    ("approve", [], False, "awaiting_delivery_authorization"),
    ("changes_required", ["scope needs review"], False, "pending"),
    ("blocked", ["missing evidence"], False, "blocked"),
    ("approve", ["unresolved finding"], False, "blocked"),
    ("approve", [], True, "blocked"),
])
def test_local_approval_manifest_binds_tested_reviewed_content_only(tmp_path, monkeypatch, verdict, findings, tamper, status):
    root, config, run, paths = queued_repairs(tmp_path, 1)
    record = paths[0]
    manifest = record.parent / "approved-content.json"
    fake_repair_tree(root, monkeypatch)
    verifications = []

    def verify(config, tree, stage, selectors, timeout):
        assert not manifest.exists()
        verifications.append(stage.name)
        return {"cases": [("tests.example", "test_add")], "failures": int(stage.name == "baseline"),
                "exit_code": int(stage.name == "baseline"), "errors": 0, "skipped": 0}

    monkeypatch.setattr(repair, "verify_tests", verify)

    def invoke(config, stage, prompt, *, result_schema=None, writable=None):
        stage.mkdir(parents=True)
        task = json.loads(record.read_text())
        tree = Path(task["worktree"])
        source, test = (tree / name for name in task["finding"]["fix_scope"])
        assert not manifest.exists()
        if stage.name == "reproduce":
            test.parent.mkdir(exist_ok=True)
            test.write_text("def test_add():\n    assert add(2, 1) == 3\n")
            return {"outcome": "reproduced", "report_markdown": "recorded assertion failure", "test_paths": [str(test.relative_to(tree))],
                    "selectors": [str(test.relative_to(tree)) + "::test_add"]}
        if stage.name == "implement":
            source.write_text("def add(a, b):\n    return a + b\n")
            return {"outcome": "fixed", "report_markdown": "local implementation", "pr_title": "draft", "pr_body": "local draft"}
        assert stage.name == "independent-review" and verifications == ["baseline", "fixed"]
        if tamper:
            source.write_text("unreviewed content after verification\n")
        return {"verdict": verdict, "findings": findings, "report_markdown": "independent local review evidence", "third_party_service": False}

    result = repair.run_repairs(root, config, run, time.monotonic() + 3000, invoke)
    task = json.loads(record.read_text())
    assert result["results"][0]["status"] == task["status"] == status, task.get("last_error")
    assert len(result["attempted"]) == len(task["attempts"]) == 1
    if status == "awaiting_delivery_authorization":
        content = json.loads(manifest.read_text())
        assert content["files"] == repair._content_files(config, Path(task["worktree"]))
        assert set(content["files"]) == set(task["finding"]["fix_scope"])
        assert all(len(item["sha256"]) == 64 for item in content["files"].values())
        assert content["task_id"] == task["id"] and content["base_sha"] == task["base_sha"]
        assert content["branch"] == task["branch"] and Path(content["review_report"]).is_file()
        assert result["local_verified"] == result["inventory"]["ready"] == [task["id"]]
        assert result["manual_todo"][0]["evidence"]["approved_content_manifest"] == str(manifest)
    else:
        assert not manifest.exists() and "approved_content_manifest" not in task
        assert result["local_verified"] == []


def test_content_manifest_covers_deletions_symlink_targets_and_file_modes(tmp_path, monkeypatch):
    (tmp_path / "regular").write_text("approved bytes\n")
    (tmp_path / "regular").chmod(0o755)
    (tmp_path / "link").symlink_to("missing-target")
    monkeypatch.setattr(repair, "changed_paths", lambda *args, **kwargs: ["regular", "deleted", "link"])
    files = repair._content_files({}, tmp_path)
    assert files["regular"]["mode"] == 0o755 and len(files["regular"]["sha256"]) == 64
    assert files["deleted"] == {"kind": "deleted"}
    assert files["link"] == {"kind": "symlink", "target": "missing-target"}


def test_local_git_commands_share_the_remaining_unit_deadline(tmp_path, monkeypatch):
    clock, budgets = [10.0], []
    monkeypatch.setattr(repair.time, "monotonic", lambda: clock[0])

    def command(argv, **kwargs):
        budgets.append(kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(repair.subprocess, "run", command)
    config = {"git": "git", "_repair_deadline": 70}
    repair.git(config, tmp_path, "diff", "--name-only")
    clock[0] = 65
    repair.git(config, tmp_path, "diff", "--name-only")
    clock[0] = 70
    with pytest.raises(TimeoutError, match="budget"):
        repair.git(config, tmp_path, "diff", "--name-only")
    assert budgets == [60, 5]


@pytest.mark.parametrize("config", [{"max_repairs": -1}, {"repair_unit_timeout_seconds": 0}])
def test_run_repairs_rejects_invalid_limits_without_touching_queue(tmp_path, config):
    with pytest.raises(ValueError, match="max_repairs"):
        repair.run_repairs(tmp_path, config, tmp_path, time.monotonic() + 3000, unexpected_repair)
    assert list(tmp_path.iterdir()) == []


def test_run_repairs_empty_inventory_does_not_create_tasks(tmp_path):
    result = repair.run_repairs(tmp_path, {}, tmp_path, time.monotonic() + 3000, unexpected_repair)
    assert result["status"] == "no_pending_repairs" and result["entered"]
    assert result["results"] == result["attempted"] == result["local_verified"] == result["manual_todo"] == []
    assert result["inventory"] == {"pending": [], "blocked": [], "needs_triage": [], "ready": [], "merged": [], "source_closed": []}
    assert list(tmp_path.iterdir()) == []


def test_merged_task_releases_file_scope_for_later_pending_task(tmp_path, monkeypatch):
    root, config, run, paths = queued_repairs(tmp_path, 2)
    first, second = [json.loads(path.read_text()) for path in paths]
    first.update({"status": "merged", "delivery": {"complete": True}})
    second["finding"]["fix_scope"] = first["finding"]["fix_scope"]
    repair.save(paths[0], first)
    repair.save(paths[1], second)
    preserved = paths[0].read_bytes()
    fake_local_attempt(monkeypatch)
    result = repair.run_repairs(root, config, run, time.monotonic() + 3000, unexpected_repair)
    assert result["attempted"] == [second["id"]] and paths[0].read_bytes() == preserved
    assert result["inventory"]["merged"] == [first["id"]] and result["manual_todo"] == []


def test_new_confirmation_after_merge_preserves_delivery_and_requires_triage(tmp_path):
    root, config, run, paths = queued_repairs(tmp_path, 1)
    task = json.loads(paths[0].read_text())
    task.update({"status": "merged", "delivery": {"complete": True, "merge_proof": {"head": A}}})
    repair.save(paths[0], task)
    retained = task["delivery"]
    ids = repair.persist_findings(root, run, {"confirmed_findings": [task["finding"]]}, "b" * 40)
    saved = json.loads(paths[0].read_text())
    assert ids == [task["id"]] and saved["status"] == "needs_triage" and saved["delivery"] == retained
    result = repair.run_repairs(root, config, run, time.monotonic() + 3000, unexpected_repair)
    assert result["attempted"] == [] and result["inventory"]["needs_triage"] == [task["id"]]
    assert "new confirmation" in result["manual_todo"][0]["reason"]


@pytest.fixture
def repair_delivery(tmp_path, monkeypatch):
    """Real local Git/content adapter; every network and gate command is intercepted."""
    root, config, run_dir, _ = local_repository(tmp_path)
    record, = (root / "repairs").glob("*/task.json")
    task = json.loads(record.read_text())
    tree = record.parent / "worktree"
    branch = f"codex/health-fix-{task['id']}"
    subprocess.run(["git", "--git-dir", str(root / "repository.git"), "worktree", "add", "-q", "-b", branch,
                    str(tree), task["base_sha"]], check=True)
    git(tree, "config", "user.name", "Test")
    git(tree, "config", "user.email", "test@example.invalid")
    (tree / "src/example.py").write_text("def add(a, b):\n    return a + b\n")
    (tree / "tests").mkdir()
    (tree / "tests/test_example.py").write_text("from src.example import add\ndef test_add():\n    assert add(2, 1) == 3\n")
    baseline = {"cases": [["tests.test_example", "test_add"]], "failures": 1, "errors": 0, "skipped": 0, "exit_code": 1}
    fixed = {**baseline, "failures": 0, "exit_code": 0}
    files = repair._content_files(config, tree)
    manifest = record.parent / "approved-content.json"
    task.update({"status": "awaiting_delivery_authorization", "stage": "delivery", "worktree": str(tree), "branch": branch,
                 "sources": [{"kind": "issue", "number": 7}], "baseline": baseline, "fixed_verification": fixed,
                 "reproduction": {"selectors": ["tests/test_example.py"], "test_paths": ["tests/test_example.py"]},
                 "verified_content": files, "approved_content_manifest": str(manifest),
                 "independent_review": {"verdict": "approve", "findings": [], "third_party_service": False},
                 "implementation": {"pr_body": "Fixes #999", "pr_title": "Fixes #999"}})
    repair.save(manifest, {"task_id": task["id"], "base_sha": task["base_sha"], "branch": branch, "worktree": str(tree),
                           "files": files, "verification": fixed})
    repair.save(record, task)
    config.update({"gh": "github-client", "repository": "fixture/repo", "repository_url": git(tree, "remote", "get-url", "origin")})
    state = {"pr": False, "merged": False, "issue": "open", "assigned": False, "reachability": True,
             "merge_readback": True, "close_timeout": False, "read_timeout": False, "wrong_head": False, "error_envelope": False,
             "checks": [{"name": name, "status": "COMPLETED", "conclusion": "SUCCESS"} for name in sorted(repair.REQUIRED_REPAIR_CHECKS)]}
    calls, forbidden = [], []
    original = repair.run

    def external(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "github-client":
            args = argv[1:]
            if args[0] == "api" and args[1] == "repos/fixture/repo/issues/7":
                if "--method" in args:
                    assert args[args.index("--method") + 1] == "PATCH"
                    state["issue"] = "closed"
                    if state["close_timeout"]:
                        state["close_timeout"] = False
                        raise TimeoutError("close response lost after the effect")
                return json.dumps({"number": 7, "state": state["issue"], "assignees": [{"login": "other"}] if state["assigned"] else []})
            if args[:2] == ["pr", "list"]:
                return json.dumps([{"number": 80}] if state["pr"] else [])
            if args[:2] == ["pr", "create"]:
                assert "--body-file" in args and "--base" in args and args[args.index("--base") + 1] == "main"
                state["body"] = Path(args[args.index("--body-file") + 1]).read_text()
                state["pr"] = True
                return "https://github.com/fixture/repo/pull/80"
            if args[:2] == ["pr", "view"]:
                if state["read_timeout"]:
                    state["read_timeout"] = False
                    raise TimeoutError("PR readback timed out after creation")
                if state["error_envelope"]:
                    return json.dumps({"ok": False, "error": "unavailable", "data": {}})
                head = git(tree, "rev-parse", "HEAD")
                return json.dumps({"number": 80, "url": "https://github.com/fixture/repo/pull/80",
                                   "state": "MERGED" if state["merged"] else "OPEN", "headRefOid": A if state["wrong_head"] else head,
                                   "headRefName": branch, "baseRefName": "main", "headRepository": {"nameWithOwner": "fixture/repo"},
                                   "mergedAt": "2026-11-07T08:00:00Z" if state["merged"] else None,
                                   "mergeCommit": {"oid": head} if state["merged"] else None,
                                   "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "statusCheckRollup": state["checks"]})
            if args[:2] == ["pr", "merge"]:
                assert "--match-head-commit" in args and args[-1] == git(tree, "rev-parse", "HEAD")
                assert "--admin" not in args and "--auto" not in args
                state["merged"] = state["merge_readback"]
                return ""
        elif argv[0] == config["verification_python"]:
            return "fixture gate passed"
        elif argv[0] == "git":
            if argv[1:3] == ["push", "origin"]:
                assert argv == ["git", "push", "origin", f"HEAD:refs/heads/{branch}"]
                return ""
            if argv[1:3] == ["fetch", "origin"]:
                target = "HEAD" if state["reachability"] else task["base_sha"]
                original(["git", "-C", str(tree), "update-ref", "refs/remotes/origin/main", git(tree, "rev-parse", target)])
                return ""
            if "push" not in argv and "fetch" not in argv:
                return original(argv, **kwargs)
        forbidden.append(argv)
        raise AssertionError(f"forbidden external effect: {argv}")

    monkeypatch.setattr(repair, "run", external)
    def deliver(*, authorized=True, approved_head=None):
        result = repair.deliver_repairs(root, config, run_dir, time.monotonic() + 300, authorized=authorized,
                                        merge_approvals={task["id"]: approved_head} if approved_head else {})
        assert forbidden == []
        return result
    return root, config, run_dir, record, tree, task, state, calls, deliver


def test_all_closed_sources_stop_publication_without_touching_the_worktree(repair_delivery):
    root, config, _, record, tree, task, _, calls, deliver = repair_delivery
    head = git(tree, "rev-parse", "HEAD")
    config.update({"issue_intake_ready": True, "issue_inputs": {"7": {"state": "closed"}}})
    result = deliver()
    assert result["processed"] == result["prs"] == result["merged"] == result["closed"] == []
    assert calls == [] and git(tree, "rev-parse", "HEAD") == head
    saved = json.loads(record.read_text())
    assert saved["status"] == "source_closed" and saved["finding"] == task["finding"]
    assert repair.saved_delivery_results(root, merge_approvals={}) == []


def test_remote_repair_delivery_defaults_to_no_commands_or_state_writes(repair_delivery):
    _, _, _, record, tree, _, _, calls, deliver = repair_delivery
    before, head = record.read_bytes(), git(tree, "rev-parse", "HEAD")
    result = deliver(authorized=False)
    assert calls == [] and record.read_bytes() == before and git(tree, "rev-parse", "HEAD") == head
    assert result["prs"] == result["merged"] == result["closed"] == []


@pytest.mark.parametrize(("delivery_config", "publishes"), [
    ({}, True), ({"repair_delivery_enabled": True}, True), ({"repair_delivery_enabled": False}, False),
    ({"repair_delivery_enabled": False, "_deliver_repairs": True}, True),
])
def test_daily_delivery_config_can_publish_but_cannot_authorize_a_merge(repair_delivery, monkeypatch, delivery_config, publishes):
    root, config, run_dir, _, _, task, _, calls, _ = repair_delivery
    config.update(delivery_config)
    monkeypatch.setattr(repair, "run_repairs", lambda *args, **kwargs:
                        {"inventory": {"ready": [task["id"]]}, "attempted": [], "local_verified": [], "manual_todo": []})
    result = review.run_repair_phase(root, config, run_dir, root, task["base_sha"], {}, "complete", time.monotonic() + 300)
    if publishes:
        assert result["prs_created"][0]["number"] == 80
        assert result["delivery"][0]["status"] == "awaiting_merge_authorization"
        assert result["inventory"]["awaiting_merge_authorization"] == [task["id"]]
        assert result["inventory"]["ready"] == []
    else:
        assert calls == [] and result["prs_created"] == []
        assert result["inventory"]["ready"] == [task["id"]]
    assert result["merged"] == result["closed"] == []
    assert not any(argv[:3] == ["github-client", "pr", "merge"] or "PATCH" in argv for argv in calls)


def test_repair_pr_publish_requires_human_head_approval_and_reuses_existing_pr(repair_delivery):
    _, _, _, record, tree, task, state, calls, deliver = repair_delivery
    first = deliver()
    assert first["prs"][0]["created"] and first["merged"] == first["closed"] == []
    assert first["results"][0]["status"] == "awaiting_merge_authorization"
    head = git(tree, "rev-parse", "HEAD")
    assert head != task["base_sha"] and "Fixes #999" not in state["body"] and "Related issues: #7" in state["body"]
    assert "## 复现方式" in state["body"] and "add(2, 1)" in state["body"]
    assert "预期：3" in state["body"] and "实际：1" in state["body"]
    assert "## 改动范围" in state["body"] and "`src/example.py`" in state["body"]
    assert "## 验证命令与结果" in state["body"] and "-m pytest -q -p no:cacheprovider tests/test_example.py" in state["body"]
    assert "exit=1" in state["body"] and "exit=0" in state["body"] and "独立审核：approve" in state["body"]
    assert "本 PR 尚未合并；须经独立逐行审查、实际验证和宿主交付门禁" in state["body"]
    assert json.loads(record.read_text())["delivery"]["head"] == head
    again = deliver(approved_head=A)
    assert not again["prs"][0]["created"] and again["merged"] == again["closed"] == []
    assert sum(argv[:3] == ["github-client", "pr", "create"] for argv in calls) == 1
    assert sum(argv[:2] == ["git", "push"] for argv in calls) == 1
    assert not any(argv[:3] == ["github-client", "pr", "merge"] or "PATCH" in argv for argv in calls)
    gates = [argv for argv in calls if argv[0].endswith("python") or argv[0] == sys.executable]
    assert any("--staged" in argv and "--check-public-surface" in argv for argv in gates)
    assert any("scripts/generate_dependency_graph.py" in argv for argv in gates)


def test_preexisting_remote_pr_is_reused_without_another_push_or_create(repair_delivery):
    _, _, _, _, tree, _, state, calls, deliver = repair_delivery
    state["pr"] = True
    first = deliver()
    head = git(tree, "rev-parse", "HEAD")
    again = deliver()
    assert first["prs"][0]["number"] == again["prs"][0]["number"] == 80
    assert not first["prs"][0]["created"] and not again["prs"][0]["created"]
    assert git(tree, "rev-parse", "HEAD") == head
    assert not any(argv[:3] in (["github-client", "pr", "create"], ["github-client", "pr", "merge"])
                   or argv[:2] == ["git", "push"] for argv in calls)


@pytest.mark.parametrize("failure", ["missing", "skipped", "pending", "failed"])
def test_repair_delivery_requires_all_eight_regression_shards_and_checks(repair_delivery, failure):
    _, _, _, _, tree, _, state, calls, deliver = repair_delivery
    deliver()
    if failure == "missing":
        state["checks"].pop()
    else:
        state["checks"][0].update({"status": "IN_PROGRESS" if failure == "pending" else "COMPLETED",
                                   "conclusion": {"skipped": "SKIPPED", "pending": None, "failed": "FAILURE"}[failure]})
    result = deliver(approved_head=git(tree, "rev-parse", "HEAD"))
    assert result["results"][0]["status"] == ("ci_failed" if failure == "failed" else "ci_pending")
    assert result["merged"] == result["closed"] == []
    assert not any(argv[:3] == ["github-client", "pr", "merge"] or "PATCH" in argv for argv in calls)


def test_failed_ci_is_reported_and_preserved_without_reruns_or_duplicate_prs(repair_delivery, monkeypatch):
    root, config, run_dir, record, tree, task, state, calls, deliver = repair_delivery
    state["checks"][1]["conclusion"] = "FAILURE"
    monkeypatch.setattr(repair, "run_repairs", lambda *args, **kwargs:
                        {"inventory": {"ready": [task["id"]]}, "manual_todo": []})
    result = review.run_repair_phase(root, config, run_dir, root, task["base_sha"], {}, "complete", time.monotonic() + 300)
    review.save_daily_summary(run_dir, {"repair": result})
    assert result["delivery"][0]["status"] == "ci_failed"
    assert result["inventory"]["blocked"] == result["inventory"]["ci_failed"] == [task["id"]]
    assert result["inventory"]["awaiting_merge_authorization"] == [task["id"]]
    persisted = json.loads(record.read_text())["delivery"]
    failure = persisted["ci_failure"]
    assert persisted["last_result"]["status"] == "ci_failed"
    assert failure["head"] == git(tree, "rev-parse", "HEAD") and failure["checks"] == state["checks"]
    summary = json.loads((run_dir / "daily-summary.json").read_text())
    assert summary["ci_failed"] == summary["blocked"] == [task["id"]]
    assert "regression (0): FAILURE" in summary["blocked_reasons"][0]["reason"]
    report = (run_dir / "report.md").read_text()
    assert "新建 PR 1" in report and "PR [#80]" in report and "CI 失败 1" in report
    assert "blocked 1" in report and "regression (0): FAILURE" in report and "核实合并修复 0" in report
    assert deliver(approved_head=failure["head"])["results"][0]["status"] == "ci_failed"
    state["checks"][1]["conclusion"] = "SUCCESS"
    assert deliver()["results"][0]["status"] == "awaiting_merge_authorization"
    assert json.loads(record.read_text())["delivery"]["ci_failure"] == failure
    assert sum(argv[:3] == ["github-client", "pr", "create"] for argv in calls) == 1
    assert sum(argv[:2] == ["git", "push"] for argv in calls) == 1
    assert not any(argv[:3] in (["github-client", "pr", "merge"], ["github-client", "run", "rerun"])
                   or "PATCH" in argv for argv in calls)


@pytest.mark.parametrize("ci_state", ["SUCCESS", "FAILURE", "PENDING"])
@pytest.mark.parametrize("skip", ["budget", "disabled"])
def test_saved_pr_and_ci_are_reported_when_delivery_is_skipped(repair_delivery, monkeypatch, ci_state, skip):
    root, config, _, record, tree, task, state, calls, deliver = repair_delivery
    if ci_state == "PENDING":
        state["checks"][1].update({"status": "IN_PROGRESS", "conclusion": None})
    else:
        state["checks"][1]["conclusion"] = ci_state
    first = deliver()
    expected = {"SUCCESS": "awaiting_merge_authorization", "FAILURE": "ci_failed", "PENDING": "ci_pending"}[ci_state]
    assert first["results"][0]["status"] == expected
    before, head = record.read_bytes(), git(tree, "rev-parse", "HEAD")
    calls.clear()
    config["repair_delivery_enabled"] = skip != "disabled"
    monkeypatch.setattr(repair, "run_repairs", lambda *args, **kwargs:
                        {"inventory": {"ready": [task["id"]]}, "manual_todo": []})
    later_run = root / "later-run"
    later_run.mkdir()
    result = review.run_repair_phase(root, config, later_run, root, task["base_sha"], {}, "complete",
                                     time.monotonic() + (4 if skip == "budget" else 300))
    assert calls == [] and record.read_bytes() == before and git(tree, "rev-parse", "HEAD") == head
    assert result["prs_created"] == result["merged"] == result["closed"] == []
    assert result["prs"][0]["number"] == 80 and not result["prs"][0]["refreshed_this_run"]
    assert result["inventory"]["ready"] == []
    assert result["inventory"]["awaiting_merge_authorization"] == [task["id"]]
    assert result["inventory"]["blocked"] == ([task["id"]] if ci_state == "FAILURE" else [])
    assert result["inventory"]["ci_failed"] == ([task["id"]] if ci_state == "FAILURE" else [])
    assert result["inventory"]["ci_pending"] == ([task["id"]] if ci_state == "PENDING" else [])
    review.save_daily_summary(later_run, {"repair": result})
    summary = json.loads((later_run / "daily-summary.json").read_text())
    assert summary["delivery_not_refreshed"][0]["status"] == expected
    report = (later_run / "report.md").read_text()
    assert "新建 PR 0" in report and "已有（本轮未刷新） PR [#80]" in report
    assert "等待合并批准 1" in report and "本地待交付 0" in report
    if ci_state == "FAILURE":
        assert "CI 失败 1" in report and "blocked 1" in report and "regression (0): FAILURE" in report


def test_saved_observation_cannot_carry_merge_approval_into_another_invocation(repair_delivery):
    root, _, _, record, tree, task, state, _, deliver = repair_delivery
    state["checks"][1]["conclusion"] = "FAILURE"
    deliver()
    head = git(tree, "rev-parse", "HEAD")
    assert not deliver(approved_head=head)["results"][0]["merge_approval_required"]
    assert not json.loads(record.read_text())["delivery"]["last_result"]["merge_approval_required"]
    saved, = repair.saved_delivery_results(root, merge_approvals={})
    assert saved["id"] == task["id"] and saved["merge_approval_required"]


def test_existing_pr_readback_timeout_retains_its_identity_without_republishing(repair_delivery):
    _, _, _, _, _, _, state, calls, deliver = repair_delivery
    deliver()
    state["read_timeout"] = True
    result = deliver()
    assert result["results"][0]["status"] == "pending"
    assert result["prs"][0]["number"] == 80 and not result["prs"][0]["readback_confirmed"]
    assert result["results"][0]["merge_approval_required"]
    assert sum(argv[:3] == ["github-client", "pr", "create"] for argv in calls) == 1
    assert sum(argv[:2] == ["git", "push"] for argv in calls) == 1


def test_pr_body_uses_recorded_commands_and_disables_issue_closing_directives(repair_delivery):
    _, config, _, _, _, task, _, _, _ = repair_delivery
    task["baseline"]["command"] = ["python-before", "-m", "pytest", "tests/test_example.py"]
    task["fixed_verification"]["command"] = ["python-after", "-m", "pytest", "tests/test_example.py"]
    task["finding"]["failure_scenario"] += "\nFixes #7\nResolves fixture/repo#8"
    body = repair._repair_pr_body(task, config, task["verified_content"], [7])
    assert "python-before -m pytest tests/test_example.py" in body
    assert "python-after -m pytest tests/test_example.py" in body
    assert "原始记录未保存命令" not in body
    assert "Fixes #7" not in body and "Resolves fixture/repo#8" not in body and "Related issues: #7" in body


def test_verified_repair_merge_closes_only_its_source_issue_once(repair_delivery):
    _, _, _, record, tree, task, state, calls, deliver = repair_delivery
    deliver()
    result = deliver(approved_head=git(tree, "rev-parse", "HEAD"))
    assert result["merged"] == [task["id"]] and result["closed"] == [7] and result["manual_todo"] == []
    saved = json.loads(record.read_text())
    assert saved["status"] == "merged" and saved["delivery"]["complete"] and saved["delivery"]["merge_proof"]
    assert saved["delivery"]["closed_issues"]["7"]["state"] == "closed" and state["issue"] == "closed"
    patch = next(index for index, argv in enumerate(calls) if "PATCH" in argv)
    assert any(argv[:3] == ["github-client", "pr", "merge"] for argv in calls[:patch])
    assert sum("--is-ancestor" in argv for argv in calls[:patch]) == 2
    before = len(calls)
    assert deliver()["processed"] == [] and len(calls) == before


@pytest.mark.parametrize("failure", ["merge_readback", "reachability", "wrong_head", "error_envelope"])
def test_delivery_cannot_close_on_unproven_merge_or_error_envelope(repair_delivery, failure):
    _, _, _, record, tree, _, state, calls, deliver = repair_delivery
    deliver()
    state[failure] = failure in {"wrong_head", "error_envelope"}
    result = deliver(approved_head=git(tree, "rev-parse", "HEAD"))
    assert result["results"][0]["status"] == "blocked" and result["merged"] == result["closed"] == []
    assert state["issue"] == "open" and not any("PATCH" in argv for argv in calls)
    assert json.loads(record.read_text())["status"] == "awaiting_delivery_authorization"


@pytest.mark.parametrize("failure", ["content", "manifest", "ownership"])
def test_delivery_keeps_unapproved_content_and_changed_ownership_local(repair_delivery, failure):
    _, _, _, record, tree, task, state, calls, deliver = repair_delivery
    if failure == "content":
        (tree / "src/example.py").write_text("unreviewed changes\n")
    elif failure == "manifest":
        Path(task["approved_content_manifest"]).unlink()
    else:
        state["assigned"] = True
    result = deliver()
    assert result["results"][0]["status"] == "blocked"
    assert git(tree, "rev-parse", "HEAD") == task["base_sha"] and result["prs"] == []
    assert not any(argv[:2] == ["git", "push"] or argv[:3] in (["github-client", "pr", "create"], ["github-client", "pr", "merge"]) or "PATCH" in argv for argv in calls)
    assert json.loads(record.read_text())["status"] == "awaiting_delivery_authorization"


def test_ambiguous_close_response_retries_readback_without_duplicate_patch(repair_delivery):
    _, _, _, record, tree, task, state, calls, deliver = repair_delivery
    deliver()
    state["close_timeout"] = True
    first = deliver(approved_head=git(tree, "rev-parse", "HEAD"))
    assert first["merged"] == [task["id"]] and first["results"][0]["status"] == "pending"
    assert state["issue"] == "closed" and not json.loads(record.read_text())["delivery"].get("complete")
    resumed = deliver()
    assert resumed["results"][0]["status"] == "merged" and resumed["merged"] == [] and resumed["closed"] == [7]
    assert json.loads(record.read_text())["delivery"]["complete"]
    assert sum("PATCH" in argv for argv in calls) == 1


@pytest.mark.parametrize("failure", ["failed", "missing"])
def test_already_merged_repair_still_requires_complete_successful_ci_before_closing(repair_delivery, failure):
    _, _, _, record, _, _, state, calls, deliver = repair_delivery
    deliver()
    state["merged"] = True
    if failure == "failed":
        state["checks"][0]["conclusion"] = "FAILURE"
    else:
        state["checks"].pop()
    result = deliver()
    assert result["results"][0]["status"] == ("ci_failed" if failure == "failed" else "ci_pending")
    assert result["merged"] == result["closed"] == [] and state["issue"] == "open"
    assert not any("PATCH" in argv for argv in calls)
    assert not json.loads(record.read_text())["delivery"].get("merge_proof")


def test_pr_creation_receipt_is_counted_even_when_its_readback_times_out(repair_delivery):
    _, _, _, record, _, _, state, calls, deliver = repair_delivery
    state["read_timeout"] = True
    first = deliver()
    assert first["results"][0]["status"] == "pending"
    assert len(first["prs"]) == 1 and first["prs"][0]["created"]
    saved = json.loads(record.read_text())["delivery"]
    assert saved["pr_created_receipt"]["url"] == "https://github.com/fixture/repo/pull/80"
    resumed = deliver()
    assert not resumed["prs"][0]["created"] and resumed["merged"] == resumed["closed"] == []
    assert sum(argv[:3] == ["github-client", "pr", "create"] for argv in calls) == 1
