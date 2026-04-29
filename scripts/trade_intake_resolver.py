from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from scripts.feishu_bitable import safe_float
from scripts.option_positions_core.domain import (
    effective_contracts_open,
    effective_expiration_ymd,
    effective_strike,
    normalize_account,
    normalize_broker,
    normalize_option_type,
    normalize_side,
    normalize_status,
)
from scripts.option_positions_core.service import persist_trade_event
from scripts.trade_event_normalizer import NormalizedTradeDeal
from scripts.trade_intake_state import lookup_deal_state
from src.application.position_workflows import (
    apply_trade_close_with,
    apply_trade_open_with,
    preview_trade_close,
    preview_trade_open,
)
from src.application.option_positions_facade import load_option_position_records


class OptionPositionsRepoLike(Protocol):
    def list_records(self, *, page_size: int = 500) -> list[dict[str, Any]]: ...
    def get_record_fields(self, record_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CloseMatch:
    record_id: str
    contracts_to_close: int
    matched_by: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CloseCandidate:
    record_id: str
    broker: str
    account: str
    symbol: str
    option_type: str
    side: str
    status: str
    contracts_open: int
    strike: float | None
    expiration_ymd: str | None
    opened_at: int
    raw_fields: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IntakeResolution:
    status: str
    action: str | None
    reason: str
    deal_id: str | None
    account: str | None
    operations: list[dict[str, Any]]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _failure(
    *,
    status: str,
    action: str | None,
    reason: str,
    deal: NormalizedTradeDeal,
    operations: list[dict[str, Any]] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> IntakeResolution:
    return IntakeResolution(
        status=status,
        action=action,
        reason=reason,
        deal_id=deal.deal_id,
        account=deal.internal_account,
        operations=list(operations or []),
        diagnostics=dict(diagnostics or {}),
    )


def _missing_account_mapping_diagnostics(deal: NormalizedTradeDeal) -> dict[str, Any]:
    return {
        "futu_account_id": deal.futu_account_id,
        "visible_account_fields": dict(deal.visible_account_fields or {}),
        "account_mapping_keys": list(deal.account_mapping_keys or []),
    }


def _required_open_missing(deal: NormalizedTradeDeal) -> list[str]:
    src = {
        "deal_id": deal.deal_id,
        "account": deal.internal_account,
        "symbol": deal.symbol,
        "option_type": deal.option_type,
        "contracts": deal.contracts,
        "price": deal.price,
        "strike": deal.strike,
        "multiplier": deal.multiplier,
        "expiration_ymd": deal.expiration_ymd,
        "currency": deal.currency,
    }
    return [k for k, v in src.items() if v in (None, "")]


def _required_close_missing(deal: NormalizedTradeDeal) -> list[str]:
    src = {
        "deal_id": deal.deal_id,
        "account": deal.internal_account,
        "symbol": deal.symbol,
        "option_type": deal.option_type,
        "contracts": deal.contracts,
        "price": deal.price,
        "strike": deal.strike,
        "expiration_ymd": deal.expiration_ymd,
    }
    return [k for k, v in src.items() if v in (None, "")]


def _record_strike(fields: dict[str, Any]) -> float | None:
    return effective_strike(fields)


def _record_expiration_ymd(fields: dict[str, Any]) -> str | None:
    return effective_expiration_ymd(fields)


def load_close_candidate_records(repo: OptionPositionsRepoLike) -> list[dict[str, Any]]:
    return list(load_option_position_records(repo))


def _normalize_close_candidate(item: dict[str, Any]) -> CloseCandidate | None:
    record_id = str(item.get("record_id") or item.get("id") or "").strip()
    fields = item.get("fields") or {}
    if not record_id or not isinstance(fields, dict):
        return None
    return CloseCandidate(
        record_id=record_id,
        broker=normalize_broker(fields.get("broker")),
        account=normalize_account(fields.get("account")),
        symbol=str(fields.get("symbol") or "").strip().upper(),
        option_type=normalize_option_type(fields.get("option_type")),
        side=normalize_side(fields.get("side")),
        status=normalize_status(fields.get("status")),
        contracts_open=effective_contracts_open(fields),
        strike=_record_strike(fields),
        expiration_ymd=_record_expiration_ymd(fields),
        opened_at=int(safe_float(fields.get("opened_at")) or 0),
        raw_fields=dict(fields),
    )


def _iter_open_candidates(repo: OptionPositionsRepoLike, deal: NormalizedTradeDeal) -> list[CloseCandidate]:
    out: list[CloseCandidate] = []
    deal_account = normalize_account(deal.internal_account)
    deal_symbol = str(deal.symbol or "").strip().upper()
    deal_option_type = normalize_option_type(deal.option_type)
    deal_strike = float(deal.strike) if deal.strike is not None else None
    deal_expiration_ymd = str(deal.expiration_ymd or "").strip() or None
    for item in load_close_candidate_records(repo):
        candidate = _normalize_close_candidate(item)
        if candidate is None:
            continue
        if candidate.broker != "富途":
            continue
        if candidate.account != deal_account:
            continue
        if candidate.symbol != deal_symbol:
            continue
        if candidate.option_type != deal_option_type:
            continue
        if candidate.side != "short":
            continue
        if candidate.status != "open":
            continue
        if candidate.contracts_open <= 0:
            continue
        if candidate.strike != deal_strike:
            continue
        if candidate.expiration_ymd != deal_expiration_ymd:
            continue
        out.append(candidate)
    out.sort(key=lambda row: (int(row.opened_at or 0), row.record_id))
    return out


def match_close_positions(repo: OptionPositionsRepoLike, deal: NormalizedTradeDeal) -> list[CloseMatch]:
    remaining = int(deal.contracts or 0)
    if remaining <= 0:
        raise ValueError("contracts must be > 0 for close matching")
    matches: list[CloseMatch] = []
    candidates = _iter_open_candidates(repo, deal)
    for item in candidates:
        open_qty = item.contracts_open
        if open_qty <= 0:
            continue
        take = min(open_qty, remaining)
        if take <= 0:
            continue
        matches.append(
            CloseMatch(
                record_id=item.record_id,
                contracts_to_close=int(take),
                matched_by="strict_exact_fifo",
            )
        )
        remaining -= int(take)
        if remaining <= 0:
            break
    if remaining > 0:
        raise ValueError(f"close_match_insufficient_contracts: remaining={remaining}")
    if not matches:
        raise ValueError("close_match_not_found")
    return matches


def resolve_trade_deal(
    deal: NormalizedTradeDeal,
    *,
    repo: OptionPositionsRepoLike,
    state: dict[str, Any] | None,
    apply_changes: bool,
    persist_trade_event_fn=None,
) -> IntakeResolution:
    persist_fn = persist_trade_event_fn or persist_trade_event
    if lookup_deal_state(state, deal.deal_id) is not None:
        return _failure(status="skipped", action=None, reason="duplicate_deal_id", deal=deal)

    if not deal.deal_id:
        return _failure(status="unresolved", action=None, reason="missing_required_fields:deal_id", deal=deal)
    if not deal.internal_account:
        diagnostics = _missing_account_mapping_diagnostics(deal)
        futu_account_id = str(diagnostics.get("futu_account_id") or "").strip()
        reason = "missing_account_mapping"
        if futu_account_id:
            reason += f":futu_account_id={futu_account_id}"
        return _failure(
            status="unresolved",
            action=None,
            reason=reason,
            deal=deal,
            diagnostics=diagnostics,
        )
    if not deal.symbol or not deal.option_type:
        return _failure(status="unresolved", action=None, reason="not_option_deal", deal=deal)
    if deal.position_effect not in ("open", "close"):
        return _failure(status="unresolved", action=None, reason="unknown_position_effect", deal=deal)

    if deal.position_effect == "open":
        if deal.side != "sell":
            return _failure(status="unresolved", action="open", reason="unsupported_open_side", deal=deal)
        missing = _required_open_missing(deal)
        if missing:
            return _failure(
                status="unresolved",
                action="open",
                reason="missing_required_fields:" + ",".join(missing),
                deal=deal,
            )
        if apply_changes:
            return IntakeResolution(
                status="applied",
                action="open",
                reason="applied_open",
                deal_id=deal.deal_id,
                account=deal.internal_account,
                operations=[apply_trade_open_with(repo, deal, persist_trade_event_fn=persist_fn)],
                diagnostics={},
            )
        preview = preview_trade_open(deal)
        return IntakeResolution(
            status="dry_run",
            action="open",
            reason="preview_open",
            deal_id=deal.deal_id,
            account=deal.internal_account,
            operations=[{"action": "open", "fields": preview["fields"]}],
            diagnostics={},
        )

    missing = _required_close_missing(deal)
    if deal.side != "buy":
        return _failure(status="unresolved", action="close", reason="unsupported_close_side", deal=deal)
    if missing:
        return _failure(
            status="unresolved",
            action="close",
            reason="missing_required_fields:" + ",".join(missing),
            deal=deal,
        )
    try:
        matches = match_close_positions(repo, deal)
    except ValueError as exc:
        return _failure(status="unresolved", action="close", reason=str(exc), deal=deal)

    operations: list[dict[str, Any]] = []
    if apply_changes:
        operations = apply_trade_close_with(
            repo,
            matches=matches,
            deal=deal,
            persist_trade_event_fn=persist_fn,
        )
        return IntakeResolution(
            status="applied",
            action="close",
            reason="applied_close",
            deal_id=deal.deal_id,
            account=deal.internal_account,
            operations=operations,
            diagnostics={},
        )

    operations = preview_trade_close(repo, matches=matches, deal=deal)
    return IntakeResolution(
        status="dry_run",
        action="close",
        reason="preview_close",
        deal_id=deal.deal_id,
        account=deal.internal_account,
        operations=operations,
        diagnostics={},
    )
