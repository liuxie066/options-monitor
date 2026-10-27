"""Bounded, contract-only live opening queries using the canonical candidate owners."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Iterator

from domain.domain.engine.candidate_engine import (
    CandidateCalculationError, calculate_opening_candidate_metrics,
    evaluate_opening_candidate_policy, rank_candidate_rows, validate_opening_quote_freshness,
)
from domain.domain.candidate_defaults import resolve_candidate_liquidity
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.config_profiles import apply_profiles
from src.application.config_sections import resolve_templates_config, resolve_watchlist_config
from src.application.covered_call_strategy_risk import resolve_covered_call_underwriting_config
from src.application.earnings_calendar import earnings_calendar_scan_date
from src.application.futu_quote_routing import resolve_futu_quote_route
from src.application.opend_symbol_fetching import FetchSymbolRequest, fetch_symbol_request
from src.application.opening_quote_evidence import normalize_underlier_observation
from src.application.runtime_paths import resolve_runtime_root
from src.application.runtime_config_freshness import RuntimeConfigFreshnessError, ensure_runtime_config_freshness
from src.application.sell_put_strategy_risk import resolve_sell_put_underwriting_config
from src.application.symbol_calibration import calibrate_symbol
from src.infrastructure.exchange_rates import current_exchange_rate_snapshot, rates_for_purpose
from src.infrastructure.futu_gateway import build_ready_futu_quote_gateway

LOG = logging.getLogger(__name__)

MAX_QUERIES = 5
MAX_RESPONSE_BYTES = 16000
QUERY_INPUT_SCHEMA = {
    "queries": {
        "type": "array", "required": True, "minItems": 1, "maxItems": MAX_QUERIES,
        "items": {
            "type": "object", "additionalProperties": False,
            "required": ["symbol", "strategy"],
            "properties": {
                "symbol": {"type": "string", "minLength": 1, "maxLength": 32},
                "strategy": {"type": "string", "enum": ["short_put", "short_call"]},
                "strike": {"type": "number", "minimum": 0},
                "dte": {"type": "array", "minItems": 2, "maxItems": 2,
                        "items": {"type": "integer", "minimum": 1, "maximum": 365}},
            },
        },
    },
}
_CODE = re.compile(r"(?:[0-9]{1,5}(?:\.HK)?|HK\.[0-9]{1,5}|(?:US\.)?[A-Z][A-Z0-9.\-]{0,10})", re.I)


def normalize_queries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the entire batch before configuration or provider I/O."""
    from src.application.tool_input_schema import build_tool_input_json_schema, validate_tool_input_payload
    validate_tool_input_payload(tool_name="option_candidates", payload=payload,
                                schema=build_tool_input_json_schema(QUERY_INPUT_SCHEMA), enforce_required=True)
    normalized, errors, seen = [], [], set()
    for index, raw in enumerate(payload["queries"]):
        query = dict(raw)
        symbol = raw["symbol"].strip()
        identity = calibrate_symbol(symbol) if _CODE.fullmatch(symbol) else None
        if identity is None or identity.status != "ok" or symbol.upper().startswith(("SH.", "SZ.", "SG.", "JP.")):
            errors.append({"path": f"queries[{index}].symbol", "expected": "unambiguous US/HK underlier code"})
            continue
        query.update(symbol=identity.canonical_symbol, market=identity.market)
        if "strike" in raw:
            strike = raw["strike"]
            try:
                valid = math.isfinite(strike) and strike > 0
            except (OverflowError, TypeError):
                valid = False
            if not valid:
                errors.append({"path": f"queries[{index}].strike", "expected": "positive finite number"})
            else:
                query["strike"] = float(strike)
        if "dte" in raw and raw["dte"][0] > raw["dte"][1]:
            errors.append({"path": f"queries[{index}].dte", "expected": "inclusive ascending range"})
        key = (query["symbol"], query["strategy"], query.get("strike"), tuple(query.get("dte", [])))
        if key in seen:
            errors.append({"path": f"queries[{index}]", "expected": "unique normalized query"})
        seen.add(key)
        normalized.append(query)
    if errors:
        raise AgentToolError(code="INPUT_ERROR", message="Invalid option query", details={"schema_errors": errors[:10]})
    return normalized


@dataclass(frozen=True)
class QueryContext:
    deadline: float | None = None
    cancelled: Callable[[], bool] | None = None
    config_paths: dict[str, str] | None = None
    runtime_root: Path | None = None


_context: ContextVar[QueryContext] = ContextVar("option_candidates_context", default=QueryContext())


@contextmanager
def option_candidates_context(*, deadline=None, cancelled=None, config_paths=None, runtime_root=None) -> Iterator[None]:
    token = _context.set(QueryContext(deadline, cancelled, config_paths, Path(runtime_root) if runtime_root else None))
    try:
        yield
    finally:
        _context.reset(token)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PreparedQuery:
    query: dict
    config: Any
    filters: dict
    endpoint: tuple[str, int]
    query_date: date


def _prepare(query: dict, context: QueryContext, now: datetime) -> PreparedQuery:
    from src.application.pipeline_watchlist import resolve_watchlist_item_runtime_config

    market = query["market"].lower()
    if context.config_paths is not None and market not in context.config_paths:
        raise AgentToolError(code="PERMISSION_DENIED", message="market outside trusted query scope")
    path, config = load_runtime_config(config_key=market,
        config_path=context.config_paths.get(market) if context.config_paths is not None else None,
        expected_market=market)
    try:
        ensure_runtime_config_freshness(config, repo_root=repo_base(), market=market, runtime_config_path=path)
    except (RuntimeConfigFreshnessError, OSError) as exc:
        raise ValueError("runtime configuration unavailable or stale") from exc
    symbol = query["symbol"]
    matches = [raw for raw in resolve_watchlist_config(config)
               if calibrate_symbol(raw.get("symbol")).canonical_symbol == symbol]
    if len(matches) > 1:
        raise ValueError("ambiguous symbol configuration")
    try:
        effective = resolve_watchlist_item_runtime_config(
            item=matches[0] if matches else {"symbol": symbol},
            profiles=resolve_templates_config(config), apply_profiles_fn=apply_profiles)
    except SystemExit as exc:
        raise ValueError("invalid effective symbol configuration") from exc
    key = "sell_put" if query["strategy"] == "short_put" else "sell_call"
    liquidity = resolve_candidate_liquidity(effective.get(f"_global_{key}_liquidity"))
    raw_policy = {**(effective.get(key) or {}), "max_spread_ratio": liquidity.max_spread_ratio}
    resolver = resolve_sell_put_underwriting_config if key == "sell_put" else resolve_covered_call_underwriting_config
    policy = resolver(raw_policy)
    if "strike" in query:
        policy = replace(policy, min_strike=query["strike"], max_strike=query["strike"])
    if "dte" in query:
        policy = replace(policy, min_dte=query["dte"][0], max_dte=query["dte"][1])
    for bound in (policy.min_strike, policy.max_strike):
        if bound is not None and (not math.isfinite(bound) or bound <= 0):
            raise ValueError("configured strike must be positive finite")
    if policy.min_strike is not None and policy.max_strike is not None and policy.min_strike > policy.max_strike:
        raise ValueError("configured strike range reversed")
    if not (1 <= policy.min_dte <= policy.max_dte <= 365):
        raise AgentToolError(code="QUERY_RANGE_EXCEEDED", message="configured DTE outside query resource bounds")
    route = resolve_futu_quote_route(config, config_key=market, symbol=symbol)
    if not route.ok:
        raise ValueError(f"quote_route_{route.status}")
    filters = {"strike": query.get("strike"), "min_strike": policy.min_strike, "max_strike": policy.max_strike,
               "dte": [policy.min_dte, policy.max_dte],
               "strike_source": "request" if "strike" in query else "config",
               "dte_source": "request" if "dte" in query else "config"}
    return PreparedQuery(query, policy, filters, (route.host, route.port), earnings_calendar_scan_date(query["market"], now))


def _empty_result(query: dict, index: int, now: datetime) -> dict:
    return {"query_index": index, "symbol": query["symbol"], "strategy": query["strategy"],
            "market": query["market"], "currency": "HKD" if query["market"] == "HK" else "USD",
            "query_date": earnings_calendar_scan_date(query["market"], now).isoformat(),
            "evaluated_at": None, "effective_filters": None,
            "outcome": "unable", "coverage": {"status": "unavailable", "enumeration_complete": False,
                "discovered": None, "evaluated": 0, "matched": 0, "excluded": 0, "unassessed": None},
            "displayed": 0, "omitted": 0, "candidates": [], "reason_codes": ["not_started"],
            "exclusion_summary": [], "earnings_coverage": "unavailable"}


def _underlier_at(bundle: dict, now: datetime):
    observed = bundle.get("meta", {}).get("underlier_observation") or {}
    return normalize_underlier_observation(code=bundle.get("underlier_code", ""), market=observed.get("market", ""),
        snapshot_row={"code": observed.get("code"), "last_price": observed.get("last_price"),
                      "update_time": observed.get("update_time"), "sec_status": observed.get("sec_status"),
                      "suspension": observed.get("suspension")},
        market_state_row={"code": observed.get("code"), "market_state": observed.get("market_state")}, now_utc=now)


def _in_scope(row: dict, prepared: PreparedQuery) -> bool:
    cfg = prepared.config
    return (row.get("option_type") == prepared.query["strategy"].removeprefix("short_")
            and isinstance(row.get("dte"), int) and cfg.min_dte <= row["dte"] <= cfg.max_dte
            and isinstance(row.get("strike"), (int, float))
            and (cfg.min_strike is None or row["strike"] >= cfg.min_strike)
            and (cfg.max_strike is None or row["strike"] <= cfg.max_strike))


def _candidate(row: dict, decision: dict, mode: str) -> dict:
    evidence_fields = {
        "underlying_price": "spot", "bid": "bid", "ask": "ask", "mid": "mid", "last": "last_price",
        "spread_ratio": "spread_ratio", "iv": "implied_volatility", "term_matched_rv": "term_matched_rv",
        "iv_minus_rv": "iv_minus_rv", "iv_rv_ratio": "iv_rv_ratio", "delta": "delta",
        "open_interest": "open_interest", "volume": "volume", "snapshot_received_at": "snapshot_received_at_utc",
        "underlying_observed_at": "spot_observed_at_utc", "last_trade_time": "last_price_update_time",
        "estimated_full_sell_fees": "estimated_full_sell_fees",
    }
    return {"contract": row["contract_symbol"], "expiry": row["expiration"], "dte": row["dte"],
            "strike": row["strike"], "sell_limit": row["sell_limit"], "multiplier": row["multiplier"],
            "net_premium": row["net_income"], "period_net_return": row["period_net_return"],
            "obligation": ({"kind": "net_acquisition_price", "price": row["breakeven"]} if mode == "put" else
                           {"kind": "sale_price", "price": row["strike"], "above_spot_ratio": row["strike_above_spot_pct"]}),
            "reasons": [f"净收益{row['period_net_return']:.2%}；波动率溢价{row['iv_minus_rv']:.1%}"],
            "earnings": {"coverage": "unavailable", "events": [], "omitted_events": 0},
            "evidence": {**{key: row.get(source) for key, source in evidence_fields.items()},
                "iv_source": "opend", "annualized_net_return": row.get("annualized_net_return", row.get("annualized_net_premium_return")),
                "conditions": decision.get("conditions", [])}}


def _evaluate(prepared: PreparedQuery, bundle: dict, fx: dict, index: int, now: datetime) -> dict:
    query, cfg = prepared.query, prepared.config
    result = _empty_result(query, index, now)
    result["query_date"] = prepared.query_date.isoformat()
    result["evaluated_at"] = now.isoformat()
    result["effective_filters"] = dict(prepared.filters)
    result["reason_codes"] = []
    meta = bundle.get("meta", {})
    unique = {}
    for position, row in enumerate(bundle.get("rows", [])):
        if not _in_scope(row, prepared):
            continue
        identity = row.get("contract_symbol") or position
        previous = unique.get(identity)
        if previous is not None and previous != row:
            row = {**row, "opening_contract_status": "data_unavailable",
                   "opening_contract_reason_codes": ["contract_identity_conflict"]}
        unique[identity] = row
    rows = list(unique.values())
    # Enumeration is a chain fact; missing snapshots/RV do not erase its denominator.
    statuses = meta.get("expiration_statuses", {})
    expirations = [expiry for expiry in meta.get("available_expirations", [])
                   if cfg.min_dte <= (date.fromisoformat(expiry) - prepared.query_date).days <= cfg.max_dte]
    complete = (isinstance(meta.get("available_expirations"), list)
                and isinstance(meta.get("expiration_statuses"), dict)
                and meta.get("expiration_enumeration_complete") is True
                and meta.get("query_scope_validated") is True
                and all(statuses.get(expiry) in {"cache", "fetched", "empty"} for expiry in expirations))
    market = _underlier_at(bundle, now)
    market_reason = None
    if earnings_calendar_scan_date(query["market"], now) != prepared.query_date:
        market_reason = "date_changed"
    elif market.status != "ready":
        market_reason = market.reason_code or "market_unavailable"
    reasons, excluded = Counter(), Counter()
    matched, unknown = [], 0
    rates = rates_for_purpose(fx, purpose="capacity", now=now)
    rate = rates.get("HKDCNY" if query["market"] == "HK" else "USDCNY")
    mode = query["strategy"].removeprefix("short_")
    for raw in rows:
        contract = raw.get("contract_symbol")
        if not isinstance(contract, str) or len(contract) > 64 or not contract.isascii() or not re.fullmatch(r"[A-Z0-9.]+", contract):
            unknown += 1
            reasons["contract_identity_invalid"] += 1
            continue
        if market_reason or rate is None:
            unknown += 1
            reasons[market_reason or "fx_unavailable"] += 1
            continue
        try:
            validate_opening_quote_freshness(raw, now_utc=now)
            metrics = calculate_opening_candidate_metrics(raw, mode=mode, cny_per_currency_unit=rate, now_utc=now)
        except CandidateCalculationError as exc:
            # Affirmative ineligibility is distinct from missing/stale evidence.
            if exc.reason in {"contract_ineligible", "policy_rejected", "option_non_standard", "net_premium_non_positive", "net_cash_basis_non_positive"}:
                excluded[exc.reason] += 1
            else:
                unknown += 1
                reasons[exc.reason] += 1
            continue
        row = {**raw, **metrics, "decision_now_utc": now.isoformat()}
        decision = evaluate_opening_candidate_policy(row, mode=mode, min_dte=cfg.min_dte, max_dte=cfg.max_dte,
            min_strike=cfg.min_strike, max_strike=cfg.max_strike, min_annualized_return=cfg.min_annualized_return,
            min_net_premium_cny=cfg.min_net_income, min_iv_rv_ratio=cfg.min_iv_rv_ratio,
            min_iv_minus_rv=cfg.min_iv_minus_rv, max_spread_ratio=cfg.max_spread_ratio, max_abs_delta=cfg.max_abs_delta,
            check_capacity=False, require_earnings_evidence=False, reject_known_earnings=False)
        if decision["accepted"]:
            row["_candidate"] = _candidate(row, decision, mode)
            matched.append(row)
        elif any(item["reason"] == "risk_delta_unavailable" for item in decision["rejects"]):
            unknown += 1
            reasons["risk_delta_unavailable"] += 1
        else:
            excluded[(decision["rejects"] or [{}])[0].get("reason", "policy_rejected")] += 1
    excluded_count = sum(excluded.values())
    evaluated = len(matched) + excluded_count
    if market_reason:
        reasons[market_reason] += 1
    if not complete:
        reasons["query_range_exceeded" if str(meta.get("error_code") or "").lower() == "query_range_exceeded" else "enumeration_incomplete"] += 1
    elif not rows and not market_reason:
        reasons["no_contracts_in_range"] += 1
    status = "complete" if complete and unknown == 0 and not market_reason else "partial" if evaluated else "unavailable"
    result.update(outcome="found" if matched else "none" if status == "complete" else "unable",
                  coverage={"status": status, "enumeration_complete": complete, "discovered": len(rows),
                            "evaluated": evaluated, "matched": len(matched), "excluded": excluded_count,
                            "unassessed": unknown if complete else None},
                  candidates=[row["_candidate"] for row in rank_candidate_rows(matched, mode=mode)[:3]],
                  reason_codes=list(reasons)[:8], exclusion_summary=[{"code": key, "count": count} for key, count in excluded.most_common(3)])
    result["displayed"] = len(result["candidates"])
    result["omitted"] = len(matched) - result["displayed"]
    return result


def _bound_response(data: dict, warnings: list[str], meta: dict) -> tuple[dict, list[str], dict]:
    def size() -> int:
        envelope = build_response(tool_name="option_candidates", ok=True, data=data, warnings=warnings, meta=meta)
        return len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))

    if size() <= MAX_RESPONSE_BYTES:
        return data, warnings, meta
    for result in data["results"]:
        for row in result["candidates"]:
            row["evidence"] = None
        if result["candidates"]:
            result["reason_codes"] = list(dict.fromkeys([*result["reason_codes"][:7], "output_limited"]))
    if size() <= MAX_RESPONSE_BYTES:
        return data, warnings, meta
    for result in data["results"]:
        result["candidates"] = result["candidates"][:1]
        result["displayed"] = len(result["candidates"])
        result["omitted"] = result["coverage"]["matched"] - result["displayed"]
        result["reason_codes"] = list(dict.fromkeys([*result["reason_codes"][:2], "output_limited"]))
        for row in result["candidates"]:
            row["reasons"] = [row["reasons"][0].encode("utf-8")[:80].decode("utf-8", errors="ignore")]
            earnings = row["earnings"]
            earnings["omitted_events"] += max(0, len(earnings["events"]) - 1)
            earnings["events"] = earnings["events"][:1]
        _earnings_summary(result)
    if size() > MAX_RESPONSE_BYTES:
        raise AgentToolError(code="INTERNAL_ERROR", message="option query minimum summary exceeds output budget")
    return data, warnings, meta


def _earnings_summary(result: dict) -> None:
    coverages = [row["earnings"]["coverage"] for row in result["candidates"]]
    result["earnings_coverage"] = ("complete" if coverages and all(value == "complete" for value in coverages)
                                   else "partial" if any(value != "unavailable" for value in coverages) else "unavailable")
    if coverages and result["earnings_coverage"] != "complete":
        result["reason_codes"] = list(dict.fromkeys(["earnings_incomplete", *result["reason_codes"]]))[:8]


def option_candidates_tool(payload: dict[str, Any]) -> tuple[dict, list[str], dict]:
    """Keep completed financial decisions even if the annotation/provider worker stalls."""
    from src.application.read_execution import RunStopped, bounded_call
    from src.application.opend_symbol_fetching import QueryFetchCache

    queries = normalize_queries(payload)
    context = _context.get()
    started, start_time = time.monotonic(), _now()
    response_deadline = min(started + 60, context.deadline - 2 if context.deadline is not None else math.inf)
    work_deadline = response_deadline - 2
    runtime = context.runtime_root or resolve_runtime_root(repo_root=repo_base()).runtime_root
    lock, stopped = threading.Lock(), threading.Event()
    prepared: dict[int, PreparedQuery] = {}
    completed: dict[int, tuple[dict, dict]] = {}
    preliminary = [_empty_result(query, index, start_time) for index, query in enumerate(queries)]
    intervals: dict[tuple, list] = {}
    earnings_requests: dict[tuple, dict] = {}
    annotation_deadline = [math.inf]
    closed = [False]
    diagnostics: dict[str, Any] = {}

    def cancelled() -> bool:
        return stopped.is_set() or bool(context.cancelled and context.cancelled())

    def check() -> None:
        if cancelled() or time.monotonic() >= work_deadline:
            raise TimeoutError("option query budget exhausted")

    def publish(index: int, result: dict, evidence: tuple[dict, dict] | None = None) -> None:
        with lock:
            if not closed[0] and not cancelled() and time.monotonic() < work_deadline:
                preliminary[index] = deepcopy(result)
                if evidence is not None:
                    completed[index] = evidence

    def work() -> None:
        gateways, bundles = {}, {}
        fetch_cache = QueryFetchCache()
        fx = None
        try:
            # Configuration is resolved once before deciding which requests can share facts.
            for index, query in enumerate(queries):
                check()
                try:
                    item = _prepare(query, context, start_time)
                except (AgentToolError, ValueError) as exc:
                    if isinstance(exc, AgentToolError) and exc.code == "PERMISSION_DENIED":
                        raise
                    result = _empty_result(query, index, start_time)
                    result["reason_codes"] = ["query_range_exceeded" if isinstance(exc, AgentToolError) and exc.code == "QUERY_RANGE_EXCEEDED" else "configuration_unavailable"]
                    publish(index, result)
                    continue
                with lock:
                    if closed[0]:
                        return
                    prepared[index] = item
                    preliminary[index]["effective_filters"] = dict(item.filters)
            for index, query in enumerate(queries):
                if index not in prepared:
                    continue
                check()
                item = prepared[index]
                with lock:
                    preliminary[index]["reason_codes"] = ["budget_exhausted"]
                key = (*item.endpoint, query["symbol"], item.query_date)
                if key not in bundles:
                    peers = [peer for peer in prepared.values() if (*peer.endpoint, peer.query["symbol"], peer.query_date) == key]
                    if item.endpoint not in gateways:
                        check()
                        connect_start = time.monotonic()
                        try:
                            gateway = build_ready_futu_quote_gateway(host=item.endpoint[0], port=item.endpoint[1],
                                is_option_chain_cache_enabled=True, deadline=work_deadline, cancelled=cancelled)
                        except Exception:
                            check()
                            result = _empty_result(query, index, start_time)
                            result.update(effective_filters=item.filters, reason_codes=["quote_connection_unavailable"])
                            publish(index, result)
                            continue
                        gateways[item.endpoint] = gateway
                        diagnostics["connection_ms"] = diagnostics.get("connection_ms", 0) + round((time.monotonic() - connect_start) * 1000)
                        check()
                    request = FetchSymbolRequest(symbol=query["symbol"], host=item.endpoint[0], port=item.endpoint[1],
                        base_dir=runtime, gateway=gateways[item.endpoint], option_types=",".join(sorted({peer.query["strategy"].removeprefix("short_") for peer in peers})),
                        min_dte=min(peer.config.min_dte for peer in peers), max_dte=max(peer.config.max_dte for peer in peers),
                        include_realized_volatility=True, chain_cache=True,
                        deadline=work_deadline, cancelled=cancelled, query_cache=fetch_cache,
                        query_filters=[{"option_type": peer.query["strategy"].removeprefix("short_"),
                                        "min_strike": peer.config.min_strike, "max_strike": peer.config.max_strike,
                                        "min_dte": peer.config.min_dte, "max_dte": peer.config.max_dte} for peer in peers])
                    fetch_start = time.monotonic()
                    bundles[key] = fetch_symbol_request(request)
                    diagnostics["fetch_ms"] = diagnostics.get("fetch_ms", 0) + round((time.monotonic() - fetch_start) * 1000)
                    fetch_meta = bundles[key].get("meta", {})
                    for field in ("opend_call_count", "expiration_opend_calls", "snapshot_opend_call_count", "spot_snapshot_opend_calls", "spot_market_state_opend_calls", "expiration_cache_hits", "rate_gate_wait_sec", "option_codes"):
                        diagnostics[field] = diagnostics.get(field, 0) + (fetch_meta.get(field) or 0)
                    check()
                else:
                    diagnostics["bundle_reuse_hits"] = diagnostics.get("bundle_reuse_hits", 0) + 1
                bundle = bundles[key]
                if fx is None and any(_in_scope(row, item) for row in bundle.get("rows", [])) and _underlier_at(bundle, _now()).status == "ready":
                    check()
                    fx_start = time.monotonic()
                    fx = current_exchange_rate_snapshot(cache_path=runtime / "output_shared/state/rate_cache.json",
                                                        write_cache=False, deadline=work_deadline, cancelled=cancelled)
                    diagnostics["fx_ms"] = round((time.monotonic() - fx_start) * 1000)
                    check()
                evaluation_start = time.monotonic()
                result = _evaluate(item, bundle, fx or {}, index, _now())
                diagnostics["evaluation_ms"] = diagnostics.get("evaluation_ms", 0) + round((time.monotonic() - evaluation_start) * 1000)
                publish(index, result, (bundle, fx or {}))
            check()
            with lock:
                annotation_deadline[0] = min(work_deadline, time.monotonic() + 5)
            earnings_start = time.monotonic()
            _collect_earnings(prepared, preliminary, gateways, earnings_requests, intervals, lock,
                              closed, annotation_deadline[0], cancelled)
            diagnostics["earnings_ms"] = round((time.monotonic() - earnings_start) * 1000)
        except TimeoutError:
            pass
        finally:
            diagnostics["fact_reuse_hits"] = fetch_cache.reuse_hits
            diagnostics["fetch"] = fetch_cache.diagnostics
            LOG.info("option_candidates diagnostic=%s", diagnostics)
            for gateway in gateways.values():
                try:
                    gateway.close()
                except Exception:
                    LOG.warning("option_candidates quote connection cleanup failed")

    try:
        bounded_call(work, deadline=work_deadline,
                     cancelled=lambda: cancelled() or time.monotonic() >= annotation_deadline[0])
    except RunStopped:
        pass
    finally:
        with lock:
            closed[0] = True
            stopped.set()
            results = deepcopy(preliminary)
            frozen_prepared = dict(prepared)
            frozen_completed = dict(completed)
            frozen_intervals = deepcopy(intervals)
            frozen_requests = deepcopy(earnings_requests)
    final_time = _now()
    evaluation_start = time.monotonic()
    for index, (bundle, fx) in frozen_completed.items():
        results[index] = _evaluate(frozen_prepared[index], bundle, fx, index, final_time)
    _annotate_earnings(results, frozen_prepared, frozen_requests, frozen_intervals)
    statuses = [result["coverage"]["status"] for result in results]
    completion = ("complete" if all(value == "complete" for value in statuses) else
                  "partial" if any(result["coverage"]["evaluated"] or result["outcome"] == "none" for result in results) else "unavailable")
    data = {"evaluation_scope": "contract_only", "completion": completion, "results": results,
            "limits": {"max_queries": 5, "max_dte": 365, "budget_seconds": 60, "max_candidates_per_query": 3},
            "account_checks": "not_evaluated"}
    warnings = ["仅筛合约，未检查账户资金或持股；参考限价不保证成交，费用与净收益为估算。"]
    if any(result["candidates"] and result["earnings_coverage"] != "complete" for result in results):
        warnings.append("财报信息不完整。")
    meta = {"started_at": start_time.isoformat(), "completed_at": final_time.isoformat(),
            "elapsed_ms": round((time.monotonic() - started) * 1000)}
    final_evaluation_ms = round((time.monotonic() - evaluation_start) * 1000)
    output_start = time.monotonic()
    result = _bound_response(data, warnings, meta)
    LOG.info("option_candidates response elapsed_ms=%s final_evaluation_ms=%s output_ms=%s",
             meta["elapsed_ms"], final_evaluation_ms, round((time.monotonic() - output_start) * 1000))
    return result


def _earnings_key(prepared: PreparedQuery) -> tuple:
    return (*prepared.endpoint, prepared.query["market"], prepared.query_date)


def _collect_earnings(prepared, results, gateways, requests, intervals, lock, closed, deadline, cancelled):
    from src.application.earnings_calendar import fetch_market_earnings_calendar

    groups: dict[tuple, dict[str, set[str]]] = {}
    for index, item in prepared.items():
        code = calibrate_symbol(item.query["symbol"]).futu_code
        expirations = {row["expiry"] for row in results[index]["candidates"]}
        if expirations:
            groups.setdefault(_earnings_key(item), {}).setdefault(code, set()).update(expirations)
    for key, expirations in groups.items():
        if cancelled() or time.monotonic() >= deadline:
            return
        scan_at = _now()
        if earnings_calendar_scan_date(key[2], scan_at) != key[3]:
            continue
        request = {"market": key[2], "scan_date": key[3], "scan_at_utc": scan_at,
                   "expirations_by_underlier": {code: sorted(values) for code, values in expirations.items()}}
        with lock:
            if closed[0]:
                return
            requests[key] = request
            intervals[key] = []

        def accept(observation, group=key):
            # Interval handoff and freeze share one lock; late SDK results cannot publish.
            with lock:
                if closed[0] or cancelled() or time.monotonic() >= deadline:
                    return
                copied = {field: value for field, value in observation.items() if field != "events"}
                copied["events"] = [dict(event) for event in observation["events"]]
                intervals[group].append(copied)

        fetch_market_earnings_calendar(gateway=gateways[key[:2]], **request,
                                      deadline=deadline, cancelled=cancelled, on_interval=accept)


def _annotate_earnings(results, prepared, requests, intervals):
    from src.application.earnings_calendar import earnings_calendar_snapshot_from_intervals, project_earnings_for_expiry

    snapshots = {key: earnings_calendar_snapshot_from_intervals(**request, intervals=intervals.get(key, []))
                 for key, request in requests.items()}
    for index, result in enumerate(results):
        item = prepared.get(index)
        snapshot = snapshots.get(_earnings_key(item)) if item else None
        if snapshot is not None:
            code = calibrate_symbol(item.query["symbol"]).futu_code
            for candidate in result["candidates"]:
                if candidate["expiry"] not in snapshot["expirations_by_underlier"].get(code, []):
                    continue
                evidence = project_earnings_for_expiry(snapshot, underlier_code=code, expiration=candidate["expiry"])
                complete = (evidence["hard_coverage_status"] == "complete"
                            and evidence["soft_coverage_status"] in {"complete", "not_applicable"})
                any_coverage = any(row["status"] == "ok" and row["start"] <= candidate["expiry"]
                                   for row in snapshot["intervals"])
                events = sorted({(event["earnings_date"], event["days_before_expiration"])
                                 for event in evidence["events"]})
                candidate["earnings"] = {
                    "coverage": "complete" if complete else "partial" if any_coverage or events else "unavailable",
                    "events": [{"date": day, "days_before_expiry": days} for day, days in events[:3]],
                    "omitted_events": max(0, len(events) - 3)}
        _earnings_summary(result)
