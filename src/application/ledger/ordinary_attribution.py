"""Qualify ordinary attribution evidence; provider I/O stays with application observers."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.symbol_identity import symbol_market
from domain.domain.trade_execution import (
    canonical_trade_execution_content, execution_economic_content, execution_identity_from_input,
    execution_instant_milliseconds, ledger_execution_event_set_is_complete,
    require_same_execution_content, structured_deal_keys_from_ledger_event,
)


def ordinary_attribution_window(fact: Mapping[str, Any]) -> tuple[int, int]:
    market = str(symbol_market(fact["contract_key"]["underlying_symbol"]) or "").upper()
    tz = ZoneInfo({"HK": "Asia/Hong_Kong", "US": "America/New_York"}[market])
    occurred = datetime.fromtimestamp(fact["event_time_ms"] / 1000, tz)
    start = datetime.combine(occurred.date(), datetime.min.time(), tzinfo=tz)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)



def ordinary_attribution_is_unopposed(*, fact: Mapping[str, Any], facts: Sequence[Mapping[str, Any]],
                                      proposals: Sequence[Mapping[str, Any]]) -> bool:
    if any(not item["candidate_id"].startswith("combo-exposure:") for item in proposals):
        return False
    if fact["contract_key"]["option_type"] != "put":
        return True
    start, _ = ordinary_attribution_window(fact)
    # Inspect all openings: asymmetric, closed and already claimed counterparts still count.
    return not any(
        other["position_side"] == "long" and other["contract_key"]["option_type"] == "call"
        and other["broker_account_ref"] == fact["broker_account_ref"]
        and other["contract_key"]["underlying_symbol"] == fact["contract_key"]["underlying_symbol"]
        and other["contract_key"]["expiration_ymd"] == fact["contract_key"]["expiration_ymd"]
        and ordinary_attribution_window(other)[0] == start for other in facts
    )


def ordinary_attribution_basis(
    *, fact: Mapping[str, Any], facts: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]],
    proposals: Sequence[Mapping[str, Any]], exposures: Sequence[Mapping[str, Any]],
    exposure_ids: Sequence[str], observation: Mapping[str, Any], now_ms: int,
) -> dict[str, Any] | None:
    """Absence needs complete source facts, never an empty candidate list alone."""
    if (fact["position_side"] != "short" or fact["reason_codes"] or fact["status"] == "linked"
            or fact["origin"] == "manual" or fact["contracts_open"] <= 0
            or not ordinary_attribution_is_unopposed(fact=fact, facts=facts, proposals=proposals)):
        return None
    try:
        start, cutoff = ordinary_attribution_window(fact)
        matched = [item for item in exposures if item.get("candidate_exposure_id") in exposure_ids]
        if len({item["candidate_exposure_id"] for item in matched}) != len(set(exposure_ids)):
            return None
        cutoff = max([cutoff, *[int(item["valid_until_ms"]) for item in matched]])
        if now_ms < cutoff:
            return None
        evidence = observation.get("ordinary_evidence") or {}
        ref = fact["broker_account_ref"]
        if (evidence.get("scope") != {**ref, "account": fact["account"]}
                or ref.get("environment") != "REAL" or ref.get("broker_id") != "futu"
                or not 0 <= now_ms - evidence["observed_at_ms"] <= 60_000
                or evidence["window_start_ms"] > start
                or not cutoff <= evidence["window_end_ms"] <= evidence["observed_at_ms"]):
            return None
        snapshot = (observation.get("portfolio") or {}).get("position_snapshot_input") or {}
        snapshot_ms = execution_instant_milliseconds(snapshot.get("observed_at_utc"))
        if snapshot_ms is None or snapshot_ms < evidence["observed_at_ms"]:
            return None
        history = evidence["history"]
        accounts = history["account_results"]
        if (history.get("dataset") != "executions" or history.get("trd_env") != ref["environment"]
                or len(accounts) != 1 or accounts[0].get("futu_account_id") != ref["external_account_id"]
                or any(accounts[0].get(key) is not True for key in ("coverage_complete", "pagination_complete"))
                or accounts[0].get("coverage_status") != "complete"
                or accounts[0].get("trd_env") != ref["environment"]
                or execution_instant_milliseconds(accounts[0].get("covered_start_utc")) != evidence["window_start_ms"]
                or execution_instant_milliseconds(accounts[0].get("covered_end_utc")) != evidence["window_end_ms"]
                or accounts[0].get("truncated") is True
                or accounts[0].get("accepted_row_count") != len(evidence["executions"])
                or accounts[0].get("row_count") != len(evidence["executions"])
                or accounts[0].get("error") or accounts[0].get("rejected_rows")
                or execution_instant_milliseconds(history["window_start_utc"]) != evidence["window_start_ms"]
                or execution_instant_milliseconds(history["window_end_utc"]) != evidence["window_end_ms"]):
            return None
        executions = evidence["executions"]
        seen: dict[str, Mapping[str, Any]] = {}
        order_quantities: dict[str, Decimal] = defaultdict(Decimal)
        option_keys = set()
        by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            if event.get("event_type") not in {"open", "close"}:
                continue
            for key in structured_deal_keys_from_ledger_event(dict(event), include_legacy_execution_identity=True):
                if key.startswith("execution:"):
                    by_key[key].append(dict(event))
        for execution in executions:
            key = execution_identity_from_input(execution)
            actual_ref = execution.get("broker_account_ref") or {}
            occurred = execution_instant_milliseconds(execution.get("occurred_at_utc"))
            order_id = execution.get("external_order_id")
            if (not key or execution.get("errors") or not order_id
                    or any(actual_ref.get(field) != value for field, value in ref.items())
                    or actual_ref.get("account_label") not in (None, "", fact["account"])
                    or occurred is None or not evidence["window_start_ms"] <= occurred <= evidence["window_end_ms"]
                    or execution["instrument_ref"].get("asset_type") not in {"option", "stock"}):
                return None
            if key in seen:
                require_same_execution_content(execution_economic_content(seen[key]), execution_economic_content(execution))
                continue
            seen[key] = execution
            quantity = Decimal(execution["quantity"])
            if not quantity.is_finite() or quantity <= 0:
                return None
            order_quantities[order_id] += quantity
            if execution["instrument_ref"]["asset_type"] != "option":
                continue
            option_keys.add(key)
            recorded = by_key.get(key, [])
            if not ledger_execution_event_set_is_complete(recorded):
                return None
            for event in recorded:
                if (event.get("contract_key") or {}).get("account") != fact["account"]:
                    return None
                require_same_execution_content(canonical_trade_execution_content(event.get("raw_payload") or {}),
                                               execution_economic_content(execution))
        if fact["execution_key"] not in option_keys:
            return None
        # Do not permit a corrected/omitted broker row to turn existing trades into absence.
        for key, group in by_key.items():
            event = group[0]
            stored = (event.get("raw_payload") or {}).get("execution_input") or {}
            stored_ref = stored.get("broker_account_ref") or {}
            if not stored_ref:
                raw = event.get("raw_payload") or {}
                stored_ref = {"external_account_id": str(raw.get("futu_account_id") or ""),
                              "environment": raw.get("trd_env"), "broker_id": "futu"}
            if (stored_ref == ref or all(stored_ref.get(field) == value for field, value in ref.items())):
                if evidence["window_start_ms"] <= event["event_time_ms"] <= evidence["window_end_ms"] and key not in option_keys:
                    return None
        orders = {}
        for receipt in (evidence["current_orders"], evidence["history_orders"]):
            if (receipt.get("scope") != evidence["scope"] or receipt.get("coverage_complete") is not True
                    or receipt.get("pagination_complete") is not True or receipt.get("truncated") is True
                    or receipt.get("error") or receipt.get("retcode") not in (0, "0")):
                return None
            for row in receipt["rows"]:
                order_id = row["order_id"]
                if (not order_id or row["status"] not in {"terminal_with_fill", "terminal_no_fill"}
                        or row["created_at_ms"] is None or row["created_at_ms"] > evidence["observed_at_ms"]):
                    return None
                if order_id in orders and orders[order_id] != row:
                    return None
                orders[order_id] = row
        if not set(order_quantities) <= orders.keys():
            return None
        for order_id, row in orders.items():
            quantity = Decimal(row["dealt_qty"])
            if not quantity.is_finite() or quantity < 0:
                return None
            if row["created_at_ms"] >= evidence["window_start_ms"] or order_id in order_quantities:
                if order_quantities[order_id] != quantity:
                    return None
        return {"scope": evidence["scope"], "window_start_ms": evidence["window_start_ms"],
                "window_end_ms": evidence["window_end_ms"], "observed_at_ms": evidence["observed_at_ms"],
                "eligible_after_ms": cutoff, "history_request_id": history["request_id"],
                "execution_sha256": canonical_sha256(executions), "orders_sha256": canonical_sha256(orders),
                "exposure_ids": sorted(exposure_ids)}
    except (KeyError, TypeError, ValueError, ArithmeticError, OverflowError, AttributeError):
        return None
