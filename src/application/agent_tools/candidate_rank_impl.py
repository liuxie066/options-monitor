from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Callable

from domain.domain.engine import (
    CandidateScoreWeights,
    explain_candidate_rank,
    normalize_strategy_mode,
    rank_candidate_rows,
)
from domain.domain.insurance_underwriting import (
    INSURANCE_UNDERWRITING_PROFILE,
    InsuranceUnderwritingConfig,
    rank_underwriting_candidates,
)
from src.application.agent_tool_contracts import AgentToolError


def _as_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(low, min(high, parsed))


def _as_float(value: Any, *, field: str) -> float:
    try:
        return float(value)
    except Exception as exc:
        raise AgentToolError(code="INPUT_ERROR", message=f"{field} must be numeric") from exc


def _score_weights_from_payload(payload: dict[str, Any]) -> CandidateScoreWeights | None:
    raw = payload.get("score_weights")
    if raw in (None, ""):
        return None
    if not isinstance(raw, dict):
        raise AgentToolError(code="INPUT_ERROR", message="score_weights must be an object")
    return CandidateScoreWeights(
        annualized_return=_as_float(raw.get("annualized_return", 1.0), field="score_weights.annualized_return"),
        net_income=_as_float(raw.get("net_income", 1e-6), field="score_weights.net_income"),
        liquidity=_as_float(raw.get("liquidity", 0.0), field="score_weights.liquidity"),
        risk_distance=_as_float(raw.get("risk_distance", 0.0), field="score_weights.risk_distance"),
        vol_edge=_as_float(raw.get("vol_edge", 0.0), field="score_weights.vol_edge"),
        delta_target=_as_float(raw.get("delta_target", 0.0), field="score_weights.delta_target"),
        concentration=_as_float(raw.get("concentration", 0.0), field="score_weights.concentration"),
        path_risk=_as_float(raw.get("path_risk", 0.0), field="score_weights.path_risk"),
    )


def _weight_payload(weights: CandidateScoreWeights | None) -> dict[str, float]:
    actual = weights or CandidateScoreWeights()
    return {
        "annualized_return": float(actual.annualized_return),
        "net_income": float(actual.net_income),
        "liquidity": float(actual.liquidity),
        "risk_distance": float(actual.risk_distance),
        "vol_edge": float(actual.vol_edge),
        "delta_target": float(actual.delta_target),
        "concentration": float(actual.concentration),
        "path_risk": float(actual.path_risk),
    }


def _resolve_path(value: Any, *, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _normalize_mode_filter(value: Any) -> str:
    mode = str(value or "all").strip().lower()
    if mode == "all":
        return mode
    return normalize_strategy_mode(mode)


def _infer_mode_from_path(path: Path, *, fallback: str) -> str:
    name = path.name.lower()
    if "sell_call" in name:
        return "call"
    if "sell_put" in name:
        return "put"
    if fallback == "all":
        raise AgentToolError(
            code="INPUT_ERROR",
            message="candidate_path mode is ambiguous; pass mode=put or mode=call",
        )
    return normalize_strategy_mode(fallback)


def _default_report_dirs(
    payload: dict[str, Any],
    *,
    repo_base: Callable[[], Path],
    resolve_output_root: Callable[[Any], Path],
) -> list[Path]:
    base = repo_base()
    if payload.get("report_dir"):
        return [_resolve_path(payload["report_dir"], base=base)]
    run_dir = _run_dir_from_payload(payload, base=base)
    if run_dir is not None:
        account = str(payload.get("account") or "").strip().lower()
        if account:
            return [(run_dir / "accounts" / account).resolve()]
        dirs = [run_dir.resolve()]
        accounts_dir = run_dir / "accounts"
        if accounts_dir.exists() and accounts_dir.is_dir():
            dirs.extend(path.resolve() for path in sorted(accounts_dir.iterdir()) if path.is_dir())
        return dirs
    if payload.get("output_dir"):
        return [(resolve_output_root(payload.get("output_dir")) / "reports").resolve()]
    candidates = [
        (base / "output_shared" / "reports").resolve(),
        (resolve_output_root(None) / "reports").resolve(),
    ]
    out: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def _run_dir_from_payload(payload: dict[str, Any], *, base: Path) -> Path | None:
    if payload.get("run_dir"):
        return _resolve_path(payload.get("run_dir"), base=base)
    if payload.get("run_id"):
        return (base / "output_runs" / str(payload.get("run_id")).strip()).resolve()
    return None


def _candidate_paths_for_mode(report_dir: Path, *, mode: str) -> list[Path]:
    if mode == "put":
        exact = [
            report_dir / "sell_put_candidates_labeled.csv",
            report_dir / "sell_put_candidates.csv",
        ]
        patterns = ["*_sell_put_candidates_labeled.csv", "*_sell_put_candidates.csv"]
    else:
        exact = [report_dir / "sell_call_candidates.csv"]
        patterns = ["*_sell_call_candidates.csv"]
    existing_exact = [path for path in exact if path.exists()]
    if any(_has_csv_rows(path) for path in existing_exact):
        return existing_exact
    out: list[Path] = list(existing_exact)
    for pattern in patterns:
        for path in sorted(path for path in report_dir.glob(pattern) if path.exists()):
            if path not in out:
                out.append(path)
    return out


def _has_csv_rows(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            return any(True for _row in csv.DictReader(fh))
    except Exception:
        return False


def _source_paths(
    payload: dict[str, Any],
    *,
    mode_filter: str,
    repo_base: Callable[[], Path],
    resolve_output_root: Callable[[Any], Path],
) -> list[tuple[Path, str]]:
    base = repo_base()
    explicit: list[Any] = []
    if payload.get("candidate_path"):
        explicit.append(payload.get("candidate_path"))
    raw_paths = payload.get("candidate_paths")
    if isinstance(raw_paths, list):
        explicit.extend(raw_paths)
    if explicit:
        paths = [_resolve_path(value, base=base) for value in explicit if str(value or "").strip()]
        out: list[tuple[Path, str]] = []
        for path in paths:
            if not path.exists():
                raise AgentToolError(
                    code="DEPENDENCY_MISSING",
                    message=f"candidate CSV not found: {path.name}",
                    details={"candidate_path": str(path)},
                )
            out.append((path, _infer_mode_from_path(path, fallback=mode_filter)))
        return out

    modes = ("put", "call") if mode_filter == "all" else (mode_filter,)
    out = []
    seen: set[tuple[Path, str]] = set()
    for report_dir in _default_report_dirs(payload, repo_base=repo_base, resolve_output_root=resolve_output_root):
        for mode in modes:
            for path in _candidate_paths_for_mode(report_dir, mode=mode):
                key = (path.resolve(), mode)
                if key in seen:
                    continue
                seen.add(key)
                out.append((path.resolve(), mode))
    return out


def _read_rows(path: Path, *, mode: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            for idx, row in enumerate(csv.DictReader(fh), start=1):
                if not isinstance(row, dict):
                    continue
                item = dict(row)
                if not str(item.get("option_type") or "").strip():
                    item["option_type"] = mode
                item["_rank_explain_row_id"] = f"{path.resolve()}:{idx}"
                item["_rank_explain_source_file"] = str(path)
                rows.append(item)
    except Exception as exc:
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message=f"failed to read candidate CSV: {path.name}",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    return rows


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    try:
        import math

        if not math.isfinite(parsed):
            return None
    except Exception:
        return None
    return parsed


def _sort_number(value: Any) -> float:
    parsed = _number_or_none(value)
    return parsed if parsed is not None else 0.0


def _first_number(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        parsed = _number_or_none(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _row_uses_underwriting(row: dict[str, Any]) -> bool:
    underwriting_fields = {
        "insurance_underwriting_mode",
        "premium_edge_score",
        "strike_safety_margin_pct",
        "strike_upside_margin_pct",
    }
    profile = str(row.get("strategy_profile") or row.get("scan_strategy_profile") or "").strip().lower()
    if profile == INSURANCE_UNDERWRITING_PROFILE:
        return True
    return any(str(row.get(key) or "").strip() for key in underwriting_fields)


def _rows_use_underwriting(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and all(_row_uses_underwriting(row) for row in rows)


def _partition_rows_by_ranking_policy(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    by_policy: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        policy = INSURANCE_UNDERWRITING_PROFILE if _row_uses_underwriting(row) else "candidate_engine"
        bucket = by_policy.get(policy)
        if bucket is None:
            bucket = []
            by_policy[policy] = bucket
            groups.append(bucket)
        bucket.append(row)
    return groups


def _underwriting_margin_key(*, mode: str) -> str:
    return "strike_safety_margin_pct" if mode == "put" else "strike_upside_margin_pct"


def _rank_underwriting_rows_for_explain(rows: list[dict[str, Any]], *, mode: str) -> list[dict[str, Any]]:
    margin_key = _underwriting_margin_key(mode=mode)
    has_any_persisted_rank_field = any(
        _number_or_none(row.get("premium_edge_score")) is not None or _number_or_none(row.get(margin_key)) is not None
        for row in rows
    )
    if not has_any_persisted_rank_field:
        return rank_underwriting_candidates(rows, mode=mode, cfg=InsuranceUnderwritingConfig())

    rows_for_sort = [dict(row) for row in rows]
    if any(
        _number_or_none(row.get("premium_edge_score")) is None or _number_or_none(row.get(margin_key)) is None
        for row in rows_for_sort
    ):
        enriched = {
            str(row.get("_rank_explain_row_id")): row
            for row in rank_underwriting_candidates(rows_for_sort, mode=mode, cfg=InsuranceUnderwritingConfig())
        }
        for row in rows_for_sort:
            fallback = enriched.get(str(row.get("_rank_explain_row_id"))) or {}
            for key in (
                "strategy_profile",
                "insurance_underwriting_mode",
                "iv_rv_ratio",
                "iv_minus_rv",
                "premium_edge_score",
                margin_key,
            ):
                if not str(row.get(key) or "").strip() and key in fallback:
                    row[key] = fallback.get(key)

    return sorted(
        rows_for_sort,
        key=lambda item: (
            -_sort_number(item.get(margin_key)),
            -_sort_number(item.get("premium_edge_score")),
            _sort_number(item.get("spread_ratio")),
            -_sort_number(item.get("open_interest")),
            -_sort_number(_first_number(item, "net_income_cny", "net_income")),
        ),
    )


def _explain_underwriting_rank(row: dict[str, Any], *, mode: str) -> dict[str, Any]:
    mode_norm = normalize_strategy_mode(mode)
    margin_key = _underwriting_margin_key(mode=mode_norm)
    margin_label = "strike 安全距离" if mode_norm == "put" else "strike 上行距离"
    compensation_label = "去重补偿分"
    score_components = {
        "premium_edge_score": _sort_number(row.get("premium_edge_score")),
        margin_key: _sort_number(row.get(margin_key)),
        "net_income": _sort_number(_first_number(row, "net_income_cny", "net_credit", "net_income")),
        "spread_ratio": _sort_number(row.get("spread_ratio")),
    }
    score_inputs = {
        "annualized_return": _first_number(
            row,
            "annualized_net_return_on_cash_basis" if mode_norm == "put" else "annualized_net_premium_return",
            "annualized_return",
        ),
        "net_income": _first_number(row, "net_income_cny", "net_credit", "net_income"),
        "spread_ratio": _first_number(row, "spread_ratio"),
        "iv_rv_ratio": _first_number(row, "iv_rv_ratio"),
        "iv_minus_rv": _first_number(row, "iv_minus_rv"),
        margin_key: _first_number(row, margin_key),
    }
    return {
        "mode": mode_norm,
        "ranking_policy": INSURANCE_UNDERWRITING_PROFILE,
        "symbol": str(row.get("symbol") or "").strip().upper() or None,
        "contract_symbol": str(row.get("contract_symbol") or row.get("option_symbol") or "").strip() or None,
        "option_type": str(row.get("option_type") or ("put" if mode_norm == "put" else "call")).strip().lower() or None,
        "expiration": str(row.get("expiration") or "").strip() or None,
        "strike": _first_number(row, "strike"),
        "strategy_score": _sort_number(row.get("premium_edge_score")),
        "annualized_return": score_inputs["annualized_return"],
        "net_income": score_inputs["net_income"],
        "score_components": score_components,
        "score_component_labels": {
            "premium_edge_score": compensation_label,
            margin_key: margin_label,
            "net_income": "净收入",
            "spread_ratio": "价差",
        },
        "score_inputs": score_inputs,
        "score_warnings": [],
        "risk_notes": [],
        "primary_drivers": [margin_key, "premium_edge_score"],
        "primary_driver_labels": [margin_label, compensation_label],
        "rank_reason": f"按承保策略排序：先看{margin_label}，再看{compensation_label}，随后比较价差、未平仓量和净收入",
    }


def _baseline_changes(
    rows: list[dict[str, Any]],
    *,
    mode: str,
    ranked_rows: list[dict[str, Any]],
    top_n: int,
) -> list[dict[str, Any]]:
    baseline = rank_candidate_rows(
        rows,
        mode=mode,
        score_weights=CandidateScoreWeights(
            annualized_return=1.0,
            net_income=0.0,
            liquidity=0.0,
            risk_distance=0.0,
            vol_edge=0.0,
            delta_target=0.0,
            concentration=0.0,
        ),
    )
    old_rank = {str(row.get("_rank_explain_row_id")): idx for idx, row in enumerate(baseline, start=1)}
    changes: list[dict[str, Any]] = []
    for idx, row in enumerate(ranked_rows[:top_n], start=1):
        row_id = str(row.get("_rank_explain_row_id"))
        previous = old_rank.get(row_id)
        if previous is None or previous == idx:
            continue
        changes.append(
            {
                "symbol": str(row.get("symbol") or "").strip().upper() or None,
                "contract_symbol": str(row.get("contract_symbol") or row.get("option_symbol") or "").strip() or None,
                "new_rank": idx,
                "baseline_rank": previous,
                "rank_delta": previous - idx,
            }
        )
    return changes


def _explain_group(
    rows: list[dict[str, Any]],
    *,
    mode: str,
    score_weights: CandidateScoreWeights | None,
    top_n: int,
    compare_baseline: bool,
    mask_path: Callable[[Any], str | None],
) -> dict[str, Any]:
    uses_underwriting = _rows_use_underwriting(rows)
    if uses_underwriting:
        ranked_rows = _rank_underwriting_rows_for_explain(rows, mode=mode)
    else:
        ranked_rows = rank_candidate_rows(rows, mode=mode, score_weights=score_weights)
    ranked: list[dict[str, Any]] = []
    for idx, row in enumerate(ranked_rows[:top_n], start=1):
        if uses_underwriting:
            explanation = _explain_underwriting_rank(row, mode=mode)
        else:
            explanation = explain_candidate_rank(row, mode=mode, score_weights=score_weights)
        explanation["rank"] = idx
        explanation["source_file"] = mask_path(row.get("_rank_explain_source_file"))
        ranked.append(explanation)
    out = {
        "mode": mode,
        "ranking_policy": INSURANCE_UNDERWRITING_PROFILE if uses_underwriting else "candidate_engine",
        "row_count": len(rows),
        "ranked": ranked,
    }
    if uses_underwriting and score_weights is not None:
        out["score_weights_ignored"] = True
    if compare_baseline:
        out["baseline"] = {
            "name": "return_then_income",
            "changes": _baseline_changes(rows, mode=mode, ranked_rows=ranked_rows, top_n=top_n),
        }
    return out


def candidate_rank_explain_tool(
    payload: dict[str, Any],
    *,
    repo_base: Callable[[], Path],
    resolve_output_root: Callable[[Any], Path],
    mask_path: Callable[[Any], str | None],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    mode_filter = _normalize_mode_filter(payload.get("mode"))
    top_n = _as_int(payload.get("top_n"), default=10, low=1, high=100)
    score_weights = _score_weights_from_payload(payload)
    compare_baseline = bool(payload.get("compare_baseline", False))
    source_paths = _source_paths(
        payload,
        mode_filter=mode_filter,
        repo_base=repo_base,
        resolve_output_root=resolve_output_root,
    )
    if not source_paths:
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="candidate CSV not found",
            hint="Run scan_opportunities first, or pass candidate_path/report_dir explicitly.",
        )

    rows_by_mode: dict[str, list[dict[str, Any]]] = {"put": [], "call": []}
    source_files: list[dict[str, Any]] = []
    for path, mode in source_paths:
        rows = _read_rows(path, mode=mode)
        rows_by_mode.setdefault(mode, []).extend(rows)
        source_files.append({"mode": mode, "path": mask_path(path), "row_count": len(rows)})

    modes = ["put", "call"] if mode_filter == "all" else [mode_filter]
    groups = []
    for mode in modes:
        rows = rows_by_mode.get(mode, [])
        if not rows:
            continue
        for group_rows in _partition_rows_by_ranking_policy(rows):
            groups.append(
                _explain_group(
                    group_rows,
                    mode=mode,
                    score_weights=score_weights,
                    top_n=top_n,
                    compare_baseline=compare_baseline,
                    mask_path=mask_path,
                )
            )
    if not groups:
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="candidate CSV contains no rows for requested mode",
            details={"mode": mode_filter, "source_files": source_files},
        )

    ranked_flat = [item for group in groups for item in group["ranked"]]
    data = {
        "mode": mode_filter,
        "top_n": top_n,
        "score_weights": _weight_payload(score_weights),
        "source_files": source_files,
        "groups": groups,
        "ranked": ranked_flat,
        "row_count": sum(int(group["row_count"]) for group in groups),
    }
    return data, [], {"source_files": source_files}
