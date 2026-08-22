from __future__ import annotations

from dataclasses import dataclass


LLM_DEFAULT_API_KEY = "llm.default.api_key"
LLM_DEEPSEEK_API_KEY = "llm.deepseek.api_key"
LLM_MOONSHOT_API_KEY = "llm.moonshot.api_key"
LLM_KIMI_API_KEY = "llm.kimi.api_key"
FEISHU_HOLDINGS_APP_SECRET = "feishu.holdings.app_secret"
FEISHU_BOT_APP_SECRET = "feishu.bot.app_secret"
INBOUND_OPERATION_HMAC_KEY = "inbound.operation_hmac_key"
QUALITY_READ_TOKEN = "quality.read_token"
COPILOT_CURSOR_HMAC_KEY = "copilot.cursor_hmac_key"


@dataclass(frozen=True)
class CredentialSpec:
    logical_name: str
    systemd_credential_id: str
    legacy_env_names: tuple[str, ...]
    purpose: str
    affected_services: tuple[str, ...]

    def public_payload(self) -> dict[str, object]:
        return {
            "logical_name": self.logical_name,
            "systemd_credential_id": self.systemd_credential_id,
            "legacy_env_names": list(self.legacy_env_names),
            "purpose": self.purpose,
            "affected_services": list(self.affected_services),
        }


_SPECS = (
    CredentialSpec(
        LLM_DEFAULT_API_KEY,
        "om-llm-default-api-key",
        ("OM_LLM_API_KEY",),
        "default LLM API authentication",
        ("options-monitor-feishu-ws.service", "options-monitor-wechat-clawbot.service"),
    ),
    CredentialSpec(
        LLM_DEEPSEEK_API_KEY,
        "om-llm-deepseek-api-key",
        ("DEEPSEEK_API_KEY",),
        "DeepSeek API authentication",
        (
            "options-monitor-feishu-ws.service",
            "options-monitor-wechat-clawbot.service",
        ),
    ),
    CredentialSpec(
        LLM_MOONSHOT_API_KEY,
        "om-llm-moonshot-api-key",
        ("MOONSHOT_API_KEY",),
        "Moonshot/Kimi API authentication",
        ("options-monitor-feishu-ws.service", "options-monitor-wechat-clawbot.service"),
    ),
    CredentialSpec(
        LLM_KIMI_API_KEY,
        "om-llm-kimi-api-key",
        ("KIMI_API_KEY",),
        "Kimi Code API authentication",
        ("options-monitor-feishu-ws.service", "options-monitor-wechat-clawbot.service"),
    ),
    CredentialSpec(
        FEISHU_HOLDINGS_APP_SECRET,
        "om-feishu-holdings-app-secret",
        ("OM_FEISHU_APP_SECRET",),
        "Feishu holdings application authentication",
        (
            "options-monitor-tick-*.service",
            "options-monitor-feishu-ws.service",
            "options-monitor-wechat-clawbot.service",
        ),
    ),
    CredentialSpec(
        FEISHU_BOT_APP_SECRET,
        "om-feishu-bot-app-secret",
        ("OM_FEISHU_BOT_APP_SECRET",),
        "Feishu bot and long-connection authentication",
        (
            "options-monitor-tick-*.service",
            "options-monitor-auto-close-*.service",
            "options-monitor-trade-intake.service",
            "options-monitor-feishu-ws.service",
        ),
    ),
    CredentialSpec(
        INBOUND_OPERATION_HMAC_KEY,
        "om-inbound-operation-hmac-key",
        ("OM_INBOUND_OPERATION_HMAC_KEY",),
        "inbound write-operation integrity",
        ("options-monitor-feishu-ws.service", "options-monitor-wechat-clawbot.service"),
    ),
    CredentialSpec(
        QUALITY_READ_TOKEN,
        "om-quality-read-token",
        ("OM_QUALITY_READ_TOKEN",),
        "quality status HTTP authentication",
        ("options-monitor-quality-http.service",),
    ),
    CredentialSpec(
        COPILOT_CURSOR_HMAC_KEY,
        "om-copilot-cursor-hmac-key",
        ("OM_COPILOT_CURSOR_HMAC_KEY",),
        "stateless Copilot pagination cursor integrity",
        (
            "options-monitor-feishu-ws.service",
            "options-monitor-wechat-clawbot.service",
        ),
    ),
)

CREDENTIAL_SPECS = {spec.logical_name: spec for spec in _SPECS}


def credential_specs() -> tuple[CredentialSpec, ...]:
    return _SPECS


def credential_spec(logical_name: str) -> CredentialSpec | None:
    return CREDENTIAL_SPECS.get(str(logical_name or "").strip())


def require_credential_spec(logical_name: str) -> CredentialSpec:
    spec = credential_spec(logical_name)
    if spec is None:
        raise ValueError(f"unknown logical credential name: {str(logical_name or '').strip()}")
    return spec


def legacy_secret_env_names() -> frozenset[str]:
    return frozenset(name for spec in _SPECS for name in spec.legacy_env_names)


__all__ = [
    "CREDENTIAL_SPECS",
    "CredentialSpec",
    "FEISHU_BOT_APP_SECRET",
    "FEISHU_HOLDINGS_APP_SECRET",
    "INBOUND_OPERATION_HMAC_KEY",
    "LLM_DEEPSEEK_API_KEY",
    "LLM_DEFAULT_API_KEY",
    "LLM_KIMI_API_KEY",
    "LLM_MOONSHOT_API_KEY",
    "QUALITY_READ_TOKEN",
    "COPILOT_CURSOR_HMAC_KEY",
    "credential_spec",
    "credential_specs",
    "legacy_secret_env_names",
    "require_credential_spec",
]
