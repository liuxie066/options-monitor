from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


CI = Path(__file__).resolve().parents[1] / ".github" / "ci"
SPEC = importlib.util.spec_from_file_location("regression_manifest", CI / "regression_manifest.py")
manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest)


def _receipts():
    full = sorted(f"tests/test_{i}.py::test_value[{j}]" for i in range(24) for j in range(2))
    return [dict(
        shard=i, count=4, revision="abc", collected=full,
        selected=[n for n in full if manifest.shard_for(n, 4) == i],
        completed=[n for n in full if manifest.shard_for(n, 4) == i],
        exitstatus=0, errors=[],
    ) for i in range(4)]


def test_union_covers_each_collected_case_once_and_preserves_files():
    receipts = _receipts()
    assert manifest.verify(receipts, count=4, revision="abc") == 48
    for receipt in receipts:
        files = [n.split("::")[0] for n in receipt["selected"]]
        assert all(files.count(f) == 2 for f in files)


@pytest.mark.parametrize("damage", [
    "missing_shard", "duplicate_shard", "missing_test", "duplicate_test", "wrong_revision",
    "different_collection", "worker_crash", "failed_test", "wrong_assignment", "empty_collection",
])
def test_union_rejects_incomplete_or_wrong_evidence(damage):
    receipts = copy.deepcopy(_receipts())
    first = receipts[0]
    if damage == "missing_shard":
        receipts.pop()
    elif damage == "duplicate_shard":
        receipts[-1] = first
    elif damage == "missing_test":
        first["completed"].pop()
    elif damage == "duplicate_test":
        first["completed"].append(first["completed"][0])
    elif damage == "wrong_revision":
        first["revision"] = "stale"
    elif damage == "different_collection":
        first["collected"].pop()
    elif damage == "worker_crash":
        first["errors"] = ["worker failed"]
    elif damage == "failed_test":
        first["exitstatus"] = 1
    elif damage == "wrong_assignment":
        first["selected"].pop()
    elif damage == "empty_collection":
        first["collected"] = []
    with pytest.raises(ValueError):
        manifest.verify(receipts, count=4, revision="abc")


def _run(tmp_path, shard, count=2, extra=()):
    receipt = tmp_path / f"receipt-{shard}.json"
    env = {**os.environ, "PYTHONPATH": str(CI), "PYTEST_ADDOPTS": ""}
    result = subprocess.run([
        sys.executable, "-m", "pytest", "-q", "-n", "2", "--dist", "loadfile",
        "--max-worker-restart=0", "-p", "pytest_shard",
        "--ci-shard", str(shard), "--ci-shard-count", str(count),
        "--ci-manifest", str(receipt), "--ci-revision", "abc", *extra,
    ], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=45)
    return result, json.loads(receipt.read_text()) if receipt.exists() else None


def test_real_xdist_shards_prove_full_discovery_including_skips(tmp_path):
    for i in range(8):
        (tmp_path / f"test_{i}.py").write_text(
            "import pytest\n"
            "@pytest.mark.parametrize('value', [1, 2])\n"
            "def test_value(value): assert value > 0\n"
            "@pytest.mark.skip(reason='fixture skip')\n"
            "def test_skipped(): pass\n"
        )
    receipts = []
    for shard in range(2):
        result, receipt = _run(tmp_path, shard)
        assert result.returncode == 0, result.stdout + result.stderr
        receipts.append(receipt)
    assert manifest.verify(receipts, count=2, revision="abc") == 24


@pytest.mark.parametrize("source", [
    "def test_bad(): assert False\n",
    "import pytest\n@pytest.fixture\ndef broken(): raise RuntimeError('setup')\ndef test_bad(broken): pass\n",
    "import os\ndef test_bad(): os._exit(1)\n",
    "def broken syntax\n",
])
def test_real_xdist_failure_cannot_produce_successful_receipt(tmp_path, source):
    (tmp_path / "test_bad.py").write_text(source)
    result, receipt = _run(tmp_path, 0, count=1)
    assert result.returncode != 0
    assert receipt is not None
    with pytest.raises(ValueError):
        manifest.verify([receipt], count=1, revision="abc")


def test_shard_refuses_filtered_discovery(tmp_path):
    (tmp_path / "test_a.py").write_text("def test_ok(): pass\n")
    result, receipt = _run(tmp_path, 0, count=1, extra=("-k", "ok"))
    assert result.returncode != 0
    assert "unfiltered automatic discovery" in result.stderr
    assert receipt is None


@pytest.mark.parametrize("event,plan,checks,regression,required,success", [
    ("pull_request", "skipped", "success", "success", "true", True),
    ("pull_request", "skipped", "success", "failure", "true", False),
    ("pull_request", "skipped", "success", "cancelled", "true", False),
    ("pull_request", "skipped", "success", "skipped", "true", False),
    ("pull_request", "skipped", "failure", "success", "true", False),
    ("push", "success", "success", "skipped", "false", True),
    ("push", "success", "success", "success", "true", True),
    ("push", "failure", "success", "skipped", "false", False),
    ("push", "skipped", "success", "skipped", "false", False),
    ("push", "success", "success", "skipped", "true", False),
])
def test_actual_required_gate_shell_rejects_failed_or_cancelled_dependencies(
    event, plan, checks, regression, required, success,
):
    import yaml

    workflow = yaml.safe_load((CI.parent / "workflows" / "guardrails.yml").read_text())
    command = workflow["jobs"]["guardrails"]["steps"][0]["run"]
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], env={
        **os.environ, "EVENT_NAME": event, "PLAN_RESULT": plan,
        "CHECKS_RESULT": checks, "REGRESSION_RESULT": regression,
        "REGRESSION_REQUIRED": required,
    }, capture_output=True, text=True, timeout=5)
    assert (result.returncode == 0) is success


def _plugin():
    # Plugin imports its existing manifest owner by module name.
    sys.modules.setdefault("regression_manifest", manifest)
    spec = importlib.util.spec_from_file_location("pytest_shard", CI / "pytest_shard.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    return plugin


def _diagnostic_receipt(tmp_path, monkeypatch, enabled=True, worker=False):
    from types import SimpleNamespace

    monkeypatch.setenv("CI_DURATION_DIAGNOSTICS", "1" if enabled else "0")
    options = dict(ci_revision="abc", ci_manifest=str(tmp_path / "manifest.json"), numprocesses=0)
    config = SimpleNamespace(getoption=lambda key, default=None: options.get(key, default))
    if worker:
        config.workeroutput = {}
    plugin = _plugin()
    owner = plugin.ShardReceipt(config, 0, 1)
    owner.collected = owner.selected = ["test_synthetic.py::test_ok"]
    session = SimpleNamespace(exitstatus=0)
    owner.pytest_sessionstart(session)
    return plugin, owner, session


def _report(owner, phase, outcome="passed", duration=0.01):
    from types import SimpleNamespace

    owner.pytest_runtest_logreport(SimpleNamespace(
        nodeid="test_synthetic.py::test_ok", when=phase, outcome=outcome,
        duration=duration, failed=outcome == "failed",
    ))


@pytest.mark.parametrize("setup", ["passed", "skipped", "failed"])
def test_diagnostics_never_change_original_manifest(tmp_path, monkeypatch, setup):
    originals = []
    for enabled in (False, True):
        root = tmp_path / str(enabled)
        _, owner, session = _diagnostic_receipt(root, monkeypatch, enabled)
        _report(owner, "setup", setup)
        if setup == "passed":
            _report(owner, "call")
        _report(owner, "teardown")
        owner.pytest_sessionfinish(session, session.exitstatus)
        originals.append(json.loads((root / "manifest.json").read_text()))
        if enabled:
            diagnostic = json.loads((root / "durations.json").read_text())
            assert diagnostic["complete"] is (setup != "failed")
            assert [s["phase"] for s in diagnostic["samples"]] == (
                ["setup", "call", "teardown"] if setup == "passed" else ["setup", "teardown"]
            )
            assert diagnostic["wall_seconds"] >= 0
        else:
            assert not (root / "durations.json").exists()
    assert originals[0] == originals[1]


@pytest.mark.parametrize("damage,code", [
    ("nan", "invalid_duration"), ("negative", "invalid_duration"),
    ("duplicate", "duplicate_phase"), ("unknown", "unknown_phase"),
    ("crash", "missing_phase"), ("cancel", "suite_unsuccessful"),
])
def test_duration_bad_samples_are_explicitly_unusable(tmp_path, monkeypatch, damage, code):
    _, owner, session = _diagnostic_receipt(tmp_path, monkeypatch)
    _report(owner, "setup")
    _report(owner, "call", duration=float("nan") if damage == "nan" else -1 if damage == "negative" else 1)
    if damage != "crash":
        _report(owner, "teardown")
    if damage == "duplicate":
        _report(owner, "call")
    if damage == "unknown":
        _report(owner, "surprise")
    if damage == "cancel":
        session.exitstatus = 2
    owner.pytest_sessionfinish(session, session.exitstatus)
    diagnostic = json.loads((tmp_path / "durations.json").read_text())
    assert not diagnostic["complete"]
    assert code in diagnostic["errors"]
    assert session.exitstatus == (1 if damage == "crash" else 2 if damage == "cancel" else 0)


def test_diagnostics_errors_cannot_hide_original_failure_or_leak_payload(tmp_path, monkeypatch, capsys):
    plugin, owner, session = _diagnostic_receipt(tmp_path, monkeypatch)
    monkeypatch.setenv("PRIVATE_SECRET", "must-not-appear")
    _report(owner, "setup")
    _report(owner, "call", "failed")
    _report(owner, "teardown")
    original_write = Path.write_text

    def fail_diagnostic(path, content, *args, **kwargs):
        if path.name == "durations.json":
            raise OSError("must-not-appear")
        return original_write(path, content, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_diagnostic)
    owner.pytest_sessionfinish(session, 1)
    assert session.exitstatus == 1
    assert json.loads((tmp_path / "manifest.json").read_text())["exitstatus"] == 1
    assert capsys.readouterr().err == "WARNING: CI duration diagnostics unusable\n"
    assert not (tmp_path / "durations.json").exists()
    # Original receipt failure is still fatal, never swallowed as telemetry.
    monkeypatch.setattr(Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("manifest")))
    with pytest.raises(OSError, match="manifest"):
        owner.pytest_sessionfinish(session, 1)


def test_worker_never_captures_diagnostics(tmp_path, monkeypatch):
    _, owner, session = _diagnostic_receipt(tmp_path, monkeypatch, worker=True)
    assert owner.diagnostics is None
    owner.pytest_sessionfinish(session, 0)
    assert "ci_collection" in owner.config.workeroutput
    assert not list(tmp_path.iterdir())


def test_diagnostics_schema_is_metadata_only(tmp_path, monkeypatch):
    _, owner, session = _diagnostic_receipt(tmp_path, monkeypatch)
    monkeypatch.setenv("PRIVATE_SECRET", "must-not-appear")
    for phase in ("setup", "call", "teardown"):
        _report(owner, phase)
    owner.pytest_sessionfinish(session, 0)
    raw = (tmp_path / "durations.json").read_text()
    payload = json.loads(raw)
    assert "must-not-appear" not in raw
    assert set(payload) == {"schema_version", "revision", "run_id", "run_attempt", "shard", "count",
                            "python_version", "runner_os", "runner_image", "xdist_workers",
                            "algorithm_version", "wall_seconds", "samples", "complete", "errors"}
    assert all(set(s) == {"nodeid", "phase", "duration", "outcome"} for s in payload["samples"])


def test_diagnostic_upload_is_separate_nonblocking_and_not_in_coverage_download():
    import yaml

    workflow = yaml.safe_load((CI.parent / "workflows" / "guardrails.yml").read_text())
    steps = workflow["jobs"]["regression"]["steps"]
    diagnostic = next(s for s in steps if s["name"] == "Preserve phase duration diagnostics")
    original = next(s for s in steps if s["name"] == "Preserve regression diagnostics")
    assert diagnostic["continue-on-error"] is True
    assert diagnostic["if"] == "${{ always() }}"
    assert diagnostic["with"]["if-no-files-found"] == "warn"
    assert diagnostic["with"]["path"] == "${{ runner.temp }}/regression/durations.json"
    assert diagnostic["with"]["name"].startswith("phase-durations-")
    assert "continue-on-error" not in original
    assert original["with"]["if-no-files-found"] == "error"
    assert "durations.json" not in original["with"]["path"]
    downloads = [s for s in workflow["jobs"]["guardrails"]["steps"] if s.get("uses", "").startswith("actions/download-artifact")]
    assert all(s["with"]["pattern"].startswith("full-regression-") for s in downloads)


def test_real_xdist_forwards_all_duration_phases_to_controller(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DURATION_DIAGNOSTICS", "1")
    (tmp_path / "test_phase.py").write_text(
        "import pytest\ndef test_ok(): pass\n"
        "@pytest.mark.skip(reason='synthetic')\ndef test_skip(): pass\n"
    )
    result, receipt = _run(tmp_path, 0, count=1)
    assert result.returncode == 0, result.stdout + result.stderr
    assert manifest.verify([receipt], count=1, revision="abc") == 2
    diagnostic = json.loads((tmp_path / "durations.json").read_text())
    assert diagnostic["complete"] is True
    assert diagnostic["errors"] == []
    assert len(diagnostic["samples"]) == 5
    assert {s["nodeid"] for s in diagnostic["samples"]} == set(receipt["selected"])


def test_capture_exception_is_isolated_and_sanitized(tmp_path, monkeypatch, capsys):
    _, owner, session = _diagnostic_receipt(tmp_path, monkeypatch)
    monkeypatch.setattr(owner.diagnostics, "capture", lambda report: (_ for _ in ()).throw(ValueError("private")))
    for phase in ("setup", "call", "teardown"):
        _report(owner, phase)
    owner.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 0
    payload = json.loads((tmp_path / "durations.json").read_text())
    assert not payload["complete"]
    assert "capture_failed" in payload["errors"]
    assert "private" not in capsys.readouterr().err
