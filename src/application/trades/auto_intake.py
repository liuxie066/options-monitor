#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

repo_base = Path(__file__).resolve().parents[3]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from domain.domain.trade_account_identity import extract_primary_account_id
from src.application.config_loader import load_config
from src.application.trades.futu_detail_lookup import enrich_trade_push_payload_with_account_id
from src.application.trades.account_mapping import resolve_trade_intake_config
from src.application.trades.normalizer import normalize_trade_deal
from src.application.trades.resolver import resolve_trade_deal
from src.application.trades.state import (
    append_trade_intake_audit,
    load_trade_intake_state,
    upsert_deal_state,
    write_trade_intake_state,
)
from src.application.trades.backfill import payload_deal_id, run_history_backfill
from src.application.trades.state_reconcile import reconcile_trade_intake_state
from src.application.trades.push_listener import OpenDTradePushListener, TradeIntakeAuthRequired
from src.application.trades.receipt import send_trade_intake_receipt
from src.application.opend_fetch_config import opend_fetch_kwargs
from src.application.ledger.api import open_position_ledger_from_runtime_config
from src.application.runtime_paths import resolve_runtime_root
from src.application.trades.intake import process_trade_payload
from src.application.write_contract import attach_write_contract, write_control
from src.infrastructure.io_utils import atomic_write_json, utc_now


TRADE_INTAKE_AUTH_REQUIRED_EXIT_CODE = 78


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Auto trade intake via OpenD deal push")
    ap.add_argument("--config", required=True)
    ap.add_argument("--data-config", default=None)
    ap.add_argument("--runtime-root", default=None, help="runtime root for state, audit, status, and active ledger store")
    ap.add_argument("--mode", choices=["dry-run", "apply"], default=None)
    ap.add_argument("--confirm", action="store_true", help="confirm high-risk trade-event writes and receipts")
    ap.add_argument("--yes", action="store_true", help="non-interactive confirmation; emits an audit_id")
    ap.add_argument("--state-path", default=None)
    ap.add_argument("--audit-path", default=None)
    ap.add_argument("--status-path", default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--once", action="store_true", help="Validate config and exit")
    ap.add_argument("--deal-json", default=None, help="Replay a single normalized/raw deal payload from a JSON file")
    ap.add_argument("--retry-failed", action="store_true", help="Allow --deal-json replay of a previously failed deal_id")
    ap.add_argument("--reconcile-state", action="store_true", help="Reconcile historical failed/unresolved deal state from ledger/audit evidence")
    ap.add_argument("--deal-id", action="append", default=None, help="Limit --reconcile-state to a specific deal_id; repeatable")
    ap.add_argument("--apply", action="store_true", help="Apply --reconcile-state local state changes; dry-run by default")
    ap.add_argument("--dry-run", action="store_true", help="Preview --reconcile-state without writing")
    return ap.parse_args(argv)


def _log(message: str) -> None:
    print(message, flush=True)


def _process_payload(
    payload: dict[str, Any],
    *,
    repo: Any,
    state_path: Path,
    audit_path: Path,
    account_mapping: dict[str, str],
    futu_account_ids: list[str],
    apply_changes: bool,
    host: str,
    port: int,
    config: dict[str, Any] | None = None,
    config_path: Path | None = None,
    runtime_root: Path | None = None,
    on_result_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    retry_failed_deal: bool = False,
    source: str = "push",
    allow_external_lookup: bool = True,
) -> dict[str, Any]:
    opend_config = opend_fetch_kwargs(config) if isinstance(config, dict) else None
    normalize_fn = normalize_trade_deal
    if isinstance(config, dict):
        normalize_fn = lambda raw, *, futu_account_mapping=None: normalize_trade_deal(
            raw,
            futu_account_mapping=futu_account_mapping,
            repo_base=repo_base,
            runtime_root=runtime_root,
            config_path=config_path,
            config=config,
            host=host,
            port=port,
            opend_fetch_config=opend_config,
            allow_opend_refresh=bool(allow_external_lookup),
        )
    def _enrich_payload(raw: dict[str, Any]) -> Any:
        return enrich_trade_push_payload_with_account_id(
            raw,
            host=host,
            port=port,
            futu_account_ids=futu_account_ids,
        )

    return process_trade_payload(
        payload,
        repo=repo,
        state_path=state_path,
        audit_path=audit_path,
        account_mapping=account_mapping,
        apply_changes=apply_changes,
        load_trade_intake_state_fn=load_trade_intake_state,
        write_trade_intake_state_fn=write_trade_intake_state,
        upsert_deal_state_fn=upsert_deal_state,
        append_trade_intake_audit_fn=append_trade_intake_audit,
        enrich_trade_payload_fn=_enrich_payload if allow_external_lookup else None,
        normalize_trade_deal_fn=normalize_fn,
        resolve_trade_deal_fn=resolve_trade_deal,
        on_result_fn=on_result_fn,
        retry_failed_deal=retry_failed_deal,
        source=source,
    )


class _ReplayRepo:
    def list_records(self, *, page_size: int = 500) -> list[dict[str, Any]]:
        return []

    def get_record_fields(self, record_id: str) -> dict[str, Any]:
        raise KeyError(record_id)

    def create_record(self, fields: dict[str, Any]) -> dict[str, Any]:
        return {"record": {"record_id": "dry_run_replay"}}



def _coordinate_listener_sources(
    sources: list[dict[str, Any]],
    *,
    run_source: Callable[[dict[str, Any], threading.Event], int],
) -> int:
    stop_event = threading.Event()
    results: queue.Queue[int] = queue.Queue()

    def _worker(source: dict[str, Any]) -> None:
        try:
            result = run_source(source, stop_event)
        except Exception as exc:
            _log(f"[ERROR] listener source={source.get('id')} crashed: {type(exc).__name__}: {exc}")
            result = 1
        results.put(result)

    threads = [
        threading.Thread(target=_worker, args=(source,), name=f"trade-intake-{source.get('id')}", daemon=False)
        for source in sources
    ]
    exit_code = 0
    completed = 0
    try:
        for thread in threads:
            thread.start()
        while completed < len(threads):
            try:
                source_code = results.get(timeout=0.2)
            except queue.Empty:
                continue
            completed += 1
            if source_code == TRADE_INTAKE_AUTH_REQUIRED_EXIT_CODE:
                exit_code = source_code
                stop_event.set()
            elif source_code != 0 and exit_code == 0:
                exit_code = source_code
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=5)
        return 0
    return exit_code

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base = repo_base
    runtime_resolution = resolve_runtime_root(repo_root=base, runtime_root=args.runtime_root)
    runtime_root = runtime_resolution.runtime_root
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()
    cfg = load_config(base=base, config_path=cfg_path, is_scheduled=False, log=_log)
    intake_cfg = resolve_trade_intake_config(
        cfg,
        mode_override=args.mode,
        state_path_override=args.state_path,
        audit_path_override=args.audit_path,
        status_path_override=args.status_path,
    )
    if args.host or args.port:
        sources = _legacy_override_sources(intake_cfg, host=args.host, port=args.port)
    else:
        sources = list(intake_cfg.get("sources") or [])
    state_path = intake_cfg["state_path"]
    audit_path = intake_cfg["audit_path"]
    status_path = intake_cfg["status_path"]
    if not state_path.is_absolute():
        state_path = (runtime_root / state_path).resolve()
    if not audit_path.is_absolute():
        audit_path = (runtime_root / audit_path).resolve()
    if not status_path.is_absolute():
        status_path = (runtime_root / status_path).resolve()
    sources = [_resolve_source_paths(source, runtime_root=runtime_root) for source in sources]
    status_base = _status_base_payload(
        cfg_path=cfg_path,
        intake_cfg=intake_cfg,
        state_path=state_path,
        audit_path=audit_path,
        status_path=status_path,
        host=str(args.host or "127.0.0.1"),
        port=int(args.port or 11111),
        runtime_root=runtime_root,
        runtime_root_source=runtime_resolution.source,
    )
    if args.retry_failed and not args.deal_json:
        print("--retry-failed requires --deal-json replay")
        return 2
    if args.deal_id and not args.reconcile_state:
        print("--deal-id is only supported with --reconcile-state")
        return 2
    if args.apply and not args.reconcile_state:
        print("--apply is only supported with --reconcile-state; use --mode apply for trade-event writes")
        return 2
    if args.dry_run and not args.reconcile_state:
        print("--dry-run is only supported with --reconcile-state; use --mode dry-run for trade intake")
        return 2
    if args.apply and args.dry_run:
        print("--dry-run cannot be combined with --apply")
        return 2
    if args.reconcile_state and args.mode:
        print("--reconcile-state uses --apply/--dry-run; do not use --mode")
        return 2
    if args.reconcile_state:
        _data_config, repo = open_position_ledger_from_runtime_config(base=runtime_root, cfg=cfg, data_config=args.data_config)
        result = reconcile_trade_intake_state(
            state_path=state_path,
            audit_path=audit_path,
            repo=repo,
            deal_ids=list(args.deal_id or []),
            apply_changes=bool(args.apply),
        )
        result["runtime_root"] = str(runtime_root)
        result["runtime_root_source"] = runtime_resolution.source
        result = attach_write_contract(
            result,
            dry_run=not bool(args.apply),
            write_applied=bool(args.apply) and int(result.get("applied_count") or 0) > 0,
            backup_path=result.get("backup_path"),
            rollback_hint="restore the auto_trade_intake_state.json backup",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.once and not args.deal_json:
        _log(
            json.dumps(
                {
                    "ok": True,
                    "runtime_root": str(runtime_root),
                    "runtime_root_source": runtime_resolution.source,
                    "mode": intake_cfg["mode"],
                    "enabled": bool(intake_cfg["enabled"]),
                    "state_path": str(state_path),
                    "audit_path": str(audit_path),
                    "status_path": str(status_path),
                    "receipt": dict(intake_cfg["receipt"]),
                    "backfill": dict(intake_cfg["backfill"]),
                    "mapped_accounts": sorted(intake_cfg["account_mapping"].values()),
                    "sources": [_source_status_payload(source) for source in sources],
                },
                ensure_ascii=False,
            )
        )
        return 0

    apply_changes = intake_cfg["mode"] == "apply"
    control = write_control(
        apply=apply_changes,
        confirm=bool(args.confirm),
        yes=bool(args.yes),
        high_risk=True,
    )
    if apply_changes and control["confirmation_required"]:
        print("trade-intake apply mode writes trade_events and may send receipts; use --confirm or --yes")
        return 2
    receipt_callback = _build_receipt_callback(
        base=base,
        cfg=cfg,
        receipt_config=intake_cfg["receipt"],
    )

    if args.deal_json:
        payload = json.loads(Path(args.deal_json).read_text(encoding="utf-8"))
        manual_source = _select_source_for_payload(
            sources,
            payload=payload,
            account_mapping=intake_cfg["account_mapping"],
            require_match=bool(apply_changes),
        )
        manual_host = str(args.host or manual_source.get("host") or "127.0.0.1")
        manual_port = int(args.port or manual_source.get("port") or 11111)
        manual_account_mapping = dict(manual_source.get("account_mapping") or intake_cfg["account_mapping"])
        manual_futu_account_ids = list(manual_source.get("futu_account_ids") or intake_cfg["futu_account_ids"])
        with contextlib.redirect_stdout(sys.stderr):
            if apply_changes:
                _data_config, repo = open_position_ledger_from_runtime_config(base=runtime_root, cfg=cfg, data_config=args.data_config)
            else:
                repo = _ReplayRepo()
            result = _process_payload(
                payload,
                repo=repo,
                state_path=state_path,
                audit_path=audit_path,
                account_mapping=manual_account_mapping,
                futu_account_ids=manual_futu_account_ids,
                apply_changes=apply_changes,
                host=manual_host,
                port=manual_port,
                config=cfg,
                config_path=cfg_path,
                runtime_root=runtime_root,
                on_result_fn=receipt_callback,
                retry_failed_deal=bool(args.retry_failed),
                source="manual",
                allow_external_lookup=bool(apply_changes),
            )
        if apply_changes:
            _write_listener_status(
                status_path,
                status_base,
                status="once",
                stage="deal_json_processed",
                last_deal_result=_result_summary(result),
                last_receipt_result=_receipt_summary(result.get("receipt")),
            )
        result = attach_write_contract(
            result,
            dry_run=not apply_changes,
            write_applied=apply_changes and str(result.get("status") or "") not in {"dry_run", "skipped"},
            rollback_hint="void created trade events or restore option_positions SQLite from backup",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    _data_config, repo = open_position_ledger_from_runtime_config(base=runtime_root, cfg=cfg, data_config=args.data_config)

    if not bool(intake_cfg["enabled"]):
        for source in sources:
            _write_listener_status(
                source["status_path"],
                _status_base_for_source(
                    cfg_path=cfg_path,
                    intake_cfg=intake_cfg,
                    source=source,
                    runtime_root=runtime_root,
                    runtime_root_source=runtime_resolution.source,
                ),
                status="error",
                stage="config",
                last_error="trade_intake.enabled=false",
            )
        raise SystemExit("trade_intake.enabled=false; refusing to start listener")

    process_lock = threading.RLock()
    if len(sources) == 1:
        return _run_listener_source_loop(
            source=sources[0],
            repo=repo,
            cfg=cfg,
            cfg_path=cfg_path,
            runtime_root=runtime_root,
            runtime_root_source=runtime_resolution.source,
            intake_cfg=intake_cfg,
            apply_changes=apply_changes,
            receipt_callback=receipt_callback,
            process_lock=process_lock,
        )

    return _coordinate_listener_sources(
        sources,
        run_source=lambda source, stop_event: _run_listener_source_loop(
            source=source,
            repo=repo,
            cfg=cfg,
            cfg_path=cfg_path,
            runtime_root=runtime_root,
            runtime_root_source=runtime_resolution.source,
            intake_cfg=intake_cfg,
            apply_changes=apply_changes,
            receipt_callback=receipt_callback,
            process_lock=process_lock,
            stop_event=stop_event,
        ),
    )


def _build_receipt_callback(
    *,
    base: Path,
    cfg: dict[str, Any],
    receipt_config: dict[str, Any],
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def _callback(context: dict[str, Any]) -> dict[str, Any]:
        return send_trade_intake_receipt(
            base=base,
            config=cfg,
            receipt_config=receipt_config,
            apply_changes=bool(context.get("apply_changes")),
            state=context.get("state") if isinstance(context.get("state"), dict) else {},
            deal=context.get("deal"),
            result=dict(context.get("result") or {}),
            payload=context.get("effective_payload") if isinstance(context.get("effective_payload"), dict) else {},
        )

    return _callback


def _select_source_for_payload(
    sources: list[dict[str, Any]],
    *,
    payload: dict[str, Any],
    account_mapping: dict[str, str],
    require_match: bool,
) -> dict[str, Any]:
    if not sources:
        return {}
    if len(sources) == 1:
        return sources[0]

    futu_account_id = extract_primary_account_id(payload) or ""
    account = str(payload.get("account") or payload.get("internal_account") or "").strip().lower()
    mapped_account = str(account_mapping.get(futu_account_id) or "").strip().lower() if futu_account_id else ""
    if account and mapped_account and account != mapped_account:
        raise SystemExit("deal-json payload account conflicts with futu_account_id mapping; pass a consistent payload or --host/--port")
    if not account and mapped_account:
        account = mapped_account

    matches: list[dict[str, Any]] = []
    for source in sources:
        source_account = str(source.get("account") or "").strip().lower()
        source_account_ids = {str(item or "").strip() for item in list(source.get("futu_account_ids") or [])}
        account_matches = bool(account and source_account and account == source_account)
        futu_account_matches = bool(futu_account_id and futu_account_id in source_account_ids)
        if account and futu_account_id:
            if account_matches and futu_account_matches:
                matches.append(source)
            continue
        if account_matches:
            matches.append(source)
            continue
        if futu_account_matches:
            matches.append(source)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SystemExit("deal-json payload matches multiple trade-intake sources; pass --host/--port explicitly")
    if require_match:
        raise SystemExit("deal-json apply mode with multiple trade-intake sources requires payload futu_account_id/account or explicit --host/--port")
    return sources[0]


def _legacy_override_sources(intake_cfg: dict[str, Any], *, host: str | None, port: int | None) -> list[dict[str, Any]]:
    return [
        {
            "id": "legacy",
            "account": None,
            "enabled": bool(intake_cfg.get("enabled", True)),
            "mode": str(intake_cfg.get("mode") or "dry-run"),
            "host": str(host or "127.0.0.1"),
            "port": int(port or 11111),
            "state_path": intake_cfg["state_path"],
            "audit_path": intake_cfg["audit_path"],
            "status_path": intake_cfg["status_path"],
            "reconnect_sec": int(intake_cfg.get("reconnect_sec") or 5),
            "receipt": dict(intake_cfg.get("receipt") or {}),
            "backfill": dict(intake_cfg.get("backfill") or {}),
            "account_mapping": dict(intake_cfg.get("account_mapping") or {}),
            "futu_account_ids": list(intake_cfg.get("futu_account_ids") or []),
        }
    ]


def _resolve_source_paths(source: dict[str, Any], *, runtime_root: Path) -> dict[str, Any]:
    out = dict(source)
    for key in ("state_path", "audit_path", "status_path"):
        path = out.get(key)
        resolved = path if isinstance(path, Path) else Path(str(path or ""))
        if not resolved.is_absolute():
            resolved = (runtime_root / resolved).resolve()
        out[key] = resolved
    return out


def _source_status_payload(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": source.get("id"),
        "account": source.get("account"),
        "enabled": bool(source.get("enabled", True)),
        "host": source.get("host"),
        "port": source.get("port"),
        "state_path": str(source.get("state_path")),
        "audit_path": str(source.get("audit_path")),
        "status_path": str(source.get("status_path")),
        "mapped_accounts": sorted(dict(source.get("account_mapping") or {}).values()),
        "futu_account_ids": list(source.get("futu_account_ids") or []),
    }


def _status_base_for_source(
    *,
    cfg_path: Path,
    intake_cfg: dict[str, Any],
    source: dict[str, Any],
    runtime_root: Path,
    runtime_root_source: str,
) -> dict[str, Any]:
    source_cfg = dict(intake_cfg)
    source_cfg["account_mapping"] = dict(source.get("account_mapping") or {})
    source_cfg["futu_account_ids"] = list(source.get("futu_account_ids") or [])
    source_cfg["receipt"] = dict(source.get("receipt") or intake_cfg.get("receipt") or {})
    source_cfg["backfill"] = dict(source.get("backfill") or intake_cfg.get("backfill") or {})
    out = _status_base_payload(
        cfg_path=cfg_path,
        intake_cfg=source_cfg,
        state_path=source["state_path"],
        audit_path=source["audit_path"],
        status_path=source["status_path"],
        host=str(source.get("host") or "127.0.0.1"),
        port=int(source.get("port") or 11111),
        runtime_root=runtime_root,
        runtime_root_source=runtime_root_source,
    )
    out["source_id"] = source.get("id")
    if source.get("account"):
        out["account"] = source.get("account")
    return out


def _run_listener_source_loop(
    *,
    source: dict[str, Any],
    repo: Any,
    cfg: dict[str, Any],
    cfg_path: Path,
    runtime_root: Path,
    runtime_root_source: str,
    intake_cfg: dict[str, Any],
    apply_changes: bool,
    receipt_callback: Callable[[dict[str, Any]], dict[str, Any]],
    process_lock: threading.RLock,
    stop_event: threading.Event | None = None,
) -> int:
    state_path = source["state_path"]
    audit_path = source["audit_path"]
    status_path = source["status_path"]
    host = str(source.get("host") or "127.0.0.1")
    port = int(source.get("port") or 11111)
    account_mapping = dict(source.get("account_mapping") or {})
    futu_account_ids = list(source.get("futu_account_ids") or [])
    status_state = _status_base_for_source(
        cfg_path=cfg_path,
        intake_cfg=intake_cfg,
        source=source,
        runtime_root=runtime_root,
        runtime_root_source=runtime_root_source,
    )
    stop = stop_event or threading.Event()

    def _on_deal(payload: dict[str, Any]) -> None:
        push_received_at = utc_now()
        with process_lock:
            result = _process_payload(
                payload,
                repo=repo,
                state_path=state_path,
                audit_path=audit_path,
                account_mapping=account_mapping,
                futu_account_ids=futu_account_ids,
                apply_changes=apply_changes,
                host=host,
                port=port,
                config=cfg,
                config_path=cfg_path,
                runtime_root=runtime_root,
                on_result_fn=receipt_callback,
                source="push",
            )
        status_state.update(
            {
                "last_push_received_utc": push_received_at,
                "last_push_deal_id": result.get("deal_id") or payload_deal_id(payload) or None,
                "last_deal_result": _result_summary(result),
                "last_receipt_result": _receipt_summary(result.get("receipt")),
            }
        )
        _write_listener_status(status_path, status_state, status="listening", stage="deal_processed")
        _log(_format_result_summary(result))

    listener = OpenDTradePushListener(host=host, port=port, on_deal=_on_deal)
    restart_count = 0
    last_backfill_monotonic: float | None = None
    last_heartbeat_monotonic: float | None = None
    backfill_cfg = dict(source.get("backfill") or intake_cfg.get("backfill") or {})
    reconnect_floor_sec = max(1, int(source.get("reconnect_sec") or intake_cfg.get("reconnect_sec") or 5))
    reconnect_delay_sec = reconnect_floor_sec
    while not stop.is_set():
        try:
            _write_listener_status(status_path, status_state, status="starting", stage="listener_start", restart_count=restart_count)
            listener.start()
            _log(f"[OK] auto trade intake listener started source={source.get('id')} {host}:{port}")
            _write_listener_status(status_path, status_state, status="listening", stage="listener_started", restart_count=restart_count)
            if bool(backfill_cfg.get("enabled", True)) and not bool(backfill_cfg.get("startup_check", True)) and last_backfill_monotonic is None:
                last_backfill_monotonic = time.monotonic()
            while not stop.is_set():
                listener.check_health()
                reconnect_delay_sec = reconnect_floor_sec
                should_backfill = bool(backfill_cfg.get("enabled", True))
                now_mono = time.monotonic()
                if should_backfill:
                    interval_sec = int(backfill_cfg.get("interval_sec") or 300)
                    startup_check = bool(backfill_cfg.get("startup_check", True))
                    due = (last_backfill_monotonic is None and startup_check) or (
                        last_backfill_monotonic is not None and now_mono - last_backfill_monotonic >= interval_sec
                    )
                    if due:
                        try:
                            result = run_history_backfill(
                                repo=repo,
                                state_path=state_path,
                                audit_path=audit_path,
                                account_mapping=account_mapping,
                                futu_account_ids=futu_account_ids,
                                apply_changes=apply_changes,
                                host=host,
                                port=port,
                                config=cfg,
                                config_path=cfg_path,
                                runtime_root=runtime_root,
                                backfill_config=backfill_cfg,
                                on_result_fn=receipt_callback,
                                process_payload_fn=_process_payload,
                                process_lock=process_lock,
                            )
                        except Exception as exc:
                            result = {
                                "ok": False,
                                "finished_at_utc": utc_now(),
                                "deal_count": 0,
                                "applied_count": 0,
                                "skipped_duplicate_count": 0,
                                "failed_count": 1,
                                "unresolved_count": 0,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                            append_trade_intake_audit(
                                audit_path,
                                {
                                    "phase": "backfill_failed",
                                    "source": "backfill",
                                    "finished_at_utc": result["finished_at_utc"],
                                    "error": result["error"],
                                },
                            )
                        last_backfill_monotonic = time.monotonic()
                        status_state.update(_update_status_from_backfill(status_state, result))
                        _write_listener_status(status_path, status_state, status="listening", stage="backfill_check", restart_count=restart_count)
                if last_heartbeat_monotonic is None or now_mono - last_heartbeat_monotonic >= 60:
                    _write_listener_status(status_path, status_state, status="listening", stage="heartbeat", restart_count=restart_count)
                    last_heartbeat_monotonic = now_mono
                if stop.wait(5):
                    break
        except KeyboardInterrupt:
            stop.set()
            listener.close()
            _write_listener_status(status_path, status_state, status="stopped", stage="keyboard_interrupt", restart_count=restart_count)
            return 0
        except TradeIntakeAuthRequired as exc:
            listener.close()
            stop.set()
            status_state["last_error"] = str(exc)
            _write_listener_status(
                status_path,
                status_state,
                status="blocked",
                stage="auth_required",
                restart_count=restart_count,
                last_error=str(exc),
                error_code=exc.error_code,
                error_message=exc.message,
            )
            _log(f"[ERROR] listener source={source.get('id')} blocked: {exc}")
            return TRADE_INTAKE_AUTH_REQUIRED_EXIT_CODE
        except Exception as exc:
            listener.close()
            restart_count += 1
            status_state["last_error"] = f"{type(exc).__name__}: {exc}"
            _write_listener_status(
                status_path,
                status_state,
                status="reconnecting",
                stage="listener_exception",
                restart_count=restart_count,
                last_error=f"{type(exc).__name__}: {exc}",
                reconnect_delay_sec=reconnect_delay_sec,
            )
            _log(f"[WARN] listener source={source.get('id')} exited: {exc}; retry in {reconnect_delay_sec} sec")
            if stop.wait(reconnect_delay_sec):
                break
            reconnect_delay_sec = min(reconnect_delay_sec * 2, 60)
    listener.close()
    _write_listener_status(status_path, status_state, status="stopped", stage="stop_event", restart_count=restart_count)
    return 0


def _status_base_payload(
    *,
    cfg_path: Path,
    intake_cfg: dict[str, Any],
    state_path: Path,
    audit_path: Path,
    status_path: Path,
    host: str,
    port: int,
    runtime_root: Path,
    runtime_root_source: str,
) -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "config_path": str(cfg_path),
        "runtime_root": str(runtime_root),
        "runtime_root_source": str(runtime_root_source),
        "mode": intake_cfg["mode"],
        "enabled": bool(intake_cfg["enabled"]),
        "state_path": str(state_path),
        "audit_path": str(audit_path),
        "status_path": str(status_path),
        "host": str(host),
        "port": int(port),
        "mapped_accounts": sorted(intake_cfg["account_mapping"].values()),
        "receipt": dict(intake_cfg.get("receipt") or {}),
        "backfill": dict(intake_cfg.get("backfill") or {}),
        "started_at_utc": utc_now(),
    }


def _write_listener_status(path: Path, base_payload: dict[str, Any], *, status: str, stage: str, **extra: Any) -> None:
    payload = dict(base_payload)
    payload.update(
        {
            "status": str(status),
            "stage": str(stage),
            "last_heartbeat_utc": utc_now(),
        }
    )
    payload.update({key: value for key, value in extra.items() if value is not None})
    atomic_write_json(path, payload)


def _update_status_from_backfill(status_state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
    out = dict(status_state)
    out.update(
        {
            "last_backfill_check_utc": result.get("finished_at_utc"),
            "last_backfill_window_start_utc": diagnostics.get("window_start_utc"),
            "last_backfill_window_end_utc": diagnostics.get("window_end_utc"),
            "last_backfill_deal_count": result.get("deal_count"),
            "last_backfill_applied_count": result.get("applied_count"),
            "last_backfill_skipped_duplicate_count": result.get("skipped_duplicate_count"),
            "last_backfill_failed_count": result.get("failed_count"),
            "last_backfill_unresolved_count": result.get("unresolved_count"),
            "last_backfill_result": result.get("last_result"),
            "last_backfill_error": result.get("error"),
        }
    )
    prior = int(out.get("missed_push_backfill_count") or 0)
    out["missed_push_backfill_count"] = prior + int(result.get("applied_count") or 0)
    return out


def _result_summary(result: dict[str, Any] | None) -> dict[str, Any]:
    data = result if isinstance(result, dict) else {}
    return {
        "status": data.get("status"),
        "action": data.get("action"),
        "reason": data.get("reason"),
        "deal_id": data.get("deal_id"),
        "account": data.get("account"),
    }


def _receipt_summary(receipt: object) -> dict[str, Any] | None:
    if not isinstance(receipt, dict):
        return None
    return {
        "status": receipt.get("status"),
        "reason": receipt.get("reason"),
        "delivery_confirmed": bool(receipt.get("delivery_confirmed")),
        "message_id": receipt.get("message_id"),
        "error_code": receipt.get("error_code"),
    }


def _format_result_summary(result: dict[str, Any]) -> str:
    summary = _result_summary(result)
    receipt = _receipt_summary(result.get("receipt"))
    parts = [
        "AUTO_TRADE_INTAKE",
        f"status={summary.get('status')}",
        f"action={summary.get('action')}",
        f"account={summary.get('account')}",
        f"deal_id={summary.get('deal_id')}",
        f"reason={summary.get('reason')}",
    ]
    if receipt is not None:
        parts.append(f"receipt={receipt.get('status')}")
        parts.append(f"receipt_confirmed={str(bool(receipt.get('delivery_confirmed'))).lower()}")
    return " ".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
