from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import time

import pytest

from scripts import code_health_delivery as delivery
from scripts import code_health_issues as issues
from scripts import code_health_pull_requests as prs
from scripts import code_health_repair as repair
from scripts import code_health_review as review
from test_code_health_issues import process, response
from test_code_health_pull_requests import git, passing_checks, repository as pr_repository  # noqa: F401
from test_code_health_review_shards import commit_review_run  # noqa: F401


A, B, C = [letter * 40 for letter in "abc"]


@pytest.mark.parametrize(("decision", "service", "outcome", "disposition"), [
    ("not_worthwhile", False, "not_worthwhile", "reply"),
    ("needs_decision", False, "needs_decision", "needs_decision"),
    ("worthwhile", True, "needs_decision", "needs_decision"),
    ("not_worthwhile", True, "needs_decision", "needs_decision"),
])
def test_confirmed_reproduction_is_not_automatic_repair_admission(tmp_path, decision, service, outcome, disposition):
    payload = response(outcome="confirmed", triage={"decision": decision,
        "reason": "Maintenance cost exceeds the value, or a product decision is needed.", "third_party_service": service})
    result = process(tmp_path, payload)
    record = issues.read_state(tmp_path)["issues"]["1"]
    assert result["reviewed"] == [1] and result["repair_tasks"] == []
    assert record["outcome"] == outcome and record["disposition"] == disposition
    assert not list((tmp_path / "repairs").glob("*/task.json"))


def test_missing_value_triage_cannot_grandfather_an_old_confirmation(tmp_path):
    payload = response(outcome="confirmed")
    del payload["reviews"][0]["triage"]
    assert process(tmp_path, payload)["reviewed"] == []
    previous = {"target": A, "input_digest": "old", "outcome": "confirmed", "reviewed_at": "2026-11-01"}
    assert issues.completed_review(previous, A, "old") is None


def test_defaults_and_independent_capacity_preserve_existing_allocations():
    config = review.runtime_config({"issues_enabled": True, "repair_enabled": True})
    assert {key: config[key] for key in ("pull_requests_enabled", "comments_enabled", "release_enabled",
                                        "pull_request_merge_enabled", "pull_request_close_enabled")} == {
        "pull_requests_enabled": True, "comments_enabled": True, "release_enabled": True,
        "pull_request_merge_enabled": True, "pull_request_close_enabled": False}
    assert config["max_repairs"] == 1 and config["max_pull_requests"] == 3
    assert review.phase_budgets(config) == {"total": 15000, "review": 6600, "issues": 3600,
                                          "repair_reserved": 1800, "pull_requests": 1800, "delivery": 1200}
    for total in (12000, 14000):
        with pytest.raises(ValueError, match="increase timeout_seconds"):
            review.phase_budgets({**config, "timeout_seconds": total})
    disabled = review.phase_budgets({**config, "pull_requests_enabled": False, "comments_enabled": False,
                                     "release_enabled": False, "timeout_seconds": 12000})
    assert disabled["review"] == 6600 and disabled["issues"] == 3600 and disabled["repair_reserved"] == 1800
    smoke = review.phase_budgets(config, smoke=True)
    assert all(smoke[key] == 0 for key in ("issues", "repair_reserved", "pull_requests", "delivery"))


@pytest.mark.parametrize("key", ["comments_enabled", "release_enabled", "pull_requests_enabled", "pull_request_merge_enabled"])
def test_write_and_pr_switches_reject_truthy_non_booleans(key):
    with pytest.raises(ValueError, match="must be a boolean"):
        review.runtime_config({key: "false"})


def test_pr_close_switch_cannot_authorize_closing_any_pr():
    with pytest.raises(ValueError, match="per-PR human decision"):
        review.runtime_config({"pull_request_close_enabled": True})


def test_phase_carryover_moves_unused_budget_forward_but_protects_reserves(commit_review_run, monkeypatch):
    root, _, clock = commit_review_run
    config = json.loads((root / "config.json").read_text())
    config.update({"timeout_seconds": 15000, "pull_requests_enabled": True, "comments_enabled": True,
                   "release_enabled": True, "issues_enabled": True, "repair_enabled": True,
                   "review_budget_seconds": 6600, "issue_timeout_seconds": 3600})
    review.write_json(root / "config.json", config)
    seen = []
    def agent(config, stage, prompt):
        plan = json.loads((stage / "plan.json").read_text())
        clock[0] += 1
        return {"outcome": "complete", "completed_commits": [plan["commits"][0]["sha"]],
                "report_markdown": "审查每个逻辑并记录完整复核和测试证据。" * 20,
                "verification_gaps": [], "confirmed_findings": []}
    def plan(root, config, target, command, *, deadline, hydrate):
        seen.append(("issues", deadline - clock[0]))
        clock[0] += 3600
        return {"issues": [], "target": target, "issue_inputs": {}}
    def intake(*args, **kwargs):
        return {"status": "no_eligible_issues", "reviewed": []}
    def pull(root, config, run, deadline, invoke, *, command):
        seen.append(("pr", deadline - clock[0]))
        clock[0] += 1800
        raise TimeoutError("PR stage used only its own budget")
    def fix(root, config, run, source, target, plan, status, deadline):
        seen.append(("repair", deadline - clock[0]))
        clock[0] = deadline
        return {"inventory": {}}
    def deliver(root, config, run, deadline, invoke, **kwargs):
        seen.append(("delivery", deadline - clock[0]))
        return {"status": "complete"}
    monkeypatch.setattr(review, "invoke_agent", agent)
    monkeypatch.setattr(issues, "plan_issues", plan)
    monkeypatch.setattr(issues, "process_issues", intake)
    monkeypatch.setattr(prs, "process_pull_requests", pull)
    monkeypatch.setattr(review, "run_repair_phase", fix)
    monkeypatch.setattr(delivery, "process_delivery", deliver)
    receipt = review.run_once(root)
    # Review finishes four units in four clock ticks, so 6596 of its 6600 seconds flow to
    # issues and then, because intake finishes before its augmented deadline, to PRs.
    assert seen[:2] == [("issues", 3600 + 6596), ("pr", 1800 + 6596)]
    assert seen[2][0] == "repair" and seen[2][1] >= 1800
    assert seen[3] == ("delivery", 1200)
    assert receipt["pull_requests"]["status"] == "pending"
    assert receipt["daily_summary"]["reviewed_commits"] == 4


def test_unused_review_budget_flows_to_prs_when_issues_are_disabled(commit_review_run, monkeypatch):
    root, _, clock = commit_review_run
    config = json.loads((root / "config.json").read_text())
    config.update({"timeout_seconds": 15000, "pull_requests_enabled": True, "comments_enabled": True,
                   "release_enabled": True, "issues_enabled": False, "repair_enabled": True,
                   "review_budget_seconds": 6600})
    review.write_json(root / "config.json", config)
    seen = []
    def agent(config, stage, prompt):
        plan = json.loads((stage / "plan.json").read_text())
        clock[0] += 1
        return {"outcome": "complete", "completed_commits": [plan["commits"][0]["sha"]],
                "report_markdown": "审查每个逻辑并记录完整复核和测试证据。" * 20,
                "verification_gaps": [], "confirmed_findings": []}
    def pull(root, config, run, deadline, invoke, *, command):
        seen.append(("pr", deadline - clock[0]))
        raise TimeoutError("PR stage used only its own budget")
    def fix(root, config, run, source, target, plan, status, deadline):
        seen.append(("repair", deadline - clock[0]))
        return {"inventory": {}}
    def deliver(root, config, run, deadline, invoke, **kwargs):
        seen.append(("delivery", deadline - clock[0]))
        return {"status": "complete"}
    monkeypatch.setattr(review, "invoke_agent", agent)
    monkeypatch.setattr(prs, "process_pull_requests", pull)
    monkeypatch.setattr(review, "run_repair_phase", fix)
    monkeypatch.setattr(delivery, "process_delivery", deliver)
    review.run_once(root)
    # Review leaves 6596 seconds unused and there is no issue stage to absorb them.
    assert seen[0] == ("pr", 1800 + 6596)


def test_pull_request_agent_no_output_skips_repair_and_delivery(commit_review_run, monkeypatch):
    root, _, clock = commit_review_run
    config = json.loads((root / "config.json").read_text())
    config.update({"timeout_seconds": 15000, "pull_requests_enabled": True, "comments_enabled": True,
                   "release_enabled": True, "issues_enabled": True, "repair_enabled": True,
                   "review_budget_seconds": 6600, "issue_timeout_seconds": 3600})
    review.write_json(root / "config.json", config)
    calls = []
    def agent(config, stage, prompt):
        plan = json.loads((stage / "plan.json").read_text())
        clock[0] += 1
        return {"outcome": "complete", "completed_commits": [plan["commits"][0]["sha"]],
                "report_markdown": "审查每个逻辑并记录完整复核和测试证据。" * 20,
                "verification_gaps": [], "confirmed_findings": []}
    def intake(*args, **kwargs):
        return {"issues": [], "target": "fixture", "issue_inputs": {}}
    def pull(*args, **kwargs):
        calls.append("pull_requests")
        return {"status": "partial", "reviewed": [1], "pending": [1, 2], "results": [],
                "agent_unavailable": {"pr": 1, "reason": "no output",
                                      "diagnostics": {"failure_kind": "agent_no_output"}}}
    def fix(*args, **kwargs):
        calls.append("repair")
        return {"inventory": {}}
    def deliver(*args, **kwargs):
        calls.append("delivery")
        return {"status": "complete"}
    monkeypatch.setattr(review, "invoke_agent", agent)
    monkeypatch.setattr(issues, "plan_issues", intake)
    monkeypatch.setattr(issues, "process_issues",
                        lambda *args, **kwargs: {"status": "no_eligible_issues", "reviewed": []})
    monkeypatch.setattr(prs, "process_pull_requests", pull)
    monkeypatch.setattr(review, "run_repair_phase", fix)
    monkeypatch.setattr(delivery, "process_delivery", deliver)
    receipt = review.run_once(root)
    assert calls == ["pull_requests"]
    assert receipt["repair"]["status"] == "skipped"
    assert receipt["delivery"]["status"] == "skipped"
    assert receipt["daily_summary"]["agent_unavailable"]["pr"] == 1
    assert "模型无输出" in (Path(receipt["run_dir"]) / "report.md").read_text()


@pytest.fixture
def comment_transport(tmp_path):
    delivery.save_json(tmp_path / "issue-state.json", {"issues": {"7": {
        "triage": {"decision": "not_worthwhile"}, "reviewed_at": "2026-11-09"}}})
    config = {"gh": "gh-fixture", "repository": "fixture/repo"}
    comments, calls = [], []
    fail = {"after_post": False, "before_get": False}
    def command(argv, **kwargs):
        calls.append(argv)
        if fail["before_get"]:
            raise RuntimeError("temporary connection failure")
        if argv[1:] == ["api", "user"]:
            return json.dumps({"login": "owner"})
        if argv[1:3] == ["issue", "comment"]:
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            comments.append({"id": 101, "body": body, "user": {"login": "owner"}, "html_url": "https://example.invalid/comment/101"})
            if fail["after_post"]:
                raise TimeoutError("response lost after GitHub accepted the reply")
            return comments[-1]["html_url"]
        if "comments?" in argv[-1]:
            return json.dumps([comments])
        if argv[-1].endswith("/issues/7"):
            return json.dumps({"number": 7, "state": "open"})
        pytest.fail(str(argv))
    def send(**extra):
        return delivery.comment(tmp_path, config, tmp_path / "run", time.monotonic() + 30,
                                command, kind="issue", number=7, body="Not worthwhile: maintenance cost exceeds user benefit.", **extra)
    return tmp_path, config, comments, calls, fail, command, send


def test_comments_are_read_back_and_sent_once(comment_transport):
    root, _, rows, calls, _, _, send = comment_transport
    assert send()["status"] == "posted"
    assert send()["id"] == 101
    assert len(rows) == 1 and sum(call[1:3] == ["issue", "comment"] for call in calls) == 1
    assert issues.own_comment_ids(root, 7) == [101]
    assert not any("PATCH" in call or "close" in call for call in calls)


@pytest.mark.parametrize("failure", ["before_get", "after_post"])
def test_comment_failures_keep_outbox_and_recover_without_duplicates(comment_transport, failure):
    root, config, rows, calls, fail, command, send = comment_transport
    fail[failure] = True
    with pytest.raises((RuntimeError, TimeoutError)):
        send()
    assert next(iter(delivery.read_json(root / "comment-state.json", {}).values()))["status"] == "pending"
    fail[failure] = False
    result = delivery.process_delivery(root, {**config, "release_enabled": False}, root / "retry", time.monotonic() + 30,
                                      None, command=command, receipt={})
    assert not result["errors"] and len(rows) == 1
    assert sum(call[1:3] == ["issue", "comment"] for call in calls) == 1


def test_comments_cannot_target_an_unhandled_issue_or_pr(tmp_path):
    for kind in ("issue", "pr"):
        with pytest.raises(ValueError, match="only locally handled"):
            delivery.comment(tmp_path, {"repository": "fixture/repo"}, tmp_path, time.monotonic() + 30,
                             lambda *a, **k: pytest.fail("no GitHub access"), kind=kind, number=3, body="unhandled")


def test_own_replies_do_not_invalidate_issue_but_real_feedback_does():
    issue = {"number": 1, "title": "Bug", "body": "Original", "state": "open", "updated_at": "before", "comments": 0}
    before = issues.issue_input_digest(issue, [])
    changed = {**issue, "updated_at": "after", "comments": 1}
    own = [{"id": 100, "body": "owned reply"}]
    assert issues.issue_input_digest(changed, [], own, ignored_comment_ids=[100]) == before
    assert issues.issue_input_digest(changed, [], own + [{"id": 101, "body": "new user evidence"}], ignored_comment_ids=[100]) != before


def test_own_pr_comment_is_not_new_input_but_forged_marker_is():
    pr = {"number": 1, "headRefOid": A, "comments": []}
    config = {"_own_pr_comments": [{"number": 1, "actor": "owner", "marker": "<!-- owned -->"}]}
    before = prs._inputs(pr, B, config)
    own = {**pr, "updatedAt": "after", "comments": [{"author": {"login": "owner"}, "body": "<!-- owned --> reply"}]}
    assert prs._inputs(own, B, config) == before
    own["comments"][0]["author"]["login"] = "someone-else"
    assert prs._inputs(own, B, config) != before


@pytest.mark.parametrize("refusal", [None, "config", "head", "tests", "service", "worktree"])
def test_only_exact_locally_verified_pr_can_be_squash_merged(pr_repository, refusal):
    ctx = pr_repository
    summary = ctx.run()
    row = summary["results"][0]
    assert row["status"] == "merge_eligible"
    mutations = []
    merged = {}
    config = {**ctx.config, "pull_request_merge_enabled": refusal != "config"}
    if refusal == "head":
        ctx.items[0]["headRefOid"] = C
    elif refusal == "tests":
        row["regression_verification"]["exit_code"] = 1
    elif refusal == "service":
        row["review"]["third_party_service"] = True
    elif refusal == "worktree":
        (Path(row["worktree"]) / "calc.py").write_text("unverified edits\n")
    def command(argv, **kwargs):
        if argv[0] == "gh-fixture" and argv[1:3] == ["pr", "merge"]:
            mutations.append(argv)
            assert "--squash" in argv and argv[argv.index("--match-head-commit") + 1] == ctx.head
            git(ctx.seed, "switch", "main")
            git(ctx.seed, "merge", "--squash", ctx.head)
            git(ctx.seed, "commit", "-m", "fixture squash")
            merged.update(number=1, state="MERGED", headRefOid=ctx.head, baseRefName="main",
                          mergedAt="2026-11-09", mergeCommit={"oid": git(ctx.seed, "rev-parse", "HEAD")})
            return ""
        if merged and argv[0] == "gh-fixture" and argv[1:3] == ["pr", "view"]:
            return json.dumps(merged)
        return ctx.command(argv, **kwargs)
    delivery.merge_pull_requests(ctx.root, config, ctx.root / "merge", time.monotonic() + 60, command=command, receipt=summary)
    if refusal is None:
        assert row["status"] == "merged" and len(mutations) == 1
        assert git(ctx.seed, "rev-parse", "HEAD^1") == ctx.base
        assert git(ctx.seed, "rev-parse", "HEAD^{tree}") == git(ctx.seed, "rev-parse", ctx.head + "^{tree}")
    else:
        assert not mutations and row["status"] != "merged"
    state = delivery.read_json(ctx.root / "pull-request-state.json", {})
    assert state["pull_requests"]["1"]["result"]["head"] == row["head"]


def release_plan(commits=(A, B)):
    return {"outcome": "ready", "reason": "Full delta reviewed as patch-only.", "third_party_service": False,
            "release_notes": [{"category": "Bug Fixes", "text": "Preserve the complete operand result.", "commits": list(commits)}],
            "no_release_note": [], "design_evidence": [{"commit": sha, "references": ["docs/CODE_HEALTH_REVIEW.md"]} for sha in commits]}


@pytest.mark.parametrize("case", ["missing", "extra", "overlap", "design", "third_party", "major"])
def test_release_plan_cannot_hide_or_expand_the_full_main_delta(case):
    plan = release_plan()
    if case == "missing":
        plan["release_notes"][0]["commits"] = [A]
    elif case == "extra":
        plan["release_notes"][0]["commits"].append(C)
    elif case == "overlap":
        plan["no_release_note"] = [{"commit": A, "reason": "No note"}]
    elif case == "design":
        plan["design_evidence"].pop()
    elif case == "third_party":
        plan["third_party_service"] = True
    else:
        plan["release_notes"][0]["category"] = "Breaking Changes"
    with pytest.raises(ValueError):
        delivery.validate_release_plan(plan, [A, B], "fixture/repo")


def test_release_changelog_preserves_prior_notes_and_history():
    original = "# Changes\n\n## Unreleased\n\n### Bug Fixes\n\n- Earlier fix\n\n## 1.2.3 - 2026-11-01\n\nHistorical note\n"
    notes = release_plan()["release_notes"]
    with pytest.raises(ValueError, match="dropped"):
        delivery.release_changelog(original, notes)
    notes.append({"category": "Bug Fixes", "text": "Earlier fix", "commits": [A]})
    rendered = delivery.release_changelog(original, notes, "1.2.4")
    assert "## Unreleased\n\n## 1.2.4" in rendered
    assert "- Earlier fix" in rendered and rendered.endswith("Historical note\n")
    with pytest.raises(ValueError, match="product/version decision"):
        delivery.release_changelog(original.replace("Bug Fixes", "New Features"), notes)


@pytest.fixture
def release_observer(tmp_path):
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "scripts/runtime_bundle.lock.json").write_text(json.dumps({"release_platforms": ["macos-arm64"]}))
    task = {"pr_number": 9, "head": A, "target": B, "version": "1.2.4", "worktree": str(tree), "sources": []}
    stem = "options-monitor-v1.2.4-macos-arm64.tar.gz"
    names = {"options-monitor-v1.2.4.tar.gz", "om-agent-spec.json", "VERSION", "om-plugins.json", "om-plugins.md",
             *(stem + suffix for suffix in ("", ".sha256", ".manifest.json", ".qualification.json")),
             *(f"plugin{index}.whl" for index in range(7))}
    state = {"pr": {"number": 9, "state": "MERGED", "headRefOid": A, "baseRefName": "main", "mergeCommit": {"oid": C},
                     "mergedAt": "2026-11-09", "mergeable": "MERGEABLE", "statusCheckRollup": passing_checks()},
             "runs": [{"databaseId": 10, "headSha": C, "event": "push", "status": "completed", "conclusion": "success"}],
             "tag": {"object": {"type": "commit", "sha": C}},
             "release": {"id": 20, "tag_name": "v1.2.4", "draft": False, "prerelease": False, "html_url": "https://example.invalid/v1.2.4",
                         "body": f"<!-- om-runtime-bundles:start -->Status: **ready** Commit: `{C}`<!-- om-runtime-bundles:end -->"},
             "assets": [{"name": name, "size": 100, "state": "uploaded"} for name in names]}
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "git" and argv[1] == "ls-remote":
            return B + "\trefs/heads/main"
        if argv[1:3] == ["pr", "view"]:
            return json.dumps(state["pr"])
        if argv[1:3] == ["run", "list"]:
            return json.dumps(state["runs"])
        if "/git/ref/" in argv[-1]:
            return json.dumps(state["tag"])
        if "/releases/tags/" in argv[-1]:
            return json.dumps(state["release"])
        if "/assets?" in argv[-1]:
            return json.dumps([state["assets"]])
        pytest.fail(str(argv))
    config = {"gh": "gh-fixture", "git": "git", "repository": "fixture/repo", "repository_url": "fixture-url"}
    def observe():
        return delivery.observe_release(tmp_path, config, tmp_path / "release", time.monotonic() + 60, command, task)
    return task, state, calls, observe


def test_release_is_complete_only_with_exact_sha_workflow_tag_assets_and_runtime_ready(release_observer):
    _, _, calls, observe = release_observer
    result = observe()
    assert result["status"] == "released" and result["version"] == "1.2.4" and result["merge_commit"] == C
    assert not any("create" in call or "merge" in call for call in calls)


@pytest.mark.parametrize("case", ["head", "tag", "draft", "runtime", "asset", "zero_asset", "wheel"])
def test_incomplete_or_unrelated_release_evidence_is_never_reported_published(release_observer, case):
    _, state, _, observe = release_observer
    if case == "head":
        state["pr"]["headRefOid"] = B
    elif case == "tag":
        state["tag"]["object"]["sha"] = B
    elif case == "draft":
        state["release"]["draft"] = True
    elif case == "runtime":
        state["release"]["body"] = state["release"]["body"].replace("ready", "building")
    elif case == "asset":
        state["assets"] = [row for row in state["assets"] if row["name"] != "VERSION"]
    elif case == "zero_asset":
        state["assets"][0]["size"] = 0
    else:
        state["assets"] = [row for row in state["assets"] if row["name"] != "plugin1.whl"]
    with pytest.raises(ValueError):
        observe()


@pytest.mark.parametrize("case", ["pending", "failed", "other_sha", "newer_failed_attempt"])
def test_failed_or_pending_release_workflow_stays_merged_unreleased(release_observer, case):
    _, state, _, observe = release_observer
    if case == "pending":
        state["runs"][0]["status"] = "in_progress"
    elif case == "failed":
        state["runs"][0]["conclusion"] = "failure"
    elif case == "other_sha":
        state["runs"][0]["headSha"] = B
    else:
        state["runs"].append({**state["runs"][0], "databaseId": 11, "conclusion": "failure"})
    assert observe()["status"] == "merged_unreleased"


@pytest.mark.parametrize("path", ["VERSION", "release/coverage/v1.2.4.json"])
def test_generic_pr_review_cannot_bypass_the_release_delta_gate(pr_repository, path):
    ctx = pr_repository
    ctx.change_head({path: "metadata\n"})
    result = ctx.run()["results"][0]
    assert result["status"] == "needs_decision" and "canonical full-delta" in result["reason"]
    assert not ctx.invocations


def test_pending_release_defers_other_loop_merges_without_skipping_review(commit_review_run, monkeypatch):
    root, _, _ = commit_review_run
    config = json.loads((root / "config.json").read_text())
    config.update({"timeout_seconds": 15000, "pull_requests_enabled": True, "comments_enabled": True,
                   "release_enabled": True, "pull_request_merge_enabled": True})
    review.write_json(root / "config.json", config)
    delivery.save_json(root / "releases/pending/task.json", {"pr_number": 12, "status": "awaiting_release_checks"})
    observed = []
    def agent(config, stage, prompt):
        sha = json.loads((stage / "plan.json").read_text())["commits"][0]["sha"]
        return {"outcome": "complete", "completed_commits": [sha], "report_markdown": "可追溯的完整审查结果和证据。" * 20,
                "verification_gaps": [], "confirmed_findings": []}
    def pull(root, config, run, deadline, invoke, *, command):
        observed.append((config["pull_request_merge_enabled"], config["_release_pr_numbers"]))
        return {"status": "complete", "reviewed": [2], "pending": [], "results": []}
    monkeypatch.setattr(review, "invoke_agent", agent)
    monkeypatch.setattr(prs, "process_pull_requests", pull)
    monkeypatch.setattr(delivery, "process_delivery", lambda *args, **kwargs: {"status": "complete"})
    receipt = review.run_once(root)
    assert observed == [(False, [12])]
    assert receipt["merge_deferred_for_release"] is True
    assert receipt["daily_summary"]["reviewed_pull_requests"] == 1
