from __future__ import annotations

import hashlib
import json
import math
import re
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domain.domain.engine import CandidateScoreWeights, explain_candidate_rank

from src.application.candidate_filter_trace import (
    build_candidate_replay_fields,
    infer_trace_scope_from_path,
    read_candidate_filter_trace,
)
from src.application.shadow_replay.candidate_analysis import analyze_rows
from src.application.shadow_replay.close_decision_policy import (
    close_decision_facts_from_row,
    evaluate_shadow_close_policy_variants,
)
from src.application.shadow_replay.common import (
    CANDIDATE_SNAPSHOT_SCHEMA_VERSION,
    CLOSE_DECISION_EPISODE_SCHEMA_VERSION,
    CLOSE_DECISION_MARK_SCHEMA_VERSION,
    CLOSE_DECISION_OUTCOME_SCHEMA_VERSION,
    DATASET_FILES,
    DATASET_SCHEMA_VERSION,
    FILTER_DECISION_SCHEMA_VERSION,
    MARK_PATH_SCHEMA_VERSION,
    OUTCOME_FACT_SCHEMA_VERSION,
    OPTIONAL_CLOSE_DATASET_FILES,
    RANK_SNAPSHOT_SCHEMA_VERSION,
    abs_first_float,
    account_hint,
    bind_legacy_decision_evidence,
    dataset_integrity_payload,
    dataset_output_dir,
    default_dataset_id,
    first_float,
    glob_many,
    normal_status,
    read_csv_rows,
    read_jsonl,
    resolve_many,
    resolve_optional,
    safe_rel,
    safety_payload,
    strategy_hint,
    strategy_mode,
    text,
    unique,
    utc_now,
    with_decision_identity,
    write_json,
    write_jsonl,
)


@dataclass(frozen=True)
class ShadowReplaySourceSelection:
    repo_root: Path
    run_id: str | None = None
    runs_root: Path | None = None
    run_dir: Path | None = None
    report_dir: Path | None = None
    candidate_paths: tuple[Path, ...] = ()
    trace_paths: tuple[Path, ...] = ()
    reject_log_paths: tuple[Path, ...] = ()
    mark_paths: tuple[Path, ...] = ()
    outcome_paths: tuple[Path, ...] = ()
    close_advice_paths: tuple[Path, ...] = ()
    position_context_paths: tuple[Path, ...] = ()
    reallocation_paths: tuple[Path, ...] = ()
    run_audit_paths: tuple[Path, ...] = ()


def build_shadow_replay_dataset(
    *,
    repo_root: Path,
    run_id: str | None = None,
    runs_root: str | Path | None = None,
    run_dir: str | Path | None = None,
    report_dir: str | Path | None = None,
    candidate_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    trace_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    reject_log_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    mark_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    outcome_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    close_advice_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    position_context_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    reallocation_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    run_audit_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    include_close_decisions: bool = False,
    output_dir: str | Path | None = None,
    dataset_root: str | Path | None = None,
    dataset_id: str | None = None,
    latest_scanned_run: bool = False,
) -> dict[str, Any]:
    """Build a local replay dataset from existing read-only scan artifacts."""

    base = repo_root.resolve()
    run_id_text = (str(run_id).strip() or None) if run_id else None
    runs_root_path = resolve_optional(runs_root, base=base)
    run_dir_path = resolve_optional(run_dir, base=base)
    latest_selection: dict[str, Any] = {
        "requested": bool(latest_scanned_run),
        "found": None,
        "path": None,
        "run_id": None,
        "searched_count": 0,
        "skipped_without_evidence_count": 0,
    }
    if run_dir_path is None and bool(latest_scanned_run):
        run_dir_path, latest_selection = latest_shadow_replay_run_dir(
            repo_root=base,
            runs_root=runs_root_path,
        )
        if run_dir_path is None:
            raise ValueError("latest scanned run with shadow replay evidence not found")
        run_id_text = run_dir_path.name
    elif run_dir_path is None and run_id_text:
        root = runs_root_path or (base / "output_runs").resolve()
        run_dir_path = (root / run_id_text).resolve()
    selection = ShadowReplaySourceSelection(
        repo_root=base,
        run_id=run_id_text,
        runs_root=runs_root_path,
        run_dir=run_dir_path,
        report_dir=resolve_optional(report_dir, base=base),
        candidate_paths=tuple(resolve_many(candidate_paths, base=base)),
        trace_paths=tuple(resolve_many(trace_paths, base=base)),
        reject_log_paths=tuple(resolve_many(reject_log_paths, base=base)),
        mark_paths=tuple(resolve_many(mark_paths, base=base)),
        outcome_paths=tuple(resolve_many(outcome_paths, base=base)),
        close_advice_paths=tuple(resolve_many(close_advice_paths, base=base)),
        position_context_paths=tuple(resolve_many(position_context_paths, base=base)),
        reallocation_paths=tuple(resolve_many(reallocation_paths, base=base)),
        run_audit_paths=tuple(resolve_many(run_audit_paths, base=base)),
    )
    resolved_candidates = candidate_paths_from_selection(selection)
    resolved_traces = trace_paths_from_selection(selection)
    resolved_reject_logs = reject_log_paths_from_selection(selection)
    resolved_marks = mark_paths_from_selection(selection)
    resolved_outcomes = outcome_paths_from_selection(selection)
    close_facet_requested = bool(
        include_close_decisions
        or close_advice_paths
        or position_context_paths
        or reallocation_paths
        or run_audit_paths
    )
    resolved_close_advice: list[Path] = []
    resolved_position_contexts: list[Path] = []
    resolved_reallocations: list[Path] = []
    resolved_run_audits: list[Path] = []
    close_decision_episodes: list[dict[str, Any]] = []
    if close_facet_requested:
        resolved_close_advice = close_advice_paths_from_selection(selection)
        resolved_position_contexts = position_context_paths_from_selection(
            selection,
            close_paths=resolved_close_advice,
        )
        resolved_reallocations = reallocation_paths_from_selection(
            selection,
            close_paths=resolved_close_advice,
        )
        resolved_run_audits = run_audit_paths_from_selection(
            selection,
            close_paths=resolved_close_advice,
        )
        close_decision_episodes = capture_close_decision_episodes(
            close_paths=resolved_close_advice,
            position_context_paths=resolved_position_contexts,
            reallocation_paths=resolved_reallocations,
            run_audit_paths=resolved_run_audits,
            base=base,
        )

    candidate_rows = accepted_candidate_snapshots(resolved_candidates, base=base)
    filter_decisions = filter_decision_rows(resolved_traces, resolved_reject_logs, base=base)
    rejected_rows = candidate_snapshots_from_filter_decisions(filter_decisions)
    candidate_snapshots = dedupe_snapshots(
        _attach_parameter_snapshots(candidate_rows + rejected_rows, filter_decisions)
    )
    rank_snapshots = rank_snapshots_for_candidates(candidate_snapshots)
    mark_snapshots = read_replay_rows(resolved_marks, schema_version=MARK_PATH_SCHEMA_VERSION, base=base)
    outcome_facts = read_replay_rows(resolved_outcomes, schema_version=OUTCOME_FACT_SCHEMA_VERSION, base=base)
    mark_snapshots = bind_legacy_decision_evidence(candidate_snapshots, mark_snapshots)
    outcome_facts = bind_legacy_decision_evidence(candidate_snapshots, outcome_facts)

    ds_id = str(dataset_id or "").strip() or default_dataset_id()
    dataset_root_path = resolve_optional(dataset_root, base=base)
    target = (
        (dataset_root_path / ds_id).resolve()
        if output_dir is None and dataset_root_path is not None
        else dataset_output_dir(output_dir, dataset_id=ds_id, base=base)
    )
    analysis_seed = analyze_rows(
        candidate_snapshots=candidate_snapshots,
        filter_decisions=filter_decisions,
        mark_snapshots=mark_snapshots,
        outcome_facts=outcome_facts,
        min_sample=1,
    )
    manifest = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "dataset_id": ds_id,
        "created_at_utc": utc_now(),
        "dataset_dir": str(target),
        "source": {
            "run_id": selection.run_id,
            "runs_root": safe_rel(selection.runs_root, base=base),
            "run_dir": safe_rel(selection.run_dir, base=base),
            "latest_scanned_run": bool(latest_scanned_run),
            "latest_scanned_run_selection": latest_selection,
            "report_dir": safe_rel(selection.report_dir, base=base),
            "candidate_paths": [safe_rel(path, base=base) for path in resolved_candidates],
            "trace_paths": [safe_rel(path, base=base) for path in resolved_traces],
            "reject_log_paths": [safe_rel(path, base=base) for path in resolved_reject_logs],
            "mark_paths": [safe_rel(path, base=base) for path in resolved_marks],
            "outcome_paths": [safe_rel(path, base=base) for path in resolved_outcomes],
        },
        "files": {name: str((target / name).resolve()) for name in DATASET_FILES},
        "summary": analysis_seed["summary"],
        "evidence_checks": analysis_seed["evidence_checks"],
        "safety": safety_payload(writes_local_dataset=True),
    }
    if close_facet_requested:
        manifest["source"].update(
            {
                "close_advice_paths": [safe_rel(path, base=base) for path in resolved_close_advice],
                "position_context_paths": [
                    safe_rel(path, base=base) for path in resolved_position_contexts
                ],
                "reallocation_paths": [safe_rel(path, base=base) for path in resolved_reallocations],
                "run_audit_paths": [safe_rel(path, base=base) for path in resolved_run_audits],
            }
        )
        manifest["files"].update(
            {name: str((target / name).resolve()) for name in OPTIONAL_CLOSE_DATASET_FILES}
        )
        manifest["summary"]["close_decision_episode_count"] = len(close_decision_episodes)
        manifest["close_decision_facet"] = {
            "episode_schema_version": CLOSE_DECISION_EPISODE_SCHEMA_VERSION,
            "mark_schema_version": CLOSE_DECISION_MARK_SCHEMA_VERSION,
            "outcome_schema_version": CLOSE_DECISION_OUTCOME_SCHEMA_VERSION,
            "episode_count": len(close_decision_episodes),
        }
    if target.exists():
        raise ValueError(f"Shadow Replay dataset target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{target.name}.staging-",
        dir=str(target.parent),
    ) as raw_staging:
        staging = Path(raw_staging)
        write_jsonl(staging / "candidate_snapshots.jsonl", candidate_snapshots)
        write_jsonl(staging / "filter_decisions.jsonl", filter_decisions)
        write_jsonl(staging / "rank_snapshots.jsonl", rank_snapshots)
        write_jsonl(staging / "mark_path_snapshots.jsonl", mark_snapshots)
        write_jsonl(staging / "outcome_facts.jsonl", outcome_facts)
        if close_facet_requested:
            write_jsonl(staging / OPTIONAL_CLOSE_DATASET_FILES[0], close_decision_episodes)
            write_jsonl(staging / OPTIONAL_CLOSE_DATASET_FILES[1], [])
            write_jsonl(staging / OPTIONAL_CLOSE_DATASET_FILES[2], [])
        (staging / ".dataset.lock").touch()
        manifest["integrity"] = dataset_integrity_payload(
            staging,
            generation_id=f"generation:{uuid.uuid4().hex}",
            revision=1,
        )
        write_json(staging / "manifest.json", manifest)
        os.replace(staging, target)
    return manifest


_RUN_ID_RE = re.compile(r"^(?P<timestamp>\d{8}T\d{6}Z)(?:[-_].*)?$")
_QUOTE_TIME_FIELDS = (
    "quote_as_of_utc",
    "quote_timestamp_utc",
    "quote_timestamp",
    "quote_time_utc",
    "quote_time",
)
_MATERIAL_RATIO_FIELDS = (
    "capture_ratio",
    "remaining_annualized_return",
    "spread_ratio",
    "close_fee_to_remaining_premium",
    "replacement_annualized_return",
    "replacement_annualized_advantage",
)
_MATERIAL_MONEY_FIELDS = (
    "close_mid",
    "bid",
    "ask",
    "remaining_premium",
    "estimated_close_fee",
    "estimated_pnl_if_close_net",
    "close_fee",
)


def capture_close_decision_episodes(
    *,
    close_paths: list[Path],
    position_context_paths: list[Path],
    reallocation_paths: list[Path],
    run_audit_paths: list[Path],
    base: Path,
) -> list[dict[str, Any]]:
    """Capture immutable, point-in-time close observations as replay episodes."""

    if not close_paths:
        raise ValueError("close decision facet requested but no close_advice.csv was found")
    contexts = _position_context_index(position_context_paths)
    reallocations = _reallocation_index(reallocation_paths)
    decision_times = _close_decision_time_index(run_audit_paths)
    observations: list[dict[str, Any]] = []
    observed_lots: set[tuple[str, str, str]] = set()
    for close_path in close_paths:
        run_id, run_started_at = _run_anchor(close_path)
        source_account = account_hint(close_path)
        close_rows = read_csv_rows(close_path)
        for row_number, source_row in enumerate(close_rows, start=1):
            row = dict(source_row)
            account = text(row.get("account")).lower() or source_account
            if not account:
                raise ValueError(f"close advice account missing: {close_path}:{row_number}")
            if source_account and account != source_account:
                raise ValueError(
                    f"close advice account conflicts with source directory: {close_path}:{row_number}"
                )
            lot_id = text(row.get("position_lot_id"))
            if not lot_id:
                raise ValueError(f"close advice position_lot_id missing: {close_path}:{row_number}")
            observation_key = (run_id, account, lot_id)
            if observation_key in observed_lots:
                raise ValueError(
                    f"close advice lot appears more than once in one run: run_id={run_id} account={account} lot_id={lot_id}"
                )
            observed_lots.add(observation_key)
            decision_time = decision_times.get((run_id, account))
            if decision_time is None:
                raise ValueError(
                    f"successful close_advice audit timestamp missing for run/account: run_id={run_id} account={account}"
                )
            audit_path, observed_at = decision_time
            if observed_at < run_started_at:
                raise ValueError(
                    f"close_advice audit timestamp precedes run start: run_id={run_id} account={account}"
                )
            context_entry = contexts.get((run_id, account))
            if context_entry is None:
                raise ValueError(
                    f"position context missing for run/account: run_id={run_id} account={account}"
                )
            context_path, context = context_entry
            _validate_context_time(context, observed_at=observed_at, path=context_path)
            position = _exact_position_lot(
                context,
                lot_id=lot_id,
                account=account,
                path=context_path,
            )
            quote_time, quote_time_basis = _quote_time(
                row,
                observed_at=observed_at,
                close_path=close_path,
                run_id=run_id,
            )
            reallocation_row = _exact_reallocation_row(
                reallocations.get((run_id, account), []),
                lot_id=lot_id,
                path_by_row=True,
            )
            _validate_replacement_time(
                reallocation_row,
                observed_at=observed_at,
                close_path=close_path,
            )
            observations.append(
                _close_episode_observation(
                    row=row,
                    position=position,
                    reallocation_row=reallocation_row,
                    account=account,
                    lot_id=lot_id,
                    run_id=run_id,
                    observed_at=observed_at,
                    quote_time=quote_time,
                    quote_time_basis=quote_time_basis,
                    strategy_context_at=text(context.get("as_of_utc")),
                    close_path=close_path,
                    context_path=context_path,
                    audit_path=audit_path,
                    source_row_number=row_number,
                    base=base,
                )
            )
    return dedupe_close_decision_episodes(observations)


def dedupe_close_decision_episodes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            text(row.get("episode_date")),
            text(row.get("account")).lower(),
            text(row.get("position_lot_id")),
            text(row.get("formal_policy_result", {}).get("policy_version")),
            text(row.get("material_fact_fingerprint")),
        )
        grouped.setdefault(key, []).append(row)

    episodes: list[dict[str, Any]] = []
    for observations in grouped.values():
        ordered = sorted(
            observations,
            key=lambda item: (
                text(item.get("observed_at_utc")),
                text(item.get("source", {}).get("close_advice_path")),
                int(item.get("source", {}).get("row_number") or 0),
            ),
        )
        episode = dict(ordered[0])
        source_run_ids = sorted(
            {text(item.get("source_run_id")) for item in ordered if text(item.get("source_run_id"))}
        )
        sources = [item.get("source") for item in ordered if isinstance(item.get("source"), dict)]
        episode["source_run_ids"] = source_run_ids
        episode["source_observation_count"] = len(ordered)
        episode["source_observations"] = sources
        episode["episode_id"] = _sha256_text(
            "|".join(
                (
                    text(episode.get("account")).lower(),
                    text(episode.get("position_lot_id")),
                    text(episode.get("formal_policy_result", {}).get("policy_version")),
                    text(episode.get("observed_at_utc")),
                    text(episode.get("material_fact_fingerprint")),
                )
            )
        )
        episodes.append(episode)
    return sorted(
        episodes,
        key=lambda item: (
            text(item.get("observed_at_utc")),
            text(item.get("account")),
            text(item.get("position_lot_id")),
            text(item.get("episode_id")),
        ),
    )


def _close_episode_observation(
    *,
    row: dict[str, Any],
    position: dict[str, Any],
    reallocation_row: dict[str, Any] | None,
    account: str,
    lot_id: str,
    run_id: str,
    observed_at: datetime,
    quote_time: str,
    quote_time_basis: str,
    strategy_context_at: str,
    close_path: Path,
    context_path: Path,
    audit_path: Path,
    source_row_number: int,
    base: Path,
) -> dict[str, Any]:
    facts = close_decision_facts_from_row(row)
    policy_results = evaluate_shadow_close_policy_variants(
        row,
        reallocation_row=reallocation_row,
    )
    formal = _formal_policy_result(row, path=close_path, row_number=source_row_number)
    normalized_results = {
        policy: _policy_result_payload(result)
        for policy, result in sorted(policy_results.items())
    }
    if (
        formal["recommendation_state"]
        != normalized_results["P0_current"]["recommendation_state"]
    ):
        raise ValueError(
            "formal close recommendation does not match P0_current projection: "
            f"path={close_path} row={source_row_number}"
        )
    decision_economics = _decision_economics(row, position=position)
    material_facts = {
        "normalized_decision_facts": asdict(facts),
        "formal_recommendation_state": formal["recommendation_state"],
        "policy_recommendations": {
            policy: result["recommendation_state"]
            for policy, result in normalized_results.items()
        },
        "economic_buckets": _material_economic_buckets(row),
        "threshold_inputs": _threshold_inputs(row),
        "decision_economics": decision_economics,
        "replacement": _material_replacement_facts(reallocation_row),
    }
    fingerprint = _sha256_json(material_facts)
    observed_text = _iso_utc(observed_at)
    return {
        "schema_version": CLOSE_DECISION_EPISODE_SCHEMA_VERSION,
        "episode_id": None,
        "episode_date": observed_text[:10],
        "episode_date_basis": "observed_at_utc",
        "account": account,
        "position_lot_id": lot_id,
        "source_run_id": run_id,
        "source_run_ids": [run_id],
        "observed_at_utc": observed_text,
        "quote_at_utc": quote_time,
        "quote_time_basis": quote_time_basis,
        "strategy_context_at_utc": strategy_context_at,
        "strategy_time_basis": "position_context_as_of_utc",
        "material_fact_fingerprint": fingerprint,
        "normalized_decision_facts": asdict(facts),
        "formal_policy_result": formal,
        "shadow_policy_results": normalized_results,
        "p0_parity": {
            "recommendation_matches": (
                formal["recommendation_state"]
                == normalized_results["P0_current"]["recommendation_state"]
            )
        },
        "material_economic_buckets": material_facts["economic_buckets"],
        "threshold_inputs": material_facts["threshold_inputs"],
        "decision_economics": decision_economics,
        "replacement_evidence": material_facts["replacement"],
        "replacement_provenance": _replacement_provenance(reallocation_row),
        "position_identity": _position_identity(position, row=row),
        "source": {
            "close_advice_path": safe_rel(close_path, base=base),
            "position_context_path": safe_rel(context_path, base=base),
            "run_audit_path": safe_rel(audit_path, base=base),
            "row_number": source_row_number,
        },
    }


def _formal_policy_result(row: dict[str, Any], *, path: Path, row_number: int) -> dict[str, Any]:
    required = (
        "policy_version",
        "recommendation_state",
        "decision_basis",
        "decision_evidence_status",
    )
    missing = [key for key in required if not text(row.get(key))]
    if missing:
        joined = ",".join(missing)
        raise ValueError(f"formal close policy fields missing ({joined}): {path}:{row_number}")
    return {
        "policy_version": text(row.get("policy_version")),
        "recommendation_state": text(row.get("recommendation_state")).lower(),
        "decision_basis": [
            token.strip()
            for token in text(row.get("decision_basis")).split(";")
            if token.strip()
        ],
        "decision_evidence_status": text(row.get("decision_evidence_status")).lower(),
    }


def _policy_result_payload(result: Any) -> dict[str, Any]:
    return {
        "policy_version": result.policy_version,
        "recommendation_state": result.recommendation_state,
        "decision_basis": list(result.decision_basis),
        "decision_evidence_status": result.decision_evidence_status,
    }


def _material_economic_buckets(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "tier": text(row.get("tier")).lower(),
        "exit_state": text(row.get("exit_state")).lower(),
        "evaluation_status": text(row.get("evaluation_status")).lower(),
        "fee_calc_status": text(row.get("fee_calc_status")).lower(),
        "close_calibration_status": text(row.get("close_calibration_status")).lower(),
        "dte": _rounded_number(row.get("dte"), digits=0),
    }
    for key in _MATERIAL_RATIO_FIELDS:
        out[key] = _rounded_number(row.get(key), digits=4)
    for key in _MATERIAL_MONEY_FIELDS:
        out[key] = _rounded_number(row.get(key), digits=2)
    return out


def _threshold_inputs(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "dte": _rounded_number(row.get("dte"), digits=0),
        "capture_ratio": _rounded_number(row.get("capture_ratio"), digits=12),
        "remaining_annualized_return": _rounded_number(
            row.get("remaining_annualized_return"),
            digits=12,
        ),
    }


def _material_replacement_facts(row: dict[str, Any] | None) -> dict[str, Any]:
    source = row if isinstance(row, dict) else {}
    return {
        "status": text(source.get("reallocation_status")).lower() or "not_evaluable",
        "reason": text(source.get("reallocation_reason")).lower(),
        "contract_symbol": text(source.get("replacement_contract_symbol")).upper() or None,
        "symbol": text(source.get("replacement_symbol")).upper() or None,
        "option_type": text(source.get("replacement_option_type")).lower() or None,
        "expiration": text(source.get("replacement_expiration")) or None,
        "strike": _rounded_number(source.get("replacement_strike"), digits=4),
        "rank": _rounded_number(source.get("replacement_rank"), digits=0),
        "entry_credit": _rounded_number(source.get("replacement_entry_credit"), digits=6),
        "contracts": _rounded_number(source.get("replacement_contracts"), digits=0),
        "multiplier": _rounded_number(source.get("replacement_multiplier"), digits=6),
        "currency": text(source.get("replacement_currency")).upper() or None,
        "fee_calc_status": text(source.get("replacement_fee_calc_status")).lower() or None,
        "open_fee": _rounded_number(source.get("replacement_open_fee"), digits=6),
        "entry_slippage": _rounded_number(source.get("replacement_spread_slippage"), digits=6),
    }


def _replacement_provenance(row: dict[str, Any] | None) -> dict[str, Any]:
    source = row if isinstance(row, dict) else {}
    if not source:
        return {
            "status": "not_applicable",
            "source_run_id": None,
            "source_run_at_utc": None,
        }
    return {
        "status": "validated_same_decision_run",
        "source_run_id": text(source.get("_source_run_id")) or None,
        "source_run_at_utc": text(source.get("_source_run_at_utc")) or None,
    }


def _decision_economics(row: dict[str, Any], *, position: dict[str, Any]) -> dict[str, Any]:
    ask = _first_number(row, "ask")
    close_mid = _first_number(row, "close_mid")
    contracts = _first_number(row, "contracts_open", fallback=position.get("contracts_open"))
    if contracts is None:
        contracts = _first_number(position, "contracts")
    multiplier = _first_number(row, "multiplier", fallback=position.get("multiplier"))
    fee = _first_number(
        row,
        "estimated_close_fee",
        "buy_to_close_fee",
        "close_fee",
    )
    close_cost = None
    close_slippage = None
    if (
        ask is not None
        and ask >= 0
        and contracts is not None
        and contracts > 0
        and multiplier is not None
        and multiplier > 0
        and fee is not None
        and fee >= 0
    ):
        close_cost = ask * multiplier * contracts + fee
        if close_mid is not None and close_mid >= 0 and ask >= close_mid:
            close_slippage = (ask - close_mid) * multiplier * contracts
    return {
        "decision_ask": _rounded_number(ask, digits=6),
        "contracts": _rounded_number(contracts, digits=0),
        "multiplier": _rounded_number(multiplier, digits=6),
        "decision_close_fee": _rounded_number(fee, digits=6),
        "decision_close_slippage": _rounded_number(close_slippage, digits=6),
        "close_now_cost": _rounded_number(close_cost, digits=6),
        "fee_calc_status": text(row.get("fee_calc_status")).lower(),
        "fee_calc_basis": text(row.get("fee_calc_basis")) or None,
        "currency": text(row.get("currency") or position.get("currency")).upper() or None,
        "broker": text(position.get("broker") or row.get("broker")) or None,
        "evidence_status": "complete" if close_cost is not None else "incomplete",
    }


def _position_identity(position: dict[str, Any], *, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": text(position.get("symbol") or row.get("symbol")).upper(),
        "option_type": text(position.get("option_type") or row.get("option_type")).lower(),
        "side": text(position.get("side") or row.get("position_side") or row.get("side")).lower(),
        "expiration": text(
            position.get("expiration")
            or position.get("expiration_ymd")
            or row.get("expiration")
        ),
        "strike": _rounded_number(position.get("strike") or row.get("strike"), digits=4),
        "contract_symbol": text(
            position.get("contract_symbol")
            or position.get("option_symbol")
            or position.get("code")
            or row.get("contract_symbol")
        ).upper()
        or None,
    }


def _position_context_index(paths: list[Path]) -> dict[tuple[str, str], tuple[Path, dict[str, Any]]]:
    out: dict[tuple[str, str], tuple[Path, dict[str, Any]]] = {}
    for path in paths:
        run_id, _observed_at = _run_anchor(path)
        account = account_hint(path)
        if not account:
            raise ValueError(f"position context account cannot be resolved from path: {path}")
        payload = _read_json_object(path)
        key = (run_id, account)
        if key in out:
            raise ValueError(f"ambiguous position contexts for run/account: {run_id}/{account}")
        out[key] = (path, payload)
    return out


def _close_decision_time_index(
    paths: list[Path],
) -> dict[tuple[str, str], tuple[Path, datetime]]:
    grouped: dict[tuple[str, str], list[tuple[Path, datetime]]] = {}
    for path in paths:
        run_id, _run_started_at = _run_anchor(path)
        for row_number, event in enumerate(read_jsonl(path), start=1):
            if text(event.get("action")).lower() != "close_advice":
                continue
            if text(event.get("status")).lower() != "ok":
                continue
            event_run_id = text(event.get("run_id"))
            if event_run_id and event_run_id != run_id:
                raise ValueError(f"audit event run_id conflicts with source path: {path}:{row_number}")
            account = text(event.get("account")).lower()
            if not account:
                raise ValueError(f"close_advice audit account missing: {path}:{row_number}")
            raw_time = text(event.get("event_at_utc"))
            if not raw_time:
                raise ValueError(f"close_advice audit event_at_utc missing: {path}:{row_number}")
            grouped.setdefault((run_id, account), []).append(
                (path, _parse_utc(raw_time, label=f"close_advice audit event_at_utc ({path})"))
            )
    out: dict[tuple[str, str], tuple[Path, datetime]] = {}
    for key, values in grouped.items():
        if len(values) != 1:
            raise ValueError(
                f"close_advice audit timestamp must resolve exactly once: run_id={key[0]} account={key[1]} matches={len(values)}"
            )
        out[key] = values[0]
    return out


def _reallocation_index(paths: list[Path]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for path in paths:
        run_id, run_at = _run_anchor(path)
        source_account = account_hint(path)
        for row_number, row in enumerate(read_csv_rows(path), start=1):
            account = text(row.get("account")).lower() or source_account
            if not account:
                raise ValueError(f"reallocation account missing: {path}:{row_number}")
            item = dict(row)
            item["_source_path"] = str(path)
            item["_source_row_number"] = row_number
            item["_source_run_id"] = run_id
            item["_source_run_at_utc"] = _iso_utc(run_at)
            out.setdefault((run_id, account), []).append(item)
    return out


def _exact_position_lot(
    context: dict[str, Any],
    *,
    lot_id: str,
    account: str,
    path: Path,
) -> dict[str, Any]:
    positions = context.get("open_positions_min")
    if not isinstance(positions, list):
        raise ValueError(f"position context open_positions_min invalid: {path}")
    matches = [
        item
        for item in positions
        if isinstance(item, dict)
        and text(item.get("record_id") or item.get("position_lot_id")) == lot_id
        and (not text(item.get("account")) or text(item.get("account")).lower() == account)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"position lot match must resolve exactly once: lot_id={lot_id} matches={len(matches)} path={path}"
        )
    return matches[0]


def _exact_reallocation_row(
    rows: list[dict[str, Any]],
    *,
    lot_id: str,
    path_by_row: bool = False,
) -> dict[str, Any] | None:
    matches = [row for row in rows if text(row.get("position_lot_id")) == lot_id]
    if len(matches) > 1:
        source = text(matches[0].get("_source_path")) if path_by_row else ""
        raise ValueError(
            f"reallocation lot match is ambiguous: lot_id={lot_id} matches={len(matches)} path={source}"
        )
    return matches[0] if matches else None


def _validate_context_time(context: dict[str, Any], *, observed_at: datetime, path: Path) -> None:
    raw = text(context.get("as_of_utc"))
    if not raw:
        raise ValueError(f"position context as_of_utc missing: {path}")
    as_of = _parse_utc(raw, label=f"position context as_of_utc ({path})")
    if as_of > observed_at:
        raise ValueError(
            f"position context is newer than close decision: as_of={_iso_utc(as_of)} observed={_iso_utc(observed_at)} path={path}"
        )


def _quote_time(
    row: dict[str, Any],
    *,
    observed_at: datetime,
    close_path: Path,
    run_id: str,
) -> tuple[str, str]:
    for key in _QUOTE_TIME_FIELDS:
        raw = text(row.get(key))
        if not raw:
            continue
        quote_at = _parse_utc(raw, label=f"{key} ({close_path})")
        if quote_at > observed_at:
            raise ValueError(
                f"quote timestamp is newer than close decision: quote={_iso_utc(quote_at)} observed={_iso_utc(observed_at)} path={close_path}"
            )
        return _iso_utc(quote_at), key
    source_run_id, _source_time = _run_anchor(close_path)
    if source_run_id != run_id:
        raise ValueError(f"close advice source is outside the decision run: {close_path}")
    return _iso_utc(observed_at), "run_anchor"


def _validate_replacement_time(
    row: dict[str, Any] | None,
    *,
    observed_at: datetime,
    close_path: Path,
) -> None:
    if not isinstance(row, dict):
        return
    source_path = Path(text(row.get("_source_path")))
    close_run_id, _close_time = _run_anchor(close_path)
    replacement_run_id, replacement_file_time = _run_anchor(source_path)
    if replacement_run_id != close_run_id or replacement_file_time > observed_at:
        raise ValueError(
            f"reallocation evidence is not from the same decision run: close={close_path} reallocation={source_path}"
        )
    for key in ("replacement_run_id", "candidate_run_id", "source_run_id"):
        raw = text(row.get(key))
        if not raw:
            continue
        replacement_time = _run_id_time(raw)
        if replacement_time > observed_at:
            raise ValueError(
                f"replacement evidence is newer than close decision: field={key} value={raw} path={source_path}"
            )


def _run_anchor(path: Path) -> tuple[str, datetime]:
    for parent in path.resolve().parents:
        if _RUN_ID_RE.fullmatch(parent.name):
            return parent.name, _run_id_time(parent.name)
    raise ValueError(f"canonical UTC run ID not found in source path: {path}")


def _run_id_time(run_id: str) -> datetime:
    match = _RUN_ID_RE.fullmatch(text(run_id))
    if match is None:
        raise ValueError(f"run ID has no canonical UTC timestamp prefix: {run_id}")
    return datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def _parse_utc(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid UTC timestamp for {label}: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timezone required for {label}: {value}")
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid position context JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"position context must be a JSON object: {path}")
    return payload


def _rounded_number(value: Any, *, digits: int) -> float | int | None:
    try:
        if value is None or isinstance(value, bool) or not str(value).strip():
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        rounded = round(number, digits)
    except (TypeError, ValueError):
        return None
    return int(rounded) if digits == 0 else rounded


def _first_number(row: dict[str, Any], *keys: str, fallback: Any = None) -> float | None:
    for key in keys:
        value = _rounded_number(row.get(key), digits=12)
        if value is not None:
            return float(value)
    value = _rounded_number(fallback, digits=12)
    return float(value) if value is not None else None


def _sha256_json(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(canonical)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def accepted_candidate_snapshots(paths: list[Path], *, base: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in paths:
        strategy = strategy_hint(path)
        mode = strategy_mode(strategy)
        scope = infer_trace_scope_from_path(path)
        account = account_hint(path) or text(scope.get("account")).lower() or None
        source_path = safe_rel(path, base=base)
        for row_number, row in enumerate(read_csv_rows(path), start=1):
            candidate_rows = _combo_pair_rows(
                row,
                strategy=strategy,
                source_path=source_path,
                run_id=text(row.get("run_id") or scope.get("run_id")) or None,
                account=account,
            )
            for candidate_row in candidate_rows:
                item = snapshot_from_row(
                    candidate_row,
                    schema_version=CANDIDATE_SNAPSHOT_SCHEMA_VERSION,
                    source_kind="candidate_csv",
                    source_path=source_path,
                    source_row_number=row_number,
                    status="accepted",
                    strategy=strategy,
                    mode=mode,
                    account_hint=account,
                )
                out.append(item)
    return out


def _combo_pair_rows(
    row: dict[str, Any],
    *,
    strategy: str | None,
    source_path: str | None,
    run_id: str | None,
    account: str | None,
) -> list[dict[str, Any]]:
    put_contract = text(row.get("put_contract_symbol"))
    call_contract = text(row.get("call_contract_symbol"))
    family = text(row.get("strategy_family") or strategy).lower().replace("-", "_")
    if family != "combo_yield" or not put_contract or not call_contract:
        return [row]

    group_id = text(row.get("strategy_group_id") or row.get("group_id")) or _combo_pair_group_id(
        row,
        source_path=source_path,
        run_id=run_id,
        account=account,
        put_contract=put_contract,
        call_contract=call_contract,
    )
    contracts = first_float(row, "contracts", "contract_count", "quantity", "qty") or 1.0
    put_contracts = first_float(row, "put_contracts") or contracts
    call_contracts = first_float(row, "call_contracts") or contracts
    put_credit = first_float(row, "put_net_credit")
    call_cost = first_float(row, "call_total_cost")
    structure_mode = text(row.get("structure_mode")).lower() or "same_expiry_pair"
    common = {
        **row,
        "net_credit": None,
        "run_id": text(row.get("run_id") or run_id) or None,
        "account": text(row.get("account") or account).lower() or None,
        "strategy_family": "combo_yield",
        "strategy_profile": text(row.get("strategy_profile") or row.get("yield_enhancement_mode")) or "combo_yield",
        "strategy_group_id": group_id,
        "candidate_pair_id": text(row.get("candidate_pair_id")) or None,
        "structure_mode": structure_mode,
        "put_expiration": text(row.get("put_expiration") or row.get("expiration") or row.get("exp")) or None,
        "put_dte": first_float(row, "put_dte", "dte"),
        "call_expiration": text(row.get("call_expiration") or row.get("expiration") or row.get("exp")) or None,
        "call_dte": first_float(row, "call_dte", "dte"),
    }
    return [
        {
            **common,
            "contract_symbol": put_contract,
            "option_type": "put",
            "mode": "put",
            "side": "short",
            "leg_role": "funding_put",
            "expiration": text(row.get("put_expiration") or row.get("expiration") or row.get("exp")) or None,
            "dte": first_float(row, "put_dte", "dte"),
            "contracts": put_contracts,
            "strike": first_float(row, "put_strike"),
            "bid": first_float(row, "put_bid"),
            "ask": first_float(row, "put_ask"),
            "mid": first_float(row, "put_mid"),
            "delta": first_float(row, "put_delta"),
            "open_interest": first_float(row, "put_open_interest"),
            "volume": first_float(row, "put_volume"),
            "spread_ratio": first_float(row, "put_spread_ratio"),
            "net_income": put_credit,
            "entry_credit": put_credit,
        },
        {
            **common,
            "contract_symbol": call_contract,
            "option_type": "call",
            "mode": "call",
            "side": "long",
            "leg_role": "participation_call",
            "expiration": text(row.get("call_expiration") or row.get("expiration") or row.get("exp")) or None,
            "dte": first_float(row, "call_dte", "dte"),
            "contracts": call_contracts,
            "strike": first_float(row, "call_strike"),
            "bid": first_float(row, "call_bid"),
            "ask": first_float(row, "call_ask"),
            "mid": first_float(row, "call_mid"),
            "delta": first_float(row, "call_delta"),
            "open_interest": first_float(row, "call_open_interest"),
            "volume": first_float(row, "call_volume"),
            "spread_ratio": first_float(row, "call_spread_ratio"),
            "net_income": -abs(call_cost) if call_cost is not None else None,
            "entry_cost": abs(call_cost) if call_cost is not None else None,
        },
    ]


def _combo_pair_group_id(
    row: dict[str, Any],
    *,
    source_path: str | None,
    run_id: str | None,
    account: str | None,
    put_contract: str,
    call_contract: str,
) -> str:
    common_parts = (
        text(row.get("run_id") or run_id or source_path),
        text(row.get("account") or account).lower(),
        text(row.get("symbol") or row.get("underlying_symbol")).upper(),
    )
    if text(row.get("structure_mode")).lower() == "staggered_expiry_pair":
        parts = (
            *common_parts,
            text(row.get("candidate_pair_id")),
            text(row.get("put_expiration") or row.get("expiration") or row.get("exp")),
            text(row.get("call_expiration") or row.get("expiration") or row.get("exp")),
            put_contract.upper(),
            call_contract.upper(),
        )
    else:
        parts = (
            *common_parts,
            text(row.get("expiration") or row.get("exp")),
            put_contract.upper(),
            call_contract.upper(),
        )
    return "combo_yield|" + "|".join(parts)


def filter_decision_rows(trace_paths: list[Path], reject_log_paths: list[Path], *, base: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in trace_paths:
        scope = infer_trace_scope_from_path(path)
        for row_number, row in enumerate(read_candidate_filter_trace(path), start=1):
            item = dict(row)
            item["schema_version"] = FILTER_DECISION_SCHEMA_VERSION
            item["source_kind"] = "candidate_filter_trace"
            item["source_path"] = safe_rel(path, base=base)
            item["source_row_number"] = row_number
            item["run_id"] = text(item.get("run_id") or scope.get("run_id")) or None
            item["account"] = text(item.get("account") or scope.get("account")).lower() or None
            item["status"] = normal_status(item.get("status") or "rejected")
            item["symbol"] = text(item.get("symbol") or item.get("underlying_symbol")).upper() or None
            item["rule"] = text(item.get("rule") or item.get("reject_rule") or item.get("reject_reason")) or None
            out.append(item)
    for path in reject_log_paths:
        strategy = strategy_hint(path)
        mode = strategy_mode(strategy)
        scope = infer_trace_scope_from_path(path)
        account = account_hint(path)
        for row_number, row in enumerate(read_csv_rows(path), start=1):
            item = {
                "schema_version": FILTER_DECISION_SCHEMA_VERSION,
                "source_kind": "reject_log_csv",
                "source_path": safe_rel(path, base=base),
                "source_row_number": row_number,
                "run_id": text(row.get("run_id") or scope.get("run_id")) or None,
                "account": text(row.get("account") or account or scope.get("account")).lower() or None,
                "symbol": text(row.get("symbol") or row.get("underlying_symbol")).upper() or None,
                "contract_symbol": text(row.get("contract_symbol") or row.get("option_symbol")) or None,
                "function": text(row.get("function") or strategy) or None,
                "mode": text(row.get("mode") or row.get("option_type")).lower() or mode,
                "status": normal_status(row.get("status") or "rejected"),
                "stage": text(row.get("engine_reject_stage") or row.get("reject_stage") or row.get("stage")) or None,
                "rule": text(row.get("engine_reject_reason") or row.get("reject_rule") or row.get("reject_reason") or row.get("rule")) or None,
                "metric_value": text(row.get("metric_value")) or None,
                "threshold": text(row.get("threshold")) or None,
                "message": text(row.get("message")) or None,
            }
            item.update(build_candidate_replay_fields(row))
            out.append(item)
    return _merge_filter_decision_rows(out)


def _merge_filter_decision_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        key = _filter_decision_merge_key(row)
        if not key:
            merged.append(row)
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = row
            merged.append(row)
            continue
        _fill_missing_decision_values(existing, row)
    return merged


def _filter_decision_merge_key(row: dict[str, Any]) -> tuple[str, ...] | None:
    contract = text(row.get("contract_symbol") or row.get("option_symbol")).upper()
    symbol = text(row.get("symbol") or row.get("underlying_symbol")).upper()
    rule = text(row.get("rule") or row.get("reject_rule") or row.get("reject_reason"))
    if not rule or not (contract or symbol):
        return None
    return (
        text(row.get("run_id")),
        text(row.get("account")).lower(),
        symbol,
        contract,
        text(row.get("mode") or row.get("option_type")).lower(),
        normal_status(row.get("status") or "rejected"),
        rule,
    )


def _fill_missing_decision_values(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key in {"schema_version", "source_kind", "source_path", "source_row_number"}:
            continue
        if _decision_value_missing(target.get(key)) and not _decision_value_missing(value):
            target[key] = value


def _decision_value_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def candidate_snapshots_from_filter_decisions(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(decisions, start=1):
        status = normal_status(row.get("status") or "rejected")
        if status not in {"rejected", "post_filtered", "ranked_below"}:
            continue
        item = snapshot_from_row(
            row,
            schema_version=CANDIDATE_SNAPSHOT_SCHEMA_VERSION,
            source_kind="filter_decision",
            source_path=row.get("source_path"),
            source_row_number=row.get("source_row_number") or idx,
            status=status,
            strategy=text(row.get("function")) or None,
            mode=text(row.get("mode")).lower() or strategy_mode(text(row.get("function")) or None),
            account_hint=text(row.get("account")).lower() or None,
        )
        item["filter_stage"] = row.get("stage")
        item["filter_rule"] = row.get("rule")
        item["filter_metric_value"] = row.get("metric_value")
        item["filter_threshold"] = row.get("threshold")
        out.append(item)
    return out


def snapshot_from_row(
    row: dict[str, Any],
    *,
    schema_version: str,
    source_kind: str,
    source_path: Any,
    source_row_number: Any,
    status: str,
    strategy: str | None,
    mode: str | None,
    account_hint: str | None,
) -> dict[str, Any]:
    mode_norm = text(row.get("mode") or row.get("option_type") or mode).lower() or None
    family = _strategy_family_value(row, strategy)
    profile = _strategy_profile_value(row, strategy=strategy, family=family)
    parameter_snapshot = _parameter_snapshot(row)
    payload = {
        "schema_version": schema_version,
        "source_kind": source_kind,
        "source_path": source_path,
        "source_row_number": source_row_number,
        "status": normal_status(status),
        "strategy": strategy,
        "strategy_family": family,
        "strategy_profile": profile,
        "parameter_snapshot": parameter_snapshot or None,
        "parameter_snapshot_sha256": (
            _payload_digest(parameter_snapshot) if parameter_snapshot else None
        ),
        "parameter_snapshot_source": (
            "decision_trace_config_values" if parameter_snapshot else None
        ),
        "strategy_group_id": text(row.get("strategy_group_id") or row.get("group_id")) or None,
        "candidate_pair_id": text(row.get("candidate_pair_id")) or None,
        "structure_mode": text(row.get("structure_mode")).lower() or None,
        "leg_role": text(row.get("leg_role") or row.get("strategy_leg_role")) or None,
        "mode": mode_norm,
        "run_id": text(row.get("run_id")) or None,
        "account": text(row.get("account") or account_hint).lower() or None,
        "symbol": text(row.get("symbol") or row.get("underlying_symbol")).upper() or None,
        "contract_symbol": text(row.get("contract_symbol") or row.get("option_symbol")) or None,
        "option_type": text(row.get("option_type")).lower() or mode_norm,
        "expiration": text(row.get("expiration") or row.get("exp")) or None,
        "put_expiration": text(row.get("put_expiration")) or None,
        "put_dte": first_float(row, "put_dte"),
        "call_expiration": text(row.get("call_expiration")) or None,
        "call_dte": first_float(row, "call_dte"),
        "strike": first_float(row, "strike"),
        "side": text(row.get("side") or row.get("position_side")).lower() or None,
        "contracts": first_float(row, "contracts", "contract_count", "quantity", "qty"),
        "multiplier": first_float(row, "multiplier", "contract_multiplier"),
        "currency": text(row.get("currency")).upper() or None,
        "spot": first_float(row, "spot", "underlying_price"),
        "dte": first_float(row, "dte"),
        "delta": first_float(row, "delta", "put_delta", "call_delta"),
        "abs_delta": abs_first_float(row, "delta", "put_delta", "call_delta"),
        "iv_rv_ratio": first_float(row, "iv_rv_ratio"),
        "iv_minus_rv": first_float(row, "iv_minus_rv"),
        "premium_edge_score": first_float(row, "premium_edge_score"),
        "strike_safety_margin_pct": first_float(row, "strike_safety_margin_pct"),
        "strike_upside_margin_pct": first_float(row, "strike_upside_margin_pct"),
        "min_strike": first_float(row, "min_strike"),
        "max_strike": first_float(row, "max_strike"),
        "effective_min_strike": first_float(row, "effective_min_strike"),
        "bid": first_float(row, "bid", "option_bid"),
        "ask": first_float(row, "ask", "option_ask"),
        "mid": first_float(row, "mid", "option_mid", "mid_price"),
        "last_price": first_float(row, "last_price", "last"),
        "open_interest": first_float(row, "open_interest", "oi"),
        "volume": first_float(row, "volume", "option_volume"),
        "spread_ratio": first_float(row, "spread_ratio", "combo_spread_ratio"),
        "single_trade_concentration": first_float(row, "single_trade_concentration"),
        "event_risk_status": text(row.get("event_risk_status")) or None,
        "event_status": text(row.get("event_status")) or None,
        "event_source_status": text(row.get("event_source_status")) or None,
        "event_risk": text(row.get("event_risk")) or None,
        "has_event_before_expiry": text(row.get("has_event_before_expiry")) or None,
        "symbol_concentration_after": first_float(row, "symbol_concentration_after"),
        "portfolio_nav_cny": first_float(row, "portfolio_nav_cny", "nav_cny"),
        "assignment_notional_cny": first_float(row, "assignment_notional_cny"),
        "cash_required_cny": first_float(row, "cash_required_cny"),
        "cash_required_usd": first_float(row, "cash_required_usd"),
        "cash_free_cny": first_float(row, "cash_free_cny"),
        "cash_free_total_cny": first_float(row, "cash_free_total_cny"),
        "cash_free_usd": first_float(row, "cash_free_usd"),
        "existing_stock_value_cny_symbol": first_float(row, "existing_stock_value_cny_symbol"),
        "existing_short_put_assignment_cny_symbol": first_float(row, "existing_short_put_assignment_cny_symbol"),
        "existing_short_put_assignment_cny_total": first_float(row, "existing_short_put_assignment_cny_total"),
        "covered_notional_cny": first_float(row, "covered_notional_cny"),
        "shares_total": first_float(row, "shares_total", "shares"),
        "shares_locked": first_float(row, "shares_locked"),
        "shares_available_for_cover": first_float(row, "shares_available_for_cover"),
        "covered_contracts_available": first_float(row, "covered_contracts_available"),
        "covered_quantity": first_float(
            row,
            "covered_quantity",
            "covered_shares",
            "covered_share_quantity",
            "shares_available_for_cover",
            "covered_contracts_available",
        ),
        "cost_basis": first_float(row, "cost_basis", "underlying_cost_basis", "avg_cost", "average_cost"),
        "cost_basis_floor": first_float(row, "cost_basis_floor", "min_strike_cost_multiplier", "strike_cost_multiplier"),
        "underlying_notional_cny": first_float(row, "underlying_notional_cny"),
        "capital_at_risk_cny": first_float(row, "capital_at_risk_cny"),
        "annualized_return": first_float(
            row,
            "annualized_net_return_on_cash_basis",
            "annualized_net_premium_return",
            "annualized_net_return",
            "annualized_return",
        ),
        "net_income_cny": first_float(row, "net_income_cny", "net_credit_cny", "premium_cny"),
        "net_income": first_float(row, "net_income", "net_credit"),
        "entry_credit": first_float(row, "entry_credit"),
        "entry_cost": first_float(row, "entry_cost"),
        "put_net_credit": first_float(row, "put_net_credit"),
        "call_total_cost": first_float(row, "call_total_cost"),
        "combo_net_credit": first_float(row, "combo_net_credit"),
        "net_credit_retention": first_float(row, "net_credit_retention"),
        "call_cost_to_put_credit": first_float(row, "call_cost_to_put_credit"),
    }
    return with_decision_identity(payload)


def _parameter_snapshot(row: dict[str, Any]) -> dict[str, float]:
    raw = row.get("parameter_snapshot")
    sources = [raw if isinstance(raw, dict) else {}, row.get("config_values"), row]
    aliases = {
        "min_annualized_return": ("min_annualized_return", "min_annualized_net_return"),
        "min_iv_rv_ratio": ("min_iv_rv_ratio",),
        "min_iv_minus_rv": ("min_iv_minus_rv",),
        "min_dte": ("min_dte",),
        "max_dte": ("max_dte",),
    }
    out: dict[str, float] = {}
    for canonical, keys in aliases.items():
        for source in sources:
            if not isinstance(source, dict):
                continue
            value = first_float(source, *keys)
            if value is not None:
                out[canonical] = value
                break
    return out


def _payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _attach_parameter_snapshots(
    candidates: list[dict[str, Any]],
    filter_decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cohorts: dict[tuple[str, str, str, str], dict[str, dict[str, float]]] = {}
    for decision in filter_decisions:
        snapshot = _parameter_snapshot(decision)
        if not snapshot:
            continue
        key = _parameter_cohort_key(decision)
        cohorts.setdefault(key, {})[_payload_digest(snapshot)] = snapshot
    out: list[dict[str, Any]] = []
    for candidate in candidates:
        payload = dict(candidate)
        if isinstance(payload.get("parameter_snapshot"), dict):
            out.append(payload)
            continue
        options = cohorts.get(_parameter_cohort_key(payload), {})
        if len(options) == 1:
            digest, snapshot = next(iter(options.items()))
            payload["parameter_snapshot"] = snapshot
            payload["parameter_snapshot_sha256"] = digest
            payload["parameter_snapshot_source"] = "run_cohort_decision_trace"
        elif len(options) > 1:
            payload["parameter_snapshot_source"] = "ambiguous_run_cohort"
        out.append(payload)
    return out


def _parameter_cohort_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        text(row.get("run_id")),
        text(row.get("account")).lower(),
        text(row.get("strategy_family") or row.get("function")).lower(),
        text(row.get("strategy_profile")).lower(),
    )


def _config_value(row: dict[str, Any], *keys: str) -> Any:
    raw = row.get("config_values")
    if not isinstance(raw, dict):
        return None
    for key in keys:
        value = raw.get(key)
        if text(value):
            return value
    return None


def _strategy_family_value(row: dict[str, Any], strategy: str | None) -> str | None:
    return (
        text(
            row.get("strategy_family")
            or _config_value(row, "strategy_family", "family")
            or row.get("function")
            or strategy
        )
        or None
    )


def _strategy_profile_value(row: dict[str, Any], *, strategy: str | None, family: str | None) -> str | None:
    explicit = text(
        row.get("strategy_profile")
        or row.get("profile")
        or row.get("strategy_mode")
        or _config_value(row, "strategy_profile", "profile", "strategy")
    )
    if explicit:
        return explicit
    family_norm = text(family or row.get("function") or strategy).lower().replace("-", "_")
    if family_norm in {"sell_put", "sell_call"} and _has_short_vol_replay_fields(row):
        return "short_vol"
    return None


def _has_short_vol_replay_fields(row: dict[str, Any]) -> bool:
    return any(
        first_float(row, key) is not None
        for key in (
            "iv_rv_ratio",
            "iv_minus_rv",
            "abs_delta",
            "delta",
            "vol_edge_score",
            "delta_target_score",
        )
    )


def rank_snapshots_for_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(candidates, start=1):
        if str(row.get("status") or "") != "accepted":
            continue
        mode = str(row.get("mode") or "").strip()
        if mode not in {"put", "call"}:
            continue
        try:
            explanation = explain_candidate_rank(row, mode=mode, score_weights=CandidateScoreWeights())
        except Exception as exc:
            explanation = {"error": f"{type(exc).__name__}: {exc}"}
        out.append(
            {
                "schema_version": RANK_SNAPSHOT_SCHEMA_VERSION,
                "source_candidate_index": idx,
                "symbol": row.get("symbol"),
                "contract_symbol": row.get("contract_symbol"),
                "mode": mode,
                "rank_explanation": explanation,
            }
        )
    return out


def read_replay_rows(paths: list[Path], *, schema_version: str, base: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in paths:
        if path.suffix.lower() == ".csv":
            rows = read_csv_rows(path)
        else:
            rows = read_jsonl(path)
        for row_number, row in enumerate(rows, start=1):
            item = dict(row)
            item.setdefault("schema_version", schema_version)
            item["source_path"] = safe_rel(path, base=base)
            item["source_row_number"] = row_number
            out.append(item)
    return out


def candidate_paths_from_selection(selection: ShadowReplaySourceSelection) -> list[Path]:
    explicit = [path for path in selection.candidate_paths if path.exists()]
    if explicit:
        return unique(explicit)
    out: list[Path] = []
    for directory in source_dirs(selection):
        out.extend(
            glob_many(
                directory,
                (
                    "*sell_put_candidates*.csv",
                    "*sell_call_candidates*.csv",
                    "*combo_yield_candidates*.csv",
                    "*yield_enhancement_candidates*.csv",
                ),
            )
        )
    return unique(path for path in out if "reject_log" not in path.name.lower())


def trace_paths_from_selection(selection: ShadowReplaySourceSelection) -> list[Path]:
    explicit = [path for path in selection.trace_paths if path.exists()]
    if explicit:
        return unique(explicit)
    return unique(directory / "candidate_filter_trace.jsonl" for directory in source_dirs(selection) if (directory / "candidate_filter_trace.jsonl").exists())


def reject_log_paths_from_selection(selection: ShadowReplaySourceSelection) -> list[Path]:
    explicit = [path for path in selection.reject_log_paths if path.exists()]
    if explicit:
        return unique(explicit)
    out: list[Path] = []
    for directory in source_dirs(selection):
        out.extend(glob_many(directory, ("*candidates_reject_log*.csv", "*reject_log*.csv")))
    return unique(out)


def mark_paths_from_selection(selection: ShadowReplaySourceSelection) -> list[Path]:
    explicit = [path for path in selection.mark_paths if path.exists()]
    if explicit:
        return unique(explicit)
    out: list[Path] = []
    for directory in source_dirs(selection):
        out.extend(glob_many(directory, ("mark_path_snapshots.jsonl", "mark_path_snapshots.csv", "*mark_path*.jsonl", "*mark_path*.csv")))
    return unique(out)


def outcome_paths_from_selection(selection: ShadowReplaySourceSelection) -> list[Path]:
    explicit = [path for path in selection.outcome_paths if path.exists()]
    if explicit:
        return unique(explicit)
    out: list[Path] = []
    for directory in source_dirs(selection):
        out.extend(glob_many(directory, ("outcome_facts.jsonl", "outcome_facts.csv", "*outcome*.jsonl", "*outcome*.csv")))
    return unique(out)


def close_advice_paths_from_selection(selection: ShadowReplaySourceSelection) -> list[Path]:
    if selection.close_advice_paths:
        return _required_explicit_paths(selection.close_advice_paths, label="close advice")
    return unique(
        directory / "close_advice.csv"
        for directory in source_dirs(selection)
        if (directory / "close_advice.csv").is_file()
    )


def position_context_paths_from_selection(
    selection: ShadowReplaySourceSelection,
    *,
    close_paths: list[Path],
) -> list[Path]:
    if selection.position_context_paths:
        return _required_explicit_paths(selection.position_context_paths, label="position context")
    inferred = [
        path.parent / "state" / "option_positions_context.json"
        for path in close_paths
        if (path.parent / "state" / "option_positions_context.json").is_file()
    ]
    return unique(inferred)


def reallocation_paths_from_selection(
    selection: ShadowReplaySourceSelection,
    *,
    close_paths: list[Path],
) -> list[Path]:
    if selection.reallocation_paths:
        return _required_explicit_paths(selection.reallocation_paths, label="reallocation")
    inferred = [
        path.parent / "close_advice_reallocation_shadow.csv"
        for path in close_paths
        if (path.parent / "close_advice_reallocation_shadow.csv").is_file()
    ]
    return unique(inferred)


def run_audit_paths_from_selection(
    selection: ShadowReplaySourceSelection,
    *,
    close_paths: list[Path],
) -> list[Path]:
    if selection.run_audit_paths:
        return _required_explicit_paths(selection.run_audit_paths, label="run audit")
    inferred: list[Path] = []
    for path in close_paths:
        run_id, _observed_at = _run_anchor(path)
        run_dir = next(parent for parent in path.resolve().parents if parent.name == run_id)
        audit_path = run_dir / "state" / "audit_events.jsonl"
        if audit_path.is_file():
            inferred.append(audit_path)
    return unique(inferred)


def _required_explicit_paths(paths: tuple[Path, ...], *, label: str) -> list[Path]:
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f"explicit {label} source does not exist: {missing[0]}")
    return unique(paths)


def source_dirs(selection: ShadowReplaySourceSelection) -> list[Path]:
    dirs: list[Path] = []
    run_dir = selection.run_dir
    if run_dir is None and selection.run_id:
        runs_root = selection.runs_root or (selection.repo_root / "output_runs").resolve()
        run_dir = (runs_root / selection.run_id).resolve()
    for root in (run_dir, selection.report_dir):
        if root is None:
            continue
        dirs.append(root.resolve())
        accounts_dir = root / "accounts"
        if accounts_dir.exists() and accounts_dir.is_dir():
            dirs.extend(path.resolve() for path in accounts_dir.iterdir() if path.is_dir())
    if not dirs:
        dirs.append((selection.repo_root / "output_shared" / "reports").resolve())
    return unique(dirs)


def latest_shadow_replay_run_dir(*, repo_root: Path, runs_root: Path | None = None) -> tuple[Path | None, dict[str, Any]]:
    root = (runs_root or (repo_root / "output_runs")).resolve()
    searched_count = 0
    skipped_without_evidence_count = 0
    if not root.exists() or not root.is_dir():
        return None, {
            "requested": True,
            "found": False,
            "source": "runs_root_mtime",
            "runs_root": safe_rel(root, base=repo_root),
            "path": None,
            "run_id": None,
            "searched_count": 0,
            "skipped_without_evidence_count": 0,
        }
    run_dirs = sorted(
        [item.resolve() for item in root.iterdir() if item.is_dir()],
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    )
    for run_dir in run_dirs:
        searched_count += 1
        probe = ShadowReplaySourceSelection(repo_root=repo_root, run_dir=run_dir, runs_root=root)
        candidate_count = len(candidate_paths_from_selection(probe))
        trace_count = len(trace_paths_from_selection(probe))
        reject_log_count = len(reject_log_paths_from_selection(probe))
        if candidate_count or trace_count or reject_log_count:
            return run_dir, {
                "requested": True,
                "found": True,
                "source": "runs_root_mtime",
                "runs_root": safe_rel(root, base=repo_root),
                "path": safe_rel(run_dir, base=repo_root),
                "run_id": run_dir.name,
                "searched_count": searched_count,
                "skipped_without_evidence_count": skipped_without_evidence_count,
                "candidate_path_count": candidate_count,
                "trace_path_count": trace_count,
                "reject_log_path_count": reject_log_count,
            }
        skipped_without_evidence_count += 1
    return None, {
        "requested": True,
        "found": False,
        "source": "runs_root_mtime",
        "runs_root": safe_rel(root, base=repo_root),
        "path": None,
        "run_id": None,
        "searched_count": searched_count,
        "skipped_without_evidence_count": skipped_without_evidence_count,
    }


def latest_close_decision_run_dir(
    *,
    repo_root: Path,
    runs_root: Path | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Return the latest run containing at least one Close Advice data row."""

    root = (runs_root or (repo_root / "output_runs")).resolve()
    searched_count = 0
    skipped_without_close_count = 0
    skipped_empty_count = 0
    if not root.exists() or not root.is_dir():
        return None, {
            "requested": True,
            "found": False,
            "source": "runs_root_mtime",
            "runs_root": safe_rel(root, base=repo_root),
            "path": None,
            "run_id": None,
            "searched_count": 0,
            "skipped_without_close_count": 0,
            "skipped_empty_count": 0,
        }
    run_dirs = sorted(
        [item.resolve() for item in root.iterdir() if item.is_dir()],
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    )
    for run_dir in run_dirs:
        searched_count += 1
        probe = ShadowReplaySourceSelection(repo_root=repo_root, run_dir=run_dir, runs_root=root)
        close_paths = close_advice_paths_from_selection(probe)
        if not close_paths:
            skipped_without_close_count += 1
            continue
        close_row_count = sum(len(read_csv_rows(path)) for path in close_paths)
        if close_row_count <= 0:
            skipped_empty_count += 1
            continue
        return run_dir, {
            "requested": True,
            "found": True,
            "source": "runs_root_mtime",
            "runs_root": safe_rel(root, base=repo_root),
            "path": safe_rel(run_dir, base=repo_root),
            "run_id": run_dir.name,
            "searched_count": searched_count,
            "skipped_without_close_count": skipped_without_close_count,
            "skipped_empty_count": skipped_empty_count,
            "close_path_count": len(close_paths),
            "close_row_count": close_row_count,
        }
    return None, {
        "requested": True,
        "found": False,
        "source": "runs_root_mtime",
        "runs_root": safe_rel(root, base=repo_root),
        "path": None,
        "run_id": None,
        "searched_count": searched_count,
        "skipped_without_close_count": skipped_without_close_count,
        "skipped_empty_count": skipped_empty_count,
    }


def dedupe_snapshots(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        key = (
            row.get("source_kind"),
            row.get("source_path"),
            row.get("source_row_number"),
            row.get("status"),
            row.get("symbol"),
            row.get("contract_symbol"),
            row.get("filter_rule"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out
