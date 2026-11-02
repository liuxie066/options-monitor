"""Optional read-only Longbridge network adapter."""
from __future__ import annotations

import hmac
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

from src.infrastructure import longbridge_gateway
from src.infrastructure.longbridge_gateway import ACCOUNT_IDENTITY_ERROR, _credentials, _fingerprint


def resolve_portfolio_provider():
    from src.application.longbridge_portfolio_context import fetch_longbridge_portfolio_context

    return fetch_longbridge_portfolio_context


class LongbridgeGateway:
    """Only assets are exposed. No orders, executions, subscriptions or token writes."""

    def __init__(
        self, settings: Mapping[str, Any], *, client: Any = None,
        context_factory: Callable[..., Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if settings.get("region") != "ap":
            raise ValueError("longbridge region must be ap for this asset adapter")
        if settings.get("trd_env") != "REAL":
            raise ValueError("longbridge asset adapter currently supports REAL only")
        # Explicit mappings are caller-owned snapshots, including healthcheck's env-file selection.
        env = longbridge_gateway.build_effective_env().values if environ is None else environ
        expected = settings.get("credential_binding_sha256")
        self.bound_account_id = None
        self.identity_error = ACCOUNT_IDENTITY_ERROR
        self.account_identity_evidence = {
            "schema_version": "longbridge.account_identity.v1", "status": "unverified",
            "binding_method": "user_confirmed_credentials_sha256", "broker_echoed": False,
            "account_id": None, "credential_binding_sha256": None,
        }
        credentials = None
        if expected is not None:
            try:
                credentials = _credentials(settings, env)
                actual = _fingerprint(settings, credentials)
                if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                    self.identity_error = "longbridge_credential_binding_invalid"
                elif not hmac.compare_digest(expected, actual):
                    self.identity_error = "longbridge_credential_binding_mismatch"
                else:
                    self.bound_account_id = settings["account_id"]
                    self.identity_error = None
                    self.account_identity_evidence.update(status="user_bound", account_id=self.bound_account_id,
                                                          credential_binding_sha256=actual)
            except ValueError:
                self.identity_error = "longbridge_binding_credentials_unavailable"
        if client is not None:
            self._client = client
            self._owned = False
            return
        credentials = credentials or _credentials(settings, env)
        # SDK defaults can inherit endpoint, paper and logging overrides from env.
        # Reject those overrides rather than silently changing the configured scope.
        if any(env.get(name) or os.environ.get(name) for name in (
            "LONGBRIDGE_HTTP_URL", "LONGBRIDGE_TRADE_WS_URL",
            "LONGBRIDGE_PAPERTRADING", "LONGBRIDGE_LOG_PATH",
        )):
            raise ValueError("longbridge SDK endpoint/paper/log environment overrides are unsupported")
        if context_factory is None:
            try:
                from longbridge.openapi import Config, TradeContext
            except ImportError as exc:
                raise RuntimeError("longbridge SDK missing; install requirements/longbridge.txt separately") from exc
            config = Config.from_apikey(
                **credentials, http_url="https://openapi.longbridge.com",
                enable_papertrading=False, enable_print_quote_packages=False,
            )
            self._client = TradeContext(config)
        else:
            self._client = context_factory(**credentials, enable_papertrading=False)
        self._owned = True

    def account_balance(self) -> Any:
        return self._client.account_balance()

    def stock_positions(self) -> Any:
        return self._client.stock_positions()

    def close(self) -> None:
        if self._owned:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
            self._client = None

    # The current public SDK has no account-profile identity query. account_channel
    # is an account *type*. Never turn configured account_id into observed identity.
    @property
    def observed_account_id(self) -> None:
        return None
