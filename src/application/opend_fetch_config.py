from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.application.option_chain_fetching import (
    DEFAULT_OPTION_CHAIN_MAX_CALLS,
    DEFAULT_OPTION_CHAIN_MAX_WAIT_SEC,
    DEFAULT_OPTION_CHAIN_WINDOW_SEC,
)

_SNAPSHOT_DEFAULT_MAX_CALLS = 60
_SNAPSHOT_DEFAULT_WINDOW_SEC = 30.0
_SNAPSHOT_DEFAULT_MAX_WAIT_SEC = 30.0
_EXPIRATION_DEFAULT_MAX_CALLS = 30
_EXPIRATION_DEFAULT_WINDOW_SEC = 30.0
_EXPIRATION_DEFAULT_MAX_WAIT_SEC = 30.0
OPEND_FETCH_KWARG_KEYS = frozenset(
    {
        "max_wait_sec",
        "option_chain_window_sec",
        "option_chain_max_calls",
        "snapshot_max_wait_sec",
        "snapshot_window_sec",
        "snapshot_max_calls",
        "expiration_max_wait_sec",
        "expiration_window_sec",
        "expiration_max_calls",
    }
)
OPEND_RATE_LIMIT_ENDPOINT_ALIASES = {
    "market_snapshot": ("market_snapshot", "snapshot", "get_market_snapshot"),
    "option_expiration": ("option_expiration", "expiration", "get_option_expiration_date"),
}
OPEND_RATE_LIMIT_ENDPOINT_KEYS = frozenset(
    key
    for aliases in OPEND_RATE_LIMIT_ENDPOINT_ALIASES.values()
    for key in aliases
)


@dataclass(frozen=True)
class OpenDEndpointRateLimit:
    window_sec: float
    max_calls: int
    max_wait_sec: float

    @classmethod
    def from_values(
        cls,
        *,
        window_sec: Any,
        max_calls: Any,
        max_wait_sec: Any,
        defaults: dict[str, float | int],
    ) -> "OpenDEndpointRateLimit":
        return cls(
            window_sec=_as_float(window_sec, float(defaults["window_sec"])),
            max_calls=_as_int(max_calls, int(defaults["max_calls"])),
            max_wait_sec=_as_float(max_wait_sec, float(defaults["max_wait_sec"])),
        )

    def as_config(self) -> dict[str, float | int]:
        return {
            "window_sec": self.window_sec,
            "max_calls": self.max_calls,
            "max_wait_sec": self.max_wait_sec,
        }

    def call_kwargs(self) -> dict[str, float | int]:
        return {
            "max_wait_sec": self.max_wait_sec,
            "window_sec": self.window_sec,
            "max_calls": self.max_calls,
        }


@dataclass(frozen=True)
class OpenDFetchLimits:
    option_chain: OpenDEndpointRateLimit
    market_snapshot: OpenDEndpointRateLimit
    option_expiration: OpenDEndpointRateLimit

    @classmethod
    def from_flat_kwargs(
        cls,
        *,
        max_wait_sec: Any = DEFAULT_OPTION_CHAIN_MAX_WAIT_SEC,
        option_chain_window_sec: Any = DEFAULT_OPTION_CHAIN_WINDOW_SEC,
        option_chain_max_calls: Any = DEFAULT_OPTION_CHAIN_MAX_CALLS,
        snapshot_max_wait_sec: Any = _SNAPSHOT_DEFAULT_MAX_WAIT_SEC,
        snapshot_window_sec: Any = _SNAPSHOT_DEFAULT_WINDOW_SEC,
        snapshot_max_calls: Any = _SNAPSHOT_DEFAULT_MAX_CALLS,
        expiration_max_wait_sec: Any = _EXPIRATION_DEFAULT_MAX_WAIT_SEC,
        expiration_window_sec: Any = _EXPIRATION_DEFAULT_WINDOW_SEC,
        expiration_max_calls: Any = _EXPIRATION_DEFAULT_MAX_CALLS,
    ) -> "OpenDFetchLimits":
        return cls(
            option_chain=OpenDEndpointRateLimit.from_values(
                window_sec=option_chain_window_sec,
                max_calls=option_chain_max_calls,
                max_wait_sec=max_wait_sec,
                defaults=_OPTION_CHAIN_DEFAULTS,
            ),
            market_snapshot=OpenDEndpointRateLimit.from_values(
                window_sec=snapshot_window_sec,
                max_calls=snapshot_max_calls,
                max_wait_sec=snapshot_max_wait_sec,
                defaults=_SNAPSHOT_DEFAULTS,
            ),
            option_expiration=OpenDEndpointRateLimit.from_values(
                window_sec=expiration_window_sec,
                max_calls=expiration_max_calls,
                max_wait_sec=expiration_max_wait_sec,
                defaults=_EXPIRATION_DEFAULTS,
            ),
        )

    def as_config(self) -> dict[str, dict[str, float | int]]:
        return {
            "option_chain": self.option_chain.as_config(),
            "market_snapshot": self.market_snapshot.as_config(),
            "option_expiration": self.option_expiration.as_config(),
        }

    def fetch_kwargs(self) -> dict[str, float | int]:
        return {
            "max_wait_sec": self.option_chain.max_wait_sec,
            "option_chain_window_sec": self.option_chain.window_sec,
            "option_chain_max_calls": self.option_chain.max_calls,
            "snapshot_max_wait_sec": self.market_snapshot.max_wait_sec,
            "snapshot_window_sec": self.market_snapshot.window_sec,
            "snapshot_max_calls": self.market_snapshot.max_calls,
            "expiration_max_wait_sec": self.option_expiration.max_wait_sec,
            "expiration_window_sec": self.option_expiration.window_sec,
            "expiration_max_calls": self.option_expiration.max_calls,
        }

    def discovery_kwargs(self) -> dict[str, float | int]:
        return {
            "snapshot_max_wait_sec": self.market_snapshot.max_wait_sec,
            "snapshot_window_sec": self.market_snapshot.window_sec,
            "snapshot_max_calls": self.market_snapshot.max_calls,
            "expiration_max_wait_sec": self.option_expiration.max_wait_sec,
            "expiration_window_sec": self.option_expiration.window_sec,
            "expiration_max_calls": self.option_expiration.max_calls,
        }


_OPTION_CHAIN_DEFAULTS = {
    "window_sec": DEFAULT_OPTION_CHAIN_WINDOW_SEC,
    "max_calls": DEFAULT_OPTION_CHAIN_MAX_CALLS,
    "max_wait_sec": DEFAULT_OPTION_CHAIN_MAX_WAIT_SEC,
}
_SNAPSHOT_DEFAULTS = {
    "window_sec": _SNAPSHOT_DEFAULT_WINDOW_SEC,
    "max_calls": _SNAPSHOT_DEFAULT_MAX_CALLS,
    "max_wait_sec": _SNAPSHOT_DEFAULT_MAX_WAIT_SEC,
}
_EXPIRATION_DEFAULTS = {
    "window_sec": _EXPIRATION_DEFAULT_WINDOW_SEC,
    "max_calls": _EXPIRATION_DEFAULT_MAX_CALLS,
    "max_wait_sec": _EXPIRATION_DEFAULT_MAX_WAIT_SEC,
}


def _as_float(value: Any, default: float) -> float:
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _as_int(value: Any, default: int) -> int:
    try:
        if value in (None, ""):
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def resolve_option_chain_fetch_limit(config: dict[str, Any] | None) -> OpenDEndpointRateLimit:
    runtime = config.get("runtime") if isinstance(config, dict) and isinstance(config.get("runtime"), dict) else {}
    raw = runtime.get("option_chain_fetch") if isinstance(runtime, dict) and isinstance(runtime.get("option_chain_fetch"), dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    return OpenDEndpointRateLimit.from_values(
        window_sec=raw.get("window_sec"),
        max_calls=raw.get("max_calls"),
        max_wait_sec=raw.get("max_wait_sec"),
        defaults=_OPTION_CHAIN_DEFAULTS,
    )


def resolve_option_chain_fetch_config(config: dict[str, Any] | None) -> dict[str, float | int]:
    return resolve_option_chain_fetch_limit(config).as_config()


def _endpoint_rate_limit(
    config: dict[str, Any] | None,
    *,
    endpoint: str,
    defaults: dict[str, float | int],
) -> OpenDEndpointRateLimit:
    runtime = config.get("runtime") if isinstance(config, dict) and isinstance(config.get("runtime"), dict) else {}
    opend = runtime.get("opend_rate_limits") if isinstance(runtime, dict) and isinstance(runtime.get("opend_rate_limits"), dict) else {}
    raw = {}
    if isinstance(opend, dict):
        for key in OPEND_RATE_LIMIT_ENDPOINT_ALIASES[endpoint]:
            candidate = opend.get(key)
            if isinstance(candidate, dict):
                raw = candidate
                break
    return OpenDEndpointRateLimit.from_values(
        window_sec=raw.get("window_sec"),
        max_calls=raw.get("max_calls"),
        max_wait_sec=raw.get("max_wait_sec"),
        defaults=defaults,
    )


def resolve_opend_fetch_limits(config: dict[str, Any] | None) -> OpenDFetchLimits:
    return OpenDFetchLimits(
        option_chain=resolve_option_chain_fetch_limit(config),
        market_snapshot=_endpoint_rate_limit(
            config,
            endpoint="market_snapshot",
            defaults=_SNAPSHOT_DEFAULTS,
        ),
        option_expiration=_endpoint_rate_limit(
            config,
            endpoint="option_expiration",
            defaults=_EXPIRATION_DEFAULTS,
        ),
    )


def resolve_opend_fetch_config(config: dict[str, Any] | None) -> dict[str, dict[str, float | int]]:
    return resolve_opend_fetch_limits(config).as_config()


def opend_fetch_kwargs(config: dict[str, Any] | None) -> dict[str, float | int]:
    return resolve_opend_fetch_limits(config).fetch_kwargs()


def filter_opend_fetch_kwargs(kwargs: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in dict(kwargs or {}).items() if k in OPEND_FETCH_KWARG_KEYS}


def opend_discovery_kwargs(config: dict[str, Any] | None) -> dict[str, float | int]:
    return resolve_opend_fetch_limits(config).discovery_kwargs()


def option_chain_fetch_kwargs(config: dict[str, Any] | None) -> dict[str, float | int]:
    resolved = resolve_option_chain_fetch_config(config)
    return {
        "max_wait_sec": resolved["max_wait_sec"],
        "option_chain_window_sec": resolved["window_sec"],
        "option_chain_max_calls": resolved["max_calls"],
    }
