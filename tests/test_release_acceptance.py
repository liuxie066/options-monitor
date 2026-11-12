from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import release_acceptance as acceptance
from src.application import agent_tool_contracts, plugin_runtime, service_deploy, service_upgrade


@pytest.fixture
def target(tmp_path):
    repo, runtime = tmp_path / "stable", tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    (repo / "VERSION").write_text("1.0.0\n")
    profile = {"service_provider": "systemd", "runtime_root": str(runtime),
               "restart": {"services": ["options-monitor-trade-intake.service"]},
               "env_file": "/fixture/private.env", "app_secret": "PROFILE_SECRET_MARKER"}
    (runtime / "service.profile.json").write_text(json.dumps(profile))
    state = SimpleNamespace(calls=[], rows=[], timers={}, timer_names=None, timer_rows=None,
        service_overrides={}, verify=None, drift=None, failed="", timeout=None, health_failure=False)
    for i in range(5):
        name = f"options-monitor-fixture-{i}.timer"
        state.timers[name] = {key: "" for key in acceptance.TIMER_PROPERTIES}
        state.timers[name].update(Id=name, LoadState="loaded", UnitFileState="enabled" if i == 4 else "disabled",
            ActiveState="active" if i == 4 else "inactive", SubState="waiting" if i == 4 else "dead", Result="success")

    def update(version):
        (repo / "VERSION").write_text(version)
        (runtime / "upgrade_status.json").write_text(json.dumps({"ok": True, "status": "ok", "target_version": version,
            "runtime_prepare": {"duration_seconds": 12 if version == "1.0.0" else 2,
                "dependency_hash": "same-dependencies", "installer": "cache", "venv_reused": True,
                "password": "METRICS_SECRET_MARKER"}, "operations": ["STATUS_SECRET_MARKER"]}))
        state.rows = []
        for key, item in plugin_runtime.PLUGINS.items():
            installed = key not in {"wechat", "broker-longbridge"}
            source = ("managed" if key in {"group-query-bot", "market-opend", "broker-futu"} else "release_source") if installed else None
            state.rows.append({"id": key, "distribution": item["distribution"], "installed": installed,
                "compatible": True if installed else None, "version": version if source == "managed" else "0.1.0" if installed else None,
                "source": source, "status": "installed" if installed else "not_installed"})

    update("1.0.0")
    def owner_inventory(**kwargs):
        assert kwargs["repo_root"] == repo.resolve()
        return {"ok": all(row.get("status") not in {"invalid", "incompatible"} for row in state.rows), "plugins": deepcopy(state.rows)}

    owners = SimpleNamespace(runtime=plugin_runtime, contracts=agent_tool_contracts, deploy=service_deploy,
        distribution=SimpleNamespace(list_plugins=owner_inventory), upgrade=SimpleNamespace(
            load_upgrade_status=service_upgrade.load_upgrade_status, upgrade_status_path=service_upgrade.upgrade_status_path,
            _restart_service_names=service_upgrade._restart_service_names,
            _child_env_from_profile=service_upgrade._child_env_from_profile,
            _post_upgrade_service_health=service_upgrade._post_upgrade_service_health))

    def properties(rows):
        return "\n\n".join("\n".join(f"{key}={value}" for key, value in row.items()) for row in rows) + "\n"

    def envelope(name, data):
        return {"schema_version": agent_tool_contracts.SCHEMA_VERSION, "tool_name": name, "ok": True,
                "data": data, "warnings": [], "error": None}

    def runner(command, **kwargs):
        state.calls.append((list(command), kwargs))
        assert "PYTHONPATH" not in kwargs["env"] and "PYTHONHOME" not in kwargs["env"]
        assert kwargs["env"]["OM_PYTHON"] == sys.executable
        assert kwargs["timeout"] > 0
        rc, output = 0, ""
        if state.timeout and state.timeout in command:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"wss://fixture/ws?token=URL_SECRET_MARKER",
                stderr=b'Authorization: Bearer HEADER_SECRET_MARKER\napp_secret="STDERR_SECRET_MARKER"')
        if command[0] == "systemctl":
            if command[1] == "list-unit-files":
                output = "\n".join(name + " disabled disabled" for name in
                    (state.timer_names if state.timer_names is not None else state.timers))
            elif command[1] == "show":
                names = [item for item in command if item.endswith((".timer", ".service"))]
                assert names, "Never issue unqualified systemctl show"
                if "UnitFileState" in command[3]:
                    output = properties(state.timer_rows if state.timer_rows is not None else [state.timers[name] for name in names])
                else:
                    rows = []
                    for name in names:
                        row = dict(Id=name, LoadState="loaded", ActiveState="active", SubState="running", Result="success",
                            ExecMainStatus="0", MainPID="4242", NRestarts="3", WorkingDirectory=str(repo))
                        row.update(state.service_overrides)
                        rows.append(row)
                    output = properties(rows)
            elif command[1] in {"is-active", "is-enabled"}:
                rc = 3 if state.health_failure else 0
                output = "active" if command[1] == "is-active" else "enabled"
            elif command[1] == "--failed":
                output = state.failed
            else:
                raise AssertionError(command)
        elif "inbound" in command and "feishu-ws" in command and "--check" in command:
            output = json.dumps({"ok": True})  # canonical channel probe, never executed here
        elif command[1:3] == ["-m", "pip"]:
            assert command == [sys.executable, "-m", "pip", "check"]
        elif command[1:3] == ["update", "verify"]:
            assert kwargs["timeout"] == 180 and "--no-check-latest" in command
            value = state.verify if state.verify is not None else envelope("update.verify", {"ok": True, "status": "ok"})
            output = value if isinstance(value, str) else json.dumps(value)
        elif command[1:3] == ["service", "drift"]:
            assert command[command.index("--profile-path") + 1] == str(runtime / "service.profile.json")
            value = state.drift if state.drift is not None else envelope("service.drift", {
                "checked": True, "supported": True, "summary": {"ok": True, "status": "warn", "error_count": 0, "warning_count": 1},
                "compatibility_warnings": [{"code": "retired_units", "units": list(state.timers)[:4]}]})
            output = value if isinstance(value, str) else json.dumps(value)
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, rc, output, "")

    def collect(post=True, **kwargs):
        method = acceptance.collect_acceptance if post else acceptance.collect_baseline
        if post:
            kwargs.setdefault("expected_version", "2.0.0")
            kwargs.setdefault("proc_cwd", lambda pid: str(repo))
        return method(repo, runtime, runner=runner, owners=owners, **kwargs)

    baseline = collect(False)
    assert baseline["status"] == "passed", baseline
    update("2.0.0")
    state.calls.clear()
    return SimpleNamespace(repo=repo, runtime=runtime, state=state, owners=owners,
                           baseline=baseline, collect=collect, envelope=envelope, runner=runner)


def test_mixed_plugins_disabled_timers_fresh_owner_and_warning_are_accepted(target, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/fixture/wrong")
    monkeypatch.setenv("PYTHONHOME", "/fixture/wrong")
    post = target.collect()
    summary = acceptance.summarize(post, target.baseline)
    assert summary["status"] == "passed", summary
    assert len(post["plugins"]["plugins"]) == len(plugin_runtime.PLUGINS)
    assert sum(row["UnitFileState"] == "disabled" for row in post["timers"]["rows"]) == 4
    assert summary["warnings"][0]["code"] == "retired_units"
    assert post["services"][0]["NRestarts"] == "3"  # observation, not a zero-only gate
    assert post["health"]["ok"] and len(post["health"]["checks"]) >= 2
    assert any(command[:2] == ["systemctl", "is-active"] for command, _ in target.state.calls)
    assert all(not set(command) & {"--ensure", "--confirm", "restart", "reconcile"} for command, _ in target.state.calls)
    comparison = summary["comparison"]
    assert comparison["dependency_hash_unchanged"] is True
    assert comparison["duration_difference_seconds"] == -10
    assert comparison["current_installer"] == "cache" and comparison["current_venv_reused"] is True


@pytest.mark.parametrize("fault,expected", [
    ("duplicate", "missing_data"), ("missing", "missing_data"),
    ("invalid", "failed"), ("incompatible", "failed"), ("selection", "failed"), ("managed", "failed"),
])
def test_bad_plugin_inventory_or_selection_never_passes(target, fault, expected):
    rows = target.state.rows
    if fault == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif fault == "missing":
        rows.pop()
    elif fault == "invalid":
        rows[0].update(installed=None, compatible=False, status="invalid")
    elif fault == "incompatible":
        rows[0].update(compatible=False, status="incompatible")
    elif fault == "selection":
        rows[0].update(installed=False, compatible=None, version=None, source=None, status="not_installed")
    else:
        next(row for row in rows if row["source"] == "managed")["source"] = "release_source"
    assert acceptance.summarize(target.collect(), target.baseline)["status"] == expected


@pytest.mark.parametrize("fault,expected", [("enable", "failed"), ("lost", "failed"),
    ("missing_row", "missing_data"), ("duplicate_row", "missing_data"), ("duplicate_name", "missing_data")])
def test_timer_preservation_requires_complete_installed_rows(target, fault, expected):
    state = target.state
    name = next(iter(state.timers))
    if fault == "enable":
        state.timers[name].update(UnitFileState="enabled", ActiveState="active")
    elif fault == "lost":
        state.timers.pop(name)
    elif fault == "missing_row":
        state.timer_rows = list(state.timers.values())[1:]
    elif fault == "duplicate_row":
        state.timer_rows = list(state.timers.values()) + [state.timers[name]]
    else:
        state.timer_names = list(state.timers) + [name]
    assert acceptance.summarize(target.collect(), target.baseline)["status"] == expected


def test_zero_timers_is_valid_and_does_not_issue_unqualified_show(target):
    target.state.timers.clear()
    empty_baseline = target.collect(False)
    post = target.collect()
    assert acceptance.summarize(post, empty_baseline)["status"] == "passed"
    assert post["timers"] == {"names": [], "rows": []}
    assert not any("UnitFileState" in " ".join(command) for command, _ in target.state.calls)


@pytest.mark.parametrize("fault", ["profile", "services", "checks", "signature", "unsupported"])
def test_skipped_or_missing_health_is_missing_data(target, fault):
    if fault in {"profile", "services"}:
        profile = {} if fault == "profile" else {"service_provider": "systemd", "services": []}
        (target.runtime / "service.profile.json").write_text(json.dumps(profile))
    elif fault == "signature":
        target.owners.upgrade._post_upgrade_service_health = lambda: None
    else:
        target.owners.upgrade._post_upgrade_service_health = lambda **kwargs: {
            "ok": fault != "unsupported", "status": "unsupported" if fault == "unsupported" else "ok", "checks": []}
    assert acceptance.summarize(target.collect(), target.baseline)["status"] == "missing_data"


@pytest.mark.parametrize("fault", ["cwd", "service", "health", "failed_units"])
def test_live_service_failures_are_independent_of_apply_success(target, fault):
    kwargs = {}
    if fault == "cwd":
        kwargs["proc_cwd"] = lambda pid: str(target.repo.parent / "old-release")
    elif fault == "service":
        target.state.service_overrides["ExecMainStatus"] = "1"
    elif fault == "health":
        target.state.health_failure = True
    else:
        target.state.failed = "options-monitor-broken.service loaded failed failed\n"
    assert acceptance.summarize(target.collect(**kwargs), target.baseline)["status"] == "failed"


def test_unreadable_process_cwd_is_missing_data(target):
    def unavailable(pid):
        raise PermissionError("fixture proc cwd unavailable")
    assert acceptance.summarize(target.collect(proc_cwd=unavailable), target.baseline)["status"] == "missing_data"


@pytest.mark.parametrize("name", ["verify", "drift"])
@pytest.mark.parametrize("fault,expected", [("json", "missing_data"), ("tool", "missing_data"),
    ("schema", "missing_data"), ("error", "failed"), ("data", "missing_data"), ("false", "failed")])
def test_cli_envelope_and_distinct_payloads_are_checked(target, name, fault, expected):
    tool = "update.verify" if name == "verify" else "service.drift"
    data = {"ok": True, "status": "ok"} if name == "verify" else {
        "checked": True, "supported": True, "summary": {"ok": True, "status": "ok", "error_count": 0}}
    value = target.envelope(tool, data)
    if fault == "json":
        value = "not JSON"
    elif fault == "tool":
        value["tool_name"] = "different.tool"
    elif fault == "schema":
        value["schema_version"] = "unknown"
    elif fault == "error":
        value.update(ok=False, error={"code": "fixture_failure"})
    elif fault == "data":
        value["data"] = None
    elif name == "verify":
        value["data"]["ok"] = False
    else:
        value["data"]["summary"].update(ok=False, error_count=1)
    setattr(target.state, name, value)
    assert acceptance.summarize(target.collect(), target.baseline)["status"] == expected


def test_prior_unknown_does_not_invent_a_hash_match(target):
    target.baseline["upgrade"] = {"runtime_prepare": None}
    summary = acceptance.summarize(target.collect(), target.baseline)
    assert summary["status"] == "passed"
    assert summary["comparison"]["prior_duration_seconds"] is None
    assert summary["comparison"]["dependency_hash_unchanged"] is None


@pytest.mark.parametrize("fault", ["missing", "target", "duration"])
def test_current_metrics_must_belong_to_target_and_be_present(target, fault):
    path = target.runtime / "upgrade_status.json"
    value = json.loads(path.read_text())
    if fault == "missing":
        value["runtime_prepare"].pop("dependency_hash")
    elif fault == "target":
        value["target_version"] = "old-version"
    else:
        value["runtime_prepare"]["duration_seconds"] = None
    path.write_text(json.dumps(value))
    assert acceptance.summarize(target.collect(), target.baseline)["status"] == "missing_data"


def test_baseline_target_mismatch_is_missing_data_and_failure_dominates(target):
    target.baseline["host"] = "different-host"
    summary = acceptance.summarize(target.collect(), target.baseline)
    assert summary["status"] == "missing_data"
    assert summary["comparison"]["prior_duration_seconds"] is None
    assert summary["comparison"]["dependency_hash_unchanged"] is None
    target.state.failed = "fixture.service loaded failed failed"
    assert acceptance.summarize(target.collect(), target.baseline)["status"] == "failed"


def test_summaries_do_not_rerun_commands_and_outputs_are_indexed(target, tmp_path, monkeypatch):
    post = target.collect()
    def forbidden(*args, **kwargs):
        raise AssertionError("Summary must not execute a probe")
    monkeypatch.setattr(subprocess, "run", forbidden)
    summary = acceptance.summarize(post, target.baseline)
    assert acceptance.summarize(post, target.baseline) == summary
    folder = tmp_path / "evidence"
    files = acceptance.write_outputs(folder, post, summary)
    assert len(files) == 4
    index = json.loads((folder / "evidence-index.json").read_text())
    assert index["collection_id"] == post["collection_id"]
    for row in index["files"]:
        data = (folder / row["file"]).read_bytes()
        assert len(data) == row["size_bytes"] and hashlib.sha256(data).hexdigest() == row["sha256"]
        assert stat.S_IMODE((folder / row["file"]).stat().st_mode) == 0o600
    text = "\n".join((folder / name).read_text() for name in files)
    assert "PROFILE_SECRET_MARKER" not in text and "METRICS_SECRET_MARKER" not in text and "STATUS_SECRET_MARKER" not in text
    original = (folder / "acceptance-evidence.json").read_bytes()
    with pytest.raises(ValueError, match="never overwritten"):
        acceptance.write_outputs(folder, post, summary)
    assert (folder / "acceptance-evidence.json").read_bytes() == original


def test_timeout_stderr_and_partial_output_are_redacted_before_writing(target, tmp_path):
    target.state.timeout = "verify"
    post = target.collect()
    summary = acceptance.summarize(post, target.baseline)
    assert summary["status"] == "failed"
    folder = tmp_path / "timeout"
    names = acceptance.write_outputs(folder, post, summary)
    text = "\n".join((folder / name).read_text() for name in names)
    for marker in ("URL_SECRET_MARKER", "HEADER_SECRET_MARKER", "STDERR_SECRET_MARKER"):
        assert marker not in text
    assert "REDACTED" in text and any(row.get("timed_out") for row in post["commands"])


def test_interruption_writes_incomplete_sanitized_partial_evidence(target, tmp_path, monkeypatch, capsys):
    before = tmp_path / "before.json"
    before.write_text(json.dumps(target.baseline))
    original = acceptance.collect_acceptance
    def interrupt_runner(command, **kwargs):
        if command[1:3] == ["-m", "pip"]:
            raise KeyboardInterrupt("wss://fixture/?token=INTERRUPT_SECRET_MARKER")
        return target.runner(command, **kwargs)
    def interrupted(repo, runtime, **kwargs):
        return original(repo, runtime, owners=target.owners, runner=interrupt_runner,
                        proc_cwd=lambda pid: str(target.repo), **kwargs)
    monkeypatch.setattr(acceptance, "collect_acceptance", interrupted)
    output = tmp_path / "partial"
    rc = acceptance.main(["accept", "--repo-root", str(target.repo), "--runtime-root", str(target.runtime),
        "--expected-version", "2.0.0", "--baseline", str(before), "--output-dir", str(output)])
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["status"] == "missing_data"
    payload = json.loads((output / "acceptance-evidence.json").read_text())
    assert payload["complete"] is False
    assert "INTERRUPT_SECRET_MARKER" not in (output / "acceptance-evidence.json").read_text()


def test_staged_cli_uses_target_owners_and_target_python(tmp_path):
    repo, runtime, bin_dir = tmp_path / "release", tmp_path / "runtime", tmp_path / "bin"
    app = repo / "src/application"
    app.mkdir(parents=True)
    runtime.mkdir()
    bin_dir.mkdir()
    (repo / "src/__init__.py").write_text("")
    (app / "__init__.py").write_text("")
    (repo / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    (repo / "VERSION").write_text("2.0.0")
    (runtime / "service.profile.json").write_text(json.dumps({"service_provider": "systemd", "runtime_root": str(runtime)}))
    (app / "plugin_runtime.py").write_text('PLUGINS = {"target-fixture": {"distribution": "target-owned"}}\n')
    (app / "plugin_distribution.py").write_text('''def list_plugins(**kwargs):
    return {"ok": True, "plugins": [{"id": "target-fixture", "distribution": "target-owned", "installed": True,
        "version": "2.0.0", "source": "managed", "compatible": True, "status": "installed"}]}
''')
    (app / "agent_tool_contracts.py").write_text('SCHEMA_VERSION = "1.0"\n')
    (app / "service_deploy.py").write_text('import json\ndef load_service_profile(path): return json.loads(path.read_text())\n')
    (app / "service_upgrade.py").write_text('''import os
from pathlib import Path
def load_upgrade_status(**kwargs): return None
def upgrade_status_path(root): return Path(root) / "upgrade_status.json"
def _restart_service_names(profile): return ["options-monitor-target.service"]
def _child_env_from_profile(profile): return {"PATH": os.environ["PATH"]}
''')
    systemctl = bin_dir / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)
    output = tmp_path / "before"
    proc = subprocess.run([str(repo / ".venv/bin/python"), "-I", str(Path(acceptance.__file__)), "baseline",
        "--repo-root", str(repo), "--runtime-root", str(runtime), "--output-dir", str(output)], cwd=tmp_path,
        env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "PYTHONPATH": "/fixture/wrong"},
        capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads((output / "baseline.json").read_text())
    assert payload["canonical_plugins"] == {"target-fixture": "target-owned"}
    assert payload["resolved_repo_root"] == str(repo) and payload["version"] == "2.0.0"


def test_current_upgrade_snapshot_is_saved_before_verify(target):
    def runner(command, **kwargs):
        if command[1:3] == ["update", "verify"]:
            (target.runtime / "upgrade_status.json").write_text('{"target_version": "later-snapshot"}')
        return target.runner(command, **kwargs)
    post = acceptance.collect_acceptance(target.repo, target.runtime, expected_version="2.0.0",
        owners=target.owners, runner=runner, proc_cwd=lambda pid: str(target.repo))
    assert acceptance.summarize(post, target.baseline)["status"] == "passed"
    assert post["upgrade"]["target_version"] == "2.0.0"


def test_canonical_channel_health_commands_are_preserved(target):
    path = target.runtime / "service.profile.json"
    profile = json.loads(path.read_text())
    profile["restart"]["services"].append("options-monitor-feishu-ws.service")
    path.write_text(json.dumps(profile))
    post = target.collect()
    assert acceptance.summarize(post, target.baseline)["status"] == "passed"
    assert any(row["check"] == "feishu-ws-check" for row in post["health"]["checks"])
    assert any("feishu-ws" in command and "--check" in command for command, _ in target.state.calls)


@pytest.mark.parametrize("fault,status,rc", [("none", "passed", 0), ("failure", "failed", 1), ("missing", "missing_data", 2),
    ("timer", "failed", 1), ("selection", "failed", 1), ("baseline", "missing_data", 2)])
def test_main_reports_real_acceptance_exit_status(target, tmp_path, monkeypatch, capsys, fault, status, rc):
    original = acceptance.collect_acceptance
    def bound(repo, runtime, **kwargs):
        return original(repo, runtime, owners=target.owners, runner=target.runner,
                        proc_cwd=lambda pid: str(target.repo), **kwargs)
    monkeypatch.setattr(acceptance, "collect_acceptance", bound)
    if fault == "failure":
        target.state.failed = "fixture.service loaded failed failed"
    elif fault == "missing":
        target.state.verify = "invalid JSON"
    elif fault == "timer":
        target.state.timers.pop(next(iter(target.state.timers)))
    elif fault == "selection":
        target.state.rows[0].update(installed=False, compatible=None, version=None, source=None, status="not_installed")
    elif fault == "baseline":
        target.baseline["host"] = "different-host"
    before = tmp_path / "baseline.json"
    before.write_text(json.dumps(target.baseline))
    output = tmp_path / "after"
    output.mkdir()  # an existing empty output directory is supported
    result = acceptance.main(["accept", "--repo-root", str(target.repo), "--runtime-root", str(target.runtime),
        "--expected-version", "2.0.0", "--baseline", str(before), "--output-dir", str(output)])
    assert result == rc
    assert json.loads(capsys.readouterr().out)["status"] == status
    for name in ("acceptance-evidence.json", "acceptance-summary.json", "upgrade-comparison.json", "evidence-index.json"):
        assert json.loads((output / name).read_text())["status"] == status
    durable = json.loads((output / "acceptance-evidence.json").read_text())
    if fault == "timer":
        assert any(row["name"] == "timer_activation_preserved" and row["status"] == "failed" for row in durable["checks"])


def test_invalid_input_and_existing_output_are_not_collected(target, tmp_path, capsys):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep")
    assert acceptance.main(["baseline", "--repo-root", str(target.repo), "--runtime-root", str(target.runtime),
                            "--output-dir", str(output)]) == 2
    assert marker.read_text() == "keep" and not target.state.calls
    assert "never overwritten" in capsys.readouterr().err
