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
    )


def _weight_payload(weights: CandidateScoreWeights | None) -> dict[str, float]:
    actual = weights or CandidateScoreWeights()
    return {
        "annualized_return": float(actual.annualized_return),
        "net_income": float(actual.net_income),
        "liquidity": float(actual.liquidity),
        "risk_distance": float(actual.risk_distance),
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
    if payload.get("output_dir"):
        return [(resolve_output_root(payload.get("output_dir")) / "reports").resolve()]
    candidates = [
        (base / "output" / "reports").resolve(),
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
        score_weights=CandidateScoreWeights(annualized_return=1.0, net_income=0.0, liquidity=0.0, risk_distance=0.0),
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
    ranked_rows = rank_candidate_rows(rows, mode=mode, score_weights=score_weights)
    ranked: list[dict[str, Any]] = []
    for idx, row in enumerate(ranked_rows[:top_n], start=1):
        explanation = explain_candidate_rank(row, mode=mode, score_weights=score_weights)
        explanation["rank"] = idx
        explanation["source_file"] = mask_path(row.get("_rank_explain_source_file"))
        ranked.append(explanation)
    out = {
        "mode": mode,
        "row_count": len(rows),
        "ranked": ranked,
    }
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
    groups = [
        _explain_group(
            rows_by_mode.get(mode, []),
            mode=mode,
            score_weights=score_weights,
            top_n=top_n,
            compare_baseline=compare_baseline,
            mask_path=mask_path,
        )
        for mode in modes
        if rows_by_mode.get(mode)
    ]
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
