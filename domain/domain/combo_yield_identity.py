from __future__ import annotations

from domain.domain.symbol_identity import canonical_symbol


def combo_yield_pair_fingerprint(*, symbol: str, put_contract_symbol: str, call_contract_symbol: str) -> str:
    normalized_symbol = canonical_symbol(symbol) or str(symbol or "").strip().upper()
    return "|".join(
        (
            "combo_yield",
            normalized_symbol,
            str(put_contract_symbol or "").strip(),
            str(call_contract_symbol or "").strip(),
        )
    )


def combo_yield_strategy_group_id(*, account: str | None, pair_fingerprint: str) -> str | None:
    normalized_account = str(account or "").strip().lower()
    fingerprint = str(pair_fingerprint or "").strip()
    if not normalized_account or not fingerprint:
        return None
    return f"combo_yield:{normalized_account}:{fingerprint}"


__all__ = ["combo_yield_pair_fingerprint", "combo_yield_strategy_group_id"]
