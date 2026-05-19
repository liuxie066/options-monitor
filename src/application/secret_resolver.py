from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_FEISHU_APP_ID_ENV = "OM_FEISHU_APP_ID"
DEFAULT_FEISHU_APP_SECRET_ENV = "OM_FEISHU_APP_SECRET"
DEFAULT_FEISHU_HOLDINGS_TABLE_ENV = "OM_FEISHU_HOLDINGS_TABLE"
DEFAULT_FEISHU_BOT_APP_ID_ENV = "OM_FEISHU_BOT_APP_ID"
DEFAULT_FEISHU_BOT_APP_SECRET_ENV = "OM_FEISHU_BOT_APP_SECRET"
DEFAULT_FEISHU_BOT_ENCRYPT_KEY_ENV = "OM_FEISHU_BOT_ENCRYPT_KEY"
DEFAULT_FEISHU_BOT_VERIFICATION_TOKEN_ENV = "OM_FEISHU_BOT_VERIFICATION_TOKEN"
DEFAULT_FEISHU_BOT_USER_OPEN_ID_ENV = "OM_FEISHU_BOT_USER_OPEN_ID"
DEFAULT_FEISHU_BOT_ALLOWED_OPEN_IDS_ENV = "OM_FEISHU_BOT_ALLOWED_OPEN_IDS"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _env(environ: Mapping[str, str] | None, name: str) -> str:
    env = environ if environ is not None else os.environ
    return _text(env.get(name))


@dataclass(frozen=True)
class FeishuHoldingsConfig:
    app_id: str
    app_secret: str
    holdings_ref: str
    app_id_env: str
    app_secret_env: str
    holdings_env: str

    @property
    def ready(self) -> bool:
        return bool(self.app_id and self.app_secret and "/" in self.holdings_ref)

    @property
    def missing_fields(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.app_id:
            missing.append(self.app_id_env)
        if not self.app_secret:
            missing.append(self.app_secret_env)
        if "/" not in self.holdings_ref:
            missing.append(self.holdings_env)
        return tuple(missing)

    def redacted_status(self) -> dict[str, Any]:
        return {
            "app_id_env": self.app_id_env,
            "app_id_configured": bool(self.app_id),
            "app_secret_env": self.app_secret_env,
            "app_secret_configured": bool(self.app_secret),
            "holdings_env": self.holdings_env,
            "holdings_configured": "/" in self.holdings_ref,
        }


@dataclass(frozen=True)
class FeishuBotConfig:
    app_id: str
    app_secret: str
    user_open_id: str
    allowed_open_ids: tuple[str, ...]
    encrypt_key: str
    verification_token: str
    app_id_env: str
    app_secret_env: str
    user_open_id_env: str
    allowed_open_ids_env: str
    encrypt_key_env: str
    verification_token_env: str

    @property
    def credentials_ready(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @property
    def send_ready(self) -> bool:
        return bool(self.credentials_ready and self.user_open_id)

    @property
    def inbound_ready(self) -> bool:
        return bool(self.credentials_ready and self.encrypt_key and self.verification_token and self.allowed_open_ids)

    @property
    def credential_missing_fields(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.app_id:
            missing.append(self.app_id_env)
        if not self.app_secret:
            missing.append(self.app_secret_env)
        return tuple(missing)

    @property
    def send_missing_fields(self) -> tuple[str, ...]:
        missing = list(self.credential_missing_fields)
        if not self.user_open_id:
            missing.append(self.user_open_id_env)
        return tuple(missing)

    @property
    def inbound_missing_fields(self) -> tuple[str, ...]:
        missing = list(self.credential_missing_fields)
        if not self.encrypt_key:
            missing.append(self.encrypt_key_env)
        if not self.verification_token:
            missing.append(self.verification_token_env)
        if not self.allowed_open_ids:
            missing.append(self.allowed_open_ids_env)
        return tuple(missing)

    def default_allowed_senders(self) -> str:
        return ",".join(f"feishu:{item}" for item in self.allowed_open_ids if item)

    def redacted_status(self) -> dict[str, Any]:
        return {
            "app_id_env": self.app_id_env,
            "app_id_configured": bool(self.app_id),
            "app_secret_env": self.app_secret_env,
            "app_secret_configured": bool(self.app_secret),
            "user_open_id_env": self.user_open_id_env,
            "user_open_id_configured": bool(self.user_open_id),
            "allowed_open_ids_env": self.allowed_open_ids_env,
            "allowed_open_ids_count": len(self.allowed_open_ids),
            "encrypt_key_env": self.encrypt_key_env,
            "encrypt_key_configured": bool(self.encrypt_key),
            "verification_token_env": self.verification_token_env,
            "verification_token_configured": bool(self.verification_token),
        }


def resolve_feishu_holdings_config(
    data_cfg: dict[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> FeishuHoldingsConfig:
    feishu_cfg = _dict(_dict(data_cfg).get("feishu"))
    tables = _dict(feishu_cfg.get("tables"))
    app_id_env = _text(feishu_cfg.get("app_id_env")) or DEFAULT_FEISHU_APP_ID_ENV
    app_secret_env = _text(feishu_cfg.get("app_secret_env")) or DEFAULT_FEISHU_APP_SECRET_ENV
    holdings_env = _text(tables.get("holdings_env") or feishu_cfg.get("holdings_env")) or DEFAULT_FEISHU_HOLDINGS_TABLE_ENV
    return FeishuHoldingsConfig(
        app_id=_env(environ, app_id_env),
        app_secret=_env(environ, app_secret_env),
        holdings_ref=_env(environ, holdings_env),
        app_id_env=app_id_env,
        app_secret_env=app_secret_env,
        holdings_env=holdings_env,
    )


def resolve_feishu_bot_config(
    notifications: dict[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> FeishuBotConfig:
    del notifications
    app_id_env = DEFAULT_FEISHU_BOT_APP_ID_ENV
    app_secret_env = DEFAULT_FEISHU_BOT_APP_SECRET_ENV
    user_open_id_env = DEFAULT_FEISHU_BOT_USER_OPEN_ID_ENV
    allowed_open_ids_env = DEFAULT_FEISHU_BOT_ALLOWED_OPEN_IDS_ENV
    encrypt_key_env = DEFAULT_FEISHU_BOT_ENCRYPT_KEY_ENV
    verification_token_env = DEFAULT_FEISHU_BOT_VERIFICATION_TOKEN_ENV
    user_open_id = _env(environ, user_open_id_env)
    allowed_open_ids = _split_csv(_env(environ, allowed_open_ids_env)) or ((user_open_id,) if user_open_id else ())
    return FeishuBotConfig(
        app_id=_env(environ, app_id_env),
        app_secret=_env(environ, app_secret_env),
        user_open_id=user_open_id,
        allowed_open_ids=allowed_open_ids,
        encrypt_key=_env(environ, encrypt_key_env),
        verification_token=_env(environ, verification_token_env),
        app_id_env=app_id_env,
        app_secret_env=app_secret_env,
        user_open_id_env=user_open_id_env,
        allowed_open_ids_env=allowed_open_ids_env,
        encrypt_key_env=encrypt_key_env,
        verification_token_env=verification_token_env,
    )


def _split_csv(value: str) -> tuple[str, ...]:
    out: list[str] = []
    for raw in str(value or "").split(","):
        item = raw.strip()
        if item and item not in out:
            out.append(item)
    return tuple(out)


__all__ = [
    "DEFAULT_FEISHU_APP_ID_ENV",
    "DEFAULT_FEISHU_APP_SECRET_ENV",
    "DEFAULT_FEISHU_HOLDINGS_TABLE_ENV",
    "DEFAULT_FEISHU_BOT_ALLOWED_OPEN_IDS_ENV",
    "DEFAULT_FEISHU_BOT_APP_ID_ENV",
    "DEFAULT_FEISHU_BOT_APP_SECRET_ENV",
    "DEFAULT_FEISHU_BOT_ENCRYPT_KEY_ENV",
    "DEFAULT_FEISHU_BOT_USER_OPEN_ID_ENV",
    "DEFAULT_FEISHU_BOT_VERIFICATION_TOKEN_ENV",
    "FeishuBotConfig",
    "FeishuHoldingsConfig",
    "resolve_feishu_holdings_config",
    "resolve_feishu_bot_config",
]
