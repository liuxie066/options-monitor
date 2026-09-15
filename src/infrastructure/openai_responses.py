from __future__ import annotations

DEFAULT_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


def resolve_responses_url(base_url: str | None) -> str:
    value = str(base_url or "").strip()
    if not value:
        return DEFAULT_OPENAI_RESPONSES_URL
    normalized = value.rstrip("/")
    if normalized.endswith("/responses"):
        return normalized
    return f"{normalized}/responses"
