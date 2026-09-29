"""Trusted runtime-config identity for Bot channel and read tools."""

from __future__ import annotations

import hashlib

from src.application.agent_tool_config import (
    DEFAULT_CONFIGS,
    load_runtime_config,
    repo_base,
    resolve_runtime_config_path,
)
from src.application.agent_tool_contracts import AgentToolError
from src.application.runtime_config_freshness import (
    RuntimeConfigFreshnessError,
    ensure_runtime_config_freshness,
    infer_runtime_config_market,
)


class BotConfigScopeError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def resolve_trusted_config_scope(
    *,
    config_key: str | None,
    config_path: str | None,
) -> tuple[str, str, str]:
    key = str(config_key or "").strip().lower()
    raw_path = str(config_path or "").strip()
    if bool(key) == bool(raw_path):
        raise BotConfigScopeError("channel_identity_or_scope_invalid")
    if key and key not in DEFAULT_CONFIGS:
        raise BotConfigScopeError("channel_identity_or_scope_invalid")

    requested_path = resolve_runtime_config_path(
        config_key=key or None,
        config_path=raw_path or None,
    )
    try:
        if not requested_path.exists():
            raise BotConfigScopeError("config_missing")
        if not requested_path.is_file():
            raise BotConfigScopeError("channel_identity_or_scope_invalid")
        resolved, cfg = load_runtime_config(
            config_key=key or None,
            config_path=raw_path or None,
        )
    except BotConfigScopeError:
        raise
    except AgentToolError as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        errors = details.get("errors")
        reason = (
            "config_identity_mismatch"
            if isinstance(errors, list) and errors
            else "config_unreadable"
        )
        raise BotConfigScopeError(reason) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise BotConfigScopeError("config_unreadable") from exc

    canonical_path = str(resolved.resolve())
    actual_market = infer_runtime_config_market(
        config_key=key or None,
        config_path=resolved,
        config=cfg,
    )
    if actual_market not in DEFAULT_CONFIGS:
        raise BotConfigScopeError("config_identity_mismatch")
    try:
        ensure_runtime_config_freshness(
            cfg,
            repo_root=repo_base(),
            market=actual_market,
            runtime_config_path=resolved,
        )
    except RuntimeConfigFreshnessError as exc:
        raise BotConfigScopeError("config_stale") from exc
    except OSError as exc:
        raise BotConfigScopeError("config_unreadable") from exc

    if key:
        return actual_market, canonical_path, f"key:{key}"
    path_digest = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()
    return actual_market, canonical_path, f"path:{path_digest}"


def config_scope_error_message(reason: str) -> str:
    return {
        "config_missing": "已授权市场的运行配置缺失，请先生成运行配置",
        "config_stale": "已授权市场的运行配置已过期，请重新生成运行配置",
        "config_unreadable": "已授权市场的运行配置当前无法读取",
        "config_identity_mismatch": "运行配置与已授权市场身份不一致",
    }.get(reason, "渠道身份或数据作用域不可用")
