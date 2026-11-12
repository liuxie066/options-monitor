"""Explicit preflight configuration, independent of OM account configuration."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.application.bot.model_config import ModelSettings
from src.application.secret_store.registry import (
    GROUP_QUERY_BOT_FEISHU_APP_SECRET,
    GROUP_QUERY_BOT_LLM_API_KEY,
    GROUP_QUERY_BOT_QUERY_API_TOKEN,
)


@dataclass(frozen=True)
class PreflightSettings:
    instance_id: str
    data_dir: Path
    app_id: str
    app_secret_ref: str
    allowed_chat_ids: tuple[str, ...]

    def public_digest(self) -> str:
        fields = {
            "instance_id": self.instance_id,
            "data_dir": str(self.data_dir),
            "feishu": {"app_id": self.app_id, "app_secret_ref": self.app_secret_ref,
                       "allowed_chat_ids": list(self.allowed_chat_ids)},
        }
        return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _object(value: object, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("preflight configuration has missing or unsupported fields")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("preflight configuration requires nonempty strings without surrounding whitespace")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("preflight configuration contains a duplicate field")
        result[key] = value
    return result


def _chat_scope(value: object, *, owner: str) -> tuple[str, ...]:
    """A concrete chat allowlist, or the sole wildcard entry ``"*"``."""
    if not isinstance(value, list) or not value:
        raise ValueError(f"{owner} requires at least one allowed chat")
    chat_ids = tuple(sorted({_text(chat) for chat in value}))
    if len(chat_ids) != len(value):
        raise ValueError(f"{owner} allowed chats must be unique")
    if "*" in chat_ids and chat_ids != ("*",):
        raise ValueError(f"{owner} wildcard chat scope must be the only entry")
    return chat_ids


def load_preflight_settings(path: str | Path) -> PreflightSettings:
    """Validate only: no secret lookup, network, default config or directory creation."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cannot read preflight JSON configuration") from exc
    raw = _object(raw, {"instance_id", "data_dir", "feishu"})
    instance = _text(raw["instance_id"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", instance):
        raise ValueError("invalid preflight instance_id")
    data_dir = Path(_text(raw["data_dir"]))
    if not data_dir.is_absolute():
        raise ValueError("preflight data_dir must be an explicit absolute path")
    feishu = _object(raw["feishu"], {"app_id", "app_secret_ref", "allowed_chat_ids"})
    app_id = _text(feishu["app_id"])
    secret_ref = _text(feishu["app_secret_ref"])
    if secret_ref != GROUP_QUERY_BOT_FEISHU_APP_SECRET:
        raise ValueError("preflight requires its dedicated Feishu credential reference")
    chats = feishu["allowed_chat_ids"]
    chat_ids = _chat_scope(chats, owner="preflight")
    return PreflightSettings(instance, data_dir, app_id, secret_ref, chat_ids)


@dataclass(frozen=True)
class PublicPolicySettings:
    """The frozen public strategy and fee policy; never account-scoped."""

    policy: dict
    fee_model_id: str
    fee_model_version: str
    strategy_version: str

    def canonical_sha256(self) -> str:
        payload = {
            "policy": self.policy,
            "fee_model_id": self.fee_model_id,
            "fee_model_version": self.fee_model_version,
            "strategy_version": self.strategy_version,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode()
        ).hexdigest()


@dataclass(frozen=True)
class APISettings:
    """Explicit loopback authorization, public policy and quote routing."""

    instance_id: str
    data_dir: Path
    bind_host: str
    bind_port: int
    token_ref: str
    app_id: str
    principal_id: str
    capabilities: tuple[str, ...]
    markets: tuple[str, ...]
    quote_source: str
    quote_host: str
    quote_port: int
    max_budget_seconds: float
    public_policy: PublicPolicySettings
    principal_requests_per_minute: int = 30

    def public_digest(self) -> str:
        fields = {
            "instance_id": self.instance_id,
            "bind": [self.bind_host, self.bind_port],
            "app_id": self.app_id,
            "principal": self.principal_id,
            "capabilities": list(self.capabilities),
            "markets": list(self.markets),
            "quote": [self.quote_source, self.quote_host, self.quote_port],
            "max_budget_seconds": self.max_budget_seconds,
            "principal_requests_per_minute": self.principal_requests_per_minute,
            "public_policy_sha256": self.public_policy.canonical_sha256(),
        }
        return hashlib.sha256(
            json.dumps(fields, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode()
        ).hexdigest()


_ACCOUNT_MARKERS = frozenset({
    "accounts", "account", "account_configs", "portfolio", "lots",
    "position_lot_id", "ledger",
})


def _reject_account_keys(value: object, path: str = "policy") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in _ACCOUNT_MARKERS:
                raise ValueError(f"{path} must not carry account configuration")
            _reject_account_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_account_keys(item, f"{path}[{index}]")


def _range(value: object, name: str, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    parsed = float(value)
    if not low <= parsed <= high:
        raise ValueError(f"{name} is outside its supported range")
    return parsed


def _port(value: object) -> int:
    if isinstance(value, bool) or type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("API port must be an integer from 1 to 65535")
    return value


def load_api_settings(path: str | Path) -> APISettings:
    """Validate explicit API settings; no secret lookup, network or fallback."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"),
                         object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cannot read API JSON configuration") from exc
    if not isinstance(raw, dict):
        raise ValueError("API configuration has missing or unsupported fields")
    if "principal_requests_per_minute" not in raw:
        raw = {**raw, "principal_requests_per_minute": 30}
    raw = _object(raw, {"instance_id", "data_dir", "api", "principal", "quote",
                        "max_budget_seconds", "public_policy",
                        "principal_requests_per_minute"})
    instance = _text(raw["instance_id"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", instance):
        raise ValueError("invalid API instance_id")
    data_dir = Path(_text(raw["data_dir"]))
    if not data_dir.is_absolute():
        raise ValueError("API data_dir must be an explicit absolute path")
    api = _object(raw["api"], {"bind_host", "bind_port", "token_ref", "app_id"})
    if _text(api["bind_host"]) != "127.0.0.1":
        raise ValueError("API must bind loopback 127.0.0.1")
    bind_port = _port(api["bind_port"])
    token_ref = _text(api["token_ref"])
    if token_ref != GROUP_QUERY_BOT_QUERY_API_TOKEN:
        raise ValueError("API requires its dedicated query credential reference")
    app_id = _text(api["app_id"])
    principal = _object(raw["principal"], {"principal_id", "capabilities", "markets",
                                           "account_scope"})
    principal_id = _text(principal["principal_id"])
    caps = principal["capabilities"]
    markets = principal["markets"]
    account_scope = principal["account_scope"]
    if not isinstance(caps, list) or not caps:
        raise ValueError("API principal requires explicit capabilities")
    if any(not isinstance(item, str) for item in caps):
        raise ValueError("API principal capabilities must be strings")
    capability_set = tuple(sorted(set(caps)))
    if len(capability_set) != len(caps):
        raise ValueError("API principal capabilities must be unique")
    if not isinstance(markets, list) or not markets:
        raise ValueError("API principal requires explicit markets")
    if any(not isinstance(item, str) for item in markets):
        raise ValueError("API principal markets must be strings")
    market_set = tuple(sorted({item.upper() for item in markets}))
    if account_scope != []:
        raise ValueError("API principal account_scope must be exactly []")
    quote = _object(raw["quote"], {"source", "host", "port"})
    quote_source = _text(quote["source"])
    quote_host = _text(quote["host"])
    quote_port = _port(quote["port"])
    max_budget = _range(raw["max_budget_seconds"], "max_budget_seconds", low=1, high=90)
    principal_per_minute = _int_range(raw.get("principal_requests_per_minute", 30),
                                      "principal_requests_per_minute", low=1, high=120)
    policy = _object(raw["public_policy"], {"policy", "fee_model_id",
                                            "fee_model_version", "strategy_version"})
    if not isinstance(policy["policy"], dict):
        raise ValueError("public policy payload must be an object")
    _reject_account_keys(policy["policy"])
    public_policy = PublicPolicySettings(
        policy=dict(policy["policy"]),
        fee_model_id=_text(policy["fee_model_id"]),
        fee_model_version=_text(policy["fee_model_version"]),
        strategy_version=_text(policy["strategy_version"]),
    )
    return APISettings(
        instance_id=instance, data_dir=data_dir, bind_host="127.0.0.1",
        bind_port=bind_port, token_ref=token_ref, app_id=app_id,
        principal_id=principal_id,
        capabilities=capability_set, markets=market_set, quote_source=quote_source,
        quote_host=quote_host, quote_port=quote_port,
        max_budget_seconds=max_budget, public_policy=public_policy,
        principal_requests_per_minute=principal_per_minute,
    )


def _int_range(value: object, name: str, *, low: int, high: int) -> int:
    if isinstance(value, bool) or type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


@dataclass(frozen=True)
class BotSettings:
    """Explicit bot configuration; no OM account config and no fallback."""

    instance_id: str
    data_dir: Path
    app_id: str
    app_secret_ref: str
    allowed_chat_ids: tuple[str, ...]
    model: ModelSettings
    query_api_base_url: str
    query_api_token_ref: str
    user_per_minute: int = 3
    group_per_minute: int = 10
    user_concurrency: int = 1
    bot_concurrency: int = 2
    turn_seconds: int = 90
    model_calls: int = 4
    api_calls: int = 3
    output_tokens: int = 2000
    reserve_tokens: int = 32000
    context_turns: int = 10
    context_tokens: int = 4000
    api_timeout_seconds: int = 30
    followup_window_seconds: int = 600
    user_timezone: str = "Asia/Shanghai"


def _loopback_base_url(value: object) -> str:
    text = _text(value)
    parsed = urlparse(text)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.port is None or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("query_api.base_url must be an explicit http://127.0.0.1:<port> URL")
    return f"http://127.0.0.1:{parsed.port}"


def load_bot_settings(path: str | Path) -> BotSettings:
    """Validate only: no secret lookup, network or directory creation."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"),
                         object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cannot read bot JSON configuration") from exc
    if isinstance(raw, dict):
        raw = {"limits": {}, "user_timezone": "Asia/Shanghai", **raw}
    raw = _object(raw, {"instance_id", "data_dir", "feishu", "model", "query_api",
                        "limits", "user_timezone"})
    user_timezone = _text(raw["user_timezone"])
    try:
        ZoneInfo(user_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("bot user_timezone must be a valid IANA time zone") from exc
    instance = _text(raw["instance_id"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", instance):
        raise ValueError("invalid bot instance_id")
    data_dir = Path(_text(raw["data_dir"]))
    if not data_dir.is_absolute():
        raise ValueError("bot data_dir must be an explicit absolute path")
    feishu = _object(raw["feishu"], {"app_id", "app_secret_ref", "allowed_chat_ids"})
    app_id = _text(feishu["app_id"])
    app_secret_ref = _text(feishu["app_secret_ref"])
    if app_secret_ref != GROUP_QUERY_BOT_FEISHU_APP_SECRET:
        raise ValueError("bot requires its dedicated Feishu credential reference")
    chats = feishu["allowed_chat_ids"]
    chat_ids = _chat_scope(chats, owner="bot")
    model = _object(raw["model"], {"provider", "model", "base_url", "api_key_ref",
                                   "context_window_tokens", "max_output_tokens",
                                   "timeout_seconds"})
    api_key_ref = _text(model["api_key_ref"])
    if api_key_ref != GROUP_QUERY_BOT_LLM_API_KEY:
        raise ValueError("bot requires its dedicated model credential reference")
    model_settings = ModelSettings.from_config({**model, "credential_name": api_key_ref})
    query_api = _object(raw["query_api"], {"base_url", "token_ref"})
    query_api_base_url = _loopback_base_url(query_api["base_url"])
    query_api_token_ref = _text(query_api["token_ref"])
    if query_api_token_ref != GROUP_QUERY_BOT_QUERY_API_TOKEN:
        raise ValueError("bot requires its dedicated query API credential reference")
    limits = raw.get("limits", {})
    if not isinstance(limits, dict):
        raise ValueError("bot limits must be an object")
    unknown_limits = sorted(set(limits) - {
        "user_per_minute", "group_per_minute", "user_concurrency", "bot_concurrency",
        "turn_seconds", "model_calls", "api_calls", "output_tokens", "reserve_tokens",
        "context_turns", "context_tokens", "api_timeout_seconds",
        "followup_window_seconds",
    })
    if unknown_limits:
        raise ValueError("bot limits contain unsupported fields: " + ", ".join(unknown_limits))
    return BotSettings(
        instance_id=instance, data_dir=data_dir, app_id=app_id,
        app_secret_ref=app_secret_ref, allowed_chat_ids=chat_ids,
        model=model_settings, query_api_base_url=query_api_base_url,
        query_api_token_ref=query_api_token_ref, user_timezone=user_timezone,
        user_per_minute=_int_range(limits.get("user_per_minute", 3),
                                   "limits.user_per_minute", low=1, high=30),
        group_per_minute=_int_range(limits.get("group_per_minute", 10),
                                    "limits.group_per_minute", low=1, high=120),
        user_concurrency=_int_range(limits.get("user_concurrency", 1),
                                    "limits.user_concurrency", low=1, high=4),
        bot_concurrency=_int_range(limits.get("bot_concurrency", 2),
                                   "limits.bot_concurrency", low=1, high=8),
        turn_seconds=_int_range(limits.get("turn_seconds", 90),
                                "limits.turn_seconds", low=10, high=90),
        model_calls=_int_range(limits.get("model_calls", 4),
                               "limits.model_calls", low=1, high=8),
        api_calls=_int_range(limits.get("api_calls", 3),
                             "limits.api_calls", low=1, high=6),
        output_tokens=_int_range(limits.get("output_tokens", 2000),
                                 "limits.output_tokens", low=256, high=4000),
        reserve_tokens=_int_range(limits.get("reserve_tokens", 32000),
                                  "limits.reserve_tokens", low=8000, high=64000),
        context_turns=_int_range(limits.get("context_turns", 10),
                                 "limits.context_turns", low=1, high=10),
        context_tokens=_int_range(limits.get("context_tokens", 4000),
                                  "limits.context_tokens", low=1000, high=8000),
        api_timeout_seconds=_int_range(limits.get("api_timeout_seconds", 30),
                                       "limits.api_timeout_seconds", low=1, high=60),
        followup_window_seconds=_int_range(limits.get("followup_window_seconds", 600),
                                          "limits.followup_window_seconds", low=0, high=3600),
    )
