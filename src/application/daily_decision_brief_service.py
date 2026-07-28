from __future__ import annotations

import math
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from domain.domain.daily_decision_brief import (
    build_daily_brief_candidate_identity,
    normalize_daily_decision_brief,
)
from domain.domain.daily_decision_event_risk import build_candidate_event_risk
from domain.domain.engine import (
    select_best_candidate_per_symbol,
    select_best_yield_enhancement_per_symbol,
)
from domain.domain.risk_capacity import compute_sell_call_share_capacity, compute_sell_put_cash_capacity
from domain.domain.cash_secured_utils import read_cash_secured_total_cny
from domain.domain.position_advice_authority import scope_for
from domain.domain.symbol_identity import canonical_symbol, symbol_market
from domain.storage import paths
from src.application import candidate_reject_summary as candidate_rejections
from src.application.cash_totals import sum_by_currency_to_cny
from src.application.strategy_scan_failures import (
    ARTIFACT_NAME as STRATEGY_FAILURE_ARTIFACT_NAME,
    FAILURE_REASON as STRATEGY_FAILURE_REASON,
    read_strategy_scan_failures,
)
from src.application.multi_tick.misc import AccountResult
from src.application.config_loader import resolve_data_config_path
from src.application.position_advice_reader import (
    read_position_advice_v2_from_ledger,
)
from src.application.position_advice_notification_authority import (
    build_notification_authority_token,
)
from src.infrastructure.position_advice_manifest_lock import (
    position_advice_state_root,
)


_DEFAULT_MAX_CANDIDATES = 3
_MARKET_TIMEZONES = {"US": "America/New_York", "HK": "Asia/Hong_Kong", "CN": "Asia/Shanghai"}


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
    trace_path = run_account_dir / "candidate_filter_trace.jsonl"
    strategy_failure_path = run_account_dir / STRATEGY_FAILURE_ARTIFACT_NAME

    data_gaps: list[dict[str, Any]] = []
    source_artifacts: list[dict[str, Any]] = []
    max_candidates = _positive_int(
        _daily_brief_config(config_map).get("max_candidates_per_strategy"),
        default=_DEFAULT_MAX_CANDIDATES,
    )

    put_rows, put_available = _load_sell_put_candidates(
        run_account_dir=run_account_dir,
        market=market_norm,
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
    )
    call_rows, call_available = _load_candidate_family(
        run_account_dir=run_account_dir,
        market=market_norm,
        family="covered_call",
        paths_to_try=sorted(run_account_dir.glob("*_sell_call_candidates.csv")),
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
    )
    combo_rows, combo_available = _load_candidate_family(
        run_account_dir=run_account_dir,
        market=market_norm,
        family="combo_yield",
        paths_to_try=sorted(run_account_dir.glob("*_combo_yield_candidates.csv")),
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
    )
    advice_authority = _daily_brief_advice_authority(
        base=base_path,
        state_dir=state_dir,
        account=account_norm,
        config=config_map,
        now_utc=effective_now,
    )
    position_advice_rows: list[dict[str, Any]] = []
    if advice_authority["mode"] == "v2":
        position_advice_rows = [
            dict(item)
            for item in advice_authority.get("rows") or []
            if isinstance(item, Mapping) and _row_market(item) == market_norm
        ]
        close_rows = []
        close_available = bool(advice_authority.get("available"))
        source_artifacts.append(
            {
                "kind": "position_advice_v2",
                "path": (
                    "current:"
                    + str(advice_authority.get("portfolio_plan_id") or "")
                ),
                "row_count": len(position_advice_rows),
            }
        )
        if not close_available:
            data_gaps.append(
                {
                    "scope": "strategy",
                    "strategy_family": "position_advice",
                    "reason": str(
                        advice_authority.get("blocker")
                        or "position_advice_unavailable"
                    ),
                }
            )
    elif advice_authority["mode"] == "authority_conflict":
        close_rows, close_available = [], False
        position_advice_rows = []
        data_gaps.append(
            {
                "scope": "authority",
                "strategy_family": "position_advice",
                "reason": str(
                    advice_authority.get("blocker")
                    or "position_advice_authority_conflict"
                ),
            }
        )
    else:
        close_rows, close_available = _load_close_advice(
            path=run_account_dir / "close_advice.csv",
            run_account_dir=run_account_dir,
            market=market_norm,
            source_artifacts=source_artifacts,
            data_gaps=data_gaps,
        )

    put_rows = _dedupe_rows(put_rows, family="sell_put")
    call_rows = _dedupe_rows(call_rows, family="covered_call")
    combo_rows = _dedupe_rows(combo_rows, family="combo_yield")
    close_rows = _dedupe_close_rows(close_rows)
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

    ranked_puts = (
        select_best_candidate_per_symbol(put_rows, mode="put")
        if put_rows
        else []
    )
    ranked_calls = (
        select_best_candidate_per_symbol(call_rows, mode="call")
        if call_rows
        else []
    )
    selected_puts = ranked_puts[:max_candidates]
    selected_calls = ranked_calls[:max_candidates]
    ranked_combos = (
        select_best_yield_enhancement_per_symbol(combo_rows)
        if combo_rows
        else []
    )
    selected_combos = ranked_combos[:max_candidates]
    if selected_puts or selected_calls or selected_combos:
        event_snapshot, event_snapshot_reason = _load_event_snapshot(
            base=base_path,
            run_id=run_id_norm,
            source_artifacts=source_artifacts,
            data_gaps=data_gaps,
        )
    else:
        event_snapshot, event_snapshot_reason = {}, ""

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
            snapshot=event_snapshot,
            snapshot_reason=event_snapshot_reason,
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
            snapshot=event_snapshot,
            snapshot_reason=event_snapshot_reason,
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
            snapshot=event_snapshot,
            snapshot_reason=event_snapshot_reason,
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

    if advice_authority["mode"] == "v2":
        for row in position_advice_rows:
            positions.append(_position_advice_position_view(row))
            if row.get("actionable") is True:
                actions.append(
                    _position_advice_action(row, account=account_norm)
                )
    else:
        for row in close_rows:
            positions.append(_position_view(row))
            actions.append(_close_action(row, account=account_norm))

    portfolio_context = _load_json_artifact(
        path=state_dir / "portfolio_context.json",
        run_account_dir=run_account_dir,
        source_kind="portfolio_context",
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
        required=True,
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
    prefetch = _load_json_artifact(
        path=state_dir / "required_data_prefetch_summary.json",
        run_account_dir=run_account_dir,
        source_kind="required_data_prefetch_summary",
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
        required=False,
    )
    _append_prefetch_gaps(prefetch, market=market_norm, data_gaps=data_gaps)

    reject_logs = sorted(run_account_dir.glob("*_reject_log.csv"))
    rejections = _build_market_rejection_summary(
        trace_path=trace_path,
        reject_log_paths=reject_logs,
        account=account_norm,
        run_id=run_id_norm,
        market=market_norm,
        max_categories=_positive_int(
            _daily_brief_config(config_map).get("max_rejection_reasons"),
            default=5,
        ),
        run_account_dir=run_account_dir,
    )
    if trace_path.exists():
        source_artifacts.append(
            {
                "kind": "candidate_filter_trace",
                "path": _source_path(run_account_dir, trace_path),
                "row_count": _count_jsonl_rows(trace_path),
            }
        )
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
    if strategy_failures and not (put_rows or call_rows or combo_rows):
        blockers.append("candidate_strategy_execution_failed")
    if all_required_context_unavailable:
        blockers.append("all_required_account_capacity_sources_unavailable")
    if not cash_total_reliable:
        blockers.append("cash_total_unavailable")
    if advice_authority["mode"] == "authority_conflict":
        blockers.append(
            str(
                advice_authority.get("blocker")
                or "position_advice_authority_conflict"
            )
        )

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
            ranked_puts=ranked_puts,
            ranked_calls=ranked_calls,
            combo_rows=combo_rows,
            event_snapshot=event_snapshot,
            event_snapshot_reason=event_snapshot_reason,
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
            "rejections": _json_safe(rejections),
            "events": events,
            "data_gaps": deduped_data_gaps,
            "source_artifacts": _dedupe_source_artifacts(source_artifacts),
            "position_advice_preview": _json_safe(
                advice_authority.get("preview") or {}
            ),
            "notification_authority": _daily_brief_notification_authority(
                advice_authority,
                account=account_norm,
                account_run_id=run_id_norm,
            ),
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
        )
    return out


def _load_sell_put_candidates(
    *,
    run_account_dir: Path,
    market: str,
    source_artifacts: list[dict[str, Any]],
    data_gaps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    labeled_suffix = "_sell_put_candidates_labeled.csv"
    raw_suffix = "_sell_put_candidates.csv"
    labeled_paths = sorted(run_account_dir.glob(f"*{labeled_suffix}"))
    raw_paths = sorted(run_account_dir.glob(f"*{raw_suffix}"))

    if not labeled_paths and not raw_paths:
        data_gaps.append(
            {"scope": "strategy", "strategy_family": "sell_put", "reason": "source_artifact_missing"}
        )
        return [], False

    labeled_keys = {path.name[: -len(labeled_suffix)] for path in labeled_paths}
    for raw_path in raw_paths:
        artifact_key = raw_path.name[: -len(raw_suffix)]
        if artifact_key in labeled_keys:
            continue
        data_gaps.append(
            {
                "scope": "source",
                "strategy_family": "sell_put",
                "artifact_key": artifact_key,
                "path": _source_path(run_account_dir, raw_path),
                "reason": "canonical_labeled_artifact_missing",
            }
        )

    if not labeled_paths:
        return [], False

    return _load_candidate_family(
        run_account_dir=run_account_dir,
        market=market,
        family="sell_put",
        paths_to_try=labeled_paths,
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
        validate_empty_schema=True,
    )


def _load_candidate_family(
    *,
    run_account_dir: Path,
    market: str,
    family: str,
    paths_to_try: list[Path],
    source_artifacts: list[dict[str, Any]],
    data_gaps: list[dict[str, Any]],
    validate_empty_schema: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    available = False
    if not paths_to_try:
        data_gaps.append({"scope": "strategy", "strategy_family": family, "reason": "source_artifact_missing"})
        return rows, available

    for path in paths_to_try:
        try:
            frame = pd.read_csv(path)
        except pd.errors.EmptyDataError as exc:
            if validate_empty_schema and not _is_controlled_empty_candidate_artifact(path):
                data_gaps.append(
                    {
                        "scope": "source",
                        "strategy_family": family,
                        "path": _source_path(run_account_dir, path),
                        "reason": "csv_unavailable",
                        "error_type": type(exc).__name__,
                    }
                )
                continue
            available = True
            source_artifacts.append(
                {
                    "kind": f"{family}_candidates",
                    "path": _source_path(run_account_dir, path),
                    "row_count": 0,
                }
            )
            continue
        except (OSError, pd.errors.ParserError, UnicodeError) as exc:
            data_gaps.append(
                {
                    "scope": "source",
                    "strategy_family": family,
                    "path": _source_path(run_account_dir, path),
                    "reason": "csv_unavailable",
                    "error_type": type(exc).__name__,
                }
            )
            continue
        if validate_empty_schema and frame.empty and not _has_minimum_candidate_columns(frame):
            data_gaps.append(
                {
                    "scope": "source",
                    "strategy_family": family,
                    "path": _source_path(run_account_dir, path),
                    "reason": "csv_unavailable",
                    "error_type": "SchemaError",
                }
            )
            continue
        available = True
        market_rows: list[dict[str, Any]] = []
        for source_row, raw in enumerate(frame.to_dict("records"), start=1):
            row = _json_safe(dict(raw))
            row_market = _row_market(row)
            if row_market is None:
                data_gaps.append(
                    {
                        "scope": "row",
                        "strategy_family": family,
                        "path": _source_path(run_account_dir, path),
                        "source_row": source_row,
                        "reason": "symbol_market_unavailable",
                    }
                )
                continue
            if row_market != market:
                continue
            row["_source_path"] = _source_path(run_account_dir, path)
            row["_source_row"] = source_row
            market_rows.append(row)
        source_artifacts.append(
            {
                "kind": f"{family}_candidates",
                "path": _source_path(run_account_dir, path),
                "row_count": len(market_rows),
            }
        )
        rows.extend(market_rows)
    return rows, available


def _is_controlled_empty_candidate_artifact(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size > 2:
            return False
        with path.open("rb") as handle:
            content = handle.read(2)
    except OSError:
        return False
    return content in {b"\n", b"\r\n"}


def _has_minimum_candidate_columns(frame: pd.DataFrame) -> bool:
    columns = {str(column).strip() for column in frame.columns}
    return "symbol" in columns and bool(columns.intersection({"contract_symbol", "code"}))


def _daily_brief_advice_authority(
    *,
    base: Path,
    state_dir: Path,
    account: str,
    config: Mapping[str, Any],
    now_utc: datetime,
) -> dict[str, Any]:
    summary_path = state_dir / "position_advice_sources.v2.json"
    scope_id = scope_for(account)
    if not summary_path.exists():
        scope_dir = position_advice_state_root(base) / scope_id
        historical = (
            scope_dir.exists()
            and scope_dir.is_dir()
            and any(
                item.name != ".current.lock" for item in scope_dir.iterdir()
            )
        )
        if historical:
            return {
                "mode": "authority_conflict",
                "available": False,
                "blocker": "position_advice_identity_source_missing",
                "rows": [],
                "preview": {},
            }
        return {
            "mode": "v1",
            "available": True,
            "blocker": None,
            "rows": [],
            "preview": {},
        }
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if not isinstance(summary, dict):
            raise ValueError("position advice source summary is not an object")
        if str(summary.get("account") or "").strip().lower() != account:
            raise ValueError("position advice source account mismatch")
        source = str(
            summary.get("normalized_portfolio_source") or ""
        ).strip()
        identity_hash = str(
            summary.get("portfolio_account_identity_hash") or ""
        ).strip()
        portfolio_cfg = (
            config.get("portfolio")
            if isinstance(config.get("portfolio"), Mapping)
            else {}
        )
        data_config_path = resolve_data_config_path(
            base=base,
            data_config=portfolio_cfg.get("data_config"),
        )
        result = read_position_advice_v2_from_ledger(
            base=base,
            normalized_account=account,
            normalized_portfolio_source=source,
            portfolio_account_identity_hash=identity_hash,
            data_config_path=data_config_path,
            now=now_utc,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "mode": "authority_conflict",
            "available": False,
            "blocker": "position_advice_authority_resolution_failed",
            "rows": [],
            "preview": {},
        }

    mode = str(result.get("authority_mode") or "").strip()
    freshness = str(
        dict(result.get("freshness") or {}).get("status") or ""
    ).strip()
    preview = {
        "authority_mode": mode or None,
        "availability_status": result.get("availability_status"),
        "freshness": dict(result.get("freshness") or {}),
        "portfolio_plan_id": result.get("portfolio_plan_id"),
        "account_run_id": result.get("account_run_id"),
        "row_count": result.get("row_count"),
        "actionable_count": result.get("actionable_count"),
        "model_actionable_count": result.get("model_actionable_count"),
    }
    authority_common = {
        "resolved_mode": mode or None,
        "authority_generation": result.get("authority_generation"),
        "authority_policy_hash": result.get("authority_policy_hash"),
        "normalized_portfolio_source": source,
        "portfolio_account_identity_hash": identity_hash,
    }
    if mode == "v1":
        return {
            "mode": "v1",
            "available": True,
            "blocker": None,
            "rows": [],
            "preview": preview,
            **authority_common,
        }
    if mode == "v2_shadow":
        return {
            "mode": "v1",
            "available": True,
            "blocker": None,
            "rows": [],
            "preview": preview,
            **authority_common,
        }
    if mode != "v2":
        return {
            "mode": "authority_conflict",
            "available": False,
            "blocker": "position_advice_authority_conflict",
            "rows": [],
            "preview": preview,
            **authority_common,
        }
    available = (
        result.get("availability_status") == "available"
        and freshness == "fresh"
    )
    return {
        "mode": "v2",
        "available": available,
        "blocker": (
            None if available else f"position_advice_{freshness or 'unavailable'}"
        ),
        "rows": (
            [
                dict(item)
                for item in result.get("rows") or []
                if isinstance(item, Mapping)
            ]
            if available
            else []
        ),
        "portfolio_plan_id": result.get("portfolio_plan_id"),
        "preview": preview,
        **authority_common,
    }


def _daily_brief_notification_authority(
    advice_authority: Mapping[str, Any],
    *,
    account: str,
    account_run_id: str,
) -> dict[str, Any]:
    selected = str(advice_authority.get("mode") or "").strip()
    available = bool(advice_authority.get("available"))
    allowed = selected == "v1" or (selected == "v2" and available)
    resolved_mode = str(
        advice_authority.get("resolved_mode") or selected
    ).strip()
    token: dict[str, Any] | None = None
    source = str(
        advice_authority.get("normalized_portfolio_source") or ""
    ).strip()
    identity_hash = str(
        advice_authority.get("portfolio_account_identity_hash") or ""
    ).strip()
    if allowed and source and len(identity_hash) == 64:
        try:
            token = build_notification_authority_token(
                normalized_account=account,
                normalized_portfolio_source=source,
                portfolio_account_identity_hash=identity_hash,
                selected_advice_contract=selected,
                resolved_mode=resolved_mode,
                authority_generation=advice_authority.get(
                    "authority_generation"
                ),
                authority_policy_hash=advice_authority.get(
                    "authority_policy_hash"
                ),
                account_run_id=account_run_id,
            )
        except (TypeError, ValueError):
            allowed = False
    if allowed and token is None:
        allowed = False
    return {
        "selected_advice_contract": (
            selected if selected in {"v1", "v2"} else None
        ),
        "resolved_mode": resolved_mode or None,
        "authority_generation": advice_authority.get(
            "authority_generation"
        ),
        "authority_policy_hash": advice_authority.get(
            "authority_policy_hash"
        ),
        "notification_allowed": allowed,
        "blocker": (
            None
            if allowed
            else str(
                advice_authority.get("blocker")
                or "position_advice_notification_authority_unavailable"
            )
        ),
        "token": token,
    }


def _load_close_advice(
    *,
    path: Path,
    run_account_dir: Path,
    market: str,
    source_artifacts: list[dict[str, Any]],
    data_gaps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    if not path.exists():
        data_gaps.append({"scope": "strategy", "strategy_family": "close_advice", "reason": "source_artifact_missing"})
        return [], False
    try:
        frame = pd.read_csv(path)
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


def _load_event_snapshot(
    *,
    base: Path,
    run_id: str,
    source_artifacts: list[dict[str, Any]],
    data_gaps: list[dict[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], str]:
    path = paths.run_state_dir(base, run_id) / "event_snapshot.json"
    display_path = "state/event_snapshot.json"
    if not path.exists():
        data_gaps.append(
            {
                "scope": "event",
                "kind": "event_snapshot",
                "strategy_family": "candidate_event_risk",
                "reason": "event_snapshot_missing",
            }
        )
        return {}, "event_snapshot_missing"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        data_gaps.append(
            {
                "scope": "event",
                "kind": "event_snapshot",
                "strategy_family": "candidate_event_risk",
                "path": display_path,
                "reason": "event_snapshot_malformed",
                "error_type": type(exc).__name__,
            }
        )
        return {}, "event_snapshot_malformed"
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1 or not isinstance(raw.get("symbols"), Mapping):
        data_gaps.append(
            {
                "scope": "event",
                "kind": "event_snapshot",
                "strategy_family": "candidate_event_risk",
                "path": display_path,
                "reason": "event_snapshot_malformed",
            }
        )
        return {}, "event_snapshot_malformed"

    symbols: dict[str, Mapping[str, Any]] = {}
    malformed_symbols = 0
    for raw_symbol, item in raw["symbols"].items():
        symbol = canonical_symbol(raw_symbol) or _text(raw_symbol).upper()
        if not symbol or not isinstance(item, Mapping):
            malformed_symbols += 1
            continue
        symbols[symbol] = item
    if malformed_symbols:
        data_gaps.append(
            {
                "scope": "event",
                "kind": "event_snapshot",
                "strategy_family": "candidate_event_risk",
                "path": display_path,
                "reason": "event_snapshot_symbol_items_malformed",
                "count": malformed_symbols,
            }
        )
    source_artifacts.append(
        {"kind": "event_snapshot", "path": display_path, "row_count": len(symbols)}
    )
    return symbols, ""


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


def _build_market_rejection_summary(
    *,
    trace_path: Path,
    reject_log_paths: list[Path],
    account: str,
    run_id: str,
    market: str,
    max_categories: int,
    run_account_dir: Path,
) -> dict[str, Any]:
    """Reuse the existing rejection classifier after market-qualifying raw rows."""

    trace_rows = candidate_rejections._read_trace_rows(trace_path)
    reject_log_rows = candidate_rejections._read_reject_log_rows(reject_log_paths)
    source = "trace" if trace_rows else ("reject_log" if reject_log_rows else "none")
    raw_rows = trace_rows if trace_rows else reject_log_rows
    rows = [
        row
        for row in raw_rows
        if candidate_rejections._matches_account(row, account)
        and candidate_rejections._matches_run_id(row, run_id)
        and _text(row.get("function")).lower() in candidate_rejections.SCAN_FUNCTIONS
        and _row_market(row) == market
    ]
    accepted_rows = [row for row in rows if _text(row.get("status")).lower() == "accepted"]
    rejected_rows = [row for row in rows if candidate_rejections._row_is_rejection(row)]

    category_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rejected_rows:
        category_rows[candidate_rejections._category_for_row(row)].append(row)

    top_categories: list[dict[str, Any]] = []
    for category, grouped_rows in sorted(
        category_rows.items(),
        key=lambda item: (-len(item[1]), candidate_rejections._category_sort_key(item[0])),
    )[:max_categories]:
        rule_counts = Counter(candidate_rejections._rule_for_row(row) for row in grouped_rows)
        top_categories.append(
            {
                "category": category,
                "label": candidate_rejections.CATEGORY_LABELS.get(category, category),
                "count": len(grouped_rows),
                "rule_counts": dict(rule_counts.most_common(max(1, max_categories))),
                "rule_labels": {
                    rule: candidate_rejections._rule_label(rule)
                    for rule, _count in rule_counts.most_common(max(1, max_categories))
                },
                "function_counts": dict(
                    Counter(_text(row.get("function")).lower() for row in grouped_rows).most_common()
                ),
                "sample_symbols": candidate_rejections._sample_symbols(grouped_rows),
            }
        )

    return {
        "schema_version": candidate_rejections.SCHEMA_VERSION,
        "available": source != "none",
        "source": source,
        "trace_path": _source_path(run_account_dir, trace_path) if trace_path.exists() else None,
        "reject_log_paths": [_source_path(run_account_dir, item) for item in reject_log_paths],
        "account": account,
        "run_id": run_id,
        "market": market,
        "accepted_count": len(accepted_rows),
        "accepted_function_counts": dict(
            Counter(_text(row.get("function")).lower() for row in accepted_rows).most_common()
        ),
        "total_rejected": len(rejected_rows),
        "status_counts": dict(
            Counter(_text(row.get("status")).lower() or "rejected" for row in rows).most_common()
        ),
        "function_counts": dict(
            Counter(_text(row.get("function")).lower() for row in rejected_rows).most_common()
        ),
        "risk_alerts": candidate_rejections._risk_alerts(rejected_rows),
        "top_categories": top_categories,
    }


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
    event_snapshot: Mapping[str, Mapping[str, Any]],
    event_snapshot_reason: str,
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
                snapshot=event_snapshot,
                snapshot_reason=event_snapshot_reason,
            )
            representative = _candidate_view(
                row,
                family=family,
                rank=rank,
                capacity=capacity,
                event_risk=event_risk,
            )
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
        cash_required_cny=row.get("cash_required_cny"),
        cash_free_cny=row.get("cash_free_cny"),
        cash_free_total_cny=row.get("cash_free_total_cny"),
        cash_required_usd=row.get("cash_required_usd"),
        cash_free_usd=row.get("cash_free_usd"),
    )
    if result.basis is None or result.cash_required is None or result.cash_free is None or result.cash_required <= 0:
        return None
    contracts = max(0, int(float(result.cash_free) // float(result.cash_required)))
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
            "put_contract_symbol": _text(row.get("put_contract_symbol")).upper(),
            "call_contract_symbol": _text(row.get("call_contract_symbol")).upper(),
            "put_leg_role": _text(row.get("put_leg_role") or "funding_put"),
            "call_leg_role": _text(row.get("call_leg_role") or "participation_call"),
            "put_expiration": _text(row.get("put_expiration") or row.get("expiration")),
            "call_expiration": _text(row.get("call_expiration") or row.get("expiration")),
            "put_strike": _number(row.get("put_strike")),
            "call_strike": _number(row.get("call_strike")),
            "put_sell_reference": put_sell_reference,
            "call_buy_reference": call_buy_reference,
            "priority": _priority_from_row(row, default="P1"),
            "metrics": _candidate_metrics(row, rank=rank),
            "capacity": dict(capacity or {}),
            "event_risk": dict(event_risk or {}),
            "source": _source_view(row),
        }
    return {
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
    tier = _text(row.get("tier")).lower()
    evaluation = _text(row.get("evaluation_status") or row.get("quote_status")).lower()
    state = "active" if tier in {"strong", "medium"} else "observe"
    if evaluation in {"unavailable", "not_evaluable", "blocked", "error"} or _text(row.get("close_action")).lower() == "not_evaluable":
        state = "blocked"
    return {
        "priority": _priority_from_row(row, default="P2"),
        "state": state,
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
        "title": _text(row.get("tier_label") or "平仓建议"),
        "reason": _text(row.get("reason")),
        "close_action": _text(row.get("close_action")),
        "metrics": {
            key: _json_safe(row.get(key))
            for key in (
                "contracts_open",
                "close_mid",
                "dte",
                "capture_ratio",
                "remaining_premium",
                "realized_if_close",
                "remaining_annualized_return",
            )
            if row.get(key) is not None
        },
        "source": _source_view(row),
    }


def _position_view(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "position_lot_id",
        "strategy_group_id",
        "leg_role",
        "symbol",
        "option_type",
        "expiration",
        "strike",
        "contract_symbol",
        "tier",
        "tier_label",
        "reason",
        "close_action",
        "evaluation_status",
        "quote_status",
    )
    out = {field: _json_safe(row.get(field)) for field in fields}
    out["metrics"] = {
        key: _json_safe(row.get(key))
        for key in (
            "close_mid",
            "realized_if_close",
            "remaining_annualized_return",
        )
        if row.get(key) is not None
    }
    out["strategy_family"] = _text(
        row.get("strategy_family") or row.get("strategy") or "close_advice"
    ).lower()
    return out


def _position_advice_action(
    row: Mapping[str, Any],
    *,
    account: str,
) -> dict[str, Any]:
    recommendation = _text(row.get("recommendation")).lower()
    action_labels = {
        "roll": "滚动当前期权",
        "replace": "替换当前期权腿",
        "reallocate": "重新分配期权资金",
        "review": "人工核查持仓事实",
    }
    reasons = [
        str(item)
        for item in row.get("reason_codes") or []
        if str(item)
    ]
    return {
        "priority": "P1" if recommendation != "review" else "P0",
        "state": "active",
        "action_type": f"position_{recommendation}",
        "strategy_family": _text(row.get("strategy_family")).lower(),
        "account": account,
        "symbol": _text(row.get("symbol")).upper(),
        "option_type": _text(row.get("option_type")).lower(),
        "side": _text(row.get("side")).lower(),
        "expiration": _text(row.get("expiration")),
        "strike": row.get("strike"),
        "contract_symbol": _text(row.get("contract_symbol")).upper(),
        "position_lot_id": _text(row.get("position_id")),
        "strategy_group_id": _text(row.get("strategy_group_id")),
        "leg_role": _text(row.get("leg_role")).lower(),
        "title": action_labels.get(recommendation, "持仓建议"),
        "reason": "; ".join(reasons),
        "recommendation": recommendation,
        "portfolio_plan_id": row.get("portfolio_plan_id"),
        "execution_order": row.get("execution_order"),
        "depends_on": list(row.get("depends_on") or []),
        "requires_user_confirmation": True,
        "metrics": {
            key: _json_safe(row.get(key))
            for key in (
                "current_daily_carry",
                "candidate_daily_carry",
                "friction",
                "net_carry_improvement_H",
                "net_carry_improvement_H_base_cny",
                "payback_days",
            )
            if row.get(key) is not None
        },
        "source": {
            "kind": "position_advice_v2",
            "position_id": row.get("position_id"),
            "portfolio_plan_id": row.get("portfolio_plan_id"),
        },
    }


def _position_advice_position_view(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    out = {
        key: _json_safe(row.get(key))
        for key in (
            "position_id",
            "strategy_group_id",
            "leg_role",
            "symbol",
            "option_type",
            "side",
            "expiration",
            "strike",
            "contract_symbol",
            "strategy_family",
            "lifecycle_state",
            "group_structure_state",
            "recommendation",
            "actionable",
            "action_scope",
            "reason_codes",
            "best_candidate",
            "resource_deltas",
            "leg_plan",
            "execution_order",
            "depends_on",
            "promotion_scope_status",
        )
    }
    out["position_lot_id"] = out.pop("position_id")
    out["evaluation_status"] = (
        "evaluable" if row.get("recommendation") != "not_evaluable"
        else "not_evaluable"
    )
    out["quote_status"] = (
        "fresh" if row.get("quote_as_of") else "unavailable"
    )
    out["close_action"] = row.get("recommendation")
    out["metrics"] = {
        key: _json_safe(row.get(key))
        for key in (
            "current_extrinsic",
            "current_daily_carry",
            "current_capital_efficiency",
            "candidate_daily_carry",
            "candidate_capital_efficiency",
            "comparison_horizon_days",
            "friction",
            "net_carry_improvement_H",
            "net_carry_improvement_H_base_cny",
            "payback_days",
        )
        if row.get(key) is not None
    }
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
    snapshot: Mapping[str, Mapping[str, Any]],
    snapshot_reason: str,
) -> dict[str, Any]:
    symbol = canonical_symbol(row.get("symbol")) or _text(row.get("symbol")).upper()
    if family == "combo_yield":
        expirations = {
            "put": row.get("put_expiration") or row.get("expiration"),
            "call": row.get("call_expiration") or row.get("expiration"),
        }
    else:
        expirations = {"contract": row.get("expiration") or row.get("expiration_ymd")}
    return build_candidate_event_risk(
        symbol=symbol,
        market_trading_date=market_date,
        expirations=expirations,
        snapshot_item=snapshot.get(symbol),
        snapshot_reason=snapshot_reason,
    )


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
    if isinstance(symbols, Mapping):
        for symbol, item in symbols.items():
            if symbol_market(symbol) != market or not isinstance(item, Mapping):
                continue
            status = _text(item.get("status") or item.get("source_status")).lower()
            if status and status not in {"ok", "ready", "success", "available"}:
                data_gaps.append(
                    {
                        "scope": "symbol",
                        "symbol": _text(symbol).upper(),
                        "reason": _text(item.get("reason") or status),
                        "source": "required_data_prefetch_summary",
                    }
                )
    summary = prefetch.get("summary")
    if isinstance(summary, Mapping) and int(_number(summary.get("errors")) or 0) > 0:
        data_gaps.append(
            {
                "scope": "prefetch",
                "market": market,
                "reason": "required_data_prefetch_errors",
                "count": int(_number(summary.get("errors")) or 0),
            }
        )


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
        key = repr(sorted(safe.items(), key=lambda pair: pair[0]))
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
