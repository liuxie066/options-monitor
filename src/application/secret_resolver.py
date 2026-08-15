from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.application.secret_store import (
    FEISHU_BOT_APP_SECRET,
    FEISHU_HOLDINGS_APP_SECRET,
    SecretProvider,
    resolve_secret,
    resolve_secret_status,
)
from src.application.settings import build_effective_env
from src.application.payload_helpers import as_dict as _dict


DEFAULT_FEISHU_APP_ID_ENV = "OM_FEISHU_APP_ID"
DEFAULT_FEISHU_APP_SECRET_ENV = "OM_FEISHU_APP_SECRET"
DEFAULT_FEISHU_HOLDINGS_TABLE_ENV = "OM_FEISHU_HOLDINGS_TABLE"
DEFAULT_FEISHU_BOT_APP_ID_ENV = "OM_FEISHU_BOT_APP_ID"
DEFAULT_FEISHU_BOT_APP_SECRET_ENV = "OM_FEISHU_BOT_APP_SECRET"
DEFAULT_FEISHU_BOT_USER_OPEN_ID_ENV = "OM_FEISHU_BOT_USER_OPEN_ID"
DEFAULT_FEISHU_BOT_ALLOWED_OPEN_IDS_ENV = "OM_FEISHU_BOT_ALLOWED_OPEN_IDS"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _env(environ: Mapping[str, str] | None, name: str) -> str:
    env = build_effective_env(environ=environ).values
    return _text(env.get(name))


def _secret(
    logical_name: str,
    *,
    environ: Mapping[str, str] | None,
    provider: SecretProvider | None,
    legacy_env_name: str,
) -> str:
    return _text(
        resolve_secret(
            logical_name,
            provider=provider,
            environ=environ if provider is None else None,
            legacy_env_name=legacy_env_name,
        )
    )


@dataclass(frozen=True)
class FeishuHoldingsConfig:
    app_id: str
    app_secret: str
    holdings_ref: str
    app_id_env: str
    app_secret_env: str
    app_secret_credential_name: str
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
            missing.append(self.app_secret_credential_name)
        if "/" not in self.holdings_ref:
            missing.append(self.holdings_env)
        return tuple(missing)

    def redacted_status(self) -> dict[str, Any]:
        return {
            "app_id_env": self.app_id_env,
            "app_id_configured": bool(self.app_id),
            "app_secret_env": self.app_secret_env,
            "app_secret_credential_name": self.app_secret_credential_name,
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
    app_id_env: str
    app_secret_env: str
    app_secret_credential_name: str
    user_open_id_env: str
    allowed_open_ids_env: str

    @property
    def credentials_ready(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @property
    def send_ready(self) -> bool:
        return bool(self.credentials_ready and self.user_open_id)

    @property
    def inbound_ready(self) -> bool:
        return bool(self.credentials_ready and self.allowed_open_ids)

    @property
    def credential_missing_fields(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.app_id:
            missing.append(self.app_id_env)
        if not self.app_secret:
            missing.append(self.app_secret_credential_name)
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
            "app_secret_credential_name": self.app_secret_credential_name,
            "app_secret_configured": bool(self.app_secret),
            "user_open_id_env": self.user_open_id_env,
            "user_open_id_configured": bool(self.user_open_id),
            "allowed_open_ids_env": self.allowed_open_ids_env,
            "allowed_open_ids_count": len(self.allowed_open_ids),
        }


def resolve_feishu_holdings_config(
    data_cfg: dict[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    secret_provider: SecretProvider | None = None,
) -> FeishuHoldingsConfig:
    feishu_cfg = _dict(_dict(data_cfg).get("feishu"))
    tables = _dict(feishu_cfg.get("tables"))
    app_id_env = _text(feishu_cfg.get("app_id_env")) or DEFAULT_FEISHU_APP_ID_ENV
    app_secret_env = _text(feishu_cfg.get("app_secret_env")) or DEFAULT_FEISHU_APP_SECRET_ENV
    holdings_env = _text(tables.get("holdings_env") or feishu_cfg.get("holdings_env")) or DEFAULT_FEISHU_HOLDINGS_TABLE_ENV
    return FeishuHoldingsConfig(
        app_id=_env(environ, app_id_env),
        app_secret=_secret(
            FEISHU_HOLDINGS_APP_SECRET,
            environ=environ,
            provider=secret_provider,
            legacy_env_name=app_secret_env,
        ),
        holdings_ref=_env(environ, holdings_env),
        app_id_env=app_id_env,
        app_secret_env=app_secret_env,
        app_secret_credential_name=FEISHU_HOLDINGS_APP_SECRET,
        holdings_env=holdings_env,
    )


def resolve_feishu_bot_config(
    notifications: dict[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    secret_provider: SecretProvider | None = None,
    metadata_only: bool = False,
) -> FeishuBotConfig:
    del notifications
    app_id_env = DEFAULT_FEISHU_BOT_APP_ID_ENV
    app_secret_env = DEFAULT_FEISHU_BOT_APP_SECRET_ENV
    user_open_id_env = DEFAULT_FEISHU_BOT_USER_OPEN_ID_ENV
    allowed_open_ids_env = DEFAULT_FEISHU_BOT_ALLOWED_OPEN_IDS_ENV
    user_open_id = _env(environ, user_open_id_env)
    allowed_open_ids = _split_csv(_env(environ, allowed_open_ids_env)) or ((user_open_id,) if user_open_id else ())
    if metadata_only:
        # Check/diagnostic path: report credential presence without resolving the value.
        # Falls back to legacy env check so the health check works in any backend context.
        status = resolve_secret_status(
            FEISHU_BOT_APP_SECRET,
            provider=secret_provider,
            environ=environ if secret_provider is None else None,
            legacy_env_name=app_secret_env,
        )
        app_secret = "configured" if status.configured else ""
    else:
        app_secret = _secret(
            FEISHU_BOT_APP_SECRET,
            environ=environ,
            provider=secret_provider,
            legacy_env_name=app_secret_env,
        )
    return FeishuBotConfig(
        app_id=_env(environ, app_id_env),
        app_secret=app_secret,
        user_open_id=user_open_id,
        allowed_open_ids=allowed_open_ids,
        app_id_env=app_id_env,
        app_secret_env=app_secret_env,
        app_secret_credential_name=FEISHU_BOT_APP_SECRET,
        user_open_id_env=user_open_id_env,
        allowed_open_ids_env=allowed_open_ids_env,
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
    "DEFAULT_FEISHU_BOT_USER_OPEN_ID_ENV",
    "FeishuBotConfig",
    "FeishuHoldingsConfig",
    "resolve_feishu_holdings_config",
    "resolve_feishu_bot_config",
]
