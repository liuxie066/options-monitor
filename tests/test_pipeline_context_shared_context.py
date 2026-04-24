from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _portfolio_ctx(account: str, *, usd_cash: float, shares: int) -> dict:
    return {
        "as_of_utc": "2026-04-14T00:00:00+00:00",
        "filters": {"market": "富途", "account": account},
        "cash_by_currency": {"USD": usd_cash},
        "stocks_by_symbol": {
            "NVDA": {
                "symbol": "NVDA",
                "shares": shares,
                "avg_cost": 100.0,
                "currency": "USD",
                "market": "富途美股",
                "account": account,
            }
        },
        "raw_selected_count": 2,
    }


def _option_ctx(account: str, *, locked: int) -> dict:
    return {
        "as_of_utc": "2026-04-14T00:00:00+00:00",
        "filters": {"broker": "富途", "account": account},
        "locked_shares_by_symbol": {"NVDA": locked},
        "cash_secured_by_symbol_by_ccy": {"NVDA": {"USD": 1000.0}},
        "cash_secured_total_by_ccy": {"USD": 1000.0},
        "cash_secured_total_cny": 7200.0,
        "exchange_rates": {"rates": {"USDCNY": 7.2}},
        "raw_selected_count": 1,
        "open_positions_min": [],
    }


def test_build_pipeline_context_resolves_portfolio_source_by_account() -> None:
    import scripts.pipeline_context as pc

    captured: dict[str, object] = {}
    old_load_portfolio_context = pc.load_portfolio_context
    old_load_option_positions_context = pc.load_option_positions_context
    old_auto_close = pc.maybe_auto_close_expired_positions
    old_load_exchange_rates = pc.load_exchange_rates
    try:
        def _fake_load_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
            captured["portfolio_source"] = kwargs.get("portfolio_source")
            captured["account"] = kwargs.get("account")
            return {"portfolio_source_name": kwargs.get("portfolio_source")}

        def _fake_load_option_positions_context(**_kwargs):  # type: ignore[no-untyped-def]
            return None, False

        pc.load_portfolio_context = _fake_load_portfolio_context  # type: ignore[assignment]
        pc.load_option_positions_context = _fake_load_option_positions_context  # type: ignore[assignment]
        pc.maybe_auto_close_expired_positions = lambda **_kwargs: None  # type: ignore[assignment]
        pc.load_exchange_rates = lambda **_kwargs: (None, None)  # type: ignore[assignment]

        with TemporaryDirectory() as td:
            root = Path(td).resolve()
            portfolio_ctx, option_ctx, usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate = pc.build_pipeline_context(
                py="python",
                base=root,
                cfg={
                    "portfolio": {
                        "data_config": "x.json",
                        "broker": "富途",
                        "account": "sy",
                        "source": "auto",
                        "source_by_account": {"sy": "holdings"},
                    }
                },
                report_dir=(root / "reports").resolve(),
                portfolio_timeout_sec=1,
                runtime={},
                is_scheduled=True,
                state_dir=(root / "state").resolve(),
                shared_state_dir=(root / "shared").resolve(),
                log=lambda _msg: None,
                no_context=False,
                want_scan=True,
            )
        assert portfolio_ctx == {"portfolio_source_name": "holdings"}
        assert option_ctx is None
        assert usd_per_cny_exchange_rate is None
        assert cny_per_hkd_exchange_rate is None
        assert captured == {"portfolio_source": "holdings", "account": "sy"}
    finally:
        pc.load_portfolio_context = old_load_portfolio_context  # type: ignore[assignment]
        pc.load_option_positions_context = old_load_option_positions_context  # type: ignore[assignment]
        pc.maybe_auto_close_expired_positions = old_auto_close  # type: ignore[assignment]
        pc.load_exchange_rates = old_load_exchange_rates  # type: ignore[assignment]


def test_shared_context_reuses_fetch_calls_across_accounts() -> None:
    import scripts.pipeline_context as pc
    import scripts.portfolio_context_service as pcs

    shared_portfolio = {
        "as_of_utc": "2026-04-14T00:00:00+00:00",
        "filters": {"market": "富途"},
        "all_accounts": _portfolio_ctx("", usd_cash=2500.0, shares=300),
        "by_account": {
            "lx": _portfolio_ctx("lx", usd_cash=1000.0, shares=100),
            "sy": _portfolio_ctx("sy", usd_cash=1500.0, shares=200),
        },
    }
    shared_option = {
        "as_of_utc": "2026-04-14T00:00:00+00:00",
        "filters": {"broker": "富途"},
        "all_accounts": _option_ctx("", locked=300),
        "by_account": {
            "lx": _option_ctx("lx", locked=100),
            "sy": _option_ctx("sy", locked=200),
        },
    }

    counts = {"portfolio": 0, "option": 0}
    old_is_fresh = pc.is_fresh
    old_load_holdings_portfolio_context = pcs.load_holdings_portfolio_context
    old_load_holdings_portfolio_shared_context = pcs.load_holdings_portfolio_shared_context
    old_load_option_positions_repo = pc.load_option_positions_repo
    old_load_option_position_records = pc.load_option_position_records
    old_build_option_positions_context = pc.build_option_positions_context
    old_build_shared_option_positions_context = pc.build_shared_option_positions_context
    old_load_option_position_exchange_rates = pc._load_option_position_exchange_rates

    try:
        pc.is_fresh = lambda path, ttl_sec: Path(path).exists()  # type: ignore[assignment]
        def _fake_load_holdings_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
            counts["portfolio"] += 1
            return dict(shared_portfolio["by_account"].get(str(kwargs.get("account") or ""), shared_portfolio["all_accounts"]))

        def _fake_load_holdings_portfolio_shared_context(**_kwargs):  # type: ignore[no-untyped-def]
            counts["portfolio"] += 1
            return shared_portfolio

        pcs.load_holdings_portfolio_context = _fake_load_holdings_portfolio_context  # type: ignore[assignment]
        pcs.load_holdings_portfolio_shared_context = _fake_load_holdings_portfolio_shared_context  # type: ignore[assignment]
        pc.load_option_positions_repo = lambda *_a, **_k: object()  # type: ignore[assignment]
        pc.load_option_position_records = lambda *_a, **_k: []  # type: ignore[assignment]
        pc._load_option_position_exchange_rates = lambda **_kwargs: {"rates": {"USDCNY": 7.2}}  # type: ignore[assignment]
        pc.build_shared_option_positions_context = lambda *_a, **_k: (counts.__setitem__("option", counts["option"] + 1) or shared_option)  # type: ignore[assignment]
        pc.build_option_positions_context = lambda *_a, **_k: (counts.__setitem__("option", counts["option"] + 1) or shared_option["all_accounts"])  # type: ignore[assignment]
        logs: list[str] = []
        with TemporaryDirectory() as td:
            root = Path(td).resolve()
            shared_dir = (root / "shared").resolve()
            p1 = pc.load_portfolio_context(
                base=root,
                data_config="x.json",
                market="富途",
                account="lx",
                ttl_sec=3600,
                state_dir=(root / "acct_lx_state").resolve(),
                shared_state_dir=shared_dir,
                log=logs.append,
            )
            p2 = pc.load_portfolio_context(
                base=root,
                data_config="x.json",
                market="富途",
                account="sy",
                ttl_sec=3600,
                state_dir=(root / "acct_sy_state").resolve(),
                shared_state_dir=shared_dir,
                log=logs.append,
            )
            o1, r1 = pc.load_option_positions_context(
                base=root,
                data_config="x.json",
                market="富途",
                account="lx",
                ttl_sec=3600,
                state_dir=(root / "acct_lx_state").resolve(),
                shared_state_dir=shared_dir,
                log=logs.append,
            )
            o2, r2 = pc.load_option_positions_context(
                base=root,
                data_config="x.json",
                market="富途",
                account="sy",
                ttl_sec=3600,
                state_dir=(root / "acct_sy_state").resolve(),
                shared_state_dir=shared_dir,
                log=logs.append,
            )
        assert counts["portfolio"] == 1
        assert counts["option"] == 1
        assert p1 and p2
        assert o1 and o2
        assert r1 is True
        assert r2 is True
        assert p1["context_source"] == "shared_refresh"
        assert p2["context_source"] == "shared_slice"
        assert o1["context_source"] == "shared_refresh"
        assert o2["context_source"] == "shared_slice"
        assert p1["cash_by_currency"]["USD"] == 1000.0
        assert p2["cash_by_currency"]["USD"] == 1500.0
        assert o1["locked_shares_by_symbol"]["NVDA"] == 100
        assert o2["locked_shares_by_symbol"]["NVDA"] == 200
        assert any("portfolio_context source=shared_slice account=sy" in x for x in logs)
        assert any("option_positions_context source=shared_slice account=sy" in x for x in logs)
    finally:
        pc.is_fresh = old_is_fresh  # type: ignore[assignment]
        pcs.load_holdings_portfolio_context = old_load_holdings_portfolio_context  # type: ignore[assignment]
        pcs.load_holdings_portfolio_shared_context = old_load_holdings_portfolio_shared_context  # type: ignore[assignment]
        pc.load_option_positions_repo = old_load_option_positions_repo  # type: ignore[assignment]
        pc.load_option_position_records = old_load_option_position_records  # type: ignore[assignment]
        pc.build_option_positions_context = old_build_option_positions_context  # type: ignore[assignment]
        pc.build_shared_option_positions_context = old_build_shared_option_positions_context  # type: ignore[assignment]
        pc._load_option_position_exchange_rates = old_load_option_position_exchange_rates  # type: ignore[assignment]


def test_shared_slice_matches_legacy_key_fields() -> None:
    from scripts.fetch_option_positions_context import (
        build_context as build_option_context,
        build_shared_context as build_option_shared_context,
        slice_shared_context_for_account as slice_option_shared_context,
    )
    from scripts.fetch_portfolio_context import (
        build_context as build_portfolio_context,
        build_shared_context as build_portfolio_shared_context,
        slice_shared_context_for_account as slice_portfolio_shared_context,
    )

    holdings_records = [
        {"fields": {"market": "富途美股", "account": "lx", "asset_type": "cash", "asset_id": "USD-CASH", "currency": "USD", "quantity": "1000"}},
        {"fields": {"market": "富途美股", "account": "lx", "asset_type": "us_stock", "asset_id": "NVDA", "asset_name": "NVIDIA", "currency": "USD", "quantity": "10", "avg_cost": "100"}},
        {"fields": {"market": "富途美股", "account": "sy", "asset_type": "cash", "asset_id": "USD-CASH", "currency": "USD", "quantity": "2000"}},
        {"fields": {"market": "富途美股", "account": "sy", "asset_type": "us_stock", "asset_id": "AAPL", "asset_name": "Apple", "currency": "USD", "quantity": "20", "avg_cost": "150"}},
    ]
    option_records = [
        {
            "record_id": "r1",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "status": "open",
                "symbol": "NVDA",
                "option_type": "call",
                "side": "short",
                "contracts": "1",
                "underlying_share_locked": "100",
            },
        },
        {
            "record_id": "r2",
            "fields": {
                "broker": "富途",
                "account": "sy",
                "status": "open",
                "symbol": "AAPL",
                "option_type": "put",
                "side": "short",
                "contracts": "1",
                "cash_secured_amount": "500",
                "currency": "USD",
            },
        },
    ]

    legacy_portfolio = build_portfolio_context(holdings_records, market="富途", account="lx")
    shared_portfolio = build_portfolio_shared_context(holdings_records, market="富途")
    sliced_portfolio = slice_portfolio_shared_context(shared_portfolio, "lx")
    assert sliced_portfolio is not None
    assert sliced_portfolio["filters"] == legacy_portfolio["filters"]
    assert sliced_portfolio["cash_by_currency"] == legacy_portfolio["cash_by_currency"]
    assert sliced_portfolio["stocks_by_symbol"] == legacy_portfolio["stocks_by_symbol"]

    rates = {"rates": {"USDCNY": 7.2}}
    legacy_option = build_option_context(option_records, broker="富途", account="lx", rates=rates)
    shared_option = build_option_shared_context(option_records, broker="富途", rates=rates)
    sliced_option = slice_option_shared_context(shared_option, "lx")
    assert sliced_option is not None
    assert sliced_option["filters"] == legacy_option["filters"]
    assert sliced_option["locked_shares_by_symbol"] == legacy_option["locked_shares_by_symbol"]
    assert sliced_option["cash_secured_by_symbol_by_ccy"] == legacy_option["cash_secured_by_symbol_by_ccy"]
    assert sliced_option["open_positions_min"] == legacy_option["open_positions_min"]


def test_load_portfolio_context_auto_prefers_futu_when_available() -> None:
    import scripts.pipeline_context as pc

    old_fetch = pc.fetch_futu_portfolio_context
    try:
        pc.fetch_futu_portfolio_context = lambda **_kwargs: {  # type: ignore[assignment]
            "as_of_utc": "2026-04-14T00:00:00+00:00",
            "filters": {"market": "富途", "account": "lx"},
            "cash_by_currency": {"CNY": 120000.0},
            "stocks_by_symbol": {},
            "raw_selected_count": 1,
            "portfolio_source_name": "futu",
        }

        logs: list[str] = []
        with TemporaryDirectory() as td:
            root = Path(td).resolve()
            out = pc.load_portfolio_context(
                base=root,
                data_config="x.json",
                market="富途",
                account="lx",
                ttl_sec=0,
                state_dir=(root / "state").resolve(),
                shared_state_dir=(root / "shared").resolve(),
                log=logs.append,
                runtime_config={"portfolio": {"source": "auto", "base_currency": "CNY"}},
                portfolio_source="auto",
            )
        assert out is not None
        assert out["portfolio_source_name"] == "futu"
        assert out["context_source"] == "futu_direct"
        assert any("portfolio_context source=futu_direct account=lx" in x for x in logs)
    finally:
        pc.fetch_futu_portfolio_context = old_fetch  # type: ignore[assignment]


def test_load_portfolio_context_auto_skips_fresh_holdings_cache_and_uses_futu() -> None:
    import scripts.pipeline_context as pc

    old_is_fresh = pc.is_fresh
    old_load_cached_json = pc.load_cached_json
    old_fetch = pc.fetch_futu_portfolio_context
    try:
        pc.is_fresh = lambda *_a, **_k: True  # type: ignore[assignment]

        def _load_cached(path: Path):  # type: ignore[no-untyped-def]
            if path.name == "portfolio_context.json":
                return {
                    "as_of_utc": "2026-04-14T00:00:00+00:00",
                    "filters": {"market": "富途", "account": "lx"},
                    "cash_by_currency": {"CNY": 88000.0},
                    "stocks_by_symbol": {},
                    "raw_selected_count": 1,
                    "portfolio_source_name": "holdings",
                }
            return None

        pc.load_cached_json = _load_cached  # type: ignore[assignment]
        pc.fetch_futu_portfolio_context = lambda **_kwargs: {  # type: ignore[assignment]
            "as_of_utc": "2026-04-14T00:01:00+00:00",
            "filters": {"market": "富途", "account": "lx"},
            "cash_by_currency": {"CNY": 120000.0},
            "stocks_by_symbol": {},
            "raw_selected_count": 1,
            "portfolio_source_name": "futu",
        }

        logs: list[str] = []
        with TemporaryDirectory() as td:
            root = Path(td).resolve()
            out = pc.load_portfolio_context(
                base=root,
                data_config="x.json",
                market="富途",
                account="lx",
                ttl_sec=3600,
                state_dir=(root / "state").resolve(),
                shared_state_dir=(root / "shared").resolve(),
                log=logs.append,
                runtime_config={"portfolio": {"source": "auto", "base_currency": "CNY"}},
                portfolio_source="auto",
            )
        assert out is not None
        assert out["portfolio_source_name"] == "futu"
        assert out["context_source"] == "futu_direct"
        assert any("portfolio_context source=futu_direct account=lx" in x for x in logs)
    finally:
        pc.is_fresh = old_is_fresh  # type: ignore[assignment]
        pc.load_cached_json = old_load_cached_json  # type: ignore[assignment]
        pc.fetch_futu_portfolio_context = old_fetch  # type: ignore[assignment]


def test_load_portfolio_context_auto_falls_back_to_holdings_when_futu_unavailable() -> None:
    import scripts.pipeline_context as pc
    import scripts.portfolio_context_service as pcs

    old_fetch = pc.fetch_futu_portfolio_context
    old_load_holdings_portfolio_shared_context = pcs.load_holdings_portfolio_shared_context

    try:
        pc.fetch_futu_portfolio_context = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("opend down"))  # type: ignore[assignment]
        pcs.load_holdings_portfolio_shared_context = lambda **_kwargs: {  # type: ignore[assignment]
            "as_of_utc": "2026-04-14T00:00:00+00:00",
            "filters": {"market": "富途"},
            "all_accounts": {
                "as_of_utc": "2026-04-14T00:00:00+00:00",
                "filters": {"market": "富途", "account": "lx"},
                "cash_by_currency": {"CNY": 88000.0},
                "stocks_by_symbol": {},
                "raw_selected_count": 1,
            },
            "by_account": {
                "lx": {
                    "as_of_utc": "2026-04-14T00:00:00+00:00",
                    "filters": {"market": "富途", "account": "lx"},
                    "cash_by_currency": {"CNY": 88000.0},
                    "stocks_by_symbol": {},
                    "raw_selected_count": 1,
                }
            },
        }

        logs: list[str] = []
        with TemporaryDirectory() as td:
            root = Path(td).resolve()
            out = pc.load_portfolio_context(
                base=root,
                data_config="x.json",
                market="富途",
                account="lx",
                ttl_sec=0,
                state_dir=(root / "state").resolve(),
                shared_state_dir=(root / "shared").resolve(),
                log=logs.append,
                runtime_config={"portfolio": {"source": "auto", "base_currency": "CNY"}},
                portfolio_source="auto",
            )
        assert out is not None
        assert out["portfolio_source_name"] == "holdings"
        assert out["context_source"] == "shared_refresh"
        assert any("fallback to holdings" in x for x in logs)
    finally:
        pc.fetch_futu_portfolio_context = old_fetch  # type: ignore[assignment]
        pcs.load_holdings_portfolio_shared_context = old_load_holdings_portfolio_shared_context  # type: ignore[assignment]


def test_load_portfolio_context_auto_reuses_local_holdings_cache_when_futu_and_fetch_fail() -> None:
    import scripts.pipeline_context as pc

    old_is_fresh = pc.is_fresh
    old_load_cached_json = pc.load_cached_json
    old_fetch = pc.fetch_futu_portfolio_context
    try:
        pc.is_fresh = lambda path, ttl_sec: Path(path).name == "portfolio_context.json"  # type: ignore[assignment]

        def _load_cached(path: Path):  # type: ignore[no-untyped-def]
            if path.name == "portfolio_context.json":
                return {
                    "as_of_utc": "2026-04-14T00:00:00+00:00",
                    "filters": {"market": "富途", "account": "lx"},
                    "cash_by_currency": {"CNY": 88000.0},
                    "stocks_by_symbol": {},
                    "raw_selected_count": 1,
                    "portfolio_source_name": "holdings",
                }
            return None

        pc.load_cached_json = _load_cached  # type: ignore[assignment]
        pc.fetch_futu_portfolio_context = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("opend down"))  # type: ignore[assignment]

        logs: list[str] = []
        with TemporaryDirectory() as td:
            root = Path(td).resolve()
            out = pc.load_portfolio_context(
                base=root,
                data_config="x.json",
                market="富途",
                account="lx",
                ttl_sec=3600,
                state_dir=(root / "state").resolve(),
                shared_state_dir=(root / "shared").resolve(),
                log=logs.append,
                runtime_config={"portfolio": {"source": "auto", "base_currency": "CNY"}},
                portfolio_source="auto",
            )
        assert out is not None
        assert out["portfolio_source_name"] == "holdings"
        assert out["context_source"] == "account_cache"
        assert any("fallback to holdings" in x for x in logs)
        assert any("portfolio_context source=account_cache account=lx" in x for x in logs)
    finally:
        pc.is_fresh = old_is_fresh  # type: ignore[assignment]
        pc.load_cached_json = old_load_cached_json  # type: ignore[assignment]
        pc.fetch_futu_portfolio_context = old_fetch  # type: ignore[assignment]


def main() -> None:
    test_shared_context_reuses_fetch_calls_across_accounts()
    test_shared_slice_matches_legacy_key_fields()
    print("OK (pipeline-context-shared)")


if __name__ == "__main__":
    main()
