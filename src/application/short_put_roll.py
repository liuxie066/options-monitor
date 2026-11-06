"""Account-scoped, on-demand short-put replacement cash estimates."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from domain.domain.engine.candidate_engine import (
    CandidateCalculationError, validate_opening_contract_evidence, validate_opening_quote_freshness,
    validate_option_contract_evidence,
)
from domain.domain.expiration_dates import expiration_market_date
from domain.domain.fee_calc import estimate_futu_option_buy_fee, estimate_futu_option_sell_fee
from domain.domain.option_position_identity import normalize_account, normalize_broker
from domain.domain.risk_capacity import evaluate_covered_call_stock_coverage
from domain.domain.symbol_identity import canonical_symbol, symbol_market
from domain.domain.trade_contract_identity import contract_key, contract_share_quantity, require_option_multiplier
from src.application.account_config import build_account_runtime_config, resolve_account_broker, resolve_configured_accounts
from src.application.agent_tool_contracts import AgentToolError
from src.application.market_data_provider import resolve_market_data_provider
from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.opend_fetch_config import opend_fetch_kwargs
from src.application.opend_symbol_fetching import FetchSymbolRequest
from src.application.opend_utils import normalize_underlier
from src.application.position_advice_inputs import read_advice_holdings as _read_holdings
from src.application.position_advice_evidence import current_position_advice_evidence
from src.application.opening_quote_evidence import OPENING_QUOTE_MAX_AGE_SECONDS
from src.application.position_advice_inputs import select_ledger_positions
from src.application.portfolio_context_service import load_account_portfolio_context, portfolio_position_snapshot_errors
from src.application.futu_portfolio_context import fetch_futu_portfolio_context
from src.application.position_advice_plugin import (
    PositionAdviceUnavailableError, position_advice_plugin_status, require_position_advice_plugin,
)
from src.application.quality.gate import assert_quality_allows
from src.application.read_execution import RunStopped, check_run
from src.application.runtime_config_freshness import infer_runtime_config_market
from src.application.runtime_paths import runtime_root_scope
from src.infrastructure.io_utils import atomic_write_json


SHORT_PUT_ROLL_INPUT_SCHEMA: dict[str, Any] = {
    "config_key": {"type": "string", "enum": ["us", "hk"]},
    "config_path": {"type": "string"},
    "account": {"type": "string", "required": True, "minLength": 1, "maxLength": 64},
    "lot_id": {"type": "string", "required": True, "minLength": 1, "maxLength": 256},
    "max_new_expiration": {"type": "string", "required": True, "minLength": 10, "maxLength": 10},
    "min_new_strike": {"type": "number", "exclusiveMinimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
}
COVERED_CALL_ROLL_INPUT_SCHEMA: dict[str, Any] = {
    key: dict(value) for key, value in SHORT_PUT_ROLL_INPUT_SCHEMA.items() if key != "min_new_strike"
}
COVERED_CALL_ROLL_INPUT_SCHEMA["max_new_strike"] = {"type": "number", "exclusiveMinimum": 0}
_CC_INTERPRETATION = (
    "两笔单腿按旧 ask 买回、新 bid 卖出的扣费现金估算；不保证组合成交，也不保证消除提前指派。"
    "仅列出两腿扣费后净收入严格大于零的上移方案，按净现金年化收益率排序；条件差额假设两边最终均按各自行权价交股，"
    "不是已实现利润或预期收益。新旧资金分母均为当前股价×覆盖股数。"
    "交股是 wheel 卖出持股的第二步；接受旧仓交股仍是有效选择。"
)
_INTERPRETATION = (
    "两笔单腿按旧 ask 买回、新 bid 卖出的扣费现金估算；不保证张数足额成交或组合成交。"
    "净增现金年化以新 strike 名义本金和今天至新到期日计算；"
    "剩余最高年化是到期归零情景的上限，不是预期收益。净收现金不等于整个交易链已盈利。"
)


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def _normalize_request(payload: Mapping[str, Any], *, covered_call: bool = False) -> dict[str, Any]:
    from src.application.tool_input_schema import build_tool_input_json_schema, validate_tool_input_payload

    validate_tool_input_payload(tool_name="covered_call_roll_candidates" if covered_call else "short_put_roll_candidates",
        payload=dict(payload), schema=build_tool_input_json_schema(
            COVERED_CALL_ROLL_INPUT_SCHEMA if covered_call else SHORT_PUT_ROLL_INPUT_SCHEMA), enforce_required=True)
    request = dict(payload)
    request["account"] = normalize_account(request["account"])
    request["lot_id"] = request["lot_id"].strip()
    if not request["account"] or not request["lot_id"]:
        raise ValueError("account and lot_id must not be blank")
    expiry = date.fromisoformat(request["max_new_expiration"])
    if expiry.isoformat() != request["max_new_expiration"]:
        raise ValueError("max_new_expiration must use YYYY-MM-DD")
    strike_bound = "max_new_strike" if covered_call else "min_new_strike"
    if strike_bound in request:
        request[strike_bound] = _positive(request[strike_bound], strike_bound)
    limit = request.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer from 1 to 50")
    request["limit"] = limit
    return request


def _identity(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return contract_key(row.get("symbol"), row.get("option_type"),
        row.get("expiration_ymd") or row.get("expiration"), row.get("strike"))


def _records(value: Any) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict"):
        return value.to_dict("records")
    return [dict(row) for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _current_requirement(*, provider: Any, gateway: Any, symbol: str, market: str,
                         runtime_root: Path, limits: dict[str, Any], clock: Callable[[], datetime],
                         deadline: float, cancelled: Callable[[], bool]) -> dict[str, Any]:
    """Collect shared state and rights after the latest option snapshot."""
    requirement: dict[str, Any] = {}
    code = normalize_underlier(symbol).code

    def limited(call: Callable[[], Any]) -> Any:
        return rate_limited_opend_call(base_dir=runtime_root, endpoint="market_snapshot",
            max_wait_sec=float(limits.get("snapshot_max_wait_sec", 30)),
            window_sec=float(limits.get("snapshot_window_sec", 30)),
            max_calls=int(limits.get("snapshot_max_calls", 60)), call=call,
            deadline=deadline, cancelled=cancelled)

    try:
        rows = _records(limited(lambda: provider.get_market_state(gateway, [code])))
        matching = [row for row in rows if row.get("code") == code]
        if len(matching) == 1:
            requirement["market_state_after_snapshot"] = str(matching[0].get("market_state") or "").upper()
        requirement["market_state_received_at_utc"] = clock().isoformat()
    except Exception:
        check_run(deadline, cancelled)
    try:
        rights = limited(lambda: provider.get_quote_rights(gateway, market=market))
        if isinstance(rights, Mapping):
            requirement.update(quote_rights_status=rights.get("status"), quote_rights_market=rights.get("market"),
                quote_rights_underlier=rights.get("underlier_right"), quote_rights_option=rights.get("option_right"))
        requirement["quote_rights_received_at_utc"] = clock().isoformat()
    except Exception:
        check_run(deadline, cancelled)
    return requirement


def _complete_enumeration(bundle: Mapping[str, Any], start: date, end: date) -> bool:
    meta = bundle.get("meta", {})
    available, statuses = meta.get("available_expirations"), meta.get("expiration_statuses")
    if not (meta.get("expiration_enumeration_complete") is True and meta.get("query_scope_validated") is True
            and isinstance(available, list) and isinstance(statuses, dict)):
        return False
    try:
        expiries = [value for value in available if start <= date.fromisoformat(value) <= end]
    except (ValueError, TypeError):
        return False
    return all(statuses.get(value) in {"cache", "fetched", "empty"} for value in expiries)


def _evaluate_bundle(*, result: dict[str, Any], position: dict[str, Any], bundle: Mapping[str, Any],
                     requirement: dict[str, Any], spot: float, market: str, now: datetime,
                     request: dict[str, Any], policy: Any,
                     enumeration_complete: bool | None = None,
                     required_data_coverage: Mapping[str, Any] | None = None) -> dict[str, Any]:
    partial_required_data = bool(required_data_coverage and required_data_coverage.get("status") == "partial")
    covered_call = position["option_type"] == "call"
    option_type = position["option_type"]
    old_key = _identity(position)
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    invalid_rows = 0
    for raw in bundle.get("rows", []):
        if not isinstance(raw, dict):
            invalid_rows += 1
            continue
        try:
            key = _identity(raw)
        except (ValueError, TypeError):
            invalid_rows += 1
            result["reason_codes"].append("contract_identity_invalid")
            continue
        grouped.setdefault(key, []).append(raw)
    old_rows = grouped.pop(old_key, [])
    if not old_rows or any(row != old_rows[0] for row in old_rows[1:]):
        result["reason_codes"].append("old_quote_missing_or_conflicting")
        return result
    old = old_rows[0]
    _, old_flags = current_position_advice_evidence(position=position, quote=old, requirement=requirement,
        market=market, now_utc=now)
    try:
        validate_option_contract_evidence(old, mode=option_type, quote_side="buy", now_utc=now)
        if old.get("currency") != position["currency"] or require_option_multiplier(old.get("multiplier")) != position["multiplier"]:
            old_flags.append("old_contract_identity_mismatch")
    except (CandidateCalculationError, ValueError, TypeError):
        old_flags.append("old_contract_evidence_unavailable")
    if old_flags:
        result["reason_codes"].extend(old_flags)
        return result
    qty = position["contracts_open"]
    old_fee = estimate_futu_option_buy_fee(position["currency"], old["ask"], contracts=qty, multiplier=position["multiplier"])
    old_expiry, max_expiry = date.fromisoformat(position["expiration"]), date.fromisoformat(request["max_new_expiration"])
    today = expiration_market_date(now, market)
    old_dte = (old_expiry - today).days
    complete = (_complete_enumeration(bundle, old_expiry, max_expiry)
                if enumeration_complete is None else enumeration_complete)
    complete = complete and not partial_required_data
    meta = bundle.get("meta", {})
    result["scope"]["enumerated_expirations"] = meta.get("requested_expirations", bundle.get("expirations", []))
    rejects: Counter[str] = Counter()
    missing: Counter[str] = Counter({"contract_identity_invalid": invalid_rows}) if invalid_rows else Counter()
    matched: list[dict[str, Any]] = []
    discovered = invalid_rows
    for key, duplicates in grouped.items():
        row = duplicates[0]
        try:
            expiry = date.fromisoformat(str(row.get("expiration")))
            strike = _positive(row.get("strike"), "strike")
        except (TypeError, ValueError):
            discovered += 1
            missing["candidate_identity_invalid"] += 1
            continue
        if covered_call:
            in_scope = (old_expiry <= expiry <= max_expiry and row.get("option_type") == "call"
                and strike > position["strike"]
                and (request.get("max_new_strike") is None or strike <= request["max_new_strike"]))
        else:
            in_scope = (old_expiry <= expiry <= max_expiry and row.get("option_type") == "put"
                and strike < position["strike"] and strike < spot
                and (request.get("min_new_strike") is None or strike >= request["min_new_strike"]))
        if not in_scope:
            continue
        discovered += 1
        if any(other != row for other in duplicates[1:]):
            missing["contract_identity_conflict"] += 1
            continue
        _, flags = current_position_advice_evidence(position=position, quote=row, requirement=requirement,
            market=market, now_utc=now)
        # A current, explicitly ineligible new leg is a known exclusion. Any
        # other currentness gap must remain unassessed, including on that leg.
        known_ineligible = row.get("opening_contract_status") == "ineligible"
        if flags and not (known_ineligible and flags == ["option_security_not_tradable"]):
            missing[flags[0]] += 1
            continue
        try:
            validate_opening_quote_freshness(row, now_utc=now)
            validate_opening_contract_evidence(row, mode=option_type, now_utc=now)
            new_fee = estimate_futu_option_sell_fee(position["currency"], row["bid"], contracts=qty, multiplier=position["multiplier"])
            candidate = {**row, "contracts": qty}
            economics = dict(old_quote=old, spot=spot, old_dte=old_dte, new_dte=(expiry - today).days,
                old_buy_fee=old_fee.amount, new_sell_fee=new_fee.amount)
            evaluate = policy.evaluate_covered_call_roll if covered_call else policy.evaluate_short_put_roll
            if covered_call:
                economics["covered_shares"] = contract_share_quantity(qty, position["multiplier"])
            decision = evaluate(position, candidate, **economics)
            per_contract_decision = decision
            per_contract_old_fee, per_contract_new_fee = old_fee, new_fee
            if qty != 1 and decision.get("metrics"):
                # Minimum commissions and per-order fees do not scale with lot size.
                per_contract_old_fee = estimate_futu_option_buy_fee(
                    position["currency"], old["ask"], contracts=1, multiplier=position["multiplier"])
                per_contract_new_fee = estimate_futu_option_sell_fee(
                    position["currency"], row["bid"], contracts=1, multiplier=position["multiplier"])
                per_contract_economics = {**economics, "old_buy_fee": per_contract_old_fee.amount,
                    "new_sell_fee": per_contract_new_fee.amount}
                if covered_call:
                    per_contract_economics["covered_shares"] = position["multiplier"]
                per_contract_decision = evaluate({**position, "contracts_open": 1}, {**candidate, "contracts": 1},
                    **per_contract_economics)
        except CandidateCalculationError as exc:
            if exc.reason in {"contract_ineligible", "option_non_standard", "option_type_mismatch"}:
                rejects[exc.reason] += 1
            else:
                missing[exc.reason] += 1
            continue
        except (ValueError, TypeError):
            missing["candidate_economics_unavailable"] += 1
            continue
        eligible = decision.get("recommendation") in {"credit_roll"} if covered_call else decision["accepted"]
        if not eligible:
            rejects.update(decision["reason_codes"])
            continue
        metrics = decision["metrics"]
        per_contract = {key: per_contract_decision["metrics"][key] for key in (
            "net_credit", "incremental_period_return", "incremental_annualized_return")}
        per_contract.update(old_buy_fee=per_contract_old_fee.to_dict(), new_sell_fee=per_contract_new_fee.to_dict())
        conclusion = {key: decision[key] for key in ("recommendation", "cash_flow_type")} if covered_call else {}
        matched.append({"contract": row["contract_symbol"], "expiration": row["expiration"], "strike": strike,
            **metrics, **conclusion, "per_contract": per_contract,
            "old_buy_fee": old_fee.to_dict(), "new_sell_fee": new_fee.to_dict(),
            "evidence": {"old_ask": old["ask"], "new_bid": row["bid"], "new_ask": row["ask"],
                "old_snapshot_received_at_utc": old.get("snapshot_received_at_utc"),
                "old_spot_observed_at_utc": old.get("spot_observed_at_utc"),
                "snapshot_received_at_utc": row.get("snapshot_received_at_utc"),
                "spot_observed_at_utc": row.get("spot_observed_at_utc"),
                "delta": row.get("delta") if not row.get("delta_reason_code") else None,
                "delta_reason_code": row.get("delta_reason_code"), "iv": row.get("implied_volatility"),
                "term_matched_rv": row.get("term_matched_rv"), **requirement}})
        if partial_required_data:
            from src.application.required_data_coverage import required_data_candidate_coverage

            matched[-1]["evidence"].update(required_data_candidate_coverage(row, required_data_coverage))
    evaluated = discovered - sum(missing.values())
    coverage_status = "complete" if complete and not missing else "partial" if evaluated or partial_required_data else "unavailable"
    ranked = policy.rank_covered_call_roll_candidates(matched) if covered_call else policy.rank_short_put_roll_candidates(matched)
    result.update(status="success" if coverage_status == "complete" else "partial",
        outcome="found" if matched else "none" if coverage_status == "complete" else "unable",
        coverage={"status": coverage_status, "enumeration_complete": complete, "discovered": discovered,
            "evaluated": evaluated, "matched": len(matched), "excluded": evaluated - len(matched),
            "unassessed": sum(missing.values()) if complete else None},
        candidates=ranked[:request["limit"]], displayed=min(len(ranked), request["limit"]),
        omitted=max(0, len(ranked) - request["limit"]),
        exclusion_summary=[{"code": key, "count": value} for key, value in rejects.items()])
    if required_data_coverage is not None:
        result["coverage"]["required_data_coverage"] = dict(required_data_coverage)
    if partial_required_data:
        result["coverage"].update(ranking_complete=False, ranking_scope="observed_contracts",
            partial_candidate_policy="retain_and_mark")
    result["position"].update(old_close_cost=old["ask"] * contract_share_quantity(qty, position["multiplier"]) + old_fee.amount,
        old_buy_fee=old_fee.to_dict(), old_dte=old_dte)
    result["position"]["old_remaining_max_annualized_return"] = policy.remaining_max_annualized_return(
        remaining_cash=result["position"]["old_close_cost"],
        capital_basis=(spot if covered_call else position["strike"]) * contract_share_quantity(qty, position["multiplier"]), dte=old_dte)
    result["reason_codes"].extend(missing)
    if not complete:
        result["reason_codes"].append("enumeration_incomplete")
    return result


def _roll_result(*, account: str, request: Mapping[str, Any], now: datetime,
                 covered_call: bool) -> dict[str, Any]:
    capability = "covered_call_roll" if covered_call else "short_put_roll"
    result: dict[str, Any] = {
        "schema_version": capability + ".output.v1", "status": "unavailable", "outcome": "unable",
        "account": account, "position": None, "evaluated_at_utc": now.isoformat(), "coverage": None,
        "candidates": [], "displayed": 0, "omitted": 0, "reason_codes": [],
        "pricing_basis": "leg_cashflow_estimate", "interpretation": _INTERPRETATION,
        "scope": {"max_new_expiration": request["max_new_expiration"],
            "min_new_strike": request.get("min_new_strike"), "old_otm_max_pct": 0.05,
            "new_put_must_be_otm": True},
    }
    if covered_call:
        result.update(interpretation=_CC_INTERPRETATION,
            scope={"max_new_expiration": request["max_new_expiration"],
                "max_new_strike": request.get("max_new_strike"), "old_otm_max_pct": 0.05,
                "new_call_must_be_otm": False, "new_expiration_must_be_later": False},
            capital_basis_kind="covered_stock_market_value", assignment_semantics="wheel_stock_sale")
    return result


def evaluate_frozen_position_roll(
    *, position: Mapping[str, Any], positions_context: Mapping[str, Any],
    portfolio_context: Mapping[str, Any] | None, requirement: Mapping[str, Any],
    quotes: list[dict[str, Any]], provenance: Mapping[str, Any],
    config: dict[str, Any], now: datetime, unavailable_reason: str | None = None,
) -> dict[str, Any]:
    """Evaluate a Tick lot using only the already validated same-run inputs.

    Completeness comes from the sealed fetch contract and its full discovery;
    the presence of a few replacement quotes alone cannot prove a zero result.
    """
    covered_call = position.get("option_type") == "call"
    account = normalize_account(position.get("account"))
    scope = requirement.get("roll_scope") or {}
    request = {"max_new_expiration": scope.get("max_new_expiration"), "limit": 50}
    result = _roll_result(account=account, request=request, now=now, covered_call=covered_call)
    result["source"] = {key: provenance.get(key) for key in (
        "manifest_sha256", "snapshot_id", "source_observed_at", "expires_at", "expected_fetch_contract_sha256",
    )}
    result["source"]["requirement_id"] = requirement.get("requirement_id")
    result["position"] = dict(position)
    required_data_coverage = provenance.get("required_data_coverage")
    if not isinstance(required_data_coverage, Mapping):
        required_data_coverage = None
    partial_required_data = bool(required_data_coverage and required_data_coverage.get("status") == "partial")
    if required_data_coverage is not None:
        result["source"]["required_data_coverage"] = dict(required_data_coverage)
    plugin_status = position_advice_plugin_status(config)
    if plugin_status != "available":
        result.update(status=plugin_status, reason_codes=["position_advice_" + plugin_status])
        return result
    if unavailable_reason or not scope:
        result["reason_codes"] = [unavailable_reason or "roll_scope_not_planned"]
        return result
    try:
        policy = require_position_advice_plugin()
        position = dict(position)
        broker = normalize_broker(position.get("broker"))
        if broker != "富途":
            result["reason_codes"] = ["unsupported_broker_fee_model"]
            return result
        market = symbol_market(position.get("symbol"))
        if market not in {"US", "HK"} or position.get("side") != "short":
            raise ValueError("unsupported roll position")
        position["strike"] = _positive(position.get("strike"), "strike")
        position["multiplier"] = require_option_multiplier(position.get("multiplier"))
        shares = contract_share_quantity(position.get("contracts_open"), position["multiplier"])
        position["contracts_open"] = shares // position["multiplier"]
        position["expiration"] = date.fromisoformat(str(position.get("expiration_ymd") or position.get("expiration"))).isoformat()
        old_expiry = date.fromisoformat(position["expiration"])
        max_expiry = date.fromisoformat(request["max_new_expiration"])
        today = expiration_market_date(now, market)
        result["position"] = dict(position)
        if old_expiry <= today or max_expiry < old_expiry:
            result.update(status="success", outcome="not_applicable", reason_codes=["roll_expiration_window_empty"])
            return result
        plan = (provenance.get("expected_fetch_contract") or {}).get("fetch_plan") or {}
        spot = _positive(plan.get("spot_reference"), "spot")
        in_scope = policy.old_covered_call_in_roll_scope if covered_call else policy.old_position_in_roll_scope
        if not in_scope(strike=position["strike"], spot=spot):
            result.update(status="success", outcome="not_applicable", reason_codes=["old_position_outside_roll_scope"])
            return result
        if covered_call:
            errors = portfolio_position_snapshot_errors(
                portfolio_context or {}, account=account, asset_type="stock", now_utc=now, market=market,
            )
            locked_reason = (positions_context.get("locked_shares_unavailable_by_symbol") or {}).get(position["symbol"])
            if positions_context.get("context_status") != "available" or locked_reason:
                errors.append(str(locked_reason or "option_positions_unavailable"))
            if errors:
                result["stock_coverage"] = {"status": "unavailable", "reason_codes": sorted(set(errors))}
                result["reason_codes"] = sorted(set(errors))
                return result
            coverage = evaluate_covered_call_stock_coverage(
                positions=positions_context.get("assignment_positions") or positions_context.get("open_positions_min") or [],
                snapshot=portfolio_context["position_snapshot_input"], account=account, broker=broker,
                symbol=position["symbol"], currency=position["currency"],
            )
            result["stock_coverage"] = coverage
            if coverage["status"] != "covered":
                result["reason_codes"] = coverage["reason_codes"]
                if coverage["status"] == "insufficient":
                    result.update(status="success", outcome="not_applicable")
                return result
        complete, expirations = _frozen_roll_scope_coverage(
            plan=plan, requirement=requirement, position=position,
        )
        if not complete and not partial_required_data:
            result["reason_codes"] = ["roll_scope_not_covered"]
            return result
        result = _evaluate_bundle(
            result=result, position=position, bundle={"rows": quotes, "expirations": expirations},
            requirement=dict(requirement), spot=spot, market=market, now=now,
            request=request, policy=policy, enumeration_complete=complete,
            required_data_coverage=required_data_coverage,
        )
        if result["candidates"]:
            expiry_times = [datetime.fromisoformat(str(provenance["expires_at"]).replace("Z", "+00:00"))]
            for candidate in result["candidates"]:
                evidence = candidate["evidence"]
                for field in ("old_snapshot_received_at_utc", "old_spot_observed_at_utc", "snapshot_received_at_utc", "spot_observed_at_utc"):
                    observed = datetime.fromisoformat(str(evidence[field]).replace("Z", "+00:00"))
                    expiry_times.append(observed + timedelta(seconds=OPENING_QUOTE_MAX_AGE_SECONDS))
            if covered_call:
                observed = datetime.fromisoformat(str(portfolio_context["position_snapshot_input"]["observed_at_utc"]).replace("Z", "+00:00"))
                expiry_times.append(observed + timedelta(seconds=300))
            result["actionable_until_utc"] = min(expiry_times).isoformat()
        return result
    except PositionAdviceUnavailableError:
        result.update(status="unavailable", outcome="unable", candidates=[], displayed=0,
            omitted=0, coverage=None, reason_codes=["position_advice_unavailable"])
    except (KeyError, TypeError, ValueError, AttributeError):
        result.update(status="unavailable", outcome="unable", candidates=[], displayed=0,
            omitted=0, coverage=None, reason_codes=["roll_evidence_unavailable"])
    return result


def _frozen_roll_scope_coverage(*, plan: Mapping[str, Any], requirement: Mapping[str, Any],
                                position: Mapping[str, Any]) -> tuple[bool, list[str]]:
    discovery = plan.get("expiration_discovery") or {}
    if (plan.get("expiration_discovery_complete") is not True
            or discovery.get("outcome") not in {"success_rows", "success_empty"}):
        return False, []
    scope = requirement["roll_scope"]
    covered_call = position["option_type"] == "call"
    old_expiry, max_expiry = position["expiration"], scope["max_new_expiration"]
    expirations = [expiry for expiry in discovery.get("expirations", [])
        if expiry >= old_expiry and expiry <= max_expiry]
    for expiry in expirations:
        if not any(
            side.get("option_type") == position["option_type"]
            and "position_advice.roll_scope" in side.get("source_fields", [])
            and expiry in side.get("explicit_expirations", [])
            and (side["strike_window"].get("max_strike") is None if covered_call
                 else side["strike_window"].get("min_strike") is None)
            and (side["strike_window"].get("min_strike") is None
                 or side["strike_window"]["min_strike"] <= position["strike"])
            and (side["strike_window"].get("max_strike") is None
                 or side["strike_window"]["max_strike"] >= position["strike"])
            for side in plan.get("side_plans", [])
        ):
            return False, expirations
    return True, expirations


def _covered_stock_evidence(*, records: list[dict[str, Any]], position: dict[str, Any],
                            config: dict[str, Any], account: str, broker: str, market: str,
                            runtime_root: Path, clock: Callable[[], datetime]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    from src.application.ledger.api import build_position_risk_context

    context = build_position_risk_context(records, broker=broker, account=account, observed_at=clock())
    unavailable = context.get("locked_shares_unavailable_by_symbol") or {}
    if context.get("context_status") != "available" or position["symbol"] in unavailable:
        return {"status": "unavailable", "reason_codes": [
            unavailable.get(position["symbol"]) or "canonical_call_obligations_unavailable"]}, None
    try:
        portfolio = load_account_portfolio_context(runtime_config=config, account=account,
            market=broker, portfolio_source="futu", write_cache=False,
            state_dir=runtime_root / "output_accounts" / account / "state", log=lambda _: None,
            fetch_futu_portfolio_context_fn=fetch_futu_portfolio_context,
            exchange_rate_observation={}, required_position_asset_types=("stock",))
    except (OSError, RuntimeError, ValueError) as exc:
        return {"status": "unavailable", "reason_codes": ["stock_coverage_observation_unavailable"],
            "error_type": type(exc).__name__}, None
    errors = portfolio_position_snapshot_errors(portfolio, account=account, market=market, now_utc=clock())
    if errors:
        return {"status": "unavailable", "reason_codes": errors}, portfolio
    snapshot = portfolio["position_snapshot_input"]
    coverage = evaluate_covered_call_stock_coverage(positions=context["open_positions_min"],
        snapshot=snapshot, account=account, broker=broker, symbol=position["symbol"], currency=position["currency"])
    coverage["observed_at_utc"] = snapshot.get("observed_at_utc")
    return coverage, portfolio


def short_put_roll_candidates(payload: Mapping[str, Any], *, config: dict[str, Any], config_path: Path,
                             base_dir: Path, runtime_root: Path, now_utc: datetime | None = None,
                             deadline: float | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Estimate one put lot's replacement choices without orders or ledger writes."""
    return _roll_candidates(payload, config=config, config_path=config_path, base_dir=base_dir,
        runtime_root=runtime_root, now_utc=now_utc, deadline=deadline, cancelled=cancelled)


def covered_call_roll_candidates(payload: Mapping[str, Any], *, config: dict[str, Any], config_path: Path,
                                base_dir: Path, runtime_root: Path, now_utc: datetime | None = None,
                                deadline: float | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Estimate covered calls with higher strikes, no earlier expiry and no net debit."""
    return _roll_candidates(payload, config=config, config_path=config_path, base_dir=base_dir,
        runtime_root=runtime_root, now_utc=now_utc, deadline=deadline, cancelled=cancelled, covered_call=True)


def _roll_candidates(payload: Mapping[str, Any], *, config: dict[str, Any], config_path: Path,
                     base_dir: Path, runtime_root: Path, now_utc: datetime | None = None,
                     deadline: float | None = None, cancelled: Callable[[], bool] | None = None,
                     covered_call: bool = False) -> dict[str, Any]:
    request = _normalize_request(payload, covered_call=covered_call)
    option_type = "call" if covered_call else "put"
    capability = "covered_call_roll" if covered_call else "short_put_roll"
    if now_utc is not None and now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    clock = (lambda: now_utc.astimezone(timezone.utc)) if now_utc is not None else (lambda: datetime.now(timezone.utc))
    started = clock()
    stop = deadline if deadline is not None else time.monotonic() + 90
    is_cancelled = cancelled or (lambda: False)
    account = request["account"]
    resolve_configured_accounts(config, [account])
    cfg = build_account_runtime_config(base_cfg=config, cfg_path=config_path, account=account, markets_to_run=[])
    result = _roll_result(account=account, request=request, now=started, covered_call=covered_call)
    plugin_status = position_advice_plugin_status(cfg)
    if plugin_status != "available":
        result.update(status=plugin_status, reason_codes=["position_advice_" + plugin_status])
        return result
    try:
        policy = require_position_advice_plugin()
    except PositionAdviceUnavailableError:
        result["reason_codes"] = ["position_advice_unavailable"]
        return result
    policy_names = ("evaluate_covered_call_roll", "rank_covered_call_roll_candidates", "old_covered_call_in_roll_scope") if covered_call else (
        "evaluate_short_put_roll", "rank_short_put_roll_candidates", "old_position_in_roll_scope")
    if not all(callable(getattr(policy, name, None)) for name in (*policy_names, "remaining_max_annualized_return")):
        result["reason_codes"] = ["position_advice_roll_unavailable"]
        return result
    broker = resolve_account_broker(cfg, account=account)
    if normalize_broker(broker) != "富途":
        result["reason_codes"] = ["unsupported_broker_fee_model"]
        return result
    try:
        check_run(stop, is_cancelled)
    except RunStopped as exc:
        result["reason_codes"] = [exc.code.lower()]
        return result
    records = _read_holdings(config=cfg, config_path=config_path, base_dir=base_dir, runtime_root=runtime_root, account=account)
    positions, _ = select_ledger_positions({"lot_ids": [request["lot_id"]]}, records=records,
        account=account, broker=broker, now_utc=started)
    if len(positions) != 1 or positions[0].get("option_type") != option_type:
        raise ValueError(f"select exactly one open short {option_type} lot")
    position = dict(positions[0])
    position["symbol"] = canonical_symbol(position["symbol"])
    position["expiration"] = position.get("expiration_ymd") or position.get("expiration")
    market = symbol_market(position["symbol"])
    config_market = infer_runtime_config_market(config=cfg, config_key=request.get("config_key"), config_path=config_path)
    if market not in {"US", "HK"} or config_market != market.lower():
        raise AgentToolError(code="PERMISSION_DENIED", message="lot market is outside the selected config scope")
    position["strike"] = _positive(position.get("strike"), "old strike")
    position["multiplier"] = require_option_multiplier(position.get("multiplier"))
    qty = position.get("contracts_open")
    if isinstance(qty, bool) or contract_share_quantity(qty, position["multiplier"]) <= 0:
        raise ValueError("contracts_open must be a positive integer")
    position["contracts_open"] = int(qty)
    result["position"] = {key: position.get(key) for key in (
        "symbol", "expiration", "strike", "contracts_open", "multiplier", "currency", "broker", "lot_id")}
    expiry, max_expiry = date.fromisoformat(position["expiration"]), date.fromisoformat(request["max_new_expiration"])
    today = expiration_market_date(started, market)
    if max_expiry < expiry:
        raise ValueError("max_new_expiration must not precede the old expiration")
    if expiry <= today:
        result["reason_codes"] = ["old_position_not_active"]
        return result
    if covered_call:
        if request.get("max_new_strike") is not None and request["max_new_strike"] <= position["strike"]:
            raise ValueError("max_new_strike must be above the old strike")
    elif request.get("min_new_strike", 0) >= position["strike"]:
        raise ValueError("min_new_strike must be below the old strike")
    with runtime_root_scope(runtime_root):
        assert_quality_allows("close_advice", account=account, market=market.lower())
    if covered_call:
        coverage, stock_portfolio = _covered_stock_evidence(records=records, position=position, config=cfg, account=account,
            broker=broker, market=market, runtime_root=runtime_root, clock=clock)
        result["stock_coverage"] = coverage
        if coverage["status"] != "covered":
            result["reason_codes"] = coverage["reason_codes"]
            if coverage["status"] == "insufficient":
                result.update(status="success", outcome="not_applicable")
            return result
        try:
            check_run(stop, is_cancelled)
        except RunStopped as exc:
            result["reason_codes"] = [exc.code.lower()]
            return result
    gateway = None
    try:
        provider = resolve_market_data_provider(cfg)
        route = provider.resolve_quote_route(cfg, market=market, symbol=position["symbol"])
        if not route.ok or not route.host or not route.port:
            result["reason_codes"] = ["quote_route_unavailable"]
            return result
        limits = opend_fetch_kwargs(cfg)
        observation = provider.get_underlier_observation(position["symbol"], host=route.host, port=route.port,
            base_dir=runtime_root, deadline=stop, cancelled=is_cancelled,
            **{key: value for key, value in limits.items() if key.startswith("snapshot_")})
        check_run(stop, is_cancelled)
        if observation.status != "ready":
            result["reason_codes"] = [observation.reason_code or "underlier_observation_unavailable"]
            return result
        spot = _positive(observation.last_price, "spot")
        result["position"].update(spot=spot,
            old_otm_pct=((position["strike"] - spot) if covered_call else (spot - position["strike"])) / spot)
        scope_policy = policy.old_covered_call_in_roll_scope if covered_call else policy.old_position_in_roll_scope
        if not scope_policy(strike=position["strike"], spot=spot):
            result.update(status="success", outcome="not_applicable", reason_codes=["old_position_outside_roll_scope"])
            return result
        if not covered_call and request.get("min_new_strike", 0) >= spot:
            raise ValueError("min_new_strike must be below the current spot for an OTM replacement")
        check_run(stop, is_cancelled)
        gateway = provider.open_quote_gateway(host=route.host, port=route.port,
            is_option_chain_cache_enabled=True, deadline=stop, cancelled=is_cancelled)
        old_dte = (expiry - today).days
        replacement_filter = {"option_type": option_type,
            "min_strike": position["strike"] if covered_call else request.get("min_new_strike"),
            "max_strike": request.get("max_new_strike") if covered_call else min(position["strike"], spot),
            "min_dte": old_dte, "max_dte": (max_expiry - today).days}
        fetch_request = FetchSymbolRequest(symbol=position["symbol"], host=route.host, port=route.port,
            market_data_provider=provider.name, base_dir=runtime_root, gateway=gateway, option_types=option_type,
            underlier_observation=observation.to_dict(), fetch_spot_if_missing=False, trading_date=today.isoformat(),
            min_dte=old_dte, max_dte=(max_expiry - today).days, include_realized_volatility=False,
            deadline=stop, cancelled=is_cancelled, chain_cache=True, freshness_policy="force_refresh", **limits,
            query_filters=[{"option_type": option_type, "min_strike": position["strike"], "max_strike": position["strike"],
                "min_dte": old_dte, "max_dte": old_dte}, replacement_filter])
        bundle = provider.fetch_symbol_request(fetch_request)
        check_run(stop, is_cancelled)
        requirement = _current_requirement(provider=provider, gateway=gateway, symbol=position["symbol"], market=market,
            runtime_root=runtime_root, limits=limits, clock=clock, deadline=stop, cancelled=is_cancelled)
        evaluated_at = clock()
        result["evaluated_at_utc"] = evaluated_at.isoformat()
        if covered_call:
            stock_errors = portfolio_position_snapshot_errors(stock_portfolio, account=account, market=market, now_utc=evaluated_at)
            if stock_errors:
                result["stock_coverage"].update(status="unavailable", reason_codes=stock_errors)
                result["reason_codes"] = stock_errors
                return result
        if expiration_market_date(evaluated_at, market) != today:
            result["reason_codes"] = ["market_date_changed"]
            return result
        result = _evaluate_bundle(result=result, position=position, bundle=bundle, requirement=requirement,
            spot=spot, market=market, now=evaluated_at, request=request, policy=policy)
        check_run(stop, is_cancelled)
        result["reason_codes"] = list(dict.fromkeys(result["reason_codes"]))
        result["source"] = {"ledger": "canonical_position_lot", "lot_id": request["lot_id"], "market_data_provider": provider.name,
            "spot_observed_at_utc": observation.observed_at_utc, "effective_max_new_expiration": max_expiry.isoformat(),
            ("max_new_strike" if covered_call else "min_new_strike"):
                request.get("max_new_strike" if covered_call else "min_new_strike")}
    except RunStopped as exc:
        result.update(status="unavailable", outcome="unable", candidates=[], displayed=0, omitted=0,
            coverage=None, reason_codes=[exc.code.lower()])
        return result
    except (TimeoutError, OSError, RuntimeError) as exc:
        reason = "quote_dependency_unavailable"
        if is_cancelled():
            reason = "cancelled"
        elif time.monotonic() >= stop:
            reason = "budget_exhausted"
        elif isinstance(exc, TimeoutError):
            reason = "quote_request_timeout"
        result.update(status="unavailable", outcome="unable", candidates=[], displayed=0, omitted=0,
            coverage=None, reason_codes=[reason])
        return result
    finally:
        if gateway is not None:
            try:
                gateway.close()
            except Exception:
                pass
    workspace = Path(runtime_root) / "output_requests" / capability / uuid4().hex
    saved_result = {**result, "report_path": str(workspace / "result.json")}
    try:
        atomic_write_json(workspace / "request.json", request)
        atomic_write_json(workspace / "result.json", saved_result)
    except OSError as exc:
        raise AgentToolError(
            code="REPORT_WRITE_FAILED", message="无法保存移仓查询报告。",
            hint="检查所选 runtime 的可用空间与写入权限后重试。",
        ) from exc
    return saved_result


__all__ = ["SHORT_PUT_ROLL_INPUT_SCHEMA", "short_put_roll_candidates"]
