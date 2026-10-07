from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from src.application import futu_doctor


def test_required_option_fields_uses_gateway(monkeypatch) -> None:
    calls: list[list[str]] = []

    class _Gateway:
        def get_option_chain(self, **_kwargs):  # noqa: ANN003, ANN201
            return [{"code": "HK.TCH260828P440000"}]

        def get_snapshot(self, codes):  # noqa: ANN001, ANN201
            batch = list(codes)
            calls.append(batch)
            if batch == ["HK.09992"]:
                return [{"code": "HK.09992", "last_price": 145.0, "sec_status": "NORMAL",
                         "suspension": False, "update_time": datetime.now(timezone.utc).isoformat()}]
            return [
                {
                    "code": batch[0],
                    "last_price": 1.0,
                    "bid_price": 0.9,
                    "ask_price": 1.1,
                    "volume": 10,
                    "option_open_interest": 20,
                    "option_implied_volatility": 0.3,
                    "option_delta": -0.2,
                    "option_contract_multiplier": 100,
                }
            ]

        def get_market_state(self, codes):
            calls.append(list(codes))
            return [{"code": codes[0], "market_state": "MORNING"}]

        def close(self) -> None:
            calls.append([])

    monkeypatch.setattr(
        futu_doctor,
        "build_ready_futu_quote_gateway",
        lambda **_kwargs: _Gateway(),
    )

    result = futu_doctor.check_required_option_fields(
        symbols=["9992.HK"],
        host="127.0.0.1",
        port=11111,
    )

    assert result["results"][0]["ok"] is True
    assert result["results"][0]["spot"] == 145.0
    assert calls == [
        ["HK.TCH260828P440000"],
        ["HK.09992"],
        ["HK.09992"],
        [],
    ]


@pytest.mark.parametrize("symbol,code", [("NVDA", "US.NVDA"), ("0700.HK", "HK.00700")])

@pytest.mark.parametrize("case,ready", [("ready", True), ("missing_price", False), ("missing_time", False),
    ("missing_state", False), ("stale", False), ("closed", False), ("suspended", False),
    ("snapshot_failure", False), ("state_failure", False), ("chain_empty", False)])
def test_doctor_checks_same_underlier_prerequisites_in_both_markets(monkeypatch, symbol, code, case, ready):
    calls = []
    class Gateway:
        def get_option_chain(self, **kwargs):
            return [] if case == "chain_empty" else [{"code": "synthetic-option"}]
        def get_snapshot(self, codes):
            calls.append(("snapshot", list(codes)))
            if codes == [code]:
                if case == "snapshot_failure":
                    raise RuntimeError("synthetic unavailable")
                result = {"code": code, "last_price": 100, "sec_status": "NORMAL", "suspension": case == "suspended",
                    "update_time": (datetime.now(timezone.utc) - timedelta(seconds=600 if case == "stale" else 0)).isoformat()}
                if case == "missing_price": result.pop("last_price")
                if case == "missing_time": result.pop("update_time")
                return [result]
            return [{**{name: 1 for name in futu_doctor.REQUIRED_SNAPSHOT_COLS}, "code": "synthetic-option"}]
        def get_market_state(self, codes):
            calls.append(("state", list(codes)))
            if case == "state_failure": raise RuntimeError("synthetic unavailable")
            return [{"code": code, "market_state": "CLOSED" if case == "closed" else None if case == "missing_state" else "MORNING"}]
        def close(self): calls.append(("close", []))
    monkeypatch.setattr(futu_doctor, "build_ready_futu_quote_gateway", lambda **kwargs: Gateway())
    result = futu_doctor.check_required_option_fields(symbols=[symbol], host="synthetic", port=11111)["results"][0]
    assert result["option_fields_ok"] is (case != "chain_empty")
    assert result["scan_prerequisites_ok"] is ready
    assert result["ok"] is ready
    assert calls[-1] == ("close", [])
    if case != "chain_empty":
        assert ("snapshot", [code]) in calls
        assert ("state", [code]) in calls
    if not ready and result["note"]:
        assert "override" not in result["note"]


def test_doctor_closes_gateway_when_option_provider_raises(monkeypatch):
    closed = []
    class Gateway:
        def get_option_chain(self, **kwargs): raise RuntimeError("synthetic failure")
        def close(self): closed.append(True)
    monkeypatch.setattr(futu_doctor, "build_ready_futu_quote_gateway", lambda **kwargs: Gateway())
    result = futu_doctor.check_required_option_fields(symbols=["NVDA"], host="synthetic", port=11111)["results"][0]
    assert not result["ok"]
    assert "synthetic failure" in result["error"]
    assert closed == [True]
