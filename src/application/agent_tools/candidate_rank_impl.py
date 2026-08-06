from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from domain.domain.engine import normalize_strategy_mode
from src.application.agent_tool_contracts import AgentToolError
from src.application.opening_candidate_snapshot import (
    OpeningCandidateSnapshotError,
    load_latest_opening_candidate_snapshot,
    load_opening_candidate_snapshot,
    ranked_opening_candidates,
)


def _as_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(low, min(high, parsed))


def _mode(value: Any) -> str:
    mode = str(value or "all").strip().lower()
    return mode if mode == "all" else normalize_strategy_mode(mode)


def _snapshot(
    payload: dict[str, Any],
    *,
    repo_base: Callable[[], Path],
) -> dict[str, Any]:
    account = str(payload.get("account") or "").strip().lower()
    if not account:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="account is required",
            hint="Pass the logical account; run_id is optional.",
        )
    base = Path(payload.get("runtime_root") or repo_base()).resolve()
    try:
        if str(payload.get("run_id") or "").strip():
            return load_opening_candidate_snapshot(
                base=base,
                run_id=str(payload["run_id"]).strip(),
                account=account,
            )
        return load_latest_opening_candidate_snapshot(base=base, account=account)
    except OpeningCandidateSnapshotError as exc:
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message=str(exc),
            details={"account": account, "run_id": payload.get("run_id")},
        ) from exc


def candidate_rank_explain_tool(
    payload: dict[str, Any],
    *,
    repo_base: Callable[[], Path],
    resolve_output_root: Callable[[Any], Path],
    mask_path: Callable[[Any], str | None],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Explain the sealed order without re-ranking recorded candidates."""

    _ = resolve_output_root
    mode_filter = _mode(payload.get("mode"))
    top_n = _as_int(payload.get("top_n"), default=10, low=1, high=100)
    snapshot = _snapshot(payload, repo_base=repo_base)
    modes = ["put", "call"] if mode_filter == "all" else [mode_filter]
    groups: list[dict[str, Any]] = []
    for mode in modes:
        source_rows = ranked_opening_candidates(snapshot, mode=mode)
        ranked: list[dict[str, Any]] = []
        for item in source_rows[:top_n]:
            explanation = dict(item.get("ranking") or {})
            explanation.update(
                {
                    "candidate_id": item.get("candidate_id"),
                    "rank": item.get("rank"),
                    "symbol": dict(item.get("facts") or {}).get("symbol"),
                    "contract_symbol": dict(item.get("facts") or {}).get(
                        "contract_symbol"
                    ),
                    "source_file": "state/opening_candidate_snapshot.json",
                }
            )
            ranked.append(explanation)
        strategy_result = next(
            (
                dict(item)
                for item in snapshot.get("strategy_results") or []
                if isinstance(item, dict) and item.get("strategy_mode") == mode
            ),
            {},
        )
        groups.append(
            {
                "mode": mode,
                "ranking_policy": "opening_candidate_snapshot",
                "strategy_status": strategy_result.get("strategy_status"),
                "capacity_status": strategy_result.get("capacity_status"),
                "row_count": len(source_rows),
                "ranked": ranked,
            }
        )
    ranked_flat = [item for group in groups for item in group["ranked"]]
    source = {
        "path": mask_path("state/opening_candidate_snapshot.json"),
        "run_id": snapshot.get("run_id"),
        "account": snapshot.get("account"),
        "content_sha256": snapshot.get("content_sha256"),
        "authority": "sealed_opening_candidate_snapshot",
    }
    data = {
        "mode": mode_filter,
        "top_n": top_n,
        "opening_status": snapshot.get("opening_status"),
        "groups": groups,
        "ranked": ranked_flat,
        "row_count": sum(int(group["row_count"]) for group in groups),
    }
    return data, [], {"source_files": [source]}
