from copy import deepcopy
import json
from pathlib import Path
import subprocess
import time

import pytest

from scripts import code_health_delivery as delivery
from scripts import code_health_issues as issues
from scripts import code_health_repair as repair
from scripts import code_health_review as review


@pytest.fixture
def completion(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(seed), *args], text=True).strip()
    git("init", "-q", "-b", "main")
    (seed / "fix.txt").write_text("verified fix\n")
    git("add", "fix.txt")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fix")
    sha = git("rev-parse", "HEAD")
    root = tmp_path / "state"
    root.mkdir()
    subprocess.run(["git", "clone", "--bare", "-q", str(seed), str(root / "repository.git")], check=True)
    subprocess.run(["git", "--git-dir", str(root / "repository.git"), "remote", "set-url", "origin",
                    "https://github.com/owner/repo.git"], check=True)
    stage = root / "review/items/7"
    (stage / "work").mkdir(parents=True)
    log = stage / "work/verify.jsonl"
    log.write_text(json.dumps({"command": "pytest tests/test_original_trigger.py", "exit_code": 0,
                               "output": "original trigger and subsequent correction passed"}) + "\n")
    (stage / "agent.jsonl").write_text(json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "command": "pytest tests/test_original_trigger.py", "exit_code": 0,
        "status": "completed", "aggregated_output": "original trigger and subsequent correction passed"}}) + "\n")
    config = {"repository": "owner/repo", "gh": "gh-fixture", "git": "git",
              "mirror": str(root / "repository.git"), "release_enabled": False}
    issue = {"number": 7, "state": "open", "title": "Original problem", "assignees": [],
             "body": "Fix: https://github.com/owner/repo/pull/50", "html_url": "https://github.com/owner/repo/issues/7"}
    pr = {"number": 50, "title": "Fix original problem", "state": "closed", "body": "Related to #7",
          "merged_at": "2026-11-22T01:00:00Z", "merge_commit_sha": sha, "updated_at": "2026-11-22",
          "html_url": "https://github.com/owner/repo/pull/50", "head": {"sha": "b" * 40}}
    remote = {"issue": issue, "pr": pr, "comments": [], "timeline": [], "fail": None, "pr_state": "MERGED",
              "checks": [{"name": name, "status": "COMPLETED", "conclusion": "SUCCESS"}
                         for name in sorted(repair.REQUIRED_REPAIR_CHECKS)]}
    calls = []
    def command(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] == "git":
            if "fetch" in argv:
                return ""  # Existing local main contains the real fixture commit; never fetch the network.
            assert argv[3] in {"merge-base", "remote"}
            return subprocess.check_output(argv, text=True).strip()
        assert argv[0] == "gh-fixture"
        args = argv[1:]
        if args[0] == "api":
            endpoint = args[-1]
            if endpoint == "user":
                return json.dumps({"login": "owner"})
            if endpoint == "repos/owner/repo/issues?state=open&sort=created&direction=asc&per_page=100":
                return json.dumps([[remote["issue"]] if remote["issue"]["state"] == "open" else []])
            if endpoint == "repos/owner/repo/issues/7":
                return json.dumps(remote["issue"])
            if endpoint.endswith("/issues/7/comments?per_page=100"):
                return json.dumps([remote["comments"]])
            if endpoint.endswith("/issues/7/timeline?per_page=100"):
                return json.dumps([remote["timeline"]])
            if endpoint.endswith("/pulls?state=open&per_page=100"):
                return "[[]]"
            if endpoint == "repos/owner/repo/pulls/50":
                return json.dumps(remote["pr"])
        if args[:2] == ["pr", "view"]:
            return json.dumps({"number": 50, "url": pr["html_url"], "state": remote["pr_state"],
                               "headRefOid": pr["head"]["sha"], "baseRefName": "main",
                               "mergeCommit": {"oid": sha}, "mergedAt": pr["merged_at"],
                               "statusCheckRollup": remote["checks"]})
        if args[:2] == ["issue", "comment"]:
            assert args[2] == "7" and args[args.index("--repo") + 1] == "owner/repo"
            body = Path(args[args.index("--body-file") + 1]).read_text()
            if remote["fail"] == "comment_before":
                raise TimeoutError("comment not submitted")
            number = 100 + len(remote["comments"])
            remote["comments"].append({"id": number, "body": body, "user": {"login": "owner", "type": "User"},
                                       "html_url": f"https://github.com/owner/repo/issues/7#issuecomment-{number}"})
            if remote["fail"] == "comment_after":
                raise TimeoutError("comment response lost")
            if remote["fail"] == "new_discussion":
                remote["comments"].append({"id": 999, "body": "Original scope still fails", "user": {"login": "reporter"}})
            return "posted"
        if args[:2] == ["issue", "close"]:
            assert args == ["issue", "close", "7", "--repo", "owner/repo", "--reason", "completed"]
            if remote["fail"] == "close_before":
                raise TimeoutError("close not submitted")
            if remote["fail"] != "close_no_effect":
                remote["issue"].update(state="closed", state_reason="completed", closed_by={"login": "owner"},
                                       closed_at=repair.timestamp())
            if remote["fail"] == "close_after":
                raise TimeoutError("close response lost")
            return "closed"
        raise AssertionError(f"forbidden external call: {argv}")
    def decide(scope="full", outcome="already_fixed", acceptance_id=0, quote=""):
        snapshot = {"number": 7, "issue": deepcopy(issue), "comments": deepcopy(remote["comments"]),
                    "linked_pull_requests": [issues.pr_summary(pr)],
                    "own_comment_ids": issues.own_comment_ids(root, 7, remote["comments"])}
        snapshot["input_digest"] = issues.issue_input_digest(snapshot["issue"], snapshot["linked_pull_requests"], snapshot["comments"],
                                                            ignored_comment_ids=snapshot["own_comment_ids"])
        item = {"number": 7, "outcome": outcome, "finding": None, "comment_draft": "Verification complete.",
                "triage": {"decision": "worthwhile", "reason": "Project defect", "third_party_service": False},
                "closure_evidence": {"reason": "Original trigger and corrections verified", "fix_commit": sha,
                    "verification_log": "verify.jsonl", "commands": [{"command": "pytest tests/test_original_trigger.py", "exit_code": 0}],
                    "resolution_scope": scope, "acceptance_comment_id": acceptance_id, "acceptance_quote": quote}}
        return issues.persist_decision(root, config, stage, {"target": sha, "open_pull_requests": []},
                                       snapshot, item, command, time.monotonic() + 30)[0]
    def run(intake=False):
        receipt = {}
        if intake:
            plan = issues.plan_issues(root, config, sha, command, full_scan=True)
            receipt["issues"] = issues.process_issues(root, config, root / "retry-issues", seed, plan,
                time.monotonic() + 30, lambda *a, **k: pytest.fail("own reply must not cause re-review"),
                review.FINDING_SCHEMA, review.validate_finding, command=command)
        result = delivery.process_delivery(root, config, root / "delivery", time.monotonic() + 30,
                                           lambda *a, **k: pytest.fail("no model or release work"), command=command, receipt=receipt)
        review.save_daily_summary(root / "delivery", {"delivery": result})
        return result
    return root, config, remote, calls, decide, run, log


def test_completed_external_repair_is_explained_closed_and_reported_once(completion):
    root, _, remote, calls, decide, run, _ = completion
    decide()
    result = run()
    assert result["closed"] == [7] and result["errors"] == []
    assert remote["issue"]["state"] == "closed"
    posted = next(i for i, call in enumerate(calls) if call[1:3] == ["issue", "comment"])
    closed = next(i for i, call in enumerate(calls) if call[1:3] == ["issue", "close"])
    assert posted < closed and any("--is-ancestor" in call for call in calls[:posted])
    assert issues.read_state(root)["issues"]["7"]["completion_receipt"]["comment_url"]
    summary = json.loads((root / "delivery/daily-summary.json").read_text())
    assert summary["closed"] == [7] and summary["issue_completions"][0]["status"] == "closed"
    assert "Issue #7 收尾：closed" in (root / "delivery/report.md").read_text()
    assert run()["closed"] == []
    assert sum(call[1:3] == ["issue", "close"] for call in calls) == 1


@pytest.mark.parametrize("failure", ["unmerged", "ci", "partial", "not_reproduced", "log",
                                     "other_repo", "discussion", "other_issue", "owned", "disabled_comments"])
def test_incomplete_or_changed_resolution_never_closes(completion, failure):
    _, config, remote, calls, decide, run, log = completion
    decide(scope="partial" if failure == "partial" else "full",
           outcome="not_reproduced" if failure == "not_reproduced" else "already_fixed")
    if failure == "unmerged": remote["pr_state"] = "OPEN"
    if failure == "ci": remote["checks"][0]["conclusion"] = "FAILURE"
    if failure == "log": log.write_text("failed or changed evidence\n")
    if failure == "other_repo": config["repository"] = "someone/else"
    if failure == "discussion": remote["issue"]["body"] += " Unresolved second requirement"
    if failure == "other_issue": remote["issue"]["number"] = 8
    if failure == "owned": remote["issue"]["assignees"] = [{"login": "other"}]
    if failure == "disabled_comments": config["comments_enabled"] = False
    assert run()["closed"] == [] and remote["issue"]["state"] == "open"
    assert not any(call[1:3] == ["issue", "close"] for call in calls)


@pytest.mark.parametrize("accepted", [False, True])
def test_limited_scope_requires_owner_acceptance(completion, accepted):
    _, _, remote, calls, decide, run, _ = completion
    remote["comments"].append({"id": 10, "body": "Accept closing this issue at the bounded scope.",
                               "user": {"login": "owner" if accepted else "contributor", "type": "User"}})
    decide(scope="accepted_scope", acceptance_id=10, quote="Accept closing this issue at the bounded scope.")
    assert run()["closed"] == ([7] if accepted else [])
    assert sum(call[1:3] == ["issue", "close"] for call in calls) == int(accepted)


@pytest.mark.parametrize("failure", ["comment_before", "comment_after", "close_before", "close_after", "close_no_effect"])
def test_unknown_external_result_reads_back_before_retry(completion, failure):
    root, _, remote, calls, decide, run, _ = completion
    decide()
    remote["fail"] = failure
    assert run()["closed"] == []
    assert issues.read_state(root)["issues"]["7"]["completion_receipt"]["status"] == "blocked"
    if failure.startswith("comment"):
        assert not any(call[1:3] == ["issue", "close"] for call in calls)
    remote["fail"] = None
    before = len(calls)
    assert run(intake=failure == "comment_after")["closed"] == [7]
    retry = calls[before:]
    assert any(call[-1] == "repos/owner/repo/issues/7" for call in retry)
    assert sum("已完成修复收尾" in row["body"] for row in remote["comments"]) == 1
    if failure == "close_after":
        assert not any(call[1:3] == ["issue", "close"] for call in retry)


def test_existing_closed_issue_is_reused_without_comment_or_close(completion):
    _, _, remote, calls, decide, run, _ = completion
    decide()
    remote["issue"]["state"] = "closed"
    result = run()
    assert result["closed"] == [] and result["issue_completions"][0]["status"] == "closed"
    assert not any(call[1:3] in (["issue", "comment"], ["issue", "close"]) for call in calls)


def test_legacy_advice_requires_fresh_issue_scope_review(tmp_path):
    row = {"outcome": "already_fixed", "target": "a" * 40, "input_digest": "old",
           "reviewed_at": "2026-11-20", "triage": {"decision": "worthwhile"}, "close_recommendation": {}}
    assert issues.completed_review(row, row["target"], "old") is None


@pytest.mark.parametrize("failure", ["missing_output", "no_transcript", "failed_event", "unmatched_event", "new_discussion"])
def test_closure_requires_execution_events_and_final_scope_readback(completion, failure):
    _, _, remote, calls, decide, run, log = completion
    if failure == "missing_output":
        row = json.loads(log.read_text())
        row.pop("output")
        log.write_text(json.dumps(row) + "\n")
    decide()
    transcript = log.parent.parent / "agent.jsonl"
    if failure == "no_transcript": transcript.unlink()
    if failure in {"failed_event", "unmatched_event"}:
        event = json.loads(transcript.read_text())
        event["item"].update({"exit_code": 1} if failure == "failed_event" else {"command": "unrelated test"})
        transcript.write_text(json.dumps(event) + "\n")
    if failure == "new_discussion": remote["fail"] = failure
    assert run()["closed"] == []
    assert not any(call[1:3] == ["issue", "close"] for call in calls)


def test_partial_scope_is_not_a_closure_recommendation(completion):
    _, _, _, _, decide, _, _ = completion
    row = decide(scope="partial")
    assert row["disposition"] == "needs_decision" and "close_recommendation" not in row


def test_reopened_issue_never_reuses_old_closure_after_cached_intake(completion):
    root, _, remote, calls, decide, run, _ = completion
    decide()
    assert run()["closed"] == [7]
    remote["issue"]["state"] = "open"
    for _ in range(2):
        assert run(intake=True)["closed"] == []
        saved = issues.read_state(root)["issues"]["7"]["completion_receipt"]
        assert saved["observed_closed_at"] and "reopened" in saved["reason"]
    assert sum(call[1:3] == ["issue", "close"] for call in calls) == 1


@pytest.mark.parametrize("reason,actor", [("not_planned", "owner"), ("completed", "someone_else")])
def test_external_close_is_observed_not_attributed_to_failed_request(completion, reason, actor):
    _, _, remote, calls, decide, run, _ = completion
    decide()
    remote["fail"] = "close_before"
    run()
    remote["issue"].update(state="closed", state_reason=reason, closed_by={"login": actor}, closed_at=repair.timestamp())
    remote["fail"] = None
    result = run()
    assert result["closed"] == []
    assert result["issue_completions"][0]["closure_origin"] == "external_or_unknown"
    assert sum(call[1:3] == ["issue", "close"] for call in calls) == 1


@pytest.mark.parametrize("changed", ["deleted", "edited"])
def test_confirmed_explanation_must_still_exist_before_retry_close(completion, changed):
    _, _, remote, calls, decide, run, _ = completion
    decide()
    remote["fail"] = "close_before"
    run()
    if changed == "deleted": remote["comments"].clear()
    else: remote["comments"][0]["body"] = "Unrelated content"
    remote["fail"] = None
    assert run()["closed"] == []
    assert sum(call[1:3] == ["issue", "close"] for call in calls) == 1


def test_reopened_after_lost_close_response_keeps_issue_open(completion):
    _, _, remote, calls, decide, run, _ = completion
    decide()
    remote['fail'] = 'close_after'
    run()
    assert remote['issue']['state'] == 'closed'
    remote['issue']['state'] = 'open'
    remote['timeline'].append({'event': 'reopened', 'created_at': repair.timestamp()})
    remote['fail'] = None
    for _ in range(2):
        result = run(intake=True)
        assert result['closed'] == [] and remote['issue']['state'] == 'open'
        assert 'reopened' in result['issue_completions'][0]['reason']
    assert sum(call[1:3] == ['issue', 'close'] for call in calls) == 1


def test_fresh_scope_review_can_complete_after_old_terminal_receipt(completion):
    _, _, remote, _, decide, run, _ = completion
    decide()
    assert run()['closed'] == [7]
    remote['issue']['state'] = 'open'
    remote['issue']['body'] += ' Additional scope independently verified.'
    decide()
    assert run()['closed'] == [7]
