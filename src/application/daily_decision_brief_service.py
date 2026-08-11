from __future__ import annotations

import math
import json
from collections.abc import Mapping
from datetime import datetime, time, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from domain.domain.daily_decision_brief import (
    build_daily_brief_candidate_identity,
    normalize_daily_decision_brief,
)
from domain.domain.close_advice import (
    safe_int,
    select_close_advice_notification_rows,
)
from domain.domain.engine import (
    EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
    EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
)
from domain.domain.risk_capacity import compute_sell_call_share_capacity, compute_sell_put_cash_capacity
from domain.domain.cash_secured_utils import read_cash_secured_total_cny
from domain.domain.symbol_identity import canonical_symbol, symbol_market
from domain.storage import paths
from src.application.cash_totals import sum_by_currency_to_cny
from src.application.strategy_scan_failures import (
    ARTIFACT_NAME as STRATEGY_FAILURE_ARTIFACT_NAME,
    FAILURE_REASON as STRATEGY_FAILURE_REASON,
    read_strategy_scan_failures,
)
from src.application.multi_tick.misc import AccountResult
from src.application.opend_symbol_outputs import SUCCESS_EMPTY_REASON_CODES
from src.application.source_receipts import (
    sha256_bytes,
)
from src.application.prepared_portfolio_context import (
    PreparedPortfolioContextError,
    load_prepared_portfolio_context,
)
from src.application.prepared_portfolio_distribution import (
    PreparedPortfolioDistribution,
)
from src.application.opening_candidate_snapshot import (
    OpeningCandidateSnapshotError,
    candidate_universe_summary,
    load_opening_candidate_snapshot,
    ranked_opening_candidate_decisions,
    ranked_opening_candidates,
    validate_opening_candidate_snapshot,
)
from src.application.ai_decision_advice.orchestration import (
    run_or_reuse_ai_decision_advice,
    unavailable_brief_view,
)
from src.application.combo_yield_candidate_snapshot import (
    ComboYieldCandidateSnapshotError,
    load_combo_yield_candidate_snapshot,
)
from src.application.cc_lp_candidate_snapshot import (
    CcLpCandidateSnapshotError,
    load_cc_lp_candidate_snapshot,
)
from src.application.close_advice_report_manifest import (
    read_close_advice_report_snapshot,
)


_DEFAULT_MAX_CANDIDATES = 3
_DEFAULT_CLOSE_ADVICE_MAX_ITEMS_PER_ACCOUNT = 5
_MARKET_TIMEZONES = {"US": "America/New_York", "HK": "Asia/Hong_Kong", "CN": "Asia/Shanghai"}
_COMBO_OCCURRENCE_FIELDS = (
    "candidate_occurrence_schema",
    "candidate_occurrence_id",
    "candidate_occurrence_generated_at_utc",
    "candidate_occurrence_data_as_of_utc",
    "candidate_row_content_hash",
)


def assemble_daily_decision_brief(
    *,
    base: Path,
    run_id: str,
    account: str,
    market: str,
    scheduler_decision: Mapping[str, Any] | None,
    account_result: AccountResult | Mapping[str, Any],
    pipeline_succeeded: bool,
    config: Mapping[str, Any] | None,
    now_utc: datetime | None = None,
    opening_candidate_snapshot: Mapping[str, Any] | None = None,
    candidate_snapshot_unavailable_reason: str | None = None,
    prepared_portfolio_distribution: (
        PreparedPortfolioDistribution | Mapping[str, Any] | None
    ) = None,
    portfolio_distribution_unavailable_reason: str = (
        "portfolio_unavailable"
    ),
    prepared_option_positions_context: Mapping[str, Any] | None = None,
    option_positions_unavailable_reason: str = (
        "option_positions_unavailable"
    ),
) -> dict[str, Any]:
    """Assemble one market-qualified brief from structured run artifacts only."""

    base_path = Path(base).resolve()
    run_id_norm = str(run_id or "").strip()
    account_norm = str(account or "").strip().lower()
    market_norm = str(market or "").strip().upper()
    if not run_id_norm or not account_norm or not market_norm:
        raise ValueError("run_id, account, and market are required")

    config_map = dict(config or {})
    scheduler = dict(scheduler_decision or {})
    effective_now = _coerce_utc(now_utc)
    market_tz = ZoneInfo(_market_timezone(config_map, market_norm))
    now_market = effective_now.astimezone(market_tz)
    market_date = now_market.date().isoformat()
    valid_until = _valid_until_utc(config_map, market_norm, now_market)
    run_account_dir = paths.run_account_dir(base_path, run_id_norm, account_norm)
    state_dir = paths.run_account_state_dir(base_path, run_id_norm, account_norm)
    strategy_failure_path = run_account_dir / STRATEGY_FAILURE_ARTIFACT_NAME

    data_gaps: list[dict[str, Any]] = []
    source_artifacts: list[dict[str, Any]] = []
    max_candidates = _positive_int(
        _daily_brief_config(config_map).get("max_candidates_per_strategy"),
        default=_DEFAULT_MAX_CANDIDATES,
    )
    close_advice_max_items = _close_advice_max_items_per_account(config_map)

    (
        put_rows,
        put_available,
        call_rows,
        call_available,
        accepted_candidate_snapshot,
    ) = (
        _load_opening_candidate_families(
            base=base_path,
            run_id=run_id_norm,
            account=account_norm,
            market=market_norm,
            source_artifacts=source_artifacts,
            data_gaps=data_gaps,
            snapshot=opening_candidate_snapshot,
            unavailable_reason=candidate_snapshot_unavailable_reason,
        )
    )
    (
        combo_rows,
        combo_available,
        combo_snapshot_status,
    ) = _load_combo_yield_snapshot_family(
        base=base_path,
        run_id=run_id_norm,
        account=account_norm,
        market=market_norm,
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
    )
    _cc_lp_rows, _cc_lp_available = _load_cc_lp_snapshot_family(
        base=base_path,
        run_id=run_id_norm,
        account=account_norm,
        market=market_norm,
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
    )
    close_rows, close_available = _load_close_advice(
        path=run_account_dir / "close_advice.csv",
        run_account_dir=run_account_dir,
        market=market_norm,
        account=account_norm,
        run_id=run_id_norm,
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
    )

    strategy_status_index = _load_json_artifact(
        path=run_account_dir / "strategy_scan_status_index.v1.json",
        run_account_dir=run_account_dir,
        source_kind="strategy_scan_status_index",
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
        required=False,
    )
    indexed_strategy_statuses = _append_strategy_status_gaps(
        strategy_status_index,
        run_id=run_id_norm,
        account=account_norm,
        market=market_norm,
        data_gaps=data_gaps,
    )
    indexed_expected_families = {
        str(item.get("strategy_family") or "").strip().lower()
        for item in indexed_strategy_statuses
    }
    indexed_completed_families = {
        str(item.get("strategy_family") or "").strip().lower()
        for item in indexed_strategy_statuses
        if str(item.get("status") or "").strip().lower() == "completed"
    }
    indexed_combo_statuses = [
        item
        for item in indexed_strategy_statuses
        if str(item.get("strategy_family") or "").strip().lower()
        == "combo_yield"
    ]
    indexed_combo_partial = any(
        str(item.get("status") or "").strip().lower() == "completed"
        and str(item.get("reason") or "").strip().lower() == "partial_data"
        for item in indexed_combo_statuses
    )
    if "sell_put" in indexed_expected_families:
        put_available = "sell_put" in indexed_completed_families
    if "covered_call" in indexed_expected_families:
        call_available = "covered_call" in indexed_completed_families
    if "combo_yield" in indexed_expected_families:
        # The sealed Combo snapshot remains the candidate authority. The
        # optional status index may further downgrade it, never overwrite an
        # unavailable snapshot with a clean completed status.
        combo_available = (
            combo_available
            and "combo_yield" in indexed_completed_families
        )
    if combo_snapshot_status == "partial_data" and not indexed_combo_partial:
        data_gaps.append(
            {
                "scope": "strategy",
                "strategy_family": "combo_yield",
                "severity": "warning",
                "actionable": False,
                "reason": "opening_candidate_strategy_partial_data",
            }
        )
    elif (
        combo_snapshot_status
        and not combo_available
        and (
            not indexed_combo_statuses
            or "combo_yield" in indexed_completed_families
        )
    ):
        data_gaps.append(
            {
                "scope": "strategy",
                "strategy_family": "combo_yield",
                "reason": "opening_candidate_strategy_data_unavailable",
                "source_status": combo_snapshot_status,
            }
        )

    put_rows = _dedupe_rows(put_rows, family="sell_put")
    call_rows = _dedupe_rows(call_rows, family="covered_call")
    combo_rows = _dedupe_rows(combo_rows, family="combo_yield")
    close_rows = _dedupe_close_rows(close_rows)
    selected_close_rows = select_close_advice_notification_rows(
        close_rows,
        max_items_per_account=close_advice_max_items,
    )
    selected_close_row_ids = {id(row) for row in selected_close_rows}
    strategy_failures = _load_strategy_step_failures(
        failure_path=strategy_failure_path,
        run_account_dir=run_account_dir,
        account=account_norm,
        run_id=run_id_norm,
        market=market_norm,
        data_gaps=data_gaps,
    )
    failed_families = {item["strategy_family"] for item in strategy_failures}
    if "sell_put" in failed_families:
        put_rows, put_available = [], False
    if "covered_call" in failed_families:
        call_rows, call_available = [], False
    if "combo_yield" in failed_families:
        combo_rows, combo_available = [], False

    selected_puts = put_rows[:max_candidates]
    selected_calls = call_rows[:max_candidates]
    ranked_combos = combo_rows
    selected_combos = ranked_combos[:max_candidates]
    actions: list[dict[str, Any]] = []
    candidate_payloads: dict[str, list[dict[str, Any]]] = {
        "sell_put": [],
        "covered_call": [],
        "combo_yield": [],
    }
    positions: list[dict[str, Any]] = []
    capacity: dict[str, Any] = {}
    required_context_missing = 0
    required_context_rows = 0

    put_capacity_rows: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for rank, row in enumerate(selected_puts, start=1):
        cap = _sell_put_capacity(row)
        event_risk = _candidate_event_risk(
            row,
            family="sell_put",
            market_date=market_date,
        )
        _append_candidate_earnings_context_gap(
            row,
            family="sell_put",
            data_gaps=data_gaps,
        )
        required_context_rows += 1
        if cap is None:
            required_context_missing += 1
            data_gaps.append(_row_gap(row, "sell_put", "cash_capacity_unavailable"))
        put_capacity_rows.append((row, cap))
        candidate_payloads["sell_put"].append(
            _candidate_view(row, family="sell_put", rank=rank, capacity=cap, event_risk=event_risk)
        )
        if cap is not None:
            actions.append(
                _candidate_action(
                    row,
                    family="sell_put",
                    account=account_norm,
                    rank=rank,
                    capacity=cap,
                    event_risk=event_risk,
                )
            )
    if put_capacity_rows:
        first_known = next((item for _row, item in put_capacity_rows if item is not None), None)
        if first_known is not None:
            capacity["sell_put"] = first_known

    call_capacity_rows: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for rank, row in enumerate(selected_calls, start=1):
        cap = _covered_call_capacity(row)
        event_risk = _candidate_event_risk(
            row,
            family="covered_call",
            market_date=market_date,
        )
        _append_candidate_earnings_context_gap(
            row,
            family="covered_call",
            data_gaps=data_gaps,
        )
        required_context_rows += 1
        if cap is None:
            required_context_missing += 1
            data_gaps.append(_row_gap(row, "covered_call", "share_capacity_unavailable"))
        call_capacity_rows.append((row, cap))
        candidate_payloads["covered_call"].append(
            _candidate_view(
                row,
                family="covered_call",
                rank=rank,
                capacity=cap,
                event_risk=event_risk,
            )
        )
        if cap is not None:
            actions.append(
                _candidate_action(
                    row,
                    family="covered_call",
                    account=account_norm,
                    rank=rank,
                    capacity=cap,
                    event_risk=event_risk,
                )
            )
    if call_capacity_rows:
        first_known = next((item for _row, item in call_capacity_rows if item is not None), None)
        if first_known is not None:
            capacity["covered_call"] = first_known

    for rank, row in enumerate(selected_combos, start=1):
        event_risk = _candidate_event_risk(
            row,
            family="combo_yield",
            market_date=market_date,
        )
        _append_candidate_earnings_context_gap(
            row,
            family="combo_yield",
            data_gaps=data_gaps,
        )
        candidate_payloads["combo_yield"].append(
            _candidate_view(row, family="combo_yield", rank=rank, event_risk=event_risk)
        )
        actions.append(
            _candidate_action(
                row,
                family="combo_yield",
                account=account_norm,
                rank=rank,
                event_risk=event_risk,
            )
        )

    for row in close_rows:
        notification_eligible = id(row) in selected_close_row_ids
        positions.append(
            _position_view(
                row,
                notification_eligible=notification_eligible,
            )
        )
    for row in selected_close_rows:
        actions.append(_close_action(row, account=account_norm))

    portfolio_context = _load_portfolio_context(
        base=base_path,
        run_id=run_id_norm,
        account=account_norm,
        state_dir=state_dir,
        run_account_dir=run_account_dir,
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
    )
    option_positions_context = _load_json_artifact(
        path=state_dir / "option_positions_context.json",
        run_account_dir=run_account_dir,
        source_kind="option_positions_context",
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
        required=True,
    )
    funds, cash_total_reliable = _build_funds(
        portfolio_context=portfolio_context,
        option_positions_context=option_positions_context,
        data_gaps=data_gaps,
    )

    metrics = _load_json_artifact(
        path=state_dir / "account_metrics.json",
        run_account_dir=run_account_dir,
        source_kind="account_metrics",
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
        required=False,
    )
    try:
        ai_decision_advice_view = _json_safe(
            run_or_reuse_ai_decision_advice(
                base=base_path,
                run_id=run_id_norm,
                account=account_norm,
                market=market_norm,
                config=config_map,
                candidate_snapshot=accepted_candidate_snapshot,
                portfolio_distribution=prepared_portfolio_distribution,
                option_positions_context=prepared_option_positions_context,
                candidate_unavailable_reason=(
                    candidate_snapshot_unavailable_reason
                    or "candidate_snapshot_missing"
                ),
                portfolio_unavailable_reason=(
                    portfolio_distribution_unavailable_reason
                ),
                option_positions_unavailable_reason=(
                    option_positions_unavailable_reason
                ),
                now=effective_now,
            )
        )
    except Exception:
        ai_decision_advice_view = _json_safe(
            unavailable_brief_view("advice_execution_failed")
        )
    ai_decision_advice_evidence_index = _json_safe(
        ai_decision_advice_view.pop("evidence_index", None) or {}
    )
    prefetch = _load_json_artifact(
        path=state_dir / "required_data_prefetch_summary.json",
        run_account_dir=run_account_dir,
        source_kind="required_data_prefetch_summary",
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
        required=False,
    )
    _append_prefetch_gaps(prefetch, market=market_norm, data_gaps=data_gaps)

    rejections = {
        "schema_version": "opening_candidate_rejection_summary.v1",
        "available": False,
        "source": "opening_candidate_snapshot",
        "account": account_norm,
        "run_id": run_id_norm,
        "market": market_norm,
        "accepted_count": len(put_rows) + len(call_rows),
        "total_rejected": 0,
        "top_categories": [],
        "risk_alerts": [],
    }
    if strategy_failure_path.exists():
        source_artifacts.append(
            {
                "kind": "strategy_scan_failures",
                "path": _source_path(run_account_dir, strategy_failure_path),
                "row_count": _count_jsonl_rows(strategy_failure_path),
            }
        )

    result_view = _account_result_view(account_result)
    pipeline_failed = not bool(pipeline_succeeded)
    all_decision_sources_unavailable = not any(
        (put_available, call_available, combo_available, close_available)
    )
    all_required_context_unavailable = (
        required_context_rows > 0
        and required_context_missing == required_context_rows
        and not close_rows
        and not selected_combos
    )

    blockers: list[str] = []
    if pipeline_failed:
        blockers.append(result_view["decision_reason"] or "account_scan_not_completed")
    elif not result_view["ran_scan"]:
        blockers.append(result_view["decision_reason"] or "account_scan_not_run")
    if all_decision_sources_unavailable:
        blockers.append("all_structured_decision_sources_unavailable")
    indexed_candidate_sources_failed = bool(
        indexed_expected_families and not indexed_completed_families
    )
    if indexed_candidate_sources_failed or (
        not indexed_expected_families
        and strategy_failures
        and not (put_rows or call_rows or combo_rows)
    ):
        blockers.append("candidate_strategy_execution_failed")
    if all_required_context_unavailable:
        blockers.append("all_required_account_capacity_sources_unavailable")
    if not cash_total_reliable:
        blockers.append("cash_total_unavailable")
    if blockers:
        actionability = "blocked"
        status = "blocked"
        actions.insert(0, _blocked_action(account_norm, market_norm, blockers))
    else:
        in_run_window = scheduler.get("in_run_window")
        if in_run_window is False or effective_now >= valid_until:
            actionability = "planning_only"
        else:
            actionability = "live_actionable"
        status = "degraded" if data_gaps else "ready"

    generated_at = effective_now.isoformat()
    data_as_of = _latest_as_of(
        metrics,
        prefetch,
        portfolio_context,
        option_positions_context,
        fallback=effective_now,
    ).isoformat()
    candidate_index = (
        _build_candidate_index(
            account=account_norm,
            market=market_norm,
            market_date=market_date,
            ranked_puts=put_rows,
            ranked_calls=call_rows,
            combo_rows=combo_rows,
            data_gaps=data_gaps,
        )
        if actionability == "live_actionable"
        else []
    )
    if actionability != "blocked":
        status = "degraded" if data_gaps else "ready"
    deduped_actions = _dedupe_actions(actions)
    events = _candidate_events(deduped_actions)
    deduped_data_gaps = _dedupe_gaps(data_gaps)
    strategy_summary = _strategy_summary(
        actionability=actionability,
        blockers=blockers,
        actions=deduped_actions,
        candidates=candidate_payloads,
        data_gaps=deduped_data_gaps,
    )

    return normalize_daily_decision_brief(
        {
            "market": market_norm,
            "market_trading_date": market_date,
            "account": account_norm,
            "revision": 0,
            "run_id": run_id_norm,
            "generated_at_utc": generated_at,
            "data_as_of_utc": data_as_of,
            "valid_until_utc": valid_until.isoformat(),
            "status": status,
            "actionability": actionability,
            "strategy_summary": strategy_summary,
            "actions": deduped_actions,
            "positions": positions,
            "capacity": capacity,
            "funds": funds,
            "candidates": candidate_payloads,
            "candidate_index": candidate_index,
            "ai_decision_advice": ai_decision_advice_view,
            "ai_decision_advice_evidence_index": ai_decision_advice_evidence_index,
            "rejections": _json_safe(rejections),
            "events": events,
            "data_gaps": deduped_data_gaps,
            "source_artifacts": _dedupe_source_artifacts(source_artifacts),
        }
    )


def assemble_daily_decision_briefs(
    *,
    base: Path,
    run_id: str,
    account: str,
    markets_to_run: list[str] | tuple[str, ...],
    scheduler_decision: Mapping[str, Any] | None,
    account_result: AccountResult | Mapping[str, Any],
    pipeline_succeeded: bool,
    config: Mapping[str, Any] | None,
    now_utc: datetime | None = None,
    opening_candidate_snapshot: Mapping[str, Any] | None = None,
    candidate_snapshot_unavailable_reason: str | None = None,
    prepared_portfolio_distribution: (
        PreparedPortfolioDistribution | Mapping[str, Any] | None
    ) = None,
    portfolio_distribution_unavailable_reason: str = (
        "portfolio_unavailable"
    ),
    prepared_option_positions_context: Mapping[str, Any] | None = None,
    option_positions_unavailable_reason: str = (
        "option_positions_unavailable"
    ),
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for raw_market in markets_to_run:
        market = str(raw_market or "").strip().upper()
        if not market or market in out:
            continue
        out[market] = assemble_daily_decision_brief(
            base=base,
            run_id=run_id,
            account=account,
            market=market,
            scheduler_decision=scheduler_decision,
            account_result=account_result,
            pipeline_succeeded=pipeline_succeeded,
            config=config,
            now_utc=now_utc,
            opening_candidate_snapshot=opening_candidate_snapshot,
            candidate_snapshot_unavailable_reason=(
                candidate_snapshot_unavailable_reason
            ),
            prepared_portfolio_distribution=(
                prepared_portfolio_distribution
            ),
            portfolio_distribution_unavailable_reason=(
                portfolio_distribution_unavailable_reason
            ),
            prepared_option_positions_context=(
                prepared_option_positions_context
            ),
            option_positions_unavailable_reason=(
                option_positions_unavailable_reason
            ),
        )
    return out


def _load_opening_candidate_families(
    *,
    base: Path,
    run_id: str,
    account: str,
    market: str,
    source_artifacts: list[dict[str, Any]],
    data_gaps: list[dict[str, Any]],
    snapshot: Mapping[str, Any] | None = None,
    unavailable_reason: str | None = None,
) -> tuple[
    list[dict[str, Any]],
    bool,
    list[dict[str, Any]],
    bool,
    dict[str, Any] | None,
]:
    accepted_snapshot: dict[str, Any] | None = None
    try:
        if snapshot is None:
            if unavailable_reason:
                raise OpeningCandidateSnapshotError(unavailable_reason)
            accepted_snapshot = load_opening_candidate_snapshot(
                base=base,
                run_id=run_id,
                account=account,
            )
        else:
            validate_opening_candidate_snapshot(
                snapshot,
                expected_run_id=run_id,
                expected_account=account,
            )
            accepted_snapshot = dict(snapshot)
    except OpeningCandidateSnapshotError as exc:
        for family in ("sell_put", "covered_call"):
            data_gaps.append(
                {
                    "scope": "strategy",
                    "strategy_family": family,
                    "reason": "opening_candidate_snapshot_unavailable",
                    "error_type": type(exc).__name__,
                }
            )
        return [], False, [], False, None
    if str(accepted_snapshot.get("market") or "").upper() != market:
        for family in ("sell_put", "covered_call"):
            data_gaps.append(
                {
                    "scope": "strategy",
                    "strategy_family": family,
                    "reason": "opening_candidate_snapshot_market_mismatch",
                }
            )
        return [], False, [], False, None

    source_artifacts.append(
        {
            "kind": "opening_candidate_snapshot",
            "path": "state/opening_candidate_snapshot.json",
            "row_count": len(
                accepted_snapshot.get("ranked_candidates") or []
            ),
            "content_sha256": accepted_snapshot.get("content_sha256"),
        }
    )
    result_by_mode = {
        str(item.get("strategy_mode") or ""): dict(item)
        for item in accepted_snapshot.get("strategy_results") or []
        if isinstance(item, Mapping)
    }
    rows_by_mode: dict[str, list[dict[str, Any]]] = {"put": [], "call": []}
    for item in ranked_opening_candidates(accepted_snapshot):
        mode = str(item.get("strategy_mode") or "")
        if mode not in rows_by_mode:
            continue
        row = _json_safe(dict(item.get("facts") or {}))
        if _row_market(row) != market:
            continue
        row["candidate_id"] = item.get("candidate_id")
        row["_opening_snapshot_rank"] = item.get("rank")
        row["_source_path"] = "state/opening_candidate_snapshot.json"
        row["_source_row"] = item.get("rank")
        rows_by_mode[mode].append(row)

    universe = candidate_universe_summary(accepted_snapshot)
    partial_modes: set[str] = set()
    for affected in universe.get("affected_scopes") or []:
        if not isinstance(affected, Mapping):
            continue
        mode = str(affected.get("strategy_mode") or "")
        family = {"put": "sell_put", "call": "covered_call"}.get(mode)
        if family is None:
            continue
        partial_modes.add(mode)
        data_gaps.append(
            {
                "scope": "strategy",
                "strategy_family": family,
                "symbol": str(affected.get("symbol") or ""),
                "severity": "warning",
                "actionable": False,
                "reason": "opening_candidate_strategy_partial_data",
                "reason_code": affected.get("reason_code"),
            }
        )

    available: dict[str, bool] = {}
    for mode, family in (("put", "sell_put"), ("call", "covered_call")):
        status = str(result_by_mode.get(mode, {}).get("strategy_status") or "")
        if status == "partial_data" and mode not in partial_modes:
            data_gaps.append(
                {
                    "scope": "strategy",
                    "strategy_family": family,
                    "severity": "warning",
                    "actionable": False,
                    "reason": "opening_candidate_strategy_partial_data",
                }
            )
        available[mode] = bool(rows_by_mode[mode]) or status in {
            "candidates_found",
            "no_candidate",
            "partial_data",
        }
        if not available[mode]:
            data_gaps.append(
                {
                    "scope": "strategy",
                    "strategy_family": family,
                    "reason": "opening_candidate_strategy_data_unavailable",
                }
            )
    return (
        rows_by_mode["put"],
        available["put"],
        rows_by_mode["call"],
        available["call"],
        accepted_snapshot,
    )


def _load_combo_yield_snapshot_family(
    *,
    base: Path,
    run_id: str,
    account: str,
    market: str,
    source_artifacts: list[dict[str, Any]],
    data_gaps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Load Combo Yield pairs from the sealed account-run snapshot."""

    try:
        snapshot = load_combo_yield_candidate_snapshot(
            base=base,
            run_id=run_id,
            account=account,
        )
    except ComboYieldCandidateSnapshotError as exc:
        data_gaps.append(
            {
                "scope": "strategy",
                "strategy_family": "combo_yield",
                "reason": "combo_snapshot_unavailable",
                "error_type": type(exc).__name__,
            }
        )
        return [], False, None
    snapshot_market = str(snapshot.get("market") or "").strip().lower()
    if snapshot_market and snapshot_market != str(market).strip().lower():
        data_gaps.append(
            {
                "scope": "strategy",
                "strategy_family": "combo_yield",
                "reason": "combo_snapshot_market_mismatch",
            }
        )
        return [], False, None
    pairs = snapshot.get("ranked_pairs") or []
    market_rows: list[dict[str, Any]] = []
    for source_row, raw in enumerate(pairs, start=1):
        row = _json_safe(dict(raw))
        row_market = _row_market(row)
        if row_market is not None and row_market != str(market).strip().upper():
            continue
        row["_source_path"] = "state/combo_yield_candidate_snapshot.json"
        row["_source_row"] = source_row
        market_rows.append(row)
    source_artifacts.append(
        {
            "kind": "combo_yield_snapshot",
            "path": "state/combo_yield_candidate_snapshot.json",
            "row_count": len(market_rows),
            "opening_status": snapshot.get("opening_status"),
        }
    )
    opening_status = str(snapshot.get("opening_status") or "").strip().lower()
    available = bool(market_rows) or opening_status in {
        "candidates_found",
        "no_candidate",
        "partial_data",
    }
    return market_rows, available, opening_status


def _load_cc_lp_snapshot_family(
    *,
    base: Path,
    run_id: str,
    account: str,
    market: str,
    source_artifacts: list[dict[str, Any]],
    data_gaps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Load CC+LP pairs from the sealed account-run snapshot (data source only, no render)."""

    try:
        snapshot = load_cc_lp_candidate_snapshot(
            base=base,
            run_id=run_id,
            account=account,
        )
    except CcLpCandidateSnapshotError as exc:
        data_gaps.append(
            {
                "scope": "strategy",
                "strategy_family": "combo_yield",
                "variant": "cc_lp",
                "reason": "cc_lp_snapshot_unavailable",
                "error_type": type(exc).__name__,
            }
        )
        return [], False
    snapshot_market = str(snapshot.get("market") or "").strip().lower()
    if snapshot_market and snapshot_market != str(market).strip().lower():
        data_gaps.append(
            {
                "scope": "strategy",
                "strategy_family": "combo_yield",
                "variant": "cc_lp",
                "reason": "cc_lp_snapshot_market_mismatch",
            }
        )
        return [], False
    pairs = snapshot.get("ranked_pairs") or []
    market_rows: list[dict[str, Any]] = []
    for source_row, raw in enumerate(pairs, start=1):
        row = _json_safe(dict(raw))
        row_market = _row_market(row)
        if row_market is not None and row_market != str(market).strip().upper():
            continue
        row["_source_path"] = "state/cc_lp_candidate_snapshot.json"
        row["_source_row"] = source_row
        market_rows.append(row)
    source_artifacts.append(
        {
            "kind": "cc_lp_snapshot",
            "path": "state/cc_lp_candidate_snapshot.json",
            "row_count": len(market_rows),
            "opening_status": snapshot.get("opening_status"),
        }
    )
    return market_rows, True



def _load_close_advice(
    *,
    path: Path,
    run_account_dir: Path,
    market: str,
    account: str,
    run_id: str,
    source_artifacts: list[dict[str, Any]],
    data_gaps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    if not path.exists():
        data_gaps.append({"scope": "strategy", "strategy_family": "close_advice", "reason": "source_artifact_missing"})
        return [], False
    snapshot = read_close_advice_report_snapshot(
        csv_path=path,
        desired_market=market,
        account=account,
        expected_run_id=run_id,
        expected_quote_mode="frozen_snapshot",
    )
    manifest = snapshot["validation"]
    if not manifest.get("ok"):
        reason = str(
            manifest.get("reason") or "close_advice_manifest_invalid"
        )
        data_gaps.append(
            {
                "scope": "source",
                "strategy_family": "close_advice",
                "path": _source_path(run_account_dir, path),
                "reason": reason,
            }
        )
        source_artifacts.append(
            {
                "kind": "close_advice",
                "path": _source_path(run_account_dir, path),
                "row_count": 0,
                "status": "invalid",
                "reason": reason,
            }
        )
        return [], False
    try:
        frame = pd.read_csv(BytesIO(snapshot["csv_bytes"]))
    except pd.errors.EmptyDataError:
        source_artifacts.append(
            {"kind": "close_advice", "path": _source_path(run_account_dir, path), "row_count": 0}
        )
        return [], True
    except (OSError, pd.errors.ParserError, UnicodeError) as exc:
        data_gaps.append(
            {
                "scope": "source",
                "strategy_family": "close_advice",
                "path": _source_path(run_account_dir, path),
                "reason": "csv_unavailable",
                "error_type": type(exc).__name__,
            }
        )
        return [], False

    rows: list[dict[str, Any]] = []
    for source_row, raw in enumerate(frame.to_dict("records"), start=1):
        row = _json_safe(dict(raw))
        if _text(row.get("account")).lower() != account:
            reason = "close_advice_report_account_row_mismatch"
            data_gaps.append(
                {
                    "scope": "source",
                    "strategy_family": "close_advice",
                    "path": _source_path(run_account_dir, path),
                    "reason": reason,
                }
            )
            source_artifacts.append(
                {
                    "kind": "close_advice",
                    "path": _source_path(run_account_dir, path),
                    "row_count": 0,
                    "status": "invalid",
                    "reason": reason,
                }
            )
            return [], False
        if _row_market(row) != market:
            continue
        row["_source_path"] = _source_path(run_account_dir, path)
        row["_source_row"] = source_row
        rows.append(row)
    source_artifacts.append(
        {"kind": "close_advice", "path": _source_path(run_account_dir, path), "row_count": len(rows)}
    )
    return rows, True


def _load_json_artifact(
    *,
    path: Path,
    run_account_dir: Path,
    source_kind: str,
    source_artifacts: list[dict[str, Any]],
    data_gaps: list[dict[str, Any]],
    required: bool,
) -> dict[str, Any]:
    if not path.exists():
        if required:
            data_gaps.append({"scope": "source", "kind": source_kind, "reason": "source_artifact_missing"})
        return {}
    try:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        data_gaps.append(
            {
                "scope": "source",
                "kind": source_kind,
                "path": _source_path(run_account_dir, path),
                "reason": "json_unavailable",
                "error_type": type(exc).__name__,
            }
        )
        return {}
    if not isinstance(raw, dict):
        data_gaps.append(
            {
                "scope": "source",
                "kind": source_kind,
                "path": _source_path(run_account_dir, path),
                "reason": "json_not_object",
            }
        )
        return {}
    source_artifacts.append({"kind": source_kind, "path": _source_path(run_account_dir, path), "row_count": 1})
    return _json_safe(raw)


def _load_portfolio_context(
    *,
    base: Path,
    run_id: str,
    account: str,
    state_dir: Path,
    run_account_dir: Path,
    source_artifacts: list[dict[str, Any]],
    data_gaps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Load the immutable prepared portfolio generation when one exists."""

    manifest_path = state_dir / "prepared_portfolio_context.v1.json"
    if not manifest_path.exists():
        return _load_json_artifact(
            path=state_dir / "portfolio_context.json",
            run_account_dir=run_account_dir,
            source_kind="portfolio_context",
            source_artifacts=source_artifacts,
            data_gaps=data_gaps,
            required=True,
        )

    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest, Mapping):
            raise PreparedPortfolioContextError(
                "prepared portfolio manifest must be an object"
            )
        account_config_path = state_dir / "config.override.json"
        account_config_bytes = account_config_path.read_bytes()
        account_config = json.loads(account_config_bytes.decode("utf-8"))
        if not isinstance(account_config, Mapping):
            raise PreparedPortfolioContextError(
                "prepared portfolio account config must be an object"
            )
        account_config_sha256 = str(
            manifest.get("account_config_sha256") or ""
        ).strip().lower()
        if not account_config_sha256 or sha256_bytes(account_config_bytes) != account_config_sha256:
            raise PreparedPortfolioContextError(
                "prepared portfolio account config hash mismatch"
            )
        context = load_prepared_portfolio_context(
            manifest_path=manifest_path,
            expected_base=base,
            expected_run_id=run_id,
            expected_account=account,
            expected_account_config_sha256=account_config_sha256,
            expected_manifest_sha256=sha256_bytes(manifest_bytes),
            expected_runtime_config=account_config,
        )
        if not isinstance(context, dict):
            raise PreparedPortfolioContextError(
                "prepared portfolio context is unavailable"
            )
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        PreparedPortfolioContextError,
    ) as exc:
        data_gaps.append(
            {
                "scope": "source",
                "kind": "portfolio_context",
                "path": _source_path(run_account_dir, manifest_path),
                "reason": "prepared_portfolio_context_unavailable",
                "error_type": type(exc).__name__,
            }
        )
        return {}

    source_artifacts.append(
        {
            "kind": "prepared_portfolio_context",
            "path": _source_path(run_account_dir, manifest_path),
            "row_count": 1,
        }
    )
    return _json_safe(context)


def _load_strategy_step_failures(
    *,
    failure_path: Path,
    run_account_dir: Path,
    account: str,
    run_id: str,
    market: str,
    data_gaps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row_number, row in enumerate(read_strategy_scan_failures(failure_path), start=1):
        if (
            _text(row.get("account")).lower() != account
            or _text(row.get("run_id")) != run_id
            or _text(row.get("reason")).lower() != STRATEGY_FAILURE_REASON
            or _row_market(row) != market
        ):
            continue
        family = _strategy_family_from_failure(row)
        symbol = canonical_symbol(row.get("symbol")) or _text(row.get("symbol")).upper()
        identity = (family, symbol)
        if not family or identity in seen:
            continue
        seen.add(identity)
        failure = {
            "scope": "strategy",
            "strategy_family": family,
            "symbol": symbol,
            "reason": STRATEGY_FAILURE_REASON,
            "error_type": _text(row.get("error_type")) or "StrategyStepError",
            "source": {
                "path": _source_path(run_account_dir, failure_path),
                "row": row_number,
            },
        }
        failures.append(failure)
        data_gaps.append(failure)
    return failures


def _strategy_family_from_failure(row: Mapping[str, Any]) -> str:
    value = _text(row.get("strategy_family")).lower()
    if value in {"sell_call", "covered_call"}:
        return "covered_call"
    if value in {"combo_yield", "yield_enhancement"}:
        return "combo_yield"
    if value == "sell_put":
        return "sell_put"
    return ""



def _build_funds(
    *,
    portfolio_context: Mapping[str, Any],
    option_positions_context: Mapping[str, Any],
    data_gaps: list[dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    cash_total = _currency_amounts(portfolio_context.get("cash_by_currency"))
    portfolio_as_of = _parse_datetime(portfolio_context.get("as_of_utc"))
    cash_total_reliable = cash_total is not None and portfolio_as_of is not None
    if not cash_total_reliable:
        data_gaps.append(
            {
                "scope": "funds",
                "kind": "cash_total",
                "reason": "portfolio_cash_unavailable",
            }
        )

    secured = _currency_amounts(option_positions_context.get("cash_secured_total_by_ccy"))
    option_as_of = _parse_datetime(option_positions_context.get("as_of_utc"))
    unavailable = option_positions_context.get("cash_secured_unavailable_by_symbol")
    ledger = option_positions_context.get("ledger")
    unavailable_reliable = unavailable is None or (isinstance(unavailable, Mapping) and not unavailable)
    ledger_reliable = ledger is None or (
        isinstance(ledger, Mapping) and not bool(ledger.get("fail_closed"))
    )
    secured_reliable = (
        secured is not None
        and option_as_of is not None
        and unavailable_reliable
        and ledger_reliable
    )
    reason = "ok"
    opening: dict[str, float] = {}
    if cash_total_reliable and secured_reliable:
        opening = {
            currency: float(amount) - float((secured or {}).get(currency, 0.0))
            for currency, amount in (cash_total or {}).items()
        }
    if not secured_reliable:
        if reason == "ok":
            reason = "option_cash_secured_unavailable"
        data_gaps.append(
            {
                "scope": "funds",
                "kind": "option_opening_available",
                "reason": reason,
            }
        )
    if not cash_total_reliable:
        reason = "portfolio_cash_unavailable"

    rate_payload = option_positions_context.get("exchange_rates")
    rates = rate_payload.get("rates") if isinstance(rate_payload, Mapping) else None
    rates = rates if isinstance(rates, Mapping) else {}
    usdcny_rate = _number(rates.get("USDCNY"))
    cny_per_hkd_rate = _number(rates.get("HKDCNY"))
    cash_total_cny: float | None = None
    if cash_total:
        cash_total_cny = sum_by_currency_to_cny(
            cash_total,
            usdcny_exchange_rate=usdcny_rate,
            cny_per_hkd_exchange_rate=cny_per_hkd_rate,
        )
        if cash_total_cny is None:
            data_gaps.append(
                {
                    "scope": "funds",
                    "kind": "cash_total_cny",
                    "reason": "cash_total_cny_unavailable",
                }
            )
    secured_total_cny = read_cash_secured_total_cny(dict(option_positions_context)) if secured_reliable else None
    opening_cny: float | None = None
    if cash_total_cny is not None and secured_total_cny is not None:
        opening_cny = cash_total_cny - secured_total_cny

    as_of_values = [item for item in (portfolio_as_of, option_as_of) if item is not None]
    return (
        {
            "as_of_utc": max(as_of_values).astimezone(timezone.utc).isoformat() if as_of_values else "",
            "cash_total_by_currency": cash_total or {},
            "option_opening_available_by_currency": opening,
            "cash_total_cny": cash_total_cny,
            "cash_secured_total_cny": secured_total_cny,
            "option_opening_available_cny": opening_cny,
            "available": bool(
                cash_total_reliable and secured_reliable and (opening or opening_cny is not None)
            ),
            "reason": reason,
        },
        cash_total_reliable,
    )


def _currency_amounts(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    out: dict[str, float] = {}
    for raw_currency, raw_amount in value.items():
        currency = _text(raw_currency).upper()
        amount = _number(raw_amount)
        if not currency or amount is None:
            return None
        out[currency] = amount
    return {currency: out[currency] for currency in sorted(out)}


def _build_candidate_index(
    *,
    account: str,
    market: str,
    market_date: str,
    ranked_puts: list[dict[str, Any]],
    ranked_calls: list[dict[str, Any]],
    combo_rows: list[dict[str, Any]],
    data_gaps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    families = (
        ("sell_put", ranked_puts, _sell_put_capacity),
        ("covered_call", ranked_calls, _covered_call_capacity),
        ("combo_yield", combo_rows, _sell_put_capacity),
    )
    for family, rows, capacity_fn in families:
        for rank, row in enumerate(rows, start=1):
            capacity = capacity_fn(row)
            if capacity is None or int(capacity.get("contracts_available") or 0) < 1:
                continue
            if not _candidate_contract_is_complete(row, family=family):
                data_gaps.append(_row_gap(row, family, "candidate_identity_fields_incomplete"))
                continue
            try:
                identity = build_daily_brief_candidate_identity(
                    account=account,
                    market=market,
                    symbol=row.get("symbol"),
                    strategy_family=family,
                )
            except ValueError:
                data_gaps.append(_row_gap(row, family, "candidate_identity_invalid"))
                continue
            current = grouped.get(identity)
            if current is not None:
                current["contract_count"] += 1
                continue
            event_risk = _candidate_event_risk(
                row,
                family=family,
                market_date=market_date,
            )
            representative = _candidate_view(
                row,
                family=family,
                rank=rank,
                capacity=capacity,
                event_risk=event_risk,
            )
            if family == "combo_yield":
                for field in _COMBO_OCCURRENCE_FIELDS:
                    representative.pop(field, None)
            canonical = canonical_symbol(row.get("symbol"))
            representative["symbol"] = canonical
            representative["strategy_family"] = family
            grouped[identity] = {
                "identity": identity,
                "symbol": canonical,
                "strategy_family": family,
                "representative": representative,
                "contract_count": 1,
            }
    return [grouped[identity] for identity in sorted(grouped)]


def _candidate_contract_is_complete(row: Mapping[str, Any], *, family: str) -> bool:
    if family == "combo_yield":
        return all(
            (
                _text(row.get("strategy_group_id") or row.get("candidate_pair_id")),
                _text(row.get("put_contract_symbol")),
                _text(row.get("call_contract_symbol")),
                _text(row.get("put_expiration") or row.get("expiration")),
                _text(row.get("call_expiration") or row.get("expiration")),
                _number(row.get("put_strike")),
                _number(row.get("call_strike")),
            )
        )
    return all(
        (
            _text(row.get("contract_symbol") or row.get("code")),
            _text(row.get("expiration") or row.get("expiration_ymd")),
            _number(row.get("strike")),
        )
    )


def _sell_put_capacity(row: Mapping[str, Any]) -> dict[str, Any] | None:
    result = compute_sell_put_cash_capacity(
        cash_required_native=row.get("cash_required_native"),
        cash_free_effective_native=row.get("cash_free_effective_native"),
        cash_native_currency=row.get("cash_native_currency"),
        cash_required_cny=row.get("cash_required_cny"),
        cash_free_cny=row.get("cash_free_cny"),
        cash_free_total_cny=row.get("cash_free_total_cny"),
        cash_required_usd=row.get("cash_required_usd"),
        cash_free_usd=row.get("cash_free_usd"),
    )
    if result.basis is None or result.cash_required is None or result.cash_free is None or result.cash_required <= 0:
        return None
    contracts = int(result.max_new_contracts)
    return {
        "contracts_available": contracts,
        "basis": result.basis,
        "cash_free": float(result.cash_free),
        "cash_required_per_contract": float(result.cash_required),
        "accepted": bool(result.accepted),
        "reason": result.reason,
        "contract_symbol": _text(row.get("contract_symbol") or row.get("code")).upper(),
    }


def _covered_call_capacity(row: Mapping[str, Any]) -> dict[str, Any] | None:
    explicit = _number(row.get("call_covered_contracts_available"))
    result = compute_sell_call_share_capacity(
        shares_total=row.get("shares_total") if row.get("shares_total") is not None else row.get("shares"),
        shares_can_sell=row.get("shares_can_sell"),
        shares_locked=row.get("shares_locked") or 0,
        shares_available_for_cover=row.get("shares_available_for_cover"),
        multiplier=row.get("multiplier"),
    )
    if explicit is None and _number(row.get("multiplier")) is None:
        return None
    contracts = max(0, int(explicit)) if explicit is not None else int(result.covered_contracts_available)
    return {
        "contracts_available": contracts,
        "shares_total": int(result.shares_total),
        "shares_can_sell": result.shares_can_sell,
        "shares_eligible": int(result.shares_eligible),
        "shares_locked": int(result.shares_locked),
        "shares_available_for_cover": int(result.shares_available_for_cover),
        "multiplier": _number(row.get("multiplier")),
        "accepted": contracts >= 1,
        "reason": result.reason if explicit is None else ("share_capacity_supported" if contracts >= 1 else "share_capacity_insufficient"),
        "contract_symbol": _text(row.get("contract_symbol") or row.get("code")).upper(),
    }


def _candidate_action(
    row: Mapping[str, Any],
    *,
    family: str,
    account: str,
    rank: int,
    capacity: Mapping[str, Any] | None = None,
    event_risk: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if family == "combo_yield":
        group_id = _text(row.get("strategy_group_id") or row.get("candidate_pair_id"))
        action = {
            "priority": _priority_from_row(row, default="P1"),
            "state": "active",
            "action_type": "open_combo_yield",
            "strategy_family": family,
            "account": account,
            "symbol": _text(row.get("symbol")).upper(),
            "option_type": "",
            "side": "",
            "expiration": _text(row.get("put_expiration") or row.get("expiration")),
            "strike": row.get("put_strike"),
            "contract_symbol": _text(row.get("put_contract_symbol")).upper(),
            "strategy_group_id": group_id,
            "leg_role": "pair",
            "title": "Combo Yield 候选",
            "reason": _text(row.get("reason") or "已通过现有组合收益筛选"),
            "metrics": _candidate_metrics(row, rank=rank),
            "event_risk": dict(event_risk or {}),
            "source": _source_view(row),
        }
        action["metrics"].update(
            {
                "put_contract_symbol": _text(row.get("put_contract_symbol")).upper(),
                "call_contract_symbol": _text(row.get("call_contract_symbol")).upper(),
                "put_leg_role": _text(row.get("put_leg_role") or "funding_put"),
                "call_leg_role": _text(row.get("call_leg_role") or "participation_call"),
            }
        )
        return action

    option_type = "put" if family == "sell_put" else "call"
    contracts = int((capacity or {}).get("contracts_available") or 0)
    return {
        "priority": _priority_from_row(row, default="P1"),
        "state": "active" if contracts >= 1 else "blocked",
        "action_type": "open_candidate",
        "strategy_family": family,
        "account": account,
        "symbol": _text(row.get("symbol")).upper(),
        "option_type": option_type,
        "side": "short",
        "expiration": _text(row.get("expiration") or row.get("expiration_ymd")),
        "strike": row.get("strike"),
        "contract_symbol": _text(row.get("contract_symbol") or row.get("code")).upper(),
        "title": "Sell Put 候选" if family == "sell_put" else "Covered Call 候选",
        "reason": _text(row.get("reason") or (capacity or {}).get("reason") or "已通过现有候选过滤"),
        "metrics": {**_candidate_metrics(row, rank=rank), "capacity": dict(capacity or {})},
        "event_risk": dict(event_risk or {}),
        "source": _source_view(row),
    }


def _candidate_view(
    row: Mapping[str, Any],
    *,
    family: str,
    rank: int,
    capacity: Mapping[str, Any] | None = None,
    event_risk: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if family == "combo_yield":
        put_sell_reference = _number(row.get("put_bid"))
        if put_sell_reference is None:
            put_sell_reference = _number(row.get("bid"))
        call_buy_reference = _number(row.get("call_ask"))
        if call_buy_reference is None:
            call_buy_reference = _number(row.get("linked_call_ask"))
        return {
            "rank": rank,
            "symbol": _text(row.get("symbol")).upper(),
            "strategy_group_id": _text(row.get("strategy_group_id") or row.get("candidate_pair_id")),
            "candidate_pair_id": _text(row.get("candidate_pair_id") or row.get("strategy_group_id")),
            "structure_mode": _text(row.get("structure_mode")).lower(),
            "put_contract_symbol": _text(row.get("put_contract_symbol")).upper(),
            "call_contract_symbol": _text(row.get("call_contract_symbol")).upper(),
            "put_leg_role": _text(row.get("put_leg_role") or "funding_put"),
            "call_leg_role": _text(row.get("call_leg_role") or "participation_call"),
            "put_expiration": _text(row.get("put_expiration") or row.get("expiration")),
            "call_expiration": _text(row.get("call_expiration") or row.get("expiration")),
            "put_strike": _number(row.get("put_strike")),
            "call_strike": _number(row.get("call_strike")),
            "currency": _text(row.get("currency")).upper(),
            "multiplier": _number(row.get("multiplier")),
            "put_sell_reference": put_sell_reference,
            "call_buy_reference": call_buy_reference,
            "priority": _priority_from_row(row, default="P1"),
            "metrics": _candidate_metrics(row, rank=rank),
            "capacity": dict(capacity or {}),
            "event_risk": dict(event_risk or {}),
            "source": _source_view(row),
            **{
                field: row.get(field)
                for field in _COMBO_OCCURRENCE_FIELDS
                if row.get(field) not in (None, "")
            },
        }
    return {
        "candidate_id": _text(row.get("candidate_id")),
        "rank": rank,
        "symbol": _text(row.get("symbol")).upper(),
        "option_type": "put" if family == "sell_put" else "call",
        "contract_symbol": _text(row.get("contract_symbol") or row.get("code")).upper(),
        "expiration": _text(row.get("expiration") or row.get("expiration_ymd")),
        "strike": _number(row.get("strike")),
        "priority": _priority_from_row(row, default="P1"),
        "metrics": _candidate_metrics(row, rank=rank),
        "capacity": dict(capacity or {}),
        "event_risk": dict(event_risk or {}),
        "source": _source_view(row),
    }


def _close_action(row: Mapping[str, Any], *, account: str) -> dict[str, Any]:
    return {
        "priority": _priority_from_row(row, default="P2"),
        "state": "active",
        "action_type": "close_position",
        "strategy_family": _text(row.get("strategy_family") or row.get("strategy") or "close_advice").lower(),
        "account": account,
        "symbol": _text(row.get("symbol")).upper(),
        "option_type": _text(row.get("option_type")).lower(),
        "side": _text(row.get("position_side") or "short").lower(),
        "expiration": _text(row.get("expiration")),
        "strike": row.get("strike"),
        "contract_symbol": _text(row.get("contract_symbol")).upper(),
        "position_lot_id": _text(row.get("position_lot_id")),
        "strategy_group_id": _text(row.get("strategy_group_id")),
        "leg_role": _text(row.get("leg_role")).lower(),
        "title": "严格平仓提醒",
        "reason": _text(row.get("reason")),
        "recommendation_state": _text(
            row.get("recommendation_state")
        ).lower(),
        "policy_version": _text(row.get("policy_version")),
        "metrics": {
            key: _json_safe(row.get(key))
            for key in (
                "contracts_open",
                "ask",
                "dte",
                "original_dte",
                "remaining_term_ratio",
                "net_capture_ratio",
                "opening_net_credit",
                "all_in_close_cost",
                "close_cost_ratio",
                "estimated_pnl_if_close_net",
            )
            if row.get(key) is not None
        },
        "source": _source_view(row),
    }


def _position_view(
    row: Mapping[str, Any],
    *,
    notification_eligible: bool,
) -> dict[str, Any]:
    fields = (
        "position_lot_id",
        "strategy_group_id",
        "leg_role",
        "symbol",
        "option_type",
        "expiration",
        "strike",
        "contract_symbol",
        "reason",
        "evaluation_status",
        "quote_status",
        "recommendation_state",
        "policy_version",
    )
    out = {field: _json_safe(row.get(field)) for field in fields}
    out["advice_kind"] = "close_advice"
    out["notification_eligible"] = notification_eligible
    out["metrics"] = {
        key: _json_safe(row.get(key))
        for key in (
            "ask",
            "remaining_term_ratio",
            "net_capture_ratio",
            "all_in_close_cost",
            "close_cost_ratio",
            "estimated_pnl_if_close_net",
        )
        if row.get(key) is not None
    }
    out["strategy_family"] = _text(
        row.get("strategy_family") or row.get("strategy") or "close_advice"
    ).lower()
    return out



def _blocked_action(account: str, market: str, blockers: list[str]) -> dict[str, Any]:
    return {
        "priority": "P0",
        "state": "blocked",
        "action_type": "resolve_data_blocker",
        "strategy_family": "account",
        "account": account,
        "symbol": "",
        "option_type": "",
        "side": "",
        "expiration": "",
        "strike": None,
        "contract_symbol": "",
        "title": f"{market} 日报不可行动",
        "reason": "; ".join(blockers),
        "metrics": {"blockers": list(blockers)},
        "source": {"kind": "daily_decision_brief_assembler"},
    }


def _candidate_metrics(row: Mapping[str, Any], *, rank: int) -> dict[str, Any]:
    keys = (
        "spot",
        "mid",
        "bid",
        "ask",
        "delta",
        "dte",
        "net_income",
        "period_net_return",
        "period_net_return_on_cash_basis",
        "period_net_premium_return",
        "annualized_return",
        "annualized_net_return_on_cash_basis",
        "annualized_net_premium_return",
        "annualized_net_credit_yield",
        "net_credit",
        "net_debit",
        "funding_ratio",
        "net_credit_retention",
        "call_cost_to_put_credit",
        "combo_spread_ratio",
    )
    out = {"rank": rank}
    out.update({key: _json_safe(row.get(key)) for key in keys if row.get(key) is not None})
    return out


def _priority_from_row(row: Mapping[str, Any], *, default: str) -> str:
    raw = next(
        (
            _text(row.get(key))
            for key in ("priority", "alert_level", "tier")
            if _text(row.get(key))
        ),
        "",
    )
    upper = raw.upper()
    if upper in {"P0", "P1", "P2"}:
        return upper
    lowered = raw.lower()
    if lowered in {"strong", "critical", "urgent", "high"}:
        return "P0"
    if lowered in {"medium", "warning", "warn"}:
        return "P1"
    if lowered:
        return "P2"
    return default


def _row_market(row: Mapping[str, Any]) -> str | None:
    explicit = _text(row.get("market") or row.get("broker")).upper()
    if explicit in {"US", "HK", "CN"}:
        return explicit
    return symbol_market(row.get("symbol") or row.get("underlier"))


def _dedupe_rows(rows: list[dict[str, Any]], *, family: str) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if family == "combo_yield":
            identity = (
                _text(row.get("strategy_group_id") or row.get("candidate_pair_id")),
                _text(row.get("put_contract_symbol")).upper(),
                _text(row.get("call_contract_symbol")).upper(),
            )
        else:
            identity = (
                _text(row.get("symbol")).upper(),
                _text(row.get("contract_symbol") or row.get("code")).upper(),
                _text(row.get("expiration") or row.get("expiration_ymd")),
                _text(row.get("strike")),
            )
        if identity in seen:
            continue
        seen.add(identity)
        out.append(row)
    return out


def _dedupe_close_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        identity = (
            _text(row.get("position_lot_id")),
            _text(row.get("symbol")).upper(),
            _text(row.get("option_type")).lower(),
            _text(row.get("expiration")),
            _text(row.get("strike")),
        )
        if identity in seen:
            continue
        seen.add(identity)
        out.append(row)
    return out


def _dedupe_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from domain.domain.daily_decision_brief import build_daily_brief_action_id

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for action in actions:
        action_id = build_daily_brief_action_id(action)
        if action_id in seen:
            continue
        seen.add(action_id)
        out.append(action)
    return out


def _row_gap(row: Mapping[str, Any], family: str, reason: str) -> dict[str, Any]:
    return {
        "scope": "candidate",
        "strategy_family": family,
        "symbol": _text(row.get("symbol")).upper(),
        "contract_symbol": _text(
            row.get("contract_symbol") or row.get("put_contract_symbol") or row.get("code")
        ).upper(),
        "reason": reason,
        "source": _source_view(row),
    }


def _append_candidate_earnings_context_gap(
    row: Mapping[str, Any],
    *,
    family: str,
    data_gaps: list[dict[str, Any]],
) -> None:
    if _text(row.get("earnings_soft_coverage_status")).lower() != "partial":
        return
    raw_reason_codes = row.get("earnings_soft_reason_codes")
    reason_codes = (
        [str(item) for item in raw_reason_codes]
        if isinstance(raw_reason_codes, (list, tuple))
        else []
    )
    gap = _row_gap(row, family, "earnings_soft_coverage_partial")
    gap.update(
        {
            "severity": "warning",
            "actionable": False,
            "reason_codes": reason_codes,
        }
    )
    data_gaps.append(gap)


def _source_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "path": _text(row.get("_source_path")),
        "row": int(row.get("_source_row") or 0),
    }


def _candidate_event_risk(
    row: Mapping[str, Any],
    *,
    family: str,
    market_date: str,
) -> dict[str, Any]:
    symbol = canonical_symbol(row.get("symbol")) or _text(row.get("symbol")).upper()
    if family == "combo_yield":
        expirations = {
            "put": row.get("put_expiration") or row.get("expiration"),
            "call": row.get("call_expiration") or row.get("expiration"),
        }
    else:
        expirations = {"contract": row.get("expiration") or row.get("expiration_ymd")}
    status = _text(row.get("earnings_evidence_status")).lower()
    if status != "ready":
        return {
            "user_state": "unknown",
            "reason_code": _text(row.get("earnings_reason_code"))
            or "earnings_evidence_unavailable",
            "reliable": False,
            "symbol": symbol,
            "selected_provider": "opend",
            "evidence_chain_id": _text(row.get("earnings_snapshot_hash")),
            "coverage": {
                "earnings": status or "unavailable",
                "earnings_hard": _text(
                    row.get("earnings_hard_coverage_status")
                ).lower()
                or "unavailable",
                "earnings_soft": _text(
                    row.get("earnings_soft_coverage_status")
                ).lower()
                or "unavailable",
            },
            "nearest_event": None,
            "events": [],
            "days_to_event": None,
            "expiration_relations": {},
            "in_attention_window": False,
        }
    if (
        _text(row.get("earnings_policy_version"))
        != EARNINGS_NEAR_EXPIRY_POLICY_VERSION
        or int(_number(row.get("earnings_window_days")) or -1)
        != EARNINGS_NEAR_EXPIRY_WINDOW_DAYS
    ):
        return {
            "user_state": "unknown",
            "reason_code": "earnings_policy_evidence_legacy_or_invalid",
            "reliable": False,
            "symbol": symbol,
            "selected_provider": "opend",
            "evidence_chain_id": _text(row.get("earnings_snapshot_hash")),
            "coverage": {"earnings": "unavailable"},
            "nearest_event": None,
            "events": [],
            "days_to_event": None,
            "expiration_relations": {},
            "in_attention_window": False,
        }

    raw_events = row.get("earnings_events") or []
    if isinstance(raw_events, str):
        try:
            decoded_events = json.loads(raw_events)
        except json.JSONDecodeError:
            decoded_events = []
        raw_events = decoded_events if isinstance(decoded_events, list) else []
    events = [
        dict(item)
        for item in raw_events
        if isinstance(item, Mapping)
    ]
    if not events:
        events = [
            {"earnings_date": value.strip()}
            for value in _text(row.get("earnings_event_dates")).split(",")
            if value.strip()
        ]
    normalized_events: list[dict[str, Any]] = []
    for item in events:
        event_date = _text(item.get("earnings_date") or item.get("event_date"))
        if not event_date:
            continue
        normalized_events.append(
            {
                **item,
                "event_type": "earnings",
                "event_date": event_date,
                "event_id": _text(item.get("event_id"))
                or f"{symbol}:earnings:{event_date}",
                "source": "opend",
            }
        )
    normalized_events.sort(key=lambda item: _text(item.get("event_date")))
    has_event = row.get("earnings_has_event")
    blocking_has_event = row.get("earnings_blocking_has_event")
    if (
        not isinstance(has_event, bool)
        or not isinstance(blocking_has_event, bool)
        or has_event != bool(normalized_events)
        or (blocking_has_event and not has_event)
    ):
        return {
            "user_state": "unknown",
            "reason_code": "earnings_event_evidence_inconsistent",
            "reliable": False,
            "symbol": symbol,
            "selected_provider": "opend",
            "evidence_chain_id": _text(row.get("earnings_snapshot_hash")),
            "coverage": {"earnings": "unavailable"},
            "nearest_event": None,
            "events": [],
            "days_to_event": None,
            "expiration_relations": {},
            "in_attention_window": False,
        }
    if not has_event:
        return {
            "user_state": "confirmed_none",
            "reason_code": "confirmed_no_upcoming_earnings",
            "reliable": True,
            "symbol": symbol,
            "selected_provider": "opend",
            "evidence_chain_id": _text(row.get("earnings_snapshot_hash")),
            "coverage": {
                "earnings": "complete",
                "earnings_hard": _text(
                    row.get("earnings_hard_coverage_status")
                ).lower(),
                "earnings_soft": _text(
                    row.get("earnings_soft_coverage_status")
                ).lower(),
            },
            "nearest_event": None,
            "events": [],
            "days_to_event": None,
            "expiration_relations": {},
            "in_attention_window": False,
        }
    nearest = normalized_events[0]
    try:
        event_date = datetime.fromisoformat(_text(nearest["event_date"])).date()
        as_of = datetime.fromisoformat(_text(market_date)).date()
    except ValueError:
        event_date = as_of = None
    relations: dict[str, dict[str, Any]] = {}
    for label, raw_expiration in expirations.items():
        try:
            expiration = datetime.fromisoformat(_text(raw_expiration)).date()
        except ValueError:
            continue
        relation = "after_expiration"
        if event_date is not None and event_date < expiration:
            relation = "before_expiration"
        elif event_date is not None and event_date == expiration:
            relation = "on_expiration"
        relations[label] = {
            "expiration": expiration.isoformat(),
            "relation": relation,
            "days_before_expiration": (
                (expiration - event_date).days
                if event_date is not None
                else None
            ),
        }
    blocking = blocking_has_event
    return {
        "user_state": "confirmed_event",
        "reason_code": (
            "confirmed_near_expiry_earnings_event"
            if blocking
            else "confirmed_distant_earnings_event"
        ),
        "reliable": True,
        "symbol": symbol,
        "selected_provider": "opend",
        "evidence_chain_id": _text(row.get("earnings_snapshot_hash")),
        "coverage": {
            "earnings": "complete",
            "earnings_hard": _text(
                row.get("earnings_hard_coverage_status")
            ).lower(),
            "earnings_soft": _text(
                row.get("earnings_soft_coverage_status")
            ).lower(),
        },
        "nearest_event": nearest,
        "events": normalized_events,
        "days_to_event": (
            (event_date - as_of).days
            if event_date is not None and as_of is not None
            else None
        ),
        "expiration_relations": relations,
        "in_attention_window": blocking,
    }


def _candidate_events(actions: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    from domain.domain.daily_decision_brief import build_daily_brief_action_id

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for action in actions:
        if _text(action.get("action_type")) not in {"open_candidate", "open_combo_yield"}:
            continue
        risk = action.get("event_risk")
        if not isinstance(risk, Mapping):
            continue
        action_id = build_daily_brief_action_id(action)
        for event in risk.get("events") or []:
            if not isinstance(event, Mapping):
                continue
            event_id = _text(event.get("event_id"))
            identity = (action_id, event_id)
            if not event_id or identity in seen:
                continue
            seen.add(identity)
            out.append(
                {
                    **dict(event),
                    "symbol": _text(action.get("symbol")).upper(),
                    "candidate_action_id": action_id,
                    "strategy_family": _text(action.get("strategy_family")).lower(),
                    "contract_symbol": _text(action.get("contract_symbol")).upper(),
                    "strategy_group_id": _text(action.get("strategy_group_id")),
                }
            )
    return out


def _append_prefetch_gaps(prefetch: Mapping[str, Any], *, market: str, data_gaps: list[dict[str, Any]]) -> None:
    if not prefetch:
        return
    symbols = prefetch.get("symbols")
    results = prefetch.get("results")
    result_map = results if isinstance(results, Mapping) else {}
    failed_symbols: set[str] = set()
    if isinstance(symbols, Mapping):
        symbol_items = [
            {"symbol": symbol, **dict(item)}
            for symbol, item in symbols.items()
            if isinstance(item, Mapping)
        ]
    elif isinstance(symbols, list):
        symbol_items = [
            dict(item)
            for item in symbols
            if isinstance(item, Mapping)
        ]
    else:
        symbol_items = []
    for item in symbol_items:
        symbol = _text(item.get("symbol")).upper()
        if symbol_market(symbol) != market:
            continue
        status = _text(
            item.get("status") or item.get("source_status")
        ).lower()
        if status and status not in {
            "ok",
            "ready",
            "success",
            "available",
            "completed",
            "cached",
        }:
            failed_symbols.add(symbol)
            data_gaps.append(
                {
                    "scope": "symbol",
                    "market": market,
                    "symbol": symbol,
                    "reason": _text(
                        item.get("reason")
                        or item.get("message")
                        or result_map.get(symbol)
                        or status
                    ),
                    "source": "required_data_prefetch_summary",
                }
            )
    errors = int(_number(prefetch.get("errors")) or 0)
    summary = prefetch.get("summary")
    if errors <= 0 and isinstance(summary, Mapping):
        errors = int(_number(summary.get("errors")) or 0)
    if errors > 0 and not failed_symbols:
        data_gaps.append(
            {
                "scope": "prefetch",
                "market": market,
                "reason": "required_data_prefetch_errors",
                "count": errors,
            }
        )


def _append_strategy_status_gaps(
    index: Mapping[str, Any],
    *,
    run_id: str,
    account: str,
    market: str,
    data_gaps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not index:
        return []
    if (
        index.get("schema_version") != "strategy_scan_status_index.v1"
        or _text(index.get("run_id")) != run_id
        or _text(index.get("account")).lower() != account
        or not isinstance(index.get("items"), list)
    ):
        data_gaps.append(
            {
                "scope": "source",
                "kind": "strategy_scan_status_index",
                "reason": "strategy_scan_status_index_invalid",
            }
        )
        return []
    relevant: list[dict[str, Any]] = []
    for raw in index.get("items") or []:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        if _text(item.get("market")).upper() != market:
            continue
        symbol = _text(item.get("symbol")).upper()
        family = _text(item.get("strategy_family")).lower()
        status = _text(item.get("status")).lower()
        if not symbol or not family:
            continue
        relevant.append(item)
        if status in {"completed", "not_applicable"}:
            if _text(item.get("reason")) == "partial_data":
                data_gaps.append(
                    {
                        "scope": "strategy",
                        "market": market,
                        "symbol": symbol,
                        "strategy_family": family,
                        "severity": "warning",
                        "actionable": False,
                        "reason": "opening_candidate_strategy_partial_data",
                    }
                )
            continue
        data_gaps.append(
            {
                "scope": "strategy",
                "market": market,
                "symbol": symbol,
                "strategy_family": family,
                "reason": _text(
                    item.get("reason") or "strategy_scan_status_invalid"
                ),
                "source_status_path": _text(
                    item.get("source_status_path")
                ),
            }
        )
    return relevant



def _account_result_view(result: AccountResult | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return {
            "ran_scan": bool(result.get("ran_scan")),
            "decision_reason": _text(result.get("decision_reason") or result.get("reason")),
        }
    return {
        "ran_scan": bool(getattr(result, "ran_scan", False)),
        "decision_reason": _text(getattr(result, "decision_reason", "")),
    }


def _daily_brief_config(config: Mapping[str, Any]) -> dict[str, Any]:
    notifications = config.get("notifications")
    if not isinstance(notifications, Mapping):
        return {}
    brief = notifications.get("daily_brief")
    return dict(brief) if isinstance(brief, Mapping) else {}


def _close_advice_max_items_per_account(config: Mapping[str, Any]) -> int:
    close_advice = config.get("close_advice")
    close_advice_map = (
        dict(close_advice)
        if isinstance(close_advice, Mapping)
        else {}
    )
    configured = safe_int(close_advice_map.get("max_items_per_account"))
    return (
        _DEFAULT_CLOSE_ADVICE_MAX_ITEMS_PER_ACCOUNT
        if configured is None
        else configured
    )


def _market_timezone(config: Mapping[str, Any], market: str) -> str:
    schedule = config.get("schedule")
    if isinstance(schedule, Mapping) and _text(schedule.get("timezone")):
        return _text(schedule.get("timezone"))
    return _MARKET_TIMEZONES.get(market, "UTC")


def _valid_until_utc(config: Mapping[str, Any], market: str, now_market: datetime) -> datetime:
    schedule = config.get("schedule")
    schedule_map = dict(schedule) if isinstance(schedule, Mapping) else {}
    window = schedule_map.get("run_window")
    window_map = dict(window) if isinstance(window, Mapping) else {}
    end = _parse_hhmm(window_map.get("end"), default=time(16, 0))
    local = datetime.combine(now_market.date(), end, tzinfo=now_market.tzinfo)
    return local.astimezone(timezone.utc)


def _latest_as_of(*sources: Mapping[str, Any], fallback: datetime) -> datetime:
    parsed = [
        item
        for source in sources
        for key in ("as_of_utc", "generated_at_utc", "updated_at_utc")
        if (item := _parse_datetime(source.get(key))) is not None
    ]
    return max(parsed) if parsed else fallback


def _strategy_summary(
    *,
    actionability: str,
    blockers: list[str],
    actions: list[dict[str, Any]],
    candidates: Mapping[str, list[dict[str, Any]]],
    data_gaps: list[dict[str, Any]],
) -> str:
    if actionability == "blocked":
        return "日报阻塞：" + "；".join(blockers)
    active = sum(1 for item in actions if item.get("state") == "active")
    summary = (
        f"有效行动 {active} 条；候选证据：Sell Put {len(candidates['sell_put'])}，"
        f"Covered Call {len(candidates['covered_call'])}，Combo Yield {len(candidates['combo_yield'])}"
    )
    if data_gaps:
        summary += f"；数据缺口 {len(data_gaps)} 条"
    return summary + "。"


def _dedupe_gaps(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        safe = _json_safe(item)
        correlation = (
            _text(safe.get("market")).upper(),
            _text(safe.get("symbol")).upper(),
            _text(safe.get("strategy_family")).lower(),
            _text(safe.get("reason")).lower(),
        )
        key = (
            repr(correlation)
            if correlation[1] and correlation[3]
            else repr(sorted(safe.items(), key=lambda pair: pair[0]))
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(safe)
    return out


def _dedupe_source_artifacts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        key = (_text(item.get("kind")), _text(item.get("path")))
        merged[key] = _json_safe(item)
    return [merged[key] for key in sorted(merged)]


def _source_path(run_account_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(run_account_dir.resolve()).as_posix()
    except ValueError:
        return path.name


def _count_jsonl_rows(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def _parse_hhmm(value: Any, *, default: time) -> time:
    text = _text(value)
    if not text:
        return default
    try:
        hour, minute = text.split(":", 1)
        return time(hour=int(hour), minute=int(minute))
    except (TypeError, ValueError):
        return default


def _parse_datetime(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_utc(value: datetime | None) -> datetime:
    parsed = value or datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _positive_int(value: Any, *, default: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if out > 0 else default


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return str(value).strip()


__all__ = ["assemble_daily_decision_brief", "assemble_daily_decision_briefs"]
