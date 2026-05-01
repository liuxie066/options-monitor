from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_state_repo_normalize_audit_event_requires_minimal_fields() -> None:
    from domain.storage.repositories.state_repo import normalize_audit_event

    out = normalize_audit_event(
        {
            "event_type": "tool_call",
            "action": "scan_scheduler",
            "status": "ok",
            "run_id": "r-1",
            "idempotency_key": "k-1",
            "extra": {"returncode": 0},
        }
    )
    assert out["schema_kind"] == "audit_event"
    assert out["schema_version"] == "1.0"
    assert out["event_type"] == "tool_call"
    assert out["action"] == "scan_scheduler"


def test_multi_tick_main_has_phase3_idempotency_and_audit_hooks() -> None:
    src = (BASE / "scripts" / "multi_tick" / "main.py").read_text(encoding="utf-8")
    assert "scope='tick_execution'" in src
    assert "append_audit_event" in src
    assert "execution_idempotency_key" in src


def test_state_repo_has_current_read_model_writers() -> None:
    src = (BASE / "domain" / "storage" / "repositories" / "state_repo.py").read_text(encoding="utf-8")
    assert "shared_current_read_model_dir" in src
    assert "write_shared_current_read_model" in src
    assert "tick_metrics.current.json" in src


def main() -> None:
    test_state_repo_normalize_audit_event_requires_minimal_fields()
    test_multi_tick_main_has_phase3_idempotency_and_audit_hooks()
    test_state_repo_has_current_read_model_writers()
    print("OK (phase3-audit-idempotency-hooks)")


if __name__ == "__main__":
    main()
