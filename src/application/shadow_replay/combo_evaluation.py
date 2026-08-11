from __future__ import annotations

"""Build Combo Shadow decision facets from captured bytes and Combo-owned Put ranks."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from domain.domain.insurance_underwriting import rank_underwriting_candidates
from src.application.sell_put_call_helper import find_sell_put_yield_enhancement_pairs
from src.application.shadow_replay.combo_variants import (
    attach_funding_put_rank_provenance,
    build_combo_pair_decisions,
    combo_rank_scope_hash,
    publish_combo_pair_facet,
)
from src.application.shadow_replay.common import dataset_dir_from_arg, text


def evaluate_combo_variant_pairs(
    *,
    dataset: str | Path,
    underwritten_put_rows: Iterable[dict[str, Any]],
    sell_put_cfg_by_symbol: dict[str, dict[str, Any]] | None = None,
    global_liquidity_by_symbol: dict[str, dict[str, Any]] | None = None,
    write: bool = False,
    pair_builder: Callable[..., pd.DataFrame] = find_sell_put_yield_enhancement_pairs,
) -> dict[str, Any]:
    """Evaluate baseline and proposed variants without changing production delegates."""

    dataset_dir = dataset_dir_from_arg(dataset)
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    _verify_required_data_hashes(dataset_dir, manifest)
    variant_spec = manifest.get("normalized_variant_spec")
    if not isinstance(variant_spec, dict):
        raise ValueError("Combo capture manifest is missing normalized_variant_spec")
    effective_by_symbol = manifest.get("normalized_effective_combo_policy")
    if not isinstance(effective_by_symbol, dict):
        raise ValueError("Combo capture manifest is missing normalized_effective_combo_policy")
    observations = manifest.get("source_quote_observations")
    observations = observations if isinstance(observations, dict) else {}
    unavailable_variants_by_symbol = _unavailable_variants_by_symbol(manifest)
    sell_put_cfg_by_symbol = (
        sell_put_cfg_by_symbol
        if sell_put_cfg_by_symbol is not None
        else dict(manifest.get("normalized_sell_put_policy") or {})
    )
    global_liquidity_by_symbol = (
        global_liquidity_by_symbol
        if global_liquidity_by_symbol is not None
        else dict(manifest.get("normalized_global_combo_liquidity") or {})
    )

    puts_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in underwritten_put_rows:
        symbol = text(row.get("symbol")).upper()
        if symbol:
            puts_by_symbol.setdefault(symbol, []).append(dict(row))

    all_decisions: list[dict[str, Any]] = []
    pair_counts: dict[str, int] = {}
    for symbol in manifest.get("symbols") or []:
        symbol = text(symbol).upper()
        puts = rank_underwriting_candidates(puts_by_symbol.get(symbol, []), mode="put")
        if not puts:
            pair_counts[symbol] = 0
            continue
        baseline_cfg = deepcopy(effective_by_symbol.get(symbol) or {})
        baseline_cfg["enabled"] = True
        baseline_pairs = pair_builder(
            df_candidates=pd.DataFrame(puts),
            symbol=symbol,
            input_root=dataset_dir / "required_data",
            yield_enhancement_cfg=baseline_cfg,
            sell_put_cfg=dict(sell_put_cfg_by_symbol.get(symbol) or {}),
            global_yield_enhancement_liquidity=dict(
                global_liquidity_by_symbol.get(symbol) or {}
            ),
            output_path=None,
        )
        baseline_keys = {
            _pair_key(row)
            for row in baseline_pairs.to_dict("records")
        }
        pair_rows: list[dict[str, Any]] = baseline_pairs.to_dict("records")
        variants = [
            item
            for item in variant_spec.get("variants") or []
            if isinstance(item, dict)
        ]
        for mode in sorted({text(item.get("structure_mode")).lower() for item in variants}):
            mode_variants = [
                item for item in variants if text(item.get("structure_mode")).lower() == mode
            ]
            superset_cfg = _superset_combo_config(baseline_cfg, mode_variants, structure_mode=mode)
            pairs = pair_builder(
                df_candidates=pd.DataFrame(puts),
                symbol=symbol,
                input_root=dataset_dir / "required_data",
                yield_enhancement_cfg=superset_cfg,
                sell_put_cfg=dict(sell_put_cfg_by_symbol.get(symbol) or {}),
                global_yield_enhancement_liquidity=dict(
                    global_liquidity_by_symbol.get(symbol) or {}
                ),
                output_path=None,
            )
            pair_rows.extend(pairs.to_dict("records"))
        pair_rows = _dedupe_pairs(pair_rows)
        observed_rows = [
            _attach_entry_observation_times(
                row,
                observations=observations.get(symbol) or [],
                captured_at=text(manifest.get("capture_observed_at_utc")),
                unavailable_variants=unavailable_variants_by_symbol.get(symbol, set()),
                baseline_eligible=_pair_key(row) in baseline_keys,
                baseline_cfg=baseline_cfg,
            )
            for row in pair_rows
        ]
        scope_hash = combo_rank_scope_hash(
            dataset_id=text(manifest.get("dataset_id")),
            account=text(manifest.get("account")),
            symbol=symbol,
            entry_observed_at_utc=text(manifest.get("capture_observed_at_utc")),
            effective_combo_policy_hash=text(manifest.get("effective_combo_policy_hash")),
            variant_spec_hash=text(manifest.get("variant_spec_hash")),
            required_data_file_sha256=dict(manifest.get("required_data_file_sha256") or {}),
        )
        ranked_rows = attach_funding_put_rank_provenance(
            pair_rows=observed_rows,
            ranked_put_rows=puts,
            combo_rank_scope_hash_value=scope_hash,
        )
        decisions = build_combo_pair_decisions(
            dataset_id=text(manifest.get("dataset_id")),
            account=text(manifest.get("account")),
            pair_rows=ranked_rows,
            effective_combo_policy_hash=text(manifest.get("effective_combo_policy_hash")),
            variant_spec=variant_spec,
            required_data_file_sha256=dict(manifest.get("required_data_file_sha256") or {}),
            entry_observed_at_utc=text(manifest.get("capture_observed_at_utc")),
            baseline_structure_mode=text(baseline_cfg.get("structure_mode")),
        )
        all_decisions.extend(decisions)
        pair_counts[symbol] = len(decisions)
    publication = publish_combo_pair_facet(
        dataset=dataset_dir,
        decisions=all_decisions,
        write=write,
    )
    return {
        "schema_version": "shadow_combo_pair_evaluation.v1",
        "dataset_dir": str(dataset_dir),
        "pair_counts_by_symbol": pair_counts,
        "decision_count": len(all_decisions),
        "selected_baseline_count": sum(
            1 for row in all_decisions if row.get("baseline_selected")
        ),
        "selected_proposed_count": sum(
            1
            for row in all_decisions
            for item in row.get("variant_decisions") or []
            if isinstance(item, dict) and item.get("selected")
        ),
        "decisions": all_decisions,
        "publication": publication,
    }


def evaluate_combo_variant_dataset(
    *,
    dataset: str | Path,
    underwritten_put_path: str | Path,
    write: bool = False,
) -> dict[str, Any]:
    source = Path(underwritten_put_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Combo-owned underwritten Put artifact does not exist: {source}")
    from src.application.shadow_replay.combo_funding import (
        validate_combo_funding_put_source,
    )

    validate_combo_funding_put_source(
        dataset=dataset,
        underwritten_put_path=source,
    )
    try:
        rows = pd.read_csv(source).to_dict("records")
    except Exception as exc:
        raise ValueError(f"failed to read Combo-owned underwritten Put artifact: {source}") from exc
    result = evaluate_combo_variant_pairs(
        dataset=dataset,
        underwritten_put_rows=rows,
        write=write,
    )
    result["underwritten_put_path"] = str(source)
    result["underwritten_put_sha256"] = _file_sha256(source)
    return result


def _superset_combo_config(
    baseline: dict[str, Any],
    variants: list[dict[str, Any]],
    *,
    structure_mode: str,
) -> dict[str, Any]:
    cfg = deepcopy(baseline)
    cfg["enabled"] = True
    cfg["structure_mode"] = structure_mode
    cfg["funding_mode"] = "credit_or_even"
    cfg["min_net_credit_retention"] = min(
        float(item["min_net_credit_retention"]) for item in variants
    )
    cfg.pop("min_net_credit_annualized", None)
    if all(item.get("max_call_cost_to_put_credit") is not None for item in variants):
        cfg["max_call_cost_to_put_credit"] = max(
            float(item["max_call_cost_to_put_credit"]) for item in variants
        )
    else:
        cfg.pop("max_call_cost_to_put_credit", None)
    call_cfg = dict(cfg.get("call") or {})
    call_cfg["min_delta"] = min(float(item["min_abs_call_delta"]) for item in variants)
    call_cfg["max_delta"] = max(float(item["max_abs_call_delta"]) for item in variants)
    cfg["call"] = call_cfg
    explicit = {
        "enabled",
        "structure_mode",
        "funding_mode",
        "min_net_credit_retention",
        "call",
    }
    if "max_call_cost_to_put_credit" in cfg:
        explicit.add("max_call_cost_to_put_credit")
    cfg["_explicit_fields"] = tuple(sorted(explicit))
    cfg["_explicit_call_fields"] = ("min_delta", "max_delta")
    return cfg


def _attach_entry_observation_times(
    row: dict[str, Any],
    *,
    observations: list[dict[str, Any]],
    captured_at: str,
    unavailable_variants: set[str],
    baseline_eligible: bool,
    baseline_cfg: dict[str, Any],
) -> dict[str, Any]:
    out = dict(row)
    put_at = _observation_for(
        observations,
        option_type="put",
        expiration=text(row.get("put_expiration"))[:10],
    )
    call_at = _observation_for(
        observations,
        option_type="call",
        expiration=text(row.get("call_expiration"))[:10],
    )
    times = [value for value in (put_at, call_at) if value]
    out["put_quote_observed_at_utc"] = put_at
    out["call_quote_observed_at_utc"] = call_at
    out["spot_observed_at_utc"] = max(times) if len(times) == 2 else None
    out["unavailable_variant_ids"] = sorted(unavailable_variants)
    out["baseline_eligible"] = bool(baseline_eligible)
    threshold = baseline_cfg.get("min_net_credit_annualized")
    annualized = out.get("annualized_net_credit_yield")
    out["same_expiry_min_net_credit_annualized_pass"] = (
        True
        if threshold in (None, "")
        else annualized not in (None, "")
        and float(annualized) >= float(threshold)
    )
    out["entry_capture_observed_at_utc"] = captured_at
    return out


def _observation_for(
    observations: list[dict[str, Any]],
    *,
    option_type: str,
    expiration: str,
) -> str | None:
    matches = [
        text(item.get("observed_at_utc"))
        for item in observations
        if option_type in [text(value).lower() for value in item.get("option_types") or []]
        and expiration in [text(value)[:10] for value in item.get("expirations") or []]
        and text(item.get("observed_at_utc"))
    ]
    return max(matches) if matches else None


def _unavailable_variants_by_symbol(
    manifest: dict[str, Any],
) -> dict[str, set[str]]:
    symbols = {
        text(symbol).upper()
        for symbol in manifest.get("symbols") or []
        if text(symbol)
    }
    unavailable: dict[str, set[str]] = {symbol: set() for symbol in symbols}
    for item in manifest.get("variant_completeness") or []:
        if not isinstance(item, dict):
            continue
        variant_id = text(item.get("variant_id"))
        if not variant_id or text(item.get("status")).lower() == "complete":
            continue
        missing = [
            row
            for row in item.get("missing_expirations_or_contracts") or []
            if isinstance(row, dict)
        ]
        affected_symbols = {
            text(row.get("symbol")).upper()
            for row in missing
            if text(row.get("symbol"))
        }
        # If the manifest has no symbol-scoped evidence, fail closed globally.
        for symbol in affected_symbols or symbols:
            unavailable.setdefault(symbol, set()).add(variant_id)
    return unavailable


def _verify_required_data_hashes(dataset_dir: Path, manifest: dict[str, Any]) -> None:
    hashes = manifest.get("required_data_file_sha256")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("Combo capture manifest has no required-data hashes")
    mismatches = []
    for relative, expected in hashes.items():
        path = (dataset_dir / str(relative)).resolve()
        try:
            path.relative_to(dataset_dir)
        except ValueError:
            mismatches.append(f"{relative}:escapes_dataset")
            continue
        actual = _file_sha256(path) if path.is_file() else None
        if actual != str(expected):
            mismatches.append(str(relative))
    if mismatches:
        raise ValueError(f"Combo required-data hash verification failed: {mismatches}")


def _dedupe_pairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        by_key[
            (
                text(row.get("structure_mode")).lower(),
                text(row.get("put_contract_symbol")),
                text(row.get("call_contract_symbol")),
            )
        ] = row
    return [by_key[key] for key in sorted(by_key)]


def _pair_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        text(row.get("structure_mode")).lower(),
        text(row.get("put_contract_symbol")),
        text(row.get("call_contract_symbol")),
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["evaluate_combo_variant_dataset", "evaluate_combo_variant_pairs"]
