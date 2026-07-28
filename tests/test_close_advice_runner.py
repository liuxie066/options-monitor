from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
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
        business_date=date(2026, 4, 16),
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
                "record_id": "lot-nvda-1",
                "account": "lx",
                "broker": "富途",
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
    assert rows[0]["position_lot_id"] == "lot-nvda-1"
    assert rows[0]["strategy_exit_mode"] == "standard_short_option"


def test_run_close_advice_preserves_last_good_report_when_context_is_malformed(
    tmp_path: Path,
) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text("{not-json", encoding="utf-8")
    out_dir = tmp_path / "reports"
    out_dir.mkdir()
    csv_path = out_dir / "close_advice.csv"
    text_path = out_dir / "close_advice.txt"
    csv_path.write_text("last-known-good-csv", encoding="utf-8")
    text_path.write_text("last-known-good-text", encoding="utf-8")

    with pytest.raises(ValueError, match="missing or malformed"):
        run_close_advice(
            config={"close_advice": {"enabled": True}},
            context_path=ctx_path,
            required_data_root=tmp_path / "required_data",
            output_dir=out_dir,
            base_dir=Path.cwd(),
        )

    assert csv_path.read_text(encoding="utf-8") == "last-known-good-csv"
    assert text_path.read_text(encoding="utf-8") == "last-known-good-text"


def test_run_close_advice_rejects_unknown_quote_provenance_for_current_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "context_status": "available",
        "filters": {"account": "lx"},
        "open_positions_min": [
            {
                "record_id": "lot-nvda-current",
                "account": "lx",
                "broker": "富途",
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
        ],
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context), encoding="utf-8")
    parsed = tmp_path / "required_data" / "parsed"
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
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    result = run_close_advice(
        config={"close_advice": {"enabled": True}},
        context_path=ctx_path,
        required_data_root=tmp_path / "required_data",
        output_dir=tmp_path / "reports",
        base_dir=Path.cwd(),
        markets_to_run=["US"],
    )

    assert result["notify_rows"] == 0
    assert result["evaluable_rows"] == 0
    assert result["quote_freshness"]["enforced"] is True
    assert (
        result["quote_freshness"]["symbols"]["NVDA"]["reason"]
        == "quote_provenance_missing"
    )
    row = pd.read_csv(tmp_path / "reports" / "close_advice.csv").iloc[0]
    assert row["evaluation_status"] == "quote_unusable"


def test_run_close_advice_keeps_distinct_lot_ids_for_same_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.application.close_advice_runner import _close_trace_key

    _freeze_close_advice_business_today(monkeypatch)
    position = {
        "account": "lx",
        "broker": "富途",
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
    context = {
        "open_positions_min": [
            {**position, "record_id": "lot-nvda-a"},
            {**position, "record_id": "lot-nvda-b"},
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    parsed = tmp_path / "required_data" / "parsed"
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
        required_data_root=tmp_path / "required_data",
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(out_dir / "close_advice.csv").to_dict("records")
    assert result["rows"] == 2
    assert {row["position_lot_id"] for row in rows} == {"lot-nvda-a", "lot-nvda-b"}
    assert len({_close_trace_key(row) for row in rows}) == 2


def test_run_close_advice_uses_underwriting_config_for_short_vol_close_thesis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "broker": "富途",
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
                "broker": "富途",
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
                "broker": "富途",
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
                "broker": "富途",
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
                "broker": "富途",
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
    assert result["evaluation_gap_rows"] == 0
    assert rows[0]["strategy_profile"] == "short_vol"
    assert rows[0]["strategy_source"] == "position_yield_enhancement_mode"
    assert rows[0]["risk_model"] == "short_vol"
    assert rows[0]["exit_state"] == "profit_capture"
    assert rows[0]["close_action"] == "close_put_keep_call"
    assert rows[0]["short_vol_thesis_status"] == "not_evaluable"
    assert "short_vol_risk_data_missing" in str(rows[0]["data_quality_flags"])
    assert "rv" in rows[0]["short_vol_reason"]


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
                "broker": "富途",
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
                "broker": "富途",
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
                "broker": "富途",
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
                "broker": "富途",
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
                "broker": "富途",
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
                "broker": "富途",
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
    assert "买回 Put（Call 腿缺失）" in text


def test_run_close_advice_holds_yield_enhancement_put_when_threshold_not_met(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "broker": "富途",
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
                "broker": "富途",
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
    assert "卖出剩余 Call 止盈" in text
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
                "broker": "富途",
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
                "broker": "富途",
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
                        "broker": "富途",
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
                "broker": "富途",
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
                "broker": "富途",
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

    business_date = datetime(2026, 5, 1, tzinfo=timezone.utc).date()
    assert runner._calc_dte("2026-05-01", business_date=business_date) == 0
    assert runner._calc_dte("2026-05-02", business_date=business_date) == 1


@pytest.mark.parametrize("expiration", [None, "not-a-date"])
def test_close_advice_never_uses_quote_dte_for_unknown_lifecycle(expiration: str | None) -> None:
    from src.application import close_advice_runner as runner

    inp, _flags = runner._position_to_input(
        {
            "account": "lx",
            "symbol": "AAPL",
            "option_type": "put",
            "side": "short",
            "expiration": expiration,
            "strike": 100,
            "contracts_open": 1,
            "premium": 1.0,
            "multiplier": 100,
            "currency": "USD",
        },
        {"bid": 0.4, "ask": 0.5, "dte": 99},
        business_date=date(2026, 4, 16),
    )

    assert inp.dte is None


def test_run_close_advice_routes_non_active_lifecycle_before_quote_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.application import close_advice_runner as runner

    _freeze_close_advice_business_today(monkeypatch)
    positions = [
        {
            "record_id": "expiry-day",
            "account": "lx",
            "broker": "富途",
            "symbol": "AAPL",
            "option_type": "put",
            "side": "short",
            "contracts_open": 1,
            "currency": "USD",
            "strike": 100,
            "multiplier": 100,
            "premium": 1.0,
            "expiration": "2026-04-16",
        },
        {
            "record_id": "expired-open",
            "account": "lx",
            "broker": "富途",
            "symbol": "MSFT",
            "option_type": "call",
            "side": "short",
            "contracts_open": 1,
            "currency": "USD",
            "strike": 200,
            "multiplier": 100,
            "premium": 1.0,
            "expiration": "2026-04-15",
        },
        {
            "record_id": "unknown",
            "account": "lx",
            "broker": "富途",
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "contracts_open": 1,
            "currency": "USD",
            "strike": 100,
            "multiplier": 100,
            "premium": 1.0,
        },
    ]
    context_path = tmp_path / "option_positions_context.json"
    context_path.write_text(
        json.dumps({"open_positions_min": positions}, ensure_ascii=False),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    (required_root / "parsed").mkdir(parents=True)
    planned: dict[str, list[str]] = {}

    def fake_ensure(**kwargs: object) -> tuple[dict, dict, dict]:
        planned["required_data"] = [str(pos.get("record_id")) for pos in kwargs["positions"]]  # type: ignore[index,union-attr]
        return {}, {}, {"attempted_symbols": 0, "fetched_symbols": 0, "errors": 0}

    def fake_fetch(**kwargs: object) -> tuple[dict, dict]:
        planned["opend_fallback"] = [str(pos.get("record_id")) for pos in kwargs["positions"]]  # type: ignore[index,union-attr]
        return {}, {}

    def fake_event_merge(**kwargs: object) -> None:
        planned["event_enrichment"] = [str(pos.get("record_id")) for pos in kwargs["positions"]]  # type: ignore[index,union-attr]

    monkeypatch.setattr(runner, "_ensure_required_data_coverage_for_positions", fake_ensure)
    monkeypatch.setattr(runner, "_fetch_missing_quotes_via_opend", fake_fetch)
    monkeypatch.setattr(runner, "_merge_event_snapshot_for_short_vol_positions", fake_event_merge)

    output_dir = tmp_path / "reports"
    result = run_close_advice(
        config={"close_advice": {"enabled": True}},
        context_path=context_path,
        required_data_root=required_root,
        output_dir=output_dir,
        base_dir=Path.cwd(),
    )

    rows = pd.read_csv(output_dir / "close_advice.csv").set_index("position_lot_id")
    assert planned == {"required_data": [], "opend_fallback": [], "event_enrichment": []}
    assert rows.loc["expiry-day", "position_lifecycle_state"] == "expiry_day"
    assert rows.loc["expired-open", "position_lifecycle_state"] == "expired_open"
    assert rows.loc["unknown", "position_lifecycle_state"] == "unknown"
    assert set(rows["evaluation_status"]) == {"not_evaluable"}
    assert set(rows["tier"]) == {"not_evaluable"}
    assert set(rows["close_action"]) == {"not_evaluable"}
    assert rows.loc["expiry-day", "quote_status"] == "not_required"
    assert rows.loc["expired-open", "quote_status"] == "not_required"
    assert rows.loc["unknown", "quote_status"] == "not_evaluable"
    assert result["notify_rows"] == 0

    from src.application.agent_tools.analysis import _close_advice_snapshot_row
    from src.application.agent_tools.close_advice_read_impl import _public_row

    lifecycle_row = {"position_lifecycle_state": "expired_open"}
    assert _public_row(lifecycle_row)["position_lifecycle_state"] == "expired_open"
    assert _close_advice_snapshot_row(lifecycle_row)["position_lifecycle_state"] == "expired_open"


def test_run_close_advice_uses_one_business_date_for_active_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.application import close_advice_runner as runner

    calls = 0

    def business_today_once() -> date:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("business date provider called more than once")
        return date(2026, 4, 16)

    monkeypatch.setattr(runner, "expiration_business_today", business_today_once)
    context_path = tmp_path / "option_positions_context.json"
    context_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "record_id": "active-lot",
                        "account": "lx",
                        "broker": "富途",
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
            },
            ensure_ascii=False,
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
                "mid": 0.2,
                "bid": 0.19,
                "ask": 0.21,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)
    output_dir = tmp_path / "reports"

    run_close_advice(
        config={"close_advice": {"enabled": True, "quote_source": "required_data"}},
        context_path=context_path,
        required_data_root=required_root,
        output_dir=output_dir,
        base_dir=Path.cwd(),
    )

    row = pd.read_csv(output_dir / "close_advice.csv").iloc[0]
    assert calls == 1
    assert row["position_lifecycle_state"] == "active"
    assert row["dte"] == 29


def test_run_close_advice_records_missing_quote_but_does_not_notify(
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
                        "broker": "富途",
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
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "broker": "富途",
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
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "broker": "富途",
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
                        "broker": "富途",
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
                        "broker": "富途",
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
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "broker": "富途",
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
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "broker": "富途",
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
                        "broker": "富途",
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
                        "broker": "富途",
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
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "broker": "富途",
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
                        "broker": "富途",
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
    row = pd.read_csv(out_dir / "close_advice.csv").iloc[0].to_dict()
    assert row["estimated_pnl_if_close_net"] == pytest.approx(-0.6033)
    assert row["fee_calc_status"] == "schedule_estimate"
    assert row["fee_calc_basis"] == "futu_us_fixed_package_2026-07-22"
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
                        "broker": "富途",
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
    assert "$2.40" in text
    assert "$0.10" in text


def test_run_close_advice_fails_closed_when_fee_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "broker": "富途",
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
    assert "fee_evidence_unavailable" in csv_text
    row = pd.read_csv(out_dir / "close_advice.csv").iloc[0].to_dict()
    assert row["tier"] == "not_evaluable"
    assert row["close_action"] == "not_evaluable"
    assert row["evaluation_status"] == "not_evaluable"


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
                        "broker": "富途",
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
                        "broker": "富途",
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
                        "broker": "富途",
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
                        "broker": "富途",
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
                        "broker": "富途",
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
                        "broker": "富途",
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
                        "broker": "富途",
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
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "broker": "富途",
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


def test_run_close_advice_counts_spread_block_as_quote_issue(
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
                        "broker": "富途",
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
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "broker": "富途",
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
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "broker": "富途",
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
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "broker": "富途",
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
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "broker": "富途",
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
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "broker": "富途",
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
    _freeze_close_advice_business_today(monkeypatch)
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "broker": "富途",
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

    assert "# OM · 决策简报 · lx" in msg
    assert "平仓建议" in msg
    assert "结论｜Sell Put 0 · Covered Call 0" in msg


def test_compact_close_advice_renders_legacy_risk_exit_as_read_only_compatibility() -> None:
    from src.application.close_advice_runner import render_markdown_compact

    text = render_markdown_compact(
        [
            {
                "account": "lx",
                "broker": "富途",
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
                "broker": "富途",
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


def test_buy_to_close_fee_exposes_explicit_close_economics(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.application import close_advice_runner

    monkeypatch.setattr(close_advice_runner, "calc_futu_option_fee", lambda *_args, **_kwargs: 2.0)
    row = close_advice_runner._apply_buy_to_close_fee(
        {
            "broker": "富途",
            "position_side": "short",
            "currency": "USD",
            "close_mid": 0.20,
            "contracts_open": 1,
            "multiplier": 100,
            "remaining_premium": 20.0,
            "realized_if_close": 80.0,
        }
    )

    assert row["buy_to_close_fee"] == 2.0
    assert row["buy_to_close_cost"] == 22.0
    assert row["close_fee_to_remaining_premium"] == pytest.approx(0.10)
    assert row["estimated_pnl_if_close_gross"] == 80.0
    assert row["estimated_close_fee"] == 2.0
    assert row["fee_calc_status"] == "schedule_estimate"
    assert row["fee_calc_basis"] == "futu_us_fixed_package_2026-07-22"
    assert row["estimated_pnl_if_close_net"] == 78.0
    assert row["realized_if_close"] == 78.0


def test_sell_to_close_fee_exposes_net_proceeds_and_hk_upper_bound() -> None:
    from src.application import close_advice_runner

    row = close_advice_runner._apply_buy_to_close_fee(
        {
            "broker": "富途",
            "position_side": "long",
            "currency": "HKD",
            "close_mid": 1.0,
            "contracts_open": 1,
            "multiplier": 100,
            "estimated_pnl_if_close_gross": -20.0,
        }
    )

    assert row["sell_to_close_fee"] == 21.0
    assert row["net_close_proceeds"] == 79.0
    assert row["fee_calc_status"] == "conservative_estimate"
    assert row["fee_calc_basis"] == "futu_hk_tier1_upper_bound_2026-07-22"


@pytest.mark.parametrize(
    ("broker", "currency", "expected_status"),
    [
        (None, "USD", "unavailable"),
        ("other", "USD", "unsupported_broker"),
        ("富途", "CNY", "unsupported_currency"),
    ],
)
def test_close_fee_evidence_rejects_unsupported_inputs(
    broker: str | None,
    currency: str,
    expected_status: str,
) -> None:
    from src.application import close_advice_runner

    row = close_advice_runner._apply_buy_to_close_fee(
        {
            "broker": broker,
            "position_side": "short",
            "currency": currency,
            "close_mid": 0.2,
            "contracts_open": 1,
            "multiplier": 100,
            "estimated_pnl_if_close_gross": 80.0,
        }
    )

    assert row["fee_calc_status"] == expected_status
    assert row["estimated_close_fee"] is None
    assert "fee_calc_unavailable" in row["data_quality_flags"]


def test_close_economics_do_not_claim_net_pnl_when_fee_is_unavailable() -> None:
    from src.application import close_advice_runner

    row = close_advice_runner._apply_buy_to_close_fee(
        {
            "position_side": "short",
            "currency": "USD",
            "close_mid": 0.20,
            "contracts_open": 1,
            "multiplier": None,
            "estimated_pnl_if_close_gross": 80.0,
            "estimated_close_fee": None,
            "estimated_pnl_if_close_net": None,
            "realized_if_close": 80.0,
        }
    )

    assert row["estimated_pnl_if_close_gross"] == 80.0
    assert row["estimated_close_fee"] is None
    assert row["estimated_pnl_if_close_net"] is None
    assert "fee_calc_unavailable" in row["data_quality_flags"]


def test_close_calibration_uses_only_explicit_replacement_and_willingness() -> None:
    from src.application import close_advice_runner

    row = close_advice_runner._apply_close_calibration_context(
        {
            "position_side": "short",
            "option_type": "put",
            "evaluation_status": "priced",
            "tier": "none",
            "exit_state": "hold",
            "buy_to_close_cost": 22.0,
            "remaining_annualized_return": 0.05,
            "remaining_risk_status": "ok",
            "remaining_stress_loss": 100.0,
        },
        {
            "side": "short",
            "option_type": "put",
            "strategy_snapshot": {
                "replacement_annualized_return": 0.12,
                "replacement_source": "manual_candidate_review",
                "assignment_acceptable": False,
            },
        },
    )

    assert row["replacement_annualized_return"] == 0.12
    assert row["replacement_annualized_advantage"] == pytest.approx(0.07)
    assert row["replacement_source"] == "manual_candidate_review"
    assert row["continued_willingness"] is False
    assert row["continued_willingness_source"] == "strategy_snapshot"
    assert row["close_calibration_status"] == "review_required"
    assert row["close_calibration_missing"] is None
    assert close_advice_runner._apply_close_action_semantics(row)["close_action"] == "hold"

    partial = close_advice_runner._apply_close_calibration_context(
        {
            "position_side": "short",
            "option_type": "call",
            "evaluation_status": "priced",
            "buy_to_close_cost": 22.0,
            "remaining_risk_status": "ok",
        },
        {"side": "short", "option_type": "call"},
    )

    assert partial["continued_willingness"] is True
    assert partial["continued_willingness_source"] == "strategy_default"
    assert partial["close_calibration_status"] == "partial"
    assert partial["close_calibration_missing"] == "replacement_opportunity"

    from src.application.agent_tools.close_advice_read_impl import _public_row

    public = _public_row({**row, "combo_group_classification": "residual_call", "combo_group_action": "hold_residual_call"})
    assert public["continued_willingness"] is False
    assert public["combo_group_classification"] == "residual_call"
    assert public["combo_group_action"] == "hold_residual_call"

    fee_public = _public_row(
        {
            "account": "lx",
            "symbol": "NVDA",
            "estimated_pnl_if_close_gross": 80.0,
            "estimated_close_fee": 2.0,
            "fee_calc_status": "schedule_estimate",
            "fee_calc_basis": "futu_us_fixed_package_2026-07-22",
            "estimated_pnl_if_close_net": 78.0,
            "net_close_proceeds": 18.0,
        }
    )
    assert fee_public["fee_calc_status"] == "schedule_estimate"
    assert fee_public["estimated_pnl_if_close_net"] == 78.0
    assert fee_public["net_close_proceeds"] == 18.0


def _combo_advice_leg(
    *,
    option_type: str,
    contracts: int,
    group_id: str | None,
    premium: float,
    close_mid: float | None,
    realized_if_close: float | None = None,
    exit_state: str = "hold",
    quote_status: str = "priced",
    evaluation_status: str = "priced",
) -> dict[str, object]:
    is_put = option_type == "put"
    row: dict[str, object] = {
        "account": "lx",
        "broker": "富途",
        "symbol": "NVDA",
        "option_type": option_type,
        "position_side": "short" if is_put else "long",
        "contracts_open": contracts,
        "premium": premium,
        "close_mid": close_mid,
        "multiplier": 100,
        "realized_if_close": realized_if_close,
        "strategy": "combo_yield",
        "leg_role": "sell_put" if is_put else "enhancement_call",
        "strategy_group_id": group_id or "",
        "yield_enhancement_mode": "income_upside_enhancement",
        "exit_state": exit_state,
        "tier": "strong" if exit_state == "profit_capture" else "none",
        "quote_status": quote_status,
        "evaluation_status": evaluation_status,
    }
    return row


def _combo_position(
    *,
    option_type: str,
    contracts: int,
    group_id: str | None,
    expiration: str,
    record_id: str,
    side: str | None = None,
    leg_role: str | None = None,
) -> dict[str, object]:
    is_put = option_type == "put"
    return {
        "record_id": record_id,
        "account": "lx",
        "broker": "富途",
        "symbol": "NVDA",
        "option_type": option_type,
        "side": side or ("short" if is_put else "long"),
        "contracts": contracts,
        "contracts_open": contracts,
        "contracts_closed": 0,
        "expiration": expiration,
        "expiration_ymd": expiration,
        "strategy": "combo_yield",
        "leg_role": leg_role or ("sell_put" if is_put else "enhancement_call"),
        "strategy_group_id": group_id,
        "yield_enhancement_mode": "income_upside_enhancement",
        "strategy_snapshot": {"expiry_structure": "diagonal"},
    }


def _apply_combo_group_advice(
    rows: list[dict[str, object]],
    positions: list[dict[str, object]],
) -> None:
    from src.application import close_advice_runner

    for row in rows:
        close_advice_runner._apply_close_action_semantics(row)
    close_advice_runner._apply_yield_enhancement_combo_economics(rows, positions=positions)


def test_combo_group_advice_aggregates_multi_lot_quantities_and_economics() -> None:
    group_id = "combo_yield:lx:diagonal-multi-lot"
    rows = [
        _combo_advice_leg(
            option_type="put",
            contracts=1,
            group_id=group_id,
            premium=1.5,
            close_mid=0.7,
            realized_if_close=80.0,
            exit_state="profit_capture",
        ),
        _combo_advice_leg(
            option_type="put",
            contracts=1,
            group_id=group_id,
            premium=1.4,
            close_mid=0.7,
            realized_if_close=70.0,
            exit_state="profit_capture",
        ),
        _combo_advice_leg(
            option_type="call", contracts=1, group_id=group_id, premium=0.3, close_mid=0.5
        ),
        _combo_advice_leg(
            option_type="call", contracts=1, group_id=group_id, premium=0.4, close_mid=0.6
        ),
    ]
    positions = [
        _combo_position(
            option_type="put", contracts=1, group_id=group_id, expiration="2026-08-21", record_id="put-1"
        ),
        _combo_position(
            option_type="put", contracts=1, group_id=group_id, expiration="2026-08-21", record_id="put-2"
        ),
        _combo_position(
            option_type="call", contracts=1, group_id=group_id, expiration="2026-09-18", record_id="call-1"
        ),
        _combo_position(
            option_type="call", contracts=1, group_id=group_id, expiration="2026-09-18", record_id="call-2"
        ),
    ]

    _apply_combo_group_advice(rows, positions)

    for row in rows:
        assert row["combo_group_classification"] == "active_combo"
        assert row["combo_group_status"] == "evaluable"
        assert row["combo_put_contracts_open"] == 2
        assert row["combo_call_contracts_open"] == 2
    for put_row in rows[:2]:
        assert put_row["close_action"] == "close_put_keep_call"
        assert put_row["combo_call_cost"] == pytest.approx(70.0)
        assert put_row["combo_call_value_if_close"] == pytest.approx(110.0)
        assert put_row["combo_net_locked_if_close_put_keep_call"] == pytest.approx(80.0)
        assert put_row["combo_net_if_close_both"] == pytest.approx(190.0)
        assert put_row["optional_combo_action"] == "close_both_optional"


def test_combo_group_advice_fails_closed_on_quantity_mismatch_without_overwriting_legs() -> None:
    group_id = "combo_yield:lx:diagonal-mismatch"
    put_row = _combo_advice_leg(
        option_type="put",
        contracts=2,
        group_id=group_id,
        premium=1.5,
        close_mid=0.7,
        realized_if_close=160.0,
        exit_state="profit_capture",
    )
    call_row = _combo_advice_leg(
        option_type="call", contracts=1, group_id=group_id, premium=0.3, close_mid=0.5
    )

    _apply_combo_group_advice(
        [put_row, call_row],
        [
            _combo_position(
                option_type="put", contracts=2, group_id=group_id, expiration="2026-08-21", record_id="put"
            ),
            _combo_position(
                option_type="call", contracts=1, group_id=group_id, expiration="2026-09-18", record_id="call"
            ),
        ],
    )

    assert put_row["close_action"] == "close_put_keep_call"
    assert put_row["strategy_exit_mode"] == "yield_enhancement_put_leg"
    assert put_row["combo_group_classification"] == "review_required"
    assert put_row["combo_group_action"] is None
    assert put_row["combo_cost_basis_status"] == "review_required"
    assert "open_quantity_mismatch" in str(put_row["combo_group_issues"])
    assert put_row.get("combo_net_if_close_both") is None


def test_combo_group_advice_fails_closed_when_current_call_quote_is_missing() -> None:
    group_id = "combo_yield:lx:diagonal-missing-quote"
    put_row = _combo_advice_leg(
        option_type="put",
        contracts=1,
        group_id=group_id,
        premium=1.5,
        close_mid=0.7,
        realized_if_close=80.0,
        exit_state="profit_capture",
    )
    call_row = _combo_advice_leg(
        option_type="call",
        contracts=1,
        group_id=group_id,
        premium=0.3,
        close_mid=None,
        exit_state="not_evaluable",
        quote_status="quote_unusable",
        evaluation_status="quote_unusable",
    )

    _apply_combo_group_advice(
        [put_row, call_row],
        [
            _combo_position(
                option_type="put", contracts=1, group_id=group_id, expiration="2026-08-21", record_id="put"
            ),
            _combo_position(
                option_type="call", contracts=1, group_id=group_id, expiration="2026-09-18", record_id="call"
            ),
        ],
    )

    assert put_row["combo_group_classification"] == "review_required"
    assert put_row["combo_group_action"] is None
    assert put_row["combo_group_quote_status"] == "incomplete"
    assert "call_quote_unavailable" in str(put_row["combo_group_issues"])
    assert put_row.get("combo_call_value_if_close") is None


def test_combo_group_advice_labels_put_only_and_residual_call_without_false_pair_wording() -> None:
    from src.application import close_advice_runner

    put_group = "combo_yield:lx:put-only"
    put_row = _combo_advice_leg(
        option_type="put",
        contracts=1,
        group_id=put_group,
        premium=1.5,
        close_mid=0.7,
        realized_if_close=80.0,
        exit_state="profit_capture",
    )
    _apply_combo_group_advice(
        [put_row],
        [
            _combo_position(
                option_type="put", contracts=1, group_id=put_group, expiration="2026-08-21", record_id="put"
            )
        ],
    )

    assert put_row["close_action"] == "close_put_keep_call"
    assert put_row["combo_group_classification"] == "missing_call"
    assert put_row["combo_group_action"] == "close_put_unpaired"
    put_label = close_advice_runner._close_action_label(put_row)
    assert "Call 腿缺失" in put_label
    assert "保留收益增强 Call" not in put_label

    call_group = "combo_yield:lx:residual-call"
    call_row = _combo_advice_leg(
        option_type="call",
        contracts=1,
        group_id=call_group,
        premium=0.3,
        close_mid=0.8,
        exit_state="take_profit",
    )
    _apply_combo_group_advice(
        [call_row],
        [
            _combo_position(
                option_type="call", contracts=1, group_id=call_group, expiration="2026-09-18", record_id="call"
            )
        ],
    )

    assert call_row["close_action"] == "sell_call_take_profit"
    assert call_row["combo_group_classification"] == "residual_call"
    assert call_row["combo_group_action"] == "sell_residual_call_take_profit"
    call_label = close_advice_runner._close_action_label(call_row)
    assert "剩余 Call" in call_label
    assert "Put" not in call_label


def test_combo_group_advice_requires_group_identity_and_rejects_mixed_inventory() -> None:
    missing_id_row = _combo_advice_leg(
        option_type="call", contracts=1, group_id=None, premium=0.3, close_mid=0.8
    )
    _apply_combo_group_advice(
        [missing_id_row],
        [
            _combo_position(
                option_type="call", contracts=1, group_id=None, expiration="2026-09-18", record_id="call-missing-id"
            )
        ],
    )
    assert missing_id_row["combo_group_classification"] == "review_required"
    assert missing_id_row["combo_group_action"] is None
    assert "missing_group_identity" in str(missing_id_row["combo_group_issues"])

    group_id = "combo_yield:lx:mixed-inventory"
    put_row = _combo_advice_leg(
        option_type="put",
        contracts=1,
        group_id=group_id,
        premium=1.5,
        close_mid=0.7,
        realized_if_close=80.0,
        exit_state="profit_capture",
    )
    _apply_combo_group_advice(
        [put_row],
        [
            _combo_position(
                option_type="put", contracts=1, group_id=group_id, expiration="2026-08-21", record_id="put"
            ),
            _combo_position(
                option_type="call",
                contracts=1,
                group_id=group_id,
                expiration="2026-09-18",
                record_id="unsupported-short-call",
                side="short",
                leg_role="enhancement_call",
            ),
        ],
    )
    assert put_row["combo_group_classification"] == "review_required"
    assert put_row["combo_group_action"] is None
    assert "unsupported_option_leg" in str(put_row["combo_group_issues"])


def test_combo_group_advice_reconciles_both_authoritative_leg_actions() -> None:
    group_id = "combo_yield:lx:both-close"
    put_row = _combo_advice_leg(
        option_type="put",
        contracts=1,
        group_id=group_id,
        premium=1.5,
        close_mid=0.7,
        realized_if_close=80.0,
        exit_state="profit_capture",
    )
    call_row = _combo_advice_leg(
        option_type="call",
        contracts=1,
        group_id=group_id,
        premium=0.3,
        close_mid=0.8,
        exit_state="take_profit",
    )
    _apply_combo_group_advice(
        [put_row, call_row],
        [
            _combo_position(
                option_type="put", contracts=1, group_id=group_id, expiration="2026-08-21", record_id="put"
            ),
            _combo_position(
                option_type="call", contracts=1, group_id=group_id, expiration="2026-09-18", record_id="call"
            ),
        ],
    )

    assert put_row["close_action"] == "close_put_keep_call"
    assert call_row["close_action"] == "sell_call_take_profit"
    assert put_row["combo_group_action"] == "close_both"
    assert call_row["combo_group_action"] == "close_both"
    assert put_row.get("optional_combo_action") is None


def test_combo_group_inventory_adapter_preserves_snapshot_only_diagonal_metadata() -> None:
    group_id = "combo_yield:lx:snapshot-only"
    put_row = _combo_advice_leg(
        option_type="put",
        contracts=1,
        group_id=group_id,
        premium=1.5,
        close_mid=0.7,
        realized_if_close=80.0,
        exit_state="profit_capture",
    )
    call_row = _combo_advice_leg(
        option_type="call", contracts=1, group_id=group_id, premium=0.3, close_mid=0.5
    )
    positions = [
        _combo_position(
            option_type="put", contracts=1, group_id=group_id, expiration="2026-08-21", record_id="put"
        ),
        _combo_position(
            option_type="call", contracts=1, group_id=group_id, expiration="2026-09-18", record_id="call"
        ),
    ]
    for position in positions:
        position["strategy_snapshot"] = {
            "strategy": position.pop("strategy"),
            "leg_role": position.pop("leg_role"),
            "strategy_group_id": position.pop("strategy_group_id"),
            "yield_enhancement_mode": position.pop("yield_enhancement_mode"),
            "expiry_structure": "diagonal",
        }

    _apply_combo_group_advice([put_row, call_row], positions)

    assert put_row["combo_group_classification"] == "active_combo"
    assert put_row["combo_group_status"] == "evaluable"
    assert put_row["combo_group_issues"] == ""


def test_combo_group_economics_remain_unknown_when_fee_calculation_is_unavailable() -> None:
    group_id = "combo_yield:lx:fee-unavailable"
    put_row = _combo_advice_leg(
        option_type="put",
        contracts=1,
        group_id=group_id,
        premium=1.5,
        close_mid=0.7,
        realized_if_close=80.0,
        exit_state="profit_capture",
    )
    call_row = _combo_advice_leg(
        option_type="call", contracts=1, group_id=group_id, premium=0.3, close_mid=0.5
    )
    call_row["data_quality_flags"] = "fee_calc_unavailable"
    _apply_combo_group_advice(
        [put_row, call_row],
        [
            _combo_position(
                option_type="put", contracts=1, group_id=group_id, expiration="2026-08-21", record_id="put"
            ),
            _combo_position(
                option_type="call", contracts=1, group_id=group_id, expiration="2026-09-18", record_id="call"
            ),
        ],
    )

    assert put_row["combo_group_status"] == "evaluable"
    assert put_row["combo_cost_basis_status"] == "fee_calc_unavailable"
    assert put_row.get("combo_net_locked_if_close_put_keep_call") is None
    assert put_row.get("combo_net_if_close_both") is None
    assert put_row.get("optional_combo_action") is None
