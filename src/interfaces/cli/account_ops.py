from __future__ import annotations

import argparse
from typing import Any, Callable

from src.application.account_management import add_account, edit_account, remove_account
from src.application.agent_tool_contracts import AgentToolError, build_response


def add_account_commands(subparsers: Any) -> None:
    accounts = subparsers.add_parser("accounts", help="manage runtime accounts")
    account_sub = accounts.add_subparsers(dest="accounts_command", required=True)
    add = account_sub.add_parser("add", help="add account")
    add.add_argument("--market", required=True, choices=("us", "hk"))
    add.add_argument("--account-label", required=True)
    add.add_argument("--account-type", required=True, choices=("futu", "external_holdings"))
    add.add_argument("--config-path", default=None)
    add.add_argument("--futu-acc-id", default=None)
    add.add_argument("--holdings-account", default=None)
    edit = account_sub.add_parser("edit", help="edit account")
    edit.add_argument("--market", required=True, choices=("us", "hk"))
    edit.add_argument("--account-label", required=True)
    edit.add_argument("--config-path", default=None)
    edit.add_argument("--account-type", choices=("futu", "external_holdings"), default=None)
    edit.add_argument("--futu-acc-id", default=None)
    edit.add_argument("--holdings-account", default=None)
    edit.add_argument("--clear-holdings-account", action="store_true")
    remove = account_sub.add_parser("remove", help="remove account")
    remove.add_argument("--market", required=True, choices=("us", "hk"))
    remove.add_argument("--account-label", required=True)
    remove.add_argument("--config-path", default=None)


def handle_account_command(
    args: argparse.Namespace,
    *,
    add_account_fn: Callable[..., dict[str, Any]] = add_account,
    edit_account_fn: Callable[..., dict[str, Any]] = edit_account,
    remove_account_fn: Callable[..., dict[str, Any]] = remove_account,
) -> dict[str, Any]:
    if args.accounts_command == "add":
        return build_response(
            tool_name="accounts.add",
            ok=True,
            data=add_account_fn(
                market=args.market,
                account_label=args.account_label,
                account_type=args.account_type,
                config_path=args.config_path,
                futu_acc_id=args.futu_acc_id,
                holdings_account=args.holdings_account,
            ),
        )

    if args.accounts_command == "edit":
        return build_response(
            tool_name="accounts.edit",
            ok=True,
            data=edit_account_fn(
                market=args.market,
                account_label=args.account_label,
                config_path=args.config_path,
                account_type=args.account_type,
                futu_acc_id=args.futu_acc_id,
                holdings_account=args.holdings_account,
                clear_holdings_account=bool(args.clear_holdings_account),
            ),
        )

    if args.accounts_command == "remove":
        return build_response(
            tool_name="accounts.remove",
            ok=True,
            data=remove_account_fn(
                market=args.market,
                account_label=args.account_label,
                config_path=args.config_path,
            ),
        )

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported accounts command: {args.accounts_command}")
