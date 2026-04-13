from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

StrategyMode = Literal["put", "call"]


@dataclass(frozen=True)
class StrategyConfig:
    mode: StrategyMode
    min_annualized_return: float | None = None
    min_net_income: float | None = None
    min_otm_pct: float | None = None
    max_spread_ratio: float | None = None
    min_if_exercised_total_return: float | None = None
    layer_order: tuple[str, ...] = ("激进", "中性", "保守")
    layered_fill_limit: int = 5


def build_strategy_config(mode: StrategyMode, **kwargs) -> StrategyConfig:
    return StrategyConfig(mode=mode, **kwargs)


def annualized_return_column(mode: StrategyMode) -> str:
    if mode == "put":
        return "annualized_net_return_on_cash_basis"
    return "annualized_net_premium_return"


def sort_columns(mode: StrategyMode) -> tuple[str, ...]:
    if mode == "put":
        return ("annualized_net_return_on_cash_basis", "net_income")
    return ("annualized_net_premium_return", "if_exercised_total_return", "net_income")


def _to_numeric(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series([float("nan")] * len(df), index=df.index, dtype="float64")
    return pd.to_numeric(df[column], errors="coerce")


def _sort_df(df: pd.DataFrame, mode: StrategyMode) -> pd.DataFrame:
    cols = [c for c in sort_columns(mode) if c in df.columns]
    if not cols:
        return df
    asc = [False] * len(cols)
    return df.sort_values(cols, ascending=asc)


def filter_candidates(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    out = df.copy()
    annual_col = annualized_return_column(cfg.mode)

    if cfg.min_annualized_return is not None:
        annual = _to_numeric(out, annual_col)
        out = out[annual >= float(cfg.min_annualized_return)]
    if cfg.min_net_income is not None:
        net_income = _to_numeric(out, "net_income")
        out = out[net_income >= float(cfg.min_net_income)]
    if cfg.min_otm_pct is not None and "otm_pct" in out.columns:
        otm = _to_numeric(out, "otm_pct")
        out = out[otm >= float(cfg.min_otm_pct)]
    if cfg.max_spread_ratio is not None and "spread_ratio" in out.columns:
        spread_ratio = _to_numeric(out, "spread_ratio")
        out = out[spread_ratio.isna() | (spread_ratio <= float(cfg.max_spread_ratio))]
    if cfg.mode == "call" and cfg.min_if_exercised_total_return is not None:
        total_ret = _to_numeric(out, "if_exercised_total_return")
        out = out[total_ret >= float(cfg.min_if_exercised_total_return)]
    return out.copy()


def score_candidates(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    out = df.copy()
    annual = _to_numeric(out, annualized_return_column(cfg.mode)).fillna(0.0)
    score = annual

    if "net_income" in out.columns:
        score = score + (_to_numeric(out, "net_income").fillna(0.0) * 1e-6)
    if cfg.mode == "call" and "if_exercised_total_return" in out.columns:
        score = score + (_to_numeric(out, "if_exercised_total_return").fillna(0.0) * 1e-3)

    out["_strategy_score"] = score
    return out


def _row_key(row: pd.Series) -> tuple:
    key_cols = ("symbol", "expiration", "strike")
    values = []
    for col in key_cols:
        values.append(row.get(col))
    return tuple(values)


def rank_candidates(
    df: pd.DataFrame,
    cfg: StrategyConfig,
    *,
    layered: bool = False,
    top: int | None = None,
) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    ranked = _sort_df(df.copy(), cfg.mode)
    if not layered or "risk_label" not in ranked.columns:
        return ranked.head(top) if top is not None else ranked

    selected: list[pd.Series] = []
    used: set[tuple] = set()

    for layer in cfg.layer_order:
        layer_df = ranked[ranked["risk_label"] == layer]
        if layer_df.empty:
            continue
        row = layer_df.iloc[0]
        key = _row_key(row)
        if key in used:
            continue
        selected.append(row)
        used.add(key)

    remaining = ranked
    if used:
        mask = ranked.apply(lambda r: _row_key(r) in used, axis=1)
        remaining = ranked[~mask]

    limit = cfg.layered_fill_limit
    for _, row in remaining.iterrows():
        if len(selected) >= limit:
            break
        selected.append(row)

    out = pd.DataFrame(selected)
    if top is not None:
        out = out.head(top)
    return out
