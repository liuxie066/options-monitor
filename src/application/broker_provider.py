"""Account-scoped holdings/cash providers; trade intake remains a separate capability."""
from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol

from src.application.account_config import build_account_portfolio_source_plan
from src.application.plugin_runtime import require_plugin


class BrokerPortfolioProvider(Protocol):
    def __call__(self, *, cfg: Mapping[str, Any], account: str | None, **kwargs: Any) -> dict[str, Any]: ...


def resolve_broker_portfolio_provider(
    config: dict[str, Any], *, account: str | None,
    futu_provider: Callable[..., dict[str, Any]],
) -> BrokerPortfolioProvider:
    provider = build_account_portfolio_source_plan(config, account=account).primary_source
    if provider == "futu":
        return require_plugin("broker-futu").resolve_portfolio_provider(futu_provider)
    if provider == "longbridge":
        return require_plugin("broker-longbridge").resolve_portfolio_provider()
    raise ValueError(f"unsupported broker portfolio provider: {provider}")
