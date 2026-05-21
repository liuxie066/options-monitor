from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_LLM_API_KEY_ENV = "OM_LLM_API_KEY"
DEFAULT_LLM_CONFIDENCE_MIN = 0.75
DEFAULT_LLM_TIMEOUT_SECONDS = 20
DEFAULT_LLM_MAX_OUTPUT_TOKENS = 512
DEFAULT_CONTEXT_WINDOW_MESSAGES = 8


@dataclass(frozen=True)
class LlmTranslatorSettings:
    enabled: bool = False
    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key_env: str = DEFAULT_LLM_API_KEY_ENV
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
            "confidence_min": float(self.confidence_min),
            "timeout_seconds": int(self.timeout_seconds),
            "max_output_tokens": int(self.max_output_tokens),
        }


@dataclass(frozen=True)
class AgentRuntimeSettings:
    enabled: bool = True
    context_window_messages: int = DEFAULT_CONTEXT_WINDOW_MESSAGES
    llm: LlmTranslatorSettings = LlmTranslatorSettings()

    @classmethod
    def from_runtime_config(cls, cfg: dict[str, Any]) -> "AgentRuntimeSettings":
        agent_cfg = _dict(cfg.get("agent"))
        runtime_cfg = _dict(agent_cfg.get("runtime"))
        llm_cfg = _dict(agent_cfg.get("llm"))
        return cls(
            enabled=_bool(runtime_cfg.get("enabled"), default=True),
            context_window_messages=_int(
                runtime_cfg.get("context_window_messages"),
                default=DEFAULT_CONTEXT_WINDOW_MESSAGES,
                minimum=0,
                maximum=20,
            ),
            llm=LlmTranslatorSettings(
                enabled=_bool(llm_cfg.get("enabled"), default=False),
                provider=str(llm_cfg.get("provider") or "").strip(),
                base_url=str(llm_cfg.get("base_url") or "").strip(),
                model=str(llm_cfg.get("model") or "").strip(),
                api_key_env=str(llm_cfg.get("api_key_env") or DEFAULT_LLM_API_KEY_ENV).strip()
                or DEFAULT_LLM_API_KEY_ENV,
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
            ),
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "context_window_messages": int(self.context_window_messages),
            "llm": self.llm.public_payload(),
        }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return bool(default)


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
