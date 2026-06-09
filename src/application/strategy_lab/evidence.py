from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.shadow_replay import load_shadow_replay_observed_evidence
from src.application.shadow_replay.common import dataset_dir_from_arg, read_jsonl


def load_strategy_lab_dataset(dataset: str | Path) -> dict[str, Any]:
    dataset_dir = dataset_dir_from_arg(dataset)
    manifest_path = dataset_dir / "manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                manifest = payload
        except json.JSONDecodeError:
            manifest = {"status": "invalid_json"}
    return {
        "dataset_dir": str(dataset_dir),
        "manifest": manifest,
        "candidate_snapshots": read_jsonl(dataset_dir / "candidate_snapshots.jsonl"),
        "filter_decisions": read_jsonl(dataset_dir / "filter_decisions.jsonl"),
        "rank_snapshots": read_jsonl(dataset_dir / "rank_snapshots.jsonl"),
        "mark_snapshots": read_jsonl(dataset_dir / "mark_path_snapshots.jsonl"),
        "outcome_facts": read_jsonl(dataset_dir / "outcome_facts.jsonl"),
    }


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
        evidence["coverage"] = {
            "mode": "dataset",
            "dataset_dir": evidence["dataset_dir"],
            "strict_backtest_allowed": bool(evidence["candidate_snapshots"]),
            "reason": "dataset_candidate_universe_ready" if evidence["candidate_snapshots"] else "candidate_universe_missing",
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
    return {
        "dataset_dir": dataset_dir,
        "manifest": manifest,
        "candidate_snapshots": list(observed["candidate_snapshots"]),
        "filter_decisions": list(observed["filter_decisions"]),
        "rank_snapshots": [],
        "mark_snapshots": list(observed["mark_snapshots"]),
        "outcome_facts": list(observed["outcome_facts"]),
        "source": observed.get("source") or {},
        "coverage": observed.get("coverage") or {},
        "filters": observed.get("filters") or {},
    }


def _has_scope_filters(*, accounts: list[str] | tuple[str, ...] | None, market: str | None) -> bool:
    return bool(accounts or (market is not None and str(market).strip()))
