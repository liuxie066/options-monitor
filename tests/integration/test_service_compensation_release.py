"""Real child processes with version-distinct fixtures; never touch host services."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.application import service_upgrade as upgrade


_OLD_OWNER = '''
import json
from pathlib import Path
LOG = Path(__file__).parents[2] / "calls.jsonl"
def record(phase, **data):
    with LOG.open("a") as stream:
        stream.write(json.dumps({"phase": phase, **data}) + "\\n")
def _profile_runtime_config_targets(profile):
    return [{"market": "us", "config_path": profile["config_paths"]["us"],
             "source": "yaml", "config_yaml": profile["config_authoring"]["config_yaml"]}]
def _run_required(command, **kwargs):
    record("yaml", command=command)
    if "incompatible" in Path(command[command.index("--config-yaml") + 1]).read_text():
        raise RuntimeError("old authoring schema rejected")
def _validate_committed_runtime_configs(prepared, **kwargs):
    record("runtime")
    if "incompatible" in Path(prepared["targets"][0]["config_path"]).read_text():
        raise RuntimeError("old runtime schema rejected")
    return [{"market": "us", "phase": "post_switch"}]
def service_drift(**kwargs):
    record("render", snapshot=kwargs["preserved_activation_states"],
           policy=kwargs["activation_policy"], profile=kwargs["profile"])
    scenario = kwargs["profile"].get("scenario", "ok")
    summary = {"ok": scenario not in {"render-error", "render-warning"},
               "status": "warn" if scenario == "render-warning" else "ok"}
    return {"summary": summary, "apply_errors": ["write failed"] if scenario == "apply-error" else [],
            "owner": "old", "snapshot": kwargs["preserved_activation_states"]}
def _service_reconcile_remediation(result):
    return ["repair old unit bundle"]
def _restart_services_from_loaded_profile(profile, **kwargs):
    record("restart")
    if profile.get("scenario") == "restart-error":
        raise RuntimeError("old restart failed")
    return ["old-bot.service"]
def _post_upgrade_service_health(profile, repo_root, **kwargs):
    record("health", flag="--assistant-config", config="config.assistant.json")
    return {"ok": profile.get("scenario") != "health-error", "owner": "old",
            "remediation": ["check old channel"]}
'''


def _release_fixture(tmp_path: Path, *, scenario: str = "ok"):
    old = tmp_path / "old"
    module = old / "src" / "application"
    module.mkdir(parents=True)
    (old / "src" / "__init__.py").write_text("")
    (module / "__init__.py").write_text("")
    (module / "service_upgrade.py").write_text(_OLD_OWNER)
    (module / "bot").mkdir()
    (module / "bot/runtime.py").write_text("# Fixture uses a Python Bot runtime.\n")
    python = old / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    new = tmp_path / "new-controller"
    new.mkdir()
    current = tmp_path / "current"
    current.symlink_to(new)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    authoring = runtime / "config.yaml"
    authoring.write_text("compatible authoring\n")
    generated = runtime / "config.us.json"
    generated.write_text('"new live snapshot"\n')
    backup = runtime / "backup.json"
    backup.write_text('"compatible snapshot"\n')
    profile = {"service_provider": "systemd", "markets": ["us"],
               "services": [{"name": "old-bot.service"}], "scenario": scenario,
               "restart": {"services": ["old-bot.service"]},
               "config_paths": {"us": str(generated)},
               "config_authoring": {"source": "yaml", "config_yaml": str(authoring)}}
    commit = {"artifacts": [{"live_path": str(generated), "backup_path": str(backup),
                              "existed_before": True}]}
    snapshot = {"tick.timer": {"activation_state": "enabled", "active_state": "inactive"}}
    return old, new, current, runtime, profile, commit, snapshot


def _compensate(fixture, *, run_cmd=subprocess.run, restart_services=True):
    old, new, current, runtime, profile, commit, snapshot = fixture
    operations = []
    out = upgrade._compensate_service_transition(
        repo_link=current, previous_dir=old, transition_dir=new, runtime_root=runtime,
        previous_profile=profile, config_commit=commit, restart_services=restart_services,
        activation_policy="preserve-existing", preserved_activation_states=snapshot,
        run_cmd=run_cmd, operations=operations)
    return out, operations


def _calls(old):
    path = old / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_compensation_executes_restored_owners_with_original_snapshot(monkeypatch, tmp_path):
    fixture = _release_fixture(tmp_path)
    old, _, current, runtime, _, _, snapshot = fixture
    # A newer controller/module is already imported, and inherited import settings
    # deliberately point there. The child must import the restored code instead.
    monkeypatch.setenv("PYTHONPATH", str(Path(upgrade.__file__).parents[2]))
    monkeypatch.setattr(upgrade, "service_drift", lambda **_: pytest.fail("new controller rendered old units"))
    request_paths = []

    def runner(command, **kwargs):
        path = Path(command[-1])
        request_paths.append(path)
        assert os.stat(path).st_mode & 0o777 == 0o600
        assert os.stat(path.parent).st_mode & 0o777 == 0o700
        assert kwargs["cwd"] == str(old)
        return subprocess.run(command, **kwargs)

    out, operations = _compensate(fixture, run_cmd=runner)
    assert out["ok"] is True
    assert current.resolve() == old
    assert (runtime / "config.us.json").read_bytes() == (runtime / "backup.json").read_bytes()
    assert (runtime / "config.yaml").read_text() == "compatible authoring\n"
    calls = _calls(old)
    assert [item["phase"] for item in calls] == ["yaml", "runtime", "render", "restart", "health"]
    assert calls[2]["snapshot"] == snapshot
    assert calls[2]["policy"] == "preserve-existing"
    assert calls[4]["flag"] == "--assistant-config"
    assert out["service_reconcile"]["owner"] == out["service_health"]["owner"] == "old"
    assert all(not path.exists() for path in request_paths)
    assert any(item["operation"] == "restore_services_from_release" and item["ok"] for item in operations)


@pytest.mark.parametrize("reject", ["yaml", "runtime"])
def test_incompatible_snapshot_stops_before_render_or_restart(tmp_path, reject):
    fixture = _release_fixture(tmp_path)
    old, _, current, runtime, *_ = fixture
    path = runtime / ("config.yaml" if reject == "yaml" else "backup.json")
    path.write_text("incompatible migrated source\n")
    before = path.read_bytes()
    out, _ = _compensate(fixture)
    assert out["ok"] is False
    assert out["symlink_restored"] is True and current.resolve() == old
    assert out["config_restore"]["ok"] is True
    assert out["service_reconcile"] == {}
    assert out["restarted_services"] == []
    assert [item["phase"] for item in _calls(old)] == (["yaml"] if reject == "yaml" else ["yaml", "runtime"])
    assert path.read_bytes() == before
    assert any("independent migrations were not reverted" in item for item in out["remediation"])
    assert any("do not restart" in item for item in out["remediation"])


@pytest.mark.parametrize("scenario,last", [
    ("render-error", "render"), ("render-warning", "render"), ("apply-error", "render"),
    ("restart-error", "restart"), ("health-error", "health"),
])
def test_restored_release_failure_never_claims_complete_compensation(tmp_path, scenario, last):
    fixture = _release_fixture(tmp_path, scenario=scenario)
    out, _ = _compensate(fixture)
    assert out["ok"] is False and out["errors"]
    calls = _calls(fixture[0])
    assert calls[-1]["phase"] == last
    if last == "render":
        assert out["restarted_services"] == [] and out["service_health"] == {}
    if last == "health":
        assert out["restarted_services"] == ["old-bot.service"]
        assert out["service_health"]["ok"] is False


def test_failed_snapshot_restore_skips_all_service_effects(tmp_path):
    fixture = _release_fixture(tmp_path)
    (fixture[3] / "backup.json").unlink()
    out, operations = _compensate(fixture, run_cmd=lambda *_a, **_k: pytest.fail("child ran after restore failure"))
    assert out["ok"] is False and out["config_restore"]["ok"] is False
    assert out["restarted_services"] == [] and not _calls(fixture[0])
    assert any(item["operation"] == "restore_runtime_config" and not item["ok"] for item in operations)


def test_missing_restored_interface_fails_closed(tmp_path):
    fixture = _release_fixture(tmp_path)
    module = fixture[0] / "src/application/service_upgrade.py"
    module.write_text(module.read_text().replace("def _validate_committed_runtime_configs", "def _missing_validator"))
    out, _ = _compensate(fixture)
    assert out["ok"] is False
    assert out["restarted_services"] == []
    assert [item["phase"] for item in _calls(fixture[0])] == ["yaml"]


@pytest.mark.parametrize("kind", ["empty", "malformed", "wrong-schema", "nonzero-success", "inconsistent", "timeout"])
def test_child_result_failures_do_not_become_success(tmp_path, kind):
    fixture = _release_fixture(tmp_path)
    paths = []

    def runner(command, **kwargs):
        paths.append(Path(command[-1]))
        assert kwargs["timeout"] >= 660  # Must cover existing long health budgets.
        if kind == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        payload = {"ok": True, "config_validate": [], "service_reconcile": {"summary": {"ok": True}},
                   "restarted_services": [], "service_health": {"ok": True}, "operations": [],
                   "errors": [], "remediation": []}
        if kind == "wrong-schema":
            payload = {"ok": True, "data": {}}
        if kind == "inconsistent":
            payload["service_reconcile"] = {"summary": {"ok": False}}
        stdout = "" if kind == "empty" else ("not json" if kind == "malformed" else json.dumps(payload))
        return subprocess.CompletedProcess(command, 2 if kind == "nonzero-success" else 0, stdout=stdout, stderr="bad child")

    out, operations = _compensate(fixture, run_cmd=runner)
    assert out["ok"] is False and out["errors"]
    assert all(not path.exists() for path in paths)
    child = next(item for item in operations if item.get("operation") == "restore_services_from_release")
    if kind == "timeout":
        assert child["timed_out"] is True


@pytest.mark.parametrize("failure", ["reconcile", "restart"])
def test_public_upgrade_does_not_claim_rollback_of_incompatible_config(monkeypatch, tmp_path, failure):
    fixture = _release_fixture(tmp_path)
    old, new, current, runtime, profile, commit, _ = fixture
    current.unlink()
    current.symlink_to(old)
    new.rename(tmp_path / "1.0.1")
    new = tmp_path / "1.0.1"
    (old / "VERSION").write_text("1.0.0\n")
    (new / "VERSION").write_text("1.0.1\n")
    (new / "src/application/bot").mkdir(parents=True)
    (new / "src/application/bot/runtime.py").write_text("# Python fixture\n")
    (runtime / "service.profile.json").write_text(json.dumps(profile))
    (runtime / "backup.json").write_text("incompatible independent migration\n")
    monkeypatch.setattr(upgrade, "service_upgrade_check", lambda **_: {
        "ok": True, "latest_version": "1.0.1", "release_tag": "v1.0.1"})
    monkeypatch.setattr(upgrade, "_materialize_release_from_git_cache", lambda **_: {
        "status": "reused", "target_dir": str(new)})
    monkeypatch.setattr(upgrade, "_ensure_release_runtime", lambda **_: {"status": "ready"})
    monkeypatch.setattr(upgrade, "_run_required", lambda *_a, **_k: None)
    monkeypatch.setattr(upgrade, "_prepare_runtime_configs_for_release", lambda **_: {"status": "prepared"})
    monkeypatch.setattr(upgrade, "_commit_prepared_runtime_configs", lambda **_: commit)
    monkeypatch.setattr(upgrade, "_validate_committed_runtime_configs", lambda **_: [])

    def reconcile(**kwargs):
        if failure == "reconcile":
            raise upgrade.ServiceTransitionError("new reconcile failed", status="upgraded_service_reconcile_failed", remediation=[])
        return {"summary": {"ok": True}}

    def restart(**kwargs):
        raise upgrade.ServiceRestartError("new restart failed", failed_services=["old-bot.service"],
                                          restarted_services=[], remediation=["new restart instruction"])

    monkeypatch.setattr(upgrade, "_reconcile_services_from_current_release", reconcile)
    monkeypatch.setattr(upgrade, "_restart_services_from_loaded_profile", restart)
    out = upgrade.service_upgrade(repo_root=current, runtime_root=runtime, releases_root=tmp_path,
                                  target_version="1.0.1", confirm=True)
    assert out["ok"] is False and out["rolled_back"] is False
    assert out["compensation"].get("symlink_restored") is True, (out.get("status"), out.get("error"))
    assert out["compensation"]["ok"] is False
    assert out["compensation"]["restarted_services"] == []
    assert current.resolve() == old
    assert any("do not restart" in item for item in out["remediation"])
    assert [item["phase"] for item in _calls(old)] == ["yaml", "runtime"]
    persisted = json.loads((runtime / "upgrade_status.json").read_text())
    assert persisted["rolled_back"] is False and persisted["remediation"] == out["remediation"]


def test_no_restart_still_validates_and_reconciles_with_restored_release(tmp_path):
    fixture = _release_fixture(tmp_path)
    out, _ = _compensate(fixture, restart_services=False)
    assert out["ok"] is True
    assert [item["phase"] for item in _calls(fixture[0])] == ["yaml", "runtime", "render"]
    assert out["restarted_services"] == [] and out["service_health"] == {}
