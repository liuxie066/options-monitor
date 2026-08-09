from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from src.application.settings import EffectiveEnv, SettingSource, build_effective_env
from src.application.secret_store import inspect_secret_credentials, legacy_secret_env_names


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
    "MOONSHOT_API_KEY",
    "KIMI_API_KEY",
)

_SECRET_NAME_PARTS = ("SECRET", "TOKEN", "PASSWORD", "PRIVATE_KEY", "API_KEY")
_REGISTERED_SECRET_ENV_NAMES = legacy_secret_env_names()


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
    secret_credentials = inspect_secret_credentials(environ=effective.values)
    secret_summary = secret_credentials.get("summary") if isinstance(secret_credentials, dict) else {}
    secret_backend = secret_summary.get("backend") if isinstance(secret_summary, dict) else None
    entries: dict[str, dict[str, Any]] = {}
    for key in keys:
        source = effective.source_of(key)
        secret_name = _is_secret_name(key)
        active_env_secret = not secret_name or secret_backend == "env"
        entries[key] = {
            "configured": bool(effective.get(key)) and active_env_secret,
            **({"legacy_env_configured": bool(effective.get(key))} if secret_name else {}),
            "source": (
                _public_source(source, mask_path=mask_path)
                if source is not None and active_env_secret
                else "ignored_legacy_env"
                if source is not None
                else None
            ),
            "secret": secret_name,
        }
    return {
        "env_file": mask_path(effective.env_file) if effective.env_file is not None else None,
        "env_file_loaded": bool(effective.env_file_loaded),
        "warnings": ["env settings warning" for _item in effective.warnings],
        "entries": entries,
        "secret_credentials": secret_credentials,
    }


def _public_source(source: SettingSource, *, mask_path: Callable[[Any], str | None]) -> str:
    if not source.path:
        return source.source
    masked = mask_path(source.path) or "..."
    return f"{source.source}:{masked}"


def _is_secret_name(name: str) -> bool:
    upper = str(name or "").upper()
    return upper in _REGISTERED_SECRET_ENV_NAMES or any(
        part in upper for part in _SECRET_NAME_PARTS
    )
