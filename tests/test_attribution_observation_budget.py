from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.application.wheel import capacity


BASE = {"portfolio": {"snapshot": "base"}}
FRESH = {"portfolio": {"snapshot": "fresh"}, "ordinary_evidence": {"observed_at_ms": 100}}


def _observe_messages(monkeypatch, tmp_path, messages, *, history=True, dead_at=None,
                      cancel_at=None, cancel_on_receive=False, queued_on_exit=None):
    """Drive only the observer's IPC boundary with a deterministic clock."""
    import multiprocessing
    import time

    clock = SimpleNamespace(now=0.0, cancelled=False)
    queue = [(at, deepcopy(value), delay) for at, value, delay in messages]
    calls = []
    stop = SimpleNamespace(is_set=lambda: clock.cancelled or (
        cancel_at is not None and clock.now >= cancel_at))

    class Receiver:
        def poll(self, timeout):
            end = clock.now + timeout
            clock.now = min(end, max(clock.now, queue[0][0])) if queue else end
            return bool(queue and queue[0][0] <= clock.now)

        def recv(self):
            _at, value, delay = queue.pop(0)
            clock.now += delay
            clock.cancelled = cancel_on_receive
            if isinstance(value, Exception):
                raise value
            return value

        def close(self):
            calls.append("receiver.close")

    class Sender:
        def close(self):
            calls.append("sender.close")

    class Process:
        pid = None
        terminated = False
        exit_message_queued = False

        def __init__(self, *, target, args, daemon):
            assert target is capacity._attribution_capacity_worker and daemon
            assert len(args) == (5 if history else 4)

        def start(self):
            self.pid = 1
            calls.append("start")

        def is_alive(self):
            alive = not self.terminated and (dead_at is None or clock.now < dead_at)
            if not alive and queued_on_exit is not None and not self.exit_message_queued:
                queue.append((clock.now, deepcopy(queued_on_exit), 0))
                self.exit_message_queued = True
            return alive

        def terminate(self):
            self.terminated = True
            calls.append("terminate")

        def join(self, timeout):
            assert timeout == 0.5
            calls.append("join")

        def kill(self):
            pytest.fail("a terminated fake worker must not require kill")

        def close(self):
            calls.append("process.close")

    context = SimpleNamespace(Pipe=lambda **_: (Receiver(), Sender()), Process=Process)
    monkeypatch.setattr(multiprocessing, "get_context", lambda name: context)
    monkeypatch.setattr(time, "monotonic", lambda: clock.now)
    result = capacity.observe_trade_attribution_capacity(
        config={}, account="qa", runtime_root=tmp_path, stop_event=stop,
        **({"ordinary_history_start_ms": 1} if history else {}),
    )
    if "start" in calls:
        assert "receiver.close" in calls and "process.close" in calls and "join" in calls
    return result, clock.now, calls


@pytest.mark.parametrize("terminal", [FRESH, {"portfolio": {"partial": True}, "ordinary_evidence": {}},
    {"portfolio": {"wrong_account": True}, "ordinary_evidence": {}},
    {"portfolio": {"expired": True}, "ordinary_evidence": {}}])
def test_timely_terminal_keeps_its_own_portfolio(monkeypatch, tmp_path, terminal):
    result, elapsed, _ = _observe_messages(monkeypatch, tmp_path,
        [(0.2, BASE, 0), (0.4, terminal, 0)])
    assert result == terminal and elapsed == pytest.approx(0.4)


def test_no_history_returns_first_observation(monkeypatch, tmp_path):
    result, elapsed, _ = _observe_messages(monkeypatch, tmp_path, [(0.2, BASE, 0)], history=False)
    assert result == BASE and elapsed == pytest.approx(0.2)


@pytest.mark.parametrize("base_received", [False, True])
@pytest.mark.parametrize("ending,error", [
    ("timeout", "provider_timeout"), ("dead", "provider_worker_exited"),
    ("eof", "provider_unavailable"), ("worker_error", "RuntimeError"),
    ("partial", "provider_unavailable"), ("non_mapping", "provider_unavailable"),
])
def test_unfinished_observation_retains_only_base_and_error(
    monkeypatch, tmp_path, base_received, ending, error,
):
    messages = [(0.2, BASE, 0)] if base_received else []
    failures = {"eof": EOFError(), "worker_error": {"error": "RuntimeError"},
                "partial": {"ordinary_evidence": {"observed_at_ms": 100}}, "non_mapping": []}
    if ending in failures:
        messages.append((0.4, failures[ending], 0))
    result, elapsed, _ = _observe_messages(monkeypatch, tmp_path, messages,
        dead_at=0.4 if ending == "dead" else None)
    expected = {**BASE, "ordinary_evidence": {"error": error}} if base_received else {"error": error}
    assert result == expected
    assert elapsed <= 10


def test_dead_worker_drains_already_available_terminal(monkeypatch, tmp_path):
    result, elapsed, _ = _observe_messages(monkeypatch, tmp_path, [(0.2, BASE, 0)],
        dead_at=0.4, queued_on_exit=FRESH)
    assert result == FRESH and elapsed == pytest.approx(0.4)


@pytest.mark.parametrize("messages,base_received", [
    ([(10, BASE, 0)], False),
    ([(0.2, BASE, 0), (9.9, FRESH, 0.2)], True),
])
def test_deadline_rejects_late_results(monkeypatch, tmp_path, messages, base_received):
    result, elapsed, _ = _observe_messages(monkeypatch, tmp_path, messages)
    assert result == ({**BASE, "ordinary_evidence": {"error": "provider_timeout"}}
                      if base_received else {"error": "provider_timeout"})
    assert elapsed >= 10


@pytest.mark.parametrize("cancel_at,cancel_on_receive", [(0, False), (0.2, False), (0.3, False), (None, True)])
def test_cancellation_wins_over_buffered_or_received_portfolio(
    monkeypatch, tmp_path, cancel_at, cancel_on_receive,
):
    result, _elapsed, calls = _observe_messages(monkeypatch, tmp_path,
        [(0.2, BASE, 0), (0.4, FRESH, 0)], cancel_at=cancel_at, cancel_on_receive=cancel_on_receive)
    assert result == {"error": "cancelled"}
    if cancel_at == 0:
        assert "start" not in calls


@pytest.mark.parametrize("ordinary", [None, {}, {"error": "incomplete"}, {"observed_at_ms": 100}])
def test_actual_worker_sends_base_before_optional_io(monkeypatch, tmp_path, ordinary):
    messages, calls = [], []

    def portfolio(**kwargs):
        fresh = "load_json_fn" in kwargs
        calls.append("fresh" if fresh else "base")
        assert kwargs["write_cache"] is False and kwargs["include_options"] is True
        if fresh:
            assert calls[-2] == "history" and kwargs["load_json_fn"](None) is None
        return {"snapshot": "fresh" if fresh else "base"}

    def history(**kwargs):
        assert messages == [BASE]
        calls.append("history")
        return ordinary

    monkeypatch.setattr(capacity, "current_exchange_rate_snapshot", lambda **_: None)
    monkeypatch.setattr(capacity, "project_exchange_rate_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(capacity, "load_account_portfolio_context", portfolio)
    monkeypatch.setattr(capacity, "_read_ordinary_attribution_broker_evidence", history)
    connection = SimpleNamespace(send=lambda value: messages.append(deepcopy(value)), close=lambda: calls.append("close"))
    capacity._attribution_capacity_worker(connection, {}, "qa", tmp_path, 1)
    fresh = bool(ordinary and not ordinary.get("error"))
    assert calls == ["base", "history", *(["fresh"] if fresh else []), "close"]
    assert messages == [BASE, {"portfolio": {"snapshot": "fresh" if fresh else "base"},
                              "ordinary_evidence": ordinary}]


@pytest.mark.parametrize("failed_read", ["base", "fresh"])
def test_actual_worker_does_not_pair_history_with_failed_fresh_read(monkeypatch, tmp_path, failed_read):
    messages, calls = [], []

    def portfolio(**kwargs):
        name = "fresh" if "load_json_fn" in kwargs else "base"
        calls.append(name)
        if name == failed_read:
            raise TimeoutError("synthetic portfolio unavailable")
        return BASE["portfolio"]

    def history(**kwargs):
        calls.append("history")
        return FRESH["ordinary_evidence"]

    monkeypatch.setattr(capacity, "current_exchange_rate_snapshot", lambda **_: None)
    monkeypatch.setattr(capacity, "project_exchange_rate_snapshot", lambda *_, **__: None)
    monkeypatch.setattr(capacity, "load_account_portfolio_context", portfolio)
    monkeypatch.setattr(capacity, "_read_ordinary_attribution_broker_evidence", history)
    connection = SimpleNamespace(send=lambda value: messages.append(deepcopy(value)), close=lambda: None)
    capacity._attribution_capacity_worker(connection, {}, "qa", tmp_path, 1)
    assert messages == ([BASE, {"error": "TimeoutError"}] if failed_read == "fresh" else [{"error": "TimeoutError"}])
    assert calls == (["base", "history", "fresh"] if failed_read == "fresh" else ["base"])
