from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


SCHEMA_VERSION_V1 = "1.0"
SCHEMA_KIND_SNAPSHOT_DTO = "snapshot_dto"
SCHEMA_KIND_DECISION = "decision"
SCHEMA_KIND_DELIVERY_PLAN = "delivery_plan"


class SchemaValidationError(ValueError):
    """Blocking schema validation error for critical multi_tick path."""


def _require_schema(payload: Mapping[str, Any] | Any, *, kind: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("payload must be a mapping")
    if str(payload.get("schema_kind") or "") != str(kind):
        raise SchemaValidationError(f"schema_kind must be {kind}")
    if str(payload.get("schema_version") or "") != SCHEMA_VERSION_V1:
        raise SchemaValidationError(f"unsupported schema_version: {payload.get('schema_version')}")
    return payload


@dataclass(frozen=True)
class SnapshotDTO:
    snapshot_name: str
    payload: dict[str, Any]
    as_of_utc: str
    schema_kind: str = SCHEMA_KIND_SNAPSHOT_DTO
    schema_version: str = SCHEMA_VERSION_V1

    def to_payload(self) -> dict[str, Any]:
        out = {
            "schema_kind": self.schema_kind,
            "schema_version": self.schema_version,
            "snapshot_name": str(self.snapshot_name or "").strip(),
            "payload": dict(self.payload or {}),
            "as_of_utc": str(self.as_of_utc or ""),
        }
        return SnapshotDTO.from_payload(out).to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_kind": self.schema_kind,
            "schema_version": self.schema_version,
            "snapshot_name": self.snapshot_name,
            "payload": self.payload,
            "as_of_utc": self.as_of_utc,
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any] | Any) -> "SnapshotDTO":
        src = _require_schema(raw, kind=SCHEMA_KIND_SNAPSHOT_DTO)
        name = str(src.get("snapshot_name") or "").strip()
        if not name:
            raise SchemaValidationError("snapshot_name is required")
        payload = src.get("payload")
        if not isinstance(payload, dict):
            raise SchemaValidationError("payload must be a dict")
        as_of_utc = str(src.get("as_of_utc") or "").strip()
        if not as_of_utc:
            raise SchemaValidationError("as_of_utc is required")
        return cls(snapshot_name=name, payload=dict(payload), as_of_utc=as_of_utc)


@dataclass(frozen=True)
class Decision:
    account: str
    should_run: bool
    should_notify: bool
    reason: str
    schema_kind: str = SCHEMA_KIND_DECISION
    schema_version: str = SCHEMA_VERSION_V1

    def to_payload(self) -> dict[str, Any]:
        out = {
            "schema_kind": self.schema_kind,
            "schema_version": self.schema_version,
            "account": str(self.account or "").strip(),
            "should_run": bool(self.should_run),
            "should_notify": bool(self.should_notify),
            "reason": str(self.reason or ""),
        }
        return Decision.from_payload(out).to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_kind": self.schema_kind,
            "schema_version": self.schema_version,
            "account": self.account,
            "should_run": self.should_run,
            "should_notify": self.should_notify,
            "reason": self.reason,
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any] | Any) -> "Decision":
        src = _require_schema(raw, kind=SCHEMA_KIND_DECISION)
        account = str(src.get("account") or "").strip()
        if not account:
            raise SchemaValidationError("account is required")
        return cls(
            account=account,
            should_run=bool(src.get("should_run")),
            should_notify=bool(src.get("should_notify")),
            reason=str(src.get("reason") or ""),
        )


@dataclass(frozen=True)
class DeliveryPlan:
    channel: str
    target: str
    account_messages: dict[str, str]
    should_send: bool
    schema_kind: str = SCHEMA_KIND_DELIVERY_PLAN
    schema_version: str = SCHEMA_VERSION_V1

    def to_payload(self) -> dict[str, Any]:
        out = {
            "schema_kind": self.schema_kind,
            "schema_version": self.schema_version,
            "channel": str(self.channel or "").strip(),
            "target": str(self.target or "").strip(),
            "account_messages": {str(k): str(v) for k, v in (self.account_messages or {}).items()},
            "should_send": bool(self.should_send),
        }
        return DeliveryPlan.from_payload(out).to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_kind": self.schema_kind,
            "schema_version": self.schema_version,
            "channel": self.channel,
            "target": self.target,
            "account_messages": self.account_messages,
            "should_send": self.should_send,
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any] | Any) -> "DeliveryPlan":
        src = _require_schema(raw, kind=SCHEMA_KIND_DELIVERY_PLAN)
        channel = str(src.get("channel") or "").strip()
        target = str(src.get("target") or "").strip()
        if not channel:
            raise SchemaValidationError("channel is required")
        if not target:
            raise SchemaValidationError("target is required")
        raw_messages = src.get("account_messages")
        if not isinstance(raw_messages, Mapping):
            raise SchemaValidationError("account_messages must be a mapping")
        account_messages: dict[str, str] = {}
        for key, value in raw_messages.items():
            acct = str(key or "").strip()
            if not acct:
                raise SchemaValidationError("account_messages key must be non-empty")
            account_messages[acct] = str(value or "")
        return cls(
            channel=channel,
            target=target,
            account_messages=account_messages,
            should_send=bool(src.get("should_send")),
        )
