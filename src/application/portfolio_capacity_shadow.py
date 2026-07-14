from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from domain.domain.engine import rank_candidate_rows
from domain.domain.insurance_underwriting import (
    INSURANCE_UNDERWRITING_PROFILE,
    rank_underwriting_candidates,
)
from domain.domain.risk_capacity import allocate_portfolio_capacity_shadow


EMPTY_OUTPUT_COLUMNS = [
    "account",
    "symbol",
    "contract_symbol",
    "option_type",
    "expiration",
    "strike",
    "strategy_family",
    "strategy_profile",
    "allocation_rank",
    "allocation_status",
    "allocation_reason",
    "allocated_contracts",
    "capacity_before",
    "capacity_required",
    "capacity_after",
    "capacity_unit",
]


def write_portfolio_capacity_shadow(*, report_dir: Path, account: str) -> dict[str, Any]:
    rows = _candidate_rows(Path(report_dir), account=account)
    allocated = allocate_portfolio_capacity_shadow(rows)
    output = Path(report_dir) / "portfolio_capacity_shadow.csv"
    pd.DataFrame(allocated, columns=EMPTY_OUTPUT_COLUMNS if not allocated else None).to_csv(output, index=False)
    counts: dict[str, int] = {}
    for row in allocated:
        status = str(row.get("allocation_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "rows": len(allocated),
        "status_counts": dict(sorted(counts.items())),
        "csv": str(output),
        "shadow_only": True,
    }


def _candidate_rows(report_dir: Path, *, account: str) -> list[dict[str, Any]]:
    put_paths = sorted(report_dir.glob("*_sell_put_candidates_labeled.csv"))
    if not put_paths:
        put_paths = sorted(report_dir.glob("*_sell_put_candidates.csv"))
    paths = [
        *(("sell_put", path) for path in put_paths),
        *(("covered_call", path) for path in sorted(report_dir.glob("*_sell_call_candidates.csv"))),
    ]
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for family, path in paths:
        try:
            frame = pd.read_csv(path)
        except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        for source_row, raw in enumerate(frame.to_dict("records"), start=1):
            row = dict(raw)
            row["account"] = str(row.get("account") or account).strip().lower()
            row["strategy_family"] = family
            row["source_path"] = path.name
            row["source_row"] = source_row
            identity = (
                row["account"],
                family,
                str(row.get("contract_symbol") or row.get("code") or ""),
                str(row.get("symbol") or ""),
                str(row.get("expiration") or row.get("expiration_ymd") or ""),
                str(row.get("strike") or ""),
            )
            if identity in seen:
                continue
            seen.add(identity)
            out.append(row)

    puts = [row for row in out if row["strategy_family"] == "sell_put"]
    calls = [row for row in out if row["strategy_family"] == "covered_call"]
    return [*_rank_sell_put_rows(puts), *calls]


def _rank_sell_put_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    underwriting = [_uses_underwriting_rank(row) for row in rows]
    if rows and all(underwriting):
        return rank_underwriting_candidates(rows, mode="put")
    if not any(underwriting):
        return rank_candidate_rows(rows, mode="put")
    # ponytail: mixed policies stay stable until a cross-policy priority is defined.
    return rows


def _uses_underwriting_rank(row: dict[str, Any]) -> bool:
    profile = str(row.get("strategy_profile") or row.get("scan_strategy_profile") or "").strip().lower()
    if profile == INSURANCE_UNDERWRITING_PROFILE:
        return True
    return any(
        str(row.get(key) or "").strip()
        for key in (
            "insurance_underwriting_mode",
            "premium_edge_score",
            "strike_safety_margin_pct",
        )
    )


__all__ = ["write_portfolio_capacity_shadow"]
