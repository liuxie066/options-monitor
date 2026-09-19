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


def _payload_from(request, *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """The canned per-symbol fetch payload the CLI consumes."""
    return {"symbol": request.symbol, "rows": [], "expiration_count": 0, "meta": meta or {}}


def _stub_cli(monkeypatch, argv: list[str], *, fetch=None, save_outputs=None) -> list[object]:
    """Install the fetch/save/metrics seams plus `sys.argv`; returns the captured requests."""
    mod = _mod()
    captured: list[object] = []

    def _default_fetch(request):
        captured.append(request)
        return _payload_from(request)

    monkeypatch.setattr(mod, "fetch_symbol_request", fetch or _default_fetch)
    monkeypatch.setattr(mod, "save_outputs", save_outputs or (lambda *args, **kwargs: (Path("raw"), Path("csv"))))
    monkeypatch.setattr(mod, "append_metrics_json", lambda *args, **kwargs: None)
    monkeypatch.setattr("sys.argv", ["prog", *argv])
    return captured


def test_cli_accepts_snapshot_batch_and_fallback_args(monkeypatch) -> None:
    captured = _stub_cli(monkeypatch, [
        "--symbols", "NVDA",
        "--snapshot-batch-size", "17",
        "--snapshot-fallback-max-codes", "33",
        "--snapshot-fallback-batch-size", "7",
        "--quiet",
    ])

    _mod().main()

    request = _request(captured[0])
    assert request.snapshot_batch_size == 17
    assert request.snapshot_fallback_max_codes == 33
    assert request.snapshot_fallback_batch_size == 7


def test_cli_forwards_explicit_trading_date_to_fetch_request(monkeypatch) -> None:
    captured = _stub_cli(monkeypatch, [
        "--symbols", "NVDA",
        "--explicit-expirations", "2026-08-07",
        "--trading-date", "2026-07-27",
        "--include-realized-volatility",
        "--quiet",
    ])

    _mod().main()

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
    captured = _stub_cli(monkeypatch, [
        "--symbols", "NVDA",
        "--underlier-observation-json", json.dumps(observation),
        "--quiet",
    ])

    _mod().main()

    request = _request(captured[0])
    assert request.underlier_observation == observation
    assert request.fetch_spot_if_missing is False


def test_cli_passes_snapshot_batch_and_fallback_args_to_fetch_symbol(monkeypatch) -> None:
    captured = _stub_cli(monkeypatch, [
        "--symbols", "AAPL",
        "--snapshot-batch-size", "9",
        "--snapshot-fallback-max-codes", "12",
        "--snapshot-fallback-batch-size", "3",
        "--quiet",
    ])

    _mod().main()

    request = _request(captured[0])
    assert request.snapshot_batch_size == 9
    assert request.snapshot_fallback_max_codes == 12
    assert request.snapshot_fallback_batch_size == 3


def test_cli_uses_defaults_when_args_absent(monkeypatch) -> None:
    captured = _stub_cli(monkeypatch, ["--symbols", "MSFT", "--quiet"])

    _mod().main()

    request = _request(captured[0])
    assert request.snapshot_batch_size == 200
    assert request.snapshot_fallback_max_codes == 100
    assert request.snapshot_fallback_batch_size == 20


def test_cli_uses_runtime_root_for_fetch_base_and_metrics(monkeypatch, tmp_path: Path) -> None:
    mod = _mod()
    runtime_root = tmp_path / "runtime"
    captured: dict[str, object] = {}

    def _fake_append_metrics_json(path, payload, *args, **kwargs):
        captured["metrics_path"] = Path(path)
        captured["metrics_payload"] = payload

    captured_requests = _stub_cli(monkeypatch, ["--symbols", "MSFT", "--chain-cache", "--quiet"])
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setattr(mod, "append_metrics_json", _fake_append_metrics_json)
    monkeypatch.setattr(mod, "prune_chain_cache", lambda *args, **kwargs: None)

    mod.main()

    request = _request(captured_requests[0])
    assert request.base_dir == runtime_root.resolve()
    assert captured["metrics_path"] == (runtime_root / "output_shared" / "state" / "opend_metrics.json").resolve()


def test_cli_normalizes_invalid_snapshot_batch_and_fallback_args(monkeypatch) -> None:
    captured = _stub_cli(monkeypatch, [
        "--symbols", "TSLA",
        "--snapshot-batch-size", "-1",
        "--snapshot-fallback-max-codes", "-2",
        "--snapshot-fallback-batch-size", "0",
        "--quiet",
    ])

    _mod().main()

    request = _request(captured[0])
    assert request.snapshot_batch_size == 1
    assert request.snapshot_fallback_max_codes == 0
    assert request.snapshot_fallback_batch_size == 20


def test_cli_normalizes_zero_snapshot_batch_and_negative_fallback_batch(monkeypatch) -> None:
    captured = _stub_cli(monkeypatch, [
        "--symbols", "AMD",
        "--snapshot-batch-size", "0",
        "--snapshot-fallback-batch-size", "-1",
        "--quiet",
    ])

    _mod().main()

    request = _request(captured[0])
    assert request.snapshot_batch_size == 1
    assert request.snapshot_fallback_max_codes == 100
    assert request.snapshot_fallback_batch_size == 20


def test_cli_exits_nonzero_when_fetch_payload_reports_error(monkeypatch) -> None:
    saved: list[str] = []

    def _fake_fetch_symbol_request(request):
        return _payload_from(request, meta={"status": "error", "error_code": "RATE_LIMIT", "error": "rate limited"})

    _stub_cli(
        monkeypatch,
        ["--symbols", "NVDA", "--quiet"],
        fetch=_fake_fetch_symbol_request,
        save_outputs=lambda _base, symbol, _payload, **_kwargs: saved.append(symbol) or (Path("raw"), Path("csv")),
    )

    with pytest.raises(SystemExit) as _caught:
        _mod().main()
    exc = _caught.value
    assert exc.code == 1

    assert saved == ["NVDA"]


def test_cli_processes_all_symbols_before_nonzero_exit(monkeypatch) -> None:
    fetched: list[str] = []
    saved: list[str] = []

    def _fake_fetch_symbol_request(request):
        fetched.append(request.symbol)
        status = "error" if request.symbol == "NVDA" else "ok"
        return _payload_from(request, meta={"status": status})

    _stub_cli(
        monkeypatch,
        ["--symbols", "NVDA", "AMD", "--quiet"],
        fetch=_fake_fetch_symbol_request,
        save_outputs=lambda _base, symbol, _payload, **_kwargs: saved.append(symbol) or (Path("raw"), Path("csv")),
    )

    with pytest.raises(SystemExit) as _caught:
        _mod().main()
    exc = _caught.value
    assert exc.code == 1

    assert fetched == ["NVDA", "AMD"]
    assert saved == ["NVDA", "AMD"]
