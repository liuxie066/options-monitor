from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_FEISHU_APP_ID_ENV = "OM_FEISHU_APP_ID"
DEFAULT_FEISHU_APP_SECRET_ENV = "OM_FEISHU_APP_SECRET"
DEFAULT_FEISHU_HOLDINGS_TABLE_ENV = "OM_FEISHU_HOLDINGS_TABLE"
DEFAULT_NOTIFY_FEISHU_APP_ID_ENV = "OM_NOTIFY_FEISHU_APP_ID"
DEFAULT_NOTIFY_FEISHU_APP_SECRET_ENV = "OM_NOTIFY_FEISHU_APP_SECRET"


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
class FeishuNotificationAppConfig:
    app_id: str
    app_secret: str
    app_id_env: str
    app_secret_env: str

    @property
    def ready(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @property
    def missing_fields(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.app_id:
            missing.append(self.app_id_env)
        if not self.app_secret:
            missing.append(self.app_secret_env)
        return tuple(missing)

    def redacted_status(self) -> dict[str, Any]:
        return {
            "app_id_env": self.app_id_env,
            "app_id_configured": bool(self.app_id),
            "app_secret_env": self.app_secret_env,
            "app_secret_configured": bool(self.app_secret),
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


def resolve_feishu_notification_app_config(
    notifications: dict[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> FeishuNotificationAppConfig:
    notifications_cfg = _dict(notifications)
    feishu_cfg = _dict(notifications_cfg.get("feishu"))
    app_id_env = (
        _text(feishu_cfg.get("app_id_env"))
        or _text(notifications_cfg.get("app_id_env"))
        or DEFAULT_NOTIFY_FEISHU_APP_ID_ENV
    )
    app_secret_env = (
        _text(feishu_cfg.get("app_secret_env"))
        or _text(notifications_cfg.get("app_secret_env"))
        or DEFAULT_NOTIFY_FEISHU_APP_SECRET_ENV
    )
    return FeishuNotificationAppConfig(
        app_id=_env(environ, app_id_env),
        app_secret=_env(environ, app_secret_env),
        app_id_env=app_id_env,
        app_secret_env=app_secret_env,
    )


__all__ = [
    "DEFAULT_FEISHU_APP_ID_ENV",
    "DEFAULT_FEISHU_APP_SECRET_ENV",
    "DEFAULT_FEISHU_HOLDINGS_TABLE_ENV",
    "DEFAULT_NOTIFY_FEISHU_APP_ID_ENV",
    "DEFAULT_NOTIFY_FEISHU_APP_SECRET_ENV",
    "FeishuHoldingsConfig",
    "FeishuNotificationAppConfig",
    "resolve_feishu_holdings_config",
    "resolve_feishu_notification_app_config",
]
