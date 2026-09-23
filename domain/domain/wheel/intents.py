"""Wheel decision plans, capacity binding and intent lifecycle planning.

Call and Put share a core per operation (``_plan_intent_create``,
``_plan_intent_cancel``, ``_plan_intent_consume``); the public entry points
below stay thin wrappers around them.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger.events import persisted_stock_settlement
from domain.domain.trade_execution import futu_order_namespace_issue
from domain.domain.trade_contract_identity import contract_share_quantity

from ._common import (
    WHEEL_EVENT_SCHEMA_V1,
    WHEEL_EVENT_SCHEMA_V2,
    _finite_float,
    _wheel_market,
)
from .events import _positive_int, _required_text, build_wheel_event
from .projection import (
    _event_type,
    _lot_fields,
    _trade_account,
    _trade_option_type,
    _trade_position_side,
    _trade_symbol,
    lot_contract_key,
    project_wheel_intents,
)


def resolve_wheel_fill_intent(branch: Mapping[str, Any], fill: Mapping[str, Any],
    wheel_events: Sequence[Mapping[str, Any]], *, now_ms: int, known_trade_event_ids: set[str]) -> dict[str, Any]:
    """Validate at fill time, consume against today's remainder, without reviving reservations."""
    event = _trade_event_fact(fill)
    instant = int(event["event_time_ms"])
    kwargs = dict(account=branch["account"], wheel_branch_id=branch["wheel_branch_id"], direction=branch["direction"],
                  known_trade_event_ids=known_trade_event_ids)
    historical = {item["intent_id"]: item for item in project_wheel_intents(wheel_events, as_of_ms=instant, **kwargs)}
    current = project_wheel_intents(wheel_events, as_of_ms=now_ms, **kwargs)
    relevant = []
    for intent in current:
        if int(intent.get("created_at_ms") or 0) > instant:
            continue
        payload = intent.get("payload") or {}
        same_contract = (payload.get("expiration_ymd") == event.get("expiration_ymd")
                         and _finite_float(payload.get("strike")) == _finite_float(event.get("strike")))
        at_fill = historical.get(intent["intent_id"], {})
        if same_contract or at_fill.get("status") == "active":
            relevant.append((intent, at_fill))
    if not relevant:
        return {"intent": None, "reserved_contracts_to_consume": 0, "reason_codes": []}
    if len(relevant) != 1:
        return {"intent": None, "reserved_contracts_to_consume": 0, "reason_codes": ["multiple_or_invalid_wheel_intents"]}
    current_intent, at_fill = relevant[0]
    candidate = {**at_fill, "remaining_contracts": min(int(current_intent.get("remaining_contracts") or 0),
                                                      int(at_fill.get("remaining_contracts") or 0))}
    payload = candidate.get("payload") or {}
    capacity = {"account": branch["account"], "symbol": branch["symbol"], "status": "available",
                "shares_available_for_cover": contract_share_quantity(event["contracts"], event["multiplier"]),
                **{key: payload.get(key) for key in ("capacity_identity_hash", "cash_reservation_currency", "cash_reservation_amount")}}
    try:
        _plan_intent_consume(branch, candidate, event, capacity, direction=branch["direction"], recorded_at_ms=now_ms)
    except (TypeError, ValueError):
        return {"intent": None, "reserved_contracts_to_consume": 0, "reason_codes": ["wheel_intent_fill_mismatch_or_consumed"]}
    return {"intent": candidate, "reserved_contracts_to_consume": int(event["contracts"]) if current_intent["status"] == "active" else 0,
            "reason_codes": []}


def plan_wheel_branch_decision(
    branch: Mapping[str, Any],
    decision: str,
    request_id: str,
    actor: str,
    expected_generation_hash: str,
    *,
    occurred_at_ms: int,
    recorded_at_ms: int,
) -> dict[str, Any]:
    decision_value = _required_text(decision, "decision").lower()
    if decision_value not in {"start", "end"}:
        raise ValueError("Wheel branch decision must be start or end")
    if branch.get("lifecycle_status") != "pending_decision":
        raise ValueError("Wheel branch is not pending a decision")
    expected = _required_text(
        expected_generation_hash,
        "expected_generation_hash",
    )
    if expected != str(branch.get("batch_generation_hash") or ""):
        raise ValueError("Wheel branch generation changed")
    account = _required_text(branch.get("account"), "account").lower()
    branch_id = _required_text(branch.get("wheel_branch_id"), "wheel_branch_id")
    request = _required_text(request_id, "request_id")
    digest = canonical_sha256(
        {
            "schema_version": "wheel_branch_decision_request.v1",
            "account": account,
            "wheel_branch_id": branch_id,
            "request_id": request,
        }
    )[:24]
    return build_wheel_event(
        event_id=f"wheel-branch-decided:{digest}",
        event_schema_version=WHEEL_EVENT_SCHEMA_V2,
        account=account,
        wheel_branch_id=branch_id,
        lot_id=str(branch.get("stock_lot_id") or "").strip() or None,
        event_type="wheel_branch_decided",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        payload={
            "schema_version": "wheel_branch_decided.v1",
            "market": _wheel_market(branch),
            "decision": decision_value,
            "request_id": request,
            "actor": _required_text(actor, "actor"),
            "expected_generation_hash": expected,
        },
    )

def _trade_event_fact(event: Any) -> dict[str, Any]:
    if isinstance(event, Mapping):
        out = dict(event)
        key = event.get("contract_key")
        key = key if isinstance(key, Mapping) else {}
        for target, source in (
            ("account", "account"),
            ("symbol", "underlying_symbol"),
            ("option_type", "option_type"),
            ("position_side", "position_side"),
            ("strike", "strike"),
            ("expiration_ymd", "expiration_ymd"),
        ):
            out.setdefault(target, key.get(source))
        return out
    contract_key = getattr(event, "contract_key", None)
    key = contract_key.to_dict() if hasattr(contract_key, "to_dict") else {}
    return {
        "event_id": getattr(event, "event_id", None),
        "event_type": getattr(event, "event_type", None),
        "event_time_ms": getattr(event, "event_time_ms", None),
        "account": key.get("account"),
        "symbol": key.get("underlying_symbol"),
        "option_type": key.get("option_type"),
        # §9.2 step 3: the contract key no longer carries the position side, so
        # read it off the event (derived from the trade side) and keep the legacy
        # key as a fallback for persisted rows.
        "position_side": getattr(event, "position_side", None) or key.get("position_side"),
        "strike": key.get("strike"),
        "expiration_ymd": key.get("expiration_ymd"),
        "contracts": getattr(event, "contracts", None),
        "multiplier": getattr(event, "multiplier", None),
        "currency": getattr(event, "currency", None),
        "target_lot_id": getattr(event, "target_lot_id", None),
        "lot_id": getattr(event, "lot_id", None),
        "raw_payload": dict(getattr(event, "raw_payload", None) or {}),
    }


def _stock_settlement(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("raw_payload")
    payload = payload if isinstance(payload, Mapping) else {}
    stock = payload.get("stock_settlement")
    return persisted_stock_settlement(stock)

def wheel_started_event_from_assignment(
    terminal_event: Any,
    source_put_lot: Mapping[str, Any],
    *,
    recorded_at_ms: int,
) -> dict[str, Any] | None:
    event = _trade_event_fact(terminal_event)
    if _event_type(event) != "assignment":
        return None
    fields = _lot_fields(source_put_lot)
    contract_key = lot_contract_key(fields)
    if (
        str(contract_key.get("option_type") or "").strip().lower() != "put"
        or str(fields.get("position_side") or "").strip().lower()
        != "short"
    ):
        return None
    event_id = _required_text(event.get("event_id"), "source_trade_event_id")
    account = _required_text(
        event.get("account") or contract_key.get("account"),
        "account",
    ).lower()
    stock = _stock_settlement(event)
    if str(stock.get("side") or "").strip().lower() != "buy":
        raise ValueError("Wheel start requires buy-side Short Put assignment settlement")
    contracts = _positive_int(event.get("contracts"), "assignment contracts")
    try:
        multiplier = _positive_int(event.get("multiplier"), "assignment multiplier")
        shares = _positive_int(stock.get("shares"), "settlement shares")
        price = float(stock.get("price"))
    except (TypeError, ValueError):
        raise ValueError("Wheel start assignment settlement is incomplete") from None
    if multiplier <= 0 or shares != contract_share_quantity(contracts, multiplier) or price < 0:
        raise ValueError("Wheel start assignment settlement quantity or price is invalid")
    occurred_at_ms = _positive_int(
        stock.get("event_time_ms") or event.get("event_time_ms"),
        "assignment occurred_at_ms",
    )
    lot_id = f"assigned-stock-{event_id}"
    return build_wheel_event(
        event_id=f"wheel-started:{event_id}",
        event_schema_version=WHEEL_EVENT_SCHEMA_V1,
        account=account,
        lot_id=lot_id,
        event_type="wheel_started",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        source_trade_event_id=event_id,
        payload={
            "schema_version": "wheel_started.v1",
            "source_option_lot_id": str(event.get("target_lot_id") or "").strip(),
            "shares": shares,
            "assignment_price": price,
            "currency": str(stock.get("currency") or event.get("currency") or "").strip().upper(),
        },
    )

def wheel_called_away_event_from_call_assignment(
    terminal_event: Any,
    source_call_lot: Mapping[str, Any],
    stock_lot_before: Mapping[str, Any] | None,
    stock_lot_after: Mapping[str, Any] | None,
    *,
    recorded_at_ms: int,
) -> dict[str, Any] | None:
    event = _trade_event_fact(terminal_event)
    if _event_type(event) != "assignment":
        return None
    fields = _lot_fields(source_call_lot)
    contract_key = lot_contract_key(fields)
    strategy = str(fields.get("strategy") or "").strip().lower()
    leg_role = str(fields.get("leg_role") or "").strip().lower()
    lot_id = str(fields.get("source_stock_lot_id") or "").strip()
    if strategy != "wheel" and leg_role != "wheel_call" and not lot_id:
        return None
    if (
        strategy != "wheel"
        or leg_role != "wheel_call"
        or not lot_id
        or str(fields.get("strategy_group_id") or "").strip()
        or str(contract_key.get("option_type") or "").strip().lower() != "call"
        or str(fields.get("position_side") or "").strip().lower()
        != "short"
    ):
        raise ValueError("Wheel Call assignment has incomplete or conflicting linkage")
    stock = _stock_settlement(event)
    if str(stock.get("side") or "").strip().lower() != "sell":
        raise ValueError("Wheel Call assignment requires sell-side stock settlement")
    contracts = _positive_int(event.get("contracts"), "assignment contracts")
    try:
        multiplier = _positive_int(event.get("multiplier"), "assignment multiplier")
        shares = _positive_int(stock.get("shares"), "settlement shares")
        before = int((stock_lot_before or {}).get("shares_remaining"))
        after = int((stock_lot_after or {}).get("shares_remaining"))
    except (TypeError, ValueError):
        raise ValueError("Wheel Call assignment stock-lot evidence is incomplete") from None
    if multiplier <= 0 or shares != contract_share_quantity(contracts, multiplier):
        raise ValueError("Wheel Call assignment settlement quantity is invalid")
    if (
        str((stock_lot_before or {}).get("stock_lot_id") or "") != lot_id
        or str((stock_lot_after or {}).get("stock_lot_id") or "") != lot_id
        or before - after != shares
        or after < 0
    ):
        raise ValueError("Wheel Call assignment did not exactly reduce its stock batch")
    if after > 0:
        return None
    source_event_id = _required_text(event.get("event_id"), "source_trade_event_id")
    account = _required_text(
        event.get("account") or contract_key.get("account"),
        "account",
    ).lower()
    occurred_at_ms = _positive_int(
        stock.get("event_time_ms") or event.get("event_time_ms"),
        "assignment occurred_at_ms",
    )
    return build_wheel_event(
        event_id=f"wheel-called-away:{source_event_id}:{lot_id}",
        event_schema_version=WHEEL_EVENT_SCHEMA_V1,
        account=account,
        lot_id=lot_id,
        event_type="wheel_called_away",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        source_trade_event_id=source_event_id,
        payload={
            "schema_version": "wheel_called_away.v1",
            "source_call_lot_id": str(event.get("target_lot_id") or "").strip(),
            "shares": shares,
        },
    )

def plan_wheel_manual_end(
    wheel_batch: Mapping[str, Any],
    request_id: str,
    actor: str,
    *,
    occurred_at_ms: int,
    recorded_at_ms: int,
    account: str,
) -> dict[str, Any]:
    lot_id = _required_text(wheel_batch.get("stock_lot_id"), "stock_lot_id")
    if wheel_batch.get("lifecycle_status") != "active":
        raise ValueError("Wheel lifecycle is not active")
    if wheel_batch.get("integrity_status") != "trusted":
        raise ValueError("Wheel lifecycle integrity is not trusted")
    if wheel_batch.get("active_call_lot_ids"):
        raise ValueError("Wheel lifecycle has an active Call")
    if wheel_batch.get("active_intent_ids"):
        raise ValueError("Wheel lifecycle has an active Call intent")
    request = _required_text(request_id, "request_id")
    actor_value = _required_text(actor, "actor")
    account_value = _required_text(account, "account").lower()
    event_digest = canonical_sha256(
        {
            "account": account_value,
            "stock_lot_id": lot_id,
            "request_id": request,
        }
    )[:24]
    return build_wheel_event(
        event_id=f"wheel-manual-ended:{event_digest}",
        event_schema_version=(
            WHEEL_EVENT_SCHEMA_V1
            if wheel_batch.get("legacy_call_adapter")
            else WHEEL_EVENT_SCHEMA_V2
        ),
        account=account_value,
        lot_id=lot_id,
        event_type="wheel_manual_ended",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        payload={
            "schema_version": "wheel_manual_ended.v1",
            "market": _wheel_market(wheel_batch),
            "request_id": request,
            "actor": actor_value,
            "batch_generation_hash": str(
                wheel_batch.get("batch_generation_hash") or ""
            ),
        },
    )

def _coverage_capacity(
    coverage_fact: Mapping[str, Any],
    *,
    account: str,
    symbol: str,
    contracts: int,
    multiplier: int,
) -> None:
    if not isinstance(coverage_fact, Mapping):
        raise ValueError("Wheel Call requires coverage_fact")
    if str(coverage_fact.get("account") or "").strip().lower() != account:
        raise ValueError("Wheel Call coverage account mismatch")
    if str(coverage_fact.get("symbol") or "").strip().upper() != symbol:
        raise ValueError("Wheel Call coverage symbol mismatch")
    if not str(coverage_fact.get("capacity_identity_hash") or "").strip():
        raise ValueError("Wheel Call coverage identity is unavailable")
    if str(coverage_fact.get("status") or "").strip().lower() != "available":
        raise ValueError("Wheel Call coverage is unavailable")
    try:
        shares_available = int(coverage_fact.get("shares_available_for_cover"))
    except (TypeError, ValueError):
        raise ValueError("Wheel Call available shares are invalid") from None
    if shares_available < contract_share_quantity(contracts, multiplier):
        raise ValueError("Wheel Call coverage is insufficient")

def build_wheel_intent_capacity_binding(
    branch: Mapping[str, Any],
    final_candidate: Mapping[str, Any],
    capacity_fact: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the immutable capacity facts bound into a new Wheel intent."""

    direction = str(branch.get("direction") or "call").strip().lower()
    if direction not in {"call", "put"}:
        raise ValueError("Wheel intent direction must be call or put")
    account = _required_text(branch.get("account"), "account").lower()
    symbol = _required_text(branch.get("symbol"), "symbol").upper()
    branch_id = _required_text(
        branch.get("wheel_branch_id") or branch.get("stock_lot_id"),
        "wheel_branch_id",
    )
    generation_hash = _required_text(
        branch.get("batch_generation_hash"),
        "batch_generation_hash",
    )
    contracts = _positive_int(
        final_candidate.get("granted_contracts"),
        "granted_contracts",
    )
    multiplier = _positive_int(final_candidate.get("multiplier"), "multiplier")
    candidate_branch_id = str(
        final_candidate.get("wheel_branch_id")
        or final_candidate.get("stock_lot_id")
        or ""
    ).strip()
    if candidate_branch_id != branch_id:
        raise ValueError("Wheel candidate branch mismatch")
    if str(final_candidate.get("account") or account).strip().lower() != account:
        raise ValueError("Wheel candidate account mismatch")
    if str(final_candidate.get("symbol") or "").strip().upper() != symbol:
        raise ValueError("Wheel candidate symbol mismatch")
    candidate_generation = str(
        final_candidate.get("batch_generation_hash")
        or ""
    ).strip()
    if candidate_generation and candidate_generation != generation_hash:
        raise ValueError("Wheel candidate branch generation mismatch")
    capacity_identity_hash = _required_text(
        capacity_fact.get("capacity_identity_hash"),
        "capacity_identity_hash",
    )
    candidate_capacity_hash = str(
        final_candidate.get("capacity_identity_hash") or ""
    ).strip()
    if candidate_capacity_hash != capacity_identity_hash:
        raise ValueError("Wheel candidate capacity identity mismatch")

    if direction == "call":
        _coverage_capacity(
            capacity_fact,
            account=account,
            symbol=symbol,
            contracts=contracts,
            multiplier=multiplier,
        )
        return {
            "direction": direction,
            "wheel_branch_id": branch_id,
            "batch_generation_hash": generation_hash,
            "capacity_identity_hash": capacity_identity_hash,
            "reserved_amount": contract_share_quantity(contracts, multiplier),
            "reservation_unit": "shares",
            "currency": None,
        }

    if str(capacity_fact.get("account") or "").strip().lower() != account:
        raise ValueError("Wheel Put cash capacity account mismatch")
    if str(capacity_fact.get("allocation_status") or "").strip().lower() != "allocated":
        raise ValueError("Wheel Put cash capacity is unavailable")
    if int(capacity_fact.get("granted_contracts") or 0) < contracts:
        raise ValueError("Wheel Put cash capacity is insufficient")
    strike = _finite_float(final_candidate.get("strike"))
    if strike is None or strike <= 0:
        raise ValueError("Wheel Put candidate strike must be positive")
    currency = _required_text(
        final_candidate.get("cash_reservation_currency")
        or final_candidate.get("currency"),
        "cash_reservation_currency",
    ).upper()
    reserved_amount = round(strike * contract_share_quantity(contracts, multiplier), 6)
    fact_currency = str(
        capacity_fact.get("cash_reservation_currency")
        or capacity_fact.get("currency")
        or ""
    ).strip().upper()
    fact_amount = _finite_float(capacity_fact.get("cash_reservation_amount"))
    if fact_currency != currency or fact_amount != reserved_amount:
        raise ValueError("Wheel Put cash reservation binding mismatch")
    return {
        "direction": direction,
        "wheel_branch_id": branch_id,
        "batch_generation_hash": generation_hash,
        "capacity_identity_hash": capacity_identity_hash,
        "reserved_amount": reserved_amount,
        "reservation_unit": "cash",
        "currency": currency,
    }

def _plan_intent_create(
    source: Mapping[str, Any],
    final_candidate: Mapping[str, Any],
    capacity_fact: Mapping[str, Any],
    expires_at_ms: int,
    request_id: str,
    actor: str,
    *,
    direction: str,
    occurred_at_ms: int,
    recorded_at_ms: int,
    broker_order_id: str | None,
) -> dict[str, Any]:
    """Create a Wheel intent-created event; Call and Put differ only by these facts."""

    direction_label = "Call" if direction == "call" else "Put"
    source_label = "lifecycle" if direction == "call" else "branch"
    active_label = "batch" if direction == "call" else "branch"
    active_lot_field = "active_call_lot_ids" if direction == "call" else "active_option_lot_ids"
    identity_field = "stock_lot_id" if direction == "call" else "wheel_branch_id"
    if direction == "put" and str(source.get("direction") or "").strip().lower() != "put":
        raise ValueError("Wheel Put intent requires a Put branch")
    if source.get("lifecycle_status") != "active":
        raise ValueError(f"Wheel {source_label} is not active")
    if source.get("integrity_status") != "trusted":
        raise ValueError(f"Wheel {source_label} integrity is not trusted")
    if source.get(active_lot_field) or source.get("active_intent_ids"):
        raise ValueError(f"Wheel {active_label} already has an active {direction_label} or intent")
    if source.get("phase") != "ready":
        raise ValueError(f"Wheel {active_label} is not ready for a {direction_label} intent")
    account = _required_text(source.get("account"), "account").lower()
    symbol = _required_text(source.get("symbol"), "symbol").upper()
    intent_owner_id = _required_text(source.get(identity_field), identity_field)
    candidate_id = _required_text(
        final_candidate.get("final_candidate_id"),
        "final_candidate_id",
    )
    contracts = _positive_int(final_candidate.get("granted_contracts"), "granted_contracts")
    multiplier = _positive_int(final_candidate.get("multiplier"), "multiplier")
    binding: Mapping[str, Any] = {}
    if direction == "call":
        strike = float(final_candidate.get("strike") or 0)
        expiration_ymd = _required_text(
            final_candidate.get("expiration_ymd"),
            "expiration_ymd",
        )
        if strike <= 0:
            raise ValueError("Wheel Call candidate strike must be positive")
        if str(final_candidate.get("account") or account).strip().lower() != account:
            raise ValueError("Wheel Call candidate account mismatch")
        if str(final_candidate.get("symbol") or "").strip().upper() != symbol:
            raise ValueError("Wheel Call candidate symbol mismatch")
        if str(final_candidate.get("stock_lot_id") or "").strip() != intent_owner_id:
            raise ValueError("Wheel Call candidate stock batch mismatch")
        if int(source.get("shares_remaining") or 0) < contract_share_quantity(contracts, multiplier):
            raise ValueError("Wheel batch shares are insufficient")
    else:
        strike = _finite_float(final_candidate.get("strike"))
        if strike is None or strike <= 0:
            raise ValueError("Wheel Put candidate strike must be positive")
        expiration_ymd = _required_text(
            final_candidate.get("expiration_ymd"),
            "expiration_ymd",
        )
        binding = build_wheel_intent_capacity_binding(source, final_candidate, capacity_fact)
    now = _positive_int(occurred_at_ms, "occurred_at_ms")
    expiry = _positive_int(expires_at_ms, "expires_at_ms")
    if expiry <= now:
        raise ValueError(f"Wheel {direction_label} intent expiry must be in the future")
    if direction == "call":
        _coverage_capacity(
            capacity_fact, account=account, symbol=symbol,
            contracts=contracts, multiplier=multiplier,
        )
    request = _required_text(request_id, "request_id")
    actor_value = _required_text(actor, "actor")
    digest = canonical_sha256(
        {"account": account, identity_field: intent_owner_id, "request_id": request}
    )[:24]
    call_side = direction == "call"
    generation_key = "batch_generation_hash"
    generation_hash = (
        str(source.get("batch_generation_hash") or "")
        if call_side
        else binding["batch_generation_hash"]
    )
    capacity_identity_hash = (
        str(capacity_fact.get("capacity_identity_hash") or "").strip()
        if call_side
        else binding["capacity_identity_hash"]
    )
    intent_payload = {
        "schema_version": f"wheel_{direction}_intent_created.v1",
        "market": _wheel_market(source),
        "request_id": request,
        "actor": actor_value,
        "final_candidate_id": candidate_id,
        "snapshot_hash": str(final_candidate.get("snapshot_hash") or "").strip(),
        generation_key: generation_hash,
        "capacity_identity_hash": capacity_identity_hash,
        "symbol": symbol,
        "strike": strike,
        "expiration_ymd": expiration_ymd,
        "contracts": contracts,
        "multiplier": multiplier,
    }
    if not call_side:
        intent_payload["cash_reservation_amount"] = binding["reserved_amount"]
        intent_payload["cash_reservation_currency"] = binding["currency"]
    intent_payload["expires_at_ms"] = expiry
    intent_payload["broker_order_id"] = str(broker_order_id or "").strip() or None
    return build_wheel_event(
        event_id=f"wheel-{direction}-intent-created:{digest}",
        event_schema_version=(
            WHEEL_EVENT_SCHEMA_V1
            if call_side and source.get("legacy_call_adapter")
            else WHEEL_EVENT_SCHEMA_V2
        ),
        account=account,
        wheel_branch_id=None if call_side else intent_owner_id,
        lot_id=(
            intent_owner_id
            if call_side
            else str(source.get("stock_lot_id") or "").strip() or None
        ),
        event_type=f"wheel_{direction}_intent_created",
        occurred_at_ms=now,
        recorded_at_ms=recorded_at_ms,
        intent_id=f"wheel-{direction}-intent:{digest}",
        payload=intent_payload,
    )


def plan_wheel_call_intent_create(
    batch: Mapping[str, Any],
    final_candidate: Mapping[str, Any],
    coverage_fact: Mapping[str, Any],
    expires_at_ms: int,
    request_id: str,
    actor: str,
    *,
    occurred_at_ms: int,
    recorded_at_ms: int,
    broker_order_id: str | None = None,
) -> dict[str, Any]:
    return _plan_intent_create(
        batch, final_candidate, coverage_fact, expires_at_ms, request_id, actor,
        direction="call", occurred_at_ms=occurred_at_ms, recorded_at_ms=recorded_at_ms,
        broker_order_id=broker_order_id,
    )


def plan_wheel_put_intent_create(
    branch: Mapping[str, Any],
    final_candidate: Mapping[str, Any],
    cash_capacity_fact: Mapping[str, Any],
    expires_at_ms: int,
    request_id: str,
    actor: str,
    *,
    occurred_at_ms: int,
    recorded_at_ms: int,
    broker_order_id: str | None = None,
) -> dict[str, Any]:
    return _plan_intent_create(
        branch, final_candidate, cash_capacity_fact, expires_at_ms, request_id, actor,
        direction="put", occurred_at_ms=occurred_at_ms, recorded_at_ms=recorded_at_ms,
        broker_order_id=broker_order_id,
    )


def _validate_put_intent_reservation(
    intent_payload: Mapping[str, Any],
    cash_capacity_fact: Mapping[str, Any],
    *,
    account: str,
) -> None:
    if str(cash_capacity_fact.get("account") or "").strip().lower() != account:
        raise ValueError("Wheel Put cash capacity account mismatch")
    expected_hash = _required_text(
        intent_payload.get("capacity_identity_hash"),
        "intent capacity_identity_hash",
    )
    if str(cash_capacity_fact.get("capacity_identity_hash") or "").strip() != expected_hash:
        raise ValueError("Wheel Put cash capacity identity changed")
    expected_currency = _required_text(
        intent_payload.get("cash_reservation_currency"),
        "intent cash_reservation_currency",
    ).upper()
    fact_currency = _required_text(
        cash_capacity_fact.get("cash_reservation_currency"),
        "cash_reservation_currency",
    ).upper()
    expected_amount = _finite_float(intent_payload.get("cash_reservation_amount"))
    fact_amount = _finite_float(cash_capacity_fact.get("cash_reservation_amount"))
    if (
        expected_currency != fact_currency
        or expected_amount is None
        or fact_amount is None
        or round(expected_amount, 6) != round(fact_amount, 6)
    ):
        raise ValueError("Wheel Put cash reservation changed")

def _plan_intent_cancel(
    source: Mapping[str, Any],
    intent: Mapping[str, Any],
    cash_capacity_fact: Mapping[str, Any] | None,
    request_id: str,
    actor: str,
    broker_order_inactive_confirmed: bool,
    reason: str,
    *,
    direction: str,
    occurred_at_ms: int,
    recorded_at_ms: int,
) -> dict[str, Any] | None:
    """Cancel a Wheel intent; Call and Put differ only by these facts."""

    source_label = "batch" if direction == "call" else "branch"
    identity_field = "stock_lot_id" if direction == "call" else "wheel_branch_id"
    if source.get("lifecycle_status") != "active" or source.get("integrity_status") != "trusted":
        raise ValueError(f"Wheel {source_label} is not an active trusted lifecycle")
    if direction == "put" and str(source.get("direction") or "").strip().lower() != "put":
        raise ValueError("Wheel Put intent requires a Put branch")
    if not broker_order_inactive_confirmed:
        raise ValueError("broker_order_inactive_confirmed=true is required")
    if str(intent.get("status") or "") != "active":
        return None
    intent_payload: Mapping[str, Any] = intent
    if direction == "call":
        intent_id = _required_text(intent.get("intent_id"), "intent_id")
        request = _required_text(request_id, "request_id")
        account = _required_text(source.get("account"), "account").lower()
        intent_owner_id = _required_text(source.get("stock_lot_id"), "stock_lot_id")
    else:
        payload = intent.get("payload")
        intent_payload = payload if isinstance(payload, Mapping) else intent
        account = _required_text(source.get("account"), "account").lower()
        _validate_put_intent_reservation(intent_payload, cash_capacity_fact, account=account)
        intent_owner_id = _required_text(source.get("wheel_branch_id"), "wheel_branch_id")
        intent_id = _required_text(intent.get("intent_id"), "intent_id")
        request = _required_text(request_id, "request_id")
    digest = canonical_sha256(
        {
            "account": account,
            identity_field: intent_owner_id,
            "intent_id": intent_id,
            "request_id": request,
        }
    )[:24]
    generation_key = "batch_generation_hash"
    cancellation_payload = {
        "schema_version": f"wheel_{direction}_intent_cancelled.v1",
        "market": _wheel_market(source),
        "request_id": request,
        "actor": _required_text(actor, "actor"),
        "reason": _required_text(reason, "reason"),
        "broker_order_inactive_confirmed": True,
        "remaining_contracts": int(intent.get("remaining_contracts") or 0),
        generation_key: str(source.get(generation_key) or ""),
    }
    if direction == "put":
        cancellation_payload["capacity_identity_hash"] = intent_payload.get(
            "capacity_identity_hash"
        )
        cancellation_payload["cash_reservation_amount"] = intent_payload.get(
            "cash_reservation_amount"
        )
        cancellation_payload["cash_reservation_currency"] = intent_payload.get(
            "cash_reservation_currency"
        )
    return build_wheel_event(
        event_id=f"wheel-{direction}-intent-cancelled:{digest}",
        event_schema_version=(
            WHEEL_EVENT_SCHEMA_V1
            if direction == "call" and source.get("legacy_call_adapter")
            else WHEEL_EVENT_SCHEMA_V2
        ),
        account=account,
        wheel_branch_id=None if direction == "call" else intent_owner_id,
        lot_id=(
            intent_owner_id
            if direction == "call"
            else str(source.get("stock_lot_id") or "").strip() or None
        ),
        event_type=f"wheel_{direction}_intent_cancelled",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        intent_id=intent_id,
        payload=cancellation_payload,
    )


def plan_wheel_call_intent_cancel(
    batch: Mapping[str, Any],
    intent: Mapping[str, Any],
    request_id: str,
    actor: str,
    broker_order_inactive_confirmed: bool,
    reason: str,
    *,
    occurred_at_ms: int,
    recorded_at_ms: int,
) -> dict[str, Any] | None:
    return _plan_intent_cancel(
        batch, intent, None, request_id, actor, broker_order_inactive_confirmed, reason,
        direction="call", occurred_at_ms=occurred_at_ms, recorded_at_ms=recorded_at_ms,
    )


def plan_wheel_put_intent_cancel(
    branch: Mapping[str, Any],
    intent: Mapping[str, Any],
    cash_capacity_fact: Mapping[str, Any],
    request_id: str,
    actor: str,
    broker_order_inactive_confirmed: bool,
    reason: str,
    *,
    occurred_at_ms: int,
    recorded_at_ms: int,
) -> dict[str, Any] | None:
    return _plan_intent_cancel(
        branch, intent, cash_capacity_fact, request_id, actor,
        broker_order_inactive_confirmed, reason, direction="put",
        occurred_at_ms=occurred_at_ms, recorded_at_ms=recorded_at_ms,
    )


def _plan_intent_consume(
    source: Mapping[str, Any],
    intent: Mapping[str, Any],
    fill: Any,
    capacity_fact: Mapping[str, Any],
    *,
    direction: str,
    recorded_at_ms: int,
) -> dict[str, Any]:
    """Consume a Wheel intent from a matching fill; Call and Put differ only by these facts."""

    direction_label = "Call" if direction == "call" else "Put"
    source_label = "batch" if direction == "call" else "branch"
    if source.get("lifecycle_status") != "active" or source.get("integrity_status") != "trusted":
        raise ValueError(f"Wheel {source_label} is not an active trusted lifecycle")
    if direction == "put" and str(source.get("direction") or "").strip().lower() != "put":
        raise ValueError("Wheel Put intent requires a Put branch")
    if str(intent.get("status") or "") != "active":
        raise ValueError(f"Wheel {direction_label} intent is not active")
    event = _trade_event_fact(fill)
    if (
        _event_type(event) != "open"
        or _trade_option_type(event) != direction
        or _trade_position_side(event) != "short"
    ):
        raise ValueError(f"Wheel {direction_label} intent can only consume a Short {direction_label} open")
    payload = intent.get("payload")
    intent_payload = payload if isinstance(payload, Mapping) else intent
    symbol = str(source.get("symbol") or "")
    if direction == "call":
        account = str(source.get("account") or "")
    else:
        account = _required_text(source.get("account"), "account").lower()
        _validate_put_intent_reservation(intent_payload, capacity_fact, account=account)
    fill_event_id = _required_text(event.get("event_id"), "source_trade_event_id")
    contracts = _positive_int(event.get("contracts"), "fill contracts")
    multiplier = _positive_int(event.get("multiplier"), "fill multiplier")
    occurred_at_ms = _positive_int(event.get("event_time_ms"), "fill occurred_at_ms")
    if contracts > int(intent.get("remaining_contracts") or 0):
        raise ValueError(f"Wheel {direction_label} fill exceeds intent remainder")
    if not (
        int(intent.get("created_at_ms") or 0)
        <= occurred_at_ms
        <= int(intent.get("expires_at_ms") or 0)
    ):
        raise ValueError(f"Wheel {direction_label} fill is outside the intent window")
    if (
        _trade_account(event) != account
        or _trade_symbol(event) != symbol
        or float(event.get("strike") or 0) != float(intent_payload.get("strike") or 0)
        or str(event.get("expiration_ymd") or "")
        != str(intent_payload.get("expiration_ymd") or "")
        or multiplier != int(intent_payload.get("multiplier") or 0)
    ):
        raise ValueError(f"Wheel {direction_label} fill does not match the intent contract")
    bound_order = str(intent_payload.get("broker_order_id") or "").strip()
    fill_payload = event.get("raw_payload") or {}
    if bound_order and (
        str(fill_payload.get("order_id") or "").strip() != bound_order
        or futu_order_namespace_issue(fill_payload) is not None
    ):
        raise ValueError(f"Wheel {direction_label} fill does not match the bound order")
    if direction == "call":
        _coverage_capacity(
            capacity_fact, account=account, symbol=symbol,
            contracts=contracts, multiplier=multiplier,
        )
    intent_id = _required_text(intent.get("intent_id"), "intent_id")
    consumed_payload = {
        "schema_version": f"wheel_{direction}_intent_consumed.v1",
        "market": _wheel_market(source),
        "contracts": contracts,
        "multiplier": multiplier,
        f"{direction}_lot_id": str(event.get("lot_id") or f"lot_{fill_event_id}"),
    }
    if direction == "put":
        consumed_payload["capacity_identity_hash"] = intent_payload.get("capacity_identity_hash")
        consumed_payload["cash_reservation_amount"] = round(
            float(intent_payload.get("strike") or 0) * contract_share_quantity(contracts, multiplier),
            6,
        )
        consumed_payload["cash_reservation_currency"] = intent_payload.get(
            "cash_reservation_currency"
        )
    intent_owner_id = (
        str(source.get("stock_lot_id") or "")
        if direction == "call"
        else _required_text(source.get("wheel_branch_id"), "wheel_branch_id")
    )
    return build_wheel_event(
        event_id=f"wheel-{direction}-intent-consumed:{intent_id}:{fill_event_id}",
        event_schema_version=(
            WHEEL_EVENT_SCHEMA_V1
            if direction == "call" and source.get("legacy_call_adapter")
            else WHEEL_EVENT_SCHEMA_V2
        ),
        account=account,
        wheel_branch_id=None if direction == "call" else intent_owner_id,
        lot_id=(
            intent_owner_id
            if direction == "call"
            else str(source.get("stock_lot_id") or "").strip() or None
        ),
        event_type=f"wheel_{direction}_intent_consumed",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        intent_id=intent_id,
        source_trade_event_id=fill_event_id,
        payload=consumed_payload,
    )


def plan_wheel_call_intent_consume(
    batch: Mapping[str, Any],
    intent: Mapping[str, Any],
    fill: Any,
    coverage_fact: Mapping[str, Any],
    *,
    recorded_at_ms: int,
) -> dict[str, Any]:
    return _plan_intent_consume(
        batch, intent, fill, coverage_fact, direction="call", recorded_at_ms=recorded_at_ms,
    )


def plan_wheel_put_intent_consume(
    branch: Mapping[str, Any],
    intent: Mapping[str, Any],
    fill: Any,
    cash_capacity_fact: Mapping[str, Any],
    *,
    recorded_at_ms: int,
) -> dict[str, Any]:
    return _plan_intent_consume(
        branch, intent, fill, cash_capacity_fact, direction="put", recorded_at_ms=recorded_at_ms,
    )

