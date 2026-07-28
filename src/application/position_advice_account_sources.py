from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from domain.domain.position_advice_authority import (
    capacity_pool_authority_id,
    normalize_account_label,
    normalize_portfolio_source,
    portfolio_account_identity_hash,
    scope_for,
)
from src.application.ledger.api import (
    decision_state_snapshot,
    open_position_ledger,
)
from src.application.position_advice_source_producers import (
    publish_candidate_decisions_snapshot,
    publish_cash_capacity_snapshot,
    publish_fx_source_snapshot,
    publish_ledger_source_snapshot,
    publish_portfolio_source_snapshot,
    publish_share_coverage_snapshot,
)
from src.application.position_advice_source_receipts import (
    PositionAdviceSourceError,
    sha256_bytes,
    source_dependency_from_receipt,
)


CASH_SCOPE_SEMANTICS_VERSION = "uncommitted_headroom.v1"


class PositionAdviceAccountSourceError(RuntimeError):
    """Raised when one Account Run cannot publish coherent v2 source receipts."""


def publish_account_run_sources(
    *,
    account_run_id: str,
    normalized_account: str,
    broker: str,
    included_markets: Iterable[str],
    account_state_dir: Path,
    required_data_root: Path,
    decision_snapshot_reader: Callable[[], Mapping[str, Any]],
    completed_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Publish the primary and derived Position Advice receipts for one Account Run.

    All inputs must already have been produced by the current Account Run.  This
    function never refreshes broker, quote, ledger, or FX facts.
    """

    account = normalize_account_label(normalized_account)
    run_id = _required_text(account_run_id, "account_run_id")
    markets = _markets(included_markets)
    state_root = _existing_directory(account_state_dir, "account_state_dir")
    quote_root = _existing_directory(required_data_root, "required_data_root")
    portfolio_context = _read_json_object(
        state_root / "portfolio_context.json",
        "portfolio context",
    )
    portfolio_source = normalize_portfolio_source(
        portfolio_context.get("portfolio_source_name")
    )
    account_identifiers = _identity_values(
        portfolio_context.get("source_account_identifiers")
    )
    identity_hash = portfolio_account_identity_hash(
        normalized_portfolio_source=portfolio_source,
        broker_account_identifiers=account_identifiers,
    )
    capacity_authority = capacity_pool_authority_id(
        normalized_portfolio_source=portfolio_source,
        broker_account_identifiers=account_identifiers,
        cash_scope_semantics_version=CASH_SCOPE_SEMANTICS_VERSION,
    )
    decision_snapshot = dict(decision_snapshot_reader() or {})
    completed = completed_at or datetime.now(timezone.utc)

    receipt_records: list[dict[str, Any]] = []
    portfolio_path, portfolio_receipt = publish_portfolio_source_snapshot(
        producer_root=state_root,
        account_run_id=run_id,
        account=account,
        broker=_required_text(broker, "broker"),
        normalized_portfolio_source=portfolio_source,
        portfolio_account_identity_hash=identity_hash,
        included_markets=markets,
        portfolio_context=portfolio_context,
        completed_at=completed,
    )
    receipt_records.append(
        _receipt_record(
            source_kind="portfolio",
            producer_root=state_root,
            receipt_path=portfolio_path,
            receipt=portfolio_receipt,
        )
    )

    ledger_path, ledger_receipt = publish_ledger_source_snapshot(
        producer_root=state_root,
        account_run_id=run_id,
        account=account,
        broker=_required_text(broker, "broker"),
        portfolio_account_identity_hash=identity_hash,
        included_markets=markets,
        decision_state_snapshot=decision_snapshot,
        completed_at=completed,
    )
    receipt_records.append(
        _receipt_record(
            source_kind="ledger_decision_state",
            producer_root=state_root,
            receipt_path=ledger_path,
            receipt=ledger_receipt,
        )
    )

    portfolio_dependency = source_dependency_from_receipt(
        receipt_path=portfolio_path,
        producer_root=state_root,
        now=completed,
        expected_source_kind="portfolio",
    )
    ledger_dependency = source_dependency_from_receipt(
        receipt_path=ledger_path,
        producer_root=state_root,
        now=completed,
        expected_source_kind="ledger_decision_state",
    )

    candidate_capture = _read_json_object(
        state_root / "position_advice_candidate_all_decisions.raw.json",
        "candidate all-decisions capture",
    )
    _validate_candidate_capture(
        candidate_capture,
        account_run_id=run_id,
        account=account,
    )
    quote_records, quote_dependencies = _quote_dependencies(
        capture=candidate_capture,
        required_data_root=quote_root,
        now=completed,
    )
    receipt_records.extend(quote_records)
    candidate_observed_at = _latest_observation(
        item["receipt"]["source_observed_at"] for item in quote_records
    )
    candidate_path, candidate_receipt = publish_candidate_decisions_snapshot(
        producer_root=state_root,
        account_run_id=run_id,
        account=account,
        broker=_required_text(broker, "broker"),
        portfolio_account_identity_hash=identity_hash,
        included_markets=markets,
        decisions=candidate_capture.get("candidate_decisions") or [],
        quote_dependencies=quote_dependencies,
        source_observed_at=candidate_observed_at,
        completed_at=completed,
    )
    receipt_records.append(
        _receipt_record(
            source_kind="candidate_decisions",
            producer_root=state_root,
            receipt_path=candidate_path,
            receipt=candidate_receipt,
        )
    )

    fx_payload = _read_json_object(
        state_root / "rate_cache.json",
        "exchange-rate snapshot",
    )
    fx_path, fx_receipt = publish_fx_source_snapshot(
        producer_root=state_root,
        producer_run_id=run_id,
        included_markets=markets,
        fx_payload=fx_payload,
        source_observed_at=_required_text(
            fx_payload.get("timestamp"),
            "FX timestamp",
        ),
        provider=_required_text(fx_payload.get("source"), "FX provider"),
        completed_at=completed,
    )
    receipt_records.append(
        _receipt_record(
            source_kind="fx",
            producer_root=state_root,
            receipt_path=fx_path,
            receipt=fx_receipt,
        )
    )
    fx_dependency = source_dependency_from_receipt(
        receipt_path=fx_path,
        producer_root=state_root,
        now=completed,
        expected_source_kind="fx",
    )

    option_context = _read_json_object(
        state_root / "option_positions_context.json",
        "option positions context",
    )
    _validate_option_context_fingerprint(
        option_context,
        decision_snapshot=decision_snapshot,
    )
    derived_observed_at = _latest_observation(
        (
            portfolio_receipt["source_observed_at"],
            ledger_receipt["source_observed_at"],
        )
    )
    cash_capacity = build_cash_capacity(
        portfolio_context=portfolio_context,
        option_positions_context=option_context,
        fx_payload=fx_payload,
    )
    cash_path, cash_receipt = publish_cash_capacity_snapshot(
        producer_root=state_root,
        account_run_id=run_id,
        account=account,
        broker=_required_text(broker, "broker"),
        portfolio_account_identity_hash=identity_hash,
        included_markets=markets,
        capacity_pool_authority_id=capacity_authority,
        cash_capacity=cash_capacity,
        dependencies=(
            portfolio_dependency,
            ledger_dependency,
            fx_dependency,
        ),
        source_observed_at=derived_observed_at,
        completed_at=completed,
    )
    receipt_records.append(
        _receipt_record(
            source_kind="cash_capacity",
            producer_root=state_root,
            receipt_path=cash_path,
            receipt=cash_receipt,
        )
    )

    share_coverage = build_share_coverage(
        portfolio_context=portfolio_context,
        option_positions_context=option_context,
    )
    coverage_path, coverage_receipt = publish_share_coverage_snapshot(
        producer_root=state_root,
        account_run_id=run_id,
        account=account,
        broker=_required_text(broker, "broker"),
        portfolio_account_identity_hash=identity_hash,
        included_markets=markets,
        share_coverage=share_coverage,
        dependencies=(portfolio_dependency, ledger_dependency),
        source_observed_at=derived_observed_at,
        completed_at=completed,
    )
    receipt_records.append(
        _receipt_record(
            source_kind="share_coverage",
            producer_root=state_root,
            receipt_path=coverage_path,
            receipt=coverage_receipt,
        )
    )

    return {
        "schema_version": "position_advice_account_sources.v2",
        "account_run_id": run_id,
        "account": account,
        "broker": _required_text(broker, "broker"),
        "included_markets": markets,
        "portfolio_scope_id": scope_for(account),
        "normalized_portfolio_source": portfolio_source,
        "portfolio_account_identity_hash": identity_hash,
        "capacity_pool_authority_id": capacity_authority,
        "decision_state_snapshot": decision_snapshot,
        "cash_capacity": cash_capacity,
        "share_coverage": share_coverage,
        "receipts": receipt_records,
        "source_kinds": sorted(
            {str(item["source_kind"]) for item in receipt_records}
        ),
    }


def publish_account_position_advice_sources(
    *,
    account_run_root: Path,
    account_state_dir: Path,
    quote_producer_root: Path,
    data_config_path: Path,
    account_run_id: str,
    account: str,
    broker: str,
    included_markets: Iterable[str],
    completed_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Account Run facade with a JSON-safe result for audit/state persistence."""

    account_value = normalize_account_label(account)
    repo = open_position_ledger(Path(data_config_path))
    result = publish_account_run_sources(
        account_run_id=account_run_id,
        normalized_account=account_value,
        broker=broker,
        included_markets=included_markets,
        account_state_dir=account_state_dir,
        required_data_root=quote_producer_root,
        decision_snapshot_reader=lambda: decision_state_snapshot(
            repo,
            account=account_value,
            portfolio_scope_id=scope_for(account_value),
        ),
        completed_at=completed_at,
    )
    account_root = Path(account_run_root).resolve()
    state_root = Path(account_state_dir).resolve()
    if state_root.parent != account_root:
        raise PositionAdviceAccountSourceError(
            "account state directory is not owned by the Account Run"
        )
    return {
        "schema_version": result["schema_version"],
        "account_run_id": result["account_run_id"],
        "account": result["account"],
        "broker": result["broker"],
        "included_markets": result["included_markets"],
        "portfolio_scope_id": result["portfolio_scope_id"],
        "normalized_portfolio_source": result[
            "normalized_portfolio_source"
        ],
        "portfolio_account_identity_hash": result[
            "portfolio_account_identity_hash"
        ],
        "capacity_pool_authority_id": result[
            "capacity_pool_authority_id"
        ],
        "decision_state_snapshot": result["decision_state_snapshot"],
        "cash_capacity": result["cash_capacity"],
        "share_coverage": result["share_coverage"],
        "source_kinds": result["source_kinds"],
        "source_receipts": [
            {
                "source_kind": item["source_kind"],
                "producer_root": str(item["producer_root"]),
                "receipt_path": str(item["receipt_path"]),
                "snapshot_id": item["receipt"].get("snapshot_id"),
                "receipt_hash": sha256_bytes(
                    Path(item["receipt_path"]).read_bytes()
                ),
            }
            for item in result["receipts"]
        ],
    }


def build_cash_capacity(
    *,
    portfolio_context: Mapping[str, Any],
    option_positions_context: Mapping[str, Any],
    fx_payload: Mapping[str, Any],
) -> dict[str, Any]:
    rates = fx_payload.get("rates")
    if not isinstance(rates, Mapping) or not rates:
        raise PositionAdviceAccountSourceError("FX rates are unavailable")
    cash_by_currency = portfolio_context.get("cash_by_currency")
    if not isinstance(cash_by_currency, Mapping):
        raise PositionAdviceAccountSourceError("portfolio cash is unavailable")
    available = Decimal("0")
    native: dict[str, str] = {}
    for raw_currency, raw_amount in sorted(cash_by_currency.items()):
        currency = str(raw_currency or "").strip().upper()
        amount = _decimal(raw_amount, f"cash {currency}")
        native[currency] = _decimal_text(amount)
        available += _to_cny(amount, currency=currency, rates=rates)

    if option_positions_context.get("decision_snapshot_status") != "trusted":
        raise PositionAdviceAccountSourceError(
            "option-position decision snapshot is not trusted"
        )
    unavailable = option_positions_context.get(
        "cash_secured_unavailable_by_symbol"
    )
    if isinstance(unavailable, Mapping) and unavailable:
        raise PositionAdviceAccountSourceError(
            "short-put collateral basis is incomplete"
        )
    secured = _decimal(
        option_positions_context.get("cash_secured_total_cny"),
        "cash_secured_total_cny",
    )
    uncommitted = available - secured
    if uncommitted < 0:
        raise PositionAdviceAccountSourceError(
            "uncommitted cash headroom is negative"
        )
    return {
        "status": "available",
        "cash_capacity_semantics": "cash_headroom.v2",
        "cash_scope_semantics_version": CASH_SCOPE_SEMANTICS_VERSION,
        "cash_available_by_currency": native,
        "cash_available_base_cny": _decimal_text(available),
        "existing_short_put_collateral_base_cny": _decimal_text(secured),
        "uncommitted_cash_headroom_base_cny": _decimal_text(uncommitted),
        "complete": True,
    }


def build_share_coverage(
    *,
    portfolio_context: Mapping[str, Any],
    option_positions_context: Mapping[str, Any],
) -> dict[str, Any]:
    if option_positions_context.get("decision_snapshot_status") != "trusted":
        raise PositionAdviceAccountSourceError(
            "option-position decision snapshot is not trusted"
        )
    stocks = portfolio_context.get("stocks_by_symbol")
    locked = option_positions_context.get("locked_shares_by_symbol")
    unavailable = option_positions_context.get(
        "locked_shares_unavailable_by_symbol"
    )
    if not isinstance(stocks, Mapping) or not isinstance(locked, Mapping):
        raise PositionAdviceAccountSourceError("share coverage facts are unavailable")
    unavailable_map = dict(unavailable) if isinstance(unavailable, Mapping) else {}
    symbols = sorted(
        {
            str(item or "").strip().upper()
            for item in [*stocks.keys(), *locked.keys()]
            if str(item or "").strip()
        }
    )
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        stock = stocks.get(symbol)
        stock = dict(stock) if isinstance(stock, Mapping) else {}
        eligible = _nonnegative_integer(stock.get("shares", 0), f"{symbol}.shares")
        locked_shares = _nonnegative_integer(
            locked.get(symbol, 0),
            f"{symbol}.locked_shares",
        )
        available = eligible - locked_shares
        reason = str(unavailable_map.get(symbol) or "").strip() or None
        complete = reason is None and available >= 0
        rows.append(
            {
                "symbol": symbol,
                "eligible_underlying_shares": eligible,
                "locked_by_open_covered_calls": locked_shares,
                "uncommitted_covered_shares": available if complete else None,
                "avg_cost": stock.get("avg_cost"),
                "currency": (
                    str(stock.get("currency") or "").strip().upper() or None
                ),
                "complete": complete,
                "reason": reason or (
                    "locked_shares_exceed_underlying"
                    if available < 0
                    else None
                ),
            }
        )
    by_symbol = {
        str(item["symbol"]): dict(item)
        for item in rows
    }
    return {
        "share_coverage_semantics": "covered_shares_headroom.v2",
        "symbols": rows,
        "by_symbol": by_symbol,
        "complete": all(bool(item["complete"]) for item in rows),
    }


def _validate_candidate_capture(
    capture: Mapping[str, Any],
    *,
    account_run_id: str,
    account: str,
) -> None:
    if capture.get("schema_version") != (
        "position_advice_candidate_all_decisions_capture.v1"
    ):
        raise PositionAdviceAccountSourceError(
            "candidate capture schema is invalid"
        )
    if capture.get("complete") is not True:
        raise PositionAdviceAccountSourceError(
            "candidate capture is incomplete"
        )
    if capture.get("account_run_id") != account_run_id:
        raise PositionAdviceAccountSourceError(
            "candidate capture run mismatch"
        )
    if str(capture.get("account") or "").strip().lower() != account:
        raise PositionAdviceAccountSourceError(
            "candidate capture account mismatch"
        )


def _validate_option_context_fingerprint(
    option_context: Mapping[str, Any],
    *,
    decision_snapshot: Mapping[str, Any],
) -> None:
    expected = str(decision_snapshot.get("decision_state_fingerprint") or "")
    observed = str(option_context.get("decision_state_fingerprint") or "")
    if not expected or observed != expected:
        raise PositionAdviceAccountSourceError(
            "option-position context fingerprint mismatch"
        )


def _quote_dependencies(
    *,
    capture: Mapping[str, Any],
    required_data_root: Path,
    now: datetime | str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw = capture.get("quote_receipt_relpaths")
    if not isinstance(raw, Mapping) or not raw:
        raise PositionAdviceAccountSourceError(
            "candidate capture has no quote receipts"
        )
    records: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    seen_snapshots: set[str] = set()
    for _symbol, raw_relpath in sorted(raw.items()):
        relpath = str(raw_relpath or "").strip()
        if not relpath:
            raise PositionAdviceAccountSourceError(
                "candidate quote receipt path is missing"
            )
        receipt_path = required_data_root / relpath
        try:
            dependency = source_dependency_from_receipt(
                receipt_path=receipt_path,
                producer_root=required_data_root,
                now=now,
                expected_source_kind="quotes",
            )
        except PositionAdviceSourceError as exc:
            raise PositionAdviceAccountSourceError(str(exc)) from exc
        snapshot_id = str(dependency["snapshot_id"])
        if snapshot_id in seen_snapshots:
            continue
        seen_snapshots.add(snapshot_id)
        receipt = _read_json_object(receipt_path, "quote receipt")
        records.append(
            _receipt_record(
                source_kind="quotes",
                producer_root=required_data_root,
                receipt_path=receipt_path,
                receipt=receipt,
            )
        )
        dependencies.append(dependency)
    return records, dependencies


def _receipt_record(
    *,
    source_kind: str,
    producer_root: Path,
    receipt_path: Path,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "source_kind": str(source_kind),
        "producer_root": Path(producer_root).resolve(),
        "receipt_path": Path(receipt_path).resolve(),
        "receipt": dict(receipt),
    }


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    candidate = Path(path)
    if candidate.is_symlink():
        raise PositionAdviceAccountSourceError(f"{label} may not be a symlink")
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PositionAdviceAccountSourceError(f"{label} is unavailable") from exc
    if not isinstance(payload, dict):
        raise PositionAdviceAccountSourceError(f"{label} must be an object")
    return payload


def _existing_directory(path: Path, field: str) -> Path:
    candidate = Path(path).resolve()
    if not candidate.is_dir() or candidate.is_symlink():
        raise PositionAdviceAccountSourceError(f"{field} is invalid")
    return candidate


def _identity_values(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        raise PositionAdviceAccountSourceError(
            "portfolio account identity is unavailable"
        )
    identifiers = sorted(
        {
            str(item or "").strip().lower()
            for item in value
            if str(item or "").strip()
        }
    )
    if not identifiers:
        raise PositionAdviceAccountSourceError(
            "portfolio account identity is unavailable"
        )
    return identifiers


def _markets(values: Iterable[str]) -> list[str]:
    markets = sorted(
        {str(item or "").strip().upper() for item in values if str(item or "").strip()}
    )
    if not markets or any(item not in {"US", "HK"} for item in markets):
        raise PositionAdviceAccountSourceError("included markets are invalid")
    return markets


def _latest_observation(values: Iterable[Any]) -> str:
    parsed: list[datetime] = []
    for value in values:
        text = _required_text(value, "source_observed_at")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            timestamp = datetime.fromisoformat(text)
        except ValueError as exc:
            raise PositionAdviceAccountSourceError(
                "source observation is invalid"
            ) from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise PositionAdviceAccountSourceError(
                "source observation lacks timezone"
            )
        parsed.append(timestamp.astimezone(timezone.utc))
    if not parsed:
        raise PositionAdviceAccountSourceError("source observation is unavailable")
    return max(parsed).isoformat().replace("+00:00", "Z")


def _to_cny(
    amount: Decimal,
    *,
    currency: str,
    rates: Mapping[str, Any],
) -> Decimal:
    if currency == "CNY":
        return amount
    pair = f"{currency}CNY"
    if pair not in rates:
        raise PositionAdviceAccountSourceError(
            f"FX rate is unavailable for {currency}"
        )
    return amount * _decimal(rates[pair], pair, positive=True)


def _decimal(
    value: Any,
    field: str,
    *,
    positive: bool = False,
) -> Decimal:
    if isinstance(value, bool):
        raise PositionAdviceAccountSourceError(f"{field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PositionAdviceAccountSourceError(
            f"{field} must be numeric"
        ) from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise PositionAdviceAccountSourceError(f"{field} is invalid")
    return parsed


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise PositionAdviceAccountSourceError(
            f"{field} must be a nonnegative integer"
        )
    numeric = _decimal(value, field)
    parsed = int(numeric)
    if numeric != parsed or parsed < 0:
        raise PositionAdviceAccountSourceError(
            f"{field} must be a nonnegative integer"
        )
    return parsed


def _decimal_text(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PositionAdviceAccountSourceError(f"{field} is required")
    return text


__all__ = [
    "CASH_SCOPE_SEMANTICS_VERSION",
    "PositionAdviceAccountSourceError",
    "build_cash_capacity",
    "build_share_coverage",
    "publish_account_position_advice_sources",
    "publish_account_run_sources",
]
