from __future__ import annotations

from dataclasses import dataclass

from src.application.agent_tool_contracts import AgentToolError
from src.application.settings import build_effective_env


DEFAULT_CONFIRM_TTL_SECONDS = 600


@dataclass(frozen=True)
class InboundOperationPolicy:
    operations_enabled: bool
    trade_write_enabled: bool
    symbol_write_enabled: bool
    admin_senders: tuple[str, ...]
    confirm_ttl_seconds: int = DEFAULT_CONFIRM_TTL_SECONDS


def load_operation_policy_from_env() -> InboundOperationPolicy:
    env = build_effective_env().values
    return InboundOperationPolicy(
        operations_enabled=_truthy(env.get("OM_INBOUND_OPERATIONS_ENABLED")),
        trade_write_enabled=_truthy(env.get("OM_INBOUND_TRADE_WRITE_ENABLED")),
        symbol_write_enabled=_truthy(env.get("OM_INBOUND_SYMBOL_WRITE_ENABLED")),
        admin_senders=_parse_sender_entries(env.get("OM_INBOUND_ADMIN_OPEN_IDS")),
        confirm_ttl_seconds=_positive_int(
            env.get("OM_INBOUND_CONFIRM_TTL_SECONDS"),
            default=DEFAULT_CONFIRM_TTL_SECONDS,
        ),
    )


def enforce_trade_write_allowed(
    *,
    channel: str,
    sender_id: str,
    policy: InboundOperationPolicy | None = None,
) -> InboundOperationPolicy:
    effective = _enforce_base_write_allowed(channel=channel, sender_id=sender_id, policy=policy)
    if not effective.trade_write_enabled:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="inbound trade recording is disabled",
            hint="Set OM_INBOUND_TRADE_WRITE_ENABLED=1 for manual trade recording.",
        )
    return effective


def enforce_symbol_write_allowed(
    *,
    channel: str,
    sender_id: str,
    policy: InboundOperationPolicy | None = None,
) -> InboundOperationPolicy:
    effective = _enforce_base_write_allowed(channel=channel, sender_id=sender_id, policy=policy)
    if not effective.symbol_write_enabled:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="inbound monitored symbol writes are disabled",
            hint="Set OM_INBOUND_SYMBOL_WRITE_ENABLED=1 for monitored symbol config writes.",
        )
    return effective


def _enforce_base_write_allowed(
    *,
    channel: str,
    sender_id: str,
    policy: InboundOperationPolicy | None = None,
) -> InboundOperationPolicy:
    effective = policy or load_operation_policy_from_env()
    if not effective.operations_enabled:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="inbound write operations are disabled",
            hint="Set OM_INBOUND_OPERATIONS_ENABLED=1 before enabling write commands.",
        )
    if not effective.admin_senders:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="no inbound operation admin sender is configured",
            hint="Set OM_INBOUND_ADMIN_OPEN_IDS to the current bot app open_id.",
        )
    if not _sender_matches(channel=channel, sender_id=sender_id, entries=effective.admin_senders):
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="sender is not allowed to write operations",
            hint="Add this sender to OM_INBOUND_ADMIN_OPEN_IDS.",
        )
    return effective


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _positive_int(value: str | None, *, default: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _parse_sender_entries(value: str | None) -> tuple[str, ...]:
    entries: list[str] = []
    for raw in str(value or "").replace("\n", ",").replace(";", ",").split(","):
        item = raw.strip()
        if item and item not in entries:
            entries.append(item)
    return tuple(entries)


def _sender_matches(*, channel: str, sender_id: str, entries: tuple[str, ...]) -> bool:
    normalized_channel = str(channel or "").strip().lower() or "local"
    normalized_sender = str(sender_id or "").strip()
    for entry in entries:
        if ":" in entry:
            entry_channel, entry_sender = entry.split(":", 1)
        else:
            entry_channel, entry_sender = "*", entry
        channel_ok = entry_channel.strip().lower() in {"*", normalized_channel}
        sender_ok = entry_sender.strip() in {"*", normalized_sender}
        if channel_ok and sender_ok:
            return True
    return False
