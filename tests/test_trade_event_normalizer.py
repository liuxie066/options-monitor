from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts.trade_event_normalizer import normalize_trade_deal


def test_normalize_trade_deal_maps_core_fields() -> None:
    payload = {
        "deal_id": "deal-1",
        "order_id": "order-1",
        "trd_acc_id": "REAL_1",
        "code": "0700.HK",
        "option_type": "PUT",
        "side": "SELL",
        "position_effect": "OPEN",
        "qty": 2,
        "price": "3.93",
        "strike": "480",
        "multiplier": 100,
        "expiration": "20260429",
        "currency": "HKD",
        "create_time": "2026-04-09 13:10:25",
    }

    deal = normalize_trade_deal(payload, futu_account_mapping={"REAL_1": "lx"})

    assert deal.deal_id == "deal-1"
    assert deal.internal_account == "lx"
    assert deal.symbol == "0700.HK"
    assert deal.option_type == "put"
    assert deal.side == "sell"
    assert deal.position_effect == "open"
    assert deal.contracts == 2
    assert deal.price == 3.93
    assert deal.strike == 480.0
    assert deal.multiplier == 100
    assert deal.multiplier_source == "payload"
    assert deal.expiration_ymd == "2026-04-29"
    assert deal.currency == "HKD"
    assert isinstance(deal.trade_time_ms, int)


def test_normalize_trade_deal_keeps_unknown_position_effect_when_missing() -> None:
    deal = normalize_trade_deal(
        {
            "deal_id": "deal-2",
            "account_id": "REAL_2",
            "symbol": "NVDA",
            "option_type": "CALL",
            "trade_side": "BUY",
            "qty": 1,
            "price": 1.23,
            "strike": 100,
            "multiplier": 100,
            "expiry_date": "260618",
            "currency_code": "USD",
        },
        futu_account_mapping={"REAL_2": "sy"},
    )

    assert deal.position_effect is None
    assert deal.internal_account == "sy"
    assert deal.multiplier == 100
    assert deal.multiplier_source == "payload"
    assert deal.expiration_ymd == "2026-06-18"


def test_normalize_trade_deal_recognizes_additional_account_id_fields() -> None:
    deal = normalize_trade_deal(
        {
            "deal_id": "deal-3",
            "trade_acc_id": "987654321",
            "symbol": "NVDA",
            "option_type": "CALL",
            "trade_side": "SELL",
            "position_effect": "OPEN",
            "qty": 1,
            "price": 1.23,
            "strike": 100,
            "multiplier": 100,
            "expiry_date": "260618",
            "currency_code": "USD",
        },
        futu_account_mapping={"987654321": "lx"},
    )

    assert deal.futu_account_id == "987654321"
    assert deal.internal_account == "lx"
    assert deal.visible_account_fields == {"trade_acc_id": "987654321"}
    assert deal.account_mapping_keys == ["987654321"]


def test_normalize_trade_deal_parses_futu_option_code_with_lookup_underlying_fields() -> None:
    deal = normalize_trade_deal(
        {
            "deal_id": "deal-4",
            "futu_account_id": "281756479859383816",
            "code": "HK.POP260528P150000",
            "stock_name": "泡泡玛特",
            "trd_side": "SELL_SHORT",
            "qty": 1,
            "price": 6.3,
            "create_time": "2026-04-28 10:15:56",
        },
        futu_account_mapping={"281756479859383816": "lx"},
    )

    assert deal.internal_account == "lx"
    assert deal.symbol == "9992.HK"
    assert deal.option_type == "put"
    assert deal.side == "sell"
    assert deal.position_effect == "open"
    assert deal.strike == 150.0
    assert deal.expiration_ymd == "2026-05-28"
    assert deal.currency == "HKD"


def test_normalize_trade_deal_accepts_futu_underlying_code_format() -> None:
    deal = normalize_trade_deal(
        {
            "deal_id": "deal-5",
            "futu_account_id": "281756479859383816",
            "code": "HK.POP260528P150000",
            "owner_stock_code": "HK.09992",
            "trd_side": "SELL_SHORT",
            "qty": 1,
            "price": 6.3,
            "create_time": "2026-04-28 10:15:56",
        },
        futu_account_mapping={"281756479859383816": "lx"},
    )

    assert deal.symbol == "9992.HK"
    assert deal.option_type == "put"
    assert deal.position_effect == "open"


def test_normalize_trade_deal_falls_back_to_option_code_root_alias_for_symbol() -> None:
    deal = normalize_trade_deal(
        {
            "deal_id": "deal-6",
            "futu_account_id": "281756479859383816",
            "code": "HK.POP260528P150000",
            "trd_side": "SELL_SHORT",
            "qty": 1,
            "price": 6.3,
            "create_time": "2026-04-28 10:15:56",
        },
        futu_account_mapping={"281756479859383816": "lx"},
    )

    assert deal.symbol == "9992.HK"
    assert deal.option_type == "put"
    assert deal.position_effect == "open"


def test_normalize_trade_deal_canonicalizes_us_prefixed_underlying_symbol() -> None:
    deal = normalize_trade_deal(
        {
            "deal_id": "deal-7",
            "futu_account_id": "REAL_US_1",
            "underlying_symbol": "US.NVDA",
            "option_type": "CALL",
            "trade_side": "SELL",
            "position_effect": "OPEN",
            "qty": 1,
            "price": 1.23,
            "strike": 100,
            "multiplier": 100,
            "expiry_date": "260618",
            "currency_code": "USD",
        },
        futu_account_mapping={"REAL_US_1": "lx"},
    )

    assert deal.symbol == "NVDA"


def test_normalize_trade_deal_uses_contract_metadata_multiplier_with_runtime_context(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _fake_resolver(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return 500, "cache", {
            "canonical_symbol": kwargs["symbol"],
            "selected_source": "cache",
            "attempted_sources": [{"source": "cache", "status": "resolved", "value": 500}],
        }

    monkeypatch.setattr("scripts.trade_event_normalizer.resolve_multiplier_with_source_and_diagnostics", _fake_resolver)

    deal = normalize_trade_deal(
        {
            "deal_id": "deal-8",
            "futu_account_id": "REAL_HK_1",
            "code": "HK.POP260528P150000",
            "owner_stock_code": "HK.09992",
            "trd_side": "SELL_SHORT",
            "qty": 1,
            "price": 6.3,
        },
        futu_account_mapping={"REAL_HK_1": "lx"},
        repo_base=tmp_path,
        config={"runtime": {"option_chain_fetch": {"max_calls": 7}}},
        host="opend-host",
        port=22222,
        opend_fetch_config={"option_chain_max_calls": 7},
    )

    assert deal.symbol == "9992.HK"
    assert deal.multiplier == 500
    assert deal.multiplier_source == "cache"
    assert captured["repo_base"] == tmp_path.resolve()
    assert captured["host"] == "opend-host"
    assert captured["port"] == 22222
    assert captured["opend_fetch_config"] == {"option_chain_max_calls": 7}
    assert deal.normalization_diagnostics["multiplier_resolution"]["selected_source"] == "cache"


def test_normalize_trade_deal_uses_static_symbol_multiplier_after_metadata_miss(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "scripts.multiplier_cache.refresh_via_opend",
        lambda **_kwargs: SimpleNamespace(ok=False, multiplier=None, error="not available in test"),
    )

    deal = normalize_trade_deal(
        {
            "deal_id": "deal-9",
            "futu_account_id": "REAL_HK_1",
            "code": "HK.POP260528P150000",
            "trd_side": "SELL_SHORT",
            "qty": 1,
            "price": 6.3,
        },
        futu_account_mapping={"REAL_HK_1": "lx"},
        repo_base=tmp_path,
        config={"intake": {"multiplier_by_symbol": {"9992.HK": 1000}}},
    )

    assert deal.symbol == "9992.HK"
    assert deal.multiplier == 1000
    assert deal.multiplier_source == "config:intake.multiplier_by_symbol"
    attempts = deal.normalization_diagnostics["multiplier_resolution"]["attempted_sources"]
    assert any(item["source"] == "config:intake.multiplier_by_symbol" and item["status"] == "resolved" for item in attempts)


def test_normalize_trade_deal_uses_configured_market_default_multiplier(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "scripts.multiplier_cache.refresh_via_opend",
        lambda **_kwargs: SimpleNamespace(ok=False, multiplier=None, error="not available in test"),
    )

    hk_deal = normalize_trade_deal(
        {
            "deal_id": "deal-10",
            "futu_account_id": "REAL_HK_1",
            "owner_stock_code": "HK.09992",
            "option_type": "PUT",
            "position_effect": "OPEN",
            "trade_side": "SELL",
            "qty": 1,
            "price": 6.3,
            "strike": 150,
            "expiry_date": "260528",
            "currency": "HKD",
        },
        futu_account_mapping={"REAL_HK_1": "lx"},
        repo_base=tmp_path,
        config={"intake": {"default_multiplier_hk": 1000}},
    )
    us_deal = normalize_trade_deal(
        {
            "deal_id": "deal-11",
            "futu_account_id": "REAL_US_1",
            "underlying_symbol": "US.NVDA",
            "option_type": "CALL",
            "position_effect": "OPEN",
            "trade_side": "SELL",
            "qty": 1,
            "price": 1.23,
            "strike": 100,
            "expiry_date": "260618",
            "currency_code": "USD",
        },
        futu_account_mapping={"REAL_US_1": "lx"},
        repo_base=tmp_path,
        config={"intake": {"default_multiplier_us": 100}},
    )

    assert hk_deal.multiplier == 1000
    assert hk_deal.multiplier_source == "config:intake.default_multiplier_hk"
    assert us_deal.multiplier == 100
    assert us_deal.multiplier_source == "config:intake.default_multiplier_us"
