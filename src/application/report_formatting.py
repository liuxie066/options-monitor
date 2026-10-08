from __future__ import annotations

import pandas as pd


def pct(v, digits: int = 2) -> str:
    if pd.isna(v):
        return "-"
    return f"{float(v) * 100:.{digits}f}%"


def num(v, digits: int = 2) -> str:
    if pd.isna(v):
        return "-"
    return f"{float(v):,.{digits}f}"


def strike_text(v) -> str:
    if pd.isna(v):
        return "-"
    fv = float(v)
    return str(int(fv)) if fv.is_integer() else f"{fv:.2f}"


def attribution_pending_text(row) -> str:
    """Describe the arbiter's result without mistaking every pending row for a choice."""
    reasons = set(row.get("reason_codes") or [])
    if row.get("status") == "conflict":
        return "归属冲突，需核对"
    if reasons == {"awaiting_ledger_commit"}:
        if row.get("rules_enabled") and row.get("selected_candidate_id"):
            return "系统归属处理中"
        return "归属待确认，OM Bot 查看"
    if reasons == {"multiple_strategy_candidates"}:
        return "归属待确认，OM Bot 查看"
    if "attribution_evidence_incomplete" in reasons:
        return "归属证据不足，需核对"
    return "归属暂受阻，需核对" if reasons else "归属待核对"
