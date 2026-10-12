from __future__ import annotations

import argparse

import pytest

from src.application.service_drift import (
    _drift_summary, _service_drift_apply_failed, _summary_with_apply_errors,
)
from src.application.service_upgrade import _service_reconcile_failed
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
