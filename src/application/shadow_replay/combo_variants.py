from __future__ import annotations

"""Shadow-only Combo Yield variant contracts, decisions, and outcomes."""

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

from domain.domain.engine import (
    ComboYieldResearchPolicy,
    combo_yield_proposed_gate_reasons,
    combo_yield_proposed_rank_key,
    rank_combo_yield_proposed_rows,
    rank_yield_enhancement_rows,
    select_best_combo_yield_proposed_pairs,
    select_best_yield_enhancement_per_symbol,
)
from domain.domain.insurance_underwriting import underwriting_rank_key
from src.application.shadow_replay.common import (
    dataset_dir_from_arg,
    dataset_read_lock,
    dataset_write_lock,
    first_float,
    read_jsonl,
    refresh_dataset_manifest,
    safety_payload,
    text,
    utc_now,
    validate_dataset_integrity,
    write_json,
    write_jsonl,
)


COMBO_VARIANT_SPEC_SCHEMA_VERSION = "shadow_combo_variant_spec.v1"
COMBO_CAPTURE_MANIFEST_SCHEMA_VERSION = "shadow_combo_capture_manifest.v1"
COMBO_PAIR_DECISION_SCHEMA_VERSION = "shadow_combo_pair_decision.v1"
COMBO_PAIR_MARK_SCHEMA_VERSION = "shadow_combo_pair_mark.v1"
COMBO_PAIR_OUTCOME_SCHEMA_VERSION = "shadow_combo_pair_outcome.v1"
COMBO_PAIR_DATASET_FILES = (
    "combo_pair_decisions.jsonl",
    "combo_pair_mark_paths.jsonl",
    "combo_pair_outcomes.jsonl",
)


def load_combo_variant_spec(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Combo variant spec does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"failed to parse Combo variant spec: {source}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Combo variant spec must be a JSON object")
    return normalize_combo_variant_spec(payload)


def normalize_combo_variant_spec(payload: dict[str, Any]) -> dict[str, Any]:
    schema = text(payload.get("schema_version")) or COMBO_VARIANT_SPEC_SCHEMA_VERSION
    if schema != COMBO_VARIANT_SPEC_SCHEMA_VERSION:
        raise ValueError(f"unsupported Combo variant spec schema_version: {schema}")
    max_calls = _positive_int(payload.get("max_estimated_option_chain_calls"))
    max_age = _positive_int(payload.get("max_entry_quote_age_seconds"))
    max_skew = _nonnegative_int(payload.get("max_entry_leg_skew_seconds"))
    if max_calls is None:
        raise ValueError("max_estimated_option_chain_calls must be a positive integer")
    if max_age is None:
        raise ValueError("max_entry_quote_age_seconds must be a positive integer")
    if max_skew is None:
        raise ValueError("max_entry_leg_skew_seconds must be a non-negative integer")
    raw_variants = payload.get("variants")
    if not isinstance(raw_variants, list) or not raw_variants:
        raise ValueError("variants must be a non-empty array")
    policies: list[ComboYieldResearchPolicy] = []
    seen: set[str] = set()
    for raw in raw_variants:
        if not isinstance(raw, dict):
            raise ValueError("each Combo variant must be an object")
        policy = combo_research_policy_from_dict(raw)
        if policy.variant_id in seen:
            raise ValueError(f"duplicate Combo variant_id: {policy.variant_id}")
        seen.add(policy.variant_id)
        policies.append(policy)
    return {
        "schema_version": COMBO_VARIANT_SPEC_SCHEMA_VERSION,
        "max_estimated_option_chain_calls": max_calls,
        "max_entry_quote_age_seconds": max_age,
        "max_entry_leg_skew_seconds": max_skew,
        "variants": [_policy_payload(policy) for policy in policies],
    }


def combo_research_policy_from_dict(raw: dict[str, Any]) -> ComboYieldResearchPolicy:
    mode = text(raw.get("structure_mode")).lower()
    return ComboYieldResearchPolicy(
        variant_id=text(raw.get("variant_id")),
        structure_mode=mode,
        min_net_credit_retention=float(raw.get("min_net_credit_retention")),
        max_call_cost_to_put_credit=(
            None
            if raw.get("max_call_cost_to_put_credit") in (None, "")
            else float(raw.get("max_call_cost_to_put_credit"))
        ),
        min_abs_call_delta=float(raw.get("min_abs_call_delta")),
        target_abs_call_delta=float(raw.get("target_abs_call_delta")),
        max_abs_call_delta=float(raw.get("max_abs_call_delta")),
        min_expiry_gap_days=(
            None if raw.get("min_expiry_gap_days") in (None, "") else int(raw.get("min_expiry_gap_days"))
        ),
        target_expiry_gap_days=(
            None if raw.get("target_expiry_gap_days") in (None, "") else int(raw.get("target_expiry_gap_days"))
        ),
        max_expiry_gap_days=(
            None if raw.get("max_expiry_gap_days") in (None, "") else int(raw.get("max_expiry_gap_days"))
        ),
    )


def combo_variant_spec_hash(spec: dict[str, Any]) -> str:
    return _canonical_hash(normalize_combo_variant_spec(spec))


def build_combo_pair_decisions(
    *,
    dataset_id: str,
    account: str,
    pair_rows: Iterable[dict[str, Any]],
    effective_combo_policy_hash: str,
    variant_spec: dict[str, Any],
    required_data_file_sha256: dict[str, str],
    entry_observed_at_utc: str,
    baseline_structure_mode: str | None = None,
) -> list[dict[str, Any]]:
    """Evaluate baseline and proposed selectors over one immutable pair universe."""

    normalized_spec = normalize_combo_variant_spec(variant_spec)
    policies = [
        combo_research_policy_from_dict(raw)
        for raw in normalized_spec["variants"]
    ]
    rows = [dict(row) for row in pair_rows]
    required_hashes = {
        str(path): str(digest)
        for path, digest in sorted(required_data_file_sha256.items())
        if str(path).strip() and str(digest).strip()
    }
    if not required_hashes:
        raise ValueError("required_data_file_sha256 must not be empty")
    baseline_selected = _baseline_selected_keys(
        rows,
        structure_mode=baseline_structure_mode,
    )
    baseline_rank = _baseline_rank_map(
        rows,
        structure_mode=baseline_structure_mode,
    )
    proposed_selected = {
        policy.variant_id: {
            _pair_key(row)
            for row in select_best_combo_yield_proposed_pairs(rows, policy)
        }
        for policy in policies
    }
    proposed_rank = {
        policy.variant_id: {
            _pair_key(row): index
            for index, row in enumerate(
                rank_combo_yield_proposed_rows(rows, policy),
                start=1,
            )
        }
        for policy in policies
    }
    capture_time = _strict_utc(entry_observed_at_utc)
    decisions: list[dict[str, Any]] = []
    for row in rows:
        pair_key = _pair_key(row)
        symbol = text(row.get("symbol")).upper()
        structure_mode = text(row.get("structure_mode")).lower()
        pair_id = shadow_combo_pair_id(
            dataset_id=dataset_id,
            account=account,
            symbol=symbol,
            structure_mode=structure_mode,
            put_contract_symbol=text(row.get("put_contract_symbol")),
            call_contract_symbol=text(row.get("call_contract_symbol")),
            entry_observed_at_utc=entry_observed_at_utc,
        )
        quote_quality = combo_entry_quote_quality(
            row,
            captured_at=capture_time,
            max_age_seconds=int(normalized_spec["max_entry_quote_age_seconds"]),
            max_skew_seconds=int(normalized_spec["max_entry_leg_skew_seconds"]),
        )
        provenance_reasons = _rank_provenance_reasons(row)
        variant_decisions = []
        for policy in policies:
            gate_reasons = list(combo_yield_proposed_gate_reasons(row, policy))
            if policy.variant_id in {
                text(value)
                for value in row.get("unavailable_variant_ids") or []
            }:
                gate_reasons.append("variant_capture_unavailable")
            if quote_quality["status"] != "complete":
                gate_reasons.extend(quote_quality["reason_codes"])
            gate_reasons.extend(provenance_reasons)
            gate_reasons = list(dict.fromkeys(gate_reasons))
            baseline_position = baseline_rank.get(pair_key)
            proposed_position = proposed_rank[policy.variant_id].get(pair_key)
            if gate_reasons:
                proposed_position = None
            variant_decisions.append(
                {
                    "variant_id": policy.variant_id,
                    "accepted": not gate_reasons,
                    "selected": pair_key in proposed_selected[policy.variant_id] and not gate_reasons,
                    "gate_reasons": gate_reasons,
                    "rank_key": (
                        _typed_rank_key(combo_yield_proposed_rank_key(row, policy))
                        if not gate_reasons
                        else None
                    ),
                    "proposed_rank": proposed_position,
                    "baseline_rank": baseline_position,
                    "rank_changed": (
                        baseline_position != proposed_position
                        if baseline_position is not None and proposed_position is not None
                        else baseline_position is not proposed_position
                    ),
                    "rank_change_reason": _rank_change_reason(
                        baseline_position,
                        proposed_position,
                        policy=policy,
                        gate_reasons=gate_reasons,
                    ),
                    "delta_target_distance": _delta_target_distance(row, policy),
                    "expiry_gap_target_distance": _gap_target_distance(row, policy),
                }
            )
        decisions.append(
            {
                "schema_version": COMBO_PAIR_DECISION_SCHEMA_VERSION,
                "shadow_combo_pair_id": pair_id,
                "dataset_id": dataset_id,
                "account": text(account).lower(),
                "symbol": symbol,
                "structure_mode": structure_mode,
                "put_contract_symbol": text(row.get("put_contract_symbol")),
                "call_contract_symbol": text(row.get("call_contract_symbol")),
                "put_expiration": row.get("put_expiration"),
                "call_expiration": row.get("call_expiration"),
                "put_strike": first_float(row, "put_strike"),
                "call_strike": first_float(row, "call_strike"),
                "currency": row.get("currency"),
                "multiplier": first_float(row, "multiplier", "contract_multiplier"),
                "entry_observed_at_utc": entry_observed_at_utc,
                "put_bid": first_float(row, "put_bid"),
                "put_ask": first_float(row, "put_ask"),
                "call_ask": first_float(row, "call_ask"),
                "call_bid": first_float(row, "call_bid"),
                "spot": first_float(row, "spot", "underlying_price"),
                "put_open_fee": first_float(row, "put_open_fee", "put_estimated_fees") or 0.0,
                "call_open_fee": first_float(row, "call_open_fee", "call_estimated_fees") or 0.0,
                "put_close_fee": first_float(row, "put_close_fee"),
                "call_close_fee": first_float(row, "call_close_fee"),
                "stock_liquidation_fee": first_float(row, "stock_liquidation_fee"),
                "put_net_credit": first_float(row, "put_net_credit"),
                "call_total_cost": first_float(row, "call_total_cost"),
                "combo_net_credit": first_float(row, "combo_net_credit"),
                "net_credit_retention": first_float(row, "net_credit_retention"),
                "call_cost_to_put_credit": first_float(row, "call_cost_to_put_credit"),
                "call_delta": first_float(row, "call_delta"),
                "put_implied_volatility": first_float(row, "put_implied_volatility"),
                "call_implied_volatility": first_float(row, "call_implied_volatility"),
                "expiry_gap_days": _expiry_gap_days(row),
                "put_spread_ratio": first_float(row, "put_spread_ratio"),
                "put_open_interest": first_float(row, "put_open_interest"),
                "call_spread_ratio": first_float(row, "call_spread_ratio"),
                "call_open_interest": first_float(row, "call_open_interest"),
                "annualized_net_credit_yield": first_float(row, "annualized_net_credit_yield"),
                "funding_put_rank": _positive_int(row.get("funding_put_rank")),
                "funding_put_rank_key": row.get("funding_put_rank_key"),
                "combo_rank_scope_hash": (
                    text(row.get("combo_rank_scope_hash"))
                    or combo_rank_scope_hash(
                        dataset_id=dataset_id,
                        account=account,
                        symbol=symbol,
                        entry_observed_at_utc=entry_observed_at_utc,
                        effective_combo_policy_hash=effective_combo_policy_hash,
                        variant_spec_hash=_canonical_hash(normalized_spec),
                        required_data_file_sha256=required_hashes,
                    )
                ),
                "source_candidate_count": _positive_int(row.get("source_candidate_count")),
                "baseline_selected": pair_key in baseline_selected,
                "baseline_rank": baseline_rank.get(pair_key),
                "variant_decisions": variant_decisions,
                "quote_quality": quote_quality,
                "required_data_file_sha256": required_hashes,
                "safety": safety_payload(writes_local_dataset=False),
            }
        )
    return sorted(
        decisions,
        key=lambda row: (
            text(row.get("symbol")),
            _positive_int(row.get("funding_put_rank")) or 2**31 - 1,
            text(row.get("put_contract_symbol")),
            text(row.get("call_contract_symbol")),
        ),
    )


def attach_funding_put_rank_provenance(
    *,
    pair_rows: Iterable[dict[str, Any]],
    ranked_put_rows: Iterable[dict[str, Any]],
    combo_rank_scope_hash_value: str,
) -> list[dict[str, Any]]:
    """Attach canonical one-based Put rank without renumbering after pair filters."""

    puts = [dict(row) for row in ranked_put_rows]
    rank_by_contract: dict[str, tuple[int, list[dict[str, Any]]]] = {}
    for index, row in enumerate(puts, start=1):
        contract = text(row.get("contract_symbol") or row.get("option_symbol"))
        if not contract:
            raise ValueError("ranked Funding Put row is missing contract_symbol")
        rank_by_contract[contract] = (
            index,
            _typed_rank_key(underwriting_rank_key(row, mode="put")),
        )
    out: list[dict[str, Any]] = []
    for source in pair_rows:
        row = dict(source)
        contract = text(row.get("put_contract_symbol"))
        provenance = rank_by_contract.get(contract)
        if provenance is None:
            row["funding_put_rank"] = None
            row["funding_put_rank_key"] = None
            row["funding_put_rank_unavailable_reason"] = "put_contract_not_in_underwritten_universe"
        else:
            row["funding_put_rank"] = provenance[0]
            row["funding_put_rank_key"] = provenance[1]
            row["funding_put_rank_unavailable_reason"] = None
        row["combo_rank_scope_hash"] = text(combo_rank_scope_hash_value) or None
        row["source_candidate_count"] = len(puts)
        out.append(row)
    return out


def combo_entry_quote_quality(
    row: dict[str, Any],
    *,
    captured_at: datetime,
    max_age_seconds: int,
    max_skew_seconds: int,
) -> dict[str, Any]:
    times: dict[str, datetime] = {}
    reasons: list[str] = []
    for leg, keys in (
        ("put", ("put_quote_observed_at_utc", "put_observed_at_utc")),
        ("call", ("call_quote_observed_at_utc", "call_observed_at_utc")),
        ("spot", ("spot_observed_at_utc", "underlying_observed_at_utc")),
    ):
        raw = next((row.get(key) for key in keys if row.get(key)), None)
        if raw is None:
            reasons.append(f"{leg}_quote_timestamp_missing")
            continue
        try:
            times[leg] = _strict_utc(str(raw))
        except ValueError:
            reasons.append(f"{leg}_quote_timestamp_invalid")
    ages = {
        leg: max(0.0, (captured_at - observed).total_seconds())
        for leg, observed in times.items()
    }
    for leg, age in ages.items():
        if age > float(max_age_seconds):
            reasons.append(f"{leg}_quote_stale")
    skew = None
    if len(times) == 3:
        skew = (max(times.values()) - min(times.values())).total_seconds()
        if skew > float(max_skew_seconds):
            reasons.append("entry_leg_time_skew")
    return {
        "status": "complete" if not reasons else "unavailable",
        "reason_codes": list(dict.fromkeys(reasons)),
        "age_seconds": ages,
        "leg_skew_seconds": skew,
        "max_age_seconds": int(max_age_seconds),
        "max_leg_skew_seconds": int(max_skew_seconds),
    }


def combo_rank_scope_hash(
    *,
    dataset_id: str,
    account: str,
    symbol: str,
    entry_observed_at_utc: str,
    effective_combo_policy_hash: str,
    variant_spec_hash: str,
    required_data_file_sha256: dict[str, str],
) -> str:
    return _canonical_hash(
        {
            "dataset_id": text(dataset_id),
            "account": text(account).lower(),
            "symbol": text(symbol).upper(),
            "entry_observed_at_utc": text(entry_observed_at_utc),
            "effective_combo_policy_hash": text(effective_combo_policy_hash),
            "variant_spec_hash": text(variant_spec_hash),
            "required_data_file_sha256": dict(sorted(required_data_file_sha256.items())),
        }
    )


def shadow_combo_pair_id(
    *,
    dataset_id: str,
    account: str,
    symbol: str,
    structure_mode: str,
    put_contract_symbol: str,
    call_contract_symbol: str,
    entry_observed_at_utc: str,
) -> str:
    return _canonical_hash(
        {
            "dataset_id": text(dataset_id),
            "account": text(account).lower(),
            "symbol": text(symbol).upper(),
            "structure_mode": text(structure_mode).lower(),
            "put_contract_symbol": text(put_contract_symbol),
            "call_contract_symbol": text(call_contract_symbol),
            "entry_observed_at_utc": text(entry_observed_at_utc),
        }
    )


def publish_combo_pair_facet(
    *,
    dataset: str | Path,
    decisions: list[dict[str, Any]],
    marks: list[dict[str, Any]] | None = None,
    outcomes: list[dict[str, Any]] | None = None,
    write: bool = False,
) -> dict[str, Any]:
    dataset_dir = dataset_dir_from_arg(dataset)
    files = {
        COMBO_PAIR_DATASET_FILES[0]: decisions,
        COMBO_PAIR_DATASET_FILES[1]: list(marks or []),
        COMBO_PAIR_DATASET_FILES[2]: list(outcomes or []),
    }
    result = {
        "schema_version": "shadow_combo_pair_facet_publish.v1",
        "dataset_dir": str(dataset_dir),
        "written": bool(write),
        "counts": {name: len(rows) for name, rows in files.items()},
        "file_sha256": {},
        "safety": safety_payload(writes_local_dataset=bool(write)),
    }
    if not write:
        return result
    if not (dataset_dir / "manifest.json").is_file():
        raise ValueError(f"Shadow Replay manifest does not exist: {dataset_dir}")
    with dataset_write_lock(dataset_dir):
        validate_dataset_integrity(dataset_dir)
        for name, rows in files.items():
            write_jsonl(dataset_dir / name, rows)
            result["file_sha256"][name] = _file_sha256(dataset_dir / name)
        refresh_combo_pair_facet_manifest(dataset_dir)
        result["dataset_integrity"] = refresh_dataset_manifest(dataset_dir)["integrity"]
    return result


def refresh_combo_pair_facet_manifest(dataset: str | Path) -> dict[str, Any]:
    dataset_dir = dataset_dir_from_arg(dataset)
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"Shadow Replay manifest does not exist: {dataset_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hashes = {
        name: _file_sha256(dataset_dir / name)
        for name in COMBO_PAIR_DATASET_FILES
        if (dataset_dir / name).is_file()
    }
    counts = {
        name: len(read_jsonl(dataset_dir / name))
        for name in COMBO_PAIR_DATASET_FILES
        if (dataset_dir / name).is_file()
    }
    manifest["combo_pair_facet"] = {
        "schema_versions": {
            "decisions": COMBO_PAIR_DECISION_SCHEMA_VERSION,
            "marks": COMBO_PAIR_MARK_SCHEMA_VERSION,
            "outcomes": COMBO_PAIR_OUTCOME_SCHEMA_VERSION,
        },
        "files": {
            name: str((dataset_dir / name).resolve())
            for name in COMBO_PAIR_DATASET_FILES
            if (dataset_dir / name).is_file()
        },
        "file_sha256": hashes,
        "completeness": {
            "decision_count": counts.get(COMBO_PAIR_DATASET_FILES[0], 0),
            "mark_count": counts.get(COMBO_PAIR_DATASET_FILES[1], 0),
            "outcome_count": counts.get(COMBO_PAIR_DATASET_FILES[2], 0),
        },
    }
    write_json(manifest_path, manifest)
    return manifest["combo_pair_facet"]


def load_combo_pair_facet(dataset: str | Path) -> dict[str, list[dict[str, Any]]]:
    dataset_dir = dataset_dir_from_arg(dataset)
    with dataset_read_lock(dataset_dir):
        validate_dataset_integrity(dataset_dir, require_manifest=False)
        result = {
            "decisions": read_jsonl(dataset_dir / COMBO_PAIR_DATASET_FILES[0]),
            "marks": read_jsonl(dataset_dir / COMBO_PAIR_DATASET_FILES[1]),
            "outcomes": read_jsonl(dataset_dir / COMBO_PAIR_DATASET_FILES[2]),
        }
        validate_dataset_integrity(dataset_dir, require_manifest=False)
        return result


def _baseline_selected_keys(
    rows: list[dict[str, Any]],
    *,
    structure_mode: str | None,
) -> set[tuple[str, str]]:
    normalized_mode = text(structure_mode).lower()
    source_rows = [
        row
        for row in rows
        if not normalized_mode
        or text(row.get("structure_mode")).lower() == normalized_mode
    ]
    source_rows = [
        row for row in source_rows if bool(row.get("baseline_eligible", True))
    ]
    per_put: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in source_rows:
        grouped.setdefault(text(row.get("put_contract_symbol")), []).append(row)
    for group in grouped.values():
        if group:
            per_put.append(rank_yield_enhancement_rows(group)[0])
    return {
        _pair_key(row)
        for row in select_best_yield_enhancement_per_symbol(per_put)
    }


def _baseline_rank_map(
    rows: list[dict[str, Any]],
    *,
    structure_mode: str | None,
) -> dict[tuple[str, str], int]:
    normalized_mode = text(structure_mode).lower()
    eligible = [
        row
        for row in rows
        if bool(row.get("baseline_eligible", True))
        and (
            not normalized_mode
            or text(row.get("structure_mode")).lower() == normalized_mode
        )
    ]
    return {
        _pair_key(row): index
        for index, row in enumerate(
            rank_yield_enhancement_rows(eligible),
            start=1,
        )
    }


def _rank_change_reason(
    baseline_rank: int | None,
    proposed_rank: int | None,
    *,
    policy: ComboYieldResearchPolicy,
    gate_reasons: list[str],
) -> str:
    if gate_reasons:
        return "proposed_hard_gate_rejected:" + ",".join(gate_reasons)
    if baseline_rank is None and proposed_rank is not None:
        return "proposed_variant_only"
    if baseline_rank is not None and proposed_rank is None:
        return "baseline_only"
    if baseline_rank == proposed_rank:
        return "unchanged"
    return (
        "funding_put_rank_then_gap_delta_liquidity"
        if policy.structure_mode == "staggered_expiry_pair"
        else "funding_put_rank_then_delta_liquidity"
    )


def _rank_provenance_reasons(row: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if _positive_int(row.get("funding_put_rank")) is None:
        reasons.append("funding_put_rank_missing")
    rank_key = row.get("funding_put_rank_key")
    if not isinstance(rank_key, list) or not rank_key:
        reasons.append("funding_put_rank_key_missing")
    elif any(
        not isinstance(item, dict) or item.get("value") is None
        for item in rank_key
    ):
        reasons.append("funding_put_rank_key_incomplete")
    if _positive_int(row.get("source_candidate_count")) is None:
        reasons.append("source_candidate_count_missing")
    return reasons


def _pair_key(row: dict[str, Any]) -> tuple[str, str]:
    return (
        text(row.get("put_contract_symbol")),
        text(row.get("call_contract_symbol")),
    )


def _policy_payload(policy: ComboYieldResearchPolicy) -> dict[str, Any]:
    return asdict(policy)


def _delta_target_distance(row: dict[str, Any], policy: ComboYieldResearchPolicy) -> float | None:
    value = first_float(row, "call_delta")
    return None if value is None else abs(abs(value) - float(policy.target_abs_call_delta))


def _gap_target_distance(row: dict[str, Any], policy: ComboYieldResearchPolicy) -> int | None:
    if policy.target_expiry_gap_days is None:
        return None
    gap = _expiry_gap_days(row)
    return None if gap is None else abs(gap - int(policy.target_expiry_gap_days))


def _expiry_gap_days(row: dict[str, Any]) -> int | None:
    direct = first_float(row, "expiry_gap_days")
    if direct is not None:
        return int(direct)
    try:
        put = datetime.fromisoformat(text(row.get("put_expiration"))[:10]).date()
        call = datetime.fromisoformat(text(row.get("call_expiration"))[:10]).date()
    except ValueError:
        return None
    return (call - put).days


def _strict_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid UTC timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timezone required for timestamp: {value}")
    return parsed.astimezone(timezone.utc)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _typed_rank_key(values: tuple[Any, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for value in values:
        normalized = (
            None
            if isinstance(value, float) and not math.isfinite(value)
            else value
        )
        out.append(
            {
            "type": (
                "bool"
                if isinstance(value, bool)
                else "int"
                if isinstance(value, int)
                else "float"
                if isinstance(value, float)
                else "str"
            ),
                "value": normalized,
            }
        )
    return out


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "COMBO_CAPTURE_MANIFEST_SCHEMA_VERSION",
    "COMBO_PAIR_DATASET_FILES",
    "COMBO_PAIR_DECISION_SCHEMA_VERSION",
    "COMBO_PAIR_MARK_SCHEMA_VERSION",
    "COMBO_PAIR_OUTCOME_SCHEMA_VERSION",
    "COMBO_VARIANT_SPEC_SCHEMA_VERSION",
    "build_combo_pair_decisions",
    "attach_funding_put_rank_provenance",
    "combo_entry_quote_quality",
    "combo_rank_scope_hash",
    "combo_research_policy_from_dict",
    "combo_variant_spec_hash",
    "load_combo_pair_facet",
    "load_combo_variant_spec",
    "normalize_combo_variant_spec",
    "publish_combo_pair_facet",
    "refresh_combo_pair_facet_manifest",
    "shadow_combo_pair_id",
]
