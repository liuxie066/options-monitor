from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from domain.domain.engine import (
    AccountSchedulerDecisionView,
    build_failure_audit_fields,
    decide_account_scan_gate,
    decide_pipeline_execution_result,
)
from domain.domain.intermediate_objects import Decision, SchemaValidationError
from domain.domain.multi_tick import decide_should_notify
from domain.domain.symbol_identity import symbol_market
from domain.domain.tool_boundary import normalize_pipeline_subprocess_output
from src.application.config_sections import (
    resolve_watchlist_config,
    set_watchlist_config,
)
from src.application.config_loader import resolve_data_config_path
from src.application.close_advice_runner import run_close_advice
from src.application.position_advice_account_sources import (
    PositionAdviceAccountSourceError,
    publish_account_position_advice_sources,
    publish_or_reuse_account_portfolio_source,
)
from src.application.position_advice_runner import (
    run_position_advice_v2_from_account_run,
)
from src.application.prepared_option_positions_context import (
    PreparedOptionPositionsContextError,
    load_prepared_option_positions_context,
)
from src.application.prepared_portfolio_context import (
    load_prepared_portfolio_context,
)
from src.application.symbol_mutations import normalize_symbol_read
from src.infrastructure.external_services import run_pipeline_script
from src.infrastructure.io_utils import utc_now
from src.application.multi_tick.misc import (
    AccountResult,
    _safe_runlog_data,
    ensure_account_output_dir,
)
from src.application.tick_run_workspace import (
    AccountRunConfigAuthority,
    AccountRunConfigError,
    load_account_run_config,
    load_retained_account_run_config,
)

from domain.storage.repositories import run_repo, state_repo


@dataclass(frozen=True)
class AccountRunRequest:
    acct: str
    base: Path
    account_config_authority: AccountRunConfigAuthority
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
    accounts_root: Path
    prefetch_done: bool
    force_mode: bool = False
    allow_mutations: bool = True
    allow_notifications: bool = True
    prefetch_lock: Any | None = None
    prefetch_state: dict[str, Any] | None = None
    scan_decision_by_account: dict[str, dict[str, Any]] | None = None
    repo_root: Path | None = None
    symbols_arg: str | None = None
    required_data_snapshot_manifest: Path | None = None
    prepared_portfolio_context_manifest: Path | None = None
    prepared_portfolio_context_manifest_sha256: str | None = None
    prepared_option_positions_context_manifest: Path | None = None
    prepared_option_positions_context_manifest_sha256: str | None = None
    required_data_snapshot_status: str | None = None
    required_data_snapshot_sha256: str | None = None
    close_advice_required_data_plan: Path | None = None
    account_config_generation_frozen: bool = False


@dataclass(frozen=True)
class AccountRunOutcome:
    result: AccountResult
    acct_metrics: dict[str, Any]
    prefetch_done: bool
    ran_pipeline: bool


def _pipeline_account_config_error_code(output: str) -> str | None:
    match = re.search(
        r"\[CONFIG_ERROR\][^\r\n]*\b(ACCOUNT_CONFIG_[A-Z0-9_]+)\b",
        str(output or ""),
    )
    return match.group(1) if match is not None else None


def _close_advice_issue_breakdown(flag_counts: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    system_issue_keys = {
        "close_advice_plan_unavailable",
        "required_data_position_not_planned",
        "required_data_symbol_config_missing",
        "required_data_symbol_source_unsupported",
        "required_data_route_conflict",
        "required_data_symbol_not_planned",
        "required_data_snapshot_unavailable",
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


def _resolve_account_scan_decision(
    *,
    account: str,
    scan_decision_by_account: dict[str, dict[str, Any]] | None,
    should_run_global: bool,
    reason_global: str,
) -> tuple[bool, str]:
    should_run = bool(should_run_global)
    reason = str(reason_global)
    decisions = scan_decision_by_account if isinstance(scan_decision_by_account, dict) else {}
    account_key = str(account or "").strip()
    raw = decisions.get(account_key) or decisions.get(account_key.lower())
    if isinstance(raw, dict):
        if "should_run" in raw:
            should_run = bool(raw.get("should_run"))
        if "reason" in raw:
            reason = str(raw.get("reason") or "")
    return should_run, reason


def _symbol_whitelist(symbols_arg: str | None, *, cfg: dict[str, Any]) -> set[str] | None:
    if not str(symbols_arg or "").strip():
        return None
    out = {
        normalize_symbol_read(item, config=cfg)
        for item in str(symbols_arg or "").split(",")
        if str(item).strip()
    }
    return {item for item in out if item} or None


def _position_advice_markets(
    cfg: dict[str, Any],
    requested: list[str],
) -> list[str]:
    explicit = sorted(
        {
            str(item or "").strip().upper()
            for item in requested
            if str(item or "").strip().upper() in {"US", "HK"}
        }
    )
    if explicit:
        return explicit
    inferred = sorted(
        {
            str(symbol_market(item.get("symbol")) or "").strip().upper()
            for item in resolve_watchlist_config(cfg)
            if isinstance(item, dict)
            and str(symbol_market(item.get("symbol")) or "").strip().upper()
            in {"US", "HK"}
        }
    )
    return inferred or ["HK", "US"]


def publish_current_run_portfolio_source(
    *,
    cfg: dict[str, Any],
    account_run_id: str,
    account: str,
    markets_to_run: list[str],
    account_state_dir: Path,
    prepared_portfolio_context: dict[str, Any],
    completed_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Publish/reuse the current-run identity receipt from frozen inputs only."""

    portfolio_cfg = (
        cfg.get("portfolio")
        if isinstance(cfg.get("portfolio"), dict)
        else {}
    )
    try:
        return publish_or_reuse_account_portfolio_source(
            account_run_id=account_run_id,
            normalized_account=str(account).strip().lower(),
            broker=str(portfolio_cfg.get("broker") or "futu"),
            included_markets=_position_advice_markets(cfg, markets_to_run),
            account_state_dir=account_state_dir,
            portfolio_context=prepared_portfolio_context,
            completed_at=completed_at,
        )
    except PositionAdviceAccountSourceError as exc:
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_PORTFOLIO_SOURCE_INVALID",
            str(exc),
        ) from exc


def build_account_runtime_config(
    *,
    base_cfg: dict[str, Any],
    cfg_path: Path,
    account: str,
    markets_to_run: list[str],
    symbols_arg: str | None = None,
) -> dict[str, Any]:
    """Build the exact account-scoped config shared by barrier and pipeline."""

    cfg = json.loads(json.dumps(base_cfg))
    cfg["config_source_path"] = str(Path(cfg_path).resolve())
    cfg.setdefault("portfolio", {})
    cfg["portfolio"]["account"] = str(account).strip().lower()
    try:
        symbols = resolve_watchlist_config(cfg)
        if markets_to_run:
            symbols = [
                item
                for item in symbols
                if isinstance(item, dict) and item.get("broker") in markets_to_run
            ]
        whitelist = _symbol_whitelist(symbols_arg, cfg=cfg)
        if whitelist is not None:
            symbols = [
                item
                for item in symbols
                if isinstance(item, dict)
                and normalize_symbol_read(item.get("symbol"), config=cfg) in whitelist
            ]
        set_watchlist_config(cfg, symbols)
    except Exception:
        pass
    return cfg


def run_one_account(
    *,
    request: AccountRunRequest,
    runlog,
    audit_fn: Callable[..., Any],
    fail_schema_validation: Callable[..., Any],
) -> AccountRunOutcome:
    acct = str(request.acct).strip().lower()
    repo_root = (request.repo_root or request.base).resolve()
    config_loader = (
        load_retained_account_run_config
        if request.account_config_generation_frozen
        else load_account_run_config
    )
    cfg = config_loader(
        authority=request.account_config_authority,
        base=request.base,
        run_id=request.run_id,
        account=acct,
    )
    cfg_override = request.account_config_authority.state_path
    acct_out = request.accounts_root / acct
    acct_metrics = {
        "account": acct,
        "account_config_sha256": request.account_config_authority.account_config_sha256,
        "scheduler_ms": request.scheduler_ms,
        "pipeline_ms": None,
        "ran_scan": False,
        "ran_pipeline": False,
        "should_notify": False,
        "meaningful": False,
        "reason": "",
    }
    ensure_account_output_dir(acct_out)

    acct_report_dir = run_repo.get_run_account_dir(request.base, request.run_id, acct)
    acct_state_dir = run_repo.get_run_account_state_dir(request.base, request.run_id, acct)
    audit_fn(
        "read",
        "validate_run_account_config",
        run_id=request.run_id,
        account=acct,
        status="ok",
        extra={
            "account_config_sha256": request.account_config_authority.account_config_sha256,
            "state_path": str(cfg_override),
            "compatibility_path": str(request.account_config_authority.compatibility_path),
        },
    )

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
            "run_id": request.run_id,
            "account": acct,
            "markets_to_run": request.markets_to_run,
            "scheduler_ms": acct_metrics.get("scheduler_ms"),
            "pipeline_ms": acct_metrics.get("pipeline_ms"),
            "pipeline_started_at_utc": acct_metrics.get(
                "pipeline_started_at_utc"
            ),
            "ran_scan": acct_metrics.get("ran_scan"),
            "ran_pipeline": acct_metrics.get("ran_pipeline"),
            "should_notify": acct_metrics.get("should_notify"),
            "meaningful": acct_metrics.get("meaningful"),
            "reason": acct_metrics.get("reason"),
            "notification_type": acct_metrics.get("notification_type"),
            "run_dir": str(request.run_dir),
            "snapshot_status": request.required_data_snapshot_status,
            "snapshot_manifest_sha256": request.required_data_snapshot_sha256,
            "account_config_sha256": request.account_config_authority.account_config_sha256,
        }
        _write_acct_run_state("account_metrics.json", payload)

    def _prepared_option_integrity_failure(
        exc: Exception,
    ) -> AccountRunOutcome:
        failure_reason = "prepared_option_context_integrity_failed"
        acct_metrics["ran_scan"] = True
        acct_metrics["ran_pipeline"] = False
        acct_metrics["should_notify"] = False
        acct_metrics["meaningful"] = False
        acct_metrics["reason"] = failure_reason
        acct_metrics["error"] = str(exc)
        _write_account_metrics_state()
        audit_fn(
            "account_run",
            "prepared_option_context_integrity",
            run_id=request.run_id,
            account=acct,
            status="error",
            message=str(exc),
            extra={"error_type": type(exc).__name__},
        )
        runlog.safe_event(
            "prepared_option_positions_context",
            "error",
            error_code="PREPARED_OPTION_CONTEXT_INTEGRITY_FAILED",
            message=str(exc),
            data={"account": acct},
        )
        return AccountRunOutcome(
            result=AccountResult(
                account=acct,
                ran_scan=True,
                should_notify=False,
                decision_reason=failure_reason,
                notification_text="",
            ),
            acct_metrics=acct_metrics,
            prefetch_done=prefetch_done,
            ran_pipeline=False,
        )

    notif_path = (acct_report_dir / "symbols_notification.txt").resolve()

    notify_decisions: dict[str, bool | dict[str, Any] | AccountSchedulerDecisionView] = {}
    for key, value in (request.notify_decision_by_account or {}).items():
        if value is not None:
            notify_decisions[key] = value
    should_notify_raw = decide_should_notify(
        account=acct,
        notify_decision_by_account=notify_decisions,
        scheduler_decision=request.scheduler_view,
    )
    scan_should_run, scan_reason = _resolve_account_scan_decision(
        account=acct,
        scan_decision_by_account=request.scan_decision_by_account,
        should_run_global=request.should_run_global,
        reason_global=request.reason_global,
    )
    try:
        decision = Decision.from_payload(
            {
                "schema_kind": "decision",
                "schema_version": "1.0",
                "account": acct,
                "should_run": bool(scan_should_run),
                "should_notify": bool(should_notify_raw),
                "reason": str(scan_reason),
            }
        )
    except SchemaValidationError as e:
        fail_schema_validation(stage="decision", exc=e, run_id=request.run_id)
        raise
    should_run = bool(decision.should_run)
    should_notify = bool(decision.should_notify)
    reason = str(decision.reason)

    acct_metrics["should_notify"] = bool(should_notify)
    acct_metrics["reason"] = str(reason)

    _write_account_metrics_state()

    scan_gate = decide_account_scan_gate(
        should_run=should_run,
        has_symbols=((not request.markets_to_run) or bool(resolve_watchlist_config(cfg))),
        reason=reason,
    )
    if not bool(scan_gate.get("run_pipeline")):
        result_should_notify = bool(should_notify)
        acct_metrics["ran_scan"] = bool(scan_gate.get("ran_scan"))
        acct_metrics["should_notify"] = result_should_notify
        acct_metrics["meaningful"] = bool(scan_gate.get("meaningful"))
        acct_metrics["reason"] = str(scan_gate.get("result_reason") or reason)
        _write_account_metrics_state()
        return AccountRunOutcome(
            result=AccountResult(
                acct,
                bool(scan_gate.get("ran_scan")),
                result_should_notify,
                str(scan_gate.get("result_reason") or reason),
                "",
            ),
            acct_metrics=acct_metrics,
            prefetch_done=request.prefetch_done,
            ran_pipeline=False,
        )

    prefetch_done = bool(request.prefetch_done)
    acct_report_dir.mkdir(parents=True, exist_ok=True)

    runlog.safe_event(
        "snapshot_batches",
        "start",
        data=_safe_runlog_data({"account": acct}),
    )

    t_pipe0 = monotonic()
    acct_metrics["pipeline_started_at_utc"] = utc_now()
    pipe = run_pipeline_script(
        vpy=request.vpy,
        base=repo_root,
        config=cfg_override,
        report_dir=acct_report_dir,
        state_dir=acct_state_dir,
        shared_required_data=request.shared_required,
        shared_context_dir=run_repo.get_run_state_dir(request.base, request.run_id),
        symbols_arg=request.symbols_arg,
        position_advice_account_run_id=request.run_id,
        required_data_snapshot_manifest=request.required_data_snapshot_manifest,
        prepared_portfolio_context_manifest=(
            request.prepared_portfolio_context_manifest
        ),
        prepared_portfolio_context_manifest_sha256=(
            request.prepared_portfolio_context_manifest_sha256
        ),
        prepared_option_positions_context_manifest=(
            request.prepared_option_positions_context_manifest
        ),
        prepared_option_positions_context_manifest_sha256=(
            request.prepared_option_positions_context_manifest_sha256
        ),
        account_config_base=request.base,
        account_config_run_id=request.run_id,
        account_config_account=acct,
        account_config_compatibility_path=(
            request.account_config_authority.compatibility_path
        ),
        account_config_sha256=(
            request.account_config_authority.account_config_sha256
        ),
        account_config_canonical_bytes=(
            request.account_config_authority.canonical_bytes
        ),
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(repo_root)),
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
        output_text = ((pipe.stdout or "") + "\n" + (pipe.stderr or "")).strip()
        typed_config_code = _pipeline_account_config_error_code(output_text)
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
            error_code=typed_config_code or "PIPELINE_FAILED",
            message=f"pipeline failed for {acct}",
            data=_safe_runlog_data({"account": acct, "returncode": pipe.returncode}),
        )
        if output_text:
            tail = "\n".join(output_text.splitlines()[-60:])
            print(f"[ERR] pipeline failed ({acct})\n{tail}")
        result_should_notify = False if typed_config_code else bool(should_notify)
        acct_metrics["ran_scan"] = (
            False if typed_config_code else bool(pipeline_result.get("ran_scan"))
        )
        acct_metrics["should_notify"] = result_should_notify
        acct_metrics["meaningful"] = (
            False if typed_config_code else bool(pipeline_result.get("meaningful"))
        )
        result_reason = (
            typed_config_code.lower()
            if typed_config_code
            else str(pipeline_result.get("reason") or "pipeline failed")
        )
        acct_metrics["reason"] = result_reason
        if typed_config_code:
            acct_metrics["typed_reason"] = result_reason
            acct_metrics["error_code"] = typed_config_code
        _write_account_metrics_state()
        return AccountRunOutcome(
            result=AccountResult(
                acct,
                False if typed_config_code else bool(pipeline_result.get("ran_scan")),
                result_should_notify,
                result_reason,
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

    prepared_option_context: dict[str, Any] | None = None
    if request.prepared_option_positions_context_manifest is not None:
        try:
            prepared_option_context = load_prepared_option_positions_context(
                manifest_path=(
                    request.prepared_option_positions_context_manifest
                ),
                expected_base=request.base,
                expected_run_id=request.run_id,
                expected_account=acct,
                expected_account_config_sha256=(
                    request.account_config_authority.account_config_sha256
                ),
                expected_manifest_sha256=(
                    request.prepared_option_positions_context_manifest_sha256
                ),
                expected_runtime_config=cfg,
            )
        except PreparedOptionPositionsContextError as exc:
            return _prepared_option_integrity_failure(exc)

    position_advice_sources: dict[str, Any] | None = None
    try:
        prepared_portfolio_context: dict[str, Any] | None = None
        if request.prepared_portfolio_context_manifest is not None:
            prepared_portfolio_context = load_prepared_portfolio_context(
                manifest_path=request.prepared_portfolio_context_manifest,
                expected_base=request.base,
                expected_run_id=request.run_id,
                expected_account=acct,
                expected_account_config_sha256=(
                    request.account_config_authority.account_config_sha256
                ),
                expected_manifest_sha256=(
                    request.prepared_portfolio_context_manifest_sha256
                ),
                expected_runtime_config=cfg,
            )
            if not isinstance(prepared_portfolio_context, dict):
                raise RuntimeError(
                    "prepared portfolio context is unavailable"
                )
        portfolio_cfg = (
            cfg.get("portfolio")
            if isinstance(cfg.get("portfolio"), dict)
            else {}
        )
        data_config_path = resolve_data_config_path(
            base=request.base,
            data_config=portfolio_cfg.get("data_config"),
        )
        prepared_fx_payload = (
            (
                prepared_option_context.get("exchange_rates")
                if isinstance(
                    prepared_option_context.get("exchange_rates"),
                    dict,
                )
                else {}
            )
            if isinstance(prepared_option_context, dict)
            else None
        )
        position_advice_sources = publish_account_position_advice_sources(
            account_run_root=acct_report_dir,
            account_state_dir=acct_state_dir,
            quote_producer_root=request.shared_required,
            data_config_path=data_config_path,
            account_run_id=request.run_id,
            account=acct,
            broker=str(portfolio_cfg.get("broker") or "futu"),
            included_markets=_position_advice_markets(
                cfg,
                request.markets_to_run,
            ),
            decision_state_snapshot_override=(
                prepared_option_context.get("decision_state_snapshot")
                if isinstance(prepared_option_context, dict)
                else None
            ),
            portfolio_context_override=prepared_portfolio_context,
            # An unavailable prepared observation is an explicit empty fact;
            # never fall back to an unrelated account-local rate cache.
            fx_payload_override=prepared_fx_payload,
        )
        source_summary = {
            key: value
            for key, value in position_advice_sources.items()
            if key
            not in {
                "decision_state_snapshot",
                "cash_capacity",
                "share_coverage",
            }
        }
        source_summary["decision_state_fingerprint"] = (
            position_advice_sources["decision_state_snapshot"].get(
                "decision_state_fingerprint"
            )
        )
        source_summary["cash_capacity_status"] = (
            position_advice_sources["cash_capacity"].get("status")
        )
        source_summary["share_coverage_symbol_count"] = len(
            (
                position_advice_sources["share_coverage"].get("by_symbol")
                or {}
            )
        )
        _write_acct_run_state(
            "position_advice_sources.v2.json",
            source_summary,
        )
        audit_fn(
            "write",
            "position_advice_sources_v2",
            run_id=request.run_id,
            account=acct,
            status="ok",
            extra={
                "portfolio_account_identity_hash": (
                    position_advice_sources[
                        "portfolio_account_identity_hash"
                    ]
                ),
                "decision_state_fingerprint": source_summary[
                    "decision_state_fingerprint"
                ],
            },
        )
    except Exception as exc:
        _record_account_run_degraded(
            runlog=runlog,
            audit_fn=audit_fn,
            run_id=request.run_id,
            account=acct,
            action="position_advice_sources_v2",
            exc=exc,
        )

    if position_advice_sources is not None:
        try:
            position_advice_result = run_position_advice_v2_from_account_run(
                base=request.base,
                account_run_root=acct_report_dir,
                account_run_id=request.run_id,
                account=acct,
                broker=str(position_advice_sources["broker"]),
                included_markets=position_advice_sources[
                    "included_markets"
                ],
                normalized_portfolio_source=str(
                    position_advice_sources[
                        "normalized_portfolio_source"
                    ]
                ),
                portfolio_account_identity_hash=str(
                    position_advice_sources[
                        "portfolio_account_identity_hash"
                    ]
                ),
                capacity_pool_authority_id=(
                    str(
                        position_advice_sources[
                            "capacity_pool_authority_id"
                        ]
                    )
                    if position_advice_sources.get(
                        "capacity_pool_authority_id"
                    )
                    else None
                ),
                source_receipts=position_advice_sources[
                    "source_receipts"
                ],
                data_config_path=data_config_path,
                decision_state_snapshot_override=(
                    prepared_option_context.get(
                        "decision_state_snapshot"
                    )
                    if isinstance(prepared_option_context, dict)
                    else None
                ),
            )
            _write_acct_run_state(
                "position_advice_v2_run.json",
                position_advice_result,
            )
            audit_fn(
                "write",
                "position_advice_v2",
                run_id=request.run_id,
                account=acct,
                status=(
                    "ok"
                    if position_advice_result.get("status")
                    in {"published", "skipped_v1_authority"}
                    else "error"
                ),
                extra={
                    "result_status": position_advice_result.get("status"),
                    "authority_mode": position_advice_result.get(
                        "authority_mode"
                    ),
                    "portfolio_plan_id": position_advice_result.get(
                        "portfolio_plan_id"
                    ),
                    "current_switched": position_advice_result.get(
                        "current_switched"
                    ),
                },
            )
        except Exception as exc:
            _record_account_run_degraded(
                runlog=runlog,
                audit_fn=audit_fn,
                run_id=request.run_id,
                account=acct,
                action="position_advice_v2",
                exc=exc,
            )

    text = notif_path.read_text(encoding="utf-8", errors="replace").strip() if notif_path.exists() else ""

    close_advice_cfg = (cfg.get("close_advice") or {}) if isinstance(cfg, dict) else {}
    if bool(close_advice_cfg.get("enabled", False)):
        if request.prepared_option_positions_context_manifest is not None:
            try:
                prepared_option_context = (
                    load_prepared_option_positions_context(
                        manifest_path=(
                            request.prepared_option_positions_context_manifest
                        ),
                        expected_base=request.base,
                        expected_run_id=request.run_id,
                        expected_account=acct,
                        expected_account_config_sha256=(
                            request.account_config_authority.account_config_sha256
                        ),
                        expected_manifest_sha256=(
                            request.prepared_option_positions_context_manifest_sha256
                        ),
                        expected_runtime_config=cfg,
                    )
                )
            except PreparedOptionPositionsContextError as exc:
                return _prepared_option_integrity_failure(exc)
        try:
            close_advice_run_cfg = cfg
            if isinstance(cfg, dict):
                cfg_copy = dict(cfg)
                close_advice_cfg_copy = dict(close_advice_cfg)
                close_advice_cfg_copy.setdefault("render_style", "compact")
                cfg_copy["close_advice"] = close_advice_cfg_copy
                close_advice_run_cfg = cfg_copy
            raw_close_result = run_close_advice(
                config=close_advice_run_cfg,
                context_path=(acct_state_dir / "option_positions_context.json").resolve(),
                required_data_root=request.shared_required,
                output_dir=acct_report_dir,
                base_dir=request.base,
                markets_to_run=request.markets_to_run,
                required_data_snapshot_manifest=(
                    request.required_data_snapshot_manifest
                ),
                required_data_snapshot_run_id=request.run_id,
                close_advice_required_data_plan=(
                    request.close_advice_required_data_plan
                ),
                account=acct,
            )
            close_result: dict[str, Any] = raw_close_result if isinstance(raw_close_result, dict) else {}
            snapshot_authority_invalid = (
                str(close_result.get("snapshot_authority") or "")
                .strip()
                .lower()
                == "invalid"
            )
            audit_fn(
                "tool_call",
                "close_advice",
                run_id=request.run_id,
                account=acct,
                status=("error" if snapshot_authority_invalid else "ok"),
                tool_name="close_advice",
                extra={
                    "result_status": close_result.get("status"),
                    "snapshot_authority": close_result.get(
                        "snapshot_authority"
                    ),
                    "rows": close_result.get("rows"),
                    "notify_rows": close_result.get("notify_rows"),
                    "quote_issue_rows": close_result.get("quote_issue_rows"),
                    "tier_counts": close_result.get("tier_counts"),
                    "flag_counts": close_result.get("flag_counts"),
                },
            )
            if snapshot_authority_invalid:
                failure_reason = (
                    "required_data_snapshot_integrity_failed"
                )
                acct_metrics["ran_scan"] = True
                acct_metrics["ran_pipeline"] = False
                acct_metrics["should_notify"] = False
                acct_metrics["meaningful"] = False
                acct_metrics["reason"] = failure_reason
                acct_metrics["close_advice_status"] = close_result.get(
                    "status"
                )
                _write_account_metrics_state()
                runlog.safe_event(
                    "close_advice",
                    "error",
                    error_code="REQUIRED_DATA_SNAPSHOT_INTEGRITY_FAILED",
                    message=(
                        f"frozen required-data authority failed for {acct}"
                    ),
                    data=_safe_runlog_data(
                        {
                            "account": acct,
                            "reason": failure_reason,
                            "integrity_failure": close_result.get(
                                "integrity_failure"
                            ),
                        }
                    ),
                )
                return AccountRunOutcome(
                    result=AccountResult(
                        acct,
                        True,
                        False,
                        failure_reason,
                        "",
                    ),
                    acct_metrics=acct_metrics,
                    prefetch_done=prefetch_done,
                    ran_pipeline=False,
                )
            close_text_path = acct_report_dir / "close_advice.txt"
            close_text = close_text_path.read_text(encoding="utf-8", errors="replace").strip() if close_text_path.exists() else ""
            if close_text:
                text = (text.strip() + "\n\n" + close_text.strip()).strip()
            elif int(close_result.get("quote_issue_rows") or 0) > 0:
                flag_counts_raw = close_result.get("flag_counts")
                flag_counts: dict[str, Any] = flag_counts_raw if isinstance(flag_counts_raw, dict) else {}
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

    try:
        run_repo.write_run_account_text(
            request.base,
            request.run_id,
            acct,
            "symbols_notification.txt",
            text + "\n",
        )
        audit_fn("write", "write_run_account_text:symbols_notification.txt", run_id=request.run_id, account=acct)
    except Exception as exc:
        _record_account_run_degraded(
            runlog=runlog,
            audit_fn=audit_fn,
            run_id=request.run_id,
            account=acct,
            action="write_run_account_artifacts",
            exc=exc,
        )

    acct_metrics["ran_scan"] = True
    acct_metrics["ran_pipeline"] = True
    acct_metrics["should_notify"] = bool(should_notify)
    acct_metrics["reason"] = str(reason)
    _write_account_metrics_state()
    return AccountRunOutcome(
        result=AccountResult(acct, True, bool(should_notify), reason, text),
        acct_metrics=acct_metrics,
        prefetch_done=prefetch_done,
        ran_pipeline=True,
    )
