from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import src.application.wheel as wheel_application
from src.application.agent_tool_config import load_runtime_config
from src.application.cash_conversion import load_cash_fx_payload
from src.application.ledger.api import (
    open_position_ledger_from_runtime_config,
    resolve_position_data_config_path,
)
from src.application.wheel import (
    build_wheel_read_model,
    end_wheel_lifecycle,
    load_wheel_candidate_snapshot,
    resolve_wheel_config,
)
from src.application.wheel.capacity import (
    load_shared_cash_capacity_fact,
    load_shared_coverage_fact,
)
from src.application.wheel.config import build_wheel_policy_hash
from src.application.wheel.workflows import (
    cancel_wheel_intent,
    confirm_wheel_linkage,
    create_wheel_intent,
    reject_wheel_linkage,
)
from src.interfaces.cli.ledger_write_safety import (
    add_write_flags,
    guard_ledger_write,
    resolve_cli_write_control,
)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _add_common(parser: argparse.ArgumentParser) -> None:
    config = parser.add_mutually_exclusive_group(required=True)
    config.add_argument("--config-key", choices=("us", "hk"))
    config.add_argument("--config", dest="config_path")
    parser.add_argument("--data-config")
    parser.add_argument("--runtime-root")
    parser.add_argument("--as-of-ms", type=int)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    add_write_flags(parser, high_risk=True)


def _add_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", required=True)
    parser.add_argument("--stock-lot-id", required=True)
    parser.add_argument("--expected-batch-generation-hash", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--actor", required=True)


def _add_branch_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", required=True)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--wheel-branch-id")
    identity.add_argument("--stock-lot-id")
    parser.add_argument("--expected-branch-generation-hash", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--actor", required=True)


def _add_neutral_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", required=True)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--wheel-branch-id")
    identity.add_argument("--stock-lot-id")
    parser.add_argument("--direction", choices=("call", "put"), default="call")
    parser.add_argument(
        "--expected-branch-generation-hash",
        "--expected-batch-generation-hash",
        dest="expected_branch_generation_hash",
        required=True,
    )
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--actor", required=True)


def _add_activation_runtime(parser: argparse.ArgumentParser, *, write: bool) -> None:
    parser.add_argument("--market", choices=("us", "hk"), required=True)
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--data-config")
    parser.add_argument("--runtime-root")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    if write:
        add_write_flags(parser, high_risk=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage Wheel lifecycle facts")
    commands = parser.add_subparsers(dest="wheel_command", required=True)

    end = commands.add_parser("end", help="manually end one Wheel lifecycle")
    _add_identity(end)
    _add_common(end)

    branch = commands.add_parser("branch", help="start or end one Wheel branch")
    branch_commands = branch.add_subparsers(dest="branch_action", required=True)
    for action in ("start", "end"):
        command = branch_commands.add_parser(action)
        _add_branch_identity(command)
        _add_common(command)

    activation = commands.add_parser(
        "activation", help="inspect or change the local Wheel activation window"
    )
    activation_commands = activation.add_subparsers(
        dest="activation_action", required=True
    )
    status = activation_commands.add_parser("status")
    status.add_argument("--account", required=True)
    _add_activation_runtime(status, write=False)
    for action in ("enable", "disable"):
        command = activation_commands.add_parser(action)
        command.add_argument("--account", required=True)
        command.add_argument("--expected-current-generation", type=int, required=True)
        command.add_argument("--request-id", required=True)
        command.add_argument("--actor", required=True)
        _add_activation_runtime(command, write=True)

    intent = commands.add_parser("intent", help="manage Wheel option intents")
    intent_commands = intent.add_subparsers(dest="intent_action", required=True)
    create = intent_commands.add_parser("create")
    _add_neutral_identity(create)
    create.add_argument("--run-id", required=True)
    create.add_argument("--final-candidate-id", required=True)
    create.add_argument("--expected-snapshot-hash", required=True)
    create.add_argument("--expires-at-ms", type=int, required=True)
    create.add_argument("--broker-order-id")
    _add_common(create)
    cancel = intent_commands.add_parser("cancel")
    _add_neutral_identity(cancel)
    cancel.add_argument("--intent-id", required=True)
    cancel.add_argument("--broker-order-inactive-confirmed", action="store_true")
    cancel.add_argument("--reason", required=True)
    _add_common(cancel)

    linkage = commands.add_parser("linkage", help="resolve Wheel option attribution")
    linkage_commands = linkage.add_subparsers(dest="linkage_action", required=True)
    for action in ("confirm", "reject"):
        command = linkage_commands.add_parser(action)
        _add_neutral_identity(command)
        command.add_argument(
            "--option-record-id",
            "--call-record-id",
            dest="option_record_id",
            required=True,
        )
        command.add_argument("--linkage-candidate-id", required=True)
        command.add_argument("--expected-input-hash", required=True)
        if action == "reject":
            command.add_argument("--reason", required=True)
        _add_common(command)
    return parser.parse_args(argv)


def _open_runtime(args: argparse.Namespace, *, apply_changes: bool) -> tuple[Path, dict[str, Any], Any]:
    base = Path(__file__).resolve().parents[3]
    config_path_value = getattr(args, "config_path", None)
    config_key = getattr(args, "config_key", None)
    if config_path_value is None and config_key is None:
        config_key = getattr(args, "market", None)
    config_path, cfg = load_runtime_config(
        config_key=config_key,
        config_path=config_path_value,
        expected_market=getattr(args, "market", None),
    )
    data_config = resolve_position_data_config_path(
        base=base,
        cfg=cfg,
        data_config=args.data_config,
        config_path=config_path,
    )
    if apply_changes and guard_ledger_write(
        data_config=data_config,
        args=args,
        as_json=args.format == "json",
    ) is None:
        raise SystemExit(2)
    _resolved, repo = open_position_ledger_from_runtime_config(
        base=base,
        cfg=cfg,
        data_config=data_config,
        config_path=config_path,
        runtime_root=args.runtime_root,
    )
    return config_path, cfg, repo


def _write_requested(args: argparse.Namespace) -> bool:
    if (getattr(args, "confirm", False) or getattr(args, "yes", False)) and not getattr(
        args, "apply", False
    ):
        raise SystemExit("Wheel writes require --apply together with --confirm or --yes")
    return bool(
        resolve_cli_write_control(
            args,
            command_name="wheel",
            high_risk=True,
        )["write_requested"]
    )


def _batch(model: dict[str, Any], stock_lot_id: str) -> dict[str, Any]:
    matches = [
        item
        for item in model.get("batches") or []
        if item.get("stock_lot_id") == stock_lot_id
    ]
    if len(matches) != 1:
        raise ValueError(f"Wheel batch must resolve uniquely: {stock_lot_id}")
    return matches[0]


def _branch(
    model: dict[str, Any],
    *,
    wheel_branch_id: str | None,
    stock_lot_id: str | None,
) -> dict[str, Any]:
    branch_id = str(wheel_branch_id or "").strip()
    lot_id = str(stock_lot_id or "").strip()
    if bool(branch_id) == bool(lot_id):
        raise ValueError("Exactly one of wheel_branch_id or stock_lot_id is required")
    matches = [
        item
        for item in model.get("wheel_branches") or []
        if (
            branch_id
            and str(item.get("wheel_branch_id") or "").strip() == branch_id
        )
        or (
            lot_id
            and item.get("direction") == "call"
            and str(item.get("stock_lot_id") or "").strip() == lot_id
        )
    ]
    if len(matches) != 1:
        identity = branch_id or lot_id
        raise ValueError(f"Wheel branch must resolve uniquely: {identity}")
    return matches[0]


def _coverage(
    repo: Any,
    cfg: dict[str, Any],
    *,
    account: str,
    batch: dict[str, Any],
    as_of_ms: int,
    source_identity: str,
) -> dict[str, Any]:
    portfolio = cfg.get("portfolio")
    portfolio = portfolio if isinstance(portfolio, dict) else {}
    return load_shared_coverage_fact(
        repo,
        config=cfg,
        account=account,
        symbol=str(batch.get("symbol") or ""),
        broker=str(batch.get("broker") or portfolio.get("broker") or "富途"),
        as_of_ms=as_of_ms,
        source_identity=source_identity,
    )


def _cash_capacity(
    repo: Any,
    cfg: dict[str, Any],
    *,
    account: str,
    branch: dict[str, Any],
    as_of_ms: int,
) -> dict[str, Any]:
    portfolio = cfg.get("portfolio")
    portfolio = portfolio if isinstance(portfolio, dict) else {}
    return load_shared_cash_capacity_fact(
        repo,
        config=cfg,
        account=account,
        broker=str(branch.get("broker") or portfolio.get("broker") or "富途"),
        as_of_ms=as_of_ms,
        fx_snapshot=load_cash_fx_payload(repo, persist=False) or {},
    )


def execute(args: argparse.Namespace) -> dict[str, Any]:
    is_activation_status = (
        args.wheel_command == "activation" and args.activation_action == "status"
    )
    apply_changes = False if is_activation_status else _write_requested(args)
    config_path, cfg, repo = _open_runtime(args, apply_changes=apply_changes)
    if args.wheel_command == "activation":
        activation_args: dict[str, Any] = {
            "action": args.activation_action,
            "market": args.market,
            "account": args.account,
        }
        if not is_activation_status:
            activation_args.update(
                expected_current_generation=args.expected_current_generation,
                request_id=args.request_id,
                actor=args.actor,
                policy_sha256=build_wheel_policy_hash(
                    cfg,
                    market=args.market,
                    account=args.account,
                ),
                apply_changes=apply_changes,
            )
        return wheel_application.change_wheel_activation(repo, **activation_args)

    instant = int(getattr(args, "as_of_ms", None) or _now_ms())
    if args.wheel_command == "branch":
        model = build_wheel_read_model(
            repo,
            args.account,
            instant,
            market=args.config_key,
        )
        branch = _branch(
            model,
            wheel_branch_id=args.wheel_branch_id,
            stock_lot_id=args.stock_lot_id,
        )
        branch_args: dict[str, Any] = {
            "account": args.account,
            "wheel_branch_id": branch["wheel_branch_id"],
            "decision": args.branch_action,
            "expected_branch_generation_hash": args.expected_branch_generation_hash,
            "request_id": args.request_id,
            "actor": args.actor,
            "market": args.config_key,
            "apply_changes": apply_changes,
            "as_of_ms": instant,
        }
        if args.branch_action == "start":
            resolved = resolve_wheel_config(
                cfg,
                args.account,
                market=getattr(args, "config_key", None),
            )
            branch_args.update(
                market=resolved.get("market"),
                activation_descriptor=resolved.get("activation_descriptor"),
                policy_sha256=resolved.get("policy_sha256"),
            )
        return wheel_application.decide_wheel_branch(repo, **branch_args)

    if args.wheel_command == "end":
        return end_wheel_lifecycle(
            repo,
            account=args.account,
            stock_lot_id=args.stock_lot_id,
            expected_batch_generation_hash=args.expected_batch_generation_hash,
            request_id=args.request_id,
            actor=args.actor,
            market=args.config_key,
            apply_changes=apply_changes,
            as_of_ms=instant,
        )
    if args.stock_lot_id and args.direction != "call":
        raise ValueError("stock_lot_id is a legacy Call-only alias")
    model = build_wheel_read_model(
        repo,
        args.account,
        instant,
        market=args.config_key,
    )
    branch = _branch(
        model,
        wheel_branch_id=args.wheel_branch_id,
        stock_lot_id=args.stock_lot_id,
    )
    if branch.get("direction") != args.direction:
        raise ValueError("Wheel branch direction mismatch")
    common = {
        "account": args.account,
        "wheel_branch_id": branch["wheel_branch_id"],
        "direction": args.direction,
        "expected_branch_generation_hash": args.expected_branch_generation_hash,
        "request_id": args.request_id,
        "actor": args.actor,
        "market": args.config_key,
        "apply_changes": apply_changes,
        "as_of_ms": instant,
    }
    if args.wheel_command == "intent" and args.intent_action == "cancel":
        return cancel_wheel_intent(
            repo,
            **common,
            intent_id=args.intent_id,
            broker_order_inactive_confirmed=args.broker_order_inactive_confirmed,
            reason=args.reason,
            capacity_fact=(
                _cash_capacity(
                    repo,
                    cfg,
                    account=args.account,
                    branch=branch,
                    as_of_ms=instant,
                )
                if args.direction == "put"
                else None
            ),
        )
    if args.wheel_command == "intent":
        snapshot = load_wheel_candidate_snapshot(
            base=config_path.parent,
            run_id=args.run_id,
            account=args.account,
        )
        capacity_fact = (
            _coverage(
                repo,
                cfg,
                account=args.account,
                batch=branch,
                as_of_ms=instant,
                source_identity=args.request_id,
            )
            if args.direction == "call"
            else _cash_capacity(
                repo,
                cfg,
                account=args.account,
                branch=branch,
                as_of_ms=instant,
            )
        )
        resolved = resolve_wheel_config(
            cfg,
            args.account,
            market=args.config_key,
        )
        return create_wheel_intent(
            repo,
            **common,
            candidate_snapshot=snapshot,
            final_candidate_id=args.final_candidate_id,
            expected_snapshot_hash=args.expected_snapshot_hash,
            expires_at_ms=args.expires_at_ms,
            broker_order_id=args.broker_order_id,
            capacity_fact=capacity_fact,
            new_intent_enabled=resolved["enabled_for_new_lifecycle"],
            activation_descriptor=resolved.get("activation_descriptor"),
            policy_sha256=str(resolved.get("policy_sha256") or ""),
        )
    if args.linkage_action == "confirm":
        capacity_fact = (
            _coverage(
                repo,
                cfg,
                account=args.account,
                batch=branch,
                as_of_ms=instant,
                source_identity=args.request_id,
            )
            if args.direction == "call"
            else _cash_capacity(
                repo,
                cfg,
                account=args.account,
                branch=branch,
                as_of_ms=instant,
            )
        )
        return confirm_wheel_linkage(
            repo,
            **common,
            option_record_id=args.option_record_id,
            linkage_candidate_id=args.linkage_candidate_id,
            expected_input_hash=args.expected_input_hash,
            capacity_fact=capacity_fact,
        )
    return reject_wheel_linkage(
        repo,
        **common,
        option_record_id=args.option_record_id,
        linkage_candidate_id=args.linkage_candidate_id,
        expected_input_hash=args.expected_input_hash,
        reason=args.reason,
    )


def _print_result(result: dict[str, Any], *, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    for key, value in result.items():
        rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
        print(f"{key}: {rendered}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _print_result(execute(args), output_format=args.format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
