"""Account-scoped, on-demand short-put replacement cash estimates."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
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
from domain.domain.symbol_identity import canonical_symbol, symbol_market
from domain.domain.trade_contract_identity import contract_key, contract_share_quantity, require_option_multiplier
from src.application.account_config import build_account_runtime_config, resolve_account_broker, resolve_configured_accounts
from src.application.agent_tool_contracts import AgentToolError
from src.application.market_data_provider import resolve_market_data_provider
from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.opend_fetch_config import opend_fetch_kwargs
from src.application.opend_symbol_fetching import FetchSymbolRequest
from src.application.opend_utils import normalize_underlier
from src.application.position_advice import _read_holdings
from src.application.position_advice_evidence import current_position_advice_evidence
from src.application.position_advice_inputs import select_ledger_positions
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
_INTERPRETATION = (
    "两笔单腿按旧 ask 买回、新 bid 卖出的扣费现金估算；不保证张数足额成交或组合成交。"
    "净增现金年化以新 strike 名义本金和今天至新到期日计算；"
    "剩余最高年化是到期归零情景的上限，不是预期收益。净收现金不等于整个交易链已盈利。"
)


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def _normalize_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    from src.application.tool_input_schema import build_tool_input_json_schema, validate_tool_input_payload

    validate_tool_input_payload(tool_name="short_put_roll_candidates", payload=dict(payload),
        schema=build_tool_input_json_schema(SHORT_PUT_ROLL_INPUT_SCHEMA), enforce_required=True)
    request = dict(payload)
    request["account"] = normalize_account(request["account"])
    request["lot_id"] = request["lot_id"].strip()
    if not request["account"] or not request["lot_id"]:
        raise ValueError("account and lot_id must not be blank")
    expiry = date.fromisoformat(request["max_new_expiration"])
    if expiry.isoformat() != request["max_new_expiration"]:
        raise ValueError("max_new_expiration must use YYYY-MM-DD")
    if "min_new_strike" in request:
        request["min_new_strike"] = _positive(request["min_new_strike"], "min_new_strike")
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
                     request: dict[str, Any], policy: Any) -> dict[str, Any]:
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
        validate_option_contract_evidence(old, mode="put", quote_side="buy", now_utc=now)
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
    complete = _complete_enumeration(bundle, old_expiry, max_expiry)
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
        if not (old_expiry <= expiry <= max_expiry and row.get("option_type") == "put"
                and strike < position["strike"] and strike < spot
                and (request.get("min_new_strike") is None or strike >= request["min_new_strike"])):
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
            validate_opening_contract_evidence(row, mode="put", now_utc=now)
            new_fee = estimate_futu_option_sell_fee(position["currency"], row["bid"], contracts=qty, multiplier=position["multiplier"])
            candidate = {**row, "contracts": qty}
            decision = policy.evaluate_short_put_roll(position, candidate, old_quote=old, spot=spot,
                old_dte=old_dte, new_dte=(expiry - today).days, old_buy_fee=old_fee.amount, new_sell_fee=new_fee.amount)
        except CandidateCalculationError as exc:
            if exc.reason in {"contract_ineligible", "option_non_standard", "option_type_mismatch"}:
                rejects[exc.reason] += 1
            else:
                missing[exc.reason] += 1
            continue
        except (ValueError, TypeError):
            missing["candidate_economics_unavailable"] += 1
            continue
        if not decision["accepted"]:
            rejects.update(decision["reason_codes"])
            continue
        metrics = decision["metrics"]
        matched.append({"contract": row["contract_symbol"], "expiration": row["expiration"], "strike": strike,
            **metrics, "old_buy_fee": old_fee.to_dict(), "new_sell_fee": new_fee.to_dict(),
            "evidence": {"old_ask": old["ask"], "new_bid": row["bid"], "new_ask": row["ask"],
                "old_snapshot_received_at_utc": old.get("snapshot_received_at_utc"),
                "snapshot_received_at_utc": row.get("snapshot_received_at_utc"),
                "spot_observed_at_utc": row.get("spot_observed_at_utc"),
                "delta": row.get("delta") if not row.get("delta_reason_code") else None,
                "delta_reason_code": row.get("delta_reason_code"), "iv": row.get("implied_volatility"),
                "term_matched_rv": row.get("term_matched_rv"), **requirement}})
    evaluated = discovered - sum(missing.values())
    coverage_status = "complete" if complete and not missing else "partial" if evaluated else "unavailable"
    ranked = policy.rank_short_put_roll_candidates(matched)
    result.update(status="success" if coverage_status == "complete" else "partial",
        outcome="found" if matched else "none" if coverage_status == "complete" else "unable",
        coverage={"status": coverage_status, "enumeration_complete": complete, "discovered": discovered,
            "evaluated": evaluated, "matched": len(matched), "excluded": evaluated - len(matched),
            "unassessed": sum(missing.values()) if complete else None},
        candidates=ranked[:request["limit"]], displayed=min(len(ranked), request["limit"]),
        omitted=max(0, len(ranked) - request["limit"]),
        exclusion_summary=[{"code": key, "count": value} for key, value in rejects.items()])
    result["position"].update(old_close_cost=old["ask"] * contract_share_quantity(qty, position["multiplier"]) + old_fee.amount,
        old_buy_fee=old_fee.to_dict(), old_dte=old_dte)
    result["position"]["old_remaining_max_annualized_return"] = policy.remaining_max_annualized_return(
        remaining_cash=result["position"]["old_close_cost"],
        capital_basis=position["strike"] * contract_share_quantity(qty, position["multiplier"]), dte=old_dte)
    result["reason_codes"].extend(missing)
    if not complete:
        result["reason_codes"].append("enumeration_incomplete")
    return result


def short_put_roll_candidates(payload: Mapping[str, Any], *, config: dict[str, Any], config_path: Path,
                             base_dir: Path, runtime_root: Path, now_utc: datetime | None = None,
                             deadline: float | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Estimate one lot's replacement choices without orders or ledger writes."""
    request = _normalize_request(payload)
    if now_utc is not None and now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    clock = (lambda: now_utc.astimezone(timezone.utc)) if now_utc is not None else (lambda: datetime.now(timezone.utc))
    started = clock()
    stop = deadline if deadline is not None else time.monotonic() + 90
    is_cancelled = cancelled or (lambda: False)
    account = request["account"]
    resolve_configured_accounts(config, [account])
    cfg = build_account_runtime_config(base_cfg=config, cfg_path=config_path, account=account, markets_to_run=[])
    result: dict[str, Any] = {"schema_version": "short_put_roll.output.v1", "status": "unavailable", "outcome": "unable",
        "account": account, "position": None, "evaluated_at_utc": started.isoformat(), "coverage": None,
        "candidates": [], "displayed": 0, "omitted": 0, "reason_codes": [],
        "pricing_basis": "leg_cashflow_estimate", "interpretation": _INTERPRETATION,
        "scope": {"max_new_expiration": request["max_new_expiration"], "min_new_strike": request.get("min_new_strike"),
            "old_otm_max_pct": 0.05, "new_put_must_be_otm": True}}
    plugin_status = position_advice_plugin_status(cfg)
    if plugin_status != "available":
        result.update(status=plugin_status, reason_codes=["position_advice_" + plugin_status])
        return result
    try:
        policy = require_position_advice_plugin()
    except PositionAdviceUnavailableError:
        result["reason_codes"] = ["position_advice_unavailable"]
        return result
    if not all(callable(getattr(policy, name, None)) for name in (
        "evaluate_short_put_roll", "rank_short_put_roll_candidates", "old_position_in_roll_scope", "remaining_max_annualized_return",
    )):
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
    if len(positions) != 1 or positions[0].get("option_type") != "put":
        raise ValueError("select exactly one open short put lot")
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
    if request.get("min_new_strike", 0) >= position["strike"]:
        raise ValueError("min_new_strike must be below the old strike")
    with runtime_root_scope(runtime_root):
        assert_quality_allows("close_advice", account=account, market=market.lower())
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
        result["position"].update(spot=spot, old_otm_pct=(spot - position["strike"]) / spot)
        if not policy.old_position_in_roll_scope(strike=position["strike"], spot=spot):
            result.update(status="success", outcome="not_applicable", reason_codes=["old_position_outside_roll_scope"])
            return result
        if request.get("min_new_strike", 0) >= spot:
            raise ValueError("min_new_strike must be below the current spot for an OTM replacement")
        check_run(stop, is_cancelled)
        gateway = provider.open_quote_gateway(host=route.host, port=route.port,
            is_option_chain_cache_enabled=True, deadline=stop, cancelled=is_cancelled)
        old_dte = (expiry - today).days
        fetch_request = FetchSymbolRequest(symbol=position["symbol"], host=route.host, port=route.port,
            market_data_provider=provider.name, base_dir=runtime_root, gateway=gateway, option_types="put",
            underlier_observation=observation.to_dict(), fetch_spot_if_missing=False, trading_date=today.isoformat(),
            min_dte=old_dte, max_dte=(max_expiry - today).days, include_realized_volatility=False,
            deadline=stop, cancelled=is_cancelled, chain_cache=True, freshness_policy="force_refresh", **limits,
            query_filters=[{"option_type": "put", "min_strike": position["strike"], "max_strike": position["strike"],
                "min_dte": old_dte, "max_dte": old_dte},
                {"option_type": "put", "min_strike": request.get("min_new_strike"),
                 "max_strike": min(position["strike"], spot), "min_dte": old_dte, "max_dte": (max_expiry - today).days}])
        bundle = provider.fetch_symbol_request(fetch_request)
        check_run(stop, is_cancelled)
        requirement = _current_requirement(provider=provider, gateway=gateway, symbol=position["symbol"], market=market,
            runtime_root=runtime_root, limits=limits, clock=clock, deadline=stop, cancelled=is_cancelled)
        evaluated_at = clock()
        result["evaluated_at_utc"] = evaluated_at.isoformat()
        if expiration_market_date(evaluated_at, market) != today:
            result["reason_codes"] = ["market_date_changed"]
            return result
        result = _evaluate_bundle(result=result, position=position, bundle=bundle, requirement=requirement,
            spot=spot, market=market, now=evaluated_at, request=request, policy=policy)
        check_run(stop, is_cancelled)
        result["reason_codes"] = list(dict.fromkeys(result["reason_codes"]))
        result["source"] = {"ledger": "canonical_position_lot", "lot_id": request["lot_id"], "market_data_provider": provider.name,
            "spot_observed_at_utc": observation.observed_at_utc, "effective_max_new_expiration": max_expiry.isoformat(),
            "min_new_strike": request.get("min_new_strike")}
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
    workspace = Path(runtime_root) / "output_requests" / "short_put_roll" / uuid4().hex
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
