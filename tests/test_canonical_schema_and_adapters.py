from __future__ import annotations

import sys
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from om.domain import normalize_processor_row, normalize_source_snapshot

_ADAPTERS_PATH = BASE / "om" / "services" / "source_adapters.py"
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


def test_three_source_adapters_produce_unified_dto() -> None:
    opend = adapt_opend_tool_payload(
        {
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


def main() -> None:
    test_normalize_processor_row_requires_symbol_and_strategy()
    test_source_snapshot_validates_and_normalizes()
    test_three_source_adapters_produce_unified_dto()
    print("OK (canonical-schema-adapters)")


if __name__ == "__main__":
    main()
