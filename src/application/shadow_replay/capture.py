from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from domain.domain.engine import CandidateScoreWeights, explain_candidate_rank

from src.application.candidate_filter_trace import (
    build_candidate_replay_fields,
    infer_trace_scope_from_path,
    read_candidate_filter_trace,
)
from src.application.shadow_replay.analysis import analyze_rows
from src.application.shadow_replay.common import (
    CANDIDATE_SNAPSHOT_SCHEMA_VERSION,
    DATASET_FILES,
    DATASET_SCHEMA_VERSION,
    FILTER_DECISION_SCHEMA_VERSION,
    MARK_PATH_SCHEMA_VERSION,
    OUTCOME_FACT_SCHEMA_VERSION,
    RANK_SNAPSHOT_SCHEMA_VERSION,
    abs_first_float,
    account_hint,
    dataset_output_dir,
    default_dataset_id,
    first_float,
    glob_many,
    normal_status,
    read_csv_rows,
    read_jsonl,
    resolve_many,
    resolve_optional,
    safe_rel,
    safety_payload,
    strategy_hint,
    strategy_mode,
    text,
    unique,
    utc_now,
    write_json,
    write_jsonl,
)


@dataclass(frozen=True)
class ShadowReplaySourceSelection:
    repo_root: Path
    run_id: str | None = None
    runs_root: Path | None = None
    run_dir: Path | None = None
    report_dir: Path | None = None
    candidate_paths: tuple[Path, ...] = ()
    trace_paths: tuple[Path, ...] = ()
    reject_log_paths: tuple[Path, ...] = ()
    mark_paths: tuple[Path, ...] = ()
    outcome_paths: tuple[Path, ...] = ()


def build_shadow_replay_dataset(
    *,
    repo_root: Path,
    run_id: str | None = None,
    runs_root: str | Path | None = None,
    run_dir: str | Path | None = None,
    report_dir: str | Path | None = None,
    candidate_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    trace_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    reject_log_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    mark_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    outcome_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    output_dir: str | Path | None = None,
    dataset_root: str | Path | None = None,
    dataset_id: str | None = None,
    latest_scanned_run: bool = False,
) -> dict[str, Any]:
    """Build a local replay dataset from existing read-only scan artifacts."""

    base = repo_root.resolve()
    run_id_text = (str(run_id).strip() or None) if run_id else None
    runs_root_path = resolve_optional(runs_root, base=base)
    run_dir_path = resolve_optional(run_dir, base=base)
    latest_selection: dict[str, Any] = {
        "requested": bool(latest_scanned_run),
        "found": None,
        "path": None,
        "run_id": None,
        "searched_count": 0,
        "skipped_without_evidence_count": 0,
    }
    if run_dir_path is None and bool(latest_scanned_run):
        run_dir_path, latest_selection = latest_shadow_replay_run_dir(
            repo_root=base,
            runs_root=runs_root_path,
        )
        if run_dir_path is None:
            raise ValueError("latest scanned run with shadow replay evidence not found")
        run_id_text = run_dir_path.name
    elif run_dir_path is None and run_id_text:
        root = runs_root_path or (base / "output_runs").resolve()
        run_dir_path = (root / run_id_text).resolve()
    selection = ShadowReplaySourceSelection(
        repo_root=base,
        run_id=run_id_text,
        runs_root=runs_root_path,
        run_dir=run_dir_path,
        report_dir=resolve_optional(report_dir, base=base),
        candidate_paths=tuple(resolve_many(candidate_paths, base=base)),
        trace_paths=tuple(resolve_many(trace_paths, base=base)),
        reject_log_paths=tuple(resolve_many(reject_log_paths, base=base)),
        mark_paths=tuple(resolve_many(mark_paths, base=base)),
        outcome_paths=tuple(resolve_many(outcome_paths, base=base)),
    )
    resolved_candidates = candidate_paths_from_selection(selection)
    resolved_traces = trace_paths_from_selection(selection)
    resolved_reject_logs = reject_log_paths_from_selection(selection)
    resolved_marks = mark_paths_from_selection(selection)
    resolved_outcomes = outcome_paths_from_selection(selection)

    candidate_rows = accepted_candidate_snapshots(resolved_candidates, base=base)
    filter_decisions = filter_decision_rows(resolved_traces, resolved_reject_logs, base=base)
    rejected_rows = candidate_snapshots_from_filter_decisions(filter_decisions)
    candidate_snapshots = dedupe_snapshots(candidate_rows + rejected_rows)
    rank_snapshots = rank_snapshots_for_candidates(candidate_snapshots)
    mark_snapshots = read_replay_rows(resolved_marks, schema_version=MARK_PATH_SCHEMA_VERSION, base=base)
    outcome_facts = read_replay_rows(resolved_outcomes, schema_version=OUTCOME_FACT_SCHEMA_VERSION, base=base)

    ds_id = str(dataset_id or "").strip() or default_dataset_id()
    dataset_root_path = resolve_optional(dataset_root, base=base)
    target = (
        (dataset_root_path / ds_id).resolve()
        if output_dir is None and dataset_root_path is not None
        else dataset_output_dir(output_dir, dataset_id=ds_id, base=base)
    )
    target.mkdir(parents=True, exist_ok=True)
    write_jsonl(target / "candidate_snapshots.jsonl", candidate_snapshots)
    write_jsonl(target / "filter_decisions.jsonl", filter_decisions)
    write_jsonl(target / "rank_snapshots.jsonl", rank_snapshots)
    write_jsonl(target / "mark_path_snapshots.jsonl", mark_snapshots)
    write_jsonl(target / "outcome_facts.jsonl", outcome_facts)

    analysis_seed = analyze_rows(
        candidate_snapshots=candidate_snapshots,
        filter_decisions=filter_decisions,
        mark_snapshots=mark_snapshots,
        outcome_facts=outcome_facts,
        min_sample=1,
    )
    manifest = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "dataset_id": ds_id,
        "created_at_utc": utc_now(),
        "dataset_dir": str(target),
        "source": {
            "run_id": selection.run_id,
            "runs_root": safe_rel(selection.runs_root, base=base),
            "run_dir": safe_rel(selection.run_dir, base=base),
            "latest_scanned_run": bool(latest_scanned_run),
            "latest_scanned_run_selection": latest_selection,
            "report_dir": safe_rel(selection.report_dir, base=base),
            "candidate_paths": [safe_rel(path, base=base) for path in resolved_candidates],
            "trace_paths": [safe_rel(path, base=base) for path in resolved_traces],
            "reject_log_paths": [safe_rel(path, base=base) for path in resolved_reject_logs],
            "mark_paths": [safe_rel(path, base=base) for path in resolved_marks],
            "outcome_paths": [safe_rel(path, base=base) for path in resolved_outcomes],
        },
        "files": {name: str((target / name).resolve()) for name in DATASET_FILES},
        "summary": analysis_seed["summary"],
        "evidence_checks": analysis_seed["evidence_checks"],
        "safety": safety_payload(writes_local_dataset=True),
    }
    write_json(target / "manifest.json", manifest)
    return manifest


def accepted_candidate_snapshots(paths: list[Path], *, base: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in paths:
        strategy = strategy_hint(path)
        mode = strategy_mode(strategy)
        scope = infer_trace_scope_from_path(path)
        account = account_hint(path) or text(scope.get("account")).lower() or None
        source_path = safe_rel(path, base=base)
        for row_number, row in enumerate(read_csv_rows(path), start=1):
            candidate_rows = _combo_pair_rows(
                row,
                strategy=strategy,
                source_path=source_path,
                run_id=text(row.get("run_id") or scope.get("run_id")) or None,
                account=account,
            )
            for candidate_row in candidate_rows:
                item = snapshot_from_row(
                    candidate_row,
                    schema_version=CANDIDATE_SNAPSHOT_SCHEMA_VERSION,
                    source_kind="candidate_csv",
                    source_path=source_path,
                    source_row_number=row_number,
                    status="accepted",
                    strategy=strategy,
                    mode=mode,
                    account_hint=account,
                )
                out.append(item)
    return out


def _combo_pair_rows(
    row: dict[str, Any],
    *,
    strategy: str | None,
    source_path: str | None,
    run_id: str | None,
    account: str | None,
) -> list[dict[str, Any]]:
    put_contract = text(row.get("put_contract_symbol"))
    call_contract = text(row.get("call_contract_symbol"))
    family = text(row.get("strategy_family") or strategy).lower().replace("-", "_")
    if family != "combo_yield" or not put_contract or not call_contract:
        return [row]

    group_id = text(row.get("strategy_group_id") or row.get("group_id")) or _combo_pair_group_id(
        row,
        source_path=source_path,
        run_id=run_id,
        account=account,
        put_contract=put_contract,
        call_contract=call_contract,
    )
    contracts = first_float(row, "contracts", "contract_count", "quantity", "qty") or 1.0
    put_contracts = first_float(row, "put_contracts") or contracts
    call_contracts = first_float(row, "call_contracts") or contracts
    put_credit = first_float(row, "put_net_credit")
    call_cost = first_float(row, "call_total_cost")
    structure_mode = text(row.get("structure_mode")).lower() or "same_expiry_pair"
    common = {
        **row,
        "net_credit": None,
        "run_id": text(row.get("run_id") or run_id) or None,
        "account": text(row.get("account") or account).lower() or None,
        "strategy_family": "combo_yield",
        "strategy_profile": text(row.get("strategy_profile") or row.get("yield_enhancement_mode")) or "combo_yield",
        "strategy_group_id": group_id,
        "candidate_pair_id": text(row.get("candidate_pair_id")) or None,
        "structure_mode": structure_mode,
        "put_expiration": text(row.get("put_expiration") or row.get("expiration") or row.get("exp")) or None,
        "put_dte": first_float(row, "put_dte", "dte"),
        "call_expiration": text(row.get("call_expiration") or row.get("expiration") or row.get("exp")) or None,
        "call_dte": first_float(row, "call_dte", "dte"),
    }
    return [
        {
            **common,
            "contract_symbol": put_contract,
            "option_type": "put",
            "mode": "put",
            "side": "short",
            "leg_role": "funding_put",
            "expiration": text(row.get("put_expiration") or row.get("expiration") or row.get("exp")) or None,
            "dte": first_float(row, "put_dte", "dte"),
            "contracts": put_contracts,
            "strike": first_float(row, "put_strike"),
            "bid": first_float(row, "put_bid"),
            "ask": first_float(row, "put_ask"),
            "mid": first_float(row, "put_mid"),
            "delta": first_float(row, "put_delta"),
            "open_interest": first_float(row, "put_open_interest"),
            "volume": first_float(row, "put_volume"),
            "spread_ratio": first_float(row, "put_spread_ratio"),
            "net_income": put_credit,
            "entry_credit": put_credit,
        },
        {
            **common,
            "contract_symbol": call_contract,
            "option_type": "call",
            "mode": "call",
            "side": "long",
            "leg_role": "participation_call",
            "expiration": text(row.get("call_expiration") or row.get("expiration") or row.get("exp")) or None,
            "dte": first_float(row, "call_dte", "dte"),
            "contracts": call_contracts,
            "strike": first_float(row, "call_strike"),
            "bid": first_float(row, "call_bid"),
            "ask": first_float(row, "call_ask"),
            "mid": first_float(row, "call_mid"),
            "delta": first_float(row, "call_delta"),
            "open_interest": first_float(row, "call_open_interest"),
            "volume": first_float(row, "call_volume"),
            "spread_ratio": first_float(row, "call_spread_ratio"),
            "net_income": -abs(call_cost) if call_cost is not None else None,
            "entry_cost": abs(call_cost) if call_cost is not None else None,
        },
    ]


def _combo_pair_group_id(
    row: dict[str, Any],
    *,
    source_path: str | None,
    run_id: str | None,
    account: str | None,
    put_contract: str,
    call_contract: str,
) -> str:
    common_parts = (
        text(row.get("run_id") or run_id or source_path),
        text(row.get("account") or account).lower(),
        text(row.get("symbol") or row.get("underlying_symbol")).upper(),
    )
    if text(row.get("structure_mode")).lower() == "staggered_expiry_pair":
        parts = (
            *common_parts,
            text(row.get("candidate_pair_id")),
            text(row.get("put_expiration") or row.get("expiration") or row.get("exp")),
            text(row.get("call_expiration") or row.get("expiration") or row.get("exp")),
            put_contract.upper(),
            call_contract.upper(),
        )
    else:
        parts = (
            *common_parts,
            text(row.get("expiration") or row.get("exp")),
            put_contract.upper(),
            call_contract.upper(),
        )
    return "combo_yield|" + "|".join(parts)


def filter_decision_rows(trace_paths: list[Path], reject_log_paths: list[Path], *, base: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in trace_paths:
        scope = infer_trace_scope_from_path(path)
        for row_number, row in enumerate(read_candidate_filter_trace(path), start=1):
            item = dict(row)
            item["schema_version"] = FILTER_DECISION_SCHEMA_VERSION
            item["source_kind"] = "candidate_filter_trace"
            item["source_path"] = safe_rel(path, base=base)
            item["source_row_number"] = row_number
            item["run_id"] = text(item.get("run_id") or scope.get("run_id")) or None
            item["account"] = text(item.get("account") or scope.get("account")).lower() or None
            item["status"] = normal_status(item.get("status") or "rejected")
            item["symbol"] = text(item.get("symbol") or item.get("underlying_symbol")).upper() or None
            item["rule"] = text(item.get("rule") or item.get("reject_rule") or item.get("reject_reason")) or None
            out.append(item)
    for path in reject_log_paths:
        strategy = strategy_hint(path)
        mode = strategy_mode(strategy)
        scope = infer_trace_scope_from_path(path)
        account = account_hint(path)
        for row_number, row in enumerate(read_csv_rows(path), start=1):
            item = {
                "schema_version": FILTER_DECISION_SCHEMA_VERSION,
                "source_kind": "reject_log_csv",
                "source_path": safe_rel(path, base=base),
                "source_row_number": row_number,
                "run_id": text(row.get("run_id") or scope.get("run_id")) or None,
                "account": text(row.get("account") or account or scope.get("account")).lower() or None,
                "symbol": text(row.get("symbol") or row.get("underlying_symbol")).upper() or None,
                "contract_symbol": text(row.get("contract_symbol") or row.get("option_symbol")) or None,
                "function": text(row.get("function") or strategy) or None,
                "mode": text(row.get("mode") or row.get("option_type")).lower() or mode,
                "status": normal_status(row.get("status") or "rejected"),
                "stage": text(row.get("engine_reject_stage") or row.get("reject_stage") or row.get("stage")) or None,
                "rule": text(row.get("engine_reject_reason") or row.get("reject_rule") or row.get("reject_reason") or row.get("rule")) or None,
                "metric_value": text(row.get("metric_value")) or None,
                "threshold": text(row.get("threshold")) or None,
                "message": text(row.get("message")) or None,
            }
            item.update(build_candidate_replay_fields(row))
            out.append(item)
    return _merge_filter_decision_rows(out)


def _merge_filter_decision_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        key = _filter_decision_merge_key(row)
        if not key:
            merged.append(row)
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = row
            merged.append(row)
            continue
        _fill_missing_decision_values(existing, row)
    return merged


def _filter_decision_merge_key(row: dict[str, Any]) -> tuple[str, ...] | None:
    contract = text(row.get("contract_symbol") or row.get("option_symbol")).upper()
    symbol = text(row.get("symbol") or row.get("underlying_symbol")).upper()
    rule = text(row.get("rule") or row.get("reject_rule") or row.get("reject_reason"))
    if not rule or not (contract or symbol):
        return None
    return (
        text(row.get("run_id")),
        text(row.get("account")).lower(),
        symbol,
        contract,
        text(row.get("mode") or row.get("option_type")).lower(),
        normal_status(row.get("status") or "rejected"),
        rule,
    )


def _fill_missing_decision_values(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key in {"schema_version", "source_kind", "source_path", "source_row_number"}:
            continue
        if _decision_value_missing(target.get(key)) and not _decision_value_missing(value):
            target[key] = value


def _decision_value_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def candidate_snapshots_from_filter_decisions(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(decisions, start=1):
        status = normal_status(row.get("status") or "rejected")
        if status not in {"rejected", "post_filtered", "ranked_below"}:
            continue
        item = snapshot_from_row(
            row,
            schema_version=CANDIDATE_SNAPSHOT_SCHEMA_VERSION,
            source_kind="filter_decision",
            source_path=row.get("source_path"),
            source_row_number=row.get("source_row_number") or idx,
            status=status,
            strategy=text(row.get("function")) or None,
            mode=text(row.get("mode")).lower() or strategy_mode(text(row.get("function")) or None),
            account_hint=text(row.get("account")).lower() or None,
        )
        item["filter_stage"] = row.get("stage")
        item["filter_rule"] = row.get("rule")
        item["filter_metric_value"] = row.get("metric_value")
        item["filter_threshold"] = row.get("threshold")
        out.append(item)
    return out


def snapshot_from_row(
    row: dict[str, Any],
    *,
    schema_version: str,
    source_kind: str,
    source_path: Any,
    source_row_number: Any,
    status: str,
    strategy: str | None,
    mode: str | None,
    account_hint: str | None,
) -> dict[str, Any]:
    mode_norm = text(row.get("mode") or row.get("option_type") or mode).lower() or None
    family = _strategy_family_value(row, strategy)
    profile = _strategy_profile_value(row, strategy=strategy, family=family)
    return {
        "schema_version": schema_version,
        "source_kind": source_kind,
        "source_path": source_path,
        "source_row_number": source_row_number,
        "status": normal_status(status),
        "strategy": strategy,
        "strategy_family": family,
        "strategy_profile": profile,
        "strategy_group_id": text(row.get("strategy_group_id") or row.get("group_id")) or None,
        "candidate_pair_id": text(row.get("candidate_pair_id")) or None,
        "structure_mode": text(row.get("structure_mode")).lower() or None,
        "leg_role": text(row.get("leg_role") or row.get("strategy_leg_role")) or None,
        "mode": mode_norm,
        "run_id": text(row.get("run_id")) or None,
        "account": text(row.get("account") or account_hint).lower() or None,
        "symbol": text(row.get("symbol") or row.get("underlying_symbol")).upper() or None,
        "contract_symbol": text(row.get("contract_symbol") or row.get("option_symbol")) or None,
        "option_type": text(row.get("option_type")).lower() or mode_norm,
        "expiration": text(row.get("expiration") or row.get("exp")) or None,
        "put_expiration": text(row.get("put_expiration")) or None,
        "put_dte": first_float(row, "put_dte"),
        "call_expiration": text(row.get("call_expiration")) or None,
        "call_dte": first_float(row, "call_dte"),
        "strike": first_float(row, "strike"),
        "side": text(row.get("side") or row.get("position_side")).lower() or None,
        "contracts": first_float(row, "contracts", "contract_count", "quantity", "qty"),
        "multiplier": first_float(row, "multiplier", "contract_multiplier"),
        "currency": text(row.get("currency")).upper() or None,
        "spot": first_float(row, "spot", "underlying_price"),
        "dte": first_float(row, "dte"),
        "delta": first_float(row, "delta", "put_delta", "call_delta"),
        "abs_delta": abs_first_float(row, "delta", "put_delta", "call_delta"),
        "iv_rv_ratio": first_float(row, "iv_rv_ratio"),
        "iv_minus_rv": first_float(row, "iv_minus_rv"),
        "premium_edge_score": first_float(row, "premium_edge_score"),
        "strike_safety_margin_pct": first_float(row, "strike_safety_margin_pct"),
        "strike_upside_margin_pct": first_float(row, "strike_upside_margin_pct"),
        "min_strike": first_float(row, "min_strike"),
        "max_strike": first_float(row, "max_strike"),
        "effective_min_strike": first_float(row, "effective_min_strike"),
        "bid": first_float(row, "bid", "option_bid"),
        "ask": first_float(row, "ask", "option_ask"),
        "mid": first_float(row, "mid", "option_mid", "mid_price"),
        "last_price": first_float(row, "last_price", "last"),
        "open_interest": first_float(row, "open_interest", "oi"),
        "volume": first_float(row, "volume", "option_volume"),
        "spread_ratio": first_float(row, "spread_ratio", "combo_spread_ratio"),
        "single_trade_concentration": first_float(row, "single_trade_concentration"),
        "event_risk_status": text(row.get("event_risk_status")) or None,
        "event_status": text(row.get("event_status")) or None,
        "event_source_status": text(row.get("event_source_status")) or None,
        "event_risk": text(row.get("event_risk")) or None,
        "has_event_before_expiry": text(row.get("has_event_before_expiry")) or None,
        "symbol_concentration_after": first_float(row, "symbol_concentration_after"),
        "portfolio_nav_cny": first_float(row, "portfolio_nav_cny", "nav_cny"),
        "assignment_notional_cny": first_float(row, "assignment_notional_cny"),
        "cash_required_cny": first_float(row, "cash_required_cny"),
        "cash_required_usd": first_float(row, "cash_required_usd"),
        "cash_free_cny": first_float(row, "cash_free_cny"),
        "cash_free_total_cny": first_float(row, "cash_free_total_cny"),
        "cash_free_usd": first_float(row, "cash_free_usd"),
        "existing_stock_value_cny_symbol": first_float(row, "existing_stock_value_cny_symbol"),
        "existing_short_put_assignment_cny_symbol": first_float(row, "existing_short_put_assignment_cny_symbol"),
        "existing_short_put_assignment_cny_total": first_float(row, "existing_short_put_assignment_cny_total"),
        "covered_notional_cny": first_float(row, "covered_notional_cny"),
        "shares_total": first_float(row, "shares_total", "shares"),
        "shares_locked": first_float(row, "shares_locked"),
        "shares_available_for_cover": first_float(row, "shares_available_for_cover"),
        "covered_contracts_available": first_float(row, "covered_contracts_available"),
        "covered_quantity": first_float(
            row,
            "covered_quantity",
            "covered_shares",
            "covered_share_quantity",
            "shares_available_for_cover",
            "covered_contracts_available",
        ),
        "cost_basis": first_float(row, "cost_basis", "underlying_cost_basis", "avg_cost", "average_cost"),
        "cost_basis_floor": first_float(row, "cost_basis_floor", "min_strike_cost_multiplier", "strike_cost_multiplier"),
        "underlying_notional_cny": first_float(row, "underlying_notional_cny"),
        "capital_at_risk_cny": first_float(row, "capital_at_risk_cny"),
        "annualized_return": first_float(
            row,
            "annualized_net_return_on_cash_basis",
            "annualized_net_premium_return",
            "annualized_net_return",
            "annualized_return",
        ),
        "net_income_cny": first_float(row, "net_income_cny", "net_credit_cny", "premium_cny"),
        "net_income": first_float(row, "net_income", "net_credit"),
        "entry_credit": first_float(row, "entry_credit"),
        "entry_cost": first_float(row, "entry_cost"),
        "put_net_credit": first_float(row, "put_net_credit"),
        "call_total_cost": first_float(row, "call_total_cost"),
        "combo_net_credit": first_float(row, "combo_net_credit"),
        "net_credit_retention": first_float(row, "net_credit_retention"),
        "call_cost_to_put_credit": first_float(row, "call_cost_to_put_credit"),
    }


def _config_value(row: dict[str, Any], *keys: str) -> Any:
    raw = row.get("config_values")
    if not isinstance(raw, dict):
        return None
    for key in keys:
        value = raw.get(key)
        if text(value):
            return value
    return None


def _strategy_family_value(row: dict[str, Any], strategy: str | None) -> str | None:
    return (
        text(
            row.get("strategy_family")
            or _config_value(row, "strategy_family", "family")
            or row.get("function")
            or strategy
        )
        or None
    )


def _strategy_profile_value(row: dict[str, Any], *, strategy: str | None, family: str | None) -> str | None:
    explicit = text(
        row.get("strategy_profile")
        or row.get("profile")
        or row.get("strategy_mode")
        or _config_value(row, "strategy_profile", "profile", "strategy")
    )
    if explicit:
        return explicit
    family_norm = text(family or row.get("function") or strategy).lower().replace("-", "_")
    if family_norm in {"sell_put", "sell_call"} and _has_short_vol_replay_fields(row):
        return "short_vol"
    return None


def _has_short_vol_replay_fields(row: dict[str, Any]) -> bool:
    return any(
        first_float(row, key) is not None
        for key in (
            "iv_rv_ratio",
            "iv_minus_rv",
            "abs_delta",
            "delta",
            "vol_edge_score",
            "delta_target_score",
        )
    )


def rank_snapshots_for_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(candidates, start=1):
        if str(row.get("status") or "") != "accepted":
            continue
        mode = str(row.get("mode") or "").strip()
        if mode not in {"put", "call"}:
            continue
        try:
            explanation = explain_candidate_rank(row, mode=mode, score_weights=CandidateScoreWeights())
        except Exception as exc:
            explanation = {"error": f"{type(exc).__name__}: {exc}"}
        out.append(
            {
                "schema_version": RANK_SNAPSHOT_SCHEMA_VERSION,
                "source_candidate_index": idx,
                "symbol": row.get("symbol"),
                "contract_symbol": row.get("contract_symbol"),
                "mode": mode,
                "rank_explanation": explanation,
            }
        )
    return out


def read_replay_rows(paths: list[Path], *, schema_version: str, base: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in paths:
        if path.suffix.lower() == ".csv":
            rows = read_csv_rows(path)
        else:
            rows = read_jsonl(path)
        for row_number, row in enumerate(rows, start=1):
            item = dict(row)
            item.setdefault("schema_version", schema_version)
            item["source_path"] = safe_rel(path, base=base)
            item["source_row_number"] = row_number
            out.append(item)
    return out


def candidate_paths_from_selection(selection: ShadowReplaySourceSelection) -> list[Path]:
    explicit = [path for path in selection.candidate_paths if path.exists()]
    if explicit:
        return unique(explicit)
    out: list[Path] = []
    for directory in source_dirs(selection):
        out.extend(
            glob_many(
                directory,
                (
                    "*sell_put_candidates*.csv",
                    "*sell_call_candidates*.csv",
                    "*combo_yield_candidates*.csv",
                    "*yield_enhancement_candidates*.csv",
                ),
            )
        )
    return unique(path for path in out if "reject_log" not in path.name.lower())


def trace_paths_from_selection(selection: ShadowReplaySourceSelection) -> list[Path]:
    explicit = [path for path in selection.trace_paths if path.exists()]
    if explicit:
        return unique(explicit)
    return unique(directory / "candidate_filter_trace.jsonl" for directory in source_dirs(selection) if (directory / "candidate_filter_trace.jsonl").exists())


def reject_log_paths_from_selection(selection: ShadowReplaySourceSelection) -> list[Path]:
    explicit = [path for path in selection.reject_log_paths if path.exists()]
    if explicit:
        return unique(explicit)
    out: list[Path] = []
    for directory in source_dirs(selection):
        out.extend(glob_many(directory, ("*candidates_reject_log*.csv", "*reject_log*.csv")))
    return unique(out)


def mark_paths_from_selection(selection: ShadowReplaySourceSelection) -> list[Path]:
    explicit = [path for path in selection.mark_paths if path.exists()]
    if explicit:
        return unique(explicit)
    out: list[Path] = []
    for directory in source_dirs(selection):
        out.extend(glob_many(directory, ("mark_path_snapshots.jsonl", "mark_path_snapshots.csv", "*mark_path*.jsonl", "*mark_path*.csv")))
    return unique(out)


def outcome_paths_from_selection(selection: ShadowReplaySourceSelection) -> list[Path]:
    explicit = [path for path in selection.outcome_paths if path.exists()]
    if explicit:
        return unique(explicit)
    out: list[Path] = []
    for directory in source_dirs(selection):
        out.extend(glob_many(directory, ("outcome_facts.jsonl", "outcome_facts.csv", "*outcome*.jsonl", "*outcome*.csv")))
    return unique(out)


def source_dirs(selection: ShadowReplaySourceSelection) -> list[Path]:
    dirs: list[Path] = []
    run_dir = selection.run_dir
    if run_dir is None and selection.run_id:
        runs_root = selection.runs_root or (selection.repo_root / "output_runs").resolve()
        run_dir = (runs_root / selection.run_id).resolve()
    for root in (run_dir, selection.report_dir):
        if root is None:
            continue
        dirs.append(root.resolve())
        accounts_dir = root / "accounts"
        if accounts_dir.exists() and accounts_dir.is_dir():
            dirs.extend(path.resolve() for path in accounts_dir.iterdir() if path.is_dir())
    if not dirs:
        dirs.append((selection.repo_root / "output_shared" / "reports").resolve())
    return unique(dirs)


def latest_shadow_replay_run_dir(*, repo_root: Path, runs_root: Path | None = None) -> tuple[Path | None, dict[str, Any]]:
    root = (runs_root or (repo_root / "output_runs")).resolve()
    searched_count = 0
    skipped_without_evidence_count = 0
    if not root.exists() or not root.is_dir():
        return None, {
            "requested": True,
            "found": False,
            "source": "runs_root_mtime",
            "runs_root": safe_rel(root, base=repo_root),
            "path": None,
            "run_id": None,
            "searched_count": 0,
            "skipped_without_evidence_count": 0,
        }
    run_dirs = sorted(
        [item.resolve() for item in root.iterdir() if item.is_dir()],
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    )
    for run_dir in run_dirs:
        searched_count += 1
        probe = ShadowReplaySourceSelection(repo_root=repo_root, run_dir=run_dir, runs_root=root)
        candidate_count = len(candidate_paths_from_selection(probe))
        trace_count = len(trace_paths_from_selection(probe))
        reject_log_count = len(reject_log_paths_from_selection(probe))
        if candidate_count or trace_count or reject_log_count:
            return run_dir, {
                "requested": True,
                "found": True,
                "source": "runs_root_mtime",
                "runs_root": safe_rel(root, base=repo_root),
                "path": safe_rel(run_dir, base=repo_root),
                "run_id": run_dir.name,
                "searched_count": searched_count,
                "skipped_without_evidence_count": skipped_without_evidence_count,
                "candidate_path_count": candidate_count,
                "trace_path_count": trace_count,
                "reject_log_path_count": reject_log_count,
            }
        skipped_without_evidence_count += 1
    return None, {
        "requested": True,
        "found": False,
        "source": "runs_root_mtime",
        "runs_root": safe_rel(root, base=repo_root),
        "path": None,
        "run_id": None,
        "searched_count": searched_count,
        "skipped_without_evidence_count": skipped_without_evidence_count,
    }


def dedupe_snapshots(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        key = (
            row.get("source_kind"),
            row.get("source_path"),
            row.get("source_row_number"),
            row.get("status"),
            row.get("symbol"),
            row.get("contract_symbol"),
            row.get("filter_rule"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out
