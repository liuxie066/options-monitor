"""Optional Futu account capability; transport and financial owners stay in OM."""
from __future__ import annotations

from typing import Any, Callable, Iterable

from src.infrastructure.futu_gateway import FutuGateway, check_fetch_control


def resolve_portfolio_provider(futu_provider: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    return futu_provider


def build_ready_broker_gateway(
    *,
    expected_account_ids: Iterable[str],
    trd_env: str,
    host: str = "127.0.0.1",
    port: int = 11111,
    is_option_chain_cache_enabled: bool = False,
    backend_cls: Any | None = None,
    client_cls: Any | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> FutuGateway:
    """Build a gateway and verify only broker identity/readiness."""
    from src.infrastructure import futu_gateway

    check_fetch_control(deadline, cancelled)
    gateway = futu_gateway.build_futu_gateway(
        host=host,
        port=port,
        is_option_chain_cache_enabled=is_option_chain_cache_enabled,
        backend_cls=backend_cls,
        client_cls=client_cls,
    )
    try:
        gateway.ensure_broker_ready(
            expected_account_ids=expected_account_ids,
            trd_env=trd_env,
            **({"deadline": deadline, "cancelled": cancelled}
               if deadline is not None or cancelled is not None else {}),
        )
        check_fetch_control(deadline, cancelled)
        return gateway
    except BaseException:
        gateway.close()
        raise
