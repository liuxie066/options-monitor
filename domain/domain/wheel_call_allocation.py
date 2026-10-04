"""Explicit per-branch allocation on one broker Call fill."""

from __future__ import annotations

from typing import Any, Mapping


def parse_wheel_call_allocations(value: Any) -> tuple[tuple[str, str, int], ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("wheel_call_allocations must be a nonempty list")
    allocations = []
    for row in value:
        if not isinstance(row, Mapping) or set(row) != {"stock_lot_id", "wheel_branch_id", "contracts"}:
            raise ValueError("wheel_call_allocations has invalid fields")
        stock, branch, contracts = row["stock_lot_id"], row["wheel_branch_id"], row["contracts"]
        if (not isinstance(stock, str) or not stock or stock.strip() != stock
                or not isinstance(branch, str) or not branch or branch.strip() != branch
                or type(contracts) is not int or contracts <= 0):
            raise ValueError("wheel_call_allocations has invalid identity or quantity")
        allocations.append((stock, branch, contracts))
    if len({row[0] for row in allocations}) != len(allocations) or len({row[1] for row in allocations}) != len(allocations):
        raise ValueError("wheel_call_allocations contains a duplicate branch")
    return tuple(sorted(allocations))
