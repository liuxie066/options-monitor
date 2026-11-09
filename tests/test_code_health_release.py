from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import pytest

from scripts import code_health_delivery as delivery
from scripts import code_health_repair as repair
from test_code_health_delivery import A, B, C, release_plan


@pytest.fixture
def release_preparation(tmp_path, monkeypatch):
    root, folder = tmp_path / "state", tmp_path / "release"
    root.mkdir()
    folder.mkdir()
    tree = folder / "worktree"
    config = {"git": "git", "gh": "gh-fixture", "repository": "fixture/repo", "verification_python": sys.executable,
              "verification_source_sha": A, "repository_url": "fixture-origin"}
    calls, agents = [], []
    failure = {"at": None}
    allowed = ["VERSION", "CHANGELOG.md", "release/coverage/v1.2.4.json"]
    def command(argv, *, cwd=None, timeout=None):
        calls.append(argv)
        assert timeout > 0
        if argv[0] == "git":
            if "worktree" in argv and "add" in argv:
                tree.mkdir()
                (tree / "VERSION").write_text("1.2.3\n")
                (tree / "CHANGELOG.md").write_text("# Changes\n\n## Unreleased\n\n## 1.2.3 - 2026-11-01\n\nOld history\n")
            if "rev-parse" in argv:
                return C if argv[-1] == "HEAD" else B
            if "log" in argv:
                return A + "\0first fix\n" + B + "\0second fix\n"
            return ""
        if "version_update" in argv:
            request = json.loads(argv[-1])
            if request["apply"]:
                assert "OM_AGENT_ENABLE_WRITE_TOOLS=true" in argv
                assert request["recommendation_digest"] == "sha256:preview"
                assert request["expected_base_version"] == "1.2.3" and request["expected_target_version"] == "1.2.4"
                (tree / "VERSION").write_text("1.2.4\n")
                return json.dumps({"ok": True, "data": {"status": "updated"}})
            return json.dumps({"ok": True, "data": {"status": "recommended", "recommendation_digest": "sha256:preview",
                "base": {"version": "1.2.3"}, "review_flags": ["COMPATIBILITY_SENSITIVE_PATH_CHANGED"] if failure["at"] == "compatibility" else [],
                "recommendation": {"bump": "patch", "target_version": "1.2.4"}}})
        if "scripts/release_delta.py" in argv:
            path = tree / allowed[-1]
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"reviewed_head": B, "commits": [{"sha": A}, {"sha": B}],
                                       "base": {"tag": "v1.2.3", "commit": C}}))
            return ""
        if "scripts/release_check.py" in argv:
            if failure["at"] == "coverage":
                raise RuntimeError("canonical delta coverage rejected a note/reference")
            return ""
        if "scripts/release_preflight.sh" in argv:
            if failure["at"] == "preflight":
                raise RuntimeError("actual full preflight returned nonzero")
            return ""
        if argv[0] == "gh-fixture" and argv[1:3] == ["pr", "create"]:
            assert any("--full" in call for call in calls)
            assert any("--require-clean" in call for call in calls)
            assert len(agents) == 2
            return "https://example.invalid/pr/9"
        if argv[0] == "gh-fixture" and argv[1:3] == ["pr", "list"]:
            return json.dumps([{"number": 9, "url": "https://example.invalid/pr/9", "headRefOid": C, "baseRefName": "main"}])
        pytest.fail(str(argv))
    def invoke(config, stage, prompt, *, result_schema):
        agents.append(stage.name)
        if result_schema == delivery.RELEASE_PLAN_SCHEMA:
            assert "全部提交的完整 diff" in prompt and "不能只看主题或本次修复" in prompt
            plan = release_plan()
            if failure["at"] == "third_party":
                plan["third_party_service"] = True
            if failure["at"] == "missing_commit":
                plan["release_notes"][0]["commits"].pop()
            return plan
        assert result_schema == repair.AUDIT_SCHEMA
        return {"verdict": "changes_required" if failure["at"] == "review" else "approve",
                "findings": ["Incorrect release scope"] if failure["at"] == "review" else [],
                "third_party_service": False, "report_markdown": "Independent full delta review"}
    monkeypatch.setattr(repair, "dependency_inputs_match", lambda config, root, target: None)
    monkeypatch.setattr(repair, "changed_paths", lambda *args, **kwargs: allowed)
    def run():
        return delivery.prepare_release(root, config, folder, time.monotonic() + 1200, invoke, command,
                                        [{"key": "pr-1", "merge_commit": B}])
    return run, failure, calls, folder, tree


def test_release_preparation_reuses_canonical_metadata_preflight_and_pr_chain(release_preparation):
    run, _, calls, folder, tree = release_preparation
    result = run()
    assert result["status"] == "awaiting_release_checks" and result["version"] == "1.2.4" and result["head"] == C
    assert result["pr_number"] == 9 and result["target"] == B
    assert (tree / "VERSION").read_text().strip() == "1.2.4"
    assert "## 1.2.4" in (tree / "CHANGELOG.md").read_text()
    manifest = delivery.read_json(tree / "release/coverage/v1.2.4.json", {})
    assert manifest["reviewed_head"] == B and manifest["release_notes"][0]["commits"] == [A, B]
    assert manifest["design_evidence"] == release_plan()["design_evidence"]
    assert any("chore: release 1.2.4" in call for call in calls)
    assert not any(call[1:3] in (["release", "create"], ["pr", "merge"]) or "tag" in call for call in calls)
    assert delivery.read_json(folder / "task.json", {})["pr_number"] == 9


@pytest.mark.parametrize("failure", ["third_party", "missing_commit", "compatibility", "coverage", "review", "preflight"])
def test_release_blockers_preserve_unreleased_status_and_never_publish_a_pr(release_preparation, failure):
    run, state, calls, folder, _ = release_preparation
    state["at"] = failure
    result = run()
    assert result["status"] == "merged_unreleased" and result["reason"]
    assert not any(call[0] == "gh-fixture" for call in calls)
    assert not any("push" in call for call in calls)
    preserved = delivery.read_json(folder / "task.json", {})
    again = run()
    assert again["status"] == "merged_unreleased" and "preserved" in again["reason"]
    assert delivery.read_json(folder / "task.json", {}) == preserved


@pytest.mark.parametrize("released", [False, True])
def test_completion_reply_contains_actual_verification_and_only_confirmed_version(tmp_path, monkeypatch, released):
    replies = []
    source = {"key": "pr-5", "number": 5, "head": A, "merge_commit": B, "url": "https://example.invalid/pr/5",
              "sources": [{"kind": "issue", "number": 7}], "finding": {"failure_scenario": "add(2, 1) returned 1", "title": "Return 3"},
              "verification": {"command": ["python", "-m", "pytest", "tests/test_calc.py"], "exit_code": 0}}
    monkeypatch.setattr(delivery, "merged_sources", lambda *args: [source])
    monkeypatch.setattr(delivery, "comment", lambda *args, **kwargs: replies.append(kwargs) or {"status": "posted"})
    if released:
        delivery.save_json(tmp_path / "releases/done/task.json", {"status": "released", "version": "1.2.4",
            "release_url": "https://example.invalid/v1.2.4", "sources": [source]})
    else:
        def failed_release(*args):
            raise RuntimeError("release environment unavailable")
        monkeypatch.setattr(delivery, "prepare_release", failed_release)
    result = delivery.process_delivery(tmp_path, {}, tmp_path / "run", time.monotonic() + 30, None,
                                       command=lambda *a, **k: pytest.fail("unexpected external write"), receipt={})
    assert {(row["kind"], row["number"]) for row in replies} == {("pr", 5), ("issue", 7)}
    for row in replies:
        assert "add(2, 1) returned 1" in row["body"] and "tests/test_calc.py" in row["body"] and "退出码 0" in row["body"]
        assert ("已发布 1.2.4" in row["body"]) is released
        assert ("已合并未发版" in row["body"]) is not released
    if not released:
        assert result["errors"] and result["release"]["status"] == "merged_unreleased"


def test_release_changelog_matches_real_repository_and_canonical_parser():
    from src.application.release_notes import parse_unreleased_categories, parse_version_categories

    root = Path(__file__).resolve().parents[1]
    original = (root / "CHANGELOG.md").read_text()
    version_parts = (root / "VERSION").read_text().strip().split(".")
    target = ".".join([*version_parts[:2], str(int(version_parts[2]) + 1)])
    notes = release_plan()["release_notes"]
    preview = delivery.release_changelog(original, notes)
    assert parse_unreleased_categories(preview)["status"] == "ok"
    published = delivery.release_changelog(original, notes, target)
    parsed = parse_version_categories(published, target)
    assert parsed["status"] == "ok"
    assert parsed["evidence"]["fixed"] == [notes[0]["text"]]
    assert published.endswith(original[original.index("\n## ", original.index("## Unreleased")):])
