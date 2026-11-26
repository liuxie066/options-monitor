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


def foreign_record_ids(conn, account):
    """PM row identities cannot silently migrate between reservation accounts."""
    if conn is None:
        return set()
    return {key for (payload,) in conn.execute(
        "SELECT payload FROM account_reserves WHERE account<>?", (account,))
        for key, record in json.loads(payload)["records"].items()
        if record["status"] != "cancelled" and not record.get("foreign_owner")}


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
    funds_refresh_failed = False

    def qualified(summary):
        if funds_refresh_failed:
            summary.update(status="unavailable", reason="withdrawal_funds_unavailable")
        return summary

    try:
        with transaction(path or database_path(), persist=False) as conn:
            previous = read_state(conn, account)
            foreign_ids = foreign_record_ids(conn, account)
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
                try:
                    funds_usable = refresh_funds() is True
                except (TimeoutError, InterruptedError, CancelledError):
                    raise
                except Exception:
                    funds_usable = False
                funds_refresh_failed = not funds_usable
            proposed = reconcile(previous, sources, flows, today=today, account=account,
                                 binding=f"futu:{environment}:{account_id}",
                                 flow_complete=flow_complete, funds_usable=funds_usable,
                                 foreign_record_ids=foreign_ids)
            stage = "storage"
            if not persist:
                stage = "state_revalidation"
                # The provider read may overlap a manual restore or another market's observation.
                with transaction(path or database_path(), persist=False) as latest_reader:
                    if (read_state(latest_reader, account) != previous
                            or foreign_record_ids(latest_reader, account) != foreign_ids):
                        stage = "concurrent_state"
                        raise ValueError("reservation changed during read")
                if not proposed["records"]:
                    return qualified({**reserve_summary(proposed), "flow_complete": flow_complete})
                if safe_after_save_failure(previous, proposed):
                    return qualified({**reserve_summary(previous), "flow_complete": flow_complete})
                return {"status": "unavailable", "reason": "withdrawal_storage_unavailable",
                        "reserved_by_currency": {}}
            stage = "state_revalidation"
            with transaction(path or database_path()) as writer:
                if (read_state(writer, account) != previous
                        or foreign_record_ids(writer, account) != foreign_ids):
                    stage = "concurrent_state"
                    raise ValueError("reservation changed during observation")
                stage = "storage"
                save_state(writer, account, proposed)
        summary = reserve_summary(proposed)
        summary.update(flow_complete=flow_complete, observed_at=now.isoformat(),
                       generation=proposed["generation"])
        return qualified(summary)
    except (TimeoutError, InterruptedError, CancelledError):
        raise
    except Exception:
        if (stage == "storage" and not funds_refresh_failed and proposed is not None
                and safe_after_save_failure(previous, proposed)):
            return {**reserve_summary(previous), "reason": "withdrawal_release_save_failed",
                    "flow_complete": False, "observed_at": now.isoformat()}
        return {"status": "unavailable", "reason": f"withdrawal_{stage}_unavailable",
                "reserved_by_currency": {}, "observed_at": now.isoformat()}


def pending_cash_component(balance_rows):
    """Validate the aggregate pending field before any settlement can be released."""
    from math import isfinite

    if len(balance_rows) != 1:
        raise ValueError("pending snapshot missing")
    row = balance_rows[0]
    currency = str(row.get("currency"))
    currency = "CNY" if currency in {"CNH", "RMB"} else currency
    pending = float(row["pending_asset"])
    if isinstance(row["pending_asset"], bool) or currency not in {"CNY", "USD", "HKD"} or not isfinite(pending) or pending < 0:
        raise ValueError("invalid pending")
    return currency, pending


def apply(context, summary, balance_rows):
    """Only this boundary changes the shared effective cash used by capacities."""
    context["withdrawal_reserve"] = summary
    context.pop("assignment_pending", None)
    try:
        currency, pending = pending_cash_component(balance_rows)
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


def verify_current_withdrawals(*, cfg, account, context, runtime_root, client=None, now=None):
    """Requalify frozen capacity against current PM and durable reservations; no broker I/O."""
    supplied_now = now
    now = now or datetime.now(timezone.utc)
    authority = context["capacity_authority"]
    binding = f'futu:{authority["trd_env"]}:{authority["futu_account_id"]}'
    expected = context.get("withdrawal_reserve", {}).get("state_fingerprint")
    if not expected or not runtime_root:
        raise ValueError("withdrawal evidence missing; repreview required")
    pm = resolve_portfolio_management_client(cfg, client=client)
    sources = validate_pm(pm.read_cash_flows(account=account, timeout=15), account,
                          supplied_now or datetime.now(timezone.utc))
    path = Path(runtime_root) / "output_shared" / "state" / "withdrawal_reserves.sqlite3"
    with transaction(path, persist=False) as conn:
        previous = read_state(conn, account)
        if previous is None:
            raise ValueError("durable withdrawal state missing; repreview required")
        current = reconcile(previous, sources, [], today=(now + timedelta(hours=8)).date(),
                            account=account, binding=binding, flow_complete=False, funds_usable=False,
                            foreign_record_ids=foreign_record_ids(conn, account))
    summary = reserve_summary(current)
    if summary["status"] != "available" or summary["state_fingerprint"] != expected:
        raise ValueError("withdrawal evidence changed; repreview required")
