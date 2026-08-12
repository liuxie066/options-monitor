from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.shadow_replay import load_shadow_replay_observed_evidence
from src.application.shadow_replay.common import (
    bind_legacy_decision_evidence,
    dataset_dir_from_arg,
    dataset_read_lock,
    freeze_decision_identities,
    read_jsonl,
    validate_dataset_integrity,
)


def load_strategy_lab_dataset(dataset: str | Path) -> dict[str, Any]:
    dataset_dir = dataset_dir_from_arg(dataset)
    with dataset_read_lock(dataset_dir):
        integrity = validate_dataset_integrity(dataset_dir, require_manifest=False)
        manifest_path = dataset_dir / "manifest.json"
        manifest: dict[str, Any] = {}
        if manifest_path.exists():
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                manifest = payload
        candidates = freeze_decision_identities(
            read_jsonl(dataset_dir / "candidate_snapshots.jsonl")
        )
        marks = bind_legacy_decision_evidence(
            candidates,
            read_jsonl(dataset_dir / "mark_path_snapshots.jsonl"),
        )
        outcomes = bind_legacy_decision_evidence(
            candidates,
            read_jsonl(dataset_dir / "outcome_facts.jsonl"),
        )
        result = {
            "dataset_dir": str(dataset_dir),
            "manifest": manifest,
            "candidate_snapshots": candidates,
            "filter_decisions": read_jsonl(dataset_dir / "filter_decisions.jsonl"),
            "rank_snapshots": read_jsonl(dataset_dir / "rank_snapshots.jsonl"),
            "mark_snapshots": marks,
            "outcome_facts": outcomes,
            "integrity": integrity,
        }
        validate_dataset_integrity(dataset_dir, require_manifest=False)
        return result


def load_strategy_lab_evidence(
    *,
    repo_root: str | Path | None = None,
    dataset: str | Path | None = None,
    runs_root: str | Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    accounts: list[str] | tuple[str, ...] | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    if dataset is not None and str(dataset).strip() and repo_root is None and not _has_scope_filters(accounts=accounts, market=market):
        evidence = load_strategy_lab_dataset(dataset)
        evidence["source"] = {"mode": "dataset", "dataset_dir": evidence["dataset_dir"]}
        source = evidence["manifest"].get("source")
        source = source if isinstance(source, dict) else {}
        candidate_coverage = source.get("candidate_evidence_coverage")
        candidate_coverage = candidate_coverage if isinstance(candidate_coverage, dict) else None
        integrity_ready = evidence["integrity"].get("status") == "verified"
        authority_ready = bool(
            candidate_coverage
            and candidate_coverage.get("strict_replay_authority") is True
        )
        candidate_ready = bool(evidence["candidate_snapshots"])
        strict_ready = integrity_ready and authority_ready and candidate_ready
        if not integrity_ready:
            reason = "dataset_integrity_unverified"
        elif candidate_coverage is None:
            reason = "candidate_evidence_coverage_missing"
        elif not authority_ready:
            reason = "candidate_evidence_coverage_incomplete"
        elif not candidate_ready:
            reason = "candidate_universe_missing"
        else:
            reason = "dataset_candidate_universe_ready"
        evidence["coverage"] = {
            "mode": "dataset",
            "dataset_dir": evidence["dataset_dir"],
            "strict_backtest_allowed": strict_ready,
            "reason": reason,
            "candidate_evidence_coverage": candidate_coverage,
            "dataset_integrity": evidence["integrity"],
        }
        evidence["filters"] = {"accounts": [], "market": None, "market_filter_applied": False}
        return evidence
    if repo_root is None:
        raise ValueError("repo_root is required for run-window Strategy Lab evidence")
    observed = load_shadow_replay_observed_evidence(
        repo_root=repo_root,
        dataset=dataset,
        runs_root=runs_root,
        start_date=start_date,
        end_date=end_date,
        accounts=accounts,
        market=market,
    )
    dataset_dir = str(dataset_dir_from_arg(dataset)) if dataset is not None and str(dataset).strip() else None
    manifest: dict[str, Any] = {}
    if dataset_dir:
        manifest_path = Path(dataset_dir) / "manifest.json"
        if manifest_path.exists():
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    manifest = payload
            except json.JSONDecodeError:
                manifest = {"status": "invalid_json"}
    candidates = freeze_decision_identities(list(observed["candidate_snapshots"]))
    marks = bind_legacy_decision_evidence(candidates, list(observed["mark_snapshots"]))
    outcomes = bind_legacy_decision_evidence(candidates, list(observed["outcome_facts"]))
    return {
        "dataset_dir": dataset_dir,
        "manifest": manifest,
        "candidate_snapshots": candidates,
        "filter_decisions": list(observed["filter_decisions"]),
        "rank_snapshots": list(observed.get("rank_snapshots") or []),
        "mark_snapshots": marks,
        "outcome_facts": outcomes,
        "source": observed.get("source") or {},
        "coverage": observed.get("coverage") or {},
        "filters": observed.get("filters") or {},
    }


def _has_scope_filters(*, accounts: list[str] | tuple[str, ...] | None, market: str | None) -> bool:
    return bool(accounts or (market is not None and str(market).strip()))
