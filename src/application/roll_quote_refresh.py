"""Point-of-use re-observation of roll quote evidence.

The scheduled evaluation is sealed to the scan-time required-data receipts.
Short-put roll advice keeps a 300s action window anchored on those receipts,
so a slow tick can spend most of the window before the candidate is sent.
This owner re-observes the same contract identities through the plan's own
market-data fetch route and returns fresh rows for the caller to overlay.

The rows keep the provider's own observation timestamps; no timestamp is
invented here. Refresh is best effort: an unavailable or empty symbol keeps
its sealed rows and reports a reason code so the caller can stay fail-closed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from src.infrastructure.futu_gateway import check_fetch_control

from domain.domain.symbol_identity import symbol_market
from src.application.market_data_provider import resolve_market_data_provider
from src.application.opend_utils import normalize_underlier
from src.application.required_data_fetching import (
    RequiredDataFetchRequest,
    execute_required_data_opend,
)


ROLL_QUOTE_REFRESH_SCHEMA = "roll_quote_refresh.v1"
# Mirrors the covered-call portfolio refresh threshold: re-observe only when
# the sealed evidence has consumed a meaningful part of the action window.
ROLL_QUOTE_REFRESH_MIN_AGE_SECONDS = 120


def _timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _received_after(received: datetime, rows: list[Mapping[str, Any]]) -> datetime:
    """Keep the receipt coherent with a provider clock ahead of the local one."""
    observed = [
        value
        for row in rows
        for field in ("snapshot_received_at_utc", "spot_observed_at_utc")
        if (value := _timestamp(row.get(field))) is not None
    ]
    return max([received, *observed])


def _quote_rights_receipt(
    provider: Any, gateway: Any, *, market: str, received: datetime,
    deadline: float | None = None, cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any] | None:
    check_fetch_control(deadline, cancelled)
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
    try:
        response = provider.get_quote_rights(gateway, market=market, **controls)
    except Exception:
        check_fetch_control(deadline, cancelled)
        return None
    check_fetch_control(deadline, cancelled)
    if not isinstance(response, Mapping) or response.get("market") != market:
        return None
    allowed = {"LV1", "LV2", "LV3", "BMP", "NO", "SF"}
    rights = {
        "quote_rights_status": (
            "ok" if response.get("status") == "ok" else "unavailable"
        ),
        "quote_rights_market": market,
    }
    for scope in ("underlier", "option"):
        value = str(response.get(f"{scope}_right") or "").upper()
        rights[f"quote_rights_{scope}"] = value if value in allowed else "UNKNOWN"
    rights["quote_rights_received_at_utc"] = received.isoformat()
    return rights


def _market_state_receipt(
    provider: Any, gateway: Any, *, symbol: str, received: datetime,
    deadline: float | None = None, cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any] | None:
    check_fetch_control(deadline, cancelled)
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
    try:
        code = normalize_underlier(symbol).code
        rows = provider.get_market_state(gateway, [code], **controls)
    except Exception:
        check_fetch_control(deadline, cancelled)
        return None
    check_fetch_control(deadline, cancelled)
    matching = [
        row
        for row in rows or []
        if isinstance(row, Mapping) and str(row.get("code") or "") == code
    ]
    if len(matching) != 1:
        return None
    state = str(matching[0].get("market_state") or "").strip().upper()
    if not state:
        return None
    return {
        "market_state_after_snapshot": state,
        "market_state_received_at_utc": received.isoformat(),
    }


def refresh_roll_quote_evidence(
    *,
    targets: list[Mapping[str, Any]],
    config: Mapping[str, Any],
    base_dir: Path,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Re-fetch the requested put contracts and their post-snapshot receipts.

    The quote rows keep the provider's own observation timestamps. Market
    state and quote rights are observed after the quote receipt so the
    evaluation's ``state_time >= snapshot`` coherence rule still holds.
    """
    provider = resolve_market_data_provider(dict(config))
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    market_state: dict[str, dict[str, Any]] = {}
    quote_rights: dict[str, dict[str, Any]] = {}
    reason_codes: list[str] = []
    observations: list[datetime] = []
    refreshed_contracts = 0
    for target in targets:
        check_fetch_control(deadline, cancelled)
        symbol = str(target.get("symbol") or "").strip()
        expirations = [str(value) for value in target.get("expirations") or []]
        if not symbol or not expirations:
            reason_codes.append("quote_refresh_target_invalid")
            continue
        try:
            payload = execute_required_data_opend(
                base=Path(base_dir),
                request=RequiredDataFetchRequest(
                    symbol=symbol,
                    limit_expirations=max(1, len(expirations)),
                    host=str(target.get("host") or "127.0.0.1"),
                    port=int(target.get("port") or 11111),
                    option_types="put",
                    min_strike=target.get("min_strike"),
                    max_strike=target.get("max_strike"),
                    explicit_expirations=expirations,
                    trading_date=target.get("trading_date"),
                    chain_cache=True,
                    market_data_provider=provider.name,
                    deadline=deadline,
                    cancelled=cancelled,
                ),
            )
        except Exception:
            check_fetch_control(deadline, cancelled)
            reason_codes.append("quote_refresh_failed")
            continue
        rows = [
            dict(row)
            for row in payload.get("rows") or []
            if isinstance(row, Mapping)
            and str(row.get("expiration") or "") in expirations
            and str(row.get("option_type") or "").strip().lower() == "put"
        ]
        if not rows:
            reason_codes.append("quote_refresh_empty")
            continue
        rows_by_symbol[symbol] = rows
        refreshed_contracts += len(rows)
        for row in rows:
            for field in ("snapshot_received_at_utc", "spot_observed_at_utc"):
                value = _timestamp(row.get(field))
                if value is not None:
                    observations.append(value)
        market = str(symbol_market(symbol) or "").upper()
        received = _received_after(datetime.now(timezone.utc), rows)
        check_fetch_control(deadline, cancelled)
        try:
            gateway = provider.open_quote_gateway(
                host=str(target.get("host") or "127.0.0.1"),
                port=int(target.get("port") or 11111),
                **controls,
            )
        except Exception:
            check_fetch_control(deadline, cancelled)
            gateway = None
        if gateway is None:
            reason_codes.append("quote_refresh_state_unavailable")
            continue
        try:
            state = _market_state_receipt(
                provider, gateway, symbol=symbol, received=received,
                deadline=deadline, cancelled=cancelled,
            )
            if state is not None:
                market_state[symbol] = state
            else:
                reason_codes.append("quote_refresh_state_unavailable")
            if market and market not in quote_rights:
                rights = _quote_rights_receipt(
                    provider, gateway, market=market, received=received,
                    deadline=deadline, cancelled=cancelled,
                )
                if rights is not None:
                    quote_rights[market] = rights
                else:
                    reason_codes.append("quote_refresh_rights_unavailable")
        finally:
            try:
                gateway.close()
            except Exception:
                pass
    check_fetch_control(deadline, cancelled)
    if not rows_by_symbol:
        status = "unavailable"
    elif reason_codes:
        status = "partial"
    else:
        status = "refreshed"
    return {
        "schema_version": ROLL_QUOTE_REFRESH_SCHEMA,
        "status": status,
        "reason_codes": sorted(set(reason_codes)),
        "rows": rows_by_symbol,
        "market_state": market_state,
        "quote_rights": quote_rights,
        "observed_at_utc": min(observations).isoformat() if observations else None,
        "refreshed_contracts": refreshed_contracts,
    }


__all__ = [
    "ROLL_QUOTE_REFRESH_MIN_AGE_SECONDS",
    "ROLL_QUOTE_REFRESH_SCHEMA",
    "refresh_roll_quote_evidence",
]
