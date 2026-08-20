from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import (
    build_open_adjustment_patch_contract,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
)
from domain.domain.wheel import (
    build_wheel_event,
    plan_wheel_call_intent_cancel,
    plan_wheel_call_intent_consume,
    plan_wheel_call_intent_create,
    plan_wheel_manual_end,
    project_wheel_call_intents,
)
from src.application.ledger.api import with_sqlite_repo_transaction
from src.application.ledger.current_decision_projection import (
    capture_trade_event_decision_projection_fence,
)
from src.application.ledger.position_projection_runtime import (
    run_position_projection_in_transaction,
)
from src.application.ledger.writer import _finish_trade_event_decision_projection
from src.application.wheel.trade_companions import (
    append_and_verify_wheel_intent_consumption,
)
from src.application.wheel.read_model import build_wheel_read_model_from_rows
from src.application.write_contract import attach_write_contract


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _wheel_batch(
    rows: Mapping[str, Any],
    *,
    account: str,
    stock_lot_id: str,
    as_of_ms: int,
) -> dict[str, Any]:
    matches = [
        batch
        for batch in build_wheel_read_model_from_rows(
            rows,
            account=account,
            as_of_ms=as_of_ms,
        )["batches"]
        if batch["stock_lot_id"] == stock_lot_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Wheel batch must resolve uniquely: stock_lot_id={stock_lot_id}"
        )
    return matches[0]


def end_wheel_lifecycle(
    repo: Any,
    *,
    account: str,
    stock_lot_id: str,
    expected_batch_generation_hash: str,
    request_id: str,
    actor: str,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    stock_lot_value = str(stock_lot_id or "").strip()
    expected_generation = str(expected_batch_generation_hash or "").strip()
    request_value = str(request_id or "").strip()
    actor_value = str(actor or "").strip()
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
            ):
                raise ValueError("Wheel manual end request identity conflicts")
            batch = _wheel_batch(
                rows,
                account=account_value,
                stock_lot_id=stock_lot_value,
                as_of_ms=max(instant, int(event.get("occurred_at_ms") or 0)),
            )
            if (
                batch["lifecycle_status"] != "manual_ended"
                or batch["phase"] is not None
                or batch["terminal_event_id"] != event["event_id"]
            ):
                raise ValueError("Wheel manual end idempotency verification failed")
            return _result(
                event=event,
                generation=expected_generation,
                status_before="active",
                status_after="manual_ended",
                dry_run=not apply_changes,
                write_applied=False,
                idempotent=True,
            )

        batch = _wheel_batch(
            rows,
            account=account_value,
            stock_lot_id=stock_lot_value,
            as_of_ms=instant,
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
            return _result(
                event=event,
                generation=expected_generation,
                status_before=batch["lifecycle_status"],
                status_after="manual_ended",
                dry_run=True,
                write_applied=False,
                idempotent=False,
            )

        created = sqlite_repo.append_wheel_event_once(event, conn=conn)
        projected = _wheel_batch(
            sqlite_repo.read_lifecycle_account_rows(
                account=account_value,
                conn=conn,
            ),
            account=account_value,
            stock_lot_id=stock_lot_value,
            as_of_ms=instant,
        )
        if (
            not created
            or projected["lifecycle_status"] != "manual_ended"
            or projected["phase"] is not None
            or projected["terminal_event_id"] != event["event_id"]
        ):
            raise ValueError("Wheel manual end projection verification failed")
        return _result(
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
    stock_lot_id: str,
    as_of_ms: int,
) -> list[dict[str, Any]]:
    return project_wheel_call_intents(
        rows.get("account_wheel_events") or [],
        account=account,
        stock_lot_id=stock_lot_id,
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
    account: str,
    stock_lot_id: str,
    final_candidate_id: str,
    expected_snapshot_hash: str,
    expected_batch_generation_hash: str,
) -> dict[str, Any]:
    if str(candidate_snapshot.get("snapshot_hash") or "").strip() != expected_snapshot_hash:
        raise ValueError("stale_snapshot: Wheel candidate snapshot hash changed")
    if str(candidate_snapshot.get("account") or "").strip().lower() != account:
        raise ValueError("stale_snapshot: Wheel candidate snapshot account mismatch")
    matches = [
        item
        for item in candidate_snapshot.get("batches") or []
        if isinstance(item, Mapping)
        and str(item.get("stock_lot_id") or "").strip() == stock_lot_id
    ]
    if len(matches) != 1:
        raise ValueError("stale_snapshot: Wheel candidate batch is not unique")
    batch_snapshot = matches[0]
    if (
        str(batch_snapshot.get("batch_generation_hash") or "").strip()
        != expected_batch_generation_hash
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
        "stock_lot_id": stock_lot_id,
        "snapshot_hash": expected_snapshot_hash,
    }


def create_wheel_call_intent(
    repo: Any,
    *,
    candidate_snapshot: Mapping[str, Any],
    account: str,
    stock_lot_id: str,
    final_candidate_id: str,
    expected_snapshot_hash: str,
    expected_batch_generation_hash: str,
    expires_at_ms: int,
    request_id: str,
    actor: str,
    coverage_fact: Mapping[str, Any],
    broker_order_id: str | None = None,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    stock_lot_value = str(stock_lot_id or "").strip()
    request_value = str(request_id or "").strip()
    candidate_id = str(final_candidate_id or "").strip()
    snapshot_hash = str(expected_snapshot_hash or "").strip()
    expected_generation = str(expected_batch_generation_hash or "").strip()
    actor_value = str(actor or "").strip()
    instant = int(as_of_ms or _now_ms())
    if not all(
        (
            account_value,
            stock_lot_value,
            request_value,
            candidate_id,
            snapshot_hash,
            expected_generation,
            actor_value,
        )
    ):
        raise ValueError("Wheel Call intent requires complete request fields")

    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        rows = sqlite_repo.read_lifecycle_account_rows(account=account_value, conn=conn)
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
                or str(payload.get("capacity_identity_hash") or "")
                != str(coverage_fact.get("capacity_identity_hash") or "").strip()
            ):
                raise ValueError("Wheel Call intent request identity conflicts")
            return _intent_result(
                event,
                status="idempotent",
                dry_run=not apply_changes,
                write_applied=False,
            )

        batch = _wheel_batch(
            rows,
            account=account_value,
            stock_lot_id=stock_lot_value,
            as_of_ms=instant,
        )
        if batch["batch_generation_hash"] != expected_generation:
            raise ValueError("stale_snapshot: Wheel batch generation changed")
        candidate = _snapshot_final_candidate(
            candidate_snapshot,
            account=account_value,
            stock_lot_id=stock_lot_value,
            final_candidate_id=candidate_id,
            expected_snapshot_hash=snapshot_hash,
            expected_batch_generation_hash=expected_generation,
        )
        candidate.setdefault("symbol", batch["symbol"])
        summaries = _intent_summaries(
            rows,
            account=account_value,
            stock_lot_id=stock_lot_value,
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
        event = plan_wheel_call_intent_create(
            batch,
            candidate,
            coverage_fact,
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
            stock_lot_id=stock_lot_value,
            as_of_ms=instant,
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


def cancel_wheel_call_intent(
    repo: Any,
    *,
    account: str,
    stock_lot_id: str,
    intent_id: str,
    expected_batch_generation_hash: str,
    request_id: str,
    actor: str,
    broker_order_inactive_confirmed: bool,
    reason: str,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    stock_lot_value = str(stock_lot_id or "").strip()
    intent_value = str(intent_id or "").strip()
    expected_generation = str(expected_batch_generation_hash or "").strip()
    request_value = str(request_id or "").strip()
    actor_value = str(actor or "").strip()
    reason_value = str(reason or "").strip()
    instant = int(as_of_ms or _now_ms())
    if not all(
        (
            account_value,
            stock_lot_value,
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
        rows = sqlite_repo.read_lifecycle_account_rows(account=account_value, conn=conn)
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
            stock_lot_id=stock_lot_value,
            as_of_ms=instant,
        )
        if batch["batch_generation_hash"] != expected_generation:
            raise ValueError("Wheel batch generation changed; refresh before confirming")
        matches = [
            item
            for item in _intent_summaries(
                rows,
                account=account_value,
                stock_lot_id=stock_lot_value,
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
            stock_lot_id=stock_lot_value,
            as_of_ms=instant,
        )
        if any(item["intent_id"] == intent_value and item["status"] == "active" for item in after):
            raise ValueError("Wheel Call intent cancellation verification failed")
        return _intent_result(event, status="cancelled", dry_run=False, write_applied=True)

    return with_sqlite_repo_transaction(repo, _run)


def _linkage_candidate(
    model: Mapping[str, Any],
    *,
    call_record_id: str,
    stock_lot_id: str,
    linkage_candidate_id: str,
    expected_input_hash: str,
    expected_batch_generation_hash: str,
) -> dict[str, Any]:
    matches = [
        item
        for item in model.get("linkage_candidates") or []
        if item["call_record_id"] == call_record_id
        and item["stock_lot_id"] == stock_lot_id
        and item["linkage_candidate_id"] == linkage_candidate_id
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


def _validate_linkage_coverage(
    coverage_fact: Mapping[str, Any],
    *,
    account: str,
    symbol: str,
) -> None:
    if (
        str(coverage_fact.get("account") or "").strip().lower() != account
        or str(coverage_fact.get("symbol") or "").strip().upper() != symbol
        or not str(coverage_fact.get("capacity_identity_hash") or "").strip()
    ):
        raise ValueError("Wheel Call linkage coverage identity is unavailable")


def confirm_wheel_call_linkage(
    repo: Any,
    *,
    account: str,
    call_record_id: str,
    stock_lot_id: str,
    linkage_candidate_id: str,
    expected_input_hash: str,
    expected_batch_generation_hash: str,
    request_id: str,
    actor: str,
    coverage_fact: Mapping[str, Any],
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    call_lot_value = str(call_record_id or "").strip()
    stock_lot_value = str(stock_lot_id or "").strip()
    candidate_id = str(linkage_candidate_id or "").strip()
    input_hash = str(expected_input_hash or "").strip()
    expected_generation = str(expected_batch_generation_hash or "").strip()
    request_value = str(request_id or "").strip()
    actor_value = str(actor or "").strip()
    instant = int(as_of_ms or _now_ms())
    if not all(
        (
            account_value,
            call_lot_value,
            stock_lot_value,
            candidate_id,
            input_hash,
            expected_generation,
            request_value,
            actor_value,
        )
    ):
        raise ValueError("Wheel Call linkage confirmation requires complete fields")

    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        rows = sqlite_repo.read_lifecycle_account_rows(account=account_value, conn=conn)
        existing = [
            item
            for item in rows.get("trade_events") or []
            if str((item.get("raw_payload") or {}).get("wheel_linkage_request_id") or "")
            == request_value
        ]
        if len(existing) > 1:
            raise ValueError("Wheel Call linkage request identity is not unique")
        if existing:
            payload = existing[0].get("raw_payload") or {}
            if (
                str(payload.get("target_lot_id") or "") != call_lot_value
                or str(payload.get("source_stock_lot_id") or "") != stock_lot_value
                or str(payload.get("actor") or "") != actor_value
                or str(payload.get("linkage_candidate_id") or "") != candidate_id
                or str(payload.get("input_snapshot_hash") or "") != input_hash
                or str(payload.get("batch_generation_hash") or "")
                != expected_generation
            ):
                raise ValueError("Wheel Call linkage request identity conflicts")
            return _linkage_result(
                status="idempotent",
                event_id=str(existing[0]["event_id"]),
                call_record_id=call_lot_value,
                stock_lot_id=stock_lot_value,
                request_id=request_value,
                dry_run=not apply_changes,
                write_applied=False,
            )

        model = build_wheel_read_model_from_rows(
            rows,
            account=account_value,
            as_of_ms=instant,
        )
        candidate = _linkage_candidate(
            model,
            call_record_id=call_lot_value,
            stock_lot_id=stock_lot_value,
            linkage_candidate_id=candidate_id,
            expected_input_hash=input_hash,
            expected_batch_generation_hash=expected_generation,
        )
        batch = next(
            item for item in model["batches"] if item["stock_lot_id"] == stock_lot_value
        )
        _validate_linkage_coverage(
            coverage_fact,
            account=account_value,
            symbol=str(candidate["symbol"]),
        )
        fields = sqlite_repo.get_position_lot_fields(call_lot_value, conn=conn)
        patch = build_open_adjustment_patch_contract(
            fields,
            strategy="wheel",
            leg_role="wheel_call",
            source_stock_lot_id=stock_lot_value,
            as_of_ms=instant,
        )
        digest = canonical_sha256(
            {
                "account": account_value,
                "call_record_id": call_lot_value,
                "stock_lot_id": stock_lot_value,
                "request_id": request_value,
            }
        )[:24]
        event = TradeEvent(
            event_id=f"wheel-call-linkage-confirmed:{digest}",
            event_type="adjust",
            event_time_ms=instant,
            contract_key=ContractKey.from_values(
                broker=fields.get("broker"),
                account=account_value,
                underlying_symbol=fields.get("symbol"),
                option_type=fields.get("option_type"),
                position_side=fields.get("side"),
                strike=effective_strike(fields),
                expiration_ymd=effective_expiration_ymd(fields),
            ),
            contracts=0,
            price=0,
            currency=str(fields.get("currency") or ""),
            source="wheel_linkage",
            multiplier=float(effective_multiplier(fields) or 0),
            target_lot_id=call_lot_value,
            raw_payload={
                "schema_version": "wheel_call_linkage_confirmed.v1",
                "source": "wheel_linkage",
                "target_lot_id": call_lot_value,
                "adjust_target_source_event_id": candidate["call_open_event_id"],
                "wheel_linkage_request_id": request_value,
                "linkage_candidate_id": candidate_id,
                "input_snapshot_hash": input_hash,
                "batch_generation_hash": expected_generation,
                "actor": actor_value,
                "source_stock_lot_id": stock_lot_value,
                "patch": patch.to_dict(),
            },
        )
        open_rows = [
            item
            for item in rows.get("trade_events") or []
            if str(item.get("event_id") or "") == candidate["call_open_event_id"]
        ]
        if len(open_rows) != 1:
            raise ValueError("Wheel Call open event is not unique")
        fill = open_rows[0]
        intent_plans: list[dict[str, Any]] = []
        known_ids = {
            str(item.get("event_id") or "").strip()
            for item in rows.get("trade_events") or []
            if str(item.get("event_id") or "").strip()
        }
        for intent in project_wheel_call_intents(
            rows.get("account_wheel_events") or [],
            account=account_value,
            stock_lot_id=stock_lot_value,
            as_of_ms=int(fill.get("event_time_ms") or 0),
            known_trade_event_ids=known_ids,
        ):
            if intent.get("status") != "active":
                continue
            try:
                intent_plans.append(
                    plan_wheel_call_intent_consume(
                        batch,
                        intent,
                        fill,
                        {
                            **dict(coverage_fact),
                            "status": "available",
                            "shares_available_for_cover": candidate["required_shares"],
                        },
                        recorded_at_ms=instant,
                    )
                )
            except ValueError:
                continue
        if len(intent_plans) > 1:
            raise ValueError("multiple Wheel Call intents match this fill")
        intent_event = intent_plans[0] if intent_plans else None
        if not apply_changes:
            return _linkage_result(
                status="planned",
                event_id=event.event_id,
                call_record_id=call_lot_value,
                stock_lot_id=stock_lot_value,
                request_id=request_value,
                dry_run=True,
                write_applied=False,
            )

        fence = capture_trade_event_decision_projection_fence(sqlite_repo, conn=conn)
        runtime = run_position_projection_in_transaction(
            sqlite_repo,
            [event],
            conn=conn,
            mode="fast_if_safe",
        )
        if runtime.created_flags != (True,):
            raise ValueError("Wheel Call linkage adjust unexpectedly replayed")
        if intent_event is not None:
            append_and_verify_wheel_intent_consumption(
                sqlite_repo,
                conn=conn,
                linked_event=event,
                intent_event=intent_event,
            )
        else:
            linked = sqlite_repo.get_position_lot_fields(call_lot_value, conn=conn)
            if (
                linked.get("strategy") != "wheel"
                or linked.get("leg_role") != "wheel_call"
                or linked.get("source_stock_lot_id") != stock_lot_value
            ):
                raise ValueError("Wheel Call linkage verification failed")
        _finish_trade_event_decision_projection(
            sqlite_repo,
            conn=conn,
            fence=fence,
            events=[event],
            created_flags=runtime.created_flags,
        )
        return _linkage_result(
            status="confirmed",
            event_id=event.event_id,
            call_record_id=call_lot_value,
            stock_lot_id=stock_lot_value,
            request_id=request_value,
            dry_run=False,
            write_applied=True,
            intent_event_id=(intent_event or {}).get("event_id"),
        )

    return with_sqlite_repo_transaction(
        repo,
        _run,
        require_projection_publication=True,
    )


def reject_wheel_call_linkage(
    repo: Any,
    *,
    account: str,
    call_record_id: str,
    stock_lot_id: str,
    linkage_candidate_id: str,
    expected_input_hash: str,
    expected_batch_generation_hash: str,
    request_id: str,
    actor: str,
    reason: str,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    values = {
        "account": str(account or "").strip().lower(),
        "call_record_id": str(call_record_id or "").strip(),
        "stock_lot_id": str(stock_lot_id or "").strip(),
        "linkage_candidate_id": str(linkage_candidate_id or "").strip(),
        "expected_input_hash": str(expected_input_hash or "").strip(),
        "expected_batch_generation_hash": str(
            expected_batch_generation_hash or ""
        ).strip(),
        "request_id": str(request_id or "").strip(),
        "actor": str(actor or "").strip(),
        "reason": str(reason or "").strip(),
    }
    instant = int(as_of_ms or _now_ms())
    if not all(values.values()):
        raise ValueError("Wheel Call linkage rejection requires complete fields")

    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        rows = sqlite_repo.read_lifecycle_account_rows(account=values["account"], conn=conn)
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
            ):
                raise ValueError("Wheel Call linkage rejection identity conflicts")
            return _linkage_result(
                status="idempotent",
                event_id=event["event_id"],
                call_record_id=values["call_record_id"],
                stock_lot_id=values["stock_lot_id"],
                request_id=values["request_id"],
                dry_run=not apply_changes,
                write_applied=False,
            )
        model = build_wheel_read_model_from_rows(
            rows,
            account=values["account"],
            as_of_ms=instant,
        )
        candidate = _linkage_candidate(
            model,
            call_record_id=values["call_record_id"],
            stock_lot_id=values["stock_lot_id"],
            linkage_candidate_id=values["linkage_candidate_id"],
            expected_input_hash=values["expected_input_hash"],
            expected_batch_generation_hash=values["expected_batch_generation_hash"],
        )
        digest = canonical_sha256(
            {
                "account": values["account"],
                "call_record_id": values["call_record_id"],
                "stock_lot_id": values["stock_lot_id"],
                "request_id": values["request_id"],
            }
        )[:24]
        event = build_wheel_event(
            event_id=f"wheel-call-linkage-rejected:{digest}",
            account=values["account"],
            stock_lot_id=values["stock_lot_id"],
            event_type="wheel_call_linkage_rejected",
            occurred_at_ms=instant,
            recorded_at_ms=instant,
            source_trade_event_id=candidate["call_open_event_id"],
            payload={
                "schema_version": "wheel_call_linkage_rejected.v1",
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
                call_record_id=values["call_record_id"],
                stock_lot_id=values["stock_lot_id"],
                request_id=values["request_id"],
                dry_run=True,
                write_applied=False,
            )
        if not sqlite_repo.append_wheel_event_once(event, conn=conn):
            raise ValueError("Wheel Call linkage rejection unexpectedly replayed")
        after = build_wheel_read_model_from_rows(
            sqlite_repo.read_lifecycle_account_rows(account=values["account"], conn=conn),
            account=values["account"],
            as_of_ms=instant,
        )
        if any(
            item["linkage_candidate_id"] == values["linkage_candidate_id"]
            for item in after["linkage_candidates"]
        ):
            raise ValueError("Wheel Call linkage rejection verification failed")
        return _linkage_result(
            status="rejected",
            event_id=event["event_id"],
            call_record_id=values["call_record_id"],
            stock_lot_id=values["stock_lot_id"],
            request_id=values["request_id"],
            dry_run=False,
            write_applied=True,
        )

    return with_sqlite_repo_transaction(repo, _run)


def _linkage_result(
    *,
    status: str,
    event_id: str,
    call_record_id: str,
    stock_lot_id: str,
    request_id: str,
    dry_run: bool,
    write_applied: bool,
    intent_event_id: str | None = None,
) -> dict[str, Any]:
    return attach_write_contract(
        {
            "schema_version": "wheel_call_linkage_result.v1",
            "status": status,
            "event_id": event_id,
            "call_record_id": call_record_id,
            "stock_lot_id": stock_lot_id,
            "request_id": request_id,
            "intent_event_id": intent_event_id,
        },
        dry_run=dry_run,
        write_applied=write_applied,
        audit_id=event_id,
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
        },
        dry_run=dry_run,
        write_applied=write_applied,
        audit_id=event["event_id"],
    )


def _result(
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


__all__ = [
    "cancel_wheel_call_intent",
    "confirm_wheel_call_linkage",
    "create_wheel_call_intent",
    "end_wheel_lifecycle",
    "reject_wheel_call_linkage",
]
