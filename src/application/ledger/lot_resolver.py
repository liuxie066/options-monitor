from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from domain.domain.ledger.position_fields import (
    effective_contracts,
    effective_contracts_open,
    effective_expiration_ymd,
    effective_strike,
    exp_ms_to_ymd,
    normalize_account,
    normalize_broker,
    normalize_currency,
    normalize_option_type,
    normalize_side,
    normalize_status,
    parse_exp_to_ms,
)
from domain.domain.symbol_identity import canonical_symbol
from src.application.ledger.repository import require_option_positions_read_repo
from src.infrastructure.feishu_bitable import safe_float


@dataclass(frozen=True)
class LotCloseSelector:
    broker: str
    account: str
    symbol: str
    option_type: str
    side: str
    strike: float | None
    expiration_ymd: str | None
    contracts_to_close: int

    @classmethod
    def from_values(
        cls,
        *,
        broker: Any = "富途",
        account: Any,
        symbol: Any,
        option_type: Any,
        position_side: Any,
        strike: Any,
        expiration_ymd: Any,
        contracts_to_close: Any,
    ) -> "LotCloseSelector":
        return cls(
            broker=normalize_broker(broker),
            account=normalize_account(account),
            symbol=_canonical_selector_symbol(symbol),
            option_type=normalize_option_type(option_type),
            side=normalize_side(position_side),
            strike=float(strike) if strike is not None else None,
            expiration_ymd=_normalize_selector_expiration(expiration_ymd),
            contracts_to_close=int(contracts_to_close),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def missing_identity_fields(self) -> list[str]:
        return [
            key
            for key in ("broker", "account", "symbol", "option_type", "side", "strike", "expiration_ymd")
            if self.to_dict().get(key) in (None, "")
        ]


@dataclass(frozen=True)
class LotCloseCandidate:
    record_id: str
    broker: str
    account: str
    symbol: str
    option_type: str
    side: str
    status: str
    contracts: int
    contracts_open: int
    contracts_closed: Any
    strike: float | None
    expiration_ymd: str | None
    opened_at: int
    premium: Any
    currency: Any
    source_event_id: Any
    raw_fields: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LotCloseMatch:
    record_id: str
    contracts_to_close: int
    matched_by: str
    candidate: LotCloseCandidate | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "record_id": self.record_id,
            "contracts_to_close": self.contracts_to_close,
            "matched_by": self.matched_by,
        }
        if self.candidate is not None:
            payload["candidate"] = self.candidate.to_dict()
        return payload


@dataclass(frozen=True)
class CloseTargetResolution:
    source: str
    strategy: str
    selector: dict[str, Any]
    matches: tuple[LotCloseMatch, ...]

    @property
    def record_ids(self) -> tuple[str, ...]:
        return tuple(match.record_id for match in self.matches)

    @property
    def contracts_to_close(self) -> int:
        return sum(int(match.contracts_to_close or 0) for match in self.matches)

    @property
    def single_match(self) -> LotCloseMatch:
        if len(self.matches) != 1:
            raise ValueError(f"expected exactly one close target, got {len(self.matches)}")
        return self.matches[0]

    @property
    def single_candidate(self) -> LotCloseCandidate:
        candidate = self.single_match.candidate
        if candidate is None:
            raise ValueError("close target resolution is missing candidate details")
        return candidate

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "resolved",
            "source": self.source,
            "strategy": self.strategy,
            "selector": dict(self.selector),
            "target_count": len(self.matches),
            "record_ids": list(self.record_ids),
            "contracts_to_close": int(self.contracts_to_close),
            "targets": [match.to_dict() for match in self.matches],
        }


class LotCloseResolutionError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        selector: LotCloseSelector,
        candidates: list[LotCloseCandidate] | None = None,
        remaining_contracts: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.selector = selector
        self.candidates = list(candidates or [])
        self.remaining_contracts = remaining_contracts


def load_close_candidate_records(repo: Any) -> list[dict[str, Any]]:
    try:
        rows = require_option_positions_read_repo(repo).list_position_lots()
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def normalize_close_candidate(item: dict[str, Any]) -> LotCloseCandidate | None:
    record_id = str(item.get("record_id") or item.get("id") or "").strip()
    fields = item.get("fields") or {}
    if not record_id or not isinstance(fields, dict):
        return None
    return LotCloseCandidate(
        record_id=record_id,
        broker=normalize_broker(fields.get("broker")),
        account=normalize_account(fields.get("account")),
        symbol=_canonical_selector_symbol(fields.get("symbol")),
        option_type=normalize_option_type(fields.get("option_type")),
        side=normalize_side(fields.get("side")),
        status=normalize_status(fields.get("status")),
        contracts=effective_contracts(fields),
        contracts_open=effective_contracts_open(fields),
        contracts_closed=fields.get("contracts_closed"),
        strike=effective_strike(fields),
        expiration_ymd=effective_expiration_ymd(fields),
        opened_at=int(safe_float(fields.get("opened_at")) or 0),
        premium=fields.get("premium"),
        currency=fields.get("currency"),
        source_event_id=fields.get("source_event_id"),
        raw_fields=dict(fields),
    )


def list_close_candidates(repo: Any) -> list[LotCloseCandidate]:
    rows: list[LotCloseCandidate] = []
    for item in load_close_candidate_records(repo):
        candidate = normalize_close_candidate(item)
        if candidate is None:
            continue
        rows.append(candidate)
    rows.sort(key=lambda row: (int(row.opened_at or 0), row.record_id))
    return rows


def resolve_unique_close_lot(repo: Any, selector: LotCloseSelector) -> LotCloseMatch:
    semantic_candidates = _semantic_candidates(repo, selector)
    exact_candidates = _exact_candidates(semantic_candidates, selector)
    eligible_candidates = [
        row for row in exact_candidates
        if int(row.contracts_open or 0) >= int(selector.contracts_to_close)
    ]
    if not exact_candidates:
        raise LotCloseResolutionError(
            "not_found",
            "no open lot matches the close selector",
            selector=selector,
            candidates=semantic_candidates,
        )
    if not eligible_candidates:
        raise LotCloseResolutionError(
            "insufficient_contracts",
            "matching lots do not have enough open contracts",
            selector=selector,
            candidates=exact_candidates,
        )
    if len(eligible_candidates) > 1:
        raise LotCloseResolutionError(
            "multiple_matches",
            "multiple open lots match the close selector",
            selector=selector,
            candidates=eligible_candidates,
        )
    candidate = eligible_candidates[0]
    return LotCloseMatch(
        record_id=candidate.record_id,
        contracts_to_close=int(selector.contracts_to_close),
        matched_by="strict_contract_unique",
        candidate=candidate,
    )


def resolve_unique_close_target(
    repo: Any,
    selector: LotCloseSelector,
    *,
    source: str,
) -> CloseTargetResolution:
    match = resolve_unique_close_lot(repo, selector)
    return CloseTargetResolution(
        source=str(source or "unknown"),
        strategy=match.matched_by,
        selector=selector.to_dict(),
        matches=(match,),
    )


def resolve_fifo_close_lots(repo: Any, selector: LotCloseSelector) -> list[LotCloseMatch]:
    remaining = int(selector.contracts_to_close or 0)
    if remaining <= 0:
        raise LotCloseResolutionError(
            "invalid_quantity",
            "contracts must be > 0 for close matching",
            selector=selector,
        )
    matches: list[LotCloseMatch] = []
    for item in _exact_candidates(_semantic_candidates(repo, selector), selector):
        open_qty = int(item.contracts_open or 0)
        if open_qty <= 0:
            continue
        take = min(open_qty, remaining)
        if take <= 0:
            continue
        matches.append(
            LotCloseMatch(
                record_id=item.record_id,
                contracts_to_close=int(take),
                matched_by="strict_exact_fifo",
                candidate=item,
            )
        )
        remaining -= int(take)
        if remaining <= 0:
            break
    if remaining > 0:
        raise LotCloseResolutionError(
            "insufficient_contracts",
            f"close_match_insufficient_contracts: remaining={remaining}",
            selector=selector,
            remaining_contracts=remaining,
        )
    if not matches:
        raise LotCloseResolutionError(
            "not_found",
            "close_match_not_found",
            selector=selector,
        )
    return matches


def resolve_fifo_close_targets(
    repo: Any,
    selector: LotCloseSelector,
    *,
    source: str,
) -> CloseTargetResolution:
    matches = resolve_fifo_close_lots(repo, selector)
    return CloseTargetResolution(
        source=str(source or "unknown"),
        strategy="strict_exact_fifo",
        selector=selector.to_dict(),
        matches=tuple(matches),
    )


def resolve_explicit_close_target(
    repo: Any,
    *,
    record_id: str,
    contracts_to_close: int,
    source: str,
    fields: dict[str, Any] | None = None,
) -> CloseTargetResolution:
    resolved_record_id = str(record_id or "").strip()
    selector = _explicit_selector(
        record_id=resolved_record_id,
        contracts_to_close=contracts_to_close,
        fields=fields,
    )
    if not resolved_record_id:
        raise LotCloseResolutionError(
            "record_id_required",
            "explicit close target resolution requires record_id",
            selector=selector,
        )
    if int(contracts_to_close) <= 0:
        raise LotCloseResolutionError(
            "invalid_quantity",
            "contracts must be > 0 for close target resolution",
            selector=selector,
        )

    candidate = _current_candidate_by_record_id(repo, resolved_record_id)
    if candidate is None:
        raise LotCloseResolutionError(
            "not_found",
            "explicit close target record_id is not a current position lot",
            selector=selector,
        )
    selector = _selector_from_candidate(candidate, contracts_to_close=contracts_to_close)
    if fields is not None:
        expected = normalize_close_candidate({"record_id": resolved_record_id, "fields": fields})
        if expected is None or _candidate_identity_tuple(candidate) != _candidate_identity_tuple(expected):
            raise LotCloseResolutionError(
                "target_identity_mismatch",
                "target identity differs: explicit close target fields do not match current lot identity",
                selector=selector,
                candidates=[candidate],
            )
    if normalize_status(candidate.status) != "open" or int(candidate.contracts_open or 0) <= 0:
        raise LotCloseResolutionError(
            "target_lot_not_open",
            "explicit close target lot is not open",
            selector=selector,
            candidates=[candidate],
        )
    if int(contracts_to_close) > int(candidate.contracts_open or 0):
        raise LotCloseResolutionError(
            "insufficient_contracts",
            "explicit close target does not have enough open contracts",
            selector=selector,
            candidates=[candidate],
            remaining_contracts=int(contracts_to_close) - int(candidate.contracts_open or 0),
        )
    match = LotCloseMatch(
        record_id=candidate.record_id,
        contracts_to_close=int(contracts_to_close),
        matched_by="explicit_record_id_current_lot",
        candidate=candidate,
    )
    return CloseTargetResolution(
        source=str(source or "unknown"),
        strategy=match.matched_by,
        selector=selector.to_dict(),
        matches=(match,),
    )


def _semantic_candidates(repo: Any, selector: LotCloseSelector) -> list[LotCloseCandidate]:
    return [
        row for row in list_close_candidates(repo)
        if row.broker == selector.broker
        and row.account == selector.account
        and row.symbol == selector.symbol
        and row.option_type == selector.option_type
        and row.side == selector.side
        and normalize_status(row.status) == "open"
        and int(row.contracts_open or 0) > 0
    ]


def _exact_candidates(candidates: list[LotCloseCandidate], selector: LotCloseSelector) -> list[LotCloseCandidate]:
    return [
        row for row in candidates
        if _same_optional_float(row.strike, selector.strike)
        and row.expiration_ymd == selector.expiration_ymd
    ]


def _current_candidate_by_record_id(repo: Any, record_id: str) -> LotCloseCandidate | None:
    for item in load_close_candidate_records(repo):
        current_record_id = str(item.get("record_id") or item.get("id") or "").strip()
        if current_record_id != record_id:
            continue
        return normalize_close_candidate(item)

    get_record_fields = getattr(repo, "get_record_fields", None)
    if not callable(get_record_fields):
        return None
    try:
        fields = get_record_fields(record_id)
    except Exception:
        return None
    if not isinstance(fields, dict):
        return None
    return normalize_close_candidate({"record_id": record_id, "fields": fields})


def _selector_from_candidate(candidate: LotCloseCandidate, *, contracts_to_close: int) -> LotCloseSelector:
    return LotCloseSelector(
        broker=candidate.broker,
        account=candidate.account,
        symbol=candidate.symbol,
        option_type=candidate.option_type,
        side=candidate.side,
        strike=candidate.strike,
        expiration_ymd=candidate.expiration_ymd,
        contracts_to_close=int(contracts_to_close),
    )


def _explicit_selector(
    *,
    record_id: str,
    contracts_to_close: int,
    fields: dict[str, Any] | None,
) -> LotCloseSelector:
    if isinstance(fields, dict):
        candidate = normalize_close_candidate({"record_id": record_id, "fields": fields})
        if candidate is not None:
            return _selector_from_candidate(candidate, contracts_to_close=contracts_to_close)
    return LotCloseSelector(
        broker="",
        account="",
        symbol="",
        option_type="",
        side="",
        strike=None,
        expiration_ymd=None,
        contracts_to_close=int(contracts_to_close or 0),
    )


def _candidate_identity_tuple(candidate: LotCloseCandidate) -> tuple[Any, ...]:
    return (
        candidate.broker,
        candidate.account,
        candidate.symbol,
        candidate.option_type,
        candidate.side,
        candidate.strike,
        candidate.expiration_ymd,
        normalize_currency(candidate.currency),
    )


def _canonical_selector_symbol(value: Any) -> str:
    return canonical_symbol(value) or str(value or "").strip().upper()


def _normalize_selector_expiration(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    exp_ms = parse_exp_to_ms(raw)
    if exp_ms is None:
        return raw
    return exp_ms_to_ymd(exp_ms)


def _same_optional_float(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return abs(float(left) - float(right)) < 1e-9
    except (TypeError, ValueError):
        return False
