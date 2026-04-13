from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


SCHEMA_VERSION_V1 = "1.0"
SCHEMA_KIND_SNAPSHOT_DTO = "snapshot_dto"
SCHEMA_KIND_DECISION = "decision"
SCHEMA_KIND_DELIVERY_PLAN = "delivery_plan"

ERR_SCHEMA_PAYLOAD_NOT_MAPPING = "E_SCHEMA_PAYLOAD_NOT_MAPPING"
ERR_SCHEMA_KIND_MISMATCH = "E_SCHEMA_KIND_MISMATCH"
ERR_SCHEMA_VERSION_UNSUPPORTED = "E_SCHEMA_VERSION_UNSUPPORTED"
ERR_SNAPSHOT_NAME_REQUIRED = "E_SNAPSHOT_NAME_REQUIRED"
ERR_SNAPSHOT_PAYLOAD_INVALID = "E_SNAPSHOT_PAYLOAD_INVALID"
ERR_SNAPSHOT_AS_OF_UTC_REQUIRED = "E_SNAPSHOT_AS_OF_UTC_REQUIRED"
ERR_DECISION_ACCOUNT_REQUIRED = "E_DECISION_ACCOUNT_REQUIRED"
ERR_DECISION_SHOULD_RUN_INVALID = "E_DECISION_SHOULD_RUN_INVALID"
ERR_DECISION_SHOULD_NOTIFY_INVALID = "E_DECISION_SHOULD_NOTIFY_INVALID"
ERR_DELIVERY_CHANNEL_REQUIRED = "E_DELIVERY_CHANNEL_REQUIRED"
ERR_DELIVERY_TARGET_REQUIRED = "E_DELIVERY_TARGET_REQUIRED"
ERR_DELIVERY_ACCOUNT_MESSAGES_INVALID = "E_DELIVERY_ACCOUNT_MESSAGES_INVALID"
ERR_DELIVERY_ACCOUNT_KEY_INVALID = "E_DELIVERY_ACCOUNT_KEY_INVALID"
ERR_DELIVERY_ACCOUNT_MESSAGE_INVALID = "E_DELIVERY_ACCOUNT_MESSAGE_INVALID"
ERR_DELIVERY_SHOULD_SEND_INVALID = "E_DELIVERY_SHOULD_SEND_INVALID"


class SchemaValidationError(ValueError):
    """Blocking schema validation error for critical multi_tick path."""


def _schema_error(code: str, message: str) -> SchemaValidationError:
    return SchemaValidationError(f"{str(code).strip()}: {message}")


def _require_bool(src: Mapping[str, Any], key: str, *, error_code: str) -> bool:
    val = src.get(key)
    if not isinstance(val, bool):
        raise _schema_error(error_code, f"{key} must be a bool")
    return bool(val)


def _require_schema(payload: Mapping[str, Any] | Any, *, kind: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise _schema_error(ERR_SCHEMA_PAYLOAD_NOT_MAPPING, "payload must be a mapping")
    if str(payload.get("schema_kind") or "") != str(kind):
        raise _schema_error(ERR_SCHEMA_KIND_MISMATCH, f"schema_kind must be {kind}")
    if str(payload.get("schema_version") or "") != SCHEMA_VERSION_V1:
        raise _schema_error(
            ERR_SCHEMA_VERSION_UNSUPPORTED,
            f"unsupported schema_version: {payload.get('schema_version')}",
        )
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
            raise _schema_error(ERR_SNAPSHOT_NAME_REQUIRED, "snapshot_name is required")
        payload = src.get("payload")
        if not isinstance(payload, Mapping):
            raise _schema_error(ERR_SNAPSHOT_PAYLOAD_INVALID, "payload must be a mapping")
        as_of_utc = str(src.get("as_of_utc") or "").strip()
        if not as_of_utc:
            raise _schema_error(ERR_SNAPSHOT_AS_OF_UTC_REQUIRED, "as_of_utc is required")
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
            raise _schema_error(ERR_DECISION_ACCOUNT_REQUIRED, "account is required")
        return cls(
            account=account,
            should_run=_require_bool(src, "should_run", error_code=ERR_DECISION_SHOULD_RUN_INVALID),
            should_notify=_require_bool(src, "should_notify", error_code=ERR_DECISION_SHOULD_NOTIFY_INVALID),
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
            raise _schema_error(ERR_DELIVERY_CHANNEL_REQUIRED, "channel is required")
        if not target:
            raise _schema_error(ERR_DELIVERY_TARGET_REQUIRED, "target is required")
        raw_messages = src.get("account_messages")
        if not isinstance(raw_messages, Mapping):
            raise _schema_error(ERR_DELIVERY_ACCOUNT_MESSAGES_INVALID, "account_messages must be a mapping")
        account_messages: dict[str, str] = {}
        for key, value in raw_messages.items():
            acct = str(key or "").strip()
            if not acct:
                raise _schema_error(ERR_DELIVERY_ACCOUNT_KEY_INVALID, "account_messages key must be non-empty")
            if not isinstance(value, str):
                raise _schema_error(
                    ERR_DELIVERY_ACCOUNT_MESSAGE_INVALID,
                    "account_messages value must be string",
                )
            account_messages[acct] = value
        return cls(
            channel=channel,
            target=target,
            account_messages=account_messages,
            should_send=_require_bool(src, "should_send", error_code=ERR_DELIVERY_SHOULD_SEND_INVALID),
        )
