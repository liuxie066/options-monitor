from __future__ import annotations

import re
import shutil
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.llm_provider_registry import (
    provider_catalog_payload,
    provider_requires_api_key,
    require_provider_spec,
)
from src.application.config_primitives import dump_yaml
from src.application.settings import build_effective_env
from src.application.write_contract import attach_write_contract
from src.infrastructure.io_utils import atomic_write_text


PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SECRET_KEYS = {"api_key", "api_key_value", "secret", "token", "bearer_token"}


@dataclass(frozen=True)
class LlmModelProfile:
    name: str
    provider: str
    model: str
    base_url: str
    api_key_env: str
    confidence_min: float | None = None
    timeout_seconds: int | None = None
    max_output_tokens: int | None = None

    def llm_config(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
        }
        if self.confidence_min is not None:
            out["confidence_min"] = float(self.confidence_min)
        if self.timeout_seconds is not None:
            out["timeout_seconds"] = int(self.timeout_seconds)
        if self.max_output_tokens is not None:
            out["max_output_tokens"] = int(self.max_output_tokens)
        return out

    def public_payload(self, *, active: bool = False, api_key_configured: bool | None = None) -> dict[str, Any]:
        out = {
            "name": self.name,
            "active": bool(active),
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "confidence_min": self.confidence_min,
            "timeout_seconds": self.timeout_seconds,
            "max_output_tokens": self.max_output_tokens,
        }
        if api_key_configured is not None:
            out["api_key_configured"] = bool(api_key_configured)
        return out


def normalize_model_profile_name(name: str, *, path: str = "model profile name") -> str:
    value = str(name or "").strip()
    if not PROFILE_NAME_RE.match(value):
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"{path} must use 1-64 letters, numbers, dots, underscores, or dashes",
            details={"value": value or None},
        )
    return value


def resolve_authoring_assistant_config(assistant_cfg: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    assistant = deepcopy(assistant_cfg if isinstance(assistant_cfg, dict) else {})
    warnings: list[str] = []
    copilot = assistant.get("copilot")
    if isinstance(copilot, dict):
        retired_keys = sorted(key for key in ("channel_scenes", "human_review") if key in copilot)
        if retired_keys:
            assistant["copilot"] = {key: value for key, value in copilot.items() if key not in retired_keys}
            warnings.append(f"retired assistant.copilot keys omitted: {', '.join(retired_keys)}")
    models_raw = assistant.pop("models", None)
    active_model_raw = assistant.pop("active_model", None)
    copilot_enabled = _assistant_copilot_enabled(assistant)

    if models_raw is None and active_model_raw is None:
        return assistant, {
            "model_profiles_enabled": False,
            "active_model": None,
            "resolved_profile": None,
            "warnings": warnings,
        }

    profiles = parse_model_profiles(models_raw)
    active_model = str(active_model_raw or "").strip()
    if not active_model:
        if copilot_enabled:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="assistant.active_model is required when assistant.models is configured and assistant.copilot.enabled is true",
            )
        return assistant, {
            "model_profiles_enabled": True,
            "active_model": None,
            "resolved_profile": None,
            "profile_count": len(profiles),
            "warnings": ["assistant.models configured without active_model; assistant Copilot is disabled so assistant.llm is unchanged"],
        }

    active_model = normalize_model_profile_name(active_model, path="assistant.active_model")
    profile = profiles.get(active_model)
    if profile is None:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"assistant.active_model references unknown model profile: {active_model}",
            details={"active_model": active_model, "available_models": sorted(profiles)},
        )
    if "llm" in assistant and isinstance(assistant.get("llm"), dict):
        warnings.append("assistant.active_model is set; resolved profile overrides assistant.llm in runtime config")
    assistant["llm"] = profile.llm_config()
    return assistant, {
        "model_profiles_enabled": True,
        "active_model": active_model,
        "resolved_profile": profile.public_payload(active=True),
        "profile_count": len(profiles),
        "warnings": warnings,
    }


def _assistant_copilot_enabled(assistant: dict[str, Any]) -> bool:
    if isinstance(assistant.get("enabled"), bool) and assistant.get("enabled") is False:
        return False
    copilot = assistant.get("copilot")
    return bool(isinstance(copilot, dict) and copilot.get("enabled") is True)


def parse_model_profiles(raw_models: Any) -> dict[str, LlmModelProfile]:
    if raw_models is None:
        return {}
    if not isinstance(raw_models, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="assistant.models must be an object keyed by model profile name")
    profiles: dict[str, LlmModelProfile] = {}
    for raw_name, raw_profile in raw_models.items():
        name = normalize_model_profile_name(str(raw_name), path="assistant.models.<key>")
        if name in profiles:
            raise AgentToolError(code="CONFIG_ERROR", message=f"duplicate assistant model profile after normalization: {name}")
        profiles[name] = parse_model_profile(name, raw_profile, path=f"assistant.models.{name}")
    return profiles


def parse_model_profile(name: str, raw_profile: Any, *, path: str = "assistant.models.<profile>") -> LlmModelProfile:
    if not isinstance(raw_profile, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be an object")
    _reject_secret_material(raw_profile, path=path)
    provider = str(raw_profile.get("provider") or "").strip().lower()
    spec = require_provider_spec(provider, path=f"{path}.provider")
    model = str(raw_profile.get("model") or "").strip()
    if not model:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path}.model is required")
    api_key_env = str(raw_profile.get("api_key_env") or spec.default_api_key_env).strip()
    if provider_requires_api_key(spec.provider_id) and not api_key_env:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path}.api_key_env is required")
    base_url = str(raw_profile.get("base_url") or spec.default_base_url).strip()
    return LlmModelProfile(
        name=name,
        provider=spec.provider_id,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        confidence_min=_optional_float(raw_profile.get("confidence_min"), path=f"{path}.confidence_min"),
        timeout_seconds=_optional_int(raw_profile.get("timeout_seconds"), path=f"{path}.timeout_seconds"),
        max_output_tokens=_optional_int(raw_profile.get("max_output_tokens"), path=f"{path}.max_output_tokens"),
    )


def configured_model_profiles_payload(
    *,
    config_doc: dict[str, Any],
    repo_root: str | Path,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
) -> dict[str, Any]:
    assistant = config_doc.get("assistant")
    assistant_cfg = assistant if isinstance(assistant, dict) else {}
    profiles = parse_model_profiles(assistant_cfg.get("models"))
    active_model = str(assistant_cfg.get("active_model") or "").strip() or None
    effective_env = build_effective_env(
        repo_root=repo_root,
        env_file=env_file,
        include_local_env_file=include_local_env_file,
    )
    items = [
        profile.public_payload(
            active=profile.name == active_model,
            api_key_configured=(
                not provider_requires_api_key(profile.provider)
                or bool(effective_env.get(profile.api_key_env))
            ),
        )
        for profile in profiles.values()
    ]
    return {
        "summary": {
            "profile_count": len(items),
            "active_model": active_model,
            "active_model_exists": bool(active_model in profiles) if active_model else False,
        },
        "env": {
            "env_file": str(effective_env.env_file) if effective_env.env_file is not None else None,
            "env_file_loaded": bool(effective_env.env_file_loaded),
            "warnings": list(effective_env.warnings),
        },
        "models": sorted(items, key=lambda item: str(item["name"])),
    }


def current_model_payload(
    *,
    config_doc: dict[str, Any],
    runtime_assistant_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assistant = config_doc.get("assistant")
    assistant_cfg = assistant if isinstance(assistant, dict) else {}
    profiles = parse_model_profiles(assistant_cfg.get("models"))
    active_model = str(assistant_cfg.get("active_model") or "").strip() or None
    active_profile = profiles.get(active_model) if active_model else None
    runtime_assistant = (
        runtime_assistant_config.get("assistant")
        if isinstance(runtime_assistant_config, dict) and isinstance(runtime_assistant_config.get("assistant"), dict)
        else {}
    )
    runtime_llm = runtime_assistant.get("llm") if isinstance(runtime_assistant, dict) and isinstance(runtime_assistant.get("llm"), dict) else {}
    authoring_llm = active_profile.llm_config() if active_profile is not None else (assistant_cfg.get("llm") if isinstance(assistant_cfg.get("llm"), dict) else {})
    drift = bool(runtime_llm) and bool(authoring_llm) and _llm_identity(runtime_llm) != _llm_identity(authoring_llm)
    return {
        "summary": {
            "active_model": active_model,
            "active_model_exists": bool(active_profile is not None) if active_model else False,
            "runtime_loaded": bool(runtime_assistant_config),
            "drift": bool(drift),
        },
        "authoring": {
            "active_model": active_model,
            "profile": active_profile.public_payload(active=True) if active_profile is not None else None,
            "llm": deepcopy(authoring_llm),
        },
        "runtime": {
            "mode": runtime_assistant.get("mode") if isinstance(runtime_assistant, dict) else None,
            "llm": deepcopy(runtime_llm),
        },
        "hint": "run `om config build-assistant --source yaml` after switching models" if drift else None,
    }


def add_model_profile_to_config(
    config_doc: dict[str, Any],
    *,
    name: str,
    provider: str,
    model: str,
    base_url: str | None = None,
    api_key_env: str | None = None,
    confidence_min: float | None = None,
    timeout_seconds: int | None = None,
    max_output_tokens: int | None = None,
    replace: bool = False,
    activate: bool = False,
) -> tuple[dict[str, Any], LlmModelProfile]:
    out = deepcopy(config_doc)
    assistant = out.setdefault("assistant", {})
    if not isinstance(assistant, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="assistant must be an object")
    models = assistant.setdefault("models", {})
    if not isinstance(models, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="assistant.models must be an object keyed by model profile name")
    profile_name = normalize_model_profile_name(name)
    if profile_name in models and not replace:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"assistant model profile already exists: {profile_name}",
            hint="Use --replace to update an existing profile.",
        )
    raw_profile: dict[str, Any] = {"provider": provider, "model": model}
    spec = require_provider_spec(provider, path="--provider")
    raw_profile["base_url"] = str(base_url if base_url is not None else spec.default_base_url).strip()
    raw_profile["api_key_env"] = str(api_key_env if api_key_env is not None else spec.default_api_key_env).strip()
    for key, value in {
        "confidence_min": confidence_min,
        "timeout_seconds": timeout_seconds,
        "max_output_tokens": max_output_tokens,
    }.items():
        if value is not None:
            raw_profile[key] = value
    profile = parse_model_profile(profile_name, raw_profile, path=f"assistant.models.{profile_name}")
    models[profile_name] = profile.llm_config()
    if activate:
        assistant["active_model"] = profile_name
    return out, profile


def switch_active_model_profile(config_doc: dict[str, Any], *, name: str) -> tuple[dict[str, Any], LlmModelProfile]:
    out = deepcopy(config_doc)
    assistant = out.get("assistant")
    if not isinstance(assistant, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="assistant must be an object with assistant.models")
    profiles = parse_model_profiles(assistant.get("models"))
    profile_name = normalize_model_profile_name(name, path="assistant.active_model")
    profile = profiles.get(profile_name)
    if profile is None:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"assistant model profile does not exist: {profile_name}",
            details={"available_models": sorted(profiles)},
        )
    assistant["active_model"] = profile_name
    return out, profile


def write_model_config_update(
    *,
    config_path: str | Path,
    before_doc: dict[str, Any],
    after_doc: dict[str, Any],
    apply: bool,
    action: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    path = Path(config_path).expanduser().resolve()
    yaml_text = dump_yaml(after_doc)
    backup_path = None
    if apply:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = _backup_existing_config(path)
        atomic_write_text(path, yaml_text, encoding="utf-8")
    return attach_write_contract(
        {
            "ok": True,
            "action": action,
            "config_yaml_path": str(path),
            "changed": before_doc != after_doc,
            **payload,
            "yaml": yaml_text,
        },
        dry_run=not bool(apply),
        write_applied=bool(apply),
        backup_path=backup_path,
        rollback_hint=f"restore {backup_path} to {path}" if backup_path else f"rerun the command or edit {path}",
    )


def model_catalog() -> dict[str, Any]:
    return provider_catalog_payload()


def _reject_secret_material(raw_profile: dict[str, Any], *, path: str) -> None:
    for key, value in raw_profile.items():
        key_text = str(key or "").strip()
        if key_text in SECRET_KEYS:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path}.{key_text} must not store secret values; use api_key_env instead",
            )
        if isinstance(value, str) and value.strip().startswith("sk-"):
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path}.{key_text} looks like an API key; store it in an env file and reference api_key_env",
            )


def _optional_float(value: Any, *, path: str) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = float(value)
    except Exception as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be a number") from exc
    if not 0 <= parsed <= 1:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be between 0 and 1")
    return parsed


def _optional_int(value: Any, *, path: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(value)
    except Exception as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be an integer") from exc
    if path.endswith("timeout_seconds") and not 1 <= parsed <= 120:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be between 1 and 120")
    if path.endswith("max_output_tokens") and not 64 <= parsed <= 4096:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be between 64 and 4096")
    return parsed


def _llm_identity(raw: dict[str, Any]) -> dict[str, str]:
    return {
        "provider": str(raw.get("provider") or "").strip().lower(),
        "base_url": str(raw.get("base_url") or "").strip(),
        "model": str(raw.get("model") or "").strip(),
        "api_key_env": str(raw.get("api_key_env") or "").strip(),
    }


def _backup_existing_config(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak.{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


__all__ = [
    "LlmModelProfile",
    "add_model_profile_to_config",
    "configured_model_profiles_payload",
    "current_model_payload",
    "model_catalog",
    "normalize_model_profile_name",
    "parse_model_profile",
    "parse_model_profiles",
    "resolve_authoring_assistant_config",
    "switch_active_model_profile",
    "write_model_config_update",
]
