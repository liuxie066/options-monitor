from src.application.agent_tools.runtime_status_impl import _ledger_context_summary


def test_ledger_context_distinguishes_missing_from_unreadable() -> None:
    assert _ledger_context_summary({"exists": False}) == {
        "available": False,
        "status": "not_generated",
        "reason": "context_artifact_missing",
        "fail_closed": False,
    }
    assert _ledger_context_summary({"exists": True, "is_file": True, "read_error": "denied"}) == {
        "available": False,
        "status": "read_failed",
        "reason": "context_artifact_unreadable",
        "fail_closed": False,
    }
