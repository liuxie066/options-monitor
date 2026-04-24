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
from scripts.close_advice import run_close_advice
from scripts.config_loader import resolve_watchlist_config, set_watchlist_config
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


@dataclass(frozen=True)
class AccountRunOutcome:
    result: AccountResult
    acct_metrics: dict[str, Any]
    prefetch_done: bool
    ran_pipeline: bool


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
            syms = [it for it in syms if isinstance(it, dict) and (it.get("market") in request.markets_to_run)]
        set_watchlist_config(cfg, syms)
    except Exception:
        pass

    cfg_override = state_repo.write_account_state_json_text(
        request.base,
        acct,
        "config.override.json",
        cfg,
    )
    audit_fn("write", "write_account_state_json_text:config.override.json", run_id=request.run_id, account=acct)

    acct_report_dir = run_repo.get_run_account_dir(request.base, request.run_id, acct)
    acct_state_dir = run_repo.get_run_account_state_dir(request.base, request.run_id, acct)
    try:
        run_repo.ensure_run_account_state_dir(request.base, request.run_id, acct)
    except Exception:
        pass

    def _write_acct_run_state(name: str, payload: dict[str, Any]) -> None:
        try:
            state_repo.write_account_run_state(request.base, request.run_id, acct, name, payload)
            audit_fn("write", f"write_account_run_state:{name}", run_id=request.run_id, account=acct)
        except Exception:
            pass

    notif_path = (acct_report_dir / "symbols_notification.txt").resolve()

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

    _write_acct_run_state(
        "account_metrics.json",
        {
            "as_of_utc": utc_now(),
            "account": acct,
            "markets_to_run": request.markets_to_run,
            "scheduler_ms": acct_metrics.get("scheduler_ms"),
            "pipeline_ms": acct_metrics.get("pipeline_ms"),
            "ran_scan": acct_metrics.get("ran_scan"),
            "should_notify": acct_metrics.get("should_notify"),
            "meaningful": acct_metrics.get("meaningful"),
            "reason": acct_metrics.get("reason"),
            "run_dir": str(request.run_dir),
        },
    )

    scan_gate = decide_account_scan_gate(
        should_run=should_run,
        has_symbols=((not request.markets_to_run) or bool(resolve_watchlist_config(cfg))),
        reason=reason,
    )
    if not bool(scan_gate.get("run_pipeline")):
        acct_metrics["ran_scan"] = bool(scan_gate.get("ran_scan"))
        acct_metrics["meaningful"] = bool(scan_gate.get("meaningful"))
        acct_metrics["reason"] = str(scan_gate.get("result_reason") or reason)
        return AccountRunOutcome(
            result=AccountResult(
                acct,
                bool(scan_gate.get("ran_scan")),
                should_notify,
                str(scan_gate.get("result_reason") or reason),
                "",
            ),
            acct_metrics=acct_metrics,
            prefetch_done=request.prefetch_done,
            ran_pipeline=False,
        )

    prefetch_done = bool(request.prefetch_done)
    if not prefetch_done:
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
        except Exception:
            pass
        runlog.safe_event("fetch_chain_cache", "ok", data=_safe_runlog_data(prefetch_stats))
        prefetch_done = True

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
        acct_metrics["ran_scan"] = bool(pipeline_result.get("ran_scan"))
        acct_metrics["meaningful"] = bool(pipeline_result.get("meaningful"))
        acct_metrics["reason"] = str(pipeline_result.get("reason") or "pipeline failed")
        return AccountRunOutcome(
            result=AccountResult(
                acct,
                bool(pipeline_result.get("ran_scan")),
                should_notify,
                str(pipeline_result.get("reason") or "pipeline failed"),
                "",
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
    except Exception:
        pass

    auto_close_path = acct_report_dir / "auto_close_summary.txt"
    auto_close_text = auto_close_path.read_text(encoding="utf-8", errors="replace").strip() if auto_close_path.exists() else ""
    auto_close_flat = flatten_auto_close_summary(auto_close_text, always_show=False)
    if auto_close_flat:
        text = (text.strip() + "\n\n" + auto_close_flat.strip()).strip()

    close_advice_cfg = (cfg.get("close_advice") or {}) if isinstance(cfg, dict) else {}
    if bool(close_advice_cfg.get("enabled", False)):
        try:
            close_result = run_close_advice(
                config=cfg,
                context_path=(acct_state_dir / "option_positions_context.json").resolve(),
                required_data_root=request.shared_required,
                output_dir=acct_report_dir,
                base_dir=request.base,
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
                },
            )
            close_text_path = acct_report_dir / "close_advice.txt"
            close_text = close_text_path.read_text(encoding="utf-8", errors="replace").strip() if close_text_path.exists() else ""
            if close_text:
                text = (text.strip() + "\n\n" + close_text.strip()).strip()
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
    acct_metrics["should_notify"] = bool(should_notify)
    acct_metrics["reason"] = str(reason)
    return AccountRunOutcome(
        result=AccountResult(acct, True, should_notify, reason, text),
        acct_metrics=acct_metrics,
        prefetch_done=prefetch_done,
        ran_pipeline=True,
    )
