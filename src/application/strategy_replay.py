from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


DEFAULT_DTE_BUCKETS: tuple[tuple[int, int | None], ...] = (
    (0, 7),
    (8, 14),
    (15, 30),
    (31, 45),
    (46, 60),
    (61, 90),
    (91, None),
)

DEFAULT_DELTA_BUCKETS: tuple[tuple[float, float | None], ...] = (
    (0.0, 0.10),
    (0.10, 0.20),
    (0.20, 0.30),
    (0.30, 0.40),
    (0.40, None),
)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    try:
        return bool(value != value)
    except Exception:
        return False


def _first(src: dict[str, Any], *names: str) -> Any:
    lower_map = {str(k).strip().lower(): v for k, v in src.items()}
    for name in names:
        if name in src and not _is_missing(src.get(name)):
            return src.get(name)
        key = str(name).strip().lower()
        if key in lower_map and not _is_missing(lower_map.get(key)):
            return lower_map.get(key)
    return None


def _as_float(value: Any) -> float | None:
    if _is_missing(value) or isinstance(value, bool):
        return None
    if isinstance(value, str):
        raw = value.strip().replace(",", "")
        if not raw:
            return None
        if raw.endswith("%"):
            try:
                return float(raw[:-1].strip()) / 100.0
            except Exception:
                return None
        value = raw
    try:
        return float(value)
    except Exception:
        return None


def _as_rate(value: Any) -> float | None:
    parsed = _as_float(value)
    if parsed is None:
        return None
    if abs(parsed) > 1.0 and abs(parsed) <= 100.0:
        return parsed / 100.0
    return parsed


def _as_int(value: Any) -> int | None:
    parsed = _as_float(value)
    if parsed is None:
        return None
    try:
        return int(parsed)
    except Exception:
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if _is_missing(value):
        return None
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "triggered", "close", "closed", "roll", "rolled"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "none", "null"}:
        return False
    return None


def _normalize_mode(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in {"put", "sell_put", "short_put", "cash_secured_put"}:
        return "put"
    if raw in {"call", "sell_call", "short_call"}:
        return "call"
    return None


def _split_reasons(value: Any) -> list[str]:
    if _is_missing(value):
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_split_reasons(item))
        return out
    raw = str(value).strip()
    if not raw:
        return []
    parts = [raw]
    for sep in (";", "|", ","):
        if sep in raw:
            parts = raw.replace("|", ";").replace(",", ";").split(";")
            break
    out: list[str] = []
    for part in parts:
        reason = str(part or "").strip()
        if reason and reason not in out:
            out.append(reason)
    return out


def _normalize_record(row: dict[str, Any], *, index: int) -> dict[str, Any]:
    mode = _normalize_mode(
        _first(row, "mode", "strategy", "side", "option_type", "option_strategy")
    )
    actual_return = _as_rate(
        _first(
            row,
            "actual_return",
            "realized_return",
            "realized_return_pct",
            "pnl_pct",
            "final_return",
            "outcome_return",
        )
    )
    predicted_return = _as_rate(
        _first(
            row,
            "predicted_return",
            "expected_return",
            "annualized_return",
            "annualized_net_return",
            "annualized_net_return_on_cash_basis",
            "annualized_net_premium_return",
            "candidate_predicted_return",
        )
    )
    delta = _as_rate(_first(row, "delta", "current_delta", "abs_delta"))
    max_drawdown = _as_rate(
        _first(row, "max_drawdown", "mdd", "max_unrealized_loss_pct", "max_adverse_return")
    )
    filter_reasons = _split_reasons(
        _first(row, "filter_reason", "filter_reasons", "reject_rule", "reject_reason", "engine_reject_reason", "filters_failed")
    )
    accepted = _as_bool(_first(row, "accepted", "selected", "notified", "executed", "passed_filter"))
    return {
        "row_id": str(_first(row, "row_id", "candidate_id", "contract_symbol", "option_symbol") or f"row-{index}"),
        "symbol": str(_first(row, "symbol", "underlying", "ticker") or "").strip().upper() or None,
        "account": str(_first(row, "account", "account_label") or "").strip().lower() or None,
        "mode": mode,
        "contract_symbol": str(_first(row, "contract_symbol", "option_symbol") or "").strip() or None,
        "expiration": str(_first(row, "expiration", "exp") or "").strip() or None,
        "dte": _as_int(_first(row, "dte", "DTE", "days_to_expiration", "remaining_dte")),
        "delta": delta,
        "abs_delta": abs(delta) if delta is not None else None,
        "predicted_return": predicted_return,
        "actual_return": actual_return,
        "actual_pnl": _as_float(_first(row, "actual_pnl", "realized_pnl", "pnl", "profit")),
        "max_drawdown": max_drawdown,
        "max_drawdown_loss": abs(max_drawdown) if max_drawdown is not None else None,
        "close_triggered": _as_bool(_first(row, "close_triggered", "triggered_close", "closed_by_advice", "take_profit_triggered")),
        "roll_triggered": _as_bool(_first(row, "roll_triggered", "triggered_roll", "rolled")),
        "accepted": accepted,
        "filter_reasons": filter_reasons,
        "raw": dict(row),
    }


def normalize_strategy_replay_rows(rows: list[dict[str, Any]] | Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError("strategy replay rows must be a list")
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        if isinstance(row, dict):
            out.append(_normalize_record(row, index=idx))
    return out


def read_strategy_replay_file(path: Path) -> list[dict[str, Any]]:
    source = Path(path).expanduser()
    if not source.exists():
        raise FileNotFoundError(str(source))
    suffix = source.suffix.lower()
    if suffix == ".csv":
        with source.open("r", encoding="utf-8", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh) if isinstance(row, dict)]
    if suffix == ".jsonl":
        rows: list[dict[str, Any]] = []
        with source.open("r", encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw:
                    continue
                item = json.loads(raw)
                if isinstance(item, dict):
                    rows.append(item)
        return rows
    payload = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "records", "candidates"):
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
    raise ValueError(f"unsupported strategy replay payload: {source.name}")


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _bucket_label(low: int | float, high: int | float | None, *, kind: str) -> str:
    if high is None:
        return f"{low}+"
    if kind == "dte":
        return f"{low}-{high}"
    return f"{float(low):.2f}-{float(high):.2f}"


def _bucket_match(value: float | int | None, buckets: tuple[tuple[Any, Any], ...]) -> tuple[Any, Any] | None:
    if value is None:
        return None
    for low, high in buckets:
        if high is None:
            if value >= low:
                return low, high
            continue
        if value >= low and value <= high:
            return low, high
    return None


def _bad_outcome(row: dict[str, Any], *, win_return_threshold: float, bad_drawdown_loss: float) -> bool | None:
    actual = row.get("actual_return")
    drawdown = row.get("max_drawdown_loss")
    has_signal = actual is not None or drawdown is not None or row.get("close_triggered") is not None or row.get("roll_triggered") is not None
    if not has_signal:
        return None
    if actual is not None and float(actual) <= float(win_return_threshold):
        return True
    if drawdown is not None and float(drawdown) >= float(bad_drawdown_loss):
        return True
    if bool(row.get("roll_triggered")):
        return True
    return False


def _summarize_group(
    rows: list[dict[str, Any]],
    *,
    min_sample: int,
    win_return_threshold: float,
    bad_drawdown_loss: float,
) -> dict[str, Any]:
    actuals = [float(row["actual_return"]) for row in rows if row.get("actual_return") is not None]
    predicted = [float(row["predicted_return"]) for row in rows if row.get("predicted_return") is not None]
    drawdowns = [float(row["max_drawdown_loss"]) for row in rows if row.get("max_drawdown_loss") is not None]
    bad_flags = [
        value
        for value in (_bad_outcome(row, win_return_threshold=win_return_threshold, bad_drawdown_loss=bad_drawdown_loss) for row in rows)
        if value is not None
    ]
    outcome_count = len(actuals)
    close_known = [row for row in rows if row.get("close_triggered") is not None]
    roll_known = [row for row in rows if row.get("roll_triggered") is not None]
    avg_actual = _avg(actuals)
    avg_drawdown = _avg(drawdowns)
    risk_adjusted = None
    if avg_actual is not None:
        risk_adjusted = avg_actual - float(avg_drawdown or 0.0)
    if outcome_count <= 0:
        confidence = "missing_outcomes"
    elif outcome_count < int(min_sample):
        confidence = "low_sample"
    else:
        confidence = "ok"
    return {
        "sample_count": len(rows),
        "outcome_count": outcome_count,
        "confidence": confidence,
        "avg_predicted_return": _avg(predicted),
        "avg_actual_return": avg_actual,
        "win_rate": _rate(sum(1 for value in actuals if value > float(win_return_threshold)), outcome_count),
        "avg_max_drawdown_loss": avg_drawdown,
        "bad_outcome_rate": _rate(sum(1 for value in bad_flags if value), len(bad_flags)),
        "close_trigger_rate": _rate(sum(1 for row in close_known if bool(row.get("close_triggered"))), len(close_known)),
        "roll_trigger_rate": _rate(sum(1 for row in roll_known if bool(row.get("roll_triggered"))), len(roll_known)),
        "risk_adjusted_return": risk_adjusted,
    }


def _rank_bucket_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda item: (
            0 if item.get("confidence") == "ok" else 1,
            -(float(item.get("risk_adjusted_return")) if item.get("risk_adjusted_return") is not None else -10**9),
            -(float(item.get("win_rate")) if item.get("win_rate") is not None else -1.0),
            -int(item.get("outcome_count") or 0),
        ),
    )


def _rank_bucket_rows_by_win_rate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda item: (
            0 if item.get("confidence") == "ok" else 1,
            -(float(item.get("win_rate")) if item.get("win_rate") is not None else -1.0),
            -(float(item.get("risk_adjusted_return")) if item.get("risk_adjusted_return") is not None else -10**9),
            -int(item.get("outcome_count") or 0),
        ),
    )


def _bucket_analysis(
    rows: list[dict[str, Any]],
    *,
    field: str,
    buckets: tuple[tuple[Any, Any], ...],
    kind: str,
    min_sample: int,
    win_return_threshold: float,
    bad_drawdown_loss: float,
) -> dict[str, Any]:
    grouped: dict[tuple[Any, Any], list[dict[str, Any]]] = {bucket: [] for bucket in buckets}
    missing = 0
    for row in rows:
        bucket = _bucket_match(row.get(field), buckets)
        if bucket is None:
            missing += 1
            continue
        grouped.setdefault(bucket, []).append(row)
    bucket_rows: list[dict[str, Any]] = []
    for low, high in buckets:
        members = grouped.get((low, high), [])
        if not members:
            continue
        summary = _summarize_group(
            members,
            min_sample=min_sample,
            win_return_threshold=win_return_threshold,
            bad_drawdown_loss=bad_drawdown_loss,
        )
        bucket_rows.append(
            {
                "range": _bucket_label(low, high, kind=kind),
                "min": low,
                "max": high,
                **summary,
            }
        )
    ranked = _rank_bucket_rows(bucket_rows)
    ranked_by_win_rate = _rank_bucket_rows_by_win_rate(bucket_rows)
    return {
        "buckets": bucket_rows,
        "best_ranges": ranked[:3],
        "best_win_rate_ranges": ranked_by_win_rate[:3],
        "missing_value_count": missing,
    }


def _symbol_analysis(
    rows: list[dict[str, Any]],
    *,
    min_sample: int,
    win_return_threshold: float,
    bad_drawdown_loss: float,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        grouped.setdefault(symbol, []).append(row)
    observed = [float(row["actual_return"]) for row in rows if row.get("actual_return") is not None]
    overall_return = _avg(observed) or 0.0
    out: list[dict[str, Any]] = []
    for symbol, members in grouped.items():
        summary = _summarize_group(
            members,
            min_sample=min_sample,
            win_return_threshold=win_return_threshold,
            bad_drawdown_loss=bad_drawdown_loss,
        )
        avg_actual = summary.get("avg_actual_return")
        avg_drawdown = summary.get("avg_max_drawdown_loss")
        high_return_drawdown_bad = (
            summary.get("confidence") == "ok"
            and avg_actual is not None
            and avg_drawdown is not None
            and float(avg_actual) >= float(overall_return)
            and float(avg_drawdown) >= float(bad_drawdown_loss)
        )
        out.append(
            {
                "symbol": symbol,
                "high_return_drawdown_bad": bool(high_return_drawdown_bad),
                **summary,
            }
        )
    return sorted(
        out,
        key=lambda item: (
            not bool(item.get("high_return_drawdown_bad")),
            -(float(item.get("avg_actual_return")) if item.get("avg_actual_return") is not None else -10**9),
            -(float(item.get("avg_max_drawdown_loss")) if item.get("avg_max_drawdown_loss") is not None else -1.0),
        ),
    )


def _filter_analysis(
    rows: list[dict[str, Any]],
    *,
    min_sample: int,
    win_return_threshold: float,
    bad_drawdown_loss: float,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for reason in row.get("filter_reasons") or []:
            grouped.setdefault(str(reason), []).append(row)
    accepted_baseline = [
        row
        for row in rows
        if row.get("actual_return") is not None and (row.get("accepted") is True or not row.get("filter_reasons"))
    ]
    if not accepted_baseline:
        accepted_baseline = [row for row in rows if row.get("actual_return") is not None]
    baseline = _summarize_group(
        accepted_baseline,
        min_sample=1,
        win_return_threshold=win_return_threshold,
        bad_drawdown_loss=bad_drawdown_loss,
    )
    baseline_return = baseline.get("avg_actual_return")
    baseline_bad_rate = baseline.get("bad_outcome_rate")
    out: list[dict[str, Any]] = []
    for reason, members in grouped.items():
        summary = _summarize_group(
            members,
            min_sample=min_sample,
            win_return_threshold=win_return_threshold,
            bad_drawdown_loss=bad_drawdown_loss,
        )
        avg_actual = summary.get("avg_actual_return")
        bad_rate = summary.get("bad_outcome_rate")
        return_gap = None
        if baseline_return is not None and avg_actual is not None:
            return_gap = float(baseline_return) - float(avg_actual)
        bad_excess = None
        if baseline_bad_rate is not None and bad_rate is not None:
            bad_excess = float(bad_rate) - float(baseline_bad_rate)
        value_score = None
        if return_gap is not None or bad_excess is not None:
            value_score = float(return_gap or 0.0) + 0.10 * float(bad_excess or 0.0)
        out.append(
            {
                "filter": reason,
                "status": "valuable" if summary.get("confidence") == "ok" and value_score is not None and value_score > 0 else summary.get("confidence"),
                "value_score": value_score,
                "return_gap_vs_baseline": return_gap,
                "bad_outcome_excess_vs_baseline": bad_excess,
                "baseline_avg_actual_return": baseline_return,
                "baseline_bad_outcome_rate": baseline_bad_rate,
                **summary,
            }
        )
    return sorted(
        out,
        key=lambda item: (
            0 if item.get("status") == "valuable" else 1,
            -(float(item.get("value_score")) if item.get("value_score") is not None else -10**9),
            -int(item.get("outcome_count") or 0),
        ),
    )


def _learning_suggestions(dte: dict[str, Any], delta: dict[str, Any]) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    best_dte = next((item for item in dte.get("best_ranges") or [] if item.get("confidence") == "ok"), None)
    if best_dte:
        suggestions.append(
            {
                "scope": "candidate_window",
                "parameter": "dte_range",
                "suggested_min_dte": best_dte.get("min"),
                "suggested_max_dte": best_dte.get("max"),
                "basis": "highest risk_adjusted_return among observed DTE buckets",
                "apply_mode": "shadow_dry_run_only",
            }
        )
    best_delta = next((item for item in delta.get("best_win_rate_ranges") or [] if item.get("confidence") == "ok"), None)
    if best_delta:
        suggestions.append(
            {
                "scope": "candidate_risk",
                "parameter": "abs_delta_range",
                "suggested_min_abs_delta": best_delta.get("min"),
                "suggested_max_abs_delta": best_delta.get("max"),
                "basis": "highest win_rate among observed Delta buckets",
                "apply_mode": "shadow_dry_run_only",
            }
        )
    return suggestions


def analyze_strategy_replay(
    rows: list[dict[str, Any]] | Any,
    *,
    min_sample: int = 5,
    win_return_threshold: float = 0.0,
    bad_drawdown_threshold: float = -0.15,
) -> dict[str, Any]:
    min_sample = max(1, int(min_sample or 1))
    bad_drawdown_loss = abs(float(bad_drawdown_threshold))
    win_return_threshold = float(win_return_threshold)
    normalized = normalize_strategy_replay_rows(rows)
    outcome_rows = [row for row in normalized if row.get("actual_return") is not None]
    rejected_rows = [row for row in normalized if row.get("filter_reasons")]
    rejected_outcome_rows = [row for row in rejected_rows if row.get("actual_return") is not None]

    dte = _bucket_analysis(
        normalized,
        field="dte",
        buckets=DEFAULT_DTE_BUCKETS,
        kind="dte",
        min_sample=min_sample,
        win_return_threshold=win_return_threshold,
        bad_drawdown_loss=bad_drawdown_loss,
    )
    delta = _bucket_analysis(
        normalized,
        field="abs_delta",
        buckets=DEFAULT_DELTA_BUCKETS,
        kind="delta",
        min_sample=min_sample,
        win_return_threshold=win_return_threshold,
        bad_drawdown_loss=bad_drawdown_loss,
    )
    filters = _filter_analysis(
        normalized,
        min_sample=min_sample,
        win_return_threshold=win_return_threshold,
        bad_drawdown_loss=bad_drawdown_loss,
    )
    warnings: list[str] = []
    if not rejected_rows:
        warnings.append("no_filter_reasons: filter value needs rejected/shadow candidate rows")
    elif not rejected_outcome_rows:
        warnings.append("no_rejected_outcomes: filter value needs shadow outcomes for rejected candidates")
    if len(outcome_rows) < min_sample:
        warnings.append("low_outcome_sample: parameter learning is advisory only")

    return {
        "summary": {
            "row_count": len(normalized),
            "outcome_count": len(outcome_rows),
            "rejected_count": len(rejected_rows),
            "rejected_outcome_count": len(rejected_outcome_rows),
            "min_sample": min_sample,
            "win_return_threshold": win_return_threshold,
            "bad_drawdown_loss_threshold": bad_drawdown_loss,
        },
        "dte_effectiveness": dte,
        "delta_effectiveness": delta,
        "symbol_risk_return": _symbol_analysis(
            normalized,
            min_sample=min_sample,
            win_return_threshold=win_return_threshold,
            bad_drawdown_loss=bad_drawdown_loss,
        ),
        "filter_value": filters,
        "dry_run_config_suggestions": _learning_suggestions(dte, delta),
        "warnings": warnings,
    }
