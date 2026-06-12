from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from src.application.ledger.api import ledger_store_write_guard
from src.application.write_contract import write_control


def runtime_root_arg(args: argparse.Namespace) -> str | None:
    return str(getattr(args, "runtime_root", "") or "").strip() or None


def add_write_flags(parser: argparse.ArgumentParser, *, high_risk: bool) -> None:
    parser.add_argument("--apply", action="store_true", help="allow local state writes")
    if high_risk:
        parser.add_argument("--confirm", action="store_true", help="confirm high-risk trade-event writes")
        parser.add_argument("--yes", action="store_true", help="non-interactive confirmation; emits an audit_id")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing; this is the default")


def resolve_cli_write_control(args: argparse.Namespace, *, command_name: str, high_risk: bool) -> dict[str, bool]:
    has_dry_run = bool(getattr(args, "dry_run", False))
    has_write_flag = any(bool(getattr(args, name, False)) for name in ("apply", "confirm", "yes"))
    if has_dry_run and has_write_flag:
        message = "--dry-run cannot be combined with --apply"
        if high_risk:
            message += ", --confirm, or --yes"
        raise SystemExit(message)
    control = write_control(
        apply=bool(getattr(args, "apply", False)),
        confirm=bool(getattr(args, "confirm", False)),
        yes=bool(getattr(args, "yes", False)),
        high_risk=high_risk,
    )
    if control["confirmation_required"]:
        raise SystemExit(f"{command_name} writes local ledger state; use --confirm or --yes to apply")
    return control


def guard_ledger_write(*, data_config: Path, args: argparse.Namespace, as_json: bool) -> dict[str, object] | None:
    guard = ledger_store_write_guard(data_config, runtime_root=runtime_root_arg(args))
    if bool(guard.get("ok")):
        return guard
    print_ledger_guard_failure(guard, as_json=as_json)
    return None


def print_ledger_guard_failure(guard: dict[str, object], *, as_json: bool) -> None:
    payload = {"ok": False, "error": "ledger_store_guard_failed", "ledger_store_guard": guard}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    raw_errors = guard.get("errors")
    errors = raw_errors if isinstance(raw_errors, list) else []
    for error in errors:
        print(f"[LEDGER_FAIL] {error}")
    raw_active = guard.get("active")
    active = cast(dict[str, object], raw_active) if isinstance(raw_active, dict) else {}
    print(
        f"[LEDGER] sqlite={active.get('sqlite_path') or '-'} "
        f"runtime_root={active.get('runtime_root') or '-'} "
        f"source={active.get('runtime_root_source') or '-'}"
    )
    raw_remediation = guard.get("remediation")
    remediation = raw_remediation if isinstance(raw_remediation, list) else []
    for item in remediation:
        print(f"[REMEDIATION] {item}")
