from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.application.secret_store.registry import (
    LLM_DEEPSEEK_API_KEY,
    LLM_DEFAULT_API_KEY,
    LLM_KIMI_API_KEY,
    LLM_MOONSHOT_API_KEY,
)


@dataclass(frozen=True)
class LlmProviderSpec:
    provider_id: str
    display_name: str
    api_kind: str
    default_base_url: str
    default_api_key_env: str
    credential_name: str
    recommended_models: tuple[str, ...]
    requires_api_key: bool = True

    def public_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "display_name": self.display_name,
            "api_kind": self.api_kind,
            "default_base_url": self.default_base_url,
            "default_api_key_env": self.default_api_key_env,
            "credential_name": self.credential_name,
            "recommended_models": list(self.recommended_models),
            "requires_api_key": self.requires_api_key,
            "supports_live_model_list": False,
        }


PROVIDER_SPECS: dict[str, LlmProviderSpec] = {
    "openai": LlmProviderSpec(
        provider_id="openai",
        display_name="OpenAI",
        api_kind="responses",
        default_base_url="",
        default_api_key_env="OM_LLM_API_KEY",
        credential_name=LLM_DEFAULT_API_KEY,
        recommended_models=("gpt-5.2",),
    ),
    "deepseek": LlmProviderSpec(
        provider_id="deepseek",
        display_name="DeepSeek",
        api_kind="chat_completions",
        default_base_url="https://api.deepseek.com",
        default_api_key_env="DEEPSEEK_API_KEY",
        credential_name=LLM_DEEPSEEK_API_KEY,
        recommended_models=("deepseek-chat", "deepseek-reasoner"),
    ),
    "kimi": LlmProviderSpec(
        provider_id="kimi",
        display_name="Kimi",
        api_kind="chat_completions",
        default_base_url="https://api.moonshot.ai/v1",
        default_api_key_env="MOONSHOT_API_KEY",
        credential_name=LLM_MOONSHOT_API_KEY,
        recommended_models=("kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"),
    ),
    "kimi-code": LlmProviderSpec(
        provider_id="kimi-code",
        display_name="Kimi Code",
        api_kind="chat_completions",
        default_base_url="https://api.kimi.com/coding/v1",
        default_api_key_env="KIMI_API_KEY",
        credential_name=LLM_KIMI_API_KEY,
        recommended_models=("kimi-for-coding",),
    ),
    "ollama": LlmProviderSpec(
        provider_id="ollama",
        display_name="Ollama",
        api_kind="chat_completions",
        default_base_url="http://127.0.0.1:11434/v1",
        default_api_key_env="",
        credential_name="",
        recommended_models=("gpt-oss:20b",),
        requires_api_key=False,
    ),
}


def resolve_output_reservation(
    provider: str,
    model: str,
    context_window_tokens: int,
    max_output_tokens: int | None,
    *,
    path: str = "model",
) -> int:
    """Validate the optional wire cap and resolve its context reservation."""
    if isinstance(context_window_tokens, bool) or not isinstance(context_window_tokens, int):
        raise ValueError(f"{path}.context_window_tokens must be an integer")
    if not 4096 <= context_window_tokens <= 2_000_000:
        raise ValueError(f"{path}.context_window_tokens must be between 4096 and 2000000")
    if max_output_tokens is not None and (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or max_output_tokens < 64
    ):
        raise ValueError(f"{path}.max_output_tokens must be an integer >= 64 or null")
    # Native capability reference: https://api-docs.deepseek.com/quick_start/agent_integrations/pi_mono/
    known = provider == "deepseek" and model in {"deepseek-v4-pro", "deepseek-v4-flash"}
    if known and context_window_tokens > 1_000_000:
        raise ValueError(f"{path}.context_window_tokens exceeds {model} maximum 1000000")
    if known and max_output_tokens is not None and max_output_tokens > 384_000:
        raise ValueError(f"{path}.max_output_tokens exceeds {model} maximum 384000")
    if max_output_tokens is None:
        if not known:
            raise ValueError(f"{path}.max_output_tokens must be specified explicitly for unknown model {provider}/{model}")
        reservation = 384_000
    else:
        reservation = max_output_tokens
    if context_window_tokens <= reservation + 2000:
        raise ValueError(
            f"{path}.context_window_tokens must exceed output reservation {reservation} by more than 2000; "
            "configure sufficient context or an explicit max_output_tokens limit"
        )
    return reservation


def normalize_llm_provider(provider: str) -> str:
    return str(provider or "").strip().lower()


def provider_specs() -> tuple[LlmProviderSpec, ...]:
    return tuple(PROVIDER_SPECS.values())


def supported_llm_providers() -> tuple[str, ...]:
    return tuple(spec.provider_id for spec in provider_specs())


def provider_spec(provider: str) -> LlmProviderSpec | None:
    return PROVIDER_SPECS.get(normalize_llm_provider(provider))


def require_provider_spec(provider: str, *, path: str = "provider") -> LlmProviderSpec:
    from src.application.agent_tool_contracts import AgentToolError

    spec = provider_spec(provider)
    if spec is None:
        supported = ", ".join(supported_llm_providers())
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"{path} must be one of: {supported}",
            details={"provider": str(provider or "").strip(), "supported_providers": list(supported_llm_providers())},
        )
    return spec


def is_supported_llm_provider(provider: str) -> bool:
    return provider_spec(provider) is not None


def provider_api_kind(provider: str) -> str:
    spec = provider_spec(provider)
    return spec.api_kind if spec is not None else "responses"


def provider_requires_api_key(provider: str) -> bool:
    spec = provider_spec(provider)
    return spec.requires_api_key if spec is not None else True


def provider_catalog_payload() -> dict[str, Any]:
    providers = [spec.public_payload() for spec in provider_specs()]
    return {
        "summary": {
            "provider_count": len(providers),
            "live_model_list_supported": False,
        },
        "providers": providers,
    }


__all__ = [
    "LlmProviderSpec",
    "is_supported_llm_provider",
    "normalize_llm_provider",
    "provider_api_kind",
    "provider_catalog_payload",
    "provider_spec",
    "provider_specs",
    "provider_requires_api_key",
    "require_provider_spec",
    "supported_llm_providers",
]
