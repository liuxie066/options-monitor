from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

StrategyMode = Literal["put", "call"]

STRATEGY_PARAM_TABLE_V1: dict[StrategyMode, dict[str, dict[str, float | None]]] = {
    "put": {
        "hard_thresholds": {
            "min_annualized_return": None,
            "min_net_income": None,
            "min_otm_pct": None,
            "max_spread_ratio": None,
            "min_if_exercised_total_return": None,
        },
        "score_weights": {
            "annualized_return": 1.0,
            "net_income": 1e-6,
            "if_exercised_total_return": 0.0,
        },
    },
    "call": {
        "hard_thresholds": {
            "min_annualized_return": None,
            "min_net_income": None,
            "min_otm_pct": None,
            "max_spread_ratio": None,
            "min_if_exercised_total_return": None,
        },
        "score_weights": {
            "annualized_return": 1.0,
            "net_income": 1e-6,
            "if_exercised_total_return": 1e-3,
        },
    },
}


@dataclass(frozen=True)
class StrategyConfig:
    mode: StrategyMode
    min_annualized_return: float | None = None
    min_net_income: float | None = None
    min_otm_pct: float | None = None
    max_spread_ratio: float | None = None
    min_if_exercised_total_return: float | None = None
    score_weight_annualized_return: float = 1.0
    score_weight_net_income: float = 1e-6
    score_weight_if_exercised_total_return: float = 0.0
    param_table_version: str = "v1"
    layer_order: tuple[str, ...] = ("激进", "中性", "保守")
    layered_fill_limit: int = 5


def build_strategy_config(mode: StrategyMode, **kwargs) -> StrategyConfig:
    table = STRATEGY_PARAM_TABLE_V1.get(mode, {})
    hard = dict(table.get("hard_thresholds") or {})
    score = dict(table.get("score_weights") or {})

    defaults = {
        "min_annualized_return": hard.get("min_annualized_return"),
        "min_net_income": hard.get("min_net_income"),
        "min_otm_pct": hard.get("min_otm_pct"),
        "max_spread_ratio": hard.get("max_spread_ratio"),
        "min_if_exercised_total_return": hard.get("min_if_exercised_total_return"),
        "score_weight_annualized_return": float(score.get("annualized_return", 1.0) or 0.0),
        "score_weight_net_income": float(score.get("net_income", 0.0) or 0.0),
        "score_weight_if_exercised_total_return": float(score.get("if_exercised_total_return", 0.0) or 0.0),
        "param_table_version": "v1",
    }
    defaults.update(kwargs)
    return StrategyConfig(mode=mode, **defaults)


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
    out, _ = filter_candidates_with_reject_log(df, cfg)
    return out


def _append_reject_rows(
    *,
    sink: list[dict],
    rejected_df: pd.DataFrame,
    metric: pd.Series,
    reject_rule: str,
    reject_stage: str,
    threshold: float,
    mode: StrategyMode,
) -> None:
    if rejected_df.empty:
        return
    for idx, row in rejected_df.iterrows():
        sink.append(
            {
                "reject_stage": reject_stage,
                "reject_rule": reject_rule,
                "metric_value": (metric.get(idx) if metric is not None else None),
                "threshold": threshold,
                "symbol": row.get("symbol"),
                "contract_symbol": row.get("contract_symbol"),
                "expiration": row.get("expiration"),
                "strike": row.get("strike"),
                "mode": row.get("option_type") or mode,
            }
        )


def filter_candidates_with_reject_log(
    df: pd.DataFrame,
    cfg: StrategyConfig,
    *,
    reject_stage: str = "step3_risk_gate",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if df.empty:
        return df.copy(), pd.DataFrame(
            columns=[
                "reject_stage",
                "reject_rule",
                "metric_value",
                "threshold",
                "symbol",
                "contract_symbol",
                "expiration",
                "strike",
                "mode",
            ]
        )

    out = df.copy()
    reject_rows: list[dict] = []
    annual_col = annualized_return_column(cfg.mode)

    if cfg.min_annualized_return is not None:
        annual = _to_numeric(out, annual_col)
        threshold = float(cfg.min_annualized_return)
        mask = annual >= threshold
        rejected = out[~mask]
        _append_reject_rows(
            sink=reject_rows,
            rejected_df=rejected,
            metric=annual,
            reject_rule="min_annualized_return",
            reject_stage=reject_stage,
            threshold=threshold,
            mode=cfg.mode,
        )
        out = out[mask]
    if cfg.min_net_income is not None:
        net_income = _to_numeric(out, "net_income")
        threshold = float(cfg.min_net_income)
        mask = net_income >= threshold
        rejected = out[~mask]
        _append_reject_rows(
            sink=reject_rows,
            rejected_df=rejected,
            metric=net_income,
            reject_rule="min_net_income",
            reject_stage=reject_stage,
            threshold=threshold,
            mode=cfg.mode,
        )
        out = out[mask]
    if cfg.min_otm_pct is not None and "otm_pct" in out.columns:
        otm = _to_numeric(out, "otm_pct")
        threshold = float(cfg.min_otm_pct)
        mask = otm >= threshold
        rejected = out[~mask]
        _append_reject_rows(
            sink=reject_rows,
            rejected_df=rejected,
            metric=otm,
            reject_rule="min_otm_pct",
            reject_stage=reject_stage,
            threshold=threshold,
            mode=cfg.mode,
        )
        out = out[mask]
    if cfg.max_spread_ratio is not None and "spread_ratio" in out.columns:
        spread_ratio = _to_numeric(out, "spread_ratio")
        threshold = float(cfg.max_spread_ratio)
        mask = spread_ratio.isna() | (spread_ratio <= threshold)
        rejected = out[~mask]
        _append_reject_rows(
            sink=reject_rows,
            rejected_df=rejected,
            metric=spread_ratio,
            reject_rule="max_spread_ratio",
            reject_stage=reject_stage,
            threshold=threshold,
            mode=cfg.mode,
        )
        out = out[mask]
    if cfg.mode == "call" and cfg.min_if_exercised_total_return is not None:
        total_ret = _to_numeric(out, "if_exercised_total_return")
        threshold = float(cfg.min_if_exercised_total_return)
        mask = total_ret >= threshold
        rejected = out[~mask]
        _append_reject_rows(
            sink=reject_rows,
            rejected_df=rejected,
            metric=total_ret,
            reject_rule="min_if_exercised_total_return",
            reject_stage=reject_stage,
            threshold=threshold,
            mode=cfg.mode,
        )
        out = out[mask]
    return out.copy(), pd.DataFrame(reject_rows)


def score_candidates(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    out = df.copy()
    annual = _to_numeric(out, annualized_return_column(cfg.mode)).fillna(0.0)
    score = annual * float(cfg.score_weight_annualized_return)

    if "net_income" in out.columns:
        score = score + (_to_numeric(out, "net_income").fillna(0.0) * float(cfg.score_weight_net_income))
    if cfg.mode == "call" and "if_exercised_total_return" in out.columns:
        score = score + (
            _to_numeric(out, "if_exercised_total_return").fillna(0.0)
            * float(cfg.score_weight_if_exercised_total_return)
        )

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

    ranked = df.copy()
    if "_strategy_score" in ranked.columns:
        tie_cols = [c for c in sort_columns(cfg.mode) if c in ranked.columns]
        ranked = ranked.sort_values(["_strategy_score", *tie_cols], ascending=[False] * (1 + len(tie_cols)))
    else:
        ranked = _sort_df(ranked, cfg.mode)
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
