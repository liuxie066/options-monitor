from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.candidate_evidence_history import (
    AccountCandidateEvidence,
    load_run_candidate_evidence,
)
from src.application.candidate_filter_trace import (
    infer_trace_scope_from_path,
    read_candidate_filter_trace,
)
from src.application.cc_lp_candidate_snapshot import (
    CC_LP_CANDIDATE_SNAPSHOT_FILE,
    project_cc_lp_candidates,
)
from src.application.combo_yield_candidate_snapshot import (
    COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    project_combo_yield_candidates,
    project_combo_yield_pair_diagnostics,
)
from src.application.opening_candidate_snapshot import (
    OPENING_CANDIDATE_SNAPSHOT_FILE,
    ranked_opening_candidates,
    ranked_opening_candidate_decisions,
    rejected_opening_candidate_decisions,
)
from src.application.research.redaction import redact_value
from src.application.runtime_logs_cli import collect_runtime_logs
from src.application.runtime_runs_cli import collect_runtime_runs


def collect_evidence(
    payload: dict[str, Any],
    *,
    runtime_status_tool_fn: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str], dict[str, Any]]],
    load_runtime_config: Callable[..., tuple[Path, dict[str, Any]]],
    repo_base: Callable[[], Path],
    mask_path: Callable[[Any], str | None],
    now_fn: Callable[[], datetime] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    base = repo_base().resolve()
    now = (now_fn or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    config_path, cfg = load_runtime_config(
        config_key=payload.get("config_key"),
        config_path=payload.get("config_path"),
    )

    runtime_payload = _runtime_status_payload(payload)
    runtime_data, runtime_warnings, runtime_meta = runtime_status_tool_fn(runtime_payload)
    source_paths = _actual_source_paths(payload, runtime_data=runtime_data, base=base)
    source_refs = _source_refs(source_paths, base=base)
    tail_limit = _as_int(payload.get("tail_limit"), default=20, low=0, high=200)
    audit_tails = _audit_tails(source_paths, base=base, tail_limit=tail_limit)
    selected_runs_root = _selected_runs_root(payload, base=base)
    runtime_runs = collect_runtime_runs(
        repo_root=base,
        runs_root=selected_runs_root,
        profile_path=_profile_path(payload),
        limit=_as_int(payload.get("runs_limit"), default=10, low=1, high=50),
        run_id=payload.get("run_id"),
        run_dir=payload.get("run_dir"),
    )
    runtime_logs = collect_runtime_logs(
        repo_root=base,
        runs_root=selected_runs_root,
        profile_path=_profile_path(payload),
        run_id=payload.get("run_id"),
        run_dir=payload.get("run_dir"),
        kind="all",
        lines=tail_limit,
    )
    collector_deployment = _deployment_snapshot(
        base=base,
        config_path=config_path,
        cfg=cfg,
        mask_path=mask_path,
    )
    producer_provenance = _producer_provenance(
        payload,
        source_paths=source_paths,
        base=base,
    )
    attribution = _historical_attribution_status(
        payload,
        producer=producer_provenance,
        collector=collector_deployment,
    )
    candidate_evidence = _candidate_evidence(
        payload,
        source_paths=source_paths,
        base=base,
        tail_limit=tail_limit,
        attribution=attribution,
    )

    scheduler_evidence = _normalize_scheduler_evidence(payload.get("scheduler_evidence"))
    evidence = {
        "schema_version": "research_evidence.v1",
        "collected_at_utc": now.isoformat().replace("+00:00", "Z"),
        "input": _safe_input_summary(payload),
        "deployment": {
            **collector_deployment,
            "collector": collector_deployment,
            "producer": producer_provenance,
            "attribution": attribution,
        },
        "scheduler_evidence": scheduler_evidence,
        "runtime_status": runtime_data,
        "runtime_status_warnings": list(runtime_warnings),
        "runtime_runs": runtime_runs,
        "runtime_logs": runtime_logs,
        "audit_tails": audit_tails,
        "candidate_evidence": candidate_evidence,
        "source_refs": source_refs,
    }
    warnings = list(runtime_warnings)
    if not scheduler_evidence.get("provided"):
        warnings.append("scheduler_evidence_missing: online scheduler status was not provided")
    meta = {
        "config_path": mask_path(config_path),
        "runtime_status_meta": runtime_meta,
    }
    return evidence, warnings, meta


def _profile_path(payload: dict[str, Any]) -> Any:
    return payload.get("profile_path")


def redacted_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return redact_value(evidence)


def _runtime_status_payload(payload: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "config_key",
        "config_path",
        "accounts",
        "report_dir",
        "state_dir",
        "shared_state_dir",
        "accounts_root",
        "runs_root",
        "run_id",
        "run_dir",
        "max_notification_chars",
        "max_run_age_minutes",
        "profile_path",
        "trigger_source",
        "trigger_job_id",
        "trigger_job_name",
        "trigger_schedule",
        "trigger_timezone",
        "delivery",
        "delivery_mode",
        "deliveryMode",
        "timeout_seconds",
        "timeoutSeconds",
    )
    return {key: payload[key] for key in keys if key in payload}


def _safe_input_summary(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (
        "config_key",
        "config_path",
        "accounts",
        "run_id",
        "run_dir",
        "report_dir",
        "state_dir",
        "shared_state_dir",
        "accounts_root",
        "runs_root",
        "runs_limit",
        "tail_limit",
        "max_run_age_minutes",
        "max_notification_chars",
        "trace_paths",
        "candidate_report_dir",
        "profile_path",
        "output",
        "scope",
    ):
        if key in payload:
            out[key] = payload.get(key)
    if isinstance(payload.get("scheduler_evidence"), dict):
        scheduler = payload["scheduler_evidence"]
        out["scheduler_evidence"] = {
            "provider": scheduler.get("provider"),
            "job_name": scheduler.get("job_name"),
            "last_run_id": scheduler.get("last_run_id") or scheduler.get("run_id"),
            "last_status": scheduler.get("last_status") or scheduler.get("status"),
            "last_exit_code": scheduler.get("last_exit_code") or scheduler.get("exit_code"),
            "last_triggered_at": scheduler.get("last_triggered_at"),
            "last_finished_at": scheduler.get("last_finished_at") or scheduler.get("finished_at"),
        }
    return out


def _deployment_snapshot(
    *,
    base: Path,
    config_path: Path,
    cfg: dict[str, Any],
    mask_path: Callable[[Any], str | None],
) -> dict[str, Any]:
    version_path = base / "VERSION"
    version = None
    if version_path.exists():
        try:
            version = version_path.read_text(encoding="utf-8").strip()
        except Exception:
            version = None
    return {
        "version": version,
        "git_commit": _git_output(base, "rev-parse", "--short", "HEAD"),
        "git_branch": _git_output(base, "rev-parse", "--abbrev-ref", "HEAD"),
        "config_path": mask_path(config_path),
        "config_digest": _file_digest(config_path),
        "config_key": _infer_config_key(config_path),
        "accounts": _accounts_from_config(cfg),
    }


def _producer_provenance(
    payload: dict[str, Any],
    *,
    source_paths: dict[str, Path | None],
    base: Path,
) -> dict[str, Any]:
    run_dir_raw = payload.get("run_dir")
    if run_dir_raw:
        run_dir = Path(str(run_dir_raw)).expanduser()
        run_dir = (
            run_dir.resolve()
            if run_dir.is_absolute()
            else (base / run_dir).resolve()
        )
    else:
        run_dir = source_paths.get("latest_run_dir")
    if run_dir is None:
        return {"status": "not_requested"}
    candidates = (
        run_dir / "run_manifest.json",
        run_dir / "manifest.json",
        run_dir / "state" / "run_manifest.json",
        run_dir / "run_audit.json",
        run_dir / "state" / "run_audit.json",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload_raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload_raw, dict):
            continue
        producer = payload_raw.get("producer")
        source = producer if isinstance(producer, dict) else payload_raw
        return {
            "status": "available",
            "source_path": _safe_rel(path, base=base),
            "version": source.get("version"),
            "git_commit": source.get("git_commit") or source.get("commit_sha"),
            "config_digest": source.get("config_digest") or source.get("config_sha256"),
            "policy_digest": source.get("policy_digest") or source.get("ranking_policy_digest"),
        }
    return {
        "status": "missing",
        "run_dir": _safe_rel(run_dir, base=base),
    }


def _historical_attribution_status(
    payload: dict[str, Any],
    *,
    producer: dict[str, Any],
    collector: dict[str, Any],
) -> dict[str, Any]:
    historical = bool(
        str(payload.get("run_id") or "").strip()
        or str(payload.get("run_dir") or "").strip()
    )
    if not historical:
        return {
            "historical_run": False,
            "status": "collector_current",
            "configured_ranking_allowed": True,
        }
    producer_commit = str(producer.get("git_commit") or "").strip()
    producer_config = str(producer.get("config_digest") or "").strip()
    commit_match = bool(
        producer_commit
        and producer_commit == str(collector.get("git_commit") or "").strip()
    )
    config_match = bool(
        producer_config
        and producer_config == str(collector.get("config_digest") or "").strip()
    )
    allowed = commit_match and config_match
    return {
        "historical_run": True,
        "status": "matched" if allowed else "unknown_or_mismatch",
        "producer_commit_match": commit_match,
        "producer_config_match": config_match,
        "configured_ranking_allowed": allowed,
    }


def _git_output(base: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(base),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _file_digest(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except Exception:
        return None
    return hashlib.sha256(data).hexdigest()


def _infer_config_key(config_path: Path) -> str | None:
    name = config_path.name.lower()
    if ".us." in name or name.endswith(".us.json"):
        return "us"
    if ".hk." in name or name.endswith(".hk.json"):
        return "hk"
    return None


def _accounts_from_config(cfg: dict[str, Any]) -> list[str]:
    raw = cfg.get("accounts")
    if isinstance(raw, list):
        return [str(item).strip().lower() for item in raw if str(item).strip()]
    account_settings = cfg.get("account_settings")
    if isinstance(account_settings, dict):
        return [str(key).strip().lower() for key in account_settings if str(key).strip()]
    return []


def _normalize_scheduler_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {"provided": False}
    return {
        "provided": True,
        "provider": value.get("provider"),
        "job_name": value.get("job_name") or value.get("name"),
        "last_run_id": value.get("last_run_id") or value.get("run_id"),
        "last_run_path": value.get("last_run_path") or value.get("run_path"),
        "last_triggered_at": value.get("last_triggered_at") or value.get("triggered_at"),
        "last_finished_at": value.get("last_finished_at") or value.get("finished_at"),
        "last_status": value.get("last_status") or value.get("status"),
        "last_exit_code": value.get("last_exit_code") if "last_exit_code" in value else value.get("exit_code"),
        "timeout": value.get("timeout") or value.get("timed_out"),
        "stdout_tail": value.get("stdout_tail"),
        "stderr_tail": value.get("stderr_tail"),
        "raw": value.get("raw") if isinstance(value.get("raw"), dict) else None,
    }


def _actual_source_paths(payload: dict[str, Any], *, runtime_data: dict[str, Any], base: Path) -> dict[str, Path | None]:
    paths_raw = runtime_data.get("paths")
    paths: dict[str, Any] = paths_raw if isinstance(paths_raw, dict) else {}
    profile_paths = _profile_source_paths(payload, base=base)
    runtime_root = _resolve_under_base(profile_paths.get("runtime_root"), base=base, default=base)
    report_dir = _resolve_under_base(
        payload.get("candidate_report_dir") or payload.get("report_dir") or profile_paths.get("report_dir") or paths.get("report_dir"),
        base=base,
        default=runtime_root / "output_shared" / "reports",
    )
    shared_state_dir = _resolve_under_base(
        payload.get("shared_state_dir") or profile_paths.get("shared_state_dir") or paths.get("shared_state_dir"),
        base=base,
        default=runtime_root / "output_shared" / "state",
    )
    runs_root = _resolve_under_base(
        payload.get("runs_root") or profile_paths.get("runs_root") or paths.get("runs_root"),
        base=base,
        default=runtime_root / "output_runs",
    )
    latest_run_path = _resolve_runtime_path(_nested(runtime_data, "latest_run", "path"), base=base, fallback_root=runs_root)
    latest_scanned_run_path = _resolve_runtime_path(_nested(runtime_data, "latest_scanned_run", "path"), base=base, fallback_root=runs_root)
    return {
        "report_dir": report_dir,
        "shared_state_dir": shared_state_dir,
        "runs_root": runs_root,
        "latest_run_dir": latest_run_path,
        "latest_scanned_run_dir": latest_scanned_run_path,
    }


def _profile_source_paths(payload: dict[str, Any], *, base: Path) -> dict[str, Any]:
    raw_profile = _profile_path(payload)
    if not raw_profile:
        return {}
    profile_path = Path(str(raw_profile)).expanduser()
    if not profile_path.is_absolute():
        profile_path = (base / profile_path).resolve()
    if not profile_path.exists() or not profile_path.is_file():
        return {}
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(profile, dict):
        return {}
    paths_raw = profile.get("paths")
    paths: dict[str, Any] = paths_raw if isinstance(paths_raw, dict) else {}
    out: dict[str, Any] = {}
    for key in ("report_dir", "shared_state_dir", "runs_root", "runtime_root"):
        value = paths.get(key)
        if value is None:
            value = profile.get(key)
        if value is not None:
            out[key] = value
    return out


def _source_refs(source_paths: dict[str, Path | None], *, base: Path) -> dict[str, Any]:
    shared_state_dir = source_paths.get("shared_state_dir")
    report_dir = source_paths.get("report_dir")
    runs_root = source_paths.get("runs_root")
    latest_run_path = source_paths.get("latest_run_dir")
    latest_scanned_run_path = source_paths.get("latest_scanned_run_dir")
    return {
        "report_dir": _safe_rel(report_dir, base=base) if report_dir else None,
        "shared_state_dir": _safe_rel(shared_state_dir, base=base) if shared_state_dir else None,
        "runs_root": _safe_rel(runs_root, base=base) if runs_root else None,
        "latest_run_dir": _safe_rel(latest_run_path, base=base) if latest_run_path else None,
        "latest_scanned_run_dir": _safe_rel(latest_scanned_run_path, base=base) if latest_scanned_run_path else None,
    }


def _audit_tails(source_paths: dict[str, Path | None], *, base: Path, tail_limit: int) -> dict[str, Any]:
    shared_state_dir = source_paths.get("shared_state_dir")
    latest_run_dir = source_paths.get("latest_run_dir")
    latest_scanned_run_dir = source_paths.get("latest_scanned_run_dir")
    out: dict[str, Any] = {}
    if shared_state_dir is not None:
        out["shared_audit_events"] = _jsonl_tail(shared_state_dir / "audit_events.jsonl", base=base, limit=tail_limit)
    if latest_run_dir is not None:
        out["latest_run_tool_execution_audit"] = _jsonl_tail(latest_run_dir / "state" / "tool_execution_audit.jsonl", base=base, limit=tail_limit)
    if latest_scanned_run_dir is not None and latest_scanned_run_dir != latest_run_dir:
        out["latest_scanned_run_tool_execution_audit"] = _jsonl_tail(latest_scanned_run_dir / "state" / "tool_execution_audit.jsonl", base=base, limit=tail_limit)
    return out


def _candidate_evidence(
    payload: dict[str, Any],
    *,
    source_paths: dict[str, Path | None],
    base: Path,
    tail_limit: int,
    attribution: dict[str, Any],
) -> dict[str, Any]:
    run_dir, runs_root, run_id = _candidate_source_context(
        payload,
        source_paths=source_paths,
        base=base,
    )
    account_evidence = (
        load_run_candidate_evidence(base=base, run_id=run_id, runs_root=runs_root)
        if run_id and runs_root is not None
        else []
    )
    candidate_rows, sealed_decisions, rank_rows = _candidate_facts(
        account_evidence,
        base=base,
    )
    trace_paths = _candidate_trace_paths(
        payload,
        run_dir=run_dir,
        report_dir=source_paths.get("report_dir"),
        base=base,
    )[:20]
    trace_decisions = _filter_decision_rows(trace_paths, base=base)
    filter_decisions = _merge_filter_decisions(sealed_decisions + trace_decisions)
    candidate_reports = _candidate_snapshot_reports(
        candidate_rows,
        account_evidence=account_evidence,
        base=base,
    )
    reject_logs = _candidate_rejection_summaries(filter_decisions)
    filter_traces = [_trace_summary(path, base=base, limit=tail_limit) for path in trace_paths]
    combo_yield_pair_diagnostics = _combo_yield_pair_diagnostics_from_evidence(
        account_evidence,
        base=base,
    )
    ranking_limit = _as_int(payload.get("ranking_limit"), default=5, low=1, high=20)
    ranking_evidence = _sealed_ranking_evidence(
        rank_rows,
        limit=ranking_limit,
        attribution=attribution,
    )
    total_candidate_rows = sum(int(item.get("row_count") or 0) for item in candidate_reports if item.get("exists"))
    total_reject_rows = sum(int(item.get("row_count") or 0) for item in reject_logs if item.get("exists"))
    return {
        "schema_version": "research_candidate_evidence.v1",
        "candidate_snapshot_reports": candidate_reports,
        "rejection_evidence": reject_logs,
        "filter_traces": filter_traces,
        "combo_yield_pair_diagnostics": combo_yield_pair_diagnostics,
        "ranking_evidence": ranking_evidence,
        "summary": {
            "candidate_snapshot_file_count": sum(
                len(item.get("owner_snapshots") or [])
                for item in candidate_reports
            ),
            "candidate_row_count": total_candidate_rows,
            "rejection_evidence_group_count": sum(
                1 for item in reject_logs if item.get("exists")
            ),
            "rejection_decision_count": total_reject_rows,
            "filter_trace_file_count": sum(1 for item in filter_traces if item.get("exists")),
            "combo_yield_pair_diagnostic_snapshot_count": _nested(combo_yield_pair_diagnostics, "summary", "file_count"),
            "combo_yield_pair_diagnostic_row_count": _nested(combo_yield_pair_diagnostics, "summary", "row_count"),
            "combo_yield_pair_diagnostic_unique_market_row_count": _nested(
                combo_yield_pair_diagnostics, "summary", "unique_market_row_count"
            ),
            "ranking_report_count": _nested(_dict_or_empty(ranking_evidence), "summary", "report_count"),
            "ranking_top_row_count": _nested(_dict_or_empty(ranking_evidence), "summary", "top_row_count"),
            "evidence_level": (
                "candidate_and_trace"
                if total_candidate_rows and any(item.get("exists") for item in filter_traces)
                else (
                    "candidate_only"
                    if total_candidate_rows
                    else ("pair_diagnostics" if _nested(combo_yield_pair_diagnostics, "summary", "row_count") else "limited")
                )
            ),
        },
    }


def _explicit_paths(value: Any, *, base: Path) -> list[Path]:
    raw_items = value if isinstance(value, list) else ([value] if value else [])
    out: list[Path] = []
    for item in raw_items:
        raw = str(item or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (base / path).resolve()
        else:
            path = path.resolve()
        out.append(path)
    return out


def _selected_runs_root(payload: dict[str, Any], *, base: Path) -> Any:
    explicit = _explicit_run_context(payload, base=base)
    return explicit[1] if explicit is not None else payload.get("runs_root")


def _explicit_run_context(
    payload: dict[str, Any], *, base: Path
) -> tuple[Path, Path, str] | None:
    run_dirs = _explicit_paths(payload.get("run_dir"), base=base)
    if not run_dirs:
        return None
    run_dir = run_dirs[0]
    if run_dir.parent.name != "output_runs":
        raise ValueError("research candidate evidence requires canonical output_runs")
    run_id = _text(payload.get("run_id"))
    if run_id and run_id != run_dir.name:
        raise ValueError("research run_id conflicts with run_dir")
    return run_dir, run_dir.parent, run_dir.name


def _candidate_source_context(
    payload: dict[str, Any],
    *,
    source_paths: dict[str, Path | None],
    base: Path,
) -> tuple[Path | None, Path | None, str | None]:
    runs_root = source_paths.get("runs_root")
    explicit = _explicit_run_context(payload, base=base)
    if explicit is not None:
        return explicit
    run_dir = None
    run_id = _text(payload.get("run_id")) or None
    if run_dir is None and run_id and runs_root is not None:
        run_dir = (runs_root / run_id).resolve()
    if run_dir is None:
        run_dir = (
            source_paths.get("latest_scanned_run_dir")
            or source_paths.get("latest_run_dir")
        )
    return run_dir, runs_root, run_id or (run_dir.name if run_dir is not None else None)


def _candidate_trace_paths(
    payload: dict[str, Any],
    *,
    run_dir: Path | None,
    report_dir: Path | None,
    base: Path,
) -> list[Path]:
    explicit = [
        path
        for path in _explicit_paths(
            payload.get("trace_paths") or payload.get("trace_path"),
            base=base,
        )
        if path.is_file()
    ]
    if explicit:
        return _unique_paths(explicit)
    roots = [path for path in (run_dir, report_dir) if path is not None]
    paths: list[Path] = []
    for root in roots:
        paths.extend(root.glob("candidate_filter_trace.jsonl"))
        accounts = root / "accounts"
        if accounts.is_dir():
            paths.extend(accounts.glob("*/candidate_filter_trace.jsonl"))
    return _unique_paths([path.resolve() for path in paths if path.is_file()])


def _candidate_facts(
    evidence: list[AccountCandidateEvidence],
    *,
    base: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    ranks: list[dict[str, Any]] = []
    for item in evidence:
        if not item.contributes_evidence:
            continue
        default_account = _text(item.classification.get("account")).lower() or None
        for owner, snapshot in item.owners.items():
            source_path = _owner_source_path(item, owner=owner, base=base)
            account = _text(snapshot.get("account") or default_account).lower() or None
            run_id = snapshot.get("run_id")
            if owner == "opening":
                for status, owner_decisions in (
                    ("accepted", ranked_opening_candidate_decisions(snapshot)),
                    ("rejected", rejected_opening_candidate_decisions(snapshot)),
                ):
                    for decision in owner_decisions:
                        mode = _text(decision.get("strategy_mode")).lower()
                        facts = dict(decision.get("normalized_input") or {})
                        candidates.append(
                            {
                                **facts,
                                "run_id": run_id,
                                "account": account,
                                "owner": owner,
                                "strategy": _opening_strategy(mode),
                                "mode": mode,
                                "status": status,
                                "source_path": source_path,
                            }
                        )
                        if status != "rejected":
                            continue
                        opening = decision.get("opening_decision")
                        rejects = opening.get("rejects") if isinstance(opening, dict) else []
                        for reject in rejects or []:
                            if not isinstance(reject, dict):
                                continue
                            decisions.append(
                                {
                                    **facts,
                                    "run_id": run_id,
                                    "account": account,
                                    "function": _opening_strategy(mode),
                                    "mode": mode,
                                    "status": "rejected",
                                    "stage": reject.get("stage"),
                                    "rule": reject.get("reason"),
                                    "metric_value": reject.get("metric_value"),
                                    "threshold": reject.get("threshold"),
                                    "message": reject.get("message"),
                                    "source_path": source_path,
                                }
                            )
                for ranked in ranked_opening_candidates(snapshot):
                    mode = _text(ranked.get("strategy_mode")).lower()
                    facts = dict(ranked.get("facts") or {})
                    ranks.append(
                        {
                            "run_id": run_id,
                            "account": account,
                            "strategy": _opening_strategy(mode),
                            "mode": mode,
                            "rank": ranked.get("rank"),
                            "symbol": facts.get("symbol"),
                            "contract_symbol": facts.get("contract_symbol"),
                            "rank_explanation": dict(ranked.get("ranking") or {}),
                            "sealed_facts": facts,
                            "source_path": source_path,
                        }
                    )
                continue
            projector = (
                project_combo_yield_candidates
                if owner == "sp_lc"
                else project_cc_lp_candidates
                if owner == "cc_lp"
                else None
            )
            if projector is None:
                continue
            for row in projector(snapshot):
                candidates.append(
                    {
                        **row,
                        "run_id": row.get("run_id") or run_id,
                        "account": _text(row.get("account") or account).lower() or None,
                        "owner": owner,
                        "status": "accepted",
                        "source_path": source_path,
                    }
                )
    return candidates, _merge_filter_decisions(decisions), ranks


def _owner_source_path(
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
    return _safe_rel(evidence.account_dir / "state" / filenames[owner], base=base)


def _opening_strategy(mode: str) -> str:
    if mode == "put":
        return "sell_put"
    if mode == "call":
        return "sell_call"
    raise ValueError(f"unsupported opening strategy mode: {mode}")


def _filter_decision_rows(paths: list[Path], *, base: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        scope = infer_trace_scope_from_path(path)
        for row in read_candidate_filter_trace(path):
            item = dict(row)
            item["source_path"] = _safe_rel(path, base=base)
            item["run_id"] = item.get("run_id") or scope.get("run_id")
            item["account"] = _text(item.get("account") or scope.get("account")).lower() or None
            item["status"] = _text(item.get("status") or "rejected").lower()
            item["rule"] = item.get("rule") or item.get("reject_rule") or item.get("reject_reason")
            rows.append(item)
    return _merge_filter_decisions(rows)


def _merge_filter_decisions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    for row in rows:
        key = (
            _text(row.get("run_id")),
            _text(row.get("account")).lower(),
            _text(row.get("symbol") or row.get("underlying_symbol")).upper(),
            _text(row.get("contract_symbol") or row.get("option_symbol")).upper(),
            _text(row.get("mode") or row.get("option_type")).lower(),
            _text(row.get("status") or "rejected").lower(),
            _text(row.get("rule") or row.get("reject_rule") or row.get("reject_reason")),
        )
        if not key[-1] or not (key[2] or key[3]):
            unkeyed.append(row)
            continue
        target = merged.setdefault(key, dict(row))
        for name, value in row.items():
            if target.get(name) in (None, "") and value not in (None, ""):
                target[name] = value
    return [*merged.values(), *unkeyed]


def _candidate_snapshot_reports(
    rows: list[dict[str, Any]],
    *,
    account_evidence: list[Any],
    base: Path,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for evidence in account_evidence:
        account = _text(evidence.classification.get("account")).lower()
        account_rows = [row for row in rows if _text(row.get("account")).lower() == account]
        status_counts = Counter(_text(row.get("status")).lower() or "unknown" for row in account_rows)
        strategy_counts = Counter(_text(row.get("strategy")) for row in account_rows if _text(row.get("strategy")))
        symbol_counts = Counter(_text(row.get("symbol")).upper() for row in account_rows if _text(row.get("symbol")))
        reports.append(
            {
                "source_kind": "sealed_candidate_snapshot",
                "path": _safe_rel(evidence.account_dir / "state", base=base),
                "exists": bool(evidence.owners),
                "account_hint": account or None,
                "row_count": len(account_rows),
                "account_counts": {account: len(account_rows)} if account else {},
                "strategy_counts": dict(strategy_counts.most_common()),
                "symbol_counts": dict(symbol_counts.most_common()),
                "status_counts": dict(status_counts.most_common()),
                "owner_snapshots": sorted(evidence.owners),
                "sample_rows": account_rows[:5],
            }
        )
    return reports


def _candidate_rejection_summaries(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(_text(row.get("account")).lower(), []).append(row)
    reports: list[dict[str, Any]] = []
    for account, items in sorted(grouped.items()):
        stage_counts = Counter(_text(row.get("stage")) for row in items if _text(row.get("stage")))
        reason_counts = Counter(_text(row.get("rule")) for row in items if _text(row.get("rule")))
        symbol_counts = Counter(_text(row.get("symbol")).upper() for row in items if _text(row.get("symbol")))
        reports.append(
            {
                "source_kind": "sealed_snapshot_and_trace",
                "account_hint": account or None,
                "row_count": len(items),
                "account_counts": {account: len(items)} if account else {},
                "stage_counts": dict(stage_counts.most_common(10)),
                "reason_counts": dict(reason_counts.most_common(20)),
                "symbol_counts": dict(symbol_counts.most_common(20)),
                "sample_rows": items[:5],
                "exists": True,
            }
        )
    return reports


_PAIR_DIAGNOSTIC_NEAREST_MISS_SPECS: dict[str, tuple[str, str | None, str]] = {
    "call_delta_below_min": ("call_delta", "policy_call_min_delta", "below"),
    "call_delta_above_max": ("call_delta", "policy_call_max_delta", "above"),
    "call_open_interest_below_min": ("call_open_interest", "policy_call_min_open_interest", "below"),
    "call_volume_below_min": ("call_volume", "policy_call_min_volume", "below"),
    "call_spread_ratio_above_max": ("call_spread_ratio", "policy_call_max_spread_ratio", "above"),
    "annualized_net_credit_yield": (
        "annualized_net_credit_yield",
        "policy_min_net_credit_annualized",
        "below",
    ),
    "combo_spread_ratio": ("combo_spread_ratio", "policy_max_combo_spread_ratio", "above"),
    "funding_mode_credit_or_even": ("combo_net_credit", None, "below"),
    "max_debit_native": ("net_debit", "policy_max_debit_native", "above"),
    "min_net_credit_retention": (
        "net_credit_retention",
        "policy_min_net_credit_retention",
        "below",
    ),
}


def _combo_yield_pair_diagnostics_from_evidence(
    account_evidence: list[Any],
    *,
    base: Path,
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    unique: dict[tuple[str, ...], dict[str, Any]] = {}
    for evidence in account_evidence:
        snapshot = evidence.owners.get("sp_lc")
        if not isinstance(snapshot, dict):
            continue
        path = evidence.account_dir / "state" / "combo_yield_candidate_snapshot.json"
        account_hint = _text(evidence.classification.get("account")).lower() or None
        file_rows = [
            dict(item)
            for item in snapshot.get("pair_evaluations") or []
            if isinstance(item, dict)
        ]
        file_info: dict[str, Any] = {
            "path": _safe_rel(path, base=base),
            "exists": True,
            "account_hint": account_hint,
            "row_count": len(file_rows),
        }
        files.append(file_info)
        for row in file_rows:
            item = dict(row)
            account = _text(row.get("account") or account_hint).lower()
            item["_account"] = account
            rows.append(item)
            key = _pair_diagnostic_key(item)
            if key not in unique:
                item["_accounts"] = {account} if account else set()
                unique[key] = item
            elif account:
                unique[key]["_accounts"].add(account)

    unique_rows = list(unique.values())
    raw_counts = _pair_diagnostic_counts(rows)
    unique_counts = _pair_diagnostic_counts(unique_rows)
    return {
        "schema_version": "combo_yield_pair_diagnostics_summary.v1",
        "files": files,
        "nearest_misses": _pair_diagnostic_nearest_misses(unique_rows),
        "summary": {
            "file_count": sum(1 for item in files if item.get("exists")),
            "row_count": len(rows),
            "unique_market_row_count": len(unique_rows),
            **raw_counts,
            "unique_status_counts": unique_counts["status_counts"],
            "unique_stage_counts": unique_counts["stage_counts"],
            "unique_reject_reason_counts": unique_counts["reject_reason_counts"],
            "unique_rejection_funnel": unique_counts["rejection_funnel"],
        },
    }


def _pair_diagnostic_key(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        f"{key}={_text(value)}"
        for key, value in sorted(row.items(), key=lambda item: str(item[0]))
        if str(key) not in {"account", "_account", "_accounts"}
    )


def _pair_diagnostic_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses: Counter[str] = Counter()
    stages: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    accounts: Counter[str] = Counter()
    symbols: Counter[str] = Counter()
    funnel: dict[str, Counter[str]] = {}
    funnel_reasons: dict[str, Counter[str]] = {}
    for row in rows:
        accepted = _bool_or_none(row.get("accepted"))
        status = "accepted" if accepted else "rejected" if accepted is False else "unknown"
        stage = _text(row.get("diagnostic_stage")) or "unknown"
        row_reasons = _pair_diagnostic_reasons(row)
        statuses[status] += 1
        stages[stage] += 1
        funnel.setdefault(stage, Counter()).update(("rows", status))
        funnel_reasons.setdefault(stage, Counter()).update(row_reasons)
        _count_text(accounts, row.get("_account") or row.get("account"))
        _count_text(symbols, _text(row.get("symbol")).upper())
        reasons.update(row_reasons)
    return {
        "status_counts": dict(statuses.most_common()),
        "stage_counts": dict(stages.most_common()),
        "reject_reason_counts": dict(reasons.most_common()),
        "account_counts": dict(accounts.most_common()),
        "symbol_counts": dict(symbols.most_common()),
        "rejection_funnel": [
            {
                "stage": stage,
                "row_count": counts["rows"],
                "accepted_count": counts["accepted"],
                "rejected_count": counts["rejected"],
                "unknown_count": counts["unknown"],
                "reject_reason_counts": dict(funnel_reasons[stage].most_common()),
            }
            for stage, counts in funnel.items()
        ],
    }


def _pair_diagnostic_nearest_misses(rows: list[dict[str, Any]], *, limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if _bool_or_none(row.get("accepted")) is not False:
            continue
        for reason in _pair_diagnostic_reasons(row):
            spec = _PAIR_DIAGNOSTIC_NEAREST_MISS_SPECS.get(reason)
            if spec is None:
                continue
            value_field, threshold_field, direction = spec
            value = _float_or_none(row.get(value_field))
            threshold = 0.0 if threshold_field is None else _float_or_none(row.get(threshold_field))
            if value is None or threshold is None:
                continue
            gap = threshold - value if direction == "below" else value - threshold
            if gap < -1e-12:
                continue
            out.setdefault(reason, []).append(
                {
                    "gap": max(0.0, gap),
                    "value": value,
                    "threshold": threshold,
                    "direction": direction,
                    "run_id": _text(row.get("run_id")) or None,
                    "accounts": sorted(row.get("_accounts") or {_text(row.get("_account")).lower()} - {""}),
                    "symbol": _text(row.get("symbol")).upper() or None,
                    "expiration": _text(row.get("expiration")) or None,
                    "diagnostic_stage": _text(row.get("diagnostic_stage")) or None,
                    "put_contract_symbol": _text(row.get("put_contract_symbol")) or None,
                    "call_contract_symbol": _text(row.get("call_contract_symbol")) or None,
                }
            )
    for reason, items in out.items():
        items.sort(
            key=lambda item: (
                float(item["gap"]),
                str(item.get("symbol") or ""),
                str(item.get("expiration") or ""),
                str(item.get("put_contract_symbol") or ""),
                str(item.get("call_contract_symbol") or ""),
            )
        )
        out[reason] = items[:limit]
    return dict(sorted(out.items()))


def _pair_diagnostic_reasons(row: dict[str, Any]) -> list[str]:
    raw = row.get("reject_reasons")
    values = raw if isinstance(raw, list) else _text(raw).split("|")
    return sorted({str(reason).strip() for reason in values if str(reason).strip()})


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    return None


def _trace_summary(path: Path, *, base: Path, limit: int) -> dict[str, Any]:
    account_hint = _account_hint(path)
    out: dict[str, Any] = {
        "path": _safe_rel(path, base=base),
        "exists": path.exists(),
        "account_hint": account_hint,
        "line_count": 0,
        "account_counts": {},
        "account_status_counts": {},
        "function_counts": {},
        "status_counts": {},
        "rule_counts": {},
        "symbol_counts": {},
        "tail_rows": [],
    }
    if not path.exists() or not path.is_file():
        return out
    function_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    rule_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    account_counts: Counter[str] = Counter()
    account_status_counts: dict[str, Counter[str]] = {}
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                text = line.strip()
                if not text:
                    continue
                out["line_count"] = int(out["line_count"]) + 1
                try:
                    row_raw = json.loads(text)
                except json.JSONDecodeError:
                    row_raw = {"raw": text[:1000]}
                row: dict[str, Any] = row_raw if isinstance(row_raw, dict) else {"raw": row_raw}
                account = str(row.get("account") or account_hint or "").strip().lower()
                status = str(row.get("status") or "").strip()
                _count_text(account_counts, account)
                if account and status:
                    account_status_counts.setdefault(account, Counter())[status] += 1
                _count_text(function_counts, row.get("function"))
                _count_text(status_counts, status)
                _count_text(rule_counts, row.get("rule"))
                _count_text(symbol_counts, row.get("symbol"))
                rows.append(_select_trace_fields(row))
    except Exception as exc:
        out["read_error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["function_counts"] = dict(function_counts.most_common(20))
    out["account_counts"] = dict(account_counts.most_common(20))
    out["account_status_counts"] = {
        account: dict(counter.most_common(20))
        for account, counter in sorted(account_status_counts.items())
    }
    out["status_counts"] = dict(status_counts.most_common(20))
    out["rule_counts"] = dict(rule_counts.most_common(30))
    out["symbol_counts"] = dict(symbol_counts.most_common(30))
    out["tail_rows"] = rows[-limit:] if limit > 0 else []
    return out


def _sealed_ranking_evidence(
    rank_rows: list[dict[str, Any]],
    *,
    limit: int,
    attribution: dict[str, Any],
) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rank_rows:
        key = (
            _text(row.get("account")).lower(),
            _text(row.get("strategy")) or "unknown",
        )
        grouped.setdefault(key, []).append(row)
    reports: list[dict[str, Any]] = []
    strategy_counts: Counter[str] = Counter()
    top_row_count = 0
    for (account, strategy), rows in sorted(grouped.items()):
        ordered = sorted(
            rows,
            key=lambda row: (
                int(_first_float(row, "rank") or 10**9),
                _text(row.get("candidate_pair_id")),
                _text(row.get("contract_symbol")),
            ),
        )
        top_rows = [_sealed_ranking_row(row) for row in ordered[:limit]]
        reports.append(
            {
                "source_kind": "sealed_candidate_snapshot",
                "exists": True,
                "account_hint": account or None,
                "strategy": strategy,
                "row_count": len(rows),
                "top_rows": top_rows,
            }
        )
        strategy_counts[strategy] += 1
        top_row_count += len(top_rows)
    return {
        "schema_version": "research_ranking_evidence.v1",
        "top_rows_per_report": limit,
        "attribution": {
            **attribution,
            "rank_source": "sealed_candidate_snapshot",
            "recomputed": False,
        },
        "reports": reports,
        "summary": {
            "report_count": len(reports),
            "top_row_count": top_row_count,
            "strategy_counts": dict(strategy_counts.most_common(20)),
            "cash_constraint_counts": {},
        },
    }


def _sealed_ranking_row(row: dict[str, Any]) -> dict[str, Any]:
    facts = row.get("sealed_facts")
    facts = facts if isinstance(facts, dict) else {}
    return {
        "rank": row.get("rank"),
        "account": row.get("account"),
        "strategy": row.get("strategy"),
        "mode": row.get("mode"),
        "symbol": row.get("symbol"),
        "contract_symbol": row.get("contract_symbol"),
        "option_type": _text(facts.get("option_type")).lower() or row.get("mode"),
        "expiration": _text(facts.get("expiration") or facts.get("exp")) or None,
        "strike": _first_float(facts, "strike"),
        "spot": _first_float(facts, "spot"),
        "metrics": _ranking_metrics(facts),
        "cash_constraint": _cash_constraint(facts),
        "configured_thresholds": {},
        "rank_explanation": row.get("rank_explanation"),
        "combo_yield_rank": (
            row.get("rank_explanation")
            if row.get("strategy") == "combo_yield"
            else None
        ),
    }


def _ranking_metrics(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "dte": _first_float(row, "dte"),
        "annualized_return": _first_float(
            row,
            "annualized_net_return_on_cash_basis",
            "annualized_net_premium_return",
            "annualized_net_return",
            "annualized_return",
            "annualized_net_credit_yield",
            "annualized_scenario_score",
        ),
        "net_income": _first_float(row, "net_income", "net_credit", "combo_net_credit"),
        "otm_pct": _first_float(row, "otm_pct", "put_otm_pct", "call_otm_pct", "strike_above_spot_pct"),
        "delta": _first_float(row, "delta", "put_delta", "call_delta"),
        "spread_ratio": _first_float(row, "spread_ratio", "combo_spread_ratio"),
        "open_interest": _first_float(row, "open_interest", "put_open_interest", "call_open_interest"),
        "volume": _first_float(row, "volume", "put_volume", "call_volume"),
        "current_score": _first_float(row, "strategy_score", "_strategy_score", "score", "premium_funding_score", "scenario_score"),
        "cash_required": _first_float(row, "cash_required", "cash_basis", "cash_required_cny", "cash_required_usd"),
    }


def _cash_constraint(row: dict[str, Any]) -> dict[str, Any]:
    required = _first_float(row, "cash_required_cny", "cash_required_usd", "cash_required", "cash_basis")
    free = _first_float(row, "cash_free_cny", "cash_free_total_cny", "cash_free_usd")
    out: dict[str, Any] = {
        key: _first_float(row, key)
        for key in (
            "cash_required_cny",
            "cash_required_usd",
            "cash_required",
            "cash_basis",
            "cash_free_cny",
            "cash_free_total_cny",
            "cash_free_usd",
        )
        if _first_float(row, key) is not None
    }
    reason = _text(row.get("cash_secured_unavailable_reason") or row.get("cash_requirement_unavailable_reason"))
    if reason:
        out["unavailable_reason"] = reason
    if required is not None and required > 0 and free is not None:
        out["cash_headroom_ratio"] = round(free / required, 6)
    return out


def _select_trace_fields(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("run_id", "account", "symbol", "function", "mode", "status", "stage", "rule", "metric_value", "threshold", "message", "evidence_path")
    return {key: row.get(key) for key in keys if row.get(key) not in (None, "")}


def _count_text(counter: Counter[str], value: Any) -> None:
    text = str(value or "").strip()
    if text:
        counter[text] += 1


def _account_hint(path: Path) -> str | None:
    parts = list(path.parts)
    for marker in ("accounts", "output_accounts"):
        if marker not in parts:
            continue
        idx = parts.index(marker)
        if idx + 1 < len(parts):
            account = str(parts[idx + 1]).strip().lower()
            return account or None
    return None


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().rstrip("%")
    if not text:
        return None
    try:
        parsed = float(text)
    except Exception:
        return None
    if str(value).strip().endswith("%"):
        return parsed / 100.0
    return parsed


def _first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        parsed = _float_or_none(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = path.resolve().as_posix()
        if key in seen:
            continue
        seen.add(key)
        out.append(path.resolve())
    return out


def _jsonl_tail(path: Path, *, base: Path, limit: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "path": _safe_rel(path, base=base),
        "exists": path.exists(),
        "rows": [],
        "line_count": 0,
    }
    if not path.exists() or not path.is_file() or limit <= 0:
        return out
    rows: list[Any] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                text = line.strip()
                if not text:
                    continue
                out["line_count"] += 1
                try:
                    item = json.loads(text)
                except json.JSONDecodeError:
                    item = {"raw": text[:1000]}
                rows.append(item)
    except Exception as exc:
        out["read_error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["rows"] = rows[-limit:]
    return out


def _resolve_under_base(value: Any, *, base: Path, default: Path) -> Path:
    raw = str(value or "").strip()
    if not raw or raw.startswith("..."):
        return default.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _resolve_runtime_path(value: Any, *, base: Path, fallback_root: Path | None = None) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("..."):
        if fallback_root is None:
            return None
        name = Path(raw).name
        return (fallback_root / name).resolve() if name else None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _safe_rel(path: Path, *, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return f".../{path.name}" if path.name else "..."


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    cur: Any = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _as_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(low, min(high, parsed))
