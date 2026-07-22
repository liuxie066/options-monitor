from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domain.domain.close_advice import FEE_USABLE_STATUSES
from domain.domain.fee_calc import calc_futu_option_fee
from domain.domain.trade_contract_identity import contract_key

from src.application.shadow_replay.common import (
    CLOSE_DECISION_MARK_SCHEMA_VERSION,
    MARK_PATH_SCHEMA_VERSION,
    OPTIONAL_CLOSE_DATASET_FILES,
    abs_first_float,
    dataset_dir_from_arg,
    first_float,
    instrument_key,
    read_csv_rows,
    read_jsonl,
    resolve_output_path,
    resolve_path,
    safe_rel,
    safety_payload,
    text,
    utc_now,
    write_json,
    write_jsonl,
)
from src.application.shadow_replay.settlement import (
    derive_outcome_result,
    expiration_intrinsic_value,
    is_expiration_mark,
    is_usable_mark,
    mark_time,
)
from src.application.symbol_aliases import load_runtime_symbol_aliases


def mark_shadow_replay_dataset(
    *,
    dataset: str | Path,
    required_data_root: str | Path,
    as_of: str | None = None,
    repo_root: str | Path | None = None,
    output: str | Path | None = None,
    write: bool = False,
    replace: bool = False,
    mark_time_basis: str | None = None,
    quote_collection_source: str | None = None,
) -> dict[str, Any]:
    """Generate local mark path snapshots from required-data CSV quotes."""

    dataset_dir = dataset_dir_from_arg(dataset)
    base = Path(repo_root).expanduser().resolve() if repo_root is not None else dataset_dir
    required_root = resolve_path(required_data_root, base=base)
    candidate_snapshots = read_jsonl(dataset_dir / "candidate_snapshots.jsonl")
    existing_marks = [] if replace else read_jsonl(dataset_dir / "mark_path_snapshots.jsonl")
    close_episode_path = dataset_dir / OPTIONAL_CLOSE_DATASET_FILES[0]
    close_facet_exists = close_episode_path.is_file()
    close_episodes = read_jsonl(close_episode_path) if close_facet_exists else []
    existing_close_marks = (
        []
        if replace
        else read_jsonl(dataset_dir / OPTIONAL_CLOSE_DATASET_FILES[1])
    )
    aliases = _load_symbol_aliases(base)
    quote_index = _load_required_data_quote_index(required_root, aliases=aliases, base=base)
    mark_at = text(as_of) or utc_now()
    resolved_mark_time_basis = text(mark_time_basis).lower() or (
        "operator_asserted_as_of" if text(as_of) else "collection_time"
    )
    if resolved_mark_time_basis not in {"collection_time", "operator_asserted_as_of"}:
        raise ValueError("mark_time_basis must be collection_time or operator_asserted_as_of")
    resolved_quote_source = text(quote_collection_source).lower() or "external_required_data"
    generated_all = [
        _mark_snapshot_from_required_data(candidate, quote_index=quote_index, aliases=aliases, mark_at=mark_at)
        for candidate in candidate_snapshots
    ]
    existing_identities = {_mark_identity(row) for row in existing_marks}
    existing_identities.discard("")
    generated = [row for row in generated_all if replace or _mark_identity(row) not in existing_identities]
    merged = generated if replace else existing_marks + generated
    generated_close_all = [
        mark
        for episode in close_episodes
        if (
            mark := _close_mark_snapshot_from_required_data(
                episode,
                quote_index=quote_index,
                aliases=aliases,
                mark_at=mark_at,
                mark_time_basis=resolved_mark_time_basis,
                quote_collection_source=resolved_quote_source,
            )
        )
        is not None
    ]
    existing_close_identities = {_close_mark_identity(row) for row in existing_close_marks}
    existing_close_identities.discard("")
    generated_close = [
        row
        for row in generated_close_all
        if replace or _close_mark_identity(row) not in existing_close_identities
    ]
    merged_close = generated_close if replace else existing_close_marks + generated_close
    usable_count = sum(1 for row in generated if is_usable_mark(row))
    missing_count = sum(1 for row in generated if str(row.get("quote_status") or "") == "missing_quote")
    result = {
        "schema_version": "shadow_replay_marking.v1",
        "dataset_dir": str(dataset_dir),
        "required_data_root": str(required_root),
        "generated_at_utc": utc_now(),
        "summary": {
            "candidate_snapshot_count": len(candidate_snapshots),
            "required_data_quote_count": quote_index["quote_count"],
            "existing_mark_snapshot_count": 0 if replace else len(existing_marks),
            "generated_mark_snapshot_count": len(generated),
            "usable_mark_snapshot_count": usable_count,
            "missing_quote_count": missing_count,
            "matched_quote_count": len(generated) - missing_count,
            "as_of": mark_at,
            "mark_time_basis": resolved_mark_time_basis,
            "quote_collection_source": resolved_quote_source,
            "written": bool(write),
            "replace": bool(replace),
            "close_decision_episode_count": len(close_episodes),
            "generated_close_mark_count": len(generated_close),
            "usable_close_mark_count": sum(
                1 for row in generated_close if is_usable_close_mark(row)
            ),
            "close_mark_outside_window_count": len(close_episodes) - len(generated_close_all),
        },
        "generated_mark_snapshots": generated,
        "generated_close_marks": generated_close,
        "safety": safety_payload(writes_local_dataset=bool(write)),
    }
    if write:
        write_jsonl(dataset_dir / "mark_path_snapshots.jsonl", merged)
        if close_facet_exists:
            write_jsonl(dataset_dir / OPTIONAL_CLOSE_DATASET_FILES[1], merged_close)
    if output:
        write_json(resolve_output_path(output), result)
    return result


_CLOSE_HORIZON_WINDOWS = (
    ("1d", 1, 2),
    ("3d", 3, 4),
    ("7d", 7, 9),
    ("14d", 14, 17),
)


def _close_mark_snapshot_from_required_data(
    episode: dict[str, Any],
    *,
    quote_index: dict[str, Any],
    aliases: Mapping[str, Any] | None,
    mark_at: str,
    mark_time_basis: str,
    quote_collection_source: str,
) -> dict[str, Any] | None:
    observed_at = _strict_utc_datetime(
        text(episode.get("observed_at_utc")),
        label="close episode observed_at_utc",
    )
    marked_at = _strict_utc_datetime(mark_at, label="close mark as_of")
    if marked_at <= observed_at:
        return None
    identity = episode.get("position_identity")
    identity = identity if isinstance(identity, dict) else {}
    horizon = _close_mark_horizon(
        observed_at=observed_at,
        marked_at=marked_at,
        expiration=text(identity.get("expiration")),
    )
    if horizon is None:
        return None
    quote, matched_by = _match_required_data_quote(
        identity,
        quote_index=quote_index,
        aliases=aliases,
    )
    base = {
        "schema_version": CLOSE_DECISION_MARK_SCHEMA_VERSION,
        "episode_id": episode.get("episode_id"),
        "horizon": horizon,
        "marked_at_utc": marked_at.isoformat().replace("+00:00", "Z"),
        "observed_at_utc": episode.get("observed_at_utc"),
        "account": episode.get("account"),
        "position_lot_id": episode.get("position_lot_id"),
        "symbol": identity.get("symbol"),
        "contract_symbol": identity.get("contract_symbol"),
        "option_type": identity.get("option_type"),
        "expiration": identity.get("expiration"),
        "strike": identity.get("strike"),
        "source_kind": "required_data_csv",
        "mark_time_basis": mark_time_basis,
        "quote_collection_source": quote_collection_source,
        "point_in_time_status": _point_in_time_status(
            mark_time_basis=mark_time_basis,
            quote_collection_source=quote_collection_source,
        ),
        "writes_runtime_config": False,
        "writes_trade_state": False,
    }
    replacement_payload = _replacement_mark_payload(
        episode,
        quote_index=quote_index,
        aliases=aliases,
    )
    if not quote:
        return {
            **base,
            **replacement_payload,
            "quote_status": "missing_quote",
            "mark_quality": "missing_quote",
            "matched_by": None,
            "future_close_fee": None,
            "future_fee_status": "not_evaluable",
            "inconclusive_reason": "required_data_quote_missing",
        }
    mid, mid_flags = _quote_mid(quote)
    ask = first_float(quote, "ask")
    bid = first_float(quote, "bid")
    spot = first_float(quote, "spot", "underlying_price")
    future_fee, future_fee_status = _close_future_fee(episode, ask=ask)
    return {
        **base,
        **replacement_payload,
        "quote_status": "matched",
        "mark_quality": "usable" if mid is not None else "missing_mid",
        "matched_by": matched_by,
        "required_data_source_path": quote.get("_source_path"),
        "required_data_source_row_number": quote.get("_source_row_number"),
        "bid": bid,
        "ask": ask,
        "option_mid": mid,
        "spot": spot,
        "dte": first_float(quote, "dte"),
        "multiplier": first_float(quote, "multiplier"),
        "currency": text(quote.get("currency")) or None,
        "quote_flags": mid_flags,
        "future_close_fee": future_fee,
        "future_fee_status": future_fee_status,
    }


def _replacement_mark_payload(
    episode: dict[str, Any],
    *,
    quote_index: dict[str, Any],
    aliases: Mapping[str, Any] | None,
) -> dict[str, Any]:
    evidence = episode.get("replacement_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    contract_symbol = text(evidence.get("contract_symbol")).upper()
    if not contract_symbol:
        return {
            "replacement_quote_status": "not_applicable",
            "replacement_inconclusive_reason": "replacement_identity_missing",
        }
    current_identity = episode.get("position_identity")
    current_identity = current_identity if isinstance(current_identity, dict) else {}
    identity = {
        "symbol": evidence.get("symbol"),
        "contract_symbol": contract_symbol,
        "option_type": evidence.get("option_type") or current_identity.get("option_type"),
        "expiration": evidence.get("expiration"),
        "strike": evidence.get("strike"),
    }
    quote, matched_by = _match_required_data_quote(
        identity,
        quote_index=quote_index,
        aliases=aliases,
    )
    if not quote:
        return {
            "replacement_quote_status": "missing_quote",
            "replacement_matched_by": None,
            "replacement_inconclusive_reason": "replacement_quote_missing",
        }
    mid, flags = _quote_mid(quote)
    ask = first_float(quote, "ask")
    fee, fee_status = _replacement_future_fee(evidence, ask=ask)
    return {
        "replacement_quote_status": "matched",
        "replacement_matched_by": matched_by,
        "replacement_bid": first_float(quote, "bid"),
        "replacement_ask": ask,
        "replacement_option_mid": mid,
        "replacement_spot": first_float(quote, "spot", "underlying_price"),
        "replacement_quote_flags": flags,
        "replacement_future_close_fee": fee,
        "replacement_future_fee_status": fee_status,
        "replacement_required_data_source_path": quote.get("_source_path"),
        "replacement_required_data_source_row_number": quote.get("_source_row_number"),
        "replacement_inconclusive_reason": None if ask is not None else "replacement_ask_missing",
    }


def _close_mark_horizon(
    *,
    observed_at: datetime,
    marked_at: datetime,
    expiration: str,
) -> str | None:
    expiration_date = _date_or_none(expiration)
    if expiration_date is not None and marked_at.date() == expiration_date:
        return "expiry"
    if expiration_date is not None and marked_at.date() > expiration_date:
        return None
    elapsed_days = (marked_at.date() - observed_at.date()).days
    for horizon, lower, upper in _CLOSE_HORIZON_WINDOWS:
        if lower <= elapsed_days <= upper:
            return horizon
    return None


def _point_in_time_status(*, mark_time_basis: str, quote_collection_source: str) -> str:
    if mark_time_basis != "collection_time":
        return "unverified_operator_as_of"
    if quote_collection_source == "opend":
        return "verified_fresh_collection"
    return "unverified_required_data_time"


def _close_future_fee(episode: dict[str, Any], *, ask: float | None) -> tuple[float | None, str]:
    economics = episode.get("decision_economics")
    economics = economics if isinstance(economics, dict) else {}
    fee_status = text(economics.get("fee_calc_status")).lower()
    currency = text(economics.get("currency")).upper()
    contracts = first_float(economics, "contracts")
    multiplier = first_float(economics, "multiplier")
    if (
        fee_status not in FEE_USABLE_STATUSES
        or ask is None
        or ask <= 0
        or contracts is None
        or contracts <= 0
        or multiplier is None
        or multiplier <= 0
    ):
        return None, "not_evaluable"
    try:
        fee = calc_futu_option_fee(
            currency,
            ask,
            contracts=int(contracts),
            multiplier=int(multiplier),
            is_sell=False,
        )
    except ValueError:
        return None, "not_evaluable"
    return fee, "estimated_from_decision_fee_basis"


def _replacement_future_fee(
    evidence: dict[str, Any],
    *,
    ask: float | None,
) -> tuple[float | None, str]:
    contracts = first_float(evidence, "contracts")
    multiplier = first_float(evidence, "multiplier")
    currency = text(evidence.get("currency")).upper()
    if (
        text(evidence.get("fee_calc_status")).lower() != "candidate_futu_fee"
        or ask is None
        or ask <= 0
        or contracts is None
        or contracts <= 0
        or multiplier is None
        or multiplier <= 0
    ):
        return None, "not_evaluable"
    try:
        fee = calc_futu_option_fee(
            currency,
            ask,
            contracts=int(contracts),
            multiplier=int(multiplier),
            is_sell=False,
        )
    except ValueError:
        return None, "not_evaluable"
    return fee, "estimated_from_replacement_candidate_fee"


def is_usable_close_mark(row: dict[str, Any]) -> bool:
    if text(row.get("point_in_time_status")).lower() != "verified_fresh_collection":
        return False
    if text(row.get("quote_status")).lower() != "matched":
        return False
    if text(row.get("horizon")).lower() == "expiry":
        return first_float(row, "spot") is not None or first_float(row, "ask") is not None
    return first_float(row, "ask") is not None


def _close_mark_identity(row: dict[str, Any]) -> str:
    episode_id = text(row.get("episode_id"))
    horizon = text(row.get("horizon"))
    marked_at = text(row.get("marked_at_utc"))
    return "|".join((episode_id, horizon, marked_at)) if all((episode_id, horizon, marked_at)) else ""


def _strict_utc_datetime(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid timestamp for {label}: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timezone required for {label}: {value}")
    return parsed.astimezone(timezone.utc)


def _date_or_none(value: str) -> Any:
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _load_symbol_aliases(base: Path) -> Mapping[str, Any] | None:
    try:
        return load_runtime_symbol_aliases(base)
    except Exception:
        return None


def _load_required_data_quote_index(required_data_root: Path, *, aliases: Mapping[str, Any] | None, base: Path) -> dict[str, Any]:
    parsed = required_data_root / "parsed"
    source_dir = parsed if parsed.exists() and parsed.is_dir() else required_data_root
    by_contract: dict[str, dict[str, Any]] = {}
    by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    quote_count = 0
    for path in sorted(source_dir.glob("*_required_data.csv")):
        symbol_from_name = path.name.removesuffix("_required_data.csv").upper()
        for row_number, row in enumerate(read_csv_rows(path), start=1):
            quote_count += 1
            item = dict(row)
            item["_source_path"] = safe_rel(path, base=base)
            item["_source_row_number"] = row_number
            contract = text(item.get("contract_symbol") or item.get("option_symbol")).upper()
            if contract:
                by_contract.setdefault(contract, item)
            key = _quote_key_for_row(
                item,
                symbol_fallback=symbol_from_name,
                aliases=aliases,
            )
            if all(key):
                by_key.setdefault(key, item)
    return {"by_contract": by_contract, "by_key": by_key, "quote_count": quote_count}


def _quote_key_for_row(row: dict[str, Any], *, symbol_fallback: str | None, aliases: Mapping[str, Any] | None) -> tuple[str, str, str, str]:
    return contract_key(
        row.get("symbol") or row.get("underlying_symbol") or symbol_fallback,
        row.get("option_type") or row.get("mode"),
        row.get("expiration") or row.get("exp"),
        row.get("strike"),
        symbol_aliases=aliases,
        option_type_fallback_raw=True,
        expiration_fallback_raw=True,
    )


def _candidate_quote_key(row: dict[str, Any], *, aliases: Mapping[str, Any] | None) -> tuple[str, str, str, str]:
    return _quote_key_for_row(row, symbol_fallback=None, aliases=aliases)


def _mark_snapshot_from_required_data(
    candidate: dict[str, Any],
    *,
    quote_index: dict[str, Any],
    aliases: Mapping[str, Any] | None,
    mark_at: str,
) -> dict[str, Any]:
    quote, matched_by = _match_required_data_quote(candidate, quote_index=quote_index, aliases=aliases)
    base = {
        "schema_version": MARK_PATH_SCHEMA_VERSION,
        "source_kind": "required_data_csv",
        "mark_at": mark_at,
        "candidate_status": candidate.get("status"),
        "account": candidate.get("account"),
        "symbol": candidate.get("symbol"),
        "contract_symbol": candidate.get("contract_symbol"),
        "option_type": candidate.get("option_type") or candidate.get("mode"),
        "expiration": candidate.get("expiration"),
        "strike": candidate.get("strike"),
        "instrument_key": instrument_key(candidate),
        "writes_runtime_config": False,
        "writes_trade_state": False,
    }
    if not quote:
        return {
            **base,
            "quote_status": "missing_quote",
            "mark_quality": "missing_quote",
            "matched_by": None,
            "reason": "required_data_quote_missing",
        }
    mid, mid_flags = _quote_mid(quote)
    payload = {
        **base,
        "quote_status": "matched",
        "matched_by": matched_by,
        "required_data_source_path": quote.get("_source_path"),
        "required_data_source_row_number": quote.get("_source_row_number"),
        "bid": first_float(quote, "bid"),
        "ask": first_float(quote, "ask"),
        "mid": first_float(quote, "mid"),
        "last_price": first_float(quote, "last_price", "last"),
        "option_mid": mid,
        "delta": first_float(quote, "delta", "put_delta", "call_delta"),
        "abs_delta": abs_first_float(quote, "delta", "put_delta", "call_delta"),
        "implied_volatility": first_float(quote, "implied_volatility", "iv"),
        "dte": first_float(quote, "dte"),
        "spot": first_float(quote, "spot", "underlying_price"),
        "spread_ratio": first_float(quote, "spread_ratio", "combo_spread_ratio"),
        "open_interest": first_float(quote, "open_interest"),
        "volume": first_float(quote, "volume"),
        "multiplier": first_float(quote, "multiplier"),
        "currency": text(quote.get("currency")) or None,
        "quote_flags": mid_flags,
    }
    expiration_intrinsic = expiration_intrinsic_value(candidate, payload)
    can_settle_expiration = is_expiration_mark(candidate, payload) and expiration_intrinsic is not None
    payload["mark_quality"] = "usable" if mid is not None else ("expiration_spot" if can_settle_expiration else "missing_mid")
    if can_settle_expiration:
        payload["expiration_intrinsic_value"] = expiration_intrinsic
    pnl, model, quality, outcome = derive_outcome_result(candidate, payload)
    if pnl is not None:
        payload["counterfactual_pnl"] = pnl
        payload["pnl_model"] = model
        payload["pnl_quality"] = quality
        payload["pnl_outcome"] = outcome
    elif mid is not None:
        payload["pnl_model"] = model
        payload["pnl_quality"] = quality
    return payload


def _match_required_data_quote(
    candidate: dict[str, Any],
    *,
    quote_index: dict[str, Any],
    aliases: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    contract = text(candidate.get("contract_symbol") or candidate.get("option_symbol")).upper()
    by_contract = quote_index.get("by_contract") if isinstance(quote_index, dict) else {}
    if contract and isinstance(by_contract, dict):
        quote = by_contract.get(contract)
        if isinstance(quote, dict):
            return quote, "contract_symbol"
    key = _candidate_quote_key(candidate, aliases=aliases)
    by_key = quote_index.get("by_key") if isinstance(quote_index, dict) else {}
    if all(key) and isinstance(by_key, dict):
        quote = by_key.get(key)
        if isinstance(quote, dict):
            return quote, "contract_key"
    return None, None


def _quote_mid(quote: dict[str, Any]) -> tuple[float | None, list[str]]:
    bid = first_float(quote, "bid")
    ask = first_float(quote, "ask")
    has_usable_bid_ask = bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid
    mid = first_float(quote, "mid", "option_mid", "mark", "option_price")
    if mid is not None and mid > 0:
        last_price = first_float(quote, "last_price", "last")
        if not has_usable_bid_ask and last_price is not None and abs(float(mid) - float(last_price)) < 0.000001:
            return mid, ["mid_fallback_last_price"]
        return mid, []
    if has_usable_bid_ask:
        assert bid is not None and ask is not None
        return round((bid + ask) / 2, 6), ["mid_from_bid_ask"]
    last_price = first_float(quote, "last_price", "last")
    if last_price is not None and last_price > 0:
        return last_price, ["mid_fallback_last_price"]
    return None, ["missing_mid"]


def _mark_identity(row: dict[str, Any]) -> str:
    key = instrument_key(row)
    if not key:
        return ""
    return "|".join(
        [
            key,
            mark_time(row) or "",
            text(row.get("required_data_source_path") or row.get("source_path")),
            text(row.get("required_data_source_row_number") or row.get("source_row_number")),
            text(row.get("quote_status")),
        ]
    )
