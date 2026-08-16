from __future__ import annotations

from src.interfaces.cli.main import parse_args
from src.interfaces.cli.options_data_ops import handle_options_data_command
from src.infrastructure.robinhood_options import RobinhoodOptionsError
from src.infrastructure.xueqiu_client import XueqiuUserStock


def test_options_data_chain_reads_token_from_env_file_and_limits_rows(tmp_path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text('ROBINHOOD_AUTH_TOKEN="secret-token"\n', encoding="utf-8")
    args = parse_args(
        [
            "options-data",
            "chain",
            "--symbol",
            "nvda",
            "--side",
            "put",
            "--limit",
            "1",
            "--env-file",
            str(env_file),
        ]
    )
    captured = {}

    def _fetch(request):
        captured["request"] = request
        return [
            {"contract_symbol": "NVDA260116P00100000"},
            {"contract_symbol": "NVDA260116P00110000"},
        ]

    payload = handle_options_data_command(args, fetch_robinhood_option_chain_fn=_fetch)

    assert payload["ok"] is True
    assert payload["schema_version"] == "options_data_chain.v1"
    assert payload["provider"] == "robinhood"
    assert payload["symbol"] == "NVDA"
    assert payload["requested_mode"] is None
    assert payload["token_configured"] is True
    assert payload["row_count"] == 2
    assert payload["rows"] == [{"contract_symbol": "NVDA260116P00100000"}]
    assert captured["request"].token == "secret-token"
    assert "secret-token" not in str(payload)


def test_options_data_chain_reports_provider_error_without_traceback(monkeypatch) -> None:
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.delenv("ROBINHOOD_AUTH_TOKEN", raising=False)
    args = parse_args(["options-data", "chain", "--symbol", "NVDA", "--limit", "1", "--no-local-env-file"])

    def _fetch(_request):
        raise RobinhoodOptionsError("set ROBINHOOD_AUTH_TOKEN")

    payload = handle_options_data_command(args, fetch_robinhood_option_chain_fn=_fetch)

    assert payload == {
        "ok": False,
        "schema_version": "options_data_chain.v1",
        "provider": "robinhood",
        "symbol": "NVDA",
        "requested_mode": None,
        "token_configured": False,
        "error": "set ROBINHOOD_AUTH_TOKEN",
    }


def test_options_data_blogger_opportunities_scans_us_stocks_with_saved_tokens(tmp_path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text(
        'ROBINHOOD_AUTH_TOKEN="rh-token"\nXUEQIU_COOKIE="xq-cookie"\n',
        encoding="utf-8",
    )
    args = parse_args(
        [
            "options-data",
            "blogger-opportunities",
            "--user-url",
            "https://xueqiu.com/u/1247347556#/stock",
            "--symbols-limit",
            "1",
            "--per-symbol-limit",
            "0",
            "--expiration",
            "2026-09-18",
            "--env-file",
            str(env_file),
        ]
    )
    captured = {"requests": []}

    def _fetch_user_stocks(target: str, **kwargs):
        captured["xueqiu"] = {"target": target, **kwargs}
        return [
            XueqiuUserStock(
                source_user_id="1247347556",
                raw_symbol="SPCX",
                symbol="SPCX",
                name="SpaceX",
                exchange="NASDAQ",
                marketplace="US",
                current=100.0,
            ),
            XueqiuUserStock(
                source_user_id="1247347556",
                raw_symbol="HK00700",
                symbol="0700.HK",
                name="Tencent",
                exchange="HK",
                marketplace="HK",
                current=400.0,
            ),
        ]

    def _fetch_options(request):
        captured["requests"].append(request)
        if request.side == "put":
            return [
                {
                    "contract_symbol": "SPCX260918P00082000",
                    "option_type": "put",
                    "expiration": "2026-09-18",
                    "strike": 82.0,
                    "bid": 8.0,
                    "ask": 8.2,
                    "mid": 8.1,
                    "volume": 100,
                    "open_interest": 1000,
                    "iv": 0.6,
                    "delta": -0.5,
                },
                {
                    "contract_symbol": "SPCX260918P00080000",
                    "option_type": "put",
                    "expiration": "2026-09-18",
                    "strike": 80.0,
                    "bid": 2.0,
                    "ask": 2.2,
                    "mid": 2.1,
                    "volume": 10,
                    "open_interest": 100,
                    "iv": 0.5,
                    "delta": -0.25,
                }
            ]
        raise AssertionError("blogger opportunities should only fetch put chains")

    def _fetch_quotes(symbols, **kwargs):
        captured["quotes"] = {"symbols": symbols, **kwargs}
        return {"SPCX": {"last_trade_price": 100.0}}

    payload = handle_options_data_command(
        args,
        fetch_robinhood_option_chain_fn=_fetch_options,
        fetch_robinhood_stock_quotes_fn=_fetch_quotes,
        fetch_user_stocks_fn=_fetch_user_stocks,
    )

    assert payload["ok"] is True
    assert payload["schema_version"] == "options_data_blogger_opportunities.v1"
    assert payload["selected_symbols"] == ["SPCX"]
    assert payload["source_stock_count"] == 2
    assert payload["us_stock_count"] == 1
    assert payload["quote_count"] == 1
    assert payload["cash_assumption"] == "unlimited_cash"
    assert payload["portfolio_nav_configured"] is False
    assert payload["policy"]["min_out_of_money_pct"] == 0.15
    assert payload["policy"]["min_iv"] == 0.4
    assert payload["opportunity_count"] == 2
    assert payload["evaluated_count"] == 2
    assert payload["returned_count"] == 2
    assert payload["truncated"] is False
    assert {item["strategy"] for item in payload["opportunities"]} == {"sell_put"}
    assert [item["contract_symbol"] for item in payload["opportunities"]] == [
        "SPCX260918P00082000",
        "SPCX260918P00080000",
    ]
    assert {item["underlying_price"] for item in payload["opportunities"]} == {100.0}
    assert payload["opportunities"][0]["final_decision"] == "NO_GO"
    assert "delta_too_high" in payload["opportunities"][0]["hard_vetoes"]
    assert payload["opportunities"][0]["annualized_return_on_cash"] > payload["opportunities"][1]["annualized_return_on_cash"]
    assert payload["opportunities"][1]["final_decision"] in {"GO_SMALL_SIZE", "GO_REDUCED_SIZE", "GO_NORMAL_SIZE"}
    assert payload["opportunities"][1]["effective_basis"] == 77.9
    assert payload["opportunities"][1]["exit_plan"]["time_exit_dte"] == 14
    assert payload["opportunities"][1]["out_of_money_pct"] == 0.2
    assert captured["xueqiu"]["cookie"] == "xq-cookie"
    assert captured["quotes"]["symbols"] == ["SPCX"]
    assert captured["quotes"]["token"] == "rh-token"
    assert [request.side for request in captured["requests"]] == ["put"]
    assert captured["requests"][0].token == "rh-token"
    assert captured["requests"][0].expiration == "2026-09-18"
    assert "rh-token" not in str(payload)
    assert "xq-cookie" not in str(payload)


def test_options_data_blogger_opportunities_scopes_dte_when_expiration_is_blank(tmp_path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text(
        'ROBINHOOD_AUTH_TOKEN="rh-token"\nXUEQIU_COOKIE="xq-cookie"\n',
        encoding="utf-8",
    )
    args = parse_args(
        [
            "options-data",
            "blogger-opportunities",
            "--user-url",
            "https://xueqiu.com/u/1247347556#/stock",
            "--symbols-limit",
            "1",
            "--per-symbol-limit",
            "0",
            "--env-file",
            str(env_file),
        ]
    )

    def _fetch_user_stocks(_target: str, **_kwargs):
        return [
            XueqiuUserStock(
                source_user_id="1247347556",
                raw_symbol="SPCX",
                symbol="SPCX",
                name="SpaceX",
                exchange="NASDAQ",
                marketplace="US",
                current=100.0,
            ),
        ]

    def _fetch_options(_request):
        return [
            {
                "contract_symbol": "SPCX260821P00080000",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": 80.0,
                "bid": 6.0,
                "ask": 6.2,
                "mid": 6.1,
                "volume": 100,
                "open_interest": 1000,
                "iv": 0.6,
                "delta": -0.2,
            },
            {
                "contract_symbol": "SPCX260918P00080000",
                "option_type": "put",
                "expiration": "2026-09-18",
                "strike": 80.0,
                "bid": 2.0,
                "ask": 2.2,
                "mid": 2.1,
                "volume": 100,
                "open_interest": 1000,
                "iv": 0.5,
                "delta": -0.25,
            },
        ]

    def _fetch_quotes(symbols, **_kwargs):
        return {"SPCX": {"last_trade_price": 100.0}}

    payload = handle_options_data_command(
        args,
        fetch_robinhood_option_chain_fn=_fetch_options,
        fetch_robinhood_stock_quotes_fn=_fetch_quotes,
        fetch_user_stocks_fn=_fetch_user_stocks,
    )

    assert payload["ok"] is True
    assert payload["evaluated_count"] == 1
    assert [item["contract_symbol"] for item in payload["opportunities"]] == ["SPCX260918P00080000"]


def test_options_data_blogger_opportunities_excludes_itm_puts_from_strategy_universe(tmp_path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text(
        'ROBINHOOD_AUTH_TOKEN="rh-token"\nXUEQIU_COOKIE="xq-cookie"\n',
        encoding="utf-8",
    )
    args = parse_args(
        [
            "options-data",
            "blogger-opportunities",
            "--user-url",
            "https://xueqiu.com/u/1247347556#/stock",
            "--symbols-limit",
            "1",
            "--per-symbol-limit",
            "0",
            "--expiration",
            "2026-09-18",
            "--env-file",
            str(env_file),
        ]
    )

    def _fetch_user_stocks(_target: str, **_kwargs):
        return [
            XueqiuUserStock(
                source_user_id="1247347556",
                raw_symbol="SPCX",
                symbol="SPCX",
                name="SpaceX",
                exchange="NASDAQ",
                marketplace="US",
                current=100.0,
            ),
        ]

    def _fetch_options(_request):
        return [
            {
                "contract_symbol": "SPCX260918P00120000",
                "option_type": "put",
                "expiration": "2026-09-18",
                "strike": 120.0,
                "bid": 22.0,
                "ask": 22.2,
                "mid": 22.1,
                "volume": 100,
                "open_interest": 1000,
                "iv": 0.8,
                "delta": -0.8,
            },
            {
                "contract_symbol": "SPCX260918P00080000",
                "option_type": "put",
                "expiration": "2026-09-18",
                "strike": 80.0,
                "bid": 2.0,
                "ask": 2.2,
                "mid": 2.1,
                "volume": 100,
                "open_interest": 1000,
                "iv": 0.5,
                "delta": -0.25,
            },
        ]

    def _fetch_quotes(symbols, **_kwargs):
        return {"SPCX": {"last_trade_price": 100.0}}

    payload = handle_options_data_command(
        args,
        fetch_robinhood_option_chain_fn=_fetch_options,
        fetch_robinhood_stock_quotes_fn=_fetch_quotes,
        fetch_user_stocks_fn=_fetch_user_stocks,
    )

    assert payload["evaluated_count"] == 1
    assert [item["contract_symbol"] for item in payload["opportunities"]] == ["SPCX260918P00080000"]


def test_options_data_blogger_opportunities_excludes_near_money_puts_from_strategy_universe(tmp_path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text(
        'ROBINHOOD_AUTH_TOKEN="rh-token"\nXUEQIU_COOKIE="xq-cookie"\n',
        encoding="utf-8",
    )
    args = parse_args(
        [
            "options-data",
            "blogger-opportunities",
            "--user-url",
            "https://xueqiu.com/u/1247347556#/stock",
            "--symbols-limit",
            "1",
            "--per-symbol-limit",
            "0",
            "--expiration",
            "2026-09-18",
            "--env-file",
            str(env_file),
        ]
    )

    def _fetch_user_stocks(_target: str, **_kwargs):
        return [
            XueqiuUserStock(
                source_user_id="1247347556",
                raw_symbol="SPCX",
                symbol="SPCX",
                name="SpaceX",
                exchange="NASDAQ",
                marketplace="US",
                current=100.0,
            ),
        ]

    def _fetch_options(_request):
        return [
            {
                "contract_symbol": "SPCX260918P00095000",
                "option_type": "put",
                "expiration": "2026-09-18",
                "strike": 95.0,
                "bid": 10.0,
                "ask": 10.2,
                "mid": 10.1,
                "volume": 100,
                "open_interest": 1000,
                "iv": 0.8,
                "delta": -0.5,
            },
            {
                "contract_symbol": "SPCX260918P00080000",
                "option_type": "put",
                "expiration": "2026-09-18",
                "strike": 80.0,
                "bid": 2.0,
                "ask": 2.2,
                "mid": 2.1,
                "volume": 100,
                "open_interest": 1000,
                "iv": 0.5,
                "delta": -0.25,
            },
        ]

    def _fetch_quotes(symbols, **_kwargs):
        return {"SPCX": {"last_trade_price": 100.0}}

    payload = handle_options_data_command(
        args,
        fetch_robinhood_option_chain_fn=_fetch_options,
        fetch_robinhood_stock_quotes_fn=_fetch_quotes,
        fetch_user_stocks_fn=_fetch_user_stocks,
    )

    assert payload["evaluated_count"] == 1
    assert [item["contract_symbol"] for item in payload["opportunities"]] == ["SPCX260918P00080000"]
    assert payload["opportunities"][0]["out_of_money_pct"] == 0.2
