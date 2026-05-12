from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
import json
import os
import threading
import time

try:
    import fcntl
except Exception:  # pragma: no cover - Windows fallback
    fcntl = None


class OpenDRateGate:
    """Sliding-window limiter with optional cross-process file backing."""

    def __init__(
        self,
        *,
        max_calls: int,
        window_sec: float,
        max_wait_sec: float,
        label: str,
        state_path: Path | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._max_calls = max(1, int(max_calls))
        self._window = max(0.001, float(window_sec))
        self._max_wait = max(0.0, float(max_wait_sec))
        self._label = str(label or "opend")
        self._state_path = Path(state_path) if state_path is not None else None
        self._clock = clock or time.monotonic
        self._sleep = sleep
        self._wall_clock: Callable[[], float] = time.time
        self._cv = threading.Condition()
        self._timestamps: deque[float] = deque()
        self._blocked_until = 0.0
        self._waiters: deque[int] = deque()
        self._next_ticket = 0

    def acquire(self) -> float:
        started = self._clock()
        deadline = started + self._max_wait
        with self._cv:
            ticket = self._next_ticket
            self._next_ticket += 1
            self._waiters.append(ticket)
            try:
                while True:
                    now = self._clock()
                    is_front = bool(self._waiters) and self._waiters[0] == ticket
                    wait_for: float | None = None

                    if self._state_path is not None:
                        if is_front:
                            acquired, wait_for = self._try_acquire_external_locked(now)
                            if acquired:
                                self._waiters.popleft()
                                self._cv.notify()
                                return self._clock() - started
                    else:
                        self._prune_locked(now)
                        blocked_wait = max(0.0, self._blocked_until - now)
                        if blocked_wait <= 0 and is_front and len(self._timestamps) < self._max_calls:
                            self._timestamps.append(now)
                            self._waiters.popleft()
                            self._cv.notify()
                            return now - started

                        if blocked_wait > 0:
                            wait_for = blocked_wait
                        elif self._timestamps and len(self._timestamps) >= self._max_calls:
                            wait_for = max(0.0, self._window - (now - self._timestamps[0]))

                    remaining = deadline - now
                    if remaining <= 0:
                        raise TimeoutError(self._timeout_message())

                    timeout = remaining
                    if wait_for is not None:
                        timeout = min(timeout, wait_for)
                    if not is_front:
                        self._cv.notify()
                    self._cv.wait(timeout=timeout)
            finally:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
                    self._cv.notify()

    def record_rate_limit(self, *, cooldown_sec: float | None = None) -> None:
        cooldown = self._window if cooldown_sec is None else max(0.0, float(cooldown_sec))
        now = self._clock()
        if self._state_path is None:
            with self._cv:
                self._blocked_until = max(self._blocked_until, now + cooldown)
                self._cv.notify()
            return

        wall_now = self._wall_clock()
        with _external_file_lock(self._state_path):
            payload = self._read_external_payload()
            raw_timestamps = payload.get("timestamps") if isinstance(payload, dict) else []
            timestamps = self._fresh_wall_timestamps(raw_timestamps, wall_now)
            blocked_until = _as_float(payload.get("blocked_until") if isinstance(payload, dict) else None, 0.0)
            blocked_until = max(blocked_until, wall_now + cooldown)
            self._write_external_timestamps(timestamps, blocked_until=blocked_until)
        with self._cv:
            self._blocked_until = max(self._blocked_until, now + cooldown)
            self._cv.notify()

    def _timeout_message(self) -> str:
        return (
            f"{self._label} rate limit wait budget exceeded: "
            f"max_calls={self._max_calls} window_sec={self._window:g} "
            f"max_wait_sec={self._max_wait:g}"
        )

    def _prune_locked(self, now: float) -> None:
        while self._timestamps and now - self._timestamps[0] >= self._window:
            self._timestamps.popleft()

    def _try_acquire_external_locked(self, now: float) -> tuple[bool, float]:
        if self._state_path is None:
            return (False, 0.0)
        wall_now = self._wall_clock()
        with _external_file_lock(self._state_path):
            payload = self._read_external_payload()
            blocked_until = _as_float(payload.get("blocked_until") if isinstance(payload, dict) else None, 0.0)
            raw = payload.get("timestamps") if isinstance(payload, dict) else []
            timestamps = self._fresh_wall_timestamps(raw, wall_now)
            if blocked_until > wall_now:
                self._timestamps = deque(ts - (wall_now - now) for ts in timestamps)
                self._blocked_until = now + (blocked_until - wall_now)
                self._write_external_timestamps(timestamps, blocked_until=blocked_until)
                return (False, max(0.0, blocked_until - wall_now))

            self._blocked_until = 0.0
            if len(timestamps) < self._max_calls:
                timestamps.append(wall_now)
                timestamps.sort()
                self._timestamps = deque(ts - (wall_now - now) for ts in timestamps)
                self._write_external_timestamps(timestamps)
                return (True, 0.0)

            oldest = timestamps[0]
            self._timestamps = deque(ts - (wall_now - now) for ts in timestamps)
            self._write_external_timestamps(timestamps)
            return (False, max(0.0, self._window - (wall_now - oldest)))

    def _fresh_wall_timestamps(self, raw: list[Any], wall_now: float) -> list[float]:
        fresh: list[float] = []
        if not isinstance(raw, list):
            return fresh
        for item in raw:
            try:
                wall_ts = float(item)
            except Exception:
                continue
            if wall_now - wall_ts >= self._window:
                continue
            fresh.append(wall_ts)
        fresh.sort()
        return fresh

    def _read_external_payload(self) -> dict[str, Any]:
        if self._state_path is None:
            return {}
        try:
            obj = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return obj if isinstance(obj, dict) else {}

    def _write_external_timestamps(self, timestamps: list[float], *, blocked_until: float | None = None) -> None:
        if self._state_path is None:
            return
        payload = {
            "updated_at": self._wall_clock(),
            "window_sec": self._window,
            "max_calls": self._max_calls,
            "timestamps": list(timestamps),
        }
        if blocked_until is not None and float(blocked_until) > self._wall_clock():
            payload["blocked_until"] = float(blocked_until)
        _atomic_write_json(self._state_path, payload)


@contextmanager
def _external_file_lock(state_path: Path) -> Iterator[None]:
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fh = lock_path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _as_float(value: Any, default: float) -> float:
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except Exception:
        return float(default)
