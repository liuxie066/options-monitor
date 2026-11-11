from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import time

import pytest

from scripts import code_entropy_review as review
from scripts import code_entropy_scan as scan

GATES = ("zero_references", "no_runtime_hits", "not_public_api", "not_entry_point")
PROXIES = ("single_implementation", "single_switch_value", "single_caller_wrapper", "single_use_parameter")


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_dead_code_requires_all_four_proven_conditions():
    assert scan.dead_code(dict.fromkeys(GATES, True))
    assert not scan.dead_code({})


@pytest.mark.parametrize("gate", GATES)
@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_dead_code_rejects_each_false_unknown_or_nonboolean_gate(gate, value):
    facts = dict.fromkeys(GATES, True)
    facts[gate] = value
    assert not scan.dead_code(facts)


@pytest.mark.parametrize("gate", GATES)
def test_dead_code_rejects_each_missing_gate(gate):
    facts = dict.fromkeys(GATES, True)
    del facts[gate]
    assert not scan.dead_code(facts)


@pytest.mark.parametrize("proxy", PROXIES)
def test_each_overdesign_proxy_is_sufficient_for_a_candidate(proxy):
    assert scan.overdesign({proxy: True})
    assert not scan.overdesign({proxy: 1})


def test_overdesign_without_a_proxy_is_not_reportable():
    assert not scan.overdesign({})
    assert not scan.overdesign(dict.fromkeys(PROXIES, False))


@pytest.mark.parametrize(("source", "rule"), [
    ("from abc import ABC\nclass Port(ABC):\n    pass\nclass One(Port):\n    pass\n", "overdesign-abstraction"),
    ("SWITCH = True\nif SWITCH:\n    value = 1\n", "overdesign-switch"),
    ("def _forward(value):\n    return owner(value)\n_forward(4)\n", "overdesign-wrapper"),
    ("def _configure(mode):\n    current = normalize(mode)\n    return current\n_configure('one')\n", "overdesign-parameter"),
])
def test_real_ast_overdesign_proxies_remain_low_confidence(tmp_path, source, rule):
    put(tmp_path, "src/example.py", source)
    _, rows = scan.scan(tmp_path, ["src/example.py"])
    matches = [row for row in rows if row["rule"] == rule]
    assert matches and all(scan.overdesign(row["facts"]) for row in matches)
    assert not any(row["high_confidence"] for row in matches)


def test_real_ast_without_overdesign_proxy_emits_none(tmp_path):
    put(tmp_path, "src/example.py", "def calculate(a, b):\n    return a + b\ncalculate(1, 2)\ncalculate(3, 4)\n")
    _, rows = scan.scan(tmp_path, ["src/example.py"])
    assert not [row for row in rows if row["rule"].startswith("overdesign")]


def test_stable_key_and_dedup_ignore_line_offsets(tmp_path):
    first = scan.candidate("dead-code", "src/a.py", "_unused", 1, "same evidence")
    shifted = scan.candidate("dead-code", "src/a.py", "_unused", 90, "same evidence")
    assert first["key"] == shifted["key"]
    assert first["evidence_digest"] == shifted["evidence_digest"]
    assert scan.stable_key("dead-code", "src/b.py", "_unused") != first["key"]
    path = put(tmp_path, "src/a.py", "if False:\n    value = 1\n")
    metrics, rows = scan.scan(tmp_path, ["src/a.py", "src/a.py"])
    path.write_text("\n\n" + path.read_text())
    _, later = scan.scan(tmp_path, ["src/a.py"])
    assert metrics["files"] == 1
    assert len({row["key"] for row in rows}) == len(rows)
    assert {row["key"] for row in later} == {row["key"] for row in rows}


def test_function_body_coverage_excludes_definition_line(tmp_path):
    put(tmp_path, "src/a.py", "def _lonely():\n    return 1\n")
    coverage = {"src/a.py": {"executed_lines": [1], "missing_lines": [2], "missing_branches": []}}
    metrics, rows = scan.scan(tmp_path, ["src/a.py"], coverage)
    row = next(row for row in rows if row["rule"] == "dead-code")
    assert row["high_confidence"] and row["facts"]["no_runtime_hits"] is True
    assert metrics["dead_code_symbols"] == 1
    coverage["src/a.py"]["executed_lines"] = [1, 2]
    metrics, rows = scan.scan(tmp_path, ["src/a.py"], coverage)
    assert metrics["dead_code_symbols"] == 0
    assert not any(row["high_confidence"] for row in rows)


@pytest.mark.parametrize("selected", [[], ["src/a.py"]])
def test_missing_coverage_is_unknown_including_empty_scope(tmp_path, selected):
    put(tmp_path, "src/a.py", "def _lonely():\n    return 1\n")
    metrics, _ = scan.scan(tmp_path, selected)
    assert metrics["dead_code_symbols"] is None
    assert metrics["untested_branches"] is None
    assert metrics["unused_config_items"] is None


def test_incremental_scope_uses_untouched_references_but_emits_only_changed_files(tmp_path):
    put(tmp_path, "src/changed.py", "def _referenced():\n    return 1\nif False:\n    value = 1\n")
    put(tmp_path, "src/steady.py", "from .changed import _referenced\nif False:\n    old = 2\n")
    put(tmp_path, "tests/consumer.py", "from src.changed import _referenced\n")
    metrics, rows = scan.scan(tmp_path, ["src/changed.py", "tests/consumer.py"])
    assert metrics["files"] == 1 and metrics["unreachable_branches"] == 1
    assert rows and {row["path"] for row in rows} == {"src/changed.py"}
    assert not [row for row in rows if row["rule"] == "dead-code"]


@pytest.mark.parametrize("ledger_status", ["published", "pending"])
def test_permanent_mute_precedes_acceptance_and_issue_dedup(tmp_path, ledger_status):
    state = tmp_path / "code-entropy-review"
    state.mkdir()
    row = scan.candidate("unreachable-branch", "src/a.py", "branch", 1, "if False", high=True)
    run = state / "runs" / "run-1"
    run.mkdir(parents=True)
    review.write_json(run / "candidates.json", [row])
    review.record_decision(state, "accept", row["key"], "verified", "run-1")
    assert review.eligible(row, review.read_json(state / "decisions.json"))
    review.record_decision(state, "mute", row["key"], "known and deliberately retained")
    review.record_decision(state, "accept", row["key"], "acceptance does not undo mute", "run-1")
    review.write_json(state / "issues.json", {row["key"]: {"status": ledger_status}})
    calls = []

    def forbidden(argv):
        calls.append(argv)
        raise AssertionError("muted findings must not contact GitHub")

    assert review.deliver([row], state, {"repository": "fixture/repo"}, "a" * 40, forbidden) == []
    assert not calls
    review.record_decision(state, "unmute", row["key"], "explicit user reconsideration")
    decisions = review.read_json(state / "decisions.json")
    assert not decisions[row["key"]]["muted"]
    assert not review.eligible(row, decisions)  # Unmuting is not a new acceptance.


def test_acceptance_requires_exact_evidence_and_high_confidence():
    row = scan.candidate("complexity", "src/a.py", "f", 1, "11", high=True)
    decisions = {row["key"]: {"accepted_evidence": row["evidence_digest"], "reason": "verified"}}
    assert review.eligible(row, decisions)
    assert not review.eligible({**row, "high_confidence": False}, decisions)
    changed = {**row, "evidence_digest": "different evidence"}
    assert not review.eligible(changed, decisions)


def test_changed_file_discovery_uses_git_range_and_nul_paths():
    calls = []

    def execute(argv):
        calls.append(argv)
        return {"exit_code": 0, "text": "" if "merge-base" in argv else "src/changed.py\0docs/note.md\0"}

    assert review.changed_files(["git", "-C", "mirror"], "old", "new", execute) == ["src/changed.py", "docs/note.md"]
    assert calls[0][-4:] == ["merge-base", "--is-ancestor", "old", "new"]
    assert "--no-renames" in calls[1] and "--diff-filter=AM" in calls[1] and "-z" in calls[1]
    with pytest.raises(ValueError, match="ancestor"):
        review.changed_files(["git"], "old", "new", lambda argv: {"exit_code": 1, "text": ""})


def confirmed_issue(state):
    row = scan.candidate("unreachable-branch", "src/a.py", "branch", 1, "if False", high=True)
    review.write_json(state / "decisions.json", {row["key"]: {
        "accepted_evidence": row["evidence_digest"], "reason": "confirmed static evidence"}})
    return row


def test_closed_remote_issue_deduplicates_stable_key(tmp_path):
    row = confirmed_issue(tmp_path)
    calls = []

    def execute(argv):
        calls.append(argv)
        assert argv[:2] == ["gh", "api"] and "--slurp" in argv
        return {"exit_code": 0, "text": json.dumps([[{
            "body": f"<!-- code-entropy:{row['key']} -->", "state": "closed", "html_url": "https://github.com/fixture/repo/issues/3"}]])}

    for _ in range(2):
        assert review.deliver([row], tmp_path, {"repository": "fixture/repo"}, "a" * 40, execute) == []
    assert len(calls) == 2
    assert review.read_json(tmp_path / "issues.json")[row["key"]]["status"] == "published"


@pytest.mark.parametrize("ambiguous", ["exit-error", "timeout"])
def test_ambiguous_creation_is_pending_and_never_blindly_retried(tmp_path, ambiguous):
    row = confirmed_issue(tmp_path)
    calls = []
    remote_exists = False

    def execute(argv):
        calls.append(argv)
        if argv[:3] == ["gh", "issue", "create"]:
            if ambiguous == "timeout":
                raise subprocess.TimeoutExpired("gh", 1)
            return {"exit_code": 1, "text": ""}
        if "--slurp" in argv:
            issues = [{"body": f"<!-- code-entropy:{row['key']} -->",
                       "html_url": "https://github.com/fixture/repo/issues/3"}] if remote_exists else []
            return {"exit_code": 0, "text": json.dumps([issues])}
        return {"exit_code": 0, "text": "{}"}  # Existing dedicated label.

    if ambiguous == "timeout":
        with pytest.raises(subprocess.TimeoutExpired):
            review.deliver([row], tmp_path, {"repository": "fixture/repo"}, "a" * 40, execute)
    else:
        with pytest.raises(RuntimeError, match="pending"):
            review.deliver([row], tmp_path, {"repository": "fixture/repo"}, "a" * 40, execute)
    assert review.read_json(tmp_path / "issues.json")[row["key"]]["status"] == "pending"
    with pytest.raises(RuntimeError, match="pending"):
        review.deliver([], tmp_path, {"repository": "fixture/repo"}, "a" * 40, execute)
    assert sum(argv[:3] == ["gh", "issue", "create"] for argv in calls) == 1
    remote_exists = True
    review.deliver([row], tmp_path, {"repository": "fixture/repo"}, "a" * 40, execute)
    assert review.read_json(tmp_path / "issues.json")[row["key"]]["status"] == "published"
    assert sum(argv[:3] == ["gh", "issue", "create"] for argv in calls) == 1


def git(repository, *arguments):
    result = subprocess.run(["git", "-C", str(repository), "-c", "user.name=Fixture",
        "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", *arguments],
        check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_real_git_daily_scope_weekly_cursor_isolation_and_failed_retry(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    put(origin, "src/steady.py", "if False:\n    old = 1\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture initial")
    first = git(origin, "rev-parse", "HEAD")
    state = tmp_path / "code-entropy-review"
    state.mkdir()
    baseline = put(tmp_path, "policy.md", "fixture baseline version 1")
    review.initialize(state, "fixture/repo", str(origin), baseline)
    monkeypatch.setattr(review, "tool_runs", lambda source, selected, execute: ({"builtin": {"available": True}}, [], {}))
    with review.run_lock(state):
        initial = review.run_scan(state, "daily", timeout_seconds=30)
    assert initial["status"] == "complete" and initial["initialized_daily_cursor"]
    assert initial["selected_files"] == [] and initial["budget_seconds"] == 30
    assert review.read_json(state / "daily-cursor.json")["sha"] == first
    put(origin, "src/new.py", "if False:\n    new = 2\n")
    put(origin, "docs/new.md", "not production Python")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture add production and docs")
    second = git(origin, "rev-parse", "HEAD")
    with review.run_lock(state):
        daily = review.run_scan(state, "daily", timeout_seconds=30)
    assert daily["status"] == "complete", daily["gaps"]
    assert daily["selected_files"] == ["src/new.py"]
    assert daily["metrics"]["files"] == 1 and daily["metrics"]["unreachable_branches"] == 1
    rows = review.read_json(state / "runs" / daily["run_id"] / "candidates.json")
    assert rows and {row["path"] for row in rows} == {"src/new.py"}
    cursor = (state / "daily-cursor.json").read_bytes()
    with review.run_lock(state):
        weekly = review.run_scan(state, "weekly", timeout_seconds=30)
    assert weekly["status"] == "complete" and weekly["metrics"]["files"] == 2
    assert weekly["main_sha"] == second and (state / "daily-cursor.json").read_bytes() == cursor
    put(origin, "src/new.py", "if False:\n    new = 2\n# unrelated edit\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture change without a new smell")
    with review.run_lock(state):
        unchanged = review.run_scan(state, "daily", timeout_seconds=30)
    assert unchanged["status"] == "complete" and unchanged["candidates"] == 0
    assert unchanged["metrics"]["unreachable_branches"] == 1
    cursor = (state / "daily-cursor.json").read_bytes()
    put(origin, "src/broken.py", "def broken(:\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture invalid syntax")
    with review.run_lock(state):
        failed = review.run_scan(state, "daily", timeout_seconds=30)
    assert failed["status"] == "failed" and failed["metrics"] is None
    assert (state / "daily-cursor.json").read_bytes() == cursor
    assert len((state / "scorecards.jsonl").read_text().splitlines()) == 5
    assert not list((state / "runs").glob("*/source-*"))


def test_coverage_manifest_binds_source_workload_and_file_digest(tmp_path):
    evidence = put(tmp_path, "coverage.json", json.dumps({"meta": {"branch_coverage": True}, "files": {
        "src/a.py": {"executed_lines": [1], "missing_lines": [2], "missing_branches": []}}}))
    manifest = {"main_sha": "a" * 40, "exit_code": 0, "command": ["python", "-m", "pytest"],
                "coverage_json": str(evidence), "coverage_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}
    path = put(tmp_path, "manifest.json", json.dumps(manifest))
    assert "src/a.py" in review.coverage_evidence(path, "a" * 40)
    with pytest.raises(ValueError, match="SHA"):
        review.coverage_evidence(path, "b" * 40)
    for change in ({"exit_code": 1}, {"command": []}, {"coverage_sha256": "wrong"}):
        path.write_text(json.dumps({**manifest, **change}))
        with pytest.raises(ValueError):
            review.coverage_evidence(path, "a" * 40)


def test_uncovered_branch_is_not_reported_as_unreachable(tmp_path):
    put(tmp_path, "src/a.py", "def run(flag):\n    if flag:\n        return 1\n    return 2\n")
    coverage = {"src/a.py": {"executed_lines": [1, 2], "missing_lines": [3, 4], "missing_branches": [[2, 3]]}}
    metrics, rows = scan.scan(tmp_path, ["src/a.py"], coverage)
    assert metrics["untested_branches"] == 1 and metrics["unreachable_branches"] == 0
    assert not [row for row in rows if row["rule"] == "unreachable-branch"]


def test_nested_function_complexity_is_independent():
    tree = ast.parse("def outer():\n    def inner(a, b):\n        if a and b:\n            return 1\n    return inner\n")
    functions = dict(scan.functions(tree))
    assert scan.complexity(functions["outer"]) == 1
    assert scan.complexity(functions["outer.inner"]) == 3


def test_duplicate_ratio_uses_only_selected_tokens_and_unions_overlap(tmp_path):
    source = "\n".join(f"var_{index} = {index}" for index in range(30)) + "\n"
    for name in ("a", "b", "c"):
        put(tmp_path, f"src/{name}.py", source)
    metrics, rows = scan.scan(tmp_path, ["src/b.py"])
    assert metrics["total_tokens"] == len(scan.tokens(source, ast.parse(source)))
    assert 0 < metrics["duplicate_tokens"] <= metrics["total_tokens"]
    assert 0 < metrics["duplicate_ratio"] <= 1
    assert rows and {row["path"] for row in rows} == {"src/b.py"}


@pytest.mark.parametrize("suffix", ["code-health-review", "other-state", "code-health-review/code-entropy-review"])
def test_state_boundary_rejects_other_scheduler_paths(tmp_path, suffix):
    with pytest.raises(ValueError, match="independent"):
        review.state_path(tmp_path / suffix)


def test_plist_render_is_preview_only_and_has_independent_budgets(tmp_path):
    state = tmp_path / "code-entropy-review"
    state.mkdir()
    paths = review.render_plists(state)
    labels = set()
    for path in paths:
        document = plistlib.loads(Path(path).read_bytes())
        mode = "daily" if ".daily." in path else "weekly"
        labels.add(document["Label"])
        assert document["RunAtLoad"] is False and document["ProcessType"] == "Background"
        assert document["LowPriorityIO"] is True and document["Nice"] == 15
        assert "KeepAlive" not in document
        argv = document["ProgramArguments"]
        assert int(argv[argv.index("--timeout-seconds") + 1]) == review.BUDGETS[mode]
        assert document["WorkingDirectory"] == str(state)
        assert "code-health" not in Path(path).read_text()
    assert len(labels) == 2
    assert labels == {"com.options-monitor.code-entropy.daily", "com.options-monitor.code-entropy.weekly"}


def test_subprocess_timeout_terminates_its_own_process_group(tmp_path):
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        review.command([sys.executable, "-I", "-c", "import time; time.sleep(30)"], tmp_path, tmp_path / "log", 0.05)
    assert time.monotonic() - started < 5


def test_successful_issue_requires_readback_receipt(tmp_path):
    row = confirmed_issue(tmp_path)
    calls = []
    url = "https://github.com/fixture/repo/issues/5"

    def execute(argv):
        calls.append(argv)
        if "--slurp" in argv:
            return {"exit_code": 0, "text": "[[]]"}
        if argv[:3] == ["gh", "issue", "create"]:
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            assert row["raw"] in body and "src/a.py:1" in body and row["tool"] in body
            assert "--label" in argv and review.LABEL in argv
            return {"exit_code": 0, "text": url + "\n"}
        if argv[-1].endswith("issues/5"):
            return {"exit_code": 0, "text": json.dumps({"body": f"<!-- code-entropy:{row['key']} -->",
                                                       "labels": [{"name": review.LABEL}]})}
        return {"exit_code": 0, "text": "{}"}

    assert review.deliver([row], tmp_path, {"repository": "fixture/repo"}, "a" * 40, execute) == [url]
    assert calls[-1][-1].endswith("issues/5")
    assert review.read_json(tmp_path / "issues.json")[row["key"]]["status"] == "published"


def test_missing_optional_tools_are_explicit_and_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "tool_command", lambda name: None)
    calls = []

    def forbidden(argv):
        calls.append(argv)
        raise AssertionError("no package installation or external command is permitted")

    tools, rows, evidence = review.tool_runs(tmp_path, [], forbidden)
    assert not rows and not calls and evidence == {"vulture": None, "radon": None}
    assert all(not tools[name]["available"] for name in ("ruff", "vulture", "coverage", "radon", "jscpd"))
    assert tools["builtin"]["available"]


def test_real_cli_can_scan_main_without_launchd(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    put(origin, "src/example.py", "if False:\n    unused = 1\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture main")
    sha = git(origin, "rev-parse", "HEAD")
    state = tmp_path / "code-entropy-review"
    script = Path(review.__file__).resolve()
    cli = [sys.executable, str(script), "--state-dir", str(state)]
    init = subprocess.run([*cli, "init", "--repository", "fixture/repo", "--remote", str(origin)],
                          check=True, capture_output=True, text=True)
    assert json.loads(init.stdout)["status"] == "initialized"
    run = subprocess.run([*cli, "run", "--mode", "weekly", "--timeout-seconds", "30"],
                         check=True, capture_output=True, text=True)
    result = json.loads(run.stdout)
    assert result["status"] == "complete" and result["main_sha"] == sha
    assert result["budget_seconds"] == 30 and result["metrics"]["files"] == 1
    assert result["metrics"]["unreachable_branches"] == 1
    assert result["metrics"]["dead_code_symbols"] is None
    assert Path(result["report"]).is_file() and not (state / "daily-cursor.json").exists()
    assert not (state / "issues.json").exists()
    assert review.BUDGETS == {"daily": 900, "weekly": 5400}


def test_own_run_lock_excludes_overlapping_daily_and_weekly(tmp_path):
    with review.run_lock(tmp_path):
        with pytest.raises(BlockingIOError):
            with review.run_lock(tmp_path):
                pytest.fail("overlapping scan acquired the same lock")


def test_whole_run_deadline_also_bounds_in_process_analysis():
    with pytest.raises(TimeoutError, match="budget"):
        with review.deadline(0.02):
            time.sleep(1)


def test_cross_module_dynamic_lookup_prevents_dead_code_confirmation(tmp_path):
    put(tmp_path, "src/target.py", "def _lonely():\n    return 1\n")
    put(tmp_path, "src/caller.py", "import target\ngetattr(target, '_' + 'lonely')()\n")
    coverage = {"src/target.py": {"executed_lines": [1], "missing_lines": [2], "missing_branches": []}}
    metrics, rows = scan.scan(tmp_path, ["src/target.py"], coverage)
    row = next(row for row in rows if row["rule"] == "dead-code")
    assert row["facts"]["zero_references"] is True
    assert row["facts"]["not_entry_point"] is None and not row["high_confidence"]
    assert metrics["dead_code_symbols"] is None


def test_branch_key_includes_qualified_function_scope(tmp_path):
    put(tmp_path, "src/a.py", "def first():\n    if False:\n        value = 1\ndef second():\n    if False:\n        value = 1\n")
    metrics, rows = scan.scan(tmp_path, ["src/a.py"])
    branches = [row for row in rows if row["rule"] == "unreachable-branch"]
    assert metrics["unreachable_branches"] == 2 and len(branches) == 2
    assert len({row["key"] for row in branches}) == 2


def test_main_baseline_change_blocks_daily_cursor_and_requires_supported_version(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    put(origin, "src/a.py", "value = 1\n")
    policy = put(origin, "docs/CODE_ENTROPY_BASELINE.md", "版本：1；初始规则\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture versioned baseline")
    state = tmp_path / "code-entropy-review"
    state.mkdir()
    review.initialize(state, "fixture/repo", str(origin), policy)
    monkeypatch.setattr(review, "tool_runs", lambda source, selected, execute: ({}, [], {}))
    with review.run_lock(state):
        assert review.run_scan(state, "daily", timeout_seconds=30)["status"] == "complete"
    cursor = (state / "daily-cursor.json").read_bytes()
    policy.write_text("版本：1；补充规则理由\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture reviewed baseline change")
    with review.run_lock(state):
        daily = review.run_scan(state, "daily", timeout_seconds=30)
        weekly = review.run_scan(state, "weekly", timeout_seconds=30)
    assert daily["status"] == "failed" and any("baseline changed" in gap for gap in daily["gaps"])
    assert weekly["status"] == "complete" and weekly["baseline_source"].startswith("main:")
    assert weekly["baseline_digest"] == hashlib.sha256(policy.read_bytes()).hexdigest()
    assert (state / "daily-cursor.json").read_bytes() == cursor
    policy.write_text("版本：2；新算法\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture supported baseline version")
    with review.run_lock(state):
        supported = review.run_scan(state, "weekly", timeout_seconds=30)
    assert supported["status"] == "complete" and supported["baseline_version"] == 2
    policy.write_text("版本：3；未支持的更新算法\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture unsupported baseline version")
    with review.run_lock(state):
        blocked = review.run_scan(state, "weekly", timeout_seconds=30)
    assert blocked["status"] == "failed" and any("unsupported" in gap for gap in blocked["gaps"])
    assert (state / "daily-cursor.json").read_bytes() == cursor


def test_duplicate_keys_do_not_depend_on_daily_vs_weekly_owner(tmp_path):
    source = "\n".join(f"var_{index} = {index}" for index in range(30)) + "\n"
    put(tmp_path, "src/a.py", source)
    put(tmp_path, "src/b.py", source)
    _, weekly = scan.scan(tmp_path, ["src/a.py", "src/b.py"])
    _, daily = scan.scan(tmp_path, ["src/a.py"])
    assert {row["key"] for row in daily if row["rule"] == "duplicate"} == {
        row["key"] for row in weekly if row["rule"] == "duplicate"}


def test_dense_identical_code_is_detected_as_duplicate(tmp_path):
    source = "\n".join(f"var_{index} = {index} + {index} + {index}" for index in range(30)) + "\n"
    put(tmp_path, "src/a.py", source)
    put(tmp_path, "src/b.py", source)
    metrics, rows = scan.scan(tmp_path, ["src/b.py"])
    assert metrics["duplicate_ratio"] == 1.0
    assert metrics["duplicate_tokens"] == metrics["total_tokens"] == 210
    assert any(row["rule"] == "duplicate" for row in rows)


@pytest.mark.parametrize("failure", ["create-error", "bad-url", "readback-error", "readback-mismatch"])
def test_publication_failure_preserves_daily_cursor_and_reconciles_pending(tmp_path, monkeypatch, failure):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    put(origin, "src/a.py", "value = 1\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture initial")
    state = tmp_path / "code-entropy-review"
    state.mkdir()
    baseline = put(tmp_path, "policy.md", "fixture baseline")
    review.initialize(state, "fixture/repo", str(origin), baseline)
    monkeypatch.setattr(review, "tool_runs", lambda source, selected, execute: ({}, [], {}))
    with review.run_lock(state):
        assert review.run_scan(state, "daily", timeout_seconds=30)["status"] == "complete"
    cursor = (state / "daily-cursor.json").read_bytes()
    put(origin, "src/a.py", "if False:\n    value = 1\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture new finding")
    with review.run_lock(state):
        weekly = review.run_scan(state, "weekly", timeout_seconds=30)
    row = review.read_json(state / "runs" / weekly["run_id"] / "candidates.json")[0]
    review.record_decision(state, "accept", row["key"], "independent evidence checked", weekly["run_id"])
    actual_command = review.command
    calls = []
    visible = False
    marker = f"<!-- code-entropy:{row['key']} -->"

    def fake_command(argv, cwd, log, timeout):
        if argv[0] != "gh":
            return actual_command(argv, cwd, log, timeout)
        calls.append(argv)
        code, text = 0, "{}"
        if "--slurp" in argv:
            text = json.dumps([[{"body": marker, "html_url": "https://github.com/fixture/repo/issues/7"}] if visible else []])
        elif argv[:3] == ["gh", "issue", "create"]:
            code = 1 if failure == "create-error" else 0
            text = "invalid response" if failure == "bad-url" else "https://github.com/fixture/repo/issues/7"
        elif argv[-1].endswith("issues/7"):
            code = 1 if failure == "readback-error" else 0
            text = json.dumps({"body": "wrong marker" if failure == "readback-mismatch" else marker,
                               "labels": [{"name": review.LABEL}]})
        log.write_text(text)
        log.with_suffix(".stderr").write_text("")
        return {"command": argv, "exit_code": code, "output": log.name, "stderr": log.with_suffix(".stderr").name}

    monkeypatch.setattr(review, "command", fake_command)
    with review.run_lock(state):
        failed = review.run_scan(state, "daily", timeout_seconds=30, publish=True)
    assert failed["status"] == "failed"
    assert (state / "daily-cursor.json").read_bytes() == cursor
    with review.run_lock(state):
        pending = review.run_scan(state, "daily", timeout_seconds=30, publish=True)
    assert pending["status"] == "failed"
    assert sum(argv[:3] == ["gh", "issue", "create"] for argv in calls) == 1
    visible = True
    with review.run_lock(state):
        reconciled = review.run_scan(state, "daily", timeout_seconds=30, publish=True)
    assert reconciled["status"] == "complete"
    assert review.read_json(state / "issues.json")[row["key"]]["status"] == "published"
    assert review.read_json(state / "daily-cursor.json")["sha"] == git(origin, "rev-parse", "HEAD")
    assert sum(argv[:3] == ["gh", "issue", "create"] for argv in calls) == 1


def test_vulture_corroboration_vetoes_uncorroborated_dead_candidates(tmp_path):
    put(tmp_path, "src/a.py", "def _confirmed():\n    return 1\n\ndef _vetoed():\n    return 2\n")
    coverage = {"src/a.py": {"executed_lines": [1], "missing_lines": [2, 4, 5], "missing_branches": []}}
    metrics, rows = scan.scan(tmp_path, ["src/a.py"], coverage, vulture_unused={"src/a.py": {1}})
    dead = [row for row in rows if row["rule"] == "dead-code"]
    assert [row["identity"] for row in dead] == ["_confirmed"]
    assert dead[0]["high_confidence"] is True and dead[0]["facts"]["vulture_unused"] is True
    assert metrics["dead_code_candidates"] == 1
    assert metrics["dead_code_vulture_rejected"] == 1
    assert metrics["dead_code_basis"] == "vulture+entropy-ast-v1"
    assert metrics["dead_code_symbols"] == 1


def test_radon_complexity_is_authoritative_with_per_function_fallback(tmp_path):
    put(tmp_path, "src/a.py", "def run(flag):\n    if flag:\n        return 1\n    return 2\n")
    metrics, rows = scan.scan(tmp_path, ["src/a.py"], radon_complexity={"src/a.py": {1: 12}})
    assert metrics["complexity_gt10"] == 1 and metrics["complexity_basis"] == "radon"
    assert metrics["complexity_unmatched"] == 0
    row = next(row for row in rows if row["rule"] == "complexity")
    assert row["facts"]["complexity"] == 12 and row["tool"] == "radon"
    metrics, rows = scan.scan(tmp_path, ["src/a.py"], radon_complexity={"src/a.py": {}})
    assert metrics["complexity_unmatched"] == 1
    assert metrics["complexity_basis"] == "radon+entropy-ast-v1-fallback"
    assert metrics["complexity_gt10"] == 0


def test_tool_runs_parses_vulture_and_radon_evidence(tmp_path, monkeypatch):
    put(tmp_path, "src/a.py", "def _dead():\n    return 1\n")
    seen = []

    def execute(argv, cwd=None, **options):
        seen.append(argv)
        name = argv[0]
        if argv[-1] == "--version":
            return {"exit_code": 0, "text": f"{name} 9.9"}
        if "check" in argv:
            return {"exit_code": 0, "text": "[]"}
        if "--min-confidence" in argv:
            return {"exit_code": 3, "text": "src/a.py:1: unused function '_dead' (60% confidence)\n"}
        return {"exit_code": 0, "text": json.dumps(
            {"src/a.py": [{"type": "function", "name": "_dead", "lineno": 1, "complexity": 7, "closures": []}]})}

    monkeypatch.setattr(review, "tool_command", lambda name: [f"fake-{name}"])
    tools, findings, evidence = review.tool_runs(tmp_path, ["src/a.py"], execute)
    assert evidence["vulture"] == {"src/a.py": {1}}
    assert evidence["radon"] == {"src/a.py": {1: 7}}
    assert tools["vulture"]["unused_symbols"] == 1
    assert tools["radon"]["measured_functions"] == 1
    assert tools["jscpd"]["adopted"] is False
    assert tools["coverage"]["available"] is True
    assert not findings
    assert any("--min-confidence" in argv for argv in seen)


def test_self_coverage_builds_disposable_environment_and_removes_build_artifacts(tmp_path):
    source = tmp_path / "source"
    (source / "tests").mkdir(parents=True)
    put(source, "requirements/dev.txt", "-r runtime.txt\n")
    put(source, "constraints/dev.txt", "")
    put(source, "plugins/x/build/junk.py", "")
    put(source, "plugins/x/src/pkg/__init__.py", "")
    (source / "plugins/x/src/pkg.egg-info").mkdir()
    output = tmp_path / "run"
    output.mkdir()
    commands = []

    def execute(argv, cwd=None, **options):
        commands.append(argv)
        if "json" in argv:
            (output / "coverage-self.json").write_text(json.dumps({
                "meta": {"branch_coverage": True},
                "files": {"src/a.py": {"executed_lines": [1], "missing_lines": [], "missing_branches": []}}}))
        return {"exit_code": 0, "text": ""}

    entries, record = review.self_coverage(source, output, execute, scratch=tmp_path / "scratch",
                                           time_left=lambda: 1000)
    assert record["status"] == "complete" and entries["src/a.py"]["executed_lines"] == [1]
    assert not (source / "plugins/x/build").exists()
    assert not (source / "plugins/x/src/pkg.egg-info").exists()
    assert any("-m" in argv and "pip" in argv and "requirements/dev.txt" in argv for argv in commands)
    assert any("-m" in argv and "pytest" in argv for argv in commands)


def test_self_coverage_workload_failure_degrades_with_explicit_reason(tmp_path):
    source = tmp_path / "source"
    (source / "tests").mkdir(parents=True)
    (source / "src").mkdir()
    put(source, "requirements/dev.txt", "")
    put(source, "constraints/dev.txt", "")
    output = tmp_path / "run"
    output.mkdir()

    def execute(argv, cwd=None, **options):
        return {"exit_code": 1 if "pytest" in argv else 0, "text": ""}

    entries, record = review.self_coverage(source, output, execute, scratch=tmp_path / "scratch",
                                           time_left=lambda: 1000)
    assert entries is None and record["status"] == "failed"
    assert "exited 1" in record["reason"]


def test_self_coverage_skips_a_snapshot_without_its_own_workload(tmp_path):
    def forbidden(argv, cwd=None, **options):
        raise AssertionError("no workload should start without tests and requirements")

    entries, record = review.self_coverage(tmp_path, tmp_path, forbidden, scratch=tmp_path,
                                           time_left=lambda: 1000)
    assert entries is None and record["status"] == "skipped"
    assert "workload" in record["reason"]


def test_real_weekly_scan_uses_installed_vulture_and_radon(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    body = "def _unused():\n    return 1\n\n\ndef complex_fn(a):\n"
    body += "".join(f"    if a == {index}:\n        a += 1\n" for index in range(12))
    body += "    return a\n"
    put(origin, "src/example.py", body)
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture tools")
    state = tmp_path / "code-entropy-review"
    state.mkdir()
    status = git(origin, "status", "--porcelain")
    assert status == ""
    baseline = put(tmp_path, "policy.md", "fixture baseline version 1")
    review.initialize(state, "fixture/repo", str(origin), baseline)
    with review.run_lock(state):
        result = review.run_scan(state, "weekly", timeout_seconds=60)
    assert result["status"] == "complete"
    assert result["metrics"]["complexity_basis"] == "radon"
    assert result["metrics"]["complexity_gt10"] == 1
    assert result["tools"]["vulture"]["available"] is True
    assert result["tools"]["vulture"]["unused_symbols"] >= 1
    assert result["metrics"]["dead_code_candidates"] == 1
    assert not any(gap.startswith(("vulture is not installed", "radon is not installed"))
                   for gap in result["gaps"])
