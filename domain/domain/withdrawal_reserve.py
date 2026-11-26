"""Cash withdrawal reservations; broker balances remain immutable evidence."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json


def money(value):
    if isinstance(value, bool):
        raise ValueError("invalid money")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid money") from exc
    if not result.is_finite() or result != result.quantize(Decimal("0.01")):
        raise ValueError("invalid money precision")
    return result


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode()).hexdigest()


def discovery_start(previous, today):
    return min(date.fromisoformat((previous or {}).get("covered_since", today.isoformat())),
               today - timedelta(days=6))


def normalize_source(row):
    fields = {key: str(row.get(key) or "").strip()
              for key in ("record_id", "account", "broker", "currency", "flow_date")}
    if not all(fields.values()) or fields["currency"] not in {"CNY", "USD", "HKD"}:
        raise ValueError("withdrawal source fields missing")
    date.fromisoformat(fields["flow_date"])
    amount = money(row.get("amount"))
    if not amount:
        raise ValueError("zero cash flow")
    if row.get("flow_type") not in (None, "", "WITHDRAW" if amount < 0 else "DEPOSIT"):
        raise ValueError("cash flow direction conflict")
    return {**fields, "amount": str(amount.quantize(Decimal("0.01")))}


def eligible_flow(row, source, today, *, manual=False):
    """Explicit transaction outflows never constitute a bank withdrawal."""
    if row.get("cashflow_direction") != "OUT" or row.get("currency") != source["currency"]:
        return False
    kind = str(row.get("cashflow_type") or "").strip()
    remark = str(row.get("cashflow_remark") or "").lower()
    # Unknown nonempty categories are not guessed to mean bank withdrawals.
    allowed = {"其他", "出金", "银行转出", "银行提款", "提款", "Withdrawal", "Bank Withdrawal"}
    if manual:
        allowed |= {"手续费", "银行手续费", "出金手续费", "Withdrawal Fee", "Bank Fee"}
    if kind not in allowed:
        return False
    if any(token in remark for token in ("fund", "opt ", "assignment", "subscription",
                                          "交易", "申购", "指派", "兑换", "exchange")):
        return False
    try:
        day = date.fromisoformat(str(row.get("clearing_date") or ""))
        return (date.fromisoformat(source["flow_date"]) <= day <= today
                and money(row.get("cashflow_amount")) < 0 and bool(row.get("cashflow_id")))
    except ValueError:
        return False


def reconcile(previous, sources, flows, *, today, account, broker="富途",
              binding, flow_complete, funds_usable, foreign_record_ids=()):
    """Consume a complete PM account snapshot and account-scoped broker flows.

    Caller owns completeness, authority and persistence. A returned state is not
    usable until its atomic commit succeeds.
    """
    state = deepcopy(previous or {"records": {}, "claimed": {}, "operations": {}})
    records = state["records"]
    for record in records.values():
        record["source"] = normalize_source(record["source"])
        record["version"] = fingerprint(record["source"])
        if "proposed" in record:
            record["proposed"] = normalize_source(record["proposed"])
    if state.get("binding") not in (None, binding):
        raise ValueError("withdrawal physical account changed")
    state["binding"] = binding
    start = discovery_start(state, today)
    seen = set()
    for raw in sources:
        source = normalize_source(raw)
        key = source["record_id"]
        if key in seen:
            raise ValueError("duplicate PM record")
        seen.add(key)
        existing = records.get(key)
        relevant = source["account"] == account and source["broker"] == broker
        if not relevant and existing is None:
            continue
        if key in foreign_record_ids and relevant and money(source["amount"]) < 0:
            if existing is None:
                existing = records[key] = {"source": source, "version": fingerprint(source),
                                           "status": "conflict", "flows": []}
            existing.update(status="conflict", foreign_owner=True)
        elif existing:
            existing.pop("foreign_owner", None)
        version = fingerprint(source)
        if existing:
            existing.pop("source_missing", None)
            if existing["status"] == "conflict":
                existing["proposed"] = source
            elif existing["version"] != version:
                if (existing["status"] != "pending" or existing.get("flows") or not relevant
                        or any(existing["source"][k] != source[k]
                               for k in ("account", "broker", "currency")) or money(source["amount"]) >= 0):
                    existing.update(status="conflict", proposed=source)
                else:
                    existing.update(source=source, version=version)
        elif relevant and money(source["amount"]) < 0 and start <= date.fromisoformat(source["flow_date"]) <= today:
            records[key] = {"source": source, "version": version, "status": "pending", "flows": []}
    # Disappearance never releases a reservation or erases a settled decision.
    for key in records.keys() - seen:
        if records[key]["status"] != "cancelled":
            records[key].update(status="conflict", source_missing=True)
    unique_flows = {}
    for row in flows:
        fid = str(row.get("cashflow_id") or "")
        if not fid or (fid in unique_flows and unique_flows[fid] != row):
            raise ValueError("invalid or conflicting cash flow identity")
        unique_flows[fid] = row
    candidates = {}
    if flow_complete:
        for key, record in records.items():
            if record["status"] not in {"pending", "matched"}:
                continue
            source = record["source"]
            matches = [fid for fid, flow in unique_flows.items()
                       if (state["claimed"].get(fid) in (None, key)
                           or records[state["claimed"][fid]]["status"] == "matched")
                       and eligible_flow(flow, source, today)
                       and money(flow["cashflow_amount"]) == money(source["amount"])]
            candidates[key] = matches
        for key, matches in candidates.items():
            if len(matches) != 1 or sum(matches[0] in values for values in candidates.values()) != 1:
                continue
            record = records[key]
            if state["claimed"].get(matches[0]) not in (None, key):
                continue
            if record["status"] == "matched" and record["flows"] != matches:
                continue
            record.update(flows=matches, status="settled" if funds_usable else "matched")
            state["claimed"][matches[0]] = key
    state["covered_since"] = today.isoformat()
    state["generation"] = int(state.get("generation", 0)) + 1
    return state


def reserve_summary(state):
    totals = {}
    conflict = False
    statuses = {}
    for record in state.get("records", {}).values():
        conflict |= record["status"] == "conflict"
        if record["status"] not in {"settled", "cancelled"}:
            source = record["source"]
            currency = source["currency"]
            totals[currency] = totals.get(currency, Decimal(0)) + abs(money(source["amount"]))
            statuses.setdefault(currency, set()).add(record["status"])
    return {"state_fingerprint": fingerprint({key: state.get(key) for key in ("binding", "records", "claimed")}),
            "status": "unavailable" if conflict else "available",
            "reason": "withdrawal_source_conflict" if conflict else "ok",
            "reserved_by_currency": {k: float(v) for k, v in totals.items()},
            "statuses_by_currency": {k: sorted(v) for k, v in statuses.items()}}


def safe_after_save_failure(previous, proposed):
    """Only an unchanged source scope with no new or increased reserve can fall back."""
    if not previous or reserve_summary(proposed)["status"] != "available":
        return False
    for key, record in proposed["records"].items():
        old = previous["records"].get(key)
        if not old:
            return False
        if any(old["source"][k] != record["source"][k] for k in ("account", "broker", "currency")):
            return False
        amount = 0 if record["status"] in {"settled", "cancelled"} else abs(money(record["source"]["amount"]))
        old_amount = 0 if old["status"] in {"settled", "cancelled"} else abs(money(old["source"]["amount"]))
        if amount > old_amount:
            return False
    return reserve_summary(previous)["status"] == "available"


def correct(state, *, record_id, action, flow_ids, flows, today, reason):
    """Preview/confirm caller binds current source version and account authority."""
    if not reason.strip():
        raise ValueError("correction reason required")
    result = deepcopy(state)
    record = result["records"][record_id]
    source = record.get("proposed", record["source"])
    if record.get("foreign_owner") and action != "cancel":
        raise ValueError("resolve previous PM account reservation before correction")
    if record.get("source_missing") and action != "cancel":
        raise ValueError("current PM record missing; only explicit cancellation allowed")
    if money(source["amount"]) >= 0:
        raise ValueError("correct PM to a valid withdrawal before manual resolution")
    if action == "settle":
        if not flow_ids or len(set(flow_ids)) != len(flow_ids):
            raise ValueError("unique cash flow IDs required")
        chosen = [flows[fid] for fid in flow_ids]
        for fid in flow_ids:
            owner = result["claimed"].get(fid)
            if owner not in (None, record_id):
                old_record = result["records"][owner]
                if fid in old_record.get("flows", []):
                    raise ValueError("cash flow already claimed")
        if not all(eligible_flow(flow, source, today, manual=True) for flow in chosen):
            raise ValueError("cash flow not eligible")
        if sum((money(flow["cashflow_amount"]) for flow in chosen), Decimal(0)) != money(source["amount"]):
            raise ValueError("withdrawal amount mismatch")
        for fid in flow_ids:
            result["claimed"][fid] = record_id
        record.update(status="settled", flows=flow_ids)
    elif action == "restore":
        # Retain disputed claims. They can be reused only by explicit correction.
        record.update(status="manual", flows=[])
    elif action == "cancel":
        if record.get("flows"):
            raise ValueError("restore disputed settlement before cancellation")
        record["status"] = "cancelled"
    else:
        raise ValueError("unsupported correction")
    record.update(source=source, version=fingerprint(source))
    record.pop("proposed", None)
    result["generation"] = int(result.get("generation", 0)) + 1
    return result
