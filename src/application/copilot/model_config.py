from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.config_validator import validate_assistant_config
from src.application.config_yaml import default_yaml_assistant_config_path
from src.application.copilot.model_client import CopilotModelSettings
from src.application.llm_provider_registry import provider_requires_api_key
from src.application.secret_store import SecretProvider, resolve_secret_status


def load_assistant_llm_config(
    *,
    config_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    require_config: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    payload, load_error = _load_assistant_config(
        config_path=config_path,
        repo_root=repo_root,
        require_config=require_config,
    )
    if load_error or payload is None:
        return None, load_error

    assistant = payload.get("assistant")
    assistant_cfg = assistant if isinstance(assistant, dict) else {}
    if assistant_cfg.get("enabled") is False:
        return None, None
    llm = assistant_cfg.get("llm")
    llm_cfg = llm if isinstance(llm, dict) else {}
    if not str(llm_cfg.get("provider") or "").strip() or not str(llm_cfg.get("model") or "").strip():
        return None, None
    return dict(llm_cfg), None


def load_assistant_copilot_toolsets(
    *,
    config_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    require_config: bool = False,
) -> tuple[frozenset[str] | None, str | None]:
    payload, load_error = _load_assistant_config(
        config_path=config_path,
        repo_root=repo_root,
        require_config=require_config,
    )
    if load_error:
        return None, load_error
    assistant = (payload or {}).get("assistant")
    assistant_cfg = assistant if isinstance(assistant, dict) else {}
    if assistant_cfg.get("enabled") is False:
        return frozenset(), None
    copilot = assistant_cfg.get("copilot")
    copilot_cfg = copilot if isinstance(copilot, dict) else {}
    if copilot_cfg.get("enabled") is not True:
        return frozenset(), None
    toolsets = copilot_cfg.get("toolsets")
    toolset_cfg = toolsets if isinstance(toolsets, dict) else {}
    return frozenset(str(name) for name, enabled in toolset_cfg.items() if enabled is True), None


def _load_assistant_config(
    *,
    config_path: str | Path | None,
    repo_root: str | Path | None,
    require_config: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    path = _assistant_config_path(config_path=config_path, repo_root=repo_root)
    if not path.exists():
        if require_config:
            return None, "assistant_config_not_found"
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, "invalid_assistant_config"
    if not isinstance(payload, dict):
        return None, "invalid_assistant_config"
    try:
        validate_assistant_config(payload)
    except SystemExit:
        return None, "invalid_assistant_config"
    return payload, None


def model_api_key_configured(
    raw: dict[str, Any],
    *,
    environ: dict[str, str] | None = None,
    secret_provider: SecretProvider | None = None,
) -> tuple[bool, str | None]:
    try:
        settings = CopilotModelSettings.from_config(raw)
    except Exception:
        return False, "invalid_model_config"
    if not provider_requires_api_key(settings.provider):
        return True, None
    status = resolve_secret_status(
        settings.credential_name,
        provider=secret_provider,
        environ=environ,
        legacy_env_name=settings.api_key_env,
    )
    if not status.configured:
        return False, "model_api_key_missing"
    return True, None


def _assistant_config_path(
    *,
    config_path: str | Path | None,
    repo_root: str | Path | None,
) -> Path:
    if config_path is not None and str(config_path).strip():
        path = Path(config_path).expanduser()
        return path if path.is_absolute() else path.resolve()
    root = Path(repo_root).expanduser().resolve() if repo_root is not None else Path(__file__).resolve().parents[3]
    repo_local = (root / "config.assistant.json").resolve()
    if repo_local.exists():
        return repo_local
    return default_yaml_assistant_config_path(repo_root=root)
