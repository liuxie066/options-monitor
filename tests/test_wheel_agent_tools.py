from pathlib import Path

import src.application.agent_tools.positions as position_tools


def test_wheel_end_agent_tool_previews_through_application_workflow(
    monkeypatch,
) -> None:
    repo = object()
    calls = []
    monkeypatch.setattr(
        position_tools,
        "_wheel_runtime",
        lambda _payload: (Path("config.us.json"), {}, repo, {"ledger_store": {}}),
    )

    def _end(active_repo, **kwargs):
        calls.append((active_repo, kwargs))
        return {"schema_version": "wheel_end_result.v1", "dry_run": True, "write_applied": False}

    monkeypatch.setattr(position_tools, "end_wheel_lifecycle", _end)
    data, warnings, _meta = position_tools.WHEEL_END_TOOL.call(
        {
            "config_key": "us",
            "account": "lx",
            "stock_lot_id": "assigned-stock-1",
            "expected_batch_generation_hash": "generation-1",
            "request_id": "request-1",
            "actor": "agent",
            "apply": False,
        }
    )

    assert data["dry_run"] is True
    assert warnings == []
    assert calls[0][0] is repo
    assert calls[0][1]["apply_changes"] is False


def test_wheel_agent_writes_are_requested_only_by_apply() -> None:
    assert position_tools.WHEEL_END_TOOL.is_write_requested({"apply": False, "confirm": True}) is False
    assert position_tools.WHEEL_END_TOOL.is_write_requested({"apply": True, "confirm": True}) is True
