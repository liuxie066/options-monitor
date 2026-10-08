from __future__ import annotations

import json
from copy import deepcopy

from cash_evidence_helpers import cash_portfolio

import pandas as pd
import pytest

from domain.domain.canonical_schema import normalize_processor_row
from domain.domain.daily_decision_brief import diff_daily_decision_briefs
from src.application.daily_decision_brief_renderer import render_full_brief
from src.application.ledger.api import open_position_ledger, resolve_position_data_config_path
from src.application.opening_candidate_snapshot import seal_opening_candidate_snapshot
from src.application.report_summaries import summarize_sell_put
from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash
from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates
from tests.test_daily_decision_brief_service import (
    _assemble,
    _earnings_evidence,
    _fixture_dependencies,
    _materialize_candidate_bundle_fixture,
    _write_labeled_put_candidates,
)
from tests.test_sell_put_strategy_risk import _account_nvda_context, _candidate


SIZING_KEYS = ("symbol_concentration_current", "symbol_concentration_after_existing_puts", "symbol_concentration_after")


def test_single_account_sizing_survives_real_decision_seal_and_brief(tmp_path) -> None:
    account_dir = _write_labeled_put_candidates(tmp_path)
    context = _account_nvda_context()
    (account_dir / "state" / "portfolio_context.json").write_text(json.dumps(context))
    open_position_ledger(resolve_position_data_config_path(base=tmp_path), runtime_root=tmp_path)
    row = _candidate(
        contract_symbol="NVDA260821P00100000",
        expiration="2026-08-21",
        cash_free_cny=800_000.0,
        cash_free_total_cny=800_000.0,
        **_earnings_evidence(),
    )
    decisions = []
    enriched = enrich_and_filter_sell_put_underwriting(
        df_labeled=pd.DataFrame([row]),
        symbol="NVDA",
        sell_put_cfg={"strategy": "insurance_underwriting"},
        portfolio_ctx=_account_nvda_context(),
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        decision_sink_fn=decisions.extend,
    )
    assert len(enriched) == 1
    expected = [round(50_000 / 850_000, 6), round(100_000 / 850_000, 6), 0.2]
    assert [enriched.iloc[0][key] for key in SIZING_KEYS] == expected
    assert [decisions[0]["normalized_input"][key] for key in SIZING_KEYS] == expected
    summary = normalize_processor_row(summarize_sell_put(enriched, "NVDA"))
    assert [summary[key] for key in SIZING_KEYS] == expected
    seal_opening_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        physical_account={
            "status": "available",
            "logical_account": "lx",
            "futu_account_id": "12345",
            "trd_env": "REAL",
            "market": "US",
            "source": "opend",
        },
        account_config_sha256="f" * 64,
        strategy_policy_sha256="1" * 64,
        dependencies=_fixture_dependencies(),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "put",
                "status": "completed",
                "reason": None,
                "quote_snapshot_id": None,
                "quote_receipt_relpath": None,
            }
        ],
        final_candidates={"put": json.loads(enriched.to_json(orient="records"))},
        candidate_evaluations={"put": decisions},
        run_mode={"scan_mode": "standard", "executable": True},
        sealed_at="2026-07-17T13:59:59Z",
    )
    _materialize_candidate_bundle_fixture(tmp_path)
    brief = _assemble(tmp_path)
    assert brief["actionability"] != "blocked", [action.get("metrics") for action in brief["actions"]]
    assert brief["account"] == "lx"
    candidate = brief["candidates"]["sell_put"][0]
    assert [candidate["metrics"][key] for key in SIZING_KEYS] == expected
    assert "当前 5.9% · 已有 Put 全指派 11.8% · 再卖 1 张后全指派 20.0%" in render_full_brief(brief)
    # Display-only changes must not create a new candidate notification.
    updated = deepcopy(brief)
    updated["revision"] += 1
    updated["candidates"]["sell_put"][0]["metrics"][SIZING_KEYS[0]] = 0.3
    for action in updated["actions"]:
        action["metrics"][SIZING_KEYS[0]] = 0.3
    assert diff_daily_decision_briefs(brief, updated)["material"] is False


@pytest.mark.parametrize("cash_required", [None, 0.0])
def test_existing_sizing_remains_available_without_new_contract_value(cash_required) -> None:
    row = _candidate(cash_required_cny=cash_required)
    enriched = enrich_and_filter_sell_put_underwriting(
        df_labeled=pd.DataFrame([row]),
        symbol="NVDA",
        sell_put_cfg={"strategy": "insurance_underwriting"},
        portfolio_ctx=_account_nvda_context(),
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
    )
    assert len(enriched) == 1
    assert enriched.iloc[0][SIZING_KEYS[0]] == round(50_000 / 850_000, 6)
    assert enriched.iloc[0][SIZING_KEYS[1]] == round(100_000 / 850_000, 6)
    assert enriched.iloc[0][SIZING_KEYS[2]] is None


def test_summary_uses_selected_contract_sizing_not_first_row() -> None:
    rows = [
        _candidate(contract_symbol="lower", annualized_net_return_on_cash_basis=0.12, symbol_concentration_after=0.23),
        _candidate(
            contract_symbol="higher",
            annualized_net_return_on_cash_basis=0.3,
            symbol_concentration_current=0.08,
            symbol_concentration_after_existing_puts=0.2,
            symbol_concentration_after=0.4,
            portfolio_risk_warnings="stock_value_estimated_from_avg_cost:NVDA",
        ),
    ]
    summary = summarize_sell_put(pd.DataFrame(rows), "NVDA")
    assert [summary[key] for key in SIZING_KEYS] == [0.08, 0.2, 0.4]
    assert summary["portfolio_risk_warnings"] == "stock_value_estimated_from_avg_cost:NVDA"
    empty = summarize_sell_put(pd.DataFrame(), "NVDA")
    assert all(empty[key] is None for key in SIZING_KEYS)


@pytest.mark.parametrize(
    "symbol,currency,existing_native,rate", [("NVDA", "USD", 7_000.0, 1 / 0.14), ("0700.HK", "HKD", 50_000.0, 0.9)]
)
@pytest.mark.parametrize("multiplier", [100, 500, 1000])
def test_sizing_reuses_cash_owner_fx_and_actual_multiplier(symbol, currency, existing_native, rate, multiplier) -> None:
    context = cash_portfolio(
        {
            "cash_by_currency": {"CNY": 800_000.0},
            "stocks_by_symbol": {symbol: {"symbol": symbol, "shares": 100, "avg_cost": 500.0, "currency": "CNY"}},
            "option_ctx": {
                "decision_snapshot_status": "trusted",
                "cash_secured_by_symbol_by_ccy": {symbol: {currency: existing_native}},
                "cash_secured_total_by_ccy": {currency: existing_native},
            },
        }
    )
    converter = CurrencyConverter(ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.9))
    frame = enrich_sell_put_candidates_with_cash(
        df_labeled=pd.DataFrame(
            [
                _candidate(
                    symbol=symbol,
                    multiplier=multiplier,
                    currency=currency,
                    net_income_cny=None,
                    option_contract_point_value_cny=None,
                    cash_required_cny=None,
                )
            ]
        ),
        symbol=symbol,
        portfolio_ctx=context,
        exchange_rate_converter=converter,
    )
    assert frame.iloc[0]["cash_required_cny"] == pytest.approx(100 * multiplier * rate)
    enriched = enrich_and_filter_sell_put_underwriting(
        df_labeled=frame,
        symbol=symbol,
        sell_put_cfg={"strategy": "insurance_underwriting"},
        portfolio_ctx=context,
        exchange_rate_converter=converter,
    )
    assert len(enriched) == 1
    row = enriched.iloc[0]
    assert row[SIZING_KEYS[0]] == round(50_000 / 850_000, 6)
    assert row[SIZING_KEYS[1]] == round((50_000 + existing_native * rate) / 850_000, 6)
    assert row[SIZING_KEYS[2]] == round((50_000 + existing_native * rate + 100 * multiplier * rate) / 850_000, 6)
    assert row["max_new_contracts"] == frame.iloc[0]["max_new_contracts"]
    summary = normalize_processor_row(summarize_sell_put(enriched, symbol))
    assert summary["portfolio_risk_warnings"] == f"stock_value_estimated_from_avg_cost:{symbol}"
