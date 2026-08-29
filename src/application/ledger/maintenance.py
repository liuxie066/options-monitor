from __future__ import annotations

import importlib
import uuid
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import (
    EXPIRE_AUTO_CLOSE,
    build_expire_auto_close_patch_contract,
    effective_contracts_open,
    effective_expiration,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    exp_ms_to_datetime,
    exp_ms_to_ymd,
    normalize_account,
    normalize_broker,
    normalize_close_type,
    normalize_status,
    now_ms,
    parse_exp_to_ms,
    safe_float,
)
from domain.domain.option_position_identity import normalize_currency
from domain.domain.symbol_identity import symbol_market
from domain.domain.trade_contract_identity import canonical_contract_symbol
from src.application.ledger.errors import LedgerPreflightError
from src.application.ledger.lifecycle import persist_lifecycle_expire_close_events_atomically
from src.application.ledger.lot_resolver import (
    LotCloseResolutionError,
    normalize_close_candidate,
    resolve_explicit_close_target,
    same_close_candidate_identity,
)
from src.application.ledger.repository import (
    require_option_positions_event_write_repo,
    require_option_positions_read_repo,
)
from src.application.ledger.results import (
    ExpiredCloseApplyResult,
    ExpiredCloseDecision,
    ExpiredCloseRunResult,
    LedgerWriteResult,
    ProjectionRefreshResult,
)
from src.application.ledger.targets import assert_position_lot_target_matches_current_state
from src.application.ledger.writer import (
    persist_trade_event_object,
    rebuild_position_lots_from_trade_events,
    safe_int_count,
)


def _canonical_trade_symbol(value: Any) -> str:
    return canonical_contract_symbol(value)


def _close_event_trade_time_ms(repo: Any, *, target_source_event_id: str, as_of_ms: int | None) -> int:
    ts = int(as_of_ms or now_ms())
    if not target_source_event_id:
        return ts
    list_trade_events = getattr(repo, "list_trade_events", None)
    if not callable(list_trade_events):
        return ts
    try:
        raw_events = list_trade_events()
        events = raw_events if isinstance(raw_events, list) else []
        for item in events:
            if not isinstance(item, dict):
                continue
            if str(item.get("event_id") or "").strip() != target_source_event_id:
                continue
            source_ts = int(item.get("trade_time_ms") or 0)
            if source_ts >= ts:
                return source_ts + 1
            return ts
    except Exception:
        return ts
    return ts


def persist_expire_auto_close_event(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any],
    contracts_to_close: int,
    close_reason: str,
    as_of_ms: int | None = None,
    exp_source: str | None = None,
    grace_days: int | None = None,
    close_target_resolution: dict[str, Any] | None = None,
) -> LedgerWriteResult:
    broker = normalize_broker(fields.get("broker"))
    if not broker:
        raise ValueError(f"position lot missing broker: {record_id}")
    fields = assert_position_lot_target_matches_current_state(
        repo,
        record_id=record_id,
        fields=fields,
        operation="expire_auto_close",
    )
    multiplier = effective_multiplier(fields)
    strike = effective_strike(fields)
    target_source_event_id = str(fields.get("source_event_id") or "").strip()
    trade_time_ms = _close_event_trade_time_ms(
        repo,
        target_source_event_id=target_source_event_id,
        as_of_ms=as_of_ms,
    )
    event = TradeEvent(
        event_id=f"auto-close-{record_id}-{uuid.uuid4().hex}",
        event_type="expire_close",
        event_time_ms=trade_time_ms,
        contract_key=ContractKey.from_values(
            broker=broker,
            account=normalize_account(fields.get("account")),
            underlying_symbol=_canonical_trade_symbol(fields.get("symbol")),
            option_type=str(fields.get("option_type") or ""),
            position_side=str(fields.get("side") or "").strip().lower(),
            strike=(float(strike) if strike is not None else None),
            expiration_ymd=effective_expiration_ymd(fields),
        ),
        contracts=int(contracts_to_close),
        price=0.0,
        currency=normalize_currency(fields.get("currency")),
        source="auto_close_expired_positions",
        multiplier=(float(multiplier) if multiplier is not None else 100.0),
        target_lot_id=str(record_id),
        raw_payload={
            "source": "om option-positions",
            "source_type": "system_trade_event",
            "mode": EXPIRE_AUTO_CLOSE,
            "record_id": str(record_id),
            "target_lot_id": str(record_id),
            "close_target_source_event_id": target_source_event_id,
            "close_target_account": normalize_account(fields.get("account")),
            "close_target_broker": broker,
            "close_type": EXPIRE_AUTO_CLOSE,
            "close_reason": str(close_reason or "expired"),
            "auto_close_exp_src": str(exp_source or ""),
            "auto_close_grace_days": int(grace_days) if grace_days is not None else None,
            "close_target_resolution": close_target_resolution,
        },
    )
    return persist_trade_event_object(repo, event)


def _auto_close_expiration_anchor(fields: dict[str, Any]) -> tuple[int | None, str, str | None, int | None]:
    exp_ms, exp_source = effective_expiration(fields)
    if exp_ms is None:
        return None, "none", None, None
    exp_ymd = exp_ms_to_ymd(exp_ms)
    normalized_ms = parse_exp_to_ms(exp_ymd) if exp_ymd else None
    if normalized_ms is None:
        normalized_ms = int(exp_ms)
    return int(normalized_ms), exp_source, exp_ymd, int(exp_ms)


def _auto_close_market_timezone(fields: dict[str, Any]) -> tuple[str | None, ZoneInfo | timezone]:
    market = symbol_market(fields.get("symbol"))
    if market == "US":
        return market, ZoneInfo("America/New_York")
    if market == "HK":
        return market, ZoneInfo("Asia/Hong_Kong")
    return market, timezone.utc


def _auto_close_eligible_after_ms(
    fields: dict[str, Any],
    *,
    exp_ms: int,
    exp_ymd: str | None,
    grace_days: int,
) -> tuple[int, str | None, str]:
    market, tz = _auto_close_market_timezone(fields)
    if not exp_ymd:
        return int(exp_ms) + int(grace_days) * 86400 * 1000, market, "UTC"
    try:
        exp_date = datetime.strptime(exp_ymd, "%Y-%m-%d").date()
    except ValueError:
        return int(exp_ms) + int(grace_days) * 86400 * 1000, market, "UTC"
    eligible_local = datetime.combine(exp_date + timedelta(days=int(grace_days)), time.min, tzinfo=tz)
    return int(eligible_local.astimezone(timezone.utc).timestamp() * 1000), market, str(getattr(tz, "key", "UTC"))


_AUTO_CLOSE_VOLATILE_EVIDENCE_KEYS = (
    "_auto_close_skip_reason",
    "_auto_close_skip_message",
    "_auto_close_underlying_spot",
    "_auto_close_underlier_code",
    "_auto_close_quote_source",
    "_auto_close_quote_status",
    "_auto_close_quote_time_ms",
    "spot",
    "underlying_spot",
    "underlying_price",
    "stock_price",
)


def _normalize_assignment_option_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"put", "p"}:
        return "put"
    if text in {"call", "c"}:
        return "call"
    return text


def _auto_close_underlying_spot(fields: dict[str, Any]) -> float | None:
    for key in ("_auto_close_underlying_spot", "spot", "underlying_spot", "underlying_price", "stock_price"):
        value = safe_float(fields.get(key))
        if value is not None and value > 0:
            return float(value)
    return None


def _assignment_review_details(fields: dict[str, Any]) -> dict[str, Any] | None:
    option_type = _normalize_assignment_option_type(fields.get("option_type"))
    position_side = str(fields.get("side") or fields.get("position_side") or "").strip().lower()
    if position_side != "short" or option_type not in {"put", "call"}:
        return None

    strike = effective_strike(fields)
    spot = _auto_close_underlying_spot(fields)
    details: dict[str, Any] = {
        "position_side": position_side,
        "option_type": option_type,
        "strike": strike,
        "spot": spot,
        "quote_source": str(fields.get("_auto_close_quote_source") or "").strip() or None,
        "quote_status": str(fields.get("_auto_close_quote_status") or "").strip() or None,
        "quote_time_ms": safe_float(fields.get("_auto_close_quote_time_ms")),
        "underlier_code": str(fields.get("_auto_close_underlier_code") or "").strip() or None,
    }
    if strike is None:
        details.update(
            {
                "status": "missing_strike",
                "moneyness": "unknown",
                "block_auto_close": True,
                "skip_reason": "expiry_assignment_review_required",
                "reason": "short option expiry auto-close requires strike; assignment review required",
            }
        )
        return details
    if spot is None:
        details.update(
            {
                "status": "missing_spot",
                "moneyness": "unknown",
                "block_auto_close": True,
                "skip_reason": "expiry_assignment_review_required",
                "reason": "short option expiry auto-close requires underlying spot; assignment review required",
            }
        )
        return details

    if option_type == "put":
        otm_verified = float(spot) > float(strike)
        intrinsic_value = max(0.0, float(strike) - float(spot))
    else:
        otm_verified = float(spot) < float(strike)
        intrinsic_value = max(0.0, float(spot) - float(strike))
    details["intrinsic_value"] = intrinsic_value
    if otm_verified:
        details.update(
            {
                "status": "otm_verified",
                "moneyness": "otm",
                "block_auto_close": False,
            }
        )
        return details

    details.update(
        {
            "status": "itm_or_atm",
            "moneyness": "itm_or_atm",
            "block_auto_close": True,
            "skip_reason": "expiry_assignment_review_required",
            "reason": (
                f"short {option_type} expired in/at the money; wait for assignment outcome: "
                f"spot={float(spot):g} strike={float(strike):g}"
            ),
        }
    )
    return details


def _merge_auto_close_volatile_evidence(current: dict[str, Any], original: dict[str, Any]) -> dict[str, Any]:
    out = dict(current)
    for key in _AUTO_CLOSE_VOLATILE_EVIDENCE_KEYS:
        value = original.get(key)
        if value not in (None, ""):
            out[key] = value
    return out


def _raise_if_legacy_position_lots_without_trade_events(repo: Any) -> None:
    candidate = require_option_positions_event_write_repo(repo)
    count_trade_events = getattr(candidate, "count_trade_events", None)
    count_position_lots = getattr(candidate, "count_position_lots", None)
    if not callable(count_trade_events) or not callable(count_position_lots):
        return
    if safe_int_count(count_trade_events()) > 0 or safe_int_count(count_position_lots()) <= 0:
        return
    raise ValueError(
        "position_lots exist without trade_events; rebuild from canonical trade_events "
        "or repair the active ledger before auto-close"
    )


def build_expired_close_decisions(
    positions: list[dict[str, Any]],
    *,
    as_of_ms: int,
    grace_days: int,
) -> list[ExpiredCloseDecision]:
    decisions: list[ExpiredCloseDecision] = []
    as_of_dt = exp_ms_to_datetime(as_of_ms)
    if as_of_dt is None:
        raise ValueError("invalid as_of_ms")

    for item in positions:
        fields = dict(item)
        record_id = str(fields.get("record_id") or "").strip()
        position_id = str(fields.get("position_id") or "").strip() or "(no position_id)"
        if not record_id:
            decisions.append(
                ExpiredCloseDecision(
                    record_id="",
                    position_id=position_id,
                    expiration_ms=None,
                    effective_exp_source="none",
                    should_close=False,
                    reason="missing record_id",
                    patch=None,
                )
            )
            continue

        if str(fields.get("_auto_close_skip_reason") or "") == "not_current_position_lot":
            decisions.append(
                ExpiredCloseDecision(
                    record_id=record_id,
                    position_id=position_id,
                    expiration_ms=None,
                    effective_exp_source="none",
                    should_close=False,
                    reason="record_id not found in current position_lots",
                    skip_reason="not_current_position_lot",
                    contracts_open=0,
                    patch=None,
                )
            )
            continue

        fresh_skip_reason = str(fields.get("_auto_close_skip_reason") or "")
        if fresh_skip_reason in {
            "position_lot_identity_changed",
            "position_lot_refresh_unavailable",
        }:
            decisions.append(
                ExpiredCloseDecision(
                    record_id=record_id,
                    position_id=position_id,
                    expiration_ms=None,
                    effective_exp_source="none",
                    should_close=False,
                    reason=str(
                        fields.get("_auto_close_skip_message")
                        or fresh_skip_reason
                    ),
                    skip_reason=fresh_skip_reason,
                    contracts_open=effective_contracts_open(fields),
                    patch=None,
                )
            )
            continue

        exp_ms, exp_source, exp_ymd, raw_exp_ms = _auto_close_expiration_anchor(fields)
        contracts_open = effective_contracts_open(fields)
        if normalize_status(fields.get("status")) == "close" or contracts_open <= 0:
            decisions.append(
                ExpiredCloseDecision(
                    record_id=record_id,
                    position_id=position_id,
                    expiration_ms=int(exp_ms) if exp_ms is not None else None,
                    raw_expiration_ms=raw_exp_ms,
                    expiration_ymd=exp_ymd,
                    effective_exp_source=exp_source if exp_ms is not None else "none",
                    should_close=False,
                    reason="already closed or no open contracts",
                    skip_reason="already_closed_or_zero_open",
                    contracts_open=contracts_open,
                    patch=None,
                )
            )
            continue
        if exp_ms is None:
            decisions.append(
                ExpiredCloseDecision(
                    record_id=record_id,
                    position_id=position_id,
                    expiration_ms=None,
                    effective_exp_source="none",
                    should_close=False,
                    reason="missing expiration (field and note)",
                    patch=None,
                )
            )
            continue

        eligible_after_ms, expiration_market, expiration_timezone = _auto_close_eligible_after_ms(
            fields,
            exp_ms=int(exp_ms),
            exp_ymd=exp_ymd,
            grace_days=grace_days,
        )
        eligible_after_dt = exp_ms_to_datetime(eligible_after_ms)
        should_close = int(as_of_ms) >= int(eligible_after_ms)
        manual_skip_reason = str(fields.get("_auto_close_skip_reason") or "").strip()
        if should_close and manual_skip_reason:
            decisions.append(
                ExpiredCloseDecision(
                    record_id=record_id,
                    position_id=position_id,
                    expiration_ms=int(exp_ms),
                    raw_expiration_ms=raw_exp_ms,
                    expiration_ymd=exp_ymd,
                    effective_exp_source=exp_source,
                    should_close=False,
                    reason=str(fields.get("_auto_close_skip_message") or manual_skip_reason),
                    skip_reason=manual_skip_reason,
                    contracts_open=contracts_open,
                    patch=None,
                )
            )
            continue
        if not should_close:
            expired_but_waiting = int(exp_ms) <= int(as_of_ms)
            skip_reason = "grace_period_pending" if expired_but_waiting else "not_expired"
            reason_prefix = "expired but waiting grace cutoff" if expired_but_waiting else "not expired"
            decisions.append(
                ExpiredCloseDecision(
                    record_id=record_id,
                    position_id=position_id,
                    expiration_ms=int(exp_ms),
                    raw_expiration_ms=raw_exp_ms,
                    expiration_ymd=exp_ymd,
                    effective_exp_source=exp_source,
                    should_close=False,
                    reason=(
                        f"{reason_prefix}: exp={exp_ms_to_ymd(exp_ms) or exp_ms} "
                        f"eligible_after_utc={eligible_after_dt.isoformat() if eligible_after_dt else eligible_after_ms} "
                        f"grace_days={grace_days} as_of_utc={as_of_dt.isoformat()}"
                    ),
                    skip_reason=skip_reason,
                    contracts_open=contracts_open,
                    patch=None,
                    details={
                        "eligible_after_ms": eligible_after_ms,
                        "eligible_after_utc": eligible_after_dt.isoformat() if eligible_after_dt else None,
                        "expiration_market": expiration_market,
                        "expiration_timezone": expiration_timezone,
                    },
                )
            )
            continue

        assignment_review = _assignment_review_details(fields)
        if assignment_review and bool(assignment_review.get("block_auto_close")):
            decisions.append(
                ExpiredCloseDecision(
                    record_id=record_id,
                    position_id=position_id,
                    expiration_ms=int(exp_ms),
                    raw_expiration_ms=raw_exp_ms,
                    expiration_ymd=exp_ymd,
                    effective_exp_source=exp_source,
                    should_close=False,
                    reason=str(assignment_review.get("reason") or "assignment review required"),
                    skip_reason=str(assignment_review.get("skip_reason") or "expiry_assignment_review_required"),
                    contracts_open=contracts_open,
                    patch=None,
                    details={
                        "eligible_after_ms": eligible_after_ms,
                        "eligible_after_utc": eligible_after_dt.isoformat() if eligible_after_dt else None,
                        "expiration_market": expiration_market,
                        "expiration_timezone": expiration_timezone,
                        "assignment_review": assignment_review,
                    },
                )
            )
            continue
        patch_contract = (
            build_expire_auto_close_patch_contract(
                fields,
                as_of_ms=as_of_ms,
                close_reason="expired",
                exp_source=exp_source,
                grace_days=grace_days,
            )
            if should_close
            else None
        )
        decisions.append(
            ExpiredCloseDecision(
                record_id=record_id,
                position_id=position_id,
                expiration_ms=int(exp_ms),
                raw_expiration_ms=raw_exp_ms,
                expiration_ymd=exp_ymd,
                effective_exp_source=exp_source,
                should_close=should_close,
                reason=(
                    f"expired: exp={exp_ms_to_ymd(exp_ms) or exp_ms} "
                    f"grace_days={grace_days} as_of_utc={as_of_dt.isoformat()}"
                ),
                contracts_open=contracts_open,
                patch=patch_contract,
                details={
                    "eligible_after_ms": eligible_after_ms,
                    "eligible_after_utc": eligible_after_dt.isoformat() if eligible_after_dt else None,
                    "expiration_market": expiration_market,
                    "expiration_timezone": expiration_timezone,
                    **({"assignment_review": assignment_review} if assignment_review else {}),
                },
            )
        )
    return decisions


def _refresh_position_lot_projection_from_trade_events(repo: Any) -> ProjectionRefreshResult | None:
    candidate = getattr(repo, "primary_repo", repo)
    count_trade_events = getattr(candidate, "count_trade_events", None)
    if not callable(count_trade_events):
        return None
    if safe_int_count(count_trade_events()) <= 0:
        return None
    return rebuild_position_lots_from_trade_events(candidate)


def _fresh_auto_close_positions(repo: Any, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current_lot_source_available = False
    try:
        current_lots = require_option_positions_read_repo(repo).list_position_lots()
        current_lot_source_available = True
    except Exception:
        current_lots = []
    current_by_record_id: dict[str, dict[str, Any]] = {}
    if isinstance(current_lots, list):
        for lot in current_lots:
            if not isinstance(lot, dict):
                continue
            fields = lot.get("fields") if isinstance(lot.get("fields"), dict) else lot
            if not isinstance(fields, dict):
                continue
            record_id = str(lot.get("record_id") or fields.get("record_id") or "").strip()
            if not record_id:
                continue
            row = dict(fields)
            row["record_id"] = record_id
            current_by_record_id[record_id] = row

    get_record_fields = getattr(repo, "get_record_fields", None)

    out: list[dict[str, Any]] = []
    for item in positions:
        if not isinstance(item, dict):
            continue
        original = dict(item)
        record_id = str(original.get("record_id") or "").strip()
        if not record_id:
            out.append(original)
            continue
        if current_lot_source_available:
            current_lot = current_by_record_id.get(record_id)
            if current_lot is None:
                stale = dict(original)
                stale["_auto_close_skip_reason"] = "not_current_position_lot"
                out.append(stale)
                continue
            if current_lot.get("position_id") in (None, "") and original.get("position_id") not in (None, ""):
                current_lot = dict(current_lot)
                current_lot["position_id"] = original.get("position_id")
            out.append(
                _fresh_auto_close_position(
                    record_id=record_id,
                    current=current_lot,
                    selected=original,
                )
            )
            continue
        if not callable(get_record_fields):
            out.append(_unavailable_auto_close_position(original))
            continue
        try:
            raw_current = get_record_fields(record_id)
        except Exception:
            out.append(_unavailable_auto_close_position(original))
            continue
        if not isinstance(raw_current, dict):
            out.append(_unavailable_auto_close_position(original))
            continue
        current = dict(raw_current)
        current["record_id"] = record_id
        if current.get("position_id") in (None, "") and original.get("position_id") not in (None, ""):
            current["position_id"] = original.get("position_id")
        out.append(
            _fresh_auto_close_position(
                record_id=record_id,
                current=current,
                selected=original,
            )
        )
    return out


def _unavailable_auto_close_position(selected: dict[str, Any]) -> dict[str, Any]:
    stale = {
        key: value
        for key, value in selected.items()
        if key not in _AUTO_CLOSE_VOLATILE_EVIDENCE_KEYS
    }
    stale["_auto_close_skip_reason"] = "position_lot_refresh_unavailable"
    stale["_auto_close_skip_message"] = (
        "position lot refresh unavailable after auto-close selection; "
        "defer to the next scoped run"
    )
    return stale


def _fresh_auto_close_position(
    *,
    record_id: str,
    current: dict[str, Any],
    selected: dict[str, Any],
) -> dict[str, Any]:
    if not _same_auto_close_position_identity(
        record_id=record_id,
        current=current,
        selected=selected,
    ):
        stale = dict(current)
        stale["record_id"] = record_id
        stale["_auto_close_skip_reason"] = "position_lot_identity_changed"
        stale["_auto_close_skip_message"] = (
            "position lot identity changed after auto-close selection; "
            "defer to the next scoped run"
        )
        return stale
    return _merge_auto_close_volatile_evidence(current, selected)


def _same_auto_close_position_identity(
    *,
    record_id: str,
    current: dict[str, Any],
    selected: dict[str, Any],
) -> bool:
    selected_candidate = normalize_close_candidate(
        {"record_id": record_id, "fields": selected}
    )
    current_candidate = normalize_close_candidate(
        {"record_id": record_id, "fields": current}
    )
    return not (
        selected_candidate is None
        or current_candidate is None
        or not same_close_candidate_identity(selected_candidate, current_candidate)
    )


def _mark_auto_close_decision_skipped_already_closed(
    decision: ExpiredCloseDecision,
    fields: dict[str, Any],
) -> ExpiredCloseDecision:
    return decision.with_skip(
        reason="already closed or no open contracts",
        skip_reason="already_closed_or_zero_open",
        contracts_open=effective_contracts_open(fields),
    )


def _same_float(left: Any, right: Any, *, tolerance: float = 1e-9) -> bool:
    try:
        return abs(float(left) - float(right)) <= tolerance
    except Exception:
        return False


def _lifecycle_case_broker(case: dict[str, Any]) -> str:
    broker = normalize_broker(case.get("broker"))
    if broker:
        return broker
    case_key = str(case.get("case_key") or "").strip()
    return normalize_broker(case_key.split("|", 1)[0]) if "|" in case_key else ""


def _lifecycle_evidence_broker(evidence: dict[str, Any]) -> str:
    for payload in (evidence, evidence.get("raw_payload"), evidence.get("raw_json")):
        if not isinstance(payload, dict):
            continue
        broker = normalize_broker(payload.get("broker"))
        if broker:
            return broker
        raw = payload.get("raw")
        if isinstance(raw, dict):
            broker = normalize_broker(raw.get("broker"))
            if broker:
                return broker
    return ""


def _same_lifecycle_contract(fields: dict[str, Any], case: dict[str, Any]) -> bool:
    if normalize_account(fields.get("account")) != normalize_account(case.get("account")):
        return False
    fields_broker = normalize_broker(fields.get("broker"))
    case_broker = _lifecycle_case_broker(case)
    if not fields_broker or not case_broker or fields_broker != case_broker:
        return False
    if _canonical_trade_symbol(fields.get("symbol")) != _canonical_trade_symbol(case.get("symbol")):
        return False
    if str(fields.get("option_type") or "").strip().lower() != str(case.get("option_type") or "").strip().lower():
        return False
    if str(fields.get("side") or "").strip().lower() != str(case.get("position_side") or "").strip().lower():
        return False
    if str(effective_expiration_ymd(fields) or "") != str(case.get("expiration_ymd") or ""):
        return False
    return _same_float(effective_strike(fields), case.get("strike"))


def _stock_evidence_matches_lifecycle_lot(
    fields: dict[str, Any],
    evidence: dict[str, Any],
    *,
    contracts_to_close: int,
) -> bool:
    if str(evidence.get("evidence_type") or "") != "stock_settlement_leg":
        return False
    if normalize_account(fields.get("account")) != normalize_account(evidence.get("account")):
        return False
    fields_broker = normalize_broker(fields.get("broker"))
    evidence_broker = _lifecycle_evidence_broker(evidence)
    if not fields_broker or not evidence_broker or fields_broker != evidence_broker:
        return False
    if _canonical_trade_symbol(fields.get("symbol")) != _canonical_trade_symbol(evidence.get("symbol")):
        return False
    option_type = str(fields.get("option_type") or "").strip().lower()
    position_side = str(fields.get("side") or "").strip().lower()
    if position_side == "short":
        expected_side = "buy" if option_type == "put" else "sell" if option_type == "call" else ""
    elif position_side == "long":
        expected_side = "buy" if option_type == "call" else "sell" if option_type == "put" else ""
    else:
        expected_side = ""
    if str(evidence.get("side") or "").strip().lower() != expected_side:
        return False
    try:
        expected_qty = int(contracts_to_close) * int(effective_multiplier(fields) or 100)
        actual_qty = abs(int(evidence.get("stock_qty") or 0))
    except Exception:
        return False
    if expected_qty <= 0 or actual_qty != expected_qty:
        return False
    try:
        strike = float(effective_strike(fields))
        price = float(evidence.get("stock_price"))
    except Exception:
        return False
    tolerance = max(0.01, abs(strike) * 0.001)
    return abs(price - strike) <= tolerance


def _lifecycle_auto_close_case(repo: Any, fields: dict[str, Any]) -> dict[str, Any] | None:
    list_cases = getattr(repo, "list_trade_lifecycle_cases", None)
    if not callable(list_cases):
        return None
    try:
        for case in list_cases():
            if not isinstance(case, dict):
                continue
            status = str(case.get("status") or "").strip().lower()
            if status not in {"pending", "waiting_settlement_evidence", "needs_review"}:
                continue
            if _same_lifecycle_contract(fields, case):
                return dict(case)
    except Exception:
        return None
    return None


def _conflicting_lifecycle_case(repo: Any, fields: dict[str, Any]) -> dict[str, Any] | None:
    list_cases = getattr(repo, "list_trade_lifecycle_cases", None)
    if not callable(list_cases):
        return None
    try:
        for case in list_cases():
            if not isinstance(case, dict):
                continue
            if str(case.get("status") or "").strip().lower() != "conflict":
                continue
            if _same_lifecycle_contract(fields, case):
                return dict(case)
    except Exception:
        return None
    return None


def _matching_lifecycle_stock_evidence(
    repo: Any,
    *,
    fields: dict[str, Any],
    contracts_to_close: int,
) -> dict[str, Any] | None:
    list_evidence = getattr(repo, "list_trade_lifecycle_evidence", None)
    if callable(list_evidence):
        try:
            evidence_rows = list_evidence(
                account=normalize_account(fields.get("account")),
                symbol=_canonical_trade_symbol(fields.get("symbol")),
            )
            for evidence in evidence_rows:
                if not isinstance(evidence, dict):
                    continue
                evidence_account = normalize_account(evidence.get("account"))
                evidence_symbol = _canonical_trade_symbol(evidence.get("symbol"))
                if (
                    str(evidence.get("evidence_type") or "") == "stock_settlement_leg"
                    and evidence_account == normalize_account(fields.get("account"))
                    and evidence_symbol == _canonical_trade_symbol(fields.get("symbol"))
                    and not _lifecycle_evidence_broker(evidence)
                ):
                    return {
                        **dict(evidence),
                        "identity_status": "broker_missing",
                    }
                if _stock_evidence_matches_lifecycle_lot(
                    fields,
                    evidence,
                    contracts_to_close=int(contracts_to_close),
                ):
                    return dict(evidence)
        except Exception:
            return None
    return None


def _first_lifecycle_option_evidence(repo: Any, case_id: Any) -> dict[str, Any] | None:
    case_key = str(case_id or "").strip()
    if not case_key:
        return None
    list_evidence = getattr(repo, "list_trade_lifecycle_evidence", None)
    if not callable(list_evidence):
        return None
    try:
        for evidence in list_evidence(case_id=case_key):
            if not isinstance(evidence, dict):
                continue
            if str(evidence.get("evidence_type") or "") == "option_zero_price_close":
                return dict(evidence)
    except Exception:
        return None
    return None


def _lifecycle_auto_close_blocker(
    repo: Any,
    *,
    fields: dict[str, Any],
    contracts_to_close: int,
) -> dict[str, Any] | None:
    stock_evidence = _matching_lifecycle_stock_evidence(
        repo,
        fields=fields,
        contracts_to_close=contracts_to_close,
    )
    if stock_evidence is not None:
        return {
            "skip_reason": "lifecycle_stock_settlement_evidence_seen",
            "reason": (
                "stock settlement evidence may indicate assignment/exercise; "
                f"evidence_id={stock_evidence.get('evidence_id') or '-'}"
            ),
            "evidence_id": stock_evidence.get("evidence_id"),
        }
    conflict = _conflicting_lifecycle_case(repo, fields)
    if conflict is not None:
        status = str(conflict.get("status") or "").strip().lower()
        return {
            "skip_reason": "lifecycle_assignment_pending",
            "reason": (
                "assignment/exercise lifecycle evidence is pending; "
                f"case_id={conflict.get('case_id') or '-'} status={status}"
            ),
            "case_id": conflict.get("case_id"),
            "status": status,
        }
    case = _lifecycle_auto_close_case(repo, fields)
    if case is not None and _first_lifecycle_option_evidence(repo, case.get("case_id")) is None:
        return {
            "skip_reason": "lifecycle_assignment_pending",
            "reason": (
                "assignment/exercise lifecycle evidence is pending but option zero-price evidence is missing; "
                f"case_id={case.get('case_id') or '-'}"
            ),
            "case_id": case.get("case_id"),
            "status": case.get("status"),
            "evidence_missing": True,
        }
    return None


def _persist_lifecycle_auto_expire_close_event(
    repo: Any,
    *,
    case: dict[str, Any],
    option_evidence: dict[str, Any],
    close_target_resolution: Any,
    contracts_to_close: int,
    event_time_ms: int,
) -> LedgerWriteResult:
    writes = persist_lifecycle_expire_close_events_atomically(
        repo,
        close_target_resolution=close_target_resolution,
        contracts_to_close=int(contracts_to_close),
        event_time_ms=int(event_time_ms),
        lifecycle_case=case,
        option_evidence=option_evidence,
        close_reason="expired_unassigned",
    )
    result = writes[-1].operation.result if writes else None
    return result if isinstance(result, LedgerWriteResult) else LedgerWriteResult()


def _ledger_preflight_error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, LedgerPreflightError):
        return {
            "status": "blocked",
            "fail_closed": True,
            "code": exc.code,
            "message": str(exc),
            "details": dict(exc.details),
        }
    if isinstance(exc, LotCloseResolutionError):
        return {
            "status": "blocked",
            "fail_closed": True,
            "code": exc.code,
            "message": str(exc),
            "details": {
                "selector": exc.selector.to_dict(),
                "candidates": [candidate.to_dict() for candidate in exc.candidates],
                "remaining_contracts": exc.remaining_contracts,
            },
        }
    return {
        "status": "blocked",
        "fail_closed": True,
        "code": type(exc).__name__,
        "message": str(exc),
        "details": {},
    }


def _resolve_preflight_expire_auto_close() -> Any:
    return getattr(importlib.import_module("src.application.ledger.preflight"), "preflight_expire_auto_close")


_PROJECTION_REFRESH_NOT_PROVIDED: Any = object()


def auto_close_expired_positions(
    repo: Any,
    positions: list[dict[str, Any]],
    *,
    as_of_ms: int,
    grace_days: int,
    max_close: int,
    projection_refresh: ProjectionRefreshResult | None = _PROJECTION_REFRESH_NOT_PROVIDED,
) -> ExpiredCloseRunResult:
    preflight_expire_auto_close = _resolve_preflight_expire_auto_close()
    if projection_refresh is _PROJECTION_REFRESH_NOT_PROVIDED:
        try:
            _refresh_position_lot_projection_from_trade_events(repo)
        except Exception as exc:
            decisions = build_expired_close_decisions(positions, as_of_ms=as_of_ms, grace_days=grace_days)
            return ExpiredCloseRunResult(
                decisions=decisions,
                applied=[],
                errors=[f"projection refresh failed before auto-close: {exc}"],
            )

    fresh_positions = _fresh_auto_close_positions(repo, positions)
    decisions = build_expired_close_decisions(fresh_positions, as_of_ms=as_of_ms, grace_days=grace_days)
    to_close_indexes = [idx for idx, decision in enumerate(decisions) if decision.should_close and decision.record_id]
    applied: list[ExpiredCloseApplyResult] = []
    errors: list[str] = []
    if len(to_close_indexes) > int(max_close):
        return ExpiredCloseRunResult(
            decisions=decisions,
            applied=applied,
            errors=[f"too many to close: {len(to_close_indexes)} > max_close={max_close}; abort"],
        )
    if to_close_indexes:
        try:
            _raise_if_legacy_position_lots_without_trade_events(repo)
        except Exception as exc:
            return ExpiredCloseRunResult(
                decisions=decisions,
                applied=applied,
                errors=[f"active ledger repair required before auto-close: {exc}"],
            )
    for index in to_close_indexes:
        decision = decisions[index]
        try:
            record_id = str(decision.record_id)
            fields = repo.get_record_fields(record_id)
            contracts_to_close = effective_contracts_open(fields)
            if not _same_auto_close_position_identity(
                record_id=record_id,
                current=fields,
                selected=fresh_positions[index],
            ):
                decisions[index] = decision.with_skip(
                    reason=(
                        "position lot identity changed after auto-close selection; "
                        "defer to the next scoped run"
                    ),
                    skip_reason="position_lot_identity_changed",
                    contracts_open=contracts_to_close,
                )
                continue
            if contracts_to_close <= 0:
                decisions[index] = _mark_auto_close_decision_skipped_already_closed(decision, fields)
                continue
            lifecycle_blocker = _lifecycle_auto_close_blocker(
                repo,
                fields=fields,
                contracts_to_close=contracts_to_close,
            )
            if lifecycle_blocker:
                decisions[index] = replace(
                    decision.with_skip(
                        reason=str(lifecycle_blocker.get("reason") or "lifecycle evidence pending"),
                        skip_reason=str(lifecycle_blocker.get("skip_reason") or "lifecycle_pending"),
                        contracts_open=contracts_to_close,
                    ),
                    details={**decision.details, "lifecycle_blocker": dict(lifecycle_blocker)},
                )
                continue
            close_target_resolution = resolve_explicit_close_target(
                repo,
                record_id=record_id,
                contracts_to_close=contracts_to_close,
                source="auto_close_expired",
                fields=fields,
            )
            decision = decision.with_close_target_resolution(close_target_resolution.to_dict())
            decisions[index] = decision
            ledger_preflight = preflight_expire_auto_close(
                repo,
                record_id=record_id,
                fields=close_target_resolution.single_candidate.raw_fields,
                contracts_to_close=contracts_to_close,
                as_of_ms=as_of_ms,
                exp_source=str(decision.effective_exp_source or ""),
                grace_days=grace_days,
            )
            decision = decision.with_ledger_preflight(ledger_preflight)
            decisions[index] = decision
            lifecycle_case = _lifecycle_auto_close_case(repo, fields)
            if lifecycle_case is not None:
                option_evidence = _first_lifecycle_option_evidence(repo, lifecycle_case.get("case_id"))
                if option_evidence is None:
                    decisions[index] = replace(
                        decision.with_skip(
                            reason="lifecycle case has no option zero-price evidence",
                            skip_reason="lifecycle_option_evidence_missing",
                            contracts_open=contracts_to_close,
                        ),
                        details={**decision.details, "lifecycle_case_id": lifecycle_case.get("case_id")},
                    )
                    continue
                result = _persist_lifecycle_auto_expire_close_event(
                    repo,
                    case=lifecycle_case,
                    option_evidence=option_evidence,
                    close_target_resolution=close_target_resolution,
                    contracts_to_close=contracts_to_close,
                    event_time_ms=int(ledger_preflight.event_time_ms),
                )
            else:
                result = persist_expire_auto_close_event(
                    repo,
                    record_id=record_id,
                    fields=close_target_resolution.single_candidate.raw_fields,
                    contracts_to_close=contracts_to_close,
                    close_reason="expired",
                    as_of_ms=int(ledger_preflight.event_time_ms),
                    exp_source=str(decision.effective_exp_source or ""),
                    grace_days=grace_days,
                    close_target_resolution=close_target_resolution.to_dict(),
                )
            updated_fields = repo.get_record_fields(record_id)
            if effective_contracts_open(updated_fields) > 0 or normalize_status(updated_fields.get("status")) != "close":
                errors.append(f"{record_id} {decision.position_id}: auto-close event did not close target lot")
                continue
            if normalize_close_type(updated_fields.get("close_type")) != EXPIRE_AUTO_CLOSE:
                errors.append(f"{record_id} {decision.position_id}: auto-close projected wrong close_type")
                continue
            applied.append(ExpiredCloseApplyResult(decision=decision, result=result))
        except Exception as exc:
            if decision.ledger_preflight is None:
                decision = decision.with_ledger_preflight(_ledger_preflight_error_payload(exc))
                decisions[index] = decision
            errors.append(f"{decision.record_id} {decision.position_id}: {exc}")
    return ExpiredCloseRunResult(decisions=decisions, applied=applied, errors=errors)
