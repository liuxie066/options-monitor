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
    if min_annual_v is not None and annual_v is not None and annual_v < min_annual_v:
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
    if min_net_v is not None and net_v is not None and net_v < min_net_v:
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
) -> dict[str, Any]:
    mode_norm = normalize_strategy_mode(mode)
    src = row if isinstance(row, dict) else {}
    if mode_norm == "put":
        annual = _coerce_float(src.get("annualized_net_return_on_cash_basis"))
        net = _coerce_float(src.get("net_income"))
        score = (annual or 0.0) + ((net or 0.0) * 1e-6)
        out: dict[str, Any] = {
            "strategy_score": score,
            "annualized_return": annual,
            "net_income": net,
            "sort_tuple": (-score, -(annual or 0.0), -(net or 0.0)),
        }
        return out

    annual = _coerce_float(src.get("annualized_net_premium_return"))
    net = _coerce_float(src.get("net_income"))
    score = (annual or 0.0) + ((net or 0.0) * 1e-6)
    out = {
        "strategy_score": score,
        "annualized_return": annual,
        "net_income": net,
        "sort_tuple": (-score, -(annual or 0.0), -(net or 0.0)),
    }
    return out


def rank_candidate_rows(
    rows: list[dict[str, Any]],
    *,
    mode: StrategyMode | str,
) -> list[dict[str, Any]]:
    mode_norm = normalize_strategy_mode(mode)
    return sorted(
        [r for r in rows if isinstance(r, dict)],
        key=lambda row: build_candidate_rank_key(row, mode=mode_norm)["sort_tuple"],
    )
