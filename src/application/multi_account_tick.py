from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from src.infrastructure.io_utils import (
    utc_now,
)
from src.infrastructure.run_log import RunLogger
from src.application.account_config import accounts_from_config
from src.application.config_sections import resolve_watchlist_config
from domain.domain.fetch_source import resolve_symbol_fetch_source
from domain.domain.symbol_identity import canonical_symbol

from src.application.multi_tick.opend_guard import (
    clear_opend_phone_verify_pending,
    is_opend_phone_verify_pending,
    mark_opend_phone_verify_pending,
    send_opend_alert,
    send_opend_recovery_notice,
)
from src.application.multi_tick.project_guard import (
    admit_project_run,
    apply_project_load_shed,
    record_project_failure,
    record_project_success,
)
from src.application.multi_tick.misc import (
    set_debug,
    AccountResult,
    _safe_runlog_data,
)
from domain.domain.config_contract import (
    ensure_runtime_canonical_config,
    ensure_runtime_schedule_matches_market,
    resolve_config_contract,
)
from domain.domain.engine import (
    AccountSchedulerDecisionView,
    build_failure_audit_fields,
)
from src.application.multi_tick_audit import MultiTickAuditHelper
from src.application.tick_guard_flow import TickGuardRequest, run_tick_guard_flow
from src.application.tick_account_execution import (
    TickAccountExecutionRequest,
    resolve_account_run_max_workers as _resolve_account_run_max_workers,
    resolve_default_account as _resolve_default_account,
    run_tick_account_execution,
)
from src.application.tick_notification_flow import (
    TickNotificationRequest,
    run_tick_notification_flow,
)
from src.application.ai_decision_advice.config import (
    ai_decision_advice_enabled,
)
from src.application.ai_decision_advice.identity import (
    build_observation_set,
    candidate_symbols_from_snapshot,
    publish_observation_partition,
)
from src.application.opening_candidate_snapshot import (
    OpeningCandidateSnapshotError,
    load_opening_candidate_snapshot,
)
from src.application.tick_run_context import (
    build_tick_idempotency_context,
    complete_tick_idempotency as _complete_tick_idempotency,
)
from src.application.tick_run_workspace import prepare_tick_run_workspace
from src.application.scan_scheduler import mark_scheduler_accounts
from src.application.runtime_trigger_context import build_trigger_context
from src.application.runtime_paths import resolve_runtime_root
from src.application.runtime_config_freshness import (
    RuntimeConfigFreshnessError,
    RuntimeConfigIdentityError,
    ensure_runtime_config_freshness,
    ensure_runtime_config_identity,
)
from src.application.tick_scheduler_context import (
    TickSchedulerRequest,
    build_tick_scheduler_context,
)
from src.application.futu_quote_routing import resolve_futu_quote_route
from src.infrastructure.external_services import (
    run_opend_watchdog,
    run_scan_scheduler_cli,
    trading_day_via_futu,
)

from domain.storage.repositories import state_repo


_CURRENT_RUN_ID: str | None = None


def current_run_id() -> str | None:
    return _CURRENT_RUN_ID


def _resolve_daily_brief_trigger_kind(*, force_mode: bool, trigger_context: dict[str, Any]) -> str:
    if force_mode:
        return 'force'
    source = str(trigger_context.get('source') or '').strip().lower()
    return 'scheduled' if source in {'cron', 'scheduler'} else 'manual'


def _has_scan_to_run(*, should_run_global: bool, scan_decision_by_account: dict[str, dict[str, Any]]) -> bool:
    if bool(should_run_global):
        return True
    for item in (scan_decision_by_account or {}).values():
        if isinstance(item, dict) and item.get("should_run") is True:
            return True
    return False


def _load_advice_candidate_snapshots(
    *,
    base: Path,
    run_id: str,
    accounts: list[str] | tuple[str, ...],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    snapshots: dict[str, dict[str, Any]] = {}
    unavailable: dict[str, str] = {}
    for raw_account in sorted(set(accounts)):
        account = str(raw_account or "").strip().lower()
        if not account:
            continue
        try:
            snapshots[account] = load_opening_candidate_snapshot(
                base=base,
                run_id=run_id,
                account=account,
            )
        except OpeningCandidateSnapshotError:
            unavailable[account] = "candidate_snapshot_unavailable"
    return snapshots, unavailable


def _publish_ai_decision_observation_partitions(
    *,
    base: Path,
    run_id: str,
    markets: list[str] | tuple[str, ...],
    config: dict[str, Any],
    successful_accounts: list[str] | tuple[str, ...],
    candidate_snapshots_by_account: dict[str, dict[str, Any]],
    portfolio_distributions_by_account: dict[str, Any],
    option_contexts_by_account: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    successful = {
        str(account or "").strip().lower()
        for account in successful_accounts
        if str(account or "").strip()
    }
    accepted = successful & {
        str(account or "").strip().lower()
        for account in candidate_snapshots_by_account
        if str(account or "").strip()
    }
    if not accepted:
        # Replacement publication is permitted only after a successful Tick
        # and an accepted same-run candidate seal. Retain the prior evidence
        # observation generation on failed, skipped or incomplete runs.
        return {}

    candidate_snapshots_by_account = {
        account: snapshot
        for account, snapshot in candidate_snapshots_by_account.items()
        if str(account or "").strip().lower() in accepted
    }
    portfolio_distributions_by_account = {
        account: prepared
        for account, prepared in portfolio_distributions_by_account.items()
        if str(account or "").strip().lower() in accepted
    }
    option_contexts_by_account = {
        account: context
        for account, context in option_contexts_by_account.items()
        if str(account or "").strip().lower() in accepted
    }
    scan_symbols = [
        str(item.get("symbol") or item.get("code") or "")
        for item in resolve_watchlist_config(config)
        if isinstance(item, dict)
    ]
    stock_symbols: list[str] = []
    for prepared in portfolio_distributions_by_account.values():
        envelope = getattr(prepared, "envelope", None)
        payload = (
            envelope.get("payload")
            if isinstance(envelope, dict)
            else None
        )
        assets = payload.get("assets") if isinstance(payload, dict) else None
        for item in assets if isinstance(assets, list) else ():
            if (
                isinstance(item, dict)
                and str(item.get("normalized_type") or "").strip().lower()
                == "stock"
            ):
                stock_symbols.append(str(item.get("code") or ""))

    option_underlyings: list[str] = []
    for context in option_contexts_by_account.values():
        rows = context.get("open_positions_min")
        for item in rows if isinstance(rows, list) else ():
            if not isinstance(item, dict):
                continue
            try:
                contracts_open = int(item.get("contracts_open") or 0)
            except (TypeError, ValueError):
                continue
            if (
                str(item.get("status") or "open").strip().lower()
                != "open"
                or contracts_open <= 0
            ):
                continue
            symbol = canonical_symbol(item.get("symbol"))
            if symbol:
                option_underlyings.append(symbol)

    candidate_symbols = [
        symbol
        for snapshot in candidate_snapshots_by_account.values()
        for symbol in candidate_symbols_from_snapshot(snapshot)
    ]
    observed = build_observation_set(
        scan_symbols=scan_symbols,
        stock_holding_symbols=stock_symbols,
        open_option_underlyings=option_underlyings,
        recent_candidate_symbols=candidate_symbols,
    )

    results: dict[str, dict[str, Any]] = {}
    for raw_market in dict.fromkeys(markets):
        market = str(raw_market or "").strip().upper()
        if not market:
            continue
        partition = [item for item in observed if item.market == market]
        try:
            publish_observation_partition(
                base=base,
                market=market,
                observed=partition,
                generation=run_id,
                generated_at=utc_now(),
            )
        except Exception:
            results[market] = {
                "status": "unavailable",
                "symbol_count": 0,
            }
            continue
        results[market] = {
            "status": "ready",
            "symbol_count": len(partition),
        }
    return results


def _is_trading_day_guard_for_market(cfg: dict[str, Any], market: str) -> tuple[bool | None, str]:
    """Return (is_trading_day, market_used) for one market.

    None means guard check failed and caller should continue without blocking.
    """
    market_used = str(market or "").strip().upper() or "US"
    route = resolve_futu_quote_route(cfg, market=market_used)
    if not route.ok or route.host is None or route.port is None:
        return None, market_used
    return trading_day_via_futu(
        host=str(route.host),
        port=int(route.port),
        market=market_used,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Multi-account tick with per-account notifications')
    ap.add_argument('--config', required=True)
    ap.add_argument('--accounts', nargs='+', default=None)
    ap.add_argument('--symbols', nargs='+', default=None, help='Comma-separated or space-separated symbol whitelist for this tick.')
    ap.add_argument('--default-account', default=None)
    ap.add_argument('--market-config', default='auto', choices=['auto', 'hk', 'us', 'all'], help='Select symbols by market at config-load time (auto=by session).')
    ap.add_argument('--no-send', action='store_true', help='Do not send messages (for smoke tests / debugging).')
    ap.add_argument('--smoke', action='store_true', help='Smoke mode: run scheduler decisions but skip pipeline execution.')
    ap.add_argument('--force', action='store_true', help='Force the scan pipeline outside normal run points; force runs do not auto-send ordinary Tick notifications.')
    ap.add_argument('--debug', action='store_true', help='Verbose logs to stdout (for manual debugging).')
    ap.add_argument('--opend-phone-verify-continue', action='store_true', help='Clear OpenD phone-verify pending pause and continue running.')
    ap.add_argument('--allow-stale-config', action='store_true', help='Emergency override: skip generated runtime config freshness checks.')
    args = ap.parse_args(argv)

    set_debug(bool(getattr(args, 'debug', False)))

    no_send = bool(getattr(args, 'no_send', False))
    smoke = bool(getattr(args, 'smoke', False))
    force_mode = bool(getattr(args, 'force', False))
    symbols_arg = ",".join(str(item) for item in (getattr(args, 'symbols', None) or []) if str(item).strip()) or None

    repo_root = Path(__file__).resolve().parents[2]
    runtime_resolution = resolve_runtime_root(repo_root=repo_root)
    base = runtime_resolution.runtime_root
    vpy = Path(sys.executable)
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (repo_root / cfg_path).resolve()
    contract_info = resolve_config_contract(
        cfg_path,
        str(getattr(args, 'market_config', 'auto') or 'auto'),
        repo_base=repo_root,
    )
    ensure_runtime_canonical_config(
        cfg_path,
        str(getattr(args, 'market_config', 'auto') or 'auto'),
        repo_base=repo_root,
        require_sibling_external=True,
    )
    base_cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    requested_market = str(getattr(args, 'market_config', 'auto') or 'auto').strip().lower()
    try:
        ensure_runtime_config_identity(
            base_cfg,
            explicit_market=requested_market if requested_market in {'us', 'hk'} else None,
            runtime_config_path=cfg_path,
        )
    except RuntimeConfigIdentityError as exc:
        raise SystemExit(str(exc)) from exc
    schedule_contract_info = ensure_runtime_schedule_matches_market(
        base_cfg,
        config_path=cfg_path,
        market_config=requested_market,
    )
    allow_stale_config = bool(getattr(args, 'allow_stale_config', False))
    freshness_info: dict[str, Any] | None = None
    freshness_market = str(schedule_contract_info.get('market') or '').strip().lower()
    if freshness_market and not allow_stale_config:
        try:
            freshness_info = ensure_runtime_config_freshness(
                base_cfg,
                repo_root=repo_root,
                market=freshness_market,
                runtime_config_path=cfg_path,
            )
        except RuntimeConfigFreshnessError as exc:
            raise SystemExit(str(exc)) from exc
    try:
        if args.accounts is None:
            args.accounts = accounts_from_config(base_cfg)
        else:
            args.accounts = accounts_from_config({'accounts': args.accounts})
    except ValueError as exc:
        raise SystemExit(f"[CONFIG_ERROR] invalid account scope: {exc}") from exc
    args.default_account = _resolve_default_account(args.default_account, args.accounts)
    trigger_context = build_trigger_context()
    trigger_kind = _resolve_daily_brief_trigger_kind(
        force_mode=force_mode,
        trigger_context=trigger_context,
    )
    runlog = RunLogger(base)
    global _CURRENT_RUN_ID
    _CURRENT_RUN_ID = runlog.run_id  # pyright: ignore[reportConstantRedefinition]
    run_id = runlog.run_id

    syms0 = resolve_watchlist_config(base_cfg)
    src_counts: dict[str, int] = {}
    for it in syms0:
        if not isinstance(it, dict):
            continue
        src, _decision = resolve_symbol_fetch_source(it.get('fetch') or {})
        src_counts[src] = src_counts.get(src, 0) + 1
    runlog.safe_event(
        'run_start',
        'start',
        data=_safe_runlog_data({
            'accounts': [str(a).strip().lower() for a in (args.accounts or []) if str(a).strip()],
            'symbols_count': len([x for x in syms0 if isinstance(x, dict)]),
            'symbols_arg': symbols_arg,
            'source_selections': src_counts,
            'market_config': str(getattr(args, 'market_config', 'auto') or 'auto'),
            'config_source_path': contract_info.get('resolved_path'),
            'config_canonical_path': contract_info.get('sibling_canonical_path'),
            'config_schedule_contract': schedule_contract_info,
            'config_freshness': freshness_info,
            'allow_stale_config': allow_stale_config,
            'repo_root': str(repo_root),
            'runtime_root': str(base),
            'runtime_root_source': runtime_resolution.source,
            'trigger_source': trigger_context.get('source'),
            'trigger_kind': trigger_kind,
            'trigger_job_id': trigger_context.get('job_id'),
            'outer_delivery_mode': trigger_context.get('delivery_mode'),
            'outer_announce_expected': trigger_context.get('announce_expected'),
            'outer_timeout_seconds': trigger_context.get('timeout_seconds'),
            'no_send': no_send,
            'smoke': bool(smoke),
            'force': force_mode,
        }),
    )
    idempotency = build_tick_idempotency_context(
        cfg_path=cfg_path,
        market_config=str(getattr(args, 'market_config', 'auto') or 'auto'),
        accounts=args.accounts or [],
        trigger_kind=trigger_kind,
        symbols=symbols_arg,
        no_send=no_send,
        trigger_job_id=str(trigger_context.get("job_id") or ""),
    )
    market_cfg = idempotency.market_config
    execution_bucket = idempotency.bucket
    execution_idempotency_key = idempotency.key
    idempotency_accounts = idempotency.accounts
    idempotency_trigger_kind = idempotency.trigger_kind
    audit_helper = MultiTickAuditHelper(
        base=base,
        base_cfg=base_cfg,
        runlog=runlog,
        safe_data_fn=_safe_runlog_data,
        append_audit_event=state_repo.append_audit_event,
        record_project_failure=record_project_failure,
        record_project_success=record_project_success,
        build_failure_audit_fields=build_failure_audit_fields,
        run_id=run_id,
        idempotency_key=execution_idempotency_key,
        write_run_artifacts=False,
    )

    def complete_tick_idempotency(
        status: str = "completed",
        message: str | None = None,
        *,
        ok: bool = True,
        error_code: str | None = None,
    ) -> None:
        try:
            _complete_tick_idempotency(
                base=base,
                key=execution_idempotency_key,
                run_id=run_id,
                market_config=market_cfg,
                accounts=idempotency_accounts,
                trigger_kind=idempotency_trigger_kind,
                status=status,
                message=message,
                ok=ok,
                error_code=error_code,
            )
        except Exception as exc:
            if not ok:
                error = "TICK_IDEMPOTENCY_TERMINAL_WRITE_FAILED"
                runlog.safe_event(
                    "idempotency",
                    "error",
                    error_code=error,
                    message=str(exc),
                    data=_safe_runlog_data({"status": status, "terminal_error_code": error_code}),
                )
                audit_helper.audit(
                    "idempotency",
                    "complete_tick_execution_failed",
                    status="error",
                    message=str(exc),
                    extra={"status": status, "error_code": error, "terminal_error_code": error_code},
                )
                raise

    for it in syms0:
        if not isinstance(it, dict):
            continue
        sym = str(it.get('symbol') or '').strip().upper()
        if not sym:
            continue
        src, decision = resolve_symbol_fetch_source(it.get('fetch') or {})
        audit_helper.audit(
            'config',
            'fetch_source_decision',
            status='ok',
            tool_name='fetch_source_resolution',
            extra={'symbol': sym, 'source': src, 'decision': decision},
        )

    dedupe = state_repo.claim_idempotency_record(
        base,
        scope='tick_execution',
        key=execution_idempotency_key,
        payload={
            'status': 'in_progress',
            'run_id': run_id,
            'pid': os.getpid(),
            'market_config': market_cfg,
            'accounts': idempotency_accounts,
            'trigger_kind': idempotency_trigger_kind,
        },
    )
    if not bool(dedupe.get('claimed')):
        record = dedupe.get('record') if isinstance(dedupe.get('record'), dict) else {}
        if str(record.get('status') or '').strip().lower() == 'unsupported_failed':
            error_code = str(record.get('error_code') or 'daily_brief_multi_market_delivery_unsupported')
            audit_helper.audit(
                'idempotency',
                'replay_terminal_tick_failure',
                status='error',
                message=error_code,
                extra={'bucket': execution_bucket, 'error_code': error_code},
            )
            runlog.safe_event('run_end', 'error', error_code=error_code, message=error_code)
            return 2
        audit_helper.audit(
            'idempotency',
            'skip_duplicate_tick',
            status='skip',
            message='duplicate tick in same execution bucket',
            extra={'bucket': execution_bucket},
        )
        runlog.safe_event('run_end', 'skip', message='duplicate tick execution skipped')
        return 0
    audit_helper.audit('idempotency', 'claim_tick_execution', extra={'bucket': execution_bucket})

    guard_outcome = run_tick_guard_flow(
        TickGuardRequest(
            base=base,
            base_cfg=base_cfg,
            accounts=[str(a).strip() for a in (args.accounts or []) if str(a).strip()],
            default_account=args.default_account,
            market_config=market_cfg,
            no_send=no_send,
            opend_phone_verify_continue=bool(getattr(args, 'opend_phone_verify_continue', False)),
            vpy=vpy,
            runlog=runlog,
            audit_helper=audit_helper,
            complete_tick_idempotency_fn=complete_tick_idempotency,
            admit_project_run_fn=admit_project_run,
            apply_project_load_shed_fn=apply_project_load_shed,
            clear_opend_phone_verify_pending_fn=clear_opend_phone_verify_pending,
            is_opend_phone_verify_pending_fn=is_opend_phone_verify_pending,
            run_opend_watchdog_fn=run_opend_watchdog,
            mark_opend_phone_verify_pending_fn=mark_opend_phone_verify_pending,
            send_opend_alert_fn=send_opend_alert,
            send_opend_recovery_notice_fn=send_opend_recovery_notice,
        )
    )
    if not guard_outcome.should_continue:
        return guard_outcome.return_code
    base_cfg = guard_outcome.base_cfg
    args.accounts = guard_outcome.accounts
    args.default_account = guard_outcome.default_account
    bj_tz = guard_outcome.bj_tz
    account_ids = [str(acct).strip() for acct in (args.accounts or []) if str(acct).strip()]
    results: list[AccountResult] = []

    scheduler_outcome = build_tick_scheduler_context(
        TickSchedulerRequest(
            vpy=vpy,
            repo_root=repo_root,
            base=base,
            cfg_path=cfg_path,
            base_cfg=base_cfg,
            accounts=[str(a).strip() for a in (args.accounts or []) if str(a).strip()],
            market_config=str(getattr(args, 'market_config', 'auto') or 'auto'),
            force_mode=force_mode,
            smoke=smoke,
            run_id=run_id,
            runlog=runlog,
            audit_helper=audit_helper,
            check_trading_day_for_market=lambda gm: _is_trading_day_guard_for_market(base_cfg, gm),
            run_scan_scheduler_cli_fn=run_scan_scheduler_cli,
            account_view_cls=AccountSchedulerDecisionView,
        )
    )
    if not scheduler_outcome.should_continue:
        results.extend(scheduler_outcome.results)
        complete_tick_idempotency(
            status="failed",
            message=scheduler_outcome.message or "scheduler_failed",
            ok=False,
            error_code=scheduler_outcome.error_code or "SCHEDULER_FAILED",
        )
        return scheduler_outcome.return_code
    assert scheduler_outcome.context is not None
    scheduler_context = scheduler_outcome.context
    markets_to_run = scheduler_context.markets_to_run
    scheduler_markets = scheduler_context.scheduler_markets
    state_path = scheduler_context.state_path
    scheduler_schedule_key = scheduler_context.scheduler_schedule_key
    scheduler_ms = scheduler_context.scheduler_ms
    scheduler_decision = scheduler_context.scheduler_decision
    scheduler_view = scheduler_context.scheduler_view
    notify_decision_by_account = scheduler_context.notify_decision_by_account
    scan_decision_by_account = scheduler_context.scan_decision_by_account
    should_run_global = scheduler_context.should_run_global
    reason_global = scheduler_context.reason_global
    scheduler_decisions_by_account = {
        str(account).strip().lower(): dict(item["scheduler_decision"])
        for account, item in scan_decision_by_account.items()
        if isinstance(item, dict) and isinstance(item.get("scheduler_decision"), dict)
    }

    def commit_scan_targets(targets: dict[str, str | None]) -> None:
        if trigger_kind != "scheduled":
            return
        mark_scheduler_accounts(
            config=cfg_path,
            state=state_path,
            schedule_key=str(scheduler_schedule_key),
            accounts=list(targets),
            mark_scanned=True,
            processed_scan_targets_by_account=targets,
            base_dir=repo_root,
        )

    if not _has_scan_to_run(
        should_run_global=should_run_global,
        scan_decision_by_account=scan_decision_by_account,
    ):
        delivery_only_allowed = (
            trigger_kind == "scheduled"
            and len(scheduler_markets) == 1
            and any(item.get("in_run_window") is True for item in scheduler_decisions_by_account.values())
        )
        if not delivery_only_allowed:
            runlog.safe_event(
                'run_end',
                'skip',
                message=str(reason_global or 'scheduler_skip_no_scan'),
                data=_safe_runlog_data(
                    {
                        'reason': reason_global,
                        'scheduler_decision': scheduler_decision,
                        'accounts': account_ids,
                        'run_workspace_created': False,
                    }
                ),
            )
            complete_tick_idempotency(status='skipped', message=str(reason_global or 'scheduler_skip_no_scan'))
            return 0
        return run_tick_notification_flow(
            TickNotificationRequest(
                base=base,
                repo_root=repo_root,
                cfg_path=cfg_path,
                state_path=state_path,
                scheduler_schedule_key=str(scheduler_schedule_key),
                base_cfg=base_cfg,
                run_id=run_id,
                runlog=runlog,
                results=[],
                tick_metrics={"delivery_only": True},
                no_send=no_send,
                bj_tz=bj_tz,
                audit_helper=audit_helper,
                vpy=vpy,
                complete_tick_idempotency_fn=complete_tick_idempotency,
                markets_to_run=markets_to_run,
                scheduler_markets=scheduler_markets,
                scheduler_decision=scheduler_decision,
                account_ids=tuple(account_ids),
                scheduler_decisions_by_account=scheduler_decisions_by_account,
                delivery_only=True,
                trigger_kind=trigger_kind,
            )
        )

    workspace = prepare_tick_run_workspace(
        base=base,
        run_id=run_id,
        default_account=args.default_account,
    )
    audit_helper.enable_run_artifacts()
    accounts_root = workspace.accounts_root
    run_dir = workspace.run_dir
    prefetch_done = False
    shared_required = workspace.shared_required

    tick_metrics: dict[str, Any] = {
        'as_of_utc': utc_now(),
        'markets_to_run': markets_to_run,
        'scheduler_markets': scheduler_markets,
        'run_dir': str(run_dir),
        'scheduler_ms': scheduler_ms,
        'scheduler_decision': scheduler_decision,
        'trigger_context': trigger_context,
        'trigger_kind': trigger_kind,
        'accounts': [],
        'sent': False,
        'reason': '',
    }

    account_count = len(account_ids)
    account_workers = _resolve_account_run_max_workers(base_cfg, account_count)
    account_execution = run_tick_account_execution(
        TickAccountExecutionRequest(
            account_ids=account_ids,
            account_workers=account_workers,
            base=base,
            repo_root=repo_root,
            base_cfg=base_cfg,
            cfg_path=cfg_path,
            vpy=vpy,
            markets_to_run=markets_to_run,
            scheduler_ms=scheduler_ms,
            scheduler_view=scheduler_view,
            notify_decision_by_account=notify_decision_by_account,
            should_run_global=should_run_global,
            reason_global=reason_global,
            run_id=run_id,
            run_dir=run_dir,
            shared_required=shared_required,
            accounts_root=accounts_root,
            prefetch_done=prefetch_done,
            force_mode=force_mode,
            smoke=smoke,
            no_send=no_send,
            scan_decision_by_account=scan_decision_by_account,
            state_path=state_path,
            scheduler_schedule_key=str(scheduler_schedule_key),
            runlog=runlog,
            audit_helper=audit_helper,
            symbols_arg=symbols_arg,
        )
    )
    tick_metrics['accounts'].extend(account_execution.account_metrics)
    tick_metrics["required_data_prefetch_invocation_count"] = (
        account_execution.prefetch_invocation_count
    )
    tick_metrics["required_data_snapshot_status"] = (
        account_execution.snapshot_status
    )
    tick_metrics["required_data_snapshot_manifest_sha256"] = (
        account_execution.snapshot_manifest_sha256
    )
    tick_metrics["prepared_portfolio_contexts"] = list(
        account_execution.prepared_context_metrics
    )
    tick_metrics["prepared_portfolio_distributions"] = list(
        account_execution.prepared_portfolio_distribution_metrics
    )
    tick_metrics["portfolio_management_distribution_read_count"] = (
        account_execution.portfolio_management_distribution_read_count
    )
    results.extend(account_execution.results)

    advice_enabled = ai_decision_advice_enabled(base_cfg)
    candidate_snapshots_by_account: dict[str, dict[str, Any]] | None = None
    candidate_snapshot_unavailable_by_account: dict[str, str] | None = None
    if advice_enabled:
        (
            candidate_snapshots_by_account,
            candidate_snapshot_unavailable_by_account,
        ) = _load_advice_candidate_snapshots(
            base=base,
            run_id=run_id,
            accounts=tuple(account_execution.ran_pipeline_accounts),
        )
        observation_status = _publish_ai_decision_observation_partitions(
            base=base,
            run_id=run_id,
            markets=tuple(markets_to_run),
            config=base_cfg,
            successful_accounts=tuple(
                account_execution.ran_pipeline_accounts
            ),
            candidate_snapshots_by_account=candidate_snapshots_by_account,
            portfolio_distributions_by_account=dict(
                getattr(
                    account_execution,
                    "prepared_portfolio_distribution_by_account",
                    {},
                )
                or {}
            ),
            option_contexts_by_account=dict(
                getattr(
                    account_execution,
                    "prepared_option_positions_context_by_account",
                    {},
                )
                or {}
            ),
        )
        tick_metrics["ai_decision_observation"] = observation_status

    return run_tick_notification_flow(
        TickNotificationRequest(
            base=base,
            repo_root=repo_root,
            cfg_path=cfg_path,
            state_path=state_path,
            scheduler_schedule_key=str(scheduler_schedule_key),
            base_cfg=base_cfg,
            run_id=run_id,
            runlog=runlog,
            results=results,
            tick_metrics=tick_metrics,
            no_send=no_send,
            bj_tz=bj_tz,
            audit_helper=audit_helper,
            vpy=vpy,
            complete_tick_idempotency_fn=complete_tick_idempotency,
            markets_to_run=markets_to_run,
            scheduler_markets=scheduler_markets,
            scheduler_decision=scheduler_decision,
            ran_pipeline_accounts=account_execution.ran_pipeline_accounts,
            account_ids=tuple(account_ids),
            scheduler_decisions_by_account=scheduler_decisions_by_account,
            scheduled_scan_targets_by_account=account_execution.scheduled_scan_targets_by_account,
            commit_scan_targets_fn=commit_scan_targets,
            trigger_kind=trigger_kind,
            opening_candidate_snapshot_by_account=(
                candidate_snapshots_by_account
            ),
            opening_candidate_snapshot_unavailable_by_account=(
                candidate_snapshot_unavailable_by_account
            ),
            prepared_portfolio_distribution_by_account=(
                getattr(
                    account_execution,
                    "prepared_portfolio_distribution_by_account",
                    None,
                )
                if advice_enabled
                else None
            ),
            prepared_portfolio_distribution_artifact_path_by_account=(
                getattr(
                    account_execution,
                    "prepared_portfolio_distribution_artifact_path_by_account",
                    None,
                )
                if advice_enabled
                else None
            ),
            prepared_portfolio_distribution_artifact_sha256_by_account=(
                getattr(
                    account_execution,
                    "prepared_portfolio_distribution_artifact_sha256_by_account",
                    None,
                )
                if advice_enabled
                else None
            ),
            prepared_portfolio_distribution_status_by_account=(
                getattr(
                    account_execution,
                    "prepared_portfolio_distribution_status_by_account",
                    None,
                )
                if advice_enabled
                else None
            ),
            prepared_option_positions_context_by_account=(
                getattr(
                    account_execution,
                    "prepared_option_positions_context_by_account",
                    None,
                )
                if advice_enabled
                else None
            ),
            prepared_option_positions_context_unavailable_by_account=(
                getattr(
                    account_execution,
                    "prepared_option_positions_context_unavailable_by_account",
                    None,
                )
                if advice_enabled
                else None
            ),
            prepared_option_positions_context_manifest_by_account=(
                getattr(
                    account_execution,
                    "prepared_option_positions_context_manifest_by_account",
                    None,
                )
                if advice_enabled
                else None
            ),
            prepared_option_positions_context_manifest_sha256_by_account=(
                getattr(
                    account_execution,
                    "prepared_option_positions_context_manifest_sha256_by_account",
                    None,
                )
                if advice_enabled
                else None
            ),
        )
    )


multi_tick_main = main


def run_tick(argv: list[str] | None = None) -> int:
    return int(multi_tick_main(list(argv or [])))


__all__ = ['main', 'multi_tick_main', 'run_tick', 'current_run_id', '_CURRENT_RUN_ID']
