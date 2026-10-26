"""Public list quality/account scope with in-memory evidence and no ledger access."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.agent_tools import operations_impl, positions
from src.application.bot.tools import build_tool_payload
from src.application.quality.gate import assert_quality_allows
from src.application.tool_execution import execute_tool


_NOW = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def position_scope(monkeypatch, tmp_path):
    calls = {name: [] for name in ("quality", "config", "data_config", "repo", "list")}
    blocked = set()
    config = {"accounts": ["alpha", "beta"]}
    repo = object()
    declarations = {(account, "us") for account in config["accounts"]}
    empty = []

    def read_quality(**scope):
        calls["quality"].append(scope)
        observed = _NOW.isoformat()
        return {
            "observed_at_utc": observed,
            "datasets": [
                {
                    "dataset_id": "om.option_positions",
                    "scope": {"account": account, "market": market},
                    "freshness": {"observed_at_utc": observed},
                    "usable_for": [] if account in blocked else ["option_position_report"],
                    "blocked_consumers": ["option_position_report"] if account in blocked else [],
                    "blocked_by": ["OM-POS-002"] if account in blocked else [],
                    "reason_codes": ["POSITION_DIVERGENCE_PERSISTENT"] if account in blocked else [],
                }
                for account, market in sorted(declarations)
            ],
        }

    quality_reader = SimpleNamespace(read_published=read_quality)

    def quality_gate(consumer, *, account, market):
        assert_quality_allows(
            consumer, account=account, market=market, service=quality_reader, now=_NOW,
        )

    def load_config(**kwargs):
        calls["config"].append(kwargs)
        return Path(kwargs.get("config_path") or tmp_path / "config.us.json"), config

    def data_config(payload, portfolio):
        calls["data_config"].append((payload, portfolio))
        return tmp_path / "portfolio.runtime.json"

    def open_repo(**kwargs):
        calls["repo"].append(kwargs)
        return tmp_path / "portfolio.runtime.json", repo

    def list_rows(actual_repo, **query):
        assert actual_repo is repo
        calls["list"].append(query)
        return [] if empty else [{"record_id": "synthetic-position", "account": query["account"]}]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("list regression must not access providers, secrets, or other ledger actions")

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "true")
    monkeypatch.setattr(positions, "assert_quality_allows", quality_gate)
    monkeypatch.setattr(positions, "load_runtime_config", load_config)
    monkeypatch.setattr("src.application.agent_tool_config.load_runtime_config", load_config)
    monkeypatch.setattr(positions, "resolve_public_data_config_path", data_config)
    monkeypatch.setattr(positions, "resolve_option_positions_repo", open_repo)
    monkeypatch.setattr(positions, "list_position_rows", list_rows)
    monkeypatch.setattr(positions, "repo_base", lambda: tmp_path)
    monkeypatch.setattr(positions, "refresh_assigned_stock_quotes", forbidden)
    monkeypatch.setattr(positions, "build_lot_event_history", forbidden)
    monkeypatch.setattr(positions, "inspect_projection_state", forbidden)
    monkeypatch.setattr(operations_impl, "resolve_secret", forbidden)
    monkeypatch.setattr(operations_impl, "ledger_store_payload", lambda *_args: {"backend": "synthetic"})
    return SimpleNamespace(calls=calls, blocked=blocked, root=tmp_path, declarations=declarations, empty=empty, config=config)


def _assert_selected_scope(state, result, account):
    assert result["ok"] is True, result
    assert state.calls["quality"] == [{
        "consumer": "option_position_report",
        "account": target,
        "market": "us",
        "lifecycle_rows_requested": False,
    } for target in ([account] if account else ["alpha", "beta"])]
    assert len(state.calls["list"]) == 1
    assert state.calls["list"][0]["account"] == account
    data = result["data"]
    assert data["action"] == "list"
    assert data["rows"] == [{"record_id": "synthetic-position", "account": account}]
    assert data["row_count"] == 1
    assert data["filters"]["account"] == account
    assert data["filters"]["query"]["account"] == account
    assert data["coverage"]["status"] == "complete"
    assert data["pagination"]["returned_count"] == 1
    assert list(state.root.iterdir()) == []


@pytest.mark.parametrize(
    "payload,expected",
    [
        pytest.param({"query": {"account": "alpha"}}, "alpha", id="nested-only-default-action"),
        pytest.param({"account": "alpha"}, "alpha", id="top-only"),
        pytest.param({"account": "alpha", "query": {"account": "alpha"}}, "alpha", id="matching"),
        pytest.param({"account": "beta", "query": {"account": "alpha"}}, "alpha", id="nested-precedence"),
        pytest.param({"account": "alpha", "query": {}}, "alpha", id="nested-omitted-fallback"),
        pytest.param({"account": "alpha", "query": {"account": None}}, "alpha", id="nested-null-fallback"),
        pytest.param({"account": "alpha", "query": {"account": ""}}, "alpha", id="nested-empty-fallback"),
        pytest.param({"account": "alpha", "query": {"account": " \t"}}, "alpha", id="nested-blank-fallback"),
        pytest.param({"account": " ALPHA "}, "alpha", id="top-normalization"),
        pytest.param({"account": "beta", "query": {"account": " ALPHA "}}, "alpha", id="nested-normalization"),
        pytest.param({"action": ["list"], "query": {"account": "alpha"}}, "alpha", id="legacy-action"),
        pytest.param({"query": {"account": [" ALPHA "]}}, "alpha", id="nested-singleton"),
        pytest.param({}, None, id="unscoped"),
        pytest.param({"account": " ", "query": {"account": ""}}, None, id="blank-unscoped"),
    ],
)
def test_public_list_quality_and_query_share_effective_account(position_scope, payload, expected):
    request = {"config_key": "us", **payload}
    original = deepcopy(request)
    result = execute_tool("option_positions_read", request)
    _assert_selected_scope(position_scope, result, expected)
    assert request == original


@pytest.mark.parametrize("top_account", ["beta", ""])
def test_blocked_nested_account_stops_before_ledger(position_scope, top_account):
    position_scope.blocked.add("alpha")
    result = execute_tool("option_positions_read", {
        "config_key": "us", "action": "list", "account": top_account,
        "query": {"account": "alpha"},
    })
    assert result["ok"] is False, result
    assert result["error"]["code"] == "QUALITY_GATE_BLOCKED"
    assert result["error"]["details"] == {
        "consumer": "option_position_report",
        "reason_code": "POSITION_DIVERGENCE_PERSISTENT",
        "blocked_by": ["OM-POS-002"],
        "retryable": False,
    }
    assert len(position_scope.calls["quality"]) == 1
    assert position_scope.calls["quality"][0]["account"] == "alpha"
    assert len(position_scope.calls["config"]) == 1
    assert all(position_scope.calls[name] == [] for name in ("data_config", "repo", "list"))
    assert list(position_scope.root.iterdir()) == []


def test_blocked_top_account_does_not_block_healthy_selected_nested_account(position_scope):
    position_scope.blocked.add("beta")
    result = execute_tool("option_positions_read", {
        "config_key": "us", "action": "list", "account": "beta", "query": {"account": "alpha"},
    })
    _assert_selected_scope(position_scope, result, "alpha")


def test_unknown_nested_account_is_rejected_before_ledger_resolution(position_scope):
    result = execute_tool("option_positions_read", {
        "config_key": "us", "action": "list", "account": "beta", "query": {"account": "unknown"},
    })
    assert result["ok"] is False, result
    assert result["error"]["code"] == "INPUT_ERROR"
    assert "accounts are not configured: unknown" in result["error"]["message"]
    assert position_scope.calls["quality"] == []
    assert len(position_scope.calls["config"]) == 1
    assert all(position_scope.calls[name] == [] for name in ("data_config", "repo", "list"))


@pytest.mark.parametrize("account", [["alpha"], None])
def test_public_top_account_type_rejection_is_preserved(position_scope, account):
    result = execute_tool("option_positions_read", {
        "config_key": "us", "action": "list", "account": account, "query": {"account": "beta"},
    })
    assert result["ok"] is False, result
    assert result["error"]["code"] == "INPUT_ERROR"
    assert all(calls == [] for calls in position_scope.calls.values())


@pytest.mark.parametrize("action", ["history", "inspect", "assigned-stock"])
def test_other_actions_keep_top_level_quality_scope(position_scope, monkeypatch, action):
    seen = []

    def downstream(payload, **kwargs):
        seen.append(payload)
        assert kwargs["admit_event_query"] is None
        kwargs["load_runtime_config"](config_key=payload.get("config_key"), config_path=payload.get("config_path"))
        return {"action": action}, [], {}

    monkeypatch.setattr(positions, "option_positions_read_tool", downstream)
    result = execute_tool("option_positions_read", {
        "config_key": "us", "action": action, "account": "beta", "query": {"account": "alpha"},
    })
    assert result["ok"] is True, result
    assert len(position_scope.calls["quality"]) == 1
    assert position_scope.calls["quality"][0]["account"] == "beta"
    assert seen[0]["query"]["account"] == "alpha"
    assert len(position_scope.calls["config"]) == 1
    assert all(position_scope.calls[name] == [] for name in ("data_config", "repo", "list"))


@pytest.mark.parametrize("selection", [{"action": "events"}, {"cursor": "synthetic-cursor"}])
def test_event_query_field_rejection_still_precedes_io(position_scope, selection):
    result = execute_tool("option_positions_read", {
        "config_key": "us", **selection, "query": {"account": "alpha"},
    })
    assert result["ok"] is False, result
    assert result["error"]["code"] == "INPUT_ERROR"
    assert "unsupported fields for action=events: query" in result["error"]["message"]
    assert all(calls == [] for calls in position_scope.calls.values())


@pytest.mark.parametrize(
    "explicit,fixed,expected_error",
    [
        pytest.param(
            {"account": "beta", "query": {"account": "alpha"}},
            {"config_key": "us"},
            "query.account is outside or conflicts",
            id="top-nested-conflict",
        ),
        pytest.param(
            {"query": {"account": "alpha"}},
            {"config_key": "us", "account": "beta"},
            "query.account is outside or conflicts",
            id="nested-fixed-conflict",
        ),
        pytest.param(
            {"account": "alpha", "query": {"account": "alpha"}},
            {"config_key": "us", "account": "beta"},
            "tool input conflicts with trusted scope: account",
            id="top-fixed-conflict",
        ),
        pytest.param(
            {"account": "alpha", "query": {"account": "alpha"}},
            {"config_key": "us", "account": "alpha"},
            None,
            id="matching-scope",
        ),
    ],
)
def test_bot_account_conflict_checks_preserve_original_fields(
    position_scope, explicit, fixed, expected_error,
):
    original = deepcopy(explicit)
    payload, error = build_tool_payload(
        "option_positions_read", {"action": "list", **explicit}, fixed_input=fixed,
    )
    if expected_error:
        assert payload is None
        assert expected_error in error
    else:
        assert error is None
        assert payload["account"] == payload["query"]["account"] == "alpha"
    assert explicit == original
    assert position_scope.calls["quality"] == []
    assert all(position_scope.calls[name] == [] for name in ("data_config", "repo", "list"))


@pytest.mark.parametrize("payload", [{"config_key": "us"}, {"config_path": "/isolated/config.us.json"}])
def test_public_positions_rejects_artifact_for_another_market(position_scope, payload):
    position_scope.declarations.clear()
    position_scope.declarations.add(("alpha", "hk"))
    result = execute_tool("option_positions_read", {**payload, "query": {"account": "alpha"}})
    assert result["ok"] is False, result
    assert result["error"]["code"] == "QUALITY_GATE_BLOCKED"
    assert result["error"]["details"]["reason_code"] == "QUALITY_DATASET_UNAVAILABLE"
    assert position_scope.calls["quality"][0]["market"] == "us"
    assert all(position_scope.calls[name] == [] for name in ("data_config", "repo", "list"))


def test_public_unscoped_positions_requires_every_configured_account(position_scope):
    position_scope.declarations.remove(("beta", "us"))
    result = execute_tool("option_positions_read", {"config_key": "us"})
    assert result["ok"] is False, result
    assert result["error"]["details"]["reason_code"] == "QUALITY_DATASET_UNAVAILABLE"
    assert [scope["account"] for scope in position_scope.calls["quality"]] == ["alpha", "beta"]
    assert position_scope.calls["repo"] == position_scope.calls["list"] == []


def test_public_positions_healthy_empty_is_not_missing_evidence(position_scope):
    position_scope.empty.append(True)
    result = execute_tool("option_positions_read", {"config_key": "us"})
    assert result["ok"] is True, result
    assert result["data"]["rows"] == []
    assert result["data"]["row_count"] == 0
    assert result["data"]["coverage"]["status"] == "complete"
    assert [scope["account"] for scope in position_scope.calls["quality"]] == ["alpha", "beta"]
    assert len(position_scope.calls["list"]) == 1


def test_public_positions_disabled_gate_preserves_empty_result(position_scope, monkeypatch):
    monkeypatch.delenv("OM_QUALITY_ONBOARDED")
    position_scope.declarations.clear()
    position_scope.empty.append(True)
    result = execute_tool("option_positions_read", {"config_key": "us"})
    assert result["ok"] is True, result
    assert result["data"]["row_count"] == 0
    assert position_scope.calls["quality"] == []
    assert len(position_scope.calls["list"]) == 1


@pytest.mark.parametrize("selected", [None, "alpha"])
def test_events_admits_actual_signed_query_accounts(position_scope, monkeypatch, selected):
    def downstream(payload, **kwargs):
        kwargs["admit_event_query"](
            {"account": selected}, {"accounts": ["alpha", "beta"], "market": "US"},
        )
        return {"action": "events", "rows": []}, [], {}

    monkeypatch.setattr(positions, "option_positions_read_tool", downstream)
    position_scope.blocked.add("beta")
    result = execute_tool("option_positions_read", {"config_key": "us", "action": "events"})
    assert result["ok"] is (selected == "alpha"), result
    if selected is None:
        assert result["error"]["code"] == "QUALITY_GATE_BLOCKED"
    assert [scope["account"] for scope in position_scope.calls["quality"]] == (
        ["alpha"] if selected else ["alpha", "beta"]
    )
