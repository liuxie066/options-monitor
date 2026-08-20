from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from domain.domain.assigned_stock import assigned_stock_fee_fact
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine import (
    CandidateCalculationError,
    calculate_opening_candidate_metrics,
    evaluate_opening_candidate_policy,
)
from domain.domain.wheel import (
    build_wheel_call_rank_key,
    evaluate_wheel_call_candidate,
)
from src.application.candidate_models import CandidateBaseValues, CandidateContractInput
from src.application.candidate_scanning import (
    CandidateScanConfig,
    CandidateScanDependencies,
    evidence_summary_from_decisions,
    project_evidence_scan_status,
    run_candidate_scan,
)
from src.application.required_data_snapshot import (
    FrozenRequiredDataBatch,
    FrozenRequiredDataUnavailable,
)
from src.application.short_vol_risk_context import enrich_short_vol_contract_cny_fields


def _stock_rows(read_model: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    projection = read_model.get("assigned_stock_projection")
    projection = projection if isinstance(projection, Mapping) else {}
    rows = projection.get("_all_assigned_stock_lots") or []
    return {
        str(row.get("stock_lot_id") or "").strip(): dict(row)
        for row in rows
        if isinstance(row, Mapping) and str(row.get("stock_lot_id") or "").strip()
    }


def _fee_total(stock: Mapping[str, Any], prefix: str) -> float | None:
    evidence = stock.get("fee_evidence")
    if not isinstance(evidence, list):
        return None
    total = 0.0
    matched = False
    for raw in evidence:
        if not isinstance(raw, Mapping):
            continue
        component = str(raw.get("component") or "")
        if not component.startswith(prefix) or "option_fee" not in component:
            continue
        if str(raw.get("basis") or "") not in {"actual", "estimated"}:
            return None
        try:
            total += float(raw.get("amount"))
        except (TypeError, ValueError):
            return None
        matched = True
    return total if matched else 0.0


def _batch_economics(batch: Mapping[str, Any], stock: Mapping[str, Any]) -> dict[str, Any]:
    put_fees = _fee_total(stock, "put_")
    call_fees = _fee_total(stock, "covered_call_")
    try:
        realized_put = float(stock.get("option_premium_attribution")) - float(put_fees)
        realized_calls = float(stock.get("covered_call_realized_pnl")) - float(call_fees)
        realized_stock = float(stock.get("assigned_stock_realized_pnl"))
    except (TypeError, ValueError):
        realized_put = realized_calls = realized_stock = None
    return {
        **dict(batch),
        "remaining_stock_cost_basis": stock.get("remaining_stock_cost_basis"),
        "realized_sell_put_net_pnl": realized_put,
        "realized_prior_call_net_pnl": realized_calls,
        "realized_prior_stock_sale_net_pnl": realized_stock,
        "currency": stock.get("currency"),
        "broker": stock.get("broker"),
    }


def _frames_from_snapshot(
    snapshot: FrozenRequiredDataBatch | Mapping[str, Any],
    symbols: list[str],
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    if isinstance(snapshot, FrozenRequiredDataBatch):
        frames: dict[str, pd.DataFrame] = {}
        unavailable: dict[str, str] = {}
        for symbol in symbols:
            try:
                _entry, csv_bytes = snapshot.resolve(symbol)
                frames[symbol] = pd.read_csv(BytesIO(csv_bytes))
            except (FrozenRequiredDataUnavailable, pd.errors.ParserError) as exc:
                unavailable[symbol] = getattr(exc, "reason", "required_data_invalid")
        return frames, unavailable
    supplied = snapshot.get("frames") if isinstance(snapshot, Mapping) else None
    supplied = supplied if isinstance(supplied, Mapping) else snapshot
    frames = {
        str(symbol).strip().upper(): frame.copy()
        for symbol, frame in supplied.items()
        if isinstance(frame, pd.DataFrame)
    }
    return frames, {
        symbol: "symbol_entry_missing" for symbol in symbols if symbol not in frames
    }


def _candidate_universe(
    *,
    symbols: list[str],
    frames: Mapping[str, pd.DataFrame],
    input_root: Path,
    exchange_rate_converter: Any,
    decision_time_ms: int,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    decisions: list[dict[str, Any]] = []

    def compute(contract: CandidateContractInput) -> dict[str, Any] | None:
        try:
            return calculate_opening_candidate_metrics(
                contract.to_gate_payload(),
                mode="call",
                avg_cost=None,
                now_utc=datetime.fromtimestamp(
                    decision_time_ms / 1000,
                    tz=timezone.utc,
                ),
            )
        except CandidateCalculationError:
            return None

    def build(
        contract: CandidateContractInput,
        base: CandidateBaseValues,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        payload = contract.to_gate_payload()
        payload.update(metrics)
        payload.update(
            enrich_short_vol_contract_cny_fields(
                payload,
                exchange_rate_converter=exchange_rate_converter,
            )
        )
        payload.update(
            {
                "dte": base.dte,
                "strike": base.strike,
                "open_interest": base.open_interest,
                "volume": base.volume,
                "covered_contracts_available": 1,
                "max_new_contracts": 1,
            }
        )
        return payload

    return (
        run_candidate_scan(
            config=CandidateScanConfig(
                mode="call",
                symbols=symbols,
                input_root=input_root,
                min_dte=0,
                max_dte=0,
                min_strike=None,
                max_strike=None,
                min_open_interest=None,
                min_volume=None,
                max_spread_ratio=None,
                min_annualized_net_return=None,
                min_net_income=0,
                required_data_frames=frames,
            ),
            deps=CandidateScanDependencies(compute_metrics_fn=compute, build_row_fn=build),
            calculation_decision_sink_fn=decisions.extend,
        ),
        decisions,
    )


def _decision_symbol(record: Mapping[str, Any]) -> str:
    decision = record.get("opening_decision")
    source = decision if isinstance(decision, Mapping) else record
    return str(source.get("symbol") or "").strip().upper()


def _exit_fee_fact(
    *,
    stock: Mapping[str, Any],
    candidate: Mapping[str, Any],
    shares: int,
    fee_context: Any,
) -> dict[str, Any]:
    custom = fee_context.get("stock_exit_fee_fact_fn") if isinstance(fee_context, Mapping) else None
    if callable(custom):
        return dict(custom(stock, candidate, shares))
    return assigned_stock_fee_fact(
        {
            "account": stock.get("account"),
            "broker": stock.get("broker"),
            "symbol": stock.get("symbol"),
            "currency": stock.get("currency"),
            "shares": shares,
            "price": candidate.get("strike"),
        },
        component="wheel_projected_stock_exit_fee",
        transaction_kind="sale",
    )


def run_wheel_call_scan(
    wheel_read_model: Mapping[str, Any],
    wheel_config: Mapping[str, Any],
    required_data_snapshot: FrozenRequiredDataBatch | Mapping[str, Any],
    coverage_fact: Mapping[str, Any],
    fee_context: Any,
    *,
    decision_time_ms: int,
) -> dict[str, Any]:
    """Build Wheel Call candidates from one already-frozen required-data batch."""

    account = str(wheel_read_model.get("account") or "").strip().lower()
    batches = [dict(row) for row in wheel_read_model.get("batches") or [] if isinstance(row, Mapping)]
    if not batches:
        return {"account": account, "scope_results": [], "raw_candidates": {}, "capacity_claims": []}
    enabled = bool(wheel_config.get("enabled_for_new_lifecycle"))
    stocks = _stock_rows(wheel_read_model)
    scan_batches = [
        row
        for row in batches
        if row.get("lifecycle_status") == "active"
        and row.get("integrity_status") == "trusted"
        and row.get("phase") == "ready"
        and enabled
    ]
    symbols = sorted({str(row.get("symbol") or "").strip().upper() for row in scan_batches})
    frames, unavailable_symbols = _frames_from_snapshot(required_data_snapshot, symbols)
    converter = fee_context.get("exchange_rate_converter") if isinstance(fee_context, Mapping) else fee_context
    universe, calculation_decisions = _candidate_universe(
        symbols=symbols,
        frames=frames,
        input_root=Path("."),
        exchange_rate_converter=converter,
        decision_time_ms=int(decision_time_ms),
    ) if symbols else (pd.DataFrame(), [])
    universe_by_symbol = {
        symbol: [dict(row) for row in universe.loc[universe["symbol"] == symbol].to_dict("records")]
        for symbol in symbols
    } if not universe.empty else {symbol: [] for symbol in symbols}
    decisions_by_symbol: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
    for decision in calculation_decisions:
        symbol = _decision_symbol(decision)
        if symbol in decisions_by_symbol:
            decisions_by_symbol[symbol].append(decision)
    common_candidates_by_symbol: dict[str, list[dict[str, Any]]] = {
        symbol: [] for symbol in symbols
    }
    for symbol, candidates in universe_by_symbol.items():
        for candidate in candidates:
            decision = evaluate_opening_candidate_policy(
                candidate,
                mode="call",
                min_dte=int(wheel_config["min_dte"]),
                max_dte=int(wheel_config["max_dte"]),
                min_annualized_return=float(wheel_config["min_annualized_net_premium_return"]),
                min_net_premium_cny=float(wheel_config["min_net_premium_cny"]),
                min_iv_rv_ratio=float(wheel_config["min_iv_rv_ratio"]),
                min_iv_minus_rv=float(wheel_config["min_iv_minus_rv"]),
                max_spread_ratio=float(wheel_config["max_spread_ratio"]),
                require_earnings_evidence=False,
                reject_known_earnings=False,
                apply_default_call_strike_cap=False,
            )
            decisions_by_symbol[symbol].append(decision)
            if decision["accepted"]:
                common_candidates_by_symbol[symbol].append(candidate)
    scopes: list[dict[str, Any]] = []
    raw_by_batch: dict[str, list[dict[str, Any]]] = {}
    claims: list[dict[str, Any]] = []
    for raw_batch in batches:
        stock_lot_id = str(raw_batch.get("stock_lot_id") or "")
        symbol = str(raw_batch.get("symbol") or "").strip().upper()
        base_scope = {
            "scope": "strategy",
            "account": account,
            "symbol": symbol,
            "stock_lot_id": stock_lot_id,
            "strategy_family": "wheel",
            "strategy_mode": "wheel",
            "candidate_owner": "wheel",
            "batch_generation_hash": raw_batch.get("batch_generation_hash"),
            "projection_hash": raw_batch.get("projection_hash"),
            "candidate_count": 0,
        }
        if raw_batch.get("lifecycle_status") != "active":
            continue
        if raw_batch.get("integrity_status") != "trusted":
            scopes.append({**base_scope, "status": "failed", "reason_code": "wheel_integrity_conflict"})
            continue
        if not enabled:
            scopes.append({**base_scope, "status": "not_applicable", "reason_code": "wheel_disabled"})
            continue
        if raw_batch.get("phase") != "ready":
            scopes.append({**base_scope, "status": "not_applicable", "reason_code": f"wheel_{raw_batch.get('phase') or 'not_ready'}"})
            continue
        if symbol in unavailable_symbols:
            scopes.append({**base_scope, "status": "unavailable", "reason_code": unavailable_symbols[symbol]})
            continue
        stock = stocks.get(stock_lot_id)
        if stock is None:
            scopes.append({**base_scope, "status": "unavailable", "reason_code": "assigned_stock_lot_unavailable"})
            continue
        batch = _batch_economics(raw_batch, stock)
        evaluated: list[dict[str, Any]] = []
        data_unavailable = False
        for candidate in common_candidates_by_symbol.get(symbol, []):
            multiplier = 0
            try:
                multiplier = int(float(candidate.get("multiplier") or 0))
                contracts = int(batch.get("shares_remaining") or 0) // multiplier
            except (TypeError, ValueError, ZeroDivisionError):
                contracts = 0
            grant_evaluations: dict[str, dict[str, Any]] = {}
            for grant in range(1, contracts + 1):
                fee = _exit_fee_fact(
                    stock=stock,
                    candidate=candidate,
                    shares=grant * max(multiplier, 0),
                    fee_context=fee_context,
                )
                grant_evaluations[str(grant)] = evaluate_wheel_call_candidate(
                    batch,
                    candidate,
                    wheel_config,
                    fee,
                    grant,
                )
            item = grant_evaluations.get(str(contracts), {})
            if item.get("wheel_candidate_status") == "data_unavailable":
                data_unavailable = True
                continue
            if not item.get("accepted"):
                continue
            item["stock_lot_id"] = stock_lot_id
            item["candidate_id"] = "wheel:" + canonical_sha256(
                {"stock_lot_id": stock_lot_id, "contract_symbol": item.get("contract_symbol")}
            )[:24]
            item["rank_key"] = build_wheel_call_rank_key(item)
            item["_grant_evaluations"] = grant_evaluations
            evaluated.append(item)
        evaluated.sort(key=lambda row: tuple((row.get("rank_key") or {}).get("sort_tuple") or ()))
        raw_by_batch[stock_lot_id] = evaluated
        evidence = evidence_summary_from_decisions(
            decisions=decisions_by_symbol.get(symbol, []),
            accepted_count=len(common_candidates_by_symbol.get(symbol, [])),
        )
        evidence_status, evidence_reason = project_evidence_scan_status(
            evidence=evidence,
            candidate_count=len(evaluated),
        )
        if evaluated:
            top = evaluated[0]
            claims.append(
                {
                    "claim_id": f"wheel:{stock_lot_id}",
                    "strategy_family": "wheel",
                    "account": account,
                    "symbol": symbol,
                    "stock_lot_id": stock_lot_id,
                    "candidate_id": top["candidate_id"],
                    "requested_contracts": int(top["contracts"]),
                    "requested_shares": int(top["candidate_covered_shares"]),
                    "multiplier": int(top["multiplier"]),
                    "assignment_at_ms": stock.get("assigned_at_ms"),
                }
            )
            scopes.append(
                {
                    **base_scope,
                    "status": "completed",
                    "reason_code": (
                        "partial_data"
                        if data_unavailable or evidence_reason == "partial_data"
                        else "candidates_found"
                    ),
                    "candidate_count": len(evaluated),
                }
            )
        else:
            scopes.append({
                **base_scope,
                "status": "unavailable" if data_unavailable else evidence_status,
                "reason_code": (
                    "wheel_candidate_data_unavailable"
                    if data_unavailable
                    else evidence_reason or "no_candidate"
                ),
            })
    return {
        "account": account,
        "scope_results": scopes,
        "raw_candidates": raw_by_batch,
        "capacity_claims": claims,
        "calculation_decisions": calculation_decisions,
    }


__all__ = ["run_wheel_call_scan"]
