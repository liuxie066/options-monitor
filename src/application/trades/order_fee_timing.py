"""Bounded, best-effort diagnostics for one automatic order-fee attempt."""
from __future__ import annotations

from contextlib import contextmanager
import time
from typing import Any, Iterator


ClockSample = tuple[int | None, int | None]
_PHASES = ("processing", "terminal_query", "fee_query", "ledger_enrichment")


def fee_clock_sample() -> ClockSample:
    try:
        wall_ms = int(time.time() * 1000)
    except Exception:
        wall_ms = None
    try:
        mono_ns = time.monotonic_ns()
    except Exception:
        mono_ns = None
    return wall_ms, mono_ns


def fee_elapsed_ms(start_ns: int | None, end_ns: int | None) -> int | None:
    if start_ns is None or end_ns is None or end_ns < start_ns:
        return None
    return (end_ns - start_ns) // 1_000_000


@contextmanager
def fee_timing_phase(
    timing: dict[str, Any] | None, phase: str,
) -> Iterator[ClockSample]:
    if timing is None:
        yield (None, None)
        return
    started = fee_clock_sample()
    outcome = "returned"
    try:
        yield started
    except BaseException:
        outcome = "raised"
        raise
    finally:
        ended = fee_clock_sample()
        try:
            timing[phase] = {
                "started_at_ms": started[0],
                "returned_at_ms": ended[0] if outcome == "returned" else None,
                "raised_at_ms": ended[0] if outcome == "raised" else None,
                "elapsed_ms": fee_elapsed_ms(started[1], ended[1]),
                "outcome": outcome,
            }
        except Exception:
            pass


def fee_timing_audit_summary(value: Any) -> dict[str, Any]:
    """Copy only known scalar fields; never log caller-supplied nested payloads."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("enqueued_at_ms", "queue_wait_ms"):
        item = value.get(key)
        if item is None or (type(item) is int and item >= 0):
            out[key] = item
    if value.get("queue_source") in ("enqueue", "recovery"):
        out["queue_source"] = value["queue_source"]
    for phase in _PHASES:
        record = value.get(phase)
        if not isinstance(record, dict):
            continue
        clean: dict[str, Any] = {}
        for key in ("started_at_ms", "returned_at_ms", "raised_at_ms", "elapsed_ms"):
            item = record.get(key)
            if item is None or (type(item) is int and item >= 0):
                clean[key] = item
        if record.get("outcome") in ("returned", "raised"):
            clean["outcome"] = record["outcome"]
        out[phase] = clean
    return out
