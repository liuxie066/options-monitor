from __future__ import annotations

from pathlib import Path
from typing import Any


CANONICAL_CONFIGS = {"config.us.json", "config.hk.json"}
DERIVED_CONFIGS = {
    "config.market_us.json",
    "config.market_hk.json",
    "config.market_us.fallback_yahoo.json",
    "config.scheduled.json",
    "config.json",
}


def resolve_config_contract(config_path: str | Path, market_config: str) -> dict[str, Any]:
    name = Path(config_path).name
    market = str(market_config or "auto").strip().lower()
    is_canonical = name in CANONICAL_CONFIGS
    is_derived = name in DERIVED_CONFIGS

    expected = {"config.us.json", "config.hk.json"} if market in {"auto", "all"} else {
        "config.hk.json" if market == "hk" else "config.us.json"
    }
    market_match = (name in expected)

    return {
        "config_name": name,
        "market_config": market,
        "is_canonical": is_canonical,
        "is_derived": is_derived,
        "market_match": market_match,
        "expected": sorted(expected),
    }


def ensure_runtime_canonical_config(
    config_path: str | Path,
    market_config: str,
    *,
    allow_derived: bool = False,
) -> dict[str, Any]:
    info = resolve_config_contract(config_path, market_config)
    if info["is_canonical"] and info["market_match"]:
        return info
    if allow_derived and (info["is_derived"] or info["is_canonical"]):
        return info
    raise SystemExit(
        "[CONFIG_ERROR] runtime config must be canonical "
        f"(expected: {', '.join(info['expected'])}; got: {info['config_name']})"
    )
