from pathlib import Path

import pytest

import src.application.agent_tools.operations_impl as operations_impl
import src.application.agent_tools.positions as position_tools
import src.application.config_authoring_transaction as config_transaction
import src.application.wheel.read_model as wheel_read_model
from src.application.agent_tool_contracts import AgentToolError
from src.application.tool_execution import execute_tool
from domain.domain.ledger.events import TradeEvent
from domain.domain.ledger.identity import ContractKey
from domain.domain.ledger.position_fields import (
    build_open_adjustment_patch,
    strategy_metadata_fields_from_payload,
)
from domain.domain.ledger.projection_state import _resumable_open_event
from tests.test_wheel_cli import (
    _activation_environment,
    _deployment_file_bytes,
    _malformed_activation_status_environment,
    _prepare_activation_storage_case,
)


def _activation_payload(
    action: str,
    *,
    runtime: Path,
    data_config: Path,
    runtime_root: Path,
    source_sha: str | None = None,
    apply: bool = False,
) -> dict:
    payload = {
        "market": "us",
        "account": "lx",
        "action": action,
        "config_path": str(runtime),
        "data_config": str(data_config),
        "runtime_root": str(runtime_root),
    }
    if action == "status":
        return payload
    payload.update(
        expected_current_generation=0,
        request_id="agent-enable-1",
        actor="agent",
        apply=apply,
    )
    if source_sha:
        payload["expected_source_sha256"] = source_sha
    if apply:
        payload["confirm"] = True
    return payload


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
                "expected_source_sha256": "a" * 64,
                "apply": True,
            },
        ),
    ),
)
def test_wheel_agent_apply_requires_confirmation(tool, payload) -> None:
    with pytest.raises(AgentToolError, match="confirm=true"):
        tool.call(payload)


def test_wheel_activation_agent_status_and_enable_preview(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        position_tools,
        "_wheel_runtime",
        lambda _payload: pytest.fail("activation must use the shared facade"),
    )

    def _change(**kwargs):
        calls.append(kwargs)
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
        {
            "repo_root": position_tools.repo_base(),
            "action": "status",
            "market": "us",
            "account": "lx",
            "config_path": None,
            "config_key": "us",
            "data_config": None,
            "runtime_root": None,
            "expected_current_generation": None,
            "request_id": None,
            "actor": None,
            "expected_source_sha256": None,
            "apply_changes": False,
        },
        {
            "repo_root": position_tools.repo_base(),
            "action": "enable",
            "market": "us",
            "account": "lx",
            "config_path": None,
            "config_key": "us",
            "data_config": None,
            "runtime_root": None,
            "expected_current_generation": 0,
            "request_id": "enable-1",
            "actor": "agent",
            "expected_source_sha256": None,
            "apply_changes": False,
        },
    ]
    assert position_tools.WHEEL_ACTIVATION_TOOL.is_write_requested(
        {"action": "status", "apply": True}
    ) is False
    assert position_tools.WHEEL_ACTIVATION_TOOL.is_write_requested(
        {"action": "enable", "apply": True}
    ) is True


def test_wheel_activation_agent_apply_requires_preview_source_sha() -> None:
    with pytest.raises(AgentToolError, match="expected_source_sha256"):
        position_tools.WHEEL_ACTIVATION_TOOL.call(
            {
                "market": "us",
                "account": "lx",
                "action": "enable",
                "expected_current_generation": 0,
                "request_id": "enable-1",
                "actor": "agent",
                "apply": True,
                "confirm": True,
            }
        )


def test_wheel_activation_agent_manifest_declares_all_writes() -> None:
    assert position_tools.WHEEL_ACTIVATION_TOOL.side_effects == (
        "writes_wheel_activation_window",
        "writes_config_yaml",
        "publishes_generated_runtime_configs",
    )
    assert "expected_source_sha256" in position_tools.WHEEL_ACTIVATION_TOOL.input_schema
    facts = position_tools.WHEEL_ACTIVATION_TOOL.output_contract["fact_fields"]
    for field in (
        "expected_source_sha256",
        "paths",
        "window_receipt",
        "config_audit",
        "readiness",
        "source_status",
        "storage_status",
        "pending_authoring_journal",
        "original_request",
        "recovered_transactions",
        "failure_phase",
        "retry_hint",
    ):
        assert field in facts


def test_wheel_activation_agent_preserves_workflow_error_details(monkeypatch) -> None:
    error = AgentToolError(
        code="WHEEL_ACTIVATION_INCOMPLETE",
        message="runtime readback failed",
        details={"failure_phase": "readback", "write_applied": True},
    )
    monkeypatch.setattr(
        position_tools.wheel_application,
        "change_wheel_activation",
        lambda **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(AgentToolError) as raised:
        position_tools.WHEEL_ACTIVATION_TOOL.call(
            {"market": "us", "account": "lx", "action": "status"}
        )

    assert raised.value is error
    assert raised.value.details == {
        "failure_phase": "readback",
        "write_applied": True,
    }


def test_wheel_activation_agent_public_entry_applies_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _source, runtime, data_config, _sqlite_path = _activation_environment(tmp_path)
    preview_response = execute_tool(
        "wheel_activation",
        _activation_payload(
            "enable",
            runtime=runtime,
            data_config=data_config,
            runtime_root=tmp_path,
        )
    )
    assert preview_response["ok"] is True
    preview = preview_response["data"]
    monkeypatch.setenv("OM_AGENT_ENABLE_WRITE_TOOLS", "true")
    applied_response = execute_tool(
        "wheel_activation",
        _activation_payload(
            "enable",
            runtime=runtime,
            data_config=data_config,
            runtime_root=tmp_path,
            source_sha=preview["expected_source_sha256"],
            apply=True,
        )
    )
    assert applied_response["ok"] is True
    applied = applied_response["data"]

    assert applied["status"] == "applied"
    assert applied["ready"] is True
    assert applied["window_receipt"]["write_applied"] is True
    assert applied["config_audit"]["write_applied"] is True
    assert applied["paths"]["config_path"] == str(runtime)
    assert applied_response["meta"] == {
        "repo_base": position_tools._mask_path_str(position_tools.repo_base())
    }


@pytest.mark.parametrize(
    ("case", "expected_status", "storage_status"),
    [
        ("missing_database", "unavailable", "missing_database"),
        ("missing_table", "unavailable", "missing_table"),
        ("unreadable", "unavailable", "unreadable"),
        ("available_no_window", "no_window", "available"),
        ("open", "open", "available"),
        ("closed", "closed", "available"),
    ],
)
def test_wheel_activation_agent_public_status_distinguishes_storage_and_window_state(
    case: str,
    expected_status: str,
    storage_status: str,
    tmp_path: Path,
) -> None:
    _source, runtime, data_config, sqlite_path = _prepare_activation_storage_case(
        tmp_path,
        case,
    )

    response = execute_tool(
        "wheel_activation",
        _activation_payload(
            "status",
            runtime=runtime,
            data_config=data_config,
            runtime_root=tmp_path,
        ),
    )

    assert response["ok"] is True
    status = response["data"]
    assert status["status"] == expected_status
    assert status["storage_status"] == storage_status
    assert status["source_status"] == "available"
    assert status["pending_authoring_journal"] is False
    if case == "missing_database":
        assert not sqlite_path.exists()
        assert not sqlite_path.with_name(sqlite_path.name + "-wal").exists()
        assert not sqlite_path.with_name(sqlite_path.name + "-shm").exists()


def test_wheel_activation_agent_status_preserves_known_window_for_malformed_descriptor(
    tmp_path: Path,
) -> None:
    runtime, data_config, _sqlite_path, expected_window = (
        _malformed_activation_status_environment(tmp_path)
    )
    before = _deployment_file_bytes(tmp_path)

    response = execute_tool(
        "wheel_activation",
        _activation_payload(
            "status",
            runtime=runtime,
            data_config=data_config,
            runtime_root=tmp_path,
        ),
    )

    assert response["ok"] is True
    status = response["data"]
    assert status["current_window"] == expected_window
    assert status["latest_window"] == expected_window
    assert status["membership"] is True
    assert status["ready"] is False
    assert status["monitoring_gate"] == "config_mismatch"
    assert status["reason_code"] == "descriptor_mismatch"
    assert status["pending_authoring_journal"] is True
    assert _deployment_file_bytes(tmp_path) == before


def test_wheel_activation_agent_source_drift_preserves_failure_facts(
    tmp_path: Path,
) -> None:
    source, runtime, data_config, _sqlite_path = _activation_environment(tmp_path)
    preview, _, _ = position_tools.WHEEL_ACTIVATION_TOOL.call(
        _activation_payload(
            "enable",
            runtime=runtime,
            data_config=data_config,
            runtime_root=tmp_path,
        )
    )
    source.write_text(source.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    with pytest.raises(AgentToolError) as raised:
        position_tools.WHEEL_ACTIVATION_TOOL.call(
            _activation_payload(
                "enable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
                source_sha=preview["expected_source_sha256"],
                apply=True,
            )
        )

    assert raised.value.code == "STALE_PREVIEW"
    assert raised.value.details["failure_phase"] == "source_validation"
    assert raised.value.details["window_receipt"] is None
    assert raised.value.details["write_applied"] is False
    assert raised.value.details["original_request"]["request_id"] == "agent-enable-1"


def test_wheel_activation_agent_status_reports_missing_yaml_without_writes(
    tmp_path: Path,
) -> None:
    source, runtime, data_config, sqlite_path = _activation_environment(tmp_path)
    source.unlink()
    before = sqlite_path.read_bytes()

    status, _, _ = position_tools.WHEEL_ACTIVATION_TOOL.call(
        _activation_payload(
            "status",
            runtime=runtime,
            data_config=data_config,
            runtime_root=tmp_path,
        )
    )

    assert status["source_status"] == "unavailable"
    assert status["storage_status"] == "available"
    assert status["write_applied"] is False
    assert sqlite_path.read_bytes() == before

    with pytest.raises(AgentToolError) as raised:
        position_tools.WHEEL_ACTIVATION_TOOL.call(
            _activation_payload(
                "enable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
            )
        )
    assert raised.value.details["failure_phase"] == "source_validation"
    assert raised.value.details["source_status"] == "unavailable"
    assert raised.value.details["write_applied"] is False
    assert sqlite_path.read_bytes() == before


def test_wheel_activation_agent_config_failure_keeps_committed_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _source, runtime, data_config, _sqlite_path = _activation_environment(tmp_path)
    preview, _, _ = position_tools.WHEEL_ACTIVATION_TOOL.call(
        _activation_payload(
            "enable",
            runtime=runtime,
            data_config=data_config,
            runtime_root=tmp_path,
        )
    )
    monkeypatch.setattr(
        config_transaction,
        "publish_yaml_config_generation_locked",
        lambda **_kwargs: (_ for _ in ()).throw(
            AgentToolError(
                code="CONFIG_WRITE_FAILED",
                message="injected config interruption",
                details={"write_applied": False, "targets": []},
            )
        ),
    )

    with pytest.raises(AgentToolError) as raised:
        position_tools.WHEEL_ACTIVATION_TOOL.call(
            _activation_payload(
                "enable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
                source_sha=preview["expected_source_sha256"],
                apply=True,
            )
        )

    details = raised.value.details
    assert raised.value.code == "CONFIG_WRITE_FAILED"
    assert details["failure_phase"] == "config_publish"
    assert details["window_receipt"]["write_applied"] is True
    assert details["window_receipt"]["expected_config_descriptor"]["generation"] == 1
    assert details["config_audit"] == {"write_applied": False, "targets": []}
    assert details["write_applied"] is True
    assert details["readiness"]["reason_code"] == "missing_descriptor"


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
