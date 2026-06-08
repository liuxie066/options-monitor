from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_LLM_API_KEY_ENV = "OM_LLM_API_KEY"
DEFAULT_LLM_CONFIDENCE_MIN = 0.75
DEFAULT_LLM_TIMEOUT_SECONDS = 20
DEFAULT_LLM_MAX_OUTPUT_TOKENS = 512
DEFAULT_CONTEXT_WINDOW_MESSAGES = 8
DEFAULT_ASSISTANT_MODE = "agent_loop"
DEFAULT_MARKET_SCOPE = ""
ASSISTANT_MODES = frozenset({"disabled", "agent_loop"})


@dataclass(frozen=True)
class PlannerSettings:
    enabled: bool = True

    def public_payload(self) -> dict[str, Any]:
        return {"enabled": bool(self.enabled)}


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
class AssistantSettings:
    mode: str = DEFAULT_ASSISTANT_MODE
    enabled: bool | None = None
    context_window_messages: int = DEFAULT_CONTEXT_WINDOW_MESSAGES
    default_market_scope: str = DEFAULT_MARKET_SCOPE
    planner: PlannerSettings = PlannerSettings()
    llm: LlmTranslatorSettings = LlmTranslatorSettings()

    def __post_init__(self) -> None:
        legacy_mode = str(self.mode or "").strip().lower()
        mode = DEFAULT_ASSISTANT_MODE
        if self.enabled is False or legacy_mode == "disabled":
            mode = "disabled"
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "enabled", mode != "disabled")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.enabled and self.planner.enabled and self.llm.enabled)

    @property
    def planner_enabled(self) -> bool:
        return bool(self.enabled and self.planner.enabled)

    @classmethod
    def from_runtime_config(cls, cfg: dict[str, Any]) -> "AssistantSettings":
        assistant_cfg = _dict(cfg.get("assistant"))
        enabled = _assistant_enabled(assistant_cfg)
        planner_cfg = _dict(assistant_cfg.get("planner"))
        planner = PlannerSettings(enabled=_bool(planner_cfg.get("enabled"), default=True))
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
            planner=planner,
            llm=_llm_settings(llm_cfg, enabled=bool(enabled and planner.enabled)),
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "enabled": bool(self.enabled),
            "context_window_messages": int(self.context_window_messages),
            "default_market_scope": self.default_market_scope,
            "planner": self.planner.public_payload(),
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


def _assistant_enabled(assistant_cfg: dict[str, Any]) -> bool:
    if "enabled" in assistant_cfg:
        return _bool(assistant_cfg.get("enabled"), default=True)
    legacy_mode = str(assistant_cfg.get("mode") or "").strip().lower()
    if legacy_mode == "disabled":
        return False
    return True


def _market_scope(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"us", "hk", "all"}:
        return text
    return DEFAULT_MARKET_SCOPE


def _llm_settings(llm_cfg: dict[str, Any], *, enabled: bool) -> LlmTranslatorSettings:
    provider = str(llm_cfg.get("provider") or "").strip()
    model = str(llm_cfg.get("model") or "").strip()
    return LlmTranslatorSettings(
        enabled=bool(enabled and provider and model),
        provider=provider,
        base_url=str(llm_cfg.get("base_url") or "").strip(),
        model=model,
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
