from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.application.llm_provider_registry import provider_spec
from src.application.payload_helpers import as_dict as _dict


DEFAULT_LLM_API_KEY_ENV = "OM_LLM_API_KEY"
DEFAULT_LLM_CONFIDENCE_MIN = 0.75
DEFAULT_LLM_TIMEOUT_SECONDS = 90
DEFAULT_LLM_MAX_OUTPUT_TOKENS = None
DEFAULT_CONTEXT_WINDOW_MESSAGES = 8
DEFAULT_MARKET_SCOPE = ""


@dataclass(frozen=True)
class BotLlmSettings:
    enabled: bool = False
    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key_env: str = DEFAULT_LLM_API_KEY_ENV
    credential_name: str = ""
    confidence_min: float = DEFAULT_LLM_CONFIDENCE_MIN
    timeout_seconds: int = DEFAULT_LLM_TIMEOUT_SECONDS
    max_output_tokens: int | None = DEFAULT_LLM_MAX_OUTPUT_TOKENS

    def public_payload(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "credential_name": self.credential_name,
            "confidence_min": float(self.confidence_min),
            "timeout_seconds": int(self.timeout_seconds),
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True)
class BotSettings:
    enabled: bool = False
    context_window_messages: int = DEFAULT_CONTEXT_WINDOW_MESSAGES
    default_market_scope: str = DEFAULT_MARKET_SCOPE
    llm: BotLlmSettings = BotLlmSettings()

    @classmethod
    def from_runtime_config(cls, cfg: dict[str, Any]) -> "BotSettings":
        if "assistant" in cfg:
            raise ValueError("assistant is retired; run om config migrate-switches")
        bot_cfg = _dict(cfg.get("bot"))
        if "copilot" in bot_cfg:
            raise ValueError("assistant.copilot is retired; run ./om bot migrate --dry-run")
        enabled = _bot_enabled(bot_cfg)
        llm_cfg = _dict(bot_cfg.get("llm"))
        return cls(
            enabled=enabled,
            context_window_messages=_int(
                bot_cfg.get("context_window_messages"),
                default=DEFAULT_CONTEXT_WINDOW_MESSAGES,
                minimum=0,
                maximum=20,
            ),
            default_market_scope=_market_scope(bot_cfg.get("default_market_scope")),
            llm=_llm_settings(llm_cfg, enabled=enabled),
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "context_window_messages": int(self.context_window_messages),
            "default_market_scope": self.default_market_scope,
            "llm": self.llm.public_payload(),
        }



def _bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return bool(default)


def _bot_enabled(bot_cfg: dict[str, Any]) -> bool:
    if "enabled" in bot_cfg:
        return _bool(bot_cfg.get("enabled"), default=False)
    return False


def _market_scope(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"us", "hk", "all"}:
        return text
    return DEFAULT_MARKET_SCOPE


def _llm_settings(llm_cfg: dict[str, Any], *, enabled: bool) -> BotLlmSettings:
    provider = str(llm_cfg.get("provider") or "").strip()
    model = str(llm_cfg.get("model") or "").strip()
    spec = provider_spec(provider)
    default_api_key_env = spec.default_api_key_env if spec is not None else DEFAULT_LLM_API_KEY_ENV
    raw_api_key_env = llm_cfg.get("api_key_env")
    return BotLlmSettings(
        enabled=bool(enabled and provider and model),
        provider=provider,
        base_url=str(llm_cfg.get("base_url") or "").strip(),
        model=model,
        api_key_env=default_api_key_env if raw_api_key_env is None else str(raw_api_key_env).strip(),
        credential_name=(
            spec.credential_name
            if spec is not None and spec.requires_api_key
            else ""
        ),
        confidence_min=_float(llm_cfg.get("confidence_min"), default=DEFAULT_LLM_CONFIDENCE_MIN),
        timeout_seconds=_int(
            llm_cfg.get("timeout_seconds"),
            default=DEFAULT_LLM_TIMEOUT_SECONDS,
            minimum=1,
            maximum=120,
        ),
        max_output_tokens=_output_limit(llm_cfg.get("max_output_tokens")),
    )


def _output_limit(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 64:
        raise ValueError("bot.llm.max_output_tokens must be an integer >= 64 or null")
    return value


def _float(value: Any, *, default: float) -> float:
    if value is None or str(value).strip() == "":
        return float(default)
    try:
        return float(value)
    except Exception:
        return float(default)


def _int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None or str(value).strip() == "":
        return int(default)
    try:
        parsed = int(value)
    except Exception:
        return int(default)
    return max(int(minimum), min(parsed, int(maximum)))
