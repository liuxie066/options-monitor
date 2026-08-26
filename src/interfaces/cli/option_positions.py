"""Manage position lots via trade events.

Supports open, buy-to-close, and list flows on top of the
trade-events / position-lots model.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from domain.domain.ledger.position_fields import (
    normalize_account,
    normalize_broker,
)
from src.application.config_loader import resolve_data_config_path
from src.application.ledger.api import (
    activate_position_projection_checkpoints,
    adopt_post_trade_combo_pair,
    adopt_existing_combo_identity,
    apply_current_decision_projection_migration,
    apply_position_projection_migration,
    build_current_decision_projection_migration_inventory,
    build_position_projection_migration_inventory,
    current_decision_projection_migration_status,
    deactivate_position_projection_checkpoints,
    format_position_cash_secured,
    format_position_money,
    inspect_ledger_stores,
    ledger_store_payload,
    list_trade_lifecycle_cases,
    list_trade_lifecycle_evidence,
    list_position_rows,
    list_combo_pair_inferences,
    open_position_ledger_from_runtime_config,
    position_projection_migration_status,
    record_trade_event_void,
    reconcile_combo_pair_inferences,
    reject_post_trade_combo_pair,
    refresh_position_lot_projection,
    resolve_ledger_store,
    resolve_position_data_config_path,
    preview_trade_event_void,
    verify_position_lot_projection,
    verify_current_decision_projection_migration,
    verify_position_projection_migration,
    supersede_post_trade_combo_pair,
)
from src.application.daily_decision_brief_repository import read_combo_candidate_exposures
from src.application.ledger.api import (
    apply_lifecycle_migration_manifest,
    build_lifecycle_migration_inventory,
    select_lifecycle_migration_targets,
)
from src.application.positions.auto_close import main as run_option_positions_auto_close
from src.application.positions.workflows import (
    ManualCloseMatchError,
    execute_manual_adjust,
    execute_manual_assignment,
    execute_manual_assigned_stock_sale,
    execute_manual_close,
    execute_manual_exercise,
    execute_manual_open,
    format_manual_close_match_error,
)
from src.application.positions.inspection import build_lot_event_history, inspect_projection_state
from src.application.trade_time_format import add_trade_time_beijing
from src.application.trades.lifecycle_reconciliation import (
    discover_lifecycle_cases,
    lifecycle_case_read_model,
    reconcile_lifecycle_evidence,
)
from src.application.trades.account_mapping import (
    combo_reconciliation_mode_for_account,
    resolve_trade_intake_config,
)
from src.application.account_config import resolve_account_broker_binding_sets
from src.application.futu_quote_routing import resolve_futu_quote_route
from src.application.trades.lifecycle_runtime import (
    reconcile_due_lifecycle_cases_for_source,
)
from src.application.trades.state import (
    append_lifecycle_attempt_checkpoint_seal,
    append_trade_intake_audit,
)
from src.application.trades.lifecycle_outbox import (
    dispatch_notification_batch_once,
    plan_notification_batch,
    reconcile_notification_batch,
    reconcile_unknown_notification,
)
from src.application.trades.manual_lifecycle_resolution import (
    resolve_lifecycle_manually,
)
from src.application.trades.receipt import (
    resolve_trade_lifecycle_notification_batch_route,
    send_trade_lifecycle_outbox_payload,
)
from src.application.cash_conversion import utc_now_ms
from src.application.wheel.config import resolve_wheel_config
from src.infrastructure.futu_gateway import (
    build_ready_futu_broker_gateway,
    build_ready_futu_quote_gateway,
)
from src.application.trades.review import replay_trade_events
from src.application.write_contract import attach_write_contract
from src.interfaces.cli.ledger_write_safety import add_write_flags as _add_local_write_flags
from src.interfaces.cli.ledger_write_safety import guard_ledger_write as _guard_write
from src.interfaces.cli.ledger_write_safety import resolve_cli_write_control as _resolve_write_control
from src.interfaces.cli.ledger_write_safety import runtime_root_arg as _runtime_root_arg


def _resolve_path_under(path: str | Path, *, base: Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = (base / resolved).resolve()
    return resolved


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"expected JSON object: {path}")
    return payload


def _lifecycle_receipt_enabled_accounts(
    intake_config: dict[str, Any],
) -> set[str]:
    fallback_receipt = (
        dict(intake_config.get("receipt") or {})
        if isinstance(intake_config.get("receipt"), dict)
        else {}
    )
    accounts: set[str] = set()
    for raw_source in intake_config.get("sources") or []:
        if not isinstance(raw_source, dict) or not bool(
            raw_source.get("enabled", True)
        ):
            continue
        source_receipt = (
            dict(raw_source.get("receipt") or {})
            if isinstance(raw_source.get("receipt"), dict)
            else fallback_receipt
        )
        if not bool(source_receipt.get("enabled", True)):
            continue
        account = str(raw_source.get("account") or "").strip().lower()
        if account:
            accounts.add(account)
        mapping = raw_source.get("account_mapping")
        if isinstance(mapping, dict):
            accounts.update(
                str(value or "").strip().lower()
                for value in mapping.values()
                if str(value or "").strip()
            )
    return accounts


def _lifecycle_dispatch_write_applied(
    result: dict[str, Any],
) -> bool:
    if isinstance(result.get("batch"), dict):
        return True
    planning = result.get("planning")
    if isinstance(planning, dict) and str(
        planning.get("status") or ""
    ) == "created":
        return True
    recovery = result.get("recovery")
    if not isinstance(recovery, dict):
        return False
    return any(
        int(recovery.get(key) or 0) > 0
        for key in (
            "reclaimed_claimed_count",
            "frozen_unknown_count",
        )
    )


def _parse_json_object_arg(raw: str | None, *, name: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{name} must be a JSON object: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise SystemExit(f"{name} must be a non-empty JSON object")
    return dict(payload)


def _store_inspect_data_config(args: argparse.Namespace, *, base: Path) -> tuple[Path, Path | None]:
    config_ref = str(getattr(args, "store_config", "") or "").strip()
    explicit_data_config = str(getattr(args, "store_data_config", "") or getattr(args, "data_config", "") or "").strip()
    if not config_ref:
        return resolve_data_config_path(base=base, data_config=(explicit_data_config or None)), None

    config_path = _resolve_path_under(config_ref, base=base)
    if explicit_data_config:
        return _resolve_path_under(explicit_data_config, base=base), config_path

    cfg = _load_json_object(config_path)
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    data_ref = str(portfolio_cfg.get("data_config") or "").strip() if isinstance(portfolio_cfg, dict) else ""
    if data_ref:
        data_path = Path(data_ref).expanduser()
        if not data_path.is_absolute():
            data_path = (config_path.parent / data_path).resolve()
        return data_path, config_path
    return (config_path.parent / "portfolio.runtime.json").resolve(), config_path


def _print_store_inspect_text(payload: dict[str, object]) -> None:
    active_raw = payload.get("active")
    summary_raw = payload.get("summary")
    active = cast(dict[str, object], active_raw) if isinstance(active_raw, dict) else {}
    summary = cast(dict[str, object], summary_raw) if isinstance(summary_raw, dict) else {}
    print("# option_positions store")
    print(f"active: {active.get('sqlite_path')}")
    print(f"runtime_root: {active.get('runtime_root')} ({active.get('runtime_root_source')})")
    print(
        "active_counts: "
        f"trade_events={active.get('trade_event_count')} "
        f"position_lots={active.get('position_lot_count')} "
        f"exists={active.get('db_exists')}"
    )
    print(
        "summary: "
        f"existing={summary.get('existing_candidate_count')} "
        f"populated={summary.get('populated_candidate_count')} "
        f"multiple_populated={summary.get('multiple_populated')}"
    )
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        print("# candidates")
        for item in candidates:
            if not isinstance(item, dict):
                continue
            item_map = cast(dict[str, object], item)
            roles_raw = item_map.get("roles")
            roles = ",".join(str(role) for role in roles_raw) if isinstance(roles_raw, list) else ""
            print(
                f"- {roles or '-'} | exists={item_map.get('exists')} "
                f"trade_events={item_map.get('trade_event_count')} "
                f"position_lots={item_map.get('position_lot_count')} | {item_map.get('path')}"
            )
    warnings = payload.get("warnings")
    if isinstance(warnings, list) and warnings:
        print("# warnings")
        for warning in warnings:
            print(f"- {warning}")


def _json_or_text_format(args: argparse.Namespace) -> str:
    return str(getattr(args, "format", "") or "text")


def _add_runtime_root_arg(
    parser: argparse.ArgumentParser,
    *,
    default: Any = argparse.SUPPRESS,
    help_text: str = "runtime root for active ledger store, e.g. /var/lib/options-monitor",
) -> None:
    parser.add_argument("--runtime-root", default=default, help=help_text)


def resolve_option_positions_repo(**kwargs: Any) -> tuple[Path, Any]:
    """Compatibility wrapper kept for tests and older call sites."""

    return open_position_ledger_from_runtime_config(**kwargs)


def _combo_reconcile_exposures(
    *,
    base: Path,
    repo: Any,
    account: str,
    runtime_environment: str,
) -> list[dict[str, Any]]:
    provisional = reconcile_combo_pair_inferences(
        repo=repo,
        account=account,
        runtime_environment=runtime_environment,
        persist=False,
    )
    scopes = {
        (str(item.get("market") or "").strip().upper(), str(item.get("market_date") or "").strip())
        for item in [
            *(provisional.get("inferences") or []),
            *(provisional.get("waiting_for_counterpart") or []),
        ]
        if str(item.get("market") or "").strip()
        and str(item.get("market_date") or "").strip()
    }
    exposures: dict[str, dict[str, Any]] = {}
    for market, market_date in sorted(scopes):
        result = read_combo_candidate_exposures(
            base=base,
            account=account,
            market=market,
            market_trading_date=market_date,
        )
        for item in result.get("exposures") or []:
            exposure_id = str(item.get("candidate_exposure_id") or "").strip()
            if exposure_id:
                exposures[exposure_id] = dict(item)
    return [exposures[key] for key in sorted(exposures)]


def _require_combo_confirmation_mode(
    *,
    base: Path,
    args: argparse.Namespace,
    inference: dict[str, Any],
) -> dict[str, str]:
    market = str(inference.get("market") or "").strip().lower()
    explicit = str(getattr(args, "config", "") or "").strip()
    if explicit:
        config_path = _resolve_path_under(explicit, base=base)
    elif market in {"us", "hk"}:
        config_path = (base / f"config.{market}.json").resolve()
    else:
        raise SystemExit(
            "confirm-combo apply cannot infer config from the inference market; pass --config"
        )
    if not config_path.exists():
        raise SystemExit(
            f"confirm-combo apply requires a runtime config with account mode=confirm: {config_path}"
        )
    config = _load_json_object(config_path)
    account = normalize_account(inference.get("account"))
    mode = combo_reconciliation_mode_for_account(config, account=account)
    if mode != "confirm":
        raise SystemExit(
            f"confirm-combo apply is disabled for account {account}: effective mode={mode}"
        )
    return {"account": account, "mode": mode, "config_path": str(config_path)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Manage position lots via trade events')
    ap.add_argument('--data-config', default=None, help='portfolio data config path; auto-resolves when omitted')
    _add_runtime_root_arg(ap, default=None)

    sub = ap.add_subparsers(dest='cmd', required=True)

    p_list = sub.add_parser('list', help='list records')
    _add_runtime_root_arg(p_list)
    p_list.add_argument('--broker', default='富途')
    p_list.add_argument('--account', default=None)
    p_list.add_argument('--status', default='open', choices=['open', 'close', 'all'])
    p_list.add_argument('--format', default='text', choices=['text', 'json'])
    p_list.add_argument('--limit', type=int, default=50)
    p_list.add_argument('--exp-within-days', type=int, default=None, help='only include rows expiring within N days from today')

    p_add = sub.add_parser('add', help='add a record')
    _add_runtime_root_arg(p_add)
    p_add.add_argument('--broker', default='富途')
    p_add.add_argument('--account', required=True)
    p_add.add_argument('--symbol', required=True)
    p_add.add_argument('--option-type', required=True, choices=['put', 'call'])
    p_add.add_argument('--side', required=True, choices=['short', 'long'])
    p_add.add_argument('--contracts', type=int, required=True)
    p_add.add_argument('--currency', default=None, choices=['USD', 'HKD', 'CNY'], help='optional; inferred from symbol when omitted (.HK => HKD, otherwise USD)')
    p_add.add_argument('--strike', type=float, required=True, help='option strike')
    p_add.add_argument('--multiplier', type=float, required=True, help='contract multiplier')
    p_add.add_argument('--exp', required=True, help='YYYY-MM-DD')
    p_add.add_argument('--premium-per-share', type=float, required=True, help='premium per share; positive, up to 3 decimals')
    p_add.add_argument('--underlying-share-locked', type=int, default=None, help='for covered call locking shares')
    p_add.add_argument('--note', default=None)
    p_add.add_argument(
        '--request-id',
        required=True,
        help='stable idempotency key reused for preview, apply, and retry',
    )
    p_add.add_argument('--format', default='text', choices=['text', 'json'])
    _add_local_write_flags(p_add, high_risk=True)

    p_buy_close = sub.add_parser('buy-close', help='buy to close a position by record_id or strict unique selector')
    _add_runtime_root_arg(p_buy_close)
    p_buy_close.add_argument('--record-id', default=None)
    p_buy_close.add_argument('--broker', default='富途')
    p_buy_close.add_argument('--account', default=None, help='required when --record-id is omitted')
    p_buy_close.add_argument('--symbol', default=None, help='required when --record-id is omitted')
    p_buy_close.add_argument('--option-type', default=None, choices=['put', 'call'], help='required when --record-id is omitted')
    p_buy_close.add_argument('--side', default='short', choices=['short', 'long'], help='target position side; buy-close normally targets short')
    p_buy_close.add_argument('--strike', type=float, default=None, help='required when --record-id is omitted')
    p_buy_close.add_argument('--exp', default=None, help='YYYY-MM-DD; required when --record-id is omitted')
    p_buy_close.add_argument('--contracts', type=int, required=True, help='contracts to close; supports partial close')
    p_buy_close.add_argument('--close-price', type=float, required=True, help='close price per share/contract unit; positive, up to 3 decimals')
    p_buy_close.add_argument('--close-reason', default='manual_buy_to_close')
    p_buy_close.add_argument('--format', default='text', choices=['text', 'json'])
    _add_local_write_flags(p_buy_close, high_risk=True)

    p_assign = sub.add_parser('assign', help='record an option assignment by record_id or strict selector')
    _add_runtime_root_arg(p_assign)
    p_assign.add_argument('--record-id', default=None)
    p_assign.add_argument('--broker', default='富途')
    p_assign.add_argument('--account', default=None, help='required when --record-id is omitted')
    p_assign.add_argument('--symbol', default=None, help='required when --record-id is omitted')
    p_assign.add_argument('--option-type', default=None, choices=['put', 'call'], help='required when --record-id is omitted')
    p_assign.add_argument('--side', default='short', choices=['short'], help='assignment currently targets short option lots')
    p_assign.add_argument('--strike', type=float, default=None, help='required when --record-id is omitted')
    p_assign.add_argument('--exp', default=None, help='YYYY-MM-DD; required when --record-id is omitted')
    p_assign.add_argument('--contracts', type=int, required=True, help='assigned option contracts')
    p_assign.add_argument('--stock-side', required=True, choices=['buy', 'sell'], help='stock settlement side; short put => buy, short call => sell')
    p_assign.add_argument('--stock-qty', type=int, required=True, help='settled stock shares')
    p_assign.add_argument('--stock-price', type=float, required=True, help='settlement stock price; should be close to strike')
    p_assign.add_argument(
        '--config',
        default=None,
        help='runtime config used to decide whether this assignment starts Wheel',
    )
    p_assign.add_argument(
        '--request-id',
        required=True,
        help='stable idempotency key reused for preview, apply, and retry',
    )
    p_assign.add_argument('--format', default='text', choices=['text', 'json'])
    _add_local_write_flags(p_assign, high_risk=True)

    p_exercise = sub.add_parser('exercise', help='record an option exercise by record_id or strict selector')
    _add_runtime_root_arg(p_exercise)
    p_exercise.add_argument('--record-id', default=None)
    p_exercise.add_argument('--broker', default='富途')
    p_exercise.add_argument('--account', default=None, help='required when --record-id is omitted')
    p_exercise.add_argument('--symbol', default=None, help='required when --record-id is omitted')
    p_exercise.add_argument('--option-type', default=None, choices=['put', 'call'], help='required when --record-id is omitted')
    p_exercise.add_argument('--side', default='long', choices=['long'], help='exercise currently targets long option lots')
    p_exercise.add_argument('--strike', type=float, default=None, help='required when --record-id is omitted')
    p_exercise.add_argument('--exp', default=None, help='YYYY-MM-DD; required when --record-id is omitted')
    p_exercise.add_argument('--contracts', type=int, required=True, help='exercised option contracts')
    p_exercise.add_argument('--stock-side', required=True, choices=['buy', 'sell'], help='stock settlement side; long call => buy, long put => sell')
    p_exercise.add_argument('--stock-qty', type=int, required=True, help='settled stock shares')
    p_exercise.add_argument('--stock-price', type=float, required=True, help='settlement stock price; should be close to strike')
    p_exercise.add_argument(
        '--request-id',
        required=True,
        help='stable idempotency key reused for preview, apply, and retry',
    )
    p_exercise.add_argument('--format', default='text', choices=['text', 'json'])
    _add_local_write_flags(p_exercise, high_risk=True)

    p_assigned_stock_sale = sub.add_parser(
        'assigned-stock-sale',
        help='record a sale of stock created by a Sell Put assignment',
    )
    _add_runtime_root_arg(p_assigned_stock_sale)
    p_assigned_stock_sale.add_argument('--target-stock-lot-id', required=True)
    p_assigned_stock_sale.add_argument('--broker', default='富途')
    p_assigned_stock_sale.add_argument('--account', default=None)
    p_assigned_stock_sale.add_argument('--symbol', default=None)
    p_assigned_stock_sale.add_argument('--currency', default=None, choices=['USD', 'HKD', 'CNY'])
    p_assigned_stock_sale.add_argument('--shares', type=int, required=True)
    p_assigned_stock_sale.add_argument('--price', type=float, required=True)
    p_assigned_stock_sale.add_argument('--trade-time-ms', type=int, required=True)
    p_assigned_stock_sale.add_argument('--source-deal-id', default=None)
    p_assigned_stock_sale.add_argument('--format', default='text', choices=['text', 'json'])
    _add_local_write_flags(p_assigned_stock_sale, high_risk=True)

    p_events = sub.add_parser('events', help='list canonical trade events')
    _add_runtime_root_arg(p_events)
    p_events.add_argument('--broker', default=None)
    p_events.add_argument('--account', default=None)
    p_events.add_argument('--format', default='text', choices=['text', 'json'])
    p_events.add_argument('--limit', type=int, default=50)

    p_history = sub.add_parser('history', help='show related trade events for a position lot')
    _add_runtime_root_arg(p_history)
    p_history.add_argument('--record-id', required=True)
    p_history.add_argument('--format', default='text', choices=['text', 'json'])

    p_rebuild = sub.add_parser('rebuild', help='rebuild position_lots projection from trade_events')
    _add_runtime_root_arg(p_rebuild)
    p_rebuild.add_argument('--format', default='text', choices=['text', 'json'])
    _add_local_write_flags(p_rebuild, high_risk=False)

    p_projection_migration = sub.add_parser(
        'projection-migration',
        help='inventory, verify, activate, or deactivate checkpoint/tail projection',
    )
    projection_migration_sub = p_projection_migration.add_subparsers(
        dest='projection_migration_cmd',
        required=True,
    )
    for command_name, help_text in (
        ('inventory', 'emit an exact read-only migration inventory'),
        ('status', 'read bounded checkpoint/head readiness status'),
    ):
        command = projection_migration_sub.add_parser(command_name, help=help_text)
        _add_runtime_root_arg(command)
        command.add_argument('--format', default='json', choices=['json'])
    p_projection_verify = projection_migration_sub.add_parser(
        'verify',
        help='run read-only full-oracle and optional runtime-shadow verification',
    )
    _add_runtime_root_arg(p_projection_verify)
    p_projection_verify.add_argument('--shadow', action='store_true')
    p_projection_verify.add_argument('--format', default='json', choices=['json'])
    p_projection_apply = projection_migration_sub.add_parser(
        'apply',
        help='apply an exact frozen inventory and seed a disabled checkpoint',
    )
    _add_runtime_root_arg(p_projection_apply)
    p_projection_apply.add_argument('--manifest', required=True)
    p_projection_apply.add_argument('--format', default='json', choices=['json'])
    _add_local_write_flags(p_projection_apply, high_risk=True)
    p_projection_activate = projection_migration_sub.add_parser(
        'activate',
        help='enable checkpoint mode from exact acceptance and shadow evidence',
    )
    _add_runtime_root_arg(p_projection_activate)
    p_projection_activate.add_argument('--acceptance-manifest', required=True)
    p_projection_activate.add_argument('--shadow-manifest', required=True)
    p_projection_activate.add_argument('--format', default='json', choices=['json'])
    _add_local_write_flags(p_projection_activate, high_risk=True)
    p_projection_deactivate = projection_migration_sub.add_parser(
        'deactivate',
        help='disable checkpoint mode without deleting projection state',
    )
    _add_runtime_root_arg(p_projection_deactivate)
    p_projection_deactivate.add_argument('--format', default='json', choices=['json'])
    _add_local_write_flags(p_projection_deactivate, high_risk=True)

    p_decision_projection = sub.add_parser(
        'decision-projection',
        help='inventory, verify, apply, or inspect the current decision projection',
    )
    decision_projection_sub = p_decision_projection.add_subparsers(
        dest='decision_projection_cmd',
        required=True,
    )
    for command_name, help_text in (
        ('inventory', 'emit an exact read-only migration inventory'),
        ('verify', 'compare the legacy oracle with proposed compact facts'),
        ('status', 'read migration readiness without changing SQLite'),
    ):
        command = decision_projection_sub.add_parser(command_name, help=help_text)
        _add_runtime_root_arg(command)
        command.add_argument('--format', default='json', choices=['json'])
    p_decision_apply = decision_projection_sub.add_parser(
        'apply',
        help='apply one exact frozen inventory and publish initial current facts',
    )
    _add_runtime_root_arg(p_decision_apply)
    p_decision_apply.add_argument('--manifest', required=True)
    p_decision_apply.add_argument('--format', default='json', choices=['json'])
    _add_local_write_flags(p_decision_apply, high_risk=True)

    p_inspect = sub.add_parser('inspect', help='inspect projected lot state and related trade events')
    _add_runtime_root_arg(p_inspect)
    p_inspect.add_argument('--record-id', default=None)
    p_inspect.add_argument('--account', default=None)
    p_inspect.add_argument('--symbol', default=None)
    p_inspect.add_argument('--option-type', default=None, choices=['put', 'call'])
    p_inspect.add_argument('--strike', type=float, default=None)
    p_inspect.add_argument('--exp', default=None, help='YYYY-MM-DD')
    p_inspect.add_argument('--format', default='json', choices=['json'])

    p_lifecycle = sub.add_parser('lifecycle', help='inspect option lifecycle cases and evidence')
    lifecycle_sub = p_lifecycle.add_subparsers(dest='lifecycle_cmd', required=True)
    p_lifecycle_list = lifecycle_sub.add_parser('list', help='list pending/reviewed assignment/expiry lifecycle cases')
    _add_runtime_root_arg(p_lifecycle_list)
    p_lifecycle_list.add_argument('--status', default=None)
    p_lifecycle_list.add_argument('--account', default=None)
    p_lifecycle_list.add_argument('--symbol', default=None)
    p_lifecycle_list.add_argument('--include-evidence', action='store_true')
    p_lifecycle_list.add_argument('--format', default='json', choices=['json', 'text'])
    p_lifecycle_inspect = lifecycle_sub.add_parser('inspect', help='inspect one lifecycle case with evidence')
    _add_runtime_root_arg(p_lifecycle_inspect)
    p_lifecycle_inspect.add_argument('--case-id', required=True)
    p_lifecycle_inspect.add_argument('--format', default='json', choices=['json', 'text'])
    p_lifecycle_reconcile = lifecycle_sub.add_parser(
        'reconcile',
        help='discover expired-open cases and optionally reconcile one canonical evidence payload',
    )
    _add_runtime_root_arg(p_lifecycle_reconcile)
    p_lifecycle_reconcile.add_argument('--account', default=None)
    p_lifecycle_reconcile.add_argument('--case-id', default=None)
    p_lifecycle_reconcile.add_argument('--target-lot-id', default=None)
    p_lifecycle_reconcile.add_argument(
        '--evidence-json',
        default=None,
        help='path to one canonical lifecycle evidence JSON object',
    )
    p_lifecycle_reconcile.add_argument('--observed-at-ms', type=int, default=None)
    p_lifecycle_reconcile.add_argument('--config', default=None)
    p_lifecycle_reconcile.add_argument('--format', default='json', choices=['json', 'text'])
    _add_local_write_flags(p_lifecycle_reconcile, high_risk=True)
    p_lifecycle_confirm_expired = lifecycle_sub.add_parser(
        'confirm-expired',
        help='confirm a pending zero-price option lifecycle case expired without assignment/exercise',
    )
    p_lifecycle_confirm_expired.add_argument('--case-id', default=None)
    p_lifecycle_confirm_expired.add_argument('--deal-id', default=None)
    _add_runtime_root_arg(p_lifecycle_confirm_expired)
    p_lifecycle_confirm_expired.add_argument('--format', default='json', choices=['json', 'text'])
    _add_local_write_flags(p_lifecycle_confirm_expired, high_risk=True)
    p_lifecycle_due = lifecycle_sub.add_parser(
        'reconcile-due',
        help=(
            'preview due lifecycle cases locally without provider I/O; '
            'only --apply --confirm uses providers and writes'
        ),
        description=(
            'Default/--dry-run is a local plan: it does not require ready '
            'broker/quote routes and does not construct or query provider '
            'gateways. Only --apply --confirm (or --apply --yes) uses '
            'providers and writes.'
        ),
    )
    _add_runtime_root_arg(p_lifecycle_due)
    p_lifecycle_due.add_argument('--account', required=True)
    p_lifecycle_due.add_argument(
        '--config',
        required=True,
        help='runtime config used to bind the account to one OpenD source',
    )
    p_lifecycle_due.add_argument('--observed-at-ms', type=int, default=None)
    p_lifecycle_due.add_argument('--format', default='json', choices=['json', 'text'])
    _add_local_write_flags(p_lifecycle_due, high_risk=True)
    for command_name, help_text in (
        (
            'resolve',
            'resolve one lifecycle case with persisted broker evidence',
        ),
        (
            'correct',
            'atomically void and replace one lifecycle terminal result',
        ),
    ):
        command = lifecycle_sub.add_parser(
            command_name,
            help=help_text,
        )
        _add_runtime_root_arg(command)
        command.add_argument('--case-id', required=True)
        command.add_argument(
            '--expected-revision',
            required=True,
            type=int,
        )
        command.add_argument(
            '--reason',
            required=True,
            choices=[
                'assignment',
                'exercise',
                'expiration-no-settlement',
                'trade-close',
            ],
        )
        command.add_argument('--broker-ref', default=None)
        command.add_argument('--note', default=None)
        command.add_argument('--config', default=None)
        if command_name == 'correct':
            command.add_argument(
                '--void-terminal-event-id',
                required=True,
            )
        command.add_argument(
            '--observed-at-ms',
            type=int,
            default=None,
        )
        command.add_argument(
            '--format',
            default='json',
            choices=['json'],
        )
        _add_local_write_flags(command, high_risk=True)
    p_lifecycle_receipts = lifecycle_sub.add_parser(
        'receipts',
        help='inspect, dispatch, or reconcile lifecycle notification Outbox rows',
    )
    receipt_sub = p_lifecycle_receipts.add_subparsers(
        dest='receipt_cmd',
        required=True,
    )
    p_receipt_inspect = receipt_sub.add_parser(
        'inspect',
        help='inspect one Outbox row or delivery batch',
    )
    _add_runtime_root_arg(p_receipt_inspect)
    inspect_identity = p_receipt_inspect.add_mutually_exclusive_group(
        required=True
    )
    inspect_identity.add_argument('--outbox-id')
    inspect_identity.add_argument('--batch-id')
    p_receipt_inspect.add_argument('--format', default='json', choices=['json'])
    p_receipt_reconcile = receipt_sub.add_parser(
        'reconcile',
        help='reconcile accepted/unknown delivery or create an explicit resend',
    )
    _add_runtime_root_arg(p_receipt_reconcile)
    reconcile_identity = p_receipt_reconcile.add_mutually_exclusive_group(
        required=True
    )
    reconcile_identity.add_argument('--outbox-id')
    reconcile_identity.add_argument('--batch-id')
    p_receipt_reconcile.add_argument(
        '--mark',
        '--action',
        dest='action',
        required=True,
        choices=['confirmed', 'unknown', 'resend'],
    )
    p_receipt_reconcile.add_argument('--broker-ref', required=True)
    p_receipt_reconcile.add_argument('--note', required=True)
    p_receipt_reconcile.add_argument('--format', default='json', choices=['json'])
    _add_local_write_flags(p_receipt_reconcile, high_risk=True)
    p_receipt_dispatch = receipt_sub.add_parser(
        'dispatch',
        help='dispatch at most one due lifecycle delivery batch',
    )
    _add_runtime_root_arg(p_receipt_dispatch)
    p_receipt_dispatch.add_argument('--once', action='store_true', required=True)
    p_receipt_dispatch.add_argument(
        '--account',
        default=None,
        help='dry-run observation filter only; applied dispatch is global',
    )
    p_receipt_dispatch.add_argument('--config', required=True)
    p_receipt_dispatch.add_argument('--format', default='json', choices=['json'])
    _add_local_write_flags(p_receipt_dispatch, high_risk=True)
    p_lifecycle_migration = lifecycle_sub.add_parser(
        'migration',
        help='inventory and apply explicit lifecycle cutover manifests',
    )
    migration_sub = p_lifecycle_migration.add_subparsers(
        dest='migration_cmd',
        required=True,
    )
    p_migration_inventory = migration_sub.add_parser(
        'inventory',
        help='build a read-only explicit migration manifest',
    )
    _add_runtime_root_arg(p_migration_inventory)
    p_migration_inventory.add_argument(
        '--select-target',
        action='append',
        default=[],
    )
    p_migration_inventory.add_argument(
        '--mapping-manifest',
        default=None,
        help=(
            'operator-curated lifecycle_explicit_mapping.v1 '
            'used to resolve legacy rows'
        ),
    )
    p_migration_inventory.add_argument(
        '--format',
        default='json',
        choices=['json'],
    )
    p_migration_apply = migration_sub.add_parser(
        'apply',
        help='apply selected exact rows from a frozen manifest',
    )
    _add_runtime_root_arg(p_migration_apply)
    p_migration_apply.add_argument('--manifest', required=True)
    p_migration_apply.add_argument(
        '--format',
        default='json',
        choices=['json'],
    )
    _add_local_write_flags(p_migration_apply, high_risk=True)

    p_store = sub.add_parser('store', help='inspect option-position SQLite store resolution')
    store_sub = p_store.add_subparsers(dest='store_cmd', required=True)
    p_store_inspect = store_sub.add_parser('inspect', help='diagnose active SQLite store candidates')
    p_store_inspect.add_argument("--config", dest="store_config", default=None, help="runtime config path; resolves portfolio.data_config relative to the config file")
    p_store_inspect.add_argument("--data-config", dest="store_data_config", default=None, help="portfolio data config path override")
    _add_runtime_root_arg(p_store_inspect, help_text="override runtime root for standard ledger path resolution")
    p_store_inspect.add_argument("--format", default="json", choices=["json", "text"])

    p_verify = sub.add_parser('verify-projection', help='verify position_lots by replaying trade_events')
    _add_runtime_root_arg(p_verify)
    p_verify.add_argument('--mode', default='auto', choices=['auto', 'full'], help='auto may reuse a trusted checkpoint when events and lots are unchanged')
    p_verify.add_argument(
        '--publish-evidence',
        action='store_true',
        help='persist the verification report and a successful replay checkpoint; omitted by default for a pure read-only verification',
    )
    p_verify.add_argument('--format', default='text', choices=['text', 'json'])

    p_void_event = sub.add_parser('void-event', help='append a void event for a canonical trade event')
    _add_runtime_root_arg(p_void_event)
    p_void_event.add_argument('--event-id', required=True)
    p_void_event.add_argument('--void-reason', default='manual_void')
    p_void_event.add_argument('--format', default='text', choices=['text', 'json'])
    _add_local_write_flags(p_void_event, high_risk=True)

    p_adjust = sub.add_parser('adjust-lot', help='append an adjustment event for an existing position lot')
    _add_runtime_root_arg(p_adjust)
    p_adjust.add_argument('--record-id', required=True)
    p_adjust.add_argument('--contracts', type=int, default=None)
    p_adjust.add_argument('--strike', type=float, default=None)
    p_adjust.add_argument('--exp', default=None, help='YYYY-MM-DD')
    p_adjust.add_argument('--premium-per-share', type=float, default=None)
    p_adjust.add_argument('--multiplier', type=float, default=None)
    p_adjust.add_argument('--opened-at-ms', type=int, default=None)
    p_adjust.add_argument('--strategy', default=None, help='strategy marker, e.g. combo_yield')
    p_adjust.add_argument('--leg-role', default=None, help='strategy leg role, e.g. enhancement_call')
    p_adjust.add_argument('--strategy-group-id', default=None)
    p_adjust.add_argument('--yield-enhancement-mode', default=None)
    p_adjust.add_argument('--strategy-snapshot-json', default=None, help='JSON object stored as strategy_snapshot')
    p_adjust.add_argument('--format', default='text', choices=['text', 'json'])
    _add_local_write_flags(p_adjust, high_risk=True)

    p_adopt_combo_identity = sub.add_parser(
        "adopt-combo-identity",
        help=("insert immutable identity for two exact existing Combo Yield legs"),
    )
    _add_runtime_root_arg(p_adopt_combo_identity)
    p_adopt_combo_identity.add_argument(
        "--strategy-group-id",
        required=True,
    )
    p_adopt_combo_identity.add_argument(
        "--funding-put-record-id",
        required=True,
    )
    p_adopt_combo_identity.add_argument(
        "--funding-put-open-event-id",
        required=True,
    )
    p_adopt_combo_identity.add_argument(
        "--participation-call-record-id",
        required=True,
    )
    p_adopt_combo_identity.add_argument(
        "--participation-call-open-event-id",
        required=True,
    )
    p_adopt_combo_identity.add_argument(
        "--expected-contracts",
        type=int,
        required=True,
    )
    p_adopt_combo_identity.add_argument(
        "--format",
        default="text",
        choices=["text", "json"],
    )
    _add_local_write_flags(
        p_adopt_combo_identity,
        high_risk=True,
    )

    p_combo_reconcile = sub.add_parser(
        "combo-reconcile",
        help="derive post-trade Combo proposals without changing canonical memberships",
    )
    _add_runtime_root_arg(p_combo_reconcile)
    p_combo_reconcile.add_argument("--account", required=True)
    p_combo_reconcile.add_argument("--runtime-environment", default="runtime")
    p_combo_reconcile.add_argument("--dry-run", action="store_true", default=True)
    p_combo_reconcile.add_argument("--format", default="json", choices=["text", "json"])

    p_combo_inferences = sub.add_parser(
        "combo-inferences",
        help="list post-trade Combo inference state",
    )
    _add_runtime_root_arg(p_combo_inferences)
    p_combo_inferences.add_argument("--account", required=True)
    p_combo_inferences.add_argument(
        "--status",
        default=None,
        choices=[
            "proposal_ready",
            "ambiguous",
            "user_confirmed",
            "user_rejected",
            "expired_unresolved",
            "superseded",
        ],
    )
    p_combo_inferences.add_argument("--format", default="json", choices=["text", "json"])

    for command, help_text in (
        ("confirm-combo", "confirm and atomically adopt one exact Combo inference"),
        ("supersede-combo", "atomically void both adoptions for a confirmed Combo inference"),
    ):
        parser = sub.add_parser(command, help=help_text)
        _add_runtime_root_arg(parser)
        parser.add_argument("--inference-id", required=True)
        parser.add_argument("--expected-input-hash", required=True)
        parser.add_argument("--actor", required=True)
        if command == "confirm-combo":
            parser.add_argument(
                "--config",
                default=None,
                help="runtime config used to verify this account has combo reconciliation mode=confirm",
            )
        if command == "supersede-combo":
            parser.add_argument("--reason", required=True)
        parser.add_argument("--format", default="json", choices=["text", "json"])
        _add_local_write_flags(parser, high_risk=True)

    p_reject_combo = sub.add_parser(
        "reject-combo",
        help="reject one exact pending Combo inference without changing ledger facts",
    )
    _add_runtime_root_arg(p_reject_combo)
    p_reject_combo.add_argument("--inference-id", required=True)
    p_reject_combo.add_argument("--expected-input-hash", required=True)
    p_reject_combo.add_argument("--reason", required=True)
    p_reject_combo.add_argument("--actor", required=True)
    p_reject_combo.add_argument("--format", default="json", choices=["text", "json"])
    _add_local_write_flags(p_reject_combo, high_risk=True)

    p_auto_close = sub.add_parser('auto-close-expired', help='auto-close expired option position lots')
    p_auto_close.add_argument("--config", dest="auto_close_config", default=None, help="runtime config path; provides accounts and portfolio.data_config")
    p_auto_close.add_argument("--data-config", dest="auto_close_data_config", default=None, help="portfolio data config path; overrides runtime config when provided")
    p_auto_close.add_argument("--accounts", nargs="+", default=None, help="accounts to process; defaults to runtime config accounts")
    p_auto_close.add_argument("--broker", default=None, help="optional broker filter override")
    p_auto_close.add_argument("--apply", action="store_true", help="append close events for expired lots")
    p_auto_close.add_argument("--confirm", action="store_true", help="confirm high-risk close-event writes and receipts")
    p_auto_close.add_argument("--yes", action="store_true", help="non-interactive confirmation; emits an audit_id")
    p_auto_close.add_argument("--dry-run", action="store_true", help="preview without writing close events")
    p_auto_close.add_argument("--as-of-utc", default=None, help="ISO datetime; default is current UTC")
    p_auto_close.add_argument("--no-send", action="store_true", help="do not send auto-close receipt notifications")
    p_auto_close.add_argument("--format", choices=["json", "text"], default="json")
    p_auto_close.add_argument("--quiet", action="store_true", help="suppress stdout")
    p_auto_close.add_argument(
        "--runtime-root",
        default=argparse.SUPPRESS,
        help="runtime root for state, audit, output, and active ledger store",
    )

    args = ap.parse_args(argv)

    base = Path(__file__).resolve().parents[3]
    if args.cmd == 'store':
        data_config_path, config_path = _store_inspect_data_config(args, base=base)
        payload = inspect_ledger_stores(
            data_config_path,
            runtime_root=getattr(args, "runtime_root", None),
            config_path=config_path,
        )
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_store_inspect_text(payload)
        return 0

    if args.cmd == 'auto-close-expired':
        auto_close_argv: list[str] = []
        if args.auto_close_config:
            auto_close_argv.extend(["--config", str(args.auto_close_config)])
        if args.auto_close_data_config:
            auto_close_argv.extend(["--data-config", str(args.auto_close_data_config)])
        elif args.data_config:
            auto_close_argv.extend(["--data-config", str(args.data_config)])
        if args.runtime_root:
            auto_close_argv.extend(["--runtime-root", str(args.runtime_root)])
        if args.accounts:
            auto_close_argv.append("--accounts")
            auto_close_argv.extend(str(item) for item in args.accounts)
        if args.broker:
            auto_close_argv.extend(["--broker", str(args.broker)])
        if args.apply:
            auto_close_argv.append("--apply")
        if args.confirm:
            auto_close_argv.append("--confirm")
        if args.yes:
            auto_close_argv.append("--yes")
        if args.dry_run:
            auto_close_argv.append("--dry-run")
        if args.as_of_utc:
            auto_close_argv.extend(["--as-of-utc", str(args.as_of_utc)])
        if args.no_send:
            auto_close_argv.append("--no-send")
        if args.format:
            auto_close_argv.extend(["--format", str(args.format)])
        if args.quiet:
            auto_close_argv.append("--quiet")
        return int(run_option_positions_auto_close(auto_close_argv))

    write_controls: dict[str, dict[str, bool]] = {}
    write_control_key = str(args.cmd)
    if args.cmd == "decision-projection" and getattr(
        args, "decision_projection_cmd", None
    ) == "apply":
        write_control_key = "decision-projection:apply"
        if (
            (bool(getattr(args, "confirm", False)) or bool(getattr(args, "yes", False)))
            and not bool(getattr(args, "apply", False))
        ):
            raise SystemExit(
                "option-positions decision-projection apply requires --apply "
                "together with --confirm or --yes"
            )
        write_controls[write_control_key] = _resolve_write_control(
            args,
            command_name="option-positions decision-projection apply",
            high_risk=True,
        )
        if not write_controls[write_control_key]["write_requested"]:
            raise SystemExit(
                "option-positions decision-projection apply requires --apply "
                "and --confirm or --yes"
            )
    elif args.cmd == "projection-migration" and getattr(
        args, "projection_migration_cmd", None
    ) in {"apply", "activate", "deactivate"}:
        migration_command = str(args.projection_migration_cmd)
        write_control_key = f"projection-migration:{migration_command}"
        if (
            (bool(getattr(args, "confirm", False)) or bool(getattr(args, "yes", False)))
            and not bool(getattr(args, "apply", False))
        ):
            raise SystemExit(
                f"option-positions projection-migration {migration_command} "
                "requires --apply together with --confirm or --yes"
            )
        write_controls[write_control_key] = _resolve_write_control(
            args,
            command_name=f"option-positions projection-migration {migration_command}",
            high_risk=True,
        )
        if not write_controls[write_control_key]["write_requested"]:
            raise SystemExit(
                f"option-positions projection-migration {migration_command} "
                "requires --apply and --confirm or --yes"
            )
    elif args.cmd == "lifecycle" and (
        getattr(args, "lifecycle_cmd", None)
        in {
            "confirm-expired",
            "reconcile",
            "reconcile-due",
            "resolve",
            "correct",
        }
        or (
            getattr(args, "lifecycle_cmd", None) == "receipts"
            and getattr(args, "receipt_cmd", None)
            in {"reconcile", "dispatch"}
        )
        or (
            getattr(args, "lifecycle_cmd", None) == "migration"
            and getattr(args, "migration_cmd", None) == "apply"
        )
    ):
        lifecycle_command = str(args.lifecycle_cmd)
        if lifecycle_command == "receipts":
            lifecycle_command = (
                f"receipts:{str(args.receipt_cmd)}"
            )
        if lifecycle_command == "migration":
            lifecycle_command = (
                f"migration:{str(args.migration_cmd)}"
            )
        write_control_key = f"lifecycle:{lifecycle_command}"
        if (
            lifecycle_command != "confirm-expired"
            and (
                bool(getattr(args, "confirm", False))
                or bool(getattr(args, "yes", False))
            )
            and not bool(getattr(args, "apply", False))
        ):
            raise SystemExit(
                "option-positions lifecycle "
                + lifecycle_command.replace(":", " ")
                + " requires --apply together with --confirm or --yes"
            )
        write_controls[write_control_key] = _resolve_write_control(
            args,
            command_name=(
                "option-positions lifecycle "
                + lifecycle_command.replace(":", " ")
            ),
            high_risk=True,
        )
    elif args.cmd in {
        "add",
        "buy-close",
        "assign",
        "exercise",
        "assigned-stock-sale",
        "void-event",
        "adjust-lot",
        "adopt-combo-identity",
        "confirm-combo",
        "reject-combo",
        "supersede-combo",
    }:
        write_controls[args.cmd] = _resolve_write_control(args, command_name=f"option-positions {args.cmd}", high_risk=True)
    elif args.cmd == "rebuild":
        write_controls[args.cmd] = _resolve_write_control(args, command_name="option-positions rebuild", high_risk=False)
    write_cmd = bool(write_controls.get(write_control_key, {}).get("write_requested", False))
    data_config_path = resolve_position_data_config_path(base=base, data_config=args.data_config)
    if write_cmd:
        guard = _guard_write(
            data_config=data_config_path,
            args=args,
            as_json=(str(getattr(args, "format", "") or "") == "json"),
        )
        if guard is None:
            return 2

    if args.cmd == "decision-projection":
        store = resolve_ledger_store(
            data_config_path,
            runtime_root=_runtime_root_arg(args),
        )
        sqlite_path = store.sqlite_path
        command = str(args.decision_projection_cmd)
        if command == "inventory":
            payload = build_current_decision_projection_migration_inventory(
                sqlite_path
            )
        elif command == "verify":
            payload = verify_current_decision_projection_migration(sqlite_path)
        elif command == "status":
            payload = current_decision_projection_migration_status(sqlite_path)
        elif command == "apply":
            payload = apply_current_decision_projection_migration(
                sqlite_path,
                _load_json_object(_resolve_path_under(args.manifest, base=base)),
            )
        else:  # pragma: no cover - argparse owns the command set
            raise SystemExit(f"unsupported decision projection command: {command}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "projection-migration":
        store = resolve_ledger_store(
            data_config_path,
            runtime_root=_runtime_root_arg(args),
        )
        sqlite_path = store.sqlite_path
        command = str(args.projection_migration_cmd)
        if command == "inventory":
            payload = build_position_projection_migration_inventory(sqlite_path)
        elif command == "status":
            payload = position_projection_migration_status(sqlite_path)
        elif command == "verify":
            payload = verify_position_projection_migration(
                sqlite_path,
                shadow=bool(args.shadow),
            )
        elif command == "apply":
            payload = apply_position_projection_migration(
                sqlite_path,
                _load_json_object(_resolve_path_under(args.manifest, base=base)),
            )
        elif command == "activate":
            payload = activate_position_projection_checkpoints(
                sqlite_path,
                acceptance_manifest=_load_json_object(
                    _resolve_path_under(args.acceptance_manifest, base=base)
                ),
                shadow_manifest=_load_json_object(
                    _resolve_path_under(args.shadow_manifest, base=base)
                ),
            )
        elif command == "deactivate":
            payload = deactivate_position_projection_checkpoints(sqlite_path)
        else:  # pragma: no cover - argparse owns the command set
            raise SystemExit(f"unsupported projection migration command: {command}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    _data_config, repo = resolve_option_positions_repo(
        base=base,
        cfg=None,
        data_config=args.data_config,
        runtime_root=_runtime_root_arg(args),
    )
    state_base = Path(str(_data_config)).resolve().parent
    ledger_store = ledger_store_payload(_data_config, repo)

    if args.cmd == 'list':
        broker = normalize_broker(args.broker)
        account = normalize_account(args.account) if args.account else None
        rows = list_position_rows(
            repo,
            broker=broker,
            account=account,
            status=args.status,
            limit=args.limit,
            expiration_within_days=args.exp_within_days,
        )
        if args.format == 'json':
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0

        if not rows:
            print('(no records)')
            return 0
        print('# position_lots')
        for r in rows:
            ccy = str(r.get('currency') or 'USD').upper()
            cash_txt = format_position_cash_secured(r.get('cash_secured_amount'), ccy)
            print(
                f"- {r['record_id']} | {r.get('account')} | {r.get('symbol')} | {r.get('side')} {r.get('option_type')} | "
                f"exp {r.get('expiration_ymd') or '-'} | strike {r.get('strike') if r.get('strike') is not None else '-'} | "
                f"contracts {r.get('contracts')} open {r.get('contracts_open')} closed {r.get('contracts_closed')} | "
                f"{ccy} cash_secured {cash_txt} | status {r.get('status')}"
            )
        return 0

    if args.cmd == 'add':
        broker = normalize_broker(args.broker)
        control = write_controls["add"]
        dry_run = not bool(control["write_requested"])
        try:
            out = execute_manual_open(
                repo,
                broker=broker,
                account=args.account,
                symbol=args.symbol,
                option_type=args.option_type,
                side=args.side,
                contracts=int(args.contracts),
                currency=args.currency,
                strike=args.strike,
                multiplier=args.multiplier,
                expiration_ymd=((args.exp or '').strip() or None),
                premium_per_share=args.premium_per_share,
                underlying_share_locked=args.underlying_share_locked,
                note=args.note,
                dry_run=dry_run,
                request_id=args.request_id,
            )
        except ValueError as e:
            raise SystemExit(str(e))

        payload = attach_write_contract(
            {"operation": "manual_open", **out, "ledger_store": ledger_store},
            dry_run=dry_run,
            write_applied=not dry_run,
            rollback_hint="void the created open trade event with option-positions void-event --confirm",
        )
        if _json_or_text_format(args) == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 0

        fields = out["fields"]
        if dry_run:
            print('[DRY_RUN] create fields:')
            print(json.dumps(fields, ensure_ascii=False, indent=2))
            return 0

        res = out["result"]
        print(f"[DONE] created event_id={res.get('event_id')}")
        if fields.get('cash_secured_amount') is not None:
            print(
                f"cash_secured_amount={format_position_money(float(fields['cash_secured_amount']), fields.get('currency') or '')}"
            )
        return 0

    if args.cmd == 'buy-close':
        control = write_controls["buy-close"]
        dry_run = not bool(control["write_requested"])
        try:
            out = execute_manual_close(
                repo,
                record_id=args.record_id,
                contracts_to_close=int(args.contracts),
                close_price=args.close_price,
                close_reason=args.close_reason,
                dry_run=dry_run,
                broker=args.broker,
                account=args.account,
                symbol=args.symbol,
                option_type=args.option_type,
                position_side=args.side,
                strike=args.strike,
                expiration_ymd=((args.exp or '').strip() or None),
            )
        except ManualCloseMatchError as e:
            raise SystemExit(format_manual_close_match_error(e))
        except ValueError as e:
            raise SystemExit(str(e))
        raw_match = out.get("match")
        match: dict[str, Any] = raw_match if isinstance(raw_match, dict) else {}
        if match.get("rule") == "strict_contract_unique":
            print(f"[MATCH] rule={match.get('rule')} record_id={match.get('record_id')}")
        payload = attach_write_contract(
            {"operation": "manual_close", **out, "ledger_store": ledger_store},
            dry_run=dry_run,
            write_applied=not dry_run,
            rollback_hint="void the created close trade event with option-positions void-event --confirm",
        )
        if _json_or_text_format(args) == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 0
        patch = out["patch"]
        if dry_run:
            print('[DRY_RUN] update fields:')
            print(json.dumps(patch, ensure_ascii=False, indent=2))
            return 0
        res = out["result"]
        closed_record_id = (match.get("record_id") if match else None) or args.record_id
        print(f"[DONE] buy-closed {closed_record_id} contracts={int(args.contracts)} event_id={res.get('event_id')}")
        return 0

    if args.cmd == 'assign':
        control = write_controls["assign"]
        dry_run = not bool(control["write_requested"])
        runtime_config = (
            _load_json_object(_resolve_path_under(args.config, base=base))
            if args.config
            else None
        )
        try:
            out = execute_manual_assignment(
                repo,
                record_id=args.record_id,
                broker=args.broker,
                account=args.account,
                symbol=args.symbol,
                option_type=args.option_type,
                position_side=args.side,
                strike=args.strike,
                expiration_ymd=((args.exp or '').strip() or None),
                contracts_to_close=int(args.contracts),
                stock_side=args.stock_side,
                stock_qty=int(args.stock_qty),
                stock_price=float(args.stock_price),
                dry_run=dry_run,
                request_id=args.request_id,
                runtime_config=runtime_config,
            )
        except ValueError as e:
            raise SystemExit(str(e))
        payload = attach_write_contract(
            {"operation": "manual_assignment", **out, "ledger_store": ledger_store},
            dry_run=dry_run,
            write_applied=not dry_run,
            rollback_hint="void the created assignment trade event(s) with option-positions void-event --confirm",
        )
        if _json_or_text_format(args) == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 0
        if dry_run:
            print("[DRY_RUN] assignment target:")
            print(json.dumps(out.get("close_target_resolution") or {}, ensure_ascii=False, indent=2))
            print("[DRY_RUN] stock settlement:")
            print(json.dumps(out.get("stock_settlement") or {}, ensure_ascii=False, indent=2))
            return 0
        operations = out.get("operations") if isinstance(out.get("operations"), list) else []
        event_ids = [
            str((item.get("result") or {}).get("event_id"))
            for item in operations
            if isinstance(item, dict) and isinstance(item.get("result"), dict) and (item.get("result") or {}).get("event_id")
        ]
        print(f"[DONE] assignment contracts={int(args.contracts)} events={len(event_ids)} event_ids={','.join(event_ids)}")
        return 0

    if args.cmd == 'exercise':
        control = write_controls["exercise"]
        dry_run = not bool(control["write_requested"])
        try:
            out = execute_manual_exercise(
                repo,
                record_id=args.record_id,
                broker=args.broker,
                account=args.account,
                symbol=args.symbol,
                option_type=args.option_type,
                position_side=args.side,
                strike=args.strike,
                expiration_ymd=((args.exp or '').strip() or None),
                contracts_to_close=int(args.contracts),
                stock_side=args.stock_side,
                stock_qty=int(args.stock_qty),
                stock_price=float(args.stock_price),
                dry_run=dry_run,
                request_id=args.request_id,
            )
        except ValueError as e:
            raise SystemExit(str(e))
        payload = attach_write_contract(
            {"operation": "manual_exercise", **out, "ledger_store": ledger_store},
            dry_run=dry_run,
            write_applied=not dry_run,
            rollback_hint="void the created exercise trade event(s) with option-positions void-event --confirm",
        )
        if _json_or_text_format(args) == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 0
        if dry_run:
            print("[DRY_RUN] exercise target:")
            print(json.dumps(out.get("close_target_resolution") or {}, ensure_ascii=False, indent=2))
            print("[DRY_RUN] stock settlement:")
            print(json.dumps(out.get("stock_settlement") or {}, ensure_ascii=False, indent=2))
            return 0
        operations = out.get("operations") if isinstance(out.get("operations"), list) else []
        event_ids = [
            str((item.get("result") or {}).get("event_id"))
            for item in operations
            if isinstance(item, dict) and isinstance(item.get("result"), dict) and (item.get("result") or {}).get("event_id")
        ]
        print(f"[DONE] exercise contracts={int(args.contracts)} events={len(event_ids)} event_ids={','.join(event_ids)}")
        return 0

    if args.cmd == 'assigned-stock-sale':
        control = write_controls["assigned-stock-sale"]
        dry_run = not bool(control["write_requested"])
        try:
            out = execute_manual_assigned_stock_sale(
                repo,
                target_stock_lot_id=args.target_stock_lot_id,
                shares=int(args.shares),
                price=float(args.price),
                trade_time_ms=int(args.trade_time_ms),
                account=args.account,
                broker=args.broker,
                symbol=args.symbol,
                currency=args.currency,
                source_deal_id=args.source_deal_id,
                dry_run=dry_run,
            )
        except ValueError as e:
            raise SystemExit(str(e))
        payload = attach_write_contract(
            {"operation": "manual_assigned_stock_sale", **out, "ledger_store": ledger_store},
            dry_run=dry_run,
            write_applied=not dry_run,
            rollback_hint="record a future assigned stock repair/void event; do not edit assigned_stock_lots directly",
        )
        if _json_or_text_format(args) == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 0
        if dry_run:
            print("[DRY_RUN] assigned stock sale:")
            print(json.dumps(out.get("sale_event") or {}, ensure_ascii=False, indent=2))
            return 0
        result = out.get("result") if isinstance(out.get("result"), dict) else {}
        print(
            "[DONE] assigned-stock-sale "
            f"stock_event_id={result.get('stock_event_id')} "
            f"created={result.get('created')}"
        )
        return 0

    if args.cmd == 'events':
        broker = normalize_broker(args.broker) if args.broker else None
        account = normalize_account(args.account) if args.account else None
        events = repo.list_trade_events()
        rows: list[dict[str, object]] = []
        for event in reversed(events):
            event_broker = normalize_broker(event.get('broker'))
            event_account = normalize_account(event.get('account')) if event.get('account') else None
            if broker and event_broker != broker:
                continue
            if account and event_account != account:
                continue
            rows.append(
                add_trade_time_beijing({
                    'event_id': event.get('event_id'),
                    'trade_time_ms': event.get('trade_time_ms'),
                    'source_type': event.get('source_type'),
                    'source_name': event.get('source_name'),
                    'broker': event_broker,
                    'account': event_account,
                    'symbol': event.get('symbol'),
                    'option_type': event.get('option_type'),
                    'side': event.get('side'),
                    'position_effect': event.get('position_effect'),
                    'contracts': event.get('contracts'),
                    'price': event.get('price'),
                    'strike': event.get('strike'),
                    'expiration_ymd': event.get('expiration_ymd'),
                    'currency': event.get('currency'),
                })
            )
            if len(rows) >= max(args.limit, 1):
                break
        if args.format == 'json':
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print('(no events)')
            return 0
        print('# trade_events')
        for row in rows:
            print(
                f"- {row.get('event_id')} | {row.get('account')} | {row.get('symbol')} | "
                f"{row.get('side')} {row.get('option_type')} {row.get('position_effect')} | "
                f"contracts {row.get('contracts')} | time {row.get('trade_time_beijing') or '-'} | "
                f"source {row.get('source_type')}:{row.get('source_name')}"
            )
        return 0

    if args.cmd == 'history':
        try:
            history = build_lot_event_history(repo, base=state_base, record_id=args.record_id)
        except ValueError as e:
            raise SystemExit(str(e))
        if args.format == 'json':
            print(json.dumps(history, ensure_ascii=False, indent=2))
            return 0
        if not history:
            print('(no related events)')
            return 0
        print(f'# lot_history {args.record_id}')
        for row in history:
            extra = []
            if row.get('void_target_event_id'):
                extra.append(f"void_target={row.get('void_target_event_id')}")
            if row.get('adjust_target_source_event_id'):
                extra.append(f"adjust_target={row.get('adjust_target_source_event_id')}")
            if row.get('close_target_source_event_id'):
                extra.append(f"close_target_src={row.get('close_target_source_event_id')}")
            print(
                f"- {row.get('event_id')} | {row.get('side')} {row.get('option_type')} {row.get('position_effect')} | "
                f"contracts {row.get('contracts')} | time {row.get('trade_time_beijing') or '-'} | "
                f"source {row.get('source_type')}:{row.get('source_name')}"
                + (f" | {' '.join(extra)}" if extra else "")
            )
        return 0

    if args.cmd == 'rebuild':
        control = write_controls["rebuild"]
        should_apply = bool(control["write_requested"])
        if should_apply:
            raw_result = refresh_position_lot_projection(repo)
            result = dict(raw_result) if isinstance(raw_result, dict) else raw_result.to_dict()
            result["mode"] = "canonical_position_lots_rebuild"
        else:
            result = replay_trade_events(repo, apply=False)
            result["mode"] = "canonical_position_lots_rebuild_dry_run"
        result["source_of_truth"] = "trade_events"
        result["projection"] = "position_lots"
        result["ledger_store"] = ledger_store
        result = attach_write_contract(
            result,
            dry_run=not should_apply,
            write_applied=should_apply,
            rollback_hint="rerun option-positions rebuild from canonical trade_events",
        )
        if args.format == 'json':
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        prefix = "[DONE]" if should_apply else "[DRY_RUN]"
        verb = "rebuilt" if should_apply else "would rebuild"
        print(
            f"{prefix} {verb} canonical position_lots projection "
            f"trade_events={result.get('trade_event_count')} "
            f"position_lots={result.get('position_lot_count')} "
            f"diagnostics={result.get('projection_diagnostic_count')} "
            f"unmatched_explicit_close={result.get('unmatched_explicit_close_count')} "
            f"unmatched_heuristic_close={result.get('unmatched_heuristic_close_count')}"
        )
        return 0

    if args.cmd == 'inspect':
        if not any(
            value is not None and str(value).strip()
            for value in (args.record_id, args.account, args.symbol, args.option_type, args.exp)
        ) and args.strike is None:
            raise SystemExit("inspect requires at least one selector")
        payload = inspect_projection_state(
            repo,
            base=state_base,
            record_id=args.record_id,
            account=args.account,
            symbol=args.symbol,
            option_type=args.option_type,
            strike=args.strike,
            expiration_ymd=((args.exp or '').strip() or None),
        )
        payload["ledger_store"] = ledger_store
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == 'lifecycle':
        if args.lifecycle_cmd == 'list':
            rows = list_trade_lifecycle_cases(
                repo,
                status=args.status,
                account=args.account,
                symbol=args.symbol,
            )
            if args.include_evidence:
                for row in rows:
                    case_id = str(row.get("case_id") or "").strip()
                    row["evidence"] = list_trade_lifecycle_evidence(repo, case_id=case_id) if case_id else []
            payload = {"cases": rows, "count": len(rows), "ledger_store": ledger_store}
            if args.format == 'json':
                print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
                return 0
            if not rows:
                print("(no lifecycle cases)")
                return 0
            print("# trade_lifecycle_cases")
            for row in rows:
                print(
                    f"- {row.get('case_id')} | {row.get('account')} | {row.get('symbol')} | "
                    f"{row.get('position_side')} {row.get('option_type')} | "
                    f"exp {row.get('expiration_ymd') or '-'} | strike {row.get('strike') if row.get('strike') is not None else '-'} | "
                    f"status {row.get('status')} decision {row.get('decision_type') or '-'}"
                )
            return 0
        if args.lifecycle_cmd == 'inspect':
            case_id = str(args.case_id or '').strip()
            rows = [row for row in list_trade_lifecycle_cases(repo) if str(row.get('case_id') or '').strip() == case_id]
            if not rows:
                raise SystemExit(f"lifecycle case not found: {case_id}")
            row = dict(rows[0])
            row["evidence"] = list_trade_lifecycle_evidence(repo, case_id=case_id)
            payload = {"case": row, "ledger_store": ledger_store}
            if args.format == 'json':
                print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
                return 0
            print("# trade_lifecycle_case")
            print(
                f"{row.get('case_id')} | {row.get('account')} | {row.get('symbol')} | "
                f"{row.get('position_side')} {row.get('option_type')} | "
                f"exp {row.get('expiration_ymd') or '-'} | strike {row.get('strike') if row.get('strike') is not None else '-'} | "
                f"status {row.get('status')} decision {row.get('decision_type') or '-'}"
            )
            evidence_rows = row.get("evidence") if isinstance(row.get("evidence"), list) else []
            if evidence_rows:
                print("# evidence")
                for evidence in evidence_rows:
                    if not isinstance(evidence, dict):
                        continue
                    print(
                        f"- {evidence.get('evidence_id')} | {evidence.get('evidence_type')} | "
                        f"source={evidence.get('source_event_id') or '-'}"
                    )
            return 0
        if args.lifecycle_cmd == 'reconcile':
            control = write_controls["lifecycle:reconcile"]
            dry_run = not bool(control["write_requested"])
            discovery = discover_lifecycle_cases(
                repo,
                account=args.account,
                observed_at_ms=args.observed_at_ms,
                apply_changes=not dry_run,
            )
            reconciliation = None
            evidence_path = str(args.evidence_json or "").strip()
            if evidence_path:
                evidence = _load_json_object(
                    _resolve_path_under(evidence_path, base=base)
                )
                wheel_start_enabled = False
                if args.config:
                    lifecycle_cfg = _load_json_object(
                        _resolve_path_under(args.config, base=base)
                    )
                    wheel_start_enabled = bool(
                        resolve_wheel_config(
                            lifecycle_cfg,
                            str(evidence.get("account") or args.account or ""),
                        ).get("enabled_for_new_lifecycle")
                    )
                reconciliation = reconcile_lifecycle_evidence(
                    repo,
                    evidence=evidence,
                    case_id=args.case_id,
                    target_lot_id=args.target_lot_id,
                    apply_changes=not dry_run,
                    now_ms=args.observed_at_ms,
                    wheel_start_enabled=wheel_start_enabled,
                ).to_dict()
            cases = list_trade_lifecycle_cases(
                repo,
                account=args.account,
            )
            read_models = []
            for row in cases:
                case_id = str(row.get("case_id") or "").strip()
                if not case_id or str(row.get("schema_version") or "").strip() != "lifecycle_case.v2":
                    continue
                read_models.append(
                    lifecycle_case_read_model(
                        repo,
                        case_id=case_id,
                        now_ms=args.observed_at_ms,
                    )
                )
            write_applied = bool(
                not dry_run
                and (
                    discovery.get("created_case_ids")
                    or discovery.get("refreshed_case_ids")
                    or (
                        isinstance(reconciliation, dict)
                        and isinstance(reconciliation.get("ledger_result"), dict)
                        and any(
                            reconciliation["ledger_result"].get(field)
                            for field in (
                                "evidence_created",
                                "evidence_bound",
                                "terminal_events_created",
                                "allocations_created",
                                "status_changed",
                            )
                        )
                    )
                )
            )
            payload = attach_write_contract(
                {
                    "operation": "lifecycle_reconcile",
                    "mode": "dry_run" if dry_run else "apply",
                    "discovery": discovery,
                    "reconciliation": reconciliation,
                    "read_models": read_models,
                    "ledger_store": ledger_store,
                },
                dry_run=dry_run,
                write_applied=write_applied,
                rollback_hint=(
                    "lifecycle facts are append-only; repair mistakes with controlled "
                    "void/repair commands, never delete allocations"
                ),
            )
            if args.format == 'json':
                print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            else:
                prefix = "[DRY_RUN]" if dry_run else "[DONE]"
                print(
                    f"{prefix} lifecycle reconcile "
                    f"created={len(discovery.get('created_case_ids') or [])} "
                    f"would_create={len(discovery.get('would_create_case_ids') or [])} "
                    f"refreshed={len(discovery.get('refreshed_case_ids') or [])} "
                    f"cases={len(read_models)}"
                )
                if reconciliation is not None:
                    print(
                        f"reconciliation={reconciliation.get('status')} "
                        f"reasons={','.join(reconciliation.get('reason_codes') or []) or '-'}"
                    )
            return 0
        if args.lifecycle_cmd in {'resolve', 'correct'}:
            command_name = str(args.lifecycle_cmd)
            control = write_controls[
                f"lifecycle:{command_name}"
            ]
            dry_run = not bool(control["write_requested"])
            observed_at_ms = int(
                args.observed_at_ms
                if args.observed_at_ms is not None
                else utc_now_ms()
            )
            wheel_start_enabled = False
            if args.config:
                lifecycle_cfg = _load_json_object(
                    _resolve_path_under(args.config, base=base)
                )
                lifecycle_case = next(
                    (
                        row
                        for row in list_trade_lifecycle_cases(repo)
                        if str(row.get("case_id") or "") == str(args.case_id)
                    ),
                    None,
                )
                if lifecycle_case is None:
                    raise SystemExit(f"lifecycle case not found: {args.case_id}")
                wheel_start_enabled = bool(
                    resolve_wheel_config(
                        lifecycle_cfg,
                        str(lifecycle_case.get("account") or ""),
                    ).get("enabled_for_new_lifecycle")
                )
            result = resolve_lifecycle_manually(
                repo,
                case_id=str(args.case_id),
                expected_revision=int(args.expected_revision),
                reason=str(args.reason),
                broker_ref=str(args.broker_ref or ""),
                note=str(args.note or ""),
                void_terminal_event_id=(
                    str(args.void_terminal_event_id)
                    if command_name == 'correct'
                    else None
                ),
                apply_changes=not dry_run,
                now_ms=observed_at_ms,
                wheel_start_enabled=wheel_start_enabled,
            )
            payload = attach_write_contract(
                {
                    "operation": (
                        f"lifecycle_{command_name}"
                    ),
                    **result,
                    "ledger_store": ledger_store,
                },
                dry_run=dry_run,
                write_applied=bool(
                    not dry_run
                    and result.get("status") == "applied"
                ),
                rollback_hint=(
                    "lifecycle corrections are append-only; correct "
                    "again with the new revision instead of deleting "
                    "events, evidence, or allocations"
                ),
            )
            print(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
            return 0
        if args.lifecycle_cmd == 'reconcile-due':
            control = write_controls["lifecycle:reconcile-due"]
            dry_run = not bool(control["write_requested"])
            observed_at_ms = int(
                args.observed_at_ms
                if args.observed_at_ms is not None
                else utc_now_ms()
            )
            config_path = _resolve_path_under(
                str(args.config),
                base=base,
            )
            cfg = _load_json_object(config_path)
            intake_cfg = resolve_trade_intake_config(cfg)
            account_value = str(args.account).strip().lower()
            sources = [
                dict(item)
                for item in intake_cfg.get("sources") or []
                if isinstance(item, dict)
                and (
                    str(
                        item.get("account") or ""
                    ).strip().lower()
                    == account_value
                    or account_value
                    in {
                        str(value or "").strip().lower()
                        for value in dict(
                            item.get("account_mapping") or {}
                        ).values()
                    }
                )
            ]
            if len(sources) != 1:
                raise SystemExit(
                    "lifecycle reconcile-due requires exactly one "
                    "configured OpenD source for account="
                    f"{account_value}; matched={len(sources)}"
                )
            source = sources[0]
            if not str(source.get("account") or "").strip():
                source["account"] = account_value
            wheel_start_enabled = bool(
                resolve_wheel_config(cfg, account_value).get(
                    "enabled_for_new_lifecycle"
                )
            )
            if dry_run:
                result = reconcile_due_lifecycle_cases_for_source(
                    repo,
                    source=source,
                    now_ms=observed_at_ms,
                    apply_changes=False,
                    wheel_start_enabled=wheel_start_enabled,
                )
            else:
                binding = resolve_account_broker_binding_sets(
                    [(None, cfg)]
                ).get(account_value)
                quote_route = resolve_futu_quote_route(cfg)
                if (
                    binding is None
                    or not binding.ok
                    or not quote_route.ok
                ):
                    raise SystemExit(
                        "lifecycle reconcile-due requires valid broker and canonical quote routes"
                    )
                audit_base = (
                    Path(str(args.runtime_root)).expanduser().resolve()
                    if getattr(args, "runtime_root", None)
                    else base
                )
                audit_path = _resolve_path_under(
                    source.get("audit_path")
                    or "output_shared/state/auto_trade_intake_audit.jsonl",
                    base=audit_base,
                )

                def seal_sink(payload: dict[str, Any]) -> None:
                    append_trade_intake_audit(
                        audit_path,
                        payload,
                        durable=True,
                    )

                try:
                    append_lifecycle_attempt_checkpoint_seal(
                        audit_path,
                        repo,
                        account=account_value,
                        source_id=str(
                            source.get("id") or account_value
                        ),
                        completed_at_ms=max(1, utc_now_ms()),
                        reason="cli_apply",
                    )
                except Exception as exc:
                    raise SystemExit(
                        "lifecycle reconcile-due seal_persist_failed: "
                        f"{type(exc).__name__}"
                    ) from exc
                broker_gateway = build_ready_futu_broker_gateway(
                    host=str(binding.host),
                    port=int(binding.port or 0),
                    expected_account_ids=(
                        binding.required_account_ids
                    ),
                    trd_env=str(binding.trd_env),
                    is_option_chain_cache_enabled=False,
                )
                quote_gateway = None
                try:
                    quote_gateway = build_ready_futu_quote_gateway(
                        host=str(quote_route.host),
                        port=int(quote_route.port or 0),
                        is_option_chain_cache_enabled=False,
                    )
                    result = (
                        reconcile_due_lifecycle_cases_for_source(
                            repo,
                            source=source,
                            broker_gateway=broker_gateway,
                            quote_gateway=quote_gateway,
                            trd_env=str(binding.trd_env),
                            now_ms=observed_at_ms,
                            apply_changes=True,
                            seal_sink=seal_sink,
                            wheel_start_enabled=wheel_start_enabled,
                        )
                    )
                finally:
                    broker_gateway.close()
                    if quote_gateway is not None:
                        quote_gateway.close()
            payload = attach_write_contract(
                {
                    "operation": "lifecycle_reconcile_due",
                    **result,
                    "ledger_store": ledger_store,
                },
                dry_run=dry_run,
                write_applied=bool(
                    not dry_run
                    and any(
                        isinstance(item, dict)
                        and isinstance(
                            item.get("write_result"),
                            dict,
                        )
                        and bool(
                            item["write_result"].get(
                                "business_state_changed"
                            )
                            or item["write_result"].get(
                                "status_changed"
                            )
                            or item["write_result"].get(
                                "notification_outbox_created"
                            )
                        )
                        for item in result.get("results") or []
                    )
                ),
                rollback_hint=(
                    "lifecycle derived state and notifications are "
                    "append-only/CAS controlled; reconcile corrections "
                    "through lifecycle correct"
                ),
            )
            print(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
            return (
                1
                if not dry_run
                and result.get("seal_status")
                == "seal_persist_failed"
                else 0
            )
        if args.lifecycle_cmd == 'migration':
            if args.migration_cmd == 'inventory':
                explicit_mapping = None
                if args.mapping_manifest:
                    explicit_mapping = _load_json_object(
                        _resolve_path_under(
                            str(args.mapping_manifest),
                            base=base,
                        )
                    )
                manifest = build_lifecycle_migration_inventory(
                    repo,
                    explicit_mapping=explicit_mapping,
                )
                selected_targets = [
                    str(item)
                    for item in args.select_target or []
                ]
                if selected_targets:
                    manifest = select_lifecycle_migration_targets(
                        manifest,
                        target_keys=selected_targets,
                    )
                print(
                    json.dumps(
                        {
                            "operation": (
                                "lifecycle_migration_inventory"
                            ),
                            "mode": "dry_run",
                            "manifest": manifest,
                            "ledger_store": ledger_store,
                        },
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    )
                )
                return 0
            if args.migration_cmd == 'apply':
                control = write_controls[
                    "lifecycle:migration:apply"
                ]
                dry_run = not bool(control["write_requested"])
                manifest = _load_json_object(
                    _resolve_path_under(
                        str(args.manifest),
                        base=base,
                    )
                )
                if isinstance(manifest.get("manifest"), dict):
                    manifest = dict(manifest["manifest"])
                result = apply_lifecycle_migration_manifest(
                    repo,
                    manifest=manifest,
                    apply_changes=not dry_run,
                )
                payload = attach_write_contract(
                    {
                        "operation": (
                            "lifecycle_migration_apply"
                        ),
                        **result,
                        "ledger_store": ledger_store,
                    },
                    dry_run=dry_run,
                    write_applied=bool(
                        not dry_run
                        and int(result.get("applied_count") or 0)
                        > 0
                    ),
                    rollback_hint=(
                        "keep trade-intake stopped; restore the "
                        "WAL-safe ledger snapshot or repair forward"
                    ),
                )
                print(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    )
                )
                return 0
        if args.lifecycle_cmd == 'receipts':
            if args.receipt_cmd == 'inspect':
                if args.batch_id:
                    batch = (
                        repo.get_trade_lifecycle_notification_batch(
                            str(args.batch_id)
                        )
                    )
                    if not isinstance(batch, dict):
                        raise SystemExit(
                            "notification delivery batch not found: "
                            f"{args.batch_id}"
                        )
                    members = (
                        repo.list_trade_lifecycle_notification_batch_members(
                            str(args.batch_id)
                        )
                    )
                    inspection = {
                        "batch": batch,
                        "members": members,
                        "outbox": None,
                    }
                else:
                    row = repo.get_trade_lifecycle_notification(
                        str(args.outbox_id)
                    )
                    if not isinstance(row, dict):
                        raise SystemExit(
                            "notification outbox row not found: "
                            f"{args.outbox_id}"
                        )
                    batch_id = str(
                        row.get("delivery_batch_id") or ""
                    ).strip()
                    inspection = {
                        "outbox": row,
                        "batch": (
                            repo.get_trade_lifecycle_notification_batch(
                                batch_id
                            )
                            if batch_id
                            else None
                        ),
                        "members": (
                            repo.list_trade_lifecycle_notification_batch_members(
                                batch_id
                            )
                            if batch_id
                            else []
                        ),
                    }
                print(
                    json.dumps(
                        {
                            "operation": (
                                "lifecycle_receipt_inspect"
                            ),
                            **inspection,
                            "ledger_store": ledger_store,
                        },
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    )
                )
                return 0
            if args.receipt_cmd == 'reconcile':
                control = write_controls[
                    "lifecycle:receipts:reconcile"
                ]
                dry_run = not bool(control["write_requested"])
                batch_id = str(args.batch_id or "").strip()
                if not batch_id:
                    row = repo.get_trade_lifecycle_notification(
                        str(args.outbox_id)
                    )
                    if not isinstance(row, dict):
                        raise SystemExit(
                            "notification outbox row not found: "
                            f"{args.outbox_id}"
                        )
                    batch_id = str(
                        row.get("delivery_batch_id") or ""
                    ).strip()
                    if batch_id:
                        batch = (
                            repo.get_trade_lifecycle_notification_batch(
                                batch_id
                            )
                        )
                        if not isinstance(batch, dict):
                            raise SystemExit(
                                "notification delivery batch not found: "
                                f"{batch_id}"
                            )
                        if int(batch.get("member_count") or 0) != 1:
                            raise SystemExit(
                                "outbox row belongs to a multi-member batch; "
                                f"re-run with --batch-id {batch_id}"
                            )
                if batch_id:
                    result = reconcile_notification_batch(
                        repo,
                        batch_id=batch_id,
                        action=str(args.action),
                        broker_ref=str(args.broker_ref),
                        note=str(args.note),
                        apply_changes=not dry_run,
                        now_ms=utc_now_ms(),
                    )
                else:
                    result = reconcile_unknown_notification(
                        repo,
                        outbox_id=str(args.outbox_id),
                        action=str(args.action),
                        broker_ref=str(args.broker_ref),
                        note=str(args.note),
                        apply_changes=not dry_run,
                        now_ms=utc_now_ms(),
                    )
                payload = attach_write_contract(
                    {
                        "operation": (
                            "lifecycle_receipt_reconcile"
                        ),
                        **result,
                        "ledger_store": ledger_store,
                    },
                    dry_run=dry_run,
                    write_applied=not dry_run,
                    rollback_hint=(
                        "manual confirmation is append-only audit "
                        "metadata; resend creates a compensating intent "
                        "and never reopens the original row"
                    ),
                )
                print(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    )
                )
                return 0
            if args.receipt_cmd == 'dispatch':
                control = write_controls[
                    "lifecycle:receipts:dispatch"
                ]
                dry_run = not bool(control["write_requested"])
                account_value = str(
                    args.account or ""
                ).strip().lower()
                if not dry_run and account_value:
                    raise SystemExit(
                        "applied lifecycle receipt dispatch is global; "
                        "remove --account to preserve same-route batching"
                    )
                config_path = _resolve_path_under(
                    str(args.config),
                    base=base,
                )
                cfg = _load_json_object(config_path)
                intake_config = resolve_trade_intake_config(cfg)
                receipt_config = dict(
                    intake_config.get("receipt") or {}
                )
                enabled_accounts = (
                    _lifecycle_receipt_enabled_accounts(intake_config)
                )
                dispatch_accounts = (
                    enabled_accounts & {account_value}
                    if account_value
                    else enabled_accounts
                )
                route = (
                    resolve_trade_lifecycle_notification_batch_route(
                        config=cfg
                    )
                    if dispatch_accounts
                    else {}
                )
                if not dispatch_accounts:
                    result = {
                        "status": (
                            "dry_run" if dry_run else "idle"
                        ),
                        "reason": (
                            "requested_account_receipt_disabled"
                            if account_value
                            else "notification_receipt_disabled"
                        ),
                        "preview": None,
                    }
                elif not bool(route.get("route_available")):
                    result = {
                        "status": (
                            "dry_run" if dry_run else "explicit_failed"
                        ),
                        "reason": "notification_route_unavailable",
                        "preview": None,
                    }
                elif dry_run:
                    result = {
                        "status": "dry_run",
                        "preview": plan_notification_batch(
                            repo,
                            route=route,
                            now_ms=utc_now_ms(),
                            allowed_accounts=dispatch_accounts,
                            apply_changes=False,
                        ),
                    }
                else:
                    result = dispatch_notification_batch_once(
                        repo,
                        route=route,
                        send_fn=lambda frozen_payload: (
                            send_trade_lifecycle_outbox_payload(
                                base=base,
                                config=cfg,
                                receipt_config=receipt_config,
                                payload=frozen_payload,
                            )
                        ),
                        now_ms=utc_now_ms(),
                        allowed_accounts=dispatch_accounts,
                    )
                payload = attach_write_contract(
                    {
                        "operation": (
                            "lifecycle_receipt_dispatch_once"
                        ),
                        **result,
                        "ledger_store": ledger_store,
                    },
                    dry_run=dry_run,
                    write_applied=bool(
                        not dry_run
                        and _lifecycle_dispatch_write_applied(result)
                    ),
                    rollback_hint=(
                        "delivery state is durable; unknown must be "
                        "reconciled manually and is never auto-retried"
                    ),
                )
                print(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    )
                )
                return 0
        if args.lifecycle_cmd == 'confirm-expired':
            raise SystemExit(
                "lifecycle confirm-expired is retired: expiration without "
                "settlement requires a complete broker settlement "
                "observation via lifecycle reconcile-due"
            )

    if args.cmd == 'verify-projection':
        try:
            report = verify_position_lot_projection(
                base=state_base,
                repo=repo,
                mode=args.mode,
                publish_evidence=args.publish_evidence,
            )
        except ValueError as e:
            raise SystemExit(str(e))
        report["ledger_store"] = ledger_store
        if args.format == 'json':
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        summary = report.get('summary') or {}
        print(
            "[DONE] verified trade_events projection against position_lots "
            f"report_id={report.get('report_id')} "
            f"ok={bool(report.get('ok'))} "
            f"mode={report.get('mode_used')} "
            f"matched={int(summary.get('matched', 0))} "
            f"missing_in_position_lots={int(summary.get('missing_in_position_lots', 0))} "
            f"extra_in_position_lots={int(summary.get('extra_in_position_lots', 0))} "
            f"field_mismatch={int(summary.get('field_mismatch', 0))}"
        )
        return 0

    if args.cmd == 'void-event':
        control = write_controls["void-event"]
        should_apply = bool(control["write_requested"])
        try:
            result = (
                record_trade_event_void(repo, event_id=args.event_id, reason=args.void_reason)
                if should_apply
                else preview_trade_event_void(repo, event_id=args.event_id, reason=args.void_reason)
            )
        except ValueError as e:
            raise SystemExit(str(e))
        result["ledger_store"] = ledger_store
        result = attach_write_contract(
            result,
            dry_run=not should_apply,
            write_applied=should_apply,
            rollback_hint="void-event appends an immutable correction; restore from backup if this was accidental",
        )
        if _json_or_text_format(args) == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if not should_apply:
            print(f"[DRY_RUN] would void event_id={args.event_id} reason={args.void_reason}")
            return 0
        print(
            f"[DONE] voided event_id={args.event_id} "
            f"via={result.get('event_id')} "
            f"position_lots={result.get('position_lot_count')}"
        )
        return 0

    if args.cmd == 'adjust-lot':
        control = write_controls["adjust-lot"]
        dry_run = not bool(control["write_requested"])
        try:
            out = execute_manual_adjust(
                repo,
                record_id=args.record_id,
                contracts=args.contracts,
                strike=args.strike,
                expiration_ymd=((args.exp or '').strip() or None),
                premium_per_share=args.premium_per_share,
                multiplier=args.multiplier,
                opened_at_ms=args.opened_at_ms,
                strategy=args.strategy,
                leg_role=args.leg_role,
                strategy_group_id=args.strategy_group_id,
                yield_enhancement_mode=args.yield_enhancement_mode,
                strategy_snapshot=_parse_json_object_arg(args.strategy_snapshot_json, name="--strategy-snapshot-json"),
                dry_run=dry_run,
            )
        except ValueError as e:
            raise SystemExit(str(e))
        payload = attach_write_contract(
            {"operation": "manual_adjust", **out, "ledger_store": ledger_store},
            dry_run=dry_run,
            write_applied=not dry_run,
            rollback_hint="void the created adjust trade event with option-positions void-event --confirm",
        )
        if _json_or_text_format(args) == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 0
        patch = out["patch"]
        if dry_run:
            print('[DRY_RUN] adjust fields:')
            print(json.dumps(patch, ensure_ascii=False, indent=2))
            return 0
        res = out["result"]
        print(f"[DONE] adjusted {args.record_id} event_id={res.get('event_id')}")
        return 0

    if args.cmd == "adopt-combo-identity":
        control = write_controls["adopt-combo-identity"]
        dry_run = not bool(control["write_requested"])
        try:
            out = adopt_existing_combo_identity(
                repo,
                strategy_group_id=args.strategy_group_id,
                funding_put_record_id=(args.funding_put_record_id),
                funding_put_open_event_id=(args.funding_put_open_event_id),
                participation_call_record_id=(args.participation_call_record_id),
                participation_call_open_event_id=(args.participation_call_open_event_id),
                expected_contracts=args.expected_contracts,
                apply_changes=not dry_run,
            )
        except (TypeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        payload = attach_write_contract(
            {**out, "ledger_store": ledger_store},
            dry_run=dry_run,
            write_applied=bool(out.get("identity_created")),
            rollback_hint=(
                "strategy group identity is insert-only; restore the pre-write SQLite backup if adoption was erroneous"
            ),
        )
        if _json_or_text_format(args) == "json":
            print(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
            return 0
        prefix = "[NOOP]" if out.get("status") == "existing" else ("[DRY_RUN]" if dry_run else "[DONE]")
        print(
            f"{prefix} Combo identity "
            f"group={out['strategy_group_id']} "
            f"put={out['funding_put']['record_id']} "
            f"call={out['participation_call']['record_id']} "
            f"hash={out['identity']['identity_hash']}"
        )
        return 0

    if args.cmd == "combo-reconcile":
        account = normalize_account(args.account)
        runtime_environment = str(args.runtime_environment or "").strip().lower()
        evidence_base = Path(getattr(args, "runtime_root", None) or base).resolve()
        exposures = _combo_reconcile_exposures(
            base=evidence_base,
            repo=repo,
            account=account,
            runtime_environment=runtime_environment,
        )
        out = reconcile_combo_pair_inferences(
            repo=repo,
            account=account,
            runtime_environment=runtime_environment,
            exposures=exposures,
            persist=False,
        )
        print(
            json.dumps(
                {**out, "ledger_store": ledger_store},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    if args.cmd == "combo-inferences":
        rows = list_combo_pair_inferences(
            repo=repo,
            account=normalize_account(args.account),
            status=args.status,
        )
        payload = {
            "ok": True,
            "count": len(rows),
            "inferences": rows,
            "ledger_store": ledger_store,
        }
        if _json_or_text_format(args) == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        else:
            for item in rows:
                print(
                    f"{item.get('status')} {item.get('inference_id')} "
                    f"{item.get('put_record_id')} + {item.get('call_record_id')} "
                    f"evidence={item.get('evidence_grade')} "
                    f"hash={item.get('input_snapshot_hash')}"
                )
        return 0

    if args.cmd in {"confirm-combo", "reject-combo", "supersede-combo"}:
        control = write_controls[args.cmd]
        apply_changes = bool(control["write_requested"])
        existing = next(
            (
                item
                for item in list_combo_pair_inferences(repo=repo)
                if str(item.get("inference_id") or "") == args.inference_id
            ),
            None,
        )
        if existing is None:
            raise SystemExit(f"combo inference not found: {args.inference_id}")
        if args.cmd == "confirm-combo":
            mode_evidence = None
            if apply_changes:
                mode_evidence = _require_combo_confirmation_mode(
                    base=base,
                    args=args,
                    inference=existing,
                )
            out = adopt_post_trade_combo_pair(
                repo=repo,
                inference_id=args.inference_id,
                expected_input_hash=args.expected_input_hash,
                actor=args.actor,
                apply_changes=apply_changes,
            )
            if mode_evidence is not None:
                out["confirmation_mode"] = mode_evidence
            write_applied = out.get("status") == "adopted"
            rollback_hint = "use supersede-combo to append-only void both adoption events"
        elif args.cmd == "supersede-combo":
            out = supersede_post_trade_combo_pair(
                repo=repo,
                inference_id=args.inference_id,
                expected_input_hash=args.expected_input_hash,
                reason=args.reason,
                actor=args.actor,
                apply_changes=apply_changes,
            )
            write_applied = out.get("status") == "superseded"
            rollback_hint = "review the append-only void events; the old identity remains auditable"
        elif not apply_changes:
            if str(existing.get("input_snapshot_hash") or "") != args.expected_input_hash:
                raise SystemExit("combo inference input hash compare-and-set failed")
            out = {
                "schema_version": "post_trade_combo_rejection.v1",
                "status": "dry_run",
                "inference_id": args.inference_id,
                "decision_reason": args.reason,
            }
            write_applied = False
            rollback_hint = "rejection does not change trade events or position lots"
        else:
            out = reject_post_trade_combo_pair(
                repo=repo,
                inference_id=args.inference_id,
                expected_input_hash=args.expected_input_hash,
                reason=args.reason,
                actor=args.actor,
            )
            write_applied = out.get("status") == "user_rejected"
            rollback_hint = "rejected exact pairs do not reopen automatically"
        payload = attach_write_contract(
            {**out, "ledger_store": ledger_store},
            dry_run=not apply_changes,
            write_applied=bool(write_applied),
            rollback_hint=rollback_hint,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    raise SystemExit("unknown cmd")

if __name__ == '__main__':
    raise SystemExit(main())
