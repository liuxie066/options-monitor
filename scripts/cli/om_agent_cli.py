#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.agent_plugin.config import repo_base as agent_repo_base
from scripts.agent_plugin.contracts import AgentToolError, build_error_payload, build_response
from scripts.agent_plugin.init_local import (
    add_account_to_local_config,
    edit_account_in_local_config,
    init_local_config,
    remove_account_from_local_config,
)
from scripts.agent_plugin.main import build_spec, dumps_json, run_tool


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="options-monitor public local agent tools")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("spec", help="print public tool manifest")

    run_parser = sub.add_parser("run", help="run one public tool and print JSON envelope")
    run_parser.add_argument("--tool", required=True)
    run_parser.add_argument("--input-json", default="{}")
    run_parser.add_argument("--input-file", default=None, help="optional JSON file; overrides --input-json")

    init_parser = sub.add_parser("init", help="generate a local runtime config for one market/account")
    init_parser.add_argument("--market", required=True, choices=("us", "hk"))
    init_parser.add_argument("--futu-acc-id", required=True)
    init_parser.add_argument("--account-label", default="user1")
    init_parser.add_argument("--symbol", action="append", dest="symbols", default=None)
    init_parser.add_argument("--config-path", default=None)
    init_parser.add_argument("--data-config-path", default=None)
    init_parser.add_argument("--holdings-account", default=None)
    init_parser.add_argument("--opend-host", default="127.0.0.1")
    init_parser.add_argument("--opend-port", type=int, default=11111)
    init_parser.add_argument("--force", action="store_true")

    add_account_parser = sub.add_parser("add-account", help="append one account to an existing runtime config")
    add_account_parser.add_argument("--market", required=True, choices=("us", "hk"))
    add_account_parser.add_argument("--account-label", required=True)
    add_account_parser.add_argument("--account-type", required=True, choices=("futu", "external_holdings"))
    add_account_parser.add_argument("--config-path", default=None)
    add_account_parser.add_argument("--futu-acc-id", default=None)
    add_account_parser.add_argument("--holdings-account", default=None)

    edit_account_parser = sub.add_parser("edit-account", help="edit one existing account in a runtime config")
    edit_account_parser.add_argument("--market", required=True, choices=("us", "hk"))
    edit_account_parser.add_argument("--account-label", required=True)
    edit_account_parser.add_argument("--config-path", default=None)
    edit_account_parser.add_argument("--account-type", default=None, choices=("futu", "external_holdings"))
    edit_account_parser.add_argument("--futu-acc-id", default=None)
    edit_account_parser.add_argument("--holdings-account", default=None)
    edit_account_parser.add_argument("--clear-holdings-account", action="store_true")

    remove_account_parser = sub.add_parser("remove-account", help="remove one account from a runtime config")
    remove_account_parser.add_argument("--market", required=True, choices=("us", "hk"))
    remove_account_parser.add_argument("--account-label", required=True)
    remove_account_parser.add_argument("--config-path", default=None)
    return parser.parse_args(argv)


def _load_input_payload(args: argparse.Namespace) -> dict:
    if args.input_file:
        return json.loads(Path(args.input_file).read_text(encoding="utf-8"))
    raw = str(args.input_json or "{}").strip() or "{}"
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "spec":
        sys.stdout.write(dumps_json(build_spec()))
        return 0

    if args.command == "init":
        try:
            result = init_local_config(
                repo_root=agent_repo_base(),
                market=str(args.market),
                futu_acc_id=str(args.futu_acc_id),
                account_label=str(args.account_label),
                symbols=args.symbols,
                config_path=args.config_path,
                data_config_path=args.data_config_path,
                holdings_account=args.holdings_account,
                opend_host=str(args.opend_host),
                opend_port=int(args.opend_port),
                force=bool(args.force),
            )
            sys.stdout.write(dumps_json(build_response(tool_name="init", ok=True, data=result)))
            return 0
        except AgentToolError as err:
            sys.stdout.write(
                dumps_json(
                    build_response(tool_name="init", ok=False, error=build_error_payload(err))
                )
            )
            return 2

    if args.command == "add-account":
        try:
            result = add_account_to_local_config(
                repo_root=agent_repo_base(),
                market=str(args.market),
                account_label=str(args.account_label),
                account_type=str(args.account_type),
                config_path=args.config_path,
                futu_acc_id=args.futu_acc_id,
                holdings_account=args.holdings_account,
            )
            sys.stdout.write(dumps_json(build_response(tool_name="add-account", ok=True, data=result)))
            return 0
        except AgentToolError as err:
            sys.stdout.write(
                dumps_json(
                    build_response(tool_name="add-account", ok=False, error=build_error_payload(err))
                )
            )
            return 2

    if args.command == "edit-account":
        try:
            result = edit_account_in_local_config(
                repo_root=agent_repo_base(),
                market=str(args.market),
                account_label=str(args.account_label),
                config_path=args.config_path,
                account_type=args.account_type,
                futu_acc_id=args.futu_acc_id,
                holdings_account=args.holdings_account,
                clear_holdings_account=bool(args.clear_holdings_account),
            )
            sys.stdout.write(dumps_json(build_response(tool_name="edit-account", ok=True, data=result)))
            return 0
        except AgentToolError as err:
            sys.stdout.write(
                dumps_json(
                    build_response(tool_name="edit-account", ok=False, error=build_error_payload(err))
                )
            )
            return 2

    if args.command == "remove-account":
        try:
            result = remove_account_from_local_config(
                repo_root=agent_repo_base(),
                market=str(args.market),
                account_label=str(args.account_label),
                config_path=args.config_path,
            )
            sys.stdout.write(dumps_json(build_response(tool_name="remove-account", ok=True, data=result)))
            return 0
        except AgentToolError as err:
            sys.stdout.write(
                dumps_json(
                    build_response(tool_name="remove-account", ok=False, error=build_error_payload(err))
                )
            )
            return 2

    payload = _load_input_payload(args)
    out = run_tool(str(args.tool), payload)
    sys.stdout.write(dumps_json(out))
    return 0 if out.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
