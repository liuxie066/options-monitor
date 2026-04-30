from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_run_symbol_monitoring_passes_fetch_plan_to_required_data_step(monkeypatch, tmp_path: Path) -> None:
    import src.application.symbol_monitoring as mod

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        mod,
        "build_required_data_fetch_plan",
        lambda **kwargs: {
            "symbol": kwargs["symbol"],
            "merged_specs": ["spec"],
            "side_plans": [],
            "to_debug_dict": lambda: {"ok": True},
        },
    )

    def _ensure_required_data_fn(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)

    deps = mod.SymbolMonitoringDependencies(
        build_converter_fn=lambda **kwargs: object(),
        apply_prefilters_fn=lambda **kwargs: type(
            "Prefilters",
            (),
            {
                "want_put": kwargs["want_put"],
                "want_call": kwargs["want_call"],
                "sp": kwargs["sp"],
                "cc": kwargs["cc"],
                "stock": None,
            },
        )(),
        apply_multiplier_cache_fn=lambda **kwargs: None,
        ensure_required_data_fn=_ensure_required_data_fn,
        run_sell_put_scan_fn=lambda **kwargs: {"strategy": "sell_put"},
        empty_sell_put_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_put"},
        run_sell_call_scan_fn=lambda **kwargs: {"strategy": "sell_call"},
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call"},
    )

    out = mod.run_symbol_monitoring(
        inputs=mod.SymbolMonitoringInputs(
            py="python3",
            base=tmp_path,
            symbol_cfg={
                "symbol": "0700.HK",
                "fetch": {"host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
                "sell_put": {"enabled": True, "min_dte": 10, "max_dte": 30, "min_strike": 420, "max_strike": 460},
                "sell_call": {"enabled": True, "min_dte": 10, "max_dte": 60, "min_strike": 505},
            },
            top_n=3,
            portfolio_ctx=None,
            usd_per_cny_exchange_rate=None,
            cny_per_hkd_exchange_rate=None,
            timeout_sec=10,
            required_data_dir=tmp_path / "required_data",
            report_dir=tmp_path / "reports",
            state_dir=tmp_path / "state",
            is_scheduled=False,
        ),
        deps=deps,
    )

    assert len(out) == 2
    assert captured["fetch_plan"]["symbol"] == "0700.HK"
    assert captured["report_dir"] == tmp_path / "reports"


def test_run_symbol_monitoring_still_builds_plan_with_local_required_data(monkeypatch, tmp_path: Path) -> None:
    import src.application.symbol_monitoring as mod

    required_data_dir = tmp_path / "required_data"
    (required_data_dir / "parsed").mkdir(parents=True, exist_ok=True)
    (required_data_dir / "parsed" / "0700.HK_required_data.csv").write_text(
        "\n".join(
            [
                "symbol,option_type,expiration,dte,contract_symbol,strike,spot,bid,ask,last_price,mid,volume,open_interest,implied_volatility,in_the_money,currency,otm_pct,delta,multiplier",
                "0700.HK,put,2026-05-29,20,P1,420,470,1,1,1,1,1,1,0.2,,HKD,0.1,-0.2,100",
                "0700.HK,put,2026-05-29,20,P2,460,470,1,1,1,1,1,1,0.2,,HKD,0.02,-0.1,100",
                "0700.HK,call,2026-05-29,20,C1,505,470,1,1,1,1,1,1,0.2,,HKD,0.07,0.2,100",
                "0700.HK,call,2026-05-29,20,C2,560,470,1,1,1,1,1,1,0.2,,HKD,0.19,0.1,100",
            ]
        ),
        encoding="utf-8",
    )

    captured_plan_calls: list[dict[str, object]] = []

    def _build_required_data_fetch_plan(**kwargs):  # type: ignore[no-untyped-def]
        captured_plan_calls.append(kwargs)
        return {
            "symbol": kwargs["symbol"],
            "merged_specs": [],
            "side_plans": [],
            "to_debug_dict": lambda: {"ok": True},
        }

    monkeypatch.setattr(mod, "build_required_data_fetch_plan", _build_required_data_fetch_plan)

    captured: dict[str, object] = {}

    def _ensure_required_data_fn(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)

    deps = mod.SymbolMonitoringDependencies(
        build_converter_fn=lambda **kwargs: object(),
        apply_prefilters_fn=lambda **kwargs: type(
            "Prefilters",
            (),
            {
                "want_put": kwargs["want_put"],
                "want_call": kwargs["want_call"],
                "sp": kwargs["sp"],
                "cc": kwargs["cc"],
                "stock": None,
            },
        )(),
        apply_multiplier_cache_fn=lambda **kwargs: None,
        ensure_required_data_fn=_ensure_required_data_fn,
        run_sell_put_scan_fn=lambda **kwargs: {"strategy": "sell_put"},
        empty_sell_put_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_put"},
        run_sell_call_scan_fn=lambda **kwargs: {"strategy": "sell_call"},
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call"},
    )

    mod.run_symbol_monitoring(
        inputs=mod.SymbolMonitoringInputs(
            py="python3",
            base=tmp_path,
            symbol_cfg={
                "symbol": "0700.HK",
                "fetch": {"host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
                "sell_put": {"enabled": True, "min_dte": 10, "max_dte": 30, "min_strike": 420, "max_strike": 460},
                "sell_call": {"enabled": True, "min_dte": 10, "max_dte": 60, "min_strike": 505},
            },
            top_n=3,
            portfolio_ctx=None,
            usd_per_cny_exchange_rate=None,
            cny_per_hkd_exchange_rate=None,
            timeout_sec=10,
            required_data_dir=required_data_dir,
            report_dir=tmp_path / "reports",
            state_dir=tmp_path / "state",
            is_scheduled=False,
        ),
        deps=deps,
    )

    assert len(captured_plan_calls) == 1
    assert captured["fetch_plan"]["symbol"] == "0700.HK"
