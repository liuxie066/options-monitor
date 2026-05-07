from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response
from src.application.agent_tool_config import write_tools_enabled
from src.application.account_management import add_account, edit_account, remove_account
from src.application.tool_execution import build_tool_manifest, execute_tool


def dumps_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="options-monitor public local agent tools")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("spec", help="print public tool manifest")

    run_parser = sub.add_parser("run", help="run one public tool and print JSON envelope")
    run_parser.add_argument("--tool", required=True)
    run_parser.add_argument("--input-json", default="{}")
    run_parser.add_argument("--input-file", default=None, help="optional JSON file; overrides --input-json")

    add_account_parser = sub.add_parser("add-account", help="append one account to an existing runtime config")
    add_account_parser.add_argument("--market", required=True, choices=("us", "hk"))
    add_account_parser.add_argument("--account-label", required=True)
    add_account_parser.add_argument("--account-type", required=True, choices=("futu", "external_holdings"))
    add_account_parser.add_argument("--config-path", default=None)
    add_account_parser.add_argument("--futu-acc-id", default=None)
    add_account_parser.add_argument("--holdings-account", default=None)
    add_account_parser.add_argument("--dry-run", action="store_true", help="validate and preview the mutation without writing")
    add_account_parser.add_argument("--confirm", action="store_true", help="required with OM_AGENT_ENABLE_WRITE_TOOLS=true for writes")

    edit_account_parser = sub.add_parser("edit-account", help="edit one existing account in a runtime config")
    edit_account_parser.add_argument("--market", required=True, choices=("us", "hk"))
    edit_account_parser.add_argument("--account-label", required=True)
    edit_account_parser.add_argument("--config-path", default=None)
    edit_account_parser.add_argument("--account-type", default=None, choices=("futu", "external_holdings"))
    edit_account_parser.add_argument("--futu-acc-id", default=None)
    edit_account_parser.add_argument("--holdings-account", default=None)
    edit_account_parser.add_argument("--clear-holdings-account", action="store_true")
    edit_account_parser.add_argument("--dry-run", action="store_true", help="validate and preview the mutation without writing")
    edit_account_parser.add_argument("--confirm", action="store_true", help="required with OM_AGENT_ENABLE_WRITE_TOOLS=true for writes")

    remove_account_parser = sub.add_parser("remove-account", help="remove one account from a runtime config")
    remove_account_parser.add_argument("--market", required=True, choices=("us", "hk"))
    remove_account_parser.add_argument("--account-label", required=True)
    remove_account_parser.add_argument("--config-path", default=None)
    remove_account_parser.add_argument("--dry-run", action="store_true", help="validate and preview the mutation without writing")
    remove_account_parser.add_argument("--confirm", action="store_true", help="required with OM_AGENT_ENABLE_WRITE_TOOLS=true for writes")
    return parser.parse_args(argv)


def _load_input_payload(args: argparse.Namespace) -> dict:
    if args.input_file:
        return json.loads(Path(args.input_file).read_text(encoding="utf-8"))
    raw = str(args.input_json or "{}").strip() or "{}"
    return json.loads(raw)


def _enforce_account_write_gate(args: argparse.Namespace) -> None:
    if bool(args.dry_run):
        return
    if not write_tools_enabled():
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message=f"{args.command} write mode is disabled",
            hint="Set OM_AGENT_ENABLE_WRITE_TOOLS=true and pass --confirm, or pass --dry-run to preview without writing.",
        )
    if not bool(args.confirm):
        raise AgentToolError(
            code="CONFIRMATION_REQUIRED",
            message=f"--confirm is required for {args.command} writes",
            hint="Run with --dry-run first, then retry with --confirm only when the config write is intended.",
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "spec":
        sys.stdout.write(dumps_json(build_tool_manifest()))
        return 0
    try:
        if args.command == "add-account":
            _enforce_account_write_gate(args)
            result = add_account(
                market=str(args.market),
                account_label=str(args.account_label),
                account_type=str(args.account_type),
                config_path=args.config_path,
                futu_acc_id=args.futu_acc_id,
                holdings_account=args.holdings_account,
                dry_run=bool(args.dry_run),
            )
            sys.stdout.write(dumps_json(build_response(tool_name="add-account", ok=True, data=result)))
            return 0
        if args.command == "edit-account":
            _enforce_account_write_gate(args)
            result = edit_account(
                market=str(args.market),
                account_label=str(args.account_label),
                config_path=args.config_path,
                account_type=args.account_type,
                futu_acc_id=args.futu_acc_id,
                holdings_account=args.holdings_account,
                clear_holdings_account=bool(args.clear_holdings_account),
                dry_run=bool(args.dry_run),
            )
            sys.stdout.write(dumps_json(build_response(tool_name="edit-account", ok=True, data=result)))
            return 0
        if args.command == "remove-account":
            _enforce_account_write_gate(args)
            result = remove_account(
                market=str(args.market),
                account_label=str(args.account_label),
                config_path=args.config_path,
                dry_run=bool(args.dry_run),
            )
            sys.stdout.write(dumps_json(build_response(tool_name="remove-account", ok=True, data=result)))
            return 0
        payload = _load_input_payload(args)
        out = execute_tool(str(args.tool), payload)
        sys.stdout.write(dumps_json(out))
        return 0 if out.get("ok") else 2
    except AgentToolError as err:
        sys.stdout.write(dumps_json(build_response(tool_name=str(args.command), ok=False, error=build_error_payload(err))))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
