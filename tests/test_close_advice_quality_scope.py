"""Public report reads bind quality to validated sources, with isolated files only."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.application.agent_tools import close_advice, close_advice_read_impl as reader
from src.application.close_advice_report_manifest import publish_close_advice_report_manifest
from src.application.quality.gate import assert_quality_allows
from src.application.tool_execution import execute_tool


@pytest.fixture
def report_scope(monkeypatch, tmp_path):
    declarations = {("alpha", "us"), ("beta", "hk")}
    blocked = set()
    calls = []
    now = datetime.now(timezone.utc)

    def read_quality(**scope):
        calls.append(scope)
        return {
            "observed_at_utc": now.isoformat(),
            "datasets": [
                {
                    "dataset_id": "om.option_positions",
                    "scope": {"account": account, "market": market},
                    "freshness": {"observed_at_utc": now.isoformat()},
                    "usable_for": [] if (account, market) in blocked else ["close_advice"],
                    "blocked_consumers": ["close_advice"] if (account, market) in blocked else [],
                    "blocked_by": ["OM-POS-002"] if (account, market) in blocked else [],
                    "reason_codes": ["POSITION_DIVERGENCE_PERSISTENT"] if (account, market) in blocked else [],
                }
                for account, market in sorted(declarations)
            ],
        }

    def gate(consumer, *, account, market):
        assert_quality_allows(
            consumer, account=account, market=market,
            service=SimpleNamespace(read_published=read_quality), now=now,
        )

    def load_config(**kwargs):
        # Deliberately differs from report accounts: reports own historical scope.
        return tmp_path / "config.us.json", {"accounts": ["config-only"], "_resolved": {"market": "us"}}

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "true")
    monkeypatch.setattr(reader, "assert_quality_allows", gate)
    monkeypatch.setattr(close_advice, "load_runtime_config", load_config)
    monkeypatch.setattr("src.application.agent_tool_config.load_runtime_config", load_config)
    monkeypatch.setattr(close_advice, "repo_base", lambda: tmp_path)
    monkeypatch.setattr(close_advice, "resolve_output_root", lambda _value: tmp_path / "agent_tools")
    return SimpleNamespace(root=tmp_path, declarations=declarations, blocked=blocked, calls=calls)


def _write_report(state, account, market, *, empty=False, name=None, accounts=None, declare_account=True):
    run_id = name or f"run-{market}-{account}"
    root = state.root / "output_runs" / run_id / "accounts" / account
    root.mkdir(parents=True)
    (root / "state").mkdir()
    (root / "state" / "config.override.json").write_text(json.dumps({"market": market}))
    rows = [] if empty else [{
        "account": account,
        "symbol": "NVDA" if market == "us" else "0700.HK",
        "option_type": "put", "position_side": "short", "status": "open",
        "evaluation_status": "priced", "recommendation_state": "hold",
        "policy_version": "strict_profit_capture.v1",
        "decision_basis": "net_capture_below_threshold", "decision_evidence_status": "complete",
        "quote_mode": "frozen_snapshot",
        "required_data_snapshot_manifest_sha256": "a" * 64,
        "close_advice_required_data_plan_sha256": "b" * 64,
    }]
    if accounts and rows:
        rows = [{**rows[0], "account": value} for value in accounts]
    csv_path, text_path = root / "close_advice.csv", root / "close_advice.txt"
    fields = list(rows[0]) if rows else ["account", "symbol"]
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    text_path.write_text("isolated report", encoding="utf-8")
    publish_close_advice_report_manifest(
        csv_path=csv_path, text_path=text_path, context_path=root / "unused.json",
        context={"filters": {"account": account} if declare_account else {}}, rows=rows, markets_to_run=[market],
        run_id=run_id, quote_mode="frozen_snapshot",
        required_data_snapshot_manifest_sha256="a" * 64,
        close_advice_required_data_plan_sha256="b" * 64,
    )
    return root


def _targets(state):
    return [(scope["account"], scope["market"]) for scope in state.calls]


def _assert_missing(result):
    assert result["ok"] is False, result
    assert result["error"]["code"] == "QUALITY_GATE_BLOCKED"
    assert result["error"]["details"]["reason_code"] == "QUALITY_DATASET_UNAVAILABLE"
    assert result["error"]["details"]["blocked_by"] == ["OM-POS-001"]


@pytest.mark.parametrize("context", [{"config_key": "us"}, {"config_path": "/isolated/config.us.json"}])
def test_public_report_requires_its_actual_account_without_request_account(report_scope, context):
    report = _write_report(report_scope, "alpha", "us")
    report_scope.declarations.remove(("alpha", "us"))
    result = execute_tool("close_advice_read", {**context, "report_path": str(report)})
    _assert_missing(result)
    assert _targets(report_scope) == [("alpha", "us")]


def test_public_symbol_market_overrides_default_config_quality_scope(report_scope):
    report = _write_report(report_scope, "beta", "hk")
    report_scope.blocked.add(("alpha", "us"))
    result = execute_tool("close_advice_read", {
        "config_key": "us", "report_path": str(report), "query": {"symbol": "0700.HK"},
    })
    assert result["ok"] is True, result
    assert result["data"]["rows"][0]["account"] == "beta"
    assert result["meta"]["market_filter"] == "HK"
    assert _targets(report_scope) == [("beta", "hk")]


@pytest.mark.parametrize("missing", [False, True])
def test_public_all_markets_requires_every_selected_source(report_scope, missing):
    _write_report(report_scope, "alpha", "us")
    _write_report(report_scope, "beta", "hk")
    if missing:
        report_scope.declarations.remove(("beta", "hk"))
    result = execute_tool("close_advice_read", {"config_key": "us", "market_scope": "all"})
    if missing:
        _assert_missing(result)
    else:
        assert result["ok"] is True, result
        assert result["data"]["returned_count"] == 2
        assert {row["account"] for row in result["data"]["rows"]} == {"alpha", "beta"}
    # Do not cross-join unrelated report sources or use config-only account.
    assert _targets(report_scope) == [("alpha", "us"), ("beta", "hk")]


def test_public_all_markets_does_not_require_unselected_absent_market(report_scope):
    _write_report(report_scope, "alpha", "us")
    report_scope.declarations.remove(("beta", "hk"))
    result = execute_tool("close_advice_read", {"config_key": "us", "market_scope": "all"})
    assert result["ok"] is True, result
    assert result["data"]["returned_count"] == 1
    assert _targets(report_scope) == [("alpha", "us")]


@pytest.mark.parametrize("available", [False, True])
def test_public_empty_report_keeps_declared_scope(report_scope, available):
    report = _write_report(report_scope, "alpha", "us", empty=True)
    if not available:
        report_scope.declarations.remove(("alpha", "us"))
    result = execute_tool("close_advice_read", {"report_path": str(report)})
    if available:
        assert result["ok"] is True, result
        assert result["data"]["row_count"] == result["data"]["returned_count"] == 0
    else:
        _assert_missing(result)
    assert _targets(report_scope) == [("alpha", "us")]


def test_public_matching_report_block_is_not_hidden_by_empty_query_result(report_scope):
    report = _write_report(report_scope, "alpha", "us")
    report_scope.blocked.add(("alpha", "us"))
    result = execute_tool("close_advice_read", {"report_path": str(report), "query": {"symbol": "TSLA"}})
    assert result["ok"] is False, result
    assert result["error"]["code"] == "QUALITY_GATE_BLOCKED"
    assert result["error"]["details"]["reason_code"] == "POSITION_DIVERGENCE_PERSISTENT"
    assert _targets(report_scope) == [("alpha", "us")]


def test_public_disabled_report_gate_preserves_read(report_scope, monkeypatch):
    report = _write_report(report_scope, "alpha", "us")
    monkeypatch.delenv("OM_QUALITY_ONBOARDED")
    report_scope.declarations.clear()
    result = execute_tool("close_advice_read", {"report_path": str(report)})
    assert result["ok"] is True, result
    assert result["data"]["returned_count"] == 1
    assert report_scope.calls == []


@pytest.mark.parametrize("selected", [None, "alpha"])
@pytest.mark.parametrize("missing", [False, True])
def test_public_combined_report_requires_each_requested_account(report_scope, selected, missing):
    report = _write_report(report_scope, "alpha", "us", accounts=["alpha", "beta"])
    if not missing:
        report_scope.declarations.add(("beta", "us"))
    result = execute_tool("close_advice_read", {
        "report_path": str(report), "query": {"account": selected},
    })
    if missing and not selected:
        _assert_missing(result)
    else:
        assert result["ok"] is True, result
        assert result["data"]["returned_count"] == (1 if selected else 2)
    assert _targets(report_scope) == ([("alpha", "us")] if selected else [("alpha", "us"), ("beta", "us")])



@pytest.mark.parametrize("entry", ["run_id", "latest"])
@pytest.mark.parametrize("beta_quality", ["healthy", "missing", "blocked"])
@pytest.mark.parametrize("selected", [None, "alpha"])
def test_public_discovered_combined_report_gates_manifest_accounts(
    report_scope, entry, beta_quality, selected,
):
    _write_report(report_scope, "alpha", "us", accounts=["alpha", "beta"])
    if beta_quality != "missing":
        report_scope.declarations.add(("beta", "us"))
    if beta_quality == "blocked":
        report_scope.blocked.add(("beta", "us"))
    payload = {"query": {"account": selected}}
    if entry == "run_id":
        payload["run_id"] = "run-us-alpha"

    result = execute_tool("close_advice_read", payload)

    if not selected and beta_quality == "missing":
        _assert_missing(result)
    elif not selected and beta_quality == "blocked":
        assert result["ok"] is False, result
        assert result["error"]["code"] == "QUALITY_GATE_BLOCKED"
        assert result["error"]["details"]["reason_code"] == "POSITION_DIVERGENCE_PERSISTENT"
        assert result["error"]["details"]["blocked_by"] == ["OM-POS-002"]
    else:
        assert result["ok"] is True, result
        assert result["data"]["returned_count"] == (1 if selected else 2)
        assert {row["account"] for row in result["data"]["rows"]} == (
            {"alpha"} if selected else {"alpha", "beta"}
        )
    assert _targets(report_scope) == (
        [("alpha", "us")] if selected else [("alpha", "us"), ("beta", "us")]
    )


def test_public_manifest_without_account_scope_is_not_empty_success(report_scope):
    report = _write_report(report_scope, "alpha", "us", empty=True, declare_account=False)
    result = execute_tool("close_advice_read", {"report_path": str(report)})
    _assert_missing(result)
    assert _targets(report_scope) == [(None, "us")]


def test_public_missing_sources_cannot_return_ungated_success(report_scope):
    result = execute_tool("close_advice_read", {})
    assert result["ok"] is False, result
    assert result["error"]["code"] == "DEPENDENCY_MISSING"
    assert report_scope.calls == []


def test_public_invalid_report_manifest_is_rejected_before_quality(report_scope):
    report = _write_report(report_scope, "alpha", "us")
    (report / "close_advice.txt").write_text("tampered report", encoding="utf-8")
    result = execute_tool("close_advice_read", {"report_path": str(report)})
    assert result["ok"] is False, result
    assert result["error"]["code"] == "DEPENDENCY_INVALID"
    assert result["error"]["details"]["reason"] == "close_advice_text_bytes_mismatch"
    assert report_scope.calls == []
