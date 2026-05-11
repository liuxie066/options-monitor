from __future__ import annotations

import importlib
from pathlib import Path
import json
import threading
import time


def _imports() -> tuple[type, type, type]:
    rate_gate_module = importlib.import_module("src.application.opend_rate_gate")
    option_chain_module = importlib.import_module("src.application.option_chain_fetching")

    return (
        rate_gate_module.OpenDRateGate,
        option_chain_module.FileRateLimiter,
        option_chain_module.OptionChainRateLimitExceeded,
    )


def test_opend_rate_gate_basic_window() -> None:
    OpenDRateGate, _, _ = _imports()
    gate = OpenDRateGate(max_calls=3, window_sec=0.1, max_wait_sec=1.0, label="test")

    first_three = [gate.acquire() for _ in range(3)]
    started = time.monotonic()
    waited = gate.acquire()
    elapsed = time.monotonic() - started

    assert max(first_three) < 0.03
    assert waited >= 0.08
    assert elapsed >= 0.08


def test_opend_rate_gate_fair_wakeup_for_concurrent_threads() -> None:
    OpenDRateGate, _, _ = _imports()
    gate = OpenDRateGate(max_calls=3, window_sec=0.2, max_wait_sec=2.0, label="fair")
    start = threading.Barrier(10)
    arrival_lock = threading.Lock()
    completion_lock = threading.Lock()
    next_arrival = [0]
    completed: list[int] = []
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            start.wait()
            with arrival_lock:
                arrival = next_arrival[0]
                next_arrival[0] += 1
            gate.acquire()
            with completion_lock:
                completed.append(arrival)
        except BaseException as exc:  # pragma: no cover - failure path
            with completion_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(10)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - started

    assert not errors
    assert completed == list(range(10))
    assert elapsed >= 0.55
    assert elapsed < 1.5


def test_opend_rate_gate_times_out_when_budget_exceeded() -> None:
    OpenDRateGate, _, _ = _imports()
    gate = OpenDRateGate(max_calls=1, window_sec=10.0, max_wait_sec=0.5, label="timeout")

    gate.acquire()
    try:
        gate.acquire()
    except TimeoutError as exc:
        assert "rate limit wait budget exceeded" in str(exc)
    else:  # pragma: no cover - failure path
        raise AssertionError("expected TimeoutError")


def test_opend_rate_gate_merges_external_state_file() -> None:
    OpenDRateGate, _, _ = _imports()
    state_path = Path(__file__).resolve().parent / ".tmp_opend_rate_gate_state.json"
    try:
        payload = {
            "updated_at": time.time(),
            "window_sec": 0.2,
            "max_calls": 1,
            "timestamps": [time.time()],
        }
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        gate = OpenDRateGate(
            max_calls=1,
            window_sec=0.2,
            max_wait_sec=1.0,
            label="merge",
            state_path=state_path,
        )

        started = time.monotonic()
        waited = gate.acquire()
        elapsed = time.monotonic() - started

        assert waited >= 0.15
        assert elapsed >= 0.15
    finally:
        state_path.unlink(missing_ok=True)


def test_file_rate_limiter_shim_preserves_api_and_exception_type(tmp_path: Path) -> None:
    _, FileRateLimiter, _ = _imports()
    limiter = FileRateLimiter(
        state_path=tmp_path / "limiter.json",
        max_calls=1,
        window_sec=10.0,
        max_wait_sec=0.05,
        clock=time.monotonic,
    )

    waited = limiter.acquire()
    assert waited >= 0.0

    try:
        limiter.acquire()
    except Exception as exc:
        assert exc.__class__.__name__ == "OptionChainRateLimitExceeded"
        assert "rate limit wait budget exceeded" in str(exc)
    else:  # pragma: no cover - failure path
        raise AssertionError("expected OptionChainRateLimitExceeded")
