from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


CANDIDATE_ENGINE_SCHEMA_VERSION = "1.0"
SCHEMA_KIND_CANDIDATE_DECISION = "candidate_decision"

StrategyMode = Literal["put", "call"]

STAGE_INPUT_NORMALIZATION = "stage0_input_normalization"
STAGE_HARD_CONSTRAINTS = "stage1_hard_constraints"
STAGE_RETURN_FLOOR = "stage2_return_floor"
STAGE_RISK_FILTER = "stage3_risk_filter"
STAGE_RANKING = "stage4_ranking"

CANDIDATE_STAGE_ORDER: tuple[str, ...] = (
    STAGE_INPUT_NORMALIZATION,
    STAGE_HARD_CONSTRAINTS,
    STAGE_RETURN_FLOOR,
    STAGE_RISK_FILTER,
    STAGE_RANKING,
)

REJECT_INPUT_MISSING = "input_missing"
REJECT_HARD_DTE = "hard_dte"
REJECT_HARD_STRIKE = "hard_strike"
REJECT_HARD_CAPACITY_PUT = "hard_capacity_put"
REJECT_HARD_CAPACITY_CALL = "hard_capacity_call"
REJECT_RETURN_ANNUALIZED = "return_annualized"
REJECT_RETURN_NET_INCOME = "return_net_income"
REJECT_RISK_OPEN_INTEREST = "risk_open_interest"
REJECT_RISK_VOLUME = "risk_volume"
REJECT_RISK_SPREAD = "risk_spread"
REJECT_RISK_EVENT_WARN = "risk_event_warn"
REJECT_RISK_EVENT_REJECT = "risk_event_reject"

CANDIDATE_REJECT_REASONS: tuple[str, ...] = (
    REJECT_INPUT_MISSING,
    REJECT_HARD_DTE,
    REJECT_HARD_STRIKE,
    REJECT_HARD_CAPACITY_PUT,
    REJECT_HARD_CAPACITY_CALL,
    REJECT_RETURN_ANNUALIZED,
    REJECT_RETURN_NET_INCOME,
    REJECT_RISK_OPEN_INTEREST,
    REJECT_RISK_VOLUME,
    REJECT_RISK_SPREAD,
    REJECT_RISK_EVENT_WARN,
    REJECT_RISK_EVENT_REJECT,
)

LEGACY_REJECT_RULE_REASON_MAP: dict[str, str] = {
    "min_annualized_return": REJECT_RETURN_ANNUALIZED,
    "min_net_income": REJECT_RETURN_NET_INCOME,
    "max_spread_ratio": REJECT_RISK_SPREAD,
}

LEGACY_REJECT_RULE_STAGE_MAP: dict[str, str] = {
    "min_annualized_return": STAGE_RETURN_FLOOR,
    "min_net_income": STAGE_RETURN_FLOOR,
    "max_spread_ratio": STAGE_RISK_FILTER,
}

COMMON_CRITICAL_FIELDS: tuple[str, ...] = (
    "symbol",
    "option_type",
    "expiration",
    "dte",
    "spot",
    "strike",
    "mid",
    "multiplier",
)

NUMERIC_INPUT_FIELDS: tuple[str, ...] = (
    "dte",
    "spot",
    "strike",
    "bid",
    "ask",
    "last_price",
    "mid",
    "open_interest",
    "volume",
    "implied_volatility",
    "delta",
    "multiplier",
)


@dataclass(frozen=True)
class CandidateReject:
    stage: str
    reason: str
    message: str = ""
    metric_value: Any = None
    threshold: Any = None

    def to_payload(self) -> dict[str, Any]:
        return normalize_candidate_reject(self)


@dataclass(frozen=True)
class CandidateScoreWeights:
    annualized_return: float = 1.0
    net_income: float = 1e-6
    liquidity: float = 0.0
    risk_distance: float = 0.0


@dataclass(frozen=True)
class CandidateStrategyScore:
    total: float
    components: dict[str, float]
    warnings: tuple[str, ...] = ()


def normalize_strategy_mode(mode: Any) -> StrategyMode:
    mode_norm = str(mode or "").strip().lower()
    if mode_norm not in {"put", "call"}:
        raise ValueError(f"unsupported candidate strategy mode: {mode}")
    return mode_norm  # type: ignore[return-value]


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    try:
        return bool(value != value)
    except Exception:
        return False


def _coerce_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _bounded(value: float, *, low: float = 0.0, high: float = 1.0) -> float:
    return max(float(low), min(float(high), float(value)))


def _score_average(parts: list[float]) -> float:
    values = [float(p) for p in parts if p is not None]
    if not values:
        return 0.0
    return sum(values) / len(values)


def _liquidity_quality(
    *,
    spread_ratio: float | None = None,
    open_interest: float | None = None,
    volume: float | None = None,
) -> float:
    parts: list[float] = []
    if spread_ratio is not None:
        parts.append(_bounded(1.0 - max(float(spread_ratio), 0.0)))
    if open_interest is not None:
        parts.append(_bounded(float(open_interest) / 100.0))
    if volume is not None:
        parts.append(_bounded(float(volume) / 10.0))
    return _score_average(parts)


def _risk_distance_quality(
    *,
    mode: StrategyMode,
    delta: float | None = None,
    otm_pct: float | None = None,
    dte: float | None = None,
) -> float:
    del mode
    parts: list[float] = []
    if delta is not None:
        parts.append(_bounded(1.0 - abs(float(delta))))
    if otm_pct is not None:
        parts.append(_bounded(float(otm_pct) / 0.20))
    if dte is not None:
        parts.append(_bounded(float(dte) / 45.0))
    return _score_average(parts)


def compute_candidate_strategy_score(
    *,
    mode: StrategyMode | str,
    annualized_return: float | None = None,
    net_income: float | None = None,
    spread_ratio: float | None = None,
    open_interest: float | None = None,
    volume: float | None = None,
    delta: float | None = None,
    otm_pct: float | None = None,
    dte: float | None = None,
    weights: CandidateScoreWeights | None = None,
) -> CandidateStrategyScore:
    mode_norm = normalize_strategy_mode(mode)
    score_weights = weights or CandidateScoreWeights()
    components = {
        "annualized_return": (_coerce_float(annualized_return) or 0.0) * float(score_weights.annualized_return),
        "net_income": (_coerce_float(net_income) or 0.0) * float(score_weights.net_income),
        "liquidity": _liquidity_quality(
            spread_ratio=_coerce_float(spread_ratio),
            open_interest=_coerce_float(open_interest),
            volume=_coerce_float(volume),
        )
        * float(score_weights.liquidity),
        "risk_distance": _risk_distance_quality(
            mode=mode_norm,
            delta=_coerce_float(delta),
            otm_pct=_coerce_float(otm_pct),
            dte=_coerce_float(dte),
        )
        * float(score_weights.risk_distance),
    }
    warnings: list[str] = []
    spread_value = _coerce_float(spread_ratio)
    if spread_value is not None and spread_value >= 0.30:
        warnings.append("wide_spread")
    total = sum(components.values())
    return CandidateStrategyScore(total=float(total), components=components, warnings=tuple(warnings))


def _first_float(src: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = _coerce_float(src.get(name))
        if value is not None:
            return value
    return None


def _candidate_score_inputs(src: dict[str, Any], *, mode: StrategyMode) -> dict[str, float | None]:
    if mode == "put":
        annualized_return = _first_float(src, "annualized_net_return_on_cash_basis")
        otm_pct = _first_float(src, "otm_pct")
    else:
        annualized_return = _first_float(src, "annualized_net_premium_return")
        otm_pct = _first_float(src, "otm_pct", "strike_above_spot_pct")
    return {
        "annualized_return": annualized_return,
        "net_income": _first_float(src, "net_income"),
        "spread_ratio": _first_float(src, "spread_ratio"),
        "open_interest": _first_float(src, "open_interest"),
        "volume": _first_float(src, "volume"),
        "delta": _first_float(src, "delta"),
        "otm_pct": otm_pct,
        "dte": _first_float(src, "dte"),
    }


_SCORE_COMPONENT_LABELS: dict[str, str] = {
    "annualized_return": "年化收益",
    "net_income": "净收入",
    "liquidity": "流动性",
    "risk_distance": "风险距离",
}

_SCORE_WARNING_LABELS: dict[str, str] = {
    "wide_spread": "价差偏宽",
}


def _primary_score_drivers(components: dict[str, float], *, limit: int = 2) -> list[str]:
    positive = [
        (name, float(value))
        for name, value in components.items()
        if _coerce_float(value) is not None and float(value) > 0.0
    ]
    positive.sort(key=lambda item: (-item[1], item[0]))
    return [name for name, _value in positive[: max(1, int(limit or 1))]]


def _rank_reason(primary_drivers: list[str], warnings: list[str]) -> str:
    parts: list[str] = []
    for driver in primary_drivers:
        if driver == "annualized_return":
            parts.append("年化收益贡献领先")
        elif driver == "net_income":
            parts.append("净收入贡献较高")
        elif driver == "liquidity":
            parts.append("流动性评分有正向贡献")
        elif driver == "risk_distance":
            parts.append("价外/Delta/DTE 风险距离有正向贡献")
    if not parts:
        parts.append("候选通过准入，排序分数主要由默认收益项决定")
    if warnings:
        warning_text = "、".join(_SCORE_WARNING_LABELS.get(item, item) for item in warnings)
        parts.append(f"存在{warning_text}提示")
    return "；".join(parts)


def explain_candidate_rank(
    row: dict[str, Any] | Any,
    *,
    mode: StrategyMode | str,
    score_weights: CandidateScoreWeights | None = None,
) -> dict[str, Any]:
    mode_norm = normalize_strategy_mode(mode)
    src = row if isinstance(row, dict) else {}
    rank_key = build_candidate_rank_key(src, mode=mode_norm, score_weights=score_weights)
    components = {
        str(name): float(value)
        for name, value in (rank_key.get("score_components") or {}).items()
        if _coerce_float(value) is not None
    }
    warnings = [str(item) for item in (rank_key.get("score_warnings") or []) if str(item).strip()]
    primary_drivers = _primary_score_drivers(components)
    score_inputs = _candidate_score_inputs(src, mode=mode_norm)
    return {
        "mode": mode_norm,
        "symbol": str(src.get("symbol") or "").strip().upper() or None,
        "contract_symbol": str(src.get("contract_symbol") or src.get("option_symbol") or "").strip() or None,
        "option_type": str(src.get("option_type") or ("put" if mode_norm == "put" else "call")).strip().lower() or None,
        "expiration": str(src.get("expiration") or "").strip() or None,
        "strike": _first_float(src, "strike"),
        "strategy_score": float(rank_key.get("strategy_score") or 0.0),
        "annualized_return": rank_key.get("annualized_return"),
        "net_income": rank_key.get("net_income"),
        "score_components": components,
        "score_component_labels": {name: _SCORE_COMPONENT_LABELS.get(name, name) for name in components},
        "score_inputs": score_inputs,
        "score_warnings": warnings,
        "risk_notes": [_SCORE_WARNING_LABELS.get(item, item) for item in warnings],
        "primary_drivers": primary_drivers,
        "primary_driver_labels": [_SCORE_COMPONENT_LABELS.get(item, item) for item in primary_drivers],
        "rank_reason": _rank_reason(primary_drivers, warnings),
    }


def _reject(
    sink: list[dict[str, Any]],
    *,
    stage: str,
    reason: str,
    message: str,
    metric_value: Any = None,
    threshold: Any = None,
) -> None:
    sink.append(
        build_candidate_reject(
            stage=stage,
            reason=reason,
            message=message,
            metric_value=metric_value,
            threshold=threshold,
        )
    )


def _normalize_candidate_input_row(
    raw: dict[str, Any],
    *,
    mode: StrategyMode,
) -> dict[str, Any]:
    out = dict(raw)
    out["mode"] = mode
    out["symbol"] = str(raw.get("symbol") or "").strip().upper()
    out["option_type"] = str(raw.get("option_type") or "").strip().lower()
    out["contract_symbol"] = str(raw.get("contract_symbol") or "").strip()
    out["expiration"] = str(raw.get("expiration") or "").strip()
    out["currency"] = str(raw.get("currency") or "").strip().upper()

    for field in NUMERIC_INPUT_FIELDS:
        if field not in raw:
            continue
        v = _coerce_float(raw.get(field))
        if field == "dte" and v is not None:
            try:
                out[field] = int(v)
            except Exception:
                out[field] = None
        else:
            out[field] = v
    return out


def normalize_candidate_reject(raw: CandidateReject | dict[str, Any] | Any) -> dict[str, Any]:
    if isinstance(raw, CandidateReject):
        src = {
            "stage": raw.stage,
            "reason": raw.reason,
            "message": raw.message,
            "metric_value": raw.metric_value,
            "threshold": raw.threshold,
        }
    elif isinstance(raw, dict):
        src = raw
    else:
        src = {}

    stage = str(src.get("stage") or "").strip()
    if stage not in CANDIDATE_STAGE_ORDER:
        raise ValueError(f"unsupported candidate reject stage: {stage}")

    reason = str(src.get("reason") or "").strip()
    if reason not in CANDIDATE_REJECT_REASONS:
        raise ValueError(f"unsupported candidate reject reason: {reason}")

    out = {
        "stage": stage,
        "reason": reason,
        "message": str(src.get("message") or ""),
    }
    if "metric_value" in src:
        out["metric_value"] = src.get("metric_value")
    if "threshold" in src:
        out["threshold"] = src.get("threshold")
    return out


def build_candidate_reject(
    *,
    stage: str,
    reason: str,
    message: str = "",
    metric_value: Any = None,
    threshold: Any = None,
) -> dict[str, Any]:
    return normalize_candidate_reject(
        CandidateReject(
            stage=stage,
            reason=reason,
            message=message,
            metric_value=metric_value,
            threshold=threshold,
        )
    )


def map_legacy_reject_rule(rule: str) -> dict[str, str]:
    rule_norm = str(rule or "").strip()
    reason = LEGACY_REJECT_RULE_REASON_MAP.get(rule_norm)
    stage = LEGACY_REJECT_RULE_STAGE_MAP.get(rule_norm)
    if not reason or not stage:
        raise ValueError(f"unsupported legacy reject rule: {rule}")
    return {"rule": rule_norm, "stage": stage, "reason": reason}


def normalize_legacy_reject_log_row(row: dict[str, Any] | Any) -> dict[str, Any]:
    """Convert existing scanner reject-log rows to Engine reject reason rows."""
    src = row if isinstance(row, dict) else {}
    mapped = map_legacy_reject_rule(str(src.get("reject_rule") or ""))
    reject = build_candidate_reject(
        stage=mapped["stage"],
        reason=mapped["reason"],
        message=str(src.get("reject_rule") or ""),
        metric_value=src.get("metric_value"),
        threshold=src.get("threshold"),
    )
    out = {
        **reject,
        "legacy_reject_stage": str(src.get("reject_stage") or ""),
        "legacy_reject_rule": mapped["rule"],
        "symbol": str(src.get("symbol") or "").strip().upper(),
        "contract_symbol": str(src.get("contract_symbol") or "").strip(),
        "expiration": str(src.get("expiration") or "").strip(),
        "strike": src.get("strike"),
        "mode": str(src.get("mode") or "").strip().lower(),
    }
    return out


def normalize_legacy_reject_log_rows(rows: list[dict[str, Any]] | Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("legacy reject log rows must be a list")
    return [normalize_legacy_reject_log_row(row) for row in rows]


def validate_candidate_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("candidate decision payload must be a dict")
    if str(payload.get("schema_kind") or "") != SCHEMA_KIND_CANDIDATE_DECISION:
        raise ValueError(f"schema_kind must be {SCHEMA_KIND_CANDIDATE_DECISION}")
    if str(payload.get("schema_version") or "") != CANDIDATE_ENGINE_SCHEMA_VERSION:
        raise ValueError(f"unsupported candidate decision schema_version: {payload.get('schema_version')}")
    normalize_strategy_mode(payload.get("mode"))
    if not isinstance(payload.get("accepted"), bool):
        raise ValueError("candidate decision accepted must be bool")
    rejects = payload.get("rejects")
    if not isinstance(rejects, list):
        raise ValueError("candidate decision rejects must be a list")
    payload["rejects"] = [normalize_candidate_reject(item) for item in rejects]
    return payload


def build_candidate_decision(
    *,
    mode: StrategyMode | str,
    symbol: str,
    contract_symbol: str | None = None,
    accepted: bool,
    rejects: list[dict[str, Any] | CandidateReject] | None = None,
    score: float | None = None,
    rank_key: dict[str, Any] | None = None,
    normalized_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reject_payloads = [normalize_candidate_reject(item) for item in (rejects or [])]
    out: dict[str, Any] = {
        "schema_kind": SCHEMA_KIND_CANDIDATE_DECISION,
        "schema_version": CANDIDATE_ENGINE_SCHEMA_VERSION,
        "mode": normalize_strategy_mode(mode),
        "symbol": str(symbol or "").strip().upper(),
        "contract_symbol": str(contract_symbol or "").strip(),
        "accepted": bool(accepted),
        "rejects": reject_payloads,
    }
    if score is not None:
        out["score"] = float(score)
    if isinstance(rank_key, dict):
        out["rank_key"] = dict(rank_key)
    if isinstance(normalized_input, dict):
        out["normalized_input"] = dict(normalized_input)
    return validate_candidate_decision_payload(out)


def evaluate_candidate_input(
    raw: dict[str, Any] | Any,
    *,
    mode: StrategyMode | str,
    extra_required_fields: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Stage 0 candidate input gate.

    This function intentionally avoids DTE, strike, return, liquidity, and event
    policy decisions. It only normalizes the input row and rejects rows that lack
    the critical fields needed by later stages.
    """
    mode_norm = normalize_strategy_mode(mode)
    src = raw if isinstance(raw, dict) else {}
    normalized = _normalize_candidate_input_row(src, mode=mode_norm)

    required = list(COMMON_CRITICAL_FIELDS)
    if extra_required_fields:
        for field in extra_required_fields:
            f = str(field or "").strip()
            if f and f not in required:
                required.append(f)

    missing = [field for field in required if _is_missing(normalized.get(field))]
    rejects: list[dict[str, Any]] = []
    if missing:
        rejects.append(
            build_candidate_reject(
                stage=STAGE_INPUT_NORMALIZATION,
                reason=REJECT_INPUT_MISSING,
                message=f"missing critical fields: {', '.join(missing)}",
                threshold=missing,
            )
        )

    option_type = str(normalized.get("option_type") or "").strip().lower()
    if option_type and option_type != mode_norm:
        rejects.append(
            build_candidate_reject(
                stage=STAGE_INPUT_NORMALIZATION,
                reason=REJECT_INPUT_MISSING,
                message=f"option_type mismatch: expected {mode_norm}, got {option_type}",
                metric_value=option_type,
                threshold=mode_norm,
            )
        )

    return build_candidate_decision(
        mode=mode_norm,
        symbol=str(normalized.get("symbol") or ""),
        contract_symbol=str(normalized.get("contract_symbol") or ""),
        accepted=(len(rejects) == 0),
        rejects=rejects,
        normalized_input=normalized,
    )


def evaluate_candidate_hard_constraints(
    raw: dict[str, Any] | Any,
    *,
    mode: StrategyMode | str,
    min_dte: int | float | None = None,
    max_dte: int | float | None = None,
    min_strike: int | float | None = None,
    max_strike: int | float | None = None,
    put_cash_required: int | float | None = None,
    put_cash_free: int | float | None = None,
    call_covered_contracts_available: int | float | None = None,
    extra_required_fields: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Stage 1 hard constraints gate.

    This preserves Stage 0 as the first gate. If input normalization rejects the
    row, Stage 1 does not add derived-policy rejects that would be based on
    incomplete inputs.
    """
    mode_norm = normalize_strategy_mode(mode)
    stage0 = evaluate_candidate_input(
        raw,
        mode=mode_norm,
        extra_required_fields=extra_required_fields,
    )
    normalized = dict(stage0.get("normalized_input") or {})
    rejects = list(stage0.get("rejects") or [])
    if not bool(stage0.get("accepted")):
        return build_candidate_decision(
            mode=mode_norm,
            symbol=str(normalized.get("symbol") or ""),
            contract_symbol=str(normalized.get("contract_symbol") or ""),
            accepted=False,
            rejects=rejects,
            normalized_input=normalized,
        )

    dte = _coerce_float(normalized.get("dte"))
    strike = _coerce_float(normalized.get("strike"))
    spot = _coerce_float(normalized.get("spot"))

    min_dte_v = _coerce_float(min_dte)
    if min_dte_v is not None and dte is not None and dte < min_dte_v:
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_DTE,
            message="dte below minimum",
            metric_value=dte,
            threshold=min_dte_v,
        )

    max_dte_v = _coerce_float(max_dte)
    if max_dte_v is not None and dte is not None and dte > max_dte_v:
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_DTE,
            message="dte above maximum",
            metric_value=dte,
            threshold=max_dte_v,
        )

    min_strike_v = _coerce_float(min_strike)
    if min_strike_v is not None and strike is not None and strike < min_strike_v:
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_STRIKE,
            message="strike below minimum",
            metric_value=strike,
            threshold=min_strike_v,
        )

    max_strike_v = _coerce_float(max_strike)
    if max_strike_v is not None and strike is not None and strike > max_strike_v:
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_STRIKE,
            message="strike above maximum",
            metric_value=strike,
            threshold=max_strike_v,
        )

    if mode_norm == "put" and strike is not None and spot is not None and strike >= spot:
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_STRIKE,
            message="put strike must be below spot",
            metric_value=strike,
            threshold={"spot": spot},
        )

    put_required_v = _coerce_float(put_cash_required)
    put_free_v = _coerce_float(put_cash_free)
    if mode_norm == "put" and put_required_v is not None and put_free_v is not None and put_required_v > put_free_v:
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_CAPACITY_PUT,
            message="put cash requirement exceeds free cash",
            metric_value=put_required_v,
            threshold=put_free_v,
        )

    call_cover_v = _coerce_float(call_covered_contracts_available)
    if mode_norm == "call" and call_cover_v is not None and call_cover_v < 1:
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_CAPACITY_CALL,
            message="covered contracts available below one",
            metric_value=call_cover_v,
            threshold=1,
        )

    return build_candidate_decision(
        mode=mode_norm,
        symbol=str(normalized.get("symbol") or ""),
        contract_symbol=str(normalized.get("contract_symbol") or ""),
        accepted=(len(rejects) == 0),
        rejects=rejects,
        normalized_input=normalized,
    )


def evaluate_candidate_return_floor(
    candidate_decision: dict[str, Any] | Any,
    *,
    min_annualized_return: int | float | None = None,
    min_net_income: int | float | None = None,
    annualized_return: int | float | None = None,
    net_income: int | float | None = None,
) -> dict[str, Any]:
    """Stage 2 return floor gate.

    Accepts a previous candidate decision DTO and appends only return-floor
    rejects when prior stages have accepted the row.
    """
    prev = validate_candidate_decision_payload(dict(candidate_decision or {}))
    mode_norm = normalize_strategy_mode(prev.get("mode"))
    normalized = dict(prev.get("normalized_input") or {})
    rejects = list(prev.get("rejects") or [])
    if not bool(prev.get("accepted")):
        return build_candidate_decision(
            mode=mode_norm,
            symbol=str(prev.get("symbol") or ""),
            contract_symbol=str(prev.get("contract_symbol") or ""),
            accepted=False,
            rejects=rejects,
            normalized_input=normalized,
        )

    annual_v = _coerce_float(annualized_return if annualized_return is not None else normalized.get("annualized_return"))
    min_annual_v = _coerce_float(min_annualized_return)
    if min_annual_v is not None and (annual_v is None or annual_v < min_annual_v):
        _reject(
            rejects,
            stage=STAGE_RETURN_FLOOR,
            reason=REJECT_RETURN_ANNUALIZED,
            message="annualized return below minimum",
            metric_value=annual_v,
            threshold=min_annual_v,
        )

    net_v = _coerce_float(net_income if net_income is not None else normalized.get("net_income"))
    min_net_v = _coerce_float(min_net_income)
    if min_net_v is not None and (net_v is None or net_v < min_net_v):
        _reject(
            rejects,
            stage=STAGE_RETURN_FLOOR,
            reason=REJECT_RETURN_NET_INCOME,
            message="net income below minimum",
            metric_value=net_v,
            threshold=min_net_v,
        )

    return build_candidate_decision(
        mode=mode_norm,
        symbol=str(prev.get("symbol") or ""),
        contract_symbol=str(prev.get("contract_symbol") or ""),
        accepted=(len(rejects) == 0),
        rejects=rejects,
        normalized_input=normalized,
    )


def evaluate_candidate_risk_filter(
    candidate_decision: dict[str, Any] | Any,
    *,
    min_open_interest: int | float | None = None,
    min_volume: int | float | None = None,
    max_spread_ratio: int | float | None = None,
    event_flag: bool = False,
    event_mode: str = "warn",
    open_interest: int | float | None = None,
    volume: int | float | None = None,
    spread_ratio: int | float | None = None,
) -> dict[str, Any]:
    """Stage 3 risk and execution quality gate."""
    prev = validate_candidate_decision_payload(dict(candidate_decision or {}))
    mode_norm = normalize_strategy_mode(prev.get("mode"))
    normalized = dict(prev.get("normalized_input") or {})
    rejects = list(prev.get("rejects") or [])
    if not bool(prev.get("accepted")):
        return build_candidate_decision(
            mode=mode_norm,
            symbol=str(prev.get("symbol") or ""),
            contract_symbol=str(prev.get("contract_symbol") or ""),
            accepted=False,
            rejects=rejects,
            normalized_input=normalized,
        )

    oi_v = _coerce_float(open_interest if open_interest is not None else normalized.get("open_interest"))
    min_oi_v = _coerce_float(min_open_interest)
    if min_oi_v is not None and oi_v is not None and oi_v < min_oi_v:
        _reject(
            rejects,
            stage=STAGE_RISK_FILTER,
            reason=REJECT_RISK_OPEN_INTEREST,
            message="open interest below minimum",
            metric_value=oi_v,
            threshold=min_oi_v,
        )

    vol_v = _coerce_float(volume if volume is not None else normalized.get("volume"))
    min_vol_v = _coerce_float(min_volume)
    if min_vol_v is not None and vol_v is not None and vol_v < min_vol_v:
        _reject(
            rejects,
            stage=STAGE_RISK_FILTER,
            reason=REJECT_RISK_VOLUME,
            message="volume below minimum",
            metric_value=vol_v,
            threshold=min_vol_v,
        )

    spread_v = _coerce_float(spread_ratio if spread_ratio is not None else normalized.get("spread_ratio"))
    max_spread_v = _coerce_float(max_spread_ratio)
    if max_spread_v is not None and spread_v is not None and spread_v > max_spread_v:
        _reject(
            rejects,
            stage=STAGE_RISK_FILTER,
            reason=REJECT_RISK_SPREAD,
            message="spread ratio above maximum",
            metric_value=spread_v,
            threshold=max_spread_v,
        )

    event_mode_norm = str(event_mode or "warn").strip().lower() or "warn"
    if bool(event_flag):
        if event_mode_norm == "reject":
            _reject(
                rejects,
                stage=STAGE_RISK_FILTER,
                reason=REJECT_RISK_EVENT_REJECT,
                message="key event hit in reject mode",
                metric_value=True,
                threshold=event_mode_norm,
            )
        else:
            _reject(
                rejects,
                stage=STAGE_RISK_FILTER,
                reason=REJECT_RISK_EVENT_WARN,
                message="key event hit in warn mode",
                metric_value=True,
                threshold=event_mode_norm,
            )

    return build_candidate_decision(
        mode=mode_norm,
        symbol=str(prev.get("symbol") or ""),
        contract_symbol=str(prev.get("contract_symbol") or ""),
        accepted=not any(r.get("reason") != REJECT_RISK_EVENT_WARN for r in rejects),
        rejects=rejects,
        normalized_input=normalized,
    )


def build_candidate_rank_key(
    row: dict[str, Any] | Any,
    *,
    mode: StrategyMode | str,
    score_weights: CandidateScoreWeights | None = None,
) -> dict[str, Any]:
    mode_norm = normalize_strategy_mode(mode)
    src = row if isinstance(row, dict) else {}
    if mode_norm == "put":
        score_inputs = _candidate_score_inputs(src, mode=mode_norm)
        annual = score_inputs["annualized_return"]
        net = score_inputs["net_income"]
        score = compute_candidate_strategy_score(
            mode=mode_norm,
            annualized_return=annual,
            net_income=net,
            spread_ratio=score_inputs["spread_ratio"],
            open_interest=score_inputs["open_interest"],
            volume=score_inputs["volume"],
            delta=score_inputs["delta"],
            otm_pct=score_inputs["otm_pct"],
            dte=score_inputs["dte"],
            weights=score_weights,
        )
        out: dict[str, Any] = {
            "strategy_score": score.total,
            "annualized_return": annual,
            "net_income": net,
            "score_components": dict(score.components),
            "score_warnings": list(score.warnings),
            "sort_tuple": (-score.total, -(annual or 0.0), -(net or 0.0)),
        }
        return out

    score_inputs = _candidate_score_inputs(src, mode=mode_norm)
    annual = score_inputs["annualized_return"]
    net = score_inputs["net_income"]
    score = compute_candidate_strategy_score(
        mode=mode_norm,
        annualized_return=annual,
        net_income=net,
        spread_ratio=score_inputs["spread_ratio"],
        open_interest=score_inputs["open_interest"],
        volume=score_inputs["volume"],
        delta=score_inputs["delta"],
        otm_pct=score_inputs["otm_pct"],
        dte=score_inputs["dte"],
        weights=score_weights,
    )
    out = {
        "strategy_score": score.total,
        "annualized_return": annual,
        "net_income": net,
        "score_components": dict(score.components),
        "score_warnings": list(score.warnings),
        "sort_tuple": (-score.total, -(annual or 0.0), -(net or 0.0)),
    }
    return out


def rank_candidate_rows(
    rows: list[dict[str, Any]],
    *,
    mode: StrategyMode | str,
    score_weights: CandidateScoreWeights | None = None,
) -> list[dict[str, Any]]:
    mode_norm = normalize_strategy_mode(mode)
    return sorted(
        [r for r in rows if isinstance(r, dict)],
        key=lambda row: build_candidate_rank_key(row, mode=mode_norm, score_weights=score_weights)["sort_tuple"],
    )
