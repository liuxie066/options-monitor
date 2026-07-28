from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any


def required_data_plan_id(symbols: list[Mapping[str, Any]]) -> str:
    """Hash the ordered canonical symbol-plan payload used by producer and seal."""

    canonical = json.dumps(
        [dict(item) for item in symbols],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["required_data_plan_id"]
