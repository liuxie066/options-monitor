from __future__ import annotations

from collections import defaultdict
from typing import Any

from domain.domain.strategy_vocab import STRATEGY_COMBO_YIELD
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
    if isinstance(snapshot, dict):
        value = snapshot.get("expiry_structure")
        if value:
            return _text(value).lower()
    return _text(row.get("expiry_structure") or "same_expiry").lower()


def build_option_group_inventory(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, source in enumerate(rows):
        row = dict(source or {})
        strategy = _text(row.get("strategy")).lower()
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
        if group_id and len(accounts) == 1 and not group_id.startswith(f"combo_yield:{next(iter(accounts))}:"):
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
                if leg_role not in {"", "sell_put"}:
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
                if leg_role not in {"", "enhancement_call"}:
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


__all__ = ["build_option_group_inventory"]
