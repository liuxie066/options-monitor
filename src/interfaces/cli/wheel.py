from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.agent_tool_config import load_runtime_config
from src.application.ledger.api import (
    open_position_ledger_from_runtime_config,
    resolve_position_data_config_path,
)
from src.application.wheel import (
    build_wheel_read_model,
    cancel_wheel_call_intent,
    confirm_wheel_call_linkage,
    create_wheel_call_intent,
    end_wheel_lifecycle,
    load_wheel_candidate_snapshot,
    reject_wheel_call_linkage,
    resolve_wheel_config,
)
from src.application.wheel.capacity import load_shared_coverage_fact
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage Wheel lifecycle facts")
    commands = parser.add_subparsers(dest="wheel_command", required=True)

    end = commands.add_parser("end", help="manually end one Wheel lifecycle")
    _add_identity(end)
    _add_common(end)

    intent = commands.add_parser("intent", help="manage Wheel Call intents")
    intent_commands = intent.add_subparsers(dest="intent_action", required=True)
    create = intent_commands.add_parser("create")
    _add_identity(create)
    create.add_argument("--run-id", required=True)
    create.add_argument("--final-candidate-id", required=True)
    create.add_argument("--expected-snapshot-hash", required=True)
    create.add_argument("--expires-at-ms", type=int, required=True)
    create.add_argument("--broker-order-id")
    _add_common(create)
    cancel = intent_commands.add_parser("cancel")
    _add_identity(cancel)
    cancel.add_argument("--intent-id", required=True)
    cancel.add_argument("--broker-order-inactive-confirmed", action="store_true")
    cancel.add_argument("--reason", required=True)
    _add_common(cancel)

    linkage = commands.add_parser("linkage", help="resolve Wheel Call attribution")
    linkage_commands = linkage.add_subparsers(dest="linkage_action", required=True)
    for action in ("confirm", "reject"):
        command = linkage_commands.add_parser(action)
        _add_identity(command)
        command.add_argument("--call-record-id", required=True)
        command.add_argument("--linkage-candidate-id", required=True)
        command.add_argument("--expected-input-hash", required=True)
        if action == "reject":
            command.add_argument("--reason", required=True)
        _add_common(command)
    return parser.parse_args(argv)


def _open_runtime(args: argparse.Namespace, *, apply_changes: bool) -> tuple[Path, dict[str, Any], Any]:
    base = Path(__file__).resolve().parents[3]
    config_path, cfg = load_runtime_config(
        config_key=args.config_key,
        config_path=args.config_path,
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
    if (args.confirm or args.yes) and not args.apply:
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


def execute(args: argparse.Namespace) -> dict[str, Any]:
    apply_changes = _write_requested(args)
    config_path, cfg, repo = _open_runtime(args, apply_changes=apply_changes)
    instant = int(args.as_of_ms or _now_ms())
    common = {
        "account": args.account,
        "stock_lot_id": args.stock_lot_id,
        "expected_batch_generation_hash": args.expected_batch_generation_hash,
        "request_id": args.request_id,
        "actor": args.actor,
        "apply_changes": apply_changes,
        "as_of_ms": instant,
    }
    if args.wheel_command == "end":
        return end_wheel_lifecycle(repo, **common)
    if args.wheel_command == "intent" and args.intent_action == "cancel":
        return cancel_wheel_call_intent(
            repo,
            **common,
            intent_id=args.intent_id,
            broker_order_inactive_confirmed=args.broker_order_inactive_confirmed,
            reason=args.reason,
        )
    model = build_wheel_read_model(repo, args.account, instant)
    batch = _batch(model, args.stock_lot_id)
    if args.wheel_command == "intent":
        snapshot = load_wheel_candidate_snapshot(
            base=config_path.parent,
            run_id=args.run_id,
            account=args.account,
        )
        coverage = _coverage(
            repo,
            cfg,
            account=args.account,
            batch=batch,
            as_of_ms=instant,
            source_identity=args.request_id,
        )
        return create_wheel_call_intent(
            repo,
            **common,
            candidate_snapshot=snapshot,
            final_candidate_id=args.final_candidate_id,
            expected_snapshot_hash=args.expected_snapshot_hash,
            expires_at_ms=args.expires_at_ms,
            broker_order_id=args.broker_order_id,
            coverage_fact=coverage,
            new_intent_enabled=resolve_wheel_config(cfg, args.account)[
                "enabled_for_new_lifecycle"
            ],
        )
    if args.linkage_action == "confirm":
        coverage = _coverage(
            repo,
            cfg,
            account=args.account,
            batch=batch,
            as_of_ms=instant,
            source_identity=args.request_id,
        )
        return confirm_wheel_call_linkage(
            repo,
            **common,
            call_record_id=args.call_record_id,
            linkage_candidate_id=args.linkage_candidate_id,
            expected_input_hash=args.expected_input_hash,
            coverage_fact=coverage,
        )
    return reject_wheel_call_linkage(
        repo,
        **common,
        call_record_id=args.call_record_id,
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
