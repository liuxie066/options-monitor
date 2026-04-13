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

ALLOW_DERIVED_STRICT_TOKEN = "strict"


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


def resolve_allow_derived_config_gate(raw: Any) -> dict[str, Any]:
    token = str(raw or "").strip()
    lowered = token.lower()
    migration_hint = (
        "默认禁用派生配置；如确需临时放开，请显式设置 "
        f"OM_ALLOW_DERIVED_CONFIG={ALLOW_DERIVED_STRICT_TOKEN} 并尽快迁移到 canonical config."
    )
    if not token:
        return {
            "allow_derived": False,
            "error_code": None,
            "message": "",
            "migration_hint": migration_hint,
            "raw": token,
        }
    if lowered == ALLOW_DERIVED_STRICT_TOKEN:
        return {
            "allow_derived": True,
            "error_code": None,
            "message": "",
            "migration_hint": migration_hint,
            "raw": token,
        }
    if lowered in {"1", "true", "yes", "on"}:
        return {
            "allow_derived": False,
            "error_code": "OM_ALLOW_DERIVED_CONFIG_LEGACY_DISABLED",
            "message": "legacy truthy value is no longer enough to enable derived config.",
            "migration_hint": migration_hint,
            "raw": token,
        }
    return {
        "allow_derived": False,
        "error_code": "OM_ALLOW_DERIVED_CONFIG_INVALID",
        "message": "OM_ALLOW_DERIVED_CONFIG is set but not recognized; treated as disabled.",
        "migration_hint": migration_hint,
        "raw": token,
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
