from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from .candidate_engine import (
    CandidateScoreWeights,
    CANDIDATE_REJECT_REASON_RULE_MAP,
    build_candidate_decision,
    build_candidate_rank_key,
    evaluate_candidate_return_floor,
    evaluate_candidate_risk_filter,
    normalize_legacy_reject_log_rows,
    rank_candidate_rows,
)

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
            "liquidity": 0.0,
            "risk_distance": 0.0,
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
            "liquidity": 0.0,
            "risk_distance": 0.0,
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
    score_weight_liquidity: float = 0.0
    score_weight_risk_distance: float = 0.0
    param_table_version: str = "v1"
    layer_order: tuple[str, ...] = ("激进", "中性", "保守")
    layered_fill_limit: int = 5

    def score_weights(self) -> CandidateScoreWeights:
        return CandidateScoreWeights(
            annualized_return=float(self.score_weight_annualized_return),
            net_income=float(self.score_weight_net_income),
            liquidity=float(self.score_weight_liquidity),
            risk_distance=float(self.score_weight_risk_distance),
        )


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
        "score_weight_liquidity": float(score.get("liquidity", 0.0) or 0.0),
        "score_weight_risk_distance": float(score.get("risk_distance", 0.0) or 0.0),
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


def filter_candidates(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    out, _ = filter_candidates_with_reject_log(df, cfg)
    return out


def _row_identity(row: pd.Series, mode: StrategyMode) -> dict:
    return {
        "symbol": row.get("symbol"),
        "contract_symbol": row.get("contract_symbol"),
        "expiration": row.get("expiration"),
        "strike": row.get("strike"),
        "mode": row.get("option_type") or mode,
    }


def _append_engine_reject_rows(
    *,
    sink: list[dict],
    row: pd.Series,
    decision: dict,
    reject_stage: str,
    mode: StrategyMode,
) -> None:
    identity = _row_identity(row, mode)
    for reject in decision.get("rejects") or []:
        if not isinstance(reject, dict):
            continue
        rule = CANDIDATE_REJECT_REASON_RULE_MAP.get(str(reject.get("reason") or ""))
        if not rule:
            continue
        sink.append(
            {
                "reject_stage": reject_stage,
                "reject_rule": rule,
                "metric_value": reject.get("metric_value"),
                "threshold": reject.get("threshold"),
                **identity,
                "engine_reject_stage": reject.get("stage"),
                "engine_reject_reason": reject.get("reason"),
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

    out_rows: list[pd.Series] = []
    reject_rows: list[dict] = []
    annual_col = annualized_return_column(cfg.mode)
    annual_values = _to_numeric(df, annual_col)
    net_income_values = _to_numeric(df, "net_income")
    spread_ratio_values = _to_numeric(df, "spread_ratio")

    for idx, row in df.iterrows():
        base = build_candidate_decision(
            mode=cfg.mode,
            symbol=str(row.get("symbol") or ""),
            contract_symbol=str(row.get("contract_symbol") or ""),
            accepted=True,
            rejects=[],
            normalized_input=row.to_dict(),
        )
        return_decision = evaluate_candidate_return_floor(
            base,
            min_annualized_return=cfg.min_annualized_return,
            min_net_income=cfg.min_net_income,
            annualized_return=annual_values.get(idx),
            net_income=net_income_values.get(idx),
        )
        risk_decision = evaluate_candidate_risk_filter(
            return_decision,
            max_spread_ratio=cfg.max_spread_ratio,
            spread_ratio=spread_ratio_values.get(idx),
        )
        if bool(risk_decision.get("accepted")):
            out_rows.append(row)
        else:
            _append_engine_reject_rows(
                sink=reject_rows,
                row=row,
                decision=risk_decision,
                reject_stage=reject_stage,
                mode=cfg.mode,
            )

    out = pd.DataFrame(out_rows, columns=df.columns)
    reject_log = pd.DataFrame(reject_rows)
    if reject_log.empty:
        return out.copy(), empty_reject_log_dataframe()
    for col in REJECT_LOG_COLUMNS:
        if col not in reject_log.columns:
            reject_log[col] = None
    return out.copy(), reject_log[REJECT_LOG_COLUMNS]


def score_candidates(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    out = df.copy()
    score_weights = cfg.score_weights()
    scores: list[float] = []
    for _, row in out.iterrows():
        rank_key = build_candidate_rank_key(
            row.to_dict(),
            mode=cfg.mode,
            score_weights=score_weights,
        )
        scores.append(float(rank_key.get("strategy_score") or 0.0))
    out["_strategy_score"] = scores
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

    ranked_rows = rank_candidate_rows(
        df.to_dict("records"),
        mode=cfg.mode,
        score_weights=cfg.score_weights(),
    )
    ranked = pd.DataFrame(ranked_rows, columns=df.columns)
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
