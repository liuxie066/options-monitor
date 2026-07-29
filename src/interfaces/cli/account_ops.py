from __future__ import annotations

import argparse
from typing import Any, Callable

from src.application.account_management import add_account, edit_account, remove_account
from src.application.agent_tool_contracts import AgentToolError, build_response


def add_account_commands(subparsers: Any) -> None:
    accounts = subparsers.add_parser("accounts", help="manage config.yaml account authority")
    account_sub = accounts.add_subparsers(dest="accounts_command", required=True)
    add = account_sub.add_parser("add", help="add account")
    add.add_argument("--market", required=True, choices=("us", "hk"))
    add.add_argument("--account-label", required=True)
    add.add_argument("--account-type", required=True, choices=("futu", "external_holdings"))
    _add_common_write_args(add)
    add.add_argument("--futu-acc-id", default=None)
    add.add_argument("--futu-host", default=None)
    add.add_argument("--futu-port", type=int, default=None)
    add.add_argument("--holdings-account", default=None)
    edit = account_sub.add_parser("edit", help="edit account")
    edit.add_argument("--market", required=True, choices=("us", "hk"))
    edit.add_argument("--account-label", required=True)
    _add_common_write_args(edit)
    edit.add_argument("--account-type", choices=("futu", "external_holdings"), default=None)
    edit.add_argument("--futu-acc-id", default=None)
    edit.add_argument("--futu-host", default=None)
    edit.add_argument("--futu-port", type=int, default=None)
    edit.add_argument("--holdings-account", default=None)
    edit.add_argument("--clear-holdings-account", action="store_true")
    remove = account_sub.add_parser("remove", help="remove account")
    remove.add_argument("--market", required=True, choices=("us", "hk"))
    remove.add_argument("--account-label", required=True)
    _add_common_write_args(remove)


def _add_common_write_args(parser: Any) -> None:
    parser.add_argument(
        "--config-yaml",
        "--config-path",
        dest="config_path",
        default=None,
        help="authoritative config.yaml path; --config-path is a compatibility alias",
    )
    parser.add_argument("--rebuild-runtime-root", default=None)
    parser.add_argument("--apply", action="store_true", help="publish config.yaml and generated snapshots")
    parser.add_argument("--confirm", action="store_true", help="required with --apply")


def _enforce_write_gate(args: argparse.Namespace) -> None:
    if not bool(getattr(args, "apply", False)):
        return
    if not bool(getattr(args, "confirm", False)):
        raise AgentToolError(
            code="CONFIRMATION_REQUIRED",
            message=f"--confirm is required for accounts {args.accounts_command} --apply",
            hint="Run without --apply to preview, then retry with --apply --confirm.",
        )


def handle_account_command(
    args: argparse.Namespace,
    *,
    add_account_fn: Callable[..., dict[str, Any]] = add_account,
    edit_account_fn: Callable[..., dict[str, Any]] = edit_account,
    remove_account_fn: Callable[..., dict[str, Any]] = remove_account,
) -> dict[str, Any]:
    _enforce_write_gate(args)
    apply = bool(getattr(args, "apply", False))
    rebuild_runtime_root = getattr(args, "rebuild_runtime_root", None)
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
                futu_host=args.futu_host,
                futu_port=args.futu_port,
                holdings_account=args.holdings_account,
                rebuild_runtime_root=rebuild_runtime_root,
                apply=apply,
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
                futu_host=args.futu_host,
                futu_port=args.futu_port,
                holdings_account=args.holdings_account,
                clear_holdings_account=bool(args.clear_holdings_account),
                rebuild_runtime_root=rebuild_runtime_root,
                apply=apply,
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
                rebuild_runtime_root=rebuild_runtime_root,
                apply=apply,
            ),
        )

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported accounts command: {args.accounts_command}")
