from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.ai_cofunder.redaction import redact_value


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
    strategy_evidence = _strategy_evidence(payload, source_paths=source_paths, base=base, tail_limit=tail_limit)

    scheduler_evidence = _normalize_scheduler_evidence(payload.get("scheduler_evidence"))
    evidence = {
        "schema_version": "ai_cofunder_evidence.v1",
        "collected_at_utc": now.isoformat().replace("+00:00", "Z"),
        "input": _safe_input_summary(payload),
        "deployment": _deployment_snapshot(base=base, config_path=config_path, cfg=cfg, mask_path=mask_path),
        "scheduler_evidence": scheduler_evidence,
        "runtime_status": runtime_data,
        "runtime_status_warnings": list(runtime_warnings),
        "audit_tails": audit_tails,
        "strategy_evidence": strategy_evidence,
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
        "openclaw_profile_path",
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
        "candidate_paths",
        "trace_paths",
        "strategy_replay_paths",
        "strategy_report_dir",
        "profile_path",
        "openclaw_profile_path",
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
    report_dir = _resolve_under_base(payload.get("strategy_report_dir") or payload.get("report_dir") or paths.get("report_dir"), base=base, default=base / "output" / "reports")
    shared_state_dir = _resolve_under_base(payload.get("shared_state_dir") or paths.get("shared_state_dir"), base=base, default=base / "output_shared" / "state")
    runs_root = _resolve_under_base(payload.get("runs_root") or paths.get("runs_root"), base=base, default=base / "output_runs")
    latest_run_path = _resolve_runtime_path(_nested(runtime_data, "latest_run", "path"), base=base, fallback_root=runs_root)
    latest_scanned_run_path = _resolve_runtime_path(_nested(runtime_data, "latest_scanned_run", "path"), base=base, fallback_root=runs_root)
    return {
        "report_dir": report_dir,
        "shared_state_dir": shared_state_dir,
        "runs_root": runs_root,
        "latest_run_dir": latest_run_path,
        "latest_scanned_run_dir": latest_scanned_run_path,
    }


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


def _strategy_evidence(payload: dict[str, Any], *, source_paths: dict[str, Path | None], base: Path, tail_limit: int) -> dict[str, Any]:
    candidate_paths = _explicit_paths(payload.get("candidate_paths") or payload.get("candidate_path"), base=base)
    trace_paths = _explicit_paths(payload.get("trace_paths") or payload.get("trace_path"), base=base)
    replay_paths = _explicit_paths(payload.get("strategy_replay_paths") or payload.get("strategy_replay_path") or payload.get("replay_path"), base=base)
    reject_log_paths: list[Path] = []
    for directory in _strategy_dirs(source_paths, base=base):
        found_candidates, found_reject_logs = _candidate_and_reject_log_paths(directory)
        candidate_paths.extend(found_candidates)
        reject_log_paths.extend(found_reject_logs)
        trace_paths.append(directory / "candidate_filter_trace.jsonl")
        replay_paths.extend(_glob_many(directory, ("strategy_replay.csv", "strategy_replay.json", "strategy_replay.jsonl")))

    explicit_reject_logs = [path for path in candidate_paths if _is_reject_log_path(path)]
    reject_log_paths.extend(explicit_reject_logs)
    candidate_paths = _unique_paths([path for path in candidate_paths if _is_candidate_report_path(path)])[:30]
    reject_log_paths = _unique_paths(reject_log_paths)[:30]
    trace_paths = _unique_paths(trace_paths)[:20]
    replay_paths = _unique_paths(replay_paths)[:20]
    candidate_reports = [_candidate_csv_summary(path, base=base) for path in candidate_paths]
    reject_logs = [_reject_log_summary(path, base=base) for path in reject_log_paths]
    filter_traces = [_trace_summary(path, base=base, limit=tail_limit) for path in trace_paths]
    replay_reports = [_replay_summary(path, base=base, limit=tail_limit) for path in replay_paths]
    total_candidate_rows = sum(int(item.get("row_count") or 0) for item in candidate_reports if item.get("exists"))
    total_reject_rows = sum(int(item.get("row_count") or 0) for item in reject_logs if item.get("exists"))
    return {
        "schema_version": "ai_cofunder_strategy_evidence.v1",
        "candidate_reports": candidate_reports,
        "reject_logs": reject_logs,
        "filter_traces": filter_traces,
        "strategy_replay": replay_reports,
        "summary": {
            "candidate_file_count": sum(1 for item in candidate_reports if item.get("exists")),
            "candidate_row_count": total_candidate_rows,
            "reject_log_file_count": sum(1 for item in reject_logs if item.get("exists")),
            "reject_log_row_count": total_reject_rows,
            "filter_trace_file_count": sum(1 for item in filter_traces if item.get("exists")),
            "strategy_replay_file_count": sum(1 for item in replay_reports if item.get("exists")),
            "evidence_level": "candidate_and_trace" if total_candidate_rows and any(item.get("exists") for item in filter_traces) else ("candidate_only" if total_candidate_rows else "limited"),
        },
    }


def _strategy_dirs(source_paths: dict[str, Path | None], *, base: Path) -> list[Path]:
    dirs: list[Path] = []
    for key in ("report_dir", "latest_run_dir", "latest_scanned_run_dir"):
        path = source_paths.get(key)
        if path is not None:
            dirs.append(path)
            accounts_dir = path / "accounts"
            if accounts_dir.exists() and accounts_dir.is_dir():
                dirs.extend(item for item in accounts_dir.iterdir() if item.is_dir())
    return _unique_paths([path for path in dirs if _is_under_base(path, base=base)])


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
        if _is_under_base(path, base=base):
            out.append(path)
    return out


def _glob_many(directory: Path, patterns: tuple[str, ...]) -> list[Path]:
    if not directory.exists() or not directory.is_dir():
        return []
    out: list[Path] = []
    for pattern in patterns:
        out.extend(path.resolve() for path in directory.glob(pattern) if path.is_file())
    return out


def _candidate_and_reject_log_paths(directory: Path) -> tuple[list[Path], list[Path]]:
    candidate_like = _glob_many(directory, ("*sell_put_candidates*.csv", "*sell_call_candidates*.csv", "*yield_enhancement_candidates*.csv"))
    reject_like = _glob_many(directory, ("*reject_log.csv",))
    candidates = [path for path in candidate_like if _is_candidate_report_path(path)]
    reject_logs = [path for path in [*candidate_like, *reject_like] if _is_reject_log_path(path)]
    return candidates, reject_logs


def _is_candidate_report_path(path: Path) -> bool:
    name = path.name.lower()
    if _is_reject_log_path(path):
        return False
    return (
        "sell_put_candidates" in name
        or "sell_call_candidates" in name
        or "yield_enhancement_candidates" in name
    ) and name.endswith(".csv")


def _is_reject_log_path(path: Path) -> bool:
    name = path.name.lower()
    return "reject_log" in name and name.endswith(".csv")


def _candidate_csv_summary(path: Path, *, base: Path) -> dict[str, Any]:
    account_hint = _account_hint(path)
    out: dict[str, Any] = {
        "path": _safe_rel(path, base=base),
        "exists": path.exists(),
        "account_hint": account_hint,
        "row_count": 0,
        "columns": [],
        "sample_rows": [],
        "metric_ranges": {},
        "account_counts": {},
        "strategy_counts": {},
        "symbol_counts": {},
    }
    if not path.exists() or not path.is_file():
        return out
    metric_values: dict[str, list[float]] = {}
    account_counts: Counter[str] = Counter()
    strategy_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            reader = csv.DictReader(fh)
            out["columns"] = list(reader.fieldnames or [])
            samples: list[dict[str, Any]] = []
            for row in reader:
                out["row_count"] = int(out["row_count"]) + 1
                if len(samples) < 5:
                    samples.append(_select_candidate_fields(row))
                _count_text(account_counts, row.get("account") or account_hint)
                _count_text(strategy_counts, row.get("strategy") or row.get("mode") or _strategy_hint(path))
                _count_text(symbol_counts, row.get("symbol"))
                _collect_metric_values(row, metric_values)
            out["sample_rows"] = samples
            out["metric_ranges"] = _metric_ranges(metric_values)
            out["account_counts"] = dict(account_counts.most_common(20))
            out["strategy_counts"] = dict(strategy_counts.most_common(20))
            out["symbol_counts"] = dict(symbol_counts.most_common(30))
    except Exception as exc:
        out["read_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _reject_log_summary(path: Path, *, base: Path) -> dict[str, Any]:
    account_hint = _account_hint(path)
    out: dict[str, Any] = {
        "path": _safe_rel(path, base=base),
        "exists": path.exists(),
        "account_hint": account_hint,
        "row_count": 0,
        "columns": [],
        "account_counts": {},
        "stage_counts": {},
        "reason_counts": {},
        "symbol_counts": {},
    }
    if not path.exists() or not path.is_file():
        return out
    stage_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    account_counts: Counter[str] = Counter()
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            reader = csv.DictReader(fh)
            out["columns"] = list(reader.fieldnames or [])
            for row in reader:
                out["row_count"] = int(out["row_count"]) + 1
                stage = str(row.get("engine_reject_stage") or row.get("reject_stage") or "").strip()
                reason = str(row.get("engine_reject_reason") or row.get("reject_rule") or row.get("reject_reason") or "").strip()
                symbol = str(row.get("symbol") or row.get("underlying_symbol") or "").strip().upper()
                _count_text(account_counts, row.get("account") or account_hint)
                if stage:
                    stage_counts[stage] += 1
                if reason:
                    reason_counts[reason] += 1
                if symbol:
                    symbol_counts[symbol] += 1
            out["account_counts"] = dict(account_counts.most_common(20))
            out["stage_counts"] = dict(stage_counts.most_common(10))
            out["reason_counts"] = dict(reason_counts.most_common(10))
            out["symbol_counts"] = dict(symbol_counts.most_common(10))
    except Exception as exc:
        out["read_error"] = f"{type(exc).__name__}: {exc}"
    return out


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


def _replay_summary(path: Path, *, base: Path, limit: int) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _candidate_csv_summary(path, base=base)
    if suffix == ".jsonl":
        return _jsonl_tail(path, base=base, limit=limit)
    out: dict[str, Any] = {"path": _safe_rel(path, base=base), "exists": path.exists()}
    if not path.exists() or not path.is_file():
        return out
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        out["read_error"] = f"{type(exc).__name__}: {exc}"
        return out
    rows = payload if isinstance(payload, list) else _nested(payload if isinstance(payload, dict) else {}, "rows")
    out["row_count"] = len(rows) if isinstance(rows, list) else None
    out["sample_rows"] = rows[:5] if isinstance(rows, list) else []
    return out


def _select_candidate_fields(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "symbol",
        "account",
        "strategy",
        "mode",
        "option_type",
        "expiration",
        "dte",
        "delta",
        "strike",
        "spot",
        "annualized_return",
        "annualized_net_return",
        "net_income",
        "score",
        "strategy_score",
        "status",
        "filter_reason",
    )
    return {key: row.get(key) for key in keys if row.get(key) not in (None, "")}


def _select_trace_fields(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("run_id", "account", "symbol", "function", "mode", "status", "stage", "rule", "metric_value", "threshold", "message", "evidence_path")
    return {key: row.get(key) for key in keys if row.get(key) not in (None, "")}


def _collect_metric_values(row: dict[str, Any], values: dict[str, list[float]]) -> None:
    for key in ("dte", "delta", "annualized_return", "annualized_net_return", "net_income", "score", "strategy_score"):
        parsed = _float_or_none(row.get(key))
        if parsed is not None:
            values.setdefault(key, []).append(parsed)


def _metric_ranges(values: dict[str, list[float]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, items in values.items():
        if not items:
            continue
        out[key] = {"min": min(items), "max": max(items), "avg": sum(items) / len(items)}
    return out


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


def _strategy_hint(path: Path) -> str | None:
    name = path.name.lower()
    if "yield_enhancement" in name:
        return "yield_enhancement"
    if "sell_call" in name:
        return "sell_call"
    if "sell_put" in name:
        return "sell_put"
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


def _is_under_base(path: Path, *, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


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
