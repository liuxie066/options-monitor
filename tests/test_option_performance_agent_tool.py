from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools import positions
from src.application.copilot import tools as copilot_tools


def _metric(
    by_currency: dict[str, float],
    *,
    cny: float | None,
    status: str = "observed",
    missing: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "by_currency": by_currency,
        "cny": cny,
        "status": status,
        "missing": list(missing or []),
        "fx_fact_ids": ["fx-private-id"],
    }


def _core_report(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": "option_period_performance.core.v1",
        "period": {
            "kind": "month",
            "requested_start_date": "2026-06-01",
            "requested_end_date": "2026-06-30",
            "status": "complete_past",
        },
        "scope": {"account": None, "broker": None, "accounts": ["lx"], "brokers": ["futu"], "symbols": []},
        "activity": {},
        "cash": {},
        "pnl": {},
        "capital": {},
        "assigned_stock": {"ending_lots": []},
        "breakdowns": {"monthly": [], "accounts": [], "symbols": []},
        "quality": {"status": "observed", "missing": [], "warnings": [], "evidence_fact_ids": []},
        "rows": list(rows or []),
        "evidence": {"schema_state": "uninitialized", "collection": {"status": "skipped_historical"}},
    }


def _patch_dependencies(monkeypatch: pytest.MonkeyPatch, *, report: dict[str, Any]) -> dict[str, Any]:
    calls: dict[str, Any] = {}

    monkeypatch.setattr(
        positions,
        "load_runtime_config",
        lambda **_kwargs: (Path("/tmp/config.us.json"), {"accounts": ["lx", "sy"], "portfolio": {}}),
    )
    monkeypatch.setattr(
        positions,
        "resolve_public_data_config_path",
        lambda _payload, _portfolio: Path("/tmp/portfolio.runtime.json"),
    )
    monkeypatch.setattr(
        positions,
        "resolve_option_positions_repo",
        lambda **_kwargs: (Path("/tmp/portfolio.runtime.json"), object()),
    )
    monkeypatch.setattr(positions, "open_performance_evidence_repository", lambda _repo: object())
    monkeypatch.setattr(positions, "repo_base", lambda: Path("/tmp"))
    monkeypatch.setattr(positions, "mask_path", lambda value: str(value))

    def _build(_repo, **kwargs):
        calls.update(kwargs)
        return report

    monkeypatch.setattr(positions, "build_option_period_performance", _build)
    return calls


def test_option_performance_report_normalizes_scope_and_caps_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "effective_at_ms": index,
            "fact_kind": "realized_gross",
            "source_event_id": f"event-{index:04d}",
            "allocation_id": None,
        }
        for index in range(1001, 0, -1)
    ]
    calls = _patch_dependencies(monkeypatch, report=_core_report(rows))

    data, warnings, _meta = positions.OPTION_PERFORMANCE_REPORT_TOOL.call(
        {
            "config_key": "us",
            "account": " LX ",
            "broker": " FUTU ",
            "period": "month",
            "month": "2026-06",
            "include_rows": True,
        }
    )

    assert warnings == []
    assert data["schema_version"] == "option_performance_report.output.v1"
    assert "assigned_stock" not in data
    assert data["assignment_lifecycle"] == {"ending_lots": []}
    assert data["scope"]["accounts"] == ["lx"]
    assert len(data["rows"]) == 1000
    assert data["rows"][0]["effective_at_ms"] == 1
    assert data["quality"]["rows_truncated"] is True
    assert data["quality"]["diagnostics"] == [
        {"code": "rows_truncated", "original_count": 1001, "returned_count": 1000}
    ]
    assert calls["account"] == "lx"
    assert calls["broker"] == "FUTU"
    assert calls["refresh_quotes"] is True
    assert calls["scope_proven"] is True


def test_option_performance_report_omitted_account_is_aggregate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_dependencies(monkeypatch, report=_core_report())

    data, _warnings, _meta = positions.OPTION_PERFORMANCE_REPORT_TOOL.call(
        {"period": "month", "month": "2026-06"}
    )

    assert calls["account"] is None
    assert calls["broker"] is None
    assert calls["scope_proven"] is True
    assert data["scope"]["accounts"] == ["lx", "sy"]
    assert "rows" not in data


def test_public_performance_presentation_is_total_first_accounted_and_identifier_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _core_report()
    report["activity"] = {
        "premium_collected_gross": _metric({"USD": 1000.0}, cny=7000.0),
    }
    report["cash"] = {
        "option_trade_cash_gross": _metric({"USD": 800.0}, cny=5600.0),
        "stock_settlement_cash_gross": _metric({"USD": -10000.0}, cny=-70000.0),
        "stock_settlement_fee_cash": _metric({"USD": -1.0}, cny=-7.0),
        "assigned_stock_sale_cash_gross": _metric({"USD": 5500.0}, cny=38500.0),
        "assigned_stock_sale_fee_cash": _metric({"USD": -2.0}, cny=-14.0),
        "total_cash_change_net": _metric({"USD": -3705.0}, cny=-25935.0),
    }
    report["pnl"] = {
        "option_realized_gross": _metric({"USD": 300.0}, cny=2100.0),
        "option_realized_net": _metric(
            {"USD": 297.0},
            cny=None,
            status="partial",
            missing=["fee:event-option-private"],
        ),
        "assigned_stock_realized_gross": _metric({"USD": 500.0}, cny=3500.0),
        "realized_gross": _metric({"USD": 800.0}, cny=5600.0),
    }
    report["breakdowns"]["accounts"] = [
        {
            "account": "sy",
            "activity": {"premium_collected_gross": _metric({"USD": 600.0}, cny=4200.0)},
            "cash": {"option_trade_cash_gross": _metric({"USD": 500.0}, cny=3500.0)},
            "pnl": {"option_realized_gross": _metric({"USD": 200.0}, cny=1400.0)},
        },
        {
            "account": "lx",
            "activity": {"premium_collected_gross": _metric({"USD": 400.0}, cny=2800.0)},
            "cash": {"option_trade_cash_gross": _metric({"USD": 300.0}, cny=2100.0)},
            "pnl": {"option_realized_gross": _metric({"USD": 100.0}, cny=700.0)},
        },
    ]
    report["quality"] = {
        "status": "partial",
        "missing": ["fee:event-option-private", "fx:USD:event-net-private"],
        "warnings": ["source_conflict:sale-private"],
        "evidence_fact_ids": ["private-evidence-id"],
    }
    _patch_dependencies(monkeypatch, report=report)

    data, _warnings, _meta = positions.OPTION_PERFORMANCE_REPORT_TOOL.call(
        {"period": "month", "month": "2026-06"}
    )

    presentation = data["presentation"]
    assert presentation["schema_version"] == "option_performance_presentation.v1"
    assert presentation["reporting_basis"]["primary"] == "gross"
    assert presentation["primary_metrics"]["option_realized_gross"]["cny"] == 2100.0
    assert presentation["primary_metrics"]["option_realized_gross"]["status"] == "observed"
    assert presentation["primary_metrics"]["option_trade_cash_gross"]["cny"] == 5600.0
    assert presentation["reporting_basis"]["net_evidence"]["status"] == "partial"
    assert [row["account"] for row in presentation["account_rows"]] == ["lx", "sy"]
    assert sum(
        row["option_realized_gross"]["cny"]
        for row in presentation["account_rows"]
    ) == presentation["primary_metrics"]["option_realized_gross"]["cny"]
    assert sum(
        row["option_trade_cash_gross"]["cny"]
        for row in presentation["account_rows"]
    ) == presentation["primary_metrics"]["option_trade_cash_gross"]["cny"]
    assert presentation["assigned_stock_impact"]["assigned_stock_realized_gross"]["cny"] == 3500.0
    assert presentation["assigned_stock_impact"]["combined_realized_gross"]["cny"] == 5600.0
    assert (
        presentation["primary_metrics"]["option_realized_gross"]["cny"]
        + presentation["assigned_stock_impact"]["assigned_stock_realized_gross"]["cny"]
        == presentation["assigned_stock_impact"]["combined_realized_gross"]["cny"]
    )
    assert presentation["definitions"]["excluded_from_option_trade_cash_gross"] == [
        "cash.stock_settlement_cash_gross",
        "cash.stock_settlement_fee_cash",
        "cash.assigned_stock_sale_cash_gross",
        "cash.assigned_stock_sale_fee_cash",
    ]
    assert presentation["limitations"] == [
        {"kind": "missing_evidence", "category": "fee", "count": 1},
        {"kind": "missing_evidence", "category": "fx", "count": 1},
        {"kind": "warning", "category": "source_conflict", "count": 1},
        {
            "kind": "metric_status",
            "metric": "option_realized_net",
            "status": "partial",
            "missing_summary": [{"category": "fee", "count": 1}],
        },
    ]
    serialized_presentation = json.dumps(presentation, ensure_ascii=False)
    assert "event-option-private" not in serialized_presentation
    assert "event-net-private" not in serialized_presentation
    assert "sale-private" not in serialized_presentation
    assert "private-evidence-id" not in serialized_presentation
    assert "fx-private-id" not in serialized_presentation
    assert data["cash"]["stock_settlement_cash_gross"]["cny"] == -70000.0
    assert data["pnl"]["option_realized_net"]["missing"] == ["fee:event-option-private"]
    observation = copilot_tools.compact_observation(
        "option_performance_report",
        {"ok": True, "data": data},
    )
    serialized_observation = json.dumps(observation, ensure_ascii=False)
    assert observation["value"]["presentation"]["primary_metrics"]["option_realized_gross"]["cny"] == 2100.0
    assert "rows" not in observation["value"]
    assert "quality" not in observation["value"]
    assert "event-option-private" not in serialized_observation
    assert "event-net-private" not in serialized_observation
    assert "sale-private" not in serialized_observation
    assert "private-evidence-id" not in serialized_observation
    assert "fx-private-id" not in serialized_observation
    assert len(serialized_observation) < 8000


def test_option_performance_output_contract_exposes_assignment_components() -> None:
    contract = positions.OPTION_PERFORMANCE_REPORT_TOOL.output_contract

    assert "pnl.option_realized_gross" in contract["fact_fields"]
    assert "pnl.option_realized_net" in contract["fact_fields"]
    assert "pnl.assigned_stock_realized_gross" in contract["fact_fields"]
    assert "pnl.assigned_stock_realized_net" in contract["fact_fields"]
    assert "cash.assigned_stock_sale_cash_gross" in contract["fact_fields"]
    assert "cash.stock_settlement_cash_gross" in contract["fact_fields"]
    assert contract["model_value_fields"] == [
        "presentation",
        "period",
        "scope",
        "evidence.schema_state",
    ]
    assert contract["model_missing_data_fields"] == ["presentation.limitations"]


def test_copilot_mtd_payload_prunes_conflicts_and_executes_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_dependencies(monkeypatch, report=_core_report())
    payload, error = copilot_tools.build_tool_payload(
        "option_performance_report",
        {
            "config_key": "us",
            "period": "mtd",
            "as_of_date": "2026-07-23",
            "month": "2026-07",
            "year": 2026,
            "start_date": "2026-07-01",
            "end_date": "2026-07-23",
        },
    )

    assert error is None
    assert payload == {
        "config_key": "us",
        "period": "mtd",
        "as_of_date": "2026-07-23",
        "include_rows": False,
        "refresh_quotes": True,
    }
    _data, warnings, _meta = positions.OPTION_PERFORMANCE_REPORT_TOOL.call(payload)

    assert warnings == []
    assert calls["period"].kind == "mtd"


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"period": "mtd", "month": "2026-06"}, "period=mtd does not accept: month"),
        ({"month": "2026-06"}, "period=mtd does not accept: month"),
        ({"period": "month"}, "month must be YYYY-MM"),
        ({"period": "year", "start_date": "2026-01-01"}, "period=year does not accept: start_date"),
        ({"period": "range", "start_date": "2026-01-01"}, "end_date is required"),
        ({"period": "month", "month": "2026-06", "unexpected": True}, "does not accept: unexpected"),
    ],
)
def test_option_performance_report_rejects_ambiguous_or_incomplete_periods(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    message: str,
) -> None:
    _patch_dependencies(monkeypatch, report=_core_report())
    with pytest.raises(AgentToolError, match=message):
        positions.OPTION_PERFORMANCE_REPORT_TOOL.call(payload)






def test_option_performance_report_does_not_prove_unconfigured_account_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_dependencies(monkeypatch, report=_core_report())

    positions.OPTION_PERFORMANCE_REPORT_TOOL.call(
        {"period": "month", "month": "2026-06", "account": "ghost"}
    )

    assert calls["account"] == "ghost"
    assert calls["scope_proven"] is False
