from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from src.application.ledger.api import (
    BrokerTradeOperation,
    CloseTargetResolution,
    LotCloseCandidate as CloseCandidate,
    LotCloseMatch as CloseMatch,
    LotCloseResolutionError,
    list_close_lot_candidates,
    record_normalized_trade_event,
    resolve_broker_trade_close_targets,
)
from src.application.trades.normalizer import NormalizedTradeDeal
from src.application.trades.state import is_retryable_unresolved_deal, lookup_deal_state
from src.application.trades.workflows import (
    apply_trade_close_with,
    apply_trade_open_with,
    preview_trade_close,
    preview_trade_open,
)


class OptionPositionsRepoLike(Protocol):
    def list_position_lots(self) -> list[dict[str, Any]]: ...
    def get_record_fields(self, record_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class IntakeResolution:
    status: str
    action: str | None
    reason: str
    deal_id: str | None
    account: str | None
    operations: list[BrokerTradeOperation]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "action": self.action,
            "reason": self.reason,
            "deal_id": self.deal_id,
            "account": self.account,
            "operations": [item.to_payload() for item in self.operations],
            "diagnostics": dict(self.diagnostics),
        }


def _failure(
    *,
    status: str,
    action: str | None,
    reason: str,
    deal: NormalizedTradeDeal,
    operations: list[BrokerTradeOperation] | None = None,
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


def _missing_required_fields_diagnostics(deal: NormalizedTradeDeal, missing: list[str]) -> dict[str, Any]:
    normalization = dict(getattr(deal, "normalization_diagnostics", {}) or {})
    multiplier_resolution = dict(normalization.get("multiplier_resolution") or {})
    symbol_info = dict(normalization.get("symbol") or {})
    retryable = set(missing) == {"multiplier"}
    return {
        "retryable": retryable,
        "missing_fields": list(missing),
        "canonical_symbol": deal.symbol,
        "raw_symbol_fields": dict(symbol_info.get("raw_fields") or {}),
        "multiplier_resolution": multiplier_resolution,
        "futu_account_id": deal.futu_account_id,
        "visible_account_fields": dict(deal.visible_account_fields or {}),
    }


def _invalid_required_fields_diagnostics(invalid: list[str]) -> dict[str, Any]:
    return {
        "retryable": False,
        "invalid_fields": list(invalid),
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


def _required_open_invalid(deal: NormalizedTradeDeal) -> list[str]:
    invalid: list[str] = []
    try:
        if deal.contracts is not None and int(deal.contracts) <= 0:
            invalid.append("contracts")
    except Exception:
        invalid.append("contracts")
    try:
        if deal.strike is not None and float(deal.strike) <= 0:
            invalid.append("strike")
    except Exception:
        invalid.append("strike")
    try:
        if deal.multiplier is not None and int(deal.multiplier) <= 0:
            invalid.append("multiplier")
    except Exception:
        invalid.append("multiplier")
    return invalid


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


def load_close_candidate_records(repo: OptionPositionsRepoLike) -> list[dict[str, Any]]:
    return list_close_lot_candidates(repo)


def match_close_positions(repo: OptionPositionsRepoLike, deal: NormalizedTradeDeal) -> list[CloseMatch]:
    return list(match_close_targets(repo, deal).matches)


def match_close_targets(repo: OptionPositionsRepoLike, deal: NormalizedTradeDeal) -> CloseTargetResolution:
    try:
        return resolve_broker_trade_close_targets(repo, deal=deal)
    except LotCloseResolutionError as exc:
        if exc.code == "invalid_quantity":
            raise ValueError("contracts must be > 0 for close matching") from exc
        if exc.code == "insufficient_contracts":
            remaining = exc.remaining_contracts
            if remaining is not None:
                raise ValueError(f"close_match_insufficient_contracts: remaining={remaining}") from exc
            raise ValueError("close_match_insufficient_contracts") from exc
        if exc.code == "not_found":
            raise ValueError("close_match_not_found") from exc
        raise ValueError(str(exc)) from exc


def resolve_trade_deal(
    deal: NormalizedTradeDeal,
    *,
    repo: OptionPositionsRepoLike,
    state: dict[str, Any] | None,
    apply_changes: bool,
    persist_trade_event_fn=None,
) -> IntakeResolution:
    persist_fn = persist_trade_event_fn or record_normalized_trade_event
    if lookup_deal_state(state, deal.deal_id) is not None and not is_retryable_unresolved_deal(state, deal.deal_id):
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
        if deal.side not in {"sell", "buy"}:
            return _failure(status="unresolved", action="open", reason="unsupported_open_side", deal=deal)
        missing = _required_open_missing(deal)
        if missing:
            return _failure(
                status="unresolved",
                action="open",
                reason="missing_required_fields:" + ",".join(missing),
                deal=deal,
                diagnostics=_missing_required_fields_diagnostics(deal, missing),
            )
        invalid = _required_open_invalid(deal)
        if invalid:
            return _failure(
                status="unresolved",
                action="open",
                reason="invalid_required_fields:" + ",".join(invalid),
                deal=deal,
                diagnostics=_invalid_required_fields_diagnostics(invalid),
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
            operations=[BrokerTradeOperation(action="open", fields=preview.fields)],
            diagnostics={},
        )

    missing = _required_close_missing(deal)
    if deal.side not in {"buy", "sell"}:
        return _failure(status="unresolved", action="close", reason="unsupported_close_side", deal=deal)
    if missing:
        return _failure(
            status="unresolved",
            action="close",
            reason="missing_required_fields:" + ",".join(missing),
            deal=deal,
        )
    try:
        close_target_resolution = match_close_targets(repo, deal)
    except ValueError as exc:
        return _failure(status="unresolved", action="close", reason=str(exc), deal=deal)
    matches = list(close_target_resolution.matches)
    close_target_diagnostics = {"close_target_resolution": close_target_resolution.to_dict()}

    operations: list[BrokerTradeOperation] = []
    if apply_changes:
        operations = apply_trade_close_with(
            repo,
            matches=matches,
            deal=deal,
            persist_trade_event_fn=persist_fn,
            close_target_resolution=close_target_resolution,
        )
        return IntakeResolution(
            status="applied",
            action="close",
            reason="applied_close",
            deal_id=deal.deal_id,
            account=deal.internal_account,
            operations=operations,
            diagnostics=close_target_diagnostics,
        )

    operations = preview_trade_close(
        repo,
        matches=matches,
        deal=deal,
        close_target_resolution=close_target_resolution,
    )
    return IntakeResolution(
        status="dry_run",
        action="close",
        reason="preview_close",
        deal_id=deal.deal_id,
        account=deal.internal_account,
        operations=operations,
        diagnostics=close_target_diagnostics,
    )
