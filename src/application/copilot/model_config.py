from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.application.config_validator import validate_assistant_config
from src.application.config_yaml import default_yaml_assistant_config_path
from src.application.llm_provider_registry import (
    provider_requires_api_key,
    require_provider_spec,
)
from src.application.secret_store import SecretProvider, resolve_secret_status


_PI_API_KINDS = {
    "responses": "openai-responses",
    "chat_completions": "openai-completions",
}


@dataclass(frozen=True)
class PiModelSettings:
    provider: str
    api_kind: str
    model: str
    base_url: str
    api_key_env: str
    credential_name: str
    timeout_seconds: int
    context_window_tokens: int
    max_output_tokens: int
    max_attempts: int

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> "PiModelSettings":
        if not isinstance(raw, dict):
            raise ValueError("model config must be an object")
        provider = str(raw.get("provider") or "").strip().lower()
        spec = require_provider_spec(provider, path="copilot.model.provider")
        model = str(raw.get("model") or "").strip()
        if not model:
            raise ValueError("copilot.model.model is required")
        base_url = str(raw.get("base_url") or spec.default_base_url).strip()
        if spec.provider_id == "openai" and not base_url:
            base_url = "https://api.openai.com/v1"
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("copilot.model.base_url must be an HTTP(S) URL")

        timeout_seconds = _strict_int(
            raw.get("timeout_seconds", 90),
            path="copilot.model.timeout_seconds",
            minimum=1,
            maximum=120,
        )
        max_output_tokens = _strict_int(
            raw.get("max_output_tokens", 2048),
            path="copilot.model.max_output_tokens",
            minimum=64,
            maximum=4096,
        )
        context_window_tokens = _strict_int(
            raw.get("context_window_tokens"),
            path="copilot.model.context_window_tokens",
            minimum=4096,
            maximum=2_000_000,
        )
        if context_window_tokens <= max_output_tokens + 2_000:
            raise ValueError(
                "copilot.model.context_window_tokens must exceed max_output_tokens by more than 2000"
            )
        return cls(
            provider=spec.provider_id,
            api_kind=_PI_API_KINDS[spec.api_kind],
            model=model,
            base_url=base_url,
            api_key_env=str(raw.get("api_key_env") or spec.default_api_key_env).strip(),
            credential_name=spec.credential_name,
            timeout_seconds=timeout_seconds,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
            max_attempts=_strict_int(
                raw.get("max_attempts", 2),
                path="copilot.model.max_attempts",
                minimum=1,
                maximum=3,
            ),
        )

    def process_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "api_kind": self.api_kind,
            "model": self.model,
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
            "context_window_tokens": self.context_window_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_attempts": self.max_attempts,
        }


def _strict_int(value: Any, *, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{path} must be between {minimum} and {maximum}")
    return value


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
        settings = PiModelSettings.from_config(raw)
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
