from __future__ import annotations

from .writer_common import (
    Any,
    ContractKey,
    Decimal,
    InvalidOperation,
    LifecycleAttemptAuditEnvelope,
    Sequence,
    advance_direct_lifecycle_anchor_resolution,
    build_initial_lifecycle_case_decision_fact,
    build_lifecycle_case,
    build_notification_intent,
    build_source_consumption_claim,
    canonical_payload_hash,
    canonical_state_fingerprint,
    capture_current_decision_projection_fence,
    date,
    datetime,
    effective_contracts_open,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    expiration_observation_start_ms,
    finalize_current_decision_projection,
    lifecycle_evidence_facts,
    normalize_currency,
    read_current_assigned_stock_fact,
    resolve_allocations,
    symbol_market,
    timezone,
    update_assigned_stock_fact,
    utc_now_ms,
    validate_assigned_stock_fact,
    with_sqlite_repo_transaction,
    write_lifecycle_case_decision_fact,
)

from .writer_decision import (
    _advance_settlement_admission_head,
    _append_lifecycle_observation_attempt,
    _begin_lifecycle_decision_projection,
    _defer_lifecycle_decision_projection,
    _finish_lifecycle_attempt_cleanup,
    _finish_lifecycle_decision_projection,
    _match_lifecycle_attempt_replay,
    _persist_direct_stock_settlement_evidence,
    _persist_settlement_admission_evidence,
    _prepare_settlement_admission,
    _require_lifecycle_generation,
)

from .writer_lifecycle_support import (
    _allocate_lifecycle_reservation,
    _effective_void_target_ids,
    _lifecycle_notification_transition,
    _lifecycle_state_payload,
    _matching_lifecycle_lots,
    _positive_lifecycle_contracts,
    _require_duplicate_settlement_issue_state,
    _require_settlement_foreign_keys_clean,
    _validate_existing_lifecycle_evidence,
    _validate_existing_zero_price_evidence,
)

def record_assigned_stock_event_atomically(
    repo: Any,
    *,
    sale_event: dict[str, Any],
    assigned_stock_after: dict[str, Any],
) -> dict[str, Any]:
    """Persist one validated sale event and its compact current after-view."""

    event = dict(sale_event or {})
    after = validate_assigned_stock_fact(assigned_stock_after)
    account = str(event.get("account") or "").strip().lower()
    if not account or account != after["account"]:
        raise ValueError("assigned stock event account mismatch")

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError(
                "assigned stock event requires SQLite transaction authority"
            )
        fence = capture_current_decision_projection_fence(
            sqlite_repo,
            accounts=(account,),
            conn=conn,
        )
        begin = fence.accounts[0]
        prior = (
            read_current_assigned_stock_fact(
                sqlite_repo,
                account=account,
                conn=conn,
            )
            if begin.projection_present and begin.clean_at_start
            else None
        )
        created = sqlite_repo.upsert_assigned_stock_event(event, conn=conn)
        if prior is not None:
            stock_lot_id = str(
                event.get("target_stock_lot_id")
                or event.get("stock_lot_id")
                or ""
            ).strip()
            lot_after = next(
                (
                    row
                    for row in after["lots"]
                    if row["stock_lot_id"] == stock_lot_id
                ),
                None,
            )
            expected = (
                update_assigned_stock_fact(
                    prior,
                    transition={
                        "kind": "assigned_stock_sale",
                        "stock_event_id": str(
                            event.get("stock_event_id")
                            or event.get("event_id")
                            or ""
                        ).strip(),
                        "stock_lot_id": stock_lot_id,
                        "shares": event.get("shares"),
                        "trade_time_ms": event.get("trade_time_ms"),
                        "lot_after": lot_after,
                    },
                    current_position_lots=(),
                )
                if created
                else prior
            )
            if expected != after:
                raise ValueError("assigned stock compact after-view mismatch")
        decision_projection = finalize_current_decision_projection(
            sqlite_repo,
            fence=fence,
            updated_at_ms=int(utc_now_ms()),
            conn=conn,
            assigned_stock_after_by_account={account: after},
        )
        return {
            "stock_event_id": str(
                event.get("stock_event_id") or event.get("event_id") or ""
            ).strip(),
            "created": bool(created),
            "decision_projection": decision_projection,
        }

    return with_sqlite_repo_transaction(repo, _run)

def accept_option_close_evidence_atomically(
    repo: Any,
    *,
    contract_identity: dict[str, Any],
    evidence: dict[str, Any],
    apply_changes: bool = True,
) -> dict[str, Any]:
    """Create/reuse one lifecycle_case.v2 and accept zero-price close evidence."""

    identity = dict(contract_identity or {})
    evidence_payload = dict(evidence or {})

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError(
                "option close evidence acceptance requires SQLite transaction authority"
            )
        account = str(identity.get("account") or "").strip().lower()
        futu_account_id = str(
            identity.get("futu_account_id") or ""
        ).strip()
        source_event_id = str(
            evidence_payload.get("source_event_id") or ""
        ).strip()
        evidence_id = str(evidence_payload.get("evidence_id") or "").strip()
        contracts = _positive_lifecycle_contracts(
            evidence_payload.get("contracts")
        )
        expected_source_prefix = f"futu:{account}:{futu_account_id}:"
        if (
            not account
            or not futu_account_id
            or not evidence_id
            or not source_event_id.startswith(expected_source_prefix)
            or source_event_id == expected_source_prefix
        ):
            raise ValueError("canonical_broker_identity_missing")
        if (
            str(evidence_payload.get("evidence_type") or "").strip().lower()
            != "option_zero_price_close"
        ):
            raise ValueError("option close evidence type is invalid")
        try:
            price = Decimal(str(evidence_payload.get("price")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("option close evidence price is invalid") from exc
        if not price.is_finite() or price != 0:
            raise ValueError("option close evidence must have exact zero price")

        contract_key = ContractKey.from_values(
            broker=identity.get("broker"),
            account=account,
            underlying_symbol=identity.get("symbol"),
            option_type=identity.get("option_type"),
            position_side=identity.get("position_side"),
            strike=identity.get("strike"),
            expiration_ymd=identity.get("expiration_ymd"),
        )
        existing_evidence = sqlite_repo.get_trade_lifecycle_evidence(
            evidence_id,
            conn=conn,
        )
        existing_source_claim = (
            sqlite_repo.get_trade_lifecycle_source_consumption(
                source_event_id,
                conn=conn,
            )
        )
        if (
            existing_source_claim is not None
            and str(
                existing_source_claim.get("owner_evidence_id") or ""
            ).strip()
            != evidence_id
        ):
            raise ValueError("lifecycle_source_event_already_consumed")
        if existing_evidence is not None:
            existing_case_id = str(
                existing_evidence.get("case_id") or ""
            ).strip()
            lifecycle_case = sqlite_repo.get_trade_lifecycle_case(
                existing_case_id,
                conn=conn,
            )
            if lifecycle_case is None:
                raise ValueError("lifecycle evidence case is missing")
            bound_futu_account_id = str(
                lifecycle_case.get("futu_account_id") or ""
            ).strip()
            if (
                not bound_futu_account_id
                or bound_futu_account_id != futu_account_id
            ):
                raise ValueError(
                    "lifecycle_case_futu_account_mismatch"
                )
            _validate_existing_zero_price_evidence(
                existing=existing_evidence,
                incoming=evidence_payload,
                contract_key=contract_key,
                contracts=contracts,
            )
            expected_claim = build_source_consumption_claim(
                source_key=source_event_id,
                case_id=existing_case_id,
                owner_evidence_id=evidence_id,
                source_role="option_anchor",
                economic_payload={
                    **identity,
                    **existing_evidence,
                    "account": account,
                    "futu_account_id": futu_account_id,
                },
            )
            if existing_source_claim is None:
                raise ValueError(
                    "lifecycle_source_claim_history_unseeded"
                )
            sqlite_repo.insert_trade_lifecycle_source_consumption_once(
                expected_claim,
                conn=conn,
            )
            return {
                "status": "existing",
                "case_id": existing_case_id,
                "case_created": False,
                "evidence_id": evidence_id,
                "evidence_created": False,
                "broker_evidence_accepted": True,
                "lifecycle_case": lifecycle_case,
                "lifecycle_evidence": existing_evidence,
                "source_claim": expected_claim,
                "source_claim_created": False,
            }

        cases = [
            item
            for item in sqlite_repo.list_trade_lifecycle_cases(
                account=account,
                conn=conn,
            )
            if str(item.get("schema_version") or "").strip()
            == "lifecycle_case.v2"
            and str(item.get("contract_key") or "").strip()
            == contract_key.position_key
        ]
        if len(cases) > 1:
            raise ValueError("multiple_lifecycle_cases_for_contract")
        lifecycle_case = dict(cases[0]) if cases else None
        case_preexisting = lifecycle_case is not None
        position_lots = list(sqlite_repo.list_position_lots(conn=conn))
        matching_lots = _matching_lifecycle_lots(
            position_lots,
            contract_key=contract_key,
        )
        if lifecycle_case is None:
            if not matching_lots:
                raise ValueError("lifecycle_close_target_not_found")
            target_contracts_by_lot = {
                lot_id: remaining
                for lot_id, remaining, _opened_at in matching_lots
            }
            lifecycle_case = {
                **build_lifecycle_case(
                    account=account,
                    broker=contract_key.broker,
                    contract_key=contract_key.position_key,
                    position_side=contract_key.position_side,
                    expiration_ymd=contract_key.expiration_ymd,
                    market=str(identity.get("market") or ""),
                    target_contracts_by_lot=target_contracts_by_lot,
                    futu_account_id=futu_account_id,
                ),
                "market": str(identity.get("market") or "").strip().upper(),
                "symbol": contract_key.underlying_symbol,
                "option_type": contract_key.option_type,
                "strike": contract_key.strike,
                "currency": normalize_currency(identity.get("currency")),
                "multiplier": float(identity.get("multiplier") or 100),
            }
        else:
            bound_futu_account_id = str(
                lifecycle_case.get("futu_account_id") or ""
            ).strip()
            if (
                bound_futu_account_id
                and bound_futu_account_id != futu_account_id
            ):
                raise ValueError(
                    "lifecycle_case_futu_account_mismatch"
                )
            lifecycle_case["futu_account_id"] = futu_account_id
        target_contracts_by_lot = dict(
            lifecycle_case.get("target_contracts_by_lot") or {}
        )
        void_event_ids = _effective_void_target_ids(sqlite_repo, conn=conn)
        allocations = list(
            sqlite_repo.list_trade_lifecycle_allocations(
                case_id=str(lifecycle_case.get("case_id") or ""),
                conn=conn,
            )
        )
        case_evidence = list(
            sqlite_repo.list_trade_lifecycle_evidence(
                case_id=str(lifecycle_case.get("case_id") or ""),
                conn=conn,
            )
        )
        resolution = resolve_allocations(
            target_contracts_by_lot,
            allocations,
            void_event_ids=void_event_ids,
        )
        if resolution.status != "ok":
            raise ValueError(
                "existing lifecycle allocations conflict: "
                + ",".join(resolution.reason_codes)
            )
        evidence_facts = lifecycle_evidence_facts(
            evidence=case_evidence,
            allocations=allocations,
            void_event_ids=void_event_ids,
        )
        for lot_id, expected_remaining in (
            resolution.remaining_contracts_by_lot.items()
        ):
            fields = sqlite_repo.get_position_lot_fields(lot_id, conn=conn)
            if int(fields.get("contracts_open") or 0) != expected_remaining:
                raise ValueError("target_lot_quantity_drift")
        available_by_lot = {
            lot_id: max(
                int(remaining)
                - int(
                    evidence_facts.reservation_contracts_by_lot.get(
                        lot_id,
                        0,
                    )
                ),
                0,
            )
            for lot_id, remaining in resolution.remaining_contracts_by_lot.items()
        }
        evidence_target_manifest = _allocate_lifecycle_reservation(
            contracts=contracts,
            available_by_lot=available_by_lot,
            matching_lots=matching_lots,
        )
        accepted_evidence = {
            **evidence_payload,
            "case_id": str(lifecycle_case.get("case_id") or ""),
            "account": account,
            "symbol": contract_key.underlying_symbol,
            "option_type": contract_key.option_type,
            "position_side": contract_key.position_side,
            "strike": contract_key.strike,
            "expiration_ymd": contract_key.expiration_ymd,
            "contracts": contracts,
            "price": "0",
            "target_contracts_by_lot": evidence_target_manifest,
            "target_lot_id": (
                next(iter(evidence_target_manifest))
                if len(evidence_target_manifest) == 1
                else None
            ),
        }
        case_created = False
        evidence_created = False
        source_claim = build_source_consumption_claim(
            source_key=source_event_id,
            case_id=str(lifecycle_case.get("case_id") or ""),
            owner_evidence_id=evidence_id,
            source_role="option_anchor",
            economic_payload={
                **identity,
                **accepted_evidence,
                "account": account,
                "futu_account_id": futu_account_id,
            },
        )
        source_claim_created = False
        decision_projection: dict[str, Any] | None = None
        if apply_changes:
            decision_fence, prior_decision_fact = (
                _begin_lifecycle_decision_projection(
                    sqlite_repo,
                    conn=conn,
                    lifecycle_case=lifecycle_case,
                    allow_missing_fact=not case_preexisting,
                )
            )
            begin = decision_fence.accounts[0]
            decision_resolution: dict[str, Any] | None = None
            decision_deferred = False
            if begin.projection_present and begin.clean_at_start:
                prior_resolution = (
                    dict(prior_decision_fact["resolution"])
                    if prior_decision_fact is not None
                    else {
                        "status": "missing",
                        "anchor_facts": [],
                        "requested_reservations_by_lot": {},
                        "effective_reservations_by_lot": {},
                        "contested_reason_codes": [],
                    }
                )
                if str(prior_resolution.get("status") or "") not in {
                    "missing",
                    "direct",
                }:
                    decision_deferred = True
                else:
                    decision_resolution = (
                        advance_direct_lifecycle_anchor_resolution(
                            lifecycle_case=lifecycle_case,
                            prior_resolution=prior_resolution,
                            evidence=accepted_evidence,
                            source_claim=source_claim,
                        )
                    )
            if case_preexisting:
                sqlite_repo.bind_trade_lifecycle_case_futu_account_once(
                    case_id=str(
                        lifecycle_case.get("case_id") or ""
                    ),
                    futu_account_id=futu_account_id,
                    conn=conn,
                )
                lifecycle_case = (
                    sqlite_repo.get_trade_lifecycle_case(
                        str(lifecycle_case.get("case_id") or ""),
                        conn=conn,
                    )
                    or lifecycle_case
                )
            case_created = sqlite_repo.insert_trade_lifecycle_case_once(
                lifecycle_case,
                conn=conn,
            )
            if not case_preexisting:
                sqlite_repo.bind_trade_lifecycle_case_futu_account_once(
                    case_id=str(
                        lifecycle_case.get("case_id") or ""
                    ),
                    futu_account_id=futu_account_id,
                    conn=conn,
                )
            evidence_created = sqlite_repo.insert_trade_lifecycle_evidence_once(
                accepted_evidence,
                conn=conn,
            )
            source_claim_created = (
                sqlite_repo.insert_trade_lifecycle_source_consumption_once(
                    source_claim,
                    conn=conn,
                )
            )
            decision_projection = (
                _defer_lifecycle_decision_projection(decision_fence)
                if decision_deferred
                else _finish_lifecycle_decision_projection(
                    sqlite_repo,
                    conn=conn,
                    fence=decision_fence,
                    prior_fact=prior_decision_fact,
                    case_id=str(lifecycle_case.get("case_id") or ""),
                    resolution=decision_resolution,
                )
            )
            sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "status": "accepted" if apply_changes else "dry_run",
            "case_id": str(lifecycle_case.get("case_id") or ""),
            "case_created": case_created,
            "evidence_id": evidence_id,
            "evidence_created": evidence_created,
            "broker_evidence_accepted": bool(apply_changes),
            "lifecycle_case": lifecycle_case,
            "lifecycle_evidence": accepted_evidence,
            "source_claim": source_claim,
            "source_claim_created": source_claim_created,
            "decision_projection": decision_projection,
        }

    return with_sqlite_repo_transaction(repo, _run)

def discover_expired_lifecycle_cases_atomically(
    repo: Any,
    *,
    account: str | None = None,
    observed_at_ms: int | None = None,
    apply_changes: bool = True,
) -> dict[str, Any]:
    """Freeze expired open option lots into lifecycle_case.v2 rows."""

    account_value = str(account or "").strip().lower()
    current_ms = int(
        observed_at_ms
        if observed_at_ms is not None
        else datetime.now(timezone.utc).timestamp() * 1000
    )

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError("lifecycle discovery requires SQLite transaction authority")
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        position_lots = list(sqlite_repo.list_position_lots(conn=conn))
        existing_cases = list(
            sqlite_repo.list_trade_lifecycle_cases(
                account=account_value or None,
                conn=conn,
            )
        )
        target_owner: dict[str, str] = {}
        for lifecycle_case in existing_cases:
            if str(lifecycle_case.get("schema_version") or "").strip() != "lifecycle_case.v2":
                continue
            case_id = str(lifecycle_case.get("case_id") or "").strip()
            target_manifest = dict(lifecycle_case.get("target_contracts_by_lot") or {})
            for lot_id in sorted(str(item or "").strip() for item in target_manifest):
                if not lot_id:
                    raise ValueError("lifecycle case target lot id is invalid")
                previous = target_owner.get(lot_id)
                if previous is not None and previous != case_id:
                    raise ValueError(f"lifecycle_case_target_overlap:{lot_id}")
                target_owner[lot_id] = case_id

        eligible_groups: dict[str, dict[str, Any]] = {}
        skipped_targeted_lot_ids: list[str] = []
        for row in position_lots:
            lot_id = str(row.get("record_id") or "").strip()
            fields = dict(row.get("fields") or {})
            lot_account = str(fields.get("account") or "").strip().lower()
            if account_value and lot_account != account_value:
                continue
            contracts_open = effective_contracts_open(fields)
            if not lot_id or contracts_open <= 0:
                continue
            expiration_ymd = effective_expiration_ymd(fields)
            strike = effective_strike(fields)
            multiplier = effective_multiplier(fields)
            try:
                contract_key = ContractKey.from_values(
                    broker=fields.get("broker"),
                    account=lot_account,
                    underlying_symbol=fields.get("symbol"),
                    option_type=fields.get("option_type"),
                    position_side=fields.get("side"),
                    strike=strike,
                    expiration_ymd=expiration_ymd,
                )
            except (TypeError, ValueError):
                continue
            market = str(symbol_market(contract_key.underlying_symbol) or "").strip().upper()
            observation_start = expiration_observation_start_ms(
                contract_key.expiration_ymd,
                market,
            )
            if observation_start is None:
                try:
                    expired_for_review = date.fromisoformat(
                        contract_key.expiration_ymd
                    ) < datetime.fromtimestamp(current_ms / 1000, tz=timezone.utc).date()
                except ValueError:
                    expired_for_review = False
                if not expired_for_review:
                    continue
            elif current_ms < observation_start:
                continue
            if lot_id in target_owner:
                skipped_targeted_lot_ids.append(lot_id)
                continue
            group = eligible_groups.setdefault(
                contract_key.position_key,
                {
                    "contract_key": contract_key,
                    "market": market,
                    "currency": normalize_currency(fields.get("currency")),
                    "multiplier": float(multiplier or 100.0),
                    "target_contracts_by_lot": {},
                },
            )
            group["target_contracts_by_lot"][lot_id] = contracts_open

        decision_accounts = sorted(
            {
                str(group["contract_key"].account).strip().lower()
                for group in eligible_groups.values()
            }
        )
        decision_fence = (
            capture_current_decision_projection_fence(
                sqlite_repo,
                accounts=decision_accounts,
                conn=conn,
            )
            if apply_changes and decision_accounts
            else None
        )
        clean_decision_accounts = {
            item.account
            for item in (decision_fence.accounts if decision_fence else ())
            if item.projection_present and item.clean_at_start
        }
        decision_mutations: dict[
            str,
            list[tuple[dict[str, Any] | None, dict[str, Any] | None]],
        ] = {}
        created_case_ids: list[str] = []
        would_create_case_ids: list[str] = []
        discovered_case_ids: list[str] = []
        for position_key, group in sorted(eligible_groups.items()):
            contract_key = group["contract_key"]
            lifecycle_case = {
                **build_lifecycle_case(
                    account=contract_key.account,
                    broker=contract_key.broker,
                    contract_key=position_key,
                    position_side=contract_key.position_side,
                    expiration_ymd=contract_key.expiration_ymd,
                    market=group["market"],
                    target_contracts_by_lot=group["target_contracts_by_lot"],
                ),
                "market": group["market"],
                "symbol": contract_key.underlying_symbol,
                "option_type": contract_key.option_type,
                "strike": contract_key.strike,
                "currency": group["currency"],
                "multiplier": group["multiplier"],
            }
            case_id = str(lifecycle_case["case_id"])
            discovered_case_ids.append(case_id)
            if apply_changes:
                created = sqlite_repo.insert_trade_lifecycle_case_once(
                    lifecycle_case,
                    conn=conn,
                )
                if created:
                    created_case_ids.append(case_id)
                    if contract_key.account in clean_decision_accounts:
                        final_case = sqlite_repo.get_trade_lifecycle_case(
                            case_id,
                            conn=conn,
                        )
                        fact_state = (
                            sqlite_repo.get_current_decision_lifecycle_fact_state(
                                case_id,
                                conn=conn,
                            )
                        )
                        if final_case is None or fact_state is None:
                            raise ValueError(
                                "new lifecycle decision fact source disappeared"
                            )
                        final_fact = build_initial_lifecycle_case_decision_fact(
                            lifecycle_case=final_case,
                            fact_state=fact_state,
                        )
                        write_lifecycle_case_decision_fact(
                            sqlite_repo,
                            fact=final_fact,
                            conn=conn,
                        )
                        decision_mutations.setdefault(
                            contract_key.account,
                            [],
                        ).append((None, final_fact))
            else:
                would_create_case_ids.append(case_id)

        refreshed_case_ids: list[str] = []
        would_refresh_case_ids: list[str] = []
        decision_projection = (
            finalize_current_decision_projection(
                sqlite_repo,
                fence=decision_fence,
                updated_at_ms=current_ms,
                conn=conn,
                case_mutations_by_account=decision_mutations,
            )
            if decision_fence is not None
            else None
        )
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "schema_version": "lifecycle_discovery_result.v2",
            "observed_at_ms": current_ms,
            "account": account_value or None,
            "apply_changes": bool(apply_changes),
            "created_case_ids": sorted(created_case_ids),
            "would_create_case_ids": sorted(would_create_case_ids),
            "discovered_case_ids": sorted(discovered_case_ids),
            "refreshed_case_ids": sorted(refreshed_case_ids),
            "would_refresh_case_ids": sorted(would_refresh_case_ids),
            "skipped_targeted_lot_ids": sorted(set(skipped_targeted_lot_ids)),
            "decision_projection": decision_projection,
        }

    return with_sqlite_repo_transaction(repo, _run)

def bind_lifecycle_timing_policy_atomically(
    repo: Any,
    *,
    case_id: str,
    policy: dict[str, Any],
    apply_changes: bool,
) -> dict[str, Any]:
    """Bind one immutable timing policy and its compact case fact."""

    case_id_value = str(case_id or "").strip()
    policy_value = dict(policy or {})

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError("lifecycle timing bind requires SQLite authority")
        lifecycle_case = sqlite_repo.get_trade_lifecycle_case(
            case_id_value,
            conn=conn,
        )
        if lifecycle_case is None:
            raise ValueError(f"lifecycle case not found: {case_id_value}")
        if (
            str(policy_value.get("case_id") or "").strip() != case_id_value
            or str(policy_value.get("market") or "").strip().upper()
            != str(lifecycle_case.get("market") or "").strip().upper()
        ):
            raise ValueError("lifecycle timing policy binding mismatch")
        existing = sqlite_repo.get_trade_lifecycle_timing_policy(
            case_id_value,
            conn=conn,
        )
        if existing is not None and dict(existing) != policy_value:
            raise ValueError(
                f"lifecycle timing policy immutable conflict for case_id={case_id_value}"
            )
        if existing is not None or not apply_changes:
            return {
                "schema_version": "lifecycle_timing_binding_result.v1",
                "case_id": case_id_value,
                "apply_changes": bool(apply_changes),
                "created": False,
                "existing": existing is not None,
                "policy": policy_value,
                "decision_projection": None,
            }
        decision_fence, prior_decision_fact = (
            _begin_lifecycle_decision_projection(
                sqlite_repo,
                conn=conn,
                lifecycle_case=lifecycle_case,
            )
        )
        created = bool(
            sqlite_repo.insert_trade_lifecycle_timing_policy_once(
                policy_value,
                conn=conn,
            )
        )
        if not created:
            raise ValueError("lifecycle timing policy insert was not applied")
        decision_projection = _finish_lifecycle_decision_projection(
            sqlite_repo,
            conn=conn,
            fence=decision_fence,
            prior_fact=prior_decision_fact,
            case_id=case_id_value,
            timing={
                "observation_start_ms": expiration_observation_start_ms(
                    str(lifecycle_case.get("expiration_ymd") or ""),
                    str(lifecycle_case.get("market") or ""),
                ),
                "pending_until_ms": int(
                    policy_value.get("settlement_deadline_ms") or 0
                ),
                "timing_policy_hash": canonical_payload_hash(policy_value),
            },
        )
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "schema_version": "lifecycle_timing_binding_result.v1",
            "case_id": case_id_value,
            "apply_changes": True,
            "created": True,
            "existing": False,
            "policy": policy_value,
            "decision_projection": decision_projection,
        }

    return with_sqlite_repo_transaction(repo, _run)

def record_lifecycle_evidence_issue_atomically(
    repo: Any,
    *,
    case_id: str,
    evidence: dict[str, Any],
    status: str,
    reason_codes: Sequence[str],
    expected_lifecycle_generation_token: str | None = None,
    attempt_evidence: dict[str, Any] | None = None,
    attempt_audit: LifecycleAttemptAuditEnvelope | None = None,
) -> dict[str, Any]:
    """Persist a uniquely matched evidence issue without creating terminal facts."""

    case_id_value = str(case_id or "").strip()
    evidence_payload = dict(evidence or {})
    attempt_evidence_payload = dict(attempt_evidence or {})
    status_value = str(status or "").strip().lower()
    reasons = sorted(
        {
            str(item or "").strip()
            for item in reason_codes
            if str(item or "").strip()
        }
    )
    if status_value not in {"needs_review", "conflict"}:
        raise ValueError("lifecycle evidence issue status must be needs_review or conflict")
    if not reasons:
        raise ValueError("lifecycle evidence issue reason_codes are required")

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError("lifecycle evidence issue requires SQLite transaction authority")
        replay = _match_lifecycle_attempt_replay(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            attempt_audit=attempt_audit,
        )
        if replay is not None:
            return replay
        if attempt_evidence_payload and attempt_audit is None:
            raise ValueError(
                "lifecycle attempt evidence requires an attempt audit"
            )
        _require_settlement_foreign_keys_clean(sqlite_repo, conn=conn)
        lifecycle_case = sqlite_repo.get_trade_lifecycle_case(case_id_value, conn=conn)
        if lifecycle_case is None:
            raise ValueError(f"lifecycle case not found: {case_id_value}")
        _require_lifecycle_generation(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            expected_generation_token=(
                expected_lifecycle_generation_token
            ),
        )
        decision_fence, prior_decision_fact = (
            _begin_lifecycle_decision_projection(
                sqlite_repo,
                conn=conn,
                lifecycle_case=lifecycle_case,
            )
        )
        admission = _prepare_settlement_admission(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            evidence=(
                attempt_evidence_payload or evidence_payload
            ),
            expected_generation_token=(
                expected_lifecycle_generation_token
            ),
        )
        if attempt_audit is not None and admission is None:
            raise ValueError(
                "lifecycle evidence issue attempt audit requires observation admission"
            )
        if (
            not attempt_evidence_payload
            and admission is not None
            and bool(admission.get("duplicate"))
        ):
            duplicate_state = _require_duplicate_settlement_issue_state(
                sqlite_repo,
                conn=conn,
                lifecycle_case=lifecycle_case,
                admission=admission,
                requested_status=status_value,
                requested_reasons=reasons,
            )
            prior_summary = duplicate_state["summary"]
            audit_result = _append_lifecycle_observation_attempt(
                sqlite_repo,
                conn=conn,
                attempt_audit=attempt_audit,
                admission=admission,
            )
            decision_projection = _finish_lifecycle_decision_projection(
                sqlite_repo,
                conn=conn,
                fence=decision_fence,
                prior_fact=prior_decision_fact,
                case_id=case_id_value,
                publish_case=bool(admission.get("head_repaired")),
            )
            return {
                "case_id": case_id_value,
                "evidence_id": admission["evidence_id"],
                "evidence_created": False,
                "evidence_bound": False,
                "status": str(
                    lifecycle_case.get("status") or status_value
                ),
                "reason_codes": list(
                    prior_summary.get("lifecycle_reason_codes") or []
                ),
                "status_changed": False,
                "source_claim_created": False,
                "resolution_revision": int(
                    prior_summary.get("resolution_revision") or 0
                ),
                "state_fingerprint": str(
                    prior_summary.get("state_fingerprint") or ""
                ),
                "business_state_changed": False,
                "notification_outbox_id": None,
                "notification_outbox_created": False,
                "notification_audit_codes": list(
                    prior_summary.get("notification_audit_codes") or []
                ),
                "terminal_event_ids": [],
                "allocation_ids": [],
                "admission_status": "duplicate_semantic",
                "semantic_fingerprint": admission[
                    "semantic_fingerprint"
                ],
                "decision_projection": decision_projection,
                **audit_result,
            }
        if attempt_evidence_payload:
            _persist_settlement_admission_evidence(
                sqlite_repo,
                conn=conn,
                case_id=case_id_value,
                evidence=attempt_evidence_payload,
                admission=admission,
            )
        evidence_id = str(evidence_payload.get("evidence_id") or "").strip()
        if not evidence_id:
            raise ValueError("lifecycle evidence_id is required")
        if evidence_payload.get("case_id") not in (None, "", case_id_value):
            raise ValueError("lifecycle evidence is bound to another case")
        existing = sqlite_repo.get_trade_lifecycle_evidence(evidence_id, conn=conn)
        if existing is None:
            evidence_created = sqlite_repo.insert_trade_lifecycle_evidence_once(
                evidence_payload,
                conn=conn,
            )
        else:
            _validate_existing_lifecycle_evidence(
                existing=existing,
                incoming=evidence_payload,
                case_id=case_id_value,
            )
            evidence_created = False
        allocations = list(
            sqlite_repo.list_trade_lifecycle_allocations(
                case_id=case_id_value,
                conn=conn,
            )
        )
        if any(
            str(item.get("evidence_id") or "").strip() == evidence_id
            for item in allocations
        ):
            raise ValueError("allocated lifecycle evidence cannot be reclassified as an issue")
        evidence_bound = sqlite_repo.bind_trade_lifecycle_evidence_case_once(
            evidence_id=evidence_id,
            case_id=case_id_value,
            conn=conn,
        )
        requires_broker_claims = (
            str(
                evidence_payload.get("source_type") or ""
            ).strip().lower()
            == "broker_settlement_pair"
            or bool(evidence_payload.get("source_evidence_ids"))
        )
        source_claim_created = False
        if requires_broker_claims:
            existing_claims = list(
                sqlite_repo.list_trade_lifecycle_source_consumptions(
                    case_id=case_id_value,
                    conn=conn,
                )
            )
            if not any(
                str(item.get("source_role") or "").strip().lower()
                == "option_anchor"
                for item in existing_claims
            ):
                raise ValueError(
                    "lifecycle_option_anchor_claim_missing"
                )
            stock = (
                dict(evidence_payload.get("stock_settlement") or {})
                if isinstance(
                    evidence_payload.get("stock_settlement"),
                    dict,
                )
                else {}
            )
            if str(stock.get("source_event_id") or "").strip():
                claim = build_source_consumption_claim(
                    source_key=str(stock["source_event_id"]),
                    case_id=case_id_value,
                    owner_evidence_id=evidence_id,
                    source_role="stock_settlement",
                    economic_payload={
                        "account": lifecycle_case.get("account"),
                        "futu_account_id": stock.get(
                            "futu_account_id"
                        ),
                        "symbol": stock.get("symbol")
                        or lifecycle_case.get("symbol"),
                        "side": stock.get("side"),
                        "shares": stock.get("shares"),
                        "price": stock.get("price"),
                        "execution_time_ms": stock.get(
                            "event_time_ms"
                        ),
                        "order_id": stock.get("order_id"),
                        "clearing_date": stock.get("clearing_date"),
                    },
                )
                source_claim_created = (
                    sqlite_repo.insert_trade_lifecycle_source_consumption_once(
                        claim,
                        conn=conn,
                    )
                )
        resolution = resolve_allocations(
            lifecycle_case.get("target_contracts_by_lot"),
            allocations,
            void_event_ids=_effective_void_target_ids(sqlite_repo, conn=conn),
        )
        prior_summary = dict(lifecycle_case.get("derived_summary") or {})
        prior_conflicts = list(prior_summary.get("conflict_evidence_ids") or [])
        void_event_ids = _effective_void_target_ids(
            sqlite_repo,
            conn=conn,
        )
        new_summary = {
            **prior_summary,
            "target_contracts_by_lot": resolution.target_contracts_by_lot,
            "resolved_contracts_by_lot": resolution.resolved_contracts_by_lot,
            "remaining_contracts_by_lot": (
                resolution.remaining_contracts_by_lot
            ),
            "resolved_contracts_by_terminal_type": (
                resolution.resolved_contracts_by_terminal_type
            ),
            "lifecycle_reason_codes": reasons,
            "conflict_evidence_ids": sorted(
                set(prior_conflicts + [evidence_id])
            ),
        }
        projected_remaining = {
            lot_id: int(
                sqlite_repo.get_position_lot_fields(
                    lot_id,
                    conn=conn,
                ).get("contracts_open")
                or 0
            )
            for lot_id in sorted(
                dict(
                    lifecycle_case.get("target_contracts_by_lot") or {}
                )
            )
        }
        state_fingerprint = canonical_state_fingerprint(
            _lifecycle_state_payload(
                lifecycle_case=lifecycle_case,
                evidence_rows=(
                    sqlite_repo.list_trade_lifecycle_evidence(
                        case_id=case_id_value,
                        conn=conn,
                    )
                ),
                source_claims=(
                    sqlite_repo.list_trade_lifecycle_source_consumptions(
                        case_id=case_id_value,
                        conn=conn,
                    )
                ),
                allocations=allocations,
                void_event_ids=void_event_ids,
                projected_remaining_by_lot=projected_remaining,
                status=status_value,
                summary=new_summary,
            )
        )
        prior_fingerprint = str(
            prior_summary.get("state_fingerprint") or ""
        ).strip()
        business_state_changed = (
            state_fingerprint != prior_fingerprint
        )
        resolution_revision = int(
            prior_summary.get("resolution_revision") or 0
        ) + int(business_state_changed)
        if resolution_revision <= 0:
            raise ValueError("lifecycle resolution revision is invalid")
        transition_type, transition_key = (
            _lifecycle_notification_transition(
                case_id=case_id_value,
                status=status_value,
            )
        )
        notification_intent = build_notification_intent(
            case_id=case_id_value,
            transition_type=transition_type,
            resolution_revision=resolution_revision,
            delivery_revision=0,
            transition_key=transition_key,
            state_fingerprint=state_fingerprint,
            payload={
                "schema_version": "trade_lifecycle_notification.v1",
                "case_id": case_id_value,
                "transition_type": transition_type,
                "resolution_revision": resolution_revision,
                "state_fingerprint": state_fingerprint,
                "account": lifecycle_case.get("account"),
                "symbol": lifecycle_case.get("symbol"),
                "option_type": lifecycle_case.get("option_type"),
                "position_side": lifecycle_case.get("position_side"),
                "strike": lifecycle_case.get("strike"),
                "expiration_ymd": lifecycle_case.get(
                    "expiration_ymd"
                ),
                "reason_codes": reasons,
                "evidence_id": evidence_id,
            },
        )
        notification_audit_codes = list(
            prior_summary.get("notification_audit_codes") or []
        )
        existing_transition = (
            sqlite_repo.get_trade_lifecycle_notification_by_transition(
                transition_key=transition_key,
                delivery_revision=0,
                conn=conn,
            )
        )
        outbox_created = False
        if business_state_changed:
            if (
                existing_transition is not None
                and (
                    str(
                        existing_transition.get("state_fingerprint")
                        or ""
                    )
                    != state_fingerprint
                    or str(existing_transition.get("payload_hash") or "")
                    != str(notification_intent.get("payload_hash") or "")
                )
            ):
                notification_audit_codes = sorted(
                    set(
                        notification_audit_codes
                        + ["notification_transition_conflict"]
                    )
                )
            else:
                outbox_created = (
                    sqlite_repo.insert_trade_lifecycle_notification_once(
                        notification_intent,
                        conn=conn,
                    )
                )
        new_summary.update(
            {
                "resolution_revision": resolution_revision,
                "state_fingerprint": state_fingerprint,
                "notification_audit_codes": (
                    notification_audit_codes
                ),
            }
        )
        status_changed = sqlite_repo.update_trade_lifecycle_case_derived_status(
            case_id=case_id_value,
            status=status_value,
            derived_summary=new_summary,
            expected_state_fingerprint=prior_fingerprint,
            conn=conn,
        )
        _advance_settlement_admission_head(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            admission=admission,
        )
        audit_result = _append_lifecycle_observation_attempt(
            sqlite_repo,
            conn=conn,
            attempt_audit=attempt_audit,
            admission=admission,
        )
        decision_projection = _finish_lifecycle_decision_projection(
            sqlite_repo,
            conn=conn,
            fence=decision_fence,
            prior_fact=prior_decision_fact,
            case_id=case_id_value,
        )
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "case_id": case_id_value,
            "evidence_id": evidence_id,
            "evidence_created": evidence_created,
            "evidence_bound": evidence_bound,
            "status": status_value,
            "reason_codes": reasons,
            "status_changed": status_changed,
            "source_claim_created": source_claim_created,
            "resolution_revision": resolution_revision,
            "state_fingerprint": state_fingerprint,
            "business_state_changed": business_state_changed,
            "notification_outbox_id": notification_intent["outbox_id"],
            "notification_outbox_created": outbox_created,
            "notification_audit_codes": notification_audit_codes,
            "terminal_event_ids": [],
            "allocation_ids": [],
            "admission_status": (
                "admitted_semantic"
                if admission is not None
                else "not_applicable"
            ),
            "semantic_fingerprint": (
                admission.get("semantic_fingerprint")
                if admission is not None
                else None
            ),
            "decision_projection": decision_projection,
            **audit_result,
        }

    return _finish_lifecycle_attempt_cleanup(
        repo,
        with_sqlite_repo_transaction(repo, _run),
    )

def record_lifecycle_attempt_audit_atomically(
    repo: Any,
    *,
    attempt_audit: LifecycleAttemptAuditEnvelope,
) -> dict[str, Any]:
    """Persist one provider failure/stale attempt without business mutation."""

    if attempt_audit.outcome_code in (1, 2):
        raise ValueError(
            "audit-only lifecycle writer accepts only failed or stale attempts"
        )

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError(
                "lifecycle attempt audit requires SQLite transaction authority"
            )
        return sqlite_repo.append_trade_lifecycle_attempt_audit_in_transaction(
            attempt_audit=attempt_audit,
            conn=conn,
        )

    return _finish_lifecycle_attempt_cleanup(
        repo,
        with_sqlite_repo_transaction(repo, _run),
    )

def record_lifecycle_observation_attempt_atomically(
    repo: Any,
    *,
    case_id: str,
    evidence: dict[str, Any],
    expected_lifecycle_generation_token: str,
    attempt_audit: LifecycleAttemptAuditEnvelope,
    direct_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Admit one provider observation without a business transition."""

    case_id_value = str(case_id or "").strip()
    evidence_payload = dict(evidence or {})
    direct_evidence_payload = dict(direct_evidence or {})

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError(
                "lifecycle observation attempt requires SQLite authority"
            )
        replay = _match_lifecycle_attempt_replay(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            attempt_audit=attempt_audit,
        )
        if replay is not None:
            return replay
        _require_settlement_foreign_keys_clean(sqlite_repo, conn=conn)
        lifecycle_case = sqlite_repo.get_trade_lifecycle_case(
            case_id_value,
            conn=conn,
        )
        if lifecycle_case is None:
            raise ValueError(f"lifecycle case not found: {case_id_value}")
        _require_lifecycle_generation(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            expected_generation_token=(
                expected_lifecycle_generation_token
            ),
        )
        decision_fence, prior_decision_fact = (
            _begin_lifecycle_decision_projection(
                sqlite_repo,
                conn=conn,
                lifecycle_case=lifecycle_case,
            )
        )
        admission = _prepare_settlement_admission(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            evidence=evidence_payload,
            expected_generation_token=(
                expected_lifecycle_generation_token
            ),
        )
        if admission is None:
            raise ValueError(
                "lifecycle observation attempt requires observation admission"
            )
        evidence_created, evidence_bound = (
            _persist_settlement_admission_evidence(
                sqlite_repo,
                conn=conn,
                case_id=case_id_value,
                evidence=evidence_payload,
                admission=admission,
            )
        )
        direct_evidence_created = (
            _persist_direct_stock_settlement_evidence(
                sqlite_repo,
                conn=conn,
                evidence=direct_evidence_payload,
            )
            if direct_evidence_payload
            else False
        )
        _advance_settlement_admission_head(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            admission=admission,
        )
        audit_result = _append_lifecycle_observation_attempt(
            sqlite_repo,
            conn=conn,
            attempt_audit=attempt_audit,
            admission=admission,
        )
        decision_projection = _finish_lifecycle_decision_projection(
            sqlite_repo,
            conn=conn,
            fence=decision_fence,
            prior_fact=prior_decision_fact,
            case_id=case_id_value,
            publish_case=(
                not bool(admission.get("duplicate"))
                or bool(admission.get("head_repaired"))
            ),
        )
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "case_id": case_id_value,
            "evidence_id": admission["evidence_id"],
            "evidence_created": evidence_created,
            "evidence_bound": evidence_bound,
            "direct_evidence_created": direct_evidence_created,
            "admission_status": (
                "duplicate_semantic"
                if bool(admission.get("duplicate"))
                else "admitted_semantic"
            ),
            "semantic_fingerprint": admission[
                "semantic_fingerprint"
            ],
            "decision_projection": decision_projection,
            **audit_result,
        }

    return _finish_lifecycle_attempt_cleanup(
        repo,
        with_sqlite_repo_transaction(repo, _run),
    )
