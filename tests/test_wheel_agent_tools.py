from pathlib import Path

import pytest

import src.application.agent_tools.operations_impl as operations_impl
import src.application.agent_tools.positions as position_tools
import src.application.wheel.read_model as wheel_read_model
from src.application.agent_tool_contracts import AgentToolError
from domain.domain.ledger.events import TradeEvent
from domain.domain.ledger.identity import ContractKey
from domain.domain.ledger.position_fields import (
    build_open_adjustment_patch,
    strategy_metadata_fields_from_payload,
)
from domain.domain.ledger.projection_state import _resumable_open_event


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


def test_wheel_branch_agent_tool_resolves_alias_and_binds_preview(
    monkeypatch,
) -> None:
    repo = object()
    calls = []
    monkeypatch.setattr(
        position_tools,
        "_wheel_runtime",
        lambda _payload: (Path("config.us.json"), {}, repo, {"ledger_store": {}}),
    )
    monkeypatch.setattr(
        position_tools,
        "build_wheel_read_model",
        lambda *_args, **_kwargs: {
            "wheel_branches": [
                {
                    "wheel_branch_id": "wheel-call-1",
                    "direction": "call",
                    "stock_lot_id": "assigned-stock-1",
                }
            ]
        },
    )
    monkeypatch.setattr(
        position_tools,
        "resolve_wheel_config",
        lambda *_args, **_kwargs: {
            "market": "us",
            "activation_descriptor": {"generation": 1},
            "policy_sha256": "a" * 64,
        },
    )

    def _decide(active_repo, **kwargs):
        calls.append((active_repo, kwargs))
        return {"dry_run": True, "write_applied": False}

    monkeypatch.setattr(
        position_tools.wheel_application,
        "decide_wheel_branch",
        _decide,
    )
    data, warnings, _meta = position_tools.WHEEL_BRANCH_DECISION_TOOL.call(
        {
            "config_key": "us",
            "account": "lx",
            "action": "start",
            "stock_lot_id": "assigned-stock-1",
            "expected_branch_generation_hash": "branch-generation-1",
            "request_id": "request-1",
            "actor": "agent",
            "as_of_ms": 123,
            "apply": False,
        }
    )

    assert data["dry_run"] is True
    assert warnings == []
    assert calls[0][0] is repo
    assert calls[0][1] == {
        "account": "lx",
        "wheel_branch_id": "wheel-call-1",
        "decision": "start",
        "expected_branch_generation_hash": "branch-generation-1",
        "request_id": "request-1",
                    "actor": "agent",
                    "market": "us",
                    "apply_changes": False,
        "as_of_ms": 123,
        "market": "us",
        "activation_descriptor": {"generation": 1},
        "policy_sha256": "a" * 64,
    }


def test_wheel_branch_agent_tool_requires_exactly_one_identity() -> None:
    with pytest.raises(AgentToolError, match="Exactly one"):
        position_tools.WHEEL_BRANCH_DECISION_TOOL.call(
            {
                "config_key": "us",
                "account": "lx",
                "action": "end",
                "wheel_branch_id": "branch-1",
                "stock_lot_id": "lot-1",
                "expected_branch_generation_hash": "generation-1",
                "request_id": "request-1",
                "actor": "agent",
                "apply": False,
            }
        )


def test_legacy_wheel_call_linkage_reject_is_market_bound(monkeypatch) -> None:
    repo = object()
    read_calls = []
    monkeypatch.setattr(
        position_tools,
        "_wheel_runtime",
        lambda _payload: (Path("config.us.json"), {}, repo, {"ledger_store": {}}),
    )

    def _read(active_repo, account, instant, *, market=None):
        read_calls.append((active_repo, account, instant, market))
        return {"batches": [{"stock_lot_id": "assigned-stock-1"}]}

    monkeypatch.setattr(position_tools, "build_wheel_read_model", _read)
    monkeypatch.setattr(
        position_tools,
        "reject_wheel_call_linkage",
        lambda *_args, **_kwargs: {"dry_run": True, "write_applied": False},
    )

    position_tools.WHEEL_CALL_LINKAGE_TOOL.call(
        {
            "config_key": "us",
            "account": "lx",
            "action": "reject",
            "stock_lot_id": "assigned-stock-1",
            "expected_batch_generation_hash": "generation-1",
            "request_id": "request-1",
            "actor": "agent",
            "call_record_id": "call-lot-1",
            "linkage_candidate_id": "candidate-1",
            "expected_input_hash": "input-1",
            "reason": "not this batch",
            "as_of_ms": 123,
            "apply": False,
        }
    )

    assert read_calls == [(repo, "lx", 123, "us")]


def test_wheel_linkage_agent_put_preview_uses_canonical_branch(
    monkeypatch,
) -> None:
    repo = object()
    calls = []
    monkeypatch.setattr(
        position_tools,
        "_wheel_runtime",
        lambda _payload: (Path("config.us.json"), {}, repo, {"ledger_store": {}}),
    )
    monkeypatch.setattr(
        position_tools,
        "build_wheel_read_model",
        lambda *_args, **_kwargs: {
            "wheel_branches": [
                {
                    "wheel_branch_id": "wheel-put-1",
                    "direction": "put",
                    "stock_lot_id": None,
                }
            ]
        },
    )

    def _reject(active_repo, **kwargs):
        calls.append((active_repo, kwargs))
        return {"dry_run": True, "write_applied": False}

    monkeypatch.setattr(position_tools, "reject_wheel_linkage", _reject)
    data, warnings, _meta = position_tools.WHEEL_LINKAGE_TOOL.call(
        {
            "config_key": "us",
            "account": "lx",
            "action": "reject",
            "direction": "put",
            "wheel_branch_id": "wheel-put-1",
            "expected_branch_generation_hash": "generation-1",
            "request_id": "request-1",
            "actor": "agent",
            "option_record_id": "put-lot-1",
            "linkage_candidate_id": "candidate-1",
            "expected_input_hash": "input-1",
            "reason": "not this cycle",
            "as_of_ms": 123,
            "apply": False,
        }
    )

    assert data["dry_run"] is True
    assert warnings == []
    assert calls == [
        (
            repo,
            {
                "account": "lx",
                "wheel_branch_id": "wheel-put-1",
                "direction": "put",
                "expected_branch_generation_hash": "generation-1",
                "request_id": "request-1",
                "actor": "agent",
                "market": "us",
                "apply_changes": False,
                "as_of_ms": 123,
                "option_record_id": "put-lot-1",
                "linkage_candidate_id": "candidate-1",
                "expected_input_hash": "input-1",
                "reason": "not this cycle",
            },
        )
    ]


def test_wheel_neutral_agent_rejects_put_stock_lot_alias() -> None:
    with pytest.raises(AgentToolError, match="Call-only alias"):
        position_tools.WHEEL_INTENT_TOOL.call(
            {
                "config_key": "us",
                "account": "lx",
                "action": "cancel",
                "direction": "put",
                "stock_lot_id": "legacy-lot",
                "expected_branch_generation_hash": "generation-1",
                "request_id": "request-1",
                "actor": "agent",
                "intent_id": "intent-1",
                "reason": "cancelled",
                "broker_order_inactive_confirmed": True,
                "apply": False,
            }
        )


@pytest.mark.parametrize(
    ("tool", "payload"),
    (
        (
            position_tools.WHEEL_BRANCH_DECISION_TOOL,
            {
                "config_key": "us",
                "account": "lx",
                "action": "end",
                "wheel_branch_id": "branch-1",
                "expected_branch_generation_hash": "generation-1",
                "request_id": "request-1",
                "actor": "agent",
                "apply": True,
            },
        ),
        (
            position_tools.WHEEL_ACTIVATION_TOOL,
            {
                "market": "us",
                "account": "lx",
                "action": "enable",
                "expected_current_generation": 0,
                "request_id": "request-1",
                "actor": "agent",
                "apply": True,
            },
        ),
    ),
)
def test_wheel_agent_apply_requires_confirmation(tool, payload) -> None:
    with pytest.raises(AgentToolError, match="confirm=true"):
        tool.call(payload)


def test_wheel_activation_agent_status_and_enable_preview(monkeypatch) -> None:
    repo = object()
    calls = []
    monkeypatch.setattr(
        position_tools,
        "_wheel_runtime",
        lambda _payload: (Path("config.us.json"), {}, repo, {"ledger_store": {}}),
    )
    monkeypatch.setattr(
        position_tools,
        "build_wheel_policy_hash",
        lambda *_args, **_kwargs: "b" * 64,
    )

    def _change(active_repo, **kwargs):
        calls.append((active_repo, kwargs))
        return {"dry_run": True, "write_applied": False}

    monkeypatch.setattr(
        position_tools.wheel_application,
        "change_wheel_activation",
        _change,
    )

    status, _, _ = position_tools.WHEEL_ACTIVATION_TOOL.call(
        {"market": "us", "account": "lx", "action": "status"}
    )
    enable, _, _ = position_tools.WHEEL_ACTIVATION_TOOL.call(
        {
            "market": "us",
            "account": "lx",
            "action": "enable",
            "expected_current_generation": 0,
            "request_id": "enable-1",
            "actor": "agent",
            "apply": False,
        }
    )

    assert status["dry_run"] is True
    assert enable["dry_run"] is True
    assert calls == [
        (repo, {"action": "status", "market": "us", "account": "lx"}),
        (
            repo,
            {
                "action": "enable",
                "market": "us",
                "account": "lx",
                "expected_current_generation": 0,
                "request_id": "enable-1",
                "actor": "agent",
                "policy_sha256": "b" * 64,
                "apply_changes": False,
            },
        ),
    ]
    assert position_tools.WHEEL_ACTIVATION_TOOL.is_write_requested(
        {"action": "status", "apply": True}
    ) is False
    assert position_tools.WHEEL_ACTIVATION_TOOL.is_write_requested(
        {"action": "enable", "apply": True}
    ) is True


def test_wheel_activation_agent_rejects_market_config_mismatch() -> None:
    with pytest.raises(AgentToolError, match="config_key must match market"):
        position_tools.WHEEL_ACTIVATION_TOOL.call(
            {
                "config_key": "hk",
                "market": "us",
                "account": "lx",
                "action": "status",
            }
        )


def test_wheel_read_model_v2_preserves_legacy_batches_and_adds_branches(
    monkeypatch,
) -> None:
    legacy_batch = {
        "account": "lx",
        "symbol": "NVDA",
        "stock_lot_id": "assigned-stock-1",
        "phase": "call_open",
        "projection_hash": "legacy-hash",
    }
    put_branch = {
        "account": "lx",
        "symbol": "NVDA",
        "wheel_branch_id": "wheel-put:1",
        "parent_branch_id": "assigned-stock-1",
        "direction": "put",
        "stock_lot_id": None,
        "phase": "pending_decision",
        "projection_hash": "branch-hash",
    }
    monkeypatch.setattr(
        wheel_read_model,
        "build_assigned_stock_projection_from_rows",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        wheel_read_model,
        "project_wheel_lifecycles",
        lambda *_args, **_kwargs: [legacy_batch],
    )
    monkeypatch.setattr(
        wheel_read_model,
        "project_wheel_branches",
        lambda *_args, **_kwargs: [
            {
                **legacy_batch,
                "wheel_branch_id": "assigned-stock-1",
                "parent_branch_id": None,
                "direction": "call",
                "phase": "option_open",
            },
            put_branch,
        ],
    )
    monkeypatch.setattr(
        wheel_read_model,
        "project_wheel_linkage_candidates",
        lambda *_args, **_kwargs: [],
    )

    result = wheel_read_model.build_wheel_read_model_from_rows(
        {
            "account_wheel_events": [],
            "trade_events": [],
            "account_position_lots": [],
        },
        account="lx",
        as_of_ms=1,
    )

    assert result["schema_version"] == "wheel_read_model.v2"
    assert len(result["batches"]) == 1
    assert result["batches"][0]["stock_lot_id"] == "assigned-stock-1"
    assert result["batches"][0]["direction"] == "call"
    assert "batch_generation_hash" in result["batches"][0]
    assert result["wheel_branches"][0]["phase"] == "option_open"
    assert result["wheel_branches"][1] == put_branch


def test_assigned_stock_output_v3_includes_branch_without_stock_row(
    monkeypatch,
    tmp_path: Path,
) -> None:
    put_branch = {
        "account": "lx",
        "symbol": "NVDA",
        "wheel_branch_id": "wheel-put:1",
        "direction": "put",
        "stock_lot_id": None,
        "phase": "pending_decision",
    }
    monkeypatch.setattr(
        operations_impl,
        "build_assigned_stock_view",
        lambda *_args, **_kwargs: {"assigned_stock_lots": []},
    )
    monkeypatch.setattr(
        operations_impl,
        "build_wheel_read_model",
        lambda *_args, **_kwargs: {
            "batches": [],
            "wheel_branches": [put_branch],
        },
    )

    result = operations_impl._assigned_stock_action(
        object(),
        {"account": "lx", "refresh_quotes": False},
        cfg={"accounts": ["lx"]},
        repo_base=lambda: tmp_path,
        quote_state_base_dir=None,
        normalize_broker=lambda value: str(value or "").strip(),
        normalize_account=lambda value: str(value or "").strip().lower(),
        refresh_assigned_stock_quotes=lambda *_args, **_kwargs: None,
    )

    assert result["schema_version"] == "option_positions_read.output.v3"
    assert result["rows"] == []
    assert result["row_count"] == 0
    assert result["wheel_branches"] == [put_branch]


def test_source_wheel_branch_id_survives_patch_and_resumable_projection() -> None:
    branch_id = "wheel-put:1"
    assert strategy_metadata_fields_from_payload(
        {"source_wheel_branch_id": branch_id}
    ) == {"source_wheel_branch_id": branch_id}
    patch = build_open_adjustment_patch(
        {
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "status": "open",
            "contracts": 1,
            "contracts_closed": 0,
            "strike": 100,
            "multiplier": 100,
        },
        source_wheel_branch_id=branch_id,
        as_of_ms=2,
    )
    assert patch["source_wheel_branch_id"] == branch_id

    event = TradeEvent(
        event_id="wheel-put-open-1",
        event_type="open",
        event_time_ms=1,
        contract_key=ContractKey.from_values(
            broker="富途",
            account="lx",
            underlying_symbol="NVDA",
            option_type="put",
            position_side="short",
            strike=100,
            expiration_ymd="2027-01-15",
        ),
        contracts=1,
        price=1.0,
        currency="USD",
        source="manual",
        multiplier=100,
        raw_payload={
            "strategy": "wheel",
            "leg_role": "wheel_put",
            "source_wheel_branch_id": branch_id,
        },
    )
    assert _resumable_open_event(event).raw_payload["source_wheel_branch_id"] == branch_id
