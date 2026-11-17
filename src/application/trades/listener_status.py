"""Known nonworking states in the trade listener status protocol."""

from datetime import datetime, timezone
from typing import Any, Mapping

from src.application.payload_helpers import parse_utc

# Working stage labels include listening / starting / once. Recovery work can
# refresh the heartbeat while reconnecting, so freshness alone is insufficient.
# Consumers retain their own time windows, severity and legacy-label policies.
NONWORKING_LISTENER_STATUSES = frozenset({"blocked", "error", "reconnecting", "stopped"})
DEFAULT_LISTENER_HEARTBEAT_SECONDS = 300

OBSERVATION_STATUSES = frozenset({"ok", "partial", "unavailable", "not_required", "cancelled", "unknown"})
OBSERVATION_REASONS = frozenset({
    "provider_timeout", "provider_worker_exited", "provider_unavailable", "cancelled",
    "position_snapshot_unavailable", "cash_snapshot_unavailable", "ordinary_evidence_unavailable",
    "recovery_unavailable", "observation_unknown", "observation_stale",
})


def observation_status(status: str, *reasons: str, now: datetime | None = None,
                       ordinary_requested: bool = False) -> dict[str, Any]:
    return {"status": status, "observed_at_utc": (now or datetime.now(timezone.utc)).isoformat(),
            "reason_codes": list(reasons), "ordinary_requested": ordinary_requested}


def attribution_observations(status: Mapping[str, Any], *, accounts: list[str],
                             source: str | None = None, market: str | None = None) -> list[dict[str, Any]]:
    """Read only diagnostic fields; never expose broker errors or identifiers."""
    recovery = status.get("attribution_recovery")
    recovery = recovery if isinstance(recovery, Mapping) else {}
    requested_market = market
    rows = []
    for account in accounts:
        entry = recovery.get(account)
        entry = entry if isinstance(entry, Mapping) else {}
        markets = [market for market in ("hk", "us") if market in entry]
        if requested_market in {"hk", "us"} and requested_market not in markets:
            markets.append(requested_market)
        for market in markets or [None]:
            item = entry.get(market) if market else entry
            item = item if isinstance(item, Mapping) else {}
            raw = item.get("observation")
            raw = raw if isinstance(raw, Mapping) else {}
            value = raw.get("status")
            value = value if isinstance(value, str) and value in OBSERVATION_STATUSES else "unknown"
            codes = raw.get("reason_codes")
            if not isinstance(codes, list) or any(not isinstance(code, str) or code not in OBSERVATION_REASONS for code in codes):
                value = "unknown"
            codes = [code for code in codes if isinstance(code, str) and code in OBSERVATION_REASONS] if isinstance(codes, list) else []
            if value in {"ok", "not_required"} and codes:
                value = "unknown"
            if recovery.get("error") or entry.get("error") or item.get("error"):
                value, codes = "unavailable", ["recovery_unavailable"]
            if value == "unknown":
                codes = ["observation_unknown"]
            instant = parse_utc(raw.get("observed_at_utc")) if isinstance(raw.get("observed_at_utc"), str) else None
            if instant is None and value not in {"partial", "unavailable"}:
                value, codes = "unknown", ["observation_unknown"]
            rows.append({"account": account, "market": market, "source": source, "status": value,
                         "observed_at_utc": instant.isoformat() if instant else None, "reason_codes": codes,
                         "ordinary_requested": raw.get("ordinary_requested") is True})
    return rows


def attribution_observation_health(rows: list[dict[str, Any]], *, now: datetime,
                                   max_age_seconds: float) -> str:
    """Keep failed evidence visible even when its timestamp is missing or old."""
    if any(row.get("status") in {"partial", "unavailable"} for row in rows):
        return "warn"
    if not rows:
        return "unknown"
    for row in rows:
        instant = parse_utc(row.get("observed_at_utc"))
        if (row.get("status") not in {"ok", "not_required"} or instant is None
                or not 0 <= (now - instant).total_seconds() <= max_age_seconds):
            return "unknown"
    return "pass"
