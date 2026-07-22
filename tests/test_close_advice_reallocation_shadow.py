from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.application.close_advice_reallocation_shadow import _evaluate_row, write_close_advice_reallocation_shadow


def _write_context(path: Path, position: dict[str, object], *, rates: dict[str, object] | None = None) -> None:
    path.write_text(
        json.dumps({"open_positions_min": [position], "exchange_rates": rates or {}}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_put_reallocation_shadow_uses_released_cny_capacity_and_keeps_inputs_unchanged(tmp_path: Path) -> None:
    close_path = tmp_path / "close_advice.csv"
    capacity_path = tmp_path / "portfolio_capacity_shadow.csv"
    context_path = tmp_path / "option_positions_context.json"
    pd.DataFrame(
        [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-19",
                "strike": 100,
                "currency": "USD",
                "strategy_family": "sell_put",
                "strategy_profile": "insurance_underwriting",
                "evaluation_status": "priced",
                "close_action": "hold",
                "tier": "none",
                "remaining_annualized_return": 0.04,
                "remaining_premium": 20,
                "close_fee": 1,
                "dte": 30,
            }
        ]
    ).to_csv(close_path, index=False)
    pd.DataFrame(
        [
            {
                "account": "lx",
                "symbol": "PDD",
                "option_type": "put",
                "contract_symbol": "PDD260619P90000",
                "expiration": "2026-06-19",
                "strike": 90,
                "currency": "USD",
                "strategy_family": "sell_put",
                "strategy_profile": "insurance_underwriting",
                "allocation_rank": 1,
                "capacity_before": 0,
                "capacity_required": 60_000,
                "dte": 30,
                "mid": 2.0,
                "bid": 1.95,
                "multiplier": 100,
                "gross_income": 200,
                "futu_fee": 2,
                "net_income": 198,
                "annualized_net_return_on_cash_basis": 0.12,
            }
        ]
    ).to_csv(capacity_path, index=False)
    _write_context(
        context_path,
        {
            "account": "lx",
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": 1781827200000,
            "expiration_ymd": "2026-06-19",
            "strike": 100,
            "contracts": 1,
            "contracts_open": 1,
            "cash_secured_amount": 10_000,
            "currency": "USD",
        },
        rates={"rates": {"USDCNY": 7.0}},
    )
    close_before = close_path.read_bytes()
    capacity_before = capacity_path.read_bytes()

    result = write_close_advice_reallocation_shadow(
        report_dir=tmp_path,
        context_path=context_path,
        account="lx",
    )

    assert result["shadow_only"] is True
    assert close_path.read_bytes() == close_before
    assert capacity_path.read_bytes() == capacity_before
    row = pd.read_csv(tmp_path / "close_advice_reallocation_shadow.csv").iloc[0]
    assert row["reallocation_status"] == "review_switch"
    assert row["replacement_symbol"] == "PDD"
    assert row["released_capacity"] == 70_000
    assert row["replacement_capacity_required"] == 60_000
    assert row["replacement_open_fee"] == 2
    assert row["replacement_entry_credit"] == 200
    assert row["replacement_contracts"] == 1
    assert row["replacement_multiplier"] == 100
    assert row["replacement_currency"] == "USD"
    assert row["replacement_fee_calc_status"] == "candidate_futu_fee"
    assert row["replacement_spread_slippage"] == pytest.approx(5)
    assert row["recovery_days"] < row["recovery_horizon_days"]


def test_call_reallocation_shadow_requires_same_symbol_and_released_shares(tmp_path: Path) -> None:
    pd.DataFrame(
        [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "call",
                "expiration": "2026-06-19",
                "strike": 140,
                "currency": "USD",
                "strategy_family": "sell_call",
                "strategy_profile": "insurance_underwriting",
                "evaluation_status": "priced",
                "close_action": "hold",
                "tier": "none",
                "remaining_annualized_return": 0.03,
                "remaining_premium": 20,
                "close_fee": 1,
                "dte": 30,
            }
        ]
    ).to_csv(tmp_path / "close_advice.csv", index=False)
    pd.DataFrame(
        [
            {
                "account": "lx",
                "symbol": "PDD",
                "option_type": "call",
                "expiration": "2026-07-17",
                "strike": 120,
                "currency": "USD",
                "strategy_family": "covered_call",
                "strategy_profile": "insurance_underwriting",
                "allocation_rank": 1,
                "capacity_before": 0,
                "capacity_required": 100,
            },
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "call",
                "contract_symbol": "NVDA260717C150000",
                "expiration": "2026-07-17",
                "strike": 150,
                "currency": "USD",
                "strategy_family": "covered_call",
                "strategy_profile": "insurance_underwriting",
                "allocation_rank": 2,
                "capacity_before": 0,
                "capacity_required": 100,
                "dte": 45,
                "mid": 2.0,
                "bid": 1.95,
                "multiplier": 100,
                "gross_income": 200,
                "futu_fee": 2,
                "net_income": 198,
                "annualized_net_premium_return": 0.10,
            },
        ]
    ).to_csv(tmp_path / "portfolio_capacity_shadow.csv", index=False)
    context_path = tmp_path / "option_positions_context.json"
    _write_context(
        context_path,
        {
            "account": "lx",
            "symbol": "NVDA",
            "option_type": "call",
            "expiration": "2026-06-19",
            "strike": 140,
            "contracts": 1,
            "contracts_open": 1,
            "underlying_share_locked": 100,
            "multiplier": 100,
            "currency": "USD",
        },
    )

    write_close_advice_reallocation_shadow(report_dir=tmp_path, context_path=context_path, account="lx")

    row = pd.read_csv(tmp_path / "close_advice_reallocation_shadow.csv").iloc[0]
    assert row["reallocation_status"] == "review_switch"
    assert row["replacement_symbol"] == "NVDA"
    assert row["replacement_rank"] == 2
    assert row["released_capacity"] == 100
    assert row["capacity_unit"] == "shares"


def test_reallocation_shadow_uses_lot_id_before_contract_fallback() -> None:
    close_row = {
        "account": "lx",
        "position_lot_id": "lot-b",
        "symbol": "NVDA",
        "option_type": "call",
        "expiration": "2026-06-19",
        "strike": 140,
        "currency": "USD",
        "strategy_family": "sell_call",
        "strategy_profile": "insurance_underwriting",
        "evaluation_status": "priced",
        "close_action": "hold",
        "tier": "none",
    }
    position = {
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "call",
        "expiration": "2026-06-19",
        "strike": 140,
        "contracts": 1,
        "contracts_open": 1,
        "underlying_share_locked": 100,
        "multiplier": 100,
        "currency": "USD",
    }

    exact = _evaluate_row(
        close_row,
        candidates=[],
        positions=[{**position, "record_id": "lot-a"}, {**position, "record_id": "lot-b"}],
        context={},
        account="lx",
        capacity_evidence_available=True,
    )
    legacy = _evaluate_row(
        {key: value for key, value in close_row.items() if key != "position_lot_id"},
        candidates=[],
        positions=[{**position, "record_id": "lot-a"}],
        context={},
        account="lx",
        capacity_evidence_available=True,
    )

    assert exact["position_lot_id"] == "lot-b"
    assert exact["reallocation_status"] == "no_feasible_replacement"
    assert exact["released_capacity"] == 100
    assert legacy["reallocation_status"] == "no_feasible_replacement"
    assert legacy["released_capacity"] == 100


def test_reallocation_shadow_fails_closed_without_put_fx_or_for_combo_leg(tmp_path: Path) -> None:
    close_rows = [
        {
            "account": "lx",
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-06-19",
            "strike": 100,
            "currency": "USD",
            "strategy_family": "sell_put",
            "strategy_profile": "insurance_underwriting",
            "evaluation_status": "priced",
            "close_action": "close",
        },
        {
            "account": "lx",
            "symbol": "PDD",
            "option_type": "put",
            "expiration": "2026-06-19",
            "strike": 90,
            "currency": "USD",
            "strategy_family": "sell_put",
            "strategy_profile": "return_first",
            "strategy_group_id": "combo-1",
            "evaluation_status": "priced",
            "close_action": "close_put_keep_call",
        },
    ]
    pd.DataFrame(close_rows).to_csv(tmp_path / "close_advice.csv", index=False)
    pd.DataFrame(columns=["account", "symbol"]).to_csv(tmp_path / "portfolio_capacity_shadow.csv", index=False)
    context_path = tmp_path / "option_positions_context.json"
    context_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "NVDA",
                        "option_type": "put",
                        "expiration": "2026-06-19",
                        "strike": 100,
                        "contracts": 1,
                        "contracts_open": 1,
                        "cash_secured_amount": 10_000,
                        "currency": "USD",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    write_close_advice_reallocation_shadow(report_dir=tmp_path, context_path=context_path, account="lx")

    rows = pd.read_csv(tmp_path / "close_advice_reallocation_shadow.csv").to_dict("records")
    assert rows[0]["reallocation_status"] == "not_evaluable"
    assert rows[0]["reallocation_reason"] == "released_capacity_not_evaluable"
    assert rows[1]["reallocation_status"] == "not_evaluable"
    assert rows[1]["reallocation_reason"] == "combo_yield_single_leg_reallocation_unsupported"


def test_reallocation_shadow_holds_when_replacement_has_no_finite_cost_recovery() -> None:
    result = _evaluate_row(
        {
            "account": "lx",
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-06-19",
            "strike": 100,
            "currency": "CNY",
            "strategy_family": "sell_put",
            "strategy_profile": "insurance_underwriting",
            "evaluation_status": "priced",
            "close_action": "hold",
            "remaining_annualized_return": 0.12,
            "remaining_premium": 300,
            "close_fee": 1,
            "dte": 30,
        },
        candidates=[
            {
                "account": "lx",
                "symbol": "PDD",
                "option_type": "put",
                "expiration": "2026-07-17",
                "strike": 90,
                "currency": "CNY",
                "strategy_family": "sell_put",
                "strategy_profile": "insurance_underwriting",
                "capacity_before": 0,
                "capacity_required": 5_000,
                "dte": 30,
                "mid": 0.1,
                "bid": 0.1,
                "multiplier": 100,
                "gross_income": 10,
                "futu_fee": 1,
                "net_income": 9,
                "annualized_net_return_on_cash_basis": 0.04,
            }
        ],
        positions=[
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-19",
                "strike": 100,
                "contracts": 1,
                "contracts_open": 1,
                "cash_secured_amount": 10_000,
                "currency": "CNY",
            }
        ],
        context={},
        account="lx",
        capacity_evidence_available=True,
    )

    assert result["daily_yield_advantage"] < 0
    assert result["recovery_days"] is None
    assert result["reallocation_status"] == "hold_more_efficient"
    assert result["reallocation_reason"] == "switch_cost_not_recovered_within_horizon"
