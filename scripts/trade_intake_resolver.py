from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

from scripts.feishu_bitable import parse_note_kv, safe_float
from scripts.option_positions_core.domain import (
    effective_contracts_open,
    effective_expiration,
    exp_ms_to_datetime,
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
class IntakeResolution:
    status: str
    action: str | None
    reason: str
    deal_id: str | None
    account: str | None
    operations: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _failure(
    *,
    status: str,
    action: str | None,
    reason: str,
    deal: NormalizedTradeDeal,
    operations: list[dict[str, Any]] | None = None,
) -> IntakeResolution:
    return IntakeResolution(
        status=status,
        action=action,
        reason=reason,
        deal_id=deal.deal_id,
        account=deal.internal_account,
        operations=list(operations or []),
    )


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
    strike = safe_float(fields.get("strike"))
    if strike is not None:
        return float(strike)
    return safe_float(parse_note_kv(fields.get("note") or "", "strike"))


def _record_expiration_ymd(fields: dict[str, Any]) -> str | None:
    exp_ms, _ = effective_expiration(fields)
    if exp_ms is None:
        return None
    dt = exp_ms_to_datetime(exp_ms)
    return dt.date().isoformat() if dt is not None else None


def load_close_candidate_records(repo: OptionPositionsRepoLike) -> list[dict[str, Any]]:
    return list(load_option_position_records(repo))


def _iter_open_candidates(repo: OptionPositionsRepoLike, deal: NormalizedTradeDeal) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in load_close_candidate_records(repo):
        record_id = str(item.get("record_id") or item.get("id") or "").strip()
        fields = item.get("fields") or {}
        if not record_id or not isinstance(fields, dict):
            continue
        if normalize_broker(fields.get("broker") or fields.get("market")) != "富途":
            continue
        if normalize_account(fields.get("account")) != normalize_account(deal.internal_account):
            continue
        if str(fields.get("symbol") or "").strip().upper() != str(deal.symbol or "").strip().upper():
            continue
        if normalize_option_type(fields.get("option_type")) != normalize_option_type(deal.option_type):
            continue
        if normalize_side(fields.get("side")) != "short":
            continue
        if normalize_status(fields.get("status")) != "open":
            continue
        if effective_contracts_open(fields) <= 0:
            continue
        if _record_strike(fields) != float(deal.strike):
            continue
        if _record_expiration_ymd(fields) != str(deal.expiration_ymd):
            continue
        out.append(
            {
                "record_id": record_id,
                "fields": fields,
                "opened_at": int(safe_float(fields.get("opened_at")) or 0),
            }
        )
    out.sort(key=lambda row: (int(row.get("opened_at") or 0), str(row.get("record_id") or "")))
    return out


def match_close_positions(repo: OptionPositionsRepoLike, deal: NormalizedTradeDeal) -> list[CloseMatch]:
    remaining = int(deal.contracts or 0)
    if remaining <= 0:
        raise ValueError("contracts must be > 0 for close matching")
    matches: list[CloseMatch] = []
    candidates = _iter_open_candidates(repo, deal)
    for item in candidates:
        fields = item.get("fields") or {}
        open_qty = effective_contracts_open(fields)
        if open_qty <= 0:
            continue
        take = min(open_qty, remaining)
        if take <= 0:
            continue
        matches.append(
            CloseMatch(
                record_id=str(item["record_id"]),
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
) -> IntakeResolution:
    if lookup_deal_state(state, deal.deal_id) is not None:
        return _failure(status="skipped", action=None, reason="duplicate_deal_id", deal=deal)

    if not deal.deal_id:
        return _failure(status="unresolved", action=None, reason="missing_required_fields:deal_id", deal=deal)
    if not deal.internal_account:
        return _failure(status="unresolved", action=None, reason="missing_account_mapping", deal=deal)
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
                operations=[apply_trade_open_with(repo, deal, persist_trade_event_fn=persist_trade_event)],
            )
        preview = preview_trade_open(deal)
        return IntakeResolution(
            status="dry_run",
            action="open",
            reason="preview_open",
            deal_id=deal.deal_id,
            account=deal.internal_account,
            operations=[{"action": "open", "fields": preview["fields"]}],
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
            persist_trade_event_fn=persist_trade_event,
        )
        return IntakeResolution(
            status="applied",
            action="close",
            reason="applied_close",
            deal_id=deal.deal_id,
            account=deal.internal_account,
            operations=operations,
        )

    operations = preview_trade_close(repo, matches=matches, deal=deal)
    return IntakeResolution(
        status="dry_run",
        action="close",
        reason="preview_close",
        deal_id=deal.deal_id,
        account=deal.internal_account,
        operations=operations,
    )
