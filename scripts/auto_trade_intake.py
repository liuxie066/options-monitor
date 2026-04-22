#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.config_loader import load_config, resolve_pm_config_path
from scripts.option_positions_core.service import OptionPositionsRepository, load_table_ref
from scripts.trade_account_mapping import resolve_trade_intake_config
from scripts.trade_event_normalizer import normalize_trade_deal
from scripts.trade_intake_resolver import resolve_trade_deal
from scripts.trade_intake_state import (
    append_trade_intake_audit,
    load_trade_intake_state,
    upsert_deal_state,
    write_trade_intake_state,
)
from scripts.trade_push_listener import OpenDTradePushListener


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Auto trade intake via OpenD deal push")
    ap.add_argument("--config", default="config.us.json")
    ap.add_argument("--pm-config", default=None)
    ap.add_argument("--mode", choices=["dry-run", "apply"], default=None)
    ap.add_argument("--state-path", default=None)
    ap.add_argument("--audit-path", default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=11111)
    ap.add_argument("--once", action="store_true", help="Validate config and exit")
    ap.add_argument("--deal-json", default=None, help="Replay a single normalized/raw deal payload from a JSON file")
    return ap.parse_args(argv)


def _log(message: str) -> None:
    print(message, flush=True)


def _build_audit_event(phase: str, *, payload: dict | None = None, deal: object | None = None, result: dict | None = None) -> dict:
    out: dict = {"phase": str(phase)}
    if isinstance(payload, dict):
        out["payload"] = payload
    if deal is not None and hasattr(deal, "to_dict"):
        deal_dict = deal.to_dict()
        out["deal"] = deal_dict
        out["deal_id"] = deal_dict.get("deal_id")
        out["account"] = deal_dict.get("internal_account")
        out["symbol"] = deal_dict.get("symbol")
        out["position_effect"] = deal_dict.get("position_effect")
        out["multiplier"] = deal_dict.get("multiplier")
        out["multiplier_source"] = deal_dict.get("multiplier_source")
    if isinstance(result, dict):
        out["result"] = result
        out["deal_id"] = out.get("deal_id") or result.get("deal_id")
        out["account"] = out.get("account") or result.get("account")
        out["action"] = result.get("action")
        out["status"] = result.get("status")
        out["reason"] = result.get("reason")
    return out


def _process_payload(
    payload: dict,
    *,
    repo: OptionPositionsRepository,
    state_path: Path,
    audit_path: Path,
    account_mapping: dict[str, str],
    apply_changes: bool,
) -> dict:
    state = load_trade_intake_state(state_path) if apply_changes else {}
    append_trade_intake_audit(audit_path, _build_audit_event("received", payload=payload))
    deal = normalize_trade_deal(payload, futu_account_mapping=account_mapping)
    append_trade_intake_audit(audit_path, _build_audit_event("normalized", deal=deal))
    result = resolve_trade_deal(deal, repo=repo, state=state, apply_changes=apply_changes)
    append_trade_intake_audit(audit_path, _build_audit_event("resolved", deal=deal, result=result.to_dict()))

    if apply_changes and deal.deal_id:
        if result.status == "applied":
            state = upsert_deal_state(
                state,
                bucket="processed_deal_ids",
                deal_id=deal.deal_id,
                payload={
                    "status": "applied",
                    "action": result.action,
                    "account": result.account,
                    "applied_record_ids": [op.get("record_id") for op in result.operations if op.get("record_id")],
                    "reason": result.reason,
                },
            )
            write_trade_intake_state(state_path, state)
        elif result.status == "unresolved":
            state = upsert_deal_state(
                state,
                bucket="unresolved_deal_ids",
                deal_id=deal.deal_id,
                payload={
                    "status": "unresolved",
                    "action": result.action,
                    "account": result.account,
                    "applied_record_ids": [],
                    "reason": result.reason,
                },
            )
            write_trade_intake_state(state_path, state)
        elif result.status == "failed":
            state = upsert_deal_state(
                state,
                bucket="failed_deal_ids",
                deal_id=deal.deal_id,
                payload={
                    "status": "failed",
                    "action": result.action,
                    "account": result.account,
                    "applied_record_ids": [],
                    "reason": result.reason,
                },
            )
            write_trade_intake_state(state_path, state)
    return result.to_dict()


class _ReplayRepo:
    def list_records(self, *, page_size: int = 500) -> list[dict]:
        return []

    def get_record_fields(self, record_id: str) -> dict:
        raise KeyError(record_id)

    def create_record(self, fields: dict) -> dict:
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
    )
    state_path = intake_cfg["state_path"]
    audit_path = intake_cfg["audit_path"]
    if not state_path.is_absolute():
        state_path = (base / state_path).resolve()
    if not audit_path.is_absolute():
        audit_path = (base / audit_path).resolve()

    if args.once and not args.deal_json:
        _log(
            json.dumps(
                {
                    "ok": True,
                    "mode": intake_cfg["mode"],
                    "enabled": bool(intake_cfg["enabled"]),
                    "state_path": str(state_path),
                    "audit_path": str(audit_path),
                    "mapped_accounts": sorted(intake_cfg["account_mapping"].values()),
                },
                ensure_ascii=False,
            )
        )
        return 0

    apply_changes = intake_cfg["mode"] == "apply"

    if args.deal_json:
        payload = json.loads(Path(args.deal_json).read_text(encoding="utf-8"))
        if apply_changes:
            pm_config = resolve_pm_config_path(base=base, pm_config=args.pm_config)
            repo: OptionPositionsRepository | _ReplayRepo = OptionPositionsRepository(load_table_ref(pm_config))
        else:
            repo = _ReplayRepo()
        result = _process_payload(
            payload,
            repo=repo,
            state_path=state_path,
            audit_path=audit_path,
            account_mapping=intake_cfg["account_mapping"],
            apply_changes=apply_changes,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    pm_config = resolve_pm_config_path(base=base, pm_config=args.pm_config)
    repo = OptionPositionsRepository(load_table_ref(pm_config))

    if not bool(intake_cfg["enabled"]):
        raise SystemExit("trade_intake.enabled=false; refusing to start listener")

    listener = OpenDTradePushListener(
        host=args.host,
        port=args.port,
        on_deal=lambda payload: _process_payload(
            payload,
            repo=repo,
            state_path=state_path,
            audit_path=audit_path,
            account_mapping=intake_cfg["account_mapping"],
            apply_changes=apply_changes,
        ),
    )
    while True:
        try:
            listener.start()
            _log("[OK] auto trade intake listener started")
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            listener.close()
            return 0
        except Exception as exc:
            listener.close()
            _log(f"[WARN] listener exited: {exc}; retry in {int(intake_cfg['reconnect_sec'])} sec")
            time.sleep(int(intake_cfg["reconnect_sec"]))


if __name__ == "__main__":
    raise SystemExit(main())
