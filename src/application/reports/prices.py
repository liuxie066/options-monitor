"""Underlier price observations for reports that need a current reference price.

Two sources are supported:

* an explicit JSON file passed with ``--prices-file`` (tests, manual snapshots);
* the latest daily decision briefs under ``output_accounts/*/state``.

A missing price is never treated as zero.  Consumers must render it as
unavailable and keep the boundary rows visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Iterable

from domain.domain.symbol_identity import canonical_symbol
from src.application.agent_tool_contracts import AgentToolError


RUNTIME_BRIEF_SOURCE = "runtime_daily_brief"
PRICES_FILE_SOURCE = "prices_file"


@dataclass(frozen=True)
class PriceObservation:
    symbol: str
    price: float
    source: str
    observed_at_utc: str | None = None
    source_path: str | None = None


@dataclass(frozen=True)
class PriceSnapshot:
    observations: dict[str, PriceObservation]
    source: str
    observed_at_utc: str | None = None

    def get(self, symbol: str) -> PriceObservation | None:
        return self.observations.get(canonical_symbol(symbol))


def resolve_price_snapshot(
    *,
    runtime_root: str | Path,
    prices_file: str | Path | None = None,
    markets: Iterable[str] = ("HK", "US"),
) -> PriceSnapshot:
    if prices_file:
        return load_prices_file(prices_file)
    return load_runtime_brief_prices(Path(runtime_root), markets=markets)


def load_prices_file(path: str | Path) -> PriceSnapshot:
    source_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentToolError(
            code="REPORT_PRICES_INVALID",
            message=f"prices file not found: {source_path}",
        ) from exc
    except (OSError, ValueError) as exc:
        raise AgentToolError(
            code="REPORT_PRICES_INVALID",
            message=f"prices file is not valid JSON: {source_path}",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not isinstance(payload, dict):
        raise AgentToolError(
            code="REPORT_PRICES_INVALID",
            message="prices file must contain a JSON object",
        )
    generated_at = _text_or_none(payload.get("generated_at_utc"))
    raw_prices = payload.get("prices") if isinstance(payload.get("prices"), dict) else payload
    observations: dict[str, PriceObservation] = {}
    errors: list[str] = []
    for raw_symbol, raw_value in raw_prices.items():
        symbol = str(raw_symbol or "").strip()
        if not symbol or symbol in {"generated_at_utc", "schema_version", "source"}:
            continue
        price_value: Any = raw_value
        observed_at = generated_at
        if isinstance(raw_value, dict):
            price_value = raw_value.get("price")
            observed_at = _text_or_none(raw_value.get("observed_at_utc")) or generated_at
        price = _finite_positive(price_value)
        if not symbol or price is None:
            errors.append(f"{symbol or '<empty>'}: price must be a positive finite number")
            continue
        try:
            key = canonical_symbol(symbol)
        except Exception as exc:
            errors.append(f"{symbol}: invalid symbol ({exc})")
            continue
        observations[key] = PriceObservation(
            symbol=key,
            price=price,
            source=PRICES_FILE_SOURCE,
            observed_at_utc=observed_at,
            source_path=str(source_path),
        )
    if errors:
        raise AgentToolError(
            code="REPORT_PRICES_INVALID",
            message="prices file contains invalid entries",
            details={"errors": errors, "path": str(source_path)},
        )
    if not observations:
        raise AgentToolError(
            code="REPORT_PRICES_INVALID",
            message="prices file contains no usable prices",
            details={"path": str(source_path)},
        )
    return PriceSnapshot(observations=observations, source=f"{PRICES_FILE_SOURCE}:{source_path}", observed_at_utc=generated_at)


def load_runtime_brief_prices(
    runtime_root: str | Path,
    *,
    markets: Iterable[str] = ("HK", "US"),
) -> PriceSnapshot:
    root = Path(runtime_root).expanduser().resolve()
    observations: dict[str, PriceObservation] = {}
    observed_at_by_symbol: dict[str, datetime] = {}
    newest_overall: datetime | None = None
    accounts_root = root / "output_accounts"
    if not accounts_root.is_dir():
        return PriceSnapshot(observations={}, source=RUNTIME_BRIEF_SOURCE)
    for market in markets:
        market_norm = str(market or "").strip().upper()
        if not market_norm:
            continue
        for path in sorted(accounts_root.glob(f"*/state/daily_decision_brief.{market_norm}.current.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            generated = _parse_iso(payload.get("generated_at_utc")) or _file_mtime(path)
            if generated is None:
                continue
            newest_overall = generated if newest_overall is None or generated > newest_overall else newest_overall
            for item in payload.get("candidate_index") or []:
                if not isinstance(item, dict):
                    continue
                representative = item.get("representative") if isinstance(item.get("representative"), dict) else {}
                metrics = representative.get("metrics") if isinstance(representative.get("metrics"), dict) else {}
                event_risk = representative.get("event_risk") if isinstance(representative.get("event_risk"), dict) else {}
                symbol = str(metrics.get("symbol") or event_risk.get("symbol") or "").strip()
                price = _finite_positive(metrics.get("spot"))
                if not symbol or price is None:
                    continue
                try:
                    key = canonical_symbol(symbol)
                except Exception:
                    continue
                previous = observed_at_by_symbol.get(key)
                if previous is not None and generated <= previous:
                    continue
                observed_at_by_symbol[key] = generated
                observations[key] = PriceObservation(
                    symbol=key,
                    price=price,
                    source=RUNTIME_BRIEF_SOURCE,
                    observed_at_utc=generated.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    source_path=str(path.resolve()),
                )
    observed_at = (
        newest_overall.isoformat(timespec="seconds").replace("+00:00", "Z")
        if newest_overall is not None
        else None
    )
    return PriceSnapshot(
        observations=observations,
        source=RUNTIME_BRIEF_SOURCE,
        observed_at_utc=observed_at,
    )


def _finite_positive(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _text_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _parse_iso(value: Any) -> datetime | None:
    text = _text_or_none(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _file_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


__all__ = [
    "PRICES_FILE_SOURCE",
    "RUNTIME_BRIEF_SOURCE",
    "PriceObservation",
    "PriceSnapshot",
    "load_prices_file",
    "load_runtime_brief_prices",
    "resolve_price_snapshot",
]
