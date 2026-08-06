from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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
        run_combo_yield_scan_fn=lambda **kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield", "count": 0},
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
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: _scan_should_not_run(),
        run_combo_yield_scan_fn=lambda **kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield", "count": 0},
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


def test_frozen_symbol_consumer_skips_market_planning_and_multiplier_writes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.symbol_monitoring as mod

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        mod,
        "build_required_data_fetch_plan",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("frozen consumer must not plan market fetches")
        ),
    )

    def _multiplier_writer(**_kwargs):
        raise AssertionError("frozen consumer must not rewrite required data")

    deps = mod.SymbolMonitoringDependencies(
        build_converter_fn=lambda **_kwargs: object(),
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
        apply_multiplier_cache_fn=_multiplier_writer,
        ensure_required_data_fn=lambda **kwargs: (
            captured.update(kwargs)
            or {"snapshot_id": "snapshot-1", "receipt_relpath": "receipt.json"}
        ),
        run_sell_put_scan_fn=lambda **_kwargs: {
            "strategy": "sell_put",
            "candidate_count": 0,
        },
        empty_sell_put_summary_fn=lambda symbol, symbol_cfg: {
            "symbol": symbol,
            "strategy": "sell_put",
        },
        run_sell_call_scan_fn=lambda **_kwargs: {},
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {
            "symbol": symbol,
            "strategy": "sell_call",
        },
        run_combo_yield_scan_fn=lambda **_kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {
            "symbol": symbol,
            "strategy": "combo_yield",
        },
        materialize_empty_combo_yield_artifacts_fn=lambda **_kwargs: None,
    )

    out = mod.run_symbol_monitoring(
        inputs=mod.SymbolMonitoringInputs(
            py="python3",
            base=tmp_path,
            symbol_cfg={
                "symbol": "NVDA",
                "sell_put": {"enabled": True},
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
            is_scheduled=True,
            required_data_snapshot_manifest=tmp_path / "manifest.json",
            required_data_snapshot_run_id="run-1",
        ),
        deps=deps,
    )

    assert out[0]["candidate_count"] == 0
    assert captured["fetch_plan"] is None
    assert captured["required_data_snapshot_run_id"] == "run-1"


def test_frozen_symbol_failure_emits_typed_artifacts_and_capture_status(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.symbol_monitoring as mod
    from src.application.required_data_snapshot import (
        FrozenRequiredDataUnavailable,
    )

    report_dir = tmp_path / "reports"
    capture_statuses: list[dict] = []

    deps = mod.SymbolMonitoringDependencies(
        build_converter_fn=lambda **_kwargs: object(),
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
        apply_multiplier_cache_fn=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("frozen consumer must not rewrite required data")
        ),
        ensure_required_data_fn=lambda **_kwargs: (_ for _ in ()).throw(
            FrozenRequiredDataUnavailable(
                symbol="NVDA",
                reason="empty_chain",
                snapshot_id="snapshot-failed",
                receipt_relpath="quotes/receipt.json",
            )
        ),
        run_sell_put_scan_fn=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unavailable frozen symbol must not scan")
        ),
        empty_sell_put_summary_fn=lambda symbol, symbol_cfg: {
            "symbol": symbol,
            "strategy": "sell_put",
        },
        run_sell_call_scan_fn=lambda **_kwargs: {},
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {},
        run_combo_yield_scan_fn=lambda **_kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {},
        materialize_empty_combo_yield_artifacts_fn=lambda **_kwargs: None,
    )

    rows = mod.run_symbol_monitoring(
        inputs=mod.SymbolMonitoringInputs(
            py="python3",
            base=tmp_path,
            symbol_cfg={
                "symbol": "NVDA",
                "sell_put": {"enabled": True},
                "sell_call": {"enabled": False},
            },
            top_n=3,
            portfolio_ctx=None,
            usd_per_cny_exchange_rate=None,
            cny_per_hkd_exchange_rate=None,
            timeout_sec=10,
            required_data_dir=tmp_path / "required_data",
            report_dir=report_dir,
            state_dir=tmp_path / "state",
            is_scheduled=True,
            runtime_config={"portfolio": {"account": "lx"}},
            final_candidates_sink_fn=lambda _mode, _rows: None,
            position_advice_producer_run_id="run-1",
            candidate_capture_status_sink_fn=capture_statuses.append,
            required_data_snapshot_manifest=tmp_path / "manifest.json",
            required_data_snapshot_run_id="run-1",
        ),
        deps=deps,
    )

    assert rows == [
        {
            "symbol": "NVDA",
            "strategy": "sell_put",
            "candidate_count": 0,
            "note": "行情快照不可用",
        }
    ]
    assert capture_statuses == [
        {
            "symbol": "NVDA",
            "strategy_mode": "put",
            "status": "failed",
            "reason": "required_data_snapshot_unavailable",
            "quote_snapshot_id": "snapshot-failed",
            "quote_receipt_relpath": "quotes/receipt.json",
        }
    ]
    status = json.loads(
        (report_dir / "nvda_sell_put_scan_status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["status"] == "unavailable"
    assert status["snapshot_id"] == "snapshot-failed"
    assert status["receipt_relpath"] == "quotes/receipt.json"


@pytest.mark.parametrize(
    ("reason_code", "expected_status", "expected_capture_reason"),
    [
        ("no_expirations", "completed", "opening_candidates_captured"),
        ("market_closed", "unavailable", "market_closed"),
    ],
)
def test_frozen_success_empty_publishes_explicit_zero_status_evidence(
    tmp_path: Path,
    reason_code: str,
    expected_status: str,
    expected_capture_reason: str,
) -> None:
    import src.application.symbol_monitoring as mod

    report_dir = tmp_path / "reports"
    capture_statuses: list[dict[str, object]] = []

    def run_sell_put(**kwargs):
        kwargs["final_candidates_sink_fn"]("put", [])
        return {
            "symbol": "NVDA",
            "strategy": "sell_put",
            "candidate_count": 0,
        }

    deps = mod.SymbolMonitoringDependencies(
        build_converter_fn=lambda **_kwargs: object(),
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
        apply_multiplier_cache_fn=lambda **_kwargs: None,
        ensure_required_data_fn=lambda **_kwargs: {
            "snapshot_id": "snapshot-empty",
            "receipt_relpath": "quotes/empty/receipt.json",
            "source_outcome": "success_empty",
            "reason_code": reason_code,
        },
        run_sell_put_scan_fn=run_sell_put,
        empty_sell_put_summary_fn=lambda symbol, symbol_cfg: {},
        run_sell_call_scan_fn=lambda **_kwargs: {},
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {},
        run_combo_yield_scan_fn=lambda **_kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {},
        materialize_empty_combo_yield_artifacts_fn=lambda **_kwargs: None,
    )

    rows = mod.run_symbol_monitoring(
        inputs=mod.SymbolMonitoringInputs(
            py="python3",
            base=tmp_path,
            symbol_cfg={
                "symbol": "NVDA",
                "sell_put": {"enabled": True},
                "sell_call": {"enabled": False},
            },
            top_n=3,
            portfolio_ctx=None,
            usd_per_cny_exchange_rate=None,
            cny_per_hkd_exchange_rate=None,
            timeout_sec=10,
            required_data_dir=tmp_path / "required_data",
            report_dir=report_dir,
            state_dir=tmp_path / "state",
            is_scheduled=True,
            runtime_config={"portfolio": {"account": "lx"}},
            final_candidates_sink_fn=lambda _mode, _rows: None,
            position_advice_producer_run_id="run-1",
            candidate_capture_status_sink_fn=capture_statuses.append,
            required_data_snapshot_manifest=tmp_path / "manifest.json",
            required_data_snapshot_run_id="run-1",
        ),
        deps=deps,
    )

    assert rows[0]["candidate_count"] == 0
    status = json.loads(
        (
            report_dir
            / "nvda_sell_put_scan_status.json"
        ).read_text(encoding="utf-8")
    )
    assert status["status"] == expected_status
    if expected_status == "completed":
        assert status["candidate_count"] == 0
        assert status["source_outcome"] == "success_empty"
        assert status["reason_code"] == reason_code
    else:
        assert "candidate_count" not in status
        assert "source_outcome" not in status
        assert "reason_code" not in status
        assert status["reason"] == "market_closed"
    assert status["snapshot_id"] == "snapshot-empty"
    assert status["receipt_relpath"] == "quotes/empty/receipt.json"
    assert capture_statuses == [
        {
            "symbol": "NVDA",
            "strategy_mode": "put",
            "status": expected_status,
            "reason": expected_capture_reason,
            "quote_snapshot_id": "snapshot-empty",
            "quote_receipt_relpath": "quotes/empty/receipt.json",
        }
    ]


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
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call"},
        run_combo_yield_scan_fn=lambda **kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield", "count": 0},
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
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call"},
        run_combo_yield_scan_fn=lambda **kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield", "count": 0},
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
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call"},
        run_combo_yield_scan_fn=lambda **kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield", "count": 0},
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
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call"},
        run_combo_yield_scan_fn=lambda **kwargs: None,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield", "count": 0},
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
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call"},
        run_combo_yield_scan_fn=lambda **kwargs: captured_scan.update(kwargs) or {"strategy": "combo_yield"},
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield"},
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
        empty_sell_call_summary_fn=lambda symbol, symbol_cfg: {"strategy": "sell_call", "count": 0},
        run_combo_yield_scan_fn=combo_runner,
        empty_combo_yield_summary_fn=lambda symbol, symbol_cfg: {"strategy": "combo_yield", "count": 0},
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


def test_combo_yield_runs_after_sell_put_failure_without_touching_historical_put_artifacts(monkeypatch, tmp_path: Path) -> None:
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
    assert (report_dir / "nvda_sell_put_candidates.csv").read_text(encoding="utf-8") == "stale\n1\n"
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


def test_sell_call_failure_is_traced_without_touching_historical_call_artifacts(
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

    assert (report_dir / "nvda_sell_call_candidates.csv").read_text(encoding="utf-8") == "stale\n1\n"
    trace = (report_dir / "strategy_scan_failures.jsonl").read_text(encoding="utf-8")
    assert '"reason": "strategy_step_failed"' in trace
    assert '"strategy_family": "covered_call"' in trace
    assert [row["strategy"] for row in out] == ["sell_put", "combo_yield", "sell_call"]
    assert out[-1]["count"] == 0


def test_sell_call_not_applicable_does_not_touch_historical_call_artifacts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    stale_path = report_dir / "nvda_sell_call_candidates.csv"
    stale_path.write_text("stale\n1\n", encoding="utf-8")

    out, _required = _run_strategy_decoupling_case(
        monkeypatch,
        tmp_path,
        sell_put_enabled=False,
        sell_put_runner=lambda **kwargs: {"strategy": "sell_put", "count": 0},
        combo_runner=lambda **kwargs: {"strategy": "combo_yield", "count": 0},
        sell_call_enabled=False,
    )

    assert stale_path.read_text(encoding="utf-8") == "stale\n1\n"
    assert out[-1]["strategy"] == "sell_call"
