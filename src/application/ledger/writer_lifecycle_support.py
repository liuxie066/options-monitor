from __future__ import annotations

from .writer_common import (
    Any,
    ComboMembershipResolution,
    ContractKey,
    Decimal,
    InvalidOperation,
    Sequence,
    SettlementAdmissionStateIncoherent,
    SettlementSemanticUnavailable,
    TradeEvent,
    allocation_id_for,
    canonical_decimal_text,
    canonical_payload_hash,
    canonical_symbol,
    effective_contracts_open,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    normalize_currency,
    resolve_allocations,
    settlement_semantic_from_evidence,
    terminal_event_id_for,
    valid_void_target_event_id,
)

def _existing_combo_adoption_leg(
    *,
    records_by_id: dict[str, Any],
    events_by_id: dict[str, dict[str, Any]],
    record_id: str,
    open_event_id: str,
    group_id: str,
    expected_contracts: int,
    expected_option_type: str,
    expected_position_side: str,
    accepted_roles: set[str],
    require_fully_open: bool,
) -> dict[str, Any]:
    record_value = str(record_id or "").strip()
    event_value = str(open_event_id or "").strip()
    record = records_by_id.get(record_value)
    event = events_by_id.get(event_value)
    if record is None or event is None:
        raise ValueError("combo identity adoption requires exact record and open event ids")
    fields = dict(
        record.get("fields", {})
        if isinstance(record, dict)
        else record.fields
    )
    event_contract = dict(event.get("contract_key") or {}) if isinstance(event.get("contract_key"), dict) else {}
    option_type = str(fields.get("option_type") or "").strip().lower()
    position_side = str(fields.get("side") or "").strip().lower()
    role = str(fields.get("leg_role") or "").strip().lower()
    original_contracts = _combo_contract_count(fields.get("contracts"))
    open_contracts = _combo_nonnegative_contract_count(
        fields.get("contracts_open")
    )
    if (
        str(fields.get("source_event_id") or "").strip() != event_value
        or str(event.get("event_type") or "").strip().lower() != "open"
        or _combo_contract_count(event.get("contracts")) != expected_contracts
        or original_contracts != expected_contracts
        or open_contracts is None
        or open_contracts > expected_contracts
        or (require_fully_open and open_contracts != expected_contracts)
        or option_type != expected_option_type
        or position_side != expected_position_side
        or role not in accepted_roles
        or str(fields.get("strategy") or "").strip().lower() != "combo_yield"
        or str(fields.get("strategy_group_id") or "").strip() != group_id
    ):
        raise ValueError("combo identity adoption leg metadata mismatch")
    contract_key = ContractKey.from_values(
        broker=fields.get("broker"),
        account=fields.get("account"),
        underlying_symbol=fields.get("symbol"),
        option_type=option_type,
        position_side=position_side,
        strike=fields.get("strike"),
        expiration_ymd=fields.get("expiration_ymd"),
    )
    event_key = ContractKey.from_values(
        broker=event_contract.get("broker"),
        account=event_contract.get("account"),
        underlying_symbol=event_contract.get("underlying_symbol"),
        option_type=event_contract.get("option_type"),
        position_side=event_contract.get("position_side"),
        strike=event_contract.get("strike"),
        expiration_ymd=event_contract.get("expiration_ymd"),
    )
    if contract_key != event_key:
        raise ValueError("combo identity adoption contract key mismatch")
    multiplier = effective_multiplier(fields)
    currency = normalize_currency(fields.get("currency"))
    if multiplier is None or not currency:
        raise ValueError("combo identity adoption leg economics incomplete")
    return {
        "strategy_group_id": group_id,
        "strategy": "combo_yield",
        "broker": contract_key.broker,
        "account": contract_key.account,
        "symbol": contract_key.underlying_symbol,
        "leg_role": role,
        "contracts": expected_contracts,
        "open_event_id": event_value,
        "record_id": record_value,
        "contract_key": contract_key.to_dict(),
        "currency": currency,
        "multiplier": float(multiplier),
        "strike": float(contract_key.strike),
        "expiration_ymd": contract_key.expiration_ymd,
    }

def _combo_contract_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = Decimal(str(value))
        parsed = int(numeric)
    except (
        InvalidOperation,
        TypeError,
        ValueError,
        OverflowError,
    ):
        return None
    if not numeric.is_finite() or parsed <= 0 or numeric != parsed:
        return None
    return parsed

def _combo_nonnegative_contract_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = Decimal(str(value))
        parsed = int(numeric)
    except (
        InvalidOperation,
        TypeError,
        ValueError,
        OverflowError,
    ):
        return None
    if not numeric.is_finite() or parsed < 0 or numeric != parsed:
        return None
    return parsed

def _assert_combo_membership_exact(
    membership: ComboMembershipResolution,
    *,
    expected_record_ids: set[str],
    require_fully_open: bool,
) -> None:
    expected = tuple(sorted(expected_record_ids))
    if (
        membership.fact.get("status") != "exact"
        or membership.global_current_record_ids != expected
        or membership.global_historical_record_ids != expected
        or membership.retag_events
        or (
            require_fully_open
            and membership.global_live_record_ids != expected
        )
        or any(
            record_id not in expected_record_ids
            for record_id in membership.global_live_record_ids
        )
    ):
        reasons = ",".join(membership.fact.get("reason_codes") or ())
        raise ValueError(
            "combo identity membership conflict"
            + (f": {reasons}" if reasons else "")
        )

def _combo_leg_from_projected_record(
    *,
    intent: dict[str, Any],
    prefix: str,
    records_by_open_event: dict[str, Any],
) -> dict[str, Any]:
    event_id = str(intent.get(f"{prefix}_open_event_id") or "").strip()
    expected_record_id = str(intent.get(f"{prefix}_expected_record_id") or "").strip()
    role = str(intent.get(f"{prefix}_role") or "").strip().lower()
    record = records_by_open_event.get(event_id)
    record_id = (
        str(record.get("record_id") or "").strip()
        if isinstance(record, dict)
        else str(getattr(record, "record_id", "") or "").strip()
    )
    if record is None or record_id != expected_record_id:
        raise ValueError(f"combo identity {prefix} projected record mismatch")
    fields = dict(
        record.get("fields", {})
        if isinstance(record, dict)
        else getattr(record, "fields", {})
        or {}
    )
    expected_contracts = int(intent.get("expected_contracts") or 0)
    if int(fields.get("contracts") or 0) != expected_contracts:
        raise ValueError(f"combo identity {prefix} original quantity mismatch")
    if int(fields.get("contracts_open") or 0) != expected_contracts:
        raise ValueError(f"combo identity {prefix} is not fully open")
    contract_key_name = (
        "funding_put"
        if role in {"funding_put", "sell_put"}
        else "participation_call"
    )
    contract_keys = intent.get("contract_keys")
    contract_key = (
        dict(contract_keys.get(contract_key_name) or {})
        if isinstance(contract_keys, dict)
        else {}
    )
    return {
        "strategy_group_id": intent.get("group_id"),
        "strategy": intent.get("strategy"),
        "account": intent.get("account"),
        "symbol": intent.get("symbol"),
        "leg_role": role,
        "contracts": expected_contracts,
        "open_event_id": event_id,
        "record_id": record_id,
        "contract_key": contract_key,
    }

def _lifecycle_evidence_business_fact(
    evidence: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(evidence or {})
    stock = (
        dict(payload.get("stock_settlement") or {})
        if isinstance(payload.get("stock_settlement"), dict)
        else {}
    )
    fact = {
        "evidence_id": str(payload.get("evidence_id") or "").strip(),
        "source_type": str(payload.get("source_type") or "").strip(),
        "source_event_id": str(
            payload.get("source_event_id") or ""
        ).strip(),
        "evidence_type": str(
            payload.get("terminal_type")
            or payload.get("evidence_type")
            or ""
        ).strip().lower(),
        "account": str(payload.get("account") or "").strip().lower(),
        "symbol": str(payload.get("symbol") or "").strip().upper(),
        "option_type": str(
            payload.get("option_type") or ""
        ).strip().lower(),
        "position_side": str(
            payload.get("position_side") or ""
        ).strip().lower(),
        "strike": (
            canonical_decimal_text(payload.get("strike"))
            if payload.get("strike") is not None
            else None
        ),
        "expiration_ymd": str(
            payload.get("expiration_ymd") or ""
        ).strip(),
        "contracts": int(payload.get("contracts") or 0),
        "event_time_ms": int(
            payload.get("event_time_ms")
            or payload.get("observed_at_ms")
            or 0
        ),
        "option_event_time_ms": int(
            payload.get("option_event_time_ms") or 0
        ),
        "target_contracts_by_lot": {
            str(key): int(value)
            for key, value in sorted(
                dict(payload.get("target_contracts_by_lot") or {}).items()
            )
        },
        "stock_settlement": {
            "source_event_id": str(
                stock.get("source_event_id") or ""
            ).strip(),
            "symbol": str(stock.get("symbol") or "").strip().upper(),
            "side": str(stock.get("side") or "").strip().lower(),
            "shares": (
                canonical_decimal_text(stock.get("shares"))
                if stock.get("shares") is not None
                else None
            ),
            "price": (
                canonical_decimal_text(stock.get("price"))
                if stock.get("price") is not None
                else None
            ),
            "event_time_ms": int(stock.get("event_time_ms") or 0),
            "order_id": str(stock.get("order_id") or "").strip() or None,
            "clearing_date": (
                str(stock.get("clearing_date") or "").strip() or None
            ),
        },
        "observation_hashes": sorted(
            {
                str(value).strip()
                for key, value in payload.items()
                if (
                    key.endswith("_hash")
                    or key in {"observation_hash", "calendar_hash"}
                )
                and str(value or "").strip()
            }
        ),
    }
    return {
        "evidence_id": fact["evidence_id"],
        "evidence_hash": canonical_payload_hash(fact),
    }

def _projected_remaining_by_lot(
    projection_lots: Sequence[Any],
    *,
    target_lot_ids: Sequence[str],
) -> dict[str, int]:
    wanted = {str(item or "").strip() for item in target_lot_ids}
    remaining: dict[str, int] = {}
    for record in projection_lots:
        record_id = str(
            record.get("record_id")
            if isinstance(record, dict)
            else getattr(record, "record_id", "")
            or ""
        ).strip()
        if record_id not in wanted:
            continue
        fields = dict(
            record.get("fields", {})
            if isinstance(record, dict)
            else getattr(record, "fields", {})
            or {}
        )
        remaining[record_id] = int(fields.get("contracts_open") or 0)
    missing = sorted(wanted - set(remaining))
    if missing:
        raise ValueError(
            "lifecycle target projection missing: " + ",".join(missing)
        )
    return dict(sorted(remaining.items()))

def _lifecycle_state_payload(
    *,
    lifecycle_case: dict[str, Any],
    evidence_rows: Sequence[dict[str, Any]],
    source_claims: Sequence[dict[str, Any]],
    allocations: Sequence[dict[str, Any]],
    void_event_ids: Sequence[str],
    projected_remaining_by_lot: dict[str, int],
    status: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    void_ids = {
        str(item or "").strip()
        for item in void_event_ids
        if str(item or "").strip()
    }
    effective_allocations = [
        {
            "allocation_id": str(
                item.get("allocation_id") or ""
            ).strip(),
            "evidence_id": str(item.get("evidence_id") or "").strip(),
            "target_lot_id": str(
                item.get("target_lot_id") or ""
            ).strip(),
            "terminal_event_id": str(
                item.get("canonical_terminal_event_id") or ""
            ).strip(),
            "terminal_type": str(
                item.get("terminal_type") or ""
            ).strip().lower(),
            "contracts": int(item.get("contracts_allocated") or 0),
        }
        for item in allocations
        if str(
            item.get("canonical_terminal_event_id") or ""
        ).strip()
        not in void_ids
    ]
    claims = [
        {
            "source_key": str(item.get("source_key") or "").strip(),
            "owner_evidence_id": str(
                item.get("owner_evidence_id") or ""
            ).strip(),
            "source_role": str(
                item.get("source_role") or ""
            ).strip().lower(),
            "source_payload_hash": str(
                item.get("source_payload_hash") or ""
            ).strip(),
        }
        for item in source_claims
    ]
    reasons = sorted(
        {
            str(item or "").strip()
            for item in (
                summary.get("lifecycle_reason_codes")
                or summary.get("reason_codes")
                or []
            )
            if str(item or "").strip()
        }
    )
    return {
        "case": {
            "case_id": str(lifecycle_case.get("case_id") or "").strip(),
            "schema_version": str(
                lifecycle_case.get("schema_version") or ""
            ).strip(),
            "target_contracts_by_lot": {
                str(key): int(value)
                for key, value in sorted(
                    dict(
                        lifecycle_case.get("target_contracts_by_lot")
                        or {}
                    ).items()
                )
            },
        },
        "evidence": sorted(
            (
                _lifecycle_evidence_business_fact(item)
                for item in evidence_rows
            ),
            key=lambda item: item["evidence_id"],
        ),
        "source_claims": sorted(
            claims,
            key=lambda item: (
                item["source_key"],
                item["source_role"],
                item["owner_evidence_id"],
            ),
        ),
        "effective_allocations": sorted(
            effective_allocations,
            key=lambda item: (
                item["target_lot_id"],
                item["terminal_event_id"],
            ),
        ),
        "effective_void_event_ids": sorted(void_ids),
        "projected_remaining_by_lot": dict(
            sorted(projected_remaining_by_lot.items())
        ),
        "reason_state": str(status or "").strip().lower(),
        "close_reason": str(
            summary.get("close_reason")
            or summary.get("decision_type")
            or ""
        ).strip().lower(),
        "reason_codes": reasons,
        "pairing_until_ms": (
            int(summary["pairing_until_ms"])
            if summary.get("pairing_until_ms") is not None
            else None
        ),
        "timing_policy_hash": str(
            summary.get("timing_policy_hash") or ""
        ).strip()
        or None,
        "observation_hashes": sorted(
            {
                str(value).strip()
                for key, value in summary.items()
                if (
                    key.endswith("_hash")
                    or key == "observation_hash"
                )
                and str(value or "").strip()
            }
        ),
    }

def _require_settlement_foreign_keys_clean(
    sqlite_repo: Any,
    *,
    conn: Any,
) -> None:
    try:
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
    except RuntimeError as exc:
        raise SettlementAdmissionStateIncoherent(
            "settlement canonical foreign keys are incoherent"
        ) from exc

def _require_duplicate_settlement_state_base(
    sqlite_repo: Any,
    *,
    conn: Any,
    lifecycle_case: dict[str, Any],
    admission: dict[str, Any],
) -> dict[str, Any]:
    case_id = str(lifecycle_case.get("case_id") or "").strip()
    evidence_id = str(admission.get("evidence_id") or "").strip()
    canonical_evidence = sqlite_repo.get_trade_lifecycle_evidence(
        evidence_id,
        conn=conn,
    )
    if canonical_evidence is None:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement evidence is missing"
        )
    if str(canonical_evidence.get("case_id") or "").strip() != case_id:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement evidence case binding is incoherent"
        )
    try:
        _semantic, canonical_fingerprint = (
            settlement_semantic_from_evidence(canonical_evidence)
        )
    except SettlementSemanticUnavailable as exc:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement evidence semantic is incoherent"
        ) from exc
    if canonical_fingerprint != str(
        admission.get("semantic_fingerprint") or ""
    ).strip():
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement evidence fingerprint is incoherent"
        )

    summary = (
        dict(lifecycle_case.get("derived_summary") or {})
        if isinstance(lifecycle_case.get("derived_summary"), dict)
        else {}
    )
    try:
        resolution_revision = int(
            summary.get("resolution_revision") or 0
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement case revision is incoherent"
        ) from exc
    if (
        resolution_revision <= 0
        or not str(summary.get("state_fingerprint") or "").strip()
    ):
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement case revision is incoherent"
        )

    allocations = list(
        sqlite_repo.list_trade_lifecycle_allocations(
            case_id=case_id,
            conn=conn,
        )
    )
    try:
        void_event_ids = _effective_void_target_ids(
            sqlite_repo,
            conn=conn,
        )
        resolution = resolve_allocations(
            lifecycle_case.get("target_contracts_by_lot"),
            allocations,
            void_event_ids=void_event_ids,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement allocations are incoherent"
        ) from exc
    if resolution.status != "ok":
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement allocations are incoherent"
        )
    expected_summary = {
        "target_contracts_by_lot": resolution.target_contracts_by_lot,
        "resolved_contracts_by_lot": resolution.resolved_contracts_by_lot,
        "remaining_contracts_by_lot": (
            resolution.remaining_contracts_by_lot
        ),
        "resolved_contracts_by_terminal_type": (
            resolution.resolved_contracts_by_terminal_type
        ),
    }
    for field, expected in expected_summary.items():
        if summary.get(field) != expected:
            raise SettlementAdmissionStateIncoherent(
                f"duplicate settlement case summary is incoherent: {field}"
            )
    try:
        projected_remaining: dict[str, int] = {}
        for lot_id in sorted(resolution.target_contracts_by_lot):
            lot_fields = sqlite_repo.get_position_lot_fields(
                lot_id,
                conn=conn,
            )
            if not isinstance(lot_fields, dict):
                raise TypeError("position lot fields are unavailable")
            projected_remaining[lot_id] = int(
                lot_fields.get("contracts_open") or 0
            )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement target projection is unavailable"
        ) from exc
    if projected_remaining != resolution.remaining_contracts_by_lot:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement target projection is incoherent"
        )
    _require_settlement_foreign_keys_clean(sqlite_repo, conn=conn)
    return {
        "canonical_evidence": canonical_evidence,
        "summary": summary,
        "allocations": allocations,
    }

def _require_duplicate_settlement_allocation_state(
    sqlite_repo: Any,
    *,
    conn: Any,
    lifecycle_case: dict[str, Any],
    admission: dict[str, Any],
    requested_status: str,
) -> dict[str, Any]:
    state = _require_duplicate_settlement_state_base(
        sqlite_repo,
        conn=conn,
        lifecycle_case=lifecycle_case,
        admission=admission,
    )
    status = str(lifecycle_case.get("status") or "").strip().lower()
    if status != str(requested_status or "").strip().lower():
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement terminal status is incoherent"
        )
    evidence_id = str(admission.get("evidence_id") or "").strip()
    evidence_allocations = [
        item
        for item in state["allocations"]
        if str(item.get("evidence_id") or "").strip() == evidence_id
    ]
    if not evidence_allocations:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement terminal allocations are missing"
        )
    canonical_evidence = state["canonical_evidence"]
    try:
        expected_contracts = _positive_lifecycle_contracts(
            canonical_evidence.get("contracts")
        )
    except ValueError as exc:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement terminal quantity is incoherent"
        ) from exc
    try:
        allocated_contracts = sum(
            int(item.get("contracts_allocated") or 0)
            for item in evidence_allocations
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement terminal allocation quantity is incoherent"
        ) from exc
    if allocated_contracts != expected_contracts:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement terminal allocation quantity is incoherent"
        )
    terminal_type = str(
        canonical_evidence.get("terminal_type")
        or canonical_evidence.get("evidence_type")
        or ""
    ).strip().lower()
    if {
        str(item.get("terminal_type") or "").strip().lower()
        for item in evidence_allocations
    } != {terminal_type}:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement terminal allocation type is incoherent"
        )
    return state

def _require_duplicate_settlement_issue_state(
    sqlite_repo: Any,
    *,
    conn: Any,
    lifecycle_case: dict[str, Any],
    admission: dict[str, Any],
    requested_status: str,
    requested_reasons: Sequence[str],
) -> dict[str, Any]:
    state = _require_duplicate_settlement_state_base(
        sqlite_repo,
        conn=conn,
        lifecycle_case=lifecycle_case,
        admission=admission,
    )
    status = str(lifecycle_case.get("status") or "").strip().lower()
    if status != str(requested_status or "").strip().lower():
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement issue status is incoherent"
        )
    evidence_id = str(admission.get("evidence_id") or "").strip()
    if any(
        str(item.get("evidence_id") or "").strip() == evidence_id
        for item in state["allocations"]
    ):
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement issue has terminal allocations"
        )
    summary = state["summary"]
    try:
        actual_reasons = sorted(
            {
                str(item or "").strip()
                for item in summary.get("lifecycle_reason_codes") or []
                if str(item or "").strip()
            }
        )
    except TypeError as exc:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement issue reasons are incoherent"
        ) from exc
    if actual_reasons != sorted(set(requested_reasons)):
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement issue reasons are incoherent"
        )
    try:
        conflict_evidence_ids = {
            str(item or "").strip()
            for item in summary.get("conflict_evidence_ids") or []
            if str(item or "").strip()
        }
    except TypeError as exc:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement issue evidence binding is incoherent"
        ) from exc
    if evidence_id not in conflict_evidence_ids:
        raise SettlementAdmissionStateIncoherent(
            "duplicate settlement issue evidence binding is incoherent"
        )
    return state

def _lifecycle_notification_transition(
    *,
    case_id: str,
    status: str,
) -> tuple[str, str]:
    status_value = str(status or "").strip().lower()
    if status_value == "ledger_written":
        transition_type = "resolution_confirmed"
    elif status_value in {"needs_review", "conflict"}:
        transition_type = status_value
    else:
        transition_type = "option_leg_closed"
    return (
        transition_type,
        f"lifecycle:{case_id}:{transition_type}",
    )

def _validate_existing_lifecycle_evidence(
    *,
    existing: dict[str, Any],
    incoming: dict[str, Any],
    case_id: str,
) -> None:
    for field in (
        "evidence_id",
        "source_type",
        "source_event_id",
        "evidence_type",
        "account",
        "symbol",
        "contracts",
    ):
        if existing.get(field) != incoming.get(field):
            raise ValueError(f"lifecycle evidence immutable conflict: {field}")
    if str(existing.get("case_id") or "").strip() not in {"", case_id}:
        raise ValueError("lifecycle evidence is already bound to another case")

def _effective_void_target_ids(
    sqlite_repo: Any,
    *,
    conn: Any,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                target
                for item in sqlite_repo.list_trade_events(conn=conn)
                for target in [valid_void_target_event_id(item)]
                if target
            }
        )
    )

def _positive_lifecycle_contracts(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("lifecycle evidence contracts must be positive")
    try:
        numeric = Decimal(str(value))
        parsed = int(numeric)
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("lifecycle evidence contracts must be positive") from exc
    if not numeric.is_finite() or parsed <= 0 or numeric != parsed:
        raise ValueError("lifecycle evidence contracts must be positive")
    return parsed

def _matching_lifecycle_lots(
    position_lots: Sequence[dict[str, Any]],
    *,
    contract_key: ContractKey,
) -> list[tuple[str, int, int]]:
    matches: list[tuple[str, int, int]] = []
    for item in position_lots:
        if not isinstance(item, dict):
            continue
        lot_id = str(item.get("record_id") or "").strip()
        fields = dict(item.get("fields") or {})
        remaining = effective_contracts_open(fields)
        if not lot_id or remaining <= 0:
            continue
        try:
            candidate_key = ContractKey.from_values(
                broker=fields.get("broker"),
                account=fields.get("account"),
                underlying_symbol=fields.get("symbol"),
                option_type=fields.get("option_type"),
                position_side=fields.get("side"),
                strike=effective_strike(fields),
                expiration_ymd=effective_expiration_ymd(fields),
            )
        except (TypeError, ValueError):
            continue
        if candidate_key.position_key != contract_key.position_key:
            continue
        try:
            opened_at = int(fields.get("opened_at") or 0)
        except (TypeError, ValueError):
            opened_at = 0
        matches.append((lot_id, remaining, opened_at))
    return sorted(matches, key=lambda item: (item[2], item[0]))

def _allocate_lifecycle_reservation(
    *,
    contracts: int,
    available_by_lot: dict[str, int],
    matching_lots: Sequence[tuple[str, int, int]],
) -> dict[str, int]:
    remaining = int(contracts)
    allocation: dict[str, int] = {}
    lot_order = [lot_id for lot_id, _contracts, _opened_at in matching_lots]
    lot_order.extend(
        lot_id
        for lot_id in sorted(available_by_lot)
        if lot_id not in lot_order
    )
    for lot_id in lot_order:
        available = int(available_by_lot.get(lot_id, 0))
        if available <= 0 or remaining <= 0:
            continue
        allocated = min(available, remaining)
        allocation[lot_id] = allocated
        remaining -= allocated
    if remaining:
        raise ValueError("lifecycle_reservation_exceeds_available_target")
    return allocation

def _validate_existing_zero_price_evidence(
    *,
    existing: dict[str, Any],
    incoming: dict[str, Any],
    contract_key: ContractKey,
    contracts: int,
) -> None:
    for field in ("evidence_id", "source_type", "source_event_id", "evidence_type"):
        if str(existing.get(field) or "").strip() != str(
            incoming.get(field) or ""
        ).strip():
            raise ValueError(f"lifecycle evidence immutable conflict: {field}")
    if int(existing.get("contracts") or 0) != contracts:
        raise ValueError("lifecycle evidence immutable conflict: contracts")
    if (
        str(existing.get("account") or "").strip().lower()
        != contract_key.account
        or str(existing.get("symbol") or "").strip().upper()
        != contract_key.underlying_symbol
        or str(existing.get("option_type") or "").strip().lower()
        != contract_key.option_type
        or str(existing.get("position_side") or "").strip().lower()
        != contract_key.position_side
        or Decimal(str(existing.get("strike"))) != Decimal(contract_key.strike)
        or str(existing.get("expiration_ymd") or "").strip()
        != contract_key.expiration_ymd
    ):
        raise ValueError("lifecycle evidence immutable conflict: contract_identity")

def _validate_lifecycle_event_allocation_plan(
    *,
    case_id: str,
    lifecycle_case: dict[str, Any],
    evidence: dict[str, Any],
    terminal_events: Sequence[TradeEvent],
    allocations: Sequence[dict[str, Any]],
    existing_allocations: Sequence[dict[str, Any]],
    void_event_ids: Sequence[str] = (),
) -> tuple[dict[str, Any], str]:
    evidence_id = str(evidence.get("evidence_id") or "").strip()
    if not terminal_events or len(terminal_events) != len(allocations):
        raise ValueError("lifecycle evidence requires one terminal event per allocation")
    try:
        evidence_contracts = int(evidence.get("contracts") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("lifecycle evidence contracts are invalid") from exc
    if evidence_contracts <= 0:
        raise ValueError("lifecycle evidence contracts must be positive")
    events_by_id = {event.event_id: event for event in terminal_events}
    if len(events_by_id) != len(terminal_events):
        raise ValueError("lifecycle terminal event ids must be unique")
    case_account = str(lifecycle_case.get("account") or "").strip().lower()
    evidence_account = str(evidence.get("account") or "").strip().lower()
    case_symbol = str(lifecycle_case.get("symbol") or "").strip().upper()
    evidence_symbol = str(evidence.get("symbol") or "").strip().upper()
    if evidence_account != case_account or evidence_symbol != case_symbol:
        raise ValueError("lifecycle evidence account or symbol mismatch")
    target_contracts = lifecycle_case.get("target_contracts_by_lot")
    case_contract_key = str(lifecycle_case.get("contract_key") or "").strip()
    allocated_total = 0
    for allocation in allocations:
        if str(allocation.get("case_id") or "").strip() != case_id:
            raise ValueError("lifecycle allocation case mismatch")
        if str(allocation.get("evidence_id") or "").strip() != evidence_id:
            raise ValueError("lifecycle allocation evidence mismatch")
        event_id = str(allocation.get("canonical_terminal_event_id") or "").strip()
        event = events_by_id.get(event_id)
        if event is None:
            raise ValueError("lifecycle allocation terminal event missing")
        contracts = int(allocation.get("contracts_allocated") or 0)
        lot_id = str(allocation.get("target_lot_id") or "").strip()
        terminal_type = str(allocation.get("terminal_type") or "").strip().lower()
        expected_allocation_id = allocation_id_for(
            case_id=case_id,
            evidence_id=evidence_id,
            target_lot_id=lot_id,
        )
        expected_event_id = terminal_event_id_for(
            case_id=case_id,
            evidence_id=evidence_id,
            target_lot_id=lot_id,
            terminal_type=terminal_type,
            contracts_allocated=contracts,
        )
        if str(allocation.get("allocation_id") or "").strip() != expected_allocation_id:
            raise ValueError("lifecycle allocation id is not deterministic")
        if event_id != expected_event_id:
            raise ValueError("lifecycle terminal event id is not deterministic")
        if (
            contracts <= 0
            or event.contracts != contracts
            or str(event.target_lot_id or "") != lot_id
            or event.event_type != terminal_type
            or event.contract_key.position_key != case_contract_key
        ):
            raise ValueError("lifecycle allocation and terminal event mismatch")
        raw_payload = dict(event.raw_payload or {})
        if (
            str(raw_payload.get("case_id") or "").strip() != case_id
            or str(raw_payload.get("evidence_id") or "").strip() != evidence_id
            or str(raw_payload.get("allocation_id") or "").strip()
            != str(allocation.get("allocation_id") or "").strip()
            or int(raw_payload.get("contracts") or 0) != contracts
        ):
            raise ValueError("lifecycle terminal event provenance mismatch")
        allocated_total += contracts
    if allocated_total != evidence_contracts:
        raise ValueError("lifecycle allocated contracts do not equal evidence contracts")
    resolution = resolve_allocations(
        target_contracts,
        [*existing_allocations, *allocations],
        void_event_ids=void_event_ids,
    )
    if resolution.status != "ok":
        raise ValueError(
            "lifecycle allocation conflicts with frozen target: "
            + ",".join(resolution.reason_codes)
        )
    status = (
        "ledger_written"
        if resolution.remaining_contracts == 0
        else "partially_resolved"
    )
    summary = {
        "target_contracts_by_lot": resolution.target_contracts_by_lot,
        "resolved_contracts_by_lot": resolution.resolved_contracts_by_lot,
        "remaining_contracts_by_lot": resolution.remaining_contracts_by_lot,
        "resolved_contracts_by_terminal_type": (
            resolution.resolved_contracts_by_terminal_type
        ),
    }
    return summary, status

def _validate_broker_settlement_pair_for_write(
    repo: Any,
    *,
    conn: Any,
    lifecycle_case: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    if (
        str(evidence.get("source_type") or "").strip().lower()
        != "broker_settlement_pair"
        or str(
            evidence.get("terminal_type")
            or evidence.get("evidence_type")
            or ""
        ).strip().lower()
        not in {"assignment", "exercise"}
    ):
        return

    case_id = str(lifecycle_case.get("case_id") or "").strip()
    stock = (
        dict(evidence.get("stock_settlement") or {})
        if isinstance(evidence.get("stock_settlement"), dict)
        else {}
    )
    case_futu_account_id = str(
        lifecycle_case.get("futu_account_id") or ""
    ).strip()
    stock_futu_account_id = str(
        stock.get("futu_account_id") or ""
    ).strip()
    if (
        not case_futu_account_id
        or not stock_futu_account_id
        or case_futu_account_id != stock_futu_account_id
    ):
        raise ValueError("stock_settlement_futu_account_mismatch")

    case_symbol = canonical_symbol(lifecycle_case.get("symbol"))
    stock_symbol = canonical_symbol(stock.get("symbol"))
    if not case_symbol or stock_symbol != case_symbol:
        raise ValueError("stock_settlement_symbol_mismatch")

    try:
        strike = Decimal(str(lifecycle_case.get("strike")))
        stock_price = Decimal(str(stock.get("price")))
        shares = Decimal(str(stock.get("shares")))
        multiplier = Decimal(
            str(lifecycle_case.get("multiplier") or 100)
        )
        contracts = int(evidence.get("contracts") or 0)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            "stock_settlement_economic_fields_invalid"
        ) from exc
    if (
        not strike.is_finite()
        or not stock_price.is_finite()
        or stock_price != strike
    ):
        raise ValueError("stock_settlement_price_mismatch")
    if (
        contracts <= 0
        or not shares.is_finite()
        or not multiplier.is_finite()
        or multiplier <= 0
        or shares != multiplier * contracts
    ):
        raise ValueError("stock_settlement_quantity_mismatch")

    terminal_type = str(
        evidence.get("terminal_type")
        or evidence.get("evidence_type")
        or ""
    ).strip().lower()
    option_type = str(
        lifecycle_case.get("option_type") or ""
    ).strip().lower()
    position_side = str(
        lifecycle_case.get("position_side") or ""
    ).strip().lower()
    expected_side = {
        ("assignment", "put", "short"): "buy",
        ("assignment", "call", "short"): "sell",
        ("exercise", "call", "long"): "buy",
        ("exercise", "put", "long"): "sell",
    }.get((terminal_type, option_type, position_side))
    actual_side = str(stock.get("side") or "").strip().lower()
    if expected_side is None or actual_side != expected_side:
        raise ValueError("stock_settlement_side_mismatch")

    try:
        settlement_time_ms = int(
            stock.get("event_time_ms")
            or evidence.get("event_time_ms")
            or 0
        )
        option_event_time_ms = int(
            evidence.get("option_event_time_ms") or 0
        )
        observation_start_ms = int(
            lifecycle_case.get("observation_start_ms") or 0
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("stock_settlement_time_invalid") from exc
    if settlement_time_ms <= 0:
        raise ValueError("stock_settlement_time_invalid")
    early_tolerance_ms = 5 * 60 * 1000
    near_option_event = (
        option_event_time_ms > 0
        and abs(settlement_time_ms - option_event_time_ms)
        <= early_tolerance_ms
    )
    if (
        observation_start_ms > 0
        and settlement_time_ms
        < observation_start_ms - early_tolerance_ms
        and not near_option_event
    ):
        raise ValueError("stock_settlement_before_lifecycle_window")
    timing_policy = repo.get_trade_lifecycle_timing_policy(
        case_id,
        conn=conn,
    )
    try:
        settlement_deadline_ms = int(
            (timing_policy or {}).get("settlement_deadline_ms")
            or 0
        )
    except (TypeError, ValueError, OverflowError):
        settlement_deadline_ms = 0
    if settlement_deadline_ms <= 0 and not near_option_event:
        raise ValueError("settlement_deadline_unavailable")
    if (
        settlement_deadline_ms > 0
        and settlement_time_ms > settlement_deadline_ms
    ):
        raise ValueError("stock_settlement_after_deadline")

    candidates = [
        item
        for item in repo.list_trade_lifecycle_cases(conn=conn)
        if _broker_settlement_case_identity_matches(
            item,
            lifecycle_case=lifecycle_case,
            stock_futu_account_id=stock_futu_account_id,
        )
    ]
    candidate_ids = {
        str(item.get("case_id") or "").strip()
        for item in candidates
    }
    if candidate_ids != {case_id}:
        raise ValueError("ambiguous_lifecycle_case_match")

def _broker_settlement_case_identity_matches(
    candidate: dict[str, Any],
    *,
    lifecycle_case: dict[str, Any],
    stock_futu_account_id: str,
) -> bool:
    if (
        str(candidate.get("schema_version") or "").strip()
        != "lifecycle_case.v2"
        or str(candidate.get("superseded_by_case_id") or "").strip()
    ):
        return False
    try:
        return (
            str(candidate.get("account") or "").strip().lower()
            == str(lifecycle_case.get("account") or "").strip().lower()
            and str(
                candidate.get("futu_account_id") or ""
            ).strip()
            == stock_futu_account_id
            and canonical_symbol(candidate.get("symbol"))
            == canonical_symbol(lifecycle_case.get("symbol"))
            and str(
                candidate.get("option_type") or ""
            ).strip().lower()
            == str(
                lifecycle_case.get("option_type") or ""
            ).strip().lower()
            and str(
                candidate.get("position_side") or ""
            ).strip().lower()
            == str(
                lifecycle_case.get("position_side") or ""
            ).strip().lower()
            and Decimal(str(candidate.get("strike")))
            == Decimal(str(lifecycle_case.get("strike")))
            and str(
                candidate.get("expiration_ymd") or ""
            ).strip()
            == str(
                lifecycle_case.get("expiration_ymd") or ""
            ).strip()
        )
    except (InvalidOperation, TypeError, ValueError):
        return False
