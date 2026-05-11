from __future__ import annotations

from pathlib import Path

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
        {"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}},
        {"symbol": "MSFT", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}},
        {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}},
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


def test_prefetch_required_data_inprocess_reuses_gateways_per_endpoint(tmp_path: Path, monkeypatch) -> None:
    watchlist = [
        {"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}},
        {"symbol": "MSFT", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 22222, "limit_expirations": 8}},
        {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}},
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
