from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from domain.domain.performance.models import (
    EvidenceEnvelope,
    FXRateFact,
    OptionInstrumentKey,
    OptionValuationPosition,
    StockInstrumentKey,
    ValuationMarkFact,
    canonical_decimal_text,
    normalize_currency,
    to_decimal,
)


@dataclass(frozen=True)
class CurrentEvidenceCollection:
    status: str
    valuation_marks: tuple[ValuationMarkFact, ...] = ()
    fx_rates: tuple[FXRateFact, ...] = ()
    diagnostics: tuple[dict[str, Any], ...] = ()

    @property
    def envelope(self) -> EvidenceEnvelope:
        return EvidenceEnvelope(valuation_marks=self.valuation_marks, fx_rates=self.fx_rates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "valuation_mark_count": len(self.valuation_marks),
            "fx_rate_count": len(self.fx_rates),
            "valuation_mark_fact_ids": [str(item.fact_id) for item in self.valuation_marks],
            "fx_rate_fact_ids": [str(item.fact_id) for item in self.fx_rates],
            "diagnostics": [dict(item) for item in self.diagnostics],
        }


OptionSnapshotRowsFetcher = Callable[[Sequence[OptionValuationPosition]], list[dict[str, Any]]]
StockPriceFetcher = Callable[[StockInstrumentKey], Mapping[str, Any] | float | None]
FXPayloadFetcher = Callable[[], Mapping[str, Any] | None]


def collect_current_performance_evidence(
    *,
    period_status: str,
    refresh_quotes: bool,
    option_positions: Sequence[OptionValuationPosition],
    stock_instruments: Sequence[StockInstrumentKey] = (),
    now_ms: int,
    cfg: Mapping[str, Any] | None = None,
    base_dir: str | Path | None = None,
    option_snapshot_rows_fetcher: OptionSnapshotRowsFetcher | None = None,
    stock_price_fetcher: StockPriceFetcher | None = None,
    fx_payload_fetcher: FXPayloadFetcher | None = None,
) -> CurrentEvidenceCollection:
    if period_status != "partial_current":
        return CurrentEvidenceCollection(status="skipped_historical")
    if not refresh_quotes:
        return CurrentEvidenceCollection(status="skipped_refresh_disabled")
    instant = int(now_ms)
    diagnostics: list[dict[str, Any]] = []
    marks: list[ValuationMarkFact] = []
    rates: list[FXRateFact] = []

    fetch_rows = option_snapshot_rows_fetcher or (
        lambda positions: _default_option_snapshot_rows(positions, cfg=cfg or {}, base_dir=base_dir)
    )
    unique_options_by_key: dict[str, OptionValuationPosition] = {}
    conflicting_option_keys: set[str] = set()
    for position in option_positions:
        key = position.instrument.instrument_key
        existing = unique_options_by_key.get(key)
        if (
            existing is not None
            and existing.market_code
            and position.market_code
            and existing.market_code != position.market_code
        ):
            conflicting_option_keys.add(key)
            diagnostics.append(
                _diag(
                    "option_market_code_conflict",
                    instrument_key=key,
                    market_codes=sorted({existing.market_code, position.market_code}),
                )
            )
            continue
        if existing is None or (not existing.market_code and position.market_code):
            unique_options_by_key[key] = position
    unique_options = tuple(
        position
        for key, position in unique_options_by_key.items()
        if key not in conflicting_option_keys
    )
    rows: list[dict[str, Any]] = []
    if unique_options:
        try:
            rows = [dict(item) for item in fetch_rows(unique_options) if isinstance(item, Mapping)]
        except Exception as exc:
            diagnostics.append(_diag("option_snapshot_fetch_failed", error=str(exc)))
    for position in unique_options:
        candidates = [row for row in rows if _row_matches_position(row, position)]
        if len(candidates) != 1:
            diagnostics.append(
                _diag(
                    "option_code_resolution_failed",
                    lot_id=position.lot_id,
                    instrument_key=position.instrument.instrument_key,
                    candidate_count=len(candidates),
                )
            )
            continue
        row = candidates[0]
        code = str(row.get("code") or row.get("contract_symbol") or position.market_code or "").strip()
        if not code:
            diagnostics.append(_diag("option_code_missing", lot_id=position.lot_id))
            continue
        price, mark_kind, mark_error = _option_mark(row)
        if price is None:
            diagnostics.append(_diag("option_mark_missing", lot_id=position.lot_id, contract_code=code, error=mark_error))
            continue
        effective_at_ms, timestamp_fallback = _snapshot_timestamp_ms(row, fallback_ms=instant)
        quality = {"persistence": "live_unpersisted"}
        if timestamp_fallback:
            quality["timestamp_fallback"] = True
        marks.append(
            ValuationMarkFact(
                fact_id=None,
                instrument=position.instrument,
                price=price,
                mark_kind=mark_kind,
                effective_at_ms=effective_at_ms,
                observed_at_ms=instant,
                source="realtime_snapshot",
                source_id=f"{code}:{effective_at_ms}",
                revision=1,
                quality=quality,
                raw=_json_safe(dict(row)),
            )
        )

    for instrument in stock_instruments:
        fetch_stock = stock_price_fetcher or (lambda item: _default_stock_price(item, cfg=cfg or {}, base_dir=base_dir))
        try:
            payload = fetch_stock(instrument)
        except Exception as exc:
            diagnostics.append(
                _diag("stock_snapshot_fetch_failed", instrument_key=instrument.instrument_key, error=str(exc))
            )
            continue
        if isinstance(payload, Mapping):
            raw = _json_safe(dict(payload))
            price_raw = raw.get("price") or raw.get("last_price") or raw.get("spot")
        else:
            raw = {"price": payload}
            price_raw = payload
        price = _positive_decimal(price_raw)
        if price is None:
            diagnostics.append(_diag("stock_mark_missing", instrument_key=instrument.instrument_key))
            continue
        effective_at_ms, timestamp_fallback = _snapshot_timestamp_ms(raw, fallback_ms=instant)
        quality = {"persistence": "live_unpersisted"}
        if timestamp_fallback:
            quality["timestamp_fallback"] = True
        marks.append(
            ValuationMarkFact(
                fact_id=None,
                instrument=instrument,
                price=price,
                mark_kind="spot",
                effective_at_ms=effective_at_ms,
                observed_at_ms=instant,
                source="realtime_snapshot",
                source_id=f"{instrument.instrument_key}:{effective_at_ms}",
                quality=quality,
                raw=raw,
            )
        )

    currencies = sorted(
        {position.currency for position in option_positions} | {item.currency for item in stock_instruments}
    )
    currencies = [item for item in currencies if item != "CNY"]
    if currencies:
        fetch_fx = fx_payload_fetcher or (lambda: _default_fx_payload(cfg=cfg or {}, base_dir=base_dir))
        try:
            fx_payload = fetch_fx()
        except Exception as exc:
            fx_payload = None
            diagnostics.append(_diag("fx_fetch_failed", error=str(exc)))
        if not isinstance(fx_payload, Mapping):
            diagnostics.append(_diag("fx_payload_missing"))
        else:
            rates_map = fx_payload.get("rates") if isinstance(fx_payload.get("rates"), Mapping) else fx_payload
            effective_at_ms, timestamp_fallback = _snapshot_timestamp_ms(fx_payload, fallback_ms=instant)
            age_ms = max(0, instant - effective_at_ms)
            source = "cache_snapshot" if age_ms > 24 * 3_600_000 else "realtime_snapshot"
            for currency in currencies:
                raw_rate = rates_map.get(f"{currency}CNY") if isinstance(rates_map, Mapping) else None
                rate = _positive_decimal(raw_rate)
                if rate is None:
                    diagnostics.append(_diag("fx_rate_missing", base_currency=currency, quote_currency="CNY"))
                    continue
                quality = {"persistence": "live_unpersisted"}
                if timestamp_fallback:
                    quality["timestamp_fallback"] = True
                if source == "cache_snapshot":
                    quality["stale_cache_fallback"] = True
                rates.append(
                    FXRateFact(
                        fact_id=None,
                        base_currency=currency,
                        quote_currency="CNY",
                        rate=rate,
                        rate_kind="spot",
                        effective_at_ms=effective_at_ms,
                        observed_at_ms=instant,
                        source=source,
                        source_id=f"{currency}CNY:{effective_at_ms}",
                        quality=quality,
                raw=_json_safe(dict(fx_payload)),
                    )
                )

    status = "collected" if marks or rates else "source_unavailable"
    return CurrentEvidenceCollection(
        status=status,
        valuation_marks=tuple(marks),
        fx_rates=tuple(rates),
        diagnostics=tuple(diagnostics),
    )


def capture_current_performance_evidence(**kwargs: Any) -> EvidenceEnvelope:
    result = collect_current_performance_evidence(**kwargs)
    if result.status not in {"collected", "source_unavailable"}:
        raise ValueError("evidence capture is current-only and requires refresh_quotes")
    return result.envelope


def _default_option_snapshot_rows(
    positions: Sequence[OptionValuationPosition],
    *,
    cfg: Mapping[str, Any],
    base_dir: str | Path | None,
) -> list[dict[str, Any]]:
    from src.application.opend_fetch_config import resolve_opend_batch_config, resolve_opend_fetch_limits
    from src.application.opend_market_snapshot_fetching import fetch_option_snapshots
    from src.application.opend_utils import get_trading_date, normalize_underlier
    from src.application.option_chain_fetching import OptionChainFetchRequest, fetch_option_chains
    from src.infrastructure.futu_gateway import build_ready_futu_gateway, retry_futu_gateway_call

    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
    host = str(cfg.get("opend_host") or cfg.get("host") or "127.0.0.1")
    port = int(cfg.get("opend_port") or cfg.get("port") or 11111)
    limits = resolve_opend_fetch_limits(dict(cfg))
    batch = resolve_opend_batch_config(dict(cfg))
    gateway = build_ready_futu_gateway(host=host, port=port, is_option_chain_cache_enabled=False)
    try:
        code_by_key: dict[str, str] = {}
        chain_row_by_key: dict[str, dict[str, Any]] = {}
        unresolved_by_symbol: dict[str, list[OptionValuationPosition]] = {}
        ambiguity_rows: list[dict[str, Any]] = []
        for position in positions:
            key = position.instrument.instrument_key
            if position.market_code:
                code_by_key[key] = position.market_code
            else:
                unresolved_by_symbol.setdefault(position.instrument.symbol, []).append(position)

        for symbol, symbol_positions in sorted(unresolved_by_symbol.items()):
            underlier = normalize_underlier(symbol, base_dir=root)
            expirations = sorted({item.instrument.expiration_ymd for item in symbol_positions})
            option_types = ",".join(sorted({item.instrument.option_type for item in symbol_positions}))
            chain = fetch_option_chains(
                gateway=gateway,
                request=OptionChainFetchRequest(
                    symbol=symbol,
                    underlier_code=underlier.code,
                    expirations=expirations,
                    host=host,
                    port=port,
                    option_types=option_types,
                    base_dir=root,
                    asof_date=get_trading_date(underlier.market).isoformat(),
                    freshness_policy="force_refresh",
                    chain_cache=False,
                    max_wait_sec=limits.option_chain.max_wait_sec,
                    window_sec=limits.option_chain.window_sec,
                    max_calls=limits.option_chain.max_calls,
                ),
                retry_call=retry_futu_gateway_call,
            )
            chain_rows = [dict(item) for item in chain.rows if isinstance(item, Mapping)]
            for position in symbol_positions:
                key = position.instrument.instrument_key
                candidates = [row for row in chain_rows if _row_matches_position(row, position)]
                if len(candidates) == 1:
                    code = str(candidates[0].get("code") or candidates[0].get("contract_symbol") or "").strip()
                    if code:
                        code_by_key[key] = code
                        chain_row_by_key[key] = candidates[0]
                elif len(candidates) > 1:
                    ambiguity_rows.extend({**row, "_requested_instrument_key": key} for row in candidates)

        exact_codes = sorted(set(code_by_key.values()))
        snapshots = fetch_option_snapshots(
            option_codes=exact_codes,
            gateway=gateway,
            snapshot_limit=limits.market_snapshot,
            base_dir=root,
            snapshot_batch_size=batch.market_snapshot,
            snapshot_fallback_max_codes=batch.market_snapshot_fallback_max_codes,
            snapshot_fallback_batch_size=batch.market_snapshot_fallback_batch_size,
        )
        out = list(ambiguity_rows)
        for position in positions:
            key = position.instrument.instrument_key
            code = code_by_key.get(key)
            if not code:
                continue
            out.append(
                {
                    **chain_row_by_key.get(key, {}),
                    **dict(snapshots.snap_map.get(code) or {}),
                    "code": code,
                    "_requested_instrument_key": key,
                }
            )
        return out
    finally:
        gateway.close()


def _default_stock_price(
    instrument: StockInstrumentKey,
    *,
    cfg: Mapping[str, Any],
    base_dir: str | Path | None,
) -> Mapping[str, Any] | float | None:
    from src.application.opend_market_snapshot_fetching import get_underlier_spot

    price = get_underlier_spot(
        instrument.symbol,
        host=str(cfg.get("opend_host") or cfg.get("host") or "127.0.0.1"),
        port=int(cfg.get("opend_port") or cfg.get("port") or 11111),
        base_dir=Path(base_dir) if base_dir is not None else None,
    )
    return {"price": price}


def _default_fx_payload(*, cfg: Mapping[str, Any], base_dir: str | Path | None) -> Mapping[str, Any] | None:
    from src.infrastructure.exchange_rates import get_exchange_rates_or_fetch_latest

    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
    cache_path = Path(cfg.get("exchange_rate_cache_path") or root / "output_shared" / "state" / "rate_cache.json")
    return get_exchange_rates_or_fetch_latest(
        cache_path=cache_path,
        max_age_hours=24,
        write_cache=False,
    )


def _row_matches_position(row: Mapping[str, Any], position: OptionValuationPosition) -> bool:
    requested = str(row.get("_requested_instrument_key") or "").strip()
    if requested:
        return requested == position.instrument.instrument_key
    code = str(row.get("code") or row.get("contract_symbol") or "").strip()
    if position.market_code:
        return code == position.market_code
    option_type = str(row.get("option_type") or row.get("type") or "").strip().lower()
    if option_type not in {position.instrument.option_type, position.instrument.option_type[0]}:
        return False
    expiration = str(row.get("expiration_ymd") or row.get("expiration") or row.get("strike_time") or "")[:10]
    if expiration != position.instrument.expiration_ymd:
        return False
    strike = _positive_decimal(row.get("strike") or row.get("strike_price"))
    if strike is None or strike != position.instrument.strike:
        return False
    multiplier_raw = row.get("multiplier") or row.get("option_contract_multiplier") or row.get("lot_size")
    if multiplier_raw not in (None, ""):
        multiplier = _positive_decimal(multiplier_raw)
        if multiplier is None or multiplier != position.instrument.multiplier:
            return False
    return bool(code)


def _option_mark(row: Mapping[str, Any]) -> tuple[Any | None, str | None, str | None]:
    bid = _positive_decimal(row.get("bid_price") or row.get("bid"))
    ask = _positive_decimal(row.get("ask_price") or row.get("ask"))
    if bid is not None and ask is not None:
        if ask < bid:
            return None, None, "crossed market"
        return (bid + ask) / 2, "midpoint", None
    last = _positive_decimal(row.get("last_price") or row.get("price") or row.get("cur_price"))
    if last is not None:
        return last, "last_fallback", None
    return None, None, "snapshot has no positive midpoint or last price"


def _positive_decimal(value: Any):
    try:
        out = to_decimal(value, field_name="value")
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _snapshot_timestamp_ms(payload: Mapping[str, Any], *, fallback_ms: int) -> tuple[int, bool]:
    for key in ("effective_at_ms", "snapshot_time_ms", "timestamp_ms", "update_time_ms"):
        try:
            value = int(payload.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if 0 < value <= int(fallback_ms):
            return value, False
    for key in ("timestamp", "update_time", "data_time", "time"):
        raw = str(payload.get(key) or "").strip()
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                continue
            value = int(parsed.astimezone(timezone.utc).timestamp() * 1000)
            if value <= int(fallback_ms):
                return value, False
        except ValueError:
            continue
    return int(fallback_ms), True


def _diag(code: str, **details: Any) -> dict[str, Any]:
    return {"code": code, **details}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return canonical_decimal_text(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    scalar = getattr(value, "item", None)
    if callable(scalar):
        try:
            return _json_safe(scalar())
        except (TypeError, ValueError):
            pass
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return str(isoformat())
        except (TypeError, ValueError):
            pass
    return str(value)


__all__ = [
    "CurrentEvidenceCollection",
    "capture_current_performance_evidence",
    "collect_current_performance_evidence",
]
