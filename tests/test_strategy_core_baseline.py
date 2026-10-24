"""Frozen 1fd7432e oracle; generated before extraction, never from the new core."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from domain.domain.engine import CandidateCalculationError, calculate_opening_candidate_metrics
from domain.domain.insurance_underwriting import InsuranceUnderwritingConfig, evaluate_underwriting_candidate
from src.application.short_vol_risk_context import enrich_short_vol_contract_cny_fields
from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

GOLDEN = json.loads((Path(__file__).parent / "fixtures/strategy_core_baseline.json").read_text())
RATES = ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.920000046)


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda case: case["name"])
def test_original_metric_and_policy_contract(case):
    raw = case["row"]
    kwargs = {"mode": case["mode"], "avg_cost": case["avg_cost"],
              "now_utc": datetime.fromisoformat(case["now_utc"])}
    if "error" in case:
        with pytest.raises(CandidateCalculationError) as exc:
            calculate_opening_candidate_metrics(raw, **kwargs)
        assert exc.value.to_payload() == case["error"]
        return
    metrics = calculate_opening_candidate_metrics(raw, **kwargs)
    assert metrics == case["metrics"]
    fields = enrich_short_vol_contract_cny_fields(
        {**raw, **metrics}, exchange_rate_converter=CurrencyConverter(RATES),
    )
    assert fields == case["cny_fields"]
    # Frozen pre-delta oracle: retain the historical policy explicitly.
    expected = deepcopy(case["decision"])
    expected["fields"]["policy_max_abs_delta"] = None
    expected["opening_decision"]["normalized_input"]["policy_max_abs_delta"] = None
    assert evaluate_underwriting_candidate(
        case["policy_row"], mode=case["mode"], cfg=InsuranceUnderwritingConfig(max_abs_delta=None),
    ) == expected


@pytest.mark.parametrize("case", GOLDEN["fx"])
def test_fx_operation_order_is_bit_exact(case):
    value = CurrencyConverter(RATES).convert(
        case["amount"], from_ccy=case["from"], to_ccy=case["to"],
    )
    assert value.hex() == float(case["result"]).hex()


@pytest.mark.parametrize("existing", [None, float("nan"), 0.0, 123.123456789])
def test_cny_supplement_preserves_existing_values(existing):
    row = {"symbol": "NVDA", "currency": "USD", "net_income": 1.123456789,
           "multiplier": 100, "net_income_cny": existing}
    result = enrich_short_vol_contract_cny_fields(row, exchange_rate_converter=CurrencyConverter(RATES))
    if existing is None or existing != existing:
        assert result["net_income_cny"].hex() == (1.123456789 / 0.14).hex()
    else:
        assert "net_income_cny" not in result
    assert row["net_income_cny"] is existing


def test_scan_adapters_keep_metric_failure_boundary():
    from src.application.scan_sell_put import compute_metrics as put_metrics
    from src.application.scan_sell_call import compute_metrics as call_metrics
    from src.application.candidate_models import CandidateContractInput
    for case in GOLDEN["cases"]:
        contract = CandidateContractInput.from_row(case["row"], mode=case["mode"])
        kwargs = {"now_utc": datetime.fromisoformat(case["now_utc"])}
        result = (put_metrics(contract, **kwargs) if case["mode"] == "put"
                  else call_metrics(contract, case["avg_cost"], **kwargs))
        if "error" in case:
            assert result is None
        else:
            assert {k: result[k] for k in case["metrics"]} == case["metrics"]
