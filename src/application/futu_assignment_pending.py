"""Read-only assignment evidence for the default Futu pending-cash policy."""
from concurrent.futures import CancelledError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from domain.domain.assignment_pending import match_assignment_pending, number
from src.infrastructure.futu_gateway import check_fetch_control


def _account_terms(cfg, account):
    from src.application.ledger.query_api import (
        open_position_query_ledger_from_data_config, list_canonical_position_lot_snapshots,
    )

    _, repo = open_position_query_ledger_from_data_config(
        base=Path(__file__).resolve().parents[2], data_config=None, cfg=dict(cfg),
    )
    return [row["fields"] for row in list_canonical_position_lot_snapshots(repo)
            if row.get("fields", {}).get("account") == account
            and row["fields"].get("broker") == "富途"
            and row["fields"].get("option_type") == "put"
            and row["fields"].get("side") == "short"]


def observe_assignment_pending(gateway, balance_rows, *, query_rows, cfg, account,
                               account_id, environment, deadline=None, cancelled=None):
    """Return the whole latest funds response and its inferred adjustment together."""
    result = {"status": "unmatched", "reason": "pending_amount_unavailable",
              "adjustment": 0.0, "pending_amount": None, "currency": ""}
    if len(balance_rows) != 1:
        return balance_rows, result
    row = balance_rows[0]
    result.update(currency=str(row.get("currency") or ""))
    pending = number(row.get("pending_asset"))
    if pending is None or pending < 0:
        return balance_rows, result
    result["pending_amount"] = float(pending)
    deficits = [ccy for ccy, field in (("USD", "us_cash"), ("HKD", "hk_cash"), ("CNY", "cn_cash"))
                if number(row.get(field)) is not None and number(row[field]) < 0]
    if pending == 0:
        return balance_rows, {**result, "status": "unmatched" if deficits else "none",
                              "reason": "negative_cash_without_pending" if deficits else "no_pending"}
    if len(deficits) != 1 or deficits[0] not in {"USD", "HKD"}:
        return balance_rows, {**result, "reason": "assignment_currency_ambiguous"}
    currency = deficits[0]
    market = "US" if currency == "USD" else "HK"
    today = datetime.now(ZoneInfo("America/New_York" if market == "US" else "Asia/Hong_Kong")).date()
    try:
        check_fetch_control(deadline, cancelled)
        receipt = gateway.get_history_orders(
            acc_id=int(account_id), trd_env=environment, order_market=market,
            start=(today - timedelta(days=7)).isoformat(), end=today.isoformat(),
        )
        if (not isinstance(receipt, dict) or receipt.get("retcode") != 0
                or receipt.get("coverage_complete") is not True
                or receipt.get("pagination_complete") is not True
                or not isinstance(receipt.get("rows"), list)
                or any(not isinstance(r, dict) for r in receipt["rows"])):
            raise ValueError("assignment history incomplete")
        terms = _account_terms(cfg, account)
        check_fetch_control(deadline, cancelled)
        # Use the entire second response, never new pending plus old cash/fund.
        fresh = query_rows(gateway, "get_account_balance", {account_id},
                           trd_env=environment, currency=currency, refresh_cache=True,
                           **({"deadline": deadline, "cancelled": cancelled}
                              if deadline is not None or cancelled is not None else {}))
        if len(fresh) != 1 or str(fresh[0].get("currency")) != currency:
            raise ValueError("assignment funds currency mismatch")
        if (str(fresh[0].get("acc_id", account_id)) != str(account_id)
                or str(fresh[0].get("trd_env", environment)) != environment):
            raise ValueError("assignment funds identity mismatch")
        result = match_assignment_pending(balance=fresh[0], orders=receipt["rows"], terms=terms,
                                          account_id=account_id, environment=environment, today=today)
        result["funds_observed_at"] = datetime.now(timezone.utc).isoformat()
        return fresh, result
    except (TimeoutError, CancelledError):
        raise
    except Exception:
        check_fetch_control(deadline, cancelled)
        return balance_rows, {**result, "reason": "assignment_evidence_unavailable"}


def apply_assignment_pending(context, evidence):
    """Replace this component, so replay cannot add money twice."""
    for currency, components in context["cash_components_by_currency"].items():
        previous = components.pop("assignment_pending", 0.0)
        context["cash_by_currency"][currency] -= previous
        context["cash_capacity_by_currency"][currency]["amount"] = context["cash_by_currency"][currency]
    evidence = dict(evidence)
    if evidence["status"] == "matched" and not context.get("cash_balance_reliable"):
        evidence.update(status="unmatched", reason="cash_balance_unreliable", adjustment=0.0)
    context["assignment_pending"] = evidence
    if evidence["status"] == "matched":
        currency = evidence["currency"]
        amount = evidence["adjustment"]
        context["cash_components_by_currency"][currency]["assignment_pending"] = amount
        context["cash_by_currency"][currency] += amount
        context["cash_capacity_by_currency"][currency]["amount"] = context["cash_by_currency"][currency]
        context["cash_source"] = "futu_cash_like_assets_with_assignment_pending"
