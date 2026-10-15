"""Atomic Wheel commands; application adapters do not own their transactions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.wheel import (
    plan_wheel_manual_end, plan_wheel_branch_decision, build_legacy_wheel_event, build_wheel_event,
    project_wheel_linkage_candidates,
)
from src.application.ledger.assigned_stock_projection import (
    resolve_wheel_batch_from_rows, resolve_wheel_branch_from_rows, build_wheel_read_model_from_rows,
)
from src.application.ledger.repository import with_sqlite_repo_transaction
from src.application.write_contract import attach_write_contract
from domain.domain.wheel.config import (
    require_wheel_activation, wheel_activation_request_hash,
    resolve_wheel_activation_request, wheel_activation_window_descriptor,
)

from domain.domain.risk_capacity import revalidate_opening_share_coverage
from domain.domain.portfolio_scope import portfolio_scope_id
from domain.domain.wheel import (
    plan_wheel_call_intent_cancel, plan_wheel_call_intent_create,
    plan_wheel_put_intent_cancel, plan_wheel_put_intent_create,
    project_wheel_call_intents, project_wheel_intents,
)
from src.application.portfolio_context_service import evaluate_account_cash_snapshot, cash_snapshot_is_usable
from src.application.candidate_snapshot_contract import sha256_text
from src.application.ledger.decision_snapshot import decision_state_snapshot_from_locked_rows
from src.application.ledger.position_context import build_position_risk_context as build_option_positions_context
from src.application.ledger.wheel_capacity import (
    revalidate_selected_wheel_put_candidate_from_rows, broker_capacity_observation_is_fresh,
)
from src.application.ledger.assigned_stock_projection import (
    resolve_wheel_batch_from_rows as _wheel_batch,
    resolve_wheel_branch_from_rows as _wheel_branch,
)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _require_market(value: str) -> str:
    market = str(value or "").strip().lower()
    if market not in {"us", "hk"}:
        raise ValueError("Wheel workflow market must be us or hk")
    return market


def decide_wheel_branch(
    repo: Any,
    *,
    account: str,
    wheel_branch_id: str,
    decision: str,
    expected_batch_generation_hash: str,
    request_id: str,
    actor: str,
    market: str,
    activation_descriptor: Mapping[str, Any] | None = None,
    account_configured: bool | None = None,
    policy_sha256: str | None = None,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    branch_id = str(wheel_branch_id or "").strip()
    decision_value = str(decision or "").strip().lower()
    expected = str(expected_batch_generation_hash or "").strip()
    request_value = str(request_id or "").strip()
    actor_value = str(actor or "").strip()
    market_value = _require_market(market)
    instant = int(as_of_ms or _now_ms())
    if (
        not all((account_value, branch_id, expected, request_value, actor_value))
        or decision_value not in {"start", "end"}
        or instant <= 0
    ):
        raise ValueError("Wheel branch decision requires complete request fields")

    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        rows = sqlite_repo.read_lifecycle_account_rows(
            account=account_value,
            conn=conn,
        )
        existing = [
            event
            for event in rows.get("account_wheel_events") or []
            if str(event.get("wheel_branch_id") or "").strip() == branch_id
            and event.get("event_type") == "wheel_branch_decided"
            and str((event.get("payload") or {}).get("request_id") or "").strip()
            == request_value
        ]
        if len(existing) > 1:
            raise ValueError("Wheel branch decision request identity is not unique")
        if existing:
            event = existing[0]
            payload = event.get("payload") or {}
            if (
                str(payload.get("decision") or "") != decision_value
                or str(payload.get("actor") or "") != actor_value
                or str(payload.get("expected_generation_hash") or "") != expected
                or str(payload.get("market") or "").strip().lower()
                != market_value
            ):
                raise ValueError("Wheel branch decision request identity conflicts")
            projected = resolve_wheel_branch_from_rows(
                rows,
                account=account_value,
                wheel_branch_id=branch_id,
                as_of_ms=max(instant, int(event["occurred_at_ms"])),
                market=market_value,
            )
            expected_status = "active" if decision_value == "start" else "manual_ended"
            if projected["lifecycle_status"] != expected_status:
                raise ValueError("Wheel branch decision idempotency verification failed")
            return _branch_decision_result(
                event,
                status="idempotent",
                status_after=expected_status,
                dry_run=not apply_changes,
                write_applied=False,
            )
        branch = resolve_wheel_branch_from_rows(
            rows,
            account=account_value,
            wheel_branch_id=branch_id,
            as_of_ms=instant,
            market=market_value,
        )
        if branch["batch_generation_hash"] != expected:
            raise ValueError("Wheel branch generation changed; refresh before confirming")
        if decision_value == "start":
            require_wheel_activation(
                durable_window=sqlite_repo.get_current_wheel_activation_window(
                    market=market_value, account=account_value, conn=conn,
                ),
                market=market_value,
                branch=branch,
                activation_descriptor=activation_descriptor,
                account_configured=account_configured is True,
                policy_sha256=str(policy_sha256 or ''),
            )
        event = plan_wheel_branch_decision(
            branch,
            decision_value,
            request_value,
            actor_value,
            expected,
            occurred_at_ms=instant,
            recorded_at_ms=instant,
        )
        if not apply_changes:
            return _branch_decision_result(
                event,
                status="planned",
                status_after=("active" if decision_value == "start" else "manual_ended"),
                dry_run=True,
                write_applied=False,
            )
        if not sqlite_repo.append_wheel_event_once(event, conn=conn):
            raise ValueError("Wheel branch decision append unexpectedly replayed")
        projected = resolve_wheel_branch_from_rows(
            sqlite_repo.read_lifecycle_account_rows(
                account=account_value,
                conn=conn,
            ),
            account=account_value,
            wheel_branch_id=branch_id,
            as_of_ms=instant,
            market=market_value,
        )
        expected_status = "active" if decision_value == "start" else "manual_ended"
        if projected["lifecycle_status"] != expected_status:
            raise ValueError("Wheel branch decision projection verification failed")
        return _branch_decision_result(
            event,
            status="decided",
            status_after=expected_status,
            dry_run=False,
            write_applied=True,
        )

    return with_sqlite_repo_transaction(repo, _run)


def change_wheel_activation_window(
    repo: Any, *, action: str, market: str, account: str,
    expected_current_generation: int, request_id: str, actor: str, policy_sha256: str,
) -> dict[str, Any]:
    if action not in {"enable", "disable"}:
        raise ValueError("Wheel activation action must be enable or disable")
    if market not in {"us", "hk"} or not account or account != account.strip().lower():
        raise ValueError("Wheel activation requires market and lowercase account")
    if not str(request_id or "").strip() or not str(actor or "").strip():
        raise ValueError("Wheel activation requires request_id and actor")
    request_hash = wheel_activation_request_hash(
        action=action, market=market, account=account,
        expected_current_generation=expected_current_generation, request_id=request_id,
        actor=actor, policy_sha256=policy_sha256,
    )

    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        windows = sqlite_repo.list_wheel_activation_windows(market=market, account=account, conn=conn)
        current = sqlite_repo.get_current_wheel_activation_window(market=market, account=account, conn=conn)
        if current is not None:
            windows[-1] = current
        resolve_wheel_activation_request(
            windows, action=action, request_id=request_id, request_hash=request_hash,
            policy_sha256=policy_sha256, expected_generation=expected_current_generation,
        )
        method = sqlite_repo.open_wheel_activation_window if action == "enable" else sqlite_repo.close_wheel_activation_window
        result = method(
            market=market, account=account, expected_current_generation=expected_current_generation,
            policy_hash=policy_sha256, request_id=request_id, request_hash=request_hash, conn=conn,
        )
        persisted = resolve_wheel_activation_request(
            sqlite_repo.list_wheel_activation_windows(market=market, account=account, conn=conn),
            action=action, request_id=request_id, request_hash=request_hash,
            policy_sha256=policy_sha256, expected_generation=expected_current_generation,
        )
        if persisted is None or persisted != result["window"]:
            raise ValueError("Wheel activation window readback verification failed")
        return wheel_activation_result(
            action=action, status="idempotent" if result["idempotent"] else "applied",
            market=market, account=account, request_id=request_id, actor=actor,
            request_hash=request_hash, window=result["window"], dry_run=False,
            write_applied=bool(result["write_applied"]), idempotent=bool(result["idempotent"]),
        )

    return with_sqlite_repo_transaction(repo, _run)


def end_wheel_lifecycle(
    repo: Any,
    *,
    account: str,
    lot_id: str,
    expected_batch_generation_hash: str,
    request_id: str,
    actor: str,
    market: str,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    stock_lot_value = str(lot_id or "").strip()
    expected_generation = str(expected_batch_generation_hash or "").strip()
    request_value = str(request_id or "").strip()
    actor_value = str(actor or "").strip()
    market_value = _require_market(market)
    instant = int(as_of_ms or _now_ms())
    if not all(
        (account_value, stock_lot_value, expected_generation, request_value, actor_value)
    ) or instant <= 0:
        raise ValueError("Wheel manual end requires complete request fields")

    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        rows = sqlite_repo.read_lifecycle_account_rows(
            account=account_value,
            conn=conn,
        )
        existing = [
            event
            for event in rows.get("account_wheel_events") or []
            if str(event.get("stock_lot_id") or "").strip() == stock_lot_value
            and str(event.get("event_type") or "").strip() == "wheel_manual_ended"
            and str((event.get("payload") or {}).get("request_id") or "").strip()
            == request_value
        ]
        if len(existing) > 1:
            raise ValueError("Wheel manual end request identity is not unique")
        if existing:
            event = existing[0]
            payload = event.get("payload") or {}
            if (
                str(payload.get("actor") or "").strip() != actor_value
                or str(payload.get("batch_generation_hash") or "").strip()
                != expected_generation
                or str(payload.get("market") or "").strip().lower()
                != market_value
            ):
                raise ValueError("Wheel manual end request identity conflicts")
            batch = resolve_wheel_batch_from_rows(
                rows,
                account=account_value,
                lot_id=stock_lot_value,
                as_of_ms=max(instant, int(event.get("occurred_at_ms") or 0)),
                market=market_value,
            )
            if (
                batch["lifecycle_status"] != "manual_ended"
                or batch["phase"] is not None
                or batch["terminal_event_id"] != event["event_id"]
            ):
                raise ValueError("Wheel manual end idempotency verification failed")
            return _manual_end_result(
                event=event,
                generation=expected_generation,
                status_before="active",
                status_after="manual_ended",
                dry_run=not apply_changes,
                write_applied=False,
                idempotent=True,
            )

        batch = resolve_wheel_batch_from_rows(
            rows,
            account=account_value,
            lot_id=stock_lot_value,
            as_of_ms=instant,
            market=market_value,
        )
        if batch["batch_generation_hash"] != expected_generation:
            raise ValueError("Wheel batch generation changed; refresh before confirming")
        event = plan_wheel_manual_end(
            batch,
            request_value,
            actor_value,
            occurred_at_ms=instant,
            recorded_at_ms=instant,
            account=account_value,
        )
        if not apply_changes:
            return _manual_end_result(
                event=event,
                generation=expected_generation,
                status_before=batch["lifecycle_status"],
                status_after="manual_ended",
                dry_run=True,
                write_applied=False,
                idempotent=False,
            )

        created = sqlite_repo.append_wheel_event_once(event, conn=conn)
        projected = resolve_wheel_batch_from_rows(
            sqlite_repo.read_lifecycle_account_rows(
                account=account_value,
                conn=conn,
            ),
            account=account_value,
            lot_id=stock_lot_value,
            as_of_ms=instant,
            market=market_value,
        )
        if (
            not created
            or projected["lifecycle_status"] != "manual_ended"
            or projected["phase"] is not None
            or projected["terminal_event_id"] != event["event_id"]
        ):
            raise ValueError("Wheel manual end projection verification failed")
        return _manual_end_result(
            event=event,
            generation=expected_generation,
            status_before=batch["lifecycle_status"],
            status_after=projected["lifecycle_status"],
            dry_run=False,
            write_applied=True,
            idempotent=False,
        )

    return with_sqlite_repo_transaction(repo, _run)


def _intent_summaries(
    rows: Mapping[str, Any],
    *,
    account: str,
    lot_id: str,
    as_of_ms: int,
) -> list[dict[str, Any]]:
    return project_wheel_call_intents(
        rows.get("account_wheel_events") or [],
        account=account,
        lot_id=lot_id,
        as_of_ms=as_of_ms,
        known_trade_event_ids={
            str(item.get("event_id") or "").strip()
            for item in rows.get("trade_events") or []
            if str(item.get("event_id") or "").strip()
        },
    )


def _snapshot_final_candidate(
    candidate_snapshot: Mapping[str, Any],
    *,
    current_strategy_policy_sha256: str,
    account: str,
    lot_id: str | None = None,
    wheel_branch_id: str | None = None,
    direction: str = "call",
    final_candidate_id: str,
    expected_snapshot_hash: str,
    expected_batch_generation_hash: str | None = None,
) -> dict[str, Any]:
    if candidate_snapshot.get("strategy_policy_sha256") != sha256_text(
        current_strategy_policy_sha256, "current_strategy_policy_sha256"
    ):
        raise ValueError("stale_snapshot: Wheel candidate strategy policy changed or unavailable")
    if str(candidate_snapshot.get("snapshot_hash") or "").strip() != expected_snapshot_hash:
        raise ValueError("stale_snapshot: Wheel candidate snapshot hash changed")
    if str(candidate_snapshot.get("account") or "").strip().lower() != account:
        raise ValueError("stale_snapshot: Wheel candidate snapshot account mismatch")
    branch_id = str(wheel_branch_id or "").strip()
    stock_lot_value = str(lot_id or "").strip()
    direction_value = str(direction or "").strip().lower()
    expected_generation = str(
        expected_batch_generation_hash or ""
    ).strip()
    matches = [
        item
        for item in (
            candidate_snapshot.get("batches")
            or candidate_snapshot.get("rows")
            or []
        )
        if isinstance(item, Mapping)
        and (
            (
                branch_id
                and str(
                    item.get("wheel_branch_id") or item.get("stock_lot_id") or ""
                ).strip()
                == branch_id
            )
            or (
                not branch_id
                and stock_lot_value
                and str(item.get("stock_lot_id") or "").strip() == stock_lot_value
            )
        )
        and str(item.get("direction") or "call").strip().lower() == direction_value
    ]
    if len(matches) != 1:
        raise ValueError("stale_snapshot: Wheel candidate batch is not unique")
    batch_snapshot = matches[0]
    if (
        str(
            batch_snapshot.get("batch_generation_hash")
            or batch_snapshot.get("branch_generation_hash")
            or ""
        ).strip()
        != expected_generation
    ):
        raise ValueError("stale_snapshot: Wheel candidate batch generation changed")
    candidate = batch_snapshot.get("final_candidate")
    if not isinstance(candidate, Mapping) or str(
        candidate.get("final_candidate_id") or candidate.get("candidate_id") or ""
    ).strip() != final_candidate_id:
        raise ValueError("stale_snapshot: Wheel final candidate changed")
    return {
        **dict(candidate),
        "account": account,
        "wheel_branch_id": branch_id or stock_lot_value,
        "stock_lot_id": stock_lot_value or None,
        "direction": direction_value,
        "snapshot_hash": expected_snapshot_hash,
    }


def create_wheel_call_intent(
    repo: Any,
    *,
    candidate_snapshot: Mapping[str, Any],
    current_strategy_policy_sha256: str,
    account: str,
    lot_id: str,
    final_candidate_id: str,
    expected_snapshot_hash: str,
    expected_batch_generation_hash: str,
    expires_at_ms: int,
    request_id: str,
    actor: str,
    coverage_fact: Mapping[str, Any],
    new_intent_enabled: bool,
    account_configured: bool,
    market: str,
    activation_descriptor: Mapping[str, Any] | None,
    policy_sha256: str,
    broker_order_id: str | None = None,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    return _create_wheel_call_intent(
        repo,
        candidate_snapshot=candidate_snapshot,
        current_strategy_policy_sha256=current_strategy_policy_sha256,
        account=account,
        lot_id=lot_id,
        final_candidate_id=final_candidate_id,
        expected_snapshot_hash=expected_snapshot_hash,
        expected_batch_generation_hash=expected_batch_generation_hash,
        expires_at_ms=expires_at_ms,
        request_id=request_id,
        actor=actor,
        coverage_fact=coverage_fact,
        new_intent_enabled=new_intent_enabled,
        account_configured=account_configured,
        market=market,
        activation_descriptor=activation_descriptor,
        policy_sha256=policy_sha256,
        broker_order_id=broker_order_id,
        apply_changes=apply_changes,
        as_of_ms=as_of_ms,
    )


def cancel_wheel_call_intent(
    repo: Any,
    *,
    account: str,
    lot_id: str,
    intent_id: str,
    expected_batch_generation_hash: str,
    request_id: str,
    actor: str,
    broker_order_inactive_confirmed: bool,
    reason: str,
    market: str,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    return _cancel_wheel_call_intent(
        repo,
        account=account,
        lot_id=lot_id,
        intent_id=intent_id,
        expected_batch_generation_hash=expected_batch_generation_hash,
        request_id=request_id,
        actor=actor,
        broker_order_inactive_confirmed=broker_order_inactive_confirmed,
        reason=reason,
        market=market,
        apply_changes=apply_changes,
        as_of_ms=as_of_ms,
    )


def _linkage_candidate(
    model: Mapping[str, Any],
    *,
    call_lot_id: str,
    lot_id: str,
    linkage_candidate_id: str,
    expected_input_hash: str,
    expected_batch_generation_hash: str,
) -> dict[str, Any]:
    matches = [
        item
        for item in model.get("linkage_candidates") or []
        if item.get("call_record_id") == call_lot_id
        and item.get("stock_lot_id") == lot_id
        and item.get("linkage_candidate_id") == linkage_candidate_id
    ]
    if len(matches) != 1:
        raise ValueError("Wheel Call linkage candidate is stale or unavailable")
    candidate = matches[0]
    if (
        candidate["input_snapshot_hash"] != expected_input_hash
        or candidate["batch_generation_hash"] != expected_batch_generation_hash
    ):
        raise ValueError("Wheel Call linkage candidate input changed")
    return candidate


def reject_wheel_call_linkage(
    repo: Any,
    *,
    account: str,
    call_lot_id: str,
    lot_id: str,
    linkage_candidate_id: str,
    expected_input_hash: str,
    expected_batch_generation_hash: str,
    request_id: str,
    actor: str,
    reason: str,
    market: str,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    return _reject_wheel_call_linkage(
        repo, account=account, call_lot_id=call_lot_id, lot_id=lot_id,
        linkage_candidate_id=linkage_candidate_id, expected_input_hash=expected_input_hash,
        expected_batch_generation_hash=expected_batch_generation_hash,
        request_id=request_id, actor=actor, reason=reason, market=market,
        apply_changes=apply_changes, as_of_ms=as_of_ms,
    )


def _canonical_wheel_result(
    result: Mapping[str, Any],
    *,
    schema_version: str,
    wheel_branch_id: str,
    direction: str,
    option_lot_id: str | None = None,
) -> dict[str, Any]:
    return {
        **dict(result),
        "schema_version": schema_version,
        "wheel_branch_id": wheel_branch_id,
        "direction": direction,
        **(
            {"option_record_id": option_lot_id}
            if option_lot_id is not None
            else {}
        ),
    }


def _wheel_linkage_candidate(
    rows: Mapping[str, Any],
    model: Mapping[str, Any],
    *,
    option_lot_id: str,
    wheel_branch_id: str,
    direction: str,
    linkage_candidate_id: str,
    expected_input_hash: str,
    expected_batch_generation_hash: str,
) -> dict[str, Any]:
    matches = [
        item
        for item in project_wheel_linkage_candidates(
            model.get("wheel_branches") or [],
            rows.get("account_position_lots") or [],
            rows.get("account_wheel_events") or [],
        )
        if item.get("option_record_id") == option_lot_id
        and item.get("wheel_branch_id") == wheel_branch_id
        and item.get("direction") == direction
        and item.get("linkage_candidate_id") == linkage_candidate_id
    ]
    if len(matches) != 1:
        raise ValueError("Wheel linkage candidate is stale or unavailable")
    candidate = matches[0]
    if (
        candidate.get("input_snapshot_hash") != expected_input_hash
        or candidate.get("batch_generation_hash")
        != expected_batch_generation_hash
    ):
        raise ValueError("Wheel linkage candidate input changed")
    return candidate


def _wheel_linkage_result(
    *,
    status: str,
    event_id: str,
    option_lot_id: str,
    wheel_branch_id: str,
    direction: str,
    request_id: str,
    market: str,
    dry_run: bool,
    write_applied: bool,
    intent_event_id: str | None = None,
) -> dict[str, Any]:
    return attach_write_contract(
        {
            "schema_version": "wheel_linkage_result.v1",
            "status": status,
            "event_id": event_id,
            "option_record_id": option_lot_id,
            "wheel_branch_id": wheel_branch_id,
            "direction": direction,
            "request_id": request_id,
            "market": market,
            "intent_event_id": intent_event_id,
        },
        dry_run=dry_run,
        write_applied=write_applied,
        audit_id=event_id,
    )


def _put_portfolio_context(capacity_fact: Mapping[str, Any]) -> dict[str, Any]:
    return {**dict(capacity_fact.get("cash_evidence") or {}),
            "cash_snapshot": capacity_fact.get("cash_snapshot")}


def _bound_put_capacity_fact(
    intent: Mapping[str, Any],
    capacity_fact: Mapping[str, Any],
) -> dict[str, Any]:
    payload = intent.get("payload")
    payload = payload if isinstance(payload, Mapping) else intent
    return {
        **dict(capacity_fact),
        "capacity_identity_hash": payload.get("capacity_identity_hash"),
        "cash_reservation_amount": payload.get("cash_reservation_amount"),
        "cash_reservation_currency": payload.get("cash_reservation_currency"),
    }


def _create_wheel_put_intent(
    repo: Any,
    *,
    candidate_snapshot: Mapping[str, Any],
    current_strategy_policy_sha256: str,
    account: str,
    wheel_branch_id: str,
    final_candidate_id: str,
    expected_snapshot_hash: str,
    expected_batch_generation_hash: str,
    expires_at_ms: int,
    request_id: str,
    actor: str,
    capacity_fact: Mapping[str, Any],
    runtime_config: Mapping[str, Any],
    new_intent_enabled: bool,
    account_configured: bool,
    market: str,
    activation_descriptor: Mapping[str, Any] | None,
    policy_sha256: str,
    broker_order_id: str | None,
    apply_changes: bool,
    as_of_ms: int,
) -> dict[str, Any]:
    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        rows = sqlite_repo.read_lifecycle_account_rows(account=account, conn=conn)
        existing = [
            event
            for event in rows.get("account_wheel_events") or []
            if event.get("event_type") == "wheel_put_intent_created"
            and str((event.get("payload") or {}).get("request_id") or "").strip()
            == request_id
        ]
        if len(existing) > 1:
            raise ValueError("Wheel Put intent request identity is not unique")
        if existing:
            event = existing[0]
            payload = event.get("payload") or {}
            if (
                event.get("wheel_branch_id") != wheel_branch_id
                or str(payload.get("actor") or "") != actor
                or str(payload.get("final_candidate_id") or "")
                != final_candidate_id
                or str(payload.get("snapshot_hash") or "") != expected_snapshot_hash
                or str(payload.get("batch_generation_hash") or "")
                != expected_batch_generation_hash
                or int(payload.get("expires_at_ms") or 0) != int(expires_at_ms)
                or str(payload.get("broker_order_id") or "")
                != str(broker_order_id or "").strip()
                or str(payload.get("market") or "").strip().lower() != market
            ):
                raise ValueError("Wheel Put intent request identity conflicts")
            return _canonical_wheel_result(
                _intent_result(
                    event,
                    status="idempotent",
                    dry_run=not apply_changes,
                    write_applied=False,
                ),
                schema_version="wheel_intent_result.v1",
                wheel_branch_id=wheel_branch_id,
                direction="put",
            )
        if not new_intent_enabled:
            raise ValueError("wheel_disabled: new Wheel Put intents are disabled")
        branch = _wheel_branch(
            rows,
            account=account,
            wheel_branch_id=wheel_branch_id,
            as_of_ms=as_of_ms,
            market=market,
        )
        require_wheel_activation(
            durable_window=sqlite_repo.get_current_wheel_activation_window(
                market=market, account=account, conn=conn,
            ),
            market=market,
            branch=branch,
            activation_descriptor=activation_descriptor,
            account_configured=account_configured,
            policy_sha256=policy_sha256,
        )
        if branch.get("direction") != "put":
            raise ValueError("Wheel branch direction mismatch")
        if branch.get("batch_generation_hash") != expected_batch_generation_hash:
            raise ValueError("stale_snapshot: Wheel branch generation changed")
        candidate = _snapshot_final_candidate(
            candidate_snapshot,
            current_strategy_policy_sha256=current_strategy_policy_sha256,
            account=account,
            wheel_branch_id=wheel_branch_id,
            direction="put",
            final_candidate_id=final_candidate_id,
            expected_snapshot_hash=expected_snapshot_hash,
            expected_batch_generation_hash=expected_batch_generation_hash,
        )
        summaries = project_wheel_intents(
            rows.get("account_wheel_events") or [],
            account=account,
            wheel_branch_id=wheel_branch_id,
            direction="put",
            as_of_ms=as_of_ms,
            known_trade_event_ids={
                str(item.get("event_id") or "").strip()
                for item in rows.get("trade_events") or []
                if str(item.get("event_id") or "").strip()
            },
        )
        order_id = str(broker_order_id or "").strip()
        if order_id and any(
            item.get("status") == "active"
            and str((item.get("payload") or {}).get("broker_order_id") or "")
            == order_id
            for item in summaries
        ):
            raise ValueError("broker_order_id already belongs to an active Wheel intent")
        portfolio = _put_portfolio_context(capacity_fact)
        confirmed_cash = evaluate_account_cash_snapshot(
            portfolio, config=runtime_config, account=account,
            evaluated_at=datetime.now(timezone.utc),
        )
        if confirmed_cash["max_age_sec"] != portfolio.get("max_age_sec"):
            raise ValueError("Wheel Put cash policy changed; repreview required")
        portfolio["cash_snapshot"] = confirmed_cash
        if not cash_snapshot_is_usable(portfolio):
            raise ValueError("Wheel Put cash evidence stale or unavailable; repreview required")
        decision_snapshot = decision_state_snapshot_from_locked_rows(
            sqlite_repo, rows,
            account=account,
            portfolio_scope_id=portfolio_scope_id(account),
            source_observed_at=datetime.now(timezone.utc).isoformat(),
            current_decision_now_ms=as_of_ms,
        )
        current_capacity = revalidate_selected_wheel_put_candidate_from_rows(
            account=account,
            portfolio_context=portfolio,
            position_lots=sqlite_repo.list_position_lots(conn=conn),
            lifecycle_rows=rows,
            broker=str(
                candidate.get("broker")
                or branch.get("broker")
                or capacity_fact.get("broker")
                or "futu"
            ),
            as_of_ms=as_of_ms,
            decision_snapshot=decision_snapshot,
            fx_snapshot=(
                capacity_fact.get("fx_snapshot")
                if isinstance(capacity_fact.get("fx_snapshot"), Mapping)
                else {}
            ),
            final_candidate=candidate,
            opening_put_candidates=[
                dict(item)
                for item in candidate_snapshot.get("opening_put_candidates") or []
                if isinstance(item, Mapping)
            ],
        )
        event = plan_wheel_put_intent_create(
            branch,
            candidate,
            current_capacity,
            expires_at_ms,
            request_id,
            actor,
            occurred_at_ms=as_of_ms,
            recorded_at_ms=as_of_ms,
            broker_order_id=order_id or None,
        )
        if not apply_changes:
            return _canonical_wheel_result(
                _intent_result(
                    event,
                    status="planned",
                    dry_run=True,
                    write_applied=False,
                ),
                schema_version="wheel_intent_result.v1",
                wheel_branch_id=wheel_branch_id,
                direction="put",
            )
        if not sqlite_repo.append_wheel_event_once(event, conn=conn):
            raise ValueError("Wheel Put intent append unexpectedly replayed")
        projected = _wheel_branch(
            sqlite_repo.read_lifecycle_account_rows(account=account, conn=conn),
            account=account,
            wheel_branch_id=wheel_branch_id,
            as_of_ms=as_of_ms,
            market=market,
        )
        if (
            projected.get("phase") != "intent_pending"
            or event["intent_id"] not in projected.get("active_intent_ids", [])
        ):
            raise ValueError("Wheel Put intent projection verification failed")
        return _canonical_wheel_result(
            _intent_result(
                event,
                status="created",
                dry_run=False,
                write_applied=True,
            ),
            schema_version="wheel_intent_result.v1",
            wheel_branch_id=wheel_branch_id,
            direction="put",
        )

    return with_sqlite_repo_transaction(repo, _run)


def create_wheel_intent(
    repo: Any,
    *,
    candidate_snapshot: Mapping[str, Any],
    current_strategy_policy_sha256: str,
    account: str,
    wheel_branch_id: str,
    direction: str,
    final_candidate_id: str,
    expected_snapshot_hash: str,
    expected_batch_generation_hash: str,
    expires_at_ms: int,
    request_id: str,
    actor: str,
    capacity_fact: Mapping[str, Any],
    new_intent_enabled: bool,
    account_configured: bool,
    market: str,
    activation_descriptor: Mapping[str, Any] | None,
    policy_sha256: str,
    runtime_config: Mapping[str, Any] | None = None,
    broker_order_id: str | None = None,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    branch_id = str(wheel_branch_id or "").strip()
    direction_value = str(direction or "").strip().lower()
    market_value = _require_market(market)
    instant = int(as_of_ms or _now_ms())
    if direction_value == "put":
        return _create_wheel_put_intent(
            repo,
            candidate_snapshot=candidate_snapshot,
            current_strategy_policy_sha256=current_strategy_policy_sha256,
            account=account_value,
            wheel_branch_id=branch_id,
            final_candidate_id=str(final_candidate_id or "").strip(),
            expected_snapshot_hash=str(expected_snapshot_hash or "").strip(),
            expected_batch_generation_hash=str(
                expected_batch_generation_hash or ""
            ).strip(),
            expires_at_ms=int(expires_at_ms),
            request_id=str(request_id or "").strip(),
            actor=str(actor or "").strip(),
            capacity_fact=capacity_fact,
            runtime_config=runtime_config or {},
            new_intent_enabled=new_intent_enabled,
            account_configured=account_configured,
            market=market_value,
            activation_descriptor=activation_descriptor,
            policy_sha256=policy_sha256,
            broker_order_id=broker_order_id,
            apply_changes=apply_changes,
            as_of_ms=instant,
        )
    if direction_value != "call" or not branch_id:
        raise ValueError("Wheel direction must be call or put and branch is required")
    result = _create_wheel_call_intent(
        repo,
        candidate_snapshot=candidate_snapshot,
        current_strategy_policy_sha256=current_strategy_policy_sha256,
        account=account_value,
        lot_id="",
        wheel_branch_id=branch_id,
        final_candidate_id=final_candidate_id,
        expected_snapshot_hash=expected_snapshot_hash,
        expected_batch_generation_hash=expected_batch_generation_hash,
        expires_at_ms=expires_at_ms,
        request_id=request_id,
        actor=actor,
        coverage_fact=capacity_fact,
        new_intent_enabled=new_intent_enabled,
        account_configured=account_configured,
        market=market_value,
        activation_descriptor=activation_descriptor,
        policy_sha256=policy_sha256,
        broker_order_id=broker_order_id,
        apply_changes=apply_changes,
        as_of_ms=instant,
    )
    return _canonical_wheel_result(
        result,
        schema_version="wheel_intent_result.v1",
        wheel_branch_id=branch_id,
        direction=direction_value,
    )


def cancel_wheel_intent(
    repo: Any,
    *,
    account: str,
    wheel_branch_id: str,
    direction: str,
    intent_id: str,
    expected_batch_generation_hash: str,
    request_id: str,
    actor: str,
    broker_order_inactive_confirmed: bool,
    reason: str,
    market: str,
    capacity_fact: Mapping[str, Any] | None = None,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    branch_id = str(wheel_branch_id or "").strip()
    direction_value = str(direction or "").strip().lower()
    market_value = _require_market(market)
    instant = int(as_of_ms or _now_ms())
    if direction_value == "put":
        if not isinstance(capacity_fact, Mapping):
            raise ValueError("Wheel Put intent cancellation requires cash capacity fact")

        def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
            rows = sqlite_repo.read_lifecycle_account_rows(
                account=account_value,
                conn=conn,
            )
            existing = [
                event
                for event in rows.get("account_wheel_events") or []
                if event.get("event_type") == "wheel_put_intent_cancelled"
                and event.get("intent_id") == intent_id
                and str((event.get("payload") or {}).get("request_id") or "")
                == request_id
            ]
            if len(existing) > 1:
                raise ValueError("Wheel Put intent cancellation identity is not unique")
            if existing:
                payload = existing[0].get("payload") or {}
                if (
                    existing[0].get("wheel_branch_id") != branch_id
                    or str(payload.get("actor") or "") != actor
                    or str(payload.get("reason") or "") != reason
                    or str(payload.get("batch_generation_hash") or "")
                    != expected_batch_generation_hash
                    or payload.get("broker_order_inactive_confirmed") is not True
                    or str(payload.get("market") or "").strip().lower()
                    != market_value
                ):
                    raise ValueError("Wheel Put intent cancellation identity conflicts")
                return _canonical_wheel_result(
                    _intent_result(
                        existing[0],
                        status="idempotent",
                        dry_run=not apply_changes,
                        write_applied=False,
                    ),
                    schema_version="wheel_intent_result.v1",
                    wheel_branch_id=branch_id,
                    direction="put",
                )
            branch = _wheel_branch(
                rows,
                account=account_value,
                wheel_branch_id=branch_id,
                as_of_ms=instant,
                market=market_value,
            )
            if branch.get("direction") != "put":
                raise ValueError("Wheel branch direction mismatch")
            if branch.get("batch_generation_hash") != expected_batch_generation_hash:
                raise ValueError(
                    "Wheel branch generation changed; refresh before confirming"
                )
            matches = [
                item
                for item in project_wheel_intents(
                    rows.get("account_wheel_events") or [],
                    account=account_value,
                    wheel_branch_id=branch_id,
                    direction="put",
                    as_of_ms=instant,
                    known_trade_event_ids={
                        str(item.get("event_id") or "").strip()
                        for item in rows.get("trade_events") or []
                        if str(item.get("event_id") or "").strip()
                    },
                )
                if item["intent_id"] == intent_id
            ]
            if len(matches) != 1:
                raise ValueError("Wheel Put intent must resolve uniquely")
            event = plan_wheel_put_intent_cancel(
                branch,
                matches[0],
                _bound_put_capacity_fact(matches[0], capacity_fact),
                request_id,
                actor,
                broker_order_inactive_confirmed,
                reason,
                occurred_at_ms=instant,
                recorded_at_ms=instant,
            )
            if event is None:
                return attach_write_contract(
                    {
                        "schema_version": "wheel_intent_result.v1",
                        "status": "already_inactive",
                        "intent_id": intent_id,
                        "wheel_branch_id": branch_id,
                        "direction": "put",
                        "market": market_value,
                    },
                    dry_run=not apply_changes,
                    write_applied=False,
                )
            if not apply_changes:
                return _canonical_wheel_result(
                    _intent_result(
                        event,
                        status="planned",
                        dry_run=True,
                        write_applied=False,
                    ),
                    schema_version="wheel_intent_result.v1",
                    wheel_branch_id=branch_id,
                    direction="put",
                )
            if not sqlite_repo.append_wheel_event_once(event, conn=conn):
                raise ValueError("Wheel Put intent cancellation unexpectedly replayed")
            after_rows = sqlite_repo.read_lifecycle_account_rows(
                account=account_value,
                conn=conn,
            )
            after = project_wheel_intents(
                after_rows.get("account_wheel_events") or [],
                account=account_value,
                wheel_branch_id=branch_id,
                direction="put",
                as_of_ms=instant,
                known_trade_event_ids={
                    str(item.get("event_id") or "").strip()
                    for item in after_rows.get("trade_events") or []
                    if str(item.get("event_id") or "").strip()
                },
            )
            if any(
                item["intent_id"] == intent_id and item["status"] == "active"
                for item in after
            ):
                raise ValueError("Wheel Put intent cancellation verification failed")
            return _canonical_wheel_result(
                _intent_result(
                    event,
                    status="cancelled",
                    dry_run=False,
                    write_applied=True,
                ),
                schema_version="wheel_intent_result.v1",
                wheel_branch_id=branch_id,
                direction="put",
            )

        return with_sqlite_repo_transaction(repo, _run)
    if direction_value != "call" or not branch_id:
        raise ValueError("Wheel direction must be call or put and branch is required")
    result = _cancel_wheel_call_intent(
        repo,
        account=account_value,
        lot_id="",
        wheel_branch_id=branch_id,
        intent_id=intent_id,
        expected_batch_generation_hash=expected_batch_generation_hash,
        request_id=request_id,
        actor=actor,
        broker_order_inactive_confirmed=broker_order_inactive_confirmed,
        reason=reason,
        market=market_value,
        apply_changes=apply_changes,
        as_of_ms=instant,
    )
    return _canonical_wheel_result(
        result,
        schema_version="wheel_intent_result.v1",
        wheel_branch_id=branch_id,
        direction=direction_value,
    )


def reject_wheel_linkage(
    repo: Any,
    *,
    account: str,
    option_lot_id: str,
    wheel_branch_id: str,
    direction: str,
    linkage_candidate_id: str,
    expected_input_hash: str,
    expected_batch_generation_hash: str,
    request_id: str,
    actor: str,
    reason: str,
    market: str,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    branch_id = str(wheel_branch_id or "").strip()
    direction_value = str(direction or "").strip().lower()
    market_value = _require_market(market)
    instant = int(as_of_ms or _now_ms())
    if direction_value == "put":
        return _reject_wheel_put_linkage(
            repo, account=account_value, option_lot_id=option_lot_id,
            wheel_branch_id=branch_id, linkage_candidate_id=linkage_candidate_id,
            expected_input_hash=expected_input_hash,
            expected_batch_generation_hash=expected_batch_generation_hash,
            request_id=request_id, actor=actor, reason=reason, market=market_value,
            apply_changes=apply_changes, as_of_ms=instant,
        )
    if direction_value != "call":
        raise ValueError("Wheel direction must be call or put")
    if not branch_id:
        raise ValueError("Wheel Call linkage rejection requires complete fields")
    result = _reject_wheel_call_linkage(
        repo,
        account=account_value,
        call_lot_id=option_lot_id,
        lot_id="",
        wheel_branch_id=branch_id,
        linkage_candidate_id=linkage_candidate_id,
        expected_input_hash=expected_input_hash,
        expected_batch_generation_hash=expected_batch_generation_hash,
        request_id=request_id,
        actor=actor,
        reason=reason,
        market=market_value,
        apply_changes=apply_changes,
        as_of_ms=instant,
    )
    return {
        **result,
        "schema_version": "wheel_linkage_result.v1",
        "wheel_branch_id": branch_id,
        "direction": direction_value,
        "option_record_id": option_lot_id,
    }


def _linkage_result(
    *,
    status: str,
    event_id: str,
    call_lot_id: str,
    lot_id: str,
    request_id: str,
    market: str,
    dry_run: bool,
    write_applied: bool,
    intent_event_id: str | None = None,
) -> dict[str, Any]:
    return attach_write_contract(
        {
            "schema_version": "wheel_call_linkage_result.v1",
            "status": status,
            "event_id": event_id,
            "call_record_id": call_lot_id,
            "stock_lot_id": lot_id,
            "request_id": request_id,
            "market": market,
            "intent_event_id": intent_event_id,
        },
        dry_run=dry_run,
        write_applied=write_applied,
        audit_id=event_id,
    )


def wheel_activation_result(
    *,
    action: str,
    status: str,
    market: str,
    account: str,
    request_id: str,
    actor: str,
    request_hash: str,
    window: Mapping[str, Any] | None,
    dry_run: bool,
    write_applied: bool,
    idempotent: bool,
) -> dict[str, Any]:
    audit_id = f"wheel-activation:{market}:{account}:{request_id}"
    return attach_write_contract(
        {
            "schema_version": "wheel_activation_result.v1",
            "status": status,
            "action": action,
            "market": market,
            "account": account,
            "request_id": request_id,
            "actor": actor,
            "request_hash": request_hash,
            "expected_config_descriptor": wheel_activation_window_descriptor(window),
            "idempotent": idempotent,
        },
        dry_run=dry_run,
        write_applied=write_applied,
        audit_id=audit_id,
    )


def _branch_decision_result(
    event: Mapping[str, Any],
    *,
    status: str,
    status_after: str,
    dry_run: bool,
    write_applied: bool,
) -> dict[str, Any]:
    payload = event.get("payload") or {}
    return attach_write_contract(
        {
            "schema_version": "wheel_branch_decision_result.v1",
            "status": status,
            "decision": payload.get("decision"),
            "wheel_branch_id": event.get("wheel_branch_id"),
            "event_id": event.get("event_id"),
            "request_id": payload.get("request_id"),
            "market": payload.get("market"),
            "expected_batch_generation_hash": payload.get(
                "expected_generation_hash"
            ),
            "lifecycle_status_after": status_after,
        },
        dry_run=dry_run,
        write_applied=write_applied,
        audit_id=str(event.get("event_id") or "wheel-branch-decision"),
    )


def _intent_result(
    event: Mapping[str, Any],
    *,
    status: str,
    dry_run: bool,
    write_applied: bool,
) -> dict[str, Any]:
    return attach_write_contract(
        {
            "schema_version": "wheel_call_intent_result.v1",
            "status": status,
            "event_id": event["event_id"],
            "intent_id": event["intent_id"],
            "stock_lot_id": event["stock_lot_id"],
            "request_id": str((event.get("payload") or {}).get("request_id") or "")
            or None,
            "market": (event.get("payload") or {}).get("market"),
        },
        dry_run=dry_run,
        write_applied=write_applied,
        audit_id=event["event_id"],
    )


def _manual_end_result(
    *,
    event: Mapping[str, Any],
    generation: str,
    status_before: str,
    status_after: str,
    dry_run: bool,
    write_applied: bool,
    idempotent: bool,
) -> dict[str, Any]:
    return attach_write_contract(
        {
            "schema_version": "wheel_end_result.v1",
            "stock_lot_id": event["stock_lot_id"],
            "event_id": event["event_id"],
            "request_id": str((event.get("payload") or {}).get("request_id") or ""),
            "market": (event.get("payload") or {}).get("market"),
            "batch_generation_hash": generation,
            "lifecycle_status_before": status_before,
            "lifecycle_status_after": status_after,
            "idempotent": idempotent,
        },
        dry_run=dry_run,
        write_applied=write_applied,
        audit_id=event["event_id"],
        rollback_hint=(
            "use the controlled wheel_event_voided repair path for an incorrect fact"
            if write_applied
            else None
        ),
    )


def _reject_wheel_call_linkage(
    repo: Any,
    *,
    account: str,
    call_lot_id: str,
    lot_id: str,
    linkage_candidate_id: str,
    expected_input_hash: str,
    expected_batch_generation_hash: str,
    request_id: str,
    actor: str,
    reason: str,
    market: str,
    wheel_branch_id: str | None = None,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    values = {
        "account": str(account or "").strip().lower(),
        "call_record_id": str(call_lot_id or "").strip(),
        "stock_lot_id": str(lot_id or "").strip(),
        "linkage_candidate_id": str(linkage_candidate_id or "").strip(),
        "expected_input_hash": str(expected_input_hash or "").strip(),
        "expected_batch_generation_hash": str(
            expected_batch_generation_hash or ""
        ).strip(),
        "request_id": str(request_id or "").strip(),
        "actor": str(actor or "").strip(),
        "reason": str(reason or "").strip(),
        "market": _require_market(market),
    }
    instant = int(as_of_ms or _now_ms())
    if not all(value for key, value in values.items() if key != "stock_lot_id") or not (
        values["stock_lot_id"] or wheel_branch_id
    ):
        raise ValueError("Wheel Call linkage rejection requires complete fields")

    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        rows = sqlite_repo.read_lifecycle_account_rows(account=values["account"], conn=conn)
        model = None
        if wheel_branch_id is not None:
            model = build_wheel_read_model_from_rows(
                rows, account=values["account"], as_of_ms=instant, market=values["market"],
            )
            branches = [item for item in model["wheel_branches"]
                        if item["wheel_branch_id"] == wheel_branch_id]
            if len(branches) != 1:
                raise ValueError(f"Wheel branch must resolve uniquely: wheel_branch_id={wheel_branch_id}")
            branch = branches[0]
            if branch.get("direction") != "call":
                raise ValueError("Wheel branch direction mismatch")
            values["stock_lot_id"] = str(branch.get("stock_lot_id") or "").strip()
            if not values["stock_lot_id"]:
                raise ValueError("Wheel Call branch has no stock lot")
        # A prior rejection changes the batch generation itself. Validate its
        # immutable request before checking current candidate inputs below.
        existing = [
            item
            for item in rows.get("account_wheel_events") or []
            if item.get("event_type") == "wheel_call_linkage_rejected"
            and str((item.get("payload") or {}).get("request_id") or "")
            == values["request_id"]
        ]
        if len(existing) > 1:
            raise ValueError("Wheel Call linkage rejection identity is not unique")
        if existing:
            event = existing[0]
            payload = event.get("payload") or {}
            if (
                event.get("stock_lot_id") != values["stock_lot_id"]
                or str(payload.get("call_record_id") or "")
                != values["call_record_id"]
                or str(payload.get("linkage_candidate_id") or "")
                != values["linkage_candidate_id"]
                or str(payload.get("input_snapshot_hash") or "")
                != values["expected_input_hash"]
                or str(payload.get("batch_generation_hash") or "")
                != values["expected_batch_generation_hash"]
                or str(payload.get("actor") or "") != values["actor"]
                or str(payload.get("reason") or "") != values["reason"]
                or str(payload.get("market") or "").strip().lower()
                != values["market"]
            ):
                raise ValueError("Wheel Call linkage rejection identity conflicts")
            return _linkage_result(
                status="idempotent",
                event_id=event["event_id"],
                call_lot_id=values["call_record_id"],
                lot_id=values["stock_lot_id"],
                request_id=values["request_id"],
                market=values["market"],
                dry_run=not apply_changes,
                write_applied=False,
            )
        if model is None:
            model = build_wheel_read_model_from_rows(
                rows, account=values["account"], as_of_ms=instant, market=values["market"],
            )
        candidate = _linkage_candidate(
            model,
            call_lot_id=values["call_record_id"],
            lot_id=values["stock_lot_id"],
            linkage_candidate_id=values["linkage_candidate_id"],
            expected_input_hash=values["expected_input_hash"],
            expected_batch_generation_hash=values["expected_batch_generation_hash"],
        )
        batch = next(
            item
            for item in model["batches"]
            if item["stock_lot_id"] == values["stock_lot_id"]
        )
        digest = canonical_sha256(
            {
                "account": values["account"],
                "call_record_id": values["call_record_id"],
                "stock_lot_id": values["stock_lot_id"],
                "request_id": values["request_id"],
            }
        )[:24]
        event_builder = (
            build_legacy_wheel_event
            if batch.get("legacy_call_adapter")
            else build_wheel_event
        )
        event = event_builder(
            event_id=f"wheel-call-linkage-rejected:{digest}",
            account=values["account"],
            lot_id=values["stock_lot_id"],
            event_type="wheel_call_linkage_rejected",
            occurred_at_ms=instant,
            recorded_at_ms=instant,
            source_trade_event_id=candidate["call_open_event_id"],
            payload={
                "schema_version": "wheel_call_linkage_rejected.v1",
                "market": values["market"],
                "call_record_id": values["call_record_id"],
                "call_open_event_id": candidate["call_open_event_id"],
                "linkage_candidate_id": values["linkage_candidate_id"],
                "input_snapshot_hash": values["expected_input_hash"],
                "batch_generation_hash": values["expected_batch_generation_hash"],
                "request_id": values["request_id"],
                "actor": values["actor"],
                "reason": values["reason"],
            },
        )
        if not apply_changes:
            return _linkage_result(
                status="planned",
                event_id=event["event_id"],
                call_lot_id=values["call_record_id"],
                lot_id=values["stock_lot_id"],
                request_id=values["request_id"],
                market=values["market"],
                dry_run=True,
                write_applied=False,
            )
        if not sqlite_repo.append_wheel_event_once(event, conn=conn):
            raise ValueError("Wheel Call linkage rejection unexpectedly replayed")
        after = build_wheel_read_model_from_rows(
            sqlite_repo.read_lifecycle_account_rows(account=values["account"], conn=conn),
            account=values["account"],
            as_of_ms=instant,
            market=values["market"],
        )
        if any(
            item["linkage_candidate_id"] == values["linkage_candidate_id"]
            for item in after["linkage_candidates"]
        ):
            raise ValueError("Wheel Call linkage rejection verification failed")
        return _linkage_result(
            status="rejected",
            event_id=event["event_id"],
            call_lot_id=values["call_record_id"],
            lot_id=values["stock_lot_id"],
            request_id=values["request_id"],
            market=values["market"],
            dry_run=False,
            write_applied=True,
        )

    return with_sqlite_repo_transaction(repo, _run)


def _reject_wheel_put_linkage(
    repo: Any,
    *,
    account: str,
    option_lot_id: str,
    wheel_branch_id: str,
    linkage_candidate_id: str,
    expected_input_hash: str,
    expected_batch_generation_hash: str,
    request_id: str,
    actor: str,
    reason: str,
    market: str,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    branch_id = str(wheel_branch_id or "").strip()
    market_value = _require_market(market)
    instant = int(as_of_ms or _now_ms())
    values = {
        "account": account_value,
        "option_record_id": str(option_lot_id or "").strip(),
        "wheel_branch_id": branch_id,
        "linkage_candidate_id": str(linkage_candidate_id or "").strip(),
        "expected_input_hash": str(expected_input_hash or "").strip(),
        "expected_batch_generation_hash": str(
            expected_batch_generation_hash or ""
        ).strip(),
        "request_id": str(request_id or "").strip(),
        "actor": str(actor or "").strip(),
        "reason": str(reason or "").strip(),
        "market": market_value,
    }
    if not all(values.values()):
        raise ValueError("Wheel Put linkage rejection requires complete fields")

    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        rows = sqlite_repo.read_lifecycle_account_rows(
            account=account_value,
            conn=conn,
        )
        existing = [
            item
            for item in rows.get("account_wheel_events") or []
            if item.get("event_type") == "wheel_put_linkage_rejected"
            and str((item.get("payload") or {}).get("request_id") or "")
            == values["request_id"]
        ]
        if len(existing) > 1:
            raise ValueError("Wheel Put linkage rejection identity is not unique")
        if existing:
            event = existing[0]
            payload = event.get("payload") or {}
            if (
                event.get("wheel_branch_id") != branch_id
                or str(payload.get("option_record_id") or "")
                != values["option_record_id"]
                or str(payload.get("linkage_candidate_id") or "")
                != values["linkage_candidate_id"]
                or str(payload.get("input_snapshot_hash") or "")
                != values["expected_input_hash"]
                or str(payload.get("batch_generation_hash") or "")
                != values["expected_batch_generation_hash"]
                or str(payload.get("actor") or "") != values["actor"]
                or str(payload.get("reason") or "") != values["reason"]
                or str(payload.get("market") or "").strip().lower()
                != market_value
            ):
                raise ValueError("Wheel Put linkage rejection identity conflicts")
            return _wheel_linkage_result(
                status="idempotent",
                event_id=str(event["event_id"]),
                option_lot_id=values["option_record_id"],
                wheel_branch_id=branch_id,
                direction="put",
                request_id=values["request_id"],
                market=market_value,
                dry_run=not apply_changes,
                write_applied=False,
            )

        model = build_wheel_read_model_from_rows(
            rows,
            account=account_value,
            as_of_ms=instant,
            market=market_value,
        )
        candidate = _wheel_linkage_candidate(
            rows,
            model,
            option_lot_id=values["option_record_id"],
            wheel_branch_id=values["wheel_branch_id"],
            direction="put",
            linkage_candidate_id=values["linkage_candidate_id"],
            expected_input_hash=values["expected_input_hash"],
            expected_batch_generation_hash=values[
                "expected_batch_generation_hash"
            ],
        )
        digest = canonical_sha256(
            {
                "account": account_value,
                "option_record_id": values["option_record_id"],
                "wheel_branch_id": branch_id,
                "request_id": values["request_id"],
            }
        )[:24]
        event = build_wheel_event(
            event_id=f"wheel-put-linkage-rejected:{digest}",
            account=account_value,
            lot_id=None,
            wheel_branch_id=branch_id,
            event_type="wheel_put_linkage_rejected",
            occurred_at_ms=instant,
            recorded_at_ms=instant,
            source_trade_event_id=candidate["option_open_event_id"],
            payload={
                "schema_version": "wheel_put_linkage_rejected.v1",
                "market": market_value,
                "direction": "put",
                "option_record_id": values["option_record_id"],
                "option_open_event_id": candidate["option_open_event_id"],
                "linkage_candidate_id": values["linkage_candidate_id"],
                "input_snapshot_hash": values["expected_input_hash"],
                "batch_generation_hash": values[
                    "expected_batch_generation_hash"
                ],
                "request_id": values["request_id"],
                "actor": values["actor"],
                "reason": values["reason"],
            },
        )
        if not apply_changes:
            return _wheel_linkage_result(
                status="planned",
                event_id=event["event_id"],
                option_lot_id=values["option_record_id"],
                wheel_branch_id=branch_id,
                direction="put",
                request_id=values["request_id"],
                market=market_value,
                dry_run=True,
                write_applied=False,
            )
        if not sqlite_repo.append_wheel_event_once(event, conn=conn):
            raise ValueError("Wheel Put linkage rejection unexpectedly replayed")
        after_rows = sqlite_repo.read_lifecycle_account_rows(
            account=account_value,
            conn=conn,
        )
        after_model = build_wheel_read_model_from_rows(
            after_rows,
            account=account_value,
            as_of_ms=instant,
            market=market_value,
        )
        remaining = project_wheel_linkage_candidates(
            after_model.get("wheel_branches") or [],
            after_rows.get("account_position_lots") or [],
            after_rows.get("account_wheel_events") or [],
        )
        if any(
            item.get("linkage_candidate_id")
            == values["linkage_candidate_id"]
            for item in remaining
        ):
            raise ValueError("Wheel Put linkage rejection verification failed")
        return _wheel_linkage_result(
            status="rejected",
            event_id=event["event_id"],
            option_lot_id=values["option_record_id"],
            wheel_branch_id=branch_id,
            direction="put",
            request_id=values["request_id"],
            market=market_value,
            dry_run=False,
            write_applied=True,
        )

    return with_sqlite_repo_transaction(repo, _run)


def _create_wheel_call_intent(
    repo: Any,
    *,
    candidate_snapshot: Mapping[str, Any],
    current_strategy_policy_sha256: str,
    account: str,
    lot_id: str,
    final_candidate_id: str,
    expected_snapshot_hash: str,
    expected_batch_generation_hash: str,
    expires_at_ms: int,
    request_id: str,
    actor: str,
    coverage_fact: Mapping[str, Any],
    new_intent_enabled: bool,
    account_configured: bool,
    market: str,
    activation_descriptor: Mapping[str, Any] | None,
    policy_sha256: str,
    broker_order_id: str | None = None,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
    wheel_branch_id: str | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    stock_lot_value = str(lot_id or "").strip()
    request_value = str(request_id or "").strip()
    candidate_id = str(final_candidate_id or "").strip()
    snapshot_hash = str(expected_snapshot_hash or "").strip()
    expected_generation = str(expected_batch_generation_hash or "").strip()
    actor_value = str(actor or "").strip()
    market_value = _require_market(market)
    instant = int(as_of_ms or _now_ms())
    if not all(
        (
            account_value,
            stock_lot_value or wheel_branch_id,
            request_value,
            candidate_id,
            snapshot_hash,
            expected_generation,
            actor_value,
        )
    ):
        raise ValueError("Wheel Call intent requires complete request fields")

    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        nonlocal stock_lot_value
        rows = sqlite_repo.read_lifecycle_account_rows(account=account_value, conn=conn)
        if wheel_branch_id is not None:
            branch = resolve_wheel_branch_from_rows(
                rows, account=account_value, wheel_branch_id=wheel_branch_id,
                as_of_ms=instant, market=market_value,
            )
            if branch.get("direction") != "call":
                raise ValueError("Wheel branch direction mismatch")
            bound_lot = str(branch.get("stock_lot_id") or "").strip()
            if not bound_lot or (stock_lot_value and stock_lot_value != bound_lot):
                raise ValueError("Wheel Call branch has no matching stock lot")
            stock_lot_value = bound_lot
        existing = [
            event
            for event in rows.get("account_wheel_events") or []
            if event.get("event_type") == "wheel_call_intent_created"
            and str((event.get("payload") or {}).get("request_id") or "").strip()
            == request_value
        ]
        if len(existing) > 1:
            raise ValueError("Wheel Call intent request identity is not unique")
        if existing:
            event = existing[0]
            payload = event.get("payload") or {}
            if (
                event.get("stock_lot_id") != stock_lot_value
                or str(payload.get("actor") or "") != actor_value
                or str(payload.get("final_candidate_id") or "") != candidate_id
                or str(payload.get("snapshot_hash") or "") != snapshot_hash
                or str(payload.get("batch_generation_hash") or "")
                != expected_generation
                or int(payload.get("expires_at_ms") or 0) != int(expires_at_ms)
                or str(payload.get("broker_order_id") or "")
                != str(broker_order_id or "").strip()
                or str(payload.get("market") or "").strip().lower()
                != market_value
            ):
                raise ValueError("Wheel Call intent request identity conflicts")
            return _intent_result(
                event,
                status="idempotent",
                dry_run=not apply_changes,
                write_applied=False,
            )
        if not new_intent_enabled:
            raise ValueError("wheel_disabled: new Wheel Call intents are disabled")

        try:
            batch = _wheel_batch(
                rows,
                account=account_value,
                lot_id=stock_lot_value,
                as_of_ms=instant,
                market=market_value,
            )
        except ValueError as exc:
            raise ValueError("wheel_disabled: descriptor_mismatch") from exc
        require_wheel_activation(
            durable_window=sqlite_repo.get_current_wheel_activation_window(
                market=market_value, account=account_value, conn=conn,
            ),
            market=market_value,
            branch=batch,
            activation_descriptor=activation_descriptor,
            account_configured=account_configured,
            policy_sha256=policy_sha256,
        )
        if batch["batch_generation_hash"] != expected_generation:
            raise ValueError("stale_snapshot: Wheel batch generation changed")
        candidate = _snapshot_final_candidate(
            candidate_snapshot,
            current_strategy_policy_sha256=current_strategy_policy_sha256,
            account=account_value,
            lot_id=stock_lot_value,
            final_candidate_id=candidate_id,
            expected_snapshot_hash=snapshot_hash,
            expected_batch_generation_hash=expected_generation,
        )
        candidate.setdefault("symbol", batch["symbol"])
        summaries = _intent_summaries(
            rows,
            account=account_value,
            lot_id=stock_lot_value,
            as_of_ms=instant,
        )
        order_id = str(broker_order_id or "").strip()
        if order_id and any(
            item.get("status") == "active"
            and str((item.get("payload") or {}).get("broker_order_id") or "")
            == order_id
            for item in summaries
        ):
            raise ValueError("broker_order_id already belongs to an active Wheel intent")
        if not broker_capacity_observation_is_fresh(coverage_fact, account=account_value, market=market_value):
            raise ValueError("Wheel Call broker capacity observation is stale or unavailable")
        decision_snapshot = decision_state_snapshot_from_locked_rows(
            sqlite_repo, rows,
            account=account_value,
            portfolio_scope_id=portfolio_scope_id(account_value),
            source_observed_at=datetime.now(timezone.utc).isoformat(),
            current_decision_now_ms=instant,
        )
        option_context = build_option_positions_context(
            list(rows["stored_position_lots"]),
            broker=str(candidate.get("broker") or "futu"),
            account=account_value,
            decision_snapshot=decision_snapshot,
            lifecycle_now_ms=instant,
        )
        symbol = str(batch.get("symbol") or "")
        if (option_context.get("context_status") != "available"
                or option_context.get("decision_snapshot_status") != "trusted"
                or symbol in (option_context.get("locked_shares_unavailable_by_symbol") or {})
                or not str(coverage_fact.get("decision_state_fingerprint") or "").strip()
                or coverage_fact.get("decision_state_fingerprint")
                   != option_context.get("decision_state_fingerprint")
                or str(candidate.get("capacity_identity_hash") or "").strip()
                   != str(coverage_fact.get("capacity_identity_hash") or "").strip()):
            raise ValueError("Wheel Call coverage settlement decision is unavailable")
        current_coverage = revalidate_opening_share_coverage(
            coverage_fact,
            list(rows.get("account_position_lots") or []),
            list(
                build_wheel_read_model_from_rows(
                    rows,
                    account=account_value,
                    as_of_ms=instant,
                    market=market_value,
                )["batches"]
            ),
            account=account_value,
            symbol=str(batch.get("symbol") or ""),
        )
        event = plan_wheel_call_intent_create(
            batch,
            candidate,
            current_coverage,
            expires_at_ms,
            request_value,
            actor_value,
            occurred_at_ms=instant,
            recorded_at_ms=instant,
            broker_order_id=order_id or None,
        )
        if not apply_changes:
            return _intent_result(
                event,
                status="planned",
                dry_run=True,
                write_applied=False,
            )
        if not sqlite_repo.append_wheel_event_once(event, conn=conn):
            raise ValueError("Wheel Call intent append unexpectedly replayed")
        projected = _wheel_batch(
            sqlite_repo.read_lifecycle_account_rows(account=account_value, conn=conn),
            account=account_value,
            lot_id=stock_lot_value,
            as_of_ms=instant,
            market=market_value,
        )
        expected_reserved = int(event["payload"]["contracts"]) * int(
            event["payload"]["multiplier"]
        )
        if (
            projected["phase"] != "call_pending"
            or event["intent_id"] not in projected["active_intent_ids"]
            or projected["active_intent_reserved_shares"] != expected_reserved
        ):
            raise ValueError("Wheel Call intent projection verification failed")
        return _intent_result(
            event,
            status="created",
            dry_run=False,
            write_applied=True,
        )

    return with_sqlite_repo_transaction(repo, _run)


def _cancel_wheel_call_intent(
    repo: Any,
    *,
    account: str,
    lot_id: str,
    intent_id: str,
    expected_batch_generation_hash: str,
    request_id: str,
    actor: str,
    broker_order_inactive_confirmed: bool,
    reason: str,
    market: str,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
    wheel_branch_id: str | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    stock_lot_value = str(lot_id or "").strip()
    intent_value = str(intent_id or "").strip()
    expected_generation = str(expected_batch_generation_hash or "").strip()
    request_value = str(request_id or "").strip()
    actor_value = str(actor or "").strip()
    reason_value = str(reason or "").strip()
    market_value = _require_market(market)
    instant = int(as_of_ms or _now_ms())
    if not all(
        (
            account_value,
            stock_lot_value or wheel_branch_id,
            intent_value,
            expected_generation,
            request_value,
            actor_value,
            reason_value,
        )
    ):
        raise ValueError("Wheel Call intent cancellation requires complete fields")
    if not broker_order_inactive_confirmed:
        raise ValueError("broker_order_inactive_confirmed=true is required")

    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        nonlocal stock_lot_value
        rows = sqlite_repo.read_lifecycle_account_rows(account=account_value, conn=conn)
        if wheel_branch_id is not None:
            branch = resolve_wheel_branch_from_rows(
                rows, account=account_value, wheel_branch_id=wheel_branch_id,
                as_of_ms=instant, market=market_value,
            )
            if branch.get("direction") != "call":
                raise ValueError("Wheel branch direction mismatch")
            bound_lot = str(branch.get("stock_lot_id") or "").strip()
            if not bound_lot or (stock_lot_value and stock_lot_value != bound_lot):
                raise ValueError("Wheel Call branch has no matching stock lot")
            stock_lot_value = bound_lot
        prior_events = rows.get("account_wheel_events") or []
        existing = [
            event
            for event in prior_events
            if event.get("event_type") == "wheel_call_intent_cancelled"
            and event.get("intent_id") == intent_value
            and str((event.get("payload") or {}).get("request_id") or "")
            == request_value
        ]
        if len(existing) > 1:
            raise ValueError("Wheel Call intent cancellation identity is not unique")
        if existing:
            payload = existing[0].get("payload") or {}
            if (
                existing[0].get("stock_lot_id") != stock_lot_value
                or str(payload.get("actor") or "") != actor_value
                or str(payload.get("reason") or "") != reason_value
                or str(payload.get("batch_generation_hash") or "")
                != expected_generation
                or payload.get("broker_order_inactive_confirmed") is not True
                or str(payload.get("market") or "").strip().lower()
                != market_value
            ):
                raise ValueError("Wheel Call intent cancellation identity conflicts")
            return _intent_result(
                existing[0],
                status="idempotent",
                dry_run=not apply_changes,
                write_applied=False,
            )
        batch = _wheel_batch(
            rows,
            account=account_value,
            lot_id=stock_lot_value,
            as_of_ms=instant,
            market=market_value,
        )
        if batch["batch_generation_hash"] != expected_generation:
            raise ValueError("Wheel batch generation changed; refresh before confirming")
        matches = [
            item
            for item in _intent_summaries(
                rows,
                account=account_value,
                lot_id=stock_lot_value,
                as_of_ms=instant,
            )
            if item["intent_id"] == intent_value
        ]
        if len(matches) != 1:
            raise ValueError("Wheel Call intent must resolve uniquely")
        event = plan_wheel_call_intent_cancel(
            batch,
            matches[0],
            request_value,
            actor_value,
            broker_order_inactive_confirmed,
            reason_value,
            occurred_at_ms=instant,
            recorded_at_ms=instant,
        )
        if event is None:
            return attach_write_contract(
                {
                    "schema_version": "wheel_call_intent_result.v1",
                    "status": "already_inactive",
                    "intent_id": intent_value,
                    "stock_lot_id": stock_lot_value,
                    "market": market_value,
                },
                dry_run=not apply_changes,
                write_applied=False,
            )
        if not apply_changes:
            return _intent_result(event, status="planned", dry_run=True, write_applied=False)
        if not sqlite_repo.append_wheel_event_once(event, conn=conn):
            raise ValueError("Wheel Call intent cancellation unexpectedly replayed")
        after_rows = sqlite_repo.read_lifecycle_account_rows(account=account_value, conn=conn)
        after = _intent_summaries(
            after_rows,
            account=account_value,
            lot_id=stock_lot_value,
            as_of_ms=instant,
        )
        if any(item["intent_id"] == intent_value and item["status"] == "active" for item in after):
            raise ValueError("Wheel Call intent cancellation verification failed")
        return _intent_result(event, status="cancelled", dry_run=False, write_applied=True)

    return with_sqlite_repo_transaction(repo, _run)


# Preserve the merged facade names while sharing the domain implementations.
validate_wheel_activation_request = resolve_wheel_activation_request
wheel_activation_descriptor = wheel_activation_window_descriptor
