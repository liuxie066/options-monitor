from __future__ import annotations

from pathlib import Path

import pandas as pd


def test_enrich_sell_put_candidates_with_cash_adds_total_cny_columns(tmp_path: Path) -> None:
    from scripts.exchange_rates import CurrencyConverter, ExchangeRates
    from scripts.sell_put_cash import enrich_sell_put_candidates_with_cash

    df = pd.DataFrame(
        [
            {
                "symbol": "0700.HK",
                "strike": 450.0,
                "multiplier": 100,
                "currency": "HKD",
            }
        ]
    )
    out_path = tmp_path / "sell_put_candidates_labeled.csv"

    result = enrich_sell_put_candidates_with_cash(
        df_labeled=df,
        symbol="0700.HK",
        portfolio_ctx={
            "cash_by_currency": {"CNY": 10000.0, "HKD": 1000.0},
            "option_ctx": {
                "cash_secured_total_by_ccy": {"HKD": 500.0},
                "cash_secured_total_cny": 460.0,
                "cash_secured_by_symbol_by_ccy": {"0700.HK": {"HKD": 500.0}},
            },
        },
        exchange_rate_converter=CurrencyConverter(ExchangeRates(cny_per_hkd=0.92)),
        out_path=out_path,
    )

    row = result.iloc[0]
    assert row["cash_available_total_cny"] == 10920.0
    assert row["cash_free_total_cny"] == 10460.0
    assert row["cash_available_cny"] == 10000.0
    assert row["cash_free_cny"] == 9540.0


def test_render_sell_put_alerts_shows_total_cny_when_base_cny_missing(tmp_path: Path) -> None:
    from scripts.render_sell_put_alerts import render_sell_put_alerts

    csv_path = tmp_path / "sell_put_candidates_labeled.csv"
    out_path = tmp_path / "sell_put_alerts.txt"

    pd.DataFrame(
        [
            {
                "symbol": "0700.HK",
                "expiration": "2026-06-29",
                "strike": 450.0,
                "spot": 500.0,
                "dte": 60,
                "mid": 14.375,
                "net_income": 1416.5,
                "annualized_net_return_on_cash_basis": 0.1977,
                "otm_pct": 0.1,
                "risk_label": "中性",
                "spread_ratio": 0.1,
                "open_interest": 100,
                "volume": 50,
                "currency": "HKD",
                "cash_required_cny": 39280.0,
                "cash_available_total_cny": 531694.0,
                "cash_free_total_cny": 11666.0,
            }
        ]
    ).to_csv(csv_path, index=False)

    text = render_sell_put_alerts(
        input_path=csv_path,
        output_path=out_path,
        top=1,
        layered=True,
    )

    assert "总现金折算(CNY): ¥531,694" in text
    assert "总可用折算(扣占用, CNY): ¥11,666" in text
    assert "加仓后余量(总折算估算, CNY): ¥-27,614" in text
    assert "判断: 所需担保现金约 ¥39,280，但当前总可用折算约 ¥11,666" in text


def test_render_sell_put_alerts_shows_usd_cash_guard_when_cny_missing(tmp_path: Path) -> None:
    from scripts.render_sell_put_alerts import render_sell_put_alerts

    csv_path = tmp_path / "sell_put_candidates_labeled.csv"
    out_path = tmp_path / "sell_put_alerts.txt"

    pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "expiration": "2026-06-29",
                "strike": 180.0,
                "spot": 200.0,
                "dte": 60,
                "mid": 2.15,
                "net_income": 210.0,
                "annualized_net_return_on_cash_basis": 0.18,
                "otm_pct": 0.1,
                "risk_label": "中性",
                "spread_ratio": 0.1,
                "open_interest": 100,
                "volume": 50,
                "currency": "USD",
                "cash_required_usd": 18000.0,
                "cash_free_usd": 15000.0,
            }
        ]
    ).to_csv(csv_path, index=False)

    text = render_sell_put_alerts(
        input_path=csv_path,
        output_path=out_path,
        top=1,
        layered=True,
    )

    assert "判断: 所需担保现金约 $18,000，但当前账户可用担保现金约 $15,000" in text
    assert "担保现金需求(生效口径, USD): $18,000" in text
    assert "账户可用担保现金(USD): $15,000" in text
    assert "加仓后余量(生效口径, USD): $-3,000" in text
