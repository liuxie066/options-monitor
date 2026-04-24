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
