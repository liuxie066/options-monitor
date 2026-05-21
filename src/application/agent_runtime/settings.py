from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_LLM_API_KEY_ENV = "OM_LLM_API_KEY"
DEFAULT_LLM_CONFIDENCE_MIN = 0.75
DEFAULT_CONTEXT_WINDOW_MESSAGES = 8


@dataclass(frozen=True)
class LlmTranslatorSettings:
    enabled: bool = False
    provider: str = ""
    model: str = ""
    api_key_env: str = DEFAULT_LLM_API_KEY_ENV
    confidence_min: float = DEFAULT_LLM_CONFIDENCE_MIN

    def public_payload(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "provider": self.provider,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "confidence_min": float(self.confidence_min),
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
            enabled=_bool(runtime_cfg.get("enabled"), default=False),
            context_window_messages=_int(
                runtime_cfg.get("context_window_messages"),
                default=DEFAULT_CONTEXT_WINDOW_MESSAGES,
                minimum=0,
                maximum=20,
            ),
            llm=LlmTranslatorSettings(
                enabled=_bool(llm_cfg.get("enabled"), default=False),
                provider=str(llm_cfg.get("provider") or "").strip(),
                model=str(llm_cfg.get("model") or "").strip(),
                api_key_env=str(llm_cfg.get("api_key_env") or DEFAULT_LLM_API_KEY_ENV).strip()
                or DEFAULT_LLM_API_KEY_ENV,
                confidence_min=_float(llm_cfg.get("confidence_min"), default=DEFAULT_LLM_CONFIDENCE_MIN),
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
