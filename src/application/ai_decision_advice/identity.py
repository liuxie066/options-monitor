from __future__ import annotations

import json
import os
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.symbol_identity import canonical_symbol, symbol_market
from src.application.ai_decision_advice.config import (
    SHARED_STATE_DIRNAME,
    SYMBOL_IDENTITY_SNAPSHOT_FILE,
)
from src.infrastructure.private_storage import (
    atomic_write_private_text,
    ensure_private_file,
    open_private_text,
)


SYMBOL_IDENTITY_SNAPSHOT_SCHEMA = "ai_decision_advice.symbol_identity_snapshot.v1"
OBSERVATION_SET_SCHEMA = "ai_decision_advice.observation_set.v1"
OBSERVATION_SET_FILE = "observation_set.json"
OBSERVATION_SET_LOCK_FILE = "observation_set.lock"

_IDENTITY_SEMANTIC_FIELDS = (
    "symbol",
    "market",
    "exchange",
    "name",
    "aliases",
    "status",
)
_OBSERVATION_PARTITION_KEYS = frozenset(
    {"market", "generation", "generated_at", "symbols", "content_sha256"}
)
_OBSERVATION_SYMBOL_KEYS = frozenset({"symbol", "market", "priority"})

# Observation-source priorities (docs/AI_DECISION_ADVICE_DESIGN.md 6.1).
PRIORITY_OPEN_OPTION = 1
PRIORITY_RECENT_CANDIDATE = 2
PRIORITY_STOCK_HOLDING = 3
PRIORITY_SCAN_CONFIG = 4

_BASICINFO_BATCH = 200


@dataclass(frozen=True)
class ObservedSymbol:
    symbol: str
    market: str
    priority: int
    sources: tuple[str, ...]


class ObservationSnapshotError(ValueError):
    """The anonymous observation handoff is malformed or has lost integrity."""


@dataclass(frozen=True)
class SymbolIdentity:
    symbol: str
    market: str
    exchange: str | None
    name: str | None
    aliases: tuple[str, ...] = ()
    status: str = "resolved"  # resolved | identity_unavailable

    def to_row(self, *, observed_at: str) -> dict[str, Any]:
        row = {
            "symbol": self.symbol,
            "market": self.market,
            "exchange": self.exchange,
            "name": self.name,
            "aliases": list(self.aliases),
            "status": self.status,
            "observed_at": observed_at,
        }
        row["identity_semantic_sha256"] = _identity_row_semantic_hash(row)
        return row


MarketSnapshotProvider = Callable[[str, list[str]], Mapping[str, Mapping[str, Any]]]
BasicInfoProvider = Callable[[list[str]], list[Mapping[str, Any]]]


def build_observation_set(
    *,
    scan_symbols: Iterable[str] = (),
    stock_holding_symbols: Iterable[str] = (),
    open_option_underlyings: Iterable[str] = (),
    recent_candidate_symbols: Iterable[str] = (),
) -> list[ObservedSymbol]:
    """Union of observation sources, canonicalized and deduped.

    Lower numeric priority wins when a symbol appears in multiple sources.
    """

    merged: dict[str, ObservedSymbol] = {}

    def _add(symbols: Iterable[str], priority: int, source: str) -> None:
        for raw in symbols:
            canonical = canonical_symbol(raw)
            if not canonical:
                continue
            market = symbol_market(canonical)
            if not market:
                continue
            existing = merged.get(canonical)
            if existing is None:
                merged[canonical] = ObservedSymbol(
                    symbol=canonical,
                    market=str(market).upper(),
                    priority=priority,
                    sources=(source,),
                )
                continue
            merged[canonical] = ObservedSymbol(
                symbol=canonical,
                market=existing.market,
                priority=min(existing.priority, priority),
                sources=tuple(sorted({*existing.sources, source})),
            )

    _add(open_option_underlyings, PRIORITY_OPEN_OPTION, "open_option")
    _add(recent_candidate_symbols, PRIORITY_RECENT_CANDIDATE, "recent_candidate")
    _add(stock_holding_symbols, PRIORITY_STOCK_HOLDING, "stock_holding")
    _add(scan_symbols, PRIORITY_SCAN_CONFIG, "scan_config")
    return sorted(merged.values(), key=lambda item: (item.priority, item.symbol))


def open_option_underlyings_from_lots(position_lots: Iterable[Mapping[str, Any]]) -> list[str]:
    """Extract open option underlyings from ledger position-lot field payloads."""

    out: list[str] = []
    for lot in position_lots:
        if str(lot.get("status") or "").strip().lower() != "open":
            continue
        if int(lot.get("contracts_open") or 0) <= 0:
            continue
        contract_key = lot.get("contract_key")
        if isinstance(contract_key, Mapping):
            underlying = contract_key.get("underlying_symbol")
        else:
            underlying = getattr(contract_key, "underlying_symbol", None)
        canonical = canonical_symbol(underlying)
        if canonical:
            out.append(canonical)
    return out


def stock_symbols_from_portfolio_context(portfolio_context: Mapping[str, Any] | None) -> list[str]:
    """Extract ordinary stock symbols from a frozen portfolio context payload."""

    if not isinstance(portfolio_context, Mapping):
        return []
    stocks = portfolio_context.get("stocks_by_symbol")
    if isinstance(stocks, Mapping):
        return [str(symbol) for symbol in stocks if str(symbol or "").strip()]
    out: list[str] = []
    for row in portfolio_context.get("positions") or []:
        if isinstance(row, Mapping) and row.get("symbol"):
            out.append(str(row["symbol"]))
    return out


def candidate_symbols_from_snapshot(snapshot: Mapping[str, Any] | None) -> list[str]:
    """Extract accepted candidate symbols from a sealed opening snapshot."""

    if not isinstance(snapshot, Mapping):
        return []
    out: list[str] = []
    for item in snapshot.get("ranked_candidates") or []:
        if not isinstance(item, Mapping):
            continue
        facts = item.get("facts")
        if isinstance(facts, Mapping) and facts.get("symbol"):
            out.append(str(facts["symbol"]))
    return out


def _resolve_symbol_identities(
    observed: list[ObservedSymbol],
    *,
    market_snapshot_provider: MarketSnapshotProvider | None,
    basic_info_provider: BasicInfoProvider | None,
    observed_at: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    unresolved: list[ObservedSymbol] = []

    by_market: dict[str, list[str]] = {}
    for item in observed:
        by_market.setdefault(item.market, []).append(item.symbol)

    snapshot_rows: dict[str, Mapping[str, Any]] = {}
    if market_snapshot_provider is not None:
        for market, symbols in sorted(by_market.items()):
            provided = market_snapshot_provider(market, sorted(symbols))
            # Provider keys may be OpenD codes (US.NVDA); only accept rows whose
            # key canonicalizes back to an observed symbol.
            wanted = set(symbols)
            for symbol, row in (provided or {}).items():
                canonical = canonical_symbol(symbol)
                if canonical and canonical in wanted:
                    snapshot_rows[canonical] = row

    for item in observed:
        row = snapshot_rows.get(item.symbol)
        name = _row_name(row)
        if name:
            rows.append(
                SymbolIdentity(
                    symbol=item.symbol,
                    market=item.market,
                    exchange=_row_exchange(row),
                    name=name,
                    status="resolved",
                ).to_row(observed_at=observed_at)
            )
        else:
            unresolved.append(item)

    basicinfo_rows: dict[str, Mapping[str, Any]] = {}
    if unresolved and basic_info_provider is not None:
        codes = [item.symbol for item in unresolved]
        wanted = set(codes)
        for start in range(0, len(codes), _BASICINFO_BATCH):
            batch = codes[start : start + _BASICINFO_BATCH]
            for row in basic_info_provider(batch) or []:
                if not isinstance(row, Mapping):
                    continue
                canonical = canonical_symbol(row.get("code"))
                if canonical and canonical in wanted:
                    basicinfo_rows[canonical] = row

    for item in unresolved:
        row = basicinfo_rows.get(item.symbol)
        name = _row_name(row)
        if name:
            rows.append(
                SymbolIdentity(
                    symbol=item.symbol,
                    market=item.market,
                    exchange=_row_exchange(row),
                    name=name,
                    status="resolved",
                ).to_row(observed_at=observed_at)
            )
        else:
            rows.append(
                SymbolIdentity(
                    symbol=item.symbol,
                    market=item.market,
                    exchange=None,
                    name=None,
                    status="identity_unavailable",
                ).to_row(observed_at=observed_at)
            )

    return sorted(rows, key=lambda row: row["symbol"])


def _row_name(row: Mapping[str, Any] | None) -> str | None:
    if not isinstance(row, Mapping):
        return None
    for key in ("name", "stock_name", "asset_name"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return None


def _row_exchange(row: Mapping[str, Any] | None) -> str | None:
    if not isinstance(row, Mapping):
        return None
    for key in ("exchange_type", "exchange", "exch_type"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return None


def build_symbol_identity_snapshot(
    observed: list[ObservedSymbol],
    *,
    market_snapshot_provider: MarketSnapshotProvider | None = None,
    basic_info_provider: BasicInfoProvider | None = None,
    observed_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Resolve identities and assemble the frozen snapshot payload."""

    observed_at_text = _utc_iso(observed_at)
    symbols = _resolve_symbol_identities(
        observed,
        market_snapshot_provider=market_snapshot_provider,
        basic_info_provider=basic_info_provider,
        observed_at=observed_at_text,
    )
    payload: dict[str, Any] = {
        "schema_version": SYMBOL_IDENTITY_SNAPSHOT_SCHEMA,
        "observed_at": observed_at_text,
        "symbols": symbols,
    }
    payload["semantic_sha256"] = canonical_sha256(
        {
            "schema_version": SYMBOL_IDENTITY_SNAPSHOT_SCHEMA,
            "symbols": [_identity_row_semantics(row) for row in symbols],
        }
    )
    payload["content_sha256"] = canonical_sha256(payload)
    return payload


def _identity_row_semantics(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in _IDENTITY_SEMANTIC_FIELDS}


def _identity_row_semantic_hash(row: Mapping[str, Any]) -> str:
    return canonical_sha256(_identity_row_semantics(row))


def snapshot_path(base: Path) -> Path:
    return Path(base) / "output_shared" / "state" / SHARED_STATE_DIRNAME / SYMBOL_IDENTITY_SNAPSHOT_FILE


def publish_symbol_identity_snapshot(*, base: Path, payload: Mapping[str, Any]) -> Path:
    """Atomically publish the identity snapshot (docs/AI_DECISION_ADVICE_DESIGN.md 5.2)."""

    path = snapshot_path(base)
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    atomic_write_private_text(path, encoded)
    return path


def load_symbol_identity_snapshot(base: Path) -> dict[str, Any] | None:
    path = snapshot_path(base)
    if not path.exists():
        return None
    try:
        with open_private_text(path) as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != SYMBOL_IDENTITY_SNAPSHOT_SCHEMA:
        return None
    content_hash = str(payload.get("content_sha256") or "")
    without_content_hash = dict(payload)
    without_content_hash.pop("content_sha256", None)
    if content_hash != canonical_sha256(without_content_hash):
        return None
    rows = payload.get("symbols")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        if str(row.get("identity_semantic_sha256") or "") != _identity_row_semantic_hash(row):
            return None
    expected_semantic_hash = canonical_sha256(
        {
            "schema_version": SYMBOL_IDENTITY_SNAPSHOT_SCHEMA,
            "symbols": [_identity_row_semantics(row) for row in rows],
        }
    )
    if str(payload.get("semantic_sha256") or "") != expected_semantic_hash:
        return None
    return payload


def identity_by_symbol(snapshot: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if not isinstance(snapshot, Mapping):
        return rows
    for row in snapshot.get("symbols") or []:
        if isinstance(row, Mapping) and row.get("symbol"):
            rows[str(row["symbol"])] = dict(row)
    return rows


def identity_semantic_hash_by_symbol(
    snapshot: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Return the per-symbol identity binding used by evidence records."""

    return {
        symbol: str(row.get("identity_semantic_sha256") or "")
        for symbol, row in identity_by_symbol(snapshot).items()
        if str(row.get("identity_semantic_sha256") or "")
    }


def observation_set_path(base: Path) -> Path:
    return (
        Path(base)
        / "output_shared"
        / "state"
        / SHARED_STATE_DIRNAME
        / OBSERVATION_SET_FILE
    )


def observation_set_lock_path(base: Path) -> Path:
    return observation_set_path(base).with_name(OBSERVATION_SET_LOCK_FILE)


@contextmanager
def _observation_set_lock(base: Path, *, exclusive: bool):
    """Serialize the strict read-modify-replace boundary across Tick writers."""

    lock_path = ensure_private_file(observation_set_lock_path(base))
    flags = os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags)
    try:
        fcntl.flock(
            descriptor,
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def publish_observation_partition(
    *,
    base: Path,
    market: str,
    observed: Iterable[ObservedSymbol | Mapping[str, Any]],
    generation: str,
    generated_at: datetime | str | None = None,
) -> Path:
    """Replace one anonymous market partition without exposing source accounts."""

    market_value = str(market or "").strip().upper()
    if not market_value:
        raise ObservationSnapshotError("observation market is required")
    generation_value = str(generation or "").strip()
    if not generation_value:
        raise ObservationSnapshotError("observation generation is required")
    try:
        generated_at_value = _utc_iso(generated_at)
    except ValueError as exc:
        raise ObservationSnapshotError(
            "observation generated_at is invalid"
        ) from exc
    rows = _observation_rows(observed, expected_market=market_value)
    partition_without_hash: dict[str, Any] = {
        "market": market_value,
        "generation": generation_value,
        "generated_at": generated_at_value,
        "symbols": rows,
    }
    partition = {
        **partition_without_hash,
        "content_sha256": canonical_sha256(partition_without_hash),
    }

    with _observation_set_lock(base, exclusive=True):
        current = _load_observation_set_unlocked(base)
        partitions = dict((current or {}).get("partitions") or {})
        partitions[market_value] = partition
        payload = {
            "schema_version": OBSERVATION_SET_SCHEMA,
            "partitions": {
                key: partitions[key]
                for key in sorted(partitions)
            },
        }
        path = observation_set_path(base)
        atomic_write_private_text(
            path,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        return path


def load_observation_set(base: Path) -> dict[str, Any] | None:
    """Load one consistent anonymous observation snapshot, failing closed."""

    with _observation_set_lock(base, exclusive=False):
        return _load_observation_set_unlocked(base)


def _load_observation_set_unlocked(base: Path) -> dict[str, Any] | None:
    path = observation_set_path(base)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        with open_private_text(path) as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ObservationSnapshotError("observation snapshot is unreadable") from exc
    _validate_observation_snapshot(payload)
    return dict(payload)


def _validate_observation_snapshot(payload: Any) -> None:
    if not isinstance(payload, Mapping):
        raise ObservationSnapshotError("observation snapshot must be an object")
    if set(payload) != {"schema_version", "partitions"}:
        raise ObservationSnapshotError("observation snapshot fields are invalid")
    if payload.get("schema_version") != OBSERVATION_SET_SCHEMA:
        raise ObservationSnapshotError("observation snapshot schema is invalid")
    partitions = payload.get("partitions")
    if not isinstance(partitions, Mapping):
        raise ObservationSnapshotError("observation partitions must be an object")
    for partition_key, raw_partition in partitions.items():
        market = str(partition_key or "").strip().upper()
        if market != partition_key or not isinstance(raw_partition, Mapping):
            raise ObservationSnapshotError("observation partition market is invalid")
        if set(raw_partition) != _OBSERVATION_PARTITION_KEYS:
            raise ObservationSnapshotError("observation partition fields are invalid")
        if raw_partition.get("market") != market:
            raise ObservationSnapshotError("observation partition market mismatch")
        if not str(raw_partition.get("generation") or "").strip():
            raise ObservationSnapshotError("observation partition generation is invalid")
        _require_timestamp(raw_partition.get("generated_at"), "generated_at")
        rows = raw_partition.get("symbols")
        if not isinstance(rows, list):
            raise ObservationSnapshotError("observation symbols must be an array")
        normalized = _observation_rows(rows, expected_market=market)
        if normalized != rows:
            raise ObservationSnapshotError("observation symbols are not canonical and ordered")
        without_hash = {
            key: raw_partition.get(key)
            for key in ("market", "generation", "generated_at", "symbols")
        }
        if str(raw_partition.get("content_sha256") or "") != canonical_sha256(without_hash):
            raise ObservationSnapshotError("observation partition hash mismatch")


def _observation_rows(
    observed: Iterable[ObservedSymbol | Mapping[str, Any]],
    *,
    expected_market: str,
) -> list[dict[str, Any]]:
    by_symbol: dict[str, dict[str, Any]] = {}
    for item in observed:
        if isinstance(item, ObservedSymbol):
            raw_symbol = item.symbol
            raw_market = item.market
            raw_priority = item.priority
        elif isinstance(item, Mapping):
            if set(item) != _OBSERVATION_SYMBOL_KEYS:
                raise ObservationSnapshotError("observation symbol fields are invalid")
            raw_symbol = item.get("symbol")
            raw_market = item.get("market")
            raw_priority = item.get("priority")
        else:
            raise ObservationSnapshotError("observation symbol row is invalid")
        symbol = canonical_symbol(raw_symbol)
        market = str(raw_market or "").strip().upper()
        if not symbol or symbol != str(raw_symbol) or market != expected_market:
            raise ObservationSnapshotError("observation symbol identity is invalid")
        if symbol_market(symbol) != expected_market:
            raise ObservationSnapshotError("observation symbol market mismatch")
        if (
            not isinstance(raw_priority, int)
            or isinstance(raw_priority, bool)
            or raw_priority not in {
                PRIORITY_OPEN_OPTION,
                PRIORITY_RECENT_CANDIDATE,
                PRIORITY_STOCK_HOLDING,
                PRIORITY_SCAN_CONFIG,
            }
        ):
            raise ObservationSnapshotError("observation symbol priority is invalid")
        existing = by_symbol.get(symbol)
        row = {
            "symbol": symbol,
            "market": expected_market,
            "priority": raw_priority,
        }
        if existing is None or raw_priority < int(existing["priority"]):
            by_symbol[symbol] = row
    return sorted(by_symbol.values(), key=lambda row: (int(row["priority"]), row["symbol"]))


def observed_symbols_from_snapshot(
    snapshot: Mapping[str, Any] | None,
) -> list[ObservedSymbol]:
    if snapshot is None:
        return []
    _validate_observation_snapshot(snapshot)
    merged: dict[str, ObservedSymbol] = {}
    for partition in (snapshot.get("partitions") or {}).values():
        for row in partition.get("symbols") or []:
            symbol = str(row["symbol"])
            item = ObservedSymbol(
                symbol=symbol,
                market=str(row["market"]),
                priority=int(row["priority"]),
                sources=(),
            )
            current = merged.get(symbol)
            if current is None or item.priority < current.priority:
                merged[symbol] = item
    return sorted(merged.values(), key=lambda item: (item.priority, item.symbol))


def _require_timestamp(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservationSnapshotError(f"observation {field_name} is invalid") from exc
    if parsed.tzinfo is None:
        raise ObservationSnapshotError(f"observation {field_name} must include timezone")
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ObservationSnapshotError(f"observation {field_name} must be UTC")
    return text


@dataclass
class RefreshQueue:
    """Priority queue with starvation protection (docs 6.1).

    Within one priority tier, the symbol with the oldest (or missing)
    last-attempt timestamp comes first; unfinished symbols re-enter the head
    of their tier.
    """

    entries: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        observed: list[ObservedSymbol],
        *,
        last_attempt_by_symbol: Mapping[str, str] | None = None,
    ) -> "RefreshQueue":
        attempts = last_attempt_by_symbol or {}
        entries = [
            {
                "symbol": item.symbol,
                "market": item.market,
                "priority": item.priority,
                "last_attempt": attempts.get(item.symbol),
            }
            for item in observed
        ]
        entries.sort(
            key=lambda row: (
                int(row["priority"]),
                row["last_attempt"] is not None,
                str(row["last_attempt"] or ""),
                str(row["symbol"]),
            )
        )
        return cls(entries=entries)

    def requeue_unfinished(self, symbols: Iterable[str]) -> None:
        """Move unfinished symbols to the head of their priority tier."""

        pending = {str(symbol) for symbol in symbols}
        if not pending:
            return
        head: list[dict[str, Any]] = []
        tail: list[dict[str, Any]] = []
        for entry in self.entries:
            if entry["symbol"] in pending:
                head.append(entry)
            else:
                tail.append(entry)
        head.sort(key=lambda row: (int(row["priority"]), str(row["symbol"])))
        self.entries = head + tail

    def symbols(self) -> list[str]:
        return [str(entry["symbol"]) for entry in self.entries]


def _utc_iso(value: datetime | str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc).isoformat()
