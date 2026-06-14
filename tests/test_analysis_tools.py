from __future__ import annotations

from pathlib import Path

import pytest

import src.application.agent_tools.analysis as analysis_module
from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.analysis import (
    ANALYSIS_CATALOG_TOOL,
    ANALYSIS_QUERY_TOOL,
    _account_monthly_income_component_rows,
    _assigned_stock_position_pnl_rows,
    _assigned_stock_sale_event_rows,
    _expiration_risk_bucket_rows,
    _execute_select,
    _open_option_exposure_rows,
    _query_explain_and_evidence,
    _strategy_config_by_symbol_account_rows,
    _symbol_income_attribution_rows,
)
from src.application.assistant.answer_verifier import verify_response_against_evidence
from src.application.assistant.evidence import build_evidence_bundle


class _AnalysisQueryContext:
    def __init__(self, base: Path | None = None):
        self.base = base or Path(".")

    def __getattr__(self, _name):
        def _stub(*_args, **_kwargs):
            return None

        return _stub

    def repo_base(self):
        return self.base

    def mask_path(self, value):
        return f".../{Path(value).name}" if value else None

    def load_runtime_config(self, **_kwargs):
        return Path("config.us.json"), {}

    def list_symbol_rows(self, *_args, **_kwargs):
        return []


class _CatalogContext:
    def load_runtime_config(self, **_kwargs):
        return Path("config.us.json"), {}

    def mask_path(self, value):
        return f".../{Path(value).name}" if value else None


def test_analysis_query_rejects_write_sql_before_context_access() -> None:
    with pytest.raises(AgentToolError) as exc:
        ANALYSIS_QUERY_TOOL.call(object(), {"sql": "delete from monthly_income_return_summary"})

    assert exc.value.code == "PERMISSION_DENIED"


def test_analysis_catalog_rejects_non_string_view_filter_before_context_access() -> None:
    with pytest.raises(AgentToolError) as exc:
        ANALYSIS_CATALOG_TOOL.call(object(), {"views": {"monthly_income_return_summary": True}})

    assert exc.value.code == "INPUT_ERROR"


def test_analysis_catalog_exposes_semantic_metadata_for_account_performance() -> None:
    data, warnings, meta = ANALYSIS_CATALOG_TOOL.call(_CatalogContext(), {"views": "account_monthly_performance"})

    assert warnings == []
    assert meta == {"config_path": ".../config.us.json"}
    assert data["schema_version"] == "analysis.catalog.v2"

    view = data["views"]["account_monthly_performance"]
    assert view["row_grain"] == "month + account"
    assert view["alias_of"] == "monthly_income_return_summary"
    assert view["safe_join_keys"] == ("month", "account")
    assert "net_income_cny" in view["fields"]

    net_income = view["field_semantics"]["net_income_cny"]
    assert net_income["type"] == "money"
    assert net_income["currency"] == "CNY"
    assert net_income["aggregation"] == "sum"

    net_return = view["field_semantics"]["net_return_rate"]
    assert net_return["type"] == "rate"
    assert net_return["aggregation"] == "weighted_recompute"
    assert "avg" in net_return["do_not"]

    assert data["field_types"]["account_monthly_performance"]["net_return_rate"] == "rate"
    assert data["aggregation_policies"]["account_monthly_performance"]["net_return_rate"] == "weighted_recompute"
    assert data["join_policies"]["account_monthly_performance"]["safe_join_keys"] == ["month", "account"]


def test_analysis_catalog_exposes_p0_semantic_views() -> None:
    data, _warnings, _meta = ANALYSIS_CATALOG_TOOL.call(
        _CatalogContext(),
        {"views": ["account_monthly_income_components", "assigned_stock_position_pnl", "assigned_stock_sale_events"]},
    )

    assert data["views"]["account_monthly_income_components"]["row_grain"] == "month + account + component"
    assert data["views"]["assigned_stock_position_pnl"]["row_grain"] == "account + symbol + stock_lot_id"
    assert data["views"]["assigned_stock_sale_events"]["row_grain"] == "account + symbol + stock_lot_id + sale event"
    assert data["views"]["assigned_stock_position_pnl"]["alias_of"] == "assigned_stock_lifecycle"
    assert data["views"]["assigned_stock_sale_events"]["alias_of"] == "assigned_stock_sales"


def test_analysis_catalog_exposes_p1_semantic_views() -> None:
    data, _warnings, _meta = ANALYSIS_CATALOG_TOOL.call(
        _CatalogContext(),
        {"views": ["open_option_exposure", "expiration_risk_buckets", "symbol_income_attribution", "strategy_config_by_symbol_account"]},
    )

    assert data["views"]["open_option_exposure"]["row_grain"] == "account + symbol + option_type + side + strike + expiration"
    assert data["views"]["expiration_risk_buckets"]["row_grain"] == "account + expiration_bucket + currency"
    assert data["views"]["symbol_income_attribution"]["row_grain"] == "month + account + symbol + component + currency"
    assert data["views"]["strategy_config_by_symbol_account"]["row_grain"] == "symbol + account + strategy_family"


def test_analysis_catalog_exposes_p2_semantic_views() -> None:
    data, _warnings, _meta = ANALYSIS_CATALOG_TOOL.call(
        _CatalogContext(),
        {"views": ["candidate_filter_diagnostics", "close_advice_snapshot", "runtime_tick_status", "quote_freshness"]},
    )

    assert data["views"]["candidate_filter_diagnostics"]["row_grain"] == "run_id + account + symbol + option_type + rule"
    assert data["views"]["close_advice_snapshot"]["row_grain"] == "account + position_id + advice_run_id"
    assert data["views"]["runtime_tick_status"]["row_grain"] == "market + account + latest_run"
    assert data["views"]["quote_freshness"]["row_grain"] == "symbol + market + source"


def test_analysis_query_authorizer_rejects_non_whitelisted_tables() -> None:
    with pytest.raises(AgentToolError) as exc:
        _execute_select(
            "select name from sqlite_master",
            {"monthly_income_return_summary": [{"month": "2026-05", "account": "lx"}]},
            limit=10,
        )

    assert exc.value.code == "INPUT_ERROR"
    assert "prohibited" in exc.value.message


def test_analysis_query_authorizer_rejects_non_whitelisted_functions() -> None:
    with pytest.raises(AgentToolError) as exc:
        _execute_select(
            "select load_extension('x') as loaded from monthly_income_return_summary",
            {"monthly_income_return_summary": [{"month": "2026-05", "account": "lx"}]},
            limit=10,
        )

    assert exc.value.code == "INPUT_ERROR"
    assert "load_extension" in exc.value.message


def test_analysis_query_account_monthly_performance_alias_executes() -> None:
    rows, columns, views_used = _execute_select(
        "select month, account, net_income_cny from account_monthly_performance order by account",
        {
            "account_monthly_performance": [
                {"month": "2026-05", "account": "lx", "net_income_cny": 35842.0},
                {"month": "2026-05", "account": "sy", "net_income_cny": 23973.0},
            ]
        },
        limit=10,
    )

    assert columns == ["month", "account", "net_income_cny"]
    assert rows == [
        {"month": "2026-05", "account": "lx", "net_income_cny": 35842.0},
        {"month": "2026-05", "account": "sy", "net_income_cny": 23973.0},
    ]
    assert views_used == ["account_monthly_performance"]


def test_analysis_query_materializes_only_referenced_monthly_views(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_monthly_tool(*_args, **_kwargs):
        calls.append("monthly")
        return {"return_summary": [{"month": "2026-05", "account": "lx", "net_income_cny": 1.0}]}, [], {}

    def fake_positions_tool(*_args, **_kwargs):
        calls.append("positions")
        return {"rows": []}, [], {}

    monkeypatch.setattr(analysis_module, "monthly_income_report_tool", fake_monthly_tool)
    monkeypatch.setattr(analysis_module, "option_positions_read_tool", fake_positions_tool)

    data, warnings, meta = ANALYSIS_QUERY_TOOL.call(
        _AnalysisQueryContext(),
        {"sql": "select month, account, net_income_cny from account_monthly_performance", "limit": 10},
    )

    assert warnings == []
    assert calls == ["monthly"]
    assert data["rows"] == [{"month": "2026-05", "account": "lx", "net_income_cny": 1.0}]
    assert meta["requested_views"] == ["account_monthly_performance"]
    assert meta["materialized_views"] == ["account_monthly_performance"]


def test_analysis_query_materializes_only_referenced_position_views(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_monthly_tool(*_args, **_kwargs):
        calls.append("monthly")
        return {"return_summary": []}, [], {}

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

    monkeypatch.setattr(analysis_module, "monthly_income_report_tool", fake_monthly_tool)
    monkeypatch.setattr(analysis_module, "option_positions_read_tool", fake_positions_tool)

    data, warnings, meta = ANALYSIS_QUERY_TOOL.call(
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
        return {}, [], {}

    def fake_positions_tool(*_args, **_kwargs):
        calls.append("positions")
        return {}, [], {}

    monkeypatch.setattr(analysis_module, "monthly_income_report_tool", fake_monthly_tool)
    monkeypatch.setattr(analysis_module, "option_positions_read_tool", fake_positions_tool)

    data, warnings, meta = ANALYSIS_QUERY_TOOL.call(_AnalysisQueryContext(), {"sql": "select 1 as ok"})

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

    data, warnings, meta = ANALYSIS_QUERY_TOOL.call(
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


def test_analysis_query_close_advice_snapshot_missing_artifact_returns_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_close_advice_tool(*_args, **_kwargs):
        raise AgentToolError(code="DEPENDENCY_MISSING", message="没有找到最近的平仓建议报告。")

    monkeypatch.setattr(analysis_module, "_call_close_advice_read_tool", fake_close_advice_tool)

    data, warnings, meta = ANALYSIS_QUERY_TOOL.call(
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

    data, warnings, meta = ANALYSIS_QUERY_TOOL.call(
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


def test_analysis_query_quote_freshness_derives_from_assigned_stock_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_monthly_tool(*_args, **_kwargs):
        return {
            "assignment_lifecycle_rows": [
                {
                    "account": "sy",
                    "symbol": "0700.HK",
                    "quote_source": "opend_realtime",
                    "quote_status": "fresh",
                    "spot": 463.6,
                    "spot_time": "2026-06-14T10:00:00+08:00",
                }
            ]
        }, [], {}

    monkeypatch.setattr(analysis_module, "monthly_income_report_tool", fake_monthly_tool)

    data, warnings, meta = ANALYSIS_QUERY_TOOL.call(
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


def test_account_monthly_income_components_split_included_and_excluded_amounts() -> None:
    rows = _account_monthly_income_component_rows(
        [
            {
                "month": "2026-05",
                "account": "lx",
                "net_income_cny": 1500.0,
                "premium_income_cny": 1200.0,
                "premium_income_by_ccy": {"USD": 166.67},
                "realized_pnl_cny": 250.0,
                "realized_pnl_by_ccy": {"USD": 34.72},
            }
        ],
        [
            {
                "month": "2026-05",
                "account": "lx",
                "currency": "USD",
                "assignment_stock_net_cashflow_gross": -10000.0,
                "assignment_stock_net_cashflow_gross_cny": -72000.0,
            }
        ],
    )

    assert [(row["component"], row["amount_cny"], row["included_in_net_income"]) for row in rows] == [
        ("premium_income", 1200.0, True),
        ("realized_pnl", 250.0, True),
        ("other_net_income", 50.0, True),
        ("excluded_assignment_stock_principal", -72000.0, False),
    ]
    assert rows[-1]["amount_by_ccy"] == {"USD": -10000.0}


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


def test_symbol_income_attribution_groups_detail_rows_by_symbol_component() -> None:
    rows = _symbol_income_attribution_rows(
        cashflow_rows=[
            {"month": "2026-05", "account": "lx", "symbol": "FUTU", "currency": "USD", "net_cashflow_gross": 100.0}
        ],
        realized_rows=[
            {"month": "2026-05", "account": "lx", "symbol": "FUTU", "currency": "USD", "realized_gross": -20.0}
        ],
        premium_rows=[
            {"month": "2026-05", "account": "lx", "symbol": "FUTU", "currency": "USD", "premium_received_gross": 120.0},
            {"month": "2026-05", "account": "lx", "symbol": "FUTU", "currency": "USD", "premium_received_gross": 30.0},
        ],
    )

    assert [(row["component"], row["amount_gross"]) for row in rows] == [
        ("net_cashflow", 100.0),
        ("premium_income", 150.0),
        ("realized_pnl", -20.0),
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
            "select month, account, net_cashflow from account_monthly_performance",
            {
                "account_monthly_performance": [
                    {"month": "2026-05", "account": "lx", "net_income_cny": 35842.0},
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
    assert exc.value.details["referenced_views"] == ["account_monthly_performance"]
    assert "net_income_cny" in exc.value.details["suggestions"]
    assert "net_return_rate" in exc.value.details["suggestions"]


def test_analysis_query_executes_read_only_aggregates() -> None:
    rows, columns, views_used = _execute_select(
        (
            "select month, "
            "sum(case when account = 'lx' then net_income_cny else 0 end) as lx_income_cny, "
            "sum(case when account = 'sy' then net_income_cny else 0 end) as sy_income_cny, "
            "sum(case when account = 'lx' then net_income_cny else 0 end) - "
            "sum(case when account = 'sy' then net_income_cny else 0 end) as income_diff_cny "
            "from monthly_income_return_summary group by month"
        ),
        {
            "monthly_income_return_summary": [
                {"month": "2026-05", "account": "lx", "net_income_cny": 35842.0},
                {"month": "2026-05", "account": "sy", "net_income_cny": 23973.0},
            ]
        },
        limit=10,
    )

    assert columns == ["month", "lx_income_cny", "sy_income_cny", "income_diff_cny"]
    assert rows == [
        {
            "month": "2026-05",
            "lx_income_cny": 35842.0,
            "sy_income_cny": 23973.0,
            "income_diff_cny": 11869.0,
        }
    ]
    assert views_used == ["monthly_income_return_summary"]


def test_analysis_query_explain_warns_on_invalid_rate_aggregation() -> None:
    query_explain, warnings, evidence = _query_explain_and_evidence(
        sql="select month, avg(net_return_rate) as avg_rate from account_monthly_performance group by month",
        rows=[{"month": "2026-05", "avg_rate": 0.12}],
        columns=["month", "avg_rate"],
        views_used=["account_monthly_performance"],
    )

    assert query_explain["views_used"] == ["account_monthly_performance"]
    assert query_explain["grain"] == ["month"]
    assert query_explain["coverage"]["months"] == ["2026-05"]
    assert query_explain["aggregations"][0]["field"] == "net_return_rate"
    assert query_explain["aggregations"][0]["policy"] == "invalid_rate_aggregation"
    assert warnings == [query_explain["aggregations"][0]["warning"]]
    assert evidence["aggregation_policy"][0]["status"] == "warning"


def test_analysis_query_explain_marks_safe_money_sum() -> None:
    query_explain, warnings, evidence = _query_explain_and_evidence(
        sql="select month, sum(net_income_cny) as total_income from account_monthly_performance group by month",
        rows=[{"month": "2026-05", "total_income": 59815.0}],
        columns=["month", "total_income"],
        views_used=["account_monthly_performance"],
    )

    assert warnings == []
    assert query_explain["aggregations"][0]["field"] == "net_income_cny"
    assert query_explain["aggregations"][0]["policy"] == "allowed"
    assert evidence["coverage"]["views"] == ["account_monthly_performance"]


def test_analysis_query_cells_become_answer_guard_evidence() -> None:
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
                    "schema_version": "analysis_query.output.v1",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month", "rows[].account"],
                },
                "data": {
                    "rows": [
                        {
                            "month": "2026-05",
                            "account": "lx",
                            "symbol": "FUTU",
                            "net_income_cny": 35842.0,
                            "income_diff_cny": 11869.0,
                        }
                    ],
                    "row_count": 1,
                },
            }
        ],
    )

    payload = bundle.public_payload()
    assert any(item["path"] == "rows[].income_diff_cny" and item["currency"] == "CNY" for item in payload["facts"])

    result = verify_response_against_evidence(
        "2026-05 lx 更高，净现金流 CNY 35,842，差额 CNY 11,869。HK 市场只是说明文字。",
        evidence_bundle=bundle,
    )

    assert result.violations == ()


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
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month", "rows[].account"],
                },
                "data": {
                    "rows": [
                        {
                            "month": "2026-05",
                            "lx_income_cny": 35842.41,
                            "sy_income_cny": 21453.29,
                        }
                    ],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["account_monthly_performance"],
                            "months": ["2026-05"],
                            "accounts": ["lx", "sy"],
                            "symbols": [],
                        },
                        "freshness": [{"view": "account_monthly_performance", "freshness": "snapshot"}],
                        "aggregation_policy": [
                            {"field": "net_income_cny", "function": "sum", "policy": "allowed", "status": "ok"}
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
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month", "rows[].account"],
                },
                "data": {
                    "rows": [
                        {
                            "month": "2026-06",
                            "account": "lx",
                            "net_income_cny": 9000.0,
                            "cash_secured_cny": 300000.0,
                        }
                    ],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["account_monthly_performance"],
                            "months": ["2026-06"],
                            "accounts": ["lx"],
                            "symbols": [],
                        },
                        "freshness": [{"view": "account_monthly_performance", "freshness": "snapshot"}],
                        "aggregation_policy": [
                            {"field": "net_income_cny", "function": "sum", "policy": "allowed", "status": "ok"}
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
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month"],
                },
                "data": {
                    "rows": [
                        {
                            "month": "2026-06",
                            "lx_income_cny": 100.0,
                            "sy_income_cny": 40.0,
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
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
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
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
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
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
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
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
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
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month", "rows[].account"],
                },
                "data": {
                    "rows": [{"month": "2026-05", "account": "lx", "net_income_cny": 35842.0}],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["account_monthly_performance"],
                            "months": ["2026-05"],
                            "accounts": ["lx", "sy"],
                            "symbols": ["FUTU"],
                        },
                        "freshness": [{"view": "account_monthly_performance", "freshness": "snapshot"}],
                        "aggregation_policy": [
                            {"field": "net_income_cny", "function": "sum", "policy": "allowed", "status": "ok"}
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
    assert analysis_evidence["coverage"]["views"] == ["account_monthly_performance"]
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
                        "select avg(net_return_rate) as avg_rate "
                        "from account_monthly_performance where account = 'lx'"
                    )
                },
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month", "rows[].account"],
                },
                "data": {
                    "rows": [{"month": "2026-06", "account": "lx", "avg_rate": 0.0123}],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["account_monthly_performance"],
                            "months": ["2026-06"],
                            "accounts": ["lx"],
                            "symbols": [],
                        },
                        "freshness": [
                            {"view": "quote_freshness", "symbol": "FUTU", "freshness": "missing"}
                        ],
                        "aggregation_policy": [
                            {
                                "field": "net_return_rate",
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
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
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
                "payload": {"sql": "select * from symbol_income_attribution where account = 'lx'"},
                "ok": True,
                "error": None,
                "output_contract": {
                    "schema_version": "analysis_query.output.v2",
                    "canonical_renderer": "analysis_result",
                    "source_label": "OM read-only analysis workspace",
                    "guard_profile": "analysis_result",
                    "primary_rows": "rows",
                    "row_count_field": "row_count",
                    "fact_fields": ["rows[].month", "rows[].account"],
                },
                "data": {
                    "rows": [{"month": "2026-06", "account": "lx", "symbol": "FUTU", "amount_cny": 520.0}],
                    "row_count": 1,
                    "evidence": {
                        "coverage": {
                            "views": ["symbol_income_attribution"],
                            "months": ["2026-06"],
                            "accounts": ["lx"],
                            "symbols": ["FUTU"],
                        },
                        "freshness": [{"view": "symbol_income_attribution", "freshness": "snapshot"}],
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
