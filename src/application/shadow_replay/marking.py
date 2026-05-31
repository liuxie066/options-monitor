from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from domain.domain.trade_contract_identity import contract_key

from src.application.shadow_replay.common import (
    MARK_PATH_SCHEMA_VERSION,
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
) -> dict[str, Any]:
    """Generate local mark path snapshots from required-data CSV quotes."""

    dataset_dir = dataset_dir_from_arg(dataset)
    base = Path(repo_root).expanduser().resolve() if repo_root is not None else dataset_dir
    required_root = resolve_path(required_data_root, base=base)
    candidate_snapshots = read_jsonl(dataset_dir / "candidate_snapshots.jsonl")
    existing_marks = [] if replace else read_jsonl(dataset_dir / "mark_path_snapshots.jsonl")
    aliases = _load_symbol_aliases(base)
    quote_index = _load_required_data_quote_index(required_root, aliases=aliases, base=base)
    mark_at = text(as_of) or utc_now()
    generated_all = [
        _mark_snapshot_from_required_data(candidate, quote_index=quote_index, aliases=aliases, mark_at=mark_at)
        for candidate in candidate_snapshots
    ]
    existing_identities = {_mark_identity(row) for row in existing_marks}
    existing_identities.discard("")
    generated = [row for row in generated_all if replace or _mark_identity(row) not in existing_identities]
    merged = generated if replace else existing_marks + generated
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
            "written": bool(write),
            "replace": bool(replace),
        },
        "generated_mark_snapshots": generated,
        "safety": safety_payload(writes_local_dataset=bool(write)),
    }
    if write:
        write_jsonl(dataset_dir / "mark_path_snapshots.jsonl", merged)
    if output:
        write_json(resolve_output_path(output), result)
    return result


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
