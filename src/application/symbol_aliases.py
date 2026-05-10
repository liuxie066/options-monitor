from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG_NAMES = ("config.us.json", "config.hk.json")


def symbol_aliases_from_config(config: Mapping[str, Any] | None) -> dict[str, str]:
    intake = config.get("intake") if isinstance(config, Mapping) else None
    aliases = intake.get("symbol_aliases") if isinstance(intake, Mapping) else None
    if not isinstance(aliases, Mapping):
        return {}

    out: dict[str, str] = {}
    for alias, symbol in aliases.items():
        alias_key = str(alias or "").strip()
        symbol_value = str(symbol or "").strip()
        if alias_key and symbol_value:
            out[alias_key.upper()] = symbol_value
    return out


@lru_cache(maxsize=8)
def _load_runtime_symbol_aliases(root: str) -> dict[str, str]:
    out: dict[str, str] = {}
    base = Path(root)
    for name in RUNTIME_CONFIG_NAMES:
        path = base / name
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.update(symbol_aliases_from_config(cfg if isinstance(cfg, Mapping) else None))
    return out


def load_runtime_symbol_aliases(base_dir: Path | str | None = None) -> dict[str, str]:
    root = Path(base_dir).resolve() if base_dir is not None else REPO_ROOT
    return dict(_load_runtime_symbol_aliases(str(root)))
