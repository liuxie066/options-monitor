from __future__ import annotations

import time
from typing import Callable


class _FetchControlTimeout(TimeoutError):
    """Keep parent cancellation/deadline separate from provider timeout errors."""


def check_fetch_control(
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Check the caller's control, also polled by the existing SDK process proxy."""
    if cancelled is not None and cancelled():
        raise _FetchControlTimeout("OpenD fetch cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise _FetchControlTimeout("OpenD fetch deadline reached")


def wait_fetch_control(
    seconds: float,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    check_fetch_control(deadline, cancelled)
    if deadline is None and cancelled is None:
        time.sleep(seconds)
        return
    end = time.monotonic() + seconds
    while True:
        check_fetch_control(deadline, cancelled)
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        if deadline is not None:
            remaining = min(remaining, max(0.0, deadline - time.monotonic()))
        time.sleep(min(remaining, 0.05) if cancelled is not None else remaining)
