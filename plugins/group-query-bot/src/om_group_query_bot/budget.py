"""Monotonic run budget shared by the model loop and the business API.

The budget only bookkeeps a single absolute deadline and the frozen call/token
counters from the design (section 9.2).  It does not cancel transports and it
never extends a deadline: a retry reuses the same remaining time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections import deque
import threading
import time
from typing import Any, Callable

DEFAULT_TURN_SECONDS = 90
DEFAULT_MODEL_CALLS = 4
DEFAULT_API_CALLS = 3
DEFAULT_OUTPUT_TOKENS = 2000
DEFAULT_RESERVE_TOKENS = 32000
DEFAULT_USER_REQUESTS_PER_MINUTE = 3
DEFAULT_GROUP_REQUESTS_PER_MINUTE = 10
DEFAULT_USER_CONCURRENCY = 1
DEFAULT_BOT_CONCURRENCY = 2
DEFAULT_PRINCIPAL_REQUESTS_PER_MINUTE = 30
ADMISSION_WINDOW_SECONDS = 60.0


class BudgetExhausted(RuntimeError):
    """No further work may start under the current deadline or counters."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class RunBudget:
    """Single-owner budget; callers must not reset or extend it."""

    deadline_monotonic: float
    max_model_calls: int = DEFAULT_MODEL_CALLS
    max_api_calls: int = DEFAULT_API_CALLS
    max_output_tokens: int = DEFAULT_OUTPUT_TOKENS
    max_reserve_tokens: int = DEFAULT_RESERVE_TOKENS
    model_calls: int = 0
    api_calls: int = 0
    reserved_tokens: int = 0
    output_tokens: int = 0
    stopped_reason: str | None = None
    stop_events: list[str] = field(default_factory=list)
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def from_request(
        cls,
        *,
        deadline_utc: datetime,
        now_utc: datetime,
        server_max_seconds: float = DEFAULT_TURN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        **limits: Any,
    ) -> "RunBudget":
        """Take the smaller of the request deadline and the server cap."""
        if deadline_utc.tzinfo is None or now_utc.tzinfo is None:
            raise ValueError("run budget requires timezone-aware timestamps")
        if server_max_seconds <= 0:
            raise ValueError("server budget cap must be positive")
        requested = (deadline_utc.astimezone(timezone.utc)
                     - now_utc.astimezone(timezone.utc)).total_seconds()
        seconds = min(requested, float(server_max_seconds))
        if seconds <= 0:
            raise BudgetExhausted("request_deadline_elapsed")
        return cls(deadline_monotonic=clock() + seconds, clock=clock, **limits)

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - self.clock())

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0 or self.stopped_reason is not None

    def stop(self, reason: str) -> None:
        """Record the first stop reason; later stops are ignored."""
        if self.stopped_reason is None:
            self.stopped_reason = reason
            self.stop_events.append(reason)

    def _ensure_active(self) -> None:
        if self.stopped_reason is not None:
            raise BudgetExhausted(self.stopped_reason)
        if self.remaining_seconds() <= 0:
            self.stop("deadline_exceeded")
            raise BudgetExhausted("deadline_exceeded")

    def begin_model_call(self) -> None:
        self._ensure_active()
        if self.model_calls >= self.max_model_calls:
            raise BudgetExhausted("model_call_limit")
        self.model_calls += 1

    def begin_api_call(self) -> None:
        self._ensure_active()
        if self.api_calls >= self.max_api_calls:
            raise BudgetExhausted("api_call_limit")
        self.api_calls += 1

    def reserve_tokens(self, tokens: int) -> None:
        if isinstance(tokens, bool) or tokens < 0:
            raise ValueError("token reservation must be a nonnegative integer")
        self._ensure_active()
        if self.reserved_tokens + tokens > self.max_reserve_tokens:
            raise BudgetExhausted("reserve_token_limit")
        self.reserved_tokens += tokens

    def record_output_tokens(self, tokens: int, *, limit: int | None = None) -> None:
        """Record one model output; the design caps each output, not the turn."""
        if isinstance(tokens, bool) or tokens < 0:
            raise ValueError("output token count must be a nonnegative integer")
        self.output_tokens += tokens
        if limit is not None and tokens > int(limit):
            self.stop("output_token_limit")

    def public_state(self) -> dict[str, Any]:
        return {
            "remaining_seconds": round(self.remaining_seconds(), 3),
            "model_calls": self.model_calls,
            "max_model_calls": self.max_model_calls,
            "api_calls": self.api_calls,
            "max_api_calls": self.max_api_calls,
            "reserved_tokens": self.reserved_tokens,
            "max_reserve_tokens": self.max_reserve_tokens,
            "output_tokens": self.output_tokens,
            "max_output_tokens": self.max_output_tokens,
            "stopped_reason": self.stopped_reason,
            "stop_events": list(self.stop_events),
        }


class AdmissionRejected(RuntimeError):
    """A request that must not reach the model or the business API."""

    def __init__(self, reason: str, retry_after_seconds: int):
        super().__init__(reason)
        self.reason = reason
        self.retry_after_seconds = max(0, int(retry_after_seconds))


class SlidingWindowLimiter:
    """Bounded per-key request rate with a monotonic sliding window."""

    def __init__(self, limit: int, *, window_seconds: float = ADMISSION_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        if isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self.clock = clock
        self._times: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float] | None:
        times = self._times.get(key)
        if times is None:
            return None
        cutoff = now - self.window_seconds
        while times and times[0] <= cutoff:
            times.popleft()
        if not times:
            self._times.pop(key, None)
            return None
        return times

    def check(self, key: str) -> None:
        with self._lock:
            now = self.clock()
            times = self._prune(key, now)
            if times is not None and len(times) >= self.limit:
                retry = times[0] + self.window_seconds - now
                raise AdmissionRejected("rate_limited", max(1, int(retry) + 1))

    def acquire(self, key: str) -> None:
        """Record one request; callers that need concurrency use a permit."""
        with self._lock:
            now = self.clock()
            times = self._prune(key, now)
            if times is None:
                times = deque()
                self._times[key] = times
            if len(times) >= self.limit:
                retry = times[0] + self.window_seconds - now
                raise AdmissionRejected("rate_limited", max(1, int(retry) + 1))
            times.append(now)


class AdmissionPermit:
    """One in-flight turn; releasing twice is a no-op."""

    def __init__(self, controller: "AdmissionController", sender: str):
        self._controller = controller
        self._sender = sender
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._controller._release(self._sender)

    def __enter__(self) -> "AdmissionPermit":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


class AdmissionController:
    """Per-user/per-group admission and in-flight concurrency for one bot."""

    def __init__(self, *, user_per_minute: int = DEFAULT_USER_REQUESTS_PER_MINUTE,
                 group_per_minute: int = DEFAULT_GROUP_REQUESTS_PER_MINUTE,
                 user_concurrency: int = DEFAULT_USER_CONCURRENCY,
                 bot_concurrency: int = DEFAULT_BOT_CONCURRENCY,
                 window_seconds: float = ADMISSION_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.user_limiter = SlidingWindowLimiter(user_per_minute, window_seconds=window_seconds,
                                                 clock=clock)
        self.group_limiter = SlidingWindowLimiter(group_per_minute, window_seconds=window_seconds,
                                                  clock=clock)
        self.user_concurrency = user_concurrency
        self.bot_concurrency = bot_concurrency
        self._active: dict[str, int] = {}
        self._total = 0
        self._lock = threading.Lock()

    def _release(self, sender: str) -> None:
        with self._lock:
            count = self._active.get(sender, 0)
            if count <= 0:
                return
            if count == 1:
                self._active.pop(sender, None)
            else:
                self._active[sender] = count - 1
            self._total = max(0, self._total - 1)

    def acquire(self, *, sender: str, chat: str) -> AdmissionPermit:
        if not sender or not chat:
            raise AdmissionRejected("identity_incomplete", 1)
        with self._lock:
            if self._active.get(sender, 0) >= self.user_concurrency:
                raise AdmissionRejected("user_concurrency_limited", 1)
            if self._total >= self.bot_concurrency:
                raise AdmissionRejected("bot_concurrency_limited", 1)
            self.user_limiter.check(sender)
            self.group_limiter.check(chat)
            self.user_limiter.acquire(sender)
            self.group_limiter.acquire(chat)
            self._active[sender] = self._active.get(sender, 0) + 1
            self._total += 1
        return AdmissionPermit(self, sender)


__all__ = [
    "ADMISSION_WINDOW_SECONDS",
    "AdmissionController",
    "AdmissionPermit",
    "AdmissionRejected",
    "BudgetExhausted",
    "DEFAULT_API_CALLS",
    "DEFAULT_BOT_CONCURRENCY",
    "DEFAULT_GROUP_REQUESTS_PER_MINUTE",
    "DEFAULT_MODEL_CALLS",
    "DEFAULT_OUTPUT_TOKENS",
    "DEFAULT_PRINCIPAL_REQUESTS_PER_MINUTE",
    "DEFAULT_RESERVE_TOKENS",
    "DEFAULT_TURN_SECONDS",
    "DEFAULT_USER_CONCURRENCY",
    "DEFAULT_USER_REQUESTS_PER_MINUTE",
    "RunBudget",
    "SlidingWindowLimiter",
]
