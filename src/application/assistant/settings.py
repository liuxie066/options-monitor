from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.application.llm_provider_registry import provider_spec
from src.application.payload_helpers import as_dict as _dict


DEFAULT_LLM_API_KEY_ENV = "OM_LLM_API_KEY"
DEFAULT_LLM_CONFIDENCE_MIN = 0.75
DEFAULT_LLM_TIMEOUT_SECONDS = 90
DEFAULT_LLM_MAX_OUTPUT_TOKENS = 2048
DEFAULT_CONTEXT_WINDOW_MESSAGES = 8
DEFAULT_MARKET_SCOPE = ""
CONFIGURABLE_COPILOT_TOOLSETS = frozenset({"portfolio"})
COPILOT_TOOL_LOADING_MODES = frozenset({"eager", "directory"})


@dataclass(frozen=True)
class CopilotSettings:
    enabled: bool = False
    toolsets: frozenset[str] = frozenset()
    tool_loading_mode: str = "eager"

    def public_payload(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "toolsets": {
                name: name in self.toolsets
                for name in sorted(CONFIGURABLE_COPILOT_TOOLSETS)
            },
            "tool_loading_mode": self.tool_loading_mode,
        }


@dataclass(frozen=True)
class AssistantLlmSettings:
    enabled: bool = False
    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key_env: str = DEFAULT_LLM_API_KEY_ENV
    credential_name: str = ""
    confidence_min: float = DEFAULT_LLM_CONFIDENCE_MIN
    timeout_seconds: int = DEFAULT_LLM_TIMEOUT_SECONDS
    max_output_tokens: int = DEFAULT_LLM_MAX_OUTPUT_TOKENS

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
            "max_output_tokens": int(self.max_output_tokens),
        }


@dataclass(frozen=True)
class AssistantSettings:
    enabled: bool | None = None
    context_window_messages: int = DEFAULT_CONTEXT_WINDOW_MESSAGES
    default_market_scope: str = DEFAULT_MARKET_SCOPE
    copilot: CopilotSettings = CopilotSettings()
    llm: AssistantLlmSettings = AssistantLlmSettings()

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", self.enabled is not False)

    @classmethod
    def from_runtime_config(cls, cfg: dict[str, Any]) -> "AssistantSettings":
        assistant_cfg = _dict(cfg.get("assistant"))
        enabled = _assistant_enabled(assistant_cfg)
        copilot_cfg = _dict(assistant_cfg.get("copilot"))
        copilot_toolsets = _dict(copilot_cfg.get("toolsets"))
        configured_copilot = CopilotSettings(
            enabled=_bool(copilot_cfg.get("enabled"), default=False),
            toolsets=frozenset(
                name
                for name in CONFIGURABLE_COPILOT_TOOLSETS
                if _bool(copilot_toolsets.get(name), default=False)
            ),
            tool_loading_mode=_tool_loading_mode(copilot_cfg.get("tool_loading_mode")),
        )
        llm_cfg = _dict(assistant_cfg.get("llm"))
        return cls(
            enabled=enabled,
            context_window_messages=_int(
                assistant_cfg.get("context_window_messages"),
                default=DEFAULT_CONTEXT_WINDOW_MESSAGES,
                minimum=0,
                maximum=20,
            ),
            default_market_scope=_market_scope(assistant_cfg.get("default_market_scope")),
            copilot=configured_copilot,
            llm=_llm_settings(llm_cfg, enabled=bool(enabled and configured_copilot.enabled)),
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "context_window_messages": int(self.context_window_messages),
            "default_market_scope": self.default_market_scope,
            "copilot": self.copilot.public_payload(),
            "llm": self.llm.public_payload(),
        }

    @property
    def enabled_copilot_toolsets(self) -> frozenset[str]:
        if not self.enabled or not self.copilot.enabled:
            return frozenset()
        return self.copilot.toolsets


def _bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return bool(default)


def _assistant_enabled(assistant_cfg: dict[str, Any]) -> bool:
    if "enabled" in assistant_cfg:
        return _bool(assistant_cfg.get("enabled"), default=True)
    return True


def _market_scope(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"us", "hk", "all"}:
        return text
    return DEFAULT_MARKET_SCOPE


def _tool_loading_mode(value: Any) -> str:
    mode = str(value or "eager").strip().lower()
    return mode if mode in COPILOT_TOOL_LOADING_MODES else "eager"


def _llm_settings(llm_cfg: dict[str, Any], *, enabled: bool) -> AssistantLlmSettings:
    provider = str(llm_cfg.get("provider") or "").strip()
    model = str(llm_cfg.get("model") or "").strip()
    spec = provider_spec(provider)
    default_api_key_env = spec.default_api_key_env if spec is not None else DEFAULT_LLM_API_KEY_ENV
    raw_api_key_env = llm_cfg.get("api_key_env")
    return AssistantLlmSettings(
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
        max_output_tokens=_int(
            llm_cfg.get("max_output_tokens"),
            default=DEFAULT_LLM_MAX_OUTPUT_TOKENS,
            minimum=64,
            maximum=4096,
        ),
    )


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
