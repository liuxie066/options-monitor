"""Read-only assignment and buyback evidence for the Futu pending-cash policy."""
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
            and row["fields"].get("option_type") in {"put", "call"}
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
    currencies = [currency for currency in ("USD", "HKD") if currency in deficits]
    if not currencies:
        return balance_rows, {**result, "reason": "assignment_currency_ambiguous"}
    terms = None
    items = []
    latest_rows = balance_rows
    for currency in currencies:
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
            if terms is None:
                terms = _account_terms(cfg, account)
            check_fetch_control(deadline, cancelled)
            # Each currency is reconciled against its own complete fresh funds response.
            fresh = query_rows(gateway, "get_account_balance", {account_id},
                               trd_env=environment, currency=currency, refresh_cache=True,
                               **({"deadline": deadline, "cancelled": cancelled}
                                  if deadline is not None or cancelled is not None else {}))
            if len(fresh) != 1 or str(fresh[0].get("currency")) != currency:
                raise ValueError("assignment funds currency mismatch")
            if (str(fresh[0].get("acc_id", account_id)) != str(account_id)
                    or str(fresh[0].get("trd_env", environment)) != environment):
                raise ValueError("assignment funds identity mismatch")
            latest_rows = fresh
            item = match_assignment_pending(balance=fresh[0], orders=receipt["rows"], terms=terms,
                                            account_id=account_id, environment=environment, today=today)
            if item["reason"] == "close_fees_unavailable":
                if len(item["order_ids"]) > 400:
                    raise ValueError("too many close orders for fee reconciliation")
                check_fetch_control(deadline, cancelled)
                fees = query_rows(gateway, "get_order_fees", {account_id}, trd_env=environment,
                                  order_id_list=item["order_ids"])
                check_fetch_control(deadline, cancelled)
                item = match_assignment_pending(
                    balance=fresh[0], orders=receipt["rows"], terms=terms, order_fees=fees,
                    account_id=account_id, environment=environment, today=today,
                )
            item["funds_observed_at"] = datetime.now(timezone.utc).isoformat()
        except (TimeoutError, CancelledError):
            raise
        except Exception:
            check_fetch_control(deadline, cancelled)
            item = {"status": "unmatched", "reason": "assignment_evidence_unavailable",
                    "currency": currency, "pending_amount": None, "adjustment": 0.0, "order_ids": []}
        items.append(item)
    # All adjustments are applied to the last whole response. A prior currency
    # may have settled between queries, so its original deficit must still exist.
    for item in items:
        field = {"USD": "us_cash", "HKD": "hk_cash"}[item["currency"]]
        if (item["status"] == "matched"
                and number(latest_rows[0].get(field)) != -number(item["adjustment"])):
            item.update(status="unmatched", reason="assignment_amount_mismatch", adjustment=0.0)
    return latest_rows, _summarize_items(items)


def _summarize_items(items):
    if len(items) == 1:
        return {**items[0], "items": items}
    statuses = {item["status"] for item in items}
    status = "unmatched" if "unmatched" in statuses else "none" if statuses == {"none"} else "matched"
    # Different currencies have no scalar total without conversion evidence.
    evidence = {"status": status, "reason": "multiple_currency_reconciliation",
                "currency": "", "pending_amount": None, "adjustment": None, "items": items}
    observed_at = [item.get("funds_observed_at") for item in items if item.get("funds_observed_at")]
    if observed_at:
        evidence["funds_observed_at"] = max(observed_at)
    return evidence


def apply_assignment_pending(context, evidence):
    """Replace this component, so replay cannot add money twice."""
    for currency, components in context["cash_components_by_currency"].items():
        previous = components.pop("assignment_pending", 0.0)
        context["cash_by_currency"][currency] -= previous
        context["cash_capacity_by_currency"][currency]["amount"] = context["cash_by_currency"][currency]
    evidence = dict(evidence)
    items = evidence.get("items") or [evidence]
    if not context.get("cash_balance_reliable"):
        items = [{**item, "status": "unmatched", "reason": "cash_balance_unreliable", "adjustment": 0.0}
                 if item.get("status") == "matched" else item for item in items]
        evidence = _summarize_items(items)
    context["assignment_pending"] = evidence
    for item in items:
        if item.get("status") != "matched":
            continue
        currency = item["currency"]
        amount = item["adjustment"]
        context["cash_components_by_currency"][currency]["assignment_pending"] = amount
        context["cash_by_currency"][currency] += amount
        context["cash_capacity_by_currency"][currency]["amount"] = context["cash_by_currency"][currency]
    if any(item.get("status") == "matched" and item.get("adjustment", 0) > 0 for item in items):
        context["cash_source"] = "futu_cash_like_assets_with_assignment_pending"
