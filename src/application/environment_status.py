from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from src.application.settings import EffectiveEnv, SettingSource, build_effective_env


ENV_DIAGNOSTIC_KEYS = (
    "OM_RUNTIME_ROOT",
    "OM_DATA_CONFIG",
    "OM_FEISHU_APP_ID",
    "OM_FEISHU_APP_SECRET",
    "OM_FEISHU_HOLDINGS_TABLE",
    "OM_FEISHU_BOT_APP_ID",
    "OM_FEISHU_BOT_APP_SECRET",
    "OM_FEISHU_BOT_USER_OPEN_ID",
    "OM_FEISHU_BOT_ALLOWED_OPEN_IDS",
    "OM_INBOUND_AUDIT_DB",
    "OM_INBOUND_OPERATION_HMAC_KEY",
    "OM_LLM_API_KEY",
    "DEEPSEEK_API_KEY",
)

_SECRET_NAME_PARTS = ("SECRET", "TOKEN", "PASSWORD", "PRIVATE_KEY", "API_KEY")


def build_effective_env_with_status(
    *,
    repo_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = False,
    mask_path: Callable[[Any], str | None],
    keys: tuple[str, ...] = ENV_DIAGNOSTIC_KEYS,
) -> tuple[EffectiveEnv, dict[str, Any]]:
    effective = build_effective_env(
        repo_root=repo_root,
        environ=environ,
        env_file=env_file,
        include_local_env_file=include_local_env_file,
    )
    return effective, summarize_effective_env(effective, mask_path=mask_path, keys=keys)


def summarize_effective_env(
    effective: EffectiveEnv,
    *,
    mask_path: Callable[[Any], str | None],
    keys: tuple[str, ...] = ENV_DIAGNOSTIC_KEYS,
) -> dict[str, Any]:
    entries: dict[str, dict[str, Any]] = {}
    for key in keys:
        source = effective.source_of(key)
        entries[key] = {
            "configured": bool(effective.get(key)),
            "source": _public_source(source, mask_path=mask_path) if source is not None else None,
            "secret": _is_secret_name(key),
        }
    return {
        "env_file": mask_path(effective.env_file) if effective.env_file is not None else None,
        "env_file_loaded": bool(effective.env_file_loaded),
        "warnings": list(effective.warnings),
        "entries": entries,
    }


def _public_source(source: SettingSource, *, mask_path: Callable[[Any], str | None]) -> str:
    if not source.path:
        return source.source
    masked = mask_path(source.path) or "..."
    return f"{source.source}:{masked}"


def _is_secret_name(name: str) -> bool:
    upper = str(name or "").upper()
    return any(part in upper for part in _SECRET_NAME_PARTS)
