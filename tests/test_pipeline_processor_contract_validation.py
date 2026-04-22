from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_watchlist_pipeline_validates_processor_rows_before_aggregation() -> None:
    from scripts.pipeline_watchlist import run_watchlist_pipeline

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        return dict(item)

    def _process_symbol(*args, **kwargs):
        item = args[2]
        if str(item.get("symbol")) == "AAPL":
            # invalid row: missing strategy, should be caught and converted to failure summary rows
            return [{"symbol": "AAPL", "candidate_count": 1}]
        return [{"symbol": "MSFT", "strategy": "sell_put", "candidate_count": 0}]

    def _build_ctx(**kwargs):
        return ({}, None, None, None)

    def _noop(*args, **kwargs):
        return None

    cfg = {
        "symbols": [
            {"symbol": "AAPL", "sell_put": {"enabled": True}, "sell_call": {"enabled": True}},
            {"symbol": "MSFT", "sell_put": {"enabled": True}, "sell_call": {"enabled": True}},
        ],
        "templates": {},
        "runtime": {},
    }

    out = run_watchlist_pipeline(
        py="python",
        base=Path("."),
        cfg=cfg,
        report_dir=Path("."),
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=True,
        symbols_arg=None,
        log=lambda _: None,
        want_fn=lambda _: True,
        apply_profiles_fn=_apply_profiles,
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=_build_ctx,
        build_symbols_summary_fn=_noop,
        build_symbols_digest_fn=_noop,
    )

    # AAPL should be converted to the standard failure pair; MSFT remains valid.
    aapl_rows = [r for r in out if r.get("symbol") == "AAPL"]
    msft_rows = [r for r in out if r.get("symbol") == "MSFT"]
    assert len(aapl_rows) == 2
    assert {r.get("strategy") for r in aapl_rows} == {"sell_put", "sell_call"}
    assert len(msft_rows) == 1
    for row in out:
        assert row["schema_kind"] == "processor_output"
        assert row["schema_version"] == "3.0"


def test_watchlist_pipeline_rejects_non_list_processor_rows_contract() -> None:
    from scripts.pipeline_watchlist import run_watchlist_pipeline

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        return dict(item)

    def _process_symbol(*args, **kwargs):
        item = args[2]
        if str(item.get("symbol")) == "AAPL":
            return {"symbol": "AAPL", "strategy": "sell_put", "candidate_count": 1}
        return [{"symbol": "MSFT", "strategy": "sell_put", "candidate_count": 0}]

    def _build_ctx(**kwargs):
        return ({}, None, None, None)

    def _noop(*args, **kwargs):
        return None

    cfg = {
        "symbols": [
            {"symbol": "AAPL", "sell_put": {"enabled": True}, "sell_call": {"enabled": True}},
            {"symbol": "MSFT", "sell_put": {"enabled": True}, "sell_call": {"enabled": True}},
        ],
        "templates": {},
        "runtime": {},
    }

    out = run_watchlist_pipeline(
        py="python",
        base=Path("."),
        cfg=cfg,
        report_dir=Path("."),
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=True,
        symbols_arg=None,
        log=lambda _: None,
        want_fn=lambda _: True,
        apply_profiles_fn=_apply_profiles,
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=_build_ctx,
        build_symbols_summary_fn=_noop,
        build_symbols_digest_fn=_noop,
    )

    aapl_rows = [r for r in out if r.get("symbol") == "AAPL"]
    assert len(aapl_rows) == 2
    assert {r.get("strategy") for r in aapl_rows} == {"sell_put", "sell_call"}
    for row in out:
        assert row["schema_kind"] == "processor_output"
        assert row["schema_version"] == "3.0"
    assert all(r.get("schema_kind") == "processor_output" for r in out)
    assert all(r.get("schema_version") == "3.0" for r in out)


def main() -> None:
    test_watchlist_pipeline_validates_processor_rows_before_aggregation()
    test_watchlist_pipeline_rejects_non_list_processor_rows_contract()
    print("OK (pipeline-processor-contract)")


if __name__ == "__main__":
    main()
