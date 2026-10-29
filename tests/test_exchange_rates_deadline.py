import json

import pytest

from src.infrastructure import exchange_rates as fx
from test_current_exchange_rate_snapshot import _at, _row


_TENCENT = 'v_whUSDCNY="310~名称~USDCNY~7.20~0~20260930110000~";'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(fx, "_utc_now", lambda: _at(9, 30, 12))
    monkeypatch.setattr(fx, "monotonic", lambda: 10.0)

    def unexpected(*args, **kwargs):
        pytest.fail("unexpected real HTTP request")

    monkeypatch.setattr(fx.urllib_request, "urlopen", unexpected)


@pytest.mark.parametrize("deadline,expected", [(None, 8.0), (40.0, 8.0), (12.5, 2.5)])
def test_http_timeout_is_clipped_and_default_parity(monkeypatch, deadline, expected):
    calls = []

    def http(url, **kwargs):
        calls.append(kwargs["timeout_sec"])
        return _TENCENT if "gtimg" in url else ""

    monkeypatch.setattr(fx, "_http_get", http)
    bounded = fx.fetch_market_exchange_rates(deadline=deadline)
    assert calls == [expected, expected]
    assert bounded == fx.fetch_market_exchange_rates()


@pytest.mark.parametrize("control", [{"deadline": 10.0}, {"cancelled": lambda: True}])
def test_stop_before_cache_or_http(monkeypatch, tmp_path, control):
    def unexpected(*args, **kwargs):
        pytest.fail("I/O after stop")

    monkeypatch.setattr(fx, "_read_cache", unexpected)
    monkeypatch.setattr(fx, "_http_get", unexpected)
    with pytest.raises(TimeoutError):
        fx.fetch_market_exchange_rates(**control)
    with pytest.raises(TimeoutError):
        fx.current_exchange_rate_snapshot(cache_path=tmp_path / "fx.json", **control)


@pytest.mark.parametrize("failure", [None, OSError("offline"), TimeoutError("transport")])
@pytest.mark.parametrize("stop", ["deadline", "cancelled"])
def test_late_source_does_not_fall_back_or_write(monkeypatch, tmp_path, failure, stop):
    clock, cancelled, calls = [10.0], [False], []
    monkeypatch.setattr(fx, "monotonic", lambda: clock[0])

    def http(url, **kwargs):
        calls.append(url)
        if stop == "deadline":
            clock[0] = 11.0
        else:
            cancelled[0] = True
        if failure is not None:
            raise failure
        return _TENCENT

    monkeypatch.setattr(fx, "_http_get", http)
    path = tmp_path / "new" / "fx.json"
    with pytest.raises(TimeoutError, match=stop):
        fx.current_exchange_rate_snapshot(
            cache_path=path, deadline=11.0, cancelled=lambda: cancelled[0],
        )
    assert len(calls) == 1
    assert not path.parent.exists()


@pytest.mark.parametrize("failure", [OSError("offline"), TimeoutError("transport")])
def test_ordinary_provider_failures_still_fall_back(monkeypatch, failure):
    calls = []

    def http(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise failure
        return ""

    monkeypatch.setattr(fx, "_http_get", http)
    assert fx.fetch_market_exchange_rates(deadline=15.0) is None
    assert len(calls) == 2


def test_cancellation_during_cache_read_propagates_before_fetch(monkeypatch, tmp_path):
    cancelled = [False]

    def read(path):
        cancelled[0] = True
        return None

    monkeypatch.setattr(fx, "_read_cache", read)
    with pytest.raises(TimeoutError, match="cancelled"):
        fx.current_exchange_rate_snapshot(cache_path=tmp_path / "fx.json", cancelled=lambda: cancelled[0])


def test_deadline_during_parsing_stops_second_source(monkeypatch):
    clock, calls = [10.0], []
    monkeypatch.setattr(fx, "monotonic", lambda: clock[0])

    def http(url, **kwargs):
        calls.append(url)
        return _TENCENT

    original = fx._parse_provider_quotes

    def parse(*args, **kwargs):
        clock[0] = 11.0
        return original(*args, **kwargs)

    monkeypatch.setattr(fx, "_http_get", http)
    monkeypatch.setattr(fx, "_parse_provider_quotes", parse)
    with pytest.raises(TimeoutError, match="deadline"):
        fx.fetch_market_exchange_rates(deadline=11.0)
    assert len(calls) == 1


def test_read_only_snapshot_passes_controls_without_lock_or_write(monkeypatch, tmp_path):
    path = tmp_path / "fx.json"
    cached = {"schema_version": 2, "pairs": {"USDCNY": _row(7.2, _at(9, 30, 11))}}
    path.write_text(json.dumps(cached))
    before = path.read_bytes()
    calls = []
    cancelled = lambda: False

    def fetch(**kwargs):
        calls.append(kwargs)
        return None

    def unexpected(*args, **kwargs):
        pytest.fail("read-only FX must not lock or write")

    monkeypatch.setattr(fx, "fetch_market_exchange_rates", fetch)
    monkeypatch.setattr(fx.fcntl, "flock", unexpected)
    monkeypatch.setattr(fx, "atomic_write_json", unexpected)
    snapshot = fx.current_exchange_rate_snapshot(
        cache_path=path, write_cache=False, deadline=20.0, cancelled=cancelled,
    )
    assert calls == [{"deadline": 20.0, "cancelled": cancelled}]
    assert snapshot["rates"] == {"USDCNY": 7.2}
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.lock").exists()


def test_cancel_during_selection_does_not_start_cache_write(monkeypatch, tmp_path):
    stopped = [False]
    monkeypatch.setattr(fx, "fetch_market_exchange_rates", lambda **kwargs: None)
    original = fx._verified_pairs

    def verify(*args, **kwargs):
        result = original(*args, **kwargs)
        stopped[0] = True
        return result

    monkeypatch.setattr(fx, "_verified_pairs", verify)
    path = tmp_path / "fx.json"
    path.write_text(json.dumps({"pairs": {"USDCNY": _row(7.2, _at(9, 30, 11))}}))
    before = path.read_bytes()
    with pytest.raises(TimeoutError, match="cancelled"):
        fx.current_exchange_rate_snapshot(cache_path=path, cancelled=lambda: stopped[0])
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.lock").exists()
