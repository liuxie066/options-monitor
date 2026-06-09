from __future__ import annotations

from src.application.strategy_lab.domains.base import StrategyDomainAdapter
from src.application.strategy_lab.domains.combo_yield import ADAPTER as COMBO_YIELD_ADAPTER
from src.application.strategy_lab.domains.covered_call import ADAPTER as COVERED_CALL_ADAPTER
from src.application.strategy_lab.domains.sell_put import ADAPTER as SELL_PUT_ADAPTER


DOMAIN_ADAPTERS = {
    SELL_PUT_ADAPTER.strategy_family: SELL_PUT_ADAPTER,
    COVERED_CALL_ADAPTER.strategy_family: COVERED_CALL_ADAPTER,
    COMBO_YIELD_ADAPTER.strategy_family: COMBO_YIELD_ADAPTER,
}


def get_domain_adapter(strategy_family: str) -> StrategyDomainAdapter | None:
    return DOMAIN_ADAPTERS.get(str(strategy_family or "").strip().lower())


def list_domain_adapters() -> list[StrategyDomainAdapter]:
    return [DOMAIN_ADAPTERS[key] for key in ("sell_put", "covered_call", "combo_yield")]


__all__ = [
    "DOMAIN_ADAPTERS",
    "StrategyDomainAdapter",
    "get_domain_adapter",
    "list_domain_adapters",
]
