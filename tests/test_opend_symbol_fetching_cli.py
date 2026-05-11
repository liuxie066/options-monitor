from __future__ import annotations

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
