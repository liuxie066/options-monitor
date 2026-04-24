from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

FAKE_FUTU_ACC_ID_LX = "123456789012345678"


def test_query_sell_put_cash_uses_futu_portfolio_context_when_runtime_config_allows_it() -> None:
    import scripts.query_sell_put_cash as m

    calls: list[list[str]] = []

    def fake_run(cmd, cwd, timeout_sec=60):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        script = str(cmd[1]) if len(cmd) > 1 else ""
        assert not script.endswith("fetch_portfolio_context.py")
        return None

    def fake_load_json(path: Path):  # type: ignore[no-untyped-def]
        if path.name == "option_positions_context.json":
            return {
                "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 72000.0}},
                "cash_secured_total_by_ccy": {"CNY": 72000.0},
                "cash_secured_total_cny": 72000.0,
            }
        return {}

    def fake_fetch_futu_portfolio_context(**_kwargs):  # type: ignore[no-untyped-def]
        return {
            "cash_by_currency": {"CNY": 130000.0, "USD": 1000.0},
            "stocks_by_symbol": {},
            "portfolio_source_name": "futu",
        }

    old_run = m.run
    old_load_json = m.load_json
    old_fetch = m.fetch_futu_portfolio_context
    try:
        m.run = fake_run
        m.load_json = fake_load_json
        m.fetch_futu_portfolio_context = fake_fetch_futu_portfolio_context

        out_dir = BASE / "output" / "state" / "test_query_sell_put_cash_futu"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="lx",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={
                "portfolio": {"source": "auto", "base_currency": "CNY"},
                "trade_intake": {"account_mapping": {"futu": {FAKE_FUTU_ACC_ID_LX: "lx"}}},
            },
            no_fx=True,
        )
    finally:
        m.run = old_run
        m.load_json = old_load_json
        m.fetch_futu_portfolio_context = old_fetch

    assert result["portfolio_source_name"] == "futu"
    assert result["cash_available_cny"] == 130000.0
    assert result["cash_free_cny"] == 58000.0
    assert any(str(cmd[1]).endswith("fetch_option_positions_context.py") for cmd in calls)


def test_query_sell_put_cash_uses_account_scoped_portfolio_source_override() -> None:
    import scripts.query_sell_put_cash as m

    calls: list[list[str]] = []

    def fake_run(cmd, cwd, timeout_sec=60):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        script = str(cmd[1]) if len(cmd) > 1 else ""
        if script.endswith("fetch_portfolio_context.py"):
            out_path = Path(str(cmd[cmd.index("--out") + 1]))
            out_path.write_text(
                '{"cash_by_currency":{"CNY":90000.0},"stocks_by_symbol":{},"portfolio_source_name":"holdings"}\n',
                encoding="utf-8",
            )
        return None

    def fake_load_json(path: Path):  # type: ignore[no-untyped-def]
        if path.name == "option_positions_context.json":
            return {
                "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 12000.0}},
                "cash_secured_total_by_ccy": {"CNY": 12000.0},
                "cash_secured_total_cny": 12000.0,
            }
        if path.name == "portfolio_context.json" and path.exists():
            return {"cash_by_currency": {"CNY": 90000.0}, "stocks_by_symbol": {}, "portfolio_source_name": "holdings"}
        return {}

    def fake_fetch_futu_portfolio_context(**_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("futu portfolio context should not run for holdings override")

    old_run = m.run
    old_load_json = m.load_json
    old_fetch = m.fetch_futu_portfolio_context
    try:
        m.run = fake_run
        m.load_json = fake_load_json
        m.fetch_futu_portfolio_context = fake_fetch_futu_portfolio_context

        out_dir = BASE / "output" / "state" / "test_query_sell_put_cash_holdings_override"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="sy",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={
                "portfolio": {
                    "source": "auto",
                    "source_by_account": {"sy": "holdings"},
                    "base_currency": "CNY",
                },
            },
            no_fx=True,
        )
    finally:
        m.run = old_run
        m.load_json = old_load_json
        m.fetch_futu_portfolio_context = old_fetch

    assert result["portfolio_source_name"] == "holdings"
    assert result["cash_available_cny"] == 90000.0
    assert result["cash_free_cny"] == 78000.0
    assert any(str(cmd[1]).endswith("fetch_portfolio_context.py") for cmd in calls)


def test_query_sell_put_cash_uses_holdings_account_mapping_for_external_account() -> None:
    import scripts.query_sell_put_cash as m

    calls: list[list[str]] = []

    def fake_run(cmd, cwd, timeout_sec=60):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        script = str(cmd[1]) if len(cmd) > 1 else ""
        if script.endswith("fetch_portfolio_context.py"):
            assert cmd[cmd.index("--account") + 1] == "Feishu EXT"
            out_path = Path(str(cmd[cmd.index("--out") + 1]))
            out_path.write_text(
                '{"cash_by_currency":{"CNY":50000.0},"stocks_by_symbol":{},"portfolio_source_name":"holdings"}\n',
                encoding="utf-8",
            )
        return None

    def fake_load_json(path: Path):  # type: ignore[no-untyped-def]
        if path.name == "option_positions_context.json":
            return {
                "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 8000.0}},
                "cash_secured_total_by_ccy": {"CNY": 8000.0},
                "cash_secured_total_cny": 8000.0,
            }
        if path.name == "portfolio_context.json" and path.exists():
            return {"cash_by_currency": {"CNY": 50000.0}, "stocks_by_symbol": {}, "portfolio_source_name": "holdings"}
        return {}

    old_run = m.run
    old_load_json = m.load_json
    try:
        m.run = fake_run
        m.load_json = fake_load_json

        out_dir = BASE / "output" / "state" / "test_query_sell_put_cash_external_holdings"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="ext1",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={
                "accounts": ["user1", "ext1"],
                "account_settings": {
                    "ext1": {"type": "external_holdings", "holdings_account": "Feishu EXT"},
                },
                "portfolio": {
                    "source": "auto",
                    "source_by_account": {"ext1": "holdings"},
                    "base_currency": "CNY",
                },
            },
            no_fx=True,
        )
    finally:
        m.run = old_run
        m.load_json = old_load_json

    assert result["portfolio_source_name"] == "holdings"
    assert result["cash_available_cny"] == 50000.0
    assert result["cash_free_cny"] == 42000.0
