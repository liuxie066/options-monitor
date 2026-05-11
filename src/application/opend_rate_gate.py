from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any, Callable
import json
import os
import threading
import time


class OpenDRateGate:
    """In-process sliding-window limiter with optional cross-process file backing."""

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
                    self._prune_locked(now)
                    if self._state_path is not None:
                        self._merge_external(now)
                        self._prune_locked(now)

                    is_front = bool(self._waiters) and self._waiters[0] == ticket
                    if is_front and len(self._timestamps) < self._max_calls:
                        self._timestamps.append(now)
                        if self._state_path is not None:
                            self._write_external(now)
                        self._waiters.popleft()
                        self._cv.notify()
                        return now - started

                    remaining = deadline - now
                    if remaining <= 0:
                        raise TimeoutError(self._timeout_message())

                    timeout = remaining
                    if self._timestamps and len(self._timestamps) >= self._max_calls:
                        timeout = min(timeout, max(0.0, self._window - (now - self._timestamps[0])))
                    elif not is_front:
                        self._cv.notify()
                    self._cv.wait(timeout=timeout)
            finally:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
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

    def _merge_external(self, now: float) -> None:
        raw = self._read_external_timestamps()
        if not raw:
            return
        wall_now = self._wall_clock()
        offset = wall_now - now
        seen_wall = [ts + offset for ts in self._timestamps]
        merged = list(self._timestamps)
        for item in raw:
            try:
                wall_ts = float(item)
            except Exception:
                continue
            if wall_now - wall_ts >= self._window:
                continue
            mono_ts = wall_ts - offset
            if now - mono_ts >= self._window:
                continue
            if any(abs(wall_ts - existing) <= 0.001 for existing in seen_wall):
                continue
            seen_wall.append(wall_ts)
            merged.append(mono_ts)
        if not merged:
            self._timestamps.clear()
            return
        merged.sort()
        self._timestamps = deque(merged)

    def _read_external_timestamps(self) -> list[Any]:
        if self._state_path is None:
            return []
        try:
            obj = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(obj, dict):
            return []
        raw = obj.get("timestamps")
        return list(raw) if isinstance(raw, list) else []

    def _write_external(self, now: float) -> None:
        if self._state_path is None:
            return
        offset = self._wall_clock() - now
        payload = {
            "updated_at": self._wall_clock(),
            "window_sec": self._window,
            "max_calls": self._max_calls,
            "timestamps": [ts + offset for ts in self._timestamps],
        }
        _atomic_write_json(self._state_path, payload)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)
