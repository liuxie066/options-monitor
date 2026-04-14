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
        "fx_rates": {"rates": {"USDCNY": 7.2}},
        "raw_selected_count": 1,
        "open_positions_min": [],
    }


def test_shared_context_reuses_fetch_calls_across_accounts() -> None:
    import scripts.pipeline_context as pc

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
    old_run_cmd = pc.run_cmd

    def _arg(cmd: list[str], name: str) -> str | None:
        try:
            i = cmd.index(name)
            return cmd[i + 1]
        except Exception:
            return None

    def _fake_run_cmd(cmd: list[str], **_kwargs):  # type: ignore[no-untyped-def]
        script = str(cmd[1]) if len(cmd) > 1 else ""
        out = _arg(cmd, "--out")
        acct = _arg(cmd, "--account")
        shared_out = _arg(cmd, "--shared-out")
        if script.endswith("fetch_portfolio_context.py"):
            counts["portfolio"] += 1
            ctx = (shared_portfolio["by_account"].get(acct) if acct else shared_portfolio["all_accounts"])
            out_path = Path(str(out)).resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(ctx, ensure_ascii=False), encoding="utf-8")
            if shared_out:
                p = Path(str(shared_out)).resolve()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(shared_portfolio, ensure_ascii=False), encoding="utf-8")
            return
        if script.endswith("fetch_option_positions_context.py"):
            counts["option"] += 1
            ctx = (shared_option["by_account"].get(acct) if acct else shared_option["all_accounts"])
            out_path = Path(str(out)).resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(ctx, ensure_ascii=False), encoding="utf-8")
            if shared_out:
                p = Path(str(shared_out)).resolve()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(shared_option, ensure_ascii=False), encoding="utf-8")
            return
        raise AssertionError(f"unexpected command: {cmd}")

    try:
        pc.run_cmd = _fake_run_cmd  # type: ignore[assignment]
        logs: list[str] = []
        with TemporaryDirectory() as td:
            root = Path(td).resolve()
            shared_dir = (root / "shared").resolve()
            p1 = pc.load_portfolio_context(
                py="python",
                base=root,
                pm_config="x.json",
                market="富途",
                account="lx",
                ttl_sec=3600,
                timeout_sec=1,
                is_scheduled=True,
                state_dir=(root / "acct_lx_state").resolve(),
                shared_state_dir=shared_dir,
                log=logs.append,
            )
            p2 = pc.load_portfolio_context(
                py="python",
                base=root,
                pm_config="x.json",
                market="富途",
                account="sy",
                ttl_sec=3600,
                timeout_sec=1,
                is_scheduled=True,
                state_dir=(root / "acct_sy_state").resolve(),
                shared_state_dir=shared_dir,
                log=logs.append,
            )
            o1, r1 = pc.load_option_positions_context(
                py="python",
                base=root,
                pm_config="x.json",
                market="富途",
                account="lx",
                ttl_sec=3600,
                timeout_sec=1,
                is_scheduled=True,
                report_dir=(root / "reports").resolve(),
                state_dir=(root / "acct_lx_state").resolve(),
                shared_state_dir=shared_dir,
                log=logs.append,
            )
            o2, r2 = pc.load_option_positions_context(
                py="python",
                base=root,
                pm_config="x.json",
                market="富途",
                account="sy",
                ttl_sec=3600,
                timeout_sec=1,
                is_scheduled=True,
                report_dir=(root / "reports").resolve(),
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
        assert p1["cash_by_currency"]["USD"] == 1000.0
        assert p2["cash_by_currency"]["USD"] == 1500.0
        assert o1["locked_shares_by_symbol"]["NVDA"] == 100
        assert o2["locked_shares_by_symbol"]["NVDA"] == 200
    finally:
        pc.run_cmd = old_run_cmd  # type: ignore[assignment]


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


def main() -> None:
    test_shared_context_reuses_fetch_calls_across_accounts()
    test_shared_slice_matches_legacy_key_fields()
    print("OK (pipeline-context-shared)")


if __name__ == "__main__":
    main()
