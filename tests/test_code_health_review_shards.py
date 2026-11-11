from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from scripts import code_health_review as review


def result(completed, outcome="complete", findings=None):
    return {"outcome": outcome, "completed_commits": completed,
            "report_markdown": "可追溯的四项检查、审查证据及明确的未完成项。" * 20,
            "verification_gaps": [], "confirmed_findings": findings or []}


@pytest.fixture
def commit_review_run(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()

    git("init", "-q", "-b", "main")
    shas = []
    for index in range(5):
        (source / "AGENTS.md").write_text("Read-only review fixture")
        (source / "example.py").write_text(f"value = {index}\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", f"unit {index}")
        shas.append(git("rev-parse", "HEAD"))
    root = tmp_path / "state"
    root.mkdir()
    (root / "previous.md").write_text("上一轮报告：已确认 old-owner:failure，尚未修复。")
    review.write_json(root / "state.json", {"cursor": shas[0], "last_review": str(root / "previous.md"),
                                            "confirmed_finding_keys": ["old-owner:failure"]})
    review.write_json(root / "config.json", {
        "git": "git", "gh": "github-client", "repository": "fixture/repo", "repository_url": str(source), "pull_requests_enabled": False, "comments_enabled": False, "release_enabled": False,
        "timeout_seconds": 1800, "max_units": 10, "issue_timeout_seconds": 600,
    })
    original = review.command
    monkeypatch.setattr(review, "command", lambda argv, **kwargs: "[]" if argv[0] == "github-client" else original(argv, **kwargs))
    clock = [0]
    monkeypatch.setattr(review.time, "monotonic", lambda: clock[0])
    return root, shas, clock


@pytest.mark.parametrize("failure", ["timeout", "subprocess_timeout", "partial", "blocked"])
def test_shards_checkpoint_prefix_before_failure_and_resume_next_run(commit_review_run, monkeypatch, failure):
    root, shas, clock = commit_review_run
    calls = []

    def invoke(config, shard, prompt):
        plan = json.loads((shard / "plan.json").read_text())
        sha = plan["commits"][0]["sha"]
        assert len(plan["commits"]) == 1
        calls.append(sha)
        persisted = json.loads((root / "state.json").read_text())
        index = shas.index(sha)
        # The cursor stops before the first unresolved unit; units after it are still attempted.
        assert persisted["cursor"] == (shas[index - 1] if index <= 3 else shas[2])
        assert "上一轮报告" in plan["previous_review"]
        assert "old-owner:failure" in plan["confirmed_finding_keys"]
        if sha == shas[3]:
            run = shard.parents[2]
            assert json.loads((run / "result.json").read_text())["completed_commits"] == shas[1:3]
            assert all((run / "work" / "shards" / f"{index:03d}-{item}" / "result.json").is_file()
                       for index, item in enumerate(shas[1:3], 1))
            if failure == "timeout":
                clock[0] += config["timeout_seconds"]
                raise TimeoutError("third unit timed out")
            if failure == "subprocess_timeout":
                raise subprocess.TimeoutExpired("model", config["timeout_seconds"])
            return result([], failure)
        clock[0] += 100
        return result([sha])

    monkeypatch.setattr(review, "invoke_agent", invoke)
    receipt = review.run_once(root)
    assert receipt["status"] == "partial"
    assert receipt["completed_commits"] == [shas[1], shas[2], shas[4]]
    assert receipt["cursor_after"] == shas[2]
    assert calls == shas[1:]
    run = Path(receipt["run_dir"])
    aggregate = json.loads((run / "result.json").read_text())
    assert review.validate_result(aggregate, shas[1:]) == [shas[1], shas[2], shas[4]]
    assert all(sha in aggregate["report_markdown"] for sha in shas[1:])
    assert json.loads((run / "work" / "shards" / f"003-{shas[3]}" / "receipt.json").read_text())["status"] == (
        "pending" if "timeout" in failure else failure
    )
    resumed = []

    def resume(config, shard, prompt):
        plan = json.loads((shard / "plan.json").read_text())
        sha = plan["commits"][0]["sha"]
        resumed.append(sha)
        assert shas[2] in plan["previous_review"] and shas[3] in plan["previous_review"]
        return result([sha])

    monkeypatch.setattr(review, "invoke_agent", resume)
    next_receipt = review.run_once(root)
    assert next_receipt["status"] == "complete"
    assert resumed == [shas[3]]
    assert next_receipt["cursor_after"] == shas[-1]


def test_finding_keys_deduplicate_across_shards_and_runs_with_durable_evidence(commit_review_run, monkeypatch):
    root, shas, _ = commit_review_run
    calls = []

    def invoke(config, shard, prompt):
        plan = json.loads((shard / "plan.json").read_text())
        sha = plan["commits"][0]["sha"]
        calls.append(sha)
        assert "old-owner:failure" in plan["confirmed_finding_keys"]
        if len(calls) > 1:
            assert "example:wrong-value" in plan["confirmed_finding_keys"]
            assert "example:wrong-value" in prompt
        work = shard / "work"
        work.mkdir()
        (work / "probe.log").write_text("expected=1 actual=2\n")
        finding = {"key": "example:wrong-value", "title": "wrong value", "commit": sha,
                   "owner_path": "example.py", "fix_scope": ["example.py", "tests/test_example.py"],
                   "failure_scenario": "fixture input", "expected": "1", "actual": "2", "evidence_log": "probe.log"}
        return result([sha], findings=[finding])

    monkeypatch.setattr(review, "invoke_agent", invoke)
    receipt = review.run_once(root)
    aggregate = json.loads((Path(receipt["run_dir"]) / "result.json").read_text())
    assert len(aggregate["confirmed_findings"]) == 1
    finding = aggregate["confirmed_findings"][0]
    assert (Path(receipt["run_dir"]) / "work" / finding["evidence_log"]).read_text() == "expected=1 actual=2\n"
    records = list((root / "repairs").glob("*/task.json"))
    assert len(records) == 1
    task = json.loads(records[0].read_text())
    assert [item["commit"] for item in task["sources"]] == shas[1:]
    assert len(receipt["repair_tasks"]) == 1
    # Revisit a unit with a saved key and evidence from an earlier run.
    state = json.loads((root / "state.json").read_text())
    review.write_json(root / "state.json", {**state, "cursor": shas[-2]})
    again = review.run_once(root)
    next_result = json.loads((Path(again["run_dir"]) / "result.json").read_text())
    assert next_result["confirmed_findings"] == []
    assert len(list((root / "repairs").glob("*/task.json"))) == 1


@pytest.mark.parametrize(("elapsed", "expected"), [(600, [600, 600]), (1171, [600])])
def test_shards_stop_at_review_budget_without_skipping_unit(commit_review_run, monkeypatch, elapsed, expected):
    root, shas, clock = commit_review_run
    config = json.loads((root / "config.json").read_text())
    review.write_json(root / "config.json", {**config, "issues_enabled": True})
    calls = []

    def invoke(config, shard, prompt):
        calls.append(config["timeout_seconds"])
        clock[0] += elapsed
        sha = json.loads((shard / "plan.json").read_text())["commits"][0]["sha"]
        return result([sha])

    monkeypatch.setattr(review, "invoke_agent", invoke)
    receipt = review.run_once(root)
    assert receipt["status"] == "partial"
    assert calls == expected
    assert receipt["completed_commits"] == shas[1:1 + len(expected)]
    assert receipt["issues"]["status"] == "no_eligible_issues"


@pytest.mark.parametrize(("review_elapsed", "intake_elapsed"), [(0, 0), (600, 60), (1200, 60)])
def test_unused_review_budget_flows_to_issues_without_breaching_the_daily_cap(
        commit_review_run, monkeypatch, review_elapsed, intake_elapsed):
    root, shas, clock = commit_review_run
    config = json.loads((root / "config.json").read_text())
    review.write_json(root / "config.json", {**config, "issues_enabled": True, "max_units": 1})
    observed = []

    def intake(root, config, target, command, *, deadline, hydrate):
        assert hydrate is False
        assert clock[0] == review_elapsed
        # Review's unused allocation (1200s here) carries into intake, capped by the daily deadline.
        assert deadline == 1800
        clock[0] += intake_elapsed
        return {"issues": [{"number": 1}], "target": target}

    def process(root, config, run, source, plan, deadline, *args, command):
        assert callable(command)
        observed.append(deadline - clock[0])
        return {"status": "complete", "reviewed": [1]}

    def invoke(config, shard, prompt):
        assert config["timeout_seconds"] == 600
        clock[0] += review_elapsed
        if review_elapsed:
            raise TimeoutError("unit exhausted its budget")
        return result([shas[1]])

    monkeypatch.setattr(review, "invoke_agent", invoke)
    monkeypatch.setattr(review.issues, "plan_issues", intake)
    monkeypatch.setattr(review.issues, "process_issues", process)
    receipt = review.run_once(root)
    assert receipt["status"] == ("partial" if review_elapsed else "complete")
    assert observed == [1800 - review_elapsed - intake_elapsed]
    assert receipt["issues"]["status"] == "complete"


def test_agent_no_output_skips_later_model_stages(commit_review_run, monkeypatch):
    root, shas, clock = commit_review_run
    config = json.loads((root / "config.json").read_text())
    review.write_json(root / "config.json", {
        **config, "issues_enabled": True, "repair_enabled": True, "pull_requests_enabled": True,
        "comments_enabled": True, "review_budget_seconds": 600, "issue_timeout_seconds": 300,
        "repair_budget_seconds": 300, "pull_request_budget_seconds": 300, "delivery_budget_seconds": 300,
    })
    calls = []
    for name, target in (("plan_issues", review.issues), ("process_issues", review.issues)):
        monkeypatch.setattr(target, name, lambda *args, _name=name, **kwargs: calls.append(_name) or {})
    monkeypatch.setattr(review.pull_requests, "process_pull_requests",
                        lambda *args, **kwargs: calls.append("pull_requests") or {})
    monkeypatch.setattr(review, "run_repair_phase", lambda *args, **kwargs: calls.append("repair") or {})
    monkeypatch.setattr(review.delivery, "process_delivery",
                        lambda *args, **kwargs: calls.append("delivery") or {})

    def invoke(config, shard, prompt):
        raise review.repair.AgentTimeout(
            "no output", diagnostics={"failure_kind": "agent_no_output", "model_output_events": 0})

    monkeypatch.setattr(review, "invoke_agent", invoke)
    receipt = review.run_once(root)
    assert calls == []
    assert receipt["agent_unavailable"]["commit"] == shas[1]
    for phase in ("issues", "pull_requests", "repair", "delivery"):
        assert receipt[phase]["status"] == "skipped"
    assert receipt["phase_timing"]["review"]["elapsed_seconds"] == 0
    assert "模型无输出" in (Path(receipt["run_dir"]) / "report.md").read_text()


def test_review_carryover_cannot_consume_reserved_repair_budget(commit_review_run, monkeypatch):
    root, shas, clock = commit_review_run
    config = json.loads((root / "config.json").read_text())
    review.write_json(root / "config.json", {**config, "issues_enabled": True, "max_units": 1,
                                             "repair_enabled": True, "repair_budget_seconds": 600})
    observed = []

    def intake(root, config, target, command, *, deadline, hydrate):
        observed.append(deadline)
        return {"issues": [], "target": target}

    def process(root, config, run, source, plan, deadline, *args, command):
        return {"status": "no_eligible_issues", "reviewed": []}

    monkeypatch.setattr(review, "invoke_agent", lambda config, shard, prompt: result([shas[1]]))
    monkeypatch.setattr(review.issues, "plan_issues", intake)
    monkeypatch.setattr(review.issues, "process_issues", process)
    monkeypatch.setattr(review, "run_repair_phase", lambda *args, **kwargs: {"status": "no_pending_repairs"})
    review.run_once(root)
    # 1800 total - 600 reserved repair = 1200 is the hard cap for review + intake.
    assert observed == [1200]


@pytest.mark.parametrize("failure", ["noncontiguous", "short_report", "missing_evidence", "interrupted"])
def test_invalid_or_interrupted_shard_cannot_erase_saved_prefix(commit_review_run, monkeypatch, failure):
    root, shas, _ = commit_review_run
    calls = []

    def invoke(config, shard, prompt):
        sha = json.loads((shard / "plan.json").read_text())["commits"][0]["sha"]
        calls.append(sha)
        if sha != shas[3]:
            return result([sha])
        if failure == "interrupted":
            raise KeyboardInterrupt("process interrupted during third unit")
        if failure == "noncontiguous":
            return result([shas[4]])
        if failure == "short_report":
            return {**result([sha]), "report_markdown": "done"}
        return result([sha], findings=[{
            "key": "example:missing-log", "title": "unproven finding", "commit": sha,
            "owner_path": "example.py", "fix_scope": ["example.py"], "failure_scenario": "input",
            "expected": "1", "actual": "2", "evidence_log": "missing.log",
        }])

    monkeypatch.setattr(review, "invoke_agent", invoke)
    with pytest.raises(KeyboardInterrupt if failure == "interrupted" else ValueError):
        review.run_once(root)
    state = json.loads((root / "state.json").read_text())
    assert state["cursor"] == shas[2]
    aggregate = json.loads(Path(state["last_review"]).with_name("result.json").read_text())
    assert aggregate["completed_commits"] == shas[1:3]
    assert review.validate_result(aggregate, shas[1:]) == shas[1:3]
    assert calls == shas[1:4]
    assert not list((root / "repairs").glob("*/task.json"))


@pytest.mark.parametrize("failure", ["partial", "timeout"])
def test_first_unfinished_shard_hands_latest_report_to_next_run(commit_review_run, monkeypatch, failure):
    root, shas, _ = commit_review_run
    marker = "首片最新进度：入口已核对，失败路径尚待验证。"

    def unfinished(config, shard, prompt):
        if failure == "timeout":
            raise TimeoutError(marker)
        return {**result([], "partial"), "report_markdown": marker * 20}

    monkeypatch.setattr(review, "invoke_agent", unfinished)
    first = review.run_once(root)
    state = json.loads((root / "state.json").read_text())
    assert state["cursor"] == shas[0]
    assert state["last_review"] == str(Path(first["run_dir"]) / "report.md")
    seen = []

    def resume(config, shard, prompt):
        plan = json.loads((shard / "plan.json").read_text())
        assert marker in plan["previous_review"]
        seen.append(plan["commits"][0]["sha"])
        return result([seen[-1]])

    monkeypatch.setattr(review, "invoke_agent", resume)
    assert review.run_once(root)["status"] == "complete"
    assert seen == shas[1:]


def test_metadata_preparation_cannot_spend_reserved_issue_budget(commit_review_run, monkeypatch):
    root, shas, clock = commit_review_run
    config = json.loads((root / "config.json").read_text())
    review.write_json(root / "config.json", {**config, "issues_enabled": True})
    original = review.command
    observed, reviewed = [], []

    def slow_metadata(argv, **kwargs):
        if argv[0] == "github-client":
            assert clock[0] < 1200
            duration = min(150, kwargs["timeout"])
            clock[0] += duration
            if duration < 150:
                raise subprocess.TimeoutExpired(argv, duration)
            if argv[-1].endswith("/pulls"):
                sha = argv[-1].split("/")[-2]
                return json.dumps([{"merge_commit_sha": sha, "number": shas.index(sha)}])
            return "{}" if argv[1] == "pr" else "[]"
        return original(argv, **kwargs)

    def invoke(config, shard, prompt):
        sha = json.loads((shard / "plan.json").read_text())["commits"][0]["sha"]
        reviewed.append(sha)
        return result([sha])

    def intake(root, config, target, command, *, deadline, hydrate):
        assert hydrate is False
        observed.append(deadline - clock[0])
        return {"issues": [], "target": target}

    monkeypatch.setattr(review, "command", slow_metadata)
    monkeypatch.setattr(review, "invoke_agent", invoke)
    monkeypatch.setattr(review.issues, "plan_issues", intake)
    receipt = review.run_once(root)
    assert receipt["status"] == "partial"
    assert receipt["cursor_after"] == shas[len(reviewed)]
    assert observed == [600]
    assert clock[0] == 1200


def test_repair_budget_exhaustion_preserves_review_partial_receipt(commit_review_run, monkeypatch):
    root, shas, clock = commit_review_run
    config = json.loads((root / "config.json").read_text())
    review.write_json(root / "config.json", {**config, "repair_enabled": True, "repair_budget_seconds": 600})
    calls, repair_remaining = [], []

    def invoke(config, shard, prompt):
        clock[0] += config["timeout_seconds"]
        sha = json.loads((shard / "plan.json").read_text())["commits"][0]["sha"]
        calls.append(sha)
        return result([sha])

    def advance(root, config, run, deadline, invoke, **kwargs):
        repair_remaining.append(deadline - clock[0])
        return {"status": "complete", "entered": True, "attempted": ["existing-task"]}

    monkeypatch.setattr(review, "invoke_agent", invoke)
    monkeypatch.setattr(review.repair, "run_repairs", advance)
    receipt = review.run_once(root)
    assert calls == shas[1:3]
    assert receipt["status"] == "partial"
    assert receipt["completed_commits"] == shas[1:3]
    assert repair_remaining == [600]
    assert receipt["repair"]["entered"] is True
    assert receipt["daily_summary"]["repair_attempted"] == ["existing-task"]
    assert json.loads((root / "last-run.json").read_text())["status"] == "partial"
    assert json.loads((Path(receipt["run_dir"]) / "result.json").read_text())["outcome"] == "partial"
