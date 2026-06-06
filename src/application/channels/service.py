from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from src.application.channels.contracts import ChannelCapabilities


SendFn = Callable[..., Any]
NormalizeFn = Callable[..., dict[str, Any]]
InboundFn = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class ChannelAdapter:
    channel: str
    capabilities: ChannelCapabilities = field(default_factory=ChannelCapabilities)
    send_fn: SendFn | None = None
    normalize_fn: NormalizeFn | None = None
    inbound_fn: InboundFn | None = None
    failure_stage: str = "send_message"


class ChannelRegistry:
    def __init__(self, adapters: Iterable[ChannelAdapter] = ()) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ChannelAdapter) -> None:
        channel = _normalize_channel(adapter.channel)
        if not channel:
            raise ValueError("channel is required")
        self._adapters[channel] = adapter

    def require(self, channel: Any) -> ChannelAdapter:
        normalized = _normalize_channel(channel)
        adapter = self._adapters.get(normalized)
        if adapter is None:
            allowed = ", ".join(self.list_channels())
            raise ValueError(f"unsupported message channel: {channel}; expected one of: {allowed}")
        return adapter

    def list_channels(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


class ChannelService:
    def __init__(self, registry: ChannelRegistry) -> None:
        self._registry = registry

    def handle_inbound(self, channel: Any, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        adapter = self._registry.require(channel)
        if not adapter.capabilities.inbound or adapter.inbound_fn is None:
            raise ValueError(f"message channel does not support inbound handling: {channel}")
        return adapter.inbound_fn(payload, **kwargs)


def _normalize_channel(value: Any) -> str:
    return str(value or "").strip().lower()
