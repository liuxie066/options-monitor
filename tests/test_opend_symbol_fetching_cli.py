from __future__ import annotations

import json

import pytest

import importlib
from pathlib import Path
from typing import Any, cast


def _mod():
    return importlib.import_module("src.application.opend_symbol_fetching_cli")


def _request(value: object) -> Any:
    return cast(Any, value)


def test_cli_accepts_snapshot_batch_and_fallback_args(monkeypatch) -> None:
    mod = _mod()

    calls: list[object] = []

    monkeypatch.setattr(
        mod,
        "fetch_symbol_request",
        lambda request: calls.append(request) or {"symbol": request.symbol, "rows": [], "expiration_count": 0, "meta": {}},
    )
    monkeypatch.setattr(mod, "save_outputs", lambda *args, **kwargs: (Path("raw"), Path("csv")))
    monkeypatch.setattr(mod, "append_metrics_json", lambda *args, **kwargs: None)

    argv = [
        "prog",
        "--symbols",
        "NVDA",
        "--snapshot-batch-size",
        "17",
        "--snapshot-fallback-max-codes",
        "33",
        "--snapshot-fallback-batch-size",
        "7",
        "--quiet",
    ]
    monkeypatch.setattr("sys.argv", argv)

    mod.main()

    request = _request(calls[0])
    assert request.snapshot_batch_size == 17
    assert request.snapshot_fallback_max_codes == 33
    assert request.snapshot_fallback_batch_size == 7


def test_cli_forwards_explicit_trading_date_to_fetch_request(monkeypatch) -> None:
    mod = _mod()

    captured: list[object] = []

    def _fake_fetch_symbol_request(request):
        captured.append(request)
        return {
            "symbol": request.symbol,
            "rows": [],
            "expiration_count": 0,
            "meta": {},
        }

    monkeypatch.setattr(mod, "fetch_symbol_request", _fake_fetch_symbol_request)
    monkeypatch.setattr(
        mod,
        "save_outputs",
        lambda *args, **kwargs: (Path("raw"), Path("csv")),
    )
    monkeypatch.setattr(mod, "append_metrics_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "prog",
            "--symbols",
            "NVDA",
            "--explicit-expirations",
            "2026-08-07",
            "--trading-date",
            "2026-07-27",
            "--include-realized-volatility",
            "--quiet",
        ],
    )

    mod.main()

    request = _request(captured[0])
    assert request.explicit_expirations == ["2026-08-07"]
    assert request.trading_date == "2026-07-27"
    assert request.include_realized_volatility is True


@pytest.mark.parametrize(
    ("status", "last_price"),
    [("ready", 180.0), ("data_unavailable", None)],
)
def test_cli_forwards_frozen_underlier_observation_without_refetch(
    monkeypatch,
    status: str,
    last_price: float | None,
) -> None:
    mod = _mod()
    captured: list[object] = []
    observation = {
        "schema_version": "opening_underlier_observation.v1",
        "code": "US.NVDA",
        "market": "US",
        "last_price": last_price,
        "update_time": None,
        "observed_at_utc": None,
        "age_seconds": None,
        "market_state": None,
        "sec_status": None,
        "suspension": None,
        "status": status,
        "reason_code": None if status == "ready" else "snapshot_row_missing",
    }

    monkeypatch.setattr(
        mod,
        "fetch_symbol_request",
        lambda request: captured.append(request)
        or {
            "symbol": request.symbol,
            "rows": [],
            "expiration_count": 0,
            "meta": {},
        },
    )
    monkeypatch.setattr(
        mod,
        "save_outputs",
        lambda *args, **kwargs: (Path("raw"), Path("csv")),
    )
    monkeypatch.setattr(mod, "append_metrics_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "prog",
            "--symbols",
            "NVDA",
            "--underlier-observation-json",
            json.dumps(observation),
            "--quiet",
        ],
    )

    mod.main()

    request = _request(captured[0])
    assert request.underlier_observation == observation
    assert request.fetch_spot_if_missing is False


def test_cli_passes_snapshot_batch_and_fallback_args_to_fetch_symbol(monkeypatch) -> None:
    mod = _mod()

    captured: list[object] = []

    def _fake_fetch_symbol_request(request):
        captured.append(request)
        return {"symbol": request.symbol, "rows": [], "expiration_count": 0, "meta": {}}

    monkeypatch.setattr(mod, "fetch_symbol_request", _fake_fetch_symbol_request)
    monkeypatch.setattr(mod, "save_outputs", lambda *args, **kwargs: (Path("raw"), Path("csv")))
    monkeypatch.setattr(mod, "append_metrics_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "prog",
            "--symbols",
            "AAPL",
            "--snapshot-batch-size",
            "9",
            "--snapshot-fallback-max-codes",
            "12",
            "--snapshot-fallback-batch-size",
            "3",
            "--quiet",
        ],
    )

    mod.main()

    request = _request(captured[0])
    assert request.snapshot_batch_size == 9
    assert request.snapshot_fallback_max_codes == 12
    assert request.snapshot_fallback_batch_size == 3


def test_cli_uses_defaults_when_args_absent(monkeypatch) -> None:
    mod = _mod()

    captured: list[object] = []

    def _fake_fetch_symbol_request(request):
        captured.append(request)
        return {"symbol": request.symbol, "rows": [], "expiration_count": 0, "meta": {}}

    monkeypatch.setattr(mod, "fetch_symbol_request", _fake_fetch_symbol_request)
    monkeypatch.setattr(mod, "save_outputs", lambda *args, **kwargs: (Path("raw"), Path("csv")))
    monkeypatch.setattr(mod, "append_metrics_json", lambda *args, **kwargs: None)
    monkeypatch.setattr("sys.argv", ["prog", "--symbols", "MSFT", "--quiet"])

    mod.main()

    request = _request(captured[0])
    assert request.snapshot_batch_size == 200
    assert request.snapshot_fallback_max_codes == 100
    assert request.snapshot_fallback_batch_size == 20


def test_cli_uses_runtime_root_for_fetch_base_and_metrics(monkeypatch, tmp_path: Path) -> None:
    mod = _mod()

    runtime_root = tmp_path / "runtime"
    captured: dict[str, object] = {}

    def _fake_fetch_symbol_request(request):
        captured["request"] = request
        return {"symbol": request.symbol, "rows": [], "expiration_count": 0, "meta": {}}

    def _fake_append_metrics_json(path, payload, *args, **kwargs):
        captured["metrics_path"] = Path(path)
        captured["metrics_payload"] = payload

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setattr(mod, "fetch_symbol_request", _fake_fetch_symbol_request)
    monkeypatch.setattr(mod, "save_outputs", lambda *args, **kwargs: (Path("raw"), Path("csv")))
    monkeypatch.setattr(mod, "append_metrics_json", _fake_append_metrics_json)
    monkeypatch.setattr(mod, "prune_chain_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr("sys.argv", ["prog", "--symbols", "MSFT", "--chain-cache", "--quiet"])

    mod.main()

    request = _request(captured["request"])
    assert request.base_dir == runtime_root.resolve()
    assert captured["metrics_path"] == (runtime_root / "output_shared" / "state" / "opend_metrics.json").resolve()


def test_cli_normalizes_invalid_snapshot_batch_and_fallback_args(monkeypatch) -> None:
    mod = _mod()

    captured: list[object] = []

    def _fake_fetch_symbol_request(request):
        captured.append(request)
        return {"symbol": request.symbol, "rows": [], "expiration_count": 0, "meta": {}}

    monkeypatch.setattr(mod, "fetch_symbol_request", _fake_fetch_symbol_request)
    monkeypatch.setattr(mod, "save_outputs", lambda *args, **kwargs: (Path("raw"), Path("csv")))
    monkeypatch.setattr(mod, "append_metrics_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "prog",
            "--symbols",
            "TSLA",
            "--snapshot-batch-size",
            "-1",
            "--snapshot-fallback-max-codes",
            "-2",
            "--snapshot-fallback-batch-size",
            "0",
            "--quiet",
        ],
    )

    mod.main()

    request = _request(captured[0])
    assert request.snapshot_batch_size == 1
    assert request.snapshot_fallback_max_codes == 0
    assert request.snapshot_fallback_batch_size == 20


def test_cli_normalizes_zero_snapshot_batch_and_negative_fallback_batch(monkeypatch) -> None:
    mod = _mod()

    captured: list[object] = []

    def _fake_fetch_symbol_request(request):
        captured.append(request)
        return {"symbol": request.symbol, "rows": [], "expiration_count": 0, "meta": {}}

    monkeypatch.setattr(mod, "fetch_symbol_request", _fake_fetch_symbol_request)
    monkeypatch.setattr(mod, "save_outputs", lambda *args, **kwargs: (Path("raw"), Path("csv")))
    monkeypatch.setattr(mod, "append_metrics_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "prog",
            "--symbols",
            "AMD",
            "--snapshot-batch-size",
            "0",
            "--snapshot-fallback-batch-size",
            "-1",
            "--quiet",
        ],
    )

    mod.main()

    request = _request(captured[0])
    assert request.snapshot_batch_size == 1
    assert request.snapshot_fallback_max_codes == 100
    assert request.snapshot_fallback_batch_size == 20


def test_cli_exits_nonzero_when_fetch_payload_reports_error(monkeypatch) -> None:
    mod = _mod()

    saved: list[str] = []

    def _fake_fetch_symbol_request(request):
        return {
            "symbol": request.symbol,
            "rows": [],
            "expiration_count": 0,
            "meta": {"status": "error", "error_code": "RATE_LIMIT", "error": "rate limited"},
        }

    monkeypatch.setattr(mod, "fetch_symbol_request", _fake_fetch_symbol_request)
    monkeypatch.setattr(mod, "save_outputs", lambda _base, symbol, _payload, **_kwargs: saved.append(symbol) or (Path("raw"), Path("csv")))
    monkeypatch.setattr(mod, "append_metrics_json", lambda *args, **kwargs: None)
    monkeypatch.setattr("sys.argv", ["prog", "--symbols", "NVDA", "--quiet"])

    with pytest.raises(SystemExit) as _caught:
        mod.main()
    exc = _caught.value
    assert exc.code == 1

    assert saved == ["NVDA"]


def test_cli_processes_all_symbols_before_nonzero_exit(monkeypatch) -> None:
    mod = _mod()

    fetched: list[str] = []
    saved: list[str] = []

    def _fake_fetch_symbol_request(request):
        fetched.append(request.symbol)
        status = "error" if request.symbol == "NVDA" else "ok"
        return {
            "symbol": request.symbol,
            "rows": [],
            "expiration_count": 0,
            "meta": {"status": status},
        }

    monkeypatch.setattr(mod, "fetch_symbol_request", _fake_fetch_symbol_request)
    monkeypatch.setattr(mod, "save_outputs", lambda _base, symbol, _payload, **_kwargs: saved.append(symbol) or (Path("raw"), Path("csv")))
    monkeypatch.setattr(mod, "append_metrics_json", lambda *args, **kwargs: None)
    monkeypatch.setattr("sys.argv", ["prog", "--symbols", "NVDA", "AMD", "--quiet"])

    with pytest.raises(SystemExit) as _caught:
        mod.main()
    exc = _caught.value
    assert exc.code == 1

    assert fetched == ["NVDA", "AMD"]
    assert saved == ["NVDA", "AMD"]
