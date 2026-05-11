from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable, cast

import pandas as pd


def _build_gateway(*, chain_rows: list[dict[str, object]], snapshot_handler):
    class _Gateway:
        def get_snapshot(self, codes):  # noqa: ANN001
            return snapshot_handler(list(codes))

        def get_option_chain(self, *, code, start=None, end=None, is_force_refresh=False):  # noqa: ANN001
            return pd.DataFrame(chain_rows)

        def get_option_expiration_dates(self, code):  # noqa: ANN001
            return pd.DataFrame([{"strike_time": "2026-06-19"}])

        def close(self) -> None:
            return None

    return _Gateway()


def _payload(value: dict[str, object]) -> dict[str, Any]:
    return cast(dict[str, Any], value)


def _chain_rows(count: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for idx in range(count):
        rows.append(
            {
                "code": f"US.NVDA.2026-06-19.P{100 + idx}",
                "strike_time": "2026-06-19",
                "strike_price": float(100 + idx),
                "option_type": "PUT",
                "lot_size": 100,
            }
        )
    return rows


def _setup_common(monkeypatch, tmp_path: Path, *, gateway) -> Callable[..., dict[str, object]]:
    import src.application.opend_symbol_fetching as mod

    monkeypatch.setattr(mod, "build_ready_futu_gateway", lambda **kwargs: gateway)
    monkeypatch.setattr(mod, "get_trading_date", lambda market: date(2026, 4, 28))
    monkeypatch.setattr(mod, "get_spot_opend", lambda gateway, code, **kwargs: 100.0)
    monkeypatch.setattr(mod, "retry_futu_gateway_call", lambda _name, fn, **kwargs: fn())
    return mod.fetch_symbol


def test_fallback_fills_missing_iv_when_batch_fails(monkeypatch, tmp_path: Path) -> None:
    chain_rows = _chain_rows(2)

    def _snapshot_handler(codes: list[str]):
        if len(codes) > 1:
            raise RuntimeError("main snapshot failed")
        code = codes[0]
        return pd.DataFrame(
            [
                {
                    "code": code,
                    "last_price": 1.0,
                    "bid_price": 0.9,
                    "ask_price": 1.1,
                    "option_implied_volatility": 25.0,
                    "option_delta": -0.2,
                    "option_contract_multiplier": 100,
                }
            ]
        )

    fetch_symbol = _setup_common(monkeypatch, tmp_path, gateway=_build_gateway(chain_rows=chain_rows, snapshot_handler=_snapshot_handler))

    payload = _payload(fetch_symbol(
        "NVDA",
        base_dir=tmp_path,
        chain_cache=False,
        snapshot_batch_size=200,
        snapshot_fallback_max_codes=100,
        snapshot_fallback_batch_size=1,
    ))

    assert len(payload["rows"]) == 2
    assert all(row["implied_volatility"] == 0.25 for row in payload["rows"])
    assert all(row["delta"] == -0.2 for row in payload["rows"])
    assert payload["meta"]["snapshot_fallback_filled"] == 2
    assert payload["meta"]["snapshot_fallback_failed"] == 0


def test_fallback_respects_max_codes_budget(monkeypatch, tmp_path: Path) -> None:
    chain_rows = _chain_rows(200)

    def _snapshot_handler(codes: list[str]):
        if len(codes) > 1:
            raise RuntimeError("main snapshot failed")
        code = codes[0]
        return pd.DataFrame(
            [{"code": code, "last_price": 1.0, "option_implied_volatility": 20.0, "option_delta": -0.1, "option_contract_multiplier": 100}]
        )

    fetch_symbol = _setup_common(monkeypatch, tmp_path, gateway=_build_gateway(chain_rows=chain_rows, snapshot_handler=_snapshot_handler))

    payload = _payload(fetch_symbol(
        "NVDA",
        base_dir=tmp_path,
        chain_cache=False,
        snapshot_batch_size=200,
        snapshot_fallback_max_codes=50,
        snapshot_fallback_batch_size=1,
    ))

    filled = [row for row in payload["rows"] if row["implied_volatility"] is not None]
    assert len(filled) == 50
    errors = payload["meta"]["snapshot_errors"]
    assert any(item["error_code"] == "FALLBACK_BUDGET_EXCEEDED" for item in errors)
    assert payload["meta"]["snapshot_fallback_filled"] == 50
    assert payload["meta"]["snapshot_fallback_failed"] == 150


def test_fallback_failure_recorded_not_raised(monkeypatch, tmp_path: Path) -> None:
    chain_rows = _chain_rows(2)

    def _snapshot_handler(codes: list[str]):
        raise RuntimeError(f"snapshot failed for {len(codes)}")

    fetch_symbol = _setup_common(monkeypatch, tmp_path, gateway=_build_gateway(chain_rows=chain_rows, snapshot_handler=_snapshot_handler))

    payload = _payload(fetch_symbol(
        "NVDA",
        base_dir=tmp_path,
        chain_cache=False,
        snapshot_batch_size=200,
        snapshot_fallback_max_codes=100,
        snapshot_fallback_batch_size=1,
    ))

    assert payload["meta"]["status"] == "error"
    assert any(item["error_code"] == "FALLBACK_FAILED" for item in payload["meta"]["snapshot_errors"])
    assert payload["meta"]["snapshot_fallback_filled"] == 0
    assert payload["meta"]["snapshot_fallback_failed"] == 2


def test_fallback_disabled_when_max_codes_zero(monkeypatch, tmp_path: Path) -> None:
    chain_rows = _chain_rows(2)
    calls: list[list[str]] = []

    def _snapshot_handler(codes: list[str]):
        calls.append(list(codes))
        raise RuntimeError("main snapshot failed")

    fetch_symbol = _setup_common(monkeypatch, tmp_path, gateway=_build_gateway(chain_rows=chain_rows, snapshot_handler=_snapshot_handler))

    payload = _payload(fetch_symbol(
        "NVDA",
        base_dir=tmp_path,
        chain_cache=False,
        snapshot_batch_size=200,
        snapshot_fallback_max_codes=0,
        snapshot_fallback_batch_size=1,
    ))

    assert len(calls) == 1
    assert payload["meta"]["snapshot_fallback_filled"] == 0
    assert payload["meta"]["snapshot_fallback_failed"] == 0
    assert not any(item["error_code"] == "FALLBACK_FAILED" for item in payload["meta"]["snapshot_errors"])


def test_fallback_uses_same_rate_limiter(monkeypatch, tmp_path: Path) -> None:
    chain_rows = _chain_rows(3)
    limiter_calls: list[tuple[str, int]] = []

    def _snapshot_handler(codes: list[str]):
        if len(codes) > 1:
            raise RuntimeError("main snapshot failed")
        code = codes[0]
        return pd.DataFrame(
            [{"code": code, "last_price": 1.0, "option_implied_volatility": 20.0, "option_delta": -0.1, "option_contract_multiplier": 100}]
        )

    fetch_symbol = _setup_common(monkeypatch, tmp_path, gateway=_build_gateway(chain_rows=chain_rows, snapshot_handler=_snapshot_handler))

    import src.application.opend_symbol_fetching as mod

    def _fake_rate_limited_opend_call(**kwargs):  # type: ignore[no-untyped-def]
        limiter_calls.append((str(kwargs["endpoint"]), len(kwargs["call"].__closure__[0].cell_contents)))
        return kwargs["call"]()

    monkeypatch.setattr(mod, "rate_limited_opend_call", _fake_rate_limited_opend_call)

    payload = _payload(fetch_symbol(
        "NVDA",
        base_dir=tmp_path,
        chain_cache=False,
        snapshot_batch_size=200,
        snapshot_fallback_max_codes=3,
        snapshot_fallback_batch_size=1,
    ))

    assert payload["meta"]["snapshot_fallback_filled"] == 3
    assert limiter_calls[0][0] == "market_snapshot"
    assert all(endpoint == "market_snapshot" for endpoint, _ in limiter_calls)
    assert len(limiter_calls) == 4
