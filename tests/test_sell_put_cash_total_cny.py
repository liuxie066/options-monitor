from __future__ import annotations

from pathlib import Path

import pandas as pd

from domain.domain.engine import (
    EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
    EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
)


def test_sell_put_opening_capacity_inputs_require_physical_futu_authority() -> None:
    from src.application.sell_put_cash import sell_put_opening_capacity_inputs
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    common = {
        "symbol": "NVDA",
        "strike": 100.0,
        "multiplier": 100,
        "currency": "USD",
        "exchange_rate_converter": CurrencyConverter(ExchangeRates()),
    }
    unavailable = sell_put_opening_capacity_inputs(
        **common,
        portfolio_ctx={
            "portfolio_source_name": "futu",
            "capacity_authority": {"status": "unavailable"},
            "cash_by_currency": {"USD": 20_000.0},
            "option_ctx": {"cash_secured_total_by_ccy": {}},
        },
    )
    available = sell_put_opening_capacity_inputs(
        **common,
        portfolio_ctx={
            "portfolio_source_name": "futu",
            "capacity_authority": {"status": "available"},
            "cash_by_currency": {"USD": 20_000.0},
            "option_ctx": {
                "cash_secured_total_by_ccy": {},
                "cash_secured_by_symbol_by_ccy": {},
            },
        },
    )
    ledger_unavailable = sell_put_opening_capacity_inputs(
        **common,
        portfolio_ctx={
            "portfolio_source_name": "futu",
            "capacity_authority": {"status": "available"},
            "cash_by_currency": {"USD": 20_000.0},
            "option_ctx": {
                "context_status": "unavailable",
                "cash_secured_total_by_ccy": {},
                "cash_secured_by_symbol_by_ccy": {},
            },
        },
    )

    assert unavailable["put_cash_capacity_available"] is False
    assert unavailable["put_cash_capacity_reason"] == (
        "physical_account_capacity_authority_unavailable"
    )
    assert available == {
        "put_cash_required": 10_000.0,
        "put_cash_free": 20_000.0,
        "put_cash_capacity_available": True,
        "put_cash_capacity_reason": "cash_supported_by_same_currency",
    }
    assert ledger_unavailable["put_cash_capacity_available"] is False
    assert ledger_unavailable["put_cash_capacity_reason"] == (
        "option_positions_cash_secured_context_unavailable"
    )


def test_enrich_sell_put_candidates_fails_closed_when_option_context_is_missing() -> None:
    from domain.domain.engine import evaluate_opening_candidate_policy
    from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    candidate = {
        "symbol": "NVDA",
        "contract_symbol": "NVDA260918P00090000",
        "expiration": "2026-09-18",
        "strike": 90.0,
        "spot": 100.0,
        "multiplier": 100,
        "currency": "USD",
        "dte": 30,
        "policy_min_dte": 7,
        "policy_max_dte": 60,
        "policy_max_strike": 100.0,
        "annualized_net_return_on_cash_basis": 0.15,
        "net_income_cny": 100.0,
        "spread_ratio": 0.10,
        "iv_rv_ratio": 1.20,
        "iv_minus_rv": 0.10,
        "earnings_evidence_status": "ready",
        "earnings_reason_code": None,
        "earnings_policy_version": EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
        "earnings_window_days": EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
        "earnings_market_date": "2026-08-06",
        "earnings_hard_window_start": "2026-09-12",
        "earnings_hard_window_end": "2026-09-18",
        "earnings_hard_coverage_status": "complete",
        "earnings_soft_coverage_status": "complete",
        "earnings_has_event": False,
        "earnings_blocking_has_event": False,
        "earnings_events": [],
        "earnings_blocking_events": [],
        "earnings_nonblocking_events": [],
    }
    enriched = enrich_sell_put_candidates_with_cash(
        df_labeled=pd.DataFrame([candidate]),
        symbol="NVDA",
        portfolio_ctx={
            "cash_by_currency": {"USD": 20_000.0},
            "capacity_authority": {
                "status": "available",
                "futu_account_id": "12345",
                "trd_env": "REAL",
                "market": "US",
            },
            "option_ctx": {
                "locked_shares_status": "unavailable",
                "locked_shares_unavailable_reason": (
                    "option_positions_context_unavailable"
                ),
                "locked_shares_by_symbol": {},
                "locked_shares_unavailable_by_symbol": {},
            },
        },
        exchange_rate_converter=CurrencyConverter(
            ExchangeRates(usd_per_cny=1 / 7.2, cny_per_hkd=0.92)
        ),
    )

    row = enriched.iloc[0]
    decision = evaluate_opening_candidate_policy(row.to_dict(), mode="put")
    assert row["max_new_contracts"] == 0
    assert row["cash_secured_unavailable_reason"] == (
        "option_positions_cash_secured_context_unavailable"
    )
    assert decision["accepted"] is False
    assert decision["rejects"][0]["reason"] == "hard_capacity_put"


def test_enrich_sell_put_candidates_with_cash_adds_total_cny_columns(tmp_path: Path) -> None:
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates
    from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash

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
    )

    row = result.iloc[0]
    assert row["cash_available_total_cny"] == 10920.0
    assert row["cash_free_total_cny"] == 10460.0
    assert row["cash_available_effective_native"] == 10920.0 / 0.92
    assert row["cash_free_effective_native"] == 10460.0 / 0.92
    assert row["cash_capacity_basis"] == "same_currency_then_fx:HKD"
    assert row["max_new_contracts"] == 0
    assert row["cash_pool_additive_across_candidates"] == False  # noqa: E712
    assert row["cash_available_cny"] == 10000.0
    assert row["cash_free_cny"] == 9540.0


def test_enrich_sell_put_candidates_with_cash_marks_unknown_cash_secured_fail_closed(tmp_path: Path) -> None:
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates
    from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash

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
    result = enrich_sell_put_candidates_with_cash(
        df_labeled=df,
        symbol="0700.HK",
        portfolio_ctx={
            "cash_by_currency": {"CNY": 10000.0, "HKD": 1000.0},
            "option_ctx": {
                "cash_secured_unavailable_by_symbol": {
                    "0700.HK": "short_put_cash_secured_basis_missing",
                },
            },
        },
        exchange_rate_converter=CurrencyConverter(ExchangeRates(cny_per_hkd=0.92)),
    )

    row = result.iloc[0]
    assert row["cash_secured_unavailable_reason"] == "0700.HK:short_put_cash_secured_basis_missing"
    assert pd.isna(row["cash_free_cny"])
    assert pd.isna(row["cash_free_total_cny"])
    assert pd.isna(row["cash_free_usd"])


def test_enrich_sell_put_candidates_with_cash_does_not_guess_missing_candidate_currency(tmp_path: Path) -> None:
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates
    from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash

    df = pd.DataFrame(
        [
            {
                "symbol": "0700.HK",
                "strike": 450.0,
                "multiplier": 500,
            }
        ]
    )
    result = enrich_sell_put_candidates_with_cash(
        df_labeled=df,
        symbol="0700.HK",
        portfolio_ctx={
            "cash_by_currency": {"CNY": 10000.0, "USD": 10000.0},
            "option_ctx": {
                "cash_secured_total_by_ccy": {},
                "cash_secured_total_cny": 0.0,
                "cash_secured_by_symbol_by_ccy": {},
            },
        },
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)),
    )

    row = result.iloc[0]
    assert row["cash_requirement_unavailable_reason"] == "sell_put_candidate_currency_missing"
    assert pd.isna(row["cash_required_usd"])
    assert pd.isna(row["cash_required_cny"])


def test_enrich_sell_put_candidates_with_cash_does_not_treat_hkd_requirement_as_usd(tmp_path: Path) -> None:
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates
    from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash

    df = pd.DataFrame(
        [
            {
                "symbol": "0700.HK",
                "strike": 450.0,
                "multiplier": 500,
                "currency": "HKD",
            }
        ]
    )
    result = enrich_sell_put_candidates_with_cash(
        df_labeled=df,
        symbol="0700.HK",
        portfolio_ctx={
            "cash_by_currency": {"USD": 1_000_000.0},
            "option_ctx": {
                "cash_secured_total_by_ccy": {},
                "cash_secured_total_cny": 0.0,
                "cash_secured_by_symbol_by_ccy": {},
            },
        },
        exchange_rate_converter=CurrencyConverter(ExchangeRates()),
    )

    row = result.iloc[0]
    assert pd.isna(row["cash_required_usd"])
    assert pd.isna(row["cash_required_cny"])
    assert row["cash_requirement_unavailable_reason"] == "cross_currency_cash_fx_unavailable:USD"


def test_enrich_sell_put_candidates_keeps_native_cash_when_fx_is_unavailable(tmp_path: Path) -> None:
    from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    result = enrich_sell_put_candidates_with_cash(
        df_labeled=pd.DataFrame(
            [{"symbol": "NVDA", "strike": 100.0, "multiplier": 100, "currency": "USD"}]
        ),
        symbol="NVDA",
        portfolio_ctx={
            "cash_by_currency": {"USD": 20_000.0, "CNY": 100_000.0},
            "option_ctx": {
                "cash_secured_total_by_ccy": {"USD": 2_000.0},
                "cash_secured_by_symbol_by_ccy": {"NVDA": {"USD": 2_000.0}},
            },
        },
        exchange_rate_converter=CurrencyConverter(ExchangeRates()),
    )

    row = result.iloc[0]
    assert row["cash_free_effective_native"] == 18_000.0
    assert row["cash_fx_status"] == "known_cash_only_cross_currency_fx_unavailable:CNY"
    assert pd.isna(row["cash_requirement_unavailable_reason"])


def test_enrich_sell_put_candidates_marks_expired_fx_without_blocking_native_cash(tmp_path: Path) -> None:
    from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    result = enrich_sell_put_candidates_with_cash(
        df_labeled=pd.DataFrame(
            [{"symbol": "NVDA", "strike": 100.0, "multiplier": 100, "currency": "USD"}]
        ),
        symbol="NVDA",
        portfolio_ctx={
            "cash_by_currency": {"USD": 20_000.0, "CNY": 100_000.0},
            "option_ctx": {
                "cash_secured_total_by_ccy": {"USD": 2_000.0},
                "cash_secured_by_symbol_by_ccy": {"NVDA": {"USD": 2_000.0}},
            },
            "_sell_put_fx_status": "unavailable_stale",
        },
        exchange_rate_converter=CurrencyConverter(ExchangeRates()),
    )

    row = result.iloc[0]
    assert row["cash_free_effective_native"] == 18_000.0
    assert row["cash_fx_status"] == "known_cash_only_cross_currency_fx_stale:CNY"
    assert pd.isna(row["cash_requirement_unavailable_reason"])
