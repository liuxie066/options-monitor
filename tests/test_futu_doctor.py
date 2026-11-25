from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from src.application import futu_doctor


def test_discovery_facade_forwards_exact_endpoint_scope_and_limit_and_closes(monkeypatch):
    calls = []

    class Gateway:
        def get_account_candidates(self, **kwargs):
            calls.append(kwargs)
            return {"status": "ok", "candidates": [{"account_id": "912340001"}]}

        def close(self):
            calls.append("closed")

    def build(**kwargs):
        calls.append(kwargs)
        return Gateway()

    monkeypatch.setattr(futu_doctor, "build_futu_gateway", build)
    result = futu_doctor.discover_account_candidates(
        host="synthetic-endpoint", port=12345, market="hk", trd_env="REAL", max_candidates=3,
    )
    assert result["candidates"][0]["account_id"] == "912340001"
    assert calls == [
        {"host": "synthetic-endpoint", "port": 12345, "is_option_chain_cache_enabled": False},
        {"trd_env": "REAL", "expected_market": "hk", "max_candidates": 3}, "closed",
    ]


def test_discovery_facade_failure_is_sanitized_and_closed(monkeypatch, capsys):
    from src.infrastructure.futu_gateway import FutuGatewayAuthExpiredError

    closed = []

    class Gateway:
        def get_account_candidates(self, **kwargs):
            raise FutuGatewayAuthExpiredError("login expired 912340001")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(futu_doctor, "build_futu_gateway", lambda **kwargs: Gateway())
    result = futu_doctor.discover_account_candidates(host="synthetic", port=12345, market="us", trd_env="REAL")
    assert result == {"status": "unavailable", "candidates": [], "error_code": "AUTH_EXPIRED"}
    assert closed == [True]
    assert "912340001" not in str(result) + capsys.readouterr().out


def test_discovery_facade_factory_failure_does_not_escape_raw_error(monkeypatch):
    def build(**kwargs):
        raise RuntimeError("unexpected SDK error account 912340001")

    monkeypatch.setattr(futu_doctor, "build_futu_gateway", build)
    assert futu_doctor.discover_account_candidates(
        host="synthetic", port=12345, market="us", trd_env="REAL",
    ) == {"status": "unavailable", "candidates": [], "error_code": "UNKNOWN"}


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


@pytest.mark.parametrize("stop_after", [None, "chain", "snapshot"])
def test_option_probe_reuses_deadline_and_always_closes(monkeypatch, stop_after):
    import time
    from types import SimpleNamespace
    clock, calls = [100.0], []
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(futu_doctor, "normalize_underlier", lambda symbol: SimpleNamespace(code="US.TEST", market="US"))
    class Gateway:
        def get_option_chain(self, **kwargs):
            calls.append(("chain", kwargs["deadline"]))
            if stop_after == "chain": clock[0] = 102.0
            return [{"code": "US.TEST-C"}]
        def get_snapshot(self, codes, **kwargs):
            calls.append(("snapshot", kwargs["deadline"]))
            if stop_after == "snapshot": clock[0] = 102.0
            return [{key: "fixture" for key in futu_doctor.REQUIRED_SNAPSHOT_COLS}]
        def close(self): calls.append(("close", None))
    def build(**kwargs):
        calls.append(("factory", kwargs["deadline"]))
        return Gateway()
    def observe(*args, **kwargs):
        calls.append(("observation", kwargs["deadline"]))
        return SimpleNamespace(status="ready", last_price=100, to_dict=lambda: {})
    monkeypatch.setattr(futu_doctor, "build_ready_futu_quote_gateway", build)
    monkeypatch.setattr(futu_doctor, "get_underlier_observation_opend", observe)
    if stop_after:
        with pytest.raises(TimeoutError):
            futu_doctor.check_required_option_fields(symbols=["TEST", "NEXT"], host="fake", port=1, deadline=101.0)
        assert [name for name, _ in calls] == (["factory", "chain", "close"] if stop_after == "chain"
                                             else ["factory", "chain", "snapshot", "close"])
    else:
        result = futu_doctor.check_required_option_fields(symbols=["TEST"], host="fake", port=1, deadline=101.0)
        assert result["results"][0]["ok"]
        assert all(value == 101.0 for name, value in calls if name != "close")
    assert calls[-1] == ("close", None)


@pytest.mark.parametrize("path", ["quote_retry", "port_retry", "start", "broker"])
def test_watchdog_subpaths_share_remaining_deadline(monkeypatch, path):
    from types import SimpleNamespace
    from src.infrastructure import opend_watchdog as watchdog, futu_gateway
    clock = [10.0]
    calls = []
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(watchdog.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))
    monkeypatch.setattr(watchdog, "port_open", lambda *args, **kwargs: path == "broker")
    if path == "quote_retry":
        def quote(*args, **kwargs):
            calls.append(kwargs)
            raise ConnectionError("connection reset")
        monkeypatch.setattr(watchdog, "get_global_state_once", quote)
        operation = lambda: watchdog.get_global_state("fixture", 11111, deadline=10.2)
    elif path == "port_retry":
        operation = lambda: watchdog.run_watchdog_check(deadline=10.2, retry_enabled=True)
    elif path == "start":
        monkeypatch.setenv("OPEND_START_SCRIPT", "/fixture/never-run.sh")
        def run(*args, **kwargs):
            calls.append(kwargs)
            assert kwargs["timeout"] == pytest.approx(0.2)
            clock[0] = 10.2
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(watchdog.subprocess, "run", run)
        operation = lambda: watchdog.try_start_opend(deadline=10.2)
    else:
        def build(**kwargs):
            calls.append(kwargs)
            clock[0] = 10.2
            return SimpleNamespace(close=lambda: calls.append("closed"))
        monkeypatch.setattr(futu_gateway, "build_ready_futu_broker_gateway", build)
        operation = lambda: watchdog.run_watchdog_check(deadline=10.2, required_capability="broker")
    with pytest.raises(TimeoutError, match="deadline"):
        operation()
    assert clock[0] == pytest.approx(10.2)
    if path in {"quote_retry", "broker"}:
        assert calls[0]["deadline"] == 10.2
        assert len(calls) == (2 if path == "broker" else 1)
    if path == "broker":
        assert calls[-1] == "closed"
