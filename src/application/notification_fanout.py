"""Durable, account-bound delivery obligations shared by notification producers.

Business owners produce a logical message once. Only this owner claims and
retries its frozen recipients; provider I/O always happens outside the lock.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, Sequence, TypedDict
import uuid

from domain.storage.json_io import atomic_write_private_json
from src.application.file_locks import exclusive_lock


CLAIM_LEASE_MS = 5 * 60 * 1000
SCHEMA_VERSION = "notification_fanout.v1"


class FrozenRecipient(TypedDict):
    route_id: str
    provider: str
    channel: str
    target: str
    identity_key: str
    transport_options: dict[str, Any]


class FrozenDeliverySpec(TypedDict):
    recipient: FrozenRecipient
    message: str
    transport_envelope: dict[str, Any] | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class FanoutRetryPolicy:
    max_attempts: int
    retry_delays_ms: tuple[int, ...]


FanoutSnapshot = dict[str, Any]
FanoutChildReceipt = dict[str, Any]


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _now(now_ms: int | None) -> int:
    return int(time.time() * 1000) if now_ms is None else int(now_ms)


def notification_fanout_id(*, account: str, logical_key: str) -> str:
    account = str(account).strip().lower()
    if not account or not str(logical_key).strip():
        raise ValueError("fanout account and logical_key are required")
    return "nf_" + _hash(account)[:16] + "_" + _hash([account, logical_key])[:32]


def _path(base: Path, fanout_id: str) -> Path:
    if not re.fullmatch(r"nf_[0-9a-f]{16}_[0-9a-f]{32}", fanout_id):
        raise ValueError("invalid notification fanout id")
    return Path(base) / "output_shared/state/notification_fanout" / fanout_id[3:19] / (fanout_id + ".json")


def _read(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION
            or not isinstance(state.get("plan"), dict) or not isinstance(state.get("children"), list)
            or state.get("plan_sha256") != _hash(state["plan"])
            or state.get("fanout_id") != path.stem):
        raise ValueError("notification fanout state is invalid")
    plan = state["plan"]
    if notification_fanout_id(account=plan["account"], logical_key=plan["logical_key"]) != state["fanout_id"]:
        raise ValueError("notification fanout identity mismatch")
    if len(state["children"]) != len(plan["deliveries"]):
        raise ValueError("notification fanout children mismatch")
    for child, spec in zip(state["children"], plan["deliveries"], strict=True):
        if child.get("spec") != spec or child.get("child_id") != _child_id(state["fanout_id"], spec):
            raise ValueError("notification fanout frozen spec mismatch")
        if child.get("status") not in {"pending", "send_started", "confirmed", "explicit_failed", "accepted", "unknown"}:
            raise ValueError("notification fanout child status is invalid")
    return state


def _child_id(fanout_id: str, spec: Mapping[str, Any]) -> str:
    recipient = spec["recipient"]
    return "nfc_" + _hash([fanout_id, recipient["route_id"], recipient["identity_key"]])[:32]


def _retryable(child: dict[str, Any], state: dict[str, Any]) -> bool:
    return child["status"] in {"pending", "explicit_failed"} and (
        child["attempt_count"] < state["plan"]["retry_policy"]["max_attempts"])


def _snapshot(state: dict[str, Any]) -> FanoutSnapshot:
    children = deepcopy(state["children"])
    statuses = {child["status"] for child in children}
    if statuses == {"confirmed"}:
        status = "confirmed"
    elif statuses == {"pending"}:
        status = "pending"
    elif statuses == {"explicit_failed"}:
        status = "explicit_failed"
    elif statuses <= {"unknown", "accepted"}:
        status = "unknown"
    elif statuses <= {"pending", "send_started"}:
        status = "in_progress"
    else:
        status = "partial"
    retryable = [child for child in children if _retryable(child, state)]
    return {"fanout_id": state["fanout_id"], "account": state["plan"]["account"],
        "logical_key": state["plan"]["logical_key"], "status": status,
        "delivery_confirmed": status == "confirmed",
        "explicit_pre_acceptance_failure": status == "explicit_failed",
        "has_retryable_children": bool(retryable),
        "has_unknown_children": bool(statuses & {"unknown", "accepted"}),
        "next_attempt_at_ms": min((child["next_attempt_at_ms"] for child in retryable), default=None),
        "children": children, "created_at_ms": state["created_at_ms"]}


def prepare_notification_fanout(
    *, base: Path, account: str, logical_key: str,
    deliveries: Sequence[FrozenDeliverySpec], retry_policy: FanoutRetryPolicy,
    now_ms: int | None = None,
) -> FanoutSnapshot:
    account = str(account).strip().lower()
    fanout_id = notification_fanout_id(account=account, logical_key=logical_key)
    specs = deepcopy(list(deliveries))
    if not specs or retry_policy.max_attempts < 1 or any(delay < 0 for delay in retry_policy.retry_delays_ms):
        raise ValueError("fanout requires recipients and a valid retry policy")
    route_ids: set[str] = set()
    identities: set[str] = set()
    for spec in specs:
        recipient = spec["recipient"]
        if any(not isinstance(recipient.get(key), str) or not recipient[key].strip()
               for key in ("route_id", "provider", "channel", "target", "identity_key")):
            raise ValueError("fanout recipient binding is incomplete")
        if recipient["route_id"] in route_ids or recipient["identity_key"] in identities:
            raise ValueError("duplicate fanout route or actual recipient identity")
        route_ids.add(recipient["route_id"])
        identities.add(recipient["identity_key"])
        if not isinstance(spec.get("message"), str) or not spec["message"].strip():
            raise ValueError("fanout frozen message is required")
        recipient.setdefault("transport_options", {})
        spec.setdefault("transport_envelope", None)
        spec.setdefault("metadata", {})
    # Configuration order is not part of a recipient's delivery identity.
    specs.sort(key=lambda item: item["recipient"]["route_id"])
    plan = {"account": account, "logical_key": logical_key, "deliveries": specs,
        "retry_policy": asdict(retry_policy)}
    plan = json.loads(json.dumps(plan, ensure_ascii=False, allow_nan=False))
    path = _path(base, fanout_id)
    with exclusive_lock(path.with_suffix(".lock")):
        previous = _read(path)
        if previous is not None:
            if previous["plan_sha256"] != _hash(plan):
                raise ValueError("notification fanout frozen plan changed")
            return _snapshot(previous)
        state = {"schema_version": SCHEMA_VERSION, "fanout_id": fanout_id,
            "plan": plan, "plan_sha256": _hash(plan), "created_at_ms": _now(now_ms), "children": []}
        for spec in plan["deliveries"]:
            child_id = _child_id(fanout_id, spec)
            state["children"].append({"child_id": child_id, "spec": spec,
                "route_id": spec["recipient"]["route_id"], "identity_key": spec["recipient"]["identity_key"],
                "status": "pending", "delivery_confirmed": False, "attempt_count": 0,
                "next_attempt_at_ms": 0, "confirmed_at_ms": None,
                "idempotency_key": "om-" + _hash([fanout_id, child_id])[:32],
                "attempt_id": None, "claim_until_ms": None, "provider_receipt": None,
                "attempts": []})
        atomic_write_private_json(path, state)
        return _snapshot(state)


def read_notification_fanout(*, base: Path, fanout_id: str) -> FanoutSnapshot | None:
    state = _read(_path(base, fanout_id))
    return _snapshot(state) if state is not None else None


def read_notification_fanout_recipient(
    *, base: Path, fanout_id: str, route_id: str, identity_key: str,
) -> FanoutChildReceipt | None:
    snapshot = read_notification_fanout(base=base, fanout_id=fanout_id)
    return next((child for child in (snapshot or {}).get("children", [])
        if child["route_id"] == route_id and child["identity_key"] == identity_key), None)


def list_notification_fanouts(
    *, base: Path, account: str, logical_key_prefix: str = "",
) -> list[FanoutSnapshot]:
    directory = Path(base) / "output_shared/state/notification_fanout" / _hash(str(account).strip().lower())[:16]
    snapshots = []
    for path in sorted(directory.glob("nf_*.json")):
        state = _read(path)
        if state is None:
            continue
        if state["plan"]["account"] != str(account).strip().lower():
            raise ValueError("notification fanout account mismatch")
        if state["plan"]["logical_key"].startswith(logical_key_prefix):
            snapshots.append(_snapshot(state))
    return sorted(snapshots, key=lambda item: (item["created_at_ms"], item["fanout_id"]))


def _outcome(result: Mapping[str, Any], spec: FrozenDeliverySpec) -> str:
    actual = result.get("attribution_route_key")
    if actual is not None and actual != spec["recipient"]["identity_key"]:
        return "unknown"
    if result.get("ambiguous_send") or result.get("duplicate_risk"):
        return "unknown"
    outcome = result.get("outcome")
    if result.get("delivery_confirmed") is True or outcome == "confirmed":
        return "confirmed" if actual == spec["recipient"]["identity_key"] else "unknown"
    if outcome in {"confirmed", "explicit_failed", "accepted", "unknown"}:
        return str(outcome)
    if result.get("explicit_pre_acceptance_failure") is True:
        return "explicit_failed"
    http_status = result.get("http_status")
    provider_code = result.get("provider_response_code", result.get("feishu_code"))
    if (isinstance(http_status, int) and (400 <= http_status < 500
            or (200 <= http_status < 300 and provider_code not in (None, 0, "0")))):
        return "explicit_failed"
    if result.get("local_error_code") and not result.get("http_attempts") and http_status is None:
        return "explicit_failed"
    if result.get("command_ok"):
        return "accepted"
    return "unknown"


def dispatch_notification_fanout(
    *, base: Path, fanout_id: str, send_fn: Callable[..., Mapping[str, Any]],
    now_ms: int | None = None,
    can_send_fn: Callable[[FrozenDeliverySpec], bool] | None = None,
) -> FanoutSnapshot:
    path = _path(base, fanout_id)
    snapshot = read_notification_fanout(base=base, fanout_id=fanout_id)
    if snapshot is None:
        raise ValueError("notification fanout not found")
    blocked = []
    for observed in snapshot["children"]:
        if observed["status"] not in {"pending", "explicit_failed", "send_started"}:
            continue
        allowed = True
        if can_send_fn is not None:
            try:
                allowed = bool(can_send_fn(deepcopy(observed["spec"])))
            except Exception as exc:
                allowed = False
                blocked.append({"child_id": observed["child_id"], "reason": type(exc).__name__})
            else:
                if not allowed:
                    blocked.append({"child_id": observed["child_id"], "reason": "route_disabled_or_changed"})
        with exclusive_lock(path.with_suffix(".lock")):
            state = _read(path)
            if state is None:
                raise ValueError("notification fanout disappeared")
            child = next(item for item in state["children"] if item["child_id"] == observed["child_id"])
            current = _now(now_ms)
            if child["status"] == "send_started" and child["claim_until_ms"] <= current:
                child.update(status="unknown", claim_until_ms=None, next_attempt_at_ms=None)
                atomic_write_private_json(path, state)
            if not allowed or not _retryable(child, state) or child["next_attempt_at_ms"] > current:
                continue
            attempt_id = uuid.uuid4().hex
            child.update(status="send_started", attempt_id=attempt_id,
                claim_until_ms=current + CLAIM_LEASE_MS, next_attempt_at_ms=None,
                attempt_count=child["attempt_count"] + 1)
            child["attempts"].append({"attempt_id": attempt_id, "started_at_ms": current, "outcome": "unknown"})
            spec, transport_key = deepcopy(child["spec"]), child["idempotency_key"]
            atomic_write_private_json(path, state)
        try:
            result = dict(send_fn(spec=spec, idempotency_key=transport_key))
            outcome = _outcome(result, spec)
        except Exception as exc:
            result = {"error_type": type(exc).__name__, "error": str(exc)}
            outcome = "unknown"
        with exclusive_lock(path.with_suffix(".lock")):
            state = _read(path)
            if state is None:
                raise ValueError("notification fanout disappeared after provider attempt")
            child = next(item for item in state["children"] if item["child_id"] == observed["child_id"])
            if child["attempt_id"] != attempt_id:
                raise ValueError("notification fanout attempt changed")
            current = _now(now_ms)
            child.update(status=outcome, provider_receipt=result, claim_until_ms=None,
                delivery_confirmed=outcome == "confirmed",
                confirmed_at_ms=current if outcome == "confirmed" else None)
            child["attempts"][-1].update(outcome=outcome, finished_at_ms=current, provider_receipt=result)
            if _retryable(child, state):
                delays = state["plan"]["retry_policy"]["retry_delays_ms"]
                delay = delays[min(child["attempt_count"] - 1, len(delays) - 1)] if delays else 0
                child["next_attempt_at_ms"] = current + delay
            atomic_write_private_json(path, state)
    result = read_notification_fanout(base=base, fanout_id=fanout_id)
    if result is None:
        raise ValueError("notification fanout disappeared")
    result["blocked_children"] = blocked
    return result
