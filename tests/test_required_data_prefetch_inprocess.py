from __future__ import annotations

from pathlib import Path
import time

from src.application.multi_tick import required_data_prefetch as mod


class _Gateway:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 11111) -> None:
        self.host = host
        self.port = int(port)
        self.close_calls = 0

    def is_connected(self) -> bool:
        return True

    def close(self) -> None:
        self.close_calls += 1


def test_prefetch_required_data_inprocess_reuses_gateways(tmp_path: Path, monkeypatch) -> None:
    watchlist = [
        {"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 2}},
        {"symbol": "MSFT", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 2}},
        {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 2}},
    ]
    built: list[_Gateway] = []
    saved: list[str] = []
    appended: list[dict[str, object]] = []
    adapted: list[dict[str, object]] = []
    execute_calls: list[object] = []

    def fake_build_ready_futu_gateway(**kwargs):
        gw = _Gateway()
        built.append(gw)
        return gw

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        assert kwargs["gateway"] in built
        assert kwargs["snapshot_batch_size"] == 200
        return {
            "symbol": symbol,
            "rows": [{"symbol": symbol, "option_type": "put", "expiration": "2026-06-19", "strike": 100}],
            "meta": {"status": "ok", "error": "", "source": "opend"},
        }

    def fake_save_outputs(base: Path, symbol: str, payload: dict[str, object], *, output_root: Path | None = None):
        saved.append(symbol)
        return output_root, output_root

    def fake_adapt(payload: dict[str, object]) -> dict[str, object]:
        adapted.append(payload)
        return {"source_name": "opend", "payload": {"symbol": payload.get("symbol")}}

    def fake_append(base: Path, snapshot: dict[str, object]) -> None:
        appended.append(snapshot)

    def fail_execute(self, intent):
        execute_calls.append(intent)
        raise AssertionError("subprocess path should not run in inprocess mode")

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(mod, "save_outputs", fake_save_outputs)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", fake_adapt)
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", fake_append)
    monkeypatch.setattr(mod.ToolExecutionService, "execute", fail_execute)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 2}}},
        shared_required=tmp_path / "shared_required",
    )

    assert result["execution_mode"] == "inprocess"
    assert result["fetched_ok"] == 3
    assert len(saved) == 3
    assert len(adapted) == 3
    assert len(appended) == 3
    assert not execute_calls
    assert 1 <= len(built) <= 2
    assert all(gw.close_calls >= 1 for gw in built)
    assert (tmp_path / "output_shared" / "state" / "required_data_prefetch.lock").exists()


def test_prefetch_required_data_inprocess_reuses_gateways_per_endpoint(tmp_path: Path, monkeypatch) -> None:
    watchlist = [
        {"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 2}},
        {"symbol": "MSFT", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 22222, "limit_expirations": 2}},
        {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 2}},
    ]
    built: list[_Gateway] = []
    fetch_calls: list[dict[str, object]] = []

    def fake_build_ready_futu_gateway(**kwargs):
        gw = _Gateway(host=str(kwargs["host"]), port=int(kwargs["port"]))
        built.append(gw)
        return gw

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        gateway = kwargs["gateway"]
        assert isinstance(gateway, _Gateway)
        assert (gateway.host, gateway.port) == (kwargs["host"], kwargs["port"])
        fetch_calls.append({"symbol": symbol, "gateway": gateway, "port": kwargs["port"]})
        return {
            "symbol": symbol,
            "rows": [{"symbol": symbol, "option_type": "put", "expiration": "2026-06-19", "strike": 100}],
            "meta": {"status": "ok", "error": "", "source": "opend"},
        }

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(mod, "save_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess"}, "prefetch_max_workers": 1}},
        shared_required=tmp_path / "shared_required",
    )

    assert result["fetched_ok"] == 3
    assert [(gw.host, gw.port) for gw in built] == [("127.0.0.1", 11111), ("127.0.0.1", 22222)]
    assert fetch_calls[0]["gateway"] is fetch_calls[2]["gateway"]
    assert fetch_calls[1]["gateway"] is not fetch_calls[0]["gateway"]
    assert all(gw.close_calls >= 1 for gw in built)


def test_prefetch_required_data_subprocess_mode_preserves_existing_dispatch(tmp_path: Path, monkeypatch) -> None:
    watchlist = [
        {"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}},
        {"symbol": "MSFT", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}},
        {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}},
    ]
    execute_calls: list[object] = []

    def fake_execute(self, intent):
        execute_calls.append(intent)
        return {
            "schema_kind": "tool_execution",
            "schema_version": "1.0",
            "tool_name": "required_data_prefetch",
            "symbol": intent.symbol,
            "source": intent.source,
            "limit_exp": intent.limit_exp,
            "idempotency_key": f"k-{intent.symbol}",
            "status": "fetched",
            "ok": True,
            "message": "fetched",
            "returncode": 0,
            "started_at_utc": "2026-01-01T00:00:00+00:00",
            "finished_at_utc": "2026-01-01T00:00:01+00:00",
        }

    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod.ToolExecutionService, "execute", fake_execute)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "subprocess", "max_workers": 2}}},
        shared_required=tmp_path / "shared_required",
    )

    assert result["execution_mode"] == "subprocess"
    assert result["fetched_ok"] == 3
    assert len(execute_calls) == 3
    for intent in execute_calls:
        cmd = list(getattr(intent, "cmd"))
        assert "--snapshot-batch-size" in cmd
        assert "--snapshot-fallback-max-codes" in cmd
        assert "--snapshot-fallback-batch-size" in cmd


def test_prefetch_worker_count_defaults_to_two() -> None:
    assert mod._resolve_prefetch_max_workers({}) == 2


def test_prefetch_worker_count_reads_runtime_value() -> None:
    assert mod._resolve_prefetch_max_workers({"runtime": {"prefetch_max_workers": 3}}) == 3
    assert mod._resolve_prefetch_max_workers({"runtime": {"prefetch": {"max_workers": 4}}}) == 4
    assert mod._resolve_prefetch_max_workers({"prefetch": {"max_workers": 5}}) == 5


def test_prefetch_worker_count_prefers_flat_runtime_override() -> None:
    assert mod._resolve_prefetch_max_workers(
        {"runtime": {"prefetch_max_workers": 3, "prefetch": {"max_workers": 4}}, "prefetch": {"max_workers": 5}}
    ) == 3


def test_prefetch_worker_count_invalid_value_falls_back_to_default() -> None:
    assert mod._resolve_prefetch_max_workers({"runtime": {"prefetch_max_workers": "bad"}}) == 2
    assert mod._resolve_prefetch_max_workers({"runtime": {"prefetch_max_workers": 0}}) == 2
    assert mod._resolve_prefetch_max_workers({"prefetch": {"max_workers": -1}}) == 2


def test_strategy_prefetch_kwargs_uses_strategy_dte_and_strike_bounds() -> None:
    out = mod._strategy_prefetch_kwargs(
        {
            "symbol": "0700.HK",
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60, "max_strike": 450},
            "sell_call": {"enabled": True, "min_dte": 30, "max_dte": 90, "min_strike": 550},
            "yield_enhancement": {"enabled": True, "max_dte": 120},
        },
        enabled=True,
    )

    assert out["option_types"] == "put,call"
    assert out["min_dte"] == 20
    assert out["max_dte"] == 120
    assert out["side_strike_windows"]["put"]["min_strike"] == 360
    assert out["side_strike_windows"]["put"]["max_strike"] == 450
    assert out["side_strike_windows"]["call"]["min_strike"] == 550
    assert out["side_strike_windows"]["call"]["max_strike"] > 660


def test_inprocess_prefetch_passes_strategy_bounds_to_fetch_symbol(tmp_path: Path, monkeypatch) -> None:
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60, "max_strike": 450},
            "sell_call": {"enabled": False},
        }
    ]
    built: list[_Gateway] = []
    captured: dict[str, object] = {}

    def fake_build_ready_futu_gateway(**kwargs):
        gw = _Gateway()
        built.append(gw)
        return gw

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "symbol": symbol,
            "rows": [{"symbol": symbol, "option_type": "put", "expiration": "2026-06-19", "strike": 400}],
            "meta": {"status": "ok", "error": "", "source": "opend"},
        }

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(mod, "save_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=tmp_path / "shared_required",
    )

    assert captured["option_types"] == "put"
    assert captured["min_dte"] == 20
    assert captured["max_dte"] == 60
    assert captured["side_strike_windows"] == {"put": {"min_strike": 360.0, "max_strike": 450.0}}


def test_prefetch_dedupes_same_run_symbol_and_merges_strategy_bounds(tmp_path: Path, monkeypatch) -> None:
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 4},
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60, "max_strike": 450},
            "sell_call": {"enabled": False},
        },
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
            "sell_put": {"enabled": False},
            "sell_call": {"enabled": True, "min_dte": 30, "max_dte": 90, "min_strike": 550},
        },
    ]
    captured_calls: list[dict[str, object]] = []

    def fake_build_ready_futu_gateway(**kwargs):
        return _Gateway()

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        captured_calls.append({"symbol": symbol, **kwargs})
        return {
            "symbol": symbol,
            "expiration_count": 1,
            "rows": [{"symbol": symbol, "option_type": "put", "expiration": "2026-06-19", "strike": 400}],
            "meta": {
                "status": "ok",
                "error": "",
                "source": "opend",
                "expiration_opend_calls": 1,
                "expiration_cache_hits": 0,
                "opend_call_count": 2,
                "rate_gate_wait_sec": 0.5,
                "from_cache_expirations": [],
                "fetched_expirations": ["2026-06-19"],
                "snapshot_requested_codes": 12,
                "snapshot_opend_call_count": 1,
                "snapshots_rows": 12,
            },
        }

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(mod, "save_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 2}}},
        shared_required=tmp_path / "shared_required",
    )

    assert len(captured_calls) == 1
    captured = captured_calls[0]
    assert captured["symbol"] == "0700.HK"
    assert captured["limit_expirations"] == 8
    assert captured["option_types"] == "put,call"
    assert captured["min_dte"] == 20
    assert captured["max_dte"] == 90
    side_strike_windows = captured["side_strike_windows"]
    assert isinstance(side_strike_windows, dict)
    assert side_strike_windows["put"] == {"min_strike": 360.0, "max_strike": 450.0}
    assert side_strike_windows["call"]["min_strike"] == 550
    assert result["symbols_total"] == 2
    assert result["unique_symbols_total"] == 1
    assert result["deduped_count"] == 1
    assert result["to_fetch"] == 1
    assert result["fetched_ok"] == 1
    assert result["fetch_metrics"]["expiration_opend_calls"] == 1
    assert result["run_fetch_summary"]["opend_calls"]["total"] == 4
    assert result["run_fetch_summary"]["bottleneck"] == "option_chain_rate_gate"


def test_inprocess_prefetch_executes_budgeted_waves_with_safe_option_chain_limit(tmp_path: Path, monkeypatch) -> None:
    watchlist = [
        {"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 4}},
        {"symbol": "MSFT", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 4}},
        {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 4}},
    ]
    captured_calls: list[dict[str, object]] = []

    def fake_build_ready_futu_gateway(**kwargs):
        return _Gateway()

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        captured_calls.append({"symbol": symbol, **kwargs})
        return {
            "symbol": symbol,
            "expiration_count": 1,
            "rows": [{"symbol": symbol, "option_type": "put", "expiration": "2026-06-19", "strike": 100}],
            "meta": {"status": "ok", "error": "", "source": "opend", "opend_call_count": 1},
        }

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(mod, "save_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={
            "runtime": {
                "prefetch": {"execution_mode": "inprocess", "max_workers": 3},
                "opend_rate_limits": {"option_chain": {"max_calls": 10, "window_sec": 30, "max_wait_sec": 90}},
            }
        },
        shared_required=tmp_path / "shared_required",
    )

    assert len(captured_calls) == 3
    assert {call["option_chain_max_calls"] for call in captured_calls} == {8}
    assert result["effective_prefetch_workers"] == 2
    assert result["prefetch_budget_plan"]["safe_option_chain_calls_per_window"] == 8
    assert [wave["symbols"] for wave in result["prefetch_budget_plan"]["waves"]] == [["AAPL", "MSFT"], ["NVDA"]]


def test_inprocess_prefetch_waits_after_rate_limited_wave_before_next_wave(tmp_path: Path, monkeypatch) -> None:
    watchlist = [
        {"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 4}},
        {"symbol": "MSFT", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 4}},
        {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 4}},
    ]
    sleeps: list[float] = []

    def fake_build_ready_futu_gateway(**kwargs):
        return _Gateway()

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        meta: dict[str, object] = {"status": "ok", "error": "", "source": "opend", "opend_call_count": 1}
        if symbol == "AAPL":
            meta = {
                "status": "partial",
                "error_code": "RATE_LIMIT",
                "errors": [
                    {
                        "expiration": "2026-09-18",
                        "error_code": "RATE_LIMIT",
                        "message": "too frequent",
                    }
                ],
            }
        return {
            "symbol": symbol,
            "expiration_count": 1,
            "rows": [{"symbol": symbol, "option_type": "put", "expiration": "2026-06-19", "strike": 100}],
            "meta": meta,
        }

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(mod, "save_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "_sleep_after_rate_limit_wave", lambda wait_sec: sleeps.append(float(wait_sec)))

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={
            "runtime": {
                "prefetch": {"execution_mode": "inprocess", "max_workers": 3},
                "opend_rate_limits": {"option_chain": {"max_calls": 10, "window_sec": 30, "max_wait_sec": 90}},
            }
        },
        shared_required=tmp_path / "shared_required",
    )

    assert sleeps == [30.0]
    assert result["rate_limit_cooldowns"] == [
        {"after_wave": 1, "reason": "opend_rate_limit", "wait_sec": 30.0}
    ]
    assert result["opend_rate_limit_classes"] == ["US"]


def test_inprocess_prefetch_summary_records_partial_expiration_rate_limit_class(tmp_path: Path, monkeypatch) -> None:
    watchlist = [
        {
            "symbol": "3690.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
            "sell_call": {"enabled": True, "min_dte": 30, "max_dte": 180, "min_strike": 110},
        }
    ]

    def fake_build_ready_futu_gateway(**kwargs):
        return _Gateway()

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        return {
            "symbol": symbol,
            "expiration_count": 2,
            "rows": [{"symbol": symbol, "option_type": "call", "expiration": "2026-06-29", "strike": 110}],
            "meta": {
                "status": "partial",
                "error_code": "RATE_LIMIT",
                "error": (
                    "get_option_chain(2026-09-29) failed: "
                    "获取期权链频率太高，请求失败，每30秒最多10次。"
                ),
                "expiration_statuses": {"2026-06-29": "fetched", "2026-09-29": "error"},
                "errors": [
                    {
                        "expiration": "2026-09-29",
                        "error_code": "RATE_LIMIT",
                        "message": "获取期权链频率太高，请求失败，每30秒最多10次。",
                    }
                ],
            },
        }

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(mod, "save_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=tmp_path / "shared_required",
    )

    assert result["fetched_ok"] == 1
    assert result["errors"] == 0
    assert result["opend_rate_limit_classes"] == ["HK"]
    assert result["opend_rate_limit_items"] == [
        {
            "symbol": "3690.HK",
            "market": "HK",
            "expiration": "2026-09-29",
            "endpoint": "option_chain",
            "error_code": "RATE_LIMIT",
            "message": "获取期权链频率太高，请求失败，每30秒最多10次。",
        }
    ]


def test_prefetch_skips_cached_required_data_when_strategy_bounds_are_covered(tmp_path: Path, monkeypatch) -> None:
    shared_required = tmp_path / "shared_required"
    (shared_required / "raw").mkdir(parents=True)
    (shared_required / "parsed").mkdir(parents=True)
    (shared_required / "raw" / "0700.HK_required_data.json").write_text("{}\n", encoding="utf-8")
    (shared_required / "parsed" / "0700.HK_required_data.csv").write_text(
        "\n".join(
            [
                "symbol,option_type,expiration,dte,strike",
                "0700.HK,put,2026-06-19,30,360",
                "0700.HK,put,2026-06-19,30,400",
                "0700.HK,put,2026-06-19,30,450",
                "0700.HK,put,2026-07-17,60,360",
                "0700.HK,put,2026-07-17,60,400",
                "0700.HK,put,2026-07-17,60,450",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60, "max_strike": 450},
            "sell_call": {"enabled": False},
        }
    ]

    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "fetch_symbol", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cache should cover")))

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=shared_required,
    )

    assert result["symbols_total"] == 1
    assert result["cached"] == 1
    assert result["fetched"] == 0


def test_prefetch_cache_check_reads_required_data_csv_once(tmp_path: Path, monkeypatch) -> None:
    import pandas as pd

    shared_required = tmp_path / "shared_required"
    (shared_required / "raw").mkdir(parents=True)
    (shared_required / "parsed").mkdir(parents=True)
    (shared_required / "raw" / "0700.HK_required_data.json").write_text("{}\n", encoding="utf-8")
    parsed = shared_required / "parsed" / "0700.HK_required_data.csv"
    parsed.write_text("symbol,option_type,expiration,dte,strike\n", encoding="utf-8")
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60, "max_strike": 450},
            "sell_call": {"enabled": False},
        }
    ]
    read_calls: list[Path] = []

    def fake_safe_read_csv(path: Path):
        read_calls.append(path)
        return pd.DataFrame(
            [
                {"symbol": "0700.HK", "option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 360},
                {"symbol": "0700.HK", "option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 400},
                {"symbol": "0700.HK", "option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 450},
                {"symbol": "0700.HK", "option_type": "put", "expiration": "2026-07-17", "dte": 60, "strike": 360},
                {"symbol": "0700.HK", "option_type": "put", "expiration": "2026-07-17", "dte": 60, "strike": 400},
                {"symbol": "0700.HK", "option_type": "put", "expiration": "2026-07-17", "dte": 60, "strike": 450},
            ]
        )

    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "safe_read_csv", fake_safe_read_csv)
    monkeypatch.setattr(mod, "fetch_symbol", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cache should cover")))

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=shared_required,
    )

    assert result["cached"] == 1
    assert result["fetched"] == 0
    assert read_calls == [parsed]


def test_prefetch_refetches_when_cached_required_data_misses_strategy_side(tmp_path: Path, monkeypatch) -> None:
    shared_required = tmp_path / "shared_required"
    (shared_required / "raw").mkdir(parents=True)
    (shared_required / "parsed").mkdir(parents=True)
    (shared_required / "raw" / "0700.HK_required_data.json").write_text("{}\n", encoding="utf-8")
    (shared_required / "parsed" / "0700.HK_required_data.csv").write_text(
        "\n".join(
            [
                "symbol,option_type,expiration,dte,strike",
                "0700.HK,call,2026-06-19,30,560",
                "0700.HK,call,2026-06-19,30,600",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60, "max_strike": 450},
            "sell_call": {"enabled": False},
        }
    ]
    built: list[_Gateway] = []
    fetched: list[str] = []

    def fake_build_ready_futu_gateway(**kwargs):
        gw = _Gateway()
        built.append(gw)
        return gw

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        fetched.append(symbol)
        return {
            "symbol": symbol,
            "expiration_count": 1,
            "rows": [{"symbol": symbol, "option_type": "put", "expiration": "2026-06-19", "strike": 400}],
            "meta": {
                "status": "ok",
                "error": "",
                "source": "opend",
                "opend_call_count": 2,
                "rate_gate_wait_sec": 1.25,
                "from_cache_expirations": ["2026-06-19"],
                "fetched_expirations": ["2026-07-17"],
                "option_codes": 12,
                "snapshot_opend_call_count": 1,
                "snapshots_rows": 12,
            },
        }

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(mod, "save_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=shared_required,
    )

    assert fetched == ["0700.HK"]
    assert result["fetched_ok"] == 1
    assert result["fetch_metrics"]["option_chain_opend_calls"] == 2
    assert result["fetch_metrics"]["option_chain_rate_gate_wait_sec"] == 1.25
    assert result["fetch_metrics"]["snapshot_opend_calls"] == 1
    assert result["fetch_metrics"]["snapshot_requested_codes"] == 12


def test_inprocess_prefetch_summary_includes_symbol_duration(tmp_path: Path, monkeypatch) -> None:
    watchlist = [{"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}}]
    built: list[_Gateway] = []

    def fake_build_ready_futu_gateway(**kwargs):
        gw = _Gateway()
        built.append(gw)
        return gw

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        time.sleep(0.01)
        return {
            "symbol": symbol,
            "rows": [{"symbol": symbol, "option_type": "put", "expiration": "2026-06-19", "strike": 100}],
            "meta": {"status": "ok", "error": "", "source": "opend"},
        }

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(mod, "save_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess"}}},
        shared_required=tmp_path / "shared_required",
    )

    assert result["prefetch_max_workers"] == 2
    assert result["effective_prefetch_workers"] == 1
    assert result["submitted_count"] == 1
    assert result["completed_count"] == 1
    assert result["skipped_count"] == 0
    assert result["failed_count"] == 0
    assert result["symbols"][0]["symbol"] == "AAPL"
    assert result["symbols"][0]["execution_mode"] == "inprocess"
    assert result["symbols"][0]["duration_sec"] >= 0.0
    assert result["audit"][0]["duration_sec"] >= 0.0
    assert built and built[0].close_calls >= 1
