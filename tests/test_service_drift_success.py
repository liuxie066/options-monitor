from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from src.application.service_drift import (
    _drift_summary, _service_drift_apply_failed, _summary_with_apply_errors,
)
from src.application.service_upgrade import (
    ServiceTransitionError, _reconcile_services_from_current_release, _service_reconcile_failed,
)
from src.interfaces.cli.service_ops import add_service_update_commands, handle_service_update_command


def _summary(**changes):
    facts = dict(
        expected_services=["options-monitor-tick-us.timer"],
        missing_required_units=[], missing_profile_units=[], missing_installed_units=[],
        extra_profile_units=[], extra_installed_units=[], extra_managed_units=[],
        extra_managed_files=[], mismatched_units=[], missing_managed_files=[],
        mismatched_managed_files=[], mode_mismatched_managed_files=[],
        activation_drift_units=[], preserved_activation_units=[],
        activation_preservation_conflicts=[], execution_drift_units=[],
        profile_content_changed=False, compatibility_warning_count=0,
    )
    return _drift_summary(**(facts | changes))


@pytest.mark.parametrize("facts", [
    {"extra_installed_units": ["options-monitor-custom.service"]},
    {"compatibility_warning_count": 1},
])
def test_information_warnings_succeed_consistently(facts):
    summary = _summary(**facts)
    result = {"summary": summary, "apply_errors": []}
    assert summary["status"] == "warn"
    assert summary["error_count"] == 0
    assert summary["warning_count"] > 0
    assert summary["ok"] is True
    assert _service_drift_apply_failed(result) is False
    assert _service_reconcile_failed(result) is False


@pytest.mark.parametrize("category", [
    "missing_required_units", "missing_profile_units", "missing_installed_units",
    "extra_profile_units", "extra_managed_units", "extra_managed_files",
    "mismatched_units", "missing_managed_files", "mismatched_managed_files",
    "mode_mismatched_managed_files", "activation_drift_units",
    "activation_preservation_conflicts", "execution_drift_units", "profile_content_changed",
])
def test_every_unresolved_drift_category_blocks(category):
    facts = {category: True if category == "profile_content_changed" else ["unit"]}
    summary = _summary(**facts)
    result = {"summary": summary, "apply_errors": []}
    assert summary["status"] == "error"
    assert summary["error_count"] > 0
    assert summary["ok"] is False
    assert _service_drift_apply_failed(result) is True
    assert _service_reconcile_failed(result) is True


def test_apply_errors_override_information_success():
    summary = _summary(extra_installed_units=["custom.service"])
    failed = _summary_with_apply_errors(summary, ["reload failed"])
    result = {"summary": failed, "apply_errors": ["reload failed"]}
    assert failed["ok"] is False
    assert failed["status"] == "error"
    assert failed["apply_error_count"] == 1
    assert failed["warning_count"] == summary["warning_count"]
    assert _service_drift_apply_failed(result) is True
    assert _service_reconcile_failed(result) is True


@pytest.mark.parametrize("summary, expected_ok", [
    ({"ok": True, "status": "warn"}, True),
    ({"ok": False, "status": "warn"}, False),
    ({"status": "ok"}, False),
    ({"ok": "true", "status": "ok"}, False),
])
def test_consumers_use_owner_boolean_not_status(tmp_path, summary, expected_ok):
    result = {"summary": summary, "apply_errors": []}
    assert _service_drift_apply_failed(result) is (not expected_ok)
    assert _service_reconcile_failed(result) is (not expected_ok)
    parser = argparse.ArgumentParser()
    add_service_update_commands(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["service", "drift", "--runtime-root", str(tmp_path)])
    response = handle_service_update_command(args, service_drift_fn=lambda **_: result)
    assert response["ok"] is expected_ok
    exit_code = 0 if expected_ok else 2

    def child(command, **_kwargs):
        return subprocess.CompletedProcess(command, exit_code, stdout=json.dumps(response), stderr="")

    kwargs = dict(repo_link=tmp_path, target_dir=tmp_path, runtime=tmp_path,
                  activation_policy="ensure-active", run_cmd=child)
    if expected_ok:
        reconciled = _reconcile_services_from_current_release(**kwargs)
        assert _service_reconcile_failed(reconciled) is False
        assert reconciled["summary"]["status"] == "warn"
    else:
        with pytest.raises(ServiceTransitionError):
            _reconcile_services_from_current_release(**kwargs)


@pytest.fixture
def runtime_drift_query(tmp_path, monkeypatch):
    from src.application import service_drift as drift_owner
    from src.application.agent_tools import diagnostics, runtime_status_impl as status_owner
    from src.application.settings import EffectiveEnv
    from src.application.tool_execution import execute_tool

    root = tmp_path / "runtime"
    shared = root / "output_shared" / "state"
    (shared / "current").mkdir(parents=True)
    config_path = root / "config.us.json"
    data_path = root / "portfolio.runtime.json"
    env_path = root / "empty.env"
    config = {
        "accounts": ["fixture"],
        "portfolio": {"data_config": str(data_path)},
        "notifications": {},
        "trade_intake": {"enabled": False},
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    data_path.write_text("{}", encoding="utf-8")
    env_path.write_text("", encoding="utf-8")
    (shared / "last_run.json").write_text(
        json.dumps({"status": "ok", "no_send": True}), encoding="utf-8",
    )
    (shared / "current" / "tick_cron_last_result.us.current.json").write_text(
        json.dumps({"status": "ok"}), encoding="utf-8",
    )
    for name, path in {
        "OM_ENV_FILE": env_path,
        "OM_DATA_CONFIG": data_path,
        "OM_RUNTIME_ROOT": root,
        "OM_SYSTEMD_UNIT_ROOT": root / "units",
    }.items():
        monkeypatch.setenv(name, str(path))

    def load_config(*, config_key, config_path: str, require_identity):
        assert config_key == "us"
        assert Path(config_path) == root / "config.us.json"
        assert require_identity is False
        return Path(config_path), config

    def ledger_store(path):
        assert path == data_path
        return {"runtime_root": str(root)}

    def effective_env(*, repo_root, env_file, mask_path):
        assert repo_root == root
        assert env_file is None
        return (
            EffectiveEnv(values={}, sources={}, env_file=env_path, env_file_loaded=True),
            {"entries": {}, "warnings": [], "env_file_loaded": True},
        )

    monkeypatch.setattr(diagnostics, "repo_base", lambda: root)
    monkeypatch.setattr(diagnostics, "load_runtime_config", load_config)
    monkeypatch.setattr(status_owner, "ledger_store_payload", ledger_store)
    monkeypatch.setattr(status_owner, "build_wheel_activation_readiness", lambda **_: {})
    monkeypatch.setattr(status_owner, "build_effective_env_with_status", effective_env)
    monkeypatch.setattr(status_owner, "_bot_runtime_summary", lambda **_: {
        "config": {"enabled": False}, "llm": {"enabled": False}, "audit": {},
    })
    monkeypatch.setattr(status_owner, "build_channel_status", lambda **_: {"channels": {}})
    monkeypatch.setattr(status_owner, "_config_authority_payload", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(status_owner, "system_alert_delivery_status", lambda *_args, **_kwargs: {"status": "ok"})

    forbidden_calls = []

    def forbidden(name):
        def fail(*_args, **_kwargs):
            forbidden_calls.append(name)
            raise AssertionError(f"unexpected I/O boundary: {name}")
        return fail

    for name in (
        "preview_trade_intake_reconciliation_from_sqlite",
        "resolve_notification_delivery_route", "resolve_secret_status",
        "service_status_from_profile", "_tick_unit_execution_status",
    ):
        monkeypatch.setattr(status_owner, name, forbidden(name))
    for name in ("_expected_bundle_from_profile", "_effective_profile_with_legacy_feishu_credential"):
        monkeypatch.setattr(drift_owner, name, forbidden(name))
    for module in (status_owner, drift_owner):
        monkeypatch.setattr(module, "subprocess", SimpleNamespace(
            run=forbidden(f"{module.__name__}.subprocess.run"),
            TimeoutExpired=subprocess.TimeoutExpired,
        ))

    payload = {
        "config_key": "us", "config_path": str(config_path), "accounts": ["fixture"],
        "report_dir": str(root / "output_shared" / "reports"),
        "state_dir": str(shared), "shared_state_dir": str(shared),
        "accounts_root": str(root / "output_accounts"), "runs_root": str(root / "output_runs"),
        "include_service_status": False,
    }

    def inventory():
        return {
            path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
            for path in root.rglob("*")
        }

    def query(drift_result=None):
        before = inventory()
        config_before = deepcopy(config)
        observed = []

        def upstream(**kwargs):
            assert kwargs == {
                "repo_root": None, "runtime_root": root, "profile_path": None, "profile": None,
            }
            result = drift_owner.service_drift_status(**kwargs) if drift_result is None else drift_result
            observed.append((result, deepcopy(result)))
            return result

        monkeypatch.setattr(status_owner, "service_drift_status", upstream)
        envelope = execute_tool("runtime_status", payload)
        assert forbidden_calls == []
        assert inventory() == before
        assert config == config_before
        assert len(observed) == 1
        result, result_before = observed[0]
        assert result == result_before
        return envelope, result

    return root, query


def _assert_runtime_drift_result(envelope, source, *, warning_code=None):
    from src.application.runtime_status_cli import (
        format_runtime_status_journal_summary, format_runtime_status_summary,
    )

    assert envelope["ok"] is True
    assert envelope["error"] is None
    data = envelope["data"]
    codes = [warning_code] if warning_code else []
    assert data["summary"]["ok"] is (warning_code is None)
    assert data["summary"]["warning_codes"] == codes
    assert data["summary"]["warning_count"] == len(codes)
    assert envelope["warnings"] == [f"runtime_status:{code}" for code in codes]
    owner_summary = source["summary"]
    public_summary = data["service_drift"]["summary"]
    assert public_summary == {
        **{key: owner_summary[key] for key in ("ok", "status", "warning_count", "error_count")
           if key in owner_summary},
        "missing_installed_count": len(source.get("missing_installed_units") or []),
        "missing_required_count": len(source.get("missing_required_units") or []),
    }
    overall = "OK" if warning_code is None else "FAIL"
    formatted = [format_runtime_status_summary(envelope), format_runtime_status_journal_summary(envelope)]
    for output in formatted:
        assert f"overall: {overall}" in output
        assert f"service drift: status={owner_summary['status']}" in output
    return json.dumps({"data": data, "warnings": envelope["warnings"]}), formatted


@pytest.mark.parametrize("facts, owner_ok, owner_status, count", [
    pytest.param({}, True, "ok", 0, id="clean"),
    pytest.param({"mismatched_units": ["PRIVATE_DRIFT_UNIT"]}, False, "error", 1, id="mismatched-unit"),
    pytest.param({"missing_managed_files": ["PRIVATE_DRIFT_FILE"]}, False, "error", 1, id="missing-managed-file"),
    pytest.param({"execution_drift_units": ["PRIVATE_DRIFT_UNIT"]}, False, "error", 1, id="execution-drift"),
    pytest.param({"extra_installed_units": ["PRIVATE_DRIFT_UNIT"]}, True, "warn", 1, id="extra-unowned-unit"),
    pytest.param({"compatibility_warning_count": 1}, True, "warn", 1, id="compatibility-warning"),
])
def test_runtime_status_retains_canonical_drift(runtime_drift_query, facts, owner_ok, owner_status, count):
    root, query = runtime_drift_query
    summary = _summary(**facts)
    assert summary["ok"] is owner_ok
    assert summary["status"] == owner_status
    assert summary["warning_count"] == count
    assert summary["error_count"] == (0 if owner_ok else 1)
    envelope, source = query({**facts, "summary": summary, "profile_path": str(root / "private-profile.json")})
    public_json, formatted = _assert_runtime_drift_result(
        envelope, source, warning_code="SERVICE_DRIFT" if count else None,
    )
    for output in [public_json, *formatted]:
        assert "PRIVATE_DRIFT" not in output
        assert str(root / "private-profile.json") not in output


@pytest.mark.parametrize("summary", [
    pytest.param({"ok": False, "status": "ok"}, id="false"),
    pytest.param({"status": "ok"}, id="missing"),
    pytest.param({"ok": "true", "status": "ok"}, id="string"),
    pytest.param({"ok": 1, "status": "ok"}, id="numeric"),
])
def test_runtime_status_requires_boolean_drift_success(runtime_drift_query, summary):
    # Invalid owner-success controls are distinct from canonical producer cases above.
    _root, query = runtime_drift_query
    envelope, source = query({"summary": summary})
    _assert_runtime_drift_result(envelope, source, warning_code="SERVICE_DRIFT")


@pytest.mark.parametrize("summary_has_required", [
    pytest.param(True, id="summary"), pytest.param(False, id="top-level-fallback"),
])
def test_runtime_status_required_drift_warning_takes_priority(runtime_drift_query, summary_has_required):
    _root, query = runtime_drift_query
    missing = ["PRIVATE_REQUIRED_UNIT"]
    summary = _summary(missing_required_units=missing)
    if not summary_has_required:
        summary.pop("missing_required_units")
    envelope, source = query({"summary": summary, "missing_required_units": missing})
    public_json, formatted = _assert_runtime_drift_result(
        envelope, source, warning_code="SERVICE_DRIFT_REQUIRED_UNIT_MISSING",
    )
    assert envelope["data"]["service_drift"]["summary"]["missing_required_count"] == 1
    for output in [public_json, *formatted]:
        assert "PRIVATE_REQUIRED_UNIT" not in output


@pytest.mark.parametrize("provider, profile, reason", [
    pytest.param("", {}, "service_profile_missing", id="missing-profile"),
    pytest.param("unsupported", {}, "unsupported_service_provider", id="unsupported-provider"),
    pytest.param("systemd", {"services": []}, "service_profile_has_no_services", id="empty-services"),
])
def test_runtime_status_keeps_actual_skipped_drift_healthy(runtime_drift_query, provider, profile, reason):
    from src.application.service_drift import _build_drift

    root, query = runtime_drift_query
    source = _build_drift({
        "profile": profile, "profile_on_disk": profile, "provider": provider,
        "profile_path": root / "service.profile.json", "repo_root": root, "runtime_root": root,
        "systemd_unit_root": root / "units", "activation_policy": "ensure-active",
        "preserved_activation_states": {},
    })
    assert source["reason"] == reason
    assert source["summary"] == {"ok": True, "status": "skipped", "error_count": 0, "warning_count": 0}
    envelope, source = query(source)
    _assert_runtime_drift_result(envelope, source)
    assert envelope["data"]["service_drift"]["reason"] == reason


def test_runtime_status_retains_real_drift_check_exception(runtime_drift_query, monkeypatch):
    from src.application import service_drift as drift_owner

    root, query = runtime_drift_query
    calls = []
    private_path = root / "PRIVATE_DRIFT_EXCEPTION"

    def fail_before_io(**kwargs):
        calls.append(kwargs)
        assert kwargs["runtime_root"] == root
        assert kwargs["repo_root"] is None
        assert kwargs["profile_path"] is None
        assert kwargs["profile"] is None
        raise PermissionError(f"synthetic unreadable profile: {private_path}")

    monkeypatch.setattr(drift_owner, "_load_profile_and_paths", fail_before_io)
    envelope, source = query()
    assert len(calls) == 1
    assert source["reason"] == "service_drift_check_failed"
    assert source["error"] == f"PermissionError: synthetic unreadable profile: {private_path}"
    assert source["summary"]["ok"] is False
    assert source["summary"]["status"] == "error"
    assert source["summary"]["error_count"] == 1
    public_json, formatted = _assert_runtime_drift_result(envelope, source, warning_code="SERVICE_DRIFT")
    assert envelope["data"]["service_drift"]["reason"] == "service_drift_check_failed"
    for output in [public_json, *formatted]:
        assert "PRIVATE_DRIFT_EXCEPTION" not in output
        assert "PermissionError" not in output
        assert str(private_path) not in output
