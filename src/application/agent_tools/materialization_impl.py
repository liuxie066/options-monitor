from __future__ import annotations

import csv
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Mapping, cast
import json
import re
import uuid

from src.application.agent_tool_contracts import AgentToolError
from domain.domain.close_advice import (
    DECISION_EVIDENCE_COMPLETE,
    RECOMMENDATION_CLOSE,
    RECOMMENDATION_HOLD,
    RECOMMENDATION_NOT_EVALUABLE,
    STRICT_CLOSE_POLICY_VERSION,
)
from domain.domain.ledger.position_fields import normalize_account
from domain.domain.performance.period import PeriodRequest, PeriodWindow, normalize_period
from domain.domain.strategy_vocab import (
    STRATEGY_COVERED_CALL,
    STRATEGY_SELL_PUT,
    STRATEGY_YIELD_ENHANCEMENT,
    canonical_strategy_id,
)
from domain.domain.trade_contract_identity import (
    contract_key,
    normalize_contract_expiration,
    normalize_contract_option_type,
)
from src.application.expiration_normalization import find_unique_near_miss_expiration
from src.application.close_advice_quote_cache import (
    DEFAULT_QUOTE_MAX_AGE_SEC,
    publish_quote_cache_metadata,
    validate_quote_cache_metadata,
)
from src.application.close_advice_report_manifest import (
    read_close_advice_report_snapshot,
)
from src.application.opend_fetch_config import opend_fetch_kwargs
from src.application.account_config import accounts_from_config
from src.application.performance.adapters import (
    assigned_stock_instruments,
    load_assigned_stock_projection,
    load_ledger_performance_inputs,
    load_option_valuation_inputs,
)
from src.application.performance.evidence_collection import collect_current_performance_evidence
from src.application.symbol_mutations import normalize_symbol_read


def _normalize_expiration(value: Any) -> str:
    return normalize_contract_expiration(value, fallback_raw=True) or ""


def _normalize_option_type(value: Any) -> str:
    return normalize_contract_option_type(value)


def _as_float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _contract_key(symbol: Any, option_type: Any, expiration: Any, strike: Any) -> tuple[str, str, str, str]:
    return contract_key(symbol, option_type, expiration, strike, expiration_fallback_raw=True)


def _position_expiration_for_fetch(row: dict[str, Any]) -> str:
    for value in (
        row.get("expiration_ymd"),
        row.get("expiration"),
    ):
        exp = _normalize_expiration(value)
        if exp:
            return exp
    note = str(row.get("note") or "")
    for token in note.replace(";", " ").split():
        if token.startswith("exp="):
            return _normalize_expiration(token.split("=", 1)[1])
    return ""


def _close_advice_scope_root(out_root: Path, payload: dict[str, Any]) -> Path:
    raw = str(payload.get("_close_advice_scope_id") or "").strip()
    if not raw:
        return out_root
    scope_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw).strip(".-")
    if not scope_id:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="close_advice request scope is invalid",
        )
    return (out_root / "requests" / scope_id).resolve()


def _validate_close_advice_context(ctx: Any) -> dict[str, Any]:
    if not isinstance(ctx, dict):
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="option positions context is unavailable",
        )
    status = str(ctx.get("context_status") or "").strip().lower()
    ledger = ctx.get("ledger") if isinstance(ctx.get("ledger"), dict) else {}
    if status == "unavailable" or bool(ledger.get("fail_closed")):
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="option positions context is explicitly unavailable",
            details={
                "context_status": status or None,
                "ledger_status": ledger.get("status"),
            },
        )
    if not isinstance(ctx.get("open_positions_min"), list):
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="option positions context has no valid open_positions_min list",
        )
    return ctx


def _extract_position_fetch_requirements(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    rows = ctx.get("open_positions_min") if isinstance(ctx, dict) else []
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol_read(row.get("symbol"))
        if not symbol:
            continue
        item = grouped.get(symbol)
        if item is None:
            item = {
                "symbol": symbol,
                "requested_expirations": set(),
                "option_types": set(),
                "strikes": [],
                "requested_contracts": set(),
                "position_count": 0,
            }
            grouped[symbol] = item
            order.append(symbol)
        position_count = item.get("position_count")
        item["position_count"] = (position_count if isinstance(position_count, int) else 0) + 1
        option_type = _normalize_option_type(row.get("option_type"))
        expiration = _position_expiration_for_fetch(row)
        strike_num = _as_float_or_none(row.get("strike"))
        if option_type:
            cast(set[str], item["option_types"]).add(option_type)
        if expiration:
            cast(set[str], item["requested_expirations"]).add(expiration)
        if strike_num is not None:
            cast(list[float], item["strikes"]).append(strike_num)
        key = _contract_key(symbol, option_type, expiration, strike_num)
        if all(key):
            cast(set[tuple[str, str, str, str]], item["requested_contracts"]).add(key)
    out: list[dict[str, Any]] = []
    for symbol in order:
        item = grouped[symbol]
        strikes = [float(v) for v in cast(list[float], item["strikes"])]
        out.append(
            {
                "symbol": symbol,
                "requested_expirations": sorted(item["requested_expirations"]),
                "option_types": sorted(item["option_types"]),
                "min_strike": min(strikes) if strikes else None,
                "max_strike": max(strikes) if strikes else None,
                "requested_contracts": set(item["requested_contracts"]),
                "position_count": int(item["position_count"]),
            }
        )
    return out


def _read_required_data_coverage(csv_path: Path) -> tuple[set[tuple[str, str, str, str]], set[str]]:
    contract_keys: set[tuple[str, str, str, str]] = set()
    expirations: set[str] = set()
    if not csv_path.exists():
        return contract_keys, expirations
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                if not isinstance(row, dict):
                    continue
                key = _contract_key(
                    row.get("symbol"),
                    row.get("option_type"),
                    row.get("expiration"),
                    row.get("strike"),
                )
                if all(key):
                    contract_keys.add(key)
                    expirations.add(key[2])
    except Exception:
        return set(), set()
    return contract_keys, expirations


def _count_required_data_rows(csv_path: Path) -> int:
    if not csv_path.exists():
        return 0
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as fh:
            return sum(1 for row in csv.DictReader(fh) if isinstance(row, dict))
    except Exception:
        return 0


def _find_contract_expiration_near_misses(
    requested_contracts: set[tuple[str, str, str, str]],
    available_contracts: set[tuple[str, str, str, str]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in sorted(requested_contracts):
        if key in available_contracts or not all(key):
            continue
        symbol, option_type, expiration, strike = key
        candidate_expirations = [
            avail_exp
            for avail_symbol, avail_option_type, avail_exp, avail_strike in available_contracts
            if avail_symbol == symbol and avail_option_type == option_type and avail_strike == strike
        ]
        near_miss = find_unique_near_miss_expiration(expiration, candidate_expirations)
        if not near_miss:
            continue
        out.append(
            {
                "symbol": symbol,
                "option_type": option_type,
                "strike": _as_float_or_none(strike),
                "requested_expiration": expiration,
                "matched_expiration": near_miss,
                "quote_key": "|".join(key),
            }
        )
    return out


def _build_coverage_summary(symbol_rows: list[dict[str, Any]]) -> dict[str, Any]:
    missing_symbols = [
        str(item.get("symbol") or "")
        for item in symbol_rows
        if isinstance(item, dict) and not bool(item.get("position_coverage_ok"))
    ]
    return {
        "symbol_count": len(symbol_rows),
        "position_count": sum(int(item.get("position_count") or 0) for item in symbol_rows if isinstance(item, dict)),
        "covered_symbol_count": sum(1 for item in symbol_rows if isinstance(item, dict) and bool(item.get("position_coverage_ok"))),
        "symbols_with_missing_coverage": missing_symbols,
        "positions_missing_coverage": sum(int(item.get("missing_contract_count") or 0) for item in symbol_rows if isinstance(item, dict)),
        "expiration_near_miss_count": sum(len(item.get("expiration_near_misses") or []) for item in symbol_rows if isinstance(item, dict)),
    }


def scan_summary_rows(summary_rows: list[dict[str, Any]], *, as_float: Callable[[Any], float | None]) -> dict[str, Any]:
    strategy_counts = {STRATEGY_SELL_PUT: 0, STRATEGY_COVERED_CALL: 0, STRATEGY_YIELD_ENHANCEMENT: 0}
    account_counts: dict[str, int] = {}
    symbol_counts: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []
    for row in summary_rows:
        if not isinstance(row, dict):
            continue
        raw_strategy = str(row.get("side") or row.get("strategy") or row.get("option_strategy") or "").strip()
        strategy = canonical_strategy_id(raw_strategy) if raw_strategy else ""
        if strategy in strategy_counts:
            strategy_counts[strategy] += 1
        account = normalize_account(row.get("account") or row.get("account_label"))
        if account:
            account_counts[account] = account_counts.get(account, 0) + 1
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        candidates.append(
            {
                "symbol": symbol or None,
                "account": account or None,
                "strategy": strategy or None,
                "net_income": as_float(row.get("net_income")),
                "annualized_return": as_float(row.get("annualized_net_return") or row.get("annualized_return") or row.get("annualized")),
                "strike": as_float(row.get("strike")),
                "expiration": (str(row.get("expiration") or "").strip() or None),
            }
        )
    top_candidates = sorted(
        candidates,
        key=lambda item: (
            -(item["net_income"] if item["net_income"] is not None else -10**12),
            -(item["annualized_return"] if item["annualized_return"] is not None else -10**12),
        ),
    )[:5]
    return {
        "row_count": len(summary_rows),
        "symbol_count": len(symbol_counts),
        "strategy_counts": strategy_counts,
        "account_counts": account_counts,
        "top_candidates": top_candidates,
    }


def close_advice_rows_summary(
    csv_path: Path,
    text_path: Path,
    *,
    safe_read_csv: Callable[[Path], Any],
    as_float: Callable[[Any], float | None],
    csv_bytes: bytes | None = None,
    text_bytes: bytes | None = None,
) -> dict[str, Any]:
    if csv_bytes is None:
        df = safe_read_csv(csv_path)
        rows = df.to_dict(orient="records") if not df.empty else []
    else:
        try:
            rows = [
                {
                    str(key): value
                    for key, value in raw.items()
                    if key is not None
                }
                for raw in csv.DictReader(
                    StringIO(csv_bytes.decode("utf-8-sig"), newline="")
                )
                if isinstance(raw, dict)
            ]
        except (UnicodeError, csv.Error):
            rows = []
    recommendation_counts: dict[str, int] = {}
    account_counts: dict[str, int] = {}
    top_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (
            str(row.get("policy_version") or "").strip()
            != STRICT_CLOSE_POLICY_VERSION
            or str(row.get("evaluation_status") or "").strip().lower()
            != "priced"
            or str(row.get("decision_evidence_status") or "").strip().lower()
            != DECISION_EVIDENCE_COMPLETE
        ):
            continue
        recommendation = str(row.get("recommendation_state") or "").strip().lower()
        if recommendation not in {RECOMMENDATION_CLOSE, RECOMMENDATION_HOLD}:
            continue
        recommendation_counts[recommendation] = (
            recommendation_counts.get(recommendation, 0) + 1
        )
        account = normalize_account(row.get("account"))
        if account:
            account_counts[account] = account_counts.get(account, 0) + 1
        top_rows.append(
            {
                "account": account or None,
                "symbol": (str(row.get("symbol") or "").strip().upper() or None),
                "option_type": (str(row.get("option_type") or "").strip().lower() or None),
                "expiration": (str(row.get("expiration") or "").strip() or None),
                "strike": as_float(row.get("strike")),
                "recommendation_state": recommendation,
                "net_capture_ratio": as_float(row.get("net_capture_ratio")),
                "all_in_close_cost": as_float(row.get("all_in_close_cost")),
            }
        )
    top_rows = sorted(
        top_rows,
        key=lambda item: (
            {
                RECOMMENDATION_CLOSE: 0,
                RECOMMENDATION_HOLD: 1,
                RECOMMENDATION_NOT_EVALUABLE: 2,
            }.get(str(item.get("recommendation_state") or ""), 3),
            -(
                item["net_capture_ratio"]
                if item["net_capture_ratio"] is not None
                else -1.0
            ),
        ),
    )[:5]
    if text_bytes is not None:
        try:
            notification_preview = text_bytes.decode("utf-8").strip()
        except UnicodeError:
            notification_preview = ""
    else:
        try:
            notification_preview = text_path.read_text(encoding="utf-8").strip()
        except Exception:
            notification_preview = ""
    return {
        "row_count": len(rows),
        "recommendation_counts": recommendation_counts,
        "account_counts": account_counts,
        "top_rows": top_rows,
        "notification_preview": notification_preview,
    }


def query_cash_headroom_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config,
    resolve_public_data_config_path,
    normalize_broker,
    resolve_output_root,
    query_sell_put_cash,
    repo_base,
    mask_path,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    data_config_path = resolve_public_data_config_path(payload, portfolio_cfg)
    broker = normalize_broker(payload.get("broker") or portfolio_cfg.get("broker"))
    out_root = resolve_output_root(payload.get("output_dir"))
    out_dir = (out_root / "query_cash_headroom").resolve()
    result = query_sell_put_cash(
        config=str(config_path),
        data_config=str(data_config_path),
        market=broker,
        account=(str(payload.get("account")).strip() if payload.get("account") else None),
        output_format="json",
        top=int(payload.get("top") or 10),
        no_exchange_rates=bool(payload.get("no_exchange_rates", False)),
        out_dir=str(out_dir),
        base_dir=repo_base(),
        runtime_config=cfg,
        write_cache=False,
    )
    return result, [], {"config_path": mask_path(config_path), "output_dir": mask_path(out_dir)}


_OPTION_PERFORMANCE_INPUT_FIELDS = frozenset(
    {
        "config_key",
        "config_path",
        "data_config",
        "account",
        "broker",
        "period",
        "as_of_date",
        "month",
        "year",
        "start_date",
        "end_date",
        "include_rows",
        "refresh_quotes",
    }
)


def normalize_option_performance_request(
    payload: dict[str, Any],
    *,
    normalize_broker,
    now_ms: int | None = None,
) -> tuple[dict[str, Any], PeriodWindow]:
    extras = sorted(str(key) for key in payload if key not in _OPTION_PERFORMANCE_INPUT_FIELDS)
    if extras:
        raise AgentToolError(
            "INVALID_ARGUMENT",
            f"option_performance_report does not accept: {', '.join(extras)}",
        )
    config_key = str(payload.get("config_key") or "us").strip().lower()
    if config_key not in {"us", "hk"}:
        raise AgentToolError("INVALID_ARGUMENT", "config_key must be us or hk")
    for field in ("include_rows", "refresh_quotes"):
        value = payload.get(field)
        if value is not None and not isinstance(value, bool):
            raise AgentToolError("INVALID_ARGUMENT", f"{field} must be a boolean")
    try:
        period_request = PeriodRequest.from_mapping(payload)
        window = normalize_period(period_request, now_ms=now_ms)
    except ValueError as exc:
        raise AgentToolError("INVALID_ARGUMENT", str(exc)) from exc

    raw_account = str(payload.get("account") or "").strip()
    account = normalize_account(raw_account) if raw_account else None
    if raw_account and not account:
        raise AgentToolError("INVALID_ARGUMENT", "account is invalid")
    raw_broker = str(payload.get("broker") or "").strip()
    broker = normalize_broker(raw_broker) if raw_broker else None
    normalized = {
        "config_key": config_key,
        "config_path": payload.get("config_path"),
        "data_config": payload.get("data_config"),
        "account": account,
        "broker": broker,
        "period": period_request.period,
        "as_of_date": period_request.as_of_date,
        "month": period_request.month,
        "year": period_request.year,
        "start_date": period_request.start_date,
        "end_date": period_request.end_date,
        "include_rows": bool(payload.get("include_rows", False)),
        "refresh_quotes": bool(payload.get("refresh_quotes", True)),
    }
    return normalized, window


def _row_order_key(row: dict[str, Any]) -> tuple[int, str, str, str]:
    try:
        effective_at_ms = int(row.get("effective_at_ms") or 0)
    except (TypeError, ValueError):
        effective_at_ms = 0
    return (
        effective_at_ms,
        str(row.get("fact_kind") or ""),
        str(row.get("source_event_id") or ""),
        str(row.get("allocation_id") or ""),
    )


_OPTION_TRADE_CASH_EXCLUDED_FIELDS = (
    "cash.stock_settlement_cash_gross",
    "cash.stock_settlement_fee_cash",
    "cash.assigned_stock_sale_cash_gross",
    "cash.assigned_stock_sale_fee_cash",
)


def _presentation_reason_category(value: Any) -> str:
    prefix = str(value or "").strip().lower().split(":", 1)[0]
    if prefix.startswith("cash_conversion"):
        return "cash_conversion"
    if prefix.startswith("fx"):
        return "fx"
    if "fee" in prefix:
        return "fee"
    if "valuation" in prefix or "mark" in prefix or "quote" in prefix:
        return "valuation"
    if prefix.startswith("missing_stock_settlement"):
        return "stock_settlement"
    if prefix.startswith("source_conflict"):
        return "source_conflict"
    if prefix.startswith("assigned_stock") or prefix.startswith("assignment"):
        return "assigned_stock"
    if prefix.startswith("capital"):
        return "capital"
    if prefix.startswith("realized") or prefix.startswith("missing_realized"):
        return "realized_pnl"
    if prefix in {"trade_events", "position_lots"}:
        return prefix
    return "other"


def _presentation_reason_summary(values: Any) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for value in values if isinstance(values, (list, tuple)) else ():
        category = _presentation_reason_category(value)
        counts[category] = counts.get(category, 0) + 1
    return [
        {"category": category, "count": counts[category]}
        for category in sorted(counts)
    ]


def _presentation_metric(value: Any) -> dict[str, Any]:
    metric = value if isinstance(value, Mapping) else {}
    by_currency = metric.get("by_currency")
    return {
        "by_currency": {
            str(currency): amount
            for currency, amount in sorted(
                by_currency.items() if isinstance(by_currency, Mapping) else (),
                key=lambda item: str(item[0]),
            )
        },
        "cny": metric.get("cny"),
        "status": str(metric.get("status") or "not_observed"),
        "missing_summary": _presentation_reason_summary(metric.get("missing")),
    }


def _build_option_performance_presentation(data: Mapping[str, Any]) -> dict[str, Any]:
    period = data.get("period") if isinstance(data.get("period"), Mapping) else {}
    scope = data.get("scope") if isinstance(data.get("scope"), Mapping) else {}
    activity = data.get("activity") if isinstance(data.get("activity"), Mapping) else {}
    cash = data.get("cash") if isinstance(data.get("cash"), Mapping) else {}
    pnl = data.get("pnl") if isinstance(data.get("pnl"), Mapping) else {}
    breakdowns = data.get("breakdowns") if isinstance(data.get("breakdowns"), Mapping) else {}
    quality = data.get("quality") if isinstance(data.get("quality"), Mapping) else {}

    account_rows: list[dict[str, Any]] = []
    raw_account_rows = breakdowns.get("accounts") if isinstance(breakdowns, Mapping) else []
    for raw_row in raw_account_rows if isinstance(raw_account_rows, list) else []:
        if not isinstance(raw_row, Mapping):
            continue
        account = str(raw_row.get("account") or "").strip().lower()
        if not account:
            continue
        row_pnl = raw_row.get("pnl") if isinstance(raw_row.get("pnl"), Mapping) else {}
        row_cash = raw_row.get("cash") if isinstance(raw_row.get("cash"), Mapping) else {}
        row_activity = raw_row.get("activity") if isinstance(raw_row.get("activity"), Mapping) else {}
        account_rows.append(
            {
                "account": account,
                "option_realized_gross": _presentation_metric(row_pnl.get("option_realized_gross")),
                "option_trade_cash_gross": _presentation_metric(row_cash.get("option_trade_cash_gross")),
                "premium_collected_gross": _presentation_metric(row_activity.get("premium_collected_gross")),
            }
        )
    account_rows.sort(key=lambda item: item["account"])

    limitations = [
        {
            "kind": "missing_evidence",
            **item,
        }
        for item in _presentation_reason_summary(quality.get("missing"))
    ]
    limitations.extend(
        {
            "kind": "warning",
            **item,
        }
        for item in _presentation_reason_summary(quality.get("warnings"))
    )
    option_realized_net = _presentation_metric(pnl.get("option_realized_net"))
    if option_realized_net["status"] != "observed":
        limitations.append(
            {
                "kind": "metric_status",
                "metric": "option_realized_net",
                "status": option_realized_net["status"],
                "missing_summary": option_realized_net["missing_summary"],
            }
        )

    return {
        "schema_version": "option_performance_presentation.v1",
        "period_scope": {
            "period": {
                key: period.get(key)
                for key in (
                    "kind",
                    "requested_start_date",
                    "requested_end_date",
                    "status",
                    "timezone",
                )
                if period.get(key) is not None
            },
            "scope": {
                key: deepcopy(scope.get(key))
                for key in ("account", "accounts", "broker", "brokers")
                if scope.get(key) not in (None, "", [])
            },
        },
        "reporting_basis": {
            "primary": "gross",
            "net_metric": "pnl.option_realized_net",
            "net_evidence": option_realized_net,
        },
        "primary_metrics": {
            "option_realized_gross": _presentation_metric(pnl.get("option_realized_gross")),
            "option_trade_cash_gross": _presentation_metric(cash.get("option_trade_cash_gross")),
        },
        "account_rows": account_rows,
        "supporting_metrics": {
            "premium_collected_gross": _presentation_metric(activity.get("premium_collected_gross")),
        },
        "assigned_stock_impact": {
            "assigned_stock_realized_gross": _presentation_metric(pnl.get("assigned_stock_realized_gross")),
            "combined_realized_gross": _presentation_metric(pnl.get("realized_gross")),
        },
        "definitions": {
            "option_realized_gross": "期权已实现毛收益，不含指派正股已实现收益。",
            "option_trade_cash_gross": "期权交易产生的有符号现金流，不含指派正股结算和卖出现金。",
            "excluded_from_option_trade_cash_gross": list(_OPTION_TRADE_CASH_EXCLUDED_FIELDS),
        },
        "limitations": limitations,
    }


def _public_option_performance_report(
    report: dict[str, Any],
    *,
    request: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    data = dict(report)
    data["schema_version"] = "option_performance_report.output.v1"
    data["assignment_lifecycle"] = data.pop("assigned_stock", {})
    scope = dict(data.get("scope") or {})
    scope["config_key"] = request["config_key"]
    observed_accounts = {
        str(item).strip().lower()
        for item in scope.get("accounts") or []
        if str(item).strip()
    }
    if request.get("account"):
        scope["accounts"] = sorted({str(request["account"]), *observed_accounts})
    else:
        configured_accounts = set(accounts_from_config(cfg, fallback=()))
        scope["accounts"] = sorted(configured_accounts | observed_accounts)
    data["scope"] = scope

    period = data.get("period") if isinstance(data.get("period"), Mapping) else {}
    valuation_end_at_ms = period.get("valuation_end_at_ms")
    freshness_status = {
        "partial_current": "current",
        "complete_past": "historical",
        "partial_cutoff": "historical",
    }.get(str(period.get("status") or ""))
    if (
        freshness_status
        and isinstance(valuation_end_at_ms, int)
        and not isinstance(valuation_end_at_ms, bool)
    ):
        data["freshness"] = {
            "status": freshness_status,
            "as_of": datetime.fromtimestamp(
                valuation_end_at_ms / 1000,
                tz=timezone.utc,
            ).isoformat(),
        }
    data["coverage"] = {
        "status": "complete",
        "complete_for": "full_query",
        "included_count": 1,
        "total_count": 1,
        "omitted_count": 0,
        "has_more": False,
    }

    quality = dict(data.get("quality") or {})
    diagnostics = [dict(item) for item in quality.get("diagnostics") or [] if isinstance(item, dict)]
    if request["include_rows"]:
        rows = sorted(
            [dict(item) for item in data.get("rows") or [] if isinstance(item, dict)],
            key=_row_order_key,
        )
        original_count = len(rows)
        truncated = original_count > 1000
        data["rows"] = rows[:1000]
        if truncated:
            diagnostics.append(
                {
                    "code": "rows_truncated",
                    "original_count": original_count,
                    "returned_count": 1000,
                }
            )
        quality["rows_truncated"] = truncated
        quality["row_count"] = len(data["rows"])
        quality["row_count_before_limit"] = original_count
    else:
        data.pop("rows", None)
        quality["rows_truncated"] = False
        quality["row_count"] = 0
    quality["diagnostics"] = diagnostics
    data["quality"] = quality
    data["presentation"] = _build_option_performance_presentation(data)
    return data


def option_performance_report_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config,
    resolve_public_data_config_path,
    normalize_broker,
    resolve_option_positions_repo,
    open_performance_evidence_repository,
    build_option_period_performance,
    repo_base,
    mask_path,
    now_ms: int | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.quality.gate import QualityGateBlocked, assert_quality_allows

    request, window = normalize_option_performance_request(
        payload,
        normalize_broker=normalize_broker,
        now_ms=now_ms,
    )
    try:
        assert_quality_allows(
            "option_performance",
            account=str(request.get("account") or "").strip().lower() or None,
            market=str(request.get("config_key") or "").strip().lower() or None,
        )
    except QualityGateBlocked as exc:
        raise AgentToolError(
            code="QUALITY_GATE_BLOCKED",
            message=str(exc),
            details={
                "consumer": exc.consumer,
                "reason_code": exc.reason_code,
                "blocked_by": list(exc.blocked_by),
            },
        ) from exc
    config_path, cfg = load_runtime_config(
        config_key=request["config_key"],
        config_path=request.get("config_path"),
    )
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    data_config_path = resolve_public_data_config_path(request, portfolio_cfg)
    _resolved_data_config, repo = resolve_option_positions_repo(base=repo_base(), data_config=data_config_path)
    evidence_repo = open_performance_evidence_repository(repo)
    configured_accounts = set(accounts_from_config(cfg, fallback=()))
    requested_account = str(request.get("account") or "")
    scope_proven = requested_account in configured_accounts if requested_account else bool(configured_accounts)
    report = build_option_period_performance(
        repo,
        period=window,
        account=request.get("account"),
        broker=request.get("broker"),
        now_ms=now_ms,
        include_rows=request["include_rows"],
        evidence_repo=evidence_repo,
        refresh_quotes=request["refresh_quotes"],
        collection_cfg=cfg,
        collection_base_dir=config_path.parent,
        scope_proven=scope_proven,
    )
    data = _public_option_performance_report(report, request=request, cfg=cfg)
    return data, [], {
        "config_path": mask_path(config_path),
        "data_config": mask_path(data_config_path),
        "period_status": window.status,
    }


def capture_option_performance_evidence(
    payload: dict[str, Any],
    *,
    apply: bool,
    load_runtime_config,
    resolve_public_data_config_path,
    normalize_broker,
    resolve_option_positions_repo,
    open_performance_evidence_repository,
    repo_base,
    mask_path,
    now_ms: int | None = None,
    evidence_collector=collect_current_performance_evidence,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    report_payload = {
        "config_key": payload.get("config_key") or "us",
        "config_path": payload.get("config_path"),
        "data_config": payload.get("data_config"),
        "account": payload.get("account"),
        "broker": payload.get("broker"),
        "period": "mtd",
        "include_rows": False,
        "refresh_quotes": True,
    }
    request, window = normalize_option_performance_request(
        report_payload,
        normalize_broker=normalize_broker,
        now_ms=now_ms,
    )
    if window.status != "partial_current":
        raise AgentToolError("INVALID_ARGUMENT", "evidence capture only supports the current period")
    config_path, cfg = load_runtime_config(
        config_key=request["config_key"],
        config_path=request.get("config_path"),
    )
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    data_config_path = resolve_public_data_config_path(request, portfolio_cfg)
    _resolved_data_config, repo = resolve_option_positions_repo(base=repo_base(), data_config=data_config_path)
    evidence_repo = open_performance_evidence_repository(repo)
    inputs = load_ledger_performance_inputs(repo)
    ending = load_option_valuation_inputs(
        inputs,
        as_of_ms=window.valuation_end_at_ms,
        account=request.get("account"),
        broker=request.get("broker"),
    )
    existing = evidence_repo.read_all()
    ending_assigned_stock = load_assigned_stock_projection(
        inputs,
        as_of_ms=window.valuation_end_at_ms,
        valuation_marks=existing.valuation_marks,
        account=request.get("account"),
        broker=request.get("broker"),
    )
    collection = evidence_collector(
        period_status=window.status,
        refresh_quotes=True,
        option_positions=ending.positions,
        stock_instruments=assigned_stock_instruments(ending_assigned_stock),
        now_ms=int(now_ms if now_ms is not None else window.valuation_end_at_ms),
        cfg=cfg,
        base_dir=config_path.parent,
    )
    migrated_at_ms = int(
        now_ms
        if now_ms is not None
        else datetime.now(timezone.utc).timestamp() * 1000
    )
    imported = evidence_repo.import_envelope(
        collection.envelope,
        apply=bool(apply),
        migrated_at_ms=migrated_at_ms,
    )
    data = imported.to_dict()
    data["schema_version"] = "option_performance_evidence_capture.output.v1"
    data["dry_run"] = not bool(apply)
    data["collection"] = collection.to_dict()
    data["scope"] = {
        "config_key": request["config_key"],
        "account": request.get("account"),
        "broker": request.get("broker"),
    }
    return data, [], {
        "config_path": mask_path(config_path),
        "data_config": mask_path(data_config_path),
    }


def get_portfolio_context_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config,
    resolve_public_data_config_path,
    normalize_broker,
    resolve_output_root,
    load_portfolio_context,
    repo_base,
    mask_path,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    account = str(payload.get("account") or portfolio_cfg.get("account") or "").strip() or None
    broker = normalize_broker(payload.get("broker") or portfolio_cfg.get("broker"))
    data_config = str(resolve_public_data_config_path(payload, portfolio_cfg))
    out_root = resolve_output_root(payload.get("output_dir"))
    state_dir = (out_root / "portfolio_context_state").resolve()
    shared_dir = (out_root / "shared").resolve()
    logs: list[str] = []
    ctx = load_portfolio_context(
        base=repo_base(),
        data_config=data_config,
        market=broker,
        account=account,
        ttl_sec=int(payload.get("ttl_sec") or 0),
        state_dir=state_dir,
        shared_state_dir=shared_dir,
        log=logs.append,
        runtime_config=cfg,
    )
    if not isinstance(ctx, dict):
        raise AgentToolError(code="DEPENDENCY_MISSING", message="portfolio context is unavailable", details={"logs": logs[-5:]})
    warnings = [item for item in logs if item.startswith("[WARN]")]
    return ctx, warnings, {"config_path": mask_path(config_path), "state_dir": mask_path(state_dir)}


def scan_opportunities_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config,
    resolve_data_config_ref,
    resolve_output_root,
    repo_base,
    load_config,
    run_watchlist_pipeline_default,
    scan_summary_rows_fn,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))

    def _log(_msg: str) -> None:
        return None

    out_root = resolve_output_root(payload.get("output_dir"))
    report_dir = (out_root / "reports").resolve()
    state_dir = (out_root / "state").resolve()
    shared_state_dir = (out_root / "shared").resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    shared_state_dir.mkdir(parents=True, exist_ok=True)

    cfg_loaded = load_config(base=repo_base(), config_path=config_path, is_scheduled=False, log=_log, state_dir=state_dir)
    if isinstance(cfg.get("portfolio"), dict):
        cfg_loaded["portfolio"] = deepcopy(cfg["portfolio"])
    if isinstance(cfg_loaded.get("portfolio"), dict):
        data_config_ref = resolve_data_config_ref(payload, cfg_loaded["portfolio"])
        if data_config_ref:
            cfg_loaded["portfolio"]["data_config"] = data_config_ref

    top_n = int(payload.get("top_n") or (cfg_loaded.get("outputs", {}) or {}).get("top_n_alerts", 3) or 3)
    runtime = cfg_loaded.get("runtime", {}) or {}
    raw_symbols = payload.get("symbols")
    symbols_arg = ",".join(str(item) for item in raw_symbols) if isinstance(raw_symbols, list) else (
        str(raw_symbols) if raw_symbols is not None else None
    )
    summary_rows = run_watchlist_pipeline_default(
        py=str((repo_base() / ".venv" / "bin" / "python").resolve()),
        base=repo_base(),
        cfg=cfg_loaded,
        report_dir=report_dir,
        state_dir=state_dir,
        shared_state_dir=shared_state_dir,
        required_data_dir=out_root,
        is_scheduled=False,
        top_n=top_n,
        symbol_timeout_sec=int(payload.get("symbol_timeout_sec") or runtime.get("symbol_timeout_sec", 120) or 120),
        portfolio_timeout_sec=int(payload.get("portfolio_timeout_sec") or runtime.get("portfolio_timeout_sec", 60) or 60),
        want_scan=True,
        no_context=bool(payload.get("no_context", False)),
        symbols_arg=symbols_arg,
        log=_log,
        want_fn=lambda _step: True,
    )
    summary = scan_summary_rows_fn(summary_rows)
    return {
        "summary_rows": summary_rows,
        "symbol_count": len({str(r.get("symbol") or "").strip() for r in summary_rows if str(r.get("symbol") or "").strip()}),
        "row_count": len(summary_rows),
        "summary": summary,
        "top_candidates": summary["top_candidates"],
    }, [], {"config_path": str(config_path), "report_dir": str(report_dir)}


def prepare_close_advice_inputs_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config,
    resolve_public_data_config_path,
    normalize_broker,
    resolve_output_root,
    load_option_positions_context,
    symbol_fetch_config_map_fn,
    extract_context_symbols_fn,
    resolve_symbol_fetch_source,
    fetch_symbol_opend,
    save_required_data_opend,
    repo_base,
    mask_path,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    data_config = str(resolve_public_data_config_path(payload, portfolio_cfg))
    account = str(payload.get("account") or portfolio_cfg.get("account") or "").strip() or None
    broker = normalize_broker(payload.get("broker") or portfolio_cfg.get("broker"))
    out_root = resolve_output_root(payload.get("output_dir"))
    request_root = _close_advice_scope_root(out_root, payload)
    state_dir = (request_root / "state").resolve()
    shared_dir = (request_root / "shared").resolve()
    required_data_root = (request_root / "required_data").resolve()
    logs: list[str] = []
    context_path = state_dir / "option_positions_context.json"
    if not Path(data_config).exists():
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="option positions data config not found",
            hint="Check portfolio.data_config / SQLite position-lot setup before preparing close_advice inputs.",
            details={"data_config": mask_path(Path(data_config))},
        )
    state_dir.mkdir(parents=True, exist_ok=True)
    shared_dir.mkdir(parents=True, exist_ok=True)
    required_data_root.mkdir(parents=True, exist_ok=True)
    try:
        ctx, _refreshed = load_option_positions_context(
            base=repo_base(),
            data_config=data_config,
            market=broker,
            account=account,
            ttl_sec=int(payload.get("ttl_sec") or 0),
            state_dir=state_dir,
            shared_state_dir=shared_dir,
            log=logs.append,
        )
    except SystemExit as exc:
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="option positions context refresh failed",
            hint="Check portfolio.data_config / SQLite position-lot setup before preparing close_advice inputs.",
            details={"exit_code": str(exc)},
        ) from exc
    try:
        ctx = _validate_close_advice_context(ctx)
    except AgentToolError as exc:
        if logs:
            exc.details.setdefault("logs", logs[-5:])
        raise

    position_requirements = _extract_position_fetch_requirements(ctx)
    if not position_requirements:
        return {
            "account": account,
            "broker": broker,
            "context_rows": len(ctx.get("open_positions_min") or []),
            "symbols": [],
            "symbol_count": 0,
            "coverage_summary": _build_coverage_summary([]),
        }, [item for item in logs if item.startswith("[WARN]")], {
            "config_path": mask_path(config_path),
            "context_path": mask_path(context_path),
            "required_data_root": mask_path(required_data_root),
        }

    symbol_map = symbol_fetch_config_map_fn(cfg)
    fetched: list[dict[str, Any]] = []
    warnings = [item for item in logs if item.startswith("[WARN]")]
    force_required_data_refresh = bool(payload.get("force_required_data_refresh", False))
    quote_max_age_sec = DEFAULT_QUOTE_MAX_AGE_SEC
    for spec in position_requirements:
        symbol = str(spec.get("symbol") or "").strip()
        raw_symbol_cfg = symbol_map.get(symbol)
        symbol_cfg = raw_symbol_cfg if isinstance(raw_symbol_cfg, dict) else {}
        raw_fetch_cfg = symbol_cfg.get("fetch")
        fetch_cfg = raw_fetch_cfg if isinstance(raw_fetch_cfg, dict) else {}
        src, _decision = resolve_symbol_fetch_source(fetch_cfg)
        limit_expirations = int(fetch_cfg.get("limit_expirations") or 8)
        csv_path = (required_data_root / "parsed" / f"{symbol}_required_data.csv").resolve()
        requested_expirations = list(spec.get("requested_expirations") or [])
        requested_contracts = set(spec.get("requested_contracts") or set())
        if force_required_data_refresh:
            fetched_contracts: set[tuple[str, str, str, str]] = set()
            fetched_expirations: set[str] = set()
        else:
            fetched_contracts, fetched_expirations = _read_required_data_coverage(csv_path)
            freshness = validate_quote_cache_metadata(
                csv_path=csv_path,
                symbol=symbol,
                max_age_sec=quote_max_age_sec,
            )
            if not freshness["ok"]:
                fetched_contracts = set()
                fetched_expirations = set()

        cache_covers_all = (
            not force_required_data_refresh
            and bool(requested_contracts)
            and all(item in fetched_contracts for item in requested_contracts)
        )
        if not cache_covers_all:
            result = fetch_symbol_opend(
                symbol,
                limit_expirations=limit_expirations,
                host=str(fetch_cfg.get("host") or "127.0.0.1"),
                port=int(fetch_cfg.get("port") or 11111),
                base_dir=repo_base(),
                option_types=",".join(str(item) for item in (spec.get("option_types") or ["put", "call"])),
                min_strike=spec.get("min_strike"),
                max_strike=spec.get("max_strike"),
                explicit_expirations=requested_expirations,
                chain_cache=True,
                chain_cache_force_refresh=force_required_data_refresh,
                **opend_fetch_kwargs(cfg),
            )
            _raw_path, csv_path = save_required_data_opend(repo_base(), symbol, result, output_root=required_data_root)
            meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            if meta.get("error"):
                warnings.append(f"{symbol}: {meta['error']}")
            else:
                publish_quote_cache_metadata(
                    csv_path=csv_path,
                    symbol=symbol,
                    source=str(src or "opend"),
                    source_run_id=str(
                        payload.get("_close_advice_scope_id")
                        or f"prepare-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
                    ),
                    observed_at=datetime.now(timezone.utc),
                )
            fetched_contracts, fetched_expirations = _read_required_data_coverage(csv_path)
            row_count = len(result.get("rows") or [])
            expiration_count = int(result.get("expiration_count") or 0)
        else:
            row_count = _count_required_data_rows(csv_path)
            expiration_count = len(fetched_expirations)
        missing_expirations = [exp for exp in requested_expirations if exp not in fetched_expirations]
        missing_contract_keys = [item for item in sorted(requested_contracts) if item not in fetched_contracts]
        missing_contracts = sorted(
            f"{item[2]} {item[3]}{'P' if item[1] == 'put' else 'C'}"
            for item in missing_contract_keys
        )
        near_misses = _find_contract_expiration_near_misses(requested_contracts, fetched_contracts)
        item = {
            "symbol": symbol,
            "source": src,
            "rows": row_count,
            "expiration_count": expiration_count,
            "csv": mask_path(csv_path),
            "position_count": int(spec.get("position_count") or 0),
            "requested_expirations": requested_expirations,
            "fetched_expirations": sorted(fetched_expirations),
            "missing_expirations": missing_expirations,
            "position_coverage_ok": not missing_contracts,
            "missing_contract_count": len(missing_contracts),
            "missing_contract_samples": missing_contracts[:3],
            "missing_contracts": [
                {
                    "symbol": key[0],
                    "option_type": key[1],
                    "expiration": key[2],
                    "strike": _as_float_or_none(key[3]),
                    "quote_key": "|".join(key),
                }
                for key in missing_contract_keys
            ],
            "expiration_near_misses": near_misses,
        }
        if missing_expirations:
            warnings.append(f"{symbol}: missing required expirations {', '.join(missing_expirations)}")
        elif missing_contracts:
            warnings.append(f"{symbol}: missing required contracts after fetch ({', '.join(missing_contracts[:3])})")
        for near_miss in near_misses:
            warnings.append(
                f"{symbol}: expiration near miss {near_miss['requested_expiration']} -> {near_miss['matched_expiration']} "
                f"for {near_miss['option_type']} {near_miss['strike']}"
            )
        fetched.append(item)

    return {
        "account": account,
        "broker": broker,
        "context_rows": len(ctx.get("open_positions_min") or []),
        "symbols": fetched,
        "symbol_count": len(fetched),
        "coverage_summary": _build_coverage_summary(fetched),
    }, warnings, {
        "config_path": mask_path(config_path),
        "context_path": mask_path(context_path),
        "required_data_root": mask_path(required_data_root),
        "force_required_data_refresh": force_required_data_refresh,
        "quote_max_age_sec": quote_max_age_sec,
    }


def close_advice_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config,
    resolve_output_root,
    resolve_local_path,
    run_close_advice,
    close_advice_rows_summary_fn,
    repo_base,
    mask_path,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    out_root = resolve_output_root(payload.get("output_dir"))
    request_root = _close_advice_scope_root(out_root, payload)
    context_path = resolve_local_path(payload.get("context_path"), default=(request_root / "state" / "option_positions_context.json"))
    required_data_root = resolve_local_path(payload.get("required_data_root"), default=(request_root / "required_data"))
    report_dir = (request_root / "reports").resolve()
    if not context_path.exists():
        raise AgentToolError(code="DEPENDENCY_MISSING", message="close_advice requires a local option_positions_context.json", hint="Run the scan/context pipeline first, or pass context_path explicitly.", details={"context_path": mask_path(context_path)})
    if not required_data_root.exists():
        raise AgentToolError(code="DEPENDENCY_MISSING", message="close_advice requires a local required_data directory", hint="Run the scan pipeline first, or pass required_data_root explicitly.", details={"required_data_root": mask_path(required_data_root)})
    try:
        market = str(payload.get("config_key") or "").strip().upper()
        result = run_close_advice(
            config=cfg,
            context_path=context_path,
            required_data_root=required_data_root,
            output_dir=report_dir,
            base_dir=repo_base(),
            markets_to_run=[market] if market in {"US", "HK"} else None,
        )
    except ValueError as exc:
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message=str(exc),
            details={"context_path": mask_path(context_path)},
        ) from exc
    report_manifest = (
        result.get("report_manifest")
        if isinstance(result.get("report_manifest"), dict)
        else {}
    )
    if not bool(result.get("enabled")) or str(
        report_manifest.get("status") or ""
    ).strip().lower() != "success":
        advice_summary = {
            "row_count": 0,
            "recommendation_counts": {},
            "account_counts": {},
            "top_rows": [],
            "notification_preview": "",
        }
    else:
        snapshot = read_close_advice_report_snapshot(
            csv_path=report_dir / "close_advice.csv",
            desired_market=market if market in {"US", "HK"} else None,
            account=normalize_account(payload.get("account")) or None,
            expected_quote_mode=str(result.get("quote_mode") or "").strip()
            or None,
        )
        validation = snapshot["validation"]
        if not validation.get("ok"):
            raise AgentToolError(
                code="DEPENDENCY_INVALID",
                message="平仓建议报告完整性校验失败。",
                hint="请重新生成严格版平仓建议报告。",
                details={
                    "csv_path": mask_path(report_dir / "close_advice.csv"),
                    "reason": str(validation.get("reason") or "unknown"),
                },
            )
        advice_summary = close_advice_rows_summary_fn(
            report_dir / "close_advice.csv",
            report_dir / "close_advice.txt",
            csv_bytes=snapshot["csv_bytes"],
            text_bytes=snapshot["text_bytes"],
        )
    return {
        **result,
        "summary": {
            "row_count": advice_summary["row_count"],
            "recommendation_counts": advice_summary["recommendation_counts"],
            "account_counts": advice_summary["account_counts"],
        },
        "top_rows": advice_summary["top_rows"],
        "notification_preview": advice_summary["notification_preview"],
    }, [], {
        "config_path": mask_path(config_path),
        "context_path": mask_path(context_path),
        "required_data_root": mask_path(required_data_root),
        "output_dir": mask_path(report_dir),
    }


def get_close_advice_tool(
    payload: dict[str, Any],
    *,
    prepare_close_advice_inputs_tool_fn,
    close_advice_tool_fn,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    scope_id = (
        str(payload.get("request_id") or "").strip()
        or f"close-advice-{uuid.uuid4().hex}"
    )
    scoped_payload = dict(payload)
    scoped_payload["_close_advice_scope_id"] = scope_id
    prepared_data, prepare_warnings, prepare_meta = prepare_close_advice_inputs_tool_fn(scoped_payload)
    advice_data, advice_warnings, advice_meta = close_advice_tool_fn(scoped_payload)
    combined_summary = {
        "prepared_symbol_count": int(prepared_data.get("symbol_count") or 0),
        "prepared_context_rows": int(prepared_data.get("context_rows") or 0),
        "advice_row_count": int(advice_data.get("rows") or advice_data.get("summary", {}).get("row_count") or 0),
        "notify_row_count": int(advice_data.get("notify_rows") or 0),
        "recommendation_counts": dict(advice_data.get("summary", {}).get("recommendation_counts")) if isinstance(advice_data.get("summary"), dict) and isinstance(advice_data.get("summary", {}).get("recommendation_counts"), dict) else {},
        "coverage_summary": dict(prepared_data.get("coverage_summary")) if isinstance(prepared_data.get("coverage_summary"), dict) else {},
    }
    return {
        "prepared": prepared_data,
        "close_advice": advice_data,
        "summary": combined_summary,
        "top_rows": list(advice_data.get("top_rows") or []),
        "notification_preview": advice_data.get("notification_preview"),
    }, [*prepare_warnings, *advice_warnings], {
        **prepare_meta,
        **advice_meta,
        "request_id": scope_id,
    }
