from __future__ import annotations

from src.application.write_contract import attach_write_contract, write_control


def test_write_control_defaults_to_dry_run() -> None:
    out = write_control()

    assert out["dry_run"] is True
    assert out["write_requested"] is False
    assert out["confirmation_required"] is False


def test_write_control_allows_apply_for_local_writes() -> None:
    out = write_control(apply=True, high_risk=False)

    assert out["dry_run"] is False
    assert out["write_requested"] is True
    assert out["confirmation_required"] is False


def test_write_control_requires_confirm_for_high_risk_apply() -> None:
    out = write_control(apply=True, high_risk=True)

    assert out["dry_run"] is True
    assert out["write_requested"] is False
    assert out["confirmation_required"] is True


def test_write_control_yes_confirms_high_risk_write() -> None:
    out = write_control(yes=True, high_risk=True)

    assert out["dry_run"] is False
    assert out["write_requested"] is True
    assert out["explicitly_confirmed"] is True
    assert out["yes"] is True


def test_attach_write_contract_adds_standard_fields() -> None:
    out = attach_write_contract(
        {"ok": True},
        dry_run=False,
        write_applied=True,
        backup_path="/tmp/backup",
        audit_id="audit_test",
        rollback_hint="restore backup",
    )

    assert out == {
        "ok": True,
        "dry_run": False,
        "write_applied": True,
        "backup_path": "/tmp/backup",
        "audit_id": "audit_test",
        "rollback_hint": "restore backup",
    }
