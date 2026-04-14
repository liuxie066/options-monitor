from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from .candidate_engine import normalize_legacy_reject_log_rows

StrategyMode = Literal["put", "call"]

REJECT_LOG_COLUMNS = [
    "reject_stage",
    "reject_rule",
    "metric_value",
    "threshold",
    "symbol",
    "contract_symbol",
    "expiration",
    "strike",
    "mode",
    "engine_reject_stage",
    "engine_reject_reason",
]

STRATEGY_PARAM_TABLE_V1: dict[StrategyMode, dict[str, dict[str, float | None]]] = {
    "put": {
        "hard_thresholds": {
            "min_annualized_return": None,
            "min_net_income": None,
            "max_spread_ratio": None,
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
            "max_spread_ratio": None,
        },
        "score_weights": {
            "annualized_return": 1.0,
            "net_income": 1e-6,
        },
    },
}


@dataclass(frozen=True)
class StrategyConfig:
    mode: StrategyMode
    min_annualized_return: float | None = None
    min_net_income: float | None = None
    max_spread_ratio: float | None = None
    score_weight_annualized_return: float = 1.0
    score_weight_net_income: float = 1e-6
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
        "max_spread_ratio": hard.get("max_spread_ratio"),
        "score_weight_annualized_return": float(score.get("annualized_return", 1.0) or 0.0),
        "score_weight_net_income": float(score.get("net_income", 0.0) or 0.0),
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
    return ("annualized_net_premium_return", "net_income")


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


def empty_reject_log_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=REJECT_LOG_COLUMNS)


def add_engine_reject_columns(reject_log: pd.DataFrame) -> pd.DataFrame:
    if reject_log.empty:
        return empty_reject_log_dataframe()
    out = reject_log.copy()
    mapped = normalize_legacy_reject_log_rows(out.to_dict("records"))
    out["engine_reject_stage"] = [row["stage"] for row in mapped]
    out["engine_reject_reason"] = [row["reason"] for row in mapped]
    for col in REJECT_LOG_COLUMNS:
        if col not in out.columns:
            out[col] = None
    return out[REJECT_LOG_COLUMNS]


def filter_candidates_with_reject_log(
    df: pd.DataFrame,
    cfg: StrategyConfig,
    *,
    reject_stage: str = "step3_risk_gate",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if df.empty:
        return df.copy(), empty_reject_log_dataframe()

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
    return out.copy(), add_engine_reject_columns(pd.DataFrame(reject_rows))


def score_candidates(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    out = df.copy()
    annual = _to_numeric(out, annualized_return_column(cfg.mode)).fillna(0.0)
    score = annual * float(cfg.score_weight_annualized_return)

    if "net_income" in out.columns:
        score = score + (_to_numeric(out, "net_income").fillna(0.0) * float(cfg.score_weight_net_income))

    out["_strategy_score"] = score
    return out


def rank_scored_candidates(
    df: pd.DataFrame,
    cfg: StrategyConfig,
    *,
    layered: bool = False,
    top: int | None = None,
) -> pd.DataFrame:
    return rank_candidates(score_candidates(df, cfg), cfg, layered=layered, top=top)


def filter_rank_candidates_with_reject_log(
    df: pd.DataFrame,
    cfg: StrategyConfig,
    *,
    reject_stage: str = "step3_risk_gate",
    layered: bool = False,
    top: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    filtered, reject_log = filter_candidates_with_reject_log(df, cfg, reject_stage=reject_stage)
    return rank_scored_candidates(filtered, cfg, layered=layered, top=top), reject_log


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
