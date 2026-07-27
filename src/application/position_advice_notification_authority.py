from __future__ import annotations

import json
from datetime import datetime, timezone
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
    generation = int(item["authority_generation"])
    dedupe_key = canonical_sha256(
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
            existing = _existing_terminal(state_dir, dedupe_key)
            if existing is not None:
                status, receipt = existing
                if status == "accepted":
                    return {
                        "ok": True,
                        "account": item["normalized_account"],
                        "delivery_confirmed": True,
                        "command_ok": True,
                        "authority_duplicate_suppressed": True,
                        "authority_receipt_id": dedupe_key,
                        "authority_receipt_status": status,
                        "message_id": receipt.get("message_id"),
                        "idempotency_key": receipt.get(
                            "provider_idempotency_key"
                        ),
                        "attempts": 0,
                        "retry_attempt_count": 0,
                    }
                if status == "unknown" and not _notification_is_resolved(
                    state_dir, dedupe_key
                ):
                    return _blocked_result(
                        account=str(item["normalized_account"]),
                        dedupe_key=dedupe_key,
                        error_code="AUTHORITY_NOTIFICATION_UNKNOWN",
                    )
                return _blocked_result(
                    account=str(item["normalized_account"]),
                    dedupe_key=dedupe_key,
                    error_code="AUTHORITY_NOTIFICATION_RESOLVED",
                )
            if _unresolved_inflight_for_dedupe(state_dir, dedupe_key):
                return _blocked_result(
                    account=str(item["normalized_account"]),
                    dedupe_key=dedupe_key,
                    error_code="AUTHORITY_NOTIFICATION_INFLIGHT",
                )

            attempt_number = _next_attempt_number(state_dir, dedupe_key)
            intent = {
                "schema_version": NOTIFICATION_AUTHORITY_RECEIPT_SCHEMA,
                "receipt_id": dedupe_key,
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
                "recorded_at": checked_at,
            }
            _write_once_or_verify(
                _attempt_receipt_path(
                    state_dir,
                    "inflight",
                    dedupe_key,
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
                (
                    _attempt_receipt_path(
                        state_dir,
                        terminal_status,
                        dedupe_key,
                        attempt_number,
                    )
                    if terminal_status == "failed"
                    else state_dir
                    / terminal_status
                    / f"{dedupe_key}.json"
                ),
                terminal,
            )
            return {
                **result,
                "authority_receipt_id": dedupe_key,
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
        unknown_path = state_dir / "unknown" / f"{receipt}.json"
        unknown = _read_json_object(unknown_path)
        payload = {
            "schema_version": NOTIFICATION_AUTHORITY_RESOLUTION_SCHEMA,
            "receipt_id": receipt,
            "unknown_receipt_hash": unknown.get("receipt_hash"),
            "resolution": outcome,
            "evidence": evidence_payload,
            "evidence_hash": canonical_sha256(evidence_payload),
            "actor": actor_value,
            "resolved_at": _timestamp(resolved_at),
        }
        payload["resolution_hash"] = canonical_sha256(payload)
        path = state_dir / "resolutions" / f"{receipt}.json"
        existing = _read_json_object(path) if path.exists() else None
        if existing is not None and existing != payload:
            raise PositionAdviceNotificationAuthorityError(
                "notification resolution conflicts with existing receipt"
            )
        plan = {
            "schema_version": (
                "position_advice_notification_resolution_plan.v1"
            ),
            "status": "ready",
            "dry_run": bool(dry_run),
            "would_change": existing is None,
            "resolution_receipt": payload,
            "resolution_path": str(path),
        }
        if dry_run:
            return plan
        if confirm is not True:
            raise PositionAdviceNotificationAuthorityError(
                "notification resolution apply requires explicit confirm"
            )
        _write_once_or_verify(path, payload)
        return {**plan, "status": "applied", "dry_run": False}


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
        if not _inflight_has_terminal(state_dir, path):
            return True
    for path in (state_dir / "unknown").glob("*.json"):
        if not _notification_is_resolved(state_dir, path.stem):
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
        or result.get("command_ok") is True
        or str(result.get("error_code") or "")
        in {"SEND_TIMEOUT", "SEND_UNCONFIRMED"}
    ):
        return "unknown"
    return "failed"


def _existing_terminal(
    state_dir: Path,
    receipt_id: str,
) -> tuple[str, dict[str, Any]] | None:
    matches: list[tuple[str, dict[str, Any]]] = []
    for status in ("accepted", "unknown"):
        path = state_dir / status / f"{receipt_id}.json"
        if path.exists():
            matches.append((status, _read_json_object(path)))
    if len(matches) > 1:
        raise PositionAdviceNotificationAuthorityError(
            "notification authority terminal receipts conflict"
        )
    return matches[0] if matches else None


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
    for status in ("inflight", "failed"):
        for path in (state_dir / status).glob(f"{receipt_id}.*.json"):
            try:
                attempts.add(int(path.stem.rsplit(".", 1)[1]))
            except (IndexError, ValueError):
                continue
    return max(attempts, default=0) + 1


def _unresolved_inflight_for_dedupe(
    state_dir: Path,
    receipt_id: str,
) -> bool:
    return any(
        not _inflight_has_terminal(state_dir, path)
        for path in (state_dir / "inflight").glob(f"{receipt_id}*.json")
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
        for status in ("accepted", "unknown")
    ):
        return True
    attempt_number = intent.get("attempt_number")
    if isinstance(attempt_number, bool):
        return False
    try:
        attempt = int(attempt_number)
    except (TypeError, ValueError, OverflowError):
        return False
    return _attempt_receipt_path(
        state_dir,
        "failed",
        receipt_id,
        attempt,
    ).is_file()


def _notification_is_resolved(state_dir: Path, receipt_id: str) -> bool:
    return (state_dir / "resolutions" / f"{receipt_id}.json").is_file()


def _blocked_result(
    *,
    account: str,
    dedupe_key: str,
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
        "authority_receipt_id": dedupe_key,
        "authority_receipt_status": "unknown",
    }


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
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
