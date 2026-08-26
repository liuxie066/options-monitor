"""第5步收口回归：Entry 仅编排，外部调用下沉到基础设施拥有者模块。"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_entry_imports_service_module() -> None:
    multi_tick = _read("src/application/multi_account_tick.py")
    cli = _read("src/interfaces/cli/main.py")
    run_ops = _read("src/interfaces/cli/run_ops.py")

    assert "from src.infrastructure.external_services import (" in multi_tick
    assert "handle_run_command(" in cli
    assert "from src.application.multi_account_tick import run_tick" in run_ops


def test_legacy_infra_service_wrappers_are_removed() -> None:
    assert not (ROOT / "scripts" / "infra" / "service.py").exists()
    assert not (ROOT / "scripts" / "infra" / "entry_external.py").exists()
    assert not (ROOT / "scripts" / "send_if_needed.py").exists()
    assert not (ROOT / "scripts" / "send_if_needed_multi.py").exists()


def test_trading_calendar_endpoint_uses_canonical_profile_resolved_route(monkeypatch) -> None:
    import src.application.multi_account_tick as tick

    cfg = {
        "_generated": {"market": "us"},
        "templates": {
            "shared_quote": {
                "fetch": {
                    "source": "futu",
                    "host": "quote.local",
                    "port": 11111,
                }
            }
        },
        "symbols": [
            {"symbol": "NVDA", "use": "shared_quote"},
            {"symbol": "PDD", "use": "shared_quote"},
        ],
    }

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        tick,
        "trading_day_via_futu",
        lambda **kwargs: captured.update(kwargs) or (True, "US"),
    )
    assert tick._is_trading_day_guard_for_market(cfg, "US") == (True, "US")
    assert captured == {"host": "quote.local", "port": 11111, "market": "US"}


def test_trading_calendar_endpoint_fails_closed_on_route_conflict(monkeypatch) -> None:
    import src.application.multi_account_tick as tick

    cfg = {
        "_generated": {"market": "us"},
        "symbols": [
            {
                "symbol": "NVDA",
                "fetch": {"source": "futu", "host": "one", "port": 11111},
            },
            {
                "symbol": "PDD",
                "fetch": {"source": "futu", "host": "two", "port": 11112},
            },
        ],
    }

    monkeypatch.setattr(
        tick,
        "trading_day_via_futu",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("conflicting route must not open OpenD")
        ),
    )
    assert tick._is_trading_day_guard_for_market(cfg, "US") == (None, "US")


def test_trading_day_via_futu_port_closed_returns_unavailable_without_sdk_context(monkeypatch) -> None:
    import time

    from src.infrastructure import external_services as svc

    monkeypatch.setattr(
        svc,
        "build_futu_gateway",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("closed port must not build a gateway")
        ),
    )
    monkeypatch.setattr(svc, "port_open", lambda host, port: False)

    t0 = time.monotonic()
    result = svc.trading_day_via_futu(host="127.0.0.9", port=11119, market="HK")
    assert result == (None, "HK")
    assert time.monotonic() - t0 < 1.0


def test_trading_day_via_futu_uses_gateway(monkeypatch) -> None:
    from datetime import date

    from src.infrastructure import external_services as svc

    calls: list[dict[str, object]] = []

    class _Gateway:
        def get_trading_days(self, **kwargs):  # noqa: ANN003, ANN201
            calls.append(kwargs)
            return [{"time": "2026-08-26", "trade_date_type": "WHOLE"}]

        def close(self) -> None:
            calls.append({"closed": True})

    monkeypatch.setattr(svc, "port_open", lambda _host, _port: True)
    monkeypatch.setattr(svc, "build_futu_gateway", lambda **_kwargs: _Gateway())
    monkeypatch.setattr(svc, "_trading_date", lambda _market: date(2026, 8, 26))

    assert svc.trading_day_via_futu(host="127.0.0.1", port=11111, market="US") == (True, "US")
    assert calls == [
        {"market": "US", "start": "2026-08-26", "end": "2026-08-26"},
        {"closed": True},
    ]
