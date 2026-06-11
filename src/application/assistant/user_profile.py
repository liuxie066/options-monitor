from __future__ import annotations

from pathlib import Path
from typing import Any

from src.application.agent_tool_config import repo_base


USER_PROFILE_SCHEMA_VERSION = "om-user-profile-v1"
DEFAULT_USER_PROFILE_FILENAME = "user.md"
MAX_USER_PROFILE_CHARS = 4000
_SENSITIVE_TOKENS = (
    "access_token",
    "api key",
    "api_key",
    "authorization",
    "cookie",
    "password",
    "private_key",
    "secret",
    "token",
    "webhook",
)


def default_user_profile_path() -> Path:
    return (repo_base() / DEFAULT_USER_PROFILE_FILENAME).resolve()


def load_user_profile_context(path: str | Path | None = None) -> dict[str, Any]:
    profile_path = Path(path).expanduser().resolve() if path else default_user_profile_path()
    if not profile_path.exists():
        return _empty_profile(reason="missing")
    if not profile_path.is_file():
        return _empty_profile(reason="not_file")

    try:
        raw = profile_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _empty_profile(reason="decode_error")
    except OSError:
        return _empty_profile(reason="read_error")

    content = raw.strip()
    if not content:
        return _empty_profile(reason="empty")

    redacted, redacted_line_count = _redact_sensitive_lines(content)
    clipped = redacted[:MAX_USER_PROFILE_CHARS]
    return {
        "schema_version": USER_PROFILE_SCHEMA_VERSION,
        "provided": True,
        "source": DEFAULT_USER_PROFILE_FILENAME,
        "format": "markdown",
        "content": clipped,
        "truncated": len(redacted) > MAX_USER_PROFILE_CHARS,
        "redacted_line_count": redacted_line_count,
        "semantics": {
            "explicit_message_wins": True,
            "profile_is_hint_only": True,
            "do_not_treat_profile_as_market_or_ledger_fact": True,
        },
    }


def user_profile_trace(profile: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(profile, dict) or not bool(profile.get("provided")):
        reason = profile.get("reason") if isinstance(profile, dict) else None
        return {"provided": False, "reason": str(reason or "missing")}
    return {
        "provided": True,
        "source": str(profile.get("source") or DEFAULT_USER_PROFILE_FILENAME),
        "format": str(profile.get("format") or "markdown"),
        "chars": len(str(profile.get("content") or "")),
        "truncated": bool(profile.get("truncated")),
        "redacted_line_count": int(profile.get("redacted_line_count") or 0),
    }


def _empty_profile(*, reason: str) -> dict[str, Any]:
    return {
        "schema_version": USER_PROFILE_SCHEMA_VERSION,
        "provided": False,
        "source": DEFAULT_USER_PROFILE_FILENAME,
        "reason": reason,
    }


def _redact_sensitive_lines(content: str) -> tuple[str, int]:
    lines: list[str] = []
    redacted = 0
    for line in content.splitlines():
        if _looks_sensitive(line):
            lines.append("[redacted sensitive line]")
            redacted += 1
        else:
            lines.append(line)
    return "\n".join(lines), redacted


def _looks_sensitive(line: str) -> bool:
    lowered = line.lower()
    if not any(token in lowered for token in _SENSITIVE_TOKENS):
        return False
    return any(marker in lowered for marker in ("=", ":", "sk-", "bearer ", "http://", "https://"))


__all__ = [
    "DEFAULT_USER_PROFILE_FILENAME",
    "MAX_USER_PROFILE_CHARS",
    "USER_PROFILE_SCHEMA_VERSION",
    "default_user_profile_path",
    "load_user_profile_context",
    "user_profile_trace",
]
