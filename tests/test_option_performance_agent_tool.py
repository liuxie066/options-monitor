from __future__ import annotations

from pathlib import Path
from typing import Any
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools import positions


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


def test_option_performance_report_omitted_account_is_aggregate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_dependencies(monkeypatch, report=_core_report())

    data, _warnings, _meta = positions.OPTION_PERFORMANCE_REPORT_TOOL.call(
        {"period": "month", "month": "2026-06"}
    )

    assert calls["account"] is None
    assert calls["broker"] is None
    assert data["scope"]["accounts"] == ["lx", "sy"]
    assert "rows" not in data


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"period": "mtd", "month": "2026-06"}, "period=mtd does not accept: month"),
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


def test_monthly_income_report_is_deprecated_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _core_report()
    report["breakdowns"]["monthly"] = [
        {
            "month": "2026-06",
            "activity": {"premium_collected_gross": {"by_currency": {"USD": 200.0}, "cny": None}},
            "cash": {"total_cash_change_net": {"by_currency": {"USD": 150.0}, "cny": None}},
            "pnl": {"realized_gross": {"by_currency": {"USD": 150.0}, "cny": None}},
        }
    ]
    _patch_dependencies(monkeypatch, report=report)

    data, warnings, _meta = positions.MONTHLY_INCOME_REPORT_TOOL.call(
        {"month": "2026-06", "account": "lx"}
    )

    assert data["deprecation"]["replacement"] == "option_performance_report"
    assert data["return_summary"][0]["realized_pnl_by_ccy"] == {"USD": 150.0}
    assert data["return_summary"][0]["realized_return_rate"] is None
    assert any("DEPRECATED" in warning for warning in warnings)


def test_monthly_income_report_maps_legacy_as_of_to_historical_mtd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_dependencies(monkeypatch, report=_core_report())
    as_of_ms = int(
        datetime(2026, 4, 30, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
    )

    _data, warnings, _meta = positions.MONTHLY_INCOME_REPORT_TOOL.call(
        {"account": "lx", "as_of_ms": as_of_ms, "refresh_quotes": True}
    )

    assert calls["period"].kind == "mtd"
    assert calls["period"].requested_end_date == "2026-04-30"
    assert calls["refresh_quotes"] is False
    assert any("mapped to an MTD as_of_date" in warning for warning in warnings)
