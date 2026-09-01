from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


STRATEGY_SELL_PUT = "sell_put"
STRATEGY_COVERED_CALL = "sell_call"
STRATEGY_COMBO_YIELD = "combo_yield"
STRATEGY_CLOSE_ADVICE = "close_advice"
STRATEGY_OTHER = "other"


@dataclass(frozen=True)
class StrategyTerm:
    internal_id: str
    display_name: str
    section_label: str
    action_label: str
    aliases: tuple[str, ...] = ()


STRATEGY_TERMS: tuple[StrategyTerm, ...] = (
    StrategyTerm(
        internal_id=STRATEGY_SELL_PUT,
        display_name="Cash-Secured Put (CSP)",
        section_label="CSP",
        action_label="CSP",
        aliases=("put", "sell put", "sell-put", "csp", "cash secured put"),
    ),
    StrategyTerm(
        internal_id=STRATEGY_COVERED_CALL,
        display_name="Covered Call (CC)",
        section_label="CC",
        action_label="CC",
        aliases=("call", "sell call", "sell-call", "covered_call", "covered call", "covered-call", "cc"),
    ),
    StrategyTerm(
        internal_id=STRATEGY_COMBO_YIELD,
        display_name="Combo Yield",
        section_label="Combo Yield",
        action_label="组合收益",
        aliases=(
            "yield_enhancement",
            "yield enhancement",
            "yield-enhancement",
            "enhancement",
            "combo yield",
            "combo-yield",
            "ye",
        ),
    ),
    StrategyTerm(
        internal_id=STRATEGY_CLOSE_ADVICE,
        display_name="Close Advice",
        section_label="Close Advice",
        action_label="平仓建议",
        aliases=("close advice", "close-advice"),
    ),
)


_TERMS_BY_ID = {term.internal_id: term for term in STRATEGY_TERMS}


def _normalize_strategy_token(value: str | None) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


_ALIASES_BY_TOKEN: dict[str, str] = {}
for _term in STRATEGY_TERMS:
    _ALIASES_BY_TOKEN[_normalize_strategy_token(_term.internal_id)] = _term.internal_id
    for _alias in _term.aliases:
        _ALIASES_BY_TOKEN[_normalize_strategy_token(_alias)] = _term.internal_id


def canonical_strategy_id(value: str | None) -> str:
    token = _normalize_strategy_token(value)
    if not token:
        return STRATEGY_OTHER
    return _ALIASES_BY_TOKEN.get(token, token)


def _fallback_label(strategy_id: str) -> str:
    token = canonical_strategy_id(strategy_id)
    if not token:
        return STRATEGY_OTHER.title()
    return token.replace("_", " ").title()


def strategy_display_name(strategy_id: str) -> str:
    canonical = canonical_strategy_id(strategy_id)
    term = _TERMS_BY_ID.get(canonical)
    return term.display_name if term else _fallback_label(canonical)


def strategy_section_label(strategy_id: str) -> str:
    canonical = canonical_strategy_id(strategy_id)
    term = _TERMS_BY_ID.get(canonical)
    return term.section_label if term else _fallback_label(canonical)


def strategy_action_label(strategy_id: str) -> str:
    canonical = canonical_strategy_id(strategy_id)
    term = _TERMS_BY_ID.get(canonical)
    return term.action_label if term else _fallback_label(canonical)


def strategy_key_help(strategy_ids: Iterable[str]) -> str:
    parts: list[str] = []
    for raw_id in strategy_ids:
        strategy_id = canonical_strategy_id(raw_id)
        display_name = strategy_display_name(strategy_id)
        if _normalize_strategy_token(display_name) != strategy_id:
            parts.append(f"{strategy_id} ({display_name} internal key)")
        else:
            parts.append(strategy_id)
    return "|".join(parts)
