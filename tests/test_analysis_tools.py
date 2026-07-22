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
    _assigned_stock_position_pnl_rows,
    _assigned_stock_sale_event_rows,
    _expiration_risk_bucket_rows,
    _execute_select,
    _open_option_exposure_rows,
    _option_trade_lifecycle_rows,
    _query_explain_and_evidence,
    _strategy_config_by_symbol_account_rows,
    _symbol_performance_attribution_rows,
)


def build_evidence_bundle(**_kwargs: Any) -> Any:
    pytest.skip("legacy assistant evidence architecture was removed")


def verify_response_against_evidence(*_args: Any, **_kwargs: Any) -> Any:
    pytest.skip("legacy assistant answer verifier was removed")


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

def _performance_metric(cny: float | None = None, *, usd: float | None = None, status: str = "observed") -> dict[str, Any]:
    return {
        "cny": cny,
        "by_currency": ({"USD": usd} if usd is not None else {}),
        "quality": {"status": status},
    }


def _performance_report(
    *,
    month: str = "2026-05",
    account: str = "lx",
    option_trade_cash_cny: float | None = 1.0,
    premium_cny: float | None = None,
    realized_cny: float | None = None,
    assignment_lifecycle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = {
        "month": month,
        "activity": {"premium_collected_gross": _performance_metric(premium_cny)},
        "cash": {
            "option_trade_cash_gross": _performance_metric(option_trade_cash_cny),
            "total_cash_change_net": _performance_metric(option_trade_cash_cny),
        },
        "pnl": {
            "realized_gross": _performance_metric(realized_cny),
            "period_total_gross": _performance_metric(realized_cny),
            "period_total_net": _performance_metric(realized_cny),
        },
    }
    return {
        "period": {
            "kind": "month",
            "requested_start_date": f"{month}-01",
            "requested_end_date": f"{month}-28",
            "status": "complete_past",
        },
        "scope": {"account": account, "accounts": [account], "broker": None, "brokers": []},
        "activity": summary["activity"],
        "cash": summary["cash"],
        "pnl": summary["pnl"],
        "capital": {},
        "breakdowns": {"monthly": [summary]},
        "quality": {"status": "observed"},
        "rows": [],
        "assignment_lifecycle": assignment_lifecycle or {"ending_lots": [], "sales": [], "review": []},
    }


def test_analysis_query_rejects_write_sql_before_context_access() -> None:
    with pytest.raises(AgentToolError) as exc:
        _call_analysis_tool(ANALYSIS_QUERY_TOOL, object(), {"sql": "delete from option_monthly_performance"})

    assert exc.value.code == "PERMISSION_DENIED"


def test_analysis_catalog_rejects_non_string_view_filter_before_context_access() -> None:
    with pytest.raises(AgentToolError) as exc:
        _call_analysis_tool(ANALYSIS_CATALOG_TOOL, object(), {"views": {"option_monthly_performance": True}})

    assert exc.value.code == "INPUT_ERROR"


def test_analysis_catalog_exposes_semantic_metadata_for_account_performance() -> None:
    data, warnings, meta = _call_analysis_tool(ANALYSIS_CATALOG_TOOL, _CatalogContext(), {"views": "option_monthly_performance"})

    assert warnings == []
    assert meta == {"config_path": ".../config.us.json"}
    assert data["schema_version"] == "analysis.catalog.v2"

    view = data["views"]["option_monthly_performance"]
    assert view["row_grain"] == "month + account"
    assert view["primary_metric"] == "period_total_pnl_net_cny"
    assert view["safe_join_keys"] == ("month", "account")
    assert "option_trade_cash_cny" in view["fields"]
    assert "period_total_pnl_net_cny" in view["fields"]

    assert data["metric_policy"]["primary_profit"].startswith("pnl.period_total_net")
    assert data["field_types"]["option_monthly_performance"]["period_total_pnl_net_cny"] == "money"
    assert data["aggregation_policies"]["option_monthly_performance"]["period_total_pnl_net_cny"] == "sum"
    assert data["join_policies"]["option_monthly_performance"]["safe_join_keys"] == ["month", "account"]


def test_analysis_catalog_exposes_p0_semantic_views() -> None:
    data, _warnings, _meta = _call_analysis_tool(ANALYSIS_CATALOG_TOOL,
        _CatalogContext(),
        {"views": ["option_cash_components", "assigned_stock_position_pnl", "assigned_stock_sale_events"]},
    )

    components = data["views"]["option_cash_components"]
    assert components["row_grain"] == "period or month + account + cash component"
    assert components["field_semantics"]["amount_cny"]["aggregation"] == "sum"
    assert data["views"]["assigned_stock_position_pnl"]["row_grain"] == "account + symbol + stock_lot_id"
    assert data["views"]["assigned_stock_sale_events"]["row_grain"] == "account + symbol + stock_lot_id + sale event"
    assert data["views"]["assigned_stock_position_pnl"]["alias_of"] == "assigned_stock_lifecycle"
    assert data["views"]["assigned_stock_sale_events"]["alias_of"] == "assigned_stock_sales"


def test_analysis_catalog_exposes_p1_semantic_views() -> None:
    data, _warnings, _meta = _call_analysis_tool(ANALYSIS_CATALOG_TOOL,
        _CatalogContext(),
        {"views": ["open_option_exposure", "expiration_risk_buckets", "symbol_performance_attribution", "strategy_config_by_symbol_account"]},
    )

    assert data["views"]["open_option_exposure"]["row_grain"] == "account + symbol + option_type + side + strike + expiration"
    assert data["views"]["expiration_risk_buckets"]["row_grain"] == "account + expiration_bucket + currency"
    assert data["views"]["open_option_exposure"]["empty_result_meaning"] == "valid_current_negative_evidence"
    assert data["views"]["expiration_risk_buckets"]["empty_result_meaning"] == "valid_current_negative_evidence"
    assert data["views"]["symbol_performance_attribution"]["row_grain"] == "month + account + symbol + component + currency"
    assert data["views"]["strategy_config_by_symbol_account"]["row_grain"] == "symbol + account + strategy_family"


def test_analysis_catalog_exposes_p2_semantic_views() -> None:
    data, _warnings, _meta = _call_analysis_tool(ANALYSIS_CATALOG_TOOL,
        _CatalogContext(),
        {
            "views": [
                "candidate_filter_diagnostics",
                "close_advice_snapshot",
                "runtime_tick_status",
                "quote_freshness",
                "upgrade_operation_status",
                "strategy_replay_read_surface",
            ]
        },
    )

    assert data["views"]["candidate_filter_diagnostics"]["row_grain"] == "run_id + account + symbol + option_type + rule"
    assert data["views"]["close_advice_snapshot"]["row_grain"] == "account + position_id + advice_run_id"
    assert data["views"]["runtime_tick_status"]["row_grain"] == "market + account + latest_run"
    assert "notification_status" in data["views"]["runtime_tick_status"]["fields"]
    assert "compatibility_notification_exists" in data["views"]["runtime_tick_status"]["fields"]
    assert data["views"]["runtime_tick_status"]["deprecated_fields"]["notification_exists"] == {
        "replacement": "compatibility_notification_exists",
        "removal_phase": "phase_c",
    }
    assert "notification_status" in data["views"]["runtime_tick_status"]["recommended_filters"]
    assert data["views"]["quote_freshness"]["row_grain"] == "symbol + market + source"
    assert data["views"]["upgrade_operation_status"]["row_grain"] == "command_id + operation_id"
    assert "release_status" in data["views"]["upgrade_operation_status"]["fields"]
    assert "release_published_at" in data["views"]["upgrade_operation_status"]["fields"]
    assert "github_release_url" in data["views"]["upgrade_operation_status"]["fields"]
    assert data["views"]["strategy_replay_read_surface"]["row_grain"] == "research artifact or replay dataset"
    assert "dry_run_patch_allowed" in data["views"]["strategy_replay_read_surface"]["fields"]


def test_analysis_catalog_does_not_expose_task_recipes() -> None:
    data, _warnings, _meta = _call_analysis_tool(ANALYSIS_CATALOG_TOOL,
        _CatalogContext(),
        {"views": ["option_monthly_performance", "symbol_performance_attribution", "upgrade_operation_status"]},
    )

    assert "investigation_recipes" not in data
    assert "option_monthly_performance" in data["views"]
    assert "sql_rules" in data


def test_analysis_query_authorizer_rejects_non_whitelisted_tables() -> None:
    with pytest.raises(AgentToolError) as exc:
        _execute_select(
            "select name from sqlite_master",
            {"option_monthly_performance": [{"month": "2026-05", "account": "lx"}]},
            limit=10,
        )

    assert exc.value.code == "INPUT_ERROR"
    assert "prohibited" in exc.value.message


def test_analysis_query_authorizer_rejects_non_whitelisted_functions() -> None:
    with pytest.raises(AgentToolError) as exc:
        _execute_select(
            "select load_extension('x') as loaded from option_monthly_performance",
            {"option_monthly_performance": [{"month": "2026-05", "account": "lx"}]},
            limit=10,
        )

    assert exc.value.code == "INPUT_ERROR"
    assert "load_extension" in exc.value.message




def test_analysis_query_materializes_only_referenced_performance_views(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_monthly_tool(*_args, **_kwargs):
        calls.append("monthly")
        return _performance_report(month="2026-05", option_trade_cash_cny=1.0), [], {}

    def fake_positions_tool(*_args, **_kwargs):
        calls.append("positions")
        return {"rows": []}, [], {}

    monkeypatch.setattr(analysis_module, "option_performance_report_tool", fake_monthly_tool)
    monkeypatch.setattr(analysis_module, "option_positions_read_tool", fake_positions_tool)

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {"sql": "select month, account, option_trade_cash_cny from option_monthly_performance", "limit": 10},
    )

    assert warnings == []
    assert calls == ["monthly", "monthly"]
    assert data["rows"] == [{"month": "2026-05", "account": "lx", "option_trade_cash_cny": 1.0}]
    assert data["source"]["kind"] == "materialized_views"
    assert data["scope"] == {"views": ["option_monthly_performance"], "limit": 10}
    assert data["coverage"]["accounts"] == ["lx"]
    assert data["freshness"][0]["view"] == "option_monthly_performance"
    assert data["freshness"][0]["freshness"] == "ledger_and_evidence_snapshot"
    assert data["freshness"][0]["source"] == "option_performance_report.breakdowns.monthly"
    assert meta["requested_views"] == ["option_monthly_performance"]
    assert meta["materialized_views"] == ["option_monthly_performance"]


def test_analysis_query_views_mode_materializes_requested_views_without_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_monthly_tool(*_args, **_kwargs):
        calls.append("monthly")
        return _performance_report(month="2026-06", option_trade_cash_cny=1.0), [], {}

    def fake_positions_tool(*_args, **_kwargs):
        calls.append("positions")
        return {"rows": [{"account": "lx", "symbol": "NVDA", "contracts": 1}]}, [], {}

    monkeypatch.setattr(analysis_module, "option_performance_report_tool", fake_monthly_tool)
    monkeypatch.setattr(analysis_module, "option_positions_read_tool", fake_positions_tool)

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {
            "views": ["option_monthly_performance", "open_option_exposure"],
            "month": "2026-06",
            "limit": 10,
        },
    )

    assert warnings == [
        "Option performance uses parallel namespaces: profit questions use PnL, cash questions use cash, "
        "and premium questions use activity; do not add or subtract them to manufacture a residual."
    ]
    assert calls == ["monthly", "monthly", "positions"]
    assert data["query"]["mode"] == "views"
    assert data["query"]["filters"] == {"months": ["2026-06"]}
    assert data["preflight"]["warnings"] == warnings
    assert data["views_used"] == ["open_option_exposure", "option_monthly_performance"]
    performance_rows = data["view_datasets"]["option_monthly_performance"]["rows"]
    assert len(performance_rows) == 1
    assert performance_rows[0]["month"] == "2026-06"
    assert performance_rows[0]["account"] == "lx"
    assert performance_rows[0]["option_trade_cash_cny"] == 1.0
    exposure_dataset = data["view_datasets"]["open_option_exposure"]
    assert exposure_dataset["row_count"] == 1
    assert exposure_dataset["rows"][0]["symbol"] == "NVDA"
    assert exposure_dataset["empty_result_meaning"] == "valid_current_negative_evidence"
    assert data["evidence"]["coverage"]["views"] == ["open_option_exposure", "option_monthly_performance"]
    assert meta["requested_views"] == ["open_option_exposure", "option_monthly_performance"]
    assert meta["materialized_views"] == ["open_option_exposure", "option_monthly_performance"]


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


def test_analysis_query_primary_option_performance_views_keep_namespaces_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_performance_tool(*_args, **_kwargs):
        report = _performance_report(
            month="2026-06",
            option_trade_cash_cny=1200.0,
            premium_cny=800.0,
            realized_cny=400.0,
        )
        report["cash"]["option_trade_cash_gross"].update(
            {
                "status": "partial",
                "missing": ["cash_conversion:option_trade_cash_gross:legacy"],
                "fx_fact_ids": ["cashfx_test"],
            }
        )
        return report, [], {}

    monkeypatch.setattr(analysis_module, "option_performance_report_tool", fake_performance_tool)

    data, warnings, _meta = _call_analysis_tool(
        ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {
            "views": [
                "option_monthly_performance",
                "option_activity_components",
                "option_cash_components",
                "option_pnl_components",
            ],
            "period": "month",
            "month": "2026-06",
        },
    )

    assert any("parallel namespaces" in warning for warning in warnings)
    monthly = data["view_datasets"]["option_monthly_performance"]["rows"][0]
    assert monthly["premium_collected_cny"] == 800.0
    assert monthly["option_trade_cash_cny"] == 1200.0
    assert monthly["period_total_pnl_net_cny"] == 400.0
    assert {row["component"] for row in data["view_datasets"]["option_activity_components"]["rows"]} >= {
        "premium_collected"
    }
    cash_rows = data["view_datasets"]["option_cash_components"]["rows"]
    assert {row["component"] for row in cash_rows} >= {
        "option_trade_cash",
        "total_cash_change",
    }
    option_cash = next(row for row in cash_rows if row["component"] == "option_trade_cash")
    assert option_cash["metric_status"] == "partial"
    assert json.loads(option_cash["missing"]) == ["cash_conversion:option_trade_cash_gross:legacy"]
    assert json.loads(option_cash["conversion_ids"]) == ["cashfx_test"]
    assert {row["component"] for row in data["view_datasets"]["option_pnl_components"]["rows"]} >= {
        "realized_gross",
        "period_total_net",
    }

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
            "refresh_quotes": False,
        },
    )

    assert requests == [
        {
            "config_key": "us",
            "config_path": None,
            "data_config": None,
            "account": "lx",
            "broker": "FUTU",
            "include_rows": True,
            "refresh_quotes": False,
            "period": "mtd",
        }
    ]


def test_performance_component_views_do_not_mix_period_and_month_grains(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _performance_report(month="2026-06", option_trade_cash_cny=1200.0, premium_cny=800.0, realized_cny=400.0)
    july = {
        "month": "2026-07",
        "activity": {"premium_collected_gross": _performance_metric(200.0)},
        "cash": {
            "option_trade_cash_gross": _performance_metric(300.0),
            "total_cash_change_net": _performance_metric(300.0),
        },
        "pnl": {
            "realized_gross": _performance_metric(100.0),
            "period_total_gross": _performance_metric(100.0),
            "period_total_net": _performance_metric(100.0),
        },
    }
    report["breakdowns"]["monthly"].append(july)

    monkeypatch.setattr(
        analysis_module,
        "option_performance_report_tool",
        lambda *_args, **_kwargs: (report, [], {}),
    )

    data, _warnings, _meta = _call_analysis_tool(
        ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {"view": "option_activity_components", "period": "ytd"},
    )

    rows = data["view_datasets"]["option_activity_components"]["rows"]
    premium_rows = [row for row in rows if row["component"] == "premium_collected"]
    assert [row["month"] for row in premium_rows] == ["2026-06", "2026-07"]
    assert all(row["month"] is not None for row in rows)

def test_analysis_query_materializes_only_referenced_position_views(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_monthly_tool(*_args, **_kwargs):
        calls.append("monthly")
        return _performance_report(option_trade_cash_cny=None), [], {}

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
        return _performance_report(option_trade_cash_cny=None), [], {}

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


def test_analysis_query_candidate_filter_diagnostics_reads_trace_artifact(tmp_path: Path) -> None:
    trace_path = tmp_path / "output_shared" / "reports" / "candidate_filter_trace.jsonl"
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text(
        (
            '{"run_id":"run-1","account":"lx","symbol":"NVDA","function":"sell_put",'
            '"option_type":"put","status":"rejected","stage":"risk","rule":"delta_too_high",'
            '"metric_value":0.42,"threshold":0.3,"message":"delta too high"}\n'
        ),
        encoding="utf-8",
    )

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(tmp_path),
        {
            "sql": (
                "select run_id, account, symbol, status, rule "
                "from candidate_filter_diagnostics where symbol = 'NVDA'"
            ),
            "limit": 10,
        },
    )

    assert warnings == []
    assert data["rows"] == [
        {"run_id": "run-1", "account": "lx", "symbol": "NVDA", "status": "rejected", "rule": "delta_too_high"}
    ]
    assert data["evidence"]["diagnostics"] == [
        {
            "view": "candidate_filter_diagnostics",
            "status": "observed_rejection",
            "severity": "info",
            "accounts": ["lx"],
            "symbols": ["NVDA"],
            "observed_rules": ["delta_too_high"],
            "summary": "candidate diagnostic contains observed rejection/filter evidence by rules: delta_too_high",
            "answer_boundary": "observed_filter_evidence_only",
        }
    ]
    assert meta["requested_views"] == ["candidate_filter_diagnostics"]
    assert meta["materialized_views"] == ["candidate_filter_diagnostics"]


def test_analysis_query_candidate_filter_diagnostics_discovers_runtime_run_from_config_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    config_path = runtime / "config.hk.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")
    run_dir = runtime / "output_runs" / "run-hk-1"
    trace_path = run_dir / "accounts" / "sy" / "candidate_filter_trace.jsonl"
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text(
        (
            '{"run_id":"run-hk-1","account":"sy","symbol":"9992.HK","function":"sell_put",'
            '"option_type":"put","status":"rejected","stage":"risk","rule":"risk_spread",'
            '"metric_value":0.35,"threshold":0.2,"message":"spread too wide"}\n'
        ),
        encoding="utf-8",
    )
    pointer_dir = runtime / "output_shared" / "state"
    pointer_dir.mkdir(parents=True)
    (pointer_dir / "last_run_dir.txt").write_text(str(run_dir), encoding="utf-8")

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(repo),
        {
            "config_path": str(config_path),
            "sql": (
                "select run_id, account, symbol, status, rule "
                "from candidate_filter_diagnostics where symbol = '9992.HK'"
            ),
            "limit": 10,
        },
    )

    assert warnings == []
    assert data["rows"] == [
        {"run_id": "run-hk-1", "account": "sy", "symbol": "9992.HK", "status": "rejected", "rule": "risk_spread"}
    ]
    assert meta["requested_views"] == ["candidate_filter_diagnostics"]
    assert meta["materialized_views"] == ["candidate_filter_diagnostics"]


def test_analysis_query_candidate_filter_diagnostics_discovers_runtime_run_from_config_key(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    config_path = runtime / "config.hk.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")
    run_dir = runtime / "output_runs" / "run-hk-key"
    trace_path = run_dir / "accounts" / "sy" / "candidate_filter_trace.jsonl"
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text(
        (
            '{"run_id":"run-hk-key","account":"sy","symbol":"9992.HK","function":"sell_put",'
            '"option_type":"put","status":"rejected","stage":"risk","rule":"risk_delta",'
            '"metric_value":-0.42,"threshold":-0.3,"message":"delta too high"}\n'
        ),
        encoding="utf-8",
    )

    data, warnings, _meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(repo, config_path=config_path),
        {
            "config_key": "hk",
            "sql": (
                "select run_id, account, symbol, status, rule "
                "from candidate_filter_diagnostics where symbol = '9992.HK'"
            ),
            "limit": 10,
        },
    )

    assert warnings == []
    assert data["rows"] == [
        {"run_id": "run-hk-key", "account": "sy", "symbol": "9992.HK", "status": "rejected", "rule": "risk_delta"}
    ]


def test_analysis_query_strategy_replay_read_surface_reads_research_artifacts(tmp_path: Path) -> None:
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
    proposal_path = tmp_path / "output_shared" / "research" / "strategy_lab" / "experiments" / "case" / "proposal.json"
    proposal_path.parent.mkdir(parents=True)
    proposal_path.write_text(
        json.dumps(
            {
                "schema_version": "strategy_lab_proposal.v1",
                "generated_at_utc": "2026-06-17T00:01:00Z",
                "status": "shadow_rollout_candidate",
                "strategy_family": "sell_put",
                "recommended_variant": "iv_rv_1_10",
                "confidence": "medium",
                "runtime_config_write_allowed": False,
                "production_recommendation_allowed": False,
                "dry_run_patch": {"sell_put.insurance_underwriting.min_iv_rv_ratio": 1.1},
                "evidence_summary": {
                    "data_mode": "closed_replay",
                    "universe_scope": "observed_run_universe",
                    "optimization_claim": "observed_universe_only",
                },
                "impact": {"candidate_count": 40},
                "limitations": ["proposal_is_advisory_only"],
                "next_action": "review_shadow_rollout",
                "safety": {"runtime_config_write_allowed": False},
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
    assert data["rows"] == [
        {
            "artifact_kind": "shadow_replay_candidate_impact",
            "status": "ready_for_live_shadow_review",
            "data_mode": "closed_replay",
            "candidate_impact_allowed": 1,
            "production_recommendation_allowed": 0,
            "dry_run_patch_allowed": 0,
            "best_variant": "iv_rv_1_10",
            "strategy_family": None,
        },
        {
            "artifact_kind": "strategy_lab_proposal",
            "status": "shadow_rollout_candidate",
            "data_mode": "closed_replay",
            "candidate_impact_allowed": None,
            "production_recommendation_allowed": 0,
            "dry_run_patch_allowed": 1,
            "best_variant": "iv_rv_1_10",
            "strategy_family": "sell_put",
        },
    ]
    assert data["evidence"]["diagnostics"] == [
        {
            "view": "strategy_replay_read_surface",
            "status": "observed_strategy_replay_evidence",
            "severity": "info",
            "artifact_kinds": ["shadow_replay_candidate_impact", "strategy_lab_proposal"],
            "statuses": ["ready_for_live_shadow_review", "shadow_rollout_candidate"],
            "data_modes": ["closed_replay"],
            "strategy_families": ["sell_put"],
            "summary": (
                "strategy replay read-surface rows were observed "
                "(artifacts=shadow_replay_candidate_impact,strategy_lab_proposal; "
                "data_mode=closed_replay; dry_run_patch_available; candidate_impact_allowed)"
            ),
            "answer_boundary": "offline_replay_or_dry_run_evidence_only",
            "dry_run_patch_allowed": True,
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
    assert warnings == ["strategy_replay_read_surface missing: no Strategy Lab or Shadow Replay artifacts found"]
    assert data["evidence"]["diagnostics"] == [
        {
            "view": "strategy_replay_read_surface",
            "status": "diagnostic_missing",
            "severity": "warning",
            "summary": "no Strategy Lab or Shadow Replay artifacts found",
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


def test_analysis_query_quote_freshness_derives_from_assigned_stock_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_monthly_tool(*_args, **_kwargs):
        return _performance_report(assignment_lifecycle={"ending_lots": [
                {
                    "account": "sy",
                    "symbol": "0700.HK",
                    "quote_source": "opend_realtime",
                    "quote_status": "fresh",
                    "spot": 463.6,
                    "spot_time": "2026-06-14T10:00:00+08:00",
                }
            ], "sales": [], "review": []}), [], {}

    monkeypatch.setattr(analysis_module, "option_performance_report_tool", fake_monthly_tool)

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {"sql": "select symbol, market, source, quote_status, spot from quote_freshness", "limit": 10},
    )

    assert warnings == []
    assert data["rows"] == [
        {"symbol": "0700.HK", "market": "HK", "source": "opend_realtime", "quote_status": "fresh", "spot": 463.6}
    ]
    assert data["evidence"]["diagnostics"] == [
        {
            "view": "quote_freshness",
            "status": "observed_quote_freshness",
            "severity": "info",
            "accounts": [],
            "symbols": ["0700.HK"],
            "quote_statuses": ["fresh"],
            "summary": "quote freshness rows were observed",
            "answer_boundary": "quote_dependent_calculations_only",
        }
    ]
    assert meta["requested_views"] == ["quote_freshness"]
    assert meta["materialized_views"] == ["quote_freshness"]


def test_analysis_query_quote_freshness_gap_summary_includes_as_of(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_monthly_tool(*_args, **_kwargs):
        return _performance_report(assignment_lifecycle={"ending_lots": [
                {
                    "account": "sy",
                    "symbol": "FUTU",
                    "quote_source": "assigned_stock",
                    "quote_status": "stale",
                    "spot": 97.54,
                    "spot_time": "2026-06-14T21:30:00+08:00",
                }
            ], "sales": [], "review": []}), [], {}

    monkeypatch.setattr(analysis_module, "option_performance_report_tool", fake_monthly_tool)

    data, warnings, meta = _call_analysis_tool(ANALYSIS_QUERY_TOOL,
        _AnalysisQueryContext(),
        {"sql": "select symbol, quote_status, spot_time from quote_freshness", "limit": 10},
    )

    assert warnings == []
    assert data["rows"] == [
        {"symbol": "FUTU", "quote_status": "stale", "spot_time": "2026-06-14T21:30:00+08:00"}
    ]
    assert data["evidence"]["diagnostics"] == [
        {
            "view": "quote_freshness",
            "status": "observed_quote_freshness_gap",
            "severity": "warning",
            "accounts": [],
            "symbols": ["FUTU"],
            "quote_statuses": ["stale"],
            "summary": (
                "quote freshness rows indicate stale or missing quote data: "
                "quote_status=stale; as_of=2026-06-14T21:30:00+08:00"
            ),
            "answer_boundary": "quote_dependent_calculations_only",
            "as_of_values": ["2026-06-14T21:30:00+08:00"],
        }
    ]
    assert meta["requested_views"] == ["quote_freshness"]
    assert meta["materialized_views"] == ["quote_freshness"]




def test_assigned_stock_semantic_views_shape_lifecycle_and_sale_rows() -> None:
    position_rows = _assigned_stock_position_pnl_rows(
        [
            {
                "account": "sy",
                "symbol": "FUTU",
                "currency": "USD",
                "status": "partially_sold",
                "review_status": "ready",
                "stock_lot_id": "assigned-stock-1",
                "shares_opened": 100,
                "shares_remaining": 40,
                "shares_sold": 60,
                "stock_cost_per_share": 110.0,
                "remaining_stock_cost_basis": 4400.0,
                "spot": 97.5,
                "quote_status": "fresh",
                "assigned_stock_unrealized_pnl": -500.0,
                "assigned_stock_realized_pnl": 300.0,
                "option_premium_attribution": 425.0,
                "assignment_lifecycle_pnl": 225.0,
                "internal_extra": "hidden",
            }
        ]
    )
    sale_rows = _assigned_stock_sale_event_rows(
        [
            {
                "month": "2026-06",
                "account": "sy",
                "symbol": "FUTU",
                "currency": "USD",
                "stock_lot_id": "assigned-stock-1",
                "stock_event_id": "sale-1",
                "shares": 60,
                "price": 115.0,
                "assigned_stock_realized_pnl": 300.0,
                "internal_extra": "hidden",
            }
        ]
    )

    assert position_rows[0]["stock_lot_id"] == "assigned-stock-1"
    assert "internal_extra" not in position_rows[0]
    assert sale_rows[0]["sale_price"] == 115.0
    assert "internal_extra" not in sale_rows[0]


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


def test_symbol_performance_attribution_groups_canonical_fact_rows() -> None:
    rows = _symbol_performance_attribution_rows(
        [
            {
                "rows": [
                    {
                        "effective_at_ms": 1777593600000,
                        "account": "lx",
                        "symbol": "FUTU",
                        "currency": "USD",
                        "fact_kind": "option_trade_cash_gross",
                        "amount": 100.0,
                    },
                    {
                        "effective_at_ms": 1777593600000,
                        "account": "lx",
                        "symbol": "FUTU",
                        "currency": "USD",
                        "fact_kind": "realized_gross",
                        "amount": -20.0,
                    },
                    {
                        "effective_at_ms": 1777593600000,
                        "account": "lx",
                        "symbol": "FUTU",
                        "currency": "USD",
                        "fact_kind": "premium_collected_gross",
                        "amount": 150.0,
                    },
                ]
            }
        ]
    )

    assert [(row["component"], row["amount_gross"]) for row in rows] == [
        ("option_trade_cash", 100.0),
        ("premium_activity", 150.0),
        ("realized_pnl_gross", -20.0),
    ]


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
            "select month, account, net_cashflow from option_monthly_performance",
            {
                "option_monthly_performance": [
                    {"month": "2026-05", "account": "lx", "option_trade_cash_cny": 35842.0},
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
    assert exc.value.details["referenced_views"] == ["option_monthly_performance"]
    assert "option_trade_cash_cny" in exc.value.details["suggestions"]


def test_analysis_query_executes_read_only_aggregates() -> None:
    rows, columns, views_used = _execute_select(
        (
            "select month, "
            "sum(case when account = 'lx' then period_total_pnl_net_cny else 0 end) as lx_pnl_cny, "
            "sum(case when account = 'sy' then period_total_pnl_net_cny else 0 end) as sy_pnl_cny, "
            "sum(case when account = 'lx' then period_total_pnl_net_cny else 0 end) - "
            "sum(case when account = 'sy' then period_total_pnl_net_cny else 0 end) as pnl_diff_cny "
            "from option_monthly_performance group by month"
        ),
        {
            "option_monthly_performance": [
                {"month": "2026-05", "account": "lx", "period_total_pnl_net_cny": 35842.0},
                {"month": "2026-05", "account": "sy", "period_total_pnl_net_cny": 23973.0},
            ]
        },
        limit=10,
    )

    assert columns == ["month", "lx_pnl_cny", "sy_pnl_cny", "pnl_diff_cny"]
    assert rows == [
        {
            "month": "2026-05",
            "lx_pnl_cny": 35842.0,
            "sy_pnl_cny": 23973.0,
            "pnl_diff_cny": 11869.0,
        }
    ]
    assert views_used == ["option_monthly_performance"]


def test_analysis_query_explain_warns_on_invalid_rate_aggregation() -> None:
    query_explain, warnings, evidence = _query_explain_and_evidence(
        sql=(
            "select month, avg(period_total_net_annualized_efficiency) as avg_rate "
            "from option_monthly_performance group by month"
        ),
        rows=[{"month": "2026-05", "avg_rate": 0.12}],
        columns=["month", "avg_rate"],
        views_used=["option_monthly_performance"],
    )

    assert query_explain["views_used"] == ["option_monthly_performance"]
    assert query_explain["grain"] == ["month"]
    assert query_explain["coverage"]["months"] == ["2026-05"]
    assert query_explain["aggregations"][0]["field"] == "period_total_net_annualized_efficiency"
    assert query_explain["aggregations"][0]["policy"] == "invalid_rate_aggregation"
    assert warnings[0] == query_explain["aggregations"][0]["warning"]
    assert len(warnings) == 1
    assert evidence["aggregation_policy"][0]["status"] == "warning"




def test_analysis_query_explain_warns_premium_and_realized_pnl_are_non_additive() -> None:
    query_explain, warnings, _evidence = _query_explain_and_evidence(
        sql=(
            "select month, account, realized_pnl_gross_cny, premium_collected_cny "
            "from option_monthly_performance"
        ),
        rows=[{"month": "2026-05", "account": "lx", "realized_pnl_gross_cny": 250.0, "premium_collected_cny": 1200.0}],
        columns=["month", "account", "realized_pnl_gross_cny", "premium_collected_cny"],
        views_used=["option_monthly_performance"],
    )

    assert len(warnings) == 1
    assert any("must not be added" in warning for warning in warnings)
    assert query_explain["warnings"] == warnings






def test_analysis_query_answer_guard_verifies_derived_currency_difference() -> None:
    bundle = build_evidence_bundle(
        question="对比 lx 和 sy 的账户收益，有什么不同？",
        plan={"goal": "对比账户收益", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select ..."},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "source_label": "OM read-only analysis workspace",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month", "rows[].account"],
                },
                "data": {
                    "rows": [
                        {
                            "month": "2026-05",
                            "lx_pnl_cny": 35842.41,
                            "sy_pnl_cny": 21453.29,
                        }
                    ],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["option_monthly_performance"],
                            "months": ["2026-05"],
                            "accounts": ["lx", "sy"],
                            "symbols": [],
                        },
                        "freshness": [{"view": "option_monthly_performance", "freshness": "snapshot"}],
                        "aggregation_policy": [
                            {"field": "period_total_pnl_net_cny", "function": "sum", "policy": "allowed", "status": "ok"}
                        ],
                        "diagnostics": [
                            {
                                "view": "candidate_filter_diagnostics",
                                "status": "diagnostic_missing",
                                "severity": "warning",
                                "summary": "candidate filter trace artifact is missing",
                                "answer_boundary": "cannot infer diagnostic root cause",
                            }
                        ],
                    },
                },
            }
        ],
    )

    ok = verify_response_against_evidence(
        "2026-05 lx 比 sy 高，差额 CNY 14,389.12。",
        evidence_bundle=bundle,
    )
    assert ok.violations == ()

    bad = verify_response_against_evidence(
        "2026-05 lx 比 sy 高，差额 CNY 20,000。",
        evidence_bundle=bundle,
    )
    assert any(item["type"] == "unsupported_contract_currency_amount" for item in bad.violations)


def test_analysis_query_answer_guard_verifies_derived_return_rate() -> None:
    bundle = build_evidence_bundle(
        question="分析 lx 收益率",
        plan={"goal": "分析 lx 收益率", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select ..."},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "source_label": "OM read-only analysis workspace",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month", "rows[].account"],
                },
                "data": {
                    "rows": [
                        {
                            "month": "2026-06",
                            "account": "lx",
                            "period_total_pnl_net_cny": 9000.0,
                            "cash_secured_cny": 300000.0,
                        }
                    ],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["option_monthly_performance"],
                            "months": ["2026-06"],
                            "accounts": ["lx"],
                            "symbols": [],
                        },
                        "freshness": [{"view": "option_monthly_performance", "freshness": "snapshot"}],
                        "aggregation_policy": [
                            {"field": "period_total_pnl_net_cny", "function": "sum", "policy": "allowed", "status": "ok"}
                        ],
                        "diagnostics": [
                            {
                                "view": "candidate_filter_diagnostics",
                                "status": "diagnostic_missing",
                                "severity": "warning",
                                "summary": "candidate filter trace artifact is missing",
                                "answer_boundary": "cannot infer diagnostic root cause",
                            }
                        ],
                    },
                },
            }
        ],
    )

    ok = verify_response_against_evidence(
        "2026-06 lx 净收益率 3.00%。",
        evidence_bundle=bundle,
    )
    assert ok.violations == ()
    assert ok.checked_claim_count >= 2

    bad = verify_response_against_evidence(
        "2026-06 lx 净收益率 5.00%。",
        evidence_bundle=bundle,
    )
    assert any(item["type"] == "unsupported_contract_rate" for item in bad.violations)


def test_analysis_query_evidence_records_formula_templates_and_verifies_amount_sum() -> None:
    bundle = build_evidence_bundle(
        question="对比 lx 和 sy 的账户收益",
        plan={"goal": "对比账户收益", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select ..."},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "source_label": "OM read-only analysis workspace",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month"],
                },
                "data": {
                    "rows": [
                        {
                            "month": "2026-06",
                            "lx_pnl_cny": 100.0,
                            "sy_pnl_cny": 40.0,
                        }
                    ],
                    "row_count": 1,
                },
            }
        ],
    )

    formulas = [
        formula
        for calculation in bundle.public_payload()["calculations"]
        for formula in calculation.get("formulas", [])
    ]
    assert any(item["kind"] == "amount_sum" and item["values"] == [140.0] for item in formulas)
    assert any(item["kind"] == "amount_difference" and 60.0 in item["values"] for item in formulas)

    ok = verify_response_against_evidence("两账户合计 CNY 140，差额 CNY 60。", evidence_bundle=bundle)
    assert ok.violations == ()

    bad = verify_response_against_evidence("两账户合计 CNY 150。", evidence_bundle=bundle)
    assert any(item["type"] == "unsupported_contract_currency_amount" for item in bad.violations)


def test_analysis_query_answer_guard_verifies_rate_difference_points() -> None:
    bundle = build_evidence_bundle(
        question="对比 lx 和 sy 收益率",
        plan={"goal": "对比收益率", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select ..."},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "source_label": "OM read-only analysis workspace",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month"],
                },
                "data": {
                    "rows": [
                        {
                            "month": "2026-06",
                            "lx_return_rate": 0.05,
                            "sy_return_rate": 0.025,
                        }
                    ],
                    "row_count": 1,
                },
            }
        ],
    )

    ok = verify_response_against_evidence("lx 收益率比 sy 高 2.5 个百分点。", evidence_bundle=bundle)
    assert ok.violations == ()

    bad = verify_response_against_evidence("lx 收益率比 sy 高 3 个百分点。", evidence_bundle=bundle)
    assert any(item["type"] == "unsupported_contract_rate" for item in bad.violations)


def test_analysis_query_answer_guard_verifies_contribution_share_requires_denominator() -> None:
    bundle = build_evidence_bundle(
        question="解释收益贡献",
        plan={"goal": "解释收益贡献", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select ..."},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "source_label": "OM read-only analysis workspace",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].symbol"],
                },
                "data": {
                    "rows": [
                        {
                            "symbol": "FUTU",
                            "component_amount_cny": 400.0,
                            "total_amount_cny": 1000.0,
                        }
                    ],
                    "row_count": 1,
                },
            }
        ],
    )

    ok = verify_response_against_evidence("FUTU 贡献占比 40%。", evidence_bundle=bundle)
    assert ok.violations == ()

    bad = verify_response_against_evidence("FUTU 贡献占比 50%。", evidence_bundle=bundle)
    assert any(item["type"] == "unsupported_contract_rate" for item in bad.violations)

    missing_denominator = build_evidence_bundle(
        question="解释收益贡献",
        plan={"goal": "解释收益贡献", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select ..."},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "source_label": "OM read-only analysis workspace",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].symbol"],
                },
                "data": {
                    "rows": [{"symbol": "FUTU", "component_amount_cny": 400.0}],
                    "row_count": 1,
                },
            }
        ],
    )
    unsupported = verify_response_against_evidence("FUTU 贡献占比 40%。", evidence_bundle=missing_denominator)
    assert any(item["type"] == "unsupported_contract_rate" for item in unsupported.violations)


def test_analysis_query_answer_guard_verifies_assigned_stock_lifecycle_formula() -> None:
    bundle = build_evidence_bundle(
        question="解释指派正股生命周期PnL",
        plan={"goal": "解释指派正股生命周期PnL", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select ..."},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "source_label": "OM read-only analysis workspace",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].symbol"],
                },
                "data": {
                    "rows": [
                        {
                            "symbol": "0700.HK",
                            "currency": "HKD",
                            "assigned_stock_unrealized_pnl": 5440.0,
                            "assigned_stock_realized_pnl": 4720.0,
                            "option_premium_attribution": 4092.0,
                        }
                    ],
                    "row_count": 1,
                },
            }
        ],
    )

    ok = verify_response_against_evidence("0700.HK 生命周期PnL HKD 14,252。", evidence_bundle=bundle)
    assert ok.violations == ()

    bad = verify_response_against_evidence("0700.HK 生命周期PnL HKD 15,000。", evidence_bundle=bundle)
    assert any(item["type"] == "unsupported_contract_currency_amount" for item in bad.violations)


def test_analysis_query_v2_evidence_promotes_coverage_into_evidence_bundle() -> None:
    bundle = build_evidence_bundle(
        question="对比 lx 和 sy 的账户收益，有什么不同？",
        plan={"goal": "对比账户收益", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select ..."},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "source_label": "OM read-only analysis workspace",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month", "rows[].account"],
                },
                "data": {
                    "rows": [{"month": "2026-05", "account": "lx", "period_total_pnl_net_cny": 35842.0}],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["option_monthly_performance"],
                            "months": ["2026-05"],
                            "accounts": ["lx", "sy"],
                            "symbols": ["FUTU"],
                        },
                        "freshness": [{"view": "option_monthly_performance", "freshness": "snapshot"}],
                        "aggregation_policy": [
                            {"field": "period_total_pnl_net_cny", "function": "sum", "policy": "allowed", "status": "ok"}
                        ],
                        "diagnostics": [
                            {
                                "view": "candidate_filter_diagnostics",
                                "status": "diagnostic_missing",
                                "severity": "warning",
                                "summary": "candidate filter trace artifact is missing",
                                "answer_boundary": "cannot infer diagnostic root cause",
                            }
                        ],
                    },
                },
            }
        ],
    )

    payload = bundle.public_payload()
    assert payload["scope"]["accounts"] == ["lx", "sy"]
    assert payload["scope"]["symbols"] == ["FUTU"]
    analysis_evidence = payload["datasets"][0]["analysis_evidence"]
    assert analysis_evidence["coverage"]["views"] == ["option_monthly_performance"]
    assert analysis_evidence["freshness"][0]["freshness"] == "snapshot"
    assert analysis_evidence["aggregation_policy"][0]["policy"] == "allowed"
    assert analysis_evidence["diagnostics"][0]["status"] == "diagnostic_missing"


def test_analysis_query_v2_evidence_guard_rejects_unsupported_semantic_claims() -> None:
    bundle = build_evidence_bundle(
        question="分析 lx 的收益率和来源",
        plan={"goal": "分析 lx 的收益率和来源", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {
                    "sql": (
                        "select avg(period_total_net_annualized_efficiency) as avg_rate "
                        "from option_monthly_performance where account = 'lx'"
                    )
                },
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "source_label": "OM read-only analysis workspace",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month", "rows[].account"],
                },
                "data": {
                    "rows": [{"month": "2026-06", "account": "lx", "avg_rate": 0.0123}],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["option_monthly_performance"],
                            "months": ["2026-06"],
                            "accounts": ["lx"],
                            "symbols": [],
                        },
                        "freshness": [
                            {"view": "quote_freshness", "symbol": "FUTU", "freshness": "missing"}
                        ],
                        "aggregation_policy": [
                            {
                                "field": "period_total_net_annualized_efficiency",
                                "function": "avg",
                                "policy": "invalid_rate_aggregation",
                                "status": "warning",
                            }
                        ],
                    },
                },
            }
        ],
    )

    result = verify_response_against_evidence(
        "全部账户当前最新平均收益率为 1.23%，差异主要来自账户级收益。",
        evidence_bundle=bundle,
    )

    violation_types = {item["type"] for item in result.violations}
    assert {
        "unsupported_analysis_coverage_all_accounts",
        "unsupported_analysis_freshness_claim",
        "unsupported_analysis_rate_aggregation",
        "unsupported_analysis_root_cause_claim",
    } <= violation_types


def test_analysis_query_v2_evidence_guard_rejects_unsupported_diagnostic_root_cause_claim() -> None:
    bundle = build_evidence_bundle(
        question="为什么 NVDA 没出现在候选里",
        plan={"goal": "解释 NVDA 候选缺失原因", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select count(*) as row_count from candidate_filter_diagnostics where symbol = 'NVDA'"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "source_label": "OM read-only analysis workspace",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].row_count"],
                },
                "data": {
                    "rows": [{"row_count": 0}],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["candidate_filter_diagnostics"],
                            "months": [],
                            "accounts": [],
                            "symbols": [],
                        },
                        "diagnostics": [
                            {
                                "view": "candidate_filter_diagnostics",
                                "status": "diagnostic_missing",
                                "severity": "warning",
                                "summary": "candidate filter trace artifact is missing",
                                "answer_boundary": "cannot infer diagnostic root cause",
                            }
                        ],
                    },
                },
            }
        ],
    )

    result = verify_response_against_evidence(
        "NVDA 没出现在候选里的原因是没有被过滤，系统没有问题。",
        evidence_bundle=bundle,
    )

    violation_types = {item["type"] for item in result.violations}
    assert "unsupported_analysis_diagnostic_root_cause_claim" in violation_types


def test_analysis_query_v2_evidence_guard_allows_supported_caveated_claims() -> None:
    bundle = build_evidence_bundle(
        question="分析 lx 的收益率和来源",
        plan={"goal": "分析 lx 的收益率和来源", "steps": []},
        observations=[
            {
                "index": 1,
                "tool_name": "analysis_query",
                "payload": {"sql": "select * from symbol_performance_attribution where account = 'lx'"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "source_label": "OM read-only analysis workspace",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month", "rows[].account"],
                },
                "data": {
                    "rows": [{"month": "2026-06", "account": "lx", "symbol": "FUTU", "amount_cny": 520.0}],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["symbol_performance_attribution"],
                            "months": ["2026-06"],
                            "accounts": ["lx"],
                            "symbols": ["FUTU"],
                        },
                        "freshness": [{"view": "symbol_performance_attribution", "freshness": "snapshot"}],
                        "aggregation_policy": [
                            {"field": "amount_cny", "function": "sum", "policy": "allowed", "status": "ok"}
                        ],
                    },
                },
            }
        ],
    )

    result = verify_response_against_evidence(
        "按当前快照看，只覆盖 lx 账户，2026-06 的收益来源包含 FUTU；这不是全部账户结论。",
        evidence_bundle=bundle,
    )

    assert result.violations == ()
