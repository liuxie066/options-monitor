from __future__ import annotations

from pathlib import Path
from typing import Any


CANONICAL_CONFIGS = {"config.us.json", "config.hk.json"}
FORBIDDEN_RUNTIME_CONFIGS = {
    "config.market_us.json",
    "config.market_hk.json",
    "config.scheduled.json",
    "config.json",
}


def _sibling_canonical_config_path(config_path: str | Path, repo_base: str | Path | None) -> Path | None:
    if repo_base is None:
        return None
    base = Path(repo_base).resolve()
    return (base.parent / "options-monitor-config" / Path(config_path).name).resolve()


def resolve_config_contract(
    config_path: str | Path,
    market_config: str,
    *,
    repo_base: str | Path | None = None,
) -> dict[str, Any]:
    raw_path = Path(config_path)
    resolved_path = raw_path.resolve()
    name = resolved_path.name
    market = str(market_config or "auto").strip().lower()
    is_canonical = name in CANONICAL_CONFIGS
    is_forbidden_runtime = name in FORBIDDEN_RUNTIME_CONFIGS

    expected = {"config.us.json", "config.hk.json"} if market in {"auto", "all"} else {
        "config.hk.json" if market == "hk" else "config.us.json"
    }
    market_match = name in expected

    sibling_canonical_path = _sibling_canonical_config_path(resolved_path, repo_base)
    sibling_canonical_exists = bool(sibling_canonical_path and sibling_canonical_path.exists())
    is_sibling_canonical = bool(sibling_canonical_path and resolved_path == sibling_canonical_path)

    return {
        "config_name": name,
        "config_path": str(raw_path),
        "resolved_path": str(resolved_path),
        "market_config": market,
        "is_canonical": is_canonical,
        "is_forbidden_runtime": is_forbidden_runtime,
        "market_match": market_match,
        "expected": sorted(expected),
        "sibling_canonical_path": (str(sibling_canonical_path) if sibling_canonical_path else None),
        "sibling_canonical_exists": sibling_canonical_exists,
        "is_sibling_canonical": is_sibling_canonical,
    }


def ensure_runtime_canonical_config(
    config_path: str | Path,
    market_config: str,
    *,
    repo_base: str | Path | None = None,
    require_sibling_external: bool = False,
) -> dict[str, Any]:
    info = resolve_config_contract(config_path, market_config, repo_base=repo_base)
    if not (info["is_canonical"] and info["market_match"]):
        raise SystemExit(
            "[CONFIG_ERROR] runtime config must be canonical "
            f"(expected: {', '.join(info['expected'])}; got: {info['config_name']})"
        )

    if require_sibling_external and info["sibling_canonical_exists"] and not info["is_sibling_canonical"]:
        raise SystemExit(
            "[CONFIG_ERROR] runtime config must use sibling canonical config when present "
            f"(got: {info['resolved_path']}; expected: {info['sibling_canonical_path']})"
        )

    return info
