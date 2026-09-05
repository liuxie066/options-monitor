from __future__ import annotations

import math
import signal
import threading
import time
from numbers import Real
from pathlib import Path
from typing import Any, Callable

from src.application.option_chain_fetching import FileRateLimiter
from src.infrastructure.opend_retcodes import classify_opend_error


INTERRUPTIBLE_OPEND_UNIT_TIMEOUT_SECONDS = 10.0


def opend_endpoint_limiter_state_path(base_dir: Path, endpoint: str) -> Path:
    safe_endpoint = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(endpoint or "opend"))
    return Path(base_dir) / "output_shared" / "state" / f"opend_{safe_endpoint}_limiter.json"


def rate_limited_opend_call(
    *,
    base_dir: Path,
    endpoint: str,
    max_wait_sec: float,
    window_sec: float,
    max_calls: int,
    call: Callable[[], Any],
) -> Any:
    limiter = FileRateLimiter(
        state_path=opend_endpoint_limiter_state_path(Path(base_dir), endpoint),
        max_calls=int(max_calls),
        window_sec=float(window_sec),
        max_wait_sec=float(max_wait_sec),
        label=f"opend_{endpoint}",
    )
    limiter.acquire()
    try:
        return call()
    except Exception as exc:
        if classify_opend_error(exc).is_rate_limit:
            limiter.record_rate_limit()
        raise


class LowPriorityOpenDCallDeferred(RuntimeError):
    reason_code = "opend_low_priority_deferred"


class InterruptibleOpenDCallError(RuntimeError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class _OpenDUnitDeadline(BaseException):
    pass


def run_interruptible_opend_unit(
    call: Callable[[], Any],
    *,
    timeout_seconds: float = INTERRUPTIBLE_OPEND_UNIT_TIMEOUT_SECONDS,
) -> Any:
    """Bound one manual OpenD unit without adding a worker process."""

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, Real)
        or not math.isfinite(float(timeout_seconds))
        or float(timeout_seconds) <= 0
    ):
        raise ValueError("OpenD unit timeout must be positive and finite")
    if (
        threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "SIGALRM")
        or not hasattr(signal, "ITIMER_REAL")
        or not hasattr(signal, "setitimer")
        or not hasattr(signal, "getitimer")
    ):
        raise InterruptibleOpenDCallError(
            "opend_low_priority_deadline_unavailable",
            "interruptible OpenD deadline is unavailable",
        )

    alarm = signal.SIGALRM
    timer = signal.ITIMER_REAL
    previous_handler = signal.getsignal(alarm)
    previous_timer = signal.getitimer(timer)
    started = time.monotonic()
    timed_out = False

    def on_deadline(_signum: int, _frame: object) -> None:
        nonlocal timed_out
        timed_out = True
        signal.setitimer(timer, 0.05)
        raise _OpenDUnitDeadline

    try:
        signal.signal(alarm, on_deadline)
        signal.setitimer(timer, float(timeout_seconds))
        try:
            result = call()
        except _OpenDUnitDeadline:
            raise InterruptibleOpenDCallError(
                "opend_low_priority_timeout",
                "interruptible OpenD unit exceeded its deadline",
            ) from None
        if timed_out:
            raise InterruptibleOpenDCallError(
                "opend_low_priority_timeout",
                "interruptible OpenD unit exceeded its deadline",
            )
        return result
    finally:
        signal.setitimer(timer, 0.0)
        elapsed = time.monotonic() - started
        signal.signal(alarm, previous_handler)
        delay, interval = previous_timer
        if delay > 0:
            delay = max(0.000001, delay - elapsed)
        signal.setitimer(timer, delay, interval)


def try_low_priority_opend_call(
    *,
    base_dir: Path,
    endpoint: str,
    window_sec: float,
    max_calls: int,
    production_reserve_calls: int,
    call: Callable[[], Any],
) -> Any:
    """Run immediately inside spare endpoint capacity or defer without calling OpenD."""

    if (
        type(max_calls) is not int
        or max_calls <= 0
        or type(production_reserve_calls) is not int
        or production_reserve_calls <= 0
        or production_reserve_calls > max_calls
    ):
        raise ValueError("OpenD low-priority reserve is invalid")
    low_priority_calls = max_calls - production_reserve_calls
    if low_priority_calls == 0:
        raise LowPriorityOpenDCallDeferred("all OpenD capacity is reserved for production")
    limiter = FileRateLimiter(
        state_path=opend_endpoint_limiter_state_path(Path(base_dir), endpoint),
        max_calls=low_priority_calls,
        window_sec=float(window_sec),
        max_wait_sec=0.0,
        label=f"opend_{endpoint}_low_priority",
    )
    if not limiter.try_acquire():
        raise LowPriorityOpenDCallDeferred("OpenD production capacity is reserved")
    try:
        return call()
    except Exception as exc:
        if classify_opend_error(exc).is_rate_limit:
            limiter.record_rate_limit()
        raise


__all__ = [
    "INTERRUPTIBLE_OPEND_UNIT_TIMEOUT_SECONDS",
    "InterruptibleOpenDCallError",
    "LowPriorityOpenDCallDeferred",
    "opend_endpoint_limiter_state_path",
    "rate_limited_opend_call",
    "run_interruptible_opend_unit",
    "try_low_priority_opend_call",
]
