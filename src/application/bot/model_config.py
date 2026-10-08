from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_validator import validate_bot_config
from src.application.config_yaml import default_yaml_bot_config_path
from src.application.llm_provider_registry import (
    provider_requires_api_key,
    require_provider_spec,
    resolve_output_reservation,
)
from src.application.secret_store import SecretProvider, resolve_secret, resolve_secret_status


_API_KINDS = {
    "responses": "openai-responses",
    "chat_completions": "openai-completions",
}


@dataclass(frozen=True)
class ModelSettings:
    provider: str
    api_kind: str
    model: str
    base_url: str
    api_key_env: str
    credential_name: str
    timeout_seconds: int
    context_window_tokens: int
    max_output_tokens: int | None
    max_attempts: int

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> "ModelSettings":
        if not isinstance(raw, dict):
            raise ValueError("model config must be an object")
        provider = str(raw.get("provider") or "").strip().lower()
        spec = require_provider_spec(provider, path="bot.model.provider")
        model = str(raw.get("model") or "").strip()
        if not model:
            raise ValueError("bot.model.model is required")
        base_url = str(raw.get("base_url") or spec.default_base_url).strip()
        if spec.provider_id == "openai" and not base_url:
            base_url = "https://api.openai.com/v1"
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("bot.model.base_url must be an HTTP(S) URL")

        timeout_seconds = _strict_int(
            raw.get("timeout_seconds", 90),
            path="bot.model.timeout_seconds",
            minimum=1,
            maximum=120,
        )
        max_output_tokens = raw.get("max_output_tokens")
        context_window_tokens = _strict_int(
            raw.get("context_window_tokens"),
            path="bot.model.context_window_tokens",
            minimum=4096,
            maximum=2_000_000,
        )
        resolve_output_reservation(
            spec.provider_id, model, context_window_tokens, max_output_tokens, path="bot.model"
        )
        return cls(
            provider=spec.provider_id,
            api_kind=_API_KINDS[spec.api_kind],
            model=model,
            base_url=base_url,
            api_key_env=str(raw.get("api_key_env") or spec.default_api_key_env).strip(),
            credential_name=spec.credential_name,
            timeout_seconds=timeout_seconds,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
            max_attempts=_strict_int(
                raw.get("max_attempts", 2),
                path="bot.model.max_attempts",
                minimum=1,
                maximum=3,
            ),
        )

    @property
    def output_reservation_tokens(self) -> int:
        return resolve_output_reservation(
            self.provider, self.model, self.context_window_tokens, self.max_output_tokens,
            path="bot.model",
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
            "output_reservation_tokens": self.output_reservation_tokens,
            "max_attempts": self.max_attempts,
        }


def _strict_int(value: Any, *, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{path} must be between {minimum} and {maximum}")
    return value


def load_bot_llm_config(
    *,
    config_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    require_config: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    payload, load_error = _load_bot_config(
        config_path=config_path,
        repo_root=repo_root,
        require_config=require_config,
    )
    if load_error or payload is None:
        return None, load_error

    bot_config = payload.get("bot")
    bot_cfg = bot_config if isinstance(bot_config, dict) else {}
    if bot_cfg.get("enabled") is not True:
        return None, None
    llm = bot_cfg.get("llm")
    llm_cfg = llm if isinstance(llm, dict) else {}
    if not str(llm_cfg.get("provider") or "").strip() or not str(llm_cfg.get("model") or "").strip():
        return None, None
    return dict(llm_cfg), None


def load_bot_read_scope(*, config_path: str | Path, primary_market: str) -> tuple[frozenset[str], str]:
    """Validated channel grant and config generation; never sourced from a model turn."""
    if primary_market not in {"us", "hk"}:
        raise ValueError("invalid primary market")
    path = _bot_config_path(config_path=config_path, repo_root=None)
    payload, error = _load_bot_config(config_path=config_path, repo_root=None, require_config=True)
    if error or payload is None:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"Bot config validation failed: {path}",
            details={"error": error or "invalid_bot_config"},
        )
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size) or json.loads(raw) != payload:
        raise ValueError("Bot config changed while reading")
    bot = payload.get("bot") or {}
    configured = bot.get("read_markets")
    markets = frozenset(configured if configured is not None else (primary_market,))
    if primary_market not in markets:
        raise ValueError("bot.read_markets must include the channel market")
    generated = payload.get("_generated") or {}
    generation = hashlib.sha256(json.dumps([
        str(path.resolve()), hashlib.sha256(raw).hexdigest(),
        generated.get("generated_at"), after.st_mtime_ns,
    ], sort_keys=True).encode()).hexdigest()
    return markets, generation


def bot_config_error(
    *, config_path: str | Path | None = None, repo_root: str | Path | None = None,
    require_config: bool = False,
) -> str | None:
    """Validate the Bot configuration at the Host boundary."""
    payload, error = _load_bot_config(
        config_path=config_path, repo_root=repo_root, require_config=require_config,
    )
    if error or payload is None:
        return error
    bot_config = payload.get("bot") or {}
    if bot_config.get("enabled") is not True:
        return "bot_disabled"
    return None


def _load_bot_config(
    *,
    config_path: str | Path | None,
    repo_root: str | Path | None,
    require_config: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    path = _bot_config_path(config_path=config_path, repo_root=repo_root)
    if not path.exists():
        if require_config:
            return None, "bot_config_not_found"
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, "invalid_bot_config"
    if not isinstance(payload, dict):
        return None, "invalid_bot_config"
    try:
        validate_bot_config(payload)
    except SystemExit:
        return None, "invalid_bot_config"
    return payload, None


def model_api_key_configured(
    raw: dict[str, Any],
    *,
    environ: dict[str, str] | None = None,
    secret_provider: SecretProvider | None = None,
) -> tuple[bool, str | None]:
    try:
        settings = ModelSettings.from_config(raw)
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


def _resolve_model_api_key(
    settings: ModelSettings,
    *,
    environ: dict[str, str] | None = None,
    secret_provider: SecretProvider | None = None,
) -> str | None:
    if not provider_requires_api_key(settings.provider):
        return None
    return resolve_secret(
        settings.credential_name,
        provider=secret_provider,
        environ=environ,
        legacy_env_name=settings.api_key_env,
    )


def _bot_config_path(
    *,
    config_path: str | Path | None,
    repo_root: str | Path | None,
) -> Path:
    if config_path is not None and str(config_path).strip():
        path = Path(config_path).expanduser()
        return path if path.is_absolute() else path.resolve()
    root = Path(repo_root).expanduser().resolve() if repo_root is not None else Path(__file__).resolve().parents[3]
    repo_local = (root / "config.bot.json").resolve()
    if repo_local.exists():
        return repo_local
    return default_yaml_bot_config_path(repo_root=root)
