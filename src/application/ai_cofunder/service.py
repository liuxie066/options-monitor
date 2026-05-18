from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.ai_cofunder.checks import run_deterministic_checks
from src.application.ai_cofunder.evidence import collect_evidence, redacted_evidence


SCHEMA_VERSION = "ai_cofunder.v1"
BUNDLE_SCHEMA_VERSION = "ai_cofunder_bundle.v1"
SCOPES = {"ledger", "account-strategy", "quality", "strategy", "full"}


def ai_cofunder_tool(
    payload: dict[str, Any],
    *,
    runtime_status_tool_fn: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str], dict[str, Any]]],
    load_runtime_config: Callable[..., tuple[Path, dict[str, Any]]],
    repo_base: Callable[[], Path],
    mask_path: Callable[[Any], str | None],
    healthcheck_tool_fn: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str], dict[str, Any]]] | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    base = repo_base().resolve()
    scope = _scope(payload.get("scope"))
    evidence, warnings, meta = collect_evidence(
        payload,
        runtime_status_tool_fn=runtime_status_tool_fn,
        load_runtime_config=load_runtime_config,
        repo_base=repo_base,
        mask_path=mask_path,
        now_fn=now_fn,
    )
    safe_evidence = redacted_evidence(evidence)
    diagnosis = run_deterministic_checks(safe_evidence)
    healthcheck_snapshot, healthcheck_warnings, healthcheck_meta = _healthcheck_snapshot(
        payload,
        scope=scope,
        healthcheck_tool_fn=healthcheck_tool_fn,
    )
    warnings.extend(healthcheck_warnings)
    safe_healthcheck_snapshot = redacted_evidence(healthcheck_snapshot)
    bundle = _build_bundle(
        scope=scope,
        evidence=safe_evidence,
        diagnosis=diagnosis,
        healthcheck_snapshot=safe_healthcheck_snapshot,
    )
    handoff_markdown = render_ai_cofunder_handoff(bundle)
    outputs = _write_outputs(
        payload,
        base=base,
        scope=scope,
        bundle=bundle,
        handoff_markdown=handoff_markdown,
        now_fn=now_fn,
    )

    data = {
        "schema_version": SCHEMA_VERSION,
        "status": str(diagnosis.get("status") or "warn"),
        "category": str(diagnosis.get("category") or "insufficient_evidence"),
        "scope": scope,
        "bundle": bundle,
        "handoff_markdown": handoff_markdown if _include_handoff(payload) else "",
        "outputs": outputs,
        "summary": {
            "status": str(diagnosis.get("status") or "warn"),
            "category": str(diagnosis.get("category") or "insufficient_evidence"),
            "finding_count": diagnosis.get("summary", {}).get("finding_count"),
            "ledger_status": _nested(bundle, "ledger_quality", "status"),
            "account_strategy_status": _nested(bundle, "account_strategy_matrix", "status"),
            "healthcheck_status": _nested(bundle, "healthcheck_snapshot", "status"),
        },
    }
    meta.update({"outputs": outputs, "scope": scope, "healthcheck": healthcheck_meta})
    return data, warnings, meta


def _build_bundle(
    *,
    scope: str,
    evidence: dict[str, Any],
    diagnosis: dict[str, Any],
    healthcheck_snapshot: dict[str, Any],
) -> dict[str, Any]:
    deployment = _dict(evidence.get("deployment"))
    runtime = _dict(evidence.get("runtime_status"))
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "manifest": {
            "collected_at_utc": evidence.get("collected_at_utc"),
            "scope": scope,
            "version": deployment.get("version"),
            "git_commit": deployment.get("git_commit"),
            "git_branch": deployment.get("git_branch"),
            "config_key": deployment.get("config_key"),
            "config_digest": deployment.get("config_digest"),
            "accounts": list(deployment.get("accounts") or []),
            "redacted": True,
        },
        "ledger_quality": _ledger_quality(runtime) if scope in {"ledger", "full"} else {"status": "skipped", "reason": f"scope={scope}"},
        "account_strategy_matrix": _account_strategy_matrix(evidence) if scope in {"account-strategy", "strategy", "full"} else {"status": "skipped", "reason": f"scope={scope}"},
        "runtime_quality": _runtime_quality(runtime=runtime, diagnosis=diagnosis) if scope in {"quality", "full"} else {"status": "skipped", "reason": f"scope={scope}"},
        "healthcheck_snapshot": healthcheck_snapshot,
        "strategy_evidence": evidence.get("strategy_evidence") if scope in {"account-strategy", "strategy", "full"} else {"status": "skipped", "reason": f"scope={scope}"},
        "scheduler_evidence": evidence.get("scheduler_evidence"),
        "source_refs": evidence.get("source_refs") or {},
        "audit_tails": evidence.get("audit_tails") or {},
    }


def _ledger_quality(runtime: dict[str, Any]) -> dict[str, Any]:
    trade_summary = _dict(_dict(runtime.get("trade_intake")).get("summary"))
    failed = _as_int(trade_summary.get("failed_count"))
    unresolved = _as_int(trade_summary.get("unresolved_count"))
    position_summary = {
        "auto_close_status": _nested(runtime, "summary", "auto_close_expired_status"),
    }
    problem_count = failed + unresolved
    status = "ok" if problem_count == 0 else "warn"
    return {
        "status": status,
        "available": bool(trade_summary or any(value is not None for value in position_summary.values())),
        "trade_intake": {
            "failed_count": failed,
            "unresolved_count": unresolved,
            "raw_summary": trade_summary,
        },
        "position_summary": position_summary,
        "known_gap": "Detailed trade_events -> position_lots invariant evidence is not collected yet.",
    }


def _account_strategy_matrix(evidence: dict[str, Any]) -> dict[str, Any]:
    runtime = _dict(evidence.get("runtime_status"))
    tick_metrics = _dict(_nested(runtime, "latest_run", "state", "tick_metrics", "json"))
    raw_accounts = tick_metrics.get("accounts")
    accounts_raw = raw_accounts if isinstance(raw_accounts, list) else []
    accounts: dict[str, Any] = {}
    for item in accounts_raw:
        if not isinstance(item, dict):
            continue
        account = str(item.get("account") or item.get("name") or "").strip().lower()
        if not account:
            continue
        accounts[account] = {
            "status": item.get("status"),
            "ran_scan": item.get("ran_scan"),
            "should_notify": item.get("should_notify"),
            "reason": item.get("reason"),
            "strategy_note": "Use candidate/filter trace evidence to distinguish market candidates from account-level filtering.",
        }
    strategy = _dict(evidence.get("strategy_evidence"))
    strategy_accounts = _strategy_account_summaries(strategy)
    for account, summary in strategy_accounts.items():
        accounts.setdefault(account, {"strategy_note": "Inferred from strategy evidence paths or rows."})
        accounts[account]["strategy_evidence"] = summary
    return {
        "status": "ok" if accounts else "warn",
        "accounts": accounts,
        "strategy_summary": strategy.get("summary") or {},
        "known_gap": "Per-account before/after strategy filter counts are not fully normalized yet.",
    }


def _strategy_account_summaries(strategy: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, dict[str, Any]] = {}
    for report in _list_of_dicts(strategy.get("candidate_reports")):
        for account, count in _dict(report.get("account_counts")).items():
            item = out.setdefault(str(account).lower(), {"candidate_rows": 0, "reject_log_rows": 0, "trace_rows": 0, "trace_status_counts": {}})
            item["candidate_rows"] = _as_int(item.get("candidate_rows")) + _as_int(count)
    for report in _list_of_dicts(strategy.get("reject_logs")):
        for account, count in _dict(report.get("account_counts")).items():
            item = out.setdefault(str(account).lower(), {"candidate_rows": 0, "reject_log_rows": 0, "trace_rows": 0, "trace_status_counts": {}})
            item["reject_log_rows"] = _as_int(item.get("reject_log_rows")) + _as_int(count)
    for trace in _list_of_dicts(strategy.get("filter_traces")):
        for account, count in _dict(trace.get("account_counts")).items():
            item = out.setdefault(str(account).lower(), {"candidate_rows": 0, "reject_log_rows": 0, "trace_rows": 0, "trace_status_counts": {}})
            item["trace_rows"] = _as_int(item.get("trace_rows")) + _as_int(count)
        status_by_account = _dict(trace.get("account_status_counts"))
        for account, counts in status_by_account.items():
            item = out.setdefault(str(account).lower(), {"candidate_rows": 0, "reject_log_rows": 0, "trace_rows": 0, "trace_status_counts": {}})
            status_counts = _dict(item.get("trace_status_counts"))
            for status, count in _dict(counts).items():
                status_counts[str(status)] = _as_int(status_counts.get(str(status))) + _as_int(count)
            item["trace_status_counts"] = status_counts
    return out


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _runtime_quality(*, runtime: dict[str, Any], diagnosis: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(diagnosis.get("status") or "warn"),
        "category": str(diagnosis.get("category") or "insufficient_evidence"),
        "summary": runtime.get("summary") or {},
        "freshness": runtime.get("freshness") or {},
        "findings": diagnosis.get("findings") or [],
    }


def _healthcheck_snapshot(
    payload: dict[str, Any],
    *,
    scope: str,
    healthcheck_tool_fn: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str], dict[str, Any]]] | None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    if scope not in {"quality", "full"}:
        return {"status": "skipped", "included": False, "reason": f"scope={scope}"}, [], {"included": False}
    if not _truthy(payload.get("include_healthcheck")):
        return {"status": "skipped", "included": False, "reason": "include_healthcheck=false"}, [], {"included": False}
    if healthcheck_tool_fn is None:
        warning = "healthcheck_snapshot_unavailable: healthcheck tool function was not provided"
        return {"status": "warn", "included": False, "reason": warning}, [warning], {"included": False}

    healthcheck_payload = _healthcheck_payload(payload)
    data, warnings, meta = healthcheck_tool_fn(healthcheck_payload)
    safe_warnings = [_redacted_text_warning(item) for item in warnings]
    summary = _dict(data.get("summary"))
    status = _healthcheck_status(summary=summary, warnings=safe_warnings)
    return (
        {
            "status": status,
            "included": True,
            "summary": summary,
            "config": data.get("config") or {},
            "account_paths": data.get("account_paths") or {},
            "checks": data.get("checks") or [],
            "warnings": safe_warnings,
        },
        [f"healthcheck_snapshot: {item}" for item in safe_warnings],
        {"included": True, **_dict(meta)},
    )


def _redacted_text_warning(value: Any) -> str:
    redacted = redacted_evidence({"warning": str(value)})
    return str(redacted.get("warning") or "")


def _healthcheck_payload(payload: dict[str, Any]) -> dict[str, Any]:
    keys = ("config_key", "config_path", "accounts", "data_config", "timeout_sec")
    return {key: payload[key] for key in keys if key in payload}


def _healthcheck_status(*, summary: dict[str, Any], warnings: list[str]) -> str:
    critical_count = _as_int(summary.get("critical_count"))
    warning_count = _as_int(summary.get("warning_count"))
    if summary.get("ok") is False or critical_count > 0:
        return "fail"
    if warnings or warning_count > 0:
        return "warn"
    return "ok"


def render_ai_cofunder_handoff(bundle: dict[str, Any]) -> str:
    manifest = _dict(bundle.get("manifest"))
    runtime_quality = _dict(bundle.get("runtime_quality"))
    ledger_quality = _dict(bundle.get("ledger_quality"))
    account_strategy = _dict(bundle.get("account_strategy_matrix"))
    healthcheck = _dict(bundle.get("healthcheck_snapshot"))
    strategy = _dict(bundle.get("strategy_evidence"))
    strategy_summary = _dict(strategy.get("summary"))
    ranking = _dict(strategy.get("ranking_evidence"))
    ranking_summary = _dict(ranking.get("summary"))
    lines = [
        "## AI Cofunder Handoff",
        f"Scope: {manifest.get('scope')}",
        f"Version: {manifest.get('version')}",
        f"Git Commit: {manifest.get('git_commit')}",
        f"Config Key: {manifest.get('config_key')}",
        f"Accounts: {', '.join(str(item) for item in manifest.get('accounts') or [])}",
        "",
        "## Ledger Quality",
        f"- status: {ledger_quality.get('status')}",
        f"- failed_trades: {_nested(ledger_quality, 'trade_intake', 'failed_count')}",
        f"- unresolved_trades: {_nested(ledger_quality, 'trade_intake', 'unresolved_count')}",
        f"- auto_close: {_nested(ledger_quality, 'position_summary', 'auto_close_status')}",
        f"- gap: {ledger_quality.get('known_gap')}",
        "",
        "## Account Strategy Matrix",
        f"- status: {account_strategy.get('status')}",
        f"- accounts: {', '.join(sorted(_dict(account_strategy.get('accounts')).keys())) or '<none>'}",
        f"- candidate_rows: {strategy_summary.get('candidate_row_count')}",
        f"- reject_log_rows: {strategy_summary.get('reject_log_row_count')}",
        f"- filter_trace_files: {strategy_summary.get('filter_trace_file_count')}",
        f"- ranking_reports: {ranking_summary.get('report_count')}",
        f"- ranking_top_rows: {ranking_summary.get('top_row_count')}",
        f"- gap: {account_strategy.get('known_gap')}",
        "",
        *_render_ranking_evidence_lines(ranking),
        "## Runtime Quality",
        f"- status: {runtime_quality.get('status')}",
        f"- category: {runtime_quality.get('category')}",
        f"- latest_status: {_nested(runtime_quality, 'summary', 'latest_status')}",
        f"- freshness: {_nested(runtime_quality, 'freshness', 'status')}",
        "",
        "## Healthcheck Snapshot",
        f"- status: {healthcheck.get('status')}",
        f"- included: {healthcheck.get('included')}",
        f"- critical_count: {_nested(healthcheck, 'summary', 'critical_count')}",
        f"- warning_count: {_nested(healthcheck, 'summary', 'warning_count')}",
        "",
        "## Codex Next Questions",
        "1. Is the ledger trustworthy enough to make sell_call and YE decisions?",
        "2. Did any account-level cash, holding, or cost-basis rule suppress a strategy unexpectedly?",
        "3. Is an observed strategy gap caused by expected account constraints or state contamination?",
        "4. Which local replay or focused test should verify the next change?",
        "",
        "## Privacy",
        "This bundle is redacted before handoff. Do not treat missing raw logs as proof that no online error occurred.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _render_ranking_evidence_lines(ranking: dict[str, Any]) -> list[str]:
    summary = _dict(ranking.get("summary"))
    lines = [
        "## Ranking Evidence",
        f"- reports: {summary.get('report_count')}",
        f"- top_rows: {summary.get('top_row_count')}",
        f"- strategies: {_format_counts(_dict(summary.get('strategy_counts')))}",
        f"- cash_constraints: {_format_counts(_dict(summary.get('cash_constraint_counts')))}",
    ]
    rows = _ranking_handoff_rows(ranking, limit=8)
    if not rows:
        lines.extend(["", "Top rows: <none>", ""])
        return lines
    lines.extend(["", "Top current-order rows:"])
    for row in rows:
        metrics = _dict(row.get("metrics"))
        cash = _dict(row.get("cash_constraint"))
        explanation = _dict(row.get("rank_explanation"))
        drivers = explanation.get("primary_drivers")
        driver_text = ",".join(str(item) for item in drivers) if isinstance(drivers, list) and drivers else None
        score = explanation.get("strategy_score")
        if score is None:
            score = metrics.get("current_score")
        lines.append(
            "- "
            f"{row.get('account') or '-'} / {row.get('strategy') or '-'} / {row.get('symbol') or '-'} "
            f"K={_fmt(row.get('strike'))} exp={row.get('expiration') or '-'} "
            f"DTE={_fmt(metrics.get('dte'))} ann={_fmt(metrics.get('annualized_return'))} "
            f"net={_fmt(metrics.get('net_income'))} otm={_fmt(metrics.get('otm_pct'))} "
            f"delta={_fmt(metrics.get('delta'))} spread={_fmt(metrics.get('spread_ratio'))} "
            f"oi={_fmt(metrics.get('open_interest'))} vol={_fmt(metrics.get('volume'))} "
            f"cash_headroom={_fmt(cash.get('cash_headroom_ratio'))} score={_fmt(score)} "
            f"drivers={driver_text or '-'}"
        )
    lines.append("")
    return lines


def _ranking_handoff_rows(ranking: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    reports = ranking.get("reports")
    if not isinstance(reports, list):
        return out
    for report in reports:
        if not isinstance(report, dict):
            continue
        rows = report.get("top_rows")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
                if len(out) >= limit:
                    return out
    return out


def _format_counts(counts: dict[str, Any]) -> str:
    if not counts:
        return "<none>"
    return ", ".join(f"{key}={value}" for key, value in counts.items())


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value).lower()
    try:
        return f"{float(value):.6g}"
    except Exception:
        text = str(value).strip()
        return text if text else "-"


def _write_outputs(
    payload: dict[str, Any],
    *,
    base: Path,
    scope: str,
    bundle: dict[str, Any],
    handoff_markdown: str,
    now_fn: Callable[[], datetime] | None,
) -> dict[str, Any]:
    if not _truthy(payload.get("write_outputs")):
        return {"written": False}
    now = (now_fn or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    output_dir = _resolve_output_path(payload.get("ai_cofunder_output_dir") or payload.get("output_dir"), base=base, default=base / "output_shared" / "ai_cofunder")
    current_dir = _resolve_output_path(payload.get("ai_cofunder_current_dir"), base=base, default=base / "output_shared" / "state" / "current")
    output_dir.mkdir(parents=True, exist_ok=True)
    current_dir.mkdir(parents=True, exist_ok=True)

    config_key = str(_nested(bundle, "manifest", "config_key") or _nested(bundle, "manifest", "scope") or "runtime").lower()
    run_id = str(_nested(bundle, "runtime_quality", "summary", "latest_run_path") or now.strftime("%Y%m%dT%H%M%SZ")).rstrip("/").split("/")[-1]
    stem = f"ai-cofunder-{scope}-{config_key}-{run_id}".replace("/", "_")
    bundle_path = output_dir / f"{stem}.bundle.json"
    handoff_path = output_dir / f"{stem}.md"
    current_path = current_dir / "ai_cofunder.current.json"
    _write_json(bundle_path, bundle)
    handoff_path.write_text(handoff_markdown, encoding="utf-8")
    current_payload = {
        "schema_version": "ai_cofunder_current.v1",
        "scope": scope,
        "status": _nested(bundle, "runtime_quality", "status"),
        "bundle_path": _relative(bundle_path, base=base),
        "handoff_path": _relative(handoff_path, base=base),
    }
    _write_json(current_path, current_payload)
    return {
        "written": True,
        "bundle_path": _relative(bundle_path, base=base),
        "handoff_path": _relative(handoff_path, base=base),
        "current_path": _relative(current_path, base=base),
    }


def _scope(value: Any) -> str:
    scope = str(value or "full").strip().lower()
    if scope not in SCOPES:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"unsupported ai-cofunder scope: {scope}",
            hint=f"Use one of: {', '.join(sorted(SCOPES))}.",
        )
    return scope


def _resolve_output_path(value: Any, *, base: Path, default: Path) -> Path:
    raw = str(value or "").strip()
    path = default.resolve() if not raw else Path(raw).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    else:
        path = path.resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="ai-cofunder output directories must stay under the repo root",
            details={"path": _relative(path, base=base)},
        ) from exc
    return path


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nested(payload: Any, *keys: str) -> Any:
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _include_handoff(payload: dict[str, Any]) -> bool:
    output = str(payload.get("output") or "handoff").strip().lower()
    return output in {"handoff", "both", "markdown", "md"}


def _relative(path: Path, *, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return f".../{path.name}" if path.name else "..."


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
