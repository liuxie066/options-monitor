"""Trusted runtime-config identity for Bot channel and read tools."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

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


def _resolve_trusted_config(
    *,
    config_key: str | None,
    config_path: str | None,
) -> tuple[str, str, str, dict[str, Any]]:
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
        return actual_market, canonical_path, f"key:{key}", cfg
    path_digest = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()
    return actual_market, canonical_path, f"path:{path_digest}", cfg



def resolve_trusted_config_scope(*, config_key: str | None, config_path: str | None) -> tuple[str, str, str]:
    return _resolve_trusted_config(config_key=config_key, config_path=config_path)[:3]


def resolve_replay_authorization(request: Any) -> dict[str, Any]:
    """Freeze channel authority from validated owners, never cached response content."""
    from src.application.bot.model_config import load_bot_read_scope
    from src.application.conversation_scope import derive_session_id

    identity = {
        "channel": request.channel, "sender_id": request.sender_id,
        "conversation_id": request.conversation_id or f"{request.channel}:{request.sender_id}",
        "message_id": request.message_id, "config_key": request.config_key,
        "config_path": request.config_path, "bot_config_path": request.bot_config_path,
    }
    identity["transport_identity"] = (request.reply_context or {}).get("reply_sender_identity")
    if not (request.config_key or request.config_path):
        if request.channel != "local":
            raise BotConfigScopeError("channel_identity_or_scope_invalid")
        authority = "local"
    else:
        market, path, authority, cfg = _resolve_trusted_config(
            config_key=request.config_key, config_path=request.config_path)
        identity["config_path"] = path if request.config_path else None
        identity["bot_config_path"] = str(Path(request.bot_config_path).resolve()) if request.bot_config_path else None
        configs = {market: {**cfg, "config_source_path": path}}
        generation = ""
        if request.bot_config_path:
            markets, generation = load_bot_read_scope(config_path=request.bot_config_path, primary_market=market)
            for secondary in sorted(markets - {market}):
                _, secondary_path, _, secondary_cfg = _resolve_trusted_config(config_key=secondary, config_path=None)
                configs[secondary] = {**secondary_cfg, "config_source_path": secondary_path}
        digest = hashlib.sha256(json.dumps(configs, sort_keys=True, ensure_ascii=False,
                                          allow_nan=False).encode()).hexdigest()
        authority = f"{authority}|{path}|{digest}|{generation}"
    identity["scope"] = derive_session_id(identity["channel"], identity["sender_id"],
                                         identity["conversation_id"], authority)
    return identity

def config_scope_error_message(reason: str) -> str:
    return {
        "config_missing": "已授权市场的运行配置缺失，请先生成运行配置",
        "config_stale": "已授权市场的运行配置已过期，请重新生成运行配置",
        "config_unreadable": "已授权市场的运行配置当前无法读取",
        "config_identity_mismatch": "运行配置与已授权市场身份不一致",
    }.get(reason, "渠道身份或数据作用域不可用")


def reply_destination(channel: str, payload: dict[str, Any]) -> str:
    fields = ("message_id", "reply_in_thread") if channel == "feishu" else ("to_user_id", "context_token", "group_id")
    return hashlib.sha256(json.dumps([payload.get(field) for field in fields],
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def freeze_reply_authorization(authorization: dict[str, Any] | None, *, channel: str,
                               payload: dict[str, Any]) -> dict[str, Any] | None:
    if not authorization:
        return None
    return {**authorization, "destination": reply_destination(channel, payload)}
