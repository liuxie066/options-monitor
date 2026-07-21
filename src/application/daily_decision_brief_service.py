from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from domain.domain.daily_decision_brief import normalize_daily_decision_brief
from domain.domain.engine import rank_candidate_rows
from domain.domain.risk_capacity import compute_sell_call_share_capacity, compute_sell_put_cash_capacity
from domain.domain.symbol_identity import symbol_market
from domain.storage import paths
from src.application import candidate_reject_summary as candidate_rejections
from src.application.multi_tick.misc import AccountResult


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

    ranked_puts = rank_candidate_rows(put_rows, mode="put") if put_rows else []
    ranked_calls = rank_candidate_rows(call_rows, mode="call") if call_rows else []
    selected_puts = ranked_puts[:max_candidates]
    selected_calls = ranked_calls[:max_candidates]
    selected_combos = combo_rows[:max_candidates]

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
        required_context_rows += 1
        if cap is None:
            required_context_missing += 1
            data_gaps.append(_row_gap(row, "sell_put", "cash_capacity_unavailable"))
        put_capacity_rows.append((row, cap))
        candidate_payloads["sell_put"].append(_candidate_view(row, family="sell_put", rank=rank, capacity=cap))
        if cap is not None:
            actions.append(_candidate_action(row, family="sell_put", account=account_norm, rank=rank, capacity=cap))
    if put_capacity_rows:
        first_known = next((item for _row, item in put_capacity_rows if item is not None), None)
        if first_known is not None:
            capacity["sell_put"] = first_known

    call_capacity_rows: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for rank, row in enumerate(selected_calls, start=1):
        cap = _covered_call_capacity(row)
        required_context_rows += 1
        if cap is None:
            required_context_missing += 1
            data_gaps.append(_row_gap(row, "covered_call", "share_capacity_unavailable"))
        call_capacity_rows.append((row, cap))
        candidate_payloads["covered_call"].append(
            _candidate_view(row, family="covered_call", rank=rank, capacity=cap)
        )
        if cap is not None:
            actions.append(
                _candidate_action(row, family="covered_call", account=account_norm, rank=rank, capacity=cap)
            )
    if call_capacity_rows:
        first_known = next((item for _row, item in call_capacity_rows if item is not None), None)
        if first_known is not None:
            capacity["covered_call"] = first_known

    for rank, row in enumerate(selected_combos, start=1):
        candidate_payloads["combo_yield"].append(_candidate_view(row, family="combo_yield", rank=rank))
        actions.append(_candidate_action(row, family="combo_yield", account=account_norm, rank=rank))

    for row in close_rows:
        positions.append(_position_view(row))
        actions.append(_close_action(row, account=account_norm))

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

    trace_path = run_account_dir / "candidate_filter_trace.jsonl"
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
    if all_decision_sources_unavailable:
        blockers.append("all_structured_decision_sources_unavailable")
    if all_required_context_unavailable:
        blockers.append("all_required_account_capacity_sources_unavailable")

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
    data_as_of = _latest_as_of(metrics, prefetch, fallback=effective_now).isoformat()
    events = _candidate_events([*selected_puts, *selected_calls, *selected_combos])
    deduped_actions = _dedupe_actions(actions)
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
            "candidates": candidate_payloads,
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
        "source": _source_view(row),
    }


def _candidate_view(
    row: Mapping[str, Any],
    *,
    family: str,
    rank: int,
    capacity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if family == "combo_yield":
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
            "priority": _priority_from_row(row, default="P1"),
            "metrics": _candidate_metrics(row, rank=rank),
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


def _candidate_events(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        event_flag = row.get("event_flag")
        event_types = _text(row.get("event_types") or row.get("event_type"))
        event_dates = _text(row.get("event_dates") or row.get("event_date"))
        if not bool(event_flag) and not event_types and not event_dates:
            continue
        item = {
            "symbol": _text(row.get("symbol")).upper(),
            "event_types": event_types,
            "event_dates": event_dates,
            "source_status": _text(row.get("event_source_status")),
        }
        identity = (item["symbol"], event_types, event_dates)
        if identity in seen:
            continue
        seen.add(identity)
        out.append(item)
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
