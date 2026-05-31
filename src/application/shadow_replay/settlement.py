from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from src.application.shadow_replay.common import (
    OUTCOME_FACT_SCHEMA_VERSION,
    dataset_dir_from_arg,
    first_float,
    instrument_key,
    parse_date,
    read_jsonl,
    resolve_output_path,
    safety_payload,
    text,
    utc_now,
    write_json,
    write_jsonl,
)


def settle_shadow_replay_dataset(
    *,
    dataset: str | Path,
    output: str | Path | None = None,
    write: bool = False,
    replace: bool = False,
) -> dict[str, Any]:
    """Derive local outcome facts from candidate snapshots and mark paths."""

    dataset_dir = dataset_dir_from_arg(dataset)
    candidate_snapshots = read_jsonl(dataset_dir / "candidate_snapshots.jsonl")
    mark_snapshots = read_jsonl(dataset_dir / "mark_path_snapshots.jsonl")
    existing_outcomes = [] if replace else read_jsonl(dataset_dir / "outcome_facts.jsonl")
    generated = derive_outcome_facts(candidate_snapshots, mark_snapshots, existing_outcomes=existing_outcomes)
    merged = generated if replace else existing_outcomes + generated
    result = {
        "schema_version": "shadow_replay_settlement.v1",
        "dataset_dir": str(dataset_dir),
        "generated_at_utc": utc_now(),
        "summary": {
            "candidate_snapshot_count": len(candidate_snapshots),
            "mark_path_snapshot_count": len(mark_snapshots),
            "existing_outcome_fact_count": 0 if replace else len(existing_outcomes),
            "generated_outcome_fact_count": len(generated),
            "written": bool(write),
            "replace": bool(replace),
        },
        "generated_outcome_facts": generated,
        "safety": safety_payload(writes_local_dataset=bool(write)),
    }
    if write:
        write_jsonl(dataset_dir / "outcome_facts.jsonl", merged)
    if output:
        write_json(resolve_output_path(output), result)
    return result


def derive_outcome_facts(
    candidate_snapshots: list[dict[str, Any]],
    mark_snapshots: list[dict[str, Any]],
    *,
    existing_outcomes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    marks_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mark in mark_snapshots:
        if not is_usable_mark(mark):
            continue
        key = instrument_key(mark)
        if key:
            marks_by_key[key].append(mark)
    existing_keys = {instrument_key(row) for row in existing_outcomes}
    existing_keys.discard("")
    out: list[dict[str, Any]] = []
    for candidate in candidate_snapshots:
        key = instrument_key(candidate)
        if not key or key in existing_keys:
            continue
        marks = marks_by_key.get(key) or []
        if not marks:
            continue
        final_mark = _latest_mark(marks)
        pnl_values = [_mark_pnl_value(row) for row in marks]
        pnl_values = [value for value in pnl_values if value is not None]
        realized_pnl, model, quality, outcome = derive_outcome_result(candidate, final_mark)
        if realized_pnl is None:
            continue
        out.append(
            {
                "schema_version": OUTCOME_FACT_SCHEMA_VERSION,
                "source": "derived_from_mark_path",
                "instrument_key": key,
                "account": candidate.get("account"),
                "symbol": candidate.get("symbol"),
                "contract_symbol": candidate.get("contract_symbol"),
                "option_type": candidate.get("option_type") or candidate.get("mode"),
                "expiration": candidate.get("expiration"),
                "strike": candidate.get("strike"),
                "candidate_status": candidate.get("status"),
                "outcome": outcome,
                "realized_pnl": realized_pnl,
                "pnl_model": model,
                "quality": quality,
                "mark_count": len(marks),
                "first_mark_at": mark_time(_earliest_mark(marks)),
                "final_mark_at": mark_time(final_mark),
                "max_adverse_pnl": min(pnl_values) if pnl_values else None,
                "max_favorable_pnl": max(pnl_values) if pnl_values else None,
                "writes_runtime_config": False,
                "writes_trade_state": False,
            }
        )
    return out


def derive_outcome_result(candidate: dict[str, Any], final_mark: dict[str, Any]) -> tuple[float | None, str, str, str]:
    expiration_pnl, expiration_model, expiration_quality, expiration_outcome = _derive_expiration_pnl(candidate, final_mark)
    if expiration_pnl is not None:
        return expiration_pnl, expiration_model, expiration_quality, expiration_outcome
    mark_pnl, mark_model, mark_quality = _derive_realized_pnl(candidate, final_mark)
    return mark_pnl, mark_model, mark_quality, "counterfactual_mark_to_market"


def is_usable_mark(row: dict[str, Any]) -> bool:
    if text(row.get("quote_status")).lower() == "missing_quote":
        return False
    quality = text(row.get("mark_quality")).lower()
    if quality == "missing_quote":
        return False
    if quality == "missing_mid" and not (is_expiration_mark(row, row) and expiration_intrinsic_value(row, row) is not None):
        return False
    if _mark_pnl_value(row) is not None:
        return True
    if first_float(row, "option_mid", "mid", "mark", "option_price", "close_price", "last_price") is not None:
        return True
    if is_expiration_mark(row, row) and expiration_intrinsic_value(row, row) is not None:
        return True
    bid = first_float(row, "bid")
    ask = first_float(row, "ask")
    return bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid


def expiration_intrinsic_value(candidate: dict[str, Any], final_mark: dict[str, Any]) -> float | None:
    option_type = text(candidate.get("option_type") or candidate.get("mode") or final_mark.get("option_type") or final_mark.get("mode")).lower()
    strike = first_float(candidate, "strike") or first_float(final_mark, "strike")
    spot = first_float(final_mark, "spot", "underlying_price")
    if option_type not in {"put", "call"} or strike is None or spot is None:
        return None
    if option_type == "put":
        return max(strike - spot, 0.0)
    return max(spot - strike, 0.0)


def mark_time(row: dict[str, Any]) -> str | None:
    return text(row.get("mark_at") or row.get("as_of") or row.get("timestamp") or row.get("time") or row.get("date")) or None


def _derive_expiration_pnl(candidate: dict[str, Any], final_mark: dict[str, Any]) -> tuple[float | None, str, str, str]:
    if not is_expiration_mark(candidate, final_mark):
        return None, "unavailable", "not_expiration_mark", "counterfactual_mark_to_market"
    entry_credit = first_float(candidate, "net_income", "net_credit", "entry_credit", "premium")
    intrinsic = expiration_intrinsic_value(candidate, final_mark)
    if entry_credit is None or intrinsic is None:
        return None, "unavailable", "missing_entry_credit_or_expiration_intrinsic", "expiration_unavailable"
    contracts = first_float(candidate, "contracts", "contract_count") or 1.0
    multiplier = first_float(candidate, "multiplier") or first_float(final_mark, "multiplier") or 100.0
    side = text(candidate.get("side") or candidate.get("position_side")).lower() or "short"
    intrinsic_value = intrinsic * multiplier * contracts
    if side == "long":
        return intrinsic_value - entry_credit, "long_option_expiration_intrinsic_minus_entry_cost", "derived_from_expiration_spot", _expiration_outcome(candidate, intrinsic=intrinsic, side=side)
    return entry_credit - intrinsic_value, "short_option_entry_credit_minus_expiration_intrinsic", "derived_from_expiration_spot", _expiration_outcome(candidate, intrinsic=intrinsic, side=side)


def is_expiration_mark(candidate: dict[str, Any], final_mark: dict[str, Any]) -> bool:
    dte = first_float(final_mark, "dte")
    if dte is not None and dte <= 0:
        return True
    expiration = parse_date(text(candidate.get("expiration") or candidate.get("exp") or final_mark.get("expiration") or final_mark.get("exp")))
    mark_date = parse_date(mark_time(final_mark) or "")
    return bool(expiration and mark_date and mark_date >= expiration)


def _expiration_outcome(candidate: dict[str, Any], *, intrinsic: float, side: str) -> str:
    if intrinsic <= 0:
        return "expired_worthless"
    option_type = text(candidate.get("option_type") or candidate.get("mode")).lower()
    if side == "short" and option_type == "put":
        return "assigned_at_expiry"
    if side == "short" and option_type == "call":
        return "called_away_at_expiry"
    return "expired_in_the_money"


def _derive_realized_pnl(candidate: dict[str, Any], final_mark: dict[str, Any]) -> tuple[float | None, str, str]:
    mark_pnl = _mark_pnl_value(final_mark)
    if mark_pnl is not None:
        return mark_pnl, "mark_pnl", "derived_from_mark_pnl"
    entry_credit = first_float(candidate, "net_income", "net_credit", "entry_credit", "premium")
    exit_price = first_float(final_mark, "option_mid", "mid", "mark", "option_price", "close_price")
    if entry_credit is None or exit_price is None:
        return None, "unavailable", "missing_entry_credit_or_exit_price"
    contracts = first_float(candidate, "contracts", "contract_count") or 1.0
    multiplier = first_float(candidate, "multiplier") or first_float(final_mark, "multiplier") or 100.0
    side = text(candidate.get("side") or candidate.get("position_side")).lower() or "short"
    exit_value = exit_price * multiplier * contracts
    if side == "long":
        return exit_value - entry_credit, "long_option_exit_value_minus_entry_cost", "derived_from_entry_and_exit_price"
    return entry_credit - exit_value, "short_option_entry_credit_minus_exit_value", "derived_from_entry_and_exit_price"


def _mark_pnl_value(row: dict[str, Any]) -> float | None:
    return first_float(row, "unrealized_pnl", "counterfactual_pnl", "realized_pnl", "pnl", "mark_pnl")


def _latest_mark(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(rows, key=_mark_sort_key)


def _earliest_mark(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return min(rows, key=_mark_sort_key)


def _mark_sort_key(row: dict[str, Any]) -> str:
    return mark_time(row) or ""
