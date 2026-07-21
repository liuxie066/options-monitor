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
        materialize_empty_sell_call_artifacts_fn=lambda **kwargs: None,
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call"},
        run_combo_yield_scan_fn=lambda **kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield", "count": 0},
        materialize_empty_sell_put_artifacts_fn=lambda **kwargs: None,
        materialize_empty_combo_yield_artifacts_fn=lambda **kwargs: None,
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


def test_run_symbol_monitoring_fetch_only_skips_scans_after_required_data(monkeypatch, tmp_path: Path) -> None:
    import src.application.symbol_monitoring as mod

    captured_required_data: dict[str, object] = {}

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

    def _scan_should_not_run(**_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("scan should not run in fetch-only mode")

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
        ensure_required_data_fn=lambda **kwargs: captured_required_data.update(kwargs),
        run_sell_put_scan_fn=_scan_should_not_run,
        empty_sell_put_summary_fn=lambda symbol, symbol_cfg: _scan_should_not_run(),
        run_sell_call_scan_fn=_scan_should_not_run,
        materialize_empty_sell_call_artifacts_fn=lambda **kwargs: None,
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: _scan_should_not_run(),
        run_combo_yield_scan_fn=lambda **kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield", "count": 0},
        materialize_empty_sell_put_artifacts_fn=lambda **kwargs: None,
        materialize_empty_combo_yield_artifacts_fn=lambda **kwargs: None,
    )

    out = mod.run_symbol_monitoring(
        inputs=mod.SymbolMonitoringInputs(
            py="python3",
            base=tmp_path,
            symbol_cfg={
                "symbol": "NVDA",
                "fetch": {"host": "127.0.0.1", "port": 11111, "limit_expirations": 2},
                "sell_put": {"enabled": True, "min_dte": 10, "max_dte": 30},
                "sell_call": {"enabled": True, "min_dte": 10, "max_dte": 60},
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
            fetch_only=True,
        ),
        deps=deps,
    )

    assert out == []
    assert captured_required_data["want_put"] is True
    assert captured_required_data["want_call"] is True


def test_run_symbol_monitoring_uses_runtime_opend_fetch_config(monkeypatch, tmp_path: Path) -> None:
    import src.application.symbol_monitoring as mod

    captured_plan: dict[str, object] = {}
    captured_required_data: dict[str, object] = {}

    def _build_required_data_fetch_plan(**kwargs):  # type: ignore[no-untyped-def]
        captured_plan.update(kwargs)
        return {
            "symbol": kwargs["symbol"],
            "merged_specs": [],
            "side_plans": [],
            "to_debug_dict": lambda: {"ok": True},
        }

    monkeypatch.setattr(mod, "build_required_data_fetch_plan", _build_required_data_fetch_plan)

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
        ensure_required_data_fn=lambda **kwargs: captured_required_data.update(kwargs),
        run_sell_put_scan_fn=lambda **kwargs: {"strategy": "sell_put"},
        empty_sell_put_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_put"},
        run_sell_call_scan_fn=lambda **kwargs: {"strategy": "sell_call"},
        materialize_empty_sell_call_artifacts_fn=lambda **kwargs: None,
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call"},
        run_combo_yield_scan_fn=lambda **kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield", "count": 0},
        materialize_empty_sell_put_artifacts_fn=lambda **kwargs: None,
        materialize_empty_combo_yield_artifacts_fn=lambda **kwargs: None,
    )

    mod.run_symbol_monitoring(
        inputs=mod.SymbolMonitoringInputs(
            py="python3",
            base=tmp_path,
            symbol_cfg={
                "symbol": "0700.HK",
                "fetch": {"host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
                "sell_put": {"enabled": True, "min_strike": 420, "max_strike": 460},
                "sell_call": {"enabled": False},
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
            runtime_config={
                "runtime": {
                    "option_chain_fetch": {"max_calls": 13, "window_sec": 12, "max_wait_sec": 11},
                    "opend_rate_limits": {
                        "market_snapshot": {"max_calls": 23, "window_sec": 22, "max_wait_sec": 21},
                        "option_expiration": {"max_calls": 33, "window_sec": 32, "max_wait_sec": 31},
                    },
                }
            },
        ),
        deps=deps,
    )

    assert captured_plan["snapshot_max_wait_sec"] == 21
    assert captured_plan["snapshot_window_sec"] == 22
    assert captured_plan["snapshot_max_calls"] == 23
    assert captured_plan["expiration_max_wait_sec"] == 31
    assert captured_plan["expiration_window_sec"] == 32
    assert captured_plan["expiration_max_calls"] == 33
    assert captured_required_data["opend_fetch_config"] == {
        "max_wait_sec": 11,
        "option_chain_window_sec": 12,
        "option_chain_max_calls": 13,
        "snapshot_max_wait_sec": 21,
        "snapshot_window_sec": 22,
        "snapshot_max_calls": 23,
        "expiration_max_wait_sec": 31,
        "expiration_window_sec": 32,
        "expiration_max_calls": 33,
    }


def test_run_symbol_monitoring_lifts_sell_call_min_strike_to_avg_cost(monkeypatch, tmp_path: Path) -> None:
    import src.application.symbol_monitoring as mod

    captured_plan: dict[str, object] = {}
    captured_scan: dict[str, object] = {}

    def _build_required_data_fetch_plan(**kwargs):  # type: ignore[no-untyped-def]
        captured_plan.update(kwargs)
        return {
            "symbol": kwargs["symbol"],
            "merged_specs": [],
            "side_plans": [],
            "to_debug_dict": lambda: {"ok": True},
        }

    monkeypatch.setattr(mod, "build_required_data_fetch_plan", _build_required_data_fetch_plan)

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
                "stock": {"shares": 200, "avg_cost": 120.0},
            },
        )(),
        apply_multiplier_cache_fn=lambda **kwargs: None,
        ensure_required_data_fn=lambda **kwargs: None,
        run_sell_put_scan_fn=lambda **kwargs: {"strategy": "sell_put"},
        empty_sell_put_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_put"},
        run_sell_call_scan_fn=lambda **kwargs: captured_scan.update(kwargs) or {"strategy": "sell_call"},
        materialize_empty_sell_call_artifacts_fn=lambda **kwargs: None,
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call"},
        run_combo_yield_scan_fn=lambda **kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield", "count": 0},
        materialize_empty_sell_put_artifacts_fn=lambda **kwargs: None,
        materialize_empty_combo_yield_artifacts_fn=lambda **kwargs: None,
    )

    mod.run_symbol_monitoring(
        inputs=mod.SymbolMonitoringInputs(
            py="python3",
            base=tmp_path,
            symbol_cfg={
                "symbol": "AAPL",
                "fetch": {"host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
                "sell_put": {"enabled": False},
                "sell_call": {"enabled": True, "min_dte": 20, "max_dte": 60, "min_strike": 100, "min_strike_cost_multiplier": 1.02},
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

    assert captured_plan["sell_call_cfg"]["min_strike"] == 122.4
    assert captured_scan["cc"]["min_strike"] == 122.4


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
        materialize_empty_sell_call_artifacts_fn=lambda **kwargs: None,
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call"},
        run_combo_yield_scan_fn=lambda **kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield", "count": 0},
        materialize_empty_sell_put_artifacts_fn=lambda **kwargs: None,
        materialize_empty_combo_yield_artifacts_fn=lambda **kwargs: None,
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


def test_run_symbol_monitoring_fetches_calls_for_sell_put_yield_enhancement(monkeypatch, tmp_path: Path) -> None:
    import src.application.symbol_monitoring as mod

    captured_plan: dict[str, object] = {}
    captured_required_data: dict[str, object] = {}

    monkeypatch.setattr(
        mod,
        "build_required_data_fetch_plan",
        lambda **kwargs: captured_plan.update(kwargs) or {
            "symbol": kwargs["symbol"],
            "merged_specs": [],
            "side_plans": [],
            "to_debug_dict": lambda: {"ok": True},
        },
    )

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
        ensure_required_data_fn=lambda **kwargs: captured_required_data.update(kwargs),
        run_sell_put_scan_fn=lambda **kwargs: [{"strategy": "sell_put"}, {"strategy": "combo_yield"}],
        empty_sell_put_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_put"},
        run_sell_call_scan_fn=lambda **kwargs: {"strategy": "sell_call"},
        materialize_empty_sell_call_artifacts_fn=lambda **kwargs: None,
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call"},
        run_combo_yield_scan_fn=lambda **kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield", "count": 0},
        materialize_empty_sell_put_artifacts_fn=lambda **kwargs: None,
        materialize_empty_combo_yield_artifacts_fn=lambda **kwargs: None,
    )

    out = mod.run_symbol_monitoring(
        inputs=mod.SymbolMonitoringInputs(
            py="python3",
            base=tmp_path,
            symbol_cfg={
                "symbol": "NVDA",
                "fetch": {"host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
                "sell_put": {
                    "enabled": True,
                    "min_dte": 20,
                    "max_dte": 60,
                },
                "combo_yield": {"enabled": True},
                "sell_call": {"enabled": False},
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

    assert len(out) == 3
    assert [row["strategy"] for row in out] == ["sell_put", "combo_yield", "sell_call"]
    assert captured_plan["yield_enhancement_cfg"]["enabled"] is True
    assert captured_plan["yield_enhancement_cfg"]["objective"] == "premium_funded_long_call"
    assert captured_plan["yield_enhancement_cfg"]["output_mode"] == "separate"
    assert captured_required_data["want_put"] is True
    assert captured_required_data["want_call"] is True


def test_run_symbol_monitoring_keeps_yield_enhancement_market_put_scope_after_account_prefilter(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.symbol_monitoring as mod

    captured_plan: dict[str, object] = {}
    captured_required_data: dict[str, object] = {}
    captured_scan: dict[str, object] = {}

    monkeypatch.setattr(
        mod,
        "build_required_data_fetch_plan",
        lambda **kwargs: captured_plan.update(kwargs) or {
            "symbol": kwargs["symbol"],
            "merged_specs": [],
            "side_plans": [],
            "to_debug_dict": lambda: {"ok": True},
        },
    )

    def _apply_prefilters_fn(**kwargs):  # type: ignore[no-untyped-def]
        capped_sp = dict(kwargs["sp"])
        capped_sp["max_strike"] = 0
        return type(
            "Prefilters",
            (),
            {
                "want_put": False,
                "want_call": kwargs["want_call"],
                "sp": capped_sp,
                "cc": kwargs["cc"],
                "stock": None,
            },
        )()

    deps = mod.SymbolMonitoringDependencies(
        build_converter_fn=lambda **kwargs: object(),
        apply_prefilters_fn=_apply_prefilters_fn,
        apply_multiplier_cache_fn=lambda **kwargs: None,
        ensure_required_data_fn=lambda **kwargs: captured_required_data.update(kwargs),
        run_sell_put_scan_fn=lambda **kwargs: (_ for _ in ()).throw(AssertionError("sell_put recommendation should be prefiltered")),
        empty_sell_put_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_put"},
        run_sell_call_scan_fn=lambda **kwargs: {"strategy": "sell_call"},
        materialize_empty_sell_call_artifacts_fn=lambda **kwargs: None,
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call"},
        run_combo_yield_scan_fn=lambda **kwargs: captured_scan.update(kwargs) or {"strategy": "combo_yield"},
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield"},
        materialize_empty_sell_put_artifacts_fn=lambda **kwargs: None,
        materialize_empty_combo_yield_artifacts_fn=lambda **kwargs: None,
    )

    out = mod.run_symbol_monitoring(
        inputs=mod.SymbolMonitoringInputs(
            py="python3",
            base=tmp_path,
            symbol_cfg={
                "symbol": "9992.HK",
                "fetch": {"host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
                "sell_put": {
                    "enabled": True,
                    "min_dte": 20,
                    "max_dte": 60,
                    "min_strike": 10,
                    "max_strike": 50,
                },
                "combo_yield": {"enabled": True},
                "sell_call": {"enabled": False},
            },
            top_n=3,
            portfolio_ctx={"cash_by_currency": {"HKD": 0}},
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

    assert [row["strategy"] for row in out] == ["sell_put", "combo_yield", "sell_call"]
    assert captured_required_data["want_put"] is True
    assert captured_required_data["want_call"] is True
    assert captured_required_data["max_strike"] == 50.0
    assert captured_plan["want_put"] is True
    assert captured_plan["sell_put_cfg"]["max_strike"] == 50
    assert captured_scan["sell_put_cfg"]["max_strike"] == 50



def _run_strategy_decoupling_case(
    monkeypatch,
    tmp_path: Path,
    *,
    sell_put_enabled: bool,
    sell_put_runner,
    combo_runner,
    sell_call_enabled: bool = False,
    sell_call_runner=None,
):
    import src.application.symbol_monitoring as mod

    captured_required_data: dict[str, object] = {}
    monkeypatch.setattr(
        mod,
        "build_required_data_fetch_plan",
        lambda **kwargs: {
            "symbol": kwargs["symbol"],
            "merged_specs": [],
            "side_plans": [],
            "to_debug_dict": lambda: {"ok": True},
        },
    )
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
        ensure_required_data_fn=lambda **kwargs: captured_required_data.update(kwargs),
        run_sell_put_scan_fn=sell_put_runner,
        empty_sell_put_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_put", "count": 0},
        run_sell_call_scan_fn=(
            sell_call_runner
            if sell_call_runner is not None
            else lambda **kwargs: {"strategy": "sell_call"}
        ),
        materialize_empty_sell_call_artifacts_fn=lambda **kwargs: __import__(
            "src.application.sell_call_steps", fromlist=["materialize_empty_sell_call_artifacts"]
        ).materialize_empty_sell_call_artifacts(**kwargs),
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call", "count": 0},
        run_combo_yield_scan_fn=combo_runner,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield", "count": 0},
        materialize_empty_sell_put_artifacts_fn=lambda **kwargs: __import__(
            "src.application.sell_put_steps", fromlist=["materialize_empty_sell_put_artifacts"]
        ).materialize_empty_sell_put_artifacts(**kwargs),
        materialize_empty_combo_yield_artifacts_fn=lambda **kwargs: __import__(
            "src.application.combo_yield_steps", fromlist=["materialize_empty_combo_yield_artifacts"]
        ).materialize_empty_combo_yield_artifacts(**kwargs),
    )
    out = mod.run_symbol_monitoring(
        inputs=mod.SymbolMonitoringInputs(
            py="python3",
            base=tmp_path,
            symbol_cfg={
                "symbol": "NVDA",
                "fetch": {"host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
                "sell_put": {"enabled": sell_put_enabled, "min_dte": 20, "max_dte": 60},
                "combo_yield": {"enabled": True},
                "sell_call": {"enabled": sell_call_enabled},
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
    return out, captured_required_data


def test_combo_yield_runs_when_sell_put_is_disabled(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    out, required = _run_strategy_decoupling_case(
        monkeypatch,
        tmp_path,
        sell_put_enabled=False,
        sell_put_runner=lambda **kwargs: (_ for _ in ()).throw(AssertionError("sell_put must stay disabled")),
        combo_runner=lambda **kwargs: calls.append("combo") or {"strategy": "combo_yield", "count": 1},
    )

    assert calls == ["combo"]
    assert required["want_put"] is True
    assert required["want_call"] is True
    assert [row["strategy"] for row in out] == ["sell_put", "combo_yield", "sell_call"]


def test_combo_yield_runs_after_sell_put_failure_and_stale_put_artifacts_are_cleared(monkeypatch, tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "nvda_sell_put_candidates.csv").write_text("stale\n1\n", encoding="utf-8")
    calls: list[str] = []

    out, _required = _run_strategy_decoupling_case(
        monkeypatch,
        tmp_path,
        sell_put_enabled=True,
        sell_put_runner=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("sell put failed")),
        combo_runner=lambda **kwargs: calls.append("combo") or {"strategy": "combo_yield", "count": 1},
    )

    assert calls == ["combo"]
    assert (report_dir / "nvda_sell_put_candidates.csv").read_text(encoding="utf-8") == "\n"
    trace = (report_dir / "strategy_scan_failures.jsonl").read_text(encoding="utf-8")
    assert '"reason": "strategy_step_failed"' in trace
    assert '"strategy_family": "sell_put"' in trace
    assert [row["strategy"] for row in out] == ["sell_put", "combo_yield", "sell_call"]


def test_combo_yield_runs_when_sell_put_returns_no_candidates(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    out, _required = _run_strategy_decoupling_case(
        monkeypatch,
        tmp_path,
        sell_put_enabled=True,
        sell_put_runner=lambda **kwargs: {"strategy": "sell_put", "count": 0},
        combo_runner=lambda **kwargs: calls.append("combo") or {"strategy": "combo_yield", "count": 1},
    )

    assert calls == ["combo"]
    assert [row["count"] for row in out[:2]] == [0, 1]


def test_sell_put_result_survives_combo_yield_failure_and_stale_combo_artifacts_are_cleared(monkeypatch, tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "nvda_combo_yield_candidates.csv").write_text("stale\n1\n", encoding="utf-8")

    out, _required = _run_strategy_decoupling_case(
        monkeypatch,
        tmp_path,
        sell_put_enabled=True,
        sell_put_runner=lambda **kwargs: {"strategy": "sell_put", "count": 1},
        combo_runner=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("combo failed")),
    )

    assert (report_dir / "nvda_combo_yield_candidates.csv").read_text(encoding="utf-8") == "\n"
    trace = (report_dir / "strategy_scan_failures.jsonl").read_text(encoding="utf-8")
    assert '"reason": "strategy_step_failed"' in trace
    assert '"strategy_family": "combo_yield"' in trace
    assert [row["strategy"] for row in out] == ["sell_put", "combo_yield", "sell_call"]
    assert [row["count"] for row in out[:2]] == [1, 0]


def test_sell_call_failure_is_traced_and_stale_call_artifacts_are_cleared(
    monkeypatch,
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "nvda_sell_call_candidates.csv").write_text("stale\n1\n", encoding="utf-8")

    out, _required = _run_strategy_decoupling_case(
        monkeypatch,
        tmp_path,
        sell_put_enabled=True,
        sell_put_runner=lambda **kwargs: {"strategy": "sell_put", "count": 1},
        combo_runner=lambda **kwargs: {"strategy": "combo_yield", "count": 1},
        sell_call_enabled=True,
        sell_call_runner=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("call failed")),
    )

    assert (report_dir / "nvda_sell_call_candidates.csv").read_text(encoding="utf-8") == "\n"
    trace = (report_dir / "strategy_scan_failures.jsonl").read_text(encoding="utf-8")
    assert '"reason": "strategy_step_failed"' in trace
    assert '"strategy_family": "covered_call"' in trace
    assert [row["strategy"] for row in out] == ["sell_put", "combo_yield", "sell_call"]
    assert out[-1]["count"] == 0
