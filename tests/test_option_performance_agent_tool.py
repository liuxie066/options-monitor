from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools import positions
from src.application.performance.service import OptionPerformanceReadError


def _report(*, include_rows: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {
        "period": {
            "kind": "mtd",
            "start_date": "2026-09-01",
            "as_of_date": "2026-09-02",
            "start_at_ms": 1788192000000,
            "end_exclusive_at_ms": 1788364800000,
            "statistic_days": 2,
            "reporting_timezone": "Asia/Shanghai",
            "freshness_status": "historical",
        },
        "scope": {
            "config_key": "us",
            "accounts": ["lx"],
            "brokers": ["富途"],
        },
        "option_net_cashflow": {"by_currency": {}},
        "sell_option_win_rate": {
            "winning_contracts": 0,
            "eligible_contracts": 0,
            "rate": None,
            "status": "not_applicable",
            "missing": [],
        },
        "buy_option_win_rate": {
            "winning_contracts": 0,
            "eligible_contracts": 0,
            "rate": None,
            "status": "not_applicable",
            "missing": [],
        },
        "option_return": {"by_currency": {}},
        "breakdowns": {
            "opening_years": [],
            "opening_months": [],
            "accounts": [],
            "currencies": [],
            "leg_types": [],
            "attribution_strategies": [],
            "parent_universes": [],
            "symbols": [],
        },
        "quality": {
            "status": "observed",
            "missing": [],
            "diagnostics": [],
            "ledger_input_hash": "a" * 64,
        },
    }
    if include_rows:
        value["rows"] = [{"fact_id": "fact-1", "status": "observed", "missing": []}]
    return value


def _patch_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    calls: dict[str, Any] = {}
    monkeypatch.setattr(
        positions,
        "load_runtime_config",
        lambda **_kwargs: (
            Path("/tmp/config.us.json"),
            {"accounts": ["lx", "sy"], "portfolio": {}},
        ),
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
    monkeypatch.setattr(positions, "repo_base", lambda: Path("/tmp"))
    monkeypatch.setattr(positions, "mask_path", lambda value: str(value))

    def _build(_repo, **kwargs):
        calls.update(kwargs)
        return report if report is not None else _report(include_rows=kwargs["include_rows"])

    monkeypatch.setattr(positions, "build_option_period_performance", _build)
    return calls


def _contains_not_observed(value: object) -> bool:
    if isinstance(value, dict):
        return any(_contains_not_observed(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_not_observed(item) for item in value)
    return value == "not_observed"


def test_option_performance_report_is_the_canonical_payload_without_legacy_presentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_dependencies(monkeypatch)

    data, warnings, meta = positions.OPTION_PERFORMANCE_REPORT_TOOL.call(
        {
            "config_key": "us",
            "account": " LX ",
            "broker": " FUTU ",
            "period": "mtd",
            "as_of_date": "2026-09-02",
            "include_rows": True,
        }
    )

    assert warnings == []
    assert set(data) == {
        "period",
        "scope",
        "option_net_cashflow",
        "sell_option_win_rate",
        "buy_option_win_rate",
        "option_return",
        "breakdowns",
        "quality",
        "rows",
    }
    assert calls["account"] == "lx"
    assert calls["broker"] == "富途"
    assert calls["configured_accounts"] == ["lx", "sy"]
    assert calls["config_key"] == "us"
    assert calls["include_rows"] is True
    assert meta["freshness_status"] == "historical"
    assert not _contains_not_observed(data)


def test_option_performance_report_omitted_scope_is_configured_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_dependencies(monkeypatch)

    data, _warnings, _meta = positions.OPTION_PERFORMANCE_REPORT_TOOL.call(
        {"period": "ytd", "as_of_date": "2026-09-02"}
    )

    assert calls["account"] is None
    assert calls["broker"] is None
    assert calls["configured_accounts"] == ["lx", "sy"]
    assert calls["period"].kind == "ytd"
    assert "rows" not in data


@pytest.mark.parametrize(
    "payload",
    [
        {"period": "month"},
        {"period": "mtd", "month": "2026-09"},
        {"period": "ytd", "year": 2026},
        {"period": "mtd", "start_date": "2026-09-01"},
        {"period": "mtd", "end_date": "2026-09-02"},
        {"period": "mtd", "refresh_quotes": False},
    ],
)
def test_option_performance_report_rejects_removed_inputs(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    _patch_dependencies(monkeypatch)

    with pytest.raises(AgentToolError) as caught:
        positions.OPTION_PERFORMANCE_REPORT_TOOL.call(payload)

    assert caught.value.code == "INPUT_ERROR"


def test_option_performance_report_translates_only_stable_read_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dependencies(monkeypatch)

    def _raise(_repo, **_kwargs):
        raise OptionPerformanceReadError(
            "scope_unproven",
            "ledger_control_graph_invalid",
            "scope_unproven",
        )

    monkeypatch.setattr(positions, "build_option_period_performance", _raise)
    with pytest.raises(AgentToolError) as caught:
        positions.OPTION_PERFORMANCE_REPORT_TOOL.call({"period": "mtd"})

    assert caught.value.code == "READ_ERROR"
    assert caught.value.details == {
        "reason_codes": ["ledger_control_graph_invalid", "scope_unproven"]
    }
    assert "scope_unproven" not in caught.value.message


@pytest.mark.parametrize(
    "dependency",
    [
        "load_runtime_config",
        "resolve_public_data_config_path",
        "resolve_option_positions_repo",
        "repo_base",
    ],
)
def test_option_performance_report_classifies_dependency_resolution_failure(
    monkeypatch: pytest.MonkeyPatch,
    dependency: str,
) -> None:
    _patch_dependencies(monkeypatch)
    monkeypatch.setattr(
        positions,
        dependency,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private path")),
    )

    with pytest.raises(AgentToolError) as caught:
        positions.OPTION_PERFORMANCE_REPORT_TOOL.call({"period": "mtd"})

    assert caught.value.code == "READ_ERROR"
    assert caught.value.details == {"reason_codes": ["ledger_read_failed"]}
    assert "private path" not in caught.value.message


def test_option_performance_output_contract_has_only_the_new_business_fields() -> None:
    contract = positions.OPTION_PERFORMANCE_REPORT_TOOL.output_contract

    assert contract["evidence_type"] == "aggregate"
    assert "schema_version" not in contract
    assert "option_net_cashflow" in contract["fact_fields"]
    assert "sell_option_win_rate" in contract["fact_fields"]
    assert "buy_option_win_rate" in contract["fact_fields"]
    assert "option_return" in contract["fact_fields"]
    serialized = str(contract)
    for removed in ("pnl.", "activity.", "assignment_lifecycle", "presentation", "refresh_quotes"):
        assert removed not in serialized


def test_option_performance_tool_schema_exposes_only_the_frozen_inputs() -> None:
    tool = positions.OPTION_PERFORMANCE_REPORT_TOOL

    assert set(tool.input_schema) == {
        "config_key",
        "config_path",
        "data_config",
        "account",
        "broker",
        "period",
        "as_of_date",
        "include_rows",
    }
    assert tool.input_schema["period"]["enum"] == ["mtd", "ytd"]
    assert set(tool.copilot_input_fields) == {
        "config_key",
        "account",
        "broker",
        "period",
        "as_of_date",
        "include_rows",
    }
