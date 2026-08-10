from __future__ import annotations

import json
from pathlib import Path
from typing import Any


TRACE_SCHEMA_VERSION = "candidate_filter_trace.v1"

FUNCTION_SELL_PUT = "sell_put"
FUNCTION_SELL_CALL = "sell_call"
FUNCTION_COMBO_YIELD = "combo_yield"
FUNCTION_YIELD_ENHANCEMENT = FUNCTION_COMBO_YIELD
FUNCTION_CASH_RESERVE = "cash_reserve"
FUNCTION_SHARE_COVERAGE = "share_coverage"

CANDIDATE_FILTER_FUNCTIONS: tuple[str, ...] = (
    FUNCTION_SELL_PUT,
    FUNCTION_SELL_CALL,
    FUNCTION_YIELD_ENHANCEMENT,
    FUNCTION_CASH_RESERVE,
    FUNCTION_SHARE_COVERAGE,
)

TRACE_STATUS_ORDER: dict[str, int] = {
    "rejected": 0,
    "post_filtered": 1,
    "ranked_below": 2,
    "accepted": 3,
    "notified": 4,
    "not_observed": 5,
    "not_applicable": 6,
}

TRACE_REPLAY_FIELD_KEYS: tuple[str, ...] = (
    "spot",
    "dte",
    "delta",
    "abs_delta",
    "iv_rv_ratio",
    "iv_minus_rv",
    "annualized_return",
    "spread_ratio",
    "open_interest",
    "volume",
    "net_income",
    "multiplier",
)


def trace_function_for_mode(mode: Any) -> str:
    mode_norm = str(mode or "").strip().lower()
    if mode_norm == "call":
        return FUNCTION_SELL_CALL
    return FUNCTION_SELL_PUT


def candidate_trace_path_for_output(output_path: Path | str | None) -> Path | None:
    if output_path is None:
        return None
    return Path(output_path).resolve().parent / "candidate_filter_trace.jsonl"


def infer_trace_scope_from_path(path: Path | str | None) -> dict[str, str | None]:
    if path is None:
        return {"run_id": None, "account": None}
    parts = list(Path(path).resolve().parts)
    run_id = None
    account = None
    try:
        idx = parts.index("output_runs")
        run_id = parts[idx + 1] if idx + 1 < len(parts) else None
    except ValueError:
        pass
    try:
        idx = parts.index("accounts")
        account = parts[idx + 1] if idx + 1 < len(parts) else None
    except ValueError:
        try:
            idx = parts.index("output_accounts")
            account = parts[idx + 1] if idx + 1 < len(parts) else None
        except ValueError:
            pass
    return {"run_id": run_id, "account": account}


def build_candidate_filter_trace_row(
    *,
    run_id: Any = None,
    account: Any = None,
    symbol: Any,
    function: Any,
    mode: Any = None,
    option_type: Any = None,
    strategy_family: Any = None,
    strategy_profile: Any = None,
    status: Any,
    stage: Any,
    rule: Any,
    metric_value: Any = None,
    threshold: Any = None,
    contract_symbol: Any = None,
    expiration: Any = None,
    strike: Any = None,
    message: Any = "",
    evidence_path: Any = None,
    config_values: dict[str, Any] | None = None,
    replay_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    function_norm = _clean_trace_function(function)
    if function_norm not in CANDIDATE_FILTER_FUNCTIONS:
        raise ValueError(f"unsupported candidate filter function: {function}")
    status_norm = _clean_text(status).lower() or "rejected"
    config_values_json = _jsonable(config_values or {})
    family_norm = _clean_strategy_family(
        strategy_family
        or _trace_config_value(config_values_json, "strategy_family", "family")
        or _default_strategy_family(function_norm)
    )
    profile_norm = _clean_strategy_profile(
        strategy_profile
        or _trace_config_value(config_values_json, "strategy_profile", "profile", "strategy")
    )
    option_type_norm = _clean_option_type(option_type or mode)
    replay_fields_json = build_candidate_replay_fields(replay_fields)
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "run_id": _clean_optional_text(run_id),
        "account": _clean_optional_text(account),
        "symbol": _clean_text(symbol).upper(),
        "function": function_norm,
        "mode": _clean_optional_text(mode),
        "option_type": option_type_norm,
        "strategy_family": family_norm,
        "strategy_profile": profile_norm,
        "status": status_norm,
        "stage": _clean_text(stage),
        "rule": _clean_text(rule),
        "metric_value": _jsonable(metric_value),
        "threshold": _jsonable(threshold),
        "contract_symbol": _clean_optional_text(contract_symbol),
        "expiration": _clean_optional_text(expiration),
        "strike": _jsonable(strike),
        **replay_fields_json,
        "message": _clean_text(message),
        "evidence_path": _clean_optional_text(evidence_path),
        "config_values": config_values_json,
    }


def build_candidate_filter_trace_rows_from_decision(
    *,
    decision: dict[str, Any],
    function: str,
    status: str,
    reject_stage: str,
    evidence_path: str | None,
    config_values: dict[str, Any],
    output_path: Path | str | None = None,
    replay_fields: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    scope = infer_trace_scope_from_path(output_path)
    normalized = dict(decision.get("normalized_input") or {})
    replay_payload = build_candidate_replay_fields(normalized, replay_fields)
    rows: list[dict[str, Any]] = []
    for reject in list(decision.get("rejects") or []):
        reason = str(reject.get("reason") or "").strip()
        if not reason:
            continue
        rows.append(
            build_candidate_filter_trace_row(
                run_id=scope.get("run_id"),
                account=scope.get("account"),
                symbol=decision.get("symbol") or normalized.get("symbol"),
                function=function,
                mode=decision.get("mode"),
                status=status,
                stage=reject.get("stage") or reject_stage,
                rule=reason,
                metric_value=reject.get("metric_value"),
                threshold=reject.get("threshold"),
                contract_symbol=decision.get("contract_symbol") or normalized.get("contract_symbol"),
                expiration=normalized.get("expiration"),
                strike=normalized.get("strike"),
                message=reject.get("message") or "",
                evidence_path=evidence_path,
                config_values=config_values,
                replay_fields=replay_payload,
            )
        )
    return rows


def append_candidate_filter_trace_rows(path: Path | str | None, rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> None:
    if path is None or not rows:
        return
    out = Path(path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        for row in rows:
            if not isinstance(row, dict):
                continue
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_candidate_filter_trace(path: Path | str) -> list[dict[str, Any]]:
    source = Path(path).expanduser()
    rows: list[dict[str, Any]] = []
    if not source.exists():
        return rows
    with source.open("r", encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def build_candidate_replay_fields(*sources: dict[str, Any] | None, annualized_return: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in TRACE_REPLAY_FIELD_KEYS:
            if key in source and not _is_missing_scalar(source.get(key)):
                out[key] = _jsonable(source.get(key))
        if "annualized_return" not in out:
            annual = _first_present(
                source,
                "annualized_return",
                "annualized_net_return_on_cash_basis",
                "annualized_net_premium_return",
                "annualized_net_return",
            )
            if annual is not None:
                out["annualized_return"] = _jsonable(annual)

    if annualized_return is not None and not _is_missing_scalar(annualized_return):
        out["annualized_return"] = _jsonable(annualized_return)

    delta = _number_or_none(out.get("delta"))
    if "abs_delta" not in out and delta is not None:
        out["abs_delta"] = abs(delta)
    elif "abs_delta" in out:
        abs_delta = _number_or_none(out.get("abs_delta"))
        if abs_delta is not None:
            out["abs_delta"] = abs(abs_delta)

    if "iv_rv_ratio" not in out or "iv_minus_rv" not in out:
        merged: dict[str, Any] = {}
        for source in sources:
            if isinstance(source, dict):
                merged.update(source)
        iv = _number_or_none(_first_present(merged, "implied_volatility", "iv"))
        rv = _number_or_none(_first_present(merged, "realized_volatility_estimate", "realized_volatility", "rv"))
        if "iv_rv_ratio" not in out and iv is not None and rv is not None and rv > 0:
            out["iv_rv_ratio"] = round(iv / rv, 6)
        if "iv_minus_rv" not in out and iv is not None and rv is not None:
            out["iv_minus_rv"] = round(iv - rv, 6)

    return {key: out[key] for key in TRACE_REPLAY_FIELD_KEYS if key in out and not _is_missing_scalar(out[key])}


def _clean_optional_text(value: Any) -> str | None:
    text = _clean_text(value)
    return text or None


def _first_present(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source and not _is_missing_scalar(source.get(key)):
            return source.get(key)
    return None


def _number_or_none(value: Any) -> float | None:
    if _is_missing_scalar(value) or isinstance(value, bool):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _default_strategy_family(function_norm: str) -> str | None:
    if function_norm in {FUNCTION_SELL_PUT, FUNCTION_SELL_CALL, FUNCTION_YIELD_ENHANCEMENT}:
        return function_norm
    return None


def _trace_config_value(config_values: Any, *keys: str) -> Any:
    if not isinstance(config_values, dict):
        return None
    for key in keys:
        value = config_values.get(key)
        if not _is_missing_scalar(value):
            return value
    return None


def _clean_option_type(value: Any) -> str | None:
    text = _clean_text(value).lower()
    if text in {"put", "call"}:
        return text
    return None


def _clean_strategy_family(value: Any) -> str | None:
    text = _clean_text(value).lower().replace("-", "_")
    if text in {"sell_put", "put"}:
        return FUNCTION_SELL_PUT
    if text in {"sell_call", "covered_call", "call"}:
        return FUNCTION_SELL_CALL
    if text in {"combo_yield", "yield_enhancement", "income_upside_enhancement", "vol_convexity_enhancement"}:
        return FUNCTION_YIELD_ENHANCEMENT
    return text or None


def _clean_trace_function(value: Any) -> str:
    text = _clean_text(value).lower().replace("-", "_")
    if text in {"combo_yield", "yield_enhancement"}:
        return FUNCTION_COMBO_YIELD
    return text


def _clean_strategy_profile(value: Any) -> str | None:
    text = _clean_text(value).lower().replace("-", "_")
    if text in {"legacy", "return", "return_first", "yield_first", "income"}:
        return "return_first"
    if text in {"short_vol", "volatility_premium", "vol_premium"}:
        return "short_vol"
    return text or None


def _jsonable(value: Any) -> Any:
    if _is_missing_scalar(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def _clean_text(value: Any) -> str:
    if _is_missing_scalar(value):
        return ""
    return str(value).strip()


def _is_missing_scalar(value: Any) -> bool:
    if value is None:
        return True
    try:
        result = value != value
    except Exception:
        result = False
    try:
        if bool(result):
            return True
    except Exception:
        pass
    return str(value) in {"<NA>", "NaT", "nan", "None"}
