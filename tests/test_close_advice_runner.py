from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from domain.domain.expiration_dates import expiration_business_today
from src.application.close_advice_runner import run_close_advice
from src.application.multi_tick.misc import AccountResult
from src.application.multi_tick.notify_format import build_account_message


def test_close_advice_input_uses_shared_account_and_currency_normalization() -> None:
    from src.application.close_advice_runner import _money, _position_to_input

    input_row, _flags = _position_to_input(
        {
            "account": " LX ",
            "symbol": "HK.00700",
            "option_type": "认沽",
            "side": "short",
            "expiration": "2026-06-18",
            "strike": 100,
            "contracts_open": 1,
            "premium": 1.0,
            "multiplier": 100,
            "currency": "港币",
        },
        {"bid": 0.5, "ask": 0.6},
    )

    assert input_row.account == "lx"
    assert input_row.currency == "HKD"
    assert _money(12.3, "港币") == "HK$12.30"


def _freeze_close_advice_business_today(monkeypatch: pytest.MonkeyPatch, ymd: str = "2026-04-16") -> None:
    frozen = datetime.fromisoformat(ymd).date()
    monkeypatch.setattr("src.application.close_advice_runner.expiration_business_today", lambda: frozen)


def test_run_close_advice_builds_csv_and_markdown_from_local_fixtures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.6,
                "expiration": "2026-05-15",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-05-15",
                "strike": 100,
                "mid": 0.22,
                "bid": 0.21,
                "ask": 0.23,
                "dte": 29,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={
            "close_advice": {
                "enabled": True,
                "notify_levels": ["strong", "medium"],
                "max_items_per_account": 5,
            }
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    assert result["enabled"] is True
    assert result["rows"] == 1
    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    assert "平仓建议" in text
    assert "NVDA Put 2026-05-15" in text
    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    assert rows[0]["strategy_exit_mode"] == "standard_short_option"


def test_run_close_advice_enables_optimizer_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.0,
                "expiration": "2026-06-15",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-15",
                "strike": 100,
                "mid": 0.20,
                "bid": 0.19,
                "ask": 0.21,
                "dte": 60,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
                "delta": 0.60,
                "otm_pct": 0.10,
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={"close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    assert result["tier_counts"].get("strong") == 1
    csv_text = (out_dir / "close_advice.csv").read_text(encoding="utf-8")
    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    assert ",defer," in csv_text
    assert "缺少 delta 数据，无法运行 optimizer" in csv_text
    assert "强烈建议平仓" in text

    assert "optimizer_tier" in csv_text
    assert "optimizer_reason" in csv_text


def test_run_close_advice_optimizer_switch_includes_redeploy_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.0,
                "expiration": "2026-06-15",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-15",
                "strike": 100,
                "mid": 0.20,
                "bid": 0.19,
                "ask": 0.21,
                "dte": 60,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
                "delta": -0.60,
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    out_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "AMD",
                "option_type": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "strike": 80,
                "annualized_net_return_on_cash_basis": 0.20,
            }
        ]
    ).to_csv(out_dir / "amd_sell_put_candidates.csv", index=False)

    def _fake_optimizer(inp, optimizer_cfg, *, alternative_annualized_return=None):
        assert alternative_annualized_return == 0.20
        return {
            "optimizer_tier": "optimizer_switch",
            "optimizer_reason": "替代候选收益明显更高",
            "effective_annualized_return": 0.02,
            "tail_risk_score": 0.05,
            "risk_adjusted_return": 0.03,
            "switch_value_ratio": 9.0,
            "alternative_annualized_return": alternative_annualized_return,
            "delta": -0.60,
            "otm_pct": 0.10,
        }

    monkeypatch.setattr("src.application.close_advice_runner.evaluate_close_optimizer", _fake_optimizer)

    run_close_advice(
        config={"close_advice": {"enabled": True, "notify_levels": ["optimizer_switch"]}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    assert rows[0]["tier"] == "optimizer_switch"
    assert rows[0]["optimizer_tier"] == "optimizer_switch"
    assert rows[0]["alternative_symbol"] == "AMD"
    assert rows[0]["alternative_contract_symbol"] == "AMD260619P00080000"
    assert rows[0]["alternative_annualized_return"] == 0.20
    assert str(rows[0]["alternative_source_path"]).endswith("amd_sell_put_candidates.csv")
    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    assert "AMD260619P00080000" in text


def test_run_close_advice_uses_underwriting_config_for_short_vol_close_thesis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.0,
                "expiration": "2026-06-15",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-15",
                "strike": 100,
                "mid": 0.20,
                "bid": 0.19,
                "ask": 0.21,
                "dte": 60,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
                "delta": -0.20,
                "implied_volatility": 0.20,
                "realized_volatility_estimate": 0.20,
                "event_source_status": "ok",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    run_close_advice(
        config={
            "close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]},
            "symbols": [
                {
                    "symbol": "NVDA",
                    "sell_put": {
                        "strategy": "insurance_underwriting",
                        "min_iv_rv_ratio": 1.15,
                        "min_iv_minus_rv": 0.05,
                    },
                }
            ],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    assert rows[0]["strategy_family"] == "sell_put"
    assert rows[0]["strategy_profile"] == "insurance_underwriting"
    assert rows[0]["risk_model"] == "short_vol"
    assert rows[0]["short_vol_thesis_status"] == "observe"
    assert rows[0]["tier"] == "strong"
    assert "IV/RV edge" in rows[0]["short_vol_reason"]


def test_run_close_advice_refreshes_short_vol_quote_missing_rv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.0,
                "expiration": "2026-06-15",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-15",
                "strike": 100,
                "mid": 0.20,
                "bid": 0.19,
                "ask": 0.21,
                "dte": 60,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
                "delta": -0.20,
                "implied_volatility": 0.30,
                "event_source_status": "ok",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    calls: list[dict[str, object]] = []

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        calls.append({"symbol": symbol, **kwargs})
        assert kwargs["include_realized_volatility"] is True
        assert kwargs["explicit_expirations"] == ["2026-06-15"]
        return {
            "rows": [
                {
                    "symbol": "NVDA",
                    "option_type": "put",
                    "expiration": "2026-06-15",
                    "strike": 100,
                    "mid": 0.20,
                    "bid": 0.19,
                    "ask": 0.21,
                    "dte": 60,
                    "multiplier": 100,
                    "spot": 120,
                    "currency": "USD",
                    "delta": -0.20,
                    "implied_volatility": 0.30,
                    "realized_volatility_estimate": 0.20,
                    "event_source_status": "ok",
                }
            ]
        }

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fake_fetch_symbol)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={
            "close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]},
            "symbols": [
                {
                    "symbol": "NVDA",
                    "fetch": {"source": "futu"},
                    "sell_put": {
                        "strategy": "insurance_underwriting",
                        "event_source_fail_closed": False,
                    },
                }
            ],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    assert calls and calls[0]["symbol"] == "NVDA"
    assert result["evaluation_gap_rows"] == 0
    assert rows[0]["realized_volatility_estimate"] == pytest.approx(0.20)
    assert "short_vol_risk_data_missing" not in str(rows[0]["data_quality_flags"])


def test_run_close_advice_does_not_require_rv_for_return_first_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.0,
                "expiration": "2026-06-15",
                "strategy_snapshot": {"strategy_family": "sell_put", "strategy_profile": "return_first"},
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-15",
                "strike": 100,
                "mid": 0.20,
                "bid": 0.19,
                "ask": 0.21,
                "dte": 60,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
                "delta": -0.20,
                "implied_volatility": 0.30,
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    def fail_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        raise AssertionError(f"unexpected short-vol RV fetch for {symbol}: {kwargs}")

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fail_fetch_symbol)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={
            "close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]},
            "symbols": [{"symbol": "NVDA", "fetch": {"source": "futu"}, "sell_put": {"strategy": "insurance_underwriting"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    assert result["evaluation_gap_rows"] == 0
    assert rows[0]["strategy_profile"] == "return_first"
    assert rows[0]["tier"] == "strong"


def test_run_close_advice_uses_income_upside_mode_for_yield_enhancement_put(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.0,
                "expiration": "2026-06-15",
                "strategy": "yield_enhancement",
                "leg_role": "sell_put",
                "strategy_group_id": "ye_nvda_income",
                "yield_enhancement_mode": "income_upside_enhancement",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-15",
                "strike": 100,
                "mid": 0.20,
                "bid": 0.19,
                "ask": 0.21,
                "dte": 60,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
                "delta": -0.20,
                "implied_volatility": 0.30,
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={
            "close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]},
            "symbols": [{"symbol": "NVDA", "sell_put": {"strategy": "insurance_underwriting"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    assert result["evaluation_gap_rows"] == 0
    assert rows[0]["strategy_profile"] == "return_first"
    assert rows[0]["strategy_source"] == "position_yield_enhancement_mode"
    assert rows[0]["risk_model"] == "return_first_legacy"
    assert rows[0]["close_action"] == "close_put_keep_call"
    assert "short_vol_risk_data_missing" not in str(rows[0]["data_quality_flags"])


def test_run_close_advice_uses_vol_convexity_mode_for_yield_enhancement_put(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.0,
                "expiration": "2026-06-15",
                "strategy": "yield_enhancement",
                "leg_role": "sell_put",
                "strategy_group_id": "ye_nvda_vol",
                "yield_enhancement_mode": "vol_convexity_enhancement",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-15",
                "strike": 100,
                "mid": 0.20,
                "bid": 0.19,
                "ask": 0.21,
                "dte": 60,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
                "delta": -0.20,
                "implied_volatility": 0.30,
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={
            "close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]},
            "symbols": [{"symbol": "NVDA", "sell_put": {"strategy": "return_first"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    assert result["evaluation_gap_rows"] == 1
    assert rows[0]["strategy_profile"] == "short_vol"
    assert rows[0]["strategy_source"] == "position_yield_enhancement_mode"
    assert rows[0]["risk_model"] == "short_vol"
    assert rows[0]["close_action"] == "not_evaluable"
    assert "short_vol_risk_data_missing" in str(rows[0]["data_quality_flags"])
    assert "rv" in rows[0]["reason"]


def test_run_close_advice_merges_event_snapshot_for_short_vol_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    out_dir = tmp_path / "output_runs" / "run1" / "accounts" / "lx"
    snapshot_path = tmp_path / "output_runs" / "run1" / "state" / "event_snapshot.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": "yfinance",
                "symbols": {
                    "PDD": {
                        "symbol": "PDD",
                        "events": [],
                        "source_status": "ok",
                        "source_error": "",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "PDD",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 85,
                "multiplier": 100,
                "premium": 1.0,
                "expiration": "2026-06-18",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "PDD",
                "option_type": "put",
                "expiration": "2026-06-18",
                "strike": 85,
                "mid": 0.20,
                "bid": 0.19,
                "ask": 0.21,
                "dte": 63,
                "multiplier": 100,
                "spot": 110,
                "currency": "USD",
                "delta": -0.20,
                "implied_volatility": 0.30,
                "realized_volatility_estimate": 0.20,
            }
        ]
    ).to_csv(parsed / "PDD_required_data.csv", index=False)

    result = run_close_advice(
        config={
            "close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]},
            "symbols": [{"symbol": "PDD", "sell_put": {"strategy": "insurance_underwriting"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    assert result["evaluation_gap_rows"] == 0
    assert rows[0]["event_source_status"] == "ok"
    assert rows[0]["event_risk_flag"] is False or rows[0]["event_risk_flag"] == "False"
    assert "event_source_unavailable" not in str(rows[0]["data_quality_flags"])


def test_run_close_advice_reports_missing_event_snapshot_for_short_vol_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "PDD",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 85,
                "multiplier": 100,
                "premium": 1.0,
                "expiration": "2026-06-18",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "PDD",
                "option_type": "put",
                "expiration": "2026-06-18",
                "strike": 85,
                "mid": 0.20,
                "bid": 0.19,
                "ask": 0.21,
                "dte": 63,
                "multiplier": 100,
                "spot": 110,
                "currency": "USD",
                "delta": -0.20,
                "implied_volatility": 0.30,
                "realized_volatility_estimate": 0.20,
            }
        ]
    ).to_csv(parsed / "PDD_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={
            "close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]},
            "symbols": [{"symbol": "PDD", "sell_put": {"strategy": "insurance_underwriting"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=tmp_path,
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    assert result["evaluation_gap_rows"] == 0
    assert rows[0]["tier"] == "strong"
    assert rows[0]["evaluation_status"] == "priced"
    assert rows[0]["event_source_status"] == "error"
    assert rows[0]["event_source_error"] == "event snapshot missing for symbol"
    assert rows[0]["event_context_status"] == "error"
    assert "event_source_unavailable" not in str(rows[0]["data_quality_flags"])
    assert "event snapshot missing for symbol" not in text


def test_run_close_advice_records_missing_event_snapshot_symbol_as_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    snapshot_path = tmp_path / "state" / "event_snapshot.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps({"schema_version": 1, "provider": "yfinance", "symbols": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "PDD",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 85,
                "multiplier": 100,
                "premium": 1.0,
                "expiration": "2026-06-18",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "PDD",
                "option_type": "put",
                "expiration": "2026-06-18",
                "strike": 85,
                "mid": 0.20,
                "bid": 0.19,
                "ask": 0.21,
                "dte": 63,
                "multiplier": 100,
                "spot": 110,
                "currency": "USD",
                "delta": -0.20,
                "implied_volatility": 0.30,
                "realized_volatility_estimate": 0.20,
            }
        ]
    ).to_csv(parsed / "PDD_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={
            "runtime": {"event_snapshot_path": str(snapshot_path)},
            "close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]},
            "symbols": [{"symbol": "PDD", "sell_put": {"strategy": "insurance_underwriting"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    assert result["evaluation_gap_rows"] == 0
    assert rows[0]["tier"] == "strong"
    assert rows[0]["evaluation_status"] == "priced"
    assert rows[0]["event_source_status"] == "error"
    assert rows[0]["event_context_status"] == "error"
    assert "event_source_unavailable" not in str(rows[0]["data_quality_flags"])
    assert "event snapshot missing for symbol" not in text


def test_run_close_advice_rechecks_stale_quote_event_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "PDD",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 85,
                "multiplier": 100,
                "premium": 1.0,
                "expiration": "2026-06-18",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "PDD",
                "option_type": "put",
                "expiration": "2026-06-18",
                "strike": 85,
                "mid": 0.20,
                "bid": 0.19,
                "ask": 0.21,
                "dte": 63,
                "multiplier": 100,
                "spot": 110,
                "currency": "USD",
                "delta": -0.20,
                "implied_volatility": 0.30,
                "realized_volatility_estimate": 0.20,
                "event_flag": True,
                "event_types": "earnings",
                "event_dates": "2026-07-01",
                "event_source_status": "ok",
            }
        ]
    ).to_csv(parsed / "PDD_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={
            "close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]},
            "symbols": [{"symbol": "PDD", "sell_put": {"strategy": "insurance_underwriting"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=tmp_path,
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    assert result["evaluation_gap_rows"] == 0
    assert rows[0]["event_risk_flag"] is False or rows[0]["event_risk_flag"] == "False"
    assert rows[0]["event_context_status"] != "in_window"
    assert str(rows[0].get("event_risk_dates") or "") in {"", "nan"}


def test_run_close_advice_prefers_position_strategy_snapshot_over_current_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.0,
                "expiration": "2026-06-15",
                "strategy_snapshot": {"strategy_family": "sell_put", "strategy_profile": "return_first"},
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-15",
                "strike": 100,
                "mid": 0.20,
                "bid": 0.19,
                "ask": 0.21,
                "dte": 60,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    run_close_advice(
        config={
            "close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]},
            "symbols": [{"symbol": "NVDA", "sell_put": {"strategy": "insurance_underwriting"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    assert rows[0]["strategy_source"] == "position_snapshot"
    assert rows[0]["strategy_profile"] == "return_first"
    assert rows[0]["risk_model"] == "return_first_legacy"
    assert rows[0]["tier"] == "strong"


def test_run_close_advice_uses_yield_enhancement_put_exit_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.6,
                "expiration": "2026-05-15",
                "strategy": "yield_enhancement",
                "leg_role": "sell_put",
                "strategy_group_id": "ye_nvda_1",
                "yield_enhancement_mode": "income_upside_enhancement",
                "strategy_snapshot": {"strategy_family": "sell_put", "strategy_profile": "return_first"},
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-05-15",
                "strike": 100,
                "mid": 0.22,
                "bid": 0.21,
                "ask": 0.23,
                "dte": 29,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    run_close_advice(
        config={"close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    assert rows[0]["close_action"] == "close_put_keep_call"
    assert pd.isna(rows[0].get("optional_combo_action")) or rows[0]["optional_combo_action"] == ""
    assert rows[0]["paired_leg_status"] == "missing"
    assert rows[0]["combo_cost_basis_status"] == "missing_paired_call"
    assert rows[0]["strategy_exit_mode"] == "yield_enhancement_put_leg"
    assert rows[0]["yield_enhancement_mode"] == "income_upside_enhancement"
    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    assert "买回 Put，保留收益增强 Call" in text


def test_run_close_advice_holds_yield_enhancement_put_when_threshold_not_met(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.6,
                "expiration": "2026-05-15",
                "strategy": "yield_enhancement",
                "leg_role": "sell_put",
                "strategy_group_id": "ye_nvda_1",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-05-15",
                "strike": 100,
                "mid": 1.40,
                "bid": 1.39,
                "ask": 1.41,
                "dte": 29,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    run_close_advice(
        config={"close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    assert rows[0]["tier"] == "none"
    assert rows[0]["close_action"] == "hold_put_keep_call"
    assert rows[0]["strategy_exit_mode"] == "yield_enhancement_put_leg"


def test_run_close_advice_uses_yield_enhancement_long_call_exit_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "call",
                "side": "long",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 140,
                "multiplier": 100,
                "premium": 1.0,
                "expiration": "2026-05-15",
                "strategy": "yield_enhancement",
                "leg_role": "enhancement_call",
                "strategy_group_id": "ye_nvda_1",
                "yield_enhancement_mode": "vol_convexity_enhancement",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "call",
                "expiration": "2026-05-15",
                "strike": 140,
                "mid": 2.20,
                "bid": 2.10,
                "ask": 2.30,
                "dte": 29,
                "multiplier": 100,
                "spot": 130,
                "currency": "USD",
                "delta": 0.25,
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    run_close_advice(
        config={"close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    assert rows[0]["exit_state"] == "take_profit"
    assert rows[0]["close_action"] == "sell_call_take_profit"
    assert rows[0]["strategy_exit_mode"] == "yield_enhancement_long_call_leg"
    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    assert "卖出收益增强 Call 止盈" in text
    assert "Call价值: 现值/成本=2.2x | 剩余DTE=29 | 浮盈=+120%" in text
    assert "建议出价=$2.20 | 买一/卖一=$2.10/$2.30" in text


def test_run_close_advice_reports_yield_enhancement_combo_economics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    monkeypatch.setattr("src.application.close_advice_runner.calc_futu_option_fee", lambda *args, **kwargs: 0.0)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.6,
                "expiration": "2026-05-15",
                "strategy": "yield_enhancement",
                "leg_role": "sell_put",
                "strategy_group_id": "ye_nvda_1",
                "yield_enhancement_mode": "income_upside_enhancement",
            },
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "call",
                "side": "long",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 140,
                "multiplier": 100,
                "premium": 0.3,
                "expiration": "2026-05-15",
                "strategy": "yield_enhancement",
                "leg_role": "enhancement_call",
                "strategy_group_id": "ye_nvda_1",
                "yield_enhancement_mode": "income_upside_enhancement",
            },
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-05-15",
                "strike": 100,
                "mid": 0.20,
                "bid": 0.19,
                "ask": 0.21,
                "dte": 29,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            },
            {
                "symbol": "NVDA",
                "option_type": "call",
                "expiration": "2026-05-15",
                "strike": 140,
                "mid": 0.50,
                "bid": 0.49,
                "ask": 0.51,
                "dte": 29,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
                "delta": 0.10,
            },
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    run_close_advice(
        config={"close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    put_row = next(row for row in rows if row["option_type"] == "put")
    assert put_row["combo_cost_basis_status"] == "ok"
    assert put_row["put_leg_realized_if_close"] == pytest.approx(140.0)
    assert put_row["combo_call_cost"] == pytest.approx(30.0)
    assert put_row["combo_call_value_if_close"] == pytest.approx(50.0)
    assert put_row["combo_net_locked_if_close_put_keep_call"] == pytest.approx(110.0)
    assert put_row["combo_net_if_close_both"] == pytest.approx(160.0)
    assert put_row["optional_combo_action"] == "close_both_optional"


def test_run_close_advice_preserves_domain_not_evaluable_status(tmp_path: Path) -> None:
    expiration = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "NVDA",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": "",
                        "expiration": expiration,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": expiration,
                "strike": 100,
                "mid": 0.10,
                "bid": 0.09,
                "ask": 0.11,
                "dte": 30,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={"close_advice": {"enabled": True, "quote_source": "required_data", "notify_levels": ["strong"]}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    df = pd.read_csv(out_dir / "close_advice.csv")
    row = df.iloc[0]
    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    assert result["notify_rows"] == 0
    assert result["evaluation_gap_rows"] == 1
    assert row["evaluation_status"] == "not_evaluable"
    assert row["quote_status"] == "not_evaluable"
    assert row["tier"] == "not_evaluable"
    assert "missing_premium" in row["data_quality_flags"]
    assert "待补数据" in text
    assert "无法评估" in text


def test_run_close_advice_prefers_context_expiration_ymd_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.6,
                "expiration_ymd": "2026-05-15",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-05-15",
                "strike": 100,
                "mid": 0.22,
                "bid": 0.21,
                "ask": 0.23,
                "dte": 29,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={"close_advice": {"enabled": True}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    assert result["rows"] == 1
    assert "NVDA Put 2026-05-15" in (out_dir / "close_advice.txt").read_text(encoding="utf-8")


def test_run_close_advice_normalizes_business_midnight_expiration_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "sy",
                "symbol": "PDD",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.6,
                "expiration": 1781712000000,
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "PDD",
                "option_type": "put",
                "expiration": "2026-06-18",
                "strike": 100,
                "mid": 0.22,
                "bid": 0.21,
                "ask": 0.23,
                "dte": 48,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "PDD_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={"close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    csv_text = (out_dir / "close_advice.csv").read_text(encoding="utf-8")
    assert result["rows"] == 1
    assert "PDD Put 2026-06-18" in text
    assert "2026-06-17" not in text
    assert "coverage_missing" not in csv_text


def test_close_advice_recalculates_dte_from_business_today(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.application import close_advice_runner as runner

    monkeypatch.setattr(
        runner,
        "expiration_business_today",
        lambda: datetime(2026, 5, 1, tzinfo=timezone.utc).date(),
    )

    assert runner._calc_dte("2026-05-01", {"dte": 99}) == 0
    assert runner._calc_dte("2026-05-02", {"dte": 99}) == 1


def test_run_close_advice_records_missing_quote_but_does_not_notify(tmp_path: Path) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "AAPL",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 1.0,
                        "expiration": "2026-05-15",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "0700.HK",
                "option_type": "put",
                "expiration": "2026-04-29",
                "strike": 480,
                "mid": None,
                "last_price": None,
                "bid": None,
                "ask": None,
                "dte": 8,
                "multiplier": 100,
                "spot": 500,
                "currency": "HKD",
            }
        ]
    ).to_csv(parsed / "0700.HK_required_data.csv", index=False)
    out_dir = tmp_path / "reports"

    run_close_advice(
        config={
            "close_advice": {"enabled": True},
            "symbols": [{"symbol": "AAPL", "fetch": {"source": "yahoo"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    assert "待补数据" in text
    assert "AAPL Put 2026-05-15 100.00P" in text
    assert "missing_quote" in (out_dir / "close_advice.csv").read_text(encoding="utf-8")


def test_run_close_advice_fetches_missing_quote_via_opend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "0700.HK",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "HKD",
                        "strike": 480,
                        "multiplier": 100,
                        "premium": 8.0,
                        "expiration": "2026-04-29",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "0700.HK",
                "option_type": "put",
                "expiration": "2026-04-29",
                "strike": 480,
                "mid": None,
                "last_price": None,
                "bid": None,
                "ask": None,
                "dte": 8,
                "multiplier": 100,
                "spot": 500,
                "currency": "HKD",
            }
        ]
    ).to_csv(parsed / "0700.HK_required_data.csv", index=False)
    out_dir = tmp_path / "reports"

    calls: list[dict[str, object]] = []

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        calls.append({"symbol": symbol, **kwargs})
        assert kwargs["explicit_expirations"] == ["2026-04-29"]
        assert kwargs["option_chain_max_calls"] == 6
        assert kwargs["option_chain_window_sec"] == 21.0
        assert kwargs["max_wait_sec"] == 22.0
        return {
            "rows": [
                {
                    "symbol": "0700.HK",
                    "option_type": "put",
                    "expiration": "2026-04-29",
                    "strike": 480,
                    "mid": 0.5,
                    "bid": 0.48,
                    "ask": 0.52,
                    "dte": 8,
                    "multiplier": 100,
                    "spot": 500,
                    "currency": "HKD",
                }
            ]
        }

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fake_fetch_symbol)

    run_close_advice(
        config={
            "close_advice": {"enabled": True},
            "runtime": {"option_chain_fetch": {"max_calls": 6, "window_sec": 21, "max_wait_sec": 22}},
            "symbols": [
                {
                    "symbol": "0700.HK",
                    "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
                }
            ],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    csv_text = (out_dir / "close_advice.csv").read_text(encoding="utf-8")
    assert "missing_quote" not in csv_text
    assert "mid_fallback_last_price" not in csv_text
    assert calls and calls[0]["symbol"] == "0700.HK"


def test_run_close_advice_uses_bid_ask_when_mid_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "NVDA",
                        "option_type": "put",
                        "side": "short",
                        "status": "open",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 1.6,
                        "expiration": "2026-05-15",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-05-15",
                "strike": 100,
                "mid": None,
                "last_price": None,
                "bid": 0.21,
                "ask": 0.23,
                "dte": 29,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    def fail_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        raise AssertionError(f"unexpected OpenD fetch for {symbol}: {kwargs}")

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fail_fetch_symbol)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={
            "close_advice": {"enabled": True, "notify_levels": ["strong", "medium"], "max_items_per_account": 5},
            "symbols": [{"symbol": "NVDA", "fetch": {"source": "futu"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    csv_text = (out_dir / "close_advice.csv").read_text(encoding="utf-8")
    assert result["quote_issue_rows"] == 0
    assert "mid_from_bid_ask" in csv_text
    assert "missing_quote" not in csv_text
    assert "missing_mid" not in csv_text


def test_run_close_advice_recalculates_dte_from_position_expiration(tmp_path: Path) -> None:
    expiration = (expiration_business_today() + timedelta(days=40)).isoformat()
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "NVDA",
                        "option_type": "put",
                        "side": "short",
                        "status": "open",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 1.0,
                        "expiration": expiration,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": expiration,
                "strike": 100,
                "mid": 0.20,
                "bid": 0.19,
                "ask": 0.21,
                "dte": 1,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={"close_advice": {"enabled": True, "notify_levels": ["strong"]}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    df = pd.read_csv(out_dir / "close_advice.csv")
    assert result["notify_rows"] == 1
    assert int(df.iloc[0]["dte"]) == 40
    assert df.iloc[0]["tier"] == "strong"


def test_run_close_advice_blocks_last_price_only_quote_from_notifications(tmp_path: Path) -> None:
    expiration = (datetime.now(timezone.utc).date() + timedelta(days=3)).isoformat()
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "NVDA",
                        "option_type": "put",
                        "side": "short",
                        "status": "open",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 1.0,
                        "expiration": expiration,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": expiration,
                "strike": 100,
                "mid": 0.04,
                "last_price": 0.04,
                "bid": 0.0,
                "ask": 0.0,
                "dte": 3,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={"close_advice": {"enabled": True, "quote_source": "required_data", "notify_levels": ["optional"]}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    csv_text = (out_dir / "close_advice.csv").read_text(encoding="utf-8")
    assert result["notify_rows"] == 0
    assert result["evaluation_gap_rows"] == 1
    assert "mid_fallback_last_price" in csv_text
    assert "not_evaluable" in csv_text


def test_run_close_advice_reports_quote_issue_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "AAPL",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 1.0,
                        "expiration": "2026-05-15",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    (required_root / "parsed").mkdir(parents=True)
    out_dir = tmp_path / "reports"

    def fail_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        raise RuntimeError(f"fetch unavailable for {symbol}")

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fail_fetch_symbol)

    result = run_close_advice(
        config={"close_advice": {"enabled": True}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    assert result["notify_rows"] == 0
    assert result["quote_issue_rows"] == 1
    assert result["flag_counts"]["missing_quote"] == 1
    assert result["flag_counts"]["required_data_fetch_error"] == 1
    assert result["evaluation_gap_rows"] == 1
    assert result["coverage_summary"]["coverage_fetch_errors"] == 1
    assert result["quote_issue_samples"][0].startswith("AAPL put 2026-05-15 100.00P: 补拉持仓覆盖失败")


def test_run_close_advice_reports_missing_expiration_coverage_without_opend_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "9992.HK",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "HKD",
                        "strike": 135,
                        "multiplier": 100,
                        "premium": 0.88,
                        "expiration": "2026-04-29",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "9992.HK",
                "option_type": "put",
                "expiration": "2026-05-28",
                "strike": 135,
                "mid": 0.04,
                "bid": 0.03,
                "ask": 0.05,
                "dte": 30,
                "multiplier": 100,
                "spot": 150,
                "currency": "HKD",
            }
        ]
    ).to_csv(parsed / "9992.HK_required_data.csv", index=False)

    def fail_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        raise AssertionError(f"unexpected OpenD fetch for {symbol}: {kwargs}")

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fail_fetch_symbol)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={
            "close_advice": {"enabled": True},
            "symbols": [{"symbol": "9992.HK", "fetch": {"source": "futu", "limit_expirations": 1}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    csv_text = (out_dir / "close_advice.csv").read_text(encoding="utf-8")
    assert "required_data_fetch_error" in csv_text
    assert "opend_fetch_no_usable_quote" not in csv_text
    assert result["coverage_summary"]["coverage_fetch_errors"] == 1
    assert result["quote_fetch_diagnostics"]["attempted"] == 0
    assert result["quote_issue_samples"][0].startswith("9992.HK put 2026-04-29 135.00P: 补拉持仓覆盖失败")
    assert "待补数据" in text
    assert "9992.HK Put 2026-04-29 135.00P" in text


def test_run_close_advice_fetches_missing_position_coverage_before_pricing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put_expiration = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
    call_expiration = (datetime.now(timezone.utc).date() + timedelta(days=62)).isoformat()
    near_miss_expiration = (datetime.now(timezone.utc).date() + timedelta(days=45)).isoformat()
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "sy",
                        "symbol": "9992.HK",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "HKD",
                        "strike": 135,
                        "multiplier": 100,
                        "premium": 0.88,
                        "expiration": put_expiration,
                        "strategy_snapshot": {"strategy_family": "sell_put", "strategy_profile": "return_first"},
                    },
                    {
                        "account": "sy",
                        "symbol": "9992.HK",
                        "option_type": "call",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "HKD",
                        "strike": 200,
                        "multiplier": 100,
                        "premium": 1.50,
                        "expiration": call_expiration,
                        "strategy_snapshot": {"strategy_family": "sell_call", "strategy_profile": "return_first"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "9992.HK",
                "option_type": "put",
                "expiration": near_miss_expiration,
                "strike": 135,
                "mid": 0.04,
                "bid": 0.035,
                "ask": 0.045,
                "dte": 30,
                "multiplier": 100,
                "spot": 150,
                "currency": "HKD",
            }
        ]
    ).to_csv(parsed / "9992.HK_required_data.csv", index=False)

    calls: list[dict[str, object]] = []

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        calls.append({"symbol": symbol, **kwargs})
        assert symbol == "9992.HK"
        assert kwargs["explicit_expirations"] == [put_expiration, call_expiration]
        assert kwargs["chain_cache_force_refresh"] is False
        assert kwargs["freshness_policy"] == "refresh_missing"
        assert kwargs["option_chain_max_calls"] == 3
        assert kwargs["option_chain_window_sec"] == 11.0
        assert kwargs["max_wait_sec"] == 12.0
        assert kwargs["snapshot_max_calls"] == 4
        assert kwargs["snapshot_window_sec"] == 13.0
        assert kwargs["snapshot_max_wait_sec"] == 14.0
        assert kwargs["expiration_max_calls"] == 5
        assert kwargs["expiration_window_sec"] == 15.0
        assert kwargs["expiration_max_wait_sec"] == 16.0
        return {
            "rows": [
                {
                    "symbol": "9992.HK",
                    "option_type": "put",
                    "expiration": put_expiration,
                    "strike": 135,
                    "mid": 0.04,
                    "bid": 0.035,
                    "ask": 0.045,
                    "dte": 30,
                    "multiplier": 100,
                    "spot": 140,
                    "currency": "HKD",
                },
                {
                    "symbol": "9992.HK",
                    "option_type": "call",
                    "expiration": call_expiration,
                    "strike": 200,
                    "mid": 1.50,
                    "bid": 1.34,
                    "ask": 1.52,
                    "dte": 62,
                    "multiplier": 100,
                    "spot": 140,
                    "currency": "HKD",
                },
            ]
        }

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fake_fetch_symbol)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={
            "close_advice": {"enabled": True},
            "runtime": {
                "option_chain_fetch": {"max_calls": 3, "window_sec": 11, "max_wait_sec": 12},
                "opend_rate_limits": {
                    "market_snapshot": {"max_calls": 4, "window_sec": 13, "max_wait_sec": 14},
                    "option_expiration": {"max_calls": 5, "window_sec": 15, "max_wait_sec": 16},
                },
            },
            "symbols": [{"symbol": "9992.HK", "fetch": {"source": "futu", "limit_expirations": 1}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    csv_text = (out_dir / "close_advice.csv").read_text(encoding="utf-8")
    refreshed_text = (parsed / "9992.HK_required_data.csv").read_text(encoding="utf-8")
    assert calls and calls[0]["symbol"] == "9992.HK"
    assert result["evaluation_gap_rows"] == 0
    assert result["coverage_summary"]["coverage_fetch_attempted_symbols"] == 1
    assert "required_data_missing_expiration" not in csv_text
    assert put_expiration in refreshed_text
    assert call_expiration in refreshed_text


def test_run_close_advice_reports_expiration_near_miss_in_quote_issue_samples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "0700.HK",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "HKD",
                        "strike": 450,
                        "multiplier": 100,
                        "premium": 0.88,
                        "expiration": "2026-05-27",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "0700.HK",
                "option_type": "put",
                "expiration": "2026-05-28",
                "strike": 450,
                "mid": 0.04,
                "bid": 0.03,
                "ask": 0.05,
                "dte": 30,
                "multiplier": 100,
                "spot": 150,
                "currency": "HKD",
            }
        ]
    ).to_csv(parsed / "0700.HK_required_data.csv", index=False)

    def fail_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        raise AssertionError(f"unexpected OpenD fetch for {symbol}: {kwargs}")

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fail_fetch_symbol)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={
            "close_advice": {"enabled": True},
            "symbols": [{"symbol": "0700.HK", "fetch": {"source": "futu", "limit_expirations": 1}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    assert result["coverage_summary"]["expiration_near_miss_count"] == 1
    assert result["quote_issue_samples"] == [
        "0700.HK put 2026-05-27 450.00P: 补拉持仓覆盖失败 | near_miss=2026-05-27->2026-05-28"
    ]


def test_run_close_advice_fee_can_block_gross_strong_signal(tmp_path: Path) -> None:
    expiration = (expiration_business_today() + timedelta(days=40)).isoformat()
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "AAPL",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 0.02,
                        "expiration": expiration,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "option_type": "put",
                "expiration": expiration,
                "strike": 100,
                "mid": 0.001,
                "bid": 0.001,
                "ask": 0.001,
                "dte": 40,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "AAPL_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={"close_advice": {"enabled": True, "notify_levels": ["strong", "medium", "optional", "weak"]}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    csv_text = (out_dir / "close_advice.csv").read_text(encoding="utf-8")
    assert result["notify_rows"] == 0
    assert "not_profitable_after_fee" in csv_text
    assert "-0.41915000000000013" in csv_text
    assert (out_dir / "close_advice.txt").read_text(encoding="utf-8") == ""


def test_run_close_advice_renders_small_money_with_decimals(tmp_path: Path) -> None:
    expiration = (expiration_business_today() + timedelta(days=40)).isoformat()
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "AAPL",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 0.05,
                        "expiration": expiration,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "option_type": "put",
                "expiration": expiration,
                "strike": 100,
                "mid": 0.001,
                "bid": 0.001,
                "ask": 0.001,
                "dte": 40,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "AAPL_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    run_close_advice(
        config={"close_advice": {"enabled": True, "notify_levels": ["strong", "medium", "optional", "weak"]}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    assert "$2.58" in text
    assert "$0.10" in text


def test_run_close_advice_keeps_tier_when_fee_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "NVDA",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 1.6,
                        "expiration": "2026-05-15",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-05-15",
                "strike": 100,
                "mid": 0.22,
                "bid": 0.21,
                "ask": 0.23,
                "dte": 29,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    monkeypatch.setattr("src.application.close_advice_runner.calc_futu_option_fee", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fee unavailable")))

    out_dir = tmp_path / "reports"
    run_close_advice(
        config={"close_advice": {"enabled": True, "notify_levels": ["strong", "medium"]}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    csv_text = (out_dir / "close_advice.csv").read_text(encoding="utf-8")
    assert "fee_calc_unavailable" in csv_text
    assert "strong" in csv_text


def test_run_close_advice_groups_mixed_accounts_and_counts_rendered_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "NVDA",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 1.6,
                        "expiration": "2026-05-15",
                    },
                    {
                        "account": "lx",
                        "symbol": "AAPL",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 1.6,
                        "expiration": "2026-05-15",
                    },
                    {
                        "account": "sy",
                        "symbol": "TSLA",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 1.6,
                        "expiration": "2026-05-15",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    for symbol in ("NVDA", "AAPL", "TSLA"):
        pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "option_type": "put",
                    "expiration": "2026-05-15",
                    "strike": 100,
                    "mid": 0.22,
                    "bid": 0.21,
                    "ask": 0.23,
                    "dte": 29,
                    "multiplier": 100,
                    "spot": 120,
                    "currency": "USD",
                }
            ]
        ).to_csv(parsed / f"{symbol}_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={"close_advice": {"enabled": True, "notify_levels": ["strong", "medium"], "max_items_per_account": 1}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    assert result["notify_rows"] == 2
    assert "### [lx] 平仓建议" in text
    assert "### [sy] 平仓建议" in text
    assert text.count("强烈建议平仓") == 2


def test_run_close_advice_max_items_zero_means_unlimited(tmp_path: Path) -> None:
    expiration = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "NVDA",
                        "option_type": "put",
                        "side": "short",
                        "status": "open",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 5.0,
                        "expiration": expiration,
                    },
                    {
                        "account": "lx",
                        "symbol": "AAPL",
                        "option_type": "put",
                        "side": "short",
                        "status": "open",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 5.0,
                        "expiration": expiration,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    for symbol in ("NVDA", "AAPL"):
        pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "option_type": "put",
                    "expiration": expiration,
                    "strike": 100,
                    "mid": 0.35,
                    "bid": 0.34,
                    "ask": 0.36,
                    "dte": 30,
                    "multiplier": 100,
                    "spot": 120,
                    "currency": "USD",
                }
            ]
        ).to_csv(parsed / f"{symbol}_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={"close_advice": {"enabled": True, "notify_levels": ["strong"], "max_items_per_account": 0}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    assert result["notify_rows"] == 2
    assert "NVDA Put" in text
    assert "AAPL Put" in text


def test_run_close_advice_filters_positions_to_current_markets(tmp_path: Path) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "0700.HK",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "HKD",
                        "strike": 480,
                        "multiplier": 100,
                        "premium": 8.0,
                        "expiration": "2026-04-29",
                    },
                    {
                        "account": "lx",
                        "symbol": "NVDA",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 1.6,
                        "expiration": "2026-05-15",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "0700.HK",
                "option_type": "put",
                "expiration": "2026-04-29",
                "strike": 480,
                "mid": 0.22,
                "bid": 0.21,
                "ask": 0.23,
                "dte": 29,
                "multiplier": 100,
                "spot": 500,
                "currency": "HKD",
            },
        ]
    ).to_csv(parsed / "0700.HK_required_data.csv", index=False)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-05-15",
                "strike": 100,
                "mid": 0.22,
                "bid": 0.21,
                "ask": 0.23,
                "dte": 29,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            },
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={"close_advice": {"enabled": True}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
        markets_to_run=["HK"],
    )

    csv_text = (out_dir / "close_advice.csv").read_text(encoding="utf-8")
    assert result["rows"] == 1
    assert "0700.HK" in csv_text
    assert "NVDA" not in csv_text


def test_run_close_advice_fetches_quote_when_required_data_row_has_no_usable_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "0700.HK",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "HKD",
                        "strike": 480,
                        "multiplier": 100,
                        "premium": 8.0,
                        "expiration": "2026-04-29",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "0700.HK",
                "option_type": "put",
                "expiration": "2026-04-29",
                "strike": 480,
                "mid": None,
                "last_price": None,
                "bid": None,
                "ask": None,
                "currency": "HKD",
            }
        ]
    ).to_csv(parsed / "0700.HK_required_data.csv", index=False)

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        assert symbol == "0700.HK"
        return {
            "rows": [
                {
                    "symbol": "0700.HK",
                    "option_type": "put",
                    "expiration": "2026-04-29",
                    "strike": 480,
                    "mid": 0.6,
                    "bid": 0.58,
                    "ask": 0.62,
                    "dte": 8,
                    "multiplier": 100,
                    "spot": 500,
                    "currency": "HKD",
                }
            ]
        }

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fake_fetch_symbol)

    run_close_advice(
        config={
            "close_advice": {"enabled": True},
            "symbols": [{"symbol": "0700.HK", "fetch": {"source": "opend"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=(tmp_path / "reports"),
        base_dir=Path.cwd(),
    )

    csv_text = ((tmp_path / "reports") / "close_advice.csv").read_text(encoding="utf-8")
    assert "missing_mid" not in csv_text
    assert "0.6" in csv_text


def test_run_close_advice_counts_spread_block_as_quote_issue(tmp_path: Path) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "NVDA",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 1.0,
                        "expiration": "2026-05-15",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-05-15",
                "strike": 100,
                "mid": 0.5,
                "bid": 0.1,
                "ask": 0.9,
                "dte": 30,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={"close_advice": {"enabled": True}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    assert result["quote_issue_rows"] == 1
    assert result["flag_counts"]["spread_too_wide"] == 1


def test_run_close_advice_fetches_quote_for_alias_symbol_via_opend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "POP",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "HKD",
                        "strike": 135,
                        "multiplier": 100,
                        "premium": 1.0,
                        "expiration": "2026-04-29",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "9992.HK",
                "option_type": "put",
                "expiration": "2026-04-29",
                "strike": 135,
                "mid": None,
                "last_price": None,
                "bid": None,
                "ask": None,
                "dte": 1,
                "multiplier": 100,
                "spot": 140,
                "currency": "HKD",
            }
        ]
    ).to_csv(parsed / "9992.HK_required_data.csv", index=False)
    out_dir = tmp_path / "reports"

    calls: list[dict[str, object]] = []

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        calls.append({"symbol": symbol, **kwargs})
        assert symbol == "9992.HK"
        return {
            "rows": [
                {
                    "symbol": "9992.HK",
                    "option_type": "put",
                    "expiration": "2026-04-29",
                    "strike": 135,
                    "last_price": 0.04,
                    "bid": 0.0,
                    "ask": 0.04,
                    "dte": 1,
                    "multiplier": 100,
                    "spot": 140,
                    "currency": "HKD",
                }
            ]
        }

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fake_fetch_symbol)

    result = run_close_advice(
        config={
            "close_advice": {"enabled": True},
            "symbols": [{"symbol": "POP", "fetch": {"source": "futu"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    csv_text = (out_dir / "close_advice.csv").read_text(encoding="utf-8")
    assert calls and calls[0]["symbol"] == "9992.HK"
    assert "missing_quote" not in csv_text
    assert "missing_mid" not in csv_text
    assert "mid_fallback_last_price" in csv_text
    assert result["notify_rows"] == 0
    assert result["evaluation_gap_rows"] == 1
    assert result["flag_counts"]["mid_fallback_last_price"] == 1


def test_run_close_advice_required_data_mode_does_not_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "0700.HK",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "HKD",
                        "strike": 480,
                        "multiplier": 100,
                        "premium": 8.0,
                        "expiration": "2026-04-29",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "option_type": "put",
                "expiration": "2026-05-15",
                "strike": 100,
                "mid": None,
                "last_price": None,
                "bid": None,
                "ask": None,
                "dte": 17,
                "multiplier": 100,
                "spot": 110,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "AAPL_required_data.csv", index=False)

    calls: list[str] = []

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        calls.append(symbol)
        return {"rows": []}

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fake_fetch_symbol)

    run_close_advice(
        config={
            "close_advice": {"enabled": True, "quote_source": "required_data"},
            "symbols": [{"symbol": "0700.HK", "fetch": {"source": "futu"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=(tmp_path / "reports"),
        base_dir=Path.cwd(),
    )

    csv_text = ((tmp_path / "reports") / "close_advice.csv").read_text(encoding="utf-8")
    assert calls == []
    assert "missing_quote" in csv_text


def test_run_close_advice_non_futu_source_skips_opend_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "AAPL",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 1.0,
                        "expiration": "2026-05-15",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "option_type": "put",
                "expiration": "2026-05-15",
                "strike": 100,
                "mid": None,
                "last_price": None,
                "bid": None,
                "ask": None,
                "dte": 17,
                "multiplier": 100,
                "spot": 110,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "AAPL_required_data.csv", index=False)

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        raise AssertionError(f"unexpected OpenD fetch for {symbol}")

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fake_fetch_symbol)

    run_close_advice(
        config={
            "close_advice": {"enabled": True},
            # Explicit non-Futu source example: runtime should skip OpenD fetches,
            # not auto-downgrade or rewrite the configured source.
            "symbols": [{"symbol": "AAPL", "fetch": {"source": "yahoo"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=(tmp_path / "reports"),
        base_dir=Path.cwd(),
    )

    csv_text = ((tmp_path / "reports") / "close_advice.csv").read_text(encoding="utf-8")
    assert "missing_mid" in csv_text
    assert "opend_fetch_skipped_non_futu_source" in csv_text


def test_run_close_advice_preserves_missing_flag_when_opend_fetch_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "0700.HK",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "HKD",
                        "strike": 480,
                        "multiplier": 100,
                        "premium": 8.0,
                        "expiration": "2026-04-29",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "0700.HK",
                "option_type": "put",
                "expiration": "2026-04-29",
                "strike": 480,
                "mid": None,
                "last_price": None,
                "bid": None,
                "ask": None,
                "dte": 1,
                "multiplier": 100,
                "spot": 500,
                "currency": "HKD",
            }
        ]
    ).to_csv(parsed / "0700.HK_required_data.csv", index=False)

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("opend unavailable")

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fake_fetch_symbol)

    run_close_advice(
        config={
            "close_advice": {"enabled": True},
            "symbols": [{"symbol": "0700.HK", "fetch": {"source": "futu"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=(tmp_path / "reports"),
        base_dir=Path.cwd(),
    )

    csv_text = ((tmp_path / "reports") / "close_advice.csv").read_text(encoding="utf-8")
    assert "missing_mid" in csv_text
    assert "opend_fetch_error" in csv_text


def test_run_close_advice_surfaces_rate_limit_sample_when_opend_is_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "0700.HK",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "HKD",
                        "strike": 480,
                        "multiplier": 100,
                        "premium": 8.0,
                        "expiration": "2026-04-29",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "0700.HK",
                "option_type": "put",
                "expiration": "2026-04-29",
                "strike": 480,
                "mid": None,
                "last_price": None,
                "bid": None,
                "ask": None,
                "dte": 1,
                "multiplier": 100,
                "spot": 500,
                "currency": "HKD",
            }
        ]
    ).to_csv(parsed / "0700.HK_required_data.csv", index=False)

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("get_option_chain failed after 4 attempts: rate limit 最多10次")

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fake_fetch_symbol)

    result = run_close_advice(
        config={
            "close_advice": {"enabled": True},
            "symbols": [{"symbol": "0700.HK", "fetch": {"source": "futu"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=(tmp_path / "reports"),
        base_dir=Path.cwd(),
    )

    csv_text = ((tmp_path / "reports") / "close_advice.csv").read_text(encoding="utf-8")
    assert "opend_fetch_error_rate_limit" in csv_text
    assert result["quote_issue_samples"] == ["0700.HK put 2026-04-29 480.00P: OpenD 限频 | opend=HK.00700"]


def test_run_close_advice_surfaces_required_data_rate_limit_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "PDD",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 2.0,
                        "expiration": "2026-05-15",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "PDD",
                "option_type": "put",
                "expiration": "2026-06-19",
                "strike": 100,
                "mid": 1.0,
                "bid": 0.95,
                "ask": 1.05,
                "dte": 50,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "PDD_required_data.csv", index=False)
    before_csv = (parsed / "PDD_required_data.csv").read_text(encoding="utf-8")

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        assert symbol == "PDD"
        assert kwargs["freshness_policy"] == "refresh_missing"
        return {
            "symbol": "PDD",
            "underlier_code": "US.PDD",
            "spot": None,
            "expiration_count": 0,
            "expirations": [],
            "rows": [],
            "meta": {
                "source": "opend",
                "status": "error",
                "error_code": "RATE_LIMIT",
                "error": "获取期权链频率太高，请求失败，每30秒最多10次。",
            },
        }

    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fake_fetch_symbol)

    result = run_close_advice(
        config={
            "close_advice": {"enabled": True},
            "symbols": [{"symbol": "PDD", "fetch": {"source": "futu"}}],
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=(tmp_path / "reports"),
        base_dir=Path.cwd(),
    )

    csv_text = ((tmp_path / "reports") / "close_advice.csv").read_text(encoding="utf-8")
    text = ((tmp_path / "reports") / "close_advice.txt").read_text(encoding="utf-8")
    assert "required_data_fetch_error_rate_limit" in csv_text
    assert "OpenD 限频" in text
    assert "缺少可用定价" not in text
    assert result["quote_issue_samples"] == ["PDD put 2026-05-15 100.00P: OpenD 限频 | detail=获取期权链频率太高，请求失败，每30秒最多10次。"]
    assert (parsed / "PDD_required_data.csv").read_text(encoding="utf-8") == before_csv


def test_close_advice_text_can_drive_account_message_without_opening_candidates() -> None:
    result = AccountResult(
        account="lx",
        ran_scan=True,
        should_notify=True,
        decision_reason="到达通知点",
        notification_text=(
            "### [lx] 平仓建议\n"
            "- NVDA Put 2026-05-15 100.00P · 强烈建议平仓\n"
            "- 已锁定: 86.0% | 剩余DTE=29 | 剩余收益年化=6.8%\n"
            "---\n"
        ),
    )

    msg = build_account_message(result, now_bj="2026-04-16 21:30:00", cash_footer_lines=[])

    assert "账户提醒（lx）" in msg
    assert "平仓建议" in msg
    assert "Put 0 / Covered Call 0" in msg


def test_compact_close_advice_renders_risk_exit_loss_as_stop_loss_not_profit() -> None:
    from src.application.close_advice_runner import render_markdown_compact

    text = render_markdown_compact(
        [
            {
                "account": "lx",
                "symbol": "0700.HK",
                "option_type": "put",
                "expiration": "2026-07-30",
                "strike": 440,
                "currency": "HKD",
                "tier": "strong",
                "tier_label": "强烈建议平仓",
                "exit_state": "risk_exit",
                "short_vol_thesis_status": "event_risk",
                "capture_ratio": -0.728,
                "dte": 59,
                "remaining_annualized_return": 0.31,
                "close_mid": 22,
                "realized_if_close": -940,
                "remaining_premium": 2178,
                "evaluation_status": "priced",
            }
        ],
        notify_levels={"strong", "medium"},
        max_items=5,
    )

    assert "🔴 风险止损 0700.HK Put 440P @ 07-30" in text
    assert "- 风险退出 事件风险 · 59天 · 余年化 31%" in text
    assert "- 建议价 ¥22 · 平仓损益 ¥-940（余 ¥2,178）" in text
    assert "已锁定 -72.8%" not in text
    assert "收益 ¥-940" not in text


def test_compact_close_advice_does_not_render_short_vol_observation_as_close() -> None:
    from src.application.close_advice_runner import render_markdown_compact

    text = render_markdown_compact(
        [
            {
                "account": "lx",
                "symbol": "0700.HK",
                "option_type": "put",
                "expiration": "2026-07-30",
                "strike": 440,
                "currency": "HKD",
                "tier": "none",
                "tier_label": "不提醒",
                "exit_state": "hold",
                "exit_reason_type": "hold",
                "short_vol_thesis_status": "observe",
                "short_vol_reason": "short-vol 持仓存在观察项：到期前存在事件风险",
                "capture_ratio": 0.20,
                "dte": 59,
                "remaining_annualized_return": 0.31,
                "close_mid": 22,
                "realized_if_close": 100,
                "remaining_premium": 2178,
                "evaluation_status": "priced",
            }
        ],
        notify_levels={"strong", "medium"},
        max_items=5,
    )

    assert text == ""


def test_fee_gate_keeps_sell_put_event_loss_as_assignment_hold() -> None:
    from domain.domain.close_advice import HOLD_REASON_TYPE_ASSIGNMENT_ACCEPTABLE
    from src.application.close_advice_runner import _apply_fee_profitability_gate

    row = _apply_fee_profitability_gate(
        {
            "option_type": "put",
            "tier": "strong",
            "tier_label": "强烈建议平仓",
            "exit_state": "risk_exit",
            "exit_reason_type": "risk_exit",
            "short_vol_thesis_status": "event_risk",
            "realized_if_close": -1.0,
            "data_quality_flags": "",
        }
    )

    assert row["tier"] == "none"
    assert row["exit_state"] == "hold"
    assert row["exit_reason_type"] == "hold"
    assert row["hold_reason_type"] == HOLD_REASON_TYPE_ASSIGNMENT_ACCEPTABLE
    assert "默认可接货" in row["reason"]


def test_fee_gate_keeps_covered_call_event_loss_as_called_away_hold() -> None:
    from domain.domain.close_advice import HOLD_REASON_TYPE_CALLED_AWAY_ACCEPTABLE
    from src.application.close_advice_runner import _apply_fee_profitability_gate

    row = _apply_fee_profitability_gate(
        {
            "option_type": "call",
            "tier": "strong",
            "tier_label": "强烈建议平仓",
            "exit_state": "risk_exit",
            "exit_reason_type": "risk_exit",
            "short_vol_thesis_status": "event_risk",
            "realized_if_close": -1.0,
            "data_quality_flags": "",
        }
    )

    assert row["tier"] == "none"
    assert row["exit_state"] == "hold"
    assert row["exit_reason_type"] == "hold"
    assert row["hold_reason_type"] == HOLD_REASON_TYPE_CALLED_AWAY_ACCEPTABLE
    assert "默认可被行权卖出正股" in row["reason"]
