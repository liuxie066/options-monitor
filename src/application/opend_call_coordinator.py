from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.application.option_chain_fetching import FileRateLimiter
from src.infrastructure.opend_retcodes import classify_opend_error


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
