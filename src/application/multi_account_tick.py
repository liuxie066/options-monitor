from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from scripts.multi_tick.main import current_run_id, main as multi_tick_main


@contextmanager
def _argv_scope(argv: list[str]) -> Iterator[None]:
    old = list(sys.argv)
    try:
        sys.argv = argv
        yield
    finally:
        sys.argv = old


def run_tick(argv: list[str] | None = None) -> int:
    scoped = ["om", "run", "tick", *(argv or [])]
    with _argv_scope(scoped):
        return int(multi_tick_main())


__all__ = ["current_run_id", "run_tick"]
