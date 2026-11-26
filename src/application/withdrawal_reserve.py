"""Account-scoped reservation workflow; no broker or PM writes."""
from concurrent.futures import CancelledError
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path

from domain.domain.withdrawal_reserve import (
    discovery_start, reconcile, reserve_summary, safe_after_save_failure,
)
from src.application.portfolio_management import resolve_portfolio_management_client
from src.application.runtime_paths import resolve_runtime_root
from src.infrastructure.private_storage import connect_private_sqlite


def database_path():
    root = resolve_runtime_root(repo_root=Path(__file__).resolve().parents[2]).runtime_root
    return root / "output_shared" / "state" / "withdrawal_reserves.sqlite3"


@contextmanager
def transaction(path, *, persist=True):
    if not persist:
        target = Path(path).resolve()
        if not target.exists():
            yield None
            return
        conn = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=30)
        try:
            yield conn
        finally:
            conn.close()
        return
    conn = connect_private_sqlite(path, timeout=30)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS account_reserves (account TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_state(conn, account):
    if conn is None:
        return None
    row = conn.execute("SELECT payload FROM account_reserves WHERE account=?", (account,)).fetchone()
    return json.loads(row[0]) if row else None


def save_state(conn, account, state):
    conn.execute("INSERT INTO account_reserves VALUES (?,?) ON CONFLICT(account) DO UPDATE SET payload=excluded.payload",
                 (account, json.dumps(state, ensure_ascii=False, sort_keys=True)))


def validate_pm(payload, account, now):
    freshness = payload.get("freshness", {})
    if (payload.get("schema_version") != "portfolio.cash_flows.v1" or payload.get("account") != account
            or payload.get("source_identity") != "pm.cash_flow" or payload.get("complete") is not True
            or payload.get("row_errors") != [] or not isinstance(payload.get("records"), list)
            or freshness.get("status") != "fresh" or freshness.get("trust_status") != "trusted"):
        raise ValueError("PM cash flow facts unavailable")
    observed = datetime.fromisoformat(str(freshness.get("observed_at_utc", "")).replace("Z", "+00:00"))
    if observed.tzinfo is None or not timedelta(0) <= now - observed <= timedelta(minutes=15):
        raise ValueError("PM cash flow facts stale")
    if any(row.get("account") != account for row in payload["records"]):
        raise ValueError("PM account mismatch")
    return payload["records"]


def collect_flows(gateway, sources, previous, *, account_id, environment, today, controls):
    dates = [date.fromisoformat(row["flow_date"]) for row in sources
             if row.get("broker") == "富途" and float(row.get("amount", 0)) < 0
             and discovery_start(previous, today) <= date.fromisoformat(row["flow_date"]) <= today]
    for record in (previous or {}).get("records", {}).values():
        if record["status"] not in {"settled", "cancelled"}:
            dates.append(date.fromisoformat(record.get("proposed", record["source"])["flow_date"]))
    if not dates:
        return []
    result = []
    current = min(dates)
    while current <= today:
        rows = gateway.get_cash_flows(acc_id=account_id, trd_env=environment,
                                      clearing_date=current.isoformat(), **controls)
        if hasattr(rows, "to_dict"):
            rows = rows.to_dict("records")
        if not isinstance(rows, list):
            raise ValueError("invalid broker cash flow response")
        for row in rows:
            if (str(row.get("acc_id", account_id)) != str(account_id)
                    or row.get("trd_env", environment) != environment
                    or row.get("clearing_date") != current.isoformat()):
                raise ValueError("broker cash flow scope mismatch")
            result.append(row)
        current += timedelta(days=1)
    return result


def observe(*, cfg, account, account_id, environment, gateway, funds_usable=True,
            controls=None, client=None, path=None, now=None, refresh_funds=None, persist=True):
    supplied_now = now
    now = now or datetime.now(timezone.utc)
    today = (now + timedelta(hours=8)).date()
    previous = proposed = None
    stage = "storage"
    try:
        with transaction(path or database_path(), persist=persist) as conn:
            previous = read_state(conn, account)
            stage = "pm"
            pm = resolve_portfolio_management_client(cfg, client=client)
            sources = validate_pm(pm.read_cash_flows(account=account, timeout=15), account,
                                  supplied_now or datetime.now(timezone.utc))
            stage = "flows"
            flow_complete = True
            try:
                flows = collect_flows(gateway, sources, previous, account_id=account_id,
                                      environment=environment, today=today, controls=controls or {})
            except (TimeoutError, InterruptedError, CancelledError):
                raise
            except Exception:
                flows, flow_complete = [], False
            if flows and refresh_funds is not None:
                funds_usable = refresh_funds() is True
            proposed = reconcile(previous, sources, flows, today=today, account=account,
                                 binding=f"futu:{environment}:{account_id}",
                                 flow_complete=flow_complete, funds_usable=funds_usable)
            stage = "storage"
            if not persist:
                if not proposed["records"]:
                    return {**reserve_summary(proposed), "flow_complete": flow_complete}
                if safe_after_save_failure(previous, proposed):
                    return {**reserve_summary(previous), "flow_complete": flow_complete}
                return {"status": "unavailable", "reason": "withdrawal_storage_unavailable",
                        "reserved_by_currency": {}}
            save_state(conn, account, proposed)
        summary = reserve_summary(proposed)
        summary.update(flow_complete=flow_complete, observed_at=now.isoformat(),
                       generation=proposed["generation"])
        return summary
    except (TimeoutError, InterruptedError, CancelledError):
        raise
    except Exception:
        if stage == "storage" and proposed is not None and safe_after_save_failure(previous, proposed):
            return {**reserve_summary(previous), "reason": "withdrawal_release_save_failed",
                    "flow_complete": False, "observed_at": now.isoformat()}
        return {"status": "unavailable", "reason": f"withdrawal_{stage}_unavailable",
                "reserved_by_currency": {}, "observed_at": now.isoformat()}


def apply(context, summary, balance_rows):
    """Only this boundary changes the shared effective cash used by capacities."""
    from math import isfinite

    context["withdrawal_reserve"] = summary
    context.pop("assignment_pending", None)
    if len(balance_rows) != 1:
        summary.update(status="unavailable", reason="pending_asset_unavailable")
        return
    row = balance_rows[0]
    currency = str(row.get("currency"))
    currency = "CNY" if currency in {"CNH", "RMB"} else currency
    try:
        pending = float(row["pending_asset"])
        if currency not in {"CNY", "USD", "HKD"} or not isfinite(pending) or pending < 0:
            raise ValueError("invalid pending")
    except (KeyError, TypeError, ValueError):
        summary.update(status="unavailable", reason="pending_asset_unavailable")
        return
    gross = dict(context["cash_by_currency"])
    gross[currency] = gross.get(currency, 0) + pending
    context["cash_gross_by_currency"] = gross
    context["cash_components_by_currency"].setdefault(currency, {})["pending_asset"] = pending
    effective = dict(gross)
    for ccy, amount in summary.get("reserved_by_currency", {}).items():
        effective[ccy] = effective.get(ccy, 0) - amount
        context["cash_components_by_currency"].setdefault(ccy, {})["withdrawal_reserve"] = -amount
    context["cash_by_currency"] = effective
    for ccy, amount in effective.items():
        context["cash_capacity_by_currency"].setdefault(ccy, {})["amount"] = amount
    context["cash_source"] = "futu_cash_with_pending_and_withdrawals"
