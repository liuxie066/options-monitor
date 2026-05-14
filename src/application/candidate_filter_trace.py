from __future__ import annotations

import json
from pathlib import Path
from typing import Any


TRACE_SCHEMA_VERSION = "candidate_filter_trace.v1"

FUNCTION_SELL_PUT = "sell_put"
FUNCTION_SELL_CALL = "sell_call"
FUNCTION_CLOSE_ADVICE = "close_advice"
FUNCTION_YIELD_ENHANCEMENT = "yield_enhancement"
FUNCTION_CASH_RESERVE = "cash_reserve"
FUNCTION_SHARE_COVERAGE = "share_coverage"

CANDIDATE_FILTER_FUNCTIONS: tuple[str, ...] = (
    FUNCTION_SELL_PUT,
    FUNCTION_SELL_CALL,
    FUNCTION_CLOSE_ADVICE,
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
) -> dict[str, Any]:
    function_norm = _clean_text(function).lower()
    if function_norm not in CANDIDATE_FILTER_FUNCTIONS:
        raise ValueError(f"unsupported candidate filter function: {function}")
    status_norm = _clean_text(status).lower() or "rejected"
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "run_id": _clean_optional_text(run_id),
        "account": _clean_optional_text(account),
        "symbol": _clean_text(symbol).upper(),
        "function": function_norm,
        "mode": _clean_optional_text(mode),
        "status": status_norm,
        "stage": _clean_text(stage),
        "rule": _clean_text(rule),
        "metric_value": _jsonable(metric_value),
        "threshold": _jsonable(threshold),
        "contract_symbol": _clean_optional_text(contract_symbol),
        "expiration": _clean_optional_text(expiration),
        "strike": _jsonable(strike),
        "message": _clean_text(message),
        "evidence_path": _clean_optional_text(evidence_path),
        "config_values": _jsonable(config_values or {}),
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
) -> list[dict[str, Any]]:
    scope = infer_trace_scope_from_path(output_path)
    normalized = dict(decision.get("normalized_input") or {})
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


def _clean_optional_text(value: Any) -> str | None:
    text = _clean_text(value)
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
