from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable


def resolve_data_config_ref(payload: dict[str, Any], portfolio_cfg: dict[str, Any]) -> str | None:
    value = payload.get("data_config") or portfolio_cfg.get("data_config")
    raw = str(value or "").strip()
    return raw or None


def resolve_public_data_config_path(
    payload: dict[str, Any],
    portfolio_cfg: dict[str, Any],
    *,
    repo_base: Callable[[], Path],
) -> Path:
    raw = resolve_data_config_ref(payload, portfolio_cfg)
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (repo_base() / path).resolve()
        return path
    return (repo_base() / "secrets" / "portfolio.sqlite.json").resolve()


def resolve_local_path(value: Any, *, default: Path, repo_base: Callable[[], Path]) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return default.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (repo_base() / path).resolve()
    return path


def read_json_object_or_empty(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def read_json_file(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
