from __future__ import annotations

import multiprocessing
import os
import sys
from collections.abc import Sequence


def _coerce_exit_code(value: object) -> int:
    try:
        code = int(value)
    except (TypeError, ValueError):
        return 1
    return code if 0 <= code <= 255 else 1


def hard_exit(code: int) -> None:
    """Exit the isolated listener child without waiting for SDK-owned threads."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(_coerce_exit_code(code))


def _trade_intake_child(argv: Sequence[str]) -> None:
    code = 1
    try:
        from src.application.trades.auto_intake import main

        code = _coerce_exit_code(main(list(argv)))
    except SystemExit as exc:
        code = _coerce_exit_code(exc.code)
    except BaseException as exc:
        print(
            f"[ERROR] trade-intake child crashed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        code = 1
    hard_exit(code)


def _parent_exit_code(child_exit_code: int | None) -> int:
    if child_exit_code is None:
        return 1
    if child_exit_code < 0:
        return min(128 + abs(child_exit_code), 255)
    return _coerce_exit_code(child_exit_code)


def run_trade_intake_process(argv: Sequence[str]) -> int:
    """Run trade intake in a disposable child and propagate its exit status."""
    context = multiprocessing.get_context("spawn")
    child = context.Process(
        target=_trade_intake_child,
        args=(list(argv),),
        name="options-monitor-trade-intake",
    )
    child.start()
    try:
        child.join()
    except KeyboardInterrupt:
        if child.is_alive():
            child.terminate()
        child.join(timeout=5)
        if child.is_alive():
            child.kill()
            child.join()
        return 0
    return _parent_exit_code(child.exitcode)
