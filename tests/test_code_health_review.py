from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import subprocess
import sys
import time

import pytest

from scripts import code_health_review as review
from scripts.code_health_review import agent_command, restore_review_instructions, run_lock, select_commits, validate_result


A, B, C, D = [letter * 40 for letter in "abcd"]
BACKFILL_REPORT = "原报告、完整讨论、冻结目标、真实入口及上下游、验证命令、退出码和证据缺口均已核对，结论与下一步单独记录。" * 8


@pytest.fixture
def backfill_environment(tmp_path, monkeypatch):
    source = tmp_path / "repository"
    source.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()

    git("init", "-q", "-b", "main")
    (source / "AGENTS.md").write_text("Read-only review fixture")
    (source / "example.py").write_text("value = 0\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (source / "example.py").write_text("value = 1\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "current")
    root = tmp_path / "state"
    root.mkdir()
    (root / "previous.md").write_text("Previous verified review")
    review.write_json(root / "state.json", {"cursor": base, "last_review": str(root / "previous.md")})
    config = {"git": "git", "gh": "github-fixture", "repository": "fixture/repo", "repository_url": str(source),
              "timeout_seconds": 12000, "review_budget_seconds": 6600, "issue_timeout_seconds": 3600,
              "issue_unit_timeout_seconds": 600, "review_unit_timeout_seconds": 600,
              "issues_enabled": True, "repair_enabled": False, "max_units": 10}
    review.write_json(root / "config.json", config)
    review.write_json(root / "installation.json", {"next_calendar_trigger": "unchanged"})
    review.write_json(root / "last-run.json", {"mode": "daily", "status": "unchanged"})
    inventory = [{"number": number, "title": f"Issue {number}", "body": "Original trigger", "state": "open",
                  "updated_at": "2026-11-07", "assignees": [],
                  "html_url": f"https://github.com/fixture/repo/issues/{number}"} for number in range(1, 65)]
    calls = []
    original = review.command

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[0] != "github-fixture":
            return original(argv, **kwargs)
        assert not any(word in argv for word in ("-X", "PATCH", "POST", "--method", "--input", "--field"))
        if argv[1:3] == ["issue", "list"]:
            return json.dumps(inventory)
        assert argv[:2] == ["github-fixture", "api"]
        path = argv[-1].removeprefix("repos/fixture/repo/")
        if path.startswith("issues?"):
            return json.dumps([inventory])
        if path.startswith("pulls?"):
            return "[[]]"
        if path.startswith("commits/"):
            return "[]"
        if "/comments?" in path or "/timeline?" in path:
            return "[[]]"
        return json.dumps(next(row for row in inventory if row["number"] == int(path.split("/")[1])))

    monkeypatch.setattr(review, "command", command)
    return root, config, inventory, calls, git


def backfill_decision(run, number, outcome):
    work = run / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "probe.log").write_text(json.dumps({"command": "probe original-trigger", "exit_code": 0,
                                               "output": "expected=1 actual=" + ("1" if outcome == "already_fixed" else "2")}) + "\n")
    finding = {"key": f"example:failure-{number}", "title": f"Failure {number}", "owner_path": "example.py",
               "failure_scenario": "original trigger", "expected": "1", "actual": "2", "evidence_log": "probe.log",
               "fix_scope": ["example.py", "tests/test_example.py"]} if outcome == "confirmed" else None
    closure = {"reason": "Original trigger now passes the complete expected assertions", "fix_commit": "",
               "verification_log": "probe.log", "commands": [{"command": "probe original-trigger", "exit_code": 0}]} if outcome == "already_fixed" else None
    return {"outcome": "complete", "report_markdown": BACKFILL_REPORT, "reviews": [{
        "number": number, "outcome": outcome, "report_markdown": BACKFILL_REPORT,
        "comment_draft": "Local recommendation only; GitHub remains open", "finding": finding,
        "closure_evidence": closure,
    }]}


def test_backfill_scans_64_checkpoints_batches_and_resumes_only_pending(backfill_environment, monkeypatch, capsys):
    root, _, inventory, calls, _ = backfill_environment
    preserved = {name: (root / name).read_bytes() for name in ("state.json", "config.json", "installation.json", "last-run.json")}
    invoked = []

    def invoke(config, run, prompt, **kwargs):
        number = json.loads((run / "plan.json").read_text())["issues"][0]["number"]
        invoked.append(number)
        assert config["timeout_seconds"] <= 600
        if number == 2:
            raise TimeoutError("one issue timed out")
        outcome = "already_fixed" if number % 4 == 0 else "confirmed" if number % 4 == 1 else "needs_decision"
        return backfill_decision(run, number, outcome)

    monkeypatch.setattr(review, "invoke_agent", invoke)
    receipt = review.run_issue_backfill(root)
    summary = receipt["issues"]["summary"]
    assert receipt["status"] == "partial"
    assert sorted(invoked) == [row["number"] for row in inventory]
    assert summary["counts"] == {"close_recommended": 16, "repair": 16, "needs_decision": 31, "pending": 1}
    assert summary["pending"] == [2]
    assert [len(batch) for batch in summary["closure_batches"]] == [10, 6]
    assert summary["github_closed"] == []
    run = Path(receipt["run_dir"])
    assert len(list((run / "issues/items").glob("*/result.json"))) == 63
    assert (run / "issues/items/2/pending.json").exists()
    assert "建议关闭" in (run / "issues/report.md").read_text()
    printed = capsys.readouterr().out
    assert '"issue_batch": 1' in printed and '"issue_batch": 7' in printed
    for name, original in preserved.items():
        assert (root / name).read_bytes() == original
    before = len(calls)
    original_plan = json.loads((run / "plan.json").read_text())
    original_input = next(row["input_digest"] for row in original_plan["issues"] if row["number"] == 2)
    inventory[1].update({"updated_at": "2026-11-08", "body": "Original trigger plus clarification"})
    resumed = []

    def finish(config, stage, prompt, **kwargs):
        resumed_plan = json.loads((stage / "plan.json").read_text())
        number = resumed_plan["issues"][0]["number"]
        assert resumed_plan["target"] == original_plan["target"]
        assert resumed_plan["issues"][0]["input_digest"] != original_input
        assert resumed_plan["issues"][0]["issue"]["body"] == inventory[1]["body"]
        resumed.append(number)
        return backfill_decision(stage, number, "needs_decision")

    monkeypatch.setattr(review, "invoke_agent", finish)
    complete = review.run_issue_backfill(root, resume_run=run)
    assert complete["status"] == "complete"
    assert resumed == [2]
    assert complete["issues"]["summary"]["counts"] == {
        "close_recommended": 16, "repair": 16, "needs_decision": 32, "pending": 0}
    assert not any("fetch" in argv for argv in calls[before:])
    assert sum("issues?" in argv[-1] for argv in calls[before:]) == 1
    assert len(list((root / "repairs").glob("*/task.json"))) == 16
    for name, original in preserved.items():
        assert (root / name).read_bytes() == original


def test_daily_phase_caps_are_independent_and_saved_time_is_available_to_repair(backfill_environment, monkeypatch):
    root, config, inventory, _, git = backfill_environment
    source = Path(config["repository_url"])
    (source / "example.py").write_text("value = 2\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "next")
    inventory[:] = inventory[:2]
    config.update({"review_budget_seconds": 600, "issue_timeout_seconds": 600, "repair_enabled": True,
                   "review_timeout_seconds": 9000})
    review.write_json(root / "config.json", config)
    clock = [0]
    monkeypatch.setattr(review.time, "monotonic", lambda: clock[0])
    calls = []

    def invoke(stage_config, stage, prompt, **kwargs):
        plan = json.loads((stage / "plan.json").read_text())
        calls.append(("issue" if "issues" in plan else "commit", stage_config["timeout_seconds"]))
        clock[0] += stage_config["timeout_seconds"]
        if "issues" in plan:
            return backfill_decision(stage, plan["issues"][0]["number"], "needs_decision")
        return {"outcome": "complete", "completed_commits": [plan["commits"][0]["sha"]],
                "report_markdown": BACKFILL_REPORT, "verification_gaps": [], "confirmed_findings": []}

    repair_budget = []
    monkeypatch.setattr(review, "invoke_agent", invoke)
    monkeypatch.setattr(review.repair, "repair_one", lambda root, config, run, deadline, invoke, **kwargs:
                        repair_budget.append(deadline - clock[0]) or {"status": "no_pending_repairs"})
    receipt = review.run_once(root)
    assert calls == [("commit", 600), ("issue", 600)]
    assert receipt["status"] == "partial"
    assert receipt["issues"]["status"] == "partial"
    assert receipt["issues"]["summary"]["pending"] == [2]
    assert repair_budget == [10800]


def test_backfill_partial_exit_is_not_reported_as_success(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["code_health_review.py", "issues-backfill", "--state-dir", str(tmp_path)])
    monkeypatch.setattr(review, "run_issue_backfill", lambda *args, **kwargs: {"status": "partial"})
    assert review.main() == 2


def fake_git(*args):
    if args == ("rev-list", "--first-parent", D):
        return "\n".join([D, C, B, A])
    if args == ("rev-list", "--first-parent", "--reverse", f"{A}..{D}"):
        return "\n".join([B, C, D])
    raise AssertionError(args)


def result(completed, outcome="partial"):
    return {
        "outcome": outcome,
        "completed_commits": completed,
        "report_markdown": "可追溯的审查证据及明确的未完成项。" * 20,
        "verification_gaps": [],
        "confirmed_findings": [],
    }


def test_selects_first_parent_units_in_order_without_skipping_direct_commits():
    assert select_commits(fake_git, A, D, 2) == [B, C]


@pytest.mark.parametrize("max_units", [1, 3, 10])
def test_review_prompt_uses_configured_mainline_limit(tmp_path, max_units):
    prompt = review.review_prompt(tmp_path, tmp_path / "run", {"cursor": A, "target": D}, max_units=max_units)
    assert f"按序最多{max_units}个主线单元" in prompt
    assert "最多三个主线单元" not in prompt


@pytest.mark.parametrize(("zone", "current", "expected"), [
    ("Asia/Shanghai", "2026-11-04T09:16:59", "2026-11-04T09:17:00+08:00"),
    ("Asia/Shanghai", "2026-11-04T09:17:00", "2026-11-05T09:17:00+08:00"),
    ("Asia/Shanghai", "2026-11-04T23:59:59", "2026-11-05T09:17:00+08:00"),
    ("Asia/Shanghai", "2026-12-31T23:59:59", "2027-01-01T09:17:00+08:00"),
    ("America/New_York", "2026-03-07T23:59:59", "2026-03-08T09:17:00-04:00"),
    ("America/New_York", "2026-10-31T23:59:59", "2026-11-01T09:17:00-05:00"),
])
def test_next_calendar_trigger_uses_next_local_0917(monkeypatch, zone, current, expected):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(current)

    try:
        with monkeypatch.context() as clock:
            clock.setenv("TZ", zone)
            time.tzset()
            clock.setattr(review, "datetime", Clock)
            assert review.next_calendar_trigger() == expected
    finally:
        time.tzset()


def test_missing_cursor_cannot_silently_restart_or_skip_history():
    with pytest.raises(ValueError, match="first-parent"):
        select_commits(fake_git, "e" * 40, D, 3)


@pytest.mark.parametrize("completed", [[C], [B, D], [B, C, D], "all"])
def test_noncontiguous_or_unplanned_results_cannot_advance(completed):
    with pytest.raises(ValueError, match="contiguous"):
        validate_result(result(completed), [B, C])


def test_only_completed_prefix_can_advance_after_partial_review():
    assert validate_result(result([B]), [B, C]) == [B]
    assert validate_result(result([B, C], "complete"), [B, C]) == [B, C]
    with pytest.raises(ValueError, match="every planned"):
        validate_result(result([B], "complete"), [B, C])
    with pytest.raises(ValueError, match="blocked"):
        validate_result(result([B], "blocked"), [B, C])


def test_result_without_report_cannot_advance():
    payload = result([B], "complete")
    payload["report_markdown"] = "done"
    with pytest.raises(ValueError, match="report"):
        validate_result(payload, [B])


def test_smoke_accepts_empty_plan_without_advancing():
    assert validate_result(result([], "complete"), []) == []


@pytest.mark.parametrize(("remaining", "expected"), [(1800, 600), (601, 600), (450, 450), (30, 30), (29, None), (0, None)])
def test_review_unit_budget_is_bounded_by_phase_remaining(monkeypatch, remaining, expected):
    monkeypatch.setattr(review.time, "monotonic", lambda: 0)
    config = {"timeout_seconds": 1800, "review_timeout_seconds": 900, "issue_timeout_seconds": 600, "repair_enabled": True}
    if expected is None:
        with pytest.raises(TimeoutError):
            review.review_stage_config(config, remaining)
    else:
        assert review.review_stage_config(config, remaining)["timeout_seconds"] == expected
    assert config["timeout_seconds"] == 1800


@pytest.mark.parametrize(("baseline", "expected"), [(0, 30), (60, 60), (450, 450), (1200, 1200), (2400, 1800)])
def test_review_unit_budget_honors_custom_baseline_and_unit_minimum(monkeypatch, baseline, expected):
    monkeypatch.setattr(review.time, "monotonic", lambda: 0)
    config = {"timeout_seconds": 1800, "review_unit_timeout_seconds": baseline}
    assert review.review_stage_config(config, 1800)["timeout_seconds"] == expected


@pytest.mark.parametrize("unit_count", [1, 3, 9])
def test_daily_review_invokes_one_model_per_planned_commit(tmp_path, monkeypatch, unit_count):
    import json
    import subprocess

    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()

    git("init", "-q", "-b", "main")
    (source / "AGENTS.md").write_text("Frozen review instructions")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    for index in range(unit_count):
        (source / "change.txt").write_text(str(index))
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", f"unit {index}")
    root = tmp_path / "state"
    root.mkdir()
    (root / "previous.md").write_text("previous report")
    review.write_json(root / "state.json", {"cursor": base, "last_review": str(root / "previous.md")})
    review.write_json(root / "config.json", {
        "git": "git", "gh": "github-client", "repository": "fixture/repo", "repository_url": str(source),
        "timeout_seconds": 1800, "review_timeout_seconds": 900, "max_units": 10, "repair_enabled": True,
    })
    original_command = review.command
    calls = []

    def command(argv, **kwargs):
        return "[]" if argv[0] == "github-client" else original_command(argv, **kwargs)

    def invoke(config, run, prompt):
        calls.append(config["timeout_seconds"])
        plan = json.loads((run / "plan.json").read_text())
        assert len(plan["commits"]) == 1
        return result([row["sha"] for row in plan["commits"]], "complete")

    monkeypatch.setattr(review, "command", command)
    monkeypatch.setattr(review, "invoke_agent", invoke)
    monkeypatch.setattr(review.time, "monotonic", lambda: 0)
    monkeypatch.setattr(review.repair, "repair_one", lambda *args, **kwargs: {"status": "no_pending_repairs"})
    receipt = review.run_once(root)
    assert calls == [600] * unit_count
    assert len(receipt["review_units"]) == unit_count
    assert len(receipt["planned_commits"]) == unit_count
    assert receipt["completed_commits"] == receipt["planned_commits"]
    assert receipt["status"] == "complete"


@pytest.mark.parametrize(("has_issues", "expected"), [(False, [600, 600, 600]), (True, [600, 600, 580])])
def test_commit_unit_limits_are_bounded_by_independent_review_budget(
    tmp_path, monkeypatch, has_issues, expected,
):
    import json

    clock = [0]
    monkeypatch.setattr(review.time, "monotonic", lambda: clock[0])
    run = tmp_path / "run"
    (run / "source").mkdir(parents=True)
    (run / "changes").mkdir()
    plan = {"cursor": A, "target": D, "previous_review": "previous report",
            "confirmed_finding_keys": [], "commits": [{"sha": sha} for sha in [B, C, D]]}
    state = {"cursor": A, "last_review": "previous report"}
    review.write_json(tmp_path / "state.json", state)
    config = {"max_units": 10, "timeout_seconds": 1800, "review_timeout_seconds": 900,
              "issue_timeout_seconds": 600, "repair_enabled": True}
    limits = []

    def invoke_agent(stage, unit, prompt):
        index = len(limits)
        limits.append(stage["timeout_seconds"])
        assert f"本阶段硬超时为{expected[index]}秒" in prompt
        clock[0] += [510, 110, 50][index]
        sha = json.loads((unit / "plan.json").read_text())["commits"][0]["sha"]
        return result([sha], outcome="complete")

    monkeypatch.setattr(review, "invoke_agent", invoke_agent)
    receipt = {}
    review.review_commits(tmp_path, config, run, plan, state, receipt, 1200 if has_issues else 1800)
    assert limits == expected
    assert receipt["status"] == "complete"
    assert receipt["completed_commits"] == [B, C, D]
    assert json.loads((tmp_path / "state.json").read_text())["cursor"] == D
    assert review.repair.remaining_config(config, 1800)["timeout_seconds"] == 1130
    assert config["timeout_seconds"] == 1800


@pytest.mark.parametrize("next_outcome", ["timeout", "partial", "budget", "report_write_error"])
def test_completed_unit_survives_later_incomplete_review_and_bounds_the_stage(tmp_path, monkeypatch, next_outcome):
    import json

    root, run = tmp_path, tmp_path / "run"
    run.mkdir()
    (run / "source").mkdir()
    (run / "changes").mkdir()
    old = root / "previous.md"
    old.write_text("previous review")
    state = {"cursor": A, "last_review": str(old)}
    review.write_json(root / "state.json", state)
    config = {"timeout_seconds": 1800, "review_timeout_seconds": 900, "issue_timeout_seconds": 600,
              "max_units": 10, "repair_enabled": True}
    plan = {"cursor": A, "target": D, "previous_review": old.read_text(), "confirmed_finding_keys": [],
            "commits": [{"sha": B}, {"sha": C}, {"sha": D}]}
    receipt = {"mode": "daily", "cursor_before": A, "target": D, "planned_commits": [B, C, D]}
    clock, calls = [0], []
    monkeypatch.setattr(review.time, "monotonic", lambda: clock[0])

    def invoke(cfg, stage, prompt):
        current = json.loads((stage / "plan.json").read_text())
        sha, = [row["sha"] for row in current["commits"]]
        calls.append((sha, cfg["timeout_seconds"]))
        assert f"硬超时为{cfg['timeout_seconds']}秒" in prompt
        assert "不是额外30分钟" in prompt
        assert f"冻结源码：{run / 'source'}" in prompt
        if sha == C:
            assert json.loads((root / "state.json").read_text())["cursor"] == B
            checkpoint = json.loads((run / "receipt.json").read_text())
            assert checkpoint["completed_commits"] == [B]
            assert "finished_at" not in checkpoint
            if next_outcome == "timeout":
                raise TimeoutError("unit budget exceeded")
            if next_outcome == "report_write_error":
                original_write = Path.write_text

                def interrupted_write(path, value, *args, **kwargs):
                    if path == run / "report.md.tmp":
                        original_write(path, value[:10], *args, **kwargs)
                        raise OSError("injected interrupted report write")
                    return original_write(path, value, *args, **kwargs)

                monkeypatch.setattr(Path, "write_text", interrupted_write)
                return result([C], "complete")
            return result([], "partial")
        assert sha == B
        work = stage / "work"
        work.mkdir()
        (work / "probe.log").write_text("assert actual == expected; actual=2 expected=1\n")
        finding = {"key": "fixture:wrong-value", "title": "Wrong value", "commit": B,
                   "owner_path": "example.py", "failure_scenario": "read value", "expected": "1", "actual": "2",
                   "evidence_log": str(work / "probe.log"), "fix_scope": ["example.py", "tests/test_example.py"]}
        clock[0] += 1190 if next_outcome == "budget" else 250
        return {**result([B], "complete"), "confirmed_findings": [finding]}

    monkeypatch.setattr(review, "invoke_agent", invoke)
    if next_outcome == "report_write_error":
        with pytest.raises(OSError, match="interrupted report"):
            review.review_commits(root, config, run, plan, state, receipt, 1200)
        saved_state = json.loads((root / "state.json").read_text())
        assert saved_state["cursor"] == B
        saved_report = Path(saved_state["last_review"]).read_text()
        assert result([B], "complete")["report_markdown"] in saved_report
        assert f"## 提交 `{C}`" not in saved_report
        assert json.loads((run / "receipt.json").read_text())["completed_commits"] == [B]
        return
    review.review_commits(root, config, run, plan, state, receipt, 1200)
    assert calls == ([(B, 600)] if next_outcome == "budget" else [(B, 600), (C, 600)])
    assert receipt["status"] == "partial" and receipt["completed_commits"] == [B]
    assert json.loads((root / "state.json").read_text())["cursor"] == B
    aggregate = json.loads((run / "result.json").read_text())
    assert validate_result(aggregate, [B, C, D], work=run / "work") == [B]
    assert len(aggregate["confirmed_findings"]) == 1
    evidence = run / "work" / aggregate["confirmed_findings"][0]["evidence_log"]
    assert evidence.read_text().startswith("assert actual")
    task_path, = (root / "repairs").glob("*/task.json")
    task = json.loads(task_path.read_text())
    assert task["finding"]["evidence_log"] == "probe.log"
    assert Path(task["evidence"]).is_file()
    assert "待续" in (run / "report.md").read_text()
    assert clock[0] < 1800 - 600


def test_last_partial_unit_is_not_promoted_to_complete(tmp_path, monkeypatch):
    import json

    run = tmp_path / "run"
    run.mkdir()
    (run / "source").mkdir()
    (run / "changes").mkdir()
    plan = {"cursor": A, "target": B, "previous_review": "history", "confirmed_finding_keys": [], "commits": [{"sha": B}]}
    state = {"cursor": A, "last_review": "history"}
    receipt = {}
    config = {"timeout_seconds": 1800, "max_units": 10}
    monkeypatch.setattr(review.time, "monotonic", lambda: 0)
    monkeypatch.setattr(review, "invoke_agent", lambda *a, **k: {**result([B], "partial"), "verification_gaps": ["follow-up"]})
    review.review_commits(tmp_path, config, run, plan, state, receipt, 1800)
    aggregate = json.loads((run / "result.json").read_text())
    assert aggregate["outcome"] == receipt["status"] == "partial"
    assert aggregate["completed_commits"] == receipt["completed_commits"] == [B]
    assert "follow-up" in aggregate["verification_gaps"]
    assert json.loads((tmp_path / "state.json").read_text())["cursor"] == B


def test_concurrent_runs_cannot_share_cursor_lock(tmp_path):
    with run_lock(tmp_path):
        with pytest.raises(RuntimeError, match="another daily"):
            with run_lock(tmp_path):
                pytest.fail("lock admitted a concurrent writer")


def test_archive_export_ignore_does_not_remove_frozen_review_instructions(tmp_path):
    calls = []

    def git(*args):
        calls.append(args)
        if args == ("ls-tree", "-r", "--name-only", D):
            return "AGENTS.md\nCLAUDE.md\nsrc/AGENTS.md\nsrc/code.py"
        assert args[0] == "show" and args[1].startswith(D + ":")
        return "frozen development instructions"

    restore_review_instructions(git, D, tmp_path)
    assert (tmp_path / "AGENTS.md").read_text() == "frozen development instructions\n"
    assert (tmp_path / "src/AGENTS.md").is_file()
    assert not (tmp_path / "src/code.py").exists()
    assert len(calls) == 4


@pytest.mark.parametrize("installed", [False, True])
def test_daily_run_resumes_pending_repair_without_new_prs(tmp_path, monkeypatch, installed):
    import json
    installation = {
        "installed_at": "2026-11-03T05:16:41+00:00", "schedule": {"hour": 9, "minute": 17},
        "next_calendar_trigger": "2026-11-04T09:17:00+08:00",
    }
    installation_path = tmp_path / "installation.json"
    if installed:
        installation_path.write_text(json.dumps(installation))
    expected_trigger = "2026-11-05T09:17:00+08:00"
    monkeypatch.setattr(review, "next_calendar_trigger", lambda: expected_trigger)
    (tmp_path / "repository.git").mkdir()
    (tmp_path / "report.md").write_text("previous review")
    (tmp_path / "config.json").write_text(json.dumps({
        "git": "git", "repository_url": "unused", "repository": "owner/repo", "max_units": 3,
        "timeout_seconds": 1800, "repair_enabled": True,
    }))
    (tmp_path / "state.json").write_text(json.dumps({"cursor": A, "last_review": str(tmp_path / "report.md")}))
    def command(argv, **kwargs):
        if "fetch" in argv:
            return ""
        if "rev-parse" in argv or argv[-3:] == ["rev-list", "--first-parent", A]:
            return A
        if "rev-list" in argv:
            return ""
        raise AssertionError(argv)
    monkeypatch.setattr(review, "command", command)
    observed = []
    def resume(root, config, run, deadline, invoke, *, revalidate_issue=None):
        assert callable(revalidate_issue)
        observed.append(root)
        return {"status": "awaiting_delivery_authorization", "id": "existing"}
    monkeypatch.setattr(review.repair, "repair_one", resume)
    receipt = review.run_once(tmp_path)
    assert receipt["status"] == "no_changes"
    assert receipt["repair"]["status"] == "awaiting_delivery_authorization"
    assert observed == [tmp_path]
    assert json.loads((tmp_path / "state.json").read_text())["cursor"] == A
    assert json.loads((tmp_path / "last-run.json").read_text()) == receipt
    if installed:
        assert json.loads(installation_path.read_text()) == {**installation, "next_calendar_trigger": expected_trigger}
    else:
        assert not installation_path.exists()


@pytest.mark.parametrize("missing_evidence", [False, True])
@pytest.mark.parametrize("max_units", [3, 10])
def test_daily_entry_persists_findings_before_cursor_with_real_git(tmp_path, monkeypatch, missing_evidence, max_units):
    import json
    import subprocess
    import sys

    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()

    git("init", "-q", "-b", "main")
    (source / "AGENTS.md").write_text("Frozen fixture instructions\n")
    (source / ".gitattributes").write_text("AGENTS.md export-ignore\n")
    (source / "example.py").write_text("value = 1\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture base")
    base = git("rev-parse", "HEAD")
    (source / "example.py").write_text("value = 2\n")
    git("add", "example.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture change")
    target = git("rev-parse", "HEAD")

    root = tmp_path / "state"
    root.mkdir()
    previous = root / "previous.md"
    previous.write_text("previous review\n")
    (root / "policy.md").write_text("Fixture review policy\n")
    (root / "state.json").write_text(json.dumps({"cursor": base, "last_review": str(previous)}))
    unrelated = root / "repairs/unrelated/task.json"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text('{"status": "blocked", "finding": {"key": "unrelated"}}\n')
    unrelated_before = unrelated.read_bytes()
    finding = {
        "key": "example:value", "title": "Fixture value differs", "commit": target,
        "owner_path": "example.py", "failure_scenario": "read value",
        "expected": "1", "actual": "2", "evidence_log": "probe.log",
        "fix_scope": ["example.py", "tests/test_example.py"],
    }
    payload = {**result([target], "complete"), "confirmed_findings": [finding]}
    agent = tmp_path / "model-client"
    agent.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "assert args[:4] == ['exec', '--ephemeral', '--ignore-user-config', '--skip-git-repo-check']\n"
        "assert args[args.index('--sandbox') + 1] == 'workspace-write'\n"
        "assert 'sandbox_workspace_write.network_access=false' in args\n"
        "assert 'sandbox_workspace_write.writable_roots=[]' in args\n"
        "work = pathlib.Path(args[args.index('--cd') + 1])\n"
        "plan = json.loads((work.parent / 'plan.json').read_text())\n"
        f"assert plan['cursor'] == {base!r} and plan['target'] == {target!r}\n"
        "assert (work.parents[3] / 'source/AGENTS.md').read_text() == 'Frozen fixture instructions\\n'\n"
        "prompt = sys.stdin.read()\n"
        "assert '禁止修业务代码' in prompt\n"
        f"assert '按序最多{max_units}个主线单元' in prompt\n"
        + ("" if missing_evidence else "(work / 'probe.log').write_text('expected=1 actual=2\\n')\n")
        + f"pathlib.Path(args[args.index('--output-last-message') + 1]).write_text({json.dumps(payload)!r})\n"
    )
    agent.chmod(0o700)
    (root / "config.json").write_text(json.dumps({
        "git": "git", "gh": "github-client", "codex": str(agent),
        "repository_url": str(source), "repository": "fixture/repo", "max_units": max_units,
        "timeout_seconds": 180, "repair_enabled": False,
        "model": "fixture", "model_base_url": "http://fixture.invalid",
        "model_catalog": str(root / "catalog.json"),
    }))

    original_command, original_write = review.command, review.write_json
    calls, forbidden, durable_at_advance = [], [], []
    responses = {
        ("github-client", "api", f"repos/fixture/repo/commits/{target}/pulls"):
            [{"number": 1, "merge_commit_sha": target}],
        ("github-client", "pr", "view", "1", "--repo", "fixture/repo", "--json",
         "headRefOid,mergeCommit,reviews,statusCheckRollup"):
            {"headRefOid": target, "mergeCommit": {"oid": target}, "reviews": [], "statusCheckRollup": []},
        ("github-client", "issue", "list", "--repo", "fixture/repo", "--state", "all", "--limit", "100",
         "--json", "number,title,state,url"): [],
    }

    def external_clients(argv, **kwargs):
        calls.append(tuple(argv))
        if tuple(argv) in responses:
            return json.dumps(responses[tuple(argv)])
        if argv[0] == "git" and not {"push", "commit", "merge", "reset", "clean"}.intersection(argv[1:]):
            return original_command(argv, **kwargs)
        forbidden.append(tuple(argv))
        raise AssertionError(f"forbidden external call: {argv}")

    def observe_cursor(path, value):
        if path == root / "state.json":
            records = [json.loads(p.read_text()) for p in (root / "repairs").glob("*/task.json")]
            durable = [item for item in records if item.get("base_sha") == target]
            assert len(durable) == 1 and durable[0]["finding"] == finding
            assert Path(durable[0]["evidence"]).read_text() == "expected=1 actual=2\n"
            durable_at_advance.append(value["cursor"])
        return original_write(path, value)

    monkeypatch.setattr(review, "command", external_clients)
    monkeypatch.setattr(review, "write_json", observe_cursor)
    if missing_evidence:
        with pytest.raises(ValueError, match="durable reproduction evidence"):
            review.run_once(root)
    else:
        receipt = review.run_once(root)
        assert receipt["status"] == "complete"
        assert len(receipt["repair_tasks"]) == 1
        plan = json.loads((Path(receipt["run_dir"]) / "plan.json").read_text())
        assert plan["commits"][0]["pull_requests"][0]["review_evidence"]["headRefOid"] == target
        assert (Path(receipt["run_dir"]) / "report.md").is_file()
    assert json.loads((root / "state.json").read_text())["cursor"] == (base if missing_evidence else target)
    assert durable_at_advance == ([] if missing_evidence else [target])
    assert unrelated.read_bytes() == unrelated_before
    assert len(list((root / "repairs").glob("*/task.json"))) == (1 if missing_evidence else 2)
    assert [call for call in calls if call[0] == "github-client"] == list(responses)
    assert forbidden == []
    assert git("status", "--porcelain") == ""


def test_model_execution_timeout_remains_resumable(tmp_path, monkeypatch):
    import subprocess

    class TimedOutModel:
        pid = 12345
        returncode = -15

        def __init__(self, *args, **kwargs):
            pass

        def communicate(self, prompt, timeout):
            raise subprocess.TimeoutExpired("model", timeout)

        def wait(self, timeout):
            return -15

    monkeypatch.setattr(review.subprocess, "Popen", TimedOutModel)
    monkeypatch.setattr(review.os, "killpg", lambda *_: None)
    monkeypatch.setattr(review, "agent_command", lambda *args, **kwargs: ["unused"])
    with pytest.raises(TimeoutError, match="resume"):
        review.invoke_agent({"timeout_seconds": 1}, tmp_path / "stage", "read only")


def test_agent_writes_only_run_workspace_without_network_or_approval():
    argv = agent_command(
        {"codex": "/test/codex", "model": "test", "model_base_url": "http://localhost", "model_catalog": "/catalog"},
        Path("/run/work"), Path("/run/schema"), Path("/run/result"),
    )
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--cd") + 1] == "/run/work"
    assert "sandbox_workspace_write.network_access=false" in argv
    assert "sandbox_workspace_write.writable_roots=[]" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert '--add-dir' not in argv
    assert '--ignore-user-config' in argv
    assert 'approval_policy="never"' in argv
