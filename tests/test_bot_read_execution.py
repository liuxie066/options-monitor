"""Bot-compatible bounded reads keep scope context and discard late results."""
from contextvars import ContextVar
import threading
import time

import pytest

from src.application import read_execution
from src.application.bot import runtime


def test_runtime_compatibility_imports_and_nested_worker_context():
    assert runtime.RunStopped is read_execution.RunStopped
    assert runtime.check_run is read_execution.check_run
    assert runtime.bounded_call is read_execution.bounded_call
    scope = ContextVar('bot_test_scope', default=None)
    token = scope.set('trusted-host-root')
    try:
        def inner():
            value = scope.get()
            scope.set('worker-only')
            return value

        def outer():
            return read_execution.bounded_call(inner, deadline=time.monotonic() + 2, cancelled=lambda: False)

        assert runtime.bounded_call(outer, deadline=time.monotonic() + 2, cancelled=lambda: False) == 'trusted-host-root'
        assert scope.get() == 'trusted-host-root'
    finally:
        scope.reset(token)


@pytest.mark.parametrize('cancelled,code', [(True, 'CANCELLED'), (False, 'BUDGET_EXHAUSTED')])
def test_stop_before_worker_launch(monkeypatch, cancelled, code):
    monkeypatch.setattr(read_execution.time, 'monotonic', lambda: 10)
    monkeypatch.setattr(read_execution.threading, 'Thread', lambda **kw: pytest.fail('worker launched'))
    with pytest.raises(runtime.RunStopped) as stopped:
        runtime.bounded_call(lambda: None, deadline=10, cancelled=lambda: cancelled)
    assert stopped.value.code == code


def test_worker_exception_is_preserved():
    error = ValueError('provider failure')
    def read():
        raise error
    with pytest.raises(ValueError) as stopped:
        runtime.bounded_call(read, deadline=time.monotonic() + 2, cancelled=lambda: False)
    assert stopped.value is error


def test_cancel_during_read_stops_waiting_without_publishing_result():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def read():
        started.set()
        try:
            assert release.wait(2)
            return 'late-result'
        finally:
            finished.set()
    try:
        with pytest.raises(runtime.RunStopped) as stopped:
            runtime.bounded_call(read, deadline=time.monotonic() + 2, cancelled=started.is_set)
        assert stopped.value.code == 'CANCELLED'
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(2)


def test_result_finishing_after_deadline_is_discarded(monkeypatch):
    clock = [1.0]
    monkeypatch.setattr(read_execution.time, 'monotonic', lambda: clock[0])
    def read():
        clock[0] = 3.0
        return 'late-result'
    with pytest.raises(runtime.RunStopped) as stopped:
        runtime.bounded_call(read, deadline=2, cancelled=lambda: False)
    assert stopped.value.code == 'BUDGET_EXHAUSTED'
