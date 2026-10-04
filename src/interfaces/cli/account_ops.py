from __future__ import annotations

import argparse
from typing import Any, Callable

from src.application.account_management import add_account, edit_account, list_accounts, remove_account
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.interfaces.cli.setup_ops import add_symbol_policy_arguments, symbol_policies_from_args


def add_account_commands(subparsers: Any) -> None:
    accounts = subparsers.add_parser("accounts", help="manage config.yaml account authority")
    account_sub = accounts.add_subparsers(dest="accounts_command", required=True)
    listing = account_sub.add_parser("list", help="list account mappings, environments and shared market symbols")
    listing.add_argument("--market", choices=("us", "hk"))
    listing.add_argument("--config-yaml", "--config-path", dest="config_path", default=None)
    add = account_sub.add_parser("add", help="add account")
    add.add_argument("--market", required=True, choices=("us", "hk"))
    add.add_argument("--account-label", required=True)
    add.add_argument("--account-type", default="futu", choices=("futu",), help=argparse.SUPPRESS)
    _add_common_write_args(add)
    add.add_argument("--futu-acc-id", default=None)
    add.add_argument("--futu-host", default=None)
    add.add_argument("--futu-port", type=int, default=None)
    add.add_argument("--trd-env", choices=("REAL", "SIMULATE"), default=None,
                     help="explicit trading environment for a new account")
    add.add_argument("--symbol", action="append", dest="symbols", help="initial symbol for a new market; repeatable")
    add_symbol_policy_arguments(add)
    edit = account_sub.add_parser("edit", help="edit account")
    edit.add_argument("--market", required=True, choices=("us", "hk"))
    edit.add_argument("--account-label", required=True)
    _add_common_write_args(edit)
    edit.add_argument("--account-type", choices=("futu",), default=None)
    edit.add_argument("--futu-acc-id", default=None)
    edit.add_argument("--futu-host", default=None)
    edit.add_argument("--futu-port", type=int, default=None)
    edit.add_argument("--trd-env", choices=("REAL", "SIMULATE"), default=None)
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
    parser.add_argument("--expected-source-sha256", default=None, help="source revision from the preview")
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
    list_accounts_fn: Callable[..., dict[str, Any]] = list_accounts,
) -> dict[str, Any]:
    if args.accounts_command == "list":
        return build_response(tool_name="accounts.list", ok=True,
                              data=list_accounts_fn(market=args.market, config_path=args.config_path))
    _enforce_write_gate(args)
    apply = bool(getattr(args, "apply", False))
    rebuild_runtime_root = getattr(args, "rebuild_runtime_root", None)
    if args.accounts_command == "add":
        if args.futu_acc_id and not getattr(args, "trd_env", None):
            raise AgentToolError(code="INPUT_ERROR", message="new account requires --trd-env REAL|SIMULATE")
        selected = getattr(args, "symbols", None)
        policies = symbol_policies_from_args(args, symbols={args.market: selected}, markets=[args.market])
        if not selected:
            policies = None
        return build_response(
            tool_name="accounts.add",
            ok=True,
            data=add_account_fn(
                market=args.market,
                account_label=args.account_label,
                account_type=args.account_type,
                symbols=selected,
                symbol_policies=policies,
                config_path=args.config_path,
                futu_acc_id=args.futu_acc_id,
                futu_host=args.futu_host,
                futu_port=args.futu_port,
                trd_env=getattr(args, "trd_env", None),
                rebuild_runtime_root=rebuild_runtime_root,
                apply=apply,
                expected_source_sha256=getattr(args, "expected_source_sha256", None),
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
                trd_env=getattr(args, "trd_env", None),
                rebuild_runtime_root=rebuild_runtime_root,
                apply=apply,
                expected_source_sha256=getattr(args, "expected_source_sha256", None),
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
                expected_source_sha256=getattr(args, "expected_source_sha256", None),
            ),
        )

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported accounts command: {args.accounts_command}")
