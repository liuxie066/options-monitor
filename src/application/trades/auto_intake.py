#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

repo_base = Path(__file__).resolve().parents[3]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

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
from src.application.trades.push_listener import OpenDTradePushListener
from src.application.trades.receipt import send_trade_intake_receipt
from src.application.opend_fetch_config import opend_fetch_kwargs
from src.application.ledger.api import open_position_ledger_from_runtime_config
from src.application.trades.intake import process_trade_payload
from src.infrastructure.io_utils import atomic_write_json, utc_now


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Auto trade intake via OpenD deal push")
    ap.add_argument("--config", default="config.us.json")
    ap.add_argument("--data-config", default=None)
    ap.add_argument("--mode", choices=["dry-run", "apply"], default=None)
    ap.add_argument("--state-path", default=None)
    ap.add_argument("--audit-path", default=None)
    ap.add_argument("--status-path", default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=11111)
    ap.add_argument("--once", action="store_true", help="Validate config and exit")
    ap.add_argument("--deal-json", default=None, help="Replay a single normalized/raw deal payload from a JSON file")
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
    on_result_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    opend_config = opend_fetch_kwargs(config) if isinstance(config, dict) else None
    normalize_fn = normalize_trade_deal
    if isinstance(config, dict):
        normalize_fn = lambda raw, *, futu_account_mapping=None: normalize_trade_deal(
            raw,
            futu_account_mapping=futu_account_mapping,
            repo_base=repo_base,
            config=config,
            host=host,
            port=port,
            opend_fetch_config=opend_config,
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
        enrich_trade_payload_fn=_enrich_payload,
        normalize_trade_deal_fn=normalize_fn,
        resolve_trade_deal_fn=resolve_trade_deal,
        on_result_fn=on_result_fn,
    )


class _ReplayRepo:
    def list_records(self, *, page_size: int = 500) -> list[dict[str, Any]]:
        return []

    def get_record_fields(self, record_id: str) -> dict[str, Any]:
        raise KeyError(record_id)

    def create_record(self, fields: dict[str, Any]) -> dict[str, Any]:
        return {"record": {"record_id": "dry_run_replay"}}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base = repo_base
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
    state_path = intake_cfg["state_path"]
    audit_path = intake_cfg["audit_path"]
    status_path = intake_cfg["status_path"]
    if not state_path.is_absolute():
        state_path = (base / state_path).resolve()
    if not audit_path.is_absolute():
        audit_path = (base / audit_path).resolve()
    if not status_path.is_absolute():
        status_path = (base / status_path).resolve()
    status_base = _status_base_payload(
        cfg_path=cfg_path,
        intake_cfg=intake_cfg,
        state_path=state_path,
        audit_path=audit_path,
        status_path=status_path,
        host=args.host,
        port=args.port,
    )

    if args.once and not args.deal_json:
        _log(
            json.dumps(
                {
                    "ok": True,
                    "mode": intake_cfg["mode"],
                    "enabled": bool(intake_cfg["enabled"]),
                    "state_path": str(state_path),
                    "audit_path": str(audit_path),
                    "status_path": str(status_path),
                    "receipt": dict(intake_cfg["receipt"]),
                    "mapped_accounts": sorted(intake_cfg["account_mapping"].values()),
                },
                ensure_ascii=False,
            )
        )
        return 0

    apply_changes = intake_cfg["mode"] == "apply"
    receipt_callback = _build_receipt_callback(
        base=base,
        cfg=cfg,
        receipt_config=intake_cfg["receipt"],
    )

    if args.deal_json:
        payload = json.loads(Path(args.deal_json).read_text(encoding="utf-8"))
        if apply_changes:
            _data_config, repo = open_position_ledger_from_runtime_config(base=base, cfg=cfg, data_config=args.data_config)
        else:
            repo = _ReplayRepo()
        result = _process_payload(
            payload,
            repo=repo,
            state_path=state_path,
            audit_path=audit_path,
            account_mapping=intake_cfg["account_mapping"],
            futu_account_ids=intake_cfg["futu_account_ids"],
            apply_changes=apply_changes,
            host=args.host,
            port=args.port,
            config=cfg,
            on_result_fn=receipt_callback,
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
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    _data_config, repo = open_position_ledger_from_runtime_config(base=base, cfg=cfg, data_config=args.data_config)

    if not bool(intake_cfg["enabled"]):
        _write_listener_status(status_path, status_base, status="error", stage="config", last_error="trade_intake.enabled=false")
        raise SystemExit("trade_intake.enabled=false; refusing to start listener")

    def _on_deal(payload: dict[str, Any]) -> None:
        result = _process_payload(
            payload,
            repo=repo,
            state_path=state_path,
            audit_path=audit_path,
            account_mapping=intake_cfg["account_mapping"],
            futu_account_ids=intake_cfg["futu_account_ids"],
            apply_changes=apply_changes,
            host=args.host,
            port=args.port,
            config=cfg,
            on_result_fn=receipt_callback,
        )
        _write_listener_status(
            status_path,
            status_base,
            status="listening",
            stage="deal_processed",
            last_deal_result=_result_summary(result),
            last_receipt_result=_receipt_summary(result.get("receipt")),
        )
        _log(_format_result_summary(result))

    listener = OpenDTradePushListener(
        host=args.host,
        port=args.port,
        on_deal=_on_deal,
    )
    restart_count = 0
    while True:
        try:
            _write_listener_status(status_path, status_base, status="starting", stage="listener_start", restart_count=restart_count)
            listener.start()
            _log("[OK] auto trade intake listener started")
            _write_listener_status(status_path, status_base, status="listening", stage="listener_started", restart_count=restart_count)
            while True:
                _write_listener_status(status_path, status_base, status="listening", stage="heartbeat", restart_count=restart_count)
                time.sleep(60)
        except KeyboardInterrupt:
            listener.close()
            _write_listener_status(status_path, status_base, status="stopped", stage="keyboard_interrupt", restart_count=restart_count)
            return 0
        except Exception as exc:
            listener.close()
            restart_count += 1
            _write_listener_status(
                status_path,
                status_base,
                status="reconnecting",
                stage="listener_exception",
                restart_count=restart_count,
                last_error=f"{type(exc).__name__}: {exc}",
            )
            _log(f"[WARN] listener exited: {exc}; retry in {int(intake_cfg['reconnect_sec'])} sec")
            time.sleep(int(intake_cfg["reconnect_sec"]))


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


def _status_base_payload(
    *,
    cfg_path: Path,
    intake_cfg: dict[str, Any],
    state_path: Path,
    audit_path: Path,
    status_path: Path,
    host: str,
    port: int,
) -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "config_path": str(cfg_path),
        "mode": intake_cfg["mode"],
        "enabled": bool(intake_cfg["enabled"]),
        "state_path": str(state_path),
        "audit_path": str(audit_path),
        "status_path": str(status_path),
        "host": str(host),
        "port": int(port),
        "mapped_accounts": sorted(intake_cfg["account_mapping"].values()),
        "receipt": dict(intake_cfg.get("receipt") or {}),
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
