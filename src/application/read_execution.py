"""Bounded read execution shared by application coordinators and Bot."""
from __future__ import annotations

import queue
import threading
import time
from contextvars import copy_context
from typing import Callable


class RunStopped(Exception):
    def __init__(self, code: str, reason: str):
        self.code, self.reason = code, reason
        super().__init__(reason)


def check_run(deadline: float, cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise RunStopped("CANCELLED", "cancelled")
    if time.monotonic() >= deadline:
        raise RunStopped("BUDGET_EXHAUSTED", "time_deadline")


def bounded_call(fn: Callable, *, deadline: float, cancelled: Callable[[], bool], **kwargs):
    """Stop waiting at the budget; in-flight calls must enforce their own I/O cancellation."""
    check_run(deadline, cancelled)
    result: queue.Queue = queue.Queue(maxsize=1)
    context = copy_context()

    def work():
        try:
            result.put((True, context.run(fn, **kwargs)))
        except BaseException as exc:
            result.put((False, exc))

    threading.Thread(target=work, daemon=True, name="bounded-read").start()
    while True:
        check_run(deadline, cancelled)
        try:
            ok, value = result.get(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
        except queue.Empty:
            continue
        check_run(deadline, cancelled)
        if not ok:
            raise value
        return value


