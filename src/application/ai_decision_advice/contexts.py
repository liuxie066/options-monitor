from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from domain.domain.symbol_identity import canonical_symbol, symbol_currency
from src.application.ai_decision_advice.evidence_store import EvidenceIndex


@dataclass(frozen=True)
class FrozenInputs:
    """The four frozen input bundles plus their hashes (docs 7)."""

    candidates: dict[str, Any]
    portfolio: dict[str, Any]
    option_positions: dict[str, Any]
    external_evidence: dict[str, Any]
    candidate_snapshot_hash: str
    portfolio_context_hash: str
    option_positions_hash: str
    external_evidence_hash: str
    external_evidence_run_id: str | None

    def input_bindings(self) -> dict[str, Any]:
        return {
            "candidate_snapshot_hash": self.candidate_snapshot_hash,
            "portfolio_context_hash": self.portfolio_context_hash,
            "option_positions_hash": self.option_positions_hash,
            "external_evidence_hash": self.external_evidence_hash,
            "external_evidence_run_id": self.external_evidence_run_id,
        }


def freeze_candidates(
    snapshot: Mapping[str, Any],
    *,
    market: str,
) -> dict[str, Any]:
    """Project accepted candidates for the model (docs 7.1).

    Only accepted (ranked) candidates are included; rejected rows never leave
    the snapshot. Candidate IDs stay sealed-order stable.
    """

    market_norm = str(market or "").strip().upper()
    sell_put: list[dict[str, Any]] = []
    covered_call: list[dict[str, Any]] = []
    for item in snapshot.get("ranked_candidates") or []:
        if not isinstance(item, Mapping):
            continue
        facts = item.get("facts")
        if not isinstance(facts, Mapping):
            continue
        row = {
            "candidate_id": item.get("candidate_id"),
            "rank": item.get("rank"),
            "symbol": facts.get("symbol"),
            "strike": facts.get("strike"),
            "expiry": facts.get("expiry") or facts.get("expiration"),
            "multiplier": facts.get("multiplier"),
            "dte": facts.get("dte"),
            "delta": facts.get("delta"),
            "period_net_return": facts.get("period_net_return"),
            "annualized_gate": facts.get("annualized_net_return_on_cash_basis")
            or facts.get("annualized_net_premium_return"),
            "net_premium": facts.get("net_premium") or facts.get("net_income"),
        }
        mode = str(item.get("strategy_mode") or "")
        if mode == "put":
            sell_put.append(row)
        elif mode == "call":
            covered_call.append(row)
    return {
        "market": market_norm,
        "sell_put": sell_put,
        "covered_call": covered_call,
        "snapshot_content_sha256": snapshot.get("content_sha256"),
    }


def freeze_portfolio(
    portfolio_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Anonymized distribution input (docs 7.2).

    Weights are relative within ordinary stock holdings; no NAV, totals, cost
    basis, or account identifiers are exposed. v1 has no industry dimension.
    """

    stocks = (portfolio_context or {}).get("stocks_by_symbol")
    rows: list[dict[str, Any]] = []
    if isinstance(stocks, Mapping):
        total_shares = 0.0
        by_symbol: dict[str, dict[str, Any]] = {}
        for symbol, row in stocks.items():
            if not isinstance(row, Mapping):
                continue
            shares = float(row.get("shares") or 0)
            if shares <= 0:
                continue
            canonical = canonical_symbol(symbol) or str(symbol)
            currency = str(row.get("currency") or symbol_currency(canonical) or "")
            by_symbol[canonical] = {"shares": shares, "currency": currency}
            total_shares += shares
        for symbol, row in sorted(by_symbol.items()):
            rows.append(
                {
                    "symbol": symbol,
                    "currency": row["currency"],
                    "weight": round(row["shares"] / total_shares, 6) if total_shares > 0 else None,
                }
            )
    cash = (portfolio_context or {}).get("cash_by_currency")
    cash_currencies = sorted(str(currency) for currency in (cash or {}).keys()) if isinstance(cash, Mapping) else []
    return {
        "symbol_weights": rows,
        "cash_currencies": cash_currencies,
    }


def freeze_option_positions(
    position_lots: Iterable[Mapping[str, Any]],
    *,
    candidate_symbols: Iterable[str] = (),
) -> dict[str, Any]:
    """Open option holdings input (docs 7.3).

    Sends only symbol/side/type/strike/expiry/contracts plus same-underlying /
    same-expiry-window relationships to current candidates. No order or trade
    identifiers.
    """

    candidate_set = {canonical_symbol(symbol) or str(symbol) for symbol in candidate_symbols}
    expiries_by_symbol: dict[str, set[str]] = {}
    rows: list[dict[str, Any]] = []
    for lot in position_lots:
        if str(lot.get("status") or "").strip().lower() != "open":
            continue
        if int(lot.get("contracts_open") or 0) <= 0:
            continue
        key = lot.get("contract_key")
        if isinstance(key, Mapping):
            underlying = canonical_symbol(key.get("underlying_symbol")) or str(key.get("underlying_symbol") or "")
            option_type = str(key.get("option_type") or "").lower()
            side = str(key.get("position_side") or "").lower()
            strike = key.get("strike")
            expiry = key.get("expiration_ymd")
        else:
            underlying = canonical_symbol(getattr(key, "underlying_symbol", None)) or ""
            option_type = str(getattr(key, "option_type", "") or "").lower()
            side = str(getattr(key, "position_side", "") or "").lower()
            strike = getattr(key, "strike", None)
            expiry = getattr(key, "expiration_ymd", None)
        if not underlying:
            continue
        rows.append(
            {
                "symbol": underlying,
                "option_type": option_type,
                "side": side,
                "strike": strike,
                "expiry": expiry,
                "contracts": int(lot.get("contracts_open") or 0),
                "same_underlying_as_candidate": underlying in candidate_set,
            }
        )
        if expiry:
            expiries_by_symbol.setdefault(underlying, set()).add(str(expiry))
    for row in rows:
        symbol_expiries = expiries_by_symbol.get(row["symbol"], set())
        row["shared_expiry_with_other_position"] = len(symbol_expiries) > 1 and row["expiry"] in symbol_expiries
    return {"open_positions": rows}


def freeze_external_evidence(
    index: EvidenceIndex,
    *,
    symbols: Iterable[str],
) -> dict[str, Any]:
    """Latest valid evidence per symbol (docs 7.4)."""

    items: list[dict[str, Any]] = []
    for symbol in symbols:
        view = index.view_for(symbol)
        if view is None:
            items.append({"symbol": symbol, "coverage": "no_evidence", "evidence": []})
            continue
        items.append(
            {
                "symbol": symbol,
                "coverage": view.coverage,
                "unavailable_reason": view.unavailable_reason,
                "last_checked_at": view.last_checked_at,
                "evidence": [dict(row) for row in view.evidence],
            }
        )
    return {
        "frozen_at": index.frozen_at,
        "index_hash": index.index_hash(),
        "symbols": items,
    }


def build_frozen_inputs(
    *,
    snapshot: Mapping[str, Any],
    portfolio_context: Mapping[str, Any] | None,
    position_lots: Iterable[Mapping[str, Any]],
    evidence_index: EvidenceIndex,
    market: str,
    evidence_run_id: str | None = None,
) -> FrozenInputs:
    candidates = freeze_candidates(snapshot, market=market)
    candidate_symbols = [
        row["symbol"]
        for row in (*candidates["sell_put"], *candidates["covered_call"])
        if row.get("symbol")
    ]
    portfolio = freeze_portfolio(portfolio_context)
    option_positions = freeze_option_positions(position_lots, candidate_symbols=candidate_symbols)
    external = freeze_external_evidence(evidence_index, symbols=sorted(set(candidate_symbols)))
    return FrozenInputs(
        candidates=candidates,
        portfolio=portfolio,
        option_positions=option_positions,
        external_evidence=external,
        candidate_snapshot_hash=_hash(candidates),
        portfolio_context_hash=_hash(portfolio),
        option_positions_hash=_hash(option_positions),
        external_evidence_hash=str(external["index_hash"]),
        external_evidence_run_id=evidence_run_id,
    )


def _hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
