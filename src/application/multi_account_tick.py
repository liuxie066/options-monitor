from __future__ import annotations

from scripts.multi_tick.main import current_run_id, main as multi_tick_main


def run_tick(argv: list[str] | None = None) -> int:
    return int(multi_tick_main(list(argv or [])))


__all__ = ["current_run_id", "run_tick"]
