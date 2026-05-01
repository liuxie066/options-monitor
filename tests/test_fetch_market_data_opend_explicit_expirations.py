from __future__ import annotations

from datetime import date
from pathlib import Path


def test_fetch_symbol_explicit_expirations_override_limit_and_cache(monkeypatch, tmp_path: Path) -> None:
    import scripts.fetch_market_data_opend as mod

    requested_chain_dates: list[str] = []

    class _Gateway:
        def get_snapshot(self, codes):  # noqa: ANN001
            import pandas as pd

            rows = []
            for code in codes:
                if str(code).startswith("US."):
                    rows.append({"code": code, "last_price": 100.0})
                else:
                    rows.append(
                        {
                            "code": code,
                            "last_price": 1.0,
                            "bid_price": 0.9,
                            "ask_price": 1.1,
                            "option_contract_multiplier": 100,
                        }
                    )
            return pd.DataFrame(rows)

        def get_option_expiration_dates(self, code):  # noqa: ANN001
            import pandas as pd

            return pd.DataFrame([{"strike_time": "2026-04-29"}, {"strike_time": "2026-06-29"}])

        def get_option_chain(self, *, code, start=None, end=None, is_force_refresh=False):  # noqa: ANN001
            import pandas as pd

            requested_chain_dates.append(str(start))
            return pd.DataFrame(
                [
                    {
                        "code": f"{code}.{start}.P135",
                        "strike_time": str(start),
                        "strike_price": 135.0,
                        "option_type": "PUT",
                        "lot_size": 100,
                    },
                    {
                        "code": f"{code}.{start}.C200",
                        "strike_time": str(start),
                        "strike_price": 200.0,
                        "option_type": "CALL",
                        "lot_size": 100,
                    },
                ]
            )

    monkeypatch.setattr(mod, "build_ready_futu_gateway", lambda **kwargs: _Gateway())
    monkeypatch.setattr(mod, "retry_futu_gateway_call", lambda _name, fn, **kwargs: fn())
    monkeypatch.setattr(mod, "get_trading_date", lambda market: date(2026, 4, 28))
    monkeypatch.setattr(mod, "get_spot_opend", lambda gateway, code, **kwargs: 100.0)
    monkeypatch.setattr(
        mod,
        "_load_chain_cache",
        lambda path: {
            "asof_date": "2026-04-28",
            "underlier_code": "US.NVDA",
            "expirations_all": ["2026-05-28"],
            "rows": [{"strike_time": "2026-05-28", "code": "US.NVDA.2026-05-28.P135"}],
        },
    )

    payload = mod.fetch_symbol(
        "NVDA",
        limit_expirations=1,
        base_dir=tmp_path,
        explicit_expirations=["2026-04-29", "2026-06-29"],
        option_types="put,call",
        chain_cache=True,
    )

    expirations = sorted({str(row.get("expiration")) for row in (payload.get("rows") or [])})
    assert requested_chain_dates == ["2026-04-29", "2026-06-29"]
    assert expirations == ["2026-04-29", "2026-06-29"]


def test_fetch_symbol_normalizes_timestamp_explicit_expirations(monkeypatch, tmp_path: Path) -> None:
    import scripts.fetch_market_data_opend as mod

    requested_chain_dates: list[str] = []

    class _Gateway:
        def get_snapshot(self, codes):  # noqa: ANN001
            import pandas as pd

            rows = []
            for code in codes:
                if str(code).startswith("US."):
                    rows.append({"code": code, "last_price": 100.0})
                else:
                    rows.append(
                        {
                            "code": code,
                            "last_price": 1.0,
                            "bid_price": 0.9,
                            "ask_price": 1.1,
                            "option_contract_multiplier": 100,
                        }
                    )
            return pd.DataFrame(rows)

        def get_option_chain(self, *, code, start=None, end=None, is_force_refresh=False):  # noqa: ANN001
            import pandas as pd

            requested_chain_dates.append(str(start))
            return pd.DataFrame(
                [
                    {
                        "code": f"{code}.{start}.P120",
                        "strike_time": str(start),
                        "strike_price": 120.0,
                        "option_type": "PUT",
                        "lot_size": 100,
                    }
                ]
            )

    monkeypatch.setattr(mod, "build_ready_futu_gateway", lambda **kwargs: _Gateway())
    monkeypatch.setattr(mod, "retry_futu_gateway_call", lambda _name, fn, **kwargs: fn())
    monkeypatch.setattr(mod, "get_trading_date", lambda market: date(2026, 4, 28))
    monkeypatch.setattr(mod, "get_spot_opend", lambda gateway, code, **kwargs: 100.0)

    payload = mod.fetch_symbol(
        "FUTU",
        limit_expirations=1,
        base_dir=tmp_path,
        explicit_expirations=[1777420800, "1781740800000"],
        option_types="put",
        chain_cache=False,
    )

    expirations = sorted({str(row.get("expiration")) for row in (payload.get("rows") or [])})
    assert requested_chain_dates == ["2026-04-29", "2026-06-18"]
    assert expirations == ["2026-04-29", "2026-06-18"]


def test_list_option_expirations_uses_shared_endpoint_limiter(monkeypatch, tmp_path: Path) -> None:
    import scripts.fetch_market_data_opend as mod

    endpoints: list[tuple[str, int, float, float]] = []

    class _Gateway:
        def get_option_expiration_dates(self, code):  # noqa: ANN001
            import pandas as pd

            assert code == "US.NVDA"
            return pd.DataFrame([{"strike_time": "2026-04-29"}])

        def close(self):  # noqa: ANN201
            return None

    def _fake_rate_limited_call(**kwargs):  # type: ignore[no-untyped-def]
        endpoints.append(
            (
                kwargs["endpoint"],
                int(kwargs["max_calls"]),
                float(kwargs["window_sec"]),
                float(kwargs["max_wait_sec"]),
            )
        )
        return kwargs["call"]()

    monkeypatch.setattr(mod, "build_ready_futu_gateway", lambda **kwargs: _Gateway())
    monkeypatch.setattr(mod, "retry_futu_gateway_call", lambda _name, fn, **kwargs: fn())
    monkeypatch.setattr(mod, "rate_limited_opend_call", _fake_rate_limited_call)

    out = mod.list_option_expirations(
        "NVDA",
        base_dir=tmp_path,
        expiration_max_calls=7,
        expiration_window_sec=17,
        expiration_max_wait_sec=27,
    )

    assert out == ["2026-04-29"]
    assert endpoints == [("option_expiration", 7, 17.0, 27.0)]


def test_fetch_symbol_uses_shared_snapshot_limiter(monkeypatch, tmp_path: Path) -> None:
    import scripts.fetch_market_data_opend as mod

    endpoints: list[tuple[str, int, float, float]] = []

    class _Gateway:
        def get_snapshot(self, codes):  # noqa: ANN001
            import pandas as pd

            rows = []
            for code in codes:
                if str(code).startswith("US.NVDA."):
                    rows.append({"code": code, "last_price": 1.0, "bid_price": 0.9, "ask_price": 1.1})
                else:
                    rows.append({"code": code, "last_price": 100.0})
            return pd.DataFrame(rows)

        def get_option_chain(self, *, code, start=None, end=None, is_force_refresh=False):  # noqa: ANN001
            import pandas as pd

            return pd.DataFrame(
                [
                    {
                        "code": f"{code}.{start}.P135",
                        "strike_time": str(start),
                        "strike_price": 135.0,
                        "option_type": "PUT",
                        "lot_size": 100,
                    }
                ]
            )

    def _fake_rate_limited_call(**kwargs):  # type: ignore[no-untyped-def]
        endpoints.append(
            (
                kwargs["endpoint"],
                int(kwargs["max_calls"]),
                float(kwargs["window_sec"]),
                float(kwargs["max_wait_sec"]),
            )
        )
        return kwargs["call"]()

    monkeypatch.setattr(mod, "build_ready_futu_gateway", lambda **kwargs: _Gateway())
    monkeypatch.setattr(mod, "retry_futu_gateway_call", lambda _name, fn, **kwargs: fn())
    monkeypatch.setattr(mod, "rate_limited_opend_call", _fake_rate_limited_call)
    monkeypatch.setattr(mod, "get_trading_date", lambda market: date(2026, 4, 28))

    payload = mod.fetch_symbol(
        "NVDA",
        limit_expirations=1,
        base_dir=tmp_path,
        explicit_expirations=["2026-04-29"],
        option_types="put",
        chain_cache=False,
        snapshot_max_calls=8,
        snapshot_window_sec=18,
        snapshot_max_wait_sec=28,
    )

    assert len(payload.get("rows") or []) == 1
    assert ("market_snapshot", 8, 18.0, 28.0) in endpoints


def test_fetch_symbol_reports_underlier_snapshot_errors(monkeypatch, tmp_path: Path) -> None:
    import scripts.fetch_market_data_opend as mod

    class _Gateway:
        def get_snapshot(self, codes):  # noqa: ANN001
            import pandas as pd

            if codes == ["US.NVDA"]:
                raise RuntimeError("underlier snapshot unavailable")
            return pd.DataFrame(
                [
                    {
                        "code": str(codes[0]),
                        "last_price": 1.0,
                        "bid_price": 0.9,
                        "ask_price": 1.1,
                    }
                ]
            )

        def get_option_chain(self, *, code, start=None, end=None, is_force_refresh=False):  # noqa: ANN001
            import pandas as pd

            return pd.DataFrame(
                [
                    {
                        "code": f"{code}.{start}.P135",
                        "strike_time": str(start),
                        "strike_price": 135.0,
                        "option_type": "PUT",
                        "lot_size": 100,
                    }
                ]
            )

        def close(self):  # noqa: ANN201
            return None

    monkeypatch.setattr(mod, "build_ready_futu_gateway", lambda **kwargs: _Gateway())
    monkeypatch.setattr(mod, "retry_futu_gateway_call", lambda _name, fn, **kwargs: fn())
    monkeypatch.setattr(mod, "get_trading_date", lambda market: date(2026, 4, 28))

    payload = mod.fetch_symbol_request(
        mod.FetchSymbolRequest(
            symbol="NVDA",
            limit_expirations=1,
            base_dir=tmp_path,
            explicit_expirations=["2026-04-29"],
            option_types="put",
            chain_cache=False,
        )
    )

    meta = payload.get("meta") or {}
    assert len(payload.get("rows") or []) == 1
    assert meta["status"] == "ok"
    assert meta["error"] is None
    assert meta["spot_errors"][0]["stage"] == "underlier_snapshot"
    assert meta["spot_errors"][0]["error_code"] == "UNKNOWN"


def test_fetch_symbol_does_not_retry_legacy_spot_signature(monkeypatch, tmp_path: Path) -> None:
    import scripts.fetch_market_data_opend as mod

    calls: list[dict] = []

    class _Gateway:
        def close(self):  # noqa: ANN201
            return None

    def _get_spot(_gateway, _code, **kwargs):  # noqa: ANN001
        calls.append(dict(kwargs))
        raise TypeError("legacy spot signature is not supported")

    monkeypatch.setattr(mod, "build_ready_futu_gateway", lambda **kwargs: _Gateway())
    monkeypatch.setattr(mod, "get_spot_opend", _get_spot)

    payload = mod.fetch_symbol(
        "NVDA",
        limit_expirations=1,
        base_dir=tmp_path,
        explicit_expirations=["2026-04-29"],
        option_types="put",
        chain_cache=False,
        snapshot_max_calls=8,
        snapshot_window_sec=18,
        snapshot_max_wait_sec=28,
    )

    assert len(calls) == 1
    assert calls[0]["base_dir"] == tmp_path
    assert calls[0]["snapshot_max_calls"] == 8
    assert calls[0]["snapshot_window_sec"] == 18
    assert calls[0]["snapshot_max_wait_sec"] == 28
    assert "errors" in calls[0]
    meta = payload.get("meta") or {}
    assert meta["status"] == "error"
    assert "TypeError" in str(meta["error"])


def test_fetch_symbol_reports_snapshot_rate_limit_errors(monkeypatch, tmp_path: Path) -> None:
    import scripts.fetch_market_data_opend as mod

    class _Gateway:
        def get_snapshot(self, codes):  # noqa: ANN001
            import pandas as pd

            if all(str(code).startswith("US.NVDA.") for code in codes):
                raise RuntimeError("rate limit wait budget exceeded")
            return pd.DataFrame([{"code": "US.NVDA", "last_price": 100.0}])

        def get_option_chain(self, *, code, start=None, end=None, is_force_refresh=False):  # noqa: ANN001
            import pandas as pd

            return pd.DataFrame(
                [
                    {
                        "code": f"{code}.{start}.P135",
                        "strike_time": str(start),
                        "strike_price": 135.0,
                        "option_type": "PUT",
                        "lot_size": 100,
                    }
                ]
            )

        def close(self):  # noqa: ANN201
            return None

    monkeypatch.setattr(mod, "build_ready_futu_gateway", lambda **kwargs: _Gateway())
    monkeypatch.setattr(mod, "retry_futu_gateway_call", lambda _name, fn, **kwargs: fn())
    monkeypatch.setattr(mod, "get_trading_date", lambda market: date(2026, 4, 28))

    payload = mod.fetch_symbol(
        "NVDA",
        limit_expirations=1,
        base_dir=tmp_path,
        explicit_expirations=["2026-04-29"],
        option_types="put",
        chain_cache=False,
    )

    meta = payload.get("meta") or {}
    assert len(payload.get("rows") or []) == 1
    assert meta["status"] == "error"
    assert meta["error_code"] == "RATE_LIMIT"
    assert meta["snapshot_errors"][0]["stage"] == "market_snapshot"
