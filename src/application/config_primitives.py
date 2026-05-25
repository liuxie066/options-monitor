from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from src.application.agent_tool_contracts import AgentToolError


MARKETS = ("us", "hk")


class IndentedYamlDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        return super().increase_indent(flow, False)


def normalize_config_market(value: str) -> str:
    market = str(value or "").strip().lower()
    if market not in MARKETS:
        raise AgentToolError(code="INPUT_ERROR", message="market must be us or hk")
    return market


def resolve_config_path(raw: str | Path | None, *, default: Path) -> Path:
    if raw is None or not str(raw).strip():
        return default.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    return path


def deep_merge_config(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = deepcopy(base)
        for key, value in override.items():
            out[key] = deep_merge_config(out[key], value) if key in out else deepcopy(value)
        return out
    return deepcopy(override)


def config_key_parts(key: str) -> list[str]:
    parts = [part.strip() for part in str(key or "").split(".")]
    if not parts or any(not part for part in parts):
        raise AgentToolError(code="INPUT_ERROR", message="config key must be a non-empty dot path")
    return parts


def config_path_get(data: Any, parts: list[str]) -> tuple[bool, Any]:
    current = data
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index < 0 or index >= len(current):
                return False, None
            current = current[index]
            continue
        return False, None
    return True, current


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def path_for_metadata(path: Path, *, repo_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def dump_yaml(payload: dict[str, Any]) -> str:
    text = yaml.dump(
        payload,
        Dumper=IndentedYamlDumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        indent=2,
        width=100,
    )
    return text.rstrip() + "\n"
