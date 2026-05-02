from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from domain.domain import (
    Decision,
    SchemaValidationError,
    decide_should_notify,
    normalize_pipeline_subprocess_output,
)
from domain.domain.engine import (
    AccountSchedulerDecisionView,
    build_failure_audit_fields,
    decide_account_scan_gate,
    decide_pipeline_execution_result,
)
from scripts.config_loader import resolve_watchlist_config, set_watchlist_config
from scripts.close_advice import run_close_advice
from scripts.infra.service import run_pipeline_script
from scripts.io_utils import utc_now
from scripts.multi_tick.misc import (
    AccountResult,
    _safe_runlog_data,
    ensure_account_output_dir,
    update_legacy_output_link,
)
from scripts.multi_tick.notify_format import flatten_auto_close_summary
from scripts.multi_tick.required_data_prefetch import prefetch_required_data
from src.application.position_maintenance import (
    format_auto_close_summary,
    run_expired_position_maintenance_for_account,
)

try:
    from domain.storage.repositories import run_repo, state_repo
except Exception:
    from scripts.domain.storage.repositories import run_repo, state_repo  # type: ignore


@dataclass(frozen=True)
class AccountRunRequest:
    acct: str
    base: Path
    base_cfg: dict[str, Any]
    cfg_path: Path
    vpy: Path
    markets_to_run: list[str]
    scheduler_ms: int
    scheduler_view: Any
    notify_decision_by_account: dict[str, AccountSchedulerDecisionView | None]
    should_run_global: bool
    reason_global: str
    run_id: str
    run_dir: Path
    shared_required: Path
    out_link: Path
    legacy_output_tmp_dir: Path
    accounts_root: Path
    prefetch_done: bool
    force_mode: bool = False
    allow_mutations: bool = True


@dataclass(frozen=True)
class AccountRunOutcome:
    result: AccountResult
    acct_metrics: dict[str, Any]
    prefetch_done: bool
    ran_pipeline: bool


def _close_advice_issue_breakdown(flag_counts: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    system_issue_keys = {
        "required_data_missing_expiration",
        "required_data_missing_contract",
        "required_data_fetch_error",
        "opend_fetch_error",
    }
    quality_issue_keys = {
        "missing_quote",
        "missing_mid",
        "opend_fetch_no_usable_quote",
        "invalid_spread",
        "spread_too_wide",
    }
    system = {key: int(flag_counts.get(key) or 0) for key in system_issue_keys}
    quality = {key: int(flag_counts.get(key) or 0) for key in quality_issue_keys}
    return system, quality


def _record_account_run_degraded(
    *,
    runlog,
    audit_fn: Callable[..., Any],
    run_id: str,
    account: str,
    action: str,
    exc: Exception,
    extra: dict[str, Any] | None = None,
) -> None:
    try:
        audit_kwargs: dict[str, Any] = {
            "run_id": run_id,
            "account": account,
            "status": "error",
            "message": str(exc),
        }
        if extra:
            audit_kwargs["extra"] = dict(extra)
        audit_fn("write", action, **audit_kwargs)
    except Exception:
        pass
    payload = {
        "account": account,
        "action": action,
        "error": str(exc),
    }
    if extra:
        payload.update(extra)
    runlog.safe_event(
        "account_run",
        "degraded",
        message=f"{action} failed for {account}: {exc}",
        data=_safe_runlog_data(payload),
    )


def _status_for_position_maintenance(result: dict[str, Any]) -> str:
    if str(result.get("mode") or "") == "skipped":
        return "skipped"
    if result.get("errors"):
        return "error"
    return "ok"


def _maintenance_notification_text(result: dict[str, Any]) -> str:
    summary_text = str(result.get("summary_text") or "").strip()
    if not summary_text:
        return ""
    return flatten_auto_close_summary(summary_text, always_show=False)


def _auto_close_grace_days_label(cfg: dict[str, Any]) -> str:
    option_positions = cfg.get("option_positions") if isinstance(cfg, dict) else {}
    auto_close = option_positions.get("auto_close") if isinstance(option_positions, dict) else {}
    if not isinstance(auto_close, dict):
        return "1"
    raw = auto_close.get("grace_days", 1)
    if isinstance(raw, bool):
        return "invalid"
    try:
        value = int(raw)
    except Exception:
        return "invalid"
    return str(value) if value >= 0 else "invalid"


def _position_maintenance_error_result(
    *,
    cfg: dict[str, Any],
    account: str,
    broker: Any,
    exc: Exception,
) -> dict[str, Any]:
    errors = [f"expired_position_maintenance failed for {account}: {type(exc).__name__}: {exc}"]
    result: dict[str, Any] = {
        "mode": "error",
        "account": account,
        "broker": str(broker or ""),
        "as_of_utc": utc_now(),
        "grace_days": _auto_close_grace_days_label(cfg),
        "max_close": None,
        "positions_checked": 0,
        "decisions": 0,
        "candidates_should_close": 0,
        "applied_closed": 0,
        "skipped_already_closed": 0,
        "errors": errors,
        "applied": [],
    }
    result["summary_text"] = format_auto_close_summary(result)
    return result


def run_one_account(
    *,
    request: AccountRunRequest,
    runlog,
    audit_fn: Callable[..., Any],
    fail_schema_validation: Callable[..., Any],
) -> AccountRunOutcome:
    acct = str(request.acct).strip()
    acct_out = request.accounts_root / acct
    acct_metrics = {
        "account": acct,
        "scheduler_ms": request.scheduler_ms,
        "pipeline_ms": None,
        "ran_scan": False,
        "should_notify": False,
        "meaningful": False,
        "reason": "",
    }
    ensure_account_output_dir(acct_out)

    try:
        update_legacy_output_link(request.out_link, acct_out, tmp_dir=request.legacy_output_tmp_dir)
    except RuntimeError as exc:
        raise SystemExit(str(exc))

    cfg = json.loads(json.dumps(request.base_cfg))
    cfg["config_source_path"] = str(request.cfg_path.resolve())
    cfg.setdefault("portfolio", {})
    cfg["portfolio"]["account"] = acct

    try:
        syms = resolve_watchlist_config(cfg)
        if request.markets_to_run:
            syms = [it for it in syms if isinstance(it, dict) and (it.get("broker") in request.markets_to_run)]
        set_watchlist_config(cfg, syms)
    except Exception:
        pass

    acct_report_dir = run_repo.get_run_account_dir(request.base, request.run_id, acct)
    acct_state_dir = run_repo.get_run_account_state_dir(request.base, request.run_id, acct)
    runtime_meta = cfg.get("_runtime")
    if not isinstance(runtime_meta, dict):
        runtime_meta = {}
        cfg["_runtime"] = runtime_meta
    runtime_meta["expired_position_maintenance_owner"] = "account_run"

    cfg_override = state_repo.write_account_state_json_text(
        request.base,
        acct,
        "config.override.json",
        cfg,
    )
    audit_fn("write", "write_account_state_json_text:config.override.json", run_id=request.run_id, account=acct)

    try:
        run_repo.ensure_run_account_state_dir(request.base, request.run_id, acct)
    except Exception as exc:
        _record_account_run_degraded(
            runlog=runlog,
            audit_fn=audit_fn,
            run_id=request.run_id,
            account=acct,
            action="ensure_run_account_state_dir",
            exc=exc,
        )

    def _write_acct_run_state(name: str, payload: dict[str, Any]) -> None:
        try:
            state_repo.write_account_run_state(request.base, request.run_id, acct, name, payload)
            audit_fn("write", f"write_account_run_state:{name}", run_id=request.run_id, account=acct)
        except Exception as exc:
            _record_account_run_degraded(
                runlog=runlog,
                audit_fn=audit_fn,
                run_id=request.run_id,
                account=acct,
                action=f"write_account_run_state:{name}",
                exc=exc,
            )

    def _write_account_metrics_state() -> None:
        payload = {
            "as_of_utc": utc_now(),
            "account": acct,
            "markets_to_run": request.markets_to_run,
            "scheduler_ms": acct_metrics.get("scheduler_ms"),
            "pipeline_ms": acct_metrics.get("pipeline_ms"),
            "ran_scan": acct_metrics.get("ran_scan"),
            "should_notify": acct_metrics.get("should_notify"),
            "meaningful": acct_metrics.get("meaningful"),
            "reason": acct_metrics.get("reason"),
            "notification_type": acct_metrics.get("notification_type"),
            "run_dir": str(request.run_dir),
        }
        _write_acct_run_state("account_metrics.json", payload)

    notif_path = (acct_report_dir / "symbols_notification.txt").resolve()
    maintenance_notification = ""

    should_notify_raw = decide_should_notify(
        account=acct,
        notify_decision_by_account=request.notify_decision_by_account,
        scheduler_decision=request.scheduler_view,
    )
    try:
        decision = Decision.from_payload(
            {
                "schema_kind": "decision",
                "schema_version": "1.0",
                "account": acct,
                "should_run": bool(request.should_run_global),
                "should_notify": bool(should_notify_raw),
                "reason": str(request.reason_global),
            }
        )
    except SchemaValidationError as e:
        fail_schema_validation(stage="decision", exc=e, run_id=request.run_id)
    should_run = bool(decision.should_run)
    should_notify = bool(decision.should_notify)
    reason = str(decision.reason)

    acct_metrics["should_notify"] = bool(should_notify)
    acct_metrics["reason"] = str(reason)

    _write_account_metrics_state()

    try:
        portfolio_cfg = cfg.get("portfolio") if isinstance(cfg, dict) else {}
        maintenance_result = run_expired_position_maintenance_for_account(
            base=request.base,
            cfg=cfg,
            account=acct,
            report_dir=acct_report_dir,
            broker=(portfolio_cfg.get("broker") if isinstance(portfolio_cfg, dict) else None),
            dry_run=(not bool(request.allow_mutations)),
        )
        maintenance_notification = _maintenance_notification_text(maintenance_result)
        maintenance_status = _status_for_position_maintenance(maintenance_result)
        audit_fn(
            "tool_call",
            "expired_position_maintenance",
            run_id=request.run_id,
            account=acct,
            status=maintenance_status,
            tool_name="expired_position_maintenance",
            extra={
                "mode": maintenance_result.get("mode"),
                "positions_checked": maintenance_result.get("positions_checked"),
                "candidates_should_close": maintenance_result.get("candidates_should_close"),
                "applied_closed": maintenance_result.get("applied_closed"),
                "skipped_already_closed": maintenance_result.get("skipped_already_closed"),
                "errors": len(maintenance_result.get("errors") or []),
            },
        )
        _write_acct_run_state("expired_position_maintenance.json", maintenance_result)
        if maintenance_status == "error":
            runlog.safe_event(
                "expired_position_maintenance",
                "error",
                message=f"expired position maintenance had errors for {acct}",
                data=_safe_runlog_data(
                    {
                        "account": acct,
                        "errors": len(maintenance_result.get("errors") or []),
                    }
                ),
            )
        elif int(maintenance_result.get("applied_closed") or 0) > 0:
            runlog.safe_event(
                "expired_position_maintenance",
                "ok",
                data=_safe_runlog_data(
                    {
                        "account": acct,
                        "applied_closed": int(maintenance_result.get("applied_closed") or 0),
                    }
                ),
            )
    except Exception as exc:
        portfolio_cfg = cfg.get("portfolio") if isinstance(cfg, dict) else {}
        maintenance_result = _position_maintenance_error_result(
            cfg=cfg,
            account=acct,
            broker=(portfolio_cfg.get("broker") if isinstance(portfolio_cfg, dict) else None),
            exc=exc,
        )
        maintenance_notification = _maintenance_notification_text(maintenance_result)
        _write_acct_run_state("expired_position_maintenance.json", maintenance_result)
        runlog.safe_event(
            "expired_position_maintenance",
            "error",
            message=f"expired position maintenance failed for {acct}",
            data=_safe_runlog_data(
                {
                    "account": acct,
                    "errors": len(maintenance_result.get("errors") or []),
                }
            ),
        )
        _record_account_run_degraded(
            runlog=runlog,
            audit_fn=audit_fn,
            run_id=request.run_id,
            account=acct,
            action="expired_position_maintenance",
            exc=exc,
        )

    scan_gate = decide_account_scan_gate(
        should_run=should_run,
        has_symbols=((not request.markets_to_run) or bool(resolve_watchlist_config(cfg))),
        reason=reason,
    )
    if not bool(scan_gate.get("run_pipeline")):
        result_should_notify = bool(should_notify or maintenance_notification)
        acct_metrics["ran_scan"] = bool(scan_gate.get("ran_scan"))
        acct_metrics["should_notify"] = result_should_notify
        acct_metrics["meaningful"] = bool(scan_gate.get("meaningful") or maintenance_notification)
        acct_metrics["reason"] = str(scan_gate.get("result_reason") or reason)
        if maintenance_notification:
            acct_metrics["notification_type"] = "auto_close"
        _write_account_metrics_state()
        return AccountRunOutcome(
            result=AccountResult(
                acct,
                bool(scan_gate.get("ran_scan")),
                result_should_notify,
                str(scan_gate.get("result_reason") or reason),
                maintenance_notification,
            ),
            acct_metrics=acct_metrics,
            prefetch_done=request.prefetch_done,
            ran_pipeline=False,
        )

    prefetch_done = bool(request.prefetch_done)
    should_prefetch = bool(request.force_mode) or (not prefetch_done)
    if should_prefetch:
        runlog.safe_event(
            "fetch_chain_cache",
            "start",
            data=_safe_runlog_data({"account": acct, "symbols_count": len(resolve_watchlist_config(cfg))}),
        )
        prefetch_stats = prefetch_required_data(
            vpy=request.vpy,
            base=request.base,
            cfg=cfg,
            shared_required=request.shared_required,
            force_refresh=bool(request.force_mode),
        )
        audit_fn(
            "tool_call",
            "required_data_prefetch",
            run_id=request.run_id,
            account=acct,
            status=("ok" if int(prefetch_stats.get("errors") or 0) == 0 else "error"),
            tool_name="required_data_prefetch",
            extra={"stats": {k: v for k, v in prefetch_stats.items() if k != "audit"}},
        )
        try:
            state_repo.write_account_run_state(
                request.base,
                request.run_id,
                acct,
                "required_data_prefetch_summary.json",
                prefetch_stats,
            )
            for item in (prefetch_stats.get("audit") or []):
                if isinstance(item, dict):
                    state_repo.append_run_audit_jsonl(
                        request.base,
                        request.run_id,
                        "tool_execution_audit.jsonl",
                        item,
                    )
                    audit_fn(
                        "tool_call",
                        "required_data_prefetch_item",
                        run_id=request.run_id,
                        account=acct,
                        status=("ok" if bool(item.get("ok")) else "error"),
                        tool_name=str(item.get("tool_name") or "required_data_prefetch"),
                        extra={"symbol": item.get("symbol"), "message": item.get("message")},
                    )
        except Exception as exc:
            _record_account_run_degraded(
                runlog=runlog,
                audit_fn=audit_fn,
                run_id=request.run_id,
                account=acct,
                action="write_required_data_prefetch_summary",
                exc=exc,
            )
        runlog.safe_event("fetch_chain_cache", "ok", data=_safe_runlog_data(prefetch_stats))
        prefetch_done = (False if bool(request.force_mode) else True)

    acct_report_dir.mkdir(parents=True, exist_ok=True)

    runlog.safe_event(
        "snapshot_batches",
        "start",
        data=_safe_runlog_data({"account": acct}),
    )

    t_pipe0 = monotonic()
    pipe = run_pipeline_script(
        vpy=request.vpy,
        base=request.base,
        config=cfg_override,
        report_dir=acct_report_dir,
        state_dir=acct_state_dir,
        shared_required_data=request.shared_required,
        shared_context_dir=run_repo.get_run_state_dir(request.base, request.run_id),
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(request.base)),
    )
    acct_metrics["pipeline_ms"] = int((monotonic() - t_pipe0) * 1000)
    audit_fn(
        "tool_call",
        "run_pipeline",
        run_id=request.run_id,
        account=acct,
        status=("ok" if pipe.returncode == 0 else "error"),
        tool_name="run_pipeline",
        extra={"duration_ms": acct_metrics["pipeline_ms"], "returncode": int(pipe.returncode)},
    )
    pipeline_tool_dto = normalize_pipeline_subprocess_output(
        returncode=pipe.returncode,
        stdout=pipe.stdout or "",
        stderr=pipe.stderr or "",
    )
    pipeline_result = decide_pipeline_execution_result(
        returncode=int(pipeline_tool_dto.get("returncode") or 0)
    )
    if not bool(pipeline_result.get("ok")):
        audit_fn(
            "tool_call",
            "run_pipeline_result",
            run_id=request.run_id,
            account=acct,
            status="error",
            tool_name="run_pipeline",
            extra=build_failure_audit_fields(
                failure_kind="io_error",
                failure_stage="run_pipeline",
                failure_adapter=str(pipeline_tool_dto.get("adapter") or "pipeline"),
            ),
        )
        runlog.safe_event(
            "snapshot_batches",
            "error",
            duration_ms=acct_metrics["pipeline_ms"],
            error_code="PIPELINE_FAILED",
            message=f"pipeline failed for {acct}",
            data=_safe_runlog_data({"account": acct, "returncode": pipe.returncode}),
        )
        out = ((pipe.stdout or "") + "\n" + (pipe.stderr or "")).strip()
        if out:
            tail = "\n".join(out.splitlines()[-60:])
            print(f"[ERR] pipeline failed ({acct})\n{tail}")
        result_should_notify = bool(should_notify or maintenance_notification)
        acct_metrics["ran_scan"] = bool(pipeline_result.get("ran_scan"))
        acct_metrics["should_notify"] = result_should_notify
        acct_metrics["meaningful"] = bool(pipeline_result.get("meaningful") or maintenance_notification)
        acct_metrics["reason"] = str(pipeline_result.get("reason") or "pipeline failed")
        if maintenance_notification:
            acct_metrics["notification_type"] = "auto_close"
        _write_account_metrics_state()
        return AccountRunOutcome(
            result=AccountResult(
                acct,
                bool(pipeline_result.get("ran_scan")),
                result_should_notify,
                str(pipeline_result.get("reason") or "pipeline failed"),
                maintenance_notification,
            ),
            acct_metrics=acct_metrics,
            prefetch_done=prefetch_done,
            ran_pipeline=False,
        )

    runlog.safe_event(
        "snapshot_batches",
        "ok",
        duration_ms=acct_metrics["pipeline_ms"],
        data=_safe_runlog_data({"account": acct}),
    )

    text = notif_path.read_text(encoding="utf-8", errors="replace").strip() if notif_path.exists() else ""

    try:
        run_repo.write_run_account_text(
            request.base,
            request.run_id,
            acct,
            "symbols_notification.txt",
            text + "\n",
        )
        audit_fn("write", "write_run_account_text:symbols_notification.txt", run_id=request.run_id, account=acct)
        if cfg_override.exists() and cfg_override.stat().st_size > 0:
            run_repo.copy_to_run_account(
                request.base,
                request.run_id,
                acct,
                cfg_override,
                "config.override.json",
            )
            audit_fn("write", "copy_to_run_account:config.override.json", run_id=request.run_id, account=acct)
    except Exception as exc:
        _record_account_run_degraded(
            runlog=runlog,
            audit_fn=audit_fn,
            run_id=request.run_id,
            account=acct,
            action="write_run_account_artifacts",
            exc=exc,
        )

    if maintenance_notification:
        text = (text.strip() + "\n\n" + maintenance_notification.strip()).strip()

    close_advice_cfg = (cfg.get("close_advice") or {}) if isinstance(cfg, dict) else {}
    if bool(close_advice_cfg.get("enabled", False)):
        try:
            close_result = run_close_advice(
                config=cfg,
                context_path=(acct_state_dir / "option_positions_context.json").resolve(),
                required_data_root=request.shared_required,
                output_dir=acct_report_dir,
                base_dir=request.base,
                markets_to_run=request.markets_to_run,
            )
            audit_fn(
                "tool_call",
                "close_advice",
                run_id=request.run_id,
                account=acct,
                status="ok",
                tool_name="close_advice",
                extra={
                    "rows": close_result.get("rows"),
                    "notify_rows": close_result.get("notify_rows"),
                    "quote_issue_rows": close_result.get("quote_issue_rows"),
                    "tier_counts": close_result.get("tier_counts"),
                    "flag_counts": close_result.get("flag_counts"),
                },
            )
            close_text_path = acct_report_dir / "close_advice.txt"
            close_text = close_text_path.read_text(encoding="utf-8", errors="replace").strip() if close_text_path.exists() else ""
            if close_text:
                text = (text.strip() + "\n\n" + close_text.strip()).strip()
            elif int(close_result.get("quote_issue_rows") or 0) > 0:
                flag_counts = close_result.get("flag_counts") if isinstance(close_result.get("flag_counts"), dict) else {}
                missing_quote = int(flag_counts.get("missing_quote") or 0)
                missing_mid = int(flag_counts.get("missing_mid") or 0)
                missing_expiration = int(flag_counts.get("required_data_missing_expiration") or 0)
                missing_contract = int(flag_counts.get("required_data_missing_contract") or 0)
                coverage_fetch_error = int(flag_counts.get("required_data_fetch_error") or 0)
                opend_fetch_error = int(flag_counts.get("opend_fetch_error") or 0)
                opend_fetch_no_usable_quote = int(flag_counts.get("opend_fetch_no_usable_quote") or 0)
                invalid_spread = int(flag_counts.get("invalid_spread") or 0)
                spread_too_wide = int(flag_counts.get("spread_too_wide") or 0)
                evaluation_gap_rows = int(close_result.get("evaluation_gap_rows") or 0)
                quote_issue_samples = close_result.get("quote_issue_samples") if isinstance(close_result.get("quote_issue_samples"), list) else []
                system_issues, quality_issues = _close_advice_issue_breakdown(flag_counts)
                system_issue_rows = sum(system_issues.values())
                quality_issue_rows = sum(quality_issues.values())
                suppress_quality_summary = (
                    system_issue_rows == 0
                    and quality_issue_rows == spread_too_wide
                    and spread_too_wide > 0
                    and missing_quote == 0
                    and missing_mid == 0
                    and missing_expiration == 0
                    and missing_contract == 0
                    and coverage_fetch_error == 0
                    and opend_fetch_error == 0
                    and opend_fetch_no_usable_quote == 0
                    and invalid_spread == 0
                )
                if suppress_quality_summary:
                    summary = f"### [{acct}] 平仓建议\n- 本次未生成 strong/medium 提醒\n"
                else:
                    summary = (
                        f"### [{acct}] 平仓建议\n"
                        f"- 本次未生成 strong/medium 提醒；系统异常 {system_issue_rows} 条，行情质量不足 {quality_issue_rows} 条\n"
                        f"- missing_quote={missing_quote} | missing_mid={missing_mid} | "
                        f"required_data_missing_expiration={missing_expiration} | required_data_missing_contract={missing_contract} | required_data_fetch_error={coverage_fetch_error} | "
                        f"opend_fetch_error={opend_fetch_error} | opend_fetch_no_usable_quote={opend_fetch_no_usable_quote} | "
                        f"invalid_spread={invalid_spread} | spread_too_wide={spread_too_wide}\n"
                        f"- 说明: evaluation_gap_rows={evaluation_gap_rows}；系统异常表示数据拉取/字段覆盖失败，行情质量不足表示有行情但定价可信度不够（如价差过大、无法形成可信 mid）\n"
                    )
                if quote_issue_samples and not suppress_quality_summary:
                    summary += f"- 样例: {' ; '.join(str(x) for x in quote_issue_samples[:3])}\n"
                text = (text.strip() + "\n\n" + summary.strip()).strip()
        except Exception as exc:
            audit_fn(
                "tool_call",
                "close_advice",
                run_id=request.run_id,
                account=acct,
                status="error",
                tool_name="close_advice",
                message=str(exc),
            )
            runlog.safe_event("close_advice", "error", message=f"close advice failed for {acct}: {exc}")

    acct_metrics["ran_scan"] = True
    acct_metrics["should_notify"] = bool(should_notify or maintenance_notification)
    acct_metrics["reason"] = str(reason)
    if maintenance_notification:
        acct_metrics["meaningful"] = True
        acct_metrics["notification_type"] = "auto_close"
    _write_account_metrics_state()
    return AccountRunOutcome(
        result=AccountResult(acct, True, bool(should_notify or maintenance_notification), reason, text),
        acct_metrics=acct_metrics,
        prefetch_done=prefetch_done,
        ran_pipeline=True,
    )
