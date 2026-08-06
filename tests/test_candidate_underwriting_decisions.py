from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

from domain.domain.engine import (
    REPLACEMENT_CAPACITY_DEFERRED,
    REPLACEMENT_REJECTED_INVARIANT,
    REJECT_RISK_INSURANCE_UNDERWRITING,
)
from domain.domain.insurance_underwriting import InsuranceUnderwritingConfig
from src.application.candidate_underwriting_decisions import (
    apply_insurance_underwriting_to_all_decisions,
)
from src.application.scan_sell_put import run_sell_put_scan
from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates


def _write_put_required_data(
    root: Path,
    *,
    strike: float = 100,
    spot: float = 110,
    implied_volatility: float = 0.30,
    realized_volatility_estimate: float = 0.20,
) -> None:
    parsed = root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "market": "US",
                "quote_update_time": "2026-04-01 10:59:00",
                "quote_observed_at_utc": "2026-04-01T14:59:00Z",
                "quote_age_seconds": 60,
                "spot_update_time": "2026-04-01 10:59:00",
                "spot_observed_at_utc": "2026-04-01T14:59:00Z",
                "spot_age_seconds": 60,
                "market_state": "MORNING",
                "underlier_observation_status": "ready",
                "underlier_observation_reason_code": None,
                "option_type": "put",
                "expiration": "2026-09-18",
                "contract_symbol": "NVDA-20260918-PUT-100",
                "currency": "USD",
                "dte": 45,
                "strike": strike,
                "spot": spot,
                "bid": 1.9,
                "ask": 2.1,
                "mid": 2.0,
                "price_tick": 0.01,
                "open_interest": 500,
                "volume": 50,
                "implied_volatility": implied_volatility,
                "realized_volatility_estimate": realized_volatility_estimate,
                "term_matched_rv": realized_volatility_estimate,
                "term_matched_rv_status": "ready",
                "term_matched_rv_reason": None,
                "term_matched_rv_input_hash": "b" * 64,
                "option_standard_type": "STANDARD",
                "stock_owner": "US.NVDA",
                "stock_type": "DRVT",
                "option_sec_status": "NORMAL",
                "option_suspension": False,
                "chain_multiplier": 100,
                "snapshot_multiplier": 100,
                "multiplier": 100,
                "opening_contract_status": "ready",
                "opening_contract_reason_codes": "",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)


def _capture_put_decisions(
    tmp_path: Path,
    *,
    put_cash_free: float,
) -> list[dict[str, object]]:
    captured: list[dict[str, object]] = []
    run_sell_put_scan(
        symbols=["NVDA"],
        input_root=tmp_path / "input",
        output=tmp_path / "candidates.csv",
        min_dte=7,
        max_dte=60,
        min_annualized_net_return=0,
        min_net_income=0,
        min_open_interest=0,
        min_volume=0,
        max_spread_ratio=1,
        event_risk_cfg={"enabled": False, "mode": "warn"},
        risk_policy_version="candidate-risk.v1",
        quote_snapshot_id="a" * 64,
        all_decisions_sink_fn=captured.extend,
        put_cash_capacity_fn=lambda _contract: {
            "put_cash_required": 10_000,
            "put_cash_free": put_cash_free,
        },
        quote_freshness_now_utc=datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc),
        quiet=True,
    )
    return captured


def _converter() -> CurrencyConverter:
    return CurrencyConverter(
        ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)
    )


def test_underwriting_reject_is_bound_to_opening_and_replacement_sidecar(
    tmp_path: Path,
) -> None:
    _write_put_required_data(
        tmp_path / "input",
        implied_volatility=0.30,
        realized_volatility_estimate=0.40,
    )
    generic = _capture_put_decisions(tmp_path, put_cash_free=20_000)

    transformed = apply_insurance_underwriting_to_all_decisions(
        generic,
        mode="put",
        cfg=InsuranceUnderwritingConfig(
            min_annualized_return=0,
            min_net_income=0,
            min_iv_rv_ratio=1.10,
            min_iv_minus_rv=0.05,
            reject_event_risk=False,
            event_source_fail_closed=False,
        ),
        exchange_rate_converter=_converter(),
    )

    decision = transformed[0]
    assert decision["opening_decision"]["accepted"] is False
    assert decision["invariant_decision"]["accepted"] is False
    assert {
        item["reason"]
        for item in decision["invariant_decision"]["rejects"]
    } == {REJECT_RISK_INSURANCE_UNDERWRITING}
    assert (
        decision["replacement_candidate_decision"]["replacement_eligibility"]
        == REPLACEMENT_REJECTED_INVARIANT
    )
    assert decision["normalized_input"]["insurance_underwriting_rule"] == (
        "risk_iv_rv_ratio"
    )
    assert (
        decision["opening_decision"]["risk_policy_hash"]
        == decision["invariant_decision"]["risk_policy_hash"]
        == decision["risk_policy_hash"]
    )


def test_capacity_only_candidate_remains_deferred_after_underwriting(
    tmp_path: Path,
) -> None:
    _write_put_required_data(tmp_path / "input")
    generic = _capture_put_decisions(tmp_path, put_cash_free=0)

    transformed = apply_insurance_underwriting_to_all_decisions(
        generic,
        mode="put",
        cfg=InsuranceUnderwritingConfig(
            min_annualized_return=0,
            min_net_income=0,
            min_iv_rv_ratio=1.10,
            min_iv_minus_rv=0.05,
            reject_event_risk=False,
            event_source_fail_closed=False,
        ),
        exchange_rate_converter=_converter(),
    )

    decision = transformed[0]
    assert decision["invariant_decision"]["accepted"] is True
    assert (
        decision["replacement_candidate_decision"]["replacement_eligibility"]
        == REPLACEMENT_CAPACITY_DEFERRED
    )


def test_real_sell_put_scanner_accepts_atm_contract(tmp_path: Path) -> None:
    _write_put_required_data(
        tmp_path / "input",
        strike=110,
        spot=110,
    )

    out = run_sell_put_scan(
        symbols=["NVDA"],
        input_root=tmp_path / "input",
        output=tmp_path / "atm.csv",
        min_dte=7,
        max_dte=60,
        min_annualized_net_return=0,
        min_net_income=0,
        min_open_interest=0,
        min_volume=0,
        max_spread_ratio=1,
        event_risk_cfg={"enabled": False, "mode": "warn"},
        quote_freshness_now_utc=datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc),
        quiet=True,
    )

    assert len(out) == 1
    assert out.iloc[0]["strike"] == out.iloc[0]["spot"] == 110
