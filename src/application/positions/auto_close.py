from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from domain.domain.ledger.position_fields import normalize_account, normalize_broker
from domain.storage.json_io import atomic_write_json as write_json
from domain.storage.repositories import run_repo, state_repo
from src.application.account_config import accounts_from_config
from src.application.config_loader import load_config
from src.application.positions.maintenance import (
    format_auto_close_summary,
    run_expired_position_maintenance_for_account,
)
from src.application.positions.maintenance_receipt import safe_send_auto_close_receipt
from src.application.write_contract import attach_write_contract, write_control
from src.infrastructure.io_utils import utc_now
from src.infrastructure.run_log import RunLogger


class _RunLoggerLike(Protocol):
    run_id: str

    def safe_event(self, step: str, status: str, **kwargs: Any) -> None: ...


def _parse_as_of_ms(value: str | None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit("--as-of-utc must be an ISO datetime, for example 2026-05-15T16:10:00Z") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return int(parsed.timestamp() * 1000)


def _copy_config_for_account(
    cfg: dict[str, Any],
    *,
    account: str,
    config_path: Path | None,
    data_config: str | None,
) -> dict[str, Any]:
    out = json.loads(json.dumps(cfg))
    if config_path is not None:
        out["config_source_path"] = str(config_path.resolve())
    portfolio = out.setdefault("portfolio", {})
    if isinstance(portfolio, dict):
        portfolio["account"] = account
        if data_config is not None and str(data_config).strip():
            portfolio["data_config"] = str(data_config)
    return out


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


def _error_result(
    *,
    cfg: dict[str, Any],
    account: str,
    broker: str | None,
    exc: BaseException,
) -> dict[str, Any]:
    errors = [f"expired_position_maintenance failed for {account}: {type(exc).__name__}: {exc}"]
    result: dict[str, Any] = {
        "mode": "error",
        "account": normalize_account(account),
        "broker": normalize_broker(broker) if broker else None,
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


def _ensure_receipt(
    *,
    base: Path,
    cfg: dict[str, Any],
    dry_run: bool,
    no_send: bool,
    result: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(result.get("receipt"), dict):
        return result
    out = dict(result)
    if no_send:
        out["receipt"] = {
            "enabled": True,
            "status": "skipped",
            "reason": "skipped_no_send",
            "delivery_confirmed": False,
            "message_id": None,
        }
    else:
        out["receipt"] = safe_send_auto_close_receipt(
            base=base,
            config=cfg,
            dry_run=bool(dry_run),
            result=out,
        )
    return out


def _account_status(result: dict[str, Any], *, dry_run: bool) -> str:
    mode = str(result.get("mode") or "").strip().lower()
    error_count = len(result.get("errors") or [])
    applied = int(result.get("applied_closed") or 0)
    if mode == "error":
        return "failed"
    if error_count > 0:
        return "partial_failed" if applied > 0 else "failed"
    if mode == "skipped":
        return "skipped"
    if dry_run or mode == "dry_run":
        return "dry_run"
    if applied > 0:
        return "applied"
    return "noop"


def _aggregate_status(account_results: list[dict[str, Any]]) -> str:
    statuses = [str(item.get("status") or "") for item in account_results]
    if any(status in {"failed", "partial_failed"} for status in statuses):
        return "partial_failed" if any(status in {"applied", "noop", "dry_run", "skipped"} for status in statuses) else "failed"
    if any(status == "applied" for status in statuses):
        return "applied"
    if any(status == "dry_run" for status in statuses):
        return "dry_run"
    if statuses and all(status == "skipped" for status in statuses):
        return "skipped"
    return "noop"


def _receipt_summary(receipt: Any) -> dict[str, Any] | None:
    if not isinstance(receipt, dict):
        return None
    return {
        "status": receipt.get("status"),
        "reason": receipt.get("reason"),
        "delivery_confirmed": bool(receipt.get("delivery_confirmed")),
        "message_id": receipt.get("message_id"),
        "error_code": receipt.get("error_code"),
        "attempt_count": receipt.get("attempt_count"),
        "receipt_key": receipt.get("receipt_key"),
    }


def _summary(account_results: list[dict[str, Any]]) -> dict[str, int]:
    out = {
        "accounts": len(account_results),
        "positions_checked": 0,
        "candidates_should_close": 0,
        "applied_closed": 0,
        "skipped_already_closed": 0,
        "errors": 0,
    }
    for item in account_results:
        raw_result = item.get("result")
        result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
        out["positions_checked"] += int(result.get("positions_checked") or 0)
        out["candidates_should_close"] += int(result.get("candidates_should_close") or 0)
        out["applied_closed"] += int(result.get("applied_closed") or 0)
        out["skipped_already_closed"] += int(result.get("skipped_already_closed") or 0)
        out["errors"] += len(result.get("errors") or [])
    return out


def run_auto_close_expired(
    *,
    base: Path,
    config_path: Path | None,
    data_config: str | None,
    accounts: list[str],
    broker: str | None,
    apply_mode: bool,
    no_send: bool,
    as_of_ms: int | None = None,
    runlog: _RunLoggerLike | None = None,
) -> dict[str, Any]:
    base = base.resolve()
    logger = runlog or RunLogger(base)
    run_id = logger.run_id
    run_dir = run_repo.ensure_run_dir(base, run_id)
    run_repo.ensure_run_state_dir(base, run_id)
    state_repo.write_last_run_dir_pointer(base, run_id)

    runtime_config: dict[str, Any]
    resolved_config_path: Path | None = None
    if config_path is not None:
        resolved_config_path = config_path if config_path.is_absolute() else (base / config_path).resolve()
        runtime_config = load_config(
            base=base,
            config_path=resolved_config_path,
            is_scheduled=False,
            log=lambda msg: print(msg, file=sys.stderr),
        )
    else:
        runtime_config = {"portfolio": {}}

    if data_config is not None and str(data_config).strip():
        runtime_config.setdefault("portfolio", {})
        if isinstance(runtime_config["portfolio"], dict):
            runtime_config["portfolio"]["data_config"] = str(data_config)

    account_ids = accounts_from_config({"accounts": accounts}, fallback=()) if accounts else accounts_from_config(runtime_config)
    account_ids = [str(item).strip().lower() for item in account_ids if str(item).strip()]
    if not account_ids:
        raise SystemExit("--accounts is required when runtime config does not define accounts")

    dry_run = not bool(apply_mode)
    account_results: list[dict[str, Any]] = []
    for account in account_ids:
        account_cfg = _copy_config_for_account(
            runtime_config,
            account=account,
            config_path=resolved_config_path,
            data_config=data_config,
        )
        report_dir = run_repo.ensure_run_account_dir(base, run_id, account)
        portfolio_cfg = account_cfg.get("portfolio") if isinstance(account_cfg.get("portfolio"), dict) else {}
        effective_broker = broker if broker is not None else (
            portfolio_cfg.get("broker") if isinstance(portfolio_cfg, dict) else None
        )
        try:
            result = run_expired_position_maintenance_for_account(
                base=base,
                cfg=account_cfg,
                account=account,
                report_dir=report_dir,
                as_of_ms=as_of_ms,
                broker=effective_broker,
                dry_run=dry_run,
                send_receipt=(not no_send),
            )
        except Exception as exc:
            result = _error_result(
                cfg=account_cfg,
                account=account,
                broker=str(effective_broker or ""),
                exc=exc,
            )
        result = _ensure_receipt(
            base=base,
            cfg=account_cfg,
            dry_run=dry_run,
            no_send=no_send,
            result=result,
        )
        status = _account_status(result, dry_run=dry_run)
        state_repo.write_account_run_state(base, run_id, account, "expired_position_maintenance.json", result)
        state_repo.append_run_audit_jsonl(
            base,
            run_id,
            "tool_execution_audit.jsonl",
            {
                "as_of_utc": utc_now(),
                "tool_name": "expired_position_maintenance",
                "account": account,
                "status": status,
                "mode": result.get("mode"),
                "positions_checked": result.get("positions_checked"),
                "candidates_should_close": result.get("candidates_should_close"),
                "applied_closed": result.get("applied_closed"),
                "errors": len(result.get("errors") or []),
                "receipt": _receipt_summary(result.get("receipt")),
            },
        )
        logger.safe_event(
            "expired_position_maintenance",
            "error" if status in {"failed", "partial_failed"} else "ok",
            data={
                "account": account,
                "status": status,
                "mode": result.get("mode"),
                "applied_closed": result.get("applied_closed"),
                "errors": len(result.get("errors") or []),
            },
        )
        account_results.append(
            {
                "account": account,
                "status": status,
                "result": result,
                "receipt": _receipt_summary(result.get("receipt")),
            }
        )

    status = _aggregate_status(account_results)
    output = {
        "schema_kind": "option_positions_auto_close_expired_run",
        "schema_version": "1.0",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "status": status,
        "mode": "apply" if apply_mode else "dry_run",
        "dry_run": dry_run,
        "runtime_config_path": str(resolved_config_path) if resolved_config_path is not None else None,
        "data_config": data_config,
        "accounts": account_ids,
        "broker": normalize_broker(broker) if broker else None,
        "as_of_utc": (
            datetime.fromtimestamp(as_of_ms / 1000, tz=timezone.utc).isoformat()
            if as_of_ms is not None
            else None
        ),
        "summary": _summary(account_results),
        "account_results": account_results,
    }
    state_repo.write_shared_state(base, "auto_close_expired.json", output)
    write_json(run_repo.ensure_run_state_dir(base, run_id) / "last_run.json", output)
    logger.safe_event(
        "run_end",
        "error" if status in {"failed", "partial_failed"} else "ok",
        data={"status": status, "summary": output["summary"]},
    )
    return output


def _print_text(result: dict[str, Any]) -> None:
    raw_summary = result.get("summary")
    summary: dict[str, Any] = raw_summary if isinstance(raw_summary, dict) else {}
    print(
        "[DONE] auto-close expired "
        f"status={result.get('status')} mode={result.get('mode')} "
        f"accounts={summary.get('accounts', 0)} "
        f"candidates={summary.get('candidates_should_close', 0)} "
        f"applied={summary.get('applied_closed', 0)} "
        f"errors={summary.get('errors', 0)} "
        f"run_id={result.get('run_id')}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auto-close expired option position lots")
    parser.add_argument("--config", default=None, help="runtime config path; provides accounts and portfolio.data_config")
    parser.add_argument("--data-config", default=None, help="portfolio data config path; overrides runtime config when provided")
    parser.add_argument("--accounts", nargs="+", default=None, help="accounts to process; defaults to runtime config accounts")
    parser.add_argument("--broker", default=None, help="optional broker filter override")
    parser.add_argument("--apply", action="store_true", help="append close events for expired lots")
    parser.add_argument("--confirm", action="store_true", help="confirm high-risk close-event writes and receipts")
    parser.add_argument("--yes", action="store_true", help="non-interactive confirmation; emits an audit_id")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing close events")
    parser.add_argument("--as-of-utc", default=None, help="ISO datetime; default is current UTC")
    parser.add_argument("--no-send", action="store_true", help="do not send auto-close receipt notifications")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument("--quiet", action="store_true", help="suppress stdout")
    args = parser.parse_args(argv)

    if args.dry_run and any(bool(getattr(args, name, False)) for name in ("apply", "confirm", "yes")):
        raise SystemExit("--dry-run cannot be combined with --apply, --confirm, or --yes")
    control = write_control(
        apply=bool(args.apply),
        confirm=bool(args.confirm),
        yes=bool(args.yes),
        high_risk=True,
    )
    if control["confirmation_required"]:
        raise SystemExit("auto-close-expired writes trade_events and may send receipts; use --confirm or --yes to apply")

    base = Path(__file__).resolve().parents[3]
    config_path = Path(args.config) if args.config else None
    result = run_auto_close_expired(
        base=base,
        config_path=config_path,
        data_config=args.data_config,
        accounts=list(args.accounts or []),
        broker=args.broker,
        apply_mode=bool(control["write_requested"]),
        no_send=bool(args.no_send),
        as_of_ms=_parse_as_of_ms(args.as_of_utc),
    )
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    result = attach_write_contract(
        result,
        dry_run=not bool(control["write_requested"]),
        write_applied=bool(control["write_requested"] and int(summary.get("applied_closed") or 0) > 0),
        rollback_hint="void auto-close close events or restore option_positions SQLite from backup",
    )
    if not args.quiet:
        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            _print_text(result)
    return 1 if str(result.get("status") or "") in {"failed", "partial_failed"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
