from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from domain.domain.symbol_identity import canonical_symbol
from src.application.events.source_yfinance import EventSourceError, classify_event_source_error
from src.application.runtime_config_paths import write_json_atomic


@dataclass(frozen=True)
class EventFetchResult:
    provider: str
    symbol: str
    events: list[dict[str, Any]]
    source_status: str
    source_error: str = ""
    error_code: str = ""
    fetched_at: str = ""
    last_success_at: str = ""
    last_error_at: str = ""
    blocked_until: str = ""
    cache_status: str = ""

    def to_snapshot_item(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "symbol": self.symbol,
            "events": list(self.events),
            "source_status": self.source_status,
            "source_error": self.source_error,
            "error_code": self.error_code,
            "fetched_at": self.fetched_at,
            "last_success_at": self.last_success_at,
            "last_error_at": self.last_error_at,
            "blocked_until": self.blocked_until,
            "cache_status": self.cache_status,
        }


class EventStore:
    def __init__(
        self,
        path: Path,
        *,
        provider: str = "yfinance",
        success_ttl_seconds: int = 86400,
        stale_ttl_seconds: int = 30 * 86400,
        error_cooldown_seconds: int = 30 * 60,
        rate_limit_cooldown_seconds: int = 60 * 60,
    ) -> None:
        self.path = Path(path)
        self.provider = str(provider or "yfinance").strip().lower() or "yfinance"
        self.success_ttl_seconds = max(1, int(success_ttl_seconds))
        self.stale_ttl_seconds = max(1, int(stale_ttl_seconds))
        self.error_cooldown_seconds = max(1, int(error_cooldown_seconds))
        self.rate_limit_cooldown_seconds = max(1, int(rate_limit_cooldown_seconds))

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "providers": {}, "symbols": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"schema_version": 1, "providers": {}, "symbols": {}}
        if not isinstance(data, dict):
            return {"schema_version": 1, "providers": {}, "symbols": {}}
        data.setdefault("schema_version", 1)
        data.setdefault("providers", {})
        data.setdefault("symbols", {})
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.path, data)

    def resolve(
        self,
        symbol: str,
        *,
        fetcher: Callable[[str], list[dict[str, Any]]],
        now: datetime | None = None,
        force_refresh: bool = False,
    ) -> EventFetchResult:
        now_dt = now or datetime.now(timezone.utc)
        sym = _canonical(symbol)
        data = self.load()
        providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
        symbols = data.get("symbols") if isinstance(data.get("symbols"), dict) else {}
        data["providers"] = providers
        data["symbols"] = symbols

        provider_state = providers.get(self.provider) if isinstance(providers.get(self.provider), dict) else {}
        entry_key = self._entry_key(sym)
        entry = symbols.get(entry_key) if isinstance(symbols.get(entry_key), dict) else None

        if entry and not force_refresh and self._success_fresh(entry, now_dt):
            return self._result_from_entry(sym, entry, source_status="ok", cache_status="hit_ok")

        provider_blocked_until = _parse_dt(provider_state.get("blocked_until"))
        entry_blocked_until = _parse_dt(entry.get("blocked_until")) if entry else None
        active_block = _max_dt(provider_blocked_until, entry_blocked_until)
        if active_block and now_dt < active_block and not force_refresh:
            return self._blocked_result(sym, entry, blocked_until=active_block, provider_state=provider_state, now=now_dt)

        try:
            events = _clean_events(fetcher(sym))
        except Exception as exc:
            error_code = _error_code(exc)
            source_error = _error_text(exc)
            blocked_until = now_dt + timedelta(
                seconds=self.rate_limit_cooldown_seconds if error_code == "rate_limited" else self.error_cooldown_seconds
            )
            if error_code == "rate_limited":
                providers[self.provider] = {
                    **provider_state,
                    "blocked_until": blocked_until.isoformat(),
                    "last_error_at": now_dt.isoformat(),
                    "last_error": source_error,
                    "error_code": error_code,
                }
            updated = dict(entry or {})
            if not _has_stale_success(updated, now_dt, self.stale_ttl_seconds):
                updated.pop("events", None)
                updated.pop("fetched_at", None)
                updated.pop("last_success_at", None)
            updated.update(
                {
                    "provider": self.provider,
                    "symbol": sym,
                    "source_status": "error",
                    "last_error_at": now_dt.isoformat(),
                    "last_error": source_error,
                    "last_error_type": type(exc).__name__,
                    "error_code": error_code,
                    "blocked_until": blocked_until.isoformat(),
                }
            )
            symbols[entry_key] = updated
            self.save(data)
            if _has_stale_success(updated, now_dt, self.stale_ttl_seconds):
                return self._result_from_entry(
                    sym,
                    updated,
                    source_status="stale",
                    source_error=source_error,
                    error_code=error_code,
                    blocked_until=blocked_until.isoformat(),
                    cache_status="stale_after_error",
                )
            return EventFetchResult(
                provider=self.provider,
                symbol=sym,
                events=[],
                source_status="error",
                source_error=source_error,
                error_code=error_code,
                last_error_at=now_dt.isoformat(),
                blocked_until=blocked_until.isoformat(),
                cache_status="fetch_error",
            )

        fetched_at = now_dt.isoformat()
        symbols[entry_key] = {
            "provider": self.provider,
            "symbol": sym,
            "source_status": "ok",
            "events": events,
            "fetched_at": fetched_at,
            "last_success_at": fetched_at,
        }
        providers.setdefault(self.provider, {})
        self.save(data)
        return EventFetchResult(
            provider=self.provider,
            symbol=sym,
            events=events,
            source_status="ok",
            fetched_at=fetched_at,
            last_success_at=fetched_at,
            cache_status="fetched",
        )

    def _entry_key(self, symbol: str) -> str:
        return f"{self.provider}:{symbol}"

    def _success_fresh(self, entry: dict[str, Any], now_dt: datetime) -> bool:
        if entry.get("source_status") != "ok":
            return False
        fetched_at = _parse_dt(entry.get("fetched_at") or entry.get("last_success_at"))
        if fetched_at is None:
            return False
        return (now_dt - fetched_at).total_seconds() <= self.success_ttl_seconds

    def _result_from_entry(
        self,
        symbol: str,
        entry: dict[str, Any],
        *,
        source_status: str,
        source_error: str = "",
        error_code: str = "",
        blocked_until: str = "",
        cache_status: str,
    ) -> EventFetchResult:
        return EventFetchResult(
            provider=str(entry.get("provider") or self.provider),
            symbol=symbol,
            events=_clean_events(entry.get("events")),
            source_status=source_status,
            source_error=source_error,
            error_code=error_code,
            fetched_at=str(entry.get("fetched_at") or ""),
            last_success_at=str(entry.get("last_success_at") or entry.get("fetched_at") or ""),
            last_error_at=str(entry.get("last_error_at") or ""),
            blocked_until=blocked_until or str(entry.get("blocked_until") or ""),
            cache_status=cache_status,
        )

    def _blocked_result(
        self,
        symbol: str,
        entry: dict[str, Any] | None,
        *,
        blocked_until: datetime,
        provider_state: dict[str, Any],
        now: datetime,
    ) -> EventFetchResult:
        source_error = str(provider_state.get("last_error") or (entry or {}).get("last_error") or "event source cooldown active")
        error_code = str(provider_state.get("error_code") or (entry or {}).get("error_code") or "source_error")
        if entry and _has_stale_success(entry, now, self.stale_ttl_seconds):
            return self._result_from_entry(
                symbol,
                entry,
                source_status="stale",
                source_error=source_error,
                error_code=error_code,
                blocked_until=blocked_until.isoformat(),
                cache_status="stale_provider_cooldown",
            )
        return EventFetchResult(
            provider=self.provider,
            symbol=symbol,
            events=[],
            source_status="error",
            source_error=source_error,
            error_code=error_code,
            blocked_until=blocked_until.isoformat(),
            cache_status="provider_cooldown",
        )


def _canonical(symbol: str) -> str:
    return canonical_symbol(symbol) or str(symbol or "").strip().upper()


def _parse_dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _max_dt(*values: datetime | None) -> datetime | None:
    dts = [v for v in values if v is not None]
    return max(dts) if dts else None


def _clean_events(events: Any) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        return []
    return [dict(x) for x in events if isinstance(x, dict)]


def _has_stale_success(entry: dict[str, Any], now_dt: datetime, stale_ttl_seconds: int) -> bool:
    if not _clean_events(entry.get("events")) and "events" not in entry:
        return False
    success_at = _parse_dt(entry.get("last_success_at") or entry.get("fetched_at"))
    if success_at is None:
        return False
    return (now_dt - success_at).total_seconds() <= stale_ttl_seconds


def _error_code(exc: Exception) -> str:
    if isinstance(exc, EventSourceError) and exc.error_code:
        return exc.error_code
    return classify_event_source_error(f"{type(exc).__name__}: {exc}")


def _error_text(exc: Exception) -> str:
    msg = str(exc).strip()
    if len(msg) > 500:
        msg = msg[:500] + "..."
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
