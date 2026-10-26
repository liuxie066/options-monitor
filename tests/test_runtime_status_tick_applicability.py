from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.account_config import accounts_from_config, normalize_accounts
from src.application.agent_tool_contracts import build_response, mask_path
from src.application.agent_tools import runtime_status_impl as runtime
from src.application.runtime_config_paths import read_json_object_or_empty
from src.application.service_deploy import build_service_profile
from src.interfaces.cli.observability_ops import add_runtime_observability_commands, handle_observability_command


@pytest.fixture
def tick_runtime(tmp_path, monkeypatch):
    """Real collector/profile/public projection/CLI; unrelated I/O is isolated."""
    config_path = tmp_path / "config.us.json"
    config = {"accounts": ["demo"], "trade_intake": {"enabled": False}, "notifications": {"enabled": False}}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    shared = tmp_path / "output_shared/state"
    (shared / "current").mkdir(parents=True)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("status must not probe a service or external provider by default")

    monkeypatch.setattr(runtime, "ledger_store_payload", lambda _path: {"runtime_root": str(tmp_path)})
    monkeypatch.setattr(runtime, "build_wheel_activation_readiness", lambda **_kw: {})
    monkeypatch.setattr(runtime, "service_drift_status", lambda **_kw: {"summary": {"ok": True}})
    monkeypatch.setattr(runtime, "build_effective_env_with_status", lambda **_kw: (
        SimpleNamespace(warnings=[], values={}), {"warnings": []},
    ))
    monkeypatch.setattr(runtime, "_bot_runtime_summary", lambda **_kw: {"enabled": False})
    monkeypatch.setattr(runtime, "build_channel_status", lambda **_kw: {"channels": {}})
    monkeypatch.setattr(runtime, "_config_authority_payload", lambda *_args, **_kw: {"ok": True})
    monkeypatch.setattr(runtime, "system_alert_delivery_status", lambda *_args, **_kw: {"status": "ok"})
    monkeypatch.setattr(runtime, "_tick_unit_execution_status", forbidden)
    dependencies = {
        "load_runtime_config": lambda **_kw: (config_path, config),
        "normalize_accounts": normalize_accounts, "accounts_from_config": accounts_from_config,
        "read_json_object_or_empty": read_json_object_or_empty,
        "repo_base": lambda: tmp_path, "mask_path": mask_path,
    }

    def prepare(*, provider="systemd", market="us", scheduled=True, result=None, last_status="ok", explicit=False):
        config_path = tmp_path / f"config.{market}.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        timer = f"options-monitor-tick-{market}.timer" if provider == "systemd" else f"com.options-monitor.tick-{market}"
        services = [timer] if scheduled else []
        profile = build_service_profile(
            target=provider, repo_root=tmp_path, runtime_root=tmp_path,
            accounts=["demo"], markets=[market], service_names=services, config_paths={market: config_path},
        )
        profile_path = tmp_path / ("explicit.profile.json" if explicit else "service.profile.json")
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        (shared / "last_run.json").write_text(json.dumps({"status": last_status, "no_send": True}), encoding="utf-8")
        if result is not None:
            (shared / "current" / f"tick_cron_last_result.{market}.current.json").write_text(
                json.dumps(result), encoding="utf-8",
            )
        payload = {"config_path": str(config_path), "config_key": market}
        if explicit:
            payload["profile_path"] = str(profile_path)
        return payload, profile_path

    def execute(name, payload):
        assert name == "runtime_status"
        data, warnings, meta = runtime.runtime_status_tool(payload, **dependencies)
        return build_response(tool_name=name, ok=True, data=data, warnings=warnings, meta=meta)

    def inventory():
        return {str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in tmp_path.rglob("*") if p.is_file()}

    return prepare, execute, inventory, shared


@pytest.mark.parametrize("provider,market", [("systemd", "us"), ("launchd", "hk")])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("output", ["normal", "journal", "json"])
@pytest.mark.parametrize("result,last_status,expected_status,reason,healthy", [
    pytest.param(None, "ok", "unknown", "TICK_EVIDENCE_MISSING", False, id="fresh-last-run-no-tick"),
    pytest.param(None, "skipped", "unknown", "TICK_EVIDENCE_MISSING", False, id="skip-without-completion"),
    pytest.param({"status": "ok"}, "ok", "ok", "TICK_COMPLETED", True, id="completed"),
    pytest.param({"status": "ok"}, "skipped", "ok", "TICK_COMPLETED", True, id="completed-legal-skip"),
    pytest.param({"status": "failed", "error_code": "TICK_TIMEOUT"}, "ok", "failed", "TICK_TIMEOUT", False, id="failed"),
    pytest.param({"status": "evidence_incomplete"}, "ok", "evidence_incomplete", "TICK_EXEC_FAILED", False, id="partial"),
    pytest.param({"status": "unknown"}, "ok", "unknown", "TICK_EVIDENCE_MISSING", False, id="unknown"),
])
def test_real_status_cli_preserves_scheduled_tick_evidence(
    tick_runtime, capsys, provider, market, explicit, output, result, last_status, expected_status, reason, healthy,
):
    prepare, execute, inventory, _ = tick_runtime
    payload, _ = prepare(provider=provider, market=market, explicit=explicit, result=result, last_status=last_status)
    before = inventory()
    parser = argparse.ArgumentParser()
    add_runtime_observability_commands(parser.add_subparsers(dest="command"))
    argv = ["status", "--config-path", payload["config_path"], "--config-key", market]
    if explicit:
        argv += ["--profile-path", payload["profile_path"]]
    argv += {"normal": [], "journal": ["--journal-summary"], "json": ["--json"]}[output]
    envelopes = []

    def public(name, actual_payload):
        envelope = execute(name, actual_payload)
        envelopes.append(envelope)
        return envelope

    # A successful diagnostic query keeps exit 0, including degraded business state.
    assert handle_observability_command(parser.parse_args(argv), execute_tool_fn=public) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    data = envelopes[0]["data"]
    assert data["freshness"]["status"] == "fresh"
    assert data["summary"]["latest_status"] == last_status
    assert data["tick_health"]["scheduled"] is True
    assert data["tick_health"]["status"] == expected_status
    assert data["tick_health"]["reason_code"] == reason
    assert data["summary"]["ok"] is healthy
    assert data["summary"]["warning_codes"] == ([] if healthy else [reason])
    if output == "json":
        assert json.loads(captured.out) == envelopes[0]
    else:
        assert f"tick: scheduled=yes status={expected_status} reason={reason}" in captured.out
        assert ("overall: OK" in captured.out) is healthy
    assert inventory() == before


@pytest.mark.parametrize("case", ["absent", "empty", "market-only", "manual-service", "other-market", "unknown-provider"])
@pytest.mark.parametrize("provider", ["systemd", "launchd"])
def test_missing_tick_is_not_failure_without_matching_schedule(tick_runtime, case, provider):
    prepare, execute, inventory, _ = tick_runtime
    payload, path = prepare(provider=provider, scheduled=False)
    profile = json.loads(path.read_text())
    if case == "absent":
        path.unlink()
    else:
        if case == "market-only":
            profile.pop("services")
        elif case == "manual-service":
            profile["services"] = [{"name": "options-monitor-tick-us.service"}]
        elif case == "other-market":
            name = "options-monitor-tick-hk.timer" if provider == "systemd" else "com.options-monitor.tick-hk"
            profile["services"] = [{"name": name}]
        elif case == "unknown-provider":
            profile["service_provider"] = "manual"
            profile["services"] = [{"name": "options-monitor-tick-us.timer"}]
        path.write_text(json.dumps(profile), encoding="utf-8")
    before = inventory()
    envelope = execute("runtime_status", payload)
    data = envelope["data"]
    assert data["tick_health"] == {
        "status": "unknown", "reason_code": "TICK_EVIDENCE_MISSING", "market": "us", "scheduled": False,
    }
    assert data["summary"]["ok"] is True
    assert data["summary"]["warning_codes"] == []
    assert inventory() == before


def test_stale_unrelated_last_run_cannot_fill_missing_tick(tick_runtime):
    prepare, execute, _, shared = tick_runtime
    payload, _ = prepare()
    os.utime(shared / "last_run.json", (1, 1))
    data = execute("runtime_status", payload)["data"]
    assert data["freshness"]["status"] == "stale"
    assert data["tick_health"]["status"] == "unknown"
    assert data["summary"]["warning_codes"] == ["TICK_EVIDENCE_MISSING"]


@pytest.mark.parametrize("provider,unit_status,expected,reason", [
    ("systemd", "unknown", "unknown", "TICK_SERVICE_UNAVAILABLE"),
    ("systemd", "failed", "failed", "TICK_SERVICE_FAILED"),
    ("systemd", "ok", "ok", "TICK_COMPLETED"),
    ("launchd", "unknown", "ok", "TICK_COMPLETED"),
])
def test_optional_systemd_probe_does_not_probe_launchd(tick_runtime, monkeypatch, provider, unit_status, expected, reason):
    prepare, execute, _, _ = tick_runtime
    payload, _ = prepare(provider=provider, result={"status": "ok"})
    payload["include_service_status"] = True
    calls = []

    def probe(market):
        calls.append(market)
        return unit_status

    monkeypatch.setattr(runtime, "_tick_unit_execution_status", probe)
    monkeypatch.setattr(runtime, "_service_profile_summary", lambda _payload: {"loaded": True})
    data = execute("runtime_status", payload)["data"]
    assert calls == (["us"] if provider == "systemd" else [])
    assert data["tick_health"]["status"] == expected
    assert data["tick_health"]["reason_code"] == reason
    assert data["summary"]["ok"] is (expected == "ok")


@pytest.mark.parametrize("services", [["options-monitor-tick-us.timer"], [{"label": "options-monitor-tick-us.timer"}]])
def test_profile_service_names_follow_existing_name_label_string_contract(tick_runtime, services):
    prepare, execute, _, _ = tick_runtime
    payload, path = prepare()
    profile = json.loads(path.read_text())
    profile["services"] = services
    path.write_text(json.dumps(profile), encoding="utf-8")
    data = execute("runtime_status", payload)["data"]
    assert data["tick_health"]["scheduled"] is True
    assert data["summary"]["warning_codes"] == ["TICK_EVIDENCE_MISSING"]


@pytest.mark.parametrize("provider", ["systemd", "launchd"])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("scheduled", [False, True])
def test_status_uses_selected_profile_inventory_over_conflicting_hints(
    tick_runtime, monkeypatch, capsys, provider, explicit, scheduled,
):
    prepare, execute, inventory, shared = tick_runtime
    payload, selected_path = prepare(provider=provider, scheduled=scheduled, explicit=explicit)
    selected_profile = json.loads(selected_path.read_text())
    other_provider = "launchd" if provider == "systemd" else "systemd"
    other_name = "com.options-monitor.tick-us" if other_provider == "launchd" else "options-monitor-tick-us.timer"
    if explicit:
        # The default disk profile deliberately contradicts the explicit selection.
        disk_profile = dict(selected_profile, service_provider=other_provider,
                            services=[] if scheduled else [{"name": other_name}])
        (shared.parents[1] / "service.profile.json").write_text(json.dumps(disk_profile), encoding="utf-8")
    reads = []
    original_read = runtime._read_json_object

    def read(path):
        if path.name in {"service.profile.json", "explicit.profile.json"}:
            reads.append(path)
        return original_read(path)

    monkeypatch.setattr(runtime, "_read_json_object", read)
    # Existing request merging permits these hints, but they cannot rewrite the
    # selected file's configured Tick inventory for the applicability decision.
    hints = {"service_provider": other_provider,
             "services": [] if scheduled else [{"name": other_name}]}
    envelopes = []

    def public(name, actual_payload):
        envelope = execute(name, {**actual_payload, **hints})
        envelopes.append(envelope)
        return envelope

    parser = argparse.ArgumentParser()
    add_runtime_observability_commands(parser.add_subparsers(dest="command"))
    argv = ["status", "--config-path", payload["config_path"], "--config-key", "us"]
    if explicit:
        argv += ["--profile-path", payload["profile_path"]]
    before = inventory()
    assert handle_observability_command(parser.parse_args(argv), execute_tool_fn=public) == 0
    assert reads == [selected_path]  # Reuse the existing merge read; no Tick reread.
    data = envelopes[0]["data"]
    assert data["tick_health"]["scheduled"] is scheduled
    assert data["tick_health"]["status"] == "unknown"
    assert data["summary"]["ok"] is (not scheduled)
    assert data["summary"]["warning_codes"] == (["TICK_EVIDENCE_MISSING"] if scheduled else [])
    assert f"tick: scheduled={'yes' if scheduled else 'no'} status=unknown" in capsys.readouterr().out
    assert inventory() == before
