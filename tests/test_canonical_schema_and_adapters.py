from __future__ import annotations

import sys
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from domain.domain.canonical_schema import (
    normalize_processor_row,
    normalize_processor_rows,
    normalize_source_snapshot,
)

_ADAPTERS_PATH = BASE / "domain" / "services" / "source_adapters.py"
_ADAPTERS_SPEC = importlib.util.spec_from_file_location("om_source_adapters", _ADAPTERS_PATH)
assert _ADAPTERS_SPEC and _ADAPTERS_SPEC.loader
_ADAPTERS_MOD = importlib.util.module_from_spec(_ADAPTERS_SPEC)
_ADAPTERS_SPEC.loader.exec_module(_ADAPTERS_MOD)

adapt_holdings_context = _ADAPTERS_MOD.adapt_holdings_context
adapt_opend_tool_payload = _ADAPTERS_MOD.adapt_opend_tool_payload
adapt_option_positions_context = _ADAPTERS_MOD.adapt_option_positions_context


def test_normalize_processor_row_requires_symbol_and_strategy() -> None:
    out = normalize_processor_row({"symbol": "aapl", "strategy": "sell_put", "candidate_count": "2"})
    assert out["schema_kind"] == "processor_output"
    assert out["schema_version"] == "3.0"
    assert out["symbol"] == "AAPL"
    assert out["candidate_count"] == 2


def test_normalize_processor_row_keeps_summary_fields_with_defaults() -> None:
    out = normalize_processor_row(
        {
            "symbol": "aapl",
            "strategy": "sell_put",
            "top_contract": "2026-05-15 180P",
            "annualized_return": 0.12,
            "net_income": 123.45,
            "strike": 180.0,
            "dte": 32,
            "risk_label": "中性",
        }
    )
    assert out["top_contract"] == "2026-05-15 180P"
    assert out["annualized_return"] == 0.12
    assert out["net_income"] == 123.45
    assert out["strike"] == 180.0
    assert out["dte"] == 32
    assert out["risk_label"] == "中性"
    assert out["delta"] is None
    assert out["iv"] is None
    assert out["cash_required_cny"] is None

    out2 = normalize_processor_row({"symbol": "msft", "strategy": "sell_call"})
    assert out2["top_contract"] == ""
    assert out2["annualized_return"] is None
    assert out2["net_income"] is None
    assert out2["strike"] is None
    assert out2["dte"] is None
    assert out2["risk_label"] == ""
    assert out2["delta"] is None
    assert out2["iv"] is None
    assert out2["cash_required_cny"] is None


def test_normalize_processor_row_preserves_put_alert_fields() -> None:
    out = normalize_processor_row(
        {
            "symbol": "nvda",
            "strategy": "sell_put",
            "candidate_count": 1,
            "delta": -0.23,
            "iv": 0.41,
            "cash_required_cny": 110720.0,
            "cash_required_usd": 15200.0,
            "cash_free_cny": 200000.0,
            "cash_free_total_cny": 198000.0,
            "cash_free_usd": 25000.0,
            "cash_free_usd_est": 24800.0,
            "cash_available_cny": 260000.0,
            "cash_available_total_cny": 258000.0,
            "cash_available_usd": 32000.0,
            "cash_available_usd_est": 31800.0,
            "mid": 5.72,
            "bid": 5.58,
            "ask": 5.86,
            "option_ccy": "HKD",
        }
    )
    assert out["delta"] == -0.23
    assert out["iv"] == 0.41
    assert out["cash_required_cny"] == 110720.0
    assert out["cash_required_usd"] == 15200.0
    assert out["cash_free_cny"] == 200000.0
    assert out["cash_free_total_cny"] == 198000.0
    assert out["cash_free_usd"] == 25000.0
    assert out["cash_free_usd_est"] == 24800.0
    assert out["cash_available_cny"] == 260000.0
    assert out["cash_available_total_cny"] == 258000.0
    assert out["cash_available_usd"] == 32000.0
    assert out["cash_available_usd_est"] == 31800.0
    assert out["mid"] == 5.72
    assert out["bid"] == 5.58
    assert out["ask"] == 5.86
    assert out["option_ccy"] == "HKD"


def test_source_snapshot_validates_and_normalizes() -> None:
    out = normalize_source_snapshot(
        source_name="holdings",
        status="ok",
        payload={"stocks_count": 2},
    )
    assert out["schema_kind"] == "source_snapshot"
    assert out["schema_version"] == "3.0"
    assert out["source_name"] == "holdings"
    assert out["status"] == "ok"
    assert out["fallback_used"] is False


def test_normalize_processor_rows_requires_list_contract() -> None:
    try:
        normalize_processor_rows({"symbol": "AAPL", "strategy": "sell_put"})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_three_source_adapters_produce_unified_dto() -> None:
    opend = adapt_opend_tool_payload(
        {
            "schema_kind": "tool_execution",
            "schema_version": "1.0",
            "symbol": "AAPL",
            "tool_name": "required_data_prefetch",
            "status": "fetched",
            "ok": True,
            "source": "opend",
            "idempotency_key": "k1",
            "returncode": 0,
        }
    )
    holdings = adapt_holdings_context(
        {
            "as_of_utc": "2026-04-12T00:00:00+00:00",
            "filters": {"market": "富途"},
            "stocks_by_symbol": {"AAPL": {"shares": 100}},
            "cash_by_currency": {"USD": 10.0},
            "raw_selected_count": 1,
        }
    )
    option_positions = adapt_option_positions_context(
        {
            "as_of_utc": "2026-04-12T00:00:00+00:00",
            "filters": {"broker": "富途"},
            "locked_shares_by_symbol": {"AAPL": 100},
            "cash_secured_by_symbol_by_ccy": {"AAPL": {"USD": 1000.0}},
            "raw_selected_count": 1,
        }
    )

    for dto, source in (
        (opend, "opend"),
        (holdings, "holdings"),
        (option_positions, "option_positions"),
    ):
        assert dto["schema_kind"] == "source_snapshot"
        assert dto["schema_version"] == "3.0"
        assert dto["source_name"] == source
        assert isinstance(dto["payload"], dict)


def test_opend_adapter_marks_fallback_and_unified_error_code() -> None:
    timeout_case = adapt_opend_tool_payload(
        {
            "schema_kind": "tool_execution",
            "schema_version": "1.0",
            "symbol": "AAPL",
            "tool_name": "required_data_prefetch",
            "status": "error",
            "ok": False,
            "source": "opend",
            "error_code": "OPEND_API_ERROR",
            "message": "request timed out",
        }
    )
    assert timeout_case["status"] == "error"
    assert timeout_case["fallback_used"] is False
    assert timeout_case["error_code"] == "ERR_TIMEOUT"
    assert timeout_case["error_category"] == "timeout"

    fallback_ok = adapt_opend_tool_payload(
        {
            "schema_kind": "tool_execution",
            "schema_version": "1.0",
            "symbol": "AAPL",
            "tool_name": "required_data_prefetch",
            "status": "fetched",
            "ok": True,
            "source": "yahoo",
            "returncode": 0,
        }
    )
    assert fallback_ok["status"] == "fallback"
    assert fallback_ok["fallback_used"] is True
    assert fallback_ok["error_code"] is None


def test_opend_adapter_rejects_non_tool_execution_schema() -> None:
    try:
        adapt_opend_tool_payload(
            {
                "schema_kind": "bad_kind",
                "schema_version": "1.0",
                "symbol": "AAPL",
                "tool_name": "required_data_prefetch",
                "status": "fetched",
                "ok": True,
                "source": "opend",
            }
        )
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "schema_kind must be tool_execution" in str(e)


def main() -> None:
    test_normalize_processor_row_requires_symbol_and_strategy()
    test_normalize_processor_row_keeps_summary_fields_with_defaults()
    test_normalize_processor_row_preserves_put_alert_fields()
    test_source_snapshot_validates_and_normalizes()
    test_normalize_processor_rows_requires_list_contract()
    test_three_source_adapters_produce_unified_dto()
    test_opend_adapter_marks_fallback_and_unified_error_code()
    test_opend_adapter_rejects_non_tool_execution_schema()
    print("OK (canonical-schema-adapters)")


if __name__ == "__main__":
    main()
