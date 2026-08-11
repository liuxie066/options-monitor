from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def _add_repo_to_syspath() -> Path:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return base


def _formal_metric_row(*, mode: str, currency: str, **overrides):  # type: ignore[no-untyped-def]
    market = "HK" if currency == "HKD" else "US"
    owner = "HK.00700" if market == "HK" else "US.NVDA"
    row = {
        "symbol": "0700.HK" if market == "HK" else "NVDA",
        "market": market,
        "option_type": mode,
        "expiration": "2026-06-19",
        "contract_symbol": f"{owner}-{mode}",
        "currency": currency,
        "dte": 21,
        "strike": 100.0,
        "spot": 110.0,
        "bid": 1.0,
        "ask": 1.2,
        "price_tick": 0.01,
        "implied_volatility": 0.30,
        "term_matched_rv": 0.20,
        "term_matched_rv_status": "ok",
        "underlier_observation_status": "ready",
        "option_standard_type": "STANDARD",
        "stock_owner": owner,
        "stock_type": "DRVT",
        "chain_multiplier": 100,
        "snapshot_multiplier": 100,
        "multiplier": 100,
        "opening_contract_status": "ready",
        "snapshot_received_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    row.update(overrides)
    return row


def test_calc_futu_hk_option_fee_applies_minimum_commission_and_system_fee() -> None:
    _add_repo_to_syspath()
    from domain.domain.fee_calc import calc_futu_hk_option_fee

    # 交易金额 1 * 100 * 1 = 100，佣金 0.2 = 最低 3.0，另加平台费 15 和系统费 3
    out = calc_futu_hk_option_fee(1.0, contracts=1, multiplier=100, is_sell=True)

    assert out == 21.0


def test_calc_futu_hk_option_fee_scales_system_fee_by_contracts() -> None:
    _add_repo_to_syspath()
    from domain.domain.fee_calc import calc_futu_hk_option_fee

    # 交易金额 2 * 100 * 3 = 600，佣金 1.2 -> 最低 3.0，平台费 15，系统费 9
    out = calc_futu_hk_option_fee(2.0, contracts=3, multiplier=100, is_sell=False)

    assert out == 27.0


def test_calc_futu_hk_option_fee_waives_tariff_at_exact_one_cent() -> None:
    _add_repo_to_syspath()
    from domain.domain.fee_calc import calc_futu_hk_option_fee

    out = calc_futu_hk_option_fee(0.01, contracts=3, multiplier=100, is_sell=True)

    assert out == 18.0


def test_calc_futu_us_option_fee_uses_standard_commission_and_sell_regulatory_fees() -> None:
    _add_repo_to_syspath()
    from domain.domain.fee_calc import calc_futu_us_option_fee

    out = calc_futu_us_option_fee(0.5, contracts=2, multiplier=100, is_sell=True)

    assert round(out, 6) == round(1.99 + 0.6 + 0.026 + 0.04 + 0.36 + 0.0006 + 0.01 + 0.01, 6)


def test_calc_futu_us_option_fee_uses_low_premium_tier_and_buy_has_no_sell_only_fees() -> None:
    _add_repo_to_syspath()
    from domain.domain.fee_calc import calc_futu_us_option_fee

    out = calc_futu_us_option_fee(0.05, contracts=1, multiplier=100, is_sell=False)

    assert round(out, 6) == round(1.99 + 0.3 + 0.013 + 0.02 + 0.18 + 0.0003, 6)


def test_calc_futu_us_option_fee_caps_occ_fee_per_order() -> None:
    _add_repo_to_syspath()
    from domain.domain.fee_calc import calc_futu_us_option_fee

    out = calc_futu_us_option_fee(1.0, contracts=3000, multiplier=100, is_sell=False)

    assert out == round(1950.0 + 900.0 + 39.0 + 55.0 + 540.0 + 0.9, 6)


def test_calc_futu_option_fee_requires_positive_multiplier() -> None:
    _add_repo_to_syspath()
    from domain.domain.fee_calc import calc_futu_option_fee

    try:
        calc_futu_option_fee("USD", 1.0, contracts=1, multiplier=0, is_sell=True)
    except ValueError as exc:
        assert "multiplier" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_calc_futu_option_fee_uses_shared_currency_aliases() -> None:
    _add_repo_to_syspath()
    from domain.domain.fee_calc import calc_futu_option_fee

    out = calc_futu_option_fee("港币", 1.0, contracts=1, multiplier=100, is_sell=True)

    assert out == 21.0


def test_calc_futu_option_fee_rejects_missing_or_unsupported_currency() -> None:
    _add_repo_to_syspath()
    from domain.domain.fee_calc import calc_futu_option_fee

    for currency in (None, "", "CNY", "EUR"):
        try:
            calc_futu_option_fee(currency, 1.0, contracts=1, multiplier=100, is_sell=True)
        except ValueError as exc:
            assert "USD or HKD" in str(exc)
        else:
            raise AssertionError(f"expected ValueError for currency={currency!r}")


def test_extract_actual_fees_prefers_explicit_total_then_components() -> None:
    _add_repo_to_syspath()
    from domain.domain.fee_calc import extract_actual_fees

    total = extract_actual_fees({"total_fee": 3.5, "commission": 2.0, "platform_fee": 1.0})
    components = extract_actual_fees({"commission": 2.0, "platform_fee": 1.0})

    assert total == {"amount": 3.5, "source": "raw_payload.total_fee", "components": ["total_fee"]}
    assert components == {
        "amount": 3.0,
        "source": "raw_payload.components",
        "components": ["commission", "platform_fee"],
    }
    assert extract_actual_fees({}) is None

    signed = extract_actual_fees({"commission": -0.99, "reg_fee": -0.01})
    assert signed == {
        "amount": 1.0,
        "source": "raw_payload.components",
        "components": ["commission", "reg_fee"],
    }
    assert extract_actual_fees({"charges": -2.5}) == {
        "amount": 2.5,
        "source": "raw_payload.charges",
        "components": ["charges"],
    }


def test_calc_futu_us_stock_fee_includes_sell_only_regulatory_fees() -> None:
    _add_repo_to_syspath()
    from domain.domain.fee_calc import calc_futu_us_stock_fee

    sell = calc_futu_us_stock_fee(105.0, shares=100, is_sell=True)
    buy = calc_futu_us_stock_fee(105.0, shares=100, is_sell=False)

    assert sell == 2.5261
    assert buy == 2.2903


def test_calc_futu_hk_stock_fee_rounds_stamp_duty_up() -> None:
    _add_repo_to_syspath()
    from domain.domain.fee_calc import calc_futu_hk_stock_fee

    out = calc_futu_hk_stock_fee(10.01, shares=100, is_sell=True)

    assert out == round(3.0 + 15.0 + 1001 * 0.000042 + 2.0 + 1001 * 0.0000565 + 1001 * 0.000027 + 1001 * 0.0000015, 6)


def test_broker_trade_writer_preserves_actual_fee_provenance() -> None:
    _add_repo_to_syspath()
    from types import SimpleNamespace

    from src.application.ledger.writer import _trade_event_from_normalized_deal

    event = _trade_event_from_normalized_deal(
        SimpleNamespace(
            broker="富途",
            futu_account_id="1",
            internal_account="lx",
            deal_id="deal-fee-1",
            order_id="order-fee-1",
            symbol="NVDA",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=2.5,
            strike=100.0,
            multiplier=100,
            multiplier_source="payload",
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1_780_000_000_000,
            raw_payload={"commission": 1.99, "platform_fee": 0.3},
        )
    )

    assert event.fees == 2.29
    assert event.raw_payload["fee_provenance"] == {
        "basis": "actual",
        "source": "raw_payload.components",
        "components": ["commission", "platform_fee"],
    }


def test_sell_put_compute_metrics_uses_full_fee_formula() -> None:
    from datetime import datetime, timezone
    _add_repo_to_syspath()
    from src.application.scan_sell_put import compute_metrics

    row = pd.Series(
        _formal_metric_row(
            mode="put",
            currency="USD",
            bid=0.49,
            ask=0.51,
            strike=90.0,
            spot=100.0,
            dte=14,
            snapshot_received_at_utc="2026-04-01T14:59:00Z",
        )
    )

    out = compute_metrics(row, now_utc=datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc))

    assert out is not None
    assert round(out["futu_fee"], 6) == round(1.99 + 0.6 + 0.013 + 0.02 + 0.18 + 0.0003 + 0.01 + 0.01, 6)
    assert round(out["net_income"], 6) == round(50.0 - out["futu_fee"], 6)


def test_sell_call_compute_metrics_uses_full_fee_formula() -> None:
    _add_repo_to_syspath()
    from src.application.scan_sell_call import compute_metrics

    row = pd.Series(
        _formal_metric_row(
            mode="call",
            currency="HKD",
            bid=7.9,
            ask=8.1,
            strike=480.0,
            spot=500.0,
            dte=21,
        )
    )

    out = compute_metrics(row, avg_cost=430.0)

    assert out is not None
    assert out["futu_fee"] == 21.0
    assert out["net_income"] == 779.0
    assert round(out["annualized_net_premium_return"], 6) == round((779.0 / (500.0 * 100)) * (365 / 21), 6)
    assert round(out["if_exercised_total_return"], 6) == round((((480.0 - 430.0) * 100) + 779.0) / (430.0 * 100), 6)
