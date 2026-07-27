from __future__ import annotations

from collections import defaultdict
from typing import Any

from domain.domain.strategy_vocab import STRATEGY_COMBO_YIELD, canonical_strategy_id
from domain.domain.symbol_identity import canonical_symbol


def _quantity(value: Any) -> int | None:
    if value in (None, ""):
        return 0
    try:
        numeric = float(value)
        parsed = int(numeric)
    except (TypeError, ValueError, OverflowError):
        return None
    if numeric != parsed or parsed < 0:
        return None
    return parsed


def _text(value: Any) -> str:
    return str(value or "").strip()


def _expiry_structure(row: dict[str, Any]) -> str:
    snapshot = row.get("strategy_snapshot")
    snapshot_fields = snapshot if isinstance(snapshot, dict) else {}
    value = snapshot_fields.get("expiry_structure") or row.get("expiry_structure")
    if value:
        return _text(value).lower()
    structure_mode = _text(snapshot_fields.get("structure_mode") or row.get("structure_mode")).lower()
    if structure_mode == "staggered_expiry_pair":
        return "diagonal"
    if structure_mode == "same_expiry_pair":
        return "same_expiry"
    return "same_expiry"


def build_option_group_inventory(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, source in enumerate(rows):
        row = dict(source or {})
        strategy = canonical_strategy_id(_text(row.get("strategy")))
        group_id = _text(row.get("strategy_group_id"))
        if strategy != STRATEGY_COMBO_YIELD and not group_id.startswith("combo_yield:"):
            continue
        key = group_id or f"__missing__:{_text(row.get('record_id')) or index}"
        grouped[key].append(row)

    inventory: list[dict[str, Any]] = []
    for key in sorted(grouped):
        lots = grouped[key]
        group_id = "" if key.startswith("__missing__:") else key
        issues: list[str] = []
        if not group_id:
            issues.append("missing_group_identity")

        symbols = {canonical_symbol(row.get("symbol")) or _text(row.get("symbol")).upper() for row in lots}
        symbols.discard("")
        if not symbols:
            issues.append("symbol_missing")
        elif len(symbols) > 1:
            issues.append("multiple_symbols")
        accounts = {_text(row.get("account")).lower() for row in lots if _text(row.get("account"))}
        if not accounts:
            issues.append("account_missing")
        elif len(accounts) > 1:
            issues.append("multiple_accounts")
        if (
            group_id.startswith("combo_yield:")
            and len(accounts) == 1
            and not group_id.startswith(f"combo_yield:{next(iter(accounts))}:")
        ):
            issues.append("group_account_mismatch")
        structures = {_expiry_structure(row) for row in lots}
        if len(structures) > 1:
            issues.append("mixed_expiry_structure")
        expiry_structure = next(iter(structures), "same_expiry")
        if expiry_structure not in {"same_expiry", "diagonal"}:
            issues.append("unsupported_expiry_structure")

        put_opened = put_open = put_closed = 0
        call_opened = call_open = call_closed = 0
        put_expirations: set[str] = set()
        call_expirations: set[str] = set()
        labels: set[str] = set()
        record_ids: list[str] = []

        for row in lots:
            record_id = _text(row.get("record_id"))
            if record_id:
                record_ids.append(record_id)
            option_type = _text(row.get("option_type")).lower()
            side = _text(row.get("side")).lower()
            leg_role = _text(row.get("leg_role")).lower()
            opened = _quantity(row.get("contracts"))
            open_count = _quantity(row.get("contracts_open"))
            closed = _quantity(row.get("contracts_closed"))
            if opened is None or open_count is None or closed is None:
                issues.append("invalid_contract_quantity")
            opened = opened or 0
            open_count = open_count or 0
            closed = closed or 0
            opened_count = max(opened, open_count + closed)
            closed_count = max(closed, opened_count - open_count)
            expiration = _text(row.get("expiration_ymd"))
            lifecycle_state = _text(row.get("lifecycle_state")).lower()
            resolved_by_lot = row.get("resolved_contracts_by_lot")
            resolved_contracts = 0
            if isinstance(resolved_by_lot, dict):
                for raw_quantity in resolved_by_lot.values():
                    quantity = _quantity(raw_quantity)
                    if quantity is None:
                        issues.append("invalid_lifecycle_resolved_quantity")
                    else:
                        resolved_contracts += quantity
            if resolved_contracts > 0:
                issues.append("lifecycle_terminal_allocation")
            if lifecycle_state and lifecycle_state != "open":
                issues.append(f"lifecycle_not_open:{lifecycle_state}")

            if option_type == "put" and side == "short":
                put_opened += opened_count
                put_open += open_count
                put_closed += closed_count
                if expiration:
                    put_expirations.add(expiration)
                if open_count:
                    labels.add("put_open")
                if closed_count:
                    labels.add("put_closed")
                if leg_role not in {"", "sell_put", "funding_put"}:
                    issues.append("put_leg_role_invalid")
            elif option_type == "call" and side == "long":
                call_opened += opened_count
                call_open += open_count
                call_closed += closed_count
                if expiration:
                    call_expirations.add(expiration)
                if open_count:
                    labels.add("call_open")
                if closed_count:
                    labels.add("call_closed")
                if leg_role not in {"", "enhancement_call", "participation_call"}:
                    issues.append("call_leg_role_invalid")
            else:
                issues.append("unsupported_option_leg")

        if len(put_expirations) > 1:
            issues.append("multiple_put_expirations")
        if len(call_expirations) > 1:
            issues.append("multiple_call_expirations")
        put_expiration = next(iter(put_expirations), None)
        call_expiration = next(iter(call_expirations), None)
        if put_opened > 0 and not put_expiration:
            issues.append("put_expiration_missing")
        if call_opened > 0 and not call_expiration:
            issues.append("call_expiration_missing")
        if put_expiration and call_expiration:
            if expiry_structure == "diagonal" and call_expiration <= put_expiration:
                issues.append("invalid_diagonal_expiry_order")
            if expiry_structure != "diagonal" and call_expiration != put_expiration:
                issues.append("same_expiry_mismatch")
        if put_open > 0 and call_open > 0 and put_open != call_open:
            issues.append("open_quantity_mismatch")

        unique_issues = sorted(set(issues))
        if unique_issues:
            classification = "review_required"
        elif put_open > 0 and call_open == put_open:
            classification = "active_combo"
        elif put_open > 0 and call_open == 0:
            classification = "missing_call"
        elif put_open == 0 and call_open > 0:
            classification = "residual_call"
        else:
            classification = "closed"

        inventory.append(
            {
                "strategy_group_id": group_id or None,
                "strategy": STRATEGY_COMBO_YIELD,
                "symbol": next(iter(symbols), None),
                "account": next(iter(accounts), None),
                "expiry_structure": expiry_structure,
                "put_expiration": put_expiration,
                "call_expiration": call_expiration,
                "put_contracts_opened": put_opened,
                "put_contracts_open": put_open,
                "put_contracts_closed": put_closed,
                "call_contracts_opened": call_opened,
                "call_contracts_open": call_open,
                "call_contracts_closed": call_closed,
                "inventory_labels": sorted(labels),
                "inventory_issues": unique_issues,
                "summary_classification": classification,
                "evidence_scope": "option_lots",
                "record_ids": sorted(set(record_ids)),
            }
        )
    return inventory


def build_full_group_lifecycle(
    option_groups: list[dict[str, Any]],
    *,
    assigned_stock_lots: list[dict[str, Any]] | None = None,
    assignment_events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    def bucket(group_id: str) -> dict[str, Any]:
        return grouped.setdefault(
            group_id,
            {
                "strategy_group_id": group_id,
                "option_group": None,
                "stock_lots": [],
                "assignment_events": [],
            },
        )

    for row in option_groups or []:
        group_id = _text(row.get("strategy_group_id"))
        if group_id:
            bucket(group_id)["option_group"] = dict(row)
    for row in assigned_stock_lots or []:
        group_id = _text(row.get("strategy_group_id"))
        if group_id:
            bucket(group_id)["stock_lots"].append(dict(row))
    for row in assignment_events or []:
        group_id = _text(row.get("strategy_group_id"))
        if group_id:
            bucket(group_id)["assignment_events"].append(dict(row))

    output: list[dict[str, Any]] = []
    for group_id in sorted(grouped):
        source = grouped[group_id]
        option = dict(source.get("option_group") or {})
        stock_lots = list(source.get("stock_lots") or [])
        assignments = list(source.get("assignment_events") or [])
        issues = [str(item) for item in option.get("inventory_issues") or [] if str(item)]

        put_open = _quantity(option.get("put_contracts_open")) or 0
        call_open = _quantity(option.get("call_contracts_open")) or 0
        assignment_contracts = 0
        assignment_event_ids: list[str] = []
        for event in assignments:
            quantity = _quantity(event.get("contracts"))
            if quantity is None:
                issues.append("invalid_assignment_contract_quantity")
            else:
                assignment_contracts += quantity
            event_id = _text(event.get("event_id"))
            if event_id:
                assignment_event_ids.append(event_id)
            if event.get("stock_settlement_valid") is False:
                issues.append("missing_assignment_settlement")

        shares_opened = shares_remaining = shares_sold = 0
        stock_lot_ids: list[str] = []
        stock_accounts: set[str] = set()
        stock_symbols: set[str] = set()
        for lot in stock_lots:
            opened = _quantity(lot.get("shares_opened"))
            remaining = _quantity(lot.get("shares_remaining"))
            sold = _quantity(lot.get("shares_sold"))
            if opened is None or remaining is None or sold is None:
                issues.append("invalid_assigned_stock_quantity")
                continue
            shares_opened += opened
            shares_remaining += remaining
            shares_sold += sold
            lot_id = _text(lot.get("stock_lot_id"))
            if lot_id:
                stock_lot_ids.append(lot_id)
            account = _text(lot.get("account")).lower()
            symbol = canonical_symbol(lot.get("symbol")) or _text(lot.get("symbol")).upper()
            if account:
                stock_accounts.add(account)
            if symbol:
                stock_symbols.add(symbol)

        option_account = _text(option.get("account")).lower()
        option_symbol = canonical_symbol(option.get("symbol")) or _text(option.get("symbol")).upper()
        if len(stock_accounts) > 1:
            issues.append("multiple_assigned_stock_accounts")
        if len(stock_symbols) > 1:
            issues.append("multiple_assigned_stock_symbols")
        if option_account and stock_accounts and stock_accounts != {option_account}:
            issues.append("assigned_stock_account_mismatch")
        if option_symbol and stock_symbols and stock_symbols != {option_symbol}:
            issues.append("assigned_stock_symbol_mismatch")
        if assignments and not stock_lots and "missing_assignment_settlement" not in issues:
            issues.append("missing_assigned_stock_lot")

        residual_call_contracts = max(0, call_open - put_open)
        if "open_quantity_mismatch" in issues and put_open + assignment_contracts == call_open:
            issues = [item for item in issues if item != "open_quantity_mismatch"]
        if shares_remaining > 0 and put_open > 0 and residual_call_contracts == 0:
            issues.append("mixed_active_combo_and_assigned_stock")

        unique_issues = sorted(set(issues))
        if unique_issues:
            classification = "review_required"
        elif shares_remaining > 0 and residual_call_contracts > 0:
            classification = "assigned_stock_with_residual_call"
        elif shares_remaining > 0 and put_open == 0 and call_open == 0:
            classification = "assigned_stock_only"
        elif shares_remaining == 0 and put_open == 0 and call_open > 0:
            classification = "residual_call"
        elif shares_remaining == 0 and put_open == 0 and call_open == 0:
            classification = "closed"
        else:
            unique_issues = sorted(set([*unique_issues, "full_lifecycle_not_terminal"]))
            classification = "review_required"

        output.append(
            {
                "strategy_group_id": group_id,
                "strategy": STRATEGY_COMBO_YIELD,
                "account": option.get("account") or next(iter(stock_accounts), None),
                "symbol": option.get("symbol") or next(iter(stock_symbols), None),
                "expiry_structure": option.get("expiry_structure"),
                "put_expiration": option.get("put_expiration"),
                "call_expiration": option.get("call_expiration"),
                "put_contracts_open": put_open,
                "call_contracts_open": call_open,
                "residual_call_contracts": residual_call_contracts,
                "assigned_contracts": assignment_contracts,
                "assigned_shares_opened": shares_opened,
                "assigned_shares_remaining": shares_remaining,
                "assigned_shares_sold": shares_sold,
                "option_inventory_classification": option.get("summary_classification"),
                "summary_classification": classification,
                "lifecycle_issues": unique_issues,
                "evidence_scope": "trade_events_and_assigned_stock_lots",
                "option_record_ids": list(option.get("record_ids") or []),
                "assignment_event_ids": sorted(set(assignment_event_ids)),
                "assigned_stock_lot_ids": sorted(set(stock_lot_ids)),
            }
        )
    return output


__all__ = ["build_full_group_lifecycle", "build_option_group_inventory"]
