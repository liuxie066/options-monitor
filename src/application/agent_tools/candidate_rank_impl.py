from __future__ import annotations

from itertools import zip_longest
from pathlib import Path
from typing import Any, Callable

from domain.domain.engine import normalize_strategy_mode
from src.application.agent_tool_contracts import AgentToolError
from src.application.candidate_snapshot_manifest import (
    CandidateSnapshotManifestError,
    load_candidate_snapshot_bundle_readonly,
    load_latest_candidate_snapshot_bundle_readonly,
)
from src.application.opening_candidate_snapshot import ranked_opening_candidates
from src.application.runtime_paths import resolve_runtime_root
from src.application.agent_tools.candidate_filter_impl import _candidate_page
from src.application.agent_tools.project_reader import ProjectReaderError, decode_cursor, digest


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
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    account = str(payload.get("account") or "").strip().lower()
    if not account:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="account is required",
            hint="Pass the logical account; run_id is optional.",
        )
    base = resolve_runtime_root(
        repo_root=repo_base(),
        runtime_root=payload.get("runtime_root"),
    ).runtime_root
    try:
        if str(payload.get("run_id") or "").strip():
            bundle = load_candidate_snapshot_bundle_readonly(
                base=base,
                run_id=str(payload["run_id"]).strip(),
                account=account,
            )
        else:
            bundle = load_latest_candidate_snapshot_bundle_readonly(
                base=base,
                account=account,
            )
        snapshot = (bundle.get("owners") or {}).get("opening")
        if not isinstance(snapshot, dict):
            raise CandidateSnapshotManifestError(
                "manifest-bound opening candidate snapshot is unavailable"
            )
        manifest = bundle.get("manifest")
        if not isinstance(manifest, dict):
            raise CandidateSnapshotManifestError(
                "candidate snapshot manifest is unavailable"
            )
        return snapshot, manifest, dict(bundle.get("source_selection") or {})
    except CandidateSnapshotManifestError as exc:
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message=str(exc),
            hint="Query an available explicit run or use notification_perception_read for the delivered report source; unchanged retries cannot restore missing history.",
            details={"account": account, "run_id": getattr(exc, "run_id", None) or payload.get("run_id"),
                     "reason": "candidate_snapshot_unavailable", "retryable": False},
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
    snapshot, manifest, selection = _snapshot(payload, repo_base=repo_base)
    requested_market = payload.get("_market")
    markets = set(manifest.get("markets") or [])
    if requested_market and (not markets or markets - {requested_market}):
        raise AgentToolError(code="PERMISSION_DENIED", message="candidate source market is outside the configured scope")
    modes = ["put", "call"] if mode_filter == "all" else [mode_filter]
    groups: list[dict[str, Any]] = []
    for mode in modes:
        source_rows = ranked_opening_candidates(snapshot, mode=mode)
        ranked: list[dict[str, Any]] = []
        for item in source_rows:
            explanation = dict(item.get("ranking") or {})
            facts = dict(item.get("facts") or item)
            explanation.update(
                {
                    "mode": mode,
                    "candidate_id": item.get("candidate_id"),
                    "rank": item.get("rank"),
                    "symbol": facts.get("symbol"),
                    "contract_symbol": facts.get("contract_symbol"),
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
        if not strategy_result and snapshot.get("scan_mode") == "experience":
            strategy_scope = next(
                (
                    dict(item)
                    for item in snapshot.get("scope_results") or []
                    if isinstance(item, dict) and item.get("strategy_mode") == mode
                ),
                {},
            )
            strategy_result = {
                "strategy_status": strategy_scope.get("status"),
                "capacity_status": "demo_scenario",
            }
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
    # Group-local recorded order stays unchanged; alternate modes so a legacy
    # all-mode preview retains both put and call candidates.
    grouped_rows = [[{"mode": group["mode"], **item} for item in group["ranked"]] for group in groups]
    all_rows = [item for pair in zip_longest(*grouped_rows) for item in pair if item is not None]
    symbol = str(payload.get("symbol") or "").strip().upper()
    matched = [item for item in all_rows if not symbol or str(item.get("symbol") or "").upper() == symbol]
    root = resolve_runtime_root(repo_root=repo_base(), runtime_root=payload.get("runtime_root")).runtime_root
    page_payload = {**payload, "runtime_root": str(root)}
    page_limit = top_n * len(modes)
    if "limit" not in payload:
        binding = digest({"tool": "candidate_rank_explain", "query": {
            key: value for key, value in page_payload.items() if key != "cursor"}})
        try:
            state = decode_cursor(payload.get("cursor"), binding)
        except ProjectReaderError as exc:
            raise AgentToolError(code="INPUT_ERROR", message=exc.code,
                hint="Repeat the query without cursor after source changes; narrow symbol/function/rule for oversized details.",
                details={"reason": exc.code}) from None
        start = state.get("index", 0) if state else 0
        if type(start) is int and 0 <= start <= len(matched):
            # Keep each mode's legacy quota, including when another mode is
            # exhausted. The shared pager advances from this same prefix.
            counts = dict.fromkeys(modes, 0)
            page_limit = 0
            for item in matched[start:start + top_n * len(modes)]:
                if counts[item["mode"]] == top_n:
                    break
                counts[item["mode"]] += 1
                page_limit += 1
            page_limit = max(1, page_limit)
    page, page_meta = _candidate_page(matched, page_payload,
        tool="candidate_rank_explain", source={"snapshot": snapshot.get("content_sha256"),
        "manifest": manifest.get("content_sha256"), "run_id": snapshot.get("run_id"), "market": snapshot.get("market")}, default=page_limit, maximum=100 * len(modes))
    page_meta["source_run_id"] = snapshot.get("run_id")
    page_meta["freshness"] = {"kind": "historical", "as_of": snapshot.get("sealed_at_utc")}
    page_meta["pagination"]["total_count"] = len(all_rows)
    for group in groups:
        group["ranked"] = [{k: v for k, v in item.items() if k != "mode"}
                           for item in page if item["mode"] == group["mode"]]
    ranked_flat = [item for group in groups for item in group["ranked"]]
    source = {
        "path": mask_path("state/opening_candidate_snapshot.json"),
        "run_id": snapshot.get("run_id"),
        "account": snapshot.get("account"),
        "content_sha256": snapshot.get("content_sha256"),
        "manifest_content_sha256": manifest.get("content_sha256"),
        "authority": "terminal_manifest_bound_opening_candidate_snapshot",
        "scan_mode": snapshot.get("scan_mode"),
        "account_display_name": snapshot.get("account_display_name"),
        "executable": snapshot.get("executable"),
    }
    returned = len(ranked_flat)
    total = sum(int(group["row_count"]) for group in groups)
    model_fields = ("mode", "rank", "symbol", "contract_symbol", "period_net_return", "annualized_return", "net_income", "primary_drivers")
    data = {
        "narrowing_hint": (
            "结果超过证据预算，请指定 mode=put 或 call，并使用 top_n=1。" if mode_filter == "all"
            else "结果超过证据预算，请使用 top_n=1。" if top_n > 1
            else "这条排名记录仍超过证据预算；此工具没有可进一步缩小该记录的参数，当前无法完整解释。"
        ),
        "ranked_summary": [{key: row.get(key) for key in model_fields} for row in ranked_flat],
        "ranking_summary": [
            {"mode": group["mode"], "strategy_status": group["strategy_status"], "capacity_status": group["capacity_status"],
             "rank_reason": group["ranked"][0].get("rank_reason") if group["ranked"] else None}
            for group in groups
        ],
        "returned_count": returned,
        "coverage": {"status": "complete", "complete_for": "requested_page", "included_count": returned,
                     "total_count": total, "omitted_count": total - returned, "has_more": total > returned},
        "freshness": {"status": "historical", "as_of": manifest.get("sealed_at_utc")},
        "scope": {"run_id": snapshot.get("run_id"), "account": snapshot.get("account"),
                  "market": snapshot.get("market"), "mode": mode_filter, "top_n": top_n},
        "source": {"run_id": snapshot.get("run_id"), "account": snapshot.get("account"),
                   "selector": "explicit_run_id" if str(payload.get("run_id") or "").strip() else "latest", **selection},
        "mode": mode_filter,
        "top_n": top_n,
        **page_meta,
        "opening_status": snapshot.get("opening_status"),
        "groups": groups,
        "ranked": ranked_flat,
        "row_count": sum(int(group["row_count"]) for group in groups),
    }
    return data, [], {"source_files": [source]}
