from __future__ import annotations
import json
from contextlib import ExitStack
from unittest.mock import patch
from pathlib import Path
from typing import Any

import pytest

import src.application.agent_tools.analysis as analysis_module
from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.analysis import (
    ANALYSIS_CATALOG_TOOL,
    ANALYSIS_QUERY_TOOL,
    _canonical_symbol_performance_rows,
    _expiration_risk_bucket_rows,
    _execute_select,
    _open_option_exposure_rows,
    _option_trade_lifecycle_rows,
    _query_explain_and_evidence,
    _strategy_config_by_symbol_account_rows,
)


class _AnalysisQueryContext:
    def __init__(self, base: Path | None = None, config_path: Path | None = None):
        self.base = base or Path(".")
        self.config_path = config_path

    def __getattr__(self, _name):
        def _stub(*_args, **_kwargs):
            return None

        return _stub

    def repo_base(self):
        return self.base

    def mask_path(self, value):
        return f".../{Path(value).name}" if value else None

    def load_runtime_config(self, **_kwargs):
        return self.config_path or Path("config.us.json"), {}

    def list_symbol_rows(self, *_args, **_kwargs):
        return []


class _CatalogContext:
    def load_runtime_config(self, **_kwargs):
        return Path("config.us.json"), {}

    def mask_path(self, value):
        return f".../{Path(value).name}" if value else None


def _call_analysis_tool(tool, context: object, payload: dict[str, Any]):
    with ExitStack() as stack:
        for name in ("repo_base", "mask_path", "load_runtime_config", "list_symbol_rows", "collect_operation_timeline"):
            if name in getattr(context, "__dict__", {}) or getattr(type(context), name, None) is not None:
                stack.enter_context(patch.object(analysis_module, name, getattr(context, name)))
        return tool.call(payload)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + ("\n" if rows else ""), encoding="utf-8")

def _performance_report(*, account: str = "lx") -> dict[str, Any]:
    cash = {
        "total": {"amount": 120.0, "status": "observed", "missing": []},
        "open": {"amount": 80.0, "status": "observed", "missing": []},
        "terminated": {"amount": 40.0, "status": "observed", "missing": []},
    }
    rates = {"winning_contracts": 2, "eligible_contracts": 3, "rate": 2 / 3, "status": "observed"}
    return {
        "period": {
            "kind": "ytd",
            "start_date": "2026-01-01",
            "as_of_date": "2026-06-30",
            "start_at_ms": 1,
            "end_exclusive_at_ms": 2,
            "statistic_days": 181.0,
        },
        "scope": {"config_key": "us", "accounts": [account], "brokers": ["富途"]},
        "option_net_cashflow": {"by_currency": {"USD": cash}, "status": "observed", "missing": []},
        "sell_option_win_rate": rates,
        "buy_option_win_rate": rates,
        "option_return": {"by_currency": {"USD": {"return": 0.12}}, "status": "observed", "missing": []},
        "breakdowns": {
            "symbols": [
                {
                    "key": "NVDA",
                    "option_net_cashflow": {"by_currency": {"USD": cash}},
                    "sell_option_win_rate": rates,
                    "buy_option_win_rate": rates,
                    "option_return": {"by_currency": {"USD": {"return": 0.12}}},
                    "status": "observed",
                    "missing": [],
                }
            ]
        },
        "quality": {"status": "observed", "missing": [], "ledger_input_hash": "abc"},
    }


def test_analysis_query_rejects_write_sql_before_context_access() -> None:
    with pytest.raises(AgentToolError) as exc:
        _call_analysis_tool(ANALYSIS_QUERY_TOOL, object(), {"sql": "delete from option_period_performance"})

    assert exc.value.code == "PERMISSION_DENIED"


def test_analysis_catalog_rejects_non_string_view_filter_before_context_access() -> None:
    with pytest.raises(AgentToolError) as exc:
        _call_analysis_tool(ANALYSIS_CATALOG_TOOL, object(), {"views": {"option_period_performance": True}})

    assert exc.value.code == "INPUT_ERROR"


def test_analysis_catalog_exposes_semantic_metadata_for_option_performance() -> None:
    data, warnings, meta = _call_analysis_tool(ANALYSIS_CATALOG_TOOL, _CatalogContext(), {"views": "option_period_performance"})

    assert warnings == []
    assert meta == {"config_path": ".../config.us.json"}
    assert data["schema_version"] == "analysis.catalog.v2"

    view = data["views"]["option_period_performance"]
    assert view["row_grain"] == "period + selected account scope"
    assert view["primary_metric"] == "option_net_cashflow_by_currency"
    assert "sell_option_win_rate" in view["fields"]
    assert "option_return_by_currency" in view["fields"]


def test_analysis_catalog_exposes_retained_performance_views() -> None:
    data, _warnings, _meta = _call_analysis_tool(ANALYSIS_CATALOG_TOOL,
        _CatalogContext(),
        {"views": ["option_period_performance", "option_cash_components", "symbol_performance_attribution"]},
    )

    views = data["views"]
    period = views["option_period_performance"]
    components = views["option_cash_components"]
    symbol = views["symbol_performance_attribution"]
    assert components["row_grain"] == "period + currency + state"
    assert components["field_semantics"]["amount"]["aggregation"] == "sum"
    scope_fields = {
        "config_key",
        "period_kind",
        "period_start_date",
        "as_of_date",
        "start_at_ms",
        "end_exclusive_at_ms",
        "statistic_days",
        "accounts",
        "brokers",
        "ledger_input_hash",
    }
    win_fields = {
        "sell_option_winning_contracts",
        "sell_option_eligible_contracts",
        "sell_option_win_rate",
        "sell_option_win_rate_status",
        "buy_option_winning_contracts",
        "buy_option_eligible_contracts",
        "buy_option_win_rate",
        "buy_option_win_rate_status",
    }
    logical_key = (
        "config_key",
        "period_kind",
        "period_start_date",
        "as_of_date",
        "start_at_ms",
        "end_exclusive_at_ms",
        "accounts",
        "brokers",
        "ledger_input_hash",
    )
    assert set(period["fields"]) == scope_fields | win_fields | {
        "option_net_cashflow_by_currency",
        "option_return_by_currency",
        "quality_status",
        "missing",
    }
    assert set(components["fields"]) == scope_fields | {
        "currency",
        "state",
        "amount",
        "status",
        "missing",
    }
    assert set(symbol["fields"]) == scope_fields | win_fields | {
        "symbol",
        "option_net_cashflow_by_currency",
        "option_return_by_currency",
        "status",
        "missing",
    }
    assert tuple(period["primary_keys"]) == logical_key
    assert tuple(period["safe_join_keys"]) == logical_key
    assert tuple(components["primary_keys"]) == (*logical_key, "currency", "state")
    assert tuple(components["safe_join_keys"]) == (*logical_key, "currency", "state")
    assert tuple(symbol["primary_keys"]) == (*logical_key, "symbol")
    assert tuple(symbol["safe_join_keys"]) == (*logical_key, "symbol")


def test_analysis_catalog_exposes_p1_semantic_views() -> None:
    data, _warnings, _meta = _call_analysis_tool(ANALYSIS_CATALOG_TOOL,
        _CatalogContext(),
        {"views": ["open_option_exposure", "expiration_risk_buckets", "symbol_performance_attribution", "strategy_config_by_symbol_account"]},
    )

    assert data["views"]["open_option_exposure"]["row_grain"] == "account + symbol + option_type + side + strike + expiration"
    assert data["views"]["expiration_risk_buckets"]["row_grain"] == "account + expiration_bucket + currency"
    assert data["views"]["open_option_exposure"]["empty_result_meaning"] == "valid_current_negative_evidence"
    assert data["views"]["expiration_risk_buckets"]["empty_result_meaning"] == "valid_current_negative_evidence"
    assert data["views"]["symbol_performance_attribution"]["row_grain"] == "period + selected account scope + symbol"
    assert data["views"]["strategy_config_by_symbol_account"]["row_grain"] == "symbol + account + strategy_family"


def test_analysis_catalog_exposes_p2_semantic_views() -> None:
    data, _warnings, _meta = _call_analysis_tool(ANALYSIS_CATALOG_TOOL,
        _CatalogContext(),
        {
            "views": [
                "close_advice_snapshot",
                "runtime_tick_status",
                "upgrade_operation_status",
                "strategy_replay_read_surface",
            ]
        },
    )

    assert data["views"]["close_advice_snapshot"]["row_grain"] == "account + position_id + advice_run_id"
    assert data["views"]["runtime_tick_status"]["row_grain"] == "market + account + latest_run"
    assert "notification_status" in data["views"]["runtime_tick_status"]["fields"]
    assert "compatibility_notification_exists" in data["views"]["runtime_tick_status"]["fields"]
    assert data["views"]["runtime_tick_status"]["deprecated_fields"]["notification_exists"] == {
        "replacement": "compatibility_notification_exists",
        "removal_phase": "phase_c",
    }
    assert "notification_status" in data["views"]["runtime_tick_status"]["recommended_filters"]
    assert data["views"]["upgrade_operation_status"]["row_grain"] == "command_id + operation_id"
    assert "release_status" in data["views"]["upgrade_operation_status"]["fields"]
    assert "release_published_at" in data["views"]["upgrade_operation_status"]["fields"]
    assert "github_release_url" in data["views"]["upgrade_operation_status"]["fields"]
    assert data["views"]["strategy_replay_read_surface"]["row_grain"] == "research artifact or replay dataset"
    assert "dry_run_patch_allowed" in data["views"]["strategy_replay_read_surface"]["fields"]


def test_analysis_catalog_does_not_expose_task_recipes() -> None:
    data, _warnings, _meta = _call_analysis_tool(ANALYSIS_CATALOG_TOOL,
        _CatalogContext(),
        {"views": ["option_period_performance", "symbol_performance_attribution", "upgrade_operation_status"]},
    )

    assert "investigation_recipes" not in data
    assert "option_period_performance" in data["views"]
    assert "sql_rules" in data


def test_analysis_query_authorizer_rejects_non_whitelisted_tables() -> None:
    with pytest.raises(AgentToolError) as exc:
        _execute_select(
            "select name from sqlite_master",
            {"option_period_performance": [{"period_kind": "ytd"}]},
            limit=10,
        )

    assert exc.value.code == "INPUT_ERROR"
    assert "prohibited" in exc.value.message


def test_analysis_query_authorizer_rejects_non_whitelisted_functions() -> None:
    with pytest.raises(AgentToolError) as exc:
        _execute_select(
            "select load_extension('x') as loaded from option_period_performance",
            {"option_period_performance": [{"period_kind": "ytd"}]},
            limit=10,
        )

    assert exc.value.code == "INPUT_ERROR"
    assert "load_extension" in exc.value.message




def test_analysis_query_materializes_only_referenced_performance_views(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_performance_tool(*_args, **_kwargs):
        calls.append("performance")
        return _performance_report(), [], {}

    def fake_positions_tool(*_args, **_kwargs):
        calls.append("positions")
        return {"rows": []}, [], {}

    monkeypatch.setattr(analysis_module, "option_performance_report_tool", fake_performance_tool)
    monkeypatch.setattr(analysis_module, "option_positions_read_tool", fake_positions_tool)

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {"sql": "select period_kind, quality_status from option_period_performance", "limit": 10},
    )

    assert warnings == []
    assert calls == ["performance"]
    assert data["rows"] == [{"period_kind": "ytd", "quality_status": "observed"}]
    assert data["source"]["kind"] == "materialized_views"
    assert data["scope"] == {"views": ["option_period_performance"], "limit": 10}
    assert data["freshness"][0]["view"] == "option_period_performance"
    assert data["freshness"][0]["source"] == "option_performance_report canonical bundle"
    assert meta["requested_views"] == ["option_period_performance"]
    assert meta["materialized_views"] == ["option_period_performance"]


def test_analysis_query_views_mode_materializes_requested_views_without_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_performance_tool(*_args, **_kwargs):
        calls.append("performance")
        return _performance_report(), [], {}

    def fake_positions_tool(*_args, **_kwargs):
        calls.append("positions")
        return {"rows": [{"account": "lx", "symbol": "NVDA", "contracts": 1}]}, [], {}

    monkeypatch.setattr(analysis_module, "option_performance_report_tool", fake_performance_tool)
    monkeypatch.setattr(analysis_module, "option_positions_read_tool", fake_positions_tool)

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {
            "views": ["option_period_performance", "open_option_exposure"],
            "period": "ytd",
            "limit": 10,
        },
    )

    assert warnings == []
    assert calls == ["performance", "positions"]
    assert data["query"]["mode"] == "views"
    assert "filters" not in data["query"]
    assert data["preflight"]["warnings"] == warnings
    assert data["views_used"] == ["open_option_exposure", "option_period_performance"]
    performance_rows = data["view_datasets"]["option_period_performance"]["rows"]
    assert len(performance_rows) == 1
    assert performance_rows[0]["period_kind"] == "ytd"
    assert performance_rows[0]["quality_status"] == "observed"
    exposure_dataset = data["view_datasets"]["open_option_exposure"]
    assert exposure_dataset["row_count"] == 1
    assert exposure_dataset["rows"][0]["symbol"] == "NVDA"
    assert exposure_dataset["empty_result_meaning"] == "valid_current_negative_evidence"
    assert data["evidence"]["coverage"]["views"] == ["open_option_exposure", "option_period_performance"]
    assert meta["requested_views"] == ["open_option_exposure", "option_period_performance"]
    assert meta["materialized_views"] == ["open_option_exposure", "option_period_performance"]


def test_analysis_query_views_mode_filters_trade_events_by_trade_month(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_positions_tool(payload, *_args, **_kwargs):
        calls.append(str(payload.get("action")))
        return {
            "rows": [
                {
                    "account": "lx",
                    "symbol": "PLTR",
                    "trade_time_beijing": "2026-04-26 15:33:43 北京时间",
                },
                {
                    "account": "lx",
                    "symbol": "NVDA",
                    "trade_time_beijing": "2026-06-03 09:30:00 北京时间",
                },
                {
                    "account": "lx",
                    "symbol": "TSLA",
                    "trade_time_ms": 1780245000000,
                },
            ]
        }, [], {}

    monkeypatch.setattr(analysis_module, "option_positions_read_tool", fake_positions_tool)

    data, warnings, meta = _call_analysis_tool(
        ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {"views": ["trade_events"], "month": "2026-06", "limit": 10},
    )

    assert warnings == []
    assert calls == ["events"]
    assert data["query"]["filters"] == {"months": ["2026-06"]}
    assert [row["symbol"] for row in data["view_datasets"]["trade_events"]["rows"]] == ["NVDA", "TSLA"]
    assert data["view_datasets"]["trade_events"]["empty_result_meaning"] == "valid_requested_period_negative_evidence"
    assert meta["requested_views"] == ["trade_events"]
    assert meta["materialized_views"] == ["trade_events"]


def test_analysis_query_paginates_trade_events_with_bounded_page_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_positions_tool(payload, *_args, **_kwargs):
        calls.append(dict(payload))
        if payload.get("cursor") is None:
            return {
                "rows": [{"symbol": f"S{index}"} for index in range(20)],
                "has_more": True,
                "next_cursor": "cursor-1",
            }, [], {}
        assert payload["cursor"] == "cursor-1"
        return {
            "rows": [{"symbol": "S20"}],
            "has_more": False,
            "next_cursor": None,
        }, [], {}

    monkeypatch.setattr(analysis_module, "option_positions_read_tool", fake_positions_tool)

    data, warnings, meta = _call_analysis_tool(
        ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {"views": ["trade_events"], "limit": 30},
    )

    assert warnings == []
    assert [call["limit"] for call in calls] == [20, 20]
    assert "cursor" not in calls[0]
    assert calls[1]["cursor"] == "cursor-1"
    assert data["view_datasets"]["trade_events"]["row_count"] == 21
    assert meta["materialized_views"] == ["trade_events"]


def test_analysis_query_trade_events_empty_meaning_requires_month_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_positions_tool(payload, *_args, **_kwargs):
        assert payload.get("action") == "events"
        return {"rows": []}, [], {}

    monkeypatch.setattr(analysis_module, "option_positions_read_tool", fake_positions_tool)

    data, warnings, meta = _call_analysis_tool(
        ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {"views": ["trade_events"], "limit": 10},
    )

    assert warnings == []
    assert data["row_count"] == 0
    assert data["query"] == {"mode": "views", "views": ["trade_events"], "limit": 10}
    assert "empty_result_meaning" not in data["view_datasets"]["trade_events"]
    assert meta["requested_views"] == ["trade_events"]
    assert meta["materialized_views"] == ["trade_events"]


def test_analysis_query_materializes_the_three_retained_performance_views(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_performance_tool(*_args, **_kwargs):
        return _performance_report(), [], {}

    monkeypatch.setattr(analysis_module, "option_performance_report_tool", fake_performance_tool)

    data, warnings, _meta = _call_analysis_tool(
        ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {
            "views": [
                "option_period_performance",
                "option_cash_components",
                "symbol_performance_attribution",
            ],
            "period": "ytd",
        },
    )

    assert warnings == []
    period = data["view_datasets"]["option_period_performance"]["rows"][0]
    assert json.loads(period["option_net_cashflow_by_currency"])["USD"]["total"]["amount"] == 120.0
    assert period["sell_option_win_rate"] == 2 / 3
    sql_rows, columns, _views = _execute_select(
        "select sell_option_win_rate from option_period_performance",
        {"option_period_performance": [period]},
        limit=10,
    )
    assert columns == ["sell_option_win_rate"]
    assert sql_rows == [{"sell_option_win_rate": pytest.approx(2 / 3)}]
    cash_rows = data["view_datasets"]["option_cash_components"]["rows"]
    assert {row["state"] for row in cash_rows} == {"total", "open", "terminated"}
    symbol = data["view_datasets"]["symbol_performance_attribution"]["rows"][0]
    assert symbol["symbol"] == "NVDA"

def test_analysis_query_forwards_account_and_broker_scope_to_performance_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, Any]] = []

    def fake_performance_tool(payload, *_args, **_kwargs):
        requests.append(dict(payload))
        return _performance_report(account="lx"), [], {}

    monkeypatch.setattr(analysis_module, "option_performance_report_tool", fake_performance_tool)

    _call_analysis_tool(
        ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {
            "view": "option_period_performance",
            "period": "mtd",
            "account": "lx",
            "broker": "FUTU",
        },
    )

    assert requests == [
        {
            "config_key": "us",
            "account": "lx",
            "broker": "FUTU",
            "period": "mtd",
        }
    ]


@pytest.mark.parametrize(
    ("period", "selector"),
    [
        ("month", {"month": "2026-08"}),
        ("year", {"year": 2025}),
    ],
)
def test_analysis_query_forwards_natural_period_selector(
    monkeypatch: pytest.MonkeyPatch,
    period: str,
    selector: dict[str, Any],
) -> None:
    requests: list[dict[str, Any]] = []

    def fake_performance_tool(payload, *_args, **_kwargs):
        requests.append(dict(payload))
        return _performance_report(), [], {}

    monkeypatch.setattr(analysis_module, "option_performance_report_tool", fake_performance_tool)

    _call_analysis_tool(
        ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {"view": "option_period_performance", "period": period, **selector},
    )

    assert requests == [{"config_key": "us", "period": period, **selector}]


def test_analysis_query_rejects_multi_month_filter_for_performance_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_performance_tool(*_args, **_kwargs):
        nonlocal called
        called = True
        return _performance_report(), [], {}

    monkeypatch.setattr(analysis_module, "option_performance_report_tool", fake_performance_tool)

    with pytest.raises(AgentToolError, match="does not accept: months"):
        _call_analysis_tool(
            ANALYSIS_QUERY_TOOL,
            _AnalysisQueryContext(),
            {"view": "option_period_performance", "months": ["2026-07", "2026-08"]},
        )

    assert called is False


def test_analysis_query_materializes_only_referenced_position_views(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_monthly_tool(*_args, **_kwargs):
        calls.append("monthly")
        return _performance_report(), [], {}

    def fake_positions_tool(payload, *_args, **_kwargs):
        calls.append(str(payload.get("action")))
        return {
            "rows": [
                {
                    "account": "lx",
                    "symbol": "NVDA",
                    "status": "open",
                    "side": "short",
                    "option_type": "put",
                    "strike": 100.0,
                    "expiration_ymd": "2099-01-20",
                    "contracts_open": 1,
                    "currency": "USD",
                    "cash_secured_amount": 10000.0,
                }
            ]
        }, [], {}

    monkeypatch.setattr(analysis_module, "option_performance_report_tool", fake_monthly_tool)
    monkeypatch.setattr(analysis_module, "option_positions_read_tool", fake_positions_tool)

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {"sql": "select account, symbol, strategy from open_option_exposure", "limit": 10},
    )

    assert warnings == []
    assert calls == ["list"]
    assert data["rows"] == [{"account": "lx", "symbol": "NVDA", "strategy": "sell_put"}]
    assert meta["requested_views"] == ["open_option_exposure"]
    assert meta["materialized_views"] == ["open_option_exposure"]


def test_analysis_query_select_constant_materializes_no_views(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_monthly_tool(*_args, **_kwargs):
        calls.append("monthly")
        return _performance_report(), [], {}

    def fake_positions_tool(*_args, **_kwargs):
        calls.append("positions")
        return {}, [], {}

    monkeypatch.setattr(analysis_module, "option_performance_report_tool", fake_monthly_tool)
    monkeypatch.setattr(analysis_module, "option_positions_read_tool", fake_positions_tool)

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL, _AnalysisQueryContext(), {"sql": "select 1 as ok"})

    assert warnings == []
    assert calls == []
    assert data["rows"] == [{"ok": 1}]
    assert meta["requested_views"] == []
    assert meta["materialized_views"] == []


def test_analysis_query_strategy_replay_read_surface_reads_candidate_impact(tmp_path: Path) -> None:
    result_path = (
        tmp_path
        / "output_shared"
        / "research"
        / "shadow_replay"
        / "backtests"
        / "candidate-impact-report-us-2026-06-02"
        / "result.us.json"
    )
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(
            {
                "schema_version": "shadow_replay_candidate_impact.v1",
                "generated_at_utc": "2026-06-17T00:00:00Z",
                "data_mode": "closed_replay",
                "universe_scope": "observed_run_universe",
                "coverage": {
                    "strict_backtest_allowed": True,
                    "selected_run_ids": ["run-1"],
                },
                "filters": {"accounts": ["lx"], "market": "us"},
                "summary": {
                    "candidate_snapshot_count": 42,
                    "underwriting_candidate_count": 40,
                    "mark_path_snapshot_count": 38,
                    "usable_mark_path_snapshot_count": 38,
                    "outcome_fact_count": 35,
                    "min_sample": 30,
                },
                "gates": {
                    "candidate_impact": {"allowed": True, "status": "ready"},
                    "production_recommendation": {"allowed": False, "status": "blocked"},
                },
                "candidate_impact": {
                    "allowed": True,
                    "status": "ready",
                    "best_variant_by_new_accepts": "iv_rv_1_10",
                },
                "recommendation": {
                    "status": "ready_for_live_shadow_review",
                    "production_recommendation_allowed": False,
                    "candidate_variant": "iv_rv_1_10",
                    "next_action": "review_variant_then_run_live_shadow_before_production_change",
                },
                "safety": {"writes_runtime_config": False},
            }
        ),
        encoding="utf-8",
    )
    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(tmp_path),
        {
            "sql": (
                "select artifact_kind, status, data_mode, candidate_impact_allowed, "
                "production_recommendation_allowed, dry_run_patch_allowed, best_variant, strategy_family "
                "from strategy_replay_read_surface order by artifact_kind"
            ),
            "limit": 10,
        },
    )

    assert warnings == []
    assert data["rows"] == [{
        "artifact_kind": "shadow_replay_candidate_impact",
        "status": "ready_for_live_shadow_review",
        "data_mode": "closed_replay",
        "candidate_impact_allowed": 1,
        "production_recommendation_allowed": 0,
        "dry_run_patch_allowed": 0,
        "best_variant": "iv_rv_1_10",
        "strategy_family": None,
    }]
    assert data["evidence"]["diagnostics"] == [
        {
            "view": "strategy_replay_read_surface",
            "status": "observed_strategy_replay_evidence",
            "severity": "info",
            "artifact_kinds": ["shadow_replay_candidate_impact"],
            "statuses": ["ready_for_live_shadow_review"],
            "data_modes": ["closed_replay"],
            "strategy_families": [],
            "summary": (
                "strategy replay read-surface rows were observed "
                "(artifacts=shadow_replay_candidate_impact; "
                "data_mode=closed_replay; candidate_impact_allowed)"
            ),
            "answer_boundary": "offline_replay_evidence_only",
            "candidate_impact_allowed": True,
        }
    ]
    assert meta["requested_views"] == ["strategy_replay_read_surface"]
    assert meta["materialized_views"] == ["strategy_replay_read_surface"]


def test_analysis_query_strategy_replay_read_surface_reads_dataset_status(tmp_path: Path) -> None:
    dataset = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets" / "case-dataset"
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                "symbol": "NVDA",
                "account": "lx",
                "status": "accepted",
                "contract_symbol": "NVDA260619P00100000",
                "option_type": "put",
                "strategy_profile": "short_vol",
            }
        ],
    )
    _write_jsonl(dataset / "filter_decisions.jsonl", [])
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(tmp_path),
        {
            "sql": (
                "select artifact_kind, dataset_id, status, data_mode, candidate_snapshot_count, "
                "production_recommendation_allowed from strategy_replay_read_surface"
            ),
            "limit": 10,
        },
    )

    assert warnings == []
    assert data["rows"] == [
        {
            "artifact_kind": "shadow_replay_dataset",
            "dataset_id": "case-dataset",
            "status": "not_ready",
            "data_mode": "filter_only",
            "candidate_snapshot_count": 1,
            "production_recommendation_allowed": 0,
        }
    ]
    assert data["evidence"]["diagnostics"][0]["status"] == "observed_strategy_replay_evidence"
    assert meta["requested_views"] == ["strategy_replay_read_surface"]
    assert meta["materialized_views"] == ["strategy_replay_read_surface"]


def test_analysis_query_strategy_replay_read_surface_missing_artifact_returns_warning(tmp_path: Path) -> None:
    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(tmp_path),
        {"sql": "select count(*) as row_count from strategy_replay_read_surface", "limit": 10},
    )

    assert data["rows"] == [{"row_count": 0}]
    assert warnings == ["strategy_replay_read_surface missing: no Shadow Replay artifacts found"]
    assert data["evidence"]["diagnostics"] == [
        {
            "view": "strategy_replay_read_surface",
            "status": "diagnostic_missing",
            "severity": "warning",
            "summary": "no Shadow Replay artifacts found",
            "answer_boundary": "cannot infer diagnostic root cause",
        }
    ]
    assert meta["requested_views"] == ["strategy_replay_read_surface"]
    assert meta["materialized_views"] == ["strategy_replay_read_surface"]


def test_analysis_query_close_advice_snapshot_missing_artifact_returns_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_close_advice_tool(*_args, **_kwargs):
        raise AgentToolError(code="DEPENDENCY_MISSING", message="没有找到最近的平仓建议报告。")

    monkeypatch.setattr(analysis_module, "_call_close_advice_read_tool", fake_close_advice_tool)

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {"sql": "select count(*) as row_count from close_advice_snapshot", "limit": 10},
    )

    assert data["rows"] == [{"row_count": 0}]
    assert warnings == ["close_advice_snapshot missing: 没有找到最近的平仓建议报告。"]
    assert data["evidence"]["diagnostics"] == [
        {
            "view": "close_advice_snapshot",
            "status": "diagnostic_missing",
            "severity": "warning",
            "summary": "没有找到最近的平仓建议报告。",
            "answer_boundary": "cannot infer diagnostic root cause",
        }
    ]
    assert meta["requested_views"] == ["close_advice_snapshot"]
    assert meta["materialized_views"] == ["close_advice_snapshot"]


def test_close_advice_snapshot_preserves_recommendation_contract() -> None:
    row = analysis_module._close_advice_snapshot_row(
        {
            "account": "lx",
            "position_lot_id": "lot-1",
            "source_run_id": "run-1",
            "symbol": "NVDA",
            "tier": "medium",
            "policy_version": "p0_current.v1",
            "recommendation_state": "close",
            "decision_basis": "profit_capture_medium",
            "decision_evidence_status": "complete",
            "close_action": "close",
        }
    )

    assert row["policy_version"] == "p0_current.v1"
    assert row["recommendation_state"] == "close"
    assert row["decision_basis"] == "profit_capture_medium"
    assert row["decision_evidence_status"] == "complete"


def test_analysis_query_runtime_tick_status_uses_runtime_read_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_runtime_status_tool(*_args, **_kwargs):
        return {
            "config": {"config_key": "us", "accounts": ["lx"]},
            "summary": {
                "latest_status": "ok",
                "latest_run_path": "output_runs/run-1",
                "warning_count": 0,
                "warning_codes": [],
            },
            "freshness": {"status": "fresh", "age_seconds": 12},
            "accounts": {"lx": {"notification": {"exists": True}}},
        }, [], {}

    monkeypatch.setattr(analysis_module, "_call_runtime_status_tool", fake_runtime_status_tool)

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {"sql": "select market, account, latest_run_id, freshness_status from runtime_tick_status", "limit": 10},
    )

    assert warnings == []
    assert data["rows"] == [{"market": "US", "account": "lx", "latest_run_id": "run-1", "freshness_status": "fresh"}]
    assert data["evidence"]["diagnostics"] == [
        {
            "view": "runtime_tick_status",
            "status": "observed_runtime_status",
            "severity": "info",
            "accounts": ["lx"],
            "summary": "runtime status rows were observed",
            "answer_boundary": "observed_runtime_status_only",
        }
    ]
    assert meta["requested_views"] == ["runtime_tick_status"]
    assert meta["materialized_views"] == ["runtime_tick_status"]


def test_analysis_query_runtime_tick_status_surfaces_notification_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_runtime_status_tool(*_args, **_kwargs):
        return {
            "config": {"config_key": "us", "accounts": ["lx"]},
            "summary": {
                "latest_status": "success",
                "latest_run_path": "output_runs/run-1",
                "warning_count": 0,
                "warning_codes": [],
            },
            "freshness": {"status": "fresh", "age_seconds": 12},
            "notification_diagnosis": {"status": "failed", "reason": "delivery returned an error"},
            "accounts": {"lx": {"notification": {"exists": True}}},
        }, [], {}

    monkeypatch.setattr(analysis_module, "_call_runtime_status_tool", fake_runtime_status_tool)

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {"sql": "select account, latest_status, notification_status from runtime_tick_status", "limit": 10},
    )

    assert warnings == []
    assert data["rows"] == [{"account": "lx", "latest_status": "success", "notification_status": "failed"}]
    assert data["evidence"]["diagnostics"] == [
        {
            "view": "runtime_tick_status",
            "status": "conflicting_evidence",
            "severity": "warning",
            "accounts": ["lx"],
            "summary": "runtime diagnostic evidence is conflicting: latest_status=success, notification_status=failed",
            "answer_boundary": "conflicting_runtime_evidence_only",
        }
    ]
    assert meta["requested_views"] == ["runtime_tick_status"]
    assert meta["materialized_views"] == ["runtime_tick_status"]


def test_analysis_runtime_tick_status_uses_compatibility_name_without_treating_absence_as_delivery_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_runtime_status_tool(*_args, **_kwargs):
        return {
            "config": {"config_key": "us", "accounts": ["lx"]},
            "summary": {
                "latest_status": "success",
                "latest_run_path": "output_runs/run-1",
                "warning_count": 0,
                "warning_codes": [],
            },
            "freshness": {"status": "fresh", "age_seconds": 12},
            "notification_diagnosis": {"status": "sent", "reason": "delivery confirmed"},
            "accounts": {"lx": {"compatibility_notification": {"exists": False}}},
        }, [], {}

    monkeypatch.setattr(analysis_module, "_call_runtime_status_tool", fake_runtime_status_tool)

    data, warnings, _meta = _call_analysis_tool(
        ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {
            "sql": (
                "select compatibility_notification_exists, notification_exists, notification_status "
                "from runtime_tick_status"
            ),
            "limit": 10,
        },
    )

    assert warnings == []
    assert data["rows"] == [
        {
            "compatibility_notification_exists": False,
            "notification_exists": False,
            "notification_status": "sent",
        }
    ]
    assert data["evidence"]["diagnostics"][0]["status"] == "observed_runtime_status"
    assert data["evidence"]["diagnostics"][0]["severity"] == "info"


def test_analysis_query_runtime_tick_status_surfaces_scheduler_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_runtime_status_tool(*_args, **_kwargs):
        return {
            "config": {"config_key": "hk", "accounts": ["sy"]},
            "summary": {
                "latest_status": "success",
                "latest_run_path": "output_runs/run-2",
                "warning_count": 0,
                "warning_codes": [],
            },
            "freshness": {"status": "fresh", "age_seconds": 30},
            "notification_diagnosis": {
                "status": "scheduler_skipped",
                "reason": "market_closed",
                "scheduler_should_run_scan": False,
                "scheduler_should_notify": False,
                "scheduler_reason": "market_closed",
            },
            "accounts": {"sy": {"notification": {"exists": False}}},
        }, [], {}

    monkeypatch.setattr(analysis_module, "_call_runtime_status_tool", fake_runtime_status_tool)

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {
            "sql": (
                "select account, notification_status, scheduler_should_run_scan, "
                "scheduler_should_notify, scheduler_reason from runtime_tick_status"
            ),
            "limit": 10,
        },
    )

    assert warnings == []
    assert data["rows"] == [
        {
            "account": "sy",
            "notification_status": "scheduler_skipped",
            "scheduler_should_run_scan": False,
            "scheduler_should_notify": False,
            "scheduler_reason": "market_closed",
        }
    ]
    assert data["evidence"]["diagnostics"] == [
        {
            "view": "runtime_tick_status",
            "status": "observed_scheduler_skip",
            "severity": "warning",
            "accounts": ["sy"],
            "summary": "scheduler skipped because market_closed",
            "answer_boundary": "observed_runtime_status_only",
        }
    ]
    assert meta["requested_views"] == ["runtime_tick_status"]
    assert meta["materialized_views"] == ["runtime_tick_status"]


def test_analysis_query_upgrade_operation_status_uses_operation_timeline() -> None:
    ctx = _AnalysisQueryContext()
    calls: list[dict[str, Any]] = []

    def fake_operation_timeline(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "schema_version": "operation-timeline-v1",
            "timeline_count": 1,
            "timelines": [
                {
                    "identity": {
                        "command_id": "in_85aa7e2e5c59ef4c6a620a68",
                        "operation_id": "in_85aa7e2e5c59ef4c6a620a68",
                    },
                    "operation": {
                        "operation_id": "in_85aa7e2e5c59ef4c6a620a68",
                        "command_id": "in_85aa7e2e5c59ef4c6a620a68",
                        "operation_type": "upgrade_now",
                        "status": "confirmed",
                        "current_version": None,
                        "target_version": None,
                        "created_at": "2026-06-14T10:00:00+00:00",
                        "confirmed_at": "2026-06-14T10:01:00+00:00",
                    },
                    "receipt": {"status": "not_observed"},
                    "outcome": {"status": "confirmed", "ok": False, "warnings": ["receipt_not_observed"]},
                    "audit": {
                        "rows": [
                            {
                                "tool_payload": {
                                    "target_version": "1.2.111",
                                    "release_tag": "v1.2.111",
                                }
                            }
                        ]
                    },
                    "warnings": ["receipt_not_observed"],
                }
            ],
            "warnings": ["receipt_not_observed"],
        }

    ctx.collect_operation_timeline = fake_operation_timeline  # type: ignore[method-assign]

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        ctx,
        {
            "sql": (
                "select command_id, operation_status, current_version, target_version, receipt_status, warning_codes "
                "from upgrade_operation_status "
                "where command_id = 'in_85aa7e2e5c59ef4c6a620a68'"
            ),
            "limit": 20,
        },
    )

    assert warnings == []
    assert calls[0]["operation_id"] == "in_85aa7e2e5c59ef4c6a620a68"
    assert calls[0]["operation_types"] == ["upgrade_now"]
    assert data["rows"] == [
        {
            "command_id": "in_85aa7e2e5c59ef4c6a620a68",
            "operation_status": "confirmed",
            "current_version": None,
            "target_version": "1.2.111",
            "receipt_status": "not_observed",
            "warning_codes": '["receipt_not_observed"]',
        }
    ]
    assert data["evidence"]["diagnostics"] == [
        {
            "view": "upgrade_operation_status",
            "status": "observed_operation_status",
            "severity": "warning",
            "command_ids": ["in_85aa7e2e5c59ef4c6a620a68"],
            "operation_ids": [],
            "operation_statuses": ["confirmed"],
            "receipt_statuses": ["not_observed"],
            "summary": (
                "upgrade operation status rows were observed "
                "(operation_status=confirmed; receipt_status=not_observed; "
                "missing=current_version_missing,receipt_not_observed)"
            ),
            "answer_boundary": "upgrade_operation_status_evidence_only",
            "missing_data": [
                {
                    "kind": "current_version_missing",
                    "impact": "cannot display or verify current version",
                    "recoverable_by": "operation_timeline",
                },
                {
                    "kind": "receipt_not_observed",
                    "impact": "cannot prove final upgrade receipt delivery from operation status evidence",
                    "recoverable_by": "operation_timeline",
                },
            ],
        }
    ]
    assert meta["requested_views"] == ["upgrade_operation_status"]
    assert meta["materialized_views"] == ["upgrade_operation_status"]


def test_analysis_query_upgrade_operation_status_marks_status_conflict() -> None:
    ctx = _AnalysisQueryContext()

    def fake_operation_timeline(**_kwargs: Any) -> dict[str, Any]:
        return {
            "schema_version": "operation-timeline-v1",
            "timeline_count": 1,
            "timelines": [
                {
                    "identity": {
                        "command_id": "in_conflict",
                        "operation_id": "in_conflict",
                    },
                    "operation": {
                        "operation_id": "in_conflict",
                        "command_id": "in_conflict",
                        "operation_type": "upgrade_now",
                        "status": "applied",
                        "current_version": "1.2.110",
                        "target_version": "1.2.111",
                    },
                    "receipt": {"status": "observed"},
                    "outcome": {"status": "failed", "ok": False},
                    "warnings": [],
                }
            ],
            "warnings": [],
        }

    ctx.collect_operation_timeline = fake_operation_timeline  # type: ignore[method-assign]

    data, warnings, _meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        ctx,
        {
            "sql": (
                "select command_id, operation_status, outcome_status, current_version, target_version, receipt_status "
                "from upgrade_operation_status where command_id = 'in_conflict'"
            ),
            "limit": 20,
        },
    )

    assert warnings == []
    assert data["rows"] == [
        {
            "command_id": "in_conflict",
            "operation_status": "applied",
            "outcome_status": "failed",
            "current_version": "1.2.110",
            "target_version": "1.2.111",
            "receipt_status": "observed",
        }
    ]
    diagnostic = data["evidence"]["diagnostics"][0]
    assert diagnostic["status"] == "conflicting_evidence"
    assert diagnostic["severity"] == "warning"
    assert diagnostic["operation_statuses"] == ["applied"]
    assert diagnostic["outcome_statuses"] == ["failed"]
    assert diagnostic["receipt_statuses"] == ["observed"]
    assert diagnostic["answer_boundary"] == "conflicting_upgrade_operation_evidence_only"
    assert diagnostic["conflicts"] == ["operation_status=applied,outcome_status=failed,receipt_status=observed"]


def test_analysis_query_upgrade_operation_status_extracts_release_publication_fields() -> None:
    ctx = _AnalysisQueryContext()

    def fake_operation_timeline(**_kwargs: Any) -> dict[str, Any]:
        return {
            "schema_version": "operation-timeline-v1",
            "timeline_count": 1,
            "timelines": [
                {
                    "identity": {"command_id": "in_release_ok", "operation_id": "in_release_ok"},
                    "operation": {
                        "operation_id": "in_release_ok",
                        "command_id": "in_release_ok",
                        "operation_type": "upgrade_now",
                        "status": "applied",
                        "current_version": "1.2.272",
                        "target_version": "1.2.273",
                        "release_tag": "v1.2.273",
                        "release_status": "published",
                        "release_published_at": "2026-06-14T19:01:30Z",
                        "github_release_url": "https://github.example/releases/tag/v1.2.273",
                    },
                    "receipt": {"status": "observed"},
                    "outcome": {"status": "succeeded", "ok": True},
                    "warnings": [],
                }
            ],
            "warnings": [],
        }

    ctx.collect_operation_timeline = fake_operation_timeline  # type: ignore[method-assign]

    data, warnings, _meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        ctx,
        {
            "sql": (
                "select command_id, release_tag, release_status, release_published_at, github_release_url "
                "from upgrade_operation_status where command_id = 'in_release_ok'"
            ),
            "limit": 20,
        },
    )

    assert warnings == []
    assert data["rows"] == [
        {
            "command_id": "in_release_ok",
            "release_tag": "v1.2.273",
            "release_status": "published",
            "release_published_at": "2026-06-14T19:01:30Z",
            "github_release_url": "https://github.example/releases/tag/v1.2.273",
        }
    ]
    diagnostic = data["evidence"]["diagnostics"][0]
    assert diagnostic["status"] == "observed_operation_status"
    assert diagnostic["severity"] == "info"
    assert diagnostic["release_statuses"] == ["published"]
    assert diagnostic["missing_data"] == []


def test_analysis_query_upgrade_operation_status_reports_missing_audit_artifact() -> None:
    ctx = _AnalysisQueryContext()

    def fake_operation_timeline(**_kwargs: Any) -> dict[str, Any]:
        return {
            "schema_version": "operation-timeline-v1",
            "timeline_count": 0,
            "timelines": [],
            "warnings": ["audit_db_missing"],
        }

    ctx.collect_operation_timeline = fake_operation_timeline  # type: ignore[method-assign]

    data, warnings, _meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        ctx,
        {"sql": "select command_id, operation_status from upgrade_operation_status", "limit": 20},
    )

    assert data["rows"] == []
    assert "upgrade_operation_status missing: audit_db_missing" in warnings
    assert any(item["status"] == "artifact_missing" for item in data["evidence"]["diagnostics"])


def test_analysis_query_upgrade_operation_status_reports_missing_command_log_artifact() -> None:
    ctx = _AnalysisQueryContext()

    def fake_operation_timeline(**_kwargs: Any) -> dict[str, Any]:
        return {
            "schema_version": "operation-timeline-v1",
            "timeline_count": 0,
            "timelines": [],
            "warnings": ["command_log_missing"],
        }

    ctx.collect_operation_timeline = fake_operation_timeline  # type: ignore[method-assign]

    data, warnings, _meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        ctx,
        {"sql": "select command_id, operation_status from upgrade_operation_status", "limit": 20},
    )

    assert data["rows"] == []
    assert "upgrade_operation_status missing: command_log_missing" in warnings
    diagnostic = data["evidence"]["diagnostics"][0]
    assert diagnostic["status"] == "artifact_missing"
    assert diagnostic["summary"] == "command_log_missing"
    assert diagnostic["answer_boundary"] == "diagnostic artifact missing"


def test_open_option_exposure_and_expiration_buckets_are_derived_from_position_rows() -> None:
    rows = _open_option_exposure_rows(
        [
            {
                "account": "lx",
                "symbol": "NVDA",
                "status": "open",
                "side": "short",
                "option_type": "put",
                "strike": 100.0,
                "expiration_ymd": "2099-01-20",
                "contracts_open": 2,
                "currency": "USD",
                "cash_secured_amount": 20000.0,
            },
            {
                "account": "lx",
                "symbol": "AAPL",
                "status": "closed",
                "side": "short",
                "option_type": "put",
                "contracts_open": 0,
            },
        ]
    )
    buckets = _expiration_risk_bucket_rows(rows)

    assert len(rows) == 1
    assert rows[0]["strategy"] == "sell_put"
    assert rows[0]["risk_model"] == "cash_secured_put"
    assert rows[0]["dte"] is not None
    assert buckets[0]["account"] == "lx"
    assert buckets[0]["currency"] == "USD"
    assert buckets[0]["position_count"] == 1
    assert buckets[0]["contracts_open"] == 2.0
    assert buckets[0]["cash_secured_amount"] == 20000.0


def test_option_trade_lifecycle_groups_open_and_close_events() -> None:
    rows = _option_trade_lifecycle_rows(
        [
            {
                "trade_time_beijing": "2026-06-01 10:00:00",
                "account": "lx",
                "symbol": "NVDA",
                "position_effect": "OPEN",
                "side": "sell",
                "option_type": "put",
                "contracts": 2,
                "strike": 100,
                "expiration_ymd": "2026-07-17",
                "currency": "USD",
            },
            {
                "trade_time_beijing": "2026-06-15 10:00:00",
                "account": "lx",
                "symbol": "NVDA",
                "position_effect": "CLOSE",
                "side": "buy",
                "option_type": "put",
                "contracts": 1,
                "strike": 100,
                "expiration_ymd": "2026-07-17",
                "currency": "USD",
            },
        ]
    )

    assert rows == [
        {
            "account": "lx",
            "symbol": "NVDA",
            "position_side": "short",
            "option_type": "put",
            "strike": 100,
            "expiration_ymd": "2026-07-17",
            "currency": "USD",
            "first_trade_time": "2026-06-01 10:00:00",
            "last_trade_time": "2026-06-15 10:00:00",
            "open_contracts": 2.0,
            "close_contracts": 1.0,
            "net_contracts": 1.0,
            "event_count": 2,
            "lifecycle_status": "open",
        }
    ]


def test_option_trade_lifecycle_matches_buy_open_with_sell_close() -> None:
    rows = _option_trade_lifecycle_rows(
        [
            {
                "trade_time_beijing": "2026-06-01 10:00:00",
                "account": "lx",
                "symbol": "NVDA",
                "position_effect": "open",
                "side": "buy",
                "option_type": "call",
                "contracts": 1,
                "strike": 120,
                "expiration_ymd": "2026-08-21",
                "currency": "USD",
            },
            {
                "trade_time_beijing": "2026-06-20 10:00:00",
                "account": "lx",
                "symbol": "NVDA",
                "position_effect": "close",
                "side": "sell",
                "option_type": "call",
                "contracts": 1,
                "strike": 120,
                "expiration_ymd": "2026-08-21",
                "currency": "USD",
            },
        ]
    )

    assert len(rows) == 1
    assert rows[0]["position_side"] == "long"
    assert rows[0]["open_contracts"] == 1.0
    assert rows[0]["close_contracts"] == 1.0
    assert rows[0]["lifecycle_status"] == "closed"


def test_symbol_performance_attribution_uses_canonical_symbol_breakdown() -> None:
    rows = _canonical_symbol_performance_rows(_performance_report())

    assert len(rows) == 1
    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["sell_option_win_rate"] == 2 / 3
    assert rows[0]["option_net_cashflow_by_currency"]["USD"]["total"]["amount"] == 120.0


def test_strategy_config_by_symbol_account_expands_accounts_and_strategy_families() -> None:
    rows = _strategy_config_by_symbol_account_rows(
        [
            {
                "symbol": "FUTU",
                "broker": "富途",
                "accounts": ["lx", "sy"],
                "sell_put_enabled": True,
                "sell_put_max_strike": 120.0,
                "sell_put_min_annualized": 0.18,
                "sell_call_enabled": False,
                "sell_call_min_strike": 140.0,
                "sell_call_min_annualized": 0.12,
                "combo_yield_enabled": True,
            }
        ]
    )

    assert len(rows) == 6
    lx_put = next(row for row in rows if row["account"] == "lx" and row["strategy_family"] == "sell_put")
    sy_combo = next(row for row in rows if row["account"] == "sy" and row["strategy_family"] == "combo_yield")
    assert lx_put["enabled"] is True
    assert lx_put["max_strike"] == 120.0
    assert lx_put["min_annualized"] == 0.18
    assert sy_combo["enabled"] is True


def test_analysis_query_unknown_column_returns_structured_suggestions() -> None:
    with pytest.raises(AgentToolError) as exc:
        _execute_select(
            "select period_kind, net_cashflow from option_period_performance",
            {
                "option_period_performance": [
                    {"period_kind": "ytd", "option_net_cashflow_by_currency": {"USD": {}}},
                ]
            },
            limit=10,
        )

    assert exc.value.code == "INPUT_ERROR"
    assert exc.value.message == "analysis_query failed: unknown column net_cashflow"
    assert exc.value.details is not None
    assert exc.value.details["error_code"] == "UNKNOWN_COLUMN"
    assert exc.value.details["preflight"]["ok"] is False
    assert exc.value.details["preflight"]["error_code"] == "UNKNOWN_COLUMN"
    assert exc.value.details["unknown_column"] == "net_cashflow"
    assert exc.value.details["referenced_views"] == ["option_period_performance"]
    assert "option_net_cashflow_by_currency" in exc.value.details["suggestions"]


def test_analysis_query_executes_read_only_aggregates() -> None:
    rows, columns, views_used = _execute_select(
        "select state, sum(amount) as total from option_cash_components group by state",
        {
            "option_cash_components": [
                {"state": "open", "amount": 80.0},
                {"state": "open", "amount": 20.0},
            ]
        },
        limit=10,
    )

    assert columns == ["state", "total"]
    assert rows == [{"state": "open", "total": 100.0}]
    assert views_used == ["option_cash_components"]


def test_analysis_query_explain_warns_on_invalid_rate_aggregation() -> None:
    query_explain, warnings, evidence = _query_explain_and_evidence(
        sql=(
            "select period_kind, avg(sell_option_win_rate) as avg_rate "
            "from option_period_performance group by period_kind"
        ),
        rows=[{"period_kind": "ytd", "avg_rate": 0.12}],
        columns=["period_kind", "avg_rate"],
        views_used=["option_period_performance"],
    )

    assert query_explain["views_used"] == ["option_period_performance"]
    assert query_explain["grain"] == ["period_kind"]
    assert query_explain["aggregations"][0]["field"] == "sell_option_win_rate"
    assert query_explain["aggregations"][0]["policy"] == "invalid_rate_aggregation"
    assert warnings[0] == query_explain["aggregations"][0]["warning"]
    assert len(warnings) == 1
    assert evidence["aggregation_policy"][0]["status"] == "warning"
