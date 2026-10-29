from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.application import opend_market_snapshot_fetching as snapshots
from src.application import required_data_planning as planning
from src.application.multi_tick import required_data_prefetch as prefetch
from src.application.opend_fetch_config import OpenDEndpointRateLimit
from src.infrastructure import futu_gateway


NOW = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0.0)

    def advance(seconds):
        state.now += seconds

    state.advance = advance
    monkeypatch.setattr(futu_gateway.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(futu_gateway.time, "sleep", advance)
    monkeypatch.setattr(futu_gateway.random, "uniform", lambda *_args: 0.0)
    return state


class Gateway:
    def __init__(self, clock, *, snapshot=(), market_state=()):
        self.clock = clock
        self.steps = {"get_snapshot": list(snapshot), "get_market_state": list(market_state)}
        self.calls = []
        self.in_limiter = False
        self.limits = []
        self.closed = False

    def _call(self, endpoint, codes):
        assert self.in_limiter, "every provider attempt must acquire the limiter"
        self.calls.append((endpoint, list(codes), self.clock.now))
        if self.steps[endpoint]:
            elapsed, result = self.steps[endpoint].pop(0)
            self.clock.advance(elapsed)
            if isinstance(result, Exception):
                raise result
            return result
        if endpoint == "get_snapshot":
            return [
                {
                    "code": code,
                    "last_price": 180.0,
                    "update_time": "2026-08-06 10:59:00",
                    "sec_status": "NORMAL",
                    "suspension": False,
                }
                for code in codes
            ]
        return [{"code": code, "market_state": "MORNING"} for code in codes]

    def get_snapshot(self, codes):
        return self._call("get_snapshot", codes)

    def get_market_state(self, codes):
        return self._call("get_market_state", codes)

    def limited(self, **kwargs):
        assert kwargs["endpoint"] == "market_snapshot"
        self.limits.append((kwargs["deadline"], kwargs["max_wait_sec"]))
        self.in_limiter = True
        try:
            return kwargs["call"]()
        finally:
            self.in_limiter = False

    def close(self):
        self.closed = True


def fetch(tmp_path, gateway, **kwargs):
    return snapshots.get_underlier_observations_opend(
        gateway,
        kwargs.pop("codes", ["US.NVDA"]),
        market="US",
        base_dir=tmp_path,
        snapshot_limit=OpenDEndpointRateLimit(window_sec=30, max_calls=60, max_wait_sec=30),
        snapshot_batch_size=kwargs.pop("batch_size", 2),
        monotonic=lambda: gateway.clock.now,
        now_utc=lambda: NOW,
        rate_limited_call=kwargs.pop("rate_limited_call", gateway.limited),
        **kwargs,
    )


@pytest.mark.parametrize("endpoint", ["get_snapshot", "get_market_state"])
def test_transient_retry_recovers_inside_limiter_and_preserves_error(tmp_path, clock, endpoint):
    gateway = Gateway(
        clock,
        **{
            "snapshot" if endpoint == "get_snapshot" else "market_state": [(1.0, ConnectionError("connection reset"))],
        },
    )
    observations = fetch(tmp_path, gateway, codes=["US.NVDA", "US.AAPL"])
    assert len(gateway.limits) == 3
    for code, observation in observations.items():
        assert observation.status == "ready"
        evidence = next(item for item in observation.to_dict()["fetch_diagnostics"] if item["endpoint"] == endpoint)
        assert evidence["symbol"] == code
        assert evidence["attempts"] == 2
        assert evidence["elapsed_sec"] == pytest.approx(1.8)
        assert evidence["stop_reason"] == "success"
        assert evidence["errors"] == [
            {
                "attempt": 1,
                "error_code": "TRANSIENT",
                "error_class": "ConnectionError",
                "message": "connection reset",
            }
        ]


def test_exhausted_snapshot_does_not_prevent_market_state_retry(tmp_path, clock):
    gateway = Gateway(
        clock, snapshot=[(0, ConnectionError("reset"))] * 3, market_state=[(0, TimeoutError("timed out"))]
    )
    observation = fetch(tmp_path, gateway)["US.NVDA"]
    assert observation.status == "data_unavailable"
    assert observation.last_price is None
    assert observation.market_state == "MORNING"
    snapshot, state = observation.fetch_diagnostics
    assert snapshot["attempts"] == 2
    assert snapshot["stop_reason"] == "max_attempts"
    assert snapshot["error_class"] == "ConnectionError"
    assert len(snapshot["errors"]) == 2
    assert state["attempts"] == 2
    assert state["stop_reason"] == "success"
    assert len(gateway.calls) == 4


@pytest.mark.parametrize("error", [RuntimeError("not logged in"), ValueError("invalid request")])
def test_permanent_error_is_not_retried(tmp_path, clock, error):
    gateway = Gateway(clock, snapshot=[(0, error)])
    observation = fetch(tmp_path, gateway)["US.NVDA"]
    evidence = observation.fetch_diagnostics[0]
    assert observation.status == "data_unavailable"
    assert evidence["attempts"] == 1
    assert evidence["error_class"] == type(error).__name__
    assert evidence["stop_reason"] == "non_retryable"
    assert len(gateway.calls) == 2


def test_empty_response_is_unavailable_without_retry_or_fallback(tmp_path, clock):
    gateway = Gateway(clock, snapshot=[(0, [])])
    observation = fetch(tmp_path, gateway)["US.NVDA"]
    assert observation.status == "data_unavailable"
    assert observation.last_price is None
    assert observation.fetch_diagnostics[0]["attempts"] == 1
    assert len(gateway.calls) == 2


@pytest.mark.parametrize("elapsed", [29.5, 30.0, 31.0])
def test_first_attempt_counts_toward_budget_and_next_endpoint_gets_own_budget(tmp_path, clock, elapsed):
    gateway = Gateway(clock, snapshot=[(elapsed, ConnectionError("connection reset"))])
    observation = fetch(tmp_path, gateway)["US.NVDA"]
    evidence = observation.fetch_diagnostics[0]
    assert evidence["attempts"] == 1
    assert evidence["elapsed_sec"] == elapsed
    assert evidence["stop_reason"] == "time_budget"
    assert observation.status == "data_unavailable"
    assert observation.market_state == "MORNING"
    assert len(gateway.calls) == 2
    assert gateway.limits == [(30.0, 30.0), (elapsed + 30.0, 30.0)]


def test_late_success_is_not_accepted(tmp_path, clock):
    gateway = Gateway(clock, snapshot=[(31, [{"code": "US.NVDA", "last_price": 999}])])
    observation = fetch(tmp_path, gateway)["US.NVDA"]
    assert observation.last_price is None
    assert observation.status == "data_unavailable"
    assert observation.fetch_diagnostics[0]["stop_reason"] == "time_budget"


@pytest.mark.parametrize(
    ("deadline", "stop", "reason", "raises"),
    [
        (0.5, 10, "deadline", True),
        (10, 0.5, "stop_monotonic", False),
        (None, 0.5, "stop_monotonic", False),
    ],
)
def test_earliest_control_limits_backoff_and_preserves_evidence(tmp_path, clock, deadline, stop, reason, raises):
    gateway = Gateway(clock, snapshot=[(0, ConnectionError("reset"))])
    diagnostics = []
    kwargs = dict(deadline=deadline, stop_monotonic=stop, diagnostics=diagnostics)
    if raises:
        with pytest.raises(TimeoutError, match="deadline"):
            fetch(tmp_path, gateway, **kwargs)
    else:
        observation = fetch(tmp_path, gateway, **kwargs)["US.NVDA"]
        assert observation.status == "data_unavailable"
    assert len(gateway.calls) == 1
    assert clock.now == 0.5
    assert gateway.limits == [(0.5, 0.5)]
    assert diagnostics[0]["attempts"] == 1
    assert diagnostics[0]["stop_reason"] == reason
    assert diagnostics[0]["errors"][0]["error_class"] == "ConnectionError"


def test_limiter_wait_consumes_endpoint_budget(tmp_path, clock):
    gateway = Gateway(clock)

    def limited(**kwargs):
        if not gateway.limits:
            clock.advance(30)
        return gateway.limited(**kwargs)

    observation = fetch(tmp_path, gateway, rate_limited_call=limited)["US.NVDA"]
    assert [call[0] for call in gateway.calls] == ["get_market_state"]
    assert observation.fetch_diagnostics[0]["attempts"] == 0
    assert observation.fetch_diagnostics[0]["elapsed_sec"] == 30
    assert observation.fetch_diagnostics[0]["stop_reason"] == "time_budget"


def test_rate_limit_retry_reacquires_limiter_with_remaining_budget(tmp_path, clock):
    gateway = Gateway(clock, snapshot=[(1, RuntimeError("rate limit"))])
    observation = fetch(tmp_path, gateway)["US.NVDA"]
    assert observation.status == "ready"
    assert gateway.limits == [(30.0, 30.0), (30.0, 27.0), (33.0, 30.0)]
    evidence = observation.fetch_diagnostics[0]
    assert evidence["attempts"] == 2
    assert evidence["elapsed_sec"] == 3.0
    assert evidence["errors"][0]["error_code"] == "RATE_LIMIT"


def test_retry_limiter_wait_cannot_start_provider_after_budget(tmp_path, clock):
    gateway = Gateway(clock, snapshot=[(0, ConnectionError("reset"))])

    def limited(**kwargs):
        if len(gateway.limits) == 1:
            clock.advance(30 - clock.now)
        return gateway.limited(**kwargs)

    observation = fetch(tmp_path, gateway, rate_limited_call=limited)["US.NVDA"]
    assert [call[0] for call in gateway.calls] == ["get_snapshot", "get_market_state"]
    assert observation.fetch_diagnostics[0]["attempts"] == 1
    assert observation.fetch_diagnostics[0]["stop_reason"] == "time_budget"
    assert observation.fetch_diagnostics[0]["errors"][0]["message"] == "reset"


@pytest.mark.parametrize("during_backoff", [False, True])
def test_cancellation_propagates_without_second_attempt(tmp_path, clock, during_backoff):
    gateway = Gateway(clock, snapshot=[(0 if during_backoff else 0.1, ConnectionError("reset"))])
    diagnostics = []
    with pytest.raises(TimeoutError, match="cancelled"):
        fetch(tmp_path, gateway, cancelled=lambda: clock.now >= 0.1, diagnostics=diagnostics)
    assert len(gateway.calls) == 1
    assert diagnostics[0]["stop_reason"] == "cancelled"
    assert diagnostics[0]["attempts"] == 1
    assert diagnostics[0]["errors"][0]["message"] == "reset"


def test_retry_budget_resets_per_batch(tmp_path, clock):
    gateway = Gateway(clock, snapshot=[(0, ConnectionError("reset")), (0, []), (0, ConnectionError("reset")), (0, [])])
    observations = fetch(tmp_path, gateway, codes=["US.NVDA", "US.AAPL"], batch_size=1)
    assert len(gateway.calls) == 6
    assert all(item.fetch_diagnostics[0]["attempts"] == 2 for item in observations.values())


def test_prefill_and_required_data_debug_preserve_retry_evidence(tmp_path, clock, monkeypatch):
    gateway = Gateway(clock, snapshot=[(0, ConnectionError("reset"))] * 2)
    identity = planning.RequiredDataPlanningIdentity(
        "NVDA",
        "opend",
        "127.0.0.1",
        11111,
        "2026-08-06",
        "US.NVDA",
        "US",
    )
    monkeypatch.setattr(prefetch, "freeze_required_data_planning_identity", lambda **_kwargs: identity)
    monkeypatch.setattr(futu_gateway, "build_ready_futu_quote_gateway", lambda **_kwargs: gateway)
    monkeypatch.setattr(
        prefetch,
        "get_underlier_observations_opend",
        lambda gateway, codes, **kwargs: fetch(
            tmp_path,
            gateway,
            codes=codes,
            stop_monotonic=kwargs["stop_monotonic"],
            diagnostics=kwargs["diagnostics"],
        ),
    )
    cache = {}
    prefetch._prefill_spot_observation_cache(
        symbol_cfgs=[{"symbol": "NVDA", "fetch": {"source": "futu"}, "sell_put": {"enabled": True}}],
        base=tmp_path,
        opend_fetch_cfg={},
        snapshot_batch_size=2,
        spot_observation_cache=cache,
    )
    monkeypatch.setattr(planning, "get_underlier_spot", lambda *_args, **_kwargs: pytest.fail("must reuse observation"))
    monkeypatch.setattr(planning, "list_option_expirations", lambda *_args, **_kwargs: ["2026-08-21"])
    bundle = planning.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=0,
        want_put=True,
        want_call=False,
        sell_put_cfg={"enabled": True},
        planning_identity=identity,
        spot_observation_cache=cache,
    )
    debug = bundle.to_debug_dict()
    assert not bundle.spot_observation_complete
    assert bundle.projection_outcome == "provider_error"
    assert debug["spot_observation_diagnostics"] == cache[identity.cache_key].fetch_diagnostics
    assert debug["underlier_observation"]["fetch_diagnostics"][0]["error_class"] == "ConnectionError"
    assert gateway.closed


@pytest.mark.parametrize("failure", ["gateway", "observation", "stop"])
def test_prefill_preserves_failure_and_skipped_observations_without_refetch(tmp_path, clock, monkeypatch, failure):
    identity = planning.RequiredDataPlanningIdentity(
        "NVDA",
        "opend",
        "127.0.0.1",
        11111,
        "2026-08-06",
        "US.NVDA",
        "US",
    )
    monkeypatch.setattr(prefetch, "freeze_required_data_planning_identity", lambda **_kwargs: identity)
    gateway = Gateway(clock)

    def fail(**_kwargs):
        raise ConnectionError("connection reset")

    monkeypatch.setattr(
        futu_gateway, "build_ready_futu_quote_gateway", fail if failure == "gateway" else lambda **_kwargs: gateway
    )
    monkeypatch.setattr(prefetch, "get_underlier_observations_opend", lambda *_args, **kwargs: fail())
    cache = {}
    prefetch._prefill_spot_observation_cache(
        symbol_cfgs=[{"symbol": "NVDA", "fetch": {"source": "futu"}, "sell_put": {"enabled": True}}],
        base=tmp_path,
        opend_fetch_cfg={},
        snapshot_batch_size=2,
        spot_observation_cache=cache,
        stop_monotonic=0 if failure == "stop" else None,
    )
    observation = planning._cached_spot_observation(cache=cache, cache_key=identity.cache_key)
    assert observation.status == "data_unavailable"
    evidence = observation.fetch_diagnostics[0]
    assert evidence["symbol"] == "US.NVDA"
    if failure == "stop":
        assert evidence["stop_reason"] == "stop_monotonic"
        assert evidence["attempts"] == 0
    else:
        assert evidence["error_class"] == "ConnectionError"
        assert evidence["message"] == "connection reset"
