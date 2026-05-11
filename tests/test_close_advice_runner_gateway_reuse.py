from __future__ import annotations

from pathlib import Path

from src.application import close_advice_runner as mod


class _Gateway:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 11111) -> None:
        self.host = host
        self.port = int(port)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def _positions() -> list[dict[str, object]]:
    return [
        {"symbol": "AAPL", "option_type": "put", "expiration": "2026-06-19", "strike": 100},
        {"symbol": "MSFT", "option_type": "put", "expiration": "2026-06-19", "strike": 200},
        {"symbol": "NVDA", "option_type": "call", "expiration": "2026-06-19", "strike": 300},
    ]


def _config() -> dict[str, object]:
    return {
        "symbols": [
            {"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}},
            {"symbol": "MSFT", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}},
            {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}},
        ]
    }


def test_ensure_required_data_coverage_reuses_single_lazy_gateway(tmp_path: Path, monkeypatch) -> None:
    built: list[_Gateway] = []
    fetch_calls: list[dict[str, object]] = []

    def fake_build_ready_futu_gateway(**kwargs):
        gw = _Gateway()
        built.append(gw)
        return gw

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        fetch_calls.append({"symbol": symbol, **kwargs})
        return {
            "rows": [
                {
                    "symbol": symbol,
                    "option_type": kwargs["option_types"].split(",")[0],
                    "expiration": kwargs["explicit_expirations"][0],
                    "strike": kwargs["min_strike"],
                }
            ],
            "meta": {"status": "ok"},
        }

    monkeypatch.setattr(mod, "load_required_data_coverage", lambda *args, **kwargs: (set(), {}))
    monkeypatch.setattr(mod, "_load_required_data_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_merge_required_data_rows", lambda existing_rows, new_rows, *, base_dir: list(new_rows))
    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr("src.application.opend_symbol_outputs.save_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)

    _fetch_reasons, _fetch_details, summary = mod._ensure_required_data_coverage_for_positions(
        config=_config(),
        positions=_positions(),
        required_data_root=tmp_path / "required_data",
        base_dir=tmp_path,
    )

    assert summary["attempted_symbols"] == 3
    assert len(built) == 1
    assert built[0].close_calls == 1
    assert len(fetch_calls) == 3
    assert all(call["gateway"] is built[0] for call in fetch_calls)


def test_ensure_required_data_coverage_isolates_lazy_gateways_by_endpoint(tmp_path: Path, monkeypatch) -> None:
    cfg = {
        "symbols": [
            {"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}},
            {"symbol": "MSFT", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 22222, "limit_expirations": 8}},
            {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}},
        ]
    }
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
        fetch_calls.append({"symbol": symbol, **kwargs})
        return {
            "rows": [
                {
                    "symbol": symbol,
                    "option_type": kwargs["option_types"].split(",")[0],
                    "expiration": kwargs["explicit_expirations"][0],
                    "strike": kwargs["min_strike"],
                }
            ],
            "meta": {"status": "ok"},
        }

    monkeypatch.setattr(mod, "load_required_data_coverage", lambda *args, **kwargs: (set(), {}))
    monkeypatch.setattr(mod, "_load_required_data_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_merge_required_data_rows", lambda existing_rows, new_rows, *, base_dir: list(new_rows))
    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr("src.application.opend_symbol_outputs.save_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)

    _fetch_reasons, _fetch_details, summary = mod._ensure_required_data_coverage_for_positions(
        config=cfg,
        positions=_positions(),
        required_data_root=tmp_path / "required_data",
        base_dir=tmp_path,
    )

    assert summary["attempted_symbols"] == 3
    assert [(gw.host, gw.port) for gw in built] == [("127.0.0.1", 11111), ("127.0.0.1", 22222)]
    assert fetch_calls[0]["gateway"] is fetch_calls[2]["gateway"]
    assert fetch_calls[1]["gateway"] is not fetch_calls[0]["gateway"]
    assert all(gw.close_calls == 1 for gw in built)


def test_ensure_required_data_coverage_uses_external_gateway_without_closing(tmp_path: Path, monkeypatch) -> None:
    provided = _Gateway()
    fetch_calls: list[dict[str, object]] = []
    build_calls: list[dict[str, object]] = []

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        fetch_calls.append({"symbol": symbol, **kwargs})
        return {
            "rows": [
                {
                    "symbol": symbol,
                    "option_type": kwargs["option_types"].split(",")[0],
                    "expiration": kwargs["explicit_expirations"][0],
                    "strike": kwargs["min_strike"],
                }
            ],
            "meta": {"status": "ok"},
        }

    monkeypatch.setattr(mod, "load_required_data_coverage", lambda *args, **kwargs: (set(), {}))
    monkeypatch.setattr(mod, "_load_required_data_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_merge_required_data_rows", lambda existing_rows, new_rows, *, base_dir: list(new_rows))
    monkeypatch.setattr("src.application.opend_symbol_fetching.fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr("src.application.opend_symbol_outputs.save_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", lambda **kwargs: build_calls.append(kwargs))

    mod._ensure_required_data_coverage_for_positions(
        config=_config(),
        positions=_positions(),
        required_data_root=tmp_path / "required_data",
        base_dir=tmp_path,
        gateway=provided,
    )

    assert not build_calls
    assert provided.close_calls == 0
    assert len(fetch_calls) == 3
    assert all(call["gateway"] is provided for call in fetch_calls)
