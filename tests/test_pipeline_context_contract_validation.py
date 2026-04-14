from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_load_portfolio_context_rejects_invalid_cached_contract() -> None:
    import scripts.pipeline_context as pc

    old_is_fresh = pc.is_fresh
    old_load_cached_json = pc.load_cached_json
    try:
        pc.is_fresh = lambda *_a, **_k: True  # type: ignore[assignment]
        pc.load_cached_json = lambda *_a, **_k: {  # type: ignore[assignment]
            "as_of_utc": "2026-04-12T00:00:00+00:00",
            "stocks_by_symbol": [],
            "cash_by_currency": {},
        }
        logs: list[str] = []
        out = pc.load_portfolio_context(
            py="python",
            base=Path("."),
            pm_config="x.json",
            market="富途",
            account=None,
            ttl_sec=3600,
            timeout_sec=1,
            is_scheduled=True,
            state_dir=Path("."),
            shared_state_dir=Path("."),
            log=logs.append,
        )
        assert out is None
        assert any("portfolio context not available" in x for x in logs)
    finally:
        pc.is_fresh = old_is_fresh  # type: ignore[assignment]
        pc.load_cached_json = old_load_cached_json  # type: ignore[assignment]


def test_load_option_positions_context_rejects_invalid_cached_contract() -> None:
    import scripts.pipeline_context as pc

    old_is_fresh = pc.is_fresh
    old_load_cached_json = pc.load_cached_json
    try:
        pc.is_fresh = lambda *_a, **_k: True  # type: ignore[assignment]
        pc.load_cached_json = lambda *_a, **_k: {  # type: ignore[assignment]
            "as_of_utc": "2026-04-12T00:00:00+00:00",
            "locked_shares_by_symbol": [],
            "cash_secured_by_symbol_by_ccy": {},
        }
        logs: list[str] = []
        out, refreshed = pc.load_option_positions_context(
            py="python",
            base=Path("."),
            pm_config="x.json",
            market="富途",
            account=None,
            ttl_sec=3600,
            timeout_sec=1,
            is_scheduled=True,
            report_dir=Path("."),
            state_dir=Path("."),
            shared_state_dir=Path("."),
            log=logs.append,
        )
        assert out is None
        assert refreshed is False
        assert any("option positions context not available" in x for x in logs)
    finally:
        pc.is_fresh = old_is_fresh  # type: ignore[assignment]
        pc.load_cached_json = old_load_cached_json  # type: ignore[assignment]


def test_load_context_persists_source_snapshots_for_valid_cached_contracts() -> None:
    import scripts.pipeline_context as pc

    old_is_fresh = pc.is_fresh
    old_load_cached_json = pc.load_cached_json
    old_append = pc.state_repo.append_source_snapshot_event
    try:
        pc.is_fresh = lambda *_a, **_k: True  # type: ignore[assignment]
        captured: list[dict] = []

        def _append(_base, payload, run_id=None):  # type: ignore[no-untyped-def]
            captured.append(payload)
            return {}

        pc.state_repo.append_source_snapshot_event = _append  # type: ignore[assignment]

        def _load_cached(path: Path):  # type: ignore[no-untyped-def]
            if path.name == "portfolio_context.json":
                return {
                    "as_of_utc": "2026-04-12T00:00:00+00:00",
                    "stocks_by_symbol": {"AAPL": {"shares": 100}},
                    "cash_by_currency": {"USD": 100.0},
                }
            return {
                "as_of_utc": "2026-04-12T00:00:00+00:00",
                "locked_shares_by_symbol": {"AAPL": 100},
                "cash_secured_by_symbol_by_ccy": {"AAPL": {"USD": 1000.0}},
            }

        pc.load_cached_json = _load_cached  # type: ignore[assignment]

        logs: list[str] = []
        pctx = pc.load_portfolio_context(
            py="python",
            base=Path("."),
            pm_config="x.json",
            market="富途",
            account=None,
            ttl_sec=3600,
            timeout_sec=1,
            is_scheduled=True,
            state_dir=Path("."),
            shared_state_dir=Path("."),
            log=logs.append,
        )
        octx, refreshed = pc.load_option_positions_context(
            py="python",
            base=Path("."),
            pm_config="x.json",
            market="富途",
            account=None,
            ttl_sec=3600,
            timeout_sec=1,
            is_scheduled=True,
            report_dir=Path("."),
            state_dir=Path("."),
            shared_state_dir=Path("."),
            log=logs.append,
        )
        assert pctx is not None
        assert octx is not None
        assert refreshed is False
        assert {str(x.get("source_name")) for x in captured} == {"holdings", "option_positions"}
    finally:
        pc.is_fresh = old_is_fresh  # type: ignore[assignment]
        pc.load_cached_json = old_load_cached_json  # type: ignore[assignment]
        pc.state_repo.append_source_snapshot_event = old_append  # type: ignore[assignment]


def main() -> None:
    test_load_portfolio_context_rejects_invalid_cached_contract()
    test_load_option_positions_context_rejects_invalid_cached_contract()
    test_load_context_persists_source_snapshots_for_valid_cached_contracts()
    print("OK (pipeline-context-contract)")


if __name__ == "__main__":
    main()
