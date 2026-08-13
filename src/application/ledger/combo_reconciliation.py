from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from domain.domain.combo_identity import build_combo_identity_intent, identity_from_intent
from domain.domain.combo_reconciliation import match_post_trade_combo_pairs
from domain.domain.config_contract import RUNTIME_SCHEDULE_TIMEZONE_BY_MARKET
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import (
    build_open_adjustment_patch_contract,
    effective_contracts,
    effective_contracts_open,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    now_ms,
)
from src.application.ledger.combo_membership import resolve_combo_group_membership
from src.application.ledger.event_codec import valid_void_target_event_id
from src.application.ledger.projection_verify import compare_projection_lots
from src.application.ledger.publisher import (
    ensure_projection_publishable,
    project_stored_trade_events_to_position_lots,
)
from src.application.ledger.position_projection_publication import (
    publish_full_position_projection,
)
from src.application.ledger.repository import (
    require_option_positions_event_read_repo,
    with_sqlite_repo_transaction,
)


_PENDING_INFERENCE_STATUSES = {"proposal_ready", "ambiguous"}
_TERMINAL_PAIR_STATUSES = {"user_rejected", "superseded"}
_REACTIVATABLE_STALE_REASON = "facts_drifted_or_leg_claimed"


def reconcile_combo_pair_inferences(
    *,
    repo: Any,
    account: str,
    runtime_environment: str,
    exposures: Iterable[Mapping[str, Any]] = (),
    persist: bool = False,
    effective_now_ms: int | None = None,
) -> dict[str, Any]:
    """Derive Combo inferences from canonical ledger facts, optionally persisting proposals."""

    account_value = str(account or "").strip().lower()
    environment_value = str(runtime_environment or "").strip().lower()
    if not account_value:
        raise ValueError("combo reconciliation requires account")
    if not environment_value:
        raise ValueError("combo reconciliation requires runtime_environment")
    now_value = int(effective_now_ms or now_ms())
    if now_value <= 0:
        raise ValueError("effective_now_ms must be > 0")
    exposure_rows = [dict(item) for item in exposures]
    if not persist:
        candidate = require_option_positions_event_read_repo(repo)
        return _reconcile_with_repo(
            candidate,
            conn=None,
            account=account_value,
            runtime_environment=environment_value,
            exposures=exposure_rows,
            persist=False,
            effective_now_ms=now_value,
        )

    return with_sqlite_repo_transaction(
        repo,
        lambda candidate, conn: _reconcile_with_repo(
            candidate,
            conn=conn,
            account=account_value,
            runtime_environment=environment_value,
            exposures=exposure_rows,
            persist=True,
            effective_now_ms=now_value,
        ),
    )


def list_combo_pair_inferences(
    *,
    repo: Any,
    account: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    candidate = getattr(repo, "primary_repo", repo)
    method = getattr(candidate, "list_combo_pair_inferences", None)
    if not callable(method):
        raise TypeError("option_positions repo does not support combo inferences")
    return method(account=account, status=status)


def adopt_post_trade_combo_pair(
    *,
    repo: Any,
    inference_id: str,
    expected_input_hash: str,
    actor: str,
    apply_changes: bool = False,
    effective_now_ms: int | None = None,
) -> dict[str, Any]:
    """Validate and optionally adopt one exact post-trade Combo inference atomically."""

    inference_value = str(inference_id or "").strip()
    hash_value = str(expected_input_hash or "").strip()
    actor_value = str(actor or "").strip()
    if not inference_value or not hash_value:
        raise ValueError("combo confirmation requires inference_id and expected_input_hash")
    if apply_changes and not actor_value:
        raise ValueError("combo confirmation apply requires actor")
    decision_ms = int(effective_now_ms or now_ms())

    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        if conn is None:
            raise TypeError("combo confirmation requires SQLite transaction authority")
        inference = sqlite_repo.get_combo_pair_inference(inference_value, conn=conn)
        if inference is None:
            raise ValueError(f"combo inference not found: {inference_value}")
        if str(inference.get("input_snapshot_hash") or "") != hash_value:
            raise ValueError("combo inference input hash compare-and-set failed")
        status = str(inference.get("status") or "").strip().lower()
        if status == "user_confirmed":
            return {
                "schema_version": "post_trade_combo_adoption.v1",
                "status": "already_confirmed",
                "apply_changes": bool(apply_changes),
                "inference": inference,
            }
        if status not in _PENDING_INFERENCE_STATUSES:
            raise ValueError(f"combo inference is not confirmable: {status}")
        if int(inference.get("proposal_expires_at_ms") or 0) < decision_ms:
            raise ValueError("combo inference proposal has expired")
        current = _validate_inference_against_current_ledger(
            sqlite_repo,
            conn=conn,
            inference=inference,
        )
        confirmed = [
            item
            for item in sqlite_repo.list_combo_pair_inferences(
                account=str(inference["account"]),
                status="user_confirmed",
                conn=conn,
            )
            if str(item.get("inference_id") or "") != inference_value
            and {
                str(item.get("put_open_event_id") or ""),
                str(item.get("call_open_event_id") or ""),
            }
            & {
                str(inference["put_open_event_id"]),
                str(inference["call_open_event_id"]),
            }
        ]
        if confirmed:
            raise ValueError("combo inference leg is already claimed")
        group_id = str(inference.get("strategy_group_id") or "").strip()
        if not group_id:
            raise ValueError("combo inference strategy_group_id is missing")
        event_ids = {
            "funding_put": _combo_decision_event_id(
                "combo-adopt", inference_value, "funding_put"
            ),
            "participation_call": _combo_decision_event_id(
                "combo-adopt", inference_value, "participation_call"
            ),
        }
        preview = {
            "schema_version": "post_trade_combo_adoption.v1",
            "status": "dry_run" if not apply_changes else "adopted",
            "apply_changes": bool(apply_changes),
            "inference_id": inference_value,
            "input_snapshot_hash": hash_value,
            "strategy_group_id": group_id,
            "put_record_id": str(inference["put_record_id"]),
            "call_record_id": str(inference["call_record_id"]),
            "put_adoption_event_id": event_ids["funding_put"],
            "call_adoption_event_id": event_ids["participation_call"],
        }
        if not apply_changes:
            return preview

        adjustment_events = [
            _combo_adjust_event(
                record=current["put_record"],
                event_id=event_ids["funding_put"],
                group_id=group_id,
                leg_role="funding_put",
                inference_id=inference_value,
                event_time_ms=decision_ms,
            ),
            _combo_adjust_event(
                record=current["call_record"],
                event_id=event_ids["participation_call"],
                group_id=group_id,
                leg_role="participation_call",
                inference_id=inference_value,
                event_time_ms=decision_ms,
            ),
        ]
        for event in adjustment_events:
            sqlite_repo.upsert_trade_event(event, conn=conn)
        events = sqlite_repo.list_trade_events(conn=conn)
        projection = project_stored_trade_events_to_position_lots(events)
        ensure_projection_publishable(projection, operation="post-trade Combo adoption")
        publish_full_position_projection(sqlite_repo, projection.lots, conn=conn)
        projected_by_id = {
            str(item.record_id): item for item in projection.lots
        }
        put_leg = _identity_leg(
            projected_by_id[str(inference["put_record_id"])],
            open_event_id=str(inference["put_open_event_id"]),
        )
        call_leg = _identity_leg(
            projected_by_id[str(inference["call_record_id"])],
            open_event_id=str(inference["call_open_event_id"]),
        )
        intent = build_combo_identity_intent(first_leg=put_leg, second_leg=call_leg)
        identity = identity_from_intent(
            intent,
            first_leg=put_leg,
            second_leg=call_leg,
        )
        membership = resolve_combo_group_membership(
            group_id=group_id,
            account=str(inference["account"]),
            expected_symbol=str(inference["symbol"]),
            trade_events=events,
            projected_position_lots=projection.lots,
        )
        if (
            membership.fact.get("status") != "exact"
            or set(membership.fact.get("current_account_member_record_ids") or [])
            != {str(inference["put_record_id"]), str(inference["call_record_id"])}
        ):
            raise ValueError("post-trade Combo adoption membership is not exact")
        sqlite_repo.insert_strategy_group_identity(identity, conn=conn)
        updated = sqlite_repo.transition_combo_pair_inference(
            inference_id=inference_value,
            expected_statuses=[status],
            new_status="user_confirmed",
            expected_input_hash=hash_value,
            decision_fields={
                "decision_at_ms": decision_ms,
                "decision_by": actor_value,
                "decision_reason": "user_confirmed_exact_pair",
                "strategy_group_id": group_id,
                "identity_hash": identity["identity_hash"],
                "put_adoption_event_id": event_ids["funding_put"],
                "call_adoption_event_id": event_ids["participation_call"],
            },
            conn=conn,
        )
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            **preview,
            "identity": identity,
            "membership": membership.fact,
            "inference": updated,
        }

    return with_sqlite_repo_transaction(
        repo,
        _run,
        require_projection_publication=True,
    )


def reject_post_trade_combo_pair(
    *,
    repo: Any,
    inference_id: str,
    expected_input_hash: str,
    reason: str,
    actor: str,
    effective_now_ms: int | None = None,
) -> dict[str, Any]:
    """Reject one exact pending inference without changing ledger facts."""

    inference_value = str(inference_id or "").strip()
    hash_value = str(expected_input_hash or "").strip()
    reason_value = str(reason or "").strip()
    actor_value = str(actor or "").strip()
    if not all((inference_value, hash_value, reason_value, actor_value)):
        raise ValueError("combo rejection requires inference, hash, reason, and actor")
    decision_ms = int(effective_now_ms or now_ms())

    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        existing = sqlite_repo.get_combo_pair_inference(inference_value, conn=conn)
        if existing is None:
            raise ValueError(f"combo inference not found: {inference_value}")
        if (
            str(existing.get("status") or "").strip().lower() == "user_rejected"
            and str(existing.get("input_snapshot_hash") or "") == hash_value
        ):
            return existing
        return sqlite_repo.transition_combo_pair_inference(
            inference_id=inference_value,
            expected_statuses=sorted(_PENDING_INFERENCE_STATUSES),
            new_status="user_rejected",
            expected_input_hash=hash_value,
            decision_fields={
                "decision_at_ms": decision_ms,
                "decision_by": actor_value,
                "decision_reason": reason_value,
            },
            conn=conn,
        )

    return with_sqlite_repo_transaction(repo, _run)


def supersede_post_trade_combo_pair(
    *,
    repo: Any,
    inference_id: str,
    expected_input_hash: str,
    reason: str,
    actor: str,
    apply_changes: bool = False,
    effective_now_ms: int | None = None,
) -> dict[str, Any]:
    """Atomically void both adoption events and supersede a confirmed inference."""

    inference_value = str(inference_id or "").strip()
    hash_value = str(expected_input_hash or "").strip()
    reason_value = str(reason or "").strip()
    actor_value = str(actor or "").strip()
    if not all((inference_value, hash_value, reason_value)):
        raise ValueError("combo supersede requires inference, hash, and reason")
    if apply_changes and not actor_value:
        raise ValueError("combo supersede apply requires actor")
    decision_ms = int(effective_now_ms or now_ms())

    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        inference = sqlite_repo.get_combo_pair_inference(inference_value, conn=conn)
        if inference is None:
            raise ValueError(f"combo inference not found: {inference_value}")
        if str(inference.get("input_snapshot_hash") or "") != hash_value:
            raise ValueError("combo inference input hash compare-and-set failed")
        status = str(inference.get("status") or "").strip().lower()
        if status == "superseded":
            return {
                "schema_version": "post_trade_combo_supersede.v1",
                "status": "already_superseded",
                "apply_changes": bool(apply_changes),
                "inference": inference,
            }
        if status != "user_confirmed":
            raise ValueError(f"combo inference is not supersedable: {status}")
        group_id = str(inference.get("strategy_group_id") or "").strip()
        adoption_ids = [
            str(inference.get("put_adoption_event_id") or "").strip(),
            str(inference.get("call_adoption_event_id") or "").strip(),
        ]
        if not group_id or not all(adoption_ids):
            raise ValueError("confirmed combo inference adoption evidence is incomplete")
        events = sqlite_repo.list_trade_events(conn=conn)
        by_id = {
            str(item.get("event_id") or "").strip(): dict(item)
            for item in events
            if str(item.get("event_id") or "").strip()
        }
        if any(event_id not in by_id for event_id in adoption_ids):
            raise ValueError("confirmed combo adoption event is missing")
        existing_void_targets = {
            target
            for item in events
            if (target := valid_void_target_event_id(item))
        }
        if any(event_id in existing_void_targets for event_id in adoption_ids):
            raise ValueError("confirmed combo adoption event is already voided")
        membership = resolve_combo_group_membership(
            group_id=group_id,
            account=str(inference["account"]),
            expected_symbol=str(inference["symbol"]),
            trade_events=events,
            projected_position_lots=project_stored_trade_events_to_position_lots(events).lots,
        )
        if membership.fact.get("status") != "exact":
            raise ValueError("confirmed combo membership is no longer exact")
        void_ids = [
            _combo_decision_event_id("combo-supersede", inference_value, "funding_put"),
            _combo_decision_event_id("combo-supersede", inference_value, "participation_call"),
        ]
        preview = {
            "schema_version": "post_trade_combo_supersede.v1",
            "status": "dry_run" if not apply_changes else "superseded",
            "apply_changes": bool(apply_changes),
            "inference_id": inference_value,
            "put_void_event_id": void_ids[0],
            "call_void_event_id": void_ids[1],
        }
        if not apply_changes:
            return preview
        for event_id, target_id in zip(void_ids, adoption_ids, strict=True):
            sqlite_repo.upsert_trade_event(
                _combo_void_event(
                    event_id=event_id,
                    target=by_id[target_id],
                    target_event_id=target_id,
                    inference_id=inference_value,
                    reason=reason_value,
                    event_time_ms=decision_ms,
                ),
                conn=conn,
            )
        events_after = sqlite_repo.list_trade_events(conn=conn)
        projection = project_stored_trade_events_to_position_lots(events_after)
        ensure_projection_publishable(projection, operation="post-trade Combo supersede")
        publish_full_position_projection(sqlite_repo, projection.lots, conn=conn)
        membership_after = resolve_combo_group_membership(
            group_id=group_id,
            account=str(inference["account"]),
            expected_symbol=str(inference["symbol"]),
            trade_events=events_after,
            projected_position_lots=projection.lots,
        )
        if membership_after.fact.get("status") == "exact":
            raise ValueError("superseded Combo membership remains exact")
        updated = sqlite_repo.transition_combo_pair_inference(
            inference_id=inference_value,
            expected_statuses=["user_confirmed"],
            new_status="superseded",
            expected_input_hash=hash_value,
            decision_fields={
                "decision_at_ms": decision_ms,
                "decision_by": actor_value,
                "decision_reason": reason_value,
                "put_void_event_id": void_ids[0],
                "call_void_event_id": void_ids[1],
            },
            conn=conn,
        )
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {**preview, "membership": membership_after.fact, "inference": updated}

    return with_sqlite_repo_transaction(
        repo,
        _run,
        require_projection_publication=True,
    )


def _reconcile_with_repo(
    repo: Any,
    *,
    conn: Any,
    account: str,
    runtime_environment: str,
    exposures: list[dict[str, Any]],
    persist: bool,
    effective_now_ms: int,
) -> dict[str, Any]:
    inference_list = getattr(repo, "list_combo_pair_inferences", None)
    identity_list = getattr(repo, "list_strategy_group_identities", None)
    if not callable(inference_list) or not callable(identity_list):
        raise TypeError("option_positions repo lacks combo reconciliation read methods")
    if persist:
        expire = getattr(repo, "expire_combo_pair_inferences", None)
        if not callable(expire):
            raise TypeError("option_positions repo lacks combo inference expiry")
        expire(
            effective_now_ms=effective_now_ms,
            account=account,
            conn=conn,
        )
    events = repo.list_trade_events(conn=conn)
    lots = repo.list_position_lots(conn=conn)
    identities = identity_list(account=account, conn=conn)
    existing = inference_list(account=account, conn=conn)
    confirmed_open_event_ids = {
        str(item.get(field) or "").strip()
        for item in existing
        if str(item.get("status") or "").strip().lower() == "user_confirmed"
        for field in ("put_open_event_id", "call_open_event_id")
        if str(item.get(field) or "").strip()
    }
    reactivatable_inference_ids = {
        str(item.get("inference_id") or "").strip()
        for item in existing
        if str(item.get("status") or "").strip().lower() == "expired_unresolved"
        and str(item.get("decision_reason") or "").strip()
        == _REACTIVATABLE_STALE_REASON
        and int(item.get("proposal_expires_at_ms") or 0) >= effective_now_ms
        and str(item.get("inference_id") or "").strip()
    }
    forbidden_inference_ids = {
        str(item.get("inference_id") or "").strip()
        for item in existing
        if str(item.get("status") or "").strip().lower()
        in _TERMINAL_PAIR_STATUSES
        or (
            str(item.get("status") or "").strip().lower()
            == "expired_unresolved"
            and str(item.get("inference_id") or "").strip()
            not in reactivatable_inference_ids
        )
        or (
            str(item.get("status") or "").strip().lower()
            in _PENDING_INFERENCE_STATUSES
            and int(item.get("proposal_expires_at_ms") or 0) < effective_now_ms
        )
    }
    effective_identity_open_event_ids = _effective_identity_open_event_ids(
        account=account,
        events=events,
        lots=lots,
        identities=identities,
    )
    lot_facts = _ledger_lot_facts(
        account=account,
        runtime_environment=runtime_environment,
        events=events,
        lots=lots,
        confirmed_open_event_ids=confirmed_open_event_ids,
        effective_identity_open_event_ids=effective_identity_open_event_ids,
    )
    matched = match_post_trade_combo_pairs(
        lots=lot_facts,
        exposures=exposures,
        forbidden_inference_ids=forbidden_inference_ids,
    )
    inserted_count = 0
    reactivated_count = 0
    stale_expired_count = 0
    if persist:
        upsert = getattr(repo, "upsert_combo_pair_inference", None)
        expire_stale = getattr(repo, "expire_stale_combo_pair_inferences", None)
        if not callable(upsert) or not callable(expire_stale):
            raise TypeError("option_positions repo lacks combo inference write methods")
        for inference in matched["inferences"]:
            inference_id = str(inference["inference_id"])
            is_reactivation = inference_id in reactivatable_inference_ids
            inserted_count += int(
                bool(
                    upsert(
                        inference,
                        reactivate_stale=is_reactivation,
                        conn=conn,
                    )
                )
            )
            reactivated_count += int(is_reactivation)
        stale_expired_count = int(
            expire_stale(
                account=account,
                active_inference_ids=[
                    str(item["inference_id"])
                    for item in matched["inferences"]
                ],
                effective_now_ms=effective_now_ms,
                conn=conn,
            )
        )
    return {
        "ok": True,
        "account": account,
        "runtime_environment": runtime_environment,
        "persisted": bool(persist),
        "effective_now_ms": effective_now_ms,
        "ledger_lot_fact_count": len(lot_facts),
        "candidate_exposure_count": len(exposures),
        "inserted_inference_count": inserted_count,
        "reactivated_inference_count": reactivated_count,
        "stale_expired_count": stale_expired_count,
        **matched,
    }


def _ledger_lot_facts(
    *,
    account: str,
    runtime_environment: str,
    events: list[dict[str, Any]],
    lots: list[dict[str, Any]],
    confirmed_open_event_ids: set[str],
    effective_identity_open_event_ids: set[str],
) -> list[dict[str, Any]]:
    events_by_id = {
        str(item.get("event_id") or "").strip(): item
        for item in events
        if str(item.get("event_id") or "").strip()
    }
    open_by_lot = {
        str(item.get("lot_id") or "").strip(): item
        for item in events
        if str(item.get("event_type") or "").strip().lower() == "open"
        and str(item.get("lot_id") or "").strip()
    }
    out: list[dict[str, Any]] = []
    for row in lots:
        fields = dict(row.get("fields") or {})
        if str(fields.get("account") or "").strip().lower() != account:
            continue
        record_id = str(row.get("record_id") or "").strip()
        open_event_id = str(fields.get("source_event_id") or "").strip()
        event = events_by_id.get(open_event_id) or open_by_lot.get(record_id) or {}
        if not open_event_id:
            open_event_id = str(event.get("event_id") or "").strip()
        contract = dict(event.get("contract_key") or {})
        trade_time_ms = _event_time_ms(event, fields)
        symbol = str(
            fields.get("symbol")
            or contract.get("underlying_symbol")
            or ""
        ).strip().upper()
        currency = str(
            fields.get("currency") or event.get("currency") or ""
        ).strip().upper()
        broker = str(
            fields.get("broker") or contract.get("broker") or ""
        ).strip().lower()
        market = _market(symbol=symbol, currency=currency, broker=broker)
        event_runtime_environment = _event_runtime_environment(
            event,
            expected_account=account,
        )
        out.append(
            {
                "record_id": record_id,
                "open_event_id": open_event_id,
                "account": account,
                "broker": broker,
                "runtime_environment": (
                    event_runtime_environment
                    if event_runtime_environment == runtime_environment
                    else ""
                ),
                "market": market,
                "market_date": _market_date(
                    event_time_ms=trade_time_ms,
                    market=market,
                ),
                "symbol": symbol,
                "option_type": str(
                    fields.get("option_type")
                    or contract.get("option_type")
                    or ""
                ).strip().lower(),
                "position_side": str(
                    fields.get("side")
                    or fields.get("position_side")
                    or contract.get("position_side")
                    or ""
                ).strip().lower(),
                "contracts_original": effective_contracts(fields),
                "contracts_open": effective_contracts_open(fields),
                "currency": currency,
                "multiplier": effective_multiplier(fields),
                "strike": effective_strike(fields),
                "expiration_ymd": (
                    str(fields.get("expiration_ymd") or "").strip()
                    or str(contract.get("expiration_ymd") or "").strip()
                    or effective_expiration_ymd(fields)
                ),
                "trade_time_ms": trade_time_ms,
                "strategy": str(
                    fields.get("strategy")
                    or fields.get("strategy_type")
                    or ""
                ).strip().lower(),
                "strategy_group_id": str(
                    fields.get("strategy_group_id") or ""
                ).strip(),
                "leg_role": str(fields.get("leg_role") or "").strip().lower(),
                "effective_combo_identity": (
                    open_event_id in effective_identity_open_event_ids
                ),
                "confirmed_combo_claim": (
                    open_event_id in confirmed_open_event_ids
                ),
            }
        )
    return out


def _event_runtime_environment(
    event: Mapping[str, Any],
    *,
    expected_account: str,
) -> str:
    raw_payload = event.get("raw_payload")
    if not isinstance(raw_payload, Mapping):
        return ""
    raw_context = raw_payload.get("_trade_intake_source")
    if not isinstance(raw_context, Mapping):
        return ""
    context = dict(raw_context)
    if (
        str(context.get("schema_version") or "").strip()
        != "trade_intake_source.v1"
        or str(context.get("transport") or "").strip().lower()
        not in {"push", "poll"}
        or str(context.get("account") or "").strip().lower()
        != str(expected_account or "").strip().lower()
    ):
        return ""
    required_text = (
        "source_id",
        "futu_account_id",
        "opend_process",
        "opend_host",
        "received_at_utc",
    )
    if any(not str(context.get(key) or "").strip() for key in required_text):
        return ""
    port = context.get("opend_port")
    if isinstance(port, bool) or not isinstance(port, int) or port <= 0:
        return ""
    host = str(context["opend_host"]).strip().lower()
    return f"opend:{host}:{port}"


def _effective_identity_open_event_ids(
    *,
    account: str,
    events: list[dict[str, Any]],
    lots: list[dict[str, Any]],
    identities: list[dict[str, Any]],
) -> set[str]:
    out: set[str] = set()
    for identity in identities:
        group_id = str(identity.get("group_id") or "").strip()
        if not group_id:
            continue
        resolved = resolve_combo_group_membership(
            group_id=group_id,
            account=account,
            expected_symbol=str(identity.get("symbol") or "").strip().upper(),
            trade_events=events,
            projected_position_lots=lots,
        )
        if resolved.fact.get("status") != "exact":
            continue
        for field in (
            "funding_put_open_event_id",
            "participation_call_open_event_id",
        ):
            value = str(identity.get(field) or "").strip()
            if value:
                out.add(value)
    return out


def _event_time_ms(event: Mapping[str, Any], fields: Mapping[str, Any]) -> int:
    for value in (
        event.get("event_time_ms"),
        event.get("trade_time_ms"),
        fields.get("opened_at"),
    ):
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0


def _market(*, symbol: str, currency: str, broker: str) -> str:
    if symbol.endswith(".HK") or currency == "HKD" or broker in {"hk", "hkcc"}:
        return "HK"
    if symbol and (currency == "USD" or broker in {"futu", "us"}):
        return "US"
    return ""


def _market_date(*, event_time_ms: int, market: str) -> str:
    timezone_name = RUNTIME_SCHEDULE_TIMEZONE_BY_MARKET.get(market.lower())
    if event_time_ms <= 0 or not timezone_name:
        return ""
    observed = datetime.fromtimestamp(
        event_time_ms / 1000,
        tz=timezone.utc,
    )
    return observed.astimezone(ZoneInfo(timezone_name)).date().isoformat()


def _validate_inference_against_current_ledger(
    repo: Any,
    *,
    conn: Any,
    inference: Mapping[str, Any],
) -> dict[str, Any]:
    events = repo.list_trade_events(conn=conn)
    projection = project_stored_trade_events_to_position_lots(events)
    ensure_projection_publishable(projection, operation="post-trade Combo confirmation precondition")
    comparison = compare_projection_lots(
        projected_lots=list(projection.lots),
        current_lots=repo.list_position_lots(conn=conn),
        diagnostics=list(projection.diagnostics),
    )
    drift = {
        key: int(value)
        for key, value in dict(comparison.get("summary") or {}).items()
        if key != "matched" and int(value) > 0
    }
    if drift:
        raise ValueError("combo confirmation requires a matching trade_events projection")
    runtime_environment = str(inference.get("runtime_environment") or "").strip().lower()
    if not runtime_environment:
        raise ValueError("combo inference runtime environment is missing")
    facts = _ledger_lot_facts(
        account=str(inference.get("account") or "").strip().lower(),
        runtime_environment=runtime_environment,
        events=events,
        lots=[
            {"record_id": item.record_id, "fields": dict(item.fields)}
            for item in projection.lots
        ],
        confirmed_open_event_ids=set(),
        effective_identity_open_event_ids=set(),
    )
    facts_by_record = {str(item["record_id"]): item for item in facts}
    records_by_id = {str(item.record_id): item for item in projection.lots}
    out: dict[str, Any] = {}
    for prefix in ("put", "call"):
        record_id = str(inference.get(f"{prefix}_record_id") or "").strip()
        open_event_id = str(inference.get(f"{prefix}_open_event_id") or "").strip()
        fact = facts_by_record.get(record_id)
        record = records_by_id.get(record_id)
        snapshot = inference.get(f"{prefix}_lot_snapshot")
        if fact is None or record is None or not isinstance(snapshot, Mapping):
            raise ValueError("combo confirmation exact lot is missing")
        if str(fact.get("open_event_id") or "") != open_event_id:
            raise ValueError("combo confirmation open event binding changed")
        snapshot_fields = (
            "record_id",
            "open_event_id",
            "account",
            "broker",
            "runtime_environment",
            "market",
            "market_date",
            "symbol",
            "option_type",
            "position_side",
            "contracts_original",
            "contracts_open",
            "currency",
            "multiplier",
            "strike",
            "expiration_ymd",
            "trade_time_ms",
            "strategy",
            "strategy_group_id",
            "leg_role",
        )
        changed_fields = [
            field
            for field in snapshot_fields
            if not _combo_snapshot_values_equal(
                fact.get(field),
                snapshot.get(field),
                decimal_value=field in {"multiplier", "strike"},
            )
        ]
        if changed_fields:
            raise ValueError(
                "combo confirmation input facts changed: " + ",".join(changed_fields)
            )
        if (
            int(fact.get("contracts_open") or 0)
            != int(fact.get("contracts_original") or 0)
            or str(fact.get("strategy_group_id") or "")
            or str(fact.get("leg_role") or "")
            or str(fact.get("strategy") or "").strip().lower() == "combo_yield"
        ):
            raise ValueError("combo confirmation lot is no longer fully ungrouped")
        out[f"{prefix}_record"] = record
    return out


def _combo_snapshot_values_equal(
    left: Any,
    right: Any,
    *,
    decimal_value: bool,
) -> bool:
    if decimal_value:
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except (InvalidOperation, TypeError, ValueError):
            return False
    return str(left if left is not None else "") == str(right if right is not None else "")


def _combo_decision_event_id(prefix: str, inference_id: str, role: str) -> str:
    return f"{prefix}:v1:" + canonical_sha256(
        {"inference_id": inference_id, "role": role}
    )


def _combo_adjust_event(
    *,
    record: Any,
    event_id: str,
    group_id: str,
    leg_role: str,
    inference_id: str,
    event_time_ms: int,
) -> TradeEvent:
    fields = dict(record.fields)
    patch = build_open_adjustment_patch_contract(
        fields,
        strategy="combo_yield",
        leg_role=leg_role,
        strategy_group_id=group_id,
        as_of_ms=event_time_ms,
    )
    contract_key = ContractKey.from_values(
        broker=fields.get("broker"),
        account=fields.get("account"),
        underlying_symbol=fields.get("symbol"),
        option_type=fields.get("option_type"),
        position_side=fields.get("side"),
        strike=fields.get("strike"),
        expiration_ymd=effective_expiration_ymd(fields),
    )
    return TradeEvent(
        event_id=event_id,
        event_type="adjust",
        event_time_ms=event_time_ms,
        contract_key=contract_key,
        contracts=0,
        price=0.0,
        currency=str(fields.get("currency") or ""),
        source="post_trade_combo_reconciliation",
        multiplier=float(effective_multiplier(fields) or 100.0),
        target_lot_id=str(record.record_id),
        raw_payload={
            "source": "post_trade_combo_reconciliation",
            "source_type": "combo_pair_inference",
            "mode": "post_trade_combo_adoption",
            "inference_id": inference_id,
            "record_id": str(record.record_id),
            "target_lot_id": str(record.record_id),
            "adjust_target_source_event_id": str(fields.get("source_event_id") or ""),
            "idempotency_key": event_id,
            "patch": patch.to_dict(),
        },
    )


def _identity_leg(record: Any, *, open_event_id: str) -> dict[str, Any]:
    fields = dict(record.fields)
    contract_key = ContractKey.from_values(
        broker=fields.get("broker"),
        account=fields.get("account"),
        underlying_symbol=fields.get("symbol"),
        option_type=fields.get("option_type"),
        position_side=fields.get("side"),
        strike=fields.get("strike"),
        expiration_ymd=effective_expiration_ymd(fields),
    )
    return {
        "strategy_group_id": str(fields.get("strategy_group_id") or "").strip(),
        "strategy": str(fields.get("strategy") or "").strip().lower(),
        "broker": str(fields.get("broker") or "").strip().lower(),
        "account": str(fields.get("account") or "").strip().lower(),
        "symbol": str(fields.get("symbol") or "").strip().upper(),
        "leg_role": str(fields.get("leg_role") or "").strip().lower(),
        "contracts": int(effective_contracts(fields) or 0),
        "open_event_id": str(open_event_id or "").strip(),
        "record_id": str(record.record_id),
        "contract_key": contract_key.to_dict(),
        "currency": str(fields.get("currency") or "").strip().upper(),
        "multiplier": float(effective_multiplier(fields) or 0),
        "strike": float(effective_strike(fields) or 0),
        "expiration_ymd": effective_expiration_ymd(fields),
    }


def _combo_void_event(
    *,
    event_id: str,
    target: Mapping[str, Any],
    target_event_id: str,
    inference_id: str,
    reason: str,
    event_time_ms: int,
) -> TradeEvent:
    contract = dict(target.get("contract_key") or {})
    return TradeEvent(
        event_id=event_id,
        event_type="void",
        event_time_ms=event_time_ms,
        contract_key=ContractKey.from_values(
            broker=contract.get("broker"),
            account=contract.get("account"),
            underlying_symbol=contract.get("underlying_symbol"),
            option_type=contract.get("option_type"),
            position_side=contract.get("position_side"),
            strike=contract.get("strike"),
            expiration_ymd=contract.get("expiration_ymd"),
        ),
        contracts=0,
        price=0.0,
        currency=str(target.get("currency") or ""),
        source="post_trade_combo_reconciliation",
        multiplier=float(target.get("multiplier") or 100.0),
        target_event_id=target_event_id,
        raw_payload={
            "source": "post_trade_combo_reconciliation",
            "source_type": "combo_pair_inference",
            "mode": "post_trade_combo_supersede",
            "inference_id": inference_id,
            "void_target_event_id": target_event_id,
            "void_reason": reason,
            "idempotency_key": event_id,
        },
    )


__all__ = [
    "adopt_post_trade_combo_pair",
    "list_combo_pair_inferences",
    "reject_post_trade_combo_pair",
    "reconcile_combo_pair_inferences",
    "supersede_post_trade_combo_pair",
]
