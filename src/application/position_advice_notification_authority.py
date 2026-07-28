from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.position_advice_authority import (
    normalize_account_label,
    scope_for,
)
from src.application.position_advice_authority_service import (
    read_authority_resolution_under_lock,
)
from src.infrastructure.io_utils import atomic_write_json
from src.infrastructure.position_advice_manifest_lock import (
    manifest_file_lock,
    portfolio_scope_state_dir,
    position_advice_manifest_locks,
)


NOTIFICATION_AUTHORITY_TOKEN_SCHEMA = (
    "position_advice_notification_authority_token.v1"
)
NOTIFICATION_AUTHORITY_RECEIPT_SCHEMA = (
    "position_advice_notification_authority_receipt.v1"
)
NOTIFICATION_AUTHORITY_RESOLUTION_SCHEMA = (
    "position_advice_notification_authority_resolution.v1"
)
NOTIFICATION_INFLIGHT_LEASE_SECONDS = 300


class PositionAdviceNotificationAuthorityError(RuntimeError):
    """Raised when a scheduled notification cannot prove one authority."""


def build_notification_authority_token(
    *,
    normalized_account: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    selected_advice_contract: str,
    resolved_mode: str,
    authority_generation: int | None,
    authority_policy_hash: str | None,
    account_run_id: str,
) -> dict[str, Any]:
    account = normalize_account_label(normalized_account)
    selected = str(selected_advice_contract or "").strip().lower()
    mode = str(resolved_mode or "").strip()
    if selected not in {"v1", "v2"}:
        raise ValueError("selected advice contract must be v1 or v2")
    if mode not in {"v1", "v2_shadow", "v2"}:
        raise ValueError("resolved authority mode is invalid")
    if selected == "v2" and mode != "v2":
        raise ValueError("v2 notification requires v2 authority")
    if selected == "v1" and mode not in {"v1", "v2_shadow"}:
        raise ValueError("v1 notification is not selected by authority")
    generation = int(authority_generation or 0)
    policy_hash = str(authority_policy_hash or "").strip() or None
    if generation < 0:
        raise ValueError("authority generation is invalid")
    if generation > 0 and (policy_hash is None or len(policy_hash) != 64):
        raise ValueError("authority policy hash is invalid")
    payload = {
        "schema_version": NOTIFICATION_AUTHORITY_TOKEN_SCHEMA,
        "normalized_account": account,
        "portfolio_scope_id": scope_for(account),
        "normalized_portfolio_source": str(
            normalized_portfolio_source or ""
        ).strip().lower(),
        "portfolio_account_identity_hash": str(
            portfolio_account_identity_hash or ""
        ).strip(),
        "selected_advice_contract": selected,
        "resolved_mode": mode,
        "authority_generation": generation,
        "authority_policy_hash": policy_hash,
        "account_run_id": str(account_run_id or "").strip(),
    }
    if (
        not payload["normalized_portfolio_source"]
        or len(payload["portfolio_account_identity_hash"]) != 64
        or not payload["account_run_id"]
    ):
        raise ValueError("notification authority identity is incomplete")
    return {**payload, "token_hash": canonical_sha256(payload)}


def execute_notification_with_authority(
    *,
    base: Path,
    token: Mapping[str, Any],
    channel: str,
    send: Callable[[], Mapping[str, Any]],
    delivery_identity: Mapping[str, Any] | None = None,
    now: datetime | str | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Hold authority and notification locks through the bounded provider send."""

    item = _validate_token(token)
    checked_at = _timestamp(now or datetime.now(timezone.utc))
    scope_id = str(item["portfolio_scope_id"])
    channel_value = str(channel or "").strip().lower()
    if not channel_value:
        raise ValueError("notification channel is required")
    delivery_identity_value = _validate_delivery_identity(
        delivery_identity,
        expected_account=str(item["normalized_account"]),
    )
    generation = int(item["authority_generation"])
    notification_key = canonical_sha256(
        {
            "portfolio_scope_id": scope_id,
            "authority_generation": generation,
            "account_run_id": item["account_run_id"],
            "channel": channel_value,
        }
    )
    state_dir = (
        portfolio_scope_state_dir(base, scope_id)
        / "notification_authority"
    )
    with position_advice_manifest_locks(
        base=base,
        portfolio_scope_id=scope_id,
        global_mode="shared",
        scope_mode="shared",
        timeout_seconds=timeout_seconds,
    ):
        resolution = read_authority_resolution_under_lock(
            base=base,
            normalized_account=str(item["normalized_account"]),
            normalized_portfolio_source=str(
                item["normalized_portfolio_source"]
            ),
            portfolio_account_identity_hash=str(
                item["portfolio_account_identity_hash"]
            ),
        )
        _assert_token_matches_resolution(item, resolution)
        with manifest_file_lock(
            state_dir / ".send.lock",
            mode="exclusive",
            timeout_seconds=timeout_seconds,
        ):
            existing = _existing_terminal(state_dir, notification_key)
            if existing is not None:
                status, receipt = existing
                if status == "accepted":
                    return _duplicate_suppressed_result(
                        account=str(item["normalized_account"]),
                        receipt=receipt,
                    )
                return _blocked_result(
                    account=str(item["normalized_account"]),
                    receipt_id=str(receipt["receipt_id"]),
                    error_code="AUTHORITY_NOTIFICATION_UNKNOWN",
                )
            inflight_state = _inflight_state_for_dedupe(
                state_dir,
                notification_key,
            )
            if inflight_state is not None:
                inflight_status, unresolved_intent = inflight_state
                if inflight_status == "delivered":
                    return _duplicate_suppressed_result(
                        account=str(item["normalized_account"]),
                        receipt=unresolved_intent,
                    )
                return _blocked_result(
                    account=str(item["normalized_account"]),
                    receipt_id=str(unresolved_intent["receipt_id"]),
                    error_code="AUTHORITY_NOTIFICATION_INFLIGHT",
                )

            attempt_number = _next_attempt_number(
                state_dir,
                notification_key,
            )
            receipt_id = canonical_sha256(
                {
                    "notification_key": notification_key,
                    "attempt_number": attempt_number,
                }
            )
            intent = {
                "schema_version": NOTIFICATION_AUTHORITY_RECEIPT_SCHEMA,
                "receipt_id": receipt_id,
                "notification_key": notification_key,
                "attempt_number": attempt_number,
                "status": "inflight",
                "portfolio_scope_id": scope_id,
                "normalized_account": item["normalized_account"],
                "selected_advice_contract": item[
                    "selected_advice_contract"
                ],
                "resolved_mode": resolution.mode,
                "authority_generation": resolution.generation,
                "authority_policy_hash": resolution.policy_hash,
                "account_run_id": item["account_run_id"],
                "channel": channel_value,
                "token_hash": item["token_hash"],
                "delivery_identity": delivery_identity_value,
                "recorded_at": checked_at,
                "lease_expires_at": _timestamp(
                    _parse_timestamp(checked_at)
                    + timedelta(
                        seconds=NOTIFICATION_INFLIGHT_LEASE_SECONDS
                    )
                ),
            }
            _write_once_or_verify(
                _attempt_receipt_path(
                    state_dir,
                    "inflight",
                    notification_key,
                    attempt_number,
                ),
                intent,
            )
            try:
                result = dict(send() or {})
            except Exception as exc:
                result = {
                    "ok": False,
                    "account": item["normalized_account"],
                    "command_ok": False,
                    "delivery_confirmed": False,
                    "error_code": "SEND_EXCEPTION",
                    "exception_type": type(exc).__name__,
                    "error_message": str(exc),
                }

            terminal_status = _terminal_status(result)
            terminal = {
                **intent,
                "status": terminal_status,
                "completed_at": _timestamp(datetime.now(timezone.utc)),
                "provider_idempotency_key": result.get("idempotency_key"),
                "message_id": result.get("message_id"),
                "upstream_message_id": result.get("upstream_message_id"),
                "command_ok": bool(result.get("command_ok")),
                "delivery_confirmed": bool(
                    result.get("delivery_confirmed")
                ),
                "error_code": result.get("error_code"),
                "ambiguous_send": bool(result.get("ambiguous_send")),
                "duplicate_risk": bool(result.get("duplicate_risk")),
            }
            terminal["receipt_hash"] = canonical_sha256(terminal)
            _write_once_or_verify(
                state_dir
                / terminal_status
                / f"{receipt_id}.json",
                terminal,
            )
            return {
                **result,
                "authority_receipt_id": receipt_id,
                "authority_receipt_status": terminal_status,
            }


def resolve_notification_unknown(
    *,
    base: Path,
    normalized_account: str,
    receipt_id: str,
    resolution: str,
    evidence: Mapping[str, Any],
    actor: str,
    resolved_at: datetime | str,
    confirm: bool,
    dry_run: bool = True,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    account = normalize_account_label(normalized_account)
    scope_id = scope_for(account)
    receipt = str(receipt_id or "").strip()
    outcome = str(resolution or "").strip().lower()
    actor_value = str(actor or "").strip()
    if outcome not in {"delivered", "failed"}:
        raise ValueError("notification resolution must be delivered or failed")
    if len(receipt) != 64 or not actor_value:
        raise ValueError("receipt id and actor are required")
    evidence_payload = dict(evidence or {})
    if not evidence_payload:
        raise ValueError("delivery resolution evidence is required")
    state_dir = (
        portfolio_scope_state_dir(base, scope_id)
        / "notification_authority"
    )
    with position_advice_manifest_locks(
        base=base,
        portfolio_scope_id=scope_id,
        global_mode="shared",
        scope_mode="exclusive",
        timeout_seconds=timeout_seconds,
    ):
        receipt_kind, source_receipt = _resolution_source_receipt(
            state_dir,
            receipt,
        )
        source_receipt_hash = str(
            source_receipt.get("receipt_hash") or ""
        ).strip() or canonical_sha256(source_receipt)
        delivery_identity = source_receipt.get("delivery_identity")
        if delivery_identity is not None and not isinstance(
            delivery_identity,
            Mapping,
        ):
            raise PositionAdviceNotificationAuthorityError(
                "notification delivery identity is invalid"
            )
        resolved_at_value = _timestamp(resolved_at)
        if receipt_kind == "inflight":
            lease_expires_at = _inflight_lease_expires_at(source_receipt)
            if _parse_timestamp(resolved_at_value) < _parse_timestamp(
                lease_expires_at
            ):
                raise PositionAdviceNotificationAuthorityError(
                    "notification inflight lease has not expired"
                )
        payload = {
            "schema_version": NOTIFICATION_AUTHORITY_RESOLUTION_SCHEMA,
            "receipt_id": receipt,
            "source_receipt_kind": receipt_kind,
            "source_receipt_hash": source_receipt_hash,
            "resolution": outcome,
            "evidence": evidence_payload,
            "evidence_hash": canonical_sha256(evidence_payload),
            "actor": actor_value,
            "resolved_at": resolved_at_value,
            "delivery_identity": (
                dict(delivery_identity)
                if isinstance(delivery_identity, Mapping)
                else None
            ),
        }
        payload["resolution_hash"] = canonical_sha256(payload)
        path = state_dir / "resolutions" / f"{receipt}.json"
        existing = _read_json_object(path) if path.exists() else None
        if existing is not None and not _same_resolution_request(
            existing=existing,
            outcome=outcome,
            evidence=evidence_payload,
            actor=actor_value,
        ):
            raise PositionAdviceNotificationAuthorityError(
                "notification resolution conflicts with existing receipt"
            )
        delivery_reconciliation = _reconcile_delivery_identity(
            base=base,
            account=account,
            delivery_identity=delivery_identity,
            outcome=outcome,
            resolved_at=resolved_at_value,
            dry_run=True,
        )
        plan = {
            "schema_version": (
                "position_advice_notification_resolution_plan.v1"
            ),
            "status": "ready",
            "dry_run": bool(dry_run),
            "would_change": existing is None,
            "resolution_receipt": existing or payload,
            "resolution_path": str(path),
            "delivery_reconciliation": delivery_reconciliation,
        }
        if dry_run:
            return plan
        if confirm is not True:
            raise PositionAdviceNotificationAuthorityError(
                "notification resolution apply requires explicit confirm"
            )
        if existing is not None:
            return {
                **plan,
                "status": "already_applied",
                "dry_run": False,
                "would_change": False,
            }
        delivery_reconciliation = _reconcile_delivery_identity(
            base=base,
            account=account,
            delivery_identity=delivery_identity,
            outcome=outcome,
            resolved_at=resolved_at_value,
            dry_run=False,
        )
        _write_once_or_verify(path, payload)
        return {
            **plan,
            "status": "applied",
            "dry_run": False,
            "delivery_reconciliation": delivery_reconciliation,
        }


def unresolved_notification_authority_exists(
    *,
    base: Path,
    portfolio_scope_id: str,
) -> bool:
    state_dir = (
        portfolio_scope_state_dir(base, portfolio_scope_id)
        / "notification_authority"
    )
    for path in (state_dir / "inflight").glob("*.json"):
        if (
            not _inflight_has_terminal(state_dir, path)
            and _notification_resolution_outcome(
                state_dir,
                _receipt_id_from_path_payload(path),
            )
            not in {"delivered", "failed"}
        ):
            return True
    for path in (state_dir / "unknown").glob("*.json"):
        if _notification_resolution_outcome(
            state_dir,
            path.stem,
        ) not in {"delivered", "failed"}:
            return True
    return False


def _validate_token(token: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(token or {})
    actual = item.pop("token_hash", None)
    if item.get("schema_version") != NOTIFICATION_AUTHORITY_TOKEN_SCHEMA:
        raise PositionAdviceNotificationAuthorityError(
            "notification authority token schema is invalid"
        )
    if actual != canonical_sha256(item):
        raise PositionAdviceNotificationAuthorityError(
            "notification authority token hash mismatch"
        )
    return {**item, "token_hash": actual}


def _assert_token_matches_resolution(
    token: Mapping[str, Any],
    resolution: Any,
) -> None:
    if resolution.resolution_status not in {
        "resolved",
        "first_use_default_v1",
    }:
        raise PositionAdviceNotificationAuthorityError(
            "notification authority conflict"
        )
    selected = token["selected_advice_contract"]
    if selected == "v2" and resolution.mode != "v2":
        raise PositionAdviceNotificationAuthorityError(
            "v2 notification authority is inactive"
        )
    if selected == "v1" and resolution.mode not in {"v1", "v2_shadow"}:
        raise PositionAdviceNotificationAuthorityError(
            "v1 notification authority is inactive"
        )
    token_generation = int(token["authority_generation"])
    if resolution.generation is None:
        if token_generation != 0 or token.get("authority_policy_hash") is not None:
            raise PositionAdviceNotificationAuthorityError(
                "first-use authority token mismatch"
            )
    elif (
        resolution.generation != token_generation
        or resolution.policy_hash != token.get("authority_policy_hash")
        or resolution.mode != token.get("resolved_mode")
    ):
        raise PositionAdviceNotificationAuthorityError(
            "notification authority generation changed"
        )


def _terminal_status(result: Mapping[str, Any]) -> str:
    if result.get("ok") is True and result.get("delivery_confirmed") is True:
        return "accepted"
    if (
        result.get("ambiguous_send") is True
        or result.get("duplicate_risk") is True
        or str(result.get("error_code") or "")
        in {"SEND_TIMEOUT", "SEND_UNCONFIRMED"}
    ):
        return "unknown"
    return "failed"


def _existing_terminal(
    state_dir: Path,
    notification_key: str,
) -> tuple[str, dict[str, Any]] | None:
    accepted: list[dict[str, Any]] = []
    unresolved_unknown: list[dict[str, Any]] = []
    for status in ("accepted", "unknown"):
        for path in (state_dir / status).glob("*.json"):
            receipt = _read_json_object(path)
            key = str(
                receipt.get("notification_key")
                or receipt.get("receipt_id")
                or ""
            ).strip()
            if key != notification_key:
                continue
            if status == "accepted":
                accepted.append(receipt)
                continue
            resolution = _read_notification_resolution(
                state_dir,
                str(receipt.get("receipt_id") or ""),
            )
            if (
                resolution is not None
                and resolution.get("resolution") == "delivered"
            ):
                accepted.append(receipt)
            elif (
                resolution is None
                or resolution.get("resolution") != "failed"
            ):
                unresolved_unknown.append(receipt)
    if accepted:
        return "accepted", max(accepted, key=_receipt_attempt_number)
    if unresolved_unknown:
        return (
            "unknown",
            max(unresolved_unknown, key=_receipt_attempt_number),
        )
    return None


def _attempt_receipt_path(
    state_dir: Path,
    status: str,
    receipt_id: str,
    attempt_number: int,
) -> Path:
    return (
        state_dir
        / status
        / f"{receipt_id}.{int(attempt_number)}.json"
    )


def _next_attempt_number(state_dir: Path, receipt_id: str) -> int:
    attempts: set[int] = set()
    for path in (state_dir / "inflight").glob(f"{receipt_id}.*.json"):
        try:
            attempts.add(int(path.stem.rsplit(".", 1)[1]))
        except (IndexError, ValueError):
            continue
    return max(attempts, default=0) + 1


def _inflight_state_for_dedupe(
    state_dir: Path,
    notification_key: str,
) -> tuple[str, dict[str, Any]] | None:
    delivered: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for path in (state_dir / "inflight").glob(
        f"{notification_key}*.json"
    ):
        if _inflight_has_terminal(state_dir, path):
            continue
        intent = _read_json_object(path)
        outcome = _notification_resolution_outcome(
            state_dir,
            str(intent.get("receipt_id") or ""),
        )
        if outcome == "delivered":
            delivered.append(intent)
        elif outcome != "failed":
            unresolved.append(intent)
    if delivered:
        return "delivered", max(delivered, key=_receipt_attempt_number)
    return (
        ("unresolved", max(unresolved, key=_receipt_attempt_number))
        if unresolved
        else None
    )


def _inflight_has_terminal(state_dir: Path, path: Path) -> bool:
    try:
        intent = _read_json_object(path)
    except (OSError, ValueError, PositionAdviceNotificationAuthorityError):
        return False
    receipt_id = str(intent.get("receipt_id") or "").strip()
    if len(receipt_id) != 64:
        return False
    if any(
        (state_dir / status / f"{receipt_id}.json").is_file()
        for status in ("accepted", "unknown", "failed")
    ):
        return True
    notification_key = str(
        intent.get("notification_key") or receipt_id
    ).strip()
    attempt_number = _receipt_attempt_number(intent)
    return _attempt_receipt_path(
        state_dir,
        "failed",
        notification_key,
        attempt_number,
    ).is_file()


def _duplicate_suppressed_result(
    *,
    account: str,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "ok": True,
        "account": account,
        "delivery_confirmed": True,
        "command_ok": True,
        "authority_duplicate_suppressed": True,
        "authority_receipt_id": receipt.get("receipt_id"),
        "authority_receipt_status": "accepted",
        "message_id": receipt.get("message_id"),
        "idempotency_key": receipt.get("provider_idempotency_key")
        or (receipt.get("delivery_identity") or {}).get(
            "transport_idempotency_key"
        ),
        "attempts": 0,
        "retry_attempt_count": 0,
    }


def _blocked_result(
    *,
    account: str,
    receipt_id: str,
    error_code: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "account": account,
        "command_ok": False,
        "delivery_confirmed": False,
        "error_code": error_code,
        "attempts": 0,
        "retry_attempt_count": 0,
        "ambiguous_send": True,
        "duplicate_risk": True,
        "authority_receipt_id": receipt_id,
        "authority_receipt_status": "unknown",
    }


def _validate_delivery_identity(
    raw: Mapping[str, Any] | None,
    *,
    expected_account: str,
) -> dict[str, Any] | None:
    if raw is None:
        return None
    item = dict(raw)
    required = {
        "account",
        "market",
        "market_trading_date",
        "delivery_key",
        "source_digest",
        "message_sha256",
        "transport_idempotency_key",
    }
    if any(not str(item.get(field) or "").strip() for field in required):
        raise ValueError("daily brief delivery identity is incomplete")
    account = normalize_account_label(str(item["account"]))
    if account != normalize_account_label(expected_account):
        raise ValueError("daily brief delivery identity account mismatch")
    from src.application.notification_delivery_adapter import (
        build_notification_transport_key,
    )

    delivery_key = str(item["delivery_key"]).strip()
    transport_key = str(item["transport_idempotency_key"]).strip()
    if transport_key != build_notification_transport_key(delivery_key):
        raise ValueError("daily brief delivery identity transport key mismatch")
    for field in ("source_digest", "message_sha256"):
        value = str(item[field]).strip()
        if len(value) != 64:
            raise ValueError(f"daily brief delivery identity {field} is invalid")
    return {
        "account": account,
        "market": str(item["market"]).strip().upper(),
        "market_trading_date": str(
            item["market_trading_date"]
        ).strip(),
        "delivery_key": delivery_key,
        "source_digest": str(item["source_digest"]).strip(),
        "message_sha256": str(item["message_sha256"]).strip(),
        "transport_idempotency_key": transport_key,
    }


def _resolution_source_receipt(
    state_dir: Path,
    receipt_id: str,
) -> tuple[str, dict[str, Any]]:
    unknown_path = state_dir / "unknown" / f"{receipt_id}.json"
    if unknown_path.is_file():
        return "unknown", _read_json_object(unknown_path)
    for path in (state_dir / "inflight").glob("*.json"):
        intent = _read_json_object(path)
        if str(intent.get("receipt_id") or "").strip() != receipt_id:
            continue
        if _inflight_has_terminal(state_dir, path):
            raise PositionAdviceNotificationAuthorityError(
                "notification inflight receipt already has a terminal result"
            )
        return "inflight", intent
    raise PositionAdviceNotificationAuthorityError(
        "notification authority receipt is unavailable"
    )


def _inflight_lease_expires_at(receipt: Mapping[str, Any]) -> str:
    explicit = str(receipt.get("lease_expires_at") or "").strip()
    if explicit:
        return _timestamp(explicit)
    recorded_at = _parse_timestamp(
        str(receipt.get("recorded_at") or "")
    )
    return _timestamp(
        recorded_at
        + timedelta(seconds=NOTIFICATION_INFLIGHT_LEASE_SECONDS)
    )


def _notification_resolution_outcome(
    state_dir: Path,
    receipt_id: str,
) -> str | None:
    try:
        payload = _read_notification_resolution(state_dir, receipt_id)
    except (
        OSError,
        ValueError,
        PositionAdviceNotificationAuthorityError,
    ):
        return None
    if payload is None:
        return None
    outcome = str(payload.get("resolution") or "").strip().lower()
    return outcome if outcome in {"delivered", "failed"} else None


def _receipt_id_from_path_payload(path: Path) -> str:
    try:
        return str(
            _read_json_object(path).get("receipt_id") or ""
        ).strip()
    except (
        OSError,
        ValueError,
        PositionAdviceNotificationAuthorityError,
    ):
        return ""


def _same_resolution_request(
    *,
    existing: Mapping[str, Any],
    outcome: str,
    evidence: Mapping[str, Any],
    actor: str,
) -> bool:
    return (
        str(existing.get("resolution") or "") == outcome
        and existing.get("evidence_hash") == canonical_sha256(evidence)
        and str(existing.get("actor") or "") == actor
    )


def _reconcile_delivery_identity(
    *,
    base: Path,
    account: str,
    delivery_identity: Any,
    outcome: str,
    resolved_at: str,
    dry_run: bool,
) -> dict[str, Any]:
    if delivery_identity is None:
        return {
            "available": False,
            "reason": "legacy_receipt_without_delivery_identity",
            "dry_run": bool(dry_run),
        }
    identity = _validate_delivery_identity(
        delivery_identity,
        expected_account=account,
    )
    assert identity is not None
    from src.application.daily_decision_brief_repository import (
        reconcile_daily_decision_brief_delivery_resolution,
    )

    result = reconcile_daily_decision_brief_delivery_resolution(
        base=base,
        account=identity["account"],
        market=identity["market"],
        market_trading_date=identity["market_trading_date"],
        delivery_key=identity["delivery_key"],
        source_digest=identity["source_digest"],
        message_sha256=identity["message_sha256"],
        transport_idempotency_key=identity[
            "transport_idempotency_key"
        ],
        resolution=outcome,
        resolved_at_utc=resolved_at,
        dry_run=dry_run,
    )
    return {"available": True, **result}


def _read_notification_resolution(
    state_dir: Path,
    receipt_id: str,
) -> dict[str, Any] | None:
    if len(receipt_id) != 64:
        return None
    path = state_dir / "resolutions" / f"{receipt_id}.json"
    return _read_json_object(path) if path.is_file() else None


def _receipt_attempt_number(receipt: Mapping[str, Any]) -> int:
    value = receipt.get("attempt_number")
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _write_once_or_verify(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    if target.exists():
        if _read_json_object(target) != dict(payload):
            raise PositionAdviceNotificationAuthorityError(
                "immutable notification authority receipt conflicts"
            )
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, dict(payload), sort_keys=True)


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise PositionAdviceNotificationAuthorityError(
            f"notification authority receipt is unavailable: {path}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PositionAdviceNotificationAuthorityError(
            "notification authority receipt is unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise PositionAdviceNotificationAuthorityError(
            "notification authority receipt must be an object"
        )
    return payload


def _timestamp(value: datetime | str) -> str:
    parsed = _parse_timestamp(value)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return parsed.astimezone(timezone.utc)


__all__ = [
    "NOTIFICATION_AUTHORITY_RECEIPT_SCHEMA",
    "NOTIFICATION_AUTHORITY_RESOLUTION_SCHEMA",
    "NOTIFICATION_AUTHORITY_TOKEN_SCHEMA",
    "PositionAdviceNotificationAuthorityError",
    "build_notification_authority_token",
    "execute_notification_with_authority",
    "resolve_notification_unknown",
    "unresolved_notification_authority_exists",
]
