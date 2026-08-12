from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from domain.domain.close_advice import (
    DECISION_EVIDENCE_COMPLETE,
    DECISION_EVIDENCE_NOT_EVALUABLE,
    RECOMMENDATION_CLOSE,
    RECOMMENDATION_HOLD,
    RECOMMENDATION_NOT_EVALUABLE,
    STRICT_CLOSE_POLICY_VERSION,
)
from src.application.candidate_filter_trace import (
    infer_trace_scope_from_path,
    read_candidate_filter_trace,
)
from src.application.candidate_evidence_history import (
    AccountCandidateEvidence,
    CANDIDATE_EVIDENCE_CLASSIFICATION_SCHEMA,
    CANDIDATE_EVIDENCE_STATES,
    NOT_SCANNED,
    SUPPORTED,
    load_run_candidate_evidence,
)
from src.application.combo_yield_candidate_snapshot import (
    COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    project_combo_yield_candidates,
    project_combo_yield_funding_put_decisions,
    project_combo_yield_pair_diagnostics,
    project_combo_yield_rank_evidence,
)
from src.application.cc_lp_candidate_snapshot import (
    CC_LP_CANDIDATE_SNAPSHOT_FILE,
    project_cc_lp_candidates,
)
from src.application.opening_candidate_snapshot import (
    OPENING_CANDIDATE_SNAPSHOT_FILE,
    ranked_opening_candidates,
    ranked_opening_candidate_decisions,
    rejected_opening_candidate_decisions,
)
from src.application.close_advice_report_manifest import (
    read_close_advice_report_snapshot,
)
from src.application.shadow_replay.candidate_analysis import analyze_rows
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
    trace_paths: tuple[Path, ...] = ()
    mark_paths: tuple[Path, ...] = ()
    outcome_paths: tuple[Path, ...] = ()
    close_advice_paths: tuple[Path, ...] = ()
    position_context_paths: tuple[Path, ...] = ()
    run_audit_paths: tuple[Path, ...] = ()


def build_shadow_replay_dataset(
    *,
    repo_root: Path,
    run_id: str | None = None,
    runs_root: str | Path | None = None,
    run_dir: str | Path | None = None,
    report_dir: str | Path | None = None,
    trace_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    mark_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    outcome_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    close_advice_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    position_context_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
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
    elif run_dir_path is not None:
        run_id_text = run_id_text or run_dir_path.name
    selection = ShadowReplaySourceSelection(
        repo_root=base,
        run_id=run_id_text,
        runs_root=runs_root_path,
        run_dir=run_dir_path,
        report_dir=resolve_optional(report_dir, base=base),
        trace_paths=tuple(resolve_many(trace_paths, base=base)),
        mark_paths=tuple(resolve_many(mark_paths, base=base)),
        outcome_paths=tuple(resolve_many(outcome_paths, base=base)),
        close_advice_paths=tuple(resolve_many(close_advice_paths, base=base)),
        position_context_paths=tuple(resolve_many(position_context_paths, base=base)),
        run_audit_paths=tuple(resolve_many(run_audit_paths, base=base)),
    )
    candidate_observations = candidate_replay_observations_from_selection(
        selection,
    )
    coverage = candidate_observations["coverage"]
    close_facet_requested = bool(
        include_close_decisions
        or close_advice_paths
        or position_context_paths
        or run_audit_paths
    )
    contributing_accounts = sum(
        int(coverage.get("counts", {}).get(status) or 0)
        for status in ("supported", "supported_limited_legacy_snapshot")
    )
    if contributing_accounts <= 0 and not close_facet_requested:
        statuses = sorted(
            status
            for status, count in (coverage.get("counts") or {}).items()
            if int(count or 0) > 0
        )
        detail = ",".join(statuses) or "no_account_candidate_evidence"
        raise ValueError(f"candidate_evidence_unsupported:{detail}")
    resolved_traces = candidate_observations["trace_paths"]
    resolved_marks = mark_paths_from_selection(selection)
    resolved_outcomes = outcome_paths_from_selection(selection)
    resolved_close_advice: list[Path] = []
    resolved_position_contexts: list[Path] = []
    resolved_run_audits: list[Path] = []
    close_decision_episodes: list[dict[str, Any]] = []
    if close_facet_requested:
        resolved_close_advice = close_advice_paths_from_selection(selection)
        resolved_position_contexts = position_context_paths_from_selection(
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
            run_audit_paths=resolved_run_audits,
            base=base,
        )

    candidate_rows = candidate_observations["candidate_snapshots"]
    filter_decisions = candidate_observations["filter_decisions"]
    candidate_snapshots = dedupe_snapshots(_attach_parameter_snapshots(candidate_rows, filter_decisions))
    rank_snapshots = candidate_observations["rank_snapshots"]
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
            "candidate_evidence_coverage": coverage,
            "trace_paths": [safe_rel(path, base=base) for path in resolved_traces],
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
                "position_context_paths": [safe_rel(path, base=base) for path in resolved_position_contexts],
                "run_audit_paths": [safe_rel(path, base=base) for path in resolved_run_audits],
            }
        )
        manifest["files"].update({name: str((target / name).resolve()) for name in OPTIONAL_CLOSE_DATASET_FILES})
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
    "net_capture_ratio",
    "remaining_term_ratio",
    "spread_ratio",
    "close_cost_ratio",
)
_MATERIAL_MONEY_FIELDS = (
    "close_mid",
    "bid",
    "ask",
    "opening_net_credit",
    "all_in_close_cost",
    "estimated_close_fee",
    "estimated_pnl_if_close_net",
)


def capture_close_decision_episodes(
    *,
    close_paths: list[Path],
    position_context_paths: list[Path],
    run_audit_paths: list[Path],
    base: Path,
) -> list[dict[str, Any]]:
    """Capture immutable, point-in-time close observations as replay episodes."""

    if not close_paths:
        raise ValueError("close decision facet requested but no close_advice.csv was found")
    contexts = _position_context_index(position_context_paths)
    decision_times = _close_decision_time_index(run_audit_paths)
    observations: list[dict[str, Any]] = []
    observed_lots: set[tuple[str, str, str]] = set()
    for close_path in close_paths:
        run_id, run_started_at = _run_anchor(close_path)
        source_account = account_hint(close_path)
        close_rows, report_validation = _validated_close_report_rows(
            close_path,
            expected_run_id=run_id,
        )
        report_accounts = {text(value).lower() for value in report_validation.get("accounts") or [] if text(value)}
        report_context_sha256 = text(report_validation.get("context_sha256")).lower()
        report_snapshot_sha256 = text(report_validation.get("required_data_snapshot_manifest_sha256")).lower()
        report_plan_sha256 = text(report_validation.get("close_advice_required_data_plan_sha256")).lower()
        for row_number, source_row in enumerate(close_rows, start=1):
            row = dict(source_row)
            account = text(row.get("account")).lower() or source_account
            if not account:
                raise ValueError(f"close advice account missing: {close_path}:{row_number}")
            if source_account and account != source_account:
                raise ValueError(f"close advice account conflicts with source directory: {close_path}:{row_number}")
            if account not in report_accounts:
                raise ValueError(f"close advice account conflicts with report manifest: {close_path}:{row_number}")
            if text(row.get("quote_mode")).lower() != "frozen_snapshot":
                raise ValueError(f"close advice row is not bound to frozen inputs: {close_path}:{row_number}")
            for field, expected_digest in (
                (
                    "required_data_snapshot_manifest_sha256",
                    report_snapshot_sha256,
                ),
                (
                    "close_advice_required_data_plan_sha256",
                    report_plan_sha256,
                ),
            ):
                if text(row.get(field)).lower() != expected_digest:
                    raise ValueError(
                        f"close advice row conflicts with report manifest: {close_path}:{row_number} field={field}"
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
                raise ValueError(f"close_advice audit timestamp precedes run start: run_id={run_id} account={account}")
            context_entry = contexts.get((run_id, account))
            if context_entry is None:
                raise ValueError(f"position context missing for run/account: run_id={run_id} account={account}")
            context_path, context = context_entry
            if _sha256_json(context) != report_context_sha256:
                raise ValueError(
                    f"close advice position context conflicts with report manifest: run_id={run_id} account={account}"
                )
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
            observations.append(
                _close_episode_observation(
                    row=row,
                    position=position,
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


def _close_report_rows_from_snapshot(
    csv_bytes: Any,
    *,
    path: Path,
) -> list[dict[str, Any]]:
    if not isinstance(csv_bytes, bytes):
        raise ValueError(f"close advice report bytes are unavailable: {path}")
    try:
        decoded = csv_bytes.decode("utf-8-sig")
        return [dict(row) for row in csv.DictReader(StringIO(decoded))]
    except (UnicodeError, csv.Error) as exc:
        raise ValueError(f"close advice report CSV is invalid: {path}") from exc


def _validated_close_report_rows(
    close_path: Path,
    *,
    expected_run_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_account = account_hint(close_path)
    report_snapshot = read_close_advice_report_snapshot(
        csv_path=close_path,
        account=source_account or None,
        expected_run_id=expected_run_id,
        expected_quote_mode="frozen_snapshot",
    )
    report_validation = report_snapshot["validation"]
    if not report_validation.get("ok"):
        raise ValueError(
            "close advice report integrity validation failed: "
            f"{close_path} reason="
            f"{report_validation.get('reason') or 'unknown'}"
        )
    close_rows = _close_report_rows_from_snapshot(
        report_snapshot.get("csv_bytes"),
        path=close_path,
    )
    report_row_count = report_validation.get("row_count")
    if (
        isinstance(report_row_count, bool)
        or not isinstance(report_row_count, int)
        or report_row_count != len(close_rows)
    ):
        raise ValueError(
            f"close advice report row count mismatch: {close_path} manifest={report_row_count} actual={len(close_rows)}"
        )
    for field in (
        "context_sha256",
        "required_data_snapshot_manifest_sha256",
        "close_advice_required_data_plan_sha256",
    ):
        if not _is_sha256(report_validation.get(field)):
            raise ValueError(f"close advice report manifest binding is invalid: {close_path} field={field}")
    return close_rows, report_validation


def _is_sha256(value: Any) -> bool:
    digest = text(value).lower()
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


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
    facts = _normalized_close_decision_facts(row)
    formal = _formal_policy_result(row, path=close_path, row_number=source_row_number)
    decision_economics = _decision_economics(row, position=position)
    material_facts = {
        "normalized_decision_facts": facts,
        "formal_policy_result": formal,
        "economic_buckets": _material_economic_buckets(row),
        "threshold_inputs": _threshold_inputs(row),
        "decision_economics": decision_economics,
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
        "normalized_decision_facts": facts,
        "formal_policy_result": formal,
        "material_economic_buckets": material_facts["economic_buckets"],
        "threshold_inputs": material_facts["threshold_inputs"],
        "decision_economics": decision_economics,
        "position_identity": _position_identity(position, row=row),
        "source": {
            "close_advice_path": safe_rel(close_path, base=base),
            "position_context_path": safe_rel(context_path, base=base),
            "run_audit_path": safe_rel(audit_path, base=base),
            "row_number": source_row_number,
        },
    }


def _normalized_close_decision_facts(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "recommendation_state": text(row.get("recommendation_state")).lower(),
        "decision_evidence_status": text(row.get("decision_evidence_status")).lower(),
        "strategy_family": text(row.get("strategy_family")).lower(),
        "strategy_profile": text(row.get("strategy_profile")).lower(),
        "option_type": text(row.get("option_type")).lower(),
        "is_otm": _optional_bool(row.get("is_otm")),
        "dte": _rounded_number(row.get("dte"), digits=0),
        "original_dte": _rounded_number(row.get("original_dte"), digits=0),
        "remaining_term_ratio": _rounded_number(row.get("remaining_term_ratio"), digits=12),
        "net_capture_ratio": _rounded_number(row.get("net_capture_ratio"), digits=12),
        "close_cost_ratio": _rounded_number(row.get("close_cost_ratio"), digits=12),
        "spread_ratio": _rounded_number(row.get("spread_ratio"), digits=12),
    }


def _formal_policy_result(row: dict[str, Any], *, path: Path, row_number: int) -> dict[str, Any]:
    required = (
        "policy_version",
        "recommendation_state",
        "decision_basis",
        "decision_evidence_status",
        "evaluation_status",
    )
    missing = [key for key in required if not text(row.get(key))]
    if missing:
        joined = ",".join(missing)
        raise ValueError(f"formal close policy fields missing ({joined}): {path}:{row_number}")
    policy_version = text(row.get("policy_version"))
    recommendation = text(row.get("recommendation_state")).lower()
    evidence_status = text(row.get("decision_evidence_status")).lower()
    evaluation_status = text(row.get("evaluation_status")).lower()
    if policy_version != STRICT_CLOSE_POLICY_VERSION:
        raise ValueError(f"unsupported close policy version: {path}:{row_number} policy_version={policy_version}")
    if recommendation not in {
        RECOMMENDATION_CLOSE,
        RECOMMENDATION_HOLD,
        RECOMMENDATION_NOT_EVALUABLE,
    }:
        raise ValueError(
            f"invalid strict close recommendation: {path}:{row_number} recommendation_state={recommendation}"
        )
    expected_evidence_status = (
        DECISION_EVIDENCE_NOT_EVALUABLE
        if recommendation == RECOMMENDATION_NOT_EVALUABLE
        else DECISION_EVIDENCE_COMPLETE
    )
    if evidence_status != expected_evidence_status:
        raise ValueError(
            "strict close evidence status mismatch: "
            f"{path}:{row_number} recommendation_state={recommendation} "
            f"decision_evidence_status={evidence_status}"
        )
    if recommendation in {RECOMMENDATION_CLOSE, RECOMMENDATION_HOLD} and evaluation_status != "priced":
        raise ValueError(
            "strict close recommendation is not priced: "
            f"{path}:{row_number} recommendation_state={recommendation} "
            f"evaluation_status={evaluation_status}"
        )
    if recommendation == RECOMMENDATION_NOT_EVALUABLE and evaluation_status == "priced":
        raise ValueError(
            "strict not-evaluable recommendation is marked priced: "
            f"{path}:{row_number} evaluation_status={evaluation_status}"
        )
    return {
        "policy_version": policy_version,
        "recommendation_state": recommendation,
        "decision_basis": [token.strip() for token in text(row.get("decision_basis")).split(";") if token.strip()],
        "decision_evidence_status": evidence_status,
    }


def _material_economic_buckets(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "recommendation_state": text(row.get("recommendation_state")).lower(),
        "evaluation_status": text(row.get("evaluation_status")).lower(),
        "fee_calc_status": text(row.get("fee_calc_status")).lower(),
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
        "original_dte": _rounded_number(row.get("original_dte"), digits=0),
        "remaining_term_ratio": _rounded_number(row.get("remaining_term_ratio"), digits=12),
        "net_capture_ratio": _rounded_number(row.get("net_capture_ratio"), digits=12),
        "close_cost_ratio": _rounded_number(row.get("close_cost_ratio"), digits=12),
        "spread_ratio": _rounded_number(row.get("spread_ratio"), digits=12),
        "is_otm": _optional_bool(row.get("is_otm")),
    }


def _decision_economics(row: dict[str, Any], *, position: dict[str, Any]) -> dict[str, Any]:
    ask = _first_number(row, "ask")
    close_mid = _first_number(row, "close_mid")
    contracts = _first_number(row, "contracts_open", fallback=position.get("contracts_open"))
    if contracts is None:
        contracts = _first_number(position, "contracts")
    multiplier = _first_number(row, "multiplier", fallback=position.get("multiplier"))
    fee = _first_number(row, "estimated_close_fee")
    open_fee = _first_number(row, "estimated_open_fee")
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
        "decision_open_fee": _rounded_number(open_fee, digits=6),
        "decision_close_fee": _rounded_number(fee, digits=6),
        "decision_close_slippage": _rounded_number(close_slippage, digits=6),
        "close_now_cost": _rounded_number(close_cost, digits=6),
        "opening_net_credit": _rounded_number(row.get("opening_net_credit"), digits=6),
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
        "expiration": text(position.get("expiration") or position.get("expiration_ymd") or row.get("expiration")),
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


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    token = text(value).lower()
    if token in {"true", "1", "yes"}:
        return True
    if token in {"false", "0", "no"}:
        return False
    return None


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


def candidate_evidence_from_selection(
    selection: ShadowReplaySourceSelection,
) -> list[AccountCandidateEvidence]:
    run_dir = selection.run_dir
    run_id = text(selection.run_id) or None
    runs_root = selection.runs_root
    if run_dir is None and run_id:
        runs_root = runs_root or (selection.repo_root / "output_runs").resolve()
        run_dir = (runs_root / run_id).resolve()
    elif run_dir is not None:
        run_dir = run_dir.resolve()
        run_id = run_id or run_dir.name
        runs_root = runs_root or run_dir.parent
    if run_dir is None or not run_id:
        inferred = _canonical_run_dir_from_report(selection.report_dir)
        if inferred is None:
            return []
        run_dir = inferred
        run_id = inferred.name
        runs_root = inferred.parent
    if runs_root is None or Path(runs_root).resolve().name != "output_runs":
        raise ValueError("Shadow Replay candidate evidence requires canonical output_runs")
    return load_run_candidate_evidence(
        base=selection.repo_root,
        run_id=run_id,
        runs_root=Path(runs_root).resolve(),
    )


def _canonical_run_dir_from_report(report_dir: Path | None) -> Path | None:
    if report_dir is None:
        return None
    resolved = report_dir.resolve()
    for parent in (resolved, *resolved.parents):
        if parent.parent.name == "output_runs":
            return parent
    return None


def candidate_evidence_coverage(
    evidence: list[AccountCandidateEvidence],
) -> dict[str, Any]:
    counts = {
        state: sum(item.classification["status"] == state for item in evidence)
        for state in sorted(CANDIDATE_EVIDENCE_STATES)
    }
    strict = bool(evidence) and all(item.classification["status"] == SUPPORTED for item in evidence)
    return {
        "schema_version": CANDIDATE_EVIDENCE_CLASSIFICATION_SCHEMA,
        "accounts": [dict(item.classification) for item in evidence],
        "counts": counts,
        "strict_replay_authority": strict,
        "reason_code": (
            "all_accounts_manifest_supported"
            if strict
            else "candidate_evidence_coverage_incomplete"
            if evidence
            else "run_has_no_account_candidate_evidence"
        ),
    }


def candidate_replay_observations_from_selection(
    selection: ShadowReplaySourceSelection,
) -> dict[str, Any]:
    base = selection.repo_root.resolve()
    evidence = candidate_evidence_from_selection(selection)
    trace_paths = trace_paths_from_selection(selection)
    candidates, sealed_decisions = candidate_snapshot_rows_from_evidence(
        evidence,
        base=base,
    )
    rank_snapshots = rank_snapshot_rows_from_evidence(evidence, base=base)
    trace_decisions = filter_decision_rows(trace_paths, base=base)
    return {
        "account_evidence": evidence,
        "candidate_snapshots": candidates,
        "rank_snapshots": rank_snapshots,
        "filter_decisions": _merge_filter_decision_rows(
            sealed_decisions + trace_decisions
        ),
        "trace_paths": trace_paths,
        "coverage": candidate_evidence_coverage(evidence),
    }


def candidate_snapshot_rows_from_evidence(
    evidence: list[AccountCandidateEvidence],
    *,
    base: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for item in evidence:
        if not item.contributes_evidence:
            continue
        for owner, snapshot in item.owners.items():
            source_path = _owner_snapshot_source_path(
                item,
                owner=owner,
                base=base,
            )
            if owner == "opening":
                owner_candidates, owner_decisions = _opening_snapshot_rows(
                    snapshot,
                    source_path=source_path,
                )
            elif owner == "sp_lc":
                owner_candidates, owner_decisions = _sp_lc_snapshot_rows(
                    snapshot,
                    source_path=source_path,
                )
            elif owner == "cc_lp":
                owner_candidates, owner_decisions = _cc_lp_snapshot_rows(
                    snapshot,
                    source_path=source_path,
                )
            else:
                raise ValueError(f"unsupported candidate evidence owner: {owner}")
            candidates.extend(owner_candidates)
            decisions.extend(owner_decisions)
    return dedupe_snapshots(candidates), _merge_filter_decision_rows(decisions)


def rank_snapshot_rows_from_evidence(
    evidence: list[AccountCandidateEvidence],
    *,
    base: Path,
) -> list[dict[str, Any]]:
    """Project rank facts exactly as sealed by each candidate owner."""

    rows: list[dict[str, Any]] = []
    for item in evidence:
        if not item.contributes_evidence:
            continue
        for owner, snapshot in item.owners.items():
            source_path = _owner_snapshot_source_path(item, owner=owner, base=base)
            if owner == "opening":
                rows.extend(
                    _opening_rank_snapshot_rows(snapshot, source_path=source_path)
                )
            elif owner == "sp_lc":
                rows.extend(
                    _sp_lc_rank_snapshot_rows(snapshot, source_path=source_path)
                )
            elif owner == "cc_lp":
                rows.extend(
                    _cc_lp_rank_snapshot_rows(snapshot, source_path=source_path)
                )
            else:
                raise ValueError(f"unsupported candidate evidence owner: {owner}")
    return rows


def _opening_rank_snapshot_rows(
    snapshot: dict[str, Any],
    *,
    source_path: str,
) -> list[dict[str, Any]]:
    run_id = text(snapshot.get("run_id")) or None
    account = text(snapshot.get("account")).lower() or None
    rows: list[dict[str, Any]] = []
    for row_number, ranked in enumerate(ranked_opening_candidates(snapshot), start=1):
        facts = dict(ranked.get("facts") or {})
        mode = text(ranked.get("strategy_mode")).lower()
        rows.append(
            {
                "schema_version": RANK_SNAPSHOT_SCHEMA_VERSION,
                "source_kind": "sealed_candidate_snapshot",
                "source_path": source_path,
                "source_row_number": row_number,
                "run_id": run_id,
                "account": account,
                "strategy": _opening_strategy(mode),
                "strategy_family": _opening_strategy_family(mode),
                "strategy_profile": text(facts.get("strategy_profile"))
                or "insurance_underwriting",
                "candidate_id": ranked.get("candidate_id"),
                "decision_hash": ranked.get("decision_hash"),
                "symbol": text(facts.get("symbol")).upper() or None,
                "contract_symbol": text(facts.get("contract_symbol")) or None,
                "mode": mode,
                "rank": ranked.get("rank"),
                "rank_explanation": dict(ranked.get("ranking") or {}),
                "sealed_facts": facts,
            }
        )
    return rows


def _sp_lc_rank_snapshot_rows(
    snapshot: dict[str, Any],
    *,
    source_path: str,
) -> list[dict[str, Any]]:
    run_id = text(snapshot.get("run_id")) or None
    account = text(snapshot.get("account")).lower() or None
    rows: list[dict[str, Any]] = []
    for row_number, record in enumerate(
        project_combo_yield_rank_evidence(snapshot), start=1
    ):
        rank_facts = {
            key: record.get(key)
            for key in (
                "baseline_rank",
                "shadow_rank",
                "baseline_selected",
                "shadow_selected",
                "rank_changed",
            )
        }
        rows.append(
            {
                "schema_version": RANK_SNAPSHOT_SCHEMA_VERSION,
                "source_kind": "sealed_candidate_snapshot",
                "source_path": source_path,
                "source_row_number": row_number,
                "run_id": run_id,
                "account": account,
                "strategy": "combo_yield",
                "strategy_family": "combo_yield",
                "strategy_profile": "combo_yield",
                "candidate_pair_id": record.get("candidate_pair_id"),
                "symbol": text(record.get("symbol")).upper() or None,
                "put_contract_symbol": record.get("put_contract_symbol"),
                "call_contract_symbol": record.get("call_contract_symbol"),
                "rank": record.get("baseline_rank"),
                "rank_explanation": rank_facts,
            }
        )
    return rows


def _cc_lp_rank_snapshot_rows(
    snapshot: dict[str, Any],
    *,
    source_path: str,
) -> list[dict[str, Any]]:
    run_id = text(snapshot.get("run_id")) or None
    account = text(snapshot.get("account")).lower() or None
    rows: list[dict[str, Any]] = []
    for row_number, pair in enumerate(project_cc_lp_candidates(snapshot), start=1):
        rows.append(
            {
                "schema_version": RANK_SNAPSHOT_SCHEMA_VERSION,
                "source_kind": "sealed_candidate_snapshot",
                "source_path": source_path,
                "source_row_number": row_number,
                "run_id": run_id,
                "account": account,
                "strategy": "combo_yield",
                "strategy_family": "combo_yield",
                "strategy_profile": text(pair.get("strategy_profile"))
                or "cc_lp_funding_call",
                "candidate_pair_id": pair.get("candidate_pair_id"),
                "symbol": text(pair.get("symbol")).upper() or None,
                "put_contract_symbol": pair.get("put_contract_symbol"),
                "call_contract_symbol": pair.get("call_contract_symbol"),
                "rank": row_number,
                "rank_explanation": {"sealed_pair_order": row_number},
                "sealed_facts": dict(pair),
            }
        )
    return rows


def _owner_snapshot_source_path(
    evidence: AccountCandidateEvidence,
    *,
    owner: str,
    base: Path,
) -> str:
    filenames = {
        "opening": OPENING_CANDIDATE_SNAPSHOT_FILE,
        "sp_lc": COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
        "cc_lp": CC_LP_CANDIDATE_SNAPSHOT_FILE,
    }
    return text(safe_rel(evidence.account_dir / "state" / filenames[owner], base=base))


def _opening_snapshot_rows(
    snapshot: dict[str, Any],
    *,
    source_path: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    run_id = text(snapshot.get("run_id")) or None
    account = text(snapshot.get("account")).lower() or None
    candidates: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for row_number, decision in enumerate(
        ranked_opening_candidate_decisions(snapshot),
        start=1,
    ):
        mode = text(decision.get("strategy_mode")).lower()
        row = {
            **dict(decision.get("normalized_input") or {}),
            "run_id": run_id,
            "account": account,
            "strategy_family": _opening_strategy_family(mode),
            "strategy_profile": text(
                (decision.get("normalized_input") or {}).get("strategy_profile")
            )
            or "insurance_underwriting",
            "candidate_id": decision.get("candidate_id"),
            "decision_hash": decision.get("decision_hash"),
            "opening_snapshot_rank": decision.get("opening_snapshot_rank"),
        }
        candidates.append(
            snapshot_from_row(
                row,
                schema_version=CANDIDATE_SNAPSHOT_SCHEMA_VERSION,
                source_kind="sealed_candidate_snapshot",
                source_path=source_path,
                source_row_number=row_number,
                status="accepted",
                strategy=_opening_strategy(mode),
                mode=mode,
                account_hint=account,
            )
        )
    for decision_number, decision in enumerate(
        rejected_opening_candidate_decisions(snapshot),
        start=1,
    ):
        mode = text(decision.get("strategy_mode")).lower()
        normalized = dict(decision.get("normalized_input") or {})
        opening = dict(decision.get("opening_decision") or {})
        strategy_profile = text(normalized.get("strategy_profile")) or (
            "insurance_underwriting"
        )
        rejects = [dict(row) for row in opening.get("rejects") or []]
        for reject_number, reject in enumerate(rejects, start=1):
            decision_row = {
                **normalized,
                "schema_version": FILTER_DECISION_SCHEMA_VERSION,
                "source_kind": "sealed_candidate_snapshot",
                "source_path": source_path,
                "source_row_number": f"{decision_number}.{reject_number}",
                "run_id": run_id,
                "account": account,
                "strategy_family": _opening_strategy_family(mode),
                "strategy_profile": strategy_profile,
                "function": _opening_strategy(mode),
                "mode": mode,
                "status": "rejected",
                "stage": reject.get("stage"),
                "rule": reject.get("reason"),
                "metric_value": reject.get("metric_value"),
                "threshold": reject.get("threshold"),
                "message": reject.get("message"),
                "candidate_id": decision.get("candidate_id"),
                "decision_hash": decision.get("decision_hash"),
            }
            decisions.append(decision_row)
        candidates.append(
            _rejected_candidate_snapshot(
                {
                    **normalized,
                    "run_id": run_id,
                    "account": account,
                    "strategy_family": _opening_strategy_family(mode),
                    "strategy_profile": strategy_profile,
                    "candidate_id": decision.get("candidate_id"),
                    "decision_hash": decision.get("decision_hash"),
                },
                source_path=source_path,
                source_row_number=decision_number,
                strategy=_opening_strategy(mode),
                mode=mode,
                account=account,
                rejects=rejects,
            )
        )
    return candidates, decisions


def _opening_strategy(mode: str) -> str:
    if mode == "put":
        return "sell_put"
    if mode == "call":
        return "sell_call"
    raise ValueError(f"unsupported opening strategy mode: {mode}")


def _opening_strategy_family(mode: str) -> str:
    return "sell_put" if mode == "put" else "covered_call"


def _rejected_candidate_snapshot(
    row: dict[str, Any],
    *,
    source_path: str,
    source_row_number: Any,
    strategy: str,
    mode: str,
    account: str | None,
    rejects: list[dict[str, Any]],
) -> dict[str, Any]:
    item = snapshot_from_row(
        row,
        schema_version=CANDIDATE_SNAPSHOT_SCHEMA_VERSION,
        source_kind="sealed_candidate_snapshot",
        source_path=source_path,
        source_row_number=source_row_number,
        status="rejected",
        strategy=strategy,
        mode=mode,
        account_hint=account,
    )
    if rejects:
        item["filter_stage"] = rejects[0].get("stage")
        item["filter_rule"] = rejects[0].get("reason")
        item["filter_metric_value"] = rejects[0].get("metric_value")
        item["filter_threshold"] = rejects[0].get("threshold")
    return item


def _sp_lc_snapshot_rows(
    snapshot: dict[str, Any],
    *,
    source_path: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    run_id = text(snapshot.get("run_id")) or None
    account = text(snapshot.get("account")).lower() or None
    candidates: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    selected_by_id = {text(row.get("candidate_pair_id")): dict(row) for row in project_combo_yield_candidates(snapshot)}
    diagnostics = project_combo_yield_pair_diagnostics(snapshot)
    if diagnostics:
        for row_number, diagnostic in enumerate(diagnostics, start=1):
            pair_id = text(diagnostic.get("candidate_pair_id"))
            selection_state = text(diagnostic.get("selection_state")).lower()
            has_pair_legs = bool(
                pair_id
                and text(diagnostic.get("put_contract_symbol"))
                and text(diagnostic.get("call_contract_symbol"))
            )
            if selection_state == "selected" and pair_id in selected_by_id:
                pair = {**diagnostic, **selected_by_id[pair_id]}
                status = "accepted"
            elif selection_state == "ranked_below" and has_pair_legs:
                pair = dict(diagnostic)
                status = "ranked_below"
            else:
                pair = dict(diagnostic)
                status = "rejected"
            if has_pair_legs or status == "accepted":
                candidates.extend(
                    _combo_pair_candidate_rows(
                        pair,
                        owner="sp_lc",
                        source_path=source_path,
                        source_row_number=row_number,
                        run_id=run_id,
                        account=account,
                        status=status,
                    )
                )
            decisions.extend(
                _combo_pair_filter_decisions(
                    pair,
                    owner="sp_lc",
                    source_path=source_path,
                    source_row_number=row_number,
                    run_id=run_id,
                    account=account,
                    status=status,
                )
            )
    else:
        for row_number, pair in enumerate(selected_by_id.values(), start=1):
            candidates.extend(
                _combo_pair_candidate_rows(
                    pair,
                    owner="sp_lc",
                    source_path=source_path,
                    source_row_number=row_number,
                    run_id=run_id,
                    account=account,
                    status="accepted",
                )
            )
    observed_selected = {
        text(row.get("candidate_pair_id"))
        for row in candidates
        if row.get("status") == "accepted"
    }
    for row_number, pair_id in enumerate(
        sorted(set(selected_by_id) - observed_selected),
        start=len(diagnostics) + 1,
    ):
        candidates.extend(
            _combo_pair_candidate_rows(
                selected_by_id[pair_id],
                owner="sp_lc",
                source_path=source_path,
                source_row_number=row_number,
                run_id=run_id,
                account=account,
                status="accepted",
            )
        )
    decisions.extend(
        _funding_put_filter_decisions(
            project_combo_yield_funding_put_decisions(snapshot),
            source_path=source_path,
            run_id=run_id,
            account=account,
        )
    )
    return candidates, decisions


def _cc_lp_snapshot_rows(
    snapshot: dict[str, Any],
    *,
    source_path: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    run_id = text(snapshot.get("run_id")) or None
    account = text(snapshot.get("account")).lower() or None
    candidates: list[dict[str, Any]] = []
    for row_number, pair in enumerate(project_cc_lp_candidates(snapshot), start=1):
        candidates.extend(
            _combo_pair_candidate_rows(
                pair,
                owner="cc_lp",
                source_path=source_path,
                source_row_number=row_number,
                run_id=run_id,
                account=account,
                status="accepted",
            )
        )
    return candidates, []


def _combo_pair_candidate_rows(
    row: dict[str, Any],
    *,
    owner: str,
    source_path: str,
    source_row_number: Any,
    run_id: str | None,
    account: str | None,
    status: str,
) -> list[dict[str, Any]]:
    put_contract = text(row.get("put_contract_symbol"))
    call_contract = text(row.get("call_contract_symbol"))
    if not put_contract or not call_contract:
        return []

    group_id = text(
        row.get("strategy_group_id") or row.get("group_id") or row.get("candidate_pair_id")
    ) or _combo_pair_group_id(
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
    put_credit = first_float(row, "put_net_credit", "put_only_net_credit")
    call_cost = first_float(row, "call_total_cost")
    call_credit = first_float(row, "call_net_credit")
    put_cost = first_float(row, "put_total_cost")
    structure_mode = text(row.get("structure_mode")).lower() or "same_expiry_pair"
    common = {
        **row,
        "net_credit": None,
        "run_id": text(row.get("run_id") or run_id) or None,
        "account": text(row.get("account") or account).lower() or None,
        "strategy_family": "combo_yield",
        "strategy_profile": text(
            row.get("strategy_profile") or row.get("yield_enhancement_mode") or row.get("put_strategy_profile")
        )
        or ("cc_lp_funding_call" if owner == "cc_lp" else "combo_yield"),
        "strategy_group_id": group_id,
        "candidate_pair_id": text(row.get("candidate_pair_id")) or None,
        "structure_mode": structure_mode,
        "put_expiration": text(row.get("put_expiration") or row.get("expiration") or row.get("exp")) or None,
        "put_dte": first_float(row, "put_dte", "dte"),
        "call_expiration": text(row.get("call_expiration") or row.get("expiration") or row.get("exp")) or None,
        "call_dte": first_float(row, "call_dte", "dte"),
    }
    if owner == "cc_lp":
        leg_rows = [
            {
                **common,
                "contract_symbol": call_contract,
                "option_type": "call",
                "mode": "call",
                "side": "short",
                "leg_role": "funding_call",
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
                "net_income": call_credit,
                "entry_credit": call_credit,
            },
            {
                **common,
                "contract_symbol": put_contract,
                "option_type": "put",
                "mode": "put",
                "side": "long",
                "leg_role": "protective_put",
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
                "net_income": -abs(put_cost) if put_cost is not None else None,
                "entry_cost": abs(put_cost) if put_cost is not None else None,
            },
        ]
    else:
        leg_rows = [
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
    return [
        snapshot_from_row(
            leg,
            schema_version=CANDIDATE_SNAPSHOT_SCHEMA_VERSION,
            source_kind="sealed_candidate_snapshot",
            source_path=source_path,
            source_row_number=f"{source_row_number}.{leg_number}",
            status=status,
            strategy="combo_yield",
            mode=text(leg.get("mode")).lower() or None,
            account_hint=account,
        )
        for leg_number, leg in enumerate(leg_rows, start=1)
    ]


def _combo_pair_filter_decisions(
    row: dict[str, Any],
    *,
    owner: str,
    source_path: str,
    source_row_number: Any,
    run_id: str | None,
    account: str | None,
    status: str,
) -> list[dict[str, Any]]:
    if status == "accepted":
        return []
    reasons = row.get("reject_reasons") or []
    if isinstance(reasons, str):
        reasons = [item for item in reasons.split("|") if item]
    if status == "ranked_below" and not reasons:
        reasons = ["ranked_below"]
    out: list[dict[str, Any]] = []
    for reason_number, reason in enumerate(reasons, start=1):
        for leg_number, leg in enumerate(
            _combo_pair_raw_legs(row, owner=owner, run_id=run_id, account=account),
            start=1,
        ):
            out.append(
                {
                    **leg,
                    "schema_version": FILTER_DECISION_SCHEMA_VERSION,
                    "source_kind": "sealed_candidate_snapshot",
                    "source_path": source_path,
                    "source_row_number": f"{source_row_number}.{reason_number}.{leg_number}",
                    "run_id": run_id,
                    "account": account,
                    "function": "combo_yield",
                    "status": status,
                    "stage": row.get("diagnostic_stage") or "ranking",
                    "rule": text(reason),
                    "metric_value": None,
                    "threshold": None,
                }
            )
    return out


def _combo_pair_raw_legs(
    row: dict[str, Any],
    *,
    owner: str,
    run_id: str | None,
    account: str | None,
) -> list[dict[str, Any]]:
    put_contract = text(row.get("put_contract_symbol"))
    call_contract = text(row.get("call_contract_symbol"))
    common = {
        **row,
        "run_id": run_id,
        "account": account,
        "strategy_family": "combo_yield",
        "strategy_group_id": row.get("candidate_pair_id"),
    }
    if owner == "cc_lp":
        rows = [
            {
                **common,
                "contract_symbol": call_contract,
                "mode": "call",
                "option_type": "call",
                "side": "short",
                "leg_role": "funding_call",
            },
            {
                **common,
                "contract_symbol": put_contract,
                "mode": "put",
                "option_type": "put",
                "side": "long",
                "leg_role": "protective_put",
            },
        ]
    else:
        rows = [
            {
                **common,
                "contract_symbol": put_contract,
                "mode": "put",
                "option_type": "put",
                "side": "short",
                "leg_role": "funding_put",
            },
            {
                **common,
                "contract_symbol": call_contract,
                "mode": "call",
                "option_type": "call",
                "side": "long",
                "leg_role": "participation_call",
            },
        ]
    return [row for row in rows if text(row.get("contract_symbol"))]


def _funding_put_filter_decisions(
    rows: list[dict[str, Any]],
    *,
    source_path: str,
    run_id: str | None,
    account: str | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for decision_number, record in enumerate(rows, start=1):
        opening = dict(record.get("opening_decision") or {})
        if opening.get("accepted") is True:
            continue
        normalized = dict(record.get("normalized_input") or {})
        for reject_number, reject in enumerate(opening.get("rejects") or [], start=1):
            out.append(
                {
                    **normalized,
                    "schema_version": FILTER_DECISION_SCHEMA_VERSION,
                    "source_kind": "sealed_candidate_snapshot",
                    "source_path": source_path,
                    "source_row_number": f"funding.{decision_number}.{reject_number}",
                    "run_id": run_id,
                    "account": account,
                    "strategy_family": "combo_yield",
                    "strategy_profile": normalized.get("strategy_profile") or "insurance_underwriting",
                    "function": "combo_yield",
                    "mode": "put",
                    "option_type": "put",
                    "side": "short",
                    "leg_role": "funding_put",
                    "status": "rejected",
                    "stage": reject.get("stage"),
                    "rule": reject.get("reason"),
                    "metric_value": reject.get("metric_value"),
                    "threshold": reject.get("threshold"),
                    "message": reject.get("message"),
                }
            )
    return out


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
    parts = (
        *common_parts,
        text(row.get("expiration") or row.get("exp")),
        put_contract.upper(),
        call_contract.upper(),
    )
    return "combo_yield|" + "|".join(parts)


def filter_decision_rows(
    trace_paths: list[Path],
    *,
    base: Path,
) -> list[dict[str, Any]]:
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
        "parameter_snapshot_sha256": (_payload_digest(parameter_snapshot) if parameter_snapshot else None),
        "parameter_snapshot_source": ("decision_trace_config_values" if parameter_snapshot else None),
        "strategy_group_id": text(row.get("strategy_group_id") or row.get("group_id")) or None,
        "candidate_id": text(row.get("candidate_id")) or None,
        "decision_hash": text(row.get("decision_hash")) or None,
        "opening_snapshot_rank": first_float(row, "opening_snapshot_rank"),
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
        "shares_can_sell": first_float(row, "shares_can_sell", "can_sell_qty"),
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
        "cost_basis_floor": first_float(
            row, "cost_basis_floor", "min_strike_cost_multiplier", "strike_cost_multiplier"
        ),
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


def trace_paths_from_selection(selection: ShadowReplaySourceSelection) -> list[Path]:
    explicit = [path for path in selection.trace_paths if path.exists()]
    if explicit:
        return unique(explicit)
    return unique(
        directory / "candidate_filter_trace.jsonl"
        for directory in source_dirs(selection)
        if (directory / "candidate_filter_trace.jsonl").exists()
    )


def mark_paths_from_selection(selection: ShadowReplaySourceSelection) -> list[Path]:
    explicit = [path for path in selection.mark_paths if path.exists()]
    if explicit:
        return unique(explicit)
    out: list[Path] = []
    for directory in source_dirs(selection):
        out.extend(
            glob_many(
                directory,
                ("mark_path_snapshots.jsonl", "mark_path_snapshots.csv", "*mark_path*.jsonl", "*mark_path*.csv"),
            )
        )
    return unique(out)


def outcome_paths_from_selection(selection: ShadowReplaySourceSelection) -> list[Path]:
    explicit = [path for path in selection.outcome_paths if path.exists()]
    if explicit:
        return unique(explicit)
    out: list[Path] = []
    for directory in source_dirs(selection):
        out.extend(
            glob_many(directory, ("outcome_facts.jsonl", "outcome_facts.csv", "*outcome*.jsonl", "*outcome*.csv"))
        )
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


def latest_shadow_replay_run_dir(
    *, repo_root: Path, runs_root: Path | None = None
) -> tuple[Path | None, dict[str, Any]]:
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
        probe = ShadowReplaySourceSelection(
            repo_root=repo_root,
            run_dir=run_dir,
            runs_root=root,
        )
        evidence = candidate_evidence_from_selection(probe)
        scanned = [item for item in evidence if item.classification["status"] != NOT_SCANNED]
        trace_count = len(trace_paths_from_selection(probe))
        if scanned:
            return run_dir, {
                "requested": True,
                "found": True,
                "source": "runs_root_mtime",
                "runs_root": safe_rel(root, base=repo_root),
                "path": safe_rel(run_dir, base=repo_root),
                "run_id": run_dir.name,
                "searched_count": searched_count,
                "skipped_without_evidence_count": skipped_without_evidence_count,
                "candidate_evidence_account_count": len(scanned),
                "candidate_evidence_status_counts": candidate_evidence_coverage(evidence)["counts"],
                "trace_path_count": trace_count,
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
        close_row_count = sum(
            len(
                _validated_close_report_rows(
                    path,
                    expected_run_id=run_dir.name,
                )[0]
            )
            for path in close_paths
        )
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
